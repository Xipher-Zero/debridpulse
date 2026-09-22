"""DP 1.0.12 canonical architecture correction, Workstream A.

Universal materialization execution-admission invariant: no dispatch origin
may reach an executor side effect (``executor.prepare()``, native
resume/start, execution-attempt creation, capacity/retry/failover accounting)
before durably proving the artifact belongs to the CURRENT authorized
materialization generation. ``transfers.repository.TransferRepository
.materialization_authorization`` derives PROCEED / HOLD / STALE from the
existing durable file-selection generation/commitment state this module
already owns -- never a transfer-global mutable ``selection_authorized`` flag.
The three dispatch entry points enforce it: ``_engine_base._dispatch``
(before ``executor.prepare()`` AND revalidated immediately before
``executor.start()``), ``_engine_base._converge_execution``'s PAUSED->resume
branch, and ``convergence_engine._dispatch_claimed``'s "existing execution ==
already fine" shortcut.

Existing coverage this file does not duplicate: the full selection-lifecycle
timing/gate contract (``test_file_selection_lifecycle.py``), the Confirm/Close
API (``test_file_selection_api.py``, ``test_file_selection_contract.py``),
executor independence (``test_file_selection_executor_independence.py``), and
recovery-claim concurrency (``test_recovery_command_concurrency.py``). This
file adds the repository-owned admission decision itself and proves the three
dispatch entry points enforce it, including the restart-with-stale-pre-fix-
execution and selection-vs-dispatch-race acceptance scenarios (specification
section 12.1).
"""
from __future__ import annotations

from dataclasses import replace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider, neutral_facts
from file_selection_support import executable, rebind_resource, seed_window
from transfers.convergence_engine import TransferEngine
from transfers.errors import NormalizedError
from transfers.models import (
    ExecutionObservation,
    Artifact, Capability, ExecutionFootprint, ExecutionHandle, ExecutionObservation, ExecutionSnapshot,
    ExecutionState, ExecutorCapabilities, ExecutorClaim, ExecutorHealth, IntegrationDescriptor,
    MaterializationAdmission, MaterializationAdmissionKind, OutcomeKind, TransferOutcome, TransferProgress,
    TransferRequest,
)
from transfers.policy import TransferPolicy
from transfers.recovery_execution import RecoveryTrigger
from transfers.recovery_repository import TransferRepository as RecoveryTransferRepository
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


@pytest_asyncio.fixture
async def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "admission.db")
    await database.init_db()
    return TransferRepository()


def _artifact(transfer_id, request_id, *, artifact_id=1, execution=None, state="queued"):
    return Artifact(
        id=artifact_id, transfer_id=transfer_id, request_id=request_id,
        name="payload.bin", target="/tmp/payload.bin", expected_bytes=4,
        state=state, candidates=(), selected=0, execution=execution,
    )


async def _child_request_id(transfer_id: int, parent_id: str) -> str:
    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT id FROM transfer_requests WHERE transfer_id=? AND parent_id=? ORDER BY ordinal LIMIT 1",
            (transfer_id, parent_id),
        )
    assert row is not None
    return row["id"]


# --------------------------------------------------------------------------- #
# Repository-owned admission decision
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_root_artifact_never_gated_by_a_selection_generation(repo):
    """A root request that never fans out through ``manifest()`` (ordinary
    non-interactive materialization) is never selection-gated -- PROCEED with
    no generation, regardless of whether an unrelated selection row exists
    elsewhere for the transfer."""
    seed = await seed_window(transfer_hash="a" * 40)
    admission = await repo.materialization_authorization(_artifact(seed.transfer_id, seed.request_id))
    assert admission == MaterializationAdmission(MaterializationAdmissionKind.PROCEED)


@pytest.mark.asyncio
async def test_child_bound_to_uncommitted_generation_is_hold(repo):
    """Specification section 7.2 step 5: interactive commitment incomplete ->
    HOLD. (Ordinary new-code dispatch never reaches this: a child is only
    ever created by ``manifest()`` AFTER commit. This proves the repository
    decision itself is correct defense-in-depth for a legacy/anomalous row.)"""
    seed = await seed_window(transfer_hash="b" * 40)
    await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=1000.0,
    )
    async with database.get_db() as db:
        generation = await db.fetchone(
            "SELECT id FROM transfer_file_selections WHERE request_id=?", (seed.request_id,),
        )
        await db.execute(
            "INSERT INTO transfer_requests(id,transfer_id,parent_id,ordinal,payload,materialized_selection_id) "
            "VALUES(?,?,?,?,?,?)",
            ("child-1", seed.transfer_id, seed.request_id, 0, "{}", generation["id"]),
        )
        await db.commit()
    admission = await repo.materialization_authorization(_artifact(seed.transfer_id, "child-1"))
    assert admission.kind == MaterializationAdmissionKind.HOLD
    assert admission.authority_generation == generation["id"]


@pytest.mark.asyncio
async def test_committed_child_of_current_generation_is_proceed(repo):
    seed = await seed_window(transfer_hash="c" * 40)
    await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=1000.0,
    )
    entries = executable(("a", "a", 1), ("b", "b", 2))
    result = await repo.commit_selected_manifest(seed.record, entries, now=1000.0)
    await repo.manifest(seed.record, result, selection_id=result.selection_id)
    child_id = await _child_request_id(seed.transfer_id, seed.request_id)
    admission = await repo.materialization_authorization(_artifact(seed.transfer_id, child_id))
    assert admission == MaterializationAdmission(MaterializationAdmissionKind.PROCEED, result.selection_id)


