"""Final public Phase-3 recovery convergence surface.

The coordinator and recovery helpers are inherited from the qualified Phase-3
owner.  This final layer keeps manual Retry's trigger-specific validation and
operator authority serialized with the same transfer lock, so repeated Retry
requests coalesce after the first invocation has already made the work current.
"""
from __future__ import annotations

import asyncio

from transfers._convergence_phase3_public_base import TransferEngine as _RecoveryCoordinator
from transfers.models import TransferState
from transfers.recovery_execution import RecoveryTrigger


class TransferEngine(_RecoveryCoordinator):
    """Production Phase-3 transfer engine."""

    async def _retry_actionable(self, artifacts) -> bool:
        for artifact in artifacts:
            if artifact.state == "completed":
                continue
            context = await self.repository.recovery_context(artifact.id)
            if artifact.state in {
                "error", "recovery_wait", "lost", "unresolved", "refresh_pending",
            }:
                return True
            if context.get("quiescence_reason") in {
                "recovery_exhausted", "provider_disabled", "executor_unavailable",
                "storage_unavailable", "retry_backoff",
            }:
                return True
        return False

    async def retry(self, transfer_id: int, *, reacquire=False):
        """Operator Retry is a serialized trigger adapter, not a recovery algorithm."""
        if reacquire:
            return await super().retry(transfer_id, reacquire=True)

        lock = self._transfer_locks.setdefault(transfer_id, asyncio.Lock())
        async with lock:
            transfer = await self.repository.get(transfer_id)
            if transfer is None:
                raise KeyError(transfer_id)
            if transfer.state in {TransferState.CONSOLIDATED, TransferState.DELETED}:
                return False
            if await self.challenges.current(transfer_id):
                return False
            if any(
                pending for _resource, _state, pending
                in await self.repository.resources(transfer_id)
            ):
                return False
            if not await self.repository.reset_postprocessing(transfer_id):
                return False

            artifacts = await self.repository.artifacts(transfer_id)
            if artifacts and not await self._retry_actionable(artifacts):
                return True

            if not await self.repository.state(
                transfer_id,
                TransferState.QUEUED,
                operator=True,
                expected_epoch=transfer.epoch,
            ):
                return False

            if not artifacts:
                await self.repository.retry_requests(transfer_id, reset_budget=True)
                return True

            ok = True
            for artifact in artifacts:
                if artifact.state == "completed":
                    continue
                applied = await self.recover_artifact(
                    artifact,
                    trigger=RecoveryTrigger.USER_RETRY,
                )
                if not applied:
                    current = next(
                        (
                            item for item in await self.repository.artifacts(transfer_id)
                            if item.id == artifact.id
                        ),
                        None,
                    )
                    # A concurrent canonical owner may have made the work
                    # productive while this operator request lost/coalesced its
                    # claim.  Treat that as success without reapplying authority.
                    if current is None or await self._retry_actionable((current,)):
                        ok = False
            await self.repository.retry_requests(transfer_id, reset_budget=True)
            await self._aggregate(transfer_id)
            return ok
