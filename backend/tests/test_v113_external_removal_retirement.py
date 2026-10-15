"""DP 1.0.13: a download client's "remove" of work only it needed takes that
transfer out of the ordinary Downloads and Dashboard lists.

An *arr removal of an unfinished job withdraws that job's own demand; the core
cancels work nobody needs any more. The CANCELLED transfer then stayed in
the ordinary lists (they hide only ``deleted`` and ``consolidated``). The
adapters now ask the canonical owner to also retire a transfer this demand
served once it is cancelled and needed by nobody, through the existing
operator delete -- its soft-delete tombstone (history, provenance and every
demand row stay), authorized against NO outstanding demand and checked in
its own commit -- with nothing removed at a provider and only DebridPulse's
own incomplete material reclaimed. Shared work, completed deliveries and
the native external API's cancellation-only withdraw are unchanged.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import db.database as database
from api import operational_downloads as downloads
from test_access_control import MAGNET, _status, _submit, core  # noqa: F401 - fixture
from test_arr_sabnzbd_adapter import (  # noqa: F401 - fixtures
    API, call, fail, history, queue, sab, sab_principal, shared_magnet, submitted)
from test_external_client_foundation import body, demands, http, transfer_of, world  # noqa: F401 - fixtures
from transfers.models import TransferState

pytestmark = pytest.mark.asyncio


async def ordinary_ids(world) -> set[int]:
    """The ids the ordinary Downloads list (and the Dashboard, by the same
    clauses) shows with no status filter."""
    listed = await downloads.list_operational_torrents(
        status=None, search=None, limit=0, offset=0,
        application=SimpleNamespace(repository=world.repository, definitions=[]))
    return {int(item["id"]) for item in listed["items"]}


async def remove(sab, nzo_id):
    response = await call(sab, "queue", name="delete", value=nzo_id, del_files="1")
    assert response.json() == {"status": True, "nzo_ids": [nzo_id]}, response.text


async def journal(transfer_id) -> list[str]:
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT event_type FROM event_journal WHERE subject_id=? ORDER BY id",
                                 (str(transfer_id),))
    return [row["event_type"] for row in rows]


async def test_removing_work_only_this_client_needed_retires_it_from_the_ordinary_lists(sab, world):
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    await world.engine.tick()
    assert transfer_id in await ordinary_ids(world)
    await remove(sab, nzo_id)
    assert (await world.repository.get(transfer_id)).state == TransferState.DELETED
    assert transfer_id not in await ordinary_ids(world)
    assert await queue(sab) == [] and await history(sab) == []
    # The soft delete keeps history: the transfer row, the demand (withdrawn
    # by this client), and the journal of both acts.
    [demand] = await demands(transfer_id)
    assert demand["withdrawn_at"] is not None and demand["withdrawn_by"].startswith("external:")
    events = await journal(transfer_id)
    assert any("cancel" in event for event in events) and any("delet" in event for event in events), events
    # A repeated removal is idempotent: same answer, nothing more happens.
    await remove(sab, nzo_id)
    assert (await world.repository.get(transfer_id)).state == TransferState.DELETED
    assert await journal(transfer_id) == events


async def test_shared_work_stays_until_its_last_client_removes_it(sab, world):
    mine, theirs, transfer_id = await shared_magnet(world, sab)
    await remove(sab, mine)
    assert (await world.repository.get(transfer_id)).state not in {TransferState.CANCELLED, TransferState.DELETED}
    assert transfer_id in await ordinary_ids(world)
    assert [row["withdrawn_at"] is None for row in await demands(transfer_id)].count(True) == 1
    # The other client's own removal is the last one.
    await world.external.withdraw(world.clients.r, theirs, retire_unclaimed=True)
    assert (await world.repository.get(transfer_id)).state == TransferState.DELETED
    assert transfer_id not in await ordinary_ids(world)


async def test_a_failed_item_keeps_its_failure_visible(sab, world):
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    await fail(world, transfer_id)
    response = await call(sab, "history", name="delete", value=nzo_id, del_files="1")
    assert response.json()["status"] is True
    assert await history(sab) == []
    # The core's failure policy keeps an unneeded FAILED transfer FAILED (only
    # an operator moves it on), so nothing is cancelled and nothing retired:
    # the failure stays for the operator to inspect.
    assert (await world.repository.get(transfer_id)).state == TransferState.FAILED
    assert transfer_id in await ordinary_ids(world)
    assert [row["withdrawn_at"] is not None for row in await demands(transfer_id)] == [True]


async def test_a_demand_arriving_between_cancel_and_retirement_keeps_the_transfer(sab, world, monkeypatch):
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    original = world.engine.delete

    async def racing(target, **kwargs):
        # Another claim on this transfer commits after the cancel and before
        # the retirement's own transaction.
        async with database.get_db() as db:
            await db.execute("INSERT INTO transfer_demands(id,transfer_id,account_id,origin,created_at) "
                             "VALUES('racing-demand',?,'someone','submission',1)", (target,))
            await db.commit()
        return await original(target, **kwargs)

    monkeypatch.setattr(world.engine, "delete", racing)
    await remove(sab, nzo_id)
    # The retirement was refused at its commit; the transfer stays listed.
    assert (await world.repository.get(transfer_id)).state == TransferState.CANCELLED
    assert transfer_id in await ordinary_ids(world)


async def test_a_completed_delivery_is_acknowledged_never_retired(sab, world):
    from test_external_client_foundation import complete
    nzo_id = await submitted(sab)
    transfer_id = await transfer_of(nzo_id)
    await complete(world, transfer_id)
    response = await call(sab, "history", name="delete", value=nzo_id, del_files="1")
    assert response.json()["status"] is True
    assert (await world.repository.get(transfer_id)).state == TransferState.COMPLETED
    assert transfer_id in await ordinary_ids(world)
    assert [row["withdrawn_at"] for row in await demands(transfer_id)] == [None]


async def test_the_native_external_withdraw_stays_cancellation_only(sab, world):
    job, _ = await world.external.submit(sab_principal(world, sab), body("native-only"))
    transfer_id = await transfer_of(job["id"])
    await world.external.withdraw(sab_principal(world, sab), job["id"])
    assert (await world.repository.get(transfer_id)).state == TransferState.CANCELLED
    assert transfer_id in await ordinary_ids(world)


async def test_consolidated_work_is_retired_only_with_its_last_demand_anywhere(core):  # noqa: F811
    repository, _engine, application = core
    canonical, alice = await _submit(repository, "alice")
    contributor, bob = await _submit(repository, "bob", payload=MAGNET.replace("b" * 40, "c" * 40))
    async with database.get_db() as db:
        primary = await db.execute_returning_id(
            "INSERT INTO download_files(torrent_id,filename,status) VALUES(?,'shared.bin','pending')", (canonical,))
        standby = await db.execute_returning_id(
            "INSERT INTO download_files(torrent_id,filename,status) VALUES(?,'shared.bin','duplicate')", (contributor,))
        request = (await db.fetchone("SELECT id FROM transfer_requests WHERE transfer_id=?", (contributor,)))["id"]
        await db.execute(
            "INSERT INTO artifact_consolidations(contributing_artifact_id,source_transfer_id,source_request_id,"
            "canonical_artifact_id) VALUES(?,?,?,?)", (standby, contributor, request, primary))
        await db.commit()
    # Bob's demand reaches the canonical transfer through consolidation.
    assert await repository.served_transfers((bob,)) == sorted([canonical, contributor])
    result = await application.withdraw_demand((alice,), by="external:alice", retire_unclaimed=True)
    assert result["retired_transfers"] == []
    assert await _status(repository, canonical) not in {TransferState.CANCELLED, TransferState.DELETED}
    result = await application.withdraw_demand((bob,), by="external:bob", retire_unclaimed=True)
    assert sorted(result["retired_transfers"]) == sorted([canonical, contributor])
    assert await _status(repository, canonical) is TransferState.DELETED
    assert await _status(repository, contributor) is TransferState.DELETED
