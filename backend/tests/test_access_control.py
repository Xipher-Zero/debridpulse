"""1.0.13 ACL Stage 2: server-enforced roles, submitter demand, streams.

Narrow proofs of the one policy (``auth.permissions``), sourced from the
live route table; of submitter demand in the transfer core (a repeat
submission's own demand, consolidation-aware protection, idempotent
withdrawal, last-demand settlement through the existing ``cancel``, account
disable withdrawing atomically and settling after an interruption); of
per-target bulk authorization; and of the live stream's sanitization and
revocation.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi.routing import APIRoute

import core.config as config
import db.database as database
from account_support import account
from auth import accounts
from auth.accounts import DEFAULT_ADMIN_ID, DEFAULT_INTEGRATION_ID, Role
from auth.models import Principal
from auth.permissions import ACTIONS, ROLE_CAPABILITIES, Capability, action_name, destruction_permitted, permits
from auth.sessions import HTTP_SESSION_COOKIE, session_store
from transfers.models import SubmissionDemand, TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

MAGNET = "magnet:?xt=urn:btih:" + "b" * 40 + "&dn=shared.bin"


def _routes():
    import main

    found = []

    def walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                found.append(route)
            elif getattr(route, "original_router", None) is not None:
                walk(route.original_router.routes)

    walk(main.app.routes)
    return found


# ── the policy, from the route table ────────────────────────────────────────

def test_every_registered_action_is_classified_and_nothing_else_is():
    registered = {action_name(route.endpoint) for route in _routes()}
    assert sorted(registered - set(ACTIONS)) == []      # unclassified: denied, so must not exist
    assert sorted(set(ACTIONS) - registered) == []      # no stale classification


def test_the_role_matrix_over_every_registered_action():
    mutating = {"POST", "PUT", "PATCH", "DELETE"}
    readonly, user, integration = (Principal(role=role.value) for role in (Role.READONLY, Role.USER, Role.INTEGRATION))
    for route in _routes():
        capability = ACTIONS[action_name(route.endpoint)]
        path = route.path_format
        # ReadOnly never mutates anything but its own session.
        if route.methods & mutating and permits(readonly, capability):
            assert capability in {Capability.PUBLIC, Capability.SESSION}, path
        # High-risk families are Admin only.
        if any(part in path for part in ("/admin/", "/backups", "/executor-work", "/aria2", "/accounts",
                                         "/auth/config", "/trace", "/integrations/", "/settings",
                                         "/transfer-policy", "/runtime-limits", "/usenet/servers")):
            assert capability in {Capability.ADMINISTER, Capability.PUBLIC, Capability.INTERFACE}, path
            assert not permits(user, capability) or capability is Capability.INTERFACE, path
    allowed = sorted(name for name, capability in ACTIONS.items()
                     if capability is not Capability.PUBLIC and permits(integration, capability))
    assert allowed == sorted([
        "api.account_routes.change_own_actual_name", "api.account_routes.change_own_display_name",
        "api.account_routes.change_own_password", "api.account_routes.own_account",
        "api.account_routes.record_own_email",
        "api.account_routes.list_requests", "api.account_routes.withdraw_request",
        "api.auth_routes.auth_session_status", "api.auth_routes.logout",
        "api.routes.add_debrid_links", "api.routes.add_link_file", "api.routes.add_magnet",
        "api.routes.add_multimeta_file", "api.routes.add_torrent_file", "api.routes.add_usenet_file",
        "api.routes.add_usenet_link", "api.routes.get_stats", "api.routes.prometheus_metrics",
        # The native external-client API (``external.client``): the token's
        # own client jobs only -- never a transfer read, operation or deletion.
        "api.external_routes.acknowledge_external_delivery", "api.external_routes.external_capabilities",
        "api.external_routes.get_external_delivery", "api.external_routes.get_external_job",
        "api.external_routes.list_external_jobs", "api.external_routes.request_external_control",
        "api.external_routes.submit_external_job", "api.external_routes.withdraw_external_job",
        # The *arr SABnzbd dialect: a translation over those same operations.
        "api.arr_sabnzbd.sabnzbd_api",
        # The *arr qBittorrent dialect (Bearer only): likewise.
        "api.arr_qbittorrent.qbittorrent_api",
    ])
    # Admin holds everything a User does except a User's OWN notification
    # configuration: the global Notifications already cover every Admin
    # request, so there is no second, personal Admin configuration.
    assert ROLE_CAPABILITIES[Role.ADMIN] >= ROLE_CAPABILITIES[Role.USER] - {Capability.NOTIFICATIONS_SELF}
    assert Capability.NOTIFICATIONS_SELF not in ROLE_CAPABILITIES[Role.ADMIN]
    assert ROLE_CAPABILITIES[Role.USER] >= ROLE_CAPABILITIES[Role.READONLY]


# ── live probes through the real boundary ───────────────────────────────────

@pytest_asyncio.fixture
async def served(tmp_path, monkeypatch):
    """The real application object and middleware, password mode, with a
    User, a ReadOnly user and an Integration token."""
    import main

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "acl.db")
    await database.init_db()
    monkeypatch.setattr(config, "_settings", config.AppSettings(auth_password_enabled=True))

    @asynccontextmanager
    async def operation():
        yield

    monkeypatch.setattr(main.app.state, "application", SimpleNamespace(application_operation=operation),
                        raising=False)
    session_store.clear()
    _record, raw = await accounts.create_token(DEFAULT_INTEGRATION_ID, "probe")
    sessions = {}
    for role in (Role.USER, Role.READONLY, Role.ADMIN):
        token, _ = session_store.create(
            Principal.password_session(account(role.value, account_id=f"{role.value}-id", role=role)),
            lifetime_seconds=3600)
        sessions[role] = token
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        def as_(role):
            if role is Role.INTEGRATION:
                return {"Authorization": f"Bearer {raw}"}
            token = sessions[role]
            return {"Cookie": f"{HTTP_SESSION_COOKIE}={token}", "X-CSRF-Token": session_store.csrf_token(token)}
        yield client, as_
    session_store.clear()


PROBES = [
    # method, path, {role: expected denial or None for "admitted past the policy"}
    ("GET", "/api/settings", {Role.USER: 403, Role.READONLY: 403, Role.INTEGRATION: 403}),
    ("POST", "/api/admin/backups/restore", {Role.USER: 403, Role.READONLY: 403, Role.INTEGRATION: 403}),
    ("GET", "/api/admin/backups/x/package", {Role.USER: 403, Role.READONLY: 403, Role.INTEGRATION: 403}),
    ("POST", "/api/admin/database/wipe", {Role.USER: 403, Role.READONLY: 403, Role.INTEGRATION: 403}),
    ("GET", "/api/events", {Role.USER: 403, Role.READONLY: 403, Role.INTEGRATION: 403}),
    ("GET", "/api/torrents/1/trace", {Role.USER: 403, Role.READONLY: 403, Role.INTEGRATION: 403}),
    ("POST", "/api/processing/pause", {Role.USER: 403, Role.READONLY: 403, Role.INTEGRATION: 403}),
    ("PATCH", "/api/execution/runtime-limits", {Role.USER: 403, Role.READONLY: 403, Role.INTEGRATION: 403}),
    ("POST", "/api/executor-work/a/cancel", {Role.USER: 403, Role.READONLY: 403, Role.INTEGRATION: 403}),
    ("GET", "/api/accounts", {Role.USER: 403, Role.READONLY: 403, Role.INTEGRATION: 403, Role.ADMIN: None}),
    ("GET", "/docs", {Role.USER: 403, Role.READONLY: 403, Role.INTEGRATION: 403}),
    ("GET", "/", {Role.INTEGRATION: 403}),
    ("GET", "/api/events/stream", {Role.INTEGRATION: 403}),
    ("GET", "/api/torrents", {Role.INTEGRATION: 403, Role.USER: None, Role.READONLY: None}),
    ("POST", "/api/torrents/add-magnet", {Role.READONLY: 403}),
    ("POST", "/api/torrents/move-to-top", {Role.READONLY: 403, Role.INTEGRATION: 403}),
    ("POST", "/api/torrents/bulk", {Role.READONLY: 403, Role.INTEGRATION: 403}),
    ("DELETE", "/api/torrents/1", {Role.READONLY: 403, Role.INTEGRATION: 403}),
    ("GET", "/api/metrics", {Role.USER: 403, Role.READONLY: 403, Role.INTEGRATION: None}),
    ("GET", "/api/requests", {Role.READONLY: 403, Role.INTEGRATION: None}),
]


async def test_direct_probes_are_decided_by_role(served):
    client, as_ = served
    for method, path, expectations in PROBES:
        for role, expected in expectations.items():
            response = await client.request(method, path, headers=as_(role), json={})
            if expected is None:
                assert response.status_code not in {401, 403}, (method, path, role, response.text)
            else:
                assert response.status_code == expected, (method, path, role, response.text)
    unauthenticated = await client.get("/api/torrents")
    assert unauthenticated.status_code == 401


async def test_the_whoami_projection_names_the_role_capabilities_and_generation(served):
    client, as_ = served
    data = (await client.get("/api/auth/session", headers=as_(Role.READONLY))).json()
    assert data["role"] == "readonly" and data["account_generation"] == 1
    assert data["capabilities"] == sorted(ROLE_CAPABILITIES[Role.READONLY])
    integration = (await client.get("/api/auth/session", headers=as_(Role.INTEGRATION))).json()
    assert integration["capabilities"] == sorted(ROLE_CAPABILITIES[Role.INTEGRATION])


# ── submitter demand in the transfer core ───────────────────────────────────

@pytest_asyncio.fixture
async def core(tmp_path, monkeypatch):
    from application.service import ApplicationService
    from transfers.engine import TransferEngine

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "demand.db")
    await database.init_db()
    repository = TransferRepository()
    engine = TransferEngine(repository, IntegrationRegistry(), download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(retry_delay=0), clock=lambda: 1000.0)
    await engine.initialize()
    return repository, engine, ApplicationService(engine)


async def _submit(repository, account_id, *, payload=MAGNET):
    demand = SubmissionDemand(account_id)
    transfer, _created = await repository.admit(
        (TransferRequest("magnet", payload, name="shared.bin", fingerprint=payload[-52:-12]),),
        name="shared.bin", demand=demand)
    return transfer.id, demand.id


async def _status(repository, transfer_id):
    return (await repository.get(transfer_id)).state


async def test_a_repeat_submission_records_its_own_demand_on_the_shared_transfer(core):
    repository, _engine, _application = core
    first, first_demand = await _submit(repository, "alice")
    again, second_demand = await _submit(repository, "bob")

    assert first == again
    owners = sorted((row["id"], row["account_id"]) for row in await repository.outstanding_demand(first))
    assert owners == sorted([(first_demand, "alice"), (second_demand, "bob")])


async def test_withdrawal_is_idempotent_and_only_the_last_demand_stops_the_work(core):
    repository, engine, application = core
    transfer_id, alice = await _submit(repository, "alice")
    _same, bob = await _submit(repository, "bob")

    assert (await application.withdraw_demand((alice,), by="account:alice"))["cancelled_transfers"] == []
    assert await _status(repository, transfer_id) is not TransferState.CANCELLED
    assert (await application.withdraw_demand((alice,), by="account:alice"))["cancelled_transfers"] == []

    result = await application.withdraw_demand((bob,), by="account:bob")
    assert result["cancelled_transfers"] == [transfer_id]
    assert await _status(repository, transfer_id) is TransferState.CANCELLED
    assert [row["withdrawn_at"] is not None for row in await repository.demands()] == [True, True]


async def test_consolidated_demand_protects_the_canonical_transfer(core):
    repository, engine, _application = core
    canonical, alice = await _submit(repository, "alice")
    contributor, bob = await _submit(repository, "bob", payload=MAGNET.replace("b" * 40, "c" * 40))
    async with database.get_db() as db:
        primary = await db.execute_returning_id(
            "INSERT INTO download_files(torrent_id,filename,status) VALUES(?,'shared.bin','pending')", (canonical,))
        standby = await db.execute_returning_id(
            "INSERT INTO download_files(torrent_id,filename,status) VALUES(?,'shared.bin','duplicate')", (contributor,))
        request = (await db.fetchone("SELECT id FROM transfer_requests WHERE transfer_id=?", (contributor,)))["id"]
        await db.execute(
            "INSERT INTO artifact_consolidations(contributing_artifact_id,source_transfer_id,source_request_id,"
            "canonical_artifact_id) VALUES(?,?,?,?)", (standby, contributor, request, primary))
        await db.commit()
    alice_principal = Principal(account_id="alice", role=Role.USER.value)

    assert await destruction_permitted(alice_principal, repository, canonical) is False
    assert await destruction_permitted(Principal(account_id="x", role=Role.ADMIN.value), repository, canonical)
    assert await engine.withdraw_demand((alice,), by="account:alice") == []
    assert await _status(repository, canonical) is not TransferState.CANCELLED   # bob's demand still served
    # The last demand anywhere on that work: both transfers settle through cancel.
    assert sorted(await engine.withdraw_demand((bob,), by="account:bob")) == sorted([canonical, contributor])
    assert await _status(repository, canonical) is TransferState.CANCELLED


async def test_legacy_demand_belongs_to_nobody_and_keeps_its_transfer(core):
    repository, engine, _application = core
    transfer, _created = await repository.admit(
        (TransferRequest("magnet", MAGNET, name="old.bin", fingerprint="b" * 40),), name="old.bin")
    rows = await repository.outstanding_demand(transfer.id)
    assert [(row["origin"], row["account_id"]) for row in rows] == [("unattributed", None)]
    assert await destruction_permitted(Principal(account_id="alice", role=Role.USER.value),
                                       repository, transfer.id) is False
    assert await engine.settle_withdrawn_demand() == []


async def test_disabling_an_account_withdraws_its_demand_atomically_and_the_next_cycle_settles_it(core, monkeypatch):
    repository, engine, _application = core
    monkeypatch.setattr(config, "_settings", config.AppSettings())
    user = await accounts.create_local_account("worker", "secret", role=Role.USER)
    transfer_id, _demand = await _submit(repository, user.id)
    token, _ = session_store.create(Principal.password_session(user), lifetime_seconds=3600)

    withdrawn = await accounts.set_enabled(DEFAULT_ADMIN_ID, user.id, False)

    # Access ended and demand withdrawn in one commit; nothing settled yet --
    # as if the process stopped here.
    assert withdrawn == [transfer_id] and session_store.resolve(token) is None
    assert await _status(repository, transfer_id) is not TransferState.CANCELLED
    # The next resolution cycle derives the pending settlement from durable demand.
    await engine.resolve_pending()
    assert await _status(repository, transfer_id) is TransferState.CANCELLED
    # Re-enabling restores access only.
    await accounts.set_enabled(DEFAULT_ADMIN_ID, user.id, True)
    assert await repository.outstanding_demand(transfer_id) == []


async def test_a_mixed_bulk_delete_applies_only_where_the_caller_owns_every_demand(core):
    from api import routes

    repository, _engine, application = core
    own, _ = await _submit(repository, "alice")
    theirs, _ = await _submit(repository, "bob", payload=MAGNET.replace("b" * 40, "d" * 40))
    request = SimpleNamespace(state=SimpleNamespace(principal=Principal(account_id="alice", role=Role.USER.value)))

    result = await routes.bulk_action(routes.BulkAction(action="delete", ids=[own, theirs]), request, application)

    assert result["results"] == [{"id": own, "outcome": "applied"}, {"id": theirs, "outcome": "denied"}]
    assert (result["ok"], result["denied"]) == (1, 1)
    assert await _status(repository, theirs) is not TransferState.DELETED


# ── the live stream ─────────────────────────────────────────────────────────

class _Connection:
    def __init__(self, principal, token=""):
        self.state = SimpleNamespace(principal=principal, auth_session_token=token)

    async def is_disconnected(self):
        return False


async def test_stream_payloads_are_public_projections():
    from api import routes

    queue: asyncio.Queue = asyncio.Queue()
    routes._sse_subscribers[queue] = routes._SseSubscriber(Principal.standalone())
    try:
        await routes._sse_broadcast("torrent_updated", {
            "id": 7, "status": "downloading", "request": {"payload": MAGNET},
            "files": [{"candidates": ["https://u:p@host/x"], "name": "x"}]})
        frame = queue.get_nowait()
    finally:
        routes._sse_subscribers.pop(queue, None)
    data = json.loads(frame.split("data: ", 1)[1])
    assert data["id"] == 7 and "request" not in data and "candidates" not in data["files"][0]
    assert MAGNET not in frame and "u:p@" not in frame


async def test_revoking_a_session_ends_its_open_stream(monkeypatch):
    from api import routes

    monkeypatch.setattr(config, "_settings", config.AppSettings(auth_password_enabled=True))
    session_store.clear()
    principal = Principal.password_session(account("viewer", account_id="viewer-id", role=Role.READONLY))
    token, _ = session_store.create(principal, lifetime_seconds=3600)
    stream = routes._sse_generator(_Connection(principal, token))
    assert (await stream.__anext__()).startswith("event: connected")
    reading = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0)

    session_store.revoke_account("viewer-id")

    assert (await asyncio.wait_for(reading, 2)).startswith("event: session_ended")
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()


# ── the Activity Log's journal signal reaches only roles that read it ───────

def _drain(queue: asyncio.Queue) -> list[str]:
    frames = []
    while not queue.empty():
        frame = queue.get_nowait()
        frames.append(getattr(frame, "text", frame))
    return frames


async def test_the_journal_signal_reaches_only_a_role_that_reads_the_activity_log(monkeypatch):
    from api import routes

    monkeypatch.setattr(config, "_settings", config.AppSettings(auth_password_enabled=True))
    session_store.clear()
    reader = ACTIONS["api.operational_downloads.list_activity_events"]
    subscribers = {"standalone": routes._SseSubscriber(Principal.standalone())}
    for role in (Role.ADMIN, Role.USER, Role.READONLY):
        principal = Principal.password_session(account(role.value, account_id=f"{role.value}-id", role=role))
        token, _ = session_store.create(principal, lifetime_seconds=3600)
        subscribers[role.value] = routes._SseSubscriber(principal, token, session_store.fingerprint(token))
    subscribers["integration"] = routes._SseSubscriber(Principal.integration_token(
        account("automation", account_id="integration-id", role=Role.INTEGRATION), "token-id"))
    # A session that has ended receives nothing gated, whatever role it held.
    ended = Principal.password_session(account("gone", account_id="gone-id", role=Role.ADMIN))
    gone, _ = session_store.create(ended, lifetime_seconds=3600)
    subscribers["ended-admin"] = routes._SseSubscriber(ended, gone, session_store.fingerprint(gone))
    session_store.revoke_account("gone-id")
    queues = {name: asyncio.Queue() for name in subscribers}
    for name, subscriber in subscribers.items():
        routes._sse_subscribers[queues[name]] = subscriber
    try:
        for queue in queues.values():
            _drain(queue)                                  # the revocation's own notice
        await routes._sse_broadcast("activity_journal_changed", {"latest": 41})
        await routes._sse_broadcast("torrent_updated", {"id": 7, "status": "queued"})
        received = {name: _drain(queue) for name, queue in queues.items()}
    finally:
        for queue in queues.values():
            routes._sse_subscribers.pop(queue, None)

    journal = {name for name, frames in received.items()
               if any(frame.startswith("event: activity_journal_changed") for frame in frames)}
    assert journal == {name for name, subscriber in subscribers.items()
                       if name != "ended-admin" and permits(subscriber.principal, reader)}
    assert journal == {"standalone", "admin"}              # ReadOnly, User and Integration: not even a hint
    frame = next(f for f in received["admin"] if f.startswith("event: activity_journal_changed"))
    assert json.loads(frame.split("data: ", 1)[1]) == {"latest": 41}
    # Every other event keeps the stream's own delivery, unchanged.
    assert {name for name, frames in received.items()
            if any(frame.startswith("event: torrent_updated") for frame in frames)} == set(subscribers)


async def test_the_statistics_signal_reaches_every_statistics_reader_and_carries_nothing(monkeypatch):
    from api import routes

    monkeypatch.setattr(config, "_settings", config.AppSettings(auth_password_enabled=True))
    session_store.clear()
    subscribers = {"standalone": routes._SseSubscriber(Principal.standalone())}
    for role in (Role.ADMIN, Role.USER, Role.READONLY):
        principal = Principal.password_session(account(role.value, account_id=f"{role.value}-id", role=role))
        token, _ = session_store.create(principal, lifetime_seconds=3600)
        subscribers[role.value] = routes._SseSubscriber(principal, token, session_store.fingerprint(token))
    subscribers["integration"] = routes._SseSubscriber(Principal.integration_token(
        account("automation", account_id="integration-id", role=Role.INTEGRATION), "token-id"))
    ended = Principal.password_session(account("gone", account_id="gone-id", role=Role.READONLY))
    gone, _ = session_store.create(ended, lifetime_seconds=3600)
    subscribers["ended-readonly"] = routes._SseSubscriber(ended, gone, session_store.fingerprint(gone))
    session_store.revoke_account("gone-id")
    queues = {name: asyncio.Queue() for name in subscribers}
    for name, subscriber in subscribers.items():
        routes._sse_subscribers[queues[name]] = subscriber
    try:
        for queue in queues.values():
            _drain(queue)
        await routes._sse_broadcast("statistics_changed", {})
        await routes._sse_broadcast("activity_journal_changed", {"latest": 9})
        received = {name: _drain(queue) for name, queue in queues.items()}
    finally:
        for queue in queues.values():
            routes._sse_subscribers.pop(queue, None)

    def got(event):
        return {name for name, frames in received.items() if any(f.startswith(f"event: {event}\n") for f in frames)}

    reader = ACTIONS["api.routes.get_stats_detail"]
    assert got("statistics_changed") == {name for name, subscriber in subscribers.items()
                                         if name != "ended-readonly" and permits(subscriber.principal, reader)}
    assert got("statistics_changed") == {"standalone", "admin", "user", "readonly"}
    frame = next(f for f in received["readonly"] if f.startswith("event: statistics_changed\n"))
    assert frame == "event: statistics_changed\ndata: {}\n\n"                 # no id, no count, no detail
    # The Activity Log's watermark still reaches only who reads the Activity Log.
    assert got("activity_journal_changed") == {"standalone", "admin"}


async def test_the_heartbeat_keeps_its_clock_while_the_stream_is_busy(monkeypatch):
    """The heartbeat is the browser's clock for figures that age with time
    alone, so a stream that never falls quiet still carries it."""
    from api import routes

    monkeypatch.setattr(config, "_settings", config.AppSettings())
    monkeypatch.setattr(routes, "_SSE_HEARTBEAT_SECONDS", 0.2, raising=False)
    stream = routes._sse_generator(_Connection(Principal.standalone()))
    assert (await stream.__anext__()).startswith("event: connected")
    queue = next(q for q, s in routes._sse_subscribers.items() if s.principal == Principal.standalone())

    async def busy():
        while True:
            queue.put_nowait("event: torrent_updated\ndata: {}\n\n")
            await asyncio.sleep(0.02)

    feeder = asyncio.ensure_future(busy())
    try:
        frames = []
        started = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - started < 1.0:
            frames.append(await asyncio.wait_for(stream.__anext__(), 2))
    finally:
        feeder.cancel()
        await stream.aclose()
    pings = [frame for frame in frames if frame.startswith("event: ping")]
    assert len(pings) >= 3                                # ~every 0.2 s, between the busy frames
    assert sum(frame.startswith("event: torrent_updated") for frame in frames) > len(pings)


async def test_a_queued_journal_signal_is_withheld_once_the_role_can_no_longer_read_it(monkeypatch):
    from api import routes

    monkeypatch.setattr(config, "_settings", config.AppSettings(auth_password_enabled=True))
    session_store.clear()
    admin = account("watcher", account_id="watcher-id", role=Role.ADMIN)
    principal = Principal.password_session(admin)
    token, _ = session_store.create(principal, lifetime_seconds=3600)
    stream = routes._sse_generator(_Connection(principal, token))
    assert (await stream.__anext__()).startswith("event: connected")
    try:
        await routes._sse_broadcast("activity_journal_changed", {"latest": 5})   # queued while Admin
        session_store.rebind_account(account("watcher", account_id="watcher-id", role=Role.READONLY,
                                             generation=2))
        await routes._sse_broadcast("activity_journal_changed", {"latest": 6})   # never queued
        # The demoted connection is told to reset; the signal queued before
        # the demotion is not sent.
        assert (await asyncio.wait_for(stream.__anext__(), 2)).startswith("event: account_changed")
    finally:
        await stream.aclose()


# ── destructive acts are decided against the demand they were authorized for ─

async def _authorized_then_another_account_submits(repository, transfer_id):
    """Authorize alice (sole owner) for a destructive act, then let bob's
    repeat submission land on the same transfer before the act commits."""
    from auth.permissions import destruction_precondition

    permitted, expected = await destruction_precondition(
        Principal(account_id="alice", role=Role.USER.value), repository, transfer_id)
    assert permitted is True
    await _submit(repository, "bob")
    return expected


async def test_demand_arriving_after_authorization_refuses_every_destructive_act(core):
    from transfers._repository_base import DemandChanged

    repository, engine, application = core
    transfer_id, _alice = await _submit(repository, "alice")
    async with database.get_db() as db:
        artifact = await db.execute_returning_id(
            "INSERT INTO download_files(torrent_id,filename,status,request_id) "
            "SELECT ?, 'shared.bin', 'pending', id FROM transfer_requests WHERE transfer_id=?",
            (transfer_id, transfer_id))
        await db.commit()
    expected = await _authorized_then_another_account_submits(repository, transfer_id)

    with pytest.raises(DemandChanged):
        await application.delete(transfer_id, expected_demand=expected)
    with pytest.raises(DemandChanged):
        await application.cancel(transfer_id, expected_demand=expected)
    with pytest.raises(DemandChanged):
        await application.select_artifact(transfer_id, artifact, selected=False, expected_demand=expected)
    with pytest.raises(DemandChanged):  # the manual candidate switch's decisive claim
        await repository.claim_recovery(artifact, "user_candidate_switch", 1000.0, expected_demand=expected)
    async with database.get_db() as db:  # the root-route switch's decisive commit
        root = (await db.fetchone("SELECT id FROM transfer_requests WHERE transfer_id=?", (transfer_id,)))["id"]
        await db.execute("INSERT INTO resolution_attempts(id,request_id,provider_id,state) VALUES('ra',?,'p','resolved')",
                         (root,))
        await db.execute("INSERT INTO route_attempt_provenance(resolution_attempt_id,transfer_id,request_id,ordinal,"
                         "operation) VALUES('ra',?,?,1,'routing')", (transfer_id, root))
        await db.commit()
    assert await repository.replace_root_route(root, expected_attempt_id="ra", expected_provider_id="p",
                                               target_provider_id="q", expected_demand=expected) == "demand_changed"

    # Nothing changed, and bob's demand is intact.
    current = await repository.get(transfer_id)
    assert current.state not in {TransferState.DELETED, TransferState.CANCELLED}
    async with database.get_db() as db:
        assert (await db.fetchone("SELECT blocked FROM download_files WHERE id=?", (artifact,)))["blocked"] == 0
        assert (await db.fetchone("SELECT state FROM resolution_attempts WHERE id='ra'"))["state"] == "resolved"
    assert "bob" in {row["account_id"] for row in await repository.outstanding_demand(transfer_id)}
    # The authorized demand still alone: the same act commits.
    await application.delete(transfer_id, expected_demand=expected | {
        row["id"] for row in await repository.outstanding_demand(transfer_id)})
    assert (await repository.get(transfer_id)).state is TransferState.DELETED


async def test_a_bulk_delete_target_whose_demand_changed_meanwhile_is_denied_untouched(core, monkeypatch):
    from api import routes
    from auth import permissions

    repository, _engine, application = core
    transfer_id, _alice = await _submit(repository, "alice")
    authorize = permissions.destruction_precondition

    async def authorize_then_bob_submits(principal, repo, tid):
        decision = await authorize(principal, repo, tid)
        await _submit(repository, "bob")          # lands between authorization and commit
        return decision

    monkeypatch.setattr(routes, "destruction_precondition", authorize_then_bob_submits)
    request = SimpleNamespace(state=SimpleNamespace(principal=Principal(account_id="alice", role=Role.USER.value)))

    result = await routes.bulk_action(routes.BulkAction(action="delete", ids=[transfer_id]), request, application)

    assert result["results"] == [{"id": transfer_id, "outcome": "denied"}]
    assert (await repository.get(transfer_id)).state is not TransferState.DELETED


# ── a role change re-binds live sessions; disable still ends them ───────────

async def test_a_role_change_takes_effect_at_once_and_resets_without_signing_out(served, monkeypatch):
    from api import routes

    client, _as = served
    holder = await accounts.create_local_account("holder", "secret-1", role=Role.ADMIN)  # keeps an Admin usable
    target = await accounts.create_local_account("target", "secret-2", role=Role.ADMIN)
    token, _ = session_store.create(Principal.password_session(target), lifetime_seconds=3600)
    headers = {"Cookie": f"{HTTP_SESSION_COOKIE}={token}", "X-CSRF-Token": session_store.csrf_token(token)}
    queue: asyncio.Queue = asyncio.Queue()
    routes._sse_subscribers[queue] = routes._SseSubscriber(Principal.password_session(target), token,
                                                           session_store.fingerprint(token))
    try:
        assert (await client.get("/api/accounts", headers=headers)).status_code not in {401, 403}

        await accounts.set_role(holder.id, target.id, Role.USER)       # demotion
        assert (await client.get("/api/accounts", headers=headers)).status_code == 403
        assert (await client.get("/api/torrents", headers=headers)).status_code not in {401, 403}
        session = (await client.get("/api/auth/session", headers=headers)).json()
        assert session["authenticated"] is True and session["role"] == "user"
        assert queue.get_nowait() == routes._SSE_ACCOUNT_CHANGED

        await accounts.set_role(holder.id, target.id, Role.ADMIN)      # promotion
        assert (await client.get("/api/accounts", headers=headers)).status_code not in {401, 403}
        assert (await client.get("/api/auth/session", headers=headers)).json()["role"] == "admin"
        assert queue.get_nowait() == routes._SSE_ACCOUNT_CHANGED

        await accounts.set_enabled(holder.id, target.id, False)         # disable still ends it
        assert queue.get_nowait() is routes._SSE_SESSION_ENDED
        assert session_store.resolve(token) is None
        assert (await client.get("/api/torrents", headers=headers)).status_code == 401
    finally:
        routes._sse_subscribers.pop(queue, None)


async def test_an_admin_moves_an_oidc_provisioned_user_through_every_human_role(served, monkeypatch):
    """An OIDC identity provisioned with the default role is an ordinary human
    account afterwards: an Admin may give it any human role, each change is
    enforced on its next request, and its session continues throughout. The
    account-type and protected-account boundaries still refuse."""
    from auth.oidc_version import oidc_configuration_version

    client, as_ = served
    oidc_ready = config.AppSettings(
        auth_password_enabled=True, auth_oidc_enabled=True, oidc_provider_name="OIDC",
        oidc_issuer_url="https://idp.example", oidc_client_id="client", oidc_client_secret="secret",
        oidc_scopes=["openid"], oidc_allow_all=True, public_base_url="https://pulse.example")
    monkeypatch.setattr(config, "_settings", oidc_ready)
    person = await accounts.resolve_oidc("https://idp.example", "person-subject", {"name": "Person"},
                                         default_role="user")
    assert person.role is Role.USER
    token, _ = session_store.create(
        Principal.oidc_session("person-subject", display_name="Person").bound_to(person), lifetime_seconds=3600,
        credential_version=oidc_configuration_version(oidc_ready))
    own = {"Cookie": f"{HTTP_SESSION_COOKIE}={token}", "X-CSRF-Token": session_store.csrf_token(token)}
    admin = as_(Role.ADMIN)
    submit = {"magnet": "magnet:?xt=urn:btih:" + "f" * 40}

    async def change(role):
        return (await client.patch(f"/api/accounts/{person.id}", headers=admin, json={"role": role})).status_code

    async def enforced(role, may_administer, may_submit):
        assert (await accounts.get_account(person.id)).role.value == role
        session = (await client.get("/api/auth/session", headers=own)).json()
        assert session["authenticated"] is True and session["role"] == role       # never signed out
        assert ((await client.get("/api/accounts", headers=own)).status_code != 403) is may_administer
        assert ((await client.post("/api/torrents/add-magnet", headers=own, json=submit)).status_code != 403) \
            is may_submit

    assert await change("readonly") == 200
    await enforced("readonly", False, False)
    assert await change("admin") == 200
    await enforced("admin", True, True)
    # It is now the only Admin that can sign in: the last-usable-Admin
    # protection refuses to demote it, and nothing changes.
    refused = await client.patch(f"/api/accounts/{person.id}", headers=admin, json={"role": "user"})
    assert (refused.status_code, refused.json()["detail"]) == (409, "At least one usable Admin account must remain")
    await enforced("admin", True, True)
    holder = await accounts.create_local_account("holder", "holder-secret", role=Role.ADMIN)
    assert await change("user") == 200
    await enforced("user", False, True)

    # Boundaries: a human never becomes an Integration, an Admin never changes
    # its own role, and the protected default Admin's role never changes.
    assert await change("integration") == 409
    holder_token, _ = session_store.create(Principal.password_session(holder), lifetime_seconds=3600)
    holder_headers = {"Cookie": f"{HTTP_SESSION_COOKIE}={holder_token}",
                      "X-CSRF-Token": session_store.csrf_token(holder_token)}
    assert (await client.patch(f"/api/accounts/{holder.id}", headers=holder_headers,
                               json={"role": "user"})).status_code == 409
    assert (await client.patch(f"/api/accounts/{DEFAULT_ADMIN_ID}", headers=holder_headers,
                               json={"role": "user"})).status_code == 409


async def test_an_integration_token_reads_the_aggregate_stats_and_nothing_wider(served, monkeypatch):
    """Homepage's customapi widget reads /api/stats with an Integration token.
    That response is aggregate counters only; the token gets it and nothing
    else statistics-, settings- or account-wide."""
    import main

    client, as_ = served
    monkeypatch.setattr(main.app.state.application, "repository", TransferRepository(), raising=False)
    for role in (Role.INTEGRATION, Role.USER, Role.READONLY, Role.ADMIN):
        response = await client.get("/api/stats", headers=as_(role))
        assert response.status_code == 200, role
    stats = (await client.get("/api/stats", headers=as_(Role.INTEGRATION))).json()
    for field in ("operator_active_downloads", "queued_downloads", "completed_count", "error_count"):
        assert type(stats[field]) is int, field                         # the Homepage contract, unchanged
    # Aggregates only: no per-transfer, request or account detail.
    assert not ({"items", "requests", "accounts", "names", "paths"} & set(stats))
    assert all(isinstance(count, int) for count in stats["by_status"].values())

    integration = as_(Role.INTEGRATION)
    for path in ("/api/stats/detail", "/api/settings", "/api/accounts", "/api/events", "/api/torrents"):
        assert (await client.get(path, headers=integration)).status_code == 403, path
    assert (await client.get("/api/metrics", headers=integration)).status_code == 200   # unchanged

    for header in ({"Authorization": "Bearer dp_not-a-real-token"}, {"Authorization": "Bearer nonsense"}):
        assert (await client.get("/api/stats", headers=header)).status_code == 401
    [token] = [item for item in await accounts.list_tokens(DEFAULT_INTEGRATION_ID) if not item["revoked_at"]]
    await accounts.revoke_token(DEFAULT_INTEGRATION_ID, token["id"])
    assert (await client.get("/api/stats", headers=integration)).status_code == 401


# ── what the interface may offer: caller-specific access ────────────────────

async def test_access_names_destroy_and_own_withdrawable_demand_per_caller(core):
    from auth.permissions import transfer_access

    repository, _engine, _application = core
    alice_p, bob_p = (Principal(account_id=name, role=Role.USER.value) for name in ("alice", "bob"))
    exclusive, alice_only = await _submit(repository, "alice", payload=MAGNET.replace("b" * 40, "9" * 40))
    shared, alice_shared = await _submit(repository, "alice")
    _same, bob_repeat = await _submit(repository, "bob")            # a repeat: no request row of its own
    contributor, bob_contrib = await _submit(repository, "bob", payload=MAGNET.replace("b" * 40, "c" * 40))
    async with database.get_db() as db:
        primary = await db.execute_returning_id(
            "INSERT INTO download_files(torrent_id,filename,status) VALUES(?,'x','pending')", (exclusive,))
        standby = await db.execute_returning_id(
            "INSERT INTO download_files(torrent_id,filename,status) VALUES(?,'x','duplicate')", (contributor,))
        request = (await db.fetchone("SELECT id FROM transfer_requests WHERE transfer_id=?", (contributor,)))["id"]
        await db.execute("INSERT INTO artifact_consolidations(contributing_artifact_id,source_transfer_id,"
                         "source_request_id,canonical_artifact_id) VALUES(?,?,?,?)",
                         (standby, contributor, request, primary))
        await db.commit()

    alice = await transfer_access(alice_p, repository, [shared])
    bob = await transfer_access(bob_p, repository, [shared, exclusive])
    assert alice[shared] == {"destroy": False, "own_requests": [alice_shared]}   # bob still needs it
    assert bob[shared] == {"destroy": False, "own_requests": [bob_repeat]}       # a repeat's own demand
    # alice's transfer now also serves bob's consolidated demand.
    assert bob[exclusive] == {"destroy": False, "own_requests": [bob_contrib]}
    assert (await transfer_access(alice_p, repository, [exclusive]))[exclusive]["destroy"] is False

    await _engine.withdraw_demand((bob_contrib,), by="account:bob")
    assert (await transfer_access(alice_p, repository, [exclusive]))[exclusive] == {
        "destroy": True, "own_requests": [alice_only]}
    readonly = await transfer_access(Principal(account_id="alice", role=Role.READONLY.value), repository,
                                     [exclusive, shared])
    assert set(map(json.dumps, readonly.values())) == {json.dumps({"destroy": False, "own_requests": []})}


async def test_stale_access_cannot_bypass_the_server(core):
    from auth.permissions import require_destruction, transfer_access
    from fastapi import HTTPException

    repository, _engine, _application = core
    transfer_id, _alice = await _submit(repository, "alice")
    principal = Principal(account_id="alice", role=Role.USER.value)
    assert (await transfer_access(principal, repository, [transfer_id]))[transfer_id]["destroy"] is True
    await _submit(repository, "bob")            # the interface still shows Remove
    with pytest.raises(HTTPException) as refused:
        await require_destruction(SimpleNamespace(state=SimpleNamespace(principal=principal)), repository, transfer_id)
    assert refused.value.status_code == 403


async def test_humans_get_the_display_timezone_and_integration_sees_only_its_own_requests(served, monkeypatch):
    client, as_ = served
    monkeypatch.setenv("TZ", "Europe/Berlin")
    for role in (Role.USER, Role.READONLY):
        assert (await client.get("/api/auth/session", headers=as_(role))).json()["display_timezone"] == "Europe/Berlin"
        assert (await client.get("/api/settings", headers=as_(role))).status_code == 403
    import main

    repository = TransferRepository()
    monkeypatch.setattr(main.app.state.application, "repository", repository, raising=False)
    await repository.admit((TransferRequest("magnet", MAGNET, name="x", fingerprint="b" * 40),), name="x",
                           demand=SubmissionDemand("someone-else"))
    listed = (await client.get("/api/requests", headers=as_(Role.INTEGRATION))).json()["requests"]
    assert all(row["account_id"] == DEFAULT_INTEGRATION_ID for row in listed)
    assert (await client.get("/api/torrents", headers=as_(Role.INTEGRATION))).status_code == 403
