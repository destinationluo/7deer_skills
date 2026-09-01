from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import unittest
from urllib.parse import urlsplit


PROJECT_DIR = Path(__file__).resolve().parents[1]
STEAM_PROJECT_DIR = PROJECT_DIR.parent / "steam-game-radar"
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(STEAM_PROJECT_DIR))

from steam_game_radar.http_client import JsonHttpClient
from steam_game_radar.official_provider import collect_official
from unified_game_radar.collectors.itch import (
    build_itch_observations,
    parse_itch_envelope,
)
from unified_game_radar.collectors.roblox import (
    build_roblox_observations,
    parse_roblox_envelope,
)
from unified_game_radar.collectors.steam import adapt_collection_result
from unified_game_radar.config import RadarConfig
from unified_game_radar.schemas import RadarRun


LIVE_ENABLED = os.environ.get("RUN_LIVE_RADAR_TESTS") == "1"


def live_run(run_id: str, observed_at: datetime, platform: str) -> RadarRun:
    return RadarRun(
        schema_version=1,
        run_id=run_id,
        started_at=observed_at,
        mode="manual",
        platforms=(platform,),
        publish_daily=False,
    )


def browser_payload(environment_name: str) -> tuple[bytes, dict[str, object], datetime]:
    configured = os.environ.get(environment_name)
    if not configured:
        raise unittest.SkipTest(f"{environment_name} is not configured")
    path = Path(configured)
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise unittest.SkipTest(f"unable to read {environment_name}: {error}") from error
    if not isinstance(payload, dict):
        raise AssertionError(f"{environment_name} must contain one JSON object")
    observed_text = payload.get("observed_at")
    if not isinstance(observed_text, str):
        raise AssertionError(f"{environment_name} is missing observed_at")
    try:
        observed_at = datetime.fromisoformat(observed_text.replace("Z", "+00:00"))
    except ValueError as error:
        raise AssertionError(f"{environment_name} has invalid observed_at") from error
    now = datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() != timezone.utc.utcoffset(now):
        raise AssertionError(f"{environment_name} observed_at must be UTC")
    if observed_at > now or observed_at.date() != now.date():
        raise AssertionError(f"{environment_name} must be a same-day UTC envelope")
    return raw, payload, observed_at.astimezone(timezone.utc)


@unittest.skipUnless(LIVE_ENABLED, "live radar provider tests are disabled")
class LiveProviderContractTests(unittest.TestCase):
    def test_steam_official_low_limit_converts_to_unified_contract(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        config = RadarConfig(
            steam_released_candidate_limit=2,
            steam_unreleased_candidate_limit=2,
            request_timeout_seconds=5,
            max_retries=0,
            minimum_request_interval_seconds=0.01,
        )
        steam_config = config.to_steam_config()
        result = collect_official(
            JsonHttpClient(steam_config),
            steam_config,
            now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        records = (*result.released, *result.unreleased)
        if not records:
            self.skipTest("Steam official provider returned no convertible candidates")
        run = live_run(
            f"{now.strftime('%Y%m%dT%H%M%SZ')}-00000000",
            now,
            "steam",
        )

        observations = adapt_collection_result(run, result)

        self.assertTrue(observations)
        self.assertLessEqual(len(result.released), 2)
        self.assertLessEqual(len(result.unreleased), 2)
        self.assertTrue(all(item.provider == "steam_official" for item in observations))

    def test_same_day_itch_envelope_uses_allowed_hosts_and_converts(self) -> None:
        raw, payload, observed_at = browser_payload("ITCH_LIVE_OBSERVATION_JSON")
        run_id = payload.get("run_id")
        if not isinstance(run_id, str):
            self.fail("ITCH_LIVE_OBSERVATION_JSON is missing run_id")
        run = live_run(run_id, observed_at, "itch")

        envelope = parse_itch_envelope(raw, run)
        observations = build_itch_observations(run, envelope)

        self.assertTrue(envelope.rows)
        self.assertTrue(observations)
        for row in envelope.rows:
            game_host = (urlsplit(row.game_url).hostname or "").casefold()
            evidence_host = (urlsplit(row.evidence_url).hostname or "").casefold()
            self.assertTrue(game_host == "itch.io" or game_host.endswith(".itch.io"))
            self.assertIn(evidence_host, {"itch.io", "www.itch.io"})

    def test_same_day_roblox_envelope_uses_allowed_hosts_and_converts(self) -> None:
        raw, payload, observed_at = browser_payload("ROBLOX_LIVE_OBSERVATION_JSON")
        run_id = payload.get("run_id")
        if not isinstance(run_id, str):
            self.fail("ROBLOX_LIVE_OBSERVATION_JSON is missing run_id")
        run = live_run(run_id, observed_at, "roblox")

        envelope = parse_roblox_envelope(raw, run)
        observations = build_roblox_observations(run, envelope)

        self.assertTrue(envelope.rows)
        self.assertTrue(observations)
        for row in envelope.rows:
            self.assertIn(
                (urlsplit(row.game_url).hostname or "").casefold(),
                {"roblox.com", "www.roblox.com"},
            )
            self.assertIn(
                (urlsplit(row.evidence_url).hostname or "").casefold(),
                {"roblox.com", "www.roblox.com"},
            )


if __name__ == "__main__":
    unittest.main()
