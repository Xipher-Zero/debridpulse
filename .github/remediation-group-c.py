"""Temporary DB-001 remediation applicator. Removed by successful runner."""
from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match, found {count}")
    target.write_text(text.replace(old, new, 1))


# Startup must classify/backup/migrate before any generic current-schema mutation.
replace_once(
    "backend/main.py",
    "from db.database import DatabaseMaintenanceActive, init_db, DB_PATH\n",
    "from db.database import DatabaseMaintenanceActive, DB_PATH\n",
)
replace_once(
    "backend/main.py",
    '''    await init_db()\n    from application.composition import application as default_application\n    application = getattr(app.state, "application", default_application)\n    app.state.application = application\n    from db.migrations.v112 import migrate\n    await migrate(external_executor=cfg.aria2_mode == "external", globally_paused=cfg.paused)\n''',
    '''    # v1.0.12 migration owns database classification and the legacy backup\n    # boundary. No current initializer may touch a predecessor database first.\n    from db.migrations.v112 import migrate\n    await migrate(external_executor=cfg.aria2_mode == "external", globally_paused=cfg.paused)\n    from application.composition import application as default_application\n    application = getattr(app.state, "application", default_application)\n    app.state.application = application\n''',
)

# Ordinary repository initialization owns current schema only, never historical data migration.
replace_once(
    "backend/transfers/repository.py",
    '''            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_request ON download_files(request_id) WHERE request_id IS NOT NULL")\n            await self._backfill_provenance(db)\n            await db.commit()\n''',
    '''            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_request ON download_files(request_id) WHERE request_id IS NOT NULL")\n            await db.commit()\n''',
)

# Generic current-schema establishment must not contain version-specific row repair.
database_path = Path("backend/db/database.py")
database_text = database_path.read_text()
repair_start = database_text.find('    _STATUS_REPR_MAP = {\n')
if repair_start < 0:
    raise SystemExit("backend/db/database.py: legacy status repair block not found")
database_path.write_text(database_text[:repair_start].rstrip() + "\n")

# Make the versioned migration the sole startup/upgrade orchestrator.
replace_once(
    "backend/db/migrations/v112.py",
    "import re\nfrom urllib.parse import urlsplit\n",
    "import re\nimport sqlite3\nfrom urllib.parse import urlsplit\n",
)

migration_helpers = r'''

_CURRENT_MARKER = "1.0.12"
_LEGACY_REQUIRED_TABLES = frozenset({"torrents", "download_files", "events"})
_CURRENT_CANONICAL_TABLES = frozenset({
    "transfer_requests", "provider_resources", "execution_attempts", "route_attempt_provenance",
})
_LEGACY_STATUS_REPR_MAP = {
    "TorrentStatus.PROCESSING": "processing",
    "TorrentStatus.UPLOADING": "uploading",
    "TorrentStatus.READY": "ready",
    "TorrentStatus.ERROR": "error",
    "TorrentStatus.COMPLETED": "completed",
    "TorrentStatus.DELETED": "deleted",
    "TorrentStatus.QUEUED": "queued",
    "TorrentStatus.DOWNLOADING": "downloading",
    "TorrentStatus.PENDING": "pending",
    "TorrentStatus.PAUSED": "paused",
}


def _readonly(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _schema_state(path: Path | None = None) -> str:
    """Classify without opening SQLite in a mode that can create or mutate files."""
    source = Path(path or database.DB_PATH)
    if not source.exists() or source.stat().st_size == 0:
        return "fresh"
    try:
        with _readonly(source) as conn:
            check = conn.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise RuntimeError("Database inspection failed integrity verification")
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
            if not tables:
                return "fresh"
            if "schema_migrations" in tables:
                marked = conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=?", (_CURRENT_MARKER,)
                ).fetchone()
                if marked:
                    return "current"
            if not _LEGACY_REQUIRED_TABLES.issubset(tables):
                raise RuntimeError("Unsupported database schema; migration refused before mutation")
            canonical = tables & _CURRENT_CANONICAL_TABLES
            if canonical:
                backup = source.with_name(source.name + ".pre-v112.sqlite3")
                if not backup.exists():
                    raise RuntimeError(
                        "Unversioned canonical schema has no verified predecessor backup; migration refused"
                    )
            return "legacy"
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("Database inspection failed before migration") from exc


def _validate_predecessor_backup(path: Path) -> None:
    try:
        with _readonly(path) as conn:
            check = conn.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise RuntimeError("Existing pre-migration backup failed verification")
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
            if not _LEGACY_REQUIRED_TABLES.issubset(tables):
                raise RuntimeError("Pre-migration backup is not a supported predecessor schema")
            if tables & _CURRENT_CANONICAL_TABLES:
                raise RuntimeError("Pre-migration backup already contains v1.0.12 canonical schema")
            if "schema_migrations" in tables and conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (_CURRENT_MARKER,)
            ).fetchone():
                raise RuntimeError("Pre-migration backup is already marked current")
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("Pre-migration backup failed verification") from exc


async def _ensure_current_schema(repository: TransferRepository) -> None:
    # These are current-schema definition owners only. Legacy data has already
    # been inspected and backed up before this function is reachable.
    await database.init_db()
    await repository.initialize()


async def _mark_current() -> None:
    async with database.get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY,applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
        await db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(?)", (_CURRENT_MARKER,))
        violations = await db.fetchall("PRAGMA foreign_key_check")
        if violations:
            raise RuntimeError("Current schema refused: database contains foreign-key violations")
        await db.commit()


async def _repair_legacy_statuses(db) -> None:
    for bad_value, good_value in _LEGACY_STATUS_REPR_MAP.items():
        await db.execute("UPDATE torrents SET status=? WHERE status=?", (good_value, bad_value))
'''
replace_once(
    "backend/db/migrations/v112.py",
    "\ndef _identity(kind, value):\n",
    migration_helpers + "\n\ndef _identity(kind, value):\n",
)

