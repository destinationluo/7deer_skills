from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from unified_game_radar.demand import DemandClassification
from unified_game_radar.schemas import (
    ExternalEvidence,
    OpportunityEvidence,
    ScoredOpportunity,
    SearchQueryEvidence,
    SerpEvidence,
    TrendEvidence,
    TrendPoint,
)
from unified_game_radar.score import (
    action_for,
    opportunity_sort_key,
    score_demand,
    score_external_spread,
    score_opportunity,
    score_seo_gap,
)


RUN_ID = "20260831T020000Z-a1b2c3d4"
OPPORTUNITY_ID = "0f840f6f-5c62-4ca6-9d53-e0be9ab2740b"
NOW = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)


def search_row(
    query: str,
    *,
    observed_at: datetime = NOW - timedelta(hours=1),
) -> SearchQueryEvidence:
    return SearchQueryEvidence(
        schema_version=1,
        query=query,
        observed_at=observed_at,
        source_url="https://www.google.com/complete/search?q=geoslice",
    )


def serp(
    *,
    guide_results: int | None = 0,
    relevant_nonofficial_results: int | None = 0,
    missing_intents: tuple[str, ...] = ("guide", "codes", "answers", "wiki"),
    observed_at: datetime = NOW - timedelta(hours=1),
) -> SerpEvidence:
    return SerpEvidence(
        query="GeoSlice game",
        relevant_nonofficial_results=relevant_nonofficial_results,
        guide_results=guide_results,
        missing_intents=missing_intents,
        evidence_url="https://www.google.com/search?q=geoslice+game",
        observed_at=observed_at,
    )


def opportunity_evidence(
    values: tuple[float, ...] = (20, 40, 30),
    *,
    autocomplete: tuple[SearchQueryEvidence, ...] = (),
    related: tuple[SearchQueryEvidence, ...] = (),
    external: tuple[ExternalEvidence, ...] = (),
    serp_evidence: SerpEvidence | None = None,
    observed_at: datetime = NOW - timedelta(hours=1),
    trends_observed_at: datetime | None = None,
) -> OpportunityEvidence:
    first_day = date(2026, 8, 30) - timedelta(days=len(values) - 1)
    return OpportunityEvidence(
        schema_version=1,
        run_id=RUN_ID,
        opportunity_id=OPPORTUNITY_ID,
        observed_at=observed_at,
        trends=TrendEvidence(
            query="GeoSlice game",
            query_type="search_term",
            timeframe="now 7-d",
            geo="US",
            category=0,
            property="web",
            timezone="UTC",
            points=tuple(
                TrendPoint(
                    date=first_day + timedelta(days=index),
                    value=value,
                    complete=True,
                )
                for index, value in enumerate(values)
            ),
            comparison_term="gpts",
            comparison_average=41,
            evidence_url="https://trends.google.com/trends/explore?q=geoslice",
            raw_artifact="data/unified-game-radar/raw/trends.json",
            observed_at=trends_observed_at or observed_at,
        ),
        autocomplete_queries=autocomplete,
        related_queries=related,
        external_evidence=external,
        serp=serp_evidence,
    )


def external_row(
    *,
    domain: str = "youtube.com",
    published_age: timedelta = timedelta(days=1),
    observed_at: datetime = NOW - timedelta(hours=1),
    author_relation: str = "independent",
    engagement_count: int | None = 0,
    suffix: str = "one",
) -> ExternalEvidence:
    return ExternalEvidence(
        source=domain,
        url=f"https://{domain}/watch/{suffix}",
        published_at=NOW - published_age,
        observed_at=observed_at,
        author_relation=author_relation,
        engagement_count=engagement_count,
        evidence_kind="video",
    )


def scored(
    opportunity_id: str,
    *,
    action: str,
    total: float,
    demand: float = 10,
    platform: float = 10,
) -> ScoredOpportunity:
    external = 0.0
    seo = round(total - demand - platform, 1)
    return ScoredOpportunity(
        schema_version=1,
        run_id=RUN_ID,
        opportunity_id=opportunity_id,
        demand_state="pass",
        platform_score=platform,
        demand_score=demand,
        external_score=external,
        seo_score=seo,
        total_score=total,
        action=action,
        warnings=(),
    )