@pytest.mark.asyncio
async def test_resource_re_resolution_supersedes_prior_generation_as_stale(repo):
    """Acceptance test D (specification 12.1.D): generation A's committed
    authorization must not authorize generation B after the same durable
    request re-resolves onto a new provider resource. A child materialized
    under generation A becomes STALE once generation B exists, EVEN THOUGH
    the root request's mutable ``resource`` column has since been overwritten
    -- proving the durable ``materialized_selection_id`` stamp, not a
    re-derivation from the root's current resource, is what decides this.

    This proves the case where generation A's materialized artifact has NO
    live execution attached (never dispatched, or already terminal): the
    repository may safely detach/advance immediately. The case where a LIVE
    (non-terminal) execution is attached -- which must retire through the
    real executor via the existing canonical STALE-dispatch machinery, never
    a blind repository-level pointer swap -- is covered end-to-end against
    the production engine/executor stack by
    ``test_live_generation_a_execution_is_retired_through_the_executor_before_generation_b_reconstructs``."""
    seed = await seed_window(transfer_hash="d" * 40)
    await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=1000.0,
    )
    entries = executable(("a", "a", 1))
    generation_a = await repo.commit_selected_manifest(seed.record, entries, now=1000.0)
    await repo.manifest(seed.record, generation_a, selection_id=generation_a.selection_id)
    child_id = await _child_request_id(seed.transfer_id, seed.request_id)

    proceed = await repo.materialization_authorization(_artifact(seed.transfer_id, child_id))
    assert proceed.kind == MaterializationAdmissionKind.PROCEED

    # A materialized artifact under generation A with NO live execution
    # (queued, never dispatched).
    from transfers.models import RequestRecord, TransferCandidate, Endpoint

    child_record = RequestRecord(child_id, seed.transfer_id, entries[0].request, "resolved")
    candidate = TransferCandidate("a", (Endpoint("memory", "memory:a"),), expected_bytes=1, provider_id="parcel-lab")
    artifact_a = await repo.materialize(child_record, (candidate,), "/tmp/payload-a.bin")

    rebound = await rebind_resource(seed, suffix="regen")
    await repo.begin_file_selection_window(
        rebound.request_id, rebound.transfer_id, rebound.provider_resource_id, rebound.provider_id,
        initially_available=True, now=2000.0,
    )
    generation_b = await repo.commit_selected_manifest(rebound.record, entries, now=2000.0)
    assert generation_b.selection_id != generation_a.selection_id

    stale = await repo.materialization_authorization(_artifact(seed.transfer_id, child_id))
    assert stale.kind == MaterializationAdmissionKind.STALE
    assert stale.authority_generation == generation_b.selection_id

    # Ordinary reconstruction: the engine calls ``manifest()`` again for the
    # SAME reused path once generation B is the root's current authority.
    # Nothing live is attached, so this is safe to advance immediately.
    await repo.manifest(rebound.record, entries, selection_id=generation_b.selection_id)

    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT status, execution_attempt_id, materialized_selection_id AS stamp FROM download_files "
            "JOIN transfer_requests ON transfer_requests.id = download_files.request_id "
            "WHERE download_files.id=?", (artifact_a.id,),
        )
    assert row["status"] == "unresolved"
    assert row["execution_attempt_id"] is None
    assert row["stamp"] == generation_b.selection_id

    reconstructed = await repo.materialization_authorization(_artifact(seed.transfer_id, child_id))
    assert reconstructed.kind == MaterializationAdmissionKind.PROCEED
    assert reconstructed.authority_generation == generation_b.selection_id


@pytest.mark.asyncio
async def test_legacy_row_without_stamp_falls_back_to_root_resource_lookup(repo):
    """A child created before ``materialized_selection_id`` existed (NULL) is
    still evaluated correctly for the common case via the root's currently
    bound resource -- specification section 12.1.F's "restart with stale
    pre-fix execution" is exactly this shape of row."""
    seed = await seed_window(transfer_hash="e" * 40)
    await repo.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=1000.0,
    )
    # Production resolution always writes the root's own resolved resource
    # onto ``transfer_requests.resource`` (``TransferRepository.resolution``);
    # ``seed_window`` only seeds ``provider_resources``, so mirror that one
    # write here for the fallback lookup to have anything to read.
    from transfers import codec
    async with database.get_db() as db:
        await db.execute(
            "UPDATE transfer_requests SET resource=? WHERE id=?",
            (codec.dump(seed.resource), seed.request_id),
        )
        await db.commit()

    entries = executable(("a", "a", 1))
    generation = await repo.commit_selected_manifest(seed.record, entries, now=1000.0)
    # Materialize WITHOUT the stamp, simulating a pre-migration row.
    await repo.manifest(seed.record, generation, selection_id=None)
    child_id = await _child_request_id(seed.transfer_id, seed.request_id)
    async with database.get_db() as db:
        row = await db.fetchone("SELECT materialized_selection_id FROM transfer_requests WHERE id=?", (child_id,))
    assert row["materialized_selection_id"] is None

    admission = await repo.materialization_authorization(_artifact(seed.transfer_id, child_id))
    assert admission.kind == MaterializationAdmissionKind.PROCEED
    assert admission.authority_generation == generation.selection_id


# --------------------------------------------------------------------------- #
# Enforcement at the three dispatch entry points (production stack:
# transfers.convergence_engine.TransferEngine + transfers.recovery_repository
# .TransferRepository, the same stack production runs -- matching
# tests/production_stack_harness.py's rationale that a regression in the
# claim/dispatch layers is invisible to the lower, isolated transfers.engine
# stack).
# --------------------------------------------------------------------------- #

async def _build_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "admission-engine.db")
    await database.init_db()
    repository = RecoveryTransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider("engine-lab")
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0,
                              max_active_executions=8, resolution_concurrency=8),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return engine, repository, provider, executor


@pytest_asyncio.fixture
async def engine_stack(tmp_path, monkeypatch):
    return await _build_engine(tmp_path, monkeypatch)


def _forced_admission(kind, generation="forced-generation"):
    async def _admission(_artifact):
        return MaterializationAdmission(kind, authority_generation=generation)
    return _admission


@pytest.mark.asyncio
async def test_dispatch_never_reaches_executor_under_hold(engine_stack, monkeypatch):
    """Specification section 7.6: HOLD causes no execution attempt, no
    executor.prepare()/start(), no retry consumption, no error."""
    engine, repository, provider, executor = engine_stack
    transfer = await engine.submit((TransferRequest("parcel", "p1", name="payload.bin"),), deduplicate=False)
    await engine.resolve_pending()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.state == "queued" and artifact.execution is None

    monkeypatch.setattr(repository, "materialization_authorization", _forced_admission(MaterializationAdmissionKind.HOLD))
    await engine.reconcile_executions()

    assert executor.calls == []
    refreshed = (await repository.artifacts(transfer.id))[0]
    assert refreshed.state == "queued"
    assert refreshed.execution is None
    assert refreshed.error is None
    assert refreshed.retries == 0


@pytest.mark.asyncio
async def test_dispatch_retires_and_requeues_under_stale(engine_stack, monkeypatch):
    """Specification section 7.5: STALE retires through existing canonical
    machinery (never resumed/reused) and reconstructs via ordinary
    re-resolution -- never a new selection-specific side path."""
    engine, repository, provider, executor = engine_stack
    transfer = await engine.submit((TransferRequest("parcel", "p1", name="payload.bin"),), deduplicate=False)
    await engine.resolve_pending()
    artifact = (await repository.artifacts(transfer.id))[0]

    monkeypatch.setattr(repository, "materialization_authorization", _forced_admission(MaterializationAdmissionKind.STALE))
    await engine.reconcile_executions()

    assert executor.calls == []
    refreshed = (await repository.artifacts(transfer.id))[0]
    assert refreshed.state == "unresolved"
    async with database.get_db() as db:
        row = await db.fetchone("SELECT state FROM transfer_requests WHERE id=?", (artifact.request_id,))
    assert row["state"] == "pending"


