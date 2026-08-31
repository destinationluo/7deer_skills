from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import unified_game_radar.artifacts as artifacts_module
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

        expected_path = (
            self.root
            / "data/raw"
            / RUN_ID
            / "steam_official"
            / "steam_store.json"
        )
        self.assertEqual(Path(artifact.path), expected_path)
        self.assertEqual(expected_path.read_bytes(), expected_bytes)
        self.assertEqual(artifact.schema_version, 1)
        self.assertEqual(artifact.run_id, RUN_ID)
        self.assertEqual(artifact.provider, "steam_official")
        self.assertEqual(artifact.observed_at, NOW)
        self.assertEqual(artifact.sha256, hashlib.sha256(expected_bytes).hexdigest())

    def test_persist_uses_atomic_sibling_install(self) -> None:
        real_link = os.link
        installations: list[tuple[str, str, object, object]] = []

        def recording_link(source: object, target: object, **kwargs: object) -> None:
            installations.append(
                (
                    str(source),
                    str(target),
                    kwargs.get("src_dir_fd"),
                    kwargs.get("dst_dir_fd"),
                )
            )
            real_link(source, target, **kwargs)

        with mock.patch(
            "unified_game_radar.artifacts.os.link",
            side_effect=recording_link,
        ):
            artifact = self.persist({"safe": True})

        self.assertEqual(len(installations), 1)
        temporary, destination, source_parent, destination_parent = installations[0]
        self.assertTrue(temporary.startswith(".steam_store.json."))
        self.assertTrue(temporary.endswith(".tmp"))
        self.assertEqual(destination, "steam_store.json")
        self.assertEqual(source_parent, destination_parent)
        artifact_path = Path(artifact.path)
        self.assertEqual(list(artifact_path.parent.glob(".*.tmp")), [])
        self.assertEqual(json.loads(artifact_path.read_text("utf-8")), {"safe": True})

    @unittest.skipUnless(os.name == "posix", "secure descriptor assertions need POSIX")
    def test_persist_never_accepts_a_replaced_temporary_path(self) -> None:
        outside = self.root / "outside.json"
        outside.write_bytes(b"outside-must-not-be-linked")
        real_link = os.link

        def replace_temporary_before_link(
            source: object,
            target: object,
            **kwargs: object,
        ) -> None:
            source_dir_fd = kwargs.get("src_dir_fd")
            if source_dir_fd is None:
                source_path = Path(source)  # type: ignore[arg-type]
                source_path.unlink()
                source_path.symlink_to(outside)
            else:
                os.unlink(source, dir_fd=source_dir_fd)  # type: ignore[arg-type]
                os.symlink(
                    outside,
                    source,
                    dir_fd=source_dir_fd,
                )
            real_link(source, target, **kwargs)

        with mock.patch(
            "unified_game_radar.artifacts.os.link",
            side_effect=replace_temporary_before_link,
        ), self.assertRaises(PersistenceError):
            self.persist({"safe": True})

        self.assertEqual(outside.read_bytes(), b"outside-must-not-be-linked")
        destination = (
            self.root
            / "data/raw"
            / RUN_ID
            / "steam_official"
            / "steam_store.json"
        )
        self.assertFalse(destination.exists())

    @unittest.skipUnless(os.name == "posix", "secure descriptor assertions need POSIX")
    def test_persist_removes_a_hardlinked_temporary_replacement(self) -> None:
        outside = self.root / "outside.json"
        outside.write_bytes(b"outside-must-not-be-installed")
        real_link = os.link

        def replace_temporary_before_link(
            source: object,
            target: object,
            **kwargs: object,
        ) -> None:
            source_dir_fd = kwargs["src_dir_fd"]
            os.unlink(source, dir_fd=source_dir_fd)  # type: ignore[arg-type]
            real_link(
                outside,
                source,
                dst_dir_fd=source_dir_fd,
                follow_symlinks=False,
            )
            real_link(source, target, **kwargs)

        with mock.patch(
            "unified_game_radar.artifacts.os.link",
            side_effect=replace_temporary_before_link,
        ), self.assertRaises(PersistenceError):
            self.persist({"safe": True})

        self.assertEqual(outside.read_bytes(), b"outside-must-not-be-installed")
        destination = (
            self.root
            / "data/raw"
            / RUN_ID
            / "steam_official"
            / "steam_store.json"
        )
        self.assertFalse(destination.exists())

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

    @unittest.skipUnless(os.name == "posix", "secure descriptor assertions need POSIX")
    def test_identical_retry_revalidates_the_returned_pathname(self) -> None:
        artifact = self.persist({"safe": 1})
        destination = Path(artifact.path)
        provider_dir = destination.parent
        moved_provider_dir = provider_dir.with_name("steam_official-moved")
        real_read_existing_at = artifacts_module._read_existing_at
        swapped = False

        def swap_provider_then_read(
            parent_descriptor: int,
            name: str,
            expected: bytes,
        ):
            nonlocal swapped
            if not swapped:
                provider_dir.rename(moved_provider_dir)
                provider_dir.mkdir()
                (provider_dir / name).write_bytes(b"poison")
                swapped = True
            return real_read_existing_at(parent_descriptor, name, expected)

        with mock.patch(
            "unified_game_radar.artifacts._read_existing_at",
            side_effect=swap_provider_then_read,
        ), self.assertRaises(PersistenceError):
            self.persist({"safe": 1})

        self.assertEqual(destination.read_bytes(), b"poison")
        self.assertEqual(
            (moved_provider_dir / destination.name).read_bytes(),
            canonical_bytes({"safe": 1}),
        )

    @unittest.skipUnless(os.name == "posix", "secure descriptor assertions need POSIX")
    def test_new_install_revalidates_path_after_temporary_cleanup(self) -> None:
        provider_dir = (
            self.root / "data/raw" / RUN_ID / "steam_official"
        )
        moved_provider_dir = provider_dir.with_name("steam_official-moved")
        destination_name = "steam_store.json"
        real_cleanup = artifacts_module._cleanup_temporary_file
        swapped = False

        def cleanup_then_swap(
            parent_descriptor: int,
            name: str,
            *args: object,
        ) -> None:
            nonlocal swapped
            real_cleanup(parent_descriptor, name, *args)
            if not swapped:
                provider_dir.rename(moved_provider_dir)
                provider_dir.mkdir()
                (provider_dir / destination_name).write_bytes(b"poison")
                swapped = True

        with mock.patch(
            "unified_game_radar.artifacts._cleanup_temporary_file",
            side_effect=cleanup_then_swap,
        ), self.assertRaises(PersistenceError):
            self.persist({"safe": True})

        self.assertEqual((provider_dir / destination_name).read_bytes(), b"poison")
        self.assertEqual(
            (moved_provider_dir / destination_name).read_bytes(),
            canonical_bytes({"safe": True}),
        )

    @unittest.skipUnless(os.name == "posix", "secure descriptor assertions need POSIX")
    def test_failed_install_cleanup_never_unlinks_a_concurrent_replacement(
        self,
    ) -> None:
        destination_name = "steam_store.json"
        moved_name = "installed-by-this-call.json"
        replacement = b"unrelated-replacement"
        real_cleanup = artifacts_module._cleanup_installed_artifact
        replaced = False

        def replace_before_cleanup(
            parent_descriptor: int,
            name: str,
            *args: object,
        ) -> None:
            nonlocal replaced
            if not replaced:
                os.rename(
                    name,
                    moved_name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                try:
                    os.write(descriptor, replacement)
                finally:
                    os.close(descriptor)
                replaced = True
            real_cleanup(parent_descriptor, name, *args)

        with mock.patch(
            "unified_game_radar.artifacts._verify_installed_artifact",
            side_effect=PersistenceError("injected verification failure"),
        ), mock.patch(
            "unified_game_radar.artifacts._cleanup_installed_artifact",
            side_effect=replace_before_cleanup,
        ), self.assertRaises(PersistenceError):
            self.persist({"safe": True})

        provider_dir = (
            self.root / "data/raw" / RUN_ID / "steam_official"
        )
        self.assertTrue(replaced)
        self.assertEqual((provider_dir / destination_name).read_bytes(), replacement)
        self.assertEqual(
            (provider_dir / moved_name).read_bytes(),
            canonical_bytes({"safe": True}),
        )

    @unittest.skipUnless(os.name == "posix", "secure descriptor assertions need POSIX")
    def test_temporary_cleanup_never_unlinks_a_concurrent_replacement(self) -> None:
        replacement = b"unrelated-temporary-replacement"
        real_cleanup = artifacts_module._cleanup_temporary_file
        replacement_name: str | None = None

        def replace_before_cleanup(
            parent_descriptor: int,
            name: str,
            *args: object,
        ) -> None:
            nonlocal replacement_name
            moved_name = f"{name}.installed-by-this-call"
            os.rename(
                name,
                moved_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_descriptor,
            )
            try:
                os.write(descriptor, replacement)
            finally:
                os.close(descriptor)
            replacement_name = name
            real_cleanup(parent_descriptor, name, *args)

        with mock.patch(
            "unified_game_radar.artifacts._cleanup_temporary_file",
            side_effect=replace_before_cleanup,
        ):
            artifact = self.persist({"safe": True})

        provider_dir = Path(artifact.path).parent
        self.assertIsNotNone(replacement_name)
        self.assertEqual(
            (provider_dir / str(replacement_name)).read_bytes(),
            replacement,
        )
        self.assertEqual(
            Path(artifact.path).read_bytes(),
            canonical_bytes({"safe": True}),
        )

    @unittest.skipUnless(os.name == "posix", "secure descriptor assertions need POSIX")
    def test_poisoned_link_is_removed_from_the_official_name_when_unlink_fails(
        self,
    ) -> None:
        outside = self.root / "outside.json"
        outside.write_bytes(b"poison")
        destination_name = "steam_store.json"
        real_link = os.link
        real_unlink = os.unlink

        def replace_temporary_before_link(
            source: object,
            target: object,
            **kwargs: object,
        ) -> None:
            source_dir_fd = kwargs["src_dir_fd"]
            real_unlink(source, dir_fd=source_dir_fd)  # type: ignore[arg-type]
            os.symlink(outside, source, dir_fd=source_dir_fd)
            real_link(source, target, **kwargs)

        def reject_retired_unlink(path: object, **kwargs: object) -> None:
            if str(path).endswith(".cleanup.tmp"):
                raise OSError("injected retired-entry unlink failure")
            real_unlink(path, **kwargs)  # type: ignore[arg-type]

        with mock.patch(
            "unified_game_radar.artifacts.os.link",
            side_effect=replace_temporary_before_link,
        ), mock.patch(
            "unified_game_radar.artifacts.os.unlink",
            side_effect=reject_retired_unlink,
        ), self.assertRaises(PersistenceError):
            self.persist({"safe": True})

        destination = (
            self.root
            / "data/raw"
            / RUN_ID
            / "steam_official"
            / destination_name
        )
        self.assertFalse(os.path.lexists(destination))
        self.assertEqual(outside.read_bytes(), b"poison")

        recovered = self.persist({"safe": True})

        self.assertEqual(Path(recovered.path), destination)
        self.assertEqual(destination.read_bytes(), canonical_bytes({"safe": True}))

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

    def test_provider_budget_is_cumulative_per_run_and_provider(self) -> None:
        first = {"payload": "a" * 18}
        second = {"payload": "b" * 18}
        self.assertLess(len(canonical_bytes(first)), 50)
        self.assertLess(len(canonical_bytes(second)), 50)
        self.assertGreater(
            len(canonical_bytes(first)) + len(canonical_bytes(second)),
            50,
        )
        config = self.config(raw_max_bytes_per_provider=50)

        self.persist(first, config=config, artifact_name="first.json")
        with self.assertRaises(InputValidationError):
            self.persist(second, config=config, artifact_name="second.json")

        self.persist(
            second,
            config=config,
            provider="itch_browser",
            artifact_name="second.json",
        )
        provider_root = self.root / "data/raw" / RUN_ID
        self.assertTrue((provider_root / "steam_official/first.json").is_file())
        self.assertFalse((provider_root / "steam_official/second.json").exists())
        self.assertTrue((provider_root / "itch_browser/second.json").is_file())

    @unittest.skipUnless(os.name == "posix", "provider locking requires POSIX")
    def test_concurrent_provider_writes_cannot_oversubscribe_budget(self) -> None:
        config = self.config(raw_max_bytes_per_provider=50)
        provider_root = (
            config.data_dir / "raw" / RUN_ID / "steam_official"
        )
        barrier = threading.Barrier(2)

        def write(name: str, fill: str) -> object:
            barrier.wait(timeout=5)
            try:
                return self.persist(
                    {"payload": fill * 18},
                    config=config,
                    artifact_name=name,
                )
            except Exception as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(
                executor.map(
                    lambda arguments: write(*arguments),
                    (("first.json", "a"), ("second.json", "b")),
                )
            )

        self.assertEqual(
            sum(not isinstance(result, Exception) for result in results),
            1,
        )
        self.assertEqual(
            sum(isinstance(result, InputValidationError) for result in results),
            1,
        )
        artifacts = tuple(provider_root.glob("*.json"))
        self.assertEqual(len(artifacts), 1)
        self.assertLessEqual(
            sum(path.stat().st_size for path in artifacts),
            config.raw_max_bytes_per_provider,
        )

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
        run_dir = file_link_data / "raw" / RUN_ID / "steam_official"
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

    def test_prune_refuses_symlinked_data_dir_without_deleting_target(self) -> None:
        outside_data = self.root / "outside-data"
        outside_artifact = outside_data / "raw" / RUN_ID / "old.json"
        outside_artifact.parent.mkdir(parents=True)
        outside_artifact.write_text("{}", encoding="utf-8")
        old = (NOW - timedelta(days=15)).timestamp()
        os.utime(outside_artifact, (old, old))
        linked_data = self.root / "linked-data"
        linked_data.symlink_to(outside_data, target_is_directory=True)

        with self.assertRaises(PersistenceError):
            prune_raw_artifacts(
                self.config(data_dir=linked_data, raw_retention_days=14),
                NOW,
            )

        self.assertTrue(outside_artifact.is_file())

    def test_existing_artifact_close_failure_is_not_silently_ignored(self) -> None:
        path = self.root / "existing.json"
        expected = canonical_bytes({"safe": True})
        path.write_bytes(expected)

        with mock.patch(
            "unified_game_radar.artifacts.os.close",
            side_effect=OSError("private close detail"),
        ), self.assertRaises(PersistenceError) as captured:
            artifacts_module._read_existing(path, expected)

        self.assertNotIn("private close detail", str(captured.exception))

    def test_temporary_cleanup_failure_is_reported(self) -> None:
        real_unlink = os.unlink

        def fail_temporary_cleanup(path: object, **kwargs: object) -> None:
            if str(path).endswith(".tmp"):
                raise OSError("private cleanup detail")
            real_unlink(path, **kwargs)  # type: ignore[arg-type]

        with mock.patch(
            "unified_game_radar.artifacts.os.unlink",
            side_effect=fail_temporary_cleanup,
        ), self.assertRaises(PersistenceError) as captured:
            self.persist({"safe": True})

        self.assertNotIn("private cleanup detail", str(captured.exception))

    def test_filesystem_failures_are_mapped_and_temporary_file_is_cleaned(self) -> None:
        with mock.patch(
            "unified_game_radar.artifacts.os.link",
            side_effect=OSError("private disk detail"),
        ), self.assertRaises(PersistenceError) as captured:
            self.persist({"safe": True})

        self.assertNotIn("private disk detail", str(captured.exception))
        run_dir = self.root / "data/raw" / RUN_ID / "steam_official"
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
        run_dir = self.root / "data/raw" / RUN_ID / "steam_official"
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
