"""Canonical Steam radar reports and monotonic ``latest`` publication.

The JSON report is the sole report model. Markdown is a deterministic view of
that validated object and never sorts candidates or derives scores. All report
I/O is anchored to an opened directory descriptor; absolute paths therefore
must use canonical, non-symlink directory components.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import errno
import json
import math
import os
from pathlib import Path
import secrets
import stat
from typing import Literal, Mapping, Sequence, cast

from .artifacts import _serialize_json, _validate_run_id
from .config import RadarConfig
from .enrichment import Evidence
from .errors import InputValidationError, PersistenceError
from .run_lock import RunLock
from .schemas import (
    GameRecord,
    MAX_JSON_SAFE_INTEGER,
    MAX_STEAM_APPID,
    MIN_JSON_SAFE_INTEGER,
    MetricObservation,
    RejectedRow,
    WarningRecord,
    _utc_timestamp,
)
from .score import ScoredCandidate, candidate_sort_key


ReportPhase = Literal["preliminary", "final"]
ReportMode = Literal[
    "official_scan",
    "official_plus_manual",
    "manual_baseline",
]
DataStatus = Literal["fresh", "stale", "manual_only"]

_PHASES = {"preliminary", "final"}
_MODES = {"official_scan", "official_plus_manual", "manual_baseline"}
_DATA_STATUSES = {"fresh", "stale", "manual_only"}
_REPORT_FIELDS = (
    "report_schema_version",
    "run_id",
    "phase",
    "mode",
    "generated_at",
    "data_status",
    "released",
    "unreleased",
    "newly_observed",
    "warnings",
    "rejected_rows",
)
_REPORT_FIELD_SET = set(_REPORT_FIELDS)
_CANDIDATE_FIELDS = {
    "appid",
    "name",
    "release_status",
    "store_url",
    "observed_metrics",
    "deltas",
    "metric_scores",
    "steam_heat_score",
    "seo_opportunity_score",
    "final_score",
    "action",
    "confidence",
    "warnings",
    "evidence",
    "recommended_content_types",
}
_MAX_REPORT_BYTES = 64 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class _StagedFile:
    name: str
    descriptor: int
    inode: tuple[int, int]
    size: int


def build_report(
    run_id: str,
    phase: ReportPhase,
    mode: ReportMode,
    generated_at: str,
    data_status: DataStatus,
    released: Sequence[ScoredCandidate],
    unreleased: Sequence[ScoredCandidate],
    newly_observed: Sequence[int],
    warnings: Sequence[WarningRecord],
    rejected_rows: Sequence[RejectedRow],
) -> dict[str, object]:
    """Build a fresh canonical report without mutating pipeline objects."""

    _validate_run_id(run_id)
    parsed_phase = _literal(phase, _PHASES, "phase")
    parsed_mode = _literal(mode, _MODES, "mode")
    parsed_status = _literal(data_status, _DATA_STATUSES, "data_status")
    generated = _utc_timestamp(generated_at)
    released_candidates = _candidate_sequence(released, "released")
    unreleased_candidates = _candidate_sequence(unreleased, "unreleased")
    appids = [candidate.record.appid for candidate in released_candidates]
    appids.extend(candidate.record.appid for candidate in unreleased_candidates)
    if len(appids) != len(set(appids)):
        raise InputValidationError("report candidates must use unique AppIDs")

    return {
        "report_schema_version": 1,
        "run_id": run_id,
        "phase": parsed_phase,
        "mode": parsed_mode,
        "generated_at": generated,
        "data_status": parsed_status,
        "released": [
            _candidate_to_dict(candidate)
            for candidate in sorted(released_candidates, key=candidate_sort_key)
        ],
        "unreleased": [
            _candidate_to_dict(candidate)
            for candidate in sorted(unreleased_candidates, key=candidate_sort_key)
        ],
        "newly_observed": _canonical_newly_observed(newly_observed),
        "warnings": _canonical_warnings(warnings),
        "rejected_rows": _canonical_rejections(rejected_rows),
    }


def render_markdown(report: Mapping[str, object]) -> str:
    """Render Markdown by walking canonical JSON arrays in their given order."""

    canonical = _canonical_report(report)
    lines = [
        "# Steam Game Radar",
        "",
        f"- Run ID: {canonical['run_id']}",
        f"- Phase: {canonical['phase']}",
        f"- Mode: {canonical['mode']}",
        f"- Generated at: {canonical['generated_at']}",
        f"- Data status: {canonical['data_status']}",
        "",
    ]
    _render_candidate_pool(lines, "Released", canonical["released"])
    _render_candidate_pool(lines, "Unreleased", canonical["unreleased"])

    lines.extend(["## Newly observed", ""])
    newly = cast(list[object], canonical["newly_observed"])
    if newly:
        lines.extend(f"- AppID {appid}" for appid in newly)
    else:
        lines.append("- None")
    lines.extend(["", "## Run warnings", ""])
    run_warnings = cast(list[Mapping[str, object]], canonical["warnings"])
    if run_warnings:
        for warning in run_warnings:
            lines.append(f"- `{warning['code']}`: {warning['message']}")
    else:
        lines.append("- None")
    lines.extend(["", "## Rejected rows", ""])
    rejections = cast(list[Mapping[str, object]], canonical["rejected_rows"])
    if rejections:
        for rejected in rejections:
            lines.append(
                f"- Row {rejected['row_number']} `{rejected['code']}`: "
                f"{rejected['message']}"
            )
    else:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def should_publish_latest(
    candidate: Mapping[str, object],
    existing: Mapping[str, object] | None,
) -> bool:
    """Apply the conservative run-time and phase monotonicity rules."""

    incoming = _canonical_report(candidate)
    if existing is None:
        return True
    current = _canonical_report(existing)
    incoming_run = cast(str, incoming["run_id"])
    current_run = cast(str, current["run_id"])
    incoming_time = _run_datetime(incoming_run)
    current_time = _run_datetime(current_run)
    if incoming_time > current_time:
        return True
    if incoming_time < current_time or incoming_run != current_run:
        return False
    return current["phase"] == "preliminary" and incoming["phase"] == "final"


def persist_report(
    config: RadarConfig,
    report: Mapping[str, object],
    lock: RunLock,
) -> tuple[Path, Path]:
    """Persist a complete immutable pair, then monotonically update latest."""

    if not isinstance(config, RadarConfig):
        raise InputValidationError("config must be a RadarConfig")
    canonical = _canonical_report(report)
    run_id = cast(str, canonical["run_id"])
    _require_owned_lock(lock, run_id)
    serialized_json = _serialize_json(canonical).encode("utf-8")
    serialized_markdown = render_markdown(canonical).encode("utf-8")
    _bounded_report(serialized_json)
    _bounded_report(serialized_markdown)
    phase = cast(str, canonical["phase"])
    json_name = f"{run_id}.{phase}.json"
    markdown_name = f"{run_id}.{phase}.md"
    report_root = Path(config.report_dir)
    directory_descriptor = _open_report_directory(report_root)
    try:
        _persist_immutable_pair(
            directory_descriptor,
            json_name,
            serialized_json,
            markdown_name,
            serialized_markdown,
        )
        existing = _load_latest_pair(directory_descriptor)
        if should_publish_latest(canonical, existing):
            _update_latest_pair(
                directory_descriptor,
                serialized_json,
                serialized_markdown,
                existing is not None,
            )
    except (InputValidationError, PersistenceError):
        raise
    except OSError as error:
        raise PersistenceError("unable to persist report pair safely") from error
    finally:
        _close_descriptor(directory_descriptor)
    return report_root / json_name, report_root / markdown_name


def _candidate_sequence(
    values: Sequence[ScoredCandidate],
    release_status: str,
) -> tuple[ScoredCandidate, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise InputValidationError(f"{release_status} candidates must be a sequence")
    result = tuple(values)
    for candidate in result:
        if not isinstance(candidate, ScoredCandidate):
            raise InputValidationError("candidate pools must contain ScoredCandidate values")
        if candidate.record.release_status != release_status:
            raise InputValidationError(
                f"{release_status} pool contains a {candidate.record.release_status} candidate"
            )
    return result


def _candidate_to_dict(candidate: ScoredCandidate) -> dict[str, object]:
    record = candidate.record
    return {
        "appid": record.appid,
        "name": record.name,
        "release_status": record.release_status,
        "store_url": record.store_url,
        "observed_metrics": {
            name: record.metrics[name].to_dict() for name in sorted(record.metrics)
        },
        "deltas": {
            name: _one_decimal(candidate.deltas[name], "delta")
            for name in sorted(candidate.deltas)
        },
        "metric_scores": {
            name: _one_decimal(candidate.metric_scores[name], "metric score")
            for name in sorted(candidate.metric_scores)
        },
        "steam_heat_score": _optional_one_decimal(candidate.steam_heat_score),
        "seo_opportunity_score": _optional_one_decimal(
            candidate.seo_opportunity_score
        ),
        "final_score": _optional_one_decimal(candidate.final_score),
        "action": candidate.action,
        "confidence": candidate.confidence,
        "warnings": _canonical_warnings(candidate.warnings),
        "evidence": [
            {"source": evidence.source, "url": evidence.url}
            for evidence in candidate.evidence
        ],
        "recommended_content_types": list(candidate.recommended_content_types),
    }


def _canonical_report(report: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(report, Mapping) or set(report) != _REPORT_FIELD_SET:
        raise InputValidationError("report has an invalid top-level schema")
    version = report["report_schema_version"]
    if type(version) is not int or version != 1:
        raise InputValidationError("report_schema_version must be exactly 1")
    run_id = report["run_id"]
    _validate_run_id(run_id)
    phase = _literal(report["phase"], _PHASES, "phase")
    mode = _literal(report["mode"], _MODES, "mode")
    generated = _utc_timestamp(report["generated_at"])
    status = _literal(report["data_status"], _DATA_STATUSES, "data_status")
    released, released_candidates = _canonical_candidate_array(
        report["released"], "released"
    )
    unreleased, unreleased_candidates = _canonical_candidate_array(
        report["unreleased"], "unreleased"
    )
    candidate_appids = [candidate.record.appid for candidate in released_candidates]
    candidate_appids.extend(
        candidate.record.appid for candidate in unreleased_candidates
    )
    if len(candidate_appids) != len(set(candidate_appids)):
        raise InputValidationError("report candidates must use unique AppIDs")
    newly = _canonical_newly_observed(report["newly_observed"])
    if report["newly_observed"] != newly:
        raise InputValidationError("newly_observed must be sorted and unique")
    warnings = _warnings_from_json(report["warnings"])
    if report["warnings"] != warnings:
        raise InputValidationError("report warnings are not canonical")
    rejected = _rejections_from_json(report["rejected_rows"])
    if report["rejected_rows"] != rejected:
        raise InputValidationError("report rejections are not canonical")
    return {
        "report_schema_version": 1,
        "run_id": run_id,
        "phase": phase,
        "mode": mode,
        "generated_at": generated,
        "data_status": status,
        "released": released,
        "unreleased": unreleased,
        "newly_observed": newly,
        "warnings": warnings,
        "rejected_rows": rejected,
    }


def _canonical_candidate_array(
    value: object,
    release_status: str,
) -> tuple[list[dict[str, object]], tuple[ScoredCandidate, ...]]:
    if not isinstance(value, list):
        raise InputValidationError(f"report {release_status} must be an array")
    canonical: list[dict[str, object]] = []
    candidates: list[ScoredCandidate] = []
    for item in value:
        parsed = _candidate_from_json(item, release_status)
        rendered = _candidate_to_dict(parsed)
        if item != rendered:
            raise InputValidationError("report candidate is not canonical")
        canonical.append(rendered)
        candidates.append(parsed)
    return canonical, tuple(candidates)


def _candidate_from_json(value: object, release_status: str) -> ScoredCandidate:
    if not isinstance(value, Mapping) or set(value) != _CANDIDATE_FIELDS:
        raise InputValidationError("report candidate has invalid fields")
    metrics_value = value["observed_metrics"]
    if not isinstance(metrics_value, Mapping):
        raise InputValidationError("observed_metrics must be a mapping")
    metrics: dict[str, MetricObservation] = {}
    for name, observation in metrics_value.items():
        if not isinstance(name, str) or not name or not isinstance(observation, Mapping):
            raise InputValidationError("observed metrics have an invalid entry")
        metrics[name] = MetricObservation.from_dict(observation)
    record = GameRecord(
        schema_version=1,
        appid=value["appid"],
        name=value["name"],
        release_status=value["release_status"],  # type: ignore[arg-type]
        store_url=value["store_url"],
        metrics=metrics,
        source_extra={},
    )
    if record.release_status != release_status:
        raise InputValidationError("candidate is in the wrong release pool")
    deltas = _numeric_mapping(value["deltas"], "deltas")
    metric_scores = _numeric_mapping(value["metric_scores"], "metric_scores")
    warnings = _warning_records_from_json(value["warnings"])
    evidence_value = value["evidence"]
    if not isinstance(evidence_value, list):
        raise InputValidationError("evidence must be an array")
    evidence: list[Evidence] = []
    for item in evidence_value:
        if not isinstance(item, Mapping) or set(item) != {"source", "url"}:
            raise InputValidationError("evidence entry has invalid fields")
        evidence.append(Evidence(item["source"], item["url"]))  # type: ignore[arg-type]
    content = value["recommended_content_types"]
    if not isinstance(content, list):
        raise InputValidationError("recommended_content_types must be an array")
    return ScoredCandidate(
        record=record,
        deltas=deltas,
        metric_scores=metric_scores,
        steam_heat_score=value["steam_heat_score"],
        seo_opportunity_score=value["seo_opportunity_score"],
        final_score=value["final_score"],
        action=value["action"],  # type: ignore[arg-type]
        confidence=value["confidence"],  # type: ignore[arg-type]
        warnings=warnings,
        evidence=evidence,
        recommended_content_types=content,
    )


def _numeric_mapping(value: object, name: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise InputValidationError(f"{name} must be a mapping")
    result: dict[str, float] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise InputValidationError(f"{name} keys must be non-empty strings")
        result[key] = _one_decimal(item, name)
    return result


def _canonical_newly_observed(value: Sequence[int] | object) -> list[int]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InputValidationError("newly_observed must be a sequence")
    appids: set[int] = set()
    for appid in value:
        if (
            isinstance(appid, bool)
            or not isinstance(appid, int)
            or appid <= 0
            or appid > MAX_STEAM_APPID
        ):
            raise InputValidationError(
                f"newly_observed AppIDs must be from 1 through {MAX_STEAM_APPID}"
            )
        appids.add(appid)
    return sorted(appids)


def _canonical_warnings(values: Sequence[WarningRecord]) -> list[dict[str, object]]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise InputValidationError("warnings must be a sequence")
    records = tuple(values)
    if not all(isinstance(item, WarningRecord) for item in records):
        raise InputValidationError("warnings must contain WarningRecord values")
    return [
        _warning_to_dict(warning)
        for warning in sorted(records, key=_warning_sort_key)
    ]


def _canonical_rejections(values: Sequence[RejectedRow]) -> list[dict[str, object]]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise InputValidationError("rejected_rows must be a sequence")
    records = tuple(values)
    if not all(isinstance(item, RejectedRow) for item in records):
        raise InputValidationError("rejected_rows must contain RejectedRow values")
    return [
        _rejection_to_dict(rejected)
        for rejected in sorted(records, key=_rejection_sort_key)
    ]


def _warning_to_dict(warning: WarningRecord) -> dict[str, object]:
    result: dict[str, object] = {
        "code": warning.code,
        "message": warning.message,
    }
    if warning.appid is not None:
        result["appid"] = warning.appid
    return result


def _rejection_to_dict(rejected: RejectedRow) -> dict[str, object]:
    result: dict[str, object] = {
        "row_number": rejected.row_number,
        "code": rejected.code,
        "message": rejected.message,
    }
    if rejected.appid is not None:
        result["appid"] = rejected.appid
    return result


def _warning_sort_key(warning: WarningRecord) -> tuple[object, ...]:
    return (
        warning.appid is not None,
        -1 if warning.appid is None else warning.appid,
        warning.code,
        warning.message,
    )


def _rejection_sort_key(rejected: RejectedRow) -> tuple[object, ...]:
    return (
        rejected.row_number,
        rejected.code,
        rejected.message,
        rejected.appid is not None,
        -1 if rejected.appid is None else rejected.appid,
    )


def _warning_records_from_json(value: object) -> tuple[WarningRecord, ...]:
    if not isinstance(value, list):
        raise InputValidationError("warnings must be an array")
    records: list[WarningRecord] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) not in (
            {"code", "message"},
            {"code", "message", "appid"},
        ):
            raise InputValidationError("warning has invalid fields")
        records.append(
            WarningRecord(
                code=item["code"],
                message=item["message"],
                appid=item.get("appid"),
            )
        )
    return tuple(records)


def _warnings_from_json(value: object) -> list[dict[str, object]]:
    records = _warning_records_from_json(value)
    return _canonical_warnings(records)


def _rejections_from_json(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise InputValidationError("rejected_rows must be an array")
    records: list[RejectedRow] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) not in (
            {"row_number", "code", "message"},
            {"row_number", "code", "message", "appid"},
        ):
            raise InputValidationError("rejected row has invalid fields")
        records.append(
            RejectedRow(
                row_number=item["row_number"],
                code=item["code"],
                message=item["message"],
                appid=item.get("appid"),
            )
        )
    return _canonical_rejections(records)


def _render_candidate_pool(
    lines: list[str],
    title: str,
    value: object,
) -> None:
    lines.extend([f"## {title}", ""])
    candidates = cast(list[Mapping[str, object]], value)
    if not candidates:
        lines.extend(["No candidates.", ""])
        return
    for index, candidate in enumerate(candidates, start=1):
        lines.extend(
            [
                f"### {index}. {candidate['name']} (AppID {candidate['appid']})",
                "",
                f"- Store: {candidate['store_url']}",
                f"- Confidence: {candidate['confidence']}",
                f"- Action: {candidate['action']}",
                "- Scores: "
                f"Steam heat {_display(candidate['steam_heat_score'])}; "
                f"SEO {_display(candidate['seo_opportunity_score'])}; "
                f"final {_display(candidate['final_score'])}",
                "- Observed metrics:",
            ]
        )
        metrics = cast(Mapping[str, Mapping[str, object]], candidate["observed_metrics"])
        if metrics:
            for name, observation in metrics.items():
                value_text = json.dumps(
                    observation["value"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                lines.append(
                    f"  - `{name}`: {value_text} "
                    f"(`{observation['source_id']}`, {observation['source_kind']}, "
                    f"{observation['observed_at']})"
                )
        else:
            lines.append("  - None")
        _render_mapping(lines, "Deltas", candidate["deltas"])
        _render_mapping(lines, "Component scores", candidate["metric_scores"])
        evidence = cast(list[Mapping[str, object]], candidate["evidence"])
        lines.append("- Evidence:")
        if evidence:
            for item in evidence:
                lines.append(f"  - {item['source']}: {item['url']}")
        else:
            lines.append("  - None")
        content = cast(list[str], candidate["recommended_content_types"])
        lines.append(
            "- Recommended content types: "
            + (", ".join(content) if content else "None")
        )
        candidate_warnings = cast(list[Mapping[str, object]], candidate["warnings"])
        lines.append("- Warnings:")
        if candidate_warnings:
            for warning in candidate_warnings:
                lines.append(f"  - `{warning['code']}`: {warning['message']}")
        else:
            lines.append("  - None")
        lines.append("")


def _render_mapping(lines: list[str], label: str, value: object) -> None:
    mapping = cast(Mapping[str, object], value)
    lines.append(f"- {label}:")
    if mapping:
        for key, item in mapping.items():
            lines.append(f"  - `{key}`: {_display(item)}")
    else:
        lines.append("  - None")


def _display(value: object) -> str:
    return "N/A" if value is None else str(value)


def _require_owned_lock(lock: RunLock, run_id: str) -> None:
    if not isinstance(lock, RunLock) or not getattr(lock, "_acquired", False):
        raise PersistenceError("report persistence requires an acquired RunLock")
    if lock.run_id != run_id:
        raise PersistenceError("report run_id does not match the acquired RunLock")
    payload = getattr(lock, "_payload", None)
    created_identity = getattr(lock, "_created_identity", None)
    parent_descriptor = getattr(lock, "_parent_descriptor", None)
    if (
        not isinstance(payload, dict)
        or payload.get("run_id") != run_id
        or not isinstance(created_identity, tuple)
        or len(created_identity) != 4
        or not isinstance(parent_descriptor, int)
        or parent_descriptor < 0
    ):
        raise PersistenceError("RunLock ownership state is incomplete")
    try:
        current, current_identity = lock._read_existing(parent_descriptor)
    except Exception as error:
        raise PersistenceError("owned RunLock cannot be verified") from error
    if current != payload or current_identity[:2] != created_identity[:2]:
        raise PersistenceError("RunLock ownership changed before report persistence")


def _open_report_directory(path: Path) -> int:
    if ".." in path.parts:
        raise InputValidationError("report path must not traverse parent directories")
    try:
        if path.is_absolute():
            descriptor = os.open(os.path.sep, _directory_flags())
            components = path.parts[1:]
        else:
            descriptor = os.open(".", _directory_flags())
            components = path.parts
    except OSError as error:
        raise PersistenceError("unable to open trusted report-path root") from error
    try:
        meaningful = tuple(component for component in components if component not in {"", "."})
        for index, component in enumerate(meaningful):
            next_descriptor = _open_report_child(
                descriptor,
                component,
                require_owner=index == len(meaningful) - 1,
            )
            old_descriptor = descriptor
            try:
                os.close(old_descriptor)
            except OSError as error:
                _close_descriptor(next_descriptor)
                descriptor = -1
                raise PersistenceError("unable to close traversed report directory") from error
            descriptor = next_descriptor
        _validate_directory(
            os.fstat(descriptor),
            require_owner=True,
            exact_mode=None,
        )
        return descriptor
    except Exception as error:
        if descriptor >= 0:
            _close_descriptor(descriptor)
        if isinstance(error, (InputValidationError, PersistenceError)):
            raise
        raise PersistenceError("unable to securely traverse report path") from error


def _open_report_child(
    parent_descriptor: int,
    name: str,
    *,
    require_owner: bool,
) -> int:
    created_identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except FileNotFoundError:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        except OSError as error:
            raise PersistenceError("unable to create report directory") from error
        else:
            created = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            _validate_directory(created, require_owner=True, exact_mode=None)
            created_identity = _inode(created)
            try:
                os.chmod(
                    name,
                    0o700,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise PersistenceError("unable to secure report directory") from error
        try:
            descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
        except OSError as error:
            raise PersistenceError("unable to open report directory") from error
    except OSError as error:
        raise PersistenceError("unable to open report directory") from error
    try:
        opened = os.fstat(descriptor)
        _validate_directory(
            opened,
            require_owner=require_owner or created_identity is not None,
            exact_mode=0o700 if created_identity is not None else None,
        )
        if created_identity is not None and _inode(opened) != created_identity:
            raise PersistenceError("new report directory changed before open")
        if require_owner:
            named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            _validate_directory(named, require_owner=True, exact_mode=None)
            if _inode(named) != _inode(opened):
                raise PersistenceError("report directory binding changed")
        return descriptor
    except Exception:
        _close_descriptor(descriptor)
        raise


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _validate_directory(
    metadata: os.stat_result,
    *,
    require_owner: bool,
    exact_mode: int | None,
) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise PersistenceError("report path component is not a directory")
    if exact_mode is not None and stat.S_IMODE(metadata.st_mode) != exact_mode:
        raise PersistenceError("new report directory must use mode 0700")
    if require_owner and hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PersistenceError("report directory has an unexpected owner")


def _persist_immutable_pair(
    directory_descriptor: int,
    json_name: str,
    json_data: bytes,
    markdown_name: str,
    markdown_data: bytes,
) -> None:
    if (
        _optional_name_metadata(directory_descriptor, json_name) is not None
        or _optional_name_metadata(directory_descriptor, markdown_name) is not None
    ):
        raise PersistenceError("timestamped report already exists")
    json_stage = _create_staged_file(directory_descriptor, ".json.tmp", json_data)
    markdown_stage: _StagedFile | None = None
    published: list[tuple[str, tuple[int, int]]] = []
    error: BaseException | None = None
    try:
        markdown_stage = _create_staged_file(
            directory_descriptor,
            ".md.tmp",
            markdown_data,
        )
        if (
            _optional_name_metadata(directory_descriptor, json_name) is not None
            or _optional_name_metadata(directory_descriptor, markdown_name) is not None
        ):
            raise PersistenceError("timestamped report already exists")
        _publish_no_replace(directory_descriptor, json_stage.name, json_name)
        published.append((json_name, json_stage.inode))
        _require_named_inode(directory_descriptor, json_name, json_stage.inode, len(json_data))
        _publish_no_replace(directory_descriptor, markdown_stage.name, markdown_name)
        published.append((markdown_name, markdown_stage.inode))
        _require_named_inode(
            directory_descriptor,
            markdown_name,
            markdown_stage.inode,
            len(markdown_data),
        )
        os.fsync(directory_descriptor)
    except BaseException as caught:
        error = caught
        expected_names = [(json_name, json_stage.inode)]
        if markdown_stage is not None:
            expected_names.append((markdown_name, markdown_stage.inode))
        reconciliation_error = _reconcile_published_names(
            directory_descriptor,
            published,
            expected_names,
        )
        rollback_error = _rollback_new_names(directory_descriptor, published)
        if reconciliation_error is not None or rollback_error is not None:
            error = reconciliation_error or rollback_error
    finally:
        cleanup_errors = [_cleanup_staged(directory_descriptor, json_stage)]
        if markdown_stage is not None:
            cleanup_errors.append(_cleanup_staged(directory_descriptor, markdown_stage))
        for cleanup_error in cleanup_errors:
            if cleanup_error is not None and error is None:
                error = cleanup_error
        _close_descriptor(json_stage.descriptor)
        if markdown_stage is not None:
            _close_descriptor(markdown_stage.descriptor)
    if error is not None:
        if isinstance(error, PersistenceError):
            raise error
        raise PersistenceError("unable to publish immutable report pair") from error


def _publish_no_replace(
    directory_descriptor: int,
    source_name: str,
    destination_name: str,
) -> None:
    os.link(
        source_name,
        destination_name,
        src_dir_fd=directory_descriptor,
        dst_dir_fd=directory_descriptor,
        follow_symlinks=False,
    )


def _load_latest_pair(directory_descriptor: int) -> dict[str, object] | None:
    json_metadata = _optional_name_metadata(directory_descriptor, "latest.json")
    markdown_metadata = _optional_name_metadata(directory_descriptor, "latest.md")
    if json_metadata is None and markdown_metadata is None:
        return None
    if json_metadata is None or markdown_metadata is None:
        raise PersistenceError("latest report pair is incomplete")
    json_data, _ = _read_named_file(directory_descriptor, "latest.json")
    markdown_data, _ = _read_named_file(directory_descriptor, "latest.md")
    parsed = _parse_report_json(json_data)
    try:
        canonical = _canonical_report(cast(Mapping[str, object], parsed))
        expected_json = _serialize_json(canonical).encode("utf-8")
        expected_markdown = render_markdown(canonical).encode("utf-8")
    except (InputValidationError, TypeError, ValueError, UnicodeError) as error:
        raise PersistenceError("latest report JSON is invalid") from error
    if json_data != expected_json:
        raise PersistenceError("latest report JSON is not canonical")
    if markdown_data != expected_markdown:
        raise PersistenceError("latest report pair does not match")
    return canonical


def _update_latest_pair(
    directory_descriptor: int,
    json_data: bytes,
    markdown_data: bytes,
    has_existing: bool,
) -> None:
    json_stage = _create_staged_file(directory_descriptor, ".latest.json.tmp", json_data)
    markdown_stage: _StagedFile | None = None
    error: BaseException | None = None
    try:
        markdown_stage = _create_staged_file(
            directory_descriptor,
            ".latest.md.tmp",
            markdown_data,
        )
        if has_existing:
            _replace_existing_latest(
                directory_descriptor,
                json_stage,
                markdown_stage,
                json_data,
                markdown_data,
            )
        else:
            _publish_new_latest(
                directory_descriptor,
                json_stage,
                markdown_stage,
                json_data,
                markdown_data,
            )
    except BaseException as caught:
        error = caught
    finally:
        for stage in (json_stage, markdown_stage):
            if stage is None:
                continue
            cleanup_error = _cleanup_staged(directory_descriptor, stage)
            if cleanup_error is not None and error is None:
                error = cleanup_error
            _close_descriptor(stage.descriptor)
    if error is not None:
        if isinstance(error, PersistenceError):
            raise error
        raise PersistenceError("unable to update latest report pair") from error


def _publish_new_latest(
    directory_descriptor: int,
    json_stage: _StagedFile,
    markdown_stage: _StagedFile,
    json_data: bytes,
    markdown_data: bytes,
) -> None:
    if (
        _optional_name_metadata(directory_descriptor, "latest.json") is not None
        or _optional_name_metadata(directory_descriptor, "latest.md") is not None
    ):
        raise PersistenceError("latest report appeared during publication")
    published: list[tuple[str, tuple[int, int]]] = []
    try:
        _publish_no_replace(directory_descriptor, json_stage.name, "latest.json")
        published.append(("latest.json", json_stage.inode))
        _require_named_inode(directory_descriptor, "latest.json", json_stage.inode, len(json_data))
        _publish_no_replace(directory_descriptor, markdown_stage.name, "latest.md")
        published.append(("latest.md", markdown_stage.inode))
        _require_named_inode(
            directory_descriptor,
            "latest.md",
            markdown_stage.inode,
            len(markdown_data),
        )
        os.fsync(directory_descriptor)
    except BaseException as error:
        reconciliation_error = _reconcile_published_names(
            directory_descriptor,
            published,
            (
                ("latest.json", json_stage.inode),
                ("latest.md", markdown_stage.inode),
            ),
        )
        rollback_error = _rollback_new_names(directory_descriptor, published)
        if reconciliation_error is not None:
            raise reconciliation_error from error
        if rollback_error is not None:
            raise rollback_error from error
        raise PersistenceError("unable to publish new latest report pair") from error


def _replace_existing_latest(
    directory_descriptor: int,
    json_stage: _StagedFile,
    markdown_stage: _StagedFile,
    json_data: bytes,
    markdown_data: bytes,
) -> None:
    old_json, old_json_metadata = _read_named_file(directory_descriptor, "latest.json")
    old_markdown, old_markdown_metadata = _read_named_file(directory_descriptor, "latest.md")
    old_json_inode = _inode(old_json_metadata)
    old_markdown_inode = _inode(old_markdown_metadata)
    json_backup = _unused_name(directory_descriptor, ".latest-json-backup-")
    markdown_backup = _unused_name(directory_descriptor, ".latest-md-backup-")
    backups: list[tuple[str, tuple[int, int]]] = []
    try:
        _link_named(directory_descriptor, "latest.json", json_backup)
        backups.append((json_backup, old_json_inode))
        _require_named_inode(directory_descriptor, json_backup, old_json_inode, len(old_json), expected_links=2)
        _link_named(directory_descriptor, "latest.md", markdown_backup)
        backups.append((markdown_backup, old_markdown_inode))
        _require_named_inode(
            directory_descriptor,
            markdown_backup,
            old_markdown_inode,
            len(old_markdown),
            expected_links=2,
        )
        _replace_staged_latest(directory_descriptor, json_stage.name, "latest.json")
        _require_named_inode(directory_descriptor, "latest.json", json_stage.inode, len(json_data))
        _replace_staged_latest(directory_descriptor, markdown_stage.name, "latest.md")
        _require_named_inode(
            directory_descriptor,
            "latest.md",
            markdown_stage.inode,
            len(markdown_data),
        )
        os.fsync(directory_descriptor)
    except BaseException as error:
        rollback_error = _restore_previous_latest(
            directory_descriptor,
            (
                ("latest.json", json_stage.inode, old_json_inode, json_backup),
                ("latest.md", markdown_stage.inode, old_markdown_inode, markdown_backup),
            ),
        )
        cleanup_error = _rollback_new_names(directory_descriptor, backups)
        try:
            os.fsync(directory_descriptor)
        except OSError as sync_error:
            cleanup_error = cleanup_error or PersistenceError(
                "unable to sync restored latest pair"
            )
            cleanup_error.__cause__ = sync_error
        if rollback_error is not None:
            raise rollback_error from error
        if cleanup_error is not None:
            raise cleanup_error from error
        raise PersistenceError("unable to replace latest report pair") from error
    cleanup_error = _rollback_new_names(directory_descriptor, backups)
    if cleanup_error is not None:
        raise cleanup_error
    try:
        os.fsync(directory_descriptor)
    except OSError as error:
        raise PersistenceError("unable to sync latest report publication") from error


def _replace_staged_latest(
    directory_descriptor: int,
    source_name: str,
    destination_name: str,
) -> None:
    os.replace(
        source_name,
        destination_name,
        src_dir_fd=directory_descriptor,
        dst_dir_fd=directory_descriptor,
    )


def _restore_previous_latest(
    directory_descriptor: int,
    entries: Sequence[
        tuple[str, tuple[int, int], tuple[int, int], str]
    ],
) -> PersistenceError | None:
    first_error: PersistenceError | None = None
    for name, new_inode, old_inode, backup_name in entries:
        try:
            current = _optional_name_metadata(directory_descriptor, name)
            if current is not None:
                current_inode = _inode(current)
                if current_inode == new_inode:
                    _remove_expected_name(directory_descriptor, name, new_inode)
                elif current_inode != old_inode:
                    raise PersistenceError(
                        "latest report name changed during rollback"
                    )
            if _optional_name_metadata(directory_descriptor, name) is None:
                _link_named(directory_descriptor, backup_name, name)
            backup = _name_metadata(directory_descriptor, backup_name)
            expected_size = backup.st_size
            _require_named_inode(
                directory_descriptor,
                name,
                old_inode,
                expected_size,
                expected_links=None,
            )
        except (OSError, PersistenceError) as error:
            if first_error is None:
                if isinstance(error, PersistenceError):
                    first_error = error
                else:
                    first_error = PersistenceError(
                        "unable to restore previous latest report pair"
                    )
                    first_error.__cause__ = error
    return first_error


def _create_staged_file(
    directory_descriptor: int,
    suffix: str,
    data: bytes,
) -> _StagedFile:
    _bounded_report(data)
    name = _unused_name(directory_descriptor, ".report-stage-", suffix)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_descriptor)
    except OSError as error:
        raise PersistenceError("unable to create staged report") from error
    try:
        os.fchmod(descriptor, 0o600)
        created = os.fstat(descriptor)
        _validate_regular(created, expected_size=0, expected_links=1)
        inode = _inode(created)
        _write_all(descriptor, data)
        os.fsync(descriptor)
        written = os.fstat(descriptor)
        _validate_regular(written, expected_size=len(data), expected_links=1)
        named = _name_metadata(directory_descriptor, name)
        _validate_regular(named, expected_size=len(data), expected_links=1)
        if _inode(written) != inode or _inode(named) != inode:
            raise PersistenceError("staged report changed during preparation")
        return _StagedFile(name, descriptor, inode, len(data))
    except Exception as error:
        stage = _StagedFile(name, descriptor, _inode(os.fstat(descriptor)), 0)
        _cleanup_staged(directory_descriptor, stage)
        _close_descriptor(descriptor)
        if isinstance(error, PersistenceError):
            raise
        raise PersistenceError("unable to prepare staged report") from error


def _cleanup_staged(
    directory_descriptor: int,
    stage: _StagedFile,
) -> PersistenceError | None:
    try:
        metadata = _optional_name_metadata(directory_descriptor, stage.name)
        if metadata is None:
            return None
        if _inode(metadata) != stage.inode:
            raise PersistenceError("staged report name was replaced")
        _remove_expected_name(directory_descriptor, stage.name, stage.inode)
        return None
    except (OSError, PersistenceError) as error:
        if isinstance(error, PersistenceError):
            return error
        wrapped = PersistenceError("unable to clean staged report")
        wrapped.__cause__ = error
        return wrapped


def _rollback_new_names(
    directory_descriptor: int,
    names: Sequence[tuple[str, tuple[int, int]]],
) -> PersistenceError | None:
    first_error: PersistenceError | None = None
    for name, inode in reversed(tuple(names)):
        try:
            metadata = _optional_name_metadata(directory_descriptor, name)
            if metadata is None:
                continue
            if _inode(metadata) != inode:
                raise PersistenceError("published report name changed during rollback")
            _remove_expected_name(directory_descriptor, name, inode)
        except (OSError, PersistenceError) as error:
            if first_error is None:
                if isinstance(error, PersistenceError):
                    first_error = error
                else:
                    first_error = PersistenceError("unable to roll back report pair")
                    first_error.__cause__ = error
    return first_error


def _reconcile_published_names(
    directory_descriptor: int,
    published: list[tuple[str, tuple[int, int]]],
    expected: Sequence[tuple[str, tuple[int, int]]],
) -> PersistenceError | None:
    """Discover links created by an operation that reported an ambiguous error."""

    known = {name for name, _inode_value in published}
    for name, inode in expected:
        if name in known:
            continue
        try:
            metadata = _optional_name_metadata(directory_descriptor, name)
        except PersistenceError as error:
            return error
        if metadata is None:
            continue
        if _inode(metadata) != inode:
            return PersistenceError(
                "an unrelated report artifact appeared during publication"
            )
        published.append((name, inode))
        known.add(name)
    return None


def _remove_expected_name(
    directory_descriptor: int,
    name: str,
    expected_inode: tuple[int, int],
) -> None:
    quarantine = _unused_name(directory_descriptor, ".report-quarantine-")
    try:
        os.rename(
            name,
            quarantine,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise PersistenceError("unable to quarantine report artifact") from error
    moved = _name_metadata(directory_descriptor, quarantine)
    if _inode(moved) != expected_inode:
        try:
            _link_named(directory_descriptor, quarantine, name)
        except OSError as restore_error:
            raise PersistenceError(
                "unexpected report artifact could not be restored"
            ) from restore_error
        raise PersistenceError("report artifact identity changed before removal")
    try:
        os.unlink(quarantine, dir_fd=directory_descriptor)
    except OSError as error:
        raise PersistenceError("unable to remove verified report artifact") from error


def _link_named(directory_descriptor: int, source: str, destination: str) -> None:
    os.link(
        source,
        destination,
        src_dir_fd=directory_descriptor,
        dst_dir_fd=directory_descriptor,
        follow_symlinks=False,
    )


def _read_named_file(
    directory_descriptor: int,
    name: str,
) -> tuple[bytes, os.stat_result]:
    before = _name_metadata(directory_descriptor, name)
    _validate_regular(before, expected_size=None, expected_links=1)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as error:
        raise PersistenceError("unable to securely open report artifact") from error
    try:
        opened = os.fstat(descriptor)
        _validate_regular(opened, expected_size=None, expected_links=1)
        if _file_identity(opened) != _file_identity(before):
            raise PersistenceError("report artifact changed while opening")
        data = _read_exact(descriptor, opened.st_size)
        after = os.fstat(descriptor)
        named = _name_metadata(directory_descriptor, name)
        _validate_regular(after, expected_size=opened.st_size, expected_links=1)
        _validate_regular(named, expected_size=opened.st_size, expected_links=1)
        if _file_identity(after) != _file_identity(opened) or _file_identity(named) != _file_identity(opened):
            raise PersistenceError("report artifact changed while reading")
        return data, after
    finally:
        _close_descriptor(descriptor)


def _parse_report_json(data: bytes) -> object:
    try:
        return json.loads(
            data.decode("utf-8", errors="strict"),
            parse_int=_parse_json_integer,
            parse_float=_parse_json_float,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise PersistenceError("unable to parse latest report JSON") from error


def _parse_json_integer(value: str) -> int:
    negative = value.startswith("-")
    digits = value[1:] if negative else value
    normalized = digits.lstrip("0") or "0"
    limit = str(abs(MIN_JSON_SAFE_INTEGER) if negative else MAX_JSON_SAFE_INTEGER)
    if len(normalized) > len(limit) or (
        len(normalized) == len(limit) and normalized > limit
    ):
        raise ValueError("report integer is outside the JSON-safe range")
    parsed = int(normalized)
    return -parsed if negative else parsed


def _parse_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("report float must be finite")
    return parsed


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("report JSON contains duplicate object keys")
        result[key] = value
    return result


def _name_metadata(directory_descriptor: int, name: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except OSError as error:
        raise PersistenceError("unable to inspect report artifact") from error


def _optional_name_metadata(
    directory_descriptor: int,
    name: str,
) -> os.stat_result | None:
    try:
        metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PersistenceError("unable to inspect report artifact") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PersistenceError("report artifact name is unsafe")
    return metadata


def _require_named_inode(
    directory_descriptor: int,
    name: str,
    expected_inode: tuple[int, int],
    expected_size: int,
    *,
    expected_links: int | None = None,
) -> None:
    metadata = _name_metadata(directory_descriptor, name)
    _validate_regular(
        metadata,
        expected_size=expected_size,
        expected_links=expected_links,
    )
    if _inode(metadata) != expected_inode:
        raise PersistenceError("report artifact identity changed")


def _validate_regular(
    metadata: os.stat_result,
    *,
    expected_size: int | None,
    expected_links: int | None,
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise PersistenceError("report artifact is not a regular file")
    if expected_links is not None and metadata.st_nlink != expected_links:
        raise PersistenceError("report artifact has an unexpected link count")
    if expected_size is not None and metadata.st_size != expected_size:
        raise PersistenceError("report artifact has an unexpected size")
    if metadata.st_size < 0 or metadata.st_size > _MAX_REPORT_BYTES:
        raise PersistenceError("report artifact exceeds the safe size limit")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise PersistenceError("report artifact must use mode 0600")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PersistenceError("report artifact has an unexpected owner")


def _read_exact(descriptor: int, size: int) -> bytes:
    if size < 0 or size > _MAX_REPORT_BYTES:
        raise PersistenceError("report artifact exceeds the safe size limit")
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
        if not chunk:
            raise PersistenceError("report artifact ended before its declared size")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise PersistenceError("report artifact grew while reading")
    return b"".join(chunks)


def _write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise PersistenceError("report write made no progress")
        remaining = remaining[written:]


def _unused_name(
    directory_descriptor: int,
    prefix: str,
    suffix: str = "",
) -> str:
    for _ in range(8):
        name = f"{prefix}{secrets.token_hex(16)}{suffix}"
        if _optional_name_metadata(directory_descriptor, name) is None:
            return name
    raise PersistenceError("unable to allocate private report artifact name")


def _bounded_report(data: bytes) -> None:
    if not isinstance(data, bytes) or len(data) > _MAX_REPORT_BYTES:
        raise PersistenceError("report exceeds the safe size limit")


def _literal(value: object, allowed: set[str], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise InputValidationError(f"invalid {name}: {value!r}")
    return value


def _one_decimal(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputValidationError(f"{name} must be a finite number")
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as error:
        raise InputValidationError(f"{name} must be a finite number") from error
    if not math.isfinite(parsed):
        raise InputValidationError(f"{name} must be a finite number")
    return round(parsed, 1)


def _optional_one_decimal(value: object) -> float | None:
    return None if value is None else _one_decimal(value, "score")


def _run_datetime(run_id: str) -> datetime:
    return datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ")


def _inode(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _close_descriptor(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass
