"""DBMAINT-001 — database-maintenance inventory, backup content and wipe-ordering proofs.

A backup (a restore point, ``services.backup``) is a whole-database copy of the
authoritative SQLite database, and the explicit wipe claims to purge it. The wipe
is driven by one inventory (``services.db_maintenance.TABLES``). Before this module existed, nothing
compared that inventory against the *real* initialized schema: the only backup test
fabricated ``sqlite_master`` rows *from* ``TABLES`` itself, so an omitted table could
never be detected — the inventory under test was also the oracle.

These regressions therefore discover the durable application schema independently,
straight out of ``sqlite_master`` on a freshly initialized database, and assert the
inventory covers it. They also exercise the real wipe against real populated canonical
rows with ``PRAGMA foreign_keys=ON``, so an unsafe delete order fails as a genuine
foreign-key error rather than a mocked call-order expectation.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio

import db.database as database
from backup_support import restore_point_database
from services import db_maintenance

# SQLite's own AUTOINCREMENT bookkeeping. It is engine-internal, is recreated by
# SQLite on demand, and carries no application fact, so database maintenance
# deliberately does not enumerate it. (The wipe does reset the
# relevant counters explicitly; that is a separate, deliberate statement.)
_ENGINE_INTERNAL_TABLES = {"sqlite_sequence"}

# Created only by the v112 migration path, never by ``init_db()``. The inventory
# legitimately names it because every migrated database has it.
_MIGRATION_ONLY_TABLES = {"schema_migrations"}

_CANONICAL_TABLES = (
    "canonical_candidate_bindings",
    "canonical_candidate_origins",
    "artifact_consolidations",
)


@pytest_asyncio.fixture
async def db_path(tmp_path, monkeypatch):
    path = tmp_path / "db-maintenance.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()
    return path


def _real_application_tables(path: Path) -> set[str]:
    """Discover the live schema independently of the inventory under test."""
    with sqlite3.connect(path) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return names - _ENGINE_INTERNAL_TABLES


async def _seed_canonical_state() -> None:
    """Representative parent rows plus populated canonical/consolidation state."""
    async with database.get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "INSERT INTO torrents(id,hash,name,status) VALUES(1,?,?,'completed')", ("c" * 40, "canonical"),
        )
        await db.execute(
            "INSERT INTO download_files(id,torrent_id,filename,size_bytes,status) VALUES(10,1,'canonical.bin',4096,'completed')"
        )
        await db.execute(
            "INSERT INTO download_files(id,torrent_id,filename,size_bytes,status) VALUES(11,1,'contributing.bin',4096,'completed')"
        )
        await db.execute(
            "INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state) VALUES('req-canonical',1,0,'{}','resolved')"
        )
        await db.execute(
            "INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state) VALUES('req-contributing',1,1,'{}','resolved')"
        )
        await db.execute(
            "INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES('att-1','req-canonical','general_http','succeeded')"
        )
        await db.execute(
            "INSERT INTO canonical_candidate_bindings"
            "(id,canonical_artifact_id,candidate_id,provider_id,source_scope,source_key,role,candidate_order) "
            "VALUES(1,10,'cand-1','general_http','host','example.org','canonical',1)"
        )
        await db.execute(
            "INSERT INTO canonical_candidate_bindings"
            "(id,canonical_artifact_id,candidate_id,provider_id,source_scope,source_key,role,candidate_order) "
            "VALUES(2,10,'cand-2','general_http','host','mirror.example.org','alternate',2)"
        )
        await db.execute(
            "INSERT INTO canonical_candidate_origins"
            "(id,binding_id,contributing_artifact_id,contributing_transfer_id,request_id,resolution_attempt_id,discovered_candidate_id) "
            "VALUES(1,1,11,1,'req-canonical','att-1','cand-1')"
        )
        await db.execute(
            "INSERT INTO artifact_consolidations"
            "(contributing_artifact_id,source_transfer_id,source_request_id,canonical_artifact_id) "
            "VALUES(11,1,'req-contributing',10)"
        )
        await db.commit()


# --------------------------------------------------------------------------- #
# RED-A1 — real-schema inventory completeness
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_backup_inventory_covers_every_durable_application_table(db_path):
    """The inventory is compared against the real initialized schema, not itself.

    This is the regression that prevents a future durable application table from
    being added to the schema while database-maintenance backup silently omits it.
    """
    real = _real_application_tables(db_path)
    assert _CANONICAL_TABLES[0] in real, "fixture must initialize the real canonical schema"

    omitted = sorted(real - set(db_maintenance.TABLES))
    assert omitted == [], (
        "database-maintenance backup inventory omits durable application tables "
        f"present in the real initialized schema: {omitted}"
    )


@pytest.mark.asyncio
async def test_backup_inventory_names_no_unknown_table(db_path):
    """Every inventory entry is a real table, or an explicitly justified exception."""
    real = _real_application_tables(db_path)
    unknown = sorted(set(db_maintenance.TABLES) - real - _MIGRATION_ONLY_TABLES)
    assert unknown == [], f"inventory names tables that do not exist in the real schema: {unknown}"


# --------------------------------------------------------------------------- #
# RED-A3 — populated backup content
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_backup_exports_populated_canonical_and_consolidation_state(db_path, tmp_path, monkeypatch):
    await _seed_canonical_state()

    copied = await restore_point_database(tmp_path, monkeypatch)
    conn = sqlite3.connect(copied)
    conn.row_factory = sqlite3.Row
    try:
        exported = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in _CANONICAL_TABLES}
    finally:
        conn.close()

    assert len(exported["canonical_candidate_bindings"]) == 2
    assert len(exported["canonical_candidate_origins"]) == 1
    assert len(exported["artifact_consolidations"]) == 1

    # The authoritative canonical facts really survive.
    assert sorted(row["candidate_order"] for row in exported["canonical_candidate_bindings"]) == [1, 2]
    assert exported["canonical_candidate_origins"][0]["discovered_candidate_id"] == "cand-1"
    assert exported["artifact_consolidations"][0]["canonical_artifact_id"] == 10


# --------------------------------------------------------------------------- #
# RED-A2 — populated foreign-key-enabled wipe
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_wipe_removes_populated_canonical_state_child_first(db_path):
    """A real FK-backed proof: the wipe must not strand or violate canonical rows."""
    await _seed_canonical_state()

    result = await db_maintenance.wipe_database(verified_quiesced=True)
    assert result["ok"] is True

    async with database.get_db() as db:
        for table in _CANONICAL_TABLES:
            rows = await db.fetchall(f"SELECT COUNT(*) AS n FROM {table}")
            assert rows[0]["n"] == 0, f"{table} survived an explicit whole-database wipe"
        for table in ("torrents", "download_files", "transfer_requests", "resolution_attempts"):
            rows = await db.fetchall(f"SELECT COUNT(*) AS n FROM {table}")
            assert rows[0]["n"] == 0, f"{table} survived an explicit whole-database wipe"
        assert (await db.fetchall("PRAGMA foreign_key_check")) == []

    assert set(_CANONICAL_TABLES) <= set(result["wiped_tables"])


@pytest.mark.asyncio
async def test_wipe_removes_the_transfer_file_selection_intent_child_first(db_path):
    """T13: the transfer-owned selection intent is ordinary application state;
    the wipe removes its entries before its roots, foreign keys enforced."""
    async with database.get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute("INSERT INTO torrents(id,hash,name,status) VALUES(1,?,?,'downloading')", ("d" * 40, "intent"))
        await db.execute(
            "INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state) VALUES('root',1,0,'{}','resolved')")
        await db.execute("INSERT INTO transfer_file_selection_intents(request_id,transfer_id,created_at,updated_at) "
                         "VALUES('root',1,1.0,1.0)")
        await db.execute("INSERT INTO transfer_file_selection_intent_entries(request_id,relative_path,expected_bytes) "
                         "VALUES('root','S1/A.mkv',100)")
        await db.commit()
    result = await db_maintenance.wipe_database(verified_quiesced=True)
    assert result["ok"] is True
    async with database.get_db() as db:
        for table in ("transfer_file_selection_intent_entries", "transfer_file_selection_intents"):
            assert (await db.fetchall(f"SELECT COUNT(*) AS n FROM {table}"))[0]["n"] == 0
        assert (await db.fetchall("PRAGMA foreign_key_check")) == []
    assert {"transfer_file_selection_intents", "transfer_file_selection_intent_entries"} <= set(result["wiped_tables"])
