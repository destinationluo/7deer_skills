from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from steam_game_radar.config import RadarConfig
from steam_game_radar.errors import ConfigurationError


class RadarConfigTests(unittest.TestCase):
    def assert_config_values(
        self,
        config: RadarConfig,
        *,
        data_dir: Path,
        report_dir: Path,
    ) -> None:
        self.assertEqual(config.schema_version, 1)
        self.assertEqual(config.country, "US")
        self.assertEqual(config.language, "english")
        self.assertEqual(config.timezone, "Asia/Shanghai")
        self.assertEqual(config.schedule, "0 11 * * *")
        self.assertEqual(config.released_candidate_limit, 100)
        self.assertEqual(config.unreleased_candidate_limit, 100)
        self.assertEqual(config.preliminary_top_n, 50)
        self.assertEqual(config.enrichment_top_n, 20)
        self.assertEqual(config.final_top_n, 20)
        self.assertEqual(config.request_timeout_seconds, 10.0)
        self.assertEqual(config.max_retries, 2)
        self.assertEqual(config.minimum_request_interval_seconds, 1.0)
        self.assertEqual(config.raw_retention_days, 14)
        self.assertEqual(config.raw_max_bytes_per_provider, 5_242_880)
        self.assertEqual(config.stale_warning_hours, 24)
        self.assertEqual(config.stale_fallback_limit_hours, 72)
        self.assertEqual(config.data_dir, data_dir)
        self.assertEqual(config.report_dir, report_dir)

    def test_all_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RadarConfig.from_mapping({}, project_root=root)

        self.assert_config_values(
            config,
            data_dir=root / "data",
            report_dir=root / "reports",
        )

    def test_from_file(self) -> None:
        values = {
            "schema_version": 1,
            "country": "CN",
            "language": "schinese",
            "timezone": "UTC",
            "schedule": "30 7 * * 1",
            "released_candidate_limit": 80,
            "unreleased_candidate_limit": 70,
            "preliminary_top_n": 40,
            "enrichment_top_n": 15,
            "final_top_n": 10,
            "request_timeout_seconds": 8.5,
            "max_retries": 4,
            "minimum_request_interval_seconds": 1.5,
            "raw_retention_days": 21,
            "raw_max_bytes_per_provider": 9_000_000,
            "stale_warning_hours": 12,
            "stale_fallback_limit_hours": 48,
            "data_dir": "state",
            "report_dir": "output",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            path.write_text(json.dumps(values), encoding="utf-8")
            config = RadarConfig.from_file(path, project_root=root)

        self.assertEqual(config.schema_version, 1)
        self.assertEqual(config.country, "CN")
        self.assertEqual(config.language, "schinese")
        self.assertEqual(config.timezone, "UTC")
        self.assertEqual(config.schedule, "30 7 * * 1")
        self.assertEqual(config.released_candidate_limit, 80)
        self.assertEqual(config.unreleased_candidate_limit, 70)
        self.assertEqual(config.preliminary_top_n, 40)
        self.assertEqual(config.enrichment_top_n, 15)
        self.assertEqual(config.final_top_n, 10)
        self.assertEqual(config.request_timeout_seconds, 8.5)
        self.assertEqual(config.max_retries, 4)
        self.assertEqual(config.minimum_request_interval_seconds, 1.5)
        self.assertEqual(config.raw_retention_days, 21)
        self.assertEqual(config.raw_max_bytes_per_provider, 9_000_000)
        self.assertEqual(config.stale_warning_hours, 12)
        self.assertEqual(config.stale_fallback_limit_hours, 48)
        self.assertEqual(config.data_dir, root / "state")
        self.assertEqual(config.report_dir, root / "output")

    def test_unknown_schema_version(self) -> None:
        with self.assertRaises(ConfigurationError):
            RadarConfig.from_mapping({"schema_version": 2})

    def test_invalid_integer_limit(self) -> None:
        positive_integer_fields = (
            "released_candidate_limit",
            "unreleased_candidate_limit",
            "preliminary_top_n",
            "enrichment_top_n",
            "final_top_n",
            "raw_max_bytes_per_provider",
        )
        for field in positive_integer_fields:
            with self.subTest(field=field), self.assertRaises(ConfigurationError):
                RadarConfig.from_mapping({field: 0})
        with self.assertRaises(ConfigurationError):
            RadarConfig.from_mapping({"released_candidate_limit": True})
        with self.assertRaises(ConfigurationError):
            RadarConfig.from_mapping({"max_retries": -1})

    def test_invalid_timeout(self) -> None:
        for field in (
            "request_timeout_seconds",
            "minimum_request_interval_seconds",
        ):
            for invalid_value in (0, float("nan"), float("inf")):
                with self.subTest(
                    field=field, invalid_value=invalid_value
                ), self.assertRaises(ConfigurationError):
                    RadarConfig.from_mapping({field: invalid_value})
        with self.assertRaises(ConfigurationError):
            RadarConfig.from_mapping(
                {"stale_warning_hours": 72, "stale_fallback_limit_hours": 72}
            )

    def test_invalid_retention(self) -> None:
        for field in ("raw_retention_days", "raw_max_bytes_per_provider"):
            with self.subTest(field=field), self.assertRaises(ConfigurationError):
                RadarConfig.from_mapping({field: 0})

    def test_absolute_paths_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "absolute-data"
            report_dir = root / "absolute-reports"
            config = RadarConfig.from_mapping(
                {"data_dir": str(data_dir), "report_dir": str(report_dir)},
                project_root=root / "ignored",
            )

        self.assertEqual(config.data_dir, data_dir)
        self.assertEqual(config.report_dir, report_dir)

    def test_relative_paths_use_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RadarConfig.from_mapping(
                {"data_dir": "var/data", "report_dir": "var/reports"},
                project_root=root,
            )

        self.assertEqual(config.data_dir, root / "var/data")
        self.assertEqual(config.report_dir, root / "var/reports")

    def test_missing_project_root_uses_cwd(self) -> None:
        previous = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                os.chdir(root)
                active_root = Path.cwd()
                config = RadarConfig.from_mapping({})
        finally:
            os.chdir(previous)

        self.assertEqual(config.data_dir, active_root / "data")
        self.assertEqual(config.report_dir, active_root / "reports")

    def test_serialized_example_matches_defaults(self) -> None:
        example_path = PROJECT_DIR / "references/config.example.json"
        values = json.loads(example_path.read_text(encoding="utf-8"))
        expected = {
            "schema_version": 1,
            "country": "US",
            "language": "english",
            "timezone": "Asia/Shanghai",
            "schedule": "0 11 * * *",
            "released_candidate_limit": 100,
            "unreleased_candidate_limit": 100,
            "preliminary_top_n": 50,
            "enrichment_top_n": 20,
            "final_top_n": 20,
            "request_timeout_seconds": 10.0,
            "max_retries": 2,
            "minimum_request_interval_seconds": 1.0,
            "raw_retention_days": 14,
            "raw_max_bytes_per_provider": 5_242_880,
            "stale_warning_hours": 24,
            "stale_fallback_limit_hours": 72,
            "data_dir": "data",
            "report_dir": "reports",
        }
        self.assertEqual(values, expected)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = RadarConfig.from_mapping(values, project_root=root)
        self.assert_config_values(
            config,
            data_dir=root / "data",
            report_dir=root / "reports",
        )


if __name__ == "__main__":
    unittest.main()
