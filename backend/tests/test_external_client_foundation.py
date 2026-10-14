"""DP 1.0.13 external submission & delivery foundation (protocol v1).

Discriminating proofs of the native external-client contract
(``external.service``) over the real canonical engine, demand ledger,
accounts and filesystem, driven by unrelated fake integrations: client-scoped
idempotency with exactly one demand, client-binding isolation and rotation,
demand-only withdrawal, advisory pause/resume adjudication against shared
interest and global holds, per-job status, delivery generations over verified
material confined to a profile root, acknowledgment and its staleness,
retention, crash/restart reconciliation and bounded polling.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

import core.config as config
import db.database as database
from auth import accounts
from auth.models import Principal
from external.profiles import DeliveryProfile, ExternalDeliverySettings
from external.service import ExternalClients, ExternalRefused, external_mutation_limiter
from fake_integrations import MemoryExecutor, ParcelProvider
from transfers.models import TransferState
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

MAGNET = "magnet:?xt=urn:btih:" + "e" * 40 + "&dn=episode.mkv"
OTHER_MAGNET = "magnet:?xt=urn:btih:" + "f" * 40 + "&dn=movie.mkv"


@pytest_asyncio.fixture
async def world(tmp_path, monkeypatch):
    """The canonical engine + application + accounts, one Integration account
    with two independently issued tokens (two clients) and a second account."""
    from application.service import ApplicationService
    from transfers.convergence_engine import TransferEngine
    from transfers.recovery_repository import TransferRepository

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "external.db")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config" / "config.json")
    await database.init_db()
    external_mutation_limiter.clear()
    payloads = tmp_path / "payloads"
    payloads.mkdir()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    # The fake provider takes this protocol's real request kinds.
    provider.descriptor = replace(provider.descriptor,
                                  request_types=frozenset({"parcel", "parcel-member", "magnet", "https"}))
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    now = [1000.0]
    engine = TransferEngine(repository, registry, download_root=str(payloads),
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=4),
                            clock=lambda: now[0])
    await engine.initialize()
    application = ApplicationService(engine)
    sonarr = await accounts.create_integration_account("Sonarr")
    radarr = await accounts.create_integration_account("Radarr")
    first, _ = await accounts.create_token(sonarr.id, "sonarr-a")
    second, _ = await accounts.create_token(sonarr.id, "sonarr-b")
    third, _ = await accounts.create_token(radarr.id, "radarr")

    def principal(account, token):
        return Principal.integration_token(account, token["id"], token["client_id"])

    clients = SimpleNamespace(a=principal(sonarr, first), b=principal(sonarr, second), r=principal(radarr, third))
    settings = config.AppSettings(download_folder=str(payloads), external_delivery=ExternalDeliverySettings(profiles=[
        DeliveryProfile(id="tv", categories=["tv"], clients=[first["client_id"], second["client_id"]],
                        local_root=str(payloads), import_root="/imports/tv"),
        DeliveryProfile(id="movies", categories=["movies"], clients=[third["client_id"], first["client_id"]],
                        local_root=str(payloads), import_root="/imports/movies"),
    ]))
    previous = config.get_settings()
    config.apply_settings(settings)
    yield SimpleNamespace(engine=engine, repository=repository, application=application, executor=executor,
                          provider=provider, external=ExternalClients(application), clients=clients,
                          accounts=SimpleNamespace(sonarr=sonarr, radarr=radarr),
                          tokens=SimpleNamespace(a=first, b=second, r=third), payloads=payloads, now=now)
    config.apply_settings(previous)


def body(key, *, source=None, category="tv", **extra):
    return {"idempotency_key": key, "category": category,
            "source": source or {"kind": "magnet", "magnet": MAGNET}, **extra}


async def demands(transfer_id=None):
    async with database.get_db() as db:
        if transfer_id is None:
            return await db.fetchall("SELECT * FROM transfer_demands ORDER BY created_at, id")
        return await db.fetchall("SELECT * FROM transfer_demands WHERE transfer_id=?", (transfer_id,))


async def transfer_of(job_id):
    async with database.get_db() as db:
        row = await db.fetchone("SELECT d.transfer_id FROM external_jobs j JOIN transfer_demands d "
                                "ON d.id=j.demand_id WHERE j.id=?", (job_id,))
    return int(row["transfer_id"])


async def complete(world, transfer_id):
    """Drive one transfer's every artifact to verified completion."""
    for _ in range(6):
        await world.engine.tick()
        artifacts = await world.repository.artifacts(transfer_id)
        running = [item for item in artifacts if item.execution and item.state != "completed"]
        for artifact in running:
            if world.executor.jobs.get(artifact.execution.attempt_id) is not None:
                world.executor.finish(artifact.execution)
        if (await world.repository.get(transfer_id)).state == TransferState.COMPLETED:
            return
    raise AssertionError(f"transfer {transfer_id} did not complete")


# ── idempotency ─────────────────────────────────────────────────────────────

async def test_concurrent_identical_retries_create_one_job_and_one_demand(world):
    results = await asyncio.gather(*(world.external.submit(world.clients.a, body("k-1")) for _ in range(6)))
    assert len({job["id"] for job, _created in results}) == 1
    assert sum(created for _job, created in results) == 1
    assert len(await demands()) == 1
    # A later retry is the same job, never a second demand.
    again, created = await world.external.submit(world.clients.a, body("k-1"))
    assert not created and again["id"] == results[0][0]["id"] and len(await demands()) == 1


async def test_same_key_with_another_intent_conflicts_and_admits_nothing(world):
    await world.external.submit(world.clients.a, body("k-1"))
    for changed in (body("k-1", source={"kind": "magnet", "magnet": OTHER_MAGNET}), body("k-1", category="movies"),
                    body("k-1", display_name="renamed")):
        with pytest.raises(ExternalRefused) as refused:
            await world.external.submit(world.clients.a, changed)
        assert (refused.value.code, refused.value.status) == ("idempotency_conflict", 409)
    assert len(await demands()) == 1


async def test_distinct_keys_are_distinct_jobs_and_demand_sharing_one_transfer(world):
    first, _ = await world.external.submit(world.clients.a, body("k-1"))
    second, _ = await world.external.submit(world.clients.a, body("k-2"))
    other, _ = await world.external.submit(world.clients.r, body("k-1", category="movies"))
    assert len({first["id"], second["id"], other["id"]}) == 3
    rows = await demands()
    assert len(rows) == 3 and len({row["transfer_id"] for row in rows}) == 1   # one physical work
    # The job ID is never the transfer, demand or delivery identity.
    assert first["id"].startswith("job_") and first["delivery"]["delivery_id"].startswith("dlv_")
    assert first["id"] not in {str(rows[0]["transfer_id"]), *(row["id"] for row in rows),
                               first["delivery"]["delivery_id"]}
    assert "transfer_id" not in first


