from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from unified_game_radar.demand import (
    aggregate_daily_means,
    classify_demand,
    completed_points,
    has_second_wave,
    is_single_spike,
    is_unambiguous_game_query,
)
from unified_game_radar.errors import InputValidationError
from unified_game_radar.schemas import (
    OpportunityEvidence,
    SearchQueryEvidence,
    TrendEvidence,
    TrendPoint,
)


RUN_ID = "20260831T020000Z-a1b2c3d4"
OPPORTUNITY_ID = "0f840f6f-5c62-4ca6-9d53-e0be9ab2740b"
NOW = datetime(2026, 8, 31, 6, tzinfo=timezone.utc)


def suggestion(
    query: str = "GeoSlice game codes",
    *,
    observed_at: datetime = NOW,
) -> SearchQueryEvidence:
    return SearchQueryEvidence(
        schema_version=1,
        query=query,
        observed_at=observed_at,
        source_url="https://www.google.com/complete/search?q=geoslice",
    )


def evidence(
    values: tuple[float | None, ...] = (10, 20, 15),
    *,
    complete: tuple[bool, ...] | None = None,
    game_name: str = "GeoSlice",
    trends_query: str | None = None,
    autocomplete: tuple[SearchQueryEvidence, ...] = (),
    related: tuple[SearchQueryEvidence, ...] = (),
    observed_at: datetime = NOW,
    trends_observed_at: datetime | None = None,
    raw_artifact: str | None = "data/unified-game-radar/raw/trends.json",
    category: int = 0,
    timeframe: str = "now 7-d",
    latest_date: date | None = None,
) -> OpportunityEvidence:
    flags = complete or tuple(True for _ in values)
    if len(flags) != len(values):
        raise ValueError("complete flags must match values")
    first_day = (latest_date or date(2026, 8, 30)) - timedelta(
        days=len(values) - 1
    )
    points = tuple(
        TrendPoint(
            date=first_day + timedelta(days=index),
            value=value,
            complete=flags[index],
        )
        for index, value in enumerate(values)
    )
    trends = TrendEvidence(
        query=trends_query or f"{game_name} game",
        query_type="search_term",
        timeframe=timeframe,
        geo="US",
        category=category,
        property="web",
        timezone="UTC",
        points=points,
        comparison_term="gpts",
        comparison_average=41,
        evidence_url="https://trends.google.com/trends/explore?q=geoslice",
        raw_artifact=raw_artifact,
        observed_at=trends_observed_at or observed_at,
    )
    return OpportunityEvidence(
        schema_version=1,
        run_id=RUN_ID,
        opportunity_id=OPPORTUNITY_ID,
        observed_at=observed_at,
        trends=trends,
        autocomplete_queries=autocomplete,
        related_queries=related,
        external_evidence=(),
        serp=None,
    )


