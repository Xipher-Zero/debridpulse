"""1.0.13: aria2's explicit refusal of a start is a definitive FAILED start.

Transfer 456: every direct-SFTP writer was refused by the daemon itself --
aria2 1.37.0 answers ``aria2.addUri`` for ``sftp://HOST:/path`` with the
JSON-RPC error ``{"code": 1, "message": "No URI to download."}`` and creates no
job. The executor reported that answer as an uncertain acknowledgement
(UNKNOWN), which core rightly never records as admitted; the next observation
found the GID ABSENT, a disappearance nobody admitted, so recovery treated it
as reconciliation and never consumed the source budget: transient backoff on
the same source, forever.

The one correction is at the executor's native submission: a daemon that
answered with its own error admitted nothing, so the start is FAILED with the
sanitized native evidence and core's ordinary failure accounting applies.
What cannot prove the answer -- a timeout, a lost connection, a malformed or
lost response -- stays UNKNOWN, and UNKNOWN is still never inferred owned from
a later ABSENT.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

import executors.aria2.client as client_module
from continuation_fakes import MIB
from execution_requests import file_request
from executors.aria2.client import Aria2ConnectionError, Aria2RPCError, Aria2Service
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from executors.aria2.translation import exception_failure
from test_aria2_executor_contract import NativeDaemon
from test_v113_ftp_sftp_convergence_runtime import _free_port, _start_aria2
from test_v113_material_continuation import admit, attach_alternate, build, checkpoint
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.models import Endpoint, ExecutionObservation, ExecutionState, InputMethod, TransferCandidate

pytestmark = pytest.mark.asyncio

TWO = (("spool-a", "spoola"), ("spool-b", "spoolb"))
# A persisted pre-canonicalization direct-SFTP candidate: exactly what aria2 refuses.
REFUSED_URI = "sftp://192.0.2.9:/srv/iso/image.iso"


def _executor(tmp_path, client, monkeypatch):
    async def validated(address, **_kwargs):
        return address
    monkeypatch.setattr("executors.aria2.executor.validate_resolved_public_destination", validated)
    grants = {}

    async def authorize(handle, action):
        return grants.get(handle.attempt_id) == handle

    egress = SimpleNamespace(ensure_started=AsyncMock(), job_options=lambda *_a, **_k: {})
    executor = Aria2Executor(client, Aria2Configuration(str(tmp_path), confirmation_delay=0,
                                                        control_confirmation_timeout=0.2),
                             authorize, egress=egress)
    candidate = TransferCandidate("image.iso", (Endpoint("sftp", REFUSED_URI),), expected_bytes=100,
                                  accepted_input_methods=(InputMethod.USERNAME_PASSWORD,))
    request = file_request(candidate, str(tmp_path / "image.iso"), "refused-attempt", root=tmp_path)
    handle = executor.prepare(request)
    grants[handle.attempt_id] = handle
    return executor, request, handle


# ── the executor boundary, against the real daemon ──────────────────────────

@pytest.mark.real_runtime
async def test_a_real_daemon_refusal_of_add_uri_is_a_definitive_failed_start(tmp_path, monkeypatch):
    proc, service = await _start_aria2(tmp_path)
    try:
        executor, request, handle = _executor(tmp_path, service, monkeypatch)
        started = await executor.start(request, handle)
        assert started.state == ExecutionState.FAILED, "aria2's own refusal was reported as an uncertain start"
        # The normal normalized failure, with the sanitized native evidence kept.
        assert started.error.category == Category.UNMAPPED_EXECUTOR_ERROR
        assert started.error.domain == Domain.EXECUTOR and started.error.stage == Stage.QUEUE
        assert started.error.native_code == "1"
        assert "No URI to download." in started.error.diagnostic
        assert "192.0.2.9" not in started.error.diagnostic
        # Nothing was admitted: the daemon holds no job for this GID.
        with pytest.raises(Aria2RPCError, match="is not found"):
            await service.tell_status(executor._handle_gid(handle))
        assert await service._call("aria2.tellActive", [["gid"]]) == []
        assert await service._call("aria2.tellStopped", [0, 10, ["gid"]]) == []
    finally:
        proc.kill()
        await proc.wait()


@pytest.mark.real_runtime
async def test_the_client_types_only_the_daemons_own_error_answer_as_a_refusal(tmp_path):
    proc, service = await _start_aria2(tmp_path)
    try:
        with pytest.raises(client_module.Aria2ResponseError) as refused:
            await service._call("aria2.addUri", [[REFUSED_URI], {"gid": "00000000000000aa"}])
        assert refused.value.code == 1 and "No URI to download." in str(refused.value)
    finally:
        proc.kill()
        await proc.wait()
    closed = Aria2Service(f"http://127.0.0.1:{_free_port()}/jsonrpc", "secret", 3)
    with pytest.raises(Aria2ConnectionError) as lost:
        await closed._call("aria2.addUri", [[REFUSED_URI], {}])
    assert not isinstance(lost.value, client_module.Aria2ResponseError)


MALFORMED = ['{"jsonrpc": "2.0", "id": "1", "error": null}', '{"jsonrpc": "2.0", "id": "1", "error": "refused?"}',
             '<html>502 Bad Gateway</html>', '']


async def _stub(body):
    """A JSON-RPC endpoint that answers every call with ``body`` verbatim."""
    async def handler(_request):
        return web.Response(text=body, content_type="application/json")
    app = web.Application()
    app.router.add_post("/jsonrpc", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = _free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, Aria2Service(f"http://127.0.0.1:{port}/jsonrpc", "secret", 3)


@pytest.mark.parametrize("body", MALFORMED, ids=["error_null", "error_string", "not_json", "empty"])
async def test_a_malformed_answer_is_not_a_refusal(body):
    runner, service = await _stub(body)
    try:
        with pytest.raises(Exception) as raised:
            await service._call("aria2.addUri", [[REFUSED_URI], {}])
        assert not isinstance(raised.value, client_module.Aria2ResponseError)
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("body", MALFORMED, ids=["error_null", "error_string", "not_json", "empty"])
async def test_a_start_whose_answer_is_malformed_stays_unknown(tmp_path, monkeypatch, body):
    runner, service = await _stub(body)
    try:
        executor, request, handle = _executor(tmp_path, service, monkeypatch)
        # The collision pre-check needs a GID-not-found answer first.
        monkeypatch.setattr(service, "tell_status", NativeDaemon().tell_status)
        assert (await executor.start(request, handle)).state == ExecutionState.UNKNOWN
    finally:
        await runner.cleanup()


class UncertainDaemon(NativeDaemon):
    """``addUri`` whose answer DP cannot prove: ``failure`` is raised instead."""

    def __init__(self, failure):
        super().__init__()
        self.failure = failure

    async def _call(self, method, params):
        if method == "aria2.addUri":
            raise self.failure
        return await super()._call(method, params)


@pytest.mark.parametrize("failure", [
    asyncio.TimeoutError(),
    Aria2ConnectionError("Connection to aria2 lost: reset"),
    Aria2RPCError("Network error communicating with aria2: truncated"),
], ids=["timeout", "connection_lost", "network_error"])
async def test_an_unproven_add_uri_answer_stays_unknown(tmp_path, monkeypatch, failure):
    executor, request, handle = _executor(tmp_path, UncertainDaemon(failure), monkeypatch)
    started = await executor.start(request, handle)
    assert started.state == ExecutionState.UNKNOWN


async def test_a_lost_acknowledgement_after_acceptance_stays_unknown(tmp_path, monkeypatch):
    daemon = NativeDaemon()
    daemon.lost_ack = True  # the job exists; the answer was lost
    executor, request, handle = _executor(tmp_path, daemon, monkeypatch)
    assert (await executor.start(request, handle)).state == ExecutionState.UNKNOWN


# ── core: what the executor now reports, through the ordinary path ──────────

def _refused(handle):
    """Exactly the observation Aria2Executor returns for the daemon's refusal
    (the normalization of the refusal is the ordinary one)."""
    error = exception_failure(Aria2RPCError("aria2 [1]: No URI to download.", code=1), stage=Stage.QUEUE)
    return ExecutionObservation(handle, ExecutionState.FAILED, error=error)


def _uncertain(handle):
    return ExecutionObservation(handle, ExecutionState.UNKNOWN, error=NormalizedError(
        Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE, Stage.QUEUE, retryability=Retryability.BACKOFF))


async def _vanishing_route(tmp_path, monkeypatch, start):
    """A canonical artifact with DP-valid sparse material on route A and a
    verified alternate B; from now on every start on A answers ``start``."""
    ctx = await build(tmp_path, monkeypatch, executors=TWO)
    transfer, first = await admit(ctx)
    await attach_alternate(ctx, transfer)
    spool_a = ctx.spools["spool-a"]
    spool_a.step(first.execution.attempt_id, 4 * MIB + 7)
    await checkpoint(ctx)
    before = await ctx.repository.material_state(first.id)
    assert before.valid
    starts = []

    async def answering(request, handle):
        starts.append(handle.attempt_id)
        return start(handle)
    monkeypatch.setattr(spool_a, "start", answering)
    current = (await ctx.repository.artifacts(transfer.id))[0]
    spool_a.jobs.pop(current.execution.attempt_id)  # the healthy writer disappears once
    return ctx, transfer, before, starts


async def _rounds(ctx, transfer, count):
    for _ in range(count):
        await checkpoint(ctx, seconds=400)
        current = (await ctx.repository.artifacts(transfer.id))[0]
        if current.execution is not None and current.execution.executor_id == "spool-b":
            return current
    return (await ctx.repository.artifacts(transfer.id))[0]


async def test_a_definitive_failed_start_is_counted_and_reaches_the_verified_alternate(tmp_path, monkeypatch):
    ctx, transfer, before, starts = await _vanishing_route(tmp_path, monkeypatch, _refused)
    current = await _rounds(ctx, transfer, 12)
    assert current.execution is not None and current.execution.executor_id == "spool-b", \
        f"still retrying the refusing route after {len(starts)} refused starts"
    assert 1 <= len(starts) <= ctx.engine.policy.same_candidate_no_progress_limit
    after = await ctx.repository.material_state(current.id)
    # Refused zero-byte starts promote nothing and discard nothing.
    assert after.valid == before.valid and after.material_generation == before.material_generation
    plan = ctx.spools["spool-b"].plans[-1]
    assert plan.retained == before.valid and plan.discarded == ()


async def test_an_uncertain_start_is_never_admitted_nor_owned_from_a_later_absence(tmp_path, monkeypatch):
    ctx, transfer, _before, starts = await _vanishing_route(tmp_path, monkeypatch, _uncertain)
    await _rounds(ctx, transfer, 6)
    assert starts, "no start was attempted"
    assert not set(starts) & ctx.engine._admitted_executions
    context = await ctx.repository.recovery_context((await ctx.repository.artifacts(transfer.id))[0].id)
    # The first (admitted, healthy) writer's disappearance is the only counted failure.
    assert int(context.get("consecutive_no_progress_failures") or 0) <= 1
    assert int(context.get("same_signature_failures") or 0) <= 1


async def test_an_accepted_start_that_then_vanishes_still_follows_owned_disappearance(tmp_path, monkeypatch):
    def accepted_then_gone(handle):
        return ExecutionObservation(handle, ExecutionState.QUEUED)  # accepted; the job never exists again
    ctx, transfer, before, starts = await _vanishing_route(tmp_path, monkeypatch, accepted_then_gone)
    current = await _rounds(ctx, transfer, 12)
    assert current.execution is not None and current.execution.executor_id == "spool-b"
    assert 1 <= len(starts) <= ctx.engine.policy.same_candidate_no_progress_limit
    after = await ctx.repository.material_state(current.id)
    assert after.valid == before.valid and after.material_generation == before.material_generation


# ── durable attempt truth: a later absence never erases a proven failure ────

async def _attempt_rows(attempt_ids):
    import db.database as database
    from transfers import codec
    async with database.get_db() as db:
        rows = [await db.fetchone("SELECT id,state,error FROM execution_attempts WHERE id=?", (item,))
                for item in attempt_ids]
    return {row["id"]: (row["state"], codec.load(row["error"]) if row["error"] else None) for row in rows}


async def _audits(transfer_id):
    import db.database as database
    from transfers import codec
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT detail FROM application_events WHERE kind='recovery_audit' AND transfer_id=?"
                                 " ORDER BY id", (transfer_id,))
    return [codec.load(row["detail"]) for row in rows]


async def _failure_outcomes(transfer_id, attempt_ids):
    import db.database as database
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT attempt_id FROM transfer_outcomes WHERE transfer_id=? AND kind='failure'",
                                 (transfer_id,))
    return [row["attempt_id"] for row in rows if row["attempt_id"] in attempt_ids]


async def test_a_refused_start_stays_durably_failed_after_recovery_observes_it_absent(tmp_path, monkeypatch):
    """Route A is refused from its very first start (as the persisted raw
    transfer-456 candidate is); B is the verified alternate."""
    from transfers.models import TransferRequest
    ctx = await build(tmp_path, monkeypatch, executors=TWO)
    spool_a = ctx.spools["spool-a"]
    starts = []

    async def refusing(request, handle):
        starts.append(handle.attempt_id)
        return _refused(handle)  # the daemon refused: no native job ever exists
    monkeypatch.setattr(spool_a, "start", refusing)
    transfer = await ctx.engine.submit((TransferRequest("spool", "movie", name="movie.bin",
                                                        preferred_provider="src-spool-a"),), deduplicate=False)
    await ctx.engine.tick()
    await attach_alternate(ctx, transfer)
    current = await _rounds(ctx, transfer, 12)
    assert current.execution is not None and current.execution.executor_id == "spool-b"
    audits = await _audits(transfer.id)
    # Recovery really re-observed each refused attempt as absent (its runtime evidence)...
    retired = [item.get("last_execution_retirement_reason") for item in audits if item.get("transition") == "application"]
    assert "execution_absent" in retired
    # ...and the durable attempt keeps the proven failure and its native cause.
    rows = await _attempt_rows(starts)
    assert len(rows) == len(starts) >= 2
    for attempt_id, (state, error) in rows.items():
        assert state == "failed", f"{attempt_id[:8]}: proven FAILED was downgraded to {state}"
        assert error is not None and error["native_code"] == "1"
        assert "No URI to download." in error["diagnostic"]
        assert error["category"] == "unmapped_executor_error"
    # Counted exactly once per refused attempt; the existing bounded ladder is unchanged.
    assert len([item for item in audits if item.get("transition") == "source_failure"]) == len(starts)
    decisions = [item.get("reason") for item in audits if item.get("transition") == "decision"]
    assert decisions == ["bounded_same_candidate_retry", "no_progress_alternate"]
    assert sorted(await _failure_outcomes(transfer.id, set(starts))) == sorted(starts)


async def test_an_owned_disappearance_is_still_durably_absent(tmp_path, monkeypatch):
    def accepted_then_gone(handle):
        return ExecutionObservation(handle, ExecutionState.QUEUED)
    ctx, transfer, _before, starts = await _vanishing_route(tmp_path, monkeypatch, accepted_then_gone)
    await _rounds(ctx, transfer, 12)
    rows = await _attempt_rows(starts)
    assert rows and all(state == "absent" and error is None for state, error in rows.values())


async def test_a_historical_handle_missing_at_startup_is_still_durably_absent(tmp_path, monkeypatch):
    from test_v113_aria2_terminal_truth_recovery import _restarted
    ctx = await build(tmp_path, monkeypatch)
    _transfer, artifact = await admit(ctx)
    _repository, engine, _spools = await _restarted(ctx, tmp_path)  # the daemon's jobs did not survive
    ctx.clock[0] += 6
    await engine.reconcile_executions()
    assert (await _attempt_rows([artifact.execution.attempt_id]))[artifact.execution.attempt_id] == ("absent", None)
