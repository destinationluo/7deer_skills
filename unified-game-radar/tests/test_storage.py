from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from unified_game_radar.errors import InputValidationError, PersistenceError
from unified_game_radar.schemas import (
    GameIdentity,
    PlatformRecord,
    Publication,
    RadarRun,
    ScoredOpportunity,
    SourceHealth,
    WarningRecord,
)
from unified_game_radar.storage import RadarStore


RUN_ID = "20260831T020000Z-a1b2c3d4"
SECOND_RUN_ID = "20260831T080000Z-b1c2d3e4"
MISSING_RUN_ID = "20260831T090000Z-c1d2e3f4"
OPPORTUNITY_ID = "0f840f6f-5c62-4ca6-9d53-e0be9ab2740b"
STARTED_AT = datetime(2026, 8, 31, 2, tzinfo=timezone.utc)


class FaultInjectingConnection:
    def __init__(self, path: Path) -> None:
        self.delegate = sqlite3.connect(str(path), isolation_level=None)
        self.fail_commit = False
        self.fail_rollback = False
        self.closed = False
        self.commit_error: sqlite3.OperationalError | None = None

    def execute(self, *args: object) -> sqlite3.Cursor:
        return self.delegate.execute(*args)

    def commit(self) -> None:
        if self.fail_commit:
            self.commit_error = sqlite3.OperationalError("injected commit failure")
            raise self.commit_error
        self.delegate.commit()

    def rollback(self) -> None:
        if self.fail_rollback:
            raise sqlite3.OperationalError("injected rollback failure")
        self.delegate.rollback()

    def close(self) -> None:
        self.closed = True
        self.delegate.close()


def make_run(run_id: str = RUN_ID) -> RadarRun:
    return RadarRun(
        schema_version=1,
        run_id=run_id,
        started_at=STARTED_AT,
        mode="scheduled",
        platforms=("itch", "steam", "roblox"),
        publish_daily=False,
    )


def make_record() -> PlatformRecord:
    return PlatformRecord(
        schema_version=1,
        platform="steam",
        platform_id="123456",
        name="Example Game",
        developer="Example Studio",
        official_domain="example.com",
        url="https://store.steampowered.com/app/123456/",
    )


def make_identity() -> GameIdentity:
    return GameIdentity(
        schema_version=1,
        opportunity_id=OPPORTUNITY_ID,
        name="Example Game",
        normalized_name="example game",
        developer="Example Studio",
        official_domain="example.com",
        platform_records=(make_record(),),
    )


def make_warning() -> WarningRecord:
    return WarningRecord(
        schema_version=1,
        code="missing_metric",
        message="Current players were unavailable",
        collector="steam",
        opportunity_id=OPPORTUNITY_ID,
    )


def make_health(run_id: str = RUN_ID) -> SourceHealth:
    return SourceHealth(
        schema_version=1,
        run_id=run_id,
        collector="steam",
        status="partial",
        observed_at=STARTED_AT,
        capabilities={"charts": True, "current_players": False},
        warnings=(make_warning(),),
    )


def make_score(run_id: str = RUN_ID) -> ScoredOpportunity:
    return ScoredOpportunity(
        schema_version=1,
        run_id=run_id,
        opportunity_id=OPPORTUNITY_ID,
        demand_state="pass",
        platform_score=15.0,
        demand_score=24.0,
        external_score=8.0,
        seo_score=16.0,
        total_score=63.0,
        action="watch",
        warnings=(make_warning(),),
    )


def make_publication() -> Publication:
    return Publication(
        schema_version=1,
        run_id=RUN_ID,
        phase="final",
        published_at=datetime(2026, 8, 31, 8, tzinfo=timezone.utc),
        report_json="reports/unified-game-radar/run.final.json",
        report_markdown="reports/unified-game-radar/run.final.md",
        daily_date=date(2026, 8, 31),
        advances_daily_latest=True,
    )


class RadarStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "radar.sqlite3"
        self.store = RadarStore(self.database_path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def test_initialize_creates_exact_version_one_schema(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()

        self.assertEqual(version, 1)
        self.assertEqual(
            names,
            {
                "runs",
                "source_health",
                "game_identities",
                "platform_records",
                "observations",
                "identity_links",
                "evidence",
                "scores",
                "publications",
            },
        )

        self.store.initialize()
        self.assertEqual(
            self.store._connection.execute("PRAGMA user_version").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.store._connection.execute("PRAGMA journal_mode").fetchone()[0],
            "wal",
        )

    def test_foreign_keys_are_enabled_and_enforced(self) -> None:
        self.assertEqual(
            self.store._connection.execute("PRAGMA foreign_keys").fetchone()[0],
            1,
        )
        with self.assertRaises(PersistenceError) as raised:
            self.store.bind_platform_record(OPPORTUNITY_ID, make_record())
        self.assertIsInstance(raised.exception.__cause__, sqlite3.IntegrityError)

    def test_transaction_rolls_back_all_rows(self) -> None:
        run = make_run()
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.create_run(run)
                raise RuntimeError("stop")
        self.assertIsNone(self.store.get_run(run.run_id))

    def test_create_and_get_run_round_trip(self) -> None:
        run = make_run()
        self.store.create_run(run)
        self.assertEqual(self.store.get_run(run.run_id), run)
        self.assertIsNone(self.store.get_run(MISSING_RUN_ID))

    def test_duplicate_run_is_reported_as_persistence_error(self) -> None:
        run = make_run()
        self.store.create_run(run)
        with self.assertRaises(PersistenceError) as raised:
            self.store.create_run(run)
        self.assertIsInstance(raised.exception.__cause__, sqlite3.IntegrityError)

    def test_write_outside_explicit_transaction_is_durable(self) -> None:
        run = make_run()
        self.store.create_run(run)
        self.store.close()

        self.store.initialize()
        self.assertEqual(self.store.get_run(run.run_id), run)

    def test_upsert_identity_and_bind_platform_record(self) -> None:
        identity = make_identity()
        record = make_record()
        self.store.upsert_identity(identity)
        self.store.bind_platform_record(identity.opportunity_id, record)

        identity_json = self.store._connection.execute(
            "SELECT canonical_json FROM game_identities WHERE opportunity_id = ?",
            (identity.opportunity_id,),
        ).fetchone()[0]
        platform_row = self.store._connection.execute(
            "SELECT opportunity_id, canonical_json FROM platform_records "
            "WHERE platform = ? AND platform_id = ?",
            (record.platform, record.platform_id),
        ).fetchone()
        self.assertEqual(
            GameIdentity.from_dict(json.loads(identity_json)), identity
        )
        self.assertEqual(platform_row[0], identity.opportunity_id)
        self.assertEqual(
            PlatformRecord.from_dict(json.loads(platform_row[1])), record
        )

    def test_source_health_crud(self) -> None:
        self.store.create_run(make_run())
        health = make_health()
        self.store.save_source_health(health)
        self.assertEqual(self.store.get_source_health(RUN_ID, "steam"), health)

        updated = replace(health, status="fresh")
        self.store.save_source_health(updated)
        self.assertEqual(self.store.get_source_health(RUN_ID, "steam"), updated)

    def test_score_crud(self) -> None:
        self.store.create_run(make_run())
        self.store.upsert_identity(make_identity())
        score = make_score()
        self.store.save_score(score)
        self.assertEqual(self.store.get_score(RUN_ID, OPPORTUNITY_ID), score)

        updated = replace(
            score,
            external_score=9.0,
            total_score=64.0,
        )
        self.store.save_score(updated)
        self.assertEqual(self.store.get_score(RUN_ID, OPPORTUNITY_ID), updated)

    def test_publication_crud(self) -> None:
        self.store.create_run(make_run())
        publication = make_publication()
        self.store.publish(publication)
        self.assertEqual(self.store.get_publication(RUN_ID, "final"), publication)

        updated = replace(publication, advances_daily_latest=False)
        self.store.publish(updated)
        self.assertEqual(self.store.get_publication(RUN_ID, "final"), updated)

    def test_same_opportunity_can_be_scored_in_multiple_runs(self) -> None:
        self.store.upsert_identity(make_identity())
        self.store.create_run(make_run())
        self.store.create_run(make_run(SECOND_RUN_ID))
        first = make_score()
        second = replace(make_score(SECOND_RUN_ID), action="worth_content_mvp")

        self.store.save_score(first)
        self.store.save_score(second)

        self.assertEqual(self.store.get_score(RUN_ID, OPPORTUNITY_ID), first)
        self.assertEqual(
            self.store.get_score(SECOND_RUN_ID, OPPORTUNITY_ID), second
        )

    def test_source_health_requires_an_existing_run(self) -> None:
        with self.assertRaises(PersistenceError) as raised:
            self.store.save_source_health(make_health(MISSING_RUN_ID))
        self.assertIsInstance(raised.exception.__cause__, sqlite3.IntegrityError)

    def test_score_requires_an_existing_run(self) -> None:
        self.store.upsert_identity(make_identity())
        with self.assertRaises(PersistenceError) as raised:
            self.store.save_score(make_score(MISSING_RUN_ID))
        self.assertIsInstance(raised.exception.__cause__, sqlite3.IntegrityError)

    def test_select_failure_is_reported_as_persistence_error(self) -> None:
        self.store._connection.execute("DROP TABLE runs")
        with self.assertRaises(PersistenceError) as raised:
            self.store.get_run(RUN_ID)
        self.assertIsInstance(raised.exception.__cause__, sqlite3.OperationalError)

    def test_faulted_connection_read_is_reported_as_persistence_error(self) -> None:
        self.store._connection.close()
        with self.assertRaises(PersistenceError) as raised:
            self.store.get_run(RUN_ID)
        self.assertIsInstance(raised.exception.__cause__, sqlite3.ProgrammingError)

    def test_invalid_stored_schema_is_reported_as_persistence_error(self) -> None:
        self.store.create_run(make_run())
        self.store._connection.execute(
            "UPDATE runs SET canonical_json = ? WHERE run_id = ?",
            ("{}", RUN_ID),
        )
        with self.assertRaises(PersistenceError) as raised:
            self.store.get_run(RUN_ID)
        self.assertIsInstance(raised.exception.__cause__, InputValidationError)

    def test_invalid_stored_json_is_reported_as_persistence_error(self) -> None:
        self.store.create_run(make_run())
        self.store._connection.execute(
            "UPDATE runs SET canonical_json = ? WHERE run_id = ?",
            ("{", RUN_ID),
        )
        with self.assertRaises(PersistenceError) as raised:
            self.store.get_run(RUN_ID)
        self.assertIsInstance(raised.exception.__cause__, json.JSONDecodeError)

    def test_close_releases_the_sqlite_connection(self) -> None:
        connection = self.store._connection
        self.store.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with self.assertRaises(PersistenceError):
            self.store.get_run(RUN_ID)

    def test_context_manager_initializes_and_closes_store(self) -> None:
        path = Path(self.temporary_directory.name) / "managed.sqlite3"
        with RadarStore(path) as managed:
            managed.create_run(make_run())
            connection = managed._connection
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_version_zero_reserved_malformed_table_is_rejected(self) -> None:
        path = Path(self.temporary_directory.name) / "malformed-v0.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, "
            "mode TEXT NOT NULL)"
        )
        connection.commit()
        connection.close()

        malformed = RadarStore(path)
        with self.assertRaises(PersistenceError):
            malformed.initialize()
        self.assertIsNone(malformed._connection)

        check = sqlite3.connect(path)
        try:
            self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 0)
        finally:
            check.close()

    def test_version_one_missing_required_index_is_rejected(self) -> None:
        path = Path(self.temporary_directory.name) / "missing-index.sqlite3"
        valid = RadarStore(path)
        valid.initialize()
        valid.close()
        connection = sqlite3.connect(path)
        connection.execute("DROP INDEX observations_history_idx")
        connection.commit()
        connection.close()

        malformed = RadarStore(path)
        with self.assertRaises(PersistenceError):
            malformed.initialize()
        self.assertIsNone(malformed._connection)

    def test_version_one_missing_required_foreign_key_is_rejected(self) -> None:
        path = Path(self.temporary_directory.name) / "missing-fk.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                canonical_json TEXT NOT NULL
            );
            CREATE TABLE source_health (
                run_id TEXT NOT NULL,
                collector TEXT NOT NULL,
                status TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                canonical_json TEXT NOT NULL,
                PRIMARY KEY (run_id, collector)
            );
            PRAGMA user_version=1;
            """
        )
        connection.close()

        malformed = RadarStore(path)
        with self.assertRaises(PersistenceError):
            malformed.initialize()
        self.assertIsNone(malformed._connection)

    def test_unsupported_version_failure_closes_local_connection(self) -> None:
        path = Path(self.temporary_directory.name) / "future.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA user_version=2")
        connection.close()
        tracked = FaultInjectingConnection(path)
        store = RadarStore(path, connection_factory=lambda _: tracked)

        with self.assertRaises(PersistenceError):
            store.initialize()
        self.assertIsNone(store._connection)
        self.assertTrue(tracked.closed)

    def test_context_manager_enter_failure_does_not_retain_connection(self) -> None:
        path = Path(self.temporary_directory.name) / "context-malformed.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL)"
        )
        connection.commit()
        connection.close()
        tracked = FaultInjectingConnection(path)
        store = RadarStore(path, connection_factory=lambda _: tracked)

        with self.assertRaises(PersistenceError):
            with store:
                self.fail("context body must not run")
        self.assertIsNone(store._connection)
        self.assertTrue(tracked.closed)

    def test_caller_exception_survives_rollback_failure(self) -> None:
        path = Path(self.temporary_directory.name) / "rollback.sqlite3"
        tracked = FaultInjectingConnection(path)
        store = RadarStore(path, connection_factory=lambda _: tracked)
        store.initialize()
        tracked.fail_rollback = True

        with self.assertRaisesRegex(RuntimeError, "caller failure"):
            with store.transaction():
                raise RuntimeError("caller failure")
        self.assertIsNone(store._connection)
        self.assertTrue(tracked.closed)

    def test_commit_failure_is_preserved_when_rollback_also_fails(self) -> None:
        path = Path(self.temporary_directory.name) / "commit.sqlite3"
        tracked = FaultInjectingConnection(path)
        store = RadarStore(path, connection_factory=lambda _: tracked)
        store.initialize()
        tracked.fail_commit = True
        tracked.fail_rollback = True

        with self.assertRaises(PersistenceError) as raised:
            with store.transaction():
                pass
        self.assertIs(raised.exception.__cause__, tracked.commit_error)
        self.assertIsNone(store._connection)
        self.assertTrue(tracked.closed)


if __name__ == "__main__":
    unittest.main()
