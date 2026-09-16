"""DP 1.0.12 canonical architecture correction, Gate 6 / specification section
13.3: the required regression proof for the speed-cap collision.

Hold a deliberately long admitted resolution operation (``ApplicationMaintenanceGate
.operation()``, the same admission ``ApplicationService.resolve_pending()`` /
``reconcile_executions()`` use); while it is active, apply a download speed
limit and a universal-concurrency change. Neither request may wait for the
unrelated resolution operation via ``ApplicationMaintenanceGate.maintenance()``,
and unrelated new mutations must not be rejected as maintenance-active solely
because these executor-local changes are being applied.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from api import routes
from services.maintenance_gate import ApplicationMaintenanceActive, ApplicationMaintenanceGate
from transfers.runtime_limits import ExecutionRuntimeLimits


def _application(gate: ApplicationMaintenanceGate, current):
    fake_aria2 = SimpleNamespace(
        change_global_options=AsyncMock(),
        get_global_options=AsyncMock(return_value={}),
        apply_memory_tuning=AsyncMock(),
    )
    return SimpleNamespace(
        integration_admin=lambda _identity: fake_aria2,
        definitions=(),
        application_operation=gate.operation,
        configuration_admission=gate.maintenance,
        configure=lambda: None,
        reconcile_executions=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_bandwidth_change_does_not_wait_behind_an_outstanding_resolution_operation():
    gate = ApplicationMaintenanceGate()
    current = routes.AppSettings()
    application = _application(gate, current)

    resolution_entered = asyncio.Event()
    release_resolution = asyncio.Event()

    async def long_resolution_operation():
        async with gate.operation():
            resolution_entered.set()
            await release_resolution.wait()

    resolution_task = asyncio.create_task(long_resolution_operation())
    await resolution_entered.wait()
    try:
        with patch("api.routes.get_settings", return_value=current), \
             patch("api.routes.load_settings", return_value=current), \
             patch("api.routes.save_settings"), \
             patch("api.routes.apply_settings"):
            result = await asyncio.wait_for(
                routes.patch_execution_runtime_limits(
                    {"max_download_bytes_per_second": 2_000_000}, application=application,
                ),
                timeout=1.0,
            )
        assert result["configured"]["max_download_bytes_per_second"] == 2_000_000
    finally:
        release_resolution.set()
        await resolution_task


@pytest.mark.asyncio
async def test_concurrency_change_does_not_wait_behind_an_outstanding_resolution_operation():
    gate = ApplicationMaintenanceGate()
    current = routes.AppSettings(max_concurrent_downloads=1, aria2_max_active_downloads=1)
    application = _application(gate, current)

    resolution_entered = asyncio.Event()
    release_resolution = asyncio.Event()

    async def long_resolution_operation():
        async with gate.operation():
            resolution_entered.set()
            await release_resolution.wait()

    resolution_task = asyncio.create_task(long_resolution_operation())
    await resolution_entered.wait()
    try:
        with patch("api.routes.get_settings", return_value=current), \
             patch("api.routes.load_settings", return_value=current), \
             patch("api.routes.save_settings"), \
             patch("api.routes.apply_settings"):
            result = await asyncio.wait_for(
                routes.aria2_set_global_options({"max_concurrent_downloads": 5}, application=application),
                timeout=1.0,
            )
        assert result["ok"] is True
    finally:
        release_resolution.set()
        await resolution_task


@pytest.mark.asyncio
async def test_unrelated_new_mutation_is_not_rejected_while_tuning_applies():
    """The executor-local change itself must not close admission to new
    mutations while it runs -- it uses ``.operation()``, which coexists with
    other concurrent ``.operation()`` callers rather than excluding them the
    way ``.maintenance()`` would."""
    gate = ApplicationMaintenanceGate()
    current = routes.AppSettings()
    application = _application(gate, current)

    tuning_entered = asyncio.Event()
    release_tuning = asyncio.Event()

    async def held_tuning_operation():
        async with gate.operation():
            tuning_entered.set()
            await release_tuning.wait()

    tuning_task = asyncio.create_task(held_tuning_operation())
    await tuning_entered.wait()
    try:
        async with gate.operation():
            pass  # an unrelated concurrent mutation is admitted, not rejected
    finally:
        release_tuning.set()
        await tuning_task


@pytest.mark.asyncio
async def test_control_case_the_old_maintenance_admission_would_have_collided():
    """Negative control proving the regression this fix addresses is real:
    the STRONGER ``.maintenance()`` admission the routes used before this
    correction genuinely does wait behind an outstanding ``.operation()`` and
    genuinely does reject a concurrent new one meanwhile."""
    gate = ApplicationMaintenanceGate()
    resolution_entered = asyncio.Event()
    release_resolution = asyncio.Event()

    async def long_resolution_operation():
        async with gate.operation():
            resolution_entered.set()
            await release_resolution.wait()

    resolution_task = asyncio.create_task(long_resolution_operation())
    await resolution_entered.wait()

    maintenance_started = asyncio.Event()

    async def hold_maintenance():
        async with gate.maintenance():
            maintenance_started.set()

    maintenance_task = asyncio.create_task(hold_maintenance())
    await asyncio.sleep(0.05)  # let .maintenance() set _maintenance_active and begin waiting
    assert not maintenance_started.is_set(), ".maintenance() must still be waiting on the outstanding operation"
    assert gate.active

    try:
        with pytest.raises(ApplicationMaintenanceActive):
            async with gate.operation():
                pass
    finally:
        release_resolution.set()
        await resolution_task
        await maintenance_task
