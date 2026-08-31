from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import urlsplit


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_DIR = PROJECT_DIR.parent
FIXTURE_DIR = REPOSITORY_DIR / "steam-game-radar/tests/fixtures"
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(REPOSITORY_DIR / "steam-game-radar"))

from steam_game_radar.config import RadarConfig as SteamRadarConfig
from steam_game_radar.errors import ProviderUnavailableError
from steam_game_radar.official_provider import (
    CollectionResult,
    collect_official,
)
from unified_game_radar.collectors.steam import (
    SteamCollector,
    adapt_collection_result,
    adapt_raw_payloads,
)
from unified_game_radar.config import RadarConfig
from unified_game_radar.errors import InputValidationError
from unified_game_radar.schemas import RadarRun


RUN_ID = "20260831T020000Z-a1b2c3d4"
STARTED_AT = datetime(2026, 8, 31, 2, tzinfo=timezone.utc)


def load_fixture(name: str) -> object:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


class FixtureClient:
    """Steam transport fake backed by the committed official fixtures."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._most_played = load_fixture("most_played.json")
        self._featured = load_fixture("featured_categories.json")
        self._appdetails = load_fixture("appdetails.json")
        self._current_players = load_fixture("current_players.json")

    def get_json(self, url: str) -> object:
        self.calls.append(url)
        if "GetMostPlayedGames" in url:
            return self._most_played
        if "featuredcategories" in url:
            return self._featured
        if "appdetails" in url:
            return self._appdetails
        if "GetNumberOfCurrentPlayers" in url:
            return self._current_players
        raise AssertionError(f"unexpected Steam URL: {url}")


def make_run() -> RadarRun:
    return RadarRun(
        schema_version=1,
        run_id=RUN_ID,
        started_at=STARTED_AT,
        mode="scheduled",
        platforms=("itch", "steam", "roblox"),
        publish_daily=False,
    )


def make_config() -> RadarConfig:
    return RadarConfig(
        country="CN",
        locale="zh-CN",
        steam_language="schinese",
        data_dir=Path("state/radar"),
    )


def legacy_fixture_result(
    client: FixtureClient | None = None,
) -> tuple[CollectionResult, FixtureClient]:
    fixture_client = FixtureClient() if client is None else client
    result = collect_official(
        fixture_client,
        SteamRadarConfig(
            country="CN",
            language="schinese",
            released_candidate_limit=50,
            unreleased_candidate_limit=50,
        ),
        "2026-08-31T02:00:00Z",
    )
    return result, fixture_client


class SteamCollectorTests(unittest.TestCase):
    def test_collect_adapts_released_and_upcoming_surface_observations(self) -> None:
        client = FixtureClient()
        provider_calls: list[tuple[object, object, str]] = []

        def provider(
            passed_client: object,
            config: SteamRadarConfig,
            observed_at: str,
        ) -> CollectionResult:
            provider_calls.append((passed_client, config, observed_at))
            return collect_official(passed_client, config, observed_at)  # type: ignore[arg-type]

        result = SteamCollector(
            make_config(),
            client,  # type: ignore[arg-type]
            collect_official_fn=provider,
        ).collect(make_run())

        self.assertEqual(len(provider_calls), 1)
        passed_client, passed_config, observed_at = provider_calls[0]
        self.assertIs(passed_client, client)
        self.assertEqual(passed_config.country, "CN")
        self.assertEqual(passed_config.language, "schinese")
        self.assertEqual(observed_at, "2026-08-31T02:00:00Z")

        self.assertEqual(
            [observation.observation_id for observation in result.observations],
            [
                "steam:730:most_played:20260831T020000Z",
                "steam:730:top_sellers:20260831T020000Z",
                "steam:730:new_releases:20260831T020000Z",
                "steam:3000001:coming_soon:20260831T020000Z",
            ],
        )
        self.assertEqual(
            [observation.source_rank for observation in result.observations],
            [1, 1, 1, 1],
        )
        self.assertEqual(
            len({row.observation_id for row in result.observations}),
            4,
        )

        released = result.observations[0]
        upcoming = result.observations[-1]
        self.assertEqual(released.platform_id, "730")
        self.assertEqual(released.release_at, datetime(2012, 8, 21, tzinfo=timezone.utc))
        self.assertEqual(released.raw_metrics["name"], "Counter-Strike 2")
        self.assertEqual(released.raw_metrics["release_status"], "released")
        self.assertEqual(upcoming.platform_id, "3000001")
        self.assertEqual(upcoming.release_at, datetime(2026, 8, 20, tzinfo=timezone.utc))
        self.assertEqual(upcoming.raw_metrics["release_status"], "unreleased")

        for observation in result.observations:
            self.assertEqual(observation.geo, "CN")
            self.assertEqual(observation.locale, "zh-CN")
            self.assertEqual(
                dict(observation.query_parameters),
                {"country": "CN", "language": "schinese"},
            )
            self.assertEqual(
                observation.evidence_urls,
                (f"https://store.steampowered.com/app/{observation.platform_id}/",),
            )

    def test_shared_metric_values_and_provenance_are_retained_per_surface(self) -> None:
        legacy, _ = legacy_fixture_result()

        observations = adapt_collection_result(
            make_run(),
            legacy,
            geo="CN",
            locale="zh-CN",
            language="schinese",
        )

        released = tuple(row for row in observations if row.platform_id == "730")
        self.assertEqual(len(released), 3)
        for observation in released:
            metrics = observation.raw_metrics["metrics"]
            self.assertEqual(
                metrics["current_players"],  # type: ignore[index]
                {
                    "value": 1_287_345,
                    "source_id": "steam_current_players",
                    "source_kind": "steam_official",
                    "observed_at": "2026-08-31T02:00:00Z",
                },
            )
            self.assertEqual(
                metrics["release_date"]["source_id"],  # type: ignore[index]
                "steam_appdetails",
            )
            self.assertEqual(
                observation.raw_metrics["discovery_sources"],
                ("most_played", "top_sellers", "new_releases"),
            )

    def test_only_populated_discovery_rank_metrics_create_observations(self) -> None:
        legacy, _ = legacy_fixture_result()

        first = adapt_collection_result(make_run(), legacy)
        second = adapt_collection_result(make_run(), legacy)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 4)
        self.assertNotIn("previous_rank", {row.surface for row in first})
        self.assertNotIn("current_players", {row.surface for row in first})
        self.assertNotIn("release_date", {row.surface for row in first})

    def test_health_uses_canonical_capabilities_and_warning_owner(self) -> None:
        result = SteamCollector(
            make_config(),
            FixtureClient(),  # type: ignore[arg-type]
        ).collect(make_run())

        self.assertEqual(result.collector, "steam")
        self.assertEqual(result.health.status, "partial")
        self.assertEqual(
            dict(result.health.capabilities),
            {
                "most_played": True,
                "featured_categories": True,
                "appdetails": False,
                "current_players": True,
            },
        )
        self.assertEqual(
            [warning.code for warning in result.health.warnings],
            [
                "steam_appdetails_malformed",
                "steam_appdetails_malformed",
                "steam_app_type_dlc_excluded",
                "steam_app_type_demo_excluded",
                "steam_app_type_software_excluded",
                "steam_app_type_unknown_excluded",
            ],
        )
        self.assertTrue(
            all(warning.collector == "steam" for warning in result.health.warnings)
        )
        self.assertNotIn("warnings", result.__dataclass_fields__)

    def test_raw_payloads_remain_pending_until_safe_artifact_persistence(self) -> None:
        legacy, _ = legacy_fixture_result()
        payloads = adapt_raw_payloads(legacy)

        self.assertEqual(payloads, legacy.raw_to_dict())
        self.assertIsNot(payloads, legacy.raw)

        def provider(
            client: object,
            config: SteamRadarConfig,
            observed_at: str,
        ) -> CollectionResult:
            del client, config, observed_at
            return legacy

        with TemporaryDirectory() as directory:
            collector = SteamCollector(
                replace(make_config(), data_dir=Path(directory)),
                object(),  # type: ignore[arg-type]
                collect_official_fn=provider,
            )
            first = collector.collect(make_run())
            second = collector.collect(make_run())

            self.assertEqual(first.pending_raw_payloads, second.pending_raw_payloads)
            self.assertEqual(len(first.pending_raw_payloads), len(legacy.raw))
            self.assertEqual(first.raw_artifacts, ())
            self.assertEqual(second.raw_artifacts, ())
            self.assertEqual(list(Path(directory).iterdir()), [])

        pending_by_name = {
            pending.artifact_name: pending
            for pending in first.pending_raw_payloads
        }
        featured = pending_by_name["steam_featured_categories.json"]
        self.assertEqual(featured.run_id, RUN_ID)
        self.assertEqual(featured.provider, "steam_official")
        self.assertEqual(featured.observed_at, STARTED_AT)
        self.assertEqual(
            _thaw_json(featured.payload),
            payloads["featured_categories"],
        )

    def test_secret_raw_values_exist_only_in_frozen_pending_memory(self) -> None:
        legacy, _ = legacy_fixture_result()
        secret_result = replace(
            legacy,
            raw={
                "request_context": {
                    "token": "top-secret",
                    "headers": {"Cookie": "session=private"},
                }
            },
        )

        def provider(
            client: object,
            config: SteamRadarConfig,
            observed_at: str,
        ) -> CollectionResult:
            del client, config, observed_at
            return secret_result

        result = SteamCollector(
            make_config(),
            object(),  # type: ignore[arg-type]
            collect_official_fn=provider,
        ).collect(make_run())

        self.assertEqual(result.raw_artifacts, ())
        self.assertEqual(len(result.pending_raw_payloads), 1)
        payload = result.pending_raw_payloads[0].payload
        self.assertEqual(payload["token"], "top-secret")  # type: ignore[index]
        self.assertEqual(
            payload["headers"]["Cookie"],  # type: ignore[index]
            "session=private",
        )
        with self.assertRaises(TypeError):
            payload["token"] = "changed"  # type: ignore[index]

    def test_exact_duplicates_are_deduplicated_with_first_order(self) -> None:
        legacy, _ = legacy_fixture_result()
        released = legacy.released[0]

        within_released = replace(
            legacy,
            released=(released, released),
        )
        across_release_sets = replace(
            legacy,
            released=(released,),
            unreleased=(released, *legacy.unreleased),
        )

        within = adapt_collection_result(make_run(), within_released)
        across = adapt_collection_result(make_run(), across_release_sets)
        baseline = adapt_collection_result(make_run(), legacy)

        self.assertEqual(within, baseline)
        self.assertEqual(across, baseline)
        self.assertEqual(
            tuple(row.observation_id for row in across),
            tuple(dict.fromkeys(row.observation_id for row in across)),
        )

    def test_same_observation_id_with_different_payload_is_rejected(self) -> None:
        legacy, _ = legacy_fixture_result()
        released = legacy.released[0]
        conflicting_data = released.to_dict()
        conflicting_data["name"] = "Conflicting Steam Name"
        conflicting = type(released).from_dict(conflicting_data)
        conflicting_results = (
            replace(legacy, released=(released, conflicting)),
            replace(
                legacy,
                released=(released,),
                unreleased=(conflicting, *legacy.unreleased),
            ),
        )

        for conflict in conflicting_results:
            with self.subTest(
                location=(len(conflict.released), len(conflict.unreleased))
            ):
                with self.assertRaisesRegex(
                    InputValidationError,
                    "observation_id.*different payload",
                ):
                    adapt_collection_result(make_run(), conflict)

    def test_automated_collection_requests_only_steam_owned_hosts(self) -> None:
        client = FixtureClient()

        SteamCollector(
            make_config(),
            client,  # type: ignore[arg-type]
        ).collect(make_run())

        hosts = {urlsplit(url).hostname for url in client.calls}
        self.assertEqual(
            hosts,
            {"api.steampowered.com", "store.steampowered.com"},
        )
        self.assertNotIn("steamdb.info", hosts)

    def test_provider_unavailability_is_left_for_orchestration_to_isolate(self) -> None:
        expected = ProviderUnavailableError("Steam is unavailable")

        def unavailable(
            client: object,
            config: SteamRadarConfig,
            observed_at: str,
        ) -> CollectionResult:
            del client, config, observed_at
            raise expected

        collector = SteamCollector(
            make_config(),
            object(),  # type: ignore[arg-type]
            collect_official_fn=unavailable,
        )

        with self.assertRaises(ProviderUnavailableError) as captured:
            collector.collect(make_run())
        self.assertIs(captured.exception, expected)


if __name__ == "__main__":
    unittest.main()
