"""Safe persistence and retention for raw Steam provider artifacts."""

from __future__ import annotations

from datetime import datetime, timezone
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


def redact(value: object) -> object:
    """Return a recursively redacted copy of a JSON-compatible value."""

    if isinstance(value, dict):
        redacted: dict[object, object] = {}
        for key, item in value.items():
            if isinstance(key, str) and _is_sensitive_key(key):
                redacted[key] = _REDACTED
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def atomic_write_json(path: Path, value: object) -> None:
    """Serialize JSON and atomically replace ``path`` from a sibling file."""

    destination = Path(path)
    serialized = _serialize_json(value)
    temporary_path: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
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
        os.replace(temporary_path, destination)
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

    path = config.data_dir / "raw" / run_id / f"{provider_id}.json"
    atomic_write_json(path, redact(value))
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
    _validate_json_native(value)
    try:
        serialized = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        serialized.encode("utf-8")
        return serialized
    except (TypeError, ValueError, UnicodeError) as error:
        raise InputValidationError("artifact must contain valid JSON values") from error


def _validate_json_native(value: object) -> None:
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise InputValidationError("artifact numbers must be finite")
    if isinstance(value, list):
        for item in value:
            _validate_json_native(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise InputValidationError("artifact mapping keys must be strings")
            _validate_json_native(item)
        return
    raise InputValidationError("artifact must contain only JSON-native values")


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


def _prune_candidates(
    raw_root: Path,
    resolved_root: Path,
) -> list[tuple[Path, float]]:
    candidates: list[tuple[Path, float]] = []
    for directory, directory_names, file_names in os.walk(
        raw_root,
        topdown=True,
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
