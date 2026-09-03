from __future__ import annotations

import aiosqlite
import pytest

import db.database as database
from integrations.runtime_state import ProviderRuntimeStateStore, RuntimeStateStorageError


@pytest.mark.asyncio
async def test_canonical_database_initialization_creates_runtime_state_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "canonical-init.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", db_path)

    await database.init_db()

    async with aiosqlite.connect(db_path) as db:
        table = await (await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='integration_runtime_state'"
        )).fetchone()
        index = await (await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_integration_runtime_state_updated'"
        )).fetchone()
        columns = await (await db.execute("PRAGMA table_info(integration_runtime_state)" )).fetchall()
    assert table == ("integration_runtime_state",)
    assert index == ("idx_integration_runtime_state_updated",)
    assert {row[1] for row in columns} == {
        "integration_id",
        "state_key",
        "schema_version",
        "payload",
        "observed_at",
        "stale_after",
        "successful_at",
        "created_at",
        "updated_at",
        "generation",
    }


@pytest.mark.asyncio
async def test_failed_replacement_transaction_rolls_back_to_last_known_good(tmp_path, monkeypatch):
    db_path = tmp_path / "replacement-rollback.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()
    store = ProviderRuntimeStateStore()
    original = await store.replace(
        "parcel-lab",
        b"known-good-A",
        schema_version="parcel-v1",
        state_key="calibration",
        observed_at=100.0,
        stale_after=200.0,
        successful_at=101.0,
    )

    # Force SQLite to abort after the replacement transaction has read the
    # current generation but before it can commit the new row.
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """CREATE TRIGGER force_runtime_state_update_failure
               BEFORE UPDATE ON integration_runtime_state
               BEGIN
                 SELECT RAISE(ABORT, 'forced runtime-state update failure');
               END"""
        )
        await db.commit()

    with pytest.raises(RuntimeStateStorageError):
        await store.replace(
            "parcel-lab",
            b"candidate-B",
            schema_version="parcel-v2",
            state_key="calibration",
            observed_at=150.0,
            stale_after=250.0,
            successful_at=151.0,
            expected_generation=original.generation,
        )

    async with aiosqlite.connect(db_path) as db:
        await db.execute("DROP TRIGGER force_runtime_state_update_failure")
        await db.commit()

    retained = await store.load("parcel-lab", "calibration")
    assert retained.payload == b"known-good-A"
    assert retained.schema_version == "parcel-v1"
    assert retained.observed_at == 100.0
    assert retained.stale_after == 200.0
    assert retained.successful_at == 101.0
    assert retained.generation == 1


@pytest.mark.asyncio
async def test_runtime_schema_initialization_does_not_forge_migration_markers(tmp_path, monkeypatch):
    db_path = tmp_path / "migration-marker.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        await db.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES('existing-checkpoint', '2026-09-01T00:00:00Z')"
        )
        await db.commit()

    await database.init_db()
    await ProviderRuntimeStateStore().initialize()

    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        )).fetchall()
    assert rows == [("existing-checkpoint", "2026-09-01T00:00:00Z")]
