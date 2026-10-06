"""Application command and read for an operator's torrent-root provider route."""
from __future__ import annotations

from transfers.manual_route_switch import route_providers, switch_root_provider


async def switch_route_provider(application, transfer_id: int, provider_id: str, *, expected_provider_id: str) -> dict:
    # Inside the one operator pause-control boundary: the switch's temporary
    # writer fence can never clear a Pause or Pause All issued meanwhile.
    async with application.operator_controls, application.application_operation():
        await application.require(int(transfer_id))
        result = await switch_root_provider(application.engine, int(transfer_id), str(provider_id),
                                            expected_provider_id=str(expected_provider_id))
        application.execution_wakeup.set()
        await application._publish(int(transfer_id))
        return result


async def root_route_providers(application, transfer_id: int) -> dict | None:
    """Read-only: the provider status of one torrent root's route."""
    await application.require(int(transfer_id))
    return await route_providers(application.engine, int(transfer_id))
