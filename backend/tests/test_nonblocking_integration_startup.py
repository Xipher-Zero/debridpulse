"""DP 1.0.13: control-plane readiness never waits for provider/executor readiness.

``application.lifetime.start_application`` is the one startup sequence. It
blocks only on DebridPulse-owned core prerequisites; integration startup is
begun -- and owned, observed and cancelled -- by the one integration lifecycle
owner (``ApplicationService``), and the scheduler loops that need started
integrations wait for it. Their first cycles are the startup recovery.

Every boundary below is proven with events the test controls, never with a
sleep: an integration start is HELD on an ``asyncio.Event`` and the assertions
are made while it is held.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
import pytest_asyncio

from application import lifetime
from application.service import ApplicationService
from core import scheduler

pytestmark = pytest.mark.asyncio


class Engine:
    """The core the lifetime owner initializes before serving."""

    repository = None

    def __init__(self):
        self.initialized = asyncio.Event()
        self.policy = SimpleNamespace(resource_poll_interval=3600)
        self.resolution_deadline = None

    async def initialize(self):
        self.initialized.set()

    async def recover_postprocessing(self):
        return None

    async def checkpoint_live_material(self, _reason):
        return None

    async def sample_throughput(self):
        return None

    @staticmethod
    def clock():
        return 0.0


class Integration:
    """A lifecycle component whose ``start`` can be held open."""

    def __init__(self, *, held=False, fails=False):
        self.release = asyncio.Event()
        if not held:
            self.release.set()
        self.fails = fails
        self.starting = asyncio.Event()
        self.started = asyncio.Event()
        self.ready = False
        self.cancelled = False
        self.stops = 0
        self.maintained = asyncio.Event()

    async def start(self):
        self.starting.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.fails:
            raise RuntimeError("the service could not be started")
        self.ready = True
        self.started.set()

    async def stop(self):
        self.stops += 1
        self.ready = False

    async def maintain(self):
        self.maintained.set()


class Application(ApplicationService):
    """The real lifecycle owner; transfer work is only recorded."""

    def __init__(self, *lifecycle):
        super().__init__(Engine(), lifecycle=lifecycle)
        self.calls = []
        self.cycles = {name: asyncio.Event() for name in ("inventory", "resolution", "executions")}

    async def reconcile_inventory(self):
        self.calls.append("inventory")
        self.cycles["inventory"].set()
        return {"imported": 0, "updated": 0, "errors": []}

    async def resolve_pending(self):
        self.calls.append("resolution")
        self.cycles["resolution"].set()

    async def reconcile_executions(self):
        self.calls.append("executions")
        self.cycles["executions"].set()

    async def process_postprocessors(self):
        return None

    async def deliver_events(self):
        return None

    async def check_resources(self):
        return {"enabled": False, "active": False}

    async def recover(self):
        self.calls.append("broad-recovery")
        raise AssertionError("startup must not run a separate broad recovery pass")


@pytest_asyncio.fixture(autouse=True)
async def scheduler_settings(monkeypatch):
    # A five-minute inventory cadence: the startup inventory must not wait for it.
    monkeypatch.setattr(scheduler, "get_settings", lambda: SimpleNamespace(full_sync_interval_minutes=5))
    yield
    await scheduler.stop_scheduler()


async def _settled(event):
    await asyncio.wait_for(event.wait(), timeout=5)


async def test_control_plane_is_ready_while_an_integration_start_is_held():
    slow, other = Integration(held=True), Integration()
    application = Application(slow, other)

    startup = asyncio.create_task(lifetime.start_application(application))
    await _settled(slow.starting)

    # The held integration has begun converging, and the control plane did not
    # wait for it: the one startup sequence is already complete.
    assert application.engine.initialized.is_set()
    assert startup.done() and startup.exception() is None
    assert not slow.ready
    # Integrations converge independently: the one after it is not held up.
    await _settled(other.started)
    # Work that needs started integrations has not begun against a held one.
    assert application.calls == []

    slow.release.set()
    await _settled(application.cycles["executions"])
    assert slow.ready
    await lifetime.stop_application(application)


async def test_a_failed_integration_start_degrades_only_that_integration(caplog):
    broken, healthy = Integration(fails=True), Integration()
    application = Application(broken, healthy)

    with caplog.at_level(logging.WARNING, logger="debridpulse.application"):
        await lifetime.start_application(application)
        await _settled(healthy.maintained)

    # The failure was observed and logged, the next integration still started,
    # the transfer loops still run, and maintenance keeps owning the broken one.
    assert not broken.ready and healthy.ready
    assert broken.maintained.is_set()
    assert "Integration startup failed" in caplog.text
    await application.integrations_started()
    startup = application._integration_startup
    assert startup.done() and not startup.cancelled() and startup.exception() is None
    await lifetime.stop_application(application)


async def test_startup_recovery_is_the_scheduler_owners_first_cycles():
    slow = Integration(held=True)
    application = Application(slow)
    await lifetime.start_application(application)
    await _settled(slow.starting)
    assert application.calls == []

    slow.release.set()
    for cycle in application.cycles.values():
        await _settled(cycle)
    await _settled(slow.maintained)
    # Inventory, resolution, execution reconciliation and integration
    # maintenance each ran through its one canonical loop -- inventory without
    # waiting out its five-minute cadence -- and no broad recovery pass ran.
    assert {"inventory", "resolution", "executions"} <= set(application.calls)
    assert application.calls.count("inventory") == 1
    assert "broad-recovery" not in application.calls
    await lifetime.stop_application(application)


async def test_shutdown_cancels_an_in_flight_integration_start():
    slow, other = Integration(held=True), Integration()
    application = Application(other, slow)
    await lifetime.start_application(application)
    await _settled(slow.starting)

    await lifetime.stop_application(application)

    # The held start was cancelled and drained before the stops ran, nothing
    # the startup owned is still running, and the loops that waited for it
    # never ran against this application.
    startup = application._integration_startup
    assert slow.cancelled and startup.done()
    assert (other.stops, slow.stops) == (1, 1)
    assert not scheduler.scheduler_running()
    assert application.calls == []
    assert not [task for task in asyncio.all_tasks()
                if task is not asyncio.current_task() and "start_integrations" in repr(task.get_coro())]
