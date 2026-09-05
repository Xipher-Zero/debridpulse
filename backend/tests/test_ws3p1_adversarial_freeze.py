"""Workspace 3 Phase 1 adversarial consolidation boundary qualification."""
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
    IntegrityMetadata,
    ResolutionResult,
    ResourceState,
    SourceIdentity,
    TransferRequest,
    TransferState,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


_SHARED_PAYLOAD_SHA256 = "a4c3ed04a95a3da14a9d235c83d868bed7c0f45cf7f3faa751ee8f50598d2211"


class AdversarialParcelProvider(ParcelProvider):
    """Provider-neutral deterministic resolver with authoritative integrity evidence."""

    def candidate(self, name="payload.bin", *, payload="parcel"):
        return replace(
            super().candidate(name, payload=payload),
            integrity=(IntegrityMetadata("sha256", _SHARED_PAYLOAD_SHA256),),
            source_identity=SourceIdentity("parcel-payload", f"{self.descriptor.id}:{payload}"),
        )

    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        name = request.name or "payload.bin"
        return ResolutionResult(
            ResourceState.AVAILABLE,
            (self.candidate(name, payload=f"shared:{name}"),),
        )


@pytest_asyncio.fixture
async def ws3(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    first = AdversarialParcelProvider("provider-a")
    second = AdversarialParcelProvider("provider-b")
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(first)
    registry.register_provider(second)
    registry.register_executor(executor)
    policy = TransferPolicy(
        retry_delay=0,
        adoption_stability_seconds=0,
        max_active_executions=32,
        resolution_concurrency=32,
    )
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "payloads"),
        policy=policy,
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return SimpleNamespace(
        engine=engine,
        repository=repository,
        registry=registry,
        a=first,
        b=second,
        executor=executor,
        policy=policy,
    )


async def admit(ws3, provider, payload, name="same.bin"):
    return await ws3.engine.submit(
        (TransferRequest("parcel", payload, name=name, preferred_provider=provider.descriptor.id),),
        name=name,
        deduplicate=False,
    )


async def admit_many(ws3, provider, names, *, label="collection"):
    return await ws3.engine.submit(
        tuple(
            TransferRequest(
                "parcel",
                f"submission:{label}:{name}",
                name=name,
                preferred_provider=provider.descriptor.id,
            )
            for name in names
        ),
        name=label,
        deduplicate=False,
    )


