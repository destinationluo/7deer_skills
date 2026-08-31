from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import sys
import threading
import unittest
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from unified_game_radar.config import RadarConfig
from unified_game_radar.errors import InputValidationError, ReportError
import unified_game_radar.report as report_module
from unified_game_radar.report import (
    FileAtomicWriter,
    build_report,
    persist_run_artifacts,
    publish_daily_if_allowed,
    render_markdown,
)
from unified_game_radar.schemas import (
    GameIdentity,
    OutstandingTask,
    PlatformRecord,
    PreliminaryResult,
    RadarRun,
    ScoredOpportunity,
    SourceHealth,
    WarningRecord,
)
from unified_game_radar.storage import RadarStore


RUN_ID = "20260831T020000Z-a1b2c3d4"
SECOND_RUN_ID = "20260901T080000Z-b1c2d3e4"
OPPORTUNITY_A = "0f840f6f-5c62-4ca6-9d53-e0be9ab2740b"
OPPORTUNITY_B = "1f840f6f-5c62-4ca6-9d53-e0be9ab2740b"


def warning(
    *,
    code: str = "partial_source",
    collector: str | None = "steam",
    opportunity_id: str | None = None,
) -> WarningRecord:
    return WarningRecord(
        schema_version=1,
        code=code,
        message=f"Warning: {code}",
        collector=collector,
        opportunity_id=opportunity_id,
    )


def record(
    platform: str,
    platform_id: str,
    name: str,
    url: str,
) -> PlatformRecord:
    return PlatformRecord(
        schema_version=1,
        platform=platform,
        platform_id=platform_id,
        name=name,
        developer="Example Studio",
        official_domain="example.com",
        url=url,
    )


def identity(
    opportunity_id: str,
    name: str,
    records: tuple[PlatformRecord, ...],
) -> GameIdentity:
    return GameIdentity(
        schema_version=1,
        opportunity_id=opportunity_id,
        name=name,
        normalized_name=name.casefold(),
        developer="Example Studio",
        official_domain="example.com",
        platform_records=records,
    )


def health(
    collector: str,
    *,
    run_id: str = RUN_ID,
    observed_at: datetime | None = None,
    status: str = "fresh",
) -> SourceHealth:
    observed = observed_at or datetime(2026, 8, 31, 2, 5, tzinfo=timezone.utc)
    return SourceHealth(
        schema_version=1,
        run_id=run_id,
        collector=collector,
        status=status,
        observed_at=observed,
        capabilities={"discovery": status == "fresh"},
        warnings=() if status == "fresh" else (warning(collector=collector),),
    )


def score(
    opportunity_id: str,
    *,
    run_id: str = RUN_ID,
    platform_score: float = 24.0,
    demand_score: float = 26.0,
    external_score: float = 14.0,
    seo_score: float = 16.0,
    action: str = "immediate_action",
    demand_state: str = "pass",
) -> ScoredOpportunity:
    return ScoredOpportunity(
        schema_version=1,
        run_id=run_id,
        opportunity_id=opportunity_id,
        demand_state=demand_state,
        platform_score=platform_score,
        demand_score=demand_score,
        external_score=external_score,
        seo_score=seo_score,
        total_score=platform_score + demand_score + external_score + seo_score,
        action=action,
        warnings=(warning(code="candidate_note", opportunity_id=opportunity_id),),
    )


def preliminary_result(run_id: str = RUN_ID) -> PreliminaryResult:
    first = identity(
        OPPORTUNITY_A,
        "Signal Garden",
        (
            record(
                "steam",
                "123456",
                "Signal Garden",
                "https://store.steampowered.com/app/123456/",
            ),
            record(
                "roblox",
                "987654",
                "Signal Garden",
                "https://www.roblox.com/games/123456/Signal-Garden",
            ),
        ),
    )
    second = identity(
        OPPORTUNITY_B,
        "Pocket Workshop",
        (
            record(
                "itch",
                "tiny.pocket-workshop",
                "Pocket Workshop",
                "https://tiny.itch.io/pocket-workshop",
            ),
        ),
    )
    observed = datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ").replace(
        tzinfo=timezone.utc
    )
    task = OutstandingTask(
        schema_version=1,
        run_id=run_id,
        collector="roblox",
        surface="rising",
        action="collect_browser_observations",
        collection_contract={"schema_version": 1, "max_rows": 200},
    )
    return PreliminaryResult(
        schema_version=1,
        run_id=run_id,
        candidates=(first, second),
        source_health=(
            health("itch", run_id=run_id, observed_at=observed),
            health(
                "steam",
                run_id=run_id,
                observed_at=observed,
                status="partial",
            ),
            health("roblox", run_id=run_id, observed_at=observed),
        ),
        warnings=(warning(code="run_note", collector=None),),
        outstanding_tasks=(task,),
    )


def final_scores(run_id: str = RUN_ID) -> tuple[ScoredOpportunity, ...]:
    return (
        score(OPPORTUNITY_A, run_id=run_id),
        score(
            OPPORTUNITY_B,
            run_id=run_id,
            platform_score=15.0,
            demand_score=18.0,
            external_score=8.0,
            seo_score=12.0,
            action="watch",
            demand_state="early_watch",
        ),
    )


def radar_run(
    *,
    run_id: str = RUN_ID,
    started_at: datetime | None = None,
    mode: str = "scheduled",
    publish_daily: bool = False,
) -> RadarRun:
    started = started_at or datetime.strptime(
        run_id[:16], "%Y%m%dT%H%M%SZ"
    ).replace(tzinfo=timezone.utc)
    return RadarRun(
        schema_version=1,
        run_id=run_id,
        started_at=started,
        mode=mode,
        platforms=("itch", "steam", "roblox"),
        publish_daily=publish_daily,
    )