class DemandScoreTests(unittest.TestCase):
    def test_persistence_boundaries(self) -> None:
        cases = (
            ((0, 0, 0), 0.0),
            ((10, 0, 0), 2.0),
            ((10, 5, 0), 4.0),
            ((10, 7, 5), 6.0),
            ((10, 8, 6, 4), 8.0),
            ((10, 8, 6, 4, 2), 8.0),
        )
        for values, expected in cases:
            with self.subTest(values=values):
                result = score_demand(
                    opportunity_evidence(values),
                    game_name="GeoSlice",
                    publication_time=NOW,
                )
                peak = max(values)
                retention = 0 if peak == 0 else 8 * values[-1] / peak
                self.assertEqual(result, round(expected + retention, 1))

    def test_latest_retention_boundaries_and_half_up_rounding(self) -> None:
        cases = (
            ((100, 0), 0.0, 0.0),
            ((100, 30), 2.4, 0.0),
            ((100, 50), 4.0, 0.0),
            ((100, 100), 8.0, 6.0),
            ((30, 10), 2.7, 0.0),
        )
        for values, expected_retention, expected_wave in cases:
            with self.subTest(values=values):
                score = score_demand(
                    opportunity_evidence(values),
                    game_name="GeoSlice",
                    publication_time=NOW,
                )
                persistence = 2 * sum(value > 0 for value in values)
                self.assertEqual(
                    score,
                    persistence + expected_retention + expected_wave,
                )

    def test_later_local_maximum_30_and_50_percent_boundaries(self) -> None:
        cases = (
            ((100, 20, 29.9, 10), 0.0),
            ((100, 20, 30, 10), 3.0),
            ((100, 20, 49.9, 10), 3.0),
            ((100, 20, 50, 10), 6.0),
        )
        for values, expected_wave in cases:
            with self.subTest(values=values):
                score = score_demand(
                    opportunity_evidence(values),
                    game_name="GeoSlice",
                    publication_time=NOW,
                )
                persistence = 2 * sum(value > 0 for value in values)
                retention = 8 * values[-1] / 100
                self.assertEqual(score, round(persistence + retention + expected_wave, 1))

    def test_autocomplete_and_related_distinct_count_boundaries(self) -> None:
        cases = (
            ((), 0.0),
            (("GeoSlice codes",), 2.0),
            (("GeoSlice codes", "GEOSLICE   CODES"), 2.0),
            (("GeoSlice codes", "GeoSlice guide"), 4.0),
        )
        for queries, expected in cases:
            rows = tuple(search_row(query) for query in queries)
            with self.subTest(kind="autocomplete", queries=queries):
                actual = score_demand(
                    opportunity_evidence((0, 0, 0), autocomplete=rows),
                    game_name="GeoSlice",
                    publication_time=NOW,
                )
                self.assertEqual(actual, expected)
            with self.subTest(kind="related", queries=queries):
                actual = score_demand(
                    opportunity_evidence((0, 0, 0), related=rows),
                    game_name="GeoSlice",
                    publication_time=NOW,
                )
                self.assertEqual(actual, expected)

    def test_irrelevant_support_does_not_score_and_components_cap_at_30(self) -> None:
        unrelated = search_row("GeoSlice soundtrack")
        self.assertEqual(
            score_demand(
                opportunity_evidence((0, 0), autocomplete=(unrelated,)),
                game_name="GeoSlice",
                publication_time=NOW,
            ),
            0.0,
        )

        support = (
            search_row("GeoSlice codes"),
            search_row("GeoSlice guide"),
        )
        result = score_demand(
            opportunity_evidence(
                (100, 80, 100, 100),
                autocomplete=support,
                related=support,
            ),
            game_name="GeoSlice",
            publication_time=NOW,
        )
        self.assertEqual(result, 30.0)

    def test_missing_or_stale_demand_evidence_scores_zero(self) -> None:
        stale = NOW - timedelta(hours=24, microseconds=1)
        cases = (
            None,
            opportunity_evidence(observed_at=stale, trends_observed_at=stale),
        )
        for item in cases:
            with self.subTest(item=item):
                self.assertEqual(
                    score_demand(
                        item,
                        game_name="GeoSlice",
                        publication_time=NOW,
                    ),
                    0.0,
                )