async def restart(ws3):
    repository = TransferRepository()
    engine = TransferEngine(
        repository,
        ws3.registry,
        download_root=ws3.engine.root,
        policy=ws3.policy,
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return engine, repository


@pytest.mark.asyncio
async def test_reverse_order_established_owner_remains_canonical(ws3):
    canonical = await admit(ws3, ws3.b, "first-b")
    await ws3.engine.resolve_pending()
    primary = (await ws3.repository.artifacts(canonical.id))[0]

    source = await admit(ws3, ws3.a, "later-a")
    await ws3.engine.resolve_pending()

    current = (await ws3.repository.artifacts(canonical.id))[0]
    relation = await ws3.engine.canonical.consolidation(source.id)
    bindings = await ws3.engine.canonical.bindings(primary.id)

    assert current.id == primary.id
    assert current.target == primary.target
    assert (await ws3.repository.get(source.id)).state == TransferState.CONSOLIDATED
    assert relation["consolidated_into"] == canonical.id
    assert [item["provider_id"] for item in bindings] == ["provider-b", "provider-a"]


@pytest.mark.asyncio
async def test_partial_seven_five_overlap_keeps_two_unique_and_survives_restart(ws3):
    canonical_names = tuple(f"file-{index}.bin" for index in range(1, 6))
    incoming_names = tuple(f"file-{index}.bin" for index in range(1, 8))
    canonical = await admit_many(ws3, ws3.a, canonical_names, label="canonical-five")
    await ws3.engine.resolve_pending()
    source = await admit_many(ws3, ws3.b, incoming_names, label="incoming-seven")
    await ws3.engine.resolve_pending()

    relation = await ws3.engine.canonical.consolidation(source.id)
    remaining = await ws3.repository.artifacts(source.id)

    assert relation["state"] == "partial"
    assert relation["canonical_transfer_ids"] == [canonical.id]
    assert len(relation["artifact_mappings"]) == 5
    assert {item.name for item in remaining} == {"file-6.bin", "file-7.bin"}
    assert (await ws3.repository.get(source.id)).state != TransferState.CONSOLIDATED
    assert source.id in {item.id for item in await ws3.repository.active()}

    restarted, repository = await restart(ws3)
    persisted = await restarted.canonical.consolidation(source.id)
    assert persisted == relation
    assert {item.name for item in await repository.artifacts(source.id)} == {"file-6.bin", "file-7.bin"}
    assert source.id in {item.id for item in await repository.active()}


@pytest.mark.asyncio
async def test_complete_seven_across_multiple_canonical_targets_has_no_singular_owner_and_survives_restart(ws3):
    first_names = tuple(f"file-{index}.bin" for index in range(1, 4))
    second_names = tuple(f"file-{index}.bin" for index in range(4, 8))
    first = await admit_many(ws3, ws3.a, first_names, label="canonical-one")
    await ws3.engine.resolve_pending()
    second = await admit_many(ws3, ws3.a, second_names, label="canonical-two")
    await ws3.engine.resolve_pending()

    source = await admit_many(
        ws3,
        ws3.b,
        tuple(f"file-{index}.bin" for index in range(1, 8)),
        label="incoming-seven",
    )
    await ws3.engine.resolve_pending()

    relation = await ws3.engine.canonical.consolidation(source.id)
    assert relation["state"] == "complete"
    assert relation["consolidated_into"] is None
    assert set(relation["canonical_transfer_ids"]) == {first.id, second.id}
    assert len(relation["artifact_mappings"]) == 7
    assert (await ws3.repository.get(source.id)).state == TransferState.CONSOLIDATED
    assert await ws3.repository.artifacts(source.id) == []
    assert source.id not in {item.id for item in await ws3.repository.active()}

    restarted, repository = await restart(ws3)
    persisted = await restarted.canonical.consolidation(source.id)
    assert persisted == relation
    assert (await repository.get(source.id)).state == TransferState.CONSOLIDATED
    assert source.id not in {item.id for item in await repository.active()}


@pytest.mark.asyncio
async def test_disabled_attached_alternate_is_not_selected_for_recovery(ws3):
    canonical = await admit(ws3, ws3.a, "canonical-a")
    await ws3.engine.resolve_pending()
    source = await admit(ws3, ws3.b, "alternate-b")
    await ws3.engine.resolve_pending()
    primary = (await ws3.repository.artifacts(canonical.id))[0]
    bindings_before = await ws3.engine.canonical.bindings(primary.id)
    assert [item["provider_id"] for item in bindings_before] == ["provider-a", "provider-b"]
    assert (await ws3.repository.get(source.id)).state == TransferState.CONSOLIDATED

    ws3.b.descriptor = replace(ws3.b.descriptor, enabled=False)
    await ws3.engine.reconcile_executions()
    running = (await ws3.repository.artifacts(canonical.id))[0]
    assert running.execution is not None
    assert running.candidates[running.selected].provider_id == "provider-a"

    failure = NormalizedError(
        Domain.NETWORK,
        Category.REMOTE_READ_FAILED,
        Stage.EXECUTION,
        Retryability.BACKOFF,
        Recovery.TRY_ALTERNATE_CANDIDATE,
    )
    ws3.executor.jobs[running.execution.attempt_id] = replace(
        ws3.executor.jobs[running.execution.attempt_id],
        state=ExecutionState.FAILED,
        error=failure,
    )
    await ws3.engine.reconcile_executions()

    recovered = (await ws3.repository.artifacts(canonical.id))[0]
    bindings_after = await ws3.engine.canonical.bindings(primary.id)
    assert recovered.selected == 0
    assert recovered.candidates[recovered.selected].provider_id == "provider-a"
    assert bindings_after == bindings_before
    assert [item["provider_id"] for item in bindings_after] == ["provider-a", "provider-b"]