@pytest.mark.asyncio
async def test_toctou_revalidation_blocks_native_start_after_hold_transition(engine_stack, monkeypatch):
    """Specification section 7.4: a HOLD transition between the initial check
    and the irreversible native commitment must still be caught. ``prepare()``
    is a pure local computation (no native side effect); only ``start()``
    actually contacts the executor, so this proves the revalidation inside
    ``_dispatch_lock`` -- not merely the early check -- is load-bearing."""
    engine, repository, provider, executor = engine_stack
    transfer = await engine.submit((TransferRequest("parcel", "p1", name="payload.bin"),), deduplicate=False)
    await engine.resolve_pending()

    calls = {"n": 0}
    real = repository.materialization_authorization

    async def flip_to_hold_on_second_call(artifact):
        calls["n"] += 1
        if calls["n"] == 1:
            return await real(artifact)  # PROCEED (no selection generation applies)
        return MaterializationAdmission(MaterializationAdmissionKind.HOLD, authority_generation="race-generation")

    monkeypatch.setattr(repository, "materialization_authorization", flip_to_hold_on_second_call)
    await engine.reconcile_executions()

    assert calls["n"] >= 2, "both the early check and the pre-commitment revalidation must call admission"
    assert all(call[0] != "start" for call in executor.calls), "native start must never occur once revalidation observes HOLD"
    refreshed = (await repository.artifacts(transfer.id))[0]
    assert refreshed.execution is None


@pytest.mark.asyncio
async def test_paused_execution_does_not_resume_under_hold(engine_stack, monkeypatch):
    """Specification section 7.5: an existing PAUSED execution handle is not
    proof of authorization. Resuming it is a native side effect that must
    stop for HOLD exactly as a fresh dispatch would."""
    engine, repository, provider, executor = engine_stack
    transfer = await engine.submit((TransferRequest("parcel", "p1", name="payload.bin"),), deduplicate=False)
    await engine.resolve_pending()
    await engine.reconcile_executions()
    started = (await repository.artifacts(transfer.id))[0]
    assert started.execution is not None

    await engine.pause(transfer.id)
    await engine.reconcile_executions()
    paused = (await repository.artifacts(transfer.id))[0]
    assert paused.state == "paused"

    resume_calls_before = sum(1 for call in executor.calls if call[0] == "resume")
    monkeypatch.setattr(repository, "materialization_authorization", _forced_admission(MaterializationAdmissionKind.HOLD))
    await engine.resume(transfer.id)
    await engine.reconcile_executions()

    resume_calls_after = sum(1 for call in executor.calls if call[0] == "resume")
    assert resume_calls_after == resume_calls_before, "resume() must never reach the executor while HOLD applies"


@pytest.mark.asyncio
async def test_ordinary_reconciliation_cadence_quiesces_active_writer_when_admission_becomes_hold(engine_stack, monkeypatch):
    """Gate 9 revision-5 rejection finding 1 (specification section 7.5): the
    ORDINARY reconciliation cadence (``engine.reconcile_executions()`` alone,
    never a manually acquired claim) must not leave an already-active native
    writer running untouched merely because HOLD -- unlike STALE -- is not
    itself a retirement decision. ``_reconcile_unauthorized_existing_execution``
    previously left HOLD completely untouched once an execution already
    existed, so the writer kept transferring/producing unauthorized bytes
    indefinitely. HOLD now reuses the SAME pause/park machinery
    (``_park_existing_execution``) every other non-error quiescence category
    (provider-disabled, executor-unavailable, storage-unavailable) already
    uses: the executor supports ``PauseResume``, so the writer is paused --
    not cancelled/retired -- no retry/failure is consumed, and the artifact's
    execution association remains intact so it can resume the instant
    admission reports PROCEED again."""
    engine, repository, provider, executor = engine_stack
    transfer = await engine.submit((TransferRequest("parcel", "p1", name="payload.bin"),), deduplicate=False)
    await engine.resolve_pending()
    await engine.reconcile_executions()
    started = (await repository.artifacts(transfer.id))[0]
    assert started.execution is not None
    assert sum(1 for call in executor.calls if call[0] == "start") == 1
    retries_before = started.retries

    monkeypatch.setattr(repository, "materialization_authorization", _forced_admission(MaterializationAdmissionKind.HOLD))
    cancel_before = sum(1 for call in executor.calls if call[0] == "cancel")

    # Ordinary cadence only -- no claim, no _dispatch_claimed.
    await engine.reconcile_executions()

    # MemoryExecutor.pause()/resume() do not append to ``executor.calls``
    # (only start/observe/cancel do); the durable execution-attempt state is
    # the authoritative proof that the writer was actually quiesced.
    assert sum(1 for call in executor.calls if call[0] == "cancel") == cancel_before, (
        "a pausable executor must be paused, not cancelled/retired"
    )
    async with database.get_db() as db:
        exec_row = await db.fetchone(
            "SELECT state, authorized FROM execution_attempts WHERE id=?",
            (started.execution.attempt_id,),
        )
    assert exec_row["state"] == "paused", "an active writer under HOLD must be quiesced, not left running"
    assert exec_row["authorized"] == 1, "pausing must not deauthorize -- this is waiting, not retirement"
    refreshed = (await repository.artifacts(transfer.id))[0]
    assert refreshed.execution is not None, "the association must remain intact so the writer can resume once authorized"
    # Parking pauses the EXISTING execution attempt rather than creating a
    # new one, so the dispatch-count counter must not move (a fresh dispatch
    # is what increments it, not this).
    assert refreshed.retries == retries_before
    assert refreshed.error is None


@pytest.mark.asyncio
async def test_dispatch_claimed_retires_existing_execution_under_stale(engine_stack, monkeypatch):
    """Specification section 7.5: ``_dispatch_claimed``'s "existing execution
    == already fine" shortcut must not trust a STALE execution. It is
    cancelled and retired through the same cancel-and-reconcile primitives
    ``_park_existing_execution`` already uses, never resumed/reused."""
    from transfers.recovery_execution import RecoveryTrigger

    engine, repository, provider, executor = engine_stack
    transfer = await engine.submit((TransferRequest("parcel", "p1", name="payload.bin"),), deduplicate=False)
    await engine.resolve_pending()
    await engine.reconcile_executions()
    started = (await repository.artifacts(transfer.id))[0]
    assert started.execution is not None
    cancel_calls_before = sum(1 for call in executor.calls if call[0] == "cancel")

    claim = await repository.claim_recovery(started.id, RecoveryTrigger.USER_RETRY, engine.clock())
    assert claim is not None
    monkeypatch.setattr(repository, "materialization_authorization", _forced_admission(MaterializationAdmissionKind.STALE))

    dispatched = await engine._dispatch_claimed(claim, started)

    assert dispatched is False
    cancel_calls_after = sum(1 for call in executor.calls if call[0] == "cancel")
    assert cancel_calls_after == cancel_calls_before + 1
    refreshed = (await repository.artifacts(transfer.id))[0]
    assert refreshed.state == "unresolved"


