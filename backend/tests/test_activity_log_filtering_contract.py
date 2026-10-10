"""Activity Log retrieval over the durable event journal (GET /api/events).

Real temporary SQLite databases and the real FTS5 runtime: navigation across
the whole history, filters applied before the page limit, cursor stability
under concurrent inserts, literal search semantics, and truthful search
completeness when the derived index lags, breaks or is unsupported.
"""
from __future__ import annotations

import sqlite3

import pytest
import pytest_asyncio
from fastapi import HTTPException

import api.operational_downloads as activity_routes
from db import database, event_journal
from db.event_journal import JournalEvent


@pytest_asyncio.fixture
async def journal_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "journal.sqlite")
    await database.init_db()
    return database


async def _record(events) -> None:
    async with database.get_db() as db:
        for event in events:
            await event_journal.record(db, event)
        await db.commit()


def _event(index: int, *, category="transfer", severity="info", message=None, transfer_id=None,
           related=None, name=None, detail=None) -> JournalEvent:
    return JournalEvent(category, f"{category}.synthetic", severity, message or f"Synthetic event {index}",
                        "synthetic", subject_id=index, transfer_id=transfer_id, related_transfer_id=related,
                        subject_name=name, detail=detail)


async def _index_all() -> None:
    while await event_journal.catch_up():
        pass


async def _page(**query):
    query.setdefault("level", None)
    query.setdefault("category", None)
    query.setdefault("timeframe", "all")
    query.setdefault("limit", event_journal.DEFAULT_PAGE_SIZE)
    query.setdefault("before", None)
    query.setdefault("snapshot", None)
    query.setdefault("search", None)
    return await activity_routes.list_activity_events(**query)


@pytest.mark.asyncio
async def test_cursor_walks_the_whole_history_beyond_500_and_ignores_events_recorded_meanwhile(journal_db):
    await _record(_event(index) for index in range(1, 1201))
    first = await _page(limit=250)
    assert [item["id"] for item in first["items"]] == list(range(1200, 950, -1))
    assert first["has_more"] and first["next_before"] == 951 and first["snapshot"] == 1200

    # Arrivals during the investigation never shift, duplicate or skip a page.
    await _record(_event(index) for index in range(1201, 1251))
    seen = [item["id"] for item in first["items"]]
    page, before = first, first["next_before"]
    while page["has_more"]:
        page = await _page(limit=250, before=before, snapshot=first["snapshot"])
        assert page["snapshot"] == 1200 and page["newer_available"] is True
        seen.extend(item["id"] for item in page["items"])
        before = page["next_before"]
    assert seen == list(range(1200, 0, -1))
    assert page["next_before"] is None and len(seen) == len(set(seen))

    # A new investigation (no snapshot) starts at the newest event.
    assert (await _page(limit=50))["items"][0]["id"] == 1250


@pytest.mark.asyncio
async def test_filters_compose_and_apply_before_the_page_limit(journal_db):
    events = []
    for index in range(1, 901):
        category = ("routing", "execution", "transfer")[index % 3]
        severity = ("info", "warning", "error")[index % 5 % 3]
        events.append(_event(index, category=category, severity=severity))
    await _record(events)
    expected = [index for index in range(900, 0, -1) if index % 3 == 0 and index % 5 % 3 == 2]
    result = await _page(category="routing", level="error", limit=50)
    assert [item["id"] for item in result["items"]] == expected[:50]
    assert result["has_more"] is (len(expected) > 50)
    # The legacy "warn" alias still selects warnings.
    warnings = await _page(level="warn", limit=250)
    assert warnings["items"] and {item["severity"] for item in warnings["items"]} == {"warning"}
    # A time window that ends before every event finds none; one that covers all keeps them.
    async with database.get_db() as db:
        await db.execute("UPDATE event_journal SET occurred_at = occurred_at - 90000 WHERE id <= 600")
        await db.commit()
    recent = await _page(timeframe="24h", limit=250)
    assert [item["id"] for item in recent["items"]] == list(range(900, 650, -1)) and recent["has_more"]
    last = await _page(timeframe="24h", limit=250, before=651, snapshot=recent["snapshot"])
    assert [item["id"] for item in last["items"]] == list(range(650, 600, -1)) and not last["has_more"]
    assert (await _page(timeframe="72h", limit=250))["items"][-1]["id"] == 651
    assert (await _page(timeframe="72h", limit=250, before=651))["items"][0]["id"] == 650


