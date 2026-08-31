"""Platform-neutral collector result and source-health rules."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol, TypeVar, runtime_checkable

from ..errors import InputValidationError
from ..platform_keys import validate_platform
from ..schemas import (
    PlatformObservation,
    RadarRun,
    RawArtifact,
    SourceHealth,
    WarningRecord,
)


Record = TypeVar("Record")


def _schema_records(
    value: object,
    name: str,
    record_type: type[Record],
) -> tuple[Record, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise InputValidationError(f"{name} must be an array")
    records = tuple(value)
    if any(not isinstance(record, record_type) for record in records):
        raise InputValidationError(
            f"{name} must contain only {record_type.__name__} records"
        )
    return records


def _helper_records(
    value: object,
    name: str,
    record_type: type[Record],
) -> tuple[Record, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must be a sequence")
    records = tuple(value)
    if any(not isinstance(record, record_type) for record in records):
        raise ValueError(
            f"{name} must contain only {record_type.__name__} records"
        )
    return records


def _helper_platform(value: object) -> str:
    try:
        return validate_platform(value, "collector")
    except InputValidationError as error:
        raise ValueError(str(error)) from error


def _aware_utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a timezone-aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _positive_hours(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _capability_map(value: object) -> dict[str, bool]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("capabilities must be a mapping or None")
    parsed: dict[str, bool] = {}
    for key, succeeded in value.items():
        if (
            not isinstance(key, str)
            or not key
            or key != key.strip()
            or len(key) > 128
        ):
            raise ValueError(
                "capability names must be nonempty text without surrounding whitespace"
            )
        if type(succeeded) is not bool:
            raise ValueError("capability values must be booleans")
        parsed[key] = succeeded
    return parsed


@runtime_checkable
class Collector(Protocol):
    """Structural interface implemented by each platform collector."""

    def collect(self, run: RadarRun) -> "CollectorResult":
        """Collect one platform for the supplied radar run."""


@dataclass(frozen=True)
class CollectorResult:
    """Immutable, provenance-checked output from one collector."""

    collector: str
    observations: tuple[PlatformObservation, ...]
    health: SourceHealth
    raw_artifacts: tuple[RawArtifact, ...]

    def __post_init__(self) -> None:
        collector = validate_platform(self.collector, "collector")
        if not isinstance(self.health, SourceHealth):
            raise InputValidationError("health must be a SourceHealth record")
        observations = _schema_records(
            self.observations,
            "observations",
            PlatformObservation,
        )
        raw_artifacts = _schema_records(
            self.raw_artifacts,
            "raw_artifacts",
            RawArtifact,
        )
        if self.health.collector != collector:
            raise InputValidationError(
                "health collector must match result collector"
            )
        for observation in observations:
            if observation.platform != collector:
                raise InputValidationError(
                    "observation platform must match result collector"
                )
            if observation.run_id != self.health.run_id:
                raise InputValidationError(
                    "observation run_id must match health run_id"
                )
        for artifact in raw_artifacts:
            if artifact.run_id != self.health.run_id:
                raise InputValidationError(
                    "raw artifact run_id must match health run_id"
                )
        object.__setattr__(self, "collector", collector)
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "raw_artifacts", raw_artifacts)


def classify_source_health(
    run_id: str,
    now: datetime,
    attempted: bool,
    active_observations: Sequence[PlatformObservation],
    capabilities: Mapping[str, bool] | None,
    fallback_observed_at: datetime | None,
    warnings: Sequence[WarningRecord],
    fresh_hours: int,
    stale_fallback_hours: int,
    *,
    collector: str,
) -> SourceHealth:
    """Classify one source without treating stale active data as fallback."""

    parsed_collector = _helper_platform(collector)
    parsed_now = _aware_utc(now, "now")
    if type(attempted) is not bool:
        raise ValueError("attempted must be a boolean")
    parsed_fresh_hours = _positive_hours(fresh_hours, "fresh_hours")
    parsed_stale_hours = _positive_hours(
        stale_fallback_hours,
        "stale_fallback_hours",
    )
    if parsed_fresh_hours >= parsed_stale_hours:
        raise ValueError("fresh_hours must be less than stale_fallback_hours")

    observations = _helper_records(
        active_observations,
        "active_observations",
        PlatformObservation,
    )
    parsed_warnings = _helper_records(
        warnings,
        "warnings",
        WarningRecord,
    )
    parsed_capabilities = _capability_map(capabilities)
    fallback = (
        None
        if fallback_observed_at is None
        else _aware_utc(fallback_observed_at, "fallback_observed_at")
    )

    for observation in observations:
        if observation.run_id != run_id:
            raise ValueError("active observation run_id must match run_id")
        if observation.platform != parsed_collector:
            raise ValueError(
                "active observation platform must match collector"
            )
    for warning in parsed_warnings:
        if warning.collector is not None and warning.collector != parsed_collector:
            raise ValueError("warning collector must match collector")

    if not attempted:
        if observations or fallback is not None or any(parsed_capabilities.values()):
            raise ValueError(
                "not-attempted source cannot include observations, fallback, "
                "or successful capabilities"
            )
        status = "not_run"
    else:
        fresh_limit = timedelta(hours=parsed_fresh_hours)
        active_is_fresh = bool(observations) and all(
            timedelta(0) <= parsed_now - observation.observed_at <= fresh_limit
            for observation in observations
        )
        if active_is_fresh:
            status = (
                "partial"
                if any(not succeeded for succeeded in parsed_capabilities.values())
                else "fresh"
            )
        else:
            stale_limit = timedelta(hours=parsed_stale_hours)
            fallback_is_usable = (
                fallback is not None
                and timedelta(0) <= parsed_now - fallback <= stale_limit
            )
            status = "stale" if fallback_is_usable else "unavailable"

    try:
        return SourceHealth(
            schema_version=1,
            run_id=run_id,
            collector=parsed_collector,
            status=status,
            observed_at=parsed_now,
            capabilities=parsed_capabilities,
            warnings=parsed_warnings,
        )
    except InputValidationError as error:
        raise ValueError(str(error)) from error


__all__ = ["Collector", "CollectorResult", "classify_source_health"]
