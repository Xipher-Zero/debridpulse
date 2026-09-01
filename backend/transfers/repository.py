"""Durable transfer, request, resource and attempt identities.

The existing parent/artifact table names remain a database-format obligation.
Their integration-specific columns are not read by this repository. Native data
is persisted only as opaque context on a resource or execution attempt.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from db.database import get_db
from transfers import codec
from transfers.errors import Category, Domain, NormalizedError, Stage, TransferError
from transfers.models import (
    Artifact, ExecutionAttempt, ExecutionHandle, ExecutionObservation, ExecutionState,
    ProviderResource, RequestRecord, ResolutionAttempt, ResolutionResult,
    ResourceState, SourceEntry, Transfer, TransferCandidate, TransferRequest,
    TransferState, new_identity,
)
from transfers.policy import transition_allowed


_SCHEMA = (
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
    """CREATE TABLE IF NOT EXISTS transfer_outcomes (
        id INTEGER PRIMARY KEY, transfer_id INTEGER NOT NULL REFERENCES torrents(id),
        attempt_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
    "CREATE INDEX IF NOT EXISTS idx_requests_ready ON transfer_requests(state,retry_at,transfer_id)",
    "CREATE INDEX IF NOT EXISTS idx_attempts_artifact ON execution_attempts(artifact_id,state)",
    "CREATE INDEX IF NOT EXISTS idx_resources_transfer ON provider_resources(transfer_id,provider_id)",
)
_COLUMNS = {
    "torrents": {"normalized_error": "TEXT", "lifecycle_epoch": "INTEGER NOT NULL DEFAULT 0", "delete_remote": "INTEGER NOT NULL DEFAULT 0"},
    "transfer_requests": {"metadata": "TEXT"},
    "provider_resources": {"cleanup_attempts": "INTEGER NOT NULL DEFAULT 0", "cleanup_retry_at": "REAL NOT NULL DEFAULT 0", "cleanup_blocked": "INTEGER NOT NULL DEFAULT 0"},
    "download_files": {
        "request_id": "TEXT", "candidates": "TEXT", "selected_candidate": "INTEGER NOT NULL DEFAULT 0",
        "execution_attempt_id": "TEXT", "normalized_error": "TEXT", "retry_at": "REAL NOT NULL DEFAULT 0",
    },
}


