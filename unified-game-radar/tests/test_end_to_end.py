from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(PROJECT_DIR))

from unified_game_radar.collectors.base import CollectorResult
from unified_game_radar.collectors.itch import parse_itch_envelope
from unified_game_radar.collectors.roblox import parse_roblox_envelope
from unified_game_radar.config import RadarConfig
from unified_game_radar.orchestration import enrich_run, ingest_run, scan_run
from unified_game_radar.schemas import (
    OpportunityEvidence,
    PlatformObservation,
    RadarRun,
    SourceHealth,
)
from unified_game_radar.storage import RadarStore


PRIOR_AT = datetime(2026, 8, 30, 8, tzinfo=timezone.utc)
CURRENT_AT = PRIOR_AT + timedelta(hours=24)
ENRICHED_AT = CURRENT_AT + timedelta(minutes=30)
POSITIVE_ACTIONS = frozenset({"immediate_action", "worth_content_mvp"})


class DeterministicIds:
    def __init__(self) -> None:
        self._next = 1

    def __call__(self) -> str:
        value = f"00000000-0000-4000-8000-{self._next:012x}"
        self._next += 1
        return value


class SteamSnapshotCollector:
    def __init__(self, *, players: int, rank: int) -> None:
        self.players = players
        self.rank = rank

    def collect(self, run: RadarRun) -> CollectorResult:
        store_url = "https://store.steampowered.com/app/4242/"
        observed_at_text = run.started_at.isoformat().replace("+00:00", "Z")
        observation = PlatformObservation(
            schema_version=1,
            observation_id=(
                f"steam:4242:most_played:"
                f"{run.started_at.strftime('%Y%m%dT%H%M%SZ')}"
            ),
            run_id=run.run_id,
            platform="steam",
            platform_id="4242",
            provider="steam_official",
            surface="most_played",
            geo="US",
            locale="en",
            query_parameters={"country": "US", "language": "english"},
            metric_definition_version=1,
            observed_at=run.started_at,
            release_at=PRIOR_AT - timedelta(days=2),
            source_rank=self.rank,
            raw_metrics={
                "name": "Durable Harbor",
                "developer": "Harbor Works",
                "release_status": "released",
                "store_url": store_url,
                "metrics": {
                    "current_players": {
                        "value": self.players,
                        "source_id": "steam_current_players",
                        "source_kind": "steam_official",
                        "observed_at": observed_at_text,
                    }
                },
            },
            evidence_urls=(store_url,),
        )
        return CollectorResult(
            collector="steam",
            observations=(observation,),
            health=SourceHealth(
                schema_version=1,
                run_id=run.run_id,
                collector="steam",
                status="fresh",
                observed_at=run.started_at,
                capabilities={"most_played": True},
                warnings=(),
            ),
            raw_artifacts=(),
            pending_raw_payloads=(),
        )


def radar_config(root: Path) -> RadarConfig:
    return RadarConfig(
        data_dir=root / "data",
        report_dir=root / "reports",
        heat_floor=1,
        preliminary_top_n=3,
        enrichment_top_n=3,
        final_top_n=3,
    )


def utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def itch_envelope(run_id: str, observed_at: datetime, popular_rank: int) -> dict[str, object]:
    common: dict[str, object] = {
        "title": "Zero Quest",
        "developer": "Zero Studio",
        "game_url": "https://zero-studio.itch.io/zero-quest",
        "surface_scope": "global",
        "browser_playable": True,
        "genre": "Puzzle",
        "is_jam": False,
        "author_release_count": 4,
        "originality": "verified_original",
        "observed_at": utc_text(observed_at),
    }
    rows = (
        {
            **common,
            "surface": "newest",
            "rank": 2,
            "evidence_url": "https://itch.io/games/newest",
        },
        {
            **common,
            "surface": "popular",
            "rank": popular_rank,
            "evidence_url": "https://itch.io/games/top-sellers",
        },
    )
    return {
        "schema_version": 1,
        "run_id": run_id,
        "collector": "itch",
        "geo": "US",
        "locale": "en",
        "metric_definition_version": 1,
        "observed_at": utc_text(observed_at),
        "rows": list(rows),
    }


def roblox_envelope(
    run_id: str,
    observed_at: datetime,
    *,
    rank: int,
    players: int,
) -> dict[str, object]:
    evidence_urls = {
        "rising": "https://www.roblox.com/charts/top-trending",
        "up-and-coming": "https://www.roblox.com/charts/top-up-and-coming",
        "charts": "https://www.roblox.com/charts/top-playing-now",
    }
    rows = [
        {
            "universe_id": 515151,
            "place_id": 616161,
            "name": "Spike Arena",
            "developer": "Spike Studio",
            "game_url": "https://www.roblox.com/games/616161/Spike-Arena",
            "surface": surface,
            "surface_scope": "global",
            "rank": rank + offset,
            "concurrent_players": players,
            "visits": players * 100,
            "favorites": players * 10,
            "observed_at": utc_text(observed_at),
            "evidence_url": evidence_url,
        }
        for offset, (surface, evidence_url) in enumerate(evidence_urls.items())
    ]
    return {
        "schema_version": 1,
        "run_id": run_id,
        "collector": "roblox",
        "geo": "US",
        "locale": "en",
        "metric_definition_version": 1,
        "observed_at": utc_text(observed_at),
        "rows": rows,
    }


