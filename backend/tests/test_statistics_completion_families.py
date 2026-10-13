"""Statistics -> Completions: each stacked bucket's source-family split.

A hand-written set of completed transfers and their durable delivery
provenance; every expected family is written out by hand from those facts.
The real ``/api/stats/detail`` route, the real catalog metadata and a real
temporary SQLite database.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from api.routes import _completion_family_members, get_stats_detail
from integrations.catalog import definitions

# id: (status, completed hours ago, [(artifact role, [(ordinal, provider, delivered), ...]), ...], family)
# An artifact role is "canonical" or "standby" (a mirror kept out of the transfer's material).
FIXTURE = {
    1: ("completed", 1, [("canonical", [(1, "realdebrid", 1)])], "premium"),
    2: ("completed", 1, [("canonical", [(1, "general_http", 1)])], "network"),
    # Failover: the first route failed, the material came through HTTP.
    3: ("completed", 2, [("canonical", [(1, "alldebrid", 0), (2, "general_http", 1)])], "network"),
    # One collection, two families: no split rule exists, so neither.
    4: ("completed", 2, [("canonical", [(1, "realdebrid", 1)]), ("canonical", [(1, "general_http", 1)])], "unattributed"),
    # Two premium providers, one family.
    5: ("completed", 3, [("canonical", [(1, "realdebrid", 1)]), ("canonical", [(1, "alldebrid", 1)])], "premium"),
    # History from before delivery provenance.
    6: ("completed", 26, [("canonical", [])], "unattributed"),
    # Native Usenet, TorBox (whose Usenet is TorBox-delivered) and Media Downloads.
    7: ("completed", 26, [("canonical", [(1, "usenet", 1)])], "premium"),
    8: ("completed", 27, [("canonical", [(1, "torbox", 1)])], "premium"),
    9: ("completed", 27, [("canonical", [(1, "media", 1)])], "network"),
    # Delivered, then reacquired from another family: the latest delivery counts.
    10: ("completed", 28, [("canonical", [(1, "realdebrid", 1), (2, "general_http", 1)])], "network"),
    # Part of the material has no recorded delivering provider.
    11: ("completed", 50, [("canonical", [(1, "realdebrid", 1)]), ("canonical", [])], "unattributed"),
    # A standby mirror is not the transfer's material.
    15: ("completed", 50, [("canonical", [(1, "realdebrid", 1)]), ("standby", [(1, "general_http", 1)])], "premium"),
    # An executor id is no source family.
    16: ("completed", 51, [("canonical", [(1, "aria2", 1)])], "unattributed"),
    # Not completions, never counted.
    12: ("error", 1, [("canonical", [(1, "realdebrid", 1)])], None),
    13: ("consolidated", 1, [("canonical", [(1, "general_http", 1)])], None),
    # Older than a week, within the year.
    14: ("completed", 24 * 40, [("canonical", [(1, "general_http", 1)])], "network"),
}


def _seed() -> None:
    conn = sqlite3.connect(database.DB_PATH)
    try:
        for transfer_id, (status, hours, artifacts, _family) in FIXTURE.items():
            conn.execute("INSERT INTO torrents(id,hash,name,status,source,completed_at) "
                         "VALUES(?,?,?,?,?,datetime('now', ?))",
                         (transfer_id, f"hash-{transfer_id}", f"Transfer {transfer_id}", status, "direct_link",
                          f"-{hours} hours"))
            for index, (role, executions) in enumerate(artifacts):
                request_id = f"req-{transfer_id}-{index}"
                conn.execute("INSERT INTO transfer_requests(id,transfer_id,parent_id,ordinal,payload) VALUES(?,?,?,?,?)",
                             (request_id, transfer_id, None, index, "{}"))
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
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "stats.sqlite3")
    await database.init_db()
    _seed()
    return database


async def _detail(period: str) -> dict:
    return await get_stats_detail(period=period, application=SimpleNamespace(definitions=definitions))


def _bucket_keys(expression: str) -> dict[int, str]:
    """Each fixture completion's bucket, by the route's own unchanged expression."""
    conn = sqlite3.connect(database.DB_PATH)
    try:
        return {row[0]: row[1] for row in conn.execute(f"SELECT id, {expression} FROM torrents")}
    finally:
        conn.close()


def _expected(period_hours: int, expression: str) -> dict[str, dict]:
    keys = _bucket_keys(expression)
    buckets: dict[str, dict] = {}
    for transfer_id, (status, hours, _artifacts, family) in FIXTURE.items():
        if status != "completed" or hours >= period_hours:
            continue
        bucket = buckets.setdefault(keys[transfer_id], {"count": 0, "premium": 0, "network": 0, "unattributed": 0})
        bucket["count"] += 1
        bucket[family] += 1
    return buckets


def test_families_come_from_the_metadata_settings_groups_by():
    members = _completion_family_members(definitions)
    assert members["premium"] == ["alldebrid", "debridlink", "premiumize", "realdebrid", "torbox", "usenet"]
    assert members["network"] == ["general_ftp", "general_http", "general_rsync", "general_scp", "general_webdav",
                                  "media", "multimeta"]


@pytest.mark.asyncio
@pytest.mark.parametrize("period, hours, expression", [
    ("7d", 24 * 7, "DATE(completed_at, 'localtime')"),
    ("1y", 24 * 365, "strftime('%Y-%m', completed_at, 'localtime')"),
    ("24h", 24, "strftime('%H:00', completed_at, 'localtime')"),
    ("30d", 24 * 30, "DATE(completed_at, 'localtime')"),
    # All time draws its last 90 days, by day.
    ("all", 24 * 90, "DATE(completed_at, 'localtime')"),
])
async def test_stacked_buckets_split_by_delivering_family_and_conserve_the_total(stats_db, period, hours, expression):
    from api.routes import _sql_date, _sql_strftime
    # The expressions above are the route's own bucket expressions, unchanged.
    assert expression in (_sql_date("completed_at"), _sql_strftime("%Y-%m", "completed_at"),
                          _sql_strftime("%H:00", "completed_at"))
    rows = (await _detail(period))["daily_completions"]
    got = {row["date"]: {key: row[key] for key in ("count", "premium", "network", "unattributed")} for row in rows}
    assert got == _expected(hours, expression)
    for row in rows:
        assert row["count"] == row["premium"] + row["network"] + row["unattributed"]
        assert min(row["premium"], row["network"], row["unattributed"]) >= 0


@pytest.mark.asyncio
async def test_seven_day_totals_by_family(stats_db):
    rows = (await _detail("7d"))["daily_completions"]
    totals = {key: sum(row[key] for row in rows) for key in ("count", "premium", "network", "unattributed")}
    # Completed within 7 days: 1-11, 15, 16. Premium 1, 5, 7, 8, 15; Network 2, 3, 9, 10;
    # unattributed 4 (mixed), 6 (no provenance), 11 (partly unrecorded), 16 (an executor).
    assert totals == {"count": 13, "premium": 5, "network": 4, "unattributed": 4}


@pytest.mark.asyncio
@pytest.mark.parametrize("period, completions", [("30d", 13), ("all", 14)])
async def test_thirty_days_and_all_time_split_by_family_with_their_totals_unchanged(stats_db, period, completions):
    rows = (await _detail(period))["daily_completions"]
    # Transfer 14 completed 40 days ago: outside 30 days, inside all time's 90-day chart.
    assert sum(row["count"] for row in rows) == completions
    assert all(set(row) == {"date", "count", "premium", "network", "unattributed"} for row in rows)


@pytest.mark.asyncio
async def test_bucket_totals_are_the_unchanged_completion_counts(stats_db):
    conn = sqlite3.connect(database.DB_PATH)
    try:
        before = dict(conn.execute(
            "SELECT strftime('%Y-%m', completed_at, 'localtime'), COUNT(*) FROM torrents "
            "WHERE completed_at >= datetime('now', '-1 year') AND status='completed' "
            "GROUP BY strftime('%Y-%m', completed_at, 'localtime')").fetchall())
    finally:
        conn.close()
    assert {row["date"]: row["count"] for row in (await _detail("1y"))["daily_completions"]} == before
