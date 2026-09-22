"""Core-owned executor runtime limits: the one global download-bandwidth allocator.

``execution_runtime_limits.max_download_bytes_per_second`` (0 = unlimited) is
the single desired value. This owner splits it equally across the executors
that currently hold a bandwidth reservation -- no transfer-count weighting, no
demand estimation, no borrowing of unused share -- and asks each executor, in
neutral language, to enforce only the ceiling assigned to it. Executors
enforce; they never learn the global policy.

Safety rules:

* a reservation is released only on positive truth (a certain observation that
  no reservation is needed, or no durable live native work at all); an
  uncertain executor keeps its previous reservation and share;
* adding an executor under a finite cap shrinks every existing reserved
  executor first, confirms, then applies and confirms the newcomer's ceiling,
  and only then admits the newcomer's native acquisition;
* a finite cap admits new work only through an executor that statically
  declares aggregate ceiling enforcement AND currently reports it available;
  otherwise admission fails closed and the configured/effective divergence is
  reported rather than guessed;
* a reserved executor whose share can no longer be proven keeps that share
  (never redistributed); where it offers the neutral acquisition gate, the
  gate is engaged until the share is provable again -- never cancellation;
* a positive cap is never assigned as ``0`` (which means unlimited): when the
  cap cannot give every reserved executor a positive share, admission fails
  closed and enforcement is reported unproven;
* every admission re-reads the executor's CURRENT health: an unreachable or
  not-ready executor, or one whose ceiling capability is currently
  unavailable, is never admitted on the strength of an earlier confirmation.

Callers serialize every mutation under the engine's execution-admission lock.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.models import ExecutorGateResult, ExecutorRuntimeCapability, ExecutorRuntimeControlResult


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


def _runtime_error(category=Category.EXECUTOR_UNAVAILABLE, integration_id: str = "") -> NormalizedError:
    return NormalizedError(Domain.EXECUTOR, category, Stage.EXECUTION, retryability=Retryability.BACKOFF,
                           integration_id=integration_id)


class ExecutionRuntimeCoordinator:
    def __init__(self, registry: Callable[[], object], repository):
        self._registry = registry
        self._repository = repository
        self._configured = 0
        self._reserved: set[str] = set()
        self._seeded = False
        self._assigned: dict[str, int | None] = {}
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
    def gated(self) -> frozenset[str]:
        """Executors whose acquisition gate this owner holds engaged."""
        return frozenset(self._gated)

    def _executors(self):
        return self._registry().executors

    def _share(self, count: int) -> int | None:
        """The equal share for ``count`` reserved executors: ``0`` only for an
        unlimited cap; ``None`` when a finite cap cannot give each a positive
        enforceable share (never collapsed into 0 = unlimited)."""
        if not self._configured:
            return 0
        share = self._configured // max(1, count)
        return share if share >= 1 else None

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
        if self._configured and (not isinstance(ceiling, int) or ceiling < 1):
            # 0 means unlimited: never the enforcement of a finite cap.
            self._assigned[identity] = None
            self._error = _runtime_error(Category.UNSUPPORTED_CAPABILITY, identity)
            return False
        if not await self._available(executor):
            self._assigned[identity] = None
            self._error = _runtime_error(Category.UNSUPPORTED_CAPABILITY, identity)
            return False
        try:
            result = await executor.set_bandwidth_ceiling(ceiling)
        except Exception:
            result = None
        confirmed = (isinstance(result, ExecutorRuntimeControlResult) and result.error is None
                     and result.effective_bytes_per_second == ceiling)
        self._assigned[identity] = ceiling if confirmed else None
        if not confirmed:
            self._error = (result.error if isinstance(result, ExecutorRuntimeControlResult) and result.error
                           else _runtime_error(integration_id=identity))
        return confirmed

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
            share = self._share(len(self._reserved))
            if share is None:
                self._error = _runtime_error(Category.UNSUPPORTED_CAPABILITY, identity)
                return False
            return self._assigned.get(identity) == share or await self._apply(executor, share)
        share = self._share(len(self._reserved) + 1)
        if share is None:
            # The cap cannot give one more executor a positive share.
            self._error = _runtime_error(Category.UNSUPPORTED_CAPABILITY, identity)
            return False
        executors = self._executors()
        for other in sorted(self._reserved):
            reserved = executors.get(other)
            if reserved is None or not reserved.capabilities.aggregate_bandwidth_ceiling:
                # A reserved executor whose share cannot be reduced: admitting
                # more acquisition could not be proven to respect the cap.
                self._error = _runtime_error(integration_id=other)
                return False
            if self._assigned.get(other) != share and not await self._apply(reserved, share):
                return await self._abandon_newcomer()
        if not await self._apply(executor, share):
            return await self._abandon_newcomer()
        self._reserved.add(identity)
        return True

    async def _abandon_newcomer(self) -> bool:
        """A prospective admission failed after existing shares may already
        have been reduced for it. The newcomer never joined the reserved set,
        so the cap is re-divided among the executors that DID: converge the
        unchanged reserved set back to its own equal split. If that succeeds
        the current enforcement stays proven and only the newcomer is
        deferred; if it fails, truth is unproven and normal gating applies."""
        await self.converge()
        return False

    async def observe(self, facts: dict[str, tuple[bool, bool]]) -> None:
        """Reservation facts from one reconcile cycle: executor id ->
        (observation certain, some execution still requires a reservation).
        Uncertainty never releases a reservation."""
        await self._seed()
        released = False
        for identity, (certain, required) in facts.items():
            if required:
                if identity not in self._reserved:
                    self._reserved.add(identity)
                    released = True  # an unadmitted acquirer: shares must be recomputed
            elif certain and identity in self._reserved:
                self._reserved.discard(identity)
                released = True
        if released:
            await self.converge()

    async def release_absent(self, live: frozenset[str]) -> None:
        """Executors without any durable live native work hold no reservation."""
        await self._seed()
        absent = self._reserved - set(live)
        if absent:
            self._reserved -= absent
            await self.converge()

    async def converge(self) -> RuntimeLimitStatus:
        """Apply the current equal split to every reserved executor (or lift
        ceilings when unlimited) and report proven truth."""
        await self._seed()
        self._error = None
        self._unproven = set()
        executors = self._executors()
        if not self._configured:
            for identity, value in list(self._assigned.items()):
                executor = executors.get(identity)
                if value == 0 or executor is None:
                    continue
                if not await self._apply(executor, 0):
                    self._unproven.add(identity)
            await self._gate_unproven()
            return self.status()
        share = self._share(len(self._reserved))
        if share is None:
            # A finite cap below the reserved executor count: no executor is
            # ever told 0 (unlimited). Enforcement is unproven and acquisition
            # is gated where the neutral gate exists.
            self._unproven = set(self._reserved)
            self._error = _runtime_error(Category.UNSUPPORTED_CAPABILITY)
            await self._gate_unproven()
            return self.status()
        for identity in sorted(self._reserved):
            executor = executors.get(identity)
            if executor is None or not executor.capabilities.aggregate_bandwidth_ceiling:
                self._unproven.add(identity)
                self._error = self._error or _runtime_error(Category.UNSUPPORTED_CAPABILITY, identity)
                continue
            if not await self._apply(executor, share):
                self._unproven.add(identity)
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
        """Hold acquisition gates on reserved executors whose required share is
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
        error = None
        if self._error is not None:
            error = self._error.message or self._error.category.value
        return RuntimeLimitStatus(self._configured, self._configured if proven else None, error)
