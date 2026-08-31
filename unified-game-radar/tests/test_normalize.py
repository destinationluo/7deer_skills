from __future__ import annotations

from itertools import permutations
from pathlib import Path
import sys
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from unified_game_radar.normalize import (
    HEAT_FLOOR,
    ItchHeatInput,
    RobloxHeatInput,
    SteamReleasedHeatInput,
    SteamUpcomingHeatInput,
    average_tie_rank,
    eligible_cohort,
    normalize_cohort,
    score_itch_heat,
    score_roblox_heat,
    score_steam_released_heat,
    score_steam_upcoming_heat,
    select_record_heat,
)
from unified_game_radar.schemas import PlatformHeat


RUN_ID = "20260831T020000Z-a1b2c3d4"
OTHER_RUN_ID = "20260831T080000Z-b1c2d3e4"
OBSERVED_AT = "20260831T020000Z"


def observation_id(
    platform_key: str,
    surface: str = "chart",
    observed_at: str = OBSERVED_AT,
) -> str:
    platform, platform_id = platform_key.split(":", 1)
    return f"{platform}:{platform_id}:{surface}:{observed_at}"


def itch_input(**changes: object) -> ItchHeatInput:
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "platform_key": "itch:studio-game",
        "observation_ids": (observation_id("itch:studio-game", "newest"),),
    }
    values.update(changes)
    return ItchHeatInput(**values)  # type: ignore[arg-type]


def released_input(**changes: object) -> SteamReleasedHeatInput:
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "platform_key": "steam:10",
        "observation_ids": (observation_id("steam:10", "most_played"),),
    }
    values.update(changes)
    return SteamReleasedHeatInput(**values)  # type: ignore[arg-type]


def upcoming_input(**changes: object) -> SteamUpcomingHeatInput:
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "platform_key": "steam:11",
        "observation_ids": (observation_id("steam:11", "coming_soon"),),
    }
    values.update(changes)
    return SteamUpcomingHeatInput(**values)  # type: ignore[arg-type]


def roblox_input(**changes: object) -> RobloxHeatInput:
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "platform_key": "roblox:12",
        "observation_ids": (observation_id("roblox:12", "rising"),),
    }
    values.update(changes)
    return RobloxHeatInput(**values)  # type: ignore[arg-type]


def heat(
    platform_key: str,
    value: float,
    *,
    surface: str | None = None,
    run_id: str = RUN_ID,
    raw_surface: str = "chart",
) -> PlatformHeat:
    platform = platform_key.split(":", 1)[0]
    default_surface = {
        "itch": "itch_discovery",
        "steam": "steam_released",
        "roblox": "roblox_global",
    }[platform]
    return PlatformHeat(
        schema_version=1,
        run_id=run_id,
        platform_key=platform_key,
        surface=surface or default_surface,
        observation_ids=(observation_id(platform_key, raw_surface),),
        heat=value,
    )


