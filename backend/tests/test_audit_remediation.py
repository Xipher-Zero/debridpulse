from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from core.config import AppSettings, apply_settings
from services.transfer_integrity import TransferIntegrityManager
import services.transfer_runtime_guard as runtime_guard
from services.transfer_runtime_guard import (
    GuardedTransferIntegrityManager,
    reject_non_public_resolution,
)


class _FakeCursor:
    def __init__(self, row=None, rowcount=1):
        self._row = row
        self.rowcount = rowcount

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return []


class _FakeDb:
    def __init__(self):
        self.writes: list[tuple[str, tuple]] = []
        self.status = "pending"
        self.alldebrid_id = None
        self.retry_count = 0

    async def fetchone(self, sql, params=()):
        if "SELECT status, alldebrid_id" in sql:
            return {
                "status": self.status,
                "alldebrid_id": self.alldebrid_id,
            }
        return None

    async def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split())
        if "SELECT upload_retry_count, status FROM torrents" in normalized:
            return _FakeCursor(
                {
                    "upload_retry_count": self.retry_count,
                    "status": self.status,
                }
            )
        self.writes.append((normalized, tuple(params)))
        return _FakeCursor(rowcount=1)

    async def commit(self):
        return None


def _fake_get_db(fake_db: _FakeDb):
    @asynccontextmanager
    async def _ctx():
        yield fake_db

    return _ctx


def test_manifest_collision_rejected_after_sanitization(tmp_path):
    apply_settings(
        AppSettings(
            download_folder=str(tmp_path),
            filters_enabled=False,
        )
    )
    files = [
        {
            "path": "Example/A:B.bin",
            "size": 4,
            "link": "https://downloads.example/one",
        },
        {
            "path": "Example/A?B.bin",
            "size": 4,
            "link": "https://downloads.example/two",
        },
    ]

    with pytest.raises(ValueError, match="path collision"):
        GuardedTransferIntegrityManager._validate_manifest_destinations(
            "Example", files
        )


def test_manifest_duplicate_entry_same_identity_is_not_collision(tmp_path):
    apply_settings(
        AppSettings(
            download_folder=str(tmp_path),
            filters_enabled=False,
        )
    )
    duplicate = {
        "path": "Example/file.bin",
        "size": 4,
        "link": "https://downloads.example/file",
    }
    GuardedTransferIntegrityManager._validate_manifest_destinations(
        "Example", [duplicate, dict(duplicate)]
    )


def test_dns_policy_rejects_private_or_mixed_answers():
    with pytest.raises(ValueError, match="non-public"):
        reject_non_public_resolution(
            ["8.8.8.8", "10.0.0.5"],
            host="downloads.example",
        )


def test_dns_policy_accepts_public_answers():
    reject_non_public_resolution(
        ["8.8.8.8", "1.1.1.1"],
        host="downloads.example",
    )


@pytest.mark.asyncio
async def test_delete_waits_for_inflight_materialization(monkeypatch):
    manager = GuardedTransferIntegrityManager()
    started = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def fake_fetch(self, ad_id):
        return [
            {
                "path": "Example/file.bin",
                "size": 4,
                "link": "https://downloads.example/file",
            }
        ]

    async def fake_materialize(self, torrent_id, ad_id, name):
        order.append("materialize-start")
        started.set()
        await release.wait()
        order.append("materialize-end")

    async def fake_delete(self, torrent_id, delete_from_ad=True):
        order.append("delete")

    monkeypatch.setattr(
        TransferIntegrityManager, "_fetch_ready_files", fake_fetch
    )
    monkeypatch.setattr(
        TransferIntegrityManager, "_engine_download", fake_materialize
    )
    monkeypatch.setattr(
        TransferIntegrityManager, "delete_torrent", fake_delete
    )
    monkeypatch.setattr(
        manager,
        "_load_transfer_row",
        AsyncMock(
            return_value={
                "id": 7,
                "hash": "a" * 40,
                "name": "Example",
                "status": "ready",
                "source": "manual",
                "alldebrid_id": "",
            }
        ),
    )

    apply_settings(
        AppSettings(download_folder="/download", filters_enabled=False)
    )

    materialize_task = asyncio.create_task(
        manager._engine_download(7, "123", "Example")
    )
    await started.wait()

    delete_task = asyncio.create_task(
        manager.delete_torrent(7, delete_from_ad=False)
    )
    await asyncio.sleep(0)
    assert order == ["materialize-start"]

    release.set()
    await materialize_task
    await delete_task

    assert order == ["materialize-start", "materialize-end", "delete"]


