from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from unified_game_radar.errors import (
    IdempotencyConflictError,
    PersistenceError,
)
from unified_game_radar.schemas import (
    GameIdentity,
    OpportunityEvidence,
    PlatformObservation,
    PlatformRecord,
    RadarRun,
)
from unified_game_radar.storage import RadarStore


RUN_ID = "20260831T160000Z-a1b2c3d4"
MISSING_RUN_ID = "20260831T170000Z-b1c2d3e4"
OPPORTUNITY_ID = "0f840f6f-5c62-4ca6-9d53-e0be9ab2740b"
MISSING_OPPORTUNITY_ID = "1f840f6f-5c62-4ca6-9d53-e0be9ab2740b"
CURRENT_AT = datetime(2026, 8, 31, 16, tzinfo=timezone.utc)
MAX_HISTORY_HOURS = 24 * 366 * 100


def compact(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def make_run(
    run_id: str = RUN_ID,
    started_at: datetime = CURRENT_AT,
) -> RadarRun:
    return RadarRun(
        schema_version=1,
        run_id=run_id,
        started_at=started_at,
        mode="scheduled",
        platforms=("steam",),
        publish_daily=False,
    )


def make_record() -> PlatformRecord:
    return PlatformRecord(
        schema_version=1,
        platform="steam",
        platform_id="123456",
        name="Example Game",
        developer="Example Studio",
        official_domain="example.com",
        url="https://store.steampowered.com/app/123456/",
    )


def make_identity() -> GameIdentity:
    return GameIdentity(
        schema_version=1,
        opportunity_id=OPPORTUNITY_ID,
        name="Example Game",
        normalized_name="example game",
        developer="Example Studio",
        official_domain="example.com",
        platform_records=(make_record(),),
    )


def make_observation(
    *,
    run_id: str = RUN_ID,
    observed_at: datetime = CURRENT_AT,
    platform: str = "steam",
    platform_id: str = "123456",
    provider: str = "steamdb",
    surface: str = "trending",
    geo: str = "US",
    locale: str = "en",
    query_parameters: dict[str, object] | None = None,
    metric_definition_version: int = 1,
) -> PlatformObservation:
    return PlatformObservation(
        schema_version=1,
        observation_id=(
            f"{platform}:{platform_id}:{surface}:{compact(observed_at)}"
        ),
        run_id=run_id,
        platform=platform,
        platform_id=platform_id,
        provider=provider,
        surface=surface,
        geo=geo,
        locale=locale,
        query_parameters=(
            query_parameters
            if query_parameters is not None
            else {"sort": "rank", "filters": {"released": True, "tag": "indie"}}
        ),
        metric_definition_version=metric_definition_version,
        observed_at=observed_at,
        release_at=None,
        source_rank=3,
        raw_metrics={"followers": 1200, "reviews": 80},
        evidence_urls=("https://steamdb.info/app/123456/",),
    )


def make_evidence(
    *,
    run_id: str = RUN_ID,
    opportunity_id: str = OPPORTUNITY_ID,
) -> OpportunityEvidence:
    return OpportunityEvidence(
        schema_version=1,
        run_id=run_id,
        opportunity_id=opportunity_id,
        observed_at=CURRENT_AT,
        trends=None,
        autocomplete_queries=(),
        related_queries=(),
        external_evidence=(),
        serp=None,
    )


class RadarStoreHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "radar.sqlite3"
        self.store = RadarStore(self.database_path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def insert_history(self, observation: PlatformObservation) -> None:
        self.store.create_run(
            make_run(observation.run_id, observation.observed_at)
        )
        self.assertTrue(self.store.insert_observation(observation))

    def test_observation_requires_its_existing_owning_run(self) -> None:
        self.store.create_run(make_run())
        observation = make_observation(run_id=MISSING_RUN_ID)

        with self.assertRaises(PersistenceError) as raised:
            self.store.insert_observation(observation)
        self.assertIsInstance(raised.exception.__cause__, sqlite3.IntegrityError)

    def test_identical_observation_retry_is_a_no_op(self) -> None:
        self.store.create_run(make_run())
        observation = make_observation()
        canonically_identical = replace(
            observation,
            query_parameters={
                "filters": {"tag": "indie", "released": True},
                "sort": "rank",
            },
        )

        self.assertTrue(self.store.insert_observation(observation))
        self.assertFalse(self.store.insert_observation(canonically_identical))

    def test_observation_id_conflict_rejects_changed_canonical_payload(self) -> None:
        self.store.create_run(make_run())
        observation = make_observation()

        self.assertTrue(self.store.insert_observation(observation))
        with self.assertRaises(IdempotencyConflictError):
            self.store.insert_observation(replace(observation, source_rank=99))

    def test_evidence_retry_is_a_no_op_and_changed_payload_conflicts(self) -> None:
        self.store.create_run(make_run())
        self.store.upsert_identity(make_identity())
        evidence = make_evidence()

        self.assertTrue(self.store.insert_evidence(evidence))
        self.assertFalse(self.store.insert_evidence(evidence))
        with self.assertRaises(IdempotencyConflictError):
            self.store.insert_evidence(
                replace(evidence, observed_at=CURRENT_AT + timedelta(minutes=1))
            )

    def test_evidence_requires_an_existing_run(self) -> None:
        self.store.upsert_identity(make_identity())

        with self.assertRaises(PersistenceError) as raised:
            self.store.insert_evidence(make_evidence(run_id=MISSING_RUN_ID))
        self.assertIsInstance(raised.exception.__cause__, sqlite3.IntegrityError)

    def test_evidence_requires_an_existing_opportunity(self) -> None:
        self.store.create_run(make_run())

        with self.assertRaises(PersistenceError) as raised:
            self.store.insert_evidence(
                make_evidence(opportunity_id=MISSING_OPPORTUNITY_ID)
            )
        self.assertIsInstance(raised.exception.__cause__, sqlite3.IntegrityError)

    def test_immutable_inserts_participate_in_an_outer_transaction(self) -> None:
        self.store.create_run(make_run())
        self.store.upsert_identity(make_identity())
        observation = make_observation()
        evidence = make_evidence()

        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.store.transaction():
                self.assertTrue(self.store.insert_observation(observation))
                self.assertTrue(self.store.insert_evidence(evidence))
                raise RuntimeError("rollback")

        self.assertTrue(self.store.insert_observation(observation))
        self.assertTrue(self.store.insert_evidence(evidence))

    def test_immutable_inserts_are_durable_without_an_outer_transaction(self) -> None:
        self.store.create_run(make_run())
        self.store.upsert_identity(make_identity())
        observation = make_observation()
        evidence = make_evidence()
        self.assertTrue(self.store.insert_observation(observation))
        self.assertTrue(self.store.insert_evidence(evidence))

        self.store.close()
        self.store.initialize()

        self.assertFalse(self.store.insert_observation(observation))
        self.assertFalse(self.store.insert_evidence(evidence))

    def test_history_requires_all_compatibility_dimensions(self) -> None:
        current = make_observation()
        compatible = replace(
            make_observation(
                run_id="20260830T160000Z-00000001",
                observed_at=CURRENT_AT - timedelta(hours=24),
                query_parameters={
                    "filters": {"tag": "indie", "released": True},
                    "sort": "rank",
                },
            ),
            source_rank=9,
        )
        self.insert_history(compatible)

        self.assertEqual(
            self.store.compatible_observation(current, 24, 6), compatible
        )

        incompatible_values = (
            {"provider": "steam"},
            {"surface": "new_releases"},
            {"geo": "GB"},
            {"locale": "en-GB"},
            {"query_parameters": {"sort": "newest"}},
            {"metric_definition_version": 2},
            {"platform_id": "654321"},
        )
        for changes in incompatible_values:
            with self.subTest(changes=changes):
                changed = make_observation(**changes)
                self.assertIsNone(
                    self.store.compatible_observation(changed, 24, 6)
                )

    def test_history_is_past_only_and_respects_target_window(self) -> None:
        current = make_observation()
        too_recent = make_observation(
            run_id="20260831T000000Z-00000002",
            observed_at=CURRENT_AT - timedelta(hours=16),
        )
        future = make_observation(
            run_id="20260901T160000Z-00000003",
            observed_at=CURRENT_AT + timedelta(hours=24),
        )
        too_old = make_observation(
            run_id="20260830T090000Z-00000004",
            observed_at=CURRENT_AT - timedelta(hours=31),
        )
        for observation in (too_recent, future, too_old):
            self.insert_history(observation)

        self.assertIsNone(self.store.compatible_observation(current, 24, 6))

    def test_intraday_observation_never_fills_one_day_history(self) -> None:
        current = make_observation()
        intraday = make_observation(
            run_id="20260831T040001Z-00000005",
            observed_at=CURRENT_AT - timedelta(hours=11, minutes=59, seconds=59),
        )
        self.insert_history(intraday)

        self.assertIsNone(self.store.compatible_observation(current, 24, 6))

    def test_history_selects_nearest_then_newest_candidate(self) -> None:
        current = make_observation()
        twenty_five_hours = make_observation(
            run_id="20260830T150000Z-00000006",
            observed_at=CURRENT_AT - timedelta(hours=25),
        )
        twenty_three_hours = make_observation(
            run_id="20260830T170000Z-00000007",
            observed_at=CURRENT_AT - timedelta(hours=23),
        )
        farther = make_observation(
            run_id="20260830T180000Z-00000008",
            observed_at=CURRENT_AT - timedelta(hours=22),
        )
        for observation in (twenty_five_hours, twenty_three_hours, farther):
            self.insert_history(observation)

        self.assertEqual(
            self.store.compatible_observation(current, 24, 6),
            twenty_three_hours,
        )

    def test_history_supports_seven_day_target(self) -> None:
        current = make_observation()
        seven_days = make_observation(
            run_id="20260824T160000Z-00000009",
            observed_at=CURRENT_AT - timedelta(hours=168),
        )
        self.insert_history(seven_days)

        self.assertEqual(
            self.store.compatible_observation(current, 168, 24), seven_days
        )

    def test_history_window_arguments_are_bounded_integers(self) -> None:
        current = make_observation()
        invalid = (
            (0, 0),
            (-1, 0),
            (24, -1),
            (24, 24),
            (24, 25),
            (True, 0),
            (24, False),
            (24.0, 6),
            (24, 6.0),
            (MAX_HISTORY_HOURS + 1, 1),
            (24, MAX_HISTORY_HOURS + 1),
        )
        for target_hours, tolerance_hours in invalid:
            with self.subTest(
                target_hours=target_hours,
                tolerance_hours=tolerance_hours,
            ):
                with self.assertRaises(ValueError):
                    self.store.compatible_observation(
                        current,
                        target_hours,  # type: ignore[arg-type]
                        tolerance_hours,  # type: ignore[arg-type]
                    )


if __name__ == "__main__":
    unittest.main()