class ItchHeatTests(unittest.TestCase):
    def test_first_seen_age_boundaries(self) -> None:
        cases = (
            (None, 0),
            (24, 25),
            (24.001, 15),
            (72, 15),
            (72.001, 5),
            (168, 5),
            (168.001, 0),
        )
        for age, expected in cases:
            with self.subTest(age=age):
                result = score_itch_heat(itch_input(first_seen_age_hours=age))
                self.assertIsNotNone(result)
                self.assertEqual(result.heat, expected)  # type: ignore[union-attr]

    def test_popular_rank_boundaries(self) -> None:
        cases = (
            (None, 0),
            (10, 35),
            (11, 25),
            (25, 25),
            (26, 15),
            (50, 15),
            (51, 0),
        )
        for rank, expected in cases:
            with self.subTest(rank=rank):
                result = score_itch_heat(itch_input(popular_rank=rank))
                self.assertEqual(result.heat, expected)  # type: ignore[union-attr]

    def test_compatible_popular_rank_improvement_boundaries(self) -> None:
        cases = (
            (20, 20),
            (19, 10),
            (5, 10),
            (4, 5),
            (1, 5),
            (0, 0),
            (-1, 0),
        )
        for improvement, expected in cases:
            with self.subTest(improvement=improvement):
                result = score_itch_heat(
                    itch_input(
                        popular_rank=51,
                        previous_popular_rank=51 + improvement,
                        rank_history_compatible=True,
                    )
                )
                self.assertEqual(result.heat, expected)  # type: ignore[union-attr]

    def test_first_or_incompatible_observation_cannot_claim_rank_growth(self) -> None:
        for previous_rank, compatible in ((None, True), (50, False)):
            with self.subTest(previous_rank=previous_rank, compatible=compatible):
                result = score_itch_heat(
                    itch_input(
                        popular_rank=51,
                        previous_popular_rank=previous_rank,
                        rank_history_compatible=compatible,
                    )
                )
                self.assertEqual(result.heat, 0)  # type: ignore[union-attr]

    def test_originality_browser_and_author_components(self) -> None:
        unknown = score_itch_heat(itch_input(originality="unknown"))
        original = score_itch_heat(itch_input(originality="verified_original"))
        browser = score_itch_heat(itch_input(browser_playable=True))
        author = score_itch_heat(
            itch_input(author_release_count=2, author_non_spam=True)
        )
        just_below_author = score_itch_heat(
            itch_input(author_release_count=1, author_non_spam=True)
        )
        spam_author = score_itch_heat(
            itch_input(author_release_count=20, author_non_spam=False)
        )

        self.assertEqual(unknown.heat, 5)  # type: ignore[union-attr]
        self.assertEqual(original.heat, 10)  # type: ignore[union-attr]
        self.assertEqual(browser.heat, 5)  # type: ignore[union-attr]
        self.assertEqual(author.heat, 5)  # type: ignore[union-attr]
        self.assertEqual(just_below_author.heat, 0)  # type: ignore[union-attr]
        self.assertEqual(spam_author.heat, 0)  # type: ignore[union-attr]

    def test_known_reupload_and_collector_filtered_rows_are_ineligible(self) -> None:
        positive_facts = {
            "first_seen_age_hours": 1,
            "popular_rank": 1,
            "previous_popular_rank": 50,
            "rank_history_compatible": True,
            "browser_playable": True,
            "author_release_count": 9,
            "author_non_spam": True,
        }
        self.assertIsNone(
            score_itch_heat(
                itch_input(originality="known_reupload", **positive_facts)
            )
        )
        self.assertIsNone(
            score_itch_heat(
                itch_input(
                    originality="verified_original",
                    collector_eligible=False,
                    **positive_facts,
                )
            )
        )

    def test_missing_verified_components_are_zero_without_reweighting(self) -> None:
        result = score_itch_heat(itch_input())
        self.assertEqual(result.heat, 0)  # type: ignore[union-attr]
        self.assertEqual(result.surface, "itch_discovery")  # type: ignore[union-attr]


class SteamReleasedHeatTests(unittest.TestCase):
    def test_official_rank_boundaries(self) -> None:
        cases = (
            (None, 0),
            (10, 25),
            (11, 20),
            (25, 20),
            (26, 15),
            (50, 15),
            (51, 8),
            (100, 8),
            (101, 0),
        )
        for rank, expected in cases:
            with self.subTest(rank=rank):
                self.assertEqual(
                    score_steam_released_heat(
                        released_input(official_rank=rank)
                    ).heat,
                    expected,
                )

    def test_rank_improvement_boundaries(self) -> None:
        cases = ((20, 25), (19, 18), (10, 18), (9, 10), (5, 10), (4, 5), (1, 5), (0, 0))
        for improvement, expected in cases:
            with self.subTest(improvement=improvement):
                result = score_steam_released_heat(
                    released_input(
                        official_rank=101,
                        previous_official_rank=101 + improvement,
                        rank_history_compatible=True,
                    )
                )
                self.assertEqual(result.heat, expected)

    def test_player_growth_boundaries_are_percentage_points(self) -> None:
        cases = (
            (None, 0),
            (100, 25),
            (99.999, 20),
            (50, 20),
            (49.999, 12),
            (20, 12),
            (19.999, 5),
            (5e-324, 5),
            (0.001, 5),
            (0, 0),
            (-0.001, 0),
        )
        for growth, expected in cases:
            with self.subTest(growth=growth):
                result = score_steam_released_heat(
                    released_input(
                        current_player_growth_percent=growth,
                        player_growth_history_compatible=True,
                    )
                )
                self.assertEqual(result.heat, expected)

    def test_first_or_incompatible_observation_cannot_claim_player_growth(self) -> None:
        for growth, compatible in ((None, True), (500, False)):
            with self.subTest(growth=growth, compatible=compatible):
                result = score_steam_released_heat(
                    released_input(
                        current_player_growth_percent=growth,
                        player_growth_history_compatible=compatible,
                    )
                )
                self.assertEqual(result.heat, 0)

    def test_current_player_scale_boundaries(self) -> None:
        cases = ((None, 0), (10000, 15), (9999, 10), (1000, 10), (999, 5), (100, 5), (99, 0))
        for players, expected in cases:
            with self.subTest(players=players):
                self.assertEqual(
                    score_steam_released_heat(
                        released_input(current_players=players)
                    ).heat,
                    expected,
                )

    def test_release_age_boundaries(self) -> None:
        cases = ((None, 0), (7, 10), (7.001, 5), (30, 5), (30.001, 0))
        for age, expected in cases:
            with self.subTest(age=age):
                self.assertEqual(
                    score_steam_released_heat(
                        released_input(release_age_days=age)
                    ).heat,
                    expected,
                )

    def test_missing_components_are_zero_without_reweighting(self) -> None:
        result = score_steam_released_heat(released_input())
        self.assertEqual(result.heat, 0)
        self.assertEqual(result.surface, "steam_released")