@pytest.mark.asyncio
async def test_live_generation_a_execution_is_retired_through_the_executor_before_generation_b_reconstructs(engine_stack):
    """Gate 9 revision-3 rejection finding 1 (specification section 7.5): a
    live (non-terminal) execution materialized under a superseded
    file-selection generation must be genuinely cancelled through the
    executor -- exactly once -- and only THEN may the artifact become
    dispatchable again under the new generation. Unlike
    ``test_resource_re_resolution_supersedes_prior_generation_as_stale``
    (which covers the case with no live execution to protect), this proves
    the live-execution case end-to-end against the REAL production engine
    and ``MemoryExecutor`` -- never a manual SQL simulation of cancellation:

    1. generation A's reconstruction attempt while the writer is still live
       must NOT orphan it -- ``manifest()`` must leave the artifact fully
       attached and admission must stay STALE, not silently advance the
       stamp and detach the pointer out from under an authorized native job;
    2. exactly one real ``executor.cancel()`` call retires it, through the
       SAME existing canonical ``_dispatch_claimed``/``_retire_stale_execution``
       machinery already used for this decision elsewhere -- no new
       selection-specific retirement path;
    3. the retired execution is durably unauthorized (``authorized=0``),
       not merely unreferenced -- zero orphan writer;
    4. once retirement is durable, reconstruction under generation B
       produces exactly one fresh execution.
    """
    from transfers.recovery_execution import RecoveryTrigger

    engine, repository, provider, executor = engine_stack
    seed = await seed_window(transfer_hash="f" * 40, provider_id=provider.descriptor.id)
    await repository.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=1000.0,
    )
    entries = executable(("a", "a", 4))
    generation_a = await repository.commit_selected_manifest(seed.record, entries, now=1000.0)
    await repository.manifest(seed.record, generation_a, selection_id=generation_a.selection_id)
    child_id = await _child_request_id(seed.transfer_id, seed.request_id)

    # Real end-to-end materialization + dispatch of generation A's child --
    # a genuine executor-owned native writer, not a hand-inserted DB row.
    await engine.resolve_pending()
    await engine.reconcile_executions()
    artifact_a = next(item for item in await repository.artifacts(seed.transfer_id) if item.request_id == child_id)
    assert artifact_a.execution is not None
    assert sum(1 for call in executor.calls if call[0] == "start") == 1

    # Re-resolution onto a new provider resource: generation B.
    rebound = await rebind_resource(seed, suffix="regen")
    await repository.begin_file_selection_window(
        rebound.request_id, rebound.transfer_id, rebound.provider_resource_id, rebound.provider_id,
        initially_available=True, now=2000.0,
    )
    generation_b = await repository.commit_selected_manifest(rebound.record, entries, now=2000.0)
    assert generation_b.selection_id != generation_a.selection_id

    # (1) Reconstruction attempt while the generation-A writer is STILL
    # live: must not orphan it -- B stays blocked.
    await repository.manifest(rebound.record, entries, selection_id=generation_b.selection_id)
    still_live = next(item for item in await repository.artifacts(seed.transfer_id) if item.request_id == child_id)
    assert still_live.id == artifact_a.id and still_live.execution is not None, "must not orphan the live writer"
    blocked = await repository.materialization_authorization(still_live)
    assert blocked.kind == MaterializationAdmissionKind.STALE
    assert blocked.authority_generation == generation_b.selection_id

    # (2) Genuine retirement through the existing canonical STALE machinery.
    cancel_before = sum(1 for call in executor.calls if call[0] == "cancel")
    claim = await repository.claim_recovery(still_live.id, RecoveryTrigger.USER_RETRY, engine.clock())
    assert claim is not None
    dispatched = await engine._dispatch_claimed(claim, still_live)
    assert dispatched is False
    assert sum(1 for call in executor.calls if call[0] == "cancel") == cancel_before + 1

    retired = next(item for item in await repository.artifacts(seed.transfer_id) if item.request_id == child_id)
    assert retired.state == "unresolved" and retired.execution is None

    # (3) Zero orphan writer: the retired execution is durably unauthorized,
    # not merely unreferenced from the artifact.
    async with database.get_db() as db:
        exec_row = await db.fetchone(
            "SELECT state, authorized FROM execution_attempts WHERE id=?",
            (artifact_a.execution.attempt_id,),
        )
    assert exec_row["state"] == "cancelled"
    assert exec_row["authorized"] == 0

    # (4) Reconstruction: the stamp now safely advances to B (no live
    # execution left to protect), the child is requeued, and dispatch
    # produces exactly ONE fresh execution under generation B.
    await repository.manifest(rebound.record, entries, selection_id=generation_b.selection_id)
    await engine.resolve_pending()
    start_before = sum(1 for call in executor.calls if call[0] == "start")
    await engine.reconcile_executions()
    assert sum(1 for call in executor.calls if call[0] == "start") == start_before + 1

    final = next(item for item in await repository.artifacts(seed.transfer_id) if item.request_id == child_id)
    assert final.execution is not None
    final_admission = await repository.materialization_authorization(final)
    assert final_admission.kind == MaterializationAdmissionKind.PROCEED
    assert final_admission.authority_generation == generation_b.selection_id


async def _generation_a_live_with_stale_generation_b(engine_stack):
    """Shared setup for the ordinary-cadence tests below: dispatch generation
    A for real (genuine executor.start()), then introduce generation B while
    A is still live. Returns everything needed to drive/assert further
    WITHOUT ever manually claiming recovery or calling ``_dispatch_claimed``
    -- the tests using this only ever call ``engine.reconcile_executions()``/
    ``engine.resolve_pending()``, the exact ordinary production cadence."""
    engine, repository, provider, executor = engine_stack
    seed = await seed_window(transfer_hash="1" * 40, provider_id=provider.descriptor.id)
    await repository.begin_file_selection_window(
        seed.request_id, seed.transfer_id, seed.provider_resource_id, seed.provider_id,
        initially_available=True, now=1000.0,
    )
    entries = executable(("a", "a", 4))
    generation_a = await repository.commit_selected_manifest(seed.record, entries, now=1000.0)
    await repository.manifest(seed.record, generation_a, selection_id=generation_a.selection_id)
    child_id = await _child_request_id(seed.transfer_id, seed.request_id)

    await engine.resolve_pending()
    await engine.reconcile_executions()
    artifact_a = next(item for item in await repository.artifacts(seed.transfer_id) if item.request_id == child_id)
    assert artifact_a.execution is not None
    assert sum(1 for call in executor.calls if call[0] == "start") == 1

    rebound = await rebind_resource(seed, suffix="regen")
    await repository.begin_file_selection_window(
        rebound.request_id, rebound.transfer_id, rebound.provider_resource_id, rebound.provider_id,
        initially_available=True, now=2000.0,
    )
    generation_b = await repository.commit_selected_manifest(rebound.record, entries, now=2000.0)
    await repository.manifest(rebound.record, entries, selection_id=generation_b.selection_id)
    still_live = next(item for item in await repository.artifacts(seed.transfer_id) if item.request_id == child_id)
    assert still_live.id == artifact_a.id and still_live.execution is not None, "must not orphan the live writer"

    return engine, repository, executor, seed, rebound, entries, generation_b, artifact_a, child_id


