"""Upstream preparation survives a root-route replacement (1.0.13, transfer 677).

Transfer 677: provider A created the root's torrent (``PREPARING``, a cache
miss); an operator switch moved the route to B's prepared backup, and in the
same commit ``replace_root_route`` gave A's torrent back (``resource
.cleanup_completed``, ``absent``) -- only because A lost the route.

Route authority and resource ownership are separate. The replaced provider's
own Prepare Backup Torrents policy (``speculative_preparation_allowed``, off
by default) decides, for an operator switch and a readiness yield alike:

* ON: A's resource is kept as A's backup of the root -- a new generation of
  its (root, provider) preparation bound to the SAME binding, in the
  replacement's commit. It is observed, shown, taken over again and
  reclaimed exactly as any backup, never created again; the released route
  still fences any NEW preparation by A.
* OFF: A's resource is given back through the existing owned cleanup, as
  before; nothing new is prepared.

Turning the policy off later gives a kept (or any) backup back through the
one reclamation. Every provider is a neutral local fixture; the policy is set
on it as each provider's own option answers it.
"""
from __future__ import annotations

import pytest
from test_v113_readiness_promotion import POLL, history, preparing, set_state
from test_v113_root_provider_switch import MAGNET, rows

from db.database import get_db
from transfers import manual_route_switch
from transfers.errors import TransferError
from transfers.manual_route_switch import route_providers, switch_root_provider
from transfers.models import ResourceState
from transfers.recovery_repository import TransferRepository

pytestmark = pytest.mark.asyncio


def backups(provider, on: bool):
    """``provider``'s Prepare Backup Torrents, as its own option answers it."""
    provider.speculative_preparation_allowed = lambda request: on


async def lab_on(tmp_path, monkeypatch, *, a_on=True):
    """Transfer 677's shape: root on parcel-a (CREATED, PREPARING), parcel-b
    holding a prepared backup."""
    lab = await preparing(tmp_path, monkeypatch, "parcel-a", "parcel-b", "parcel-c")
    backups(lab.providers["parcel-a"], a_on)
    backups(lab.providers["parcel-b"], True)                            # B holds a backup: B's policy is on
    standby_id, backup = await lab.backup("parcel-b")
    return lab, standby_id, backup


async def binding(lab, resource):
    (row,) = await rows("SELECT id,state,cleanup_authority,cleanup_attempts FROM provider_resources "
                        "WHERE transfer_id=? AND resource_key=?", (lab.transfer.id, resource.id))
    return row


def calls(lab, identity, kind):
    """``identity``'s calls of ``kind``; a ``resolve`` counts only the root's
    own creation (members legitimately resolve on whichever route holds them)."""
    return [call for call in lab.providers[identity].calls
            if call[0] == kind and (kind != "resolve" or call[1] == MAGNET)]


async def kept(lab, identity="parcel-a"):
    return [item for item in await lab.repository.standbys(lab.transfer.id)
            if item["provider_id"] == identity and item["promoted_at"] is None and item["state"] == "bound"]


async def move(lab, how, target="parcel-b"):
    """A -> ``target`` by an operator switch, or by the readiness yield."""
    if how == "operator_switch":
        await switch_root_provider(lab.engine, lab.transfer.id, target, expected_provider_id="parcel-a")
        await lab.tick(times=2)
    else:
        await lab.tick(POLL)
    assert (await lab.root()).resource.provider_id == target


async def status(lab, identity):
    providers = (await route_providers(lab.engine, lab.transfer.id))["providers"]
    return next((entry["status"], entry["selectable"]) for entry in providers if entry["provider_id"] == identity)


# -- 1 / 3 / 7: ON keeps the former primary, for a switch and a yield alike ----------------------------------------

@pytest.mark.parametrize("how", ["operator_switch", "readiness_promotion"])
async def test_on_the_replaced_primary_is_kept_as_its_providers_backup(tmp_path, monkeypatch, how):
    lab, _standby, backup = await lab_on(tmp_path, monkeypatch)
    resolves = len(calls(lab, "parcel-a", "resolve"))
    await move(lab, how)
    root = await lab.root()
    assert root.resource.id == backup.id
    # The exact upstream object: still bound, no cleanup authority, never removed.
    row = await binding(lab, lab.primary)
    assert (row["state"], row["cleanup_authority"], row["cleanup_attempts"]) == ("preparing", None, 0)
    assert not calls(lab, "parcel-a", "cleanup")
    # A's backup of the root, bound to that same binding (one owner, one object).
    (item,) = await kept(lab)
    assert item["binding_id"] == row["id"] and item["resource"].id == lab.primary.id
    assert [event["event_type"] for event in await rows(
        "SELECT event_type FROM event_journal WHERE subject_id=?", (item["id"],))] == ["resource.standby_retained"]
    # History is truthful: A's route was released, B's opened by this operation.
    states = [(entry["provider_id"], entry["state"], entry["operation"]) for entry in await history(root.id)]
    assert states[0][:2] == ("parcel-a", "released") and states[-1][2] == how
    # Shown for what it is: still preparing, so not a target yet.
    assert await status(lab, "parcel-a") == (manual_route_switch.PREPARING, False)
    # Observed, never created again: the released route fences any new preparation.
    await lab.tick(POLL, times=3)
    assert len(calls(lab, "parcel-a", "resolve")) == resolves
    assert len(await kept(lab)) == 1 and calls(lab, "parcel-a", "observe")
    assert "parcel-a" in await lab.repository.standby_fenced_providers(lab.transfer.id, root.id)


