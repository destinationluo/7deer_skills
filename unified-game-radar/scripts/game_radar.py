#!/usr/bin/env python3
"""Import-safe command line entry point for the unified game radar."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import socket
import sys
import traceback
from typing import TextIO
from uuid import uuid4


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
STEAM_PACKAGE_ROOT = REPOSITORY_ROOT / "steam-game-radar"
for package_root in (STEAM_PACKAGE_ROOT, PACKAGE_ROOT):
    package_text = str(package_root)
    if package_text not in sys.path:
        sys.path.insert(0, package_text)

from unified_game_radar.config import RadarConfig
from unified_game_radar.errors import InputValidationError, RadarError
from unified_game_radar.run_lock import RunLock
from unified_game_radar.schemas import CommandManifest


_PLATFORM_CHOICES = ("all", "itch", "steam", "roblox")
_MAX_INPUT_BYTES = 10 * 1024 * 1024


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InputValidationError(message)


@dataclass(frozen=True)
class CommandRequest:
    command: str
    project_root: Path
    config: RadarConfig
    run_id: str
    started_at: datetime | None
    platforms: tuple[str, ...]
    input_path: Path | None
    publish_daily: bool
    clock: Callable[[], datetime]
    id_factory: Callable[[], str]


CommandRunner = Callable[[CommandRequest], CommandManifest]
LockFactory = Callable[..., object]


def _parser() -> _Parser:
    parser = _Parser(prog="game_radar.py")
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan")
    scan.add_argument("--config", required=True)
    scan.add_argument("--platform", choices=_PLATFORM_CHOICES, default="all")
    scan.add_argument("--publish-daily", action="store_true")

    for name in ("ingest", "enrich"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--input", required=True)

    report = commands.add_parser("report")
    report.add_argument("--config", required=True)
    report.add_argument("--run-id", required=True)
    return parser


def _new_run(clock: Callable[[], datetime], id_factory: Callable[[], str]) -> tuple[datetime, str]:
    from unified_game_radar.orchestration import new_run_id

    try:
        return new_run_id(clock, id_factory)
    except (TypeError, ValueError) as error:
        raise InputValidationError(str(error)) from error


def _resolve_path(value: object, project_root: Path, name: str) -> Path:
    if not isinstance(value, str) or not value or value != value.strip():
        raise InputValidationError(f"{name} must be nonempty path text")
    path = Path(value)
    return path if path.is_absolute() else project_root / path


def _request(
    namespace: argparse.Namespace,
    *,
    project_root: Path,
    clock: Callable[[], datetime],
    id_factory: Callable[[], str],
) -> CommandRequest:
    config_path = _resolve_path(namespace.config, project_root, "config")
    config = RadarConfig.from_file(config_path, project_root=project_root)
    command = namespace.command
    if command == "scan":
        started_at, run_id = _new_run(clock, id_factory)
        if namespace.platform == "all":
            platforms = tuple(config.enabled_platforms)
        else:
            platforms = (namespace.platform,)
            if namespace.platform not in config.enabled_platforms:
                raise InputValidationError(
                    f"platform is disabled by configuration: {namespace.platform}"
                )
        return CommandRequest(
            command=command,
            project_root=project_root,
            config=config,
            run_id=run_id,
            started_at=started_at,
            platforms=platforms,
            input_path=None,
            publish_daily=namespace.publish_daily,
            clock=clock,
            id_factory=id_factory,
        )
    return CommandRequest(
        command=command,
        project_root=project_root,
        config=config,
        run_id=namespace.run_id,
        started_at=None,
        platforms=(),
        input_path=(
            _resolve_path(namespace.input, project_root, "input")
            if command in {"ingest", "enrich"}
            else None
        ),
        publish_daily=False,
        clock=clock,
        id_factory=id_factory,
    )


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        return True
    return True


def _lock(
    factory: LockFactory,
    request: CommandRequest,
) -> object:
    return factory(
        path=request.project_root / ".unified-game-radar-run.lock",
        run_id=request.run_id,
        now=request.clock,
        hostname=socket.gethostname,
        pid_alive=_pid_alive,
    )


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InputValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path: Path) -> object:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise InputValidationError(f"unable to read input: {path}") from error
    if len(raw) > _MAX_INPUT_BYTES:
        raise InputValidationError("input JSON exceeds the 10 MiB command limit")
    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                InputValidationError(f"invalid JSON number: {value}")
            ),
        )
    except InputValidationError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise InputValidationError("input must contain valid strict JSON") from error


def _evidence_batch(value: object) -> tuple[object, ...]:
    if isinstance(value, dict):
        rows = (value,)
    elif isinstance(value, list):
        rows = tuple(value)
    else:
        raise InputValidationError(
            "enrichment input must be one evidence object or an array of objects"
        )
    from unified_game_radar.schemas import OpportunityEvidence

    return tuple(OpportunityEvidence.from_dict(row) for row in rows)


def _run_command(request: CommandRequest) -> CommandManifest:
    """Execute one parsed request. Imports stay local for direct bootstrapping."""

    from steam_game_radar.http_client import JsonHttpClient
    from unified_game_radar.collectors.itch import parse_itch_envelope
    from unified_game_radar.collectors.roblox import parse_roblox_envelope
    from unified_game_radar.collectors.steam import SteamCollector
    from unified_game_radar.orchestration import (
        enrich_run,
        ingest_run,
        report_run,
        scan_run,
    )
    from unified_game_radar.storage import RadarStore

    database = Path(request.config.data_dir) / "radar.sqlite3"
    with RadarStore(database) as store:
        if request.command == "scan":
            collectors = {}
            if "steam" in request.platforms:
                steam_config = request.config.to_steam_config()
                collectors["steam"] = SteamCollector(
                    request.config,
                    JsonHttpClient(steam_config),
                )
            assert request.started_at is not None
            scan_run(
                request.config,
                store,
                collectors,
                lambda: request.started_at,  # one instant owns run and lock
                request.id_factory,
                request.platforms,
                started_at=request.started_at,
                run_id=request.run_id,
                publish_daily=request.publish_daily,
            )
            return report_run(
                request.config,
                store,
                request.run_id,
                request.clock,
            )
        if request.command == "report":
            return report_run(
                request.config,
                store,
                request.run_id,
                request.clock,
            )

        assert request.input_path is not None
        payload = _read_json(request.input_path)
        if request.command == "ingest":
            if not isinstance(payload, dict):
                raise InputValidationError(
                    "browser ingest input must contain one JSON object"
                )
            ingest_run(
                request.config,
                store,
                request.run_id,
                payload,
                {
                    "itch": parse_itch_envelope,
                    "roblox": parse_roblox_envelope,
                },
                request.clock,
                request.id_factory,
            )
            return report_run(
                request.config,
                store,
                request.run_id,
                request.clock,
            )
        if request.command == "enrich":
            return enrich_run(
                request.config,
                store,
                request.run_id,
                _evidence_batch(payload),
                request.clock,
            )
    raise InputValidationError(f"unsupported command: {request.command}")


def main(
    argv: Sequence[str] | None = None,
    *,
    project_root: Path | None = None,
    clock: Callable[[], datetime] | None = None,
    id_factory: Callable[[], str] | None = None,
    command_runner: CommandRunner | None = None,
    lock_factory: LockFactory | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Parse one command, run it under the project lock, and print a manifest."""

    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    try:
        namespace = _parser().parse_args(argv)
        root_value = (
            REPOSITORY_ROOT
            if project_root is None
            else Path(project_root)
        )
        try:
            resolved_root = root_value.resolve(strict=True)
        except OSError as error:
            raise InputValidationError("project_root must be an existing directory") from error
        if not resolved_root.is_dir():
            raise InputValidationError("project_root must be an existing directory")
        active_clock = (
            (lambda: datetime.now(timezone.utc)) if clock is None else clock
        )
        active_id_factory = (
            (lambda: str(uuid4())) if id_factory is None else id_factory
        )
        request = _request(
            namespace,
            project_root=resolved_root,
            clock=active_clock,
            id_factory=active_id_factory,
        )
        active_runner = _run_command if command_runner is None else command_runner
        active_lock_factory = RunLock if lock_factory is None else lock_factory
        with _lock(active_lock_factory, request):  # type: ignore[attr-defined]
            result = active_runner(request)
        if not isinstance(result, CommandManifest):
            raise TypeError("command runner must return CommandManifest")
        output.write(
            json.dumps(
                result.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        return 0
    except RadarError as error:
        errors.write(f"error: {error}\n")
        return error.exit_code
    except Exception:
        traceback.print_exc(file=errors)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