def canonical_json(report: object) -> str:
    return json.dumps(
        report,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


class FailAfterWriter:
    def __init__(self, fail_on: int) -> None:
        self.fail_on = fail_on
        self.calls = 0
        self.delegate = FileAtomicWriter()

    def write_text_at(
        self,
        directory_fd: int,
        filename: str,
        content: str,
    ) -> None:
        self.calls += 1
        self.delegate.write_text_at(directory_fd, filename, content)
        if self.calls == self.fail_on:
            raise OSError(f"failure after write {self.fail_on}")

    def write_text_immutable_at(
        self,
        directory_fd: int,
        filename: str,
        content: str,
    ) -> None:
        self.calls += 1
        self.delegate.write_text_immutable_at(directory_fd, filename, content)
        if self.calls == self.fail_on:
            raise OSError(f"failure after write {self.fail_on}")


class ReportBuildTests(unittest.TestCase):
    def test_builds_exact_final_schema_with_scores_provenance_and_evidence(self) -> None:
        report = build_report(preliminary_result(), final_scores(), "final")

        self.assertEqual(
            set(report),
            {
                "schema_version",
                "run_id",
                "phase",
                "generated_at",
                "candidates",
                "source_health",
                "warnings",
                "outstanding_tasks",
            },
        )
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["run_id"], RUN_ID)
        self.assertEqual(report["phase"], "final")
        self.assertEqual(report["generated_at"], "2026-08-31T02:00:00Z")

        candidates = report["candidates"]
        self.assertIsInstance(candidates, list)
        first = candidates[0]
        self.assertEqual(
            set(first),
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
            },
        )
        self.assertEqual(first["rank"], 1)
        self.assertEqual(first["opportunity_id"], OPPORTUNITY_A)
        self.assertEqual(
            first["component_scores"],
            {"platform": 24.0, "demand": 26.0, "external": 14.0, "seo": 16.0},
        )
        self.assertEqual(first["total_score"], 80.0)
        self.assertEqual(first["action"], "immediate_action")
        self.assertEqual(
            [item["platform"] for item in first["platforms"]],
            ["steam", "roblox"],
        )
        self.assertEqual(
            first["evidence_urls"],
            [
                "https://store.steampowered.com/app/123456/",
                "https://www.roblox.com/games/123456/Signal-Garden",
            ],
        )
        self.assertEqual(
            [item["collector"] for item in first["evidence_timestamps"]],
            ["steam", "roblox"],
        )
        self.assertEqual(
            {item["collector"] for item in report["source_health"]},
            {"itch", "steam", "roblox"},
        )
        self.assertEqual(report["warnings"][0]["code"], "run_note")
        self.assertEqual(first["warnings"][0]["code"], "candidate_note")
        self.assertEqual(report["outstanding_tasks"][0]["collector"], "roblox")

    def test_supports_preliminary_scores_but_final_requires_complete_scores(self) -> None:
        preliminary = build_report(
            preliminary_result(),
            (final_scores()[0],),
            "preliminary",
        )
        self.assertEqual(preliminary["phase"], "preliminary")
        self.assertEqual(preliminary["candidates"][1]["component_scores"], None)
        self.assertIsNone(preliminary["candidates"][1]["action"])

        with self.assertRaises(InputValidationError):
            build_report(preliminary_result(), (final_scores()[0],), "final")

    def test_rejects_duplicate_unknown_or_cross_run_scores_and_invalid_phase(self) -> None:
        unknown = replace(final_scores()[0], opportunity_id=OPPORTUNITY_B)
        cases = (
            (final_scores() + (final_scores()[0],), "final"),
            ((replace(final_scores()[0], run_id=SECOND_RUN_ID),), "preliminary"),
            ((unknown, final_scores()[1]), "final"),
        )
        for scores, phase in cases:
            with self.subTest(phase=phase, count=len(scores)):
                with self.assertRaises(InputValidationError):
                    build_report(preliminary_result(), scores, phase)
        with self.assertRaises(InputValidationError):
            build_report(preliminary_result(), final_scores(), "scan")

    def test_markdown_is_a_deterministic_view_of_only_the_canonical_mapping(self) -> None:
        report = build_report(preliminary_result(), final_scores(), "final")
        changed = copy.deepcopy(report)
        changed["candidates"][0]["name"] = "Markdown Source"
        markdown = render_markdown(changed)

        self.assertIn("# Unified Game Opportunity Radar", markdown)
        self.assertIn("Markdown Source", markdown)
        self.assertIn("80.0", markdown)
        self.assertIn("24.0 / 26.0 / 14.0 / 16.0", markdown)
        self.assertIn("steam:123456", markdown)
        self.assertIn("2026-08-31T02:00:00Z", markdown)
        self.assertIn("partial_source", markdown)
        self.assertIn("candidate_note", markdown)
        self.assertIn("https://store.steampowered.com/app/123456/", markdown)
        self.assertLess(markdown.index("Markdown Source"), markdown.index("Pocket Workshop"))
        self.assertEqual(markdown, render_markdown(changed))

    def test_markdown_html_escapes_untrusted_text_and_does_not_autolink_urls(self) -> None:
        report = copy.deepcopy(
            build_report(preliminary_result(), final_scores(), "final")
        )
        unsafe_url = (
            "https://store.steampowered.com/app/123456/>\n"
            "<script>alert(1)</script>?left=1&right=2"
        )
        report["candidates"][0]["name"] = "Danger <b> & signal"
        report["candidates"][0]["platforms"][0]["url"] = unsafe_url
        report["candidates"][0]["evidence_urls"][0] = unsafe_url
        report["candidates"][0]["warnings"][0]["message"] = (
            "warning <img src=x> & more"
        )

        markdown = render_markdown(report)

        self.assertNotIn("<b>", markdown)
        self.assertNotIn("<script>", markdown)
        self.assertNotIn("<img", markdown)
        self.assertNotIn(f"<{unsafe_url}>", markdown)
        self.assertNotIn("\n<script>", markdown)
        self.assertIn("&lt;b&gt;", markdown)
        self.assertIn("&lt;script&gt;", markdown)
        self.assertIn("&lt;img", markdown)
        self.assertIn("&amp;", markdown)
        self.assertIn("&gt;", markdown)

    def test_canonical_renderer_maps_malformed_exact_schema_to_validation_error(self) -> None:
        report = build_report(preliminary_result(), final_scores(), "final")
        invalid_phase = copy.deepcopy(report)
        invalid_phase["phase"] = []
        unexpected_key = copy.deepcopy(report)
        unexpected_key["calculated_override"] = True
        for malformed in (invalid_phase, unexpected_key):
            with self.subTest(keys=tuple(malformed)):
                with self.assertRaises(InputValidationError):
                    render_markdown(malformed)

    def test_canonical_renderer_rejects_noncanonical_array_order(self) -> None:
        report = build_report(preliminary_result(), final_scores(), "final")
        reversed_health = copy.deepcopy(report)
        reversed_health["source_health"].reverse()

        reversed_platforms = copy.deepcopy(report)
        first = reversed_platforms["candidates"][0]
        first["platforms"].reverse()
        first["evidence_urls"].reverse()
        first["evidence_timestamps"].reverse()

        reversed_candidates = copy.deepcopy(report)
        reversed_candidates["candidates"].reverse()
        for rank, candidate in enumerate(reversed_candidates["candidates"], start=1):
            candidate["rank"] = rank

        for malformed in (
            reversed_health,
            reversed_platforms,
            reversed_candidates,
        ):
            with self.subTest(first_candidate=malformed["candidates"][0]["name"]):
                with self.assertRaises(InputValidationError):
                    render_markdown(malformed)

    def test_candidate_platforms_require_health_and_warning_ownership(self) -> None:
        result = preliminary_result()
        missing_roblox_health = replace(
            result,
            source_health=tuple(
                item for item in result.source_health if item.collector != "roblox"
            ),
        )
        with self.assertRaises(InputValidationError):
            build_report(missing_roblox_health, final_scores(), "final")

        report = build_report(result, final_scores(), "final")
        missing_provenance = copy.deepcopy(report)
        missing_provenance["source_health"] = [
            item
            for item in missing_provenance["source_health"]
            if item["collector"] != "roblox"
        ]
        missing_provenance["candidates"][0]["evidence_timestamps"] = [
            item
            for item in missing_provenance["candidates"][0]["evidence_timestamps"]
            if item["collector"] != "roblox"
        ]

        wrong_candidate_warning = copy.deepcopy(report)
        wrong_candidate_warning["candidates"][0]["warnings"][0][
            "opportunity_id"
        ] = OPPORTUNITY_B

        owned_run_warning = copy.deepcopy(report)
        owned_run_warning["warnings"][0]["opportunity_id"] = OPPORTUNITY_A

        for malformed in (
            missing_provenance,
            wrong_candidate_warning,
            owned_run_warning,
        ):
            with self.assertRaises(InputValidationError):
                render_markdown(malformed)


