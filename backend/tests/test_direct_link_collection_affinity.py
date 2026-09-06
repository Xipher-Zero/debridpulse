"""Corrective qualification for direct-link collection route affinity and terminal cleanup."""
from __future__ import annotations

from dataclasses import replace

import pytest

import db.database as database
import transfers.engine as engine_module
from executors.aria2.translation import native_failure
from fake_integrations import MemoryExecutor
from transfers.applicability import (
    ApplicabilityReadiness,
    HostClaim,
    HostClaimScope,
    ProviderApplicability,
)
from transfers.engine import TransferEngine
from transfers.errors import (
    Category,
    Domain,
    NormalizedError,
    Origin,
    Recovery,
    Retryability,
    Stage,
)
from transfers.models import (
    Artifact,
    Capability,
    Endpoint,
    IntegrationDescriptor,
    ResolutionResult,
    ResourceState,
    TransferCandidate,
    TransferRequest,
    TransferState,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository

pytestmark = pytest.mark.asyncio


class UrlFixtureProvider:
    """Provider-neutral URL fixture with explicit applicability and deterministic responses."""

    def __init__(
        self,
        identity: str,
        *,
        generic: bool = False,
        host: str | None = None,
        unresolved: bool = False,
        priority: int = 0,
    ):
        self.descriptor = IntegrationDescriptor(
            identity,
            identity,
            frozenset({Capability.RESOLVE}),
            request_types=frozenset({"http", "https"}),
            priority=priority,
        )
        claims = () if host is None else (
            HostClaim(host, HostClaimScope.DOMAIN, frozenset({"http", "https"})),
        )
        self.applicability = ProviderApplicability(
            generic_schemes=frozenset({"http", "https"}) if generic else frozenset(),
            specialized_hosts=claims,
            specialized=bool(host) or unresolved,
            readiness=(
                ApplicabilityReadiness.UNRESOLVED
                if unresolved else ApplicabilityReadiness.READY
            ),
        )
        self.calls: list[TransferRequest] = []
        self.fail_payloads: set[str] = set()
        self.no_candidate_payloads: set[str] = set()

    async def resolve(self, request: TransferRequest) -> ResolutionResult:
        self.calls.append(request)
        payload = str(request.payload)
        if payload in self.fail_payloads:
            return ResolutionResult(
                ResourceState.UNKNOWN,
                error=NormalizedError(
                    Domain.PROVIDER,
                    Category.SOURCE_NOT_FOUND,
                    Stage.RESOLUTION,
                    retryability=Retryability.NEVER,
                    recovery=Recovery.FAIL,
                    origin=Origin.REMOTE_SOURCE,
                    integration_id=self.descriptor.id,
                ),
            )
        if payload in self.no_candidate_payloads:
            return ResolutionResult(ResourceState.AVAILABLE)
        return ResolutionResult(
            ResourceState.AVAILABLE,
            (
                TransferCandidate(
                    request.name or "payload.bin",
                    (Endpoint("memory", f"memory:{payload}"),),
                    expected_bytes=4,
                    provider_id=self.descriptor.id,
                ),
            ),
        )


def request(host: str, name: str, *, preferred_provider: str | None = None) -> TransferRequest:
    return TransferRequest(
        "https",
        f"https://{host}/{name}",
        name=name,
        preferred_provider=preferred_provider,
    )


async def build_core(tmp_path, monkeypatch, name: str, *providers: UrlFixtureProvider):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / name)
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    for provider in providers:
        registry.register_provider(provider)
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "downloads"),
        policy=TransferPolicy(),
    )
    await engine.initialize()
    return repository, registry, executor, engine


async def submit_collection(engine: TransferEngine, *requests: TransferRequest):
    return await engine.submit(
        tuple(requests),
        name="collection",
        source="direct_link",
        deduplicate=False,
    )


@pytest.mark.parametrize("special_first", [True, False])
async def test_one_specialized_sibling_binds_entire_collection_regardless_of_order(
    tmp_path, monkeypatch, special_first
):
    specialized = UrlFixtureProvider("special", host="special.test")
    generic = UrlFixtureProvider("generic", generic=True)
    repository, _registry, _executor, engine = await build_core(
        tmp_path, monkeypatch, f"affinity-{special_first}.sqlite3", specialized, generic
    )
    special = request("special.test", "special.bin")
    ordinary = request("ordinary.test", "ordinary.bin")
    submitted = (special, ordinary) if special_first else (ordinary, special)
    transfer = await submit_collection(engine, *submitted)

    await engine.resolve_pending()

    assert await repository.collection_route_provider(transfer.id) == "special"
    assert sorted(str(item.payload) for item in specialized.calls) == sorted(
        str(item.payload) for item in submitted
    )
    assert generic.calls == []
    details = await repository.presentation(transfer.id, details=True)
    assert [item["provider_id"] for item in details["route_attempts"]] == [
        "special", "special"
    ]


