"""WS-S1: browser pairing, browser tokens and their fence.

Every live proof drives a real backend in its own process over real HTTP,
with real sign-ins (``/login``), real accounts and real tokens -- nothing in
the server is replaced. The fence itself is also proven over the ONE policy
table (``auth.permissions``), as a pure function of a principal.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
import pytest

from auth.accounts import DEFAULT_INTEGRATION_ID
from auth.models import AuthMechanism, Principal
from auth.permissions import ACTIONS, Capability, permits

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
PASSWORD = "Pairing-proof-0"
FIREFOX = "moz-extension://5b1f0c9e-2d55-4d5e-9b51-6a2f1d0c7e11"
CHROMIUM = "chrome-extension://gefngolnibkiicmimloecpgdmmokmgme"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Server:
    """One real backend on a disposable root: password sign-in, server deployment."""

    def __init__(self, root: Path):
        self.root, self.port = root, _free_port()
        (root / "config").mkdir(parents=True)
        (root / "data").mkdir()
        (root / "downloads").mkdir()
        (root / "config" / "config.json").write_text(json.dumps({
            "download_folder": str(root / "downloads"), "deployment_type": "server",
            "auth_password_enabled": True, "auth_username": "owner", "auth_password": PASSWORD}))
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG", "TMPDIR"}}
        env.update(DB_PATH=str(root / "data" / "downloadpulse.db"), CONFIG_PATH=str(root / "config" / "config.json"),
                   STATIC_DIR=str(ROOT / "frontend" / "static"))
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--app-dir", str(BACKEND), "--host", "127.0.0.1",
             "--port", str(self.port), "--workers", "1", "--no-access-log"],
            cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=open(root / "server.log", "wb"))
        self.base = f"http://127.0.0.1:{self.port}"

    def wait_ready(self, timeout: float = 90) -> None:
        import urllib.request

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            assert self.process.poll() is None, (self.root / "server.log").read_text(errors="replace")[-3000:]
            try:
                with urllib.request.urlopen(self.base + "/api/health", timeout=2) as reply:
                    if reply.status == 200:
                        return
            except OSError:
                time.sleep(0.25)
        raise AssertionError("backend never became healthy")

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(30)
        except subprocess.TimeoutExpired:
            self.process.kill()


class Person:
    """A signed-in browser session: real login form, real cookies, real CSRF token."""

    def __init__(self, server: Server, http: aiohttp.ClientSession, csrf: str):
        self.server, self.http, self.csrf = server, http, csrf

    @classmethod
    async def sign_in(cls, server: Server, username: str, password: str = PASSWORD) -> "Person":
        http = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        async with http.get(server.base + "/login") as page:
            form = await page.text()
        csrf_form = re.search(r'name="csrf_token" value="([^"]+)"', form).group(1)
        async with http.post(server.base + "/login", allow_redirects=False,
                             data={"username": username, "password": password, "csrf_token": csrf_form, "next": "/"},
                             headers={"Origin": server.base}) as reply:
            assert reply.status in {302, 303}, (reply.status, await reply.text())
        async with http.get(server.base + "/api/auth/session") as reply:
            session = await reply.json()
        return cls(server, http, session["csrf_token"])

    async def call(self, method: str, path: str, *, csrf: bool = True, **kwargs):
        headers = {"Origin": self.server.base, **kwargs.pop("headers", {})}
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        async with self.http.request(method, self.server.base + path, headers=headers, **kwargs) as reply:
            return reply.status, await reply.json(content_type=None)

    async def close(self) -> None:
        await self.http.close()


async def anonymous(server: Server, method: str, path: str, **kwargs):
    async with aiohttp.ClientSession() as http:
        async with http.request(method, server.base + path, **kwargs) as reply:
            try:
                body = await reply.json(content_type=None)
            except ValueError:
                body = await reply.text()
            return reply.status, body


async def extension(server: Server, method: str, path: str, token: str | None = None, origin: str = FIREFOX,
                    **kwargs):
    headers = {"Origin": origin, **kwargs.pop("headers", {})}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return await anonymous(server, method, path, headers=headers, **kwargs)


async def pair(server: Server, person: Person, label: str = "Laptop-Firefox") -> tuple[str, str]:
    """The whole flow: start, approve as ``person``, collect. Returns (token, token id)."""
    status, started = await extension(server, "POST", "/api/browser/v1/pairings", json={"client_name": "Firefox on Linux"})
    assert status == 200, started
    status, approved = await person.call("POST", f"/api/account/browser-pairings/{started['user_code']}/approve",
                                         json={"label": label})
    assert status == 200, approved
    status, collected = await extension(server, "POST", "/api/browser/v1/pairings/poll",
                                        json={"device_code": started["device_code"]})
    assert status == 200 and collected["status"] == "approved", collected
    return collected["token"], collected["token_id"]


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """One backend for the module, with an Admin (owner), a User and a ReadOnly person."""
    server = Server(tmp_path_factory.mktemp("browser-pairing"))
    try:
        server.wait_ready()

        async def people():
            owner = await Person.sign_in(server, "owner")
            for username, role in (("una", "user"), ("rhea", "readonly")):
                status, body = await owner.call("POST", "/api/accounts", json={
                    "kind": "local", "role": role, "username": username, "password": PASSWORD,
                    "display_name": username.title()})
                assert status == 200, body
            await owner.close()

        asyncio.run(people())
        yield server
    finally:
        server.stop()


# ── the fence, over the one policy table ────────────────────────────────────

@pytest.mark.parametrize("role", ["admin", "user"])
def test_a_browser_token_holds_the_browser_client_and_nothing_else_whatever_its_role(role):
    browser = Principal(authenticated=True, mechanism=AuthMechanism.API_TOKEN, account_id="a", role=role,
                        credential_id="t", client_id="t", credential_kind="browser")
    admitted = {action for action, capability in ACTIONS.items()
                if capability is not Capability.PUBLIC and permits(browser, capability)}
    assert admitted == {action for action, capability in ACTIONS.items() if capability is Capability.BROWSER_CLIENT}
    assert admitted == {"api.browser_routes.browser_session", "api.browser_routes.disconnect_this_browser",
                        "api.browser_routes.show_application",
                        "api.browser_routes.submit_browser_job",
                        "api.browser_routes.list_browser_jobs", "api.browser_routes.get_browser_job",
                        "api.browser_routes.withdraw_browser_job", "api.browser_routes.request_browser_control"}


@pytest.mark.parametrize("role", ["admin", "user", "readonly", "integration"])
def test_no_role_reaches_the_browser_client_api(role):
    principal = Principal(authenticated=True, account_id="a", role=role)
    assert not permits(principal, Capability.BROWSER_CLIENT)
    assert permits(principal, Capability.BROWSERS_OWN) is (role in {"admin", "user"})


# ── live: pairing ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_browser_pairs_on_approval_and_collects_its_token_exactly_once(world):
    owner = await Person.sign_in(world, "owner")
    try:
        status, started = await extension(world, "POST", "/api/browser/v1/pairings",
                                          json={"client_name": "Firefox on Linux"})
        assert status == 200 and started["protocol"] == "downloadpulse.browser/1"
        assert re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", started["user_code"])
        assert started["verification_path"] == f"/?connect-browser={started['user_code']}"
        poll = {"device_code": started["device_code"]}
        assert (await extension(world, "POST", "/api/browser/v1/pairings/poll", json=poll))[1]["status"] == "pending"

        status, shown = await owner.call("GET", f"/api/account/browser-pairings/{started['user_code'].lower()}",
                                         csrf=False)
        assert status == 200 and shown["client_name"] == "Firefox on Linux"
        assert started["device_code"] not in json.dumps(shown)
        status, approved = await owner.call("POST", f"/api/account/browser-pairings/{started['user_code']}/approve",
                                            json={"label": "Laptop-Firefox"})
        assert status == 200 and approved["browser"]["label"] == "Laptop-Firefox" and "token" not in approved

        await asyncio.sleep(2.1)   # the poll interval (RFC 8628 ``interval``)
        status, collected = await extension(world, "POST", "/api/browser/v1/pairings/poll", json=poll)
        assert collected["status"] == "approved" and collected["token"].startswith("dp_")
        assert (await extension(world, "POST", "/api/browser/v1/pairings/poll", json=poll))[1] == {"status": "expired"}

        for origin in (FIREFOX, CHROMIUM):
            status, session = await extension(world, "GET", "/api/browser/v1/session", collected["token"], origin)
            assert status == 200 and session == {
                "protocol": "downloadpulse.browser/1", "token": {"id": collected["token_id"], "label": "Laptop-Firefox"},
                "account": {"display_name": "Administrator"},
                "capabilities": {"sources": ["links", "magnet", "metalink", "nzb", "nzb_link", "torrent"],
                                 "controls": ["pause", "resume"], "withdraw": True, "estimate": True,
                                 "cookies": {"max_count": 300, "max_bytes": 32768, "sources": ["links"]}},
                "appearance": {"mode": "dark", "dark_palette": "downloadpulse-dark", "light_palette": "downloadpulse-light"}}
        # The person's appearance is its owner's answer: a change there is the extension's next read.
        status, changed = await owner.call("PATCH", "/api/account/appearance",
                                           json={"mode": "light", "light_palette": "nord-light"})
        assert status == 200, changed
        _, session = await extension(world, "GET", "/api/browser/v1/session", collected["token"])
        assert session["appearance"] == {"mode": "light", "dark_palette": "downloadpulse-dark", "light_palette": "nord-light"}
        assert (await owner.call("PATCH", "/api/account/appearance", json={"mode": "dark"}))[0] == 200

        status, events = await owner.call("GET", "/api/events?limit=50", csrf=False)
        assert status == 200, events
        text = json.dumps(events)
        assert "administration.browser_connected" in text
        assert collected["token"] not in text and started["device_code"] not in text
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_a_denied_or_unknown_pairing_issues_nothing(world):
    owner = await Person.sign_in(world, "owner")
    try:
        _, started = await extension(world, "POST", "/api/browser/v1/pairings", json={"client_name": "Chrome"})
        assert (await owner.call("POST", f"/api/account/browser-pairings/{started['user_code']}/deny"))[0] == 200
        status, body = await owner.call("POST", f"/api/account/browser-pairings/{started['user_code']}/approve",
                                        json={"label": "late"})
        assert status == 404 and body["code"] == "browser.pairing.not_found"
        poll = {"device_code": started["device_code"]}
        assert (await extension(world, "POST", "/api/browser/v1/pairings/poll", json=poll))[1] == {"status": "denied"}
        assert (await extension(world, "POST", "/api/browser/v1/pairings/poll",
                                json={"device_code": "never-issued"}))[1] == {"status": "expired"}
        status, body = await owner.call("GET", "/api/account/browser-pairings/AAAA-AAAA", csrf=False)
        assert status == 404 and body["code"] == "browser.pairing.not_found"
        status, body = await extension(world, "POST", "/api/browser/v1/pairings", json={"client_name": "  "})
        assert status == 422 and body["code"] == "browser.pairing.name_required"
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_approving_needs_a_person_who_may_connect_and_their_csrf_token(world):
    owner, rhea = await Person.sign_in(world, "owner"), await Person.sign_in(world, "rhea")
    try:
        token, _ = await pair(world, owner)
        _, started = await extension(world, "POST", "/api/browser/v1/pairings", json={"client_name": "Edge"})
        approve = f"/api/account/browser-pairings/{started['user_code']}/approve"
        assert (await owner.call("POST", approve, csrf=False, json={"label": "x"}))[0] == 403    # no CSRF token
        assert (await rhea.call("POST", approve, json={"label": "x"}))[0] == 403                # ReadOnly
        assert (await extension(world, "POST", approve, token, json={"label": "x"}))[0] == 403   # a browser token
        assert (await anonymous(world, "POST", approve, json={"label": "x"}))[0] == 401
        status, integration = await owner.call("POST", f"/api/accounts/{DEFAULT_INTEGRATION_ID}/tokens",
                                               json={"label": "sonarr"})
        assert status == 200
        assert (await anonymous(world, "POST", approve, json={"label": "x"},
                                headers={"Authorization": f"Bearer {integration['token']}"}))[0] == 403
        assert (await owner.call("GET", f"/api/account/browser-pairings/{started['user_code']}", csrf=False))[0] == 200
    finally:
        await owner.close()
        await rhea.close()


# ── live: the browser token's fence ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_browser_token_on_an_admin_account_reaches_nothing_but_the_browser_api(world):
    owner = await Person.sign_in(world, "owner")
    try:
        token, _ = await pair(world, owner, "Admin's browser")
        for method, path in (("GET", "/api/settings"), ("GET", "/api/accounts"), ("GET", "/api/torrents"),
                             ("GET", "/api/auth/session"), ("GET", "/api/stats"), ("GET", "/api/metrics"),
                             ("GET", "/api/account/browsers"), ("GET", "/api/external/v1/capabilities"),
                             ("POST", "/api/torrents/add-magnet"), ("POST", "/api/processing/pause"),
                             ("GET", "/")):
            status, _ = await extension(world, method, path, token, json={} if method == "POST" else None)
            assert status == 403, (method, path, status)
        # The download-client dialects refuse it exactly as an unknown key.
        status, body = await anonymous(world, "GET", f"/api?mode=queue&output=json&apikey={token}")
        assert body in ({"status": False, "error": "API Key Incorrect"}, {"detail": "Not Found"}), body
        # A session, and an Integration token, never reach the browser API.
        assert (await owner.call("GET", "/api/browser/v1/session", csrf=False))[0] == 403
        _, integration = await owner.call("POST", f"/api/accounts/{DEFAULT_INTEGRATION_ID}/tokens",
                                          json={"label": "radarr"})
        assert (await extension(world, "GET", "/api/browser/v1/session", integration["token"]))[0] == 403
        assert (await owner.call("POST", "/api/browser/v1/application/show"))[0] == 403
        # A server has no window to bring forward: the extension is told to open the page.
        assert await extension(world, "POST", "/api/browser/v1/application/show", token) == (200, {"desktop": False})
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_only_an_extension_origin_is_admitted_and_only_under_the_browser_prefix(world):
    owner = await Person.sign_in(world, "owner")
    try:
        status, body = await extension(world, "POST", "/api/browser/v1/pairings", origin="https://evil.example",
                                       json={"client_name": "x"})
        assert status == 403 and "Forbidden" in str(body)
        # An extension origin outside the browser prefix is refused, even with a valid session.
        async with owner.http.post(world.base + "/api/torrents/add-magnet", json={"magnet": "x"},
                                   headers={"Origin": FIREFOX, "X-CSRF-Token": owner.csrf}) as reply:
            assert reply.status == 403 and "Forbidden origin" in await reply.text()
        assert (await extension(world, "POST", "/api/browser/v1/pairings", origin=CHROMIUM,
                                json={"client_name": "Chrome"}))[0] == 200
    finally:
        await owner.close()


# ── live: who manages which browsers ────────────────────────────────────────

@pytest.mark.asyncio
async def test_people_manage_only_their_own_browsers_and_an_admin_may_disconnect_anyones(world):
    owner, una = await Person.sign_in(world, "owner"), await Person.sign_in(world, "una")
    try:
        una_token, una_id = await pair(world, una, "Una's laptop")
        owner_token, owner_id = await pair(world, owner, "Owner's desktop")
        _, mine = await una.call("GET", "/api/account/browsers", csrf=False)
        assert [item["label"] for item in mine["browsers"]] == ["Una's laptop"]
        assert set(mine["browsers"][0]) == {"id", "label", "created_at", "last_seen_at"}
        # Another person's browser, and an Integration token, are simply not found.
        status, body = await una.call("DELETE", f"/api/account/browsers/{owner_id}")
        assert status == 404 and body["code"] == "account.browser_token_not_found"
        _, integration = await owner.call("POST", f"/api/accounts/{DEFAULT_INTEGRATION_ID}/tokens",
                                          json={"label": "lidarr"})
        assert (await owner.call("DELETE", f"/api/account/browsers/{integration['id']}"))[0] == 404
        assert (await extension(world, "GET", "/api/browser/v1/session", owner_token))[0] == 200

        # An Admin sees every connected browser, with its person, in one list; a User does not.
        status, overview = await owner.call("GET", "/api/browser-tokens", csrf=False)
        assert status == 200
        listed = {(item["label"], item["account_name"]) for item in overview["browsers"]}
        assert {("Una's laptop", "Una"), ("Owner's desktop", "Administrator")} <= listed
        assert all(set(item) == {"id", "label", "account_id", "account_name", "created_at", "last_seen_at"}
                   for item in overview["browsers"])
        assert (await una.call("GET", "/api/browser-tokens", csrf=False))[0] == 403
        # An Admin sees Una's browser among her tokens and disconnects it.
        _, accounts_list = await owner.call("GET", "/api/accounts", csrf=False)
        una_account = next(item for item in accounts_list["accounts"] if item.get("username") == "una")
        _, tokens = await owner.call("GET", f"/api/accounts/{una_account['id']}/tokens", csrf=False)
        assert [(item["id"], item["kind"]) for item in tokens["tokens"]] == [(una_id, "browser")]
        assert (await owner.call("DELETE", f"/api/accounts/{una_account['id']}/tokens/{una_id}"))[0] == 200
        assert (await extension(world, "GET", "/api/browser/v1/session", una_token))[0] == 401

        # Una disconnects her next browser herself.
        una_token, una_id = await pair(world, una, "Una's phone")
        assert (await una.call("DELETE", f"/api/account/browsers/{una_id}"))[0] == 200
        assert (await extension(world, "GET", "/api/browser/v1/session", una_token))[0] == 401
        _, events = await owner.call("GET", "/api/events?limit=100", csrf=False)
        text = json.dumps(events)
        assert "disconnected by an Admin" in text and "disconnected by its owner" in text
    finally:
        await owner.close()
        await una.close()


def test_a_browser_disconnects_itself_and_its_token_stops_at_once(tmp_path):
    """The extension's Disconnect: the token revokes itself; another browser's is untouched.
    Its own backend: the module's shared one has spent its per-peer pairing budget."""
    server = Server(tmp_path)
    try:
        server.wait_ready()

        async def scenario():
            owner = await Person.sign_in(server, "owner")
            try:
                mine, mine_id = await pair(server, owner, "Leaving browser")
                other, _ = await pair(server, owner, "Staying browser")
                status, body = await extension(server, "DELETE", "/api/browser/v1/session", mine)
                assert status == 200 and body == {"ok": True}
                assert (await extension(server, "GET", "/api/browser/v1/session", mine))[0] == 401
                assert (await extension(server, "GET", "/api/browser/v1/session", other))[0] == 200
                _, events = await owner.call("GET", "/api/events?limit=50", csrf=False)
                assert any(item["type"] == "administration.browser_disconnected" and mine_id in (item["detail"] or "")
                           and "by its owner" in item["message"] for item in events["items"])
                # A session never reaches it.
                assert (await owner.call("DELETE", "/api/browser/v1/session"))[0] == 403
            finally:
                await owner.close()

        asyncio.run(scenario())
    finally:
        server.stop()


