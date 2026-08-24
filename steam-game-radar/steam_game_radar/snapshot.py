"""Immutable radar snapshots and deterministic historical selection.

Snapshot I/O stays anchored to an opened directory descriptor. Absolute paths
must use canonical, non-symlink components; relative paths are resolved from an
opened descriptor for the active working directory. Callers serialize snapshot
operations with the project ``RunLock``. New snapshots are journaled before
publication and keep the journal link permanently, so recovery never needs a
name-based delete or rename.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import stat
from typing import Mapping, Sequence

from .artifacts import _serialize_json, _validate_run_id
from .config import RadarConfig
from .errors import InputValidationError, PersistenceError
from .schemas import (
    GameRecord,
    MAX_JSON_SAFE_INTEGER,
    MIN_JSON_SAFE_INTEGER,
    _freeze_json,
    _thaw_json,
    _utc_timestamp,
)


_SNAPSHOT_FIELDS = {
    "schema_version",
    "run_id",
    "observed_at",
    "records",
    "metadata",
}
_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
_MAX_JSON_NESTING = 128
_READ_CHUNK_BYTES = 64 * 1024
_JOURNAL_DIRECTORY = ".journal"


@dataclass(frozen=True)
class _ArtifactState:
    device: int
    inode: int
    owner: int
    mode: int
    size: int
    links: int


def make_run_id(now: datetime, entropy: bytes) -> str:
    """Create the canonical UTC run identifier from exactly 32 entropy bits."""

    utc_now = _aware_utc(now, "run timestamp")
    if not isinstance(entropy, bytes) or len(entropy) != 4:
        raise InputValidationError("run entropy must contain exactly four bytes")
    return f"{utc_now.strftime('%Y%m%dT%H%M%SZ')}-{entropy.hex()}"


def persist_snapshot(
    config: RadarConfig,
    run_id: str,
    records: Sequence[GameRecord],
    metadata: Mapping[str, object],
) -> Path:
    """Publish one versioned snapshot without replacing an existing run."""

    _validate_run_id(run_id)
    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "observed_at": _run_observed_at(run_id),
        "records": _canonical_records(records),
        "metadata": _canonical_metadata(metadata),
    }
    serialized = _serialize_json(payload).encode("utf-8")
    snapshot_root = Path(config.data_dir) / "snapshots"
    directory_descriptor = _open_directory(snapshot_root, create=True)
    if directory_descriptor is None:  # pragma: no cover - create=True contract
        raise PersistenceError("unable to create snapshot directory")
    try:
        journal_descriptor = _open_journal_directory(
            directory_descriptor,
            create=True,
        )
        if journal_descriptor is None:  # pragma: no cover - create=True contract
            raise PersistenceError("unable to create snapshot journal")
        try:
            _write_immutable(
                directory_descriptor,
                journal_descriptor,
                f"{run_id}.json",
                serialized,
            )
        finally:
            _close_descriptor(journal_descriptor)
    finally:
        _close_descriptor(directory_descriptor)
    return snapshot_root / f"{run_id}.json"


def load_snapshots(config: RadarConfig) -> Sequence[Mapping[str, object]]:
    """Load deeply immutable snapshots by explicit UTC observation time."""

    snapshot_root = Path(config.data_dir) / "snapshots"
    directory_descriptor = _open_directory(snapshot_root, create=False)
    if directory_descriptor is None:
        return ()
    journal_descriptor: int | None = None
    try:
        journal_descriptor = _open_journal_directory(
            directory_descriptor,
            create=False,
        )
        names = sorted(
            name
            for name in os.listdir(directory_descriptor)
            if name.endswith(".json")
        )
        loaded = [
            _load_snapshot(
                directory_descriptor,
                journal_descriptor,
                name,
            )
            for name in names
        ]
    except PersistenceError:
        raise
    except OSError as error:
        raise PersistenceError("unable to enumerate snapshots safely") from error
    finally:
        if journal_descriptor is not None:
            _close_descriptor(journal_descriptor)
        _close_descriptor(directory_descriptor)
    loaded.sort(
        key=lambda snapshot: (
            _snapshot_datetime(snapshot),
            snapshot["run_id"],
        )
    )
    return tuple(loaded)


def select_comparison(
    snapshots: Sequence[Mapping[str, object]],
    current_time: datetime,
    target_hours: int,
    minimum_hours: int,
    maximum_hours: int,
) -> Mapping[str, object] | None:
    """Select history closest to a target age, preferring newer on ties."""

    current = _aware_utc(current_time, "current_time")
    _validate_window(target_hours, minimum_hours, maximum_hours)
    if isinstance(snapshots, (str, bytes)) or not isinstance(snapshots, Sequence):
        raise InputValidationError("snapshots must be a sequence")

    selected: Mapping[str, object] | None = None
    selected_key: tuple[float, float] | None = None
    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping):
            raise InputValidationError("each snapshot must be a mapping")
        observed_at = _snapshot_datetime(snapshot)
        age_hours = (current - observed_at).total_seconds() / 3_600
        if age_hours < minimum_hours or age_hours > maximum_hours:
            continue
        key = (abs(age_hours - target_hours), -observed_at.timestamp())
        if selected_key is None or key < selected_key:
            selected = snapshot
            selected_key = key
    return selected


def _open_directory(path: Path, *, create: bool) -> int | None:
    if ".." in path.parts:
        raise InputValidationError("snapshot path must not traverse parent directories")
    try:
        if path.is_absolute():
            descriptor = os.open(os.path.sep, _directory_flags())
            components = path.parts[1:]
        else:
            descriptor = os.open(".", _directory_flags())
            components = path.parts
    except OSError as error:
        raise PersistenceError("unable to open trusted snapshot-path root") from error

    try:
        for component in components:
            if component in {"", "."}:
                continue
            next_descriptor = _open_child_directory(
                descriptor,
                component,
                create=create,
                require_mode=False,
            )
            if next_descriptor is None:
                _close_descriptor(descriptor)
                return None
            old_descriptor = descriptor
            try:
                os.close(old_descriptor)
            except OSError as error:
                _close_descriptor(next_descriptor)
                descriptor = -1
                raise PersistenceError(
                    "unable to close traversed snapshot directory"
                ) from error
            descriptor = next_descriptor
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise PersistenceError("snapshot root is not a directory")
        return descriptor
    except Exception as error:
        if descriptor >= 0:
            _close_descriptor(descriptor)
        if isinstance(error, (InputValidationError, PersistenceError)):
            raise
        raise PersistenceError("unable to securely traverse snapshot path") from error


def _open_journal_directory(
    snapshots_descriptor: int,
    *,
    create: bool,
) -> int | None:
    return _open_child_directory(
        snapshots_descriptor,
        _JOURNAL_DIRECTORY,
        create=create,
        require_mode=True,
    )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_child_directory(
    parent_descriptor: int,
    name: str,
    *,
    create: bool,
    require_mode: bool,
) -> int | None:
    created_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        except OSError as error:
            raise PersistenceError("unable to create snapshot directory") from error
        else:
            try:
                created = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                _validate_directory(created, exact_mode=None)
                created_identity = _inode(created)
                os.chmod(
                    name,
                    0o700,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                secured = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                _validate_directory(secured, exact_mode=0o700)
                if _inode(secured) != created_identity:
                    raise PersistenceError(
                        "snapshot directory changed while it was secured"
                    )
            except PersistenceError:
                raise
            except OSError as error:
                raise PersistenceError(
                    "unable to secure snapshot directory"
                ) from error
        try:
            descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
        except OSError as error:
            raise PersistenceError("unable to open snapshot directory") from error
    except OSError as error:
        raise PersistenceError("unable to open snapshot directory") from error

    try:
        opened = os.fstat(descriptor)
        _validate_directory(
            opened,
            exact_mode=0o700 if require_mode else None,
            require_owner=require_mode or created_identity is not None,
        )
        if created_identity is not None and _inode(opened) != created_identity:
            raise PersistenceError("new snapshot directory changed before open")
        if require_mode:
            named = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            _validate_directory(named, exact_mode=0o700)
            if _inode(opened) != _inode(named):
                raise PersistenceError("snapshot journal changed while it was opened")
        return descriptor
    except (OSError, PersistenceError) as error:
        _close_descriptor(descriptor)
        if isinstance(error, PersistenceError):
            raise
        raise PersistenceError("unable to verify snapshot directory") from error


def _validate_directory(
    metadata: os.stat_result,
    *,
    exact_mode: int | None,
    require_owner: bool = True,
) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise PersistenceError("snapshot artifact is not a directory")
    if exact_mode is not None and stat.S_IMODE(metadata.st_mode) != exact_mode:
        raise PersistenceError("snapshot directory must use mode 0700")
    if (
        require_owner
        and hasattr(os, "geteuid")
        and metadata.st_uid != os.geteuid()
    ):
        raise PersistenceError("snapshot directory has an unexpected owner")


def _write_immutable(
    snapshots_descriptor: int,
    journal_descriptor: int,
    destination_name: str,
    serialized: bytes,
) -> None:
    if len(serialized) > _MAX_SNAPSHOT_BYTES:
        raise PersistenceError("snapshot exceeds the safe size limit")
    try:
        snapshots_status = os.fstat(snapshots_descriptor)
        journal_status = os.fstat(journal_descriptor)
        _validate_directory(
            snapshots_status,
            exact_mode=None,
            require_owner=False,
        )
        _validate_directory(journal_status, exact_mode=0o700)
    except PersistenceError:
        raise
    except OSError as error:
        raise PersistenceError("unable to inspect snapshot directories") from error
    snapshots_identity = _inode(snapshots_status)
    journal_identity = _inode(journal_status)
    if _optional_name_metadata(snapshots_descriptor, destination_name) is not None:
        raise PersistenceError("snapshot already exists")

    expected_entry = _create_journal_entry(
        journal_descriptor,
        destination_name,
        serialized,
    )
    _validate_journal_directory_binding(
        snapshots_descriptor,
        journal_descriptor,
        snapshots_identity,
        journal_identity,
    )
    journal_data, journal = _read_named_file(
        journal_descriptor,
        destination_name,
        expected_links=1,
    )
    _require_artifact_state(journal, expected_entry, expected_links=1)
    if journal_data != serialized:
        raise PersistenceError("snapshot journal payload changed before publish")
    if _optional_name_metadata(snapshots_descriptor, destination_name) is not None:
        raise PersistenceError("snapshot already exists")
    _validate_journal_directory_binding(
        snapshots_descriptor,
        journal_descriptor,
        snapshots_identity,
        journal_identity,
    )
    try:
        os.link(
            destination_name,
            destination_name,
            src_dir_fd=journal_descriptor,
            dst_dir_fd=snapshots_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        if error.errno == errno.EEXIST:
            raise PersistenceError("snapshot already exists") from error
        raise PersistenceError("unable to atomically publish snapshot") from error

    _validate_journal_directory_binding(
        snapshots_descriptor,
        journal_descriptor,
        snapshots_identity,
        journal_identity,
    )
    journal_after = _inspect_named_file(
        journal_descriptor,
        destination_name,
        expected_links=2,
    )
    _require_artifact_state(journal_after, expected_entry, expected_links=2)
    published_data, published = _read_named_file(
        snapshots_descriptor,
        destination_name,
        expected_links=2,
    )
    _require_artifact_state(published, expected_entry, expected_links=2)
    if published_data != serialized:
        raise PersistenceError("published snapshot payload changed")
    _validate_journal_directory_binding(
        snapshots_descriptor,
        journal_descriptor,
        snapshots_identity,
        journal_identity,
    )
    try:
        os.fsync(snapshots_descriptor)
    except OSError as error:
        raise PersistenceError("unable to sync published snapshot") from error
    _validate_journal_directory_binding(
        snapshots_descriptor,
        journal_descriptor,
        snapshots_identity,
        journal_identity,
    )


def _create_journal_entry(
    journal_descriptor: int,
    name: str,
    serialized: bytes,
) -> _ArtifactState:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(
            name,
            flags,
            0o600,
            dir_fd=journal_descriptor,
        )
    except FileExistsError:
        existing, metadata = _read_named_file(
            journal_descriptor,
            name,
            expected_links=1,
        )
        if existing != serialized:
            raise PersistenceError(
                "snapshot journal conflicts with the requested payload"
            )
        return _artifact_state(metadata)
    except OSError as error:
        raise PersistenceError("unable to create snapshot journal entry") from error

    try:
        os.fchmod(descriptor, 0o600)
        created = os.fstat(descriptor)
        _validate_regular(created, expected_links=1, expected_size=0)
        identity = _inode(created)
        named = _name_metadata(journal_descriptor, name)
        _validate_regular(named, expected_links=1, expected_size=0)
        if _inode(named) != identity:
            raise PersistenceError("snapshot journal entry changed during creation")
        _write_all(descriptor, serialized)
        os.fsync(descriptor)
        written = os.fstat(descriptor)
        named_after = _name_metadata(journal_descriptor, name)
        _validate_regular(
            written,
            expected_links=1,
            expected_size=len(serialized),
        )
        _validate_regular(
            named_after,
            expected_links=1,
            expected_size=len(serialized),
        )
        if _inode(written) != identity or _inode(named_after) != identity:
            raise PersistenceError("snapshot journal entry changed during write")
        if _artifact_state(written) != _artifact_state(named_after):
            raise PersistenceError("snapshot journal entry metadata changed")
        os.fsync(journal_descriptor)
        return _artifact_state(written)
    except PersistenceError:
        raise
    except OSError as error:
        raise PersistenceError("unable to write snapshot journal entry") from error
    finally:
        _close_descriptor(descriptor)


def _write_all(descriptor: int, value: bytes) -> None:
    remaining = memoryview(value)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise PersistenceError("snapshot write made no progress")
        remaining = remaining[written:]


def _load_snapshot(
    snapshots_descriptor: int,
    journal_descriptor: int | None,
    name: str,
) -> Mapping[str, object]:
    if not isinstance(name, str) or not name.endswith(".json"):
        raise PersistenceError("snapshot filename is invalid")
    run_id = name[:-5]
    try:
        _validate_run_id(run_id)
    except InputValidationError as error:
        raise PersistenceError("snapshot filename has an invalid run ID") from error

    before = _name_metadata(snapshots_descriptor, name)
    _validate_regular(before, expected_links=None)
    if before.st_nlink not in {1, 2}:
        raise PersistenceError("snapshot artifact has an unexpected link count")
    if before.st_nlink == 1:
        _require_journal_entry_absent(
            snapshots_descriptor,
            journal_descriptor,
            name,
        )
    else:
        _validate_journal_link(
            snapshots_descriptor,
            journal_descriptor,
            name,
            before,
        )

    data, opened = _read_named_file(
        snapshots_descriptor,
        name,
        expected_links=before.st_nlink,
    )
    if _file_identity(opened) != _file_identity(before):
        raise PersistenceError("snapshot changed before it was read")
    if opened.st_nlink == 1:
        _require_journal_entry_absent(
            snapshots_descriptor,
            journal_descriptor,
            name,
        )
    else:
        _validate_journal_link(
            snapshots_descriptor,
            journal_descriptor,
            name,
            opened,
        )

    raw = _parse_snapshot_json(data)
    canonical = _validate_snapshot(raw, name)
    try:
        frozen = _freeze_json(canonical, "snapshot")
    except (InputValidationError, RecursionError) as error:
        raise PersistenceError("snapshot cannot be frozen safely") from error
    if not isinstance(frozen, Mapping):  # pragma: no cover - canonical is a dict
        raise PersistenceError("snapshot has an invalid canonical shape")
    return frozen


def _validate_journal_link(
    snapshots_descriptor: int,
    journal_descriptor: int | None,
    name: str,
    final: os.stat_result,
) -> None:
    if journal_descriptor is None:
        raise PersistenceError("linked snapshot does not have a journal")
    _validate_journal_directory_binding(
        snapshots_descriptor,
        journal_descriptor,
    )
    journal = _inspect_named_file(
        journal_descriptor,
        name,
        expected_links=2,
    )
    _validate_regular(final, expected_links=2, expected_size=journal.st_size)
    if _file_identity(journal) != _file_identity(final):
        raise PersistenceError("snapshot journal does not match its final link")


def _require_journal_entry_absent(
    snapshots_descriptor: int,
    journal_descriptor: int | None,
    name: str,
) -> None:
    if journal_descriptor is None:
        if (
            _optional_name_metadata(
                snapshots_descriptor,
                _JOURNAL_DIRECTORY,
            )
            is not None
        ):
            raise PersistenceError("snapshot journal appeared during legacy load")
        return
    _validate_journal_directory_binding(
        snapshots_descriptor,
        journal_descriptor,
    )
    if _optional_name_metadata(journal_descriptor, name) is not None:
        raise PersistenceError("legacy snapshot conflicts with a journal entry")


def _validate_journal_directory_binding(
    snapshots_descriptor: int,
    journal_descriptor: int,
    expected_snapshots: tuple[int, int] | None = None,
    expected_journal: tuple[int, int] | None = None,
) -> None:
    try:
        snapshots = os.fstat(snapshots_descriptor)
        opened = os.fstat(journal_descriptor)
        named = os.stat(
            _JOURNAL_DIRECTORY,
            dir_fd=snapshots_descriptor,
            follow_symlinks=False,
        )
        _validate_directory(
            snapshots,
            exact_mode=None,
            require_owner=False,
        )
        _validate_directory(opened, exact_mode=0o700)
        _validate_directory(named, exact_mode=0o700)
    except PersistenceError:
        raise
    except OSError as error:
        raise PersistenceError("unable to verify snapshot journal") from error
    if expected_snapshots is not None and _inode(snapshots) != expected_snapshots:
        raise PersistenceError("snapshot directory identity changed")
    if expected_journal is not None and _inode(opened) != expected_journal:
        raise PersistenceError("opened snapshot journal identity changed")
    if _inode(opened) != _inode(named):
        raise PersistenceError("snapshot journal directory binding changed")


def _inspect_named_file(
    directory_descriptor: int,
    name: str,
    *,
    expected_links: int,
) -> os.stat_result:
    _, metadata = _access_named_file(
        directory_descriptor,
        name,
        expected_links=expected_links,
        read_content=False,
    )
    return metadata


def _read_named_file(
    directory_descriptor: int,
    name: str,
    *,
    expected_links: int,
) -> tuple[bytes, os.stat_result]:
    data, metadata = _access_named_file(
        directory_descriptor,
        name,
        expected_links=expected_links,
        read_content=True,
    )
    if data is None:  # pragma: no cover - read_content=True contract
        raise PersistenceError("snapshot content was not read")
    return data, metadata


def _access_named_file(
    directory_descriptor: int,
    name: str,
    *,
    expected_links: int,
    read_content: bool,
) -> tuple[bytes | None, os.stat_result]:
    before = _name_metadata(directory_descriptor, name)
    _validate_regular(before, expected_links=expected_links)
    descriptor = _open_snapshot_file(directory_descriptor, name)
    try:
        opened = os.fstat(descriptor)
        _validate_regular(opened, expected_links=expected_links)
        if _file_identity(before) != _file_identity(opened):
            raise PersistenceError("snapshot changed while it was opened")
        data = _read_exact(descriptor, opened.st_size) if read_content else None
        after_read = os.fstat(descriptor)
        _validate_regular(after_read, expected_links=expected_links)
        if _file_identity(after_read) != _file_identity(opened):
            raise PersistenceError("snapshot changed while it was accessed")
        named_after = _name_metadata(directory_descriptor, name)
        _validate_regular(named_after, expected_links=expected_links)
        if _file_identity(named_after) != _file_identity(opened):
            raise PersistenceError("snapshot name changed while it was accessed")
        return data, after_read
    except PersistenceError:
        raise
    except OSError as error:
        raise PersistenceError("unable to safely access snapshot") from error
    finally:
        _close_descriptor(descriptor)


def _open_snapshot_file(directory_descriptor: int, name: str) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        return os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as error:
        raise PersistenceError("unable to securely open snapshot") from error


def _read_exact(descriptor: int, size: int) -> bytes:
    if size < 0 or size > _MAX_SNAPSHOT_BYTES:
        raise PersistenceError("snapshot exceeds the safe read limit")
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
        if not chunk:
            raise PersistenceError("snapshot ended before its declared size")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise PersistenceError("snapshot grew while it was read")
    return b"".join(chunks)


def _parse_snapshot_json(data: bytes) -> object:
    try:
        text = data.decode("utf-8", errors="strict")
        return json.loads(
            text,
            parse_int=_parse_json_integer,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise PersistenceError("unable to parse snapshot JSON") from error


def _validate_snapshot(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _SNAPSHOT_FIELDS:
        raise PersistenceError("snapshot has an invalid top-level schema")
    if (
        isinstance(value["schema_version"], bool)
        or not isinstance(value["schema_version"], int)
        or value["schema_version"] != 1
    ):
        raise PersistenceError("snapshot has an unsupported schema version")
    run_id = value["run_id"]
    try:
        _validate_run_id(run_id)
    except InputValidationError as error:
        raise PersistenceError("snapshot has an invalid run ID") from error
    if name != f"{run_id}.json":
        raise PersistenceError("snapshot filename does not match its run ID")

    observed_at = value["observed_at"]
    try:
        _utc_timestamp(observed_at)
    except InputValidationError as error:
        raise PersistenceError(
            "snapshot has an invalid UTC observation time"
        ) from error
    if observed_at != _run_observed_at(run_id):
        raise PersistenceError("snapshot time does not match its run ID")

    raw_records = value["records"]
    if not isinstance(raw_records, list):
        raise PersistenceError("snapshot records must be a JSON array")
    try:
        records = [GameRecord.from_dict(record).to_dict() for record in raw_records]
        metadata = _canonical_metadata(value["metadata"])
    except (
        InputValidationError,
        AttributeError,
        TypeError,
        RecursionError,
    ) as error:
        raise PersistenceError(
            "snapshot contains invalid records or metadata"
        ) from error
    return {
        "schema_version": 1,
        "run_id": run_id,
        "observed_at": observed_at,
        "records": records,
        "metadata": metadata,
    }


def _validate_regular(
    metadata: os.stat_result,
    *,
    expected_links: int | None,
    expected_size: int | None = None,
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise PersistenceError("snapshot artifact is not a regular file")
    if expected_links is not None and metadata.st_nlink != expected_links:
        raise PersistenceError("snapshot artifact has an unexpected link count")
    if expected_size is not None and metadata.st_size != expected_size:
        raise PersistenceError("snapshot artifact has an unexpected size")
    if metadata.st_size < 0 or metadata.st_size > _MAX_SNAPSHOT_BYTES:
        raise PersistenceError("snapshot exceeds the safe size limit")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise PersistenceError("snapshot artifact must use mode 0600")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PersistenceError("snapshot artifact has an unexpected owner")


def _name_metadata(directory_descriptor: int, name: str) -> os.stat_result:
    try:
        return os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise PersistenceError("unable to inspect snapshot artifact name") from error


def _optional_name_metadata(
    directory_descriptor: int,
    name: str,
) -> os.stat_result | None:
    try:
        return os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PersistenceError("unable to inspect snapshot artifact name") from error


def _inode(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _artifact_state(metadata: os.stat_result) -> _ArtifactState:
    return _ArtifactState(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        owner=metadata.st_uid,
        mode=stat.S_IMODE(metadata.st_mode),
        size=metadata.st_size,
        links=metadata.st_nlink,
    )


def _require_artifact_state(
    metadata: os.stat_result,
    expected: _ArtifactState,
    *,
    expected_links: int,
) -> None:
    _validate_regular(
        metadata,
        expected_links=expected_links,
        expected_size=expected.size,
    )
    required = _ArtifactState(
        device=expected.device,
        inode=expected.inode,
        owner=expected.owner,
        mode=expected.mode,
        size=expected.size,
        links=expected_links,
    )
    if _artifact_state(metadata) != required:
        raise PersistenceError("snapshot artifact identity changed")


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _close_descriptor(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _canonical_records(records: Sequence[GameRecord]) -> list[dict[str, object]]:
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise InputValidationError("records must be a sequence")
    canonical: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, GameRecord):
            raise InputValidationError("records must contain GameRecord values")
        canonical.append(record.to_dict())
    return canonical


def _canonical_metadata(metadata: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(metadata, Mapping):
        raise InputValidationError("snapshot metadata must be a mapping")
    try:
        _validate_metadata_structure(metadata, depth=0, active=set())
        frozen = _freeze_json(metadata, "snapshot metadata")
        thawed = _thaw_json(frozen)
    except RecursionError as error:
        raise InputValidationError(
            "snapshot metadata nesting is too deep"
        ) from error
    if not isinstance(thawed, dict):
        raise InputValidationError("snapshot metadata must be a mapping")
    return thawed


def _validate_metadata_structure(
    value: object,
    *,
    depth: int,
    active: set[int],
) -> None:
    if depth > _MAX_JSON_NESTING:
        raise InputValidationError("snapshot metadata nesting is too deep")
    if not isinstance(value, (Mapping, list)):
        return
    identity = id(value)
    if identity in active:
        raise InputValidationError("snapshot metadata must not contain cycles")
    active.add(identity)
    try:
        nested_values = value.values() if isinstance(value, Mapping) else value
        for nested in nested_values:
            _validate_metadata_structure(
                nested,
                depth=depth + 1,
                active=active,
            )
    finally:
        active.remove(identity)


def _snapshot_datetime(snapshot: Mapping[str, object]) -> datetime:
    observed_at = snapshot.get("observed_at")
    try:
        value = _utc_timestamp(observed_at)
    except InputValidationError as error:
        raise InputValidationError(
            "snapshot observed_at must be an ISO-8601 UTC timestamp"
        ) from error
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _run_observed_at(run_id: str) -> str:
    parsed = datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ").replace(
        tzinfo=timezone.utc
    )
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _aware_utc(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise InputValidationError(f"{name} must include a timezone")
    return value.astimezone(timezone.utc)


def _validate_window(target: int, minimum: int, maximum: int) -> None:
    for name, value in (
        ("target_hours", target),
        ("minimum_hours", minimum),
        ("maximum_hours", maximum),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InputValidationError(f"{name} must be a non-negative integer")
    if minimum > target or target > maximum:
        raise InputValidationError(
            "comparison hours must satisfy minimum <= target <= maximum"
        )


def _parse_json_integer(value: str) -> int:
    negative = value.startswith("-")
    digits = value[1:] if negative else value
    if not digits or any(character < "0" or character > "9" for character in digits):
        raise ValueError("snapshot integer is malformed")
    normalized = digits.lstrip("0") or "0"
    limit = str(
        abs(MIN_JSON_SAFE_INTEGER) if negative else MAX_JSON_SAFE_INTEGER
    )
    if len(normalized) > len(limit) or (
        len(normalized) == len(limit) and normalized > limit
    ):
        raise ValueError("snapshot integer is outside the JSON-safe range")
    parsed = int(normalized)
    return -parsed if negative else parsed


def _reject_json_constant(value: str) -> float:
    raise ValueError(f"invalid JSON numeric constant: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("snapshot JSON contains duplicate object keys")
        value[key] = item
    return value
