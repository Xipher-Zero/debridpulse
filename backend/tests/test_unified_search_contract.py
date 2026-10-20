"""The one search language (core.search_contract) and its two executors.

The fixture below is a hand-written set of transfers, relations and journal
events; every expected answer is written out by hand from those facts, never
computed by the code under test. Both lists are driven through their real
routes on a real temporary SQLite database with the real FTS5 index.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException

import api.operational_downloads as routes
from core import search_contract
from db import database, event_journal
from db.event_journal import JournalEvent

SECRET_URL = "https://host.example/file.bin?token=SECRETTOKEN123&sig=abcdef"

# id: (name, status, label, hash, error, request kind, filenames, created_at)
TRANSFERS = {
    1: ("Ubuntu 24.04 Desktop", "completed", "linux", "a" * 40, None, "magnet",
        ["ubuntu-24.04-desktop-amd64.iso"], "2026-10-01 10:00:00"),
    2: ("1fichier.com - abcd + 23 more", "downloading", "100% legit", "hash-two", None, "https",
        ["Example.Release.part01.rar", "Example.Release.part02.rar"], "2026-10-01 10:00:00"),
    3: ("movie.mkv", "error", "file_name", "hash-three", "Provider said: quota exceeded", "https",
        ["movie.mkv"], "2026-10-01 10:00:00"),
    4: ("Other 580 things", "completed", "", "hash-four", None, "https", [], "2026-10-02 10:00:00"),
    5: ("Ubuntu deleted copy", "deleted", "", "deleted:5:fp", None, "https", [], "2026-10-02 10:00:00"),
    6: ("Contributor Alpha", "consolidated", "", "hash-six", None, "https", ["alpha.bin"], "2026-10-02 10:00:00"),
    7: ('He said "hi" it\'s C:\\path [x] (y) a.b/c', "queued", "", "hash-seven", None, "https", [],
        "2026-10-03 10:00:00"),
    8: ("Ünïcode ÄBC", "queued", "", "hash-eight", None, "https", [], "2026-10-03 10:00:00"),
    9: ("secret-file.bin", "queued", "", "hash-nine", None, "https", [], "2026-10-03 10:00:00"),
    10: ("a  b   spaced", "queued", "", "hash-ten", None, "https", [], "2026-10-03 10:00:00"),
    11: ("fileXname 1000 items", "queued", "", "hash-eleven", None, "https", [], "2026-10-03 10:00:00"),
    580: ("zzz", "queued", "", "hash-580", None, "https", [], "2026-10-04 10:00:00"),
}
# Artifact consolidation: transfer 6's file was absorbed into transfer 3's.
# Source hosts of transfer 2's canonical artifacts.
HOSTS = {2: ["1fichier.com", "rapidgator.net"]}


async def _seed() -> None:
    conn = sqlite3.connect(database.DB_PATH)
    try:
        artifact_ids = {}
        for tid, (name, status, label, digest, error, kind, files, created) in TRANSFERS.items():
            conn.execute("INSERT INTO torrents(id,hash,name,status,source,label,error_message,created_at) "
                         "VALUES(?,?,?,?,?,?,?,?)",
                         (tid, digest, name, status, "direct_link", label, error, created))
            url = SECRET_URL if tid == 9 else f"https://files.example/{tid}"
            conn.execute("INSERT INTO transfer_requests(id,transfer_id,parent_id,ordinal,payload) VALUES(?,?,?,?,?)",
                         (f"req-{tid}", tid, None, 0, json.dumps({"kind": kind, "payload": url, "name": name})))
            for index, filename in enumerate(files):
                cur = conn.execute("INSERT INTO download_files(torrent_id,filename,request_id) VALUES(?,?,?)",
                                   (tid, filename, f"req-{tid}" if index == 0 else None))
                if index:
                    conn.execute("INSERT INTO transfer_requests(id,transfer_id,parent_id,ordinal,payload) "
                                 "VALUES(?,?,?,?,?)", (f"req-{tid}-{index}", tid, f"req-{tid}", index,
                                                        json.dumps({"kind": kind, "payload": "x", "name": filename})))
                    conn.execute("UPDATE download_files SET request_id=? WHERE id=?", (f"req-{tid}-{index}", cur.lastrowid))
                artifact_ids.setdefault(tid, []).append(cur.lastrowid)
        for tid, hosts in HOSTS.items():
            for order, host in enumerate(hosts, start=1):
                conn.execute("INSERT INTO canonical_candidate_bindings(canonical_artifact_id,candidate_id,provider_id,"
                             "source_scope,source_key,role,candidate_order) VALUES(?,?,?,?,?,?,?)",
                             (artifact_ids[tid][0], f"c{order}", "general_http", "host", host,
                              "canonical" if order == 1 else "alternate", order))
        conn.execute("INSERT INTO artifact_consolidations(contributing_artifact_id,source_transfer_id,source_request_id,"
                     "canonical_artifact_id) VALUES(?,?,?,?)", (artifact_ids[6][0], 6, "req-6", artifact_ids[3][0]))
        conn.commit()
    finally:
        conn.close()
    async with database.get_db() as db:
        for tid, (name, *_rest) in TRANSFERS.items():
            await event_journal.record(db, JournalEvent(
                "transfer", "transfer.synthetic", "info", f"Added {name}", "transfer", subject_id=tid,
                transfer_id=tid, subject_name=name))
        # A consolidation event names the contributor and its owner.
        await event_journal.record(db, JournalEvent(
            "consolidation", "consolidation.synthetic", "info", "Merged duplicate", "transfer", subject_id=6,
            transfer_id=6, related_transfer_id=3, subject_name="Contributor Alpha"))
        await db.commit()
    while await event_journal.catch_up():
        pass


@pytest_asyncio.fixture
async def search_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "search.sqlite")
    await database.init_db()
    await _seed()
    return database


class _Repository:
    async def _root_route_facts_by_root(self, _db, _ids):
        return {}

    async def presentation(self, transfer_id):
        return {"id": transfer_id}


_APP = SimpleNamespace(repository=_Repository(), definitions=[], engine=None)


async def _downloads(search, **query):
    query = {"status": None, "limit": 25, "offset": 0, "order": None, **query}
    return await routes.list_operational_torrents(search=search, application=_APP, **query)


async def _events(search, **query):
    query = {"level": None, "category": None, "timeframe": "all", "limit": 100, "before": None,
             "snapshot": None, "after": None, "last": False, "window_start": None, **query}
    return await routes.list_activity_events(search=search, **query)


async def _download_ids(search, **query):
    return sorted(item["id"] for item in (await _downloads(search, **query))["items"])


async def _event_transfer_ids(search):
    return sorted({item["transfer_id"] for item in (await _events(search))["items"]})


# ── the language ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw, mode, text, transfer_id", [
    ("#580", "id", None, 580),
    ("  #580  ", "id", None, 580),
    ("#0580", "id", None, 580),
    ("580", "id_or_text", "580", 580),
    ("0580", "id_or_text", "0580", 580),
    ("58", "id", None, 58),
    ("000", "text", "000", None),
    ("1" * 19, "text", "1" * 19, None),
    ("abc#5", "text", "abc#5", None),
    ("Ubuntu   24", "text", "Ubuntu 24", None),
    ("100%", "text", "100%", None),
    ("x" * 200, "text", "x" * 200, None),
])
def test_language_interprets_ids_and_literal_text(raw, mode, text, transfer_id):
    plan = search_contract.plan(raw)
    assert (plan.mode, plan.text, plan.transfer_id) == (mode, text, transfer_id)


@pytest.mark.parametrize("raw", ["#0", "#", "#abc", "# 580", "#-5", "#+5", "#0x1F", "#١٢", "#" + "1" * 19,
                                 "0", "-5", "ab", "x" * 201, "ab\x00c", "abc\u200b"])
def test_language_rejects_what_it_cannot_answer(raw):
    with pytest.raises(search_contract.InvalidSearch):
        search_contract.plan(raw)


@pytest.mark.parametrize("raw", [None, "", "   \t "])
def test_empty_input_is_no_search(raw):
    assert search_contract.plan(raw) is None


def test_short_number_says_its_text_alternative_was_not_searched():
    meta = search_contract.plan("58").describe()
    assert meta["mode"] == "id" and "3 characters" in meta["notice"]


def test_case_fold_is_the_fts5_engines_own_fold():
    # Every character the engine folds folds the same way outside it, and
    # every character it leaves alone is left alone: compared on the engine.
    fold = search_contract.case_fold()
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE f USING fts5(t, tokenize='trigram')")
    samples = "AZÄÖÜÉÑÇİẞΣΑΩДЖᎠᏴϿԨǅǄ"
    for index, char in enumerate(samples, start=1):
        conn.execute("INSERT INTO f(rowid, t) VALUES(?, ?)", (index, f"<{char}>"))
    for index, char in enumerate(samples, start=1):
        for probe in {char, char.lower()}:
            engine = bool(conn.execute("SELECT 1 FROM f WHERE f MATCH ? AND rowid = ?",
                                       (f'"<{probe}>"', index)).fetchone())
            assert engine == (search_contract.fold(f"<{probe}>") == search_contract.fold(f"<{char}>")), (char, probe)
    assert fold[ord("A")] == "a"


# ── the same answers on both lists ──────────────────────────────────────────

# query: (Downloads ids, Activity Log transfer ids), written from the fixture.
SHARED = {
    "Ubuntu 24": ([1], [1]),
    "ubuntu": ([1], [1, 5]),
    "movie.mkv": ([3], [3]),
    "100%": ([2], []),
    "file_name": ([3], []),
    '"hi"': ([7], [7]),
    "it's": ([7], [7]),
    "C:\\path": ([7], [7]),
    "[x] (y)": ([7], [7]),
    "a.b/c": ([7], [7]),
    "ünïcode äbc": ([8], [8]),
    "ÜNÏCODE": ([8], [8]),
    "a b spaced": ([10], [10]),
    "a    b spaced": ([10], [10]),
    "Contributor Alpha": ([3], [6]),
    "#580": ([580], [580]),
    "580": ([4, 580], [4, 580]),
    "#3": ([3], [3, 6]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("query", list(SHARED))
async def test_both_lists_answer_the_same_language(search_db, query):
    downloads, events = SHARED[query]
    assert await _download_ids(query) == downloads
    assert await _event_transfer_ids(query) == events


@pytest.mark.asyncio
async def test_percent_and_underscore_are_literal(search_db):
    # "100%" is not "100 then anything"; "_" is not any one character.
    assert 11 not in await _download_ids("100%")
    assert 11 not in await _download_ids("file_name")


# ── Downloads fields, identity and visibility ──────────────────────────────


@pytest.mark.asyncio
async def test_downloads_finds_the_name_its_row_displays(search_db):
    shown = {item["id"]: item["display_name"] for item in (await _downloads(None, limit=100))["items"]}
    assert shown[2] == "Example.Release + 2 files"
    for transfer_id in (1, 2, 3, 7, 8):
        assert transfer_id in await _download_ids(shown[transfer_id])
    # A torrent's own name is its displayed name, not its member file's.
    assert await _download_ids("Example.Release + 2") == [2]


@pytest.mark.asyncio
async def test_downloads_fields_and_associations(search_db):
    assert await _download_ids("linux") == [1]                    # label
    assert await _download_ids("quota exceeded") == [3]           # current error message
    assert await _download_ids("hash-three") == [3]               # hash as displayed
    assert await _download_ids("rapidgator") == [2]               # an alternate source host
    assert await _download_ids("1fichier.com") == [2]             # root name and host: one result
    assert await _download_ids("part02.rar") == [2]               # a member file name
    assert await _download_ids("alpha.bin") == []                 # a consolidated contributor's file is not this one's


@pytest.mark.asyncio
async def test_downloads_never_matches_hidden_or_secret_material(search_db):
    for secret in ("SECRETTOKEN123", "token=", "sig=abc", "host.example/file", "direct_link", "files.example"):
        assert await _download_ids(secret) == [], secret
    payload = await _downloads("secret-file")
    assert [item["id"] for item in payload["items"]] == [9]
    assert "SECRETTOKEN" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_downloads_visibility_rules_hold_under_search(search_db):
    assert await _download_ids("Ubuntu deleted") == []
    assert await _download_ids("#5") == []
    assert await _download_ids("#6") == []
    assert await _download_ids("#6", status="consolidated") == [6]


@pytest.mark.asyncio
async def test_downloads_filters_before_paginating_with_an_exact_total(search_db):
    first = await _downloads("hash", limit=2, offset=0, order="newest")
    second = await _downloads("hash", limit=2, offset=2, order="newest")
    # Visible transfers whose displayed hash contains "hash": 2,3,4,7,8,9,10,11,580.
    assert first["total"] == second["total"] == 9
    # Newest first, equal timestamps broken by the higher id.
    assert [item["id"] for item in first["items"]] == [580, 11]
    assert [item["id"] for item in second["items"]] == [10, 9]
    assert first["search"] == {"query": "hash", "mode": "text", "transfer_id": None, "status": "complete",
                               "pending": 0, "finality": "exact", "notice": None}


@pytest.mark.asyncio
async def test_unsearched_lists_report_no_search(search_db):
    assert (await _downloads(None))["search"]["mode"] == "none"
    assert (await _events(None))["search"]["mode"] == "none"


# ── invalid input is the same on both lists ────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["ab", "#abc", "#0", "x" * 201, "#" + "9" * 19])
async def test_invalid_query_is_the_same_400_on_both_lists(search_db, query):
    details = []
    for call in (_downloads, _events):
        with pytest.raises(HTTPException) as caught:
            await call(query)
        assert caught.value.status_code == 400
        details.append(caught.value.detail)
    assert details[0] == details[1] and details[0]["code"] == "invalid_query"


# ── Activity Log history ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_history_of_a_removed_transfer_stays_searchable_by_id(search_db):
    events = (await _events("#5"))["items"]
    assert [event["transfer_id"] for event in events] == [5]
    assert events[0]["transfer_available"] is False


@pytest.mark.asyncio
async def test_related_transfer_id_finds_the_consolidation_event(search_db):
    related = [event for event in (await _events("#3"))["items"] if event["transfer_id"] == 6]
    assert related and related[0]["related_transfer_id"] == 3


@pytest.mark.asyncio
async def test_hash_id_is_not_also_searched_as_text(search_db):
    async with database.get_db() as db:
        await event_journal.record(db, JournalEvent("transfer", "transfer.synthetic", "info",
                                                    "mentions #580 in text", "synthetic", subject_id=1))
        await db.commit()
    while await event_journal.catch_up():
        pass
    assert await _event_transfer_ids("#580") == [580]


@pytest.mark.asyncio
async def test_activity_reports_index_lag_as_incomplete_with_unproven_finality(search_db):
    async with database.get_db() as db:
        for index in range(event_journal.INDEX_BATCH + 5):
            await event_journal.record(db, JournalEvent("transfer", "transfer.synthetic", "info",
                                                        f"Lagging needle {index}", "synthetic", subject_id=index))
        await db.commit()
    meta = (await _events("needle"))["search"]
    assert meta["status"] == "index_incomplete" and meta["finality"] == "unproven" and meta["pending"] > 0
    while await event_journal.catch_up():
        pass
    meta = (await _events("needle"))["search"]
    assert meta["status"] == "complete" and meta["finality"] == "bounded" and meta["pending"] == 0