@pytest.mark.parametrize("how", ["operator_switch", "readiness_promotion"])
async def test_off_the_replaced_primary_is_given_back_and_nothing_new_is_prepared(tmp_path, monkeypatch, how):
    lab, _standby, _backup = await lab_on(tmp_path, monkeypatch, a_on=False)
    await move(lab, how)
    await lab.tick(POLL, times=2)
    row = await binding(lab, lab.primary)
    assert row["state"] == "absent" and len(calls(lab, "parcel-a", "cleanup")) == 1   # once, the one owner
    assert not await kept(lab)
    assert not calls(lab, "parcel-b", "resolve") and not calls(lab, "parcel-c", "resolve")  # nothing speculative
    assert len(await lab.repository.standbys(lab.transfer.id)) == 1                  # only B's, now promoted


# -- 2: back to A with the object A already holds, only once it is ready ---------------------------------------------

async def test_a_kept_primary_is_taken_over_again_without_a_second_creation(tmp_path, monkeypatch):
    lab, _standby, backup = await lab_on(tmp_path, monkeypatch)
    await move(lab, "operator_switch")
    resolves = len(calls(lab, "parcel-a", "resolve"))
    # Still preparing: switching back is refused exactly as for any preparing backup.
    with pytest.raises(TransferError):
        await switch_root_provider(lab.engine, lab.transfer.id, "parcel-a", expected_provider_id="parcel-b")
    assert (await lab.root()).resource.id == backup.id
    set_state(lab.providers["parcel-a"], lab.primary, ResourceState.AVAILABLE)
    await lab.tick(POLL)                                                # its next observation: ready
    assert await status(lab, "parcel-a") == (manual_route_switch.PREPARED, True)
    await switch_root_provider(lab.engine, lab.transfer.id, "parcel-a", expected_provider_id="parcel-b")
    await lab.tick(times=2)
    root = await lab.root()
    assert root.resource.id == lab.primary.id                          # the very object A created first
    assert len(calls(lab, "parcel-a", "resolve")) == resolves           # never created again
    assert len(await rows("SELECT 1 FROM provider_resources WHERE transfer_id=? AND provider_id='parcel-a'",
                          (lab.transfer.id,))) == 1                     # one binding
    assert not await kept(lab)                                          # taken over: provenance now
    assert (await binding(lab, lab.primary))["cleanup_authority"] is None


# -- 4: capacity pressure and remote absence end a kept backup; it is never prepared again -------------------------

async def test_primary_work_reclaims_a_kept_backup_once_and_it_is_never_recreated(tmp_path, monkeypatch):
    lab, _standby, _backup = await lab_on(tmp_path, monkeypatch)
    await move(lab, "operator_switch")
    (item,) = await kept(lab)
    held = await lab.repository.reclaimable_standbys("parcel-a")        # the existing reclaim owner sees it
    assert [entry["id"] for entry in held] == [item["id"]]
    assert await lab.engine._reclaim(held) == 1                         # primary work takes the slot back
    assert (await binding(lab, lab.primary))["state"] == "absent"
    assert len(calls(lab, "parcel-a", "cleanup")) == 1
    resolves = len(calls(lab, "parcel-a", "resolve"))
    await lab.tick(POLL, times=3)
    assert len(calls(lab, "parcel-a", "resolve")) == resolves           # the released-route fence holds
    assert len(calls(lab, "parcel-a", "cleanup")) == 1                  # cleaned once
    assert not await kept(lab)


async def test_a_kept_backup_the_provider_dropped_is_released_and_never_recreated(tmp_path, monkeypatch):
    lab, _standby, _backup = await lab_on(tmp_path, monkeypatch)
    await move(lab, "operator_switch")
    lab.providers["parcel-a"].resources.pop(lab.primary.id)             # gone at the provider
    resolves = len(calls(lab, "parcel-a", "resolve"))
    await lab.tick(POLL, times=3)
    assert not await kept(lab)
    assert len(calls(lab, "parcel-a", "resolve")) == resolves
    assert await status(lab, "parcel-a") != (manual_route_switch.PREPARED, True)


