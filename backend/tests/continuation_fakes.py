"""Real byte-moving executors and providers for continuation proofs.

``SpoolExecutor`` shares nothing with aria2: it claims only its own endpoint
scheme, keeps its own private native journal beside the target, writes real
bytes into the planned FILE target, and continues ONLY from what the core
continuation plan authorizes -- it never reads another executor's control
state, and never trusts file length. Two instances with different identities
and schemes are two different executors for cross-executor handoff proofs.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from transfers.applicability import ProviderApplicability
from transfers.models import (
    Capability, ContinuationCapability, ContinuationStrategy, Endpoint, ExecutionActivity, ExecutionControl,
    ExecutionFootprint, ExecutionHandle, ExecutionObservation, ExecutionSnapshot, ExecutionState,
    ExecutorCapabilities, ExecutorClaim, ExecutorHealth, IntegrityMetadata, IntegrationDescriptor,
    MaterializationKind, MaterializationResult, MaterializedEntry, ResolutionResult, ResourceState, SourceIdentity,
    TransferCandidate, TransferProgress,
)

MIB = 1 << 20
CONTINUES = frozenset({
    ContinuationCapability.FULL_RESTART, ContinuationCapability.CONTIGUOUS_FROM_OFFSET,
    ContinuationCapability.IMPORT_EXISTING_MATERIAL, ContinuationCapability.EXPORT_MATERIAL_RANGES,
    ContinuationCapability.NATIVE_QUIESCE,
})


def payload(size: int, seed: str = "artifact") -> bytes:
    block = hashlib.sha256(seed.encode()).digest()
    return (block * (size // len(block) + 1))[:size]


class SpoolProvider:
    """Resolves ``spool`` requests: ``payload`` names a shared source; the
    provider's ``scheme`` decides which executor can claim its candidate.
    Candidates of the same source carry the same strong integrity, so core
    equivalence binds them to one logical artifact."""

    def __init__(self, identity: str, scheme: str, sources: dict[str, bytes], *, report_size: bool = True):
        self.descriptor = IntegrationDescriptor(identity, identity, frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"spool"}))
        self.scheme = scheme
        self.sources = sources
        self.report_size = report_size
        self.calls = []

    @property
    def applicability(self):
        return ProviderApplicability()

    async def resolve(self, request):
        self.calls.append(request.payload)
        data = self.sources[str(request.payload)]
        return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
            request.name or str(request.payload), (Endpoint(self.scheme, f"{self.scheme}:{request.payload}"),),
            expected_bytes=len(data) if self.report_size else 0, provider_id=self.descriptor.id,
            integrity=(IntegrityMetadata("sha256", hashlib.sha256(data).hexdigest()),) if self.report_size else (),
            source_identity=SourceIdentity("spool", f"{self.descriptor.id}:{request.payload}"),
        ),))


@dataclass
class SpoolJob:
    target: str
    source: bytes
    start: int
    cursor: int
    state: ExecutionState = ExecutionState.RUNNING
    plan: object = None


class SpoolExecutor:
    def __init__(self, authorize, sources: dict[str, bytes], *, identity="spool-a", scheme="spoola",
                 continuation=CONTINUES, alignment=1, export=True, report_total=True):
        self.descriptor = IntegrationDescriptor(identity, identity, frozenset())
        continuation = frozenset(continuation)
        if not export:
            continuation -= {ContinuationCapability.EXPORT_MATERIAL_RANGES}
        self.capabilities = ExecutorCapabilities(per_execution_pause=True, continuation=continuation,
                                                 continuation_alignment=alignment)
        self.authorize = authorize
        self.sources = sources
        self.scheme = scheme
        self.jobs: dict[str, SpoolJob] = {}
        self.plans = []
        self.calls = []
        self.quiesce_hangs = False
        self.report_total = report_total

    def claim(self, subject):
        return ExecutorClaim(any(item.scheme == self.scheme for item in subject.candidate.endpoints))

    def journal(self, target) -> str:
        """This executor's private native state beside the target."""
        return str(target) + f".{self.descriptor.id}-journal"

    def footprint(self, work):
        return ExecutionFootprint((self.journal(work.materialization.target),))

    def prepare(self, request):
        return ExecutionHandle(self.descriptor.id, request.attempt_id,
                               {"target": request.work.materialization.target})

    async def start(self, request, handle):
        assert await self.authorize(handle, "start")
        plan = request.continuation
        self.plans.append(plan)
        self.calls.append(("start", handle.attempt_id))
        target = request.work.materialization.target
        address = request.work.subject.candidate.endpoints[0].address
        source = self.sources[address.split(":", 1)[1]]
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        boundary = plan.boundary if plan is not None and plan.strategy == ContinuationStrategy.CONTIGUOUS_FROM_OFFSET else 0
        if boundary:
            # Continue exactly at the DP boundary: the retained prefix must be
            # physically present, and nothing past it is kept.
            if not os.path.exists(target) or os.path.getsize(target) < boundary:
                job = SpoolJob(target, source, boundary, boundary, ExecutionState.FAILED, plan)
                self.jobs[handle.attempt_id] = job
                return self._observation(handle, job)
            os.truncate(target, boundary)
        else:
            with open(target, "wb"):
                pass
        Path(self.journal(target)).write_text(str(boundary))
        job = SpoolJob(target, source, boundary, boundary, plan=plan)
        self.jobs[handle.attempt_id] = job
        return self._observation(handle, job)

    def step(self, attempt_id: str, count: int) -> None:
        """Move ``count`` real bytes for a running job."""
        job = self.jobs[attempt_id]
        assert job.state == ExecutionState.RUNNING
        end = min(len(job.source), job.cursor + count)
        with open(job.target, "r+b") as handle:
            handle.seek(job.cursor)
            handle.write(job.source[job.cursor:end])
        job.cursor = end
        Path(self.journal(job.target)).write_text(str(end))
        if end == len(job.source):
            job.state = ExecutionState.SUCCEEDED
            Path(self.journal(job.target)).unlink(missing_ok=True)

    def _observation(self, handle, job):
        material = ((job.start, job.cursor),) if job.cursor > job.start else ()
        result = None
        if job.state == ExecutionState.SUCCEEDED:
            result = MaterializationResult(MaterializationKind.FILE, (MaterializedEntry(
                Path(job.target).name, len(job.source)),))
        controls = frozenset({ExecutionControl.PAUSE}) if job.state == ExecutionState.RUNNING else frozenset()
        activity = (ExecutionActivity(network_active=True, bandwidth_reservation_required=True,
                                      progress_expected=True)
                    if job.state == ExecutionState.RUNNING else ExecutionActivity())
        total = len(job.source) if self.report_total else 0
        return ExecutionObservation(handle, job.state, TransferProgress(total, job.cursor, 1),
                                    activity=activity, controls=controls, materialization=result,
                                    material=material if self.capabilities.continuation >= {
                                        ContinuationCapability.EXPORT_MATERIAL_RANGES} else None)

    async def observe_many(self, handles):
        results = []
        for handle in handles:
            job = self.jobs.get(handle.attempt_id)
            results.append(ExecutionObservation(handle, ExecutionState.ABSENT) if job is None
                           else self._observation(handle, job))
        return ExecutionSnapshot(tuple(results))

    async def pause(self, handle):
        assert await self.authorize(handle, "pause")
        self.calls.append(("pause", handle.attempt_id))
        job = self.jobs[handle.attempt_id]
        if self.quiesce_hangs:
            import asyncio
            await asyncio.sleep(3600)
        if job.state == ExecutionState.RUNNING:
            job.state = ExecutionState.PAUSED
        return self._observation(handle, job)

    async def resume(self, handle):
        raise AssertionError("DP Resume must never be native executor resume")

    async def cancel(self, handle):
        assert await self.authorize(handle, "cancel")
        self.calls.append(("cancel", handle.attempt_id))
        job = self.jobs.get(handle.attempt_id)
        if job is None:
            return ExecutionObservation(handle, ExecutionState.ABSENT)
        if job.state in {ExecutionState.RUNNING, ExecutionState.PAUSED, ExecutionState.QUEUED}:
            job.state = ExecutionState.CANCELLED
        return self._observation(handle, job)

    async def health(self):
        return ExecutorHealth(True, True)


