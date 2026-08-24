from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from steam_game_radar.config import RadarConfig
from steam_game_radar.http_client import JsonHttpClient
from steam_game_radar.official_provider import collect_official


class LiveOfficialProviderTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("STEAM_RADAR_LIVE") == "1",
        "live Steam check disabled",
    )
    def test_live_official_provider_has_an_available_capability(self) -> None:
        config = RadarConfig.from_file(
            PROJECT_DIR / "references" / "config.example.json",
            project_root=PROJECT_DIR,
        )
        config = replace(
            config,
            released_candidate_limit=2,
            unreleased_candidate_limit=2,
        )
        observed_at = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )

        result = collect_official(JsonHttpClient(config), config, observed_at)

        self.assertTrue(any(result.capabilities.values()))
