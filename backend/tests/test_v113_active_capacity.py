"""Provider active capacity and primary priority over backups.

A provider states its concurrent active-resource capacity for a request class
(``ActiveCapacitySource``): the effective maximum (its account maximum,
lowered by the operator's own ceiling, never raised) and the account-wide
occupancy. Core keeps backups out of a full provider (deferred through the
ordinary backup cadence, no productive call) and lets primary work take back
the slots DebridPulse's own unpromoted backups hold -- proactively when the
maximum is known, and, when it is not, once after a genuine concurrent-
capacity refusal (one backup, one retry). TorBox is the first provider to
state it: its plan's active slots, and every torrent TorBox reports active.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_v113_collection_route_generic_closure import Clock
from test_v113_standby_preparation import (
    Primary,
    creates,
    lab,
    magnet,
    root_of,
    submitted,
)
from test_v113_torbox_provider import FakeClient, torrent

from providers.torbox.account import ACTIVE_SLOTS, active_slot_maximum
from providers.torbox.client import TORRENT, TorBoxAPIError
from providers.torbox.provider import TorBoxProvider
from transfers.models import ActiveCapacity, ResourceState, TransferRequest

pytestmark = pytest.mark.asyncio


class CappedClient(FakeClient):
    """A TorBox account that enforces its active-slot cap: torrents it
    creates are active, and a creation past the cap is refused."""

    def __init__(self, cap, **kwargs):
        super().__init__(**kwargs)
        self.cap = cap
        self.refused = []

    def active(self):
        return sum(1 for item in self.objects[TORRENT].values() if item.get("active") is True)

    async def create_torrent(self, *, magnet="", metainfo=None, name=""):
        if self.active() >= self.cap:
            self.calls.append(("create_torrent_refused", magnet or metainfo))
            self.refused.append(magnet or metainfo)
            raise TorBoxAPIError("ACTIVE_LIMIT", "active slots full", 403)
        native = await super().create_torrent(magnet=magnet, metainfo=metainfo, name=name)
        self.objects[TORRENT][native]["active"] = True
        return native


def tb(client, *, plan=None, override=None, on=True):
    provider = TorBoxProvider(client, prepare_backup_torrents=on, max_active_torrents=override)
    provider.account = SimpleNamespace(contract=AsyncMock(), entitlements=SimpleNamespace(plan=plan))
    return provider


def external(client, native_id, *, active=True, cached=False):
    """A torrent on the account that DebridPulse did not add."""
    item = torrent(native_id=native_id, state="cached" if cached else "downloading", present=cached)
    item["active"] = active
    client.objects[TORRENT][str(native_id)] = item


def preferring_torbox(digest):
    request = magnet(digest)
    return TransferRequest(request.kind, request.payload, name=request.name, fingerprint=request.fingerprint,
                           preferred_provider="torbox")


async def standby_rows(repository, transfer):
    return await repository.standbys(transfer.id)


# -- the provider fact: plan maximum, operator ceiling, account-wide active occupancy -----------------

@pytest.mark.parametrize("plan, maximum", [("Free", 1), ("Essential", 3), ("Standard", 5), ("Pro", 10)])
async def test_each_plan_states_its_active_slot_maximum(plan, maximum):
    assert active_slot_maximum(SimpleNamespace(plan=plan)) == maximum == ACTIVE_SLOTS[plan]
    assert await tb(FakeClient(), plan=plan).active_capacity(magnet()) == ActiveCapacity(maximum, 0)


async def test_an_unknown_plan_states_no_maximum_and_a_ceiling_alone_only_lowers():
    assert (await tb(FakeClient()).active_capacity(magnet())).maximum is None
    assert (await tb(FakeClient(), override=2).active_capacity(magnet())).maximum == 2


@pytest.mark.parametrize("plan, override, effective", [
    ("Pro", None, 10),          # unset follows the plan
    ("Pro", 4, 4),              # a lower ceiling wins
    ("Essential", 8, 3),        # a ceiling above the plan never raises it
])
async def test_the_effective_maximum_is_the_lower_of_plan_and_ceiling(plan, override, effective):
    assert (await tb(FakeClient(), plan=plan, override=override).active_capacity(magnet())).maximum == effective


async def test_the_maximum_follows_the_plan_as_it_changes():
    unset = tb(FakeClient(), plan="Pro")
    ceiling = tb(FakeClient(), plan="Essential", override=4)
    unset.account.entitlements = SimpleNamespace(plan="Essential")          # plan decreases
    assert (await unset.active_capacity(magnet())).maximum == 3
    unset.account.entitlements = SimpleNamespace(plan="Standard")           # plan increases, no ceiling
    assert (await unset.active_capacity(magnet())).maximum == 5
    assert (await ceiling.active_capacity(magnet())).maximum == 3           # plan below the ceiling
    ceiling.account.entitlements = SimpleNamespace(plan="Pro")              # plan increases past it
    assert (await ceiling.active_capacity(magnet())).maximum == 4           # the lower ceiling remains


async def test_occupancy_is_every_active_torrent_on_the_account_and_never_a_cached_one():
    client = FakeClient()
    external(client, 1)                                   # active, not DebridPulse's: counts
    external(client, 2)
    external(client, 3, active=False, cached=True)        # cached: occupies no slot
    external(client, 4, active=False)
    assert await tb(client, plan="Pro").active_capacity(magnet()) == ActiveCapacity(10, 2)


async def test_a_request_without_torrent_slots_states_no_capacity():
    provider = tb(FakeClient(), plan="Pro")
    assert await provider.active_capacity(TransferRequest("https", "https://hoster.example/f/1")) is None


# -- standby admission ------------------------------------------------------------------------------------

async def test_a_full_provider_gets_no_backup_create_and_the_backup_waits(tmp_path, monkeypatch):
    clock = Clock()
    client = CappedClient(3)
    for native in (1, 2, 3):
        external(client, native)                          # the account's own activity fills the plan
    primary = Primary()
    repository, _registry, engine = await lab(tmp_path, monkeypatch, primary, tb(client, plan="Essential"),
                                              clock=clock)
    transfer = await submitted(engine)

    await engine.resolve_pending()

    assert creates(client) == [] and client.refused == []                 # zero productive calls
    (standby,) = await standby_rows(repository, transfer)
    assert standby["state"] == "deferred" and standby["error"].category.value == "concurrency_limited"
    root = await root_of(repository, transfer)
    assert root.resource.provider_id == "provider-a" and root.state == "waiting"     # primary untouched

    client.objects[TORRENT]["3"]["active"] = False                        # a slot frees on the account
    clock.now = standby["retry_at"]
    await engine.resolve_pending()
    assert len(creates(client)) == 1
    assert (await standby_rows(repository, transfer))[0]["state"] == "bound"


async def test_a_backup_may_use_every_idle_slot(tmp_path, monkeypatch):
    client = CappedClient(1)
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), tb(client, plan="Free"))
    transfer = await submitted(engine)
    await engine.resolve_pending()
    (standby,) = await standby_rows(repository, transfer)
    assert standby["state"] == "bound" and client.active() == 1           # capacity 1: the backup holds it


# -- primary priority ---------------------------------------------------------------------------------------

async def test_capacity_one_a_primary_takes_the_slot_its_backup_held(tmp_path, monkeypatch):
    clock = Clock()
    client = CappedClient(1)
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), tb(client, plan="Free"),
                                              clock=clock)
    first = await submitted(engine)
    await engine.resolve_pending()
    (held,) = await standby_rows(repository, first)
    native = str(client.next_id)
    assert held["state"] == "bound" and client.active() == 1

    second = await submitted(engine, preferring_torbox("b" * 40))         # primary work that needs TorBox
    await engine.resolve_pending()

    assert client.refused == []                                           # never refused because of DP's backup
    assert ("delete", TORRENT, native) in client.calls                    # the backup was given back ...
    (displaced,) = await standby_rows(repository, first)
    assert displaced["state"] == "deferred" and displaced["binding_id"] is None
    root = await root_of(repository, second)
    assert root.resource.provider_id == "torbox" and root.resource.id != held["resource"].id   # ... the primary has it
    first_root = await root_of(repository, first)
    assert first_root.resource.provider_id == "provider-a"                # the other transfer is untouched

    # The displaced backup prepares again through the ordinary cadence once
    # a slot is idle -- never by taking the primary's.
    clock.now += 3_600
    await engine.resolve_pending()
    assert (await standby_rows(repository, first))[0]["state"] == "deferred" and client.refused == []
    client.objects[TORRENT][str(client.next_id)]["active"] = False
    clock.now += 3_600
    await engine.resolve_pending()
    assert (await standby_rows(repository, first))[0]["state"] == "bound"


async def test_only_as_many_backups_as_needed_are_reclaimed(tmp_path, monkeypatch):
    client = CappedClient(3)
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), tb(client, plan="Essential"))
    transfers = [await submitted(engine, magnet(f"{index}" * 40)) for index in range(1, 4)]
    await engine.resolve_pending()
    assert client.active() == 3
    for item in transfers:
        assert (await standby_rows(repository, item))[0]["state"] == "bound"

    await submitted(engine, preferring_torbox("f" * 40))
    await engine.resolve_pending()

    assert len([call for call in client.calls if call[0] == "delete"]) == 1    # exactly one slot freed
    assert client.refused == []


async def test_external_activity_and_current_primaries_are_never_reclaimed(tmp_path, monkeypatch):
    client = CappedClient(1)
    external(client, 1)                                                    # the account's own torrent
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), tb(client, plan="Free"))
    transfer = await submitted(engine, preferring_torbox("b" * 40))
    await engine.resolve_pending()
    assert not any(call[0] == "delete" for call in client.calls)           # nothing of DP's to give back
    assert creates(client) == [] and client.refused == []                 # no productive call over the maximum
    root = await root_of(repository, transfer)
    assert root.resource is None or root.resource.provider_id != "torbox"


# -- reactive, when the maximum cannot be known ---------------------------------------------------------------

async def test_an_unknown_maximum_reclaims_one_backup_after_a_real_capacity_refusal(tmp_path, monkeypatch):
    client = CappedClient(1)
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), tb(client))   # plan unknown
    first = await submitted(engine)
    await engine.resolve_pending()
    (held,) = await standby_rows(repository, first)
    assert held["state"] == "bound"

    second = await submitted(engine, preferring_torbox("b" * 40))
    await engine.resolve_pending()

    primary_refusals = [item for item in client.refused if "b" * 40 in str(item)]
    assert len(primary_refusals) == 1                                      # refused once, then room was made
    assert len([call for call in client.calls if call[0] == "delete"]) == 1
    assert (await root_of(repository, second)).resource.provider_id == "torbox"


async def test_reactive_reclaim_is_one_backup_one_retry_and_never_a_purge(tmp_path, monkeypatch):
    client = CappedClient(2)
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), tb(client))
    held = [await submitted(engine, magnet(f"{index}" * 40)) for index in (1, 2)]
    await engine.resolve_pending()
    assert client.active() == 2
    external(client, 900)                                                  # outside activity takes the freed slot
    client.cap = 2

    async def refuse_always(*, magnet="", metainfo=None, name=""):
        client.refused.append(magnet)
        raise TorBoxAPIError("ACTIVE_LIMIT", "active slots full", 403)

    client.create_torrent = refuse_always
    await submitted(engine, preferring_torbox("c" * 40))
    await engine._resolve(next(item for item in await repository.requests(
        (await repository.active())[-1].id) if item.parent_id is None))

    assert len(client.refused) == 2                                        # the attempt, and ONE retry
    assert len([call for call in client.calls if call[0] == "delete"]) == 1   # one backup, never all of them
    still_bound = 0
    for item in held:
        still_bound += (await standby_rows(repository, item))[0]["state"] == "bound"
    assert still_bound == 1


@pytest.mark.parametrize("code", ["RATE_LIMIT", "COOLDOWN_LIMIT", "MONTHLY_LIMIT", "PLAN_RESTRICTED_FEATURE"])
async def test_no_other_refusal_ever_reclaims_a_backup(tmp_path, monkeypatch, code):
    client = CappedClient(5)
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), tb(client))
    first = await submitted(engine)
    await engine.resolve_pending()
    assert (await standby_rows(repository, first))[0]["state"] == "bound"

    async def refuse(*, magnet="", metainfo=None, name=""):
        raise TorBoxAPIError(code, "no", 429 if code == "RATE_LIMIT" else 403)

    client.create_torrent = refuse
    await submitted(engine, preferring_torbox("b" * 40))
    await engine.resolve_pending()
    assert not any(call[0] == "delete" for call in client.calls)
    assert (await standby_rows(repository, first))[0]["state"] == "bound"


# -- promotion still reuses, and cleanup returns capacity -------------------------------------------------------

async def test_a_reclaimed_resource_is_never_promoted_and_ordinary_cleanup_returns_the_slot(tmp_path, monkeypatch):
    client = CappedClient(1)
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), tb(client, plan="Free"))
    first = await submitted(engine)
    await engine.resolve_pending()
    second = await submitted(engine, preferring_torbox("b" * 40))
    await engine.resolve_pending()
    assert (await repository.promotable_standby((await root_of(repository, first)).id, "torbox")) is None

    await engine.delete(second.id, remote=True)                           # the primary's transfer ends
    assert client.active() == 0                                            # its slot is back
    resources = [state for resource, state, _p in await repository.resources(second.id)
                 if resource.provider_id == "torbox"]
    assert resources == [ResourceState.ABSENT]


# -- Settings: an operator ceiling exists only when the operator sets one ---------------------------------------

async def test_the_ceiling_persists_only_when_set_and_clears_to_no_value():
    from fastapi import HTTPException
    from test_v113_torbox_routes import TOKEN, _application, _settings_owner, _Stored

    from api.routes import (
        IntegrationConfigurationUpdate,
        patch_integration_configuration,
    )
    stored = _Stored(enabled=True, api_token=TOKEN)
    with _settings_owner(stored):
        await patch_integration_configuration("torbox", IntegrationConfigurationUpdate(
            options={"prepare_backup_torrents": True}), _application())
    assert stored.cfg.integrations["torbox"].options.get("max_active_torrents") is None   # no synthetic default
    for value in (4, None):
        with _settings_owner(stored):
            await patch_integration_configuration("torbox", IntegrationConfigurationUpdate(
                options={"max_active_torrents": value}), _application())
        assert stored.cfg.integrations["torbox"].options["max_active_torrents"] == value
    with _settings_owner(stored), pytest.raises(HTTPException) as refused:
        await patch_integration_configuration("torbox", IntegrationConfigurationUpdate(
            options={"max_active_torrents": 11}), _application())                       # above any plan
    assert refused.value.status_code == 400
    assert stored.cfg.integrations["torbox"].options["max_active_torrents"] is None



# -- reclamation completes durably; a slot is free only once cleanup confirms it --------------------------------

async def test_a_slot_freed_by_a_later_cleanup_admits_the_primary_and_releases_the_backup(tmp_path, monkeypatch):
    clock = Clock()
    client = CappedClient(1)
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), tb(client, plan="Free"),
                                              clock=clock)
    first = await submitted(engine)
    await engine.resolve_pending()
    (held,) = await standby_rows(repository, first)
    native = str(client.next_id)
    deletes = client.delete

    async def unavailable(family, native_id):
        client.calls.append(("delete_failed", family, native_id))
        raise TorBoxAPIError("UNKNOWN_ERROR", "try later", 503)

    client.delete = unavailable
    second = await submitted(engine, preferring_torbox("b" * 40))
    await engine.resolve_pending()

    # Reclamation was asked for but not confirmed: no productive primary call,
    # the backup is not promotable, and its cleanup is still owed.
    assert [call for call in client.calls if call[0] == "create_torrent" and "b" * 40 in str(call[1])] == []
    assert client.refused == []
    assert await repository.promotable_standby((await root_of(repository, first)).id, "torbox") is None
    assert [pending for resource, _state, pending in await repository.resources(first.id)
            if resource.provider_id == "torbox"] == ["owned"]
    assert (await standby_rows(repository, first))[0]["state"] == "bound"

    client.delete = deletes                                                # the ordinary cleanup cadence succeeds
    clock.now += 60
    await engine.resolve_pending()

    assert ("delete", TORRENT, native) in client.calls
    (released,) = await standby_rows(repository, first)
    assert released["state"] == "deferred" and released["binding_id"] is None
    assert (await root_of(repository, second)).resource.provider_id == "torbox"   # the primary proceeds
    assert client.refused == []
    # The displaced backup prepares again once a slot is idle.
    client.objects[TORRENT][str(client.next_id)]["active"] = False
    clock.now += 3_600
    await engine.resolve_pending()
    assert (await standby_rows(repository, first))[0]["state"] == "bound"
    assert held["resource"].id != (await standby_rows(repository, first))[0]["resource"].id


# -- what a new backup occupies is the provider's to say ----------------------------------------------------------

class CachedClient(CappedClient):
    """Creations TorBox already holds: ready at once, occupying no slot."""

    async def create_torrent(self, *, magnet="", metainfo=None, name=""):
        native = await super().create_torrent(magnet=magnet, metainfo=metainfo, name=name)
        self.objects[TORRENT][native].update(active=False, download_state="cached", download_present=True)
        return native


@pytest.mark.parametrize("client_type, bound", [(CachedClient, 2), (CappedClient, 1)])
async def test_backups_are_admitted_by_the_providers_own_count(tmp_path, monkeypatch, client_type, bound):
    client = client_type(1)
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), tb(client, plan="Free"))
    transfers = [await submitted(engine, magnet(digest * 40)) for digest in ("1", "2")]
    await engine.resolve_pending()
    states = [(await standby_rows(repository, item))[0]["state"] for item in transfers]
    # Cached backups occupy nothing, so both fit in a one-slot plan; an
    # actively downloading one takes the slot and the next one waits.
    assert states.count("bound") == bound and client.refused == []


# -- the effective maximum binds primary work too ------------------------------------------------------------------

async def test_the_operator_ceiling_holds_a_primary_when_nothing_of_dps_can_be_given_back(tmp_path, monkeypatch):
    client = CappedClient(10)                                              # the plan would accept more
    for native in (1, 2, 3):
        external(client, native)
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(),
                                              tb(client, plan="Pro", override=3))
    transfer = await submitted(engine, preferring_torbox("b" * 40))
    await engine.resolve_pending()
    assert creates(client) == [] and client.active() == 3                 # the ceiling is respected
    root = await root_of(repository, transfer)
    assert root.resource is None or root.resource.provider_id != "torbox"


async def test_the_operator_ceiling_admits_a_primary_by_giving_back_one_backup(tmp_path, monkeypatch):
    client = CappedClient(10)
    external(client, 1)
    external(client, 2)
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(),
                                              tb(client, plan="Pro", override=3))
    first = await submitted(engine)
    await engine.resolve_pending()
    assert (await standby_rows(repository, first))[0]["state"] == "bound" and client.active() == 3

    second = await submitted(engine, preferring_torbox("b" * 40))
    await engine.resolve_pending()

    assert (await root_of(repository, second)).resource.provider_id == "torbox"
    assert client.active() == 3                                            # never above the ceiling
    assert len([call for call in client.calls if call[0] == "delete"]) == 1


async def test_an_overfull_provider_gives_back_enough_backups_to_admit_the_primary(tmp_path, monkeypatch):
    client = CappedClient(10)
    external(client, 1)
    external(client, 2)
    _repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), tb(client, plan="Pro"))
    for digest in ("1", "2", "3"):
        await submitted(engine, magnet(digest * 40))
    await engine.resolve_pending()
    assert client.active() == 5                                            # 2 external + 3 backups

    # The operator lowers the ceiling to 3: occupancy 5 is now over it.
    repository, _registry, lowered = await lab(tmp_path, monkeypatch, Primary(),
                                               tb(client, plan="Pro", override=3), fresh=False)
    second = await lowered.submit((preferring_torbox("b" * 40),), name="Show", deduplicate=False)
    await lowered.resolve_pending()

    assert len([call for call in client.calls if call[0] == "delete"]) == 3   # down to maximum - 1 ...
    assert (await root_of(repository, second)).resource.provider_id == "torbox"
    assert client.active() == 3                                            # ... then the primary: exactly 3



# -- provenance: who actually refused ----------------------------------------------------------------------------

async def test_a_locally_held_primary_is_recorded_as_core_not_provider(tmp_path, monkeypatch):
    from transfers.errors import Origin
    from transfers.policy import provider_attributable
    client = CappedClient(10)
    for native in (1, 2, 3):
        external(client, native)
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), tb(client, plan="Pro", override=3))
    transfer = await submitted(engine, preferring_torbox("b" * 40))
    await engine._resolve(await root_of(repository, transfer))

    assert creates(client) == [] and client.refused == []                 # TorBox was never asked
    error = (await root_of(repository, transfer)).error
    assert (error.domain.value, error.category.value, error.retryability.value) == (
        "provider", "concurrency_limited", "backoff")
    assert error.origin == Origin.CORE and error.integration_id == "torbox"
    assert provider_attributable(error)                                    # same routing as a refusal


async def test_a_real_active_limit_refusal_is_recorded_as_provider(tmp_path, monkeypatch):
    from transfers.errors import Origin
    client = CappedClient(1)
    external(client, 1)
    repository, _registry, engine = await lab(tmp_path, monkeypatch, Primary(), tb(client))   # maximum unknown
    transfer = await submitted(engine, preferring_torbox("b" * 40))
    await engine._resolve(await root_of(repository, transfer))

    assert len(client.refused) == 1                                        # TorBox answered ACTIVE_LIMIT
    error = (await root_of(repository, transfer)).error
    assert error.category.value == "concurrency_limited" and error.origin == Origin.PROVIDER
