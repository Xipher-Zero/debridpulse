"""Workspace 4 focused cohort/canonicalization exit-gate regressions."""
import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage
from transfers.models import ArtifactFingerprint, FingerprintKind, ResolutionResult, ResourceState, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


class BatchProvider(ParcelProvider):
    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        name = request.name or "payload.bin"
        return ResolutionResult(
            ResourceState.AVAILABLE,
            (self.candidate(name, payload=f"payload:{request.payload}"),),
        )


@pytest_asyncio.fixture
async def cohort_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    first = BatchProvider("provider-a")
    second = BatchProvider("provider-b")
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(first)
    registry.register_provider(second)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(
            retry_delay=1,
            adoption_stability_seconds=0,
            max_active_executions=64,
            resolution_concurrency=64,
        ),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, registry=registry,
                           a=first, b=second, executor=executor)


async def submit_names(pair, provider, prefix, names):
    return await pair.engine.submit(
        tuple(
            TransferRequest(
                "parcel",
                f"{prefix}-{index}",
                name=name,
                preferred_provider=provider.descriptor.id,
            )
            for index, name in enumerate(names, 1)
        ),
        name=prefix,
        deduplicate=False,
    )


def _starts(pair):
    return [call for call in pair.executor.calls if call[0] == "start"]


def _prefix_fingerprint(candidate):
    signature = f"prefix:{candidate.name.casefold()}"
    return ArtifactFingerprint(
        candidate.expected_bytes,
        signature,
        FingerprintKind.PREFIX_CONTENT_SAMPLE,
        "range_ignored",
        signature,
    )


def _full_fingerprint(candidate):
    return ArtifactFingerprint(
        candidate.expected_bytes,
        f"full:{candidate.name.casefold()}",
        FingerprintKind.FULL_CONTENT_SAMPLE,
    )


@pytest.mark.asyncio
async def test_same_name_and_size_but_different_full_content_stays_independent(cohort_pair, monkeypatch):
    first = await submit_names(cohort_pair, cohort_pair.a, "a", ("same.bin",))
    await cohort_pair.engine.tick()

    async def different(candidate):
        return ArtifactFingerprint(
            candidate.expected_bytes,
            f"full:{candidate.provider_id}",
            FingerprintKind.FULL_CONTENT_SAMPLE,
        )

    monkeypatch.setattr(cohort_pair.executor, "fingerprint", different)
    second = await submit_names(cohort_pair, cohort_pair.b, "b", ("same.bin",))
    await cohort_pair.engine.tick()

    assert len(await cohort_pair.repository.artifacts(first.id)) == 1
    assert len(await cohort_pair.repository.artifacts(second.id)) == 1
    assert (await cohort_pair.repository.get(second.id)).state.value != "consolidated"
    assert len(_starts(cohort_pair)) == 2


@pytest.mark.asyncio
async def test_partial_overlap_full_proof_consolidates_only_proven_members(cohort_pair, monkeypatch):
    first = await submit_names(cohort_pair, cohort_pair.a, "a", ("part1.rar", "part2.rar"))
    await cohort_pair.engine.tick()

    async def full(candidate):
        return _full_fingerprint(candidate)

    monkeypatch.setattr(cohort_pair.executor, "fingerprint", full)
    second = await submit_names(
        cohort_pair,
        cohort_pair.b,
        "b",
        ("part1.rar", "part2.rar", "new.rar"),
    )
    await cohort_pair.engine.tick()

    canonicals = await cohort_pair.repository.artifacts(first.id)
    independent = await cohort_pair.repository.artifacts(second.id)
    assert len(canonicals) == 2
    assert all(len(item.candidates) == 2 for item in canonicals)
    assert len(independent) == 1
    assert independent[0].candidates[0].name == "new.rar"
    assert len(_starts(cohort_pair)) == 3

    async with database.get_db() as db:
        mappings = await db.fetchone(
            "SELECT COUNT(*) AS n FROM artifact_consolidations WHERE source_transfer_id=?",
            (second.id,),
        )
    assert int(mappings["n"]) == 2


@pytest.mark.asyncio
async def test_slow_sibling_does_not_block_unrelated_transfer(cohort_pair, monkeypatch):
    first = await submit_names(cohort_pair, cohort_pair.a, "a", ("part1.rar", "part2.rar"))
    await cohort_pair.engine.tick()

    async def prefix(candidate):
        return _prefix_fingerprint(candidate)

    monkeypatch.setattr(cohort_pair.executor, "fingerprint", prefix)
    second = await submit_names(cohort_pair, cohort_pair.b, "b", ("part1.rar", "part2.rar"))
    records = sorted(await cohort_pair.repository.requests(second.id), key=lambda item: item.request.name)

    # Establish one weakly-proven member at the durable barrier first.
    await cohort_pair.engine._resolve(records[0])
    parked = next(item for item in await cohort_pair.repository.requests(second.id) if item.id == records[0].id)
    assert parked.state == "materializing"

    original_resolve = cohort_pair.b.resolve
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(request):
        if request.name == "part2.rar":
            entered.set()
            await release.wait()
        return await original_resolve(request)

    monkeypatch.setattr(cohort_pair.b, "resolve", slow)
    sibling_task = asyncio.create_task(cohort_pair.engine._resolve(records[1]))
    await asyncio.wait_for(entered.wait(), timeout=1)

    unrelated = await submit_names(cohort_pair, cohort_pair.a, "unrelated", ("other.bin",))
    unrelated_record = (await cohort_pair.repository.requests(unrelated.id))[0]
    try:
        await asyncio.wait_for(cohort_pair.engine._resolve(unrelated_record), timeout=1)
        assert len(await cohort_pair.repository.artifacts(unrelated.id)) == 1
    finally:
        release.set()
        await sibling_task

    assert (await cohort_pair.repository.get(second.id)).state.value == "consolidated"
    assert len(_starts(cohort_pair)) == 2  # only the original canonical pair; unrelated is not dispatched by _resolve