class ExternalSpreadScoreTests(unittest.TestCase):
    def test_source_domain_diversity_and_evidence_count_boundaries(self) -> None:
        cases = (
            ((), 0.0),
            ((external_row(),), 11.0),
            (
                (
                    external_row(domain="youtube.com", suffix="one"),
                    external_row(domain="youtube.com", suffix="two"),
                ),
                13.0,
            ),
            (
                (
                    external_row(domain="youtube.com"),
                    external_row(domain="reddit.com"),
                ),
                17.0,
            ),
            (
                (
                    external_row(domain="youtube.com"),
                    external_row(domain="reddit.com"),
                    external_row(domain="tiktok.com"),
                ),
                17.0,
            ),
        )
        for rows, expected in cases:
            with self.subTest(count=len(rows)):
                self.assertEqual(
                    score_external_spread(rows, publication_time=NOW),
                    expected,
                )

    def test_highest_verified_engagement_boundaries(self) -> None:
        cases = (
            (None, 0.0),
            (0, 1.0),
            (19, 1.0),
            (20, 2.0),
            (99, 2.0),
            (100, 4.0),
            (999, 4.0),
            (1_000, 6.0),
            (9_999, 6.0),
            (10_000, 8.0),
        )
        for engagement, expected_engagement in cases:
            row = external_row(engagement_count=engagement)
            with self.subTest(engagement=engagement):
                actual = score_external_spread((row,), publication_time=NOW)
                self.assertEqual(actual, 4 + 2 + expected_engagement + 4)

    def test_recency_two_and_seven_day_boundaries(self) -> None:
        cases = (
            (timedelta(days=2), 4.0),
            (timedelta(days=2, microseconds=1), 2.0),
            (timedelta(days=7), 2.0),
            (timedelta(days=7, microseconds=1), None),
        )
        for age, expected_recency in cases:
            row = external_row(published_age=age)
            with self.subTest(age=age):
                actual = score_external_spread((row,), publication_time=NOW)
                expected = 0.0 if expected_recency is None else 4 + 2 + 1 + expected_recency
                self.assertEqual(actual, expected)

    def test_developer_unknown_and_future_rows_receive_no_credit(self) -> None:
        rows = (
            external_row(author_relation="developer", suffix="developer"),
            external_row(author_relation="unknown", suffix="unknown"),
            external_row(
                published_age=timedelta(days=-1),
                observed_at=NOW + timedelta(days=1),
                suffix="future",
            ),
        )
        self.assertEqual(score_external_spread(rows, publication_time=NOW), 0.0)

    def test_external_score_caps_at_20(self) -> None:
        rows = tuple(
            external_row(
                domain=domain,
                suffix=str(index),
                engagement_count=10_000,
            )
            for index, domain in enumerate(
                ("youtube.com", "reddit.com", "tiktok.com", "twitch.tv")
            )
        )
        self.assertEqual(score_external_spread(rows, publication_time=NOW), 20.0)


class SeoGapScoreTests(unittest.TestCase):
    def test_guide_count_boundaries(self) -> None:
        cases = ((None, 0), (0, 10), (1, 7), (2, 7), (3, 3), (5, 3), (6, 0))
        for count, expected in cases:
            with self.subTest(count=count):
                actual = score_seo_gap(
                    serp(
                        guide_results=count,
                        relevant_nonofficial_results=None,
                        missing_intents=(),
                    ),
                    publication_time=NOW,
                )
                self.assertEqual(actual, expected)

    def test_nonofficial_count_boundaries(self) -> None:
        cases = ((None, 0), (0, 6), (1, 4), (3, 4), (4, 2), (10, 2), (11, 0))
        for count, expected in cases:
            with self.subTest(count=count):
                actual = score_seo_gap(
                    serp(
                        guide_results=None,
                        relevant_nonofficial_results=count,
                        missing_intents=(),
                    ),
                    publication_time=NOW,
                )
                self.assertEqual(actual, expected)

    def test_missing_intent_boundaries_and_total_cap(self) -> None:
        intents = ("guide", "codes", "answers", "wiki")
        for count in range(5):
            with self.subTest(count=count):
                actual = score_seo_gap(
                    serp(
                        guide_results=None,
                        relevant_nonofficial_results=None,
                        missing_intents=intents[:count],
                    ),
                    publication_time=NOW,
                )
                self.assertEqual(actual, float(count))
        self.assertEqual(score_seo_gap(serp(), publication_time=NOW), 20.0)

    def test_missing_counts_contribute_zero_without_reweighting(self) -> None:
        result = score_seo_gap(
            serp(
                guide_results=None,
                relevant_nonofficial_results=0,
                missing_intents=("guide",),
            ),
            publication_time=NOW,
        )
        self.assertEqual(result, 7.0)

    def test_missing_or_stale_serp_scores_zero(self) -> None:
        self.assertEqual(score_seo_gap(None, publication_time=NOW), 0.0)
        stale = serp(observed_at=NOW - timedelta(hours=24, microseconds=1))
        self.assertEqual(score_seo_gap(stale, publication_time=NOW), 0.0)


