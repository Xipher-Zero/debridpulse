"""Phase-3 recovery persistence corrections and audit completion.

This public owner layers lifecycle-visible quiescence, transaction-bound fencing,
and structured recovery application provenance over the durable Phase-3 claim
foundation. Recovery state remains in the existing ``application_events``
history; no second history store or recovery policy is introduced.
"""
from __future__ import annotations

from db.database import get_db
from transfers import codec
from transfers._recovery_repository_phase3 import TransferRepository as _Phase3RecoveryRepository
from transfers.errors import NormalizedError
from transfers.recovery_execution import RecoveryClaim


_AUDIT_DEFAULTS = {
    "decision_recovery_epoch": None,
    "failure_classification": None,
    "classification_confidence": None,
    "classification_evidence": None,
    "bytes_at_failure": None,
    "bytes_since_prior_failure": None,
    "last_refresh_reason": None,
    "last_candidate_switch_reason": None,
    "last_reconstruction_reason": None,
    "last_execution_retirement_reason": None,
    "last_terminalization_reason": None,
    "partial_state_preserved": None,
    "target_change_reason": None,
}


class TransferRepository(_Phase3RecoveryRepository):
    """Canonical Phase-3 repository surface used by production composition."""

    @classmethod
    async def _recovery_snapshot(cls, db, artifact_id: int, *, row=None) -> dict:
        snapshot = await super()._recovery_snapshot(db, artifact_id, row=row)
        event = await db.fetchone(
            "SELECT detail FROM application_events WHERE kind=? ORDER BY id DESC LIMIT 1",
            (cls._recovery_event_kind(artifact_id),),
        )
        stored = {}
        if event and event.get("detail"):
            try:
                value = codec.load(event["detail"], {})
            except (TypeError, ValueError):
                value = {}
            if isinstance(value, dict):
                stored = value
        for key, default in _AUDIT_DEFAULTS.items():
            snapshot[key] = stored.get(key, snapshot.get(key, default))
        return snapshot

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
            snapshot["durable_target"] = str(
                row.get("local_path") or snapshot.get("durable_target") or ""
            )
            snapshot["partial_state_preserved"] = True
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), claim.artifact_id, snapshot,
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
            snapshot["decision_recovery_epoch"] = int(snapshot.get("recovery_epoch") or 0)
            if isinstance(error, NormalizedError):
                snapshot["failure_classification"] = {
                    "domain": error.domain.value,
                    "category": error.category.value,
                    "stage": error.stage.value,
                    "retryability": error.retryability.value,
                    "permanence": error.permanence.value,
                    "origin": error.origin.value,
                }
                snapshot["classification_confidence"] = error.confidence.value
                snapshot["classification_evidence"] = error.evidence_basis.value
            if completed_bytes is not None:
                value = max(0, int(completed_bytes))
                previous = snapshot.get("bytes_at_failure")
                snapshot["bytes_since_prior_failure"] = (
                    None if previous is None else max(0, value - int(previous))
                )
                snapshot["bytes_at_failure"] = value
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), claim.artifact_id, snapshot,
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
            snapshot["durable_target"] = str(
                row.get("local_path") or snapshot.get("durable_target") or ""
            )
            snapshot["partial_state_preserved"] = bool(partial_preserved)
            snapshot["target_change_reason"] = target_change_reason
            if action == "refresh_candidate":
                snapshot["last_refresh_reason"] = str(reason)
            if action == "try_alternate_candidate":
                snapshot["last_candidate_switch_reason"] = str(reason)
            if action == "fail_permanently":
                snapshot["last_terminalization_reason"] = str(reason)
            if reconstruction_reason is not None:
                snapshot["last_reconstruction_reason"] = str(reconstruction_reason)
            if retirement_reason is not None:
                snapshot["last_execution_retirement_reason"] = str(retirement_reason)
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), claim.artifact_id, snapshot,
            )
            await db.commit()
        return True
