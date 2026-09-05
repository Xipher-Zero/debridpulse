from types import SimpleNamespace

import pytest
import pytest_asyncio

from api.operational_downloads import list_operational_torrents
from db import database


@pytest_asyncio.fixture()
async def operational_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "operational-downloads.sqlite")
    await database.init_db()
    return database


async def _insert_transfer(db, suffix, status):
    return await db.execute_returning_id(
        "INSERT INTO torrents(hash, name, status) VALUES(?, ?, ?)",
        (f"hash-{suffix}", f"transfer-{suffix}", status),
    )


@pytest.mark.asyncio
async def test_default_operational_list_hides_fully_consolidated_source_but_keeps_targets_and_partial_source(
    operational_db, monkeypatch
):
    async with database.get_db() as db:
        canonical_id = await _insert_transfer(db, "canonical", "downloading")
        consolidated_id = await _insert_transfer(db, "absorbed", "consolidated")
        partial_id = await _insert_transfer(db, "partial", "downloading")
        deleted_id = await _insert_transfer(db, "deleted", "deleted")
        await db.commit()

    presentations = {
        canonical_id: {"id": canonical_id, "status": "downloading"},
        consolidated_id: {"id": consolidated_id, "status": "consolidated"},
        partial_id: {"id": partial_id, "status": "downloading"},
        deleted_id: {"id": deleted_id, "status": "deleted"},
    }

    class Repository:
        async def presentation(self, transfer_id):
            return presentations[transfer_id]

    application = SimpleNamespace(repository=Repository(), definitions=())
    monkeypatch.setattr(
        "api.operational_downloads._public_transfer_presentation",
        lambda item, _definitions: item,
    )

    result = await list_operational_torrents(
        status=None,
        search=None,
        limit=0,
        offset=0,
        application=application,
    )
    ids = {item["id"] for item in result["items"]}
    assert result["total"] == 2
    assert ids == {canonical_id, partial_id}
    assert consolidated_id not in ids
    assert deleted_id not in ids


@pytest.mark.asyncio
async def test_explicit_consolidated_query_preserves_durable_history_access(operational_db, monkeypatch):
    async with database.get_db() as db:
        consolidated_id = await _insert_transfer(db, "absorbed", "consolidated")
        await _insert_transfer(db, "active", "downloading")
        await db.commit()

    class Repository:
        async def presentation(self, transfer_id):
            return {"id": transfer_id, "status": "consolidated"}

    application = SimpleNamespace(repository=Repository(), definitions=())
    monkeypatch.setattr(
        "api.operational_downloads._public_transfer_presentation",
        lambda item, _definitions: item,
    )

    result = await list_operational_torrents(
        status="consolidated",
        search=None,
        limit=0,
        offset=0,
        application=application,
    )
    assert result == {
        "items": [{"id": consolidated_id, "status": "consolidated"}],
        "total": 1,
    }