async def test_a_submission_without_an_idempotency_key_is_refused_never_deduplicated_by_content(world):
    for missing in ({"category": "tv", "source": {"kind": "magnet", "magnet": MAGNET}},
                    body("   ")):
        with pytest.raises(ExternalRefused) as refused:
            await world.external.submit(world.clients.a, missing)
        assert refused.value.code == "idempotency_key_required"
    assert await demands() == []


async def test_submission_cannot_choose_provider_executor_priority_or_destination(world):
    for field in ("provider", "executor", "priority", "queue_position", "destination", "local_path"):
        with pytest.raises(ExternalRefused) as refused:
            await world.external.submit(world.clients.a, body("k", **{field: "x"}))
        assert refused.value.code == "invalid_request"
    for category in ("unknown", "../../etc", "/download"):
        with pytest.raises(ExternalRefused) as refused:
            await world.external.submit(world.clients.a, body("k", category=category))
        assert refused.value.code == "unknown_category"
    # A category the client is not authorized for is indistinguishable from none.
    with pytest.raises(ExternalRefused) as refused:
        await world.external.submit(world.clients.b, body("k", category="movies"))
    assert refused.value.code == "unknown_category"
    with pytest.raises(ExternalRefused) as refused:
        await world.external.submit(world.clients.a, body("k", source={"kind": "nzb_file", "data": "x"}))
    assert refused.value.code == "unsupported_source"
    assert await demands() == []


# ── client identity ─────────────────────────────────────────────────────────

