import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.database as database
import services.direct_link_result_guard as result_guard
from services.direct_link_result_guard import DirectLinkResultGuardManager


async def _prepare_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "direct-link-results.db"
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


async def _insert_transfer(db_path: Path, *, physical_status: str, physical_reason=None):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO torrents
               (hash, name, status, source, download_client, provider_status,
                size_bytes, progress, error_message)
               VALUES (?, ?, ?, 'direct_link', 'aria2', 'ready', ?, ?, ?)""",
            (
                "direct:test-result-authority",
                "GF200826-TMNTSFS-RN.rar (11 links)",
                "downloading",
                3_595_501_360,
                99.8,
                "8 of 11 links could not be generated",
            ),
        )
        torrent_id = int(cur.lastrowid)

        conn.execute(
            """INSERT INTO download_files
               (torrent_id, filename, size_bytes, source_url, download_url,
                local_path, status, download_id, download_client, blocked,
                block_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'aria2', 0, ?)""",
            (
                torrent_id,
                "GF200826-TMNTSFS-RN.rar",
                3_595_501_360,
                "https://1fichier.com/?primary",
                "https://download.invalid/primary",
                "/download/GF200826-TMNTSFS-RN.rar",
                physical_status,
                "physical-gid",
                physical_reason,
            ),
        )

        for host, size, variance in (
            ("rapidgator.net", 3_595_501_360, "0 bytes (0.0000%)"),
            ("megaup.net", 3_597_035_110, "1533750 bytes (0.0426%)"),
        ):
            conn.execute(
                """INSERT INTO download_files
                   (torrent_id, filename, size_bytes, source_url, status,
                    download_client, blocked, block_reason)
                   VALUES (?, ?, ?, ?, 'duplicate', 'aria2', NULL, ?)""",
                (
                    torrent_id,
                    "GF200826-TMNTSFS-RN.rar",
                    size,
                    f"https://{host}/mirror",
                    f"Duplicate mirror of 1fichier.com; size variance {variance}",
                ),
            )

        source_outcomes = [
            ("1024terabox.com", "error"),
            ("vikingfile.com", "error"),
            ("gofile.io", "missing"),
            ("pixeldrain.com", "error"),
            ("send.now", "missing"),
            ("rootz.so", "error"),
            ("ddownload.com", "missing"),
            ("datanodes.to", "error"),
        ]
        for index, (host, status) in enumerate(source_outcomes, start=1):
            conn.execute(
                """INSERT INTO download_files
                   (torrent_id, filename, size_bytes, source_url, status,
                    download_client, blocked, block_reason)
                   VALUES (?, ?, 0, ?, ?, 'aria2', 0, ?)""",
                (
                    torrent_id,
                    f"unavailable-{index}",
                    f"https://{host}/missing-{index}",
                    status,
                    "source unavailable before aria2 dispatch",
                ),
            )

        conn.commit()
        return torrent_id
    finally:
        conn.close()


def _disable_completion_side_effects(manager, monkeypatch):
    monkeypatch.setattr(
        result_guard,
        "get_settings",
        lambda: SimpleNamespace(discord_notify_finished=False),
    )
    monkeypatch.setattr(result_guard, "is_builtin_mode", lambda: False)
    manager._mark_finished = AsyncMock()

    def discard_maintenance(coro, *, label):
        coro.close()
        return None

    manager._track_maintenance_task = discard_maintenance


@pytest.mark.asyncio
async def test_completed_payload_with_failed_sources_is_success_with_warning(tmp_path, monkeypatch):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    torrent_id = await _insert_transfer(db_path, physical_status="completed")
    manager = DirectLinkResultGuardManager()
    _disable_completion_side_effects(manager, monkeypatch)

    await manager._finalize_aria2_torrent(torrent_id)

    parent = _read_one(
        db_path,
        "SELECT status, progress, size_bytes, error_message, completed_at FROM torrents WHERE id=?",
        (torrent_id,),
    )
    assert parent["status"] == "completed"
    assert parent["progress"] == 100.0
    assert parent["size_bytes"] == 3_595_501_360
    assert parent["error_message"] == "8 of 11 links could not be generated"
    assert parent["completed_at"] is not None

    rows = _read_all(
        db_path,
        "SELECT status, blocked, local_path FROM download_files WHERE torrent_id=? ORDER BY id",
        (torrent_id,),
    )
    assert sum(row["status"] == "completed" for row in rows) == 1
    assert sum(row["status"] == "duplicate" for row in rows) == 2
    source_rows = [
        row for row in rows
        if row["status"] in {"error", "missing"} and row["local_path"] is None
    ]
    assert len(source_rows) == 8
    assert all(row["blocked"] is None for row in source_rows)
    manager._mark_finished.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_physical_aria2_error_remains_failure(tmp_path, monkeypatch):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    torrent_id = await _insert_transfer(
        db_path,
        physical_status="error",
        physical_reason="3: Resource not found",
    )
    manager = DirectLinkResultGuardManager()
    _disable_completion_side_effects(manager, monkeypatch)

    # The inherited finalizer owns real physical failures. Disable notification
    # so the test observes only persistence semantics.
    monkeypatch.setattr(
        "services.manager_v2.get_settings",
        lambda: SimpleNamespace(
            discord_notify_error=False,
            discord_notify_finished=False,
        ),
    )

    await manager._finalize_aria2_torrent(torrent_id)

    parent = _read_one(
        db_path,
        "SELECT status, error_message FROM torrents WHERE id=?",
        (torrent_id,),
    )
    assert parent["status"] == "error"
    assert parent["error_message"] == "One or more aria2 transfers failed"

    physical = _read_one(
        db_path,
        "SELECT status, blocked, local_path FROM download_files WHERE torrent_id=? AND local_path IS NOT NULL",
        (torrent_id,),
    )
    assert physical["status"] == "error"
    assert physical["blocked"] == 0


@pytest.mark.asyncio
async def test_false_terminal_error_can_be_repaired_from_completed_physical_payload(tmp_path, monkeypatch):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    torrent_id = await _insert_transfer(db_path, physical_status="completed")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE torrents SET status='error', error_message=? WHERE id=?",
            ("One or more aria2 transfers failed", torrent_id),
        )
        conn.commit()
    finally:
        conn.close()

    manager = DirectLinkResultGuardManager()
    _disable_completion_side_effects(manager, monkeypatch)
    repaired = await manager._complete_direct_link_result(torrent_id)

    assert repaired is True
    parent = _read_one(
        db_path,
        "SELECT status, progress, size_bytes, error_message FROM torrents WHERE id=?",
        (torrent_id,),
    )
    assert parent == {
        "status": "completed",
        "progress": 100.0,
        "size_bytes": 3_595_501_360,
        "error_message": "8 of 11 links could not be generated",
    }