@pytest.mark.asyncio
async def test_ordinary_reconciliation_cadence_retires_stale_generation_a_before_generation_b_starts(engine_stack):
    """Gate 9 revision-4 rejection finding 1 (specification section 7.5):
    unlike ``test_live_generation_a_execution_is_retired_through_the_executor_
    before_generation_b_reconstructs`` (which proves retirement WORKS once
    something routes the artifact through an explicit recovery trigger), this
    proves retirement happens automatically on the ORDINARY scheduler cadence
    -- ``engine.reconcile_executions()`` alone, never a manually acquired
    ``claim_recovery(..., RecoveryTrigger.USER_RETRY)`` nor a direct
    ``_dispatch_claimed()`` call. Generation A is actively "transferring" (no
    error, no candidate/executor problem) when generation B becomes
    authoritative; only the admission check added to ordinary existing-
    execution reconciliation can catch this."""
    engine, repository, executor, seed, rebound, entries, generation_b, artifact_a, child_id = (
        await _generation_a_live_with_stale_generation_b(engine_stack)
    )
    cancel_before = sum(1 for call in executor.calls if call[0] == "cancel")
    start_before = sum(1 for call in executor.calls if call[0] == "start")

    # Ordinary cadence only -- no claim, no _dispatch_claimed.
    await engine.reconcile_executions()

    assert sum(1 for call in executor.calls if call[0] == "cancel") == cancel_before + 1
    retired = next(item for item in await repository.artifacts(seed.transfer_id) if item.request_id == child_id)
    assert retired.state == "unresolved" and retired.execution is None
    async with database.get_db() as db:
        exec_row = await db.fetchone(
            "SELECT state, authorized FROM execution_attempts WHERE id=?", (artifact_a.execution.attempt_id,),
        )
    assert exec_row["state"] == "cancelled"
    assert exec_row["authorized"] == 0
    # Retirement is durable BEFORE generation B has dispatched anything.
    assert sum(1 for call in executor.calls if call[0] == "start") == start_before
    # Gate 9 revision-5 rejection finding 2: a genuinely confirmed
    # retirement (unlike the deferred cases in the sibling tests below) must
    # be reported truthfully.
    context = await repository.recovery_context(retired.id)
    assert context.get("last_application_outcome") == "retired"
    assert context.get("last_execution_retirement_reason") == "materialization_superseded"

    # Further ordinary cadence reconstructs and dispatches exactly ONE
    # generation-B execution.
    await repository.manifest(rebound.record, entries, selection_id=generation_b.selection_id)
    await engine.resolve_pending()
    await engine.reconcile_executions()
    assert sum(1 for call in executor.calls if call[0] == "start") == start_before + 1
    final = next(item for item in await repository.artifacts(seed.transfer_id) if item.request_id == child_id)
    assert final.execution is not None
    final_admission = await repository.materialization_authorization(final)
    assert final_admission.kind == MaterializationAdmissionKind.PROCEED


@pytest.mark.asyncio
async def test_ordinary_reconciliation_cadence_never_orphans_generation_a_when_cancel_fails(engine_stack, monkeypatch):
    """Gate 9 revision-4 rejection finding 2: a cancel-failure outcome from
    the executor must leave generation A's execution association untouched
    (fenced, retryable) rather than detaching it while the native writer may
    still be live. Generation B must stay blocked (STALE) the entire time --
    admission compares against the transfer's independently-tracked current
    generation, not this retirement helper's completion."""
    from transfers.errors import Domain, Category, Stage

    engine, repository, executor, seed, rebound, entries, generation_b, artifact_a, child_id = (
        await _generation_a_live_with_stale_generation_b(engine_stack)
    )

    async def failing_cancel(handle):
        return ExecutionObservation(handle, ExecutionState.UNKNOWN,
                                    error=NormalizedError(Domain.EXECUTOR, Category.TRANSFER_FAILED, Stage.CLEANUP))

    monkeypatch.setattr(executor, "cancel", failing_cancel)
    start_before = sum(1 for call in executor.calls if call[0] == "start")

    await engine.reconcile_executions()

    still_live = next(item for item in await repository.artifacts(seed.transfer_id) if item.request_id == child_id)
    assert still_live.execution is not None, "a failed cancel must not detach the association"
    assert still_live.id == artifact_a.id
    async with database.get_db() as db:
        exec_row = await db.fetchone(
            "SELECT state, authorized FROM execution_attempts WHERE id=?", (artifact_a.execution.attempt_id,),
        )
    assert exec_row["authorized"] == 1, "a failed cancel must not deauthorize the still-possibly-live execution"
    still_blocked = await repository.materialization_authorization(still_live)
    assert still_blocked.kind == MaterializationAdmissionKind.STALE
    assert sum(1 for call in executor.calls if call[0] == "start") == start_before, "generation B must remain blocked"
    # Gate 9 revision-5 rejection finding 2: a deferred (not actually
    # retired) STALE reconciliation must never be durably reported as
    # "retired" provenance.
    context = await repository.recovery_context(still_live.id)
    assert context.get("last_application_outcome") != "retired"
    assert context.get("last_execution_retirement_reason") != "materialization_superseded"


