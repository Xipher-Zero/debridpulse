from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

import db.database as database
import services.db_maintenance as db_maintenance
from integrations.runtime_state import ProviderRuntimeStateStore
from transfers.repository import TransferRepository


@pytest.mark.asyncio
async def test_runtime_state_participates_in_canonical_backup_and_explicit_database_wipe(tmp_path, monkeypatch):
    db_path = tmp_path / "maintenance.sqlite3"
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(
        db_maintenance,
        "get_settings",
        lambda: SimpleNamespace(
            db_backup_enabled=True,
            db_backup_folder=str(backup_root),
            db_backup_keep_days=7,
        ),
    )

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

    backup = await db_maintenance.run_database_backup()
    assert backup["errors"] == []
    assert backup["tables"]["integration_runtime_state"] == 1
    payload = json.loads((backup_root / backup["timestamp"] / "database.json").read_text(encoding="utf-8"))
    rows = payload["tables"]["integration_runtime_state"]
    assert len(rows) == 1
    assert rows[0]["integration_id"] == "parcel-lab"
    assert rows[0]["state_key"] == "calibration"
    assert rows[0]["schema_version"] == record.schema_version
    assert rows[0]["payload"] == {
        "__base64__": base64.b64encode(record.payload).decode("ascii")
    }
    assert rows[0]["generation"] == 1

    wiped = await db_maintenance.wipe_database(verified_quiesced=True)
    assert "integration_runtime_state" in wiped["wiped_tables"]
    assert await store.load("parcel-lab", "calibration") is None


@pytest.mark.asyncio
async def test_runtime_state_is_not_implicitly_pruned_by_database_event_cleanup(tmp_path, monkeypatch):
    db_path = tmp_path / "cleanup.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", db_path)

    await database.init_db()
    store = ProviderRuntimeStateStore()
    await store.replace(
        "parcel-lab",
        b"retain-me",
        schema_version="parcel-maintenance-v1",
        observed_at=1200.0,
    )

    result = await db_maintenance.cleanup_old_events(keep_days=1)
    assert result["keep_days"] == 1
    assert (await store.load("parcel-lab")).payload == b"retain-me"
