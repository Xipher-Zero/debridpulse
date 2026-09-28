"""One canonical candidate-activation operation (DP 1.0.12 recovery leveling,
Section 10).

Both automatic recovery
(``transfers.convergence_engine.TransferEngine._apply_recovery_decision``'s
``TRY_ALTERNATE_CANDIDATE`` branch) and operator-requested candidate switch
(``transfers.convergence_engine.TransferEngine.activate_candidate_command``)
call ``activate_candidate`` below. It owns:

- old-writer retirement, when the old writer might still be genuinely active
  (the operator path) as well as when it is already confirmed terminal (the
  automatic path, whose caller already observed this upstream);
- the one writer retirement (``retire_writer``): graceful quiesce, forced
  material checkpoint, then fencing. Which existing material a replacement
  keeps is decided by the one Continuation Planner at the next admission
  (``transfers.continuation``), never by comparing executors or sidecars;
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

from transfers.errors import TransferError
from transfers.filesystem import retire_materialization
from transfers.continuation import parks_on_pause
from transfers.models import ExecutionState, ExecutionSubject, MaterializationKind, TransferCandidate
from transfers.recovery_execution import RecoveryClaim
from transfers.size_evidence import reported_sizes_compatible

_TERMINAL_EXECUTION_STATES = frozenset({
    ExecutionState.FAILED, ExecutionState.ABSENT, ExecutionState.CANCELLED, ExecutionState.SUCCEEDED,
})


@dataclass(frozen=True)
class ActivationResult:
    """Outcome of one ``activate_candidate`` call.

    ``committed`` is the ONLY fact a caller may use to decide whether the
    switch happened. ``reason`` is a short machine token for logging/
    provenance, never for control flow beyond ``committed``. ``retirement``
    is one of ``"not_needed"`` (no prior writer), ``"confirmed"`` (prior
    writer was already terminal before this call, e.g. the automatic path),
    ``"requested_confirmed"`` (this call cancelled a genuinely active writer
    and confirmed retirement), or ``"uncertain"`` (retirement could not be
    confirmed; ``committed`` is always False in that case).
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


async def retire_writer(engine, artifact, old_candidate, replacement_artifact, replacement_candidate, *,
                        boundary: str = "handoff", park: bool = False) -> WriterRetirement:
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

    ``park`` (Pause only): the temporary collection/no-range-export
    compatibility exception (``transfers.continuation.parks_on_pause``) -- such
    a writer is left quiesced rather than cancelled once it is observed paused,
    holding no progress authority while the pause intent stands."""
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
            observed, quiesce = await engine._quiesce_and_checkpoint(current, old_executor, observed,
                                                                     boundary=boundary)
            if (park and observed.state == ExecutionState.PAUSED and old_work is not None
                    and parks_on_pause(old_executor.capabilities, old_work.materialization.kind)):
                await engine.repository.execution(observed)
                await engine.repository.record_material_event(
                    transfer_id, artifact_id, "writer_parked", boundary=boundary, quiesce=quiesce,
                    executor_id=old_executor.descriptor.id, attempt_id=artifact.execution.attempt_id)
                return WriterRetirement("", "parked", "reused", quiesce)
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


async def activate_candidate(
    engine, artifact, target_index: int, *, retry_at: float, claim: RecoveryClaim, error=None,
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

    retired = await retire_writer(engine, artifact, old_candidate, artifact, new_candidate,
                                  boundary=claim.trigger.value)
    retirement, partial_decision = retired.retirement, retired.partial_decision
    if retired.reason:
        return await _record(
            ActivationResult(
                False, retired.reason, old_candidate=old_candidate, new_candidate=new_candidate,
                retirement=retirement, transfer_id=transfer_id, artifact_id=artifact_id,
            ), partial_decision=partial_decision, admission_decision="not_applicable", old_execution_id=old_execution_id,
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
