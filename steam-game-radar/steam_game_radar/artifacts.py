"""Safe persistence and retention for raw Steam provider artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Sequence

from .config import RadarConfig
from .errors import InputValidationError, PersistenceError


_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = ("key", "token", "authorization", "cookie", "secret")
_RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", flags=re.ASCII)
_PROVIDER_ID = re.compile(
    r"[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?",
    flags=re.ASCII,
)


@dataclass(frozen=True)
class _WriteConfinement:
    raw_root: Path
    resolved_raw_root: Path
    raw_identity: tuple[int, int]
    destination_parent: Path
    resolved_destination_parent: Path
    parent_identity: tuple[int, int]


def redact(value: object) -> object:
    """Return a recursively redacted copy of a JSON-compatible value."""

    try:
        _validate_json_native(value)
        return _redact(value)
    except RecursionError as error:
        raise InputValidationError("artifact nesting is too deep") from error


def _redact(value: object) -> object:
    if isinstance(value, dict):
        redacted: dict[str, object] = {}
        for key, item in value.items():
            if isinstance(key, str) and _is_sensitive_key(key):
                redacted[key] = _REDACTED
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def atomic_write_json(path: Path, value: object) -> None:
    """Serialize JSON and atomically replace ``path`` from a sibling file."""

    destination = Path(path)
    serialized = _serialize_json(value)
    _atomic_write_serialized(destination, serialized)


def _atomic_write_serialized(
    destination: Path,
    serialized: str,
    confinement: _WriteConfinement | None = None,
) -> None:
    temporary_path: Path | None = None
    try:
        if confinement is None:
            destination.parent.mkdir(parents=True, exist_ok=True)
        else:
            _validate_write_confinement(confinement)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        if confinement is not None:
            _validate_write_confinement(confinement)
        os.replace(temporary_path, destination)
        if confinement is not None:
            _validate_write_confinement(confinement)
        _fsync_parent_directory(destination.parent)
    except PersistenceError:
        raise
    except OSError as error:
        raise PersistenceError("unable to persist JSON artifact safely") from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def persist_raw(
    config: RadarConfig,
    run_id: str,
    provider_id: str,
    value: object,
    now: datetime,
) -> Path:
    """Validate, redact, and atomically persist one raw provider response."""

    del now
    _validate_run_id(run_id)
    _validate_provider_id(provider_id)
    original = _serialize_json(value).encode("utf-8")
    if len(original) > config.raw_max_bytes_per_provider:
        raise InputValidationError("raw provider artifact exceeds configured size")

    serialized_redacted = _serialize_json(redact(value))
    path, confinement = _prepare_raw_destination(config, run_id, provider_id)
    _atomic_write_serialized(path, serialized_redacted, confinement)
    return path


def prune_raw(config: RadarConfig, now: datetime) -> Sequence[Path]:
    """Remove safe raw JSON files strictly beyond the retention boundary."""

    raw_root = config.data_dir / "raw"
    try:
        try:
            root_status = raw_root.lstat()
        except FileNotFoundError:
            return []
        if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(
            root_status.st_mode
        ):
            raise PersistenceError("configured raw root is not a safe directory")
        resolved_root = raw_root.resolve(strict=True)
        candidates = _prune_candidates(raw_root, resolved_root)
    except PersistenceError:
        raise
    except OSError as error:
        raise PersistenceError("unable to inspect raw artifacts safely") from error

    cutoff = _utc_timestamp(now) - config.raw_retention_days * 86_400
    expired = sorted(
        path for path, modified_at in candidates if modified_at < cutoff
    )
    try:
        for path in expired:
            path.unlink()
    except OSError as error:
        raise PersistenceError("unable to prune raw artifacts safely") from error
    return expired


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _serialize_json(value: object) -> str:
    try:
        _validate_json_native(value)
        serialized = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        serialized.encode("utf-8")
        return serialized
    except InputValidationError:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise InputValidationError("artifact must contain valid JSON values") from error


def _validate_json_native(
    value: object,
    active_containers: set[int] | None = None,
) -> None:
    if active_containers is None:
        active_containers = set()
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise InputValidationError("artifact numbers must be finite")
    if isinstance(value, list):
        identity = _enter_container(value, active_containers)
        try:
            for item in value:
                _validate_json_native(item, active_containers)
        finally:
            active_containers.remove(identity)
        return
    if isinstance(value, dict):
        identity = _enter_container(value, active_containers)
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise InputValidationError(
                        "artifact mapping keys must be strings"
                    )
                _validate_json_native(item, active_containers)
        finally:
            active_containers.remove(identity)
        return
    raise InputValidationError("artifact must contain only JSON-native values")


def _enter_container(value: object, active_containers: set[int]) -> int:
    identity = id(value)
    if identity in active_containers:
        raise InputValidationError("artifact must not contain circular values")
    active_containers.add(identity)
    return identity


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise InputValidationError("invalid run_id")
    try:
        datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ")
    except ValueError as error:
        raise InputValidationError("invalid run_id") from error


def _validate_provider_id(provider_id: str) -> None:
    if not isinstance(provider_id, str) or _PROVIDER_ID.fullmatch(provider_id) is None:
        raise InputValidationError("invalid provider_id")


def _prepare_raw_destination(
    config: RadarConfig,
    run_id: str,
    provider_id: str,
) -> tuple[Path, _WriteConfinement]:
    data_dir = Path(config.data_dir)
    raw_root = data_dir / "raw"
    destination_parent = raw_root / run_id
    try:
        _ensure_real_directory(data_dir, parents=True)
        _ensure_real_directory(raw_root)
        _ensure_real_directory(destination_parent)
        confinement = _capture_write_confinement(
            raw_root,
            destination_parent,
        )
    except PersistenceError:
        raise
    except OSError as error:
        raise PersistenceError("unsafe raw artifact destination") from error
    return destination_parent / f"{provider_id}.json", confinement


def _ensure_real_directory(path: Path, *, parents: bool = False) -> None:
    try:
        status = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=parents, exist_ok=False)
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


def _fsync_parent_directory(parent: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    unsupported_errors = {
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
    try:
        descriptor = os.open(parent, flags)
    except OSError as error:
        if error.errno in unsupported_errors:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in unsupported_errors:
                raise
    finally:
        os.close(descriptor)


def _prune_candidates(
    raw_root: Path,
    resolved_root: Path,
) -> list[tuple[Path, float]]:
    candidates: list[tuple[Path, float]] = []
    for directory, directory_names, file_names in os.walk(
        raw_root,
        topdown=True,
        onerror=_raise_walk_error,
        followlinks=False,
    ):
        current = Path(directory)
        for directory_name in directory_names:
            child = current / directory_name
            child_status = child.lstat()
            if stat.S_ISLNK(child_status.st_mode):
                raise PersistenceError("unsafe symlink found below raw root")
        for file_name in file_names:
            if not file_name.endswith(".json"):
                continue
            candidate = current / file_name
            candidate_status = candidate.lstat()
            if stat.S_ISLNK(candidate_status.st_mode):
                raise PersistenceError("unsafe symlink found below raw root")
            if not stat.S_ISREG(candidate_status.st_mode):
                raise PersistenceError("unsafe non-regular raw artifact found")
            resolved_candidate = candidate.resolve(strict=True)
            if not _is_relative_to(resolved_candidate, resolved_root):
                raise PersistenceError("raw artifact resolves outside raw root")
            candidates.append((candidate, candidate_status.st_mtime))
    return candidates


def _raise_walk_error(error: OSError) -> None:
    raise error


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _utc_timestamp(value: datetime) -> float:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InputValidationError("retention timestamp must include a timezone")
    return value.astimezone(timezone.utc).timestamp()
