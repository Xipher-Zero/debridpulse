"""The SAB-backed execution boundary.

Core speaks only the generalized executor contract; every SAB-native fact
terminates here or in ``translation.py``. Nothing in this module knows about
provider policy, candidate equivalence, retry policy or the transfer lifecycle.

Two safety properties dominate the design, both grounded in characterization
against a real SABnzbd 5.1.3:

* **A lost start acknowledgement never resubmits.** ``prepare()`` mints a
  durable correlation token which ``start()`` submits as the native job name.
  SAB persists it as the queue ``filename`` and the history ``name``, so an
  ambiguous submission is reconciled by searching for that token. Duplicate
  submission is proven to create a second independent job, so blind retry is
  never acceptable.
* **Unreachability is never absence.** Only a valid SAB answer that omits the
  job from BOTH queue and history proves ``ABSENT``; a transport or API error
  is always ``UNKNOWN``.

Naming is separated from correlation in TIME, because SABnzbd 5.1.3 leaves no
other way. Characterized against the bundled service: a queue slot publishes
exactly one DebridPulse-settable string, the job name, and queue search matches
that name alone -- the uploaded file's name survives only into history, where it
is neither visible in the queue nor searchable, and no custom metadata field
exists. So the correlation token can only occupy the job name, and only while
the submission is still unproven. The moment the native id is bound the job is
renamed to the candidate's release name, and it carries that name for the rest
of its life.

This matters because post-processing calls ``deobfuscate(nzo, files,
nzo.final_name)``: an obfuscated dominant member, and its lookalikes, are
renamed to the JOB name. Running the bundled module against a token-named job
reproduced the production defect exactly -- ``dp-9ce15fce994c527f7712b4c4.mp4``
beside an untouched ``rename.par2``. Giving SAB the release name instead is the
whole correction; DebridPulse renames nothing afterwards and reconstructs no
collection from any single member.

Separating the two names in time is not sufficient on its own, because the job
exists and is named with the token from the instant it is created. If it were
allowed to run in that window -- which is precisely the lost-acknowledgement
window the correlation machinery exists to survive -- a small posting could
finish and be post-processed under the token, reach history, and become
permanently contaminated: the service can only rename a job that is still
queued. So the handoff is a fence, and the invariant is absolute:

    No native job may acquire or post-process while its name is the
    DebridPulse correlation token.

Every job is therefore created PAUSED, and the ``RESUME`` control is withheld
until the job is bound AND provably carries its release name. This adds no
second pause owner: core remains the only pause/resume authority, and this
executor merely declines to offer a job as resumable before it is safe to run.
A rename that is refused, lost or unobservable simply leaves the job paused for
the existing observation machinery to converge.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Awaitable, Callable

from executors.sabnzbd import topology
from executors.sabnzbd.client import (
    NATIVE_PRIORITY_PAUSED, PP_REPAIR_ONLY, SabApiError, SabTransportError,
)
from executors.sabnzbd.translation import (
    EXECUTOR_ID, failure, native_activity, native_failure, native_progress, native_state, sanitize,
    unreachable,
)
from transfers.errors import Category, Domain, Retryability, Stage, TransferError
from transfers.filesystem import safe_name
from transfers.staged_input import StagedInputError, StagedPayload
from transfers.models import (
    ExecutionControl, ExecutionFootprint, ExecutionHandle, ExecutionObservation,
    ExecutionRequest, ExecutionSnapshot, ExecutionState, ExecutorCapabilities, ExecutorClaim,
    ExecutorHealth, ExecutorRuntimeCapability, ExecutorRuntimeControlResult, ExecutorThroughput,
    IntegrationDescriptor,
    MaterializationKind, MaterializationResult, MaterializedEntry,
)

# The canonical request class this executor delivers. Executor-private: core
# never routes by it, it only asks ``claim()``.
SUPPORTED_REQUEST_KIND = "nzb"
# Where the provider parked its durable reference to the submitted manifest.
# A reference, never the manifest: the bytes live in the neutral staged-input
# owner and are streamed from there, so neither the candidate row nor this
# executor ever holds a whole large posting in memory.
CONTEXT_STAGED_INPUT = "staged_input"
# The longest native job name DebridPulse will assign. SAB sanitizes a job name
# into a folder name, and a release name is the only thing that belongs here.
_MAXIMUM_DISPLAY_NAME = 200
# Below this, SABnzbd parses the figure as a percentage rather than a byte rate
# (characterized against 5.1.3), so an absolute ceiling cannot be expressed.
_MINIMUM_EXPRESSIBLE_CEILING = 101


@dataclass(frozen=True)
class SabnzbdConfiguration:
    local_root: str
    working_directory: str
    complete_directory: str
    confirmation_delay: float = 0.05
    secrets: tuple[str, ...] = field(default=(), repr=False)


class SabnzbdExecutor:
    descriptor = IntegrationDescriptor(EXECUTOR_ID, "SABnzbd", frozenset())
    # Declared ONLY where real SAB semantics prove the neutral operation valid.
    # Deliberately absent, each for a characterized reason (see Gate 2):
    #   acquisition_gate -- SAB's pause is global across all jobs.
    #   native_assisted_retry -- SAB's retry mints a NEW nzo_id and deletes the
    #     prior history record, destroying provenance evidence.
    #   candidate_sampling -- no bounded pre-writer sample exists for an NZB.
    #   transient_input -- NNTP credentials are configuration, not per-execution input.
    capabilities = ExecutorCapabilities(
        per_execution_pause=True,
        aggregate_bandwidth_ceiling=True,
        # The service measures throughput for itself as a whole and publishes
        # no per-job rate, so this executor reports the neutral executor-level
        # figure rather than letting core invent one per execution.
        aggregate_throughput=True,
        materialization_kinds=frozenset({MaterializationKind.COLLECTION}),
    )

    def __init__(self, client, configuration: SabnzbdConfiguration,
                 authorize: Callable[[ExecutionHandle, str], Awaitable[bool]],
                 staged_input=None):
        self.client = client
        self.configuration = configuration
        self.authorize = authorize
        # The neutral durable-input owner. Borrowed to READ the submitted
        # manifest; this executor owns none of its lifecycle.
        self.staged_input = staged_input

    # --- pure contract ---------------------------------------------------

    def claim(self, subject) -> ExecutorClaim:
        """Pure: this executor delivers exactly the canonical NZB request class."""
        return ExecutorClaim(subject.request_kind == SUPPORTED_REQUEST_KIND)

    def footprint(self, work) -> ExecutionFootprint:
        """This attempt's own native transient trees, named exactly.

        The service creates one job's incomplete tree from the name the job was
        submitted with -- this attempt's correlation token -- and, characterized
        against SABnzbd 5.1.3, a later rename does NOT move it. So that tree is
        exactly nameable for THIS attempt, and core can reason about ownership,
        pre-existing material and cleanup without ever touching another
        attempt's.

        The completed tree is deliberately NOT declared. Its name is derived at
        post-processing time from the job's release name and uniquified by the
        service, so DebridPulse cannot name it in advance. Declaring a path that
        is merely plausible would be worse than declaring none: possession of a
        finished payload is taken from the location SAB itself reports.

        Before core has allocated an attempt there is nothing to report: the
        attempt's directories cannot exist yet.
        """
        attempt_id = getattr(work, "attempt_id", None)
        if not attempt_id:
            return ExecutionFootprint()
        return ExecutionFootprint(transient_trees=(
            str(Path(self.configuration.working_directory)
                / topology.INCOMPLETE_DIRECTORY_NAME / self._token_for(attempt_id)),
        ))

    def _plan_root(self, request: ExecutionRequest) -> Path:
        plan = request.work.materialization
        if plan.kind != MaterializationKind.COLLECTION or plan.target is not None:
            raise TransferError(failure(Category.UNSUPPORTED_CAPABILITY, stage=Stage.QUEUE,
                                        domain=Domain.REQUEST))
        resolved = topology.contained(self.configuration.local_root, plan.root)
        if resolved is None:
            raise TransferError(failure(Category.PATH_POLICY_VIOLATION, domain=Domain.SECURITY))
        return resolved

    @staticmethod
    def _token_for(attempt_id: str) -> str:
        """The durable correlation token of ONE DP execution attempt.

        Derived solely from the core-owned attempt identity, so a retry of the
        same candidate can never search for, adopt, or bind the previous
        attempt's native job. Never derived from a secret.
        """
        return "dp-" + hashlib.sha256(str(attempt_id).encode()).hexdigest()[:24]

    @staticmethod
    def _display_for(request: ExecutionRequest) -> str:
        """The human name this job should carry natively.

        Taken from the canonical candidate name, which is already an edge fact
        here -- core gains no naming field for a native service's benefit. It is
        sanitized as a folder name because SAB makes it one, and it is never
        compared, searched or used to identify anything.
        """
        candidate = request.work.subject.candidate
        return safe_name(str(getattr(candidate, "name", "") or ""))[:_MAXIMUM_DISPLAY_NAME]

    def prepare(self, request: ExecutionRequest) -> ExecutionHandle:
        """Allocate the durable correlation only. NO native call happens here.

        Core invokes this BEFORE its execution-admission capacity gate, so any
        native submission here would place DP work in SAB that core never
        admitted. The method is synchronous and performs no I/O at all.
        """
        root = self._plan_root(request)
        if not request.attempt_id:
            raise TransferError(failure(Category.INVALID_REQUEST, stage=Stage.QUEUE,
                                        domain=Domain.REQUEST))
        return ExecutionHandle(self.descriptor.id, request.attempt_id, {
            "token": self._token_for(request.attempt_id),
            "root": str(root),
            # The release name to give the native job. Executor-private
            # bookkeeping, recorded here so a restart can still finish naming a
            # job it had already bound. It is never identity: nothing matches,
            # searches or reconciles on it.
            "display": self._display_for(request),
        })

    # --- helpers ---------------------------------------------------------

    def _secrets(self) -> tuple[str, ...]:
        return tuple(self.configuration.secrets) + tuple(getattr(self.client, "secrets", ()) or ())

    @staticmethod
    def _token(handle: ExecutionHandle) -> str:
        return str(handle.correlation.get("token") or "")

    @staticmethod
    def _nzo(handle: ExecutionHandle) -> str:
        return str((handle.native or {}).get("nzo_id") or "")

    def _bind(self, handle: ExecutionHandle, nzo_id: str) -> ExecutionHandle:
        """This prepared handle's one legal native binding."""
        if handle.native is not None:
            return handle
        return ExecutionHandle(handle.executor_id, handle.attempt_id, handle.correlation,
                               {"nzo_id": str(nzo_id)})

    async def _check(self, handle: ExecutionHandle, action: str) -> None:
        if handle.executor_id != self.descriptor.id or not await self.authorize(handle, "observe"):
            raise TransferError(failure(Category.OWNERSHIP_CONFLICT, domain=Domain.LIFECYCLE))
        if not self._token(handle):
            raise TransferError(failure(Category.INVALID_ADAPTER_RESPONSE))
        if action != "observe" and not await self.authorize(handle, action):
            raise TransferError(failure(Category.OWNERSHIP_CONFLICT, domain=Domain.LIFECYCLE))

    def _staged(self, request: ExecutionRequest) -> StagedPayload:
        """This execution's durable input reference, as the provider left it."""
        raw = (request.work.subject.candidate.context or {}).get(CONTEXT_STAGED_INPUT)
        if self.staged_input is None:
            raise TransferError(failure(Category.INVALID_REQUEST, stage=Stage.QUEUE,
                                        domain=Domain.REQUEST))
        try:
            return StagedPayload.from_context(raw)
        except StagedInputError as exc:
            raise TransferError(failure(Category.INVALID_REQUEST, stage=Stage.QUEUE,
                                        domain=Domain.REQUEST)) from exc

    async def _locate(self, token: str, nzo_id: str = ""):
        """Find this execution's native job. Returns ``(slot, in_history)``.

        ``None`` means a VALID SAB answer that contains the job in neither
        queue nor history -- the only evidence of genuine absence. Transport
        and API errors propagate; they are never absence.

        The narrowing search is by NAME only while the job is still unbound,
        because the token is the job's name only in that window. Once the
        native id is bound it is the identity, and the job has been renamed to
        its release name -- so searching for the token then would prove
        "absent" about a job that is present.
        """
        search = None if nzo_id else (token or None)
        for slot in await self.client.queue_slots(search=search):
            if self._matches(slot, token, nzo_id):
                return slot, False
        history = await self.client.history_slots(search=search,
                                                  nzo_id=nzo_id or None)
        for slot in history:
            if self._matches(slot, token, nzo_id):
                return slot, True
        return None

    @staticmethod
    def _matches(slot, token: str, nzo_id: str) -> bool:
        identity = str(_get(slot, "nzo_id") or "")
        if nzo_id:
            return identity == nzo_id
        name = str(_get(slot, "name") or _get(slot, "filename") or "")
        return bool(token) and name == token

    # --- start -----------------------------------------------------------

    async def start(self, request: ExecutionRequest, handle: ExecutionHandle) -> ExecutionObservation:
        secrets = self._secrets()
        try:
            await self._check(handle, "start")
            if self.prepare(request) != handle:
                raise TransferError(failure(Category.OWNERSHIP_CONFLICT, domain=Domain.LIFECYCLE))
            token = self._token(handle)
            staged = self._staged(request)
        except TransferError as exc:
            return ExecutionObservation(handle, ExecutionState.FAILED, error=exc.error)

        try:
            # The staged input is proven intact BEFORE a byte of it is sent, so
            # altered or truncated input is never submitted, and it is streamed
            # rather than read whole -- a manifest can be hundreds of megabytes.
            with self.staged_input.opened(staged) as manifest:
                # ALWAYS paused on arrival, whatever core intends. The job is
                # created carrying the correlation token, and nothing may run
                # under that name -- including during an acknowledgement this
                # call never receives.
                nzo_id = await self.client.addfile(
                    manifest, job_name=token, pp=PP_REPAIR_ONLY,
                    priority=NATIVE_PRIORITY_PAUSED,
                )
        except StagedInputError as exc:
            # Nothing was submitted: the input could not be proven.
            return ExecutionObservation(handle, ExecutionState.FAILED, error=failure(
                Category.INVALID_REQUEST, stage=Stage.QUEUE, domain=Domain.REQUEST,
                diagnostic=sanitize(str(exc), secrets)))
        except (SabTransportError, SabApiError) as exc:
            # The submission may or may not have reached SAB. Reconcile by the
            # durable correlation token; NEVER submit a second time.
            return await self._reconcile_ambiguous_start(handle, sanitize(str(exc), secrets),
                                                         paused=request.paused)
        except Exception as exc:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN,
                                        error=unreachable(sanitize(str(exc), secrets)))
        return await self._establish_native_name(self._bind(handle, nzo_id),
                                                 paused=request.paused)

    async def _establish_native_name(self, handle: ExecutionHandle, *,
                                     paused: bool) -> ExecutionObservation:
        """Complete the submission: name the bound job, then release the fence.

        This runs inside ``start()``, before core has accepted the native
        binding, so it reads and controls the job through the native boundary
        directly rather than through the authorized observation path -- the
        authorization for this work is the one ``start`` already took, and the
        bound handle is not yet the one core holds.

        The job was created paused and carries the correlation token. It is
        renamed, and resumed ONLY when the rename is observed to have taken and
        core's canonical lifecycle says this execution should be running.
        Naming that cannot be completed or proven is not an error: the job
        stays paused under the token, which is exactly where it is safe, and
        the next observation converges it.
        """
        token = self._token(handle)
        display = str(handle.correlation.get("display") or "")
        nzo_id = self._nzo(handle)
        try:
            located = await self._locate(token, nzo_id)
            if located is None:
                return ExecutionObservation(handle, ExecutionState.ABSENT)
            slot, in_history = located
            native_name = await self._converge_display_name(handle, slot, in_history)
            if not paused and not in_history and display and native_name == display:
                # Named, and core wants it running: release the fence.
                await self.client.resume(nzo_id)
                located = await self._locate(token, nzo_id)
                if located is None:
                    return ExecutionObservation(handle, ExecutionState.ABSENT)
                slot, in_history = located
            return self._observation(handle, slot, in_history, native_name)
        except (SabTransportError, SabApiError) as exc:
            # The job exists and is paused under the token. Uncertain, safe.
            return ExecutionObservation(handle, ExecutionState.UNKNOWN,
                                        error=unreachable(sanitize(str(exc), self._secrets())))

    async def _converge_display_name(self, handle: ExecutionHandle, slot,
                                     in_history: bool) -> str:
        """Ensure a queued job carries its release name; report the name it has.

        An acknowledgement is not truth, and DebridPulse may have stopped
        between binding a job and naming it. Observation is where that is
        noticed, and renaming is only possible while the job is still queued.
        A job already carrying its release name, or one renamed to something
        else entirely, is left alone.

        The returned name is what the caller must judge the fence by, so a
        rename that has just succeeded does not cost the job another cycle
        paused.
        """
        observed = str(_get(slot, "filename") or _get(slot, "name") or "")
        if in_history or observed != self._token(handle):
            return observed
        display = str(handle.correlation.get("display") or "")
        nzo_id = self._nzo(handle)
        if not display or not nzo_id:
            return observed
        try:
            renamed = await self.client.rename(nzo_id, display)
        except Exception:
            # Uncertain, like every other unacknowledged native call here.
            return observed
        return display if renamed else observed

    async def _reconcile_ambiguous_start(self, handle: ExecutionHandle, diagnostic: str, *,
                                         paused: bool) -> ExecutionObservation:
        """Resolve a lost/ambiguous submission acknowledgement without resubmitting."""
        try:
            located = await self._locate(self._token(handle))
        except Exception:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN, error=unreachable(diagnostic))
        if located is None:
            # Could not prove the submission landed. Uncertain, never failed:
            # core keeps ownership and no duplicate job is ever created here.
            return ExecutionObservation(handle, ExecutionState.UNKNOWN, error=unreachable(diagnostic))
        slot, in_history = located
        # The job landed after all. It is paused under the correlation token,
        # so it has acquired nothing; finish the submission exactly as the
        # acknowledged path does.
        return await self._establish_native_name(
            self._bind(handle, str(_get(slot, "nzo_id") or "")), paused=paused)

    # --- observation -----------------------------------------------------

    async def observe_many(self, handles: tuple[ExecutionHandle, ...]) -> ExecutionSnapshot:
        """ONE bulk queue snapshot (plus at most one history snapshot),
        reconstructed locally into per-handle observations.

        Absence is concluded only from snapshots that SAB itself reports as
        complete; a paginated or truncated listing yields ``UNKNOWN`` for the
        handles it could not account for, never ``ABSENT``.
        """
        if not handles:
            return ExecutionSnapshot(())
        permitted, results = [], []
        for handle in handles:
            try:
                await self._check(handle, "observe")
                permitted.append(handle)
            except TransferError as exc:
                results.append(ExecutionObservation(handle, ExecutionState.UNKNOWN, error=exc.error))
        if not permitted:
            return ExecutionSnapshot(tuple(results))

        secrets = self._secrets()
        try:
            queue = await self.client.queue_snapshot()
        except (SabTransportError, SabApiError) as exc:
            error = unreachable(sanitize(str(exc), secrets))
            return ExecutionSnapshot(
                tuple(results) + tuple(ExecutionObservation(item, ExecutionState.UNKNOWN, error=error)
                                       for item in permitted), error)

        located = self._index(queue.slots)
        outstanding = [item for item in permitted if self._key(item) not in located]
        history = None
        if outstanding:
            try:
                history = await self.client.history_snapshot()
            except (SabTransportError, SabApiError) as exc:
                error = unreachable(sanitize(str(exc), secrets))
                for item in permitted:
                    slot = located.get(self._key(item))
                    results.append(self._observation(self._bind(item, str(_get(slot, "nzo_id") or "")),
                                                     slot, False,
                                                     str(_get(slot, "filename") or _get(slot, "name") or ""))
                                   if slot is not None else
                                   ExecutionObservation(item, ExecutionState.UNKNOWN, error=error))
                return ExecutionSnapshot(tuple(results))
            located |= {key: (slot, True) for key, slot in self._index(history.slots).items()
                        if key not in located}

        # Absence is only knowable when BOTH listings were authoritative.
        authoritative = queue.complete and (history is None or history.complete)
        for item in permitted:
            found = located.get(self._key(item))
            if found is None:
                if authoritative:
                    results.append(ExecutionObservation(item, ExecutionState.ABSENT))
                else:
                    results.append(ExecutionObservation(
                        item, ExecutionState.UNKNOWN,
                        error=unreachable("SAB listing was not authoritative")))
                continue
            slot, in_history = found if isinstance(found, tuple) else (found, False)
            bound = self._bind(item, str(_get(slot, "nzo_id") or ""))
            native_name = await self._converge_display_name(bound, slot, in_history)
            results.append(self._observation(bound, slot, in_history, native_name))
        return ExecutionSnapshot(tuple(results))

    @staticmethod
    def _key(handle: ExecutionHandle) -> str:
        """What identifies this handle's native job in a bulk listing: its bound
        ``nzo_id`` when it has one, otherwise its correlation token."""
        native = (handle.native or {}).get("nzo_id")
        return str(native) if native else str(handle.correlation.get("token") or "")

    @staticmethod
    def _index(slots) -> dict:
        """Bulk slots keyed by BOTH native id and job name, so a handle matches
        whether or not it has bound its native identity yet."""
        index = {}
        for slot in slots:
            for key in (_get(slot, "nzo_id"), _get(slot, "name"), _get(slot, "filename")):
                if key:
                    index.setdefault(str(key), slot)
        return index

    async def _observe(self, handle: ExecutionHandle) -> ExecutionObservation:
        try:
            await self._check(handle, "observe")
        except TransferError as exc:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN, error=exc.error)
        try:
            located = await self._locate(self._token(handle), self._nzo(handle))
        except (SabTransportError, SabApiError) as exc:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN,
                                        error=unreachable(sanitize(str(exc), self._secrets())))
        except Exception as exc:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN,
                                        error=unreachable(sanitize(str(exc), self._secrets())))
        if located is None:
            # A VALID answer that omits the job from queue AND history.
            return ExecutionObservation(self._bound_or_self(handle), ExecutionState.ABSENT)
        slot, in_history = located
        bound = self._bind(handle, str(_get(slot, "nzo_id") or ""))
        native_name = await self._converge_display_name(bound, slot, in_history)
        return self._observation(bound, slot, in_history, native_name)

    @staticmethod
    def _bound_or_self(handle: ExecutionHandle) -> ExecutionHandle:
        return handle

    def _observation(self, handle: ExecutionHandle, slot, in_history: bool,
                     native_name: str = "") -> ExecutionObservation:
        status = str(_get(slot, "status") or "")
        # THE FENCE. A job still carrying the correlation token has not been
        # named yet, and nothing may run under that name, so it is not offered
        # as resumable. Core is still the only pause/resume authority -- it
        # simply has nothing to act on until naming is established.
        fenced = not in_history and native_name == self._token(handle)
        state = native_state(status, in_history=in_history)
        secrets = self._secrets()
        if state == ExecutionState.FAILED:
            message = str(_get(slot, "fail_message") or "")
            # A failed job's SAB `storage` points into the working area and is
            # never material.
            return ExecutionObservation(handle, ExecutionState.FAILED,
                                        error=native_failure(message, secrets))
        if state != ExecutionState.SUCCEEDED:
            controls = frozenset()
            if state == ExecutionState.PAUSED:
                controls = frozenset() if fenced else frozenset({ExecutionControl.RESUME})
            elif state in {ExecutionState.QUEUED, ExecutionState.RUNNING}:
                controls = frozenset({ExecutionControl.PAUSE})
            return ExecutionObservation(handle, state, progress=native_progress(slot),
                                        activity=native_activity(status), controls=controls)
        return self._succeeded(handle, slot)

    def _succeeded(self, handle: ExecutionHandle, slot) -> ExecutionObservation:
        """Deliver the repaired payload into the core plan root and report it.

        The payload is located SOLELY from the position SAB reports for this
        job. The completed directory is named from the job's release name and
        uniquified by the service, so no path DebridPulse could compose from its
        own correlation token would be right -- and an output path was never a
        safe way to identify anything. A report DebridPulse cannot contain
        inside its own working area is uncertainty, never a guess.
        """
        plan_root = str(handle.correlation.get("root") or "")
        storage = str(_get(slot, "storage") or "")
        reported = Path(storage) if storage else None
        parent = (reported if reported is not None and reported.is_dir()
                  else (reported.parent if reported is not None else None))
        if parent is None or topology.contained(self.configuration.working_directory,
                                                str(parent)) is None:
            return ExecutionObservation(
                handle, ExecutionState.UNKNOWN,
                error=failure(Category.MATERIALIZATION_FAILED, stage=Stage.VERIFICATION,
                              retryability=Retryability.BACKOFF))
        source = str(parent)
        exact = _int(_get(slot, "bytes"))
        if topology.contained(self.configuration.local_root, plan_root) is None:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN,
                                        error=failure(Category.PATH_POLICY_VIOLATION,
                                                      domain=Domain.SECURITY))
        if not topology.deliver(source, plan_root):
            # SAB succeeded but DebridPulse could not take ownership of the
            # payload. Uncertain -- never a false success, never falsely terminal.
            return ExecutionObservation(
                handle, ExecutionState.UNKNOWN,
                error=failure(Category.MATERIALIZATION_FAILED, stage=Stage.VERIFICATION,
                              retryability=Retryability.BACKOFF))
        entries = topology.collection_entries(plan_root)
        if not entries:
            return ExecutionObservation(
                handle, ExecutionState.UNKNOWN,
                error=failure(Category.MATERIALIZATION_FAILED, stage=Stage.VERIFICATION,
                              retryability=Retryability.BACKOFF))
        return ExecutionObservation(
            handle, ExecutionState.SUCCEEDED,
            progress=native_progress(slot, exact_bytes=exact),
            materialization=MaterializationResult(
                MaterializationKind.COLLECTION,
                tuple(MaterializedEntry(name, size) for name, size in entries)),
        )

    # --- controls --------------------------------------------------------

    async def pause(self, handle: ExecutionHandle) -> ExecutionObservation:
        return await self._control(handle, resume=False)

    async def resume(self, handle: ExecutionHandle) -> ExecutionObservation:
        return await self._control(handle, resume=True)

    async def _control(self, handle: ExecutionHandle, *, resume: bool) -> ExecutionObservation:
        action = "resume" if resume else "pause"
        try:
            await self._check(handle, action)
        except TransferError as exc:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN, error=exc.error)
        before = await self._observe(handle)
        if before.state == ExecutionState.UNKNOWN or not before.resumable:
            return before
        if resume and ExecutionControl.RESUME not in before.controls:
            # The fence, honoured even when asked directly: an unnamed job is
            # never resumed, whoever asks. Observed truth is returned unchanged.
            return before
        nzo_id = self._nzo(before.handle) or self._nzo(handle)
        try:
            # The acknowledgement is NOT truth: SAB acknowledges a control for
            # an unknown id just as readily. Only re-observation proves it.
            await (self.client.resume(nzo_id) if resume else self.client.pause(nzo_id))
        except Exception as exc:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN,
                                        error=unreachable(sanitize(str(exc), self._secrets())))
        return await self._observe(handle)

    async def cancel(self, handle: ExecutionHandle) -> ExecutionObservation:
        """Ask SAB to remove this job and report OBSERVED truth."""
        try:
            await self._check(handle, "cancel")
        except TransferError as exc:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN, error=exc.error)
        before = await self._observe(handle)
        if before.state == ExecutionState.UNKNOWN:
            return before
        if not before.resumable:
            return before
        nzo_id = self._nzo(before.handle) or self._nzo(handle)
        try:
            await self.client.delete(nzo_id)
        except Exception as exc:
            return ExecutionObservation(handle, ExecutionState.UNKNOWN,
                                        error=unreachable(sanitize(str(exc), self._secrets())))
        after = await self._observe(handle)
        if after.stopped:
            if after.state == ExecutionState.ABSENT:
                return ExecutionObservation(after.handle, ExecutionState.CANCELLED, before.progress)
            return after
        return ExecutionObservation(after.handle, ExecutionState.UNKNOWN, before.progress,
                                    error=failure(Category.RECONCILIATION_FAILED,
                                                  stage=Stage.CLEANUP,
                                                  retryability=Retryability.BACKOFF))

    # --- health ----------------------------------------------------------

    async def health(self) -> ExecutorHealth:
        try:
            version = await self.client.version() if hasattr(self.client, "version") else "ok"
        except Exception as exc:
            return ExecutorHealth(False, False, error=unreachable(sanitize(str(exc), self._secrets())))
        ready = bool(version)
        # The aggregate ceiling is enforceable exactly while the service answers.
        return ExecutorHealth(True, ready,
                              frozenset({ExecutorRuntimeCapability.AGGREGATE_BANDWIDTH_CEILING}))

    async def aggregate_download_throughput(self) -> ExecutorThroughput:
        """The one rate this executor can truthfully measure: its own.

        An unreachable or unusable service is UNKNOWN, never its last value:
        the operator must not be shown a speed that is no longer happening.
        """
        try:
            return ExecutorThroughput(await self.client.download_throughput(), True)
        except (SabTransportError, SabApiError):
            return ExecutorThroughput(0, False)

    async def set_bandwidth_ceiling(self, bytes_per_second: int) -> ExecutorRuntimeControlResult:
        """Enforce the core-assigned share as the service's global download
        limit, and confirm it by reading the effective value back.

        ``0`` is unlimited. A value of 1..100 bytes/sec is NOT expressible:
        SABnzbd 5.1.3 reinterprets any figure resolving into that range as a
        PERCENTAGE (``downloader.py::limit_speed``) and, with no maximum
        bandwidth configured, applies no absolute limit at all. Reporting such a
        ceiling as enforced would be false, so it is reported unproven and the
        core owner fails closed.
        """
        requested = max(0, int(bytes_per_second))
        if 0 < requested < _MINIMUM_EXPRESSIBLE_CEILING:
            return ExecutorRuntimeControlResult(requested, None, failure(
                Category.UNSUPPORTED_CAPABILITY, retryability=Retryability.NEVER,
                diagnostic="ceilings below 101 bytes/sec are not expressible"))
        try:
            effective = await self.client.set_speedlimit(str(requested))
        except Exception as exc:
            return ExecutorRuntimeControlResult(
                requested, None, unreachable(sanitize(str(exc), self._secrets())))
        return ExecutorRuntimeControlResult(
            requested, effective if effective == requested else None,
            None if effective == requested else failure(
                Category.RECONCILIATION_FAILED, stage=Stage.RECONCILIATION,
                retryability=Retryability.BACKOFF,
                diagnostic="the service did not confirm the assigned ceiling"))


def _get(slot, key):
    if isinstance(slot, dict):
        return slot.get(key)
    return getattr(slot, key, None)


def _int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
