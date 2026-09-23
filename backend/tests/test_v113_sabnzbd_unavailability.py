"""1.0.13: prolonged SAB unreachability is never evidence of absence or failure.

Gate 1 proved the discriminator against a real SABnzbd 5.1.3:
  transport failure                                  -> UNKNOWN (keep ownership)
  valid answer, absent from queue AND history        -> ABSENT
A SAB restart preserves nzo_id, job name and state, so the SAME execution
reconciles without resubmission.
"""
from __future__ import annotations

import pytest

from transfers.models import ExecutionState

from sab_fakes import FakeSab  # noqa: F401
from test_v113_sabnzbd_executor import lab, request_for  # noqa: F401


async def started(lab):
    sab, executor = lab.sab, lab.executor
    request = request_for(lab.root)
    handle = executor.prepare(request)
    bound = (await executor.start(request, handle)).handle
    # Prove it is observable at least once before the outage.
    first = (await executor.observe_many((bound,))).observations[0]
    assert first.state in {ExecutionState.QUEUED, ExecutionState.RUNNING}
    return executor, bound


@pytest.mark.asyncio
async def test_many_consecutive_failures_never_yield_absent_or_failed(lab):
    sab = lab.sab
    executor, bound = await started(lab)
    sab.reachable = False
    for _ in range(25):
        snapshot = await executor.observe_many((bound,))
        for observation in snapshot.observations:
            assert observation.state not in {ExecutionState.ABSENT, ExecutionState.FAILED,
                                             ExecutionState.CANCELLED, ExecutionState.SUCCEEDED}
    # And nothing was ever resubmitted.
    assert len(sab.submissions) == 1


@pytest.mark.asyncio
async def test_same_execution_reconciles_after_the_outage(lab):
    sab = lab.sab
    executor, bound = await started(lab)
    nzo = bound.native["nzo_id"]
    sab.reachable = False
    for _ in range(10):
        await executor.observe_many((bound,))
    sab.reachable = True
    observation = (await executor.observe_many((bound,))).observations[0]
    assert observation.handle.native["nzo_id"] == nzo
    assert observation.state in {ExecutionState.QUEUED, ExecutionState.RUNNING}
    assert len(sab.submissions) == 1


@pytest.mark.asyncio
async def test_job_completing_during_the_outage_is_recovered_from_history(lab):
    sab = lab.sab
    executor, bound = await started(lab)
    nzo = bound.native["nzo_id"]
    sab.reachable = False
    for _ in range(5):
        await executor.observe_many((bound,))
    # It finished while DP could not see it.
    sab.finish(nzo)
    sab.reachable = True
    observation = (await executor.observe_many((bound,))).observations[0]
    assert observation.state == ExecutionState.SUCCEEDED
    assert observation.handle.native["nzo_id"] == nzo
    assert len(sab.submissions) == 1


@pytest.mark.asyncio
async def test_api_authentication_failure_is_not_absence(lab):
    sab = lab.sab
    executor, bound = await started(lab)
    sab.authorized = False
    snapshot = await executor.observe_many((bound,))
    for observation in snapshot.observations:
        assert observation.state not in {ExecutionState.ABSENT, ExecutionState.FAILED}


@pytest.mark.asyncio
async def test_positive_absence_requires_a_valid_answer_from_both_lists(lab):
    sab = lab.sab
    executor, bound = await started(lab)
    sab.queue.clear()          # valid answer; absent from queue AND history
    observation = (await executor.observe_many((bound,))).observations[0]
    assert observation.state == ExecutionState.ABSENT
