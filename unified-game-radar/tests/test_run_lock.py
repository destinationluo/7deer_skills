from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
from typing import Any, Optional


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from unified_game_radar.errors import (
    InputValidationError,
    PersistenceError,
    RunBusyError,
)
from unified_game_radar.run_lock import RunLock


NOW = datetime(2026, 8, 24, 3, 0, 0, 987654, tzinfo=timezone.utc)
RUN_ID = "20260824T030000Z-a1b2c3d4"
SAFE_TEMP_DIR = str(Path(tempfile.gettempdir()).resolve())


class _PausingRunLock(RunLock):
    def __init__(self, *args: Any, stale_read: Any, resume: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._stale_read_event = stale_read
        self._resume_event = resume

    def _collision_is_removable(self, *args: Any, **kwargs: Any) -> bool:
        removable = super()._collision_is_removable(*args, **kwargs)
        if removable:
            self._stale_read_event.set()
            if not self._resume_event.wait(10):
                raise RuntimeError("timed out waiting to resume stale recovery")
        return removable


class _GateSignalingRunLock(RunLock):
    def __init__(self, *args: Any, gate_attempt: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._gate_attempt_event = gate_attempt

    def _acquire_gate(self, parent_descriptor: int) -> int:
        self._gate_attempt_event.set()
        return super()._acquire_gate(parent_descriptor)


def _run_lock_process(
    path_text: str,
    run_id: str,
    role: str,
    stale_read: Any,
    resume: Any,
    gate_attempt: Any,
    owner_release: Any,
    results: Any,
) -> None:
    arguments = {
        "path": Path(path_text),
        "run_id": run_id,
        "now": lambda: NOW,
        "hostname": lambda: "worker-1",
        "pid_alive": lambda _pid: False,
    }
    if role == "b":
        lock = _PausingRunLock(
            **arguments,
            stale_read=stale_read,
            resume=resume,
        )
    else:
        lock = _GateSignalingRunLock(
            **arguments,
            gate_attempt=gate_attempt,
        )
    try:
        with lock:
            results.put((role, "entered"))
            if role == "b" and not owner_release.wait(10):
                raise RuntimeError("timed out holding recovered lock")
    except RunBusyError:
        results.put((role, "busy"))
    except Exception as exc:
        results.put((role, f"error:{type(exc).__name__}:{exc}"))


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
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            path = Path(directory) / "nested" / "radar.lock"
            with mock.patch("unified_game_radar.run_lock.os.getpid", return_value=123):
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
            gate = path.parent / ".unified-game-radar-run-lock.gate"
            self.assertTrue(gate.exists())
            self.assertEqual(stat.S_IMODE(gate.stat().st_mode), 0o600)

            checked = Path(directory) / "checked"
            checked.mkdir()
            parked = Path(directory) / "checked-opened"
            redirect_target = Path(directory) / "redirect-target"
            redirect_target.mkdir()
            swapped_path = checked / "nested" / "radar.lock"
            real_open = os.open
            swapped = False

            def swap_ancestor_after_open(
                file: object,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: Optional[int] = None,
            ) -> int:
                nonlocal swapped
                descriptor = real_open(file, flags, mode, dir_fd=dir_fd)
                if file == "checked" and not swapped:
                    swapped = True
                    checked.rename(parked)
                    checked.symlink_to(redirect_target, target_is_directory=True)
                return descriptor

            swapped_lock = self._lock(swapped_path)
            with mock.patch(
                "unified_game_radar.run_lock.os.open",
                side_effect=swap_ancestor_after_open,
            ):
                swapped_lock.__enter__()
            self.assertTrue(swapped)
            self.assertTrue((parked / "nested" / "radar.lock").exists())
            self.assertFalse((redirect_target / "nested").exists())
            checked.unlink()
            parked.rename(checked)
            self.assertFalse(swapped_lock.__exit__(None, None, None))
            self.assertFalse(swapped_path.exists())
            self.assertTrue(
                (checked / "nested" / ".unified-game-radar-run-lock.gate").exists()
            )

            original_working_directory = Path.cwd()
            relative_root = Path(directory) / "relative"
            alternate_working_directory = Path(directory) / "alternate-cwd"
            alternate_working_directory.mkdir()
            try:
                os.chdir(directory)
                relative_path = Path("relative/nested/radar.lock")
                relative_lock = self._lock(relative_path)
                relative_lock.__enter__()
                self.assertTrue(relative_path.exists())
                os.chdir(alternate_working_directory)
                self.assertFalse(relative_lock.__exit__(None, None, None))
            finally:
                os.chdir(original_working_directory)
            self.assertFalse((relative_root / "nested/radar.lock").exists())
            self.assertTrue(
                (relative_root / "nested/.unified-game-radar-run-lock.gate").exists()
            )
            self.assertFalse((alternate_working_directory / "relative").exists())

            for unsafe_path in (Path("../radar.lock"), Path("safe/../radar.lock")):
                with self.subTest(unsafe_path=unsafe_path), self.assertRaises(
                    InputValidationError
                ):
                    self._lock(unsafe_path)
            invalid_run_ids = (
                "",
                "20260824T030000Z-A1B2C3D4",
                "20260824T030000Z-1234567",
                f"20260824T030000Z-{'a' * 33}",
                "20260230T030000Z-a1b2c3d4",
                "20260824T250000Z-a1b2c3d4",
            )
            for run_id in invalid_run_ids:
                with self.subTest(run_id=run_id), self.assertRaises(
                    InputValidationError
                ):
                    self._lock(path, run_id=run_id)

            for suffix_length in (8, 9, 16, 32):
                with self.subTest(suffix_length=suffix_length):
                    self._lock(
                        path,
                        run_id=f"20260824T030000Z-{'a' * suffix_length}",
                    )

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
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            path = root / "radar.lock"
            first = self._lock(path)
            with first:
                with self.assertRaises(RunBusyError):
                    self._lock(path).__enter__()

            conservative_values = (
                b"not-json",
                b'{"pid":1}',
                b'{"pid":1,"pid":2,'
                b'"run_id":"20260823T000000Z-deadbeef",'
                b'"host":"worker-1","acquired_at":"2026-08-23T00:00:00Z"}',
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

            state_directory = root / "hardlink-state"
            state_directory.mkdir()
            victim = root / "outside-gate-victim.txt"
            victim.write_text("do-not-touch", encoding="utf-8")
            victim.chmod(0o644)
            hardlinked_gate = state_directory / ".unified-game-radar-run-lock.gate"
            os.link(victim, hardlinked_gate)
            victim_before = victim.stat()
            with self.assertRaises(PersistenceError):
                self._lock(state_directory / "radar.lock").__enter__()
            victim_after = victim.stat()
            self.assertEqual(victim.read_text(encoding="utf-8"), "do-not-touch")
            self.assertEqual(stat.S_IMODE(victim_after.st_mode), 0o644)
            self.assertEqual(victim_after.st_ino, victim_before.st_ino)
            self.assertEqual(victim_after.st_nlink, 2)
            self.assertEqual(hardlinked_gate.stat().st_ino, victim_before.st_ino)
            self.assertFalse((state_directory / "radar.lock").exists())

            wrong_mode_state = root / "wrong-mode-state"
            wrong_mode_state.mkdir()
            wrong_mode_gate = wrong_mode_state / ".unified-game-radar-run-lock.gate"
            wrong_mode_gate.write_bytes(b"")
            wrong_mode_gate.chmod(0o640)
            with self.assertRaises(PersistenceError):
                self._lock(wrong_mode_state / "radar.lock").__enter__()
            self.assertEqual(stat.S_IMODE(wrong_mode_gate.stat().st_mode), 0o640)
            self.assertFalse((wrong_mode_state / "radar.lock").exists())

    def test_context_manager_cleanup_missing_lock_and_double_exit_are_harmless(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            path = Path(directory) / "radar.lock"

            explicit = self._lock(
                path,
                run_id=f"20260824T030000Z-{'a' * 32}",
            )
            self.assertIs(explicit.acquire(), explicit)
            try:
                self.assertTrue(path.exists())
            finally:
                explicit.release()
            self.assertFalse(path.exists())
            self.assertIsNone(explicit.release())

            lock = self._lock(path)
            returned = lock.__enter__()
            self.assertIs(returned, lock)
            self.assertTrue(path.exists())
            outer_payload = path.read_bytes()
            with self.assertRaises(RunBusyError):
                lock.__enter__()
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), outer_payload)
            self.assertFalse(lock.__exit__(None, None, None))
            self.assertFalse(path.exists())
            self.assertFalse(lock.__exit__(None, None, None))

            missing = self._lock(path)
            missing.__enter__()
            path.unlink()
            self.assertFalse(missing.__exit__(None, None, None))
            self.assertFalse(path.exists())

    def test_release_retries_after_transient_gate_or_read_failure(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)

            gate_path = root / "gate-failure.lock"
            gate_lock = self._lock(gate_path)
            gate_lock.acquire()
            with mock.patch.object(
                gate_lock,
                "_acquire_gate",
                side_effect=PersistenceError("transient gate failure"),
            ), self.assertRaisesRegex(PersistenceError, "transient gate failure"):
                gate_lock.release()
            self.assertTrue(gate_path.exists())
            with self.assertRaises(RunBusyError):
                self._lock(gate_path).__enter__()
            gate_lock.release()
            self.assertFalse(gate_path.exists())

            read_path = root / "read-failure.lock"
            read_lock = self._lock(read_path)
            read_lock.acquire()
            with mock.patch(
                "unified_game_radar.run_lock.os.read",
                side_effect=OSError("transient read failure"),
            ), self.assertRaisesRegex(
                PersistenceError,
                "cannot safely inspect owned run lock",
            ):
                read_lock.release()
            self.assertTrue(read_path.exists())
            with self.assertRaises(RunBusyError):
                self._lock(read_path).__enter__()
            read_lock.release()
            self.assertFalse(read_path.exists())

            removal_path = root / "removal-failure.lock"
            removal_lock = self._lock(removal_path)
            removal_lock.acquire()
            with mock.patch(
                "unified_game_radar.run_lock.os.rename",
                side_effect=OSError("transient quarantine failure"),
            ), self.assertRaisesRegex(
                PersistenceError,
                "cannot release owned run lock atomically",
            ):
                removal_lock.release()
            self.assertTrue(removal_path.exists())
            with self.assertRaises(RunBusyError):
                self._lock(removal_path).__enter__()
            removal_lock.release()
            self.assertFalse(removal_path.exists())

    def test_cleanup_on_body_exception_does_not_suppress_or_mask_it(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            path = Path(directory) / "radar.lock"
            with self.assertRaisesRegex(ValueError, "body failed"):
                with self._lock(path):
                    raise ValueError("body failed")
            self.assertFalse(path.exists())

            lock = self._lock(path)
            lock.__enter__()
            with mock.patch(
                "unified_game_radar.run_lock.os.unlink",
                side_effect=OSError("cleanup failed"),
            ):
                self.assertFalse(
                    lock.__exit__(ValueError, ValueError("body failed"), None)
                )
            quarantines = list(Path(directory).glob(".unified-game-radar-lock-*.quarantine"))
            self.assertFalse(path.exists())
            self.assertEqual(len(quarantines), 1)
            self.assertEqual(json.loads(quarantines[0].read_text()), {
                "pid": os.getpid(),
                "run_id": RUN_ID,
                "host": "worker-1",
                "acquired_at": "2026-08-24T03:00:00Z",
            })
            lock.release()
            quarantines[0].unlink()

            lock = self._lock(path)
            lock.__enter__()
            with mock.patch(
                "unified_game_radar.run_lock.os.unlink",
                side_effect=OSError("cleanup failed"),
            ), self.assertRaises(PersistenceError):
                lock.__exit__(None, None, None)
            quarantines = list(Path(directory).glob(".unified-game-radar-lock-*.quarantine"))
            self.assertFalse(path.exists())
            self.assertEqual(len(quarantines), 1)

            lock.release()
            quarantines[0].unlink()
            real_fchmod = os.fchmod
            fchmod_calls = 0

            def fail_canonical_fchmod(descriptor: int, mode: int) -> None:
                nonlocal fchmod_calls
                fchmod_calls += 1
                if fchmod_calls == 1:
                    raise OSError("mode failed")
                real_fchmod(descriptor, mode)

            with mock.patch(
                "unified_game_radar.run_lock.os.fchmod",
                side_effect=fail_canonical_fchmod,
            ), self.assertRaises(PersistenceError):
                self._lock(path).__enter__()
            self.assertFalse(path.exists())
            self.assertEqual(fchmod_calls, 1)

            real_rename = os.rename
            before_move = Path(directory) / "cleanup-before-move.lock"
            lock = self._lock(before_move)
            lock.__enter__()
            replacement_before = {
                "pid": 333_333,
                "run_id": "20260824T030001Z-feedface",
                "host": "worker-2",
                "acquired_at": "2026-08-24T03:00:01Z",
            }
            replacement_before_inode: list[int] = []

            def replace_before_cleanup_move(
                source: object,
                destination: object,
                *args: object,
                **kwargs: object,
            ) -> None:
                before_move.unlink()
                before_move.write_text(
                    json.dumps(replacement_before), encoding="utf-8"
                )
                replacement_before_inode.append(before_move.stat().st_ino)
                real_rename(source, destination, *args, **kwargs)

            with mock.patch(
                "unified_game_radar.run_lock.os.rename",
                side_effect=replace_before_cleanup_move,
            ), self.assertRaises(PersistenceError):
                lock.__exit__(None, None, None)
            self.assertEqual(json.loads(before_move.read_text()), replacement_before)
            self.assertEqual(before_move.stat().st_ino, replacement_before_inode[0])
            lock.release()
            self.assertEqual(json.loads(before_move.read_text()), replacement_before)

            after_move = Path(directory) / "cleanup-after-move.lock"
            lock = self._lock(after_move)
            lock.__enter__()
            replacement_after = {
                "pid": 444_444,
                "run_id": "20260824T030002Z-cafebabe",
                "host": "worker-2",
                "acquired_at": "2026-08-24T03:00:02Z",
            }
            replacement_after_inode: list[int] = []

            def replace_after_cleanup_move(
                source: object,
                destination: object,
                *args: object,
                **kwargs: object,
            ) -> None:
                real_rename(source, destination, *args, **kwargs)
                after_move.write_text(
                    json.dumps(replacement_after), encoding="utf-8"
                )
                replacement_after_inode.append(after_move.stat().st_ino)

            with mock.patch(
                "unified_game_radar.run_lock.os.rename",
                side_effect=replace_after_cleanup_move,
            ):
                self.assertFalse(lock.__exit__(None, None, None))
            self.assertEqual(json.loads(after_move.read_text()), replacement_after)
            self.assertEqual(after_move.stat().st_ino, replacement_after_inode[0])

    def test_same_host_live_pid_remains_blocking_after_two_hours(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
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
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
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

            real_rename = os.rename
            before_move = root / "stale-before-move.lock"
            self._write_lock(before_move)
            replacement_before = {
                "pid": 555_555,
                "run_id": "20260824T030003Z-abcdef12",
                "host": "worker-2",
                "acquired_at": "2026-08-24T03:00:03Z",
            }
            replacement_before_inode: list[int] = []

            def replace_before_stale_move(
                source: object,
                destination: object,
                *args: object,
                **kwargs: object,
            ) -> None:
                before_move.unlink()
                before_move.write_text(
                    json.dumps(replacement_before), encoding="utf-8"
                )
                replacement_before_inode.append(before_move.stat().st_ino)
                real_rename(source, destination, *args, **kwargs)

            with mock.patch(
                "unified_game_radar.run_lock.os.rename",
                side_effect=replace_before_stale_move,
            ), self.assertRaises(RunBusyError):
                self._lock(before_move).__enter__()
            self.assertEqual(json.loads(before_move.read_text()), replacement_before)
            self.assertEqual(before_move.stat().st_ino, replacement_before_inode[0])

            after_move = root / "stale-after-move.lock"
            self._write_lock(after_move)
            replacement_after = {
                "pid": 666_666,
                "run_id": "20260824T030004Z-1234abcd",
                "host": "worker-1",
                "acquired_at": "2026-08-23T00:00:00Z",
            }
            replacement_after_inode: list[int] = []

            def replace_after_stale_move(
                source: object,
                destination: object,
                *args: object,
                **kwargs: object,
            ) -> None:
                real_rename(source, destination, *args, **kwargs)
                after_move.write_text(
                    json.dumps(replacement_after), encoding="utf-8"
                )
                replacement_after_inode.append(after_move.stat().st_ino)

            with mock.patch(
                "unified_game_radar.run_lock.os.rename",
                side_effect=replace_after_stale_move,
            ), self.assertRaises(RunBusyError):
                self._lock(after_move).__enter__()
            self.assertEqual(json.loads(after_move.read_text()), replacement_after)
            self.assertEqual(after_move.stat().st_ino, replacement_after_inode[0])

            if "fork" not in multiprocessing.get_all_start_methods():
                self.skipTest("requires multiprocessing fork and fcntl")
            process_path = root / "multiprocess.lock"
            self._write_lock(process_path)
            context = multiprocessing.get_context("fork")
            stale_read = context.Event()
            resume = context.Event()
            owner_release = context.Event()
            b_gate_attempt = context.Event()
            c_gate_attempt = context.Event()
            d_gate_attempt = context.Event()
            results = context.Queue()
            processes = [
                context.Process(
                    target=_run_lock_process,
                    args=(
                        str(process_path),
                        "20260824T030010Z-0000000b",
                        "b",
                        stale_read,
                        resume,
                        b_gate_attempt,
                        owner_release,
                        results,
                    ),
                ),
                context.Process(
                    target=_run_lock_process,
                    args=(
                        str(process_path),
                        "20260824T030011Z-0000000c",
                        "c",
                        stale_read,
                        resume,
                        c_gate_attempt,
                        owner_release,
                        results,
                    ),
                ),
                context.Process(
                    target=_run_lock_process,
                    args=(
                        str(process_path),
                        "20260824T030012Z-0000000d",
                        "d",
                        stale_read,
                        resume,
                        d_gate_attempt,
                        owner_release,
                        results,
                    ),
                ),
            ]
            try:
                processes[0].start()
                self.assertTrue(stale_read.wait(5))
                processes[1].start()
                processes[2].start()
                self.assertTrue(c_gate_attempt.wait(5))
                self.assertTrue(d_gate_attempt.wait(5))
                resume.set()
                outcomes = [results.get(timeout=10) for _ in range(3)]
                self.assertEqual(
                    sorted(outcomes),
                    [("b", "entered"), ("c", "busy"), ("d", "busy")],
                )
            finally:
                resume.set()
                owner_release.set()
                for process in processes:
                    process.join(timeout=5)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)
                results.close()
                results.join_thread()
            self.assertTrue(all(process.exitcode == 0 for process in processes))
            self.assertFalse(process_path.exists())
            self.assertTrue(
                (root / ".unified-game-radar-run-lock.gate").exists()
            )

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
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
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
