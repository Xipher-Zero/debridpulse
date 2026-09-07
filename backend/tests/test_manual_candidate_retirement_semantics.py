"""Regression coverage for real-runtime manual candidate retirement semantics."""
from __future__ import annotations

from dataclasses import replace

import pytest

from test_manual_candidate_failover import attach_two, build_engine
from transfers.manual_failover import manual_candidate_failover
from transfers.models import ExecutionState, OutcomeKind, TransferOutcome


@pytest.mark.asyncio
async def test_manual_switch_treats_post_cancel_absence_as_retired_writer(tmp_path, monkeypatch):
    """A daemon forgetting a successfully cancelled writer must not fail its source."""
    engine, repository, first, second, executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, _artifact = await attach_two(engine, repository, first, second)

    await engine.reconcile_executions()
    initial = (await repository.artifacts(canonical.id))[0]
    original = initial.candidates[0]
    alternate = initial.candidates[1]
    retired = initial.execution
    assert retired is not None

    async def forgetful_cancel(handle):
        assert await executor.authorize(handle, "cancel")
        executor.calls.append(("cancel", handle))
        executor.jobs.pop(handle.attempt_id, None)
        return TransferOutcome(OutcomeKind.CANCELLED)

    monkeypatch.setattr(executor, "cancel", forgetful_cancel)

    await manual_candidate_failover(
        engine, canonical.id, initial.id, str(alternate.id),
    )

    attempts = {item.handle.attempt_id: item for item in await repository.executions(canonical.id)}
    assert attempts[retired.attempt_id].state == ExecutionState.CANCELLED

    first_view = await repository.presentation(canonical.id, details=True)
    candidates = {
        item["candidate_id"]: item
        for item in first_view["files"][0]["acquisition_candidates"]
    }
    assert candidates[str(original.id)]["relationship"] == "Original"
    assert "Failed" not in candidates[str(original.id)]["dispositions"]
    assert candidates[str(original.id)]["switch_eligible"] is True

    await engine.reconcile_executions()
    active_alternate = (await repository.artifacts(canonical.id))[0]
    assert active_alternate.selected == 1 and active_alternate.execution is not None

    result = await manual_candidate_failover(
        engine, canonical.id, active_alternate.id, str(original.id),
    )
    assert result["candidate_id"] == str(original.id)
    assert (await repository.artifacts(canonical.id))[0].selected == 0


@pytest.mark.asyncio
async def test_historical_execution_failure_does_not_permanently_veto_candidate(tmp_path, monkeypatch):
    """Historical failure remains visible but retryable source eligibility is current-state driven."""
    engine, repository, first, second, executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, _artifact = await attach_two(engine, repository, first, second)

    await engine.reconcile_executions()
    initial = (await repository.artifacts(canonical.id))[0]
    original = initial.candidates[0]
    alternate = initial.candidates[1]
    original_handle = initial.execution
    assert original_handle is not None

    executor.jobs[original_handle.attempt_id] = replace(
        executor.jobs[original_handle.attempt_id],
        state=ExecutionState.FAILED,
    )

    await manual_candidate_failover(
        engine, canonical.id, initial.id, str(alternate.id),
    )

    view = await repository.presentation(canonical.id, details=True)
    candidates = {
        item["candidate_id"]: item
        for item in view["files"][0]["acquisition_candidates"]
    }
    assert "Failed" in candidates[str(original.id)]["dispositions"]
    assert candidates[str(original.id)]["switch_eligible"] is True

    await engine.reconcile_executions()
    active_alternate = (await repository.artifacts(canonical.id))[0]
    assert active_alternate.selected == 1 and active_alternate.execution is not None

    result = await manual_candidate_failover(
        engine, canonical.id, active_alternate.id, str(original.id),
    )
    assert result["candidate_id"] == str(original.id)
    assert (await repository.artifacts(canonical.id))[0].selected == 0
