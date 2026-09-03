from __future__ import annotations

import sqlite3

import pytest
from pathlib import Path

import db.database as database
from integrations.runtime_state import ProviderRuntimeStateStore, RuntimeStateStorageError
from transfers.repository import TransferRepository


def _use_database(monkeypatch, tmp_path):
    path = tmp_path / "dbarch.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", path)
    return path


@pytest.mark.asyncio
async def test_repository_runtime_initializer_cannot_create_absent_database(monkeypatch, tmp_path):
    path = _use_database(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="bootstrap must run first"):
        await TransferRepository().initialize()
    assert not path.exists()


@pytest.mark.asyncio
async def test_provider_runtime_state_initializer_cannot_create_absent_database(monkeypatch, tmp_path):
    path = _use_database(monkeypatch, tmp_path)
    with pytest.raises(RuntimeStateStorageError):
        await ProviderRuntimeStateStore().initialize()
    assert not path.exists()


@pytest.mark.asyncio
async def test_database_bootstrap_owns_schema_then_runtime_initializers_only_validate(monkeypatch, tmp_path):
    path = _use_database(monkeypatch, tmp_path)
    await database.init_db()
    assert path.exists()
    await TransferRepository().initialize()
    await ProviderRuntimeStateStore().initialize()


@pytest.mark.asyncio
async def test_repository_initializer_rejects_missing_canonical_table_without_repair(monkeypatch, tmp_path):
    path = _use_database(monkeypatch, tmp_path)
    await database.init_db()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE transfer_outcomes")
        conn.commit()
    with pytest.raises(RuntimeError, match="transfer repository schema is incomplete"):
        await TransferRepository().initialize()
    with sqlite3.connect(path) as conn:
        present = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='transfer_outcomes'").fetchone()
    assert present is None


@pytest.mark.asyncio
async def test_runtime_state_initializer_rejects_missing_table_without_repair(monkeypatch, tmp_path):
    path = _use_database(monkeypatch, tmp_path)
    await database.init_db()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE integration_runtime_state")
        conn.commit()
    with pytest.raises(RuntimeStateStorageError):
        await ProviderRuntimeStateStore().initialize()
    with sqlite3.connect(path) as conn:
        present = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='integration_runtime_state'").fetchone()
    assert present is None


def test_runtime_components_contain_no_schema_mutation_authority():
    repository_source = (Path(__file__).parents[1] / "transfers/repository.py").read_text()
    runtime_source = (Path(__file__).parents[1] / "integrations/runtime_state.py").read_text()
    for source in (repository_source, runtime_source):
        upper = source.upper()
        assert "CREATE TABLE" not in upper
        assert "ALTER TABLE" not in upper
        assert "CREATE INDEX" not in upper