class TransferRepository:
    async def initialize(self) -> None:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA:
                await db.execute(statement)
            for table, definitions in _COLUMNS.items():
                existing = {row["name"] for row in await db.fetchall(f"PRAGMA table_info({table})")}
                for column, definition in definitions.items():
                    if column not in existing:
                        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_request ON download_files(request_id) WHERE request_id IS NOT NULL")
            await db.commit()

    @staticmethod
    def _transfer(row) -> Transfer | None:
        if not row:
            return None
        return Transfer(int(row["id"]), str(row["name"] or ""), TransferState(row["status"]),
                        str(row["hash"] or ""), str(row["source"] or ""), int(row["priority"] or 0),
                        bool(row.get("paused_intent")), float(row["progress"] or 0), codec.error(row.get("normalized_error")), int(row.get("lifecycle_epoch") or 0))

    async def get(self, transfer_id: int) -> Transfer | None:
        async with get_db() as db:
            row = await db.fetchone("""SELECT t.*, COALESCE(p.paused,0) AS paused_intent FROM torrents t
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id WHERE t.id=?""", (transfer_id,))
        return self._transfer(row)

    async def active(self) -> tuple[Transfer, ...]:
        async with get_db() as db:
            rows = await db.fetchall("""SELECT t.*, COALESCE(p.paused,0) AS paused_intent FROM torrents t
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id
                WHERE t.status NOT IN ('completed','deleted')
                AND EXISTS(SELECT 1 FROM transfer_requests r WHERE r.transfer_id=t.id)
                ORDER BY t.priority DESC,t.id""")
        return tuple(self._transfer(row) for row in rows)

    async def admit(self, requests: tuple[TransferRequest, ...], *, name: str, source: str = "manual", priority=0, deduplicate=True) -> tuple[Transfer, bool]:
        fingerprint = requests[0].fingerprint if len(requests) == 1 else ""
        # Routing preferences and display names are not logical source identity.
        # The same accepted request can be resolved through another integration.
        fingerprint = fingerprint or hashlib.sha256(codec.dump(tuple(
            (item.kind, item.payload, item.fingerprint) for item in requests)).encode()).hexdigest()
        if not deduplicate:
            fingerprint = "request:" + new_identity()
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT * FROM torrents WHERE hash=?", (fingerprint,))
            if row:
                transfer_id, created = int(row["id"]), False
            else:
                transfer_id = await db.execute_returning_id(
                    """INSERT INTO torrents(hash,name,status,source,priority,download_client)
                    VALUES(?,?,'pending',?,?,'')""", (fingerprint, name, source, priority))
                created = True
            existing = await db.fetchone("SELECT id FROM transfer_requests WHERE transfer_id=? LIMIT 1", (transfer_id,))
            if not existing:
                for ordinal, request in enumerate(requests):
                    await db.execute("INSERT INTO transfer_requests(id,transfer_id,ordinal,payload) VALUES(?,?,?,?)",
                                     (new_identity(), transfer_id, ordinal, codec.dump(request)))
            await db.commit()
        return await self.get(transfer_id), created

    async def state(self, transfer_id: int, target: TransferState, *, progress=None, error=None, operator=False, expected_epoch=None) -> bool:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT status,lifecycle_epoch FROM torrents WHERE id=?", (transfer_id,))
            if not row or not transition_allowed(TransferState(row["status"]), target, operator=operator):
                return False
            if expected_epoch is not None and row["lifecycle_epoch"] != expected_epoch:
                return False
            await db.execute("""UPDATE torrents SET status=?, progress=COALESCE(?,progress), normalized_error=?,
                error_message=?, updated_at=CURRENT_TIMESTAMP,
                completed_at=CASE WHEN ?='completed' THEN COALESCE(completed_at,CURRENT_TIMESTAMP)
                    WHEN ? IN ('pending','queued') THEN NULL ELSE completed_at END WHERE id=?""",
                (target, progress, codec.dump(error) if error else None, error.message if error else None, target, target, transfer_id))
            await db.commit()
        return True

    async def pause_intent(self, transfer_id: int, paused: bool) -> None:
        async with get_db() as db:
            await db.execute("""INSERT INTO transfer_pause_intents(torrent_id,paused) VALUES(?,?)
                ON CONFLICT(torrent_id) DO UPDATE SET paused=excluded.paused,updated_at=CURRENT_TIMESTAMP""", (transfer_id, int(paused)))
            await db.commit()

    async def requests(self, transfer_id: int) -> tuple[RequestRecord, ...]:
        async with get_db() as db:
            rows = await db.fetchall("SELECT * FROM transfer_requests WHERE transfer_id=? ORDER BY parent_id,ordinal", (transfer_id,))
        return tuple(RequestRecord(row["id"], transfer_id, codec.request(codec.load(row["payload"])), row["state"],
                                   row["parent_id"], codec.resource(codec.load(row["resource"])), row["attempts"],
                                   row["retry_at"], codec.error(row["error"]), codec.entry(codec.load(row["metadata"]))) for row in rows)

    async def begin_resolution(self, request_id: str, provider_id: str) -> ResolutionAttempt | None:
        identity = new_identity()
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT r.* FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id WHERE r.id=? AND r.state='pending'
                AND t.status NOT IN ('deleted','completed') AND COALESCE(p.paused,0)=0""", (request_id,))
            if not row:
                return None
            await db.execute("UPDATE transfer_requests SET state='resolving',attempts=attempts+1 WHERE id=?", (request_id,))
            await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES(?,?,?,'started')", (identity, request_id, provider_id))
            await db.commit()
        return ResolutionAttempt(identity, request_id, provider_id, "started")

    @staticmethod
    async def _resource(db, transfer_id: int, resource: ProviderResource, state: ResourceState):
        existing = await db.fetchone("SELECT transfer_id,provider_id,payload FROM provider_resources WHERE id=?", (resource.id,))
        if existing and (existing["transfer_id"] != transfer_id or existing["provider_id"] != resource.provider_id):
            raise TransferError(NormalizedError(Domain.LIFECYCLE, Category.OWNERSHIP_CONFLICT, Stage.RESOLUTION))
        if existing:
            resource = replace(resource, ownership=codec.resource(codec.load(existing["payload"])).ownership)
        await db.execute("""INSERT INTO provider_resources(id,transfer_id,provider_id,payload,state) VALUES(?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,state=excluded.state,updated_at=CURRENT_TIMESTAMP""",
            (resource.id, transfer_id, resource.provider_id, codec.dump(resource), state))

    async def resolution(self, attempt: ResolutionAttempt, result: ResolutionResult) -> bool:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT r.transfer_id,t.status FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                WHERE r.id=?""", (attempt.request_id,))
            if not row:
                return False
            if result.observation:
                # Persist even after Delete so late-created remote resources can
                # be cleaned up without reviving the transfer.
                await self._resource(db, row["transfer_id"], result.observation.resource, result.observation.state)
            error = codec.dump(result.error) if result.error else None
            status = "failed" if result.error else "succeeded"
            await db.execute("UPDATE resolution_attempts SET state=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, error, attempt.id))
            resource = codec.dump(result.observation.resource) if result.observation else None
            request_state = "failed" if result.error else "waiting" if result.observation and not result.candidates else "resolved"
            if row["status"] != "deleted":
                await db.execute("UPDATE transfer_requests SET state=?,resource=COALESCE(?,resource),error=? WHERE id=?",
                                 (request_state, resource, error, attempt.request_id))
            await db.commit()
        return row["status"] != "deleted"

    async def request_failure(self, request_id: str, error: NormalizedError, retry_at: float | None, *, retry_state="pending", consume_attempt=False) -> None:
        async with get_db() as db:
            await db.execute("""UPDATE transfer_requests SET state=?,error=?,retry_at=?,attempts=attempts+? WHERE id=?
                AND transfer_id IN (SELECT id FROM torrents WHERE status!='deleted')""",
                (retry_state if retry_at is not None else "failed", codec.dump(error), retry_at or 0, int(consume_attempt), request_id))
            await db.commit()

    async def manifest(self, record: RequestRecord, entries: tuple[SourceEntry, ...]) -> None:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            parent = await db.fetchone("SELECT status FROM torrents WHERE id=?", (record.transfer_id,))
            if not parent or parent["status"] == "deleted":
                return
            for ordinal, entry in enumerate(entries):
                identity = uuid5(NAMESPACE_URL, f"request:{record.id}:{entry.relative_path}").hex
                await db.execute("""INSERT OR IGNORE INTO transfer_requests(id,transfer_id,parent_id,ordinal,payload,metadata)
                    VALUES(?,?,?,?,?,?)""", (identity, record.transfer_id, record.id, ordinal, codec.dump(entry.request), codec.dump(entry)))
            await db.execute("UPDATE transfer_requests SET state='resolved',error=NULL WHERE id=?", (record.id,))
            await db.commit()

    async def resource_observation(self, transfer_id: int, resource: ProviderResource, state: ResourceState):
        async with get_db() as db:
            await self._resource(db, transfer_id, resource, state)
            await db.commit()

    async def materialize(self, record: RequestRecord, candidates: tuple[TransferCandidate, ...], target: str) -> Artifact | None:
        if not candidates:
            return None
        chosen = candidates[0]
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            parent = await db.fetchone("SELECT status FROM torrents WHERE id=?", (record.transfer_id,))
            if not parent or parent["status"] == "deleted":
                return None
            previous = await db.fetchone("SELECT id FROM download_files WHERE request_id=?", (record.id,))
            if previous:
                await db.execute("""UPDATE download_files SET candidates=?,selected_candidate=0,
                    size_bytes=?,normalized_error=NULL,
                    status=CASE WHEN status='unresolved' AND execution_attempt_id IS NULL THEN 'queued' ELSE status END
                    WHERE id=?""", (codec.dump(candidates), chosen.expected_bytes, previous["id"]))
            else:
                await db.execute("""INSERT INTO download_files(torrent_id,request_id,filename,size_bytes,local_path,status,candidates,download_client)
                    VALUES(?,?,?,?,?,'queued',?,'')""",
                    (record.transfer_id, record.id, chosen.name, chosen.expected_bytes, target, codec.dump(candidates)))
            await db.commit()
        return next(item for item in await self.artifacts(record.transfer_id) if item.request_id == record.id)

    async def artifacts(self, transfer_id: int) -> tuple[Artifact, ...]:
        async with get_db() as db:
            rows = await db.fetchall("""SELECT f.*,e.handle FROM download_files f
                LEFT JOIN execution_attempts e ON e.id=f.execution_attempt_id
                WHERE f.torrent_id=? AND f.request_id IS NOT NULL ORDER BY f.id""", (transfer_id,))
        return tuple(Artifact(row["id"], transfer_id, row["request_id"], row["filename"], row["local_path"], row["size_bytes"] or 0,
                              row["status"], tuple(codec.candidate(item) for item in codec.load(row["candidates"], [])),
                              row["selected_candidate"], codec.handle(codec.load(row["handle"])), row["retry_count"] or 0,
                              row["retry_at"], codec.error(row["normalized_error"])) for row in rows)

    async def occupied_paths(self) -> set[str]:
        async with get_db() as db:
            rows = await db.fetchall("""SELECT f.local_path FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                WHERE t.status NOT IN ('deleted','completed','error') AND f.local_path IS NOT NULL""")
        return {str(row["local_path"]).casefold() for row in rows}

    async def prepare_execution(self, artifact: Artifact, handle: ExecutionHandle) -> bool:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT f.* FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id
                WHERE f.id=? AND f.status='queued' AND f.execution_attempt_id IS NULL
                AND t.status NOT IN ('deleted','completed') AND COALESCE(p.paused,0)=0""", (artifact.id,))
            if not row:
                return False
            await db.execute("""INSERT INTO execution_attempts(id,transfer_id,artifact_id,executor_id,handle,state)
                VALUES(?,?,?,?,?,'prepared')""", (handle.attempt_id, artifact.transfer_id, artifact.id, handle.executor_id, codec.dump(handle)))
            await db.execute("""UPDATE download_files SET execution_attempt_id=?,download_client=?,retry_count=retry_count+1,
                normalized_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?""", (handle.attempt_id, handle.executor_id, artifact.id))
            await db.commit()
        return True

    async def authorize_execution(self, handle: ExecutionHandle, action: str) -> bool:
        async with get_db() as db:
            row = await db.fetchone("""SELECT e.*,t.status AS transfer_status,COALESCE(p.paused,0) AS paused_intent
                FROM execution_attempts e JOIN torrents t ON t.id=e.transfer_id
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id WHERE e.id=?""", (handle.attempt_id,))
        if not row or not row["authorized"] or row["executor_id"] != handle.executor_id or codec.load(row["handle"]) != codec.load(codec.dump(handle)):
            return False
        if action in {"start", "resume"} and (row["transfer_status"] in {"deleted", "completed"} or row["paused_intent"]):
            return False
        if action in {"start", "resume"} and await self.globally_paused():
            return False
        return action != "start" or row["state"] == "prepared"

    async def execution(self, observation: ExecutionObservation) -> None:
        handle = observation.handle
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT * FROM execution_attempts WHERE id=?", (handle.attempt_id,))
            if not row or codec.load(row["handle"]) != codec.load(codec.dump(handle)):
                raise TransferError(NormalizedError(Domain.LIFECYCLE, Category.OWNERSHIP_CONFLICT, Stage.RECONCILIATION))
            error = codec.dump(observation.error) if observation.error else None
            revoked = observation.error is not None and observation.error.category == Category.OWNERSHIP_CONFLICT
            await db.execute("""UPDATE execution_attempts SET state=?,progress=?,error=?,authorized=CASE WHEN ? THEN 0 ELSE authorized END,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""", (observation.state, codec.dump(observation.progress), error, revoked, handle.attempt_id))
            states = {ExecutionState.TRANSFERRING: "downloading", ExecutionState.QUEUED: "queued", ExecutionState.PAUSED: "paused",
                      ExecutionState.SUCCEEDED: "verifying", ExecutionState.FAILED: "error", ExecutionState.CANCELLED: "cancelled",
                      ExecutionState.ABSENT: "lost", ExecutionState.UNKNOWN: "unknown"}
            await db.execute("""UPDATE download_files SET status=?,normalized_error=?,updated_at=CURRENT_TIMESTAMP
                WHERE execution_attempt_id=? AND torrent_id IN (SELECT id FROM torrents WHERE status!='deleted')""",
                (states[observation.state], error, handle.attempt_id))
            await db.commit()

    async def artifact_state(self, artifact_id: int, state: str, *, error=None, retry_at=0, release=False, selected=None):
        async with get_db() as db:
            await db.execute("""UPDATE download_files SET status=?,normalized_error=?,retry_at=?,
                execution_attempt_id=CASE WHEN ? THEN NULL ELSE execution_attempt_id END,
                selected_candidate=COALESCE(?,selected_candidate),updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND torrent_id IN (SELECT id FROM torrents WHERE status!='deleted')""",
                (state, codec.dump(error) if error else None, retry_at, release, selected, artifact_id))
            await db.commit()

    async def executions(self, transfer_id: int | None = None) -> tuple[ExecutionAttempt, ...]:
        async with get_db() as db:
            rows = await db.fetchall("SELECT * FROM execution_attempts" + (" WHERE transfer_id=?" if transfer_id is not None else ""),
                                     (transfer_id,) if transfer_id is not None else ())
        from transfers.models import TransferProgress
        return tuple(ExecutionAttempt(codec.handle(codec.load(row["handle"])), row["transfer_id"], row["artifact_id"], row["state"],
                                      TransferProgress(**codec.load(row["progress"], {})), codec.error(row["error"])) for row in rows)

    async def resources(self, transfer_id: int):
        async with get_db() as db:
            rows = await db.fetchall("SELECT * FROM provider_resources WHERE transfer_id=?", (transfer_id,))
        return tuple((codec.resource(codec.load(row["payload"])), ResourceState(row["state"]), row["cleanup_authority"]) for row in rows)

    async def cleanup_intent(self, resource_id: str, authority: str | None, *, error=None):
        async with get_db() as db:
            await db.execute("UPDATE provider_resources SET cleanup_authority=?,cleanup_error=? WHERE id=?",
                             (authority, codec.dump(error) if error else None, resource_id))
            await db.commit()

    async def pending_cleanup(self, now):
        async with get_db() as db:
            rows = await db.fetchall("""SELECT * FROM provider_resources WHERE cleanup_authority IS NOT NULL
                AND cleanup_blocked=0 AND cleanup_retry_at<=?""", (now,))
        return tuple((row["transfer_id"], codec.resource(codec.load(row["payload"])), row["cleanup_authority"], row["cleanup_attempts"]) for row in rows)

    async def claim_cleanup(self, resource_id: str):
        async with get_db() as db:
            result = await db.execute("""UPDATE provider_resources SET cleanup_attempts=cleanup_attempts+1,cleanup_blocked=1
                WHERE id=? AND cleanup_blocked=0""", (resource_id,))
            await db.commit()
        return result.rowcount == 1

    async def cleanup_retry(self, resource_id: str, error, retry_at):
        async with get_db() as db:
            await db.execute("""UPDATE provider_resources SET cleanup_error=?,cleanup_blocked=?,cleanup_retry_at=? WHERE id=?""",
                             (codec.dump(error) if error else None, retry_at is None, retry_at or 0, resource_id))
            await db.commit()

    async def outcome(self, transfer_id: int, outcome, *, attempt_id=None):
        async with get_db() as db:
            await db.execute("INSERT INTO transfer_outcomes(transfer_id,attempt_id,kind,payload) VALUES(?,?,?,?)",
                             (transfer_id, attempt_id, outcome.kind, codec.dump(outcome)))
            message = outcome.error.message if outcome.error else str(outcome.kind)
            await db.execute("INSERT INTO events(torrent_id,level,message) VALUES(?,?,?)",
                             (transfer_id, "error" if outcome.error else "info", message))
            await db.commit()

    async def retry_requests(self, transfer_id: int, *, request_id=None):
        async with get_db() as db:
            await db.execute("UPDATE transfer_requests SET state='pending',retry_at=0,error=NULL WHERE transfer_id=? AND " +
                             ("id=?" if request_id else "state='failed'"), (transfer_id, request_id) if request_id else (transfer_id,))
            await db.commit()

    async def globally_paused(self) -> bool:
        async with get_db() as db:
            row = await db.fetchone("SELECT value FROM transfer_controls WHERE key='paused'")
        return bool(row and row["value"] == "1")

    async def global_pause(self, paused: bool):
        async with get_db() as db:
            await db.execute("""INSERT INTO transfer_controls(key,value) VALUES('paused',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", ("1" if paused else "0",))
            await db.commit()

    async def delete(self, transfer_id: int, *, remote: bool):
        async with get_db() as db:
            await db.execute("""UPDATE torrents SET status='deleted',delete_remote=?,lifecycle_epoch=lifecycle_epoch+1,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""", (int(remote), transfer_id))
            await db.commit()

    async def delete_remote_requested(self, transfer_id: int) -> bool:
        async with get_db() as db:
            row = await db.fetchone("SELECT delete_remote FROM torrents WHERE id=?", (transfer_id,))
        return bool(row and row["delete_remote"])

    async def rename(self, transfer_id: int, name: str):
        async with get_db() as db:
            await db.execute("UPDATE torrents SET name=? WHERE id=? AND status!='deleted'", (name, transfer_id))
            await db.commit()

    async def attach_inventory(self, transfer_id: int, resource: ProviderResource):
        async with get_db() as db:
            await db.execute("""UPDATE transfer_requests SET state='waiting',resource=?,error=NULL
                WHERE transfer_id=? AND parent_id IS NULL AND state IN ('pending','resolving','failed')
                AND transfer_id IN (SELECT id FROM torrents WHERE status NOT IN ('completed','deleted'))""",
                (codec.dump(resource), transfer_id))
            await db.commit()

    async def begin_refresh(self, record: RequestRecord, provider_id: str):
        identity = new_identity()
        async with get_db() as db:
            await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES(?,?,?,'started')",
                             (identity, record.id, provider_id))
            await db.commit()
        return ResolutionAttempt(identity, record.id, provider_id, "started")
