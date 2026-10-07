from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch


def test_default_database_path_uses_debridpulse_name_and_preserves_legacy_install():
    from db.database import _default_sqlite_path

    with patch.dict(os.environ, {}, clear=True), patch.object(Path, "exists", return_value=False):
        assert _default_sqlite_path() == Path("/app/data/debridpulse.db")

    with patch.dict(os.environ, {}, clear=True), patch.object(
        Path, "exists", side_effect=[True, False]
    ):
        assert _default_sqlite_path() == Path("/app/data/alldebrid.db")

    with patch.dict(os.environ, {"DB_PATH": "/custom/library.db"}, clear=True):
        assert _default_sqlite_path() == Path("/custom/library.db")


def test_runtime_database_is_sqlite_only():
    root = Path(__file__).resolve().parents[1]
    source = (root / "db" / "database.py").read_text().lower()
    config = (root / "core" / "config.py").read_text().lower()
    assert "asyncpg" not in source
    assert "postgres" not in source
    assert "db_type" not in config
    assert not (root / "db" / "migration.py").exists()


# -- the pinned pre-change database upgrades without inventing intent ----------------------------------------------

import sqlite3 as _sqlite3
import zipfile as _zipfile
from pathlib import Path as _Path

import pytest as _pytest

# Written by the pinned baseline's own code (6b691ef7, before transfer-owned
# selection intent existed): a live interactive transfer whose explicit
# selection (two of three files) is committed and fanned out.
BASELINE_BACKUP = _Path(__file__).parent / "fixtures" / "backup-1.0.13-6b691ef7.zip"
BASELINE_SCHEMA = "sha256:4877a1c666288a4fbef8d6b8fdf9c92165cf7c153a246ce3cfbf3dd2727c7078"


def _baseline_database(tmp_path) -> _Path:
    with _zipfile.ZipFile(BASELINE_BACKUP) as package:
        package.extract("debridpulse.db", tmp_path)
    return tmp_path / "debridpulse.db"


def _facts(path):
    with _sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        selections = conn.execute("SELECT id,decision,decision_reason,manifest_committed_at,continuity "
                                  "FROM transfer_file_selections ORDER BY id").fetchall()
        entries = conn.execute("SELECT selection_id,entry_id FROM transfer_file_selection_entries "
                               "ORDER BY selection_id,entry_id").fetchall()
    return tables, selections, entries


def test_the_pinned_baseline_database_holds_a_committed_selection_and_no_intent(tmp_path):
    """FB-6 (prerequisite facts): the history that must never be read as
    current intent."""
    from services import backup
    path = _baseline_database(tmp_path)
    tables, selections, entries = _facts(path)
    assert not {t for t in tables if "intent" in t} - {"transfer_pause_intents"}
    assert [(s[1], s[2], s[3] is not None, s[4]) for s in selections] == [("explicit", "confirmed", True, "proven")]
    assert len(entries) == 2
    with backup._open_frozen(path) as conn:
        assert backup._fingerprint(conn) == BASELINE_SCHEMA


@_pytest.mark.asyncio
async def test_upgrading_the_baseline_database_creates_the_intent_schema_without_backfill(tmp_path, monkeypatch):
    """T11: the canonical bootstrap upgrades the pinned database to the
    current schema; no intent is inferred from its history."""
    import db.database as database
    path = _baseline_database(tmp_path)
    _tables, selections, entries = _facts(path)
    await database.init_db(path)
    tables, after_selections, after_entries = _facts(path)
    assert {"transfer_file_selection_intents", "transfer_file_selection_intent_entries"} <= tables
    with _sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transfer_file_selection_intents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM transfer_file_selection_intent_entries").fetchone()[0] == 0
    assert (after_selections, after_entries) == (selections, entries)
    fresh = tmp_path / "fresh.db"
    monkeypatch.setattr(database, "DB_PATH", fresh)
    await database.init_db()
    assert _structure(path) == _structure(fresh)        # the markers are the v112 migration's, unchanged


def _structure(path):
    """Every (table, column) pair -- the schema fingerprint without the v112
    migration's own marker table."""
    with _sqlite3.connect(path) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name != 'schema_migrations'")]
        return sorted(f"{table}.{column[1]}" for table in tables
                      for column in conn.execute(f'PRAGMA table_info("{table}")'))
