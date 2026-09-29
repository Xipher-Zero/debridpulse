"""DP 1.0.13 adverse multi-source convergence, Pass C: aria2 terminal truth and
bounded recovery.

aria2 1.37.0 (characterized with DP's shipped daemon options): an immediately
failing job is accepted -- ``addUri`` returns its GID -- and is terminal within
milliseconds (a refused port, a wrong host key, an anonymous login on an SSH
server), with its native code and message kept ONLY in the daemon's bounded
stopped-result FIFO (``max-download-result``). ``tellStatus`` answers from that
history; once the result is evicted the GID is simply "not found". So the one
observation owner confirms admission before it returns (a short bounded read
through ``observe``), and ABSENT keeps meaning exactly "no live execution and no
terminal record exists".

A current-generation attempt this engine admitted that then disappears with no
progress is a failure of the selected execution path: it consumes the ordinary
bounded no-progress budget, so repeated disappearance reaches the verified
alternate. A historical handle found missing by startup reconciliation stays
infrastructure reconciliation and consumes nothing.
"""
from __future__ import annotations

import pytest

import db.database as database
from continuation_fakes import MIB
from executors.aria2.client import Aria2DownloadStatus
from fake_integrations import VaultExecutor, VaultProvider
from test_aria2_executor_contract import NativeDaemon, execution  # noqa: F401  (fixture)
from test_input_required_lifecycle import base  # noqa: F401  (fixture)
from test_v113_material_continuation import admit, attach_alternate, build, checkpoint
from transfers.convergence_engine import TransferEngine
from transfers.models import ExecutionState, InputOrigin, TransferRequest
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

TWO = (("spool-a", "spoola"), ("spool-b", "spoolb"))
SSH_REFUSED = "SSH authentication failure: Authentication failed (username/password)"


class DyingDaemon(NativeDaemon):
    """aria2's accepted-then-terminal job: ``addUri`` returns the GID and the
    job is already ``error`` (code 1 with its native message), exactly as the
    real daemon reports an SSH login refusal. ``evict`` models the bounded
    stopped-result FIFO dropping it before anyone observed it."""

    async def _call(self, method, params):
        gid = await super()._call(method, params)
        if method == "aria2.addUri":
            job = self.jobs[params[1]["gid"]]
            self.jobs[job.gid] = Aria2DownloadStatus(job.gid, "error", 0, 0, 0, files=job.files,
                                                     error_code="1", error_message=SSH_REFUSED)
        return gid

    def evict(self, gid):
        self.jobs.pop(gid, None)


# ── C1: terminal history beats ABSENT ───────────────────────────────────────

async def test_c1_an_accepted_job_that_died_at_once_reports_its_native_terminal_failure(execution):
    daemon = DyingDaemon()
    execution.executor.client = daemon
    started = await execution.executor.start(execution.request, execution.handle)
    # The terminal result is later evicted from the bounded history: the start
    # itself already carried the native truth, so nothing is left to be lost.
    daemon.evict(execution.handle.native["gid"])
    assert started.state == ExecutionState.FAILED, "the native terminal cause was lost behind an accepted start"
    assert started.error.native_code == "1"
    assert "SSH authentication failure" in started.error.diagnostic


async def test_c1_a_terminal_result_still_in_history_is_observed_as_the_native_failure(execution):
    daemon = DyingDaemon()
    execution.executor.client = daemon
    await execution.executor.start(execution.request, execution.handle)
    observed = await execution.executor.observe(execution.handle)
    assert observed.state == ExecutionState.FAILED and observed.error.native_code == "1"


# ── C2: a true absence stays ABSENT ─────────────────────────────────────────

async def test_c2_no_live_job_and_no_terminal_record_is_absent(execution):
    assert (await execution.executor.observe(execution.handle)).state == ExecutionState.ABSENT
    await execution.executor.start(execution.request, execution.handle)
    execution.daemon.jobs.clear()  # neither live nor in the stopped-result history
    assert (await execution.executor.observe(execution.handle)).state == ExecutionState.ABSENT


# ── C3: a fresh owned disappearance counts ──────────────────────────────────

async def _context(ctx, artifact_id):
    return await ctx.repository.recovery_context(artifact_id)


async def test_c3_a_fresh_owned_attempt_that_vanishes_advances_the_no_progress_streak(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    transfer, artifact = await admit(ctx)
    ctx.spools["spool-a"].jobs.pop(artifact.execution.attempt_id)  # admitted, then gone with no progress
    await checkpoint(ctx)
    context = await _context(ctx, artifact.id)
    assert int(context.get("consecutive_no_progress_failures") or 0) == 1
    assert context.get("failure_signature")


# ── C4: a stale startup orphan is not a source failure ──────────────────────

async def _restarted(ctx, tmp_path):
    from continuation_fakes import SpoolExecutor, SpoolProvider
    repository, registry = TransferRepository(), IntegrationRegistry()
    spools = {}
    for identity, scheme in (("spool-a", "spoola"),):
        registry.register_provider(SpoolProvider("src-" + identity, scheme, ctx.sources))
        spools[identity] = SpoolExecutor(repository.authorize_execution, ctx.sources, identity=identity, scheme=scheme)
        registry.register_executor(spools[identity])
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"), policy=TransferPolicy(
        retry_delay=0, adoption_stability_seconds=0, max_active_executions=4), clock=lambda: ctx.clock[0])
    await engine.initialize()
    return repository, engine, spools


