from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from unified_game_radar.collectors.base import CollectorResult
from unified_game_radar.collectors.itch import parse_itch_envelope
from unified_game_radar.config import RadarConfig
from unified_game_radar.errors import (
    IdempotencyConflictError,
    InputValidationError,
)
from unified_game_radar.orchestration import (
    enrich_run,
    ingest_run,
    new_run_id,
    report_run,
    scan_run,
)
from unified_game_radar.report import FileAtomicWriter
from unified_game_radar.schemas import (
    ExternalEvidence,
    GameIdentity,
    OpportunityEvidence,
    PlatformObservation,
    RadarRun,
    SearchQueryEvidence,
    SerpEvidence,
    SourceHealth,
    TrendEvidence,
    TrendPoint,
)
from unified_game_radar.storage import RadarStore


STARTED_AT = datetime(2026, 8, 31, 8, tzinfo=timezone.utc)
ENRICHED_AT = STARTED_AT + timedelta(minutes=30)
RUN_ID = "20260831T080000Z-11111111"
RUN_SUFFIX_ID = "11111111-1111-4111-8111-111111111111"
IDENTITY_IDS = (
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
    "44444444-4444-4444-8444-444444444444",
    "55555555-5555-4555-8555-555555555555",
    "66666666-6666-4666-8666-666666666666",
)


class SequenceIdFactory:
    def __init__(self, values: tuple[str, ...]) -> None:
        self.values = iter(values)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return next(self.values)


class FixtureCollector:
    def __init__(
        self,
        platform: str,
        rows,
        *,
        status: str = "fresh",
    ) -> None:
        self.platform = platform
        self.rows = rows
        self.status = status

    def collect(self, run: RadarRun) -> CollectorResult:
        return CollectorResult(
            collector=self.platform,
            observations=tuple(self.rows(run)),
            health=SourceHealth(
                schema_version=1,
                run_id=run.run_id,
                collector=self.platform,
                status=self.status,
                observed_at=run.started_at,
                capabilities={"listing": self.status == "fresh"},
                warnings=(),
            ),
            raw_artifacts=(),
            pending_raw_payloads=(),
        )


class CountingWriter:
    def __init__(self) -> None:
        self.delegate = FileAtomicWriter()
        self.calls = 0

    def write_text_at(
        self,
        directory_fd: int,
        filename: str,
        content: str,
    ) -> None:
        self.calls += 1
        self.delegate.write_text_at(directory_fd, filename, content)


def config(root: Path, **changes: object) -> RadarConfig:
    values: dict[str, object] = {
        "data_dir": root / "data",
        "report_dir": root / "reports",
        "preliminary_top_n": 4,
        "enrichment_top_n": 3,
        "final_top_n": 2,
        "heat_floor": 1,
    }
    values.update(changes)
    return RadarConfig(**values)  # type: ignore[arg-type]


