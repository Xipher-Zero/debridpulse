from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from core.config import AppSettings, apply_settings
from services.transfer_integrity import TransferIntegrityManager
from services.transfer_runtime_guard import (
    GuardedTransferIntegrityManager,
    reject_non_public_resolution,
)


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
async def test_active_magnet_still_uses_existing_duplicate_gate(monkeypatch):
    manager = GuardedTransferIntegrityManager()
    delegated = AsyncMock(return_value={"id": 8, "_duplicate": {"action": "skip"}})

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
