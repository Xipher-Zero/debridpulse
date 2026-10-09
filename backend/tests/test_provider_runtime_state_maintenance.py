from __future__ import annotations

import sqlite3

import pytest

import db.database as database
from backup_support import restore_point_database
import services.db_maintenance as db_maintenance
from integrations.runtime_state import ProviderRuntimeStateStore
from transfers.repository import TransferRepository


@pytest.mark.asyncio
async def test_runtime_state_participates_in_canonical_backup_and_explicit_database_wipe(tmp_path, monkeypatch):
    db_path = tmp_path / "maintenance.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", db_path)

    await database.init_db()
    repository = TransferRepository()
    await repository.initialize()
    store = ProviderRuntimeStateStore()
    record = await store.replace(
        "parcel-lab",
        b"opaque-maintenance-payload",
        schema_version="parcel-maintenance-v1",
        state_key="calibration",
        observed_at=1000.0,
        stale_after=1100.0,
        successful_at=1001.0,
    )

    copied = await restore_point_database(tmp_path, monkeypatch)
    conn = sqlite3.connect(copied)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM integration_runtime_state").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["integration_id"] == "parcel-lab"
    assert rows[0]["state_key"] == "calibration"
    assert rows[0]["schema_version"] == record.schema_version
    assert bytes(rows[0]["payload"]) == record.payload
    assert rows[0]["generation"] == 1

    wiped = await db_maintenance.wipe_database(verified_quiesced=True)
    assert "integration_runtime_state" in wiped["wiped_tables"]
    assert await store.load("parcel-lab", "calibration") is None