class ActionTests(unittest.TestCase):
    def test_exact_thresholds(self) -> None:
        cases = (
            (49.9, "skip"),
            (50.0, "watch"),
            (64.9, "watch"),
            (65.0, "worth_content_mvp"),
            (79.9, "worth_content_mvp"),
            (80.0, "immediate_action"),
        )
        for total, expected in cases:
            with self.subTest(total=total):
                self.assertEqual(
                    action_for(
                        total,
                        demand_state="pass",
                        demand_evidence_known=True,
                        serp_evidence_known=True,
                        has_independent_evidence=True,
                    ),
                    expected,
                )

    def test_hard_gate_overrides_total(self) -> None:
        cases = (
            ("fail", "skip"),
            ("early_watch", "watch"),
            ("unknown", "needs_verification"),
        )
        for state, expected in cases:
            with self.subTest(state=state):
                self.assertEqual(
                    action_for(
                        100,
                        demand_state=state,
                        demand_evidence_known=state != "unknown",
                        serp_evidence_known=True,
                        has_independent_evidence=True,
                    ),
                    expected,
                )

    def test_action_uses_the_persisted_one_decimal_total(self) -> None:
        self.assertEqual(
            action_for(
                64.95,
                demand_state="pass",
                demand_evidence_known=True,
                serp_evidence_known=True,
                has_independent_evidence=True,
            ),
            "worth_content_mvp",
        )

    def test_unknown_search_or_serp_blocks_positive_action(self) -> None:
        for demand_known, serp_known in ((False, True), (True, False)):
            with self.subTest(demand_known=demand_known, serp_known=serp_known):
                self.assertEqual(
                    action_for(
                        90,
                        demand_state="pass",
                        demand_evidence_known=demand_known,
                        serp_evidence_known=serp_known,
                        has_independent_evidence=True,
                    ),
                    "needs_verification",
                )

    def test_no_independent_evidence_demotes_immediate_action(self) -> None:
        self.assertEqual(
            action_for(
                90,
                demand_state="pass",
                demand_evidence_known=True,
                serp_evidence_known=True,
                has_independent_evidence=False,
            ),
            "worth_content_mvp",
        )


