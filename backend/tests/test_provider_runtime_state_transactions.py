from __future__ import annotations

import aiosqlite
import pytest

import db.database as database
from integrations.runtime_state import ProviderRuntimeStateStore, RuntimeStateStorageError


@pytest.mark.asyncio
async def test_failed_replacement_transaction_rolls_back_to_last_known_good(tmp_path, monkeypatch):
    db_path = tmp_path / "replacement-rollback.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", db_path)
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

    await ProviderRuntimeStateStore().initialize()

    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        )).fetchall()
    assert rows == [("existing-checkpoint", "2026-09-01T00:00:00Z")]
