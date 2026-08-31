from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from unified_game_radar.collectors.base import (
    CollectorResult,
    PendingRawPayload,
)
from unified_game_radar.config import RadarConfig
from unified_game_radar.orchestration import scan_run
from unified_game_radar.schemas import (
    PlatformObservation,
    RadarRun,
    SourceHealth,
)
from unified_game_radar.storage import RadarStore


NOW = datetime(2026, 8, 31, 16, tzinfo=timezone.utc)
PRIOR_RUN_ID = "20260830T160000Z-99999999"
IDS = (
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
    "44444444-4444-4444-8444-444444444444",
    "55555555-5555-4555-8555-555555555555",
    "66666666-6666-4666-8666-666666666666",
)


class SequenceIdFactory:
    def __init__(self, values: tuple[str, ...] = IDS) -> None:
        self._values = iter(values)
        self.calls: list[str] = []

    def __call__(self) -> str:
        value = next(self._values)
        self.calls.append(value)
        return value


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


def compact(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def observation(
    run: RadarRun,
    *,
    platform: str,
    platform_id: str,
    name: str,
    developer: str,
    surface: str,
    rank: int,
    observed_at: datetime | None = None,
    raw_metrics: dict[str, object] | None = None,
    query_parameters: dict[str, object] | None = None,
    release_at: datetime | None = None,
) -> PlatformObservation:
    instant = observed_at or run.started_at
    if platform == "steam":
        url = f"https://store.steampowered.com/app/{platform_id}/"
        provider = "steam_official"
        metrics = {
            "name": name,
            "developer": developer,
            "release_status": "released",
            "store_url": url,
            "metrics": {
                "current_players": {
                    "value": 1_000,
                    "source_id": "steam_current_players",
                    "source_kind": "steam_official",
                    "observed_at": instant.isoformat().replace("+00:00", "Z"),
                }
            },
        }
        parameters = {"country": "US", "language": "english"}
    elif platform == "itch":
        url = f"https://studio-{platform_id}.itch.io/{platform_id}"
        provider = "itch_agent_browser"
        metrics = {
            "title": name,
            "developer": developer,
            "game_url": url,
            "browser_playable": True,
            "genre": "Puzzle",
            "is_jam": False,
            "author_release_count": 3,
            "originality": "verified_original",
            "author_non_spam": True,
            "collector_eligible": True,
            "exclusion_reasons": (),
        }
        parameters = {"surface_scope": "global"}
    else:
        place_id = int(platform_id) + 10_000
        url = f"https://www.roblox.com/games/{place_id}/{name.replace(' ', '-')}"
        provider = "roblox_agent_browser"
        metrics = {
            "universe_id": int(platform_id),
            "place_id": place_id,
            "name": name,
            "developer": developer,
            "game_url": url,
            "concurrent_players": 1_000,
            "visits": 50_000,
            "favorites": 2_000,
            "global_cohort_eligible": True,
        }
        parameters = {
            "surface_scope": "global",
            "cohort_surface": "roblox_global",
        }
    if raw_metrics is not None:
        metrics.update(raw_metrics)
    if query_parameters is not None:
        parameters = query_parameters
    return PlatformObservation(
        schema_version=1,
        observation_id=(
            f"{platform}:{platform_id}:{surface}:{compact(instant)}"
        ),
        run_id=run.run_id,
        platform=platform,
        platform_id=platform_id,
        provider=provider,
        surface=surface,
        geo="US",
        locale="en",
        query_parameters=parameters,
        metric_definition_version=1,
        observed_at=instant,
        release_at=release_at,
        source_rank=rank,
        raw_metrics=metrics,
        evidence_urls=(url,),
    )


class FixtureCollector:
    def __init__(
        self,
        platform: str,
        build_rows,
        *,
        pending: bool = False,
        failure: Exception | None = None,
        health_status: str = "fresh",
    ) -> None:
        self.platform = platform
        self.build_rows = build_rows
        self.pending = pending
        self.failure = failure
        self.health_status = health_status
        self.calls: list[RadarRun] = []

    def collect(self, run: RadarRun) -> CollectorResult:
        self.calls.append(run)
        if self.failure is not None:
            raise self.failure
        rows = tuple(self.build_rows(run))
        health = SourceHealth(
            schema_version=1,
            run_id=run.run_id,
            collector=self.platform,
            status=self.health_status,
            observed_at=run.started_at,
            capabilities={"listing": self.health_status == "fresh"},
            warnings=(),
        )
        pending = (
            PendingRawPayload(
                run_id=run.run_id,
                provider=f"{self.platform}_fixture",
                artifact_name=f"{self.platform}_fixture.json",
                observed_at=run.started_at,
                payload={"authorization": "must-not-be-rendered", "rows": [1]},
            ),
        ) if self.pending else ()
        return CollectorResult(
            collector=self.platform,
            observations=rows,
            health=health,
            raw_artifacts=(),
            pending_raw_payloads=pending,
        )


def stored_identities(path: Path) -> tuple[dict[str, object], ...]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT canonical_json FROM game_identities ORDER BY opportunity_id"
        ).fetchall()
    return tuple(json.loads(row[0]) for row in rows)


class ScanRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.store = RadarStore(self.root / "radar.sqlite3")
        self.store.initialize()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def test_creates_deterministic_run_filters_collectors_and_returns_browser_task(self) -> None:
        steam = FixtureCollector(
            "steam",
            lambda run: (
                observation(
                    run,
                    platform="steam",
                    platform_id="10",
                    name="Steam Signal",
                    developer="Studio S",
                    surface="most_played",
                    rank=10,
                    release_at=NOW - timedelta(days=2),
                ),
            ),
            pending=True,
        )
        unselected = FixtureCollector("roblox", lambda run: ())
        ids = SequenceIdFactory()

        result = scan_run(
            config(self.root),
            self.store,
            {"steam": steam, "roblox": unselected},
            lambda: NOW,
            ids,
            ("steam", "itch"),
        )

        self.assertEqual(result.run_id, "20260831T160000Z-11111111")
        persisted = self.store.get_run(result.run_id)
        assert persisted is not None
        self.assertEqual(persisted.mode, "manual")
        self.assertEqual(persisted.platforms, ("itch", "steam"))
        self.assertFalse(persisted.publish_daily)
        self.assertEqual(len(steam.calls), 1)
        self.assertEqual(unselected.calls, [])
        self.assertEqual(
            tuple((health.collector, health.status) for health in result.source_health),
            (("itch", "not_run"), ("steam", "fresh")),
        )
        self.assertEqual(
            tuple((task.collector, task.surface) for task in result.outstanding_tasks),
            (("itch", "itch_discovery"),),
        )
        self.assertEqual(
            result.outstanding_tasks[0].collection_contract["required_surfaces"],
            ("newest", "popular"),
        )
        self.assertEqual(len(result.candidates), 1)
        self.assertNotIn("must-not-be-rendered", repr(steam.calls[0]))

    def test_collector_failures_are_isolated_and_use_stale_fallback_when_available(self) -> None:
        prior_at = NOW - timedelta(hours=48)
        prior = RadarRun(
            schema_version=1,
            run_id="20260829T160000Z-99999999",
            started_at=prior_at,
            mode="scheduled",
            platforms=("steam",),
            publish_daily=False,
        )
        self.store.create_run(prior)
        self.store.insert_observation(
            observation(
                prior,
                platform="steam",
                platform_id="10",
                name="Old Steam",
                developer="Studio",
                surface="most_played",
                rank=20,
                observed_at=prior_at,
            )
        )
        itch = FixtureCollector(
            "itch",
            lambda run: (
                observation(
                    run,
                    platform="itch",
                    platform_id="bright",
                    name="Bright Signal",
                    developer="Studio I",
                    surface="popular",
                    rank=10,
                ),
            ),
        )
        steam = FixtureCollector("steam", lambda run: (), failure=RuntimeError("secret"))
        roblox = FixtureCollector("roblox", lambda run: (), failure=RuntimeError("secret"))

        result = scan_run(
            config(self.root),
            self.store,
            {"itch": itch, "steam": steam, "roblox": roblox},
            lambda: NOW,
            SequenceIdFactory(),
            ("itch", "steam", "roblox"),
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(
            {health.collector: health.status for health in result.source_health},
            {"itch": "fresh", "steam": "stale", "roblox": "unavailable"},
        )
        self.assertEqual(
            tuple(warning.code for warning in result.warnings),
            ("collector_failed", "collector_failed"),
        )
        self.assertTrue(all("secret" not in warning.message for warning in result.warnings))
        self.assertIsNotNone(self.store.get_source_health(result.run_id, "steam"))
        self.assertIsNotNone(self.store.get_source_health(result.run_id, "roblox"))

    def test_stale_and_unavailable_current_rows_do_not_enter_candidates(self) -> None:
        itch = FixtureCollector(
            "itch",
            lambda run: (
                observation(
                    run,
                    platform="itch",
                    platform_id="stale",
                    name="Stale Itch",
                    developer="Studio I",
                    surface="popular",
                    rank=1,
                ),
            ),
            health_status="stale",
        )
        roblox = FixtureCollector(
            "roblox",
            lambda run: (
                observation(
                    run,
                    platform="roblox",
                    platform_id="90",
                    name="Unavailable Roblox",
                    developer="Studio R",
                    surface="rising",
                    rank=1,
                ),
            ),
            health_status="unavailable",
        )

        result = scan_run(
            config(self.root),
            self.store,
            {"itch": itch, "roblox": roblox},
            lambda: NOW,
            SequenceIdFactory(),
            ("itch", "roblox"),
        )

        self.assertEqual(result.candidates, ())
        self.assertEqual(stored_identities(self.store.path), ())
        self.assertEqual(
            tuple(item.collector for item in result.outstanding_tasks),
            ("itch", "roblox"),
        )

    def test_partial_platform_keeps_successful_surface_candidate(self) -> None:
        itch = FixtureCollector(
            "itch",
            lambda run: (
                observation(
                    run,
                    platform="itch",
                    platform_id="partial",
                    name="Partial Itch",
                    developer="Studio I",
                    surface="popular",
                    rank=1,
                ),
            ),
            health_status="partial",
        )

        result = scan_run(
            config(self.root),
            self.store,
            {"itch": itch},
            lambda: NOW,
            SequenceIdFactory(),
            ("itch",),
        )

        self.assertEqual(
            tuple(item.name for item in result.candidates),
            ("Partial Itch",),
        )
        self.assertEqual(
            tuple(item.collector for item in result.outstanding_tasks),
            ("itch",),
        )

    def test_links_identities_before_one_global_top_n_and_applies_heat_floor(self) -> None:
        itch = FixtureCollector(
            "itch",
            lambda run: (
                observation(
                    run,
                    platform="itch",
                    platform_id="winner",
                    name="Shared Winner",
                    developer="Shared Studio",
                    surface="popular",
                    rank=10,
                ),
            ),
        )
        steam = FixtureCollector(
            "steam",
            lambda run: (
                observation(
                    run,
                    platform="steam",
                    platform_id="20",
                    name="Shared Winner",
                    developer="Shared Studio",
                    surface="most_played",
                    rank=50,
                    release_at=NOW - timedelta(days=2),
                ),
                observation(
                    run,
                    platform="steam",
                    platform_id="21",
                    name="Steam Runner",
                    developer="Studio S",
                    surface="most_played",
                    rank=50,
                    release_at=NOW - timedelta(days=2),
                ),
            ),
        )
        roblox = FixtureCollector(
            "roblox",
            lambda run: (
                observation(
                    run,
                    platform="roblox",
                    platform_id="30",
                    name="Roblox Third",
                    developer="Studio R",
                    surface="charts",
                    rank=25,
                ),
                observation(
                    run,
                    platform="roblox",
                    platform_id="31",
                    name="Below Floor",
                    developer="Studio R",
                    surface="charts",
                    rank=100,
                    raw_metrics={"concurrent_players": 0},
                ),
            ),
        )

        result = scan_run(
            config(
                self.root,
                preliminary_top_n=2,
                enrichment_top_n=2,
                final_top_n=2,
            ),
            self.store,
            {"roblox": roblox, "steam": steam, "itch": itch},
            lambda: NOW,
            SequenceIdFactory(),
            ("roblox", "steam", "itch"),
        )

        self.assertEqual(tuple(item.name for item in result.candidates), ("Shared Winner", "Steam Runner"))
        shared = result.candidates[0]
        self.assertEqual(
            {(record.platform, record.platform_id) for record in shared.platform_records},
            {("itch", "winner"), ("steam", "20")},
        )
        all_stored = stored_identities(self.store.path)
        self.assertEqual(len(all_stored), 4)
        below = next(item for item in all_stored if item["name"] == "Below Floor")
        self.assertNotIn(below["opportunity_id"], {item.opportunity_id for item in result.candidates})

    def test_roblox_one_day_history_changes_global_ranking(self) -> None:
        prior_at = NOW - timedelta(hours=24)
        prior = RadarRun(
            schema_version=1,
            run_id=PRIOR_RUN_ID,
            started_at=prior_at,
            mode="scheduled",
            platforms=("roblox",),
            publish_daily=False,
        )
        self.store.create_run(prior)
        self.store.insert_observation(
            observation(
                prior,
                platform="roblox",
                platform_id="40",
                name="History Rocket",
                developer="Studio R",
                surface="rising",
                rank=30,
                observed_at=prior_at,
                raw_metrics={"concurrent_players": 100},
            )
        )
        itch = FixtureCollector(
            "itch",
            lambda run: (
                observation(
                    run,
                    platform="itch",
                    platform_id="steady",
                    name="Steady Itch",
                    developer="Studio I",
                    surface="popular",
                    rank=10,
                ),
            ),
        )
        roblox = FixtureCollector(
            "roblox",
            lambda run: (
                observation(
                    run,
                    platform="roblox",
                    platform_id="40",
                    name="History Rocket",
                    developer="Studio R",
                    surface="rising",
                    rank=5,
                    raw_metrics={"concurrent_players": 300},
                ),
            ),
        )

        result = scan_run(
            config(self.root),
            self.store,
            {"itch": itch, "roblox": roblox},
            lambda: NOW,
            SequenceIdFactory(),
            ("itch", "roblox"),
        )

        self.assertEqual(
            tuple(item.name for item in result.candidates),
            ("History Rocket", "Steady Itch"),
        )

    def test_roblox_counts_one_two_and_three_compatible_daily_appearances(self) -> None:
        def collector() -> FixtureCollector:
            return FixtureCollector(
                "roblox",
                lambda run: (
                    observation(
                        run,
                        platform="roblox",
                        platform_id="41",
                        name="Persistent Roblox",
                        developer="Studio R",
                        surface="rising",
                        rank=50,
                        raw_metrics={"concurrent_players": 100},
                    ),
                ),
            )

        first = scan_run(
            config(self.root, heat_floor=19),
            self.store,
            {"roblox": collector()},
            lambda: NOW - timedelta(hours=48),
            SequenceIdFactory(),
            ("roblox",),
        )
        second = scan_run(
            config(self.root, heat_floor=19),
            self.store,
            {"roblox": collector()},
            lambda: NOW - timedelta(hours=24),
            SequenceIdFactory(),
            ("roblox",),
        )
        third = scan_run(
            config(self.root, heat_floor=22),
            self.store,
            {"roblox": collector()},
            lambda: NOW,
            SequenceIdFactory(),
            ("roblox",),
        )

        self.assertEqual(first.candidates, ())
        self.assertEqual(
            tuple(item.name for item in second.candidates),
            ("Persistent Roblox",),
        )
        self.assertEqual(
            tuple(item.name for item in third.candidates),
            ("Persistent Roblox",),
        )

    def test_itch_first_seen_uses_platform_history_instead_of_resetting_daily(self) -> None:
        prior_at = NOW - timedelta(days=8)
        prior = RadarRun(
            schema_version=1,
            run_id="20260823T160000Z-99999999",
            started_at=prior_at,
            mode="scheduled",
            platforms=("itch",),
            publish_daily=False,
        )
        self.store.create_run(prior)
        self.store.insert_observation(
            observation(
                prior,
                platform="itch",
                platform_id="old-game",
                name="Old Itch Game",
                developer="Studio I",
                surface="newest",
                rank=50,
                observed_at=prior_at,
            )
        )
        collector = FixtureCollector(
            "itch",
            lambda run: (
                observation(
                    run,
                    platform="itch",
                    platform_id="old-game",
                    name="Old Itch Game",
                    developer="Studio I",
                    surface="popular",
                    rank=50,
                ),
            ),
        )

        result = scan_run(
            config(self.root, heat_floor=50),
            self.store,
            {"itch": collector},
            lambda: NOW,
            SequenceIdFactory(),
            ("itch",),
        )

        self.assertEqual(result.candidates, ())

    def test_itch_first_seen_accepts_current_browser_observation_after_run_start(self) -> None:
        collector = FixtureCollector(
            "itch",
            lambda run: (
                observation(
                    run,
                    platform="itch",
                    platform_id="browser-late",
                    name="Browser Late",
                    developer="Studio I",
                    surface="popular",
                    rank=50,
                    observed_at=run.started_at + timedelta(hours=2),
                ),
            ),
        )

        result = scan_run(
            config(self.root, heat_floor=50),
            self.store,
            {"itch": collector},
            lambda: NOW,
            SequenceIdFactory(),
            ("itch",),
        )

        self.assertEqual(
            tuple(item.name for item in result.candidates),
            ("Browser Late",),
        )

    def test_itch_compatible_popular_rank_improvement_changes_candidate_order(self) -> None:
        prior_at = NOW - timedelta(hours=24)
        prior = RadarRun(
            schema_version=1,
            run_id=PRIOR_RUN_ID,
            started_at=prior_at,
            mode="scheduled",
            platforms=("itch",),
            publish_daily=False,
        )
        self.store.create_run(prior)
        self.store.insert_observation(
            observation(
                prior,
                platform="itch",
                platform_id="static",
                name="Alpha Static",
                developer="Studio I",
                surface="popular",
                rank=5,
                observed_at=prior_at,
            )
        )
        self.store.insert_observation(
            observation(
                prior,
                platform="itch",
                platform_id="rising",
                name="Zulu Rising",
                developer="Studio I",
                surface="popular",
                rank=30,
                observed_at=prior_at,
            )
        )
        collector = FixtureCollector(
            "itch",
            lambda run: (
                observation(
                    run,
                    platform="itch",
                    platform_id="static",
                    name="Alpha Static",
                    developer="Studio I",
                    surface="popular",
                    rank=5,
                ),
                observation(
                    run,
                    platform="itch",
                    platform_id="rising",
                    name="Zulu Rising",
                    developer="Studio I",
                    surface="popular",
                    rank=10,
                ),
            ),
        )

        result = scan_run(
            config(self.root),
            self.store,
            {"itch": collector},
            lambda: NOW,
            SequenceIdFactory(),
            ("itch",),
        )

        self.assertEqual(
            tuple(item.name for item in result.candidates),
            ("Zulu Rising", "Alpha Static"),
        )

    def test_repeated_scan_reuses_persisted_identity_and_stable_order(self) -> None:
        def rows(run: RadarRun):
            return (
                observation(
                    run,
                    platform="steam",
                    platform_id="50",
                    name="Alpha",
                    developer="Studio",
                    surface="most_played",
                    rank=50,
                    release_at=run.started_at - timedelta(days=2),
                ),
                observation(
                    run,
                    platform="steam",
                    platform_id="51",
                    name="Beta",
                    developer="Studio",
                    surface="most_played",
                    rank=50,
                    release_at=run.started_at - timedelta(days=2),
                ),
            )

        first = scan_run(
            config(self.root),
            self.store,
            {"steam": FixtureCollector("steam", rows)},
            lambda: NOW,
            SequenceIdFactory(),
            ("steam",),
        )
        second_ids = SequenceIdFactory(
            (
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            )
        )
        second = scan_run(
            config(self.root),
            self.store,
            {"steam": FixtureCollector("steam", rows)},
            lambda: NOW + timedelta(days=1),
            second_ids,
            ("steam",),
        )

        self.assertEqual(
            tuple(item.opportunity_id for item in second.candidates),
            tuple(item.opportunity_id for item in first.candidates),
        )
        self.assertEqual(tuple(item.name for item in second.candidates), ("Alpha", "Beta"))
        self.assertEqual(len(second_ids.calls), 1)


if __name__ == "__main__":
    unittest.main()
