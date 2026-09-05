"""Workspace 1 Phase 1 cross-transfer canonical artifact qualification."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage
from transfers.models import (
    ArtifactFingerprint, ExecutionState, ResolutionResult, ResourceState,
    TransferRequest,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


class NamedParcelProvider(ParcelProvider):
    """Two unrelated providers that can resolve independent URLs to one payload."""

    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        if self.entered:
            self.entered.set()
            await self.release.wait()
        if self.responses:
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        name = request.name or "payload.bin"
        return ResolutionResult(
            ResourceState.AVAILABLE,
            (self.candidate(name, payload=f"shared:{name}"),),
        )


@pytest_asyncio.fixture
async def pair(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    first = NamedParcelProvider("provider-a")
    second = NamedParcelProvider("provider-b")
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(first)
    registry.register_provider(second)
    registry.register_executor(executor)
    now = [1000.0]
    policy = TransferPolicy(
        retry_delay=1,
        adoption_stability_seconds=0,
        max_active_executions=32,
        resolution_concurrency=32,
    )
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "payloads"),
        policy=policy,
        clock=lambda: now[0],
    )
    await engine.initialize()
    return SimpleNamespace(
        engine=engine,
        repository=repository,
        registry=registry,
        a=first,
        b=second,
        executor=executor,
        now=now,
    )


async def admit(pair, provider, payload, name="same.bin"):
    return await pair.engine.submit(
        (TransferRequest("parcel", payload, name=name, preferred_provider=provider.descriptor.id),),
        name=name,
        deduplicate=False,
    )


async def standby_rows(request_id=None):
    async with database.get_db() as db:
        if request_id:
            return await db.fetchall(
                "SELECT * FROM download_files WHERE request_id=? AND mirror_state='standby'",
                (request_id,),
            )
        return await db.fetchall("SELECT * FROM download_files WHERE mirror_state='standby' ORDER BY id")


@pytest.mark.asyncio
async def test_later_equivalent_transfer_attaches_to_established_writer(pair):
    first = await admit(pair, pair.a, "submission-a")
    await pair.engine.tick()
    primary = (await pair.repository.artifacts(first.id))[0]
    handle = primary.execution

    second = await admit(pair, pair.b, "submission-b")
    second_request = (await pair.repository.requests(second.id))[0]
    await pair.engine.tick()

    canonical = (await pair.repository.artifacts(first.id))[0]
    assert canonical.id == primary.id
    assert canonical.execution == handle
    assert [candidate.provider_id for candidate in canonical.candidates] == ["provider-a", "provider-b"]
    assert await pair.repository.artifacts(second.id) == ()
    standby = await standby_rows(second_request.id)
    assert len(standby) == 1
    assert standby[0]["mirror_group_id"] == primary.id
    assert len([call for call in pair.executor.calls if call[0] == "start"]) == 1


@pytest.mark.asyncio
async def test_same_name_and_size_without_fingerprint_remain_independent(pair, monkeypatch):
    first = await admit(pair, pair.a, "submission-a")
    await pair.engine.tick()

    async def unavailable(_candidate):
        return None

    monkeypatch.setattr(pair.executor, "fingerprint", unavailable)
    second = await admit(pair, pair.b, "submission-b")
    await pair.engine.tick()

    a = (await pair.repository.artifacts(first.id))[0]
    b = (await pair.repository.artifacts(second.id))[0]
    assert a.id != b.id
    assert a.target != b.target
    assert not await standby_rows((await pair.repository.requests(second.id))[0].id)


@pytest.mark.asyncio
async def test_fingerprint_mismatch_remains_independent(pair):
    first = await admit(pair, pair.a, "submission-a")
    await pair.engine.tick()
    pair.b.responses = [ResolutionResult(
        ResourceState.AVAILABLE,
        (pair.b.candidate("same.bin", payload="different-payload"),),
    )]
    second = await admit(pair, pair.b, "submission-b")
    await pair.engine.tick()
    assert len(await pair.repository.artifacts(first.id)) == 1
    assert len(await pair.repository.artifacts(second.id)) == 1


@pytest.mark.asyncio
async def test_incompatible_expected_size_remains_independent(pair):
    first = await admit(pair, pair.a, "submission-a")
    await pair.engine.tick()
    pair.b.responses = [ResolutionResult(
        ResourceState.AVAILABLE,
        (replace(pair.b.candidate("same.bin", payload="shared:same.bin"), expected_bytes=4000),),
    )]
    second = await admit(pair, pair.b, "submission-b")
    await pair.engine.tick()
    assert len(await pair.repository.artifacts(first.id)) == 1
    assert len(await pair.repository.artifacts(second.id)) == 1


@pytest.mark.asyncio
async def test_true_concurrent_tie_uses_durable_admission_order(pair):
    first = await admit(pair, pair.a, "submission-a")
    second = await admit(pair, pair.b, "submission-b")
    assert first.id < second.id

    await pair.engine.resolve_pending()
    await pair.engine.resolve_pending()

    first_artifacts = await pair.repository.artifacts(first.id)
    second_artifacts = await pair.repository.artifacts(second.id)
    assert len(first_artifacts) == 1
    assert second_artifacts == ()
    assert len(first_artifacts[0].candidates) == 2
    assert len(await pair.engine.canonical.canonical_artifacts()) == 1

    await pair.engine.reconcile_executions()
    assert len([call for call in pair.executor.calls if call[0] == "start"]) == 1


@pytest.mark.asyncio
async def test_resolution_speed_inversion_keeps_later_established_writer(pair):
    pair.a.entered, pair.a.release = asyncio.Event(), asyncio.Event()
    first = await admit(pair, pair.a, "slow-first")
    second = await admit(pair, pair.b, "fast-second")
    assert first.id < second.id

    resolving = asyncio.create_task(pair.engine.resolve_pending())
    await pair.a.entered.wait()
    async with asyncio.timeout(2):
        while True:
            second_artifacts = await pair.repository.artifacts(second.id)
            if second_artifacts:
                break
            await asyncio.sleep(0.01)
    second_primary = second_artifacts[0]
    assert second_primary.candidates[0].provider_id == "provider-b"

    pair.a.release.set()
    await resolving

    assert await pair.repository.artifacts(first.id) == ()
    winner = (await pair.repository.artifacts(second.id))[0]
    assert winner.id == second_primary.id
    assert [candidate.provider_id for candidate in winner.candidates] == ["provider-b", "provider-a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", ["complete", "cancel", "delete"])
async def test_slow_fingerprint_revalidates_owner_before_attachment(pair, monkeypatch, invalidate):
    first = await admit(pair, pair.a, "submission-a")
    await pair.engine.resolve_pending()
    primary = (await pair.repository.artifacts(first.id))[0]

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def blocked_fingerprint(candidate):
        nonlocal calls
        calls += 1
        if calls >= 2:
            entered.set()
        await release.wait()
        return ArtifactFingerprint(candidate.expected_bytes, "same-sampled-payload")

    monkeypatch.setattr(pair.executor, "fingerprint", blocked_fingerprint)
    second = await admit(pair, pair.b, "submission-b")
    materializing = asyncio.create_task(pair.engine.resolve_pending())
    await entered.wait()

    # Remote sampling must hold neither the path mutex nor a SQLite write
    # transaction.  Both can be acquired while the sampling calls are blocked.
    async with asyncio.timeout(1):
        async with pair.engine._paths_lock:
            pass
    async with database.get_db() as db:
        await asyncio.wait_for(db.execute("BEGIN IMMEDIATE"), timeout=1)
        await db.rollback()

    if invalidate == "complete":
        await pair.repository.artifact_state(primary.id, "completed")
    elif invalidate == "cancel":
        await pair.engine.cancel(first.id)
    else:
        await pair.engine.delete(first.id, remote=False)

    release.set()
    await materializing

    incoming = await pair.repository.artifacts(second.id)
    assert len(incoming) == 1
    second_request = (await pair.repository.requests(second.id))[0]
    assert not await standby_rows(second_request.id)


@pytest.mark.asyncio
async def test_seven_plus_seven_proven_mirrors_create_seven_canonical_artifacts(pair):
    first_requests = tuple(
        TransferRequest("parcel", f"a-{index}", name=f"file{index}.rar", preferred_provider="provider-a")
        for index in range(1, 8)
    )
    second_requests = tuple(
        TransferRequest("parcel", f"b-{index}", name=f"file{index}.rar", preferred_provider="provider-b")
        for index in range(1, 8)
    )
    first = await pair.engine.submit(first_requests, name="first-set", deduplicate=False)
    await pair.engine.resolve_pending()
    second = await pair.engine.submit(second_requests, name="second-set", deduplicate=False)
    await pair.engine.resolve_pending()

    canonicals = await pair.repository.artifacts(first.id)
    assert len(canonicals) == 7
    assert await pair.repository.artifacts(second.id) == ()
    assert len(await pair.engine.canonical.canonical_artifacts()) == 7
    assert all(len(artifact.candidates) == 2 for artifact in canonicals)
    assert len(await standby_rows()) == 7

    await pair.engine.reconcile_executions()
    assert len([call for call in pair.executor.calls if call[0] == "start"]) == 7


@pytest.mark.asyncio
async def test_paused_established_writer_is_not_resumed_by_alternate_arrival(pair):
    first = await admit(pair, pair.a, "submission-a")
    await pair.engine.tick()
    primary = (await pair.repository.artifacts(first.id))[0]
    handle = primary.execution
    await pair.engine.pause(first.id)
    assert pair.executor.jobs[handle.attempt_id].state == ExecutionState.PAUSED

    second = await admit(pair, pair.b, "submission-b")
    await pair.engine.tick()

    canonical = (await pair.repository.artifacts(first.id))[0]
    assert canonical.execution == handle
    assert len(canonical.candidates) == 2
    assert await pair.repository.artifacts(second.id) == ()
    assert pair.executor.jobs[handle.attempt_id].state == ExecutionState.PAUSED
    assert len([call for call in pair.executor.calls if call[0] == "start"]) == 1


@pytest.mark.asyncio
async def test_phase1_origin_linkage_survives_restart_without_inference(pair):
    first = await admit(pair, pair.a, "submission-a")
    await pair.engine.resolve_pending()
    second = await admit(pair, pair.b, "submission-b")
    await pair.engine.resolve_pending()
    primary = (await pair.repository.artifacts(first.id))[0]
    first_request = (await pair.repository.requests(first.id))[0]
    second_request = (await pair.repository.requests(second.id))[0]

    restarted = TransferEngine(
        TransferRepository(),
        pair.registry,
        download_root=pair.engine.root,
        policy=pair.engine.policy,
        clock=lambda: pair.now[0],
    )
    await restarted.initialize()
    origins = await restarted.canonical.origins(primary.id)

    assert {origin.request.id for origin in origins} == {first_request.id, second_request.id}
    assert {origin.contributing_transfer_id for origin in origins} == {first.id, second.id}
    assert {origin.provider_id for origin in origins} == {"provider-a", "provider-b"}
    assert all(origin.resolution_attempt_id for origin in origins)
    assert all(origin.candidate_id for origin in origins)


@pytest.mark.asyncio
async def test_foreign_alternate_refresh_uses_its_actual_originating_request(pair):
    first = await admit(pair, pair.a, "submission-a")
    await pair.engine.resolve_pending()
    second = await admit(pair, pair.b, "submission-b")
    await pair.engine.resolve_pending()
    primary = (await pair.repository.artifacts(first.id))[0]
    second_request = (await pair.repository.requests(second.id))[0]
    selected = next(index for index, item in enumerate(primary.candidates) if item.provider_id == "provider-b")
    candidate = primary.candidates[selected]
    await pair.repository.artifact_state(primary.id, "queued", selected=selected, expected_bytes=candidate.expected_bytes)
    current = (await pair.repository.artifacts(first.id))[0]

    await pair.engine._refresh(current)

    assert any(call[0] == "refresh" and call[1] == candidate.id for call in pair.b.calls)
    assert not any(call[0] == "refresh" for call in pair.a.calls)
    async with database.get_db() as db:
        latest = await db.fetchone(
            "SELECT request_id,provider_id FROM resolution_attempts ORDER BY rowid DESC LIMIT 1"
        )
    assert latest == {"request_id": second_request.id, "provider_id": "provider-b"}


@pytest.mark.asyncio
async def test_cross_transfer_alternate_failover_keeps_same_canonical_artifact(pair):
    first = await admit(pair, pair.a, "submission-a")
    await pair.engine.tick()
    second = await admit(pair, pair.b, "submission-b")
    await pair.engine.tick()
    primary = (await pair.repository.artifacts(first.id))[0]
    original_id = primary.id
    original_target = primary.target
    error = NormalizedError(
        Domain.NETWORK,
        Category.REMOTE_READ_FAILED,
        Stage.EXECUTION,
        Retryability.BACKOFF,
        Recovery.TRY_ALTERNATE_CANDIDATE,
    )
    pair.executor.jobs[primary.execution.attempt_id] = replace(
        pair.executor.jobs[primary.execution.attempt_id],
        state=ExecutionState.FAILED,
        error=error,
    )

    await pair.engine.tick()
    await pair.engine.tick()

    current = (await pair.repository.artifacts(first.id))[0]
    assert current.id == original_id
    assert current.target == original_target
    assert current.selected == 1
    assert current.candidates[current.selected].provider_id == "provider-b"
    attempts = await pair.repository.executions(first.id)
    assert len(attempts) == 2
    assert {attempt.candidate.provider_id for attempt in attempts} == {"provider-a", "provider-b"}
    assert await pair.repository.artifacts(second.id) == ()
