"""SQLite persistence layer for DebridPulse.

DebridPulse is a single-process appliance. SQLite/WAL is the authoritative and
only runtime datastore; server-database failover and dialect translation were
removed in v1.0.5 because they added failure states without product benefit.
"""
from __future__ import annotations

import asyncio
import json
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


async def _retire_and_backfill_source_fingerprints(db: aiosqlite.Connection) -> None:
    """Idempotent additive-schema step for the deleted-transfer dedupe correction.

    ``torrents.source_fingerprint`` records the original logical source identity.
    After Delete, the active unique ``hash`` key is retired to a deterministic,
    transfer-specific tombstone (``deleted:<id>:<source_fingerprint>``) so the same
    source can be re-submitted as a genuinely fresh transfer without destroying
    historical identity. This bootstrap step brings existing 1.0.12 databases to
    that model:

    * every non-deleted row keeps its active ``hash`` and gains
      ``source_fingerprint = hash``;
    * every already-deleted legacy row still holding an un-retired active key
      preserves the original fingerprint and has its ``hash`` retired to the
      tombstone form;
    * a deleted row already tombstoned but missing ``source_fingerprint`` (a
      partially-applied state) has it recovered from the tombstone.

    Each statement is guarded so repeated initialization is a no-op and the
    tombstone is never recursively prefixed. Restoring an untouched pre-migration
    copy returns fully to the pre-migration state because the live database is the
    only thing mutated and no row data is discarded (the original ``hash`` of a
    retired row is preserved verbatim in ``source_fingerprint``).
    """
    try:
        cur = await db.execute("PRAGMA table_info(torrents)")
        columns = {row[1] for row in await cur.fetchall()}
        if "source_fingerprint" not in columns:
            return
        await db.execute(
            "UPDATE torrents SET source_fingerprint = hash "
            "WHERE source_fingerprint IS NULL AND status != 'deleted'"
        )
        await db.execute(
            "UPDATE torrents "
            "SET source_fingerprint = hash, "
            "    hash = 'deleted:' || id || ':' || hash "
            "WHERE status = 'deleted' AND source_fingerprint IS NULL "
            "  AND hash NOT LIKE 'deleted:%'"
        )
        await db.execute(
            "UPDATE torrents "
            "SET source_fingerprint = substr(hash, length('deleted:' || id || ':') + 1) "
            "WHERE status = 'deleted' AND source_fingerprint IS NULL "
            "  AND hash LIKE 'deleted:' || id || ':%'"
        )
        await db.commit()
    except Exception as exc:  # pragma: no cover - defensive startup guard
        logger.error("source_fingerprint backfill failed: %s", exc)
        raise RuntimeError("source_fingerprint backfill failed") from exc


async def _backfill_provider_resource_bindings(db: aiosqlite.Connection) -> None:
    """Idempotent additive-schema step for the deleted-transfer generation model.

    ``provider_resources.id`` is the durable (transfer, canonical-resource)
    binding-generation identity; ``provider_resources.resource_key`` is the
    canonical, transfer-independent DP resource identity (== ``ProviderResource.id``).
    Existing rows predate the split: their primary key IS the canonical id, so
    ``resource_key`` is backfilled from ``id`` and the historical primary key is
    left untouched. New bindings are created with
    ``id = UUIDv5("transfer-provider-resource:<transfer_id>:<resource_key>")`` and
    coexist with any historical row for the same canonical resource on another
    transfer. The unique index enforces one binding per (transfer, canonical
    resource). Repeated initialization is a no-op.
    """
    try:
        cur = await db.execute("PRAGMA table_info(provider_resources)")
        columns = {row[1] for row in await cur.fetchall()}
        if "resource_key" not in columns:
            return
        await db.execute(
            "UPDATE provider_resources SET resource_key = id WHERE resource_key IS NULL"
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_resources_binding "
            "ON provider_resources(transfer_id, resource_key)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_resources_key "
            "ON provider_resources(resource_key)"
        )
        await db.commit()
    except Exception as exc:  # pragma: no cover - defensive startup guard
        logger.error("provider_resources binding backfill failed: %s", exc)
        raise RuntimeError("provider_resources binding backfill failed") from exc


async def _normalize_legacy_cleanup_claims(db: aiosqlite.Connection) -> None:
    """Idempotent upgrade step for the boolean-claim -> leased-claim model.

    Before the lease model a provider-cleanup claim was the boolean
    ``cleanup_blocked=1``. It was set on claim, never cleared on success/skip, and
    set again as a "terminal marker", so upgraded databases can hold (a) genuinely
    stranded claims that no owner will ever finalize, (b) misleading markers on
    completed/absent/abandoned rows. Neither carries a lease token, so under the
    lease model none is a current claim: the ordinary cleanup cadence claims any
    still-owed row on its next pass, and terminal rows simply stop being
    enumerated. This step only retires the misleading marker itself; it never
    touches ``cleanup_claim_token``/``cleanup_claim_until``, so an active new-format
    lease is never stolen. Repeated initialization is a no-op.
    """
    try:
        cur = await db.execute("PRAGMA table_info(provider_resources)")
        columns = {row[1] for row in await cur.fetchall()}
        if "cleanup_blocked" not in columns:
            return
        await db.execute("UPDATE provider_resources SET cleanup_blocked=0 WHERE cleanup_blocked!=0")
        await db.commit()
    except Exception as exc:  # pragma: no cover - defensive startup guard
        logger.error("legacy cleanup-claim normalization failed: %s", exc)
        raise RuntimeError("legacy cleanup-claim normalization failed") from exc


