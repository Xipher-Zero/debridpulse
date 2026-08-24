import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db.database as database
import services.direct_link_result_guard as result_guard
from services.direct_link_result_guard import DirectLinkResultGuardManager


async def _prepare_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "mirror-failover.db"
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


def _insert_failover_group(db_path: Path, *, reason="3: Resource not found", extra_standby=True):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO torrents
               (hash, name, status, source, download_client, provider_status,
                size_bytes, progress)
               VALUES ('direct:failover', 'archive.rar (3 links)', 'downloading',
                       'direct_link', 'aria2', 'ready', 1000, 61.0)"""
        )
        torrent_id = int(cur.lastrowid)
        primary = conn.execute(
            """INSERT INTO download_files
               (torrent_id, filename, size_bytes, source_url, download_url,
                local_path, status, download_id, download_client, blocked,
                block_reason, mirror_group_id, mirror_state)
               VALUES (?, 'archive.rar', 1000, 'https://one.example/a',
                       'https://capability.invalid/a', ?, 'error', 'gid-a',
                       'aria2', 0, ?, NULL, 'active')""",
            (torrent_id, str(Path(db_path).parent / "archive.rar"), reason),
        )
        primary_id = int(primary.lastrowid)
        conn.execute(
            "UPDATE download_files SET mirror_group_id=? WHERE id=?",
            (primary_id, primary_id),
        )
        standby_ids = []
        for host in (["two.example", "three.example"] if extra_standby else ["two.example"]):
            row = conn.execute(
                """INSERT INTO download_files
                   (torrent_id, filename, size_bytes, source_url, status,
                    download_client, blocked, block_reason,
                    mirror_group_id, mirror_state)
                   VALUES (?, 'archive.rar', 1000, ?, 'duplicate',
                           'aria2', NULL, 'validated standby', ?, 'standby')""",
                (torrent_id, f"https://{host}/a", primary_id),
            )
            standby_ids.append(int(row.lastrowid))
        conn.commit()
        return torrent_id, primary_id, standby_ids
    finally:
        conn.close()


def _disable_async_dispatch(manager):
    def discard(coro, *, label):
        coro.close()
        return None
    manager._track_maintenance_task = discard


@pytest.mark.asyncio
async def test_source_specific_failure_promotes_first_standby(tmp_path, monkeypatch):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    torrent_id, primary_id, standby_ids = _insert_failover_group(db_path)
    target = Path(db_path).parent / "archive.rar"
    target.write_bytes(b"partial")
    Path(f"{target}.aria2").write_bytes(b"control")

    manager = DirectLinkResultGuardManager()
    _disable_async_dispatch(manager)
    manager._broadcast_direct_link_update = AsyncMock()
    monkeypatch.setattr(result_guard, "is_builtin_mode", lambda: False)

    promoted = await manager._promote_direct_link_mirror_failover(torrent_id)

    assert promoted is True
    assert not target.exists()
    assert not Path(f"{target}.aria2").exists()

    primary = _read_one(
        db_path,
        "SELECT status, blocked, download_url, mirror_state FROM download_files WHERE id=?",
        (primary_id,),
    )
    assert primary == {
        "status": "error",
        "blocked": None,
        "download_url": None,
        "mirror_state": "exhausted",
    }
    promoted_row = _read_one(
        db_path,
        "SELECT status, blocked, local_path, download_id, mirror_state FROM download_files WHERE id=?",
        (standby_ids[0],),
    )
    assert promoted_row == {
        "status": "pending",
        "blocked": 0,
        "local_path": str(target),
        "download_id": None,
        "mirror_state": "active",
    }
    untouched = _read_one(
        db_path,
        "SELECT status, blocked, mirror_state FROM download_files WHERE id=?",
        (standby_ids[1],),
    )
    assert untouched == {
        "status": "duplicate",
        "blocked": None,
        "mirror_state": "standby",
    }
    parent = _read_one(
        db_path,
        "SELECT status, error_message FROM torrents WHERE id=?",
        (torrent_id,),
    )
    assert parent == {"status": "queued", "error_message": None}
    events = _read_all(
        db_path,
        "SELECT level, message FROM events WHERE torrent_id=? ORDER BY id",
        (torrent_id,),
    )
    assert any("Mirror source exhausted: one.example" in row["message"] for row in events)
    assert any("Mirror failover: promoted two.example standby" in row["message"] for row in events)
    manager._broadcast_direct_link_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_disk_failure_does_not_cycle_standbys(tmp_path, monkeypatch):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    torrent_id, primary_id, standby_ids = _insert_failover_group(
        db_path,
        reason="9: Not enough disk space available",
        extra_standby=False,
    )
    manager = DirectLinkResultGuardManager()
    _disable_async_dispatch(manager)
    monkeypatch.setattr(result_guard, "is_builtin_mode", lambda: False)

    promoted = await manager._promote_direct_link_mirror_failover(torrent_id)

    assert promoted is False
    primary = _read_one(
        db_path,
        "SELECT blocked, mirror_state FROM download_files WHERE id=?",
        (primary_id,),
    )
    standby = _read_one(
        db_path,
        "SELECT status, mirror_state FROM download_files WHERE id=?",
        (standby_ids[0],),
    )
    assert primary == {"blocked": 0, "mirror_state": "active"}
    assert standby == {"status": "duplicate", "mirror_state": "standby"}
    events = _read_all(
        db_path,
        "SELECT message FROM events WHERE torrent_id=?",
        (torrent_id,),
    )
    assert any("local/system failure" in row["message"] for row in events)


@pytest.mark.asyncio
async def test_source_unlock_failure_without_gid_is_failover_eligible():
    manager = DirectLinkResultGuardManager()
    eligible, reason = await manager._mirror_failure_is_failover_eligible(
        {
            "download_id": None,
            "block_reason": "source-unlock: LINK_DOWN",
            "download_url": None,
        }
    )
    assert eligible is True
    assert reason == "LINK_DOWN"


@pytest.mark.asyncio
async def test_local_aria2_dispatch_failure_without_gid_is_not_failover_eligible():
    manager = DirectLinkResultGuardManager()
    eligible, reason = await manager._mirror_failure_is_failover_eligible(
        {
            "download_id": None,
            "block_reason": "aria2-dispatch: Unable to queue aria2 download",
            "download_url": None,
        }
    )
    assert eligible is False
    assert reason == "Unable to queue aria2 download"


@pytest.mark.asyncio
async def test_success_after_failover_is_plain_done_and_unused_standby_is_retained(tmp_path, monkeypatch):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    torrent_id, primary_id, standby_ids = _insert_failover_group(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE download_files SET blocked=NULL, mirror_state='exhausted', download_url=NULL WHERE id=?",
            (primary_id,),
        )
        conn.execute(
            """UPDATE download_files
               SET status='completed', blocked=0, local_path=?, download_id='gid-b',
                   mirror_state='active'
               WHERE id=?""",
            (str(Path(db_path).parent / "archive.rar"), standby_ids[0]),
        )
        conn.commit()
    finally:
        conn.close()

    manager = DirectLinkResultGuardManager()
    _disable_async_dispatch(manager)
    manager._mark_finished = AsyncMock()
    monkeypatch.setattr(
        result_guard,
        "get_settings",
        lambda: SimpleNamespace(discord_notify_finished=False),
    )
    monkeypatch.setattr(result_guard, "is_builtin_mode", lambda: False)

    completed = await manager._complete_direct_link_result(torrent_id)

    assert completed is True
    parent = _read_one(
        db_path,
        "SELECT status, error_message, progress FROM torrents WHERE id=?",
        (torrent_id,),
    )
    assert parent == {"status": "completed", "error_message": None, "progress": 100.0}
    unused = _read_one(
        db_path,
        "SELECT status, mirror_state FROM download_files WHERE id=?",
        (standby_ids[1],),
    )
    assert unused == {"status": "duplicate", "mirror_state": "unused"}
    events = _read_all(
        db_path,
        "SELECT message FROM events WHERE torrent_id=? ORDER BY id",
        (torrent_id,),
    )
    assert any("mirror source(s) exhausted during failover" in row["message"] for row in events)
    assert any("unused standby mirror(s)" in row["message"] for row in events)


def test_failover_schema_and_dispatch_contract_are_persisted():
    root = Path(__file__).resolve().parents[2]
    db_source = (root / "backend/db/database.py").read_text()
    dispatch_source = (root / "backend/services/dispatch_coordinator.py").read_text()
    manager_source = (root / "backend/services/manager_v2.py").read_text()
    result_source = (root / "backend/services/direct_link_result_guard.py").read_text()

    assert '("mirror_group_id", "INTEGER")' in db_source
    assert '("mirror_state", "TEXT DEFAULT \'\'")' in db_source
    assert "mirror_state='standby'" in dispatch_source
    assert "alternates retained as automatic failover standbys" in dispatch_source
    assert 'reason=f"source-unlock: {error_text}"' in manager_source
    assert "Failover source no longer matches the validated mirror artifact" in manager_source
    assert "_promote_direct_link_mirror_failover" in result_source
    assert "mirror_state='exhausted'" in result_source
    assert "mirror_state='unused'" in result_source