class SteamUpcomingHeatTests(unittest.TestCase):
    def test_coming_soon_rank_boundaries(self) -> None:
        cases = ((None, 0), (10, 30), (11, 24), (25, 24), (26, 15), (50, 15), (51, 0))
        for rank, expected in cases:
            with self.subTest(rank=rank):
                self.assertEqual(
                    score_steam_upcoming_heat(
                        upcoming_input(coming_soon_rank=rank)
                    ).heat,
                    expected,
                )

    def test_rank_improvement_boundaries(self) -> None:
        cases = ((20, 30), (19, 20), (10, 20), (9, 10), (1, 10), (0, 0))
        for improvement, expected in cases:
            with self.subTest(improvement=improvement):
                result = score_steam_upcoming_heat(
                    upcoming_input(
                        coming_soon_rank=51,
                        previous_coming_soon_rank=51 + improvement,
                        rank_history_compatible=True,
                    )
                )
                self.assertEqual(result.heat, expected)

    def test_verified_follower_or_wishlist_growth_boundaries(self) -> None:
        cases = (
            (None, 0),
            (50, 20),
            (49.999, 15),
            (20, 15),
            (19.999, 8),
            (5e-324, 8),
            (0.001, 8),
            (0, 0),
        )
        for growth, expected in cases:
            with self.subTest(growth=growth):
                result = score_steam_upcoming_heat(
                    upcoming_input(
                        follower_or_wishlist_growth_percent=growth,
                        growth_verified=True,
                        growth_history_compatible=True,
                    )
                )
                self.assertEqual(result.heat, expected)

    def test_unverified_first_or_incompatible_growth_is_zero(self) -> None:
        cases = (
            (50, False, True),
            (50, True, False),
            (None, True, True),
        )
        for growth, verified, compatible in cases:
            with self.subTest(
                growth=growth, verified=verified, compatible=compatible
            ):
                result = score_steam_upcoming_heat(
                    upcoming_input(
                        follower_or_wishlist_growth_percent=growth,
                        growth_verified=verified,
                        growth_history_compatible=compatible,
                    )
                )
                self.assertEqual(result.heat, 0)

    def test_release_proximity_boundaries(self) -> None:
        cases = ((None, 0), (-0.001, 0), (0, 10), (7, 10), (7.001, 7), (30, 7), (30.001, 3))
        for days, expected in cases:
            with self.subTest(days=days):
                self.assertEqual(
                    score_steam_upcoming_heat(
                        upcoming_input(release_days_away=days)
                    ).heat,
                    expected,
                )

    def test_same_run_steam_discovery_surface_count_boundary(self) -> None:
        for count, expected in ((None, 0), (0, 0), (1, 0), (2, 10)):
            with self.subTest(count=count):
                self.assertEqual(
                    score_steam_upcoming_heat(
                        upcoming_input(same_run_discovery_surface_count=count)
                    ).heat,
                    expected,
                )

    def test_missing_components_are_zero_without_reweighting(self) -> None:
        result = score_steam_upcoming_heat(upcoming_input())
        self.assertEqual(result.heat, 0)
        self.assertEqual(result.surface, "steam_upcoming")