def test_a_person_moved_to_readonly_stops_every_browser_of_theirs_at_once(tmp_path):
    """Its own backend: the module's shared one has spent its per-peer pairing budget."""
    server = Server(tmp_path)
    try:
        server.wait_ready()

        async def demote():
            owner = await Person.sign_in(server, "owner")
            try:
                status, created = await owner.call("POST", "/api/accounts", json={
                    "kind": "local", "role": "user", "username": "vera", "password": PASSWORD,
                    "display_name": "Vera"})
                assert status == 200, created
                vera = await Person.sign_in(server, "vera")
                token, _ = await pair(server, vera, "Vera's browser")
                await vera.close()
                assert (await extension(server, "GET", "/api/browser/v1/session", token))[0] == 200
                _, listed = await owner.call("GET", "/api/accounts", csrf=False)
                vera_id = next(item["id"] for item in listed["accounts"] if item.get("username") == "vera")
                status, body = await owner.call("PATCH", f"/api/accounts/{vera_id}", json={"role": "readonly"})
                assert status == 200, body
                assert (await extension(server, "GET", "/api/browser/v1/session", token))[0] == 401
                # Disabling a person revokes their browsers (re-enabling restores sign-in, never a browser).
                status, created = await owner.call("POST", "/api/accounts", json={
                    "kind": "local", "role": "user", "username": "dora", "password": PASSWORD, "display_name": "Dora"})
                assert status == 200, created
                dora = await Person.sign_in(server, "dora")
                dora_token, dora_id = await pair(server, dora, "Dora's browser")
                await dora.close()
                _, listed = await owner.call("GET", "/api/accounts", csrf=False)
                dora_account = next(item["id"] for item in listed["accounts"] if item.get("username") == "dora")
                status, body = await owner.call("PATCH", f"/api/accounts/{dora_account}", json={"enabled": False})
                assert status == 200, body
                _, overview = await owner.call("GET", "/api/browser-tokens", csrf=False)
                assert dora_id not in {item["id"] for item in overview["browsers"]}
                assert (await owner.call("PATCH", f"/api/accounts/{dora_account}", json={"enabled": True}))[0] == 200
                assert (await extension(server, "GET", "/api/browser/v1/session", dora_token))[0] == 401
            finally:
                await owner.close()

        asyncio.run(demote())
    finally:
        server.stop()


def test_starting_pairings_is_rate_limited_per_peer(tmp_path):
    server = Server(tmp_path)
    try:
        server.wait_ready()

        async def flood():
            answers = [(await extension(server, "POST", "/api/browser/v1/pairings",
                                        json={"client_name": f"browser {n}"}))[0] for n in range(11)]
            return answers

        answers = asyncio.run(flood())
        assert answers[:10] == [200] * 10 and answers[10] == 429
    finally:
        server.stop()
