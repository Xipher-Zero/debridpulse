"""One canonical candidate-activation operation (DP 1.0.12 recovery leveling,
Section 10).

Both automatic recovery
(``transfers.convergence_engine.TransferEngine._apply_recovery_decision``'s
``TRY_ALTERNATE_CANDIDATE`` branch) and operator-requested candidate switch
(``transfers.convergence_engine.TransferEngine.activate_candidate_command``,
and the Resume that completes a paused one,
``TransferEngine._complete_source_transition``) call ``activate_candidate``
below. It owns:

- old-writer retirement, when the old writer might still be genuinely active
  (the operator path) as well as when it is already confirmed terminal (the
  automatic path, whose caller already observed this upstream);
- the one writer retirement (``retire_writer``): graceful quiesce, forced
  material checkpoint, then fencing -- or, when the same executor can keep its
  quiesced native job for the replacement source, the native-state handoff
  (``_hand_off_writer``), which commits the switch together with the new
  writer (while paused, the switch is only a durable desired source that
  Resume completes). Which existing material a replacement keeps is decided
  by the one Continuation Planner (``transfers.continuation``), never by
  comparing executors or sidecars;
- the durable commit, through ``transition_recovery(candidate_switched=True)``
  -- the same gate that already refuses to authorize a new writer before the
  old execution_attempts row is confirmed terminal, and already revokes that
  old row's authorization in the same transaction (Section 27).

This module owns no claim/fence lifecycle itself -- claim acquisition and
completion are the caller's responsibility (Section 11), exactly like every
other recovery mutation in this codebase. It does VERIFY the caller's claim,
as every productive recovery mutation does: the claim must still be current
before any side effect (writer retirement, partial-file retirement) and the
durable commit is atomically fenced by the same token/generation. A
superseded or expired claim therefore cannot switch a candidate.
``activate_candidate`` never raises on a caller-facing "was the switch
accepted" question; it always returns an ``ActivationResult`` so a caller can
implement Section 26's truthful acknowledgement contract without
special-casing exceptions.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError, unknown_failure
from transfers.filesystem import retire_materialization
from transfers.continuation import parks_on_pause
from transfers.models import (
    ContinuationStrategy, ExecutionControl, ExecutionObservation, ExecutionRequest, ExecutionState,
    ExecutionSubject, MaterializationKind, RetargetTruth, TransferCandidate, new_identity,
)
from transfers.recovery_execution import RecoveryClaim
from transfers.size_evidence import reported_sizes_compatible

_TERMINAL_EXECUTION_STATES = frozenset({
    ExecutionState.FAILED, ExecutionState.ABSENT, ExecutionState.CANCELLED, ExecutionState.SUCCEEDED,
})
# Retirements after which the candidate switch is durably committed by the
# native-state handoff itself: the new writer was admitted (and, if its native
# retarget failed, fenced closed) in the handoff transaction.
HANDOFF_RETIREMENTS = frozenset({"handed_off", "handoff_abandoned", "handoff_uncertain"})


@dataclass(frozen=True)
class ActivationResult:
    """Outcome of one ``activate_candidate`` call.

    ``committed`` is the ONLY fact a caller may use to decide whether the
    switch happened. ``reason`` is a short machine token for logging/
    provenance, never for control flow beyond ``committed``. ``retirement``
    is one of ``"not_needed"`` (no prior writer), ``"confirmed"`` (prior
    writer was already terminal before this call, e.g. the automatic path),
    ``"requested_confirmed"`` (this call cancelled a genuinely active writer
    and confirmed retirement), ``"uncertain"`` (retirement could not be
    confirmed; ``committed`` is always False in that case), or one of
    ``HANDOFF_RETIREMENTS`` (the writer's native object was handed to the new
    writer: ``"handed_off"``; its retarget failed and the inherited job was
    removed: ``"handoff_abandoned"``; or its state is still being reconciled
    under the new writer's sole authority: ``"handoff_uncertain"``), or
    ``"desired_source"`` (paused: the parked writer is untouched and Resume
    completes the switch -- ``select_desired_source``).
    """
    committed: bool
    reason: str
    retirement: str = "not_applicable"
    old_candidate: TransferCandidate | None = None
    new_candidate: TransferCandidate | None = None
    artifact_id: int | None = None
    transfer_id: int | None = None
    # DP 1.0.12 recovery leveling, Section 26/29: the durable
    # transfers.repository.TransferRepository.record_candidate_activation
    # provenance write happens AFTER the candidate-selection commit itself,
    # so its own failure is a reconciliation problem, not grounds to turn a
    # committed (or already-decided) activation into a caller-facing
    # exception. False here means exactly that -- ``committed`` is still the
    # only fact governing whether the switch happened.
    provenance_recorded: bool = True


def _index_for(artifact, candidate_id: str) -> int | None:
    wanted = str(candidate_id or "").strip()
    return next(
        (index for index, item in enumerate(artifact.candidates) if str(item.id) == wanted),
        None,
    )


def _source_matches(left, right) -> bool:
    """Match refresh descendants only by provider plus normalized source identity."""
    if left is None or right is None:
        return False
    left_source = getattr(left, "source_identity", None)
    right_source = getattr(right, "source_identity", None)
    return bool(
        left_source is not None
        and right_source is not None
        and str(left.provider_id or "") == str(right.provider_id or "")
        and left_source == right_source
    )


def resolve_candidate_index(artifact, candidate: TransferCandidate) -> int | None:
    """Re-find ``candidate`` on a freshly-read ``artifact``: by exact id first,
    then by provider+source-identity equivalence (a candidate refresh may
    replace the id, or coalesce onto an already-canonical equivalent)."""
    exact = _index_for(artifact, str(candidate.id))
    if exact is not None:
        return exact
    return next(
        (index for index, item in enumerate(artifact.candidates) if _source_matches(item, candidate)),
        None,
    )


@dataclass(frozen=True)
class WriterRetirement:
    """Outcome of ``retire_writer``. ``reason`` is empty exactly when no writer
    is left: there was none, or it is confirmed terminal. Otherwise it names
    why the old writer is still (possibly) productive and nothing was
    detached or retired."""
    reason: str
    retirement: str = "not_needed"
    partial_decision: str = "not_applicable"
    # ``graceful`` (quiesced, then checkpointed), ``forced`` (checkpointed
    # without native quiesce), ``timeout`` (quiesce exceeded the graceful stop
    # timeout: force-fenced, nothing further checkpointed), ``stopped`` or
    # ``not_needed``.
    quiesce: str = "not_needed"


@dataclass(frozen=True)
class NativeHandoff:
    """Ask ``retire_writer`` to hand the retired writer's quiesced native
    object to a new writer for the replacement candidate instead of fencing
    it by cancellation, under ``claim`` (the switch's own recovery claim).
    ``required``: the caller may not fall back to a portable continuation
    that would discard DP-valid material (an operator who confirmed no
    discard); the switch is then refused and the old writer left intact."""
    claim: RecoveryClaim
    required: bool = False


async def retire_writer(engine, artifact, old_candidate, replacement_artifact, replacement_candidate, *,
                        boundary: str = "handoff", park: bool = False,
                        handoff: NativeHandoff | None = None) -> WriterRetirement:
    """THE writer retirement of every execution replacement (Pause, operator
    candidate switch, automatic failover, collection ownership convergence).

    One sequence, whatever the reason: quiesce the writer gracefully where the
    executor can (bounded by the graceful stop timeout), force a material
    checkpoint of the work it has proven written, then fence it -- a genuinely
    active writer is cancelled and must be observed terminal; an
    already-succeeded writer is never retired; an uncertain stop detaches
    nothing. ``boundary`` names the lifecycle boundary in provenance.

    Physical material is not an executor's to keep or lose. For a FILE
    artifact that stays at the same target, the payload stays exactly where it
    is and the next writer's continuation plan (``transfers.continuation``)
    decides what of it is kept (a different executor's private native state is
    discarded when that next writer is admitted). Material is
    retired only when the replacement no longer lives where it was written (or,
    for a collection, which has no range model in v1, when another executor
    or shape takes over) -- and then only material this execution owns. The
    caller holds the recovery claim (or, for Pause, the durable pause fence)
    that fences all of this.

    ``park`` (Pause only): a writer of an executor that resumes its own
    quiesced job (``transfers.continuation.parks_on_pause``) is left quiesced
    rather than cancelled once it is observed paused, holding no progress
    authority while the pause intent stands.

    ``handoff`` (operator source switch): once quiesced and checkpointed, the
    writer's native object is handed to a new writer for the replacement
    candidate (``_hand_off_writer``) when core and the executor both allow it;
    otherwise the writer is fenced as usual (or, when the handoff is
    ``required``, left intact and the switch refused)."""
    transfer_id, artifact_id = artifact.transfer_id, artifact.id
    partial_decision = "not_applicable"
    old_executor = None
    old_work = old_footprint = None
    old_owned = False
    retirement = "not_needed"
    quiesce = "not_needed"
    if artifact.execution is not None:
        old_executor = engine.registry.executor_for_handle(artifact.execution)
        if old_executor is None:
            return WriterRetirement("old_executor_unavailable", "not_applicable")
        if old_candidate is not None:
            old_work = engine._work(artifact, old_candidate)
            old_footprint = engine._footprint(old_executor, old_work)
        old_owned = await engine.repository.execution_owns_target(artifact.execution)
        if await engine.repository.execution_start_pending(artifact.execution.attempt_id):
            # A native start may still be in flight: its dispatcher records the
            # result and converges it against the current intent. Fencing it
            # from here could only orphan a native job that starts afterwards.
            return WriterRetirement("writer_start_in_flight", "uncertain")
        async with engine._convergence_lock(artifact.execution.attempt_id):
            current = await engine._current_artifact(transfer_id, artifact_id)
            if current is None or current.execution != artifact.execution:
                return WriterRetirement("execution_changed_concurrently", "not_applicable")
            observed = await engine._observe_execution(old_executor, artifact.execution)
            if observed.state == ExecutionState.SUCCEEDED:
                await engine.repository.execution(observed)
                return WriterRetirement("writer_already_succeeded", "not_applicable")
            observed, quiesce, checkpointed = await engine._quiesce_and_checkpoint(current, old_executor, observed,
                                                                                   boundary=boundary)
            if park and observed.state == ExecutionState.PAUSED and parks_on_pause(old_executor.capabilities):
                await engine.repository.execution(observed)
                await engine.repository.record_material_event(
                    transfer_id, artifact_id, "writer_parked", boundary=boundary, quiesce=quiesce,
                    checkpointed=checkpointed, native_private_resume=True,
                    executor_id=old_executor.descriptor.id, attempt_id=artifact.execution.attempt_id)
                return WriterRetirement("", "parked", "reused", quiesce)
            if handoff is not None:
                handed = await _hand_off_writer(engine, current, observed, checkpointed, replacement_candidate,
                                                handoff, quiesce)
                if handed is not None:
                    return handed
            if observed.state in _TERMINAL_EXECUTION_STATES:
                # Already confirmed terminal before we ever asked -- the
                # automatic path's caller observed this upstream.
                retirement = "confirmed"
            else:
                # Fence: cancel the (quiesced or still active) writer. The
                # executor reports observed stop truth; an unconfirmed or lost
                # acknowledgement stays uncertain and nothing is detached.
                observed = await engine._cancel_execution(old_executor, artifact.execution)
                retirement = "requested_confirmed" if observed.stopped else "uncertain"
            await engine.repository.execution(observed)
            if observed.state not in _TERMINAL_EXECUTION_STATES or retirement == "uncertain":
                return WriterRetirement("writer_retirement_uncertain", "uncertain", quiesce=quiesce)
            await engine.repository.record_material_event(
                transfer_id, artifact_id, "writer_retired", boundary=boundary, quiesce=quiesce,
                retirement=retirement, executor_id=old_executor.descriptor.id,
                attempt_id=artifact.execution.attempt_id)

    if old_executor is not None and old_work is not None:
        new_executor = engine.registry.executor_for_subject(ExecutionSubject.of(replacement_candidate))
        new_work = engine._work(replacement_artifact, replacement_candidate)
        relocated = old_work.materialization != new_work.materialization
        state = (await engine.repository.material_state(artifact_id)
                 if old_work.materialization.kind == MaterializationKind.COLLECTION and not relocated else None)
        if not relocated and (old_work.materialization.kind == MaterializationKind.FILE
                              or (state is not None and state.members)):
            # The payload stays for the next writer's continuation plan (a
            # collection too, once DebridPulse holds member material for it).
            partial_decision = "reused"
        elif relocated or new_executor is None or old_executor.descriptor.id != new_executor.descriptor.id \
                or old_footprint != engine._footprint(new_executor, new_work):
            if old_owned:
                retire_materialization(engine.root, old_work.materialization, old_footprint, owned=True)
                await engine.repository.invalidate_material(artifact_id, "material_relocated")
                partial_decision = "retired"
            else:
                # Material the retired writer does not durably own (it existed
                # before that execution was admitted) is never deleted.
                partial_decision = "preserved_unowned"
        else:
            partial_decision = "reused"
    return WriterRetirement("", retirement, partial_decision, quiesce)


async def _hand_off_writer(engine, current, observed, checkpointed: bool, replacement_candidate,
                           handoff: NativeHandoff, quiesce: str) -> WriterRetirement | None:
    """THE native-state handoff (inside ``retire_writer``, under the old
    writer's convergence lock, after its quiesce and forced checkpoint).

    Native object continuity is not writer-authority continuity: the old
    attempt keeps its candidate and history and is fenced; a NEW attempt for
    the replacement candidate becomes the next writer generation under a
    ``NATIVE_STATE_HANDOFF`` plan from the one planner, adopting the old
    native object -- both in one transaction that also commits the switch.
    The new attempt stays 'prepared' (not yet established) until the executor
    has positively retargeted the native source; the executor may run the job
    briefly to do so, which is why a handoff happens only while acquisition is
    permitted (a paused switch is completed at Resume). The job is left
    quiesced for the canonical lifecycle owner to resume. What it fetched
    during the retarget is not checkpointed from that transition observation.

    ``None``: no handoff was possible and the caller may fence the writer as
    usual. Before the commit nothing is changed. After it the switch stands:
    a retarget that failed or is uncertain is fenced closed by cancelling the
    inherited job through the new attempt, which alone owns it -- no second
    native writer is ever admitted while its stop is unproven."""
    transfer_id, artifact_id, previous = current.transfer_id, current.id, current.execution
    index = resolve_candidate_index(current, replacement_candidate)
    candidate = current.candidates[index] if index is not None else None
    executor = engine.registry.executor_for_subject(ExecutionSubject.of(candidate)) if candidate else None
    successor = replace(current, selected=index, execution=None) if candidate is not None else None
    handle = request = plan = None
    if (candidate is not None and observed.state == ExecutionState.PAUSED and checkpointed
            and await engine.native_handoff_eligible(current, candidate, executor)):
        attempt_id = new_identity()
        work = engine._work(successor, candidate, attempt_id)
        _state, _facts, plan = await engine._plan_material(successor, candidate, executor, work,
                                                           handoff.claim.trigger.value, native_handoff=True)
        if plan.strategy == ContinuationStrategy.NATIVE_STATE_HANDOFF:
            request = ExecutionRequest(work, attempt_id, continuation=plan)
            handle = await engine._retarget_handle(executor, request, previous)
    if handle is None:
        portable = await engine.preview_continuation(current, candidate, native=False) if candidate else None
        await engine.repository.record_material_event(
            transfer_id, artifact_id, "native_retarget", accepted=False, native_state="abandoned",
            fallback=(portable.strategy.value if portable is not None else "none"), checkpointed=checkpointed,
            quiesce=quiesce, old_attempt_id=previous.attempt_id,
            new_candidate_id=str(candidate.id) if candidate else None,
            discarded_bytes=portable.discarded_bytes if portable is not None else None)
        if handoff.required and (portable is None or portable.discarded_bytes):
            # The operator confirmed no discard: leave the quiesced writer
            # intact; the canonical lifecycle owner resumes or parks it.
            return WriterRetirement("native_handoff_unavailable", "not_applicable", quiesce=quiesce)
        return None
    old_candidate = await engine.writer_candidate(current)
    detail = engine.repository.build_candidate_activation_detail(
        transfer_id=transfer_id, artifact_id=artifact_id, old_candidate=old_candidate, new_candidate=candidate,
        authority=handoff.claim.trigger.value, recovery_generation=handoff.claim.generation,
        old_execution_id=previous.attempt_id, partial_decision="reused", admission_decision="native_handoff",
        outcome="activated",
    )
    detail["new_execution_id"] = handle.attempt_id
    # The observed quiesce is the durable precondition of the handoff.
    await engine.repository.execution(observed)
    accepted_size = current.expected_bytes if current.expected_bytes > 0 else candidate.expected_bytes
    committed = await engine.repository.hand_off_execution(
        successor, previous, handle, plan, activation_provenance=detail, expected_bytes=max(0, accepted_size),
        claim=handoff.claim, handoff={
            "executor_id": executor.descriptor.id, "old_attempt_id": previous.attempt_id,
            "new_attempt_id": handle.attempt_id, "old_candidate_id": str(old_candidate.id),
            "new_candidate_id": str(candidate.id), "strategy": plan.strategy.value, "quiesce": quiesce,
            "valid_bytes": plan.retained_bytes,
        })
    if not committed:
        # Refused atomically (a newer intent, claim or writer won): nothing
        # changed and the quiesced writer is left to the lifecycle owner.
        return WriterRetirement("native_handoff_refused", "not_applicable", quiesce=quiesce)
    try:
        result = await executor.retarget_from(request, handle, previous)
    except Exception as exc:
        result = ExecutionObservation(handle, ExecutionState.UNKNOWN, error=unknown_failure(
            exc, integration_id=executor.descriptor.id, domain=Domain.EXECUTOR, stage=Stage.QUEUE))
    try:
        result = await engine._accept_observation(handle, result)
    except TransferError as exc:
        result = ExecutionObservation(handle, ExecutionState.UNKNOWN, error=exc.error)
    await engine.repository.execution(result)
    # The executor's acknowledgement is not proof: the new attempt gains
    # acquisition authority only once the native source is positively proven.
    # (The switch's caller reports a failed switch to the operator itself.)
    return await reconcile_native_transition(engine, transfer_id, artifact_id, required=handoff.required,
                                             quiesce=quiesce, report=False)


async def reconcile_native_transition(engine, transfer_id: int, artifact_id: int, *, required: bool = True,
                                      quiesce: str = "not_needed", report: bool = True) -> WriterRetirement:
    """THE resolution of a native-state handoff whose source replacement is
    not yet proven (``native_transition_from``). While unresolved the new
    attempt has no start/resume authority, whatever any observation says.

    Under the attempt's convergence lock the inherited job is first quiesced
    if it is acquiring at all, then the executor is asked -- read-only -- which
    source it positively serves (``retarget_truth``):

    - the replacement: the transition is resolved; ordinary authority returns;
    - the previous source: the switch did not happen natively, so ownership is
      restored through the one handoff admission -- a NEW attempt for the
      previous candidate, already proven, adopting the same native object
      (history is never rewritten; the unproven attempt is fenced) -- and,
      with ``report``, the operator is told the switch was not applied;
    - anything else (or a restore that cannot be admitted): the job is
      cancelled through the unproven attempt's own authority and the artifact
      continues portably (held for confirmation when ``required`` and that
      would discard material). A cancellation that is itself unproven leaves
      the attempt current and unresolved, so no second writer is admitted and
      nothing acquires until native truth is known."""
    current = await engine._current_artifact(transfer_id, artifact_id)
    handle = current.execution if current is not None else None
    previous_id = await engine.repository.native_transition_from(handle.attempt_id) if handle else None
    if previous_id is None:
        return WriterRetirement("", "handed_off", "reused", quiesce)
    executor = engine.registry.executor_for_handle(handle)
    if executor is None:
        return WriterRetirement("", "handoff_uncertain", "reused", quiesce)
    attempts = {item.handle.attempt_id: item for item in await engine.repository.executions(transfer_id)}
    original, successor = attempts.get(previous_id), attempts.get(handle.attempt_id)
    truth = RetargetTruth.UNKNOWN
    async with engine._convergence_lock(handle.attempt_id):
        observed = await engine._observe_execution(executor, handle)
        if (observed.state in {ExecutionState.QUEUED, ExecutionState.RUNNING}
                and ExecutionControl.PAUSE in engine._controls(executor, observed)):
            try:
                observed = await engine._accept_observation(handle, await executor.pause(handle))
            except Exception:
                pass
        await engine.repository.execution(observed)
        if (observed.state == ExecutionState.PAUSED and original is not None and original.candidate is not None
                and successor is not None and successor.candidate is not None):
            request = ExecutionRequest(engine._work(current, successor.candidate, handle.attempt_id),
                                       handle.attempt_id,
                                       continuation=await engine.repository.execution_continuation(handle.attempt_id))
            original_request = ExecutionRequest(engine._work(current, original.candidate, previous_id), previous_id)
            try:
                truth = RetargetTruth(await executor.retarget_truth(request, handle, original_request))
            except Exception:
                truth = RetargetTruth.UNKNOWN
        if truth == RetargetTruth.RETARGETED:
            await engine.repository.resolve_native_transition(handle)
            await engine.repository.record_material_event(
                transfer_id, artifact_id, "native_retarget", accepted=True, native_state="reused",
                old_attempt_id=previous_id, new_attempt_id=handle.attempt_id)
            return WriterRetirement("", "handed_off", "reused", quiesce)
    if truth == RetargetTruth.ORIGINAL:
        restored = await _restore_native_source(engine, current, executor, handle, original, quiesce, report)
        if restored is not None:
            return restored
    stopped = await engine._cancel_execution(executor, handle)
    await engine.repository.execution(stopped)
    if not stopped.stopped:
        await engine.repository.record_material_event(
            transfer_id, artifact_id, "native_retarget", accepted=False, native_state="uncertain", fallback="none",
            truth=truth.value, old_attempt_id=previous_id, new_attempt_id=handle.attempt_id)
        return WriterRetirement("", "handoff_uncertain", "reused", quiesce)
    # Proven stopped: nothing native remains to be resolved.
    await engine.repository.resolve_native_transition(handle)
    transfer = await engine.repository.get(transfer_id)
    paused = bool(transfer and transfer.paused) or await engine.repository.globally_paused()
    await engine.repository.detach_retired_writer(artifact_id, handle.attempt_id, state="paused" if paused else "queued")
    candidate = successor.candidate if successor is not None else None
    portable = await engine.preview_continuation(current, candidate, native=False) if candidate else None
    if required and (portable is None or portable.discarded_bytes):
        # The operator chose a switch that discards nothing: the portable
        # continuation that would now discard waits for the ordinary
        # confirmation instead of being admitted automatically.
        await engine.repository.artifact_state(artifact_id, "error", error=NormalizedError(
            Domain.LIFECYCLE, Category.RESOURCE_STATE_CONFLICT, Stage.RECONCILIATION,
            retryability=Retryability.NEVER, operator_action_required=True, integration_id=executor.descriptor.id))
    await engine.repository.record_material_event(
        transfer_id, artifact_id, "native_retarget", accepted=False, native_state="abandoned", fallback="portable",
        truth=truth.value, old_attempt_id=previous_id, new_attempt_id=handle.attempt_id)
    return WriterRetirement("", "handoff_abandoned", "reused", quiesce)


async def _restore_native_source(engine, current, executor, unproven, original, quiesce: str,
                                 report: bool) -> WriterRetirement | None:
    """The inherited job provably still serves the previous source: hand it,
    through the one handoff admission, to a NEW attempt for that previous
    candidate -- admitted already proven, since its source never changed.
    ``None`` when that cannot be admitted (the caller then retires the job)."""
    index = resolve_candidate_index(current, original.candidate)
    if index is None:
        return None
    candidate = current.candidates[index]
    successor = replace(current, selected=index, execution=None)
    attempt_id = new_identity()
    work = engine._work(successor, candidate, attempt_id)
    _state, _facts, plan = await engine._plan_material(successor, candidate, executor, work, "native_restore",
                                                       native_handoff=True)
    if plan.strategy != ContinuationStrategy.NATIVE_STATE_HANDOFF:
        return None
    request = ExecutionRequest(work, attempt_id, continuation=plan)
    handle = await engine._retarget_handle(executor, request, unproven)
    if handle is None:
        return None
    replaced = current.candidates[current.selected]
    detail = engine.repository.build_candidate_activation_detail(
        transfer_id=current.transfer_id, artifact_id=current.id, old_candidate=replaced, new_candidate=candidate,
        authority="native_transition_reconciliation", recovery_generation=None,
        old_execution_id=unproven.attempt_id, partial_decision="reused", admission_decision="native_restore",
        outcome="restored")
    detail["new_execution_id"] = attempt_id
    if not await engine.repository.hand_off_execution(
            successor, unproven, handle, plan, activation_provenance=detail, unresolved=False, handoff={
                "executor_id": executor.descriptor.id, "old_attempt_id": unproven.attempt_id,
                "new_attempt_id": attempt_id, "old_candidate_id": str(replaced.id),
                "new_candidate_id": str(candidate.id), "strategy": plan.strategy.value, "quiesce": quiesce,
                "valid_bytes": plan.retained_bytes, "restored": True}):
        return None
    await engine.repository.execution(await engine._observe_execution(executor, handle))
    await engine.repository.record_material_event(
        current.transfer_id, current.id, "native_retarget", accepted=False, native_state="restored",
        fallback="original_source", old_attempt_id=unproven.attempt_id, new_attempt_id=attempt_id)
    if report:
        await engine.repository.record_manual_candidate_failover(
            transfer_id=current.transfer_id, artifact_id=current.id, filename=current.name,
            requested_candidate_id=str(replaced.id), previous_candidate=candidate, selected_candidate=replaced,
            source_host="", outcome="failure", execution_transition="native_restore", error=NormalizedError(
                Domain.LIFECYCLE, Category.RESOURCE_STATE_CONFLICT, Stage.RECONCILIATION,
                retryability=Retryability.NEVER, operator_action_required=True,
                integration_id=executor.descriptor.id))
    restored = await engine._current_artifact(current.transfer_id, current.id)
    if restored is not None and restored.execution == handle:
        # Proven and ordinary again: the lifecycle owner resumes it (or keeps
        # it parked) exactly as any writer.
        await engine._converge_execution(restored, executor)
    return WriterRetirement("native_retarget_reverted", "handoff_restored", "reused", quiesce)


async def activate_candidate(
    engine, artifact, target_index: int, *, retry_at: float, claim: RecoveryClaim, error=None,
    permit_discard: bool = True,
) -> ActivationResult:
    """Activate ``artifact.candidates[target_index]`` as the selected candidate.

    ``artifact`` must be the caller's own freshly-read current-state Artifact.
    This function re-validates against a fresh read immediately before the
    retirement dance and again immediately before commit, so a concurrent
    mutation is detected rather than silently overwritten.

    ``claim`` (DP 1.0.12 recovery leveling, Section 11) is REQUIRED: the
    caller's ALREADY-HELD ``transfers.recovery_execution.RecoveryClaim``.
    Every candidate activation is attributable to exactly one real recovery
    authority and generation -- there is no claim-less mode. This function
    never acquires or finishes a claim itself; that stays the caller's
    responsibility, exactly like every other recovery mutation. Automatic
    recovery (``transfers.convergence_engine.TransferEngine
    ._apply_recovery_decision``, already running inside the claim
    ``recover_artifact`` acquired for its real trigger -- AUTO_RETRY,
    EXECUTOR_RECOVERY, PROVIDER_RECOVERY, STARTUP_RECONCILE, USER_RETRY, or
    RESUME) passes that SAME claim through, so the activation is attributed
    to its real recovery authority in provenance rather than borrowing the
    operator's identity. The manual path
    (``convergence_engine.TransferEngine.activate_candidate_command``)
    acquires its own fresh USER_CANDIDATE_SWITCH claim -- it is a new
    top-level command, not already inside one -- and passes that instead.

    Anything that is not a real ``RecoveryClaim`` is a programming error and
    raises ``TypeError`` before any state is read or mutated; it is never
    downgraded to a generic authority.
    """
    if not isinstance(claim, RecoveryClaim):
        raise TypeError("activate_candidate requires the caller's real RecoveryClaim")
    transfer_id, artifact_id = artifact.transfer_id, artifact.id
    authority = claim.trigger.value
    recovery_generation = claim.generation

    async def _record(result: ActivationResult, *, partial_decision: str, admission_decision: str, old_execution_id):
        # Section 26 applies to this write too: it happens strictly after
        # ``result`` (in particular ``committed``) was already decided, so
        # its own failure must never turn an already-decided outcome --
        # committed or not -- into a caller-facing exception.
        try:
            await engine.repository.record_candidate_activation(
                transfer_id=transfer_id, artifact_id=artifact_id,
                old_candidate=result.old_candidate, new_candidate=result.new_candidate,
                authority=authority, recovery_generation=recovery_generation,
                old_execution_id=old_execution_id, partial_decision=partial_decision,
                admission_decision=admission_decision, outcome=result.reason,
            )
        except Exception:
            return replace(result, provenance_recorded=False)
        return result

    # The claim is authority, not just provenance: a claim that was superseded by a newer generation, or whose
    # lease expired, must not retire a writer, retire partial bytes, or switch a candidate.
    if not await engine.repository.recovery_claim_current(claim, now=engine.clock()):
        return await _record(
            ActivationResult(False, "claim_not_current", transfer_id=transfer_id, artifact_id=artifact_id),
            partial_decision="not_applicable", admission_decision="not_applicable", old_execution_id=None,
        )

    if (
        target_index is None
        or not (0 <= target_index < len(artifact.candidates))
        or target_index == artifact.selected
    ):
        return await _record(
            ActivationResult(False, "invalid_target", transfer_id=transfer_id, artifact_id=artifact_id),
            partial_decision="not_applicable", admission_decision="not_applicable", old_execution_id=None,
        )

    new_candidate = artifact.candidates[target_index]
    old_candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
    old_execution_id = artifact.execution.attempt_id if artifact.execution is not None else None
    partial_decision = "not_applicable"
    # Section 13: was the old writer, right now, genuinely occupying one of
    # the states the admission gate counts as an active slot? Computed BEFORE
    # any retirement mutation -- retirement itself changes the execution's
    # state away from these values, so this must be captured first.
    was_occupying_slot = False
    if artifact.execution is not None:
        live = await engine.repository.live_executions()
        was_occupying_slot = any(
            item.handle.attempt_id == artifact.execution.attempt_id
            and item.state in {"prepared", "queued", "running", "unknown"}
            for item in live
        )
    if (
        artifact.expected_bytes > 0
        and new_candidate.expected_bytes > 0
        and not reported_sizes_compatible(artifact.expected_bytes, new_candidate.expected_bytes)
    ):
        return await _record(
            ActivationResult(
                False, "size_mismatch", old_candidate=old_candidate, new_candidate=new_candidate,
                transfer_id=transfer_id, artifact_id=artifact_id,
            ), partial_decision=partial_decision, admission_decision="not_applicable", old_execution_id=old_execution_id,
        )
    # Requested-replacement eligibility (Section 10 item 3): the same
    # provider-neutral route-binding gate transfers._engine_recovery
    # .TransferEngine._next_alternate_index already filters automatic
    # candidates through before ever offering one here. An operator-named
    # target has not been pre-filtered, so it is re-checked unconditionally;
    # for an automatic target this is a cheap, idempotent re-confirmation.
    origin = await engine.canonical.origin_for(artifact, new_candidate)
    if origin is None:
        return await _record(
            ActivationResult(
                False, "candidate_route_unbound", old_candidate=old_candidate, new_candidate=new_candidate,
                transfer_id=transfer_id, artifact_id=artifact_id,
            ), partial_decision=partial_decision, admission_decision="not_applicable", old_execution_id=old_execution_id,
        )
    # Administrative disablement/health of the NEW candidate's provider is an
    # explicit hard stop -- this never reopens provider competition for an
    # already-persisted route (transfers.registry.IntegrationRegistry
    # .provider_for_bound_route), it only confirms the bound owner is
    # currently usable.
    try:
        engine.registry.provider_for_bound_route(new_candidate.provider_id, origin.request.request)
    except TransferError:
        return await _record(
            ActivationResult(
                False, "candidate_provider_unavailable", old_candidate=old_candidate, new_candidate=new_candidate,
                transfer_id=transfer_id, artifact_id=artifact_id,
            ), partial_decision=partial_decision, admission_decision="not_applicable", old_execution_id=old_execution_id,
        )

    # A writer whose native object the replacement's writer can inherit is
    # handed off instead of cancelled (``permit_discard``: whether the caller
    # may fall back to a portable continuation that discards material).
    handoff = None
    if artifact.execution is not None and await engine.native_handoff_eligible(
            artifact, new_candidate, engine.registry.executor_for_subject(ExecutionSubject.of(new_candidate))):
        transfer = await engine.repository.get(transfer_id)
        paused = bool(transfer and transfer.paused) or await engine.repository.globally_paused()
        parked = await engine.repository.previous_writer(artifact_id)
        if not paused:
            handoff = NativeHandoff(claim, required=not permit_discard)
        elif (parked is not None and parked.handle == artifact.execution and parked.state == "paused"
                and await engine.native_retarget_available(artifact, new_candidate)):
            # Paused, with a parked writer: a durable desired-source transition
            # -- no native mutation, no new writer authority, no discard.
            # Resume completes it; switching again only changes the desire.
            # (Otherwise the writer is retired below, never handed off while
            # acquisition is paused.)
            writer = await engine.writer_candidate(artifact)
            withdrawn = writer is not None and resolve_candidate_index(artifact, writer) == target_index
            committed = writer is not None and await engine.repository.select_desired_source(
                artifact_id, artifact.execution, target_index, claim=claim,
                activation_provenance=engine.repository.build_candidate_activation_detail(
                    transfer_id=transfer_id, artifact_id=artifact_id, old_candidate=old_candidate,
                    new_candidate=new_candidate, authority=authority, recovery_generation=recovery_generation,
                    old_execution_id=old_execution_id, partial_decision="reused",
                    admission_decision="source_transition_withdrawn" if withdrawn else "source_transition_pending",
                    outcome="activated"),
                transition={"transition": "withdrawn" if withdrawn else "pending",
                            "from_candidate_id": str(writer.id) if writer is not None else None,
                            "to_candidate_id": str(new_candidate.id)})
            return ActivationResult(
                committed, "activated" if committed else "commit_conflict", retirement="desired_source",
                old_candidate=old_candidate, new_candidate=new_candidate, transfer_id=transfer_id,
                artifact_id=artifact_id,
            )
    retired = await retire_writer(engine, artifact, old_candidate, artifact, new_candidate,
                                  boundary=claim.trigger.value, handoff=handoff)
    retirement, partial_decision = retired.retirement, retired.partial_decision
    if retired.reason:
        return await _record(
            ActivationResult(
                False, retired.reason, old_candidate=old_candidate, new_candidate=new_candidate,
                retirement=retirement, transfer_id=transfer_id, artifact_id=artifact_id,
            ), partial_decision=partial_decision, admission_decision="not_applicable", old_execution_id=old_execution_id,
        )
    if retirement in HANDOFF_RETIREMENTS:
        # Committed by the handoff transaction itself, provenance included.
        current = await engine._current_artifact(transfer_id, artifact_id)
        activated = current.candidates[current.selected] if current is not None else new_candidate
        await engine.repository.record_candidate_attempt(artifact_id, str(activated.id), *(
            (str(old_candidate.id),) if old_candidate is not None else ()))
        if retirement == "handed_off" and current is not None and current.execution is not None:
            # The inherited job is quiesced: the canonical lifecycle owner
            # resumes it now if the intent is RUNNING, or keeps it parked.
            executor = engine.registry.executor_for_handle(current.execution)
            if executor is not None:
                await engine._converge_execution(current, executor)
        return ActivationResult(
            True, "activated", old_candidate=old_candidate, new_candidate=activated,
            retirement=retirement, transfer_id=transfer_id, artifact_id=artifact_id,
        )

    current = await engine._current_artifact(transfer_id, artifact_id)
    if current is None:
        return await _record(
            ActivationResult(
                False, "artifact_disappeared", old_candidate=old_candidate, new_candidate=new_candidate,
                retirement=retirement, transfer_id=transfer_id, artifact_id=artifact_id,
            ), partial_decision=partial_decision, admission_decision="not_applicable", old_execution_id=old_execution_id,
        )
    resolved_index = resolve_candidate_index(current, new_candidate)
    if resolved_index is None:
        return await _record(
            ActivationResult(
                False, "candidate_no_longer_present", old_candidate=old_candidate, new_candidate=new_candidate,
                retirement=retirement, transfer_id=transfer_id, artifact_id=artifact_id,
            ), partial_decision=partial_decision, admission_decision="not_applicable", old_execution_id=old_execution_id,
        )
    resolved_candidate = current.candidates[resolved_index]
    accepted_size = current.expected_bytes if current.expected_bytes > 0 else resolved_candidate.expected_bytes
    # Section 13: an artifact that genuinely held a live slot before its old
    # writer was retired above keeps a bounded, durable continuation
    # reservation across this same commit, so unrelated queued work cannot
    # steal the slot before the replacement dispatches. An artifact that was
    # NOT actively occupying a slot gains no priority merely from switching.
    admission_decision = "not_needed"
    continuation_reservation_until = None
    if was_occupying_slot:
        continuation_reservation_until = engine.clock() + max(300.0, float(engine.policy.max_retry_delay))
        admission_decision = "reserved"
    # Section 26/29: the provenance record is built BEFORE the commit and
    # handed to transition_recovery so it is written in the SAME transaction
    # as the candidate-selection commit itself -- "the switch committed but
    # its provenance was lost" is structurally impossible for this path,
    # rather than merely caught-and-flagged after the fact.
    activation_detail = engine.repository.build_candidate_activation_detail(
        transfer_id=transfer_id, artifact_id=artifact_id,
        old_candidate=old_candidate, new_candidate=resolved_candidate,
        authority=authority, recovery_generation=recovery_generation,
        old_execution_id=old_execution_id, partial_decision=partial_decision,
        admission_decision=admission_decision, outcome="activated",
    )
    try:
        committed = await engine.repository.transition_recovery(
            current.id, "queued", error=error, retry_at=retry_at,
            selected=resolved_index, expected_bytes=max(0, accepted_size),
            candidate_switched=True, clear_quiescence=True,
            continuation_reservation_until=continuation_reservation_until,
            activation_provenance=activation_detail,
            claim=claim,
        )
    except Exception:
        # The provenance write is now part of THIS SAME transaction: if it
        # (or anything else in the transaction) raises, the whole commit
        # rolls back -- correctly, nothing durably happened -- but this
        # function still never raises on the caller-facing "was the switch
        # accepted" question (module docstring); a graceful, real rejection
        # is reported instead of an uncaught exception.
        committed = False
    if not committed:
        return await _record(
            ActivationResult(
                False, "commit_conflict", old_candidate=old_candidate, new_candidate=resolved_candidate,
                retirement=retirement, transfer_id=transfer_id, artifact_id=artifact_id,
            ), partial_decision=partial_decision, admission_decision="not_applicable", old_execution_id=old_execution_id,
        )
    # Section 12: both the candidate just abandoned and the one just
    # activated are now durably "tried" for this recovery episode, so a
    # later automatic search never cycles back to either -- regardless of
    # index order. Best-effort relative to the commit above (the commit
    # itself, not this bookkeeping, is what "activation succeeded" means;
    # see Section 26).
    attempted_ids = [str(resolved_candidate.id)]
    if old_candidate is not None:
        attempted_ids.append(str(old_candidate.id))
    await engine.repository.record_candidate_attempt(current.id, *attempted_ids)
    # provenance_recorded is unconditionally True here: it was written
    # atomically with the commit above, not as a separate step that could
    # independently fail.
    return ActivationResult(
        True, "activated", old_candidate=old_candidate, new_candidate=resolved_candidate,
        retirement=retirement, transfer_id=transfer_id, artifact_id=artifact_id,
    )