# Strengthen/reuse only a genuinely pristine predecessor backup.
replace_once(
    "backend/db/migrations/v112.py",
    '''    if final.exists():\n        async with aiosqlite.connect(final) as existing:\n            check = await (await existing.execute("PRAGMA quick_check")).fetchone()\n            if check != ("ok",):\n                raise RuntimeError("Existing pre-migration backup failed verification")\n        return final\n''',
    '''    if final.exists():\n        _validate_predecessor_backup(final)\n        return final\n''',
)
replace_once(
    "backend/db/migrations/v112.py",
    '''        os.replace(temporary, final)\n''',
    '''        _validate_predecessor_backup(temporary)\n        os.replace(temporary, final)\n''',
)

# Replace the old marker-check -> mutation order with migration-first ownership.
replace_once(
    "backend/db/migrations/v112.py",
    '''async def migrate(*, external_executor: bool, globally_paused: bool = False) -> dict:\n    async with database.get_db() as db:\n        present = await db.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'")\n        if present and await db.fetchone("SELECT version FROM schema_migrations WHERE version='1.0.12'"):\n            return {"migrated": False}\n    backup = await _backup()\n    repository = TransferRepository()\n    await repository.initialize()\n    count = 0\n    async with database.get_db() as db:\n        await db.execute("BEGIN IMMEDIATE")\n        await db.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY,applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)")\n''',
    '''async def migrate(*, external_executor: bool, globally_paused: bool = False) -> dict:\n    state = _schema_state()\n    repository = TransferRepository()\n    if state == "fresh":\n        await _ensure_current_schema(repository)\n        await _mark_current()\n        return {"migrated": False}\n    if state == "current":\n        await _ensure_current_schema(repository)\n        return {"migrated": False}\n\n    # The predecessor image is captured before either current schema owner runs.\n    backup = await _backup()\n    await _ensure_current_schema(repository)\n    count = 0\n    async with database.get_db() as db:\n        await db.execute("BEGIN IMMEDIATE")\n        await db.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY,applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)")\n        await _repair_legacy_statuses(db)\n''',
)
replace_once(
    "backend/db/migrations/v112.py",
    '''        await db.execute("INSERT INTO schema_migrations(version) VALUES('1.0.12')")\n''',
    '''        await db.execute("INSERT INTO schema_migrations(version) VALUES(?)", (_CURRENT_MARKER,))\n''',
)

# Permanent migration tests must start from an exact predecessor schema fixture.
replace_once(
    "backend/tests/test_universal_migration.py",
    '''@pytest_asyncio.fixture\nasync def legacy(tmp_path, monkeypatch):\n    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")\n    await database.init_db()\n    return tmp_path\n''',
    '''@pytest_asyncio.fixture\nasync def legacy(tmp_path, monkeypatch):\n    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")\n    fixture = Path(__file__).with_name("fixtures") / "v1.0.11.1.sql"\n    with sqlite3.connect(database.DB_PATH) as conn:\n        conn.executescript(fixture.read_text())\n    return tmp_path\n''',
)

Path("backend/tests/test_database_migration_ownership.py").write_text(r'''"""DB-001: migration-first startup and pristine predecessor backup contracts."""
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
        conn.execute("INSERT INTO integration_runtime_state(integration_id,state,detail) VALUES('provider-a','ready','stable')")
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
''')
