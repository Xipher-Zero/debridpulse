"""Application mutation/execution admission gate for destructive maintenance."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class ApplicationMaintenanceActive(RuntimeError):
    """Raised when new application work is attempted during maintenance."""


class ApplicationMaintenanceGate:
    """Drain admitted work, then reject new mutation/execution operations.

    Admission closes before maintenance waits for already-admitted operations.
    A task admitted before closure may finish nested/reentrant service calls; a
    different task cannot enter until maintenance releases the gate. The
    maintenance owner itself may call gated operations needed to quiesce state.
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active_operations = 0
        self._depths: dict[asyncio.Task, int] = {}
        self._maintenance_active = False
        self._owner: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self._maintenance_active

    @asynccontextmanager
    async def operation(self):
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("Application operation requires an asyncio task")
        owner = current is self._owner
        counted = False
        async with self._condition:
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
                if self._maintenance_active:
                    raise ApplicationMaintenanceActive("Application maintenance is already in progress")
                self._maintenance_active = True
                self._owner = current
                claimed = True
                # A settings operation can upgrade its own admission while
                # draining other operations; it must not wait for itself.
                own_operation = 1 if self._depths.get(current, 0) else 0
                while self._active_operations > own_operation:
                    await self._condition.wait()
            yield
        finally:
            if claimed:
                async with self._condition:
                    if self._owner is current:
                        self._owner = None
                        self._maintenance_active = False
                        self._condition.notify_all()
