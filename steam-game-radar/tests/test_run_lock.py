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

from steam_game_radar.errors import (
    InputValidationError,
    PersistenceError,
    RunBusyError,
)
from steam_game_radar.run_lock import RunLock


NOW = datetime(2026, 8, 24, 3, 0, 0, 987654, tzinfo=timezone.utc)
RUN_ID = "20260824T030000Z-a1b2c3d4"


class RunLockTests(unittest.TestCase):
    def _lock(
        self,
        path: Path,
        *,
        run_id: str = RUN_ID,
        now: datetime = NOW,
        host: str = "worker-1",
        alive: bool = False,
    ) -> RunLock:
        return RunLock(
            path=path,
            run_id=run_id,
            now=lambda: now,
            hostname=lambda: host,
            pid_alive=lambda _pid: alive,
        )

    def _write_lock(
        self,
        path: Path,
        *,
        pid: int = 999_999,
        run_id: str = "20260823T000000Z-deadbeef",
        host: str = "worker-1",
        acquired_at: str = "2026-08-23T00:00:00Z",
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "pid": pid,
            "run_id": run_id,
            "host": host,
            "acquired_at": acquired_at,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return payload

    def test_payload_fields_permissions_and_constructor_callback_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "radar.lock"
            with mock.patch("steam_game_radar.run_lock.os.getpid", return_value=123):
                lock = self._lock(path, host="  worker-1  ")
                with lock:
                    self.assertEqual(
                        json.loads(path.read_text(encoding="utf-8")),
                        {
                            "pid": 123,
                            "run_id": RUN_ID,
                            "host": "worker-1",
                            "acquired_at": "2026-08-24T03:00:00Z",
                        },
                    )
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            self.assertFalse(path.exists())
            invalid_run_ids = (
                "",
                "20260824T030000Z-A1B2C3D4",
                "20260230T030000Z-a1b2c3d4",
                "20260824T250000Z-a1b2c3d4",
            )
            for run_id in invalid_run_ids:
                with self.subTest(run_id=run_id), self.assertRaises(
                    InputValidationError
                ):
                    self._lock(path, run_id=run_id)

            constructor_cases = (
                {"path": str(path)},
                {"now": NOW},
                {"hostname": "worker-1"},
                {"pid_alive": False},
            )
            for overrides in constructor_cases:
                arguments = {
                    "path": path,
                    "run_id": RUN_ID,
                    "now": lambda: NOW,
                    "hostname": lambda: "worker-1",
                    "pid_alive": lambda _pid: False,
                }
                arguments.update(overrides)
                with self.subTest(overrides=overrides), self.assertRaises(
                    InputValidationError
                ):
                    RunLock(**arguments)

            bad_callback_cases = (
                (lambda: datetime(2026, 8, 24, 3), lambda: "worker-1"),
                (lambda: "not a datetime", lambda: "worker-1"),
                (lambda: NOW, lambda: ""),
                (lambda: NOW, lambda: " worker/1 "),
                (lambda: NOW, lambda: "worker\\1"),
                (lambda: NOW, lambda: "worker\n1"),
                (lambda: NOW, lambda: "worker-\ud800"),
                (lambda: NOW, lambda: "w" * (64 * 1024)),
                (lambda: NOW, lambda: 123),
            )
            for now_callback, host_callback in bad_callback_cases:
                with self.subTest(
                    now=now_callback(), host=host_callback()
                ), self.assertRaises(InputValidationError):
                    with RunLock(
                        path=path,
                        run_id=RUN_ID,
                        now=now_callback,
                        hostname=host_callback,
                        pid_alive=lambda _pid: False,
                    ):
                        pass

    def test_exclusive_collision_and_conservative_existing_lock_handling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "radar.lock"
            first = self._lock(path)
            with first:
                with self.assertRaises(RunBusyError):
                    self._lock(path).__enter__()

            conservative_values = (
                b"not-json",
                b'{"pid":1}',
                b'{"pid":true,"run_id":"20260823T000000Z-deadbeef",'
                b'"host":"worker-1","acquired_at":"2026-08-23T00:00:00Z"}',
                b"\xff",
                b"x" * (64 * 1024 + 1),
            )
            for index, value in enumerate(conservative_values):
                candidate = root / f"malformed-{index}.lock"
                candidate.write_bytes(value)
                with self.subTest(index=index), self.assertRaises(RunBusyError):
                    self._lock(candidate).__enter__()
                self.assertEqual(candidate.read_bytes(), value)

            target = root / "target.lock"
            target.write_text("unchanged", encoding="utf-8")
            symlink = root / "symlink.lock"
            try:
                symlink.symlink_to(target)
            except (OSError, NotImplementedError):
                pass
            else:
                with self.assertRaises(RunBusyError):
                    self._lock(symlink).__enter__()
                self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

            future = root / "future.lock"
            self._write_lock(
                future,
                acquired_at="2026-08-24T03:00:01Z",
            )
            with self.assertRaises(RunBusyError):
                self._lock(future).__enter__()

    def test_context_manager_cleanup_missing_lock_and_double_exit_are_harmless(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.lock"
            lock = self._lock(path)
            returned = lock.__enter__()
            self.assertIs(returned, lock)
            self.assertTrue(path.exists())
            self.assertFalse(lock.__exit__(None, None, None))
            self.assertFalse(path.exists())
            self.assertFalse(lock.__exit__(None, None, None))

            missing = self._lock(path)
            missing.__enter__()
            path.unlink()
            self.assertFalse(missing.__exit__(None, None, None))
            self.assertFalse(path.exists())

    def test_cleanup_on_body_exception_does_not_suppress_or_mask_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.lock"
            with self.assertRaisesRegex(ValueError, "body failed"):
                with self._lock(path):
                    raise ValueError("body failed")
            self.assertFalse(path.exists())

            lock = self._lock(path)
            lock.__enter__()
            with mock.patch(
                "steam_game_radar.run_lock.os.unlink",
                side_effect=OSError("cleanup failed"),
            ):
                self.assertFalse(
                    lock.__exit__(ValueError, ValueError("body failed"), None)
                )
            self.assertTrue(path.exists())
            path.unlink()

            lock = self._lock(path)
            lock.__enter__()
            with mock.patch(
                "steam_game_radar.run_lock.os.unlink",
                side_effect=OSError("cleanup failed"),
            ), self.assertRaises(PersistenceError):
                lock.__exit__(None, None, None)
            self.assertTrue(path.exists())

            path.unlink()
            with mock.patch(
                "steam_game_radar.run_lock.os.fchmod",
                side_effect=OSError("mode failed"),
            ), self.assertRaises(PersistenceError):
                self._lock(path).__enter__()
            self.assertFalse(path.exists())

    def test_same_host_live_pid_remains_blocking_after_two_hours(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.lock"
            original = self._write_lock(path)
            for age in (timedelta(hours=2, seconds=1), timedelta(days=30)):
                with self.subTest(age=age), self.assertRaises(RunBusyError):
                    self._lock(
                        path,
                        now=datetime.fromisoformat(
                            str(original["acquired_at"]).replace("Z", "+00:00")
                        )
                        + age,
                        alive=True,
                    ).__enter__()
                self.assertEqual(json.loads(path.read_text()), original)

            callback_cases = (
                lambda _pid: 1,
                lambda _pid: (_ for _ in ()).throw(OSError("unknown")),
            )
            for callback in callback_cases:
                with self.subTest(callback=callback), self.assertRaises(RunBusyError):
                    RunLock(
                        path=path,
                        run_id=RUN_ID,
                        now=lambda: NOW,
                        hostname=lambda: "worker-1",
                        pid_alive=callback,
                    ).__enter__()

    def test_same_host_dead_stale_lock_is_reacquired_but_boundary_or_changed_lock_is_not_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            acquired_at = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)
            for seconds, stale in ((2 * 60 * 60, False), (2 * 60 * 60 + 1, True)):
                path = root / f"age-{seconds}.lock"
                old = self._write_lock(
                    path,
                    acquired_at=acquired_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
                lock = self._lock(path, now=acquired_at + timedelta(seconds=seconds))
                if stale:
                    with lock:
                        current = json.loads(path.read_text(encoding="utf-8"))
                        self.assertEqual(current["run_id"], RUN_ID)
                        self.assertNotEqual(current, old)
                    self.assertFalse(path.exists())
                else:
                    with self.assertRaises(RunBusyError):
                        lock.__enter__()
                    self.assertEqual(json.loads(path.read_text()), old)

            changed = root / "changed.lock"
            old = self._write_lock(changed)
            replacement = dict(old, run_id="20260822T000000Z-feedface")

            def replace_during_alive(_pid: int) -> bool:
                changed.unlink()
                changed.write_text(
                    json.dumps(replacement, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8",
                )
                return False

            lock = RunLock(
                path=changed,
                run_id=RUN_ID,
                now=lambda: NOW,
                hostname=lambda: "worker-1",
                pid_alive=replace_during_alive,
            )
            with self.assertRaises(RunBusyError):
                lock.__enter__()
            self.assertEqual(json.loads(changed.read_text()), replacement)

            owned = root / "owned.lock"
            lock = self._lock(owned)
            lock.__enter__()
            owned_payload = json.loads(owned.read_text())
            owned.unlink()
            new_owner = dict(owned_payload, run_id="20260824T030001Z-feedface")
            owned.write_text(json.dumps(new_owner), encoding="utf-8")
            self.assertFalse(lock.__exit__(None, None, None))
            self.assertEqual(json.loads(owned.read_text()), new_owner)

    def test_foreign_host_lock_remains_blocking_regardless_of_age(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "radar.lock"
            original = self._write_lock(
                path,
                host="worker-2",
                acquired_at="2020-01-01T00:00:00Z",
            )
            alive_calls: list[int] = []

            def record_alive(pid: int) -> bool:
                alive_calls.append(pid)
                return False

            with self.assertRaises(RunBusyError):
                RunLock(
                    path=path,
                    run_id=RUN_ID,
                    now=lambda: NOW,
                    hostname=lambda: "worker-1",
                    pid_alive=record_alive,
                ).__enter__()
            self.assertEqual(alive_calls, [])
            self.assertEqual(json.loads(path.read_text()), original)


if __name__ == "__main__":
    unittest.main()