class OpportunityScoreTests(unittest.TestCase):
    def test_score_opportunity_builds_one_decimal_record(self) -> None:
        independent = external_row(engagement_count=10_000)
        evidence = opportunity_evidence(
            autocomplete=(
                search_row("GeoSlice codes"),
                search_row("GeoSlice guide"),
            ),
            related=(
                search_row("GeoSlice wiki"),
                search_row("GeoSlice answers"),
            ),
            external=(independent,),
            serp_evidence=serp(),
        )

        result = score_opportunity(
            run_id=RUN_ID,
            opportunity_id=OPPORTUNITY_ID,
            game_name="GeoSlice",
            platform_score=29.95,
            evidence=evidence,
            publication_time=NOW,
        )

        self.assertEqual(result.platform_score, 30.0)
        self.assertEqual(result.demand_state, "pass")
        self.assertEqual(result.demand_score, 20.0)
        self.assertEqual(result.external_score, 18.0)
        self.assertEqual(result.seo_score, 20.0)
        self.assertEqual(result.total_score, 88.0)
        self.assertEqual(result.action, "immediate_action")

    def test_unknown_or_missing_serp_blocks_score_opportunity_action(self) -> None:
        for serp_evidence in (
            None,
            serp(guide_results=None),
            serp(relevant_nonofficial_results=None),
            serp(observed_at=NOW - timedelta(days=2)),
        ):
            with self.subTest(serp=serp_evidence):
                result = score_opportunity(
                    run_id=RUN_ID,
                    opportunity_id=OPPORTUNITY_ID,
                    game_name="GeoSlice",
                    platform_score=30,
                    evidence=opportunity_evidence(
                        autocomplete=(search_row("GeoSlice codes"),),
                        external=(external_row(engagement_count=10_000),),
                        serp_evidence=serp_evidence,
                    ),
                    publication_time=NOW,
                )
                self.assertEqual(result.action, "needs_verification")

    def test_score_opportunity_never_lets_total_override_gate(self) -> None:
        cases = (
            ((0, 0, 0), "fail", "skip"),
            ((0, 100, 32, 18), "early_watch", "watch"),
        )
        for values, state, action in cases:
            with self.subTest(values=values):
                result = score_opportunity(
                    run_id=RUN_ID,
                    opportunity_id=OPPORTUNITY_ID,
                    game_name="GeoSlice",
                    platform_score=30,
                    evidence=opportunity_evidence(
                        values,
                        autocomplete=(
                            ()
                            if state == "fail"
                            else (search_row("GeoSlice codes"),)
                        ),
                        external=(external_row(engagement_count=10_000),),
                        serp_evidence=serp(),
                    ),
                    publication_time=NOW,
                )
                self.assertEqual(result.demand_state, state)
                self.assertEqual(result.action, action)

    def test_missing_independent_evidence_blocks_immediate_action(self) -> None:
        result = score_opportunity(
            run_id=RUN_ID,
            opportunity_id=OPPORTUNITY_ID,
            game_name="GeoSlice",
            platform_score=30,
            evidence=opportunity_evidence(
                autocomplete=(
                    search_row("GeoSlice codes"),
                    search_row("GeoSlice guide"),
                ),
                related=(
                    search_row("GeoSlice wiki"),
                    search_row("GeoSlice answers"),
                ),
                serp_evidence=serp(),
            ),
            publication_time=NOW,
        )
        self.assertEqual(result.total_score, 70.0)
        self.assertEqual(result.action, "worth_content_mvp")

    def test_provenance_mismatch_and_invalid_numeric_inputs_fail_closed(self) -> None:
        evidence = opportunity_evidence()
        invalid_values = (True, -0.1, 30.1, float("nan"), float("inf"), 10**400)
        for value in invalid_values:
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    score_opportunity(
                        run_id=RUN_ID,
                        opportunity_id=OPPORTUNITY_ID,
                        game_name="GeoSlice",
                        platform_score=value,
                        evidence=evidence,
                        publication_time=NOW,
                    )
        with self.assertRaises(ValueError):
            score_opportunity(
                run_id="20260831T030000Z-b1c2d3e4",
                opportunity_id=OPPORTUNITY_ID,
                game_name="GeoSlice",
                platform_score=10,
                evidence=evidence,
                publication_time=NOW,
            )

    def test_stable_sort_uses_action_total_demand_platform_name_and_id(self) -> None:
        ids = (
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
            "00000000-0000-4000-8000-000000000003",
            "00000000-0000-4000-8000-000000000004",
            "00000000-0000-4000-8000-000000000005",
            "00000000-0000-4000-8000-000000000006",
        )
        rows = (
            (scored(ids[0], action="worth_content_mvp", total=30), "Zulu"),
            (scored(ids[1], action="immediate_action", total=30), "Zulu"),
            (scored(ids[2], action="immediate_action", total=31), "Zulu"),
            (scored(ids[3], action="immediate_action", total=31, demand=11, platform=9), "Zulu"),
            (scored(ids[4], action="immediate_action", total=31, demand=11, platform=10), "Beta"),
            (scored(ids[5], action="immediate_action", total=31, demand=11, platform=10), "Alpha"),
        )

        ordered = sorted(rows, key=lambda row: opportunity_sort_key(row[0], row[1]))

        self.assertEqual(
            tuple(item[0].opportunity_id for item in ordered),
            (ids[5], ids[4], ids[3], ids[2], ids[1], ids[0]),
        )

    def test_sort_uses_opportunity_id_as_final_tie_break(self) -> None:
        first = "00000000-0000-4000-8000-000000000001"
        second = "00000000-0000-4000-8000-000000000002"
        rows = (
            (scored(second, action="watch", total=30), "same name"),
            (scored(first, action="watch", total=30), "Same Name"),
        )
        ordered = sorted(rows, key=lambda row: opportunity_sort_key(row[0], row[1]))
        self.assertEqual(tuple(row[0].opportunity_id for row in ordered), (first, second))


if __name__ == "__main__":
    unittest.main()
