from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from services.transfer_integrity import TransferIntegrityManager
from services.transfer_runtime_guard import GuardedTransferIntegrityManager


@pytest.mark.asyncio
async def test_delete_does_not_wait_for_aria2_state_lock(monkeypatch):
    """Reconciliation may hold aria2 state while finalization enters lifecycle."""
    manager = GuardedTransferIntegrityManager()
    base_delete = AsyncMock(return_value=None)

    monkeypatch.setattr(
        manager,
        "_load_transfer_row",
        AsyncMock(
            return_value={
                "id": 41,
                "hash": "4" * 40,
                "name": "Lock order",
                "status": "queued",
                "source": "manual",
                "alldebrid_id": "",
            }
        ),
    )
    monkeypatch.setattr(
        TransferIntegrityManager,
        "delete_torrent",
        base_delete,
    )

    await manager._aria2_state_lock.acquire()
    try:
        await asyncio.wait_for(
            manager.delete_torrent(41, delete_from_ad=False),
            timeout=0.25,
        )
    finally:
        manager._aria2_state_lock.release()

    base_delete.assert_awaited_once_with(41, delete_from_ad=False)
