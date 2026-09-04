"""SQLite persistence layer for DebridPulse.

DebridPulse is a single-process appliance. SQLite/WAL is the authoritative and
only runtime datastore; server-database failover and dialect translation were
removed in v1.0.5 because they added failure states without product benefit.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

import aiosqlite

logger = logging.getLogger("debridpulse.db")


def _default_sqlite_path() -> Path:
    configured = os.getenv("DB_PATH", "").strip()
    if configured:
        return Path(configured)
    current = Path("/app/data/debridpulse.db")
    legacy = Path("/app/data/alldebrid.db")
    if legacy.exists() and not current.exists():
        logger.warning("Using legacy SQLite path %s; set DB_PATH=%s to migrate explicitly", legacy, current)
        return legacy
    return current


DB_PATH = _default_sqlite_path()


class DatabaseMaintenanceActive(RuntimeError):
    """Raised when a non-maintenance task attempts DB access during maintenance."""


class DatabaseMaintenanceGate:
    """Exclusive destructive-maintenance gate for SQLite sessions.

    Maintenance flips admission closed before waiting for existing get_db()
    sessions to drain. New sessions from other tasks fail immediately instead
    of waiting and later replaying stale pre-wipe work after the database has
    been cleared. The maintenance owner itself may open DB sessions for the
    verified backup and wipe transaction.
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active_sessions = 0
        self._maintenance_active = False
        self._owner: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self._maintenance_active

    @asynccontextmanager
    async def session(self):
        current = asyncio.current_task()
        counted = False
        async with self._condition:
            if self._maintenance_active and current is not self._owner:
                raise DatabaseMaintenanceActive("Database maintenance is in progress")
            if current is not self._owner:
                self._active_sessions += 1
                counted = True
        try:
            yield
        finally:
            if counted:
                async with self._condition:
                    self._active_sessions = max(0, self._active_sessions - 1)
                    if self._active_sessions == 0:
                        self._condition.notify_all()

    @asynccontextmanager
    async def maintenance(self):
        current = asyncio.current_task()
        claimed = False
        try:
            async with self._condition:
                if self._maintenance_active:
                    raise DatabaseMaintenanceActive("Database maintenance is already in progress")
                self._maintenance_active = True
                self._owner = current
                claimed = True
                while self._active_sessions:
                    await self._condition.wait()
            yield
        finally:
            if claimed:
                async with self._condition:
                    if self._owner is current:
                        self._owner = None
                        self._maintenance_active = False
                        self._condition.notify_all()


database_maintenance_gate = DatabaseMaintenanceGate()


def database_maintenance():
    return database_maintenance_gate.maintenance()


class _CursorWrapper:
    def __init__(self, cursor):
        self._cursor = cursor

    async def fetchall(self):
        rows = await self._cursor.fetchall()
        return [dict(r) for r in rows]

    async def fetchone(self):
        row = await self._cursor.fetchone()
        return dict(row) if row else None

    @property
    def rowcount(self):
        return getattr(self._cursor, "rowcount", -1)


class _DbConnection:
    """Small SQLite API used by the repository and legacy materialization engine."""
    backend = "sqlite"

    def __init__(self, raw: aiosqlite.Connection):
        self._raw = raw

    async def execute(self, sql: str, params: Sequence[Any] = ()):
        return _CursorWrapper(await self._raw.execute(sql, params))

    async def executemany(self, sql: str, params_list: List[Sequence[Any]]):
        if params_list:
            await self._raw.executemany(sql, params_list)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        self._raw.row_factory = aiosqlite.Row
        cur = await self._raw.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        self._raw.row_factory = aiosqlite.Row
        cur = await self._raw.execute(sql, params)
        row = await cur.fetchone()
        return dict(row) if row else None

    async def execute_returning_id(self, sql: str, params: tuple = ()) -> Optional[int]:
        cur = await self._raw.execute(sql, params)
        return cur.lastrowid

    async def commit(self):
        await self._raw.commit()

    async def rollback(self):
        await self._raw.rollback()


async def _configure_sqlite_connection(conn: aiosqlite.Connection) -> None:
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA busy_timeout=10000")
    await conn.execute("PRAGMA temp_store=MEMORY")
    await conn.execute("PRAGMA cache_size=-65536")
    await conn.execute("PRAGMA mmap_size=268435456")
    await conn.execute("PRAGMA foreign_keys=ON")


