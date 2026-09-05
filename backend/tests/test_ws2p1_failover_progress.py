"""WS2 P1 alternate failover and execution-discovered progress qualification."""
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from providers.general_http.provider import GeneralHttpProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Recovery, Retryability, Stage
from transfers.models import (
    Capability, Endpoint, ExecutionObservation, ExecutionState, IntegrationDescriptor,
    IntegrityMetadata, ResolutionResult, ResourceState, SourceIdentity, TransferCandidate,
    TransferProgress, TransferRequest, TransferState,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


_SHARED_SHA256 = "a4c3ed04a95a3da14a9d235c83d868bed7c0f45cf7f3faa751ee8f50598d2211"


class EquivalentParcelProvider(ParcelProvider):
    def candidate(self, name="same.bin", *, payload="parcel"):
        return replace(
            super().candidate(name, payload=f"shared:{name}"),
            integrity=(IntegrityMetadata("sha256", _SHARED_SHA256),),
            source_identity=SourceIdentity("parcel-source", f"{self.descriptor.id}:shared:{name}"),
        )

    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        name = request.name or "same.bin"
        return ResolutionResult(ResourceState.AVAILABLE, (self.candidate(name, payload=request.payload),))

    async def refresh(self, candidate):
        payload = candidate.refresh_request.payload if candidate.refresh_request else None
        self.calls.append(("refresh_request", payload))
        return ResolutionResult(ResourceState.AVAILABLE, (replace(candidate, expires_at=None),))


class NoProgressMemoryExecutor(MemoryExecutor):
    async def start(self, request, handle):
        assert await self.authorize(handle, "start")
        self.calls.append(("start", handle))
        error = self.start_errors.pop(0) if self.start_errors else None
        result = ExecutionObservation(
            handle,
            ExecutionState.FAILED if error else ExecutionState.TRANSFERRING,
            TransferProgress(4, 0, 1),
            (request.target,),
            error,
        )
        self.jobs[handle.attempt_id] = result
        return result


class RuntimeHttpExecutor(MemoryExecutor):
    descriptor = IntegrationDescriptor(
        "runtime-http",
        "Runtime HTTP",
        frozenset({Capability.PAUSE, Capability.RESUME, Capability.RECONCILE}),
        schemes=frozenset({"http", "https"}),
    )

    def __init__(self, authorize=None, *, total=10, completed=2):
        super().__init__(authorize)
        self.total = total
        self.completed = completed

    async def start(self, request, handle):
        assert await self.authorize(handle, "start")
        self.calls.append(("start", handle))
        result = ExecutionObservation(
            handle,
            ExecutionState.TRANSFERRING,
            TransferProgress(self.total, self.completed, 3),
            (request.target,),
        )
        self.jobs[handle.attempt_id] = result
        return result

    def finish(self, handle, *, materialize=True):
        current = self.jobs[handle.attempt_id]
        if materialize:
            target = Path(current.paths[0])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x" * max(1, self.total))
        self.jobs[handle.attempt_id] = replace(
            current,
            state=ExecutionState.SUCCEEDED,
            progress=TransferProgress(self.total, self.total, 0),
        )


class ContradictoryMemoryExecutor(NoProgressMemoryExecutor):
    async def start(self, request, handle):
        assert await self.authorize(handle, "start")
        self.calls.append(("start", handle))
        result = ExecutionObservation(
            handle,
            ExecutionState.TRANSFERRING,
            TransferProgress(8, 2, 1),
            (request.target,),
        )
        self.jobs[handle.attempt_id] = result
        return result


class MultiUnknownProvider:
    descriptor = IntegrationDescriptor(
        "multi-unknown",
        "Multi unknown",
        frozenset({Capability.RESOLVE}),
        request_types=frozenset({"multi"}),
    )

    async def resolve(self, request):
        return ResolutionResult(
            ResourceState.AVAILABLE,
            (
                TransferCandidate(
                    request.name or "unknown.bin",
                    (Endpoint("memory", "memory:first"),),
                    provider_id=self.descriptor.id,
                    source_identity=SourceIdentity("multi", "first"),
                ),
                TransferCandidate(
                    request.name or "unknown.bin",
                    (Endpoint("memory", "memory:second"),),
                    provider_id=self.descriptor.id,
                    source_identity=SourceIdentity("multi", "second"),
                ),
            ),
        )


async def build_engine(tmp_path, monkeypatch, providers, executor, *, policy=None):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    for provider in providers:
        registry.register_provider(provider)
    executor.authorize = repository.authorize_execution
    registry.register_executor(executor)
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "payloads"),
        policy=policy or TransferPolicy(retry_delay=0, adoption_stability_seconds=0),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return engine, repository, registry


