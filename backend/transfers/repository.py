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
