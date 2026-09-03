"""Upgrade actual v1 SQLite rows without changing identity or remote authority."""
import json
from pathlib import Path
import sqlite3

import pytest
import pytest_asyncio

import db.database as database
from db.migrations.v112 import migrate
from transfers.errors import Category, TransferError
from transfers.models import Ownership, TransferState
from transfers.repository import TransferRepository


@pytest_asyncio.fixture
async def legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    fixture = Path(__file__).with_name("fixtures") / "v1.0.11.1.sql"
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.executescript(fixture.read_text())
    return tmp_path


async def parent(identity, *, status="downloading", resource="123", source="manual", payload=None):
    async with database.get_db() as db:
        await db.execute("""INSERT INTO torrents(id,hash,name,magnet,status,alldebrid_id,source,provider_status_code,progress)
            VALUES(?,?,?, ?,?,?,?,?,?)""", (identity, f"{identity:040x}", f"Payload {identity}", payload or f"magnet:?xt=urn:btih:{identity:040x}",
            status, resource, source, 4, 37))
        await db.commit()


async def file(identity, transfer_id, root, *, status="downloading", gid="0123456789abcdef", owned=True,
               source="https://source.example/file", address="https://download.example/capability", mirror=None):
    async with database.get_db() as db:
        await db.execute("""INSERT INTO download_files(id,torrent_id,filename,size_bytes,source_url,download_url,local_path,status,
            download_id,download_client,mirror_group_id,mirror_state) VALUES(?,?,?,?,?,?,?,?,?,'aria2',?,?)""",
            (identity, transfer_id, f"file-{identity}.bin", 4, source, address, str(root / f"file-{identity}.bin"), status, gid,
             mirror, "standby" if mirror else ""))
        if owned and gid:
            await db.execute("INSERT INTO debridpulse_aria2_owned_gids(gid,download_file_id,torrent_id) VALUES(?,?,?)", (gid, identity, transfer_id))
        await db.commit()


@pytest.mark.asyncio
async def test_active_identity_owned_execution_and_history_survive_upgrade(legacy):
    await parent(7)
    await file(11, 7, legacy)
    async with database.get_db() as db:
        await db.execute("INSERT INTO events(torrent_id,message) VALUES(7,'history remains')")
        await db.commit()
    report = await migrate(external_executor=True)
    assert report["transfers"] == 1
    assert Path(report["backup"]).is_file()
    repository = TransferRepository()
    transfer = await repository.get(7)
    artifact = (await repository.artifacts(7))[0]
    assert transfer.id == 7 and transfer.state == TransferState.TRANSFERRING
    assert artifact.id == 11
    assert artifact.target == str(legacy / "file-11.bin")
    assert artifact.execution.context["gid"] == "0123456789abcdef"
    assert await repository.authorize_execution(artifact.execution, "observe")
    assert (await repository.resources(7))[0][0].ownership == Ownership.CREATED
    async with database.get_db() as db:
        assert (await db.fetchone("SELECT count(*) AS n FROM events"))["n"] == 1
        assert not await db.fetchall("PRAGMA foreign_key_check")
    with sqlite3.connect(report["backup"]) as backup:
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT status FROM torrents WHERE id=7").fetchone()[0] == "downloading"
        assert backup.execute("SELECT name FROM sqlite_master WHERE name='transfer_requests'").fetchone() is None
    assert await migrate(external_executor=True) == {"migrated": False}
    assert len(await repository.executions(7)) == 1


@pytest.mark.asyncio
async def test_external_job_without_durable_ownership_is_not_adopted(legacy):
    await parent(1)
    await file(1, 1, legacy, owned=False)
    await migrate(external_executor=True)
    repository = TransferRepository()
    artifact = (await repository.artifacts(1))[0]
    assert not await repository.authorize_execution(artifact.execution, "cancel")
    assert artifact.state == "error"
    assert artifact.error.category == Category.OWNERSHIP_CONFLICT


