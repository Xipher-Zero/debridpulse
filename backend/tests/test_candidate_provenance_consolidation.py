"""Workspace 1 Phase 2 durable candidate provenance and consolidation qualification."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage
from transfers.models import (
    ExecutionState,
    ResolutionResult,
    ResourceState,
    SourceIdentity,
    TransferRequest,
    TransferState,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


class ProvenanceParcelProvider(ParcelProvider):
    """Resolver with explicit secret-free source identity independent of provider identity."""

    def candidate(self, name="payload.bin", *, payload="parcel"):
        return replace(
            super().candidate(name, payload=payload),
            source_identity=SourceIdentity("parcel-payload", str(payload)),
        )

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
async def p2(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    first = ProvenanceParcelProvider("provider-a")
    second = ProvenanceParcelProvider("provider-b")
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


async def admit(p2, provider, payload, name="same.bin"):
    return await p2.engine.submit(
        (TransferRequest("parcel", payload, name=name, preferred_provider=provider.descriptor.id),),
        name=name,
        deduplicate=False,
    )


async def admit_many(p2, provider, names):
    return await p2.engine.submit(
        tuple(
            TransferRequest("parcel", f"submission:{name}", name=name, preferred_provider=provider.descriptor.id)
            for name in names
        ),
        name="collection",
        deduplicate=False,
    )


async def restart(p2):
    repository = TransferRepository()
    engine = TransferEngine(
        repository,
        p2.registry,
        download_root=p2.engine.root,
        policy=p2.engine.policy,
        clock=lambda: p2.now[0],
    )
    await engine.initialize()
    return engine, repository


@pytest.mark.asyncio
async def test_p1_origin_handoff_migrates_without_filename_or_path_inference(p2):
    canonical_transfer = await admit(p2, p2.a, "submission-a")
    await p2.engine.resolve_pending()
    source_transfer = await admit(p2, p2.b, "submission-b")
    await p2.engine.resolve_pending()
    primary = (await p2.repository.artifacts(canonical_transfer.id))[0]
    source_request = (await p2.repository.requests(source_transfer.id))[0]

    # Recreate the exact durable P1 handoff shape: canonical + foreign standby
    # + route provenance, but no P2 binding/consolidation rows. Deliberately
    # change display/path fields that must never participate in reconstruction.
    async with database.get_db() as db:
        await db.execute("DELETE FROM canonical_candidate_origins")
        await db.execute("DELETE FROM artifact_consolidations")
        await db.execute("DELETE FROM canonical_candidate_bindings")
        await db.execute("UPDATE torrents SET status='processing',progress=0 WHERE id=?", (source_transfer.id,))
        await db.execute("UPDATE download_files SET filename='unrelated-display-name.bin',local_path='/unrelated/path' WHERE request_id=?", (source_request.id,))
        await db.commit()

    restarted, repository = await restart(p2)
    migrated = await repository.get(source_transfer.id)
    origins = await restarted.canonical.origins(primary.id)
    relation = await restarted.canonical.consolidation(source_transfer.id)

    assert migrated.state == TransferState.CONSOLIDATED
    assert relation["state"] == "complete"
    assert relation["consolidated_into"] == canonical_transfer.id
    assert {origin.request.id for origin in origins} == {
        (await repository.requests(canonical_transfer.id))[0].id,
        source_request.id,
    }
    assert {origin.provider_id for origin in origins} == {"provider-a", "provider-b"}
    assert all(origin.resolution_attempt_id for origin in origins)


@pytest.mark.asyncio
async def test_complete_one_target_consolidation_survives_restart_and_never_schedules(p2):
    canonical_transfer = await admit(p2, p2.a, "submission-a")
    await p2.engine.resolve_pending()
    source_transfer = await admit(p2, p2.b, "submission-b")
    await p2.engine.resolve_pending()

    assert (await p2.repository.get(source_transfer.id)).state == TransferState.CONSOLIDATED
    relation = await p2.engine.canonical.consolidation(source_transfer.id)
    assert relation["consolidated_into"] == canonical_transfer.id
    assert relation["canonical_transfer_ids"] == [canonical_transfer.id]
    assert len(relation["artifact_mappings"]) == 1
    assert source_transfer.id not in {item.id for item in await p2.repository.active()}
    assert await p2.engine.retry(source_transfer.id) is False

    _engine, repository = await restart(p2)
    assert (await repository.get(source_transfer.id)).state == TransferState.CONSOLIDATED
    assert source_transfer.id not in {item.id for item in await repository.active()}


@pytest.mark.asyncio
async def test_complete_multi_target_consolidation_has_no_false_singular_owner(p2):
    first = await admit(p2, p2.a, "canonical-one", name="one.bin")
    await p2.engine.resolve_pending()
    second = await admit(p2, p2.a, "canonical-two", name="two.bin")
    await p2.engine.resolve_pending()

    source = await admit_many(p2, p2.b, ("one.bin", "two.bin"))
    await p2.engine.resolve_pending()

    relation = await p2.engine.canonical.consolidation(source.id)
    assert (await p2.repository.get(source.id)).state == TransferState.CONSOLIDATED
    assert relation["state"] == "complete"
    assert relation["consolidated_into"] is None
    assert set(relation["canonical_transfer_ids"]) == {first.id, second.id}
    assert len(relation["artifact_mappings"]) == 2

    restarted, repository = await restart(p2)
    persisted = await restarted.canonical.consolidation(source.id)
    assert (await repository.get(source.id)).state == TransferState.CONSOLIDATED
    assert persisted == relation


@pytest.mark.asyncio
async def test_partial_consolidation_restart_keeps_only_unmatched_work_active(p2):
    canonical = await admit(p2, p2.a, "canonical-one", name="one.bin")
    await p2.engine.resolve_pending()
    source = await admit_many(p2, p2.b, ("one.bin", "unique.bin"))
    await p2.engine.resolve_pending()

    relation = await p2.engine.canonical.consolidation(source.id)
    artifacts = await p2.repository.artifacts(source.id)
    assert (await p2.repository.get(source.id)).state != TransferState.CONSOLIDATED
    assert relation["state"] == "partial"
    assert relation["canonical_transfer_ids"] == [canonical.id]
    assert len(relation["artifact_mappings"]) == 1
    assert [item.name for item in artifacts] == ["unique.bin"]

    restarted, repository = await restart(p2)
    persisted = await restarted.canonical.consolidation(source.id)
    assert persisted["state"] == "partial"
    assert [item.name for item in await repository.artifacts(source.id)] == ["unique.bin"]
    assert source.id in {item.id for item in await repository.active()}


@pytest.mark.asyncio
async def test_binding_preserves_request_resolution_provider_source_and_order(p2):
    canonical = await admit(p2, p2.a, "submission-a")
    await p2.engine.resolve_pending()
    source = await admit(p2, p2.b, "submission-b")
    await p2.engine.resolve_pending()
    primary = (await p2.repository.artifacts(canonical.id))[0]
    canonical_request = (await p2.repository.requests(canonical.id))[0]
    source_request = (await p2.repository.requests(source.id))[0]

    bindings = await p2.engine.canonical.bindings(primary.id)
    assert [item["candidate_order"] for item in bindings] == [1, 2]
    assert [item["role"] for item in bindings] == ["canonical", "alternate"]
    assert [item["provider_id"] for item in bindings] == ["provider-a", "provider-b"]
    assert {item["source_identity"]["key"] for item in bindings} == {"shared:same.bin"}
    origins = [origin for binding in bindings for origin in binding["origins"]]
    assert {item["request_id"] for item in origins} == {canonical_request.id, source_request.id}
    assert all(item["resolution_attempt_id"] for item in origins)


@pytest.mark.asyncio
async def test_duplicate_underlying_source_adds_origin_without_duplicate_candidate(p2):
    canonical = await admit(p2, p2.a, "submission-a")
    await p2.engine.resolve_pending()
    source_b = await admit(p2, p2.b, "submission-b")
    await p2.engine.resolve_pending()
    source_c = await admit(p2, p2.b, "submission-c")
    await p2.engine.resolve_pending()

    primary = (await p2.repository.artifacts(canonical.id))[0]
    bindings = await p2.engine.canonical.bindings(primary.id)
    provider_b = next(item for item in bindings if item["provider_id"] == "provider-b")

    assert len(primary.candidates) == 2
    assert len(bindings) == 2
    assert {item["contributing_transfer_id"] for item in provider_b["origins"]} == {source_b.id, source_c.id}
    assert (await p2.repository.get(source_b.id)).state == TransferState.CONSOLIDATED
    assert (await p2.repository.get(source_c.id)).state == TransferState.CONSOLIDATED


@pytest.mark.asyncio
async def test_foreign_alternate_execution_provenance_uses_foreign_resolution_attempt(p2):
    canonical = await admit(p2, p2.a, "submission-a")
    await p2.engine.tick()
    source = await admit(p2, p2.b, "submission-b")
    await p2.engine.tick()
    primary = (await p2.repository.artifacts(canonical.id))[0]
    first_handle = primary.execution

    failure = NormalizedError(
        Domain.NETWORK,
        Category.REMOTE_READ_FAILED,
        Stage.EXECUTION,
        Retryability.BACKOFF,
        Recovery.TRY_ALTERNATE_CANDIDATE,
    )
    p2.executor.jobs[first_handle.attempt_id] = replace(
        p2.executor.jobs[first_handle.attempt_id],
        state=ExecutionState.FAILED,
        error=failure,
    )
    await p2.engine.tick()
    await p2.engine.tick()

    current = (await p2.repository.artifacts(canonical.id))[0]
    assert current.candidates[current.selected].provider_id == "provider-b"
    p2.executor.finish(current.execution)
    await p2.engine.tick()

    bindings = await p2.engine.canonical.bindings(primary.id)
    foreign = next(item for item in bindings if item["provider_id"] == "provider-b")
    foreign_attempt = foreign["origins"][0]["resolution_attempt_id"]
    async with database.get_db() as db:
        delivered = await db.fetchone(
            """SELECT route_attempt_id,provider_id,candidate_id,delivered
                FROM execution_attempt_provenance
                WHERE artifact_id=? AND provider_id='provider-b' ORDER BY ordinal DESC LIMIT 1""",
            (primary.id,),
        )
    assert delivered["delivered"] == 1
    assert delivered["route_attempt_id"] == foreign_attempt
    assert delivered["candidate_id"] == foreign["candidate_id"]
    assert (await p2.repository.get(source.id)).state == TransferState.CONSOLIDATED


@pytest.mark.asyncio
async def test_established_winner_and_bindings_survive_restart(p2):
    canonical = await admit(p2, p2.a, "submission-a")
    await p2.engine.resolve_pending()
    primary = (await p2.repository.artifacts(canonical.id))[0]
    source = await admit(p2, p2.b, "submission-b")
    await p2.engine.resolve_pending()

    restarted, repository = await restart(p2)
    primary_after = (await repository.artifacts(canonical.id))[0]
    relation = await restarted.canonical.consolidation(source.id)
    bindings = await restarted.canonical.bindings(primary.id)

    assert primary_after.id == primary.id
    assert primary_after.target == primary.target
    assert relation["consolidated_into"] == canonical.id
    assert [item["candidate_order"] for item in bindings] == [1, 2]
