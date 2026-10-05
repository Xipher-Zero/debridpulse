"""Read-only provider availability and READY preference.

Among the providers already competing for an unbound root (the TASK1
claimant set), a provider that declares ``Capability.AVAILABILITY`` may say,
without beginning any acquisition, whether it can deliver the root now
(``AvailabilityState``). For a BitTorrent-class root a READY competitor is
preferred, each group keeping the established order; with no READY answer the
order is exactly what it was. NOT_READY and UNKNOWN never reorder, never
remove a competitor and never exhaust one. A hoster (HTTP(S)) root keeps its
established winner and is not asked at all: one provider being able to read a
cache is no evidence that another cannot deliver.

Provider fixtures are neutral; the TorBox adapter is exercised through its
real client over a scripted transport.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from test_v113_collection_route_generic_closure import (
    Clock,
    Route,
    by_payload,
    drive,
    lab,
    submit,
    url,
)
from test_v113_torbox_provider import NoLimit, Transport, ok

import transfers.engine as engine_module
from db.database import get_db
from providers.torbox.client import API, TorBoxService, webdl_cache_key
from providers.torbox.provider import TorBoxProvider
from transfers.applicability import HostClaim, HostClaimScope, ProviderApplicability
from transfers.errors import Category, TransferError
from transfers.models import (
    AvailabilityState,
    CachePresence,
    Capability,
    Endpoint,
    IntegrationDescriptor,
    ResolutionResult,
    ResourceState,
    TransferCandidate,
    TransferRequest,
)
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

READY, NOT_READY, UNKNOWN = AvailabilityState.READY, AvailabilityState.NOT_READY, AvailabilityState.UNKNOWN
HASH = "0123456789abcdef0123456789abcdef01234567"


def magnet(digest=HASH):
    return TransferRequest("magnet", f"magnet:?xt=urn:btih:{digest}", name=digest[:6], fingerprint=digest)


class Claimant:
    """A neutral provider of one request class. ``state`` is its read-only
    availability answer (a state, an exception to raise, ``"slow"`` to
    outlast the bound, or a malformed answer); ``observes=False`` declares no
    availability at all."""

    def __init__(self, identity, *, kinds=("magnet",), hosts=(), priority=0, state=UNKNOWN, observes=True,
                 enabled=True, entitled=True, ready_payloads=None):
        capabilities = {Capability.RESOLVE} | ({Capability.AVAILABILITY} if observes else set())
        self.descriptor = IntegrationDescriptor(identity, identity, frozenset(capabilities),
                                                request_types=frozenset(kinds), priority=priority, enabled=enabled)
        if hosts:
            self.applicability = ProviderApplicability(
                specialized_hosts=tuple(HostClaim(host, HostClaimScope.DOMAIN, frozenset(kinds)) for host in hosts),
                specialized=True)
        self.state = state
        self.entitled = entitled
        self.ready_payloads = ready_payloads
        self.asked: list[tuple] = []
        self.resolved: list = []

    def entitlement_for(self, request):
        return self.entitled

    async def availability(self, requests):
        self.asked.append(tuple(str(request.payload) for request in requests))
        if isinstance(self.state, BaseException):
            raise self.state
        if self.state == "slow":
            await asyncio.sleep(10)
        if self.state == "short":
            return ()
        if self.ready_payloads is not None:
            return tuple(READY if str(request.payload) in self.ready_payloads else NOT_READY for request in requests)
        return tuple(self.state for _ in requests)

    async def resolve(self, request):
        self.resolved.append(str(request.payload))
        return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
            "payload.bin", (Endpoint("memory", f"memory:{self.descriptor.id}:{len(self.resolved)}"),),
            expected_bytes=4, provider_id=self.descriptor.id),))


async def winner(tmp_path, monkeypatch, *providers, request=None):
    """The provider that resolves one submitted root, and the decision that started it."""
    request = request or magnet()
    repository, registry, _executor, engine = await lab(tmp_path, monkeypatch, *providers)
    transfer = await engine.submit((request,), name="root", deduplicate=False)
    await drive(engine, 3)
    taken = [provider.descriptor.id for provider in providers if provider.resolved]
    async with get_db() as db:
        row = await db.fetchone("SELECT routing_decision FROM route_attempt_provenance WHERE transfer_id=? "
                                "ORDER BY ordinal LIMIT 1", (transfer.id,))
    decision = json.loads(row["routing_decision"]) if row and row["routing_decision"] else None
    return taken, decision, registry, repository, engine, transfer


def task1_winner(*providers, request=None):
    """The TASK1 selection for ``request`` with no availability at all."""
    registry = IntegrationRegistry()
    for provider in providers:
        registry.register_provider(provider)
    return registry.provider_for(request or magnet()).descriptor.id


def availability_of(decision):
    return {item["provider_id"]: item.get("availability") for item in decision["providers"]}


# -- T2.1 / F2.2: every competitor UNKNOWN keeps the TASK1 winner ------------------------

async def test_t2_1_all_unknown_preserves_the_task1_winner(tmp_path, monkeypatch):
    a = Claimant("provider-a", priority=10, state=UNKNOWN)
    b = Claimant("provider-b", observes=False)
    expected = task1_winner(Claimant("provider-a", priority=10), Claimant("provider-b"))
    taken, decision, *_ = await winner(tmp_path, monkeypatch, a, b)
    assert taken == [expected] == ["provider-a"]
    assert availability_of(decision) == {"provider-a": "unknown", "provider-b": "unknown"}


# -- T2.2 / F2.1, T2.3: a READY BitTorrent competitor outranks the others -----------------

@pytest.mark.parametrize("a_state, a_observes", [(NOT_READY, True), (UNKNOWN, True), (UNKNOWN, False)])
async def test_t2_2_t2_3_ready_outranks_not_ready_and_unknown(tmp_path, monkeypatch, a_state, a_observes):
    a = Claimant("provider-a", priority=10, state=a_state, observes=a_observes)
    b = Claimant("provider-b", priority=0, state=READY)
    assert task1_winner(Claimant("provider-a", priority=10), Claimant("provider-b")) == "provider-a"
    taken, decision, *_ = await winner(tmp_path, monkeypatch, a, b)
    assert taken == ["provider-b"]
    assert a.resolved == []        # still a competitor; it was simply not first
    selected = next(item for item in decision["providers"] if item["disposition"] == "selected")
    assert (selected["provider_id"], selected["availability"]) == ("provider-b", "ready")
    assert {item["provider_id"]: item["disposition"] for item in decision["providers"]}["provider-a"] == \
        "applicable_not_selected"


# -- T2.4: several READY keep the established order among themselves ------------------------

async def test_t2_4_multiple_ready_use_the_existing_deterministic_order(tmp_path, monkeypatch):
    a = Claimant("provider-a", priority=10, state=NOT_READY)
    b = Claimant("provider-b", priority=1, state=READY)
    c = Claimant("provider-c", priority=5, state=READY)
    taken, *_ = await winner(tmp_path, monkeypatch, a, b, c)
    assert taken == ["provider-c"]


async def test_t2_4_equal_priority_ready_fall_back_to_identity_order():
    registry = IntegrationRegistry()
    for identity in ("provider-c", "provider-b", "provider-a"):
        registry.register_provider(Claimant(identity))
    route = registry.provider_route(magnet(), availability={"provider-c": READY, "provider-b": READY,
                                                            "provider-a": NOT_READY})
    assert route.provider.descriptor.id == "provider-b"


# -- T2.5: no READY preserves the existing order exactly ---------------------------------------

@pytest.mark.parametrize("leader", ["provider-a", "provider-b"])
async def test_t2_5_not_ready_versus_unknown_adds_no_ordering(tmp_path, monkeypatch, leader):
    a = Claimant("provider-a", priority=int(leader == "provider-a"), state=NOT_READY)
    b = Claimant("provider-b", priority=int(leader == "provider-b"), state=UNKNOWN)
    taken, *_ = await winner(tmp_path, monkeypatch, a, b)
    assert taken == [leader]


# -- T2.6: a failed observation is UNKNOWN and never fails routing ---------------------------

@pytest.mark.parametrize("failure", ["transfer_error", "exception", "timeout", "short", "historical_type"])
async def test_t2_6_probe_failure_becomes_unknown_and_routing_proceeds(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(engine_module, "AVAILABILITY_TIMEOUT_SECONDS", 0.05)
    state = {"transfer_error": TransferError(replace(_unsupported(), integration_id="provider-a")),
             "exception": RuntimeError("probe broke"), "timeout": "slow", "short": "short",
             "historical_type": CachePresence.HIT}[failure]
    a = Claimant("provider-a", priority=10, state=state)
    b = Claimant("provider-b", state=NOT_READY)
    taken, decision, _registry, repository, _engine, transfer = await winner(tmp_path, monkeypatch, a, b)
    assert taken == ["provider-a"]                       # the TASK1 winner, unchanged
    assert availability_of(decision)["provider-a"] == "unknown"
    root = next(iter((await by_payload(repository, transfer.id)).values()))
    assert root.state == "resolved" and root.error is None
    assert await repository.exhausted_route_providers(root.id) == frozenset()


def _unsupported():
    from test_v113_collection_route_generic_closure import UNSUPPORTED
    return UNSUPPORTED


# -- T2.7: observation is read-only ------------------------------------------------------------

async def test_t2_7_observation_creates_nothing_and_begins_no_acquisition(tmp_path, monkeypatch):
    a = Claimant("provider-a", priority=10, state=NOT_READY)
    b = Claimant("provider-b", state=READY)
    repository, _registry, _executor, engine = await lab(tmp_path, monkeypatch, a, b)
    transfer = await engine.submit((magnet(),), name="root", deduplicate=False)
    record = next(iter((await by_payload(repository, transfer.id)).values()))

    route = await engine._route(record)

    # Both competitors were asked, the decision was made, and nothing anywhere
    # was resolved, created or recorded as a route attempt.
    assert a.asked and b.asked and route.provider is b
    assert a.resolved == [] and b.resolved == []
    assert await repository.resources(transfer.id) == ()
    assert (await repository.presentation(transfer.id, details=True))["route_attempts"] == []


async def test_t2_7_torbox_availability_uses_only_its_read_endpoints():
    transport = Transport({
        ("GET", "torrents/checkcached"): [ok({HASH: {"name": "x", "size": 1, "hash": HASH}})],
        ("POST", "webdl/checkcached"): [ok({})],
    })
    provider = TorBoxProvider(TorBoxService("t", rate_limiter=NoLimit(), transport=transport))
    states = await provider.availability((magnet(), TransferRequest("https", "https://hoster.example/f/1")))
    assert states == (READY, NOT_READY)
    # Any other route (create, add, upload, delete) is absent from the script
    # and would have failed the call.
    assert [(call["method"], call["url"].removeprefix(API + "/")) for call in transport.calls] == [
        ("GET", "torrents/checkcached"), ("POST", "webdl/checkcached")]


# -- T2.8: nobody outside the TASK1 competition is asked --------------------------------------

async def test_t2_8_nonclaimants_are_never_queried(tmp_path, monkeypatch):
    a = Claimant("provider-a", state=NOT_READY)
    disabled = Claimant("disabled-bt", state=READY, enabled=False)
    unhealthy = Claimant("unhealthy-bt", state=READY)
    hoster_only = Claimant("hoster-only", kinds=("https",), hosts=("hoster.test",), state=READY)
    _repository, registry, _executor, engine = await lab(tmp_path, monkeypatch, a, disabled, unhealthy, hoster_only)
    registry.mark_health("unhealthy-bt", healthy=False)
    await engine.submit((magnet(),), name="root", deduplicate=False)
    await drive(engine, 3)
    assert a.asked and a.resolved
    assert disabled.asked == unhealthy.asked == hoster_only.asked == []


# -- T2.9: batching --------------------------------------------------------------------------------

async def test_t2_9_torbox_observes_many_roots_in_one_batched_read_per_cache():
    digests = [f"{index:040x}" for index in range(3)]
    links = ["https://hoster.example/a", "https://hoster.example/b"]
    transport = Transport({
        ("GET", "torrents/checkcached"): [ok({digests[1]: {"name": "held"}})],
        ("POST", "webdl/checkcached"): [ok({webdl_cache_key(links[0]): {"name": "held"}})],
    })
    provider = TorBoxProvider(TorBoxService("t", rate_limiter=NoLimit(), transport=transport))
    requests = (magnet(digests[0]), TransferRequest("https", links[0]), magnet(digests[1]),
                TransferRequest("torrent", b"metainfo", name="t", fingerprint=digests[2]),
                TransferRequest("https", links[1]), TransferRequest("magnet", "magnet:?dn=no-hash"))
    states = await provider.availability(requests)
    assert states == (NOT_READY, READY, READY, NOT_READY, NOT_READY, UNKNOWN)   # order and cardinality kept
    torrent_reads = [call for call in transport.calls if call["url"].endswith("torrents/checkcached")]
    assert len(torrent_reads) == 1 and sorted(torrent_reads[0]["params"]["hash"].split(",")) == sorted(digests)
    assert len([call for call in transport.calls if call["url"].endswith("webdl/checkcached")]) == 1


async def test_t2_9_a_large_batch_is_split_at_the_providers_bound():
    digests = [f"{index:040x}" for index in range(150)]
    transport = Transport({("GET", "torrents/checkcached"): [ok({}), ok({digests[-1]: {"name": "held"}})]})
    provider = TorBoxProvider(TorBoxService("t", rate_limiter=NoLimit(), transport=transport))
    states = await provider.availability(tuple(magnet(digest) for digest in digests))
    assert len(transport.calls) == 2
    assert [len(call["params"]["hash"].split(",")) for call in transport.calls] == [100, 50]
    assert states[-1] == READY and set(states[:-1]) == {NOT_READY}


async def test_t2_6_a_failed_torbox_read_is_a_normalized_failure_the_engine_reads_as_unknown():
    transport = Transport({("GET", "torrents/checkcached"): [(500, {"success": False, "error": "UNKNOWN_ERROR",
                                                                    "detail": "x", "data": None})]})
    provider = TorBoxProvider(TorBoxService("t", rate_limiter=NoLimit(), transport=transport))
    with pytest.raises(TransferError):
        await provider.availability((magnet(),))


# -- T2.10: TASK1 submission closure is preserved ------------------------------------------------

async def test_t2_10_availability_never_reopens_generic_competition(tmp_path, monkeypatch):
    specialized = Route("special-x", hosts=("special.test",))
    generic = Claimant("generic-route", kinds=("https",), state=READY)
    generic.applicability = ProviderApplicability(generic_schemes=frozenset({"https"}))
    repository, registry, _executor, engine = await lab(tmp_path, monkeypatch, specialized, generic)
    claimed, unclaimed = url("special.test", "a.bin"), url("junk.test", "b.bin")
    transfer = await submit(engine, claimed, unclaimed)
    await drive(engine)
    roots = await by_payload(repository, transfer.id)
    assert await repository.collection_route_authority(transfer.id) is True
    assert roots[unclaimed].state == "failed" and roots[unclaimed].error.category == Category.UNSUPPORTED_REQUEST
    assert generic.asked == [] and generic.resolved == []
    # Even handed a READY answer, a provider outside the competition gains nothing.
    route = registry.provider_route(TransferRequest("https", unclaimed), generic_closed=True,
                                    availability={"generic-route": READY})
    assert route.provider is None


# -- T2.11: a provider-bound or member request is never re-competed --------------------------------

async def test_t2_11_members_and_bound_routes_are_never_observed(tmp_path, monkeypatch):
    a = Claimant("provider-a", priority=10, state=NOT_READY)
    b = Claimant("provider-b", state=READY)
    repository, _registry, _executor, engine = await lab(tmp_path, monkeypatch, a, b)
    transfer = await engine.submit((magnet(),), name="root", deduplicate=False)
    root = next(iter((await by_payload(repository, transfer.id)).values()))

    await engine._route(replace(root, parent_id="parent"))     # a member continues its route
    assert a.asked == [] and b.asked == []

    await drive(engine, 3)
    asked = (len(a.asked), len(b.asked))
    route = await engine._route(root)                        # bound now: never re-decided
    assert route.provider is b and route.decision is None
    assert (len(a.asked), len(b.asked)) == asked


# -- T2.12 / T2.13 / F2.3: hoster roots keep the TASK1 winner ----------------------------------------

HOSTER_COMBOS = (("alldebrid", "torbox"), ("realdebrid", "torbox"), ("alldebrid", "realdebrid", "torbox"))


@pytest.mark.parametrize("combo", HOSTER_COMBOS)
async def test_t2_12_hoster_winner_is_unchanged_by_asymmetric_observability(tmp_path, monkeypatch, combo):
    request = TransferRequest("https", "https://hoster.test/f.bin")
    providers = [Claimant(identity, kinds=("https",), hosts=("hoster.test",),
                          state=READY if identity == "torbox" else UNKNOWN,
                          observes=identity == "torbox") for identity in combo]
    expected = task1_winner(*[Claimant(identity, kinds=("https",), hosts=("hoster.test",)) for identity in combo],
                            request=request)
    taken, decision, *_ = await winner(tmp_path, monkeypatch, *providers, request=request)
    assert taken == [expected] and expected != "torbox"
    torbox = next(provider for provider in providers if provider.descriptor.id == "torbox")
    assert torbox.asked == []                                # not even asked: an answer cannot order here
    assert all("availability" not in item for item in decision["providers"])


@pytest.mark.parametrize("combo", HOSTER_COMBOS)
async def test_t2_13_a_hoster_ready_fact_may_be_observed_without_ordering_effect(combo):
    link = "https://hoster.test/f.bin"
    transport = Transport({("POST", "webdl/checkcached"): [ok({webdl_cache_key(link): {"name": "held"}})]})
    observed = await TorBoxProvider(TorBoxService("t", rate_limiter=NoLimit(), transport=transport)).availability(
        (TransferRequest("https", link),))
    assert observed == (READY,)                              # the adapter's truthful observation

    request = TransferRequest("https", link)
    registry = IntegrationRegistry()
    for identity in combo:
        registry.register_provider(Claimant(identity, kinds=("https",), hosts=("hoster.test",)))
    unordered = registry.provider_route(request)
    route = registry.provider_route(request, availability={"torbox": observed[0]})
    assert route.provider.descriptor.id == unordered.provider.descriptor.id != "torbox"
    assert availability_of(json.loads(route.decision.encode()))["torbox"] == "ready"   # observed, recorded, not ordering


# -- contract and trace -----------------------------------------------------------------------------

async def test_declaring_availability_requires_the_contract():
    class Undeclared(Claimant):
        availability = None

    registry = IntegrationRegistry()
    with pytest.raises(TypeError):
        registry.register_provider(Undeclared("broken"))


async def test_the_decision_records_only_consumed_neutral_facts(tmp_path, monkeypatch):
    a = Claimant("provider-a", priority=10, state=NOT_READY)
    b = Claimant("provider-b", state=READY)
    other = Claimant("hoster-only", kinds=("https",), hosts=("hoster.test",), state=READY)
    _taken, decision, *_ = await winner(tmp_path, monkeypatch, a, b, other)
    assert set(decision) == {"v", "outcome", "providers"}
    assert [(item["provider_id"], item["disposition"], item.get("availability")) for item in decision["providers"]] \
        == [("provider-b", "selected", "ready"), ("provider-a", "applicable_not_selected", "not_ready")]
    for item in decision["providers"]:
        assert set(item) <= {"provider_id", "disposition", "class", "availability"}


# -- T2.9 through routing: one batched read covers the roots waiting to be routed ------------------

async def test_t2_9_twenty_single_magnet_transfers_are_observed_in_one_batched_read(tmp_path, monkeypatch):
    """Twenty magnets submitted together arrive as twenty single-root
    transfers (Quick Add / link file: one transfer per magnet). The batching
    provider is asked once, about all of them, and each root is routed by
    its own answer."""
    digests = [f"{index:040x}" for index in range(20)]
    payloads = [str(magnet(digest).payload) for digest in digests]
    ready = set(payloads[::2])
    a = Claimant("provider-a", priority=10, observes=False)
    b = Claimant("provider-b", ready_payloads=ready)
    _repository, _registry, _executor, engine = await lab(tmp_path, monkeypatch, a, b)
    for digest in digests:
        await engine.submit((magnet(digest),), name=digest[:6], deduplicate=False)

    await drive(engine, 3)

    assert len(b.asked) == 1 and sorted(b.asked[0]) == sorted(payloads)
    assert a.asked == []                                     # declares no availability: never asked
    assert sorted(b.resolved) == sorted(ready)               # READY roots: the READY competitor
    assert sorted(a.resolved) == sorted(set(payloads) - ready)   # the rest: the TASK1 winner


async def test_t2_9_one_transfer_with_many_magnet_roots_is_observed_in_one_batched_read(tmp_path, monkeypatch):
    digests = [f"{index:040x}" for index in range(5)]
    a = Claimant("provider-a", priority=10, observes=False)
    b = Claimant("provider-b", ready_payloads={str(magnet(digests[3]).payload)})
    _repository, _registry, _executor, engine = await lab(tmp_path, monkeypatch, a, b)
    await engine.submit(tuple(magnet(digest) for digest in digests), name="many", deduplicate=False)

    await drive(engine, 3)

    assert len(b.asked) == 1 and len(b.asked[0]) == 5
    assert b.resolved == [str(magnet(digests[3]).payload)] and len(a.resolved) == 4


async def test_t2_9_a_round_answer_is_used_once_and_only_while_fresh(tmp_path, monkeypatch):
    clock = Clock()
    a = Claimant("provider-a", priority=10, observes=False)
    b = Claimant("provider-b", state=READY)
    repository, _registry, _executor, engine = await lab(tmp_path, monkeypatch, a, b, clock=clock)
    first = await engine.submit((magnet("1" * 40),), name="one", deduplicate=False)
    second = await engine.submit((magnet("2" * 40),), name="two", deduplicate=False)
    third = await engine.submit((magnet("3" * 40),), name="three", deduplicate=False)
    root = {transfer.id: next(iter((await by_payload(repository, transfer.id)).values()))
            for transfer in (first, second, third)}

    assert (await engine._route(root[first.id])).provider is b
    assert len(b.asked) == 1 and len(b.asked[0]) == 3        # one round covered all three
    assert (await engine._route(root[second.id])).provider is b
    assert len(b.asked) == 1                                 # its answer from that round, used once
    assert (await engine._route(root[second.id])).provider is b
    assert len(b.asked) == 2                                 # used up: observed again
    clock.now += engine_module.AVAILABILITY_ROUND_FRESH_SECONDS + 1
    assert (await engine._route(root[third.id])).provider is b
    assert len(b.asked) == 3                                 # stale: never used, observed again


# -- entitlement: READY never promotes past entitlement uncertainty --------------------------------

async def test_ready_never_promotes_an_entitlement_unknown_provider_over_an_entitled_winner(tmp_path, monkeypatch):
    a = Claimant("provider-a", priority=10, state=NOT_READY, entitled=True)
    b = Claimant("provider-b", priority=0, state=READY, entitled=None)
    taken, decision, *_ = await winner(tmp_path, monkeypatch, a, b)
    assert taken == ["provider-a"]                           # the TASK1 winner routes, nothing waits
    assert {item["provider_id"]: (item["disposition"], item.get("availability"))
            for item in decision["providers"]} == {
        "provider-a": ("selected", "not_ready"), "provider-b": ("entitlement_unresolved", "ready")}


async def test_an_entitlement_unknown_task1_winner_stays_exactly_as_premature(tmp_path, monkeypatch):
    u = Claimant("provider-u", priority=10, state=NOT_READY, entitled=None)
    c = Claimant("provider-c", priority=0, state=READY, entitled=True)
    repository, registry, _executor, engine = await lab(tmp_path, monkeypatch, u, c)
    assert registry.provider_route(magnet()).unresolved == ("provider-u",)        # TASK1: held
    transfer = await engine.submit((magnet(),), name="root", deduplicate=False)
    await drive(engine, 3)
    root = next(iter((await by_payload(repository, transfer.id)).values()))
    assert u.resolved == c.resolved == []                    # still held for u's account truth
    assert (root.state, root.error) == ("pending", None)
    assert (await engine._route(root)).unresolved == ("provider-u",)


async def test_ready_promotes_only_entitlement_established_competitors(tmp_path, monkeypatch):
    a = Claimant("provider-a", priority=10, state=NOT_READY, entitled=True)
    u = Claimant("provider-u", priority=5, state=READY, entitled=None)
    c = Claimant("provider-c", priority=0, state=READY, entitled=True)
    registry = IntegrationRegistry()
    for provider in (a, u, c):
        registry.register_provider(provider)
    route = registry.provider_route(magnet(), availability={"provider-a": NOT_READY, "provider-u": READY,
                                                            "provider-c": READY})
    assert route.provider is c
    order = [item.provider_id for item in route.decision.providers]
    assert order == ["provider-c", "provider-a", "provider-u"]   # u keeps its place behind a
