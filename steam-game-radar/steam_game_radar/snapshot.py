"""Immutable radar snapshots and deterministic historical selection.

Snapshot I/O stays anchored to an opened directory descriptor. Absolute paths
must use canonical, non-symlink components; relative paths are resolved from an
opened descriptor for the active working directory. Callers serialize snapshot
operations with the project ``RunLock``; orphan-stage recovery is nevertheless
idempotent if it overlaps a publisher after the final hard link exists.
"""

from __future__ import annotations

from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import re
import secrets
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
_TEMP_ATTEMPTS = 8
_READ_CHUNK_BYTES = 64 * 1024
_STAGED_NAME = re.compile(r"\.snapshot-[0-9a-f]{24}\.staged\Z", re.ASCII)
_QUARANTINE_NAME = re.compile(
    r"\.quarantine-[0-9a-f]{48}\.holding\Z",
    re.ASCII,
)


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
        _write_immutable(
            directory_descriptor,
            f"{run_id}.json",
            serialized,
        )
    finally:
        _close_descriptor(directory_descriptor)
    return snapshot_root / f"{run_id}.json"


def load_snapshots(config: RadarConfig) -> Sequence[Mapping[str, object]]:
    """Load deeply immutable snapshots by explicit UTC observation time."""

    snapshot_root = Path(config.data_dir) / "snapshots"
    directory_descriptor = _open_directory(snapshot_root, create=False)
    if directory_descriptor is None:
        return ()
    try:
        entries = os.listdir(directory_descriptor)
        _recover_orphaned_stages(directory_descriptor, entries)
        names = sorted(
            name
            for name in os.listdir(directory_descriptor)
            if name.endswith(".json")
        )
        loaded = [
            _load_snapshot(directory_descriptor, name)
            for name in names
        ]
    except PersistenceError:
        raise
    except OSError as error:
        raise PersistenceError("unable to enumerate snapshots safely") from error
    finally:
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
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        if path.is_absolute():
            descriptor = os.open(os.path.sep, flags)
            components = path.parts[1:]
        else:
            descriptor = os.open(".", flags)
            components = path.parts
    except OSError as error:
        raise PersistenceError("unable to open trusted snapshot-path root") from error

    try:
        for component in components:
            if component in {"", "."}:
                continue
            created_directory_identity: tuple[int, int] | None = None
            try:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    _close_descriptor(descriptor)
                    return None
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                except OSError as error:
                    raise PersistenceError(
                        "unable to create snapshot directory component"
                    ) from error
                else:
                    try:
                        created_status = os.stat(
                            component,
                            dir_fd=descriptor,
                            follow_symlinks=False,
                        )
                        if not stat.S_ISDIR(created_status.st_mode):
                            raise PersistenceError(
                                "new snapshot path component is not a directory"
                            )
                        if (
                            hasattr(os, "geteuid")
                            and created_status.st_uid != os.geteuid()
                        ):
                            raise PersistenceError(
                                "new snapshot directory has an unexpected owner"
                            )
                        created_directory_identity = _inode(created_status)
                        os.chmod(
                            component,
                            0o700,
                            dir_fd=descriptor,
                            follow_symlinks=False,
                        )
                        secured_status = os.stat(
                            component,
                            dir_fd=descriptor,
                            follow_symlinks=False,
                        )
                        if (
                            _inode(secured_status) != created_directory_identity
                            or stat.S_IMODE(secured_status.st_mode) != 0o700
                        ):
                            raise PersistenceError(
                                "new snapshot directory changed during setup"
                            )
                    except PersistenceError:
                        raise
                    except OSError as error:
                        raise PersistenceError(
                            "unable to secure snapshot directory component"
                        ) from error
                try:
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                except OSError as error:
                    raise PersistenceError(
                        "unable to securely open snapshot directory component"
                    ) from error
            except OSError as error:
                raise PersistenceError(
                    "unable to securely open snapshot directory component"
                ) from error
            try:
                opened_status = os.fstat(next_descriptor)
                if not stat.S_ISDIR(opened_status.st_mode):
                    raise PersistenceError(
                        "snapshot path component is not a directory"
                    )
                if (
                    created_directory_identity is not None
                    and _inode(opened_status) != created_directory_identity
                ):
                    raise PersistenceError(
                        "new snapshot directory changed before open"
                    )
            except Exception:
                _close_descriptor(next_descriptor)
                raise
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


