from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_DIR / "scripts" / "steam_radar.py"
sys.path.insert(0, str(PROJECT_DIR))

SPEC = importlib.util.spec_from_file_location("steam_radar_cli", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - importlib contract
    raise RuntimeError("unable to load Steam radar CLI")
cli = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cli
SPEC.loader.exec_module(cli)

from steam_game_radar.config import RadarConfig
from steam_game_radar.errors import (
    ConfigurationError,
    InputValidationError,
    PersistenceError,
    ProviderUnavailableError,
    RunBusyError,
)
from steam_game_radar.official_provider import CollectionResult
from steam_game_radar.schemas import GameRecord, MetricObservation, WarningRecord
from steam_game_radar.snapshot import persist_snapshot


UTC = timezone.utc
NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)
RUN_A = "20260824T120000Z-01020304"
RUN_B = "20260824T130000Z-05060708"
SAFE_TEMP_DIR = str(Path(tempfile.gettempdir()).resolve())


class CliTests(unittest.TestCase):
    def write_config(self, root: Path) -> Path:
        path = root / "radar.json"
        path.write_text(
            json.dumps(
                {
                    "data_dir": "state/steam-radar",
                    "report_dir": "output/steam-radar",
                    "max_retries": 0,
                    "minimum_request_interval_seconds": 0.01,
                }
            ),
            encoding="utf-8",
        )
        return path

    def config(self, root: Path) -> RadarConfig:
        return RadarConfig.from_file(self.write_config(root), project_root=root)

    def services(
        self,
        root: Path,
        *,
        now: datetime = NOW,
        entropy: bytes = b"\x01\x02\x03\x04",
        collection: CollectionResult | None = None,
    ) -> object:
        base = cli.Services()
        overrides: dict[str, object] = {
            "clock": lambda: now,
            "entropy": lambda size: entropy if size == 4 else b"",
            "hostname": lambda: "cli-test-host",
            "pid_alive": lambda _pid: False,
            "project_root": lambda: root,
            "client_factory": lambda _config: object(),
        }
        if collection is not None:
            overrides["collect_official"] = (
                lambda _client, _config, _observed_at: collection
            )
        return replace(base, **overrides)

    def official_record(
        self,
        appid: int = 10,
        *,
        observed: datetime = NOW,
        players: int = 1_000,
    ) -> GameRecord:
        stamp = observed.strftime("%Y-%m-%dT%H:%M:%SZ")
        return GameRecord(
            schema_version=1,
            appid=appid,
            name=f"Official {appid}",
            release_status="released",
            store_url=f"https://store.steampowered.com/app/{appid}/",
            metrics={
                "current_players": MetricObservation(
                    players,
                    "steam_current_players",
                    "steam_official",
                    stamp,
                ),
                "release_date": MetricObservation(
                    "2026-08-20",
                    "steam_appdetails",
                    "steam_official",
                    stamp,
                ),
            },
            source_extra={"app_type": "game"},
        )

    def collection(self) -> CollectionResult:
        return CollectionResult(
            released=(self.official_record(),),
            unreleased=(),
            capabilities={
                "most_played": True,
                "featured_categories": True,
                "appdetails": True,
                "current_players": True,
            },
            warnings=(),
            raw={"most_played": {"response": {"ok": True}}},
        )

    def write_manual_csv(self, root: Path, *, partial: bool = False) -> Path:
        path = root / "steamdb.csv"
        rows = [
            "appid,name,wishlist_7d_gain,release_date",
            "20,Manual Twenty,1000,2026-09-01",
        ]
        if partial:
            rows.append("not-an-appid,Broken,900,2026-09-02")
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return path

    def write_enrichment(self, root: Path, run_id: str) -> Path:
        path = root / f"{run_id}.enrichment.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "observed_at": "2026-08-24T12:20:00Z",
                    "games": [
                        {
                            "appid": 20,
                            "google_competition_gap_score": 80,
                            "expandable_queries": ["manual twenty guide"],
                            "youtube_relevant_7d": 3,
                            "reddit_relevant_7d": None,
                            "reddit_upvotes_7d": None,
                            "evidence": [
                                {
                                    "source": "google",
                                    "url": "https://www.google.com/search?q=manual+twenty",
                                },
                                {
                                    "source": "youtube",
                                    "url": "https://www.youtube.com/results?search_query=manual+twenty",
                                },
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def persist_official_history(self, root: Path, age: timedelta) -> str:
        observed = NOW - age
        run_id = observed.strftime("%Y%m%dT%H%M%SZ") + "-a1b2c3d4"
        persist_snapshot(
            self.config(root),
            run_id,
            (self.official_record(observed=observed),),
            {
                "provider": "steam_official",
                "mode": "official_scan",
                "data_status": "fresh",
                "warnings": [],
                "rejected_rows": [],
                "capabilities": {
                    "most_played": True,
                    "featured_categories": True,
                    "appdetails": True,
                    "current_players": True,
                },
            },
        )
        return run_id

    def latest(self, root: Path) -> dict[str, object]:
        return json.loads(
            (root / "output" / "steam-radar" / "latest.json").read_text(
                encoding="utf-8"
            )
        )

    def test_build_parser_parses_scan_exactly(self) -> None:
        parsed = cli.build_parser().parse_args(["scan", "--config", "cfg.json"])
        self.assertEqual(vars(parsed), {"command": "scan", "config": Path("cfg.json")})

    def test_build_parser_parses_import_exactly(self) -> None:
        parsed = cli.build_parser().parse_args(
            [
                "import-steamdb",
                "--config",
                "cfg.json",
                "--view",
                "wishlist_activity",
                "--input",
                "rows.csv",
            ]
        )
        self.assertEqual(
            vars(parsed),
            {
                "command": "import-steamdb",
                "config": Path("cfg.json"),
                "view": "wishlist_activity",
                "input": Path("rows.csv"),
            },
        )

    def test_build_parser_parses_enrich_exactly(self) -> None:
        parsed = cli.build_parser().parse_args(
            [
                "enrich",
                "--config",
                "cfg.json",
                "--run-id",
                RUN_A,
                "--input",
                "enrichment.json",
            ]
        )
        self.assertEqual(
            vars(parsed),
            {
                "command": "enrich",
                "config": Path("cfg.json"),
                "run_id": RUN_A,
                "input": Path("enrichment.json"),
            },
        )

    def test_direct_file_execution_imports_sibling_and_resolves_project_paths(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertLess(source.index("SKILL_ROOT ="), source.index("from steam_game_radar"))
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            self.write_config(root)
            self.write_manual_csv(root)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "import-steamdb",
                    "--config",
                    "radar.json",
                    "--view",
                    "wishlist_activity",
                    "--input",
                    "steamdb.csv",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = self.latest(root)
            self.assertEqual(report["mode"], "manual_baseline")
            self.assertTrue((root / "state" / "steam-radar" / "raw").is_dir())

    def test_run_lock_is_released_in_finally(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            self.write_config(root)
            services = replace(
                self.services(root),
                collect_official=lambda *_args: (_ for _ in ()).throw(
                    RuntimeError("collection exploded")
                ),
            )
            args = cli.build_parser().parse_args(["scan", "--config", "radar.json"])
            with self.assertRaisesRegex(RuntimeError, "collection exploded"):
                cli.run_scan(args, services)
            self.assertFalse(
                (root / "state" / "steam-radar" / ".run.lock").exists()
            )

    def test_scan_fresh_official_baseline_persists_raw_snapshot_and_report(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            self.write_config(root)
            events: list[str] = []
            base = self.services(root, collection=self.collection())
            services = replace(
                base,
                build_report=lambda **kwargs: (
                    events.append("build"),
                    cli.default_build_report(**kwargs),
                )[1],
                persist_report=lambda config, report, lock: (
                    events.append("persist"),
                    cli.default_persist_report(config, report, lock),
                )[1],
            )
            args = cli.build_parser().parse_args(["scan", "--config", "radar.json"])
            self.assertEqual(cli.run_scan(args, services), 0)
            report = self.latest(root)
            self.assertEqual((report["mode"], report["data_status"]), ("official_scan", "fresh"))
            self.assertEqual(report["newly_observed"], [10])
            self.assertEqual(events, ["build", "persist"])
            self.assertTrue(
                (root / "state" / "steam-radar" / "snapshots" / f"{RUN_A}.json").exists()
            )
            raw_files = list(
                (root / "state" / "steam-radar" / "raw" / RUN_A).glob(
                    "*.json"
                )
            )
            self.assertEqual([path.name for path in raw_files], ["most_played.json"])
            snapshot_path = (
                root / "state" / "steam-radar" / "snapshots" / f"{RUN_A}.json"
            )
            raw_path = raw_files[0]
            original_snapshot = snapshot_path.read_bytes()
            original_raw = raw_path.read_bytes()
            duplicate_events: list[str] = []
            duplicate_collection = CollectionResult(
                released=(self.official_record(players=99_999),),
                unreleased=(),
                capabilities=self.collection().capabilities,
                warnings=(),
                raw={"most_played": {"response": {"changed": True}}},
            )

            def duplicate_collect(*_args: object) -> CollectionResult:
                duplicate_events.append("collect")
                return duplicate_collection

            def duplicate_raw(*call_args: object) -> Path:
                duplicate_events.append("raw")
                return cli.default_persist_raw(*call_args)

            duplicate_services = replace(
                self.services(root),
                collect_official=duplicate_collect,
                persist_raw=duplicate_raw,
            )
            with self.assertRaises(PersistenceError):
                cli.run_scan(args, duplicate_services)
            self.assertEqual(duplicate_events, [])
            self.assertEqual(snapshot_path.read_bytes(), original_snapshot)
            self.assertEqual(raw_path.read_bytes(), original_raw)

    def test_scan_provider_failure_uses_fresh_official_fallback(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            self.persist_official_history(root, timedelta(hours=12))
            unavailable = CollectionResult(
                released=(),
                unreleased=(),
                capabilities={
                    "most_played": False,
                    "featured_categories": False,
                    "appdetails": True,
                    "current_players": True,
                },
                warnings=(
                    WarningRecord(
                        "steam_most_played_unavailable",
                        "Steam most-played data is unavailable.",
                    ),
                ),
                raw={"most_played": {"malformed": True}},
            )
            services = replace(
                self.services(root),
                collect_official=lambda *_args: unavailable,
            )
            args = cli.build_parser().parse_args(["scan", "--config", "radar.json"])
            self.assertEqual(cli.run_scan(args, services), 0)
            report = self.latest(root)
            self.assertEqual(report["data_status"], "fresh")
            self.assertIn(
                "steam_official_provider_unavailable",
                [warning["code"] for warning in report["warnings"]],
            )
            self.assertIn(
                "steam_most_played_unavailable",
                [warning["code"] for warning in report["warnings"]],
            )

        valid_capabilities = {
            "most_played": True,
            "featured_categories": True,
            "appdetails": True,
            "current_players": True,
        }

        def persist_fallback_candidate(
            root: Path,
            *,
            age_hours: int,
            entropy: str,
            appid: int,
            metadata: dict[str, object],
            include_record: bool = True,
        ) -> None:
            observed = NOW - timedelta(hours=age_hours)
            run_id = observed.strftime("%Y%m%dT%H%M%SZ") + f"-{entropy}"
            records = (
                (self.official_record(appid, observed=observed),)
                if include_record
                else ()
            )
            persist_snapshot(
                self.config(root),
                run_id,
                records,
                metadata,
            )

        valid_metadata: dict[str, object] = {
            "provider": "steam_official",
            "mode": "official_scan",
            "data_status": "fresh",
            "warnings": [],
            "rejected_rows": [],
            "capabilities": valid_capabilities,
        }
        invalid_metadata: tuple[tuple[int, str, int, dict[str, object], bool], ...] = (
            (
                20,
                "20202020",
                120,
                {key: value for key, value in valid_metadata.items() if key != "capabilities"},
                True,
            ),
            (
                19,
                "19191919",
                119,
                {
                    **valid_metadata,
                    "capabilities": {
                        **valid_capabilities,
                        "current_players": False,
                    },
                },
                True,
            ),
            (
                18,
                "18181818",
                118,
                {**valid_metadata, "capabilities": "not-a-mapping"},
                True,
            ),
            (17, "17171717", 117, valid_metadata, False),
            (
                16,
                "16161616",
                116,
                {**valid_metadata, "mode": "official_plus_manual"},
                True,
            ),
            (
                15,
                "15151515",
                115,
                {**valid_metadata, "data_status": "stale"},
                True,
            ),
            (
                14,
                "14141414",
                114,
                {**valid_metadata, "provider": "steamdb_manual_import"},
                True,
            ),
            (
                13,
                "13131313",
                113,
                {
                    **valid_metadata,
                    "provider": "steam_official_plus_manual",
                    "mode": "official_plus_manual",
                },
                True,
            ),
        )

        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            persist_fallback_candidate(
                root,
                age_hours=24,
                entropy="24242424",
                appid=10,
                metadata=valid_metadata,
            )
            for age, entropy, appid, metadata, include_record in invalid_metadata:
                persist_fallback_candidate(
                    root,
                    age_hours=age,
                    entropy=entropy,
                    appid=appid,
                    metadata=metadata,
                    include_record=include_record,
                )
            services = replace(
                self.services(root),
                collect_official=lambda *_args: (_ for _ in ()).throw(
                    ProviderUnavailableError("offline")
                ),
            )
            args = cli.build_parser().parse_args(
                ["scan", "--config", "radar.json"]
            )
            self.assertEqual(cli.run_scan(args, services), 0)
            self.assertEqual(
                [candidate["appid"] for candidate in self.latest(root)["released"]],
                [10],
            )

        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            missing_capabilities = invalid_metadata[0]
            persist_fallback_candidate(
                root,
                age_hours=missing_capabilities[0],
                entropy=missing_capabilities[1],
                appid=missing_capabilities[2],
                metadata=missing_capabilities[3],
            )
            scan_services = replace(
                self.services(root),
                collect_official=lambda *_args: (_ for _ in ()).throw(
                    ProviderUnavailableError("offline")
                ),
            )
            scan_args = cli.build_parser().parse_args(
                ["scan", "--config", "radar.json"]
            )
            with self.assertRaises(ProviderUnavailableError):
                cli.run_scan(scan_args, scan_services)

        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            missing_capabilities = invalid_metadata[0]
            persist_fallback_candidate(
                root,
                age_hours=missing_capabilities[0],
                entropy=missing_capabilities[1],
                appid=missing_capabilities[2],
                metadata=missing_capabilities[3],
            )
            self.write_manual_csv(root)
            import_args = cli.build_parser().parse_args(
                [
                    "import-steamdb",
                    "--config",
                    "radar.json",
                    "--view",
                    "wishlist_activity",
                    "--input",
                    "steamdb.csv",
                ]
            )
            self.assertEqual(cli.run_import(import_args, self.services(root)), 0)
            self.assertEqual(self.latest(root)["mode"], "manual_baseline")

    def test_scan_provider_failure_uses_stale_official_fallback_with_warning(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            self.persist_official_history(root, timedelta(hours=48))
            services = replace(
                self.services(root),
                collect_official=lambda *_args: (_ for _ in ()).throw(
                    ProviderUnavailableError("offline")
                ),
            )
            args = cli.build_parser().parse_args(["scan", "--config", "radar.json"])
            self.assertEqual(cli.run_scan(args, services), 0)
            report = self.latest(root)
            self.assertEqual(report["data_status"], "stale")
            self.assertIn(
                "steam_official_snapshot_stale",
                [warning["code"] for warning in report["warnings"]],
            )

        for age, expected_status in (
            (timedelta(hours=36), "fresh"),
            (timedelta(hours=36, seconds=1), "stale"),
        ):
            with self.subTest(age=age), tempfile.TemporaryDirectory(
                dir=SAFE_TEMP_DIR
            ) as directory:
                root = Path(directory)
                self.persist_official_history(root, age)
                services = replace(
                    self.services(
                        root,
                        now=NOW + timedelta(microseconds=500_000),
                    ),
                    collect_official=lambda *_args: (_ for _ in ()).throw(
                        ProviderUnavailableError("offline")
                    ),
                )
                args = cli.build_parser().parse_args(
                    ["scan", "--config", "radar.json"]
                )
                self.assertEqual(cli.run_scan(args, services), 0)
                self.assertEqual(self.latest(root)["data_status"], expected_status)

    def test_scan_provider_failure_rejects_expired_or_missing_fallback(self) -> None:
        for age in (None, timedelta(hours=72, seconds=1)):
            with self.subTest(age=age), tempfile.TemporaryDirectory(
                dir=SAFE_TEMP_DIR
            ) as directory:
                root = Path(directory)
                self.write_config(root)
                if age is not None:
                    self.persist_official_history(root, age)
                services = replace(
                    self.services(root),
                    collect_official=lambda *_args: (_ for _ in ()).throw(
                        ProviderUnavailableError("offline")
                    ),
                )
                args = cli.build_parser().parse_args(["scan", "--config", "radar.json"])
                with self.assertRaises(ProviderUnavailableError):
                    cli.run_scan(args, services)

        complete = self.collection()
        capability_variants = (
            {
                **dict(complete.capabilities),
                "current_players": False,
            },
            {
                **dict(complete.capabilities),
                "most_played": False,
            },
        )
        unusable_collections = tuple(
            CollectionResult(
                released=complete.released,
                unreleased=complete.unreleased,
                capabilities=capabilities,
                warnings=(),
                raw={},
            )
            for capabilities in capability_variants
        ) + (
            CollectionResult(
                released=(),
                unreleased=(),
                capabilities=complete.capabilities,
                warnings=(),
                raw={},
            ),
        )
        for collection in unusable_collections:
            with self.subTest(capabilities=collection.capabilities):
                with tempfile.TemporaryDirectory(
                    dir=SAFE_TEMP_DIR
                ) as directory:
                    root = Path(directory)
                    self.write_config(root)
                    args = cli.build_parser().parse_args(
                        ["scan", "--config", "radar.json"]
                    )
                    with self.assertRaises(ProviderUnavailableError):
                        cli.run_scan(
                            args,
                            self.services(root, collection=collection),
                        )

        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            self.persist_official_history(root, timedelta(hours=72))
            services = replace(
                self.services(
                    root,
                    now=NOW + timedelta(microseconds=500_000),
                ),
                collect_official=lambda *_args: (_ for _ in ()).throw(
                    ProviderUnavailableError("offline")
                ),
            )
            args = cli.build_parser().parse_args(
                ["scan", "--config", "radar.json"]
            )
            self.assertEqual(cli.run_scan(args, services), 0)
            self.assertEqual(self.latest(root)["data_status"], "stale")

    def test_import_manual_baseline_persists_canonical_raw_and_partial_rejections(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            self.write_config(root)
            self.write_manual_csv(root, partial=True)
            args = cli.build_parser().parse_args(
                [
                    "import-steamdb",
                    "--config",
                    "radar.json",
                    "--view",
                    "wishlist_activity",
                    "--input",
                    "steamdb.csv",
                ]
            )
            self.assertEqual(cli.run_import(args, self.services(root)), 0)
            report = self.latest(root)
            self.assertEqual((report["mode"], report["data_status"]), ("manual_baseline", "manual_only"))
            self.assertEqual(len(report["rejected_rows"]), 1)
            self.assertIn(
                "steamdb_rows_rejected",
                [warning["code"] for warning in report["warnings"]],
            )
            candidate = report["unreleased"][0]
            self.assertEqual(candidate["confidence"], "C")
            self.assertEqual(candidate["deltas"], {})
            self.assertIsNone(candidate["final_score"])
            self.assertEqual(candidate["action"], "needs_seo_enrichment")
            raw_path = (
                root
                / "state"
                / "steam-radar"
                / "raw"
                / RUN_A
                / "steamdb_wishlist_activity.json"
            )
            snapshot_path = (
                root / "state" / "steam-radar" / "snapshots" / f"{RUN_A}.json"
            )
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            self.assertEqual(raw["view"], "wishlist_activity")
            self.assertEqual(len(raw["rows"]), 2)
            original_raw = raw_path.read_bytes()
            original_snapshot = snapshot_path.read_bytes()
            self.write_manual_csv(root, partial=True).write_text(
                "appid,name,wishlist_7d_gain,release_date\n"
                "20,Manual Twenty,2000,2026-09-01\n"
                "not-an-appid,Broken,900,2026-09-02\n",
                encoding="utf-8",
            )
            duplicate_events: list[str] = []
            base = self.services(root)

            def duplicate_import(*call_args: object) -> object:
                duplicate_events.append("import")
                return cli.default_import_steamdb(*call_args)

            def duplicate_raw(*call_args: object) -> Path:
                duplicate_events.append("raw")
                return cli.default_persist_raw(*call_args)

            duplicate_services = replace(
                base,
                import_steamdb=duplicate_import,
                persist_raw=duplicate_raw,
            )
            with self.assertRaises(PersistenceError):
                cli.run_import(args, duplicate_services)
            self.assertEqual(duplicate_events, [])
            self.assertEqual(raw_path.read_bytes(), original_raw)
            self.assertEqual(snapshot_path.read_bytes(), original_snapshot)

    def test_import_merges_newest_eligible_official_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            self.persist_official_history(root, timedelta(hours=12))
            self.write_manual_csv(root)
            args = cli.build_parser().parse_args(
                [
                    "import-steamdb",
                    "--config",
                    "radar.json",
                    "--view",
                    "wishlist_activity",
                    "--input",
                    "steamdb.csv",
                ]
            )
            self.assertEqual(cli.run_import(args, self.services(root)), 0)
            report = self.latest(root)
            self.assertEqual(report["mode"], "official_plus_manual")
            self.assertEqual(
                ([item["appid"] for item in report["released"]], [item["appid"] for item in report["unreleased"]]),
                ([10], [20]),
            )

        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            self.persist_official_history(root, timedelta(hours=72))
            self.write_manual_csv(root)
            args = cli.build_parser().parse_args(
                [
                    "import-steamdb",
                    "--config",
                    "radar.json",
                    "--view",
                    "wishlist_activity",
                    "--input",
                    "steamdb.csv",
                ]
            )
            services = self.services(
                root,
                now=NOW + timedelta(microseconds=500_000),
            )
            self.assertEqual(cli.run_import(args, services), 0)
            self.assertEqual(self.latest(root)["mode"], "official_plus_manual")

    def test_enrich_requires_matching_run_and_builds_final_report(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            self.write_config(root)
            self.write_manual_csv(root)
            import_args = cli.build_parser().parse_args(
                [
                    "import-steamdb",
                    "--config",
                    "radar.json",
                    "--view",
                    "wishlist_activity",
                    "--input",
                    "steamdb.csv",
                ]
            )
            services = self.services(root)
            self.assertEqual(cli.run_import(import_args, services), 0)
            good = self.write_enrichment(root, RUN_A)
            bad = self.write_enrichment(root, RUN_B)
            enrich_args = cli.build_parser().parse_args(
                [
                    "enrich",
                    "--config",
                    "radar.json",
                    "--run-id",
                    RUN_A,
                    "--input",
                    good.name,
                ]
            )
            self.assertEqual(cli.run_enrich(enrich_args, services), 0)
            self.assertEqual(self.latest(root)["phase"], "final")
            mismatch = cli.build_parser().parse_args(
                [
                    "enrich",
                    "--config",
                    "radar.json",
                    "--run-id",
                    RUN_A,
                    "--input",
                    bad.name,
                ]
            )
            with self.assertRaises(InputValidationError):
                cli.run_enrich(mismatch, services)

    def test_main_maps_domain_errors_to_codes_two_through_six(self) -> None:
        cases = (
            (InputValidationError("bad input"), 2),
            (ProviderUnavailableError("offline"), 3),
            (ConfigurationError("bad config"), 4),
            (PersistenceError("disk"), 5),
            (RunBusyError("busy"), 6),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__), mock.patch.object(
                cli, "Services", return_value=object()
            ), mock.patch.object(cli, "run_scan", side_effect=error), mock.patch(
                "sys.stderr", new_callable=io.StringIO
            ):
                self.assertEqual(cli.main(["scan", "--config", "cfg.json"]), expected)

    def test_main_unexpected_exception_prints_traceback_and_returns_one(self) -> None:
        original = RuntimeError("unexpected sentinel")
        with mock.patch.object(
            cli, "Services", return_value=object()
        ), mock.patch.object(cli, "run_scan", side_effect=original), mock.patch(
            "sys.stderr", new_callable=io.StringIO
        ) as stderr:
            self.assertEqual(cli.main(["scan", "--config", "cfg.json"]), 1)
            self.assertIn("Traceback", stderr.getvalue())
            self.assertIn("RuntimeError: unexpected sentinel", stderr.getvalue())
            self.assertIsNone(original.__cause__)

    def test_delayed_older_enrichment_writes_final_without_replacing_latest(self) -> None:
        with tempfile.TemporaryDirectory(dir=SAFE_TEMP_DIR) as directory:
            root = Path(directory)
            self.write_config(root)
            self.write_manual_csv(root)

            def persist_manual_history(
                observed_at: datetime,
                wishlist_gain: int,
                entropy: str,
            ) -> None:
                stamp = observed_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                run_id = observed_at.strftime("%Y%m%dT%H%M%SZ") + f"-{entropy}"
                path = root / f"history-{entropy}.csv"
                path.write_text(
                    "appid,name,wishlist_7d_gain,release_date\n"
                    f"20,Manual Twenty,{wishlist_gain},2026-09-01\n",
                    encoding="utf-8",
                )
                imported = cli.default_import_steamdb(
                    path,
                    "wishlist_activity",
                    stamp,
                )
                persist_snapshot(
                    self.config(root),
                    run_id,
                    imported.records,
                    {"provider": "steamdb_manual_import"},
                )

            persist_manual_history(
                NOW - timedelta(days=7),
                250,
                "17171717",
            )
            persist_manual_history(
                NOW - timedelta(days=1),
                500,
                "23232323",
            )
            parser = cli.build_parser()
            import_args = parser.parse_args(
                [
                    "import-steamdb",
                    "--config",
                    "radar.json",
                    "--view",
                    "wishlist_activity",
                    "--input",
                    "steamdb.csv",
                ]
            )
            self.assertEqual(cli.run_import(import_args, self.services(root)), 0)
            self.assertEqual(
                cli.run_import(
                    import_args,
                    self.services(
                        root,
                        now=NOW + timedelta(hours=1),
                        entropy=b"\x05\x06\x07\x08",
                    ),
                ),
                0,
            )
            self.assertEqual(self.latest(root)["run_id"], RUN_B)
            enrichment = self.write_enrichment(root, RUN_A)
            enrich_args = parser.parse_args(
                [
                    "enrich",
                    "--config",
                    "radar.json",
                    "--run-id",
                    RUN_A,
                    "--input",
                    enrichment.name,
                ]
            )
            delayed_services = self.services(
                root,
                now=NOW + timedelta(hours=25),
            )
            self.assertEqual(cli.run_enrich(enrich_args, delayed_services), 0)
            final_path = root / "output" / "steam-radar" / f"{RUN_A}.final.json"
            self.assertTrue(final_path.exists())
            final_report = json.loads(final_path.read_text(encoding="utf-8"))
            self.assertEqual(final_report["generated_at"], "2026-08-25T13:00:00Z")
            final_candidate = final_report["unreleased"][0]
            self.assertEqual(
                final_candidate["deltas"],
                {
                    "wishlist_gain_7d_1d_percent": 100.0,
                    "wishlist_gain_7d_7d_percent": 300.0,
                },
            )
            self.assertEqual(final_candidate["steam_heat_score"], 55.0)
            self.assertEqual(final_candidate["confidence"], "B")
            self.assertIsNotNone(final_candidate["final_score"])
            self.assertEqual(self.latest(root)["run_id"], RUN_B)


if __name__ == "__main__":
    unittest.main()
