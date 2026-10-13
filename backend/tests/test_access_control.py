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
        "api.account_routes.change_own_display_name", "api.account_routes.change_own_password",
        "api.account_routes.list_requests", "api.account_routes.withdraw_request",
        "api.auth_routes.auth_session_status", "api.auth_routes.logout",
        "api.routes.add_debrid_links", "api.routes.add_link_file", "api.routes.add_magnet",
        "api.routes.add_multimeta_file", "api.routes.add_torrent_file", "api.routes.add_usenet_file",
        "api.routes.add_usenet_link", "api.routes.prometheus_metrics",
    ])
    assert ROLE_CAPABILITIES[Role.ADMIN] >= ROLE_CAPABILITIES[Role.USER] >= ROLE_CAPABILITIES[Role.READONLY]


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
