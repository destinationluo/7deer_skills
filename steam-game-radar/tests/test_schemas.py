from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from steam_game_radar.errors import InputValidationError
from steam_game_radar.schemas import (
    GameRecord,
    MetricObservation,
    RejectedRow,
    WarningRecord,
)


class SchemaTests(unittest.TestCase):
    def metric_dict(self) -> dict[str, object]:
        return {
            "value": 125_000,
            "source_id": "steam-current-players",
            "source_kind": "steam_official",
            "observed_at": "2026-08-24T03:00:00Z",
        }

    def game_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "appid": 730,
            "name": "Counter-Strike 2",
            "release_status": "released",
            "store_url": "https://store.steampowered.com/app/730",
            "metrics": {"current_players": self.metric_dict()},
            "source_extra": {"rank": 1, "tags": ["FPS", "Competitive"]},
        }

    def test_metric_round_trip(self) -> None:
        expected = self.metric_dict()
        metric = MetricObservation.from_dict(expected)

        self.assertEqual(metric.to_dict(), expected)
        self.assertEqual(MetricObservation.from_dict(metric.to_dict()), metric)

    def test_game_round_trip(self) -> None:
        expected = self.game_dict()
        game = GameRecord.from_dict(expected)

        self.assertEqual(game.to_dict(), expected)
        self.assertEqual(GameRecord.from_dict(game.to_dict()), game)
        warning = {"code": "stale", "message": "data is stale", "appid": 730}
        rejected = {
            "row_number": 4,
            "code": "invalid_appid",
            "message": "appid is invalid",
            "appid": None,
        }
        self.assertEqual(WarningRecord.from_dict(warning).to_dict(), warning)
        self.assertEqual(RejectedRow.from_dict(rejected).to_dict(), rejected)

    def test_required_schema_version(self) -> None:
        missing = self.game_dict()
        del missing["schema_version"]
        with self.assertRaises(InputValidationError):
            GameRecord.from_dict(missing)

        for schema_version in (2, 1.0, True):
            unsupported = self.game_dict()
            unsupported["schema_version"] = schema_version
            with self.subTest(schema_version=schema_version), self.assertRaises(
                InputValidationError
            ):
                GameRecord.from_dict(unsupported)

    def test_positive_appid(self) -> None:
        for appid in (0, -1, True, "730"):
            value = self.game_dict()
            value["appid"] = appid
            with self.subTest(appid=appid), self.assertRaises(InputValidationError):
                GameRecord.from_dict(value)

    def test_https_steam_store_url(self) -> None:
        invalid_urls = (
            "http://store.steampowered.com/app/730",
            "https://example.com/app/730",
            "https://store.steampowered.com/app/570",
            "https://store.steampowered.com/sub/730",
        )
        for store_url in invalid_urls:
            value = self.game_dict()
            value["store_url"] = store_url
            with self.subTest(store_url=store_url), self.assertRaises(
                InputValidationError
            ):
                GameRecord.from_dict(value)

    def test_allowed_release_states(self) -> None:
        for release_status in ("released", "unreleased", "unknown"):
            value = self.game_dict()
            value["release_status"] = release_status
            self.assertEqual(
                GameRecord.from_dict(value).release_status,
                release_status,
            )

        invalid = self.game_dict()
        invalid["release_status"] = "early_access"
        with self.assertRaises(InputValidationError):
            GameRecord.from_dict(invalid)

    def test_iso_utc_observed_at(self) -> None:
        valid = self.metric_dict()
        valid["observed_at"] = "2026-08-24T03:04:05.123456Z"
        self.assertEqual(
            MetricObservation.from_dict(valid).observed_at,
            "2026-08-24T03:04:05.123456Z",
        )

        for observed_at in (
            "2026-08-24T03:00:00+08:00",
            "2026-08-24 03:00:00Z",
            "not-a-time",
        ):
            value = self.metric_dict()
            value["observed_at"] = observed_at
            with self.subTest(observed_at=observed_at), self.assertRaises(
                InputValidationError
            ):
                MetricObservation.from_dict(value)

    def test_unknown_metrics_omitted_not_zero(self) -> None:
        value = self.game_dict()
        value["metrics"] = {
            "followers": {
                "value": 4_321,
                "source_id": "steamdb-followers-import",
                "source_kind": "steamdb_manual_import",
                "observed_at": "2026-08-24T03:00:00Z",
            }
        }
        game = GameRecord.from_dict(value)

        self.assertEqual(set(game.metrics), {"followers"})
        self.assertNotIn("current_players", game.to_dict()["metrics"])
        self.assertNotIn("wishlist_activity", game.to_dict()["metrics"])


if __name__ == "__main__":
    unittest.main()
