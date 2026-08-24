"""Deterministic official/manual merge and bounded fallback selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import re
from typing import Literal, Mapping, Sequence, cast

from .config import RadarConfig
from .errors import InputValidationError
from .schemas import GameRecord, MetricObservation, RejectedRow, WarningRecord


MergeMode = Literal["official_plus_manual", "manual_baseline"]
DataStatus = Literal["fresh", "stale", "manual_only"]

_SNAPSHOT_FIELDS = {
    "schema_version",
    "run_id",
    "observed_at",
    "records",
    "metadata",
}
_RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$", re.ASCII)
_UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:"
    r"[0-9]{2}(?:\.[0-9]{1,6})?Z$",
    re.ASCII,
)
_STALE_WARNING = WarningRecord(
    code="steam_official_snapshot_stale",
    message="Official Steam snapshot is older than 36 hours.",
)
_RELEASE_CONFLICT_CODE = "steam_release_status_conflict"
_RELEASE_CONFLICT_MESSAGE = (
    "release status conflicts with the available release date"
)


@dataclass(frozen=True)
class MergeResult:
    """Deeply stable merge outcome consumed by trend analysis and reports."""

    records: Sequence[GameRecord]
    rejected_rows: Sequence[RejectedRow]
    warnings: Sequence[WarningRecord]
    mode: MergeMode
    data_status: DataStatus

    def __post_init__(self) -> None:
        records = tuple(self.records)
        rejected = tuple(self.rejected_rows)
        warnings = tuple(self.warnings)
        if not all(isinstance(record, GameRecord) for record in records):
            raise InputValidationError("records must contain GameRecord values")
        if not all(isinstance(row, RejectedRow) for row in rejected):
            raise InputValidationError("rejected_rows must contain RejectedRow values")
        if not all(isinstance(warning, WarningRecord) for warning in warnings):
            raise InputValidationError("warnings must contain WarningRecord values")
        if self.mode not in {"official_plus_manual", "manual_baseline"}:
            raise InputValidationError("invalid merge mode")
        if self.data_status not in {"fresh", "stale", "manual_only"}:
            raise InputValidationError("invalid merge data status")
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "rejected_rows", rejected)
        object.__setattr__(self, "warnings", warnings)


def merge_import_with_official(
    imported: Sequence[GameRecord],
    official_snapshot: Mapping[str, object] | None,
    now: datetime,
    config: RadarConfig,
) -> MergeResult:
    """Merge one manual observation with an eligible official snapshot.

    Official fallback is inclusive at the configured age limit. A supplied
    snapshot is always schema-validated before its age is considered, so a
    malformed artifact cannot silently become a manual-only run.
    """

    current_time = _aware_utc(now, "now")
    if not isinstance(config, RadarConfig):
        raise InputValidationError("config must be a RadarConfig")
    manual_records, manual_rows = _validate_imported(imported)

    official_records: tuple[GameRecord, ...] = ()
    snapshot_time: datetime | None = None
    official_eligible = False
    if official_snapshot is not None:
        snapshot_time, validated = _validate_official_snapshot(official_snapshot)
        age_hours = (current_time - snapshot_time).total_seconds() / 3_600
        if age_hours < 0:
            raise InputValidationError("official snapshot must not be in the future")
        if age_hours <= config.stale_fallback_limit_hours:
            official_eligible = True
            official_records = validated

    if not official_eligible:
        return _manual_baseline(manual_records, manual_rows, current_time.date())

    # An eligible empty official snapshot remains an official fallback. Its
    # emptiness is capability information, not evidence that the artifact is old.
    official_age = (
        current_time - cast(datetime, snapshot_time)
    ).total_seconds() / 3_600
    warnings: tuple[WarningRecord, ...] = ()
    data_status: DataStatus = "fresh"
    if official_age > config.stale_warning_hours:
        data_status = "stale"
        warnings = (_STALE_WARNING,)

    official_by_appid = {record.appid: record for record in official_records}
    manual_by_appid = {record.appid: record for record in manual_records}
    records: list[GameRecord] = []
    rejected: list[RejectedRow] = []
    for appid in sorted(set(official_by_appid) | set(manual_by_appid)):
        official = official_by_appid.get(appid)
        manual = manual_by_appid.get(appid)
        if official is not None and manual is not None:
            merged = _merge_pair(official, manual, current_time.date())
            if merged is None:
                rejected.append(_release_rejection(manual_rows[appid], appid))
            else:
                records.append(merged)
        elif official is not None:
            records.append(official)
        elif manual is not None:
            normalized = _normalize_manual_release(manual, current_time.date())
            if normalized is None:
                rejected.append(_release_rejection(manual_rows[appid], appid))
            else:
                records.append(normalized)

    return MergeResult(
        records=records,
        rejected_rows=sorted(rejected, key=lambda row: row.row_number),
        warnings=warnings,
        mode="official_plus_manual",
        data_status=data_status,
    )


def _manual_baseline(
    records: Sequence[GameRecord],
    row_numbers: Mapping[int, int],
    today: date,
) -> MergeResult:
    accepted: list[GameRecord] = []
    rejected: list[RejectedRow] = []
    for record in records:
        normalized = _normalize_manual_release(record, today)
        if normalized is None:
            rejected.append(_release_rejection(row_numbers[record.appid], record.appid))
        else:
            accepted.append(normalized)
    return MergeResult(
        records=sorted(accepted, key=lambda record: record.appid),
        rejected_rows=sorted(rejected, key=lambda row: row.row_number),
        warnings=(),
        mode="manual_baseline",
        data_status="manual_only",
    )


def _validate_imported(
    imported: Sequence[GameRecord],
) -> tuple[tuple[GameRecord, ...], dict[int, int]]:
    if isinstance(imported, (str, bytes)) or not isinstance(imported, Sequence):
        raise InputValidationError("imported records must be a sequence")
    result: list[GameRecord] = []
    row_numbers: dict[int, int] = {}
    for row_number, record in enumerate(imported, start=1):
        if not isinstance(record, GameRecord):
            raise InputValidationError("imported records must contain GameRecord values")
        if record.appid in row_numbers:
            raise InputValidationError("imported records must use unique AppIDs")
        if any(
            observation.source_kind != "steamdb_manual_import"
            for observation in record.metrics.values()
        ):
            raise InputValidationError("imported metrics must be manual observations")
        row_numbers[record.appid] = row_number
        result.append(record)
    return tuple(result), row_numbers


def _validate_official_snapshot(
    snapshot: Mapping[str, object],
) -> tuple[datetime, tuple[GameRecord, ...]]:
    if not isinstance(snapshot, Mapping) or set(snapshot) != _SNAPSHOT_FIELDS:
        raise InputValidationError("official snapshot has an invalid top-level schema")
    version = snapshot["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise InputValidationError("official snapshot schema_version must be exactly 1")
    run_id = snapshot["run_id"]
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise InputValidationError("official snapshot has an invalid run ID")
    observed_at = _parse_utc(snapshot["observed_at"], "official snapshot observed_at")
    expected_time = datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ").replace(
        tzinfo=timezone.utc
    )
    if observed_at != expected_time:
        raise InputValidationError("official snapshot time does not match its run ID")
    if not isinstance(snapshot["metadata"], Mapping):
        raise InputValidationError("official snapshot metadata must be a mapping")

    raw_records = snapshot["records"]
    if isinstance(raw_records, (str, bytes)) or not isinstance(raw_records, Sequence):
        raise InputValidationError("official snapshot records must be a sequence")
    records: list[GameRecord] = []
    appids: set[int] = set()
    for raw_record in raw_records:
        if isinstance(raw_record, GameRecord):
            canonical = raw_record.to_dict()
        elif isinstance(raw_record, Mapping):
            canonical = _thaw_snapshot_value(raw_record)
        else:
            raise InputValidationError("official snapshot records are invalid")
        if not isinstance(canonical, Mapping):  # pragma: no cover - guarded above
            raise InputValidationError("official snapshot records are invalid")
        record = GameRecord.from_dict(canonical)
        if record.appid in appids:
            raise InputValidationError("official snapshot contains duplicate AppIDs")
        if any(
            observation.source_kind != "steam_official"
            for observation in record.metrics.values()
        ):
            raise InputValidationError("official snapshot contains non-official metrics")
        appids.add(record.appid)
        records.append(record)
    return observed_at, tuple(records)


def _merge_pair(
    official: GameRecord,
    manual: GameRecord,
    today: date,
) -> GameRecord | None:
    metrics = _merge_metrics(official.metrics, manual.metrics)
    if _has_official_appdetails(official):
        release_status = official.release_status
    else:
        release_status = _resolve_release_status(
            (official.release_status, manual.release_status),
            metrics.get("release_date"),
            today,
        )
        if release_status is None:
            return None

    manual_extra = cast(dict[str, object], manual.to_dict()["source_extra"])
    official_extra = cast(dict[str, object], official.to_dict()["source_extra"])
    source_extra = dict(manual_extra)
    source_extra.update(official_extra)
    return GameRecord(
        schema_version=1,
        appid=official.appid,
        name=official.name,
        release_status=cast(str, release_status),
        store_url=official.store_url,
        metrics=metrics,
        source_extra={key: source_extra[key] for key in sorted(source_extra)},
    )


def _normalize_manual_release(record: GameRecord, today: date) -> GameRecord | None:
    release_status = _resolve_release_status(
        (record.release_status,),
        record.metrics.get("release_date"),
        today,
    )
    if release_status is None:
        return None
    if release_status == record.release_status:
        return record
    return GameRecord(
        schema_version=record.schema_version,
        appid=record.appid,
        name=record.name,
        release_status=cast(str, release_status),
        store_url=record.store_url,
        metrics=record.metrics,
        source_extra=cast(Mapping[str, object], record.to_dict()["source_extra"]),
    )


def _resolve_release_status(
    statuses: Sequence[str],
    release_date: MetricObservation | None,
    today: date,
) -> str | None:
    concrete = {status for status in statuses if status != "unknown"}
    if len(concrete) > 1:
        return None
    date_status: str | None = None
    if release_date is not None:
        if not isinstance(release_date.value, str):
            return None
        try:
            parsed = date.fromisoformat(release_date.value)
        except ValueError:
            return None
        date_status = "released" if parsed <= today else "unreleased"

    if concrete == {"released"}:
        return "released" if date_status == "released" else None
    if concrete == {"unreleased"}:
        return None if date_status == "released" else "unreleased"
    return date_status or "unknown"


def _merge_metrics(
    official: Mapping[str, MetricObservation],
    manual: Mapping[str, MetricObservation],
) -> dict[str, MetricObservation]:
    merged: dict[str, MetricObservation] = {}
    for name in sorted(set(official) | set(manual)):
        official_value = official.get(name)
        manual_value = manual.get(name)
        if official_value is None:
            merged[name] = cast(MetricObservation, manual_value)
        elif manual_value is None:
            merged[name] = official_value
        else:
            merged[name] = _newer_observation(official_value, manual_value)
    return merged


def _newer_observation(
    first: MetricObservation,
    second: MetricObservation,
) -> MetricObservation:
    first_time = _parse_utc(first.observed_at, "metric observed_at")
    second_time = _parse_utc(second.observed_at, "metric observed_at")
    if first_time != second_time:
        return first if first_time > second_time else second
    if first.source_kind != second.source_kind:
        if first.source_kind == "steam_official":
            return first
        if second.source_kind == "steam_official":
            return second
    return min(
        (first, second),
        key=lambda item: json.dumps(
            item.to_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ),
    )


def _has_official_appdetails(record: GameRecord) -> bool:
    if record.source_extra.get("app_type") == "game":
        return True
    return any(
        observation.source_kind == "steam_official"
        and observation.source_id == "steam_appdetails"
        for observation in record.metrics.values()
    )


def _release_rejection(row_number: int, appid: int) -> RejectedRow:
    return RejectedRow(
        row_number=row_number,
        code=_RELEASE_CONFLICT_CODE,
        message=_RELEASE_CONFLICT_MESSAGE,
        appid=appid,
    )


def _aware_utc(value: datetime, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise InputValidationError(f"{name} must include a timezone")
    return value.astimezone(timezone.utc)


def _parse_utc(value: object, name: str) -> datetime:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise InputValidationError(f"{name} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise InputValidationError(
            f"{name} must be an ISO-8601 UTC timestamp"
        ) from error
    return parsed.astimezone(timezone.utc)


def _thaw_snapshot_value(
    value: object,
    *,
    depth: int = 0,
    active: set[int] | None = None,
) -> object:
    """Convert Task 7's deep-frozen JSON shape back to JSON-native values."""

    if depth > 256:
        raise InputValidationError("official snapshot nesting is too deep")
    if active is None:
        active = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise InputValidationError("official snapshot must not contain cycles")
        active.add(identity)
        try:
            return {
                key: _thaw_snapshot_value(
                    nested, depth=depth + 1, active=active
                )
                for key, nested in value.items()
            }
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise InputValidationError("official snapshot must not contain cycles")
        active.add(identity)
        try:
            return [
                _thaw_snapshot_value(item, depth=depth + 1, active=active)
                for item in value
            ]
        finally:
            active.remove(identity)
    return value
