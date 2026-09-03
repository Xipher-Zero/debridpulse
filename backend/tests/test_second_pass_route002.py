"""Second-pass ROUTE-002 persisted route ownership contracts."""
from dataclasses import replace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor
from transfers.applicability import HostClaim, HostClaimScope, ProviderApplicability
from transfers.engine import TransferEngine
from transfers.input_required import auth_required, username_password
from transfers.models import Capability, Endpoint, IntegrationDescriptor, ResourceState, ResolutionResult, TransferCandidate, TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


class AuthHttpProvider:
    def __init__(self, provider_id="route-a", *, priority=10, facts=None):
        self.descriptor = IntegrationDescriptor(
            provider_id, provider_id, frozenset({Capability.RESOLVE}),
            request_types=frozenset({"http"}), priority=priority,
        )
        self.facts = facts or ProviderApplicability(
            specialized_hosts=(HostClaim("owned.example", HostClaimScope.EXACT, frozenset({"http"})),)
        )
        self.resolve_calls = 0
        self.continuation_calls = 0

    @property
    def applicability(self):
        return self.facts

    async def resolve(self, request):
        self.resolve_calls += 1
        return ResolutionResult(ResourceState.UNKNOWN, input_required=auth_required(username_password()))

    async def resolve_with_input(self, request, submitted):
        self.continuation_calls += 1
        candidate = TransferCandidate(
            "payload.bin", (Endpoint("memory", "memory:route"),), expected_bytes=4,
            provider_id=self.descriptor.id,
        )
        return ResolutionResult(ResourceState.AVAILABLE, (candidate,))


class ImmediateHttpProvider(AuthHttpProvider):
    async def resolve(self, request):
        self.resolve_calls += 1
        candidate = TransferCandidate(
            "other.bin", (Endpoint("memory", "memory:other"),), expected_bytes=4,
            provider_id=self.descriptor.id,
        )
        return ResolutionResult(ResourceState.AVAILABLE, (candidate,))


@pytest_asyncio.fixture
async def routed(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "route002.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    owner = AuthHttpProvider()
    registry.register_provider(owner)
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=0, resolution_retry_delay=0, adoption_stability_seconds=0),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    transfer = await engine.submit((TransferRequest("http", "http://owned.example/file.bin"),), deduplicate=False)
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    record = (await repository.requests(transfer.id))[0]
    assert challenge and challenge.integration_id == owner.descriptor.id
    assert await repository.bound_route_provider(record.id) == owner.descriptor.id
    return repository, registry, engine, owner, executor, transfer, record, challenge


async def answer(engine, transfer_id, challenge):
    await engine.submit_input(transfer_id, challenge.id, "username_password", {"username": "u", "password": "p"})
    await engine.tick()


@pytest.mark.asyncio
async def test_route002_unhealthy_owner_still_owns_pending_input_continuation(routed):
    repository, registry, engine, owner, _, transfer, _, challenge = routed
    registry.mark_health(owner.descriptor.id, healthy=False)
    competitor = ImmediateHttpProvider("route-b", priority=100)
    registry.register_provider(competitor)
    await answer(engine, transfer.id, challenge)
    assert owner.continuation_calls == 1
    assert competitor.resolve_calls == 0
    assert (await repository.get(transfer.id)).state == TransferState.TRANSFERRING


@pytest.mark.asyncio
async def test_route002_changed_applicability_cannot_strand_existing_owner(routed):
    repository, registry, engine, owner, _, transfer, _, challenge = routed
    owner.facts = ProviderApplicability()
    generic = ImmediateHttpProvider("generic-http", priority=100, facts=ProviderApplicability(generic_schemes=frozenset({"http"})))
    registry.register_provider(generic)
    assert registry.provider_for(TransferRequest("http", "http://owned.example/new.bin")).descriptor.id == "generic-http"
    await answer(engine, transfer.id, challenge)
    assert owner.continuation_calls == 1
    assert generic.resolve_calls == 0
    assert (await repository.get(transfer.id)).state == TransferState.TRANSFERRING


