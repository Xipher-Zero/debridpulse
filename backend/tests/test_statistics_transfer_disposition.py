"""Statistics -> Transfer Disposition: settled transfers by their current status.

A hand-written set of transfers, one row each, at every current lifecycle
status; every expected count is written out by hand. The real
``/api/stats/detail`` route and a real temporary SQLite database.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from api.routes import get_stats_detail
from integrations.catalog import definitions
from transfers.models import TransferState

# (status, extraction_status, added days ago)
FIXTURE = [
    ("completed", "", 1), ("completed", "", 1), ("completed", "done", 2),
    # Completed but still extracting: in progress, not yet settled.
    ("completed", "extracting", 1),
    ("consolidated", "", 1), ("error", "", 2), ("error", "", 3), ("cancelled", "", 3),
    # Removed from Downloads: Remove persists `deleted`, the row is kept.
    ("deleted", "", 1), ("deleted", "", 2),
    # Legacy raw settled statuses keep their own rows.
    ("failed", "", 4), ("lost", "", 4),
    # Every active or waiting status: no disposition.
    ("pending", "", 1), ("processing", "", 1), ("input_required", "", 1), ("ready", "", 1), ("queued", "", 1),
    ("downloading", "", 1), ("paused", "", 1), ("verifying", "", 1), ("extracting", "", 1),
    # Outside the last 7 days.
    ("completed", "", 40), ("deleted", "", 40),
]


@pytest_asyncio.fixture
async def stats_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "disposition.sqlite3")
    await database.init_db()
    conn = sqlite3.connect(database.DB_PATH)
    try:
        for index, (status, extraction, days) in enumerate(FIXTURE, start=1):
            conn.execute("INSERT INTO torrents(id,hash,name,status,extraction_status,source,created_at) "
                         "VALUES(?,?,?,?,?,?,datetime('now', ?))",
                         (index, f"hash-{index}", f"Transfer {index}", status, extraction, "manual", f"-{days} days"))
        conn.commit()
    finally:
        conn.close()
    return database


async def _disposition(period: str) -> dict[str, int]:
    detail = await get_stats_detail(period=period, application=SimpleNamespace(definitions=definitions))
    rows = detail["torrent_status"]
    counts = [row["count"] for row in rows]
    assert counts == sorted(counts, reverse=True)
    assert len({row["status"] for row in rows}) == len(rows)
    return {row["status"]: row["count"] for row in rows}


def test_the_fixture_covers_every_current_lifecycle_status():
    assert {state.value for state in TransferState} <= {status for status, _, _ in FIXTURE}


@pytest.mark.asyncio
async def test_only_settled_transfers_count_each_once_at_its_current_status(stats_db):
    assert await _disposition("7d") == {
        "completed": 3, "consolidated": 1, "error": 2, "cancelled": 1, "deleted": 2, "failed": 1, "lost": 1}
    assert await _disposition("all") == {
        "completed": 4, "consolidated": 1, "error": 2, "cancelled": 1, "deleted": 3, "failed": 1, "lost": 1}


@pytest.mark.asyncio
async def test_a_removal_moves_its_transfer_rather_than_adding_one(stats_db):
    before = await _disposition("all")
    conn = sqlite3.connect(database.DB_PATH)
    try:
        # A completed transfer removed, and an active one removed.
        conn.execute("UPDATE torrents SET status='deleted' WHERE id=1")
        conn.execute("UPDATE torrents SET status='deleted' WHERE id=18")
        conn.commit()
    finally:
        conn.close()
    after = await _disposition("all")
    assert after["completed"] == before["completed"] - 1
    assert after["deleted"] == before["deleted"] + 2
    assert sum(after.values()) == sum(before.values()) + 1