@pytest.mark.asyncio
async def test_search_is_literal_case_insensitive_substring_plus_exact_transfer_id(journal_db):
    await _record([
        _event(1, message="Route failed: Rate limited", name="Ubuntu-24.04_[x86]".lower(), transfer_id=7),
        _event(2, message="File completed", detail='Release "v2.0 final" 100%_literal', transfer_id=8),
        _event(3, message="Collection member ownership converged", transfer_id=9, related=7),
        _event(4, message="Transfer 77 accepted", transfer_id=77),
    ])
    await _index_all()

    async def ids(search):
        return [item["id"] for item in (await _page(search=search))["items"]]

    assert await ids("RATE LIMIT") == [1]                 # case-insensitive substring
    assert await ids("untu-24.04_[X86") == [1]           # partial name with punctuation
    assert await ids('"v2.0 final"') == [2]              # quotes and spaces are literal text
    assert await ids("100%_lit") == [2]                  # SQL wildcards are literal
    assert await ids("#7") == [3, 1]                     # exact transfer id, as subject or related
    assert await ids("7") == [3, 1]                      # a short number is the id alone
    assert await ids("77") == [4]
    assert await ids("nothing like this") == []
    with pytest.raises(HTTPException) as short:
        await _page(search="ab")
    assert short.value.status_code == 400


@pytest.mark.asyncio
async def test_search_reports_index_lag_and_becomes_complete_once_caught_up(journal_db):
    await _record(_event(index, message=f"needle {index}") for index in range(1, 2 * event_journal.INDEX_BATCH + 101))
    async with database.get_db() as db:
        lagging = await event_journal.page(db, limit=10, before=None, snapshot=None, category=None, severity=None,
                                           since=None, terms=event_journal.search_terms("needle"))
    assert lagging["search"] == {"text": "indexed", "pending": event_journal.INDEX_BATCH + 1, "complete": False}
    assert lagging["items"] == []  # nothing is indexed yet, and the result says so
    # A search indexes one bounded batch first; the rest is still reported.
    partial = await _page(search="needle")
    assert partial["search"]["complete"] is False and partial["search"]["pending"] > 0
    await _index_all()
    complete = await _page(search="needle", limit=50)
    assert complete["search"] == {"text": "indexed", "pending": 0, "complete": True}
    assert complete["items"][0]["id"] == 2 * event_journal.INDEX_BATCH + 100


@pytest.mark.asyncio
async def test_a_corrupt_index_is_detected_dropped_and_rebuilt_without_touching_the_journal(journal_db):
    await _record(_event(index, message=f"alpha {index}") for index in range(1, 3001))
    await _index_all()
    conn = sqlite3.connect(database.DB_PATH)
    try:
        conn.execute("UPDATE event_journal_fts_data SET block=zeroblob(length(block)) WHERE id > 10 AND id % 3 = 0")
        conn.commit()
    finally:
        conn.close()
    assert await event_journal.verify_index() is False
    rebuilding = await _page(search="alpha", limit=50)
    assert rebuilding["search"]["complete"] is False
    await _index_all()
    assert await event_journal.verify_index() is True
    rebuilt = await _page(search="alpha", limit=50)
    assert rebuilt["search"]["complete"] is True and rebuilt["items"][0]["id"] == 3000
    async with database.get_db() as db:
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM event_journal"))["n"] == 3000


@pytest.mark.asyncio
async def test_without_fts5_the_journal_boots_and_text_search_is_truthfully_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "no-fts.sqlite")
    monkeypatch.setattr(event_journal, "fts_supported", lambda: False)
    await database.init_db()
    await _record([_event(1, message="plain text", transfer_id=55), _event(2, message="other")])
    assert await event_journal.catch_up() == 0
    async with database.get_db() as db:
        assert await db.fetchone("SELECT 1 FROM sqlite_master WHERE name='event_journal_fts'") is None
    assert [item["id"] for item in (await _page())["items"]] == [2, 1]
    with pytest.raises(HTTPException) as unavailable:
        await _page(search="plain")
    assert unavailable.value.status_code == 503
    # A transfer id still answers, and says the text half could not run.
    by_id = await _page(search="#55")
    assert [item["id"] for item in by_id["items"]] == [1]
    assert by_id["search"] == {"text": "unavailable", "pending": 0, "complete": False}


