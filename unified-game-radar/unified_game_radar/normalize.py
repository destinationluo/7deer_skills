"""Pure platform heat transforms and compatible-cohort normalization."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import math
from typing import Sequence

from .platform_keys import parse_platform_key, validate_platform
from .schemas import NormalizedHeat, PlatformHeat


HEAT_FLOOR = 30.0

ITCH_DISCOVERY = "itch_discovery"
STEAM_RELEASED = "steam_released"
STEAM_UPCOMING = "steam_upcoming"
ROBLOX_GLOBAL = "roblox_global"
ROBLOX_PERSONALIZED = "roblox_personalized"


@dataclass(frozen=True)
class ItchHeatInput:
    """Verified itch facts for one record across compatible discovery rows."""

    run_id: str
    platform_key: str
    observation_ids: tuple[str, ...]
    first_seen_age_hours: float | None = None
    popular_rank: int | None = None
    previous_popular_rank: int | None = None
    rank_history_compatible: bool = False
    originality: str | None = None
    browser_playable: bool | None = None
    author_release_count: int | None = None
    author_non_spam: bool = False
    collector_eligible: bool = True


@dataclass(frozen=True)
class SteamReleasedHeatInput:
    """Verified Steam released-game facts and compatible historical deltas."""

    run_id: str
    platform_key: str
    observation_ids: tuple[str, ...]
    official_rank: int | None = None
    previous_official_rank: int | None = None
    rank_history_compatible: bool = False
    current_player_growth_percent: float | None = None
    player_growth_history_compatible: bool = False
    current_players: int | None = None
    release_age_days: float | None = None


@dataclass(frozen=True)
class SteamUpcomingHeatInput:
    """Verified Steam upcoming-game facts, including allowed imported growth."""

    run_id: str
    platform_key: str
    observation_ids: tuple[str, ...]
    coming_soon_rank: int | None = None
    previous_coming_soon_rank: int | None = None
    rank_history_compatible: bool = False
    follower_or_wishlist_growth_percent: float | None = None
    growth_verified: bool = False
    growth_history_compatible: bool = False
    release_days_away: float | None = None
    same_run_discovery_surface_count: int | None = None


@dataclass(frozen=True)
class RobloxHeatInput:
    """Verified Roblox chart facts for an explicit global/personalized cohort."""

    run_id: str
    platform_key: str
    observation_ids: tuple[str, ...]
    cohort_surface: str = ROBLOX_GLOBAL
    chart_rank: int | None = None
    previous_chart_rank: int | None = None
    rank_history_compatible: bool = False
    concurrent_player_growth_percent: float | None = None
    player_growth_history_compatible: bool = False
    concurrent_players: int | None = None
    consecutive_compatible_appearances: int | None = None


def _round_one_decimal(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed


def _optional_number(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _finite_number(value, name)


def _optional_nonnegative_number(value: object, name: str) -> float | None:
    parsed = _optional_number(value, name)
    if parsed is not None and parsed < 0:
        raise ValueError(f"{name} must be nonnegative")
    return parsed


def _optional_integer(
    value: object,
    name: str,
    *,
    minimum: int = 0,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _platform_heat(
    *,
    expected_platform: str,
    run_id: str,
    platform_key: str,
    surface: str,
    observation_ids: tuple[str, ...],
    heat: float,
) -> PlatformHeat:
    platform, _ = parse_platform_key(platform_key)
    if platform != expected_platform:
        raise ValueError(
            f"{platform_key} cannot be scored by the {expected_platform} formula"
        )
    return PlatformHeat(
        schema_version=1,
        run_id=run_id,
        platform_key=platform_key,
        surface=surface,
        observation_ids=observation_ids,
        heat=_round_one_decimal(heat),
    )


def _rank_points(
    rank: int | None,
    thresholds: Sequence[tuple[int, int]],
    name: str,
) -> int:
    parsed = _optional_integer(rank, name, minimum=1)
    if parsed is None:
        return 0
    for upper_bound, points in thresholds:
        if parsed <= upper_bound:
            return points
    return 0


def _rank_improvement_points(
    current_rank: int | None,
    previous_rank: int | None,
    compatible: bool,
    thresholds: Sequence[tuple[int, int]],
    current_name: str,
    previous_name: str,
) -> int:
    current = _optional_integer(current_rank, current_name, minimum=1)
    previous = _optional_integer(previous_rank, previous_name, minimum=1)
    if not _boolean(compatible, "rank_history_compatible"):
        return 0
    if current is None or previous is None:
        return 0
    improvement = previous - current
    for lower_bound, points in thresholds:
        if improvement >= lower_bound:
            return points
    return 0


def _growth_points(
    growth_percent: float | None,
    compatible: bool,
    thresholds: Sequence[tuple[float, int]],
    name: str,
) -> int:
    growth = _optional_number(growth_percent, name)
    if not _boolean(compatible, "growth_history_compatible") or growth is None:
        return 0
    for lower_bound, points in thresholds:
        if (lower_bound == 0 and growth > 0) or (
            lower_bound != 0 and growth >= lower_bound
        ):
            return points
    return 0


def score_itch_heat(facts: ItchHeatInput) -> PlatformHeat | None:
    """Score verified itch facts; filtered/reuploaded records return ``None``."""

    if not isinstance(facts, ItchHeatInput):
        raise TypeError("facts must be ItchHeatInput")
    # Validate provenance even when the row is excluded.
    context = _platform_heat(
        expected_platform="itch",
        run_id=facts.run_id,
        platform_key=facts.platform_key,
        surface=ITCH_DISCOVERY,
        observation_ids=facts.observation_ids,
        heat=0,
    )
    eligible = _boolean(facts.collector_eligible, "collector_eligible")
    if facts.originality not in {
        None,
        "verified_original",
        "unknown",
        "known_reupload",
    }:
        raise ValueError("originality is invalid")
    if not eligible or facts.originality == "known_reupload":
        return None

    age = _optional_nonnegative_number(
        facts.first_seen_age_hours, "first_seen_age_hours"
    )
    age_points = 0
    if age is not None:
        if age <= 24:
            age_points = 25
        elif age <= 72:
            age_points = 15
        elif age <= 168:
            age_points = 5

    rank_points = _rank_points(
        facts.popular_rank,
        ((10, 35), (25, 25), (50, 15)),
        "popular_rank",
    )
    improvement_points = _rank_improvement_points(
        facts.popular_rank,
        facts.previous_popular_rank,
        facts.rank_history_compatible,
        ((20, 20), (5, 10), (1, 5)),
        "popular_rank",
        "previous_popular_rank",
    )
    originality_points = {
        None: 0,
        "verified_original": 10,
        "unknown": 5,
    }[facts.originality]
    if facts.browser_playable is not None and not isinstance(
        facts.browser_playable, bool
    ):
        raise ValueError("browser_playable must be a boolean or None")
    browser_points = 5 if facts.browser_playable is True else 0
    author_count = _optional_integer(
        facts.author_release_count,
        "author_release_count",
        minimum=0,
    )
    author_non_spam = _boolean(facts.author_non_spam, "author_non_spam")
    author_points = (
        5 if author_non_spam and author_count is not None and author_count >= 2 else 0
    )
    return PlatformHeat(
        schema_version=context.schema_version,
        run_id=context.run_id,
        platform_key=context.platform_key,
        surface=context.surface,
        observation_ids=context.observation_ids,
        heat=_round_one_decimal(
            age_points
            + rank_points
            + improvement_points
            + originality_points
            + browser_points
            + author_points
        ),
    )


def score_steam_released_heat(facts: SteamReleasedHeatInput) -> PlatformHeat:
    """Score a released Steam record without reweighting missing components."""

    if not isinstance(facts, SteamReleasedHeatInput):
        raise TypeError("facts must be SteamReleasedHeatInput")
    official_rank_points = _rank_points(
        facts.official_rank,
        ((10, 25), (25, 20), (50, 15), (100, 8)),
        "official_rank",
    )
    improvement_points = _rank_improvement_points(
        facts.official_rank,
        facts.previous_official_rank,
        facts.rank_history_compatible,
        ((20, 25), (10, 18), (5, 10), (1, 5)),
        "official_rank",
        "previous_official_rank",
    )
    growth_points = _growth_points(
        facts.current_player_growth_percent,
        facts.player_growth_history_compatible,
        ((100, 25), (50, 20), (20, 12), (0, 5)),
        "current_player_growth_percent",
    )
    players = _optional_integer(facts.current_players, "current_players")
    scale_points = 0
    if players is not None:
        if players >= 10_000:
            scale_points = 15
        elif players >= 1_000:
            scale_points = 10
        elif players >= 100:
            scale_points = 5
    release_age = _optional_nonnegative_number(
        facts.release_age_days, "release_age_days"
    )
    release_points = 0
    if release_age is not None:
        if release_age <= 7:
            release_points = 10
        elif release_age <= 30:
            release_points = 5
    return _platform_heat(
        expected_platform="steam",
        run_id=facts.run_id,
        platform_key=facts.platform_key,
        surface=STEAM_RELEASED,
        observation_ids=facts.observation_ids,
        heat=official_rank_points
        + improvement_points
        + growth_points
        + scale_points
        + release_points,
    )


def score_steam_upcoming_heat(facts: SteamUpcomingHeatInput) -> PlatformHeat:
    """Score an upcoming Steam record, accepting only verified growth."""

    if not isinstance(facts, SteamUpcomingHeatInput):
        raise TypeError("facts must be SteamUpcomingHeatInput")
    coming_soon_points = _rank_points(
        facts.coming_soon_rank,
        ((10, 30), (25, 24), (50, 15)),
        "coming_soon_rank",
    )
    improvement_points = _rank_improvement_points(
        facts.coming_soon_rank,
        facts.previous_coming_soon_rank,
        facts.rank_history_compatible,
        ((20, 30), (10, 20), (1, 10)),
        "coming_soon_rank",
        "previous_coming_soon_rank",
    )
    growth_verified = _boolean(facts.growth_verified, "growth_verified")
    growth_points = 0
    if growth_verified:
        growth_points = _growth_points(
            facts.follower_or_wishlist_growth_percent,
            facts.growth_history_compatible,
            ((50, 20), (20, 15), (0, 8)),
            "follower_or_wishlist_growth_percent",
        )
    else:
        # Still validate supplied primitive values without treating them as evidence.
        _optional_number(
            facts.follower_or_wishlist_growth_percent,
            "follower_or_wishlist_growth_percent",
        )
        _boolean(facts.growth_history_compatible, "growth_history_compatible")

    release_days = _optional_number(facts.release_days_away, "release_days_away")
    proximity_points = 0
    if release_days is not None:
        if 0 <= release_days <= 7:
            proximity_points = 10
        elif 7 < release_days <= 30:
            proximity_points = 7
        elif release_days > 30:
            proximity_points = 3
    surfaces = _optional_integer(
        facts.same_run_discovery_surface_count,
        "same_run_discovery_surface_count",
    )
    surface_points = 10 if surfaces is not None and surfaces >= 2 else 0
    return _platform_heat(
        expected_platform="steam",
        run_id=facts.run_id,
        platform_key=facts.platform_key,
        surface=STEAM_UPCOMING,
        observation_ids=facts.observation_ids,
        heat=coming_soon_points
        + improvement_points
        + growth_points
        + proximity_points
        + surface_points,
    )


def score_roblox_heat(facts: RobloxHeatInput) -> PlatformHeat:
    """Score a Roblox chart record within an explicit scope cohort."""

    if not isinstance(facts, RobloxHeatInput):
        raise TypeError("facts must be RobloxHeatInput")
    if facts.cohort_surface not in {ROBLOX_GLOBAL, ROBLOX_PERSONALIZED}:
        raise ValueError("cohort_surface must be roblox_global or roblox_personalized")
    rank_points = _rank_points(
        facts.chart_rank,
        ((10, 30), (25, 24), (50, 15)),
        "chart_rank",
    )
    improvement_points = _rank_improvement_points(
        facts.chart_rank,
        facts.previous_chart_rank,
        facts.rank_history_compatible,
        ((20, 30), (10, 20), (1, 10)),
        "chart_rank",
        "previous_chart_rank",
    )
    growth_points = _growth_points(
        facts.concurrent_player_growth_percent,
        facts.player_growth_history_compatible,
        ((100, 25), (50, 20), (20, 12), (0, 5)),
        "concurrent_player_growth_percent",
    )
    players = _optional_integer(facts.concurrent_players, "concurrent_players")
    scale_points = 0
    if players is not None:
        if players >= 10_000:
            scale_points = 10
        elif players >= 1_000:
            scale_points = 7
        elif players >= 100:
            scale_points = 3
    appearances = _optional_integer(
        facts.consecutive_compatible_appearances,
        "consecutive_compatible_appearances",
    )
    appearance_points = 0
    if appearances is not None:
        if appearances >= 3:
            appearance_points = 5
        elif appearances == 2:
            appearance_points = 3
    return _platform_heat(
        expected_platform="roblox",
        run_id=facts.run_id,
        platform_key=facts.platform_key,
        surface=facts.cohort_surface,
        observation_ids=facts.observation_ids,
        heat=rank_points
        + improvement_points
        + growth_points
        + scale_points
        + appearance_points,
    )


def select_record_heat(
    heats: Sequence[PlatformHeat],
    *,
    compatible_surface: str,
) -> PlatformHeat:
    """Select one record's maximum compatible heat and retain all evidence IDs."""

    values = tuple(heats)
    if not values:
        raise ValueError("heats must not be empty")
    if not isinstance(compatible_surface, str) or not compatible_surface:
        raise ValueError("compatible_surface must be nonempty text")
    first = values[0]
    if not all(isinstance(item, PlatformHeat) for item in values):
        raise TypeError("heats must contain PlatformHeat records")
    if any(item.run_id != first.run_id for item in values):
        raise ValueError("record heats must belong to one run")
    if any(item.platform_key != first.platform_key for item in values):
        raise ValueError("record heats must belong to one platform key")
    if any(item.surface != compatible_surface for item in values):
        raise ValueError("record heats must use the declared compatible surface")
    observation_ids = tuple(
        sorted({identifier for item in values for identifier in item.observation_ids})
    )
    return PlatformHeat(
        schema_version=1,
        run_id=first.run_id,
        platform_key=first.platform_key,
        surface=compatible_surface,
        observation_ids=observation_ids,
        heat=_round_one_decimal(max(item.heat for item in values)),
    )


