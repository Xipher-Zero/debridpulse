"""1.0.13 SAB-backed executor: canonical contract, native truth at the edge.

Every expectation encodes a fact characterized against a real SABnzbd 5.1.3
instance at Gate 1, not an analogy to aria2.
"""
from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from transfers.models import (
    ExecutionHandle, ExecutionRequest, ExecutionState, ExecutionSubject, ExecutionWork,
    MaterializationKind, MaterializationPlan, TransferCandidate,
)

from sab_fakes import FakeSab, SabTransportError, staged_context, staged_store  # noqa: F401


@pytest.fixture
def lab(tmp_path):
    """A SAB fake whose working area is a real directory tree."""
    root = tmp_path / "download"
    (root / ".dpwork" / "complete").mkdir(parents=True)
    (root / ".dpwork" / "incomplete").mkdir(parents=True)
    sab = FakeSab(complete_dir=str(root / ".dpwork" / "complete"),
                  download_dir=str(root / ".dpwork" / "incomplete"))
    return SimpleNamespace(sab=sab, root=str(root), executor=build(sab, root=str(root)))


def build(sab, *, root):
    from executors.sabnzbd.executor import SabnzbdConfiguration, SabnzbdExecutor
    configuration = SabnzbdConfiguration(
        local_root=root, working_directory=root + "/.dpwork",
        complete_directory=root + "/.dpwork/complete",
    )

    async def authorize(handle, action):
        return True

    return SabnzbdExecutor(sab, configuration, authorize,
        staged_input=staged_store(),
    )


def subject_for(name="posted", kind="nzb"):
    candidate = TransferCandidate(
        name=name, endpoints=(), provider_id="usenet",
        materialization=MaterializationKind.COLLECTION, request_kind=kind,
        context=staged_context(),
    )
    return ExecutionSubject.of(candidate)


def request_for(root, name="posted", attempt="attempt-1"):
    subject = subject_for(name)
    plan = MaterializationPlan(MaterializationKind.COLLECTION, f"{root}/{name}")
    return ExecutionRequest(ExecutionWork(subject, plan, attempt), attempt)


# --- claim ---------------------------------------------------------------

def test_claims_nzb_subject_and_rejects_others(lab):
    executor = lab.executor
    assert executor.claim(subject_for(kind="nzb")).supported is True
    for kind in ("http", "https", "magnet", "torrent", "ftp"):
        assert executor.claim(subject_for(kind=kind)).supported is False


def test_declares_collection_and_no_unproven_capabilities(lab):
    executor = lab.executor
    caps = executor.capabilities
    assert caps.materialization_kinds == frozenset({MaterializationKind.COLLECTION})
    assert caps.per_execution_pause is True
    # Enforced through the neutral seam for the DP-dedicated topology; its own
    # limits are proven in test_v113_usenet_bandwidth_ceiling.py.
    assert caps.aggregate_bandwidth_ceiling is True
    # Still absent, each for a characterized reason (never for symmetry).
    assert caps.acquisition_gate is False
    assert caps.native_assisted_retry is False
    assert caps.candidate_sampling is False
    assert caps.transient_input is False


# --- prepare: no native side effect --------------------------------------

def test_prepare_creates_no_native_work_and_mints_a_correlation_token(lab):
    sab, executor = lab.sab, lab.executor
    handle = executor.prepare(request_for(lab.root))
    assert isinstance(handle, ExecutionHandle)
    assert handle.native is None                 # nzo_id is server-minted at start
    assert handle.correlation.get("token")
    assert sab.queue == {} and sab.submissions == []


# --- start: exactly once, one legal binding ------------------------------

@pytest.mark.asyncio
async def test_start_submits_exactly_one_job_and_binds_native_once(lab):
    sab, executor = lab.sab, lab.executor
    request = request_for(lab.root)
    handle = executor.prepare(request)
    observed = await executor.start(request, handle)
    assert len(sab.submissions) == 1
    assert len(sab.queue) == 1
    assert observed.state in {ExecutionState.QUEUED, ExecutionState.RUNNING}
    assert handle.binds(observed.handle)
    assert observed.handle.native["nzo_id"] in sab.queue


@pytest.mark.asyncio
async def test_lost_start_acknowledgement_never_resubmits_and_recovers_the_same_job(lab):
    sab, executor = lab.sab, lab.executor
    sab.drop_next_response = True
    request = request_for(lab.root)
    handle = executor.prepare(request)
    observed = await executor.start(request, handle)
    # The submission DID reach SAB exactly once.
    assert len(sab.submissions) == 1
    assert len(sab.queue) == 1
    # It is reconciled by correlation token, never resubmitted.
    assert observed.state != ExecutionState.FAILED
    if observed.handle.native is not None:
        assert observed.handle.native["nzo_id"] in sab.queue
    else:
        assert observed.state == ExecutionState.UNKNOWN
    assert len(sab.submissions) == 1


