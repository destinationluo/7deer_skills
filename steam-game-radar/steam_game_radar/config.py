"""Validated configuration for the Steam game radar."""

from __future__ import annotations

from dataclasses import dataclass, fields
import json
import math
from pathlib import Path
from typing import Mapping

from .errors import ConfigurationError


@dataclass(frozen=True)
class RadarConfig:
    """Versioned radar settings with deterministic path resolution."""

    schema_version: int = 1
    country: str = "US"
    language: str = "english"
    timezone: str = "Asia/Shanghai"
    schedule: str = "0 11 * * *"
    released_candidate_limit: int = 100
    unreleased_candidate_limit: int = 100
    preliminary_top_n: int = 50
    enrichment_top_n: int = 20
    final_top_n: int = 20
    request_timeout_seconds: float = 10.0
    max_retries: int = 2
    minimum_request_interval_seconds: float = 1.0
    raw_retention_days: int = 14
    raw_max_bytes_per_provider: int = 5_242_880
    stale_warning_hours: int = 24
    stale_fallback_limit_hours: int = 72
    data_dir: Path = Path("data")
    report_dir: Path = Path("reports")

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

        schema_version = _integer(values["schema_version"], "schema_version")
        if schema_version != 1:
            raise ConfigurationError(
                f"unsupported configuration schema_version: {schema_version}"
            )

        positive_integer_fields = (
            "released_candidate_limit",
            "unreleased_candidate_limit",
            "preliminary_top_n",
            "enrichment_top_n",
            "final_top_n",
            "raw_retention_days",
            "raw_max_bytes_per_provider",
            "stale_warning_hours",
            "stale_fallback_limit_hours",
        )
        for name in positive_integer_fields:
            parsed = _integer(values[name], name)
            if parsed <= 0:
                raise ConfigurationError(f"{name} must be positive")
            values[name] = parsed

        max_retries = _integer(values["max_retries"], "max_retries")
        if max_retries < 0:
            raise ConfigurationError("max_retries must not be negative")
        values["max_retries"] = max_retries

        for name in (
            "request_timeout_seconds",
            "minimum_request_interval_seconds",
        ):
            parsed_float = _positive_number(values[name], name)
            values[name] = parsed_float

        if values["stale_warning_hours"] >= values["stale_fallback_limit_hours"]:
            raise ConfigurationError(
                "stale_warning_hours must be below stale_fallback_limit_hours"
            )

        for name in ("country", "language", "timezone", "schedule"):
            values[name] = _non_empty_string(values[name], name)

        root = Path.cwd() if project_root is None else Path(project_root)
        for name in ("data_dir", "report_dir"):
            configured_path = _path(values[name], name)
            values[name] = (
                configured_path
                if configured_path.is_absolute()
                else root / configured_path
            )

        values["schema_version"] = schema_version
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


def _non_empty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{name} must be a non-empty string")
    return value


def _path(value: object, name: str) -> Path:
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str) and value:
        path = Path(value)
    else:
        raise ConfigurationError(f"{name} must be a non-empty path")
    return path
