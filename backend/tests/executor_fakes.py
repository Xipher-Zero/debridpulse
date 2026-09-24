"""Deterministic executors that share nothing with aria2.

``LedgerExecutor`` claims subjects by canonical request kind (never by URL
scheme), may learn its native identity only after native acceptance, reports
lifecycle/activity/controls independently, can produce multi-file
collections, and optionally implements every optional semantic protocol of the
generalized executor contract. ``LedgerProvider`` resolves ``ledger`` requests
into endpoint-less candidates.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import hashlib
from pathlib import Path

from transfers.applicability import ProviderApplicability
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.input_required import auth_required, username_password
from transfers.models import (
    ArtifactFingerprint, Capability, ExecutionActivity, ExecutionControl, ExecutionFootprint,
    ExecutionHandle, ExecutionObservation, ExecutionSnapshot, ExecutionState, ExecutorCapabilities,
    ExecutorClaim, ExecutorGateResult, ExecutorHealth, ExecutorRuntimeCapability, ExecutorRuntimeControlResult,
    ExecutorThroughput,
    InputField, InputMethod, IntegrationDescriptor, MaterializationKind, MaterializationResult, MaterializedEntry,
    ResolutionResult, ResourceState, TransferCandidate, TransferProgress,
)


class LedgerProvider:
    """Resolves ``ledger`` requests into candidates without any endpoint.

    ``payload`` is ``<name>`` or ``<name>:<collection>``; a collection
    candidate declares the neutral COLLECTION materialization shape."""

    def __init__(self, identity="ledger-lab", *, input_methods=()):
        self.descriptor = IntegrationDescriptor(identity, "Ledger lab", frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"ledger", "tome"}))
        self.input_methods = tuple(input_methods)

    @property
    def applicability(self):
        return ProviderApplicability()

    async def resolve(self, request):
        name, _, shape = str(request.payload).partition(":")
        kind = MaterializationKind.COLLECTION if shape == "collection" else MaterializationKind.FILE
        # A payload naming a relative path resolves to the nested destination
        # core would build for it, so path-scaffolding behavior is provable
        # without a manifest.
        return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
            request.name or name, (), expected_bytes=4 if kind == MaterializationKind.FILE else 0,
            relative_path=name if "/" in name else "",
            provider_id=self.descriptor.id, materialization=kind,
            accepted_input_methods=self.input_methods,
        ),))


@dataclass
class LedgerJob:
    native_id: str
    ticket: str
    state: ExecutionState = ExecutionState.QUEUED
    progress: TransferProgress = field(default_factory=lambda: TransferProgress(4, 1, 1))
    activity: ExecutionActivity = field(default_factory=lambda: ExecutionActivity(
        network_active=True, bandwidth_reservation_required=True, progress_expected=True))
    controls: frozenset = frozenset({ExecutionControl.PAUSE})
    materialization: MaterializationResult | None = None
    error: NormalizedError | None = None
    owner_attempt: str = ""


class LedgerExecutor:
    """A non-URL, server-assigned-identity executor for core-neutrality proofs."""

    def __init__(self, authorize, *, identity="ledger-copy", kinds=("ledger",), priority=0,
                 deferred_native=True, capabilities: ExecutorCapabilities | None = None,
                 runtime_available=None, log=None, name="Ledger copy"):
        self.descriptor = IntegrationDescriptor(identity, name, frozenset(), priority=priority)
        self.capabilities = capabilities or ledger_capabilities()
        self.authorize = authorize
        self.kinds = frozenset(kinds)
        self.deferred_native = deferred_native
        self.jobs: dict[str, LedgerJob] = {}
        self.calls: list = []
        self.log = log if log is not None else []
        self.counter = 0
        self.lose_start_ack = False
        self.cancel_mode = "confirm"
        self.observe_failure: NormalizedError | None = None
        self.ceiling_failure = False
        self.ceilings: list[int] = []
        self.gate: list[bool] = []
        # Executor-level throughput, for the neutral aggregate telemetry seam.
        self.aggregate_throughput = 0
        self.throughput_unobservable = False
        self.runtime_available = (frozenset(runtime_available) if runtime_available is not None
                                  else frozenset(ExecutorRuntimeCapability))
        self.collection_files: dict[str, bytes] = {"part-1.bin": b"ab", "nested/part-2.bin": b"cd"}
        self.retries: list = []
        self.samples: list = []
        self.lose_retry_ack = False
        self.fail_before_retry_mutation = False
        self.reachable = True
        self.ready = True

    # -- applicability ----------------------------------------------------
    def claim(self, subject):
        return ExecutorClaim(subject.request_kind in self.kinds)

    def footprint(self, work):
        plan = work.materialization
        if plan.kind == MaterializationKind.FILE:
            return ExecutionFootprint((str(plan.target) + ".ledger-journal",))
        # An rsync-like partial directory: a whole transient subtree.
        return ExecutionFootprint((str(Path(plan.root) / ".ledger-journal"),),
                                  (str(Path(plan.root) / ".ledger-partial"),))

    # -- identity -----------------------------------------------------------
    def prepare(self, request):
        native = None if self.deferred_native else {"job": "pre-" + request.attempt_id}
        return ExecutionHandle(self.descriptor.id, request.attempt_id, {"ticket": request.attempt_id}, native)

    def _bound(self, handle, job):
        return handle if handle.native is not None else replace(handle, native={"job": job.native_id})

    def _observation(self, handle, job):
        return ExecutionObservation(self._bound(handle, job), job.state, job.progress, job.error, job.activity,
                                    job.controls if job.state not in {ExecutionState.SUCCEEDED, ExecutionState.FAILED,
                                                                      ExecutionState.CANCELLED} else frozenset(),
                                    job.materialization if job.state == ExecutionState.SUCCEEDED else None)

    def _find(self, handle):
        if handle.native is not None:
            return next((job for job in self.jobs.values() if job.native_id == handle.native["job"]), None)
        return next((job for job in self.jobs.values() if job.ticket == handle.correlation["ticket"]
                     and job.owner_attempt == handle.attempt_id), None)

    async def start(self, request, handle):
        assert await self.authorize(handle, "start"), "core must persist authority before native contact"
        self.calls.append(("start", handle.attempt_id))
        self.log.append((self.descriptor.id, "start"))
        self.counter += 1
        native_id = handle.native["job"] if handle.native else f"srv-{self.descriptor.id}-{self.counter}"
        job = LedgerJob(native_id, handle.correlation["ticket"], owner_attempt=handle.attempt_id)
        if request.paused:
            job.state = ExecutionState.PAUSED
        self.jobs[native_id] = job
        if self.lose_start_ack:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN, error=NormalizedError(
                Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE, Stage.QUEUE, retryability=Retryability.UNKNOWN,
                integration_id=self.descriptor.id))
        return self._observation(handle, job)

    async def observe_many(self, handles):
        self.calls.append(("observe_many", tuple(item.attempt_id for item in handles)))
        if self.observe_failure is not None:
            return ExecutionSnapshot((), self.observe_failure)
        results = []
        for handle in handles:
            job = self._find(handle)
            results.append(ExecutionObservation(handle, ExecutionState.ABSENT) if job is None
                           else self._observation(handle, job))
        return ExecutionSnapshot(tuple(results))

    async def pause(self, handle):
        assert await self.authorize(handle, "pause")
        self.calls.append(("pause", handle.attempt_id))
        job = self._find(handle)
        if job and ExecutionControl.PAUSE in job.controls and job.state in {ExecutionState.QUEUED, ExecutionState.RUNNING}:
            job.state = ExecutionState.PAUSED
            job.controls = frozenset({ExecutionControl.RESUME})
            job.activity = replace(job.activity, network_active=False)
        return self._observation(handle, job) if job else ExecutionObservation(handle, ExecutionState.ABSENT)

    async def resume(self, handle):
        assert await self.authorize(handle, "resume")
        self.calls.append(("resume", handle.attempt_id))
        self.log.append((self.descriptor.id, "resume"))
        job = self._find(handle)
        if job and ExecutionControl.RESUME in job.controls and job.state == ExecutionState.PAUSED:
            job.state = ExecutionState.RUNNING
            job.controls = frozenset({ExecutionControl.PAUSE})
            job.activity = replace(job.activity, network_active=True)
        return self._observation(handle, job) if job else ExecutionObservation(handle, ExecutionState.ABSENT)

    async def cancel(self, handle):
        assert await self.authorize(handle, "cancel")
        self.calls.append(("cancel", handle.attempt_id))
        job = self._find(handle)
        uncertain = ExecutionObservation(handle, ExecutionState.UNKNOWN, error=NormalizedError(
            Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE, Stage.CLEANUP, retryability=Retryability.BACKOFF,
            integration_id=self.descriptor.id))
        if self.cancel_mode == "unconfirmed":
            return uncertain
        if job is None:
            return ExecutionObservation(handle, ExecutionState.ABSENT)
        job.state = ExecutionState.CANCELLED
        job.activity = ExecutionActivity()
        if self.cancel_mode == "lost_ack":
            return uncertain
        return self._observation(handle, job)

    async def health(self):
        return ExecutorHealth(self.reachable, self.ready, self.runtime_available)

    # -- optional semantic operations (declared through capabilities) --------
    async def set_bandwidth_ceiling(self, bytes_per_second):
        self.log.append((self.descriptor.id, "ceiling", bytes_per_second))
        if self.ceiling_failure:
            return ExecutorRuntimeControlResult(bytes_per_second, None, NormalizedError(
                Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE, Stage.EXECUTION, retryability=Retryability.BACKOFF,
                integration_id=self.descriptor.id))
        self.ceilings.append(bytes_per_second)
        return ExecutorRuntimeControlResult(bytes_per_second, bytes_per_second)

    async def set_acquisition_paused(self, paused):
        self.log.append((self.descriptor.id, "gate", paused))
        self.gate.append(paused)
        return ExecutorGateResult(paused, paused)

    async def aggregate_download_throughput(self):
        """The neutral executor-level throughput seam, proven by an executor
        that is neither of the two shipped ones: nothing about it is specific
        to how any particular service measures."""
        self.log.append((self.descriptor.id, "throughput", self.aggregate_throughput))
        if self.throughput_unobservable:
            return ExecutorThroughput(0, False)
        return ExecutorThroughput(self.aggregate_throughput, True)

    async def retry_from(self, request, prepared, previous):
        assert await self.authorize(prepared, "start")
        self.retries.append((previous.attempt_id, prepared.attempt_id))
        if self.fail_before_retry_mutation:
            raise ConnectionError("native retry never reached the executor")
        job = self._find(previous)
        if job is None:
            return await self.start(request, prepared)
        job.owner_attempt = prepared.attempt_id
        job.ticket = prepared.correlation["ticket"]
        job.state = ExecutionState.QUEUED
        job.error = None
        job.controls = frozenset({ExecutionControl.PAUSE})
        job.activity = ExecutionActivity(True, True, True)
        if self.lose_retry_ack:
            return ExecutionObservation(prepared, ExecutionState.UNKNOWN, error=NormalizedError(
                Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE, Stage.QUEUE, retryability=Retryability.UNKNOWN,
                integration_id=self.descriptor.id))
        return self._observation(prepared, job)

    async def fingerprint(self, subject):
        self.samples.append((str(subject.candidate.id), None))
        return ArtifactFingerprint(4, hashlib.sha256(subject.candidate.name.encode()).hexdigest())

    # -- native-side test drivers -------------------------------------------
    def job_for(self, handle):
        return self._find(handle)

    def run(self, handle, *, progress_expected=True, network_active=True, completed=None):
        job = self._find(handle)
        job.state = ExecutionState.RUNNING
        job.activity = ExecutionActivity(network_active, True, progress_expected)
        if completed is not None:
            job.progress = TransferProgress(job.progress.total_bytes, completed, 0)

    def fail(self, handle, error=None):
        job = self._find(handle)
        job.state = ExecutionState.FAILED
        job.error = error or NormalizedError(Domain.NETWORK, Category.REMOTE_READ_FAILED, Stage.EXECUTION,
                                             retryability=Retryability.BACKOFF, integration_id=self.descriptor.id)
        job.activity = ExecutionActivity()

    def finish_file(self, handle, target: str, content=b"done"):
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        job = self._find(handle)
        job.state = ExecutionState.SUCCEEDED
        job.progress = TransferProgress(len(content), len(content), 0)
        job.activity = ExecutionActivity()
        job.materialization = MaterializationResult(MaterializationKind.FILE, (
            MaterializedEntry(path.name, len(content)),))

    def finish_collection(self, handle, root: str, *, files=None, entries=None):
        files = self.collection_files if files is None else files
        base = Path(root)
        for relative, content in files.items():
            target = base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        job = self._find(handle)
        job.state = ExecutionState.SUCCEEDED
        total = sum(len(item) for item in files.values())
        job.progress = TransferProgress(total, total, 0)
        job.activity = ExecutionActivity()
        job.materialization = MaterializationResult(MaterializationKind.COLLECTION, tuple(
            entries if entries is not None else (MaterializedEntry(relative, len(content))
                                                 for relative, content in files.items())))


class RecordingPostProcessor:
    descriptor = IntegrationDescriptor("record-post", "Recording post-processor", frozenset())

    def __init__(self):
        self.calls = []

    async def process(self, transfer_id, paths):
        from transfers.models import OutcomeKind, TransferOutcome
        self.calls.append((transfer_id, tuple(paths)))
        return TransferOutcome(OutcomeKind.SUCCESS)


def ledger_capabilities(**overrides) -> ExecutorCapabilities:
    values = {"per_execution_pause": True,
              "materialization_kinds": frozenset({MaterializationKind.FILE, MaterializationKind.COLLECTION})}
    values.update(overrides)
    return ExecutorCapabilities(**values)


async def settle(engine, rounds=3):
    for _ in range(rounds):
        await engine.tick()
        await asyncio.sleep(0)


async def ledger_core(tmp_path, monkeypatch, *, executors=None, providers=None, postprocessors=(), policy=None,
                      now=1000.0):
    """The canonical production engine/repository driven only by ledger fakes.

    ``executors`` is a callable ``authorize -> iterable of executors``."""
    from types import SimpleNamespace

    import db.database as database
    from transfers.convergence_engine import TransferEngine
    from transfers.policy import TransferPolicy
    from transfers.recovery_repository import TransferRepository
    from transfers.registry import IntegrationRegistry

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    providers = tuple(providers) if providers is not None else (LedgerProvider(),)
    for provider in providers:
        registry.register_provider(provider)
    built = tuple(executors(repository.authorize_execution)) if executors else (
        LedgerExecutor(repository.authorize_execution),)
    for executor in built:
        registry.register_executor(executor)
    clock = [now]
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=policy or TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                                                            max_active_executions=3),
                            postprocessors=postprocessors, clock=lambda: clock[0])
    await engine.initialize()
    return SimpleNamespace(engine=engine, repository=repository, registry=registry, providers=providers,
                           provider=providers[0], executors=built, executor=built[0], now=clock, root=tmp_path / "payloads")


async def submit_ledger(core, payload="ledger-item", name="", kind="ledger"):
    from transfers.models import TransferRequest
    transfer = await core.engine.submit((TransferRequest(kind, payload, name=name or payload.partition(":")[0]),))
    await core.engine.tick()
    return transfer


async def artifact_of(core, transfer_id):
    return (await core.repository.artifacts(transfer_id))[0]
