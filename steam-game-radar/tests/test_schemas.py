from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from steam_game_radar.errors import InputValidationError
from steam_game_radar import (
    MAX_JSON_SAFE_INTEGER,
    MAX_STEAM_APPID,
    MIN_JSON_SAFE_INTEGER,
)
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
        self.assertEqual(json.loads(json.dumps(metric.to_dict())), expected)

        caller_value = {"history": [1, {"label": "before"}]}
        isolated = MetricObservation(
            value=caller_value,
            source_id="test-source",
            source_kind="steam_official",
            observed_at="2026-08-24T03:00:00Z",
        )
        caller_value["history"].append(2)
        caller_value["history"][1]["label"] = "after"
        self.assertEqual(
            isolated.to_dict()["value"],
            {"history": [1, {"label": "before"}]},
        )
        with self.assertRaises(TypeError):
            isolated.value["new"] = "blocked"

        non_string_key = self.metric_dict()
        non_string_key["value"] = {"nested": {1: "invalid"}}
        with self.assertRaises(InputValidationError):
            MetricObservation.from_dict(non_string_key)

        tuple_value = self.metric_dict()
        tuple_value["value"] = {"not_json_array": (1, 2)}
        with self.assertRaises(InputValidationError):
            MetricObservation.from_dict(tuple_value)

        safe_value = {
            "bounds": [
                MIN_JSON_SAFE_INTEGER,
                {"maximum": MAX_JSON_SAFE_INTEGER, "flag": True},
            ]
        }
        safe_metric = MetricObservation(
            value=safe_value,
            source_id="json-safe-bounds",
            source_kind="steam_official",
            observed_at="2026-08-24T03:00:00Z",
        )
        self.assertEqual(safe_metric.to_dict()["value"], safe_value)
        self.assertEqual(
            json.loads(json.dumps(safe_metric.to_dict()))["value"],
            safe_value,
        )
        for unsafe_value in (
            {"nested": [MAX_JSON_SAFE_INTEGER + 1]},
            [{"nested": {"minimum": MIN_JSON_SAFE_INTEGER - 1}}],
        ):
            with self.subTest(unsafe_value=unsafe_value), self.assertRaises(
                InputValidationError
            ):
                MetricObservation(
                    value=unsafe_value,
                    source_id="unsafe-integer",
                    source_kind="steam_official",
                    observed_at="2026-08-24T03:00:00Z",
                )
        self.assertEqual(MAX_JSON_SAFE_INTEGER, 9_007_199_254_740_991)
        self.assertEqual(MIN_JSON_SAFE_INTEGER, -9_007_199_254_740_991)

    def test_game_round_trip(self) -> None:
        expected = self.game_dict()
        game = GameRecord.from_dict(expected)

        self.assertEqual(game.to_dict(), expected)
        self.assertEqual(GameRecord.from_dict(game.to_dict()), game)
        self.assertEqual(json.loads(json.dumps(game.to_dict())), expected)
        warning = {"code": "stale", "message": "data is stale", "appid": 730}
        rejected = {
            "row_number": 4,
            "code": "invalid_appid",
            "message": "appid is invalid",
            "appid": None,
        }
        self.assertEqual(WarningRecord.from_dict(warning).to_dict(), warning)
        self.assertEqual(RejectedRow.from_dict(rejected).to_dict(), rejected)
        max_warning = {
            "code": "max_appid",
            "message": "maximum AppID",
            "appid": MAX_STEAM_APPID,
        }
        max_rejected = {
            "row_number": 5,
            "code": "max_appid",
            "message": "maximum AppID",
            "appid": MAX_STEAM_APPID,
        }
        max_warning_record = WarningRecord.from_dict(max_warning)
        max_rejected_record = RejectedRow.from_dict(max_rejected)
        self.assertEqual(max_warning_record.to_dict(), max_warning)
        self.assertEqual(max_rejected_record.to_dict(), max_rejected)
        self.assertIsInstance(json.dumps(max_warning_record.to_dict()), str)
        self.assertIsInstance(json.dumps(max_rejected_record.to_dict()), str)
        max_row_record = RejectedRow(
            MAX_JSON_SAFE_INTEGER,
            "max_row",
            "maximum portable row number",
        )
        self.assertIsInstance(json.dumps(max_row_record.to_dict()), str)
        with self.assertRaises(InputValidationError):
            RejectedRow(
                MAX_JSON_SAFE_INTEGER + 1,
                "invalid_row",
                "row number is not portable",
            )
        for optional_appid in (True, MAX_STEAM_APPID + 1):
            with self.subTest(
                record="warning", optional_appid=optional_appid
            ), self.assertRaises(InputValidationError):
                WarningRecord("invalid_appid", "invalid AppID", optional_appid)
            with self.subTest(
                record="rejected", optional_appid=optional_appid
            ), self.assertRaises(InputValidationError):
                RejectedRow(6, "invalid_appid", "invalid AppID", optional_appid)

        caller_metrics = {"current_players": MetricObservation.from_dict(self.metric_dict())}
        caller_extra = {"tags": ["FPS"], "nested": {"label": "before"}}
        isolated = GameRecord(
            schema_version=1,
            appid=730,
            name="Counter-Strike 2",
            release_status="released",
            store_url="https://store.steampowered.com/app/730",
            metrics=caller_metrics,
            source_extra=caller_extra,
        )
        caller_metrics.clear()
        caller_extra["tags"].append("Action")
        caller_extra["nested"]["label"] = "after"
        self.assertEqual(set(isolated.metrics), {"current_players"})
        self.assertEqual(
            isolated.to_dict()["source_extra"],
            {"tags": ["FPS"], "nested": {"label": "before"}},
        )
        with self.assertRaises(TypeError):
            isolated.metrics["followers"] = MetricObservation.from_dict(
                self.metric_dict()
            )

        safe_extra = {
            "bounds": [
                MIN_JSON_SAFE_INTEGER,
                {"maximum": MAX_JSON_SAFE_INTEGER, "flag": False},
            ]
        }
        safe_game = GameRecord(
            schema_version=1,
            appid=730,
            name="Counter-Strike 2",
            release_status="released",
            store_url="https://store.steampowered.com/app/730",
            metrics={},
            source_extra=safe_extra,
        )
        self.assertEqual(safe_game.to_dict()["source_extra"], safe_extra)
        self.assertEqual(
            json.loads(json.dumps(safe_game.to_dict()))["source_extra"],
            safe_extra,
        )
        for unsafe_extra in (
            {"nested": [MAX_JSON_SAFE_INTEGER + 1]},
            {"nested": [{"minimum": MIN_JSON_SAFE_INTEGER - 1}]},
        ):
            with self.subTest(unsafe_extra=unsafe_extra), self.assertRaises(
                InputValidationError
            ):
                GameRecord(
                    schema_version=1,
                    appid=730,
                    name="Counter-Strike 2",
                    release_status="released",
                    store_url="https://store.steampowered.com/app/730",
                    metrics={},
                    source_extra=unsafe_extra,
                )

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
        self.assertEqual(MAX_STEAM_APPID, 4_294_967_295)
        maximum = self.game_dict()
        maximum["appid"] = MAX_STEAM_APPID
        maximum["store_url"] = (
            f"https://store.steampowered.com/app/{MAX_STEAM_APPID}"
        )
        maximum_record = GameRecord.from_dict(maximum)
        self.assertEqual(maximum_record.appid, MAX_STEAM_APPID)
        self.assertIsInstance(json.dumps(maximum_record.to_dict()), str)

        for appid in (0, -1, True, "730", MAX_STEAM_APPID + 1):
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