async def test_all_generic_collection_preserves_per_request_generic_routing(tmp_path, monkeypatch):
    specialized = UrlFixtureProvider("special", host="special.test")
    generic = UrlFixtureProvider("generic", generic=True)
    repository, _registry, _executor, engine = await build_core(
        tmp_path, monkeypatch, "all-generic.sqlite3", specialized, generic
    )
    transfer = await submit_collection(
        engine,
        request("one.test", "one.bin"),
        request("two.test", "two.bin"),
    )

    await engine.resolve_pending()

    assert await repository.collection_route_provider(transfer.id) is None
    assert specialized.calls == []
    assert len(generic.calls) == 2


async def test_unresolved_specialized_readiness_holds_whole_collection_then_binds(
    tmp_path, monkeypatch
):
    specialized = UrlFixtureProvider("special", unresolved=True)
    generic = UrlFixtureProvider("generic", generic=True)
    repository, _registry, _executor, engine = await build_core(
        tmp_path, monkeypatch, "unresolved.sqlite3", specialized, generic
    )
    transfer = await submit_collection(
        engine,
        request("ordinary.test", "ordinary.bin"),
        request("special.test", "special.bin"),
    )

    await engine.resolve_pending()
    pending = await repository.requests(transfer.id)
    assert specialized.calls == []
    assert generic.calls == []
    assert all(item.state == "pending" and item.attempts == 0 for item in pending)
    assert await repository.collection_route_provider(transfer.id) is None

    specialized.applicability = ProviderApplicability(
        specialized_hosts=(
            HostClaim("special.test", HostClaimScope.DOMAIN, frozenset({"https"})),
        )
    )
    await engine.resolve_pending()

    assert await repository.collection_route_provider(transfer.id) == "special"
    assert len(specialized.calls) == 2
    assert generic.calls == []


@pytest.mark.parametrize("unavailable", ["disabled", "unhealthy"])
async def test_specialized_unavailable_before_binding_allows_generic(
    tmp_path, monkeypatch, unavailable
):
    specialized = UrlFixtureProvider("special", host="special.test")
    generic = UrlFixtureProvider("generic", generic=True)
    repository, registry, _executor, engine = await build_core(
        tmp_path, monkeypatch, f"prebind-{unavailable}.sqlite3", specialized, generic
    )
    if unavailable == "disabled":
        specialized.descriptor = replace(specialized.descriptor, enabled=False)
    else:
        registry.mark_health("special", healthy=False)
    transfer = await submit_collection(
        engine,
        request("special.test", "special.bin"),
        request("ordinary.test", "ordinary.bin"),
    )

    await engine.resolve_pending()

    assert await repository.collection_route_provider(transfer.id) is None
    assert specialized.calls == []
    assert len(generic.calls) == 2


async def test_collection_affinity_survives_repository_and_engine_restart(tmp_path, monkeypatch):
    specialized = UrlFixtureProvider("special", host="special.test")
    generic = UrlFixtureProvider("generic", generic=True)
    repository, _registry, _executor, engine = await build_core(
        tmp_path, monkeypatch, "restart.sqlite3", specialized, generic
    )
    transfer = await submit_collection(
        engine,
        request("special.test", "special.bin"),
        request("ordinary.test", "ordinary.bin"),
    )
    assert await engine._prepare_collection_affinity() == set()
    assert await repository.collection_route_provider(transfer.id) == "special"
    assert specialized.calls == [] and generic.calls == []

    restarted_repository = TransferRepository()
    restarted_registry = IntegrationRegistry()
    restarted_specialized = UrlFixtureProvider("special", host="special.test")
    restarted_generic = UrlFixtureProvider("generic", generic=True)
    restarted_registry.register_provider(restarted_specialized)
    restarted_registry.register_provider(restarted_generic)
    restarted_registry.register_executor(MemoryExecutor(restarted_repository.authorize_execution))
    restarted_engine = TransferEngine(
        restarted_repository,
        restarted_registry,
        download_root=str(tmp_path / "downloads"),
        policy=TransferPolicy(),
    )
    await restarted_engine.initialize()
    await restarted_engine.resolve_pending()

    assert await restarted_repository.collection_route_provider(transfer.id) == "special"
    assert len(restarted_specialized.calls) == 2
    assert restarted_generic.calls == []


