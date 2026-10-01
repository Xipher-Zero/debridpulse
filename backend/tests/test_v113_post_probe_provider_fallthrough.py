"""DP 1.0.13 Generalized Extension A -- post-probe provider fallthrough.

A provider whose claim on a request is *conditional* (``ProviderApplicability
.conditional``) proves applicability by a probe. Only positive evidence that
the resource is not its interpretation (the server answered, and described the
path through no discovery semantics: ``RemoteObjectKind.OPAQUE``) lets it
decline (``ResolutionResult.declined``). Core -- never the provider -- then
continues the SAME established competition with the next otherwise-eligible
provider. Every failure (network, TLS, authentication, authorization, rate
limit, transient server error, malformed answer) keeps its existing meaning
and never hands the request over, and a provider that has already resolved a
request can never decline it: bound routes are not reopened.

Everything here is provider-neutral: two fake providers over the unrelated
``vault`` transport, and an in-memory executor. No WebDAV name appears.
"""
from __future__ import annotations

from urllib.parse import urlsplit

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import VaultExecutor
from transfers.applicability import ProviderApplicability
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.input_required import auth_required, username_password
from transfers.models import (
    Capability, DiscoveryRequest, DiscoveryResult, Endpoint, ExecutorCapabilities, InputField, InputMethod,
    IntegrationDescriptor, RemoteObjectKind, ResolutionResult, ResourceState, SourceIdentity, TransferCandidate,
    TransferRequest, TransferState,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

USER, PASSWORD = "probe-user-sentinel", "probe-password-sentinel"
CONTENT = b"four"


def _conditional(schemes):
    return ProviderApplicability(generic_schemes=frozenset(schemes), conditional=True)


class ProbingSource:
    """Conditional claimant: a slash-terminated ``vault`` URL is its reading
    only if discovery proves it; anything else it never claims."""

    def __init__(self, identity="probe-source"):
        self.descriptor = IntegrationDescriptor(
            identity, "Probing source", frozenset({Capability.RESOLVE}), request_types=frozenset({"vault"}))
        self.resolved, self.discovered = [], []

    def applicability_for(self, request):
        if str(request.payload).endswith("/"):
            return _conditional({"vault"})
        return ProviderApplicability()

    async def resolve(self, request):
        self.resolved.append(request.payload)
        return ResolutionResult(ResourceState.PREPARING, discovery=DiscoveryRequest(
            Endpoint("vault", request.payload), (InputMethod.USERNAME_PASSWORD,)))

    async def resolve_discovered(self, request, discovered):
        self.discovered.append(discovered.kind)
        if discovered.kind == RemoteObjectKind("opaque"):
            return ResolutionResult(ResourceState.UNKNOWN, declined=True)
        host = urlsplit(request.payload).hostname
        return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
            "probed.bin", (Endpoint("vault", request.payload + "probed.bin"),), expected_bytes=len(CONTENT),
            provider_id=self.descriptor.id, source_identity=SourceIdentity("host", host),
            accepted_input_methods=(InputMethod.USERNAME_PASSWORD,)),))


class PlainSource:
    """Unconditional claimant of every ``vault`` URL (it can never decline)."""

    applicability = ProviderApplicability(generic_schemes=frozenset({"vault"}))

    def __init__(self, identity="plain-source", *, decline=False):
        self.descriptor = IntegrationDescriptor(
            identity, "Plain source", frozenset({Capability.RESOLVE}), request_types=frozenset({"vault"}))
        self.resolved = []
        self.decline = decline

    async def resolve(self, request):
        self.resolved.append(request.payload)
        if self.decline:
            return ResolutionResult(ResourceState.UNKNOWN, declined=True)
        host = urlsplit(request.payload).hostname
        return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
            "plain.bin", (Endpoint("vault", request.payload),), expected_bytes=len(CONTENT),
            provider_id=self.descriptor.id, source_identity=SourceIdentity("host", host),
            accepted_input_methods=(InputMethod.USERNAME_PASSWORD,)),))