@pytest.mark.asyncio
async def test_secrets_are_removed_before_storage_so_neither_rows_nor_the_index_hold_them(journal_db):
    await _record([JournalEvent(
        "routing", "routing.route_failed", "warning",
        "Route failed for https://host.example/file?token=s3cretvalue", "route_attempt",
        subject_name="https://user:hunter2pass@host.example/x.bin",
        detail="Authorization: Bearer abc.def.ghi password=hunter2pass")])
    await _index_all()
    async with database.get_db() as db:
        row = await db.fetchone("SELECT * FROM event_journal")
    stored = " ".join(str(value) for value in row.values())
    for secret in ("s3cretvalue", "hunter2pass", "abc.def.ghi", "https://"):
        assert secret not in stored
        if len(secret) >= 3:
            assert (await _page(search=secret))["items"] == []


@pytest.mark.asyncio
async def test_query_plans_are_indexed_for_every_filter_shape(journal_db):
    await _record(_event(index, category="routing" if index % 2 else "execution") for index in range(1, 201))
    plans = {}
    shapes = {
        "unfiltered": ("j.id <= ?", [999]),
        "category": ("j.id <= ? AND j.category = ?", [999, "routing"]),
        "severity": ("j.id <= ? AND j.severity = ?", [999, "error"]),
        "category+severity": ("j.id <= ? AND j.category = ? AND j.severity = ?", [999, "routing", "error"]),
        "transfer": ("j.id <= ? AND (j.transfer_id = ? OR j.related_transfer_id = ?)", [999, 5, 5]),
        "transfer+text": ("j.id <= ? AND (j.transfer_id = ? OR j.related_transfer_id = ? OR j.id IN "
                          "(SELECT rowid FROM event_journal_fts WHERE event_journal_fts MATCH ? AND rowid <= ?))",
                          [999, 5, 5, '"abc"', 999]),
    }
    # Text search is driven by the index, newest match first, the other filters
    # checked per match: it stops at the page instead of collecting every match.
    text_shapes = {
        "search": ("", []),
        "search+category+severity": (" AND j.category = ? AND j.severity = ?", ["routing", "error"]),
    }
    conn = sqlite3.connect(database.DB_PATH)
    try:
        for name, (where, params) in shapes.items():
            plan = " | ".join(row[3] for row in conn.execute(
                f"EXPLAIN QUERY PLAN SELECT j.id FROM event_journal j WHERE {where} ORDER BY j.id DESC LIMIT 101",
                params))
            plans[name] = plan
            # Exact-transfer search (alone, or with text) unions index seeks and
            # orders only what they found; every other shape walks an index in
            # navigation order and stops at the limit.
            if not name.startswith("transfer"):
                assert "USE TEMP B-TREE" not in plan, (name, plan)
            assert "SCAN j " not in plan + " ", (name, plan)
        for name, (extra, params) in text_shapes.items():
            plan = " | ".join(row[3] for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT j.id FROM event_journal_fts f CROSS JOIN event_journal j ON j.id = f.rowid "
                f"WHERE f.event_journal_fts MATCH ? AND f.rowid <= ?{extra} ORDER BY f.rowid DESC LIMIT 101",
                ['"abc"', 999, *params]))
            plans[name] = plan
            assert plan.startswith("SCAN f VIRTUAL TABLE"), (name, plan)
            assert "SEARCH j USING INTEGER PRIMARY KEY" in plan, (name, plan)
            assert "TEMP B-TREE" not in plan and "LIST SUBQUERY" not in plan, (name, plan)
    finally:
        conn.close()
    assert "idx_event_journal_category" in plans["category"]
    assert "idx_event_journal_severity" in plans["severity"]
    assert "idx_event_journal_category_severity" in plans["category+severity"]
    assert "idx_event_journal_transfer" in plans["transfer"] and "idx_event_journal_related" in plans["transfer"]
    print("\n".join(f"{name}: {plan}" for name, plan in plans.items()))


@pytest.mark.asyncio
async def test_recorded_count_is_every_committed_journal_record_and_nothing_else(journal_db):
    assert await activity_routes.count_activity_events() == {"recorded": 0}
    await _record(_event(index, category="routing" if index % 2 else "transfer", transfer_id=index % 7)
                  for index in range(1, 1301))
    async with database.get_db() as db:
        # Legacy rows are not the journal ...
        await db.execute("INSERT INTO events(torrent_id,level,message) VALUES(NULL,'info','legacy row')")
        await db.commit()
        # ... and an uncommitted record is not one.
        await event_journal.record(db, _event(9999))
        await db.rollback()
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM events"))["n"] == 1
    # Not the index watermark (nothing indexed yet), a page, a filter or one transfer's share.
    assert await activity_routes.count_activity_events() == {"recorded": 1300}
    await _index_all()
    assert (await _page(category="routing", limit=50))["has_more"] is True
    assert await activity_routes.count_activity_events() == {"recorded": 1300}
