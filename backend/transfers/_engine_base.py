"""Universal transfer lifecycle.

Integrations supply facts through contracts. This owner admits requests, creates
durable attempts, applies retry policy, confirms possession, and orchestrates
cleanup and post-processing. It imports no concrete provider or executor.

Concurrency / mutation-fencing model (DP 1.0.12 leveling remediation)
-------------------------------------------------------------------------------
Four mechanisms exist. No transfer mutation command needs a fifth. This is an
audited claim, not an aspiration -- the table below names, for the actual
PRODUCTION stack (``transfers.convergence_engine.TransferEngine`` /
``transfers.recovery_repository.TransferRepository``), exactly which
mechanism(s) protect each command, verified by reading every override in the
leveled production engine MRO (``convergence_engine.TransferEngine`` ->
``engine.TransferEngine`` -> ``_engine_recovery.TransferEngine`` ->
``_engine_base.TransferEngine``; none of ``pause``/``resume``/``resume_all``/
``cancel``/``delete``/``select_artifact``/``submit``/``activate_candidate_command``
are further overridden below ``convergence_engine.TransferEngine`` except
where the table says so). A claim here that is not also proven by a named
regression test is not a claim this module makes.

1. **Per-transfer asyncio lock** (``self._transfer_locks``, initialized here,
   this class): used by ``resolve_pending()`` (this base class; held for a
   transfer exactly while that cycle has admitted resolution units of it in
   flight) and ``cancel()`` (this base class; production has no override).
   ``convergence_engine.TransferEngine`` -- the sole owner of every
   recovery/control decision, per CANON-001 -- also reaches into this same
   shared dict from its own ``retry()`` (both the operator-retry branch and
   the ``reacquire=True`` terminal-transfer-reacquisition branch, the latter
   via ``_reacquire_transfer()``, which along with pause/resume/pause_all/
   resume_all/refresh/candidate-refresh scheduling is defined ONLY on
   ``convergence_engine.TransferEngine``; no lower class defines any of
   them). It is NOT used by ``select_artifact``, ``submit`` itself, or
   ``activate_candidate_command`` -- those commands are safe through
   mechanisms 2-4 below instead, not through this lock. Do not assume this
   lock protects a command not named in this paragraph.
2. **Per-execution-attempt convergence lock** (``self._convergence_lock``,
   this class): serializes every native pause/resume/observe/cancel call
   against ONE execution handle, across every caller that might touch it --
   ordinary scheduler observation (``_converge_execution``, entered for every
   observed execution; it mutates only through controls the current
   observation advertises),
   ``pause``/``resume``/``resume_all`` (via ``_converge_execution`` per
   artifact), and candidate activation's own old-writer retirement dance
   (``transfers.candidate_activation.activate_candidate``). Proven for
   "manual switch vs pause" and "manual switch vs ordinary scheduler
   execution observation" by
   ``tests/test_recovery_command_concurrency.py::test_manual_activation_vs_pause_is_deterministic``
   and ``::test_manual_activation_vs_scheduler_execution_observation_is_deterministic``.
3. **Exclusive recovery claim** (``transfers.recovery_execution
   .RecoveryClaim`` / ``TransferRepository.claim_recovery``, DB-backed):
   exclusive across every ``RecoveryTrigger`` INCLUDING
   ``USER_CANDIDATE_SWITCH`` -- a concurrent AUTO_RETRY, USER_RETRY, RESUME,
   or operator candidate switch for the same artifact can never interleave.
   Production ``resume``/``resume_all``/``retry`` route each artifact
   through this SAME claim system via ``recover_artifact(trigger=...)``, and
   so does every automatic failure path (``_recover_artifact`` ->
   ``recover_artifact(trigger=AUTO_RETRY)``) -- never a second, unclaimed
   mutation path. Proven for "manual switch vs RESUME/USER_RETRY/AUTO_RETRY"
   by
   ``test_manual_activation_vs_resume_is_generation_safe``,
   ``test_manual_activation_vs_retry_is_generation_safe``, and
   ``test_manual_activation_vs_auto_retry_is_generation_safe``.
4. **DB-transaction atomicity, including epoch-CAS** (every mutating
   repository method: ``BEGIN IMMEDIATE`` plus a fresh read immediately
   before the write): the ONLY layer protecting a command with no claim or
   per-attempt lock of its own -- ``delete()`` (bumps ``torrents
   .lifecycle_epoch``; nothing else does), ``select_artifact``/``retry``/
   ``cancel_with_execution_cleanup`` (each re-check ``expected_epoch=
   transfer.epoch`` before writing, so a DELETE that lands first is always
   detected rather than silently raced past), candidate activation's own
   commit (``transition_recovery`` refuses to apply once the transfer row
   already reads ``deleted``/``completed``/``consolidated``/``cancelled``),
   and parent-lifecycle aggregation (Sections 21-22,
   ``TransferRepository.aggregate_lifecycle`` /
   ``force_queued_for_autonomous_wait``, which runs on its own schedule and
   therefore cannot reasonably hold a claim or a per-transfer lock for its
   whole read-decide-write). Proven for "manual switch vs DELETE" by
   ``test_manual_activation_vs_delete_never_reauthorizes_or_corrupts``:
   whichever of the switch's ``transition_recovery`` commit or ``delete``'s
   own cleanup UPDATE reaches SQLite's write lock first is respected, and
   the other reads fresh (never stale) state before it writes, so it either
   cleanly no-ops (activation sees an already-deleted transfer) or still
   correctly retires whatever writer is currently authorized (delete's
   cleanup query re-reads ``authorized=1`` at commit time, never a
   pre-race id).

Ordering: (1) and (2) are acquired, when needed, OUTSIDE (3) -- a caller
already holding a recovery claim never needs (1)/(2) for the same artifact,
since (3) already excludes every other recovery-triggered mutation, though
(2) still applies underneath it for the specific execution-handle dance
(candidate activation's writer retirement acquires (2) while already holding
its (3) claim). (4) always applies last/innermost regardless of which of
(1)-(3), if any, guard the caller -- it is what makes even an unclaimed,
unlocked mutation (``delete``, ordinary progress persistence, plain
aggregation) safe against every other writer.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
import logging
from pathlib import Path
import time
from weakref import WeakValueDictionary

from transfers.canonical import CanonicalOwnership
from transfers.contracts import Cleanup, Inventory, ProviderInputContinuation
from transfers import codec
from transfers.errors import (
    Category, Domain, NormalizedError, Recovery, Retryability, Stage,
    TransferError, unknown_failure,
)
from transfers.filesystem import (
    adoptable_material, destination, material_initially_absent, materialization_plan, retire_materialization,
    safe_name, validate_plan, verified_material_paths, verify_materialization,
)
from transfers.input_required import EphemeralInputBroker, InputChallengeStore, InputSubmissionRejected
from transfers.models import (
    Artifact, CancellationInitiator, Capability, CleanupAuthority, CleanupDirective,
    ExecutionActivity, ExecutionAttempt, ExecutionControl, ExecutionFootprint, ExecutionHandle, ExecutionObservation,
    ExecutionRequest, ExecutionSnapshot, ExecutionState, ExecutionSubject, ExecutionWork, ExecutorRuntimeCapability,
    ExecutorThroughput, InputChallenge,
    InputOrigin, InputRequirement, MaterializationAdmissionKind, OutcomeKind, Ownership, ProviderObservation,
    RequestRecord, ResolutionAttempt, ResolutionResult, ResourceState, SizeKnowledge,
    TransferOutcome, TransferRequest, TransferCandidate, TransferState, new_identity,
)
from transfers.mirrors import EvidenceContext, shared_size
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import SelectionAuthority, TransferRepository
from transfers.runtime_coordination import ExecutionRuntimeCoordinator
from transfers.runtime_telemetry import ExecutionThroughputMeter


logger = logging.getLogger(__name__)


class _CleanupOwnershipLost(Exception):
    """The cleanup claim this worker held was taken over while its provider call
    was running; the call has been aborted and nothing may be finalized."""


# Request states the resolution scheduler may admit; every other state is
# owned by a later lifecycle stage (or is terminal) and is never resolution work.
_SCHEDULABLE_REQUEST_STATES = frozenset({"pending", "waiting", "materializing", "resolving"})


class _ResolutionCycle:
    """Admission bookkeeping of ONE ``resolve_pending()`` cycle.

    Ephemeral by construction: created when a cycle starts, dropped when it
    ends, never persisted and never read by anything but the resolution
    scheduler. It records what this cycle already admitted; it holds no
    capacity truth (``_resolution_slots`` does) and no lifecycle truth (the
    repository does).
    """

    def __init__(self):
        # Set whenever an admission boundary opens: a provider slot was
        # released, an admitted unit finished, or a durable mutation made
        # resolution work runnable (``_resolution_opportunity``).
        self.opportunity = asyncio.Event()
        self.units: dict[asyncio.Task, int] = {}
        # Admitted units that may still claim a provider-resolution slot.
        self.slot_bound: set[asyncio.Task] = set()
        # Per entered transfer: its schedulable requests this cycle has not
        # served, as of the transfer's latest census of current durable truth
        # (``_resolution_census``). A request created by this cycle's own
        # work (e.g. a manifest child) is THIS cycle's work: the unit that
        # created it takes the census that finds it.
        self.remaining: dict[int, list[RequestRecord]] = {}
        # Request id -> the durable scheduling incarnation this cycle already
        # served (``None`` while that unit is still in flight). Re-reading
        # current truth therefore never admits an in-flight request twice and
        # never hot-loops a served request that still looks schedulable; only
        # a request this cycle has not seen, or one whose durable facts were
        # rewritten since it was served (a requeue), is work again.
        # A provider-input challenge id is recorded here too and stays
        # ``None``: one continuation per challenge identity per cycle.
        self.admitted: dict[str, tuple | None] = {}
        self.retired: set[int] = set()
        # Transfers ``_resolution_opportunity`` named since the scheduler last
        # re-entered them. Consumed by the scheduler at its own boundary, so a
        # census or liveness read that was already in progress when the
        # mutation landed can never overwrite the request to look again.
        self.reconsider: set[int] = set()
        self.locks: dict[int, asyncio.Lock] = {}
        self.served: dict[int, int] = {}
        # The current bootstrap round: the pathless transfers that were
        # runnable when it started and are still owed their one turn. ``None``
        # means no round is populated; membership never grows mid-round.
        self.round: set[int] | None = None
        self.changed: set[int] = set()
        self.failure: BaseException | None = None

    def in_flight(self, transfer_id: int) -> int:
        return sum(owner == transfer_id for owner in self.units.values())


class TransferEngine:
    def __init__(self, repository: TransferRepository, registry: IntegrationRegistry, *,
                 download_root: str, policy: TransferPolicy | None = None, postprocessors=(), clock=time.time):
        self.repository = repository
        self.registry = registry
        self.canonical = CanonicalOwnership(repository)
        self.challenges = InputChallengeStore(clock=clock)
        self.inputs = EphemeralInputBroker(clock=clock)
        self.root = str(Path(download_root).resolve())
        self.policy = policy or TransferPolicy()
        self.postprocessors = tuple(postprocessors)
        self.clock = clock
        self._cycle_lock = asyncio.Lock()
        self._resolution_cycle_lock = asyncio.Lock()
        self._execution_cycle_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._paths_lock = asyncio.Lock()
        self._postprocess_lock = asyncio.Lock()
        self._resolution_slots = asyncio.Semaphore(max(1, self.policy.resolution_concurrency))
        self._resolution_cycle: _ResolutionCycle | None = None
        # Weak-value lock maps: a caller holding/awaiting a lock keeps the strong
        # local reference that keeps its entry alive; once every holder/waiter
        # for a key is gone the entry is collected instead of retaining one
        # asyncio.Lock per transfer/cohort id for the life of the process. Never
        # delete-on-release: that can race with a concurrent waiter and hand out
        # two lock objects for the same active key.
        self._transfer_locks = WeakValueDictionary()
        self._execution_convergence_locks = WeakValueDictionary()
        self._collection_affinity_locks = WeakValueDictionary()
        self._cohort_locks = WeakValueDictionary()
        self.dispatch_permitted = True
        # Positive, execution-layer-owned evidence (DP 1.0.12 recovery
        # leveling, Section 9) that the REAL _dispatch() reached the capacity
        # admission gate for this artifact -- having already passed target
        # validation, candidate expiry, existing-payload, executor.prepare()
        # (no InputRequirement), and storage/pause admission via actual code
        # execution, not a presentation-side reconstruction of those gates --
        # and was rejected there. Reset once per reconcile_executions() cycle
        # (see reconcile_executions below) and populated only at the one real
        # capacity check in _dispatch() below; never persisted, never another
        # recovery lifecycle. Presentation only ever reads this set; it never
        # decides independently that capacity is the blocker.
        self._capacity_only_blocked: set[int] = set()
        # The one core owner of executor runtime limits (global download
        # bandwidth split across reserved executors).
        self.runtime = ExecutionRuntimeCoordinator(lambda: self.registry, repository)
        # The one core owner of current aggregate download throughput. Rebuilt
        # from scratch each reconcile cycle, so it can never retain a stale rate.
        self.throughput = ExecutionThroughputMeter()
        # Executors whose acquisition gate global pause has confirmed engaged.
        self._acquisition_gated: set[str] = set()

    async def initialize(self):
        await self.repository.initialize()
        await self.canonical.initialize()
        await self.challenges.initialize()

    def configure_policy(self, policy):
        """Called only after application admission has drained active work."""
        self.policy = policy
        self._resolution_slots = asyncio.Semaphore(max(1, policy.resolution_concurrency))

    def configure_runtime_limits(self, max_download_bytes_per_second: int) -> None:
        """Inject the canonical global runtime limit into its core owner."""
        self.runtime.configure(max_download_bytes_per_second)

    async def converge_runtime_limits(self):
        """Converge executor ceilings to the configured global limit; neutral status."""
        async with self._dispatch_lock:
            return await self.runtime.converge()

    async def _live(self, transfer_id: int, *, admission=False) -> bool:
        transfer = await self.repository.get(transfer_id)
        if not transfer or transfer.state in {TransferState.DELETED, TransferState.COMPLETED, TransferState.CONSOLIDATED, TransferState.CANCELLED}:
            return False
        return not admission or (not transfer.paused and not await self.repository.globally_paused())

    @staticmethod
    def _error(category, stage, *, domain=Domain.INTERNAL, retryability=Retryability.UNKNOWN):
        return NormalizedError(domain, category, stage, retryability=retryability)

    @classmethod
    def _authoritative_provider_result(cls, provider_id: str, result: ResolutionResult, *,
                                       request_kind: str) -> ResolutionResult:
        """Validate and stamp provider output with the selected route identity
        and the canonical request class each candidate was resolved for."""
        if not isinstance(result, ResolutionResult):
            raise TransferError(cls._error(
                Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION, domain=Domain.PROVIDER,
                retryability=Retryability.NEVER,
            ))

        def authoritative_resource(value):
            if value is None:
                return None
            if value.provider_id and value.provider_id != provider_id:
                raise TransferError(cls._error(
                    Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION, domain=Domain.PROVIDER,
                    retryability=Retryability.NEVER,
                ))
            return value if value.provider_id == provider_id else replace(value, provider_id=provider_id)

        candidates = []
        for candidate in result.candidates:
            if not isinstance(candidate, TransferCandidate):
                raise TransferError(cls._error(
                    Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION, domain=Domain.PROVIDER,
                    retryability=Retryability.NEVER,
                ))
            if candidate.provider_id and candidate.provider_id != provider_id:
                raise TransferError(cls._error(
                    Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION, domain=Domain.PROVIDER,
                    retryability=Retryability.NEVER,
                ))
            candidates.append(replace(
                candidate, provider_id=provider_id, resource=authoritative_resource(candidate.resource),
                request_kind=str(request_kind),
            ))

        observation = result.observation
        if observation is not None:
            observation = replace(observation, resource=authoritative_resource(observation.resource))
        return replace(result, candidates=tuple(candidates), observation=observation)

    async def submit(self, requests: tuple[TransferRequest, ...], *, name="", source="manual", priority=0, reacquire=True, deduplicate=True):
        if not requests or len(requests) > 100 or any(not isinstance(item, TransferRequest) or not item.kind or not item.payload for item in requests):
            raise TransferError(self._error(Category.INVALID_REQUEST, Stage.SUBMISSION, domain=Domain.REQUEST, retryability=Retryability.NEVER))
        transfer, created = await self.repository.admit(requests, name=safe_name(name or requests[0].name or "Transfer"), source=source, priority=priority, deduplicate=deduplicate)
        if not created and reacquire and transfer.state in {TransferState.COMPLETED, TransferState.DELETED}:
            if not await self.retry(transfer.id, reacquire=True):
                raise TransferError(self._error(Category.RECOVERY_FAILED, Stage.RECONCILIATION, domain=Domain.RECONCILIATION))
        elif await self.repository.globally_paused():
            await self.repository.state(transfer.id, TransferState.PAUSED)
        # A running resolution cycle reassesses at once; it never makes a new
        # transfer wait for previously admitted work to drain.
        self._resolution_opportunity(transfer.id)
        return await self.repository.get(transfer.id)

    async def tick(self):
        """One bounded scheduling/reconciliation cycle; retry delays never sleep a lock."""
        async with self._cycle_lock:
            await self.resolve_pending()
            await self.reconcile_executions()
            await self.process_postprocessors()

    async def resolve_pending(self):
        """Provider cadence can run independently of fast execution observation.

        One cycle admits resolution work a single fair unit at a time and
        reassesses current durable truth at every admission boundary (a
        provider slot was released, an admitted unit finished, or
        ``_resolution_opportunity`` reported a durable mutation: a submission,
        an operator requeue, an unpause) -- never a whole request set up
        front. A cycle boundary is an implementation detail, never latency
        policy: work that becomes runnable while the cycle runs is this
        cycle's work. Work already in flight is never preempted or admitted
        twice; ``_resolution_slots`` alone bounds provider I/O, and no lock of
        this scheduler is held while admitted work runs.

        Returns the exact set of transfer ids whose canonical selected-manifest
        commitment changed THIS cycle (empty when none did), so a caller can
        target the existing semantic publication at exactly those transfers
        instead of discovering the change indirectly or publishing everything.
        """
        async with self._resolution_cycle_lock:
            await self._cleanup_pending()
            cycle = self._resolution_cycle = _ResolutionCycle()
            try:
                while cycle.failure is None:
                    cycle.opportunity.clear()
                    while await self._admit_resolution_unit(cycle):
                        pass
                    if cycle.opportunity.is_set():
                        continue
                    if not cycle.units:
                        break
                    await cycle.opportunity.wait()
            finally:
                self._resolution_cycle = None
                await self._drain_resolution_units(cycle)
            if cycle.failure is not None:
                raise cycle.failure
            return frozenset(cycle.changed)

    def _resolution_opportunity(self, *transfer_ids: int) -> None:
        """The one scheduler wake: canonical resolution work may be runnable
        now, so a running cycle reassesses at once.

        A caller names the transfers whose durable state it just changed (a
        submission, an operator requeue, an unpause). The cycle re-enters
        those from current truth at its next admission boundary; it forgets
        only its cached view of them, never what it already admitted
        (``_ResolutionCycle.admitted``). The caller decides nothing else:
        priority, fairness and capacity stay with the scheduler.
        """
        cycle = self._resolution_cycle
        if cycle is not None:
            cycle.reconsider.update(transfer_ids)
            cycle.opportunity.set()

    def _resolution_slot_released(self) -> None:
        """The calling admitted unit no longer claims provider-resolution
        capacity (it left the slot, or never needs one); its remaining
        post-resolution work must not keep that capacity from other work."""
        cycle = self._resolution_cycle
        if cycle is not None:
            cycle.slot_bound.discard(asyncio.current_task())
            cycle.opportunity.set()

    @asynccontextmanager
    async def _resolution_slot(self):
        """The one way provider-resolution I/O takes ``_resolution_slots``."""
        try:
            async with self._resolution_slots:
                yield
        finally:
            self._resolution_slot_released()

    def _resolution_ready(self, record: RequestRecord) -> bool:
        return record.state in _SCHEDULABLE_REQUEST_STATES and record.retry_at <= self.clock()

    async def _has_viable_path(self, transfer_id: int) -> bool:
        """The one bootstrap-critical vs enrichment classification.

        Derived from current canonical artifact/candidate/execution facts on
        every call and never stored: a transfer is ENRICHMENT once any of its
        artifacts is materialized, is held by a live execution attempt, or is
        queued and dispatchable right now through its selected candidate, an
        eligible executor and the canonical materialization authorization.
        Everything else -- no artifact, HOLD/STALE, no eligible executor, a
        recovery wait, an error -- is still BOOTSTRAP-CRITICAL.
        """
        now = self.clock()
        for artifact in await self.repository.artifacts(transfer_id):
            if artifact.state == "completed":
                return True
            if artifact.execution is not None:
                if artifact.state in {"queued", "downloading", "verifying"}:
                    return True
                continue
            if (artifact.state != "queued" or artifact.retry_at > now
                    or not 0 <= artifact.selected < len(artifact.candidates)
                    or not self.registry.claimants(ExecutionSubject.of(artifact.candidates[artifact.selected]))):
                continue
            admission = await self.repository.materialization_authorization(artifact)
            if admission.kind == MaterializationAdmissionKind.PROCEED:
                return True
        return False

    @staticmethod
    def _resolution_incarnation(record: RequestRecord) -> tuple:
        """The durable facts every requeue rewrites; derived, never stored."""
        return record.state, record.attempts, record.retry_at, record.error

    async def _resolution_census(self, cycle: _ResolutionCycle, transfer_id: int, *, served: str | None = None) -> None:
        """Rebuild this transfer's unserved schedulable requests from current
        durable truth -- the only place the cycle learns of a request created
        or requeued after the transfer entered it.

        ``served`` is the request whose admitted unit is finishing: its
        current incarnation is recorded as served. That unit still runs under
        the transfer lock the cycle holds, so an operator requeue -- which
        takes the same lock -- always lands after the record and is seen as
        the new incarnation it is.
        """
        remaining = []
        for record in await self.repository.requests(transfer_id):
            incarnation = self._resolution_incarnation(record)
            if record.id == served:
                cycle.admitted[record.id] = incarnation
            elif record.state in _SCHEDULABLE_REQUEST_STATES and (
                    record.id not in cycle.admitted or cycle.admitted[record.id] not in (None, incarnation)):
                remaining.append(record)
        cycle.remaining[transfer_id] = remaining

    async def _resolution_work(self, cycle: _ResolutionCycle, transfer, capacity: int):
        """This transfer's next admissible unit right now.

        Liveness and the input challenge are read when the transfer enters
        the cycle, and again only when ``_resolution_opportunity`` names it;
        its request census is also retaken whenever its admitted work
        finishes. Readiness is re-evaluated against the clock at every
        boundary, and the admitted request is re-read before it runs.
        """
        if transfer.id in cycle.reconsider:
            cycle.reconsider.discard(transfer.id)
            cycle.retired.discard(transfer.id)
            cycle.remaining.pop(transfer.id, None)
        if transfer.id in cycle.retired:
            return None
        in_flight = cycle.in_flight(transfer.id)
        # One concurrency width may resolve while one more waits behind the
        # transfer's own serialized post-resolution stage; a large multilink
        # never parks one coroutine per request anywhere.
        if in_flight >= 2 * capacity:
            return None
        if not in_flight and self._transfer_locks.setdefault(transfer.id, asyncio.Lock()).locked():
            return None
        if transfer.id not in cycle.remaining:
            if not await self._live(transfer.id, admission=True):
                cycle.retired.add(transfer.id)
                return None
            challenge = await self.challenges.current(transfer.id)
            if challenge:
                # A challenged transfer resolves nothing else this cycle; only
                # a provider- or evidence-origin challenge has a continuation to
                # admit here, and never beside work of the transfer that is
                # still in flight.
                if in_flight:
                    return None
                if (challenge.origin in {InputOrigin.PROVIDER, InputOrigin.EVIDENCE}
                        and challenge.id not in cycle.admitted):
                    return challenge
                cycle.retired.add(transfer.id)
                return None
            await self._resolution_census(cycle, transfer.id)
        for record in cycle.remaining[transfer.id]:
            if self._resolution_ready(record):
                return record
        if not in_flight:
            cycle.retired.add(transfer.id)
        return None

    async def _fair_resolution_choice(self, cycle: _ResolutionCycle, runnable):
        """Explicit priority, then bootstrap-critical before enrichment, then
        the least recently served transfer.

        Bootstrap preference is a turn, not ownership. A bootstrap round's
        membership is fixed when it starts -- the pathless transfers runnable
        at that boundary, from current truth -- and each member is owed one
        turn. A pathless transfer that appears later never extends the round
        in progress; it joins the next one. Once the members are served, one
        enrichment opportunity is granted if any is runnable, and the next
        round starts from current truth (at once, when there is no enrichment:
        no capacity is ever left idle). Enrichment therefore receives a
        bounded opportunity however long pathless transfers keep arriving,
        while a new pathless transfer that meets no populated round is next.
        """
        top = max(transfer.priority for transfer, _work in runnable)
        tier = [item for item in runnable if item[0].priority == top]
        if len(tier) == 1:
            choice = tier[0]
            if not cycle.round:
                # Uncontended work is whatever turn an already-served round
                # still owed; the next contended boundary starts a new round.
                cycle.round = None
        else:
            pathless = {item[0].id for item in tier if not await self._has_viable_path(item[0].id)}
            if cycle.round is None:
                cycle.round = set(pathless)
            pool = [item for item in tier if item[0].id in cycle.round & pathless]
            if not pool:
                pool = [item for item in tier if item[0].id not in pathless]
                cycle.round = None if pool else set(pathless)
                pool = pool or tier
            choice = min(pool, key=lambda item: cycle.served.get(item[0].id, 0))
        if cycle.round is not None:
            cycle.round.discard(choice[0].id)
        return choice

    async def _admit_resolution_unit(self, cycle: _ResolutionCycle) -> bool:
        """Admit at most one unit; False when nothing can be admitted now."""
        capacity = max(1, self.policy.resolution_concurrency)
        if cycle.failure is not None or len(cycle.slot_bound) >= capacity:
            return False
        runnable = []
        for transfer in await self.repository.active():
            work = await self._resolution_work(cycle, transfer, capacity)
            if work is not None:
                runnable.append((transfer, work))
        if not runnable:
            return False
        transfer, work = await self._fair_resolution_choice(cycle, runnable)
        if transfer.id not in cycle.locks:
            lock = self._transfer_locks.setdefault(transfer.id, asyncio.Lock())
            if lock.locked():
                return True
            await lock.acquire()
            cycle.locks[transfer.id] = lock
        if isinstance(work, RequestRecord):
            # The census chose the transfer; the admitted unit itself always
            # runs on the request's current durable state.
            await self._resolution_census(cycle, transfer.id)
            work = next((record for record in cycle.remaining[transfer.id] if record.id == work.id), None)
            if work is None or not self._resolution_ready(work):
                self._release_resolution_transfer(cycle, transfer.id)
                return True
            cycle.remaining[transfer.id].remove(work)
            cycle.admitted[work.id] = None
            unit = self._serve_resolution_request(cycle, work)
        else:
            cycle.retired.add(transfer.id)
            cycle.admitted[work.id] = None
            unit = self._serve_input_continuation(work)
        cycle.served[transfer.id] = max(cycle.served.values(), default=0) + 1
        task = asyncio.create_task(unit)
        cycle.units[task] = transfer.id
        cycle.slot_bound.add(task)
        task.add_done_callback(lambda done: self._resolution_unit_done(cycle, done))
        return True

    async def _serve_resolution_request(self, cycle: _ResolutionCycle, record: RequestRecord):
        """One admitted request unit: admissibility, the work, then the census
        that records it as served and discovers what it made runnable."""
        if not await self._live(record.transfer_id, admission=True):
            # Paused or retired after it entered the cycle. Nothing was
            # served: the request keeps its incarnation, and the transfer is
            # out of this cycle until ``_resolution_opportunity`` names it.
            del cycle.admitted[record.id]
            cycle.retired.add(record.transfer_id)
            return None
        changed = await self._process_request(record)
        await self._resolution_census(cycle, record.transfer_id, served=record.id)
        return changed

    async def _serve_input_continuation(self, challenge: InputChallenge):
        """One admitted provider- or evidence-input continuation. Whatever it
        changed -- the challenge cleared or replaced, requests created, a
        writer admitted -- the scheduler re-enters the transfer from current
        truth through its ordinary entry path: liveness, the CURRENT challenge,
        then the census. A challenge that is still current keeps blocking the
        transfer, and its identity is never continued twice in one cycle."""
        if challenge.origin == InputOrigin.EVIDENCE:
            changed = await self._continue_evidence_input(challenge)
        else:
            changed = await self._continue_provider_input(challenge)
        self._resolution_opportunity(challenge.transfer_id)
        return changed

    @staticmethod
    def _release_resolution_transfer(cycle: _ResolutionCycle, transfer_id: int) -> None:
        """The per-transfer lock is held only while units of it are in flight."""
        if not cycle.in_flight(transfer_id) and transfer_id in cycle.locks:
            cycle.locks.pop(transfer_id).release()

    @staticmethod
    def _resolution_unit_done(cycle: _ResolutionCycle, task: asyncio.Task) -> None:
        transfer_id = cycle.units.pop(task, None)
        if transfer_id is None:
            return
        cycle.slot_bound.discard(task)
        TransferEngine._release_resolution_transfer(cycle, transfer_id)
        if not task.cancelled():
            if task.exception() is not None:
                cycle.failure = cycle.failure or task.exception()
            elif task.result():
                cycle.changed.add(transfer_id)
        cycle.opportunity.set()

    async def _drain_resolution_units(self, cycle: _ResolutionCycle) -> None:
        """A cycle that ends early (failure/cancellation) leaves no unit behind."""
        pending = tuple(cycle.units)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in pending:
            self._resolution_unit_done(cycle, task)
        for transfer_id in tuple(cycle.locks):
            self._release_resolution_transfer(cycle, transfer_id)

    async def _process_request(self, record: RequestRecord):
        if not self._resolution_ready(record) or not await self._live(record.transfer_id, admission=True):
            return
        try:
            if record.state == "pending":
                return await self._resolve(record)
            elif record.state == "waiting":
                async with self._resolution_slot():
                    return await self._observe_resource(record)
            # Neither remaining state performs provider-resolution I/O.
            self._resolution_slot_released()
            if record.state == "materializing":
                candidates = await self.repository.resolved_candidates(record.id)
                if candidates:
                    await self._materialize(record, candidates)
                else:
                    raise TransferError(self._error(Category.RECOVERY_FAILED, Stage.RECONCILIATION, domain=Domain.RECONCILIATION))
            elif record.state == "resolving":
                error = self._error(Category.RECOVERY_FAILED, Stage.RECONCILIATION, domain=Domain.RECONCILIATION)
                await self.repository.request_failure(record.id, error, None)
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(exc, integration_id="", domain=Domain.INTERNAL, stage=Stage.RECONCILIATION)
            await self._request_failure(record, error)

    def capacity_only_blocked_ids(self) -> frozenset[int]:
        """Artifact ids the REAL dispatch path most recently confirmed are
        blocked ONLY by execution capacity (Section 9). Reset every
        reconcile cycle, so this reflects at most one scheduler tick of
        staleness -- an artifact not dispatch-attempted this cycle (paused,
        an input challenge, retry not yet elapsed, or simply not yet
        reached) is correctly absent rather than optimistically carried
        forward."""
        return frozenset(self._capacity_only_blocked)

    async def reconcile_executions(self):
        """Reconcile cleanup obligations, then active execution attempts."""
        async with self._execution_cycle_lock:
            self._capacity_only_blocked = set()
            await self._cleanup_executions_pending()
            transfers = await self.repository.active()
            artifacts_by_transfer = {transfer.id: await self.repository.artifacts(transfer.id) for transfer in transfers}
            challenges = {transfer.id: await self.challenges.current(transfer.id) for transfer in transfers}
            grouped = {}
            for transfer in transfers:
                for artifact in artifacts_by_transfer[transfer.id]:
                    if artifact.execution and artifact.state in {"queued", "downloading", "unknown", "verifying", "paused"}:
                        grouped.setdefault(artifact.execution.executor_id, []).append(artifact.execution)
            observations = {}
            reservation_facts = {}
            throughput_contributions = {}
            for executor_id, handles in grouped.items():
                # Batched observation of work that already exists: resolved
                # through the bound-execution seam, never the claim router.
                executor = self.registry.executor_for_handle(handles[0])
                if executor is None:
                    continue
                snapshot = await self._observe_batch(executor, tuple(handles))
                certain = snapshot.error is None
                for handle, observation in zip(handles, snapshot.observations):
                    try:
                        observation = await self._accept_observation(handle, observation)
                    except TransferError as exc:
                        observation = ExecutionObservation(handle, ExecutionState.UNKNOWN, error=exc.error)
                    observations[handle.attempt_id] = observation
                    certain = certain and observation.error is None and observation.state != ExecutionState.UNKNOWN
                observed = [observations[handle.attempt_id] for handle in handles]
                reservation_facts[executor_id] = (certain, any(
                    item.resumable and item.activity.bandwidth_reservation_required
                    for item in observed))
                throughput_contributions[executor_id] = await self._executor_throughput(executor, observed)
            await self.runtime.observe(reservation_facts)
            # Rebuilt every cycle from the executors that actually hold live
            # handles: an executor with none contributes nothing at all, so a
            # finished or paused acquisition cannot leave a live rate behind.
            self.throughput.record(throughput_contributions)
            for transfer in transfers:
                challenge = challenges[transfer.id]
                await self._process_executions(transfer.id, artifacts_by_transfer[transfer.id], observations,
                                               dispatch_allowed=challenge is None)
                if challenge and challenge.origin == InputOrigin.EXECUTOR and await self._live(transfer.id, admission=True):
                    await self._continue_executor_input(challenge, await self.repository.artifacts(transfer.id))
            await self._release_runtime_reservations()

    @staticmethod
    async def _executor_throughput(executor, observations) -> int:
        """One executor's contribution to the aggregate download throughput.

        Exactly one path per executor, which is what makes counting the same
        bytes twice impossible: an executor that can only measure ITSELF
        reports that single figure (counted once, whatever its job count) and
        none of its per-execution rates is added; any other executor
        contributes the sum of the rates its network-active executions report.
        An error, a wrong shape or an unobserved answer contributes nothing --
        never a previous value.
        """
        if getattr(executor.capabilities, "aggregate_throughput", False):
            try:
                reported = await executor.aggregate_download_throughput()
            except Exception:
                return 0
            if not isinstance(reported, ExecutorThroughput) or not reported.observed:
                return 0
            return max(0, int(reported.bytes_per_second or 0))
        return sum(max(0, int(item.progress.bytes_per_second or 0))
                   for item in observations if item.activity.network_active)

    async def _release_runtime_reservations(self):
        """Positive durable truth that an executor holds no live native work
        releases its bandwidth reservation; remaining shares may then rise."""
        async with self._dispatch_lock:
            await self.runtime.release_absent(await self.repository.executors_with_live_work())

    async def _observe_batch(self, executor, handles: tuple[ExecutionHandle, ...]) -> ExecutionSnapshot:
        """The one executor observation call: one neutral batch per executor.

        Returns exactly one observation per requested handle, in order. A
        native/batch failure never becomes an empty or absent snapshot: every
        affected handle is UNKNOWN with the snapshot's error."""
        try:
            snapshot = await executor.observe_many(tuple(handles))
            if not isinstance(snapshot, ExecutionSnapshot):
                raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RECONCILIATION))
            if snapshot.error:
                return ExecutionSnapshot(tuple(ExecutionObservation(handle, ExecutionState.UNKNOWN, error=snapshot.error)
                                               for handle in handles), snapshot.error)
            by_attempt = {}
            for observation in snapshot.observations:
                if not isinstance(observation, ExecutionObservation) or observation.handle.attempt_id in by_attempt:
                    raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RECONCILIATION))
                by_attempt[observation.handle.attempt_id] = observation
            if set(by_attempt) != {handle.attempt_id for handle in handles}:
                raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RECONCILIATION))
            return ExecutionSnapshot(tuple(by_attempt[handle.attempt_id] for handle in handles))
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                exc, integration_id=executor.descriptor.id, domain=Domain.EXECUTOR, stage=Stage.RECONCILIATION)
            return ExecutionSnapshot(tuple(ExecutionObservation(handle, ExecutionState.UNKNOWN, error=error)
                                           for handle in handles), error)

    async def _observe_execution(self, executor, handle: ExecutionHandle) -> ExecutionObservation:
        """Observe one execution through the batch contract and accept it."""
        observed = (await self._observe_batch(executor, (handle,))).observations[0]
        return await self._accept_observation(handle, observed)

    async def _accept_observation(self, handle: ExecutionHandle, observed) -> ExecutionObservation:
        """THE acceptance of every executor observation for ``handle``.

        The observation must name the same executor and DP attempt. Its
        handle is either the persisted one, or that handle's one legal native
        binding (``None`` -> value), which is durably bound here BEFORE the
        observation can be persisted. Every other handle mutation is refused."""
        if not isinstance(observed, ExecutionObservation) or not isinstance(observed.handle, ExecutionHandle):
            raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RECONCILIATION))
        if not isinstance(observed.state, ExecutionState) or not isinstance(observed.activity, ExecutionActivity) \
                or not isinstance(observed.controls, frozenset) \
                or any(not isinstance(item, ExecutionControl) for item in observed.controls):
            raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RECONCILIATION))
        if observed.handle == handle:
            return observed
        if handle.binds(observed.handle) and await self.repository.bind_execution_handle(handle, observed.handle):
            return observed
        raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RECONCILIATION))

    @staticmethod
    def _controls(executor, observed: ExecutionObservation) -> frozenset:
        """Controls usable now: advertised by the current observation AND
        backed by the executor's static per-execution control capability."""
        return observed.controls if executor.capabilities.per_execution_pause else frozenset()

    async def _cancel_execution(self, executor, handle: ExecutionHandle) -> ExecutionObservation:
        """Ask the executor to stop native work and accept the observed truth.

        The result proves a stop only when it is ``stopped``; an unconfirmed
        or lost acknowledgement stays uncertain (UNKNOWN) and keeps its
        reservation, cleanup authority and material ownership."""
        try:
            observed = await executor.cancel(handle)
        except Exception as exc:
            observed = ExecutionObservation(handle, ExecutionState.UNKNOWN,
                                            error=self._executor_cleanup_exception(executor.descriptor.id, exc))
        return await self._accept_observation(handle, observed)

    @staticmethod
    def _cancellation_outcome(observed: ExecutionObservation, initiator=CancellationInitiator.USER) -> TransferOutcome:
        if observed.stopped:
            return TransferOutcome(OutcomeKind.CANCELLED, cancellation_initiator=initiator)
        return TransferOutcome(OutcomeKind.FAILURE, observed.error or NormalizedError(
            Domain.RECONCILIATION, Category.RECONCILIATION_FAILED, Stage.CLEANUP, retryability=Retryability.BACKOFF))

    def _work(self, artifact: Artifact, candidate: TransferCandidate,
              attempt_id: str | None = None) -> ExecutionWork:
        """Core output policy for one artifact's selected subject.

        The artifact's durable attempt is carried when it has one, so an
        executor can name attempt-scoped native transient material.
        """
        if attempt_id is None and artifact.execution is not None:
            attempt_id = artifact.execution.attempt_id
        return ExecutionWork(ExecutionSubject.of(candidate),
                             materialization_plan(self.root, artifact.target, candidate.materialization),
                             attempt_id)

    def _footprint(self, executor, work: ExecutionWork) -> ExecutionFootprint:
        footprint = executor.footprint(work)
        if not isinstance(footprint, ExecutionFootprint):
            raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.QUEUE))
        validate_plan(self.root, work.materialization, footprint)
        return footprint

    def _artifact_work(self, artifact: Artifact, executor=None):
        """(executor, work, footprint) for an artifact's current selection; the
        executor is the attempt's own when one exists, else the core-selected
        claimant."""
        candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
        if candidate is None:
            return executor, None, None
        if executor is None:
            executor = (self.registry.executor_for_handle(artifact.execution) if artifact.execution
                        else self.registry.executor_for_subject(ExecutionSubject.of(candidate)))
        if executor is None:
            return None, None, None
        work = self._work(artifact, candidate)
        return executor, work, self._footprint(executor, work)

    async def _current_artifact(self, transfer_id: int, artifact_id: int):
        return next((item for item in await self.repository.artifacts(transfer_id) if item.id == artifact_id), None)

    def _convergence_lock(self, attempt_id: str):
        return self._execution_convergence_locks.setdefault(attempt_id, asyncio.Lock())

    @staticmethod
    def _control_error(exc, executor_id: str):
        if isinstance(exc, TransferError):
            error = exc.error
            if error.category != Category.UNMAPPED_EXECUTOR_ERROR:
                return error
            diagnostic = error.diagnostic
        else:
            native = unknown_failure(exc, integration_id=executor_id, domain=Domain.EXECUTOR, stage=Stage.RECONCILIATION)
            if native.category != Category.UNMAPPED_EXECUTOR_ERROR:
                return native
            diagnostic = native.diagnostic
        return NormalizedError(
            Domain.RECONCILIATION, Category.RECONCILIATION_FAILED, Stage.RECONCILIATION,
            retryability=Retryability.BACKOFF,
            operator_action_required=False, integration_id=executor_id, diagnostic=diagnostic,
        )

    async def _converge_execution(self, artifact: Artifact, executor, observed: ExecutionObservation | None = None,
                                  *, persist_passive=True):
        """Single native control owner for one durable execution attempt.

        Callers may arrive with stale observations. Any observation that would
        trigger a native mutation is revalidated while holding the per-execution
        lock, so explicit controls and scheduler reconciliation cannot both emit
        the same pause/unpause. A control is invoked only while the CURRENT
        observation advertises it (and the executor statically supports
        per-execution control); an unavailable control is never guessed at --
        the execution stays observed and owned until a later observation offers
        it. Durable pause intent is reread after every native action, allowing a
        newer opposite intent to win before ownership is released. Scheduler
        callers can leave passive observation persistence to activity accounting
        so byte progress is measured before it is stored.
        """
        if artifact.execution is None:
            return observed
        handle = artifact.execution
        controllable = executor.capabilities.per_execution_pause
        async with self._convergence_lock(handle.attempt_id):
            current = await self._current_artifact(artifact.transfer_id, artifact.id)
            if current is None or current.execution is None or current.execution.attempt_id != handle.attempt_id:
                return observed or ExecutionObservation(handle, ExecutionState.UNKNOWN, error=self._error(
                    Category.OWNERSHIP_CONFLICT, Stage.RECONCILIATION, domain=Domain.LIFECYCLE,
                    retryability=Retryability.NEVER,
                ))
            handle = current.execution
            try:
                transfer = await self.repository.get(artifact.transfer_id)
                desired_paused = bool(transfer and transfer.paused) or await self.repository.globally_paused()
                mutation_implied = observed is None or (controllable and (
                    (desired_paused and observed.state in {ExecutionState.QUEUED, ExecutionState.RUNNING})
                    or (not desired_paused and observed.state == ExecutionState.PAUSED)))
                if mutation_implied:
                    observed = await self._observe_execution(executor, handle)
                else:
                    observed = await self._accept_observation(handle, observed)
                handle = observed.handle

                for _ in range(4):
                    current = await self._current_artifact(artifact.transfer_id, artifact.id)
                    if current is None or current.execution is None or current.execution.attempt_id != handle.attempt_id:
                        return observed
                    transfer = await self.repository.get(artifact.transfer_id)
                    if transfer is None or transfer.state in {TransferState.DELETED, TransferState.COMPLETED, TransferState.CONSOLIDATED, TransferState.CANCELLED}:
                        return observed
                    desired_paused = transfer.paused or await self.repository.globally_paused()
                    controls = self._controls(executor, observed)

                    if observed.state in {ExecutionState.UNKNOWN, ExecutionState.FAILED, ExecutionState.ABSENT,
                                          ExecutionState.CANCELLED, ExecutionState.SUCCEEDED}:
                        if persist_passive:
                            await self.repository.execution(observed)
                        return observed

                    if desired_paused:
                        if (observed.state in {ExecutionState.QUEUED, ExecutionState.RUNNING}
                                and ExecutionControl.PAUSE in controls):
                            observed = await self._accept_observation(handle, await executor.pause(handle))
                            await self.repository.execution(observed)
                            continue
                        if persist_passive:
                            await self.repository.execution(observed)
                        return observed

                    if observed.state == ExecutionState.PAUSED and ExecutionControl.RESUME in controls:
                        async with self._dispatch_lock:
                            current = await self._current_artifact(artifact.transfer_id, artifact.id)
                            transfer = await self.repository.get(artifact.transfer_id)
                            if (current is None or current.execution is None
                                    or current.execution.attempt_id != handle.attempt_id
                                    or transfer is None or transfer.paused or await self.repository.globally_paused()
                                    or not self.dispatch_permitted):
                                continue
                            # Universal execution-admission invariant
                            # (Workstream A, specification section 7.5): an
                            # existing PAUSED execution handle is not proof of
                            # authorization. Resuming it is a native side
                            # effect; it must stop for the same HOLD/STALE
                            # authority a fresh dispatch would. HOLD is
                            # ordinary waiting state -- stay paused, no
                            # mutation, no error, no retry consumption.
                            admission = await self.repository.materialization_authorization(current)
                            if admission.kind != MaterializationAdmissionKind.PROCEED:
                                if persist_passive:
                                    await self.repository.execution(observed)
                                return observed
                            occupied = await self.repository.occupied_execution_slots(
                                self.clock(), exclude_artifact_id=artifact.id,
                            )
                            if (occupied >= max(1, self.policy.max_active_executions)
                                    or not await self.runtime.admit(executor)):
                                if persist_passive:
                                    await self.repository.execution(observed)
                                return observed
                            await self.repository.execution(ExecutionObservation(
                                handle, ExecutionState.QUEUED, observed.progress, activity=observed.activity,
                            ))
                        observed = await self._accept_observation(handle, await executor.resume(handle))
                        await self.repository.execution(observed)
                        continue

                    if persist_passive:
                        await self.repository.execution(observed)
                    return observed

                return ExecutionObservation(handle, ExecutionState.UNKNOWN, observed.progress, NormalizedError(
                    Domain.RECONCILIATION, Category.RECONCILIATION_FAILED, Stage.RECONCILIATION,
                    retryability=Retryability.BACKOFF,
                    operator_action_required=False, integration_id=executor.descriptor.id,
                ))
            except Exception as exc:
                return ExecutionObservation(handle, ExecutionState.UNKNOWN,
                    error=self._control_error(exc, executor.descriptor.id))

    async def _process_executions(self, transfer_id, artifacts, observations, *, dispatch_allowed=True):
        for artifact in artifacts:
            if not await self._live(transfer_id):
                break
            try:
                if artifact.execution and artifact.state in {"queued", "downloading", "unknown", "verifying", "paused"}:
                    executor = self.registry.executor_for_handle(artifact.execution)
                    if executor is None:
                        error = self._error(Category.UNSUPPORTED_CAPABILITY, Stage.RECONCILIATION, domain=Domain.REQUEST, retryability=Retryability.NEVER)
                        await self.repository.artifact_state(artifact.id, "error", error=error)
                        continue
                    observed = observations.get(artifact.execution.attempt_id)
                    if observed is None:
                        observed = await self._observe_execution(executor, artifact.execution)
                    else:
                        observed = await self._accept_observation(artifact.execution, observed)
                    artifact = replace(artifact, execution=observed.handle)
                    observed = await self._converge_execution(artifact, executor, observed, persist_passive=False)
                    await self._execution_result(artifact, executor, observed)
                elif dispatch_allowed and await self._live(transfer_id, admission=True) and artifact.state == "queued" and artifact.retry_at <= self.clock():
                    await self._dispatch(artifact)
                elif dispatch_allowed and await self._live(transfer_id, admission=True) and artifact.state == "refresh_pending" and artifact.retry_at <= self.clock():
                    await self._refresh(artifact)
            except Exception as exc:
                error = exc.error if isinstance(exc, TransferError) else unknown_failure(exc,
                    integration_id=artifact.execution.executor_id if artifact.execution else "",
                    domain=Domain.RECONCILIATION, stage=Stage.RECONCILIATION)
                await self.repository.artifact_state(artifact.id, "error", error=error)
                await self.repository.outcome(transfer_id, TransferOutcome(OutcomeKind.FAILURE, error))
        await self._aggregate(transfer_id)

    async def _request_failure(self, record: RequestRecord, error: NormalizedError, *, attempts=None, waiting=False):
        count = record.attempts + int(waiting) if attempts is None else attempts
        decision = self.policy.retry_resolution(error, count, self.clock())
        retry_state = "waiting" if waiting and decision.action != Recovery.RERESOLVE else "pending"
        await self.repository.request_failure(record.id, error, decision.retry_at, retry_state=retry_state, consume_attempt=waiting)
        await self.repository.outcome(record.transfer_id, TransferOutcome(OutcomeKind.FAILURE, error))

    async def _resolve(self, record: RequestRecord):
        raise NotImplementedError("_resolve is implemented by transfers.engine.TransferEngine")

    async def _apply_resolution(self, record: RequestRecord, attempt: ResolutionAttempt, provider, result: ResolutionResult,
                                *, challenge: InputChallenge | None = None):
        result = self._authoritative_provider_result(provider.descriptor.id, result, request_kind=record.request.kind)
        if result.input_required:
            if result.error or result.candidates or result.observation or not isinstance(result.input_required, InputRequirement):
                raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RESOLUTION))
            if not isinstance(provider, ProviderInputContinuation):
                raise TransferError(self._error(Category.UNSUPPORTED_CAPABILITY, Stage.RESOLUTION, domain=Domain.REQUEST,
                                                retryability=Retryability.NEVER))
            if challenge:
                await self.challenges.replace(challenge, result.input_required)
            else:
                await self.challenges.wait_provider(attempt, result.input_required, provider.descriptor.id)
            return
        live = await self.repository.resolution(attempt, result)
        if challenge:
            await self.challenges.clear(challenge)
            await self.inputs.clear(challenge.id)
        if not live:
            if await self.repository.delete_remote_requested(record.transfer_id):
                await self._cleanup_resources(record.transfer_id, explicit=True)
            return
        await self._secure_root_selection(record, provider, result.observation)
        if result.error:
            await self._request_failure(record, result.error, attempts=record.attempts + 1)
        elif result.candidates:
            await self._materialize(record, result.candidates)
        elif result.observation:
            await self._converge_root_observation_name(record, result.observation)
            if result.observation.state == ResourceState.AVAILABLE:
                return await self._observe_resource(replace(record, resource=result.observation.resource, state="waiting", attempts=record.attempts + 1))
        else:
            raise TransferError(self._error(Category.NO_TRANSFER_CANDIDATE, Stage.RESOLUTION, domain=Domain.RESOLUTION))

    @staticmethod
    def _file_manifest_root(record: RequestRecord, provider) -> bool:
        """A root request routed to a provider DP trusts for a neutral file
        manifest. This is the capability boundary only -- it says nothing about
        whether the interactive lifecycle is engaged; that policy has exactly one
        owner (``TransferRepository.ensure_selection_generation``)."""
        return (
            record.parent_id is None
            and Capability.FILE_MANIFEST in provider.descriptor.capabilities
        )

    async def _converge_root_observation_name(self, record: RequestRecord,
                                              observation: ProviderObservation | None) -> None:
        """THE acceptance of a bound provider's authoritative name for a ROOT request.

        A provider resolves the name of what it is preparing on its own
        schedule, so the fact can arrive with the first resolution, with any
        later observation while the resource is still preparing, once it is
        available, or on the reconciliation that follows a restart. Every one
        of those paths funnels through here, which is what makes ``torrents.name``
        converge on whichever observation actually carries the fact instead of
        depending on when it happened to arrive; it remains the one canonical
        root name, with no second store and no presentation-side repair.

        Two facts are deliberately not names: a member observation describes
        only itself and never renames the root it belongs to, and an empty
        name is the absence of a fact, which never overwrites what is stored.
        Provider-native placeholders for "not resolved yet" are normalized to
        that empty fact at their own provider's translation boundary.
        """
        if observation is None or record.parent_id is not None or not observation.name:
            return
        await self.repository.rename(record.transfer_id, safe_name(observation.name))

    async def _secure_root_selection(self, record: RequestRecord, provider, observation: ProviderObservation | None,
                                     *, resource=None):
        """THE post-binding lifecycle owner for a manifest-capable root: guarantee
        that a root which needs interactive file selection has its own current
        selection generation before anything can expand its manifest.

        Every path that binds a provider resource to a root funnels through this
        one method -- normal resolution (``_apply_resolution``), inventory
        adoption (``reconcile_inventory``) and, as the fail-closed materialization
        guard, every observation that could fan children out
        (``TransferEngine._observe_resource``: it is where restart reconciliation,
        provider recovery, failover, reuse and any future binding path all
        converge). The policy itself lives in the repository; this method only
        adds the capability boundary and never reads ``selection_mode``.
        """
        if observation is None or not self._file_manifest_root(record, provider):
            return SelectionAuthority()
        resource = resource or observation.resource
        if resource is None:
            return SelectionAuthority()
        return await self.repository.ensure_selection_generation(
            record, provider.descriptor.id, resource,
            available=(observation.state == ResourceState.AVAILABLE),
            file_manifest=observation.file_manifest, now=self.clock(),
        )

    async def _continue_provider_input(self, challenge: InputChallenge):
        if not await self.inputs.has(challenge) or not await self._live(challenge.transfer_id, admission=True):
            return
        records = await self.repository.requests(challenge.transfer_id)
        record = next((item for item in records if item.id == challenge.request_id), None)
        if record is None or record.state != "input_required":
            await self.challenges.clear(challenge)
            await self.inputs.clear(challenge.id)
            return
        submitted = None
        bound_provider_id = await self.repository.bound_route_provider(record.id)
        try:
            if not bound_provider_id or bound_provider_id != challenge.integration_id:
                raise TransferError(self._error(
                    Category.OWNERSHIP_CONFLICT, Stage.RESOLUTION, domain=Domain.LIFECYCLE,
                    retryability=Retryability.NEVER,
                ))
            provider = self.registry.provider_for_bound_continuation(bound_provider_id, record.request)
            if not isinstance(provider, ProviderInputContinuation):
                raise TransferError(self._error(
                    Category.UNSUPPORTED_CAPABILITY, Stage.RESOLUTION, domain=Domain.REQUEST,
                    retryability=Retryability.NEVER,
                ))
            async with self._resolution_slot():
                if not await self._live(challenge.transfer_id, admission=True):
                    return
                submitted = await self.inputs.take(challenge)
                if submitted is None:
                    return
                result = await provider.resolve_with_input(record.request, submitted)
            attempt = ResolutionAttempt(challenge.operation_id, record.id, bound_provider_id, "input_required")
            return await self._apply_resolution(record, attempt, provider, result, challenge=challenge)
        except Exception as exc:
            secrets = submitted.secret_values() if submitted else ()
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                exc, integration_id=bound_provider_id or challenge.integration_id, domain=Domain.PROVIDER, stage=Stage.RESOLUTION, secrets=secrets)
            attempt = ResolutionAttempt(challenge.operation_id, record.id, bound_provider_id or challenge.integration_id, "input_required")
            await self.repository.resolution(attempt, ResolutionResult(ResourceState.UNKNOWN, error=error))
            await self.challenges.clear(challenge)
            await self._request_failure(record, error, attempts=record.attempts + 1)
        finally:
            if submitted:
                submitted.discard()

    async def _evidence_target(self, challenge: InputChallenge):
        """The still-current subject of an evidence challenge, or ``None``.

        Current means: the challenged request is still deciding its
        materialization (``InputChallengeStore.current``), the challenged
        candidate identity is still in that request's resolved candidate set
        (a newer resolution generation carries new identities), and the
        challenged integration is still the sampling capability selected for
        that candidate. Nothing else may authorize the submitted input."""
        record = next((item for item in await self.repository.requests(challenge.transfer_id)
                       if item.id == challenge.request_id), None)
        if record is None or record.state != "materializing":
            return None
        candidates = await self.repository.resolved_candidates(record.id)
        candidate = next((item for item in candidates if str(item.id) == challenge.operation_id), None)
        if candidate is None:
            return None
        # Fenced to the EXACT executor identity that raised the requirement:
        # if the core-selected claimant for this subject changed, the challenge
        # is stale and ordinary routing re-enters -- the submitted input never
        # moves to another executor.
        try:
            executor = self.registry.executor_for_subject(ExecutionSubject.of(candidate))
        except TransferError:
            return None
        capabilities = executor.capabilities
        if (executor.descriptor.id != challenge.integration_id or not capabilities.candidate_sampling
                or not capabilities.transient_input):
            return None
        return record, candidates, candidate, executor

    async def _evidence_input_required(self, record: RequestRecord, candidate: TransferCandidate,
                                       integration_id: str, requirement: InputRequirement) -> InputChallenge | None:
        """Durably ask for evidence input for one of ``record``'s own candidates.

        An answered challenge whose continuation still needs input for the
        same candidate is replaced (next generation, fenced on the exact
        current row); anything else starts a new challenge. A request that
        stopped being challengeable meanwhile simply stays held: the next
        ordinary decision re-evaluates it from current truth."""
        current = await self.challenges.current(record.transfer_id)
        try:
            if (current is not None and current.origin == InputOrigin.EVIDENCE and current.request_id == record.id
                    and current.operation_id == str(candidate.id) and current.integration_id == integration_id):
                return await self.challenges.replace(current, requirement)
            return await self.challenges.wait_evidence(record.transfer_id, record.id, str(candidate.id),
                                                       integration_id, requirement)
        except InputSubmissionRejected:
            return None

    async def _continue_evidence_input(self, challenge: InputChallenge):
        """Continue the SAME evidence acquisition with the submitted input.

        The input is lent to one ordinary materialization decision
        (``EvidenceContext``) for exactly the challenged candidate, so
        equivalence decides before any writer exists. When that decision
        admits a writer for exactly this candidate, the proven input is handed
        to its execution once through the one broker; otherwise it is
        discarded here."""
        if not await self.inputs.has(challenge) or not await self._live(challenge.transfer_id, admission=True):
            return
        target = await self._evidence_target(challenge)
        if target is None:
            await self.challenges.clear(challenge)
            await self.inputs.clear(challenge.id)
            return
        record, candidates, candidate, executor = target
        # Evidence acquisition is not provider-resolution I/O.
        self._resolution_slot_released()
        submitted = await self.inputs.take(challenge)
        if submitted is None:
            return
        try:
            context = EvidenceContext(inputs={str(candidate.id): submitted})
            await self._materialize(record, candidates, evidence=context)
            current = await self.challenges.current(challenge.transfer_id)
            if current is not None and current.id == challenge.id:
                await self.challenges.clear(challenge)
            proven = context.proven_evidence(candidate.id)
            if proven is not None:
                # The evidence (never the input) outlives this decision, so a
                # later mirror can compare against this canonical member after
                # the transient input is gone -- including across restarts.
                await self.canonical.retain_evidence(str(candidate.id), proven)
                artifact = next((item for item in await self.repository.artifacts(record.transfer_id)
                                 if item.request_id == record.id), None)
                if (artifact is not None and artifact.execution is None and artifact.candidates
                        and str(artifact.candidates[artifact.selected].id) == str(candidate.id)):
                    await self.inputs.hand_off(record.transfer_id, record.id, str(candidate.id),
                                               executor.descriptor.id, submitted)
                    submitted = None
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                exc, integration_id=challenge.integration_id, domain=Domain.EXECUTOR, stage=Stage.CANDIDATE_PREPARATION,
                secrets=submitted.secret_values() if submitted else ())
            await self.challenges.clear(challenge)
            await self._request_failure(record, error)
        finally:
            if submitted:
                submitted.discard()

    async def _observe_resource(self, record: RequestRecord):
        raise NotImplementedError("_observe_resource is implemented by transfers.engine.TransferEngine")

    async def _materialize(self, record: RequestRecord, candidates, *, evidence: EvidenceContext | None = None):
        # Structural validity only: whether some executor can act on a
        # candidate is a claim over its subject, decided at dispatch.
        if any(not isinstance(candidate, TransferCandidate) or candidate.expected_bytes < 0 for candidate in candidates):
            raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.CANDIDATE_PREPARATION))
        candidates = tuple(sorted(candidates, key=lambda candidate: -candidate.priority))
        if record.entry:
            candidates = tuple(replace(candidate, name=record.entry.name, relative_path=record.entry.relative_path,
                                       expected_bytes=candidate.expected_bytes or record.entry.expected_bytes) for candidate in candidates)
        existing = next((item for item in await self.repository.artifacts(record.transfer_id) if item.request_id == record.id), None)
        if existing:
            await self.repository.materialize(record, candidates, existing.target)
            return
        transfer = await self.repository.get(record.transfer_id)
        if transfer is None:
            return
        relative = candidates[0].relative_path or candidates[0].name
        if record.parent_id:
            relative = str(Path(safe_name(transfer.name)) / relative)

        async def equivalent_size(other_candidates):
            for left in other_candidates:
                for right in candidates:
                    size = await shared_size(left, right, self.registry, evidence)
                    if size is not None:
                        return size
            return None

        def canonical_key(item):
            return item.id, tuple(str(candidate.id) for candidate in item.candidates)

        def contender_key(item):
            contender, contender_candidates, _order = item
            return contender.id, tuple(str(candidate.id) for candidate in contender_candidates)

        while await self._live(record.transfer_id):
            existing = next((item for item in await self.repository.artifacts(record.transfer_id) if item.request_id == record.id), None)
            if existing:
                return

            canonicals = tuple(
                item for item in await self.canonical.canonical_artifacts()
                if item.request_id != record.id and item.candidates
            )
            canonical_keys = {canonical_key(item) for item in canonicals}
            for primary in canonicals:
                size = await equivalent_size(primary.candidates)
                if size is None:
                    continue
                if await self.canonical.attach(primary, record, candidates, size):
                    return
                if not await self._live(record.transfer_id):
                    return
                existing = next((item for item in await self.repository.artifacts(record.transfer_id) if item.request_id == record.id), None)
                if existing:
                    return

            contenders = await self.canonical.lower_materializing(record)
            contender_keys = {contender_key(item) for item in contenders}
            restart = False
            for contender, contender_candidates, _order in contenders:
                size = await equivalent_size(contender_candidates)
                if size is None:
                    continue
                for _ in range(8):
                    await asyncio.sleep(0)
                    winner = next((item for item in await self.canonical.canonical_artifacts()
                                   if item.request_id == contender.id and item.candidates), None)
                    if winner is not None:
                        winner_size = await equivalent_size(winner.candidates)
                        if winner_size is not None and await self.canonical.attach(winner, record, candidates, winner_size):
                            return
                        if not await self._live(record.transfer_id):
                            return
                        restart = True
                        break
                    lower = await self.canonical.lower_materializing(record)
                    if not any(item[0].id == contender.id for item in lower):
                        restart = True
                        break
                if restart:
                    break
                return
            if restart:
                continue

            retry_snapshot = False
            async with self._paths_lock:
                fresh_canonicals = tuple(
                    item for item in await self.canonical.canonical_artifacts()
                    if item.request_id != record.id and item.candidates
                )
                fresh_contenders = await self.canonical.lower_materializing(record)
                fresh_canonical_keys = {canonical_key(item) for item in fresh_canonicals}
                fresh_contender_keys = {contender_key(item) for item in fresh_contenders}
                if (fresh_canonical_keys - canonical_keys) or (fresh_contender_keys - contender_keys):
                    retry_snapshot = True
                else:
                    target = destination(self.root, relative)
                    occupied = await self.repository.occupied_paths()
                    if record.parent_id and str(target).casefold() in occupied:
                        raise TransferError(self._error(Category.LOCAL_PATH_CONFLICT, Stage.CANDIDATE_PREPARATION,
                            domain=Domain.LOCAL_RESOURCE, retryability=Retryability.AFTER_RESOURCE_CHANGE))
                    if not record.parent_id:
                        original, index = target, 2
                        while target.exists() or target.is_symlink() or str(target).casefold() in occupied:
                            target = original.with_name(f"{original.stem} ({index}){original.suffix}")
                            index += 1
                    await self.repository.materialize(record, candidates, str(target))
                    return
            if retry_snapshot:
                continue

    async def _retire_stale_materialization(self, artifact: Artifact) -> None:
        """Retire executable work superseded by a newer materialization
        authority (STALE admission, specification section 7.5) through the
        SAME re-resolution machinery already used whenever an artifact's
        candidate is no longer valid (``_plan_after_reconcile``'s
        "no current candidates" branch) -- never a selection-specific side
        path. The affected request alone is requeued; ordinary reconciliation
        reconstructs current authorized work from there.
        """
        await self.repository.artifact_state(artifact.id, "unresolved", release=True)
        await self.repository.retry_requests(artifact.transfer_id, request_id=artifact.request_id)

    async def _dispatch(self, artifact: Artifact, *, retry_from: ExecutionHandle | None = None):
        try:
            # Universal execution-admission invariant (DP 1.0.12 canonical
            # architecture correction, Workstream A): stops before ANY
            # executor side effect -- validate_target, prepare(), native
            # allocation, capacity accounting, retry consumption, candidate
            # expiry/refresh -- whenever the artifact's owning request is not
            # durably authorized for the CURRENT materialization generation.
            # HOLD is ordinary waiting state: no mutation, no error; the
            # existing scheduler/reconciliation cadence alone retries once
            # commitment becomes durable.
            admission = await self.repository.materialization_authorization(artifact)
            if admission.kind == MaterializationAdmissionKind.HOLD:
                return
            if admission.kind == MaterializationAdmissionKind.STALE:
                await self._retire_stale_materialization(artifact)
                return
            candidate = artifact.candidates[artifact.selected]
            executor = self.registry.executor_for_subject(ExecutionSubject.of(candidate))
            # The attempt identity is allocated FIRST so the work, its
            # footprint, the request, prepare() and the durable prepared
            # execution are all the same attempt. An executor whose native
            # transient material is attempt-scoped can therefore name it from
            # the very first evaluation, and the admission/materialization
            # checks below see those real paths rather than an empty set.
            attempt_id = artifact.execution.attempt_id if artifact.execution else new_identity()
            work = self._work(artifact, candidate, attempt_id)
            footprint = self._footprint(executor, work)
            if await adoptable_material(work.materialization, footprint, artifact.expected_bytes, candidate.integrity,
                                        delay=self.policy.adoption_stability_seconds):
                await self.repository.artifact_state(artifact.id, "completed")
                return
            if candidate.expires_at is not None and candidate.expires_at <= self.clock():
                error = self._error(Category.CANDIDATE_EXPIRED, Stage.CANDIDATE_PREPARATION, domain=Domain.RESOLUTION,
                    retryability=Retryability.AFTER_RERESOLUTION)
                await self._schedule_refresh(artifact, error)
                return
            request = ExecutionRequest(work, attempt_id)
            prepared = executor.prepare(request)
            if isinstance(prepared, InputRequirement):
                if not executor.capabilities.transient_input:
                    raise TransferError(self._error(Category.UNSUPPORTED_CAPABILITY, Stage.QUEUE, domain=Domain.REQUEST, retryability=Retryability.NEVER))
                await self.challenges.wait_executor(artifact, executor.descriptor.id, request.attempt_id, prepared)
                return
            self._require_prepared(prepared, executor.descriptor.id, request.attempt_id)
            handle = prepared
            native_retry = retry_from is not None and await self._native_retry_available(executor, retry_from)
            submitted = None
            async with self._dispatch_lock:
                # Close the TOCTOU window (specification section 7.4):
                # revalidate the same authority immediately before the
                # irreversible native commitment below. A HOLD/STALE
                # transition after the check above is caught here; retirement
                # itself happens on the next ordinary dispatch attempt rather
                # than while holding this lock.
                revalidation = await self.repository.materialization_authorization(artifact)
                if revalidation.kind != MaterializationAdmissionKind.PROCEED:
                    return
                if not self.dispatch_permitted or not await self._live(artifact.transfer_id, admission=True):
                    return
                # Section 13: exclude this artifact's own (if any) continuation
                # reservation from the count -- it is the artifact entitled to
                # consume it, not an unrelated competitor for it.
                occupied = await self.repository.occupied_execution_slots(
                    self.clock(), exclude_artifact_id=artifact.id,
                )
                if occupied >= max(1, self.policy.max_active_executions):
                    # Positive evidence (Section 9): target validated, not an
                    # already-stable completed payload, candidate not
                    # expired, executor.prepare() succeeded with no
                    # InputRequirement, storage/pause admission already
                    # confirmed above -- capacity is the ONLY remaining
                    # reason this artifact did not dispatch this attempt.
                    self._capacity_only_blocked.add(artifact.id)
                    return
                # Global bandwidth admission: under a finite global cap the
                # executor's assigned ceiling is proven (existing shares shrunk
                # first) before its native acquisition may begin.
                if not await self.runtime.admit(executor):
                    return
                if not await self.repository.prepare_execution(
                        artifact, handle,
                        target_initially_absent=material_initially_absent(work.materialization, footprint)):
                    return
                # Input that already proved this exact candidate's evidence
                # starts the writer admitted for it, once, through the
                # existing continuation -- still inside this admission lock,
                # which every pause-intent write also takes. A pause therefore
                # lands strictly before admission (the handoff stays with the
                # broker until the next admission) or strictly after the native
                # start (the input was consumed); never in between. The broker
                # hands input only to the exact executor identity it was proven
                # for: admitting any other claimant discards it. The executor
                # still enforces its own security facts.
                submitted = await self.inputs.take_handoff(
                    artifact.transfer_id, artifact.request_id, str(candidate.id), executor.descriptor.id,
                )
                if submitted is not None and not executor.capabilities.transient_input:
                    submitted.discard()
                    submitted = None
                if submitted is not None:
                    try:
                        observed = await executor.start_with_input(request, handle, submitted)
                    except Exception as exc:
                        observed = ExecutionObservation(handle, ExecutionState.UNKNOWN,
                            error=unknown_failure(exc, integration_id=executor.descriptor.id, domain=Domain.EXECUTOR,
                                                  stage=Stage.QUEUE, secrets=submitted.secret_values()))
                    finally:
                        submitted.discard()
            if submitted is None:
                try:
                    if native_retry:
                        # Core already decided this same-candidate retry and
                        # fenced the previous attempt; the executor may carry
                        # its native state into the new, durably prepared one.
                        observed = await executor.retry_from(request, handle, retry_from)
                    else:
                        observed = await executor.start(request, handle)
                except Exception as exc:
                    observed = ExecutionObservation(handle, ExecutionState.UNKNOWN,
                        error=unknown_failure(exc, integration_id=executor.descriptor.id, domain=Domain.EXECUTOR, stage=Stage.QUEUE))
            current = next(item for item in await self.repository.artifacts(artifact.transfer_id) if item.id == artifact.id)
            await self._execution_result(current, executor, observed)
        except Exception as exc:
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(exc, integration_id="", domain=Domain.INTERNAL, stage=Stage.QUEUE)
            await self.repository.artifact_state(artifact.id, "error", error=error)

    def _require_prepared(self, prepared, executor_id: str, attempt_id: str) -> None:
        if (not isinstance(prepared, ExecutionHandle) or prepared.executor_id != executor_id
                or prepared.attempt_id != attempt_id or not isinstance(prepared.correlation, Mapping)
                or not (prepared.native is None or isinstance(prepared.native, Mapping))):
            raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.QUEUE))

    async def _native_retry_available(self, executor, previous: ExecutionHandle) -> bool:
        """Static capability AND current runtime availability, for a previous
        attempt of this very executor."""
        if not executor.capabilities.native_assisted_retry or previous.executor_id != executor.descriptor.id:
            return False
        try:
            health = await executor.health()
        except Exception:
            return False
        return ExecutorRuntimeCapability.NATIVE_ASSISTED_RETRY in health.available_runtime_capabilities

    async def _continue_executor_input(self, challenge: InputChallenge, artifacts):
        if not await self.inputs.has(challenge) or not await self._live(challenge.transfer_id, admission=True):
            return
        artifact = next((item for item in artifacts if item.id == challenge.artifact_id), None)
        if artifact is None or artifact.state != "input_required" or not artifact.candidates:
            await self.challenges.clear(challenge)
            await self.inputs.clear(challenge.id)
            return
        # Universal execution-admission invariant (DP 1.0.12 canonical
        # architecture correction, Workstream A): an interactive executor-
        # input continuation (``start_with_input``/``prepare_with_input`` +
        # ``start``, below) is a native side effect exactly like
        # ``executor.prepare()`` in ``_dispatch()`` -- it must stop for the
        # SAME HOLD/STALE authority before either native call, not only the
        # ordinary dispatch path.
        admission = await self.repository.materialization_authorization(artifact)
        if admission.kind == MaterializationAdmissionKind.HOLD:
            return
        if admission.kind == MaterializationAdmissionKind.STALE:
            await self.challenges.clear(challenge)
            await self.inputs.clear(challenge.id)
            await self._retire_stale_materialization(artifact)
            return
        candidate = artifact.candidates[artifact.selected]
        try:
            executor = self.registry.executor_for_subject(ExecutionSubject.of(candidate))
        except TransferError:
            executor = None
        if (executor is None or executor.descriptor.id != challenge.integration_id
                or not executor.capabilities.transient_input):
            # The challenged executor is no longer the selected claimant: its
            # challenge is retired, the submitted input dies unused, and the
            # artifact re-enters ordinary routing (which asks again through the
            # one INPUT_REQUIRED lifecycle if the new claimant needs input).
            await self._retire_executor_challenge(challenge, artifact)
            return
        # The challenge's operation IS this continuation's attempt, so the work
        # names it explicitly rather than inheriting the artifact's current one.
        work = self._work(artifact, candidate, challenge.operation_id)
        request = ExecutionRequest(work, challenge.operation_id)
        submitted = None

        if artifact.execution is not None and artifact.execution.attempt_id == challenge.operation_id:
            try:
                async with self._dispatch_lock:
                    if not self.dispatch_permitted or not await self._live(challenge.transfer_id, admission=True):
                        return
                    # Close the TOCTOU window (specification section 7.4):
                    # revalidate the same authority immediately before the
                    # irreversible native commitment below.
                    revalidation = await self.repository.materialization_authorization(artifact)
                    if revalidation.kind != MaterializationAdmissionKind.PROCEED:
                        return
                    occupied = await self.repository.occupied_execution_slots(
                        self.clock(), exclude_artifact_id=artifact.id,
                    )
                    if occupied >= max(1, self.policy.max_active_executions) or not await self.runtime.admit(executor):
                        return
                    submitted = await self.inputs.take(challenge)
                    if submitted is None:
                        return
                    observed = await executor.start_with_input(request, artifact.execution, submitted)
                    current = next(item for item in await self.repository.artifacts(challenge.transfer_id) if item.id == artifact.id)
                    await self._execution_result(current, executor, observed)
                    await self.challenges.current(challenge.transfer_id)
            except Exception as exc:
                secrets = submitted.secret_values() if submitted else ()
                error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                    exc, integration_id=challenge.integration_id, domain=Domain.EXECUTOR, stage=Stage.QUEUE, secrets=secrets)
                await self.challenges.clear(challenge)
                await self.repository.artifact_state(artifact.id, "error", error=error)
                await self.repository.outcome(challenge.transfer_id, TransferOutcome(OutcomeKind.FAILURE, error))
            finally:
                if submitted:
                    submitted.discard()
            return

        try:
            footprint = self._footprint(executor, work)
            async with self._dispatch_lock:
                if not self.dispatch_permitted or not await self._live(challenge.transfer_id, admission=True):
                    return
                # Close the TOCTOU window (specification section 7.4):
                # revalidate the same authority immediately before the
                # irreversible native commitment below.
                revalidation = await self.repository.materialization_authorization(artifact)
                if revalidation.kind != MaterializationAdmissionKind.PROCEED:
                    return
                occupied = await self.repository.occupied_execution_slots(
                    self.clock(), exclude_artifact_id=artifact.id,
                )
                if occupied >= max(1, self.policy.max_active_executions) or not await self.runtime.admit(executor):
                    return
                submitted = await self.inputs.take(challenge)
                if submitted is None:
                    return
                prepared = executor.prepare_with_input(request, submitted)
                if isinstance(prepared, InputRequirement):
                    await self.challenges.replace(challenge, prepared)
                    return
                self._require_prepared(prepared, challenge.integration_id, challenge.operation_id)
                if not await self.repository.prepare_execution(
                        artifact, prepared, from_input_required=True,
                        target_initially_absent=material_initially_absent(work.materialization, footprint)):
                    return
                handle = prepared
            await self.challenges.clear(challenge)
            try:
                observed = await executor.start(request, handle)
            except Exception as exc:
                observed = ExecutionObservation(handle, ExecutionState.UNKNOWN,
                    error=unknown_failure(exc, integration_id=executor.descriptor.id, domain=Domain.EXECUTOR,
                                          stage=Stage.QUEUE, secrets=submitted.secret_values()))
            current = next(item for item in await self.repository.artifacts(challenge.transfer_id) if item.id == artifact.id)
            await self._execution_result(current, executor, observed)
        except Exception as exc:
            secrets = submitted.secret_values() if submitted else ()
            error = exc.error if isinstance(exc, TransferError) else unknown_failure(
                exc, integration_id=challenge.integration_id, domain=Domain.EXECUTOR, stage=Stage.QUEUE, secrets=secrets)
            await self.challenges.clear(challenge)
            await self.repository.artifact_state(artifact.id, "error", error=error)
            await self.repository.outcome(challenge.transfer_id, TransferOutcome(OutcomeKind.FAILURE, error))
        finally:
            if submitted:
                submitted.discard()

    async def _retire_executor_challenge(self, challenge: InputChallenge, artifact: Artifact) -> None:
        """Retire an executor-origin challenge whose executor stopped being the
        selected claimant, and return the artifact to ordinary routing without
        orphaning a native writer: a challenged attempt is requeued only once
        its native work is positively stopped (or never existed)."""
        await self.challenges.clear(challenge)
        await self.inputs.clear(challenge.id)
        if artifact.execution is not None:
            owner = self.registry.executor_for_handle(artifact.execution)
            if owner is None:
                return
            observed = await self._observe_execution(owner, artifact.execution)
            await self.repository.execution(observed)
            if not observed.stopped:
                return
        await self.repository.artifact_state(artifact.id, "queued", release=True)

    async def _execution_result(self, artifact, executor, observed):
        observed = await self._accept_observation(artifact.execution, observed)
        artifact = replace(artifact, execution=observed.handle)
        idle_seconds = await self.repository.execution_idle_seconds(observed, self.clock())
        await self.repository.execution(observed)
        if artifact.candidates and executor.capabilities.transient_input:
            requirement = executor.input_requirement(artifact.candidates[artifact.selected], observed)
            if requirement is not None:
                if not isinstance(requirement, InputRequirement):
                    raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.RECONCILIATION))
                await self.challenges.wait_executor(artifact, executor.descriptor.id, observed.handle.attempt_id, requirement)
                return
        if not await self._live(artifact.transfer_id):
            await self.repository.execution(await self._cancel_execution(executor, observed.handle))
            return
        transfer = await self.repository.get(artifact.transfer_id)
        if (observed.state in {ExecutionState.QUEUED, ExecutionState.RUNNING}
                and (transfer.paused or await self.repository.globally_paused())):
            await self._converge_execution(artifact, executor, observed)
            return
        if observed.state == ExecutionState.UNKNOWN:
            return
        if (observed.state == ExecutionState.RUNNING and observed.error is None and observed.activity.progress_expected
                and self.policy.stalled_after_seconds > 0 and idle_seconds >= self.policy.stalled_after_seconds):
            confirmed = await self._cancel_execution(executor, observed.handle)
            await self.repository.execution(confirmed)
            await self.repository.outcome(artifact.transfer_id, self._cancellation_outcome(
                confirmed, CancellationInitiator.POLICY), attempt_id=observed.handle.attempt_id)
            if confirmed.error or confirmed.state not in {ExecutionState.ABSENT, ExecutionState.CANCELLED}:
                return
            error = self._error(Category.TRANSFER_STALLED, Stage.EXECUTION, domain=Domain.EXECUTOR,
                retryability=Retryability.BACKOFF)
            await self._recover_artifact(artifact, error)
        elif observed.state == ExecutionState.SUCCEEDED:
            candidate = artifact.candidates[artifact.selected] if artifact.candidates else None
            if candidate is None:
                raise TransferError(self._error(Category.NO_TRANSFER_CANDIDATE, Stage.VERIFICATION))
            work = self._work(artifact, candidate)
            footprint = self._footprint(executor, work)
            # DP 1.0.12 canonical lifecycle/recovery/completion rework,
            # Section 5: the executor's materialization report is a fact to
            # verify, never to trust. The one generalized verifier reconciles
            # the provider-reported size, the executor's final total and the
            # artifact's recorded size against stable local material (a size
            # that is unknown everywhere never collapses into an affirmative
            # zero-byte completion) and returns the size the material proves;
            # a missing/invalid report verifies nothing.
            verified = await verify_materialization(
                self.root, work.materialization, observed.materialization, footprint,
                reported_bytes=candidate.expected_bytes, observed_total=observed.progress.total_bytes,
                recorded_bytes=artifact.expected_bytes, integrity=candidate.integrity,
                delay=self.policy.adoption_stability_seconds,
            )
            if verified is not None and await self.repository.record_materialization(observed.handle, verified.result):
                # FUNC-001: record the canonical size fact durably alongside the
                # accepted size, so a restart reconstructs the same semantics
                # instead of re-reading a bare number. KNOWN_ZERO is returned
                # only when trusted affirmative-zero evidence AND stable
                # material proved it; an unknown/defaulted zero verifies nothing.
                await self.repository.artifact_state(
                    artifact.id, "completed", expected_bytes=verified.total_bytes,
                    size_knowledge=verified.size_knowledge,
                )
            else:
                error = self._error(Category.MATERIALIZATION_FAILED, Stage.VERIFICATION, domain=Domain.INTEGRITY,
                                    retryability=Retryability.AFTER_RESOURCE_CHANGE)
                # Read before the failure is recorded: ownership belongs to the
                # still-current execution, and is a durable admission-time fact
                # -- never inferred here from size, name, mtime or the executor.
                owned = await self.repository.execution_owns_target(observed.handle)
                await self.repository.artifact_state(artifact.id, "error", error=error)
                await self.repository.outcome(artifact.transfer_id, TransferOutcome(OutcomeKind.FAILURE, error), attempt_id=observed.handle.attempt_id)
                if owned:
                    await self._retire_execution_owned_material(artifact, work, footprint)
        elif observed.state == ExecutionState.FAILED:
            error = observed.error or self._error(Category.UNMAPPED_EXECUTOR_ERROR, Stage.EXECUTION, domain=Domain.EXECUTOR)
            await self._recover_artifact(artifact, error)
        elif observed.state == ExecutionState.ABSENT:
            error = self._error(Category.ORPHANED_RESOURCE, Stage.RECONCILIATION, domain=Domain.RECONCILIATION,
                                retryability=Retryability.BACKOFF)
            await self._recover_artifact(artifact, error)
        elif observed.state == ExecutionState.CANCELLED:
            await self.repository.outcome(artifact.transfer_id, TransferOutcome(OutcomeKind.CANCELLED,
                cancellation_initiator=CancellationInitiator.EXECUTOR), attempt_id=observed.handle.attempt_id)


    async def _retire_execution_owned_material(self, artifact, work: ExecutionWork, footprint: ExecutionFootprint,
                                               *, prune_empty_parents=False, required=False) -> None:
        """Retire the material an execution itself created -- its FILE target
        or its dedicated COLLECTION root, plus its declared transient paths --
        through the one hardened cleanup owner. The caller has already proven
        positive execution ownership; this method never infers it.

        ``required`` says whether retirement is part of an obligation the
        caller still owes. Verification rejection does not owe one: that
        failure is already durable and a cleanup failure changes nothing about
        it, so it is logged. A deleted transfer's cleanup does owe one -- the
        space is not reclaimed until the material is gone -- so its failure
        propagates and the caller keeps the obligation open."""
        try:
            await asyncio.to_thread(retire_materialization, self.root, work.materialization, footprint,
                                    owned=True, prune_empty_parents=prune_empty_parents)
        except (TransferError, OSError) as exc:
            if required:
                raise
            logger.warning(
                "execution-owned invalid material could not be retired transfer=%s artifact=%s: %s",
                artifact.transfer_id, artifact.id, type(exc).__name__,
            )

    async def _retire_deleted_execution_material(self, executor, attempt: ExecutionAttempt) -> None:
        """Reclaim the incomplete material a DELETED transfer's now-stopped
        execution positively owns.

        Ordering is the invariant, not an optimization: the caller reaches here
        only once it has positively observed that the native writer stopped, so
        nothing can still be writing what is removed. Ownership is the durable
        admission-time fact and nothing else -- never a file's name, size, age,
        path shape, provider or executor -- so material that pre-dated the
        execution, or that a newer attempt now owns, is not this execution's to
        retire and survives untouched.

        Delivered payload is out of scope twice over: a verified artifact's
        attempt is no longer a live writer and is never handed to cleanup at
        all, and a completed artifact is skipped outright here. Cancellation
        semantics are unchanged -- only an explicitly DELETED parent reclaims
        local material. Anything that cannot be reconstructed from durable
        attempt facts raises instead of guessing, which leaves the caller's
        cleanup obligation open for the existing retry cadence.
        """
        transfer = await self.repository.get(attempt.transfer_id)
        if transfer is None or transfer.state != TransferState.DELETED:
            return
        if not await self.repository.execution_owns_target(attempt.handle):
            return
        artifact = await self._current_artifact(attempt.transfer_id, attempt.artifact_id)
        if artifact is not None and artifact.state == "completed":
            return
        if artifact is None or attempt.candidate is None:
            raise TransferError(self._error(Category.LOCAL_CLEANUP_FAILED, Stage.CLEANUP, domain=Domain.CLEANUP,
                                            retryability=Retryability.AFTER_RESOURCE_CHANGE))
        work = self._work(artifact, attempt.candidate, attempt.handle.attempt_id)
        await self._retire_execution_owned_material(artifact, work, self._footprint(executor, work),
                                                    prune_empty_parents=True, required=True)

    async def _aggregate(self, transfer_id: int):
        """DP 1.0.12 recovery leveling, Sections 21-22: the decision and the
        write are one atomic ``TransferRepository.aggregate_lifecycle`` call
        (transfers/_repository_base.py) rather than several independently
        timed reads followed by a separate write -- see that method's
        docstring for why this is required, not merely tidier. This engine
        method now only supplies the one in-memory (non-database) fact the
        repository cannot see for itself, and runs the completion sequence
        (executor I/O; cannot happen inside that same bounded transaction)
        using the EXACT artifact snapshot the decision was made from."""
        challenge = await self.challenges.current(transfer_id)
        outcome = await self.repository.aggregate_lifecycle(transfer_id, input_required=bool(challenge))
        if outcome is None:
            return
        if outcome.should_complete:
            await self._complete(transfer_id, outcome.artifacts)

    async def _complete(self, transfer_id: int, artifacts):
        if (await self.repository.get(transfer_id)).state == TransferState.POST_PROCESSING:
            return
        outputs: list[str] = []
        for artifact in artifacts:
            executor = None
            try:
                executor, work, footprint = self._artifact_work(artifact)
                if executor is None or work is None:
                    raise TransferError(self._error(Category.UNSUPPORTED_CAPABILITY, Stage.VERIFICATION,
                        domain=Domain.REQUEST, retryability=Retryability.NEVER))
            except TransferError as exc:
                await self.repository.artifact_state(artifact.id, "error", error=exc.error)
                await self.repository.state(transfer_id, TransferState.FAILED, error=exc.error)
                return
            try:
                # FUNC-001: empty material is acceptable only where canonical
                # durable size truth affirmatively says this object is zero
                # bytes. The presence of an execution is not evidence about
                # size -- every failed, pathological and unknown-size execution
                # has one too -- so it can never authorize committing an empty
                # file as delivered artifact material. This boundary defends
                # itself directly rather than relying on upstream sequencing:
                # reached with unproven material through any caller, it fails
                # closed into the requeue/verification path below.
                stored = (await self.repository.execution_materialization(artifact.execution.attempt_id)
                          if artifact.execution else None)
                paths = await asyncio.to_thread(
                    verified_material_paths, self.root, work.materialization, stored, footprint,
                    expected_bytes=artifact.expected_bytes,
                    allow_empty=artifact.size_knowledge == SizeKnowledge.KNOWN_ZERO,
                )
                if paths is not None:
                    outputs.extend(paths)
                    continue
                if artifact.execution:
                    stopped = await self._cancel_execution(executor, artifact.execution)
                    await self.repository.execution(stopped)
                    if not stopped.stopped:
                        raise TransferError(stopped.error or self._error(
                            Category.RECONCILIATION_FAILED, Stage.RECONCILIATION, retryability=Retryability.BACKOFF))
                await self.repository.artifact_state(artifact.id, "queued", release=True)
                await self.repository.state(transfer_id, TransferState.QUEUED)
                return
            except Exception as exc:
                error = exc.error if isinstance(exc, TransferError) else unknown_failure(exc,
                    integration_id=executor.descriptor.id if executor else "", domain=Domain.EXECUTOR, stage=Stage.VERIFICATION)
                await self.repository.artifact_state(artifact.id, "error", error=error)
                await self.repository.state(transfer_id, TransferState.FAILED, error=error)
                await self.repository.outcome(transfer_id, TransferOutcome(OutcomeKind.FAILURE, error))
                return
        if self.postprocessors:
            await self.repository.state(transfer_id, TransferState.POST_PROCESSING, progress=100, verified=True)
            await self.repository.queue_postprocessing(transfer_id, self.postprocessors, tuple(outputs))
            return
        await self._delivered(transfer_id)

    async def _delivered(self, transfer_id):
        if await self.repository.state(transfer_id, TransferState.COMPLETED, progress=100, verified=True):
            await self.repository.outcome(transfer_id, TransferOutcome(OutcomeKind.SUCCESS))
            if self.policy.cleanup_after_completion:
                await self._cleanup_resources(transfer_id)

    async def recover_postprocessing(self):
        for job in await self.repository.interrupted_postprocessing():
            error = self._error(Category.RECOVERY_FAILED, Stage.POST_PROCESSING, domain=Domain.POST_PROCESSING)
            outcome = TransferOutcome(OutcomeKind.FAILURE, error)
            await self.repository.outcome(job["transfer_id"], outcome)
            if await self.repository.finish_postprocessing(job["transfer_id"], job["processor_id"], outcome):
                await self._delivered(job["transfer_id"])

    async def process_postprocessors(self):
        async with self._postprocess_lock:
            processors = {item.descriptor.id: item for item in self.postprocessors}
            for job in await self.repository.postprocessing_jobs():
                transfer_id, processor_id = job["transfer_id"], job["processor_id"]
                if not await self._live(transfer_id, admission=True):
                    continue
                if not await self.repository.claim_postprocessing(transfer_id, processor_id):
                    continue
                processor = processors.get(processor_id)
                try:
                    if processor is None:
                        raise TransferError(self._error(Category.UNSUPPORTED_CAPABILITY, Stage.POST_PROCESSING, domain=Domain.POST_PROCESSING))
                    outcome = await processor.process(transfer_id, tuple(codec.load(job["paths"])))
                    if not isinstance(outcome, TransferOutcome):
                        raise TransferError(self._error(Category.INVALID_ADAPTER_RESPONSE, Stage.POST_PROCESSING, domain=Domain.POST_PROCESSING))
                except Exception as exc:
                    error = exc.error if isinstance(exc, TransferError) else unknown_failure(exc,
                        integration_id=processor_id, domain=Domain.POST_PROCESSING, stage=Stage.POST_PROCESSING)
                    outcome = TransferOutcome(OutcomeKind.FAILURE, error)
                await self.repository.outcome(transfer_id, outcome)
                if await self.repository.finish_postprocessing(transfer_id, processor_id, outcome):
                    await self._delivered(transfer_id)

    async def select_artifact(self, transfer_id: int, artifact_id: int, *, selected: bool):
        transfer = await self.repository.get(transfer_id)
        if not transfer or transfer.state in {TransferState.DELETED, TransferState.CONSOLIDATED}:
            raise KeyError(transfer_id)
        await self.repository.select_artifact(transfer_id, artifact_id, selected)
        if selected and transfer.state == TransferState.COMPLETED:
            await self.repository.state(transfer_id, TransferState.QUEUED, operator=True, expected_epoch=transfer.epoch)

    async def cancel_artifact(self, transfer_id: int, artifact_id: int):
        artifact = next((item for item in await self.repository.artifacts(transfer_id) if item.id == artifact_id), None)
        if artifact is None:
            raise KeyError(artifact_id)
        if artifact.execution:
            executor = self.registry.executor_for_handle(artifact.execution)
            stopped = await self._cancel_execution(executor, artifact.execution)
            await self.repository.execution(stopped)
            await self.repository.outcome(transfer_id, self._cancellation_outcome(stopped),
                                          attempt_id=artifact.execution.attempt_id)
            if not stopped.stopped:
                # Acknowledgement is not stop truth: the attempt stays owned
                # and reconciled until its native writer is proven stopped.
                raise TransferError(stopped.error or self._error(
                    Category.RECONCILIATION_FAILED, Stage.CLEANUP, domain=Domain.RECONCILIATION,
                    retryability=Retryability.BACKOFF))
        await self.repository.artifact_state(artifact_id, "cancelled")
        await self._aggregate(transfer_id)

    async def submit_input(self, transfer_id: int, challenge_id: str, method: str, values):
        transfer = await self.repository.get(transfer_id)
        if transfer is None:
            raise KeyError(transfer_id)
        challenge = await self.challenges.current(transfer_id)
        if transfer.state != TransferState.INPUT_REQUIRED or challenge is None or challenge.id != challenge_id:
            raise InputSubmissionRejected("Input challenge is stale")
        if challenge.origin == InputOrigin.EVIDENCE and await self._evidence_target(challenge) is None:
            await self.challenges.clear(challenge)
            raise InputSubmissionRejected("Input challenge is stale")
        await self.inputs.submit(challenge, method, values)
        return challenge

    async def cancel(self, transfer_id: int):
        lock = self._transfer_locks.setdefault(transfer_id, asyncio.Lock())
        async with lock:
            transfer = await self.repository.get(transfer_id)
            if transfer is None:
                raise KeyError(transfer_id)
            if transfer.state == TransferState.CONSOLIDATED:
                return ()

            if not await self.repository.cancel_with_execution_cleanup(
                transfer_id, expected_epoch=transfer.epoch, now=self.clock(),
            ):
                current = await self.repository.get(transfer_id)
                if not current or current.state != TransferState.CANCELLED:
                    raise TransferError(self._error(
                        Category.RESOURCE_STATE_CONFLICT, Stage.EXECUTION, domain=Domain.LIFECYCLE,
                        retryability=Retryability.NEVER,
                    ))

            challenge = await self.challenges.current(transfer_id)
            if challenge:
                await self.inputs.clear(challenge.id)
                await self.challenges.clear_transfer(transfer_id)
            await self.inputs.discard_transfer(transfer_id)

            return await self._cleanup_executions_pending(transfer_id=transfer_id)

    def _executor_cleanup_exception(self, executor_id: str, exc: Exception) -> NormalizedError:
        if isinstance(exc, TransferError):
            return exc.error
        native = unknown_failure(
            exc, integration_id=executor_id, domain=Domain.EXECUTOR, stage=Stage.CLEANUP,
        )
        return NormalizedError(
            Domain.CLEANUP, Category.REMOTE_CLEANUP_FAILED, Stage.CLEANUP,
            retryability=Retryability.BACKOFF,
            integration_id=executor_id, diagnostic=native.diagnostic,
        )

    def _execution_cleanup_poll_at(self, now: float) -> float:
        cadence = min(float(self.policy.resource_poll_interval), float(self.policy.max_retry_delay))
        return now + max(1.0, cadence)

    async def _cleanup_executions_pending(self, *, transfer_id: int | None = None):
        errors = []
        now = self.clock()
        for attempt, attempts, previous_error in await self.repository.pending_execution_cleanup(
            now, transfer_id=transfer_id,
        ):
            handle = attempt.handle
            poll_at = self._execution_cleanup_poll_at(now)
            if not await self.repository.claim_execution_cleanup(
                handle.attempt_id, now=now, lease_until=poll_at,
            ):
                continue

            executor = self.registry.executor_for_handle(handle)
            if executor is None:
                error = self._error(
                    Category.UNSUPPORTED_CAPABILITY, Stage.CLEANUP, domain=Domain.CLEANUP,
                    retryability=Retryability.NEVER,
                )
                await self.repository.execution_cleanup_retry(handle.attempt_id, error, poll_at)
                errors.append(error)
                continue

            cancel_attempted = False
            try:
                observed = await self._observe_execution(executor, handle)
                handle = observed.handle
                await self.repository.execution(observed)
                if observed.stopped:
                    await self._retire_deleted_execution_material(executor, replace(attempt, handle=handle))
                    await self.repository.execution_cleanup_complete(handle.attempt_id)
                    continue
                if observed.error is not None:
                    await self.repository.execution_cleanup_retry(handle.attempt_id, observed.error, poll_at)
                    errors.append(observed.error)
                    continue

                destructive_allowed = attempts < max(1, self.policy.max_attempts)
                if destructive_allowed and previous_error is not None and attempts:
                    destructive_allowed = self.policy.retry(previous_error, attempts, now).automatic
                if not destructive_allowed:
                    error = previous_error or self._error(
                        Category.REMOTE_CLEANUP_FAILED, Stage.CLEANUP, domain=Domain.CLEANUP,
                        retryability=Retryability.BACKOFF,
                    )
                    await self.repository.execution_cleanup_retry(handle.attempt_id, error, poll_at)
                    continue

                if not await self.repository.execution_cleanup_attempt(handle.attempt_id):
                    continue
                cancel_attempted = True
                stopped = await self._cancel_execution(executor, handle)
                await self.repository.execution(stopped)
                await self.repository.outcome(attempt.transfer_id, self._cancellation_outcome(stopped),
                                              attempt_id=handle.attempt_id)
                if not stopped.stopped:
                    # Native stop is not proven (an unconfirmed or lost
                    # acknowledgement): cleanup authority is retained and the
                    # next pass reconciles by observation before cancelling again.
                    raise TransferError(stopped.error or self._error(
                        Category.REMOTE_CLEANUP_FAILED, Stage.CLEANUP, domain=Domain.CLEANUP,
                        retryability=Retryability.BACKOFF,
                    ))

                await self._retire_deleted_execution_material(executor, replace(attempt, handle=handle))
                await self.repository.execution_cleanup_complete(handle.attempt_id)
            except Exception as exc:
                error = self._executor_cleanup_exception(handle.executor_id, exc)
                retry_at = poll_at
                if cancel_attempted:
                    decision = self.policy.retry(error, attempts + 1, now)
                    if decision.retry_at is not None:
                        retry_at = decision.retry_at
                await self.repository.execution_cleanup_retry(handle.attempt_id, error, retry_at)
                errors.append(error)
        return tuple(errors)

    async def delete(self, transfer_id: int, *, remote=True):
        challenge = await self.challenges.current(transfer_id)
        if challenge:
            await self.inputs.clear(challenge.id)
        await self.challenges.clear_transfer(transfer_id)
        await self.inputs.discard_transfer(transfer_id)
        await self.repository.delete(transfer_id, remote=remote, now=self.clock())
        await self._cleanup_executions_pending(transfer_id=transfer_id)
        if remote:
            await self._cleanup_resources(transfer_id, explicit=True)

    async def _cleanup_resources(self, transfer_id: int, *, explicit=False):
        for resource, state, pending in await self.repository.resources(transfer_id):
            if state == ResourceState.ABSENT:
                continue
            if not explicit and resource.ownership not in {Ownership.CREATED, Ownership.ADOPTED}:
                continue
            authority = CleanupAuthority.USER_REQUEST if explicit else CleanupAuthority.OWNED
            await self.repository.cleanup_intent(transfer_id, resource.id, authority)
        await self._cleanup_pending()

    # Upper bound on how long a DEAD cleanup owner (cancelled task, crash, an
    # exception between claim and finalization) can hold a claim: its heartbeat
    # stops with it, so the lease runs out and the ordinary cadence reclaims it.
    # Short enough that a fresh same-object transfer fenced behind it resumes
    # within a couple of minutes, with no restart and no operator action.
    CLEANUP_CLAIM_LEASE_SECONDS = 120.0
    # A LIVE owner is never reclaimed however long its provider call legitimately
    # runs: it renews the lease with its own token once a third of it has elapsed.
    # The renewal decision is taken on the engine clock; this is only how often
    # the running call's heartbeat looks at it.
    CLEANUP_HEARTBEAT_POLL_SECONDS = 1.0

    async def _cleanup_pending(self):
        """The ONE cleanup cadence. Every path that owes provider cleanup (delete,
        completion cleanup, restartable re-resolution, the scheduler tick) drains
        through here, and every outcome after a claim is acquired is finalized
        conditionally on that claim's token (see ``repository.claim_cleanup``)."""
        scan_now = self.clock()
        for transfer_id, resource, authority, attempts, binding_id in await self.repository.pending_cleanup(scan_now):
            provider = self.registry.providers.get(resource.provider_id)
            if provider is None:
                continue
            # The scan is one snapshot, but bindings are worked serially and an
            # earlier provider call may legitimately run longer than a whole lease.
            # Every lease therefore begins at the instant ITS claim is taken; a lease
            # dated from the scan could already be expired at birth and be reclaimed
            # by a competing worker before its first heartbeat.
            claim_now = self.clock()
            lease_until = claim_now + self.CLEANUP_CLAIM_LEASE_SECONDS
            token = await self.repository.claim_cleanup(binding_id, now=claim_now, lease_until=lease_until)
            if token is None:
                continue
            if not isinstance(provider, Cleanup):
                # No cleanup operation can ever act on this resource, so the
                # obligation is permanently unactionable: abandon it (evidence and
                # reason kept) rather than let the predecessor fence wait forever.
                await self.repository.cleanup_retry(binding_id, token, self._error(
                    Category.UNSUPPORTED_CAPABILITY, Stage.CLEANUP, domain=Domain.CLEANUP,
                    retryability=Retryability.NEVER,
                ), None)
                continue
            try:
                outcome = await self._run_owned_cleanup(
                    provider.cleanup(CleanupDirective(resource, CleanupAuthority(authority))),
                    binding_id, token, lease_until,
                )
            except _CleanupOwnershipLost:
                continue                    # the new owner drives it; this one finalizes nothing
            except Exception as exc:
                outcome = TransferOutcome(OutcomeKind.FAILURE, unknown_failure(exc,
                    integration_id=provider.descriptor.id, domain=Domain.CLEANUP, stage=Stage.CLEANUP))
            else:
                if not isinstance(outcome, TransferOutcome):
                    outcome = TransferOutcome(OutcomeKind.FAILURE, self._error(
                        Category.INVALID_ADAPTER_RESPONSE, Stage.CLEANUP, domain=Domain.PROVIDER,
                        retryability=Retryability.NEVER,
                    ))
            await self.repository.outcome(transfer_id, outcome)
            if outcome.kind in {OutcomeKind.SUCCESS, OutcomeKind.SKIPPED}:
                await self.repository.cleanup_complete(
                    binding_id, token, absent=resource if outcome.kind == OutcomeKind.SUCCESS else None,
                )
            else:
                error = outcome.error or self._error(Category.REMOTE_CLEANUP_FAILED, Stage.CLEANUP, domain=Domain.CLEANUP)
                decision = self.policy.retry(error, attempts + 1, self.clock())
                await self.repository.cleanup_retry(binding_id, token, error, decision.retry_at)

    async def _run_owned_cleanup(self, operation, binding_id: str, token: str, held_until: float):
        """Run one provider cleanup call while its owner heartbeats the lease, so
        the claim represents live ownership: at most ONE cleanup call is ever in
        flight for a binding. If the owner is cancelled the heartbeat stops with
        it (the lease then expires and the cadence reclaims); if the heartbeat
        finds the claim was lost, the call is aborted and ``_CleanupOwnershipLost``
        is raised instead of returning an outcome."""
        state = {"lost": False}
        call = asyncio.ensure_future(operation)
        beat = asyncio.ensure_future(self._hold_cleanup_lease(binding_id, token, held_until, call, state))
        try:
            result = await call
            if state["lost"]:
                # The heartbeat aborted this call, but the provider swallowed the
                # cancellation and returned anyway. Ownership already moved on, so
                # that outcome must never be recorded.
                raise _CleanupOwnershipLost()
            return result
        except asyncio.CancelledError:
            if state["lost"]:
                raise _CleanupOwnershipLost() from None
            raise
        finally:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)

    async def _hold_cleanup_lease(self, binding_id: str, token: str, held_until: float, call, state) -> None:
        lease = self.CLEANUP_CLAIM_LEASE_SECONDS
        while True:
            await asyncio.sleep(self.CLEANUP_HEARTBEAT_POLL_SECONDS)
            now = self.clock()
            if now < held_until - lease * 2 / 3:
                continue
            try:
                renewed = await self.repository.renew_cleanup_claim(
                    binding_id, token, now=now, lease_until=now + lease,
                )
            except Exception:
                renewed = None                     # transient store failure: retry on the next beat
            if renewed:
                held_until = now + lease
            elif renewed is False or now >= held_until:
                # Ownership is lost (taken over, or the lease ran out unrenewed): stop
                # acting on the resource rather than run beside the new owner.
                state["lost"] = True
                call.cancel()
                return

    async def cleanup_pending(self):
        """Retry durable cleanup intents; this never invents cleanup authority."""
        await self._cleanup_pending()

    async def reconcile_inventory(self):
        """Missing from an incomplete inventory is never evidence of absence."""
        reports = []
        for provider in self.registry.providers.values():
            if not provider.descriptor.enabled or not isinstance(provider, Inventory):
                continue
            try:
                snapshot = await provider.inventory()
            except Exception as exc:
                reports.append(unknown_failure(exc, integration_id=provider.descriptor.id, domain=Domain.PROVIDER, stage=Stage.RECONCILIATION))
                continue
            if snapshot.error:
                reports.append(snapshot.error)
                continue
            known = {resource.id: transfer.id for transfer in await self.repository.active()
                     for resource, _state, _pending in await self.repository.resources(transfer.id)}
            for item in snapshot.observations:
                if item.resource.id in known:
                    await self.repository.resource_observation(known[item.resource.id], item.resource, item.state)
                elif item.request:
                    transfer = await self.submit((item.request,), name=item.name, source="inventory", reacquire=False)
                    # Adoption is the one binding path that never passes through a
                    # resolution attempt, so it is owned end to end by the
                    # repository transition (inventory-created imports only,
                    # fence-honouring) and then normalized by the same post-binding
                    # selection owner as every other binding.
                    if await self.repository.adopt_inventory_resource(transfer.id, item.resource, item.state):
                        root = next((record for record in await self.repository.requests(transfer.id)
                                     if record.parent_id is None), None)
                        if root is not None:
                            await self._secure_root_selection(root, provider, item)
        return tuple(reports)