@pytest.mark.asyncio
async def test_terminal_sibling_releases_weak_barrier_without_hanging(cohort_pair, monkeypatch):
    await submit_names(cohort_pair, cohort_pair.a, "a", ("part1.rar", "part2.rar"))
    await cohort_pair.engine.tick()

    async def prefix(candidate):
        return _prefix_fingerprint(candidate)

    monkeypatch.setattr(cohort_pair.executor, "fingerprint", prefix)
    second = await submit_names(cohort_pair, cohort_pair.b, "b", ("part1.rar", "part2.rar"))
    records = sorted(await cohort_pair.repository.requests(second.id), key=lambda item: item.request.name)
    await cohort_pair.engine._resolve(records[0])

    original_resolve = cohort_pair.b.resolve
    terminal = NormalizedError(
        Domain.PROVIDER,
        Category.PROVIDER_UNAVAILABLE,
        Stage.RESOLUTION,
        Retryability.NEVER,
        Recovery.FAIL,
    )

    async def fail_second(request):
        if request.name == "part2.rar":
            return ResolutionResult(ResourceState.UNAVAILABLE, error=terminal)
        return await original_resolve(request)

    monkeypatch.setattr(cohort_pair.b, "resolve", fail_second)
    await cohort_pair.engine._resolve(records[1])
    await cohort_pair.engine.tick()

    refreshed = {item.id: item for item in await cohort_pair.repository.requests(second.id)}
    assert refreshed[records[1].id].state == "failed"
    assert refreshed[records[0].id].state != "materializing"
    artifacts = await cohort_pair.repository.artifacts(second.id)
    assert len(artifacts) == 1
    assert artifacts[0].candidates[0].name == "part1.rar"


@pytest.mark.asyncio
async def test_concurrent_equivalent_batches_preserve_one_writer_and_canonical_winner(cohort_pair, monkeypatch):
    names = tuple(f"part{index}.rar" for index in range(1, 8))
    first = await submit_names(cohort_pair, cohort_pair.a, "a", names)
    await cohort_pair.engine.tick()
    assert len(_starts(cohort_pair)) == 7

    async def prefix(candidate):
        return _prefix_fingerprint(candidate)

    monkeypatch.setattr(cohort_pair.executor, "fingerprint", prefix)
    second = await submit_names(cohort_pair, cohort_pair.b, "b", names)
    third = await submit_names(cohort_pair, cohort_pair.b, "c", names)
    records = tuple(await cohort_pair.repository.requests(second.id)) + tuple(
        await cohort_pair.repository.requests(third.id)
    )
    await asyncio.gather(*(cohort_pair.engine._resolve(record) for record in records))
    await cohort_pair.engine.reconcile_executions()

    assert (await cohort_pair.repository.get(second.id)).state.value == "consolidated"
    assert (await cohort_pair.repository.get(third.id)).state.value == "consolidated"
    canonicals = await cohort_pair.repository.artifacts(first.id)
    assert len(canonicals) == 7
    assert all(len(item.candidates) == 3 for item in canonicals)
    assert len(_starts(cohort_pair)) == 7
    assert await cohort_pair.repository.artifacts(second.id) == ()
    assert await cohort_pair.repository.artifacts(third.id) == ()


@pytest.mark.asyncio
async def test_restart_during_pending_weak_cohort_reconstructs_without_duplicate_writer(cohort_pair, monkeypatch):
    first = await submit_names(cohort_pair, cohort_pair.a, "a", ("part1.rar", "part2.rar"))
    await cohort_pair.engine.tick()
    assert len(_starts(cohort_pair)) == 2

    async def prefix(candidate):
        return _prefix_fingerprint(candidate)

    monkeypatch.setattr(cohort_pair.executor, "fingerprint", prefix)
    second = await submit_names(cohort_pair, cohort_pair.b, "b", ("part1.rar", "part2.rar"))
    records = sorted(await cohort_pair.repository.requests(second.id), key=lambda item: item.request.name)
    await cohort_pair.engine._resolve(records[0])
    parked = next(item for item in await cohort_pair.repository.requests(second.id) if item.id == records[0].id)
    assert parked.state == "materializing"
    assert await cohort_pair.repository.artifacts(second.id) == ()

    restarted = TransferEngine(
        TransferRepository(),
        cohort_pair.registry,
        download_root=cohort_pair.engine.root,
        policy=cohort_pair.engine.policy,
        clock=lambda: 1000.0,
    )
    await restarted.initialize()
    await restarted.tick()
    await restarted.tick()

    assert (await cohort_pair.repository.get(second.id)).state.value == "consolidated"
    assert await cohort_pair.repository.artifacts(second.id) == ()
    canonicals = await cohort_pair.repository.artifacts(first.id)
    assert len(canonicals) == 2
    assert all(len(item.candidates) == 2 for item in canonicals)
    assert len(_starts(cohort_pair)) == 2
