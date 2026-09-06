"""Workspace 4 Phase 1 real-world equivalence and cohort regression tests."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.mirrors import comparable
from transfers.models import (
    ArtifactFingerprint, FingerprintKind, ResolutionResult, ResourceState,
    SourceIdentity, TransferRequest,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


class BatchProvider(ParcelProvider):
    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        name = request.name or "payload.bin"
        return ResolutionResult(
            ResourceState.AVAILABLE,
            (self.candidate(name, payload=f"payload:{name}"),),
        )


@pytest_asyncio.fixture
async def batch_pair(tmp_path, monkeypatch):
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
            max_active_executions=32,
            resolution_concurrency=32,
        ),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, a=first, b=second, executor=executor)


async def submit_batch(pair, provider, prefix):
    requests = tuple(
        TransferRequest("parcel", f"{prefix}-{index}", name=f"part{index}.rar",
                        preferred_provider=provider.descriptor.id)
        for index in range(1, 8)
    )
    return await pair.engine.submit(requests, name=prefix, deduplicate=False)


def test_known_sizes_use_bounded_reported_size_compatibility():
    provider = ParcelProvider()
    base = provider.candidate("same.bin")
    left = replace(base, expected_bytes=1000, source_identity=SourceIdentity("host", "one"))
    same = replace(provider.candidate("same.bin"), expected_bytes=1000,
                   source_identity=SourceIdentity("host", "two"))
    near = replace(provider.candidate("same.bin"), expected_bytes=1001,
                   source_identity=SourceIdentity("host", "three"))
    outside = replace(provider.candidate("same.bin"), expected_bytes=1002,
                      source_identity=SourceIdentity("host", "outside"))
    unknown = replace(provider.candidate("same.bin"), expected_bytes=0,
                      source_identity=SourceIdentity("host", "four"))
    assert comparable(left, same)
    assert comparable(left, near)
    assert not comparable(left, outside)
    assert not comparable(left, unknown)


@pytest.mark.asyncio
async def test_single_large_prefix_only_match_remains_independent(batch_pair, monkeypatch):
    first = await batch_pair.engine.submit(
        (TransferRequest("parcel", "a", name="same.bin", preferred_provider=batch_pair.a.descriptor.id),),
        name="a", deduplicate=False,
    )
    await batch_pair.engine.tick()

    async def prefix(candidate):
        signature = f"prefix:{candidate.name.casefold()}"
        return ArtifactFingerprint(candidate.expected_bytes, signature,
                                   FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                   "range_ignored", signature)

    monkeypatch.setattr(batch_pair.executor, "fingerprint", prefix)
    second = await batch_pair.engine.submit(
        (TransferRequest("parcel", "b", name="same.bin", preferred_provider=batch_pair.b.descriptor.id),),
        name="b", deduplicate=False,
    )
    await batch_pair.engine.tick()

    assert len(await batch_pair.repository.artifacts(first.id)) == 1
    assert len(await batch_pair.repository.artifacts(second.id)) == 1
    assert (await batch_pair.repository.get(second.id)).state.value != "consolidated"


@pytest.mark.asyncio
async def test_real_world_seven_plus_seven_prefix_cohort_consolidates_without_duplicate_writers(batch_pair, monkeypatch):
    first = await submit_batch(batch_pair, batch_pair.a, "submission-a")
    await batch_pair.engine.tick()
    assert len(await batch_pair.repository.artifacts(first.id)) == 7
    assert len([call for call in batch_pair.executor.calls if call[0] == "start"]) == 7

    async def prefix(candidate):
        signature = f"prefix:{candidate.name.casefold()}"
        return ArtifactFingerprint(candidate.expected_bytes, signature,
                                   FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                   "range_ignored", signature)

    monkeypatch.setattr(batch_pair.executor, "fingerprint", prefix)
    second = await submit_batch(batch_pair, batch_pair.b, "submission-b")
    await batch_pair.engine.resolve_pending()
    await batch_pair.engine.reconcile_executions()

    assert (await batch_pair.repository.get(second.id)).state.value == "consolidated"
    assert len(await batch_pair.repository.artifacts(second.id)) == 0
    canonicals = await batch_pair.repository.artifacts(first.id)
    assert len(canonicals) == 7
    assert all(len(item.candidates) == 2 for item in canonicals)
    assert len([call for call in batch_pair.executor.calls if call[0] == "start"]) == 7

    async with database.get_db() as db:
        origins = await db.fetchone(
            "SELECT COUNT(*) AS n FROM transfer_requests WHERE transfer_id IN (?,?) AND NOT EXISTS(SELECT 1 FROM transfer_requests c WHERE c.parent_id=transfer_requests.id)",
            (first.id, second.id),
        )
        mappings = await db.fetchone(
            "SELECT COUNT(*) AS n FROM artifact_consolidations WHERE source_transfer_id=?",
            (second.id,),
        )
        duplicates = await db.fetchall(
            "SELECT filename FROM download_files WHERE torrent_id=? AND filename LIKE '% (2)%'",
            (second.id,),
        )
    assert int(origins["n"]) == 14
    assert int(mappings["n"]) == 7
    assert duplicates == []


@pytest.mark.asyncio
async def test_seven_plus_seven_one_prefix_mismatch_releases_whole_weak_cohort(batch_pair, monkeypatch):
    first = await submit_batch(batch_pair, batch_pair.a, "submission-a")
    await batch_pair.engine.tick()

    async def prefix(candidate):
        signature = f"prefix:{candidate.name.casefold()}"
        if candidate.provider_id == batch_pair.b.descriptor.id and candidate.name == "part7.rar":
            signature += ":different"
        return ArtifactFingerprint(candidate.expected_bytes, signature,
                                   FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                   "range_ignored", signature)

    monkeypatch.setattr(batch_pair.executor, "fingerprint", prefix)
    second = await submit_batch(batch_pair, batch_pair.b, "submission-b")
    for _ in range(3):
        await batch_pair.engine.tick()

    assert len(await batch_pair.repository.artifacts(first.id)) == 7
    assert len(await batch_pair.repository.artifacts(second.id)) == 7
    assert (await batch_pair.repository.get(second.id)).state.value != "consolidated"
    async with database.get_db() as db:
        mappings = await db.fetchone(
            "SELECT COUNT(*) AS n FROM artifact_consolidations WHERE source_transfer_id=?",
            (second.id,),
        )
    assert int(mappings["n"]) == 0


@pytest.mark.asyncio
async def test_ambiguous_duplicate_filename_never_uses_collection_guess(batch_pair, monkeypatch):
    first_candidates = (
        replace(batch_pair.a.candidate("same.rar", payload="left"), source_identity=SourceIdentity("host", "a-left")),
        replace(batch_pair.a.candidate("same.rar", payload="right"), source_identity=SourceIdentity("host", "a-right")),
    )
    batch_pair.a.responses = [ResolutionResult(ResourceState.AVAILABLE, (first_candidates[0],)),
                              ResolutionResult(ResourceState.AVAILABLE, (first_candidates[1],))]
    # Use the inherited responder for this deliberately ambiguous setup.
    batch_pair.a.resolve = ParcelProvider.resolve.__get__(batch_pair.a, BatchProvider)
    first = await batch_pair.engine.submit(
        (TransferRequest("parcel", "a1", name="same.rar", preferred_provider=batch_pair.a.descriptor.id),
         TransferRequest("parcel", "a2", name="same.rar", preferred_provider=batch_pair.a.descriptor.id)),
        name="ambiguous-a", deduplicate=False,
    )
    await batch_pair.engine.tick()
    assert len(await batch_pair.repository.artifacts(first.id)) == 2

    async def prefix(candidate):
        signature = "same-prefix"
        return ArtifactFingerprint(candidate.expected_bytes, signature,
                                   FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                   "range_ignored", signature)

    monkeypatch.setattr(batch_pair.executor, "fingerprint", prefix)
    second = await batch_pair.engine.submit(
        (TransferRequest("parcel", "b1", name="same.rar", preferred_provider=batch_pair.b.descriptor.id),
         TransferRequest("parcel", "b2", name="same.rar", preferred_provider=batch_pair.b.descriptor.id)),
        name="ambiguous-b", deduplicate=False,
    )
    for _ in range(2):
        await batch_pair.engine.tick()
    assert len(await batch_pair.repository.artifacts(second.id)) == 2
    assert (await batch_pair.repository.get(second.id)).state.value != "consolidated"
