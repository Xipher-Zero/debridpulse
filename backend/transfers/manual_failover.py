"""Provider-neutral operator-triggered failover to one exact canonical candidate.

The actual activation mutation (writer retirement, partial-file/resume policy,
and the durable commit) is owned entirely by
``transfers.candidate_activation.activate_candidate`` -- the SAME operation
automatic Phase-3 recovery's ``TRY_ALTERNATE_CANDIDATE`` decision uses (DP
1.0.12 recovery leveling, Section 10). This module owns only what is
genuinely specific to an operator naming an exact candidate: resolving the
requested candidate id, the pre-activation expiry-refresh step, claim
acquisition/fencing (Section 11, via
``transfers.convergence_engine.TransferEngine.activate_candidate_command``),
UI-facing transition provenance, and the truthful post-commit acknowledgement
contract (Section 26).
"""
from __future__ import annotations

from dataclasses import replace

from transfers.candidate_activation import resolve_candidate_index
from transfers.contracts import CandidateRefresh
from transfers.errors import (
    Category,
    Domain,
    NormalizedError,
    Origin,
    Retryability,
    Stage,
    TransferError,
    unknown_failure,
)
from transfers.models import ResolutionResult, ResourceState


# Canonical candidate-switch lifecycle-eligibility owner (DP 1.0.12 recovery
# leveling, Section 31 corrective pass). This is the ONE set of artifact
# lifecycle states in which a candidate switch is ever permitted -- both the
# command's own precondition below and every presentation projection
# (``transfers.repository._SWITCHABLE_ARTIFACT_STATES``,
# ``transfers.presentation_repository._SWITCHABLE_STATES``, and
# ``api.operational_downloads._SWITCHABLE_STATES_SQL``, which is derived from
# ``transfers.repository._SWITCHABLE_ARTIFACT_STATES``) import THIS frozenset
# rather than defining their own. There is no second literal anywhere in the
# codebase; a presentation projection that merely re-declared an equal-valued
# set would still be a second authority free to drift the next time this set
# changes -- delegation, not coincidental equality, is what keeps them
# actually in step. See
# ``tests/test_manual_candidate_failover.py::
# test_switch_eligible_lifecycle_states_delegate_to_the_canonical_owner``.
SWITCH_ELIGIBLE_LIFECYCLE_STATES = frozenset({
    "pending", "processing", "ready", "queued", "downloading", "paused",
    "refresh_pending", "error",
})


def _error(
    category: Category,
    stage: Stage,
    *,
    domain: Domain = Domain.LIFECYCLE,
    retryability: Retryability = Retryability.NEVER,
    integration_id: str = "",
) -> TransferError:
    return TransferError(NormalizedError(
        domain,
        category,
        stage,
        retryability=retryability,
        origin=Origin.CORE,
        operator_action_required=True,
        integration_id=integration_id,
    ))


def _index_for(artifact, candidate_id: str) -> int | None:
    wanted = str(candidate_id or "").strip()
    return next(
        (index for index, item in enumerate(artifact.candidates) if str(item.id) == wanted),
        None,
    )


async def _bound_provider(engine, artifact, candidate):
    """Resolve the candidate's persisted route owner without reopening competition."""
    origin = await engine.canonical.origin_for(artifact, candidate)
    if origin is None:
        raise _error(Category.OWNERSHIP_CONFLICT, Stage.CANDIDATE_PREPARATION)
    provider = engine.registry.provider_for_bound_route(
        candidate.provider_id,
        origin.request.request,
    )
    return origin, provider


