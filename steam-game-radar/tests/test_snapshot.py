from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from steam_game_radar.artifacts import atomic_write_json, persist_raw, prune_raw
from steam_game_radar.config import RadarConfig
from steam_game_radar.errors import InputValidationError, PersistenceError
import steam_game_radar.snapshot as snapshot_module
from steam_game_radar.schemas import (
    GameRecord,
    MAX_JSON_SAFE_INTEGER,
    MetricObservation,
)
from steam_game_radar.snapshot import (
    load_snapshots,
    make_run_id,
    persist_snapshot,
    select_comparison,
)


UTC = timezone.utc
SAFE_TEMP_DIR = str(Path(tempfile.gettempdir()).resolve())


class SnapshotTests(unittest.TestCase):
    def record(self, observed_at: str = "2026-08-24T03:04:05Z") -> GameRecord:
        return GameRecord(
            schema_version=1,
            appid=730,
            name="Counter-Strike 2",
            release_status="released",
            store_url="https://store.steampowered.com/app/730/",
            metrics={
                "current_players": MetricObservation(
                    value=1_234_567,
                    source_id="steam_current_players",
                    source_kind="steam_official",
                    observed_at=observed_at,
                )
            },
            source_extra={"genres": ["Action"]},
        )

    def comparison(self, observed_at: str, marker: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "run_id": "20260824T000000Z-1234abcd",
            "observed_at": observed_at,
            "records": [],
            "metadata": {"marker": marker},
        }

    def test_make_run_id_matches_utc_pattern(self) -> None:
        now = datetime.fromisoformat("2026-08-24T11:04:05+08:00")

        run_id = make_run_id(now, bytes.fromhex("12ab34cd"))

        self.assertEqual(run_id, "20260824T030405Z-12ab34cd")
        self.assertIsNotNone(
            re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", run_id)
        )
        for invalid_time, entropy in (
            (datetime(2026, 8, 24, 3, 4, 5), bytes.fromhex("12ab34cd")),
            (now, b"short"[:3]),
            (now, b"too-long"),
        ):
            with self.subTest(
                invalid_time=invalid_time, entropy=entropy
            ), self.assertRaises(InputValidationError):
                make_run_id(invalid_time, entropy)

    def test_same_day_snapshot_names_are_immutable(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            config = RadarConfig(data_dir=root)
            first_run = "20260824T030405Z-1234abcd"
            second_run = "20260824T030405Z-fedcba98"

            first = persist_snapshot(config, first_run, [self.record()], {"run": 1})
            second = persist_snapshot(
                config, second_run, [self.record()], {"run": 2}
            )
            original = first.read_bytes()

            self.assertNotEqual(first, second)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            with self.assertRaises(PersistenceError):
                persist_snapshot(config, first_run, [], {"run": "replacement"})
            self.assertEqual(first.read_bytes(), original)

            entry_replaced = False
            real_create_entry = snapshot_module._create_journal_entry

            def replace_created_entry(
                journal_descriptor: int,
                name: str,
                serialized: bytes,
            ) -> object:
                nonlocal entry_replaced
                result = real_create_entry(
                    journal_descriptor,
                    name,
                    serialized,
                )
                entry = first.parent / ".journal" / name
                original_inode = entry.stat().st_ino
                evil = json.loads(entry.read_text(encoding="utf-8"))
                evil["metadata"]["run"] = 9
                atomic_write_json(entry, evil)
                entry.chmod(0o600)
                self.assertNotEqual(entry.stat().st_ino, original_inode)
                self.assertEqual(len(entry.read_bytes()), len(serialized))
                entry_replaced = True
                return result

            replaced_run = "20260824T030406Z-dddddddd"
            with mock.patch.object(
                snapshot_module,
                "_create_journal_entry",
                side_effect=replace_created_entry,
            ), self.assertRaises(PersistenceError):
                persist_snapshot(config, replaced_run, [], {"run": 1})
            self.assertTrue(entry_replaced)
            self.assertFalse((first.parent / f"{replaced_run}.json").exists())

            journal = first.parent / ".journal"
            for run_id, final in ((first_run, first), (second_run, second)):
                journal_entry = journal / f"{run_id}.json"
                self.assertTrue(journal_entry.exists())
                self.assertEqual(final.stat().st_ino, journal_entry.stat().st_ino)
                self.assertEqual(final.stat().st_nlink, 2)
                self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o600)

            second.unlink()
            recovered = persist_snapshot(
                config,
                second_run,
                [self.record()],
                {"run": 2},
            )
            self.assertEqual(recovered, second)
            orphan = journal / "20260824T030406Z-eeeeeeee.json"
            orphan.write_bytes(b"incomplete journal entry")
            self.assertEqual(len(load_snapshots(config)), 2)

    def test_persist_snapshot_atomically_publishes_valid_json(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            config = RadarConfig(data_dir=root)
            run_id = "20260824T030404Z-99999999"
            journal_swapped = False
            real_create_entry = snapshot_module._create_journal_entry

            def swap_journal_directory(
                journal_descriptor: int,
                name: str,
                serialized: bytes,
            ) -> object:
                nonlocal journal_swapped
                result = real_create_entry(
                    journal_descriptor,
                    name,
                    serialized,
                )
                visible = root / "snapshots" / ".journal"
                visible.rename(root / "snapshots" / ".journal-displaced")
                visible.mkdir(mode=0o700)
                visible.chmod(0o700)
                journal_swapped = True
                return result

            with mock.patch.object(
                snapshot_module,
                "_create_journal_entry",
                side_effect=swap_journal_directory,
            ), self.assertRaises(PersistenceError):
                persist_snapshot(config, run_id, [], {"run": 1})
            self.assertTrue(journal_swapped)
            self.assertFalse((root / "snapshots" / f"{run_id}.json").exists())

        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            config = RadarConfig(data_dir=Path(directory))
            run_id = "20260824T030405Z-1234abcd"
            with mock.patch(
                "steam_game_radar.snapshot.os.open",
                wraps=os.open,
            ) as secure_open, mock.patch(
                "steam_game_radar.snapshot.os.link",
                wraps=os.link,
            ) as secure_link:
                path = persist_snapshot(
                    config,
                    run_id,
                    [self.record()],
                    {"capabilities": {"appdetails": True}},
                )

            create_calls = [
                call
                for call in secure_open.call_args_list
                if call.kwargs.get("dir_fd") is not None
                and call.args[1] & os.O_CREAT
                and call.args[1] & os.O_EXCL
                and call.args[1] & getattr(os, "O_NOFOLLOW", 0)
            ]
            self.assertTrue(create_calls)
            link_call = secure_link.call_args
            self.assertIsNotNone(link_call)
            self.assertIsInstance(link_call.kwargs.get("src_dir_fd"), int)
            self.assertIsInstance(link_call.kwargs.get("dst_dir_fd"), int)
            self.assertEqual(link_call.args[1], f"{run_id}.json")
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["run_id"],
                run_id,
            )
            self.assertEqual(
                sorted(entry.name for entry in path.parent.iterdir()),
                [".journal", f"{run_id}.json"],
            )
            journal_entry = path.parent / ".journal" / path.name
            self.assertEqual(path.stat().st_ino, journal_entry.stat().st_ino)
            self.assertEqual(path.stat().st_nlink, 2)
            self.assertEqual(stat.S_IMODE(journal_entry.stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE(journal_entry.parent.stat().st_mode),
                0o700,
            )
            self.assertFalse(
                any(
                    entry.name.startswith((".snapshot-", ".quarantine-"))
                    for entry in path.parent.iterdir()
                )
            )

        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            alias = root / "alias"
            alias.symlink_to(outside, target_is_directory=True)
            escaped_data = alias / "data"
            with self.assertRaises(PersistenceError):
                persist_snapshot(
                    RadarConfig(data_dir=escaped_data),
                    run_id,
                    [self.record()],
                    {"must_not_escape": True},
                )
            self.assertFalse((outside / "data").exists())

        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            restrictive = RadarConfig(data_dir=Path(directory))
            previous_umask = os.umask(0o777)
            try:
                restricted_path = persist_snapshot(
                    restrictive,
                    "20260824T030405Z-87654321",
                    [self.record()],
                    {"permissions": "strict"},
                )
            finally:
                os.umask(previous_umask)
            self.assertEqual(stat.S_IMODE(restricted_path.stat().st_mode), 0o600)
            self.assertEqual(len(load_snapshots(restrictive)), 1)

    def test_snapshot_has_versioned_canonical_schema(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            config = RadarConfig(data_dir=Path(directory))
            run_id = "20260824T030405Z-1234abcd"
            path = persist_snapshot(
                config,
                run_id,
                [self.record()],
                {
                    "mode": "official_scan",
                    "nested": {"safe": True},
                    "portable_integer": MAX_JSON_SAFE_INTEGER,
                },
            )

            payload = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(
                set(payload),
                {"schema_version", "run_id", "observed_at", "records", "metadata"},
            )
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["run_id"], run_id)
            self.assertEqual(payload["observed_at"], "2026-08-24T03:04:05Z")
            self.assertEqual(payload["records"], [self.record().to_dict()])
            self.assertEqual(
                payload["metadata"],
                {
                    "mode": "official_scan",
                    "nested": {"safe": True},
                    "portable_integer": MAX_JSON_SAFE_INTEGER,
                },
            )
            with self.assertRaises(InputValidationError):
                persist_snapshot(
                    config,
                    "20260824T030406Z-fedcba98",
                    [],
                    {"unsafe_integer": MAX_JSON_SAFE_INTEGER + 1},
                )

            payload["schema_version"] = 1.0
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(PersistenceError):
                load_snapshots(config)

            payload["schema_version"] = 1
            path.write_text(json.dumps(payload), encoding="utf-8")
            tampered = json.loads(path.read_text(encoding="utf-8"))
            tampered["metadata"]["mode"] = "atomic-replacement"
            atomic_write_json(path, tampered)
            path.chmod(0o600)
            journal_entry = path.parent / ".journal" / path.name
            self.assertEqual(path.stat().st_nlink, 1)
            self.assertEqual(journal_entry.stat().st_nlink, 1)
            self.assertNotEqual(path.stat().st_ino, journal_entry.stat().st_ino)
            with self.assertRaises(PersistenceError):
                load_snapshots(config)
            journal_entry.unlink()
            self.assertEqual(
                load_snapshots(config)[0]["metadata"]["mode"],
                "atomic-replacement",
            )

        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            config = RadarConfig(data_dir=Path(directory))
            cycle: dict[str, object] = {}
            cycle["self"] = cycle
            with self.assertRaises(InputValidationError):
                persist_snapshot(
                    config,
                    "20260824T030407Z-aaaaaaaa",
                    [],
                    cycle,
                )

            deep: dict[str, object] = {}
            cursor = deep
            for _ in range(500):
                child: dict[str, object] = {}
                cursor["nested"] = child
                cursor = child
            with self.assertRaises(InputValidationError):
                persist_snapshot(
                    config,
                    "20260824T030408Z-bbbbbbbb",
                    [],
                    deep,
                )

            run_id = "20260824T030409Z-cccccccc"
            path = persist_snapshot(config, run_id, [], {})
            deep_json = (
                '{"metadata":{"nested":'
                + "[" * 500
                + "0"
                + "]" * 500
                + '},"observed_at":"2026-08-24T03:04:09Z",'
                + '"records":[],"run_id":"'
                + run_id
                + '","schema_version":1}'
            )
            path.write_text(deep_json, encoding="utf-8")
            with self.assertRaises(PersistenceError):
                load_snapshots(config)

            huge_integer_json = (
                '{"metadata":{"huge":'
                + "9" * 50_000
                + '},"observed_at":"2026-08-24T03:04:09Z",'
                + '"records":[],"run_id":"'
                + run_id
                + '","schema_version":1}'
            )
            path.write_text(huge_integer_json, encoding="utf-8")
            real_int = int

            def reject_large_int_conversion(value: object, *args: object) -> int:
                if isinstance(value, str) and len(value) > 1_000:
                    raise AssertionError("oversized integer reached int()")
                return real_int(value, *args)

            with mock.patch(
                "builtins.int",
                side_effect=reject_large_int_conversion,
            ), self.assertRaises(PersistenceError):
                load_snapshots(config)

    def test_load_snapshots_sorts_by_explicit_utc_time_not_mtime(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            config = RadarConfig(data_dir=Path(directory))
            later = persist_snapshot(
                config,
                "20260824T030000Z-22222222",
                [self.record("2026-08-24T03:00:00Z")],
                {"order": 2, "origin": "inside"},
            )
            earlier = persist_snapshot(
                config,
                "20260823T230000Z-11111111",
                [self.record("2026-08-23T23:00:00Z")],
                {"order": 1},
            )
            os.utime(earlier, (2_000_000_000, 2_000_000_000))
            os.utime(later, (1_000_000_000, 1_000_000_000))
            loaded = load_snapshots(config)

            self.assertEqual(
                [snapshot["run_id"] for snapshot in loaded],
                [
                    "20260823T230000Z-11111111",
                    "20260824T030000Z-22222222",
                ],
            )
            self.assertEqual(loaded[0]["records"][0]["appid"], 730)
            self.assertEqual(loaded[1]["metadata"]["origin"], "inside")
            with self.assertRaises(TypeError):
                loaded[0]["metadata"]["mutated"] = True
            with self.assertRaises(TypeError):
                loaded[0]["records"][0]["metrics"]["current_players"][
                    "value"
                ] = 0
            with self.assertRaises(AttributeError):
                loaded[0]["records"].append({})

        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            config = RadarConfig(data_dir=root / "data")
            run_id = "20260824T030000Z-33333333"
            snapshot_path = persist_snapshot(
                config,
                run_id,
                [self.record("2026-08-24T03:00:00Z")],
                {"origin": "inside"},
            )
            replacement = json.loads(snapshot_path.read_text(encoding="utf-8"))
            replacement["metadata"]["origin"] = "outside"
            outside = root / "outside.json"
            atomic_write_json(outside, replacement)
            real_open = os.open
            swapped = False

            def swap_before_open(
                name: object,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal swapped
                if name == snapshot_path.name and dir_fd is not None and not swapped:
                    swapped = True
                    snapshot_path.unlink()
                    snapshot_path.symlink_to(outside)
                return real_open(name, flags, mode, dir_fd=dir_fd)

            with mock.patch(
                "steam_game_radar.snapshot.os.open",
                side_effect=swap_before_open,
            ), self.assertRaises(PersistenceError):
                load_snapshots(config)
            self.assertTrue(swapped)

    def test_one_day_window_includes_18_and_36_hour_boundaries(self) -> None:
        current = datetime(2026, 8, 24, 12, tzinfo=UTC)
        for hours in (18, 36):
            snapshot = self.comparison(
                (current - timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
                str(hours),
            )
            with self.subTest(hours=hours):
                self.assertIs(
                    select_comparison([snapshot], current, 24, 18, 36),
                    snapshot,
                )

    def test_seven_day_window_includes_144_and_192_hour_boundaries(self) -> None:
        current = datetime(2026, 8, 24, 12, tzinfo=UTC)
        for hours in (144, 192):
            snapshot = self.comparison(
                (current - timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
                str(hours),
            )
            with self.subTest(hours=hours):
                self.assertIs(
                    select_comparison([snapshot], current, 168, 144, 192),
                    snapshot,
                )

    def test_comparison_selects_snapshot_closest_to_target(self) -> None:
        current = datetime(2026, 8, 24, 12, tzinfo=UTC)
        farther = self.comparison("2026-08-23T05:00:00Z", "31h")
        closest = self.comparison("2026-08-23T11:00:00Z", "25h")

        result = select_comparison([farther, closest], current, 24, 18, 36)

        self.assertIs(result, closest)

    def test_comparison_tie_prefers_newer_observation(self) -> None:
        current = datetime(2026, 8, 24, 12, tzinfo=UTC)
        older = self.comparison("2026-08-23T11:00:00Z", "25h")
        newer = self.comparison("2026-08-23T13:00:00Z", "23h")

        result = select_comparison([older, newer], current, 24, 18, 36)

        self.assertIs(result, newer)

    def test_comparison_omits_snapshots_outside_window(self) -> None:
        current = datetime(2026, 8, 24, 12, tzinfo=UTC)
        snapshots = [
            self.comparison("2026-08-23T18:00:01Z", "under-18h"),
            self.comparison("2026-08-22T23:59:59Z", "over-36h"),
            self.comparison("2026-08-24T13:00:00Z", "future"),
        ]

        self.assertIsNone(
            select_comparison(snapshots, current, 24, 18, 36)
        )

    def test_manual_canonical_raw_uses_shared_redacted_copy(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            config = RadarConfig(data_dir=Path(directory))
            canonical = {
                "schema_version": 1,
                "view": "wishlist_activity",
                "rows": [
                    {"appid": 730, "name": "Counter-Strike 2", "api_token": "hidden"}
                ],
            }

            path = persist_raw(
                config,
                "20260824T030405Z-1234abcd",
                "steamdb_manual",
                canonical,
                datetime(2026, 8, 24, 3, 4, 5, tzinfo=UTC),
            )

            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["rows"][0]["api_token"], "[REDACTED]")
            self.assertEqual(stored["rows"][0]["name"], "Counter-Strike 2")
            self.assertEqual(canonical["rows"][0]["api_token"], "hidden")

    def test_raw_retention_and_size_stay_in_shared_artifact_module(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            now = datetime(2026, 8, 24, tzinfo=UTC)
            small_limit = RadarConfig(
                data_dir=root,
                raw_max_bytes_per_provider=24,
                raw_retention_days=14,
            )
            with self.assertRaises(InputValidationError):
                persist_raw(
                    small_limit,
                    "20260824T000000Z-1234abcd",
                    "steamdb_manual",
                    {"rows": ["x" * 100]},
                    now,
                )

            config = RadarConfig(data_dir=root, raw_retention_days=14)
            expired = persist_raw(
                config,
                "20260801T000000Z-abcdef12",
                "steamdb_manual",
                {"rows": []},
                now,
            )
            old_time = (now - timedelta(days=14, seconds=1)).timestamp()
            os.utime(expired, (old_time, old_time))

            self.assertEqual(prune_raw(config, now), [expired])
            self.assertFalse(expired.exists())


if __name__ == "__main__":
    unittest.main()