async def _migrate_recovery_state_from_events(db: aiosqlite.Connection) -> None:
    """Idempotent additive backfill for DP 1.0.12 recovery leveling, Section 19.

    Before this leveling pass, "current" recovery state was reconstructed at
    read time from the latest ``application_events`` row of kind
    ``transfer_recovery:<artifact_id>`` (see the historical
    ``transfers.repository.TransferRepository._recovery_snapshot``). This
    step performs that exact reconstruction ONE TIME per artifact and writes
    the result forward into the new canonical ``artifact_recovery_state``
    current-state row, using the same "known column facts win" rule the old
    read path used for ``recovery_failures``/``recovery_refreshes``.

    * Only artifacts that do not already have an ``artifact_recovery_state``
      row are considered, so this is a no-op once an artifact has been
      migrated or has otherwise acquired current state the ordinary way.
    * An artifact with no historical snapshot event is left with no row at
      all -- exactly matching the pre-leveling behavior where an absent event
      seeded an all-defaults snapshot rather than fabricating history.
    * No ``application_events`` row is ever modified or deleted by this step;
      the historical snapshot events remain, byte-identical, as durable
      audit trivia. A database restored from a backup taken before this
      migration ran is therefore read identically by the pre-leveling
      reconstruction logic, whether or not this migration ever executed
      against the live copy in between.
    * Safe to run repeatedly and safe against a partially-upgraded database
      (some artifacts already migrated, others not).
    """
    try:
        cur = await db.execute("PRAGMA table_info(artifact_recovery_state)")
        if not await cur.fetchall():
            return
        candidates = await (await db.execute(
            """SELECT f.id AS artifact_id, f.torrent_id AS transfer_id,
                      f.recovery_failures, f.recovery_refreshes
               FROM download_files f
               WHERE NOT EXISTS(
                   SELECT 1 FROM artifact_recovery_state s WHERE s.artifact_id = f.id
               )"""
        )).fetchall()
        for row in candidates:
            artifact_id = int(row["artifact_id"])
            transfer_id = int(row["transfer_id"])
            event_cur = await db.execute(
                "SELECT detail FROM application_events WHERE kind=? ORDER BY id DESC LIMIT 1",
                (f"transfer_recovery:{artifact_id}",),
            )
            event = await event_cur.fetchone()
            if not event or not event["detail"]:
                continue
            try:
                stored = json.loads(event["detail"])
            except (TypeError, ValueError):
                continue
            if not isinstance(stored, dict):
                continue
            failures = max(
                int(stored.get("consecutive_no_progress_failures") or 0),
                int(row["recovery_failures"] or 0),
            )
            refreshes = max(
                int(stored.get("candidate_refreshes") or 0),
                int(row["recovery_refreshes"] or 0),
            )
            history = stored.get("candidate_attempt_history")
            history_json = json.dumps(history if isinstance(history, list) else [])
            # DP 1.0.12 recovery leveling, Section 14/15 (post-review
            # correction): the canonical current-state table holds ONLY
            # current/actionable facts -- no historical pocket of any shape.
            # A legacy snapshot's historical facts (last_applied_trigger,
            # failure_classification, durable_target, etc.) are NOT migrated
            # forward into the new sparse audit-log shape; they remain
            # exactly where they already were, unmodified, in the preserved
            # legacy application_events row this reads from. The new sparse
            # recovery_audit trail starts accumulating fresh entries only
            # from this point forward.
            await db.execute(
                """INSERT INTO artifact_recovery_state(
                    artifact_id, transfer_id, version, recovery_epoch, progress_anchor,
                    consecutive_no_progress_failures, failures_since_meaningful_progress,
                    failure_signature, same_signature_failures, candidate_refreshes,
                    candidate_switches, decision_action, decision_reason, quiescence_reason,
                    wake_condition, candidate_attempt_history, recovery_generation,
                    recovery_claim_token, recovery_claim_trigger, recovery_claim_until,
                    recovery_claim_id, recovery_decision_id, last_failure_identity,
                    last_refresh_decision_id, refresh_inflight_decision_id,
                    refresh_inflight_attempt_id, blocked_retry_at, last_applied_action,
                    last_applied_reason
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(artifact_id) DO NOTHING""",
                (
                    artifact_id, transfer_id, max(3, int(stored.get("version") or 0)),
                    int(stored.get("recovery_epoch") or 0), stored.get("progress_anchor"),
                    failures, int(stored.get("failures_since_meaningful_progress") or 0),
                    stored.get("failure_signature"), int(stored.get("same_signature_failures") or 0),
                    refreshes, int(stored.get("candidate_switches") or 0),
                    stored.get("decision_action"), stored.get("decision_reason"),
                    stored.get("quiescence_reason"), stored.get("wake_condition"), history_json,
                    int(stored.get("recovery_generation") or 0), stored.get("recovery_claim_token"),
                    stored.get("recovery_claim_trigger"), float(stored.get("recovery_claim_until") or 0),
                    stored.get("recovery_claim_id"), stored.get("recovery_decision_id"),
                    stored.get("last_failure_identity"), stored.get("last_refresh_decision_id"),
                    stored.get("refresh_inflight_decision_id"), stored.get("refresh_inflight_attempt_id"),
                    float(stored.get("blocked_retry_at") or 0), stored.get("last_applied_action"),
                    stored.get("last_applied_reason"),
                ),
            )
        await db.commit()
    except Exception as exc:  # pragma: no cover - defensive startup guard
        logger.error("recovery-state migration from application_events failed: %s", exc)
        raise RuntimeError("recovery-state migration from application_events failed") from exc


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
        facts TEXT NOT NULL DEFAULT '[]',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_transfer_input_challenge_id ON transfer_input_challenges(challenge_id)",
    # Retained schema object; no in-scope reader as of the DP 1.0.12 leveling
    # remediation (FUNC-001 removed the bounded "recovery net" read-model
    # compensation that used to SEEK this table by recency -- see
    # transfers._repository_base._retire_transfer_auxiliary_state_in_db for
    # the corrected owner: a settled transfer's row is now retired
    # transactionally at settlement instead of compensated for at read time).
    "CREATE INDEX IF NOT EXISTS idx_transfer_input_challenges_updated ON transfer_input_challenges(updated_at)",
)

