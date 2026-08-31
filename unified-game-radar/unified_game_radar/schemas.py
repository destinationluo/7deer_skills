"""Strict, immutable version-1 contracts shared by the unified radar."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date as Date
from datetime import datetime, timezone
from decimal import Decimal
import ipaddress
import math
import re
from types import MappingProxyType
from typing import Mapping, Type, TypeVar
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import InputValidationError


_SCHEMA_VERSION = 1
_PLATFORMS = frozenset({"itch", "steam", "roblox"})
_RUN_MODES = frozenset({"scheduled", "manual"})
_PHASES = frozenset({"preliminary", "final"})
_SOURCE_STATUSES = frozenset(
    {"fresh", "partial", "stale", "unavailable", "not_run"}
)
_DEMAND_STATES = frozenset({"pass", "early_watch", "fail", "unknown"})
_ACTIONS = frozenset(
    {
        "immediate_action",
        "worth_content_mvp",
        "watch",
        "skip",
        "needs_verification",
    }
)
_OUTSTANDING_ACTIONS = frozenset(
    {"collect_browser_observations", "collect_demand_evidence"}
)
_AUTHOR_RELATIONS = frozenset({"independent", "developer", "unknown"})
_MISSING_INTENTS = frozenset({"guide", "codes", "answers", "wiki"})
MAX_SAFE_INTEGER = 2**53 - 1
_RUN_ID = re.compile(
    r"(?P<timestamp>\d{8}T\d{6}Z)-(?P<suffix>[0-9a-f]{8,32})\Z"
)
_PLATFORM_ID = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9]|[._-](?=[A-Za-z0-9])){0,127}\Z"
)
_SURFACE = re.compile(
    r"[a-z0-9](?:[a-z0-9]|[_-](?=[a-z0-9])){0,63}\Z"
)
_HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
    r"[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
    r"[A-Za-z0-9])?))*\Z"
)
_WARNING_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_COUNTRY = re.compile(r"[A-Z]{2}\Z")
_LOCALE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UTC_TEXT = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z"
)


T = TypeVar("T")


@dataclass(frozen=True)
class _ObservationIdParts:
    raw: str
    platform: str
    platform_id: str
    surface: str
    observed_at: datetime


@dataclass(frozen=True)
class _PlatformKeyParts:
    raw: str
    platform: str
    platform_id: str


def _error(message: str) -> InputValidationError:
    return InputValidationError(message)


def _strict_mapping(
    value: object,
    schema_type: Type[object],
) -> Mapping[str, object]:
    owner = schema_type.__name__
    if not isinstance(value, Mapping):
        raise _error(f"{owner} must be a JSON object")
    expected = tuple(field.name for field in fields(schema_type))
    actual = set(value)
    unexpected = actual - set(expected)
    if unexpected:
        names = ", ".join(sorted(str(name) for name in unexpected))
        raise _error(f"{owner} has unexpected keys: {names}")
    missing = set(expected) - actual
    if missing:
        names = ", ".join(sorted(missing))
        raise _error(f"{owner} is missing keys: {names}")
    return value


def _to_dict(instance: object) -> dict[str, object]:
    return {
        field.name: _json_value(getattr(instance, field.name))
        for field in fields(instance)
    }


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return _format_utc(value)
    if isinstance(value, Date):
        return value.isoformat()
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if type(value) is int:
        return _integer(value, "JSON integer")
    if type(value) is float:
        if not math.isfinite(value) or abs(value) > MAX_SAFE_INTEGER:
            raise _error("JSON numbers must be finite and within the safe range")
        return value
    if value is None or type(value) in (str, bool):
        return value
    raise _error(f"unsupported JSON value: {type(value).__name__}")


def _schema_version(value: object) -> int:
    parsed = _integer(value, "schema_version", minimum=1)
    if parsed != _SCHEMA_VERSION:
        raise _error(f"unsupported schema_version: {parsed}")
    return parsed


def _text(value: object, name: str, maximum: int = 2048) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _error(f"{name} must be nonempty text without surrounding whitespace")
    if "\x00" in value or len(value) > maximum:
        raise _error(f"{name} is invalid")
    return value


def _optional_text(value: object, name: str, maximum: int = 2048) -> str | None:
    if value is None:
        return None
    return _text(value, name, maximum)


def _literal(value: object, name: str, allowed: frozenset[str]) -> str:
    parsed = _text(value, name, 128)
    if parsed not in allowed:
        choices = ", ".join(sorted(allowed))
        raise _error(f"{name} must be one of: {choices}")
    return parsed


def _compact_utc_timestamp(value: object, name: str) -> datetime:
    parsed = _text(value, name, 16)
    try:
        instant = datetime.strptime(parsed, "%Y%m%dT%H%M%SZ")
    except ValueError as error:
        raise _error(f"{name} must contain a valid compact UTC timestamp") from error
    if instant.strftime("%Y%m%dT%H%M%SZ") != parsed:
        raise _error(f"{name} must contain a canonical compact UTC timestamp")
    return instant.replace(tzinfo=timezone.utc)


def _run_id(value: object) -> str:
    parsed = _text(value, "run_id", 50)
    match = _RUN_ID.fullmatch(parsed)
    if match is None:
        raise _error(
            "run_id must be YYYYMMDDTHHMMSSZ followed by an 8-32 character "
            "lowercase hexadecimal suffix"
        )
    _compact_utc_timestamp(match.group("timestamp"), "run_id timestamp")
    return parsed


def _platform_id(value: object, name: str = "platform_id") -> str:
    parsed = _text(value, name, 128)
    if _PLATFORM_ID.fullmatch(parsed) is None:
        raise _error(
            f"{name} must be a 1-128 character safe ASCII token without "
            "leading, trailing, or consecutive separators"
        )
    return parsed


def _surface(value: object, name: str = "surface") -> str:
    parsed = _text(value, name, 64)
    if _SURFACE.fullmatch(parsed) is None:
        raise _error(
            f"{name} must be a lowercase slug without leading, trailing, "
            "or consecutive separators"
        )
    return parsed


def _parse_observation_id(value: object) -> _ObservationIdParts:
    parsed = _text(value, "observation_id", 240)
    parts = parsed.split(":")
    if len(parts) != 4:
        raise _error(
            "observation_id must be platform:platform_id:surface:timestamp"
        )
    platform, platform_id, surface, timestamp = parts
    return _ObservationIdParts(
        raw=parsed,
        platform=_platform(platform),
        platform_id=_platform_id(platform_id),
        surface=_surface(surface),
        observed_at=_compact_utc_timestamp(timestamp, "observation_id timestamp"),
    )


def _uuid(value: object, name: str) -> str:
    parsed = _text(value, name, 36)
    try:
        canonical = str(UUID(parsed))
    except (ValueError, AttributeError) as error:
        raise _error(f"{name} must be a UUID") from error
    if parsed != canonical:
        raise _error(f"{name} must be a canonical lowercase UUID")
    return parsed


def _platform(value: object, name: str = "platform") -> str:
    return _literal(value, name, _PLATFORMS)


def _parse_platform_key(value: object) -> _PlatformKeyParts:
    parsed = _text(value, "platform_key", 136)
    platform, separator, platform_id = parsed.partition(":")
    if not separator or not platform_id or ":" in platform_id:
        raise _error("platform_key must contain a platform and platform ID")
    return _PlatformKeyParts(
        raw=parsed,
        platform=_platform(platform),
        platform_id=_platform_id(platform_id),
    )


def _domain(value: object, name: str) -> str:
    parsed = _text(value, name, 253)
    if _DOMAIN.fullmatch(parsed) is None:
        raise _error(f"{name} must be a lowercase domain")
    return parsed


def _optional_domain(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _domain(value, name)


def _country(value: object) -> str:
    parsed = _text(value, "geo", 2)
    if _COUNTRY.fullmatch(parsed) is None:
        raise _error("geo must be a two-letter uppercase country code")
    return parsed


def _locale(value: object) -> str:
    parsed = _text(value, "locale", 64)
    if _LOCALE.fullmatch(parsed) is None:
        raise _error("locale is invalid")
    return parsed


def _timezone_name(value: object) -> str:
    parsed = _text(value, "timezone", 128)
    try:
        ZoneInfo(parsed)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise _error(f"unknown timezone: {parsed}") from error
    return parsed


def _https_url(value: object, name: str) -> str:
    parsed = _text(value, name, 8192)
    try:
        split = urlsplit(parsed)
        hostname = split.hostname
        port = split.port
    except ValueError as error:
        raise _error(f"{name} must be a valid HTTPS URL") from error
    if (
        split.scheme != "https"
        or not split.netloc
        or hostname is None
        or split.username is not None
        or split.password is not None
    ):
        raise _error(f"{name} must be an HTTPS URL without credentials")
    if split.netloc.endswith(":"):
        raise _error(f"{name} must not contain an empty port")
    if port is not None and port < 1:
        raise _error(f"{name} must use a valid numeric port")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        if _HOSTNAME.fullmatch(hostname) is None:
            raise _error(f"{name} must contain a valid hostname")
    return parsed


def _path_text(value: object, name: str) -> str:
    parsed = _text(value, name, 4096)
    if "://" in parsed:
        raise _error(f"{name} must be a local path")
    return parsed


def _optional_path_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _path_text(value, name)


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise _error(f"{name} must be a boolean")
    return value


def _integer(
    value: object,
    name: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if type(value) is not int:
        raise _error(f"{name} must be an integer")
    if value < -MAX_SAFE_INTEGER or value > MAX_SAFE_INTEGER:
        raise _error(f"{name} must be within the JSON safe integer range")
    if minimum is not None and value < minimum:
        raise _error(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise _error(f"{name} must not exceed {maximum}")
    return value


def _optional_integer(
    value: object,
    name: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if value is None:
        return None
    return _integer(value, name, minimum=minimum, maximum=maximum)


def _number(
    value: object,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(f"{name} must be a number")
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as error:
        raise _error(f"{name} must be finite") from error
    if not math.isfinite(parsed):
        raise _error(f"{name} must be finite")
    if parsed < minimum or parsed > maximum:
        raise _error(f"{name} must be between {minimum:g} and {maximum:g}")
    return parsed


def _optional_number(
    value: object,
    name: str,
    minimum: float,
    maximum: float,
) -> float | None:
    if value is None:
        return None
    return _number(value, name, minimum, maximum)


def _utc_datetime(value: object, name: str) -> datetime:
    if isinstance(value, str):
        if _UTC_TEXT.fullmatch(value) is None:
            raise _error(f"{name} must be an ISO UTC timestamp ending in Z")
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as error:
            raise _error(f"{name} must be a valid ISO UTC timestamp") from error
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise _error(f"{name} must be an ISO UTC timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise _error(f"{name} must be timezone-aware UTC")
    return parsed.astimezone(timezone.utc)


def _optional_utc_datetime(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    return _utc_datetime(value, name)


def _format_utc(value: datetime) -> str:
    parsed = _utc_datetime(value, "timestamp")
    if parsed.microsecond:
        base = parsed.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0")
    else:
        base = parsed.strftime("%Y-%m-%dT%H:%M:%S")
    return base + "Z"


def _date(value: object, name: str) -> Date:
    if isinstance(value, datetime):
        raise _error(f"{name} must be a date, not a timestamp")
    if isinstance(value, Date):
        return value
    if not isinstance(value, str):
        raise _error(f"{name} must be an ISO date")
    try:
        parsed = Date.fromisoformat(value)
    except ValueError as error:
        raise _error(f"{name} must be a valid ISO date") from error
    if parsed.isoformat() != value:
        raise _error(f"{name} must be a canonical ISO date")
    return parsed


def _sequence(value: object, name: str) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise _error(f"{name} must be an array")
    return tuple(value)


def _parsed_observation_ids(
    value: object,
    name: str = "observation_ids",
) -> tuple[_ObservationIdParts, ...]:
    parsed = tuple(_parse_observation_id(item) for item in _sequence(value, name))
    if not parsed:
        raise _error(f"{name} must not be empty")
    if len({item.raw for item in parsed}) != len(parsed):
        raise _error(f"{name} must not contain duplicates")
    return parsed


def _heat_reference_fields(
    platform_key: object,
    surface: object,
    observation_ids: object,
) -> tuple[str, str, tuple[str, ...]]:
    key = _parse_platform_key(platform_key)
    parsed_surface = _surface(surface)
    references = _parsed_observation_ids(observation_ids)
    for reference in references:
        if (
            reference.platform != key.platform
            or reference.platform_id != key.platform_id
        ):
            raise _error("observation reference does not match platform_key")
    return key.raw, parsed_surface, tuple(item.raw for item in references)


def _one_decimal_number(
    value: object,
    name: str,
    maximum: float,
) -> float:
    parsed = _number(value, name, 0, maximum)
    decimal_value = Decimal(str(parsed))
    if decimal_value != decimal_value.quantize(Decimal("0.1")):
        raise _error(f"{name} must have at most one decimal place")
    return parsed


def _text_tuple(
    value: object,
    name: str,
    *,
    allow_empty: bool = True,
    validator=None,
) -> tuple[str, ...]:
    items = _sequence(value, name)
    if not allow_empty and not items:
        raise _error(f"{name} must not be empty")
    check = validator or (lambda item: _text(item, name))
    parsed = tuple(check(item) for item in items)
    if len(set(parsed)) != len(parsed):
        raise _error(f"{name} must not contain duplicates")
    return parsed


def _typed_tuple(value: object, name: str, item_type: Type[T]) -> tuple[T, ...]:
    items = _sequence(value, name)
    parsed: list[T] = []
    for item in items:
        if not isinstance(item, item_type):
            raise _error(f"{name} must contain only {item_type.__name__} records")
        parsed.append(item)
    return tuple(parsed)


def _from_record_tuple(
    value: object,
    name: str,
    item_type: Type[T],
) -> tuple[T, ...]:
    return tuple(item_type.from_dict(item) for item in _sequence(value, name))


def _freeze_json(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _error(f"{name} mapping keys must be strings")
            frozen[key] = _freeze_json(item, f"{name}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, name) for item in value)
    if value is None or type(value) in (str, bool):
        return value
    if type(value) is int:
        return _integer(value, name)
    if type(value) is float:
        if not math.isfinite(value) or abs(value) > MAX_SAFE_INTEGER:
            raise _error(
                f"{name} contains a number outside the finite JSON safe range"
            )
        return value
    raise _error(f"{name} contains a non-JSON value")


def _frozen_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _error(f"{name} must be a JSON object")
    frozen = _freeze_json(value, name)
    assert isinstance(frozen, Mapping)
    return frozen


def _boolean_mapping(value: object, name: str) -> Mapping[str, bool]:
    if not isinstance(value, Mapping):
        raise _error(f"{name} must be an object")
    parsed: dict[str, bool] = {}
    for key, item in value.items():
        parsed[_text(key, f"{name} key", 128)] = _boolean(
            item, f"{name}.{key}"
        )
    return MappingProxyType(parsed)


@dataclass(frozen=True)
class RadarRun:
    schema_version: int
    run_id: str
    started_at: datetime
    mode: str
    platforms: tuple[str, ...]
    publish_daily: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "run_id", _run_id(self.run_id))
        object.__setattr__(self, "started_at", _utc_datetime(self.started_at, "started_at"))
        object.__setattr__(self, "mode", _literal(self.mode, "mode", _RUN_MODES))
        platforms = _text_tuple(
            self.platforms,
            "platforms",
            allow_empty=False,
            validator=_platform,
        )
        object.__setattr__(self, "platforms", platforms)
        object.__setattr__(self, "publish_daily", _boolean(self.publish_daily, "publish_daily"))

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "RadarRun":
        data = _strict_mapping(value, cls)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class PlatformRecord:
    schema_version: int
    platform: str
    platform_id: str
    name: str
    developer: str | None
    official_domain: str | None
    url: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "platform", _platform(self.platform))
        object.__setattr__(self, "platform_id", _platform_id(self.platform_id))
        object.__setattr__(self, "name", _text(self.name, "name", 512))
        object.__setattr__(self, "developer", _optional_text(self.developer, "developer", 512))
        object.__setattr__(self, "official_domain", _optional_domain(self.official_domain, "official_domain"))
        object.__setattr__(self, "url", _https_url(self.url, "url"))

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "PlatformRecord":
        data = _strict_mapping(value, cls)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class GameIdentity:
    schema_version: int
    opportunity_id: str
    name: str
    normalized_name: str
    developer: str | None
    official_domain: str | None
    platform_records: tuple[PlatformRecord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "opportunity_id", _uuid(self.opportunity_id, "opportunity_id"))
        object.__setattr__(self, "name", _text(self.name, "name", 512))
        object.__setattr__(self, "normalized_name", _text(self.normalized_name, "normalized_name", 512))
        object.__setattr__(self, "developer", _optional_text(self.developer, "developer", 512))
        object.__setattr__(self, "official_domain", _optional_domain(self.official_domain, "official_domain"))
        records = _typed_tuple(self.platform_records, "platform_records", PlatformRecord)
        keys = tuple((record.platform, record.platform_id) for record in records)
        if len(set(keys)) != len(keys):
            raise _error("platform_records must not contain duplicate platform IDs")
        object.__setattr__(self, "platform_records", records)

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "GameIdentity":
        data = dict(_strict_mapping(value, cls))
        data["platform_records"] = _from_record_tuple(
            data["platform_records"], "platform_records", PlatformRecord
        )
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class PlatformObservation:
    schema_version: int
    observation_id: str
    run_id: str
    platform: str
    platform_id: str
    provider: str
    surface: str
    geo: str
    locale: str
    query_parameters: Mapping[str, object]
    metric_definition_version: int
    observed_at: datetime
    release_at: datetime | None
    source_rank: int | None
    raw_metrics: Mapping[str, object]
    evidence_urls: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        observation_id = _parse_observation_id(self.observation_id)
        object.__setattr__(self, "observation_id", observation_id.raw)
        object.__setattr__(self, "run_id", _run_id(self.run_id))
        platform = _platform(self.platform)
        platform_id = _platform_id(self.platform_id)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "platform_id", platform_id)
        object.__setattr__(self, "provider", _text(self.provider, "provider", 128))
        surface = _surface(self.surface)
        object.__setattr__(self, "surface", surface)
        object.__setattr__(self, "geo", _country(self.geo))
        object.__setattr__(self, "locale", _locale(self.locale))
        object.__setattr__(self, "query_parameters", _frozen_mapping(self.query_parameters, "query_parameters"))
        object.__setattr__(self, "metric_definition_version", _integer(self.metric_definition_version, "metric_definition_version", minimum=1))
        observed_at = _utc_datetime(self.observed_at, "observed_at")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "release_at", _optional_utc_datetime(self.release_at, "release_at"))
        object.__setattr__(self, "source_rank", _optional_integer(self.source_rank, "source_rank", minimum=1))
        object.__setattr__(self, "raw_metrics", _frozen_mapping(self.raw_metrics, "raw_metrics"))
        object.__setattr__(self, "evidence_urls", _text_tuple(self.evidence_urls, "evidence_urls", validator=lambda item: _https_url(item, "evidence_url")))
        if (
            observation_id.platform != platform
            or observation_id.platform_id != platform_id
            or observation_id.surface != surface
            or observation_id.observed_at != observed_at
        ):
            raise _error(
                "observation_id provenance must exactly match platform, "
                "platform_id, surface, and observed_at"
            )

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "PlatformObservation":
        data = _strict_mapping(value, cls)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class ObservationEnvelope:
    schema_version: int
    run_id: str
    collector: str
    surface: str
    geo: str
    locale: str
    metric_definition_version: int
    observations: tuple[PlatformObservation, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "run_id", _run_id(self.run_id))
        object.__setattr__(self, "collector", _platform(self.collector, "collector"))
        object.__setattr__(self, "surface", _surface(self.surface))
        object.__setattr__(self, "geo", _country(self.geo))
        object.__setattr__(self, "locale", _locale(self.locale))
        object.__setattr__(self, "metric_definition_version", _integer(self.metric_definition_version, "metric_definition_version", minimum=1))
        observations = _typed_tuple(
            self.observations,
            "observations",
            PlatformObservation,
        )
        for observation in observations:
            if observation.run_id != self.run_id:
                raise _error("envelope observation run_id does not match envelope")
            if observation.platform != self.collector:
                raise _error("envelope observation platform does not match collector")
            if observation.surface.replace("_", "-") != self.surface.replace("_", "-"):
                raise _error("envelope observation surface does not match envelope")
            if observation.geo != self.geo or observation.locale != self.locale:
                raise _error("envelope observation geo/locale does not match envelope")
            if observation.metric_definition_version != self.metric_definition_version:
                raise _error(
                    "envelope observation metric version does not match envelope"
                )
        object.__setattr__(self, "observations", observations)

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "ObservationEnvelope":
        data = dict(_strict_mapping(value, cls))
        data["observations"] = _from_record_tuple(
            data["observations"], "observations", PlatformObservation
        )
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class TrendPoint:
    date: Date
    value: float | None
    complete: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "date", _date(self.date, "date"))
        object.__setattr__(self, "value", _optional_number(self.value, "value", 0, 100))
        object.__setattr__(self, "complete", _boolean(self.complete, "complete"))

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "TrendPoint":
        data = _strict_mapping(value, cls)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class TrendEvidence:
    query: str
    query_type: str
    timeframe: str
    geo: str
    category: int
    property: str
    timezone: str
    points: tuple[TrendPoint, ...]
    comparison_term: str | None
    comparison_average: float | None
    evidence_url: str
    raw_artifact: str | None
    observed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", _text(self.query, "query", 512))
        object.__setattr__(self, "query_type", _literal(self.query_type, "query_type", frozenset({"search_term"})))
        object.__setattr__(self, "timeframe", _text(self.timeframe, "timeframe", 128))
        object.__setattr__(self, "geo", _country(self.geo))
        object.__setattr__(self, "category", _integer(self.category, "category", minimum=0))
        object.__setattr__(self, "property", _literal(self.property, "property", frozenset({"web"})))
        object.__setattr__(self, "timezone", _timezone_name(self.timezone))
        object.__setattr__(self, "points", _typed_tuple(self.points, "points", TrendPoint))
        object.__setattr__(self, "comparison_term", _optional_text(self.comparison_term, "comparison_term", 512))
        object.__setattr__(self, "comparison_average", _optional_number(self.comparison_average, "comparison_average", 0, 100))
        object.__setattr__(self, "evidence_url", _https_url(self.evidence_url, "evidence_url"))
        object.__setattr__(self, "raw_artifact", _optional_path_text(self.raw_artifact, "raw_artifact"))
        object.__setattr__(self, "observed_at", _utc_datetime(self.observed_at, "observed_at"))

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "TrendEvidence":
        data = dict(_strict_mapping(value, cls))
        data["points"] = _from_record_tuple(data["points"], "points", TrendPoint)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class SearchQueryEvidence:
    schema_version: int
    query: str
    observed_at: datetime
    source_url: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "query", _text(self.query, "query", 512))
        object.__setattr__(self, "observed_at", _utc_datetime(self.observed_at, "observed_at"))
        object.__setattr__(self, "source_url", _https_url(self.source_url, "source_url"))

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "SearchQueryEvidence":
        data = _strict_mapping(value, cls)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class ExternalEvidence:
    source: str
    url: str
    published_at: datetime
    observed_at: datetime
    author_relation: str
    engagement_count: int | None
    evidence_kind: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _text(self.source, "source", 253))
        object.__setattr__(self, "url", _https_url(self.url, "url"))
        published_at = _utc_datetime(self.published_at, "published_at")
        observed_at = _utc_datetime(self.observed_at, "observed_at")
        if published_at > observed_at:
            raise _error("published_at must not be later than observed_at")
        object.__setattr__(self, "published_at", published_at)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "author_relation", _literal(self.author_relation, "author_relation", _AUTHOR_RELATIONS))
        object.__setattr__(self, "engagement_count", _optional_integer(self.engagement_count, "engagement_count", minimum=0))
        object.__setattr__(self, "evidence_kind", _text(self.evidence_kind, "evidence_kind", 128))

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "ExternalEvidence":
        data = _strict_mapping(value, cls)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class SerpEvidence:
    query: str
    relevant_nonofficial_results: int | None
    guide_results: int | None
    missing_intents: tuple[str, ...]
    evidence_url: str
    observed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", _text(self.query, "query", 512))
        object.__setattr__(self, "relevant_nonofficial_results", _optional_integer(self.relevant_nonofficial_results, "relevant_nonofficial_results", minimum=0))
        object.__setattr__(self, "guide_results", _optional_integer(self.guide_results, "guide_results", minimum=0))
        intents = _text_tuple(
            self.missing_intents,
            "missing_intents",
            validator=lambda item: _literal(item, "missing_intent", _MISSING_INTENTS),
        )
        object.__setattr__(self, "missing_intents", intents)
        object.__setattr__(self, "evidence_url", _https_url(self.evidence_url, "evidence_url"))
        object.__setattr__(self, "observed_at", _utc_datetime(self.observed_at, "observed_at"))

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "SerpEvidence":
        data = _strict_mapping(value, cls)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class OpportunityEvidence:
    schema_version: int
    run_id: str
    opportunity_id: str
    observed_at: datetime
    trends: TrendEvidence | None
    autocomplete_queries: tuple[SearchQueryEvidence, ...]
    related_queries: tuple[SearchQueryEvidence, ...]
    external_evidence: tuple[ExternalEvidence, ...]
    serp: SerpEvidence | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "run_id", _run_id(self.run_id))
        object.__setattr__(self, "opportunity_id", _uuid(self.opportunity_id, "opportunity_id"))
        object.__setattr__(self, "observed_at", _utc_datetime(self.observed_at, "observed_at"))
        if self.trends is not None and not isinstance(self.trends, TrendEvidence):
            raise _error("trends must be TrendEvidence or null")
        object.__setattr__(self, "autocomplete_queries", _typed_tuple(self.autocomplete_queries, "autocomplete_queries", SearchQueryEvidence))
        object.__setattr__(self, "related_queries", _typed_tuple(self.related_queries, "related_queries", SearchQueryEvidence))
        object.__setattr__(self, "external_evidence", _typed_tuple(self.external_evidence, "external_evidence", ExternalEvidence))
        if self.serp is not None and not isinstance(self.serp, SerpEvidence):
            raise _error("serp must be SerpEvidence or null")

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "OpportunityEvidence":
        data = dict(_strict_mapping(value, cls))
        if data["trends"] is not None:
            data["trends"] = TrendEvidence.from_dict(data["trends"])
        data["autocomplete_queries"] = _from_record_tuple(data["autocomplete_queries"], "autocomplete_queries", SearchQueryEvidence)
        data["related_queries"] = _from_record_tuple(data["related_queries"], "related_queries", SearchQueryEvidence)
        data["external_evidence"] = _from_record_tuple(data["external_evidence"], "external_evidence", ExternalEvidence)
        if data["serp"] is not None:
            data["serp"] = SerpEvidence.from_dict(data["serp"])
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class WarningRecord:
    schema_version: int
    code: str
    message: str
    collector: str | None
    opportunity_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        code = _text(self.code, "code", 128)
        if _WARNING_CODE.fullmatch(code) is None:
            raise _error("code must be a lowercase warning identifier")
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "message", _text(self.message, "message", 2048))
        if self.collector is not None:
            object.__setattr__(self, "collector", _platform(self.collector, "collector"))
        if self.opportunity_id is not None:
            object.__setattr__(self, "opportunity_id", _uuid(self.opportunity_id, "opportunity_id"))

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "WarningRecord":
        data = _strict_mapping(value, cls)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class SourceHealth:
    schema_version: int
    run_id: str
    collector: str
    status: str
    observed_at: datetime
    capabilities: Mapping[str, bool]
    warnings: tuple[WarningRecord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "run_id", _run_id(self.run_id))
        object.__setattr__(self, "collector", _platform(self.collector, "collector"))
        object.__setattr__(self, "status", _literal(self.status, "status", _SOURCE_STATUSES))
        object.__setattr__(self, "observed_at", _utc_datetime(self.observed_at, "observed_at"))
        object.__setattr__(self, "capabilities", _boolean_mapping(self.capabilities, "capabilities"))
        object.__setattr__(self, "warnings", _typed_tuple(self.warnings, "warnings", WarningRecord))

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "SourceHealth":
        data = dict(_strict_mapping(value, cls))
        data["warnings"] = _from_record_tuple(data["warnings"], "warnings", WarningRecord)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class PlatformHeat:
    schema_version: int
    run_id: str
    platform_key: str
    surface: str
    observation_ids: tuple[str, ...]
    heat: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "run_id", _run_id(self.run_id))
        platform_key, surface, observation_ids = _heat_reference_fields(
            self.platform_key,
            self.surface,
            self.observation_ids,
        )
        object.__setattr__(self, "platform_key", platform_key)
        object.__setattr__(self, "surface", surface)
        object.__setattr__(self, "observation_ids", observation_ids)
        object.__setattr__(self, "heat", _number(self.heat, "heat", 0, 100))

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "PlatformHeat":
        data = _strict_mapping(value, cls)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class NormalizedHeat:
    schema_version: int
    run_id: str
    platform_key: str
    surface: str
    observation_ids: tuple[str, ...]
    heat: float
    platform_score: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "run_id", _run_id(self.run_id))
        platform_key, surface, observation_ids = _heat_reference_fields(
            self.platform_key,
            self.surface,
            self.observation_ids,
        )
        object.__setattr__(self, "platform_key", platform_key)
        object.__setattr__(self, "surface", surface)
        object.__setattr__(self, "observation_ids", observation_ids)
        object.__setattr__(self, "heat", _number(self.heat, "heat", 0, 100))
        object.__setattr__(self, "platform_score", _number(self.platform_score, "platform_score", 0, 30))

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "NormalizedHeat":
        data = _strict_mapping(value, cls)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class ScoredOpportunity:
    schema_version: int
    run_id: str
    opportunity_id: str
    demand_state: str
    platform_score: float
    demand_score: float
    external_score: float
    seo_score: float
    total_score: float
    action: str
    warnings: tuple[WarningRecord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "run_id", _run_id(self.run_id))
        object.__setattr__(self, "opportunity_id", _uuid(self.opportunity_id, "opportunity_id"))
        object.__setattr__(self, "demand_state", _literal(self.demand_state, "demand_state", _DEMAND_STATES))
        platform_score = _one_decimal_number(
            self.platform_score, "platform_score", 30
        )
        demand_score = _one_decimal_number(self.demand_score, "demand_score", 30)
        external_score = _one_decimal_number(
            self.external_score, "external_score", 20
        )
        seo_score = _one_decimal_number(self.seo_score, "seo_score", 20)
        total_score = _one_decimal_number(self.total_score, "total_score", 100)
        expected_total = sum(
            Decimal(str(score))
            for score in (
                platform_score,
                demand_score,
                external_score,
                seo_score,
            )
        ).quantize(Decimal("0.1"))
        if Decimal(str(total_score)) != expected_total:
            raise _error("total_score must equal the sum of component scores")
        object.__setattr__(self, "platform_score", platform_score)
        object.__setattr__(self, "demand_score", demand_score)
        object.__setattr__(self, "external_score", external_score)
        object.__setattr__(self, "seo_score", seo_score)
        object.__setattr__(self, "total_score", total_score)
        object.__setattr__(self, "action", _literal(self.action, "action", _ACTIONS))
        object.__setattr__(self, "warnings", _typed_tuple(self.warnings, "warnings", WarningRecord))

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "ScoredOpportunity":
        data = dict(_strict_mapping(value, cls))
        data["warnings"] = _from_record_tuple(data["warnings"], "warnings", WarningRecord)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class RawArtifact:
    schema_version: int
    run_id: str
    provider: str
    path: str
    observed_at: datetime
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "run_id", _run_id(self.run_id))
        object.__setattr__(self, "provider", _text(self.provider, "provider", 128))
        object.__setattr__(self, "path", _path_text(self.path, "path"))
        object.__setattr__(self, "observed_at", _utc_datetime(self.observed_at, "observed_at"))
        sha256 = _text(self.sha256, "sha256", 64)
        if _SHA256.fullmatch(sha256) is None:
            raise _error("sha256 must be exactly 64 lowercase hexadecimal characters")
        object.__setattr__(self, "sha256", sha256)

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "RawArtifact":
        data = _strict_mapping(value, cls)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Publication:
    schema_version: int
    run_id: str
    phase: str
    published_at: datetime
    report_json: str
    report_markdown: str
    daily_date: Date
    advances_daily_latest: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "run_id", _run_id(self.run_id))
        object.__setattr__(self, "phase", _literal(self.phase, "phase", _PHASES))
        object.__setattr__(self, "published_at", _utc_datetime(self.published_at, "published_at"))
        object.__setattr__(self, "report_json", _path_text(self.report_json, "report_json"))
        object.__setattr__(self, "report_markdown", _path_text(self.report_markdown, "report_markdown"))
        object.__setattr__(self, "daily_date", _date(self.daily_date, "daily_date"))
        object.__setattr__(self, "advances_daily_latest", _boolean(self.advances_daily_latest, "advances_daily_latest"))

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "Publication":
        data = _strict_mapping(value, cls)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class OutstandingTask:
    schema_version: int
    run_id: str
    collector: str
    surface: str
    action: str
    collection_contract: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "run_id", _run_id(self.run_id))
        object.__setattr__(self, "collector", _platform(self.collector, "collector"))
        object.__setattr__(self, "surface", _surface(self.surface))
        object.__setattr__(self, "action", _literal(self.action, "action", _OUTSTANDING_ACTIONS))
        object.__setattr__(self, "collection_contract", _frozen_mapping(self.collection_contract, "collection_contract"))

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "OutstandingTask":
        data = _strict_mapping(value, cls)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class CommandManifest:
    schema_version: int
    run_id: str
    phase: str
    report_json: str
    report_markdown: str
    source_health: tuple[SourceHealth, ...]
    warnings: tuple[WarningRecord, ...]
    outstanding_tasks: tuple[OutstandingTask, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "run_id", _run_id(self.run_id))
        object.__setattr__(self, "phase", _literal(self.phase, "phase", _PHASES))
        object.__setattr__(self, "report_json", _path_text(self.report_json, "report_json"))
        object.__setattr__(self, "report_markdown", _path_text(self.report_markdown, "report_markdown"))
        source_health = _typed_tuple(
            self.source_health,
            "source_health",
            SourceHealth,
        )
        outstanding_tasks = _typed_tuple(
            self.outstanding_tasks,
            "outstanding_tasks",
            OutstandingTask,
        )
        if any(health.run_id != self.run_id for health in source_health):
            raise _error("source_health run_id must match manifest run_id")
        if any(task.run_id != self.run_id for task in outstanding_tasks):
            raise _error("outstanding_tasks run_id must match manifest run_id")
        object.__setattr__(self, "source_health", source_health)
        object.__setattr__(self, "warnings", _typed_tuple(self.warnings, "warnings", WarningRecord))
        object.__setattr__(self, "outstanding_tasks", outstanding_tasks)

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "CommandManifest":
        data = dict(_strict_mapping(value, cls))
        data["source_health"] = _from_record_tuple(data["source_health"], "source_health", SourceHealth)
        data["warnings"] = _from_record_tuple(data["warnings"], "warnings", WarningRecord)
        data["outstanding_tasks"] = _from_record_tuple(data["outstanding_tasks"], "outstanding_tasks", OutstandingTask)
        return cls(**data)  # type: ignore[arg-type]


@dataclass(frozen=True)
class PreliminaryResult:
    schema_version: int
    run_id: str
    candidates: tuple[GameIdentity, ...]
    source_health: tuple[SourceHealth, ...]
    warnings: tuple[WarningRecord, ...]
    outstanding_tasks: tuple[OutstandingTask, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "run_id", _run_id(self.run_id))
        object.__setattr__(self, "candidates", _typed_tuple(self.candidates, "candidates", GameIdentity))
        source_health = _typed_tuple(
            self.source_health,
            "source_health",
            SourceHealth,
        )
        outstanding_tasks = _typed_tuple(
            self.outstanding_tasks,
            "outstanding_tasks",
            OutstandingTask,
        )
        if any(health.run_id != self.run_id for health in source_health):
            raise _error("source_health run_id must match preliminary run_id")
        if any(task.run_id != self.run_id for task in outstanding_tasks):
            raise _error("outstanding_tasks run_id must match preliminary run_id")
        object.__setattr__(self, "source_health", source_health)
        object.__setattr__(self, "warnings", _typed_tuple(self.warnings, "warnings", WarningRecord))
        object.__setattr__(self, "outstanding_tasks", outstanding_tasks)

    def to_dict(self) -> dict[str, object]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, value: object) -> "PreliminaryResult":
        data = dict(_strict_mapping(value, cls))
        data["candidates"] = _from_record_tuple(data["candidates"], "candidates", GameIdentity)
        data["source_health"] = _from_record_tuple(data["source_health"], "source_health", SourceHealth)
        data["warnings"] = _from_record_tuple(data["warnings"], "warnings", WarningRecord)
        data["outstanding_tasks"] = _from_record_tuple(data["outstanding_tasks"], "outstanding_tasks", OutstandingTask)
        return cls(**data)  # type: ignore[arg-type]


__all__ = [
    "CommandManifest",
    "ExternalEvidence",
    "GameIdentity",
    "MAX_SAFE_INTEGER",
    "NormalizedHeat",
    "ObservationEnvelope",
    "OpportunityEvidence",
    "OutstandingTask",
    "PlatformHeat",
    "PlatformObservation",
    "PlatformRecord",
    "PreliminaryResult",
    "Publication",
    "RadarRun",
    "RawArtifact",
    "ScoredOpportunity",
    "SearchQueryEvidence",
    "SerpEvidence",
    "SourceHealth",
    "TrendEvidence",
    "TrendPoint",
    "WarningRecord",
]
