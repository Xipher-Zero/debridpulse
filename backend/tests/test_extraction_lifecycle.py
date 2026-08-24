from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile

import pytest

import db.database as database
import services.extraction_service as extraction_service
from services.extraction_service import ExtractionService
from services.extractor import Extractor


async def _prepare_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "extraction.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()
    return db_path


def _settings(**overrides):
    values = {
        "extract_enabled": True,
        "extract_delete_archive": True,
        "extract_max_concurrent": 1,
        "discord_notify_extract": False,
        "discord_webhook_url": "",
        "discord_webhook_added": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _insert_completed(db_path: Path, local_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO torrents
               (hash, name, status, source, download_client, progress, completed_at)
               VALUES (?, ?, 'completed', 'direct_link', 'aria2', 100, CURRENT_TIMESTAMP)""",
            ("extract:" + local_path.name, local_path.name),
        )
        torrent_id = int(cur.lastrowid)
        conn.execute(
            """INSERT INTO download_files
               (torrent_id, filename, local_path, status, download_client, blocked)
               VALUES (?, ?, ?, 'completed', 'aria2', 0)""",
            (torrent_id, local_path.name, str(local_path)),
        )
        conn.commit()
        return torrent_id
    finally:
        conn.close()


def _parent(db_path: Path, torrent_id: int):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute(
            "SELECT extraction_status, extraction_error, status FROM torrents WHERE id=?",
            (torrent_id,),
        ).fetchone())
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_completed_zip_runs_through_post_download_extraction(tmp_path, monkeypatch):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    archive = tmp_path / "payload.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("payload.txt", b"payload")
    torrent_id = _insert_completed(db_path, archive)
    monkeypatch.setattr(extraction_service, "get_settings", lambda: _settings())
    monkeypatch.setattr(extraction_service, "publish", AsyncMock())

    result = await ExtractionService().extract_completed_transfer(torrent_id)

    assert result["attempted"] is True
    assert result["status"] == "completed"
    assert not archive.exists()
    assert (tmp_path / "payload.txt").read_bytes() == b"payload"
    assert _parent(db_path, torrent_id) == {
        "extraction_status": "completed",
        "extraction_error": None,
        "status": "completed",
    }
    published_states = [
        call.args[1].get("extraction_status")
        for call in extraction_service.publish.await_args_list
        if call.args and call.args[0] == "torrent_updated"
    ]
    assert published_states == ["extracting", "completed"]


@pytest.mark.asyncio
async def test_inaccessible_rar_is_visible_failure_not_silent_skip(tmp_path, monkeypatch):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    archive = tmp_path / "missing.rar"
    torrent_id = _insert_completed(db_path, archive)
    monkeypatch.setattr(extraction_service, "get_settings", lambda: _settings())
    monkeypatch.setattr(extraction_service, "publish", AsyncMock())

    result = await ExtractionService().extract_completed_transfer(torrent_id)

    assert result["attempted"] is True
    assert result["status"] == "error"
    parent = _parent(db_path, torrent_id)
    assert parent["status"] == "completed"
    assert parent["extraction_status"] == "error"
    assert "not accessible to DebridPulse" in parent["extraction_error"]


@pytest.mark.asyncio
async def test_extract_archives_reports_missing_archive(tmp_path):
    archive = tmp_path / "missing.rar"
    results = await Extractor(max_concurrent=1).extract_archives([archive])
    assert len(results) == 1
    path, ok, message = results[0]
    assert path == archive
    assert ok is False
    assert "not accessible to DebridPulse" in message


def test_extraction_state_is_persisted_and_operator_visible():
    root = Path(__file__).resolve().parents[2]
    database_source = (root / "backend/db/database.py").read_text()
    routes_source = (root / "backend/api/routes.py").read_text()
    app_source = (root / "frontend/static/app.js").read_text()
    assert "extraction_status" in database_source
    assert '("extraction_error", "TEXT")' in database_source
    assert "active_operations" in routes_source
    assert "extracting_count" in routes_source
    assert "extracting:'📦 Extracting'" in app_source
    assert "t.extraction_status" in app_source


@pytest.mark.asyncio
async def test_extraction_events_form_durable_operator_audit(tmp_path, monkeypatch):
    db_path = await _prepare_db(tmp_path, monkeypatch)
    archive = tmp_path / "payload.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("payload.txt", b"payload")
    torrent_id = _insert_completed(db_path, archive)
    monkeypatch.setattr(extraction_service, "get_settings", lambda: _settings())
    monkeypatch.setattr(extraction_service, "publish", AsyncMock())

    await ExtractionService().extract_completed_transfer(torrent_id)

    conn = sqlite3.connect(db_path)
    try:
        events = [
            row[0]
            for row in conn.execute(
                "SELECT message FROM events WHERE torrent_id=? ORDER BY id",
                (torrent_id,),
            ).fetchall()
        ]
    finally:
        conn.close()

    assert "Auto-extract: Attempted · 1 archive(s) detected" in events
    assert "Extraction status: Extracting" in events
    assert "Extraction status: Completed · 1/1 archive(s) extracted" in events


def test_external_extraction_stages_inside_destination(tmp_path, monkeypatch):
    from services.extraction_safety import staged_external_extract

    archive = tmp_path / "payload.rar"
    archive.write_bytes(b"archive")
    dest = tmp_path / "download"
    dest.mkdir()
    observed = {}

    def runner(stage):
        observed["stage"] = Path(stage)
        (Path(stage) / "payload.txt").write_text("ok")

    monkeypatch.setattr(
        "services.extraction_safety.validate_extracted_tree",
        lambda stage, archive: None,
    )
    staged_external_extract(archive, dest, runner)

    assert observed["stage"].parent == dest.resolve()
    assert (dest / "payload.txt").read_text() == "ok"


def test_frontend_surfaces_extraction_failure_toast():
    root = Path(__file__).resolve().parents[2]
    app_source = (root / "frontend/static/app.js").read_text()
    assert "patchExtractionTransferEvent" in app_source
    assert "Extraction failed:" in app_source
    assert "extraction_error" in app_source
