"""Provider-neutral operator-triggered failover to one exact canonical candidate."""
from __future__ import annotations

import asyncio
from dataclasses import replace

from transfers.contracts import CandidateRefresh
from transfers.errors import (
    Category,
    Domain,
    NormalizedError,
    Origin,
    Recovery,
    Retryability,
    Stage,
    TransferError,
    unknown_failure,
)
from transfers.filesystem import retire_partial
from transfers.mirrors import reported_sizes_compatible
from transfers.models import (
    ExecutionState,
    OutcomeKind,
    ResolutionResult,
    ResourceState,
)


_OPERATIONAL_STATES = frozenset({
    "pending", "processing", "ready", "queued", "downloading", "paused",
    "refresh_pending", "error",
})
_POST_RETIREMENT_STATES = _OPERATIONAL_STATES | frozenset({"cancelled", "lost"})
_TERMINAL_EXECUTION_STATES = frozenset({
    ExecutionState.CANCELLED,
    ExecutionState.FAILED,
    ExecutionState.ABSENT,
})


def _error(
    category: Category,
    stage: Stage,
    *,
    domain: Domain = Domain.LIFECYCLE,
    retryability: Retryability = Retryability.NEVER,
    recovery: Recovery = Recovery.FAIL,
    integration_id: str = "",
) -> TransferError:
    return TransferError(NormalizedError(
        domain,
        category,
        stage,
        retryability=retryability,
        recovery=recovery,
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
            recovery=Recovery.REQUIRE_OPERATOR,
            integration_id=candidate.provider_id,
        )

    attempt = None
    try:
        bound = replace(candidate, refresh_request=origin.request.request)
        attempt = await engine.repository.begin_refresh(
            origin.request,
            provider.descriptor.id,
        )
        result = engine._authoritative_provider_result(
            provider.descriptor.id,
            await provider.refresh(bound),
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
                recovery=Recovery.REQUIRE_OPERATOR,
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
        exact = _index_for(current, str(replacement.id))
        if exact is not None:
            return current, exact
        equivalent = next(
            (
                current_index
                for current_index, item in enumerate(current.candidates)
                if _source_matches(item, replacement)
            ),
            None,
        )
        if equivalent is not None:
            return current, equivalent

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

    # The execution-cycle lock excludes scheduler reconciliation while the
    # transfer lock excludes retry/cancel and a second manual switch.
    async with engine._execution_cycle_lock:
        lock = engine._transfer_locks.setdefault(int(transfer_id), asyncio.Lock())
        async with lock:
            artifact = None
            candidate = None
            old_candidate = None
            try:
                transfer = await engine.repository.get(int(transfer_id))
                if transfer is None:
                    raise _error(
                        Category.RESOURCE_NOT_FOUND,
                        Stage.CANDIDATE_PREPARATION,
                        domain=Domain.REQUEST,
                    )
                artifact = await engine._current_artifact(
                    int(transfer_id),
                    int(artifact_id),
                )
                if artifact is None:
                    raise _error(
                        Category.RESOURCE_NOT_FOUND,
                        Stage.CANDIDATE_PREPARATION,
                        domain=Domain.REQUEST,
                    )

                index = _index_for(artifact, wanted)
                if index is None:
                    raise _error(
                        Category.SOURCE_NOT_FOUND,
                        Stage.CANDIDATE_PREPARATION,
                        domain=Domain.REQUEST,
                    )
                if (
                    artifact.state not in _OPERATIONAL_STATES
                    or len(artifact.candidates) < 2
                ):
                    raise _error(
                        Category.RESOURCE_STATE_CONFLICT,
                        Stage.CANDIDATE_PREPARATION,
                    )
                if index == artifact.selected:
                    raise _error(
                        Category.RESOURCE_STATE_CONFLICT,
                        Stage.CANDIDATE_PREPARATION,
                    )

                old_candidate = artifact.candidates[artifact.selected]
                candidate = artifact.candidates[index]
                if (
                    candidate.expires_at is not None
                    and candidate.expires_at <= engine.clock()
                ):
                    artifact, index = await _refresh_exact(engine, artifact, index)
                    candidate = artifact.candidates[index]

                # Bound-route validation is the existing provider-neutral
                # enablement/health/capability gate. It never reselects a provider.
                await _bound_provider(engine, artifact, candidate)
                new_executor = engine.registry.executor_for(candidate)
                if (
                    artifact.expected_bytes > 0
                    and candidate.expected_bytes > 0
                    and not reported_sizes_compatible(
                        artifact.expected_bytes,
                        candidate.expected_bytes,
                    )
                ):
                    raise _error(
                        Category.SIZE_MISMATCH,
                        Stage.CANDIDATE_PREPARATION,
                        domain=Domain.INTEGRITY,
                    )

                old_executor = None
                old_sidecars = ()
                had_execution = artifact.execution is not None
                if artifact.execution is not None:
                    old_executor = engine.registry.executors.get(
                        artifact.execution.executor_id
                    )
                    if old_executor is None:
                        raise _error(
                            Category.EXECUTOR_UNAVAILABLE,
                            Stage.RECONCILIATION,
                            domain=Domain.EXECUTOR,
                        )
                    old_sidecars = old_executor.resumable_paths(artifact.target)
                    async with engine._convergence_lock(
                        artifact.execution.attempt_id
                    ):
                        current = await engine._current_artifact(
                            transfer_id,
                            artifact_id,
                        )
                        if (
                            current is None
                            or current.execution != artifact.execution
                        ):
                            raise _error(
                                Category.RESOURCE_STATE_CONFLICT,
                                Stage.RECONCILIATION,
                            )
                        observed = await old_executor.observe(artifact.execution)
                        if observed.state == ExecutionState.SUCCEEDED:
                            await engine.repository.execution(observed)
                            raise _error(
                                Category.RESOURCE_STATE_CONFLICT,
                                Stage.RECONCILIATION,
                            )
                        if observed.state not in _TERMINAL_EXECUTION_STATES:
                            outcome = await old_executor.cancel(artifact.execution)
                            if (
                                outcome.error is not None
                                or outcome.kind
                                not in {OutcomeKind.CANCELLED, OutcomeKind.SUCCESS}
                            ):
                                if outcome.error is not None:
                                    raise TransferError(outcome.error)
                                raise _error(
                                    Category.RECONCILIATION_FAILED,
                                    Stage.RECONCILIATION,
                                    domain=Domain.RECONCILIATION,
                                )
                            observed = await old_executor.observe(artifact.execution)
                            # Some executors, including external aria2 daemons,
                            # may forget a force-removed job immediately. Once this
                            # exact command has successfully cancelled a live writer,
                            # post-cancel absence is evidence of retirement, not an
                            # orphaned/failed source. Pre-existing ABSENT/FAILED
                            # observations are left untouched and remain truthful.
                            if observed.state == ExecutionState.ABSENT:
                                observed = replace(
                                    observed,
                                    state=ExecutionState.CANCELLED,
                                    error=None,
                                )
                        await engine.repository.execution(observed)
                        if observed.state not in _TERMINAL_EXECUTION_STATES:
                            raise _error(
                                Category.RECONCILIATION_FAILED,
                                Stage.RECONCILIATION,
                                domain=Domain.RECONCILIATION,
                            )

                # Reuse partial state only when the same executor owns the same
                # resumable sidecar contract. Otherwise integrity wins.
                new_sidecars = new_executor.resumable_paths(artifact.target)
                if old_executor is not None and (
                    old_executor.descriptor.id != new_executor.descriptor.id
                    or tuple(old_sidecars) != tuple(new_sidecars)
                ):
                    retire_partial(engine.root, artifact.target, old_sidecars)

                current = await engine._current_artifact(transfer_id, artifact_id)
                if current is None or current.state not in _POST_RETIREMENT_STATES:
                    raise _error(
                        Category.RESOURCE_STATE_CONFLICT,
                        Stage.RECONCILIATION,
                    )
                index = _index_for(current, str(candidate.id))
                if index is None:
                    # A refresh may have coalesced by source identity.
                    index = next(
                        (
                            current_index
                            for current_index, item in enumerate(current.candidates)
                            if _source_matches(item, candidate)
                        ),
                        None,
                    )
                if index is None:
                    raise _error(
                        Category.OWNERSHIP_CONFLICT,
                        Stage.RECONCILIATION,
                    )
                candidate = current.candidates[index]
                accepted_size = (
                    current.expected_bytes
                    if current.expected_bytes > 0
                    else candidate.expected_bytes
                )
                if not await engine.repository.transition_recovery(
                    current.id,
                    "queued",
                    selected=index,
                    expected_bytes=max(0, accepted_size),
                    retry_at=0,
                    error=None,
                    reset_budget=True,
                ):
                    raise _error(
                        Category.RESOURCE_STATE_CONFLICT,
                        Stage.RECONCILIATION,
                    )

                source = candidate.source_identity
                host = (
                    str(source.key).lower().removeprefix("www.").rstrip(".")
                    if source is not None and str(source.scope) == "host"
                    else "source"
                )
                await engine.repository.record_manual_candidate_failover(
                    transfer_id=current.transfer_id,
                    artifact_id=current.id,
                    filename=current.name,
                    requested_candidate_id=wanted,
                    previous_candidate=old_candidate,
                    selected_candidate=candidate,
                    source_host=host,
                    outcome="success",
                    execution_transition=(
                        "retired_and_redispatch"
                        if had_execution
                        else "queued_for_selected_candidate"
                    ),
                    error=None,
                )
                return {
                    "ok": True,
                    "transfer_id": current.transfer_id,
                    "artifact_id": current.id,
                    "filename": current.name,
                    "candidate_id": str(candidate.id),
                    "source_host": host,
                    "provider_id": candidate.provider_id,
                }
            except TransferError as exc:
                await _record_failure(
                    engine,
                    transfer_id=transfer_id,
                    artifact=artifact,
                    requested_candidate_id=wanted,
                    selected_candidate=candidate,
                    previous_candidate=old_candidate,
                    error=exc.error,
                )
                raise
            except Exception as exc:
                error = unknown_failure(
                    exc,
                    integration_id=(
                        str(getattr(candidate, "provider_id", "") or "")
                        if candidate is not None
                        else ""
                    ),
                    domain=Domain.RECONCILIATION,
                    stage=Stage.RECONCILIATION,
                )
                await _record_failure(
                    engine,
                    transfer_id=transfer_id,
                    artifact=artifact,
                    requested_candidate_id=wanted,
                    selected_candidate=candidate,
                    previous_candidate=old_candidate,
                    error=error,
                )
                raise TransferError(error) from exc
