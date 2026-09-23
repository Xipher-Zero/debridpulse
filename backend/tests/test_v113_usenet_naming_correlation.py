"""1.0.13 release blocker A/B/C: native job naming vs durable attempt correlation.

Characterized against the exact bundled SABnzbd 5.1.3 (source sha256 pinned in
the Dockerfile) before any of this was written:

* ``nzbname`` becomes ``NzbObject.final_name``; the queue publishes it as slot
  ``filename`` and the history as ``name``. The queue publishes NO other
  DebridPulse-settable string -- no ``name`` key, no ``nzb_name``, no custom
  metadata -- and queue search matches ``final_name`` alone.
* Post-processing calls ``deobfuscate(nzo, files, nzo.final_name)``, which
  renames an obfuscated dominant member (and its lookalikes) to that job name.
  Running the bundled module reproduced the production defect exactly:
  ``dp-9ce15fce994c527f7712b4c4.mp4`` beside an untouched ``rename.par2``.
* ``mode=queue&name=rename`` changes ``final_name`` only -- the incomplete
  folder keeps its original name -- takes effect immediately, is persisted, and
  answers ``{"status": false}`` for an id that is not in the queue.

So the correlation token may occupy the job name only while the submission is
still unproven, and the release name must own it from the moment the native id
is bound.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from transfers.models import (
    ExecutionRequest, ExecutionState, ExecutionSubject, ExecutionWork,
    MaterializationKind, MaterializationPlan, TransferCandidate,
)

from sab_fakes import FakeSab, SabTransportError  # noqa: F401

RELEASE = "PornWorld.26.09.22.Marica.Chanelle.XXX.2160p.MP4-WRB"


@pytest.fixture
def lab(tmp_path):
    root = tmp_path / "download"
    (root / ".dpwork" / "complete").mkdir(parents=True)
    (root / ".dpwork" / "incomplete").mkdir(parents=True)
    sab = FakeSab(complete_dir=str(root / ".dpwork" / "complete"),
                  download_dir=str(root / ".dpwork" / "incomplete"))
    return SimpleNamespace(sab=sab, root=str(root), executor=_build(sab, str(root), tmp_path))


def _build(sab, root, tmp_path):
    from executors.sabnzbd.executor import SabnzbdConfiguration, SabnzbdExecutor
    from transfers.staged_input import StagedInputStore

    async def authorize(handle, action):
        return True

    return SabnzbdExecutor(
        sab,
        SabnzbdConfiguration(local_root=root, working_directory=root + "/.dpwork",
                             complete_directory=root + "/.dpwork/complete"),
        authorize,
        staged_input=StagedInputStore(str(tmp_path / "staged")),
    )


def _request(lab, *, name=RELEASE, attempt="attempt-A"):
    """One execution request whose NZB lives in the neutral staged-input owner."""
    from transfers.staged_input import StagedInputStore
    store: StagedInputStore = lab.executor.staged_input
    reference = store.stage_bytes(b'<?xml version="1.0"?><nzb><file/></nzb>')
    candidate = TransferCandidate(
        name=name, endpoints=(), provider_id="usenet",
        materialization=MaterializationKind.COLLECTION, request_kind="nzb",
        context={"staged_input": reference.as_context()},
    )
    plan = MaterializationPlan(MaterializationKind.COLLECTION, f"{lab.root}/{name}")
    return ExecutionRequest(ExecutionWork(ExecutionSubject.of(candidate), plan, attempt), attempt)


def _token(handle):
    return str(handle.correlation.get("token") or "")


# --- A1: the human job name is not the correlation token --------------------

@pytest.mark.asyncio
async def test_a1_the_native_job_carries_the_release_name_not_the_attempt_token(lab):
    request = _request(lab)
    handle = lab.executor.prepare(request)
    observation = await lab.executor.start(request, handle)

    # Created paused under the token, named, then released: live, not fenced.
    assert observation.state in {ExecutionState.QUEUED, ExecutionState.RUNNING}
    job = lab.sab.queue[observation.handle.native["nzo_id"]]
    assert job.name == RELEASE, "SAB must hold the release name, never the DP token"
    assert not job.name.startswith("dp-")


@pytest.mark.asyncio
async def test_a1_the_durable_correlation_remains_independently_recoverable(lab):
    request = _request(lab)
    handle = lab.executor.prepare(request)
    token = _token(handle)

    assert token.startswith("dp-") and len(token) == 27
    # It survives renaming the native job, because it never lived there.
    observation = await lab.executor.start(request, handle)
    assert _token(observation.handle) == token
    assert observation.handle.native["nzo_id"]


@pytest.mark.asyncio
async def test_a1_the_token_is_the_submitted_job_name_only_while_unproven(lab):
    """The one window in which a name is identity, closed as soon as it can be."""
    request = _request(lab)
    handle = lab.executor.prepare(request)
    await lab.executor.start(request, handle)
    submitted_names = [name for name, _ in lab.sab.submissions]
    assert submitted_names == [_token(handle)]
    assert lab.sab.renames and lab.sab.renames[-1][1] == RELEASE


# --- A2: single-file obfuscated payload -------------------------------------

@pytest.mark.asyncio
async def test_a2_a_deobfuscated_member_never_inherits_the_dp_token(lab):
    request = _request(lab)
    handle = lab.executor.prepare(request)
    started = await lab.executor.start(request, handle)
    nzo_id = started.handle.native["nzo_id"]

    # SAB deobfuscates the dominant member to its JOB name -- which is now the
    # release name -- and leaves the PAR2 sidecar alone.
    lab.sab.finish(nzo_id, files=((f"{RELEASE}.mp4", 4096), ("rename.par2", 512)))
    observation = await lab.executor._observe(started.handle)

    assert observation.state == ExecutionState.SUCCEEDED
    names = [entry.relative_path for entry in observation.materialization.entries]
    assert sorted(names) == sorted([f"{RELEASE}.mp4", "rename.par2"])
    assert not any(name.startswith("dp-") for name in names)


# --- A3/A4: collections are enumerated, never reconstructed -----------------

@pytest.mark.asyncio
@pytest.mark.parametrize("members", [1, 10, 100, 1000])
async def test_a3_a4_every_collection_member_is_preserved_independently(lab, members):
    request = _request(lab)
    handle = lab.executor.prepare(request)
    started = await lab.executor.start(request, handle)

    files = []
    for index in range(members):
        # Mixed extensions, PAR2 sidecars and repeated same-extension payloads.
        files.append((f"{RELEASE}.part{index:04d}.mkv", 1024 + index))
        if index % 10 == 0:
            files.append((f"{RELEASE}.vol{index:03d}.par2", 64))
    lab.sab.finish(started.handle.native["nzo_id"], files=tuple(files))

    observation = await lab.executor._observe(started.handle)
    assert observation.state == ExecutionState.SUCCEEDED
    entries = {entry.relative_path: entry.bytes for entry in observation.materialization.entries}

    assert len(entries) == len(files), "no member may be dropped or merged"
    for name, size in files:
        assert entries[name] == size, "relative path and size are preserved verbatim"
    assert not any(name.startswith("dp-") for name in entries), \
        "no member may be invented from the DP correlation token"


# --- A5: lost acknowledgement ----------------------------------------------

@pytest.mark.asyncio
async def test_a5_a_lost_acknowledgement_reconciles_without_resubmitting(lab):
    request = _request(lab)
    handle = lab.executor.prepare(request)
    lab.sab.drop_next_response = True

    observation = await lab.executor.start(request, handle)

    assert len(lab.sab.submissions) == 1, "a lost ACK must never cause a second submission"
    assert len(lab.sab.queue) == 1, "exactly one native job exists"
    assert observation.handle.native["nzo_id"] == next(iter(lab.sab.queue))
    assert observation.state in {ExecutionState.QUEUED, ExecutionState.PAUSED,
                                 ExecutionState.RUNNING}


@pytest.mark.asyncio
async def test_a5_the_recovered_job_still_ends_up_with_the_release_name(lab):
    request = _request(lab)
    handle = lab.executor.prepare(request)
    lab.sab.drop_next_response = True

    observation = await lab.executor.start(request, handle)
    job = lab.sab.queue[observation.handle.native["nzo_id"]]
    assert job.name == RELEASE, "recovery must not leave the token as the job name"


@pytest.mark.asyncio
async def test_a5_reconciliation_searches_the_token_never_the_release_name(lab):
    request = _request(lab)
    handle = lab.executor.prepare(request)
    lab.sab.drop_next_response = True
    await lab.executor.start(request, handle)

    assert _token(handle) in lab.sab.searches
    assert RELEASE not in lab.sab.searches, \
        "a human display name must never be used as recovery identity"


# --- A6: restart ------------------------------------------------------------

@pytest.mark.asyncio
async def test_a6_a_restart_reconciles_the_same_native_job(lab, tmp_path):
    request = _request(lab)
    handle = lab.executor.prepare(request)
    started = await lab.executor.start(request, handle)
    nzo_id = started.handle.native["nzo_id"]

    # A fresh executor over the same durable handle: nothing in-process survives.
    reborn = _build(lab.sab, lab.root, tmp_path)
    observation = await reborn._observe(started.handle)

    assert observation.handle.native["nzo_id"] == nzo_id
    assert len(lab.sab.queue) == 1
    assert len(lab.sab.submissions) == 1


@pytest.mark.asyncio
async def test_a6_the_token_is_stable_across_restart(lab, tmp_path):
    request = _request(lab)
    first = lab.executor.prepare(request)
    reborn = _build(lab.sab, lab.root, tmp_path)
    assert _token(reborn.prepare(request)) == _token(first)


@pytest.mark.asyncio
async def test_a6_output_names_stay_independent_of_the_correlation(lab):
    request = _request(lab)
    handle = lab.executor.prepare(request)
    started = await lab.executor.start(request, handle)
    lab.sab.finish(started.handle.native["nzo_id"],
                   files=(("Episode.One.mkv", 10), ("Episode.Two.mkv", 20)))
    observation = await lab.executor._observe(started.handle)
    assert sorted(e.relative_path for e in observation.materialization.entries) == \
        ["Episode.One.mkv", "Episode.Two.mkv"]


# --- A7: attempt separation -------------------------------------------------

@pytest.mark.asyncio
async def test_a7_two_attempts_of_one_candidate_have_different_tokens(lab):
    first = lab.executor.prepare(_request(lab, attempt="attempt-A"))
    second = lab.executor.prepare(_request(lab, attempt="attempt-B"))
    assert _token(first) != _token(second)


@pytest.mark.asyncio
async def test_a7_a_new_attempt_can_never_adopt_an_older_attempts_job(lab):
    """Both jobs end up displaying the SAME release name. Identity must not care."""
    first_request = _request(lab, attempt="attempt-A")
    first = await lab.executor.start(first_request, lab.executor.prepare(first_request))
    second_request = _request(lab, attempt="attempt-B")
    second = await lab.executor.start(second_request, lab.executor.prepare(second_request))

    first_id = first.handle.native["nzo_id"]
    second_id = second.handle.native["nzo_id"]
    assert first_id != second_id
    assert lab.sab.queue[first_id].name == lab.sab.queue[second_id].name == RELEASE

    snapshot = await lab.executor.observe_many((first.handle, second.handle))
    resolved = {o.handle.attempt_id: o.handle.native["nzo_id"] for o in snapshot.observations}
    assert resolved == {"attempt-A": first_id, "attempt-B": second_id}


@pytest.mark.asyncio
async def test_a7_an_unbound_attempt_is_never_matched_by_a_shared_release_name(lab):
    """Attempt A is renamed and bound; attempt B is still searching for its token."""
    first_request = _request(lab, attempt="attempt-A")
    await lab.executor.start(first_request, lab.executor.prepare(first_request))

    second_request = _request(lab, attempt="attempt-B")
    unbound = lab.executor.prepare(second_request)
    located = await lab.executor._locate(_token(unbound))
    assert located is None, "attempt B must not find attempt A's renamed job"


# --- the rename is a converging contract, not a compensating pass -----------

@pytest.mark.asyncio
async def test_a_failed_rename_converges_on_a_later_observation(lab):
    request = _request(lab)
    handle = lab.executor.prepare(request)
    lab.sab.refuse_renames = True
    started = await lab.executor.start(request, handle)
    nzo_id = started.handle.native["nzo_id"]
    assert lab.sab.queue[nzo_id].name == _token(handle), "precondition: rename did not take"

    lab.sab.refuse_renames = False
    await lab.executor._observe(started.handle)
    assert lab.sab.queue[nzo_id].name == RELEASE


@pytest.mark.asyncio
async def test_a_rename_is_not_reissued_once_the_job_already_carries_it(lab):
    request = _request(lab)
    started = await lab.executor.start(request, lab.executor.prepare(request))
    before = len(lab.sab.renames)
    await lab.executor._observe(started.handle)
    await lab.executor._observe(started.handle)
    assert len(lab.sab.renames) == before, "convergence must be idempotent, not chatty"


# ===========================================================================
# The submission fence.
#
# Separating the names in time is not enough on its own. Between submission and
# the moment DebridPulse learns the native id, the job exists and is named with
# the correlation token -- and if it is allowed to run in that window, a small
# posting can finish and be post-processed under that name. Deobfuscation would
# then stamp the token onto the payload, the job would land in history, and no
# later reconciliation could undo it: the service can only rename a job that is
# still queued.
#
# That window is exactly the lost-acknowledgement case the correlation
# machinery exists to survive, so the invariant is absolute:
#
#     No native job may acquire or post-process while its name is the
#     DebridPulse correlation token.
#
# It is enforced by creating every job paused and withholding the RESUME
# control until the job is bound AND provably carries its release name. Core
# remains the only pause/resume authority; it simply cannot resume a job the
# executor does not yet offer as resumable.
# ===========================================================================

TOKEN_PATTERN = "dp-"


def _acquired_under_token(lab):
    return [name for _, name in lab.sab.acquired_under_name if name.startswith(TOKEN_PATTERN)]


@pytest.mark.asyncio
async def test_fence_a_lost_acknowledgement_cannot_acquire_under_the_token(lab):
    """The rejected design's race, run deliberately.

    The submission lands, the answer is lost, and the native worker is then
    allowed to do everything it can -- before DebridPulse reconciles anything.
    """
    request = _request(lab)
    handle = lab.executor.prepare(request)
    lab.sab.drop_next_response = True
    lab.sab.worker_runs_during_ambiguity = True   # the service does not wait

    observation = await lab.executor.start(request, handle)

    # And it is given every further opportunity once the call returns.
    lab.sab.run_native_worker()

    assert _acquired_under_token(lab) == [], \
        "a job acquired while it still carried the DP correlation token"
    assert len(lab.sab.submissions) == 1, "a lost ACK must never resubmit"
    assert len(lab.sab.queue) + len(lab.sab.history) == 1, "exactly one native job exists"
    assert observation.handle.native["nzo_id"], "the exact native job was still bound"


@pytest.mark.asyncio
async def test_fence_a_job_is_created_paused_and_named_with_the_token(lab):
    """Before anything else can happen, both halves of the fence must hold."""
    request = _request(lab)
    handle = lab.executor.prepare(request)
    lab.sab.drop_next_response = True
    lab.sab.refuse_renames = True          # keep the fence closed, to observe it

    await lab.executor.start(request, handle)

    job = next(iter(lab.sab.queue.values()))
    assert job.status == "Paused", "a newly created native job must not be running"
    assert job.name == _token(handle), "it must carry the correlation token while unbound"


@pytest.mark.asyncio
async def test_fence_a_token_named_job_is_never_offered_as_resumable(lab):
    """Core resumes only what the executor offers. It must not be offered."""
    from transfers.models import ExecutionControl
    request = _request(lab)
    handle = lab.executor.prepare(request)
    lab.sab.refuse_renames = True
    started = await lab.executor.start(request, handle)

    observation = await lab.executor._observe(started.handle)
    assert observation.state == ExecutionState.PAUSED
    assert ExecutionControl.RESUME not in observation.controls, \
        "a token-named job must never be advertised as resumable"

    snapshot = await lab.executor.observe_many((started.handle,))
    assert ExecutionControl.RESUME not in snapshot.observations[0].controls


@pytest.mark.asyncio
async def test_fence_a_direct_resume_of_a_token_named_job_is_refused(lab):
    """Defence in depth: the fence does not rely on the caller's restraint."""
    request = _request(lab)
    handle = lab.executor.prepare(request)
    lab.sab.refuse_renames = True
    started = await lab.executor.start(request, handle)

    await lab.executor.resume(started.handle)
    lab.sab.run_native_worker()

    assert _acquired_under_token(lab) == []
    assert next(iter(lab.sab.queue.values())).status == "Paused"


