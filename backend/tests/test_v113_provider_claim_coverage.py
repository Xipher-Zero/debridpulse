"""Submission-wide specialized authority with per-root provider claim coverage.

A direct-link submission of independent roots has specialized authority when
any specialized provider legitimately claims any of its roots
(``torrents.collection_route_authority``); that closes generic competition for
EVERY root, including the unclaimed ones (the transfer-489 class). Which
specialized provider takes a root is that root's own competition: a per-root
union, so no provider has to claim the whole set, a provider that cannot claim
one root still competes for the others, and a root no specialized provider
claims is unsupported. The authority names no provider: a mixed-provider
submission has it without any single collection owner.

Every provider here is a neutral fixture; no concrete integration is named.
"""
from __future__ import annotations

import json

import pytest
from fake_integrations import ParcelProvider
from test_v113_collection_route_generic_closure import (
    Route,
    assert_generic_never_touched,
    by_payload,
    drive,
    lab,
    routes,
    submit,
    url,
)

from db.database import get_db
from services import transfer_trace
from transfers.errors import Category
from transfers.models import ResourceState, TransferRequest, TransferState

pytestmark = pytest.mark.asyncio

HOSTS = {index: url(f"host{index}.test", f"f{index}.bin") for index in range(1, 6)}


def overlapping(*, a_priority=0, b_priority=0):
    """A claims hosts 1-3, B claims hosts 2-4, nobody specialized claims 5."""
    a = Route("provider-a", hosts=("host1.test", "host2.test", "host3.test"), priority=a_priority)
    b = Route("provider-b", hosts=("host2.test", "host3.test", "host4.test"), priority=b_priority)
    return a, b, Route("generic-route", generic=True)


async def decisions(transfer_id):
    """Each root's recorded routing decision: the one that started its first
    route attempt, else the one recorded on the request (held/unsupported)."""
    async with get_db() as db:
        requests = await db.fetchall(
            "SELECT id,payload,routing_decision FROM transfer_requests WHERE transfer_id=? AND parent_id IS NULL",
            (transfer_id,))
        attempts = await db.fetchall(
            "SELECT request_id,routing_decision FROM route_attempt_provenance WHERE transfer_id=? ORDER BY ordinal",
            (transfer_id,))
    first = {}
    for row in attempts:
        if row["routing_decision"]:
            first.setdefault(row["request_id"], row["routing_decision"])
    found = {}
    for row in requests:
        payload = json.loads(row["payload"])["payload"]
        raw = first.get(row["id"]) or row["routing_decision"]
        found[payload] = json.loads(raw) if raw else None
    return found


def claimants(decision):
    return {item["provider_id"] for item in decision["providers"]
            if item["disposition"] in {"selected", "applicable_not_selected"}}


def disposition(decision, provider_id):
    return next(item["disposition"] for item in decision["providers"] if item["provider_id"] == provider_id)


# -- T1.1: the transfer-489 closure ---------------------------------------------------------

async def test_t1_1_some_roots_claimed_closes_generic_for_the_whole_submission(tmp_path, monkeypatch):
    claimed = [url("special.test", f"c{index}.bin") for index in range(4)]
    unclaimed = [url(f"junk{index}.test", f"u{index}.bin") for index in range(3)]
    owner = Route("special-x", hosts=("special.test",))
    generic = Route("generic-route", generic=True)
    repository, _registry, _executor, engine = await lab(tmp_path, monkeypatch, owner, generic)
    transfer = await submit(engine, *claimed, *unclaimed)

    await drive(engine)

    assert await repository.collection_route_authority(transfer.id) is True
    # Zero generic attempts, and no specialized provider is asked for a root it
    # never claimed.
    assert generic.resolved == []
    assert sorted(owner.resolved) == sorted(claimed)
    roots = await by_payload(repository, transfer.id)
    for payload in unclaimed:
        assert roots[payload].state == "failed"
        assert roots[payload].error.category == Category.UNSUPPORTED_REQUEST
        assert await repository.exhausted_route_providers(roots[payload].id) == frozenset()
    assert all(roots[payload].state == "resolved" for payload in claimed)
    history = await routes(repository, transfer.id)
    assert sorted(item["provider_id"] for item in history) == ["special-x"] * 4
    assert_generic_never_touched(generic, await repository.artifacts(transfer.id), history)


