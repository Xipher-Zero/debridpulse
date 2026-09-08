"""Pause lifecycle convergence and persisted presentation-truth regressions."""
from types import SimpleNamespace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.engine import TransferEngine
from transfers.models import ExecutionState, TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.presentation_repository import TransferRepository
from transfers.registry import IntegrationRegistry


@pytest_asyncio.fixture
async def pause_context(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "pause-truth.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=2),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return SimpleNamespace(
        engine=engine, repository=repository, provider=provider, executor=executor,
    )


async def active_transfer(ctx):
    transfer = await ctx.engine.submit((TransferRequest("parcel", "box", name="payload.bin"),))
    await ctx.engine.tick()
    assert (await ctx.repository.get(transfer.id)).state == TransferState.TRANSFERRING
    return transfer


@pytest.mark.asyncio
async def test_pause_converges_parent_execution_and_canonical_presentation(pause_context):
    ctx = pause_context
    transfer = await active_transfer(ctx)
    before = (await ctx.repository.artifacts(transfer.id))[0]
    target = before.target
    candidates = before.candidates
    budget = await ctx.repository.recovery_budget(before.id)

    assert await ctx.engine.pause(transfer.id) == ()

    current = await ctx.repository.get(transfer.id)
    artifact = (await ctx.repository.artifacts(transfer.id))[0]
    execution = (await ctx.repository.executions(transfer.id))[0]
    presentation = await ctx.repository.presentation(transfer.id)
    assert current.paused is True
    assert current.state == TransferState.PAUSED
    assert execution.state == ExecutionState.PAUSED.value
    assert presentation["presentation_status"] == "paused"
    assert artifact.target == target
    assert artifact.candidates == candidates
    assert await ctx.repository.recovery_budget(artifact.id) == budget


@pytest.mark.asyncio
async def test_paused_aggregate_self_heals_stale_parent_without_dispatch_or_retry_authority(pause_context):
    ctx = pause_context
    transfer = await active_transfer(ctx)
    await ctx.engine.pause(transfer.id)
    artifact = (await ctx.repository.artifacts(transfer.id))[0]
    budget = await ctx.repository.recovery_budget(artifact.id)
    provider_calls = tuple(ctx.provider.calls)
    executor_calls = tuple(ctx.executor.calls)

    # Reproduce the persisted crash window: pause intent + native execution are
    # already paused, but the legacy parent row is stale.
    assert await ctx.repository.state(transfer.id, TransferState.TRANSFERRING)
    stale = await ctx.repository.get(transfer.id)
    assert stale.paused is True
    assert stale.state == TransferState.TRANSFERRING

    await ctx.engine._aggregate(transfer.id)

    repaired = await ctx.repository.get(transfer.id)
    execution = (await ctx.repository.executions(transfer.id))[0]
    presentation = await ctx.repository.presentation(transfer.id)
    assert repaired.paused is True
    assert repaired.state == TransferState.PAUSED
    assert execution.state == ExecutionState.PAUSED.value
    assert presentation["presentation_status"] == "paused"
    assert tuple(ctx.provider.calls) == provider_calls
    assert tuple(ctx.executor.calls) == executor_calls
    assert await ctx.repository.recovery_budget(artifact.id) == budget


@pytest.mark.asyncio
async def test_pause_self_heal_never_overwrites_terminal_parent(pause_context):
    ctx = pause_context
    transfer = await active_transfer(ctx)
    await ctx.engine.pause(transfer.id)
    assert await ctx.repository.state(transfer.id, TransferState.CANCELLED)
    await ctx.engine._aggregate(transfer.id)
    assert (await ctx.repository.get(transfer.id)).state == TransferState.CANCELLED
