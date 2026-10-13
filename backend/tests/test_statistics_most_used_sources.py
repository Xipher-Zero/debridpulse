"""Statistics -> Most Used Sources: an upload is named by what it contained.

A hand-written set of transfers with their durable root (and child) requests;
every expected source is written out by hand from those facts. The real
``/api/stats/detail`` route and a real temporary SQLite database.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from api.routes import get_stats_detail
from integrations.catalog import definitions

# id: (channel, status, added days ago, [root request kinds], [child request kinds], expected source)
FIXTURE = {
    1: ("manual_file", "completed", 1, ["nzb"], [], "nzb_file"),
    2: ("manual_file", "error", 1, ["nzb"], ["https"], "nzb_file"),
    3: ("manual_file", "completed", 1, ["torrent"], ["https", "https"], "torrent_file"),
    # "file" is a stored legacy spelling of a torrent upload.
    4: ("manual_file", "deleted", 2, ["file"], [], "torrent_file"),
    5: ("manual_file", "completed", 2, ["meta4"], [], "metalink_file"),
    # A pre-NZB history row the v1.0.12 migration rebuilt as a magnet: what
    # was uploaded is not recorded, so it stays the channel.
    6: ("manual_file", "completed", 3, ["magnet"], [], "manual_file"),
    # No root request at all, and two different kinds: neither is named.
    7: ("manual_file", "completed", 3, [], [], "manual_file"),
    8: ("manual_file", "completed", 3, ["nzb", "torrent"], [], "manual_file"),
    # An empty kind beside a recorded one does not hide it.
    9: ("manual_file", "consolidated", 4, ["nzb", ""], [], "nzb_file"),
    # Other channels keep their identity whatever they carried.
    10: ("manual", "completed", 1, ["magnet"], [], "manual"),
    11: ("direct_link", "completed", 1, ["nzb"], [], "direct_link"),
    12: ("direct_link", "completed", 1, ["https"], [], "direct_link"),
    13: ("inventory", "completed", 2, ["magnet"], [], "inventory"),
    14: ("api", "completed", 2, [], [], "api"),
    # Outside the last 7 days.
    15: ("manual_file", "completed", 40, ["nzb"], [], "nzb_file"),
}


def _seed() -> None:
    conn = sqlite3.connect(database.DB_PATH)
    try:
        for transfer_id, (channel, status, days, roots, children, _expected) in FIXTURE.items():
            conn.execute("INSERT INTO torrents(id,hash,name,status,source,created_at) "
                         "VALUES(?,?,?,?,?,datetime('now', ?))",
                         (transfer_id, f"hash-{transfer_id}", f"Transfer {transfer_id}", status, channel,
                          f"-{days} days"))
            for ordinal, kind in enumerate(roots):
                conn.execute("INSERT INTO transfer_requests(id,transfer_id,parent_id,ordinal,payload) VALUES(?,?,?,?,?)",
                             (f"root-{transfer_id}-{ordinal}", transfer_id, None, ordinal, json.dumps({"kind": kind})))
            for ordinal, kind in enumerate(children):
                conn.execute("INSERT INTO transfer_requests(id,transfer_id,parent_id,ordinal,payload) VALUES(?,?,?,?,?)",
                             (f"child-{transfer_id}-{ordinal}", transfer_id, f"root-{transfer_id}-0", ordinal,
                              json.dumps({"kind": kind})))
        conn.commit()
    finally:
        conn.close()


@pytest_asyncio.fixture
async def stats_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "sources.sqlite3")
    await database.init_db()
    _seed()
    return database


async def _sources(period: str) -> list[dict]:
    return (await get_stats_detail(period=period, application=SimpleNamespace(definitions=definitions)))["sources"]


def _expected(max_days: int | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _channel, _status, days, _roots, _children, expected in FIXTURE.values():
        if max_days is None or days < max_days:
            counts[expected] = counts.get(expected, 0) + 1
    return counts


@pytest.mark.asyncio
@pytest.mark.parametrize("period, max_days", [("7d", 7), ("all", None)])
async def test_each_transfer_counts_once_under_its_own_source(stats_db, period, max_days):
    rows = await _sources(period)
    assert {row["source"]: row["count"] for row in rows} == _expected(max_days)
    # Ranked by count, most used first.
    counts = [row["count"] for row in rows]
    assert counts == sorted(counts, reverse=True)


@pytest.mark.asyncio
async def test_the_split_conserves_every_channel_total(stats_db):
    rows = {row["source"]: row["count"] for row in await _sources("all")}
    conn = sqlite3.connect(database.DB_PATH)
    try:
        channels = dict(conn.execute("SELECT source, COUNT(*) FROM torrents GROUP BY source").fetchall())
    finally:
        conn.close()
    uploads = sum(rows.get(key, 0) for key in ("manual_file", "nzb_file", "torrent_file", "metalink_file"))
    assert uploads == channels["manual_file"]
    assert {key: rows[key] for key in ("manual", "direct_link", "inventory", "api")} == {
        key: channels[key] for key in ("manual", "direct_link", "inventory", "api")}
    assert sum(rows.values()) == sum(channels.values())


@pytest.mark.asyncio
async def test_the_top_ten_cap_is_kept(stats_db):
    conn = sqlite3.connect(database.DB_PATH)
    try:
        for index in range(12):
            conn.execute("INSERT INTO torrents(hash,name,status,source) VALUES(?,?,?,?)",
                         (f"extra-{index}", f"Extra {index}", "completed", f"channel_{index}"))
        conn.commit()
    finally:
        conn.close()
    assert len(await _sources("all")) == 10
