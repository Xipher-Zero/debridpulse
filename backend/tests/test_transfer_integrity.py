"""Regression coverage for filesystem/aria2 transfer integrity."""
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


def _cfg(tmp_path):
    return SimpleNamespace(
        download_folder=str(tmp_path),
        min_free_disk_gb=0,
        filters_enabled=False,
        discord_notify_finished=False,
        aria2_operation_timeout_seconds=15,
    )


def _manager(files):
    manager = TransferIntegrityManager()
    manager.download_client_name = lambda: "aria2"
    manager.is_paused = lambda: False
    manager._fetch_ready_files = AsyncMock(return_value=files)
    manager._send_partial_summary = AsyncMock()
    manager._delete_magnet_after_completion = AsyncMock()
    manager._mark_finished = AsyncMock()
    manager._log_event = AsyncMock()
    manager.advance_aria2_queue = AsyncMock()
    manager._notify_provider_error = AsyncMock()
    manager._remove_owned_aria2_gid = AsyncMock()
    return manager


async def _run_materializer(tmp_path, manager, db, *, name="Example"):
    @asynccontextmanager
    async def fake_get_db():
        yield db

    with patch("services.transfer_integrity.get_db", fake_get_db), patch(
        "services.transfer_integrity.get_settings", return_value=_cfg(tmp_path)
    ), patch(
        "services.transfer_integrity._EXISTING_PAYLOAD_STABILITY_SECONDS", 0
    ):
        await manager._engine_download(101, "ad-101", name)


def _parent_status_updates(db):
    return [
        params
        for sql, params in db.statements
        if isinstance(params, tuple)
        and "UPDATE torrents SET status=?, local_path=?" in sql
    ]


@pytest.mark.asyncio
async def test_exact_stable_existing_target_is_adopted_without_aria2(tmp_path):
    destination = tmp_path / "Example" / "File.bin"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"data")

    db = _FakeDb()
    manager = _manager([
        {
            "path": "Example/File.bin",
            "size": 4,
            "link": "https://provider.invalid/file",
        }
    ])

    await _run_materializer(tmp_path, manager, db)

    assert len(db.manifest_rows) == 1
    manifest = db.manifest_rows[0]
    assert manifest[5] == str(destination)
    assert manifest[6] == "completed"
    assert manifest[7] == "aria2"
    manager.advance_aria2_queue.assert_not_awaited()
    manager._delete_magnet_after_completion.assert_awaited_once()
    manager._mark_finished.assert_awaited_once()
    assert _parent_status_updates(db)[-1][0] == "completed"


@pytest.mark.asyncio
async def test_missing_target_is_pending_even_after_historical_completion(tmp_path):
    destination = tmp_path / "Example" / "File.bin"
    assert not destination.exists()

    db = _FakeDb()
    manager = _manager([
        {
            "path": "Example/File.bin",
            "size": 4,
            "link": "https://provider.invalid/file",
        }
    ])

    await _run_materializer(tmp_path, manager, db)

    manifest = db.manifest_rows[0]
    assert manifest[5] == str(destination)
    assert manifest[6] == "pending"
    manager._delete_magnet_after_completion.assert_not_awaited()
    manager._mark_finished.assert_not_awaited()
    manager.advance_aria2_queue.assert_awaited_once()
    assert _parent_status_updates(db)[-1][0] == "queued"


@pytest.mark.asyncio
async def test_wrong_size_existing_target_is_pending(tmp_path):
    destination = tmp_path / "Example" / "File.bin"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"bad")

    db = _FakeDb()
    manager = _manager([
        {
            "path": "Example/File.bin",
            "size": 4,
            "link": "https://provider.invalid/file",
        }
    ])

    await _run_materializer(tmp_path, manager, db)

    assert db.manifest_rows[0][6] == "pending"
    manager.advance_aria2_queue.assert_awaited_once()
    manager._delete_magnet_after_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_provider_size_is_not_adopted(tmp_path):
    destination = tmp_path / "Example" / "File.bin"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"data")

    db = _FakeDb()
    manager = _manager([
        {
            "path": "Example/File.bin",
            "size": 0,
            "link": "https://provider.invalid/file",
        }
    ])

    await _run_materializer(tmp_path, manager, db)

    assert db.manifest_rows[0][6] == "pending"
    manager.advance_aria2_queue.assert_awaited_once()