@pytest.mark.parametrize("unavailable", ["disabled", "unhealthy"])
async def test_bound_collection_never_reopens_generic_competition(
    tmp_path, monkeypatch, unavailable
):
    specialized = UrlFixtureProvider("special", host="special.test")
    generic = UrlFixtureProvider("generic", generic=True)
    repository, registry, _executor, engine = await build_core(
        tmp_path, monkeypatch, f"postbind-{unavailable}.sqlite3", specialized, generic
    )
    transfer = await submit_collection(
        engine,
        request("special.test", "special.bin"),
        request("ordinary.test", "ordinary.bin"),
    )
    assert await engine._prepare_collection_affinity() == set()
    assert await repository.collection_route_provider(transfer.id) == "special"
    if unavailable == "disabled":
        specialized.descriptor = replace(specialized.descriptor, enabled=False)
    else:
        registry.mark_health("special", healthy=False)

    await engine.resolve_pending()

    assert generic.calls == []
    assert await repository.collection_route_provider(transfer.id) == "special"
    requests = await repository.requests(transfer.id)
    assert all(item.error and item.error.category == Category.PROVIDER_UNAVAILABLE for item in requests)


async def test_multiple_specialized_providers_use_neutral_deterministic_policy():
    alpha = UrlFixtureProvider("alpha", host="alpha.test", priority=5)
    beta = UrlFixtureProvider("beta", host="beta.test", priority=10)
    registry = IntegrationRegistry()
    registry.register_provider(alpha)
    registry.register_provider(beta)

    requests = (
        request("alpha.test", "a.bin"),
        request("beta.test", "b.bin"),
    )
    assert registry.collection_provider_for(requests) is beta
    assert registry.collection_provider_for(tuple(reversed(requests))) is beta

    preferred = (
        request("alpha.test", "a.bin", preferred_provider="alpha"),
        request("beta.test", "b.bin"),
    )
    assert registry.collection_provider_for(preferred) is alpha


async def test_single_link_direct_link_submission_keeps_existing_routing(tmp_path, monkeypatch):
    specialized = UrlFixtureProvider("special", host="special.test")
    generic = UrlFixtureProvider("generic", generic=True)
    repository, _registry, _executor, engine = await build_core(
        tmp_path, monkeypatch, "single.sqlite3", specialized, generic
    )
    transfer = await submit_collection(engine, request("special.test", "single.bin"))

    await engine.resolve_pending()

    assert await repository.collection_route_provider(transfer.id) is None
    assert len(specialized.calls) == 1
    assert generic.calls == []


async def test_historical_route_history_is_not_reinterpreted_by_new_affinity(tmp_path, monkeypatch):
    specialized = UrlFixtureProvider("special", unresolved=True)
    generic = UrlFixtureProvider("generic", generic=True)
    repository, _registry, _executor, engine = await build_core(
        tmp_path, monkeypatch, "historical.sqlite3", specialized, generic
    )
    transfer = await submit_collection(
        engine,
        request("one.test", "one.bin"),
        request("two.test", "two.bin"),
    )
    records = await repository.requests(transfer.id)
    attempt = await repository.begin_resolution(records[0].id, "generic")
    assert attempt is not None

    blocked = await engine._prepare_collection_affinity()

    assert blocked == set()
    assert await repository.collection_route_provider(transfer.id) is None
    assert await repository.bound_route_provider(records[0].id) == "generic"


async def test_mixed_source_outcomes_complete_when_one_canonical_payload_succeeds(
    tmp_path, monkeypatch
):
    specialized = UrlFixtureProvider("special", host="special.test")
    generic = UrlFixtureProvider("generic", generic=True)
    failed = request("ordinary.test", "failed.bin")
    specialized.fail_payloads.add(str(failed.payload))
    repository, _registry, executor, engine = await build_core(
        tmp_path, monkeypatch, "mixed-outcome.sqlite3", specialized, generic
    )
    transfer = await submit_collection(
        engine,
        request("special.test", "success.bin"),
        failed,
    )

    await engine.tick()
    artifacts = await repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    assert artifacts[0].execution is not None
    executor.finish(artifacts[0].execution)
    await engine.tick()

    current = await repository.get(transfer.id)
    requests = await repository.requests(transfer.id)
    assert current.state == TransferState.COMPLETED
    assert any(item.state == "failed" and item.error for item in requests)
    assert generic.calls == []


async def test_all_collection_sources_failed_produces_truthful_transfer_failure(tmp_path, monkeypatch):
    specialized = UrlFixtureProvider("special", host="special.test")
    generic = UrlFixtureProvider("generic", generic=True)
    first = request("special.test", "one.bin")
    second = request("ordinary.test", "two.bin")
    specialized.fail_payloads.update({str(first.payload), str(second.payload)})
    repository, _registry, _executor, engine = await build_core(
        tmp_path, monkeypatch, "all-failed.sqlite3", specialized, generic
    )
    transfer = await submit_collection(engine, first, second)

    await engine.resolve_pending()

    current = await repository.get(transfer.id)
    assert current.state == TransferState.FAILED
    assert current.error is not None
    assert current.error.category == Category.SOURCE_NOT_FOUND
    assert await repository.artifacts(transfer.id) == ()
    assert generic.calls == []


