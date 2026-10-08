"""The Media Downloads executor: one sandboxed yt-dlp worker per attempt.

It carries out exactly the plan its provider made (``providers.media.plan``,
the candidate context ``media_plan``) and owns no policy: identity, selection,
retry, re-resolution, pause, recovery and materialization stay core's.

* Claim: a subject is this executor's when its candidate carries a Media
  Downloads plan and no addressable endpoint -- so no other executor can
  claim it, and this one claims nothing else.
* Footprint: one attempt-private workspace beside the planned target
  (``.<target>.dp-media``) holds every native component, the subtitle and
  the finalized output; core sees only the one FILE the plan named, and the
  workspace is gone before success is reported.
* Process: one owned process group per attempt (``executors
  .process_ownership``): a duplicate start is refused, liveness survives a
  DebridPulse restart, cancellation stops the whole group (yt-dlp, its
  downloaders, the JavaScript runtime and the finalizer alike).
* Completion truth: the worker leaves one durable attempt record
  (``<runtime>/results``) -- completed with the installed size, failed with
  its outcome -- and this executor writes one when it proves a cancellation.
  A group that is gone without a record is ABSENT, never a guessed success.
* Continuation: FULL_RESTART only. Nothing partial is ever DebridPulse
  material; a new attempt re-reads the medium and starts from zero.
* Bandwidth: every byte crosses the guard on a route drawing on this
  executor's one download budget, so the core-assigned aggregate ceiling
  holds for all of its attempts together.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable

from executors.media.sandbox import MediaSandbox, public_scope
from executors.process_ownership import OwnedProcess, ProcessGroupAlive
from integrations.media.outcomes import outcome_error
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.filesystem import validate_target
from transfers.models import (
    ContinuationCapability, ExecutionActivity, ExecutionFootprint, ExecutionHandle, ExecutionObservation,
    ExecutionRequest, ExecutionSnapshot, ExecutionState, ExecutorCapabilities, ExecutorClaim, ExecutorHealth,
    ExecutorRuntimeCapability, ExecutorRuntimeControlResult, IntegrationDescriptor, MaterializationKind,
    MaterializationResult, MaterializedEntry, TransferProgress,
)

EXECUTOR_ID = "yt_dlp"
PLAN_KEY = "media_plan"
_REQUEST_KINDS = frozenset({"http", "https", "media-member"})
_TERMINATE_GRACE = 5.0
_STDERR_LIMIT = 16 * 1024
_FINISHED_MEMORY = 4096
_RECORD_RETENTION_SECONDS = 7 * 24 * 3600
_HEALTH_TTL = 60.0


def plan_of(candidate) -> dict | None:
    value = (getattr(candidate, "context", None) or {}).get(PLAN_KEY)
    if not isinstance(value, dict) or value.get("v") != 1:
        return None
    formats = value.get("formats")
    if (not isinstance(formats, list) or not formats or not all(isinstance(item, str) and item for item in formats)
            or not all(isinstance(value.get(key), str) and value.get(key) for key in ("url", "extractor", "id",
                                                                                      "container"))):
        return None
    return value


@dataclass
class _Run:
    owned: OwnedProcess
    target: Path
    workspace: Path
    components: int
    started: float
    progress: dict = field(default_factory=dict)
    finalizing: bool = False
    stderr: bytearray = field(default_factory=bytearray)
    cancelled: bool = False
    tasks: list = field(default_factory=list)
    sample: tuple[float, int] = (0.0, 0)
    terminal: ExecutionObservation | None = None


class MediaExecutor:
    descriptor = IntegrationDescriptor(EXECUTOR_ID, "Media Downloads", frozenset())
    capabilities = ExecutorCapabilities(
        aggregate_bandwidth_ceiling=True,
        materialization_kinds=frozenset({MaterializationKind.FILE}),
        continuation=frozenset({ContinuationCapability.FULL_RESTART}),
    )

    def __init__(self, local_root: str, runtime_dir: str,
                 authorize: Callable[[ExecutionHandle, str], Awaitable[bool]], *, sandbox: MediaSandbox | None = None):
        self.local_root = local_root
        self.runtime_dir = Path(runtime_dir)
        self.authorize = authorize
        self.sandbox = sandbox or MediaSandbox(runtime_dir, budget=EXECUTOR_ID)
        self.processes = self.sandbox.processes
        self.records = self.runtime_dir / "results"
        self._runs: dict[str, _Run] = {}
        self._finished: OrderedDict[str, None] = OrderedDict()
        self._health: tuple[float, ExecutorHealth] | None = None
        self.binding = f"{Path(local_root).resolve()}|{self.runtime_dir.resolve()}"

    # ── claim, identity, footprint ─────────────────────────────────────────

    def claim(self, subject) -> ExecutorClaim:
        candidate = subject.candidate
        return ExecutorClaim(subject.request_kind in _REQUEST_KINDS and not candidate.endpoints
                             and plan_of(candidate) is not None)

    def _failure(self, category: Category, stage=Stage.QUEUE, *, domain=Domain.EXECUTOR) -> TransferError:
        return TransferError(NormalizedError(domain, category, stage, retryability=Retryability.NEVER,
                                             integration_id=self.descriptor.id))

    @staticmethod
    def workspace(target: Path) -> Path:
        """The attempt's private workspace: one writer per artifact at a time,
        so one workspace per target is never shared."""
        return target.parent / f".{target.name}.dp-media"

    def footprint(self, work) -> ExecutionFootprint:
        target = validate_target(self.local_root, work.materialization.target)
        return ExecutionFootprint(transient_trees=(str(self.workspace(Path(target))),))

    def _target(self, request: ExecutionRequest) -> tuple[Path, dict]:
        plan = request.work.materialization
        if plan.kind != MaterializationKind.FILE or plan.target is None:
            raise self._failure(Category.UNSUPPORTED_CAPABILITY, domain=Domain.REQUEST)
        media = plan_of(request.work.subject.candidate)
        if media is None:
            raise self._failure(Category.UNSUPPORTED_CAPABILITY, domain=Domain.REQUEST)
        target = validate_target(self.local_root, plan.target)
        # The plan fixed the container before core named the file; the file it
        # names must be that container, never renamed into another one.
        if PurePosixPath(target.name).suffix[1:].casefold() != media["container"]:
            raise self._failure(Category.PATH_POLICY_VIOLATION, domain=Domain.SECURITY)
        return target.resolve(), media

    def prepare(self, request: ExecutionRequest) -> ExecutionHandle:
        target, _media = self._target(request)
        if not request.attempt_id:
            raise self._failure(Category.INVALID_REQUEST)
        return ExecutionHandle(self.descriptor.id, request.attempt_id,
                               {"target": str(target), "binding": self.binding})

    async def _check(self, handle: ExecutionHandle, action: str) -> bool:
        if handle.executor_id != self.descriptor.id or not await self.authorize(handle, "observe"):
            raise self._failure(Category.OWNERSHIP_CONFLICT, domain=Domain.LIFECYCLE)
        if handle.correlation.get("binding") != self.binding:
            raise self._failure(Category.OWNERSHIP_CONFLICT, domain=Domain.LIFECYCLE)
        return action == "observe" or await self.authorize(handle, action)

    def _record_path(self, attempt_id: str) -> Path:
        return self.records / f"{public_scope(attempt_id)}.json"

    def _record(self, attempt_id: str) -> dict | None:
        try:
            value = json.loads(self._record_path(attempt_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) and value.get("attempt") == str(attempt_id) else None

    def _write_record(self, attempt_id: str, value: dict) -> None:
        path = self._record_path(attempt_id)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"attempt": str(attempt_id), **value}), encoding="utf-8")
        os.replace(temporary, path)

    def _prune_records(self) -> None:
        cutoff = time.time() - _RECORD_RETENTION_SECONDS
        try:
            for item in self.records.glob("*.json"):
                if item.stat().st_mtime < cutoff:
                    item.unlink(missing_ok=True)
        except OSError:
            pass

    # ── start ──────────────────────────────────────────────────────────────

    async def start(self, request: ExecutionRequest, handle: ExecutionHandle) -> ExecutionObservation:
        try:
            if not await self._check(handle, "start"):
                return ExecutionObservation(handle, ExecutionState.PAUSED)
            if self.prepare(request) != handle:
                raise self._failure(Category.OWNERSHIP_CONFLICT, domain=Domain.LIFECYCLE)
            if handle.attempt_id in self._runs or self.processes.alive(handle.attempt_id):
                # This attempt already owns a native group: never a second one.
                return await self.observe(handle)
            target, media = self._target(request)
            workspace = self.workspace(target)
            # FULL_RESTART: nothing of an earlier attempt is ever reused.
            shutil.rmtree(workspace, ignore_errors=True)
            workspace.mkdir(mode=0o700, parents=True)
            self.records.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._prune_records()
            self._record_path(handle.attempt_id).unlink(missing_ok=True)
            # A pause or deletion may have revoked authority meanwhile.
            if not await self._check(handle, "start"):
                return ExecutionObservation(handle, ExecutionState.PAUSED)
            try:
                owned = await self.sandbox.start(handle.attempt_id, url=media["url"], plan=media,
                                                 workspace=workspace, target=target,
                                                 result=self._record_path(handle.attempt_id))
            except ProcessGroupAlive:
                return await self.observe(handle)
            run = _Run(owned, target, workspace, len(media["formats"]), time.monotonic())
            self._runs[handle.attempt_id] = run
            run.tasks = [asyncio.ensure_future(self._follow(run)), asyncio.ensure_future(self._pump_stderr(run))]
            return self._running(handle, run)
        except Exception as exc:  # noqa: BLE001 -- every start failure is reported, never raised
            if isinstance(exc, TransferError):
                error = exc.error
            elif getattr(exc, "code", None):
                error = outcome_error(exc.code, Stage.QUEUE, detail=getattr(exc, "detail", ""),
                                      integration_id=self.descriptor.id)
            else:
                error = outcome_error("", Stage.QUEUE, detail=type(exc).__name__, integration_id=self.descriptor.id)
            return ExecutionObservation(handle, ExecutionState.FAILED, error=error)

    async def _follow(self, run: _Run) -> None:
        """The worker's progress events: per component, never double counted."""
        stream = run.owned.process.stdout
        while line := await stream.readline():
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "progress" and isinstance(event.get("component"), int):
                total = event.get("total")
                run.progress[event["component"]] = (max(0, int(event.get("downloaded") or 0)),
                                                    int(total) if isinstance(total, int) and total > 0 else None)
            elif event.get("event") == "phase" and event.get("phase") == "finalize":
                run.finalizing = True

    async def _pump_stderr(self, run: _Run) -> None:
        stream = run.owned.process.stderr
        while chunk := await stream.read(4096):
            room = _STDERR_LIMIT - len(run.stderr)
            if room > 0:
                run.stderr.extend(chunk[:room])

    # ── observation ────────────────────────────────────────────────────────

    def _running(self, handle: ExecutionHandle, run: _Run) -> ExecutionObservation:
        completed = sum(downloaded for downloaded, _total in run.progress.values())
        totals = [total for _downloaded, total in run.progress.values()]
        # A total is known only when every planned component reported its own.
        total = sum(totals) if len(totals) == run.components and all(totals) else 0
        now = time.monotonic()
        then, before = run.sample
        rate = int((completed - before) / (now - then)) if then and now > then and completed >= before else 0
        run.sample = (now, completed)
        if run.finalizing:
            # Lossless finalization is local work: no network, no progress.
            return ExecutionObservation(handle, ExecutionState.RUNNING, TransferProgress(total, completed, 0),
                                        activity=ExecutionActivity())
        return ExecutionObservation(
            handle, ExecutionState.RUNNING, TransferProgress(total, completed, max(0, rate)),
            activity=ExecutionActivity(network_active=True, bandwidth_reservation_required=True,
                                       progress_expected=bool(run.progress)))

    def _from_record(self, handle: ExecutionHandle, record: dict | None, target: Path | None
                     ) -> ExecutionObservation | None:
        """The attempt's durable completion truth, verified against the target."""
        if record is None:
            return None
        state = record.get("state")
        if state == "cancelled":
            return ExecutionObservation(handle, ExecutionState.CANCELLED)
        if state == "failed":
            return ExecutionObservation(handle, ExecutionState.FAILED, error=outcome_error(
                str(record.get("outcome") or ""), Stage.EXECUTION, detail=str(record.get("detail") or ""),
                integration_id=self.descriptor.id, context=record.get("context")))
        if state != "completed" or target is None:
            return None
        try:
            info = target.lstat()
        except FileNotFoundError:
            info = None
        size = int(record.get("bytes") or 0)
        if info is None or not stat.S_ISREG(info.st_mode) or info.st_size != size or size <= 0:
            return ExecutionObservation(handle, ExecutionState.FAILED, error=outcome_error(
                "output_missing", Stage.EXECUTION, integration_id=self.descriptor.id))
        # The workspace never outlives the writer (verification refuses a
        # payload whose declared transient path still exists).
        shutil.rmtree(self.workspace(target), ignore_errors=True)
        relative = target.relative_to(Path(self.local_root).resolve()).as_posix()
        return ExecutionObservation(handle, ExecutionState.SUCCEEDED, TransferProgress(size, size),
                                    materialization=MaterializationResult(MaterializationKind.FILE, (
                                        MaterializedEntry(relative, size),)))

    async def _terminal(self, handle: ExecutionHandle, run: _Run) -> ExecutionObservation:
        if run.terminal is not None:
            return run.terminal
        try:
            await asyncio.wait_for(asyncio.gather(*run.tasks, return_exceptions=True), timeout=5)
        except TimeoutError:
            for task in run.tasks:
                task.cancel()
        self.sandbox.revoke(public_scope(handle.attempt_id))
        if run.cancelled:
            observation = ExecutionObservation(handle, ExecutionState.CANCELLED)
        else:
            observation = self._from_record(handle, self._record(handle.attempt_id), run.target)
            if observation is None:
                # The worker ended without leaving its truth: a crash, never a success.
                code = run.owned.process.returncode
                observation = ExecutionObservation(handle, ExecutionState.FAILED, error=NormalizedError(
                    Domain.EXECUTOR, Category.TRANSFER_INTERRUPTED, Stage.EXECUTION,
                    retryability=Retryability.BACKOFF, integration_id=self.descriptor.id,
                    native_code=str(code), diagnostic=bytes(run.stderr).decode("utf-8", "replace")[-300:]))
        run.terminal = observation
        self.processes.forget(handle.attempt_id)
        self._retain(handle.attempt_id)
        return observation

    def _retain(self, attempt_id: str) -> None:
        self._finished[attempt_id] = None
        self._finished.move_to_end(attempt_id)
        while len(self._finished) > _FINISHED_MEMORY:
            oldest, _ = self._finished.popitem(last=False)
            run = self._runs.get(oldest)
            if run is not None and run.terminal is not None:
                del self._runs[oldest]

    async def observe(self, handle: ExecutionHandle) -> ExecutionObservation:
        try:
            await self._check(handle, "observe")
        except TransferError as exc:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN, error=exc.error)
        run = self._runs.get(handle.attempt_id)
        if run is not None:
            if run.owned.process.returncode is None:
                return self._running(handle, run)
            return await self._terminal(handle, run)
        alive = self.processes.alive(handle.attempt_id)
        if alive:
            # A group this attempt started before a restart: owned and still
            # running, with no native records here any more.
            return ExecutionObservation(handle, ExecutionState.RUNNING,
                                        activity=ExecutionActivity(network_active=True,
                                                                   bandwidth_reservation_required=True))
        if alive is False:
            self.processes.forget(handle.attempt_id)
        target = Path(str(handle.correlation.get("target") or "")) if handle.correlation.get("target") else None
        recorded = self._from_record(handle, self._record(handle.attempt_id), target)
        return recorded or ExecutionObservation(handle, ExecutionState.ABSENT)

    async def observe_many(self, handles: tuple[ExecutionHandle, ...]) -> ExecutionSnapshot:
        results = []
        for handle in handles:
            try:
                results.append(await self.observe(handle))
            except Exception as exc:  # noqa: BLE001 -- one handle never hides another
                results.append(ExecutionObservation(handle, ExecutionState.UNKNOWN, error=outcome_error(
                    "", Stage.RECONCILIATION, detail=type(exc).__name__, integration_id=self.descriptor.id)))
        return ExecutionSnapshot(tuple(results))

    # ── cancellation ───────────────────────────────────────────────────────

    async def cancel(self, handle: ExecutionHandle) -> ExecutionObservation:
        """Stop the attempt's whole group; only a group proven gone is CANCELLED."""
        try:
            if not await self._check(handle, "cancel"):
                raise self._failure(Category.OWNERSHIP_CONFLICT, Stage.CLEANUP, domain=Domain.LIFECYCLE)
        except TransferError as exc:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN, error=exc.error)
        run = self._runs.get(handle.attempt_id)
        if run is not None and run.owned.process.returncode is not None:
            return await self._terminal(handle, run)
        if run is not None:
            run.cancelled = True
        alive = self.processes.alive(handle.attempt_id)
        if run is None and not alive:
            if alive is False:
                self.processes.forget(handle.attempt_id)
            target = Path(str(handle.correlation.get("target") or "")) if handle.correlation.get("target") else None
            return self._from_record(handle, self._record(handle.attempt_id), target) or ExecutionObservation(
                handle, ExecutionState.ABSENT)
        if not await self.processes.terminate(run.owned if run else None, handle.attempt_id, grace=_TERMINATE_GRACE):
            return ExecutionObservation(handle, ExecutionState.UNKNOWN, error=NormalizedError(
                Domain.RECONCILIATION, Category.RECONCILIATION_FAILED, Stage.CLEANUP,
                retryability=Retryability.BACKOFF, integration_id=self.descriptor.id))
        self.sandbox.revoke(public_scope(handle.attempt_id))
        try:
            self._write_record(handle.attempt_id, {"state": "cancelled"})
        except OSError:
            pass
        if run is not None:
            return await self._terminal(handle, run)
        self.processes.forget(handle.attempt_id)
        return ExecutionObservation(handle, ExecutionState.CANCELLED)

    # ── runtime ────────────────────────────────────────────────────────────

    async def health(self) -> ExecutorHealth:
        """Every packaged tool present; nothing is ever installed at runtime."""
        now = time.monotonic()
        if self._health is not None and now - self._health[0] < _HEALTH_TTL:
            return self._health[1]
        missing = self.sandbox.tools.missing()
        health = (ExecutorHealth(True, True, frozenset({ExecutorRuntimeCapability.AGGREGATE_BANDWIDTH_CEILING}))
                  if not missing else ExecutorHealth(False, False, error=outcome_error(
                      "runtime_unavailable", Stage.QUEUE, detail="missing: " + ", ".join(missing),
                      integration_id=self.descriptor.id)))
        self._health = (now, health)
        return health

    async def set_bandwidth_ceiling(self, bytes_per_second: int) -> ExecutorRuntimeControlResult:
        """Every route of every attempt draws on this executor's one budget."""
        requested = max(0, int(bytes_per_second))
        effective = self.sandbox.egress.budget(self.descriptor.id).set_rate(requested)
        return ExecutorRuntimeControlResult(requested, effective if effective == requested else None)