# -- T1.2 / T1.3: overlapping claimant sets, no common provider ----------------------------

async def test_t1_2_overlapping_providers_give_each_root_its_own_claimant_set(tmp_path, monkeypatch):
    a, b, generic = overlapping()
    _repository, _registry, _executor, engine = await lab(tmp_path, monkeypatch, a, b, generic)
    transfer = await submit(engine, *HOSTS.values())

    await drive(engine)

    found = await decisions(transfer.id)
    assert {index: claimants(found[HOSTS[index]]) for index in HOSTS} == {
        1: {"provider-a"},
        2: {"provider-a", "provider-b"},
        3: {"provider-a", "provider-b"},
        4: {"provider-b"},
        5: set(),
    }
    assert found[HOSTS[5]]["outcome"] == "unsupported"
    # Generic competition is closed on every root, the unclaimed one included.
    assert {disposition(found[HOSTS[index]], "generic-route") for index in HOSTS} == {
        "held_by_specialized_authority"}
    assert generic.resolved == []


async def test_t1_3_no_provider_needs_to_cover_the_whole_set(tmp_path, monkeypatch):
    a, b, generic = overlapping()
    repository, registry, _executor, engine = await lab(tmp_path, monkeypatch, a, b, generic)
    roots_1_to_4 = tuple(TransferRequest("https", HOSTS[index]) for index in range(1, 5))
    # Neither provider claims all of 1-4, and the submission still has authority.
    assert registry.collection_route_authority(roots_1_to_4) is True
    transfer = await submit(engine, *(HOSTS[index] for index in range(1, 5)))

    await drive(engine)

    assert await repository.collection_route_authority(transfer.id) is True
    roots = await by_payload(repository, transfer.id)
    assert all(roots[HOSTS[index]].state == "resolved" for index in range(1, 5))
    assert a.resolved == [HOSTS[1], HOSTS[2], HOSTS[3]]
    assert b.resolved == [HOSTS[4]]
    assert generic.resolved == []


# -- T1.4: a provider's inability is local ------------------------------------------------------

@pytest.mark.parametrize("leader", ["provider-a", "provider-b"])
async def test_t1_4_a_root_a_provider_cannot_claim_never_removes_it_from_the_others(
        tmp_path, monkeypatch, leader):
    a, b, generic = overlapping(a_priority=int(leader == "provider-a"), b_priority=int(leader == "provider-b"))
    repository, _registry, _executor, engine = await lab(tmp_path, monkeypatch, a, b, generic)
    transfer = await submit(engine, *HOSTS.values())

    await drive(engine)

    if leader == "provider-a":
        # A not claiming 4 leaves it the provider of 1-3.
        assert (a.resolved, b.resolved) == ([HOSTS[1], HOSTS[2], HOSTS[3]], [HOSTS[4]])
    else:
        # B not claiming 1 leaves it the provider of 2-4.
        assert (a.resolved, b.resolved) == ([HOSTS[1]], [HOSTS[2], HOSTS[3], HOSTS[4]])
    roots = await by_payload(repository, transfer.id)
    assert all(roots[HOSTS[index]].state == "resolved" for index in range(1, 5))
    assert roots[HOSTS[5]].state == "failed" and roots[HOSTS[5]].error.category == Category.UNSUPPORTED_REQUEST
    assert generic.resolved == []


# -- T1.5: an unsupported root follows the existing group semantics -------------------------

