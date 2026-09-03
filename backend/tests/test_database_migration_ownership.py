"""DB-001: migration-first startup and pristine predecessor backup contracts."""
from pathlib import Path
import shutil
import sqlite3

import pytest

import db.database as database
from db.migrations import v112
from transfers.repository import TransferRepository


FIXTURE = Path(__file__).with_name("fixtures") / "v1.0.11.1.sql"
PREDECESSOR_SHA = "f06742847f60b5924e4584714055d0a311172158"


def build_predecessor(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(FIXTURE.read_text())


def tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}


@pytest.mark.asyncio
async def test_fresh_database_becomes_current_without_predecessor_backup(tmp_path, monkeypatch):
    path = tmp_path / "fresh.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    assert await v112.migrate(external_executor=False) == {"migrated": False}
    assert not Path(str(path) + ".pre-v112.sqlite3").exists()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE version='1.0.12'").fetchone() == (1,)
    assert v112._CURRENT_CANONICAL_TABLES.issubset(tables(path))


@pytest.mark.asyncio
async def test_backup_exists_pristine_before_current_initializer_can_run(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    build_predecessor(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO torrents(hash,name,status) VALUES('sentinel','sentinel','downloading')")
        conn.commit()
    monkeypatch.setattr(database, "DB_PATH", path)
    original = database.init_db
    observed = {"called": False}

    async def guarded_init():
        backup = Path(str(path) + ".pre-v112.sqlite3")
        assert backup.exists()
        assert not (tables(backup) & v112._CURRENT_CANONICAL_TABLES)
        with sqlite3.connect(backup) as conn:
            assert conn.execute("SELECT name FROM torrents WHERE hash='sentinel'").fetchone() == ("sentinel",)
        observed["called"] = True
        await original()

    monkeypatch.setattr(database, "init_db", guarded_init)
    report = await v112.migrate(external_executor=False)
    assert report["migrated"] and observed["called"]


@pytest.mark.asyncio
async def test_corrupt_database_fails_before_mutation_or_backup(tmp_path, monkeypatch):
    path = tmp_path / "corrupt.db"
    original = b"not a sqlite database\x00audit"
    path.write_bytes(original)
    monkeypatch.setattr(database, "DB_PATH", path)
    with pytest.raises(RuntimeError):
        await v112.migrate(external_executor=False)
    assert path.read_bytes() == original
    assert not Path(str(path) + ".pre-v112.sqlite3").exists()


@pytest.mark.asyncio
async def test_incompatible_schema_fails_before_mutation_or_backup(tmp_path, monkeypatch):
    path = tmp_path / "unknown.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE mystery(id INTEGER PRIMARY KEY, payload TEXT)")
        conn.execute("INSERT INTO mystery(payload) VALUES('unchanged')")
        conn.commit()
    before = path.read_bytes()
    monkeypatch.setattr(database, "DB_PATH", path)
    with pytest.raises(RuntimeError):
        await v112.migrate(external_executor=False)
    assert path.read_bytes() == before
    assert not Path(str(path) + ".pre-v112.sqlite3").exists()


@pytest.mark.asyncio
async def test_interrupted_after_backup_and_schema_setup_recovers(tmp_path, monkeypatch):
    path = tmp_path / "interrupted.db"
    build_predecessor(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO torrents(id,hash,name,status) VALUES(41,?,?,'downloading')", ("4" * 40, "survivor"))
        conn.commit()
    monkeypatch.setattr(database, "DB_PATH", path)
    backup = await v112._backup()
    pristine = backup.read_bytes()
    await database.init_db()
    await TransferRepository().initialize()
    assert tables(path) & v112._CURRENT_CANONICAL_TABLES
    report = await v112.migrate(external_executor=False)
    assert report["migrated"]
    assert backup.read_bytes() == pristine
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM torrents WHERE id=41").fetchone() == ("survivor",)
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE version='1.0.12'").fetchone() == (1,)


@pytest.mark.asyncio
async def test_predecessor_backup_is_restorable_and_reupgradeable(tmp_path, monkeypatch):
    original = tmp_path / "original.db"
    build_predecessor(original)
    with sqlite3.connect(original) as conn:
        conn.execute("INSERT INTO torrents(id,hash,name,status) VALUES(52,?,?,'completed')", ("5" * 40, "restore-me"))
        conn.commit()
    monkeypatch.setattr(database, "DB_PATH", original)
    report = await v112.migrate(external_executor=False)
    backup = Path(report["backup"])
    restored = tmp_path / "restored.db"
    shutil.copy2(backup, restored)
    monkeypatch.setattr(database, "DB_PATH", restored)
    second = await v112.migrate(external_executor=False)
    assert second["migrated"]
    with sqlite3.connect(restored) as conn:
        assert conn.execute("SELECT name FROM torrents WHERE id=52").fetchone() == ("restore-me",)
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE version='1.0.12'").fetchone() == (1,)


@pytest.mark.asyncio
async def test_current_restart_preserves_runtime_state_and_provenance(tmp_path, monkeypatch):
    path = tmp_path / "current.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    await v112.migrate(external_executor=False)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO integration_runtime_state("
            "integration_id,state_key,schema_version,payload,observed_at,stale_after,"
            "successful_at,created_at,updated_at,generation"
            ") VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("provider-a", "default", "1", b"stable", 1000.0, 2000.0, 1000.0, 900.0, 1000.0, 1),
        )
        conn.execute("INSERT INTO torrents(id,hash,name,status) VALUES(70,?,?,'completed')", ("7" * 40, "current"))
        conn.execute("INSERT INTO transfer_requests(id,transfer_id,ordinal,payload,state) VALUES('r70',70,0,'{}','resolved')")
        conn.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES('a70','r70','provider-a','succeeded')")
        conn.execute("INSERT INTO route_attempt_provenance(resolution_attempt_id,transfer_id,request_id,ordinal,operation,outcome,history_quality) VALUES('a70',70,'r70',1,'resolve','resolved','exact')")
        conn.commit()
        before_runtime = conn.execute("SELECT * FROM integration_runtime_state WHERE integration_id='provider-a'").fetchone()
        before_prov = conn.execute("SELECT * FROM route_attempt_provenance WHERE resolution_attempt_id='a70'").fetchone()
    assert await v112.migrate(external_executor=False) == {"migrated": False}
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM integration_runtime_state WHERE integration_id='provider-a'").fetchone() == before_runtime
        assert conn.execute("SELECT * FROM route_attempt_provenance WHERE resolution_attempt_id='a70'").fetchone() == before_prov


def test_predecessor_fixture_is_release_anchored_and_not_current_schema():
    text = FIXTURE.read_text()
    assert PREDECESSOR_SHA in text
    scratch = FIXTURE.with_name(".fixture-check.sqlite3")
    try:
        scratch.unlink(missing_ok=True)
        build_predecessor(scratch)
        present = tables(scratch)
        assert v112._LEGACY_REQUIRED_TABLES.issubset(present)
        assert not (present & v112._CURRENT_CANONICAL_TABLES)
        assert "schema_migrations" not in present
    finally:
        scratch.unlink(missing_ok=True)
