"""Speculative backup preparation: durable standby ownership.

After a root's primary route has begun, each other provider still competing
for it that is entitled and allows a speculative preparation of it may
prepare it as a backup, through its ordinary ``resolve`` inside
``speculative_preparation``. The root keeps exactly its primary route,
request state and primary resource; the backup is the root's claim
(``standby_resources``) on an ordinary ``provider_resources`` binding of the
transfer, so its state, cleanup authority and inventory identity are every
resource's. A backup is never a candidate here; a refusal that only says "not
now" defers it without contracting anything, and any other failure ends only
that backup.

The primary is a neutral fixture; the backup provider is the real TorBox
adapter over its scripted account, so the operator control is exercised end
to end.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fake_integrations import MemoryExecutor
from test_v113_collection_route_generic_closure import Clock
from test_v113_torbox_provider import FakeClient, torrent

import db.database as database
from db.database import get_db
from providers.torbox.client import TORRENT, TorBoxAPIError
from providers.torbox.provider import TorBoxProvider
from services import transfer_trace
from transfers.contracts import speculative_attempt
from transfers.convergence_engine import TransferEngine
from transfers.models import (
    Capability,
    IntegrationDescriptor,
    Ownership,
    ProviderObservation,
    ProviderResource,
    ResolutionResult,
    ResourceState,
    TransferRequest,
    TransferState,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

HASH = "a" * 40          # the info-hash FakeClient's torrents carry


def magnet(digest=HASH):
    return TransferRequest("magnet", f"magnet:?xt=urn:btih:{digest}&dn=Show", name="Show", fingerprint=digest)


class Primary:
    """A neutral BitTorrent provider that accepts a root and keeps preparing it."""

    def __init__(self, identity="provider-a", priority=10):
        self.descriptor = IntegrationDescriptor(identity, identity,
                                                frozenset({Capability.RESOLVE, Capability.RESOURCE_LOOKUP}),
                                                request_types=frozenset({"magnet", "torrent", "https"}),
                                                priority=priority)
        self.resolved: list[str] = []
        self.observed = 0

    async def resolve(self, request):
        assert not speculative_attempt()                 # a primary call is never speculative
        self.resolved.append(str(request.payload))
        resource = ProviderResource(self.descriptor.id, {"n": len(self.resolved)}, Ownership.CREATED,
                                    id=f"{self.descriptor.id}:{len(self.resolved)}")
        observation = ProviderObservation(resource, ResourceState.PREPARING, "Show", request=request)
        return ResolutionResult(ResourceState.PREPARING, observation=observation)

    async def observe(self, resource):
        self.observed += 1
        return ProviderObservation(resource, ResourceState.PREPARING, "Show")


def torbox(client=None, *, on=True):
    provider = TorBoxProvider(client or FakeClient(), prepare_backup_torrents=on)
    provider.account = SimpleNamespace(contract=AsyncMock(), entitlements=None)
    return provider


async def lab(tmp_path, monkeypatch, *providers, fresh=True, clock=None, policy=None):
    if fresh:
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "standby.sqlite3")
        await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    for provider in providers:
        registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "downloads"),
                            policy=policy or TransferPolicy(retry_delay=0.0, max_attempts=3),
                            clock=clock or Clock())
    await engine.initialize()
    return repository, registry, engine


async def submitted(engine, request=None):
    return await engine.submit((request or magnet(),), name="Show", deduplicate=False)


async def root_of(repository, transfer):
    return next(item for item in await repository.requests(transfer.id) if item.parent_id is None)


def creates(client):
    return [call for call in client.calls if call[0] == "create_torrent"]


async def route_providers(transfer_id):
    async with get_db() as db:
        rows = await db.fetchall("SELECT a.provider_id FROM resolution_attempts a JOIN transfer_requests r "
                                 "ON r.id=a.request_id WHERE r.transfer_id=?", (transfer_id,))
    return [row["provider_id"] for row in rows]


# -- P3.1 / P3.2: after admission, after the primary route, never ahead of primary work -----------

async def test_p3_1_nothing_is_prepared_before_the_primary_route_begins(tmp_path, monkeypatch):
    client = FakeClient()
    primary = Primary()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, primary, torbox(client))
    transfer = await submitted(engine)

    await engine._prepare_standbys()                       # admitted, but its primary route has not begun

    assert creates(client) == [] and await repository.standbys(transfer.id) == ()
    await engine.resolve_pending()
    assert primary.resolved == [str(magnet().payload)]
    assert len(creates(client)) == 1                       # the primary first, then the backup


async def test_p3_2_a_backup_yields_to_runnable_primary_work(tmp_path, monkeypatch):
    client = FakeClient()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client))
    first = await submitted(engine)
    await engine.resolve_pending()
    assert len(creates(client)) == 1
    await submitted(engine, magnet("b" * 40))              # primary work arrives and is runnable

    await engine._prepare_standbys()

    assert len(creates(client)) == 1                       # yielded: no backup ahead of it
    await engine.resolve_pending()
    assert len(creates(client)) == 2 and len(await repository.standbys(first.id)) == 1


# -- P3.3 / T3.4 / T3.5: fail-closed, OFF, ON -------------------------------------------------------

@pytest.mark.parametrize("on", [False, True])
async def test_t3_4_t3_5_the_torbox_control_decides_whether_a_backup_is_made(tmp_path, monkeypatch, on):
    client = FakeClient()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client, on=on))
    transfer = await submitted(engine)

    for _ in range(3):
        await engine.resolve_pending()

    assert len(creates(client)) == (1 if on else 0)
    assert len(await repository.standbys(transfer.id)) == (1 if on else 0)


async def test_p3_3_a_provider_without_the_contract_is_never_asked(tmp_path, monkeypatch):
    other = Primary("provider-b", priority=0)                  # competes, offers no speculative contract
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), other)
    transfer = await submitted(engine)
    for _ in range(3):
        await engine.resolve_pending()
    assert other.resolved == [] and await repository.standbys(transfer.id) == ()


async def test_a_hoster_root_is_never_prepared_as_a_backup(tmp_path, monkeypatch):
    client = FakeClient()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client))
    transfer = await submitted(engine, TransferRequest("https", "https://hoster.example/f/1", name="f"))
    for _ in range(3):
        await engine.resolve_pending()
    assert not any(call[0] in {"create_torrent", "create_webdl", "create_webdl_cached"} for call in client.calls)
    assert await repository.standbys(transfer.id) == ()


# -- P3.4: one subordinate resource, the root untouched -----------------------------------------------

async def test_p3_4_an_eligible_alternate_prepares_one_subordinate_resource(tmp_path, monkeypatch):
    client = FakeClient()
    primary = Primary()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, primary, torbox(client))
    transfer = await submitted(engine)
    await engine.resolve_pending()

    root = await root_of(repository, transfer)
    (standby,) = await repository.standbys(transfer.id)
    assert (standby["state"], standby["provider_id"], standby["request_id"]) == ("bound", "torbox", root.id)
    assert standby["resource"].provider_id == "torbox" and standby["resource"].ownership == Ownership.CREATED
    assert standby["resource_state"] == ResourceState.PREPARING.value
    # The root keeps exactly its primary: route, request state and resource.
    assert await repository.bound_route_provider(root.id) == "provider-a"
    assert root.state == "waiting" and root.resource.provider_id == "provider-a"
    assert await route_providers(transfer.id) == ["provider-a"]          # no route attempt for the backup
    resources = {resource.provider_id for resource, _state, _pending in await repository.resources(transfer.id)}
    assert resources == {"provider-a", "torbox"}                        # one logical transfer, two resources
    assert len(await repository.active()) == 1
    # Never a candidate.
    assert all(candidate.provider_id != "torbox" for artifact in await repository.artifacts(transfer.id)
               for candidate in artifact.candidates)


# -- P3.5 / P3.6: no duplicate, restart reconciles ----------------------------------------------------

async def test_p3_5_concurrent_and_repeated_passes_never_create_twice(tmp_path, monkeypatch):
    client = FakeClient()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client))
    transfer = await submitted(engine)
    await engine.resolve_pending()
    await asyncio.gather(*(engine._prepare_standbys() for _ in range(5)))
    for _ in range(3):
        await engine.resolve_pending()
    assert len(creates(client)) == 1 and len(await repository.standbys(transfer.id)) == 1


async def test_p3_6_restart_observes_the_known_backup_and_never_creates_another(tmp_path, monkeypatch):
    clock = Clock()
    client = FakeClient()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client), clock=clock)
    transfer = await submitted(engine)
    await engine.resolve_pending()
    (standby,) = await repository.standbys(transfer.id)
    native = standby["resource"].id
    client.objects[TORRENT][str(client.next_id)].update(download_state="cached", download_present=True,
                                                         progress=1.0)

    clock.now += 3_600
    restarted, _registry, restarted_engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client),
                                                      fresh=False, clock=clock)
    await restarted_engine.resolve_pending()

    assert len(creates(client)) == 1                                     # observed, never created again
    (after,) = await restarted.standbys(transfer.id)
    assert after["id"] == standby["id"] and after["resource_state"] == ResourceState.AVAILABLE.value
    assert after["resource"].id == native


async def test_p3_6_an_interrupted_create_is_reconciled_by_adopting_never_by_creating_again(tmp_path, monkeypatch):
    client = FakeClient()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client))
    transfer = await submitted(engine)
    await engine._resolve(await root_of(repository, transfer))      # primary only; no backup pass yet
    root = await root_of(repository, transfer)
    # The backup's create reached TorBox, then the process died before binding it.
    assert await repository.begin_standby(transfer.id, root.id, "torbox", engine.clock()) is not None
    client.objects[TORRENT]["900"] = torrent(native_id=900)
    client.refusal = TorBoxAPIError("DUPLICATE_ITEM", "already added", 409)

    restarted, _registry, restarted_engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client),
                                                      fresh=False)
    await restarted_engine._prepare_standbys()

    (standby,) = await restarted.standbys(transfer.id)
    assert standby["state"] == "bound" and standby["resource"].ownership == Ownership.ADOPTED
    assert list(client.objects[TORRENT]) == ["900"]                      # still one remote torrent


# -- P3.7 / P3.8: isolation ----------------------------------------------------------------------------

@pytest.mark.parametrize("refusal", ["ACTIVE_LIMIT", "RATE_LIMIT", "COOLDOWN_LIMIT", "PLAN_RESTRICTED_FEATURE"])
async def test_p3_7_a_limit_class_refusal_is_non_contracting(tmp_path, monkeypatch, refusal):
    client = FakeClient()
    backup = torbox(client)
    primary = Primary()
    repository, registry, engine = await lab(tmp_path, monkeypatch, primary, backup)
    transfer = await submitted(engine)
    client.refusal = TorBoxAPIError(refusal, "limit", 429 if refusal == "RATE_LIMIT" else 403)

    await engine.resolve_pending()

    (standby,) = await repository.standbys(transfer.id)
    assert standby["state"] in {"deferred", "failed"} and standby["binding_id"] is None
    backup.account.contract.assert_not_awaited()                         # entitlement never contracted
    assert "torbox" not in registry._unhealthy                           # never unhealthy
    root = await root_of(repository, transfer)
    assert await repository.exhausted_route_providers(root.id) == frozenset()
    assert root.state == "waiting" and root.error is None                # the healthy primary is untouched
    assert (await repository.get(transfer.id)).state not in {TransferState.FAILED}
    if refusal != "PLAN_RESTRICTED_FEATURE":
        assert standby["state"] == "deferred"
        client.refusal = None
        await engine.resolve_pending()                                   # deferral elapsed (retry_delay 0)
        assert (await repository.standbys(transfer.id))[0]["state"] == "bound"


async def test_p3_7_the_same_refusal_on_a_primary_still_contracts():
    backup = torbox(FakeClient())
    await backup._refused(TorBoxAPIError("PLAN_RESTRICTED_FEATURE", "", 403), "magnet")
    backup.account.contract.assert_awaited_once()


async def test_p3_7_a_deferred_backup_backs_off_within_the_existing_retry_policy(tmp_path, monkeypatch):
    clock = Clock()
    client = FakeClient()
    policy = TransferPolicy(retry_delay=5.0, max_retry_delay=300.0, max_attempts=3)
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client), clock=clock,
                                              policy=policy)
    transfer = await submitted(engine)
    client.refusal = TorBoxAPIError("ACTIVE_LIMIT", "slots", 403)
    waits = []
    await engine.resolve_pending()
    for _ in range(8):
        (standby,) = await repository.standbys(transfer.id)
        waits.append(standby["retry_at"] - clock.now)
        clock.now = standby["retry_at"]
        await engine.resolve_pending()
    assert waits[:7] == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0]
    assert waits[7] == 300.0


async def test_p3_8_a_hard_backup_failure_ends_only_that_backup(tmp_path, monkeypatch):
    client = FakeClient()
    primary = Primary()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, primary, torbox(client))
    transfer = await submitted(engine)
    client.refusal = TorBoxAPIError("INVALID_MAGNET", "bad", 400)
    await engine.resolve_pending()
    (standby,) = await repository.standbys(transfer.id)
    assert standby["state"] == "failed" and standby["error"] is not None
    for _ in range(3):
        await engine.resolve_pending()
    assert len(creates(client)) == 1                                     # never retried
    root = await root_of(repository, transfer)
    assert root.state == "waiting" and await repository.bound_route_provider(root.id) == "provider-a"


# -- T3.6 / disable: ON -> OFF and a disabled provider keep the ordinary lifecycle -------------------------

async def test_t3_6_turning_backups_off_makes_no_new_one_and_keeps_the_existing_one(tmp_path, monkeypatch):
    clock = Clock()
    client = FakeClient()
    _repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client, on=True), clock=clock)
    first = await submitted(engine)
    await engine.resolve_pending()
    assert len(creates(client)) == 1
    native = str(client.next_id)
    client.calls.clear()

    clock.now += 3_600
    off, _registry, off_engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client, on=False),
                                          fresh=False, clock=clock)          # the rebuild after saving OFF
    second = await submitted(off_engine, magnet("b" * 40))
    await off_engine.resolve_pending()

    assert creates(client) == []                                         # no new backup
    assert await off.standbys(second.id) == ()
    (kept,) = await off.standbys(first.id)
    assert kept["state"] == "bound"                                      # the existing one is kept ...
    assert ("item", TORRENT, native) in client.calls                     # ... and still observed
    assert not any(call[0] == "delete" for call in client.calls)         # never deleted for being OFF


# -- P3.9: cleanup authority ---------------------------------------------------------------------------------

@pytest.mark.parametrize("remote", [True, False])
async def test_p3_9_a_backup_follows_the_existing_cleanup_authority(tmp_path, monkeypatch, remote):
    client = FakeClient()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client))
    transfer = await submitted(engine)
    await engine.resolve_pending()
    assert len(await repository.standbys(transfer.id)) == 1
    native = str(client.next_id)

    await engine.delete(transfer.id, remote=remote)

    deleted = ("delete", TORRENT, native) in client.calls
    assert deleted is remote                                             # exactly a primary resource's rule
    if not remote:
        assert native in client.objects[TORRENT]


# -- trace -----------------------------------------------------------------------------------------------------

async def test_the_trace_exports_the_backup_without_secrets_or_native_payloads(tmp_path, monkeypatch):
    client = FakeClient()
    _repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client))
    transfer = await submitted(engine)
    await engine.resolve_pending()

    trace = await transfer_trace.build(transfer.id, None)

    rows = [item["row"] for item in trace["data"]["standby_resources"] if item["scope"] == "primary"]
    assert [(row["provider_id"], row["state"]) for row in rows] == [("torbox", "bound")]
    assert rows[0]["binding_id"] in {item["row"]["id"] for item in trace["data"]["provider_resources"]}
    text = json.dumps(trace)
    assert client.token not in text and "magnet:?xt" not in text


# -- an interrupted claim is reconciled read-only, whatever the control says now --------------------------

async def _interrupted_claim(tmp_path, monkeypatch, client):
    """ON: the primary route begins and a backup claim is durably started;
    the process dies before the claim is bound (whatever TorBox did)."""
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client, on=True))
    transfer = await submitted(engine)
    await engine._resolve(await root_of(repository, transfer))
    root = await root_of(repository, transfer)
    assert await repository.begin_standby(transfer.id, root.id, "torbox", engine.clock()) is not None
    return transfer, root


async def test_off_after_a_crash_adopts_the_torrent_the_interrupted_claim_created(tmp_path, monkeypatch):
    clock = Clock()
    client = FakeClient()
    transfer, root = await _interrupted_claim(tmp_path, monkeypatch, client)
    client.objects[TORRENT]["900"] = torrent(native_id=900)              # TorBox did create it
    client.calls.clear()

    restarted, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client, on=False),
                                            fresh=False, clock=clock)    # the operator turned it OFF
    await engine.resolve_pending()

    assert creates(client) == []                                         # zero new creates
    (standby,) = await restarted.standbys(transfer.id)
    assert standby["state"] == "bound" and standby["resource"].ownership == Ownership.ADOPTED
    assert standby["resource"].id == next(resource.id for resource, _s, _p in await restarted.resources(transfer.id)
                                          if resource.provider_id == "torbox")
    client.calls.clear()
    clock.now += 3_600
    await engine.resolve_pending()
    assert ("item", TORRENT, "900") in client.calls                      # ordinary observation continues
    after = await root_of(restarted, transfer)
    assert after.state == root.state and after.resource == root.resource
    assert await restarted.bound_route_provider(root.id) == "provider-a"
    await engine.delete(transfer.id, remote=True)
    assert ("delete", TORRENT, "900") in client.calls                    # and ordinary cleanup authority


async def test_off_after_a_crash_settles_a_claim_whose_create_never_happened(tmp_path, monkeypatch):
    client = FakeClient()
    transfer, root = await _interrupted_claim(tmp_path, monkeypatch, client)   # TorBox never saw it
    client.calls.clear()

    restarted, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client, on=False),
                                            fresh=False)
    for _ in range(3):
        await engine.resolve_pending()

    assert creates(client) == []                                         # zero creates
    assert all(resource.provider_id != "torbox" for resource, _s, _p in await restarted.resources(transfer.id))
    (standby,) = await restarted.standbys(transfer.id)
    assert standby["state"] == "failed" and standby["binding_id"] is None   # settled, not ambiguous
    assert standby["error"].category.value == "resource_not_found"
    after = await root_of(restarted, transfer)
    assert after.state == root.state and after.resource == root.resource
    assert await restarted.bound_route_provider(root.id) == "provider-a"


async def test_on_after_a_crash_a_proven_absent_claim_gets_its_one_ordinary_attempt(tmp_path, monkeypatch):
    client = FakeClient()
    transfer, _root = await _interrupted_claim(tmp_path, monkeypatch, client)
    restarted, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client, on=True),
                                            fresh=False)
    await engine.resolve_pending()
    assert len(creates(client)) == 1
    (standby,) = await restarted.standbys(transfer.id)
    assert standby["state"] == "bound" and standby["resource"].ownership == Ownership.CREATED


async def test_an_ambiguous_inventory_never_binds_or_creates(tmp_path, monkeypatch):
    client = FakeClient()
    transfer, _root = await _interrupted_claim(tmp_path, monkeypatch, client)
    client.objects[TORRENT]["900"] = torrent(native_id=900)
    client.objects[TORRENT]["901"] = torrent(native_id=901)              # two candidates: no proof which
    client.calls.clear()
    restarted, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client, on=False),
                                            fresh=False)
    await engine.resolve_pending()
    assert creates(client) == []
    (standby,) = await restarted.standbys(transfer.id)
    assert standby["state"] == "creating" and standby["binding_id"] is None


# -- an interrupted claim nothing proves absent is never attempted again, even ON ------------------------

@pytest.mark.parametrize("inventory", ["errors", "incomplete"])
async def test_on_an_inconclusive_inventory_never_repeats_the_interrupted_create(tmp_path, monkeypatch, inventory):
    from transfers.models import ResourceSnapshot
    client = FakeClient()
    transfer, root = await _interrupted_claim(tmp_path, monkeypatch, client)
    client.calls.clear()
    backup = torbox(client, on=True)

    async def inconclusive():
        if inventory == "errors":
            raise RuntimeError("inventory unavailable")
        return ResourceSnapshot((), complete=False)

    backup.inventory = inconclusive
    restarted, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), backup, fresh=False)
    for _ in range(3):
        await engine.resolve_pending()

    assert creates(client) == []                                         # 0 productive creates
    (standby,) = await restarted.standbys(transfer.id)
    assert standby["state"] == "creating" and standby["binding_id"] is None
    after = await root_of(restarted, transfer)
    assert after.state == root.state and await restarted.bound_route_provider(root.id) == "provider-a"


async def test_on_an_ambiguous_inventory_never_binds_or_repeats_the_create(tmp_path, monkeypatch):
    client = FakeClient()
    transfer, _root = await _interrupted_claim(tmp_path, monkeypatch, client)
    client.objects[TORRENT]["900"] = torrent(native_id=900)
    client.objects[TORRENT]["901"] = torrent(native_id=901)
    client.calls.clear()
    restarted, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), torbox(client, on=True),
                                            fresh=False)
    for _ in range(3):
        await engine.resolve_pending()
    assert creates(client) == []                                         # 0 productive creates
    (standby,) = await restarted.standbys(transfer.id)
    assert standby["state"] == "creating" and standby["binding_id"] is None   # no arbitrary bind
    assert all(resource.provider_id != "torbox" for resource, _s, _p in await restarted.resources(transfer.id))