async def test_t1_5_an_unsupported_alternative_hands_its_group_on_and_a_required_root_fails_as_before(
        tmp_path, monkeypatch):
    a, b, generic = overlapping()
    repository, _registry, executor, engine = await lab(tmp_path, monkeypatch, a, b, generic)
    unclaimed_alternative, claimed_alternative = url("host5.test", "alt.bin"), url("host1.test", "alt.bin")
    required, unsupported_required = HOSTS[4], url("other5.test", "req.bin")
    transfer = await engine.submit(
        tuple(TransferRequest("https", payload, name=payload.rsplit("/", 1)[-1])
              for payload in (unclaimed_alternative, claimed_alternative, required, unsupported_required)),
        name="grouped", source="direct_link", deduplicate=False, alternative_groups=(1, 1, None, None))

    await drive(engine)

    roots = await by_payload(repository, transfer.id)
    # The selected alternative nobody claims fails terminally; the existing
    # group rule hands the group to its next alternative in submitted order.
    assert roots[unclaimed_alternative].state == "failed"
    assert roots[unclaimed_alternative].error.category == Category.UNSUPPORTED_REQUEST
    assert roots[claimed_alternative].state == "resolved"
    assert a.resolved == [claimed_alternative]
    # An ungrouped unsupported root fails exactly like any terminal root failure.
    assert roots[unsupported_required].state == "failed"
    assert roots[unsupported_required].error.category == Category.UNSUPPORTED_REQUEST
    assert roots[required].state == "resolved" and b.resolved == [required]
    await engine.tick()
    for artifact in await repository.artifacts(transfer.id):
        executor.finish(artifact.execution)
    for _ in range(4):
        await engine.tick()
    # The existing settlement rule (a failed source among produced artifacts
    # still completes the transfer), unchanged.
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert generic.resolved == []


# -- T1.6: an atomic provider-native collection is unchanged ---------------------------------

async def test_t1_6_one_provider_native_collection_stays_one_atomic_resource(tmp_path, monkeypatch):
    parcel = ParcelProvider()
    specialized = Route("special-x", hosts=("special.test",))
    generic = Route("generic-route", generic=True)
    repository, registry, _executor, engine = await lab(tmp_path, monkeypatch, parcel, specialized, generic)
    parcel.responses.append(parcel.parcel("box", state=ResourceState.AVAILABLE, files=(
        ("one.bin", "box/one.bin", 4), ("two.bin", "box/two.bin", 4), ("three.bin", "box/three.bin", 4))))
    transfer = await engine.submit((TransferRequest("parcel", "box", name="box"),), name="box", deduplicate=False)
    assert registry.collection_route_authority((TransferRequest("parcel", "box"),)) is False

    for _ in range(6):
        await engine.tick()

    assert await repository.collection_route_authority(transfer.id) is False
    resources = await repository.resources(transfer.id)
    assert [resource.provider_id for resource, _state, _pending in resources] == ["parcel-lab"]
    members = [item for item in await repository.requests(transfer.id) if item.parent_id is not None]
    assert len(members) == 3
    artifacts = await repository.artifacts(transfer.id)
    assert sorted(artifact.name for artifact in artifacts) == ["one.bin", "three.bin", "two.bin"]
    assert {candidate.provider_id for artifact in artifacts for candidate in artifact.candidates} == {"parcel-lab"}
    assert specialized.resolved == [] and generic.resolved == []


# -- T1.7: a generic-only submission is unchanged ------------------------------------------

async def test_t1_7_no_specialized_claim_anywhere_keeps_generic_routing_exactly(tmp_path, monkeypatch):
    specialized = Route("special-x", hosts=("special.test",))
    generic = Route("generic-route", generic=True)
    repository, registry, _executor, engine = await lab(tmp_path, monkeypatch, specialized, generic)
    one, two, three = url("one.test", "1.bin"), url("two.test", "2.bin"), url("three.test", "3.bin")
    assert registry.collection_route_authority(tuple(TransferRequest("https", item) for item in (one, two, three))) is False
    transfer = await submit(engine, one, two, three)

    await drive(engine)

    assert await repository.collection_route_authority(transfer.id) is False
    assert specialized.resolved == []
    assert sorted(generic.resolved) == sorted([one, two, three])
    for decision in (await decisions(transfer.id)).values():
        assert decision["outcome"] == "selected"
        assert {item["provider_id"]: item["disposition"] for item in decision["providers"]} == {
            "generic-route": "selected", "special-x": "not_applicable"}


# -- T1.8: exhaustion never reopens generic ------------------------------------------------

