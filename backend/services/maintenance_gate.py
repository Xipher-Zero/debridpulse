"""Application mutation/execution admission gate for destructive maintenance."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class ApplicationMaintenanceActive(RuntimeError):
    """Raised when new application work is attempted during maintenance."""


class ApplicationSealed(ApplicationMaintenanceActive):
    """Raised for all application work after an unresolved restore sealed admission."""


class ApplicationMaintenanceBusy(ApplicationMaintenanceActive):
    """Maintenance was NOT entered: work admitted before it (an upload still
    streaming its body, a long request) did not finish within the drain bound.
    Admission reopened as it was; nothing of the maintenance ran, and nothing
    in flight was interrupted. The operator may try again once it finishes."""

    def __init__(self, waiting: int, seconds: float) -> None:
        super().__init__(f"{waiting} running operation(s) did not finish within {seconds:g} s")
        self.waiting = waiting
        self.seconds = seconds


# How long maintenance waits for already-admitted work to finish. While it
# waits, admission is closed -- new work and the engine's own passes are held --
# so the wait has an end: past it, maintenance gives up and admission reopens.
MAINTENANCE_DRAIN_SECONDS = 60.0


class ApplicationMaintenanceGate:
    """Drain admitted work, then reject new mutation/execution operations.

    Admission closes before maintenance waits for already-admitted operations.
    A task admitted before closure may finish nested/reentrant service calls; a
    different task cannot enter until maintenance releases the gate. The
    maintenance owner itself may call gated operations needed to quiesce state.
    """

    def __init__(self, *, drain_seconds: float = MAINTENANCE_DRAIN_SECONDS) -> None:
        self._drain_seconds = drain_seconds
        self._condition = asyncio.Condition()
        self._active_operations = 0
        self._depths: dict[asyncio.Task, int] = {}
        self._maintenance_active = False
        self._owner: asyncio.Task | None = None
        self._sealed = False

    @property
    def active(self) -> bool:
        return self._maintenance_active

    @property
    def sealed(self) -> bool:
        return self._sealed

    def seal(self) -> None:
        """Refuse every operation and maintenance for the rest of this
        process: a restore left the stored state neither restored nor
        reversed, so no settings write, restore or other work may act on it
        before the next start reverses it."""
        self._sealed = True

    @asynccontextmanager
    async def operation(self):
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("Application operation requires an asyncio task")
        owner = current is self._owner
        counted = False
        async with self._condition:
            if self._sealed:
                raise ApplicationSealed("Stored state is awaiting restore reversal at the next start")
            depth = self._depths.get(current, 0)
            if self._maintenance_active and not owner and depth == 0:
                raise ApplicationMaintenanceActive("Application maintenance is in progress")
            if not owner:
                if depth == 0:
                    self._active_operations += 1
                    counted = True
                self._depths[current] = depth + 1
        try:
            yield
        finally:
            if not owner:
                async with self._condition:
                    depth = self._depths.get(current, 0)
                    if depth <= 1:
                        self._depths.pop(current, None)
                        if counted:
                            self._active_operations = max(0, self._active_operations - 1)
                            self._condition.notify_all()
                    else:
                        self._depths[current] = depth - 1

    @asynccontextmanager
    async def maintenance(self):
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("Application maintenance requires an asyncio task")
        claimed = False
        try:
            async with self._condition:
                if self._sealed:
                    raise ApplicationSealed("Stored state is awaiting restore reversal at the next start")
                if self._maintenance_active:
                    raise ApplicationMaintenanceActive("Application maintenance is already in progress")
                self._maintenance_active = True
                self._owner = current
                claimed = True
                # A settings operation can upgrade its own admission while
                # draining other operations; it must not wait for itself.
                own_operation = 1 if self._depths.get(current, 0) else 0
                try:
                    await asyncio.wait_for(
                        self._condition.wait_for(lambda: self._active_operations <= own_operation),
                        self._drain_seconds)
                except TimeoutError:
                    # Not entered: the claim is released below (``finally``)
                    # before anything of the maintenance has run.
                    raise ApplicationMaintenanceBusy(
                        max(1, self._active_operations - own_operation), self._drain_seconds) from None
            yield
        finally:
            if claimed:
                async with self._condition:
                    if self._owner is current:
                        self._owner = None
                        self._maintenance_active = False
                        self._condition.notify_all()