class DailyAggregationTests(unittest.TestCase):
    def test_hourly_points_become_local_daily_means_and_current_day_is_incomplete(self) -> None:
        points = (
            (datetime(2026, 8, 31, 5, tzinfo=timezone.utc), 10),
            (datetime(2026, 8, 31, 6, tzinfo=timezone.utc), 30),
            (datetime(2026, 8, 31, 8, tzinfo=timezone.utc), 60),
        )

        result = aggregate_daily_means(
            points,
            timezone_name="America/Los_Angeles",
            publication_time=datetime(2026, 8, 31, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(
            result,
            (
                TrendPoint(date=date(2026, 8, 30), value=20, complete=True),
                TrendPoint(date=date(2026, 8, 31), value=60, complete=False),
            ),
        )

    def test_daily_mean_ignores_missing_samples_but_preserves_all_missing_day(self) -> None:
        result = aggregate_daily_means(
            (
                (datetime(2026, 8, 29, 1, tzinfo=timezone.utc), None),
                (datetime(2026, 8, 29, 2, tzinfo=timezone.utc), 40),
                (datetime(2026, 8, 30, 1, tzinfo=timezone.utc), None),
            ),
            timezone_name="UTC",
            publication_time=NOW,
        )

        self.assertEqual(result[0].value, 40)
        self.assertIsNone(result[1].value)
        self.assertTrue(all(point.complete for point in result))

    def test_hourly_input_requires_aware_utc_timestamps_and_bounded_values(self) -> None:
        invalid_rows = (
            ((datetime(2026, 8, 30, 1), 10),),
            ((datetime(2026, 8, 30, 1, tzinfo=timezone(timedelta(hours=1))), 10),),
            ((datetime(2026, 8, 30, 1, tzinfo=timezone.utc), True),),
            ((datetime(2026, 8, 30, 1, tzinfo=timezone.utc), 101),),
        )
        for rows in invalid_rows:
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    aggregate_daily_means(
                        rows,
                        timezone_name="UTC",
                        publication_time=NOW,
                    )

    def test_completed_points_excludes_declared_incomplete_and_current_local_day(self) -> None:
        trends = evidence(
            (10, 20, 30),
            complete=(True, False, True),
        ).trends
        assert trends is not None
        publication_time = datetime(2026, 8, 30, 23, tzinfo=timezone.utc)

        result = completed_points(trends, publication_time=publication_time)

        self.assertEqual(tuple(point.value for point in result), (10,))


class EvidenceContractTests(unittest.TestCase):
    def test_publication_time_is_required_even_for_fresh_or_old_evidence(self) -> None:
        cases = (
            evidence(
                autocomplete=(suggestion(),),
            ),
            evidence(
                observed_at=NOW - timedelta(days=30),
                trends_observed_at=NOW - timedelta(days=30),
                autocomplete=(
                    suggestion(observed_at=NOW - timedelta(days=30)),
                ),
            ),
        )

        for item in cases:
            with self.subTest(observed_at=item.observed_at):
                result = classify_demand(item, game_name="GeoSlice")

                self.assertEqual(result.state, "unknown")
                self.assertEqual(result.reason, "missing_publication_time")

    def test_search_collections_require_typed_rows_not_loose_strings(self) -> None:
        with self.assertRaises(InputValidationError):
            OpportunityEvidence(
                schema_version=1,
                run_id=RUN_ID,
                opportunity_id=OPPORTUNITY_ID,
                observed_at=NOW,
                trends=None,
                autocomplete_queries=("GeoSlice codes",),
                related_queries=(),
                external_evidence=(),
                serp=None,
            )

    def test_search_query_rejects_unexpected_keys_missing_timestamp_and_non_https(self) -> None:
        valid = {
            "schema_version": 1,
            "query": "GeoSlice game codes",
            "observed_at": "2026-08-31T06:00:00Z",
            "source_url": "https://www.google.com/complete/search?q=geoslice",
        }
        cases = []
        unexpected = dict(valid, score=100)
        cases.append(unexpected)
        missing = dict(valid)
        del missing["observed_at"]
        cases.append(missing)
        non_https = dict(valid, source_url="http://www.google.com/complete")
        cases.append(non_https)
        non_utc = dict(valid, observed_at="2026-08-31T06:00:00+00:00")
        cases.append(non_utc)

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(InputValidationError):
                    SearchQueryEvidence.from_dict(payload)

    def test_trends_contract_preserves_query_metadata_and_raw_artifact(self) -> None:
        trends = evidence().trends
        assert trends is not None

        self.assertEqual(trends.query_type, "search_term")
        self.assertEqual(trends.category, 0)
        self.assertEqual(trends.property, "web")
        self.assertEqual(
            trends.raw_artifact,
            "data/unified-game-radar/raw/trends.json",
        )

    def test_missing_raw_artifact_or_wrong_category_is_unknown(self) -> None:
        for item in (
            evidence(raw_artifact=None, autocomplete=(suggestion(),)),
            evidence(category=8, autocomplete=(suggestion(),)),
        ):
            with self.subTest(item=item):
                result = classify_demand(
                    item,
                    game_name="GeoSlice",
                    publication_time=NOW,
                )
                self.assertEqual(result.state, "unknown")

    def test_nested_stale_or_future_positive_claim_is_unknown(self) -> None:
        stale = NOW - timedelta(hours=24, microseconds=1)
        future = NOW + timedelta(microseconds=1)
        cases = (
            evidence(
                autocomplete=(suggestion(observed_at=stale),),
            ),
            evidence(
                trends_observed_at=future,
                autocomplete=(suggestion(),),
            ),
        )
        for item in cases:
            with self.subTest(item=item):
                self.assertEqual(
                    classify_demand(
                        item,
                        game_name="GeoSlice",
                        publication_time=NOW,
                    ).state,
                    "unknown",
                )

    def test_wrong_trends_timeframe_is_unknown(self) -> None:
        result = classify_demand(
            evidence(
                timeframe="today 1-m",
                autocomplete=(suggestion(),),
            ),
            game_name="GeoSlice",
            publication_time=NOW,
        )

        self.assertEqual(result.state, "unknown")
        self.assertEqual(result.reason, "invalid_trends_window")

    def test_fresh_envelope_with_ancient_trends_dates_is_unknown(self) -> None:
        result = classify_demand(
            evidence(
                latest_date=date(2020, 1, 7),
                autocomplete=(suggestion(),),
            ),
            game_name="GeoSlice",
            publication_time=NOW,
        )

        self.assertEqual(result.state, "unknown")
        self.assertEqual(result.reason, "invalid_trends_window")

    def test_trends_window_must_include_latest_completed_local_day(self) -> None:
        result = classify_demand(
            evidence(
                latest_date=date(2026, 8, 29),
                autocomplete=(suggestion(),),
            ),
            game_name="GeoSlice",
            publication_time=NOW,
        )

        self.assertEqual(result.state, "unknown")
        self.assertEqual(result.reason, "invalid_trends_window")

    def test_now_seven_day_window_calendar_boundaries_are_enforced(self) -> None:
        at_lower_boundary = evidence(
            (10, 20, 15, 20, 15, 20, 15),
            autocomplete=(suggestion(),),
        )
        before_lower_boundary = evidence(
            (10, 20, 15, 20, 15, 20, 15, 20),
            autocomplete=(suggestion(),),
        )
        future_point = evidence(
            (10, 20, 15),
            latest_date=date(2026, 9, 1),
            autocomplete=(suggestion(),),
        )

        self.assertEqual(
            classify_demand(
                at_lower_boundary,
                game_name="GeoSlice",
                publication_time=NOW,
            ).state,
            "pass",
        )
        too_old = classify_demand(
            before_lower_boundary,
            game_name="GeoSlice",
            publication_time=NOW,
        )
        self.assertEqual(too_old.state, "unknown")
        self.assertEqual(too_old.reason, "invalid_trends_window")
        future = classify_demand(
            future_point,
            game_name="GeoSlice",
            publication_time=NOW,
        )
        self.assertEqual(future.state, "unknown")
        self.assertEqual(future.reason, "future_trends_date")


class QueryDisambiguationTests(unittest.TestCase):
    def test_exact_game_intent_disambiguates_name_collision(self) -> None:
        self.assertTrue(is_unambiguous_game_query("Atlas", "Atlas game"))
        self.assertTrue(is_unambiguous_game_query("ATLAS", "atlas   game"))
        self.assertFalse(is_unambiguous_game_query("Atlas", "Atlas"))
        self.assertFalse(is_unambiguous_game_query("Atlas", "Atlas Earth game"))

    def test_invalid_or_empty_query_text_is_not_unambiguous(self) -> None:
        self.assertFalse(is_unambiguous_game_query("Atlas", ""))
        self.assertFalse(is_unambiguous_game_query("", "Atlas game"))

    def test_ambiguous_query_is_unknown_before_zero_demand_can_fail(self) -> None:
        result = classify_demand(
            evidence(
                (0, 0, 0),
                game_name="Atlas",
                trends_query="Atlas",
            ),
            game_name="Atlas",
            publication_time=NOW,
        )

        self.assertEqual(result.state, "unknown")


class OrderedDemandGateTests(unittest.TestCase):
    def classify(
        self,
        item: OpportunityEvidence | None,
        *,
        game_name: str = "GeoSlice",
        publication_time: datetime = NOW,
    ) -> str:
        return classify_demand(
            item,
            game_name=game_name,
            publication_time=publication_time,
        ).state

    def test_missing_or_stale_evidence_is_unknown(self) -> None:
        self.assertEqual(self.classify(None), "unknown")
        self.assertEqual(
            self.classify(
                evidence(observed_at=NOW - timedelta(hours=24, microseconds=1))
            ),
            "unknown",
        )

    def test_exactly_24_hour_old_evidence_remains_fresh(self) -> None:
        observed_at = NOW - timedelta(hours=24)
        item = evidence(
            (10, 20, 15),
            observed_at=observed_at,
            autocomplete=(suggestion(observed_at=observed_at),),
        )

        self.assertEqual(self.classify(item), "pass")

    def test_airlinia_and_meltspell_all_zero_without_support_fail(self) -> None:
        for name in ("Airlinia", "Meltspell"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.classify(
                        evidence((0, 0, 0), game_name=name),
                        game_name=name,
                    ),
                    "fail",
                )

    def test_geoslice_one_wave_decline_is_early_watch_even_with_support(self) -> None:
        item = evidence(
            (0, 0, 100, 32, 18),
            autocomplete=(suggestion(),),
        )

        self.assertEqual(self.classify(item), "early_watch")

    def test_positive_incomplete_current_day_spike_is_early_watch_not_fail(self) -> None:
        item = evidence(
            (0, 0, 100),
            complete=(True, True, False),
            latest_date=date(2026, 8, 31),
        )

        self.assertEqual(self.classify(item), "early_watch")

    def test_two_nonzero_days_without_support_or_second_wave_are_early_watch(self) -> None:
        self.assertEqual(self.classify(evidence((10, 30))), "early_watch")

    def test_sustained_demand_with_typed_autocomplete_passes(self) -> None:
        item = evidence(
            (10, 20, 15),
            autocomplete=(suggestion(),),
        )

        self.assertEqual(self.classify(item), "pass")

    def test_sustained_demand_with_related_query_passes(self) -> None:
        item = evidence(
            (10, 20, 15),
            related=(suggestion("GeoSlice wiki"),),
        )

        self.assertEqual(self.classify(item), "pass")

    def test_irrelevant_fresh_query_does_not_poison_relevant_support(self) -> None:
        item = evidence(
            (10, 20, 15),
            autocomplete=(
                suggestion("GeoSlice game codes"),
                suggestion("GeoSlice soundtrack"),
            ),
        )

        self.assertEqual(self.classify(item), "pass")

    def test_irrelevant_only_queries_follow_missing_support_rules(self) -> None:
        item = evidence(
            (10, 20, 15),
            autocomplete=(suggestion("GeoSlice soundtrack"),),
        )

        self.assertEqual(self.classify(item), "early_watch")

    def test_stale_irrelevant_query_still_invalidates_evidence_bundle(self) -> None:
        item = evidence(
            (10, 20, 15),
            autocomplete=(
                suggestion(
                    "GeoSlice soundtrack",
                    observed_at=NOW - timedelta(hours=24, microseconds=1),
                ),
            ),
        )

        self.assertEqual(self.classify(item), "unknown")

    def test_verified_second_wave_can_replace_query_support(self) -> None:
        item = evidence((10, 100, 20, 60))

        self.assertEqual(self.classify(item), "pass")

    def test_latest_completed_below_30_percent_is_early_watch(self) -> None:
        item = evidence(
            (20, 100, 60, 29),
            autocomplete=(suggestion(),),
        )

        self.assertEqual(self.classify(item), "early_watch")

    def test_single_spike_boundary_is_inclusive_at_twice_second_highest(self) -> None:
        points = (50, 100, 20, 10)

        self.assertTrue(is_single_spike(points))
        self.assertEqual(
            self.classify(
                evidence(points, autocomplete=(suggestion(),))
            ),
            "early_watch",
        )

    def test_single_spike_requires_postpeak_strictly_below_40_percent(self) -> None:
        points = (0, 100, 40, 30)

        self.assertFalse(is_single_spike(points))
        self.assertEqual(
            self.classify(
                evidence(points, autocomplete=(suggestion(),))
            ),
            "pass",
        )

    def test_wave_helpers_reject_missing_completed_values_consistently(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be null"):
            is_single_spike((100, None, 10))  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "must not be null"):
            has_second_wave((100, None, 50))  # type: ignore[arg-type]

    def test_later_local_maximum_exactly_50_percent_is_second_wave(self) -> None:
        points = (0, 100, 20, 50, 30)

        self.assertTrue(has_second_wave(points))
        self.assertEqual(self.classify(evidence(points)), "pass")

    def test_latest_retention_exactly_30_percent_passes_with_support(self) -> None:
        item = evidence(
            (20, 100, 30),
            autocomplete=(suggestion(),),
        )

        self.assertEqual(self.classify(item), "pass")

    def test_latest_zero_without_support_fails_but_with_support_watches(self) -> None:
        self.assertEqual(self.classify(evidence((10, 20, 0))), "fail")
        self.assertEqual(
            self.classify(
                evidence(
                    (10, 20, 0),
                    autocomplete=(suggestion(),),
                )
            ),
            "early_watch",
        )

    def test_all_zero_with_support_is_early_watch(self) -> None:
        item = evidence(
            (0, 0, 0),
            autocomplete=(suggestion(),),
        )

        self.assertEqual(self.classify(item), "early_watch")

    def test_missing_completed_value_is_unknown(self) -> None:
        item = evidence(
            (10, None, 20),
            autocomplete=(suggestion(),),
        )

        self.assertEqual(self.classify(item), "unknown")


if __name__ == "__main__":
    unittest.main()