async def test_t1_8_exhausting_every_specialized_route_never_reopens_generic(tmp_path, monkeypatch):
    a, b, generic = overlapping()
    a.unsupported.update({HOSTS[1], HOSTS[2]})
    b.unsupported.add(HOSTS[2])
    repository, _registry, _executor, engine = await lab(tmp_path, monkeypatch, a, b, generic)
    transfer = await submit(engine, HOSTS[1], HOSTS[2], HOSTS[3])

    await drive(engine)

    roots = await by_payload(repository, transfer.id)
    # Root 1's only claimant exhausted it; root 2 moved from A to B, which
    # exhausted it too: both end unsupported, neither reaches generic.
    assert await repository.exhausted_route_providers(roots[HOSTS[1]].id) == frozenset({"provider-a"})
    assert await repository.exhausted_route_providers(roots[HOSTS[2]].id) == frozenset({"provider-a", "provider-b"})
    assert roots[HOSTS[1]].state == "failed" and roots[HOSTS[2]].state == "failed"
    assert roots[HOSTS[3]].state == "resolved"
    assert b.resolved == [HOSTS[2]]
    history = await routes(repository, transfer.id)
    # The only provider change is the specialized handoff A -> B on root 2.
    assert [item["provider_id"] for item in history if item["transition_kind"] == "provider_change"] == ["provider-b"]
    assert all(item["provider_id"] != "generic-route" for item in history)

    # An operator retry starts a new routing campaign under the same authority.
    a.unsupported.clear()
    assert await engine.retry(transfer.id)
    await drive(engine)
    assert await repository.collection_route_authority(transfer.id) is True
    assert generic.resolved == []


async def test_t1_8_a_claimed_root_whose_claimant_is_unhealthy_waits_and_never_reaches_generic(
        tmp_path, monkeypatch):
    a, b, generic = overlapping()
    repository, registry, _executor, engine = await lab(tmp_path, monkeypatch, a, b, generic)
    transfer = await submit(engine, HOSTS[1], HOSTS[4])
    await engine._prepare_collection_affinity()
    registry.mark_health("provider-a", healthy=False)

    await drive(engine)

    roots = await by_payload(repository, transfer.id)
    assert (roots[HOSTS[1]].state, roots[HOSTS[1]].error, roots[HOSTS[1]].attempts) == ("pending", None, 0)
    held = (await decisions(transfer.id))[HOSTS[1]]
    assert held["outcome"] == "held" and disposition(held, "provider-a") == "unhealthy"
    assert roots[HOSTS[4]].state == "resolved" and b.resolved == [HOSTS[4]]
    assert a.resolved == [] and generic.resolved == []

    registry.mark_health("provider-a", healthy=True)
    await drive(engine)
    assert a.resolved == [HOSTS[1]] and generic.resolved == []


# -- T1.9: trace / provenance ----------------------------------------------------------------

async def test_t1_9_the_trace_explains_authority_claimants_selection_and_the_unsupported_root(
        tmp_path, monkeypatch):
    a, b, generic = overlapping()
    _repository, _registry, _executor, engine = await lab(tmp_path, monkeypatch, a, b, generic)
    transfer = await submit(engine, *HOSTS.values())
    await drive(engine)

    trace = await transfer_trace.build(transfer.id, None)

    def rows(table):
        return [item["row"] for item in trace["data"][table] if item["scope"] == "primary"]

    submission = next(row for row in rows("torrents") if row["id"] == transfer.id)
    assert submission["collection_route_authority"] == 1
    assert submission["collection_route_provider_id"] is None   # no single provider owns this submission
    recorded = [json.loads(row["routing_decision"]) for row in rows("route_attempt_provenance")
                if row["routing_decision"]]
    unsupported = [json.loads(row["routing_decision"]) for row in rows("transfer_requests")
                   if row["routing_decision"]]
    assert sorted(next(item["provider_id"] for item in decision["providers"] if item["disposition"] == "selected")
                  for decision in recorded) == ["provider-a", "provider-a", "provider-a", "provider-b"]
    assert sorted(len(claimants(decision)) for decision in recorded) == [1, 1, 2, 2]
    assert [decision["outcome"] for decision in unsupported] == ["unsupported"]
    assert claimants(unsupported[0]) == set()
    assert disposition(unsupported[0], "generic-route") == "held_by_specialized_authority"
    # Neutral facts only: no provider-native payload rides on a decision.
    for decision in recorded + unsupported:
        assert set(decision) == {"v", "outcome", "providers"}
        assert all(set(item) <= {"provider_id", "disposition", "class"} for item in decision["providers"])


