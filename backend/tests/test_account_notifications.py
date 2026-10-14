"""1.0.13: a standard User's own notifications, beside the unchanged global ones.

Narrow proofs, each at its owner:

* the destination rule (``services.notifications.is_discord_webhook_url``) and
  the account-owned client: Discord webhook endpoints only, no redirects, the
  account's own identity -- while the global client is unchanged;
* the self-scoped API (``api.account_routes``): the principal's own document
  only, write-only masked destinations, blank-keeps / explicit-clear, no
  unknown keys, no global fallback, every other role refused, Test bounded per
  account;
* delivery through the one dispatcher (``application.observability``): one
  global send per event, and one personal send per eligible owner of the
  outstanding demand -- shared and consolidated transfers, withdrawal,
  disabled accounts, unattributed demand, failure isolation, no replay;
* owner-scoped statistics and the one personal report pass;
* lifecycle and durability: Remove, Merge, OIDC reprovision, role change,
  migration of a pre-existing database, restart, wipe and backup/restore.

No test contacts Discord: every outbound request is recorded by a fake
transport at ``services.notifications.aiohttp.ClientSession``.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

import core.config as config
import db.database as database
import services.notifications as transport
from application.observability import Observability
from auth import accounts
from auth.accounts import DEFAULT_ADMIN_ID, DEFAULT_INTEGRATION_ID, Role
from auth.models import Principal
from auth.sessions import HTTP_SESSION_COOKIE, session_store
from auth.throttle import personal_notification_test_rate_limiter
from services import account_notifications
from services.account_notifications import AccountNotificationUpdate
from services.notifications import NotificationService, is_discord_webhook_url
from account_support import admin_with_password
from test_access_control import MAGNET, _submit, core  # noqa: F401  (fixture)
from test_backup_restore_points import installation  # noqa: F401  (fixture)
from test_backup_restore_points import served as backup_served  # noqa: F401  (fixture)
from test_details_requested_by import _consolidate, _magnet

pytestmark = pytest.mark.asyncio

GLOBAL_HOOK = "https://discord.com/api/webhooks/100/global-token"
ALICE_HOOK = "https://discord.com/api/webhooks/200/alice-token"
ALICE_ADDED = "https://discord.com/api/webhooks/201/alice-added"
ALICE_REPORT = "https://discord.com/api/webhooks/202/alice-report"
BOB_HOOK = "https://discordapp.com/api/webhooks/300/bob-token"
DAVE_HOOK = "https://canary.discord.com/api/webhooks/400/dave-token"
CAROL_HOOK = "https://ptb.discord.com/api/webhooks/500/carol-token"


# ── a recording transport ────────────────────────────────────────────────────

class _Response:
    def __init__(self, status, wire=None):
        self.status = status
        self.wire = wire

    async def __aenter__(self):
        if self.wire is not None:
            self.wire.in_flight += 1
            self.wire.peak = max(self.wire.peak, self.wire.in_flight)
            if self.wire.delay:
                await asyncio.sleep(self.wire.delay)
        return self

    async def __aexit__(self, *exc):
        if self.wire is not None:
            self.wire.in_flight -= 1
        return False

    async def json(self, content_type=None):
        return {"retry_after": 0}

    async def text(self):
        return "refused"


class Wire:
    """Every webhook POST, and per-URL answers (default 204)."""

    def __init__(self):
        self.posts: list[dict] = []
        self.status: dict[str, int] = {}
        # Concurrency observed at the transport: posts in flight, and the most
        # ever in flight at once (``delay`` holds each one open).
        self.delay = 0.0
        self.in_flight = 0
        self.peak = 0

    def session(self, *args, **kwargs):
        wire = self

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def post(self, url, json=None, **options):
                wire.posts.append({"url": url, "json": json, "options": options})
                return _Response(wire.status.get(url, 204), wire)

        return Session()

    def to(self, url):
        return [post for post in self.posts if post["url"] == url]

    def titles(self, url):
        return [post["json"]["embeds"][0]["title"] for post in self.to(url)]


@pytest.fixture
def wire(monkeypatch):
    recorded = Wire()
    monkeypatch.setattr(transport.aiohttp, "ClientSession", recorded.session)
    monkeypatch.setattr(transport, "_RATE_LIMIT_SECONDS", 0.0)
    monkeypatch.setattr(NotificationService, "_last_sent_at", {})
    monkeypatch.setattr(NotificationService, "_sent_hashes", {})
    monkeypatch.setattr(NotificationService, "_throttle_lock", None)
    personal_notification_test_rate_limiter.clear()
    yield recorded
    personal_notification_test_rate_limiter.clear()


def _settings(**values):
    return config.AppSettings(auth_password_enabled=True, **values)


# ── 1. the destination rule and the account-owned client ────────────────────

@pytest.mark.parametrize("url", [
    "https://discord.com/api/webhooks/123/abc_DEF-9",
    "https://discordapp.com/api/webhooks/123/abc",
    "https://canary.discord.com/api/webhooks/123/abc",
    "https://ptb.discordapp.com/api/v10/webhooks/123/abc",
])
async def test_exact_discord_webhook_endpoints_are_admitted(url):
    assert is_discord_webhook_url(url)


@pytest.mark.parametrize("url", [
    "https://example.com/api/webhooks/123/abc",                 # well-formed HTTPS, not Discord
    "http://discord.com/api/webhooks/123/abc",                  # not HTTPS
    "https://discord.com:8443/api/webhooks/123/abc",            # alternate port
    "https://discord.com:443/api/webhooks/123/abc",             # any explicit port
    "https://user:pw@discord.com/api/webhooks/123/abc",         # userinfo
    "https://discord.com@evil.example/api/webhooks/123/abc",    # authority confusion
    "https://discord.com.evil.example/api/webhooks/123/abc",    # look-alike host
    "https://evil-discord.com/api/webhooks/123/abc",
    "https://discord.com/api/webhooks/123",                     # no token
    "https://discord.com/api/webhooks/abc/def",                 # non-numeric id
    "https://discord.com/api/webhooks/123/abc/slack",           # another endpoint
    "https://discord.com/api/webhooks/123/abc?wait=true",       # query
    "https://discord.com/api/webhooks/123/abc#x",               # fragment
    "https://discord.com/redirect?to=/api/webhooks/123/abc",
    " https://discord.com/api/webhooks/123/abc",
    "https://discord.com/api/webhooks/123/a\nb",
    "https://127.0.0.1/api/webhooks/123/abc",
    "",
])
async def test_anything_else_is_refused(url):
    assert not is_discord_webhook_url(url)


async def test_the_account_client_posts_only_to_discord_and_never_follows_a_redirect(wire):
    refused = NotificationService("https://example.com/api/webhooks/1/x", discord_only=True, dedup=False)
    assert await refused.send("t", "d") is False
    with pytest.raises(RuntimeError, match="not a Discord webhook"):
        await refused.test(strict=True)
    assert wire.posts == []

    wire.status[ALICE_HOOK] = 302                       # Discord answering with a redirect
    own = NotificationService(ALICE_HOOK, identity=("Alice Bot", "https://img.example/a.png"),
                              discord_only=True, dedup=False)
    with pytest.raises(RuntimeError):
        await own.test(strict=True)
    post = wire.posts[-1]
    assert post["options"] == {"allow_redirects": False}
    assert post["json"]["username"] == "Alice Bot" and post["json"]["avatar_url"] == "https://img.example/a.png"


async def test_the_global_client_is_unchanged(wire, monkeypatch):
    monkeypatch.setattr(config, "_settings", _settings(discord_username="Global Bot"))
    assert await NotificationService("https://hooks.example/generic").send("t", "d")   # Admin may use any URL
    assert await NotificationService(GLOBAL_HOOK).send("t", "d")
    assert all(post["options"] == {} for post in wire.posts)                         # redirects as before
    assert wire.to(GLOBAL_HOOK)[0]["json"]["username"] == "Global Bot"


# ── 2. the self-scoped API through the real boundary ────────────────────────

@pytest_asyncio.fixture
async def live(tmp_path, monkeypatch, wire):
    import main

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "notifications.db")
    await database.init_db()
    monkeypatch.setattr(config, "_settings", _settings(discord_webhook_url=GLOBAL_HOOK))

    @asynccontextmanager
    async def operation():
        yield

    monkeypatch.setattr(main.app.state, "application", SimpleNamespace(application_operation=operation),
                        raising=False)
    session_store.clear()
    await admin_with_password()                       # a usable sign-in, so role alone decides
    people = {
        "alice": await accounts.create_local_account("alice", "alice-secret", role="user"),
        "bob": await accounts.create_local_account("bob", "bob-secret", role="user"),
        "reader": await accounts.create_local_account("reader", "reader-secret", role="readonly"),
        "admin": await accounts.get_account(DEFAULT_ADMIN_ID),
    }
    sessions = {name: session_store.create(Principal.password_session(person), lifetime_seconds=3600)[0]
                for name, person in people.items()}
    _record, raw = await accounts.create_token(DEFAULT_INTEGRATION_ID, "probe")
    transport_ = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport_, base_url="http://127.0.0.1") as client:
        def as_(name):
            if name == "integration":
                return {"Authorization": f"Bearer {raw}"}
            token = sessions[name]
            return {"Cookie": f"{HTTP_SESSION_COOKIE}={token}", "X-CSRF-Token": session_store.csrf_token(token)}
        yield SimpleNamespace(client=client, as_=as_, people=people, wire=wire)
    session_store.clear()


OWN = "/api/account/notifications"


async def test_each_user_reads_and_writes_only_their_own_masked_configuration(live):
    client, as_ = live.client, live.as_
    fresh = (await client.get(OWN, headers=as_("alice"))).json()
    # New accounts take part in nothing, and have nothing of the global's.
    assert fresh["discord_notifications_enabled"] is False and fresh["stats_reporting_enabled"] is False
    assert fresh["discord_webhook_url"] == "" and fresh["discord_webhook_url_configured"] is False
    assert fresh["discord_notifications_configured"] is False and fresh["stats_reporting_configured"] is False
    assert "discord_notify_update" not in fresh and "update_check_interval_hours" not in fresh
    assert "notification_verification" not in fresh

    saved = await client.put(OWN, headers=as_("alice"), json={
        "discord_notifications_enabled": True, "discord_webhook_url": ALICE_HOOK,
        "discord_username": "Alice Bot", "stats_report_window_hours": 168})
    assert saved.status_code == 200, saved.text
    assert ALICE_HOOK not in saved.text and "alice-token" not in saved.text
    body = saved.json()
    assert body["discord_webhook_url"] == "" and body["discord_webhook_url_configured"] is True
    # Same-owner fallback: reporting is configured through Alice's OWN primary.
    assert body["stats_reporting_configured"] is True and body["stats_report_webhook_url_configured"] is False

    await client.put(OWN, headers=as_("bob"), json={"discord_webhook_url": BOB_HOOK})
    alice = (await client.get(OWN, headers=as_("alice"))).json()
    assert alice["discord_username"] == "Alice Bot" and alice["stats_report_window_hours"] == 168
    bob = (await client.get(OWN, headers=as_("bob"))).json()
    assert bob["discord_username"] == "" and bob["discord_notifications_enabled"] is False
    # The global configuration is untouched by either.
    assert config.get_settings().discord_webhook_url == GLOBAL_HOOK
    assert (await account_notifications.load(live.people["alice"].id)).discord_webhook_url == ALICE_HOOK
    assert (await account_notifications.load(live.people["bob"].id)).discord_webhook_url == BOB_HOOK

    # Blank keeps; an explicit clear erases exactly the one named.
    await client.put(OWN, headers=as_("alice"), json={"discord_webhook_url": "", "discord_webhook_added": ALICE_ADDED})
    stored = await account_notifications.load(live.people["alice"].id)
    assert stored.discord_webhook_url == ALICE_HOOK and stored.discord_webhook_added == ALICE_ADDED
    cleared = (await client.put(OWN, headers=as_("alice"), json={"clear_secrets": ["discord_webhook_url"]})).json()
    assert cleared["discord_webhook_url_configured"] is False and cleared["discord_webhook_added_configured"] is True
    # No global fallback: the global webhook does not make Alice configured.
    assert cleared["discord_notifications_configured"] is False and cleared["stats_reporting_configured"] is False


@pytest.mark.parametrize("body", [
    {"account_id": "someone-else"},
    {"discord_notify_update": True},
    {"update_check_interval_hours": 1},
    {"role": "admin"},
    {"discord_webhook_url": "https://example.com/api/webhooks/1/x"},   # non-Discord HTTPS
    {"discord_webhook_added": "http://discord.com/api/webhooks/1/x"},
    {"stats_report_webhook_url": "https://discord.com.evil.example/api/webhooks/1/x"},
    {"discord_avatar_url": "http://img.example/a.png"},
    {"discord_avatar_url": "https://10.0.0.1:8080/a.png"},
    {"clear_secrets": ["discord_username"]},
    {"stats_report_interval_hours": 169},
])
async def test_a_forged_or_unsupported_change_is_refused_and_changes_nothing(live, body):
    response = await live.client.put(OWN, headers=live.as_("alice"), json=body)
    assert response.status_code == 422, response.text
    assert (await account_notifications.load(live.people["alice"].id)) == account_notifications.AccountNotificationSettings()


async def test_only_a_signed_in_user_reaches_personal_notifications(live):
    client, as_ = live.client, live.as_
    for path, method in ((OWN, "GET"), (OWN, "PUT"), (f"{OWN}/test-discord", "POST"), (f"{OWN}/send-report", "POST")):
        for name in ("reader", "admin", "integration"):
            response = await client.request(method, path, headers=as_(name), json={})
            assert response.status_code == 403, (path, name, response.text)
        assert (await client.request(method, path, json={})).status_code == 401
    # ...and a User reaches none of the global notification surfaces.
    for method, path in (("GET", "/api/settings"), ("PUT", "/api/settings"),
                         ("POST", "/api/settings/validate-discord"), ("POST", "/api/settings/send-stats-report"),
                         ("POST", "/api/settings/upload-avatar")):
        assert (await client.request(method, path, headers=as_("alice"), json={})).status_code == 403, path
    # A disabled User's session ends: nothing personal is reachable.
    await accounts.set_enabled(DEFAULT_ADMIN_ID, live.people["bob"].id, False)
    assert (await client.get(OWN, headers=as_("bob"))).status_code == 401
    assert live.wire.posts == []


async def test_a_user_test_reaches_only_their_own_endpoint_and_is_bounded_per_account(live):
    client, as_, wire = live.client, live.as_, live.wire
    assert (await client.post(f"{OWN}/test-discord", headers=as_("alice"))).status_code == 400   # nothing saved
    await client.put(OWN, headers=as_("alice"), json={"discord_webhook_url": ALICE_HOOK, "discord_username": "Alice"})
    await client.put(OWN, headers=as_("bob"), json={"discord_webhook_url": BOB_HOOK})

    tested = await client.post(f"{OWN}/test-discord", headers=as_("alice"))
    assert tested.status_code == 200 and tested.json()["notifications"]["discord_notifications_verified"] is True
    assert [post["url"] for post in wire.posts] == [ALICE_HOOK]
    assert wire.posts[0]["json"]["username"] == "Alice" and "Transfer" not in wire.titles(ALICE_HOOK)[0]
    assert ALICE_HOOK not in tested.text
    # A body naming another target is not an input: the destination is the saved one.
    await client.post(f"{OWN}/test-discord", headers=as_("alice"), json={"webhook_url": BOB_HOOK})
    await client.post(f"{OWN}/send-report", headers=as_("alice"))
    assert wire.to(BOB_HOOK) == [] and len(wire.to(ALICE_HOOK)) == 3
    limited = await client.post(f"{OWN}/test-discord", headers=as_("alice"))
    assert limited.status_code == 429 and len(wire.to(ALICE_HOOK)) == 3
    # Bounded per account: Bob's own allowance is untouched.
    assert (await client.post(f"{OWN}/test-discord", headers=as_("bob"))).status_code == 200

    # A redirect answer is a failure, and it retires the proof.
    personal_notification_test_rate_limiter.clear()
    wire.status[ALICE_HOOK] = 307
    failed = await client.post(f"{OWN}/test-discord", headers=as_("alice"))
    assert failed.status_code == 502 and ALICE_HOOK not in failed.text
    assert (await client.get(OWN, headers=as_("alice"))).json()["discord_notifications_verified"] is False


# ── 3. delivery through the one dispatcher ──────────────────────────────────

async def _person(name, hook=None, **choices):
    person = await accounts.create_local_account(name, f"{name}-secret", role="user")
    if hook:
        await account_notifications.update(person.id, AccountNotificationUpdate(
            discord_notifications_enabled=True, discord_webhook_url=hook, discord_username=name.title(), **choices))
    return person


async def _ask(repository, account_id, char, name):
    """One submission of its own, under its own name (the global client's
    existing content dedup keys on the name)."""
    from transfers.models import SubmissionDemand, TransferRequest

    payload = _magnet(char)
    demand = SubmissionDemand(account_id) if account_id else None
    transfer, _created = await repository.admit(
        (TransferRequest("magnet", payload, name=name, fingerprint=payload[-52:-12]),), name=name, demand=demand)
    return transfer.id, demand.id if demand else None


async def _event(transfer_id, kind, detail=None):
    async with database.get_db() as db:
        await db.execute("INSERT INTO application_events(transfer_id,kind,detail) VALUES(?,?,?)",
                         (transfer_id, kind, detail))
        await db.commit()


async def test_shared_and_consolidated_work_reaches_each_owner_once_and_global_once(core, wire, monkeypatch):
    repository, _engine, _application = core
    monkeypatch.setattr(config, "_settings", _settings(discord_webhook_url=GLOBAL_HOOK))
    observability = Observability(repository)
    alice = await _person("alice", ALICE_HOOK)
    bob = await _person("bob", BOB_HOOK)
    carol = await _person("carol", CAROL_HOOK)
    dave = await _person("dave", DAVE_HOOK)
    quiet = await _person("quiet")                                       # a User who configured nothing

    shared, _a = await _ask(repository, alice.id, "b", "shared.bin")
    same, _b = await _ask(repository, bob.id, "b", "shared.bin")         # joins Alice's transfer
    assert same == shared
    await _ask(repository, quiet.id, "b", "shared.bin")
    own, _d = await _ask(repository, dave.id, "d", "dave.bin")
    unattributed, _none = await _ask(repository, None, "e", "legacy.bin")  # a legacy/unknown owner
    contributor, _c = await _ask(repository, carol.id, "c", "carol.bin")
    await _consolidate(contributor, shared)
    await observability.deliver()

    # Added: the global path exactly as before, once per new transfer; each
    # User for their OWN request, including one that joined existing work.
    assert len(wire.titles(GLOBAL_HOOK)) == 4
    assert wire.titles(ALICE_HOOK) == ["📥 Transfer Added"] and wire.titles(BOB_HOOK) == ["📥 Transfer Added"]
    assert wire.titles(DAVE_HOOK) == ["📥 Transfer Added"] and wire.titles(CAROL_HOOK) == ["📥 Transfer Added"]
    added_to_bob = wire.to(BOB_HOOK)[0]["json"]
    assert added_to_bob["username"] == "Bob" and alice.id not in str(added_to_bob)

    wire.posts.clear()
    await _event(shared, "completed")
    await observability.deliver()
    await observability.deliver()                                        # replay: nothing new
    assert wire.titles(GLOBAL_HOOK) == ["✅ Download Complete"]
    assert wire.titles(ALICE_HOOK) == ["✅ Download Complete"] and wire.titles(BOB_HOOK) == ["✅ Download Complete"]
    assert wire.titles(CAROL_HOOK) == ["✅ Download Complete"]             # consolidated demand
    assert wire.to(DAVE_HOOK) == []                                      # not his work
    demand_ids = [row["id"] for row in await repository.demand_history(shared)]
    for post in wire.posts:                                              # no account or request identity
        for leaked in (alice.id, bob.id, carol.id, quiet.id, *demand_ids):
            assert leaked not in str(post["json"])
    assert {post["json"]["username"] for post in wire.to(BOB_HOOK)} == {"Bob"}

    # Withdrawal suppresses that owner only.
    wire.posts.clear()
    bob_demand = next(row["id"] for row in await repository.outstanding_demand(shared) if row["account_id"] == bob.id)
    await repository.withdraw_demand([bob_demand], by=bob.id)
    await _event(shared, "error", "Provider refused the link")
    await observability.deliver()
    assert wire.titles(GLOBAL_HOOK) == ["❌ Error"] and wire.titles(ALICE_HOOK) == ["❌ Error"]
    assert wire.to(BOB_HOOK) == []

    # An unattributed transfer reaches the global only.
    wire.posts.clear()
    await _event(unattributed, "completed")
    await _event(own, "completed")
    await observability.deliver()
    assert len(wire.titles(GLOBAL_HOOK)) == 2 and wire.titles(DAVE_HOOK) == ["✅ Download Complete"]
    assert {post["url"] for post in wire.posts} == {GLOBAL_HOOK, DAVE_HOOK}


async def test_disabled_removed_or_reroled_users_and_toggles_receive_nothing_while_global_continues(core, wire, monkeypatch):
    repository, _engine, _application = core
    monkeypatch.setattr(config, "_settings", _settings(discord_webhook_url=GLOBAL_HOOK))
    observability = Observability(repository)
    alice = await _person("alice", ALICE_HOOK)
    bob = await _person("bob", BOB_HOOK, discord_notify_finished=False)
    carol = await _person("carol", CAROL_HOOK)
    dave = await _person("dave", DAVE_HOOK)
    shared, _ = await _submit(repository, alice.id)
    for person in (bob, carol, dave):
        await _submit(repository, person.id)
    await observability.deliver()
    wire.posts.clear()

    await accounts.set_enabled(DEFAULT_ADMIN_ID, alice.id, False)        # also withdraws her demand
    await accounts.delete_account(DEFAULT_ADMIN_ID, carol.id)
    await accounts.set_role(DEFAULT_ADMIN_ID, dave.id, Role.READONLY)
    await _event(shared, "completed")
    await observability.deliver()
    assert wire.titles(GLOBAL_HOOK) == ["✅ Download Complete"]
    assert {post["url"] for post in wire.posts} == {GLOBAL_HOOK}          # Bob chose no completion notices
    assert await account_notifications.load(carol.id) == account_notifications.AccountNotificationSettings()


async def test_one_failing_personal_webhook_blocks_neither_the_global_nor_another_user(core, wire, monkeypatch):
    repository, _engine, _application = core
    monkeypatch.setattr(config, "_settings", _settings(discord_webhook_url=GLOBAL_HOOK))
    alice = await _person("alice", ALICE_HOOK)
    bob = await _person("bob", BOB_HOOK)
    shared, _ = await _submit(repository, alice.id)
    await _submit(repository, bob.id)
    wire.status[ALICE_HOOK] = 500
    await Observability(repository).deliver()
    assert len(wire.to(ALICE_HOOK)) == 1                                  # attempted, failed
    assert wire.titles(BOB_HOOK) == ["📥 Transfer Added"] and wire.titles(GLOBAL_HOOK) == ["📥 Transfer Added"]


async def test_two_users_choosing_the_same_webhook_each_get_their_own_message(core, wire, monkeypatch):
    repository, _engine, _application = core
    monkeypatch.setattr(config, "_settings", _settings())
    alice = await _person("alice", ALICE_HOOK)
    bob = await _person("bob", ALICE_HOOK)
    await _submit(repository, alice.id)
    await _submit(repository, bob.id)
    await Observability(repository).deliver()
    assert sorted(post["json"]["username"] for post in wire.to(ALICE_HOOK)) == ["Alice", "Bob"]


async def test_every_eligible_owner_of_a_widely_shared_transfer_is_delivered_with_bounded_concurrency(
        core, wire, monkeypatch):
    """More owners than any fixed cap: every eligible configured User gets
    exactly one notice, the excluded ones none, the global one send, and no
    more than ``MAX_CONCURRENT_SENDS`` posts are ever in flight together."""
    repository, _engine, _application = core
    monkeypatch.setattr(config, "_settings", _settings(discord_webhook_url=GLOBAL_HOOK))
    hook = lambda n: f"https://discord.com/api/webhooks/{7000 + n}/owner-{n}"  # noqa: E731

    async def owner(n, *, configured=True):
        # An OIDC first sign-in: an enabled User, without hashing a password.
        person = await accounts.resolve_oidc("https://idp.example", f"owner-{n}", {"name": f"Owner {n}"},
                                             default_role="user")
        if configured:
            await account_notifications.update(person.id, AccountNotificationUpdate(
                discord_notifications_enabled=True, discord_webhook_url=hook(n)))
        return person

    eligible = [await owner(n) for n in range(70)]                       # beyond any 64-owner boundary
    unconfigured = [await owner(n, configured=False) for n in range(100, 103)]
    disabled, removed, withdrawn, demoted = [await owner(n) for n in range(200, 204)]
    integration = await accounts.create_integration_account("Automation probe")
    await account_notifications.update(integration.id, AccountNotificationUpdate(
        discord_notifications_enabled=True, discord_webhook_url=hook(300)))
    await account_notifications.update(DEFAULT_ADMIN_ID, AccountNotificationUpdate(
        discord_notifications_enabled=True, discord_webhook_url=hook(301)))

    owners = [*eligible, *unconfigured, disabled, removed, withdrawn, demoted, integration]
    shared = None
    for person in owners:
        transfer_id, demand_id = await _ask(repository, person.id, "b", "shared.bin")
        shared = shared or transfer_id
        assert transfer_id == shared
        if person is withdrawn:
            await repository.withdraw_demand([demand_id], by=person.id)
    await _ask(repository, DEFAULT_ADMIN_ID, "b", "shared.bin")
    await accounts.set_enabled(DEFAULT_ADMIN_ID, disabled.id, False)
    await accounts.delete_account(DEFAULT_ADMIN_ID, removed.id)
    await accounts.set_role(DEFAULT_ADMIN_ID, demoted.id, Role.READONLY)
    observability = Observability(repository)
    await observability.deliver()                                        # admission notices, set aside
    assert not await repository.pending_events()
    assert len({row["account_id"] for row in await repository.outstanding_demand(shared)}) > 64

    wire.posts.clear()
    wire.delay, wire.peak = 0.02, 0
    await _event(shared, "completed")
    await observability.deliver()

    for n, _person in enumerate(eligible):
        assert wire.titles(hook(n)) == ["✅ Download Complete"], n
    excluded = [*range(200, 204), 300, 301]
    assert all(wire.to(hook(n)) == [] for n in excluded)
    assert wire.titles(GLOBAL_HOOK) == ["✅ Download Complete"]
    assert len(wire.posts) == len(eligible) + 1                          # nobody else, nothing twice
    assert 1 < wire.peak <= account_notifications.MAX_CONCURRENT_SENDS


# ── 4. owner-scoped statistics and the personal report pass ─────────────────

async def _complete(transfer_id, size=1000):
    async with database.get_db() as db:
        await db.execute("UPDATE torrents SET status='completed', size_bytes=?, completed_at=CURRENT_TIMESTAMP "
                         "WHERE id=?", (size, transfer_id))
        await db.commit()


async def test_a_personal_report_counts_only_that_owners_requests(core, wire, monkeypatch):
    from services.stats import generate_report, send_stats_report

    repository, _engine, _application = core
    monkeypatch.setattr(config, "_settings", _settings())
    alice = await _person("alice", ALICE_HOOK)
    bob = await _person("bob", BOB_HOOK)
    shared, _ = await _submit(repository, alice.id)
    await _submit(repository, alice.id)                                  # a repeat: the same one transfer
    await _submit(repository, bob.id)
    alone, alone_demand = await _submit(repository, alice.id, payload=_magnet("f"))
    others = [(await _submit(repository, bob.id, payload=_magnet(char)))[0] for char in "ghij"]
    await _complete(shared, 1000)
    await _complete(others[0], 5000)
    await repository.withdraw_demand([alone_demand], by=alice.id)        # history keeps its attribution

    mine = (await generate_report(24, owner_ids=await accounts.owned_account_ids(alice.id)))["report"]["summary"]
    assert (mine["torrents_processed"], mine["completed"], mine["total_downloaded"]) == (2, 1, "1000.0 B")
    theirs = (await generate_report(24, owner_ids=[bob.id]))["report"]["summary"]
    assert (theirs["torrents_processed"], theirs["completed"]) == (5, 2)
    everyone = (await generate_report(24))["report"]["summary"]
    assert everyone["torrents_processed"] == 6                           # the global report is instance-wide
    nobody = (await generate_report(24, owner_ids=[]))["report"]["summary"]
    assert nobody["torrents_processed"] == 0
    assert await generate_report(24, owner_ids=[alice.id]) and alone

    settings = await account_notifications.load(alice.id)
    await send_stats_report(24, triggered_by="manual", owner_ids=[alice.id],
                            sender=account_notifications.sender(settings, ALICE_HOOK))
    sent = wire.to(ALICE_HOOK)[-1]["json"]["embeds"][0]
    assert sent["title"].startswith("📊 Your Statistics Report")
    assert {field["name"]: field["value"] for field in sent["fields"]}["Torrents"] == "2"


async def test_the_personal_report_pass_keeps_each_users_own_cadence(core, wire, monkeypatch):
    repository, _engine, _application = core
    monkeypatch.setattr(config, "_settings", _settings(stats_reporting_enabled=True, stats_report_interval_hours=1,
                                                       discord_webhook_url=GLOBAL_HOOK))
    alice = await _person("alice", ALICE_HOOK)
    await account_notifications.update(alice.id, AccountNotificationUpdate(
        stats_reporting_enabled=True, stats_report_interval_hours=2, stats_report_webhook_url=ALICE_REPORT))
    bob = await _person("bob", BOB_HOOK)
    await account_notifications.update(bob.id, AccountNotificationUpdate(stats_reporting_enabled=True,
                                                                         stats_report_interval_hours=0))
    now = time.time()
    assert await account_notifications.due_reports(now) == []            # Alice's cadence starts now
    assert await account_notifications.due_reports(now + 3600) == []
    due = await account_notifications.due_reports(now + 7200)
    assert [account_id for account_id, _ in due] == [alice.id]
    await account_notifications.mark_reported(alice.id, now + 7200)
    assert await account_notifications.due_reports(now + 7300) == []
    await accounts.set_enabled(DEFAULT_ADMIN_ID, alice.id, False)
    assert await account_notifications.due_reports(now + 20000) == []    # disabled: no scheduled report


# ── 5. lifecycle and durability ─────────────────────────────────────────────

async def test_merge_and_oidc_reprovision_never_carry_a_destination_across_identities(core, monkeypatch):
    survivor = await _person("survivor", ALICE_HOOK)
    absorbed = await accounts.resolve_oidc("https://idp.example", "absorbed", {"name": "Absorbed"},
                                           default_role="user")
    await accounts.set_enabled(DEFAULT_ADMIN_ID, absorbed.id, True)
    await account_notifications.update(absorbed.id, AccountNotificationUpdate(
        discord_notifications_enabled=True, discord_webhook_url=BOB_HOOK))
    await accounts.merge_accounts(DEFAULT_ADMIN_ID, [absorbed.id], survivor.id)
    assert (await account_notifications.load(survivor.id)).discord_webhook_url == ALICE_HOOK
    assert await account_notifications.load(absorbed.id) == account_notifications.AccountNotificationSettings()

    erin = await accounts.resolve_oidc("https://idp.example", "erin", {"name": "Erin"}, default_role="user")
    await account_notifications.update(erin.id, AccountNotificationUpdate(
        discord_notifications_enabled=True, discord_webhook_url=DAVE_HOOK))
    await accounts.delete_account(DEFAULT_ADMIN_ID, erin.id)
    # Signing in again provisions a NEW, disabled account (no session results).
    assert await accounts.resolve_oidc("https://idp.example", "erin", {"name": "Erin"}, default_role="user") is None
    async with database.get_db() as db:
        bound = await db.fetchone("SELECT account_id FROM account_oidc_bindings WHERE subject='erin'")
    again = await accounts.get_account(bound["account_id"])
    assert again.id != erin.id and not again.enabled
    assert await account_notifications.load(again.id) == account_notifications.AccountNotificationSettings()
    assert not await account_notifications.eligible(again.id)


async def test_a_pre_existing_database_gains_the_table_and_configuration_survives_restart(tmp_path, monkeypatch):
    path = tmp_path / "upgrade.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()
    async with database.get_db() as db:                                  # a database from before this table
        await db.execute("DROP TABLE account_notifications")
        await db.commit()
    global_before = _settings(discord_webhook_url=GLOBAL_HOOK, stats_report_interval_hours=6)
    monkeypatch.setattr(config, "_settings", global_before)
    await database.init_db()                                             # startup migrates it
    person = await accounts.create_local_account("alice", "alice-secret", role="user")
    await account_notifications.update(person.id, AccountNotificationUpdate(discord_webhook_url=ALICE_HOOK))
    await database.init_db()                                             # a restart
    assert (await account_notifications.load(person.id)).discord_webhook_url == ALICE_HOOK
    assert config.get_settings() is global_before and global_before.discord_webhook_url == GLOBAL_HOOK


async def test_the_wipe_keeps_account_notification_configuration(tmp_path, monkeypatch):
    from services.db_maintenance import wipe_database

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "wipe.db")
    await database.init_db()
    person = await accounts.create_local_account("alice", "alice-secret", role="user")
    await account_notifications.update(person.id, AccountNotificationUpdate(discord_webhook_url=ALICE_HOOK))
    result = await wipe_database(verified_quiesced=True)
    assert "account_notifications" not in result["wiped_tables"]
    assert (await account_notifications.load(person.id)).discord_webhook_url == ALICE_HOOK


async def test_a_restore_point_carries_account_notification_configuration(backup_served):
    _app, client, _composed, _calls = backup_served
    alice = await accounts.create_local_account("alice", "alice-secret", role="user")
    await account_notifications.update(alice.id, AccountNotificationUpdate(discord_webhook_url=ALICE_HOOK))
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    await account_notifications.update(alice.id, AccountNotificationUpdate(discord_webhook_url=BOB_HOOK))

    restored = await client.post("/api/admin/backups/restore", json={"id": point})
    assert restored.status_code == 200, restored.text
    assert (await account_notifications.load(alice.id)).discord_webhook_url == ALICE_HOOK
