"""Regression coverage for populated legacy Downloads read scaling."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from api import operational_downloads
import db.database as database
from db.migrations import v112
from transfers.presentation_repository import TransferRepository as PresentationRepository


FIXTURE = Path(__file__).with_name("fixtures") / "v1.0.11.1.sql"


@pytest.mark.asyncio
async def test_operational_list_releases_query_session_and_bounds_projection(monkeypatch):
    query_session_active = False
    in_flight = 0
    max_in_flight = 0
    row_count = 12

    class FakeDb:
        async def fetchall(self, sql, params):
            assert "SELECT t.id" in sql
            assert "file_count" not in sql
            assert "blocked_count" not in sql
            return [{"id": transfer_id} for transfer_id in range(1, row_count + 1)]

        async def fetchone(self, sql, params):
            assert "COUNT(*) AS cnt" in sql
            return {"cnt": row_count}

    @asynccontextmanager
    async def fake_get_db():
        nonlocal query_session_active
        assert not query_session_active
        query_session_active = True
        try:
            yield FakeDb()
        finally:
            query_session_active = False

    class FakeRepository:
        async def presentation(self, transfer_id):
            nonlocal in_flight, max_in_flight
            assert not query_session_active, "list DB session leaked into canonical presentation"
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.01)
                return {"id": transfer_id}
            finally:
                in_flight -= 1

    monkeypatch.setattr(operational_downloads, "get_db", fake_get_db)
    monkeypatch.setattr(
        operational_downloads,
        "_public_transfer_presentation",
        lambda presentation, definitions: presentation,
    )
    application = SimpleNamespace(repository=FakeRepository(), definitions={})

    result = await operational_downloads.list_operational_torrents(
        status=None,
        search=None,
        limit=row_count,
        offset=0,
        application=application,
    )

    assert result["total"] == row_count
    assert [item["id"] for item in result["items"]] == list(range(1, row_count + 1))
    assert 2 <= max_in_flight <= operational_downloads._PRESENTATION_CONCURRENCY


@pytest.mark.asyncio
async def test_migrated_multifile_presentation_has_constant_sqlite_acquires(tmp_path, monkeypatch):
    """A populated v1.0.11 predecessor must not open one DB session per file."""
    path = tmp_path / "legacy-scale.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(FIXTURE.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO torrents(id,hash,name,status,source,progress) VALUES(?,?,?,?,?,?)",
            (900, "9" * 40, "legacy-scale", "queued", "direct_link", 0),
        )
        conn.executemany(
            "INSERT INTO download_files(id,torrent_id,filename,size_bytes,status) VALUES(?,?,?,?,?)",
            [
                (1000 + index, 900, f"legacy-{index:02d}.bin", 1024, "queued")
                for index in range(40)
            ],
        )
        conn.commit()

    monkeypatch.setattr(database, "DB_PATH", path)
    report = await v112.migrate(external_executor=False)
    assert report["migrated"] is True

    before = database.db_runtime_metrics()["sqlite_acquires"]
    result = await PresentationRepository().presentation(900, details=False)
    after = database.db_runtime_metrics()["sqlite_acquires"]

    assert result and result["id"] == 900
    assert result["name"] == "legacy-scale"
    # Base canonical presentation plus the browser projection should stay
    # constant regardless of the 40 migrated artifacts. The old read path
    # acquired an additional SQLite connection for every artifact.
    assert after - before <= 3