async def _refresh_exact(engine, artifact, index: int):
    """Refresh only the source explicitly named by the operator."""
    candidate = artifact.candidates[index]
    origin, provider = await _bound_provider(engine, artifact, candidate)
    if not isinstance(provider, CandidateRefresh):
        raise _error(
            Category.CANDIDATE_EXPIRED,
            Stage.CANDIDATE_PREPARATION,
            domain=Domain.RESOLUTION,
            retryability=Retryability.AFTER_RERESOLUTION,
            integration_id=candidate.provider_id,
        )

    attempt = None
    try:
        attempt = await engine.repository.begin_refresh(
            origin.request,
            provider.descriptor.id,
        )
        bound = replace(candidate, refresh_request=origin.request.request)
        result = engine._authoritative_provider_result(
            provider.descriptor.id,
            await provider.refresh(bound),
            request_kind=origin.request.request.kind,
        )
        live = await engine.repository.resolution(attempt, result)
        if not live and origin.request.transfer_id == artifact.transfer_id:
            raise _error(Category.OWNERSHIP_CONFLICT, Stage.CANDIDATE_PREPARATION)
        if result.error:
            raise TransferError(result.error)
        if not result.candidates:
            raise _error(
                Category.NO_TRANSFER_CANDIDATE,
                Stage.CANDIDATE_PREPARATION,
                domain=Domain.RESOLUTION,
            )
        if any(
            item.expires_at is not None and item.expires_at <= engine.clock()
            for item in result.candidates
        ):
            raise _error(
                Category.CANDIDATE_EXPIRED,
                Stage.CANDIDATE_PREPARATION,
                domain=Domain.RESOLUTION,
                retryability=Retryability.AFTER_RERESOLUTION,
            )
        if not await engine.canonical.refresh_candidate(
            artifact,
            origin,
            candidate,
            result.candidates,
        ):
            raise _error(Category.OWNERSHIP_CONFLICT, Stage.CANDIDATE_PREPARATION)
    except TransferError:
        raise
    except Exception as exc:
        error = unknown_failure(
            exc,
            integration_id=provider.descriptor.id,
            domain=Domain.PROVIDER,
            stage=Stage.CANDIDATE_PREPARATION,
        )
        if attempt is not None:
            await engine.repository.resolution(
                attempt,
                ResolutionResult(ResourceState.UNKNOWN, error=error),
            )
        raise TransferError(error) from exc

    current = await engine._current_artifact(artifact.transfer_id, artifact.id)
    if current is None:
        raise _error(Category.OWNERSHIP_CONFLICT, Stage.CANDIDATE_PREPARATION)

    # Candidate refresh may replace the ID, or coalesce the refreshed route into
    # an already-canonical equivalent source. Preserve affinity to the requested
    # source rather than falling back to an unrelated candidate at the old index.
    for replacement in result.candidates:
        resolved = resolve_candidate_index(current, replacement)
        if resolved is not None:
            return current, resolved

    raise _error(Category.OWNERSHIP_CONFLICT, Stage.CANDIDATE_PREPARATION)


async def _record_failure(
    engine,
    *,
    transfer_id: int,
    artifact,
    requested_candidate_id: str,
    selected_candidate=None,
    previous_candidate=None,
    error: NormalizedError,
) -> None:
    if artifact is None:
        return
    await engine.repository.record_manual_candidate_failover(
        transfer_id=int(transfer_id),
        artifact_id=int(artifact.id),
        filename=str(artifact.name or "artifact"),
        requested_candidate_id=str(requested_candidate_id or ""),
        previous_candidate=previous_candidate,
        selected_candidate=selected_candidate,
        source_host="",
        outcome="failure",
        execution_transition="unchanged",
        error=error,
    )


# Section 26: an activation reason that reached a durable commit must never be
# reported back to the caller as a plain failure -- only these reasons can
# ever precede a commit attempt at all; every one of them means nothing was
# written.
_NOT_COMMITTED_ERROR = {
    "invalid_target": lambda: _error(Category.RESOURCE_STATE_CONFLICT, Stage.CANDIDATE_PREPARATION),
    "size_mismatch": lambda: _error(Category.SIZE_MISMATCH, Stage.CANDIDATE_PREPARATION, domain=Domain.INTEGRITY),
    "candidate_route_unbound": lambda: _error(Category.OWNERSHIP_CONFLICT, Stage.CANDIDATE_PREPARATION),
    "candidate_provider_unavailable": lambda: _error(
        Category.PROVIDER_UNAVAILABLE, Stage.CANDIDATE_PREPARATION, domain=Domain.PROVIDER,
        retryability=Retryability.BACKOFF,
    ),
    "old_executor_unavailable": lambda: _error(Category.EXECUTOR_UNAVAILABLE, Stage.RECONCILIATION, domain=Domain.EXECUTOR),
    "execution_changed_concurrently": lambda: _error(Category.RESOURCE_STATE_CONFLICT, Stage.RECONCILIATION),
    "writer_already_succeeded": lambda: _error(Category.RESOURCE_STATE_CONFLICT, Stage.RECONCILIATION),
    "writer_retirement_uncertain": lambda: _error(Category.RECONCILIATION_FAILED, Stage.RECONCILIATION, domain=Domain.RECONCILIATION),
    "artifact_disappeared": lambda: _error(Category.RESOURCE_NOT_FOUND, Stage.RECONCILIATION, domain=Domain.REQUEST),
    "candidate_no_longer_present": lambda: _error(Category.OWNERSHIP_CONFLICT, Stage.RECONCILIATION),
    "commit_conflict": lambda: _error(Category.RESOURCE_STATE_CONFLICT, Stage.RECONCILIATION),
    "not_found": lambda: _error(Category.RESOURCE_NOT_FOUND, Stage.CANDIDATE_PREPARATION, domain=Domain.REQUEST),
}