async def test_no_candidate_failure_never_creates_bogus_artifact(tmp_path, monkeypatch):
    specialized = UrlFixtureProvider("special", host="special.test")
    generic = UrlFixtureProvider("generic", generic=True)
    ordinary = request("ordinary.test", "none.bin")
    specialized.no_candidate_payloads.add(str(ordinary.payload))
    repository, _registry, _executor, engine = await build_core(
        tmp_path, monkeypatch, "no-candidate.sqlite3", specialized, generic
    )
    transfer = await submit_collection(
        engine,
        request("special.test", "special.bin"),
        ordinary,
    )

    await engine.resolve_pending()

    requests = await repository.requests(transfer.id)
    assert any(item.error and item.error.category == Category.NO_TRANSFER_CANDIDATE for item in requests)
    assert len(await repository.artifacts(transfer.id)) == 1
    assert generic.calls == []


async def test_unknown_aria2_failure_has_useful_primary_message_and_sanitized_diagnostic():
    error = native_failure(
        "987654",
        "native failure token=secret https://download.example/file?cap=opaque",
        secrets=("secret",),
    )

    assert error.category == Category.UNMAPPED_EXECUTOR_ERROR
    assert error.message == "Download failed"
    advanced = error.as_dict(diagnostics=True)
    assert advanced["native_code"] == "987654"
    assert "secret" not in advanced["diagnostic"]
    assert "download.example" not in advanced["diagnostic"]
    assert error.retryability == Retryability.UNKNOWN


class TerminalRepository:
    def __init__(self, result: bool = True):
        self.result = result
        self.calls = []

    async def transition_recovery(self, artifact_id, state, **kwargs):
        self.calls.append((artifact_id, state, kwargs))
        return self.result


async def test_terminal_remote_source_cleanup_occurs_only_after_writer_revocation(monkeypatch, tmp_path):
    repository = TerminalRepository()
    engine = object.__new__(TransferEngine)
    engine.repository = repository
    engine.root = str(tmp_path)
    engine.registry = IntegrationRegistry()
    artifact = Artifact(
        1, 1, "request", "payload.bin", str(tmp_path / "payload.bin"), 4,
        "error", (),
    )
    calls = []
    monkeypatch.setattr(engine, "_candidate_sidecars", lambda _artifact: (str(tmp_path / "payload.bin.aria2"),))
    monkeypatch.setattr(
        engine_module,
        "retire_partial",
        lambda root, target, sidecars: calls.append((root, target, sidecars, len(repository.calls))),
    )
    remote = NormalizedError(
        Domain.EXECUTOR,
        Category.SOURCE_NOT_FOUND,
        Stage.EXECUTION,
        retryability=Retryability.NEVER,
        recovery=Recovery.FAIL,
        origin=Origin.REMOTE_SOURCE,
    )

    assert await engine._terminal_recovery(artifact, remote)
    assert calls == [(
        str(tmp_path), artifact.target, (str(tmp_path / "payload.bin.aria2"),), 1
    )]

    calls.clear()
    local = NormalizedError(
        Domain.LOCAL_RESOURCE,
        Category.DISK_FULL,
        Stage.EXECUTION,
        retryability=Retryability.AFTER_RESOURCE_CHANGE,
        recovery=Recovery.REQUIRE_OPERATOR,
        origin=Origin.LOCAL_SYSTEM,
    )
    assert await engine._terminal_recovery(artifact, local)
    assert calls == []


async def test_terminal_cleanup_does_not_run_when_writer_revocation_is_rejected(monkeypatch, tmp_path):
    repository = TerminalRepository(result=False)
    engine = object.__new__(TransferEngine)
    engine.repository = repository
    engine.root = str(tmp_path)
    engine.registry = IntegrationRegistry()
    artifact = Artifact(
        1, 1, "request", "payload.bin", str(tmp_path / "payload.bin"), 4,
        "error", (),
    )
    calls = []
    monkeypatch.setattr(engine, "_candidate_sidecars", lambda _artifact: ())
    monkeypatch.setattr(engine_module, "retire_partial", lambda *args: calls.append(args))
    remote = NormalizedError(
        Domain.EXECUTOR,
        Category.SOURCE_NOT_FOUND,
        Stage.EXECUTION,
        retryability=Retryability.NEVER,
        recovery=Recovery.FAIL,
        origin=Origin.REMOTE_SOURCE,
    )

    assert not await engine._terminal_recovery(artifact, remote)
    assert calls == []