async def test_t1_8_an_unhealthy_alternate_claimant_survives_the_current_provider_s_exhaustion(
        tmp_path, monkeypatch):
    """One root, two specialized claimants. A routes it; B turns unhealthy;
    A exhausts the root. B is still a remaining specialized claimant -- the
    exhaustion handoff and ordinary selection give the same answer: the root
    continues, held while B is unhealthy, and B takes it once healthy."""
    a, b, generic = overlapping()
    shared = HOSTS[2]
    repository, registry, _executor, engine = await lab(tmp_path, monkeypatch, a, b, generic)
    transfer = await submit(engine, HOSTS[1], shared)
    await engine._prepare_collection_affinity()
    assert await repository.collection_route_authority(transfer.id) is True
    a.unsupported.add(shared)
    resolve = a.resolve

    async def resolve_then_b_turns_unhealthy(request):
        if str(request.payload) == shared:
            registry.mark_health("provider-b", healthy=False)
        return await resolve(request)

    a.resolve = resolve_then_b_turns_unhealthy

    await drive(engine)

    roots = await by_payload(repository, transfer.id)
    assert a.resolved == [HOSTS[1], shared] and b.resolved == [] and generic.resolved == []
    assert await repository.exhausted_route_providers(roots[shared].id) == frozenset({"provider-a"})
    assert roots[shared].state == "pending"
    async with get_db() as db:
        row = await db.fetchone("SELECT routing_decision FROM transfer_requests WHERE id=?", (roots[shared].id,))
    held = json.loads(row["routing_decision"])
    assert held["outcome"] == "held"
    assert disposition(held, "provider-b") == "unhealthy"
    assert disposition(held, "provider-a") == "exhausted"
    assert disposition(held, "generic-route") == "held_by_specialized_authority"

    registry.mark_health("provider-b", healthy=True)
    await drive(engine)

    roots = await by_payload(repository, transfer.id)
    assert b.resolved == [shared]
    assert roots[shared].state == "resolved"
    assert await repository.bound_route_provider(roots[shared].id) == "provider-b"
    assert generic.resolved == []
    assert all(item["provider_id"] != "generic-route" for item in await routes(repository, transfer.id))


@pytest.mark.parametrize("b_health", ["healthy", "unhealthy"])
async def test_t1_8_control_no_alternate_claimant_ends_the_root_and_generic_stays_closed(
        tmp_path, monkeypatch, b_health):
    """A claims the root, B does not. A exhausts it: no specialized claimant
    remains, so the root ends as before -- whether or not the non-claiming B
    is healthy (only a claimant is ever waited for) -- and generic stays
    closed under the established authority."""
    a, b, generic = overlapping()
    only_a = HOSTS[1]
    repository, registry, _executor, engine = await lab(tmp_path, monkeypatch, a, b, generic)
    transfer = await submit(engine, only_a, HOSTS[4])
    await engine._prepare_collection_affinity()
    assert await repository.collection_route_authority(transfer.id) is True
    a.unsupported.add(only_a)
    if b_health == "unhealthy":
        registry.mark_health("provider-b", healthy=False)

    await drive(engine)

    roots = await by_payload(repository, transfer.id)
    assert a.resolved == [only_a] and generic.resolved == []
    assert roots[only_a].state == "failed"
    assert roots[only_a].error.category == Category.UNSUPPORTED_REQUEST
    assert await repository.exhausted_route_providers(roots[only_a].id) == frozenset({"provider-a"})
    assert all(item["provider_id"] != "generic-route" for item in await routes(repository, transfer.id))