class RobloxHeatTests(unittest.TestCase):
    def test_chart_rank_boundaries(self) -> None:
        cases = ((None, 0), (10, 30), (11, 24), (25, 24), (26, 15), (50, 15), (51, 0))
        for rank, expected in cases:
            with self.subTest(rank=rank):
                self.assertEqual(
                    score_roblox_heat(roblox_input(chart_rank=rank)).heat,
                    expected,
                )

    def test_compatible_rank_improvement_boundaries(self) -> None:
        cases = ((20, 30), (19, 20), (10, 20), (9, 10), (1, 10), (0, 0))
        for improvement, expected in cases:
            with self.subTest(improvement=improvement):
                result = score_roblox_heat(
                    roblox_input(
                        chart_rank=51,
                        previous_chart_rank=51 + improvement,
                        rank_history_compatible=True,
                    )
                )
                self.assertEqual(result.heat, expected)

    def test_concurrent_player_growth_boundaries(self) -> None:
        cases = (
            (None, 0),
            (100, 25),
            (99.999, 20),
            (50, 20),
            (49.999, 12),
            (20, 12),
            (19.999, 5),
            (5e-324, 5),
            (0.001, 5),
            (0, 0),
        )
        for growth, expected in cases:
            with self.subTest(growth=growth):
                result = score_roblox_heat(
                    roblox_input(
                        concurrent_player_growth_percent=growth,
                        player_growth_history_compatible=True,
                    )
                )
                self.assertEqual(result.heat, expected)

    def test_first_or_incompatible_observation_cannot_claim_growth(self) -> None:
        for growth, compatible in ((None, True), (500, False)):
            with self.subTest(growth=growth, compatible=compatible):
                result = score_roblox_heat(
                    roblox_input(
                        concurrent_player_growth_percent=growth,
                        player_growth_history_compatible=compatible,
                    )
                )
                self.assertEqual(result.heat, 0)

    def test_concurrent_scale_boundaries(self) -> None:
        cases = ((None, 0), (10000, 10), (9999, 7), (1000, 7), (999, 3), (100, 3), (99, 0))
        for players, expected in cases:
            with self.subTest(players=players):
                self.assertEqual(
                    score_roblox_heat(
                        roblox_input(concurrent_players=players)
                    ).heat,
                    expected,
                )

    def test_consecutive_appearance_boundaries(self) -> None:
        for appearances, expected in ((None, 0), (1, 0), (2, 3), (3, 5)):
            with self.subTest(appearances=appearances):
                self.assertEqual(
                    score_roblox_heat(
                        roblox_input(
                            consecutive_compatible_appearances=appearances
                        )
                    ).heat,
                    expected,
                )

    def test_personalized_surface_is_explicit_and_separate(self) -> None:
        result = score_roblox_heat(
            roblox_input(cohort_surface="roblox_personalized", chart_rank=1)
        )
        self.assertEqual(result.surface, "roblox_personalized")

    def test_missing_components_are_zero_without_reweighting(self) -> None:
        result = score_roblox_heat(roblox_input())
        self.assertEqual(result.heat, 0)
        self.assertEqual(result.surface, "roblox_global")


