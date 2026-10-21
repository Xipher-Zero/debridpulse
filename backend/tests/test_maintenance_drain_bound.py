"""Exclusive maintenance waits for already-admitted work only for a bounded
time (ARCH-A03): while it waits, admission is closed and the engine is held,
so a request that never finishes (an upload still streaming its body) must not
hold everything forever. Past the bound maintenance is NOT entered, admission
reopens, nothing in flight is interrupted, and the refusal is reported as such."""
from __future__ import annotations

import asyncio

import pytest

from services.maintenance_gate import (
    ApplicationMaintenanceActive, ApplicationMaintenanceBusy, ApplicationMaintenanceGate,
)


@pytest.mark.asyncio
async def test_maintenance_behind_work_that_does_not_finish_gives_up_and_reopens_admission():
    gate = ApplicationMaintenanceGate(drain_seconds=0.05)
    release, admitted = asyncio.Event(), asyncio.Event()
    finished = []

    async def streaming_upload():
        async with gate.operation():
            admitted.set()
            await release.wait()          # the body is still arriving
            finished.append(True)

    upload = asyncio.create_task(streaming_upload())
    await admitted.wait()
    entered = []

    async def maintain():
        async with gate.maintenance():
            entered.append(True)

    with pytest.raises(ApplicationMaintenanceBusy) as busy:
        await asyncio.wait_for(maintain(), 2)   # bounded: never hangs
    assert busy.value.waiting == 1
    assert entered == []                         # nothing of the maintenance ran
    assert gate.active is False                  # admission reopened
    async with gate.operation():                 # new work is admitted again
        pass
    assert not upload.done()                     # the in-flight work was not interrupted
    release.set()
    await upload
    assert finished == [True]
    # And maintenance can be entered once the work has finished.
    await asyncio.wait_for(maintain(), 2)
    assert entered == [True]


@pytest.mark.asyncio
async def test_maintenance_still_drains_work_that_finishes_within_the_bound():
    """Control: admitted work that ends in time is drained, then maintenance runs
    with admission closed to everything else."""
    gate = ApplicationMaintenanceGate(drain_seconds=2)
    admitted = asyncio.Event()

    async def short_request():
        async with gate.operation():
            admitted.set()
            await asyncio.sleep(0.05)

    request = asyncio.create_task(short_request())
    await admitted.wait()
    async def other_request():
        async with gate.operation():
            pass

    async with gate.maintenance():
        assert request.done()
        # Any other task is refused while maintenance owns admission (the
        # maintenance owner's own task may still enter, by design).
        with pytest.raises(ApplicationMaintenanceActive):
            await asyncio.create_task(other_request())
    assert gate.active is False


@pytest.mark.asyncio
async def test_a_busy_refusal_is_answered_as_nothing_changed():
    from main import application_maintenance_busy_handler

    response = await application_maintenance_busy_handler(None, ApplicationMaintenanceBusy(2, 60))
    assert response.status_code == 409
    assert b"nothing was changed" in response.body
