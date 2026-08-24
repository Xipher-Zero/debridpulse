from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from core.config import AppSettings, apply_settings
import services.transfer_runtime_guard as runtime_guard
from services.transfer_runtime_guard import GuardedTransferIntegrityManager


class _Cursor:
    def __init__(self, row=None, rowcount=1):
        self._row = row
        self.rowcount = rowcount

    async def fetchone(self):
        return self._row


class _RetryDb:
    def __init__(self):
        self.cleared_old_id = asyncio.Event()
        self.writes: list[tuple[str, tuple]] = []

    async def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split())
        if "SELECT upload_retry_count, status FROM torrents" in normalized:
            return _Cursor({"upload_retry_count": 0, "status": "uploading"})
        self.writes.append((normalized, tuple(params)))
        if "SET alldebrid_id=NULL" in normalized:
            self.cleared_old_id.set()
        return _Cursor(rowcount=1)

    async def commit(self):
        return None


def _get_db(fake: _RetryDb):
    @asynccontextmanager
    async def _ctx():
        yield fake

    return _ctx


@pytest.mark.asyncio
async def test_retry_clears_confirmed_deleted_provider_id_before_wait(monkeypatch):
    manager = GuardedTransferIntegrityManager()
    fake_db = _RetryDb()
    provider = AsyncMock()
    provider.delete_magnet.return_value = True

    monkeypatch.setattr(runtime_guard, "get_db", _get_db(fake_db))
    monkeypatch.setattr(manager, "ad", lambda: provider)

    apply_settings(
        AppSettings(
            upload_fail_retry_count=3,
            upload_fail_retry_delay_minutes=1,
        )
    )

    row = {
        "id": 51,
        "name": "Retry identity",
        "alldebrid_id": "old-provider-id",
        "magnet": f"magnet:?xt=urn:btih:{'5' * 40}",
        "source": "manual",
    }

    task = asyncio.create_task(
        manager._handle_upload_failed(row, "provider upload failed")
    )
    await asyncio.wait_for(fake_db.cleared_old_id.wait(), timeout=0.25)
    manager._begin_delete_intent(51)
    await asyncio.wait_for(task, timeout=0.25)

    provider.delete_magnet.assert_awaited_once_with("old-provider-id")
    assert any(
        "SET alldebrid_id=NULL" in sql
        and params == (51, "old-provider-id")
        for sql, params in fake_db.writes
    )
    assert provider.upload_magnet.await_count == 0
