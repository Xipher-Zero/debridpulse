"""Phase-3 recovery persistence corrections and audit completion.

This public owner layers lifecycle-visible quiescence, transaction-bound fencing,
and structured recovery application provenance over the durable Phase-3 claim
foundation. Current recovery state lives in the canonical
``artifact_recovery_state`` row (``transfers.repository.TransferRepository``);
every fact this layer records is historical/explainability-only (Section 15)
and is written SOLELY to the sparse ``recovery_audit`` trail via
``_append_recovery_audit`` -- never duplicated into current state in any
shape. This class therefore needs no ``_recovery_snapshot`` override of its
own: it has no current-state fields to default.
"""
from __future__ import annotations

from db.database import get_db
from transfers._recovery_repository_phase3 import TransferRepository as _Phase3RecoveryRepository
from transfers.errors import NormalizedError
from transfers.recovery_execution import RecoveryClaim


class TransferRepository(_Phase3RecoveryRepository):
    """Canonical Phase-3 repository surface used by production composition."""

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
            # durable_target/partial_state_preserved are historical (Section
            # 15): sparse-audit-only, never current state in any shape.
            await self._append_recovery_audit(
                db, int(row["torrent_id"]), claim.artifact_id, "quiescence_enter",
                reason=str(reason), wake_condition=str(wake_condition),
                durable_target=str(row.get("local_path") or ""), partial_state_preserved=True,
            )
            await db.commit()
        return True

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
            # Section 15: classification/epoch-at-decision/byte-delta facts
            # are never read back for a policy decision -- historical
            # explainability only, recorded SOLELY in the sparse audit trail,
            # never duplicated into current state in any shape.
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
            # Section 15: every field this method records is historical/audit
            # trivia -- never read back for a policy decision (verified
            # before this leveling pass) -- so this is a pure sparse audit
            # append; it never touches the current-state row at all.
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
