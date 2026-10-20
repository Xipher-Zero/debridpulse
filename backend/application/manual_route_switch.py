"""Application command and read for an operator's root provider route."""
from __future__ import annotations

from transfers.manual_route_switch import route_status, switch_root_provider


async def switch_route_provider(application, transfer_id: int, provider_id: str, *, expected_provider_id: str,
                                expected_demand=None, artifact_id: int | None = None,
                                expected_root_id: str | None = None) -> dict:
    # Inside the one operator pause-control boundary: the switch's temporary
    # writer fence can never clear a Pause or Pause All issued meanwhile.
    async with application.operator_controls, application.application_operation():
        await application.require(int(transfer_id))
        result = await switch_root_provider(application.engine, int(transfer_id), str(provider_id),
                                            expected_provider_id=str(expected_provider_id),
                                            expected_demand=expected_demand, artifact_id=artifact_id,
                                            expected_root_id=expected_root_id)
        # The replacement left the root pending on its new route: resolution
        # work is runnable now, not at the next provider poll. The engine's
        # own opportunity reaches only a cycle already running.
        application.resolution_wakeup.set()
        application.execution_wakeup.set()
        await application._publish(int(transfer_id))
        return result


async def root_route_providers(application, transfer_id: int, *, artifact_id: int | None = None) -> dict | None:
    """Read-only: the provider status of one root's route -- the torrent
    root's, the transfer's only root's, or the root that produced the selected
    candidate of ``artifact_id``."""
    await application.require(int(transfer_id))
    return await route_status(application.engine, int(transfer_id), artifact_id)