class BoundarySpoolExecutor(SpoolExecutor):
    """A SpoolExecutor that can only continue where its (fake) source
    segments begin -- a data-dependent boundary, like decoded article starts."""

    def __init__(self, authorize, sources, *, segment_starts, answer=None, **kwargs):
        super().__init__(authorize, sources, continuation=CONTINUES | {ContinuationCapability.BOUNDARY_DISCOVERY},
                         **kwargs)
        self.segment_starts = tuple(sorted(segment_starts))
        self.answer = answer  # override (a misbehaving executor)
        self.asked = []

    async def continuation_boundary(self, subject, member, prefix):
        self.asked.append((member, prefix))
        if self.answer is not None:
            return self.answer
        return max((start for start in self.segment_starts if start <= prefix), default=0)


class CollectionSpoolExecutor:
    """A byte-moving COLLECTION executor: each member file of one artifact is
    written under the collection root and reported per member; it continues
    every member exactly at its planned member boundary."""

    def __init__(self, authorize, members: dict[str, bytes], *, identity="bundle", scheme="bundle"):
        self.descriptor = IntegrationDescriptor(identity, identity, frozenset())
        self.capabilities = ExecutorCapabilities(
            per_execution_pause=True, continuation=CONTINUES,
            materialization_kinds=frozenset({MaterializationKind.COLLECTION}))
        self.authorize = authorize
        self.members = dict(members)
        self.scheme = scheme
        self.jobs = {}
        self.plans = []
        self.report = None  # override of reported member ranges (adversarial)

    def claim(self, subject):
        return ExecutorClaim(any(item.scheme == self.scheme for item in subject.candidate.endpoints))

    def footprint(self, work):
        return ExecutionFootprint()

    def prepare(self, request):
        return ExecutionHandle(self.descriptor.id, request.attempt_id, {"root": request.work.materialization.root})

    async def start(self, request, handle):
        assert await self.authorize(handle, "start")
        plan = request.continuation
        self.plans.append(plan)
        root = Path(request.work.materialization.root)
        cursors = {}
        for member in self.members:
            path = root / member
            path.parent.mkdir(parents=True, exist_ok=True)
            boundary = plan.member_boundary(member) if plan is not None else 0
            if boundary:
                os.truncate(path, boundary)
            else:
                path.write_bytes(b"")
            cursors[member] = boundary
        self.jobs[handle.attempt_id] = {"root": root, "cursors": cursors, "starts": dict(cursors),
                                        "state": ExecutionState.RUNNING}
        return self._observation(handle, self.jobs[handle.attempt_id])

    def step(self, attempt_id, member, count):
        job = self.jobs[attempt_id]
        data = self.members[member]
        start = job["cursors"][member]
        end = min(len(data), start + count)
        with open(job["root"] / member, "r+b") as handle:
            handle.seek(start)
            handle.write(data[start:end])
        job["cursors"][member] = end
        if all(job["cursors"][name] == len(self.members[name]) for name in self.members):
            job["state"] = ExecutionState.SUCCEEDED

    def _observation(self, handle, job):
        material = self.report if self.report is not None else tuple(
            (member, ((job["starts"][member], cursor),) if cursor > job["starts"][member] else ())
            for member, cursor in sorted(job["cursors"].items()))
        result = None
        if job["state"] == ExecutionState.SUCCEEDED:
            result = MaterializationResult(MaterializationKind.COLLECTION, tuple(
                MaterializedEntry(member, len(data)) for member, data in sorted(self.members.items())))
        running = job["state"] == ExecutionState.RUNNING
        return ExecutionObservation(
            handle, job["state"], TransferProgress(0, 0, 1),
            activity=ExecutionActivity(network_active=running, bandwidth_reservation_required=running,
                                       progress_expected=running),
            controls=frozenset({ExecutionControl.PAUSE}) if running else frozenset(),
            materialization=result, member_material=material)

    async def observe_many(self, handles):
        return ExecutionSnapshot(tuple(
            self._observation(handle, self.jobs[handle.attempt_id]) if handle.attempt_id in self.jobs
            else ExecutionObservation(handle, ExecutionState.ABSENT) for handle in handles))

    async def pause(self, handle):
        assert await self.authorize(handle, "pause")
        job = self.jobs[handle.attempt_id]
        if job["state"] == ExecutionState.RUNNING:
            job["state"] = ExecutionState.PAUSED
        return self._observation(handle, job)

    async def resume(self, handle):
        raise AssertionError("DP Resume must never be native executor resume")

    async def cancel(self, handle):
        assert await self.authorize(handle, "cancel")
        job = self.jobs.get(handle.attempt_id)
        if job is None:
            return ExecutionObservation(handle, ExecutionState.ABSENT)
        if job["state"] in {ExecutionState.RUNNING, ExecutionState.PAUSED}:
            job["state"] = ExecutionState.CANCELLED
        return self._observation(handle, job)

    async def health(self):
        return ExecutorHealth(True, True)


class CollectionSpoolProvider:
    def __init__(self, identity="src-bundle", scheme="bundle"):
        self.descriptor = IntegrationDescriptor(identity, identity, frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"bundle"}))
        self.scheme = scheme

    @property
    def applicability(self):
        return ProviderApplicability()

    async def resolve(self, request):
        return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
            request.name or "bundle", (Endpoint(self.scheme, f"{self.scheme}:{request.payload}"),),
            provider_id=self.descriptor.id, materialization=MaterializationKind.COLLECTION),))
