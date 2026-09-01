from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from unified_game_radar.collectors.base import CollectorResult
from unified_game_radar.collectors.itch import (
    build_itch_observations,
    parse_itch_envelope,
)
from unified_game_radar.collectors.roblox import parse_roblox_envelope
from unified_game_radar.artifacts import persist_raw_artifact
from unified_game_radar.config import RadarConfig
from unified_game_radar.errors import (
    IdempotencyConflictError,
    InputValidationError,
    PersistenceError,
)
from unified_game_radar.orchestration import ingest_run, scan_run
from unified_game_radar.schemas import (
    GameIdentity,
    OpportunityEvidence,
    PlatformRecord,
    RadarRun,
    ScoredOpportunity,
    SourceHealth,
)
from unified_game_radar.storage import RadarStore


RUN_ID = "20260831T020000Z-a1b2c3d4"
FOREIGN_RUN_ID = "20260831T010000Z-99999999"
STARTED_AT = datetime(2026, 8, 31, 2, tzinfo=timezone.utc)
OBSERVED_AT = datetime(2026, 8, 31, 2, 5, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 31, 2, 10, tzinfo=timezone.utc)
OPPORTUNITY_IDS = (
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
    "44444444-4444-4444-8444-444444444444",
)


class SequenceIdFactory:
    def __init__(self, values: tuple[str, ...] = OPPORTUNITY_IDS) -> None:
        self._values = iter(values)

    def __call__(self) -> str:
        return next(self._values)


def config(root: Path, **changes: object) -> RadarConfig:
    values: dict[str, object] = {
        "data_dir": root / "data",
        "report_dir": root / "reports",
        "preliminary_top_n": 20,
        "enrichment_top_n": 10,
        "final_top_n": 10,
        "heat_floor": 30,
    }
    values.update(changes)
    return RadarConfig(**values)  # type: ignore[arg-type]


def utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def run(
    *,
    run_id: str = RUN_ID,
    started_at: datetime = STARTED_AT,
    platforms: tuple[str, ...] = ("itch", "roblox"),
) -> RadarRun:
    return RadarRun(
        schema_version=1,
        run_id=run_id,
        started_at=started_at,
        mode="manual",
        platforms=platforms,
        publish_daily=False,
    )


def not_run_health(radar_run: RadarRun, platform: str) -> SourceHealth:
    return SourceHealth(
        schema_version=1,
        run_id=radar_run.run_id,
        collector=platform,
        status="not_run",
        observed_at=radar_run.started_at,
        capabilities={"browser_collection": False},
        warnings=(),
    )


def itch_row(
    *,
    slug: str = "signal-garden",
    title: str = "Signal Garden",
    developer: str = "Tiny Studio",
    surface: str = "popular",
    rank: int = 3,
    observed_at: datetime = OBSERVED_AT,
) -> dict[str, object]:
    evidence_url = {
        "newest": "https://itch.io/games/newest",
        "popular": "https://itch.io/games/top-sellers",
    }[surface]
    return {
        "title": title,
        "developer": developer,
        "game_url": f"https://tiny-studio.itch.io/{slug}",
        "surface": surface,
        "surface_scope": "global",
        "rank": rank,
        "browser_playable": True,
        "genre": "Puzzle",
        "is_jam": False,
        "author_release_count": 3,
        "originality": "verified_original",
        "observed_at": utc_text(observed_at),
        "evidence_url": evidence_url,
    }


def itch_envelope(
    rows: list[dict[str, object]] | None = None,
    *,
    run_id: str = RUN_ID,
    observed_at: datetime = OBSERVED_AT,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "collector": "itch",
        "geo": "US",
        "locale": "en",
        "metric_definition_version": 1,
        "observed_at": utc_text(observed_at),
        "rows": [itch_row(observed_at=observed_at)] if rows is None else rows,
    }


def roblox_row(
    *,
    universe_id: int = 1234567890,
    name: str = "Signal Garden",
    developer: str = "Tiny Studio",
    surface: str = "rising",
    rank: int = 3,
    observed_at: datetime = OBSERVED_AT,
) -> dict[str, object]:
    place_id = universe_id + 1000
    evidence_url = {
        "rising": "https://www.roblox.com/charts/top-trending",
        "up-and-coming": "https://www.roblox.com/charts/top-up-and-coming",
        "charts": "https://www.roblox.com/charts/top-playing-now",
    }[surface]
    return {
        "universe_id": universe_id,
        "place_id": place_id,
        "name": name,
        "developer": developer,
        "game_url": f"https://www.roblox.com/games/{place_id}/Signal-Garden",
        "surface": surface,
        "surface_scope": "global",
        "rank": rank,
        "concurrent_players": 10_000,
        "visits": 400_000,
        "favorites": 18_000,
        "observed_at": utc_text(observed_at),
        "evidence_url": evidence_url,
    }