@pytest.mark.asyncio
async def test_ordinary_reconciliation_cadence_never_orphans_generation_a_on_unknown_observation(engine_stack, monkeypatch):
    """Gate 9 revision-4 rejection finding 2: cancellation itself may report
    success while the post-cancel observation is UNKNOWN/still-active
    (nonterminal). That must also leave the association untouched -- only a
    CONFIRMED terminal observation may retire the artifact."""
    engine, repository, executor, seed, rebound, entries, generation_b, artifact_a, child_id = (
        await _generation_a_live_with_stale_generation_b(engine_stack)
    )

    real_observe = executor.observe

    async def unknown_observe(handle):
        observed = await real_observe(handle)
        return replace(observed, state=ExecutionState.UNKNOWN)

    monkeypatch.setattr(executor, "observe", unknown_observe)
    start_before = sum(1 for call in executor.calls if call[0] == "start")

    await engine.reconcile_executions()

    # Cancel itself was attempted (and "succeeded" per the executor), but
    # confirmation never arrived -- the artifact must stay fenced, not
    # detached.
    still_live = next(item for item in await repository.artifacts(seed.transfer_id) if item.request_id == child_id)
    assert still_live.execution is not None, "an unconfirmed (UNKNOWN) observation must not detach the association"
    async with database.get_db() as db:
        exec_row = await db.fetchone(
            "SELECT authorized FROM execution_attempts WHERE id=?", (artifact_a.execution.attempt_id,),
        )
    assert exec_row["authorized"] == 1
    still_blocked = await repository.materialization_authorization(still_live)
    assert still_blocked.kind == MaterializationAdmissionKind.STALE
    assert sum(1 for call in executor.calls if call[0] == "start") == start_before, "generation B must remain blocked"
    context = await repository.recovery_context(still_live.id)
    assert context.get("last_application_outcome") != "retired"
    assert context.get("last_execution_retirement_reason") != "materialization_superseded"


@pytest.mark.asyncio
async def test_ordinary_reconciliation_cadence_never_orphans_generation_a_on_handle_mismatch(engine_stack, monkeypatch):
    """Gate 9 revision-5 rejection finding 2: a terminal state reported
    against a DIFFERENT execution handle than the one being retired must
    never authorize detach. ``_retire_stale_execution`` must require the
    confirmed observation's handle to match the execution being retired
    before trusting its terminal state -- exactly as every other
    executor-truth boundary in this engine already treats a handle mismatch
    as untrustworthy (``_reconcile_current``/``_park_existing_execution``
    both raise on a mismatch for the SAME reason)."""
    engine, repository, executor, seed, rebound, entries, generation_b, artifact_a, child_id = (
        await _generation_a_live_with_stale_generation_b(engine_stack)
    )

    foreign_handle = replace(artifact_a.execution, attempt_id="foreign-attempt-id")

    async def mismatched_observe(_handle):
        return ExecutionObservation(foreign_handle, ExecutionState.CANCELLED)

    monkeypatch.setattr(executor, "observe", mismatched_observe)
    start_before = sum(1 for call in executor.calls if call[0] == "start")

    await engine.reconcile_executions()

    still_live = next(item for item in await repository.artifacts(seed.transfer_id) if item.request_id == child_id)
    assert still_live.execution is not None, "a mismatched-handle terminal report must not detach the association"
    async with database.get_db() as db:
        exec_row = await db.fetchone(
            "SELECT authorized FROM execution_attempts WHERE id=?", (artifact_a.execution.attempt_id,),
        )
    assert exec_row["authorized"] == 1
    still_blocked = await repository.materialization_authorization(still_live)
    assert still_blocked.kind == MaterializationAdmissionKind.STALE
    assert sum(1 for call in executor.calls if call[0] == "start") == start_before, "generation B must remain blocked"
    context = await repository.recovery_context(still_live.id)
    assert context.get("last_application_outcome") != "retired"
    assert context.get("last_execution_retirement_reason") != "materialization_superseded"


