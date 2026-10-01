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
the planner keeps the most DP-valid material the selected executor can
consume: the maximal prefix it can continue from; every whole chunk of a FILE
of known size for an executor that imports sparse material
(``IMPORT_SPARSE_MATERIAL``, a fresh job acquiring only the rest); or every
DP-valid range in place as the basis of an executor that reconstructs
destination-aware (``DESTINATION_AWARE_CONTINUATION`` -- a bandwidth-saving
reconstruction, never instant continuation: its private work is not DP
material until the verified whole payload is installed).

What the selected executor cannot consume is stated as exactly that
(``ContinuationPlan.unusable``: a capability limitation, never a finding about
the material). Of it, only what the new writer will rewrite (its authorized
region) leaves VALID at admission (``discarded``).
A source switch is planned exactly like every other replacement: the new
writer is a fresh execution that keeps what this planner retains, never the
previous writer's native object.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from transfers import material as mat
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.models import (
    ContinuationCapability, ContinuationPlan, ContinuationStrategy, ExecutorCapabilities, MaterializationKind,
    TransferCandidate,
)
from transfers.size_evidence import positive_size, reported_sizes_compatible

_CONTINUES = frozenset({ContinuationCapability.CONTIGUOUS_FROM_OFFSET, ContinuationCapability.IMPORT_EXISTING_MATERIAL})


def parks_on_pause(capabilities: ExecutorCapabilities) -> bool:
    """Whether Pause may leave a writer quiesced ("parked") instead of
    fencing it -- decided from declared capabilities only, never from an
    executor identity or a materialization kind: the executor quiesces
    natively AND resumes that same quiesced job from its own private state.

    Parking keeps disposable acceleration (the executor's private progress,
    e.g. sparse pieces the DP-valid prefix does not cover); it never widens
    DebridPulse material, which the forced checkpoint before parking already
    committed. While the durable pause intent stands a parked job has NO
    progress authority (``authorize_execution`` refuses start/resume; a parked
    job observed acquiring again is re-quiesced by the one writer retirement),
    only Resume, through the canonical lifecycle owner, may continue it, and a
    job whose material generation went stale is retired instead of resumed.
    Parking that cannot be proven falls back to ordinary retirement."""
    continuation = capabilities.continuation
    return (ContinuationCapability.NATIVE_PRIVATE_RESUME in continuation
            and ContinuationCapability.NATIVE_QUIESCE in continuation)


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
    positional_unusable = mat.subtract(state.valid, retained)
    plan = ContinuationPlan(
        artifact_id=state.artifact_id,
        material_generation=state.material_generation,
        geometry_version=mat.GEOMETRY_VERSION,
        candidate_id=str(candidate.id),
        executor_id=str(executor_id),
        strategy=(ContinuationStrategy.CONTIGUOUS_FROM_OFFSET if boundary or member_boundaries
                  else ContinuationStrategy.FULL_RESTART),
        boundary=boundary,
        retained=retained,
        # Everything valid the new writer cannot keep -- sparse ranges past
        # the prefix and any alignment tail -- is unusable to it, and since it
        # writes from the boundary on, rewritten: it leaves VALID at admission.
        discarded=positional_unusable,
        authorized=((boundary, bound if bound is not None else mat.OPEN_END),),
        expected_size=expected,
        reason=str(reason),
        capabilities=tuple(sorted(item.value for item in capabilities.continuation)),
        alignment=int(capabilities.continuation_alignment),
        member_boundaries=tuple(member_boundaries),
        member_discarded=tuple(member_discarded),
        unusable=positional_unusable,
    )
    if (ContinuationCapability.IMPORT_SPARSE_MATERIAL in capabilities.continuation
            and state.geometry_version == mat.GEOMETRY_VERSION and bound is not None
            and candidate.materialization == MaterializationKind.FILE
            and candidate.materialization in capabilities.materialization_kinds):
        # A fresh job is handed every whole chunk DP holds valid (the chunk is
        # the geometry grain, or the executor's coarser alignment); what is not
        # a whole chunk is unusable to it and rewritten. Only a KNOWN total can
        # be stated to the executor, so an open-ended artifact keeps its prefix.
        grain = max(int(capabilities.continuation_alignment), mat.CHUNK_BYTES)
        kept = mat.align_inward(state.valid, end_of_file=expected, chunk=grain)
        if mat.total(kept) > plan.retained_bytes:
            unusable = mat.subtract(state.valid, kept)
            plan = replace(plan, strategy=ContinuationStrategy.SPARSE_IMPORT, boundary=mat.contiguous_prefix(kept),
                           retained=kept, discarded=unusable, unusable=unusable, authorized=((0, bound),))
    if (ContinuationCapability.DESTINATION_AWARE_CONTINUATION in capabilities.continuation
            and state.geometry_version == mat.GEOMETRY_VERSION
            and candidate.materialization == MaterializationKind.FILE
            and candidate.materialization in capabilities.materialization_kinds
            and mat.total(state.valid) > plan.retained_bytes):
        # The destination itself is the new writer's basis: every DP-valid
        # range is kept where it is -- sparse ranges past the prefix included
        # -- and nothing is discarded for its geometry. UNKNOWN stays unknown,
        # and the writer commits only the complete verified payload. Chosen
        # only when it keeps more than the positional plan above would.
        return replace(plan, strategy=ContinuationStrategy.DESTINATION_AWARE, boundary=state.safe_prefix,
                       retained=state.valid, discarded=(), unusable=(),
                       authorized=((0, bound if bound is not None else mat.OPEN_END),))
    return plan
