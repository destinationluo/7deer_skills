from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from steam_game_radar.enrichment import EnrichmentRecord, Evidence
from steam_game_radar.errors import InputValidationError
from steam_game_radar.schemas import GameRecord, MetricObservation, WarningRecord
from steam_game_radar.score import (
    ScoredCandidate,
    apply_final_score,
    candidate_sort_key,
    interpolate,
    score_released,
    score_seo,
    score_unreleased,
)
from steam_game_radar.trend import AnalyzedCandidate


OBSERVED_AT = "2026-08-24T12:00:00Z"


class ScoreTests(unittest.TestCase):
    def observation(
        self,
        value: object,
        source_id: str,
        *,
        kind: str = "steam_official",
        observed_at: str = OBSERVED_AT,
    ) -> MetricObservation:
        return MetricObservation(
            value=value,
            source_id=source_id,
            source_kind=kind,  # type: ignore[arg-type]
            observed_at=observed_at,
        )

    def candidate(
        self,
        *,
        appid: int = 10,
        name: str = "Example Game",
        release_status: str = "released",
        metrics: dict[str, MetricObservation] | None = None,
        deltas: dict[str, float] | None = None,
        newly_observed: bool = False,
    ) -> AnalyzedCandidate:
        return AnalyzedCandidate(
            record=GameRecord(
                schema_version=1,
                appid=appid,
                name=name,
                release_status=release_status,  # type: ignore[arg-type]
                store_url=f"https://store.steampowered.com/app/{appid}/",
                metrics=metrics or {},
                source_extra={},
            ),
            deltas=deltas or {},
            newly_observed=newly_observed,
            warnings=(WarningRecord("fixture", "fixture warning", appid),),
        )

    def enrichment(
        self,
        *,
        google: int = 100,
        queries: tuple[str, ...] = tuple(f"query {index}" for index in range(20)),
        youtube: int | None = None,
        reddit: int | None = None,
        reddit_upvotes: int | None = None,
    ) -> EnrichmentRecord:
        evidence = [Evidence("google", "https://google.example/search")]
        if youtube is not None:
            evidence.append(Evidence("youtube", "https://youtube.example/results"))
        if reddit is not None or reddit_upvotes is not None:
            evidence.append(Evidence("reddit", "https://reddit.example/search"))
        return EnrichmentRecord(
            appid=10,
            google_competition_gap_score=google,
            expandable_queries=queries,
            youtube_relevant_7d=youtube,
            reddit_relevant_7d=reddit,
            reddit_upvotes_7d=reddit_upvotes,
            evidence=evidence,
        )

    def preliminary(self, heat: float | None, *, candidate: AnalyzedCandidate | None = None) -> ScoredCandidate:
        analyzed = candidate or self.candidate()
        return ScoredCandidate(
            record=analyzed.record,
            deltas=analyzed.deltas,
            metric_scores={},
            steam_heat_score=heat,
            seo_opportunity_score=None,
            final_score=None,
            action="needs_seo_enrichment" if heat is not None else "insufficient_data",
            confidence="C",
            warnings=analyzed.warnings,
            evidence=(),
            recommended_content_types=(),
        )

    def test_interpolate_clamps_boundaries_midpoints_and_rejects_invalid_values(self) -> None:
        points = ((0, 0), (10, 20), (30, 100))
        cases = ((-5, 0), (0, 0), (5, 10), (10, 20), (20, 60), (30, 100), (50, 100))
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(interpolate(points, value), expected)
        for bad_points, value in (
            ((), 1),
            (((0, 0), (0, 1)), 1),
            (((1, 0), (0, 1)), 1),
            (((0, 0), (1, 101)), 1),
        ):
            with self.subTest(points=bad_points), self.assertRaises(InputValidationError):
                interpolate(bad_points, value)
        for value in (True, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(InputValidationError):
                interpolate(points, value)

    def test_released_player_growth_and_current_scale_transforms(self) -> None:
        growth_cases = ((-10, 0), (0, 0), (2.5, 12.5), (5, 25), (10, 37.5), (15, 50), (22.5, 62.5), (30, 75), (45, 87.5), (60, 100), (100, 100))
        for growth, expected in growth_cases:
            candidate = self.candidate(deltas={"current_players_1d_percent": growth, "current_players_7d_percent": growth - 1})
            with self.subTest(growth=growth):
                self.assertEqual(score_released(candidate).metric_scores["player_growth"], expected)

        scale_cases = ((0, 0), (50, 10), (100, 20), (550, 35), (1000, 50), (5500, 65), (10_000, 80), (55_000, 90), (100_000, 100), (200_000, 100))
        for players, expected in scale_cases:
            candidate = self.candidate(metrics={"current_players": self.observation(players, "players")})
            with self.subTest(players=players):
                self.assertEqual(score_released(candidate).metric_scores["current_player_scale"], expected)

    def test_released_rank_uses_seven_day_then_one_day_then_provider(self) -> None:
        metrics = {
            "most_played_rank": self.observation(10, "steam_most_played_rank"),
            "previous_rank": self.observation(60, "steam_previous_rank"),
        }
        precedence = (
            ({"most_played_rank_7d_change": 5, "most_played_rank_1d_change": 20}, 40),
            ({"most_played_rank_1d_change": 20}, 70),
            ({}, 100),
        )
        for deltas, expected in precedence:
            with self.subTest(deltas=deltas):
                scored = score_released(self.candidate(metrics=metrics, deltas=deltas))
                self.assertEqual(scored.metric_scores["rank_improvement"], expected)
        for improvement, expected in ((-1, 0), (0, 0), (2.5, 20), (5, 40), (12.5, 55), (20, 70), (35, 85), (50, 100), (75, 100)):
            with self.subTest(improvement=improvement):
                scored = score_released(
                    self.candidate(
                        metrics={"rank": self.observation(10, "rank")},
                        deltas={"rank_7d_change": improvement},
                    )
                )
                self.assertEqual(scored.metric_scores["rank_improvement"], expected)

    def test_released_recency_uses_release_observation_utc_date(self) -> None:
        cases = (("2026-08-24", 100), ("2026-08-17", 100), ("2026-08-16", 70), ("2026-07-25", 70), ("2026-07-24", 40), ("2026-05-26", 40), ("2026-05-25", 0))
        for release_date, expected in cases:
            observation = self.observation(release_date, "release", observed_at="2026-08-24T00:30:00Z")
            with self.subTest(release_date=release_date):
                scored = score_released(self.candidate(metrics={"release_date": observation}))
                self.assertEqual(scored.metric_scores["release_recency"], expected)
        future = score_released(self.candidate(metrics={"release_date": self.observation("2026-08-25", "release")}))
        self.assertNotIn("release_recency", future.metric_scores)

    def test_released_gate_uses_available_weighted_average(self) -> None:
        insufficient = score_released(self.candidate(metrics={"current_players": self.observation(100_000, "players")}))
        self.assertIsNone(insufficient.steam_heat_score)
        self.assertEqual(insufficient.action, "insufficient_data")
        self.assertEqual(insufficient.confidence, "C")

        passing = score_released(self.candidate(metrics={"current_players": self.observation(100_000, "players")}, deltas={"current_players_1d_percent": 15}))
        self.assertEqual(dict(passing.metric_scores), {"current_player_scale": 100.0, "player_growth": 50.0})
        self.assertEqual(passing.steam_heat_score, 64.3)
        self.assertEqual(passing.action, "needs_seo_enrichment")
        self.assertEqual(passing.warnings, (WarningRecord("fixture", "fixture warning", 10),))

        wrong_status = self.candidate(release_status="unreleased")
        with self.assertRaises(InputValidationError):
            score_released(wrong_status)

    def test_unreleased_rank_transform_and_precedence(self) -> None:
        cases = (
            ({"coming_soon_rank_7d_change": 5, "coming_soon_rank_1d_change": 50}, 40),
            ({"coming_soon_rank_1d_change": 20}, 70),
        )
        for deltas, expected in cases:
            with self.subTest(deltas=deltas):
                scored = score_unreleased(
                    self.candidate(
                        release_status="unreleased",
                        metrics={
                            "coming_soon_rank": self.observation(10, "coming_soon")
                        },
                        deltas=deltas,
                    )
                )
                self.assertEqual(scored.metric_scores["upcoming_rank_improvement"], expected)
        for improvement, expected in ((0, 0), (2.5, 20), (5, 40), (12.5, 55), (20, 70), (35, 85), (50, 100)):
            with self.subTest(improvement=improvement):
                scored = score_unreleased(
                    self.candidate(
                        release_status="unreleased",
                        metrics={"rank": self.observation(10, "rank")},
                        deltas={"rank_7d_change": improvement},
                    )
                )
                self.assertEqual(scored.metric_scores["upcoming_rank_improvement"], expected)

    def test_unreleased_uses_larger_wishlist_or_follower_gain(self) -> None:
        gain_cases = ((0, 0), (50, 10), (100, 20), (550, 40), (1000, 60), (3000, 72.5), (5000, 85), (12_500, 92.5), (20_000, 100), (30_000, 100))
        for gain, expected in gain_cases:
            metrics = {
                "wishlist_gain_7d": self.observation(gain, "wishlist"),
                "follower_gain_7d": self.observation(max(0, gain - 1), "followers"),
            }
            with self.subTest(gain=gain):
                scored = score_unreleased(self.candidate(release_status="unreleased", metrics=metrics))
                self.assertEqual(scored.metric_scores["wishlist_or_follower_gain"], expected)

    def test_unreleased_proximity_and_visibility_exact_ranges(self) -> None:
        proximity = (("2026-08-24", 100), ("2026-09-07", 100), ("2026-09-08", 80), ("2026-09-23", 80), ("2026-09-24", 60), ("2026-11-22", 60), ("2026-11-23", 30), ("2027-02-20", 30), ("2027-02-21", 0))
        for release_date, expected in proximity:
            with self.subTest(release_date=release_date):
                scored = score_unreleased(self.candidate(release_status="unreleased", metrics={"release_date": self.observation(release_date, "release")}))
                self.assertEqual(scored.metric_scores["release_proximity"], expected)
        for rank, expected in ((1, 100), (2, 80), (5, 80), (6, 50), (20, 50), (21, 20), (50, 20), (51, 0)):
            with self.subTest(rank=rank):
                scored = score_unreleased(self.candidate(release_status="unreleased", metrics={"coming_soon_rank": self.observation(rank, "coming_soon")}))
                self.assertEqual(scored.metric_scores["coming_soon_visibility"], expected)

    def test_unreleased_gate_uses_available_weighted_average(self) -> None:
        insufficient = score_unreleased(self.candidate(release_status="unreleased", metrics={"release_date": self.observation("2026-08-30", "release")}))
        self.assertIsNone(insufficient.steam_heat_score)
        self.assertEqual(insufficient.action, "insufficient_data")

        passing = score_unreleased(self.candidate(release_status="unreleased", metrics={"release_date": self.observation("2026-08-30", "release"), "wishlist_gain_7d": self.observation(1000, "wishlist")}))
        self.assertEqual(passing.steam_heat_score, 73.3)
        self.assertEqual(passing.action, "needs_seo_enrichment")
        with self.assertRaises(InputValidationError):
            score_unreleased(self.candidate(release_status="released"))

    def test_seo_transforms_require_google_plus_another_metric_and_weight_30(self) -> None:
        self.assertIsNone(score_seo(self.enrichment(queries=(), youtube=None, reddit=None)))
        query_score = score_seo(self.enrichment(google=80, queries=tuple(f"q{i}" for i in range(10))))
        self.assertEqual(query_score, 70.0)
        capped = score_seo(self.enrichment(google=100, queries=tuple(f"q{i}" for i in range(30))))
        self.assertEqual(capped, 100.0)
        cross_signal = score_seo(self.enrichment(google=80, queries=(), youtube=3, reddit=10))
        self.assertEqual(cross_signal, 75.0)
        youtube_only = score_seo(self.enrichment(google=80, queries=(), youtube=1))
        self.assertEqual(youtube_only, 60.0)

    def test_final_score_actions_confidence_evidence_and_content_are_exact(self) -> None:
        record = self.enrichment(youtube=25, reddit=25, reddit_upvotes=20)
        for heat, expected_score, action in (
            (0.0, 40.0, "skip"),
            (16.7, 50.0, "watch"),
            (41.7, 65.0, "worth_positioning"),
            (66.7, 80.0, "immediate_action"),
            (100.0, 100.0, "immediate_action"),
        ):
            with self.subTest(heat=heat):
                final = apply_final_score(self.preliminary(heat), record, {"steam_official", "steamdb_manual_import", "historical_comparison"})
                self.assertEqual(final.seo_opportunity_score, 100.0)
                self.assertEqual(final.final_score, expected_score)
                self.assertEqual(final.action, action)
                self.assertEqual(final.confidence, "A")
                self.assertEqual(final.evidence, record.evidence)
                self.assertEqual(final.recommended_content_types, ("wiki_or_guide", "video", "community"))

        for provenance in (
            {"steam_official", "historical_comparison"},
            {"steamdb_manual_import", "historical_comparison"},
        ):
            self.assertEqual(apply_final_score(self.preliminary(100), record, provenance).confidence, "B")
        for provenance in (
            {"steamdb_manual_import"},
            {"steam_official"},
            {"historical_comparison"},
            set(),
        ):
            final = apply_final_score(self.preliminary(100), record, provenance)
            self.assertEqual(final.confidence, "C")
            self.assertIsNone(final.final_score)
            self.assertEqual(final.action, "needs_seo_enrichment")
        insufficient = apply_final_score(self.preliminary(None), record, {"steam_official", "historical_comparison"})
        self.assertEqual(insufficient.action, "insufficient_data")
        self.assertIsNone(insufficient.final_score)
        with self.assertRaises(InputValidationError):
            apply_final_score(self.preliminary(100), record, {"steam_official", "history"})
        with self.assertRaises(InputValidationError):
            replace(
                self.preliminary(80),
                seo_opportunity_score=80,
                final_score=80,
                action="watch",
                confidence="B",
            )
        with self.assertRaises(InputValidationError):
            self.preliminary(float("nan"))

    def test_stable_sort_is_score_confidence_scale_name_then_appid(self) -> None:
        def scored(*, appid: int, name: str, primary: float, confidence: str, players: int = 0, release_status: str = "released", gain: int = 0, final: bool = True) -> ScoredCandidate:
            metrics = {"current_players": self.observation(players, "players")}
            if release_status == "unreleased":
                metrics = {"wishlist_gain_7d": self.observation(gain, "gain")}
            analyzed = self.candidate(appid=appid, name=name, release_status=release_status, metrics=metrics)
            base = self.preliminary(primary, candidate=analyzed)
            if not final:
                return replace(base, confidence=confidence)  # type: ignore[arg-type]
            return replace(
                base,
                seo_opportunity_score=primary,
                final_score=primary,
                action="immediate_action" if primary >= 80 else "watch",
                confidence=confidence,
            )  # type: ignore[arg-type]

        values = [
            scored(appid=5, name="Zulu", primary=90, confidence="C", players=999, final=False),
            scored(appid=4, name="Zulu", primary=80, confidence="B", players=100),
            scored(appid=3, name="Alpha", primary=80, confidence="A", players=10),
            scored(appid=2, name="alpha", primary=80, confidence="A", players=20),
            scored(appid=1, name="alpha", primary=80, confidence="A", players=20),
            scored(appid=8, name="Unreleased", primary=80, confidence="A", release_status="unreleased", gain=50),
            scored(appid=7, name="Unreleased", primary=80, confidence="A", release_status="unreleased", gain=100),
        ]
        ordered = sorted(values, key=candidate_sort_key)
        self.assertEqual([item.record.appid for item in ordered], [5, 7, 8, 1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
