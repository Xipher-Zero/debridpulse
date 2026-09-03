from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if text.count(old) != 1:
        raise SystemExit(f"{path}: expected one match, got {text.count(old)}")
    p.write_text(text.replace(old, new, 1))

REGISTRY = "backend/transfers/registry.py"
ENGINE = "backend/transfers/engine.py"
GATE = "backend/tests/post_audit_qualification.txt"
TEST = Path("backend/tests/test_second_pass_route002.py")

replace_once(
    REGISTRY,
    '''    def provider_for_bound_route(self, provider_id: str, request: TransferRequest) -> Provider:\n        """Recover an existing route without reopening global provider selection.\n\n        Applicability is an initial-routing fact. Once selected, the durable route\n        remains authoritative across retries and restart. Enablement/health retain\n        their existing admitted-work semantics, but neither may select a replacement.\n        """\n        provider = self.providers.get(provider_id)\n        if (provider is None or Capability.RESOLVE not in provider.descriptor.capabilities\n                or request.kind not in provider.descriptor.request_types):\n            raise TransferError(NormalizedError(\n                Domain.REQUEST, Category.UNSUPPORTED_CAPABILITY, Stage.RESOLUTION,\n                retryability=Retryability.NEVER, integration_id=provider_id,\n            ))\n        if not provider.descriptor.enabled:\n            raise TransferError(NormalizedError(\n                Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION,\n                retryability=Retryability.NEVER, recovery=Recovery.FAIL,\n                integration_id=provider_id,\n            ))\n        if provider_id in self._unhealthy:\n            raise TransferError(NormalizedError(\n                Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION,\n                retryability=Retryability.BACKOFF, recovery=Recovery.BACKOFF,\n                integration_id=provider_id,\n            ))\n        return provider\n''',
    '''    def _provider_for_bound_owner(self, provider_id: str, request: TransferRequest, *, require_health: bool) -> Provider:\n        provider = self.providers.get(provider_id)\n        if (provider is None or Capability.RESOLVE not in provider.descriptor.capabilities\n                or request.kind not in provider.descriptor.request_types):\n            raise TransferError(NormalizedError(\n                Domain.REQUEST, Category.UNSUPPORTED_CAPABILITY, Stage.RESOLUTION,\n                retryability=Retryability.NEVER, recovery=Recovery.FAIL, integration_id=provider_id,\n            ))\n        if not provider.descriptor.enabled:\n            # Administrative disablement is an explicit admitted-work hard stop;\n            # it never reopens provider competition for an existing route.\n            raise TransferError(NormalizedError(\n                Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION,\n                retryability=Retryability.NEVER, recovery=Recovery.FAIL,\n                integration_id=provider_id,\n            ))\n        if require_health and provider_id in self._unhealthy:\n            raise TransferError(NormalizedError(\n                Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION,\n                retryability=Retryability.BACKOFF, recovery=Recovery.BACKOFF,\n                integration_id=provider_id,\n            ))\n        return provider\n\n    def provider_for_bound_route(self, provider_id: str, request: TransferRequest) -> Provider:\n        """Recover an existing route without reopening global provider selection."""\n        return self._provider_for_bound_owner(provider_id, request, require_health=True)\n\n    def provider_for_bound_continuation(self, provider_id: str, request: TransferRequest) -> Provider:\n        """Continue provider-owned interaction through its persisted route owner.\n\n        Applicability and transient health can change while a human supplies input.\n        Neither fact is route ownership. Administrative disablement remains an\n        explicit hard stop, but no replacement provider is ever selected here.\n        """\n        return self._provider_for_bound_owner(provider_id, request, require_health=False)\n'''
)

replace_once(
    ENGINE,
    '''        eligible = {item.descriptor.id: item for item in self.registry.eligible_providers(record.request)}\n        provider = eligible.get(challenge.integration_id)\n        if not isinstance(provider, ProviderInputContinuation):\n            return\n        submitted = None\n        try:\n''',
    '''        submitted = None\n        bound_provider_id = await self.repository.bound_route_provider(record.id)\n        try:\n            # ROUTE-002: initial classification chooses an owner; persisted route\n            # state owns every continuation. Never recalculate global eligibility.\n            if not bound_provider_id or bound_provider_id != challenge.integration_id:\n                raise TransferError(self._error(\n                    Category.OWNERSHIP_CONFLICT, Stage.RESOLUTION, domain=Domain.LIFECYCLE,\n                    retryability=Retryability.NEVER, recovery=Recovery.FAIL,\n                    integration_id=bound_provider_id or challenge.integration_id,\n                ))\n            provider = self.registry.provider_for_bound_continuation(bound_provider_id, record.request)\n            if not isinstance(provider, ProviderInputContinuation):\n                raise TransferError(self._error(\n                    Category.UNSUPPORTED_CAPABILITY, Stage.RESOLUTION, domain=Domain.REQUEST,\n                    retryability=Retryability.NEVER, recovery=Recovery.FAIL,\n                    integration_id=bound_provider_id,\n                ))\n'''
)

replace_once(
    ENGINE,
    '''            attempt = ResolutionAttempt(challenge.operation_id, record.id, challenge.integration_id, "input_required")\n            await self._apply_resolution(record, attempt, provider, result, challenge=challenge)\n        except Exception as exc:\n            secrets = submitted.secret_values() if submitted else ()\n            error = exc.error if isinstance(exc, TransferError) else unknown_failure(\n                exc, integration_id=challenge.integration_id, domain=Domain.PROVIDER, stage=Stage.RESOLUTION, secrets=secrets)\n            attempt = ResolutionAttempt(challenge.operation_id, record.id, challenge.integration_id, "input_required")\n''',
    '''            attempt = ResolutionAttempt(challenge.operation_id, record.id, bound_provider_id, "input_required")\n            await self._apply_resolution(record, attempt, provider, result, challenge=challenge)\n        except Exception as exc:\n            secrets = submitted.secret_values() if submitted else ()\n            error = exc.error if isinstance(exc, TransferError) else unknown_failure(\n                exc, integration_id=bound_provider_id or challenge.integration_id, domain=Domain.PROVIDER, stage=Stage.RESOLUTION, secrets=secrets)\n            attempt = ResolutionAttempt(challenge.operation_id, record.id, bound_provider_id or challenge.integration_id, "input_required")\n'''
)

TEST.write_text(r'''"""Second-pass ROUTE-002 persisted route ownership contracts."""
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
''')

replace_once(
    GATE,
    '# STATE-001 + STATE-002\ntests/test_audit_remediation_state.py\ntests/test_second_pass_state002.py\n',
    '# STATE-001 + STATE-002 + ROUTE-002\ntests/test_audit_remediation_state.py\ntests/test_second_pass_state002.py\ntests/test_second_pass_route002.py\n',
)
