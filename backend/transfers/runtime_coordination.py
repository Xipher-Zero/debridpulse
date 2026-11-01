"""Core-owned executor runtime limits: the one global download-bandwidth allocator.

``execution_runtime_limits.max_download_bytes_per_second`` (0 = unlimited) is
the single desired value C, a maximum for the aggregate. This owner divides it
among the executors that currently hold a bandwidth reservation, work-conserving
and at the executor level -- never per transfer -- and asks each executor, in
neutral language, to enforce only the ceiling assigned to it. Executors
enforce; they never learn the global policy.

Allocation (``_plan``): every reserved executor holds at least a floor
(``C // FLOOR_DIVISOR``, never below its declared smallest enforceable ceiling)
so work it can start or resume on its own is always bounded by a ceiling it
already enforces. The rest is water-filled from the demand facts of the
reconcile pass (``ExecutorDemand``): an executor acquiring at or above
``SATURATION`` of its confirmed ceiling wants more; one below it keeps
``DEMAND_HEADROOM`` times its measured rate; a dormant one (reserved, not
acquiring) keeps its floor; saturated executors share what remains equally,
and whatever is still unassigned is lent to the acquiring ones. A changed
ceiling is judged only once the rate window has seen it (``SETTLE_SECONDS``),
and a demand-driven change is applied only when it moves some ceiling by at
least ``C // REBALANCE_DIVISOR``. A newcomer, or an executor without facts on a
cap or reservation change, starts with a bootstrap (equal) share.

Safety rules:

* conservation: for each reserved executor this owner keeps the highest ceiling
  that may be in effect (its exposure; unbounded until confirmed, while
  Unlimited, or after an ambiguous apply). A ceiling is only ever RAISED within
  C minus every other member's exposure, and only after every needed decrease
  was positively confirmed; a failed, timed-out or unconfirmed apply counts the
  worse of the old and the requested value, never released bandwidth;
* a reservation is released only on positive truth (a certain observation that
  no reservation is needed, or no durable live native work at all); an
  uncertain executor keeps its reservation and the ceiling it may hold, which
  is never lent; one whose observation recovers is re-confirmed before
  anything is lent against it (a native restart may have lost its ceiling);
* adding an executor under a finite cap shrinks the others first, confirms,
  then applies and confirms the newcomer's ceiling, and only then admits the
  newcomer's native acquisition;
* a finite cap admits new work only through an executor that statically
  declares aggregate ceiling enforcement AND currently reports it available;
  otherwise admission fails closed and the configured/effective divergence is
  reported rather than guessed;
* a reserved executor whose ceiling can no longer be proven keeps it; where it
  offers the neutral acquisition gate, the gate is engaged until it is
  provable again -- never cancellation;
* a positive cap is never assigned as ``0`` (which means unlimited), nor below
  an executor's smallest enforceable ceiling: when the cap cannot give every
  reserved executor one, admission fails closed and enforcement is reported
  unproven;
* every admission re-reads the executor's CURRENT health: an unreachable or
  not-ready executor, or one whose ceiling capability is currently
  unavailable, is never admitted on the strength of an earlier confirmation.

Callers serialize every mutation under the engine's execution-admission lock.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Callable

from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.models import ExecutorGateResult, ExecutorRuntimeCapability, ExecutorRuntimeControlResult
from transfers.runtime_telemetry import WINDOW_SECONDS

# A reserved executor's floor is this part of the cap (never below its own
# smallest enforceable ceiling): what it may use if it starts on its own.
FLOOR_DIVISOR = 32
# Acquiring at or above this part of its confirmed ceiling, an executor wants more.
SATURATION = 0.9
# Below that it keeps this multiple of its measured rate. 1 / 1.25 < 0.9, so a
# consumer given exactly its want stays below saturation (no oscillation),
# and one delivering more than 80% of its ceiling still grows into the rest.
DEMAND_HEADROOM = 1.25
# A changed ceiling is judged only once the rate window has seen it.
SETTLE_SECONDS = WINDOW_SECONDS + 1.0
# A demand-driven rebalance moves some ceiling by at least cap // this.
REBALANCE_DIVISOR = 32
# A control call taking longer than this is ambiguous, never released bandwidth.
CONTROL_TIMEOUT_SECONDS = 15.0

_UNBOUNDED = None


@dataclass(frozen=True)
class RuntimeLimitStatus:
    """Configured desired value and the value currently proven enforced across
    the reserved executor set (``None`` when that cannot be proven)."""
    configured: int
    effective: int | None
    last_apply_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.last_apply_error is None and self.effective is not None


@dataclass(frozen=True)
class ExecutorDemand:
    """One reconcile pass's neutral facts about one executor: whether its
    observation was certain, whether any execution still requires a
    bandwidth reservation, whether any is acquiring over the network now, and
    their summed measured rate (bytes/second over the rate window)."""
    certain: bool
    required: bool
    acquiring: bool = False
    rate: float = 0.0


def _runtime_error(category=Category.EXECUTOR_UNAVAILABLE, integration_id: str = "") -> NormalizedError:
    return NormalizedError(Domain.EXECUTOR, category, Stage.EXECUTION, retryability=Retryability.BACKOFF,
                           integration_id=integration_id)


def _worse(exposure: int | None, requested: int) -> int | None:
    """The highest ceiling that may be in effect after an unconfirmed apply."""
    if exposure is _UNBOUNDED or not requested:
        return _UNBOUNDED
    return max(exposure, requested)


class ExecutionRuntimeCoordinator:
    def __init__(self, registry: Callable[[], object], repository, *, clock: Callable[[], float] = time.monotonic):
        self._registry = registry
        self._repository = repository
        self._clock = clock
        self._configured = 0
        self._reserved: set[str] = set()
        self._seeded = False
        # The highest ceiling that may be in effect per executor (bytes/s), or
        # unbounded; and the ceiling positively confirmed (0 = unlimited).
        self._exposure: dict[str, int | None] = {}
        self._confirmed: dict[str, int] = {}
        self._changed: dict[str, float] = {}
        self._facts: dict[str, ExecutorDemand] = {}
        self._error: NormalizedError | None = None
        self._unproven: set[str] = set()
        self._gated: set[str] = set()

    def configure(self, max_download_bytes_per_second: int) -> None:
        self._configured = max(0, int(max_download_bytes_per_second or 0))

    @property
    def configured(self) -> int:
        return self._configured

    @property
    def reserved(self) -> frozenset[str]:
        return frozenset(self._reserved)

    @property
    def assigned(self) -> dict[str, int]:
        """The ceilings positively confirmed for the reserved executors."""
        return {identity: self._confirmed[identity] for identity in self._reserved if identity in self._confirmed}

    @property
    def gated(self) -> frozenset[str]:
        """Executors whose acquisition gate this owner holds engaged."""
        return frozenset(self._gated)

    def _executors(self):
        return self._registry().executors

    @staticmethod
    def _minimum(executor) -> int:
        return max(1, int(executor.capabilities.minimum_bandwidth_ceiling))

    def _exposure_of(self, identity: str) -> int | None:
        return self._exposure.get(identity, _UNBOUNDED)

    async def _current(self, executor, *, ceiling: bool):
        """Current runtime truth gating a new native side effect. Returns the
        health when the executor is reachable and ready (and, when a finite cap
        applies, currently reports ceiling enforcement available)."""
        try:
            health = await executor.health()
        except Exception:
            return None
        if not (health.reachable and health.ready):
            return None
        if ceiling and ExecutorRuntimeCapability.AGGREGATE_BANDWIDTH_CEILING not in health.available_runtime_capabilities:
            return None
        return health

    async def _seed(self) -> None:
        """After a restart, every executor that durably still owns live native
        work keeps a reservation until positive truth releases it."""
        if not self._seeded:
            self._reserved |= set(await self._repository.executors_with_live_work())
            self._seeded = True

    async def _available(self, executor) -> bool:
        """Static declaration is necessary, never sufficient: the executor must
        also be reachable, ready and report the ceiling capability available
        right now."""
        if not executor.capabilities.aggregate_bandwidth_ceiling:
            return False
        return await self._current(executor, ceiling=True) is not None

    async def _apply(self, executor, ceiling: int) -> bool:
        identity = executor.descriptor.id
        if self._configured and (not isinstance(ceiling, int) or ceiling < self._minimum(executor)):
            # 0 means unlimited, and below its minimum it cannot enforce a
            # finite cap: never told either.
            self._error = _runtime_error(Category.UNSUPPORTED_CAPABILITY, identity)
            return False
        if not await self._available(executor):
            self._confirmed.pop(identity, None)
            self._error = _runtime_error(Category.UNSUPPORTED_CAPABILITY, identity)
            return False
        previous = self._exposure_of(identity)
        try:
            result = await asyncio.wait_for(executor.set_bandwidth_ceiling(ceiling), CONTROL_TIMEOUT_SECONDS)
        except Exception:
            result = None
        if (isinstance(result, ExecutorRuntimeControlResult) and result.error is None
                and result.effective_bytes_per_second == ceiling):
            if self._confirmed.get(identity) != ceiling:
                self._changed[identity] = self._clock()
            self._confirmed[identity] = ceiling
            self._exposure[identity] = ceiling or _UNBOUNDED
            return True
        # Unconfirmed: it may hold either value -- count the worse one.
        self._confirmed.pop(identity, None)
        self._exposure[identity] = _worse(previous, ceiling)
        self._error = (result.error if isinstance(result, ExecutorRuntimeControlResult) and result.error
                       else _runtime_error(integration_id=identity))
        return False

    def _plan(self, members, *, settling: bool, newcomer: str | None = None) -> dict[str, int] | None:
        """Ceilings for ``members`` under the finite cap, or ``None`` when no
        safe positive allocation exists (the caller fails closed).

        ``settling``: a demand rebalance, which holds an executor whose ceiling
        changed less than ``SETTLE_SECONDS`` ago and treats one without facts
        as dormant; otherwise (admission, cap or reservation change) one
        without facts takes a bootstrap share."""
        cap = self._configured
        executors = self._executors()
        now = self._clock()
        held: dict[str, int] = {}
        minimums: dict[str, int] = {}
        wants: dict[str, float] = {}
        acquiring: set[str] = set()
        for identity in members:
            executor = executors.get(identity)
            fact = None if identity == newcomer else self._facts.get(identity)
            confirmed = self._confirmed.get(identity)
            if (executor is None or not executor.capabilities.aggregate_bandwidth_ceiling
                    or (fact is not None and not fact.certain)):
                # Uncontrollable or uncertain: it keeps whatever it may hold.
                exposure = self._exposure_of(identity)
                if exposure is _UNBOUNDED:
                    return None
                held[identity] = exposure
                continue
            if fact is not None and fact.acquiring:
                acquiring.add(identity)
            if settling and confirmed and now - self._changed.get(identity, -math.inf) < SETTLE_SECONDS:
                held[identity] = confirmed
                continue
            minimums[identity] = self._minimum(executor)
            if fact is None:
                wants[identity] = 0.0 if settling else math.inf
            elif not fact.acquiring:
                wants[identity] = 0.0
            elif not confirmed or fact.rate >= SATURATION * confirmed:
                wants[identity] = math.inf
            else:
                wants[identity] = DEMAND_HEADROOM * fact.rate
        free = cap - sum(held.values())
        if sum(minimums.values()) > free:
            return None
        floors = {identity: max(minimum, cap // FLOOR_DIVISOR) for identity, minimum in minimums.items()}
        if sum(floors.values()) > free:
            floors = minimums
        allocation = dict(floors)
        free -= sum(floors.values())
        # Max-min fair water-filling: finite wants are met first, saturated
        # executors share the rest equally.
        pending = {identity: wants[identity] - allocation[identity]
                   for identity in allocation if wants[identity] > allocation[identity]}
        while pending and free >= len(pending):
            level = free // len(pending)
            met = {identity: need for identity, need in pending.items() if need <= level}
            if not met:
                for identity in pending:
                    allocation[identity] += level
                free -= level * len(pending)
                break
            for identity, need in met.items():
                grant = math.ceil(need)
                allocation[identity] += grant
                free -= grant
                del pending[identity]
        # Work-conserving: nothing stays unlent. Acquiring executors take the
        # remainder (everyone, when none is).
        takers = sorted(identity for identity in allocation if identity in acquiring) or sorted(allocation)
        if takers and free >= len(takers):
            for identity in takers:
                allocation[identity] += free // len(takers)
        allocation.update(held)
        return allocation

    async def _enforce(self, targets: dict[str, int], members) -> bool:
        """Decrease, confirm, then increase -- each raise within the cap less
        every other member's exposure -- then confirm what is in effect but
        unconfirmed. Returns whether every member is now proven."""
        self._error = None
        self._unproven = set()
        executors = self._executors()
        controllable = {}
        for identity in sorted(members):
            executor = executors.get(identity)
            if executor is None or not executor.capabilities.aggregate_bandwidth_ceiling:
                self._unproven.add(identity)
                self._error = self._error or _runtime_error(Category.UNSUPPORTED_CAPABILITY, identity)
            else:
                controllable[identity] = executor
        for identity, executor in controllable.items():
            current = self._exposure_of(identity)
            if current is _UNBOUNDED or targets[identity] < current:
                if not await self._apply(executor, targets[identity]):
                    self._unproven.add(identity)
        for identity, executor in controllable.items():
            current = self._exposure_of(identity)
            if current is _UNBOUNDED or targets[identity] <= current:
                continue
            others = [self._exposure_of(other) for other in members if other != identity]
            if any(value is _UNBOUNDED for value in others):
                continue                                    # nothing is lent against an unbounded exposure
            value = min(targets[identity], self._configured - sum(others))
            if value > current and not await self._apply(executor, value):
                self._unproven.add(identity)
        for identity, executor in controllable.items():
            if identity in self._unproven or self._confirmed.get(identity):
                continue
            current = self._exposure_of(identity)
            if current is _UNBOUNDED or not await self._apply(executor, current):
                self._unproven.add(identity)
        return not self._unproven and self._error is None

    async def admit(self, executor) -> bool:
        """May ``executor`` begin or resume DP-owned acquisition now?

        Raises a neutral UNSUPPORTED_CAPABILITY error when a finite cap can
        never be enforced through this executor; returns ``False`` while the
        required ceiling cannot currently be proven (admission is deferred)."""
        await self._seed()
        identity = executor.descriptor.id
        if self._configured and not executor.capabilities.aggregate_bandwidth_ceiling:
            raise TransferError(NormalizedError(
                Domain.EXECUTOR, Category.UNSUPPORTED_CAPABILITY, Stage.QUEUE, retryability=Retryability.NEVER,
                integration_id=identity,
            ))
        # Current truth, every time: a previously confirmed ceiling is never
        # authority for a new native side effect by itself.
        if await self._current(executor, ceiling=bool(self._configured)) is None:
            self._error = _runtime_error(integration_id=identity)
            return False
        if not self._configured:
            self._reserved.add(identity)
            return True
        if identity in self._reserved:
            if self._confirmed.get(identity):
                return True
            await self.converge()
            return bool(self._confirmed.get(identity))
        executors = self._executors()
        for other in sorted(self._reserved):
            reserved = executors.get(other)
            if reserved is None or not reserved.capabilities.aggregate_bandwidth_ceiling:
                # A reserved executor whose ceiling cannot be reduced: admitting
                # more acquisition could not be proven to respect the cap.
                self._error = _runtime_error(integration_id=other)
                return False
        members = self._reserved | {identity}
        # The newcomer has no live work: it draws nothing until its ceiling
        # is set, which is the last raise.
        self._exposure[identity] = 0
        self._confirmed.pop(identity, None)
        targets = self._plan(members, settling=False, newcomer=identity)
        if targets is None:
            # The cap cannot give one more executor an enforceable share.
            self._error = _runtime_error(Category.UNSUPPORTED_CAPABILITY, identity)
            return False
        if not await self._enforce(targets, members) or not self._confirmed.get(identity):
            return await self._abandon_newcomer()
        self._reserved.add(identity)
        return True

    async def _abandon_newcomer(self) -> bool:
        """A prospective admission failed after other ceilings may already have
        been reduced for it. The newcomer never joined the reserved set, so the
        cap is re-divided among the executors that DID. If that succeeds the
        current enforcement stays proven and only the newcomer is deferred; if
        it fails, truth is unproven and normal gating applies."""
        await self.converge()
        return False

    async def observe(self, facts: dict[str, ExecutorDemand]) -> None:
        """Facts from one reconcile cycle, per executor. Uncertainty never
        releases a reservation. A reservation change re-divides the cap; an
        unchanged set is rebalanced to the observed demand."""
        await self._seed()
        changed = False
        for identity, fact in facts.items():
            previous = self._facts.get(identity)
            if fact.certain and previous is not None and not previous.certain and identity in self._reserved:
                # Observable again after an outage (possibly a native restart
                # that dropped its ceiling): never lent against until re-confirmed.
                self._confirmed.pop(identity, None)
                self._exposure[identity] = _UNBOUNDED
                changed = True
            if fact.required:
                if identity not in self._reserved:
                    self._reserved.add(identity)
                    changed = True  # an unadmitted acquirer: ceilings must be recomputed
            elif fact.certain and identity in self._reserved:
                self._reserved.discard(identity)
                changed = True
        self._facts = dict(facts)
        if changed:
            await self.converge()
        elif self._configured:
            await self._rebalance()
        elif self._unproven:
            await self.converge()                           # retry lifting a ceiling to Unlimited

    async def _rebalance(self) -> None:
        targets = self._plan(self._reserved, settling=True)
        if targets is None:
            return
        step = max(1, self._configured // REBALANCE_DIVISOR)
        if all(self._confirmed.get(identity) and abs(target - self._confirmed[identity]) < step
               for identity, target in targets.items()):
            return
        await self._enforce(targets, set(self._reserved))
        await self._gate_unproven()

    async def release_absent(self, live: frozenset[str]) -> None:
        """Executors without any durable live native work hold no reservation."""
        await self._seed()
        absent = self._reserved - set(live)
        if absent:
            self._reserved -= absent
            await self.converge()

    async def converge(self) -> RuntimeLimitStatus:
        """Apply the current allocation to every reserved executor (or lift
        ceilings when unlimited) and report proven truth."""
        await self._seed()
        executors = self._executors()
        if not self._configured:
            self._error = None
            self._unproven = set()
            # Every executor reserved or ever controlled, unless Unlimited is
            # already confirmed there: one whose last ceiling went unconfirmed
            # may still enforce it, and stays unproven (retried) until lifted.
            for identity in sorted(self._reserved | set(self._confirmed) | set(self._exposure)):
                executor = executors.get(identity)
                if (self._confirmed.get(identity) == 0 or executor is None
                        or not executor.capabilities.aggregate_bandwidth_ceiling):
                    continue
                if not await self._apply(executor, 0):
                    self._unproven.add(identity)
            await self._gate_unproven()
            return self.status()
        targets = self._plan(self._reserved, settling=False)
        if targets is None:
            # The cap cannot give every reserved executor an enforceable
            # ceiling: no executor is ever told 0 (unlimited). Enforcement is
            # unproven and acquisition is gated where the neutral gate exists.
            self._unproven = set(self._reserved)
            self._error = _runtime_error(Category.UNSUPPORTED_CAPABILITY)
            await self._gate_unproven()
            return self.status()
        await self._enforce(targets, set(self._reserved))
        await self._gate_unproven()
        return self.status()

    async def _gate(self, executor, paused: bool) -> bool:
        if not executor.capabilities.acquisition_gate:
            return False
        try:
            health = await executor.health()
            if ExecutorRuntimeCapability.ACQUISITION_GATE not in health.available_runtime_capabilities:
                return False
            result = await executor.set_acquisition_paused(paused)
        except Exception:
            return False
        return (isinstance(result, ExecutorGateResult) and result.error is None
                and result.effective_paused is paused)

    async def _gate_unproven(self) -> None:
        """Hold acquisition gates on reserved executors whose ceiling is
        unprovable under a finite cap; release them once it is provable again
        (unless global pause intent still wants them engaged)."""
        executors = self._executors()
        wanted = self._unproven if self._configured else set()
        for identity in sorted(wanted - self._gated):
            executor = executors.get(identity)
            if executor is not None and await self._gate(executor, True):
                self._gated.add(identity)
        release = self._gated - wanted
        if release and not await self._repository.globally_paused():
            for identity in sorted(release):
                executor = executors.get(identity)
                if executor is None or await self._gate(executor, False):
                    self._gated.discard(identity)

    def status(self) -> RuntimeLimitStatus:
        proven = self._error is None and not self._unproven
        if proven and self._configured:
            ceilings = [self._confirmed.get(identity) for identity in self._reserved]
            proven = all(ceilings) and sum(ceilings) <= self._configured
        error = None
        if self._error is not None:
            error = self._error.message or self._error.category.value
        return RuntimeLimitStatus(self._configured, self._configured if proven else None, error)
