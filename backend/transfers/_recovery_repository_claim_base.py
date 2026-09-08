"""Canonical Phase-3 recovery repository surface.

Claims are exclusive across all trigger types, including USER_RETRY. Operator
authority changes what the common recovery machinery may do after it owns the
artifact; it does not authorize stealing an in-flight productive claim.
"""
from __future__ import annotations

from db.database import get_db
from transfers._recovery_repository_audit import TransferRepository as _AuditedRecoveryRepository
from transfers.models import new_identity
from transfers.recovery_execution import RecoveryClaim, RecoveryTrigger


class TransferRepository(_AuditedRecoveryRepository):
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
                "durable_target": str(row.get("local_path") or snapshot.get("durable_target") or ""),
            })
            await self._save_recovery_snapshot(
                db, int(row["torrent_id"]), artifact_id, snapshot,
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
