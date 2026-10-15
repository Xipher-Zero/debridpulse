"""Terminal resource ownership (1.0.13): a transfer's end decides what is
owed at every provider holding work for it, and inventory never brings
retired work back.

* An operator Delete with remote removal owes every resource of the transfer
  -- the route's and each prepared backup's -- a USER_REQUEST cleanup; the
  final external withdrawal (an *arr removal of work only it needed) owes
  every resource DebridPulse CREATED or ADOPTED an OWNED cleanup, never one it
  only observed; a Delete that kept remote resources owes nothing.
* A backup whose creation answers after its transfer ended is bound with that
  end's cleanup in the same transaction (never left to escape it).
* Inventory listing a resource a retired transfer bound is evidence of that
  resource, never new demand: nothing is admitted -- not on the first pass,
  not on any later one -- and a deleted transfer's resource that is still
  listed is owed its Delete's cleanup again. Its NATIVE identity decides,
  never its content hash: a fresh explicit submission of the same content is
  admitted, unrelated inventory is still imported, and a resource bound
  between the inventory read and its admission is refused inside the
  admission's own transaction.

Every provider is a local neutral fake.
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from test_v113_readiness_promotion import POLL, offer, preparing, rows
from test_v113_root_provider_switch import MAGNET

from transfers._repository_base import DemandChanged, KnownProviderResource
from transfers.models import (
    CleanupAuthority, Ownership, ProviderObservation, ProviderResource, ResourceState, TransferRequest,
)

pytestmark = pytest.mark.asyncio


def cleanups(provider):
    return [(directive.resource.id, directive.authority) for name, directive in provider.calls if name == "cleanup"]


async def with_backup(tmp_path, monkeypatch, *, ownership=Ownership.CREATED):
    """A root bound to parcel-a (still preparing) with a parcel-b backup that
    is still preparing too, so nothing is promoted."""
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b", ownership=ownership)
    _standby, backup = await lab.backup("parcel-b", ResourceState.PREPARING)
    return lab, backup


async def withdraw(lab):
    """The final external withdrawal exactly as the adapters drive it
    (``ApplicationService.withdraw_demand(retire_unclaimed=True)``): every
    demand withdrawn -- the core cancels the work nobody needs -- then the
    canonical delete authorized against NO outstanding demand."""
    demands = tuple(row["id"] for row in await rows("SELECT id FROM transfer_demands WHERE transfer_id=?",
                                                     (lab.transfer.id,)))
    await lab.engine.withdraw_demand(demands, by="external-client")
    await lab.engine.delete(lab.transfer.id, remote=False, expected_demand=frozenset(), owned_cleanup=True)


async def test_an_operator_delete_removes_the_route_and_every_backup(tmp_path, monkeypatch):
    lab, backup = await with_backup(tmp_path, monkeypatch)
    await lab.engine.delete(lab.transfer.id, remote=True)
    assert cleanups(lab.providers["parcel-a"]) == [(lab.primary.id, CleanupAuthority.USER_REQUEST)]
    assert cleanups(lab.providers["parcel-b"]) == [(backup.id, CleanupAuthority.USER_REQUEST)]


async def test_a_delete_that_keeps_remote_resources_owes_nothing(tmp_path, monkeypatch):
    lab, _backup = await with_backup(tmp_path, monkeypatch)
    await lab.engine.delete(lab.transfer.id, remote=False)
    await lab.engine.cleanup_pending()
    assert cleanups(lab.providers["parcel-a"]) == cleanups(lab.providers["parcel-b"]) == []


async def test_the_final_external_withdrawal_gives_back_every_resource_dp_created(tmp_path, monkeypatch):
    lab, backup = await with_backup(tmp_path, monkeypatch)
    await withdraw(lab)
    assert cleanups(lab.providers["parcel-a"]) == [(lab.primary.id, CleanupAuthority.OWNED)]
    assert cleanups(lab.providers["parcel-b"]) == [(backup.id, CleanupAuthority.OWNED)]


async def test_an_observed_resource_is_never_removed_by_a_withdrawal(tmp_path, monkeypatch):
    lab, backup = await with_backup(tmp_path, monkeypatch, ownership=Ownership.OBSERVED)
    await withdraw(lab)
    assert cleanups(lab.providers["parcel-a"]) == []                       # it only observed the route's
    assert cleanups(lab.providers["parcel-b"]) == [(backup.id, CleanupAuthority.OWNED)]


async def test_a_withdrawal_refused_by_remaining_demand_removes_nothing(tmp_path, monkeypatch):
    lab, _backup = await with_backup(tmp_path, monkeypatch)
    async with __import__("db.database", fromlist=["get_db"]).get_db() as db:
        await db.execute("INSERT INTO transfer_demands(id,transfer_id,account_id,origin,created_at) "
                         "VALUES('another-claimant',?,'someone','submission',1)", (lab.transfer.id,))
        await db.commit()
    with pytest.raises(DemandChanged):
        await lab.engine.delete(lab.transfer.id, remote=False, expected_demand=frozenset(), owned_cleanup=True)
    await lab.engine.cleanup_pending()
    assert cleanups(lab.providers["parcel-a"]) == cleanups(lab.providers["parcel-b"]) == []
    assert (await lab.repository.get(lab.transfer.id)).state != "deleted"


@pytest.mark.parametrize("remote, owned, authority", [
    (True, False, CleanupAuthority.USER_REQUEST), (False, True, CleanupAuthority.OWNED), (False, False, None)])
async def test_a_backup_created_after_the_transfer_ended_is_owed_that_end(tmp_path, monkeypatch, remote, owned,
                                                                          authority):
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    root = await lab.root()
    standby_id, _attempts = await lab.repository.begin_standby(lab.transfer.id, root.id, "parcel-b",
                                                               lab.engine.clock())
    await (withdraw(lab) if owned else lab.engine.delete(lab.transfer.id, remote=remote))
    late = offer(lab.providers["parcel-b"], "late", ResourceState.PREPARING)   # its creation answers now
    await lab.repository.bind_standby(standby_id, lab.transfer.id, late, ResourceState.PREPARING,
                                      lab.engine.clock())
    await lab.engine.cleanup_pending()
    assert cleanups(lab.providers["parcel-b"]) == ([(late.id, authority)] if authority else [])


async def test_no_backup_is_prepared_where_the_route_was_released_or_cleanup_is_owed(tmp_path, monkeypatch):
    """The fence: a provider whose route of this root was given back (an
    operator switch) is not re-prepared as a backup -- which re-bound and
    could re-create the very resource its cleanup had just removed."""
    from transfers.manual_route_switch import switch_root_provider

    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b")
    root = await lab.root()
    offer(lab.providers["parcel-b"], "chosen", ResourceState.PREPARING)
    await switch_root_provider(lab.engine, lab.transfer.id, "parcel-b", expected_provider_id="parcel-a")
    await lab.tick(times=2)
    fenced = await lab.repository.standby_fenced_providers(lab.transfer.id, root.id)
    assert "parcel-a" in fenced
    for _ in range(3):
        await lab.tick(POLL)
    assert await rows("SELECT * FROM standby_resources WHERE transfer_id=? AND provider_id='parcel-a'",
                      (lab.transfer.id,)) == []


# -- inventory never resurrects retired work ----------------------------------------------------------------------

def listing(resource, state=ResourceState.AVAILABLE, *, request=None):
    request = request or TransferRequest("magnet", MAGNET, name="Show")
    return ProviderObservation(
        ProviderResource(resource.provider_id, dict(resource.context), Ownership.OBSERVED, id=resource.id),
        state, request.name, request=request)


async def inventory_imports(lab):
    return [transfer for transfer in await lab.repository.active() if transfer.source == "inventory"]


@pytest.mark.parametrize("remote, owned", [(True, False), (False, True)], ids=["operator-delete", "withdrawal"])
async def test_a_deleted_transfer_s_resource_still_listed_is_cleaned_again_never_reimported(
        tmp_path, monkeypatch, remote, owned):
    lab, backup = await with_backup(tmp_path, monkeypatch)
    provider = lab.providers["parcel-a"]
    await (withdraw(lab) if owned else lab.engine.delete(lab.transfer.id, remote=remote))
    assert len(cleanups(provider)) == 1
    # Its provider answered the removal, yet still lists it (an unconfirmed
    # delete): every inventory pass sees it again.
    provider.inventory_items = (listing(lab.primary), listing(backup))
    lab.providers["parcel-b"].inventory_items = (listing(backup),)
    for _ in range(3):
        await lab.engine.reconcile_inventory()
        await lab.engine.cleanup_pending()
        assert await inventory_imports(lab) == []                          # never a ghost, on any pass
    expected = CleanupAuthority.USER_REQUEST if remote else CleanupAuthority.OWNED
    assert cleanups(provider) == [(lab.primary.id, expected)] * 4          # owed again each time it is listed
    assert set(cleanups(lab.providers["parcel-b"])) == {(backup.id, expected)}


async def test_a_kept_transfer_s_resource_is_never_reimported_and_never_removed(tmp_path, monkeypatch):
    lab, _backup = await with_backup(tmp_path, monkeypatch)
    await lab.engine.delete(lab.transfer.id, remote=False)               # the operator kept it remotely
    lab.providers["parcel-a"].inventory_items = (listing(lab.primary),)
    for _ in range(2):
        await lab.engine.reconcile_inventory()
        await lab.engine.cleanup_pending()
    assert await inventory_imports(lab) == []
    assert cleanups(lab.providers["parcel-a"]) == []


async def test_unrelated_inventory_is_still_imported(tmp_path, monkeypatch):
    lab, _backup = await with_backup(tmp_path, monkeypatch)
    await lab.engine.delete(lab.transfer.id, remote=True)
    other = offer(lab.providers["parcel-a"], "unrelated", ResourceState.AVAILABLE)
    other_magnet = TransferRequest("magnet", "magnet:?xt=urn:btih:" + "d" * 40, name="Other", fingerprint="d" * 40)
    lab.providers["parcel-a"].inventory_items = (listing(lab.primary), listing(other, request=other_magnet))
    await lab.engine.reconcile_inventory()
    imported = await inventory_imports(lab)
    assert [transfer.name for transfer in imported] == ["Other"]


async def test_a_fresh_explicit_submission_of_the_same_content_is_admitted(tmp_path, monkeypatch):
    lab, _backup = await with_backup(tmp_path, monkeypatch)
    await lab.engine.delete(lab.transfer.id, remote=True)
    fresh = await lab.engine.submit((TransferRequest("magnet", MAGNET, name="Show"),), name="Show")
    assert fresh.id != lab.transfer.id
    assert (await lab.repository.get(fresh.id)).state != "deleted"


async def test_a_resource_bound_between_the_inventory_read_and_admission_is_refused_in_its_transaction(
        tmp_path, monkeypatch):
    lab, _backup = await with_backup(tmp_path, monkeypatch)
    other_magnet = TransferRequest("magnet", "magnet:?xt=urn:btih:" + "e" * 40, name="Raced", fingerprint="e" * 40)
    with pytest.raises(KnownProviderResource):
        await lab.engine.submit((other_magnet,), name="Raced", source="inventory", independent=False,
                                observed=(lab.primary, ResourceState.AVAILABLE))
    assert await inventory_imports(lab) == []

    # The engine's inventory pass: the history check found nothing, then the
    # resource was bound before admission -- the pass skips it quietly.
    unseen = replace(lab.primary, id="parcel-a:raced-binding")
    original = lab.repository.historical_binding

    async def not_yet_bound(resource_key):
        return None if resource_key == unseen.id else await original(resource_key)

    monkeypatch.setattr(lab.repository, "historical_binding", not_yet_bound)
    lab.providers["parcel-a"].inventory_items = (listing(unseen, request=other_magnet),)
    await lab.engine.delete(lab.transfer.id, remote=False)
    async with __import__("db.database", fromlist=["get_db"]).get_db() as db:
        await db.execute("UPDATE provider_resources SET resource_key=? WHERE resource_key=?",
                         (unseen.id, lab.primary.id))
        await db.commit()
    await lab.engine.reconcile_inventory()
    assert await inventory_imports(lab) == []