_db_metrics: Dict[str, float] = {"sqlite_acquires": 0, "wait_seconds": 0.0}


def db_runtime_metrics() -> Dict[str, Any]:
    total = int(_db_metrics["sqlite_acquires"])
    return {
        "sqlite_acquires": total,
        "total_acquires": total,
        "wait_seconds": round(float(_db_metrics["wait_seconds"]), 6),
        "average_wait_ms": round((_db_metrics["wait_seconds"] / total) * 1000.0, 3) if total else 0.0,
    }


@asynccontextmanager
async def get_db() -> AsyncIterator[_DbConnection]:
    async with database_maintenance_gate.session():
        started = time.monotonic()
        async with aiosqlite.connect(DB_PATH, timeout=30) as conn:
            await _configure_sqlite_connection(conn)
            _db_metrics["sqlite_acquires"] += 1
            _db_metrics["wait_seconds"] += max(0.0, time.monotonic() - started)
            yield _DbConnection(conn)


async def close_db_runtime() -> None:
    return None


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, definition: str):
    """Ensure one required runtime column exists or fail startup explicitly."""
    try:
        cur = await db.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cur.fetchall()}
        if column not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            await db.commit()
            logger.debug("Added column %s.%s (%s)", table, column, definition)
    except Exception as exc:
        logger.error("Required schema migration failed for %s.%s: %s", table, column, exc)
        raise RuntimeError(
            f"Required schema migration failed for {table}.{column}"
        ) from exc


_SCHEMA_COLUMNS_TORRENTS = [
    ("provider_status", "TEXT"),
    ("provider_status_code", "INTEGER"),
    ("polling_failures", "INTEGER DEFAULT 0"),
    ("download_client", "TEXT DEFAULT 'aria2'"),
    ("label", "TEXT DEFAULT ''"),
    ("priority", "INTEGER DEFAULT 0"),
    ("upload_retry_count", "INTEGER DEFAULT 0"),
    ("extraction_status", "TEXT DEFAULT ''"),
    ("extraction_error", "TEXT"),
]

