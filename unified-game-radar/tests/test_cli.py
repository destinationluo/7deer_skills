from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from scripts import game_radar
from unified_game_radar.errors import (
    ConfigurationError,
    IdempotencyConflictError,
    InputValidationError,
    PersistenceError,
    ProviderUnavailableError,
    RunBusyError,
)
from unified_game_radar.schemas import CommandManifest


RUN_ID = "20260831T020000Z-a1b2c3d4"
NOW = datetime(2026, 8, 31, 2, tzinfo=timezone.utc)


def manifest(run_id: str = RUN_ID, phase: str = "preliminary") -> CommandManifest:
    return CommandManifest(
        schema_version=1,
        run_id=run_id,
        phase=phase,
        report_json=f"/tmp/{run_id}.{phase}.json",
        report_markdown=f"/tmp/{run_id}.{phase}.md",
        source_health=(),
        warnings=(),
        outstanding_tasks=(),
    )


class RecordingLock:
    def __init__(self, events: list[tuple[str, object]], **kwargs: object) -> None:
        self.events = events
        self.kwargs = kwargs

    def __enter__(self) -> "RecordingLock":
        self.events.append(("enter", self.kwargs["run_id"]))
        return self

    def __exit__(self, *args: object) -> None:
        self.events.append(("exit", self.kwargs["run_id"]))


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = self.root / "radar.json"
        self.config.write_text(
            json.dumps(
                {
                    "data_dir": "var/data",
                    "report_dir": "var/reports",
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_main(
        self,
        arguments: list[str],
        *,
        runner=None,
        lock_factory=None,
    ) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = game_radar.main(
                arguments,
                project_root=self.root,
                clock=lambda: NOW,
                id_factory=lambda: "a1b2c3d4",
                command_runner=runner,
                lock_factory=lock_factory,
            )
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_scan_all_prints_exactly_one_compact_manifest_line(self) -> None:
        requests = []
        events: list[tuple[str, object]] = []

        def runner(request):
            requests.append(request)
            events.append(("runner", request.run_id))
            return manifest(request.run_id)

        def lock_factory(**kwargs):
            return RecordingLock(events, **kwargs)

        code, stdout, stderr = self.run_main(
            [
                "scan",
                "--config",
                str(self.config),
                "--platform",
                "all",
                "--publish-daily",
            ],
            runner=runner,
            lock_factory=lock_factory,
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout.count("\n"), 1)
        parsed = json.loads(stdout)
        self.assertEqual(parsed, manifest(requests[0].run_id).to_dict())
        self.assertEqual(requests[0].command, "scan")
        self.assertEqual(requests[0].platforms, ("itch", "steam", "roblox"))
        self.assertTrue(requests[0].publish_daily)
        self.assertEqual(requests[0].config.data_dir, self.root / "var/data")
        self.assertEqual(requests[0].config.report_dir, self.root / "var/reports")
        self.assertEqual(
            events,
            [
                ("enter", requests[0].run_id),
                ("runner", requests[0].run_id),
                ("exit", requests[0].run_id),
            ],
        )

    def test_platform_filter_maps_each_choice_to_one_platform(self) -> None:
        for platform in ("itch", "steam", "roblox"):
            with self.subTest(platform=platform):
                requests = []
                code, _, _ = self.run_main(
                    [
                        "scan",
                        "--config",
                        str(self.config),
                        "--platform",
                        platform,
                    ],
                    runner=lambda request: (
                        requests.append(request) or manifest(request.run_id)
                    ),
                    lock_factory=lambda **kwargs: RecordingLock([], **kwargs),
                )
                self.assertEqual(code, 0)
                self.assertEqual(requests[0].platforms, (platform,))

    def test_all_uses_enabled_platforms_and_disabled_explicit_choice_is_input_error(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "data_dir": "var/data",
                    "report_dir": "var/reports",
                    "enabled_platforms": ["itch", "steam"],
                }
            ),
            encoding="utf-8",
        )
        requests = []
        code, _, _ = self.run_main(
            ["scan", "--config", str(self.config), "--platform", "all"],
            runner=lambda request: requests.append(request) or manifest(request.run_id),
            lock_factory=lambda **kwargs: RecordingLock([], **kwargs),
        )
        self.assertEqual(code, 0)
        self.assertEqual(requests[0].platforms, ("itch", "steam"))

        code, stdout, stderr = self.run_main(
            ["scan", "--config", str(self.config), "--platform", "roblox"],
            runner=mock.Mock(),
            lock_factory=mock.Mock(),
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("disabled", stderr)

    def test_ingest_enrich_and_report_forward_exact_run_and_input(self) -> None:
        input_path = self.root / "input.json"
        input_path.write_text("{}", encoding="utf-8")
        cases = (
            ("ingest", True),
            ("enrich", True),
            ("report", False),
        )
        for command, has_input in cases:
            with self.subTest(command=command):
                requests = []
                arguments = [
                    command,
                    "--config",
                    str(self.config),
                    "--run-id",
                    RUN_ID,
                ]
                if has_input:
                    arguments.extend(("--input", str(input_path)))
                code, _, _ = self.run_main(
                    arguments,
                    runner=lambda request: (
                        requests.append(request) or manifest(RUN_ID)
                    ),
                    lock_factory=lambda **kwargs: RecordingLock([], **kwargs),
                )
                self.assertEqual(code, 0)
                self.assertEqual(requests[0].run_id, RUN_ID)
                self.assertEqual(requests[0].command, command)
                self.assertEqual(
                    requests[0].input_path,
                    input_path if has_input else None,
                )

    def test_json_input_read_is_bounded_before_parsing(self) -> None:
        class RecordingReader:
            def __init__(self) -> None:
                self.read_sizes: list[int] = []

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                self.read_sizes.append(size)
                return b"{}"

        reader = RecordingReader()
        with mock.patch.object(Path, "open", return_value=reader):
            payload = game_radar._read_json(self.root / "bounded.json")

        self.assertEqual(payload, {})
        self.assertEqual(reader.read_sizes, [game_radar._MAX_INPUT_BYTES + 1])

    def test_lock_is_released_when_runner_raises(self) -> None:
        events: list[tuple[str, object]] = []

        def fail(_request):
            events.append(("runner", RUN_ID))
            raise PersistenceError("disk full")

        code, stdout, stderr = self.run_main(
            [
                "report",
                "--config",
                str(self.config),
                "--run-id",
                RUN_ID,
            ],
            runner=fail,
            lock_factory=lambda **kwargs: RecordingLock(events, **kwargs),
        )

        self.assertEqual(code, 5)
        self.assertEqual(stdout, "")
        self.assertIn("disk full", stderr)
        self.assertEqual(
            events,
            [("enter", RUN_ID), ("runner", RUN_ID), ("exit", RUN_ID)],
        )

    def test_expected_failures_map_to_documented_exit_codes(self) -> None:
        cases = (
            (InputValidationError("bad input"), 2),
            (ProviderUnavailableError("offline"), 3),
            (ConfigurationError("bad config"), 4),
            (PersistenceError("disk"), 5),
            (RunBusyError("busy"), 6),
            (IdempotencyConflictError("changed"), 7),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                def fail(_request, error=error):
                    raise error

                code, stdout, stderr = self.run_main(
                    [
                        "report",
                        "--config",
                        str(self.config),
                        "--run-id",
                        RUN_ID,
                    ],
                    runner=fail,
                    lock_factory=lambda **kwargs: RecordingLock([], **kwargs),
                )
                self.assertEqual(code, expected)
                self.assertEqual(stdout, "")
                self.assertIn(str(error), stderr)

    def test_argument_errors_return_two_without_calling_runner_or_lock(self) -> None:
        runner = mock.Mock()
        lock_factory = mock.Mock()
        code, stdout, stderr = self.run_main(
            ["scan", "--config", str(self.config), "--platform", "invalid"],
            runner=runner,
            lock_factory=lock_factory,
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertNotEqual(stderr, "")
        runner.assert_not_called()
        lock_factory.assert_not_called()

    def test_unexpected_failure_returns_one_and_prints_traceback(self) -> None:
        def fail(_request):
            raise RuntimeError("boom")

        code, stdout, stderr = self.run_main(
            [
                "report",
                "--config",
                str(self.config),
                "--run-id",
                RUN_ID,
            ],
            runner=fail,
            lock_factory=lambda **kwargs: RecordingLock([], **kwargs),
        )
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Traceback", stderr)
        self.assertIn("RuntimeError: boom", stderr)

    def test_lock_collision_returns_six_before_runner(self) -> None:
        runner = mock.Mock()

        def busy(**_kwargs):
            raise RunBusyError("owned")

        code, stdout, stderr = self.run_main(
            [
                "report",
                "--config",
                str(self.config),
                "--run-id",
                RUN_ID,
            ],
            runner=runner,
            lock_factory=busy,
        )
        self.assertEqual(code, 6)
        self.assertEqual(stdout, "")
        self.assertIn("owned", stderr)
        runner.assert_not_called()

    def test_direct_execution_is_import_safe_and_returns_argument_error(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(PROJECT_DIR / "scripts/game_radar.py")],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertNotEqual(completed.stderr, "")

    def test_default_runner_scan_itch_writes_report_and_database(self) -> None:
        code, stdout, stderr = self.run_main(
            [
                "scan",
                "--config",
                str(self.config),
                "--platform",
                "itch",
            ],
        )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        restored = CommandManifest.from_dict(payload)
        self.assertEqual(restored.phase, "preliminary")
        self.assertEqual(
            tuple(task.collector for task in restored.outstanding_tasks),
            ("itch",),
        )
        self.assertTrue(Path(restored.report_json).is_file())
        self.assertTrue(Path(restored.report_markdown).is_file())
        self.assertTrue((self.root / "var/data/radar.sqlite3").is_file())
        self.assertFalse((self.root / "var/data/run.lock").exists())


if __name__ == "__main__":
    unittest.main()
