from __future__ import annotations

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPOSITORY_DIR = PROJECT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(REPOSITORY_DIR / "steam-game-radar"))

from steam_game_radar.config import RadarConfig as SteamRadarConfig
from unified_game_radar.config import IdentityAlias, RadarConfig
from unified_game_radar.errors import (
    EXIT_CONFIGURATION_ERROR,
    EXIT_IDEMPOTENCY_CONFLICT,
    EXIT_INPUT_ERROR,
    EXIT_PERSISTENCE_ERROR,
    EXIT_PROVIDER_UNAVAILABLE,
    EXIT_RUN_LOCKED,
    EXIT_SUCCESS,
    ConfigurationError,
    IdempotencyConflictError,
    InputValidationError,
    PersistenceError,
    ProviderUnavailableError,
    ReportError,
    RunBusyError,
)


class RadarConfigTests(unittest.TestCase):
    def test_complete_version_one_defaults(self) -> None:
        config = RadarConfig()

        self.assertEqual(
            config,
            RadarConfig(
                schema_version=1,
                timezone="Asia/Shanghai",
                country="US",
                locale="en",
                steam_language="english",
                steam_released_candidate_limit=50,
                steam_unreleased_candidate_limit=50,
                collection_hours=(10, 16),
                daily_publish_hour=16,
                enabled_platforms=("itch", "steam", "roblox"),
                preliminary_top_n=20,
                enrichment_top_n=10,
                final_top_n=10,
                heat_floor=30.0,
                fresh_hours=6,
                stale_fallback_hours=72,
                raw_retention_days=14,
                raw_max_bytes_per_provider=5_242_880,
                request_timeout_seconds=15.0,
                max_retries=3,
                minimum_request_interval_seconds=1.0,
                data_dir=Path("data/unified-game-radar"),
                report_dir=Path("reports/unified-game-radar"),
                identity_aliases=(),
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            config.country = "CN"  # type: ignore[misc]

    def test_defaults_resolve_from_project_root(self) -> None:
        config = RadarConfig.from_mapping({}, project_root=Path("/tmp/project"))

        self.assertEqual(
            config.data_dir,
            Path("/tmp/project/data/unified-game-radar"),
        )
        self.assertEqual(
            config.report_dir,
            Path("/tmp/project/reports/unified-game-radar"),
        )
        self.assertEqual(config.collection_hours, (10, 16))
        self.assertEqual(config.daily_publish_hour, 16)

    def test_file_loading_normalizes_sequences_aliases_and_paths(self) -> None:
        mapping = {
            "schema_version": 1,
            "timezone": "UTC",
            "country": "CN",
            "locale": "zh-CN",
            "steam_language": "schinese",
            "steam_released_candidate_limit": 80,
            "steam_unreleased_candidate_limit": 70,
            "collection_hours": [9, 17],
            "daily_publish_hour": 17,
            "enabled_platforms": ["steam", "roblox"],
            "preliminary_top_n": 30,
            "enrichment_top_n": 15,
            "final_top_n": 8,
            "heat_floor": 35,
            "fresh_hours": 8,
            "stale_fallback_hours": 48,
            "raw_retention_days": 21,
            "raw_max_bytes_per_provider": 9_000_000,
            "request_timeout_seconds": 8.5,
            "max_retries": 4,
            "minimum_request_interval_seconds": 1.5,
            "data_dir": "state",
            "report_dir": "output",
            "identity_aliases": [
                {
                    "schema_version": 1,
                    "source": "steam:123",
                    "target": "roblox:456",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            path.write_text(json.dumps(mapping), encoding="utf-8")

            config = RadarConfig.from_file(path, project_root=root)

        self.assertEqual(config.timezone, "UTC")
        self.assertEqual(config.country, "CN")
        self.assertEqual(config.locale, "zh-CN")
        self.assertEqual(config.collection_hours, (9, 17))
        self.assertEqual(config.enabled_platforms, ("steam", "roblox"))
        self.assertEqual(
            config.identity_aliases,
            (
                IdentityAlias(
                    schema_version=1,
                    source="steam:123",
                    target="roblox:456",
                ),
            ),
        )
        self.assertEqual(config.data_dir, root / "state")
        self.assertEqual(config.report_dir, root / "output")

    def test_absolute_paths_are_preserved(self) -> None:
        config = RadarConfig.from_mapping(
            {
                "data_dir": "/var/tmp/radar-data",
                "report_dir": "/var/tmp/radar-reports",
            },
            project_root=Path("/tmp/ignored"),
        )

        self.assertEqual(config.data_dir, Path("/var/tmp/radar-data"))
        self.assertEqual(config.report_dir, Path("/var/tmp/radar-reports"))

    def test_unknown_fields_are_rejected_at_every_mapping_boundary(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "unknown configuration"):
            RadarConfig.from_mapping({"score": 100})
        with self.assertRaisesRegex(ConfigurationError, "unknown identity alias"):
            RadarConfig.from_mapping(
                {
                    "identity_aliases": [
                        {
                            "schema_version": 1,
                            "source": "steam:123",
                            "target": "roblox:456",
                            "reason": "same developer",
                        }
                    ]
                }
            )

    def test_invalid_timezone_and_schema_versions_are_rejected(self) -> None:
        for value in ("Mars/Olympus", " UTC", ""):
            with self.subTest(timezone=value), self.assertRaises(ConfigurationError):
                RadarConfig.from_mapping({"timezone": value})
        for value in (2, "1", True):
            with self.subTest(schema_version=value), self.assertRaises(
                ConfigurationError
            ):
                RadarConfig.from_mapping({"schema_version": value})
        with self.assertRaises(ConfigurationError):
            IdentityAlias(2, "steam:123", "roblox:456")

    def test_invalid_limits_are_rejected(self) -> None:
        positive_integer_fields = (
            "steam_released_candidate_limit",
            "steam_unreleased_candidate_limit",
            "preliminary_top_n",
            "enrichment_top_n",
            "final_top_n",
            "fresh_hours",
            "stale_fallback_hours",
            "raw_retention_days",
            "raw_max_bytes_per_provider",
        )
        for field in positive_integer_fields:
            for value in (0, -1, True, 1.5):
                with self.subTest(field=field, value=value), self.assertRaises(
                    ConfigurationError
                ):
                    RadarConfig.from_mapping({field: value})
        for field in (
            "heat_floor",
            "request_timeout_seconds",
            "minimum_request_interval_seconds",
        ):
            for value in (0, -1, float("nan"), float("inf"), True):
                with self.subTest(field=field, value=value), self.assertRaises(
                    ConfigurationError
                ):
                    RadarConfig.from_mapping({field: value})
        with self.assertRaises(ConfigurationError):
            RadarConfig.from_mapping({"max_retries": -1})
        with self.assertRaises(ConfigurationError):
            RadarConfig.from_mapping({"max_retries": True})

    def test_numeric_limits_wrap_extreme_integer_conversion_errors(self) -> None:
        huge_integer = 10**400
        for field in (
            "heat_floor",
            "request_timeout_seconds",
            "minimum_request_interval_seconds",
        ):
            with self.subTest(
                field=field
            ), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(json.dumps({field: huge_integer}), encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    RadarConfig.from_file(path)

    def test_heat_floor_is_a_percentage(self) -> None:
        self.assertEqual(
            RadarConfig.from_mapping({"heat_floor": 100}).heat_floor,
            100,
        )
        for value in (100.1, 10**400):
            with self.subTest(value_type=type(value)), self.assertRaises(
                ConfigurationError
            ):
                RadarConfig.from_mapping({"heat_floor": value})

    def test_collection_and_daily_publication_settings_are_validated(self) -> None:
        self.assertEqual(
            RadarConfig.from_mapping(
                {"collection_hours": [10, 16], "daily_publish_hour": 16}
            ).collection_hours,
            (10, 16),
        )
        for hours in ([], [10, 10], [16, 10], [-1, 16], [10, 24], [10, True]):
            with self.subTest(hours=hours), self.assertRaises(ConfigurationError):
                RadarConfig.from_mapping({"collection_hours": hours})
        with self.assertRaises(ConfigurationError):
            RadarConfig.from_mapping({"daily_publish_hour": 11})

    def test_identity_aliases_are_immutable_exact_platform_key_pairs(self) -> None:
        alias = IdentityAlias(1, "steam:123", "roblox:456")
        with self.assertRaises(FrozenInstanceError):
            alias.target = "itch:example"  # type: ignore[misc]

        for source, target in (
            ("unknown:123", "roblox:456"),
            ("steam:", "roblox:456"),
            (" steam:123", "roblox:456"),
            ("steam:123", "steam:123"),
            ("steam:123", "steam:456"),
        ):
            with self.subTest(source=source, target=target), self.assertRaises(
                ConfigurationError
            ):
                IdentityAlias(1, source, target)

    def test_identity_alias_platform_ids_use_safe_canonical_formats(self) -> None:
        self.assertEqual(
            IdentityAlias(1, "itch:author-game_1.2", "steam:123"),
            IdentityAlias(1, "itch:author-game_1.2", "steam:123"),
        )
        valid_pairs = (
            ("steam:1", "roblox:999999"),
            ("roblox:7", "itch:game"),
        )
        for source, target in valid_pairs:
            with self.subTest(source=source, target=target):
                IdentityAlias(1, source, target)

        invalid_keys = (
            "steam:0",
            "steam:-1",
            "steam:+1",
            "steam:01",
            "steam:1/2",
            "roblox:0",
            "roblox:12.5",
            "roblox:\uff11",
            "itch:Game",
            "itch:-game",
            "itch:game-",
            "itch:game--copy",
            "itch:author/game",
            "itch:bad\x00key",
            "itch:bad\nkey",
            f"itch:{'a' * 129}",
        )
        for key in invalid_keys:
            with self.subTest(key=repr(key)), self.assertRaises(ConfigurationError):
                IdentityAlias(1, key, "roblox:456")

    def test_identity_alias_numeric_ids_respect_json_safe_boundary(self) -> None:
        IdentityAlias(1, f"steam:{2**53 - 1}", "roblox:1")

        for invalid in (2**53, 10**400):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ConfigurationError) as captured:
                    IdentityAlias(1, f"steam:{invalid}", "roblox:1")
                self.assertIsInstance(captured.exception.__cause__, InputValidationError)

    def test_enabled_platforms_are_exact_and_ordered(self) -> None:
        for platforms in (
            [],
            ["steam", "steam"],
            ["youtube"],
            ["roblox", "steam"],
        ):
            with self.subTest(platforms=platforms), self.assertRaises(
                ConfigurationError
            ):
                RadarConfig.from_mapping({"enabled_platforms": platforms})

    def test_to_steam_config_returns_complete_legacy_configuration(self) -> None:
        config = RadarConfig.from_mapping({}, project_root=Path("/tmp/project"))

        self.assertEqual(
            config.to_steam_config(),
            SteamRadarConfig(
                schema_version=1,
                country="US",
                language="english",
                timezone="Asia/Shanghai",
                schedule="0 10,16 * * *",
                released_candidate_limit=50,
                unreleased_candidate_limit=50,
                preliminary_top_n=20,
                enrichment_top_n=10,
                final_top_n=10,
                request_timeout_seconds=15.0,
                max_retries=3,
                minimum_request_interval_seconds=1.0,
                raw_retention_days=14,
                raw_max_bytes_per_provider=5_242_880,
                stale_warning_hours=6,
                stale_fallback_limit_hours=72,
                data_dir=Path("/tmp/project/data/unified-game-radar/steam"),
                report_dir=Path("/tmp/project/reports/unified-game-radar/steam"),
            ),
        )

    def test_to_steam_config_propagates_every_custom_legacy_field(self) -> None:
        config = RadarConfig.from_mapping(
            {
                "timezone": "UTC",
                "country": "CN",
                "locale": "zh-CN",
                "steam_language": "schinese",
                "steam_released_candidate_limit": 80,
                "steam_unreleased_candidate_limit": 70,
                "collection_hours": [9, 17],
                "daily_publish_hour": 17,
                "enabled_platforms": ["steam"],
                "preliminary_top_n": 30,
                "enrichment_top_n": 15,
                "final_top_n": 8,
                "heat_floor": 35,
                "fresh_hours": 8,
                "stale_fallback_hours": 48,
                "raw_retention_days": 21,
                "raw_max_bytes_per_provider": 9_000_000,
                "request_timeout_seconds": 8.5,
                "max_retries": 4,
                "minimum_request_interval_seconds": 1.5,
                "data_dir": "state",
                "report_dir": "output",
            },
            project_root=Path("/tmp/custom-project"),
        )

        self.assertEqual(
            config.to_steam_config(),
            SteamRadarConfig(
                schema_version=1,
                country="CN",
                language="schinese",
                timezone="UTC",
                schedule="0 9,17 * * *",
                released_candidate_limit=80,
                unreleased_candidate_limit=70,
                preliminary_top_n=30,
                enrichment_top_n=15,
                final_top_n=8,
                request_timeout_seconds=8.5,
                max_retries=4,
                minimum_request_interval_seconds=1.5,
                raw_retention_days=21,
                raw_max_bytes_per_provider=9_000_000,
                stale_warning_hours=8,
                stale_fallback_limit_hours=48,
                data_dir=Path("/tmp/custom-project/state/steam"),
                report_dir=Path("/tmp/custom-project/output/steam"),
            ),
        )

    def test_file_loading_rejects_duplicate_keys_at_any_depth(self) -> None:
        duplicate_documents = (
            '{"schema_version": 1, "schema_version": 1}',
            (
                '{"identity_aliases": [{"schema_version": 1, '
                '"source": "steam:123", "target": "roblox:456", '
                '"target": "roblox:789"}]}'
            ),
        )
        for document in duplicate_documents:
            with self.subTest(
                document=document
            ), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(document, encoding="utf-8")
                with self.assertRaisesRegex(
                    ConfigurationError,
                    "duplicate JSON key",
                ):
                    RadarConfig.from_file(path)

    def test_file_loading_wraps_invalid_missing_and_non_object_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid_path = root / "invalid.json"
            invalid_path.write_text("[", encoding="utf-8")
            array_path = root / "array.json"
            array_path.write_text("[]", encoding="utf-8")

            for path in (invalid_path, array_path, root / "missing.json"):
                with self.subTest(path=path), self.assertRaises(ConfigurationError):
                    RadarConfig.from_file(path)

    def test_serialized_example_is_the_complete_default_mapping(self) -> None:
        example = json.loads(
            (PROJECT_DIR / "references/config.example.json").read_text(
                encoding="utf-8"
            )
        )
        expected = {
            "schema_version": 1,
            "timezone": "Asia/Shanghai",
            "country": "US",
            "locale": "en",
            "steam_language": "english",
            "steam_released_candidate_limit": 50,
            "steam_unreleased_candidate_limit": 50,
            "collection_hours": [10, 16],
            "daily_publish_hour": 16,
            "enabled_platforms": ["itch", "steam", "roblox"],
            "preliminary_top_n": 20,
            "enrichment_top_n": 10,
            "final_top_n": 10,
            "heat_floor": 30.0,
            "fresh_hours": 6,
            "stale_fallback_hours": 72,
            "raw_retention_days": 14,
            "raw_max_bytes_per_provider": 5_242_880,
            "request_timeout_seconds": 15.0,
            "max_retries": 3,
            "minimum_request_interval_seconds": 1.0,
            "data_dir": "data/unified-game-radar",
            "report_dir": "reports/unified-game-radar",
            "identity_aliases": [],
        }

        self.assertEqual(example, expected)
        self.assertEqual(
            RadarConfig.from_mapping(example, project_root=Path("/tmp/project")),
            RadarConfig.from_mapping({}, project_root=Path("/tmp/project")),
        )

    def test_exit_code_contract(self) -> None:
        self.assertEqual(EXIT_SUCCESS, 0)
        exception_codes = (
            (InputValidationError, EXIT_INPUT_ERROR, 2),
            (ProviderUnavailableError, EXIT_PROVIDER_UNAVAILABLE, 3),
            (ConfigurationError, EXIT_CONFIGURATION_ERROR, 4),
            (PersistenceError, EXIT_PERSISTENCE_ERROR, 5),
            (ReportError, EXIT_PERSISTENCE_ERROR, 5),
            (RunBusyError, EXIT_RUN_LOCKED, 6),
            (IdempotencyConflictError, EXIT_IDEMPOTENCY_CONFLICT, 7),
        )
        for error_type, constant, expected in exception_codes:
            with self.subTest(error_type=error_type):
                self.assertEqual(constant, expected)
                self.assertEqual(error_type.exit_code, expected)


if __name__ == "__main__":
    unittest.main()
