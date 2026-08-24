from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

import services.provider_gateway as provider_gateway_module
from services.provider_gateway import ProviderGateway


class _DeletedRowsDb:
    async def fetchall(self, sql, params=()):
        assert "status='deleted'" in sql
        return [{"id": 31}, {"id": 32}]


@asynccontextmanager
async def _deleted_rows_db():
    yield _DeletedRowsDb()


class _Engine:
    def __init__(self):
        self._deleted_transfer_ids: set[int] = set()
        self._events: dict[int, asyncio.Event] = {}
        self.reconcile_provider_inventory = AsyncMock(
            return_value={"imported": 0, "updated": 0, "snapshot_count": 0}
        )
        self.import_existing_magnets = AsyncMock(return_value=[])
        self.full_alldebrid_sync = AsyncMock(return_value={"ok": True})

    def _delete_event(self, transfer_id: int) -> asyncio.Event:
        return self._events.setdefault(int(transfer_id), asyncio.Event())


@pytest.mark.asyncio
async def test_reconcile_primes_persisted_delete_tombstones(monkeypatch):
    engine = _Engine()
    gateway = ProviderGateway(engine)
    monkeypatch.setattr(provider_gateway_module, "get_db", _deleted_rows_db)

    result = await gateway.reconcile_inventory()

    assert result["imported"] == 0
    assert engine._deleted_transfer_ids == {31, 32}
    assert engine._delete_event(31).is_set()
    assert engine._delete_event(32).is_set()
    engine.reconcile_provider_inventory.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_manual_import_primes_persisted_delete_tombstones(monkeypatch):
    engine = _Engine()
    gateway = ProviderGateway(engine)
    monkeypatch.setattr(provider_gateway_module, "get_db", _deleted_rows_db)

    result = await gateway.import_existing()

    assert result == []
    assert engine._deleted_transfer_ids == {31, 32}
    engine.import_existing_magnets.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_full_sync_primes_persisted_delete_tombstones(monkeypatch):
    engine = _Engine()
    gateway = ProviderGateway(engine)
    monkeypatch.setattr(provider_gateway_module, "get_db", _deleted_rows_db)

    result = await gateway.full_sync()

    assert result == {"ok": True}
    assert engine._deleted_transfer_ids == {31, 32}
    assert engine._delete_event(31).is_set()
    assert engine._delete_event(32).is_set()
    engine.full_alldebrid_sync.assert_awaited_once_with()
