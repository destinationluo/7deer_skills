"""Platform-neutral collector result and source-health rules."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import re
from types import MappingProxyType
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
_RUN_ID = re.compile(
    r"(?P<timestamp>\d{8}T\d{6}Z)-[0-9a-f]{8,32}\Z",
    flags=re.ASCII,
)
_PROVIDER = re.compile(r"[a-z][a-z0-9_]{0,127}\Z", flags=re.ASCII)
_ARTIFACT_NAME = re.compile(
    r"[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?\.json\Z",
    flags=re.ASCII,
)
_MAX_SAFE_INTEGER = 9_007_199_254_740_991


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


def _pending_run_id(value: object) -> str:
    if not isinstance(value, str):
        raise InputValidationError("pending raw run_id must be text")
    match = _RUN_ID.fullmatch(value)
    if match is None:
        raise InputValidationError("pending raw run_id is invalid")
    try:
        datetime.strptime(match.group("timestamp"), "%Y%m%dT%H%M%SZ")
    except ValueError as error:
        raise InputValidationError("pending raw run_id is invalid") from error
    return value


def _pending_provider(value: object) -> str:
    if not isinstance(value, str) or _PROVIDER.fullmatch(value) is None:
        raise InputValidationError(
            "pending raw provider must be a lowercase identifier"
        )
    return value


def _pending_artifact_name(value: object) -> str:
    if not isinstance(value, str) or _ARTIFACT_NAME.fullmatch(value) is None:
        raise InputValidationError(
            "pending raw artifact_name must be a safe lowercase JSON filename"
        )
    return value


def _pending_observed_at(value: object) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise InputValidationError(
            "pending raw observed_at must be a timezone-aware UTC datetime"
        )
    return value.astimezone(timezone.utc)


def _freeze_pending_json(value: object, name: str = "pending raw payload") -> object:
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InputValidationError(
                    f"{name} mappings must use string keys"
                )
            frozen[key] = _freeze_pending_json(item, name)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_pending_json(item, name) for item in value)
    if value is None or type(value) in (str, bool):
        return value
    if type(value) is int:
        if value < -_MAX_SAFE_INTEGER or value > _MAX_SAFE_INTEGER:
            raise InputValidationError(
                f"{name} integers must be within the JSON-safe range"
            )
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise InputValidationError(f"{name} must contain only JSON values")


def _positive_window(value: object, name: str) -> timedelta:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    try:
        return timedelta(hours=value)
    except OverflowError as error:
        raise ValueError(f"{name} is too large") from error


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


def _validate_result_semantics(
    collector: str,
    observations: tuple[PlatformObservation, ...],
    health: SourceHealth,
    raw_artifacts: tuple[RawArtifact, ...],
    pending_raw_payloads: tuple["PendingRawPayload", ...],
) -> None:
    for warning in health.warnings:
        if warning.collector is not None and warning.collector != collector:
            raise InputValidationError(
                "warning collector must match result collector"
            )

    has_failed_capability = any(
        not succeeded for succeeded in health.capabilities.values()
    )
    if health.status in {"fresh", "partial"} and not observations:
        raise InputValidationError(
            f"{health.status} health requires at least one observation"
        )
    if health.status == "fresh" and has_failed_capability:
        raise InputValidationError(
            "fresh health cannot contain a failed capability"
        )
    if health.status == "partial" and not has_failed_capability:
        raise InputValidationError(
            "partial health requires at least one failed capability"
        )
    if health.status == "not_run" and (
        observations
        or raw_artifacts
        or pending_raw_payloads
        or any(health.capabilities.values())
    ):
        raise InputValidationError(
            "not_run health cannot contain observations, raw artifacts, "
            "pending raw payloads, or successful capabilities"
        )


@runtime_checkable
class Collector(Protocol):
    """Structural interface implemented by each platform collector."""

    def collect(self, run: RadarRun) -> "CollectorResult":
        """Collect one platform for the supplied radar run."""


@dataclass(frozen=True)
class PendingRawPayload:
    """Immutable provider bytes awaiting Task 12A redaction and persistence."""

    run_id: str
    provider: str
    artifact_name: str
    observed_at: datetime
    payload: object

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _pending_run_id(self.run_id))
        object.__setattr__(self, "provider", _pending_provider(self.provider))
        object.__setattr__(
            self,
            "artifact_name",
            _pending_artifact_name(self.artifact_name),
        )
        object.__setattr__(
            self,
            "observed_at",
            _pending_observed_at(self.observed_at),
        )
        try:
            payload = _freeze_pending_json(self.payload)
        except RecursionError as error:
            raise InputValidationError(
                "pending raw payload must not contain recursive values"
            ) from error
        object.__setattr__(self, "payload", payload)


@dataclass(frozen=True)
class CollectorResult:
    """Immutable, provenance-checked output from one collector."""

    collector: str
    observations: tuple[PlatformObservation, ...]
    health: SourceHealth
    raw_artifacts: tuple[RawArtifact, ...]
    pending_raw_payloads: tuple[PendingRawPayload, ...] = ()

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
        pending_raw_payloads = _schema_records(
            self.pending_raw_payloads,
            "pending_raw_payloads",
            PendingRawPayload,
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
        for pending in pending_raw_payloads:
            if pending.run_id != self.health.run_id:
                raise InputValidationError(
                    "pending raw payload run_id must match health run_id"
                )
            if pending.observed_at != self.health.observed_at:
                raise InputValidationError(
                    "pending raw observed_at must match health observed_at"
                )
            if not (
                pending.provider == collector
                or pending.provider.startswith(f"{collector}_")
            ):
                raise InputValidationError(
                    "pending raw payload provider must belong to result collector"
                )
            if not (
                pending.artifact_name == f"{collector}.json"
                or pending.artifact_name.startswith(f"{collector}_")
            ):
                raise InputValidationError(
                    "pending raw artifact_name must belong to result collector"
                )
        _validate_result_semantics(
            collector,
            observations,
            self.health,
            raw_artifacts,
            pending_raw_payloads,
        )
        object.__setattr__(self, "collector", collector)
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "raw_artifacts", raw_artifacts)
        object.__setattr__(
            self,
            "pending_raw_payloads",
            pending_raw_payloads,
        )


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
    fresh_window = _positive_window(fresh_hours, "fresh_hours")
    stale_window = _positive_window(
        stale_fallback_hours,
        "stale_fallback_hours",
    )
    if fresh_window >= stale_window:
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
        active_is_fresh = bool(observations) and all(
            timedelta(0) <= parsed_now - observation.observed_at <= fresh_window
            for observation in observations
        )
        if active_is_fresh:
            status = (
                "partial"
                if any(not succeeded for succeeded in parsed_capabilities.values())
                else "fresh"
            )
        else:
            fallback_is_usable = (
                fallback is not None
                and timedelta(0) <= parsed_now - fallback <= stale_window
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


__all__ = [
    "Collector",
    "CollectorResult",
    "PendingRawPayload",
    "classify_source_health",
]