@pytest.mark.asyncio
async def test_delete_cancels_inflight_direct_link_preparation(monkeypatch):
    manager = GuardedTransferIntegrityManager()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    base_delete = AsyncMock(return_value=None)

    async def fake_prepare(self, torrent_id, links):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(
        TransferIntegrityManager,
        "_prepare_direct_link_collection",
        fake_prepare,
    )
    monkeypatch.setattr(
        TransferIntegrityManager,
        "delete_torrent",
        base_delete,
    )
    monkeypatch.setattr(
        manager,
        "_load_transfer_row",
        AsyncMock(
            return_value={
                "id": 13,
                "hash": "direct:test",
                "name": "Direct",
                "status": "processing",
                "source": "direct_link",
                "alldebrid_id": "",
            }
        ),
    )

    task = asyncio.create_task(
        manager._prepare_direct_link_collection(
            13, ["https://example.invalid/file"]
        )
    )
    await started.wait()

    await manager.delete_torrent(13, delete_from_ad=False)
    await asyncio.sleep(0)

    assert cancelled.is_set()
    assert task.cancelled()
    base_delete.assert_awaited_once_with(13, delete_from_ad=False)


@pytest.mark.asyncio
async def test_delete_serializes_with_aria2_dispatch(monkeypatch):
    manager = GuardedTransferIntegrityManager()
    base_delete = AsyncMock(return_value=None)

    monkeypatch.setattr(
        TransferIntegrityManager,
        "delete_torrent",
        base_delete,
    )
    monkeypatch.setattr(
        manager,
        "_load_transfer_row",
        AsyncMock(
            return_value={
                "id": 14,
                "hash": "e" * 40,
                "name": "Dispatching",
                "status": "queued",
                "source": "manual",
                "alldebrid_id": "",
            }
        ),
    )

    await manager._aria2_dispatch_lock.acquire()
    delete_task = asyncio.create_task(
        manager.delete_torrent(14, delete_from_ad=False)
    )
    await asyncio.sleep(0)
    assert base_delete.await_count == 0

    manager._aria2_dispatch_lock.release()
    await delete_task

    base_delete.assert_awaited_once_with(14, delete_from_ad=False)


@pytest.mark.asyncio
async def test_upload_failure_retry_delay_aborts_when_delete_wins(monkeypatch):
    manager = GuardedTransferIntegrityManager()
    fake_db = _FakeDb()
    provider = AsyncMock()
    old_provider_deleted = asyncio.Event()

    async def delete_old(ad_id):
        old_provider_deleted.set()
        return True

    provider.delete_magnet.side_effect = delete_old
    provider.upload_magnet.return_value = {
        "id": "new-provider-id",
        "hash": "f" * 40,
    }

    monkeypatch.setattr(
        runtime_guard,
        "get_db",
        _fake_get_db(fake_db),
    )
    monkeypatch.setattr(manager, "ad", lambda: provider)

    apply_settings(
        AppSettings(
            upload_fail_retry_count=3,
            upload_fail_retry_delay_minutes=1,
        )
    )

    row = {
        "id": 15,
        "name": "Retry",
        "alldebrid_id": "old-provider-id",
        "magnet": f"magnet:?xt=urn:btih:{'f' * 40}",
        "source": "manual",
    }

    task = asyncio.create_task(
        manager._handle_upload_failed(row, "provider upload failed")
    )
    await old_provider_deleted.wait()
    manager._begin_delete_intent(15)

    await asyncio.wait_for(task, timeout=0.5)

    assert provider.upload_magnet.await_count == 0


@pytest.mark.asyncio
async def test_expired_reimport_cleans_new_provider_object_if_delete_wins(
    monkeypatch,
):
    manager = GuardedTransferIntegrityManager()
    fake_db = _FakeDb()
    provider = AsyncMock()
    upload_started = asyncio.Event()
    release_upload = asyncio.Event()

    async def upload(_magnet):
        upload_started.set()
        await release_upload.wait()
        return {"id": "replacement-id", "hash": "1" * 40}

    provider.upload_magnet.side_effect = upload
    provider.delete_magnet.return_value = True

    monkeypatch.setattr(
        runtime_guard,
        "get_db",
        _fake_get_db(fake_db),
    )
    monkeypatch.setattr(manager, "ad", lambda: provider)

    row = {
        "id": 16,
        "name": "Expired",
        "hash": "1" * 40,
        "source": "manual",
    }

    task = asyncio.create_task(
        manager._handle_expired_reimport(
            row, f"magnet:?xt=urn:btih:{'1' * 40}"
        )
    )
    await upload_started.wait()

    manager._begin_delete_intent(16)
    release_upload.set()
    await task

    provider.delete_magnet.assert_awaited_once_with("replacement-id")
    assert not any(
        "SET alldebrid_id =" in sql for sql, _params in fake_db.writes
    )


