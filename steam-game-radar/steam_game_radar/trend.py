"""Pure same-source historical trend analysis for normalized game records."""

from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Mapping, Sequence

from .errors import InputValidationError
from .schemas import GameRecord, MetricObservation, WarningRecord


_MOST_PLAYED_RANK_SOURCE = "steam_most_played_rank"
_PREVIOUS_RANK_SOURCE = "steam_previous_rank"


@dataclass(frozen=True)
class AnalyzedCandidate:
    record: GameRecord
    deltas: Mapping[str, float]
    newly_observed: bool
    warnings: Sequence[WarningRecord]

    def __post_init__(self) -> None:
        if not isinstance(self.record, GameRecord):
            raise InputValidationError("record must be a GameRecord")
        if not isinstance(self.deltas, Mapping):
            raise InputValidationError("deltas must be a mapping")
        frozen_deltas: dict[str, float] = {}
        for name in sorted(self.deltas):
            value = self.deltas[name]
            if not isinstance(name, str) or not name:
                raise InputValidationError("delta names must be non-empty strings")
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise InputValidationError("delta values must be finite numbers")
            frozen_deltas[name] = float(value)
        if not isinstance(self.newly_observed, bool):
            raise InputValidationError("newly_observed must be boolean")
        warnings = tuple(self.warnings)
        if not all(isinstance(warning, WarningRecord) for warning in warnings):
            raise InputValidationError("warnings must contain WarningRecord values")
        object.__setattr__(self, "deltas", MappingProxyType(frozen_deltas))
        object.__setattr__(self, "warnings", warnings)


def analyze_trends(
    current: Sequence[GameRecord],
    one_day_snapshot: Mapping[str, object] | None,
    seven_day_snapshot: Mapping[str, object] | None,
) -> Sequence[AnalyzedCandidate]:
    """Calculate 1d/7d deltas only for exact metric/source identities."""

    current_records = _current_records(current)
    one_day = _historical_records(one_day_snapshot, "one-day")
    seven_day = _historical_records(seven_day_snapshot, "seven-day")
    candidates: list[AnalyzedCandidate] = []
    for record in sorted(current_records, key=lambda value: value.appid):
        deltas: dict[str, float] = {}
        _add_window_deltas(record, one_day.get(record.appid), "1d", deltas)
        _add_window_deltas(record, seven_day.get(record.appid), "7d", deltas)
        candidates.append(
            AnalyzedCandidate(
                record=record,
                deltas=deltas,
                newly_observed=(
                    record.appid not in one_day and record.appid not in seven_day
                ),
                warnings=(),
            )
        )
    return tuple(candidates)


def select_rank_improvement(candidate: AnalyzedCandidate) -> float | None:
    """Choose 7d, then 1d, then provider-supplied rank improvement."""

    if not isinstance(candidate, AnalyzedCandidate):
        raise InputValidationError("candidate must be an AnalyzedCandidate")
    for window in ("7d", "1d"):
        suffix = f"rank_{window}_change"
        values = [
            value
            for name, value in candidate.deltas.items()
            if name.endswith(suffix) and not name.startswith("previous_rank_")
        ]
        if values:
            return max(values)

    current_rank = candidate.record.metrics.get("most_played_rank")
    previous_rank = candidate.record.metrics.get("previous_rank")
    if (
        current_rank is None
        or previous_rank is None
        or current_rank.source_id != _MOST_PLAYED_RANK_SOURCE
        or previous_rank.source_id != _PREVIOUS_RANK_SOURCE
        or current_rank.source_kind != "steam_official"
        or previous_rank.source_kind != "steam_official"
    ):
        return None
    current_value = _numeric(current_rank.value)
    previous_value = _numeric(previous_rank.value)
    if current_value is None or previous_value is None:
        return None
    return previous_value - current_value


def _current_records(current: Sequence[GameRecord]) -> tuple[GameRecord, ...]:
    if isinstance(current, (str, bytes)) or not isinstance(current, Sequence):
        raise InputValidationError("current records must be a sequence")
    records: list[GameRecord] = []
    appids: set[int] = set()
    for record in current:
        if not isinstance(record, GameRecord):
            raise InputValidationError("current records must contain GameRecord values")
        if record.appid in appids:
            raise InputValidationError("current records must use unique AppIDs")
        appids.add(record.appid)
        records.append(record)
    return tuple(records)


def _historical_records(
    snapshot: Mapping[str, object] | None,
    label: str,
) -> dict[int, GameRecord]:
    if snapshot is None:
        return {}
    if not isinstance(snapshot, Mapping):
        raise InputValidationError(f"{label} snapshot must be a mapping")
    raw_records = snapshot.get("records")
    if isinstance(raw_records, (str, bytes)) or not isinstance(raw_records, Sequence):
        raise InputValidationError(f"{label} snapshot records must be a sequence")
    records: dict[int, GameRecord] = {}
    for raw_record in raw_records:
        if isinstance(raw_record, GameRecord):
            canonical = raw_record.to_dict()
        elif isinstance(raw_record, Mapping):
            canonical = _thaw_snapshot_value(raw_record)
        else:
            raise InputValidationError(f"{label} snapshot contains an invalid record")
        if not isinstance(canonical, Mapping):  # pragma: no cover - guarded above
            raise InputValidationError(f"{label} snapshot contains an invalid record")
        record = GameRecord.from_dict(canonical)
        if record.appid in records:
            raise InputValidationError(f"{label} snapshot contains duplicate AppIDs")
        records[record.appid] = record
    return records


def _add_window_deltas(
    current: GameRecord,
    historical: GameRecord | None,
    window: str,
    deltas: dict[str, float],
) -> None:
    if historical is None:
        return
    for metric_name in sorted(current.metrics):
        if metric_name == "previous_rank":
            continue
        current_observation = current.metrics[metric_name]
        old_observation = historical.metrics.get(metric_name)
        if (
            old_observation is None
            or old_observation.source_id != current_observation.source_id
        ):
            continue
        current_value = _numeric(current_observation.value)
        old_value = _numeric(old_observation.value)
        if current_value is None or old_value is None:
            continue
        if _is_rank_metric(metric_name):
            deltas[f"{metric_name}_{window}_change"] = old_value - current_value
        elif old_value != 0:
            deltas[f"{metric_name}_{window}_percent"] = (
                (current_value - old_value) / old_value * 100.0
            )


def _is_rank_metric(metric_name: str) -> bool:
    return metric_name == "rank" or metric_name.endswith("_rank")


def _numeric(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _thaw_snapshot_value(
    value: object,
    *,
    depth: int = 0,
    active: set[int] | None = None,
) -> object:
    """Convert Task 7 mapping proxies/tuples into JSON-native containers."""

    if depth > 256:
        raise InputValidationError("historical snapshot nesting is too deep")
    if active is None:
        active = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise InputValidationError("historical snapshot must not contain cycles")
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
            raise InputValidationError("historical snapshot must not contain cycles")
        active.add(identity)
        try:
            return [
                _thaw_snapshot_value(item, depth=depth + 1, active=active)
                for item in value
            ]
        finally:
            active.remove(identity)
    return value
