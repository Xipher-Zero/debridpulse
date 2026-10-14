"""Durable external-client state: jobs, delivery generations, advisory
controls. Schema created by the canonical bootstrap (``db.database.init_db``
through ``ensure_schema``), wiped child-first by ``services.db_maintenance``.

* ``external_jobs`` -- one row per external submission. ``(client_id,
  idempotency_key)`` is unique. ``reserved_demand_id`` is the demand ID chosen
  before admission; ``demand_id`` (a foreign key to the demand ledger) is set
  once that demand exists, so a job is ACCEPTED exactly when its demand is
  durable. A crash between the two is completed from the ledger itself.
* ``external_deliveries`` -- a job's committed delivery generations, each an
  immutable record of one material set; acknowledgment is per generation.
* ``external_controls`` -- the job's advisory pause/resume requests and the
  decision DebridPulse made on each; an applied pause records the hold the
  pause owner granted (``pause_holder``, ``pause_generation`` of
  ``transfer_pause_intents``), never a copy of the pause itself. A control
  that applies a core transition is written ``pending`` -- with the holder it
  applies or releases and the intent generation it observed -- BEFORE that
  transition, so an interruption anywhere leaves a durable record its
  recovery (``ExternalClients._recover_pending``) can settle.
"""
from __future__ import annotations

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS external_jobs (
        id TEXT PRIMARY KEY,
        client_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        credential_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        intent_digest TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        source_summary TEXT NOT NULL,
        category TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        profile_snapshot TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT '',
        client_reference TEXT,
        reserved_demand_id TEXT NOT NULL UNIQUE,
        demand_id TEXT UNIQUE REFERENCES transfer_demands(id),
        delivery_id TEXT NOT NULL UNIQUE,
        created_at REAL NOT NULL,
        accepted_at REAL,
        revision INTEGER NOT NULL DEFAULT 0,
        change_seq INTEGER NOT NULL DEFAULT 0,
        projection TEXT,
        refreshed_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL,
        UNIQUE(client_id, idempotency_key))""",
    "CREATE INDEX IF NOT EXISTS idx_external_jobs_client_seq ON external_jobs(client_id, change_seq)",
    "CREATE INDEX IF NOT EXISTS idx_external_jobs_refresh ON external_jobs(accepted_at, refreshed_at)",
    """CREATE TABLE IF NOT EXISTS external_deliveries (
        job_id TEXT NOT NULL REFERENCES external_jobs(id),
        generation INTEGER NOT NULL CHECK (generation > 0),
        material_key TEXT NOT NULL,
        files TEXT NOT NULL,
        import_root TEXT NOT NULL,
        committed_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        superseded_at REAL,
        unavailable_reason TEXT,
        expired_at REAL,
        acknowledged_at REAL,
        acknowledged_by TEXT,
        PRIMARY KEY (job_id, generation))""",
    """CREATE TABLE IF NOT EXISTS external_controls (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES external_jobs(id),
        request_key TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('pause','resume')),
        credential_id TEXT NOT NULL,
        disposition TEXT NOT NULL CHECK (disposition IN ('pending','honored','declined','revoked','superseded')),
        reason TEXT NOT NULL,
        transfer_id INTEGER,
        pause_holder TEXT,
        pause_generation INTEGER,
        observed_generation INTEGER,
        created_at REAL NOT NULL,
        retired_at REAL,
        retired_reason TEXT,
        UNIQUE(job_id, request_key))""",
    "CREATE INDEX IF NOT EXISTS idx_external_controls_active ON external_controls(kind, disposition, retired_at)",
)
# Child-first: what ``services.db_maintenance`` deletes before the demand ledger.
TABLES = ("external_controls", "external_deliveries", "external_jobs")


async def ensure_schema(db) -> None:
    for statement in SCHEMA:
        await db.execute(statement)