@pytest.mark.asyncio
async def test_pause_intent_and_deferred_torrent_bytes_survive(legacy):
    await parent(2, status="paused", resource=None)
    payload = b"d4:infod4:name7:payload6:lengthi4eee"
    async with database.get_db() as db:
        await db.execute("INSERT INTO deferred_provider_submissions(torrent_id,kind,payload,filename) VALUES(2,'torrent_file',?,'payload.torrent')", (payload,))
        await db.commit()
    await migrate(external_executor=True, globally_paused=True)
    repository = TransferRepository()
    assert await repository.globally_paused()
    assert (await repository.get(2)).paused
    record = (await repository.requests(2))[0]
    assert record.request.kind == "torrent"
    assert record.request.payload == payload
    assert record.state == "pending"


@pytest.mark.asyncio
async def test_completed_history_is_retained_without_claiming_current_possession(legacy):
    await parent(3, status="completed", source="alldebrid_existing")
    await file(3, 3, legacy, status="completed", gid=None)
    await migrate(external_executor=True)
    repository = TransferRepository()
    assert (await repository.get(3)).state == TransferState.COMPLETED
    assert not Path((await repository.artifacts(3))[0].target).exists()
    assert (await repository.resources(3))[0][0].ownership == Ownership.OBSERVED
    assert not await repository.executions(3)


@pytest.mark.asyncio
async def test_source_outcomes_and_standby_mirrors_stay_outside_physical_denominator(legacy):
    links = ["https://one.example/a", "https://two.example/a", "https://three.example/a"]
    await parent(4, resource=None, source="direct_link", payload=json.dumps(links))
    await file(40, 4, legacy, gid=None, status="queued", source=links[0])
    await file(41, 4, legacy, gid=None, status="duplicate", source=links[1], mirror=40)
    async with database.get_db() as db:
        await db.execute("""INSERT INTO download_files(id,torrent_id,filename,source_url,status,blocked,block_reason)
            VALUES(42,4,'missing',?,'missing',NULL,'source unavailable')""", (links[2],))
        await db.commit()
    await migrate(external_executor=True)
    repository = TransferRepository()
    artifacts = await repository.artifacts(4)
    assert len(artifacts) == 1
    assert artifacts[0].id == 40
    assert len(artifacts[0].candidates) == 2
    assert sum(record.state == "failed" for record in await repository.requests(4)) == 1
    async with database.get_db() as db:
        assert (await db.fetchone("SELECT count(*) AS n FROM download_files WHERE torrent_id=4"))["n"] == 3


@pytest.mark.asyncio
async def test_unresolved_provider_link_is_not_migrated_as_usable_candidate(legacy):
    await parent(5)
    await file(5, 5, legacy, status="pending", gid=None, address="https://source.example/file")
    await migrate(external_executor=True)
    repository = TransferRepository()
    artifact = (await repository.artifacts(5))[0]
    assert artifact.candidates == ()
    assert artifact.state == "unresolved"
    assert next(record for record in await repository.requests(5) if record.id == artifact.request_id).state == "pending"


@pytest.mark.asyncio
async def test_migration_rolls_back_conflicting_provider_ownership(legacy):
    await parent(8, status="uploading", resource="collision")
    await parent(9, status="uploading", resource="collision")
    with pytest.raises(TransferError):
        await migrate(external_executor=True)
    async with database.get_db() as db:
        assert (await db.fetchone("SELECT count(*) AS n FROM transfer_requests"))["n"] == 0
        assert (await db.fetchone("SELECT count(*) AS n FROM torrents WHERE status='uploading'"))["n"] == 2
        marker = await db.fetchone("SELECT name FROM sqlite_master WHERE name='schema_migrations'")
        assert marker is None or not await db.fetchone("SELECT version FROM schema_migrations WHERE version='1.0.12'")
    assert Path(str(database.DB_PATH) + ".pre-v112.sqlite3").is_file()