def _deterministic_heat_order(item: PlatformHeat) -> tuple[object, ...]:
    return (-item.heat, item.platform_key, item.observation_ids)


def eligible_cohort(
    heats: Sequence[PlatformHeat],
    *,
    platform: str,
    surface: str,
    heat_floor: float = HEAT_FLOOR,
) -> tuple[PlatformHeat, ...]:
    """Filter a broader run sequence to one platform/surface cohort."""

    parsed_platform = validate_platform(platform)
    if not isinstance(surface, str) or not surface:
        raise ValueError("surface must be nonempty text")
    floor = _finite_number(heat_floor, "heat_floor")
    if floor < 0 or floor > 100:
        raise ValueError("heat_floor must be between 0 and 100")
    values = tuple(heats)
    if not all(isinstance(item, PlatformHeat) for item in values):
        raise TypeError("heats must contain PlatformHeat records")
    if len({item.run_id for item in values}) > 1:
        raise ValueError("eligible cohort input must belong to one run")
    selected = tuple(
        item
        for item in values
        if parse_platform_key(item.platform_key)[0] == parsed_platform
        and item.surface == surface
        and item.heat >= floor
    )
    return tuple(sorted(selected, key=_deterministic_heat_order))


def average_tie_rank(values: Sequence[float]) -> tuple[float, ...]:
    """Return descending one-based average ranks aligned to input order."""

    parsed = tuple(_finite_number(value, "heat") for value in values)
    positions: dict[float, list[int]] = {}
    for position, value in enumerate(sorted(parsed, reverse=True), start=1):
        positions.setdefault(value, []).append(position)
    rank_by_value = {
        value: sum(value_positions) / len(value_positions)
        for value, value_positions in positions.items()
    }
    return tuple(float(rank_by_value[value]) for value in parsed)


