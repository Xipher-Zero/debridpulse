"""Durable transfer, request, resource and attempt identities.

The existing parent/artifact table names remain a database-format obligation.
Their integration-specific columns are not read by this repository. Native data
is persisted only as opaque context on a resource or execution attempt.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from db.database import get_db, validate_transfer_repository_schema
from transfers import codec
from transfers.errors import Category, Domain, NormalizedError, Stage, TransferError
from transfers.input_required import public_challenge
from transfers.models import (
    Artifact, ExecutionAttempt, ExecutionHandle, ExecutionObservation, ExecutionState,
    ProviderResource, RequestRecord, ResolutionAttempt, ResolutionResult,
    ResourceState, SourceEntry, Transfer, TransferCandidate, TransferRequest,
    TransferState, TransferProgress, new_identity,
)
from transfers.policy import transition_allowed


class TransferRepository:
    async def has_integration_references(self, identity=None):
        """Connection changes cannot abandon live jobs or unresolved resources."""
        async with get_db() as db:
            params = () if identity is None else (identity,)
            executor_filter = "" if identity is None else " AND executor_id=?"
            provider_filter = "" if identity is None else " AND provider_id=?"
            if await db.fetchone("SELECT id FROM execution_attempts WHERE authorized=1 AND state IN ('prepared','queued','transferring','paused','unknown')" + executor_filter + " LIMIT 1", params):
                return True
            return bool(await db.fetchone("SELECT id FROM provider_resources WHERE state!='absent'" + provider_filter + " LIMIT 1", params))

    async def pending_events(self):
        async with get_db() as db:
            return await db.fetchall("SELECT * FROM application_events WHERE claimed=0 ORDER BY id LIMIT 100")

    async def claim_event(self, event_id):
        async with get_db() as db:
            cursor = await db.execute("UPDATE application_events SET claimed=1 WHERE id=? AND claimed=0", (event_id,))
            await db.commit()
            return bool(cursor.rowcount)

    async def queue_postprocessing(self, transfer_id, processors, paths):
        async with get_db() as db:
            created = False
            for processor in processors:
                cursor = await db.execute("INSERT OR IGNORE INTO postprocess_attempts(transfer_id,processor_id,paths) VALUES(?,?,?)",
                    (transfer_id, processor.descriptor.id, codec.dump(paths)))
                created = created or bool(cursor.rowcount)
            if created:
                await db.execute("UPDATE torrents SET extraction_status='pending' WHERE id=? AND status NOT IN ('completed','consolidated','deleted') AND COALESCE(extraction_status,'')!='extracting'", (transfer_id,))
            await db.commit()

    async def postprocessing_jobs(self):
        async with get_db() as db:
            return await db.fetchall("""SELECT p.* FROM postprocess_attempts p JOIN torrents t ON t.id=p.transfer_id
                WHERE p.state='pending' AND t.status='extracting' ORDER BY t.priority DESC,t.id""")

    async def claim_postprocessing(self, transfer_id, processor_id):
        async with get_db() as db:
            cursor = await db.execute("UPDATE postprocess_attempts SET state='processing' WHERE transfer_id=? AND processor_id=? AND state='pending'", (transfer_id, processor_id))
            await db.execute("UPDATE torrents SET extraction_status='extracting' WHERE id=? AND status='extracting'", (transfer_id,))
            await db.commit()
            return bool(cursor.rowcount)

    async def finish_postprocessing(self, transfer_id, processor_id, outcome):
        async with get_db() as db:
            await db.execute("UPDATE postprocess_attempts SET state='finished',outcome=? WHERE transfer_id=? AND processor_id=?", (codec.dump(outcome), transfer_id, processor_id))
            message = f"Post-processing {processor_id}: " + (outcome.error.message if outcome.error else outcome.detail or outcome.kind)
            await db.execute("INSERT INTO events(torrent_id,level,message) VALUES(?,?,?)", (transfer_id, "error" if outcome.error else "info", message))
            jobs = await db.fetchall("SELECT state,outcome FROM postprocess_attempts WHERE transfer_id=?", (transfer_id,))
            finished = all(job["state"] == "finished" for job in jobs)
            if finished:
                outcomes = [codec.load(job["outcome"]) for job in jobs if job["outcome"]]
                errors = [outcome.get("error") for outcome in outcomes]
                error = next((NormalizedError.from_dict(item) for item in errors if item), None)
                status = "error" if error else "skipped" if all(item["kind"] == "skipped" for item in outcomes) else "completed"
                await db.execute("UPDATE torrents SET extraction_status=?,extraction_error=? WHERE id=? AND status NOT IN ('deleted','consolidated')", (status, error.message if error else None, transfer_id))
            await db.commit()
            return finished

    async def interrupted_postprocessing(self):
        async with get_db() as db:
            return await db.fetchall("SELECT transfer_id,processor_id FROM postprocess_attempts WHERE state='processing'")

    async def initialize(self) -> None:
        # Runtime repositories consume the schema; database bootstrap/migration
        # is the sole authority allowed to create or repair it.
        await validate_transfer_repository_schema()

    @staticmethod
    def _safe_candidate_source(candidate):
        source = candidate.source_identity if candidate else None
        if source is not None:
            return {"scope": str(source.scope), "key": str(source.key)}
        return {"scope": "candidate", "key": str(candidate.id)} if candidate else None

    @classmethod
    def _candidate_summary(cls, candidates):
        return codec.dump([
            {
                "candidate_id": str(candidate.id),
                "provider_id": str(candidate.provider_id or ""),
                "ordinal": ordinal,
                "source": cls._safe_candidate_source(candidate),
            }
            for ordinal, candidate in enumerate(candidates, start=1)
        ])

    @staticmethod
    def _execution_outcome(state):
        value = str(state)
        if value == "succeeded":
            return "succeeded"
        if value in {"failed", "absent"}:
            return "failed"
        if value == "cancelled":
            return "cancelled"
        if value == "unknown":
            return "unknown"
        return "active"

    @classmethod
    async def _candidate_route(cls, db, transfer_id, candidate, *, artifact_id=None):
        if candidate is None or not candidate.id or not candidate.provider_id:
            return None
        if artifact_id is not None:
            bound = await db.fetchone(
                """SELECT o.resolution_attempt_id FROM canonical_candidate_bindings b
                    JOIN canonical_candidate_origins o ON o.binding_id=b.id
                    WHERE b.canonical_artifact_id=? AND b.candidate_id=? AND b.provider_id=?
                    ORDER BY CASE WHEN o.discovered_candidate_id=b.candidate_id THEN 0 ELSE 1 END,o.id LIMIT 1""",
                (artifact_id, str(candidate.id), str(candidate.provider_id)),
            )
            if bound:
                return bound["resolution_attempt_id"]
        rows = await db.fetchall("""SELECT p.resolution_attempt_id,p.ordinal,p.candidate_summary,a.provider_id
            FROM route_attempt_provenance p JOIN resolution_attempts a ON a.id=p.resolution_attempt_id
            WHERE p.transfer_id=? AND a.provider_id=? ORDER BY p.ordinal DESC,a.updated_at DESC,a.id DESC""",
            (transfer_id, candidate.provider_id))
        for row in rows:
            for item in codec.load(row["candidate_summary"], []):
                if str(item.get("candidate_id") or "") == str(candidate.id):
                    return row["resolution_attempt_id"]
        return None

    @classmethod
    async def _backfill_provenance(cls, db):
        """Idempotently migrate only facts already durably present before Item 9."""
        route_rows = await db.fetchall("""SELECT a.*,r.transfer_id FROM resolution_attempts a
            JOIN transfer_requests r ON r.id=a.request_id
            LEFT JOIN route_attempt_provenance p ON p.resolution_attempt_id=a.id
            WHERE p.resolution_attempt_id IS NULL
            ORDER BY r.transfer_id,a.created_at,a.id""")
        for row in route_rows:
            ordinal_row = await db.fetchone("SELECT COALESCE(MAX(ordinal),0) AS n FROM route_attempt_provenance WHERE transfer_id=?", (row["transfer_id"],))
            ordinal = int(ordinal_row["n"] or 0) + 1
            candidates = ()
            if row.get("result"):
                try:
                    payload = codec.load(row["result"], {})
                    candidates = tuple(codec.candidate(value) for value in payload.get("candidates", []))
                except (TypeError, ValueError, KeyError):
                    candidates = ()
            outcome = "failed" if row["state"] == "failed" else "resolved" if row["state"] == "succeeded" else "unknown"
            await db.execute("""INSERT OR IGNORE INTO route_attempt_provenance(
                resolution_attempt_id,transfer_id,request_id,ordinal,operation,candidate_summary,outcome,history_quality)
                VALUES(?,?,?,?,?,?,?,'legacy_known')""",
                (row["id"], row["transfer_id"], row["request_id"], ordinal, "legacy", cls._candidate_summary(candidates), outcome))

        execution_rows = await db.fetchall("""SELECT e.*,f.status AS artifact_status,f.execution_attempt_id AS current_execution_id,
                f.candidates AS artifact_candidates,f.selected_candidate
            FROM execution_attempts e JOIN download_files f ON f.id=e.artifact_id
            LEFT JOIN execution_attempt_provenance p ON p.execution_attempt_id=e.id
            WHERE p.execution_attempt_id IS NULL
            ORDER BY e.transfer_id,e.artifact_id,e.created_at,e.id""")
        for row in execution_rows:
            candidate = None
            if row.get("candidate"):
                try:
                    candidate = codec.candidate(codec.load(row["candidate"]))
                except (TypeError, ValueError, KeyError):
                    candidate = None
            if candidate is None and row.get("current_execution_id") == row["id"] and row.get("artifact_candidates"):
                try:
                    candidates = [codec.candidate(value) for value in codec.load(row["artifact_candidates"], [])]
                    selected = int(row.get("selected_candidate") or 0)
                    candidate = candidates[selected] if 0 <= selected < len(candidates) else None
                except (TypeError, ValueError, KeyError, IndexError):
                    candidate = None
            provider_id = str(candidate.provider_id) if candidate and candidate.provider_id else None
            route_attempt_id = await cls._candidate_route(
                db, row["transfer_id"], candidate, artifact_id=row["artifact_id"]
            ) if candidate else None
            ordinal_row = await db.fetchone("SELECT COALESCE(MAX(ordinal),0) AS n FROM execution_attempt_provenance WHERE artifact_id=?", (row["artifact_id"],))
            ordinal = int(ordinal_row["n"] or 0) + 1
            delivered = bool(provider_id and row["state"] == "succeeded" and row.get("artifact_status") == "completed" and row.get("current_execution_id") == row["id"])
            await db.execute("""INSERT OR IGNORE INTO execution_attempt_provenance(
                execution_attempt_id,route_attempt_id,transfer_id,artifact_id,ordinal,provider_id,candidate_id,candidate_source,
                outcome,delivered,history_quality) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (row["id"], route_attempt_id, row["transfer_id"], row["artifact_id"], ordinal, provider_id,
                 str(candidate.id) if candidate else None, codec.dump(cls._safe_candidate_source(candidate)) if candidate else None,
                 "completed" if delivered else cls._execution_outcome(row["state"]), int(delivered),
                 "legacy_known" if provider_id else "legacy_unknown"))
            if delivered and route_attempt_id:
                await db.execute("UPDATE route_attempt_provenance SET outcome='completed',updated_at=CURRENT_TIMESTAMP WHERE resolution_attempt_id=?", (route_attempt_id,))

    @classmethod
    async def _begin_route_provenance(cls, db, attempt_id, transfer_id, request_id, provider_id, *, operation):
        previous = await db.fetchone("""SELECT a.id,a.provider_id,a.error,p.ordinal,p.outcome
            FROM resolution_attempts a JOIN route_attempt_provenance p ON p.resolution_attempt_id=a.id
            WHERE a.request_id=? AND a.id!=? ORDER BY p.ordinal DESC LIMIT 1""", (request_id, attempt_id))
        ordinal_row = await db.fetchone(
            "SELECT COALESCE(MAX(ordinal),0) AS n FROM route_attempt_provenance WHERE transfer_id=?", (transfer_id,)
        )
        ordinal = int(ordinal_row["n"] or 0) + 1
        previous_id = previous["id"] if previous else None
        transition_kind = None
        transition_reason = None
        if previous:
            if operation == "refresh":
                transition_kind = "candidate_refresh"
                transition_reason = "candidate_refresh"
            elif previous["provider_id"] != provider_id:
                transition_kind = "provider_change"
                transition_reason = "route_reselected"
            else:
                transition_kind = "resolution_retry"
                transition_reason = "retry"
            error = codec.error(previous.get("error"))
            if error is not None:
                transition_reason = str(error.category.value)
            if previous.get("outcome") in {"started", "resolved", "unknown"}:
                await db.execute("UPDATE route_attempt_provenance SET outcome='superseded',updated_at=CURRENT_TIMESTAMP WHERE resolution_attempt_id=?", (previous_id,))
        await db.execute("""INSERT INTO route_attempt_provenance(
            resolution_attempt_id,transfer_id,request_id,ordinal,operation,previous_attempt_id,transition_kind,transition_reason,
            candidate_summary,outcome,history_quality) VALUES(?,?,?,?,?,?,?,?,?,'started','recorded')""",
            (attempt_id, transfer_id, request_id, ordinal, operation, previous_id, transition_kind, transition_reason, codec.dump([])))

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

    async def presentation(self, transfer_id: int, *, details=False):
        """Explicit canonical read model; opaque integration context stays private."""
        async with get_db() as db:
            row = await db.fetchone("""SELECT id,hash,name,status,size_bytes,progress,local_path,source,label,priority,
                error_message,normalized_error,extraction_status,extraction_error,created_at,updated_at,completed_at
                FROM torrents WHERE id=?""", (transfer_id,))
            if not row:
                return None
            files = await db.fetchall("""SELECT f.id,f.torrent_id,f.filename,f.size_bytes,f.local_path,f.status,f.download_client,
                f.blocked,f.block_reason,f.retry_count,f.mirror_group_id,f.mirror_state,f.updated_at,f.normalized_error,
                e.progress AS execution_progress FROM download_files f
                LEFT JOIN execution_attempts e ON e.id=f.execution_attempt_id WHERE f.torrent_id=? ORDER BY f.id""", (transfer_id,))
            requests = await db.fetchall("""SELECT id,state,error,payload,metadata FROM transfer_requests
                WHERE transfer_id=? ORDER BY CASE WHEN parent_id IS NULL THEN 0 ELSE 1 END,ordinal,id""", (transfer_id,))
            resources = await db.fetchall("SELECT id,provider_id,state FROM provider_resources WHERE transfer_id=?", (transfer_id,))
            providers = await db.fetchall("SELECT DISTINCT a.provider_id FROM resolution_attempts a JOIN transfer_requests r ON r.id=a.request_id WHERE r.transfer_id=?", (transfer_id,))
            route_attempts = await db.fetchall("""SELECT p.resolution_attempt_id AS id,p.request_id,p.ordinal,p.operation,p.previous_attempt_id,
                p.transition_kind,p.transition_reason,p.candidate_summary,p.outcome,p.history_quality,a.provider_id,
                a.state AS resolution_state,a.created_at,a.updated_at FROM route_attempt_provenance p
                JOIN resolution_attempts a ON a.id=p.resolution_attempt_id WHERE p.transfer_id=?
                ORDER BY p.ordinal,p.resolution_attempt_id""", (transfer_id,))
            execution_history = await db.fetchall("""SELECT e.id,e.artifact_id,e.executor_id,e.state AS execution_state,e.created_at,e.updated_at,
                p.route_attempt_id,p.provider_id,p.candidate_id,p.candidate_source,p.ordinal,p.outcome,p.delivered,p.history_quality
                FROM execution_attempt_provenance p JOIN execution_attempts e ON e.id=p.execution_attempt_id
                WHERE p.transfer_id=? ORDER BY p.created_at,p.artifact_id,p.ordinal,p.execution_attempt_id""", (transfer_id,))
            consolidations = await db.fetchall("""SELECT a.contributing_artifact_id,a.source_request_id,a.canonical_artifact_id,
                c.torrent_id AS canonical_transfer_id FROM artifact_consolidations a
                JOIN download_files c ON c.id=a.canonical_artifact_id
                WHERE a.source_transfer_id=? ORDER BY a.contributing_artifact_id""", (transfer_id,))
            candidate_bindings = await db.fetchall("""SELECT b.* FROM canonical_candidate_bindings b
                JOIN download_files f ON f.id=b.canonical_artifact_id
                WHERE f.torrent_id=? AND COALESCE(f.mirror_state,'')!='standby'
                ORDER BY b.canonical_artifact_id,b.candidate_order,b.id""", (transfer_id,))
            candidate_origins = await db.fetchall("""SELECT o.*,b.canonical_artifact_id FROM canonical_candidate_origins o
                JOIN canonical_candidate_bindings b ON b.id=o.binding_id
                JOIN download_files f ON f.id=b.canonical_artifact_id
                WHERE f.torrent_id=? AND COALESCE(f.mirror_state,'')!='standby'
                ORDER BY b.canonical_artifact_id,b.candidate_order,o.id""", (transfer_id,)) if details else []
            events = await db.fetchall("SELECT id,torrent_id,level,message,created_at FROM events WHERE torrent_id=? ORDER BY id DESC LIMIT 50", (transfer_id,)) if details else []
            input_challenge = await db.fetchone("SELECT * FROM transfer_input_challenges WHERE transfer_id=?", (transfer_id,))
        def normalized(item, field="normalized_error"):
            error = codec.error(item.pop(field, None))
            item["error"] = error.as_dict() if error else None
            item["error_message"] = error.message if error else None
            return item
        result = normalized(dict(row))
        result["file_count"] = len(files)
        result["blocked_count"] = sum(bool(item["blocked"]) for item in files)
        result["source_failure_count"] = sum(item["state"] == "failed" for item in requests)
        result["resources"] = [dict(item) for item in resources]
        historical_providers = sorted({item["provider_id"] for item in (*resources, *providers) if item.get("provider_id")})
        delivering_providers = sorted({item["provider_id"] for item in execution_history if item.get("delivered") and item.get("provider_id")})
        current_provider_id = next((item["provider_id"] for item in reversed(route_attempts) if item.get("provider_id")), None)
        result["historical_providers"] = historical_providers
        result["current_provider_id"] = current_provider_id
        result["delivering_provider_ids"] = delivering_providers
        result["delivering_provider_id"] = delivering_providers[0] if len(delivering_providers) == 1 else None
        result["provider_provenance_status"] = "recorded" if delivering_providers else "unknown_legacy" if result["status"] == "completed" else "pending"
        result["providers"] = delivering_providers if result["status"] == "completed" else historical_providers
        result["executors"] = sorted({item["download_client"] for item in files if item["download_client"]})
        result["input_required"] = public_challenge(input_challenge)
        targets = sorted({int(item["canonical_transfer_id"]) for item in consolidations})
        complete_consolidation = result["status"] == "consolidated"
        result["consolidation"] = {
            "state": "complete" if complete_consolidation else "partial" if consolidations else "none",
            "consolidated_into": targets[0] if complete_consolidation and len(targets) == 1 else None,
            "canonical_transfer_ids": targets,
            "artifact_mappings": [dict(item) for item in consolidations],
        }
        if details:
            result["request"] = codec.load(requests[0]["payload"], {}) if requests else None
            result["files"] = []
            for row in files:
                item = normalized(dict(row))
                progress = TransferProgress(**codec.load(item.pop("execution_progress", None), {}))
                item["download_speed"] = progress.bytes_per_second if item["status"] == "downloading" else 0
                item["progress"] = 100 if item["status"] == "completed" else min(100, progress.completed_bytes / item["size_bytes"] * 100) if item["size_bytes"] else 0
                result["files"].append(item)
            result["source_outcomes"] = []
            for item in requests:
                if item["state"] == "failed":
                    request = codec.request(codec.load(item["payload"]))
                    error = codec.error(item["error"])
                    result["source_outcomes"].append({"id": item["id"], "name": request.name or "Source request", "status": "error",
                        "error": error.as_dict() if error else None, "error_message": error.message if error else None})
            result["route_attempts"] = []
            for row in route_attempts:
                item = dict(row)
                item["candidates"] = codec.load(item.pop("candidate_summary"), [])
                result["route_attempts"].append(item)
            result["execution_attempts"] = []
            for row in execution_history:
                item = dict(row)
                item["candidate_source"] = codec.load(item.get("candidate_source"), None)
                item["delivered"] = bool(item.get("delivered"))
                result["execution_attempts"].append(item)
            origins_by_binding = {}
            for origin in candidate_origins:
                origins_by_binding.setdefault(int(origin["binding_id"]), []).append({
                    "contributing_artifact_id": int(origin["contributing_artifact_id"]),
                    "contributing_transfer_id": int(origin["contributing_transfer_id"]),
                    "request_id": origin["request_id"],
                    "resolution_attempt_id": origin["resolution_attempt_id"],
                    "discovered_candidate_id": origin["discovered_candidate_id"],
                })
            result["candidate_bindings"] = []
            for binding in candidate_bindings:
                source_identity = None
                if binding.get("source_scope") and binding.get("source_key"):
                    source_identity = {"scope": binding["source_scope"], "key": binding["source_key"]}
                result["candidate_bindings"].append({
                    "canonical_artifact_id": int(binding["canonical_artifact_id"]),
                    "candidate_id": binding["candidate_id"],
                    "provider_id": binding["provider_id"],
                    "source_identity": source_identity,
                    "role": binding["role"],
                    "candidate_order": int(binding["candidate_order"]),
                    "origins": origins_by_binding.get(int(binding["id"]), []),
                })
            result["events"] = [dict(item) for item in events]
        return result

    async def aggregate_metadata(self, transfer_id, *, total_bytes, local_path):
        async with get_db() as db:
            await db.execute("UPDATE torrents SET size_bytes=?,local_path=? WHERE id=? AND status NOT IN ('deleted','consolidated')", (total_bytes, local_path, transfer_id))
            await db.commit()

    async def update_metadata(self, transfer_id, *, label=None, priority=None):
        async with get_db() as db:
            if not await db.fetchone("SELECT id FROM torrents WHERE id=?", (transfer_id,)):
                raise KeyError(transfer_id)
            await db.execute("UPDATE torrents SET label=COALESCE(?,label),priority=COALESCE(?,priority),updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (label, priority, transfer_id))
            await db.commit()

    async def active(self) -> tuple[Transfer, ...]:
        async with get_db() as db:
            rows = await db.fetchall("""SELECT t.*, COALESCE(p.paused,0) AS paused_intent FROM torrents t
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id
                WHERE t.status NOT IN ('completed','consolidated','deleted','cancelled')
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
            if created:
                await db.execute("INSERT INTO events(torrent_id,level,message) VALUES(?,'info','Transfer accepted')", (transfer_id,))
                await db.execute("INSERT INTO application_events(transfer_id,kind) VALUES(?,'accepted')", (transfer_id,))
            await db.commit()
        return await self.get(transfer_id), created

    async def state(self, transfer_id: int, target: TransferState, *, progress=None, error=None, operator=False, expected_epoch=None, verified=False) -> bool:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT status,progress,lifecycle_epoch,normalized_error FROM torrents WHERE id=?", (transfer_id,))
            if not row or not transition_allowed(TransferState(row["status"]), target, operator=operator, verified=verified):
                return False
            if expected_epoch is not None and row["lifecycle_epoch"] != expected_epoch:
                return False
            if row["status"] == target and (progress is None or row["progress"] == progress) and row["normalized_error"] == (codec.dump(error) if error else None):
                return True
            await db.execute("""UPDATE torrents SET status=?, progress=COALESCE(?,progress), normalized_error=?,
                error_message=?, updated_at=CURRENT_TIMESTAMP,
                completed_at=CASE WHEN ?='completed' THEN COALESCE(completed_at,CURRENT_TIMESTAMP)
                    WHEN ? IN ('pending','queued') THEN NULL ELSE completed_at END WHERE id=?""",
                (target, progress, codec.dump(error) if error else None, error.message if error else None, target, target, transfer_id))
            if row["status"] != target or row["normalized_error"] != (codec.dump(error) if error else None):
                message = f"Transfer {target}" + (f": {error.message}" if error else "")
                await db.execute("INSERT INTO events(torrent_id,level,message) VALUES(?,?,?)", (transfer_id, "error" if error else "info", message))
                await db.execute("INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,?,?)", (transfer_id, target, error.message if error else None))
            await db.commit()
        return True

    async def cancel_with_execution_cleanup(self, transfer_id: int, *, expected_epoch: int, now: float) -> bool:
        """Atomically close logical lifecycle and persist external cleanup responsibility."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT status,progress,lifecycle_epoch,normalized_error FROM torrents WHERE id=?",
                (transfer_id,),
            )
            if not row:
                return False
            current = TransferState(row["status"])
            if current == TransferState.CANCELLED:
                await db.commit()
                return True
            if not transition_allowed(current, TransferState.CANCELLED) or row["lifecycle_epoch"] != expected_epoch:
                return False

            await db.execute(
                """UPDATE torrents SET status='cancelled',normalized_error=NULL,error_message=NULL,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (transfer_id,),
            )
            await db.execute(
                "INSERT INTO events(torrent_id,level,message) VALUES(?,'info','Transfer cancelled')",
                (transfer_id,),
            )
            await db.execute(
                "INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,'cancelled',NULL)",
                (transfer_id,),
            )
            await db.execute(
                """UPDATE execution_attempts SET cleanup_state='pending',cleanup_attempts=0,
                    cleanup_retry_at=?,cleanup_error=NULL
                    WHERE transfer_id=? AND authorized=1
                    AND state IN ('prepared','queued','transferring','paused','unknown')
                    AND id IN (SELECT execution_attempt_id FROM download_files
                        WHERE torrent_id=? AND execution_attempt_id IS NOT NULL)""",
                (now, transfer_id, transfer_id),
            )
            await db.execute(
                """UPDATE download_files SET status='cancelled',normalized_error=NULL,updated_at=CURRENT_TIMESTAMP
                    WHERE torrent_id=? AND status!='completed'""",
                (transfer_id,),
            )
            await db.commit()
        return True

    async def pending_execution_cleanup(self, now: float, *, transfer_id: int | None = None):
        async with get_db() as db:
            where_transfer = " AND e.transfer_id=?" if transfer_id is not None else ""
            params = (now, transfer_id) if transfer_id is not None else (now,)
            rows = await db.fetchall(
                """SELECT e.* FROM execution_attempts e JOIN torrents t ON t.id=e.transfer_id
                    WHERE e.cleanup_state IN ('pending','blocked') AND e.cleanup_retry_at<=?
                    AND t.status IN ('cancelled','deleted')""" + where_transfer +
                " ORDER BY e.transfer_id,e.created_at,e.id",
                params,
            )
        return tuple((self._execution_attempt(row), int(row["cleanup_attempts"] or 0), codec.error(row["cleanup_error"])) for row in rows)

    async def claim_execution_cleanup(self, attempt_id: str, *, now: float, lease_until: float) -> bool:
        async with get_db() as db:
            cursor = await db.execute(
                """UPDATE execution_attempts SET cleanup_state='pending',cleanup_retry_at=?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND cleanup_state IN ('pending','blocked') AND cleanup_retry_at<=?""",
                (lease_until, attempt_id, now),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def execution_cleanup_attempt(self, attempt_id: str) -> bool:
        """Consume one destructive executor-cancel attempt after a cleanup lease is held."""
        async with get_db() as db:
            cursor = await db.execute(
                """UPDATE execution_attempts SET cleanup_attempts=cleanup_attempts+1,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND cleanup_state='pending'""",
                (attempt_id,),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def execution_cleanup_retry(self, attempt_id: str, error: NormalizedError, retry_at: float | None) -> None:
        async with get_db() as db:
            await db.execute(
                """UPDATE execution_attempts SET cleanup_state=?,cleanup_error=?,cleanup_retry_at=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=? AND cleanup_state='pending'""",
                ("pending" if retry_at is not None else "blocked", codec.dump(error), retry_at or 0, attempt_id),
            )
            await db.commit()

    async def execution_cleanup_complete(self, attempt_id: str) -> None:
        async with get_db() as db:
            await db.execute(
                """UPDATE execution_attempts SET cleanup_state='complete',cleanup_error=NULL,cleanup_retry_at=0,
                    authorized=0,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (attempt_id,),
            )
            await db.commit()

    async def execution_cleanup_status(self, attempt_id: str):
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT cleanup_state,cleanup_attempts,cleanup_retry_at,cleanup_error,authorized,state FROM execution_attempts WHERE id=?",
                (attempt_id,),
            )
        if not row:
            return None
        return {
            "state": row["cleanup_state"],
            "attempts": int(row["cleanup_attempts"] or 0),
            "retry_at": float(row["cleanup_retry_at"] or 0),
            "error": codec.error(row["cleanup_error"]),
            "authorized": bool(row["authorized"]),
            "execution_state": row["state"],
        }

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

    async def bound_route_provider(self, request_id: str) -> str | None:
        """Return the provider owning the latest durable route for this request."""
        async with get_db() as db:
            row = await db.fetchone(
                """SELECT a.provider_id FROM route_attempt_provenance p
                JOIN resolution_attempts a ON a.id=p.resolution_attempt_id
                WHERE a.request_id=? ORDER BY p.ordinal DESC LIMIT 1""",
                (request_id,),
            )
        return str(row["provider_id"]) if row and row.get("provider_id") else None

    async def begin_resolution(self, request_id: str, provider_id: str) -> ResolutionAttempt | None:
        identity = new_identity()
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT r.* FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id WHERE r.id=? AND r.state='pending'
                AND t.status NOT IN ('deleted','completed','consolidated','cancelled') AND COALESCE(p.paused,0)=0""", (request_id,))
            if not row:
                return None
            await db.execute("UPDATE transfer_requests SET state='resolving',attempts=attempts+1 WHERE id=?", (request_id,))
            await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES(?,?,?,'started')", (identity, request_id, provider_id))
            await self._begin_route_provenance(db, identity, row["transfer_id"], request_id, provider_id, operation="resolve")
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
        # Defense in depth: route identity is selected by the universal core.
        identities = [candidate.provider_id for candidate in result.candidates]
        identities.extend(candidate.resource.provider_id for candidate in result.candidates if candidate.resource)
        if result.observation:
            identities.append(result.observation.resource.provider_id)
        if any(identity and identity != attempt.provider_id for identity in identities):
            raise TransferError(NormalizedError(
                Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION,
                integration_id=attempt.provider_id,
            ))
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
            await db.execute("UPDATE resolution_attempts SET state=?,error=?,result=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, error, codec.dump(result), attempt.id))
            await db.execute("""UPDATE route_attempt_provenance SET outcome=?,candidate_summary=?,updated_at=CURRENT_TIMESTAMP
                WHERE resolution_attempt_id=?""",
                ("failed" if result.error else "resolved", self._candidate_summary(result.candidates), attempt.id))
            resource = codec.dump(result.observation.resource) if result.observation else None
            request_state = "failed" if result.error else "waiting" if result.observation and not result.candidates else "materializing" if result.candidates else "resolved"
            if row["status"] not in {"deleted", "completed", "consolidated", "cancelled"}:
                await db.execute("UPDATE transfer_requests SET state=?,resource=COALESCE(?,resource),error=? WHERE id=?",
                                 (request_state, resource, error, attempt.request_id))
            await db.commit()
        return row["status"] not in {"deleted", "completed", "consolidated", "cancelled"}

    async def request_failure(self, request_id: str, error: NormalizedError, retry_at: float | None, *, retry_state="pending", consume_attempt=False) -> None:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            error_blob = codec.dump(error)
            started = await db.fetchall("SELECT id FROM resolution_attempts WHERE request_id=? AND state='started'", (request_id,))
            for item in started:
                await db.execute("UPDATE resolution_attempts SET state='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (error_blob, item["id"]))
                await db.execute("UPDATE route_attempt_provenance SET outcome='failed',updated_at=CURRENT_TIMESTAMP WHERE resolution_attempt_id=?", (item["id"],))
            await db.execute("""UPDATE transfer_requests SET state=?,error=?,retry_at=?,attempts=attempts+? WHERE id=?
                AND transfer_id IN (SELECT id FROM torrents WHERE status NOT IN ('deleted','completed','consolidated','cancelled'))""",
                (retry_state if retry_at is not None else "failed", error_blob, retry_at or 0, int(consume_attempt), request_id))
            await db.commit()

    async def manifest(self, record: RequestRecord, entries: tuple[SourceEntry, ...]) -> None:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            parent = await db.fetchone("SELECT status FROM torrents WHERE id=?", (record.transfer_id,))
            if not parent or parent["status"] in {"deleted", "completed", "consolidated", "cancelled"}:
                return
            for ordinal, entry in enumerate(entries):
                identity = uuid5(NAMESPACE_URL, f"request:{record.id}:{entry.relative_path}").hex
                await db.execute("""INSERT OR IGNORE INTO transfer_requests(id,transfer_id,parent_id,ordinal,payload,metadata)
                    VALUES(?,?,?,?,?,?)""", (identity, record.transfer_id, record.id, ordinal, codec.dump(entry.request), codec.dump(entry)))
                await db.execute("""UPDATE transfer_requests SET payload=?,metadata=?,state=CASE WHEN state='waiting_parent' THEN 'pending' ELSE state END
                    WHERE id=?""", (codec.dump(entry.request), codec.dump(entry), identity))
            missing_error = NormalizedError(Domain.RESOLUTION, Category.SOURCE_NOT_FOUND, Stage.RESOLUTION)
            missing = await db.fetchall("SELECT id FROM transfer_requests WHERE parent_id=? AND state='waiting_parent'", (record.id,))
            for child in missing:
                await db.execute("UPDATE transfer_requests SET state='failed',error=? WHERE id=?", (codec.dump(missing_error), child["id"]))
                await db.execute("UPDATE download_files SET status='error',normalized_error=? WHERE request_id=? AND status!='completed'", (codec.dump(missing_error), child["id"]))
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
            if not parent or parent["status"] in {"deleted", "completed", "consolidated", "cancelled"}:
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
            await db.execute("UPDATE transfer_requests SET state='resolved',error=NULL WHERE id=?", (record.id,))
            await db.commit()
        return next(item for item in await self.artifacts(record.transfer_id) if item.request_id == record.id)

    async def artifacts(self, transfer_id: int) -> tuple[Artifact, ...]:
        async with get_db() as db:
            rows = await db.fetchall("""SELECT f.*,e.handle FROM download_files f
                LEFT JOIN execution_attempts e ON e.id=f.execution_attempt_id
                WHERE f.torrent_id=? AND f.request_id IS NOT NULL AND COALESCE(f.blocked,0)=0
                AND COALESCE(f.mirror_state,'')!='standby' ORDER BY f.id""", (transfer_id,))
        return tuple(Artifact(row["id"], transfer_id, row["request_id"], row["filename"], row["local_path"], row["size_bytes"] or 0,
                              row["status"], tuple(codec.candidate(item) for item in codec.load(row["candidates"], [])),
                              row["selected_candidate"], codec.handle(codec.load(row["handle"])), row["retry_count"] or 0,
                              row["retry_at"], codec.error(row["normalized_error"])) for row in rows)

    async def occupied_paths(self) -> set[str]:
        async with get_db() as db:
            rows = await db.fetchall("""SELECT f.local_path FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                WHERE f.local_path IS NOT NULL AND COALESCE(f.mirror_state,'')!='standby'
                AND (t.status NOT IN ('deleted','completed','consolidated','error')
                    OR EXISTS (SELECT 1 FROM execution_attempts e WHERE e.id=f.execution_attempt_id AND e.authorized=1
                        AND e.state IN ('prepared','queued','transferring','paused','unknown')))""")
        return {str(row["local_path"]).casefold() for row in rows}

    async def prepare_execution(self, artifact: Artifact, handle: ExecutionHandle, *, from_input_required: bool = False) -> bool:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT f.* FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id
                WHERE f.id=? AND f.status=? AND f.execution_attempt_id IS NULL
                AND t.status NOT IN ('deleted','completed','consolidated','cancelled') AND COALESCE(p.paused,0)=0""",
                (artifact.id, "input_required" if from_input_required else "queued"))
            if not row:
                return False
            candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
            route_attempt_id = await self._candidate_route(
                db, artifact.transfer_id, candidate, artifact_id=artifact.id
            )
            ordinal_row = await db.fetchone("SELECT COALESCE(MAX(ordinal),0) AS n FROM execution_attempt_provenance WHERE artifact_id=?", (artifact.id,))
            ordinal = int(ordinal_row["n"] or 0) + 1
            await db.execute("""INSERT INTO execution_attempts(id,transfer_id,artifact_id,executor_id,handle,state,candidate)
                VALUES(?,?,?,?,?,'prepared',?)""", (handle.attempt_id, artifact.transfer_id, artifact.id, handle.executor_id, codec.dump(handle),
                codec.dump(candidate) if candidate else None))
            await db.execute("""INSERT INTO execution_attempt_provenance(
                execution_attempt_id,route_attempt_id,transfer_id,artifact_id,ordinal,provider_id,candidate_id,candidate_source,
                outcome,delivered,history_quality) VALUES(?,?,?,?,?,?,?,?, 'prepared',0,'recorded')""",
                (handle.attempt_id, route_attempt_id, artifact.transfer_id, artifact.id, ordinal,
                 candidate.provider_id if candidate and candidate.provider_id else None, str(candidate.id) if candidate else None,
                 codec.dump(self._safe_candidate_source(candidate)) if candidate else None))
            await db.execute("""UPDATE download_files SET execution_attempt_id=?,download_client=?,retry_count=retry_count+1,
                status=CASE WHEN ? THEN 'queued' ELSE status END,normalized_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (handle.attempt_id, handle.executor_id, int(from_input_required), artifact.id))
            await db.commit()
        return True

    async def authorize_execution(self, handle: ExecutionHandle, action: str) -> bool:
        async with get_db() as db:
            row = await db.fetchone("""SELECT e.*,t.status AS transfer_status,COALESCE(p.paused,0) AS paused_intent,
                f.execution_attempt_id AS current_execution_id
                FROM execution_attempts e JOIN torrents t ON t.id=e.transfer_id
                JOIN download_files f ON f.id=e.artifact_id
                LEFT JOIN transfer_pause_intents p ON p.torrent_id=t.id WHERE e.id=?""", (handle.attempt_id,))
        if not row or not row["authorized"] or row["executor_id"] != handle.executor_id or codec.load(row["handle"]) != codec.load(codec.dump(handle)):
            return False
        is_current = row.get("current_execution_id") == handle.attempt_id
        if action in {"start", "resume", "pause"} and not is_current:
            return False
        if action == "cancel" and not is_current:
            cleanup_owned = row["transfer_status"] in {"deleted", "cancelled"} and row.get("cleanup_state") in {"pending", "blocked"}
            if not cleanup_owned:
                return False
        if action in {"start", "resume"} and (row["transfer_status"] in {"deleted", "completed", "consolidated", "cancelled"} or row["paused_intent"]):
            return False
        if action in {"start", "resume"} and await self.globally_paused():
            return False
        return action != "start" or row["state"] == "prepared"

    async def execution_idle_seconds(self, observation, now):
        """Durable activity clock; repeated snapshots never manufacture progress."""
        async with get_db() as db:
            row = await db.fetchone("SELECT state,progress,progress_at,artifact_id FROM execution_attempts WHERE id=?", (observation.handle.attempt_id,))
            if not row:
                return 0
            previous = TransferProgress(**codec.load(row["progress"], {}))
            active = observation.state == ExecutionState.TRANSFERRING and observation.error is None
            progressed = observation.progress.completed_bytes > previous.completed_bytes
            changed = previous.completed_bytes != observation.progress.completed_bytes or row["state"] != observation.state
            if progressed:
                await db.execute("UPDATE download_files SET recovery_failures=0,recovery_refreshes=0 WHERE id=?", (row["artifact_id"],))
            if row["progress_at"] is None or not active or changed:
                await db.execute("UPDATE execution_attempts SET progress_at=? WHERE id=?", (now, observation.handle.attempt_id))
                await db.commit()
                return 0
            if progressed:
                await db.commit()
            return max(0, now - row["progress_at"])

    async def execution(self, observation: ExecutionObservation) -> None:
        handle = observation.handle
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT * FROM execution_attempts WHERE id=?", (handle.attempt_id,))
            if not row or codec.load(row["handle"]) != codec.load(codec.dump(handle)):
                raise TransferError(NormalizedError(Domain.LIFECYCLE, Category.OWNERSHIP_CONFLICT, Stage.RECONCILIATION))
            previous = TransferProgress(**codec.load(row["progress"], {}))
            if observation.progress.completed_bytes > previous.completed_bytes:
                await db.execute("UPDATE download_files SET recovery_failures=0,recovery_refreshes=0 WHERE id=?", (row["artifact_id"],))
            error = codec.dump(observation.error) if observation.error else None
            revoked = observation.error is not None and observation.error.category == Category.OWNERSHIP_CONFLICT
            await db.execute("""UPDATE execution_attempts SET state=?,progress=?,error=?,authorized=CASE WHEN ? THEN 0 ELSE authorized END,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""", (observation.state, codec.dump(observation.progress), error, revoked, handle.attempt_id))
            await db.execute("UPDATE execution_attempt_provenance SET outcome=?,updated_at=CURRENT_TIMESTAMP WHERE execution_attempt_id=?",
                             (self._execution_outcome(observation.state), handle.attempt_id))
            states = {ExecutionState.TRANSFERRING: "downloading", ExecutionState.QUEUED: "queued", ExecutionState.PAUSED: "paused",
                      ExecutionState.SUCCEEDED: "verifying", ExecutionState.FAILED: "error", ExecutionState.CANCELLED: "cancelled",
                      ExecutionState.ABSENT: "lost", ExecutionState.UNKNOWN: "unknown"}
            await db.execute("""UPDATE download_files SET status=?,normalized_error=?,updated_at=CURRENT_TIMESTAMP
                WHERE execution_attempt_id=? AND torrent_id IN (SELECT id FROM torrents WHERE status NOT IN ('deleted','consolidated','cancelled'))""",
                (states[observation.state], error, handle.attempt_id))
            await db.commit()

    async def artifact_state(self, artifact_id: int, state: str, *, error=None, retry_at=0, release=False, selected=None, expected_bytes=None):
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            current = await db.fetchone("SELECT execution_attempt_id FROM download_files WHERE id=?", (artifact_id,))
            cursor = await db.execute("""UPDATE download_files SET status=?,normalized_error=?,retry_at=?,
                execution_attempt_id=CASE WHEN ? THEN NULL ELSE execution_attempt_id END,
                selected_candidate=COALESCE(?,selected_candidate),size_bytes=COALESCE(?,size_bytes),updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND torrent_id IN (SELECT id FROM torrents
                    WHERE status NOT IN ('deleted','consolidated') AND (status!='cancelled' OR ?='cancelled'))""",
                (state, codec.dump(error) if error else None, retry_at, release, selected, expected_bytes, artifact_id, state))
            if cursor.rowcount and release and current and current.get("execution_attempt_id"):
                await db.execute("""UPDATE execution_attempts SET authorized=0,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND state IN ('failed','absent','cancelled','succeeded')""", (current["execution_attempt_id"],))
            if cursor.rowcount and state == "completed" and current and current.get("execution_attempt_id"):
                execution_id = current["execution_attempt_id"]
                await db.execute("""UPDATE execution_attempt_provenance SET delivered=1,outcome='completed',updated_at=CURRENT_TIMESTAMP
                    WHERE execution_attempt_id=?""", (execution_id,))
                route = await db.fetchone("SELECT route_attempt_id FROM execution_attempt_provenance WHERE execution_attempt_id=?", (execution_id,))
                if route and route.get("route_attempt_id"):
                    await db.execute("UPDATE route_attempt_provenance SET outcome='completed',updated_at=CURRENT_TIMESTAMP WHERE resolution_attempt_id=?",
                                     (route["route_attempt_id"],))
            await db.commit()

    async def executions(self, transfer_id: int | None = None) -> tuple[ExecutionAttempt, ...]:
        async with get_db() as db:
            rows = await db.fetchall("SELECT * FROM execution_attempts" + (" WHERE transfer_id=?" if transfer_id is not None else ""),
                                     (transfer_id,) if transfer_id is not None else ())
        return tuple(self._execution_attempt(row) for row in rows)

    @staticmethod
    def _execution_attempt(row):
        return ExecutionAttempt(codec.handle(codec.load(row["handle"])), row["transfer_id"], row["artifact_id"], row["state"],
                                TransferProgress(**codec.load(row["progress"], {})), codec.error(row["error"]),
                                codec.candidate(codec.load(row["candidate"])) if row.get("candidate") else None)

    async def live_executions(self):
        async with get_db() as db:
            rows = await db.fetchall("""SELECT e.* FROM execution_attempts e
                JOIN download_files f ON f.execution_attempt_id=e.id JOIN torrents t ON t.id=e.transfer_id
                WHERE t.status NOT IN ('deleted','completed','consolidated','cancelled') AND e.authorized=1""")
        return tuple(self._execution_attempt(row) for row in rows)

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

    async def retry_requests(self, transfer_id: int, *, request_id=None, reset_budget=False):
        async with get_db() as db:
            await db.execute("UPDATE transfer_requests SET state='pending',retry_at=0,error=NULL,attempts=CASE WHEN ? THEN 0 ELSE attempts END WHERE transfer_id=? AND transfer_id IN (SELECT id FROM torrents WHERE status NOT IN ('completed','consolidated','deleted','cancelled')) AND " +
                             ("id=?" if request_id else "state='failed'"), (reset_budget, transfer_id, request_id) if request_id else (reset_budget, transfer_id))
            await db.commit()

    async def renew_parent(self, record, retry_at, *, reset_budget=False):
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("""UPDATE transfer_requests SET state='pending',retry_at=?,error=NULL,attempts=CASE WHEN ? THEN 0 ELSE attempts END
                WHERE id=? AND transfer_id IN (SELECT id FROM torrents WHERE status NOT IN ('completed','consolidated','deleted','cancelled'))""",
                (retry_at, reset_budget, record.id))
            await db.execute("""UPDATE transfer_requests SET state='waiting_parent',error=NULL WHERE parent_id=? AND id IN
                (SELECT request_id FROM download_files WHERE status IN ('error','unresolved','lost','cancelled'))
                AND transfer_id IN (SELECT id FROM torrents WHERE status NOT IN ('completed','consolidated','deleted','cancelled'))""", (record.id,))
            await db.commit()

    async def recovery_budget(self, artifact_id: int) -> tuple[int, int]:
        async with get_db() as db:
            row = await db.fetchone("SELECT recovery_failures,recovery_refreshes FROM download_files WHERE id=?", (artifact_id,))
        if not row:
            raise KeyError(artifact_id)
        return int(row["recovery_failures"] or 0), int(row["recovery_refreshes"] or 0)

    async def record_source_failure(self, artifact_id: int) -> tuple[int, int]:
        """Consume one failure in the current no-progress source-recovery episode."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("UPDATE download_files SET recovery_failures=recovery_failures+1 WHERE id=?", (artifact_id,))
            if cursor.rowcount != 1:
                await db.rollback()
                raise KeyError(artifact_id)
            row = await db.fetchone("SELECT recovery_failures,recovery_refreshes FROM download_files WHERE id=?", (artifact_id,))
            await db.commit()
        return int(row["recovery_failures"] or 0), int(row["recovery_refreshes"] or 0)

    async def consume_recovery_refresh(self, artifact_id: int) -> bool:
        """Allow at most one automatic candidate refresh per no-progress episode."""
        async with get_db() as db:
            cursor = await db.execute("""UPDATE download_files SET recovery_refreshes=recovery_refreshes+1
                WHERE id=? AND recovery_refreshes<1""", (artifact_id,))
            await db.commit()
        return cursor.rowcount == 1

    async def reset_source_recovery(self, artifact_id: int) -> None:
        async with get_db() as db:
            await db.execute("UPDATE download_files SET recovery_failures=0,recovery_refreshes=0 WHERE id=?", (artifact_id,))
            await db.commit()

    async def reset_retry_budget(self, artifact_id):
        async with get_db() as db:
            await db.execute("""UPDATE download_files SET retry_count=0,recovery_failures=0,recovery_refreshes=0
                WHERE id=?""", (artifact_id,))
            await db.commit()

    async def reset_postprocessing(self, transfer_id):
        async with get_db() as db:
            if await db.fetchone("SELECT transfer_id FROM postprocess_attempts WHERE transfer_id=? AND state IN ('pending','processing')", (transfer_id,)):
                return False
            await db.execute("DELETE FROM postprocess_attempts WHERE transfer_id=?", (transfer_id,))
            await db.commit()
        return True

    async def globally_paused(self) -> bool:
        async with get_db() as db:
            row = await db.fetchone("SELECT value FROM transfer_controls WHERE key='paused'")
        return bool(row and row["value"] == "1")

    async def global_pause(self, paused: bool):
        async with get_db() as db:
            await db.execute("""INSERT INTO transfer_controls(key,value) VALUES('paused',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", ("1" if paused else "0",))
            await db.commit()

    async def delete(self, transfer_id: int, *, remote: bool, now: float = 0) -> None:
        """Atomically tombstone a transfer and retain responsibility for launched executions."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("""UPDATE torrents SET status='deleted',delete_remote=?,lifecycle_epoch=lifecycle_epoch+1,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""", (int(remote), transfer_id))
            await db.execute(
                """UPDATE execution_attempts SET cleanup_state='pending',
                    cleanup_attempts=CASE WHEN cleanup_state IN ('pending','blocked') THEN cleanup_attempts ELSE 0 END,
                    cleanup_retry_at=CASE
                        WHEN cleanup_state IN ('pending','blocked') AND cleanup_retry_at>? THEN cleanup_retry_at
                        ELSE ? END,
                    cleanup_error=CASE WHEN cleanup_state IN ('pending','blocked') THEN cleanup_error ELSE NULL END,
                    updated_at=CURRENT_TIMESTAMP
                    WHERE transfer_id=? AND authorized=1
                    AND state IN ('prepared','queued','transferring','paused','unknown')""",
                (now, now, transfer_id),
            )
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
                AND transfer_id IN (SELECT id FROM torrents WHERE status NOT IN ('completed','consolidated','deleted'))""",
                (codec.dump(resource), transfer_id))
            await db.commit()

    async def begin_refresh(self, record: RequestRecord, provider_id: str):
        identity = new_identity()
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            # Refresh is deliberately allowed for a request owned by a fully
            # consolidated source transfer: that request still owns the foreign
            # candidate's acquisition provenance even though its submission has
            # no independent scheduling authority.
            await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES(?,?,?,'started')",
                             (identity, record.id, provider_id))
            await self._begin_route_provenance(db, identity, record.transfer_id, record.id, provider_id, operation="refresh")
            await db.commit()
        return ResolutionAttempt(identity, record.id, provider_id, "started")

    async def resolved_candidates(self, request_id: str):
        async with get_db() as db:
            row = await db.fetchone("""SELECT a.result FROM resolution_attempts a
                LEFT JOIN route_attempt_provenance p ON p.resolution_attempt_id=a.id
                WHERE a.request_id=? AND a.state='succeeded'
                ORDER BY COALESCE(p.ordinal,0) DESC,a.updated_at DESC,a.id DESC LIMIT 1""", (request_id,))
        result = codec.load(row["result"], {}) if row else {}
        return tuple(codec.candidate(value) for value in result.get("candidates", []))

    async def poll_after(self, request_id: str, timestamp: float, *, waiting=False):
        async with get_db() as db:
            await db.execute("UPDATE transfer_requests SET retry_at=?,state=CASE WHEN ? THEN 'waiting' ELSE state END WHERE id=?", (timestamp, waiting, request_id))
            await db.commit()

    async def add_alternate(self, primary: Artifact, record: RequestRecord, candidates, size: int):
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            current = await db.fetchone("""SELECT f.* FROM download_files f JOIN torrents t ON t.id=f.torrent_id
                WHERE f.id=? AND t.status NOT IN ('deleted','consolidated')""", (primary.id,))
            if not current:
                return False
            retained = [replace(codec.candidate(item), expected_bytes=size) for item in codec.load(current["candidates"], [])]
            alternatives = tuple(replace(item, expected_bytes=size) for item in candidates)
            retained.extend(alternatives)
            await db.execute("UPDATE download_files SET candidates=?,size_bytes=?,mirror_group_id=?,mirror_state='primary' WHERE id=?",
                             (codec.dump(retained), size, primary.id, primary.id))
            await db.execute("""INSERT INTO download_files(torrent_id,request_id,filename,size_bytes,local_path,status,blocked,
                mirror_group_id,mirror_state,candidates,download_client) VALUES(?,?,?,?,?,'duplicate',NULL,?,'standby',?,'')""",
                (record.transfer_id, record.id, alternatives[0].name, size, primary.target, primary.id, codec.dump(alternatives)))
            await db.execute("UPDATE transfer_requests SET state='resolved',error=NULL WHERE id=?", (record.id,))
            await db.commit()
        return True

    async def select_artifact(self, transfer_id: int, artifact_id: int, selected: bool):
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT * FROM download_files WHERE id=? AND torrent_id=?", (artifact_id, transfer_id))
            if not row or not row["request_id"]:
                raise KeyError(artifact_id)
            transfer = await db.fetchone("SELECT status FROM torrents WHERE id=?", (transfer_id,))
            if not transfer or transfer["status"] == "consolidated":
                raise TransferError(NormalizedError(Domain.LIFECYCLE, Category.RESOURCE_STATE_CONFLICT, Stage.QUEUE))
            if bool(row["blocked"]) == (not selected):
                return
            if row["execution_attempt_id"] or row["status"] not in {"queued", "unresolved", "paused", "blocked", "pending"}:
                raise TransferError(NormalizedError(Domain.LIFECYCLE, Category.RESOURCE_STATE_CONFLICT, Stage.QUEUE))
            ready = bool(codec.load(row["candidates"], []))
            await db.execute("UPDATE download_files SET blocked=?,status=? WHERE id=?",
                             (int(not selected), ("queued" if ready else "unresolved") if selected else "blocked", artifact_id))
            await db.execute("UPDATE transfer_requests SET state=? WHERE id=?",
                             (("resolved" if ready else "pending") if selected else "skipped", row["request_id"]))
            await db.commit()

    async def blocked_artifact_count(self, transfer_id: int):
        async with get_db() as db:
            row = await db.fetchone("SELECT COUNT(*) AS n FROM download_files WHERE torrent_id=? AND blocked=1", (transfer_id,))
        return int(row["n"])