@pytest.mark.asyncio
async def test_recovery_hierarchy_retries_refreshes_then_fails_over_forward_only(tmp_path, monkeypatch):
    first = EquivalentParcelProvider("provider-a")
    second = EquivalentParcelProvider("provider-b")
    executor = NoProgressMemoryExecutor(None)
    policy = TransferPolicy(
        max_attempts=3,
        retry_delay=0,
        max_retry_delay=10,
        adoption_stability_seconds=0,
        max_active_executions=8,
        resolution_concurrency=8,
    )
    engine, repository, registry = await build_engine(
        tmp_path, monkeypatch, (first, second), executor, policy=policy,
    )

    canonical = await engine.submit(
        (TransferRequest("parcel", "original-a", name="same.bin", preferred_provider="provider-a"),),
        deduplicate=False,
    )
    await engine.resolve_pending()
    source = await engine.submit(
        (TransferRequest("parcel", "original-b", name="same.bin", preferred_provider="provider-b"),),
        deduplicate=False,
    )
    await engine.resolve_pending()

    artifact = (await repository.artifacts(canonical.id))[0]
    assert [item.provider_id for item in artifact.candidates] == ["provider-a", "provider-b"]
    assert (await repository.get(source.id)).state == TransferState.CONSOLIDATED

    failure = NormalizedError(
        Domain.NETWORK,
        Category.REMOTE_READ_FAILED,
        Stage.EXECUTION,
        Retryability.BACKOFF,
        Recovery.TRY_ALTERNATE_CANDIDATE,
        origin=Origin.REMOTE_SOURCE,
        operator_action_required=False,
    )

    await engine.reconcile_executions()
    artifact = (await repository.artifacts(canonical.id))[0]
    first_attempt = artifact.execution
    executor.jobs[first_attempt.attempt_id] = replace(
        executor.jobs[first_attempt.attempt_id], state=ExecutionState.FAILED, error=failure,
    )
    await engine.reconcile_executions()
    artifact = (await repository.artifacts(canonical.id))[0]
    assert artifact.selected == 0
    assert artifact.state == "queued"
    assert artifact.execution is None

    await engine.reconcile_executions()
    artifact = (await repository.artifacts(canonical.id))[0]
    second_attempt = artifact.execution
    executor.jobs[second_attempt.attempt_id] = replace(
        executor.jobs[second_attempt.attempt_id], state=ExecutionState.FAILED, error=failure,
    )
    await engine.reconcile_executions()
    artifact = (await repository.artifacts(canonical.id))[0]
    assert artifact.selected == 0
    assert artifact.state == "refresh_pending"

    await engine.reconcile_executions()
    assert ("refresh_request", "original-a") in first.calls
    artifact = (await repository.artifacts(canonical.id))[0]
    assert artifact.selected == 0
    assert artifact.state == "queued"

    await engine.reconcile_executions()
    artifact = (await repository.artifacts(canonical.id))[0]
    third_attempt = artifact.execution
    Path(artifact.target).parent.mkdir(parents=True, exist_ok=True)
    Path(artifact.target).write_bytes(b"old")
    Path(artifact.target + ".memory-progress").write_bytes(b"resume")
    executor.jobs[third_attempt.attempt_id] = replace(
        executor.jobs[third_attempt.attempt_id], state=ExecutionState.FAILED, error=failure,
    )
    await engine.reconcile_executions()
    artifact = (await repository.artifacts(canonical.id))[0]
    assert artifact.selected == 1
    assert artifact.state == "queued"
    assert artifact.execution is None
    assert artifact.candidates[artifact.selected].provider_id == "provider-b"
    assert not Path(artifact.target).exists()
    assert not Path(artifact.target + ".memory-progress").exists()

    restarted_repository = TransferRepository()
    restarted = TransferEngine(
        restarted_repository,
        registry,
        download_root=engine.root,
        policy=policy,
        clock=lambda: 1000.0,
    )
    await restarted.initialize()
    persisted = (await restarted_repository.artifacts(canonical.id))[0]
    assert persisted.selected == 1
    assert persisted.execution is None
    await restarted.reconcile_executions()
    persisted = (await restarted_repository.artifacts(canonical.id))[0]
    assert persisted.execution is not None
    assert persisted.candidates[persisted.selected].provider_id == "provider-b"


@pytest.mark.asyncio
async def test_disabled_current_candidate_fails_over_without_refreshing_it(tmp_path, monkeypatch):
    first = EquivalentParcelProvider("provider-a")
    second = EquivalentParcelProvider("provider-b")
    executor = NoProgressMemoryExecutor(None)
    engine, repository, _registry = await build_engine(tmp_path, monkeypatch, (first, second), executor)

    canonical = await engine.submit(
        (TransferRequest("parcel", "a", name="same.bin", preferred_provider="provider-a"),), deduplicate=False,
    )
    await engine.resolve_pending()
    await engine.submit(
        (TransferRequest("parcel", "b", name="same.bin", preferred_provider="provider-b"),), deduplicate=False,
    )
    await engine.resolve_pending()
    first.descriptor = replace(first.descriptor, enabled=False)

    await engine.reconcile_executions()
    artifact = (await repository.artifacts(canonical.id))[0]
    assert artifact.selected == 1
    assert artifact.execution is None
    assert not any(call[0] == "refresh_request" for call in first.calls)


