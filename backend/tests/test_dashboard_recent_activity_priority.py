"""DP 1.0.12 UI Finishing (Correction 2): Dashboard Recent Activity must show
live/actionable work ahead of settled history, classified BEFORE the final
Dashboard visible-limit truncation -- not by sorting an already-truncated page.

Real-SQLite regression coverage (never a mocked connection), following the
same pattern as test_operational_downloads_real_sql_performance.py: seeds a
realistic workload, exercises the real ``order="activity"`` mode of
``api.operational_downloads.list_operational_torrents``, and proves both
required boundedness properties from the finishing-pass task (Section 4):

1. application/projection boundedness -- the candidate id count entering
   effective-presentation projection never exceeds the requested limit,
   regardless of total historical transfer count;
2. database execution boundedness -- the candidate queries use the same
   indexed created_at scan/seek shape as the existing default page query,
   never a full-table sort, and the number of SQL statements issued stays
   fixed as history grows.
"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import api.operational_downloads as downloads
import db.database as database
from test_operational_downloads_projection import _ExplodingRepository, _tracking_db


def _seed(db_path: Path, rows) -> None:
    """``rows`` is an iterable of (id, status, created_at) tuples."""
    conn = sqlite3.connect(db_path)
    try:
        for tid, status, created_at in rows:
            conn.execute(
                "INSERT INTO torrents(id,hash,name,status,source,progress,created_at,completed_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (tid, f"hash-{tid}", f"Transfer {tid}", status, "magnet", 0.0, created_at, None),
            )
        conn.commit()
    finally:
        conn.close()


@pytest_asyncio.fixture
async def activity_db(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()
    return db_path


def _application():
    return SimpleNamespace(repository=_ExplodingRepository(), definitions=[])


@pytest.mark.asyncio
async def test_older_active_transfer_outranks_newer_completed_rows_beyond_default_limit(activity_db):
    # One old live transfer, then nine newer completed rows -- the exact
    # defect: with limit=5 the old live row is entirely outside the naive
    # "newest 5 by created_at" page, so a browser-side sort of the truncated
    # page can never recover it.
    rows = [(1, "downloading", "2026-01-01T00:00:00")]
    rows += [(100 + i, "completed", f"2026-06-{i + 1:02d}T00:00:00") for i in range(9)]
    _seed(activity_db, rows)

    default_page = await downloads.list_operational_torrents(
        status=None, search=None, limit=5, offset=0, application=_application(),
    )
    assert 1 not in [item["id"] for item in default_page["items"]]

    activity_page = await downloads.list_operational_torrents(
        status=None, search=None, limit=5, offset=0, order="activity", application=_application(),
    )
    ids = [item["id"] for item in activity_page["items"]]
    assert 1 in ids
    assert ids[0] == 1
    assert len(ids) == 5


@pytest.mark.asyncio
async def test_failed_does_not_outrank_active_work(activity_db):
    rows = [
        (1, "error", "2026-06-05T00:00:00"),   # newer, but settled
        (2, "downloading", "2026-06-01T00:00:00"),  # older, but live
    ]
    _seed(activity_db, rows)

    result = await downloads.list_operational_torrents(
        status=None, search=None, limit=10, offset=0, order="activity", application=_application(),
    )
    ids = [item["id"] for item in result["items"]]
    assert ids == [2, 1]
    assert result["items"][0]["presentation_status"] != "failed"
    assert result["items"][1]["presentation_status"] == "failed"


@pytest.mark.asyncio
async def test_actionable_states_join_the_live_cohort(activity_db):
    rows = [
        (1, "completed", "2026-06-09T00:00:00"),
        (2, "paused", "2026-06-01T00:00:00"),
        (3, "input_required", "2026-06-02T00:00:00"),
        (4, "queued", "2026-06-03T00:00:00"),
        (5, "verifying", "2026-06-04T00:00:00"),
    ]
    _seed(activity_db, rows)

    result = await downloads.list_operational_torrents(
        status=None, search=None, limit=5, offset=0, order="activity", application=_application(),
    )
    ids = [item["id"] for item in result["items"]]
    # All four nonterminal rows precede the single completed row, newest-first
    # inside the live cohort.
    assert ids == [5, 4, 3, 2, 1]


@pytest.mark.asyncio
async def test_recency_is_stable_inside_each_cohort(activity_db):
    rows = [
        (1, "downloading", "2026-06-01T00:00:00"),
        (2, "downloading", "2026-06-03T00:00:00"),
        (3, "completed", "2026-06-02T00:00:00"),
        (4, "completed", "2026-06-04T00:00:00"),
    ]
    _seed(activity_db, rows)

    result = await downloads.list_operational_torrents(
        status=None, search=None, limit=4, offset=0, order="activity", application=_application(),
    )
    ids = [item["id"] for item in result["items"]]
    assert ids == [2, 1, 4, 3]


@pytest.mark.asyncio
async def test_default_downloads_ordering_and_shape_unchanged_when_order_omitted(activity_db):
    rows = [(1, "downloading", "2026-01-01T00:00:00")]
    rows += [(100 + i, "completed", f"2026-06-{i + 1:02d}T00:00:00") for i in range(9)]
    _seed(activity_db, rows)

    async with _tracking_db() as tracker:
        result = await downloads.list_operational_torrents(
            status=None, search=None, limit=5, offset=0, application=_application(),
        )
    assert [item["id"] for item in result["items"]] == [108, 107, 106, 105, 104]
    assert [kind for kind, _sql in tracker.calls] == ["fetchall", "fetchone"]


@pytest.mark.asyncio
async def test_activity_mode_does_not_engage_with_explicit_status_or_offset(activity_db):
    rows = [(1, "downloading", "2026-01-01T00:00:00")]
    rows += [(100 + i, "completed", f"2026-06-{i + 1:02d}T00:00:00") for i in range(9)]
    _seed(activity_db, rows)

    # Explicit status filter: activity mode is meaningless, falls through.
    filtered = await downloads.list_operational_torrents(
        status="completed", search=None, limit=5, offset=0, order="activity", application=_application(),
    )
    assert 1 not in [item["id"] for item in filtered["items"]]

    # Nonzero offset: falls through to ordinary paginated behavior.
    paged = await downloads.list_operational_torrents(
        status=None, search=None, limit=5, offset=5, order="activity", application=_application(),
    )
    assert [item["id"] for item in paged["items"]] == [103, 102, 101, 100, 1]


def _capture_fetchall_sql(monkeypatch):
    """Wrap database.get_db so the test can inspect the exact SQL text of
    every fetchall call, keyed to database.py's real connection (not a mock).
    """
    captured = []
    real_get_db = database.get_db

    @asynccontextmanager
    async def capturing_get_db():
        async with real_get_db() as conn:
            real_fetchall = conn.fetchall

            async def fetchall(query, params=()):
                captured.append((query, tuple(params)))
                return await real_fetchall(query, params)

            conn.fetchall = fetchall
            yield conn

    monkeypatch.setattr(downloads, "get_db", capturing_get_db)
    return captured


@pytest.mark.asyncio
async def test_activity_mode_sql_statement_count_and_candidate_bound_stay_fixed_with_history_growth(activity_db):
    """Section 4 boundedness proof, property 1 (application/projection
    boundedness): the candidate id count entering projection, and the number
    of SQL statements issued, must not grow with total historical row count.
    """
    small_rows = [(1, "downloading", "2026-01-01T00:00:00")]
    small_rows += [(100 + i, "completed", f"2026-02-{i + 1:02d}T00:00:00") for i in range(30)]
    _seed(activity_db, small_rows)

    async with _tracking_db() as small_tracker:
        small_result = await downloads.list_operational_torrents(
            status=None, search=None, limit=10, offset=0, order="activity", application=_application(),
        )
    small_calls = [kind for kind, _sql in small_tracker.calls]

    # Grow the settled cohort by two orders of magnitude.
    big_rows = [
        (5000 + i, "completed", f"2026-03-{(i % 27) + 1:02d}T00:00:00")
        for i in range(3000)
    ]
    _seed(activity_db, big_rows)

    async with _tracking_db() as big_tracker:
        big_result = await downloads.list_operational_torrents(
            status=None, search=None, limit=10, offset=0, order="activity", application=_application(),
        )
    big_calls = [kind for kind, _sql in big_tracker.calls]

    assert len(small_result["items"]) <= 10
    assert len(big_result["items"]) <= 10
    # Fixed statement shape regardless of table size: one SEEK per known live
    # raw status (9) + one SEEK per settled raw status only if room remains
    # (7) + 1 main projection fetchall + 1 total-count fetchone = 18 fixed
    # statements, independent of table size.
    expected = ["fetchall"] * (len(downloads._ACTIVITY_LIVE_RAW_STATUSES) + len(downloads._SETTLED_RAW_STATUSES) + 1) + ["fetchone"]
    assert small_calls == expected
    assert big_calls == small_calls
    assert big_result["total"] == small_result["total"] + len(big_rows)


@pytest.mark.asyncio
async def test_activity_cohort_queries_use_indexed_seek_never_a_scan(activity_db, monkeypatch):
    """Section 4 boundedness proof, property 2 (database execution
    boundedness): every bounded cohort candidate query must be a SEARCH
    (indexed equality seek) against idx_torrents_status_created, never a
    SCAN (sequential walk) and never a temp-b-tree sort. A SEARCH plan is the
    stronger claim EXPLAIN QUERY PLAN is asked to prove here: it means
    SQLite locates the matching status range directly and can stop at
    LIMIT, rather than "an index happens to provide useful ordering" (which
    a SCAN can also claim while still examining an unbounded number of
    non-matching rows first -- exactly the gap a bare "uses an index"
    claim does not close).
    """
    rows = [(1, "downloading", "2026-01-01T00:00:00")]
    rows += [(100 + i, "completed", f"2026-02-{i + 1:02d}T00:00:00") for i in range(500)]
    _seed(activity_db, rows)

    captured = _capture_fetchall_sql(monkeypatch)
    await downloads.list_operational_torrents(
        status=None, search=None, limit=10, offset=0, order="activity", application=_application(),
    )

    live_count = len(downloads._ACTIVITY_LIVE_RAW_STATUSES)
    live_status_queries = captured[:live_count]
    assert len(live_status_queries) == live_count

    conn = sqlite3.connect(activity_db)
    try:
        for sql, params in live_status_queries:
            assert "SELECT" in sql and "torrents" in sql
            assert "JOIN" not in sql  # no correlated/joined per-transfer subquery
            plan = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
            plan_text = "\n".join(str(row) for row in plan)
            assert "idx_torrents_status_created" in plan_text
            assert "SEARCH" in plan_text
            assert "SCAN" not in plan_text
            assert "USE TEMP B-TREE FOR ORDER BY" not in plan_text
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_candidate_acquisition_time_does_not_grow_with_history_size(activity_db):
    """Section 4 boundedness proof, property 2, adversarial distribution.

    Bare EXPLAIN QUERY PLAN text is not sufficient by itself (it describes
    the chosen access strategy, not how many rows are actually examined) --
    this directly measures wall-clock time. Isolates
    ``_activity_cohort_candidate_ids`` itself (not the full
    ``list_operational_torrents`` request), because that request also runs a
    pre-existing, unrelated ``COUNT(*)`` query for pagination ``total`` that
    already scales with history size in BOTH ordering modes today -- mixing
    it in would blur a real pre-existing characteristic together with the
    property this test exists to prove about the NEW candidate-acquisition
    mechanism specifically.

    Historical note: an earlier "recovery net" compensation query once lived
    here to cover a settled raw status that could still carry a stale
    ``transfer_pause_intents``/``transfer_input_challenges`` row. DP 1.0.12
    leveling remediation (FUNC-001) fixed that gap at the source -- a settled
    transfer can no longer carry either row -- so the compensation query was
    removed entirely rather than kept as dead read-model scaffolding.
    """
    import time

    async def _time_candidate_acquisition(preceding_settled_count: int) -> float:
        db_path = activity_db.parent / f"adversarial-{preceding_settled_count}.db"
        database.DB_PATH = db_path
        await database.init_db()
        rows = [(1, "downloading", "2020-01-01T00:00:00")]  # oldest possible
        rows += [
            (1000 + i, "completed", f"2026-01-{(i % 27) + 1:02d}T00:00:00")
            for i in range(preceding_settled_count)
        ]
        _seed(db_path, rows)
        start = time.perf_counter()
        for _ in range(10):
            ids = await downloads._activity_cohort_candidate_ids(
                ["t.status NOT IN ('deleted', 'consolidated')"], [], 5
            )
        elapsed = (time.perf_counter() - start) / 10
        assert 1 in ids
        return elapsed

    small_time = await _time_candidate_acquisition(2_000)
    large_time = await _time_candidate_acquisition(300_000)

    # A per-status SEEK plus a CROSS-JOIN-pinned recovery net are both
    # independent of how many *other* rows exist; a plan that instead scans/
    # sorts history would take roughly 150x longer at 150x the preceding
    # settled row count. Assert the large case is not meaningfully slower
    # than the small case (generous ceiling to stay non-flaky under shared
    # CI hardware, while still failing hard on real O(n) growth).
    assert large_time < small_time * 5 + 0.05, (
        f"candidate acquisition time grew with history size: "
        f"{small_time * 1000:.3f}ms at 2k rows vs {large_time * 1000:.3f}ms at 300k rows"
    )


@pytest.mark.asyncio
async def test_unknown_presentation_status_never_outranks_known_live_work(activity_db):
    """Correctness requirement: a presentation_status this ordering has never
    seen before must never outrank a KNOWN live/actionable item -- it also
    must not be silently assumed settled. Exercised directly against the
    pure classifier (no plausible real raw status maps to an unrecognized
    presentation_status today, so this is a unit-level proof of the
    contract, not an end-to-end fixture).
    """
    assert downloads._activity_priority_tier("downloading") == 0
    assert downloads._activity_priority_tier("some_future_state_nobody_added_yet") == 1
    assert downloads._activity_priority_tier("failed") == 2
    # Tier ordering is what the sort key relies on: known-live < unknown < settled.
    tiers = [
        downloads._activity_priority_tier(status)
        for status in ("downloading", "some_future_state_nobody_added_yet", "failed")
    ]
    assert tiers == sorted(tiers)
    assert tiers[0] < tiers[1] < tiers[2]


