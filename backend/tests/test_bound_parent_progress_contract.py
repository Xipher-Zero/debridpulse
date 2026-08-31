import sqlite3
from pathlib import Path

import pytest

import db.database as database
from services.direct_link_result_guard import DirectLinkResultGuardManager
from services.transfer_service import TransferService


async def _prepare_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "bound-parent-progress.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()
    return db_path


def _read_parent(db_path: Path, transfer_id: int):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status, progress FROM torrents WHERE id=?",
            (int(transfer_id),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _insert_parent(db_path: Path, *, suffix: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO torrents
               (hash, name, status, source, download_client, provider_status,
                size_bytes, progress)
               VALUES (?, ?, 'queued', 'direct_link', 'aria2', 'ready', 100, 0)""",
            (f"direct:bound-progress-{suffix}", f"bound-progress-{suffix}"),
        )
        transfer_id = int(cur.lastrowid)
        conn.commit()
        return transfer_id
    finally:
        conn.close()


def _insert_file(
    db_path: Path,
    transfer_id: int,
    *,
    filename: str,
    status: str,
    size_bytes: int,
    local_path=None,
    download_id=None,
):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO download_files
               (torrent_id, filename, size_bytes, source_url, download_url,
                local_path, status, download_id, download_client, blocked)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'aria2', 0)""",
            (
                int(transfer_id),
                filename,
                int(size_bytes),
                f"https://source.invalid/{filename}",
                f"https://download.invalid/{filename}" if download_id else None,
                local_path,
                status,
                download_id,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_bound_architecture_excludes_predispatch_source_errors_from_parent_progress(
    tmp_path, monkeypatch
):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    transfer_id = _insert_parent(db_path, suffix="source-outcomes")
    _insert_file(
        db_path,
        transfer_id,
        filename="payload.rar",
        status="completed",
        size_bytes=100,
        local_path="/download/payload.rar",
        download_id="physical-complete-gid",
    )
    for index in range(8):
        _insert_file(
            db_path,
            transfer_id,
            filename=f"unavailable-{index}",
            status="error",
            size_bytes=0,
        )

    engine = DirectLinkResultGuardManager()
    TransferService(engine)

    # Exercise the public manager hook after architecture binding.  This routes
    # through TransferControlService -> TransferStateMachine -> TransferRepository,
    # which is the live scheduler path rather than the standalone guard finalizer.
    await engine._update_aria2_parent_progress([])

    parent = _read_parent(db_path, transfer_id)
    assert parent == {"status": "queued", "progress": 100.0}

    rows = await engine._architecture.repository.parent_progress_rows()
    assert len(rows) == 1
    assert rows[0]["file_status"] == "completed"


@pytest.mark.asyncio
async def test_bound_architecture_keeps_real_physical_error_in_parent_progress(
    tmp_path, monkeypatch
):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    transfer_id = _insert_parent(db_path, suffix="physical-error")
    _insert_file(
        db_path,
        transfer_id,
        filename="good.bin",
        status="completed",
        size_bytes=100,
        local_path="/download/good.bin",
        download_id="good-gid",
    )
    physical_error_id = _insert_file(
        db_path,
        transfer_id,
        filename="failed.bin",
        status="error",
        size_bytes=100,
        local_path="/download/failed.bin",
        download_id="failed-gid",
    )

    engine = DirectLinkResultGuardManager()
    TransferService(engine)
    await engine._update_aria2_parent_progress([])

    parent = _read_parent(db_path, transfer_id)
    assert parent == {"status": "queued", "progress": 50.0}

    rows = await engine._architecture.repository.parent_progress_rows()
    assert {int(row["file_id"]) for row in rows} == {
        physical_error_id - 1,
        physical_error_id,
    }
    assert any(
        int(row["file_id"]) == physical_error_id and row["file_status"] == "error"
        for row in rows
    )
