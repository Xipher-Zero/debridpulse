"""Regression coverage for reversible operator-requested candidate switching."""
from __future__ import annotations

import pytest

from test_manual_candidate_failover import attach_two, build_engine
from transfers.manual_failover import manual_candidate_failover


@pytest.mark.asyncio
async def test_manual_switch_can_return_to_original_candidate(tmp_path, monkeypatch):
    """A writer retired by A->B remains an eligible exact candidate for B->A."""
    engine, repository, first, second, _executor = await build_engine(tmp_path, monkeypatch)
    canonical, _source, _artifact = await attach_two(engine, repository, first, second)

    await engine.reconcile_executions()
    initial = (await repository.artifacts(canonical.id))[0]
    original = initial.candidates[0]
    alternate = initial.candidates[1]

    first_switch = await manual_candidate_failover(
        engine, canonical.id, initial.id, str(alternate.id),
    )
    assert first_switch["candidate_id"] == str(alternate.id)

    after_first = (await repository.artifacts(canonical.id))[0]
    assert after_first.selected == 1
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

    second_switch = await manual_candidate_failover(
        engine, canonical.id, active_alternate.id, str(original.id),
    )
    assert second_switch["candidate_id"] == str(original.id)
    assert second_switch["provider_id"] == "provider-a"

    restored = (await repository.artifacts(canonical.id))[0]
    assert restored.selected == 0
    restored_view = await repository.presentation(canonical.id, details=True)
    restored_candidates = {
        item["candidate_id"]: item
        for item in restored_view["files"][0]["acquisition_candidates"]
    }
    assert restored_candidates[str(original.id)]["is_active"] is True
    assert restored_candidates[str(original.id)]["switch_eligible"] is False
    assert "Failed" not in restored_candidates[str(alternate.id)]["dispositions"]
    assert restored_candidates[str(alternate.id)]["switch_eligible"] is True

    successes = [
        item for item in restored_view["manual_candidate_failovers"]
        if item["outcome"] == "success"
    ]
    assert [item["selected_candidate_id"] for item in successes[-2:]] == [
        str(alternate.id), str(original.id),
    ]
