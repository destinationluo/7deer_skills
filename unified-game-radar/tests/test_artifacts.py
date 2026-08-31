from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
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

from unified_game_radar.artifacts import (
    persist_raw_artifact,
    prune_raw_artifacts,
    redact_json,
)
from unified_game_radar.collectors.base import PendingRawPayload
from unified_game_radar.config import RadarConfig
from unified_game_radar.errors import (
    IdempotencyConflictError,
    InputValidationError,
    PersistenceError,
)
from unified_game_radar.schemas import MAX_SAFE_INTEGER


RUN_ID = "20260831T020000Z-a1b2c3d4"
NOW = datetime(2026, 8, 31, 2, tzinfo=timezone.utc)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class ArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def config(self, **changes: object) -> RadarConfig:
        values: dict[str, object] = {
            "data_dir": self.root / "data",
            "report_dir": self.root / "reports",
        }
        values.update(changes)
        return RadarConfig(**values)  # type: ignore[arg-type]

    def persist(
        self,
        payload: object,
        *,
        config: RadarConfig | None = None,
        run_id: str = RUN_ID,
        provider: str = "steam_official",
        artifact_name: str = "steam_store.json",
        observed_at: datetime = NOW,
    ):
        return persist_raw_artifact(
            config or self.config(),
            run_id,
            provider,
            artifact_name,
            payload,
            observed_at,
        )

    def test_redact_json_recursively_redacts_sensitive_key_fragments(self) -> None:
        source = {
            "safe": "visible",
            "ApiKey": "hidden",
            "nested": [
                {"refresh_token": "hidden", "count": 2},
                {"AUTHORIZATIONHeader": "hidden"},
                {"sessionCookieValue": "hidden"},
                {"client_secret_value": "hidden"},
            ],
        }

        redacted = redact_json(source)

        self.assertEqual(
            redacted,
            {
                "safe": "visible",
                "ApiKey": "[REDACTED]",
                "nested": [
                    {"refresh_token": "[REDACTED]", "count": 2},
                    {"AUTHORIZATIONHeader": "[REDACTED]"},
                    {"sessionCookieValue": "[REDACTED]"},
                    {"client_secret_value": "[REDACTED]"},
                ],
            },
        )
        self.assertEqual(source["ApiKey"], "hidden")

    def test_redact_json_accepts_frozen_pending_payloads(self) -> None:
        pending = PendingRawPayload(
            run_id=RUN_ID,
            provider="steam_official",
            artifact_name="steam_store.json",
            observed_at=NOW,
            payload={"rows": [{"authorization": "hidden", "safe": True}]},
        )

        self.assertEqual(
            redact_json(pending.payload),
            {"rows": [{"authorization": "[REDACTED]", "safe": True}]},
        )

    def test_redact_json_rejects_non_json_and_circular_values(self) -> None:
        cyclic: list[object] = []
        cyclic.append(cyclic)
        invalid = (
            {"unsafe": MAX_SAFE_INTEGER + 1},
            {"unsafe": float("nan")},
            {"unsafe": object()},
            {1: "non-string-key"},
            cyclic,
        )

        for value in invalid:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(
                InputValidationError
            ):
                redact_json(value)

    def test_persist_writes_redacted_canonical_json_and_returns_provenance(
        self,
    ) -> None:
        payload = {"z": "游戏", "api_token": "hidden", "a": [2, 1]}
        expected_value = {
            "a": [2, 1],
            "api_token": "[REDACTED]",
            "z": "游戏",
        }
        expected_bytes = canonical_bytes(expected_value)

        artifact = self.persist(payload)

        expected_path = self.root / "data/raw" / RUN_ID / "steam_store.json"
        self.assertEqual(Path(artifact.path), expected_path)
        self.assertEqual(expected_path.read_bytes(), expected_bytes)
        self.assertEqual(artifact.schema_version, 1)
        self.assertEqual(artifact.run_id, RUN_ID)
        self.assertEqual(artifact.provider, "steam_official")
        self.assertEqual(artifact.observed_at, NOW)
        self.assertEqual(artifact.sha256, hashlib.sha256(expected_bytes).hexdigest())

    def test_persist_uses_atomic_sibling_install(self) -> None:
        real_link = os.link
        installations: list[tuple[Path, Path]] = []

        def recording_link(source: object, target: object, **kwargs: object) -> None:
            del kwargs
            installations.append((Path(source), Path(target)))
            real_link(source, target)

        with mock.patch(
            "unified_game_radar.artifacts.os.link",
            side_effect=recording_link,
        ):
            artifact = self.persist({"safe": True})

        self.assertEqual(len(installations), 1)
        temporary, destination = installations[0]
        self.assertEqual(temporary.parent, destination.parent)
        self.assertEqual(destination, Path(artifact.path))
        self.assertFalse(temporary.exists())
        self.assertEqual(json.loads(destination.read_text("utf-8")), {"safe": True})

    def test_identical_retry_is_noop_and_changed_content_conflicts(self) -> None:
        first = self.persist({"safe": 1})
        path = Path(first.path)
        preserved_timestamp = (NOW - timedelta(days=2)).timestamp()
        os.utime(path, (preserved_timestamp, preserved_timestamp))
        before = path.stat().st_mtime_ns

        retried = self.persist({"safe": 1})

        self.assertEqual(retried, first)
        self.assertEqual(path.stat().st_mtime_ns, before)
        with self.assertRaises(IdempotencyConflictError):
            self.persist({"safe": 2})
        self.assertEqual(path.read_bytes(), canonical_bytes({"safe": 1}))

    def test_persist_enforces_original_and_redacted_byte_limits(self) -> None:
        with self.assertRaises(InputValidationError):
            self.persist(
                {"authorization": "x" * 100},
                config=self.config(raw_max_bytes_per_provider=40),
            )
        with self.assertRaises(InputValidationError):
            self.persist(
                {"key": "x"},
                config=self.config(raw_max_bytes_per_provider=15),
            )
        self.assertFalse((self.root / "data/raw").exists())

    def test_persist_rejects_traversal_identifiers_before_creating_raw_root(
        self,
    ) -> None:
        invalid_arguments = (
            {"run_id": "../20260831T020000Z-a1b2c3d4"},
            {"provider": "../steam"},
            {"artifact_name": "../steam.json"},
            {"artifact_name": "steam/secret.json"},
        )

        for changes in invalid_arguments:
            with self.subTest(changes=changes), self.assertRaises(
                InputValidationError
            ):
                self.persist({}, **changes)  # type: ignore[arg-type]
        self.assertFalse((self.root / "data/raw").exists())

    def test_persist_refuses_symlinked_raw_run_and_destination_paths(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()

        raw_link_data = self.root / "raw-link-data"
        raw_link_data.mkdir()
        (raw_link_data / "raw").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(PersistenceError):
            self.persist({}, config=self.config(data_dir=raw_link_data))

        run_link_data = self.root / "run-link-data"
        (run_link_data / "raw").mkdir(parents=True)
        (run_link_data / "raw" / RUN_ID).symlink_to(
            outside,
            target_is_directory=True,
        )
        with self.assertRaises(PersistenceError):
            self.persist({}, config=self.config(data_dir=run_link_data))

        file_link_data = self.root / "file-link-data"
        run_dir = file_link_data / "raw" / RUN_ID
        run_dir.mkdir(parents=True)
        outside_file = outside / "outside.json"
        outside_file.write_bytes(b"outside-must-survive")
        (run_dir / "steam_store.json").symlink_to(outside_file)
        with self.assertRaises(PersistenceError):
            self.persist({}, config=self.config(data_dir=file_link_data))
        self.assertEqual(outside_file.read_bytes(), b"outside-must-survive")

    def test_prune_is_limited_to_exact_raw_root_and_strict_older_than_cutoff(
        self,
    ) -> None:
        config = self.config(raw_retention_days=14)
        raw_run = config.data_dir / "raw" / RUN_ID
        raw_run.mkdir(parents=True)
        boundary = raw_run / "boundary.json"
        older = raw_run / "older.json"
        ignored = raw_run / "older.txt"
        adjacent = config.data_dir / "raw-backup" / "outside.json"
        adjacent.parent.mkdir(parents=True)
        for path in (boundary, older, ignored, adjacent):
            path.write_text("{}", encoding="utf-8")
        boundary_timestamp = (NOW - timedelta(days=14)).timestamp()
        older_timestamp = (NOW - timedelta(days=14, seconds=1)).timestamp()
        os.utime(boundary, (boundary_timestamp, boundary_timestamp))
        for path in (older, ignored, adjacent):
            os.utime(path, (older_timestamp, older_timestamp))

        removed = prune_raw_artifacts(config, NOW)

        self.assertEqual(removed, (older,))
        self.assertTrue(boundary.exists())
        self.assertFalse(older.exists())
        self.assertTrue(ignored.exists())
        self.assertTrue(adjacent.exists())

    def test_prune_refuses_symlinks_without_deleting_outside_content(self) -> None:
        config = self.config(raw_retention_days=14)
        raw_run = config.data_dir / "raw" / RUN_ID
        raw_run.mkdir(parents=True)
        outside = self.root / "outside.json"
        outside.write_text("outside", encoding="utf-8")
        (raw_run / "escape.json").symlink_to(outside)

        with self.assertRaises(PersistenceError):
            prune_raw_artifacts(config, NOW)

        self.assertEqual(outside.read_text("utf-8"), "outside")

    def test_filesystem_failures_are_mapped_and_temporary_file_is_cleaned(self) -> None:
        with mock.patch(
            "unified_game_radar.artifacts.os.link",
            side_effect=OSError("private disk detail"),
        ), self.assertRaises(PersistenceError) as captured:
            self.persist({"safe": True})

        self.assertNotIn("private disk detail", str(captured.exception))
        run_dir = self.root / "data/raw" / RUN_ID
        self.assertEqual(list(run_dir.glob(".*.tmp")), [])

        expired = run_dir / "expired.json"
        expired.write_text("{}", encoding="utf-8")
        old = (NOW - timedelta(days=15)).timestamp()
        os.utime(expired, (old, old))
        with mock.patch(
            "unified_game_radar.artifacts.os.scandir",
            side_effect=PermissionError("private traversal detail"),
        ), self.assertRaises(PersistenceError) as captured:
            prune_raw_artifacts(self.config(raw_retention_days=14), NOW)
        self.assertNotIn("private traversal detail", str(captured.exception))
        self.assertTrue(expired.exists())

    def test_concurrent_destination_removal_is_mapped_to_persistence_error(
        self,
    ) -> None:
        with mock.patch(
            "unified_game_radar.artifacts.os.link",
            side_effect=FileExistsError("destination raced"),
        ), self.assertRaises(PersistenceError) as captured:
            self.persist({"safe": True})

        self.assertNotIn("destination raced", str(captured.exception))
        run_dir = self.root / "data/raw" / RUN_ID
        self.assertEqual(list(run_dir.glob(".*.tmp")), [])

    @unittest.skipUnless(os.name == "posix", "secure descriptor assertions need POSIX")
    def test_persist_fsyncs_file_and_parent_directory(self) -> None:
        real_fsync = os.fsync
        synced_types: list[int] = []

        def recording_fsync(descriptor: int) -> None:
            synced_types.append(stat.S_IFMT(os.fstat(descriptor).st_mode))
            real_fsync(descriptor)

        with mock.patch(
            "unified_game_radar.artifacts.os.fsync",
            side_effect=recording_fsync,
        ):
            self.persist({"safe": True})

        self.assertIn(stat.S_IFREG, synced_types)
        self.assertIn(stat.S_IFDIR, synced_types)


if __name__ == "__main__":
    unittest.main()