async def test_c4_a_historical_handle_missing_at_startup_consumes_no_source_budget(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    _transfer, artifact = await admit(ctx)
    repository, engine, _spools = await _restarted(ctx, tmp_path)  # the daemon's jobs did not survive
    ctx.clock[0] += 6
    await engine.reconcile_executions()
    context = await repository.recovery_context(artifact.id)
    assert int(context.get("consecutive_no_progress_failures") or 0) == 0
    assert int(context.get("same_signature_failures") or 0) == 0


async def test_c4_a_historical_handle_first_unknown_then_missing_consumes_no_source_budget(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch)
    _transfer, artifact = await admit(ctx)
    repository, engine, spools = await _restarted(ctx, tmp_path)
    original = spools["spool-a"].observe_many

    async def unavailable(handles):
        from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
        from transfers.models import ExecutionSnapshot
        return ExecutionSnapshot((), NormalizedError(Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE,
                                                     Stage.RECONCILIATION, retryability=Retryability.BACKOFF))
    spools["spool-a"].observe_many = unavailable  # the daemon is still starting at startup
    ctx.clock[0] += 6
    await engine.reconcile_executions()
    spools["spool-a"].observe_many = original      # ...and then reports the job gone
    ctx.clock[0] += 6
    await engine.reconcile_executions()
    context = await repository.recovery_context(artifact.id)
    assert int(context.get("consecutive_no_progress_failures") or 0) == 0


# ── C5: repeated fresh failure fails over ───────────────────────────────────

async def test_c5_repeated_zero_progress_disappearance_selects_the_verified_alternate(tmp_path, monkeypatch):
    ctx = await build(tmp_path, monkeypatch, executors=TWO)
    transfer, first = await admit(ctx)
    await attach_alternate(ctx, transfer)
    spool_a, spool_b = ctx.spools["spool-a"], ctx.spools["spool-b"]
    spool_a.step(first.execution.attempt_id, 4 * MIB + 7)
    await checkpoint(ctx)
    before = await ctx.repository.material_state(first.id)
    assert before.valid  # DP-valid material exists before the route starts vanishing
    vanished = []
    for _ in range(12):
        current = (await ctx.repository.artifacts(transfer.id))[0]
        if current.execution is not None and current.execution.executor_id == "spool-b":
            break
        if current.execution is not None and current.execution.attempt_id in spool_a.jobs:
            spool_a.jobs.pop(current.execution.attempt_id)  # admitted, then gone with no further progress
            vanished.append(current.execution.attempt_id)
        await checkpoint(ctx, seconds=400)
    current = (await ctx.repository.artifacts(transfer.id))[0]
    # Bounded: never an indefinite Waiting for Retry on the vanishing route.
    assert current.execution is not None and current.execution.executor_id == "spool-b", \
        f"still retrying the vanishing route after {len(vanished)} disappearances"
    assert len(vanished) <= 3
    after = await ctx.repository.material_state(first.id)
    # Every DP-valid range survives the failover; the material generation is unchanged.
    assert after.valid == before.valid and after.material_generation == before.material_generation
    plan = spool_b.plans[-1]
    assert plan.retained == before.valid and plan.discarded == ()


class VanishingVault(VaultExecutor):
    """The open route's writer is admitted and then disappears; the locked
    route's writer (the verified alternate) works."""

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.vanishing = "open.example"

    async def start(self, request, handle):
        observed = await super().start(request, handle)
        if self._object(request.work.subject.candidate).startswith(self.vanishing + "/"):
            self.jobs.pop(handle.attempt_id, None)
        return observed


async def test_c5_failover_to_an_alternate_with_accepted_access_asks_no_credential(base):
    repository, registry, engine, now = base
    registry.register_provider(VaultProvider())
    vault = VanishingVault(repository.authorize_execution, objects={
        "open.example/item.bin": b"same", "locked.example/item.bin": b"same"},
        locks={"locked.example": ("locked-user", "locked-secret")})
    registry.register_executor(vault)
    canonical = await engine.submit((TransferRequest("vault", "open.example/item.bin", name="item.bin"),),
                                    deduplicate=False)
    vault.vanishing = ""  # the canonical's first writer is healthy while the alternate is proven
    for _ in range(2):
        now[0] += 5
        await engine.tick()
    incoming = await engine.submit((TransferRequest("vault", "locked.example/item.bin", name="item.bin"),),
                                   deduplicate=False)
    for _ in range(3):
        now[0] += 5
        await engine.tick()
    challenge = await engine.challenges.current(incoming.id)
    assert challenge is not None and challenge.origin == InputOrigin.EVIDENCE
    await engine.submit_input(incoming.id, challenge.id, "username_password",
                              {"username": "locked-user", "password": "locked-secret"})
    for _ in range(4):
        now[0] += 5
        await engine.tick()
    (artifact,) = await repository.artifacts(canonical.id)
    assert len(artifact.candidates) == 2  # the authenticated alternate was adopted
    async with database.get_db() as db:
        asked_before = (await db.fetchone("SELECT COUNT(*) AS n FROM application_events WHERE kind='input_required'"))["n"]
    # From now on every writer of the open route is admitted and then vanishes.
    vault.vanishing = "open.example"
    vault.jobs.pop(artifact.execution.attempt_id, None)
    for _ in range(12):
        now[0] += 400
        await engine.tick()
        current = (await repository.artifacts(canonical.id))[0]
        if current.execution is not None and "locked.example" in \
                current.candidates[current.selected].endpoints[0].address:
            break
    current = (await repository.artifacts(canonical.id))[0]
    assert "locked.example" in current.candidates[current.selected].endpoints[0].address, \
        "the vanishing route was retried indefinitely instead of failing over"
    # The alternate's accepted access was reused: no new question anywhere.
    async with database.get_db() as db:
        asked_after = (await db.fetchone("SELECT COUNT(*) AS n FROM application_events WHERE kind='input_required'"))["n"]
    assert asked_after == asked_before
    assert await engine.challenges.current(canonical.id) is None
    assert [user for _candidate, user in vault.input_starts] == ["locked-user"]
