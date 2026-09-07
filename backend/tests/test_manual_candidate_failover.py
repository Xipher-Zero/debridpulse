"""Focused qualification for operator-requested canonical candidate switching."""
from __future__ import annotations

from dataclasses import replace

import pytest

import db.database as database
from fake_integrations import MemoryExecutor
from test_ws2p1_failover_progress import EquivalentParcelProvider
from transfers.engine import TransferEngine
from transfers.errors import Category, TransferError
from transfers.manual_failover import manual_candidate_failover
from transfers.manual_repository import TransferRepository
from transfers.models import (
    ExecutionObservation,
    ExecutionState,
    SourceIdentity,
    TransferProgress,
    TransferRequest,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry


class HostParcelProvider(EquivalentParcelProvider):
    def candidate(self, name="same.bin", *, payload="parcel"):
        return replace(
            super().candidate(name, payload=payload),
            source_identity=SourceIdentity("host", f"{self.descriptor.id}.example"),
        )


async def build_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    first = HostParcelProvider("provider-a")
    second = HostParcelProvider("provider-b")
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(first)
    registry.register_provider(second)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(
            retry_delay=0,
            adoption_stability_seconds=0,
            max_active_executions=8,
            resolution_concurrency=8,
        ),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return engine, repository, first, second, executor


async def attach_two(engine, repository, first, second):
    canonical = await engine.submit((TransferRequest(
        "parcel", "original-a", name="same.bin", preferred_provider=first.descriptor.id,
    ),), deduplicate=False)
    await engine.resolve_pending()
    source = await engine.submit((TransferRequest(
        "parcel", "original-b", name="same.bin", preferred_provider=second.descriptor.id,
    ),), deduplicate=False)
    await engine.resolve_pending()
    artifact = (await repository.artifacts(canonical.id))[0]
    assert [item.provider_id for item in artifact.candidates] == ["provider-a", "provider-b"]
    return canonical, source, artifact


@pytest.mark.asyncio
async def test_switch_retires_old_writer_and_redispatches_exact_candidate(tmp_path, monkeypatch):
    engine, repository, first, second, executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    await engine.reconcile_executions()
    artifact = (await repository.artifacts(canonical.id))[0]
    old = artifact.execution
    wanted = artifact.candidates[1]

    result = await manual_candidate_failover(engine, canonical.id, artifact.id, str(wanted.id))
    switched = (await repository.artifacts(canonical.id))[0]
    assert result["candidate_id"] == str(wanted.id)
    assert result["source_host"] == "provider-b.example"
    assert switched.selected == 1 and switched.execution is None and switched.state == "queued"
    attempts = {item.handle.attempt_id: item for item in await repository.executions(canonical.id)}
    assert attempts[old.attempt_id].state == ExecutionState.CANCELLED
    assert not await repository.authorize_execution(old, "start")

    await engine.reconcile_executions()
    active = (await repository.artifacts(canonical.id))[0]
    assert active.execution is not None and active.execution != old
    live = [item for item in await repository.executions(canonical.id)
            if item.state in {"prepared", "queued", "transferring", "paused", "unknown"}]
    assert len(live) == 1 and live[0].candidate.provider_id == "provider-b"
    assert len([call for call in executor.calls if call[0] == "start"]) == 2

    view = await repository.presentation(canonical.id, details=True)
    assert view["current_provider_id"] == "provider-b"
    event = view["manual_candidate_failovers"][-1]
    assert event["reason"] == "USER_REQUESTED" and event["outcome"] == "success"
    assert event["selected_candidate_id"] == str(wanted.id)
    assert "original-a" not in str(event) and "original-b" not in str(event)


@pytest.mark.asyncio
async def test_switch_while_manually_paused_stays_paused_until_resume(tmp_path, monkeypatch):
    engine, repository, first, second, _executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    await engine.reconcile_executions()
    await engine.pause(canonical.id)
    artifact = (await repository.artifacts(canonical.id))[0]
    await manual_candidate_failover(engine, canonical.id, artifact.id, str(artifact.candidates[1].id))
    switched = (await repository.artifacts(canonical.id))[0]
    assert switched.selected == 1 and switched.execution is None
    assert (await repository.get(canonical.id)).paused is True
    await engine.reconcile_executions()
    assert (await repository.artifacts(canonical.id))[0].execution is None
    await engine.resume(canonical.id)
    await engine.reconcile_executions()
    resumed = (await repository.artifacts(canonical.id))[0]
    assert resumed.selected == 1 and resumed.execution is not None


@pytest.mark.asyncio
async def test_switch_respects_global_pause(tmp_path, monkeypatch):
    engine, repository, first, second, _executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    await engine.reconcile_executions()
    await engine.pause_all()
    artifact = (await repository.artifacts(canonical.id))[0]
    await manual_candidate_failover(engine, canonical.id, artifact.id, str(artifact.candidates[1].id))
    switched = (await repository.artifacts(canonical.id))[0]
    assert switched.selected == 1 and switched.execution is None
    await engine.reconcile_executions()
    assert (await repository.artifacts(canonical.id))[0].execution is None
    await engine.resume_all()
    await engine.reconcile_executions()
    assert (await repository.artifacts(canonical.id))[0].execution is not None


@pytest.mark.asyncio
async def test_unknown_wrong_and_already_active_candidates_fail_truthfully(tmp_path, monkeypatch):
    engine, repository, first, second, _executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    with pytest.raises(TransferError) as missing:
        await manual_candidate_failover(engine, canonical.id, artifact.id, "not-a-candidate")
    assert missing.value.error.category == Category.SOURCE_NOT_FOUND
    with pytest.raises(TransferError) as active:
        await manual_candidate_failover(engine, canonical.id, artifact.id, str(artifact.candidates[0].id))
    assert active.value.error.category == Category.RESOURCE_STATE_CONFLICT

    other = await engine.submit((TransferRequest(
        "parcel", "different", name="other.bin", preferred_provider=first.descriptor.id,
    ),), deduplicate=False)
    await engine.resolve_pending()
    other_artifact = (await repository.artifacts(other.id))[0]
    with pytest.raises(TransferError) as wrong:
        await manual_candidate_failover(engine, other.id, other_artifact.id, str(artifact.candidates[1].id))
    assert wrong.value.error.category == Category.SOURCE_NOT_FOUND

    failures = [item for item in (await repository.presentation(canonical.id, details=True))["manual_candidate_failovers"]
                if item["outcome"] == "failure"]
    assert len(failures) == 2
    assert all(item["execution_transition"] == "unchanged" for item in failures)


@pytest.mark.asyncio
async def test_disabled_selected_provider_is_rejected_without_substitution(tmp_path, monkeypatch):
    engine, repository, first, second, _executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    second.descriptor = replace(second.descriptor, enabled=False)
    wanted = artifact.candidates[1]
    with pytest.raises(TransferError) as rejected:
        await manual_candidate_failover(engine, canonical.id, artifact.id, str(wanted.id))
    assert rejected.value.error.category == Category.PROVIDER_UNAVAILABLE
    current = (await repository.artifacts(canonical.id))[0]
    assert current.selected == 0 and current.execution is None
    event = (await repository.presentation(canonical.id, details=True))["manual_candidate_failovers"][-1]
    assert event["outcome"] == "failure"
    assert event["requested_candidate_id"] == str(wanted.id)
    assert event["error"]["category"] == Category.PROVIDER_UNAVAILABLE.value


@pytest.mark.asyncio
async def test_duplicate_activation_and_stale_callback_cannot_restore_old_owner(tmp_path, monkeypatch):
    engine, repository, first, second, executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, artifact = await attach_two(engine, repository, first, second)
    await engine.reconcile_executions()
    artifact = (await repository.artifacts(canonical.id))[0]
    old = artifact.execution
    wanted = artifact.candidates[1]
    await manual_candidate_failover(engine, canonical.id, artifact.id, str(wanted.id))
    with pytest.raises(TransferError) as duplicate:
        await manual_candidate_failover(engine, canonical.id, artifact.id, str(wanted.id))
    assert duplicate.value.error.category == Category.RESOURCE_STATE_CONFLICT

    await repository.execution(ExecutionObservation(
        old,
        ExecutionState.TRANSFERRING,
        TransferProgress(4, 3, 1),
        ((await repository.artifacts(canonical.id))[0].target,),
    ))
    current = (await repository.artifacts(canonical.id))[0]
    assert current.selected == 1 and current.execution is None
    attempts = {item.handle.attempt_id: item for item in await repository.executions(canonical.id)}
    assert attempts[old.attempt_id].state == ExecutionState.CANCELLED
    await engine.reconcile_executions()
    active = (await repository.artifacts(canonical.id))[0]
    assert active.selected == 1 and active.execution is not None
    assert len([call for call in executor.calls if call[0] == "start"]) == 2
    successes = [item for item in (await repository.presentation(canonical.id, details=True))["manual_candidate_failovers"]
                 if item["outcome"] == "success"]
    assert len(successes) == 1
