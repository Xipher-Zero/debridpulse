"""DP 1.0.13: Provider Status follows authoritative runtime transitions live.

A managed integration's lifecycle owns its runtime transitions. At each one it
calls the application's neutral ``notify_status_changed``, which publishes ONE
generic invalidation event -- no integration id, no state, no endpoint, no
credential. The browser's neutral Provider Status owner reacts by re-observing
every canonical status endpoint, so the endpoint stays the only authority.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.service import ApplicationService
from services import event_bus
from test_v113_usenet_service_lifetime import FakeRuntime, admin_for

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def published(monkeypatch):
    events = []

    async def capture(event_type, payload):
        events.append((event_type, payload))

    monkeypatch.setattr(event_bus, "_publisher", capture)
    return events


@pytest.mark.asyncio
async def test_the_status_event_is_a_bare_neutral_invalidation(published):
    application = ApplicationService(SimpleNamespace(repository=None))
    await application.notify_status_changed()
    # Nothing a browser could mistake for status, and nothing secret.
    assert published == [("integration_status_changed", {})]


def _notifying_admin(**kwargs):
    admin, runtime = admin_for(**kwargs)
    signals = []

    async def notify():
        signals.append(("healthy" if runtime.running and runtime._healthy else "not-healthy",
                        runtime.starts, runtime.restarts, runtime.stops))

    admin._notify_status = notify
    return admin, runtime, signals


@pytest.mark.asyncio
async def test_a_starting_service_announces_non_ready_then_converged():
    applied = []

    async def apply():
        applied.append(True)

    admin, _runtime, signals = _notifying_admin(enabled=True, applied=apply)
    await admin.start()
    # Announced not healthy BEFORE it was started, and again once converged
    # (started and configured) -- the browser re-observes at both points.
    assert signals == [("not-healthy", 0, 0, 0), ("healthy", 1, 0, 0)]
    assert applied == [True]


@pytest.mark.asyncio
async def test_a_healthy_service_announces_nothing():
    admin, runtime, signals = _notifying_admin(enabled=True, runtime=FakeRuntime(running=True))
    await admin.maintain()
    assert signals == [] and runtime.restarts == 0


@pytest.mark.asyncio
async def test_degradation_and_recovery_are_both_announced():
    runtime = FakeRuntime(running=True, healthy=False)  # it was healthy; maintenance finds it is not
    admin, runtime, signals = _notifying_admin(enabled=True, runtime=runtime, applied=_noop)
    await admin.maintain()
    assert signals == [("not-healthy", 0, 0, 0), ("healthy", 0, 1, 0)]


@pytest.mark.asyncio
async def test_a_failed_start_still_announces_its_truthful_outcome():
    class FailingRuntime(FakeRuntime):
        async def start(self):
            self.starts += 1
            return await self.status()

    admin, _runtime, signals = _notifying_admin(enabled=True, runtime=FailingRuntime(), applied=_noop)
    await admin.maintain()
    assert signals == [("not-healthy", 0, 0, 0), ("not-healthy", 1, 0, 0)]


@pytest.mark.asyncio
async def test_a_service_stopped_because_it_is_no_longer_required_is_announced():
    admin, runtime, signals = _notifying_admin(enabled=False, runtime=FakeRuntime(running=True))
    await admin.maintain()
    assert runtime.stops == 1 and signals == [("not-healthy", 0, 0, 1)]


async def _noop():
    return None


def test_the_usenet_lifecycle_receives_the_neutral_notifier():
    from integrations.usenet import definition

    commands = SimpleNamespace(notify_status_changed=object())
    environment = SimpleNamespace(repository=SimpleNamespace(authorize_execution=None,
                                                             converge_staged_input=None),
                                  download_root="/download", commands=commands, staged_input=None)
    _provider, executor = definition.build(definition.UsenetOptions(), environment)
    assert executor.lifecycle._notify_status is commands.notify_status_changed


def test_neutral_startup_and_event_owners_name_no_integration():
    import inspect

    lifecycle_owner = "".join(inspect.getsource(getattr(ApplicationService, name)) for name in (
        "notify_status_changed", "start_integrations", "begin_integrations", "integrations_started",
        "stop_integrations", "maintain_integrations"))
    sources = {
        "lifetime": (ROOT / "backend" / "application" / "lifetime.py").read_text(),
        "scheduler": (ROOT / "backend" / "core" / "scheduler.py").read_text(),
        "service": lifecycle_owner,
        "status": (ROOT / "frontend" / "static" / "ui-provider-status.js").read_text(),
    }
    for name, source in sources.items():
        lowered = source.lower()
        for integration in ("realdebrid", "alldebrid", "usenet", "sabnzbd", "aria2"):
            assert integration not in lowered, (name, integration)
    start = sources["lifetime"].split("async def start_application", 1)[1].split("async def stop_application", 1)[0]
    assert "application.begin_integrations()" in start
    assert "start_integrations" not in start and "recover()" not in start


def test_the_live_signal_reaches_the_provider_status_owner_without_a_timer():
    app = (ROOT / "frontend" / "static" / "app.js").read_text()
    status = (ROOT / "frontend" / "static" / "ui-provider-status.js").read_text()
    forward = app.split("'integration_status_changed'", 1)[1].split("es.addEventListener", 1)[0]
    assert "new CustomEvent('debridpulse:integration-status-changed')" in forward
    assert "JSON.parse" not in forward  # nothing in the event is read
    assert "document.addEventListener('debridpulse:integration-status-changed', reobserve)" in status
    assert "document.addEventListener('debridpulse:pulse-connected', reobserve)" in status
    assert "const reobserve = () => refresh()" in status
    for timer in ("setInterval", "setTimeout"):
        assert timer not in status
    # The one existing fallback cadence is unchanged; nothing was added beside it.
    assert app.count("()=>checkConnections().catch(()=>{})") == 1
    assert "setInterval" not in forward


def test_scheduler_loops_that_need_integrations_wait_for_their_startup():
    import inspect
    from core import scheduler

    for loop in (scheduler.sync_status_loop, scheduler.full_sync_loop,
                 scheduler.sync_download_clients_loop, scheduler.integration_maintenance_loop):
        body = inspect.getsource(loop)
        assert body.index("await application.integrations_started()") < body.index("while True:")
    for loop in (scheduler.throughput_sampling_loop, scheduler.postprocessing_loop,
                 scheduler.application_events_loop, scheduler.disk_guard_loop):
        assert "integrations_started" not in inspect.getsource(loop)


@pytest.mark.asyncio
async def test_waiting_for_startup_never_cancels_it():
    release = asyncio.Event()

    class Held:
        async def start(self):
            await release.wait()

        async def stop(self):
            return None

        async def maintain(self):
            return None

    application = ApplicationService(SimpleNamespace(repository=None, checkpoint_live_material=None),
                                     lifecycle=(Held(),))
    application.begin_integrations()
    waiter = asyncio.create_task(application.integrations_started())
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert not application._integration_startup.done()
    release.set()
    await application.integrations_started()
    assert application._integration_startup.done() and not application._integration_startup.cancelled()
