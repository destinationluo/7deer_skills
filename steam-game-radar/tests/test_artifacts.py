from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
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
from steam_game_radar.schemas import (
    MAX_JSON_SAFE_INTEGER,
    MIN_JSON_SAFE_INTEGER,
)


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
        shared = {"secret": "hidden"}
        self.assertEqual(
            redact([shared, shared]),
            [
                {"secret": "[REDACTED]"},
                {"secret": "[REDACTED]"},
            ],
        )

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

            expansion_data = Path(directory) / "redaction-expansion"
            expansion_config = RadarConfig(
                data_dir=expansion_data,
                raw_max_bytes_per_provider=15,
            )
            with self.assertRaises(InputValidationError):
                persist_raw(
                    expansion_config,
                    "20260824T030405Z-1234abcd",
                    "steam_store",
                    {"key": "x"},
                    datetime.now(timezone.utc),
                )
            self.assertFalse(expansion_data.exists())

            unsafe_integer_data = Path(directory) / "unsafe-integers"
            unsafe_integer_config = RadarConfig(data_dir=unsafe_integer_data)
            unsafe_values = (
                {"nested": [MAX_JSON_SAFE_INTEGER + 1]},
                {"nested": {"minimum": MIN_JSON_SAFE_INTEGER - 1}},
            )
            for unsafe_value in unsafe_values:
                with self.subTest(
                    redact_unsafe_integer=unsafe_value
                ), self.assertRaises(InputValidationError):
                    redact(unsafe_value)
                with self.subTest(
                    atomic_unsafe_integer=unsafe_value
                ), self.assertRaises(InputValidationError):
                    atomic_write_json(
                        unsafe_integer_data / "artifact.json",
                        unsafe_value,
                    )
                with self.subTest(
                    persist_unsafe_integer=unsafe_value
                ), self.assertRaises(InputValidationError):
                    persist_raw(
                        unsafe_integer_config,
                        "20260824T030405Z-1234abcd",
                        "steam_store",
                        unsafe_value,
                        datetime.now(timezone.utc),
                    )
            self.assertFalse(unsafe_integer_data.exists())

            cyclic_list: list[object] = []
            cyclic_list.append(cyclic_list)
            cyclic_dict: dict[str, object] = {}
            cyclic_dict["nested"] = cyclic_dict
            for cyclic_value in (cyclic_list, cyclic_dict):
                with self.subTest(
                    cyclic_type=type(cyclic_value).__name__
                ), self.assertRaises(InputValidationError):
                    redact(cyclic_value)
                with self.subTest(
                    persisted_cyclic_type=type(cyclic_value).__name__
                ), self.assertRaises(InputValidationError):
                    persist_raw(
                        config,
                        "20260824T030405Z-1234abcd",
                        "steam_store",
                        cyclic_value,
                        datetime.now(timezone.utc),
                    )

            deeply_nested: list[object] = []
            cursor = deeply_nested
            for _ in range(sys.getrecursionlimit() + 100):
                child: list[object] = []
                cursor.append(child)
                cursor = child
            with self.assertRaises(InputValidationError):
                atomic_write_json(
                    Path(directory) / "deeply-nested.json",
                    deeply_nested,
                )
            with self.assertRaises(InputValidationError):
                persist_raw(
                    config,
                    "20260824T030405Z-1234abcd",
                    "steam_store",
                    deeply_nested,
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
            real_fsync = os.fsync
            replacements: list[tuple[Path, Path]] = []
            fsynced_file_types: list[int] = []

            def recording_replace(source: object, target: object) -> None:
                replacements.append((Path(source), Path(target)))
                real_replace(source, target)

            def recording_fsync(file_descriptor: int) -> None:
                fsynced_file_types.append(
                    stat.S_IFMT(os.fstat(file_descriptor).st_mode)
                )
                real_fsync(file_descriptor)

            with mock.patch(
                "steam_game_radar.artifacts.os.replace",
                side_effect=recording_replace,
            ), mock.patch(
                "steam_game_radar.artifacts.os.fsync",
                side_effect=recording_fsync,
            ):
                atomic_write_json(path, {"z": 2, "a": "游戏"})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": "游戏", "z": 2})
            self.assertEqual(len(replacements), 1)
            source, target = replacements[0]
            self.assertEqual(source.parent, path.parent)
            self.assertEqual(target, path)
            self.assertFalse(source.exists())
            if os.name == "posix":
                self.assertIn(stat.S_IFREG, fsynced_file_types)
                self.assertIn(stat.S_IFDIR, fsynced_file_types)

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
                {
                    "safe": 1,
                    "api_key": "hidden",
                    "minimum": MIN_JSON_SAFE_INTEGER,
                    "maximum": MAX_JSON_SAFE_INTEGER,
                    "flag": True,
                    "ratio": 1.25,
                },
                now,
            )

            self.assertEqual(result, expected)
            self.assertEqual(
                json.loads(expected.read_text(encoding="utf-8")),
                {
                    "api_key": "[REDACTED]",
                    "flag": True,
                    "maximum": MAX_JSON_SAFE_INTEGER,
                    "minimum": MIN_JSON_SAFE_INTEGER,
                    "ratio": 1.25,
                    "safe": 1,
                },
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

            outside_raw = root / "outside-raw"
            outside_raw.mkdir()
            raw_link_data = root / "raw-link-data"
            raw_link_data.mkdir()
            (raw_link_data / "raw").symlink_to(
                outside_raw,
                target_is_directory=True,
            )
            with self.assertRaises(PersistenceError):
                persist_raw(
                    RadarConfig(data_dir=raw_link_data),
                    "20260824T030405Z-abcdef12",
                    "steam_store",
                    {"safe": True},
                    now,
                )
            self.assertEqual(list(outside_raw.iterdir()), [])

            run_link_data = root / "run-link-data"
            safe_raw = run_link_data / "raw"
            safe_raw.mkdir(parents=True)
            outside_run = root / "outside-run"
            outside_run.mkdir()
            run_id = "20260824T030405Z-fedcba98"
            (safe_raw / run_id).symlink_to(
                outside_run,
                target_is_directory=True,
            )
            with self.assertRaises(PersistenceError):
                persist_raw(
                    RadarConfig(data_dir=run_link_data),
                    run_id,
                    "steam_store",
                    {"safe": True},
                    now,
                )
            self.assertEqual(list(outside_run.iterdir()), [])

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
            safe_expired = raw_run / "a-safe-expired.json"
            safe_expired.write_text("{}", encoding="utf-8")
            expired_timestamp = (
                datetime(2026, 8, 24, tzinfo=timezone.utc)
                - timedelta(days=15)
            ).timestamp()
            os.utime(
                safe_expired,
                (expired_timestamp, expired_timestamp),
            )
            (raw_run / "z-escape.json").symlink_to(outside)
            config = RadarConfig(data_dir=root / "data")

            with self.assertRaises(PersistenceError):
                prune_raw(config, datetime(2026, 8, 24, tzinfo=timezone.utc))

            self.assertTrue(outside.exists())
            self.assertTrue(safe_expired.exists())

            race_data = root / "race-data"
            race_run = race_data / "raw/20260801T000000Z-abcdef12"
            race_run.mkdir(parents=True)
            expired = race_run / "victim.json"
            expired.write_bytes(b"inside")
            now = datetime(2026, 8, 24, tzinfo=timezone.utc)
            expired_timestamp = (now - timedelta(days=15)).timestamp()
            os.utime(expired, (expired_timestamp, expired_timestamp))
            outside_run = root / "outside-run"
            outside_run.mkdir()
            outside_victim = outside_run / expired.name
            outside_victim.write_bytes(b"outside-must-survive")
            outside_identity = outside_victim.stat().st_ino
            moved_run = race_run.with_name(f"{race_run.name}-moved")
            real_candidates = __import__(
                "steam_game_radar.artifacts",
                fromlist=["_prune_candidates"],
            )._prune_candidates

            def replace_run_after_enumeration(
                raw_root_descriptor: int,
                raw_root: Path,
            ) -> object:
                candidates = real_candidates(raw_root_descriptor, raw_root)
                race_run.rename(moved_run)
                race_run.symlink_to(outside_run, target_is_directory=True)
                return candidates

            with mock.patch(
                "steam_game_radar.artifacts._prune_candidates",
                side_effect=replace_run_after_enumeration,
            ):
                try:
                    prune_raw(
                        RadarConfig(
                            data_dir=race_data,
                            raw_retention_days=14,
                        ),
                        now,
                    )
                except PersistenceError:
                    pass

            self.assertEqual(outside_victim.read_bytes(), b"outside-must-survive")
            self.assertEqual(outside_victim.stat().st_ino, outside_identity)

            missing_data = root / "missing-data"
            self.assertEqual(
                prune_raw(
                    RadarConfig(data_dir=missing_data),
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                ),
                [],
            )

            file_data = root / "file-data"
            file_data.mkdir()
            (file_data / "raw").write_text("not a directory", encoding="utf-8")
            with self.assertRaises(PersistenceError):
                prune_raw(
                    RadarConfig(data_dir=file_data),
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )

            linked_data = root / "linked-data"
            linked_data.mkdir()
            outside_raw = root / "outside-raw"
            outside_raw.mkdir()
            linked_victim = outside_raw / "victim.json"
            linked_victim.write_bytes(b"linked-outside")
            (linked_data / "raw").symlink_to(
                outside_raw,
                target_is_directory=True,
            )
            with self.assertRaises(PersistenceError):
                prune_raw(
                    RadarConfig(data_dir=linked_data),
                    datetime(2026, 8, 24, tzinfo=timezone.utc),
                )
            self.assertEqual(linked_victim.read_bytes(), b"linked-outside")

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

            config = RadarConfig(data_dir=Path(directory), raw_retention_days=14)
            run_dir = (
                config.data_dir / "raw/20260801T000000Z-1234abcd"
            )
            run_dir.mkdir(parents=True)
            expired = run_dir / "expired.json"
            expired.write_text("{}", encoding="utf-8")
            now = datetime(2026, 8, 24, tzinfo=timezone.utc)
            expired_timestamp = (now - timedelta(days=15)).timestamp()
            os.utime(expired, (expired_timestamp, expired_timestamp))

            with mock.patch(
                "steam_game_radar.artifacts.os.scandir",
                side_effect=PermissionError("private traversal detail"),
            ), self.assertRaises(PersistenceError) as captured:
                prune_raw(config, now)

            self.assertNotIn("private traversal detail", str(captured.exception))
            self.assertTrue(expired.exists())


if __name__ == "__main__":
    unittest.main()