async def test_a_second_token_of_the_same_account_is_another_client(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    for other in (world.clients.b, world.clients.r):
        with pytest.raises(ExternalRefused) as hidden:
            await world.external.job(other, job["id"])
        assert (hidden.value.code, hidden.value.status) == ("job_not_found", 404)
        for act in (world.external.withdraw(other, job["id"]),
                    world.external.control(other, job["id"], {"kind": "pause", "request_key": "p"}),
                    world.external.delivery(other, job["id"])):
            with pytest.raises(ExternalRefused) as denied:
                await act
            assert denied.value.status == 404
        assert (await world.external.jobs(other))["jobs"] == []
    # The same idempotency key under another client is its own job.
    mine, created = await world.external.submit(world.clients.b, body("k-1"))
    assert created and mine["id"] != job["id"]


async def test_a_guessed_client_id_or_a_session_is_no_external_client(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    forged = replace(world.clients.b, client_id=world.clients.a.client_id, account_id=world.accounts.radarr.id)
    with pytest.raises(ExternalRefused):
        await world.external.job(forged, job["id"])   # the account fence holds too
    admin = Principal.standalone()
    with pytest.raises(ExternalRefused) as refused:
        await world.external.job(admin, job["id"])
    assert (refused.value.code, refused.value.status) == ("client_credential_required", 403)


async def test_rotation_continues_the_client_and_revocation_keeps_its_demand(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    successor, raw = await accounts.create_token(world.accounts.sonarr.id, "sonarr-a-2",
                                                 succeeds=world.tokens.a["id"])
    assert successor["client_id"] == world.clients.a.client_id
    resolved = await accounts.resolve_token(raw)
    assert resolved[2] == world.clients.a.client_id
    rotated = Principal.integration_token(*resolved)
    assert (await world.external.job(rotated, job["id"]))["id"] == job["id"]
    # The predecessor stopped authenticating; nothing was withdrawn.
    tokens = {item["id"] for item in await accounts.list_tokens(world.accounts.sonarr.id)}
    assert world.tokens.a["id"] not in tokens
    assert [row["withdrawn_at"] for row in await demands()] == [None]
    async with database.get_db() as db:
        audit = await db.fetchall("SELECT event_type FROM event_journal WHERE event_type LIKE 'administration.%'")
    assert "administration.integration_token_rotated" in {row["event_type"] for row in audit}
    # A token of ANOTHER account can never succeed this one.
    with pytest.raises(accounts.AccountRefused):
        await accounts.create_token(world.accounts.radarr.id, "steal", succeeds=successor["id"])
    # Plain revocation: access stops, the job and its demand stay.
    await accounts.revoke_token(world.accounts.sonarr.id, successor["id"])
    assert await accounts.resolve_token(raw) is None
    assert [row["withdrawn_at"] for row in await demands()] == [None]
    # Explicit administrative recovery: a successor of the revoked token.
    _recovered, raw2 = await accounts.create_token(world.accounts.sonarr.id, "sonarr-a-3", succeeds=successor["id"])
    recovered_principal = Principal.integration_token(*await accounts.resolve_token(raw2))
    assert (await world.external.job(recovered_principal, job["id"]))["phase"] == "accepted"


async def test_account_disable_withdraws_its_demand_by_policy_and_history_survives(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    await accounts.set_enabled(accounts.DEFAULT_ADMIN_ID, world.accounts.sonarr.id, False)
    await world.application.settle_withdrawn_demand()
    await accounts.set_enabled(accounts.DEFAULT_ADMIN_ID, world.accounts.sonarr.id, True)
    viewed = await world.external.job(world.clients.a, job["id"])
    assert (viewed["phase"], viewed["reason"]) == ("withdrawn", "withdrawn_by_administrator")


async def test_a_merged_accounts_client_continues_only_through_an_authorized_successor(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    target = await accounts.create_integration_account("Sonarr (merged)")
    await accounts.merge_accounts(accounts.DEFAULT_ADMIN_ID, [world.accounts.sonarr.id], target.id)
    # The folded account is no longer active: its token admits nothing.
    _survivor, raw = await accounts.create_token(target.id, "sonarr-merged", succeeds=world.tokens.a["id"])
    principal = Principal.integration_token(*await accounts.resolve_token(raw))
    assert (await world.external.job(principal, job["id"]))["id"] == job["id"]
    # A fresh token of the surviving account is another client.
    _plain, raw_plain = await accounts.create_token(target.id, "unrelated")
    with pytest.raises(ExternalRefused):
        await world.external.job(Principal.integration_token(*await accounts.resolve_token(raw_plain)), job["id"])
    assert [row["withdrawn_at"] for row in await demands()] == [None]


# ── withdrawal and shared interest ──────────────────────────────────────────

async def test_withdrawal_retires_only_the_clients_own_demand(world):
    mine, _ = await world.external.submit(world.clients.a, body("k-1"))
    theirs, _ = await world.external.submit(world.clients.r, body("k-1", category="movies"))
    transfer_id = await transfer_of(mine["id"])
    withdrawn = await world.external.withdraw(world.clients.a, mine["id"])
    assert (withdrawn["phase"], withdrawn["reason"], withdrawn["already_withdrawn"]) == (
        "withdrawn", "withdrawn_by_client", False)
    again = await world.external.withdraw(world.clients.a, mine["id"])
    assert again["already_withdrawn"] and again["revision"] == withdrawn["revision"]
    assert (await world.repository.get(transfer_id)).state not in {TransferState.CANCELLED, TransferState.DELETED}
    await complete(world, transfer_id)
    assert (await world.external.job(world.clients.r, theirs["id"]))["phase"] == "completed"
    # The withdrawn client still sees withdrawn, and was never given a delivery.
    late = await world.external.job(world.clients.a, mine["id"])
    assert late["phase"] == "withdrawn" and late["delivery"]["generation"] is None


async def test_the_last_withdrawal_stops_the_work_through_the_core(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    await world.external.withdraw(world.clients.a, job["id"])
    assert (await world.repository.get(transfer_id)).state == TransferState.CANCELLED
    assert (await world.external.job(world.clients.a, job["id"]))["phase"] == "withdrawn"


# ── advisory pause / resume ─────────────────────────────────────────────────

async def test_a_sole_consumer_pause_is_honored_then_released_when_another_requester_joins(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    await world.engine.tick()
    decision = await world.external.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"})
    assert (decision["disposition"], decision["reason"]) == ("honored", "paused")
    assert decision["effective"] in {"paused", "pausing"}
    assert (await world.external.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"}))[
        "replayed"]
    # Another client's request for the same work joins: the pause cannot stand.
    await world.external.submit(world.clients.r, body("k-9", category="movies"))
    assert await world.external._reconcile_controls() == 1
    assert not (await world.repository.pause_hold(transfer_id) or {}).get("paused")
    control = (await world.external.job(world.clients.a, job["id"]))["control"]
    assert (control["disposition"], control["retired_reason"], control["effective"]) == (
        "revoked", "shared_interest_joined", "not_paused")


async def test_a_pause_of_shared_work_is_declined_and_changes_nothing(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    await world.external.submit(world.clients.r, body("k-1", category="movies"))
    transfer_id = await transfer_of(job["id"])
    decision = await world.external.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"})
    assert (decision["disposition"], decision["reason"]) == ("declined", "shared_interest")
    assert await world.repository.pause_hold(transfer_id) is None


async def test_resume_never_overrides_an_operator_or_global_hold(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    await world.application.pause(transfer_id)          # the operator's own pause
    declined = await world.external.control(world.clients.a, job["id"], {"kind": "resume", "request_key": "r1"})
    assert (declined["disposition"], declined["reason"]) == ("declined", "paused_independently")
    pause = await world.external.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"})
    assert (pause["disposition"], pause["reason"]) == ("declined", "paused_independently")
    assert (await world.repository.pause_hold(transfer_id))["holder"] is None    # the operator's own hold
    await world.application.resume(transfer_id)
    # A client pause, then a global pause: the client's resume is declined and
    # the global pause is never lifted by it.
    honored = await world.external.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p2"})
    assert honored["disposition"] == "honored"
    await world.application.pause_all()
    held = await world.external.control(world.clients.a, job["id"], {"kind": "resume", "request_key": "r2"})
    assert (held["disposition"], held["reason"]) == ("declined", "globally_paused")
    assert await world.repository.globally_paused()
    await world.application.resume_all()
    # ``resume_all`` cleared the client's pause: it is retired, not re-applied.
    await world.external._reconcile_controls()
    control = (await world.external.job(world.clients.a, job["id"]))["control"]
    assert control["kind"] == "resume" and control["disposition"] == "declined"
    async with database.get_db() as db:
        retired = await db.fetchone("SELECT retired_reason FROM external_controls WHERE request_key='p2'")
    assert retired["retired_reason"] == "operator_changed_pause"


async def _same_second_operator_pause(world, transfer_id):
    """The operator pauses the client-held transfer, and the intent's
    timestamp is forced equal to the client's: a same-second collision,
    made deterministic. Only the pause owner's generation tells them apart."""
    client_hold = await world.repository.pause_hold(transfer_id)
    async with database.get_db() as db:
        stamp = (await db.fetchone("SELECT updated_at FROM transfer_pause_intents WHERE torrent_id=?",
                                   (transfer_id,)))["updated_at"]
    await world.application.pause(transfer_id)
    async with database.get_db() as db:
        await db.execute("UPDATE transfer_pause_intents SET updated_at=? WHERE torrent_id=?", (stamp, transfer_id))
        await db.commit()
    operator_hold = await world.repository.pause_hold(transfer_id)
    assert operator_hold["holder"] is None and operator_hold["generation"] > client_hold["generation"]
    return operator_hold


async def restarted(world):
    """A fresh engine, application and external owner over the same durable state."""
    from application.service import ApplicationService
    from transfers.convergence_engine import TransferEngine

    engine = TransferEngine(world.repository, world.engine.registry, download_root=str(world.payloads),
                            policy=world.engine.policy, clock=lambda: world.now[0])
    await engine.initialize()
    return ExternalClients(ApplicationService(engine))


async def test_an_operator_pause_in_the_same_second_cannot_be_released_by_the_client(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    pause = await world.external.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"})
    assert (pause["disposition"], pause["reason"]) == ("honored", "paused")
    operator_hold = await _same_second_operator_pause(world, transfer_id)
    resume = await world.external.control(world.clients.a, job["id"], {"kind": "resume", "request_key": "r1"})
    assert (resume["disposition"], resume["reason"], resume["effective"]) == (
        "declined", "paused_independently", "held_by_operator")
    assert await world.repository.pause_hold(transfer_id) == operator_hold          # nothing written
    # The client's control reports what is actually true now.
    control = (await world.external.job(world.clients.a, job["id"]))["paused_by"]
    assert control == "operator"
    # Restart and recovery change nothing: the operator's hold stands.
    after = await restarted(world)
    resume = await after.control(world.clients.a, job["id"], {"kind": "resume", "request_key": "r2"})
    assert (resume["disposition"], resume["reason"]) == ("declined", "paused_independently")
    assert await world.repository.pause_hold(transfer_id) == operator_hold


async def test_reconciliation_never_releases_an_operator_pause_that_replaced_a_client_hold(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    await world.external.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"})
    operator_hold = await _same_second_operator_pause(world, transfer_id)
    # Another requester joins: shared interest would release a CLIENT hold,
    # but this hold is the operator's now.
    await world.external.submit(world.clients.r, body("k-9", category="movies"))
    assert await world.external._reconcile_controls() == 1
    assert await world.repository.pause_hold(transfer_id) == operator_hold
    async with database.get_db() as db:
        control = await db.fetchone("SELECT disposition, retired_reason FROM external_controls WHERE request_key='p1'")
    assert (control["disposition"], control["retired_reason"]) == ("honored", "operator_changed_pause")
    # ... and after a restart nothing is released either.
    after = await restarted(world)
    assert await after._reconcile_controls() == 0
    assert (await after.maintain())["released"] == 0
    assert await world.repository.pause_hold(transfer_id) == operator_hold
    assert (await world.repository.get(transfer_id)).paused


async def test_a_proven_client_hold_survives_restart_and_is_released_exactly(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    await world.external.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"})
    after = await restarted(world)
    resumed = await after.control(world.clients.a, job["id"], {"kind": "resume", "request_key": "r1"})
    assert (resumed["disposition"], resumed["reason"], resumed["effective"]) == ("honored", "resumed", "not_paused")
    assert not (await world.repository.pause_hold(transfer_id))["paused"]
    # The same hold, once more after restart, is released by shared interest.
    await after.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p2"})
    await after.submit(world.clients.r, body("k-9", category="movies"))
    again = await restarted(world)
    assert await again._reconcile_controls() == 1
    assert not (await world.repository.pause_hold(transfer_id))["paused"]
    assert not (await world.repository.get(transfer_id)).paused


async def test_the_fenced_release_refuses_a_stale_generation_and_a_global_pause(world):
    """The pause owner's own fence, directly: only the exact standing hold is
    released, and never while processing is globally paused."""
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    await world.engine.pause(transfer_id, holder="external:ctl_test")
    hold = await world.repository.pause_hold(transfer_id)
    assert hold["holder"] == "external:ctl_test"
    stale = ("external:ctl_test", int(hold["generation"]) - 1)
    assert await world.engine.resume(transfer_id, held_by=stale) is None
    assert await world.engine.resume(transfer_id, held_by=("external:ctl_other", int(hold["generation"]))) is None
    await world.repository.global_pause(True)
    assert await world.engine.resume(transfer_id, held_by=("external:ctl_test", int(hold["generation"]))) is None
    assert await world.repository.globally_paused()
    await world.repository.global_pause(False)
    assert await world.engine.resume(transfer_id, held_by=("external:ctl_test", int(hold["generation"]))) == ()
    assert not (await world.repository.pause_hold(transfer_id))["paused"]


class Interrupted(RuntimeError):
    """An injected crash at one persistence boundary of an advisory control."""


def interrupt(monkeypatch, owner, name):
    original = getattr(owner, name)

    async def crash(*args, **kwargs):
        monkeypatch.setattr(owner, name, original)
        raise Interrupted(name)

    monkeypatch.setattr(owner, name, crash)


async def _controls(job_id):
    async with database.get_db() as db:
        return await db.fetchall("SELECT request_key, kind, disposition, reason, pause_holder, pause_generation, "
                                 "retired_reason FROM external_controls WHERE job_id=? ORDER BY created_at", (job_id,))


async def test_interrupted_before_the_canonical_pause_write_leaves_nothing_and_settles_as_interrupted(
        world, monkeypatch):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    interrupt(monkeypatch, world.repository, "set_pause_and_fence")        # the canonical write itself
    with pytest.raises(Interrupted):
        await world.external.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"})
    assert [(row["disposition"], row["reason"]) for row in await _controls(job["id"])] == [("pending", "applying")]
    assert await world.repository.pause_hold(transfer_id) is None
    after = await restarted(world)
    assert (await after.maintain())["recovered"] == 1
    assert [(row["disposition"], row["reason"]) for row in await _controls(job["id"])] == [
        ("declined", "interrupted")]
    replay = await after.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"})
    assert (replay["replayed"], replay["disposition"], replay["reason"], replay["effective"]) == (
        True, "declined", "interrupted", "not_paused")
    assert await world.repository.pause_hold(transfer_id) is None          # the retry created no hold


async def test_interrupted_after_the_pause_committed_is_recovered_by_the_same_key_as_the_clients_hold(
        world, monkeypatch):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    interrupt(monkeypatch, ExternalClients, "_record_control")
    with pytest.raises(Interrupted):
        await world.external.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"})
    hold = await world.repository.pause_hold(transfer_id)
    pending = (await _controls(job["id"]))[0]
    assert hold["paused"] and hold["holder"] == pending["pause_holder"] and pending["disposition"] == "pending"
    after = await restarted(world)
    replay = await after.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"})
    assert (replay["replayed"], replay["disposition"], replay["reason"], replay["effective"]) == (
        True, "honored", "paused", "paused")
    assert await world.repository.pause_hold(transfer_id) == hold          # recovered, never re-applied
    assert (await _controls(job["id"]))[0]["pause_generation"] == hold["generation"]
    # It is now an ordinary, provably owned client hold: the client releases it.
    resumed = await after.control(world.clients.a, job["id"], {"kind": "resume", "request_key": "r1"})
    assert (resumed["disposition"], resumed["reason"]) == ("honored", "resumed")
    assert not (await world.repository.pause_hold(transfer_id))["paused"]


async def test_an_orphaned_hold_is_recovered_by_maintenance_and_released_when_another_requester_joins(
        world, monkeypatch):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    interrupt(monkeypatch, ExternalClients, "_record_control")
    with pytest.raises(Interrupted):
        await world.external.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"})
    await world.external.submit(world.clients.r, body("k-9", category="movies"))
    after = await restarted(world)
    outcome = await after.maintain()
    assert (outcome["recovered"], outcome["released"]) == (1, 1)
    assert not (await world.repository.pause_hold(transfer_id))["paused"]
    assert [(row["disposition"], row["retired_reason"]) for row in await _controls(job["id"])] == [
        ("revoked", "shared_interest_joined")]


async def test_an_operator_pause_before_recovery_is_never_undone(world, monkeypatch):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    interrupt(monkeypatch, ExternalClients, "_record_control")
    with pytest.raises(Interrupted):
        await world.external.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"})
    operator_hold = await _same_second_operator_pause(world, transfer_id)
    after = await restarted(world)
    assert (await after.maintain())["recovered"] == 1
    assert [(row["disposition"], row["reason"]) for row in await _controls(job["id"])] == [
        ("superseded", "interrupted_unconfirmed")]
    replay = await after.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"})
    assert (replay["disposition"], replay["effective"]) == ("superseded", "held_by_operator")
    resume = await after.control(world.clients.a, job["id"], {"kind": "resume", "request_key": "r1"})
    assert (resume["disposition"], resume["reason"]) == ("declined", "paused_independently")
    await after.submit(world.clients.r, body("k-9", category="movies"))
    await after.maintain()
    assert await world.repository.pause_hold(transfer_id) == operator_hold


async def test_interrupted_after_the_result_was_recorded_replays_the_recorded_decision(world, monkeypatch):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    interrupt(monkeypatch, ExternalClients, "_refresh")                     # after the result commit
    with pytest.raises(Interrupted):
        await world.external.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"})
    hold = await world.repository.pause_hold(transfer_id)
    after = await restarted(world)
    assert (await after.maintain())["recovered"] == 0
    replay = await after.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"})
    assert (replay["replayed"], replay["disposition"], replay["reason"]) == (True, "honored", "paused")
    assert await world.repository.pause_hold(transfer_id) == hold
    assert len(await _controls(job["id"])) == 1


async def test_an_interrupted_resume_is_settled_without_reapplying_or_repausing(world, monkeypatch):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    await world.external.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"})
    # Before the release write: the client's hold still stands, provably.
    interrupt(monkeypatch, world.repository, "set_pause_and_fence")
    with pytest.raises(Interrupted):
        await world.external.control(world.clients.a, job["id"], {"kind": "resume", "request_key": "r1"})
    held = await world.repository.pause_hold(transfer_id)
    after = await restarted(world)
    replay = await after.control(world.clients.a, job["id"], {"kind": "resume", "request_key": "r1"})
    assert (replay["disposition"], replay["reason"], replay["effective"]) == ("declined", "interrupted", "paused")
    assert await world.repository.pause_hold(transfer_id) == held
    # After the release committed: the outcome cannot be attributed, so it is
    # recorded as unconfirmed and the pause control retired; nothing re-pauses.
    interrupt(monkeypatch, ExternalClients, "_record_control")
    with pytest.raises(Interrupted):
        await after.control(world.clients.a, job["id"], {"kind": "resume", "request_key": "r2"})
    released = await world.repository.pause_hold(transfer_id)
    assert not released["paused"]
    again = await restarted(world)
    assert (await again.maintain())["recovered"] == 1
    rows = {row["request_key"]: row for row in await _controls(job["id"])}
    assert (rows["r2"]["disposition"], rows["r2"]["reason"]) == ("superseded", "interrupted_unconfirmed")
    assert rows["p1"]["retired_reason"] == "interrupted_unconfirmed"
    assert await world.repository.pause_hold(transfer_id) == released


async def test_a_declined_or_released_control_never_stops_shared_work(world):
    mine, _ = await world.external.submit(world.clients.a, body("k-1"))
    theirs, _ = await world.external.submit(world.clients.r, body("k-1", category="movies"))
    transfer_id = await transfer_of(mine["id"])
    declined = await world.external.control(world.clients.a, mine["id"], {"kind": "pause", "request_key": "p"})
    assert declined["disposition"] == "declined"
    await complete(world, transfer_id)
    assert (await world.external.job(world.clients.r, theirs["id"]))["phase"] == "completed"
    assert (await world.external.job(world.clients.a, mine["id"]))["phase"] == "completed"


async def test_client_resume_releases_only_its_own_pause(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    await world.external.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p1"})
    resumed = await world.external.control(world.clients.a, job["id"], {"kind": "resume", "request_key": "r1"})
    assert (resumed["disposition"], resumed["reason"], resumed["effective"]) == ("honored", "resumed", "not_paused")
    assert not (await world.repository.pause_hold(transfer_id))["paused"]
    noop = await world.external.control(world.clients.a, job["id"], {"kind": "resume", "request_key": "r2"})
    assert (noop["disposition"], noop["reason"]) == ("honored", "not_paused")
    with pytest.raises(ExternalRefused) as conflict:
        await world.external.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "r1"})
    assert conflict.value.code == "control_conflict"


async def test_a_completed_or_withdrawn_job_declines_controls(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    await complete(world, await transfer_of(job["id"]))
    decision = await world.external.control(world.clients.a, job["id"], {"kind": "pause", "request_key": "p"})
    assert (decision["disposition"], decision["reason"]) == ("declined", "already_settled")
    other, _ = await world.external.submit(world.clients.a, body("k-2", source={"kind": "magnet",
                                                                                 "magnet": OTHER_MAGNET}))
    await world.external.withdraw(world.clients.a, other["id"])
    decision = await world.external.control(world.clients.a, other["id"], {"kind": "pause", "request_key": "p"})
    assert (decision["disposition"], decision["reason"]) == ("declined", "job_withdrawn")


# ── completion and delivery ─────────────────────────────────────────────────

async def test_one_missing_member_never_reports_completion(world):
    from transfers.models import ResourceState, TransferRequest

    # One magnet whose resource holds two members: one logical request.
    parcel = world.provider.parcel("season", state=ResourceState.AVAILABLE,
                                   files=[("one.bin", "Show/one.bin", 4), ("two.bin", "Show/two.bin", 4)])
    world.provider.responses.append(replace(parcel, observation=replace(
        parcel.observation, request=TransferRequest("magnet", MAGNET))))
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    for _ in range(3):
        await world.engine.tick()
    artifacts = await world.repository.artifacts(transfer_id)
    assert len(artifacts) == 2
    world.executor.finish(artifacts[0].execution)
    await world.engine.tick()
    partial = await world.external.job(world.clients.a, job["id"])
    assert partial["phase"] != "completed" and partial["delivery"]["state"] == "partial"
    assert partial["progress"]["completed_members"] == 1 and partial["progress"]["total_members"] == 2
    assert (await world.external.delivery(world.clients.a, job["id"])).get("files") is None
    await complete(world, transfer_id)
    done = await world.external.delivery(world.clients.a, job["id"])
    assert done["state"] == "available" and done["generation"] == 1 and done["complete"]
    assert sorted(item["name"] for item in done["files"]) == ["one.bin", "two.bin"]
    assert all(item["import_path"].startswith("/imports/tv/") for item in done["files"])
    assert str(world.payloads) not in json.dumps(done)          # DebridPulse's own root is never disclosed


async def test_finalization_delays_readiness_and_a_finalization_without_an_inventory_is_not_delivered(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    await complete(world, transfer_id)
    async with database.get_db() as db:
        await db.execute("UPDATE torrents SET status='extracting' WHERE id=?", (transfer_id,))
        await db.commit()
    waiting = await world.external.job(world.clients.a, job["id"])
    assert (waiting["phase"], waiting["reason"], waiting["delivery"]["generation"]) == (
        "processing", "finalizing", None)
    # A finalization recorded before outputs were recorded (or none at all):
    # what the transfer now consists of is unknown, so nothing is committed.
    async with database.get_db() as db:
        await db.execute("UPDATE torrents SET status='completed', extraction_status='completed' WHERE id=?",
                         (transfer_id,))
        await db.execute("INSERT INTO postprocess_attempts(transfer_id,processor_id,state,paths,outcome) "
                         "VALUES(?,'archive','finished','[]',?)",
                         (transfer_id, json.dumps({"kind": "success", "detail": "Extracted 1 archive(s)"})))
        await db.commit()
    extracted = await world.external.job(world.clients.a, job["id"])
    assert (extracted["phase"], extracted["reason"], extracted["delivery"]["state"]) == (
        "failed", "extraction_outputs_unrecorded", "unavailable")
    async with database.get_db() as db:
        await db.execute("UPDATE torrents SET extraction_status='error' WHERE id=?", (transfer_id,))
        await db.commit()
    assert (await world.external.job(world.clients.a, job["id"]))["reason"] == "finalization_failed"


@pytest.mark.parametrize("delete_archive", [True, False])
async def test_a_real_extraction_delivers_exactly_its_recorded_final_file_set(world, monkeypatch, delete_archive):
    """The archive finalizer records the final file set it left (the files it
    extracted, the inputs it kept) and delivery commits exactly that, verified
    on disk; an unrelated file in the same directory is never delivered."""
    import zipfile

    from postprocessors.archive.processor import ArchivePostProcessor
    from transfers.models import ExecutionState, ResolutionResult, ResourceState, TransferProgress

    source = world.payloads.parent / "fixture.zip"
    with zipfile.ZipFile(source, "w") as handle:
        handle.writestr("Show/episode.mkv", b"episode payload")
        handle.writestr("Show/episode.nfo", b"info")
    payload = source.read_bytes()
    candidate = replace(world.provider.candidate("fixture.zip"), expected_bytes=len(payload))
    world.provider.responses = [ResolutionResult(ResourceState.AVAILABLE, (candidate,))]
    world.engine.postprocessors = (ArchivePostProcessor(),)
    monkeypatch.setattr("postprocessors.archive.processor.get_settings", lambda: SimpleNamespace(
        extract_max_concurrent=1, extract_delete_archive=delete_archive))
    (world.payloads / "unrelated.bin").write_bytes(b"someone else's")
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    transfer_id = await transfer_of(job["id"])
    await world.engine.tick()
    artifact = (await world.repository.artifacts(transfer_id))[0]
    Path(artifact.target).parent.mkdir(parents=True, exist_ok=True)
    Path(artifact.target).write_bytes(payload)
    running = world.executor.jobs[artifact.execution.attempt_id]
    world.executor.jobs[artifact.execution.attempt_id] = replace(
        running, state=ExecutionState.SUCCEEDED, progress=TransferProgress(len(payload), len(payload)))
    await world.engine.tick()
    async with database.get_db() as db:
        status = await db.fetchone("SELECT status, extraction_status FROM torrents WHERE id=?", (transfer_id,))
    assert (status["status"], status["extraction_status"]) == ("completed", "completed")
    delivered = await world.external.delivery(world.clients.a, job["id"])
    expected = ["Show/episode.mkv", "Show/episode.nfo"] + ([] if delete_archive else ["fixture.zip"])
    assert delivered["state"] == "available"
    assert sorted(item["relative_path"] for item in delivered["files"]) == sorted(expected)
    # Re-extraction over the same paths is new material: a new generation.
    episode = world.payloads / "Show" / "episode.mkv"
    os.utime(episode, ns=(1, 1))
    assert (await world.external.delivery(world.clients.a, job["id"]))["generation"] == 2
    episode.unlink()
    lost = await world.external.delivery(world.clients.a, job["id"])
    assert (lost["state"], lost["reason"]) == ("unavailable", "material_missing")


async def test_two_profiles_over_one_canonical_result_keep_independent_paths_and_acknowledgments(world):
    tv, _ = await world.external.submit(world.clients.a, body("k-1"))
    movies, _ = await world.external.submit(world.clients.r, body("k-1", category="movies"))
    await complete(world, await transfer_of(tv["id"]))
    tv_delivery = await world.external.delivery(world.clients.a, tv["id"])
    movie_delivery = await world.external.delivery(world.clients.r, movies["id"])
    assert [item["relative_path"] for item in tv_delivery["files"]] == \
        [item["relative_path"] for item in movie_delivery["files"]]
    assert tv_delivery["files"][0]["import_path"].startswith("/imports/tv/")
    assert movie_delivery["files"][0]["import_path"].startswith("/imports/movies/")
    acked = await world.external.acknowledge(world.clients.a, tv["id"], {
        "delivery_id": tv_delivery["delivery_id"], "generation": tv_delivery["generation"]})
    assert acked["state"] == "acknowledged" and not acked["already_acknowledged"]
    assert (await world.external.delivery(world.clients.r, movies["id"]))["state"] == "available"
    # Acknowledgment deleted nothing.
    assert all(os.path.exists(world.payloads / item["relative_path"]) for item in tv_delivery["files"])
    with pytest.raises(ExternalRefused) as foreign:
        await world.external.acknowledge(world.clients.a, tv["id"], {
            "delivery_id": movie_delivery["delivery_id"], "generation": 1})
    assert foreign.value.code == "delivery_mismatch"


async def test_a_stale_generation_acknowledgment_conflicts_and_a_duplicate_is_idempotent(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    await complete(world, await transfer_of(job["id"]))
    first = await world.external.delivery(world.clients.a, job["id"])
    ack = {"delivery_id": first["delivery_id"], "generation": 1}
    assert (await world.external.acknowledge(world.clients.a, job["id"], ack))["state"] == "acknowledged"
    assert (await world.external.acknowledge(world.clients.a, job["id"], ack))["already_acknowledged"]
    # The Admin remaps the shared directory: the same material is a new
    # import contract, so a new generation -- never a mutation of the old.
    settings = config.get_settings()
    profiles = [profile.model_copy(update={"import_root": "/mnt/media/tv"}) if profile.id == "tv" else profile
                for profile in settings.external_delivery.profiles]
    config.apply_settings(settings.model_copy(update={"external_delivery": ExternalDeliverySettings(
        profiles=profiles, revision=1)}))
    second = await world.external.delivery(world.clients.a, job["id"])
    assert second["generation"] == 2 and second["state"] == "available"
    assert second["files"][0]["import_path"].startswith("/mnt/media/tv/")
    with pytest.raises(ExternalRefused) as stale:
        await world.external.acknowledge(world.clients.a, job["id"], ack)
    assert (stale.value.code, stale.value.extra["current_generation"]) == ("stale_generation", 2)
    assert (await world.external.delivery(world.clients.a, job["id"]))["state"] == "available"
    async with database.get_db() as db:
        old = await db.fetchone("SELECT acknowledged_at, superseded_at, files FROM external_deliveries "
                                "WHERE generation=1")
    assert old["acknowledged_at"] is not None and old["superseded_at"] is not None
    assert "/imports/tv/" in old["files"]                       # the old evidence record is unchanged


async def test_a_profile_root_that_does_not_contain_the_material_is_a_truthful_refusal(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    await complete(world, await transfer_of(job["id"]))
    elsewhere = world.payloads / "elsewhere"
    elsewhere.mkdir()
    settings = config.get_settings()
    profiles = [profile.model_copy(update={"local_root": str(elsewhere)}) if profile.id == "tv" else profile
                for profile in settings.external_delivery.profiles]
    config.apply_settings(settings.model_copy(update={"external_delivery": ExternalDeliverySettings(
        profiles=profiles)}))
    viewed = await world.external.job(world.clients.a, job["id"])
    assert (viewed["phase"], viewed["reason"], viewed["delivery"]["state"]) == (
        "failed", "outside_export_root", "unavailable")
    assert (await world.external.delivery(world.clients.a, job["id"])).get("files") is None


async def test_material_lost_or_replaced_by_a_symlink_is_never_advertised(world):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    await complete(world, await transfer_of(job["id"]))
    delivered = await world.external.delivery(world.clients.a, job["id"])
    path = world.payloads / delivered["files"][0]["relative_path"]
    outside = world.payloads.parent / "secret.bin"
    outside.write_bytes(b"done")
    path.unlink()
    path.symlink_to(outside)
    lost = await world.external.delivery(world.clients.a, job["id"])
    assert (lost["state"], lost["reason"], lost.get("files")) == ("unavailable", "material_missing", None)
    path.unlink()
    lost = await world.external.job(world.clients.a, job["id"])
    assert lost["phase"] == "completed" and lost["delivery"]["state"] == "unavailable"


async def test_while_executors_start_nothing_is_claimed_and_nothing_is_called_missing(world, monkeypatch):
    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    await complete(world, await transfer_of(job["id"]))
    assert (await world.external.delivery(world.clients.a, job["id"]))["state"] == "available"
    monkeypatch.setattr(world.application, "integrations_starting", lambda: True)
    starting = await world.external.delivery(world.clients.a, job["id"])
    assert (starting["state"], starting["reason"], starting.get("files")) == (
        "unavailable", "verifying_material", None)
    async with database.get_db() as db:
        stored = await db.fetchone("SELECT unavailable_reason FROM external_deliveries WHERE generation=1")
        journal = await db.fetchall("SELECT 1 FROM event_journal WHERE event_type='transfer.external_delivery_unavailable'")
    assert stored["unavailable_reason"] is None and journal == []
    monkeypatch.setattr(world.application, "integrations_starting", lambda: False)
    assert (await world.external.delivery(world.clients.a, job["id"]))["state"] == "available"


async def test_retention_expiry_withdraws_the_advertisement_and_deletes_nothing(world, monkeypatch):
    import external.service as service

    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    await complete(world, await transfer_of(job["id"]))
    delivered = await world.external.delivery(world.clients.a, job["id"])
    later = service._now() + 169 * 3600
    monkeypatch.setattr(service, "_now", lambda: later)
    expired = await world.external.delivery(world.clients.a, job["id"])
    assert (expired["state"], expired["reason"], expired.get("files")) == ("expired", "retention_expired", None)
    assert (world.payloads / delivered["files"][0]["relative_path"]).exists()


# ── crash, restart and polling ──────────────────────────────────────────────

async def test_a_crash_before_admission_or_before_acceptance_never_duplicates_demand(world):
    from external import intake

    principal = world.clients.a
    profile = config.get_settings().external_delivery.select(principal.client_id, "tv")
    source = intake.normalize({"kind": "magnet", "magnet": MAGNET})
    digest = intake.intent_digest(source, profile_id="tv", category_key="tv", display_name="", client_reference=None)
    # Crash 1: reserved, never admitted. The retry admits exactly once.
    reserved = await world.external._reserve(principal, principal.client_id, "k-1", digest, source, "tv", profile,
                                             "", None)
    job, created = await world.external.submit(principal, body("k-1"))
    assert created and job["id"] == reserved["id"] and len(await demands()) == 1
    # Crash 2: admitted, never bound. Maintenance binds it from the ledger.
    other = intake.normalize({"kind": "magnet", "magnet": OTHER_MAGNET})
    digest = intake.intent_digest(other, profile_id="tv", category_key="tv", display_name="", client_reference=None)
    reserved = await world.external._reserve(principal, principal.client_id, "k-2", digest, other, "tv", profile,
                                             "", None)
    await world.external._admit(principal, reserved, other)
    outcome = await world.external.maintain()
    assert outcome["accepted"] == 1
    replay, created = await world.external.submit(principal, body("k-2", source={"kind": "magnet",
                                                                                 "magnet": OTHER_MAGNET}))
    assert not created and replay["id"] == reserved["id"] and len(await demands()) == 2


async def test_restart_reconstructs_the_same_ids_owner_and_delivery(world):
    from application.service import ApplicationService
    from transfers.convergence_engine import TransferEngine

    job, _ = await world.external.submit(world.clients.a, body("k-1"))
    await complete(world, await transfer_of(job["id"]))
    before = await world.external.delivery(world.clients.a, job["id"])
    engine = TransferEngine(world.repository, world.engine.registry, download_root=str(world.payloads),
                            policy=world.engine.policy, clock=lambda: world.now[0])
    await engine.initialize()
    restarted = ExternalClients(ApplicationService(engine))
    after = await restarted.delivery(world.clients.a, job["id"])
    assert (after["delivery_id"], after["generation"], after["files"]) == (
        before["delivery_id"], before["generation"], before["files"])
    replay, created = await restarted.submit(world.clients.a, body("k-1"))
    assert not created and replay["id"] == job["id"] and len(await demands()) == 1


async def test_polling_by_cursor_returns_exactly_the_jobs_that_changed(world):
    first, _ = await world.external.submit(world.clients.a, body("k-1"))
    second, _ = await world.external.submit(world.clients.a, body("k-2", source={"kind": "magnet",
                                                                                 "magnet": OTHER_MAGNET}))
    page = await world.external.jobs(world.clients.a)
    assert [item["id"] for item in page["jobs"]] == [first["id"], second["id"]]
    cursor = page["next_cursor"]
    assert (await world.external.jobs(world.clients.a, after=cursor))["jobs"] == []
    await world.external.withdraw(world.clients.a, first["id"])
    changed = await world.external.jobs(world.clients.a, after=cursor)
    assert [(item["id"], item["phase"]) for item in changed["jobs"]] == [(first["id"], "withdrawn")]
    assert changed["next_cursor"] > cursor
    # Another client's changes never appear, not even as a cursor gap it can read.
    await world.external.submit(world.clients.r, body("k-1", category="movies"))
    assert (await world.external.jobs(world.clients.a, after=changed["next_cursor"]))["jobs"] == []


async def test_polling_is_bounded_for_hundreds_of_jobs(world, monkeypatch):
    import external.service as service

    principal = world.clients.a
    for index in range(260):
        await world.external.submit(principal, body(f"k-{index}", source={
            "kind": "links", "links": [f"https://files.example/{index}.bin"]}))
        external_mutation_limiter.clear()
    refreshed = []
    original = ExternalClients._refresh

    async def counting(self, job_id):
        refreshed.append(job_id)
        return await original(self, job_id)

    monkeypatch.setattr(ExternalClients, "_refresh", counting)
    page = await world.external.jobs(principal, limit=200)
    assert len(page["jobs"]) == 200 and page["more"]
    assert len(refreshed) <= service.REFRESH_BUDGET
    rest = await world.external.jobs(principal, after=page["next_cursor"], limit=200)
    assert not rest["more"] and len({item["id"] for item in page["jobs"] + rest["jobs"]}) == 260


async def test_the_audit_trail_names_no_source_token_or_path(world):
    secret = "https://user:hunter2@files.example/private/path.bin?key=abc123"
    job, _ = await world.external.submit(world.clients.a, body("k-1", source={"kind": "links", "links": [secret]}))
    assert job["source"] == {"kind": "links", "count": 1, "hosts": ["files.example"]}
    await world.external.withdraw(world.clients.a, job["id"])
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT * FROM event_journal WHERE event_type LIKE 'transfer.external_%'")
        stored = await db.fetchone("SELECT * FROM external_jobs WHERE id=?", (job["id"],))
    assert {row["event_type"] for row in rows} >= {"transfer.external_job_accepted", "transfer.external_job_withdrawn"}
    text = json.dumps([dict(row) for row in rows]) + json.dumps(dict(stored))
    for leaked in ("hunter2", "abc123", "/private/path"):
        assert leaked not in text


# ── the HTTP boundary ───────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def http(world, monkeypatch, tmp_path):
    """The real application object and middleware (password mode) over the
    world's application: an Integration token for the ``tv`` profile, one for
    no profile, and an Admin session."""
    import httpx
    import main
    from account_support import account
    from auth.accounts import Role
    from auth.sessions import HTTP_SESSION_COOKIE, session_store

    from auth.passwords import hash_password

    await accounts.import_legacy_credential("operator", hash_password("external-secret"))
    token, raw = await accounts.create_token(world.accounts.sonarr.id, "http-client")
    _other, other_raw = await accounts.create_token(world.accounts.sonarr.id, "http-other")
    settings = config.get_settings()
    profiles = [profile.model_copy(update={"clients": [*profile.clients, token["client_id"]]})
                if profile.id == "tv" else profile for profile in settings.external_delivery.profiles]
    current = settings.model_copy(update={"auth_password_enabled": True, "external_delivery": ExternalDeliverySettings(
        profiles=profiles)})
    config.apply_settings(current)
    config.save_settings(current)
    monkeypatch.setattr(main.app.state, "application", world.application, raising=False)
    session_store.clear()
    admin, _ = session_store.create(Principal.password_session(account("admin", account_id=accounts.DEFAULT_ADMIN_ID,
                                                                       role=Role.ADMIN)), lifetime_seconds=3600)
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        yield SimpleNamespace(
            client=client, token=token,
            client_headers={"Authorization": f"Bearer {raw}"},
            other_headers={"Authorization": f"Bearer {other_raw}"},
            admin_headers={"Cookie": f"{HTTP_SESSION_COOKIE}={admin}", "X-CSRF-Token": session_store.csrf_token(admin)})
    session_store.clear()


async def test_http_status_semantics_and_isolation(http, world):
    client = http.client
    created = await client.post("/api/external/v1/jobs", headers=http.client_headers, json=body("h-1"))
    assert created.status_code == 201, created.text
    job = created.json()
    replay = await client.post("/api/external/v1/jobs", headers=http.client_headers, json=body("h-1"))
    assert (replay.status_code, replay.json()["id"]) == (200, job["id"])
    conflict = await client.post("/api/external/v1/jobs", headers=http.client_headers,
                                 json=body("h-1", display_name="other"))
    assert (conflict.status_code, conflict.json()["detail"]["code"]) == (409, "idempotency_conflict")
    unknown = await client.post("/api/external/v1/jobs", headers=http.client_headers, json=body("h-2", category="x"))
    assert (unknown.status_code, unknown.json()["detail"]["code"]) == (422, "unknown_category")
    assert (await client.get(f"/api/external/v1/jobs/{job['id']}", headers=http.client_headers)).status_code == 200
    hidden = await client.get(f"/api/external/v1/jobs/{job['id']}", headers=http.other_headers)
    missing = await client.get("/api/external/v1/jobs/job_doesnotexist", headers=http.other_headers)
    assert hidden.status_code == missing.status_code == 404 and hidden.json() == missing.json()
    assert (await client.get("/api/external/v1/jobs/x", headers={"Authorization": "Bearer dp_wrong"})).status_code \
        == 401
    # The underlying transfer stays out of reach, though the job serves it.
    transfer_id = await transfer_of(job["id"])
    for method, path in (("GET", f"/api/torrents/{transfer_id}"), ("POST", f"/api/torrents/{transfer_id}/pause"),
                         ("POST", f"/api/torrents/{transfer_id}/resume"), ("DELETE", f"/api/torrents/{transfer_id}"),
                         ("POST", f"/api/torrents/{transfer_id}/retry"), ("POST", f"/api/torrents/{transfer_id}/cancel"),
                         ("PATCH", f"/api/torrents/{transfer_id}/priority"), ("POST", "/api/torrents/move-to-top"),
                         ("PUT", f"/api/torrents/{transfer_id}/label"), ("POST", f"/api/torrents/{transfer_id}/input"),
                         ("POST", f"/api/torrents/{transfer_id}/files/1/block"),
                         ("POST", "/api/torrents/bulk"), ("GET", f"/api/torrents/{transfer_id}/trace"),
                         ("POST", "/api/processing/pause"), ("POST", "/api/processing/resume"),
                         ("GET", "/api/torrents"), ("GET", "/api/events/stream"), ("GET", "/api/external/profiles")):
        response = await client.request(method, path, headers=http.client_headers)
        assert response.status_code == 403, (method, path, response.text)
    # An Admin session is no external client; profiles are the Admin's.
    assert (await client.get("/api/external/v1/jobs", headers=http.admin_headers)).status_code == 403
    assert (await client.get("/api/external/profiles", headers=http.admin_headers)).status_code == 200
    capabilities = (await client.get("/api/external/v1/capabilities", headers=http.client_headers)).json()
    assert capabilities["protocol"] == "debridpulse.external/1"
    assert [profile["id"] for profile in capabilities["profiles"]] == ["tv"]
    assert "clients" not in capabilities["profiles"][0] and "local_root" not in capabilities["profiles"][0]
    # Revocation fails the very next call; the job and its demand stay.
    await accounts.revoke_token(world.accounts.sonarr.id, http.token["id"])
    assert (await client.get(f"/api/external/v1/jobs/{job['id']}", headers=http.client_headers)).status_code == 401
    assert [row["withdrawn_at"] for row in await demands()] == [None]


async def test_profiles_are_replaced_by_the_admin_with_revision_and_root_confinement(http, world):
    client = http.client
    current = (await client.get("/api/external/profiles", headers=http.admin_headers)).json()
    profiles = current["profiles"]
    outside = await client.put("/api/external/profiles", headers=http.admin_headers, json={
        "expected_revision": current["revision"],
        "profiles": [{**profiles[0], "local_root": "/etc"}, profiles[1]]})
    assert (outside.status_code, outside.json()["detail"]["code"]) == (422, "invalid_profile")
    duplicate = await client.put("/api/external/profiles", headers=http.admin_headers, json={
        "expected_revision": current["revision"], "profiles": [profiles[0], {**profiles[1], "categories": ["TV"]}]})
    assert duplicate.status_code == 422
    saved = await client.put("/api/external/profiles", headers=http.admin_headers, json={
        "expected_revision": current["revision"], "profiles": [{**profiles[0], "retention_hours": 24}, profiles[1]]})
    assert saved.status_code == 200 and saved.json()["revision"] == current["revision"] + 1
    stale = await client.put("/api/external/profiles", headers=http.admin_headers, json={
        "expected_revision": current["revision"], "profiles": profiles})
    assert (stale.status_code, stale.json()["detail"]["code"]) == (409, "stale_revision")
    # The whole-settings document never writes the catalog.
    document = (await client.get("/api/settings", headers=http.admin_headers)).json()
    document["external_delivery"] = {"profiles": [], "revision": 0}
    put = await client.put("/api/settings", headers=http.admin_headers, json=document)
    assert put.status_code == 200, put.text
    assert config.get_settings().external_delivery.revision == current["revision"] + 1
    assert config.get_settings().external_delivery.by_id("tv").retention_hours == 24
