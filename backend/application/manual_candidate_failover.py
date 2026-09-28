"""Application command for operator-requested canonical candidate activation."""
from __future__ import annotations

from transfers.manual_failover import manual_candidate_failover, preview_candidate_switch


async def switch_candidate(
    application,
    transfer_id: int,
    artifact_id: int,
    candidate_id: str,
    *,
    discard_confirmed: bool = False,
    discard_confirmation: dict | None = None,
) -> dict:
    async with application.application_operation():
        await application.require(int(transfer_id))
        result = await manual_candidate_failover(
            application.engine,
            int(transfer_id),
            int(artifact_id),
            str(candidate_id),
            discard_confirmed=bool(discard_confirmed),
            discard_confirmation=discard_confirmation,
        )
        application.execution_wakeup.set()
        await application._publish(int(transfer_id))
        return result


async def preview_switch(application, transfer_id: int, artifact_id: int, candidate_id: str) -> dict:
    """Read-only: the continuation consequence of one requested switch."""
    await application.require(int(transfer_id))
    return await preview_candidate_switch(application.engine, int(transfer_id), int(artifact_id), str(candidate_id))