@pytest.mark.asyncio
async def test_route002_new_specialized_provider_does_not_compete_for_existing_route(routed):
    repository, registry, engine, owner, _, transfer, _, challenge = routed
    competitor = ImmediateHttpProvider("route-b", priority=100)
    registry.register_provider(competitor)
    assert registry.provider_for(TransferRequest("http", "http://owned.example/new.bin")).descriptor.id == "route-b"
    await answer(engine, transfer.id, challenge)
    assert owner.continuation_calls == 1
    assert competitor.resolve_calls == 0
    assert (await repository.get(transfer.id)).state == TransferState.TRANSFERRING


@pytest.mark.asyncio
async def test_route002_generic_preference_for_new_request_does_not_reopen_current_route(routed):
    repository, registry, engine, owner, _, transfer, _, challenge = routed
    owner.facts = ProviderApplicability()
    generic = ImmediateHttpProvider("generic-http", priority=100, facts=ProviderApplicability(generic_schemes=frozenset({"http"})))
    registry.register_provider(generic)
    await answer(engine, transfer.id, challenge)
    assert owner.continuation_calls == 1
    assert generic.resolve_calls == 0
    assert (await repository.get(transfer.id)).state == TransferState.TRANSFERRING


@pytest.mark.asyncio
async def test_route002_persisted_challenge_continues_through_owner_after_restart(routed):
    repository, registry, engine, owner, _, transfer, _, challenge = routed
    owner.facts = ProviderApplicability()
    registry.mark_health(owner.descriptor.id, healthy=False)
    restarted = TransferEngine(repository, registry, download_root=engine.root, policy=engine.policy, clock=engine.clock)
    await restarted.initialize()
    current = await restarted.challenges.current(transfer.id)
    assert current and current.id == challenge.id
    await answer(restarted, transfer.id, current)
    assert owner.continuation_calls == 1
    assert (await repository.get(transfer.id)).state == TransferState.TRANSFERRING


@pytest.mark.asyncio
async def test_route002_challenge_owner_conflict_fails_closed_before_secret_consumption(routed):
    repository, registry, engine, owner, _, transfer, record, challenge = routed
    attacker = AuthHttpProvider("route-b", priority=100)
    registry.register_provider(attacker)
    async with database.get_db() as db:
        await db.execute("UPDATE transfer_input_challenges SET integration_id=? WHERE challenge_id=?", ("route-b", challenge.id))
        await db.commit()
    conflict = await engine.challenges.current(transfer.id)
    assert conflict.integration_id == "route-b"
    await engine.submit_input(transfer.id, conflict.id, "username_password", {"username": "secret-user", "password": "secret-pass"})
    await engine.tick()
    assert owner.continuation_calls == 0
    assert attacker.continuation_calls == 0
    assert await engine.challenges.current(transfer.id) is None
    assert await repository.bound_route_provider(record.id) == "route-a"
    assert (await repository.get(transfer.id)).state == TransferState.FAILED


@pytest.mark.asyncio
async def test_route002_admin_disable_is_explicit_hard_stop_not_reroute(routed):
    repository, registry, engine, owner, _, transfer, record, challenge = routed
    competitor = ImmediateHttpProvider("route-b", priority=100)
    registry.register_provider(competitor)
    owner.descriptor = replace(owner.descriptor, enabled=False)
    await answer(engine, transfer.id, challenge)
    assert owner.continuation_calls == 0
    assert competitor.resolve_calls == 0
    assert await repository.bound_route_provider(record.id) == "route-a"
    assert await engine.challenges.current(transfer.id) is None
    current = await repository.get(transfer.id)
    assert current.state == TransferState.FAILED
    assert current.error and current.error.category.value == "provider_unavailable"