@pytest.mark.asyncio
async def test_unknown_execution_authority_vetoes_candidate_switch_and_survives_restart(tmp_path, monkeypatch):
    provider = MultiUnknownProvider()
    executor = NoProgressMemoryExecutor(None)
    engine, repository, registry = await build_engine(tmp_path, monkeypatch, (provider,), executor)
    transfer = await engine.submit((TransferRequest("multi", "x", name="unknown.bin"),), deduplicate=False)
    await engine.tick()
    artifact = (await repository.artifacts(transfer.id))[0]
    handle = artifact.execution
    unknown = ExecutionObservation(
        handle,
        ExecutionState.UNKNOWN,
        TransferProgress(0, 0, 0),
        (artifact.target,),
        NormalizedError(
            Domain.EXECUTOR,
            Category.EXECUTOR_UNAVAILABLE,
            Stage.RECONCILIATION,
            Retryability.BACKOFF,
            Recovery.RECONCILE,
            operator_action_required=False,
        ),
    )
    await repository.execution(unknown)
    assert await repository.transition_recovery(
        artifact.id, "queued", selected=1, expected_bytes=0, reset_budget=True,
    ) is False
    current = (await repository.artifacts(transfer.id))[0]
    assert current.selected == 0
    assert current.execution == handle

    restarted_repository = TransferRepository()
    restarted = TransferEngine(
        restarted_repository,
        registry,
        download_root=engine.root,
        policy=engine.policy,
        clock=lambda: 1000.0,
    )
    await restarted.initialize()
    persisted = (await restarted_repository.artifacts(transfer.id))[0]
    assert persisted.selected == 0
    assert persisted.execution == handle
    assert (await restarted_repository.executions(transfer.id))[0].state == "unknown"


@pytest.mark.asyncio
async def test_runtime_total_size_is_accepted_in_core_and_drives_transfer_and_file_progress(tmp_path, monkeypatch):
    provider = GeneralHttpProvider()
    executor = RuntimeHttpExecutor(total=10, completed=2)
    engine, repository, registry = await build_engine(tmp_path, monkeypatch, (provider,), executor)
    transfer = await engine.submit(
        (TransferRequest("https", "https://example.test/runtime.bin", name="runtime.bin"),), deduplicate=False,
    )
    await engine.tick()

    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.candidates[0].expected_bytes == 0
    assert artifact.expected_bytes == 10
    assert (await repository.get(transfer.id)).progress == 20.0
    details = await repository.presentation(transfer.id, details=True)
    assert details["files"][0]["size_bytes"] == 10
    assert details["files"][0]["progress"] == 20.0

    restarted_repository = TransferRepository()
    restarted = TransferEngine(
        restarted_repository,
        registry,
        download_root=engine.root,
        policy=engine.policy,
        clock=lambda: 1000.0,
    )
    await restarted.initialize()
    persisted = (await restarted_repository.artifacts(transfer.id))[0]
    assert persisted.expected_bytes == 10
    assert (await restarted_repository.presentation(transfer.id, details=True))["files"][0]["progress"] == 20.0


@pytest.mark.asyncio
async def test_runtime_total_does_not_override_known_artifact_size(tmp_path, monkeypatch):
    provider = ParcelProvider()
    executor = ContradictoryMemoryExecutor(None)
    engine, repository, _registry = await build_engine(tmp_path, monkeypatch, (provider,), executor)
    transfer = await engine.submit((TransferRequest("parcel", "known", name="known.bin"),), deduplicate=False)
    await engine.tick()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.expected_bytes == 4
    assert artifact.candidates[0].expected_bytes == 4
    details = await repository.presentation(transfer.id, details=True)
    assert details["files"][0]["size_bytes"] == 4
    assert details["files"][0]["progress"] == 50.0


@pytest.mark.asyncio
async def test_zero_runtime_total_remains_backward_compatible(tmp_path, monkeypatch):
    provider = GeneralHttpProvider()
    executor = RuntimeHttpExecutor(total=0, completed=0)
    engine, repository, _registry = await build_engine(tmp_path, monkeypatch, (provider,), executor)
    transfer = await engine.submit(
        (TransferRequest("https", "https://example.test/unknown.bin", name="unknown.bin"),), deduplicate=False,
    )
    await engine.tick()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.expected_bytes == 0
    assert (await repository.get(transfer.id)).state != TransferState.FAILED
    details = await repository.presentation(transfer.id, details=True)
    assert details["files"][0]["size_bytes"] == 0
    assert details["files"][0]["progress"] == 0