@pytest.mark.asyncio
async def test_fence_the_ordinary_path_introduces_no_visible_pause(lab):
    """Scenario 2: a prompt acknowledgement must still start acquiring."""
    request = _request(lab)
    handle = lab.executor.prepare(request)

    observation = await lab.executor.start(request, handle)

    assert observation.state != ExecutionState.PAUSED, \
        "the fence must not surface as an operator-visible pause"
    job = lab.sab.queue[observation.handle.native["nzo_id"]]
    assert job.status != "Paused", "the job must be running once correctly named"
    assert job.name == RELEASE
    assert len(lab.sab.submissions) == 1


@pytest.mark.asyncio
async def test_fence_a_canonically_paused_execution_is_not_auto_resumed(lab):
    """Scenario 3: the fence releases naming, never the operator's intent."""
    request = _request(lab)
    paused_request = ExecutionRequest(request.work, request.attempt_id, paused=True)
    handle = lab.executor.prepare(paused_request)

    observation = await lab.executor.start(paused_request, handle)

    job = lab.sab.queue[observation.handle.native["nzo_id"]]
    assert job.status == "Paused", "a canonically paused execution must stay paused"
    assert job.name == RELEASE, "but it must still be correctly named"
    assert observation.state == ExecutionState.PAUSED
    lab.sab.run_native_worker()
    assert _acquired_under_token(lab) == []
    assert lab.sab.acquired_under_name == [], "nothing may acquire while DP says paused"


