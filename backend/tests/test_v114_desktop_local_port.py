"""The desktop application's Local Port (application.desktop_listener).

External applications reach the desktop application at a port that stays put
-- Local Port, on this computer only -- while the window keeps its own private
port. Each case starts a real desktop backend the way the supervisor does (its
control channel, its internal port given as DOWNLOADPULSE_DESKTOP_PORT), on a
disposable root, and talks to it over real sockets: nothing patched.

* The application is served on Local Port, byte for byte the same app.
* A change applies at once: the new port serves, the old one stops accepting,
  a connection already open on it finishes, and the saved setting follows.
* A port another program holds changes nothing; an invalid one is refused.
* A port taken at launch leaves the application running and says so (the
  read and the supervisor's status); saving it once free binds it.
* A token's use is recorded (at most every five minutes), and the clients
  seen in the past week are what a change would cut off.
* A server installation has no Local Port.
"""
from __future__ import annotations

import asyncio
import json
import socket

import aiohttp
import pytest

from test_v114_desktop_control import Backend
from desktop.supervisor import free_port


def _port_backend(tmp_path, port: int, **kwargs) -> Backend:
    backend = Backend(tmp_path, locked=False, config_extra={"desktop_local_port": port}, **kwargs)
    backend.env = {**backend.env, "DOWNLOADPULSE_DESKTOP_PORT": str(backend.port)}
    return backend


async def _get(port: int, path: str, **kwargs) -> tuple[int, object]:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}{path}", **kwargs) as reply:
            try:
                return reply.status, await reply.json(content_type=None)
            except ValueError:
                return reply.status, None


async def _refused(port: int) -> bool:
    try:
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return True
    writer.close()
    return False


