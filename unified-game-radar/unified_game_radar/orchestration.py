"""Deterministic preliminary collection, persistence, and candidate rebuild."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from uuid import UUID

from .artifacts import persist_raw_artifact
from .collectors.base import (
    Collector,
    CollectorResult,
    classify_source_health,
)
from .collectors.itch import ItchBrowserEnvelope, build_itch_observations
from .collectors.roblox import RobloxBrowserEnvelope, build_roblox_observations
from .config import RadarConfig
from .errors import InputValidationError
from .identity import match_identity, normalize_name, platform_key
from .normalize import (
    ITCH_DISCOVERY,
    ROBLOX_GLOBAL,
    STEAM_RELEASED,
    STEAM_UPCOMING,
    ItchHeatInput,
    RobloxHeatInput,
    SteamReleasedHeatInput,
    SteamUpcomingHeatInput,
    normalize_cohort,
    score_itch_heat,
    score_roblox_heat,
    score_steam_released_heat,
    score_steam_upcoming_heat,
    select_record_heat,
)
from .platform_keys import canonical_platform_key, validate_platform
from .report import (
    AtomicWriter,
    FileAtomicWriter,
    build_report,
    persist_run_artifacts,
    publish_daily_if_allowed,
)
from .score import opportunity_sort_key, score_opportunity
from .schemas import (
    CommandManifest,
    GameIdentity,
    NormalizedHeat,
    OpportunityEvidence,
    OutstandingTask,
    PlatformHeat,
    PlatformObservation,
    PlatformRecord,
    PreliminaryResult,
    RadarRun,
    ScoredOpportunity,
    SourceHealth,
    WarningRecord,
)
from .storage import RadarStore


_HEX_SUFFIX = re.compile(r"[0-9a-f]{8,32}\Z", flags=re.ASCII)
_BROWSER_SURFACES = {
    "itch": ("newest", "popular"),
    "roblox": ("rising", "up-and-coming", "charts"),
}
_BROWSER_COHORT = {
    "itch": ITCH_DISCOVERY,
    "roblox": ROBLOX_GLOBAL,
}


@dataclass(frozen=True)
class _ParsedBrowserIngest:
    observed_at: datetime
    surfaces: tuple[str, ...]
    observations: tuple[PlatformObservation, ...]


def _utc_seconds(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must return a timezone-aware UTC datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must return a timezone-aware UTC datetime")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _run_suffix(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("id_factory must return text")
    try:
        return UUID(value).hex[:8]
    except (ValueError, AttributeError):
        if _HEX_SUFFIX.fullmatch(value) is None:
            raise ValueError(
                "the first id_factory value must be a UUID or lowercase hex suffix"
            )
        return value


def new_run_id(
    clock: Callable[[], datetime],
    id_factory: Callable[[], str],
) -> tuple[datetime, str]:
    """Return the one UTC instant and run ID shared by locking and scan."""

    if not callable(clock) or not callable(id_factory):
        raise TypeError("clock and id_factory must be callable")
    started_at = _utc_seconds(clock(), "clock")
    suffix = _run_suffix(id_factory())
    return (
        started_at,
        f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-{suffix}",
    )


def _selected_platforms(
    config: RadarConfig,
    platforms: Sequence[str],
) -> tuple[str, ...]:
    if isinstance(platforms, (str, bytes)) or not isinstance(platforms, Sequence):
        raise ValueError("platforms must be a nonempty sequence")
    requested = tuple(platforms)
    if not requested or len(set(requested)) != len(requested):
        raise ValueError("platforms must be nonempty and unique")
    for platform in requested:
        validate_platform(platform)
        if platform not in config.enabled_platforms:
            raise ValueError(f"platform is disabled by configuration: {platform}")
    return tuple(
        platform for platform in config.enabled_platforms if platform in requested
    )


def _latest_observed_at(
    store: RadarStore,
    platform: str,
    before: datetime,
) -> datetime | None:
    row = store._fetchone(  # type: ignore[attr-defined]
        """
        SELECT MAX(observed_at) FROM observations
        WHERE platform = ? AND observed_at < ?
        """,
        (platform, before.isoformat().replace("+00:00", "Z")),
    )
    if row is None or row[0] is None or not isinstance(row[0], str):
        return None
    try:
        parsed = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc_seconds(parsed, "stored observation timestamp")


def _warning(platform: str, code: str, message: str) -> WarningRecord:
    return WarningRecord(
        schema_version=1,
        code=code,
        message=message,
        collector=platform,
        opportunity_id=None,
    )


def _failure_health(
    config: RadarConfig,
    store: RadarStore,
    run: RadarRun,
    platform: str,
    warning: WarningRecord,
) -> SourceHealth:
    return classify_source_health(
        run_id=run.run_id,
        now=run.started_at,
        attempted=True,
        active_observations=(),
        capabilities={"collection": False},
        fallback_observed_at=_latest_observed_at(
            store,
            platform,
            run.started_at,
        ),
        warnings=(warning,),
        fresh_hours=config.fresh_hours,
        stale_fallback_hours=config.stale_fallback_hours,
        collector=platform,
    )


def _not_run_health(
    config: RadarConfig,
    run: RadarRun,
    platform: str,
) -> SourceHealth:
    return classify_source_health(
        run_id=run.run_id,
        now=run.started_at,
        attempted=False,
        active_observations=(),
        capabilities={"browser_collection": False},
        fallback_observed_at=None,
        warnings=(),
        fresh_hours=config.fresh_hours,
        stale_fallback_hours=config.stale_fallback_hours,
        collector=platform,
    )


def _outstanding_browser_task(run: RadarRun, platform: str) -> OutstandingTask:
    surfaces = _BROWSER_SURFACES[platform]
    return OutstandingTask(
        schema_version=1,
        run_id=run.run_id,
        collector=platform,
        surface=_BROWSER_COHORT[platform],
        action="collect_browser_observations",
        collection_contract={
            "schema_version": 1,
            "required_surfaces": surfaces,
            "surface_scope": "global",
            "max_rows": 200,
            "reference": "references/collection-contract.md",
        },
    )


def _validate_collector_result(
    run: RadarRun,
    platform: str,
    result: object,
) -> CollectorResult:
    if not isinstance(result, CollectorResult):
        raise TypeError("collector must return CollectorResult")
    if result.collector != platform:
        raise ValueError("collector result platform does not match selected platform")
    if result.health.run_id != run.run_id:
        raise ValueError("collector result run_id does not match active run")
    return result


def _persist_collector_result(
    config: RadarConfig,
    store: RadarStore,
    result: CollectorResult,
) -> None:
    for pending in result.pending_raw_payloads:
        persist_raw_artifact(
            config,
            pending.run_id,
            pending.provider,
            pending.artifact_name,
            pending.payload,
            pending.observed_at,
        )
    with store.transaction():
        store.save_source_health(result.health)
        for observation in result.observations:
            store.insert_observation(observation)


def _collect_selected(
    config: RadarConfig,
    store: RadarStore,
    run: RadarRun,
    collectors: Mapping[str, Collector],
) -> tuple[
    tuple[CollectorResult, ...],
    tuple[SourceHealth, ...],
    tuple[WarningRecord, ...],
    tuple[OutstandingTask, ...],
]:
    results: list[CollectorResult] = []
    health_rows: list[SourceHealth] = []
    warnings: list[WarningRecord] = []
    outstanding: list[OutstandingTask] = []

    for platform in run.platforms:
        collector = collectors.get(platform)
        if collector is None and platform in _BROWSER_SURFACES:
            health = _not_run_health(config, run, platform)
            store.save_source_health(health)
            health_rows.append(health)
            outstanding.append(_outstanding_browser_task(run, platform))
            continue
        if collector is None:
            warning = _warning(
                platform,
                "collector_missing",
                f"{platform} collector is not configured",
            )
            health = _failure_health(config, store, run, platform, warning)
            store.save_source_health(health)
            health_rows.append(health)
            warnings.append(warning)
            continue

        try:
            result = _validate_collector_result(
                run,
                platform,
                collector.collect(run),
            )
        except Exception as error:
            warning = _warning(
                platform,
                "collector_failed",
                f"{platform} collector failed ({type(error).__name__})",
            )
            health = _failure_health(config, store, run, platform, warning)
            store.save_source_health(health)
            health_rows.append(health)
            warnings.append(warning)
            if platform in _BROWSER_SURFACES:
                outstanding.append(_outstanding_browser_task(run, platform))
            continue

        _persist_collector_result(config, store, result)
        results.append(result)
        health_rows.append(result.health)
        warnings.extend(result.health.warnings)
        if (
            platform in _BROWSER_SURFACES
            and result.health.status != "fresh"
        ):
            outstanding.append(_outstanding_browser_task(run, platform))

    return (
        tuple(results),
        tuple(health_rows),
        tuple(warnings),
        tuple(outstanding),
    )


def _load_identities(store: RadarStore) -> tuple[GameIdentity, ...]:
    rows = store._fetchall(  # type: ignore[attr-defined]
        "SELECT canonical_json FROM game_identities ORDER BY opportunity_id"
    )
    identities: list[GameIdentity] = []
    for row in rows:
        if len(row) != 1 or not isinstance(row[0], str):
            raise ValueError("stored identity row is invalid")
        identities.append(GameIdentity.from_dict(json.loads(row[0])))
    return tuple(identities)


def _metric_text(observation: PlatformObservation, *names: str) -> str | None:
    for name in names:
        value = observation.raw_metrics.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _record_from_observations(
    observations: Sequence[PlatformObservation],
) -> PlatformRecord:
    ordered = tuple(sorted(observations, key=lambda item: item.observation_id))
    first = ordered[0]
    name = _metric_text(first, "name", "title")
    if name is None:
        raise ValueError("platform observation is missing a visible game name")
    developer = _metric_text(first, "developer")
    url = _metric_text(first, "game_url", "store_url")
    if url is None:
        if not first.evidence_urls:
            raise ValueError("platform observation is missing a canonical game URL")
        url = first.evidence_urls[-1]
    return PlatformRecord(
        schema_version=1,
        platform=first.platform,
        platform_id=first.platform_id,
        name=name,
        developer=developer,
        official_domain=None,
        url=url,
    )


def _platform_records(
    observations: Sequence[PlatformObservation],
) -> tuple[PlatformRecord, ...]:
    grouped: dict[str, list[PlatformObservation]] = {}
    for observation in observations:
        key = canonical_platform_key(observation.platform, observation.platform_id)
        grouped.setdefault(key, []).append(observation)
    return tuple(
        _record_from_observations(grouped[key]) for key in sorted(grouped)
    )


def _run_observations(
    store: RadarStore,
    run_id: str,
) -> tuple[PlatformObservation, ...]:
    rows = store._fetchall(  # type: ignore[attr-defined]
        """
        SELECT canonical_json FROM observations
        WHERE run_id = ?
        ORDER BY observation_id
        """,
        (run_id,),
    )
    observations: list[PlatformObservation] = []
    for row in rows:
        if len(row) != 1 or not isinstance(row[0], str):
            raise ValueError("stored observation row is invalid")
        observation = PlatformObservation.from_dict(json.loads(row[0]))
        if observation.run_id != run_id:
            raise ValueError("stored observation run_id is invalid")
        observations.append(observation)
    return tuple(observations)


def _verified_run_observations(
    store: RadarStore,
    run: RadarRun,
) -> tuple[PlatformObservation, ...]:
    eligible_platforms: set[str] = set()
    for platform in run.platforms:
        health = store.get_source_health(run.run_id, platform)
        if (
            health is not None
            and health.run_id == run.run_id
            and health.collector == platform
            and health.status in {"fresh", "partial"}
        ):
            eligible_platforms.add(platform)
    return tuple(
        observation
        for observation in _run_observations(store, run.run_id)
        if observation.platform in eligible_platforms
    )


def _merge_record(identity: GameIdentity, record: PlatformRecord) -> GameIdentity:
    by_key = {
        platform_key(existing): existing for existing in identity.platform_records
    }
    by_key[platform_key(record)] = record
    records = tuple(by_key[key] for key in sorted(by_key))
    return GameIdentity(
        schema_version=1,
        opportunity_id=identity.opportunity_id,
        name=identity.name,
        normalized_name=identity.normalized_name,
        developer=identity.developer or record.developer,
        official_domain=identity.official_domain or record.official_domain,
        platform_records=records,
    )


def _link_identities(
    config: RadarConfig,
    store: RadarStore,
    records: Sequence[PlatformRecord],
    id_factory: Callable[[], str],
) -> tuple[GameIdentity, ...]:
    identities = list(_load_identities(store))
    touched: set[str] = set()
    with store.transaction():
        for record in records:
            opportunity_id = match_identity(
                record,
                identities,
                aliases=config.identity_aliases,
            )
            if opportunity_id is None:
                identity = GameIdentity(
                    schema_version=1,
                    opportunity_id=id_factory(),
                    name=record.name,
                    normalized_name=normalize_name(record.name),
                    developer=record.developer,
                    official_domain=record.official_domain,
                    platform_records=(record,),
                )
                identities.append(identity)
            else:
                index = next(
                    index
                    for index, existing in enumerate(identities)
                    if existing.opportunity_id == opportunity_id
                )
                identity = _merge_record(identities[index], record)
                identities[index] = identity
            store.upsert_identity(identity)
            store.bind_platform_record(identity.opportunity_id, record)
            touched.add(identity.opportunity_id)
    return tuple(
        identity for identity in identities if identity.opportunity_id in touched
    )


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _integer(value: object) -> int | None:
    if type(value) is not int or value < 0:
        return None
    return value


def _steam_metric(
    observation: PlatformObservation,
    metric_name: str,
) -> float | int | None:
    metrics = observation.raw_metrics.get("metrics")
    if not isinstance(metrics, Mapping):
        return None
    metric = metrics.get(metric_name)
    if not isinstance(metric, Mapping):
        return None
    value = metric.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _growth_percent(current: object, previous: object) -> float | None:
    current_value = _number(current)
    previous_value = _number(previous)
    if current_value is None or previous_value is None or previous_value <= 0:
        return None
    return 100 * (current_value - previous_value) / previous_value


def _compatible_previous(
    store: RadarStore,
    observation: PlatformObservation,
) -> PlatformObservation | None:
    return store.compatible_observation(
        observation,
        target_hours=24,
        tolerance_hours=6,
    )


def _earliest_observed_at(
    store: RadarStore,
    observation: PlatformObservation,
    at_or_before: datetime,
) -> datetime | None:
    row = store._fetchone(  # type: ignore[attr-defined]
        """
        SELECT MIN(observed_at) FROM observations
        WHERE platform = ?
          AND platform_id = ?
          AND observed_at <= ?
        """,
        (
            observation.platform,
            observation.platform_id,
            at_or_before.isoformat().replace("+00:00", "Z"),
        ),
    )
    if row is None or row[0] is None or not isinstance(row[0], str):
        return None
    try:
        parsed = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        return _utc_seconds(parsed, "stored observation timestamp")
    except ValueError:
        return None


def _itch_heat(
    run: RadarRun,
    store: RadarStore,
    key: str,
    rows: Sequence[PlatformObservation],
) -> PlatformHeat | None:
    ordered = tuple(sorted(rows, key=lambda item: item.observation_id))
    popular = tuple(row for row in ordered if row.surface == "popular")
    facts = min(
        popular,
        key=lambda row: (
            row.source_rank if row.source_rank is not None else 10**9,
            row.observation_id,
        ),
        default=ordered[0],
    )
    eligible = facts.raw_metrics.get("collector_eligible") is True
    if not eligible:
        return None
    previous = _compatible_previous(store, facts) if popular else None
    first_seen = _earliest_observed_at(store, facts, facts.observed_at)
    age_hours = (
        max(0.0, (run.started_at - first_seen).total_seconds() / 3600)
        if first_seen is not None
        else None
    )
    return score_itch_heat(
        ItchHeatInput(
            run_id=run.run_id,
            platform_key=key,
            observation_ids=tuple(row.observation_id for row in ordered),
            first_seen_age_hours=age_hours,
            popular_rank=facts.source_rank if popular else None,
            previous_popular_rank=(
                previous.source_rank if previous is not None else None
            ),
            rank_history_compatible=previous is not None,
            originality=(
                facts.raw_metrics.get("originality")
                if isinstance(facts.raw_metrics.get("originality"), str)
                else None
            ),
            browser_playable=(
                facts.raw_metrics.get("browser_playable")
                if isinstance(facts.raw_metrics.get("browser_playable"), bool)
                else None
            ),
            author_release_count=_integer(
                facts.raw_metrics.get("author_release_count")
            ),
            author_non_spam=facts.raw_metrics.get("author_non_spam") is True,
            collector_eligible=True,
        )
    )


def _steam_heat(
    run: RadarRun,
    store: RadarStore,
    key: str,
    rows: Sequence[PlatformObservation],
) -> PlatformHeat:
    ordered = tuple(sorted(rows, key=lambda item: item.observation_id))
    first = ordered[0]
    status = first.raw_metrics.get("release_status")
    if status == "unreleased":
        coming = tuple(
            row for row in ordered if row.surface == "coming_soon"
        )
        current = min(
            coming or ordered,
            key=lambda row: (
                row.source_rank if row.source_rank is not None else 10**9,
                row.observation_id,
            ),
        )
        previous = _compatible_previous(store, current)
        release_days = (
            (current.release_at - run.started_at).total_seconds() / 86400
            if current.release_at is not None
            else None
        )
        return score_steam_upcoming_heat(
            SteamUpcomingHeatInput(
                run_id=run.run_id,
                platform_key=key,
                observation_ids=tuple(row.observation_id for row in ordered),
                coming_soon_rank=current.source_rank,
                previous_coming_soon_rank=(
                    previous.source_rank if previous is not None else None
                ),
                rank_history_compatible=previous is not None,
                release_days_away=release_days,
                same_run_discovery_surface_count=len(
                    {row.surface for row in ordered}
                ),
            )
        )

    current = min(
        ordered,
        key=lambda row: (
            row.source_rank if row.source_rank is not None else 10**9,
            row.observation_id,
        ),
    )
    previous = _compatible_previous(store, current)
    current_players = _steam_metric(current, "current_players")
    previous_players = (
        _steam_metric(previous, "current_players")
        if previous is not None
        else None
    )
    release_age = (
        (run.started_at - current.release_at).total_seconds() / 86400
        if current.release_at is not None and current.release_at <= run.started_at
        else None
    )
    return score_steam_released_heat(
        SteamReleasedHeatInput(
            run_id=run.run_id,
            platform_key=key,
            observation_ids=tuple(row.observation_id for row in ordered),
            official_rank=current.source_rank,
            previous_official_rank=(
                previous.source_rank if previous is not None else None
            ),
            rank_history_compatible=previous is not None,
            current_player_growth_percent=_growth_percent(
                current_players,
                previous_players,
            ),
            player_growth_history_compatible=(
                previous is not None
                and current_players is not None
                and previous_players is not None
            ),
            current_players=_integer(current_players),
            release_age_days=release_age,
        )
    )


def _consecutive_compatible_appearances(
    store: RadarStore,
    current: PlatformObservation,
    previous: PlatformObservation | None,
) -> int:
    count = 1
    seen = {current.observation_id}
    cursor = previous
    while cursor is not None and cursor.observation_id not in seen:
        seen.add(cursor.observation_id)
        count += 1
        if count == 3:
            return count
        cursor = _compatible_previous(store, cursor)
    return count


def _roblox_heat(
    run: RadarRun,
    store: RadarStore,
    key: str,
    rows: Sequence[PlatformObservation],
) -> PlatformHeat | None:
    global_rows = tuple(
        sorted(
            (
                row
                for row in rows
                if row.query_parameters.get("cohort_surface") == ROBLOX_GLOBAL
                and row.raw_metrics.get("global_cohort_eligible") is True
            ),
            key=lambda item: item.observation_id,
        )
    )
    if not global_rows:
        return None
    heats: list[PlatformHeat] = []
    for current in global_rows:
        previous = _compatible_previous(store, current)
        current_players = _integer(
            current.raw_metrics.get("concurrent_players")
        )
        previous_players = (
            _integer(previous.raw_metrics.get("concurrent_players"))
            if previous is not None
            else None
        )
        heats.append(
            score_roblox_heat(
                RobloxHeatInput(
                    run_id=run.run_id,
                    platform_key=key,
                    observation_ids=(current.observation_id,),
                    cohort_surface=ROBLOX_GLOBAL,
                    chart_rank=current.source_rank,
                    previous_chart_rank=(
                        previous.source_rank if previous is not None else None
                    ),
                    rank_history_compatible=previous is not None,
                    concurrent_player_growth_percent=_growth_percent(
                        current_players,
                        previous_players,
                    ),
                    player_growth_history_compatible=(
                        previous is not None
                        and current_players is not None
                        and previous_players is not None
                    ),
                    concurrent_players=current_players,
                    consecutive_compatible_appearances=(
                        _consecutive_compatible_appearances(
                            store,
                            current,
                            previous,
                        )
                    ),
                )
            )
        )
    return select_record_heat(heats, compatible_surface=ROBLOX_GLOBAL)


def _platform_heats(
    run: RadarRun,
    store: RadarStore,
    observations: Sequence[PlatformObservation],
) -> tuple[PlatformHeat, ...]:
    grouped: dict[str, list[PlatformObservation]] = {}
    for observation in observations:
        key = canonical_platform_key(observation.platform, observation.platform_id)
        grouped.setdefault(key, []).append(observation)

    heats: list[PlatformHeat] = []
    for key in sorted(grouped):
        rows = grouped[key]
        platform = rows[0].platform
        if platform == "itch":
            heat = _itch_heat(run, store, key, rows)
        elif platform == "steam":
            heat = _steam_heat(run, store, key, rows)
        else:
            heat = _roblox_heat(run, store, key, rows)
        if heat is not None:
            heats.append(heat)
    return tuple(heats)


def _normalized_heats(
    heats: Sequence[PlatformHeat],
    heat_floor: float,
) -> tuple[NormalizedHeat, ...]:
    cohorts: dict[tuple[str, str], list[PlatformHeat]] = {}
    for heat in heats:
        platform = heat.platform_key.partition(":")[0]
        cohorts.setdefault((platform, heat.surface), []).append(heat)
    normalized: list[NormalizedHeat] = []
    for cohort in sorted(cohorts):
        normalized.extend(
            normalize_cohort(cohorts[cohort], heat_floor=heat_floor)
        )
    return tuple(normalized)


def _select_candidates(
    identities: Sequence[GameIdentity],
    normalized: Sequence[NormalizedHeat],
    limit: int,
) -> tuple[GameIdentity, ...]:
    identity_by_key = {
        platform_key(record): identity
        for identity in identities
        for record in identity.platform_records
    }
    best: dict[str, tuple[float, float, GameIdentity]] = {}
    for heat in normalized:
        identity = identity_by_key.get(heat.platform_key)
        if identity is None:
            continue
        ranking = (heat.platform_score, heat.heat, identity)
        prior = best.get(identity.opportunity_id)
        if prior is None or ranking[:2] > prior[:2]:
            best[identity.opportunity_id] = ranking
    ordered = sorted(
        best.values(),
        key=lambda item: (
            -item[0],
            -item[1],
            item[2].normalized_name,
            item[2].opportunity_id,
        ),
    )
    return tuple(item[2] for item in ordered[:limit])


def _candidate_platform_scores(
    identities: Sequence[GameIdentity],
    normalized: Sequence[NormalizedHeat],
) -> dict[str, float]:
    identity_by_key = {
        platform_key(record): identity
        for identity in identities
        for record in identity.platform_records
    }
    best: dict[str, tuple[float, float]] = {}
    for heat in normalized:
        identity = identity_by_key.get(heat.platform_key)
        if identity is None:
            continue
        ranking = (heat.platform_score, heat.heat)
        prior = best.get(identity.opportunity_id)
        if prior is None or ranking > prior:
            best[identity.opportunity_id] = ranking
    return {
        opportunity_id: ranking[0]
        for opportunity_id, ranking in best.items()
    }


def _rebuild_scoring_context(
    config: RadarConfig,
    store: RadarStore,
    run: RadarRun,
    id_factory: Callable[[], str],
) -> tuple[tuple[GameIdentity, ...], dict[str, float]]:
    observations = _verified_run_observations(store, run)
    records = _platform_records(observations)
    identities = _link_identities(config, store, records, id_factory)
    heats = _platform_heats(run, store, observations)
    normalized = _normalized_heats(heats, config.heat_floor)
    candidates = _select_candidates(
        identities,
        normalized,
        config.preliminary_top_n,
    )
    return candidates, _candidate_platform_scores(identities, normalized)


def _rebuild_candidates(
    config: RadarConfig,
    store: RadarStore,
    run: RadarRun,
    id_factory: Callable[[], str],
) -> tuple[GameIdentity, ...]:
    candidates, _ = _rebuild_scoring_context(
        config,
        store,
        run,
        id_factory,
    )
    return candidates


def _run_is_finalized(store: RadarStore, run_id: str) -> bool:
    row = store._fetchone(  # type: ignore[attr-defined]
        """
        SELECT 1 FROM evidence WHERE run_id = ?
        UNION ALL
        SELECT 1 FROM scores WHERE run_id = ?
        LIMIT 1
        """,
        (run_id, run_id),
    )
    return row is not None


def _ingest_target(
    store: RadarStore,
    run_id: object,
    envelope: object,
) -> tuple[RadarRun, str]:
    if not isinstance(run_id, str):
        raise InputValidationError("run_id must be text")
    run = store.get_run(run_id)
    if run is None:
        raise InputValidationError("ingest run was not found")
    if _run_is_finalized(store, run.run_id):
        raise InputValidationError("ingest run is finalized")
    if not isinstance(envelope, Mapping):
        raise InputValidationError("browser envelope must be a mapping")
    if envelope.get("run_id") != run.run_id:
        raise InputValidationError("browser envelope run_id must match ingest run_id")
    collector = validate_platform(envelope.get("collector"), "collector")
    if collector not in _BROWSER_SURFACES:
        raise InputValidationError("collector must be itch or roblox")
    if collector not in run.platforms:
        raise InputValidationError("originating run did not select collector")
    return run, collector


def _parse_browser_observations(
    run: RadarRun,
    collector: str,
    envelope: Mapping[str, object],
    parser_registry: Mapping[
        str,
        Callable[[object, RadarRun], object],
    ],
    now: datetime,
) -> _ParsedBrowserIngest:
    parser = parser_registry.get(collector)
    if not callable(parser):
        raise InputValidationError(
            f"browser parser is not configured for {collector}"
        )
    parsed = parser(envelope, run)
    if collector == "itch":
        if not isinstance(parsed, ItchBrowserEnvelope):
            raise InputValidationError(
                "itch parser must return an ItchBrowserEnvelope"
            )
        envelope_observed_at = parsed.observed_at
        observations = build_itch_observations(run, parsed)
    else:
        if not isinstance(parsed, RobloxBrowserEnvelope):
            raise InputValidationError(
                "roblox parser must return a RobloxBrowserEnvelope"
            )
        envelope_observed_at = parsed.observed_at
        observations = build_roblox_observations(run, parsed)
    if envelope_observed_at > now:
        raise InputValidationError("browser envelope must not be in the future")
    if any(
        observation.run_id != run.run_id
        or observation.platform != collector
        for observation in observations
    ):
        raise InputValidationError(
            "parsed browser observations do not match the ingest target"
        )
    return _ParsedBrowserIngest(
        observed_at=envelope_observed_at,
        surfaces=tuple(
            sorted({observation.surface for observation in observations})
        ),
        observations=observations,
    )


def _validate_ingest_now(run: RadarRun, now: datetime) -> None:
    if now < run.started_at:
        raise InputValidationError("ingest clock must not precede run started_at")


def _validate_ingest_observation_times(
    observations: Sequence[PlatformObservation],
    now: datetime,
) -> None:
    if any(observation.observed_at > now for observation in observations):
        raise InputValidationError(
            "browser observations must not be in the future"
        )


def _browser_capabilities(
    collector: str,
    observations: Sequence[PlatformObservation],
) -> dict[str, bool]:
    global_surfaces = {
        observation.surface
        for observation in observations
        if observation.query_parameters.get("surface_scope") == "global"
    }
    return {
        surface: surface in global_surfaces
        for surface in _BROWSER_SURFACES[collector]
    }


def _active_ingest_observations(
    store: RadarStore,
    run: RadarRun,
    collector: str,
    observations: Sequence[PlatformObservation],
) -> tuple[PlatformObservation, ...]:
    by_id = {
        observation.observation_id: observation
        for observation in _run_observations(store, run.run_id)
        if observation.platform == collector
    }
    for observation in observations:
        by_id[observation.observation_id] = observation
    return tuple(by_id[key] for key in sorted(by_id))


def _ingest_health(
    config: RadarConfig,
    store: RadarStore,
    run: RadarRun,
    collector: str,
    observations: Sequence[PlatformObservation],
    now: datetime,
) -> SourceHealth:
    return classify_source_health(
        run_id=run.run_id,
        now=now,
        attempted=True,
        active_observations=observations,
        capabilities=_browser_capabilities(collector, observations),
        fallback_observed_at=_latest_observed_at(
            store,
            collector,
            run.started_at,
        ),
        warnings=(),
        fresh_hours=config.fresh_hours,
        stale_fallback_hours=config.stale_fallback_hours,
        collector=collector,
    )


def _persist_ingest(
    config: RadarConfig,
    store: RadarStore,
    run: RadarRun,
    collector: str,
    envelope: Mapping[str, object],
    parsed: _ParsedBrowserIngest,
    health: SourceHealth,
) -> None:
    surface_key = "_".join(parsed.surfaces) or "empty"
    artifact_name = (
        f"{collector}_{surface_key}_"
        f"{parsed.observed_at.strftime('%Y%m%dt%H%M%Sz')}.json"
    )
    persist_raw_artifact(
        config,
        run.run_id,
        f"{collector}_agent_browser",
        artifact_name,
        envelope,
        parsed.observed_at,
    )
    with store.transaction():
        for observation in parsed.observations:
            store.insert_observation(observation)
        store.save_source_health(health)


def _result_context(
    store: RadarStore,
    run: RadarRun,
) -> tuple[
    tuple[SourceHealth, ...],
    tuple[WarningRecord, ...],
    tuple[OutstandingTask, ...],
]:
    health_rows = tuple(
        health
        for platform in run.platforms
        if (health := store.get_source_health(run.run_id, platform)) is not None
    )
    warnings = tuple(
        warning
        for health in health_rows
        for warning in health.warnings
    )
    health_by_collector = {
        health.collector: health for health in health_rows
    }
    outstanding = tuple(
        _outstanding_browser_task(run, platform)
        for platform in run.platforms
        if platform in _BROWSER_SURFACES
        and (
            platform not in health_by_collector
            or health_by_collector[platform].status != "fresh"
        )
    )
    return health_rows, warnings, outstanding


def _existing_identity_only() -> str:
    raise InputValidationError(
        "persisted observations must already be linked to an identity"
    )


def _persisted_preliminary_result(
    config: RadarConfig,
    store: RadarStore,
    run: RadarRun,
) -> tuple[PreliminaryResult, dict[str, float]]:
    candidates, platform_scores = _rebuild_scoring_context(
        config,
        store,
        run,
        _existing_identity_only,
    )
    health, warnings, outstanding = _result_context(store, run)
    return (
        PreliminaryResult(
            schema_version=1,
            run_id=run.run_id,
            candidates=candidates,
            source_health=health,
            warnings=warnings,
            outstanding_tasks=outstanding,
        ),
        platform_scores,
    )


def _load_run_evidence(
    store: RadarStore,
    run_id: str,
) -> dict[str, OpportunityEvidence]:
    rows = store._fetchall(  # type: ignore[attr-defined]
        """
        SELECT canonical_json FROM evidence
        WHERE run_id = ?
        ORDER BY opportunity_id
        """,
        (run_id,),
    )
    evidence: dict[str, OpportunityEvidence] = {}
    for row in rows:
        if len(row) != 1 or not isinstance(row[0], str):
            raise InputValidationError("stored evidence row is invalid")
        item = OpportunityEvidence.from_dict(json.loads(row[0]))
        if item.run_id != run_id or item.opportunity_id in evidence:
            raise InputValidationError("stored evidence provenance is invalid")
        evidence[item.opportunity_id] = item
    return evidence


def _load_run_scores(
    store: RadarStore,
    run_id: str,
) -> dict[str, ScoredOpportunity]:
    rows = store._fetchall(  # type: ignore[attr-defined]
        """
        SELECT canonical_json FROM scores
        WHERE run_id = ?
        ORDER BY opportunity_id
        """,
        (run_id,),
    )
    scores: dict[str, ScoredOpportunity] = {}
    for row in rows:
        if len(row) != 1 or not isinstance(row[0], str):
            raise InputValidationError("stored score row is invalid")
        item = ScoredOpportunity.from_dict(json.loads(row[0]))
        if item.run_id != run_id or item.opportunity_id in scores:
            raise InputValidationError("stored score provenance is invalid")
        scores[item.opportunity_id] = item
    return scores


def _required_run(store: RadarStore, run_id: object) -> RadarRun:
    if not isinstance(run_id, str):
        raise InputValidationError("run_id must be text")
    run = store.get_run(run_id)
    if run is None:
        raise InputValidationError("radar run was not found")
    return run


def _validate_evidence_batch(
    run: RadarRun,
    candidates: Sequence[GameIdentity],
    evidence_batch: Sequence[OpportunityEvidence],
) -> tuple[OpportunityEvidence, ...]:
    if isinstance(evidence_batch, (str, bytes)) or not isinstance(
        evidence_batch,
        Sequence,
    ):
        raise InputValidationError(
            "evidence_batch must be a sequence of OpportunityEvidence"
        )
    evidence_by_id: dict[str, OpportunityEvidence] = {}
    for index, evidence in enumerate(evidence_batch):
        if not isinstance(evidence, OpportunityEvidence):
            raise InputValidationError(
                f"evidence_batch[{index}] must be OpportunityEvidence"
            )
        if evidence.run_id != run.run_id:
            raise InputValidationError("evidence run_id must match run_id")
        if evidence.opportunity_id in evidence_by_id:
            raise InputValidationError(
                "evidence batch must not contain duplicate opportunity IDs"
            )
        evidence_by_id[evidence.opportunity_id] = evidence
    expected_ids = tuple(candidate.opportunity_id for candidate in candidates)
    if set(evidence_by_id) != set(expected_ids):
        raise InputValidationError(
            "evidence batch must exactly cover the enrichment candidates"
        )
    return tuple(evidence_by_id[opportunity_id] for opportunity_id in expected_ids)


def _final_result(
    result: PreliminaryResult,
    scores: Mapping[str, ScoredOpportunity],
    limit: int,
) -> tuple[PreliminaryResult, tuple[ScoredOpportunity, ...]]:
    ordered = sorted(
        result.candidates,
        key=lambda candidate: opportunity_sort_key(
            scores[candidate.opportunity_id],
            candidate.normalized_name,
        ),
    )[:limit]
    final_scores = tuple(scores[candidate.opportunity_id] for candidate in ordered)
    return (
        PreliminaryResult(
            schema_version=1,
            run_id=result.run_id,
            candidates=tuple(ordered),
            source_health=result.source_health,
            warnings=result.warnings,
            outstanding_tasks=result.outstanding_tasks,
        ),
        final_scores,
    )


def scan_run(
    config: RadarConfig,
    store: RadarStore,
    collectors: Mapping[str, Collector],
    clock: Callable[[], datetime],
    id_factory: Callable[[], str],
    platforms: Sequence[str],
    *,
    started_at: datetime | None = None,
    run_id: str | None = None,
    mode: str = "manual",
    publish_daily: bool = False,
) -> PreliminaryResult:
    """Run selected collectors and return one cross-platform candidate list."""

    if not isinstance(config, RadarConfig):
        raise TypeError("config must be RadarConfig")
    if not isinstance(store, RadarStore):
        raise TypeError("store must be RadarStore")
    if not isinstance(collectors, Mapping):
        raise TypeError("collectors must be a mapping")
    if not callable(clock) or not callable(id_factory):
        raise TypeError("clock and id_factory must be callable")

    selected = _selected_platforms(config, platforms)
    if (started_at is None) != (run_id is None):
        raise ValueError("started_at and run_id must be provided together")
    if started_at is None:
        scan_started_at, scan_run_id = new_run_id(clock, id_factory)
    else:
        scan_started_at = _utc_seconds(started_at, "started_at")
        if not isinstance(run_id, str):
            raise ValueError("run_id must be text")
        scan_run_id = run_id
        if not scan_run_id.startswith(
            f"{scan_started_at.strftime('%Y%m%dT%H%M%SZ')}-"
        ):
            raise ValueError("run_id timestamp must match started_at")
    run = RadarRun(
        schema_version=1,
        run_id=scan_run_id,
        started_at=scan_started_at,
        mode=mode,
        platforms=selected,
        publish_daily=publish_daily,
    )
    store.create_run(run)
    results, health, warnings, outstanding = _collect_selected(
        config,
        store,
        run,
        collectors,
    )
    candidates = _rebuild_candidates(
        config,
        store,
        run,
        id_factory,
    )
    return PreliminaryResult(
        schema_version=1,
        run_id=run.run_id,
        candidates=candidates,
        source_health=health,
        warnings=warnings,
        outstanding_tasks=outstanding,
    )


def ingest_run(
    config: RadarConfig,
    store: RadarStore,
    run_id: str,
    envelope: Mapping[str, object],
    parser_registry: Mapping[
        str,
        Callable[[object, RadarRun], object],
    ],
    clock: Callable[[], datetime],
    id_factory: Callable[[], str],
) -> PreliminaryResult:
    """Persist one strict browser envelope and deterministically rebuild."""

    if not isinstance(config, RadarConfig):
        raise TypeError("config must be RadarConfig")
    if not isinstance(store, RadarStore):
        raise TypeError("store must be RadarStore")
    if not isinstance(parser_registry, Mapping):
        raise TypeError("parser_registry must be a mapping")
    if not callable(clock) or not callable(id_factory):
        raise TypeError("clock and id_factory must be callable")

    run, collector = _ingest_target(store, run_id, envelope)
    now = _utc_seconds(clock(), "clock")
    _validate_ingest_now(run, now)
    parsed = _parse_browser_observations(
        run,
        collector,
        envelope,
        parser_registry,
        now,
    )
    observations = parsed.observations
    _validate_ingest_observation_times(observations, now)
    active_observations = _active_ingest_observations(
        store,
        run,
        collector,
        observations,
    )
    health = _ingest_health(
        config,
        store,
        run,
        collector,
        active_observations,
        now,
    )
    _persist_ingest(
        config,
        store,
        run,
        collector,
        envelope,
        parsed,
        health,
    )
    candidates = _rebuild_candidates(config, store, run, id_factory)
    health_rows, warnings, outstanding = _result_context(store, run)
    return PreliminaryResult(
        schema_version=1,
        run_id=run.run_id,
        candidates=candidates,
        source_health=health_rows,
        warnings=warnings,
        outstanding_tasks=outstanding,
    )


def report_run(
    config: RadarConfig,
    store: RadarStore,
    run_id: str,
    clock: Callable[[], datetime],
    *,
    writer: AtomicWriter | None = None,
) -> CommandManifest:
    """Rebuild one run from SQLite and persist its canonical report."""

    if not isinstance(config, RadarConfig):
        raise TypeError("config must be RadarConfig")
    if not isinstance(store, RadarStore):
        raise TypeError("store must be RadarStore")
    if not callable(clock):
        raise TypeError("clock must be callable")
    run = _required_run(store, run_id)
    now = _utc_seconds(clock(), "clock")
    if now < run.started_at:
        raise InputValidationError("report clock must not precede run started_at")
    result, _ = _persisted_preliminary_result(config, store, run)
    enrichment_candidates = result.candidates[: config.enrichment_top_n]
    expected_ids = {
        candidate.opportunity_id for candidate in enrichment_candidates
    }
    evidence = _load_run_evidence(store, run.run_id)
    scores = _load_run_scores(store, run.run_id)
    if not evidence and not scores:
        report_result = result
        report_scores: tuple[ScoredOpportunity, ...] = ()
        phase = "preliminary"
    else:
        if set(evidence) != expected_ids or set(scores) != expected_ids:
            raise InputValidationError(
                "final run evidence and scores must exactly cover enrichment candidates"
            )
        enrichment_result = PreliminaryResult(
            schema_version=1,
            run_id=result.run_id,
            candidates=enrichment_candidates,
            source_health=result.source_health,
            warnings=result.warnings,
            outstanding_tasks=result.outstanding_tasks,
        )
        report_result, report_scores = _final_result(
            enrichment_result,
            scores,
            config.final_top_n,
        )
        phase = "final"
    report = build_report(report_result, report_scores, phase)
    active_writer = FileAtomicWriter() if writer is None else writer
    paths = persist_run_artifacts(
        report,
        Path(config.report_dir),
        active_writer,
    )
    if phase == "final":
        publish_daily_if_allowed(
            config,
            store,
            run,
            report,
            paths,
            now,
            active_writer,
        )
    return CommandManifest(
        schema_version=1,
        run_id=run.run_id,
        phase=phase,
        report_json=str(paths[0]),
        report_markdown=str(paths[1]),
        source_health=report_result.source_health,
        warnings=report_result.warnings,
        outstanding_tasks=report_result.outstanding_tasks,
    )


def enrich_run(
    config: RadarConfig,
    store: RadarStore,
    run_id: str,
    evidence_batch: Sequence[OpportunityEvidence],
    clock: Callable[[], datetime],
    *,
    writer: AtomicWriter | None = None,
) -> CommandManifest:
    """Atomically persist one complete enrichment batch and final scores."""

    if not isinstance(config, RadarConfig):
        raise TypeError("config must be RadarConfig")
    if not isinstance(store, RadarStore):
        raise TypeError("store must be RadarStore")
    if not callable(clock):
        raise TypeError("clock must be callable")
    run = _required_run(store, run_id)
    now = _utc_seconds(clock(), "clock")
    if now < run.started_at:
        raise InputValidationError("enrichment clock must not precede run started_at")
    result, platform_scores = _persisted_preliminary_result(config, store, run)
    candidates = result.candidates[: config.enrichment_top_n]
    evidence = _validate_evidence_batch(run, candidates, evidence_batch)
    expected_ids = {candidate.opportunity_id for candidate in candidates}
    existing_evidence = _load_run_evidence(store, run.run_id)
    existing_scores = _load_run_scores(store, run.run_id)
    if existing_evidence or existing_scores:
        if (
            set(existing_evidence) != expected_ids
            or set(existing_scores) != expected_ids
        ):
            raise InputValidationError(
                "existing final enrichment batch is incomplete"
            )
        with store.transaction():
            for item in evidence:
                store.insert_evidence(item)
        return report_run(
            config,
            store,
            run.run_id,
            lambda: now,
            writer=writer,
        )

    evidence_by_id = {item.opportunity_id: item for item in evidence}
    scores = tuple(
        score_opportunity(
            run_id=run.run_id,
            opportunity_id=candidate.opportunity_id,
            game_name=candidate.name,
            platform_score=platform_scores[candidate.opportunity_id],
            evidence=evidence_by_id[candidate.opportunity_id],
            publication_time=now,
        )
        for candidate in candidates
    )
    with store.transaction():
        for item in evidence:
            store.insert_evidence(item)
        for score in scores:
            store.save_score(score)
    return report_run(
        config,
        store,
        run.run_id,
        lambda: now,
        writer=writer,
    )


__all__ = [
    "enrich_run",
    "ingest_run",
    "new_run_id",
    "report_run",
    "scan_run",
]
