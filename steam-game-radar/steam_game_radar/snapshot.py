"""Immutable radar snapshots and deterministic historical selection."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Mapping, Sequence

from .artifacts import (
    _fsync_parent_directory,
    _validate_run_id,
    atomic_write_json,
)
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
    canonical_records = _canonical_records(records)
    canonical_metadata = _canonical_metadata(metadata)
    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "observed_at": _run_observed_at(run_id),
        "records": canonical_records,
        "metadata": canonical_metadata,
    }
    destination = Path(config.data_dir) / "snapshots" / f"{run_id}.json"
    _publish_immutable_json(destination, payload)
    return destination


def load_snapshots(config: RadarConfig) -> Sequence[Mapping[str, object]]:
    """Load valid snapshots in ascending explicit observation-time order."""

    snapshot_root = Path(config.data_dir) / "snapshots"
    try:
        root_status = snapshot_root.lstat()
    except FileNotFoundError:
        return ()
    except OSError as error:
        raise PersistenceError("unable to inspect snapshot directory") from error
    if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
        raise PersistenceError("snapshot root is not a safe directory")

    try:
        paths = sorted(snapshot_root.glob("*.json"), key=lambda path: path.name)
        loaded = [_load_snapshot(path) for path in paths]
    except PersistenceError:
        raise
    except OSError as error:
        raise PersistenceError("unable to load snapshots") from error
    loaded.sort(key=lambda snapshot: (_snapshot_datetime(snapshot), snapshot["run_id"]))
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
    frozen = _freeze_json(metadata, "snapshot metadata")
    thawed = _thaw_json(frozen)
    if not isinstance(thawed, dict):
        raise InputValidationError("snapshot metadata must be a mapping")
    return thawed


def _publish_immutable_json(destination: Path, payload: object) -> None:
    stage: Path | None = None
    published = False
    try:
        _ensure_snapshot_directory(destination.parent)
        descriptor, stage_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.stem}.",
            suffix=".staged",
        )
        stage = Path(stage_name)
        os.close(descriptor)
        atomic_write_json(stage, payload)
        os.link(stage, destination, follow_symlinks=False)
        published = True
        _fsync_parent_directory(destination.parent)
    except FileExistsError as error:
        raise PersistenceError("snapshot already exists") from error
    except (InputValidationError, PersistenceError):
        if published:
            _rollback_publication(destination)
        raise
    except OSError as error:
        if published:
            _rollback_publication(destination)
        raise PersistenceError("unable to persist immutable snapshot") from error
    finally:
        if stage is not None:
            try:
                stage.unlink(missing_ok=True)
                _fsync_parent_directory(destination.parent)
            except OSError:
                pass


def _ensure_snapshot_directory(directory: Path) -> None:
    try:
        data_directory = directory.parent
        data_directory.mkdir(parents=True, exist_ok=True)
        data_status = data_directory.lstat()
        directory.mkdir(parents=True, exist_ok=True)
        status = directory.lstat()
    except OSError as error:
        raise PersistenceError("unable to prepare snapshot directory") from error
    if (
        stat.S_ISLNK(data_status.st_mode)
        or not stat.S_ISDIR(data_status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or not stat.S_ISDIR(status.st_mode)
    ):
        raise PersistenceError("snapshot root is not a safe directory")


def _rollback_publication(destination: Path) -> None:
    try:
        destination.unlink(missing_ok=True)
        _fsync_parent_directory(destination.parent)
    except OSError:
        pass


def _load_snapshot(path: Path) -> dict[str, object]:
    try:
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise PersistenceError("snapshot path is not a regular file")
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            parse_int=_parse_json_integer,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_object,
        )
    except PersistenceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise PersistenceError("unable to parse snapshot JSON") from error
    return _validate_snapshot(raw, path)


def _validate_snapshot(value: object, path: Path) -> dict[str, object]:
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
    if path.name != f"{run_id}.json":
        raise PersistenceError("snapshot filename does not match its run ID")

    observed_at = value["observed_at"]
    try:
        _utc_timestamp(observed_at)
    except InputValidationError as error:
        raise PersistenceError("snapshot has an invalid UTC observation time") from error
    if observed_at != _run_observed_at(run_id):
        raise PersistenceError("snapshot time does not match its run ID")

    raw_records = value["records"]
    if not isinstance(raw_records, list):
        raise PersistenceError("snapshot records must be a JSON array")
    try:
        records = [GameRecord.from_dict(record).to_dict() for record in raw_records]
        metadata = _canonical_metadata(value["metadata"])
    except (InputValidationError, AttributeError, TypeError) as error:
        raise PersistenceError("snapshot contains invalid records or metadata") from error
    return {
        "schema_version": 1,
        "run_id": run_id,
        "observed_at": observed_at,
        "records": records,
        "metadata": metadata,
    }


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
    parsed = int(value)
    if parsed < MIN_JSON_SAFE_INTEGER or parsed > MAX_JSON_SAFE_INTEGER:
        raise ValueError("snapshot integer is outside the JSON-safe range")
    return parsed


def _reject_json_constant(value: str) -> float:
    raise ValueError(f"invalid JSON numeric constant: {value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("snapshot JSON contains duplicate object keys")
        value[key] = item
    return value
