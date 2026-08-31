"""Deterministic preliminary collection, persistence, and candidate rebuild."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
import json
import re
from uuid import UUID

from .collectors.base import (
    Collector,
    CollectorResult,
    classify_source_health,
)
from .config import RadarConfig
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
from .schemas import (
    GameIdentity,
    NormalizedHeat,
    OutstandingTask,
    PlatformHeat,
    PlatformObservation,
    PlatformRecord,
    PreliminaryResult,
    RadarRun,
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
    store: RadarStore,
    result: CollectorResult,
) -> None:
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

        _persist_collector_result(store, result)
        results.append(result)
        health_rows.append(result.health)
        warnings.extend(result.health.warnings)
        if (
            platform in _BROWSER_SURFACES
            and result.health.status not in {"fresh", "partial"}
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


def _itch_heat(
    run: RadarRun,
    key: str,
    rows: Sequence[PlatformObservation],
) -> PlatformHeat | None:
    ordered = tuple(sorted(rows, key=lambda item: item.observation_id))
    facts = next(
        (
            row
            for row in ordered
            if row.surface == "popular"
        ),
        ordered[0],
    )
    eligible = facts.raw_metrics.get("collector_eligible") is True
    if not eligible:
        return None
    popular_ranks = tuple(
        row.source_rank
        for row in ordered
        if row.surface == "popular" and row.source_rank is not None
    )
    first_seen = min(row.observed_at for row in ordered)
    age_hours = max(0.0, (run.started_at - first_seen).total_seconds() / 3600)
    return score_itch_heat(
        ItchHeatInput(
            run_id=run.run_id,
            platform_key=key,
            observation_ids=tuple(row.observation_id for row in ordered),
            first_seen_age_hours=age_hours,
            popular_rank=min(popular_ranks) if popular_ranks else None,
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
                        2 if previous is not None else 1
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
            heat = _itch_heat(run, key, rows)
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


def _rebuild_candidates(
    config: RadarConfig,
    store: RadarStore,
    run: RadarRun,
    results: Sequence[CollectorResult],
    id_factory: Callable[[], str],
) -> tuple[GameIdentity, ...]:
    observations = tuple(
        observation
        for result in results
        for observation in result.observations
    )
    # Pending payloads deliberately remain attached to `results` until this
    # rebuild completes. Task 12A owns their redaction and artifact writes.
    records = _platform_records(observations)
    identities = _link_identities(config, store, records, id_factory)
    heats = _platform_heats(run, store, observations)
    normalized = _normalized_heats(heats, config.heat_floor)
    return _select_candidates(
        identities,
        normalized,
        config.preliminary_top_n,
    )


def scan_run(
    config: RadarConfig,
    store: RadarStore,
    collectors: Mapping[str, Collector],
    clock: Callable[[], datetime],
    id_factory: Callable[[], str],
    platforms: Sequence[str],
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
    started_at = _utc_seconds(clock(), "clock")
    suffix = _run_suffix(id_factory())
    run = RadarRun(
        schema_version=1,
        run_id=f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-{suffix}",
        started_at=started_at,
        mode="manual",
        platforms=selected,
        publish_daily=False,
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
        results,
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


__all__ = ["scan_run"]