def bind_fixture(
    fixture_name: str,
    *,
    run_id: str,
    opportunity_id: str,
    game_name: str,
) -> OpportunityEvidence:
    text = (FIXTURES / fixture_name).read_text(encoding="utf-8")
    for marker, replacement in (
        ("{{RUN_ID}}", run_id),
        ("{{OPPORTUNITY_ID}}", opportunity_id),
        ("{{GAME_NAME}}", game_name),
    ):
        text = text.replace(marker, replacement)
    return OpportunityEvidence.from_dict(json.loads(text))


def observations_for(store: RadarStore, run_id: str) -> tuple[PlatformObservation, ...]:
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute(
            "SELECT canonical_json FROM observations WHERE run_id = ? "
            "ORDER BY observation_id",
            (run_id,),
        ).fetchall()
    return tuple(PlatformObservation.from_dict(json.loads(row[0])) for row in rows)


class UnifiedRadarEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.config = radar_config(self.root)
        self.store = RadarStore(self.root / "radar.sqlite3")
        self.store.initialize()
        self.ids = DeterministicIds()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def snapshot(
        self,
        started_at: datetime,
        *,
        steam_players: int,
        steam_rank: int,
        itch_popular_rank: int,
        roblox_rank: int,
        roblox_players: int,
    ):
        result = scan_run(
            self.config,
            self.store,
            {
                "steam": SteamSnapshotCollector(
                    players=steam_players,
                    rank=steam_rank,
                )
            },
            lambda: started_at,
            self.ids,
            ("itch", "steam", "roblox"),
        )
        observed_at = started_at + timedelta(minutes=5)
        result = ingest_run(
            self.config,
            self.store,
            result.run_id,
            itch_envelope(result.run_id, observed_at, itch_popular_rank),
            {"itch": parse_itch_envelope, "roblox": parse_roblox_envelope},
            lambda: started_at + timedelta(minutes=10),
            self.ids,
        )
        return ingest_run(
            self.config,
            self.store,
            result.run_id,
            roblox_envelope(
                result.run_id,
                observed_at,
                rank=roblox_rank,
                players=roblox_players,
            ),
            {"itch": parse_itch_envelope, "roblox": parse_roblox_envelope},
            lambda: started_at + timedelta(minutes=10),
            self.ids,
        )

    def test_two_snapshots_browser_ingest_and_batch_enrichment_enforce_demand_gates(self) -> None:
        prior = self.snapshot(
            PRIOR_AT,
            steam_players=1_000,
            steam_rank=25,
            itch_popular_rank=20,
            roblox_rank=20,
            roblox_players=500,
        )
        current = self.snapshot(
            CURRENT_AT,
            steam_players=20_000,
            steam_rank=1,
            itch_popular_rank=2,
            roblox_rank=1,
            roblox_players=10_000,
        )

        self.assertEqual(len(current.candidates), 3)
        self.assertEqual(
            {record.platform for item in current.candidates for record in item.platform_records},
            {"itch", "steam", "roblox"},
        )
        for observation in observations_for(self.store, current.run_id):
            previous = self.store.compatible_observation(
                observation,
                target_hours=24,
                tolerance_hours=6,
            )
            self.assertIsNotNone(previous)
            assert previous is not None
            self.assertEqual(previous.run_id, prior.run_id)

        fixtures = {
            "itch": "evidence_zero_demand.json",
            "steam": "evidence_sustained.json",
            "roblox": "evidence_single_spike.json",
        }
        evidence_batch = []
        candidate_by_platform = {}
        for candidate in current.candidates:
            self.assertEqual(len(candidate.platform_records), 1)
            platform = candidate.platform_records[0].platform
            candidate_by_platform[platform] = candidate
            evidence_batch.append(
                bind_fixture(
                    fixtures[platform],
                    run_id=current.run_id,
                    opportunity_id=candidate.opportunity_id,
                    game_name=candidate.name,
                )
            )

        manifest = enrich_run(
            self.config,
            self.store,
            current.run_id,
            tuple(evidence_batch),
            lambda: ENRICHED_AT,
        )

        self.assertEqual(manifest.phase, "final")
        scores = {
            platform: self.store.get_score(
                current.run_id,
                candidate.opportunity_id,
            )
            for platform, candidate in candidate_by_platform.items()
        }
        self.assertEqual(scores["itch"].demand_state, "fail")
        self.assertEqual(scores["itch"].action, "skip")
        self.assertEqual(scores["roblox"].demand_state, "early_watch")
        self.assertEqual(scores["roblox"].action, "watch")
        self.assertEqual(scores["steam"].demand_state, "pass")
        positive = tuple(
            platform
            for platform, score in scores.items()
            if score.action in POSITIVE_ACTIONS
        )
        self.assertEqual(positive, ("steam",))


if __name__ == "__main__":
    unittest.main()