@pytest.mark.asyncio
async def test_aria2_sidecar_routes_existing_payload_through_aria2(tmp_path):
    destination = tmp_path / "Example" / "File.bin"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"data")
    Path(f"{destination}.aria2").write_bytes(b"resume-state")

    db = _FakeDb()
    manager = _manager([
        {
            "path": "Example/File.bin",
            "size": 4,
            "link": "https://provider.invalid/file",
        }
    ])

    await _run_materializer(tmp_path, manager, db)

    assert db.manifest_rows[0][6] == "pending"
    manager.advance_aria2_queue.assert_awaited_once()
    manager._delete_magnet_after_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_candidate_that_disappears_before_finalisation_is_requeued(tmp_path):
    destination = tmp_path / "Example" / "File.bin"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"data")

    db = _FakeDb()
    manager = _manager([
        {
            "path": "Example/File.bin",
            "size": 4,
            "link": "https://provider.invalid/file",
        }
    ])
    manager._local_payload_matches_manifest = patch.object(
        manager,
        "_local_payload_matches_manifest",
        side_effect=[True, False],
    ).start()
    try:
        await _run_materializer(tmp_path, manager, db)
    finally:
        patch.stopall()

    assert db.manifest_rows[0][6] == "completed"
    demotions = [
        params
        for sql, params in db.statements
        if "SET status='pending'" in sql and "source_url=?" in sql
    ]
    assert len(demotions) == 1
    manager.advance_aria2_queue.assert_awaited_once()
    manager._delete_magnet_after_completion.assert_not_awaited()
    manager._mark_finished.assert_not_awaited()
    assert _parent_status_updates(db)[-1][0] == "queued"


@pytest.mark.asyncio
async def test_mixed_existing_and_missing_manifest_downloads_only_missing_file(tmp_path):
    existing = tmp_path / "Example" / "Already.bin"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"done")

    db = _FakeDb()
    manager = _manager([
        {
            "path": "Example/Already.bin",
            "size": 4,
            "link": "https://provider.invalid/already",
        },
        {
            "path": "Example/Missing.bin",
            "size": 7,
            "link": "https://provider.invalid/missing",
        },
    ])

    await _run_materializer(tmp_path, manager, db)

    assert [row[6] for row in db.manifest_rows] == ["completed", "pending"]
    manager.advance_aria2_queue.assert_awaited_once()
    manager._delete_magnet_after_completion.assert_not_awaited()
    assert _parent_status_updates(db)[-1][0] == "queued"


@pytest.mark.asyncio
async def test_two_missing_files_in_otherwise_complete_manifest_stay_queued(tmp_path):
    root = tmp_path / "Example"
    root.mkdir(parents=True)
    for name, payload in (
        ("01.bin", b"1111"),
        ("02.bin", b"2222"),
        ("03.bin", b"3333"),
    ):
        (root / name).write_bytes(payload)

    files = [
        {
            "path": f"Example/{name}",
            "size": 4,
            "link": f"https://provider.invalid/{name}",
        }
        for name in ("01.bin", "02.bin", "03.bin", "Cover-A.jpg", "Cover-B.jpg")
    ]

    db = _FakeDb()
    manager = _manager(files)
    await _run_materializer(tmp_path, manager, db)

    statuses = [row[6] for row in db.manifest_rows]
    assert statuses == ["completed", "completed", "completed", "pending", "pending"]
    assert _parent_status_updates(db)[-1][0] == "queued"
    manager.advance_aria2_queue.assert_awaited_once()
    manager._delete_magnet_after_completion.assert_not_awaited()
    manager._mark_finished.assert_not_awaited()


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


def test_integrity_policy_requires_exact_stable_manifest_match():
    source = (
        Path(__file__).resolve().parents[1]
        / "services"
        / "transfer_integrity.py"
    ).read_text()
    assert "_local_payload_matches_manifest" in source
    assert "_directory_contains_name" in source
    assert "os.scandir" in source
    assert "os.open" in source
    assert "os.fstat" in source
    assert "os.pread" in source
    assert "await asyncio.sleep(_EXISTING_PAYLOAD_STABILITY_SECONDS)" in source
    assert "_EXISTING_PAYLOAD_STABILITY_SECONDS = 3.25" in source
    assert "accounted_count" in source
    assert "completed_at=NULL" in source
    assert "filesystem/aria2 delivery authority" in source
