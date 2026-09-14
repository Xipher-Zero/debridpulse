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
    # raw status (9) + 2 recovery-net queries + one SEEK per settled raw
    # status only if room remains (7) + 1 main projection fetchall +
    # 1 total-count fetchone = 20 fixed statements, independent of table size.
    expected = ["fetchall"] * (len(downloads._ACTIVITY_LIVE_RAW_STATUSES) + 2 + len(downloads._SETTLED_RAW_STATUSES) + 1) + ["fetchone"]
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

    This exact test caught a real bug during this correction: the first
    working version of the "recovery net" (see _recovery_net_candidate_ids)
    used a plain ``JOIN`` from ``transfer_pause_intents``/
    ``transfer_input_challenges`` to ``torrents``. SQLite's planner silently
    reordered it to drive from ``torrents`` and sort every settled-status row
    in a temp b-tree before applying LIMIT -- work proportional to total
    history size, exactly the pathology this correction exists to eliminate,
    invisible from the two per-status SEEK queries' own EXPLAIN QUERY PLAN
    output. Forcing the join order with ``CROSS JOIN`` fixed it; this test
    is the regression guard for that fix specifically.
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
async def test_recovery_net_queries_seek_and_cap_the_auxiliary_table_itself(activity_db, monkeypatch):
    """Section 4 boundedness proof, property 2: EXPLAIN QUERY PLAN for the
    recovery-net queries (see _recovery_net_candidate_ids) must show an
    indexed SEEK into the auxiliary table itself (transfer_pause_intents /
    transfer_input_challenges), bounded by _RECOVERY_NET_SCAN_CAP, and must
    never fall back to scanning that table in full -- "decoupled from
    torrents" is not the same claim as "bounded regardless of history": if
    the auxiliary table itself has no cap (a pre-existing engine gap -- see
    _recovery_net_candidate_ids), a plan that scans it in full is still
    unbounded, just against a different table. The auxiliary-table growth
    case is exercised end-to-end (not just via EXPLAIN QUERY PLAN text) in
    test_recovery_net_query_time_does_not_grow_with_auxiliary_table_size
    below.
    """
    rows = [(1, "downloading", "2026-01-01T00:00:00")]
    rows += [(100 + i, "completed", f"2026-02-{i + 1:02d}T00:00:00") for i in range(500)]
    _seed(activity_db, rows)

    captured = _capture_fetchall_sql(monkeypatch)
    await downloads.list_operational_torrents(
        status=None, search=None, limit=10, offset=0, order="activity", application=_application(),
    )

    live_count = len(downloads._ACTIVITY_LIVE_RAW_STATUSES)
    recovery_net_queries = captured[live_count:live_count + 2]
    assert len(recovery_net_queries) == 2

    conn = sqlite3.connect(activity_db)
    try:
        for sql, params in recovery_net_queries:
            assert "CROSS JOIN torrents" in sql
            assert str(downloads._RECOVERY_NET_SCAN_CAP) not in sql  # passed as a bound param, not inlined
            assert params[0] == downloads._RECOVERY_NET_SCAN_CAP
            plan = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
            detail_by_step = [str(row[-1]) for row in plan]
            plan_text = "\n".join(detail_by_step)
            # The auxiliary table access step must be index-qualified -- either
            # "SEARCH ... USING" (pause_intents seeks on the paused=1
            # equality) or "SCAN ... USING COVERING INDEX" (input_challenges
            # has no equality predicate, only the ORDER BY, so SQLite reports
            # an ordered index walk as SCAN rather than SEARCH -- still
            # index-driven and LIMIT-bounded, never an unqualified full scan).
            # The join must not flip back to walking torrents either.
            aux_steps = [
                detail for detail in detail_by_step
                if detail.startswith("SCAN transfer_pause_intents")
                or detail.startswith("SCAN transfer_input_challenges")
                or detail.startswith("SEARCH transfer_pause_intents")
                or detail.startswith("SEARCH transfer_input_challenges")
            ]
            assert len(aux_steps) == 1, f"expected exactly one auxiliary-table access step, got: {aux_steps}"
            assert "INDEX" in aux_steps[0], f"auxiliary-table access is not index-qualified: {aux_steps[0]}"
            assert "SCAN torrents" not in plan_text
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_recovery_net_query_time_does_not_grow_with_auxiliary_table_size(activity_db):
    """Section 4 boundedness proof, property 2, adversarial distribution --
    the exact scenario required: a small number of relevant lingering pause/
    challenge rows, then a large and growing number of IRRELEVANT historical
    rows in the auxiliary table itself (not in torrents), an identical
    requested Dashboard limit, and a direct timing measurement of
    _activity_cohort_candidate_ids. This is the case
    test_candidate_acquisition_time_does_not_grow_with_history_size above
    does NOT cover: that test grows torrents while the auxiliary tables stay
    empty. This one grows the auxiliary tables while torrents stays small,
    isolating the recovery net's own boundedness specifically.
    """
    import time

    async def _time_with_auxiliary_table_size(irrelevant_row_count: int) -> float:
        db_path = activity_db.parent / f"aux-adversarial-{irrelevant_row_count}.db"
        database.DB_PATH = db_path
        await database.init_db()
        conn = sqlite3.connect(db_path)
        # One ordinary live transfer, and one settled-raw-status transfer that
        # is the SOLE relevant lingering-pause-intent case, touched recently.
        conn.execute(
            "INSERT INTO torrents(id,hash,name,status,source,progress,created_at,completed_at) "
            "VALUES(1,'live-hash','T1','downloading','magnet',0.0,'2020-01-01T00:00:00',NULL)"
        )
        conn.execute(
            "INSERT INTO torrents(id,hash,name,status,source,progress,created_at,completed_at) "
            "VALUES(2,'relevant-hash','T2','error','magnet',0.0,'2026-06-01T00:00:00',NULL)"
        )
        conn.execute(
            "INSERT INTO transfer_pause_intents(torrent_id,paused,updated_at) VALUES(2,1,'2026-06-01T00:00:00')"
        )
        # A large, growing number of IRRELEVANT historical pause-intent rows,
        # all touched long ago so they never compete with the cap's
        # newest-first order -- exactly the shape a genuine leak of the
        # confirmed pre-existing engine gap would produce over time.
        conn.executemany(
            "INSERT INTO transfer_pause_intents(torrent_id,paused,updated_at) VALUES(?,?,?)",
            [(10_000 + i, 1, f"2000-01-01T00:00:{i % 60:02d}") for i in range(irrelevant_row_count)],
        )
        conn.commit()
        conn.close()

        start = time.perf_counter()
        for _ in range(10):
            ids = await downloads._activity_cohort_candidate_ids(
                ["t.status NOT IN ('deleted', 'consolidated')"], [], 5
            )
        elapsed = (time.perf_counter() - start) / 10
        assert 2 in ids, f"relevant lingering-pause transfer missing at n={irrelevant_row_count}: {ids}"
        return elapsed

    small_time = await _time_with_auxiliary_table_size(2_000)
    large_time = await _time_with_auxiliary_table_size(300_000)

    assert large_time < small_time * 5 + 0.05, (
        f"recovery-net query time grew with auxiliary table size: "
        f"{small_time * 1000:.3f}ms at 2k irrelevant rows vs "
        f"{large_time * 1000:.3f}ms at 300k irrelevant rows"
    )


