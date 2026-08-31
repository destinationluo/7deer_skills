"""Canonical unified reports and recoverable daily publication."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Protocol
from zoneinfo import ZoneInfo

from .config import RadarConfig
from .errors import InputValidationError, ReportError
from .score import opportunity_sort_key
from .schemas import (
    GameIdentity,
    OutstandingTask,
    PlatformRecord,
    PreliminaryResult,
    Publication,
    RadarRun,
    ScoredOpportunity,
    SourceHealth,
    WarningRecord,
)
from .storage import RadarStore


_PHASES = frozenset({"preliminary", "final"})
_PLATFORM_ORDER = {"itch": 0, "steam": 1, "roblox": 2}
_RUN_ID = re.compile(r"(?P<stamp>\d{8}T\d{6}Z)-[0-9a-f]{8,32}\Z")
_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "phase",
        "generated_at",
        "candidates",
        "source_health",
        "warnings",
        "outstanding_tasks",
    }
)
_CANDIDATE_KEYS = frozenset(
    {
        "rank",
        "opportunity_id",
        "name",
        "normalized_name",
        "developer",
        "official_domain",
        "platforms",
        "evidence_timestamps",
        "evidence_urls",
        "demand_state",
        "component_scores",
        "total_score",
        "action",
        "warnings",
    }
)
_COMPONENT_KEYS = frozenset({"platform", "demand", "external", "seo"})
_TIMESTAMP_KEYS = frozenset({"collector", "observed_at"})


class AtomicWriter(Protocol):
    """Minimal injectable atomic-text writer used by report persistence."""

    def write_text(self, path: Path, content: str) -> None: ...


class FileAtomicWriter:
    """Write complete UTF-8 text by replacing from a sibling temporary file."""

    def write_text(self, path: Path, content: str) -> None:
        destination = Path(path)
        temporary_path: Path | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_symlink():
                raise ReportError("report destination must not be a symlink")
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, destination)
            temporary_path = None
            _fsync_directory(destination.parent)
        except ReportError:
            raise
        except OSError as error:
            raise ReportError("unable to atomically write report file") from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass


def _fsync_directory(directory: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _invalid(message: str) -> InputValidationError:
    return InputValidationError(message)


def _exact_keys(value: object, expected: frozenset[str], owner: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _invalid(f"{owner} must be a JSON object")
    actual = set(value)
    if actual != expected:
        unexpected = actual - expected
        missing = expected - actual
        details: list[str] = []
        if unexpected:
            details.append("unexpected " + ", ".join(sorted(unexpected)))
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        raise _invalid(f"{owner} has invalid keys: {'; '.join(details)}")
    return value


def _run_datetime(run_id: object) -> datetime:
    if not isinstance(run_id, str):
        raise _invalid("run_id must be text")
    match = _RUN_ID.fullmatch(run_id)
    if match is None:
        raise _invalid("run_id is invalid")
    try:
        parsed = datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ")
    except ValueError as error:
        raise _invalid("run_id timestamp is invalid") from error
    return parsed.replace(tzinfo=timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise _invalid(f"{name} must be a canonical UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise _invalid(f"{name} must be a canonical UTC timestamp") from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise _invalid(f"{name} must be a canonical UTC timestamp")
    return parsed.replace(tzinfo=timezone.utc)


def _utc_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _invalid(f"{name} must be timezone-aware")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError) as error:
        raise _invalid(f"{name} must be a valid timestamp") from error
    if offset is None:
        raise _invalid(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _json_copy(value: object) -> object:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        serialized.encode("utf-8")
        return json.loads(serialized)
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise _invalid("report must contain finite JSON-native values") from error


def _canonical_json(report: Mapping[str, object]) -> str:
    canonical = _canonical_report(report)
    return json.dumps(
        canonical,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def _warning_key(warning: WarningRecord) -> tuple[object, ...]:
    return (
        warning.code,
        warning.collector or "",
        warning.opportunity_id or "",
        warning.message,
    )


def _task_key(task: OutstandingTask) -> tuple[object, ...]:
    return (task.collector, task.surface, task.action)


def _candidate_payload(
    candidate: GameIdentity,
    scored: ScoredOpportunity | None,
    health_by_collector: Mapping[str, SourceHealth],
) -> dict[str, object]:
    records = sorted(
        candidate.platform_records,
        key=lambda item: (_PLATFORM_ORDER[item.platform], item.platform_id),
    )
    timestamps = [
        {
            "collector": record.platform,
            "observed_at": health_by_collector[record.platform].to_dict()["observed_at"],
        }
        for record in records
        if record.platform in health_by_collector
    ]
    score_warnings: list[dict[str, object]] = []
    component_scores: dict[str, object] | None = None
    demand_state: str | None = None
    total_score: float | None = None
    action: str | None = None
    if scored is not None:
        component_scores = {
            "platform": scored.platform_score,
            "demand": scored.demand_score,
            "external": scored.external_score,
            "seo": scored.seo_score,
        }
        demand_state = scored.demand_state
        total_score = scored.total_score
        action = scored.action
        score_warnings = [
            item.to_dict() for item in sorted(scored.warnings, key=_warning_key)
        ]
    return {
        "rank": 0,
        "opportunity_id": candidate.opportunity_id,
        "name": candidate.name,
        "normalized_name": candidate.normalized_name,
        "developer": candidate.developer,
        "official_domain": candidate.official_domain,
        "platforms": [item.to_dict() for item in records],
        "evidence_timestamps": timestamps,
        "evidence_urls": list(dict.fromkeys(item.url for item in records)),
        "demand_state": demand_state,
        "component_scores": component_scores,
        "total_score": total_score,
        "action": action,
        "warnings": score_warnings,
    }


def build_report(
    result: PreliminaryResult,
    scores: Sequence[ScoredOpportunity],
    phase: str,
) -> Mapping[str, object]:
    """Build the sole canonical report model from persisted schema records."""

    if not isinstance(result, PreliminaryResult):
        raise _invalid("result must be a PreliminaryResult")
    if phase not in _PHASES:
        raise _invalid("phase must be preliminary or final")
    if isinstance(scores, (str, bytes)) or not isinstance(scores, Sequence):
        raise _invalid("scores must be a sequence of ScoredOpportunity")

    score_by_id: dict[str, ScoredOpportunity] = {}
    candidate_by_id = {
        candidate.opportunity_id: candidate for candidate in result.candidates
    }
    for index, scored in enumerate(scores):
        if not isinstance(scored, ScoredOpportunity):
            raise _invalid(f"scores[{index}] must be a ScoredOpportunity")
        if scored.run_id != result.run_id:
            raise _invalid("score run_id must match result run_id")
        if scored.opportunity_id not in candidate_by_id:
            raise _invalid("score opportunity_id must belong to a candidate")
        if scored.opportunity_id in score_by_id:
            raise _invalid("scores must not contain duplicate opportunity IDs")
        score_by_id[scored.opportunity_id] = scored
    if phase == "final" and set(score_by_id) != set(candidate_by_id):
        raise _invalid("final reports require exactly one score per candidate")

    health_by_collector = {
        health.collector: health for health in result.source_health
    }
    ordered_candidates = sorted(
        result.candidates,
        key=lambda candidate: (
            (0,) + opportunity_sort_key(
                score_by_id[candidate.opportunity_id],
                candidate.normalized_name,
            )
            if candidate.opportunity_id in score_by_id
            else (1, candidate.normalized_name, candidate.opportunity_id)
        ),
    )
    candidates: list[dict[str, object]] = []
    for rank, candidate in enumerate(ordered_candidates, start=1):
        payload = _candidate_payload(
            candidate,
            score_by_id.get(candidate.opportunity_id),
            health_by_collector,
        )
        payload["rank"] = rank
        candidates.append(payload)

    report = {
        "schema_version": 1,
        "run_id": result.run_id,
        "phase": phase,
        "generated_at": _format_utc(_run_datetime(result.run_id)),
        "candidates": candidates,
        "source_health": [
            item.to_dict()
            for item in sorted(
                result.source_health,
                key=lambda health: _PLATFORM_ORDER[health.collector],
            )
        ],
        "warnings": [
            item.to_dict() for item in sorted(result.warnings, key=_warning_key)
        ],
        "outstanding_tasks": [
            item.to_dict()
            for item in sorted(result.outstanding_tasks, key=_task_key)
        ],
    }
    return _canonical_report(report)


def _canonical_report(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _invalid("report must be a mapping")
    copied = _json_copy(value)
    report = _exact_keys(copied, _REPORT_KEYS, "report")
    if type(report["schema_version"]) is not int or report["schema_version"] != 1:
        raise _invalid("report schema_version must be 1")
    run_id = report["run_id"]
    run_time = _run_datetime(run_id)
    if not isinstance(report["phase"], str) or report["phase"] not in _PHASES:
        raise _invalid("report phase must be preliminary or final")
    if _parse_utc(report["generated_at"], "generated_at") != run_time:
        raise _invalid("generated_at must equal the run timestamp")

    health_rows = report["source_health"]
    if not isinstance(health_rows, list):
        raise _invalid("source_health must be an array")
    source_health = tuple(SourceHealth.from_dict(item) for item in health_rows)
    if any(item.run_id != run_id for item in source_health):
        raise _invalid("source health run_id must match report")
    if len({item.collector for item in source_health}) != len(source_health):
        raise _invalid("source_health collectors must be unique")
    health_by_collector = {item.collector: item for item in source_health}

    warning_rows = report["warnings"]
    if not isinstance(warning_rows, list):
        raise _invalid("warnings must be an array")
    tuple(WarningRecord.from_dict(item) for item in warning_rows)
    task_rows = report["outstanding_tasks"]
    if not isinstance(task_rows, list):
        raise _invalid("outstanding_tasks must be an array")
    tasks = tuple(OutstandingTask.from_dict(item) for item in task_rows)
    if any(item.run_id != run_id for item in tasks):
        raise _invalid("outstanding task run_id must match report")

    candidate_rows = report["candidates"]
    if not isinstance(candidate_rows, list):
        raise _invalid("candidates must be an array")
    opportunity_ids: set[str] = set()
    for expected_rank, raw_candidate in enumerate(candidate_rows, start=1):
        candidate = _exact_keys(raw_candidate, _CANDIDATE_KEYS, "candidate")
        if type(candidate["rank"]) is not int or candidate["rank"] != expected_rank:
            raise _invalid("candidate ranks must be consecutive and ordered")
        platforms_value = candidate["platforms"]
        if not isinstance(platforms_value, list):
            raise _invalid("candidate platforms must be an array")
        platforms = tuple(PlatformRecord.from_dict(item) for item in platforms_value)
        identity = GameIdentity(
            schema_version=1,
            opportunity_id=candidate["opportunity_id"],  # type: ignore[arg-type]
            name=candidate["name"],  # type: ignore[arg-type]
            normalized_name=candidate["normalized_name"],  # type: ignore[arg-type]
            developer=candidate["developer"],  # type: ignore[arg-type]
            official_domain=candidate["official_domain"],  # type: ignore[arg-type]
            platform_records=platforms,
        )
        if identity.opportunity_id in opportunity_ids:
            raise _invalid("candidate opportunity IDs must be unique")
        opportunity_ids.add(identity.opportunity_id)

        evidence_urls = candidate["evidence_urls"]
        if not isinstance(evidence_urls, list) or evidence_urls != list(
            dict.fromkeys(record.url for record in platforms)
        ):
            raise _invalid("candidate evidence_urls must match platform provenance")
        timestamps = candidate["evidence_timestamps"]
        if not isinstance(timestamps, list):
            raise _invalid("evidence_timestamps must be an array")
        expected_timestamps = []
        for platform in platforms:
            if platform.platform in health_by_collector:
                expected_timestamps.append(
                    {
                        "collector": platform.platform,
                        "observed_at": health_by_collector[platform.platform].to_dict()["observed_at"],
                    }
                )
        for timestamp in timestamps:
            row = _exact_keys(timestamp, _TIMESTAMP_KEYS, "evidence timestamp")
            _parse_utc(row["observed_at"], "evidence observed_at")
        if timestamps != expected_timestamps:
            raise _invalid("evidence_timestamps must match source health provenance")

        candidate_warnings = candidate["warnings"]
        if not isinstance(candidate_warnings, list):
            raise _invalid("candidate warnings must be an array")
        parsed_warnings = tuple(
            WarningRecord.from_dict(item) for item in candidate_warnings
        )
        components = candidate["component_scores"]
        if components is None:
            if report["phase"] == "final":
                raise _invalid("final candidates require component scores")
            if any(
                candidate[field] is not None
                for field in ("demand_state", "total_score", "action")
            ) or parsed_warnings:
                raise _invalid("unscored preliminary candidates must use null score fields")
        else:
            parsed_components = _exact_keys(
                components,
                _COMPONENT_KEYS,
                "component_scores",
            )
            scored = ScoredOpportunity(
                schema_version=1,
                run_id=run_id,  # type: ignore[arg-type]
                opportunity_id=identity.opportunity_id,
                demand_state=candidate["demand_state"],  # type: ignore[arg-type]
                platform_score=parsed_components["platform"],  # type: ignore[arg-type]
                demand_score=parsed_components["demand"],  # type: ignore[arg-type]
                external_score=parsed_components["external"],  # type: ignore[arg-type]
                seo_score=parsed_components["seo"],  # type: ignore[arg-type]
                total_score=candidate["total_score"],  # type: ignore[arg-type]
                action=candidate["action"],  # type: ignore[arg-type]
                warnings=parsed_warnings,
            )
            if scored.opportunity_id != identity.opportunity_id:
                raise _invalid("score opportunity ID must match candidate")
    return report


def _markdown_text(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    for character in "\\`*_{}[]()#+-.!|>":
        text = text.replace(character, f"\\{character}")
    return text


def render_markdown(report: Mapping[str, object]) -> str:
    """Render Markdown solely by walking the validated canonical mapping."""

    canonical = _canonical_report(report)
    lines = [
        "# Unified Game Opportunity Radar",
        "",
        f"- Run ID: `{canonical['run_id']}`",
        f"- Phase: `{canonical['phase']}`",
        f"- Generated at: `{canonical['generated_at']}`",
        "",
        "## Source health",
        "",
    ]
    for item in canonical["source_health"]:  # type: ignore[union-attr]
        lines.append(
            f"- `{item['collector']}`: `{item['status']}` at `{item['observed_at']}`"
        )
        for health_warning in item["warnings"]:
            lines.append(
                f"  - `{health_warning['code']}`: "
                f"{_markdown_text(health_warning['message'])}"
            )
    if not canonical["source_health"]:
        lines.append("- None")
    lines.extend(["", "## Candidates", ""])
    for candidate in canonical["candidates"]:  # type: ignore[union-attr]
        lines.extend(
            [
                f"### {candidate['rank']}. {_markdown_text(candidate['name'])}",
                "",
                f"- Opportunity ID: `{candidate['opportunity_id']}`",
                f"- Action: `{candidate['action']}`",
                f"- Demand state: `{candidate['demand_state']}`",
                f"- Total score: `{candidate['total_score']}`",
            ]
        )
        components = candidate["component_scores"]
        if components is None:
            lines.append("- Components: not scored")
        else:
            lines.append(
                "- Components (platform / demand / external / SEO): "
                f"`{components['platform']} / {components['demand']} / "
                f"{components['external']} / {components['seo']}`"
            )
        platform_keys = ", ".join(
            f"{item['platform']}:{item['platform_id']}"
            for item in candidate["platforms"]
        )
        lines.append(f"- Platforms: `{platform_keys}`")
        timestamps = ", ".join(
            f"{item['collector']} {item['observed_at']}"
            for item in candidate["evidence_timestamps"]
        )
        lines.append(f"- Evidence timestamps: `{timestamps}`")
        lines.append("- Evidence URLs:")
        if candidate["evidence_urls"]:
            lines.extend(f"  - <{url}>" for url in candidate["evidence_urls"])
        else:
            lines.append("  - None")
        lines.append("- Warnings:")
        if candidate["warnings"]:
            for candidate_warning in candidate["warnings"]:
                lines.append(
                    f"  - `{candidate_warning['code']}`: "
                    f"{_markdown_text(candidate_warning['message'])}"
                )
        else:
            lines.append("  - None")
        lines.append("")

    lines.extend(["## Run warnings", ""])
    if canonical["warnings"]:
        for run_warning in canonical["warnings"]:  # type: ignore[union-attr]
            lines.append(
                f"- `{run_warning['code']}`: {_markdown_text(run_warning['message'])}"
            )
    else:
        lines.append("- None")
    lines.extend(["", "## Outstanding tasks", ""])
    if canonical["outstanding_tasks"]:
        for task in canonical["outstanding_tasks"]:  # type: ignore[union-attr]
            lines.append(
                f"- `{task['collector']}:{task['surface']}` → `{task['action']}`"
            )
    else:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def _writer(value: object) -> AtomicWriter:
    method = getattr(value, "write_text", None)
    if not callable(method):
        raise _invalid("writer must provide write_text(path, content)")
    return value  # type: ignore[return-value]


def _read_existing(path: Path) -> str | None:
    try:
        if path.is_symlink():
            raise ReportError("report path must not be a symlink")
        if not path.exists():
            return None
        if not path.is_file():
            raise ReportError("report path must be a regular file")
        return path.read_text(encoding="utf-8")
    except ReportError:
        raise
    except (OSError, UnicodeError) as error:
        raise ReportError("unable to read report file") from error


def _write_content(
    path: Path,
    content: str,
    writer: AtomicWriter,
    *,
    immutable: bool,
) -> None:
    existing = _read_existing(path)
    if existing == content:
        return
    if immutable and existing is not None:
        raise ReportError("immutable run report conflicts with existing content")
    try:
        writer.write_text(path, content)
    except ReportError:
        raise
    except Exception as error:
        raise ReportError("unable to write report file") from error
    if _read_existing(path) != content:
        raise ReportError("atomic writer did not persist exact report content")


def _run_paths(
    report: Mapping[str, object],
    report_dir: Path,
) -> tuple[Path, Path]:
    run_id = str(report["run_id"])
    phase = str(report["phase"])
    return (
        report_dir / f"{run_id}.{phase}.json",
        report_dir / f"{run_id}.{phase}.md",
    )


def persist_run_artifacts(
    report: Mapping[str, object],
    report_dir: Path,
    writer: AtomicWriter,
) -> tuple[Path, Path]:
    """Persist the immutable canonical run JSON/Markdown pair in order."""

    canonical = _canonical_report(report)
    if not isinstance(report_dir, Path):
        raise _invalid("report_dir must be a Path")
    parsed_writer = _writer(writer)
    paths = _run_paths(canonical, report_dir)
    _write_content(
        paths[0],
        _canonical_json(canonical),
        parsed_writer,
        immutable=True,
    )
    _write_content(
        paths[1],
        render_markdown(canonical),
        parsed_writer,
        immutable=True,
    )
    return paths


def _load_report(path: Path) -> dict[str, object] | None:
    content = _read_existing(path)
    if content is None:
        return None
    try:
        parsed = json.loads(content)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ReportError("existing canonical report JSON is invalid") from error
    try:
        return _canonical_report(parsed)
    except InputValidationError as error:
        raise ReportError("existing canonical report schema is invalid") from error


def _report_order(report: Mapping[str, object]) -> tuple[object, ...]:
    canonical = _canonical_report(report)
    return (
        _run_datetime(canonical["run_id"]),
        str(canonical["run_id"]),
        1 if canonical["phase"] == "final" else 0,
    )


def _persist_mutable_pair(
    report: Mapping[str, object],
    json_path: Path,
    markdown_path: Path,
    writer: AtomicWriter,
) -> None:
    canonical = _canonical_report(report)
    _write_content(
        json_path,
        _canonical_json(canonical),
        writer,
        immutable=False,
    )
    _write_content(
        markdown_path,
        render_markdown(canonical),
        writer,
        immutable=False,
    )


def _publication_allowed(
    config: RadarConfig,
    run: RadarRun,
    report: Mapping[str, object],
) -> bool:
    if report["phase"] != "final":
        return False
    if run.mode == "manual":
        return run.publish_daily
    local_started_at = run.started_at.astimezone(ZoneInfo(config.timezone))
    return local_started_at.hour == config.daily_publish_hour


def _record_publication(store: RadarStore, publication: Publication) -> Publication:
    with store.transaction():
        existing = store.get_publication(publication.run_id, publication.phase)
        if existing is not None:
            if (
                existing.report_json != publication.report_json
                or existing.report_markdown != publication.report_markdown
                or existing.daily_date != publication.daily_date
                or existing.advances_daily_latest
                != publication.advances_daily_latest
            ):
                raise ReportError("publication retry conflicts with canonical record")
            return existing
        store.publish(publication)
    return publication


def publish_daily_if_allowed(
    config: RadarConfig,
    store: RadarStore,
    run: RadarRun,
    report: Mapping[str, object],
    run_paths: tuple[Path, Path],
    now: datetime,
    writer: AtomicWriter,
) -> Publication:
    """Repair daily files and record publication only after every file succeeds."""

    if not isinstance(config, RadarConfig):
        raise _invalid("config must be a RadarConfig")
    if not isinstance(store, RadarStore):
        raise _invalid("store must be a RadarStore")
    if not isinstance(run, RadarRun):
        raise _invalid("run must be a RadarRun")
    canonical = _canonical_report(report)
    if canonical["run_id"] != run.run_id:
        raise _invalid("report run_id must match run")
    if _run_datetime(run.run_id) != run.started_at:
        raise _invalid("run started_at must equal the run_id timestamp")
    published_at = _utc_datetime(now, "now")
    parsed_writer = _writer(writer)
    if (
        not isinstance(run_paths, tuple)
        or len(run_paths) != 2
        or any(not isinstance(path, Path) for path in run_paths)
    ):
        raise _invalid("run_paths must be a JSON/Markdown Path pair")
    expected_paths = _run_paths(canonical, Path(config.report_dir))
    if run_paths != expected_paths:
        raise _invalid("run_paths must match the configured report directory")
    expected_run_content = (
        _canonical_json(canonical),
        render_markdown(canonical),
    )
    if tuple(_read_existing(path) for path in run_paths) != expected_run_content:
        raise ReportError("immutable run report pair is incomplete or inconsistent")

    daily_date = run.started_at.astimezone(ZoneInfo(config.timezone)).date()
    if not _publication_allowed(config, run, canonical):
        publication = Publication(
            schema_version=1,
            run_id=run.run_id,
            phase=canonical["phase"],  # type: ignore[arg-type]
            published_at=published_at,
            report_json=str(run_paths[0]),
            report_markdown=str(run_paths[1]),
            daily_date=daily_date,
            advances_daily_latest=False,
        )
        return _record_publication(store, publication)

    daily_root = Path(config.report_dir) / "daily"
    date_stem = daily_date.isoformat()
    dated_paths = (
        daily_root / f"{date_stem}.json",
        daily_root / f"{date_stem}.md",
    )
    existing_dated = _load_report(dated_paths[0])
    dated_target = canonical
    if existing_dated is not None and _report_order(existing_dated) > _report_order(canonical):
        dated_target = existing_dated
    _persist_mutable_pair(
        dated_target,
        dated_paths[0],
        dated_paths[1],
        parsed_writer,
    )
    dated_is_current = dated_target["run_id"] == run.run_id

    latest_paths = (daily_root / "latest.json", daily_root / "latest.md")
    existing_latest = _load_report(latest_paths[0])
    latest_target = canonical
    if existing_latest is not None and _report_order(existing_latest) > _report_order(canonical):
        latest_target = existing_latest
    _persist_mutable_pair(
        latest_target,
        latest_paths[0],
        latest_paths[1],
        parsed_writer,
    )
    advances_latest = dated_is_current and latest_target["run_id"] == run.run_id
    published_paths = dated_paths if dated_is_current else run_paths
    publication = Publication(
        schema_version=1,
        run_id=run.run_id,
        phase=canonical["phase"],  # type: ignore[arg-type]
        published_at=published_at,
        report_json=str(published_paths[0]),
        report_markdown=str(published_paths[1]),
        daily_date=daily_date,
        advances_daily_latest=advances_latest,
    )
    return _record_publication(store, publication)


__all__ = [
    "AtomicWriter",
    "FileAtomicWriter",
    "build_report",
    "persist_run_artifacts",
    "publish_daily_if_allowed",
    "render_markdown",
]
