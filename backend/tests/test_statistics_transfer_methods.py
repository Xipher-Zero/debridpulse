"""Statistics -> Transfer Methods: how material was actually acquired.

A hand-written set of transfers, what was submitted for each, and their
durable delivery provenance; every expected count is written out by hand
from those facts. The real ``/api/stats/detail`` route, the real catalog
metadata and a real temporary SQLite database.
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

# id: (submitted as, status, added days ago, completed?,
#      [(artifact role, [(ordinal, provider, delivered), ...]), ...], expected methods)
# An artifact role is "canonical" or "standby" (a mirror kept out of the transfer's material).
FIXTURE = {
    # Single methods, whatever was submitted.
    1: ("magnet", "completed", 1, True, [("canonical", [(1, "realdebrid", 1)])], {"Debrid"}),
    2: ("https", "completed", 1, True, [("canonical", [(1, "general_http", 1)])], {"HTTP(S)"}),
    3: ("https", "completed", 1, True, [("canonical", [(1, "media", 1)])], {"Media Downloads"}),
    4: ("https", "completed", 1, True, [("canonical", [(1, "media", 1)])], {"Media Downloads"}),
    5: ("ftp", "completed", 1, True, [("canonical", [(1, "general_ftp", 1)])], {"FTP / SFTP"}),
    6: ("sftp", "completed", 1, True, [("canonical", [(1, "general_ftp", 1)])], {"FTP / SFTP"}),
    7: ("scp", "completed", 1, True, [("canonical", [(1, "general_scp", 1)])], {"SCP"}),
    8: ("rsync", "completed", 1, True, [("canonical", [(1, "general_rsync", 1)])], {"rsync"}),
    9: ("https", "completed", 1, True, [("canonical", [(1, "general_webdav", 1)])], {"WebDAV"}),
    # An NZB: natively over NNTP is Usenet; through TorBox it is TorBox-delivered, so Debrid.
    10: ("nzb", "completed", 2, True, [("canonical", [(1, "usenet", 1)])], {"Usenet"}),
    11: ("nzb", "completed", 2, True, [("canonical", [(1, "torbox", 1)])], {"Debrid"}),
    # A Metalink names sources; what delivered its files is the method.
    12: ("meta4", "completed", 2, True, [("canonical", [(1, "general_http", 1)]),
                                         ("canonical", [(1, "general_ftp", 1)])], {"HTTP(S)", "FTP / SFTP"}),
    # Three debrid services: one Debrid. With HTTP(S) material too: also HTTP(S).
    13: ("magnet", "completed", 2, True, [("canonical", [(1, "alldebrid", 1)]), ("canonical", [(1, "realdebrid", 1)]),
                                          ("canonical", [(1, "premiumize", 1)])], {"Debrid"}),
    14: ("magnet", "completed", 2, True, [("canonical", [(1, "debridlink", 1)]),
                                          ("canonical", [(1, "general_http", 1)])], {"Debrid", "HTTP(S)"}),
    # Retries and failed routes count nothing: only the delivering route.
    15: ("https", "completed", 3, True, [("canonical", [(1, "alldebrid", 0), (2, "realdebrid", 0),
                                                        (3, "general_http", 1)])], {"HTTP(S)"}),
    # Delivered, then reacquired elsewhere: the latest delivery is the material's.
    16: ("magnet", "completed", 3, True, [("canonical", [(1, "realdebrid", 1), (2, "general_http", 1)])], {"HTTP(S)"}),
    # A standby mirror is not the transfer's material.
    17: ("magnet", "completed", 3, True, [("canonical", [(1, "realdebrid", 1)]),
                                          ("standby", [(1, "general_http", 1)])], {"Debrid"}),
    # Part delivered before it failed: that method did deliver material.
    18: ("magnet", "error", 3, False, [("canonical", [(1, "realdebrid", 1)]), ("canonical", [(1, "realdebrid", 0)])],
         {"Debrid"}),
    # Nothing delivered: no method, and not a completion, so not Unattributed either.
    19: ("https", "error", 3, False, [("canonical", [(1, "general_http", 0)])], set()),
    # Completed with no attributable delivery: history from before provenance,
    # an executor id, Multimeta (which delivers nothing itself), no provider.
    20: ("magnet", "completed", 4, True, [("canonical", [])], {"Unattributed"}),
    21: ("https", "completed", 4, True, [("canonical", [(1, "aria2", 1)])], {"Unattributed"}),
    22: ("meta4", "completed", 4, True, [("canonical", [(1, "multimeta", 1)])], {"Unattributed"}),
    23: ("https", "deleted", 4, True, [("canonical", [(1, None, 1)])], {"Unattributed"}),
    24: ("magnet", "completed", 4, True, [], {"Unattributed"}),
    # Outside the last 7 days.
    25: ("nzb", "completed", 40, True, [("canonical", [(1, "usenet", 1)])], {"Usenet"}),
    26: ("magnet", "completed", 40, True, [], {"Unattributed"}),
}


def _seed() -> None:
    conn = sqlite3.connect(database.DB_PATH)
    try:
        for transfer_id, (kind, status, days, completed, artifacts, _methods) in FIXTURE.items():
            conn.execute("INSERT INTO torrents(id,hash,name,status,source,created_at,completed_at) "
                         "VALUES(?,?,?,?,?,datetime('now', ?),?)",
                         (transfer_id, f"hash-{transfer_id}", f"Transfer {transfer_id}", status,
                          "manual_file" if kind in ("nzb", "meta4") else "direct_link", f"-{days} days",
                          "2026-01-01 00:00:00" if completed else None))
            conn.execute("INSERT INTO transfer_requests(id,transfer_id,parent_id,ordinal,payload) VALUES(?,?,?,?,?)",
                         (f"root-{transfer_id}", transfer_id, None, 0, json.dumps({"kind": kind})))
            for index, (role, executions) in enumerate(artifacts):
                request_id = f"req-{transfer_id}-{index}"
                conn.execute("INSERT INTO transfer_requests(id,transfer_id,parent_id,ordinal,payload) VALUES(?,?,?,?,?)",
                             (request_id, transfer_id, f"root-{transfer_id}", index, "{}"))
                artifact = conn.execute(
                    "INSERT INTO download_files(torrent_id,filename,request_id,status,mirror_state) VALUES(?,?,?,?,?)",
                    (transfer_id, f"file-{transfer_id}-{index}", request_id, "completed",
                     "standby" if role == "standby" else "")).lastrowid
                for ordinal, provider, delivered in executions:
                    execution = f"exec-{transfer_id}-{index}-{ordinal}"
                    conn.execute("INSERT INTO execution_attempts(id,transfer_id,artifact_id,executor_id,handle,state) "
                                 "VALUES(?,?,?,?,?,?)", (execution, transfer_id, artifact, "aria2", "{}",
                                                         "succeeded" if delivered else "failed"))
                    conn.execute("INSERT INTO execution_attempt_provenance(execution_attempt_id,transfer_id,artifact_id,"
                                 "ordinal,provider_id,outcome,delivered) VALUES(?,?,?,?,?,?,?)",
                                 (execution, transfer_id, artifact, ordinal, provider,
                                  "completed" if delivered else "failed", delivered))
        conn.commit()
    finally:
        conn.close()


@pytest_asyncio.fixture
async def stats_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "methods.sqlite3")
    await database.init_db()
    _seed()
    return database


async def _methods(period: str) -> list[dict]:
    return (await get_stats_detail(period=period, application=SimpleNamespace(definitions=definitions)))[
        "transfer_methods"]


def _expected(max_days: int | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _kind, _status, days, _completed, _artifacts, methods in FIXTURE.values():
        if max_days is None or days < max_days:
            for method in methods:
                counts[method] = counts.get(method, 0) + 1
    return counts


@pytest.mark.asyncio
@pytest.mark.parametrize("period, max_days", [("7d", 7), ("all", None)])
async def test_each_transfer_counts_once_under_each_method_that_delivered_it(stats_db, period, max_days):
    rows = await _methods(period)
    assert {row["method"]: row["count"] for row in rows} == _expected(max_days)
    # Ranked by count, most used first; equal counts by name.
    assert rows == sorted(rows, key=lambda row: (-row["count"], row["method"]))


@pytest.mark.asyncio
async def test_the_counts_written_out(stats_db):
    assert {row["method"]: row["count"] for row in await _methods("7d")} == {
        "Debrid": 6, "HTTP(S)": 5, "FTP / SFTP": 3, "Media Downloads": 2, "SCP": 1, "rsync": 1, "WebDAV": 1,
        "Usenet": 1, "Unattributed": 5}


@pytest.mark.asyncio
async def test_what_was_submitted_is_never_a_method(stats_db):
    names = {row["method"] for row in await _methods("all")}
    assert not names & {"magnet", "nzb", "nzb_file", "torrent_file", "metalink_file", "meta4", "manual_file",
                        "direct_link", "Multimeta", "Metalink", "aria2", "SABnzbd", "Torrent"}


def test_every_delivering_integration_declares_its_method_and_a_descriptor_none():
    declared = {definition.id: definition.presentation.acquisition_method for definition in definitions}
    assert declared == {
        "alldebrid": "Debrid", "debridlink": "Debrid", "premiumize": "Debrid", "realdebrid": "Debrid",
        "torbox": "Debrid", "usenet": "Usenet", "general_http": "HTTP(S)", "general_ftp": "FTP / SFTP",
        "general_scp": "SCP", "general_rsync": "rsync", "general_webdav": "WebDAV", "media": "Media Downloads",
        "multimeta": None, "aria2": None, "rsync": None}


@pytest.mark.asyncio
async def test_the_top_ten_cap_is_kept(stats_db):
    rows = await _methods("all")
    assert len(rows) <= 10
    source = (await get_stats_detail(period="all", application=SimpleNamespace(definitions=definitions)))
    assert "sources" not in source
