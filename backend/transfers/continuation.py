"""The one Continuation Planner.

Every execution replacement -- first admission, automatic failover, operator
candidate switch, Resume, startup/provider/executor recovery and collection
convergence -- reaches a new writer through ``TransferEngine._dispatch``, which
asks this planner exactly one question: given the artifact's DebridPulse-owned
material and the executor core selected, what may the new writer keep, and
where may it write? Executors only declare what they can honor
(``ExecutorCapabilities.continuation``); retention, rollback and restart are
decided here and nowhere else. A plan is bound to the material generation it
was computed against, so a plan made stale by an invalidation cannot be
admitted (``TransferRepository.prepare_execution`` re-checks it atomically).

A changed source, protocol or executor never forces a restart by itself:
equivalence already decided the candidate is the same logical artifact, and
the planner keeps the maximal prefix the selected executor can continue from.
"""
from __future__ import annotations

from collections.abc import Mapping

from transfers import material as mat
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.models import (
    ContinuationCapability, ContinuationPlan, ContinuationStrategy, ExecutorCapabilities, MaterializationKind,
    TransferCandidate,
)
from transfers.size_evidence import positive_size, reported_sizes_compatible

_CONTINUES = frozenset({ContinuationCapability.CONTIGUOUS_FROM_OFFSET, ContinuationCapability.IMPORT_EXISTING_MATERIAL})


def parks_on_pause(capabilities: ExecutorCapabilities, materialization: MaterializationKind) -> bool:
    """TEMPORARY COMPATIBILITY EXCEPTION -- collection artifacts of executors
    that export no final-file ranges (today: one stepping-stone executor,
    pending its replacement by an executor that exports exact per-member
    ranges and so needs no exception; the executor names itself at its own
    capability declaration).

    Such an artifact has no DebridPulse range material, so fencing its writer
    at Pause would discard all progress; the executor's own paused job is the
    only reusable state. Pause may therefore leave that native job quiesced
    ("parked") instead of cancelling it -- decided here from declared
    capabilities only, never from an executor identity. While DebridPulse's
    durable pause intent stands the parked job has NO progress authority
    (``authorize_execution`` refuses start/resume; a parked job observed
    running again is re-quiesced by the one writer retirement), and only
    Resume, through the canonical lifecycle owner, may continue it.

    Never for a FILE artifact: there Pause always fences the writer and DP
    material carries the progress."""
    continuation = capabilities.continuation
    return (materialization == MaterializationKind.COLLECTION
            and ContinuationCapability.NATIVE_PRIVATE_RESUME in continuation
            and ContinuationCapability.NATIVE_QUIESCE in continuation
            and ContinuationCapability.EXPORT_MATERIAL_RANGES not in continuation)


def continuation_conflict() -> TransferError:
    """A supposedly equivalent source whose authoritative size contradicts the
    artifact's: an identity/reconciliation conflict, never a harmless resize."""
    return TransferError(NormalizedError(
        Domain.INTEGRITY, Category.SIZE_MISMATCH, Stage.CANDIDATE_PREPARATION,
        retryability=Retryability.AFTER_RERESOLUTION,
    ))


def plan_continuation(state: mat.MaterialState, *, candidate: TransferCandidate, executor_id: str,
                      capabilities: ExecutorCapabilities, reason: str,
                      discovered: Mapping[str, int] | None = None) -> ContinuationPlan:
    """``discovered``: exact continuation boundaries the selected executor
    reported for concrete source data (``BOUNDARY_DISCOVERY``), keyed ``""``
    for a FILE artifact and by member path for a collection. A boundary is
    never above the DP-valid prefix, whatever an executor answers."""
    expected = state.expected_size
    offered = positive_size(candidate.expected_bytes)
    if expected and offered is not None and not reported_sizes_compatible(expected, offered):
        raise continuation_conflict()
    # The writer is bounded by the artifact's size only when the candidate does
    # not state a different one; an unknown or merely plausible size leaves
    # the end open rather than guessing where the artifact stops.
    bound = expected if expected and (offered is None or offered == expected) else None
    discovered = dict(discovered or {})
    continues = (_CONTINUES <= capabilities.continuation and state.geometry_version == mat.GEOMETRY_VERSION
                 and candidate.materialization in capabilities.materialization_kinds)

    def boundary_for(key: str, prefix: int) -> int:
        boundary = mat.align_down(prefix, capabilities.continuation_alignment)
        if key in discovered:
            boundary = min(boundary, max(0, int(discovered[key])))
        return boundary

    boundary = 0
    member_boundaries, member_discarded = [], []
    if continues and candidate.materialization == MaterializationKind.FILE:
        boundary = boundary_for("", state.safe_prefix)
        if bound is not None:
            boundary = min(boundary, bound)
    if candidate.materialization == MaterializationKind.COLLECTION:
        for member, ranges in state.members:
            kept = boundary_for(member, mat.contiguous_prefix(ranges)) if continues else 0
            if kept:
                member_boundaries.append((member, kept))
            dropped = mat.subtract(ranges, ((0, kept),) if kept else ())
            if dropped:
                member_discarded.append((member, dropped))
    retained = ((0, boundary),) if boundary else ()
    return ContinuationPlan(
        artifact_id=state.artifact_id,
        material_generation=state.material_generation,
        geometry_version=mat.GEOMETRY_VERSION,
        candidate_id=str(candidate.id),
        executor_id=str(executor_id),
        strategy=(ContinuationStrategy.CONTIGUOUS_FROM_OFFSET if boundary or member_boundaries
                  else ContinuationStrategy.FULL_RESTART),
        boundary=boundary,
        retained=retained,
        # Everything valid the new writer does not keep -- sparse ranges past
        # the prefix and any alignment tail -- is reclassified, never counted.
        discarded=mat.subtract(state.valid, retained),
        authorized=((boundary, bound if bound is not None else mat.OPEN_END),),
        expected_size=expected,
        reason=str(reason),
        capabilities=tuple(sorted(item.value for item in capabilities.continuation)),
        alignment=int(capabilities.continuation_alignment),
        member_boundaries=tuple(member_boundaries),
        member_discarded=tuple(member_discarded),
    )