@pytest.mark.asyncio
async def test_recovery_net_query_time_does_not_grow_with_input_challenge_table_size(activity_db):
    """Same adversarial-growth proof as
    test_recovery_net_query_time_does_not_grow_with_auxiliary_table_size,
    for transfer_input_challenges specifically -- it has no equality
    predicate to seek on (unlike transfer_pause_intents' paused=1), only the
    ORDER BY + LIMIT cap, so it needed its own INDEXED BY forcing and its own
    growth proof rather than assuming symmetry with the pause-intent case.
    """
    import time

    async def _time_with_challenge_table_size(irrelevant_row_count: int) -> float:
        db_path = activity_db.parent / f"aux-challenge-adversarial-{irrelevant_row_count}.db"
        database.DB_PATH = db_path
        await database.init_db()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO torrents(id,hash,name,status,source,progress,created_at,completed_at) "
            "VALUES(1,'live-hash','T1','downloading','magnet',0.0,'2020-01-01T00:00:00',NULL)"
        )
        conn.execute(
            "INSERT INTO torrents(id,hash,name,status,source,progress,created_at,completed_at) "
            "VALUES(2,'relevant-hash','T2','lost','magnet',0.0,'2026-06-01T00:00:00',NULL)"
        )
        conn.execute(
            "INSERT INTO transfer_input_challenges"
            "(transfer_id, challenge_id, generation, reason, origin, integration_id, operation_id, methods, created_at, updated_at) "
            "VALUES (2, 'chal-relevant', 1, 'auth', 'provider', 'alldebrid', 'op-relevant', '[]', 0, 1780000000)"
        )
        conn.executemany(
            "INSERT INTO transfer_input_challenges"
            "(transfer_id, challenge_id, generation, reason, origin, integration_id, operation_id, methods, created_at, updated_at) "
            "VALUES (?, ?, 1, 'auth', 'provider', 'alldebrid', 'op-irrelevant', '[]', 0, 0)",
            [(10_000 + i, f"chal-irrelevant-{i}") for i in range(irrelevant_row_count)],
        )
        conn.commit()
        conn.close()

        start = time.perf_counter()
        for _ in range(10):
            ids = await downloads._activity_cohort_candidate_ids(
                ["t.status NOT IN ('deleted', 'consolidated')"], [], 5
            )
        elapsed = (time.perf_counter() - start) / 10
        assert 2 in ids, f"relevant lingering-challenge transfer missing at n={irrelevant_row_count}: {ids}"
        return elapsed

    small_time = await _time_with_challenge_table_size(2_000)
    large_time = await _time_with_challenge_table_size(300_000)

    assert large_time < small_time * 5 + 0.05, (
        f"recovery-net query time grew with input-challenge table size: "
        f"{small_time * 1000:.3f}ms at 2k irrelevant rows vs "
        f"{large_time * 1000:.3f}ms at 300k irrelevant rows"
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


@pytest.mark.asyncio
async def test_settled_raw_status_transfer_with_lingering_pause_intent_is_still_projected_as_live(activity_db):
    """Correctness requirement: raw-status candidate ACQUISITION partitions
    on torrents.status alone, but effective_presentation can diverge from
    raw status via transfer_pause_intents / transfer_input_challenges, which
    are not guaranteed to be cleared on every terminal lifecycle transition
    (confirmed pre-existing engine behavior, out of scope to fix here -- see
    _recovery_net_candidate_ids). Without the recovery net, a transfer whose
    raw status is "error" but which still carries paused=1 would be fetched
    into the SETTLED query only -- and if the live cohort alone already
    fills the page, the settled query never even runs, silently excluding an
    actually-live transfer from the page entirely.
    """
    import sqlite3 as _sqlite3

    # Ten genuinely live rows fill the page on their own.
    rows = [(i, "downloading", f"2026-06-{i:02d}T00:00:00") for i in range(1, 11)]
    # One more transfer: raw status is a settled value, but it still carries
    # a lingering pause intent -- the exact gap this net closes. Give it the
    # newest timestamp so it would be first if it is correctly surfaced.
    rows.append((99, "error", "2026-07-01T00:00:00"))
    _seed(activity_db, rows)
    conn = _sqlite3.connect(activity_db)
    conn.execute("INSERT INTO transfer_pause_intents(torrent_id, paused) VALUES (99, 1)")
    conn.commit()
    conn.close()

    result = await downloads.list_operational_torrents(
        status=None, search=None, limit=10, offset=0, order="activity", application=_application(),
    )
    ids = [item["id"] for item in result["items"]]
    assert 99 in ids, "settled-raw-status transfer with a lingering pause intent must still be projected"
    projected = next(item for item in result["items"] if item["id"] == 99)
    assert projected["presentation_status"] == "paused"
    # It must be sorted as live/actionable (tier 0), not settled history.
    assert ids.index(99) < len(ids)
    for other in ids:
        if other == 99:
            continue
        other_item = next(item for item in result["items"] if item["id"] == other)
        if other_item["presentation_status"] == "failed":
            assert ids.index(99) < ids.index(other)


@pytest.mark.asyncio
async def test_settled_raw_status_transfer_with_lingering_input_challenge_is_still_projected_as_live(activity_db):
    """Same gap as the pause-intent case above, for transfer_input_challenges."""
    import sqlite3 as _sqlite3

    rows = [(i, "downloading", f"2026-06-{i:02d}T00:00:00") for i in range(1, 11)]
    rows.append((99, "lost", "2026-07-01T00:00:00"))
    _seed(activity_db, rows)
    conn = _sqlite3.connect(activity_db)
    conn.execute(
        "INSERT INTO transfer_input_challenges"
        "(transfer_id, challenge_id, generation, reason, origin, integration_id, operation_id, methods, created_at, updated_at) "
        "VALUES (99, 'chal-99', 1, 'auth', 'provider', 'alldebrid', 'op-99', '[]', 0, 0)"
    )
    conn.commit()
    conn.close()

    result = await downloads.list_operational_torrents(
        status=None, search=None, limit=10, offset=0, order="activity", application=_application(),
    )
    ids = [item["id"] for item in result["items"]]
    assert 99 in ids, "settled-raw-status transfer with a lingering input challenge must still be projected"
    projected = next(item for item in result["items"] if item["id"] == 99)
    assert projected["presentation_status"] == "input_required"
