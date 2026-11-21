"""A Usenet server Test works whether or not Usenet is enabled.

The Test is a real NNTP check made through DownloadPulse's own acquisition
service, which runs only while Usenet is required (enabled, or owning work).
So a Test with Usenet disabled brings the service up for itself, and afterwards
leaves it exactly as the lifecycle decides: stopped again while Usenet is still
disabled, kept when it was enabled meanwhile. While any Test runs the service
is required, so an overlapping Test or a periodic ``maintain`` never stops it
under one. The administration is given its runtime and native client as its
constructor takes them (no patching); the runtime records what it was asked.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from executors.sabnzbd.admin import SabnzbdAdministration
from executors.sabnzbd.client import SabTransportError
from test_v113_usenet_service_lifetime import FakeRuntime


class TestingSab:
    """The native client's server Test: answers only while the service runs,
    as the real one does (a refused connection otherwise)."""

    def __init__(self, runtime, *, during=None, hold: asyncio.Event | None = None):
        self.runtime, self.during, self.hold = runtime, during, hold
        self.seen_running = []

    async def test_server(self, **values):
        self.seen_running.append(self.runtime.running)
        if not self.runtime.running:
            raise SabTransportError("connection refused: 127.0.0.1:8090")
        if self.during is not None:
            self.during()
        if self.hold is not None:
            await self.hold.wait()
        return True, "Connection Successful!"


def administration(*, enabled: bool, running: bool, live_work=frozenset(), client_for=None):
    from integrations.usenet.definition import UsenetOptions, UsenetServer
    runtime = FakeRuntime(running=running)
    descriptor = SimpleNamespace(id="sabnzbd", enabled=enabled)
    executor = SimpleNamespace(descriptor=descriptor)

    async def live():
        return frozenset(live_work)
    repository = SimpleNamespace(executors_with_live_work=live)
    client = (client_for or (lambda runtime: TestingSab(runtime)))(runtime)
    admin = SabnzbdAdministration(client, UsenetOptions(servers=[UsenetServer(host="news.a.net")]),
                                  "/download", runtime, executor, repository=repository)

    async def applied():
        return None
    admin.apply_configuration = applied          # configuration push is another test's concern
    return admin, runtime, client, descriptor


def _check(admin):
    return admin.test_server(host="news.a.net", port=563, ssl=True, username="u", password="p", connections=8)


@pytest.mark.asyncio
async def test_a_test_with_usenet_disabled_starts_the_service_for_itself_then_stops_it():
    admin, runtime, client, _ = administration(enabled=False, running=False)
    result = await _check(admin)
    assert result == {"ok": True, "reachable": True, "message": "Connection Successful!"}
    assert client.seen_running == [True]                    # it ran while the check was made
    assert (runtime.starts, runtime.running) == (1, False)  # and is stopped again: Usenet is still off
    assert runtime.stops == 1


@pytest.mark.asyncio
async def test_a_test_with_usenet_enabled_leaves_the_running_service_alone():
    admin, runtime, client, _ = administration(enabled=True, running=True)
    assert (await _check(admin))["ok"] is True
    assert (runtime.starts, runtime.stops, runtime.restarts, runtime.running) == (0, 0, 0, True)


@pytest.mark.asyncio
async def test_usenet_enabled_during_the_test_keeps_the_service_running():
    holder = {}
    admin, runtime, client, descriptor = administration(
        enabled=False, running=False,
        client_for=lambda runtime: TestingSab(runtime, during=lambda: setattr(holder["descriptor"], "enabled", True)))
    holder["descriptor"] = descriptor
    assert (await _check(admin))["ok"] is True
    assert runtime.running is True and runtime.stops == 0


@pytest.mark.asyncio
async def test_overlapping_tests_keep_the_service_until_the_last_one_finishes():
    hold = asyncio.Event()
    admin, runtime, client, _ = administration(
        enabled=False, running=False, client_for=lambda runtime: TestingSab(runtime, hold=hold))
    first, second = asyncio.ensure_future(_check(admin)), asyncio.ensure_future(_check(admin))
    await asyncio.sleep(0.05)
    await admin.maintain()                                  # the periodic lifecycle, mid-Test
    assert runtime.running is True and runtime.stops == 0
    hold.set()
    results = await asyncio.gather(first, second)
    assert all(result["ok"] for result in results)
    assert client.seen_running == [True, True]
    assert (runtime.starts, runtime.stops, runtime.running) == (1, 1, False)


@pytest.mark.asyncio
async def test_a_service_that_cannot_start_is_reported_by_the_test_and_nothing_is_left_running():
    class NeverStarts(FakeRuntime):
        async def start(self):
            self.starts += 1
            return await self.status()
    admin, _, _, _ = administration(enabled=False, running=False)
    admin.runtime = runtime = NeverStarts(running=False)
    admin.client = TestingSab(runtime)
    result = await _check(admin)
    assert result["ok"] is False and result["reachable"] is False
    assert "connection refused" in result["message"]
    assert runtime.running is False
