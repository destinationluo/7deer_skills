"""Validated configuration for the Steam game radar."""

from __future__ import annotations

from dataclasses import dataclass, fields
import json
import math
from pathlib import Path
import re
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigurationError


_COUNTRY = re.compile(r"[A-Z]{2}", flags=re.ASCII)
_LANGUAGE = re.compile(r"[a-z][a-z0-9_]*", flags=re.ASCII)
_CRON_ITEM = r"(?:\*|\d+(?:-\d+)?)(?:/\d+)?"
_CRON_FIELD = re.compile(rf"{_CRON_ITEM}(?:,{_CRON_ITEM})*", flags=re.ASCII)


@dataclass(frozen=True)
class RadarConfig:
    """Versioned radar settings with deterministic path resolution."""

    schema_version: int = 1
    country: str = "US"
    language: str = "english"
    timezone: str = "Asia/Shanghai"
    schedule: str = "0 11 * * *"
    released_candidate_limit: int = 50
    unreleased_candidate_limit: int = 50
    preliminary_top_n: int = 20
    enrichment_top_n: int = 10
    final_top_n: int = 10
    request_timeout_seconds: float = 15.0
    max_retries: int = 3
    minimum_request_interval_seconds: float = 1.0
    raw_retention_days: int = 14
    raw_max_bytes_per_provider: int = 5_242_880
    stale_warning_hours: int = 36
    stale_fallback_limit_hours: int = 72
    data_dir: Path = Path("data/steam-game-radar")
    report_dir: Path = Path("reports/steam-game-radar")

    def __post_init__(self) -> None:
        schema_version = _integer(self.schema_version, "schema_version")
        if schema_version != 1:
            raise ConfigurationError(
                f"unsupported configuration schema_version: {schema_version}"
            )

        _country(self.country)
        _language(self.language)
        _timezone(self.timezone)
        _schedule(self.schedule)

        for name in (
            "released_candidate_limit",
            "unreleased_candidate_limit",
            "preliminary_top_n",
            "enrichment_top_n",
            "final_top_n",
            "raw_retention_days",
            "raw_max_bytes_per_provider",
            "stale_warning_hours",
            "stale_fallback_limit_hours",
        ):
            parsed = _integer(getattr(self, name), name)
            if parsed <= 0:
                raise ConfigurationError(f"{name} must be positive")

        max_retries = _integer(self.max_retries, "max_retries")
        if max_retries < 0:
            raise ConfigurationError("max_retries must not be negative")

        for name in (
            "request_timeout_seconds",
            "minimum_request_interval_seconds",
        ):
            parsed_float = _positive_number(getattr(self, name), name)
            object.__setattr__(self, name, parsed_float)

        if self.stale_warning_hours >= self.stale_fallback_limit_hours:
            raise ConfigurationError(
                "stale_warning_hours must be below stale_fallback_limit_hours"
            )

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

        root = Path.cwd() if project_root is None else Path(project_root)
        for name in ("data_dir", "report_dir"):
            configured_path = _path(values[name], name)
            values[name] = (
                configured_path
                if configured_path.is_absolute()
                else root / configured_path
            )

        return cls(**values)

    @classmethod
    def from_file(
        cls,
        path: Path,
        project_root: Path | None = None,
    ) -> "RadarConfig":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ConfigurationError(f"unable to read configuration: {path}") from error
        if not isinstance(raw, Mapping):
            raise ConfigurationError("configuration file must contain a JSON object")
        return cls.from_mapping(raw, project_root=project_root)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an integer")
    return value


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return parsed


def _string_without_surrounding_whitespace(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ConfigurationError(
            f"{name} must be a non-empty string without surrounding whitespace"
        )
    return value


def _country(value: object) -> str:
    country = _string_without_surrounding_whitespace(value, "country")
    if _COUNTRY.fullmatch(country) is None:
        raise ConfigurationError("country must be two uppercase ASCII letters")
    return country


def _language(value: object) -> str:
    language = _string_without_surrounding_whitespace(value, "language")
    if _LANGUAGE.fullmatch(language) is None:
        raise ConfigurationError(
            "language must begin with a lowercase ASCII letter and contain only "
            "lowercase ASCII letters, digits, or underscores"
        )
    return language


def _timezone(value: object) -> str:
    timezone = _string_without_surrounding_whitespace(value, "timezone")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ConfigurationError(f"unknown timezone: {timezone}") from error
    return timezone


def _schedule(value: object) -> str:
    schedule = _string_without_surrounding_whitespace(value, "schedule")
    cron_fields = schedule.split()
    if len(cron_fields) != 5 or any(
        _CRON_FIELD.fullmatch(field) is None for field in cron_fields
    ):
        raise ConfigurationError(
            "schedule must contain exactly five numeric cron fields"
        )
    return schedule


def _path(value: object, name: str) -> Path:
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str) and value:
        path = Path(value)
    else:
        raise ConfigurationError(f"{name} must be a non-empty path")
    return path
