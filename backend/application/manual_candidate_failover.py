"""Application command for operator-requested canonical candidate activation."""
from __future__ import annotations

from transfers.manual_failover import manual_candidate_failover


async def switch_candidate(
    application,
    transfer_id: int,
    artifact_id: int,
    candidate_id: str,
    *,
    discard_confirmed: bool = False,
) -> dict:
    async with application.application_operation():
        await application.require(int(transfer_id))
        result = await manual_candidate_failover(
            application.engine,
            int(transfer_id),
            int(artifact_id),
            str(candidate_id),
            discard_confirmed=bool(discard_confirmed),
        )
        application.execution_wakeup.set()
        await application._publish(int(transfer_id))
        return result