def normalize_cohort(
    heats: Sequence[PlatformHeat],
    *,
    heat_floor: float = HEAT_FLOOR,
) -> tuple[NormalizedHeat, ...]:
    """Normalize exactly one compatible cohort into a deterministic score list."""

    values = tuple(heats)
    floor = _finite_number(heat_floor, "heat_floor")
    if floor < 0 or floor > 100:
        raise ValueError("heat_floor must be between 0 and 100")
    if not values:
        return ()
    if not all(isinstance(item, PlatformHeat) for item in values):
        raise TypeError("heats must contain PlatformHeat records")
    platforms = {parse_platform_key(item.platform_key)[0] for item in values}
    surfaces = {item.surface for item in values}
    run_ids = {item.run_id for item in values}
    platform_keys = tuple(item.platform_key for item in values)
    if len(platforms) != 1:
        raise ValueError("normalize_cohort cannot mix platforms")
    if len(surfaces) != 1:
        raise ValueError("normalize_cohort cannot mix compatible surfaces")
    if len(run_ids) != 1:
        raise ValueError("normalize_cohort cannot mix runs")
    if len(set(platform_keys)) != len(platform_keys):
        raise ValueError("normalize_cohort requires one heat per platform key")

    eligible = tuple(
        sorted(
            (item for item in values if item.heat >= floor),
            key=_deterministic_heat_order,
        )
    )
    if not eligible:
        return ()

    count = len(eligible)
    ranks = average_tie_rank(tuple(item.heat for item in eligible))
    all_tied = len({item.heat for item in eligible}) == 1
    results: list[NormalizedHeat] = []
    for item, average_rank in zip(eligible, ranks):
        absolute_score = 30 * item.heat / 100
        if count < 5 or all_tied:
            platform_score = min(15, absolute_score)
        else:
            percentile = (count - average_rank) / (count - 1)
            platform_score = min(absolute_score, 30 * percentile)
        results.append(
            NormalizedHeat(
                schema_version=1,
                run_id=item.run_id,
                platform_key=item.platform_key,
                surface=item.surface,
                observation_ids=item.observation_ids,
                heat=_round_one_decimal(item.heat),
                platform_score=_round_one_decimal(platform_score),
            )
        )
    return tuple(results)


__all__ = [
    "HEAT_FLOOR",
    "ITCH_DISCOVERY",
    "ROBLOX_GLOBAL",
    "ROBLOX_PERSONALIZED",
    "STEAM_RELEASED",
    "STEAM_UPCOMING",
    "ItchHeatInput",
    "RobloxHeatInput",
    "SteamReleasedHeatInput",
    "SteamUpcomingHeatInput",
    "average_tie_rank",
    "eligible_cohort",
    "normalize_cohort",
    "score_itch_heat",
    "score_roblox_heat",
    "score_steam_released_heat",
    "score_steam_upcoming_heat",
    "select_record_heat",
]