@pytest.mark.asyncio
async def test_start_transport_failure_is_unknown_never_failed(lab):
    sab, executor = lab.sab, lab.executor
    sab.reachable = False
    request = request_for(lab.root)
    handle = executor.prepare(request)
    observed = await executor.start(request, handle)
    assert observed.state == ExecutionState.UNKNOWN


# --- observation ---------------------------------------------------------

@pytest.mark.asyncio
async def test_queue_history_and_absence_observation(lab):
    sab, executor = lab.sab, lab.executor
    request = request_for(lab.root)
    handle = executor.prepare(request)
    bound = (await executor.start(request, handle)).handle
    nzo = bound.native["nzo_id"]

    snapshot = await executor.observe_many((bound,))
    assert snapshot.error is None
    assert snapshot.observations[0].state in {ExecutionState.QUEUED, ExecutionState.RUNNING}

    sab.finish(nzo)
    snapshot = await executor.observe_many((bound,))
    assert snapshot.observations[0].state == ExecutionState.SUCCEEDED

    # Operator deleted it outside DP: valid answer, absent from queue AND history.
    sab.history.clear()
    snapshot = await executor.observe_many((bound,))
    assert snapshot.observations[0].state == ExecutionState.ABSENT


@pytest.mark.asyncio
async def test_failed_job_is_failed_and_reports_no_materialization(lab):
    sab, executor = lab.sab, lab.executor
    request = request_for(lab.root)
    handle = executor.prepare(request)
    bound = (await executor.start(request, handle)).handle
    sab.fail(bound.native["nzo_id"])
    observed = (await executor.observe_many((bound,))).observations[0]
    assert observed.state == ExecutionState.FAILED
    # A failed job's SAB `storage` points into the working dir; never material.
    assert observed.materialization is None


@pytest.mark.asyncio
async def test_unreachable_sab_is_a_snapshot_error_never_an_empty_success(lab):
    sab, executor = lab.sab, lab.executor
    request = request_for(lab.root)
    handle = executor.prepare(request)
    bound = (await executor.start(request, handle)).handle
    sab.reachable = False
    snapshot = await executor.observe_many((bound,))
    assert snapshot.error is not None or all(
        o.state == ExecutionState.UNKNOWN for o in snapshot.observations)
    # Never ABSENT / FAILED from mere unreachability.
    for observation in snapshot.observations:
        assert observation.state not in {ExecutionState.ABSENT, ExecutionState.FAILED}


# --- cancel: acknowledgement is not truth --------------------------------

@pytest.mark.asyncio
async def test_cancel_reports_observed_truth_not_the_acknowledgement(lab):
    sab, executor = lab.sab, lab.executor
    request = request_for(lab.root)
    handle = executor.prepare(request)
    bound = (await executor.start(request, handle)).handle
    observed = await executor.cancel(bound)
    assert observed.state in {ExecutionState.CANCELLED, ExecutionState.ABSENT}
    assert bound.native["nzo_id"] not in sab.queue


@pytest.mark.asyncio
async def test_cancel_while_unreachable_stays_unknown(lab):
    sab, executor = lab.sab, lab.executor
    request = request_for(lab.root)
    handle = executor.prepare(request)
    bound = (await executor.start(request, handle)).handle
    sab.reachable = False
    observed = await executor.cancel(bound)
    assert observed.state == ExecutionState.UNKNOWN


# --- unpack boundary -----------------------------------------------------

@pytest.mark.asyncio
async def test_submission_always_disables_native_unpack(lab):
    sab, executor = lab.sab, lab.executor
    request = request_for(lab.root)
    handle = executor.prepare(request)
    await executor.start(request, handle)
    job = next(iter(sab.queue.values()))
    assert job.pp == "R"   # repair only: SAB never unpacks for DP


# --- health --------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_reflects_reachability_and_the_enforceable_ceiling(lab):
    from transfers.models import ExecutorRuntimeCapability
    sab, executor = lab.sab, lab.executor
    health = await executor.health()
    assert health.reachable and health.ready
    # The ceiling is enforceable exactly while the service answers.
    assert health.available_runtime_capabilities == frozenset(
        {ExecutorRuntimeCapability.AGGREGATE_BANDWIDTH_CEILING})
    sab.reachable = False
    unreachable = await executor.health()
    assert unreachable.reachable is False
    assert unreachable.available_runtime_capabilities == frozenset()
