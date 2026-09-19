"""Canonical durable recovery repository owner.

Current recovery state lives in the canonical ``artifact_recovery_state`` row
(``transfers.repository.TransferRepository._recovery_snapshot`` /
``_save_recovery_snapshot``). This owner extends that current-state model
with the durable per-artifact claim/fence, quiescence, refresh single-flight,
and structured application/decision provenance (recorded SOLELY in the sparse
``recovery_audit`` trail via ``_append_recovery_audit`` -- never duplicated
into current state). It does not create a second recovery policy or state
store.

Claims are exclusive across all trigger types, including ``USER_RETRY``.
Operator authority changes what the common recovery machinery may do after it
owns the artifact; it does not authorize stealing an in-flight productive
claim.
"""
from __future__ import annotations

from db.database import get_db
from transfers.errors import NormalizedError
from transfers.manual_repository import TransferRepository as _QualifiedRepository
from transfers.models import new_identity
from transfers.policy import failure_signature
from transfers.recovery_execution import RecoveryClaim, RecoveryTrigger


class TransferRepository(_QualifiedRepository):
    """Single production owner for durable recovery claim/fence/quiescence/
    refresh/audit behavior (DP 1.0.12 leveling remediation, ARCH-001)."""

    # ------------------------------------------------------------------
    # Durable claim / fence
    # ------------------------------------------------------------------

    async def claim_recovery(
        self,
        artifact_id: int,
        trigger: RecoveryTrigger,
        now: float,
        *,
        lease_seconds: float = 300.0,
    ) -> RecoveryClaim | None:
        trigger = RecoveryTrigger(trigger)
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                """SELECT f.torrent_id,f.local_path,f.recovery_failures,f.recovery_refreshes
                   FROM download_files f WHERE f.id=?""",
                (artifact_id,),
            )
            if not row:
                await db.rollback()
                return None
            snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
            token = snapshot.get("recovery_claim_token")
            claim_until = float(snapshot.get("recovery_claim_until") or 0)
            # An active, unexpired claim is exclusive across EVERY trigger,
            # including USER_RETRY -- operator authority changes what the
            # recovery machinery may do once it owns the artifact, it does
            # not authorize stealing an in-flight productive claim.
            if token and claim_until > float(now):
                await db.rollback()
                return None
            generation = int(snapshot.get("recovery_generation") or 0) + 1
            token = new_identity()
            decision_id = f"{artifact_id}:{int(snapshot.get('recovery_epoch') or 0)}:{generation}"
            snapshot.update({
                "recovery_generation": generation,
                "recovery_claim_token": token,
                "recovery_claim_trigger": trigger.value,
                "recovery_claim_until": float(now) + max(1.0, float(lease_seconds)),
                "recovery_claim_id": decision_id,
            })
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), artifact_id, snapshot,
            )
            # durable_target is historical (sparse-audit-only, never current
            # state in any shape).
            await self._append_recovery_audit(
                db, int(row["torrent_id"]), artifact_id, "claim",
                trigger=trigger.value, generation=generation, decision_id=decision_id,
                durable_target=str(row.get("local_path") or ""),
            )
            await db.commit()
        return RecoveryClaim(
            artifact_id=artifact_id,
            token=token,
            generation=generation,
            trigger=trigger,
            decision_id=decision_id,
            target=str(row.get("local_path") or ""),
        )

    async def renew_recovery_claim(
        self,
        claim: RecoveryClaim,
        now: float,
        *,
        lease_seconds: float = 300.0,
    ) -> bool:
        """Extend a still-current fence immediately before a productive mutation."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (claim.artifact_id,),
            )
            if not row:
                await db.rollback()
                return False
            snapshot = await self._recovery_snapshot(db, claim.artifact_id, row=row)
            if (
                snapshot.get("recovery_claim_token") != claim.token
                or int(snapshot.get("recovery_generation") or 0) != claim.generation
                or float(snapshot.get("recovery_claim_until") or 0) <= float(now)
            ):
                await db.rollback()
                return False
            snapshot["recovery_claim_until"] = float(now) + max(1.0, float(lease_seconds))
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), claim.artifact_id, snapshot,
            )
            await db.commit()
        return True

    async def recovery_claim_current(self, claim: RecoveryClaim, *, now: float | None = None) -> bool:
        async with get_db() as db:
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (claim.artifact_id,),
            )
            if not row:
                return False
            snapshot = await self._recovery_snapshot(db, claim.artifact_id, row=row)
        if snapshot.get("recovery_claim_token") != claim.token:
            return False
        if int(snapshot.get("recovery_generation") or 0) != claim.generation:
            return False
        if now is not None and float(snapshot.get("recovery_claim_until") or 0) <= float(now):
            return False
        return True

    @classmethod
    async def recovery_claim_current_in_db(
        cls,
        db,
        claim: RecoveryClaim,
        *,
        now: float | None = None,
    ) -> bool:
        """Validate a recovery fence inside an already-open mutation transaction."""
        row = await db.fetchone(
            "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
            (claim.artifact_id,),
        )
        if not row:
            return False
        snapshot = await cls._recovery_snapshot(db, claim.artifact_id, row=row)
        if snapshot.get("recovery_claim_token") != claim.token:
            return False
        if int(snapshot.get("recovery_generation") or 0) != claim.generation:
            return False
        if now is not None and float(snapshot.get("recovery_claim_until") or 0) <= float(now):
            return False
        return True

    async def finish_recovery_claim(
        self,
        claim: RecoveryClaim,
        *,
        action: str | None = None,
        reason: str | None = None,
        outcome: str | None = None,
        execution_attempt: str | None = None,
        execution_identity: str | None = None,
        candidate_id: str | None = None,
        candidate_changed: bool = False,
        reconstruction_reason: str | None = None,
        retirement_reason: str | None = None,
    ) -> bool:
        """Release the fence without erasing structured facts recorded earlier."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                """SELECT torrent_id,recovery_failures,recovery_refreshes,local_path
                   FROM download_files WHERE id=?""",
                (claim.artifact_id,),
            )
            if not row:
                await db.rollback()
                return False
            snapshot = await self._recovery_snapshot(db, claim.artifact_id, row=row)
            if (
                snapshot.get("recovery_claim_token") != claim.token
                or int(snapshot.get("recovery_generation") or 0) != claim.generation
            ):
                await db.rollback()
                return False
            snapshot.update({
                "recovery_claim_token": None,
                "recovery_claim_trigger": None,
                "recovery_claim_until": 0.0,
                "recovery_claim_id": None,
                # last_applied_action/reason are surfaced by the bounded
                # Downloads/Dashboard projection (api/operational_downloads.py,
                # artifact_presentation_facts) as live explanatory text, so
                # they stay real current-state columns even though nothing in
                # the backend policy engine branches on them (presentation is
                # explicitly permitted to read audit-shaped facts; keeping
                # them here avoids re-deriving "latest applied action" from a
                # kind-specific audit-event join for a live list page).
                "last_applied_action": action,
                "last_applied_reason": reason,
            })
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), claim.artifact_id, snapshot,
            )
            # Everything else here is historical/audit trivia never read
            # back for a policy decision: recorded SOLELY in the sparse
            # audit trail, never duplicated into current state.
            audit_fields: dict = {
                "action": action, "reason": reason, "outcome": outcome,
                "last_applied_trigger": claim.trigger.value,
                "last_application_outcome": outcome,
                "last_execution_attempt": execution_attempt,
                "last_execution_identity": execution_identity,
                "durable_target": str(row.get("local_path") or ""),
            }
            if reconstruction_reason is not None:
                audit_fields["last_reconstruction_reason"] = str(reconstruction_reason)
            if retirement_reason is not None:
                audit_fields["last_execution_retirement_reason"] = str(retirement_reason)
            if candidate_changed:
                audit_fields["candidate_changed"] = True
            if candidate_id is not None:
                audit_fields["last_candidate_id"] = str(candidate_id)
            await self._append_recovery_audit(
                db, int(row["torrent_id"]), claim.artifact_id, "finish_claim", **audit_fields,
            )
            await db.commit()
        return True

    # ------------------------------------------------------------------
    # Pause/resume fencing
    # ------------------------------------------------------------------

    async def set_pause_and_fence(self, transfer_id: int, paused: bool) -> None:
        """Atomically publish Pause/Resume intent and fence every older recovery owner.

        Pausing releases any continuation reservation this transfer's
        artifacts are holding. Dispatch is not legally permitted while
        paused, so a reservation surviving a pause would consume real
        capacity for up to its full bound (``max(300, max_retry_delay)``
        seconds) for no reachable purpose; an artifact resumed later
        re-earns admission the ordinary way rather than resurrecting a
        stale hold.
        """
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """INSERT INTO transfer_pause_intents(torrent_id,paused) VALUES(?,?)
                   ON CONFLICT(torrent_id) DO UPDATE
                   SET paused=excluded.paused,updated_at=CURRENT_TIMESTAMP""",
                (transfer_id, int(paused)),
            )
            await db.execute(
                "UPDATE download_files SET continuation_reservation_expires_at=NULL WHERE torrent_id=?",
                (transfer_id,),
            )
            rows = await db.fetchall(
                """SELECT id,torrent_id,recovery_failures,recovery_refreshes,local_path
                   FROM download_files WHERE torrent_id=? AND request_id IS NOT NULL
                   AND COALESCE(mirror_state,'')!='standby'""",
                (transfer_id,),
            )
            for row in rows:
                artifact_id = int(row["id"])
                snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
                snapshot.update({
                    "recovery_generation": int(snapshot.get("recovery_generation") or 0) + 1,
                    "recovery_claim_token": None,
                    "recovery_claim_trigger": None,
                    "recovery_claim_until": 0.0,
                    "recovery_claim_id": None,
                })
                await self._save_recovery_snapshot(
                    db, int(row["torrent_id"]), artifact_id, snapshot,
                )
                await self._append_recovery_audit(
                    db, int(row["torrent_id"]), artifact_id, "pause_fence", paused=bool(paused),
                    last_application_outcome="paused" if paused else "resume_requested",
                    durable_target=str(row.get("local_path") or ""),
                )
            await db.commit()

    # ------------------------------------------------------------------
    # Quiescence
    # ------------------------------------------------------------------

    async def record_recovery_quiescence(
        self,
        claim: RecoveryClaim,
        *,
        reason: str,
        wake_condition: str,
        blocked_retry_at: float = 0.0,
    ) -> bool:
        """Publish nonproductive lifecycle truth while preserving a reusable GID."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                """SELECT torrent_id,recovery_failures,recovery_refreshes,local_path,
                          retry_at,execution_attempt_id
                   FROM download_files WHERE id=?""",
                (claim.artifact_id,),
            )
            if not row:
                await db.rollback()
                return False
            snapshot = await self._recovery_snapshot(db, claim.artifact_id, row=row)
            if (
                snapshot.get("recovery_claim_token") != claim.token
                or int(snapshot.get("recovery_generation") or 0) != claim.generation
            ):
                await db.rollback()
                return False
            retry_at = max(float(row.get("retry_at") or 0), float(blocked_retry_at or 0))
            state = "error" if reason == "recovery_exhausted" else "recovery_wait"
            await db.execute(
                """UPDATE download_files SET status=?,retry_at=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (state, retry_at, claim.artifact_id),
            )
            snapshot["quiescence_reason"] = str(reason)
            snapshot["wake_condition"] = str(wake_condition)
            snapshot["blocked_retry_at"] = max(
                float(snapshot.get("blocked_retry_at") or 0), retry_at,
            )
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), claim.artifact_id, snapshot,
            )
            # durable_target/partial_state_preserved are historical
            # (sparse-audit-only, never current state in any shape).
            await self._append_recovery_audit(
                db, int(row["torrent_id"]), claim.artifact_id, "quiescence_enter",
                reason=str(reason), wake_condition=str(wake_condition),
                durable_target=str(row.get("local_path") or ""), partial_state_preserved=True,
            )
            await db.commit()
        return True

    async def clear_recovery_quiescence(self, claim: RecoveryClaim) -> bool:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (claim.artifact_id,),
            )
            if not row:
                await db.rollback()
                return False
            snapshot = await self._recovery_snapshot(db, claim.artifact_id, row=row)
            if (
                snapshot.get("recovery_claim_token") != claim.token
                or int(snapshot.get("recovery_generation") or 0) != claim.generation
            ):
                await db.rollback()
                return False
            snapshot["quiescence_reason"] = None
            snapshot["wake_condition"] = None
            snapshot["blocked_retry_at"] = 0.0
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), claim.artifact_id, snapshot,
            )
            await self._append_recovery_audit(
                db, int(row["torrent_id"]), claim.artifact_id, "quiescence_exit",
            )
            await db.commit()
        return True

    # ------------------------------------------------------------------
    # Source-failure accounting
    # ------------------------------------------------------------------

    async def record_source_failure_once(
        self,
        artifact_id: int,
        error: NormalizedError,
        failure_identity: str,
    ) -> tuple[int, int, bool]:
        """Consume one no-progress failure for one durable failure identity."""
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
            identity = f"{int(snapshot.get('recovery_epoch') or 0)}:{failure_identity}"
            failures_before = int(row.get("recovery_failures") or 0)
            refreshes = int(row.get("recovery_refreshes") or 0)
            if snapshot.get("last_failure_identity") == identity:
                await db.rollback()
                return failures_before, refreshes, False
            cursor = await db.execute(
                "UPDATE download_files SET recovery_failures=recovery_failures+1 WHERE id=?",
                (artifact_id,),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise KeyError(artifact_id)
            failures = failures_before + 1
            signature = failure_signature(error)
            snapshot["consecutive_no_progress_failures"] = failures
            snapshot["failures_since_meaningful_progress"] = int(
                snapshot.get("failures_since_meaningful_progress") or 0
            ) + 1
            snapshot["same_signature_failures"] = (
                int(snapshot.get("same_signature_failures") or 0) + 1
                if snapshot.get("failure_signature") == signature else 1
            )
            snapshot["failure_signature"] = signature
            snapshot["last_failure_identity"] = identity
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), artifact_id, snapshot,
            )
            # last_budget_before/after are historical (sparse-audit-only,
            # never current state).
            await self._append_recovery_audit(
                db, int(row["torrent_id"]), artifact_id, "source_failure",
                failures=failures,
                last_budget_before={"failures": failures_before, "refreshes": refreshes},
                last_budget_after={"failures": failures, "refreshes": refreshes},
            )
            await db.commit()
        return failures, refreshes, True

    # ------------------------------------------------------------------
    # Decision / application audit
    # ------------------------------------------------------------------

    async def record_phase3_decision(
        self,
        claim: RecoveryClaim,
        *,
        decision_id: str,
        action: str,
        reason: str,
        error: NormalizedError | None = None,
        completed_bytes: int | None = None,
    ) -> bool:
        """Persist structured classification + policy decision facts under the fence."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (claim.artifact_id,),
            )
            if not row:
                await db.rollback()
                return False
            snapshot = await self._recovery_snapshot(db, claim.artifact_id, row=row)
            if (
                snapshot.get("recovery_claim_token") != claim.token
                or int(snapshot.get("recovery_generation") or 0) != claim.generation
            ):
                await db.rollback()
                return False
            snapshot["recovery_decision_id"] = str(decision_id)
            snapshot["decision_action"] = str(action)
            snapshot["decision_reason"] = str(reason)
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), claim.artifact_id, snapshot,
            )
            # Classification/epoch-at-decision/byte-delta facts are never
            # read back for a policy decision -- historical explainability
            # only, recorded SOLELY in the sparse audit trail, never
            # duplicated into current state in any shape.
            audit_fields: dict = {
                "decision_id": str(decision_id), "action": str(action), "reason": str(reason),
                "decision_recovery_epoch": int(snapshot.get("recovery_epoch") or 0),
            }
            if isinstance(error, NormalizedError):
                audit_fields["failure_classification"] = {
                    "domain": error.domain.value,
                    "category": error.category.value,
                    "stage": error.stage.value,
                    "retryability": error.retryability.value,
                    "permanence": error.permanence.value,
                    "origin": error.origin.value,
                }
                audit_fields["classification_confidence"] = error.confidence.value
                audit_fields["classification_evidence"] = error.evidence_basis.value
            if completed_bytes is not None:
                value = max(0, int(completed_bytes))
                # The only prior value of bytes_at_failure now reachable is
                # whatever the sparse audit trail itself last recorded --
                # there is no current-state copy to diff against anymore.
                previous_facts = await self._historical_audit_facts(
                    db, claim.artifact_id, int(row["torrent_id"]),
                )
                previous = previous_facts.get("bytes_at_failure")
                audit_fields["bytes_since_prior_failure"] = (
                    None if previous is None else max(0, value - int(previous))
                )
                audit_fields["bytes_at_failure"] = value
            await self._append_recovery_audit(
                db, int(row["torrent_id"]), claim.artifact_id, "decision", **audit_fields,
            )
            await db.commit()
        return True

    async def record_phase3_application(
        self,
        claim: RecoveryClaim,
        *,
        action: str,
        reason: str,
        reconstruction_reason: str | None = None,
        retirement_reason: str | None = None,
        target_change_reason: str | None = None,
        partial_preserved: bool = True,
    ) -> bool:
        """Persist execution-application facts separately from policy classification."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                """SELECT torrent_id,recovery_failures,recovery_refreshes,local_path
                   FROM download_files WHERE id=?""",
                (claim.artifact_id,),
            )
            if not row:
                await db.rollback()
                return False
            snapshot = await self._recovery_snapshot(db, claim.artifact_id, row=row)
            if (
                snapshot.get("recovery_claim_token") != claim.token
                or int(snapshot.get("recovery_generation") or 0) != claim.generation
            ):
                await db.rollback()
                return False
            # Every field this method records is historical/audit trivia --
            # never read back for a policy decision -- so this is a pure
            # sparse audit append; it never touches the current-state row
            # at all.
            audit_fields: dict = {
                "action": str(action), "reason": str(reason),
                "durable_target": str(row.get("local_path") or ""),
                "partial_state_preserved": bool(partial_preserved),
                "target_change_reason": target_change_reason,
            }
            if action == "refresh_candidate":
                audit_fields["last_refresh_reason"] = str(reason)
            if action == "try_alternate_candidate":
                audit_fields["last_candidate_switch_reason"] = str(reason)
            if action == "fail_permanently":
                audit_fields["last_terminalization_reason"] = str(reason)
            if reconstruction_reason is not None:
                audit_fields["last_reconstruction_reason"] = str(reconstruction_reason)
            if retirement_reason is not None:
                audit_fields["last_execution_retirement_reason"] = str(retirement_reason)
            await self._append_recovery_audit(
                db, int(row["torrent_id"]), claim.artifact_id, "application", **audit_fields,
            )
            await db.commit()
        return True

    async def retire_stale_materialization_if_claim_current(
        self, claim: RecoveryClaim, artifact_id: int, transfer_id: int, request_id: str,
    ) -> bool:
        """Atomically fence STALE retirement's detach/requeue mutation behind
        the SAME recovery claim ``_cancel_and_confirm_stopped`` already
        confirmed terminal (Gate 9 revision-7 rejection: the prior sequence
        -- revalidate the claim, THEN separately call ``artifact_state()``/
        ``retry_requests()`` -- left a window between that check and the
        mutation where a concurrent recovery owner (e.g. a pause/resume fence
        via ``set_pause_and_fence``) could advance ``recovery_generation``,
        making the claim stale while the caller still went on to perform the
        detach/requeue as an independent, unfenced transaction. Verifying the
        claim and performing the detach + release + requeue inside ONE
        ``BEGIN IMMEDIATE`` transaction makes that window structurally
        impossible: either both happen together, or neither does.

        Replicates ``TransferRepository.artifact_state(artifact_id,
        "unresolved", release=True)`` followed by
        ``TransferRepository.retry_requests(transfer_id, request_id=request_id)``
        -- never a parallel policy decision -- fenced by the same claim
        token/generation check every other claim-scoped mutation in this
        module uses.
        """
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                """SELECT torrent_id,recovery_failures,recovery_refreshes,execution_attempt_id
                   FROM download_files WHERE id=?""",
                (artifact_id,),
            )
            if not row:
                await db.rollback()
                return False
            snapshot = await self._recovery_snapshot(db, artifact_id, row=row)
            if (
                snapshot.get("recovery_claim_token") != claim.token
                or int(snapshot.get("recovery_generation") or 0) != claim.generation
            ):
                await db.rollback()
                return False
            current_execution_id = row.get("execution_attempt_id")
            cursor = await db.execute(
                """UPDATE download_files SET status='unresolved',normalized_error=NULL,retry_at=0,
                    execution_attempt_id=NULL,continuation_reservation_expires_at=NULL,
                    updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND torrent_id IN (SELECT id FROM torrents
                        WHERE status NOT IN ('deleted','consolidated','cancelled'))""",
                (artifact_id,),
            )
            if not cursor.rowcount:
                await db.rollback()
                return False
            if current_execution_id:
                await db.execute(
                    """UPDATE execution_attempts SET authorized=0,updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND state IN ('failed','absent','cancelled','succeeded')""",
                    (current_execution_id,),
                )
            await db.execute(
                """UPDATE transfer_requests SET state='pending',retry_at=0,error=NULL
                    WHERE transfer_id=? AND transfer_id IN
                        (SELECT id FROM torrents WHERE status NOT IN ('completed','consolidated','deleted','cancelled'))
                        AND id=?""",
                (transfer_id, request_id),
            )
            # No audit append here: this mirrors ``artifact_state()``/
            # ``retry_requests()`` (neither of which touches the sparse
            # audit trail either) plus the atomic claim fence -- nothing
            # more. The caller (``_reconcile_unauthorized_existing_
            # execution``'s ``finally`` block) already records the
            # "retired"/``materialization_superseded`` provenance via
            # ``record_phase3_application``/``finish_recovery_claim`` once
            # it observes this call actually succeeded; duplicating that
            # here would create two audit rows for one fact.
            await db.commit()
        return True

    # ------------------------------------------------------------------
    # Refresh single-flight
    # ------------------------------------------------------------------

    async def reserve_recovery_refresh(
        self,
        claim: RecoveryClaim,
        decision_id: str,
        *,
        limit: int = 1,
    ) -> bool:
        """Idempotently reserve the core-selected refresh exactly once."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (claim.artifact_id,),
            )
            if not row:
                await db.rollback()
                return False
            snapshot = await self._recovery_snapshot(db, claim.artifact_id, row=row)
            if (
                snapshot.get("recovery_claim_token") != claim.token
                or int(snapshot.get("recovery_generation") or 0) != claim.generation
            ):
                await db.rollback()
                return False
            if snapshot.get("last_refresh_decision_id") == decision_id:
                await db.rollback()
                return True
            refreshes = int(row.get("recovery_refreshes") or 0)
            if refreshes >= max(1, int(limit)):
                await db.rollback()
                return False
            cursor = await db.execute(
                """UPDATE download_files SET recovery_refreshes=recovery_refreshes+1
                   WHERE id=? AND recovery_refreshes=?""",
                (claim.artifact_id, refreshes),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return False
            snapshot["candidate_refreshes"] = max(
                int(snapshot.get("candidate_refreshes") or 0), refreshes + 1,
            )
            snapshot["last_refresh_decision_id"] = str(decision_id)
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), claim.artifact_id, snapshot,
            )
            # last_budget_before/after are historical (sparse-audit-only,
            # never current state).
            await self._append_recovery_audit(
                db, int(row["torrent_id"]), claim.artifact_id, "refresh_reserved",
                decision_id=str(decision_id),
                last_budget_before={"failures": int(row.get("recovery_failures") or 0), "refreshes": refreshes},
                last_budget_after={"failures": int(row.get("recovery_failures") or 0), "refreshes": refreshes + 1},
            )
            await db.commit()
        return True

    async def begin_recovery_refresh(
        self,
        claim: RecoveryClaim,
        record,
        provider_id: str,
        decision_id: str,
    ):
        """Single-flight durable refresh attempt for one core decision."""
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (claim.artifact_id,),
            )
            if not row:
                await db.rollback()
                return None
            snapshot = await self._recovery_snapshot(db, claim.artifact_id, row=row)
            if (
                snapshot.get("recovery_claim_token") != claim.token
                or int(snapshot.get("recovery_generation") or 0) != claim.generation
                or snapshot.get("last_refresh_decision_id") != decision_id
            ):
                await db.rollback()
                return None
            previous = snapshot.get("refresh_inflight_attempt_id")
            if (
                previous
                and snapshot.get("refresh_inflight_decision_id") == decision_id
            ):
                attempt = await db.fetchone(
                    "SELECT id,state,error,result FROM resolution_attempts WHERE id=?",
                    (previous,),
                )
                await db.rollback()
                return {
                    "attempt_id": previous,
                    "created": False,
                    "state": (attempt or {}).get("state"),
                    "error": (attempt or {}).get("error"),
                    "result": (attempt or {}).get("result"),
                }
            identity = new_identity()
            await db.execute(
                "INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES(?,?,?,'started')",
                (identity, record.id, provider_id),
            )
            await self._begin_route_provenance(
                db, identity, record.transfer_id, record.id, provider_id, operation="refresh",
            )
            snapshot["refresh_inflight_decision_id"] = str(decision_id)
            snapshot["refresh_inflight_attempt_id"] = identity
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), claim.artifact_id, snapshot,
            )
            await self._append_recovery_audit(
                db, int(row["torrent_id"]), claim.artifact_id, "refresh_begin",
                decision_id=str(decision_id), attempt_id=identity,
            )
            await db.commit()
        return {
            "attempt_id": identity,
            "created": True,
            "state": "started",
            "error": None,
            "result": None,
        }

    async def clear_recovery_refresh_inflight(
        self,
        claim: RecoveryClaim,
        decision_id: str,
    ) -> bool:
        async with get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await db.fetchone(
                "SELECT torrent_id,recovery_failures,recovery_refreshes FROM download_files WHERE id=?",
                (claim.artifact_id,),
            )
            if not row:
                await db.rollback()
                return False
            snapshot = await self._recovery_snapshot(db, claim.artifact_id, row=row)
            if (
                snapshot.get("recovery_claim_token") != claim.token
                or int(snapshot.get("recovery_generation") or 0) != claim.generation
            ):
                await db.rollback()
                return False
            if snapshot.get("refresh_inflight_decision_id") == decision_id:
                snapshot["refresh_inflight_decision_id"] = None
                snapshot["refresh_inflight_attempt_id"] = None
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), claim.artifact_id, snapshot,
            )
            await db.commit()
        return True
