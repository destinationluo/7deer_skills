"""Safe, immutable persistence and retention for unified raw artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys

try:
    import fcntl
except ImportError:  # pragma: no cover - secure persistence fails closed below.
    fcntl = None  # type: ignore[assignment]

from .config import RadarConfig
from .errors import (
    IdempotencyConflictError,
    InputValidationError,
    PersistenceError,
)
from .schemas import MAX_SAFE_INTEGER, RawArtifact


_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = ("key", "token", "authorization", "cookie", "secret")
_RUN_ID = re.compile(
    r"(?P<timestamp>\d{8}T\d{6}Z)-[0-9a-f]{8,32}\Z",
    flags=re.ASCII,
)
_PROVIDER = re.compile(r"[a-z][a-z0-9_]{0,127}\Z", flags=re.ASCII)
_ARTIFACT_NAME = re.compile(
    r"[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?\.json\Z",
    flags=re.ASCII,
)
_BUDGET_LOCK_NAME = ".provider-budget.lock"


@dataclass(frozen=True)
class _WriteConfinement:
    raw_root: Path
    resolved_raw_root: Path
    raw_identity: tuple[int, int]
    destination_parent: Path
    resolved_destination_parent: Path
    parent_identity: tuple[int, int]


@dataclass(frozen=True)
class _PruneDirectory:
    name: str
    device: int
    inode: int
    mode: int


@dataclass(frozen=True)
class _PruneCandidate:
    directory_chain: tuple[_PruneDirectory, ...]
    name: str
    device: int
    inode: int
    mode: int
    modified_at: float
    modified_at_ns: int
    logical_path: Path


def redact_json(value: object) -> object:
    """Return a detached JSON-native copy with sensitive values removed."""

    try:
        normalized = _normalize_json(value)
        return _redact_normalized(normalized)
    except RecursionError as error:
        raise InputValidationError("artifact nesting is too deep") from error


def persist_raw_artifact(
    config: RadarConfig,
    run_id: str,
    provider: str,
    artifact_name: str,
    value: object,
    observed_at: datetime,
) -> RawArtifact:
    """Redact and atomically install one immutable run-scoped JSON artifact."""

    _validate_config(config)
    _validate_run_id(run_id)
    _validate_provider(provider)
    _validate_artifact_name(artifact_name)
    _validate_observed_at(observed_at)

    original = _canonical_bytes(value)
    if len(original) > config.raw_max_bytes_per_provider:
        raise InputValidationError("raw provider artifact exceeds configured size")
    redacted = _canonical_bytes(redact_json(value))
    if len(redacted) > config.raw_max_bytes_per_provider:
        raise InputValidationError(
            "redacted raw provider artifact exceeds configured size"
        )

    destination = (
        Path(config.data_dir) / "raw" / run_id / provider / artifact_name
    )
    artifact = RawArtifact(
        schema_version=1,
        run_id=run_id,
        provider=provider,
        path=str(destination),
        observed_at=observed_at,
        sha256=hashlib.sha256(redacted).hexdigest(),
    )
    path, confinement = _prepare_raw_destination(
        config,
        run_id,
        provider,
        artifact_name,
    )
    _atomic_install(
        path,
        redacted,
        confinement,
        config.raw_max_bytes_per_provider,
    )
    return artifact


def prune_raw_artifacts(
    config: RadarConfig,
    now: datetime,
) -> tuple[Path, ...]:
    """Remove JSON artifacts strictly older than the configured retention."""

    _validate_config(config)
    if os.name != "posix" or not all(
        hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW")
    ):
        raise PersistenceError("raw retention requires secure POSIX directory access")

    raw_root = Path(config.data_dir) / "raw"
    raw_root_descriptor: int | None = None
    try:
        raw_root_descriptor = _open_raw_root(Path(config.data_dir), raw_root)
        if raw_root_descriptor is None:
            return ()
        candidates = _prune_candidates(raw_root_descriptor, raw_root)
        cutoff = _utc_timestamp(now) - config.raw_retention_days * 86_400
        expired = tuple(
            sorted(
                (
                    candidate
                    for candidate in candidates
                    if candidate.modified_at < cutoff
                ),
                key=lambda candidate: candidate.logical_path.parts,
            )
        )
        for candidate in expired:
            _delete_prune_candidate(raw_root_descriptor, candidate)
        return tuple(candidate.logical_path for candidate in expired)
    except (InputValidationError, PersistenceError):
        raise
    except OSError as error:
        raise PersistenceError("unable to prune raw artifacts safely") from error
    finally:
        if raw_root_descriptor is not None:
            _close_prune_root_descriptor(raw_root_descriptor)


def _redact_normalized(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: (
                _REDACTED
                if _is_sensitive_key(key)
                else _redact_normalized(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_normalized(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _normalize_json(
    value: object,
    active_containers: set[int] | None = None,
) -> object:
    if active_containers is None:
        active_containers = set()
    if value is None or type(value) in (str, bool):
        return value
    if type(value) is int:
        if value < -MAX_SAFE_INTEGER or value > MAX_SAFE_INTEGER:
            raise InputValidationError(
                "artifact integers must be within the JSON-safe range"
            )
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise InputValidationError("artifact numbers must be finite")
        return value
    if isinstance(value, Mapping):
        identity = _enter_container(value, active_containers)
        try:
            normalized: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise InputValidationError(
                        "artifact mapping keys must be strings"
                    )
                normalized[key] = _normalize_json(item, active_containers)
            return normalized
        finally:
            active_containers.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = _enter_container(value, active_containers)
        try:
            return [
                _normalize_json(item, active_containers)
                for item in value
            ]
        finally:
            active_containers.remove(identity)
    raise InputValidationError("artifact must contain only JSON values")


def _enter_container(value: object, active_containers: set[int]) -> int:
    identity = id(value)
    if identity in active_containers:
        raise InputValidationError("artifact must not contain circular values")
    active_containers.add(identity)
    return identity


def _canonical_bytes(value: object) -> bytes:
    try:
        normalized = _normalize_json(value)
        serialized = json.dumps(
            normalized,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return serialized.encode("utf-8")
    except InputValidationError:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise InputValidationError("artifact must contain valid JSON values") from error


def _validate_config(config: object) -> None:
    if not isinstance(config, RadarConfig):
        raise TypeError("config must be RadarConfig")


def _validate_run_id(run_id: object) -> None:
    if not isinstance(run_id, str):
        raise InputValidationError("invalid run_id")
    match = _RUN_ID.fullmatch(run_id)
    if match is None:
        raise InputValidationError("invalid run_id")
    try:
        datetime.strptime(match.group("timestamp"), "%Y%m%dT%H%M%SZ")
    except ValueError as error:
        raise InputValidationError("invalid run_id") from error


def _validate_provider(provider: object) -> None:
    if not isinstance(provider, str) or _PROVIDER.fullmatch(provider) is None:
        raise InputValidationError("invalid artifact provider")


def _validate_artifact_name(artifact_name: object) -> None:
    if (
        not isinstance(artifact_name, str)
        or _ARTIFACT_NAME.fullmatch(artifact_name) is None
    ):
        raise InputValidationError("invalid artifact filename")


def _validate_observed_at(value: object) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset().total_seconds() != 0
    ):
        raise InputValidationError(
            "artifact observed_at must be a timezone-aware UTC datetime"
        )


def _prepare_raw_destination(
    config: RadarConfig,
    run_id: str,
    provider: str,
    artifact_name: str,
) -> tuple[Path, _WriteConfinement]:
    data_dir = Path(config.data_dir)
    raw_root = data_dir / "raw"
    run_root = raw_root / run_id
    destination_parent = run_root / provider
    try:
        _ensure_real_directory(data_dir, parents=True)
        _ensure_real_directory(raw_root)
        _ensure_real_directory(run_root)
        _ensure_real_directory(destination_parent)
        confinement = _capture_write_confinement(raw_root, destination_parent)
    except PersistenceError:
        raise
    except OSError as error:
        raise PersistenceError("unsafe raw artifact destination") from error
    return destination_parent / artifact_name, confinement


def _ensure_real_directory(path: Path, *, parents: bool = False) -> None:
    try:
        status = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(parents=parents, exist_ok=False)
        except FileExistsError:
            pass
        status = path.lstat()
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise PersistenceError("raw artifact path contains an unsafe directory")


def _capture_write_confinement(
    raw_root: Path,
    destination_parent: Path,
) -> _WriteConfinement:
    raw_status = _real_directory_status(raw_root)
    parent_status = _real_directory_status(destination_parent)
    resolved_raw_root = raw_root.resolve(strict=True)
    resolved_parent = destination_parent.resolve(strict=True)
    if not _is_relative_to(resolved_parent, resolved_raw_root):
        raise PersistenceError("raw artifact destination escapes raw root")
    return _WriteConfinement(
        raw_root=raw_root,
        resolved_raw_root=resolved_raw_root,
        raw_identity=(raw_status.st_dev, raw_status.st_ino),
        destination_parent=destination_parent,
        resolved_destination_parent=resolved_parent,
        parent_identity=(parent_status.st_dev, parent_status.st_ino),
    )


def _validate_write_confinement(confinement: _WriteConfinement) -> None:
    try:
        raw_status = _real_directory_status(confinement.raw_root)
        parent_status = _real_directory_status(confinement.destination_parent)
        resolved_raw_root = confinement.raw_root.resolve(strict=True)
        resolved_parent = confinement.destination_parent.resolve(strict=True)
    except PersistenceError:
        raise
    except OSError as error:
        raise PersistenceError("unsafe raw artifact destination") from error

    if (
        (raw_status.st_dev, raw_status.st_ino) != confinement.raw_identity
        or (parent_status.st_dev, parent_status.st_ino)
        != confinement.parent_identity
        or resolved_raw_root != confinement.resolved_raw_root
        or resolved_parent != confinement.resolved_destination_parent
        or not _is_relative_to(resolved_parent, resolved_raw_root)
    ):
        raise PersistenceError("raw artifact destination changed during write")


def _real_directory_status(path: Path) -> os.stat_result:
    status = path.lstat()
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise PersistenceError("raw artifact path contains an unsafe directory")
    return status


def _atomic_install(
    destination: Path,
    serialized: bytes,
    confinement: _WriteConfinement,
    provider_budget: int,
) -> None:
    parent_descriptor: int | None = None
    lock_descriptor: int | None = None
    temporary_descriptor: int | None = None
    temporary_name: str | None = None
    temporary_status: os.stat_result | None = None
    installed_status: os.stat_result | None = None
    committed_status: os.stat_result | None = None
    committed = False
    try:
        _require_secure_install_support()
        _validate_write_confinement(confinement)
        parent_descriptor = _open_write_parent(confinement)
        lock_descriptor = _lock_provider_budget(parent_descriptor)
        existing_status = _read_existing_at(
            parent_descriptor,
            destination.name,
            serialized,
        )
        if existing_status is not None:
            _verify_returned_artifact(
                destination,
                confinement,
                parent_descriptor,
                existing_status,
                serialized,
            )
            committed_status = existing_status
            committed = True
            return
        current_usage = _provider_usage(parent_descriptor)
        if current_usage + len(serialized) > provider_budget:
            raise InputValidationError(
                "raw provider artifacts exceed configured cumulative size"
            )

        temporary_name, temporary_descriptor = _create_temporary_file(
            parent_descriptor,
            destination.name,
        )
        _write_all(temporary_descriptor, serialized)
        os.fsync(temporary_descriptor)
        temporary_status = os.fstat(temporary_descriptor)
        if not stat.S_ISREG(temporary_status.st_mode):
            raise PersistenceError("raw artifact temporary file is unsafe")

        _validate_write_confinement(confinement)
        try:
            os.link(
                temporary_name,
                destination.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing_status = _read_existing_at(
                parent_descriptor,
                destination.name,
                serialized,
            )
            if existing_status is not None:
                _verify_returned_artifact(
                    destination,
                    confinement,
                    parent_descriptor,
                    existing_status,
                    serialized,
                )
                committed_status = existing_status
                committed = True
                return
            raise PersistenceError(
                "raw artifact destination changed during installation"
            )
        installed_status = temporary_status
        linked_destination = os.stat(
            destination.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        linked_source = os.stat(
            temporary_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not _same_entry_identity(linked_destination, linked_source):
            raise PersistenceError("raw artifact installed link identity is unsafe")
        installed_status = linked_destination
        _verify_installed_artifact(
            parent_descriptor,
            destination.name,
            temporary_status,
            serialized,
        )
        if _provider_usage(parent_descriptor) > provider_budget:
            raise PersistenceError(
                "raw provider budget changed during artifact installation"
        )
        _validate_write_confinement(confinement)
        _fsync_directory_descriptor(parent_descriptor)
        _verify_installed_artifact(
            parent_descriptor,
            destination.name,
            temporary_status,
            serialized,
        )
        _verify_returned_artifact(
            destination,
            confinement,
            parent_descriptor,
            temporary_status,
            serialized,
        )
        committed_status = temporary_status
        committed = True
    except (IdempotencyConflictError, PersistenceError):
        raise
    except OSError as error:
        raise PersistenceError("unable to persist raw artifact safely") from error
    finally:
        primary_error = sys.exc_info()[1]
        cleanup_errors: list[BaseException] = []
        if (
            not committed
            and installed_status is not None
            and parent_descriptor is not None
        ):
            _cleanup_installed_artifact(
                parent_descriptor,
                destination.name,
                installed_status,
                cleanup_errors,
            )
        if (
            temporary_name is not None
            and temporary_status is not None
            and parent_descriptor is not None
        ):
            _cleanup_temporary_file(
                parent_descriptor,
                temporary_name,
                temporary_status,
                cleanup_errors,
            )
        if (
            committed
            and committed_status is not None
            and parent_descriptor is not None
        ):
            try:
                _verify_returned_artifact(
                    destination,
                    confinement,
                    parent_descriptor,
                    committed_status,
                    serialized,
                )
            except (PersistenceError, OSError) as error:
                cleanup_errors.append(error)
        for descriptor in (
            temporary_descriptor,
            lock_descriptor,
            parent_descriptor,
        ):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as error:
                    cleanup_errors.append(error)
        _raise_or_note_cleanup_errors(primary_error, cleanup_errors)


def _read_existing(destination: Path, expected: bytes) -> bool | None:
    try:
        descriptor = os.open(
            destination,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PersistenceError("existing raw artifact is unsafe") from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise PersistenceError("existing raw artifact is not a regular file")
        chunks: list[bytes] = []
        remaining = len(expected) + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        actual = b"".join(chunks)
    except PersistenceError:
        raise
    except OSError as error:
        raise PersistenceError("unable to inspect existing raw artifact") from error
    finally:
        primary_error = sys.exc_info()[1]
        try:
            os.close(descriptor)
        except OSError as error:
            if primary_error is None:
                raise PersistenceError(
                    "unable to close existing raw artifact safely"
                ) from error
            _add_cleanup_note(
                primary_error,
                "existing raw artifact descriptor cleanup also failed",
            )
    if actual == expected:
        return True
    raise IdempotencyConflictError(
        "raw artifact path was reused with changed content"
    )


def _require_secure_install_support() -> None:
    if fcntl is None or os.name != "posix" or not all(
        hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW")
    ):
        raise PersistenceError("raw persistence requires secure POSIX file access")


def _open_write_parent(confinement: _WriteConfinement) -> int:
    descriptor = os.open(
        confinement.destination_parent,
        _directory_open_flags() | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        actual = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(actual.st_mode)
            or (actual.st_dev, actual.st_ino) != confinement.parent_identity
        ):
            raise PersistenceError("raw artifact destination changed during write")
        return descriptor
    except BaseException:
        _close_descriptors((descriptor,))
        raise


def _lock_provider_budget(parent_descriptor: int) -> int:
    if fcntl is None:  # pragma: no cover - guarded by support validation.
        raise PersistenceError("raw persistence requires provider file locking")
    base_flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(
            _BUDGET_LOCK_NAME,
            base_flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_descriptor,
        )
    except FileExistsError:
        descriptor = os.open(
            _BUDGET_LOCK_NAME,
            base_flags,
            dir_fd=parent_descriptor,
        )
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise PersistenceError("raw provider budget lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        visible = os.stat(
            _BUDGET_LOCK_NAME,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not _same_regular_inode(status, visible):
            raise PersistenceError("raw provider budget lock changed")
        return descriptor
    except BaseException:
        _close_descriptors((descriptor,))
        raise


def _provider_usage(parent_descriptor: int) -> int:
    total = 0
    with os.scandir(parent_descriptor) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        status = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if name.endswith(".tmp"):
            if not (
                stat.S_ISREG(status.st_mode)
                or stat.S_ISLNK(status.st_mode)
            ):
                raise PersistenceError("unsafe raw provider temporary entry")
            continue
        if stat.S_ISLNK(status.st_mode):
            raise PersistenceError("unsafe symlink found in raw provider directory")
        if name == _BUDGET_LOCK_NAME:
            if not stat.S_ISREG(status.st_mode):
                raise PersistenceError("unsafe raw provider control file")
            continue
        if not name.endswith(".json"):
            if not stat.S_ISREG(status.st_mode):
                raise PersistenceError("unsafe entry found in raw provider directory")
            continue
        if _ARTIFACT_NAME.fullmatch(name) is None or not stat.S_ISREG(status.st_mode):
            raise PersistenceError("unsafe raw provider artifact found")
        total += status.st_size
    return total


def _create_temporary_file(
    parent_descriptor: int,
    destination_name: str,
) -> tuple[str, int]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _ in range(128):
        name = f".{destination_name}.{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(
                name,
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
        except FileExistsError:
            continue
        return name, descriptor
    raise PersistenceError("unable to allocate raw artifact temporary file")


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "short raw artifact write")
        offset += written


def _read_existing_at(
    parent_descriptor: int,
    name: str,
    expected: bytes,
) -> os.stat_result | None:
    try:
        descriptor = os.open(name, _file_open_flags(), dir_fd=parent_descriptor)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PersistenceError("existing raw artifact is unsafe") from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise PersistenceError("existing raw artifact is not a regular file")
        actual = _read_bounded(descriptor, len(expected) + 1)
    except PersistenceError:
        raise
    except OSError as error:
        raise PersistenceError("unable to inspect existing raw artifact") from error
    finally:
        primary_error = sys.exc_info()[1]
        try:
            os.close(descriptor)
        except OSError as error:
            if primary_error is None:
                raise PersistenceError(
                    "unable to close existing raw artifact safely"
                ) from error
            _add_cleanup_note(
                primary_error,
                "existing raw artifact descriptor cleanup also failed",
            )
    if actual == expected:
        return status
    raise IdempotencyConflictError(
        "raw artifact path was reused with changed content"
    )


def _verify_installed_artifact(
    parent_descriptor: int,
    name: str,
    temporary_status: os.stat_result,
    expected: bytes,
) -> None:
    try:
        descriptor = os.open(name, _file_open_flags(), dir_fd=parent_descriptor)
    except OSError as error:
        raise PersistenceError("installed raw artifact is unsafe") from error
    try:
        installed = os.fstat(descriptor)
        if not _same_regular_inode(temporary_status, installed):
            raise PersistenceError("installed raw artifact identity is unsafe")
        actual = _read_bounded(descriptor, len(expected) + 1)
        if (
            actual != expected
            or hashlib.sha256(actual).digest()
            != hashlib.sha256(expected).digest()
        ):
            raise PersistenceError("installed raw artifact content is unsafe")
    finally:
        primary_error = sys.exc_info()[1]
        try:
            os.close(descriptor)
        except OSError as error:
            if primary_error is None:
                raise PersistenceError(
                    "unable to close installed raw artifact safely"
                ) from error
            _add_cleanup_note(
                primary_error,
                "installed raw artifact descriptor cleanup also failed",
            )
    visible = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if not _same_regular_inode(temporary_status, visible):
        raise PersistenceError("installed raw artifact path changed")


def _verify_returned_artifact(
    destination: Path,
    confinement: _WriteConfinement,
    parent_descriptor: int,
    expected_status: os.stat_result,
    expected: bytes,
) -> None:
    """Revalidate the caller-visible pathname immediately before success."""

    descriptor: int | None = None
    try:
        _validate_write_confinement(confinement)
        descriptor = os.open(destination, _file_open_flags())
        opened = os.fstat(descriptor)
        if not _same_regular_inode(expected_status, opened):
            raise PersistenceError("returned raw artifact identity is unsafe")
        actual = _read_bounded(descriptor, len(expected) + 1)
        if (
            actual != expected
            or hashlib.sha256(actual).digest()
            != hashlib.sha256(expected).digest()
        ):
            raise PersistenceError("returned raw artifact content is unsafe")
        visible = destination.lstat()
        anchored = os.stat(
            destination.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not _same_regular_inode(expected_status, visible)
            or not _same_regular_inode(expected_status, anchored)
        ):
            raise PersistenceError("returned raw artifact path changed")
        _validate_write_confinement(confinement)
    except PersistenceError:
        raise
    except OSError as error:
        raise PersistenceError("unable to validate returned raw artifact") from error
    finally:
        if descriptor is not None:
            primary_error = sys.exc_info()[1]
            try:
                os.close(descriptor)
            except OSError as error:
                if primary_error is None:
                    raise PersistenceError(
                        "unable to close returned raw artifact safely"
                    ) from error
                _add_cleanup_note(
                    primary_error,
                    "returned raw artifact descriptor cleanup also failed",
                )


def _read_bounded(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum
    while remaining > 0:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _same_regular_inode(
    expected: os.stat_result,
    actual: os.stat_result,
) -> bool:
    return (
        stat.S_ISREG(actual.st_mode)
        and expected.st_dev == actual.st_dev
        and expected.st_ino == actual.st_ino
    )


def _same_entry_identity(
    expected: os.stat_result,
    actual: os.stat_result,
) -> bool:
    return (
        stat.S_IFMT(expected.st_mode) == stat.S_IFMT(actual.st_mode)
        and expected.st_dev == actual.st_dev
        and expected.st_ino == actual.st_ino
    )


def _cleanup_installed_artifact(
    parent_descriptor: int,
    name: str,
    expected_status: os.stat_result,
    errors: list[BaseException],
) -> None:
    _retire_owned_entry(
        parent_descriptor,
        name,
        expected_status,
        errors,
    )


def _cleanup_temporary_file(
    parent_descriptor: int,
    name: str,
    expected_status: os.stat_result,
    errors: list[BaseException],
) -> None:
    _retire_owned_entry(
        parent_descriptor,
        name,
        expected_status,
        errors,
    )


def _retire_owned_entry(
    parent_descriptor: int,
    name: str,
    expected_status: os.stat_result,
    errors: list[BaseException],
) -> None:
    try:
        visible = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        errors.append(error)
        return
    if not _same_entry_identity(expected_status, visible):
        return

    retired_name = _unused_retired_name(parent_descriptor, name, errors)
    if retired_name is None:
        return
    try:
        os.rename(
            name,
            retired_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        errors.append(error)
        return

    try:
        retired = os.stat(
            retired_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        errors.append(error)
        return
    if not _same_entry_identity(expected_status, retired):
        errors.append(PersistenceError("raw artifact cleanup identity changed"))
        return

    try:
        still_owned = os.stat(
            retired_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not _same_entry_identity(expected_status, still_owned):
            errors.append(PersistenceError("raw artifact cleanup identity changed"))
            return
        os.unlink(retired_name, dir_fd=parent_descriptor)
    except FileNotFoundError:
        return
    except OSError as error:
        errors.append(error)


def _unused_retired_name(
    parent_descriptor: int,
    name: str,
    errors: list[BaseException],
) -> str | None:
    for _ in range(128):
        candidate = f".{name}.{secrets.token_hex(16)}.cleanup.tmp"
        try:
            os.stat(
                candidate,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return candidate
        except OSError as error:
            errors.append(error)
            return None
    errors.append(PersistenceError("unable to allocate raw cleanup pathname"))
    return None


def _raise_or_note_cleanup_errors(
    primary_error: BaseException | None,
    cleanup_errors: Sequence[BaseException],
) -> None:
    if not cleanup_errors:
        return
    if primary_error is not None:
        _add_cleanup_note(primary_error, "raw artifact cleanup also failed")
        return
    raise PersistenceError("unable to clean up raw artifact safely") from cleanup_errors[0]


def _add_cleanup_note(error: BaseException, message: str) -> None:
    if hasattr(error, "add_note"):
        error.add_note(message)


def _open_raw_root(data_dir: Path, raw_root: Path) -> int | None:
    data_descriptor: int | None = None
    try:
        expected_data = data_dir.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(expected_data.st_mode) or not stat.S_ISDIR(
        expected_data.st_mode
    ):
        raise PersistenceError("configured data directory is not safe")

    try:
        data_descriptor = os.open(data_dir, _directory_open_flags())
        actual_data = os.fstat(data_descriptor)
        if not _same_directory(expected_data, actual_data):
            raise PersistenceError("configured data directory changed")
        try:
            expected = os.stat(
                "raw",
                dir_fd=data_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(expected.st_mode) or not stat.S_ISDIR(expected.st_mode):
            raise PersistenceError("configured raw root is not a safe directory")
        descriptor = os.open(
            "raw",
            _directory_open_flags(),
            dir_fd=data_descriptor,
        )
        try:
            actual = os.fstat(descriptor)
            if not _same_directory(expected, actual):
                raise PersistenceError("configured raw root changed during inspection")
            return descriptor
        except BaseException:
            _close_descriptors((descriptor,))
            raise
    finally:
        if data_descriptor is not None:
            primary_error = sys.exc_info()[1]
            try:
                os.close(data_descriptor)
            except OSError as cleanup_error:
                if primary_error is None:
                    raise PersistenceError(
                        "unable to close configured data directory safely"
                    ) from cleanup_error
                _add_cleanup_note(
                    primary_error,
                    "configured data directory cleanup also failed",
                )
def _prune_candidates(
    raw_root_descriptor: int,
    raw_root: Path,
) -> list[_PruneCandidate]:
    candidates: list[_PruneCandidate] = []
    pending: list[tuple[_PruneDirectory, ...]] = [()]
    while pending:
        directory_chain = pending.pop()
        descriptor = _open_anchored_directory(raw_root_descriptor, directory_chain)
        try:
            with os.scandir(descriptor) as entries:
                names = sorted(entry.name for entry in entries)
            child_directories: list[tuple[_PruneDirectory, ...]] = []
            for name in names:
                status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(status.st_mode):
                    raise PersistenceError("unsafe symlink found below raw root")
                if stat.S_ISDIR(status.st_mode):
                    child = _PruneDirectory(
                        name=name,
                        device=status.st_dev,
                        inode=status.st_ino,
                        mode=status.st_mode,
                    )
                    _verify_child_directory(descriptor, child)
                    child_directories.append(directory_chain + (child,))
                    continue
                if not name.endswith(".json"):
                    continue
                if not stat.S_ISREG(status.st_mode):
                    raise PersistenceError("unsafe non-regular raw artifact found")
                relative_parts = tuple(
                    component.name for component in directory_chain
                ) + (name,)
                candidates.append(
                    _PruneCandidate(
                        directory_chain=directory_chain,
                        name=name,
                        device=status.st_dev,
                        inode=status.st_ino,
                        mode=status.st_mode,
                        modified_at=status.st_mtime,
                        modified_at_ns=status.st_mtime_ns,
                        logical_path=raw_root.joinpath(*relative_parts),
                    )
                )
            pending.extend(reversed(child_directories))
        finally:
            _close_descriptors((descriptor,))
    return candidates


def _verify_child_directory(
    parent_descriptor: int,
    expected: _PruneDirectory,
) -> None:
    descriptor = os.open(
        expected.name,
        _directory_open_flags(),
        dir_fd=parent_descriptor,
    )
    try:
        actual = os.fstat(descriptor)
        if not _same_frozen_directory(expected, actual):
            raise PersistenceError("raw artifact directory changed during inspection")
    finally:
        _close_descriptors((descriptor,))


def _open_anchored_directory(
    raw_root_descriptor: int,
    directory_chain: tuple[_PruneDirectory, ...],
) -> int:
    descriptors = [os.dup(raw_root_descriptor)]
    try:
        for expected in directory_chain:
            parent = descriptors[-1]
            visible = os.stat(
                expected.name,
                dir_fd=parent,
                follow_symlinks=False,
            )
            if not _same_frozen_directory(expected, visible):
                raise PersistenceError("raw artifact directory changed during pruning")
            child = os.open(
                expected.name,
                _directory_open_flags(),
                dir_fd=parent,
            )
            descriptors.append(child)
            actual = os.fstat(child)
            if not _same_frozen_directory(expected, actual):
                raise PersistenceError("raw artifact directory changed during pruning")
        return descriptors.pop()
    finally:
        _close_descriptors(tuple(reversed(descriptors)))


def _delete_prune_candidate(
    raw_root_descriptor: int,
    candidate: _PruneCandidate,
) -> None:
    parent = _open_anchored_directory(
        raw_root_descriptor,
        candidate.directory_chain,
    )
    file_descriptor: int | None = None
    try:
        visible = os.stat(
            candidate.name,
            dir_fd=parent,
            follow_symlinks=False,
        )
        if not _same_frozen_candidate(candidate, visible):
            raise PersistenceError("raw artifact changed during pruning")
        file_descriptor = os.open(
            candidate.name,
            _file_open_flags(),
            dir_fd=parent,
        )
        opened = os.fstat(file_descriptor)
        if not _same_frozen_candidate(candidate, opened):
            raise PersistenceError("raw artifact changed during pruning")
        final = os.stat(
            candidate.name,
            dir_fd=parent,
            follow_symlinks=False,
        )
        if not _same_frozen_candidate(candidate, final):
            raise PersistenceError("raw artifact changed during pruning")
        os.unlink(candidate.name, dir_fd=parent)
        _fsync_directory_descriptor(parent)
    finally:
        descriptors = (
            (file_descriptor, parent)
            if file_descriptor is not None
            else (parent,)
        )
        _close_descriptors(descriptors)


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _file_open_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _same_directory(expected: os.stat_result, actual: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(actual.st_mode)
        and expected.st_dev == actual.st_dev
        and expected.st_ino == actual.st_ino
        and expected.st_mode == actual.st_mode
    )


def _same_frozen_directory(
    expected: _PruneDirectory,
    actual: os.stat_result,
) -> bool:
    return (
        stat.S_ISDIR(actual.st_mode)
        and expected.device == actual.st_dev
        and expected.inode == actual.st_ino
        and expected.mode == actual.st_mode
    )


def _same_frozen_candidate(
    expected: _PruneCandidate,
    actual: os.stat_result,
) -> bool:
    return (
        stat.S_ISREG(actual.st_mode)
        and expected.device == actual.st_dev
        and expected.inode == actual.st_ino
        and expected.mode == actual.st_mode
        and expected.modified_at_ns == actual.st_mtime_ns
    )


def _fsync_directory_descriptor(descriptor: int) -> None:
    unsupported = {
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
    try:
        os.fsync(descriptor)
    except OSError as error:
        if error.errno not in unsupported:
            raise


def _close_descriptors(descriptors: Sequence[int]) -> None:
    active_exception = sys.exc_info()[0] is not None
    first_error: OSError | None = None
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError as error:
            if first_error is None:
                first_error = error
    if first_error is not None and not active_exception:
        raise first_error


def _close_prune_root_descriptor(descriptor: int) -> None:
    primary_error = sys.exc_info()[1]
    try:
        os.close(descriptor)
    except OSError as cleanup_error:
        if primary_error is None:
            raise PersistenceError(
                "unable to close raw retention root safely"
            ) from cleanup_error
        if isinstance(primary_error, Exception) and hasattr(primary_error, "add_note"):
            primary_error.add_note(
                "raw retention descriptor cleanup also failed"
            )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _utc_timestamp(value: object) -> float:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise InputValidationError("retention timestamp must include a timezone")
    try:
        return value.astimezone(timezone.utc).timestamp()
    except (OverflowError, OSError, ValueError) as error:
        raise InputValidationError(
            "retention timestamp is outside the supported range"
        ) from error


__all__ = [
    "persist_raw_artifact",
    "prune_raw_artifacts",
    "redact_json",
]