class RecordHeatSelectionTests(unittest.TestCase):
    def test_selects_max_compatible_heat_and_retains_all_observations(self) -> None:
        first = PlatformHeat(
            1,
            RUN_ID,
            "steam:10",
            "steam_released",
            (observation_id("steam:10", "most_played"),),
            51.24,
        )
        second = PlatformHeat(
            1,
            RUN_ID,
            "steam:10",
            "steam_released",
            (observation_id("steam:10", "top_sellers"),),
            72.35,
        )

        selected = select_record_heat(
            (first, second), compatible_surface="steam_released"
        )

        self.assertEqual(selected.heat, 72.4)
        self.assertEqual(
            selected.observation_ids,
            tuple(sorted(first.observation_ids + second.observation_ids)),
        )

    def test_tie_selection_is_deterministic_across_input_order(self) -> None:
        values = (
            heat("steam:10", 50, raw_surface="most_played"),
            heat("steam:10", 50, raw_surface="top_sellers"),
            heat("steam:10", 40, raw_surface="new_releases"),
        )
        expected = select_record_heat(
            values, compatible_surface="steam_released"
        )
        for ordering in permutations(values):
            self.assertEqual(
                select_record_heat(
                    ordering, compatible_surface="steam_released"
                ),
                expected,
            )

    def test_rejects_incompatible_record_or_surface_inputs(self) -> None:
        cases = (
            (heat("steam:10", 50), heat("steam:11", 60)),
            (
                heat("steam:10", 50),
                heat("steam:10", 60, surface="steam_upcoming"),
            ),
            (heat("steam:10", 50), heat("steam:10", 60, run_id=OTHER_RUN_ID)),
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    select_record_heat(
                        values, compatible_surface="steam_released"
                    )


class CohortNormalizationTests(unittest.TestCase):
    def test_heat_floor_is_exactly_thirty(self) -> None:
        self.assertEqual(HEAT_FLOOR, 30.0)
        selected = eligible_cohort(
            (heat("steam:1", 29.999), heat("steam:2", 30)),
            platform="steam",
            surface="steam_released",
        )
        self.assertEqual(tuple(item.platform_key for item in selected), ("steam:2",))

    def test_eligible_cohort_filters_broad_sequence_without_mixing(self) -> None:
        values = (
            heat("roblox:1", 80),
            heat("steam:2", 70),
            heat("steam:3", 60, surface="steam_upcoming"),
            heat("steam:4", 20),
            heat("steam:5", 90),
        )
        selected = eligible_cohort(
            values, platform="steam", surface="steam_released"
        )
        self.assertEqual(
            tuple(item.platform_key for item in selected),
            ("steam:5", "steam:2"),
        )

    def test_average_tie_ranks_are_one_based_and_input_aligned(self) -> None:
        self.assertEqual(
            average_tie_rank((80, 100, 80, 40)),
            (2.5, 1.0, 2.5, 4.0),
        )
        self.assertEqual(average_tie_rank((50, 50, 50)), (2.0, 2.0, 2.0))

    def test_one_item_cohort_cannot_receive_more_than_fifteen(self) -> None:
        scored = normalize_cohort((heat("steam:1", 100),))
        self.assertEqual(scored[0].platform_score, 15.0)

    def test_four_item_cohort_uses_small_cohort_cap(self) -> None:
        scored = normalize_cohort(
            tuple(
                heat(f"steam:{index}", value)
                for index, value in enumerate((100, 80, 60, 30), start=1)
            )
        )
        self.assertEqual(
            tuple(item.platform_score for item in scored),
            (15.0, 15.0, 15.0, 9.0),
        )

    def test_five_item_cohort_uses_percentile_formula(self) -> None:
        scored = normalize_cohort(
            tuple(
                heat(f"steam:{index}", value)
                for index, value in enumerate((100, 80, 60, 40, 30), start=1)
            )
        )
        self.assertEqual(
            tuple(item.platform_score for item in scored),
            (30.0, 22.5, 15.0, 7.5, 0.0),
        )

    def test_ties_use_average_rank_and_one_decimal_rounding(self) -> None:
        scored = normalize_cohort(
            tuple(
                heat(f"steam:{index}", value)
                for index, value in enumerate((100, 80, 80, 40, 30), start=1)
            )
        )
        self.assertEqual(
            tuple(item.platform_score for item in scored),
            (30.0, 18.8, 18.8, 7.5, 0.0),
        )

    def test_all_tied_cohort_uses_small_cohort_cap_even_at_five(self) -> None:
        for value, expected in ((100, 15.0), (40, 12.0)):
            with self.subTest(value=value):
                scored = normalize_cohort(
                    tuple(heat(f"steam:{index}", value) for index in range(1, 6))
                )
                self.assertEqual(
                    tuple(item.platform_score for item in scored),
                    (expected,) * 5,
                )

    def test_all_low_heat_is_excluded(self) -> None:
        self.assertEqual(
            normalize_cohort((heat("steam:1", 0), heat("steam:2", 29.9))),
            (),
        )

    def test_persisted_heat_and_score_are_rounded_to_one_decimal(self) -> None:
        scored = normalize_cohort((heat("steam:1", 33.35),))
        self.assertEqual(scored[0].heat, 33.4)
        self.assertEqual(scored[0].platform_score, 10.0)

    def test_output_order_is_deterministic_for_ties(self) -> None:
        values = (
            heat("steam:3", 50),
            heat("steam:1", 50),
            heat("steam:2", 70),
        )
        expected = ("steam:2", "steam:1", "steam:3")
        for ordering in permutations(values):
            self.assertEqual(
                tuple(item.platform_key for item in normalize_cohort(ordering)),
                expected,
            )

    def test_rejects_platform_surface_run_and_duplicate_record_mixing(self) -> None:
        cases = (
            (heat("steam:1", 50), heat("roblox:2", 50)),
            (
                heat("steam:1", 50),
                heat("steam:2", 50, surface="steam_upcoming"),
            ),
            (heat("steam:1", 50), heat("steam:2", 50, run_id=OTHER_RUN_ID)),
            (heat("steam:1", 50), heat("steam:1", 60)),
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    normalize_cohort(values)


if __name__ == "__main__":
    unittest.main()