class RunArtifactTests(unittest.TestCase):
    def test_persists_exact_immutable_run_json_and_markdown_idempotently(self) -> None:
        report = build_report(preliminary_result(), final_scores(), "final")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = persist_run_artifacts(report, root, FileAtomicWriter())

            self.assertEqual(
                paths,
                (
                    root / f"{RUN_ID}.final.json",
                    root / f"{RUN_ID}.final.md",
                ),
            )
            self.assertEqual(paths[0].read_text(encoding="utf-8"), canonical_json(report))
            self.assertEqual(paths[1].read_text(encoding="utf-8"), render_markdown(report))
            before = tuple(path.stat().st_mtime_ns for path in paths)
            self.assertEqual(
                persist_run_artifacts(report, root, FileAtomicWriter()),
                paths,
            )
            self.assertEqual(tuple(path.stat().st_mtime_ns for path in paths), before)

            changed = copy.deepcopy(report)
            changed["candidates"][0]["name"] = "Conflicting Name"
            with self.assertRaises(ReportError):
                persist_run_artifacts(changed, root, FileAtomicWriter())

    def test_retry_repairs_failure_after_each_run_file_without_changing_content(self) -> None:
        report = build_report(preliminary_result(), final_scores(), "final")
        for fail_on in (1, 2):
            with self.subTest(fail_on=fail_on), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaises(ReportError):
                    persist_run_artifacts(report, root, FailAfterWriter(fail_on))

                paths = persist_run_artifacts(report, root, FileAtomicWriter())
                self.assertEqual(paths[0].read_text(encoding="utf-8"), canonical_json(report))
                self.assertEqual(paths[1].read_text(encoding="utf-8"), render_markdown(report))

    def test_preliminary_reports_use_content_addressed_snapshots_and_latest_view(self) -> None:
        first = build_report(preliminary_result(), (), "preliminary")
        changed_result = preliminary_result()
        changed_identity = replace(
            changed_result.candidates[0],
            name="Signal Garden Expanded",
            normalized_name="signal garden expanded",
        )
        changed_result = replace(
            changed_result,
            candidates=(changed_identity, changed_result.candidates[1]),
        )
        second = build_report(changed_result, (), "preliminary")
        completed_identity = replace(
            changed_result.candidates[0],
            name="Signal Garden Complete",
            normalized_name="signal garden complete",
        )
        completed_result = replace(
            changed_result,
            candidates=(completed_identity, changed_result.candidates[1]),
        )
        third = build_report(completed_result, (), "preliminary")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_paths = persist_run_artifacts(first, root, FileAtomicWriter())
            second_paths = persist_run_artifacts(second, root, FileAtomicWriter())
            third_paths = persist_run_artifacts(third, root, FileAtomicWriter())

            first_hash = hashlib.sha256(canonical_json(first).encode("utf-8")).hexdigest()
            second_hash = hashlib.sha256(canonical_json(second).encode("utf-8")).hexdigest()
            third_hash = hashlib.sha256(canonical_json(third).encode("utf-8")).hexdigest()
            self.assertNotEqual(first_hash, second_hash)
            self.assertEqual(
                first_paths,
                (
                    root / f"{RUN_ID}.preliminary.{first_hash}.json",
                    root / f"{RUN_ID}.preliminary.{first_hash}.md",
                ),
            )
            self.assertEqual(
                second_paths,
                (
                    root / f"{RUN_ID}.preliminary.{second_hash}.json",
                    root / f"{RUN_ID}.preliminary.{second_hash}.md",
                ),
            )
            self.assertRegex(first_paths[0].name, re.compile(r"[0-9a-f]{64}\.json\Z"))
            self.assertEqual(first_paths[0].read_text("utf-8"), canonical_json(first))
            self.assertEqual(second_paths[0].read_text("utf-8"), canonical_json(second))
            self.assertEqual(third_paths[0].read_text("utf-8"), canonical_json(third))
            self.assertEqual(
                (root / f"{RUN_ID}.preliminary.latest.json").read_text("utf-8"),
                canonical_json(third),
            )
            self.assertEqual(
                (root / f"{RUN_ID}.preliminary.latest.md").read_text("utf-8"),
                render_markdown(third),
            )
            revision_index = json.loads(
                (root / f"{RUN_ID}.preliminary.revisions.json").read_text("utf-8")
            )
            self.assertEqual(
                revision_index,
                {
                    "schema_version": 1,
                    "run_id": RUN_ID,
                    "current_revision": 3,
                    "current_sha256": third_hash,
                    "revisions": [
                        {"revision": 1, "sha256": first_hash},
                        {"revision": 2, "sha256": second_hash},
                        {"revision": 3, "sha256": third_hash},
                    ],
                },
            )
            self.assertNotIn("candidates", revision_index)

            first_paths[1].unlink()
            persist_run_artifacts(first, root, FileAtomicWriter())
            self.assertTrue(first_paths[1].is_file())
            self.assertEqual(
                (root / f"{RUN_ID}.preliminary.latest.json").read_text("utf-8"),
                canonical_json(third),
            )
            self.assertEqual(
                (root / f"{RUN_ID}.preliminary.latest.md").read_text("utf-8"),
                render_markdown(third),
            )

    def test_preliminary_retry_repairs_each_snapshot_and_latest_stage(self) -> None:
        report = build_report(preliminary_result(), (), "preliminary")
        for fail_on in (1, 2, 3, 4, 5):
            with self.subTest(fail_on=fail_on), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaises(ReportError):
                    persist_run_artifacts(report, root, FailAfterWriter(fail_on))

                paths = persist_run_artifacts(report, root, FileAtomicWriter())
                self.assertEqual(paths[0].read_text("utf-8"), canonical_json(report))
                self.assertEqual(paths[1].read_text("utf-8"), render_markdown(report))
                self.assertEqual(
                    (root / f"{RUN_ID}.preliminary.latest.json").read_text("utf-8"),
                    canonical_json(report),
                )
                self.assertEqual(
                    (root / f"{RUN_ID}.preliminary.latest.md").read_text("utf-8"),
                    render_markdown(report),
                )

    def test_old_preliminary_retry_repairs_the_current_mixed_latest_pair(self) -> None:
        first = build_report(preliminary_result(), (), "preliminary")
        changed_result = preliminary_result()
        changed_identity = replace(
            changed_result.candidates[0],
            name="Signal Garden Second",
            normalized_name="signal garden second",
        )
        second = build_report(
            replace(
                changed_result,
                candidates=(changed_identity, changed_result.candidates[1]),
            ),
            (),
            "preliminary",
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            persist_run_artifacts(first, root, FileAtomicWriter())
            with self.assertRaises(ReportError):
                persist_run_artifacts(second, root, FailAfterWriter(4))

            persist_run_artifacts(first, root, FileAtomicWriter())

            self.assertEqual(
                (root / f"{RUN_ID}.preliminary.latest.json").read_text("utf-8"),
                canonical_json(second),
            )
            self.assertEqual(
                (root / f"{RUN_ID}.preliminary.latest.md").read_text("utf-8"),
                render_markdown(second),
            )

    def test_file_writer_never_overwrites_a_concurrent_immutable_destination(
        self,
    ) -> None:
        report = build_report(preliminary_result(), final_scores(), "final")
        competitor = b"concurrent-immutable-owner"
        real_link = os.link

        def install_competitor_before_link(source, destination, **kwargs):
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=kwargs["dst_dir_fd"],
            )
            try:
                os.write(descriptor, competitor)
            finally:
                os.close(descriptor)
            return real_link(source, destination, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch(
                "unified_game_radar.report.os.link",
                side_effect=install_competitor_before_link,
            ), self.assertRaises(ReportError):
                persist_run_artifacts(report, root, FileAtomicWriter())

            self.assertEqual(
                (root / f"{RUN_ID}.final.json").read_bytes(),
                competitor,
            )

    def test_file_writer_poison_race_keeps_official_name_retryable(self) -> None:
        report = build_report(preliminary_result(), final_scores(), "final")
        real_link = os.link
        real_unlink = os.unlink

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "reports"
            outside = base / "outside.json"
            outside.write_text("outside-sentinel", "utf-8")

            def poison_temporary_before_link(source, destination, **kwargs):
                source_fd = kwargs["src_dir_fd"]
                real_unlink(source, dir_fd=source_fd)
                os.symlink(outside, source, dir_fd=source_fd)
                return real_link(source, destination, **kwargs)

            with patch(
                "unified_game_radar.report.os.link",
                side_effect=poison_temporary_before_link,
            ), self.assertRaises(ReportError):
                persist_run_artifacts(report, root, FileAtomicWriter())

            official = root / f"{RUN_ID}.final.json"
            self.assertFalse(os.path.lexists(official))
            self.assertEqual(outside.read_text("utf-8"), "outside-sentinel")

            paths = persist_run_artifacts(report, root, FileAtomicWriter())
            self.assertEqual(paths[0].read_text("utf-8"), canonical_json(report))

    def test_preliminary_lock_symlink_is_rejected_without_touching_target(self) -> None:
        report = build_report(preliminary_result(), (), "preliminary")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "reports"
            root.mkdir()
            outside = base / "outside.lock"
            outside.write_text("sentinel", "utf-8")
            lock = root / f".{RUN_ID}.preliminary.lock"
            lock.symlink_to(outside)

            with self.assertRaises(ReportError):
                persist_run_artifacts(report, root, FileAtomicWriter())

            self.assertEqual(outside.read_text("utf-8"), "sentinel")
            self.assertEqual(tuple(root.iterdir()), (lock,))

    def test_preliminary_lock_replacement_before_flock_is_rejected(self) -> None:
        report = build_report(preliminary_result(), (), "preliminary")
        real_flock = report_module.fcntl.flock
        swapped = False

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / f".{RUN_ID}.preliminary.lock"
            moved_lock = root / "moved-preliminary.lock"

            def swap_lock_then_flock(descriptor, operation):
                nonlocal swapped
                if operation == report_module.fcntl.LOCK_EX and not swapped:
                    lock.rename(moved_lock)
                    lock.write_text("replacement-lock", "utf-8")
                    swapped = True
                return real_flock(descriptor, operation)

            with patch(
                "unified_game_radar.report.fcntl.flock",
                side_effect=swap_lock_then_flock,
            ), self.assertRaises(ReportError):
                persist_run_artifacts(report, root, FileAtomicWriter())

            self.assertTrue(swapped)
            self.assertEqual(lock.read_text("utf-8"), "replacement-lock")
            self.assertFalse(any(root.glob(f"{RUN_ID}.preliminary.*.json")))

    def test_persist_refuses_symlinked_report_root(self) -> None:
        report = build_report(preliminary_result(), final_scores(), "final")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            report_link = root / "reports"
            report_link.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(ReportError):
                persist_run_artifacts(report, report_link, FileAtomicWriter())

            self.assertEqual(list(outside.iterdir()), [])

    def test_report_root_swap_cannot_write_through_outside_symlink(self) -> None:
        report = build_report(preliminary_result(), final_scores(), "final")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "reports"
            root.mkdir()
            moved = base / "moved-reports"
            outside = base / "outside"
            outside.mkdir()
            real_link = os.link
            swapped = False

            def attack_link(source, destination, *args, **kwargs):
                nonlocal swapped
                if not swapped:
                    root.rename(moved)
                    root.symlink_to(outside, target_is_directory=True)
                    (outside / Path(source).name).write_text("attacker", "utf-8")
                    swapped = True
                return real_link(source, destination, *args, **kwargs)

            with patch(
                "unified_game_radar.report.os.link",
                side_effect=attack_link,
            ):
                with self.assertRaises(ReportError):
                    persist_run_artifacts(report, root, FileAtomicWriter())

            self.assertFalse((outside / f"{RUN_ID}.final.json").exists())


class DailyPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.report_dir = root / "reports"
        self.store = RadarStore(root / "radar.sqlite3")
        self.store.initialize()
        self.config = replace(RadarConfig(), report_dir=self.report_dir)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def _prepare(
        self,
        run: RadarRun,
        *,
        phase: str = "final",
    ) -> tuple[dict[str, object], tuple[Path, Path]]:
        self.store.create_run(run)
        report = build_report(
            preliminary_result(run.run_id),
            final_scores(run.run_id),
            phase,
        )
        paths = persist_run_artifacts(report, self.report_dir, FileAtomicWriter())
        return report, paths

    def _publish(
        self,
        run: RadarRun,
        report: dict[str, object],
        paths: tuple[Path, Path],
        *,
        now: datetime | None = None,
        writer: object | None = None,
    ):
        return publish_daily_if_allowed(
            self.config,
            self.store,
            run,
            report,
            paths,
            now or datetime(2026, 8, 31, 8, 10, tzinfo=timezone.utc),
            writer or FileAtomicWriter(),
        )

    def test_scheduled_10_is_collection_only_and_preliminary_never_advances(self) -> None:
        ten = radar_run()
        report, paths = self._prepare(ten)
        publication = self._publish(ten, report, paths)
        self.assertFalse(publication.advances_daily_latest)
        self.assertFalse((self.report_dir / "daily").exists())
        self.assertEqual(self.store.get_publication(RUN_ID, "final"), publication)

        preliminary_run = radar_run(
            run_id="20260831T080000Z-b1c2d3e4",
            started_at=datetime(2026, 8, 31, 8, tzinfo=timezone.utc),
        )
        preliminary_report, preliminary_paths = self._prepare(
            preliminary_run,
            phase="preliminary",
        )
        preliminary_publication = self._publish(
            preliminary_run,
            preliminary_report,
            preliminary_paths,
        )
        self.assertFalse(preliminary_publication.advances_daily_latest)

    def test_scheduled_16_publishes_dated_and_latest_pairs(self) -> None:
        run = radar_run(
            run_id="20260831T080000Z-b1c2d3e4",
            started_at=datetime(2026, 8, 31, 8, tzinfo=timezone.utc),
        )
        report, paths = self._prepare(run)
        publication = self._publish(run, report, paths)

        daily = self.report_dir / "daily"
        self.assertTrue(publication.advances_daily_latest)
        self.assertEqual(publication.daily_date, date(2026, 8, 31))
        self.assertEqual(Path(publication.report_json), paths[0])
        self.assertEqual(Path(publication.report_markdown), paths[1])
        for path in (
            daily / "2026-08-31.json",
            daily / "2026-08-31.md",
            daily / "latest.json",
            daily / "latest.md",
        ):
            self.assertTrue(path.is_file())
        self.assertEqual((daily / "latest.json").read_text(), canonical_json(report))
        self.assertEqual((daily / "latest.md").read_text(), render_markdown(report))
        self.assertEqual(self.store.get_publication(run.run_id, "final"), publication)

    def test_same_date_publications_keep_immutable_audit_paths(self) -> None:
        scheduled = radar_run(
            run_id="20260831T080000Z-c1d2e3f4",
            started_at=datetime(2026, 8, 31, 8, tzinfo=timezone.utc),
        )
        first_report, first_paths = self._prepare(scheduled)
        first = self._publish(scheduled, first_report, first_paths)

        manual = radar_run(
            run_id="20260831T090000Z-d1e2f3a4",
            started_at=datetime(2026, 8, 31, 9, tzinfo=timezone.utc),
            mode="manual",
            publish_daily=True,
        )
        second_report, second_paths = self._prepare(manual)
        second = self._publish(
            manual,
            second_report,
            second_paths,
            now=datetime(2026, 8, 31, 9, 5, tzinfo=timezone.utc),
        )

        self.assertEqual(Path(first.report_json), first_paths[0])
        self.assertEqual(Path(first.report_markdown), first_paths[1])
        self.assertEqual(Path(second.report_json), second_paths[0])
        self.assertEqual(Path(second.report_markdown), second_paths[1])
        self.assertEqual(Path(first.report_json).read_text("utf-8"), canonical_json(first_report))
        self.assertEqual(Path(second.report_json).read_text("utf-8"), canonical_json(second_report))
        self.assertEqual(
            (self.report_dir / "daily/2026-08-31.json").read_text("utf-8"),
            canonical_json(second_report),
        )

    def test_manual_requires_explicit_publish_daily_and_uses_configured_timezone_date(self) -> None:
        manual = radar_run(mode="manual")
        report, paths = self._prepare(manual)
        self.assertFalse(self._publish(manual, report, paths).advances_daily_latest)

        explicit = radar_run(
            run_id="20260831T010000Z-b1c2d3e4",
            started_at=datetime(2026, 8, 31, 1, tzinfo=timezone.utc),
            mode="manual",
            publish_daily=True,
        )
        explicit_report, explicit_paths = self._prepare(explicit)
        self.config = replace(self.config, timezone="America/Los_Angeles")
        publication = self._publish(
            explicit,
            explicit_report,
            explicit_paths,
            now=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        self.assertTrue(publication.advances_daily_latest)
        self.assertEqual(publication.daily_date, date(2026, 8, 30))
        self.assertTrue(
            (self.report_dir / "daily" / "2026-08-30.json").is_file()
        )

    def test_publication_rejects_run_timestamp_that_disagrees_with_started_at(self) -> None:
        inconsistent = radar_run(
            started_at=datetime(2026, 8, 31, 8, tzinfo=timezone.utc),
        )
        report, paths = self._prepare(inconsistent)
        with self.assertRaises(InputValidationError):
            self._publish(inconsistent, report, paths)

    def test_older_scheduled_date_never_replaces_daily_latest(self) -> None:
        newer = radar_run(
            run_id=SECOND_RUN_ID,
            started_at=datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
        )
        newer_report, newer_paths = self._prepare(newer)
        newer_publication = self._publish(
            newer,
            newer_report,
            newer_paths,
            now=datetime(2026, 9, 1, 8, 5, tzinfo=timezone.utc),
        )
        self.assertTrue(newer_publication.advances_daily_latest)

        older = radar_run(
            run_id="20260831T080000Z-c1d2e3f4",
            started_at=datetime(2026, 8, 31, 8, tzinfo=timezone.utc),
        )
        older_report, older_paths = self._prepare(older)
        older_publication = self._publish(
            older,
            older_report,
            older_paths,
            now=datetime(2026, 9, 1, 9, tzinfo=timezone.utc),
        )

        self.assertFalse(older_publication.advances_daily_latest)
        latest = self.report_dir / "daily" / "latest.json"
        self.assertEqual(latest.read_text(encoding="utf-8"), canonical_json(newer_report))
        self.assertTrue((self.report_dir / "daily" / "2026-08-31.json").is_file())

    def test_historical_publication_retry_returns_original_after_latest_advances(self) -> None:
        older = radar_run(
            run_id="20260831T080000Z-c1d2e3f4",
            started_at=datetime(2026, 8, 31, 8, tzinfo=timezone.utc),
        )
        older_report, older_paths = self._prepare(older)
        original = self._publish(
            older,
            older_report,
            older_paths,
            now=datetime(2026, 8, 31, 8, 5, tzinfo=timezone.utc),
        )

        newer = radar_run(
            run_id=SECOND_RUN_ID,
            started_at=datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
        )
        newer_report, newer_paths = self._prepare(newer)
        self._publish(
            newer,
            newer_report,
            newer_paths,
            now=datetime(2026, 9, 1, 8, 5, tzinfo=timezone.utc),
        )
        latest_before = (self.report_dir / "daily/latest.json").read_text("utf-8")

        retried = self._publish(
            older,
            older_report,
            older_paths,
            now=datetime(2026, 9, 2, 8, 5, tzinfo=timezone.utc),
        )

        self.assertEqual(retried, original)
        self.assertEqual(
            (self.report_dir / "daily/latest.json").read_text("utf-8"),
            latest_before,
        )

    def test_concurrent_daily_publications_cannot_regress_latest(self) -> None:
        older = radar_run(
            run_id="20260831T080000Z-c1d2e3f4",
            started_at=datetime(2026, 8, 31, 8, tzinfo=timezone.utc),
        )
        newer = radar_run(
            run_id=SECOND_RUN_ID,
            started_at=datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
        )
        older_report, older_paths = self._prepare(older)
        newer_report, newer_paths = self._prepare(newer)
        database_path = self.report_dir.parent / "radar.sqlite3"
        older_paused = threading.Event()
        release_older = threading.Event()
        newer_entered_writer = threading.Event()
        newer_done = threading.Event()
        errors: list[BaseException] = []

        class PausingLatestWriter:
            def __init__(self) -> None:
                self.delegate = FileAtomicWriter()
                self.paused = False

            def write_text_at(
                self,
                directory_fd: int,
                filename: str,
                content: str,
            ) -> None:
                if filename == "latest.json" and not self.paused:
                    self.paused = True
                    older_paused.set()
                    if not release_older.wait(timeout=5):
                        raise RuntimeError("timed out waiting to resume older write")
                self.delegate.write_text_at(directory_fd, filename, content)

        class NotifyingWriter:
            def __init__(self) -> None:
                self.delegate = FileAtomicWriter()

            def write_text_at(
                self,
                directory_fd: int,
                filename: str,
                content: str,
            ) -> None:
                newer_entered_writer.set()
                self.delegate.write_text_at(directory_fd, filename, content)

        def publish_in_thread(
            run: RadarRun,
            report: dict[str, object],
            paths: tuple[Path, Path],
            writer: object,
            done: threading.Event | None = None,
        ) -> None:
            store = RadarStore(database_path)
            store.initialize()
            try:
                publish_daily_if_allowed(
                    self.config,
                    store,
                    run,
                    report,
                    paths,
                    run.started_at,
                    writer,
                )
            except BaseException as error:
                errors.append(error)
            finally:
                store.close()
                if done is not None:
                    done.set()

        older_thread = threading.Thread(
            target=publish_in_thread,
            args=(older, older_report, older_paths, PausingLatestWriter()),
        )
        newer_thread = threading.Thread(
            target=publish_in_thread,
            args=(
                newer,
                newer_report,
                newer_paths,
                NotifyingWriter(),
                newer_done,
            ),
        )
        older_thread.start()
        self.assertTrue(older_paused.wait(timeout=5))
        newer_thread.start()
        if newer_entered_writer.wait(timeout=1):
            self.assertTrue(newer_done.wait(timeout=5))
        release_older.set()
        older_thread.join(timeout=5)
        newer_thread.join(timeout=5)

        self.assertFalse(older_thread.is_alive())
        self.assertFalse(newer_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            (self.report_dir / "daily/latest.json").read_text("utf-8"),
            canonical_json(newer_report),
        )
        self.assertTrue(
            self.store.get_publication(newer.run_id, "final").advances_daily_latest
        )

    def test_daily_publication_refuses_symlinked_daily_parent(self) -> None:
        run = radar_run(
            run_id="20260906T080000Z-a1b2c3d6",
            started_at=datetime(2026, 9, 6, 8, tzinfo=timezone.utc),
        )
        report, paths = self._prepare(run)
        outside = self.report_dir.parent / "outside-daily"
        outside.mkdir()
        (self.report_dir / "daily").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(ReportError):
            self._publish(run, report, paths)

        self.assertEqual(list(outside.iterdir()), [])
        self.assertIsNone(self.store.get_publication(run.run_id, "final"))

    def test_daily_parent_swap_cannot_write_through_outside_symlink(self) -> None:
        run = radar_run(
            run_id="20260908T080000Z-a1b2c3d8",
            started_at=datetime(2026, 9, 8, 8, tzinfo=timezone.utc),
        )
        report, paths = self._prepare(run)
        daily = self.report_dir / "daily"
        daily.mkdir()
        moved = self.report_dir / "moved-daily"
        outside = self.report_dir.parent / "outside-daily-swap"
        outside.mkdir()
        real_replace = os.replace
        swapped = False

        def attack_replace(source, destination, *args, **kwargs):
            nonlocal swapped
            if not swapped:
                daily.rename(moved)
                daily.symlink_to(outside, target_is_directory=True)
                (outside / Path(source).name).write_text("attacker", "utf-8")
                swapped = True
            return real_replace(source, destination, *args, **kwargs)

        with patch(
            "unified_game_radar.report.os.replace",
            side_effect=attack_replace,
        ):
            with self.assertRaises(ReportError):
                self._publish(run, report, paths)

        self.assertFalse((outside / "2026-09-08.json").exists())
        self.assertIsNone(self.store.get_publication(run.run_id, "final"))

    def test_daily_parent_swap_after_last_write_prevents_publication(self) -> None:
        run = radar_run(
            run_id="20260909T080000Z-a1b2c3d9",
            started_at=datetime(2026, 9, 9, 8, tzinfo=timezone.utc),
        )
        report, paths = self._prepare(run)
        daily = self.report_dir / "daily"
        moved = self.report_dir / "moved-daily-after-write"
        outside = self.report_dir.parent / "outside-daily-after-write"
        outside.mkdir()
        real_replace = os.replace
        replacements = 0

        def attack_after_replace(source, destination, *args, **kwargs):
            nonlocal replacements
            result = real_replace(source, destination, *args, **kwargs)
            replacements += 1
            if replacements == 4:
                daily.rename(moved)
                daily.symlink_to(outside, target_is_directory=True)
            return result

        with patch(
            "unified_game_radar.report.os.replace",
            side_effect=attack_after_replace,
        ):
            with self.assertRaises(ReportError):
                self._publish(run, report, paths)

        self.assertEqual(list(outside.iterdir()), [])
        self.assertIsNone(self.store.get_publication(run.run_id, "final"))

    def test_report_root_swap_before_database_record_rolls_back_publication(self) -> None:
        run = radar_run(
            run_id="20260910T080000Z-a1b2c3da",
            started_at=datetime(2026, 9, 10, 8, tzinfo=timezone.utc),
        )
        report, paths = self._prepare(run)
        moved = self.report_dir.with_name("moved-reports-before-db")
        outside = self.report_dir.parent / "outside-before-db"
        outside.mkdir()
        real_record = report_module._record_publication

        def swap_root_then_record(store, publication):
            self.report_dir.rename(moved)
            self.report_dir.symlink_to(outside, target_is_directory=True)
            return real_record(store, publication)

        with patch(
            "unified_game_radar.report._record_publication",
            side_effect=swap_root_then_record,
        ), self.assertRaises(ReportError):
            self._publish(run, report, paths)

        self.assertIsNone(self.store.get_publication(run.run_id, "final"))
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((moved / paths[0].name).is_file())

    def test_publication_reads_are_confined_when_report_root_is_replaced_by_symlink(
        self,
    ) -> None:
        run = radar_run(
            run_id="20260907T080000Z-a1b2c3d7",
            started_at=datetime(2026, 9, 7, 8, tzinfo=timezone.utc),
        )
        report, paths = self._prepare(run)
        moved = self.report_dir.with_name("moved-reports")
        self.report_dir.rename(moved)
        self.report_dir.symlink_to(moved, target_is_directory=True)

        with self.assertRaises(ReportError):
            self._publish(run, report, paths)

        self.assertFalse((moved / "daily").exists())
        self.assertIsNone(self.store.get_publication(run.run_id, "final"))

    def test_retry_repairs_each_partial_daily_stage_without_false_publication(self) -> None:
        for fail_on in (1, 2, 3, 4):
            with self.subTest(fail_on=fail_on):
                run_id = f"2026090{fail_on}T080000Z-a1b2c3d{fail_on}"
                run = radar_run(
                    run_id=run_id,
                    started_at=datetime(2026, 9, fail_on, 8, tzinfo=timezone.utc),
                )
                report, paths = self._prepare(run)
                with self.assertRaises(ReportError):
                    self._publish(
                        run,
                        report,
                        paths,
                        now=datetime(2026, 9, fail_on, 8, 5, tzinfo=timezone.utc),
                        writer=FailAfterWriter(fail_on),
                    )
                self.assertIsNone(self.store.get_publication(run_id, "final"))

                publication = self._publish(
                    run,
                    report,
                    paths,
                    now=datetime(2026, 9, fail_on, 8, 6, tzinfo=timezone.utc),
                )
                self.assertTrue(publication.advances_daily_latest)
                self.assertEqual(
                    self._publish(
                        run,
                        report,
                        paths,
                        now=datetime(2026, 9, fail_on, 8, 6, tzinfo=timezone.utc),
                    ),
                    publication,
                )

    def test_database_failure_after_insert_rolls_back_and_retry_is_idempotent(self) -> None:
        run = radar_run(
            run_id="20260905T080000Z-a1b2c3d5",
            started_at=datetime(2026, 9, 5, 8, tzinfo=timezone.utc),
        )
        report, paths = self._prepare(run)
        original_publish = self.store.publish

        def fail_after_insert(publication) -> None:
            original_publish(publication)
            raise RuntimeError("failure after database publication")

        with patch.object(self.store, "publish", side_effect=fail_after_insert):
            with self.assertRaises(RuntimeError):
                self._publish(
                    run,
                    report,
                    paths,
                    now=datetime(2026, 9, 5, 8, 5, tzinfo=timezone.utc),
                )
        self.assertIsNone(self.store.get_publication(run.run_id, "final"))

        publication = self._publish(
            run,
            report,
            paths,
            now=datetime(2026, 9, 5, 8, 6, tzinfo=timezone.utc),
        )
        self.assertEqual(self.store.get_publication(run.run_id, "final"), publication)


if __name__ == "__main__":
    unittest.main()