@pytest.mark.asyncio
async def test_fence_a_restart_before_naming_completes_converges(lab, tmp_path):
    """Scenario 4: DebridPulse stops between binding and naming."""
    from transfers.models import ExecutionControl
    request = _request(lab)
    handle = lab.executor.prepare(request)
    lab.sab.refuse_renames = True
    started = await lab.executor.start(request, handle)
    nzo_id = started.handle.native["nzo_id"]
    assert lab.sab.queue[nzo_id].name == _token(handle), "precondition: naming did not complete"

    # A fresh executor over the same durable handle.
    reborn = _build(lab.sab, lab.root, tmp_path)
    fenced = await reborn._observe(started.handle)
    assert ExecutionControl.RESUME not in fenced.controls
    lab.sab.run_native_worker()
    assert _acquired_under_token(lab) == []

    lab.sab.refuse_renames = False
    converged = await reborn._observe(started.handle)
    assert lab.sab.queue[nzo_id].name == RELEASE
    assert ExecutionControl.RESUME in converged.controls, \
        "once correctly named, core may resume it through the ordinary control path"
    assert len(lab.sab.queue) == 1 and len(lab.sab.submissions) == 1


@pytest.mark.asyncio
async def test_fence_holds_when_the_service_is_unreachable(lab):
    """Scenario 5: naming cannot be proven, so nothing may run."""
    request = _request(lab)
    handle = lab.executor.prepare(request)
    lab.sab.refuse_renames = True
    started = await lab.executor.start(request, handle)

    lab.sab.reachable = False
    unknown = await lab.executor._observe(started.handle)
    assert unknown.state == ExecutionState.UNKNOWN
    assert not unknown.controls, "an unobservable job is never advertised as resumable"

    lab.sab.reachable = True
    lab.sab.run_native_worker()
    assert _acquired_under_token(lab) == []
    assert next(iter(lab.sab.queue.values())).status == "Paused"


@pytest.mark.asyncio
async def test_fence_the_completed_payload_is_correct_after_a_lost_acknowledgement(lab):
    """End to end, under the adversarial condition: the filename is right."""
    request = _request(lab)
    handle = lab.executor.prepare(request)
    lab.sab.drop_next_response = True
    lab.sab.worker_runs_during_ambiguity = True

    started = await lab.executor.start(request, handle)
    lab.sab.run_native_worker()          # only now may it run, and only if named

    observation = await lab.executor._observe(started.handle)
    assert observation.state == ExecutionState.SUCCEEDED
    members = sorted(entry.relative_path for entry in observation.materialization.entries)
    assert members == sorted([f"{RELEASE}.mp4", "rename.par2"])
    assert not any(name.startswith(TOKEN_PATTERN) for name in members)
    assert _acquired_under_token(lab) == []
