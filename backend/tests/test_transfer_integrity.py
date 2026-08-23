"""Regression coverage for aria2-authoritative transfer integrity."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.aria2 import Aria2DownloadStatus
from services.transfer_integrity import (
    TransferIntegrityAria2Service,
    TransferIntegrityManager,
)


class _Cursor:
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    async def fetchall(self):
        return list(self._rows)

    async def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDb:
    def __init__(self):
        self.statements = []
        self.manifest_rows = []

    async def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if "SELECT download_id FROM download_files" in sql:
            return _Cursor([])
        return _Cursor([])

    async def executemany(self, sql, rows):
        materialized = list(rows)
        self.statements.append((sql, materialized))
        if "INSERT INTO download_files" in sql:
            self.manifest_rows.extend(materialized)
        return _Cursor([])

    async def fetchone(self, sql, params=()):
        self.statements.append((sql, params))
        if "SELECT source FROM torrents" in sql:
            return {"source": "manual"}
        return None

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_existing_full_target_is_still_materialized_pending(tmp_path):
    """Filesystem presence alone must never certify a fresh provider delivery."""
    destination = tmp_path / "Example" / "File.bin"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"data")

    db = _FakeDb()

    @asynccontextmanager
    async def fake_get_db():
        yield db

    cfg = SimpleNamespace(
        download_folder=str(tmp_path),
        min_free_disk_gb=0,
        filters_enabled=False,
        discord_notify_finished=False,
        aria2_operation_timeout_seconds=15,
    )

    manager = TransferIntegrityManager()
    manager.download_client_name = lambda: "aria2"
    manager.is_paused = lambda: False
    manager._fetch_ready_files = AsyncMock(
        return_value=[
            {
                "path": "Example/File.bin",
                "size": 4,
                "link": "https://provider.invalid/file",
            }
        ]
    )
    manager._send_partial_summary = AsyncMock()
    manager._delete_magnet_after_completion = AsyncMock()
    manager._mark_finished = AsyncMock()
    manager._log_event = AsyncMock()
    manager.advance_aria2_queue = AsyncMock()
    manager._notify_provider_error = AsyncMock()
    manager._remove_owned_aria2_gid = AsyncMock()

    with patch("services.transfer_integrity.get_db", fake_get_db), patch(
        "services.transfer_integrity.get_settings", return_value=cfg
    ):
        await manager._engine_download(101, "ad-101", "Example")

    assert len(db.manifest_rows) == 1
    manifest = db.manifest_rows[0]
    assert manifest[5] == str(destination)
    assert manifest[6] == "pending"
    assert manifest[7] == "aria2"
    manager._delete_magnet_after_completion.assert_not_awaited()
    manager._mark_finished.assert_not_awaited()
    manager.advance_aria2_queue.assert_awaited_once()

    parent_updates = [
        params
        for sql, params in db.statements
        if isinstance(params, tuple)
        and "UPDATE torrents SET status=?, local_path=?" in sql
    ]
    assert parent_updates
    assert parent_updates[-1][0] == "queued"


@pytest.mark.asyncio
async def test_stopped_complete_result_does_not_satisfy_fresh_builtin_dispatch():
    service = TransferIntegrityAria2Service(
        "http://localhost:6800/jsonrpc", timeout_seconds=5
    )
    stopped = Aria2DownloadStatus(
        gid="old-complete",
        status="complete",
        total_length=4,
        completed_length=4,
        download_speed=0,
        files=[
            {
                "path": "/download/File.bin",
                "uris": [{"uri": "https://provider.invalid/file"}],
            }
        ],
    )
    service.get_all = AsyncMock(return_value=[stopped])
    service._call = AsyncMock(return_value="fresh-gid")

    with patch("services.transfer_integrity.is_builtin_mode", return_value=True), patch(
        "services.aria2._is_builtin_mode", return_value=True
    ):
        gid = await service.ensure_download(
            "https://provider.invalid/file",
            options={"dir": "/download", "out": "File.bin"},
        )

    assert gid == "fresh-gid"
    service._call.assert_awaited_once()
    assert service._call.await_args.args[0] == "aria2.addUri"


@pytest.mark.asyncio
async def test_live_paused_result_remains_reusable_for_builtin_resume():
    service = TransferIntegrityAria2Service(
        "http://localhost:6800/jsonrpc", timeout_seconds=5
    )
    paused = Aria2DownloadStatus(
        gid="resume-gid",
        status="paused",
        total_length=4,
        completed_length=2,
        download_speed=0,
        files=[
            {
                "path": "/download/File.bin",
                "uris": [{"uri": "https://provider.invalid/file"}],
            }
        ],
    )
    service.get_all = AsyncMock(return_value=[paused])
    service._call = AsyncMock()

    with patch("services.transfer_integrity.is_builtin_mode", return_value=True), patch(
        "services.aria2._is_builtin_mode", return_value=True
    ):
        gid = await service.ensure_download(
            "https://provider.invalid/file",
            options={"dir": "/download", "out": "File.bin"},
        )

    assert gid == "resume-gid"
    service._call.assert_not_awaited()


def test_runtime_service_root_uses_integrity_engine():
    root = (
        Path(__file__).resolve().parents[1]
        / "services"
        / "transfer_service.py"
    ).read_text()
    assert "from services.transfer_integrity import manager as engine" in root
    assert "from services.manager_v2 import manager as engine" not in root


def test_integrity_materializer_has_no_filesystem_completion_shortcut():
    source = (
        Path(__file__).resolve().parents[1]
        / "services"
        / "transfer_integrity.py"
    ).read_text()
    assert "local_path.exists()" not in source
    assert '"pending",\n                    "aria2"' in source
    assert "filesystem presence is not delivery proof" in source
