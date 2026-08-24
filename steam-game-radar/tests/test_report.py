from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
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

from steam_game_radar.config import RadarConfig
from steam_game_radar.enrichment import Evidence
from steam_game_radar.errors import InputValidationError, PersistenceError
import steam_game_radar.report as report_module
from steam_game_radar.report import (
    build_report,
    persist_report,
    render_markdown,
    should_publish_latest,
)
from steam_game_radar.run_lock import RunLock
from steam_game_radar.schemas import (
    GameRecord,
    MAX_STEAM_APPID,
    MetricObservation,
    RejectedRow,
    WarningRecord,
)
from steam_game_radar.score import ScoredCandidate, candidate_sort_key


UTC = timezone.utc
GENERATED_AT = "2026-08-24T03:00:30Z"
RUN_A = "20260824T030000Z-a1b2c3d4"
RUN_B = "20260824T040000Z-b1c2d3e4"
RUN_OLD = "20260824T020000Z-01020304"
SAFE_TEMP_DIR = str(Path(tempfile.gettempdir()).resolve())


class ReportTests(unittest.TestCase):
    def observation(
        self,
        value: object,
        source_id: str = "steam-current-players",
        *,
        source_kind: str = "steam_official",
        observed_at: str = "2026-08-24T03:00:00Z",
    ) -> MetricObservation:
        return MetricObservation(
            value=value,
            source_id=source_id,
            source_kind=source_kind,  # type: ignore[arg-type]
            observed_at=observed_at,
        )

    def candidate(
        self,
        *,
        appid: int = 10,
        name: str = "Example Game",
        release_status: str = "released",
        score: float = 80.0,
        confidence: str = "C",
        scale: int = 1_000,
        final: bool = False,
        source_kind: str = "steam_official",
    ) -> ScoredCandidate:
        metric_name = (
            "current_players"
            if release_status == "released"
            else "wishlist_gain_7d"
        )
        record = GameRecord(
            schema_version=1,
            appid=appid,
            name=name,
            release_status=release_status,  # type: ignore[arg-type]
            store_url=f"https://store.steampowered.com/app/{appid}/",
            metrics={
                metric_name: self.observation(
                    scale,
                    f"source-{appid}",
                    source_kind=source_kind,
                )
            },
            source_extra={"ignored_by_report": ["provider-private"]},
        )
        warnings = (WarningRecord("candidate_warning", "Needs review", appid),)
        if final:
            return ScoredCandidate(
                record=record,
                deltas={f"{metric_name}_7d_percent": 12.34},
                metric_scores={"momentum": score},
                steam_heat_score=score,
                seo_opportunity_score=score,
                final_score=score,
                action="immediate_action" if score >= 80 else "watch",
                confidence=confidence,  # type: ignore[arg-type]
                warnings=warnings,
                evidence=(
                    Evidence("google", f"https://google.example/search?q={appid}"),
                    Evidence("youtube", f"https://youtube.example/results?q={appid}"),
                ),
                recommended_content_types=("wiki_or_guide", "video"),
            )
        return ScoredCandidate(
            record=record,
            deltas={f"{metric_name}_7d_percent": 12.34},
            metric_scores={"momentum": score},
            steam_heat_score=score,
            seo_opportunity_score=None,
            final_score=None,
            action="needs_seo_enrichment",
            confidence="C",
            warnings=warnings,
            evidence=(),
            recommended_content_types=(),
        )

    def report(
        self,
        run_id: str = RUN_A,
        *,
        phase: str = "preliminary",
        released: tuple[ScoredCandidate, ...] | None = None,
        unreleased: tuple[ScoredCandidate, ...] = (),
        generated_at: str = GENERATED_AT,
    ) -> dict[str, object]:
        return build_report(
            run_id=run_id,
            phase=phase,  # type: ignore[arg-type]
            mode="official_scan" if phase == "preliminary" else "official_plus_manual",
            generated_at=generated_at,
            data_status="fresh",
            released=released or (self.candidate(),),
            unreleased=unreleased,
            newly_observed=(10, 10),
            warnings=(WarningRecord("run_warning", "Run level"),),
            rejected_rows=(RejectedRow(2, "bad_row", "Rejected", 99),),
        )

    def lock(self, root: Path, run_id: str) -> RunLock:
        return RunLock(
            path=root / "state" / ".run.lock",
            run_id=run_id,
            now=lambda: datetime(2026, 8, 24, 3, tzinfo=UTC),
            hostname=lambda: "report-worker",
            pid_alive=lambda _pid: False,
        )

    def test_build_report_has_exact_top_level_schema_literals_and_isolated_inputs(self) -> None:
        released = [self.candidate()]
        warnings = [WarningRecord("z_warning", "Last"), WarningRecord("a_warning", "First")]
        rejected = [RejectedRow(9, "late", "Late"), RejectedRow(2, "early", "Early", 10)]

        result = build_report(
            RUN_A,
            "preliminary",
            "official_scan",
            GENERATED_AT,
            "fresh",
            released,
            [],
            [10, 10],
            warnings,
            rejected,
        )

        self.assertEqual(
            list(result),
            [
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
            ],
        )
        self.assertEqual(result["report_schema_version"], 1)
        self.assertEqual(result["run_id"], RUN_A)
        self.assertEqual(result["phase"], "preliminary")
        self.assertEqual(result["mode"], "official_scan")
        self.assertEqual(result["generated_at"], GENERATED_AT)
        self.assertEqual(result["data_status"], "fresh")
        self.assertEqual(len(result["released"]), 1)
        self.assertEqual(result["unreleased"], [])
        released.clear()
        warnings.clear()
        rejected.clear()
        self.assertEqual(len(result["released"]), 1)
        self.assertEqual(len(result["warnings"]), 2)
        self.assertEqual(len(result["rejected_rows"]), 2)

        invalid_cases = (
            {"run_id": "not-a-run"},
            {"phase": "draft"},
            {"mode": "steamdb_scrape"},
            {"generated_at": "2026-08-24T11:00:30+08:00"},
            {"data_status": "expired"},
        )
        for overrides in invalid_cases:
            values = {
                "run_id": RUN_A,
                "phase": "preliminary",
                "mode": "official_scan",
                "generated_at": GENERATED_AT,
                "data_status": "fresh",
            }
            values.update(overrides)
            with self.subTest(overrides=overrides), self.assertRaises(InputValidationError):
                build_report(
                    values["run_id"],  # type: ignore[arg-type]
                    values["phase"],  # type: ignore[arg-type]
                    values["mode"],  # type: ignore[arg-type]
                    values["generated_at"],  # type: ignore[arg-type]
                    values["data_status"],  # type: ignore[arg-type]
                    [],
                    [],
                    [],
                    [],
                    [],
                )

    def test_candidate_schema_preserves_observations_provenance_scores_evidence_and_content(self) -> None:
        candidate = self.candidate(final=True, confidence="B")

        result = self.report(phase="final", released=(candidate,))
        item = result["released"][0]

        self.assertEqual(
            set(item),
            {
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
            },
        )
        self.assertEqual(item["appid"], 10)
        self.assertEqual(item["name"], "Example Game")
        self.assertEqual(item["release_status"], "released")
        self.assertEqual(item["store_url"], "https://store.steampowered.com/app/10/")
        self.assertEqual(
            item["observed_metrics"]["current_players"],
            {
                "value": 1_000,
                "source_id": "source-10",
                "source_kind": "steam_official",
                "observed_at": "2026-08-24T03:00:00Z",
            },
        )
        self.assertEqual(item["deltas"], {"current_players_7d_percent": 12.3})
        self.assertEqual(item["metric_scores"], {"momentum": 80.0})
        self.assertEqual(item["steam_heat_score"], 80.0)
        self.assertEqual(item["seo_opportunity_score"], 80.0)
        self.assertEqual(item["final_score"], 80.0)
        self.assertEqual(item["action"], "immediate_action")
        self.assertEqual(item["confidence"], "B")
        self.assertEqual(item["warnings"], [{"code": "candidate_warning", "message": "Needs review", "appid": 10}])
        self.assertEqual(
            item["evidence"],
            [
                {"source": "google", "url": "https://google.example/search?q=10"},
                {"source": "youtube", "url": "https://youtube.example/results?q=10"},
            ],
        )
        self.assertEqual(item["recommended_content_types"], ["wiki_or_guide", "video"])
        self.assertNotIn("source_extra", item)
        self.assertEqual(candidate.deltas["current_players_7d_percent"], 12.34)

    def test_newly_observed_warnings_and_rejections_are_canonical(self) -> None:
        result = build_report(
            RUN_A,
            "preliminary",
            "manual_baseline",
            GENERATED_AT,
            "manual_only",
            [],
            [],
            [MAX_STEAM_APPID, 3, 3, 2],
            [
                WarningRecord("z", "Z", 10),
                WarningRecord("a", "A"),
                WarningRecord("a", "B", 2),
            ],
            [
                RejectedRow(9, "z", "Z"),
                RejectedRow(2, "b", "B", 10),
                RejectedRow(2, "a", "A"),
            ],
        )

        self.assertEqual(result["newly_observed"], [2, 3, MAX_STEAM_APPID])
        self.assertEqual(
            result["warnings"],
            [
                {"code": "a", "message": "A"},
                {"code": "a", "message": "B", "appid": 2},
                {"code": "z", "message": "Z", "appid": 10},
            ],
        )
        self.assertEqual(
            result["rejected_rows"],
            [
                {"row_number": 2, "code": "a", "message": "A"},
                {"row_number": 2, "code": "b", "message": "B", "appid": 10},
                {"row_number": 9, "code": "z", "message": "Z"},
            ],
        )
        for invalid in (0, -1, True, "10", MAX_STEAM_APPID + 1):
            with self.subTest(invalid=invalid), self.assertRaises(InputValidationError):
                build_report(
                    RUN_A,
                    "preliminary",
                    "official_scan",
                    GENERATED_AT,
                    "fresh",
                    [],
                    [],
                    [invalid],  # type: ignore[list-item]
                    [],
                    [],
                )

    def test_candidate_sorting_is_stable_and_release_pools_are_separate(self) -> None:
        released = (
            self.candidate(appid=4, name="Zulu", score=80, scale=100),
            self.candidate(appid=3, name="alpha", score=80, scale=100),
            self.candidate(appid=2, name="Alpha", score=90, scale=1),
            self.candidate(appid=1, name="alpha", score=80, scale=100),
        )
        unreleased = (
            self.candidate(appid=8, name="Later", release_status="unreleased", score=70, scale=20),
            self.candidate(appid=7, name="Soon", release_status="unreleased", score=70, scale=50),
        )

        result = build_report(
            RUN_A,
            "preliminary",
            "official_scan",
            GENERATED_AT,
            "fresh",
            released,
            unreleased,
            [],
            [],
            [],
        )

        self.assertEqual(
            [item["appid"] for item in result["released"]],
            [candidate.record.appid for candidate in sorted(released, key=candidate_sort_key)],
        )
        self.assertEqual([item["appid"] for item in result["unreleased"]], [7, 8])
        with self.assertRaises(InputValidationError):
            build_report(
                RUN_A,
                "preliminary",
                "official_scan",
                GENERATED_AT,
                "fresh",
                [unreleased[0]],
                [],
                [],
                [],
                [],
            )

    def test_markdown_uses_canonical_json_order_and_values_without_rescoring(self) -> None:
        result = build_report(
            RUN_A,
            "preliminary",
            "official_scan",
            GENERATED_AT,
            "stale",
            (
                self.candidate(appid=1, name="First & Best", score=90),
                self.candidate(appid=2, name="Second", score=80),
            ),
            (self.candidate(appid=3, name="Future", release_status="unreleased", score=70),),
            [3],
            [WarningRecord("stale", "Using fallback")],
            [],
        )

        with mock.patch.object(
            report_module,
            "candidate_sort_key",
            side_effect=AssertionError("Markdown must not sort"),
        ):
            markdown = render_markdown(result)

        self.assertLess(markdown.index("First & Best"), markdown.index("Second"))
        self.assertLess(markdown.index("Second"), markdown.index("Future"))
        self.assertIn("90.0", markdown)
        self.assertIn("source-1", markdown)
        self.assertIn("steam_official", markdown)
        self.assertIn("2026-08-24T03:00:00Z", markdown)
        self.assertIn("Using fallback", markdown)

    def test_should_publish_latest_is_monotonic_and_strict(self) -> None:
        preliminary = self.report(RUN_A)
        final = self.report(RUN_A, phase="final", released=(self.candidate(final=True, confidence="B"),))
        newer = self.report(RUN_B, generated_at="2026-08-24T04:00:30Z")
        older = self.report(RUN_OLD, phase="final", generated_at="2026-08-24T02:00:30Z", released=(self.candidate(final=True, confidence="B"),))

        self.assertTrue(should_publish_latest(preliminary, None))
        self.assertTrue(should_publish_latest(final, preliminary))
        self.assertTrue(should_publish_latest(newer, final))
        self.assertFalse(should_publish_latest(older, newer))
        self.assertFalse(should_publish_latest(preliminary, final))
        self.assertFalse(should_publish_latest(preliminary, preliminary))
        same_timestamp_other_run = self.report("20260824T030000Z-ffffffff")
        self.assertFalse(should_publish_latest(same_timestamp_other_run, preliminary))
        invalid = dict(preliminary)
        invalid["unexpected"] = True
        with self.assertRaises(InputValidationError):
            should_publish_latest(invalid, preliminary)
        with self.assertRaises(InputValidationError):
            should_publish_latest(preliminary, invalid)

    def test_persist_report_writes_immutable_pair_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            config = RadarConfig(report_dir=root / "reports" / "steam-game-radar")
            report = self.report()
            lock = self.lock(root, RUN_A)

            with lock:
                json_path, markdown_path = persist_report(config, report, lock)
                original_json = json_path.read_bytes()
                original_markdown = markdown_path.read_bytes()
                self.assertEqual(json_path.name, f"{RUN_A}.preliminary.json")
                self.assertEqual(markdown_path.name, f"{RUN_A}.preliminary.md")
                self.assertEqual(json.loads(original_json), report)
                self.assertEqual(original_markdown.decode("utf-8"), render_markdown(report))
                self.assertEqual(stat.S_IMODE(json_path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(markdown_path.stat().st_mode), 0o600)
                with self.assertRaises(PersistenceError):
                    persist_report(config, report, lock)

            self.assertEqual(json_path.read_bytes(), original_json)
            self.assertEqual(markdown_path.read_bytes(), original_markdown)

    def test_same_run_preliminary_to_final_advances_latest_pair(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            config = RadarConfig(report_dir=root / "reports")
            preliminary = self.report()
            final = self.report(phase="final", released=(self.candidate(final=True, confidence="B"),))

            with self.lock(root, RUN_A) as lock:
                persist_report(config, preliminary, lock)
                persist_report(config, final, lock)

            latest_json = config.report_dir / "latest.json"
            latest_markdown = config.report_dir / "latest.md"
            stored = json.loads(latest_json.read_text(encoding="utf-8"))
            self.assertEqual(stored["run_id"], RUN_A)
            self.assertEqual(stored["phase"], "final")
            self.assertEqual(latest_markdown.read_text(encoding="utf-8"), render_markdown(stored))
            self.assertTrue((config.report_dir / f"{RUN_A}.preliminary.json").exists())
            self.assertTrue((config.report_dir / f"{RUN_A}.preliminary.md").exists())
            self.assertTrue((config.report_dir / f"{RUN_A}.final.json").exists())
            self.assertTrue((config.report_dir / f"{RUN_A}.final.md").exists())

    def test_newer_run_advances_latest_and_older_delayed_final_only_writes_immutable(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            config = RadarConfig(report_dir=root / "reports")
            initial = self.report(RUN_A)
            newer = self.report(RUN_B, generated_at="2026-08-24T04:00:30Z")
            delayed = self.report(
                RUN_OLD,
                phase="final",
                generated_at="2026-08-24T05:00:30Z",
                released=(self.candidate(final=True, confidence="B"),),
            )

            for run_id, candidate in ((RUN_A, initial), (RUN_B, newer), (RUN_OLD, delayed)):
                with self.lock(root, run_id) as lock:
                    persist_report(config, candidate, lock)

            latest = json.loads((config.report_dir / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["run_id"], RUN_B)
            self.assertEqual(
                (config.report_dir / "latest.md").read_text(encoding="utf-8"),
                render_markdown(latest),
            )
            self.assertTrue((config.report_dir / f"{RUN_OLD}.final.json").exists())
            self.assertTrue((config.report_dir / f"{RUN_OLD}.final.md").exists())

    def test_latest_pair_failure_restores_previous_pair_and_keeps_immutable_pair(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            config = RadarConfig(report_dir=root / "reports")
            first = self.report(RUN_A)
            second = self.report(RUN_B, generated_at="2026-08-24T04:00:30Z")
            with self.lock(root, RUN_A) as lock:
                persist_report(config, first, lock)
            old_json = (config.report_dir / "latest.json").read_bytes()
            old_markdown = (config.report_dir / "latest.md").read_bytes()
            real_replace = report_module._replace_staged_latest

            def fail_markdown(
                directory_descriptor: int,
                source_name: str,
                destination_name: str,
            ) -> None:
                if destination_name == "latest.md":
                    raise OSError("simulated second latest failure")
                real_replace(directory_descriptor, source_name, destination_name)

            with self.lock(root, RUN_B) as lock, mock.patch.object(
                report_module,
                "_replace_staged_latest",
                side_effect=fail_markdown,
            ), self.assertRaises(PersistenceError):
                persist_report(config, second, lock)

            self.assertEqual((config.report_dir / "latest.json").read_bytes(), old_json)
            self.assertEqual((config.report_dir / "latest.md").read_bytes(), old_markdown)
            self.assertTrue((config.report_dir / f"{RUN_B}.preliminary.json").exists())
            self.assertTrue((config.report_dir / f"{RUN_B}.preliminary.md").exists())

    def test_immutable_pair_failure_rolls_back_only_new_first_file(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            config = RadarConfig(report_dir=root / "reports")
            report = self.report()
            real_publish = report_module._publish_no_replace

            def fail_markdown(
                directory_descriptor: int,
                source_name: str,
                destination_name: str,
            ) -> None:
                if destination_name.endswith(".md"):
                    raise OSError("simulated immutable markdown failure")
                real_publish(directory_descriptor, source_name, destination_name)

            with self.lock(root, RUN_A) as lock, mock.patch.object(
                report_module,
                "_publish_no_replace",
                side_effect=fail_markdown,
            ), self.assertRaises(PersistenceError):
                persist_report(config, report, lock)

            self.assertFalse((config.report_dir / f"{RUN_A}.preliminary.json").exists())
            self.assertFalse((config.report_dir / f"{RUN_A}.preliminary.md").exists())
            self.assertFalse((config.report_dir / "latest.json").exists())
            self.assertFalse((config.report_dir / "latest.md").exists())

            def publish_markdown_then_fail(
                directory_descriptor: int,
                source_name: str,
                destination_name: str,
            ) -> None:
                real_publish(directory_descriptor, source_name, destination_name)
                if destination_name.endswith(".md"):
                    raise OSError("simulated ambiguous post-link failure")

            with self.lock(root, RUN_A) as lock, mock.patch.object(
                report_module,
                "_publish_no_replace",
                side_effect=publish_markdown_then_fail,
            ), self.assertRaises(PersistenceError):
                persist_report(config, report, lock)

            self.assertFalse((config.report_dir / f"{RUN_A}.preliminary.json").exists())
            self.assertFalse((config.report_dir / f"{RUN_A}.preliminary.md").exists())

    def test_persist_requires_owned_matching_lock_and_rejects_symlinked_report_path(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            config = RadarConfig(report_dir=root / "reports")
            report = self.report()
            idle = self.lock(root, RUN_A)
            with self.assertRaises(PersistenceError):
                persist_report(config, report, idle)
            self.assertFalse(config.report_dir.exists())

            mismatched = self.lock(root, RUN_B)
            with mismatched, self.assertRaises(PersistenceError):
                persist_report(config, report, mismatched)
            self.assertFalse(config.report_dir.exists())

            outside = root / "outside"
            outside.mkdir()
            alias = root / "alias"
            alias.symlink_to(outside, target_is_directory=True)
            escaped = RadarConfig(report_dir=alias / "reports")
            with self.lock(root, RUN_A) as lock, self.assertRaises(PersistenceError):
                persist_report(escaped, report, lock)
            self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