@pytest.mark.asyncio
async def test_ordinary_reconciliation_cadence_never_retires_generation_a_when_claim_lost_after_cancel_confirmation(
    engine_stack, monkeypatch,
):
    """Gate 9 revision-6 rejection finding 2: the earlier revision's
    ``_CLAIM_LOST`` result only detected claim loss BEFORE the first
    executor operation, so a fence lost during the cancel->observe window
    (e.g. a concurrent pause/resume incrementing the recovery generation)
    could still let the stale owner perform the detach/requeue mutation
    afterward. The fence must be revalidated again immediately before that
    durable mutation -- proven here by failing ``recovery_claim_current``
    only from its SECOND call onward (the first, pre-cancel check still
    succeeds, so cancellation and observation genuinely run; only the final,
    pre-mutation revalidation fails)."""
    engine, repository, executor, seed, rebound, entries, generation_b, artifact_a, child_id = (
        await _generation_a_live_with_stale_generation_b(engine_stack)
    )

    real_claim_current = repository.recovery_claim_current
    calls = {"n": 0}

    async def lose_claim_after_first_check(claim, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return await real_claim_current(claim, **kwargs)
        return False

    monkeypatch.setattr(repository, "recovery_claim_current", lose_claim_after_first_check)
    start_before = sum(1 for call in executor.calls if call[0] == "start")
    cancel_before = sum(1 for call in executor.calls if call[0] == "cancel")

    await engine.reconcile_executions()

    assert calls["n"] >= 2, "both the pre-cancel check and the pre-mutation revalidation must run"
    assert sum(1 for call in executor.calls if call[0] == "cancel") == cancel_before + 1, (
        "cancellation must have actually been attempted -- the first (pre-cancel) fence check succeeded"
    )
    still_live = next(item for item in await repository.artifacts(seed.transfer_id) if item.request_id == child_id)
    assert still_live.execution is not None, (
        "a claim lost immediately before the detach/requeue mutation must not detach the association"
    )
    assert still_live.id == artifact_a.id
    async with database.get_db() as db:
        exec_row = await db.fetchone(
            "SELECT authorized FROM execution_attempts WHERE id=?", (artifact_a.execution.attempt_id,),
        )
    assert exec_row["authorized"] == 1, "a claim lost before the final mutation must not deauthorize the execution"
    still_blocked = await repository.materialization_authorization(still_live)
    assert still_blocked.kind == MaterializationAdmissionKind.STALE
    assert sum(1 for call in executor.calls if call[0] == "start") == start_before, "generation B must remain blocked"
    context = await repository.recovery_context(still_live.id)
    assert context.get("last_application_outcome") != "retired"
    assert context.get("last_execution_retirement_reason") != "materialization_superseded"


@pytest.mark.asyncio
async def test_ordinary_reconciliation_cadence_resumes_quiesced_writer_once_admission_returns_to_proceed(
    engine_stack, monkeypatch,
):
    """Gate 9 revision-6 rejection finding 1 (specification section 7.5): HOLD
    handling must complete the full quiesce -> wake -> resume lifecycle
    (TRANSFERRING -> HOLD -> PAUSED/quiesced -> PROCEED -> ordinary cadence
    -> resumed) through ordinary reconciliation cadence alone -- no user
    Retry, no manually acquired recovery claim. The prior revision proved
    only the quiesce half; without a wake path for
    ``quiescence_reason="materialization_hold"``, the base execution loop
    (which only reprocesses existing executions in queued/downloading/
    unknown/verifying/paused, never ``recovery_wait``) left a HOLD-parked
    writer parked indefinitely even after admission returned to PROCEED."""
    engine, repository, provider, executor = engine_stack
    transfer = await engine.submit((TransferRequest("parcel", "p1", name="payload.bin"),), deduplicate=False)
    await engine.resolve_pending()
    await engine.reconcile_executions()
    started = (await repository.artifacts(transfer.id))[0]
    assert started.execution is not None
    attempt_id = started.execution.attempt_id
    assert sum(1 for call in executor.calls if call[0] == "start") == 1

    real_admission = repository.materialization_authorization
    hold = {"on": True}

    async def toggled_admission(artifact):
        if hold["on"]:
            return MaterializationAdmission(MaterializationAdmissionKind.HOLD, authority_generation="forced-generation")
        return await real_admission(artifact)

    monkeypatch.setattr(repository, "materialization_authorization", toggled_admission)

    # Quiesce: TRANSFERRING -> HOLD -> PAUSED (ordinary cadence only).
    await engine.reconcile_executions()
    quiesced = (await repository.artifacts(transfer.id))[0]
    assert quiesced.state == "recovery_wait"
    async with database.get_db() as db:
        exec_row = await db.fetchone(
            "SELECT state, authorized FROM execution_attempts WHERE id=?", (attempt_id,),
        )
    assert exec_row["state"] == "paused"
    assert exec_row["authorized"] == 1
    context = await repository.recovery_context(quiesced.id)
    assert context.get("quiescence_reason") == "materialization_hold"

    # Authorization is durably restored -- the SAME admission function now
    # reports PROCEED, not a forced/hardcoded value.
    hold["on"] = False

    # Wake + resume: ordinary cadence only, no user Retry, no manually
    # acquired claim.
    await engine.reconcile_executions()

    resumed = (await repository.artifacts(transfer.id))[0]
    assert resumed.state == "downloading", "the writer must resume, not stay parked"
    assert resumed.execution is not None and resumed.execution.attempt_id == attempt_id, (
        "the SAME execution attempt must resume, never a fresh dispatch"
    )
    async with database.get_db() as db:
        exec_row = await db.fetchone(
            "SELECT state, authorized FROM execution_attempts WHERE id=?", (attempt_id,),
        )
    assert exec_row["state"] == "running"
    assert exec_row["authorized"] == 1
    assert sum(1 for call in executor.calls if call[0] == "start") == 1, (
        "resumption must never dispatch a second execution attempt"
    )
    context = await repository.recovery_context(resumed.id)
    assert context.get("quiescence_reason") is None


# --------------------------------------------------------------------------- #
# Gate 9 revision-7 rejection: the final claim check must be ATOMIC with the
# mutation it authorizes, not merely a prior check.
# --------------------------------------------------------------------------- #

async def _invalidate_claim_via_competing_acquisition(repository, claim, clock):
    """Simulate a concurrent recovery owner fencing ``claim`` in exactly the
    window a caller might otherwise treat a prior ``_RETIRED``/success result
    as a durable authorization token: release the current claim and let a
    competing acquisition take it, which durably advances
    ``recovery_generation`` -- the SAME durable fence a pause/resume would
    advance -- making ``claim`` (the one the caller is still holding) stale."""
    assert await repository.finish_recovery_claim(claim)
    competing = await repository.claim_recovery(claim.artifact_id, RecoveryTrigger.AUTO_RETRY, clock())
    assert competing is not None, "the competing acquisition itself must succeed for this to be a valid race"
    return competing


@pytest.mark.asyncio
async def test_stale_retirement_atomic_commit_never_detaches_or_requeues_when_claim_invalidated_after_retired(
    engine_stack, monkeypatch,
):
    """Gate 9 revision-7 rejection: the earlier revision revalidated the claim
    a second time inside ``_cancel_and_confirm_stopped`` and returned
    ``_RETIRED``, but the caller (``_retire_stale_execution``) then performed
    the actual detach/release/requeue as a SEPARATE, unfenced mutation
    afterward -- so a concurrent recovery owner fencing the artifact in the
    gap between "``_cancel_and_confirm_stopped`` said ``_RETIRED``" and "the
    detach/requeue commits" could still let a now-stale caller perform that
    mutation. ``TransferRepository.retire_stale_materialization_if_claim_
    current`` closes this by re-verifying the SAME claim atomically, inside
    the SAME transaction as the mutation. This deliberately invalidates the
    claim AFTER ``_cancel_and_confirm_stopped`` has already returned
    ``_RETIRED`` (cancellation and confirmation genuinely happened) but
    BEFORE the detach/requeue mutation runs, then proves no detach, no
    deauthorization, and no requeue occurred."""
    engine, repository, executor, seed, rebound, entries, generation_b, artifact_a, child_id = (
        await _generation_a_live_with_stale_generation_b(engine_stack)
    )

    real_cancel_and_confirm = engine._cancel_and_confirm_stopped
    invalidated = {"done": False, "competing_claim": None}

    async def invalidate_after_retired(claim, artifact, executor_):
        result, confirmed = await real_cancel_and_confirm(claim, artifact, executor_)
        if result == engine._RETIRED and not invalidated["done"]:
            invalidated["done"] = True
            invalidated["competing_claim"] = await _invalidate_claim_via_competing_acquisition(
                repository, claim, engine.clock,
            )
        return result, confirmed

    monkeypatch.setattr(engine, "_cancel_and_confirm_stopped", invalidate_after_retired)
    start_before = sum(1 for call in executor.calls if call[0] == "start")
    cancel_before = sum(1 for call in executor.calls if call[0] == "cancel")

    # Ordinary cadence only -- no manually driven claim/dispatch on the test's
    # own part; the race is injected entirely inside the monkeypatched helper.
    await engine.reconcile_executions()

    assert invalidated["done"], "the race window must actually have been exercised"
    assert sum(1 for call in executor.calls if call[0] == "cancel") == cancel_before + 1, (
        "cancellation and confirmation must have genuinely happened before invalidation"
    )
    still_live = next(item for item in await repository.artifacts(seed.transfer_id) if item.request_id == child_id)
    assert still_live.execution is not None, (
        "a claim lost after _cancel_and_confirm_stopped returned _RETIRED but before the atomic "
        "detach/requeue commits must NOT detach the association"
    )
    assert still_live.id == artifact_a.id
    async with database.get_db() as db:
        exec_row = await db.fetchone(
            "SELECT authorized FROM execution_attempts WHERE id=?", (artifact_a.execution.attempt_id,),
        )
        request_row = await db.fetchone(
            "SELECT state FROM transfer_requests WHERE id=?", (child_id,),
        )
    assert exec_row["authorized"] == 1, (
        "a claim lost immediately before the atomic mutation must not deauthorize the execution"
    )
    assert request_row["state"] != "pending", "a claim lost before the atomic mutation must not requeue the request"
    still_blocked = await repository.materialization_authorization(still_live)
    assert still_blocked.kind == MaterializationAdmissionKind.STALE
    assert sum(1 for call in executor.calls if call[0] == "start") == start_before, "generation B must remain blocked"
    context = await repository.recovery_context(still_live.id)
    assert context.get("last_application_outcome") != "retired"
    assert context.get("last_execution_retirement_reason") != "materialization_superseded"

    # Cleanup: release the competing claim this test itself acquired so it
    # does not leak a live fence across test boundaries.
    await repository.finish_recovery_claim(invalidated["competing_claim"])


class _UnpausableMemoryExecutor:
    """A writer that structurally cannot be paused -- it declares no
    per-execution control capability and defines NO ``pause``/``resume``
    attributes at all, forcing ``_park_existing_execution`` into its
    cancel/retire-on-HOLD branch rather than the pause branch
    ``MemoryExecutor`` (which DOES declare per-execution pause) would take."""

    capabilities = ExecutorCapabilities()

    def __init__(self, authorize):
        self.descriptor = IntegrationDescriptor("unpausable-memory-copy", "Unpausable memory copy", frozenset())
        self.authorize = authorize
        self.calls = []
        self.jobs = {}

    def claim(self, subject):
        return ExecutorClaim(any(endpoint.scheme == "memory" for endpoint in subject.candidate.endpoints))

    def footprint(self, work):
        return ExecutionFootprint((work.materialization.target + ".memory-progress",))

    def prepare(self, request):
        return ExecutionHandle(self.descriptor.id, request.attempt_id,
                               {"copy_ticket": request.attempt_id, "destination": request.work.materialization.target})

    async def start(self, request, handle):
        assert await self.authorize(handle, "start"), "Core must persist authority before executor contact"
        self.calls.append(("start", handle))
        result = neutral_facts(ExecutionObservation(handle, ExecutionState.RUNNING, TransferProgress(4, 1, 1)))
        self.jobs[handle.attempt_id] = result
        return result

    async def observe(self, handle):
        assert await self.authorize(handle, "observe")
        self.calls.append(("observe", handle))
        return self.jobs.get(handle.attempt_id, ExecutionObservation(handle, ExecutionState.ABSENT))

    async def observe_many(self, handles):
        return ExecutionSnapshot(tuple([await self.observe(handle) for handle in handles]))

    async def cancel(self, handle):
        assert await self.authorize(handle, "cancel")
        self.calls.append(("cancel", handle))
        if handle.attempt_id in self.jobs:
            self.jobs[handle.attempt_id] = replace(self.jobs[handle.attempt_id], state=ExecutionState.CANCELLED)
            return self.jobs[handle.attempt_id]
        return ExecutionObservation(handle, ExecutionState.ABSENT)

    async def health(self):
        return ExecutorHealth(True, True)


@pytest_asyncio.fixture
async def unpausable_engine_stack(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "admission-engine-unpausable.db")
    await database.init_db()
    repository = RecoveryTransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider("engine-lab-unpausable")
    executor = _UnpausableMemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0,
                              max_active_executions=8, resolution_concurrency=8),
        clock=lambda: 1000.0,
    )
    await engine.initialize()
    return engine, repository, provider, executor