_FAILURES = {
    "timeout.example": (Domain.NETWORK, Category.CONNECTION_TIMEOUT, Retryability.BACKOFF),
    "refused.example": (Domain.NETWORK, Category.CONNECTION_REFUSED, Retryability.BACKOFF),
    "dns.example": (Domain.NETWORK, Category.DNS_FAILURE, Retryability.BACKOFF),
    "tls.example": (Domain.NETWORK, Category.TLS_FAILURE, Retryability.NEVER),
    "blocked.example": (Domain.SECURITY, Category.DESTINATION_BLOCKED, Retryability.NEVER),
    "denied.example": (Domain.RESOLUTION, Category.AUTHORIZATION_FAILED, Retryability.NEVER),
    "missing.example": (Domain.RESOLUTION, Category.SOURCE_NOT_FOUND, Retryability.NEVER),
    "limited.example": (Domain.NETWORK, Category.RATE_LIMITED, Retryability.BACKOFF),
    "busy.example": (Domain.NETWORK, Category.SOURCE_TEMPORARILY_UNAVAILABLE, Retryability.BACKOFF),
    "garbled.example": (Domain.RESOLUTION, Category.PROTOCOL_ERROR, Retryability.NEVER),
}


class ProbeTransport(VaultExecutor):
    """In-memory ``vault`` transport with read-only discovery.

    ``answers`` maps a host to what its server says: ``"opaque"`` (it answered
    and describes the path through no discovery semantics), ``"listed"`` (a
    described file), or -- via ``_FAILURES`` -- a definitive failure. A locked
    host asks for credentials until the right ones arrive."""

    descriptor = IntegrationDescriptor("probe-transport", "Probe transport", frozenset())
    capabilities = ExecutorCapabilities(candidate_sampling=True, per_execution_pause=True, transient_input=True,
                                        remote_discovery=True)

    def __init__(self, authorize, *, answers, **kwargs):
        super().__init__(authorize, **kwargs)
        self.answers = dict(answers)
        self.discoveries = []

    async def discover(self, subject, submitted=None, **_tree):
        host = urlsplit(subject.candidate.endpoints[0].address).hostname
        self.discoveries.append((host, submitted.value(InputField.USERNAME) if submitted else None))
        expected = self.locks.get(host)
        if expected is not None and (submitted is None or (
                submitted.value(InputField.USERNAME), submitted.value(InputField.PASSWORD)) != expected):
            return auth_required(username_password())
        answer = self.answers[host]
        if answer in _FAILURES:
            domain, category, retryability = _FAILURES[answer]
            raise TransferError(NormalizedError(domain, category, Stage.RESOLUTION, retryability=retryability,
                                                integration_id=self.descriptor.id))
        if answer == "opaque":
            return DiscoveryResult(kind=RemoteObjectKind("opaque"))
        return DiscoveryResult(kind=RemoteObjectKind.FILE, expected_bytes=len(CONTENT))

    async def start(self, request, handle):
        observed = await super().start(request, handle)
        if observed.error is None:
            self.finish(handle)
        return observed


@pytest_asyncio.fixture
async def arena(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fallthrough.sqlite3")
    await database.init_db()
    now = [1000.0]
    repository = TransferRepository()
    registry = IntegrationRegistry()
    policy = TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=5)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"), policy=policy,
                            clock=lambda: now[0])
    await engine.initialize()
    return repository, registry, engine, now


def _wire(arena, *, answers, locks=None, plain=None, probing=None):
    repository, registry, engine, now = arena
    probing = probing or ProbingSource()
    plain = plain or PlainSource()
    # Registered plain-first so no registration order can explain the result.
    registry.register_provider(plain)
    registry.register_provider(probing)
    hosts = set(answers)
    objects = {f"{host}/{path}": CONTENT for host in hosts for path in ("dir/", "dir/probed.bin")}
    transport = ProbeTransport(repository.authorize_execution, answers=answers, objects=objects,
                               locks=dict(locks or {}))
    registry.register_executor(transport)
    return repository, engine, probing, plain, transport, now


async def _drive(engine, repository, transfer_id, now, *, answers=(), count=40):
    queue, seen = list(answers), []
    for _ in range(count):
        now[0] += 5
        await engine.tick()
        current = await engine.challenges.current(transfer_id)
        if current is not None and current.id not in {item.id for item in seen}:
            seen.append(current)
            if queue:
                await engine.submit_input(transfer_id, current.id, "username_password",
                                          {"username": USER, "password": queue.pop(0)})
        if (await repository.get(transfer_id)).state in {TransferState.COMPLETED, TransferState.FAILED}:
            break
    return seen


