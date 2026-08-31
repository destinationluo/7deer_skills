"""Validated configuration for the unified game opportunity radar."""

from __future__ import annotations

from dataclasses import dataclass, fields
import json
import math
from pathlib import Path
import re
from typing import TYPE_CHECKING, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigurationError

if TYPE_CHECKING:
    from steam_game_radar.config import RadarConfig as SteamRadarConfig


_COUNTRY = re.compile(r"[A-Z]{2}", flags=re.ASCII)
_LOCALE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
_STEAM_LANGUAGE = re.compile(r"[a-z][a-z0-9_]*", flags=re.ASCII)
_NUMERIC_PLATFORM_KEY = re.compile(
    r"(?:steam|roblox):[1-9][0-9]*",
    flags=re.ASCII,
)
_ITCH_IDENTIFIER = re.compile(
    r"[a-z0-9]+(?:[-_.][a-z0-9]+)*",
    flags=re.ASCII,
)
_MAX_ITCH_IDENTIFIER_LENGTH = 128
_PLATFORM_ORDER = {"itch": 0, "steam": 1, "roblox": 2}


@dataclass(frozen=True)
class IdentityAlias:
    """A versioned, explicit cross-platform identity link."""

    schema_version: int
    source: str
    target: str

    def __post_init__(self) -> None:
        _schema_version(self.schema_version, "identity alias")
        source = _platform_key(self.source, "source")
        target = _platform_key(self.target, "target")
        if source == target:
            raise ConfigurationError("identity alias source and target must differ")
        if source.partition(":")[0] == target.partition(":")[0]:
            raise ConfigurationError(
                "identity alias source and target must use different platforms"
            )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "IdentityAlias":
        if not isinstance(value, Mapping):
            raise ConfigurationError("identity alias must be a mapping")
        allowed = {field.name for field in fields(cls)}
        unknown = set(value) - allowed
        if unknown:
            names = ", ".join(sorted(str(name) for name in unknown))
            raise ConfigurationError(f"unknown identity alias keys: {names}")
        missing = allowed - set(value)
        if missing:
            names = ", ".join(sorted(missing))
            raise ConfigurationError(f"missing identity alias keys: {names}")
        return cls(
            schema_version=value["schema_version"],  # type: ignore[arg-type]
            source=value["source"],  # type: ignore[arg-type]
            target=value["target"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class RadarConfig:
    """Complete version-1 configuration with deterministic path resolution."""

    schema_version: int = 1
    timezone: str = "Asia/Shanghai"
    country: str = "US"
    locale: str = "en"
    steam_language: str = "english"
    steam_released_candidate_limit: int = 50
    steam_unreleased_candidate_limit: int = 50
    collection_hours: tuple[int, ...] = (10, 16)
    daily_publish_hour: int = 16
    enabled_platforms: tuple[str, ...] = ("itch", "steam", "roblox")
    preliminary_top_n: int = 20
    enrichment_top_n: int = 10
    final_top_n: int = 10
    heat_floor: float = 30.0
    fresh_hours: int = 6
    stale_fallback_hours: int = 72
    raw_retention_days: int = 14
    raw_max_bytes_per_provider: int = 5_242_880
    request_timeout_seconds: float = 15.0
    max_retries: int = 3
    minimum_request_interval_seconds: float = 1.0
    data_dir: Path = Path("data/unified-game-radar")
    report_dir: Path = Path("reports/unified-game-radar")
    identity_aliases: tuple[IdentityAlias, ...] = ()

    def __post_init__(self) -> None:
        _schema_version(self.schema_version, "configuration")
        _timezone(self.timezone)
        _country(self.country)
        _locale(self.locale)
        _steam_language(self.steam_language)

        for name in (
            "steam_released_candidate_limit",
            "steam_unreleased_candidate_limit",
            "preliminary_top_n",
            "enrichment_top_n",
            "final_top_n",
            "fresh_hours",
            "stale_fallback_hours",
            "raw_retention_days",
            "raw_max_bytes_per_provider",
        ):
            _positive_integer(getattr(self, name), name)

        max_retries = _integer(self.max_retries, "max_retries")
        if max_retries < 0:
            raise ConfigurationError("max_retries must not be negative")

        for name in (
            "heat_floor",
            "request_timeout_seconds",
            "minimum_request_interval_seconds",
        ):
            parsed = _positive_number(getattr(self, name), name)
            object.__setattr__(self, name, parsed)
        if self.heat_floor > 100:
            raise ConfigurationError("heat_floor must not exceed 100")

        collection_hours = _collection_hours(self.collection_hours)
        object.__setattr__(self, "collection_hours", collection_hours)
        publish_hour = _hour(self.daily_publish_hour, "daily_publish_hour")
        if publish_hour not in collection_hours:
            raise ConfigurationError(
                "daily_publish_hour must be one of collection_hours"
            )

        object.__setattr__(
            self,
            "enabled_platforms",
            _enabled_platforms(self.enabled_platforms),
        )
        aliases = _identity_aliases(self.identity_aliases)
        object.__setattr__(self, "identity_aliases", aliases)

        if self.fresh_hours >= self.stale_fallback_hours:
            raise ConfigurationError(
                "fresh_hours must be below stale_fallback_hours"
            )
        if self.enrichment_top_n > self.preliminary_top_n:
            raise ConfigurationError(
                "enrichment_top_n must not exceed preliminary_top_n"
            )
        if self.final_top_n > self.enrichment_top_n:
            raise ConfigurationError("final_top_n must not exceed enrichment_top_n")

        object.__setattr__(self, "data_dir", _path(self.data_dir, "data_dir"))
        object.__setattr__(
            self,
            "report_dir",
            _path(self.report_dir, "report_dir"),
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        project_root: Path | None = None,
    ) -> "RadarConfig":
        if not isinstance(value, Mapping):
            raise ConfigurationError("configuration must be a mapping")

        allowed = {field.name for field in fields(cls)}
        unknown = set(value) - allowed
        if unknown:
            names = ", ".join(sorted(str(name) for name in unknown))
            raise ConfigurationError(f"unknown configuration keys: {names}")

        defaults = cls()
        values = {
            field.name: value.get(field.name, getattr(defaults, field.name))
            for field in fields(cls)
        }
        values["collection_hours"] = _sequence(
            values["collection_hours"], "collection_hours"
        )
        values["enabled_platforms"] = _sequence(
            values["enabled_platforms"], "enabled_platforms"
        )
        values["identity_aliases"] = _identity_aliases(
            values["identity_aliases"]
        )

        root = Path.cwd() if project_root is None else Path(project_root)
        for name in ("data_dir", "report_dir"):
            configured_path = _path(values[name], name)
            values[name] = (
                configured_path
                if configured_path.is_absolute()
                else root / configured_path
            )
        return cls(**values)  # type: ignore[arg-type]

    @classmethod
    def from_file(
        cls,
        path: Path,
        project_root: Path | None = None,
    ) -> "RadarConfig":
        try:
            raw = json.loads(
                Path(path).read_text(encoding="utf-8"),
                object_pairs_hook=_strict_json_object,
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ConfigurationError(f"unable to read configuration: {path}") from error
        if not isinstance(raw, Mapping):
            raise ConfigurationError("configuration file must contain a JSON object")
        return cls.from_mapping(raw, project_root=project_root)

    def to_steam_config(self) -> "SteamRadarConfig":
        """Construct the complete legacy Steam provider configuration lazily."""

        from steam_game_radar.config import RadarConfig as SteamRadarConfig

        schedule = f"0 {','.join(str(hour) for hour in self.collection_hours)} * * *"
        return SteamRadarConfig(
            schema_version=self.schema_version,
            country=self.country,
            language=self.steam_language,
            timezone=self.timezone,
            schedule=schedule,
            released_candidate_limit=self.steam_released_candidate_limit,
            unreleased_candidate_limit=self.steam_unreleased_candidate_limit,
            preliminary_top_n=self.preliminary_top_n,
            enrichment_top_n=self.enrichment_top_n,
            final_top_n=self.final_top_n,
            request_timeout_seconds=self.request_timeout_seconds,
            max_retries=self.max_retries,
            minimum_request_interval_seconds=self.minimum_request_interval_seconds,
            raw_retention_days=self.raw_retention_days,
            raw_max_bytes_per_provider=self.raw_max_bytes_per_provider,
            stale_warning_hours=self.fresh_hours,
            stale_fallback_limit_hours=self.stale_fallback_hours,
            data_dir=self.data_dir / "steam",
            report_dir=self.report_dir / "steam",
        )


def _schema_version(value: object, owner: str) -> int:
    parsed = _integer(value, "schema_version")
    if parsed != 1:
        raise ConfigurationError(
            f"unsupported {owner} schema_version: {parsed}"
        )
    return parsed


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an integer")
    return value


def _positive_integer(value: object, name: str) -> int:
    parsed = _integer(value, name)
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return parsed


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as error:
        raise ConfigurationError(f"{name} must be a finite number") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return parsed


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ConfigurationError(
            f"{name} must be a non-empty string without surrounding whitespace"
        )
    return value


def _timezone(value: object) -> str:
    timezone = _text(value, "timezone")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ConfigurationError(f"unknown timezone: {timezone}") from error
    return timezone


def _country(value: object) -> str:
    country = _text(value, "country")
    if _COUNTRY.fullmatch(country) is None:
        raise ConfigurationError("country must be two uppercase ASCII letters")
    return country


def _locale(value: object) -> str:
    locale = _text(value, "locale")
    if _LOCALE.fullmatch(locale) is None:
        raise ConfigurationError("locale must be a valid language tag")
    return locale


def _steam_language(value: object) -> str:
    language = _text(value, "steam_language")
    if _STEAM_LANGUAGE.fullmatch(language) is None:
        raise ConfigurationError("steam_language must be a lowercase Steam language")
    return language


def _sequence(value: object, name: str) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise ConfigurationError(f"{name} must be an array")
    return tuple(value)


def _collection_hours(value: object) -> tuple[int, ...]:
    values = _sequence(value, "collection_hours")
    if not values:
        raise ConfigurationError("collection_hours must not be empty")
    hours = tuple(_hour(value, "collection_hours") for value in values)
    if tuple(sorted(set(hours))) != hours:
        raise ConfigurationError(
            "collection_hours must be unique and in ascending order"
        )
    return hours


def _hour(value: object, name: str) -> int:
    hour = _integer(value, name)
    if hour < 0 or hour > 23:
        raise ConfigurationError(f"{name} must contain hours from 0 through 23")
    return hour


def _enabled_platforms(value: object) -> tuple[str, ...]:
    values = _sequence(value, "enabled_platforms")
    if not values:
        raise ConfigurationError("enabled_platforms must not be empty")
    for platform in values:
        if not isinstance(platform, str) or platform not in _PLATFORM_ORDER:
            raise ConfigurationError(
                "enabled_platforms supports only itch, steam, and roblox"
            )
    expected = tuple(sorted(set(values), key=_PLATFORM_ORDER.__getitem__))
    if values != expected:
        raise ConfigurationError(
            "enabled_platforms must be unique and in itch, steam, roblox order"
        )
    return values  # type: ignore[return-value]


def _identity_aliases(value: object) -> tuple[IdentityAlias, ...]:
    values = _sequence(value, "identity_aliases")
    aliases: list[IdentityAlias] = []
    for item in values:
        if isinstance(item, IdentityAlias):
            aliases.append(item)
        elif isinstance(item, Mapping):
            aliases.append(IdentityAlias.from_mapping(item))
        else:
            raise ConfigurationError(
                "identity_aliases must contain identity alias objects"
            )
    sources = [alias.source for alias in aliases]
    if len(set(sources)) != len(sources):
        raise ConfigurationError("identity alias sources must be unique")
    return tuple(aliases)


def _platform_key(value: object, name: str) -> str:
    key = _text(value, name)
    if _NUMERIC_PLATFORM_KEY.fullmatch(key) is not None:
        return key
    platform, separator, identifier = key.partition(":")
    if (
        platform != "itch"
        or not separator
        or len(identifier) > _MAX_ITCH_IDENTIFIER_LENGTH
        or _ITCH_IDENTIFIER.fullmatch(identifier) is None
    ):
        raise ConfigurationError(
            f"{name} must be an exact itch, steam, or roblox platform key"
        )
    return key


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _path(value: object, name: str) -> Path:
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str) and value:
        path = Path(value)
    else:
        raise ConfigurationError(f"{name} must be a non-empty path")
    return path