_SCHEMA_COLUMNS_FILES = [
    ("source_url", "TEXT"),
    ("download_id", "TEXT"),
    ("download_client", "TEXT DEFAULT 'aria2'"),
    ("retry_count", "INTEGER DEFAULT 0"),
    ("mirror_group_id", "INTEGER"),
    ("mirror_state", "TEXT DEFAULT ''"),
    ("updated_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
]

RUNTIME_STATE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS integration_runtime_state (
        integration_id TEXT NOT NULL,
        state_key TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        payload BLOB NOT NULL,
        observed_at REAL NOT NULL,
        stale_after REAL,
        successful_at REAL NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        generation INTEGER NOT NULL CHECK(generation > 0),
        PRIMARY KEY(integration_id, state_key)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_integration_runtime_state_updated ON integration_runtime_state(integration_id, updated_at)",
)

INPUT_CHALLENGE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS transfer_input_challenges (
        transfer_id INTEGER PRIMARY KEY REFERENCES torrents(id),
        challenge_id TEXT NOT NULL UNIQUE,
        generation INTEGER NOT NULL CHECK(generation > 0),
        reason TEXT NOT NULL,
        origin TEXT NOT NULL,
        integration_id TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        request_id TEXT,
        artifact_id INTEGER,
        methods TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_transfer_input_challenge_id ON transfer_input_challenges(challenge_id)",
)

_INPUT_CHALLENGE_COLUMNS = {
    "transfer_id", "challenge_id", "generation", "reason", "origin", "integration_id",
    "operation_id", "request_id", "artifact_id", "methods", "created_at", "updated_at",
}

_RUNTIME_STATE_COLUMNS = {
    "integration_id",
    "state_key",
    "schema_version",
    "payload",
    "observed_at",
    "stale_after",
    "successful_at",
    "created_at",
    "updated_at",
    "generation",
}


TRANSFER_REPOSITORY_SCHEMA = ('CREATE TABLE IF NOT EXISTS application_events (\n'
 '        id INTEGER PRIMARY KEY, transfer_id INTEGER NOT NULL REFERENCES torrents(id),\n'
 '        kind TEXT NOT NULL, detail TEXT, claimed INTEGER NOT NULL DEFAULT 0,\n'
 '        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)',
 'CREATE TABLE IF NOT EXISTS postprocess_attempts (\n'
 '        transfer_id INTEGER NOT NULL REFERENCES torrents(id), processor_id TEXT NOT NULL,\n'
 "        state TEXT NOT NULL DEFAULT 'pending', paths TEXT NOT NULL, outcome TEXT,\n"
 '        PRIMARY KEY(transfer_id,processor_id))',
 'CREATE TABLE IF NOT EXISTS transfer_controls(key TEXT PRIMARY KEY,value TEXT NOT NULL)',
 'CREATE TABLE IF NOT EXISTS transfer_requests (\n'
 '        id TEXT PRIMARY KEY, transfer_id INTEGER NOT NULL REFERENCES torrents(id),\n'
 '        parent_id TEXT REFERENCES transfer_requests(id), ordinal INTEGER NOT NULL DEFAULT 0,\n'
 "        payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', resource TEXT,\n"
 '        attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,\n'
 '        error TEXT, UNIQUE(transfer_id,parent_id,ordinal))',
 'CREATE TABLE IF NOT EXISTS provider_resources (\n'
 '        id TEXT PRIMARY KEY, transfer_id INTEGER NOT NULL REFERENCES torrents(id),\n'
 '        provider_id TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,\n'
 '        cleanup_authority TEXT, cleanup_error TEXT, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)',
 'CREATE TABLE IF NOT EXISTS resolution_attempts (\n'
 '        id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES transfer_requests(id),\n'
 '        provider_id TEXT NOT NULL, state TEXT NOT NULL, error TEXT,\n'
 '        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)',
 'CREATE TABLE IF NOT EXISTS execution_attempts (\n'
 '        id TEXT PRIMARY KEY, transfer_id INTEGER NOT NULL REFERENCES torrents(id),\n'
 '        artifact_id INTEGER NOT NULL REFERENCES download_files(id),\n'
 '        executor_id TEXT NOT NULL, handle TEXT NOT NULL, state TEXT NOT NULL,\n'
 '        authorized INTEGER NOT NULL DEFAULT 1, progress TEXT, error TEXT,\n'
 '        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)',
 'CREATE TABLE IF NOT EXISTS route_attempt_provenance (\n'
 '        resolution_attempt_id TEXT PRIMARY KEY REFERENCES resolution_attempts(id),\n'
 '        transfer_id INTEGER NOT NULL REFERENCES torrents(id),\n'
 '        request_id TEXT NOT NULL REFERENCES transfer_requests(id),\n'
 '        ordinal INTEGER NOT NULL CHECK(ordinal > 0), operation TEXT NOT NULL,\n'
 '        previous_attempt_id TEXT REFERENCES resolution_attempts(id),\n'
 "        transition_kind TEXT, transition_reason TEXT, candidate_summary TEXT NOT NULL DEFAULT '[]',\n"
 "        outcome TEXT NOT NULL DEFAULT 'started', history_quality TEXT NOT NULL DEFAULT 'recorded',\n"
 '        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,\n'
 '        UNIQUE(transfer_id,ordinal))',
 'CREATE TABLE IF NOT EXISTS execution_attempt_provenance (\n'
 '        execution_attempt_id TEXT PRIMARY KEY REFERENCES execution_attempts(id),\n'
 '        route_attempt_id TEXT REFERENCES resolution_attempts(id),\n'
 '        transfer_id INTEGER NOT NULL REFERENCES torrents(id),\n'
 '        artifact_id INTEGER NOT NULL REFERENCES download_files(id),\n'
 '        ordinal INTEGER NOT NULL CHECK(ordinal > 0), provider_id TEXT, candidate_id TEXT, candidate_source '
 'TEXT,\n'
 "        outcome TEXT NOT NULL DEFAULT 'prepared', delivered INTEGER NOT NULL DEFAULT 0,\n"
 "        history_quality TEXT NOT NULL DEFAULT 'recorded',\n"
 '        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,\n'
 '        UNIQUE(artifact_id,ordinal))',
 'CREATE INDEX IF NOT EXISTS idx_route_provenance_transfer ON '
 'route_attempt_provenance(transfer_id,request_id,ordinal)',
 'CREATE INDEX IF NOT EXISTS idx_execution_provenance_transfer ON '
 'execution_attempt_provenance(transfer_id,artifact_id,ordinal)',
 'CREATE INDEX IF NOT EXISTS idx_execution_provenance_route ON '
 'execution_attempt_provenance(route_attempt_id)',
 'CREATE TABLE IF NOT EXISTS transfer_outcomes (\n'
 '        id INTEGER PRIMARY KEY, transfer_id INTEGER NOT NULL REFERENCES torrents(id),\n'
 '        attempt_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL,\n'
 '        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)',
 'CREATE INDEX IF NOT EXISTS idx_requests_ready ON transfer_requests(state,retry_at,transfer_id)',
 'CREATE INDEX IF NOT EXISTS idx_attempts_artifact ON execution_attempts(artifact_id,state)',
 'CREATE INDEX IF NOT EXISTS idx_resources_transfer ON provider_resources(transfer_id,provider_id)')

TRANSFER_REPOSITORY_COLUMNS = {'torrents': {'normalized_error': 'TEXT',
              'lifecycle_epoch': 'INTEGER NOT NULL DEFAULT 0',
              'delete_remote': 'INTEGER NOT NULL DEFAULT 0'},
 'transfer_requests': {'metadata': 'TEXT'},
 'provider_resources': {'cleanup_attempts': 'INTEGER NOT NULL DEFAULT 0',
                        'cleanup_retry_at': 'REAL NOT NULL DEFAULT 0',
                        'cleanup_blocked': 'INTEGER NOT NULL DEFAULT 0'},
 'resolution_attempts': {'result': 'TEXT'},
 'execution_attempts': {'candidate': 'TEXT',
                        'progress_at': 'REAL',
                        'cleanup_state': 'TEXT',
                        'cleanup_attempts': 'INTEGER NOT NULL DEFAULT 0',
                        'cleanup_retry_at': 'REAL NOT NULL DEFAULT 0',
                        'cleanup_error': 'TEXT'},
 'download_files': {'request_id': 'TEXT',
                    'candidates': 'TEXT',
                    'selected_candidate': 'INTEGER NOT NULL DEFAULT 0',
                    'execution_attempt_id': 'TEXT',
                    'normalized_error': 'TEXT',
                    'retry_at': 'REAL NOT NULL DEFAULT 0',
                    'recovery_failures': 'INTEGER NOT NULL DEFAULT 0',
                    'recovery_refreshes': 'INTEGER NOT NULL DEFAULT 0'}}

_TRANSFER_REPOSITORY_REQUIRED_COLUMNS = {'application_events': {'id', 'created_at', 'claimed', 'transfer_id', 'detail', 'kind'},
 'download_files': {'candidates',
                    'execution_attempt_id',
                    'normalized_error',
                    'request_id',
                    'retry_at',
                    'selected_candidate',
                    'recovery_failures',
                    'recovery_refreshes'},
 'execution_attempt_provenance': {'artifact_id',
                                  'candidate_id',
                                  'candidate_source',
                                  'created_at',
                                  'delivered',
                                  'execution_attempt_id',
                                  'history_quality',
                                  'ordinal',
                                  'outcome',
                                  'provider_id',
                                  'route_attempt_id',
                                  'transfer_id',
                                  'updated_at'},
 'execution_attempts': {'artifact_id',
                        'authorized',
                        'candidate',
                        'cleanup_attempts',
                        'cleanup_error',
                        'cleanup_retry_at',
                        'cleanup_state',
                        'created_at',
                        'error',
                        'executor_id',
                        'handle',
                        'id',
                        'progress',
                        'progress_at',
                        'state',
                        'transfer_id',
                        'updated_at'},
 'postprocess_attempts': {'processor_id', 'paths', 'state', 'transfer_id', 'outcome'},
 'provider_resources': {'cleanup_attempts',
                        'cleanup_authority',
                        'cleanup_blocked',
                        'cleanup_error',
                        'cleanup_retry_at',
                        'id',
                        'payload',
                        'provider_id',
                        'state',
                        'transfer_id',
                        'updated_at'},
 'resolution_attempts': {'created_at',
                         'error',
                         'id',
                         'provider_id',
                         'request_id',
                         'result',
                         'state',
                         'updated_at'},
 'route_attempt_provenance': {'candidate_summary',
                              'created_at',
                              'history_quality',
                              'operation',
                              'ordinal',
                              'outcome',
                              'previous_attempt_id',
                              'request_id',
                              'resolution_attempt_id',
                              'transfer_id',
                              'transition_kind',
                              'transition_reason',
                              'updated_at'},
 'torrents': {'normalized_error', 'lifecycle_epoch', 'delete_remote'},
 'transfer_controls': {'value', 'key'},
 'transfer_outcomes': {'id', 'attempt_id', 'created_at', 'payload', 'transfer_id', 'kind'},
 'transfer_requests': {'attempts',
                       'error',
                       'id',
                       'metadata',
                       'ordinal',
                       'parent_id',
                       'payload',
                       'resource',
                       'retry_at',
                       'state',
                       'transfer_id'}}


async def _validate_schema_readonly(required: dict[str, set[str]], *, owner: str) -> None:
    path = Path(DB_PATH)
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"{owner} schema is unavailable; database bootstrap must run first")
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        async with aiosqlite.connect(uri, uri=True) as db:
            check = await (await db.execute("PRAGMA quick_check")).fetchone()
            if not check or check[0] != "ok":
                raise RuntimeError(f"{owner} schema failed SQLite integrity verification")
            missing_by_table: dict[str, list[str]] = {}
            for table, expected in required.items():
                rows = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
                present = {row[1] for row in rows}
                missing = sorted(expected - present)
                if not rows or missing:
                    missing_by_table[table] = missing or ["<table>"]
            if missing_by_table:
                raise RuntimeError(f"{owner} schema is incomplete: {missing_by_table}")
    except aiosqlite.Error as exc:
        raise RuntimeError(f"{owner} schema could not be verified read-only") from exc


