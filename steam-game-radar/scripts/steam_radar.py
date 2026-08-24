#!/usr/bin/env python3
"""Command-line orchestration for the Steam game radar."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import secrets
import socket
import sys
import traceback
from typing import Any, cast

SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from steam_game_radar.artifacts import persist_raw as default_persist_raw
from steam_game_radar.artifacts import prune_raw as default_prune_raw
from steam_game_radar.config import RadarConfig
from steam_game_radar.enrichment import (
    EnrichmentBundle,
    load_enrichment as default_load_enrichment,
)
from steam_game_radar.errors import (
    ConfigurationError,
    InputValidationError,
    PersistenceError,
    ProviderUnavailableError,
    RunBusyError,
)
from steam_game_radar.http_client import JsonHttpClient
from steam_game_radar.merge import (
    MergeResult,
    merge_import_with_official as default_merge_import,
)
from steam_game_radar.official_provider import (
    CollectionResult,
    collect_official as default_collect_official,
)
from steam_game_radar.report import (
    build_report as default_build_report,
    persist_report as default_persist_report,
)
from steam_game_radar.run_lock import RunLock
from steam_game_radar.schemas import GameRecord, RejectedRow, WarningRecord
from steam_game_radar.score import (
    ScoredCandidate,
    apply_final_score as default_apply_final_score,
    candidate_sort_key,
    score_released as default_score_released,
    score_unreleased as default_score_unreleased,
)
from steam_game_radar.snapshot import (
    load_snapshots as default_load_snapshots,
    make_run_id,
    persist_snapshot as default_persist_snapshot,
    select_comparison as default_select_comparison,
)
from steam_game_radar.steamdb_import import (
    ImportResult,
    import_steamdb as default_import_steamdb,
)
from steam_game_radar.trend import (
    AnalyzedCandidate,
    analyze_trends as default_analyze_trends,
)


_STEAMDB_VIEWS = (
    "trending_games",
    "wishlist_activity",
    "trending_followers",
    "recent_releases",
)
_OFFICIAL_CAPABILITIES = (
    "most_played",
    "featured_categories",
    "appdetails",
    "current_players",
)
_DOMAIN_EXIT_CODES = (
    (InputValidationError, 2),
    (ProviderUnavailableError, 3),
    (ConfigurationError, 4),
    (PersistenceError, 5),
    (RunBusyError, 6),
)
_DOMAIN_ERROR_TYPES = tuple(
    error_type for error_type, _code in _DOMAIN_EXIT_CODES
)
_PROVIDER_WARNING = WarningRecord(
    code="steam_official_provider_unavailable",
    message="Official Steam collection failed; a stored official snapshot was used.",
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class Services:
    """Injected runtime boundaries for deterministic, offline orchestration tests."""

    client_factory: Callable[[RadarConfig], object] = JsonHttpClient
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    entropy: Callable[[int], bytes] = secrets.token_bytes
    hostname: Callable[[], str] = socket.gethostname
    pid_alive: Callable[[int], bool] = _pid_alive
    project_root: Callable[[], Path] = Path.cwd
    load_config: Callable[..., RadarConfig] = RadarConfig.from_file
    lock_factory: Callable[..., RunLock] = RunLock
    collect_official: Callable[..., CollectionResult] = default_collect_official
    persist_raw: Callable[..., Path] = default_persist_raw
    prune_raw: Callable[..., Sequence[Path]] = default_prune_raw
    load_snapshots: Callable[..., Sequence[Mapping[str, object]]] = (
        default_load_snapshots
    )
    persist_snapshot: Callable[..., Path] = default_persist_snapshot
    select_comparison: Callable[..., Mapping[str, object] | None] = (
        default_select_comparison
    )
    import_steamdb: Callable[..., ImportResult] = default_import_steamdb
    merge_import: Callable[..., MergeResult] = default_merge_import
    load_enrichment: Callable[..., EnrichmentBundle] = default_load_enrichment
    analyze_trends: Callable[..., Sequence[AnalyzedCandidate]] = (
        default_analyze_trends
    )
    score_released: Callable[[AnalyzedCandidate], ScoredCandidate] = (
        default_score_released
    )
    score_unreleased: Callable[[AnalyzedCandidate], ScoredCandidate] = (
        default_score_unreleased
    )
    apply_final_score: Callable[..., ScoredCandidate] = default_apply_final_score
    build_report: Callable[..., dict[str, object]] = default_build_report
    persist_report: Callable[..., tuple[Path, Path]] = default_persist_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="steam_radar.py",
        description="Scan official Steam trends or import a local SteamDB export.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", allow_abbrev=False)
    scan.add_argument("--config", type=Path, required=True)

    imported = commands.add_parser("import-steamdb", allow_abbrev=False)
    imported.add_argument("--config", type=Path, required=True)
    imported.add_argument("--view", choices=_STEAMDB_VIEWS, required=True)
    imported.add_argument("--input", type=Path, required=True)

    enrich = commands.add_parser("enrich", allow_abbrev=False)
    enrich.add_argument("--config", type=Path, required=True)
    enrich.add_argument("--run-id", required=True)
    enrich.add_argument("--input", type=Path, required=True)
    return parser


def run_scan(args: argparse.Namespace, services: Services) -> int:
    project_root, config = _runtime_config(args, services)
    del project_root
    started_at = _utc_second(services.clock())
    run_id = make_run_id(started_at, services.entropy(4))
    observed_at = _timestamp(started_at)
    lock = services.lock_factory(
        config.data_dir / ".run.lock",
        run_id,
        services.clock,
        services.hostname,
        services.pid_alive,
    )
    with lock:
        history = tuple(services.load_snapshots(config))
        _require_new_run_id(history, run_id)
        provider_error: ProviderUnavailableError | None = None
        collection: CollectionResult | None = None
        try:
            client = services.client_factory(config)
            collection = services.collect_official(client, config, observed_at)
            for provider_id, payload in sorted(collection.raw_to_dict().items()):
                services.persist_raw(
                    config,
                    run_id,
                    provider_id,
                    payload,
                    started_at,
                )
            _require_usable_official_collection(collection)
        except ProviderUnavailableError as error:
            provider_error = error

        if provider_error is None:
            if collection is None:  # pragma: no cover - guarded assignment
                raise AssertionError("official collection result is missing")
            records = tuple(collection.released) + tuple(collection.unreleased)
            mode = "official_scan"
            data_status = "fresh"
            warnings = tuple(collection.warnings)
            metadata = _snapshot_metadata(
                provider="steam_official",
                mode=mode,
                data_status=data_status,
                warnings=warnings,
                rejected_rows=(),
                capabilities=collection.capabilities,
            )
        else:
            fallback = _newest_official_fallback(
                history,
                started_at,
                config,
                services,
            )
            if fallback is None:
                raise provider_error
            fallback_snapshot, merged = fallback
            records = tuple(merged.records)
            mode = "official_scan"
            data_status = merged.data_status
            attempt_warnings = () if collection is None else tuple(collection.warnings)
            warnings = (
                attempt_warnings
                + (_PROVIDER_WARNING,)
                + tuple(merged.warnings)
            )
            metadata = _snapshot_metadata(
                provider="steam_official_fallback",
                mode=mode,
                data_status=data_status,
                warnings=warnings,
                rejected_rows=(),
                capabilities=(
                    None if collection is None else collection.capabilities
                ),
                fallback_run_id=cast(str, fallback_snapshot["run_id"]),
            )

        services.persist_snapshot(config, run_id, records, metadata)
        services.prune_raw(config, started_at)
        _persist_preliminary(
            config=config,
            run_id=run_id,
            generated_at=observed_at,
            mode=mode,
            data_status=data_status,
            records=records,
            history=history,
            warnings=warnings,
            rejected_rows=(),
            lock=lock,
            services=services,
            use_history=True,
        )
    return 0


def run_import(args: argparse.Namespace, services: Services) -> int:
    project_root, config = _runtime_config(args, services)
    input_path = _project_path(cast(Path, args.input), project_root)
    started_at = _utc_second(services.clock())
    run_id = make_run_id(started_at, services.entropy(4))
    observed_at = _timestamp(started_at)
    lock = services.lock_factory(
        config.data_dir / ".run.lock",
        run_id,
        services.clock,
        services.hostname,
        services.pid_alive,
    )
    with lock:
        history = tuple(services.load_snapshots(config))
        _require_new_run_id(history, run_id)
        imported = services.import_steamdb(input_path, args.view, observed_at)
        services.persist_raw(
            config,
            run_id,
            f"steamdb_{imported.view}",
            imported.raw_to_dict(),
            started_at,
        )
        fallback = _newest_official_fallback(
            history,
            started_at,
            config,
            services,
        )
        official_snapshot = None if fallback is None else fallback[0]
        merged = services.merge_import(
            imported.records,
            official_snapshot,
            started_at,
            config,
        )
        rejected_rows = tuple(imported.rejected_rows) + tuple(merged.rejected_rows)
        warnings = list(merged.warnings)
        if rejected_rows:
            warnings.append(
                WarningRecord(
                    code="steamdb_rows_rejected",
                    message=f"{len(rejected_rows)} SteamDB row(s) were rejected.",
                )
            )
        provider = (
            "steamdb_manual_import"
            if merged.mode == "manual_baseline"
            else "steam_official_plus_manual"
        )
        metadata = _snapshot_metadata(
            provider=provider,
            mode=merged.mode,
            data_status=merged.data_status,
            warnings=warnings,
            rejected_rows=rejected_rows,
            fallback_run_id=(
                None
                if official_snapshot is None
                else cast(str, official_snapshot["run_id"])
            ),
        )
        services.persist_snapshot(config, run_id, merged.records, metadata)
        services.prune_raw(config, started_at)
        _persist_preliminary(
            config=config,
            run_id=run_id,
            generated_at=observed_at,
            mode=merged.mode,
            data_status=merged.data_status,
            records=merged.records,
            history=history,
            warnings=warnings,
            rejected_rows=rejected_rows,
            lock=lock,
            services=services,
            use_history=merged.mode != "manual_baseline",
        )
    return 0


def run_enrich(args: argparse.Namespace, services: Services) -> int:
    project_root, config = _runtime_config(args, services)
    input_path = _project_path(cast(Path, args.input), project_root)
    generated_at = _utc_second(services.clock())
    run_id = cast(str, args.run_id)
    lock = services.lock_factory(
        config.data_dir / ".run.lock",
        run_id,
        services.clock,
        services.hostname,
        services.pid_alive,
    )
    with lock:
        enrichment = services.load_enrichment(input_path, run_id)
        snapshots = tuple(services.load_snapshots(config))
        current = _snapshot_for_run(snapshots, run_id)
        records = _snapshot_records(current)
        metadata = _pipeline_metadata(current)
        target_observed_at = _snapshot_observed_at(current)
        history = tuple(
            snapshot
            for snapshot in snapshots
            if _snapshot_observed_at(snapshot) < target_observed_at
        )
        analyzed = _analyze(
            records,
            history,
            target_observed_at,
            services,
            use_history=True,
        )
        preliminary = _score_analyzed(analyzed, services)
        final: list[ScoredCandidate] = []
        for candidate in preliminary:
            enriched = enrichment.games.get(candidate.record.appid)
            if enriched is None:
                final.append(candidate)
                continue
            final.append(
                services.apply_final_score(
                    candidate,
                    enriched,
                    _provenance(candidate),
                )
            )
        released, unreleased = _candidate_pools(
            final,
            config.final_top_n,
        )
        newly_observed = sorted(
            candidate.record.appid
            for candidate in analyzed
            if candidate.newly_observed
        )
        report = services.build_report(
            run_id=run_id,
            phase="final",
            mode=metadata["mode"],
            generated_at=_timestamp(generated_at),
            data_status=metadata["data_status"],
            released=released,
            unreleased=unreleased,
            newly_observed=newly_observed,
            warnings=metadata["warnings"],
            rejected_rows=metadata["rejected_rows"],
        )
        services.persist_report(config, report, lock)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        services = Services()
        if args.command == "scan":
            return run_scan(args, services)
        if args.command == "import-steamdb":
            return run_import(args, services)
        if args.command == "enrich":
            return run_enrich(args, services)
        raise InputValidationError(f"unsupported command: {args.command}")
    except _DOMAIN_ERROR_TYPES as error:
        for error_type, code in _DOMAIN_EXIT_CODES:
            if isinstance(error, error_type):
                print(f"{error_type.__name__}: {error}", file=sys.stderr)
                return code
        raise AssertionError("unreachable domain-error mapping")
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1


def _runtime_config(
    args: argparse.Namespace,
    services: Services,
) -> tuple[Path, RadarConfig]:
    project_root = Path(services.project_root()).resolve()
    config_path = _project_path(cast(Path, args.config), project_root)
    config = services.load_config(config_path, project_root=project_root)
    if not isinstance(config, RadarConfig):
        raise ConfigurationError("configuration loader did not return RadarConfig")
    return project_root, config


def _project_path(path: Path, project_root: Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else project_root / candidate


def _utc_now(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise InputValidationError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _utc_second(value: datetime) -> datetime:
    return _utc_now(value).replace(microsecond=0)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _require_usable_official_collection(collection: CollectionResult) -> None:
    complete = all(
        collection.capabilities.get(name) is True
        for name in _OFFICIAL_CAPABILITIES
    )
    if not complete or not (collection.released or collection.unreleased):
        raise ProviderUnavailableError(
            "official Steam discovery did not produce a usable collection"
        )


def _require_new_run_id(
    snapshots: Sequence[Mapping[str, object]],
    run_id: str,
) -> None:
    if any(snapshot.get("run_id") == run_id for snapshot in snapshots):
        raise PersistenceError("run_id already has an immutable snapshot")


def _newest_official_fallback(
    snapshots: Sequence[Mapping[str, object]],
    now: datetime,
    config: RadarConfig,
    services: Services,
) -> tuple[Mapping[str, object], MergeResult] | None:
    for snapshot in reversed(tuple(snapshots)):
        if not _is_complete_official_snapshot(snapshot):
            continue
        try:
            merged = services.merge_import((), snapshot, now, config)
        except InputValidationError:
            continue
        if merged.mode == "official_plus_manual":
            return snapshot, merged
    return None


def _is_complete_official_snapshot(
    snapshot: Mapping[str, object],
) -> bool:
    metadata = snapshot.get("metadata")
    records = snapshot.get("records")
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("provider") != "steam_official"
        or metadata.get("mode") != "official_scan"
        or metadata.get("data_status") != "fresh"
        or isinstance(records, (str, bytes))
        or not isinstance(records, Sequence)
        or not records
    ):
        return False
    capabilities = metadata.get("capabilities")
    return (
        isinstance(capabilities, Mapping)
        and set(capabilities) == set(_OFFICIAL_CAPABILITIES)
        and all(capabilities[name] is True for name in _OFFICIAL_CAPABILITIES)
    )


def _snapshot_metadata(
    *,
    provider: str,
    mode: str,
    data_status: str,
    warnings: Sequence[WarningRecord],
    rejected_rows: Sequence[RejectedRow],
    capabilities: Mapping[str, bool] | None = None,
    fallback_run_id: str | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "provider": provider,
        "mode": mode,
        "data_status": data_status,
        "warnings": [warning.to_dict() for warning in warnings],
        "rejected_rows": [row.to_dict() for row in rejected_rows],
    }
    if capabilities is not None:
        metadata["capabilities"] = dict(capabilities)
    if fallback_run_id is not None:
        metadata["fallback_run_id"] = fallback_run_id
    return metadata


def _persist_preliminary(
    *,
    config: RadarConfig,
    run_id: str,
    generated_at: str,
    mode: str,
    data_status: str,
    records: Sequence[GameRecord],
    history: Sequence[Mapping[str, object]],
    warnings: Sequence[WarningRecord],
    rejected_rows: Sequence[RejectedRow],
    lock: RunLock,
    services: Services,
    use_history: bool,
) -> None:
    current_time = datetime.fromisoformat(generated_at[:-1] + "+00:00")
    analyzed = _analyze(records, history, current_time, services, use_history)
    scored = _score_analyzed(analyzed, services)
    released, unreleased = _candidate_pools(scored, config.preliminary_top_n)
    report = services.build_report(
        run_id=run_id,
        phase="preliminary",
        mode=mode,
        generated_at=generated_at,
        data_status=data_status,
        released=released,
        unreleased=unreleased,
        newly_observed=sorted(
            candidate.record.appid
            for candidate in analyzed
            if candidate.newly_observed
        ),
        warnings=warnings,
        rejected_rows=rejected_rows,
    )
    services.persist_report(config, report, lock)


def _analyze(
    records: Sequence[GameRecord],
    history: Sequence[Mapping[str, object]],
    now: datetime,
    services: Services,
    use_history: bool,
) -> Sequence[AnalyzedCandidate]:
    one_day = None
    seven_day = None
    if use_history:
        one_day = services.select_comparison(history, now, 24, 18, 36)
        seven_day = services.select_comparison(history, now, 168, 144, 192)
    return services.analyze_trends(records, one_day, seven_day)


def _score_analyzed(
    analyzed: Sequence[AnalyzedCandidate],
    services: Services,
) -> tuple[ScoredCandidate, ...]:
    scored: list[ScoredCandidate] = []
    for candidate in analyzed:
        if candidate.record.release_status == "released":
            scored.append(services.score_released(candidate))
        elif candidate.record.release_status == "unreleased":
            scored.append(services.score_unreleased(candidate))
        else:
            raise InputValidationError("snapshot contains an unsupported release status")
    return tuple(scored)


def _candidate_pools(
    candidates: Sequence[ScoredCandidate],
    limit: int,
) -> tuple[tuple[ScoredCandidate, ...], tuple[ScoredCandidate, ...]]:
    released = tuple(
        sorted(
            (
                candidate
                for candidate in candidates
                if candidate.record.release_status == "released"
            ),
            key=candidate_sort_key,
        )[:limit]
    )
    unreleased = tuple(
        sorted(
            (
                candidate
                for candidate in candidates
                if candidate.record.release_status == "unreleased"
            ),
            key=candidate_sort_key,
        )[:limit]
    )
    return released, unreleased


def _snapshot_for_run(
    snapshots: Sequence[Mapping[str, object]],
    run_id: str,
) -> Mapping[str, object]:
    matches = [snapshot for snapshot in snapshots if snapshot.get("run_id") == run_id]
    if len(matches) != 1:
        raise InputValidationError("enrichment run snapshot was not found")
    return matches[0]


def _snapshot_records(snapshot: Mapping[str, object]) -> tuple[GameRecord, ...]:
    values = snapshot.get("records")
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise InputValidationError("snapshot records are invalid")
    records: list[GameRecord] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise InputValidationError("snapshot record is invalid")
        records.append(GameRecord.from_dict(value))
    return tuple(records)


def _snapshot_observed_at(snapshot: Mapping[str, object]) -> datetime:
    value = snapshot.get("observed_at")
    if not isinstance(value, str) or not value.endswith("Z"):
        raise InputValidationError("snapshot observed_at is invalid")
    try:
        observed_at = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise InputValidationError("snapshot observed_at is invalid") from error
    return _utc_now(observed_at)


def _pipeline_metadata(snapshot: Mapping[str, object]) -> dict[str, Any]:
    value = snapshot.get("metadata")
    if not isinstance(value, Mapping):
        raise InputValidationError("snapshot pipeline metadata is missing")
    mode = value.get("mode")
    data_status = value.get("data_status")
    if not isinstance(mode, str) or mode not in {
        "official_scan",
        "official_plus_manual",
        "manual_baseline",
    }:
        raise InputValidationError("snapshot mode is invalid")
    if not isinstance(data_status, str) or data_status not in {
        "fresh",
        "stale",
        "manual_only",
    }:
        raise InputValidationError("snapshot data_status is invalid")
    warning_values = value.get("warnings")
    rejected_values = value.get("rejected_rows")
    if not isinstance(warning_values, Sequence) or isinstance(
        warning_values, (str, bytes)
    ):
        raise InputValidationError("snapshot warnings are invalid")
    if not isinstance(rejected_values, Sequence) or isinstance(
        rejected_values, (str, bytes)
    ):
        raise InputValidationError("snapshot rejected_rows are invalid")
    warnings = tuple(
        WarningRecord.from_dict(item)
        for item in warning_values
        if isinstance(item, Mapping)
    )
    rejected_rows = tuple(
        RejectedRow.from_dict(item)
        for item in rejected_values
        if isinstance(item, Mapping)
    )
    if len(warnings) != len(warning_values) or len(rejected_rows) != len(
        rejected_values
    ):
        raise InputValidationError("snapshot pipeline records are invalid")
    return {
        "mode": mode,
        "data_status": data_status,
        "warnings": warnings,
        "rejected_rows": rejected_rows,
    }


def _provenance(candidate: ScoredCandidate) -> set[str]:
    tokens = {
        observation.source_kind
        for observation in candidate.record.metrics.values()
        if observation.source_kind in {"steam_official", "steamdb_manual_import"}
    }
    if candidate.deltas:
        tokens.add("historical_comparison")
    return tokens


if __name__ == "__main__":
    raise SystemExit(main())
