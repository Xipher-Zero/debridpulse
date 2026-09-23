"""1.0.13 Gate-9 rev-4, item 3: service lifetime is not the routing toggle.

The Usenet Enable toggle governs whether the provider/executor may accept NEW
work. It must never make the native service unavailable to executions that are
already durable -- those must remain observable, controllable and recoverable,
exactly as they would be for any internal executor.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import db.database as database
from executors.sabnzbd.admin import SabnzbdAdministration
from transfers.models import ExecutionState


class FakeRuntime:
    """Records supervision decisions without launching a process."""

    def __init__(self, *, running=False, healthy=True):
        self.running = running
        self._healthy = healthy
        self.starts = 0
        self.stops = 0
        self.restarts = 0

    async def start(self):
        self.starts += 1
        self.running = True
        self._healthy = True
        return await self.status()

    async def stop(self):
        self.stops += 1
        self.running = False
        return await self.status()

    async def restart(self):
        self.restarts += 1
        self.running = True
        self._healthy = True
        return await self.status()

    async def status(self):
        return {"running": self.running, "endpoint": "http://127.0.0.1:8090"}

    async def healthy(self):
        return self.running and self._healthy


def admin_for(*, enabled, live_work=frozenset(), runtime=None, applied=None):
    from integrations.usenet.definition import UsenetOptions, UsenetServer
    from sab_fakes import FakeSab

    runtime = runtime or FakeRuntime()
    options = UsenetOptions(servers=[UsenetServer(host="news.a.net")])
    executor = SimpleNamespace(descriptor=SimpleNamespace(id="sabnzbd", enabled=enabled))
    repository = SimpleNamespace(
        executors_with_live_work=lambda: _immediate(frozenset(live_work)))
    admin = SabnzbdAdministration(FakeSab(), options, "/download", runtime, executor,
                                  repository=repository)
    if applied is not None:
        admin.apply_configuration = applied
    return admin, runtime


async def _immediate(value):
    return value


# --- lifetime is driven by durable work, never by the toggle alone -------

@pytest.mark.asyncio
async def test_an_enabled_integration_runs_the_service():
    admin, runtime = admin_for(enabled=True)
    await admin.maintain()
    assert runtime.running is True


@pytest.mark.asyncio
async def test_a_disabled_integration_with_owned_work_keeps_the_service_available():
    """Disabling stops NEW routing; it must not strand a durable execution."""
    admin, runtime = admin_for(enabled=False, live_work={"sabnzbd"})
    await admin.maintain()
    assert runtime.running is True, "a durable execution still needs its executor"
    assert runtime.stops == 0


@pytest.mark.asyncio
async def test_a_disabled_integration_with_no_owned_work_may_stop_the_service():
    admin, runtime = admin_for(enabled=False, live_work=frozenset())
    runtime.running = True
    await admin.maintain()
    assert runtime.running is False


@pytest.mark.asyncio
async def test_stopping_is_never_decided_by_the_toggle_alone():
    """Even disabled, the presence of owned work wins over the toggle."""
    admin, runtime = admin_for(enabled=False, live_work={"sabnzbd"})
    runtime.running = True
    await admin.maintain()
    assert runtime.stops == 0 and runtime.running is True


# --- convergence ---------------------------------------------------------

@pytest.mark.asyncio
async def test_an_alive_but_unhealthy_service_is_actually_recovered():
    """start() no-ops on a live process, so maintenance must restart it."""
    runtime = FakeRuntime(running=True, healthy=False)
    admin, runtime = admin_for(enabled=True, runtime=runtime)
    await admin.maintain()
    assert runtime.restarts == 1, "an unhealthy live service must be restarted"
    assert await runtime.healthy() is True


@pytest.mark.asyncio
async def test_enabling_converges_without_a_second_save():
    """disabled -> enabled must yield a running service AND applied config."""
    applied = []

    async def apply_configuration():
        from integrations.definition import ConfigurationApplication
        applied.append(True)
        return ConfigurationApplication(True, "configuration applied")

    admin, runtime = admin_for(enabled=True, applied=apply_configuration)
    await admin.maintain()
    assert runtime.running is True
    assert applied, "canonical configuration must be applied on convergence"


@pytest.mark.asyncio
async def test_a_healthy_running_service_is_left_alone():
    runtime = FakeRuntime(running=True, healthy=True)
    admin, runtime = admin_for(enabled=True, runtime=runtime)
    await admin.maintain()
    assert runtime.restarts == 0 and runtime.stops == 0


# --- the executor itself is never gated by the toggle -------------------

@pytest.mark.asyncio
async def test_an_application_restart_while_disabled_still_serves_durable_work():
    """Composition calls start() on boot, not maintain().

    Restarting DebridPulse while Usenet is switched off must still bring the
    service up when a durable execution belongs to it -- otherwise the restart
    is exactly when an in-flight download becomes permanently unobservable.
    """
    admin, runtime = admin_for(enabled=False, live_work={"sabnzbd"})
    await admin.start()
    assert runtime.running is True
    assert runtime.starts == 1


@pytest.mark.asyncio
async def test_an_application_restart_while_disabled_and_idle_starts_nothing():
    admin, runtime = admin_for(enabled=False)
    await admin.start()
    assert runtime.running is False
    assert runtime.starts == 0


@pytest.mark.asyncio
async def test_an_unanswerable_durable_question_keeps_the_service_available():
    """Uncertainty is not evidence of idleness.

    If the durable answer cannot be obtained, stopping would strand whatever
    it could not see, so the service stays up.
    """
    from types import SimpleNamespace as NS

    admin, runtime = admin_for(enabled=False, live_work={"sabnzbd"})

    async def unavailable():
        raise RuntimeError("repository unavailable")

    admin.repository = NS(executors_with_live_work=unavailable)
    runtime.running = True
    await admin.maintain()
    assert runtime.running is True
    assert runtime.stops == 0


@pytest.mark.asyncio
async def test_observation_of_a_durable_execution_survives_disabling(tmp_path, monkeypatch):
    """A disabled integration's durable execution is still observable."""
    from executors.sabnzbd.executor import SabnzbdConfiguration, SabnzbdExecutor
    from sab_fakes import FakeSab
    from test_v113_sabnzbd_executor import request_for

    root = tmp_path / "download"
    (root / ".dpwork" / "complete").mkdir(parents=True)
    (root / ".dpwork" / "incomplete").mkdir(parents=True)
    sab = FakeSab(complete_dir=str(root / ".dpwork" / "complete"),
                  download_dir=str(root / ".dpwork" / "incomplete"))

    async def authorize(handle, action):
        return True

    executor = SabnzbdExecutor(sab, SabnzbdConfiguration(
        local_root=str(root), working_directory=str(root / ".dpwork"),
        complete_directory=str(root / ".dpwork" / "complete")), authorize)

    request = request_for(str(root))
    bound = (await executor.start(request, executor.prepare(request))).handle

    # Routing is switched off; the executor descriptor is disabled.
    executor.descriptor = executor.descriptor.__class__(
        executor.descriptor.id, executor.descriptor.name, executor.descriptor.capabilities,
        enabled=False)

    observed = (await executor.observe_many((bound,))).observations[0]
    assert observed.state in {ExecutionState.QUEUED, ExecutionState.RUNNING}
    assert observed.handle.native["nzo_id"] == bound.native["nzo_id"]
    cancelled = await executor.cancel(bound)
    assert cancelled.state in {ExecutionState.CANCELLED, ExecutionState.ABSENT}