async def validate_transfer_repository_schema() -> None:
    await _validate_schema_readonly(_TRANSFER_REPOSITORY_REQUIRED_COLUMNS, owner="transfer repository")


async def validate_runtime_state_schema() -> None:
    await _validate_schema_readonly({"integration_runtime_state": _RUNTIME_STATE_COLUMNS}, owner="integration runtime state")


async def init_db():
    await _init_db_sqlite()


async def _init_db_sqlite():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await _configure_sqlite_connection(db)
        await db.commit()
        await db.execute("""
            CREATE TABLE IF NOT EXISTS torrents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hash TEXT UNIQUE NOT NULL,
                name TEXT,
                magnet TEXT,
                status TEXT DEFAULT 'pending',
                alldebrid_id TEXT,
                size_bytes INTEGER DEFAULT 0,
                progress REAL DEFAULT 0,
                download_url TEXT,
                local_path TEXT,
                source TEXT DEFAULT '',
                provider_status TEXT,
                provider_status_code INTEGER,
                polling_failures INTEGER DEFAULT 0,
                download_client TEXT DEFAULT 'aria2',
                label TEXT DEFAULT '',
                priority INTEGER DEFAULT 0,
                error_message TEXT,
                extraction_status TEXT DEFAULT '',
                extraction_error TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS download_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torrent_id INTEGER,
                filename TEXT,
                size_bytes INTEGER,
                source_url TEXT,
                download_url TEXT,
                local_path TEXT,
                status TEXT DEFAULT 'pending',
                download_id TEXT,
                download_client TEXT DEFAULT 'aria2',
                blocked INTEGER DEFAULT 0,
                block_reason TEXT,
                retry_count INTEGER DEFAULT 0,
                recovery_failures INTEGER NOT NULL DEFAULT 0,
                recovery_refreshes INTEGER NOT NULL DEFAULT 0,
                mirror_group_id INTEGER,
                mirror_state TEXT DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (torrent_id) REFERENCES torrents(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torrent_id INTEGER,
                level TEXT DEFAULT 'info',
                message TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (torrent_id) REFERENCES torrents(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stats_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_json TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transfer_pause_intents (
                torrent_id INTEGER PRIMARY KEY,
                paused INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS deferred_provider_submissions (
                torrent_id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                payload BLOB NOT NULL,
                filename TEXT,
                source TEXT DEFAULT 'manual',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (torrent_id) REFERENCES torrents(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS debridpulse_aria2_owned_gids (
                gid TEXT PRIMARY KEY,
                download_file_id INTEGER,
                torrent_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(RUNTIME_STATE_SCHEMA[0])
        await db.execute(INPUT_CHALLENGE_SCHEMA[0])
        for statement in TRANSFER_REPOSITORY_SCHEMA:
            await db.execute(statement)
        for col, defn in _SCHEMA_COLUMNS_TORRENTS:
            await _ensure_column(db, "torrents", col, defn)
        for col, defn in _SCHEMA_COLUMNS_FILES:
            await _ensure_column(db, "download_files", col, defn)
        for table, definitions in TRANSFER_REPOSITORY_COLUMNS.items():
            for column, definition in definitions.items():
                await _ensure_column(db, table, column, definition)
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_request ON download_files(request_id) WHERE request_id IS NOT NULL")
        await db.commit()

    async with aiosqlite.connect(DB_PATH) as idx_db:
        for ddl in [
            "CREATE INDEX IF NOT EXISTS idx_dlfiles_torrent_status ON download_files (torrent_id, status, blocked)",
            "CREATE INDEX IF NOT EXISTS idx_dlfiles_queue ON download_files (status, download_client, blocked, torrent_id, id)",
            "CREATE INDEX IF NOT EXISTS idx_dlfiles_download_id ON download_files (download_id)",
            "CREATE INDEX IF NOT EXISTS idx_dlfiles_mirror_group ON download_files (torrent_id, mirror_group_id, mirror_state, status)",
            "CREATE INDEX IF NOT EXISTS idx_torrents_alldebrid_id ON torrents (alldebrid_id)",
            "CREATE INDEX IF NOT EXISTS idx_torrents_status ON torrents (status)",
            "CREATE INDEX IF NOT EXISTS idx_torrents_status_alldebrid ON torrents (status, alldebrid_id)",
            "CREATE INDEX IF NOT EXISTS idx_torrents_status_updated ON torrents (status, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_torrents_status_priority ON torrents (status, priority DESC, id ASC)",
            "CREATE INDEX IF NOT EXISTS idx_torrents_completed_at ON torrents (completed_at)",
            "CREATE INDEX IF NOT EXISTS idx_torrents_priority ON torrents (priority DESC, id ASC)",
            "CREATE INDEX IF NOT EXISTS idx_torrents_hash ON torrents (hash)",
            "CREATE INDEX IF NOT EXISTS idx_torrents_created_at ON torrents (created_at)",
            "CREATE INDEX IF NOT EXISTS idx_dlfiles_local_path ON download_files (local_path)",
            "CREATE INDEX IF NOT EXISTS idx_events_torrent_id ON events (torrent_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_created_at ON events (created_at)",
            RUNTIME_STATE_SCHEMA[1],
            INPUT_CHALLENGE_SCHEMA[1],
        ]:
            await idx_db.execute(ddl)
        await idx_db.commit()
    logger.debug("SQLite indexes ensured")

    async with aiosqlite.connect(DB_PATH) as verify_db:
        required = {
            "torrents": {"id", "hash", "status"} | {name for name, _ in _SCHEMA_COLUMNS_TORRENTS},
            "download_files": {"id", "torrent_id", "status", "blocked"}
            | {name for name, _ in _SCHEMA_COLUMNS_FILES},
            "integration_runtime_state": _RUNTIME_STATE_COLUMNS,
            "transfer_input_challenges": _INPUT_CHALLENGE_COLUMNS,
        }
        for table, expected in _TRANSFER_REPOSITORY_REQUIRED_COLUMNS.items():
            required.setdefault(table, set()).update(expected)
        missing_by_table: dict[str, list[str]] = {}
        for table, expected in required.items():
            cur = await verify_db.execute(f"PRAGMA table_info({table})")
            cols = {row[1] for row in await cur.fetchall()}
            missing = sorted(expected - cols)
            if missing:
                missing_by_table[table] = missing
        if missing_by_table:
            logger.error("CRITICAL: required schema remains incomplete: %s", missing_by_table)
            raise RuntimeError(f"Required SQLite schema is incomplete: {missing_by_table}")
        logger.info("SQLite schema verified — all required runtime columns present")
    logger.info("SQLite database initialised: %s", DB_PATH)
