"""Regression coverage for the 1 Hz active-transfer reconciliation cadence.

TASK_DP_1.0.12_Active_Transfer_1Hz_Progress_Update: the only intended
behavioral change is the steady-state execution reconciliation cadence
moving from ~2s to ~1s. These tests pin the two numeric cadence owners
(``ApplicationService.execution_poll_interval`` and the scheduler's
``max(..., application.execution_poll_interval)`` floor) and confirm the
existing event-driven wakeup and bounded batched-progress-publication
architecture (already covered by test_active_state_overlay.py) survives
the faster cadence unchanged.
"""
import asyncio

import pytest

import core.scheduler as scheduler_module
from application.service import ApplicationService


def test_execution_poll_interval_default_is_one_second():
    """ApplicationService.execution_poll_interval must default to 1.

    Against the pre-change source (self.execution_poll_interval = 2) this
    assertion fails; it passes once the cadence owner is changed to 1.
    """
    engine = type("_Engine", (), {"repository": object()})()
    application = ApplicationService(engine=engine)
    assert application.execution_poll_interval == 1


class _FakeApplication:
    def __init__(self, poll_interval):
        self.execution_wakeup = asyncio.Event()
        self.execution_poll_interval = poll_interval

    def application_storage_permitted(self):
        # Skip the reconcile_executions() body entirely -- this test targets
        # only the wait-timeout the scheduler loop computes and passes to
        # _wait_for_work, not reconciliation behavior itself.
        return False


class _StopLoop(Exception):
    """Sentinel used to escape sync_download_clients_loop()'s `while True`."""


@pytest.mark.asyncio
async def test_scheduler_execution_reconciliation_floor_permits_one_second(monkeypatch):
    """The scheduler must not force a >1 poll_interval back up to a 2s floor.

    Against the pre-change source (max(2, application.execution_poll_interval))
    this assertion fails for a poll_interval of 1; it passes once the floor
    becomes max(1, application.execution_poll_interval).
    """
    recorded_timeouts = []

    async def _capture_wait(_event, timeout):
        recorded_timeouts.append(timeout)
        raise _StopLoop

    monkeypatch.setattr(scheduler_module, "application", _FakeApplication(poll_interval=1))
    monkeypatch.setattr(scheduler_module, "_wait_for_work", _capture_wait)

    with pytest.raises(_StopLoop):
        await scheduler_module.sync_download_clients_loop()

    assert recorded_timeouts == [1]


@pytest.mark.asyncio
async def test_explicit_execution_wakeup_still_preempts_the_steady_state_wait():
    """An execution_wakeup signal must still wake reconciliation before the
    steady-state timeout elapses -- the faster cadence must not have been
    implemented by discarding the existing event-driven wakeup."""
    event = asyncio.Event()
    event.set()

    # If _wait_for_work still awaits event.wait() first, this returns
    # immediately; a regression to an unconditional sleep would instead
    # block for the full timeout and trip the outer guard below.
    await asyncio.wait_for(scheduler_module._wait_for_work(event, 5), timeout=0.5)


@pytest.mark.asyncio
async def test_wait_for_work_still_bounds_on_timeout_without_a_signal():
    """Without a signal, the wait must still resolve at the bounded timeout
    (no busy loop, no indefinite block) rather than raising."""
    event = asyncio.Event()
    await scheduler_module._wait_for_work(event, 0.05)
    assert not event.is_set()
