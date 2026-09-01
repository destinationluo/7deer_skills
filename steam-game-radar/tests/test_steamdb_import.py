from __future__ import annotations

import ast
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(PROJECT_DIR))

from steam_game_radar.artifacts import persist_raw
from steam_game_radar.config import RadarConfig
from steam_game_radar.errors import InputValidationError
from steam_game_radar.schemas import (
    MAX_JSON_SAFE_INTEGER,
    MAX_STEAM_APPID,
    MIN_JSON_SAFE_INTEGER,
)
from steam_game_radar.steamdb_import import (
    ImportResult,
    extract_appid,
    import_steamdb,
    parse_number,
    parse_release_date,
)


OBSERVED_AT = "2026-08-24T03:04:05Z"


class SteamDBImportTests(unittest.TestCase):
    def _write(self, directory: str, name: str, value: object) -> Path:
        path = Path(directory) / name
        if name.lower().endswith(".json"):
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        else:
            path.write_text(str(value), encoding="utf-8")
        return path

    def test_parse_number_accepts_missing_commas_plus_suffixes_and_percentages(self) -> None:
        cases = {
            None: None,
            "": None,
            "  ": None,
            "—": None,
            12: 12,
            12.0: 12,
            12.5: 12.5,
            "+1,234": 1234,
            "1.25K": 1250,
            "2m": 2_000_000,
            str(MAX_JSON_SAFE_INTEGER): MAX_JSON_SAFE_INTEGER,
            "000" + str(MAX_JSON_SAFE_INTEGER): MAX_JSON_SAFE_INTEGER,
            str(MAX_STEAM_APPID + 1): MAX_STEAM_APPID + 1,
            "87.5%": 87.5,
            "+1.2K%": 1200,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_number(value), expected)
        self.assertEqual(parse_number(MAX_JSON_SAFE_INTEGER), MAX_JSON_SAFE_INTEGER)
        self.assertEqual(
            parse_number(float(MAX_JSON_SAFE_INTEGER)),
            MAX_JSON_SAFE_INTEGER,
        )

    def test_parse_number_rejects_negative_nonfinite_bool_and_malformed_values(self) -> None:
        invalid = (
            True,
            False,
            -1,
            -0.5,
            math.inf,
            math.nan,
            [],
            MAX_JSON_SAFE_INTEGER + 1,
            float(MAX_JSON_SAFE_INTEGER + 1),
            str(MAX_JSON_SAFE_INTEGER + 1),
            "-1",
            "++1",
            "1,23",
            "1,,000",
            "1e3",
            "K",
            "1KM",
            "10%%",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(InputValidationError):
                parse_number(value)
        oversized_digits = "9" * 250_000
        with mock.patch(
            "steam_game_radar.steamdb_import.Decimal",
            side_effect=AssertionError("impossible integer reached conversion"),
        ), mock.patch(
            "steam_game_radar.steamdb_import._convert_bounded_digits",
            side_effect=AssertionError("impossible integer reached int conversion"),
        ), self.assertRaises(InputValidationError):
            parse_number(oversized_digits)

    def test_parse_release_date_normalizes_supported_formats_and_rejects_invalid(self) -> None:
        self.assertIsNone(parse_release_date(None))
        self.assertIsNone(parse_release_date(" "))
        self.assertIsNone(parse_release_date("—"))
        self.assertEqual(parse_release_date("2026-08-24"), "2026-08-24")
        self.assertEqual(parse_release_date("24 Aug 2026"), "2026-08-24")
        for value in ("2026-02-30", "1 Aug 2026", "24 august 2026", 20260824):
            with self.subTest(value=value), self.assertRaises(InputValidationError):
                parse_release_date(value)

    def test_extract_appid_accepts_every_alias_and_url_path_and_rejects_ambiguity(self) -> None:
        for alias in ("appid", " APP_ID ", "Steam_AppId"):
            with self.subTest(alias=alias):
                self.assertEqual(extract_appid({alias: "123"}), 123)
        for alias in ("url", " STEAMDB_URL ", "App_URL"):
            with self.subTest(alias=alias):
                self.assertEqual(
                    extract_appid(
                        {alias: "https://steamdb.info/app/456/charts/?week=1"}
                    ),
                    456,
                )
        self.assertEqual(
            extract_appid(
                {"appid": 789, "url": "https://steamdb.info/app/789/"}
            ),
            789,
        )
        self.assertEqual(
            extract_appid(
                {
                    "url": "https://steamdb.info/app/321/",
                    "app_url": "https://steamdb.info/app/321/charts/?week=1",
                }
            ),
            321,
        )
        max_text = str(MAX_STEAM_APPID)
        self.assertEqual(extract_appid({"appid": MAX_STEAM_APPID}), MAX_STEAM_APPID)
        self.assertEqual(extract_appid({"app_id": max_text}), MAX_STEAM_APPID)
        self.assertEqual(
            extract_appid({"app_id": "+000" + max_text}),
            MAX_STEAM_APPID,
        )
        self.assertEqual(
            extract_appid(
                {"url": "https://steamdb.info/app/000" + max_text + "/"}
            ),
            MAX_STEAM_APPID,
        )
        self.assertEqual(
            extract_appid(
                {
                    "appid": max_text,
                    "url": "https://steamdb.info/app/" + max_text + "/",
                }
            ),
            MAX_STEAM_APPID,
        )
        for row in (
            {"name": "Name only"},
            {"appid": True},
            {"appid": "1", "app_id": "2"},
            {"appid": "1", "url": "https://steamdb.info/app/2/"},
            {"url": "https://steamdb.info/app/1/", "app_url": "https://steamdb.info/app/2/charts/"},
            {"url": "https://steamdb.info/apps/1/"},
            {"appid": MAX_STEAM_APPID + 1},
            {"app_id": str(MAX_STEAM_APPID + 1)},
            {"url": f"https://steamdb.info/app/{MAX_STEAM_APPID + 1}/"},
        ):
            with self.subTest(row=row), self.assertRaises(InputValidationError):
                extract_appid(row)
        oversized_digits = "9" * 250_000
        with mock.patch(
            "steam_game_radar.steamdb_import.Decimal",
            side_effect=AssertionError("impossible AppID reached conversion"),
        ), mock.patch(
            "steam_game_radar.steamdb_import._convert_bounded_digits",
            side_effect=AssertionError("impossible AppID reached int conversion"),
        ):
            for oversized_row in (
                {"app_id": oversized_digits},
                {"url": "https://steamdb.info/app/" + oversized_digits + "/"},
            ):
                with self.subTest(oversized_row=tuple(oversized_row)), self.assertRaises(
                    InputValidationError
                ):
                    extract_appid(oversized_row)

    def test_wishlist_csv_fixture_maps_metrics_unknown_columns_and_persists_raw(self) -> None:
        result = import_steamdb(
            FIXTURES / "steamdb_wishlist.csv",
            "wishlist_activity",
            OBSERVED_AT,
        )
        self.assertEqual(result.view, "wishlist_activity")
        self.assertEqual([record.appid for record in result.records], [12345, 67890])
        first = result.records[0]
        self.assertEqual(first.name, "Wish Alpha")
        self.assertEqual(first.release_status, "unreleased")
        self.assertEqual(first.store_url, "https://store.steampowered.com/app/12345/")
        self.assertEqual(first.metrics["wishlist_gain_7d"].value, 1250)
        self.assertEqual(first.metrics["followers"].value, 2500)
        self.assertEqual(first.metrics["rating_percent"].value, 91)
        self.assertEqual(first.metrics["release_date"].value, "2026-09-10")
        self.assertEqual(
            first.metrics["release_date"].source_id,
            "steamdb_wishlist_activity_release_date",
        )
        self.assertEqual(
            first.metrics["release_date"].source_kind,
            "steamdb_manual_import",
        )
        self.assertEqual(first.metrics["release_date"].observed_at, OBSERVED_AT)
        for metric_name, metric in first.metrics.items():
            with self.subTest(metric_name=metric_name):
                self.assertEqual(metric.source_kind, "steamdb_manual_import")
                self.assertEqual(
                    metric.source_id,
                    f"steamdb_wishlist_activity_{metric_name}",
                )
                self.assertEqual(metric.observed_at, OBSERVED_AT)
        self.assertEqual(
            dict(first.source_extra),
            {"Note": "Editorial pick", "steamdb_view": "wishlist_activity"},
        )
        raw_one = result.raw_to_dict()
        raw_two = result.raw_to_dict()
        self.assertEqual(raw_one, raw_two)
        raw_one["rows"][0]["Note"] = "mutated"
        self.assertEqual(result.raw_to_dict()["rows"][0]["Note"], "Editorial pick")
        with tempfile.TemporaryDirectory() as directory:
            path = persist_raw(
                RadarConfig(data_dir=Path(directory)),
                "20260824T030405Z-1234abcd",
                "steamdb_manual",
                result.raw_to_dict(),
                datetime(2026, 8, 24, 3, 4, 5, tzinfo=timezone.utc),
            )
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["view"], "wishlist_activity")

    def test_json_wrapper_supplies_trending_followers_view_and_is_copy_isolated(self) -> None:
        result = import_steamdb(FIXTURES / "steamdb_views.json", None, OBSERVED_AT)
        self.assertEqual(result.view, "trending_followers")
        self.assertEqual(len(result.records), 1)
        record = result.records[0]
        self.assertEqual(record.appid, 24680)
        self.assertEqual(record.name, "Follower Rising")
        self.assertEqual(record.release_status, "unreleased")
        self.assertEqual(record.metrics["follower_gain_7d"].value, 1200)
        self.assertEqual(record.metrics["followers"].value, 2_500_000)
        self.assertEqual(dict(record.source_extra)["Campaign"], "organic")
        self.assertEqual(
            dict(record.source_extra)["Meta"],
            {" inner ": 2},
        )
        exported = result.raw_to_dict()
        self.assertEqual(exported["rows"][0]["Meta"], {" inner ": 2})
        exported["rows"][0]["title"] = "changed"
        exported["rows"][0]["Meta"][" inner "] = 99
        self.assertEqual(result.raw_to_dict()["rows"][0]["title"], "Follower Rising")
        self.assertEqual(result.raw_to_dict()["rows"][0]["Meta"], {" inner ": 2})

    def test_json_row_array_requires_explicit_view_and_imports_trending_games(self) -> None:
        rows = [{"appid": 11, "name": "Trending", "#": "+3", "online": "4K"}]
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "rows.json", rows)
            with self.assertRaises(InputValidationError):
                import_steamdb(path, None, OBSERVED_AT)
            result = import_steamdb(path, "trending_games", OBSERVED_AT)
            bom_path = Path(directory) / "bom.json"
            bom_path.write_bytes(
                b"\xef\xbb\xbf"
                + json.dumps(rows, ensure_ascii=False).encode("utf-8")
            )
            bom_result = import_steamdb(
                bom_path,
                "trending_games",
                OBSERVED_AT,
            )
            bounds_path = self._write(
                directory,
                "appid-bounds.json",
                [
                    {
                        "appid": MAX_STEAM_APPID,
                        "name": "Max",
                        "rank": 1,
                        "Minimum": MIN_JSON_SAFE_INTEGER,
                        "Maximum": MAX_JSON_SAFE_INTEGER,
                        "Fraction": 1.25,
                    },
                    {"appid": MAX_STEAM_APPID + 1, "name": "Native", "rank": 2},
                    {"app_id": str(MAX_STEAM_APPID + 1), "name": "String", "rank": 3},
                    {
                        "url": f"https://steamdb.info/app/{MAX_STEAM_APPID + 1}/",
                        "name": "URL",
                        "rank": 4,
                    },
                    {"appid": 35, "name": "Negative metric", "rank": -1},
                ],
            )
            bounds_result = import_steamdb(
                bounds_path,
                "trending_games",
                OBSERVED_AT,
            )
        self.assertEqual(result.records[0].release_status, "released")
        self.assertEqual(result.records[0].metrics["rank"].value, 3)
        self.assertEqual(result.records[0].metrics["current_players"].value, 4000)
        self.assertEqual(bom_result.records[0].appid, 11)
        self.assertEqual(
            [record.appid for record in bounds_result.records],
            [MAX_STEAM_APPID],
        )
        self.assertEqual(
            dict(bounds_result.records[0].source_extra),
            {
                "Minimum": MIN_JSON_SAFE_INTEGER,
                "Maximum": MAX_JSON_SAFE_INTEGER,
                "Fraction": 1.25,
                "steamdb_view": "trending_games",
            },
        )
        json.dumps(bounds_result.records[0].to_dict(), allow_nan=False)
        json.dumps(bounds_result.raw_to_dict(), allow_nan=False)
        self.assertEqual(
            [
                (row.row_number, row.code, row.appid)
                for row in bounds_result.rejected_rows
            ],
            [
                (2, "steamdb_row_invalid", None),
                (3, "steamdb_row_invalid", None),
                (4, "steamdb_row_invalid", None),
                (5, "steamdb_row_invalid", 35),
            ],
        )

    def test_recent_releases_requires_date_and_players_and_normalizes_date(self) -> None:
        rows = [
            {
                "app_url": "https://steamdb.info/app/22/",
                "game": "New Release",
                "release": "24 Aug 2026",
                "24h_peak": "1.5K",
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "recent.json", rows)
            result = import_steamdb(path, "recent_releases", OBSERVED_AT)
        record = result.records[0]
        self.assertEqual(record.release_status, "released")
        self.assertEqual(record.metrics["release_date"].value, "2026-08-24")
        self.assertEqual(record.metrics["peak_players"].value, 1500)

    def test_each_view_enforces_required_fields_and_name(self) -> None:
        cases = (
            ("trending_games", {"appid": 1, "name": "Game"}),
            ("wishlist_activity", {"appid": 2, "name": "Wish"}),
            ("trending_followers", {"appid": 3, "name": "Follow", "followers": 2}),
            ("recent_releases", {"appid": 4, "name": "Recent", "release_date": "2026-08-24"}),
            ("trending_games", {"appid": 5, "name": "  ", "rank": 1}),
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, (view, row) in enumerate(cases):
                with self.subTest(view=view, row=row):
                    path = self._write(directory, f"invalid-{index}.json", [row])
                    result = import_steamdb(path, view, OBSERVED_AT)
                    self.assertEqual(result.records, ())
                    self.assertEqual(result.rejected_rows[0].code, "steamdb_row_invalid")

    def test_json_wrapper_rejects_schema_drift_view_mismatch_and_nonmapping_rows(self) -> None:
        invalid_values = (
            ({"view": "trending_games", "rows": []}, None),
            ({"schema_version": True, "view": "trending_games", "rows": []}, None),
            ({"schema_version": 1.0, "view": "trending_games", "rows": []}, None),
            ({"schema_version": 0, "view": "trending_games", "rows": []}, None),
            ({"schema_version": -1, "view": "trending_games", "rows": []}, None),
            ({"schema_version": 2, "view": "trending_games", "rows": []}, None),
            ({"schema_version": 1, "view": "trending_games", "rows": [], "extra": 1}, None),
            ({"schema_version": 1, "view": "trending_games", "rows": []}, "recent_releases"),
            ({"schema_version": 1, "view": "trending_games", "rows": [1]}, None),
            ({"schema_version": 1, "view": "unknown", "rows": []}, None),
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, (value, view) in enumerate(invalid_values):
                with self.subTest(value=value, view=view), self.assertRaises(InputValidationError):
                    path = self._write(directory, f"wrapper-{index}.json", value)
                    import_steamdb(path, view, OBSERVED_AT)

    def test_csv_requires_explicit_view_header_and_valid_syntax_and_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            valid = self._write(directory, "valid.CSV", "appid,name,rank\n1,Game,1\n")
            with self.assertRaises(InputValidationError):
                import_steamdb(valid, None, OBSERVED_AT)
            self.assertEqual(len(import_steamdb(valid, "trending_games", OBSERVED_AT).records), 1)
            bom_csv = Path(directory) / "bom.csv"
            bom_csv.write_bytes(
                b"\xef\xbb\xbfappid,name,rank\n2,BOM Game,2\n"
            )
            self.assertEqual(
                import_steamdb(
                    bom_csv,
                    "trending_games",
                    OBSERVED_AT,
                ).records[0].appid,
                2,
            )
            max_csv = Path(directory) / "max.csv"
            max_csv.write_text(
                "app_id,name,rank,players_now\n"
                + str(MAX_STEAM_APPID)
                + ",Max CSV,1,"
                + str(MAX_JSON_SAFE_INTEGER)
                + "\n"
                + str(MAX_STEAM_APPID + 1)
                + ",Too Large AppID,2,1\n"
                + "123,Too Large Metric,3,"
                + str(MAX_JSON_SAFE_INTEGER + 1)
                + "\n",
                encoding="utf-8",
            )
            max_result = import_steamdb(
                max_csv,
                "trending_games",
                OBSERVED_AT,
            )
            self.assertEqual(max_result.records[0].appid, MAX_STEAM_APPID)
            self.assertEqual(
                max_result.records[0].metrics["current_players"].value,
                MAX_JSON_SAFE_INTEGER,
            )
            self.assertEqual(
                max_result.records[0].store_url,
                f"https://store.steampowered.com/app/{MAX_STEAM_APPID}/",
            )
            self.assertEqual(
                [
                    (row.row_number, row.code, row.appid)
                    for row in max_result.rejected_rows
                ],
                [
                    (2, "steamdb_row_invalid", None),
                    (3, "steamdb_row_invalid", 123),
                ],
            )
            json.dumps(max_result.records[0].to_dict(), allow_nan=False)
            json.dumps(max_result.raw_to_dict(), allow_nan=False)
            persistence_config = RadarConfig(
                data_dir=Path(directory) / "persisted-max"
            )
            persisted_raw = persist_raw(
                persistence_config,
                "20260824T030405Z-abcdef12",
                "steamdb_max_appid",
                max_result.raw_to_dict(),
                datetime(2026, 8, 24, 3, 4, 5, tzinfo=timezone.utc),
            )
            persisted_record = persist_raw(
                persistence_config,
                "20260824T030405Z-abcdef12",
                "steamdb_max_metric",
                max_result.records[0].to_dict(),
                datetime(2026, 8, 24, 3, 4, 5, tzinfo=timezone.utc),
            )
            self.assertTrue(persisted_raw.is_file())
            self.assertTrue(persisted_record.is_file())
            for name, content in (
                ("empty.csv", ""),
                ("broken.csv", 'appid,name,rank\n1,"unterminated,1\n'),
                ("input.txt", "appid,name,rank\n1,Game,1\n"),
            ):
                with self.subTest(name=name), self.assertRaises(InputValidationError):
                    path = self._write(directory, name, content)
                    import_steamdb(path, "trending_games", OBSERVED_AT)

    def test_file_safety_rejects_oversize_symlink_nonregular_and_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            too_large = root / "large.json"
            too_large.write_bytes(b" " * (5 * 1024 * 1024 + 1))
            exact_limit = root / "exact.json"
            exact_limit.write_bytes(b"[]" + b" " * (5 * 1024 * 1024 - 2))
            self.assertEqual(
                import_steamdb(
                    exact_limit,
                    "trending_games",
                    OBSERVED_AT,
                ).records,
                (),
            )
            target = self._write(directory, "target.json", [])
            symlink = root / "link.json"
            symlink.symlink_to(target)
            folder = root / "folder.json"
            folder.mkdir()
            fifo = root / "pipe.json"
            if hasattr(os, "mkfifo"):
                os.mkfifo(fifo)
            invalid_utf8 = root / "utf8.json"
            invalid_utf8.write_bytes(b"\xff")
            unsafe_paths = [
                too_large,
                symlink,
                folder,
                invalid_utf8,
                root / "missing.json",
                root / "nul\0.json",
            ]
            if fifo.exists():
                unsafe_paths.append(fifo)
            for path in unsafe_paths:
                with self.subTest(path=path), self.assertRaises(InputValidationError):
                    import_steamdb(path, "trending_games", OBSERVED_AT)

    def test_malformed_json_nonfinite_json_and_invalid_observed_at_are_overall_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            malformed = Path(directory) / "malformed.json"
            malformed.write_text('{"rows": [', encoding="utf-8")
            nonfinite = Path(directory) / "nonfinite.json"
            nonfinite.write_text('[{"appid":1,"name":"Game","rank":NaN}]', encoding="utf-8")
            overflow_float = Path(directory) / "overflow-float.json"
            overflow_float.write_text(
                '[{"appid":1,"name":"Game","rank":1,"Meta":1e400}]',
                encoding="utf-8",
            )
            too_positive = self._write(
                directory,
                "too-positive.json",
                [{"appid": 1, "name": "Game", "rank": 1, "Meta": MAX_JSON_SAFE_INTEGER + 1}],
            )
            too_negative = self._write(
                directory,
                "too-negative.json",
                [{"appid": 1, "name": "Game", "rank": 1, "Meta": MIN_JSON_SAFE_INTEGER - 1}],
            )
            oversized_json = Path(directory) / "oversized-integer.json"
            oversized_json.write_text(
                '[{"appid":'
                + "9" * 250_000
                + ',"name":"Oversized","rank":1}]',
                encoding="utf-8",
            )
            valid = self._write(directory, "valid.json", [{"appid": 1, "name": "Game", "rank": 1}])
            for path, observed_at in (
                (malformed, OBSERVED_AT),
                (nonfinite, OBSERVED_AT),
                (overflow_float, OBSERVED_AT),
                (too_positive, OBSERVED_AT),
                (too_negative, OBSERVED_AT),
                (valid, "2026-08-24T03:04:05+00:00"),
                (valid, "not-a-time"),
            ):
                with self.subTest(path=path, observed_at=observed_at), self.assertRaises(InputValidationError):
                    import_steamdb(path, "trending_games", observed_at)

            with mock.patch(
                "steam_game_radar.steamdb_import.Decimal",
                side_effect=AssertionError("impossible JSON integer reached conversion"),
            ), mock.patch(
                "steam_game_radar.steamdb_import._convert_bounded_digits",
                side_effect=AssertionError(
                    "impossible JSON integer reached int conversion"
                ),
            ), self.assertRaises(InputValidationError):
                import_steamdb(
                    oversized_json,
                    "trending_games",
                    OBSERVED_AT,
                )

            deep_value = "{\"x\":" * 600 + "0" + "}" * 600
            deep = Path(directory) / "deep.json"
            deep.write_text(
                '[{"appid":1,"name":"Deep","rank":1,"Meta":'
                + deep_value
                + "}]",
                encoding="utf-8",
            )
            with self.assertRaises(InputValidationError):
                import_steamdb(deep, "trending_games", OBSERVED_AT)

            cyclic: dict[str, object] = {}
            cyclic["self"] = cyclic
            with self.assertRaises(InputValidationError):
                ImportResult(
                    records=(),
                    rejected_rows=(),
                    raw_canonical=cyclic,
                    view="trending_games",
                )

            safe_raw = ImportResult(
                records=(),
                rejected_rows=(),
                raw_canonical={
                    "bounds": {
                        "minimum": MIN_JSON_SAFE_INTEGER,
                        "maximum": MAX_JSON_SAFE_INTEGER,
                    },
                    "flag": True,
                    "fraction": 1.5,
                },
                view="trending_games",
            )
            self.assertEqual(
                safe_raw.raw_to_dict(),
                {
                    "bounds": {
                        "minimum": MIN_JSON_SAFE_INTEGER,
                        "maximum": MAX_JSON_SAFE_INTEGER,
                    },
                    "flag": True,
                    "fraction": 1.5,
                },
            )
            json.dumps(safe_raw.raw_to_dict(), allow_nan=False)
            safe_path = persist_raw(
                RadarConfig(data_dir=Path(directory) / "safe-raw"),
                "20260824T030405Z-12345678",
                "steamdb_safe_integers",
                safe_raw.raw_to_dict(),
                datetime(2026, 8, 24, 3, 4, 5, tzinfo=timezone.utc),
            )
            self.assertTrue(safe_path.is_file())
            for unsafe_integer in (
                MIN_JSON_SAFE_INTEGER - 1,
                MAX_JSON_SAFE_INTEGER + 1,
            ):
                with self.subTest(unsafe_integer=unsafe_integer), self.assertRaises(
                    InputValidationError
                ):
                    ImportResult(
                        records=(),
                        rejected_rows=(),
                        raw_canonical={"nested": [{"unsafe": unsafe_integer}]},
                        view="trending_games",
                    )

    def test_duplicate_appids_reject_every_occurrence_and_preserve_other_order(self) -> None:
        rows = [
            {"appid": 1, "name": "First", "rank": 1},
            {"appid": 2, "name": "Middle", "rank": 2},
            {"app_url": "https://steamdb.info/app/1/charts/", "name": "Again", "online": 3},
            {"appid": 3, "name": "Last", "rank": 4},
        ]
        with tempfile.TemporaryDirectory() as directory:
            result = import_steamdb(
                self._write(directory, "duplicates.json", rows),
                "trending_games",
                OBSERVED_AT,
            )
        self.assertEqual([record.appid for record in result.records], [2, 3])
        self.assertEqual(
            [row.to_dict() for row in result.rejected_rows],
            [
                {"row_number": 1, "code": "steamdb_duplicate_appid", "message": "duplicate AppID in import", "appid": 1},
                {"row_number": 3, "code": "steamdb_duplicate_appid", "message": "duplicate AppID in import", "appid": 1},
            ],
        )
        self.assertEqual(len(result.raw_to_dict()["rows"]), 4)

    def test_aliases_conflicts_constraints_and_partial_rows_have_stable_rejections(self) -> None:
        rows = [
            {
                "APPID": 10,
                "title": "All Aliases",
                "rank": 1,
                "players_now": "10",
                "current": "10",
                "24h_peak": "20",
                "peak": "20",
                "followers": "30",
                "follows": "30",
                "7d_gain": "4",
                "followers_7d_gain": "4",
                "wishlist_7d_gain": "5",
                "wishlists_7d_gain": "5",
                "rating": "90%",
                "rating_percent": 90,
                "release": "2026-08-24",
                "release_date": "24 Aug 2026",
            },
            {"appid": 11, "name": "Conflict", "rank": 1, "#": 2},
            {"appid": 12, "name": "Bad rating", "rank": 2, "rating": "101%"},
            {"appid": 13, "name": "Bad rank", "rank": 1.5},
            {"appid": 14, "name": "Good", "online": 0},
        ]
        with tempfile.TemporaryDirectory() as directory:
            result = import_steamdb(
                self._write(directory, "partial.json", rows),
                "trending_games",
                OBSERVED_AT,
            )
        self.assertEqual([record.appid for record in result.records], [10, 14])
        self.assertEqual(result.records[0].metrics["release_date"].value, "2026-08-24")
        self.assertEqual(result.records[1].metrics["current_players"].value, 0)
        self.assertEqual(
            [(row.row_number, row.code, row.appid) for row in result.rejected_rows],
            [
                (2, "steamdb_row_invalid", 11),
                (3, "steamdb_row_invalid", 12),
                (4, "steamdb_row_invalid", 13),
            ],
        )

        reserved_cases = (
            ({"appid": 20, "name": "Reserved", "rank": 1, " steamdb_view ": " trending_games "}, True),
            ({"appid": 24, "name": "Repeated", "rank": 1, "steamdb_view": "trending_games", "STEAMDB_VIEW": " trending_games "}, True),
            ({"appid": 21, "name": "Wrong", "rank": 1, "steamdb_view": "recent_releases"}, False),
            ({"appid": 22, "name": "Typed", "rank": 1, "steamdb_view": 1}, False),
            ({"appid": 23, "name": "Conflict", "rank": 1, "steamdb_view": "trending_games", "STEAMDB_VIEW": "wishlist_activity"}, False),
        )
        numeric_syntax_cases = (
            {"appid": 30, "name": "Percent rank", "rank": "1%"},
            {"appid": 31, "name": "Percent count", "online": "10%"},
            {"appid": 32, "name": "Suffixed rating", "rank": 1, "rating": "0.1K%"},
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, (row, accepted) in enumerate(reserved_cases):
                with self.subTest(reserved=row, accepted=accepted):
                    reserved_result = import_steamdb(
                        self._write(directory, f"reserved-{index}.json", [row]),
                        "trending_games",
                        OBSERVED_AT,
                    )
                    if accepted:
                        self.assertEqual(len(reserved_result.records), 1)
                        self.assertEqual(
                            dict(reserved_result.records[0].source_extra),
                            {"steamdb_view": "trending_games"},
                        )
                    else:
                        self.assertEqual(reserved_result.records, ())
                        self.assertEqual(
                            reserved_result.rejected_rows[0].code,
                            "steamdb_row_invalid",
                        )
                    raw_row = reserved_result.raw_to_dict()["rows"][0]
                    for key, value in row.items():
                        if key.strip().casefold() == "steamdb_view":
                            self.assertEqual(raw_row[key.strip()], value)
            for index, row in enumerate(numeric_syntax_cases):
                with self.subTest(numeric_syntax=row):
                    syntax_result = import_steamdb(
                        self._write(directory, f"syntax-{index}.json", [row]),
                        "trending_games",
                        OBSERVED_AT,
                    )
                    self.assertEqual(syntax_result.records, ())
                    self.assertEqual(
                        syntax_result.rejected_rows[0].code,
                        "steamdb_row_invalid",
                    )

    def test_import_module_has_no_network_or_automated_steamdb_dependency(self) -> None:
        module_path = PROJECT_DIR / "steam_game_radar" / "steamdb_import.py"
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_roots = {"urllib", "http", "socket", "requests", "subprocess", "selenium", "playwright"}
        imported_roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])
        self.assertTrue(forbidden_roots.isdisjoint(imported_roots))
        self.assertNotIn("steamdb.info", source.lower())


if __name__ == "__main__":
    unittest.main()
