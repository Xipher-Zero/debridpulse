"""Canonical transfer persistence, including durable recovery accounting.

The qualified base remains the owner of ordinary transfer/request persistence.
This public owner contains the atomic recovery extensions, progress-aware epoch
accounting, candidate provenance presentation, and execution-discovered size
acceptance. Recovery snapshots use the existing durable ``application_events``
table and are marked claimed so they are state history, not work-queue events.
"""
from __future__ import annotations

from db.database import get_db
from transfers import codec
from transfers import file_selection as fs
from transfers._repository_base import TransferRepository as _QualifiedTransferRepository
from transfers.errors import Category, Domain, NormalizedError, Stage, TransferError
from transfers.models import ExecutionState, TransferProgress
from transfers.policy import failure_signature, meaningful_progress_threshold


_TERMINAL_EXECUTION_STATES = frozenset({"failed", "absent", "cancelled", "succeeded"})
_MUTATING_EXECUTION_STATES = frozenset({"prepared", "queued", "transferring", "paused", "unknown"})
_RUNTIME_TOTAL_STATES = frozenset({
    ExecutionState.QUEUED,
    ExecutionState.TRANSFERRING,
    ExecutionState.PAUSED,
})
_FAILED_CANDIDATE_OUTCOMES = frozenset({"failed", "error", "rejected", "absent"})


def _safe_source_label(scope, key) -> str:
    """Project durable source identity without exposing opaque candidate keys."""
    normalized_scope = str(scope or "").strip().lower()
    normalized_key = str(key or "").strip()
    if normalized_scope == "host" and normalized_key:
        return normalized_key.lower()
    if normalized_scope in {"scheme", "protocol"} and normalized_key:
        return normalized_key.upper()
    return "Source"