async def _routes(transfer_id):
    async with database.get_db() as db:
        rows = await db.fetchall(
            """SELECT a.provider_id, a.state, p.outcome, p.transition_kind, p.transition_reason
               FROM route_attempt_provenance p JOIN resolution_attempts a ON a.id=p.resolution_attempt_id
               WHERE p.transfer_id=? ORDER BY p.ordinal""", (transfer_id,))
    return [dict(row) for row in rows]


async def _root(repository, transfer_id):
    return next(record for record in await repository.requests(transfer_id) if record.parent_id is None)


# ── routing: conditional claims get their probe first; declined ones are excluded ──

async def test_a_conditional_claim_is_ordered_before_an_unconditional_one_of_the_same_class():
    registry = IntegrationRegistry()
    registry.register_provider(PlainSource())
    registry.register_provider(ProbingSource())
    request = TransferRequest("vault", "vault://opaque.example/dir/")
    assert [item.descriptor.id for item in registry.eligible_providers(request)] == ["probe-source", "plain-source"]
    # Without a conditional claim the established ordering is exactly unchanged.
    plain = TransferRequest("vault", "vault://opaque.example/dir/file.bin")
    assert [item.descriptor.id for item in registry.eligible_providers(plain)] == ["plain-source"]


async def test_core_continues_the_same_competition_without_a_declined_provider():
    registry = IntegrationRegistry()
    registry.register_provider(PlainSource())
    registry.register_provider(ProbingSource())
    request = TransferRequest("vault", "vault://opaque.example/dir/")
    assert registry.provider_for(request).descriptor.id == "probe-source"
    assert registry.provider_for(request, declined=frozenset({"probe-source"})).descriptor.id == "plain-source"
    with pytest.raises(TransferError) as raised:
        registry.provider_for(request, declined=frozenset({"probe-source", "plain-source"}))
    assert raised.value.error.category == Category.UNSUPPORTED_REQUEST


# ── positive non-applicability evidence hands the request over ────────────────

async def test_a_positive_decline_hands_the_request_to_the_next_provider(arena):
    repository, engine, probing, plain, transport, now = _wire(arena, answers={"opaque.example": "opaque"})
    transfer = await engine.submit((TransferRequest("vault", "vault://opaque.example/dir/"),), deduplicate=False)
    await _drive(engine, repository, transfer.id, now)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert probing.resolved == ["vault://opaque.example/dir/"] and plain.resolved == ["vault://opaque.example/dir/"]
    routes = await _routes(transfer.id)
    assert [(item["provider_id"], item["outcome"]) for item in routes] == [
        ("probe-source", "declined"), ("plain-source", "completed")]
    # Details can explain the hand-over from durable provenance alone.
    assert (routes[1]["transition_kind"], routes[1]["transition_reason"]) == ("provider_change", "provider_declined")
    # The request's route owner is now the provider that took it; a decline is
    # not a failure and spends no resolution attempt.
    root = await _root(repository, transfer.id)
    assert await repository.bound_route_provider(root.id) == "plain-source"
    assert root.error is None and root.attempts == 1


async def test_a_decline_after_answered_input_hands_over_and_reuses_the_accepted_material(arena):
    """A protected server that turns out not to be the probing provider's
    reading: the operator answered once, and the next provider's work on the
    same scope is satisfied by that same accepted material -- no second ask."""
    repository, engine, probing, plain, transport, now = _wire(
        arena, answers={"locked.example": "opaque"}, locks={"locked.example": (USER, PASSWORD)})
    transfer = await engine.submit((TransferRequest("vault", "vault://locked.example/dir/"),), deduplicate=False)
    seen = await _drive(engine, repository, transfer.id, now, answers=[PASSWORD])
    assert len(seen) == 1 and seen[0].integration_id == "probe-source"
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert plain.resolved == ["vault://locked.example/dir/"]
    assert [(item["provider_id"], item["outcome"]) for item in await _routes(transfer.id)] == [
        ("probe-source", "declined"), ("plain-source", "completed")]
    assert [user for _candidate, user in transport.input_starts] == [USER]


# ── nothing but positive evidence ever falls through ──────────────────────────

@pytest.mark.parametrize("host", sorted(_FAILURES))
async def test_a_failure_never_falls_through(arena, host):
    repository, engine, probing, plain, transport, now = _wire(arena, answers={host: host})
    transfer = await engine.submit((TransferRequest("vault", f"vault://{host}/dir/"),), deduplicate=False)
    await _drive(engine, repository, transfer.id, now, count=6)
    assert plain.resolved == [], f"{host} fell through to another provider"
    routes = await _routes(transfer.id)
    assert routes and {item["provider_id"] for item in routes} == {"probe-source"}
    assert "declined" not in {item["outcome"] for item in routes}
    root = await _root(repository, transfer.id)
    assert root.error is not None and root.error.category == _FAILURES[host][1]