def _stamp(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def observation(
    run: RadarRun,
    *,
    platform: str,
    platform_id: str,
    name: str,
    rank: int,
    players: int,
) -> PlatformObservation:
    if platform == "itch":
        surface = "popular"
        provider = "itch_agent_browser"
        url = f"https://studio.itch.io/{platform_id}"
        query_parameters = {"surface_scope": "global"}
        raw_metrics = {
            "title": name,
            "developer": "Shared Studio",
            "game_url": url,
            "browser_playable": True,
            "genre": "Puzzle",
            "is_jam": False,
            "author_release_count": 4,
            "originality": "verified_original",
            "author_non_spam": True,
            "collector_eligible": True,
            "exclusion_reasons": (),
        }
        release_at = None
    elif platform == "steam":
        surface = "most_played"
        provider = "steam_official"
        url = f"https://store.steampowered.com/app/{platform_id}/"
        query_parameters = {"country": "US", "language": "english"}
        raw_metrics = {
            "name": name,
            "developer": "Shared Studio",
            "release_status": "released",
            "store_url": url,
            "metrics": {
                "current_players": {
                    "value": players,
                    "source_id": "steam_current_players",
                    "source_kind": "steam_official",
                    "observed_at": run.started_at.isoformat().replace(
                        "+00:00", "Z"
                    ),
                }
            },
        }
        release_at = run.started_at - timedelta(days=2)
    else:
        surface = "rising"
        provider = "roblox_agent_browser"
        place_id = int(platform_id) + 10_000
        url = f"https://www.roblox.com/games/{place_id}/Signal"
        query_parameters = {
            "surface_scope": "global",
            "cohort_surface": "roblox_global",
        }
        raw_metrics = {
            "universe_id": int(platform_id),
            "place_id": place_id,
            "name": name,
            "developer": "Shared Studio",
            "game_url": url,
            "concurrent_players": players,
            "visits": players * 100,
            "favorites": players * 10,
            "global_cohort_eligible": True,
        }
        release_at = None
    return PlatformObservation(
        schema_version=1,
        observation_id=(
            f"{platform}:{platform_id}:{surface}:{_stamp(run.started_at)}"
        ),
        run_id=run.run_id,
        platform=platform,
        platform_id=platform_id,
        provider=provider,
        surface=surface,
        geo="US",
        locale="en",
        query_parameters=query_parameters,
        metric_definition_version=1,
        observed_at=run.started_at,
        release_at=release_at,
        source_rank=rank,
        raw_metrics=raw_metrics,
        evidence_urls=(url,),
    )


def collectors(*, outage: bool = False) -> dict[str, FixtureCollector]:
    status = "unavailable" if outage else "fresh"
    return {
        "itch": FixtureCollector(
            "itch",
            lambda run: (
                observation(
                    run,
                    platform="itch",
                    platform_id="signal-garden",
                    name="Itch Signal",
                    rank=1,
                    players=0,
                ),
            ),
            status=status,
        ),
        "steam": FixtureCollector(
            "steam",
            lambda run: (
                observation(
                    run,
                    platform="steam",
                    platform_id="101",
                    name="Steam Alpha",
                    rank=1,
                    players=20_000,
                ),
                observation(
                    run,
                    platform="steam",
                    platform_id="102",
                    name="Steam Beta",
                    rank=20,
                    players=2_000,
                ),
            ),
            status=status,
        ),
        "roblox": FixtureCollector(
            "roblox",
            lambda run: (
                observation(
                    run,
                    platform="roblox",
                    platform_id="201",
                    name="Roblox Signal",
                    rank=1,
                    players=15_000,
                ),
            ),
            status=status,
        ),
    }


def evidence(
    run_id: str,
    candidate: GameIdentity,
    values: tuple[float, ...],
    *,
    support: bool,
) -> OpportunityEvidence:
    observed_at = ENRICHED_AT - timedelta(minutes=5)
    days = tuple(date(2026, 8, 24) + timedelta(days=index) for index in range(8))
    points = tuple(
        TrendPoint(date=day, value=value, complete=True)
        for day, value in zip(days[:-1], values)
    ) + (TrendPoint(date=days[-1], value=values[-1], complete=False),)
    query = f"{candidate.name} game"
    autocomplete_rows = (
        SearchQueryEvidence(
            schema_version=1,
            query=f"{candidate.name} game codes",
            observed_at=observed_at,
            source_url="https://www.google.com/complete/search?q=signal",
        ),
        SearchQueryEvidence(
            schema_version=1,
            query=f"{candidate.name} game guide",
            observed_at=observed_at,
            source_url="https://www.google.com/complete/search?q=guide",
        ),
    ) if support else ()
    related_rows = (
        SearchQueryEvidence(
            schema_version=1,
            query=f"{candidate.name} game wiki",
            observed_at=observed_at,
            source_url="https://trends.google.com/trends/explore?q=wiki",
        ),
        SearchQueryEvidence(
            schema_version=1,
            query=f"{candidate.name} game walkthrough",
            observed_at=observed_at,
            source_url="https://trends.google.com/trends/explore?q=walkthrough",
        ),
    ) if support else ()
    external = (
        ExternalEvidence(
            source="youtube.com",
            url=f"https://youtube.com/watch?v={candidate.opportunity_id[:8]}",
            published_at=observed_at - timedelta(days=1),
            observed_at=observed_at,
            author_relation="independent",
            engagement_count=20_000,
            evidence_kind="gameplay",
        ),
        ExternalEvidence(
            source="reddit.com",
            url=f"https://reddit.com/r/games/{candidate.opportunity_id[:8]}",
            published_at=observed_at - timedelta(days=2),
            observed_at=observed_at,
            author_relation="independent",
            engagement_count=5_000,
            evidence_kind="discussion",
        ),
    )
    return OpportunityEvidence(
        schema_version=1,
        run_id=run_id,
        opportunity_id=candidate.opportunity_id,
        observed_at=observed_at,
        trends=TrendEvidence(
            query=query,
            query_type="search_term",
            timeframe="now 7-d",
            geo="US",
            category=0,
            property="web",
            timezone="Asia/Shanghai",
            points=points,
            comparison_term="gpts",
            comparison_average=41,
            evidence_url="https://trends.google.com/trends/explore?q=signal",
            raw_artifact=f"data/raw/{candidate.opportunity_id}.json",
            observed_at=observed_at,
        ),
        autocomplete_queries=autocomplete_rows,
        related_queries=related_rows,
        external_evidence=external,
        serp=SerpEvidence(
            query=query,
            relevant_nonofficial_results=0,
            guide_results=0,
            missing_intents=("guide", "codes", "answers", "wiki"),
            evidence_url="https://www.google.com/search?q=signal+game",
            observed_at=observed_at,
        ),
    )


def table_count(store: RadarStore, table: str, run_id: str) -> int:
    if table not in {"evidence", "scores"}:
        raise ValueError("unsupported table")
    with sqlite3.connect(store.path) as connection:
        row = connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


class FinalOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.config = config(self.root)
        self.store = RadarStore(self.root / "radar.sqlite3")
        self.store.initialize()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def scan_world(
        self,
        *,
        outage: bool = False,
        mode: str = "scheduled",
        publish_daily: bool = True,
    ):
        return scan_run(
            self.config,
            self.store,
            collectors(outage=outage),
            lambda: STARTED_AT,
            SequenceIdFactory((RUN_SUFFIX_ID,) + IDENTITY_IDS),
            ("itch", "steam", "roblox"),
            mode=mode,
            publish_daily=publish_daily,
        )

    def selected_evidence(
        self,
        candidates: tuple[GameIdentity, ...],
    ) -> tuple[OpportunityEvidence, ...]:
        states = (
            ((10, 20, 30, 40, 30, 25, 30), True),
            ((0, 0, 0, 0, 0, 0, 0), False),
            ((0, 0, 0, 100, 32, 18, 20), True),
        )
        return tuple(
            evidence(
                RUN_ID,
                candidates[index],
                values,
                support=support,
            )
            for index, (values, support) in enumerate(states)
        )

    def test_new_run_id_can_be_reused_by_scan_without_consuming_new_identity(self) -> None:
        suffixes = SequenceIdFactory((RUN_SUFFIX_ID,))
        started_at, run_id = new_run_id(lambda: STARTED_AT, suffixes)

        def unexpected_call():
            raise AssertionError("pre-generated scan must not call clock/id_factory")

        result = scan_run(
            self.config,
            self.store,
            {},
            unexpected_call,
            unexpected_call,
            ("itch",),
            started_at=started_at,
            run_id=run_id,
        )

        self.assertEqual(run_id, "20260831T080000Z-11111111")
        self.assertEqual(result.run_id, run_id)
        self.assertEqual(suffixes.calls, 1)
        with self.assertRaisesRegex(ValueError, "together"):
            scan_run(
                self.config,
                self.store,
                {},
                lambda: STARTED_AT,
                SequenceIdFactory((RUN_SUFFIX_ID,)),
                ("itch",),
                started_at=STARTED_AT,
            )
        with self.assertRaisesRegex(ValueError, "timestamp"):
            scan_run(
                self.config,
                self.store,
                {},
                lambda: STARTED_AT,
                SequenceIdFactory((RUN_SUFFIX_ID,)),
                ("itch",),
                started_at=STARTED_AT,
                run_id="20260830T080000Z-11111111",
            )

    def test_rejects_nonexact_evidence_batch_before_any_write(self) -> None:
        result = self.scan_world()
        selected = result.candidates[: self.config.enrichment_top_n]
        batch = self.selected_evidence(selected)

        wrong_run = replace(
            batch[0],
            run_id="20260830T080000Z-99999999",
        )
        with self.assertRaisesRegex(InputValidationError, "run_id"):
            enrich_run(
                self.config,
                self.store,
                result.run_id,
                (wrong_run,) + batch[1:],
                lambda: ENRICHED_AT,
            )
        excluded = result.candidates[self.config.enrichment_top_n]
        wrong_candidate = evidence(
            result.run_id,
            excluded,
            (10, 20, 30, 40, 35, 30, 25),
            support=True,
        )
        with self.assertRaisesRegex(InputValidationError, "exactly"):
            enrich_run(
                self.config,
                self.store,
                result.run_id,
                batch[:-1] + (wrong_candidate,),
                lambda: ENRICHED_AT,
            )

        self.assertEqual(table_count(self.store, "evidence", result.run_id), 0)
        self.assertEqual(table_count(self.store, "scores", result.run_id), 0)

    def test_batch_retry_is_noop_conflicts_atomically_and_enforces_demand_gates(self) -> None:
        result = self.scan_world()
        selected = result.candidates[: self.config.enrichment_top_n]
        batch = self.selected_evidence(selected)
        writer = CountingWriter()

        first = enrich_run(
            self.config,
            self.store,
            result.run_id,
            batch,
            lambda: ENRICHED_AT,
            writer=writer,
        )
        report_bytes = Path(first.report_json).read_bytes()
        first_write_count = writer.calls
        second = enrich_run(
            self.config,
            self.store,
            result.run_id,
            batch,
            lambda: ENRICHED_AT + timedelta(hours=1),
            writer=writer,
        )

        self.assertEqual(second, first)
        self.assertEqual(Path(second.report_json).read_bytes(), report_bytes)
        self.assertGreater(first_write_count, 0)
        self.assertEqual(table_count(self.store, "evidence", result.run_id), 3)
        self.assertEqual(table_count(self.store, "scores", result.run_id), 3)
        scores = tuple(
            self.store.get_score(result.run_id, candidate.opportunity_id)
            for candidate in selected
        )
        self.assertEqual(
            tuple(item.demand_state for item in scores if item is not None),
            ("pass", "fail", "early_watch"),
        )
        self.assertEqual(
            tuple(item.action for item in scores if item is not None)[1:],
            ("skip", "watch"),
        )

        changed = replace(batch[1], observed_at=batch[1].observed_at + timedelta(seconds=1))
        with self.assertRaises(IdempotencyConflictError):
            enrich_run(
                self.config,
                self.store,
                result.run_id,
                batch[:1] + (changed,) + batch[2:],
                lambda: ENRICHED_AT,
            )
        self.assertEqual(table_count(self.store, "evidence", result.run_id), 3)
        self.assertEqual(table_count(self.store, "scores", result.run_id), 3)
        self.assertEqual(Path(first.report_json).read_bytes(), report_bytes)

    def test_one_unified_enrichment_pool_final_top_n_and_daily_publication(self) -> None:
        result = self.scan_world()
        self.assertEqual(len(result.candidates), 4)
        selected = result.candidates[: self.config.enrichment_top_n]
        selected_platforms = {
            record.platform
            for candidate in selected
            for record in candidate.platform_records
        }
        self.assertGreaterEqual(len(selected_platforms), 2)

        manifest = enrich_run(
            self.config,
            self.store,
            result.run_id,
            self.selected_evidence(selected),
            lambda: ENRICHED_AT,
        )

        self.assertEqual(manifest.phase, "final")
        report = json.loads(Path(manifest.report_json).read_text(encoding="utf-8"))
        self.assertEqual(len(report["candidates"]), self.config.final_top_n)
        ordering = tuple(
            (candidate["action"], candidate["total_score"])
            for candidate in report["candidates"]
        )
        self.assertEqual(ordering[0][0], "immediate_action")
        self.assertEqual(ordering[1][0], "watch")
        publication = self.store.get_publication(result.run_id, "final")
        assert publication is not None
        self.assertTrue(publication.advances_daily_latest)
        self.assertTrue((self.config.report_dir / "daily" / "latest.json").is_file())

    def test_source_outage_is_excluded_and_report_remains_preliminary(self) -> None:
        result = self.scan_world(outage=True, publish_daily=False)
        self.assertEqual(result.candidates, ())

        manifest = report_run(
            self.config,
            self.store,
            result.run_id,
            lambda: ENRICHED_AT,
        )

        self.assertEqual(manifest.phase, "preliminary")
        report = json.loads(Path(manifest.report_json).read_text(encoding="utf-8"))
        self.assertEqual(report["candidates"], [])
        self.assertEqual(
            {item["status"] for item in report["source_health"]},
            {"unavailable"},
        )

    def test_preliminary_report_can_change_after_browser_ingest(self) -> None:
        scan_result = scan_run(
            self.config,
            self.store,
            {"steam": collectors()["steam"]},
            lambda: STARTED_AT,
            SequenceIdFactory((RUN_SUFFIX_ID,) + IDENTITY_IDS),
            ("itch", "steam"),
        )
        first = report_run(
            self.config,
            self.store,
            scan_result.run_id,
            lambda: STARTED_AT + timedelta(minutes=1),
        )
        observed_at = STARTED_AT + timedelta(minutes=5)
        envelope = {
            "schema_version": 1,
            "run_id": scan_result.run_id,
            "collector": "itch",
            "geo": "US",
            "locale": "en",
            "metric_definition_version": 1,
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "rows": [
                {
                    "title": "Browser Arrival",
                    "developer": "Browser Studio",
                    "game_url": "https://browser-studio.itch.io/browser-arrival",
                    "surface": "popular",
                    "surface_scope": "global",
                    "rank": 1,
                    "browser_playable": True,
                    "genre": "Puzzle",
                    "is_jam": False,
                    "author_release_count": 3,
                    "originality": "verified_original",
                    "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                    "evidence_url": "https://itch.io/games/top-sellers",
                }
            ],
        }
        ingest_run(
            self.config,
            self.store,
            scan_result.run_id,
            envelope,
            {"itch": parse_itch_envelope},
            lambda: STARTED_AT + timedelta(minutes=10),
            SequenceIdFactory((IDENTITY_IDS[-1],)),
        )

        second = report_run(
            self.config,
            self.store,
            scan_result.run_id,
            lambda: STARTED_AT + timedelta(minutes=15),
        )

        self.assertEqual(first.phase, "preliminary")
        self.assertEqual(second.phase, "preliminary")
        self.assertNotEqual(first.report_json, second.report_json)
        self.assertTrue(Path(first.report_json).is_file())
        self.assertTrue(Path(second.report_json).is_file())
        self.assertIsNone(
            self.store.get_publication(scan_result.run_id, "preliminary")
        )

    def test_final_report_is_rebuilt_deterministically_from_sqlite(self) -> None:
        result = self.scan_world()
        selected = result.candidates[: self.config.enrichment_top_n]
        first = enrich_run(
            self.config,
            self.store,
            result.run_id,
            self.selected_evidence(selected),
            lambda: ENRICHED_AT,
        )
        json_path = Path(first.report_json)
        markdown_path = Path(first.report_markdown)
        expected_json = json_path.read_bytes()
        expected_markdown = markdown_path.read_bytes()
        json_path.unlink()
        markdown_path.unlink()
        self.store.close()
        self.store = RadarStore(self.root / "radar.sqlite3")
        self.store.initialize()

        rebuilt = report_run(
            self.config,
            self.store,
            result.run_id,
            lambda: ENRICHED_AT + timedelta(hours=2),
        )

        self.assertEqual(rebuilt, first)
        self.assertEqual(json_path.read_bytes(), expected_json)
        self.assertEqual(markdown_path.read_bytes(), expected_markdown)


if __name__ == "__main__":
    unittest.main()