class TransferRepository(_QualifiedTransferRepository):
    @staticmethod
    def _recovery_event_kind(artifact_id: int) -> str:
        return f"transfer_recovery:{int(artifact_id)}"

    @classmethod
    async def _recovery_snapshot(cls, db, artifact_id: int, *, row=None) -> dict:
        if row is None:
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (artifact_id,),
            )
        if not row:
            raise KeyError(artifact_id)
        snapshot = {
            "version": 2,
            "recovery_epoch": 0,
            "progress_anchor": None,
            "consecutive_no_progress_failures": int(row.get("recovery_failures") or 0),
            "failures_since_meaningful_progress": int(row.get("recovery_failures") or 0),
            "failure_signature": None,
            "same_signature_failures": 0,
            "candidate_refreshes": int(row.get("recovery_refreshes") or 0),
            "candidate_switches": 0,
            "decision_action": None,
            "decision_reason": None,
            "quiescence_reason": None,
            "wake_condition": None,
        }
        event = await db.fetchone(
            "SELECT detail FROM application_events WHERE kind=? ORDER BY id DESC LIMIT 1",
            (cls._recovery_event_kind(artifact_id),),
        )
        if event and event.get("detail"):
            try:
                stored = codec.load(event["detail"], {})
            except (TypeError, ValueError):
                stored = {}
            if isinstance(stored, dict):
                for key in snapshot:
                    if key in stored:
                        snapshot[key] = stored[key]
        # Existing Phase-1 counters are known facts. If no Phase-2 snapshot
        # exists they seed only counters, never a fabricated signature/progress.
        snapshot["consecutive_no_progress_failures"] = max(
            int(snapshot.get("consecutive_no_progress_failures") or 0),
            int(row.get("recovery_failures") or 0),
        )
        snapshot["candidate_refreshes"] = max(
            int(snapshot.get("candidate_refreshes") or 0),
            int(row.get("recovery_refreshes") or 0),
        )
        return snapshot

    @classmethod
    async def _save_recovery_snapshot(cls, db, transfer_id: int, artifact_id: int, snapshot: dict) -> None:
        await db.execute(
            "INSERT INTO application_events(transfer_id,kind,detail,claimed) VALUES(?,?,?,1)",
            (transfer_id, cls._recovery_event_kind(artifact_id), codec.dump(snapshot)),
        )

    async def recovery_context(self, artifact_id: int) -> dict:
        """Return durable recovery/accounting facts without inventing legacy history."""
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (artifact_id,),
            )
            if not row:
                raise KeyError(artifact_id)
            snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
            attempts = await db.fetchone(
                "SELECT COUNT(*) AS n FROM execution_attempt_provenance WHERE artifact_id=?",
                (artifact_id,),
            )
        snapshot["execution_attempts"] = int((attempts or {}).get("n") or 0)
        return snapshot

    async def record_recovery_decision(self, artifact_id: int, action: str, reason: str) -> None:
        """Append the core-owned decision/reason without rewriting factual history."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (artifact_id,),
            )
            if not row:
                await db.rollback()
                raise KeyError(artifact_id)
            snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
            snapshot["decision_action"] = str(action)
            snapshot["decision_reason"] = str(reason)
            await self._save_recovery_snapshot(db, int(row["torrent_id"]), artifact_id, snapshot)
            await db.commit()

    async def record_source_failure(self, artifact_id: int, error=None) -> tuple[int, int]:
        """Consume one factual no-progress failure in the current recovery epoch."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (artifact_id,),
            )
            if not row:
                await db.rollback()
                raise KeyError(artifact_id)
            snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
            cursor = await db.execute(
                "UPDATE download_files SET recovery_failures=recovery_failures+1 WHERE id=?",
                (artifact_id,),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise KeyError(artifact_id)
            failures = int(row.get("recovery_failures") or 0) + 1
            snapshot["consecutive_no_progress_failures"] = failures
            snapshot["failures_since_meaningful_progress"] = int(
                snapshot.get("failures_since_meaningful_progress") or 0
            ) + 1
            if isinstance(error, NormalizedError):
                signature = failure_signature(error)
                snapshot["same_signature_failures"] = (
                    int(snapshot.get("same_signature_failures") or 0) + 1
                    if snapshot.get("failure_signature") == signature else 1
                )
                snapshot["failure_signature"] = signature
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), artifact_id, snapshot,
            )
            await db.commit()
        return failures, int(row.get("recovery_refreshes") or 0)

    async def consume_recovery_refresh(self, artifact_id: int) -> bool:
        """Consume one refresh in this recovery epoch and persist the accounting."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (artifact_id,),
            )
            if not row:
                await db.rollback()
                return False
            snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
            if int(row.get("recovery_refreshes") or 0) >= 1:
                await db.rollback()
                return False
            cursor = await db.execute(
                "UPDATE download_files SET recovery_refreshes=recovery_refreshes+1 WHERE id=? AND recovery_refreshes<1",
                (artifact_id,),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            snapshot["candidate_refreshes"] = int(snapshot.get("candidate_refreshes") or 0) + 1
            await self._save_recovery_snapshot(db, int(row["torrent_id"]), artifact_id, snapshot)
            await db.commit()
        return True

    async def reset_source_recovery(self, artifact_id: int) -> None:
        """Explicitly reset bounded counters without manufacturing progress/epoch."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (artifact_id,),
            )
            if not row:
                await db.rollback()
                raise KeyError(artifact_id)
            snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
            await db.execute(
                "UPDATE download_files SET recovery_failures=0,recovery_refreshes=0 WHERE id=?",
                (artifact_id,),
            )
            snapshot.update({
                "consecutive_no_progress_failures": 0,
                "failures_since_meaningful_progress": 0,
                "failure_signature": None,
                "same_signature_failures": 0,
                "candidate_refreshes": 0,
                "decision_action": None,
                "decision_reason": None,
            })
            await self._save_recovery_snapshot(db, int(row["torrent_id"]), artifact_id, snapshot)
            await db.commit()

    async def reset_retry_budget(self, artifact_id):
        """Operator retry clears exhaustion but is not counted as forward progress."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (artifact_id,),
            )
            if not row:
                await db.rollback()
                raise KeyError(artifact_id)
            snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
            await db.execute(
                "UPDATE download_files SET retry_count=0,recovery_failures=0,recovery_refreshes=0 WHERE id=?",
                (artifact_id,),
            )
            snapshot.update({
                "consecutive_no_progress_failures": 0,
                "failures_since_meaningful_progress": 0,
                "failure_signature": None,
                "same_signature_failures": 0,
                "candidate_refreshes": 0,
                "candidate_switches": 0,
                "decision_action": None,
                "decision_reason": None,
                "quiescence_reason": None,
                "wake_condition": None,
            })
            await self._save_recovery_snapshot(db, int(row["torrent_id"]), artifact_id, snapshot)
            await db.commit()

    async def execution_idle_seconds(self, observation, now):
        """Activity clock: byte movement resets stall time, not recovery epochs."""
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT state,progress,progress_at FROM execution_attempts WHERE id=?",
                (observation.handle.attempt_id,),
            )
            if not row:
                return 0
            previous = TransferProgress(**codec.load(row["progress"], {}))
            active = observation.state == ExecutionState.TRANSFERRING and observation.error is None
            changed = previous.completed_bytes != observation.progress.completed_bytes or row["state"] != observation.state
            if row["progress_at"] is None or not active or changed:
                await db.execute(
                    "UPDATE execution_attempts SET progress_at=? WHERE id=?",
                    (now, observation.handle.attempt_id),
                )
                await db.commit()
                return 0
            return max(0, now - row["progress_at"])

    async def collection_route_provider(self, transfer_id: int) -> str | None:
        async with get_db() as db:
            row = await db.fetchone("SELECT collection_route_provider_id FROM torrents WHERE id=?", (transfer_id,))
        value = str((row or {}).get("collection_route_provider_id") or "").strip()
        return value or None

    async def bind_collection_route(self, transfer_id: int, provider_id: str) -> str | None:
        provider_id = str(provider_id or "").strip()
        if not provider_id:
            raise ValueError("Collection route provider identity is required")
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            parent = await db.fetchone("SELECT source,collection_route_provider_id FROM torrents WHERE id=?", (transfer_id,))
            if not parent or str(parent.get("source") or "") != "direct_link":
                await db.rollback(); return None
            existing = str(parent.get("collection_route_provider_id") or "").strip()
            if existing:
                await db.rollback(); return existing
            roots = await db.fetchone("SELECT COUNT(*) AS count FROM transfer_requests WHERE transfer_id=? AND parent_id IS NULL", (transfer_id,))
            if int((roots or {}).get("count") or 0) <= 1:
                await db.rollback(); return None
            routed = await db.fetchone("""SELECT 1 AS present FROM resolution_attempts a JOIN transfer_requests r ON r.id=a.request_id WHERE r.transfer_id=? LIMIT 1""", (transfer_id,))
            if routed:
                await db.rollback(); return None
            cursor = await db.execute("UPDATE torrents SET collection_route_provider_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND collection_route_provider_id IS NULL", (provider_id, transfer_id))
            if cursor.rowcount != 1:
                current = await db.fetchone("SELECT collection_route_provider_id FROM torrents WHERE id=?", (transfer_id,))
                await db.rollback()
                value = str((current or {}).get("collection_route_provider_id") or "").strip()
                return value or None
            await db.commit()
        return provider_id

    async def bound_route_provider(self, request_id: str) -> str | None:
        routed = await super().bound_route_provider(request_id)
        if routed:
            return routed
        async with get_db() as db:
            row = await db.fetchone("""SELECT t.collection_route_provider_id FROM transfer_requests r JOIN torrents t ON t.id=r.transfer_id WHERE r.id=?""", (request_id,))
        value = str((row or {}).get("collection_route_provider_id") or "").strip()
        return value or None

    async def accept_execution_total(self, artifact_id: int, handle, total_bytes: int) -> bool:
        if (not isinstance(total_bytes, int) or isinstance(total_bytes, bool) or total_bytes <= 0 or handle is None):
            return False
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT f.size_bytes,f.execution_attempt_id,e.executor_id,e.handle FROM download_files f LEFT JOIN execution_attempts e ON e.id=f.execution_attempt_id WHERE f.id=?""", (artifact_id,))
            if (not row or row.get("execution_attempt_id") != handle.attempt_id or row.get("executor_id") != handle.executor_id or codec.load(row.get("handle")) != codec.load(codec.dump(handle)) or int(row.get("size_bytes") or 0) > 0):
                await db.rollback(); return False
            cursor = await db.execute("UPDATE download_files SET size_bytes=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND execution_attempt_id=? AND COALESCE(size_bytes,0)<=0", (total_bytes, artifact_id, handle.attempt_id))
            await db.commit()
        return cursor.rowcount == 1

    async def execution(self, observation) -> None:
        """Persist execution evidence and reset recovery only on meaningful progress."""
        handle = observation.handle
        accepted_total = None
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("SELECT * FROM execution_attempts WHERE id=?", (handle.attempt_id,))
            if not row or codec.load(row["handle"]) != codec.load(codec.dump(handle)):
                await db.rollback()
                raise TransferError(NormalizedError(Domain.LIFECYCLE, Category.OWNERSHIP_CONFLICT, Stage.RECONCILIATION))
            if (not bool(row.get("authorized")) and row.get("state") in _TERMINAL_EXECUTION_STATES):
                await db.rollback()
                return
            previous = TransferProgress(**codec.load(row["progress"], {}))
            artifact = await db.fetchone(
                "SELECT id,torrent_id,size_bytes,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (row["artifact_id"],),
            )
            if not artifact:
                await db.rollback()
                raise KeyError(row["artifact_id"])
            snapshot = await self._recovery_snapshot(db, int(artifact["id"]), row=artifact)
            initialized = snapshot.get("progress_anchor") is not None
            if not initialized:
                snapshot["progress_anchor"] = int(previous.completed_bytes or 0)
            completed = int(observation.progress.completed_bytes or 0)
            if completed > int(previous.completed_bytes or 0):
                anchor = int(snapshot.get("progress_anchor") or 0)
                threshold = meaningful_progress_threshold(int(artifact.get("size_bytes") or 0))
                if completed - anchor >= threshold:
                    await db.execute(
                        "UPDATE download_files SET recovery_failures=0,recovery_refreshes=0 WHERE id=?",
                        (artifact["id"],),
                    )
                    snapshot.update({
                        "recovery_epoch": int(snapshot.get("recovery_epoch") or 0) + 1,
                        "progress_anchor": completed,
                        "consecutive_no_progress_failures": 0,
                        "failures_since_meaningful_progress": 0,
                        "failure_signature": None,
                        "same_signature_failures": 0,
                        "candidate_refreshes": 0,
                        "candidate_switches": 0,
                        "decision_action": None,
                        "decision_reason": None,
                        "quiescence_reason": None,
                        "wake_condition": None,
                    })
            if not initialized or completed > int(previous.completed_bytes or 0):
                await self._save_recovery_snapshot(db, int(artifact["torrent_id"]), int(artifact["id"]), snapshot)

            error = codec.dump(observation.error) if observation.error else None
            revoked = observation.error is not None and observation.error.category == Category.OWNERSHIP_CONFLICT
            await db.execute("""UPDATE execution_attempts SET state=?,progress=?,error=?,authorized=CASE WHEN ? THEN 0 ELSE authorized END,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                             (observation.state, codec.dump(observation.progress), error, revoked, handle.attempt_id))
            await db.execute("UPDATE execution_attempt_provenance SET outcome=?,updated_at=CURRENT_TIMESTAMP WHERE execution_attempt_id=?",
                             (self._execution_outcome(observation.state), handle.attempt_id))
            states = {ExecutionState.TRANSFERRING: "downloading", ExecutionState.QUEUED: "queued", ExecutionState.PAUSED: "paused",
                      ExecutionState.SUCCEEDED: "verifying", ExecutionState.FAILED: "error", ExecutionState.CANCELLED: "cancelled",
                      ExecutionState.ABSENT: "lost", ExecutionState.UNKNOWN: "unknown"}
            await db.execute("""UPDATE download_files SET status=?,normalized_error=?,updated_at=CURRENT_TIMESTAMP WHERE execution_attempt_id=? AND torrent_id IN (SELECT id FROM torrents WHERE status NOT IN ('deleted','consolidated','cancelled'))""",
                             (states[observation.state], error, handle.attempt_id))
            total = observation.progress.total_bytes
            credible = (
                observation.state in _RUNTIME_TOTAL_STATES
                and isinstance(total, int) and not isinstance(total, bool) and total > 0
                and isinstance(observation.progress.completed_bytes, int)
                and not isinstance(observation.progress.completed_bytes, bool)
                and 0 <= observation.progress.completed_bytes <= total
                and int(artifact.get("size_bytes") or 0) <= 0
            )
            if credible:
                accepted_total = (int(artifact["id"]), total)
            await db.commit()
        if accepted_total:
            await self.accept_execution_total(accepted_total[0], handle, accepted_total[1])

    async def refine_execution_total(self, artifact_id: int, handle, total_bytes: int) -> bool:
        if (not isinstance(total_bytes, int) or isinstance(total_bytes, bool) or total_bytes < 0 or handle is None):
            return False
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT f.size_bytes,f.execution_attempt_id,f.torrent_id,e.executor_id,e.handle,e.state,e.candidate FROM download_files f JOIN execution_attempts e ON e.id=f.execution_attempt_id WHERE f.id=?""", (artifact_id,))
            candidate = None
            if row and row.get("candidate"):
                try:
                    candidate = codec.candidate(codec.load(row["candidate"]))
                except (TypeError, ValueError, KeyError):
                    candidate = None
            if (not row or row.get("execution_attempt_id") != handle.attempt_id or row.get("executor_id") != handle.executor_id or codec.load(row.get("handle")) != codec.load(codec.dump(handle)) or row.get("state") != "succeeded" or candidate is None or candidate.expected_bytes > 0):
                await db.rollback(); return False
            previous = int(row.get("size_bytes") or 0)
            cursor = await db.execute("UPDATE download_files SET size_bytes=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND execution_attempt_id=?", (total_bytes, artifact_id, handle.attempt_id))
            if cursor.rowcount and previous != total_bytes:
                await db.execute("INSERT INTO events(torrent_id,level,message) VALUES(?,'warning','Final verified materialization refined execution-observed artifact size')", (row["torrent_id"],))
            await db.commit()
        return cursor.rowcount == 1

    async def transition_recovery(self, artifact_id: int, state: str, *, error=None,
                                  retry_at: float = 0, selected: int | None = None,
                                  expected_bytes: int | None = None,
                                  reset_budget: bool = False,
                                  quiescence_reason: str | None = None,
                                  wake_condition: str | None = None,
                                  clear_quiescence: bool = False,
                                  candidate_switched: bool = False) -> bool:
        """Atomically revoke terminal writer authority and persist recovery state."""
        if expected_bytes is not None and expected_bytes < 0:
            return False
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone("""SELECT f.*,t.status AS transfer_status FROM download_files f JOIN torrents t ON t.id=f.torrent_id WHERE f.id=?""", (artifact_id,))
            if not row or row["transfer_status"] in {"deleted", "completed", "consolidated", "cancelled"}:
                await db.rollback(); return False
            current_id = row.get("execution_attempt_id")
            if current_id is not None:
                current = await db.fetchone("SELECT state,authorized FROM execution_attempts WHERE id=? AND artifact_id=?", (current_id, artifact_id))
                if not current or current["state"] not in _TERMINAL_EXECUTION_STATES:
                    await db.rollback(); return False
                await db.execute("UPDATE execution_attempts SET authorized=0,updated_at=CURRENT_TIMESTAMP WHERE id=?", (current_id,))
            placeholders = ",".join("?" for _ in _MUTATING_EXECUTION_STATES)
            active = await db.fetchone(f"SELECT id FROM execution_attempts WHERE artifact_id=? AND authorized=1 AND state IN ({placeholders}) LIMIT 1", (artifact_id, *_MUTATING_EXECUTION_STATES))
            if active:
                await db.rollback(); return False

            snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
            assignments = ["status=?", "normalized_error=?", "retry_at=?", "execution_attempt_id=NULL", "updated_at=CURRENT_TIMESTAMP"]
            params = [state, codec.dump(error) if error else None, retry_at]
            if selected is not None:
                assignments.append("selected_candidate=?"); params.append(selected)
            if expected_bytes is not None:
                assignments.append("size_bytes=?"); params.append(expected_bytes)
            if reset_budget:
                assignments.extend(["retry_count=0", "recovery_failures=0", "recovery_refreshes=0"])
                snapshot.update({"consecutive_no_progress_failures": 0, "failures_since_meaningful_progress": 0,
                                 "failure_signature": None, "same_signature_failures": 0, "candidate_refreshes": 0})
            if candidate_switched:
                assignments.extend(["recovery_failures=0", "recovery_refreshes=0"])
                snapshot.update({"consecutive_no_progress_failures": 0, "failure_signature": None,
                                 "same_signature_failures": 0, "candidate_refreshes": 0,
                                 "candidate_switches": int(snapshot.get("candidate_switches") or 0) + 1})
            if clear_quiescence:
                snapshot["quiescence_reason"] = None; snapshot["wake_condition"] = None
            if quiescence_reason is not None:
                snapshot["quiescence_reason"] = str(quiescence_reason)
                snapshot["wake_condition"] = str(wake_condition or "") or None
            params.append(artifact_id)
            cursor = await db.execute(f"UPDATE download_files SET {','.join(assignments)} WHERE id=?", tuple(params))
            if cursor.rowcount:
                await self._save_recovery_snapshot(db, int(row["torrent_id"]), artifact_id, snapshot)
            await db.commit()
        return cursor.rowcount == 1

    async def _candidate_presentation(self, transfer_id: int) -> dict[int, dict]:
        async with get_db() as db:
            files = await db.fetchall("""SELECT id,candidates,selected_candidate,execution_attempt_id FROM download_files WHERE torrent_id=? AND request_id IS NOT NULL AND COALESCE(blocked,0)=0 AND COALESCE(mirror_state,'')!='standby' ORDER BY id""", (transfer_id,))
            artifact_ids = [int(row["id"]) for row in files]
            if not artifact_ids:
                return {}
            placeholders = ",".join("?" for _ in artifact_ids)
            bindings = await db.fetchall(f"""SELECT canonical_artifact_id,candidate_id,provider_id,source_scope,source_key,role,candidate_order FROM canonical_candidate_bindings WHERE canonical_artifact_id IN ({placeholders}) ORDER BY canonical_artifact_id,candidate_order,id""", tuple(artifact_ids))
            attempts = await db.fetchall(f"""SELECT p.artifact_id,p.candidate_id,p.outcome,p.delivered,p.ordinal,p.execution_attempt_id,e.state,e.authorized FROM execution_attempt_provenance p LEFT JOIN execution_attempts e ON e.id=p.execution_attempt_id WHERE p.artifact_id IN ({placeholders}) ORDER BY p.artifact_id,p.ordinal,p.execution_attempt_id""", tuple(artifact_ids))
        selected_ids = {}; current_attempt_ids = {}; has_durable_candidate = {}
        for row in files:
            artifact_id = int(row["id"]); selected_id = None
            try:
                candidates = [codec.candidate(value) for value in codec.load(row.get("candidates"), [])]
                has_durable_candidate[artifact_id] = bool(candidates)
                selected = int(row.get("selected_candidate") or 0)
                if 0 <= selected < len(candidates): selected_id = str(candidates[selected].id)
            except (TypeError, ValueError, KeyError, IndexError):
                has_durable_candidate[artifact_id] = False
            selected_ids[artifact_id] = selected_id; current_attempt_ids[artifact_id] = row.get("execution_attempt_id")
        attempt_history = {}
        for row in attempts:
            candidate_id = str(row.get("candidate_id") or "").strip()
            if candidate_id: attempt_history.setdefault((int(row["artifact_id"]), candidate_id), []).append(dict(row))
        by_artifact = {}; seen = {}
        for row in bindings:
            artifact_id = int(row["canonical_artifact_id"]); candidate_id = str(row["candidate_id"])
            if candidate_id in seen.setdefault(artifact_id, set()): continue
            seen[artifact_id].add(candidate_id); history = attempt_history.get((artifact_id, candidate_id), [])
            delivered = any(bool(item.get("delivered")) for item in history); latest = history[-1] if history else None
            failed = bool(latest and (str(latest.get("outcome") or "").strip().lower() in _FAILED_CANDIDATE_OUTCOMES or str(latest.get("state") or "").strip().lower() in _FAILED_CANDIDATE_OUTCOMES)) and not delivered
            selected = selected_ids.get(artifact_id) == candidate_id
            current = bool(latest and current_attempt_ids.get(artifact_id) == latest.get("execution_attempt_id"))
            active = bool(current and latest and bool(latest.get("authorized")) and str(latest.get("state") or "").strip().lower() in _MUTATING_EXECUTION_STATES)
            dispositions = []
            if delivered: dispositions.append("Delivering")
            elif failed: dispositions.append("Failed")
            elif active: dispositions.append("Active")
            elif selected: dispositions.append("Selected")
            by_artifact.setdefault(artifact_id, []).append({"candidate_id": candidate_id, "source_label": _safe_source_label(row.get("source_scope"), row.get("source_key")), "provider_id": str(row.get("provider_id") or "").strip() or None, "relationship": "Original" if row.get("role") == "canonical" else "Consolidated", "dispositions": dispositions, "is_selected": selected, "is_delivering": delivered})
        result = {}
        for artifact_id in artifact_ids:
            candidates = by_artifact.get(artifact_id, []); candidate_count = len(candidates)
            if candidate_count == 0 and has_durable_candidate.get(artifact_id, False): candidate_count = 1
            result[artifact_id] = {"candidate_count": candidate_count, "acquisition_candidates": candidates if candidate_count > 1 else []}
        return result

    async def presentation(self, transfer_id: int, details: bool = False):
        result = await super().presentation(transfer_id, details=details)
        if not result or not details:
            return result
        candidate_projection = await self._candidate_presentation(transfer_id)
        for file_row in result.get("files", []):
            artifact_id = int(file_row.get("id") or 0)
            projection = candidate_projection.get(artifact_id, {"candidate_count": 0, "acquisition_candidates": []})
            file_row["candidate_count"] = projection["candidate_count"]
            if projection["candidate_count"] > 1:
                file_row["acquisition_candidates"] = projection["acquisition_candidates"]
        return result

    # -------------------------------------------------------------------------
    # Universal file-selection manifest overlay (specification sections 13-38).
    #
    # The repository stores and atomically transitions durable facts; it never
    # sources wall time for product timing. Core passes ``now`` and absolute
    # deadlines derived from the injected engine clock. The
    # Confirm-vs-materialization race is serialized entirely by SQLite
    # ``BEGIN IMMEDIATE`` on the ``transfer_file_selections`` row; an in-memory
    # lock is never the correctness authority.
    #
    # Selection provenance follows the provider resource that produced the file
    # facts. Each row in ``transfer_file_selections`` is one selection
    # *generation* keyed by (request_id, provider_resource_id). A request that is
    # re-resolved onto a new provider resource gets a fresh generation; the prior
    # generation stays as historical truth and is never inherited or overwritten.
    # Every mutating/materializing operation binds to a specific generation, not
    # merely to the durable request id.
    # -------------------------------------------------------------------------

    @staticmethod
    async def _selection_generation(db, request_id: str, provider_resource_id: str):
        return await db.fetchone(
            "SELECT * FROM transfer_file_selections WHERE request_id=? AND provider_resource_id=?",
            (request_id, provider_resource_id),
        )

    @staticmethod
    async def _current_generation(db, transfer_id: int):
        """The transfer's current selection generation: the newest one.

        A re-resolution onto a new provider resource always creates a strictly
        newer generation, so the newest row is the live selector; older
        generations remain only as historical truth.
        """
        return await db.fetchone(
            """SELECT * FROM transfer_file_selections WHERE transfer_id=?
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (transfer_id,),
        )

    @classmethod
    async def _selection_by_manifest(cls, db, transfer_id: int, manifest_id: str):
        """Resolve the CURRENT selection generation only if it observed this
        manifest. ``manifest_id`` is UUIDv5(provider-resource-id : digest), so a
        stale browser tab holding an older generation's manifest id resolves to
        nothing here and the caller returns a stale-manifest conflict — never a
        re-interpretation against the newer generation.
        """
        current = await cls._current_generation(db, transfer_id)
        if current is not None and str(current["manifest_id"] or "") == str(manifest_id):
            return current
        return None

    @staticmethod
    def _selection_state(row, file_count: int) -> fs.SelectionWindowState:
        return fs.SelectionWindowState(
            decision=str(row["decision"]),
            initially_available=bool(row["initially_available"]),
            manifest_wait_until=float(row["manifest_wait_until"]),
            hold_until=row["hold_until"],
            manifest_id=row["manifest_id"],
            manifest_file_count=int(file_count),
            manifest_committed_at=row["manifest_committed_at"],
            auto_offer_dismissed_at=row["auto_offer_dismissed_at"],
        )

    @staticmethod
    async def _manifest_file_count(db, manifest_id) -> int:
        if not manifest_id:
            return 0
        row = await db.fetchone(
            "SELECT COUNT(*) AS n FROM transfer_file_manifest_entries WHERE manifest_id=?",
            (manifest_id,),
        )
        return int((row or {}).get("n") or 0)

    async def begin_file_selection_window(
        self, request_id: str, transfer_id: int, provider_resource_id: str,
        provider_id: str, *, initially_available: bool, now: float,
    ):
        """Idempotently open the durable file-selection generation for
        (request, provider resource).

        A different provider resource for the same durable request creates a new
        generation; it never overwrites the prior generation and never inherits
        its explicit subset. The 60-second automatic manifest window is anchored
        to ``now`` here and is never reset by a later call, an application
        restart, or a re-resolution. The factual initial-availability
        observation is captured once per generation.
        """
        selection_id = fs.selection_identity(request_id, provider_resource_id)
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            parent = await db.fetchone("SELECT status FROM torrents WHERE id=?", (transfer_id,))
            if not parent or parent["status"] in {"deleted", "completed", "consolidated", "cancelled"}:
                await db.rollback()
                return None
            if not await db.fetchone("SELECT 1 FROM transfer_requests WHERE id=? AND transfer_id=?", (request_id, transfer_id)):
                await db.rollback()
                return None
            if not await db.fetchone("SELECT 1 FROM provider_resources WHERE id=?", (provider_resource_id,)):
                await db.rollback()
                return None
            await db.execute(
                """INSERT OR IGNORE INTO transfer_file_selections(
                        id, request_id, transfer_id, provider_resource_id, provider_id,
                        initially_available, manifest_wait_until, created_at, updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                (selection_id, request_id, transfer_id, provider_resource_id, str(provider_id),
                 int(bool(initially_available)), fs.manifest_wait_deadline(now), now, now),
            )
            row = await self._selection_generation(db, request_id, provider_resource_id)
            await db.commit()
        return row

    async def record_file_manifest(self, request_id: str, provider_resource_id: str, manifest, *, now: float):
        """Validate and persist a neutral early manifest, then bind it to the
        (request, provider resource) selection generation.

        Returns the canonical manifest on success, or ``None`` when there is no
        open generation for this resource, when mutation is already closed, or
        when the optional early manifest is malformed. A malformed early manifest
        is deliberately non-fatal before explicit confirmation: the selector
        stays unavailable and default ALL keeps governing the full transfer.
        """
        async with get_db() as db:
            sel = await self._selection_generation(db, request_id, provider_resource_id)
            if not sel or sel["manifest_committed_at"] is not None or sel["decision"] != "pending":
                return None
            try:
                canonical = fs.canonicalize_manifest(provider_resource_id, manifest)
            except fs.ManifestInvalid:
                return None
            await db.execute("BEGIN IMMEDIATE")
            sel = await self._selection_generation(db, request_id, provider_resource_id)
            if not sel or sel["manifest_committed_at"] is not None or sel["decision"] != "pending":
                await db.rollback()
                return None
            await db.execute(
                """INSERT OR IGNORE INTO transfer_file_manifests(
                        id, transfer_id, request_id, provider_resource_id, provider_id,
                        manifest_digest, observed_at)
                    VALUES(?,?,?,?,?,?,?)""",
                (canonical.manifest_id, sel["transfer_id"], request_id, provider_resource_id,
                 sel["provider_id"], canonical.manifest_digest, now),
            )
            for entry in canonical.entries:
                await db.execute(
                    """INSERT OR IGNORE INTO transfer_file_manifest_entries(
                            manifest_id, entry_id, ordinal, name, relative_path, expected_bytes)
                        VALUES(?,?,?,?,?,?)""",
                    (canonical.manifest_id, entry.entry_id, entry.ordinal, entry.name,
                     entry.relative_path, entry.expected_bytes),
                )
            assignments = ["manifest_id=?", "updated_at=?"]
            params = [canonical.manifest_id, now]
            # The 120-second decision hold begins in this same durable transaction
            # whenever a populated multi-file selector becomes usable inside the
            # 60-second auto-presentation window, whether the provider resource was
            # initially AVAILABLE or initially PREPARING (specification sections
            # 5, 6.2, 6.3). It is anchored once to this arrival and never
            # restarted by a repeat observation, a duplicate manifest, a
            # scheduler pass, a browser reconnect, or a restart. A manifest first
            # observed after the window closed gets no automatic hold (section
            # 6.9); manual Details selection stays available while mutable.
            hold_until = sel["hold_until"]
            within_auto_window = now < float(sel["manifest_wait_until"])
            if canonical.file_count > 1 and hold_until is None and within_auto_window:
                hold_until = fs.decision_hold_deadline(now)
                assignments.append("hold_until=?")
                params.append(hold_until)
            # An offer is queued (and the durable browser event emitted, once)
            # only when this manifest is genuinely auto-presentable now: multi-file,
            # not previously dismissed, and inside the 60s window or an active
            # cached hold. Repeated provider polls cannot re-queue it.
            bound_state = fs.SelectionWindowState(
                decision="pending", initially_available=bool(sel["initially_available"]),
                manifest_wait_until=float(sel["manifest_wait_until"]), hold_until=hold_until,
                manifest_id=canonical.manifest_id, manifest_file_count=canonical.file_count,
                manifest_committed_at=None, auto_offer_dismissed_at=sel["auto_offer_dismissed_at"],
            )
            queue_offer = sel["auto_offer_queued_at"] is None and fs.auto_offer_active(bound_state, now)
            if queue_offer:
                assignments.append("auto_offer_queued_at=?")
                params.append(now)
            params.append(sel["id"])
            await db.execute(
                f"UPDATE transfer_file_selections SET {','.join(assignments)} WHERE id=?",
                tuple(params),
            )
            if queue_offer:
                await db.execute(
                    "INSERT INTO application_events(transfer_id,kind,detail,claimed) VALUES(?,?,?,0)",
                    (sel["transfer_id"], "file_selection_available", None),
                )
            await db.commit()
        return canonical

    async def file_selection_gate(self, request_id: str, provider_resource_id: str, *, now: float) -> str:
        """Neutral gate: may executable child fan-out proceed for this
        (request, provider resource)?

        Atomically settles a still-``pending`` decision to durable ALL with a
        neutral reason when a bounded window has elapsed or the manifest is
        single-file. Returns one of :class:`fs.SelectionGate`.
        """
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._selection_generation(db, request_id, provider_resource_id)
            if not row:
                await db.rollback()
                return str(fs.SelectionGate.PROCEED)
            file_count = await self._manifest_file_count(db, row["manifest_id"])
            evaluation = fs.evaluate_gate(self._selection_state(row, file_count), now)
            if (evaluation.resolve_decision is not None and row["decision"] == "pending"
                    and row["manifest_committed_at"] is None):
                await db.execute(
                    """UPDATE transfer_file_selections
                       SET decision=?, decision_reason=?, decision_at=?, updated_at=?
                       WHERE id=? AND decision='pending' AND manifest_committed_at IS NULL""",
                    (str(evaluation.resolve_decision), str(evaluation.resolve_reason), now, now, row["id"]),
                )
            await db.commit()
        return str(evaluation.gate)

    @staticmethod
    async def _release_selection_poll_wait(db, request_id: str, now: float) -> None:
        """Atomically end the scheduler wait that the file-selection gate created.

        §9a investigation — ``transfer_requests.retry_at`` is MULTI-PURPOSE. Two
        code paths set it forward on a request that ends up ``state='waiting'``:

          * ``_repository_base.poll_after()`` — the file-selection gate wait
            (``engine._observe_resource``: ``gate != PROCEED`` → ``poll_after(...,
            waiting=True, clear_error=True)``) and the PREPARING re-poll cadence.
            Neither records an ``error``.
          * ``_repository_base.request_failure()`` (via
            ``_engine_base._request_failure(..., waiting=True)``) — a provider
            observation error / ABSENT / EXPIRED / reconciliation-exception
            backoff, whose delay is ``policy.retry_resolution(error).retry_at``.
            This path ALWAYS writes a non-null ``error`` blob and increments
            ``attempts``.

        A cross-transfer equivalence proof-retry also writes ``retry_at`` forward
        (``cohorts.py``: ``UPDATE ... retry_at=? WHERE id=? AND
        state='materializing'``) but only for ``state='materializing'`` rows,
        never ``state='waiting'``.

        The existing distinction the correction relies on is therefore
        ``state='waiting' AND error IS NULL``: the file-selection gate wait, and
        only it (or a benign PREPARING re-poll), leaves the request without an
        error. A provider backoff on the same request keeps its longer,
        legitimate ``retry_at`` because ``error IS NOT NULL``. ``clear_error`` on
        the gate-wait ``poll_after`` keeps this predicate honest after a request
        recovered from an earlier transient failure.
        """
        await db.execute(
            "UPDATE transfer_requests SET retry_at=? "
            "WHERE id=? AND state='waiting' AND error IS NULL AND retry_at > ?",
            (now, request_id, now),
        )

    async def confirm_file_selection(
        self, transfer_id: int, manifest_id: str, entry_ids, *, now: float,
    ) -> "fs.SelectionCommandResult":
        """Durably commit an explicit file subset, or lose the race to materialization.

        The selection generation is resolved from (transfer, manifest): the
        manifest id is bound to exactly one provider resource, so a stale browser
        tab holding an older manifest id cannot reach a newer generation. The
        ``BEGIN IMMEDIATE`` write lock on ``transfer_file_selections`` is the sole
        correctness authority for the Confirm-vs-materialization race. Confirm
        never reports success once the executable manifest is committed.
        """
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._selection_by_manifest(db, transfer_id, str(manifest_id))
            if not row:
                await db.rollback()
                return fs.SelectionCommandResult(str(fs.SelectionOutcome.CONFLICT), "stale_manifest")
            if row["manifest_committed_at"] is not None:
                await db.rollback()
                return fs.SelectionCommandResult(
                    str(fs.SelectionOutcome.CONFLICT), "materialization_committed",
                    decision=str(row["decision"]), manifest_id=row["manifest_id"], committed=True,
                )
            if str(row["decision"]) == "all":
                await db.rollback()
                return fs.SelectionCommandResult(
                    str(fs.SelectionOutcome.CONFLICT), "already_all",
                    decision="all", manifest_id=row["manifest_id"],
                )
            known = {
                r["entry_id"] for r in await db.fetchall(
                    "SELECT entry_id FROM transfer_file_manifest_entries WHERE manifest_id=?", (manifest_id,))
            }
            requested, seen = [], set()
            for value in (entry_ids or ()):
                text = str(value)
                if text in seen:
                    await db.rollback()
                    return fs.SelectionCommandResult(str(fs.SelectionOutcome.INVALID), "duplicate_selection")
                seen.add(text)
                requested.append(text)
            if not requested:
                await db.rollback()
                return fs.SelectionCommandResult(str(fs.SelectionOutcome.INVALID), "empty_selection")
            if len(requested) > fs.MAX_SELECTION_ENTRIES:
                await db.rollback()
                return fs.SelectionCommandResult(str(fs.SelectionOutcome.INVALID), "too_many_selected")
            if not seen.issubset(known):
                await db.rollback()
                return fs.SelectionCommandResult(str(fs.SelectionOutcome.INVALID), "unknown_selection_entry")
            existing = {
                r["entry_id"] for r in await db.fetchall(
                    "SELECT entry_id FROM transfer_file_selection_entries WHERE selection_id=?", (row["id"],))
            }
            if str(row["decision"]) == "explicit":
                if existing != seen:
                    await db.rollback()
                    return fs.SelectionCommandResult(
                        str(fs.SelectionOutcome.CONFLICT), "selection_superseded",
                        decision="explicit", manifest_id=row["manifest_id"],
                    )
                # Idempotent same-subset Confirm — self-heal a scheduler wait that
                # a crash/restart left in place after an earlier run persisted
                # EXPLICIT but before it released the file-selection gate wait
                # (§12). Never broadens the selection; still first-writer safe.
                await self._release_selection_poll_wait(db, str(row["request_id"]), now)
                await db.commit()
                return fs.SelectionCommandResult(
                    str(fs.SelectionOutcome.CONFIRMED), "idempotent",
                    decision="explicit", manifest_id=row["manifest_id"],
                )
            for entry_id in requested:
                await db.execute(
                    "INSERT OR IGNORE INTO transfer_file_selection_entries(selection_id, manifest_id, entry_id) VALUES(?,?,?)",
                    (row["id"], str(manifest_id), entry_id),
                )
            cursor = await db.execute(
                """UPDATE transfer_file_selections
                   SET decision='explicit', decision_reason=?, decision_at=?, updated_at=?
                   WHERE id=? AND decision='pending' AND manifest_committed_at IS NULL""",
                (str(fs.DecisionReason.CONFIRMED), now, now, row["id"]),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return fs.SelectionCommandResult(str(fs.SelectionOutcome.CONFLICT), "materialization_won")
            # The decision is settled; the 120s hold is no longer active. Release
            # the file-selection gate wait in the SAME transaction so the next
            # resolution cycle materialises the confirmed subset immediately —
            # without waiting out the old decision deadline or the last provider
            # poll timestamp. Only the selection-induced wait is released (§9a).
            await self._release_selection_poll_wait(db, str(row["request_id"]), now)
            await db.commit()
        return fs.SelectionCommandResult(
            str(fs.SelectionOutcome.CONFIRMED), "confirmed",
            decision="explicit", manifest_id=str(manifest_id),
        )

    async def dismiss_file_selection(
        self, transfer_id: int, manifest_id: str, *, now: float,
    ) -> "fs.SelectionCommandResult":
        """Record a Close/X. Releases an active cached hold immediately; otherwise
        leaves default ALL and keeps the decision mutable for later Details use."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await self._selection_by_manifest(db, transfer_id, str(manifest_id))
            if not row:
                await db.rollback()
                return fs.SelectionCommandResult(str(fs.SelectionOutcome.CONFLICT), "stale_manifest")
            if row["manifest_committed_at"] is not None:
                await db.rollback()
                return fs.SelectionCommandResult(
                    str(fs.SelectionOutcome.CONFLICT), "materialization_committed",
                    decision=str(row["decision"]), committed=True,
                )
            file_count = await self._manifest_file_count(db, row["manifest_id"])
            # Close/X on any live auto-presented multi-file hold settles ALL and
            # releases immediately, whatever the resource's initial availability
            # (specification section 6.5).
            active_hold = (
                file_count > 1
                and row["hold_until"] is not None and str(row["decision"]) == "pending"
            )
            if active_hold:
                cursor = await db.execute(
                    """UPDATE transfer_file_selections
                       SET decision='all', decision_reason=?, decision_at=?,
                           auto_offer_dismissed_at=?, updated_at=?
                       WHERE id=? AND decision='pending' AND manifest_committed_at IS NULL""",
                    (str(fs.DecisionReason.CLOSED), now, now, now, row["id"]),
                )
                if cursor.rowcount == 1:
                    # Close/X settled the decision to default ALL; the 120s hold
                    # is over. Release the file-selection gate wait in the same
                    # transaction so ALL materialisation proceeds immediately
                    # (§10). Only the selection-induced wait is released (§9a).
                    await self._release_selection_poll_wait(db, str(row["request_id"]), now)
            else:
                await db.execute(
                    """UPDATE transfer_file_selections
                       SET auto_offer_dismissed_at=?, updated_at=?
                       WHERE id=? AND manifest_committed_at IS NULL""",
                    (now, now, row["id"]),
                )
            await db.commit()
        return fs.SelectionCommandResult(
            str(fs.SelectionOutcome.DISMISSED), "closed_hold" if active_hold else "dismissed",
            decision="all" if active_hold else str(row["decision"]), manifest_id=str(manifest_id),
        )

    async def commit_selected_manifest(self, record, full_entries, *, now: float):
        """Filter the full executable manifest to the authorized subset and durably
        record the materialization-commit fact for this provider resource.

        The selection generation is bound to ``(record.id, record.resource.id)``:
        a replacement provider resource for the same durable request can never
        consume a prior resource's selection rows. Returns the authorized
        ``tuple[SourceEntry, ...]``:

        * no selection generation for this resource, or a settled ALL /
          still-pending decision -> the full provider list;
        * a confirmed EXPLICIT subset -> only the members proven to match the
          executable manifest by normalized relative path and compatible size.

        A confirmed explicit subset that can no longer be proven fails closed
        with a neutral ``RESOURCE_STATE_CONFLICT`` and never broadens to ALL.

        ``manifest_committed_at`` marks that core has *authorized* materialization
        for this generation and frozen mutation. The child-request fan-out
        (``repository.manifest``) is a following idempotent transaction; a crash
        between the two is recovered by the engine re-driving observation ->
        gate PROCEED -> this call (idempotent) -> fan-out (INSERT OR IGNORE).
        """
        full_entries = tuple(full_entries)
        canonical_resource_id = record.resource.id if record.resource is not None else None
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            binding_id = (
                await self._resolve_binding(db, record.transfer_id, canonical_resource_id)
                if canonical_resource_id else None
            )
            row = (
                await self._selection_generation(db, record.id, binding_id)
                if binding_id else None
            )
            if not row:
                await db.rollback()
                return full_entries
            already = row["manifest_committed_at"] is not None
            if str(row["decision"]) in ("pending", "all"):
                authorized = full_entries
                if not already:
                    reason = row["decision_reason"] or str(fs.DecisionReason.DEFAULT_MATERIALIZATION)
                    await db.execute(
                        """UPDATE transfer_file_selections
                           SET decision='all',
                               decision_reason=COALESCE(decision_reason, ?),
                               decision_at=COALESCE(decision_at, ?),
                               manifest_committed_at=?, updated_at=?
                           WHERE id=?""",
                        (reason, now, now, now, row["id"]),
                    )
            else:
                selected = await db.fetchall(
                    """SELECT e.relative_path AS relative_path, e.expected_bytes AS expected_bytes
                       FROM transfer_file_selection_entries s
                       JOIN transfer_file_manifest_entries e
                         ON e.manifest_id=s.manifest_id AND e.entry_id=s.entry_id
                       WHERE s.selection_id=? ORDER BY e.ordinal""",
                    (row["id"],),
                )
                if not selected:
                    await db.rollback()
                    raise TransferError(NormalizedError(
                        Domain.LIFECYCLE, Category.RESOURCE_STATE_CONFLICT, Stage.RECONCILIATION))
                try:
                    authorized = fs.reconcile_executable_subset(
                        [(r["relative_path"], int(r["expected_bytes"] or 0)) for r in selected],
                        full_entries,
                    )
                except fs.SelectionUnprovable as exc:
                    await db.rollback()
                    raise TransferError(NormalizedError(
                        Domain.LIFECYCLE, Category.RESOURCE_STATE_CONFLICT, Stage.RECONCILIATION,
                    )) from exc
                if not already:
                    await db.execute(
                        "UPDATE transfer_file_selections SET manifest_committed_at=?, updated_at=? WHERE id=?",
                        (now, now, row["id"]),
                    )
            await db.commit()
        return authorized

    async def file_selection_presentation(self, transfer_id: int, *, now: float):
        """Safe core-only read model for one transfer's file selection.

        Reads durable state only: no provider call, no executor handle, no
        signed URL, no per-row Downloads-projection work.
        """
        async with get_db() as db:
            row = await self._current_generation(db, transfer_id)
            if row is None:
                return None
            file_count = await self._manifest_file_count(db, row["manifest_id"])
            entries = []
            if row["manifest_id"]:
                entries = await db.fetchall(
                    """SELECT entry_id, name, relative_path, expected_bytes, ordinal
                       FROM transfer_file_manifest_entries WHERE manifest_id=? ORDER BY ordinal""",
                    (row["manifest_id"],),
                )
            selected = [
                r["entry_id"] for r in await db.fetchall(
                    "SELECT entry_id FROM transfer_file_selection_entries WHERE selection_id=?", (row["id"],))
            ]
        state = self._selection_state(row, file_count)
        return {
            "eligible": True,
            "mutable": fs.selection_mutable(state),
            "selection_id": row["id"],
            "request_id": row["request_id"],
            "provider_resource_id": row["provider_resource_id"],
            "manifest_id": row["manifest_id"],
            "decision": str(row["decision"]),
            "decision_reason": row["decision_reason"],
            "file_count": file_count,
            "total_size_bytes": sum(int(e["expected_bytes"] or 0) for e in entries),
            "entries": [
                {"entry_id": e["entry_id"], "name": e["name"],
                 "relative_path": e["relative_path"], "size_bytes": int(e["expected_bytes"] or 0)}
                for e in entries
            ],
            "selected_entry_ids": selected,
            "auto_offer": fs.auto_offer_active(state, now),
            "auto_offer_until": float(row["manifest_wait_until"]),
            # The 120s decision deadline is current control authority ONLY while
            # the decision is still pending. Once Confirm/Close/timeout settles it
            # the durable ``hold_until`` is retained as historical evidence but is
            # never presented as an active deadline (§11).
            "decision_deadline": row["hold_until"] if str(row["decision"]) == "pending" else None,
            "initially_available": bool(row["initially_available"]),
            "server_now": float(now),
        }

    async def active_file_selection_offers(self, *, now: float) -> list:
        """Bounded list of currently auto-presentable multi-file offers."""
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT s.*,
                          (SELECT COUNT(*) FROM transfer_file_manifest_entries e
                           WHERE e.manifest_id=s.manifest_id) AS file_count
                   FROM transfer_file_selections s
                   JOIN torrents t ON t.id=s.transfer_id
                   WHERE s.manifest_committed_at IS NULL AND s.decision='pending'
                     AND s.manifest_id IS NOT NULL AND s.auto_offer_dismissed_at IS NULL
                     AND t.status NOT IN ('deleted','completed','consolidated','cancelled')
                   ORDER BY s.transfer_id LIMIT 500""",
            )
        offers = []
        for row in rows:
            file_count = int(row["file_count"] or 0)
            if file_count <= 1:
                continue
            if fs.auto_offer_active(self._selection_state(row, file_count), now):
                offers.append({
                    "transfer_id": int(row["transfer_id"]),
                    "selection_id": row["id"],
                    "request_id": row["request_id"],
                    "manifest_id": row["manifest_id"],
                    "file_count": file_count,
                    "decision_deadline": row["hold_until"],
                    "auto_offer_until": float(row["manifest_wait_until"]),
                })
        return offers
