from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from steam_game_radar.artifacts import (
    atomic_write_json,
    persist_raw,
    prune_raw,
    redact,
)
from steam_game_radar.config import RadarConfig
from steam_game_radar.errors import InputValidationError, PersistenceError


class ArtifactTests(unittest.TestCase):
    def test_redact_recurses_through_dicts_and_lists_case_insensitively(self) -> None:
        value = {
            "apiKey": "alpha",
            "nested": [
                {"Access_Token": "bravo", "safe": "visible"},
                {"AUTHORIZATION_header": "charlie"},
                {"sessionCookie": "delta"},
                {"clientSECRETvalue": "echo"},
            ],
        }

        self.assertEqual(
            redact(value),
            {
                "apiKey": "[REDACTED]",
                "nested": [
                    {"Access_Token": "[REDACTED]", "safe": "visible"},
                    {"AUTHORIZATION_header": "[REDACTED]"},
                    {"sessionCookie": "[REDACTED]"},
                    {"clientSECRETvalue": "[REDACTED]"},
                ],
            },
        )
        self.assertEqual(value["apiKey"], "alpha")

    def test_persist_raw_rejects_original_size_before_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = RadarConfig(
                data_dir=Path(directory),
                raw_max_bytes_per_provider=24,
            )
            with self.assertRaises(InputValidationError):
                persist_raw(
                    config,
                    "20260824T030405Z-1234abcd",
                    "steam_store",
                    {"secret": "x" * 100},
                    datetime.now(timezone.utc),
                )
            with self.assertRaises(InputValidationError):
                persist_raw(
                    config,
                    "20260824T030405Z-1234abcd",
                    "steam_store",
                    {"value": "\ud800"},
                    datetime.now(timezone.utc),
                )

            self.assertFalse((Path(directory) / "raw").exists())

    def test_atomic_write_json_uses_sibling_replace_and_leaves_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.json"
            real_replace = os.replace
            replacements: list[tuple[Path, Path]] = []

            def recording_replace(source: object, target: object) -> None:
                replacements.append((Path(source), Path(target)))
                real_replace(source, target)

            with mock.patch(
                "steam_game_radar.artifacts.os.replace",
                side_effect=recording_replace,
            ):
                atomic_write_json(path, {"z": 2, "a": "游戏"})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": "游戏", "z": 2})
            self.assertEqual(len(replacements), 1)
            source, target = replacements[0]
            self.assertEqual(source.parent, path.parent)
            self.assertEqual(target, path)
            self.assertFalse(source.exists())

    def test_persist_raw_validates_provider_filename(self) -> None:
        invalid_provider_ids = (
            "Steam",
            "_steam",
            "steam_",
            "steam.db",
            "../steam",
            "steam/db",
            "steam\\db",
            "steam token",
            "steam数据",
        )
        with tempfile.TemporaryDirectory() as directory:
            config = RadarConfig(data_dir=Path(directory))
            for provider_id in invalid_provider_ids:
                with self.subTest(provider_id=provider_id), self.assertRaises(
                    InputValidationError
                ):
                    persist_raw(
                        config,
                        "20260824T030405Z-1234abcd",
                        provider_id,
                        {},
                        datetime.now(timezone.utc),
                    )

    def test_persist_raw_validates_run_id_and_writes_redacted_contract_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RadarConfig(data_dir=root)
            now = datetime(2026, 8, 24, 3, 4, 5, tzinfo=timezone.utc)
            expected = root / "raw/20260824T030405Z-1234abcd/steam_store.json"

            result = persist_raw(
                config,
                "20260824T030405Z-1234abcd",
                "steam_store",
                {"safe": 1, "api_key": "hidden"},
                now,
            )

            self.assertEqual(result, expected)
            self.assertEqual(
                json.loads(expected.read_text(encoding="utf-8")),
                {"api_key": "[REDACTED]", "safe": 1},
            )
            for invalid_run_id in (
                "20260824T030405Z-1234ABCd",
                "20260824T030405-1234abcd",
                "20260824T030405Z-1234abc",
                "20261324T030405Z-1234abcd",
                "../20260824T030405Z-1234abcd",
            ):
                with self.subTest(run_id=invalid_run_id), self.assertRaises(
                    InputValidationError
                ):
                    persist_raw(config, invalid_run_id, "steam_store", {}, now)

    def test_prune_raw_removes_only_files_strictly_older_than_retention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RadarConfig(data_dir=root, raw_retention_days=14)
            raw = root / "raw/20260801T000000Z-1234abcd"
            raw.mkdir(parents=True)
            exactly_boundary = raw / "boundary.json"
            older = raw / "older.json"
            ignored = raw / "older.txt"
            for path in (exactly_boundary, older, ignored):
                path.write_text("{}", encoding="utf-8")
            now = datetime(2026, 8, 24, tzinfo=timezone.utc)
            boundary_timestamp = (now - timedelta(days=14)).timestamp()
            older_timestamp = (now - timedelta(days=14, seconds=1)).timestamp()
            os.utime(exactly_boundary, (boundary_timestamp, boundary_timestamp))
            os.utime(older, (older_timestamp, older_timestamp))
            os.utime(ignored, (older_timestamp, older_timestamp))

            removed = prune_raw(config, now)

            self.assertEqual(removed, [older])
            self.assertTrue(exactly_boundary.exists())
            self.assertFalse(older.exists())
            self.assertTrue(ignored.exists())

    def test_prune_raw_refuses_symlink_that_escapes_raw_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            raw_run = root / "data/raw/20260801T000000Z-1234abcd"
            raw_run.mkdir(parents=True)
            (raw_run / "escape.json").symlink_to(outside)
            config = RadarConfig(data_dir=root / "data")

            with self.assertRaises(PersistenceError):
                prune_raw(config, datetime(2026, 8, 24, tzinfo=timezone.utc))

            self.assertTrue(outside.exists())

            broken_data = root / "broken-data"
            broken_data.mkdir()
            (broken_data / "raw").symlink_to(root / "missing-raw-root")
            with self.assertRaises(PersistenceError):
                prune_raw(
                    RadarConfig(data_dir=broken_data),
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )

    def test_filesystem_errors_are_converted_to_persistence_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.json"
            with mock.patch(
                "steam_game_radar.artifacts.os.replace",
                side_effect=OSError("disk unavailable"),
            ), self.assertRaises(PersistenceError) as captured:
                atomic_write_json(path, {"safe": True})

            self.assertNotIn("disk unavailable", str(captured.exception))
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
