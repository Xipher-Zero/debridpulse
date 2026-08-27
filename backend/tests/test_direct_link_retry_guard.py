from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.database as database
import services.direct_link_retry_guard as retry_guard
from services.direct_link_retry_guard import DirectLinkRetryGuardManager


async def _prepare_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "direct-link-retry.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()
    return db_path


def _read_one(db_path: Path, sql: str, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _read_all(db_path: Path, sql: str, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _settings(tmp_path: Path):
    return SimpleNamespace(
        paused=False,
        download_folder=str(tmp_path),
        aria2_waiting_window=100,
        aria2_stopped_window=100,
    )


def _configure_manager(manager, tmp_path: Path, monkeypatch, snapshot=()):
    settings = _settings(tmp_path)
    monkeypatch.setattr(retry_guard, "get_settings", lambda: settings)
    monkeypatch.setattr("services.manager_v2.get_settings", lambda: settings)
    monkeypatch.setattr(retry_guard, "_EXISTING_PAYLOAD_STABILITY_SECONDS", 0)
    manager._aria2_get_all = AsyncMock(return_value=list(snapshot))
    manager._aria2_confirm_gid = AsyncMock(return_value=None)
    manager._broadcast_direct_link_update = AsyncMock()
    manager.advance_aria2_queue = AsyncMock(return_value=0)
    manager._schedule_direct_link_collection = AsyncMock()


async def _insert_parent(
    db_path: Path,
    tmp_path: Path,
    file_specs,
    *,
    source_failure=False,
):
    links = [
        f"https://host.invalid/source-{index}"
        for index in range(1, len(file_specs) + 1)
    ]
    if source_failure:
        links.append("https://host.invalid/recover-me")
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO torrents
               (hash, magnet, name, status, source, download_client, provider_status,
                size_bytes, progress, error_message)
               VALUES (?, ?, ?, 'error', 'direct_link', 'aria2', 'ready', ?, 67.0, ?)""",
            (
                "direct:retry-guard",
                json.dumps(links),
                f"Retry Guard ({len(links)} links)",
                sum(spec[1] for spec in file_specs),
                "One or more aria2 transfers failed",
            ),
        )
        torrent_id = int(cur.lastrowid)
        for index, (name, size, status, gid) in enumerate(file_specs, start=1):
            local_path = tmp_path / name
            conn.execute(
                """INSERT INTO download_files
                   (torrent_id, filename, size_bytes, source_url, download_url,
                    local_path, status, download_id, download_client, blocked,
                    block_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'aria2', 0, ?)""",
                (
                    torrent_id,
                    name,
                    size,
                    links[index - 1],
                    f"https://cdn.invalid/{name}",
                    str(local_path),
                    status,
                    gid,
                    "29: temporary source failure" if status == "error" else None,
                ),
            )
        if source_failure:
            conn.execute(
                """INSERT INTO download_files
                   (torrent_id, filename, size_bytes, source_url, status,
                    download_client, blocked, block_reason)
                   VALUES (?, 'recover-me', 0, ?, 'error', 'aria2', 0, ?)""",
                (
                    torrent_id,
                    links[-1],
                    "source-unlock: temporarily unavailable",
                ),
            )
        conn.commit()
        return torrent_id
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_retry_preserves_complete_and_resumable_paths(tmp_path, monkeypatch):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    size = 16
    complete = tmp_path / "archive.part01.rar"
    partial = tmp_path / "archive.part02.rar"
    missing = tmp_path / "archive.part03.rar"
    complete.write_bytes(b"A" * size)
    partial.write_bytes(b"B" * size)  # aria2 can preallocate the final size
    Path(f"{partial}.aria2").write_bytes(b"resume-state")

    torrent_id = await _insert_parent(
        db_path,
        tmp_path,
        [
            (complete.name, size, "completed", "complete-gid"),
            (partial.name, size, "error", "partial-gid"),
            (missing.name, size, "error", "missing-gid"),
        ],
    )
    manager = DirectLinkRetryGuardManager()
    _configure_manager(manager, tmp_path, monkeypatch)

    result = await manager.retry_direct_link_collection(torrent_id)

    assert result["new_status"] == "queued"
    assert result["verified_complete"] == 1
    assert result["resumable"] == 1
    assert result["pending"] == 1
    rows = _read_all(
        db_path,
        "SELECT filename, local_path, status, download_id "
        "FROM download_files WHERE torrent_id=? ORDER BY id",
        (torrent_id,),
    )
    assert len(rows) == 3
    assert [row["local_path"] for row in rows] == [
        str(complete),
        str(partial),
        str(missing),
    ]
    assert not any(" (2)" in row["local_path"] for row in rows)
    assert rows[0]["status"] == "completed"
    assert rows[1]["status"] == "pending" and rows[1]["download_id"] is None
    assert rows[2]["status"] == "pending" and rows[2]["download_id"] is None

    parent = _read_one(
        db_path,
        "SELECT status, progress, size_bytes, error_message "
        "FROM torrents WHERE id=?",
        (torrent_id,),
    )
    assert parent == {
        "status": "queued",
        "progress": 33.3,
        "size_bytes": size * 3,
        "error_message": None,
    }
    manager.advance_aria2_queue.assert_awaited_once()
    manager._schedule_direct_link_collection.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_keeps_confirmed_live_aria2_job(tmp_path, monkeypatch):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    size = 16
    partial = tmp_path / "archive.part02.rar"
    partial.write_bytes(b"B" * size)
    Path(f"{partial}.aria2").write_bytes(b"resume-state")
    torrent_id = await _insert_parent(
        db_path,
        tmp_path,
        [(partial.name, size, "error", "live-gid")],
    )
    live = SimpleNamespace(gid="live-gid", status="active")
    manager = DirectLinkRetryGuardManager()
    _configure_manager(manager, tmp_path, monkeypatch, snapshot=[live])

    result = await manager.retry_direct_link_collection(torrent_id)

    row = _read_one(
        db_path,
        "SELECT local_path, status, download_id "
        "FROM download_files WHERE torrent_id=?",
        (torrent_id,),
    )
    assert row == {
        "local_path": str(partial),
        "status": "downloading",
        "download_id": "live-gid",
    }
    assert result["live"] == 1
    assert result["new_status"] == "downloading"
    manager._aria2_confirm_gid.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_confirms_snapshot_miss_before_clearing_gid(tmp_path, monkeypatch):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    size = 16
    partial = tmp_path / "archive.part02.rar"
    partial.write_bytes(b"B" * size)
    Path(f"{partial}.aria2").write_bytes(b"resume-state")
    torrent_id = await _insert_parent(
        db_path,
        tmp_path,
        [(partial.name, size, "error", "uncertain-gid")],
    )
    manager = DirectLinkRetryGuardManager()
    _configure_manager(manager, tmp_path, monkeypatch)
    manager._aria2_confirm_gid = AsyncMock(
        side_effect=RuntimeError("aria2 RPC unavailable")
    )

    with pytest.raises(RuntimeError, match="aria2 RPC unavailable"):
        await manager.retry_direct_link_collection(torrent_id)

    row = _read_one(
        db_path,
        "SELECT local_path, status, download_id "
        "FROM download_files WHERE torrent_id=?",
        (torrent_id,),
    )
    assert row == {
        "local_path": str(partial),
        "status": "error",
        "download_id": "uncertain-gid",
    }
    parent = _read_one(
        db_path,
        "SELECT status FROM torrents WHERE id=?",
        (torrent_id,),
    )
    assert parent["status"] == "error"


@pytest.mark.asyncio
async def test_retry_can_recover_source_outcome_without_rebuilding_siblings(
    tmp_path,
    monkeypatch,
):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    size = 16
    complete = tmp_path / "archive.part01.rar"
    complete.write_bytes(b"A" * size)
    torrent_id = await _insert_parent(
        db_path,
        tmp_path,
        [(complete.name, size, "completed", None)],
        source_failure=True,
    )
    manager = DirectLinkRetryGuardManager()
    _configure_manager(manager, tmp_path, monkeypatch)
    provider = SimpleNamespace(
        unlock_link=AsyncMock(
            return_value={
                "link": "https://cdn.invalid/archive.part02.rar",
                "filename": "archive.part02.rar",
                "filesize": size,
            }
        )
    )
    manager.ad = lambda: provider

    result = await manager.retry_direct_link_collection(torrent_id)

    rows = _read_all(
        db_path,
        "SELECT filename, local_path, status, blocked "
        "FROM download_files WHERE torrent_id=? ORDER BY id",
        (torrent_id,),
    )
    assert rows[0]["local_path"] == str(complete)
    assert rows[0]["status"] == "completed"
    assert rows[1]["filename"] == "archive.part02.rar"
    assert rows[1]["local_path"] == str(tmp_path / "archive.part02.rar")
    assert rows[1]["status"] == "pending"
    assert rows[1]["blocked"] == 0
    assert result["recovered_sources"] == 1
    manager._schedule_direct_link_collection.assert_not_awaited()


def test_transfer_service_uses_retry_guard_engine():
    source = (
        Path(__file__).resolve().parents[1]
        / "services"
        / "transfer_service.py"
    ).read_text()
    assert "from services.direct_link_retry_guard import manager as engine" in source
