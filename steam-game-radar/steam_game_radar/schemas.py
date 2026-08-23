"""Versioned JSON records shared by radar pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import re
from types import MappingProxyType
from typing import Literal, Mapping, cast
from urllib.parse import urlsplit

from .errors import InputValidationError


SourceKind = Literal[
    "steam_official",
    "steamdb_manual_import",
    "seo_enrichment",
]
ReleaseStatus = Literal["released", "unreleased", "unknown"]

_SOURCE_KINDS = {"steam_official", "steamdb_manual_import", "seo_enrichment"}
_RELEASE_STATUSES = {"released", "unreleased", "unknown"}
_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


@dataclass(frozen=True)
class MetricObservation:
    value: object
    source_id: str
    source_kind: SourceKind
    observed_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _freeze_json(self.value, "value"))
        _non_empty_string(self.source_id, "source_id")
        if (
            not isinstance(self.source_kind, str)
            or self.source_kind not in _SOURCE_KINDS
        ):
            raise InputValidationError(f"invalid source_kind: {self.source_kind!r}")
        _utc_timestamp(self.observed_at)

    def to_dict(self) -> dict[str, object]:
        return {
            "value": _thaw_json(self.value),
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "MetricObservation":
        _mapping_with_keys(
            value,
            required={"value", "source_id", "source_kind", "observed_at"},
            record_name="MetricObservation",
        )
        return cls(
            value=value["value"],
            source_id=_non_empty_string(value["source_id"], "source_id"),
            source_kind=_source_kind(value["source_kind"]),
            observed_at=_utc_timestamp(value["observed_at"]),
        )


@dataclass(frozen=True)
class GameRecord:
    schema_version: int
    appid: int
    name: str
    release_status: ReleaseStatus
    store_url: str
    metrics: Mapping[str, MetricObservation]
    source_extra: Mapping[str, object]

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != 1
        ):
            raise InputValidationError("schema_version must be exactly 1")
        _positive_appid(self.appid)
        _non_empty_string(self.name, "name")
        if (
            not isinstance(self.release_status, str)
            or self.release_status not in _RELEASE_STATUSES
        ):
            raise InputValidationError(
                f"invalid release_status: {self.release_status!r}"
            )
        _store_url(self.store_url, self.appid)
        if not isinstance(self.metrics, Mapping):
            raise InputValidationError("metrics must be a mapping")
        frozen_metrics: dict[str, MetricObservation] = {}
        for metric_name, observation in self.metrics.items():
            name = _non_empty_string(metric_name, "metric name")
            if not isinstance(observation, MetricObservation):
                raise InputValidationError(
                    f"metric {metric_name!r} must be a MetricObservation"
                )
            frozen_metrics[name] = observation
        if not isinstance(self.source_extra, Mapping):
            raise InputValidationError("source_extra must be a mapping")
        frozen_extra = _freeze_json(self.source_extra, "source_extra")
        object.__setattr__(self, "metrics", MappingProxyType(frozen_metrics))
        object.__setattr__(self, "source_extra", frozen_extra)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "appid": self.appid,
            "name": self.name,
            "release_status": self.release_status,
            "store_url": self.store_url,
            "metrics": {
                name: observation.to_dict()
                for name, observation in self.metrics.items()
            },
            "source_extra": _thaw_json(self.source_extra),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "GameRecord":
        _mapping_with_keys(
            value,
            required={
                "schema_version",
                "appid",
                "name",
                "release_status",
                "store_url",
                "metrics",
                "source_extra",
            },
            record_name="GameRecord",
        )
        schema_version = value["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != 1
        ):
            raise InputValidationError("schema_version must be exactly 1")
        appid = _positive_appid(value["appid"])
        metrics_value = value["metrics"]
        if not isinstance(metrics_value, Mapping):
            raise InputValidationError("metrics must be a mapping")
        metrics: dict[str, MetricObservation] = {}
        for metric_name, observation in metrics_value.items():
            name = _non_empty_string(metric_name, "metric name")
            if not isinstance(observation, Mapping):
                raise InputValidationError(
                    f"metric {metric_name!r} must be a mapping"
                )
            metrics[name] = MetricObservation.from_dict(observation)

        source_extra = value["source_extra"]
        if not isinstance(source_extra, Mapping):
            raise InputValidationError("source_extra must be a mapping")

        return cls(
            schema_version=1,
            appid=appid,
            name=_non_empty_string(value["name"], "name"),
            release_status=_release_status(value["release_status"]),
            store_url=_store_url(value["store_url"], appid),
            metrics=metrics,
            source_extra=source_extra,
        )


@dataclass(frozen=True)
class WarningRecord:
    code: str
    message: str
    appid: int | None = None

    def __post_init__(self) -> None:
        _non_empty_string(self.code, "code")
        _non_empty_string(self.message, "message")
        _optional_appid(self.appid)

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "appid": self.appid}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "WarningRecord":
        _mapping_with_keys(
            value,
            required={"code", "message", "appid"},
            record_name="WarningRecord",
        )
        return cls(
            code=_non_empty_string(value["code"], "code"),
            message=_non_empty_string(value["message"], "message"),
            appid=_optional_appid(value["appid"]),
        )


@dataclass(frozen=True)
class RejectedRow:
    row_number: int
    code: str
    message: str
    appid: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.row_number, bool) or not isinstance(self.row_number, int):
            raise InputValidationError("row_number must be a positive integer")
        if self.row_number <= 0:
            raise InputValidationError("row_number must be a positive integer")
        _non_empty_string(self.code, "code")
        _non_empty_string(self.message, "message")
        _optional_appid(self.appid)

    def to_dict(self) -> dict[str, object]:
        return {
            "row_number": self.row_number,
            "code": self.code,
            "message": self.message,
            "appid": self.appid,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RejectedRow":
        _mapping_with_keys(
            value,
            required={"row_number", "code", "message", "appid"},
            record_name="RejectedRow",
        )
        return cls(
            row_number=value["row_number"],
            code=_non_empty_string(value["code"], "code"),
            message=_non_empty_string(value["message"], "message"),
            appid=_optional_appid(value["appid"]),
        )


def _mapping_with_keys(
    value: object,
    *,
    required: set[str],
    record_name: str,
) -> None:
    if not isinstance(value, Mapping):
        raise InputValidationError(f"{record_name} must be a mapping")
    keys = set(value)
    missing = required - keys
    unknown = keys - required
    if missing:
        names = ", ".join(sorted(missing))
        raise InputValidationError(f"{record_name} missing fields: {names}")
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise InputValidationError(f"{record_name} has unknown fields: {names}")


def _non_empty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputValidationError(f"{name} must be a non-empty string")
    return value


def _positive_appid(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InputValidationError("appid must be a positive integer")
    return value


def _optional_appid(value: object) -> int | None:
    if value is None:
        return None
    return _positive_appid(value)


def _source_kind(value: object) -> SourceKind:
    if not isinstance(value, str) or value not in _SOURCE_KINDS:
        raise InputValidationError(f"invalid source_kind: {value!r}")
    return cast(SourceKind, value)


def _release_status(value: object) -> ReleaseStatus:
    if not isinstance(value, str) or value not in _RELEASE_STATUSES:
        raise InputValidationError(f"invalid release_status: {value!r}")
    return cast(ReleaseStatus, value)


def _utc_timestamp(value: object) -> str:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise InputValidationError("observed_at must be an ISO-8601 UTC timestamp")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise InputValidationError(
            "observed_at must be an ISO-8601 UTC timestamp"
        ) from error
    return value


def _store_url(value: object, appid: int) -> str:
    if not isinstance(value, str):
        raise InputValidationError("store_url must be a Steam HTTPS app URL")
    parsed = urlsplit(value)
    expected_paths = {f"/app/{appid}", f"/app/{appid}/"}
    if (
        parsed.scheme != "https"
        or parsed.netloc != "store.steampowered.com"
        or parsed.path not in expected_paths
        or parsed.query
        or parsed.fragment
    ):
        raise InputValidationError(
            f"store_url must be https://store.steampowered.com/app/{appid}"
        )
    return value


def _freeze_json(value: object, name: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InputValidationError(f"{name} must be JSON-compatible")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, nested_value in value.items():
            if not isinstance(key, str):
                raise InputValidationError(
                    f"{name} mappings must use string keys"
                )
            frozen[key] = _freeze_json(nested_value, name)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, name) for item in value)
    raise InputValidationError(f"{name} must be JSON-compatible")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value
