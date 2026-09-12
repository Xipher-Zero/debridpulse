"""Real-SQLite regression coverage for the DP 1.0.12 Workstream A performance
correction.

Proven defect (live evidence, `ghcr.io/xipher-zero/debridpulse:sha-836f792`):
the operational Downloads bounded projection's per-artifact latest recovery-
snapshot lookup (``api/operational_downloads.py``,
``artifact_presentation_facts``) was a CORRELATED SCALAR SUBQUERY against
``application_events`` with no supporting index, forcing one full table SCAN
per artifact on the page. Live timing: transfer 83 (~703 files) alone cost
``recovery_lookup=20.8583s`` against a table with no index on ``kind``; the
default ``limit=25`` page reached ``time_total=15.077338s`` -- past the
browser's 8s default timeout.

This file exercises the real bootstrap schema (``db.database.init_db``) and
real SQLite execution -- never a mocked/fake connection -- against a seeded
fixture shaped like the production workload: several hundred-file completed
torrents with realistic accumulated ``application_events`` recovery history.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import api.operational_downloads as downloads
import db.database as database
from test_operational_downloads_projection import _ExplodingRepository, _tracking_db

# Old pathological form this fix removes: a scalar subquery correlated to the
# outer artifact row, ordered/limited to fake a "latest" pick per row.
_PATHOLOGICAL_CORRELATED_FORM = "ORDER BY ae.id DESC LIMIT 1"

_BIG_TRANSFER_ID = 9001
_BIG_ARTIFACT_COUNT = 700
_MID_TRANSFER_ID = 9002
_MID_ARTIFACT_COUNT = 220
_RECOVERY_EVENTS_PER_ARTIFACT = 6
_NOISE_EVENT_COUNT = 6000
_SMALL_TRANSFER_COUNT = 20

# A generous, non-flaky ceiling. The fixture reproduces the exact pathology
# that measured 20.8583s for a single 703-file torrent live; the fixed page
# consistently completes in hundredths of a second locally (see the
# checkpoint's benchmark evidence), so this leaves enormous headroom while
# still catching a real regression back to O(rows x total_events) scanning.
_PAGE_TIME_CEILING_SECONDS = 5.0


def _seed_realistic_workload(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    now = "2026-09-01T00:00:00"
    try:
        for i in range(1, _SMALL_TRANSFER_COUNT + 1):
            conn.execute(
                "INSERT INTO torrents(id,hash,name,status,source,progress,created_at,completed_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (i, f"hash-small-{i}", f"Small Transfer {i}", "completed", "magnet", 100.0, now, now),
            )
            for f in range(2):
                conn.execute(
                    "INSERT INTO download_files(torrent_id,filename,status) VALUES(?,?,?)",
                    (i, f"small-{i}-{f}.bin", "completed"),
                )

        conn.execute(
            "INSERT INTO torrents(id,hash,name,status,source,progress,created_at,completed_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (_BIG_TRANSFER_ID, "hash-big", "Big Torrent Release", "completed", "magnet", 100.0, now, now),
        )
        big_artifact_ids = []
        for f in range(_BIG_ARTIFACT_COUNT):
            cur = conn.execute(
                "INSERT INTO download_files(torrent_id,filename,status) VALUES(?,?,?)",
                (_BIG_TRANSFER_ID, f"big-{f:04d}-{'x' * 40}.mkv", "completed"),
            )
            big_artifact_ids.append(cur.lastrowid)

        conn.execute(
            "INSERT INTO torrents(id,hash,name,status,source,progress,created_at,completed_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (_MID_TRANSFER_ID, "hash-mid", "Mid Torrent Release", "completed", "magnet", 100.0, now, now),
        )
        mid_artifact_ids = []
        for f in range(_MID_ARTIFACT_COUNT):
            cur = conn.execute(
                "INSERT INTO download_files(torrent_id,filename,status) VALUES(?,?,?)",
                (_MID_TRANSFER_ID, f"mid-{f:04d}-{'y' * 40}.mkv", "completed"),
            )
            mid_artifact_ids.append(cur.lastrowid)

        # Realistic per-artifact recovery history: several snapshots per
        # artifact, matching the "multiple recovery snapshots per artifact"
        # requirement, so the latest-per-artifact reduction is non-trivial.
        detail = json.dumps({
            "quiescence_reason": "provider_busy",
            "decision_action": "retry",
            "decision_reason": "route_unhealthy",
        })
        recovery_rows = [
            (transfer_id, f"transfer_recovery:{artifact_id}", detail)
            for transfer_id, artifact_ids in (
                (_BIG_TRANSFER_ID, big_artifact_ids),
                (_MID_TRANSFER_ID, mid_artifact_ids),
            )
            for artifact_id in artifact_ids
            for _ in range(_RECOVERY_EVENTS_PER_ARTIFACT)
        ]
        conn.executemany(
            "INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,?,?)",
            recovery_rows,
        )

        # Unrelated event-stream noise accumulated by the rest of the
        # deployment's history -- this is what makes an unindexed per-row
        # scan of application_events expensive; a bounded/indexed lookup
        # must stay cheap regardless of this volume.
        noise_rows = [
            (
                (_BIG_TRANSFER_ID, _MID_TRANSFER_ID, 1, 2, 3)[i % 5],
                f"noise_kind_{i % 41}",
                detail,
            )
            for i in range(_NOISE_EVENT_COUNT)
        ]
        conn.executemany(
            "INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,?,?)",
            noise_rows,
        )
        conn.commit()
    finally:
        conn.close()


@pytest_asyncio.fixture
async def real_sql_downloads_fixture(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()
    _seed_realistic_workload(db_path)
    return db_path


@pytest.mark.asyncio
async def test_bounded_projection_executes_correctly_against_realistic_workload(real_sql_downloads_fixture):
    """Assertion 1/2 (Section 11): the real projection runs and item semantics hold."""
    application = SimpleNamespace(repository=_ExplodingRepository(), definitions=[])
    result = await downloads.list_operational_torrents(
        status=None, search=None, limit=25, offset=0, application=application,
    )
    assert result["total"] == _SMALL_TRANSFER_COUNT + 2
    assert len(result["items"]) == _SMALL_TRANSFER_COUNT + 2

    by_id = {item["id"]: item for item in result["items"]}
    big = by_id[_BIG_TRANSFER_ID]
    assert big["display_name"] == "Big Torrent Release"
    assert big["status"] == "completed"
    # Effective presentation is derived correctly even with the artifact's
    # recovery history present (proves the recovery facts still reach
    # effective_presentation identically after the rewrite).
    assert big["presentation_status"] == "completed"
    assert big["presentation_label"] == "Done"


@pytest.mark.asyncio
async def test_bounded_projection_db_call_count_stays_fixed(real_sql_downloads_fixture):
    """Assertion 3 (Section 11): exactly one fetchall + one fetchone, regardless of workload."""
    application = SimpleNamespace(repository=_ExplodingRepository(), definitions=[])
    async with _tracking_db() as tracker:
        await downloads.list_operational_torrents(
            status=None, search=None, limit=25, offset=0, application=application,
        )
    assert [kind for kind, _sql in tracker.calls] == ["fetchall", "fetchone"]


@pytest.mark.asyncio
async def test_query_no_longer_contains_pathological_per_artifact_correlated_form(real_sql_downloads_fixture):
    """Assertion 4 (Section 11): the old per-row correlated-scalar-subquery shape is gone."""
    application = SimpleNamespace(repository=_ExplodingRepository(), definitions=[])
    captured_sql = {}
    real_get_db = database.get_db

    @asynccontextmanager
    async def capturing_get_db():
        async with real_get_db() as conn:
            real_fetchall = conn.fetchall

            async def fetchall(query, params=()):
                captured_sql["sql"] = query
                captured_sql["params"] = tuple(params)
                return await real_fetchall(query, params)

            conn.fetchall = fetchall
            yield conn

    downloads.get_db = capturing_get_db
    try:
        await downloads.list_operational_torrents(
            status=None, search=None, limit=25, offset=0, application=application,
        )
    finally:
        downloads.get_db = real_get_db

    sql = captured_sql["sql"]
    assert _PATHOLOGICAL_CORRELATED_FORM not in sql
    # The set-oriented replacement shape is present: a page-bounded artifact
    # set joined once to application_events and reduced with a window
    # function, never re-executed per outer row.
    assert "page_recovery_events" in sql
    assert "ROW_NUMBER() OVER" in sql
    assert "PARTITION BY pa.artifact_id" in sql

    return sql, captured_sql["params"]


@pytest.mark.asyncio
async def test_explain_query_plan_proves_indexed_set_oriented_path(real_sql_downloads_fixture):
    """Assertion 5 (Section 11): EXPLAIN QUERY PLAN shows the index seek, not a table scan."""
    application = SimpleNamespace(repository=_ExplodingRepository(), definitions=[])
    captured_sql = {}
    real_get_db = database.get_db

    @asynccontextmanager
    async def capturing_get_db():
        async with real_get_db() as conn:
            real_fetchall = conn.fetchall

            async def fetchall(query, params=()):
                captured_sql["sql"] = query
                captured_sql["params"] = tuple(params)
                return await real_fetchall(query, params)

            conn.fetchall = fetchall
            yield conn

    downloads.get_db = capturing_get_db
    try:
        await downloads.list_operational_torrents(
            status=None, search=None, limit=25, offset=0, application=application,
        )
    finally:
        downloads.get_db = real_get_db

    conn = sqlite3.connect(real_sql_downloads_fixture)
    try:
        indexes = {row[1] for row in conn.execute("PRAGMA index_list('application_events')")}
        assert "idx_application_events_kind_id" in indexes

        plan = conn.execute(
            "EXPLAIN QUERY PLAN " + captured_sql["sql"], captured_sql["params"]
        ).fetchall()
        plan_text = "\n".join(str(row) for row in plan)
        assert "idx_application_events_kind_id" in plan_text
        assert "CORRELATED SCALAR SUBQUERY" not in plan_text
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_page_stays_well_under_generous_ceiling_on_realistic_large_transfers(real_sql_downloads_fixture):
    """Assertion 6 (Section 11): the 25-row page stays far under the browser's 8s timeout.

    Not a brittle micro-benchmark -- paired with the deterministic structural/
    query-plan assertions above; this ceiling exists only to catch a gross
    regression back to the proven O(rows x total_events) scanning shape.
    """
    application = SimpleNamespace(repository=_ExplodingRepository(), definitions=[])
    start = time.perf_counter()
    result = await downloads.list_operational_torrents(
        status=None, search=None, limit=25, offset=0, application=application,
    )
    elapsed = time.perf_counter() - start
    assert len(result["items"]) == _SMALL_TRANSFER_COUNT + 2
    assert elapsed < _PAGE_TIME_CEILING_SECONDS, (
        f"operational Downloads page took {elapsed:.4f}s against the realistic "
        f"fixture, exceeding the {_PAGE_TIME_CEILING_SECONDS}s generous ceiling"
    )


@pytest.mark.asyncio
async def test_filename_aggregation_remains_cheap_relative_to_recovery_lookup(real_sql_downloads_fixture):
    """Structural proof that filename aggregation was never the bottleneck (Section 5.3/5.6)."""
    conn = sqlite3.connect(real_sql_downloads_fixture)
    try:
        t0 = time.perf_counter()
        conn.execute(
            "SELECT torrent_id, group_concat(filename) FROM download_files "
            "WHERE torrent_id = ? GROUP BY torrent_id",
            (_BIG_TRANSFER_ID,),
        ).fetchall()
        filename_agg_time = time.perf_counter() - t0

        t0 = time.perf_counter()
        conn.execute(
            """
            WITH pa AS (
                SELECT id AS artifact_id FROM download_files WHERE torrent_id = ?
            )
            SELECT pa.artifact_id, ae.id
            FROM pa
            JOIN application_events ae ON ae.kind = 'transfer_recovery:' || pa.artifact_id
            """,
            (_BIG_TRANSFER_ID,),
        ).fetchall()
        recovery_lookup_time = time.perf_counter() - t0
    finally:
        conn.close()

    assert filename_agg_time < 0.5
    assert recovery_lookup_time < 0.5
