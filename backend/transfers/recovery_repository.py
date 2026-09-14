"""Final public Phase-3 recovery repository surface."""
from __future__ import annotations

from db.database import get_db
from transfers._recovery_repository_claim_base import TransferRepository as _ClaimQualifiedRepository
from transfers.recovery_execution import RecoveryClaim
from transfers.repository import RecoveryResetAuthority, apply_recovery_reset


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
            apply_recovery_reset(snapshot, RecoveryResetAuthority.OPERATOR_RETRY)
            snapshot.update({
                "blocked_retry_at": 0.0,
                "recovery_decision_id": None,
                "last_failure_identity": None,
                # Section 12: a full budget reset (the live USER_RETRY
                # mechanism, TriggerAuthority.reset_exhaustion) is the one
                # explicit "start over" boundary that restores every
                # candidate's eligibility, including ones already tried.
                "candidate_attempt_history": [],
            })
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), artifact_id, snapshot,
            )
            # last_budget_before/after are historical (Section 15):
            # sparse-audit-only, never current state in any shape.
            await self._append_recovery_audit(
                db, int(row["torrent_id"]), artifact_id, "operator_retry",
                last_budget_before={
                    "failures": int(row.get("recovery_failures") or 0),
                    "refreshes": int(row.get("recovery_refreshes") or 0),
                },
                last_budget_after={"failures": 0, "refreshes": 0},
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
                # last_applied_action/reason are surfaced by the bounded
                # Downloads/Dashboard projection (api/operational_downloads.py,
                # artifact_presentation_facts) as live explanatory text, so
                # they stay real current-state columns even though nothing in
                # the backend policy engine branches on them (Section 15
                # explicitly permits presentation to read audit-shaped facts;
                # keeping them here avoids re-deriving "latest applied
                # action" from a kind-specific audit-event join for a live
                # list page).
                "last_applied_action": action,
                "last_applied_reason": reason,
            })
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), claim.artifact_id, snapshot,
            )
            # Everything else here is historical/audit trivia never read
            # back for a policy decision (Section 15): recorded SOLELY in
            # the sparse audit trail, never duplicated into current state.
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