@pytest.mark.asyncio
async def test_hold_unpausable_retirement_atomic_settlement_never_parks_when_claim_invalidated_after_retired(
    unpausable_engine_stack, monkeypatch,
):
    """Gate 9 revision-7 rejection: the same architectural hole affected the
    unpausable-HOLD caller (``_park_existing_execution``'s cancel/retire
    branch) -- after ``_cancel_and_confirm_stopped`` returned ``_RETIRED`` it
    continued into ``_settle_parked_execution``'s park/settle transition
    (ultimately ``TransferRepository.transition_recovery``) as an unfenced
    mutation outside the atomic claim check. ``transition_recovery`` now
    accepts the SAME claim and re-verifies it atomically inside its own
    transaction. This deliberately invalidates the claim AFTER
    ``_cancel_and_confirm_stopped`` has already returned ``_RETIRED`` but
    BEFORE the settlement mutation runs, then proves no parking transition
    (no ``recovery_wait``, no deauthorization) occurred."""
    engine, repository, provider, executor = unpausable_engine_stack
    transfer = await engine.submit((TransferRequest("parcel", "p1", name="payload.bin"),), deduplicate=False)
    await engine.resolve_pending()
    await engine.reconcile_executions()
    started = (await repository.artifacts(transfer.id))[0]
    assert started.execution is not None
    attempt_id = started.execution.attempt_id
    assert sum(1 for call in executor.calls if call[0] == "start") == 1

    monkeypatch.setattr(
        repository, "materialization_authorization",
        _forced_admission(MaterializationAdmissionKind.HOLD),
    )

    real_cancel_and_confirm = engine._cancel_and_confirm_stopped
    invalidated = {"done": False, "competing_claim": None}

    async def invalidate_after_retired(claim, artifact, executor_):
        result, confirmed = await real_cancel_and_confirm(claim, artifact, executor_)
        if result == engine._RETIRED and not invalidated["done"]:
            invalidated["done"] = True
            invalidated["competing_claim"] = await _invalidate_claim_via_competing_acquisition(
                repository, claim, engine.clock,
            )
        return result, confirmed

    monkeypatch.setattr(engine, "_cancel_and_confirm_stopped", invalidate_after_retired)
    cancel_before = sum(1 for call in executor.calls if call[0] == "cancel")
    start_before = sum(1 for call in executor.calls if call[0] == "start")

    await engine.reconcile_executions()

    assert invalidated["done"], "the race window must actually have been exercised"
    assert sum(1 for call in executor.calls if call[0] == "cancel") == cancel_before + 1, (
        "cancellation and confirmation must have genuinely happened before invalidation"
    )
    still_live = (await repository.artifacts(transfer.id))[0]
    assert still_live.execution is not None and still_live.execution.attempt_id == attempt_id, (
        "a claim lost after _cancel_and_confirm_stopped returned _RETIRED but before the atomic "
        "settlement commits must NOT detach the association"
    )
    assert still_live.state != "recovery_wait", "no parking transition may commit under a lost claim"
    async with database.get_db() as db:
        exec_row = await db.fetchone(
            "SELECT state, authorized FROM execution_attempts WHERE id=?", (attempt_id,),
        )
    assert exec_row["state"] == "cancelled", "the truthful terminal observation is still persisted"
    assert exec_row["authorized"] == 1, (
        "a claim lost immediately before the atomic settlement must not deauthorize the execution"
    )
    assert sum(1 for call in executor.calls if call[0] == "start") == start_before, (
        "no fresh dispatch may occur while settlement is unresolved"
    )
    context = await repository.recovery_context(still_live.id)
    assert context.get("quiescence_reason") != "materialization_hold", (
        "no quiescence provenance may be recorded for a settlement that never committed"
    )

    await repository.finish_recovery_claim(invalidated["competing_claim"])