def _write_immutable(
    directory_descriptor: int,
    destination_name: str,
    serialized: bytes,
) -> None:
    if len(serialized) > _MAX_SNAPSHOT_BYTES:
        raise PersistenceError("snapshot exceeds the safe size limit")
    temporary_name, descriptor = _create_temporary(directory_descriptor)
    created_identity: tuple[int, int] | None = None
    published = False
    try:
        before = os.fstat(descriptor)
        _validate_regular(before, expected_links=1, expected_size=0)
        created_identity = _inode(before)
        _write_all(descriptor, serialized)
        os.fsync(descriptor)
        written = os.fstat(descriptor)
        _validate_regular(
            written,
            expected_links=1,
            expected_size=len(serialized),
        )
        if _inode(written) != created_identity:
            raise PersistenceError("snapshot temporary file changed during write")
        named_temporary = _name_metadata(directory_descriptor, temporary_name)
        _validate_regular(
            named_temporary,
            expected_links=1,
            expected_size=len(serialized),
        )
        if _inode(named_temporary) != created_identity:
            raise PersistenceError("snapshot temporary name changed before publish")

        try:
            os.link(
                temporary_name,
                destination_name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            if error.errno == errno.EEXIST:
                raise PersistenceError("snapshot already exists") from error
            raise PersistenceError("unable to atomically publish snapshot") from error
        published = True

        named_destination = _name_metadata(directory_descriptor, destination_name)
        _validate_regular(
            named_destination,
            expected_links=2,
            expected_size=len(serialized),
        )
        if _inode(named_destination) != created_identity:
            raise PersistenceError("published snapshot has an unexpected identity")
        temporary_after_link = _name_metadata(
            directory_descriptor,
            temporary_name,
        )
        if _inode(temporary_after_link) != created_identity:
            raise PersistenceError("snapshot temporary name changed during publish")

        _quarantine_remove_expected(
            directory_descriptor,
            temporary_name,
            created_identity,
            missing_ok=True,
        )
        temporary_name = ""
        final = _name_metadata(directory_descriptor, destination_name)
        _validate_regular(
            final,
            expected_links=1,
            expected_size=len(serialized),
        )
        if _inode(final) != created_identity:
            raise PersistenceError("published snapshot changed before completion")
        os.fsync(directory_descriptor)
    except (InputValidationError, PersistenceError, OSError) as error:
        if published and created_identity is not None:
            _quarantine_remove_expected(
                directory_descriptor,
                destination_name,
                created_identity,
                missing_ok=True,
            )
        if isinstance(error, (InputValidationError, PersistenceError)):
            raise
        raise PersistenceError("unable to persist immutable snapshot") from error
    finally:
        _close_descriptor(descriptor)
        if temporary_name and created_identity is not None:
            _quarantine_remove_expected(
                directory_descriptor,
                temporary_name,
                created_identity,
                missing_ok=True,
            )


def _create_temporary(directory_descriptor: int) -> tuple[str, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    for _ in range(_TEMP_ATTEMPTS):
        name = f".snapshot-{secrets.token_hex(12)}.staged"
        try:
            descriptor = os.open(
                name,
                flags,
                0o600,
                dir_fd=directory_descriptor,
            )
        except FileExistsError:
            continue
        except OSError as error:
            raise PersistenceError(
                "unable to create snapshot temporary file"
            ) from error
        identity: tuple[int, int] | None = None
        try:
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            identity = _inode(metadata)
            _validate_regular(metadata, expected_links=1, expected_size=0)
            named = _name_metadata(directory_descriptor, name)
            _validate_regular(named, expected_links=1, expected_size=0)
            if _inode(named) != identity:
                raise PersistenceError(
                    "snapshot temporary name changed during creation"
                )
        except (OSError, PersistenceError) as error:
            _close_descriptor(descriptor)
            if identity is not None:
                _quarantine_remove_expected(
                    directory_descriptor,
                    name,
                    identity,
                    missing_ok=True,
                )
            if isinstance(error, PersistenceError):
                raise
            raise PersistenceError(
                "unable to secure snapshot temporary file"
            ) from error
        return name, descriptor
    raise PersistenceError("unable to allocate snapshot temporary file")


def _write_all(descriptor: int, value: bytes) -> None:
    remaining = memoryview(value)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise PersistenceError("snapshot write made no progress")
        remaining = remaining[written:]


def _load_snapshot(
    directory_descriptor: int,
    name: str,
) -> Mapping[str, object]:
    if not isinstance(name, str) or not name.endswith(".json"):
        raise PersistenceError("snapshot filename is invalid")
    run_id = name[:-5]
    try:
        _validate_run_id(run_id)
    except InputValidationError as error:
        raise PersistenceError("snapshot filename has an invalid run ID") from error

    before = _name_metadata(directory_descriptor, name)
    _validate_regular(before, expected_links=1)
    descriptor = _open_snapshot_file(directory_descriptor, name)
    try:
        opened = os.fstat(descriptor)
        _validate_regular(opened, expected_links=1)
        if _file_identity(before) != _file_identity(opened):
            raise PersistenceError("snapshot changed while it was opened")
        data = _read_exact(descriptor, opened.st_size)
        after_read = os.fstat(descriptor)
        if _file_identity(after_read) != _file_identity(opened):
            raise PersistenceError("snapshot changed while it was read")
        named_after = _name_metadata(directory_descriptor, name)
        if _file_identity(named_after) != _file_identity(opened):
            raise PersistenceError("snapshot name changed while it was read")
    except PersistenceError:
        raise
    except OSError as error:
        raise PersistenceError("unable to safely read snapshot") from error
    finally:
        _close_descriptor(descriptor)

    raw = _parse_snapshot_json(data)
    canonical = _validate_snapshot(raw, name)
    try:
        frozen = _freeze_json(canonical, "snapshot")
    except (InputValidationError, RecursionError) as error:
        raise PersistenceError("snapshot cannot be frozen safely") from error
    if not isinstance(frozen, Mapping):  # pragma: no cover - canonical is a dict
        raise PersistenceError("snapshot has an invalid canonical shape")
    return frozen


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


def _recover_orphaned_stages(
    directory_descriptor: int,
    entries: Sequence[str],
) -> None:
    final_names = [name for name in entries if _is_final_name(name)]
    for staged_name in sorted(
        name
        for name in entries
        if _STAGED_NAME.fullmatch(name) is not None
        or _QUARANTINE_NAME.fullmatch(name) is not None
    ):
        staged = _name_metadata(directory_descriptor, staged_name)
        if (
            staged.st_nlink != 2
            and _QUARANTINE_NAME.fullmatch(staged_name) is not None
        ):
            continue
        _validate_regular(staged, expected_links=2)
        identity = _inode(staged)
        matching_finals: list[tuple[str, os.stat_result]] = []
        for final_name in final_names:
            final = _name_metadata(directory_descriptor, final_name)
            if _inode(final) == identity:
                matching_finals.append((final_name, final))
        if len(matching_finals) != 1:
            raise PersistenceError(
                "orphan snapshot stage does not have exactly one final link"
            )
        final_name, final_before = matching_finals[0]
        _validate_regular(
            final_before,
            expected_links=2,
            expected_size=staged.st_size,
        )
        _quarantine_remove_expected(
            directory_descriptor,
            staged_name,
            identity,
            missing_ok=True,
        )
        final_after = _name_metadata(directory_descriptor, final_name)
        _validate_regular(
            final_after,
            expected_links=1,
            expected_size=staged.st_size,
        )
        if _inode(final_after) != identity:
            raise PersistenceError(
                "recovered snapshot final changed identity"
            )
        os.fsync(directory_descriptor)


def _is_final_name(name: str) -> bool:
    if not isinstance(name, str) or not name.endswith(".json"):
        return False
    try:
        _validate_run_id(name[:-5])
    except InputValidationError:
        return False
    return True


def _quarantine_remove_expected(
    directory_descriptor: int,
    name: str,
    expected_identity: tuple[int, int],
    *,
    missing_ok: bool,
) -> bool:
    quarantine = f".quarantine-{secrets.token_hex(24)}.holding"
    try:
        os.rename(
            name,
            quarantine,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
    except FileNotFoundError:
        if missing_ok:
            return False
        raise PersistenceError("snapshot cleanup target disappeared")
    except OSError as error:
        raise PersistenceError("unable to quarantine snapshot artifact") from error

    descriptor: int | None = None
    try:
        descriptor = _open_snapshot_file(directory_descriptor, quarantine)
        opened = os.fstat(descriptor)
        named = _name_metadata(directory_descriptor, quarantine)
        if _inode(opened) != _inode(named):
            raise PersistenceError("snapshot quarantine changed during open")
        if _inode(opened) != expected_identity:
            _close_descriptor(descriptor)
            descriptor = None
            _restore_quarantine(
                directory_descriptor,
                quarantine,
                name,
            )
            raise PersistenceError(
                "snapshot cleanup refused a competing inode"
            )
        _validate_regular(opened, expected_links=None)
        _validate_regular(
            named,
            expected_links=opened.st_nlink,
            expected_size=opened.st_size,
        )
        os.unlink(quarantine, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
        return True
    except (PersistenceError, OSError) as error:
        if _name_exists(directory_descriptor, quarantine):
            _restore_quarantine(
                directory_descriptor,
                quarantine,
                name,
            )
        if isinstance(error, PersistenceError):
            raise
        raise PersistenceError("unable to remove quarantined snapshot") from error
    finally:
        if descriptor is not None:
            _close_descriptor(descriptor)


def _restore_quarantine(
    directory_descriptor: int,
    quarantine: str,
    original_name: str,
) -> None:
    try:
        os.link(
            quarantine,
            original_name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise PersistenceError(
            "unable to restore competing snapshot artifact; quarantine retained"
        ) from error
    quarantine_metadata = _name_metadata(directory_descriptor, quarantine)
    restored_metadata = _name_metadata(directory_descriptor, original_name)
    if _inode(quarantine_metadata) != _inode(restored_metadata):
        raise PersistenceError(
            "restored snapshot artifact has an unexpected identity"
        )
    try:
        os.unlink(quarantine, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    except OSError as error:
        raise PersistenceError(
            "unable to finalize competing snapshot restoration"
        ) from error


def _name_exists(directory_descriptor: int, name: str) -> bool:
    try:
        os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _inode(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    _validate_regular(metadata, expected_links=1)
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