def roblox_envelope(
    rows: list[dict[str, object]] | None = None,
    *,
    run_id: str = RUN_ID,
    observed_at: datetime = OBSERVED_AT,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "collector": "roblox",
        "geo": "US",
        "locale": "en",
        "metric_definition_version": 1,
        "observed_at": utc_text(observed_at),
        "rows": [roblox_row(observed_at=observed_at)] if rows is None else rows,
    }


def parser_registry(calls: list[str] | None = None):
    def parse_itch(value: object, radar_run: RadarRun):
        if calls is not None:
            calls.append("itch")
        return parse_itch_envelope(value, radar_run)

    def parse_roblox(value: object, radar_run: RadarRun):
        if calls is not None:
            calls.append("roblox")
        return parse_roblox_envelope(value, radar_run)

    return {"itch": parse_itch, "roblox": parse_roblox}


def observation_ids(path: Path, run_id: str) -> tuple[str, ...]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT observation_id FROM observations WHERE run_id = ? "
            "ORDER BY observation_id",
            (run_id,),
        ).fetchall()
    return tuple(row[0] for row in rows)


class IngestRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.store = RadarStore(self.root / "radar.sqlite3")
        self.store.initialize()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def create_run(self, radar_run: RadarRun | None = None) -> RadarRun:
        created = radar_run or run()
        self.store.create_run(created)
        for platform in created.platforms:
            if platform in {"itch", "roblox"}:
                self.store.save_source_health(not_run_health(created, platform))
        return created

    def ingest(
        self,
        envelope: dict[str, object],
        *,
        run_id: str = RUN_ID,
        clock=lambda: NOW,
        ids: SequenceIdFactory | None = None,
        radar_config: RadarConfig | None = None,
        registry=None,
    ):
        return ingest_run(
            radar_config or config(self.root),
            self.store,
            run_id,
            envelope,
            registry or parser_registry(),
            clock,
            ids or SequenceIdFactory(),
        )

    def test_dispatches_only_the_declared_parser_and_marks_missing_surface_partial(self) -> None:
        self.create_run()
        calls: list[str] = []

        result = self.ingest(
            itch_envelope(),
            registry=parser_registry(calls),
        )

        self.assertEqual(calls, ["itch"])
        health = self.store.get_source_health(RUN_ID, "itch")
        assert health is not None
        self.assertEqual(health.status, "partial")
        self.assertEqual(health.capabilities, {"newest": False, "popular": True})
        self.assertEqual(
            tuple(item.name for item in result.candidates),
            ("Signal Garden",),
        )
        self.assertEqual(
            tuple(item.collector for item in result.outstanding_tasks),
            ("itch", "roblox"),
        )

    def test_complete_browser_surfaces_change_not_run_health_to_fresh(self) -> None:
        self.create_run()
        rows = [
            itch_row(surface="newest", rank=1),
            itch_row(surface="popular", rank=1),
        ]

        result = self.ingest(itch_envelope(rows))

        health = self.store.get_source_health(RUN_ID, "itch")
        assert health is not None
        self.assertEqual(health.status, "fresh")
        self.assertEqual(health.capabilities, {"newest": True, "popular": True})
        self.assertEqual(
            tuple(item.collector for item in result.outstanding_tasks),
            ("roblox",),
        )

    def test_separate_ingests_accumulate_surfaces_before_health_classification(self) -> None:
        self.create_run()
        ids = SequenceIdFactory()
        first = self.ingest(
            itch_envelope([itch_row(surface="newest", rank=1)]),
            ids=ids,
        )
        second = self.ingest(
            itch_envelope([itch_row(surface="popular", rank=1)]),
            ids=ids,
        )

        first_health = next(
            item for item in first.source_health if item.collector == "itch"
        )
        second_health = next(
            item for item in second.source_health if item.collector == "itch"
        )
        self.assertEqual(first_health.status, "partial")
        self.assertEqual(second_health.status, "fresh")
        self.assertEqual(len(observation_ids(self.store.path, RUN_ID)), 2)
        raw_dir = self.root / "data" / "raw" / RUN_ID / "itch_agent_browser"
        self.assertEqual(
            tuple(path.name for path in sorted(raw_dir.glob("*.json"))),
            (
                "itch_newest_20260831t020500z.json",
                "itch_popular_20260831t020500z.json",
            ),
        )

    def test_browser_ingest_persists_itch_and_roblox_raw_before_observations(self) -> None:
        self.create_run()
        artifacts = []

        def persist(*args, **kwargs):
            artifact = persist_raw_artifact(*args, **kwargs)
            artifacts.append(artifact)
            return artifact

        insert_observation = self.store.insert_observation

        def insert_after_artifact(item) -> None:
            expected = (
                self.root
                / "data"
                / "raw"
                / item.run_id
                / f"{item.platform}_agent_browser"
                / f"{item.platform}_{item.surface}_20260831t020500z.json"
            )
            self.assertTrue(expected.is_file())
            insert_observation(item)

        ids = SequenceIdFactory()
        with (
            mock.patch(
                "unified_game_radar.orchestration.persist_raw_artifact",
                side_effect=persist,
            ),
            mock.patch.object(
                self.store,
                "insert_observation",
                side_effect=insert_after_artifact,
            ),
        ):
            self.ingest(itch_envelope(), ids=ids)
            self.ingest(roblox_envelope(), ids=ids)

        self.assertEqual(
            tuple(artifact.provider for artifact in artifacts),
            ("itch_agent_browser", "roblox_agent_browser"),
        )
        for artifact in artifacts:
            raw_bytes = Path(artifact.path).read_bytes()
            self.assertEqual(
                artifact.sha256,
                hashlib.sha256(raw_bytes).hexdigest(),
            )
            self.assertEqual(
                raw_bytes,
                json.dumps(
                    json.loads(raw_bytes),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8"),
            )

    def test_browser_artifact_failure_writes_no_health_or_observation(self) -> None:
        radar_run = run(platforms=("itch",))
        self.store.create_run(radar_run)

        with (
            mock.patch(
                "unified_game_radar.orchestration.persist_raw_artifact",
                side_effect=PersistenceError("artifact write failed"),
            ),
            self.assertRaises(PersistenceError),
        ):
            self.ingest(itch_envelope())

        self.assertIsNone(self.store.get_source_health(RUN_ID, "itch"))
        self.assertEqual(observation_ids(self.store.path, RUN_ID), ())

    def test_scan_keeps_browser_task_when_collector_health_is_partial(self) -> None:
        class PartialItchCollector:
            def collect(self, radar_run: RadarRun) -> CollectorResult:
                observed_at = radar_run.started_at + timedelta(minutes=1)
                parsed = parse_itch_envelope(
                    itch_envelope(
                        [itch_row(observed_at=observed_at)],
                        run_id=radar_run.run_id,
                        observed_at=observed_at,
                    ),
                    radar_run,
                )
                observations = build_itch_observations(radar_run, parsed)
                return CollectorResult(
                    collector="itch",
                    observations=observations,
                    health=SourceHealth(
                        schema_version=1,
                        run_id=radar_run.run_id,
                        collector="itch",
                        status="partial",
                        observed_at=observed_at,
                        capabilities={"newest": False, "popular": True},
                        warnings=(),
                    ),
                    raw_artifacts=(),
                    pending_raw_payloads=(),
                )

        result = scan_run(
            config(self.root),
            self.store,
            {"itch": PartialItchCollector()},
            lambda: NOW,
            SequenceIdFactory(),
            ("itch",),
        )

        self.assertEqual(result.source_health[0].status, "partial")
        self.assertEqual(
            tuple(item.collector for item in result.outstanding_tasks),
            ("itch",),
        )

    def test_rejects_missing_run_before_parser_dispatch(self) -> None:
        calls: list[str] = []

        with self.assertRaisesRegex(InputValidationError, "not found"):
            self.ingest(itch_envelope(), registry=parser_registry(calls))

        self.assertEqual(calls, [])
        self.assertEqual(observation_ids(self.store.path, RUN_ID), ())

    def test_rejects_envelope_run_mismatch_before_parser_dispatch(self) -> None:
        self.create_run()
        calls: list[str] = []

        with self.assertRaisesRegex(InputValidationError, "run_id"):
            self.ingest(
                itch_envelope(run_id=FOREIGN_RUN_ID),
                registry=parser_registry(calls),
            )

        self.assertEqual(calls, [])
        self.assertEqual(observation_ids(self.store.path, RUN_ID), ())

    def test_rejects_runs_finalized_by_evidence_or_score(self) -> None:
        cases = (
            ("20260831T020000Z-aaaaaaaa", "evidence"),
            ("20260831T020000Z-bbbbbbbb", "score"),
        )
        for index, (run_id, finalizer) in enumerate(cases):
            with self.subTest(finalizer=finalizer):
                radar_run = self.create_run(run(run_id=run_id, platforms=("itch",)))
                opportunity_id = OPPORTUNITY_IDS[index]
                self.store.upsert_identity(
                    GameIdentity(
                        schema_version=1,
                        opportunity_id=opportunity_id,
                        name="Finalized",
                        normalized_name="finalized",
                        developer="Studio",
                        official_domain=None,
                        platform_records=(
                            PlatformRecord(
                                schema_version=1,
                                platform="itch",
                                platform_id=f"finalized-{index}",
                                name="Finalized",
                                developer="Studio",
                                official_domain=None,
                                url=(
                                    f"https://studio.itch.io/finalized-{index}"
                                ),
                            ),
                        ),
                    )
                )
                if finalizer == "evidence":
                    self.store.insert_evidence(
                        OpportunityEvidence(
                            schema_version=1,
                            run_id=run_id,
                            opportunity_id=opportunity_id,
                            observed_at=NOW,
                            trends=None,
                            autocomplete_queries=(),
                            related_queries=(),
                            external_evidence=(),
                            serp=None,
                        )
                    )
                else:
                    self.store.save_score(
                        ScoredOpportunity(
                            schema_version=1,
                            run_id=run_id,
                            opportunity_id=opportunity_id,
                            demand_state="unknown",
                            platform_score=0,
                            demand_score=0,
                            external_score=0,
                            seo_score=0,
                            total_score=0,
                            action="needs_verification",
                            warnings=(),
                        )
                    )
                calls: list[str] = []

                with self.assertRaisesRegex(InputValidationError, "finalized"):
                    self.ingest(
                        itch_envelope(run_id=radar_run.run_id),
                        run_id=radar_run.run_id,
                        registry=parser_registry(calls),
                    )

                self.assertEqual(calls, [])

    def test_identical_retry_is_noop_and_rebuild_is_stable(self) -> None:
        self.create_run()
        rows = [
            itch_row(surface="newest", rank=2),
            itch_row(surface="popular", rank=4),
        ]
        ids = SequenceIdFactory()

        first = self.ingest(itch_envelope(rows), ids=ids)
        first_observations = observation_ids(self.store.path, RUN_ID)
        second = self.ingest(itch_envelope(rows), ids=ids)

        self.assertEqual(second.candidates, first.candidates)
        self.assertEqual(observation_ids(self.store.path, RUN_ID), first_observations)
        self.assertEqual(len(first_observations), 2)

    def test_changed_payload_reusing_observation_id_is_a_conflict(self) -> None:
        self.create_run()
        original = itch_envelope([itch_row(title="Original")])
        self.ingest(original)
        before = observation_ids(self.store.path, RUN_ID)

        with self.assertRaises(IdempotencyConflictError):
            self.ingest(itch_envelope([itch_row(title="Changed")]))

        self.assertEqual(observation_ids(self.store.path, RUN_ID), before)

    def test_conservatively_links_matching_cross_platform_identity(self) -> None:
        self.create_run()
        ids = SequenceIdFactory()
        self.ingest(
            itch_envelope(
                [
                    itch_row(
                        title="Shared Signal",
                        developer="Shared Studio",
                    )
                ]
            ),
            ids=ids,
        )

        result = self.ingest(
            roblox_envelope(
                [
                    roblox_row(
                        name="Shared Signal",
                        developer="Shared Studio",
                    )
                ]
            ),
            ids=ids,
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(
            {(record.platform, record.platform_id) for record in result.candidates[0].platform_records},
            {("itch", "tiny-studio.signal-garden"), ("roblox", "1234567890")},
        )

    def test_itch_and_roblox_share_one_deterministic_global_top_n(self) -> None:
        self.create_run()
        ids = SequenceIdFactory()
        radar_config = config(
            self.root,
            preliminary_top_n=1,
            enrichment_top_n=1,
            final_top_n=1,
        )
        self.ingest(
            itch_envelope(
                [itch_row(slug="winner", title="Itch Winner", rank=1)]
            ),
            ids=ids,
            radar_config=radar_config,
        )
        envelope = roblox_envelope(
            [roblox_row(universe_id=777, name="Roblox Runner", rank=1)]
        )

        first = self.ingest(
            envelope,
            ids=ids,
            radar_config=radar_config,
        )
        second = self.ingest(
            envelope,
            ids=ids,
            radar_config=radar_config,
        )

        self.assertEqual(
            tuple(item.name for item in first.candidates),
            ("Itch Winner",),
        )
        self.assertEqual(second.candidates, first.candidates)

    def test_rebuild_excludes_observations_from_other_runs(self) -> None:
        foreign = run(
            run_id=FOREIGN_RUN_ID,
            started_at=STARTED_AT - timedelta(hours=1),
            platforms=("itch",),
        )
        self.create_run(foreign)
        parsed = parse_itch_envelope(
            itch_envelope(
                [itch_row(slug="foreign", title="Foreign Winner", rank=1)],
                run_id=FOREIGN_RUN_ID,
            ),
            foreign,
        )
        for item in build_itch_observations(foreign, parsed):
            self.store.insert_observation(item)
        self.create_run()

        result = self.ingest(
            roblox_envelope(
                [roblox_row(universe_id=888, name="Current Runner", rank=1)]
            )
        )

        self.assertEqual(
            tuple(item.name for item in result.candidates),
            ("Current Runner",),
        )

    def test_rejects_non_utc_clock_before_parsing_or_persistence(self) -> None:
        self.create_run()
        calls: list[str] = []

        with self.assertRaisesRegex(ValueError, "UTC"):
            self.ingest(
                itch_envelope(),
                clock=lambda: NOW.astimezone(timezone(timedelta(hours=8))),
                registry=parser_registry(calls),
            )

        self.assertEqual(calls, [])
        self.assertEqual(observation_ids(self.store.path, RUN_ID), ())

    def test_rejects_future_browser_observations_before_any_persistence(self) -> None:
        self.create_run()
        future = NOW + timedelta(minutes=1)

        with self.assertRaisesRegex(InputValidationError, "future"):
            self.ingest(
                itch_envelope(
                    [itch_row(observed_at=future)],
                    observed_at=future,
                )
            )

        health = self.store.get_source_health(RUN_ID, "itch")
        assert health is not None
        self.assertEqual(health.status, "not_run")
        self.assertEqual(observation_ids(self.store.path, RUN_ID), ())

    def test_rejects_future_browser_envelope_even_when_rows_are_empty(self) -> None:
        self.create_run()
        future = NOW + timedelta(minutes=1)

        with self.assertRaisesRegex(InputValidationError, "future"):
            self.ingest(itch_envelope([], observed_at=future))

        health = self.store.get_source_health(RUN_ID, "itch")
        assert health is not None
        self.assertEqual(health.status, "not_run")
        self.assertEqual(observation_ids(self.store.path, RUN_ID), ())

    def test_rejects_clock_before_run_start_even_for_empty_envelope(self) -> None:
        self.create_run()
        calls: list[str] = []

        with self.assertRaisesRegex(InputValidationError, "run started_at"):
            self.ingest(
                itch_envelope([]),
                clock=lambda: STARTED_AT - timedelta(seconds=1),
                registry=parser_registry(calls),
            )

        self.assertEqual(calls, [])
        health = self.store.get_source_health(RUN_ID, "itch")
        assert health is not None
        self.assertEqual(health.status, "not_run")
        self.assertEqual(observation_ids(self.store.path, RUN_ID), ())

    def test_preserves_strict_row_and_envelope_timestamp_validation(self) -> None:
        self.create_run()
        mismatched = itch_envelope(
            [itch_row(observed_at=OBSERVED_AT + timedelta(minutes=1))]
        )

        with self.assertRaises(InputValidationError):
            self.ingest(mismatched)

        health = self.store.get_source_health(RUN_ID, "itch")
        assert health is not None
        self.assertEqual(health.status, "not_run")
        self.assertEqual(observation_ids(self.store.path, RUN_ID), ())


if __name__ == "__main__":
    unittest.main()