async def manual_candidate_failover(
    engine,
    transfer_id: int,
    artifact_id: int,
    candidate_id: str,
) -> dict:
    """Make one existing candidate authoritative without creating a new artifact."""
    wanted = str(candidate_id or "").strip()
    if not wanted:
        raise _error(
            Category.INVALID_REQUEST,
            Stage.CANDIDATE_PREPARATION,
            domain=Domain.REQUEST,
        )

    transfer = await engine.repository.get(int(transfer_id))
    if transfer is None:
        raise _error(Category.RESOURCE_NOT_FOUND, Stage.CANDIDATE_PREPARATION, domain=Domain.REQUEST)
    artifact = await engine._current_artifact(int(transfer_id), int(artifact_id))
    if artifact is None:
        raise _error(Category.RESOURCE_NOT_FOUND, Stage.CANDIDATE_PREPARATION, domain=Domain.REQUEST)

    old_candidate = None
    candidate = None
    claim_result = None
    try:
        index = _index_for(artifact, wanted)
        if index is None:
            raise _error(Category.SOURCE_NOT_FOUND, Stage.CANDIDATE_PREPARATION, domain=Domain.REQUEST)
        if artifact.state not in SWITCH_ELIGIBLE_LIFECYCLE_STATES or len(artifact.candidates) < 2:
            raise _error(Category.RESOURCE_STATE_CONFLICT, Stage.CANDIDATE_PREPARATION)
        if index == artifact.selected:
            raise _error(Category.RESOURCE_STATE_CONFLICT, Stage.CANDIDATE_PREPARATION)

        old_candidate = artifact.candidates[artifact.selected]
        candidate = artifact.candidates[index]
        if candidate.expires_at is not None and candidate.expires_at <= engine.clock():
            artifact, index = await _refresh_exact(engine, artifact, index)
            candidate = artifact.candidates[index]

        claim_result = await engine.activate_candidate_command(int(transfer_id), int(artifact_id), index)
        if claim_result is None:
            # A concurrent AUTO_RETRY/USER_RETRY/RESUME/scheduler recovery
            # currently owns this artifact's claim (Section 11). Nothing was
            # attempted; this is an ordinary, retryable "busy" failure, not a
            # committed-then-reported-as-failed outcome.
            raise _error(
                Category.RESOURCE_STATE_CONFLICT,
                Stage.RECONCILIATION,
                retryability=Retryability.IMMEDIATE,
            )
        if not claim_result.committed:
            build_error = _NOT_COMMITTED_ERROR.get(claim_result.reason)
            raise (build_error() if build_error else _error(Category.RESOURCE_STATE_CONFLICT, Stage.RECONCILIATION))
    except TransferError as exc:
        await _record_failure(
            engine, transfer_id=transfer_id, artifact=artifact, requested_candidate_id=wanted,
            selected_candidate=(claim_result.new_candidate if claim_result else None) or candidate,
            previous_candidate=(claim_result.old_candidate if claim_result else None) or old_candidate,
            error=exc.error,
        )
        raise

    # ACTIVATION_COMMITTED (Section 26): from here on, the switch itself is
    # durably true. Every remaining step -- refetching current state for
    # presentation, writing durable success provenance, and re-aggregating
    # the parent -- is a best-effort reconciliation concern. None of them may
    # retroactively turn this into a reported failure: that would fabricate
    # rollback of an already-retired writer and invite an unsafe duplicate
    # retry of a switch that genuinely succeeded.
    activated_candidate = claim_result.new_candidate
    source = activated_candidate.source_identity
    host = (
        str(source.key).lower().removeprefix("www.").rstrip(".")
        if source is not None and str(source.scope) == "host"
        else "source"
    )
    result = {
        "ok": True,
        "transfer_id": int(transfer_id),
        "artifact_id": int(artifact_id),
        "filename": artifact.name,
        "candidate_id": str(activated_candidate.id),
        "source_host": host,
        "provider_id": activated_candidate.provider_id,
    }
    # The canonical activation provenance write (transfers.candidate_activation
    # .activate_candidate's own record_candidate_activation call) already
    # happened before this function ever saw claim_result -- Section 26
    # applies to it the identical way; claim_result.provenance_recorded is
    # its own truthful "did that succeed" flag, surfaced here rather than
    # silently dropped.
    reconciliation_pending = not claim_result.provenance_recorded

    try:
        current = await engine._current_artifact(int(transfer_id), int(artifact_id))
        if current is not None:
            result["filename"] = current.name
    except Exception:
        reconciliation_pending = True

    try:
        await engine.repository.record_manual_candidate_failover(
            transfer_id=int(transfer_id),
            artifact_id=int(artifact_id),
            filename=result["filename"],
            requested_candidate_id=wanted,
            previous_candidate=claim_result.old_candidate,
            selected_candidate=activated_candidate,
            source_host=host,
            outcome="success",
            execution_transition=(
                "retired_and_redispatch" if claim_result.retirement != "not_needed" else "queued_for_selected_candidate"
            ),
            error=None,
        )
    except Exception:
        reconciliation_pending = True

    try:
        await engine._aggregate(int(transfer_id))
    except Exception:
        reconciliation_pending = True

    if reconciliation_pending:
        result["reconciliation_pending"] = True
    return result