async def _raw_health(reader, writer) -> bytes:
    writer.write(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    await writer.drain()
    return await asyncio.wait_for(reader.readuntil(b"\r\n"), 15)


def _saved_port(backend: Backend) -> int:
    return json.loads((backend.root / "config" / "config.json").read_text(encoding="utf-8"))["desktop_local_port"]


def _occupy() -> tuple[socket.socket, int]:
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    return holder, holder.getsockname()[1]


@pytest.mark.asyncio
async def test_local_port_serves_the_application_and_moves_at_once(tmp_path):
    first, second = free_port(), free_port()
    backend = await _port_backend(tmp_path, first).start()
    try:
        assert await backend.ready()
        status, body = await _get(first, "/api/health")
        assert status == 200 and body["status"] == "ok"                       # the same application, relayed
        status, reading = await backend.exchange("GET", "/api/settings/local-port")
        assert status == 200 and reading == {"port": first, "listening": True, "recent_clients": []}
        assert (await backend.channel.ask("status"))["status"]["local_port"] == {"port": first, "listening": True}

        # A connection open on the old port before the move.
        reader, writer = await asyncio.open_connection("127.0.0.1", first)
        assert (await _raw_health(reader, writer)).startswith(b"HTTP/1.1 200")
        await asyncio.wait_for(reader.readuntil(b"}"), 15)

        status, reading = await backend.exchange("PUT", "/api/settings/local-port", json={"port": second})
        assert status == 200 and reading["port"] == second and reading["listening"] is True
        assert (await _get(second, "/api/health"))[0] == 200                  # the new port serves at once
        assert await _refused(first)                                          # the old one accepts no one new
        assert _saved_port(backend) == second                                 # and the setting followed
        assert (await _raw_health(reader, writer)).startswith(b"HTTP/1.1 200")  # the open connection finishes
        writer.close()

        # A port another program holds changes nothing.
        holder, taken = _occupy()
        try:
            status, body = await backend.exchange("PUT", "/api/settings/local-port", json={"port": taken})
            assert status == 409 and body["code"] == "local_port.in_use" and body["params"] == {"port": str(taken)}
            assert (await _get(second, "/api/health"))[0] == 200 and _saved_port(backend) == second
        finally:
            holder.close()

        # Invalid ports are refused, and nothing changes.
        status, body = await backend.exchange("PUT", "/api/settings/local-port", json={"port": 80})
        assert status == 422 and body["code"] == "local_port.invalid"
        assert (await backend.exchange("PUT", "/api/settings/local-port", json={"port": "8095"}))[0] == 422
        assert _saved_port(backend) == second

        # The broad Settings write never changes it.
        status, settings = await backend.exchange("GET", "/api/settings")
        assert status == 200
        assert (await backend.exchange("PUT", "/api/settings", json={**settings, "desktop_local_port": first}))[0] == 200
        assert _saved_port(backend) == second
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_a_port_taken_at_launch_leaves_the_application_running_and_says_so(tmp_path):
    holder, taken = _occupy()
    backend = await _port_backend(tmp_path, taken).start()
    try:
        assert await backend.ready()
        status, reading = await backend.exchange("GET", "/api/settings/local-port")
        assert status == 200 and reading["port"] == taken and reading["listening"] is False
        assert (await backend.channel.ask("status"))["status"]["local_port"] == {"port": taken, "listening": False}
        holder.close()                                                        # freed: saving it binds it
        status, reading = await backend.exchange("PUT", "/api/settings/local-port", json={"port": taken})
        assert status == 200 and reading["listening"] is True
        assert (await _get(taken, "/api/health"))[0] == 200
    finally:
        holder.close()
        await backend.stop()


@pytest.mark.asyncio
async def test_a_tokens_use_is_recorded_and_the_clients_of_the_past_week_are_listed(tmp_path):
    from auth.accounts import DEFAULT_INTEGRATION_ID
    port = free_port()
    backend = await _port_backend(tmp_path, port).start()
    try:
        assert await backend.ready()
        status, made = await backend.exchange("POST", f"/api/accounts/{DEFAULT_INTEGRATION_ID}/tokens",
                                              json={"label": "Sonarr"})
        assert status == 200
        status, _ = await _get(port, "/api/stats", headers={"Authorization": f"Bearer {made['token']}"})
        assert status == 200
        status, reading = await backend.exchange("GET", "/api/settings/local-port")
        assert [client["label"] for client in reading["recent_clients"]] == ["Sonarr"]
        seen = reading["recent_clients"][0]["last_seen"]
        await _get(port, "/api/stats", headers={"Authorization": f"Bearer {made['token']}"})
        status, again = await backend.exchange("GET", "/api/settings/local-port")
        assert again["recent_clients"][0]["last_seen"] == seen                # at most once per five minutes
        # A revoked token is no client in use.
        assert (await backend.exchange("DELETE", f"/api/accounts/{DEFAULT_INTEGRATION_ID}/tokens/{made['id']}"))[0] == 200
        assert (await backend.exchange("GET", "/api/settings/local-port"))[1]["recent_clients"] == []
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_a_server_installation_has_no_local_port(tmp_path):
    backend = await Backend(tmp_path, desktop=False, channel=False, locked=False).start()
    try:
        assert await backend.ready()
        for method, body in (("GET", None), ("PUT", {"port": 9000})):
            status, answer = await backend.exchange(method, "/api/settings/local-port", json=body)
            assert status == 404 and answer["code"] == "local_port.desktop_only"
    finally:
        await backend.stop()


def test_the_supervisor_says_once_that_local_port_is_taken_and_nothing_while_it_listens(tmp_path):
    from desktop.supervisor import Words, local_port_notice
    words = Words(tmp_path / "missing-config.json")                     # English
    assert local_port_notice({"local_port": {"port": 8095, "listening": True}}, words) is None
    assert local_port_notice({"active": 0}, words) is None               # a backend that reports none
    title, body = local_port_notice({"local_port": {"port": 8095, "listening": False}}, words)
    assert title == "Local Port unavailable"
    assert "Port 8095 is in use by another program" in body and "8,095" not in body


def test_local_port_is_explained_only_in_the_desktop_application_and_only_where_the_card_is_shown():
    from pathlib import Path
    static = Path(__file__).resolve().parents[2] / "frontend" / "static"
    auth = (static / "auth.js").read_text(encoding="utf-8")
    assert "desktop: window.downloadPulseDesktop === true," in auth                 # the one Help context owner
    topic = (static / "ui-settings-help.js").read_text(encoding="utf-8")
    assert "{heading: 'help.topic.heading.local_port', when: onDesktop, list: [" in topic
    manual = (static / "ui-help-page.js").read_text(encoding="utf-8")
    shown = manual[manual.index("${c.shows('deployment-type') ? `${c.desktop ? '' : `<p ${keyed('help.settings.access_integrations.deployment_type')}></p>`}"):]
    assert shown.index("${c.desktop ? `<p ${keyed('help.settings.access_integrations.local_port'") < shown.index("` : ''}")
    page = (static / "ui-settings-page.js").read_text(encoding="utf-8")
    assert "const localPort = window.downloadPulseDesktop !== true ? '' : `" in page   # the card's item: the desktop's alone


def test_the_card_fits_one_row_then_the_triangle_then_stacked_and_a_server_card_is_unchanged():
    """The one fitter: a row of all items, then all but the last (the rest
    beneath: with three, the inverted triangle), then every item alone. With
    two items (a server) that is exactly the side-by-side or beneath it was."""
    from pathlib import Path
    page = (Path(__file__).resolve().parents[2] / "frontend" / "static" / "ui-settings-page.js").read_text(encoding="utf-8")
    fit = page[page.index("  function fitDeploymentType() {"):page.index("  /* Local Port (the desktop application's;")]
    assert "for (let count = items.length; count >= 2 && !row; count--) {" in fit
    assert "set(pair, 'data-language-layout', row === items.length ? 'beside' : row ? 'triangle' : 'below');" in fit
    assert "if (port) items.push({kind: 'port'" in page
    assert "if (assembly) items.push({kind: 'deployment'" in page                  # the desktop card has none


def test_the_desktop_card_has_no_deployment_type_and_its_switching_is_not_explained_there():
    from pathlib import Path
    static = Path(__file__).resolve().parents[2] / "frontend" / "static"
    page = (static / "ui-settings-page.js").read_text(encoding="utf-8")
    card = page[page.index("  function deploymentCard() {"):page.index("  /* Deployment & Language: its items")]
    assert "const deployment = window.downloadPulseDesktop === true ? '' : `" in card
    assert "headerCenter: window.downloadPulseDesktop === true ? null : say('settings.deployment.copy')," in card
    topic = (static / "ui-settings-help.js").read_text(encoding="utf-8")
    for heading in ("server", "standalone", "what_switching_keeps", "what_switching_changes"):
        assert f"{{heading: 'help.topic.heading.{heading}', when: offDesktop, list: [" in topic
    assert "{when: offDesktop, text: [\n          'help.topic.deployment_type.intro.deployment_type_decides'," in topic
    manual = (static / "ui-help-page.js").read_text(encoding="utf-8")
    assert "${c.desktop ? '' : `<p ${keyed('help.settings.access_integrations.deployment_type')}></p>`}" in manual


@pytest.mark.asyncio
async def test_the_desktop_application_refuses_a_deployment_type_change_and_changes_nothing(tmp_path):
    """The transition would only clear the desktop's sign-in (it is always
    Standalone): it is refused before anything is touched -- the stored
    configuration is byte for byte what it was."""
    backend = await Backend(tmp_path, locked=False).start()
    try:
        assert await backend.ready()
        stored = (backend.root / "config" / "config.json").read_bytes()
        for target in ("server", "standalone"):
            status, body = await backend.exchange("PUT", "/api/auth/deployment-type",
                                                  json={"deployment_type": target, "confirm": True})
            assert status == 409 and body["code"] == "deployment.desktop_standalone", body
        assert (backend.root / "config" / "config.json").read_bytes() == stored
    finally:
        await backend.stop()
