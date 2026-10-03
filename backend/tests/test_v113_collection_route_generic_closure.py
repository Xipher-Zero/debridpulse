"""Collection route authority keeps generic competition closed.

Once a specialized provider durably owns a direct-link collection
(``torrents.collection_route_provider_id``), a root that its owner exhausts
under policy re-competes only among the specialized claimants that remain --
the existing neutral exhaustion handoff -- never among generic ones. Without
that collection fact (single-root transfers, all-generic collections) the
established competition is untouched.

Every provider here is a neutral fixture: no concrete integration is named.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

import db.database as database
from fake_integrations import MemoryExecutor
from transfers.applicability import ApplicabilityReadiness, HostClaim, HostClaimScope, ProviderApplicability
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage, TransferError
from transfers.models import (
    Capability, Endpoint, IntegrationDescriptor, ResolutionResult, ResourceState, TransferCandidate,
    TransferRequest, TransferState,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.recovery_repository import TransferRepository

pytestmark = pytest.mark.asyncio

# The provider-final "this provider does not support that link" failure, as the
# existing translators emit it (provider domain, provider origin, never retried).
UNSUPPORTED = NormalizedError(Domain.PROVIDER, Category.UNSUPPORTED_REQUEST, Stage.RESOLUTION,
                              Retryability.NEVER, origin=Origin.PROVIDER)
UNAVAILABLE = NormalizedError(Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION,
                              Retryability.BACKOFF, origin=Origin.PROVIDER)


class Route:
    """Neutral URL provider: unsupported for the payloads in ``unsupported``,
    else one executable candidate named after the payload."""

    def __init__(self, identity, *, hosts=(), generic=False, priority=0):
        self.descriptor = IntegrationDescriptor(identity, identity, frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"https"}), priority=priority)
        self.applicability = ProviderApplicability(
            generic_schemes=frozenset({"https"}) if generic else frozenset(),
            specialized_hosts=tuple(HostClaim(host, HostClaimScope.DOMAIN, frozenset({"https"})) for host in hosts),
            specialized=bool(hosts), readiness=ApplicabilityReadiness.READY)
        self.unsupported: set[str] = set()
        self.resolved: list[str] = []

    async def resolve(self, request):
        payload = str(request.payload)
        self.resolved.append(payload)
        if payload in self.unsupported:
            raise TransferError(replace(UNSUPPORTED, integration_id=self.descriptor.id))
        name = payload.rsplit("/", 1)[-1]
        return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
            name, (Endpoint("memory", f"memory:{self.descriptor.id}:{name}"),), expected_bytes=4,
            provider_id=self.descriptor.id),))


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


def url(host, name):
    return f"https://{host}/{name}"


async def lab(tmp_path, monkeypatch, *providers, fresh=True, clock=None):
    if fresh:
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "closure.sqlite3")
        await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    for provider in providers:
        registry.register_provider(provider)
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "downloads"),
                            policy=TransferPolicy(retry_delay=0.0, max_attempts=3), clock=clock or Clock())
    await engine.initialize()
    return repository, registry, executor, engine


async def submit(engine, *payloads, source="direct_link"):
    return await engine.submit(tuple(TransferRequest("https", payload, name=payload.rsplit("/", 1)[-1])
                                     for payload in payloads),
                               name="collection", source=source, deduplicate=False)


async def drive(engine, passes=8):
    for _ in range(passes):
        await engine.resolve_pending()


async def by_payload(repository, transfer_id):
    return {str(item.request.payload): item for item in await repository.requests(transfer_id)
            if item.parent_id is None}


async def routes(repository, transfer_id):
    return (await repository.presentation(transfer_id, details=True))["route_attempts"]


async def owned_489_shape(tmp_path, monkeypatch, *, owner_claims_b=False, extra=()):
    """owner X claims A and C; B is unsupported through X; generic would take B."""
    a, b, c = url("special.test", "a.bin"), url("other.test", "b.bin"), url("special.test", "c.bin")
    owner = Route("special-x", hosts=("special.test",) + (("other.test",) if owner_claims_b else ()))
    owner.unsupported.add(b)
    generic = Route("generic-route", generic=True)
    repository, registry, executor, engine = await lab(tmp_path, monkeypatch, owner, generic, *extra)
    transfer = await submit(engine, a, b, c)
    return (a, b, c), owner, generic, repository, registry, executor, engine, transfer


def assert_generic_never_touched(generic, artifacts, history):
    assert generic.resolved == []
    assert all(item["provider_id"] != "generic-route" for item in history)
    assert all(item["transition_kind"] != "provider_change" for item in history)
    for artifact in artifacts:
        assert all(candidate.provider_id != "generic-route" for candidate in artifact.candidates)


# -- 11.1 / Section 9: the owner blocks request-level generic fallback ------------------

@pytest.mark.parametrize("owner_claims_b", [False, True])
async def test_an_owned_collection_root_its_owner_exhausts_never_falls_to_generic(
        tmp_path, monkeypatch, owner_claims_b):
    (a, b, c), owner, generic, repository, _registry, _executor, engine, transfer = await owned_489_shape(
        tmp_path, monkeypatch, owner_claims_b=owner_claims_b)

    await drive(engine)

    assert await repository.collection_route_provider(transfer.id) == "special-x"
    assert sorted(owner.resolved) == sorted([a, b, c])
    history = await routes(repository, transfer.id)
    assert [(item["provider_id"], item["transition_kind"], item["transition_reason"]) for item in history
            if item["provider_id"] == "generic-route"] == []
    assert generic.resolved == []
    roots = await by_payload(repository, transfer.id)
    assert roots[b].state == "failed"
    assert roots[b].error.category == Category.UNSUPPORTED_REQUEST
    assert roots[a].state != "failed" and roots[c].state != "failed"
    assert await repository.exhausted_route_providers(roots[b].id) == frozenset({"special-x"})
    artifacts = await repository.artifacts(transfer.id)
    assert {artifact.name for artifact in artifacts} == {"a.bin", "c.bin"}
    assert_generic_never_touched(generic, artifacts, await routes(repository, transfer.id))


# -- Gap A inside an owned collection: specialized -> specialized stays legal ----------

async def test_another_specialized_claimant_still_takes_the_root_its_owner_exhausted(tmp_path, monkeypatch):
    other = Route("special-y", hosts=("other.test",))
    (a, b, c), owner, generic, repository, _registry, _executor, engine, transfer = await owned_489_shape(
        tmp_path, monkeypatch, owner_claims_b=True, extra=(other,))

    await drive(engine)

    assert await repository.collection_route_provider(transfer.id) == "special-x"
    assert other.resolved == [b]
    roots = await by_payload(repository, transfer.id)
    assert await repository.bound_route_provider(roots[b].id) == "special-y"
    assert generic.resolved == []
    assert await repository.exhausted_route_providers(roots[b].id) == frozenset({"special-x"})
    history = [(item["provider_id"], item["transition_kind"]) for item in await routes(repository, transfer.id)]
    assert ("special-y", "provider_change") in history
    assert all(provider != "generic-route" for provider, _kind in history)


# -- 11.2 / 11.3: no collection owner, no change ---------------------------------------

async def test_an_unowned_collection_still_routes_through_generic(tmp_path, monkeypatch):
    owner = Route("special-x", hosts=("special.test",))
    generic = Route("generic-route", generic=True)
    repository, _registry, executor, engine = await lab(tmp_path, monkeypatch, owner, generic)
    one, two = url("one.test", "one.bin"), url("two.test", "two.bin")
    transfer = await submit(engine, one, two)

    await drive(engine)
    await engine.tick()

    assert await repository.collection_route_provider(transfer.id) is None
    assert owner.resolved == []
    assert sorted(generic.resolved) == sorted([one, two])
    for artifact in await repository.artifacts(transfer.id):
        executor.finish(artifact.execution)
    await engine.tick()
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED


async def test_a_single_request_keeps_the_established_specialized_then_generic_handoff(tmp_path, monkeypatch):
    owner = Route("special-x", hosts=("special.test",))
    generic = Route("generic-route", generic=True)
    payload = url("special.test", "single.bin")
    owner.unsupported.add(payload)
    repository, _registry, _executor, engine = await lab(tmp_path, monkeypatch, owner, generic)
    transfer = await submit(engine, payload)

    await drive(engine)

    # No collection owner exists for one root: the pre-ownership competition
    # (provider exhaustion failover) is exactly what it was.
    assert await repository.collection_route_provider(transfer.id) is None
    assert owner.resolved == [payload] and generic.resolved == [payload]


# -- 11.4: restart ---------------------------------------------------------------------

async def test_restart_keeps_generic_closed_and_the_specialized_handoff_open(tmp_path, monkeypatch):
    other = Route("special-y", hosts=("other.test",))
    (a, b, c), owner, generic, repository, _registry, _executor, engine, transfer = await owned_489_shape(
        tmp_path, monkeypatch, owner_claims_b=True, extra=(other,))
    await engine._prepare_collection_affinity()
    record = (await by_payload(repository, transfer.id))[b]
    # One resolution outside a running cycle: the exhaustion commits and nothing
    # re-enters before the "crash".
    await engine._resolve(record)
    assert other.resolved == [] and generic.resolved == []
    assert (await by_payload(repository, transfer.id))[b].state == "pending"

    restarted_owner = Route("special-x", hosts=("special.test", "other.test"))
    restarted_owner.unsupported.add(b)
    restarted_other = Route("special-y", hosts=("other.test",))
    restarted_generic = Route("generic-route", generic=True)
    restarted, _registry, _executor, restarted_engine = await lab(
        tmp_path, monkeypatch, restarted_owner, restarted_generic, restarted_other, fresh=False)
    await drive(restarted_engine)

    assert await restarted.collection_route_provider(transfer.id) == "special-x"
    assert b not in restarted_owner.resolved
    assert restarted_other.resolved == [b]
    assert restarted_generic.resolved == []


async def test_restart_never_reconsiders_an_owned_root_as_unowned(tmp_path, monkeypatch):
    (a, b, c), owner, generic, repository, _registry, _executor, engine, transfer = await owned_489_shape(
        tmp_path, monkeypatch)
    await engine._prepare_collection_affinity()
    await engine._resolve((await by_payload(repository, transfer.id))[b])

    restarted_owner = Route("special-x", hosts=("special.test",))
    restarted_owner.unsupported.add(b)
    restarted_generic = Route("generic-route", generic=True)
    restarted, _registry, _executor, restarted_engine = await lab(
        tmp_path, monkeypatch, restarted_owner, restarted_generic, fresh=False)
    await drive(restarted_engine)

    assert restarted_generic.resolved == []
    assert b not in restarted_owner.resolved
    assert (await by_payload(restarted, transfer.id))[b].state == "failed"


# -- 11.5: provider recovery ------------------------------------------------------------

async def test_provider_recovery_resumes_the_owner_and_never_reopens_generic(tmp_path, monkeypatch):
    clock = Clock()
    a, b = url("special.test", "a.bin"), url("other.test", "b.bin")
    owner = Route("special-x", hosts=("special.test",))
    owner.unsupported.add(b)
    generic = Route("generic-route", generic=True)
    repository, registry, _executor, engine = await lab(tmp_path, monkeypatch, owner, generic, clock=clock)
    transfer = await submit(engine, a, b)
    await engine._prepare_collection_affinity()
    registry.mark_health("special-x", healthy=False)

    await drive(engine)
    assert owner.resolved == [] and generic.resolved == []
    assert all(item.error and item.error.category == Category.PROVIDER_UNAVAILABLE
               for item in (await by_payload(repository, transfer.id)).values())

    registry.mark_health("special-x", healthy=True)
    clock.now += 3_600
    await drive(engine)

    assert sorted(owner.resolved) == sorted([a, b])
    assert generic.resolved == []
    assert (await by_payload(repository, transfer.id))[b].state == "failed"


# -- 11.7: the 489-shaped collection settles -----------------------------------------------

async def test_a_489_shaped_collection_settles_every_leaf_and_completes(tmp_path, monkeypatch):
    good = [url("special.test", f"good-{index}.bin") for index in range(2)]
    bad = [url(f"junk-{index}.test", f"junk-{index}.bin") for index in range(5)]
    owner = Route("special-x", hosts=("special.test",))
    owner.unsupported.update(bad)
    generic = Route("generic-route", generic=True)
    repository, _registry, executor, engine = await lab(tmp_path, monkeypatch, owner, generic)
    transfer = await submit(engine, good[0], *bad[:3], good[1], *bad[3:])

    await drive(engine)
    await engine.tick()
    for artifact in await repository.artifacts(transfer.id):
        assert artifact.execution is not None
        executor.finish(artifact.execution)
    for _ in range(4):
        await engine.tick()

    current = await repository.get(transfer.id)
    roots = await by_payload(repository, transfer.id)
    assert current.state == TransferState.COMPLETED
    assert all(roots[payload].state == "failed" and roots[payload].error.category == Category.UNSUPPORTED_REQUEST
               for payload in bad)
    assert all(roots[payload].state not in {"pending", "resolving", "waiting", "materializing"} for payload in good)
    artifacts = await repository.artifacts(transfer.id)
    assert {artifact.name for artifact in artifacts} == {"good-0.bin", "good-1.bin"}
    assert all(artifact.state == "completed" for artifact in artifacts)
    assert_generic_never_touched(generic, artifacts, await routes(repository, transfer.id))
    # Settled: further cycles resolve nothing anywhere.
    resolved = len(owner.resolved)
    await drive(engine)
    assert len(owner.resolved) == resolved and generic.resolved == []


# -- 11.8: manual Retry -------------------------------------------------------------------

async def test_operator_retry_reenters_the_owner_and_never_generic(tmp_path, monkeypatch):
    a, b = url("special.test", "a.bin"), url("other.test", "b.bin")
    owner = Route("special-x", hosts=("special.test",))
    owner.unsupported.update({a, b})
    generic = Route("generic-route", generic=True)
    repository, _registry, _executor, engine = await lab(tmp_path, monkeypatch, owner, generic)
    transfer = await submit(engine, a, b)
    await drive(engine)
    assert (await repository.get(transfer.id)).state == TransferState.FAILED
    assert generic.resolved == []

    owner.unsupported.discard(a)  # the owner can serve A now; B stays unsupported
    assert await engine.retry(transfer.id)
    await drive(engine)

    assert sorted(owner.resolved) == sorted([a, b, a, b])
    assert generic.resolved == []
    roots = await by_payload(repository, transfer.id)
    assert roots[b].state == "failed" and roots[a].state != "failed"
    assert await repository.collection_route_provider(transfer.id) == "special-x"


# -- 11.9: Resume -------------------------------------------------------------------------

@pytest.mark.parametrize("specialized_alternate", [False, True])
async def test_resume_keeps_generic_closed_for_an_owned_collection(tmp_path, monkeypatch, specialized_alternate):
    other = Route("special-y", hosts=("other.test",))
    a, b = url("special.test", "a.bin"), url("other.test", "b.bin")
    owner = Route("special-x", hosts=("special.test", "other.test"))
    owner.unsupported.add(b)
    generic = Route("generic-route", generic=True)
    extra = (other,) if specialized_alternate else ()
    repository, _registry, _executor, engine = await lab(tmp_path, monkeypatch, owner, generic, *extra)
    transfer = await submit(engine, a, b)
    await engine._prepare_collection_affinity()
    await engine.pause(transfer.id)
    await drive(engine)
    assert owner.resolved == []

    await engine.resume(transfer.id)
    await drive(engine)

    assert sorted(owner.resolved) == sorted([a, b])
    assert other.resolved == ([b] if specialized_alternate else [])
    assert generic.resolved == []
    if not specialized_alternate:
        assert (await by_payload(repository, transfer.id))[b].state == "failed"
