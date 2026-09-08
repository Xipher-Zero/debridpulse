"""Final public Phase-3 recovery repository surface."""
from __future__ import annotations

from db.database import get_db
from transfers._recovery_repository_claim_base import TransferRepository as _ClaimQualifiedRepository
from transfers.recovery_execution import RecoveryClaim


class TransferRepository(_ClaimQualifiedRepository):
    """Production durable recovery owner used by Phase-3 composition."""

    async def reset_retry_budget(self, artifact_id):
        """Reopen bounded recovery without fabricating progress or stale dedupe."""
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
                """UPDATE download_files SET retry_count=0,recovery_failures=0,recovery_refreshes=0
                   WHERE id=?""",
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
                "blocked_retry_at": 0.0,
                "recovery_decision_id": None,
                "last_failure_identity": None,
                "last_budget_before": None,
                "last_budget_after": None,
            })
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), artifact_id, snapshot,
            )
            await db.commit()

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
                "last_applied_trigger": claim.trigger.value,
                "last_applied_action": action,
                "last_applied_reason": reason,
                "last_application_outcome": outcome,
                "last_execution_attempt": execution_attempt,
                "last_execution_identity": execution_identity,
                "durable_target": str(
                    row.get("local_path") or snapshot.get("durable_target") or ""
                ),
            })
            if reconstruction_reason is not None:
                snapshot["last_reconstruction_reason"] = str(reconstruction_reason)
            if retirement_reason is not None:
                snapshot["last_execution_retirement_reason"] = str(retirement_reason)
            if candidate_changed:
                snapshot["candidate_generation"] = int(
                    snapshot.get("candidate_generation") or 0
                ) + 1
            if candidate_id is not None:
                snapshot["last_candidate_id"] = str(candidate_id)
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), claim.artifact_id, snapshot,
            )
            await db.commit()
        return True