# -- 7: ON -> OFF gives a kept backup back; the current primary is untouched ------------------------------------------

async def test_turning_the_policy_off_gives_a_kept_backup_back(tmp_path, monkeypatch):
    lab, _standby, backup = await lab_on(tmp_path, monkeypatch)
    await move(lab, "operator_switch")
    assert await kept(lab)
    backups(lab.providers["parcel-a"], False)
    await lab.tick(POLL)
    assert not await kept(lab)
    assert (await binding(lab, lab.primary))["state"] == "absent"
    assert len(calls(lab, "parcel-a", "cleanup")) == 1
    root = await lab.root()
    assert root.resource.id == backup.id                                # the current primary is untouched
    assert (await binding(lab, backup))["cleanup_authority"] is None
    resolves = len(calls(lab, "parcel-a", "resolve"))
    await lab.tick(POLL, times=2)
    assert len(calls(lab, "parcel-a", "resolve")) == resolves


# -- 5: a restart reconstructs the same ownership ------------------------------------------------------------------

async def test_a_restart_keeps_the_same_route_and_the_same_kept_backup(tmp_path, monkeypatch):
    lab, _standby, backup = await lab_on(tmp_path, monkeypatch)
    await move(lab, "operator_switch")
    (before,) = await kept(lab)
    resolves = len(calls(lab, "parcel-a", "resolve"))
    lab.engine.repository = lab.repository = TransferRepository()      # a new process over the same database
    await lab.engine.initialize()
    await lab.tick(POLL, times=2)
    (after,) = await kept(lab)
    assert (after["id"], after["binding_id"]) == (before["id"], before["binding_id"])
    assert (await lab.root()).resource.id == backup.id
    assert len(calls(lab, "parcel-a", "resolve")) == resolves and not calls(lab, "parcel-a", "cleanup")


# -- 6: a refused replacement changes nothing ------------------------------------------------------------------------

async def test_a_refused_replacement_keeps_nothing_and_gives_nothing_back(tmp_path, monkeypatch):
    lab, _standby, _backup = await lab_on(tmp_path, monkeypatch)
    root = await lab.root()
    latest = await lab.repository.latest_root_route(root.id)
    outcome = await lab.repository.replace_root_route(
        root.id, expected_attempt_id="not-the-route", expected_provider_id="parcel-a",
        target_provider_id="parcel-b", retain_replaced=True, now=lab.engine.clock())
    assert outcome == "stale"
    assert not await kept(lab)
    assert (await binding(lab, lab.primary))["cleanup_authority"] is None
    assert (await lab.repository.latest_root_route(root.id))["id"] == latest["id"]
    with pytest.raises(TransferError):                                  # the operator saw another provider
        await switch_root_provider(lab.engine, lab.transfer.id, "parcel-b", expected_provider_id="parcel-c")
    assert not await kept(lab) and not (await lab.repository.get(lab.transfer.id)).paused


async def test_a_provider_with_a_live_preparation_never_gets_a_second_owner(tmp_path, monkeypatch):
    """One live backup per root and provider: where A already has one, the
    replaced resource is given back as before instead of a second owner."""
    lab, _standby, _backup = await lab_on(tmp_path, monkeypatch)
    root = await lab.root()
    async with get_db() as db:
        await db.execute("INSERT INTO standby_resources(id,transfer_id,request_id,provider_id,generation,state,"
                         "created_at,updated_at) VALUES('live-a',?,?,'parcel-a',1,'creating',0,0)",
                         (lab.transfer.id, root.id))
        await db.commit()
    await move(lab, "operator_switch")
    assert not await kept(lab)
    row = await binding(lab, lab.primary)
    assert row["cleanup_authority"] is not None or row["state"] == "absent"


# -- 4: a terminal disposition still owns a kept backup's cleanup ----------------------------------------------------

async def test_deleting_the_transfer_cleans_a_kept_backup_up(tmp_path, monkeypatch):
    lab, _standby, backup = await lab_on(tmp_path, monkeypatch)
    await move(lab, "operator_switch")
    assert await kept(lab)
    await lab.engine.delete(lab.transfer.id, remote=True, owned_cleanup=True)
    await lab.engine._cleanup_pending()
    assert (await binding(lab, lab.primary))["state"] == "absent"
    assert len(calls(lab, "parcel-a", "cleanup")) == 1
    assert (await binding(lab, backup))["state"] == "absent"