@pytest.mark.asyncio
async def test_completed_magnet_resubmit_bypasses_historical_skip(monkeypatch):
    manager = GuardedTransferIntegrityManager()
    history = {
        "id": 42,
        "hash": "a" * 40,
        "name": "Example",
        "status": "completed",
        "source": "manual",
        "alldebrid_id": "old",
    }
    reacquire = AsyncMock(return_value={"id": 42, "status": "uploading"})

    monkeypatch.setattr(
        manager,
        "_deleted_transfer_by_hash",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        manager,
        "_completed_transfer_by_hash",
        AsyncMock(return_value=history),
    )
    monkeypatch.setattr(
        manager,
        "_reacquire_completed_magnet",
        reacquire,
    )

    magnet = f"magnet:?xt=urn:btih:{'a' * 40}"
    result = await manager.add_magnet_direct(magnet, source="manual")

    assert result["id"] == 42
    reacquire.assert_awaited_once_with(
        magnet,
        "a" * 40,
        "manual",
        history,
    )


@pytest.mark.asyncio
async def test_explicit_resubmit_clears_completed_delete_intent(monkeypatch):
    manager = GuardedTransferIntegrityManager()
    delegated = AsyncMock(return_value={"id": 21, "status": "uploading"})
    manager._begin_delete_intent(21)

    monkeypatch.setattr(
        manager,
        "_deleted_transfer_by_hash",
        AsyncMock(
            return_value={
                "id": 21,
                "hash": "2" * 40,
                "status": "deleted",
            }
        ),
    )
    monkeypatch.setattr(
        manager,
        "_completed_transfer_by_hash",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        TransferIntegrityManager,
        "add_magnet_direct",
        delegated,
    )

    magnet = f"magnet:?xt=urn:btih:{'2' * 40}"
    await manager.add_magnet_direct(magnet, source="manual")

    assert not manager._delete_requested(21)
    delegated.assert_awaited_once_with(magnet, source="manual")


@pytest.mark.asyncio
async def test_active_magnet_still_uses_existing_duplicate_gate(monkeypatch):
    manager = GuardedTransferIntegrityManager()
    delegated = AsyncMock(return_value={"id": 8, "_duplicate": {"action": "skip"}})

    monkeypatch.setattr(
        manager,
        "_deleted_transfer_by_hash",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        manager,
        "_completed_transfer_by_hash",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        TransferIntegrityManager,
        "add_magnet_direct",
        delegated,
    )

    magnet = f"magnet:?xt=urn:btih:{'b' * 40}"
    result = await manager.add_magnet_direct(magnet, source="manual")

    assert result["_duplicate"]["action"] == "skip"
    delegated.assert_awaited_once_with(magnet, source="manual")


@pytest.mark.asyncio
async def test_explicit_delete_honors_provider_deletion_for_completed_import(monkeypatch):
    manager = GuardedTransferIntegrityManager()
    provider = AsyncMock()
    provider.delete_magnet.return_value = True
    base_delete = AsyncMock(return_value=None)

    monkeypatch.setattr(
        manager,
        "_load_transfer_row",
        AsyncMock(
            return_value={
                "id": 11,
                "hash": "c" * 40,
                "name": "Imported",
                "status": "completed",
                "source": "alldebrid_existing",
                "alldebrid_id": "999",
            }
        ),
    )
    monkeypatch.setattr(manager, "ad", lambda: provider)
    monkeypatch.setattr(
        TransferIntegrityManager,
        "delete_torrent",
        base_delete,
    )

    await manager.delete_torrent(11, delete_from_ad=True)

    provider.delete_magnet.assert_awaited_once_with("999")
    base_delete.assert_awaited_once_with(11, delete_from_ad=False)


@pytest.mark.asyncio
async def test_explicit_delete_tolerates_already_cleaned_owned_completed_provider(monkeypatch):
    manager = GuardedTransferIntegrityManager()
    provider = AsyncMock()
    provider.delete_magnet.return_value = False
    base_delete = AsyncMock(return_value=None)

    monkeypatch.setattr(
        manager,
        "_load_transfer_row",
        AsyncMock(
            return_value={
                "id": 12,
                "hash": "d" * 40,
                "name": "Owned",
                "status": "completed",
                "source": "manual",
                "alldebrid_id": "1000",
            }
        ),
    )
    monkeypatch.setattr(manager, "ad", lambda: provider)
    monkeypatch.setattr(
        TransferIntegrityManager,
        "delete_torrent",
        base_delete,
    )

    await manager.delete_torrent(12, delete_from_ad=True)

    provider.delete_magnet.assert_awaited_once_with("1000")
    base_delete.assert_awaited_once_with(12, delete_from_ad=False)