_INPUT_CHALLENGE_COLUMNS = {
    "transfer_id", "challenge_id", "generation", "reason", "origin", "integration_id",
    "operation_id", "request_id", "artifact_id", "methods", "facts", "created_at", "updated_at",
}
# Non-secret challenge facts (for example an observed server identity), added
# in place to challenge tables created before they existed.
_INPUT_CHALLENGE_FACTS_DEFINITION = "TEXT NOT NULL DEFAULT '[]'"

_RUNTIME_STATE_COLUMNS = {
    "integration_id", "state_key", "schema_version", "payload", "observed_at", "stale_after",
    "successful_at", "created_at", "updated_at", "generation",
}


TRANSFER_REPOSITORY_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS application_events (
        id INTEGER PRIMARY KEY, transfer_id INTEGER NOT NULL REFERENCES torrents(id),
        kind TEXT NOT NULL, detail TEXT, claimed INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS postprocess_attempts (
        transfer_id INTEGER NOT NULL REFERENCES torrents(id), processor_id TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', paths TEXT NOT NULL, outcome TEXT,
        PRIMARY KEY(transfer_id,processor_id))""",
    "CREATE TABLE IF NOT EXISTS transfer_controls(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS transfer_requests (
        id TEXT PRIMARY KEY, transfer_id INTEGER NOT NULL REFERENCES torrents(id),
        parent_id TEXT REFERENCES transfer_requests(id), ordinal INTEGER NOT NULL DEFAULT 0,
        payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', resource TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
        error TEXT, UNIQUE(transfer_id,parent_id,ordinal))""",
    """CREATE TABLE IF NOT EXISTS provider_resources (
        id TEXT PRIMARY KEY, transfer_id INTEGER NOT NULL REFERENCES torrents(id),
        provider_id TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
        cleanup_authority TEXT, cleanup_error TEXT, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS resolution_attempts (
        id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES transfer_requests(id),
        provider_id TEXT NOT NULL, state TEXT NOT NULL, error TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS execution_attempts (
        id TEXT PRIMARY KEY, transfer_id INTEGER NOT NULL REFERENCES torrents(id),
        artifact_id INTEGER NOT NULL REFERENCES download_files(id),
        executor_id TEXT NOT NULL, handle TEXT NOT NULL, state TEXT NOT NULL,
        authorized INTEGER NOT NULL DEFAULT 1, progress TEXT, error TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS route_attempt_provenance (
        resolution_attempt_id TEXT PRIMARY KEY REFERENCES resolution_attempts(id),
        transfer_id INTEGER NOT NULL REFERENCES torrents(id),
        request_id TEXT NOT NULL REFERENCES transfer_requests(id),
        ordinal INTEGER NOT NULL CHECK(ordinal > 0), operation TEXT NOT NULL,
        previous_attempt_id TEXT REFERENCES resolution_attempts(id),
        transition_kind TEXT, transition_reason TEXT, candidate_summary TEXT NOT NULL DEFAULT '[]',
        outcome TEXT NOT NULL DEFAULT 'started', history_quality TEXT NOT NULL DEFAULT 'recorded',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(transfer_id,ordinal))""",
    """CREATE TABLE IF NOT EXISTS execution_attempt_provenance (
        execution_attempt_id TEXT PRIMARY KEY REFERENCES execution_attempts(id),
        route_attempt_id TEXT REFERENCES resolution_attempts(id),
        transfer_id INTEGER NOT NULL REFERENCES torrents(id),
        artifact_id INTEGER NOT NULL REFERENCES download_files(id),
        ordinal INTEGER NOT NULL CHECK(ordinal > 0), provider_id TEXT, candidate_id TEXT, candidate_source TEXT,
        outcome TEXT NOT NULL DEFAULT 'prepared', delivered INTEGER NOT NULL DEFAULT 0,
        history_quality TEXT NOT NULL DEFAULT 'recorded',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(artifact_id,ordinal))""",
    """CREATE TABLE IF NOT EXISTS canonical_candidate_bindings (
        id INTEGER PRIMARY KEY,
        canonical_artifact_id INTEGER NOT NULL REFERENCES download_files(id),
        candidate_id TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        source_scope TEXT,
        source_key TEXT,
        role TEXT NOT NULL CHECK(role IN ('canonical','alternate')),
        candidate_order INTEGER NOT NULL CHECK(candidate_order > 0),
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(canonical_artifact_id,candidate_id),
        UNIQUE(canonical_artifact_id,candidate_order))""",
    """CREATE TABLE IF NOT EXISTS canonical_candidate_origins (
        id INTEGER PRIMARY KEY,
        binding_id INTEGER NOT NULL REFERENCES canonical_candidate_bindings(id),
        contributing_artifact_id INTEGER NOT NULL REFERENCES download_files(id),
        contributing_transfer_id INTEGER NOT NULL REFERENCES torrents(id),
        request_id TEXT NOT NULL REFERENCES transfer_requests(id),
        resolution_attempt_id TEXT NOT NULL REFERENCES resolution_attempts(id),
        discovered_candidate_id TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(binding_id,request_id,resolution_attempt_id,discovered_candidate_id))""",
    """CREATE TABLE IF NOT EXISTS artifact_consolidations (
        contributing_artifact_id INTEGER PRIMARY KEY REFERENCES download_files(id),
        source_transfer_id INTEGER NOT NULL REFERENCES torrents(id),
        source_request_id TEXT NOT NULL UNIQUE REFERENCES transfer_requests(id),
        canonical_artifact_id INTEGER NOT NULL REFERENCES download_files(id),
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
    "CREATE INDEX IF NOT EXISTS idx_route_provenance_transfer ON route_attempt_provenance(transfer_id,request_id,ordinal)",
    "CREATE INDEX IF NOT EXISTS idx_execution_provenance_transfer ON execution_attempt_provenance(transfer_id,artifact_id,ordinal)",
    "CREATE INDEX IF NOT EXISTS idx_execution_provenance_route ON execution_attempt_provenance(route_attempt_id)",
    "CREATE INDEX IF NOT EXISTS idx_candidate_bindings_artifact ON canonical_candidate_bindings(canonical_artifact_id,candidate_order)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_bindings_source ON canonical_candidate_bindings(canonical_artifact_id,provider_id,source_scope,source_key) WHERE source_scope IS NOT NULL AND source_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_candidate_origins_request ON canonical_candidate_origins(request_id,resolution_attempt_id)",
    "CREATE INDEX IF NOT EXISTS idx_candidate_origins_transfer ON canonical_candidate_origins(contributing_transfer_id,binding_id)",
    "CREATE INDEX IF NOT EXISTS idx_artifact_consolidations_source ON artifact_consolidations(source_transfer_id,source_request_id)",
    "CREATE INDEX IF NOT EXISTS idx_artifact_consolidations_canonical ON artifact_consolidations(canonical_artifact_id)",
    # DP 1.0.12 recovery leveling, Section 14: the canonical CURRENT recovery
    # state store -- exactly one row per artifact, updated in place. Replaces
    # the prior model of reconstructing "current" state by scanning the
    # latest application_events row of kind 'transfer_recovery:<artifact_id>'
    # (an unbounded, append-only history that grew one full-snapshot row per
    # meaningful mutation, including every progress tick that advanced
    # completed bytes).
    #
    # Every column below is read somewhere to gate a fencing/traversal/dedup
    # decision, or (last_applied_action/last_applied_reason) consumed live by
    # the bounded Downloads/Dashboard projection -- verified by repository-
    # wide search before this leveling pass. Section 15: this table holds NO
    # historical/audit-only facts in ANY shape -- not as further flat
    # columns, and not as a JSON "latest value" pocket either, since a
    # mutable latest-value copy is itself a second, overwritable home for
    # data that must live solely in the append-only record. Facts that are
    # never read back for a policy decision are recorded ONLY as sparse
    # application_events rows of kind 'recovery_audit'
    # (transfers.repository.TransferRepository._append_recovery_audit) and
    # reconstructed at read time, on demand, by
    # transfers.repository.TransferRepository.recovery_context() scanning
    # that sparse trail -- never persisted back here.
    """CREATE TABLE IF NOT EXISTS artifact_recovery_state (
        artifact_id INTEGER PRIMARY KEY REFERENCES download_files(id),
        transfer_id INTEGER NOT NULL REFERENCES torrents(id),
        version INTEGER NOT NULL DEFAULT 3,
        recovery_epoch INTEGER NOT NULL DEFAULT 0,
        progress_anchor INTEGER,
        consecutive_no_progress_failures INTEGER NOT NULL DEFAULT 0,
        failures_since_meaningful_progress INTEGER NOT NULL DEFAULT 0,
        failure_signature TEXT,
        same_signature_failures INTEGER NOT NULL DEFAULT 0,
        candidate_refreshes INTEGER NOT NULL DEFAULT 0,
        candidate_switches INTEGER NOT NULL DEFAULT 0,
        decision_action TEXT,
        decision_reason TEXT,
        quiescence_reason TEXT,
        wake_condition TEXT,
        candidate_attempt_history TEXT NOT NULL DEFAULT '[]',
        recovery_generation INTEGER NOT NULL DEFAULT 0,
        recovery_claim_token TEXT,
        recovery_claim_trigger TEXT,
        recovery_claim_until REAL NOT NULL DEFAULT 0,
        recovery_claim_id TEXT,
        recovery_decision_id TEXT,
        last_failure_identity TEXT,
        last_refresh_decision_id TEXT,
        refresh_inflight_decision_id TEXT,
        refresh_inflight_attempt_id TEXT,
        blocked_retry_at REAL NOT NULL DEFAULT 0,
        last_applied_action TEXT,
        last_applied_reason TEXT,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
    "CREATE INDEX IF NOT EXISTS idx_artifact_recovery_state_transfer ON artifact_recovery_state(transfer_id)",
    """CREATE TRIGGER IF NOT EXISTS trg_execution_provenance_candidate_route
        AFTER INSERT ON execution_attempt_provenance
        WHEN NEW.route_attempt_id IS NULL AND NEW.candidate_id IS NOT NULL
        BEGIN
            UPDATE execution_attempt_provenance
            SET route_attempt_id=(
                    SELECT o.resolution_attempt_id
                    FROM canonical_candidate_bindings b
                    JOIN canonical_candidate_origins o ON o.binding_id=b.id
                    WHERE b.canonical_artifact_id=NEW.artifact_id AND b.candidate_id=NEW.candidate_id
                    ORDER BY CASE WHEN o.discovered_candidate_id=b.candidate_id THEN 0 ELSE 1 END,o.id
                    LIMIT 1
                ),
                history_quality=CASE WHEN EXISTS(
                    SELECT 1 FROM canonical_candidate_bindings b
                    JOIN canonical_candidate_origins o ON o.binding_id=b.id
                    WHERE b.canonical_artifact_id=NEW.artifact_id AND b.candidate_id=NEW.candidate_id
                ) THEN 'recorded' ELSE history_quality END
            WHERE execution_attempt_id=NEW.execution_attempt_id;
        END""",
    """CREATE TABLE IF NOT EXISTS transfer_outcomes (
        id INTEGER PRIMARY KEY, transfer_id INTEGER NOT NULL REFERENCES torrents(id),
        attempt_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
    "CREATE INDEX IF NOT EXISTS idx_requests_ready ON transfer_requests(state,retry_at,transfer_id)",
    "CREATE INDEX IF NOT EXISTS idx_attempts_artifact ON execution_attempts(artifact_id,state)",
    "CREATE INDEX IF NOT EXISTS idx_resources_transfer ON provider_resources(transfer_id,provider_id)",
    # Universal file-selection manifest overlay (additive 1.0.12 current schema).
    # A capable provider exposes a neutral file tree before core commits the
    # executable manifest; these tables hold the observed manifest, its neutral
    # identity, and the durable ALL/EXPLICIT selection decision + provenance.
    """CREATE TABLE IF NOT EXISTS transfer_file_manifests (
        id TEXT PRIMARY KEY,
        transfer_id INTEGER NOT NULL REFERENCES torrents(id),
        request_id TEXT NOT NULL REFERENCES transfer_requests(id),
        provider_resource_id TEXT NOT NULL REFERENCES provider_resources(id),
        provider_id TEXT NOT NULL,
        manifest_digest TEXT NOT NULL,
        observed_at REAL NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(provider_resource_id, manifest_digest))""",
    """CREATE TABLE IF NOT EXISTS transfer_file_manifest_entries (
        manifest_id TEXT NOT NULL REFERENCES transfer_file_manifests(id),
        entry_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        name TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        expected_bytes INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(manifest_id, entry_id),
        UNIQUE(manifest_id, relative_path))""",
    """CREATE TABLE IF NOT EXISTS transfer_file_selections (
        id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL REFERENCES transfer_requests(id),
        transfer_id INTEGER NOT NULL REFERENCES torrents(id),
        provider_resource_id TEXT NOT NULL REFERENCES provider_resources(id),
        provider_id TEXT NOT NULL,
        manifest_id TEXT REFERENCES transfer_file_manifests(id),
        initially_available INTEGER NOT NULL DEFAULT 0,
        -- Retired as a submission-relative window (Torrent/Magnet File-Selection
        -- Lifecycle Correction §14). Now a mirror of the post-AVAILABLE manifest
        -- grace deadline: 0.0 until the resource is first observed AVAILABLE
        -- without a usable manifest, an absolute deadline thereafter. The gate
        -- reads ``available_at``, never this column.
        manifest_wait_until REAL NOT NULL,
        -- Core ``now`` at which this generation's resource was first observed
        -- executable/AVAILABLE. NULL while still PREPARING. Anchors the bounded
        -- 60s post-AVAILABLE manifest-acquisition grace; never a decision timer.
        available_at REAL,
        auto_offer_queued_at REAL,
        auto_offer_dismissed_at REAL,
        decision TEXT NOT NULL DEFAULT 'pending' CHECK(decision IN ('pending','explicit','all')),
        decision_reason TEXT,
        decision_at REAL,
        hold_until REAL,
        manifest_committed_at REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE(request_id, provider_resource_id))""",
    """CREATE TABLE IF NOT EXISTS transfer_file_selection_entries (
        selection_id TEXT NOT NULL REFERENCES transfer_file_selections(id),
        manifest_id TEXT NOT NULL,
        entry_id TEXT NOT NULL,
        PRIMARY KEY(selection_id, entry_id),
        FOREIGN KEY(manifest_id, entry_id) REFERENCES transfer_file_manifest_entries(manifest_id, entry_id))""",
    "CREATE INDEX IF NOT EXISTS idx_file_manifests_transfer ON transfer_file_manifests(transfer_id,request_id)",
    "CREATE INDEX IF NOT EXISTS idx_file_manifest_entries_ordinal ON transfer_file_manifest_entries(manifest_id,ordinal)",
    "CREATE INDEX IF NOT EXISTS idx_file_selections_transfer ON transfer_file_selections(transfer_id,created_at)",
    "CREATE INDEX IF NOT EXISTS idx_file_selections_request ON transfer_file_selections(request_id,provider_resource_id)",
    "CREATE INDEX IF NOT EXISTS idx_file_selections_active ON transfer_file_selections(decision,manifest_committed_at,manifest_id)",
    "CREATE INDEX IF NOT EXISTS idx_file_selection_entries_manifest ON transfer_file_selection_entries(manifest_id,entry_id)",
)

TRANSFER_REPOSITORY_COLUMNS = {
    'torrents': {
        'normalized_error': 'TEXT',
        'lifecycle_epoch': 'INTEGER NOT NULL DEFAULT 0',
        'delete_remote': 'INTEGER NOT NULL DEFAULT 0',
        'collection_route_provider_id': 'TEXT',
        # Original logical source fingerprint, retained as durable provenance even
        # after Delete retires the active unique ``hash`` dedupe key.
        'source_fingerprint': 'TEXT',
    },
    'transfer_requests': {
        'metadata': 'TEXT',
        'equivalence_retry_count': 'INTEGER NOT NULL DEFAULT 0',
        'equivalence_reason': 'TEXT',
        'equivalence_disposition': "TEXT NOT NULL DEFAULT ''",
        # The one canonical artifact (``download_files.id``) an ``unverified``
        # request is durably associated with for lifecycle/presentation only --
        # never a canonical membership (no binding, origin or consolidation row
        # is ever derived from it). Owned by the same equivalence authority as
        # the three columns above (``transfers.cohorts``). Additive and
        # nullable: every existing request is correctly NULL; no backfill.
        'equivalence_target_artifact_id': 'INTEGER',
        # DP 1.0.12 canonical architecture correction, Workstream A: the
        # ``transfer_file_selections.id`` generation that authorized this
        # file-selection CHILD row's materialization (set once, at
        # ``TransferRepository.manifest()`` fan-out time; NULL for a root
        # request and for any row created before this column existed).
        # Durable and immune to a later root re-resolution rebinding
        # ``transfer_requests.resource`` -- see
        # ``TransferRepository.materialization_authorization``.
        'materialized_selection_id': 'TEXT',
    },
    'provider_resources': {
        'cleanup_attempts': 'INTEGER NOT NULL DEFAULT 0', 'cleanup_retry_at': 'REAL NOT NULL DEFAULT 0',
        # RETIRED legacy boolean claim marker. It is never read or written by the
        # cleanup lifecycle any more (a boolean cannot expire, which is how a
        # cancelled claim stranded a same-object re-add forever); the column is
        # only kept so existing databases stay structurally compatible, and
        # ``_normalize_legacy_cleanup_claims`` zeroes it on every initialization.
        'cleanup_blocked': 'INTEGER NOT NULL DEFAULT 0',
        # Canonical, transfer-independent DP resource identity (== ProviderResource.id).
        # provider_resources.id is the (transfer, resource) binding-generation id.
        'resource_key': 'TEXT',
        # Set only after a provider cleanup call has returned and policy has given
        # up permanently.
        'cleanup_abandoned': 'INTEGER NOT NULL DEFAULT 0',
        # The ONE durable cleanup-claim lease: a unique owner token plus an absolute
        # (engine clock) expiry. A claim is current while the token is set and the
        # expiry has not passed; an expired claim is claimable by the ordinary
        # cleanup cadence, and finalization is conditional on the current token so
        # a stale claimant can never overwrite a newer owner.
        'cleanup_claim_token': 'TEXT',
        'cleanup_claim_until': 'REAL NOT NULL DEFAULT 0',
    },
    'resolution_attempts': {'result': 'TEXT'},
    'execution_attempts': {
        'candidate': 'TEXT', 'progress_at': 'REAL', 'cleanup_state': 'TEXT', 'cleanup_attempts': 'INTEGER NOT NULL DEFAULT 0', 'cleanup_retry_at': 'REAL NOT NULL DEFAULT 0', 'cleanup_error': 'TEXT',
        # Execution-owned target authority, recorded once at final execution
        # admission: 1 = immediately before this execution received native
        # start authority the validated target (and its executor-declared
        # resumable sidecars) held no pre-existing material; 0 = material was
        # already there. Additive and nullable: a row written before this
        # column existed stays NULL, which reads as "ownership unknown" and
        # therefore never authorizes cleanup. No backfill exists or is needed.
        'target_initially_absent': 'INTEGER',
    },
    # Additive nullable column for databases created before the Torrent/Magnet
    # File-Selection Lifecycle Correction. A metadata-only ALTER: every existing
    # row is left NULL, which is exactly the correct "resource not yet observed
    # AVAILABLE under the corrected engine" value — the next ordinary AVAILABLE
    # observation anchors it. There is no data backfill (§14 / §14a).
    'transfer_file_selections': {'available_at': 'REAL'},
    'download_files': {
        'request_id': 'TEXT', 'candidates': 'TEXT', 'selected_candidate': 'INTEGER NOT NULL DEFAULT 0',
        'execution_attempt_id': 'TEXT', 'normalized_error': 'TEXT', 'retry_at': 'REAL NOT NULL DEFAULT 0',
        'recovery_failures': 'INTEGER NOT NULL DEFAULT 0', 'recovery_refreshes': 'INTEGER NOT NULL DEFAULT 0',
        # DP 1.0.12 recovery leveling, Section 13: a durable, bounded (TTL-expiring)
        # continuation-admission reservation. Set only by
        # transfers.candidate_activation.activate_candidate's commit, when the
        # writer it just retired was genuinely occupying a live execution slot,
        # so unrelated queued work cannot steal that slot during the short
        # writer-replacement handoff. NULL is "no reservation held". Expiry is
        # absolute (engine clock time), not a duration, so a restarted process
        # reconstructs correctness by comparing against current time alone --
        # no separate restart-reconciliation step is needed.
        'continuation_reservation_expires_at': 'REAL',
        # FUNC-001: the durable half of the canonical size fact. ``size_bytes``
        # alone cannot distinguish "zero because no size evidence exists" from
        # "affirmatively zero bytes", and that distinction must survive restart
        # and recovery. Additive and nullable: every existing row stays NULL,
        # which ``transfers.models.SizeKnowledge.durable`` -- the one reader --
        # interprets as UNKNOWN for a non-positive byte count and as
        # KNOWN_POSITIVE for a positive one. A historical ``0`` is therefore
        # never promoted to legitimate zero, and no backfill exists or is
        # wanted. Written only by a verified completion (the one writer), so a
        # value here is always evidence something actually proved, never a
        # default.
        'size_knowledge': 'TEXT',
    },
}

_TRANSFER_REPOSITORY_REQUIRED_COLUMNS = {
    'application_events': {'id', 'created_at', 'claimed', 'transfer_id', 'detail', 'kind'},
    'download_files': {'candidates', 'execution_attempt_id', 'normalized_error', 'request_id', 'retry_at', 'selected_candidate', 'recovery_failures', 'recovery_refreshes', 'continuation_reservation_expires_at', 'size_knowledge'},
    'execution_attempt_provenance': {'artifact_id', 'candidate_id', 'candidate_source', 'created_at', 'delivered', 'execution_attempt_id', 'history_quality', 'ordinal', 'outcome', 'provider_id', 'route_attempt_id', 'transfer_id', 'updated_at'},
    'execution_attempts': {'artifact_id', 'authorized', 'candidate', 'cleanup_attempts', 'cleanup_error', 'cleanup_retry_at', 'cleanup_state', 'created_at', 'error', 'executor_id', 'handle', 'id', 'progress', 'progress_at', 'state', 'target_initially_absent', 'transfer_id', 'updated_at'},
    'postprocess_attempts': {'processor_id', 'paths', 'state', 'transfer_id', 'outcome'},
    'provider_resources': {'cleanup_abandoned', 'cleanup_attempts', 'cleanup_authority', 'cleanup_blocked', 'cleanup_claim_token', 'cleanup_claim_until', 'cleanup_error', 'cleanup_retry_at', 'id', 'payload', 'provider_id', 'resource_key', 'state', 'transfer_id', 'updated_at'},
    'resolution_attempts': {'created_at', 'error', 'id', 'provider_id', 'request_id', 'result', 'state', 'updated_at'},
    'route_attempt_provenance': {'candidate_summary', 'created_at', 'history_quality', 'operation', 'ordinal', 'outcome', 'previous_attempt_id', 'request_id', 'resolution_attempt_id', 'transfer_id', 'transition_kind', 'transition_reason', 'updated_at'},
    'canonical_candidate_bindings': {'id', 'canonical_artifact_id', 'candidate_id', 'provider_id', 'source_scope', 'source_key', 'role', 'candidate_order', 'created_at', 'updated_at'},
    'canonical_candidate_origins': {'id', 'binding_id', 'contributing_artifact_id', 'contributing_transfer_id', 'request_id', 'resolution_attempt_id', 'discovered_candidate_id', 'created_at'},
    'artifact_consolidations': {'contributing_artifact_id', 'source_transfer_id', 'source_request_id', 'canonical_artifact_id', 'created_at', 'updated_at'},
    'transfer_file_manifests': {'id', 'transfer_id', 'request_id', 'provider_resource_id', 'provider_id', 'manifest_digest', 'observed_at', 'created_at'},
    'transfer_file_manifest_entries': {'manifest_id', 'entry_id', 'ordinal', 'name', 'relative_path', 'expected_bytes'},
    'transfer_file_selections': {'id', 'request_id', 'transfer_id', 'provider_resource_id', 'provider_id', 'manifest_id', 'initially_available', 'manifest_wait_until', 'available_at', 'auto_offer_queued_at', 'auto_offer_dismissed_at', 'decision', 'decision_reason', 'decision_at', 'hold_until', 'manifest_committed_at', 'created_at', 'updated_at'},
    'transfer_file_selection_entries': {'selection_id', 'manifest_id', 'entry_id'},
    'torrents': {'normalized_error', 'lifecycle_epoch', 'delete_remote', 'collection_route_provider_id', 'source_fingerprint'},
    'transfer_controls': {'value', 'key'},
    'transfer_outcomes': {'id', 'attempt_id', 'created_at', 'payload', 'transfer_id', 'kind'},
    'transfer_requests': {
        'attempts', 'error', 'id', 'metadata', 'ordinal', 'parent_id', 'payload', 'resource', 'retry_at', 'state',
        'transfer_id', 'equivalence_retry_count', 'equivalence_reason', 'equivalence_disposition',
        'equivalence_target_artifact_id',
    },
    'artifact_recovery_state': {
        'artifact_id', 'transfer_id', 'version', 'recovery_epoch', 'progress_anchor',
        'consecutive_no_progress_failures', 'failures_since_meaningful_progress', 'failure_signature',
        'same_signature_failures', 'candidate_refreshes', 'candidate_switches', 'decision_action',
        'decision_reason', 'quiescence_reason', 'wake_condition', 'candidate_attempt_history',
        'recovery_generation', 'recovery_claim_token', 'recovery_claim_trigger', 'recovery_claim_until',
        'recovery_claim_id', 'recovery_decision_id', 'last_failure_identity', 'last_refresh_decision_id',
        'refresh_inflight_decision_id', 'refresh_inflight_attempt_id', 'blocked_retry_at',
        'last_applied_action', 'last_applied_reason', 'updated_at',
    },
}


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
        # Retained schema object; no in-scope reader as of the DP 1.0.12
        # leveling remediation (FUNC-001 removed the bounded "recovery net"
        # read-model compensation that used to SEEK this table by recency --
        # see transfers._repository_base._retire_transfer_auxiliary_state_in_db
        # for the corrected owner: a settled transfer's row is now retired
        # transactionally at settlement instead of compensated for at read
        # time).
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_transfer_pause_intents_paused_updated "
            "ON transfer_pause_intents (paused, updated_at)"
        )
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
        await _ensure_column(db, "transfer_input_challenges", "facts", _INPUT_CHALLENGE_FACTS_DEFINITION)
        for statement in TRANSFER_REPOSITORY_SCHEMA:
            await db.execute(statement)
        for col, defn in _SCHEMA_COLUMNS_TORRENTS:
            await _ensure_column(db, "torrents", col, defn)
        for col, defn in _SCHEMA_COLUMNS_FILES:
            await _ensure_column(db, "download_files", col, defn)
        for table, definitions in TRANSFER_REPOSITORY_COLUMNS.items():
            for column, definition in definitions.items():
                await _ensure_column(db, table, column, definition)
        await _retire_and_backfill_source_fingerprints(db)
        await _backfill_provider_resource_bindings(db)
        await _normalize_legacy_cleanup_claims(db)
        await _migrate_recovery_state_from_events(db)
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
            # DP 1.0.12 UI Finishing (Correction 2): the Dashboard Recent
            # Activity priority-cohort candidate queries (api.operational_
            # downloads._bounded_status_candidates) SEEK directly to one raw
            # status value at a time and walk it in created_at order, so a
            # single-column (status) or (status, updated_at) index cannot
            # satisfy the ORDER BY without a sort. This composite lets each
            # per-status query be an indexed SEEK + a walk bounded by LIMIT,
            # never a scan/sort proportional to total history size.
            "CREATE INDEX IF NOT EXISTS idx_torrents_status_created ON torrents (status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_torrents_status_priority ON torrents (status, priority DESC, id ASC)",
            "CREATE INDEX IF NOT EXISTS idx_torrents_completed_at ON torrents (completed_at)",
            "CREATE INDEX IF NOT EXISTS idx_torrents_priority ON torrents (priority DESC, id ASC)",
            "CREATE INDEX IF NOT EXISTS idx_torrents_hash ON torrents (hash)",
            "CREATE INDEX IF NOT EXISTS idx_torrents_created_at ON torrents (created_at)",
            "CREATE INDEX IF NOT EXISTS idx_dlfiles_local_path ON download_files (local_path)",
            "CREATE INDEX IF NOT EXISTS idx_events_torrent_id ON events (torrent_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_created_at ON events (created_at)",
            # DP 1.0.12 Workstream A performance correction (originally): the
            # operational Downloads projection's per-artifact latest
            # recovery-snapshot lookup used to match ``kind = 'transfer_
            # recovery:' || <artifact id>`` ordered by id DESC against an
            # unbounded, ever-growing application_events history, and this
            # index made that a seek instead of a full table SCAN (live
            # evidence up to ~20.9s for a single 703-file torrent).
            # DP 1.0.12 recovery leveling, Section 14, replaced that whole
            # per-progress-tick-appended history with the single-row-per-
            # artifact ``artifact_recovery_state`` table, so
            # ``artifact_presentation_facts`` no longer queries
            # ``application_events`` at all. This index remains useful for
            # ``kind``-scoped lookups against the now-sparse audit rows this
            # leveling pass writes instead (``recovery_audit``,
            # ``candidate_activation``) and for the one-time legacy-snapshot
            # migration reader (``_migrate_recovery_state_from_events``).
            "CREATE INDEX IF NOT EXISTS idx_application_events_kind_id ON application_events (kind, id DESC)",
            RUNTIME_STATE_SCHEMA[1],
            INPUT_CHALLENGE_SCHEMA[1],
            INPUT_CHALLENGE_SCHEMA[2],
        ]:
            await idx_db.execute(ddl)
        await idx_db.commit()
    logger.debug("SQLite indexes ensured")

    async with aiosqlite.connect(DB_PATH) as verify_db:
        required = {
            "torrents": {"id", "hash", "status"} | {name for name, _ in _SCHEMA_COLUMNS_TORRENTS},
            "download_files": {"id", "torrent_id", "status", "blocked"} | {name for name, _ in _SCHEMA_COLUMNS_FILES},
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
