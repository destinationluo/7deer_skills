"""SQLite persistence for canonical unified-radar snapshots."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
from typing import Callable, Iterator, Protocol, Sequence

from .errors import IdempotencyConflictError, PersistenceError
from .schemas import (
    GameIdentity,
    OpportunityEvidence,
    PlatformObservation,
    PlatformRecord,
    Publication,
    RadarRun,
    ScoredOpportunity,
    SourceHealth,
)


_SCHEMA_VERSION = 1
_MAX_HISTORY_HOURS = 24 * 366 * 100


class _Connection(Protocol):
    def execute(
        self,
        statement: str,
        parameters: Sequence[object] = (),
    ) -> sqlite3.Cursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


def _open_connection(path: str) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


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


_Column = tuple[str, str, int, int]
_TABLE_MANIFEST: dict[str, tuple[_Column, ...]] = {
    "runs": (
        ("run_id", "TEXT", 0, 1),
        ("started_at", "TEXT", 1, 0),
        ("mode", "TEXT", 1, 0),
        ("canonical_json", "TEXT", 1, 0),
    ),
    "source_health": (
        ("run_id", "TEXT", 1, 1),
        ("collector", "TEXT", 1, 2),
        ("status", "TEXT", 1, 0),
        ("observed_at", "TEXT", 1, 0),
        ("canonical_json", "TEXT", 1, 0),
    ),
    "game_identities": (
        ("opportunity_id", "TEXT", 0, 1),
        ("normalized_name", "TEXT", 1, 0),
        ("official_domain", "TEXT", 0, 0),
        ("canonical_json", "TEXT", 1, 0),
    ),
    "platform_records": (
        ("platform", "TEXT", 1, 1),
        ("platform_id", "TEXT", 1, 2),
        ("opportunity_id", "TEXT", 1, 0),
        ("name", "TEXT", 1, 0),
        ("canonical_json", "TEXT", 1, 0),
    ),
    "observations": (
        ("observation_id", "TEXT", 0, 1),
        ("run_id", "TEXT", 1, 0),
        ("platform", "TEXT", 1, 0),
        ("platform_id", "TEXT", 1, 0),
        ("provider", "TEXT", 1, 0),
        ("surface", "TEXT", 1, 0),
        ("geo", "TEXT", 1, 0),
        ("locale", "TEXT", 1, 0),
        ("query_parameters_json", "TEXT", 1, 0),
        ("metric_definition_version", "INTEGER", 1, 0),
        ("observed_at", "TEXT", 1, 0),
        ("canonical_json", "TEXT", 1, 0),
    ),
    "identity_links": (
        ("source_platform", "TEXT", 1, 1),
        ("source_platform_id", "TEXT", 1, 2),
        ("target_platform", "TEXT", 1, 3),
        ("target_platform_id", "TEXT", 1, 4),
        ("canonical_json", "TEXT", 1, 0),
    ),
    "evidence": (
        ("run_id", "TEXT", 1, 1),
        ("opportunity_id", "TEXT", 1, 2),
        ("observed_at", "TEXT", 1, 0),
        ("canonical_json", "TEXT", 1, 0),
    ),
    "scores": (
        ("run_id", "TEXT", 1, 1),
        ("opportunity_id", "TEXT", 1, 2),
        ("demand_state", "TEXT", 1, 0),
        ("total_score", "REAL", 1, 0),
        ("action", "TEXT", 1, 0),
        ("canonical_json", "TEXT", 1, 0),
    ),
    "publications": (
        ("run_id", "TEXT", 1, 1),
        ("phase", "TEXT", 1, 2),
        ("published_at", "TEXT", 1, 0),
        ("daily_date", "TEXT", 1, 0),
        ("advances_daily_latest", "INTEGER", 1, 0),
        ("canonical_json", "TEXT", 1, 0),
    ),
}


_ForeignKey = tuple[str, str, tuple[tuple[int, str, str], ...]]
_FOREIGN_KEY_MANIFEST: dict[str, frozenset[_ForeignKey]] = {
    "runs": frozenset(),
    "source_health": frozenset(
        {("runs", "CASCADE", ((0, "run_id", "run_id"),))}
    ),
    "game_identities": frozenset(),
    "platform_records": frozenset(
        {
            (
                "game_identities",
                "CASCADE",
                ((0, "opportunity_id", "opportunity_id"),),
            )
        }
    ),
    "observations": frozenset(
        {("runs", "CASCADE", ((0, "run_id", "run_id"),))}
    ),
    "identity_links": frozenset(
        {
            (
                "platform_records",
                "CASCADE",
                (
                    (0, "source_platform", "platform"),
                    (1, "source_platform_id", "platform_id"),
                ),
            ),
            (
                "platform_records",
                "CASCADE",
                (
                    (0, "target_platform", "platform"),
                    (1, "target_platform_id", "platform_id"),
                ),
            ),
        }
    ),
    "evidence": frozenset(
        {
            ("runs", "CASCADE", ((0, "run_id", "run_id"),)),
            (
                "game_identities",
                "CASCADE",
                ((0, "opportunity_id", "opportunity_id"),),
            ),
        }
    ),
    "scores": frozenset(
        {
            ("runs", "CASCADE", ((0, "run_id", "run_id"),)),
            (
                "game_identities",
                "CASCADE",
                ((0, "opportunity_id", "opportunity_id"),),
            ),
        }
    ),
    "publications": frozenset(
        {("runs", "CASCADE", ((0, "run_id", "run_id"),))}
    ),
}


_INDEX_MANIFEST: dict[str, tuple[str, tuple[str, ...]]] = {
    "runs_started_at_idx": ("runs", ("started_at",)),
    "game_identities_normalized_name_idx": (
        "game_identities",
        ("normalized_name",),
    ),
    "platform_records_opportunity_idx": (
        "platform_records",
        ("opportunity_id",),
    ),
    "observations_history_idx": (
        "observations",
        (
            "platform",
            "platform_id",
            "provider",
            "surface",
            "geo",
            "locale",
            "metric_definition_version",
            "observed_at",
        ),
    ),
    "evidence_observed_at_idx": ("evidence", ("observed_at",)),
    "publications_daily_idx": (
        "publications",
        ("daily_date", "advances_daily_latest", "published_at"),
    ),
}


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


def _execute_on(
    connection: _Connection,
    statement: str,
    parameters: Sequence[object] = (),
) -> sqlite3.Cursor:
    try:
        return connection.execute(statement, parameters)
    except sqlite3.Error as error:
        raise PersistenceError("SQLite operation failed") from error


def _fetchone_on(
    connection: _Connection,
    statement: str,
    parameters: Sequence[object] = (),
) -> tuple[object, ...] | None:
    try:
        return _execute_on(connection, statement, parameters).fetchone()
    except sqlite3.Error as error:
        raise PersistenceError("SQLite read failed") from error


def _fetchall_on(
    connection: _Connection,
    statement: str,
    parameters: Sequence[object] = (),
) -> list[tuple[object, ...]]:
    try:
        return _execute_on(connection, statement, parameters).fetchall()
    except sqlite3.Error as error:
        raise PersistenceError("SQLite read failed") from error


def _validate_columns(connection: _Connection, table: str) -> None:
    rows = _fetchall_on(connection, f'PRAGMA table_info("{table}")')
    actual = tuple(
        (
            str(row[1]),
            str(row[2]).upper(),
            int(row[3]),
            int(row[5]),
        )
        for row in rows
    )
    if actual != _TABLE_MANIFEST[table]:
        raise PersistenceError(f"incompatible SQLite table schema: {table}")


def _validate_foreign_keys(connection: _Connection, table: str) -> None:
    rows = _fetchall_on(
        connection,
        f'PRAGMA foreign_key_list("{table}")',
    )
    grouped: dict[int, tuple[str, str, list[tuple[int, str, str]]]] = {}
    for row in rows:
        identifier = int(row[0])
        target_table = str(row[2])
        on_delete = str(row[6]).upper()
        if identifier not in grouped:
            grouped[identifier] = (target_table, on_delete, [])
        group_table, group_delete, columns = grouped[identifier]
        if group_table != target_table or group_delete != on_delete:
            raise PersistenceError(
                f"incompatible SQLite foreign key schema: {table}"
            )
        columns.append((int(row[1]), str(row[3]), str(row[4])))
    actual = frozenset(
        (target, on_delete, tuple(sorted(columns)))
        for target, on_delete, columns in grouped.values()
    )
    if actual != _FOREIGN_KEY_MANIFEST[table]:
        raise PersistenceError(f"incompatible SQLite foreign keys: {table}")


def _validate_index(
    connection: _Connection,
    index: str,
    table: str,
    columns: tuple[str, ...],
) -> None:
    record = _fetchone_on(
        connection,
        "SELECT type, tbl_name FROM sqlite_master WHERE name = ?",
        (index,),
    )
    if record != ("index", table):
        raise PersistenceError(f"missing or incompatible SQLite index: {index}")
    index_rows = _fetchall_on(
        connection,
        f'PRAGMA index_list("{table}")',
    )
    matching = [row for row in index_rows if row[1] == index]
    if len(matching) != 1:
        raise PersistenceError(f"missing or incompatible SQLite index: {index}")
    row = matching[0]
    if int(row[2]) != 0 or str(row[3]) != "c" or int(row[4]) != 0:
        raise PersistenceError(f"incompatible SQLite index options: {index}")
    column_rows = _fetchall_on(
        connection,
        f'PRAGMA index_info("{index}")',
    )
    actual_columns = tuple(
        str(column_row[2])
        for column_row in sorted(column_rows, key=lambda item: int(item[0]))
    )
    if actual_columns != columns:
        raise PersistenceError(f"incompatible SQLite index columns: {index}")


def _validate_schema(connection: _Connection) -> None:
    for table in _TABLE_MANIFEST:
        _validate_columns(connection, table)
        _validate_foreign_keys(connection, table)
    for index, (table, columns) in _INDEX_MANIFEST.items():
        _validate_index(connection, index, table, columns)


def _rollback_local(connection: _Connection) -> None:
    try:
        connection.rollback()
    except BaseException:
        pass


def _initialize_version_zero(connection: _Connection) -> None:
    _execute_on(connection, "BEGIN IMMEDIATE")
    try:
        for statement in _SCHEMA:
            _execute_on(connection, statement)
        _execute_on(connection, f"PRAGMA user_version={_SCHEMA_VERSION}")
        _validate_schema(connection)
        try:
            connection.commit()
        except sqlite3.Error as error:
            _rollback_local(connection)
            raise PersistenceError(
                "could not commit radar schema migration"
            ) from error
    except BaseException:
        _rollback_local(connection)
        raise


class RadarStore:
    """Own one thread-confined SQLite connection for a radar database."""

    def __init__(
        self,
        path: Path,
        connection_factory: Callable[[str], _Connection] | None = None,
    ) -> None:
        self.path = Path(path)
        self._connection_factory = connection_factory or _open_connection
        self._connection: _Connection | None = None
        self._in_transaction = False

    def __enter__(self) -> "RadarStore":
        self.initialize()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def initialize(self) -> None:
        """Open the database and migrate an empty/version-1 file safely."""

        if self._connection is not None:
            connection = self._connection
            try:
                version_row = _fetchone_on(
                    connection,
                    "PRAGMA user_version",
                )
                assert version_row is not None
                version = version_row[0]
                if version != _SCHEMA_VERSION:
                    raise PersistenceError(
                        f"unsupported radar database version: {version}"
                    )
                _validate_schema(connection)
            except BaseException:
                self._discard_connection(connection)
                raise
            return

        connection: _Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connection_factory(str(self.path))
            _execute_on(connection, "PRAGMA foreign_keys=ON")
            foreign_keys_row = _fetchone_on(
                connection,
                "PRAGMA foreign_keys",
            )
            if foreign_keys_row is None or foreign_keys_row[0] != 1:
                raise PersistenceError("SQLite foreign keys could not be enabled")
            _execute_on(connection, "PRAGMA journal_mode=WAL")
            version_row = _fetchone_on(
                connection,
                "PRAGMA user_version",
            )
            assert version_row is not None
            current_version = version_row[0]
            if current_version == 0:
                _initialize_version_zero(connection)
            elif current_version == _SCHEMA_VERSION:
                _validate_schema(connection)
            else:
                raise PersistenceError(
                    f"unsupported radar database version: {current_version}"
                )
        except BaseException as error:
            if connection is not None:
                try:
                    connection.close()
                except BaseException:
                    pass
            self._connection = None
            self._in_transaction = False
            if isinstance(error, PersistenceError):
                raise
            if isinstance(error, (OSError, sqlite3.Error)):
                raise PersistenceError(
                    f"could not open radar database: {self.path}"
                ) from error
            raise
        assert connection is not None
        self._connection = connection

    def close(self) -> None:
        """Close the owned connection; closing an active transaction rolls it back."""

        connection = self._connection
        if connection is None:
            return
        if self._in_transaction:
            try:
                connection.rollback()
            except BaseException:
                pass
        self._discard_connection(connection)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run writes atomically while preserving exceptions from callers."""

        connection = self._require_connection()
        if self._in_transaction:
            raise PersistenceError("nested radar transactions are not supported")
        self._execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield
        except BaseException:
            try:
                connection.rollback()
            except BaseException:
                self._discard_connection(connection)
            self._in_transaction = False
            raise
        else:
            try:
                connection.commit()
            except sqlite3.Error as error:
                try:
                    connection.rollback()
                except BaseException:
                    self._discard_connection(connection)
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
        row = self._fetchone(
            "SELECT canonical_json FROM runs WHERE run_id = ?",
            (run_id,),
        )
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
        row = self._fetchone(
            """
            SELECT canonical_json FROM source_health
            WHERE run_id = ? AND collector = ?
            """,
            (run_id, collector),
        )
        if row is None:
            return None
        restored = _restore(row[0], SourceHealth)
        assert isinstance(restored, SourceHealth)
        return restored

    def insert_observation(self, observation: PlatformObservation) -> bool:
        """Insert one immutable observation; exact retries are no-ops."""

        observation_dict = observation.to_dict()
        canonical = _canonical_json(observation_dict)
        parameters = (
            observation.observation_id,
            observation.run_id,
            observation.platform,
            observation.platform_id,
            observation.provider,
            observation.surface,
            observation.geo,
            observation.locale,
            _canonical_json(observation_dict["query_parameters"]),
            observation.metric_definition_version,
            observation_dict["observed_at"],
            canonical,
        )

        def insert() -> bool:
            existing = self._fetchone(
                """
                SELECT canonical_json FROM observations
                WHERE observation_id = ?
                """,
                (observation.observation_id,),
            )
            if existing is not None:
                if existing[0] == canonical:
                    return False
                raise IdempotencyConflictError(
                    "observation_id was reused with changed content: "
                    f"{observation.observation_id}"
                )
            self._execute(
                """
                INSERT INTO observations(
                    observation_id, run_id, platform, platform_id, provider,
                    surface, geo, locale, query_parameters_json,
                    metric_definition_version, observed_at, canonical_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                parameters,
            )
            return True

        return self._immutable_write(insert)

    def insert_evidence(self, evidence: OpportunityEvidence) -> bool:
        """Insert immutable evidence for one run/opportunity pair."""

        evidence_dict = evidence.to_dict()
        canonical = _canonical_json(evidence_dict)

        def insert() -> bool:
            existing = self._fetchone(
                """
                SELECT canonical_json FROM evidence
                WHERE run_id = ? AND opportunity_id = ?
                """,
                (evidence.run_id, evidence.opportunity_id),
            )
            if existing is not None:
                if existing[0] == canonical:
                    return False
                raise IdempotencyConflictError(
                    "evidence key was reused with changed content: "
                    f"{evidence.run_id}/{evidence.opportunity_id}"
                )
            self._execute(
                """
                INSERT INTO evidence(
                    run_id, opportunity_id, observed_at, canonical_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    evidence.run_id,
                    evidence.opportunity_id,
                    evidence_dict["observed_at"],
                    canonical,
                ),
            )
            return True

        return self._immutable_write(insert)

    def compatible_observation(
        self,
        current: PlatformObservation,
        target_hours: int,
        tolerance_hours: int,
    ) -> PlatformObservation | None:
        """Return the nearest compatible past observation in a safe window."""

        self._validate_history_window(target_hours, tolerance_hours)
        try:
            oldest = current.observed_at - timedelta(
                hours=target_hours + tolerance_hours
            )
            newest = current.observed_at - timedelta(
                hours=target_hours - tolerance_hours
            )
        except (OverflowError, ValueError) as error:
            raise ValueError(
                "history window is outside the datetime range"
            ) from error

        current_dict = current.to_dict()
        query_parameters_json = _canonical_json(
            current_dict["query_parameters"]
        )
        rows = self._fetchall(
            """
            SELECT query_parameters_json, canonical_json
            FROM observations
            WHERE platform = ?
              AND platform_id = ?
              AND provider = ?
              AND surface = ?
              AND geo = ?
              AND locale = ?
              AND metric_definition_version = ?
              AND observed_at >= ?
              AND observed_at <= ?
              AND observed_at < ?
            """,
            (
                current.platform,
                current.platform_id,
                current.provider,
                current.surface,
                current.geo,
                current.locale,
                current.metric_definition_version,
                oldest.isoformat().replace("+00:00", "Z"),
                newest.isoformat().replace("+00:00", "Z"),
                current_dict["observed_at"],
            ),
        )

        candidates: list[PlatformObservation] = []
        for stored_parameters, canonical in rows:
            if stored_parameters != query_parameters_json:
                continue
            restored = _restore(canonical, PlatformObservation)
            assert isinstance(restored, PlatformObservation)
            if (
                restored.platform != current.platform
                or restored.platform_id != current.platform_id
                or restored.provider != current.provider
                or restored.surface != current.surface
                or restored.geo != current.geo
                or restored.locale != current.locale
                or restored.metric_definition_version
                != current.metric_definition_version
                or not oldest <= restored.observed_at <= newest
                or restored.observed_at >= current.observed_at
                or _canonical_json(
                    restored.to_dict()["query_parameters"]
                )
                != query_parameters_json
            ):
                continue
            candidates.append(restored)

        if not candidates:
            return None
        target_seconds = target_hours * 60 * 60
        candidates.sort(key=lambda candidate: candidate.observation_id)
        candidates.sort(key=lambda candidate: candidate.observed_at, reverse=True)
        candidates.sort(
            key=lambda candidate: abs(
                (current.observed_at - candidate.observed_at).total_seconds()
                - target_seconds
            )
        )
        return candidates[0]

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
        row = self._fetchone(
            """
            SELECT canonical_json FROM scores
            WHERE run_id = ? AND opportunity_id = ?
            """,
            (run_id, opportunity_id),
        )
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
        row = self._fetchone(
            """
            SELECT canonical_json FROM publications
            WHERE run_id = ? AND phase = ?
            """,
            (run_id, phase),
        )
        if row is None:
            return None
        restored = _restore(row[0], Publication)
        assert isinstance(restored, Publication)
        return restored

    def _write(self, statement: str, parameters: Sequence[object]) -> None:
        if self._in_transaction:
            self._execute(statement, parameters)
            return
        with self.transaction():
            self._execute(statement, parameters)

    def _immutable_write(self, operation: Callable[[], bool]) -> bool:
        if self._in_transaction:
            return operation()
        with self.transaction():
            return operation()

    @staticmethod
    def _validate_history_window(
        target_hours: int,
        tolerance_hours: int,
    ) -> None:
        if (
            type(target_hours) is not int
            or target_hours <= 0
            or target_hours > _MAX_HISTORY_HOURS
        ):
            raise ValueError(
                f"target_hours must be an integer from 1 to {_MAX_HISTORY_HOURS}"
            )
        if (
            type(tolerance_hours) is not int
            or tolerance_hours < 0
            or tolerance_hours > _MAX_HISTORY_HOURS
        ):
            raise ValueError(
                "tolerance_hours must be a nonnegative bounded integer"
            )
        if target_hours <= tolerance_hours:
            raise ValueError("target_hours must be greater than tolerance_hours")

    def _execute(
        self,
        statement: str,
        parameters: Sequence[object] = (),
    ) -> sqlite3.Cursor:
        return _execute_on(
            self._require_connection(),
            statement,
            parameters,
        )

    def _fetchone(
        self,
        statement: str,
        parameters: Sequence[object] = (),
    ) -> tuple[object, ...] | None:
        return _fetchone_on(
            self._require_connection(),
            statement,
            parameters,
        )

    def _fetchall(
        self,
        statement: str,
        parameters: Sequence[object] = (),
    ) -> list[tuple[object, ...]]:
        return _fetchall_on(
            self._require_connection(),
            statement,
            parameters,
        )

    def _discard_connection(self, connection: _Connection) -> None:
        self._in_transaction = False
        if self._connection is connection:
            self._connection = None
        try:
            connection.close()
        except BaseException:
            pass

    def _require_connection(self) -> _Connection:
        if self._connection is None:
            raise PersistenceError("radar store is not initialized")
        return self._connection


__all__ = ["RadarStore"]