async def test_an_authentication_requirement_never_falls_through(arena):
    repository, engine, probing, plain, transport, now = _wire(
        arena, answers={"locked.example": "opaque"}, locks={"locked.example": (USER, PASSWORD)})
    transfer = await engine.submit((TransferRequest("vault", "vault://locked.example/dir/"),), deduplicate=False)
    seen = await _drive(engine, repository, transfer.id, now, count=6)
    assert len(seen) == 1 and seen[0].integration_id == "probe-source"
    assert plain.resolved == []


async def test_a_rejected_answer_never_falls_through(arena):
    repository, engine, probing, plain, transport, now = _wire(
        arena, answers={"locked.example": "opaque"}, locks={"locked.example": (USER, PASSWORD)})
    transfer = await engine.submit((TransferRequest("vault", "vault://locked.example/dir/"),), deduplicate=False)
    await _drive(engine, repository, transfer.id, now, answers=["wrong-password"], count=8)
    assert plain.resolved == []
    assert "declined" not in {item["outcome"] for item in await _routes(transfer.id)}


async def test_a_positive_probe_keeps_the_probing_provider(arena):
    repository, engine, probing, plain, transport, now = _wire(arena, answers={"listed.example": "listed"})
    transfer = await engine.submit((TransferRequest("vault", "vault://listed.example/dir/"),), deduplicate=False)
    await _drive(engine, repository, transfer.id, now)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert plain.resolved == []
    assert [(item["provider_id"], item["outcome"]) for item in await _routes(transfer.id)] == [
        ("probe-source", "completed")]


# ── the decline boundary ──────────────────────────────────────────────────────

async def test_an_unconditional_provider_can_never_decline(arena):
    repository, engine, probing, plain, transport, now = _wire(
        arena, answers={"opaque.example": "opaque"}, plain=PlainSource(decline=True))
    transfer = await engine.submit((TransferRequest("vault", "vault://opaque.example/dir/file.bin"),),
                                   deduplicate=False)
    await _drive(engine, repository, transfer.id, now, count=6)
    root = await _root(repository, transfer.id)
    assert root.error is not None and root.error.category == Category.INVALID_ADAPTER_RESPONSE
    assert "declined" not in {item["outcome"] for item in await _routes(transfer.id)}


async def test_a_bound_route_is_never_reopened_by_a_later_decline(arena):
    """Once the probing provider has resolved the request, its route is bound:
    a later re-resolution that now reads the server as opaque is an ordinary
    failure of that route, never a reason to select another provider."""
    repository, engine, probing, plain, transport, now = _wire(arena, answers={"flip.example": "listed"})
    transfer = await engine.submit((TransferRequest("vault", "vault://flip.example/dir/"),), deduplicate=False)
    await _drive(engine, repository, transfer.id, now)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    root = await _root(repository, transfer.id)
    # Re-enter resolution of the SAME bound request with the server now opaque.
    transport.answers["flip.example"] = "opaque"
    async with database.get_db() as db:
        await db.execute("UPDATE torrents SET status='queued' WHERE id=?", (transfer.id,))
        await db.execute("UPDATE transfer_requests SET state='pending',retry_at=0 WHERE id=?", (root.id,))
        await db.commit()
    await engine._resolve(await _root(repository, transfer.id))
    assert plain.resolved == []
    routes = await _routes(transfer.id)
    assert {item["provider_id"] for item in routes} == {"probe-source"}
    assert "declined" not in {item["outcome"] for item in routes}
    after = await _root(repository, transfer.id)
    assert after.error is not None and after.error.category == Category.RESOURCE_STATE_CONFLICT
    assert await repository.bound_route_provider(root.id) == "probe-source"


async def test_the_core_contract_names_no_concrete_provider():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for module in ("transfers/registry.py", "transfers/applicability.py", "transfers/_engine_base.py",
                   "transfers/engine.py", "transfers/_repository_base.py", "transfers/models.py"):
        text = (root / module).read_text().casefold()
        assert "webdav" not in text and "general_http" not in text, module
