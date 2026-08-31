"""SQLite persistence for canonical unified-radar snapshots."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Iterator, Sequence

from .errors import PersistenceError
from .schemas import (
    GameIdentity,
    PlatformRecord,
    Publication,
    RadarRun,
    ScoredOpportunity,
    SourceHealth,
)


_SCHEMA_VERSION = 1

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        started_at TEXT NOT NULL,
        mode TEXT NOT NULL,
        canonical_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_health (
        run_id TEXT NOT NULL,
        collector TEXT NOT NULL,
        status TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        canonical_json TEXT NOT NULL,
        PRIMARY KEY (run_id, collector),
        FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS game_identities (
        opportunity_id TEXT PRIMARY KEY,
        normalized_name TEXT NOT NULL,
        official_domain TEXT,
        canonical_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS platform_records (
        platform TEXT NOT NULL,
        platform_id TEXT NOT NULL,
        opportunity_id TEXT NOT NULL,
        name TEXT NOT NULL,
        canonical_json TEXT NOT NULL,
        PRIMARY KEY (platform, platform_id),
        FOREIGN KEY (opportunity_id)
            REFERENCES game_identities(opportunity_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS observations (
        observation_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        platform_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        surface TEXT NOT NULL,
        geo TEXT NOT NULL,
        locale TEXT NOT NULL,
        query_parameters_json TEXT NOT NULL,
        metric_definition_version INTEGER NOT NULL,
        observed_at TEXT NOT NULL,
        canonical_json TEXT NOT NULL,
        FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS identity_links (
        source_platform TEXT NOT NULL,
        source_platform_id TEXT NOT NULL,
        target_platform TEXT NOT NULL,
        target_platform_id TEXT NOT NULL,
        canonical_json TEXT NOT NULL,
        PRIMARY KEY (
            source_platform,
            source_platform_id,
            target_platform,
            target_platform_id
        ),
        FOREIGN KEY (source_platform, source_platform_id)
            REFERENCES platform_records(platform, platform_id) ON DELETE CASCADE,
        FOREIGN KEY (target_platform, target_platform_id)
            REFERENCES platform_records(platform, platform_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence (
        run_id TEXT NOT NULL,
        opportunity_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        canonical_json TEXT NOT NULL,
        PRIMARY KEY (run_id, opportunity_id),
        FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
        FOREIGN KEY (opportunity_id)
            REFERENCES game_identities(opportunity_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scores (
        run_id TEXT NOT NULL,
        opportunity_id TEXT NOT NULL,
        demand_state TEXT NOT NULL,
        total_score REAL NOT NULL,
        action TEXT NOT NULL,
        canonical_json TEXT NOT NULL,
        PRIMARY KEY (run_id, opportunity_id),
        FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
        FOREIGN KEY (opportunity_id)
            REFERENCES game_identities(opportunity_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS publications (
        run_id TEXT NOT NULL,
        phase TEXT NOT NULL,
        published_at TEXT NOT NULL,
        daily_date TEXT NOT NULL,
        advances_daily_latest INTEGER NOT NULL,
        canonical_json TEXT NOT NULL,
        PRIMARY KEY (run_id, phase),
        FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS runs_started_at_idx ON runs(started_at)",
    """
    CREATE INDEX IF NOT EXISTS game_identities_normalized_name_idx
        ON game_identities(normalized_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS platform_records_opportunity_idx
        ON platform_records(opportunity_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS observations_history_idx
        ON observations(
            platform,
            platform_id,
            provider,
            surface,
            geo,
            locale,
            metric_definition_version,
            observed_at
        )
    """,
    """
    CREATE INDEX IF NOT EXISTS evidence_observed_at_idx
        ON evidence(observed_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS publications_daily_idx
        ON publications(daily_date, advances_daily_latest, published_at)
    """,
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _restore(payload: str, schema_type: object) -> object:
    try:
        decoded = json.loads(payload)
        return schema_type.from_dict(decoded)  # type: ignore[attr-defined]
    except Exception as error:
        raise PersistenceError("stored canonical JSON is invalid") from error


class RadarStore:
    """Own one explicit SQLite connection for a radar database."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._in_transaction = False

    def __enter__(self) -> "RadarStore":
        self.initialize()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def initialize(self) -> None:
        """Open the database and migrate an empty/version-1 file safely."""

        if self._connection is None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(
                    str(self.path),
                    isolation_level=None,
                )
                connection.execute("PRAGMA foreign_keys=ON")
                if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                    connection.close()
                    raise PersistenceError("SQLite foreign keys could not be enabled")
                connection.execute("PRAGMA journal_mode=WAL")
                self._connection = connection
            except (OSError, sqlite3.Error) as error:
                raise PersistenceError(
                    f"could not open radar database: {self.path}"
                ) from error

        connection = self._require_connection()
        try:
            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if current_version not in (0, _SCHEMA_VERSION):
                raise PersistenceError(
                    f"unsupported radar database version: {current_version}"
                )
            with self.transaction():
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        except PersistenceError:
            raise
        except sqlite3.Error as error:
            raise PersistenceError("could not initialize radar database") from error

    def close(self) -> None:
        """Close the owned connection; closing an active transaction rolls it back."""

        connection = self._connection
        if connection is None:
            return
        try:
            if self._in_transaction:
                connection.rollback()
            connection.close()
        finally:
            self._in_transaction = False
            self._connection = None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run writes atomically while preserving exceptions from callers."""

        connection = self._require_connection()
        if self._in_transaction:
            raise PersistenceError("nested radar transactions are not supported")
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as error:
            raise PersistenceError("could not begin radar transaction") from error
        self._in_transaction = True
        try:
            yield
        except BaseException:
            try:
                connection.rollback()
            finally:
                self._in_transaction = False
            raise
        else:
            try:
                connection.commit()
            except sqlite3.Error as error:
                try:
                    connection.rollback()
                finally:
                    self._in_transaction = False
                raise PersistenceError("could not commit radar transaction") from error
            self._in_transaction = False

    def create_run(self, run: RadarRun) -> None:
        self._write(
            """
            INSERT INTO runs(run_id, started_at, mode, canonical_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.to_dict()["started_at"],
                run.mode,
                _canonical_json(run.to_dict()),
            ),
        )

    def get_run(self, run_id: str) -> RadarRun | None:
        row = self._require_connection().execute(
            "SELECT canonical_json FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        restored = _restore(row[0], RadarRun)
        assert isinstance(restored, RadarRun)
        return restored

    def upsert_identity(self, identity: GameIdentity) -> None:
        self._write(
            """
            INSERT INTO game_identities(
                opportunity_id, normalized_name, official_domain, canonical_json
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(opportunity_id) DO UPDATE SET
                normalized_name = excluded.normalized_name,
                official_domain = excluded.official_domain,
                canonical_json = excluded.canonical_json
            """,
            (
                identity.opportunity_id,
                identity.normalized_name,
                identity.official_domain,
                _canonical_json(identity.to_dict()),
            ),
        )

    def bind_platform_record(
        self,
        opportunity_id: str,
        record: PlatformRecord,
    ) -> None:
        self._write(
            """
            INSERT INTO platform_records(
                platform, platform_id, opportunity_id, name, canonical_json
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(platform, platform_id) DO UPDATE SET
                opportunity_id = excluded.opportunity_id,
                name = excluded.name,
                canonical_json = excluded.canonical_json
            """,
            (
                record.platform,
                record.platform_id,
                opportunity_id,
                record.name,
                _canonical_json(record.to_dict()),
            ),
        )

    def save_source_health(self, health: SourceHealth) -> None:
        self._write(
            """
            INSERT INTO source_health(
                run_id, collector, status, observed_at, canonical_json
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id, collector) DO UPDATE SET
                status = excluded.status,
                observed_at = excluded.observed_at,
                canonical_json = excluded.canonical_json
            """,
            (
                health.run_id,
                health.collector,
                health.status,
                health.to_dict()["observed_at"],
                _canonical_json(health.to_dict()),
            ),
        )

    def get_source_health(
        self,
        run_id: str,
        collector: str,
    ) -> SourceHealth | None:
        row = self._require_connection().execute(
            """
            SELECT canonical_json FROM source_health
            WHERE run_id = ? AND collector = ?
            """,
            (run_id, collector),
        ).fetchone()
        if row is None:
            return None
        restored = _restore(row[0], SourceHealth)
        assert isinstance(restored, SourceHealth)
        return restored

    def save_score(self, score: ScoredOpportunity) -> None:
        self._write(
            """
            INSERT INTO scores(
                run_id, opportunity_id, demand_state, total_score,
                action, canonical_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, opportunity_id) DO UPDATE SET
                demand_state = excluded.demand_state,
                total_score = excluded.total_score,
                action = excluded.action,
                canonical_json = excluded.canonical_json
            """,
            (
                score.run_id,
                score.opportunity_id,
                score.demand_state,
                score.total_score,
                score.action,
                _canonical_json(score.to_dict()),
            ),
        )

    def get_score(
        self,
        run_id: str,
        opportunity_id: str,
    ) -> ScoredOpportunity | None:
        row = self._require_connection().execute(
            """
            SELECT canonical_json FROM scores
            WHERE run_id = ? AND opportunity_id = ?
            """,
            (run_id, opportunity_id),
        ).fetchone()
        if row is None:
            return None
        restored = _restore(row[0], ScoredOpportunity)
        assert isinstance(restored, ScoredOpportunity)
        return restored

    def publish(self, publication: Publication) -> None:
        self._write(
            """
            INSERT INTO publications(
                run_id, phase, published_at, daily_date,
                advances_daily_latest, canonical_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, phase) DO UPDATE SET
                published_at = excluded.published_at,
                daily_date = excluded.daily_date,
                advances_daily_latest = excluded.advances_daily_latest,
                canonical_json = excluded.canonical_json
            """,
            (
                publication.run_id,
                publication.phase,
                publication.to_dict()["published_at"],
                publication.to_dict()["daily_date"],
                int(publication.advances_daily_latest),
                _canonical_json(publication.to_dict()),
            ),
        )

    def get_publication(
        self,
        run_id: str,
        phase: str,
    ) -> Publication | None:
        row = self._require_connection().execute(
            """
            SELECT canonical_json FROM publications
            WHERE run_id = ? AND phase = ?
            """,
            (run_id, phase),
        ).fetchone()
        if row is None:
            return None
        restored = _restore(row[0], Publication)
        assert isinstance(restored, Publication)
        return restored

    def _write(self, statement: str, parameters: Sequence[object]) -> None:
        connection = self._require_connection()
        if self._in_transaction:
            connection.execute(statement, parameters)
            return
        with self.transaction():
            connection.execute(statement, parameters)

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise PersistenceError("radar store is not initialized")
        return self._connection


__all__ = ["RadarStore"]
