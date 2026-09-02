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
        for col, defn in _SCHEMA_COLUMNS_TORRENTS:
            await _ensure_column(db, "torrents", col, defn)
        for col, defn in _SCHEMA_COLUMNS_FILES:
            await _ensure_column(db, "download_files", col, defn)
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
        }
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

    _STATUS_REPR_MAP = {
        "TorrentStatus.PROCESSING": "processing",
        "TorrentStatus.UPLOADING": "uploading",
        "TorrentStatus.READY": "ready",
        "TorrentStatus.ERROR": "error",
        "TorrentStatus.COMPLETED": "completed",
        "TorrentStatus.DELETED": "deleted",
        "TorrentStatus.QUEUED": "queued",
        "TorrentStatus.DOWNLOADING": "downloading",
        "TorrentStatus.PENDING": "pending",
        "TorrentStatus.PAUSED": "paused",
    }
    async with aiosqlite.connect(DB_PATH) as fix_db:
        for bad_val, good_val in _STATUS_REPR_MAP.items():
            cur = await fix_db.execute(
                "SELECT COUNT(*) FROM torrents WHERE status = ?",
                (bad_val,),
            )
            (count,) = await cur.fetchone()
            if count:
                logger.warning(
                    "Repairing %d torrent(s) with corrupted status %r → %r",
                    count,
                    bad_val,
                    good_val,
                )
                await fix_db.execute(
                    "UPDATE torrents SET status = ? WHERE status = ?",
                    (good_val, bad_val),
                )
        await fix_db.commit()
