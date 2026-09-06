"""Lifecycle regression for bounded reported-size artifact consolidation."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.mirrors import reported_sizes_compatible
from transfers.models import ArtifactFingerprint, ResolutionResult, ResourceState, SourceIdentity, TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


REPORT_A = 3_597_035_110
REPORT_B = 3_595_501_360
REPORT_C = 3_596_250_000
ACTUAL = 3_595_501_360


class NearSizeProvider(ParcelProvider):
    def __init__(self, identity, reported_size):
        super().__init__(identity)
        self.reported_size = reported_size

    def candidate(self, name="payload.bin", *, payload="parcel"):
        return replace(
            super().candidate(name, payload="shared-content"),
            expected_bytes=self.reported_size,
            source_identity=SourceIdentity("test-provider", self.descriptor.id),
        )

    async def resolve(self, request):
        self.calls.append(("resolve", request.payload))
        return ResolutionResult(
            ResourceState.AVAILABLE,
            (self.candidate(request.name or "payload.bin", payload=str(request.payload)),),
        )


@pytest_asyncio.fixture
async def near_size_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    providers = (
        NearSizeProvider("provider-a", REPORT_A),
        NearSizeProvider("provider-b", REPORT_B),
        NearSizeProvider("provider-c", REPORT_C),
    )
    executor = MemoryExecutor(repository.authorize_execution)

    async def fingerprint(candidate):
        return ArtifactFingerprint(ACTUAL, "bounded-shared-content")

    monkeypatch.setattr(executor, "fingerprint", fingerprint)
    for provider in providers:
        registry.register_provider(provider)
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
    return SimpleNamespace(
        engine=engine,
        repository=repository,
        registry=registry,
        providers=providers,
        executor=executor,
        root=tmp_path / "payloads",
    )


async def submit_one(ctx, provider, payload):
    return await ctx.engine.submit(
        (TransferRequest(
            "parcel",
            payload,
            name="GF200826-TMNTSFS-RN.rar",
            preferred_provider=provider.descriptor.id,
        ),),
        name="GF200826-TMNTSFS-RN.rar",
        deduplicate=False,
    )


@pytest.mark.asyncio
async def test_three_near_size_sibling_requests_consolidate_to_one_artifact_and_writer(near_size_engine):
    ctx = near_size_engine
    requests = tuple(
        TransferRequest(
            "parcel",
            f"submission-{index}",
            name="GF200826-TMNTSFS-RN.rar",
            preferred_provider=provider.descriptor.id,
        )
        for index, provider in enumerate(ctx.providers, start=1)
    )
    transfer = await ctx.engine.submit(requests, name="near-size cohort", deduplicate=False)

    # Resolve until all sibling requests have left the resolution/materialization
    # barrier, then allow normal execution dispatch.
    for _ in range(4):
        await ctx.engine.resolve_pending()
    await ctx.engine.reconcile_executions()

    artifacts = await ctx.repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert len(artifact.candidates) == 3
    assert {candidate.provider_id for candidate in artifact.candidates} == {
        "provider-a", "provider-b", "provider-c",
    }
    assert reported_sizes_compatible(REPORT_A, REPORT_B)
    assert reported_sizes_compatible(REPORT_A, REPORT_C)
    assert len([call for call in ctx.executor.calls if call[0] == "start"]) == 1
    assert "(2)" not in artifact.target

    async with database.get_db() as db:
        physical = await db.fetchall(
            "SELECT id,local_path FROM download_files WHERE torrent_id=?",
            (transfer.id,),
        )
    assert len(physical) == 1


@pytest.mark.asyncio
async def test_later_near_size_transfer_attaches_to_existing_canonical_writer(near_size_engine):
    ctx = near_size_engine
    first = await submit_one(ctx, ctx.providers[0], "submission-a")
    await ctx.engine.tick()
    primary = (await ctx.repository.artifacts(first.id))[0]
    original_target = primary.target
    original_execution = primary.execution

    second = await submit_one(ctx, ctx.providers[1], "submission-b")
    await ctx.engine.tick()

    canonical = (await ctx.repository.artifacts(first.id))[0]
    assert canonical.id == primary.id
    assert canonical.target == original_target
    assert canonical.execution == original_execution
    assert [candidate.provider_id for candidate in canonical.candidates] == ["provider-a", "provider-b"]
    assert await ctx.repository.artifacts(second.id) == ()
    assert len([call for call in ctx.executor.calls if call[0] == "start"]) == 1
