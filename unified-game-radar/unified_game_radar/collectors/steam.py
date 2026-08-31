"""Thin adapter from the proven official Steam provider to unified records."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timezone
import re

from steam_game_radar.config import RadarConfig as SteamRadarConfig
from steam_game_radar.http_client import JsonHttpClient
from steam_game_radar.official_provider import (
    CollectionResult,
    collect_official,
)
from steam_game_radar.schemas import GameRecord, WarningRecord as SteamWarning

from ..config import RadarConfig
from ..errors import InputValidationError
from ..schemas import (
    PlatformObservation,
    RadarRun,
    WarningRecord,
)
from .base import (
    CollectorResult,
    PendingRawPayload,
    classify_source_health,
)


_PROVIDER = "steam_official"
_METRIC_DEFINITION_VERSION = 1
_SURFACE_RANKS = (
    ("most_played_rank", "most_played"),
    ("top_seller_rank", "top_sellers"),
    ("new_release_rank", "new_releases"),
    ("coming_soon_rank", "coming_soon"),
)
_RAW_KEY = re.compile(r"[a-z0-9](?:[a-z0-9_]*[a-z0-9])?\Z")


Provider = Callable[[JsonHttpClient, SteamRadarConfig, str], CollectionResult]


class SteamCollector:
    """Invoke the legacy official provider once and adapt its immutable output."""

    def __init__(
        self,
        config: RadarConfig,
        client: JsonHttpClient,
        collect_official_fn: Provider = collect_official,
    ) -> None:
        if not isinstance(config, RadarConfig):
            raise TypeError("config must be a RadarConfig")
        if not callable(collect_official_fn):
            raise TypeError("collect_official_fn must be callable")
        self._config = config
        self._client = client
        self._collect_official = collect_official_fn

    def collect(self, run: RadarRun) -> CollectorResult:
        """Collect Steam-owned discovery data without adding provider logic."""

        if not isinstance(run, RadarRun):
            raise TypeError("run must be a RadarRun")
        observed_at = _run_observed_at(run)
        legacy_result = self._collect_official(
            self._client,
            self._config.to_steam_config(),
            _format_utc(observed_at),
        )
        if not isinstance(legacy_result, CollectionResult):
            raise TypeError("collect_official_fn must return CollectionResult")

        observations = adapt_collection_result(
            run,
            legacy_result,
            geo=self._config.country,
            locale=self._config.locale,
            language=self._config.steam_language,
        )
        warnings = tuple(_adapt_warning(warning) for warning in legacy_result.warnings)
        health = classify_source_health(
            run_id=run.run_id,
            now=observed_at,
            attempted=True,
            active_observations=observations,
            capabilities=legacy_result.capabilities,
            fallback_observed_at=None,
            warnings=warnings,
            fresh_hours=self._config.fresh_hours,
            stale_fallback_hours=self._config.stale_fallback_hours,
            collector="steam",
        )
        payloads = adapt_raw_payloads(legacy_result)
        pending_raw_payloads = _pending_raw_payloads(
            run,
            observed_at,
            payloads,
        )
        return CollectorResult(
            collector="steam",
            observations=observations,
            health=health,
            raw_artifacts=(),
            pending_raw_payloads=pending_raw_payloads,
        )


def adapt_collection_result(
    run: RadarRun,
    legacy_result: CollectionResult,
    *,
    geo: str = "US",
    locale: str = "en",
    language: str = "english",
) -> tuple[PlatformObservation, ...]:
    """Convert each populated Steam discovery rank into one observation."""

    if not isinstance(run, RadarRun):
        raise TypeError("run must be a RadarRun")
    if not isinstance(legacy_result, CollectionResult):
        raise TypeError("legacy_result must be a CollectionResult")
    observed_at = _run_observed_at(run)
    observations: list[PlatformObservation] = []
    observations_by_id: dict[str, PlatformObservation] = {}
    for record in (*legacy_result.released, *legacy_result.unreleased):
        record_observations = _record_observations(
            run,
            record,
            observed_at,
            geo=geo,
            locale=locale,
            language=language,
        )
        for observation in record_observations:
            existing = observations_by_id.get(observation.observation_id)
            if existing is None:
                observations_by_id[observation.observation_id] = observation
                observations.append(observation)
            elif existing != observation:
                raise InputValidationError(
                    "Steam observation_id was reused with a different payload: "
                    f"{observation.observation_id}"
                )
    return tuple(observations)


def adapt_raw_payloads(
    legacy_result: CollectionResult,
) -> dict[str, object]:
    """Return lossless JSON-native copies for later unified persistence."""

    if not isinstance(legacy_result, CollectionResult):
        raise TypeError("legacy_result must be a CollectionResult")
    return legacy_result.raw_to_dict()


def _record_observations(
    run: RadarRun,
    record: GameRecord,
    observed_at: datetime,
    *,
    geo: str,
    locale: str,
    language: str,
) -> tuple[PlatformObservation, ...]:
    raw_metrics = _raw_metrics(record)
    release_at = _release_at(record)
    timestamp = observed_at.strftime("%Y%m%dT%H%M%SZ")
    observations: list[PlatformObservation] = []
    for metric_name, surface in _SURFACE_RANKS:
        rank = record.metrics.get(metric_name)
        if rank is None:
            continue
        observations.append(
            PlatformObservation(
                schema_version=1,
                observation_id=(
                    f"steam:{record.appid}:{surface}:{timestamp}"
                ),
                run_id=run.run_id,
                platform="steam",
                platform_id=str(record.appid),
                provider=_PROVIDER,
                surface=surface,
                geo=geo,
                locale=locale,
                query_parameters={
                    "country": geo,
                    "language": language,
                },
                metric_definition_version=_METRIC_DEFINITION_VERSION,
                observed_at=observed_at,
                release_at=release_at,
                source_rank=rank.value,  # validated by PlatformObservation
                raw_metrics=raw_metrics,
                evidence_urls=(record.store_url,),
            )
        )
    return tuple(observations)


def _raw_metrics(record: GameRecord) -> Mapping[str, object]:
    data = record.to_dict()
    source_extra = data["source_extra"]
    assert isinstance(source_extra, Mapping)
    discovery_sources = source_extra.get("discovery_sources", [])
    genres = source_extra.get("genres", [])
    return {
        "name": record.name,
        "release_status": record.release_status,
        "store_url": record.store_url,
        "genres": genres,
        "discovery_sources": discovery_sources,
        "metrics": data["metrics"],
    }


def _release_at(record: GameRecord) -> datetime | None:
    release_date = record.metrics.get("release_date")
    if release_date is None or not isinstance(release_date.value, str):
        return None
    try:
        parsed = date.fromisoformat(release_date.value)
    except ValueError as error:
        raise InputValidationError("Steam release_date must be an ISO date") from error
    return datetime.combine(parsed, time.min, tzinfo=timezone.utc)


def _adapt_warning(warning: SteamWarning) -> WarningRecord:
    return WarningRecord(
        schema_version=1,
        code=warning.code,
        message=warning.message,
        collector="steam",
        opportunity_id=None,
    )


def _pending_raw_payloads(
    run: RadarRun,
    observed_at: datetime,
    payloads: Mapping[str, object],
) -> tuple[PendingRawPayload, ...]:
    pending: list[PendingRawPayload] = []
    for raw_key in sorted(payloads):
        if _RAW_KEY.fullmatch(raw_key) is None:
            raise InputValidationError("Steam raw payload key is not a safe identifier")
        pending.append(
            PendingRawPayload(
                run_id=run.run_id,
                provider=_PROVIDER,
                artifact_name=f"steam_{raw_key}.json",
                observed_at=observed_at,
                payload=payloads[raw_key],
            )
        )
    return tuple(pending)


def _run_observed_at(run: RadarRun) -> datetime:
    return run.started_at.astimezone(timezone.utc).replace(microsecond=0)


def _format_utc(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "SteamCollector",
    "adapt_collection_result",
    "adapt_raw_payloads",
]
