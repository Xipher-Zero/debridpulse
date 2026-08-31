"""Capability-oriented aria2 boundary.

Read-only observation is available for either built-in or external aria2. Any
per-GID mutation passes the DebridPulse ownership ledger first when the daemon
is shared, while daemon-global mutation remains built-in-only.
"""
from __future__ import annotations

from services.aria2_runtime import is_builtin_mode


class Aria2Gateway:
    def __init__(self, engine, ownership, recovery=None):
        self.engine = engine
        self.ownership = ownership
        self.recovery = recovery

    async def _require_owned_mutation(self, gid: str) -> str:
        normalized = str(gid or "").strip()
        if not normalized:
            raise ValueError("aria2 GID is required")
        if not is_builtin_mode() and not await self.ownership.owns(normalized):
            raise PermissionError(f"aria2 GID {normalized} is not owned by DebridPulse")
        return normalized

    async def raw_snapshot(self):
        return await self.engine._engine_aria2_get_all()

    async def get_global_stat(self):
        return await self.engine.aria2().get_global_stat()

    async def get_active(self):
        return await self.engine.aria2().get_active()

    async def get_all(self, *args, **kwargs):
        return await self.engine.aria2().get_all(*args, **kwargs)

    async def get_owned(self, *args, **kwargs):
        downloads = await self.get_all(*args, **kwargs)
        return await self.ownership.filter_owned(downloads)

    def rpc_metrics(self):
        return self.engine.aria2().rpc_metrics()

    async def get_global_options(self):
        return await self.engine.aria2().get_global_options()

    async def change_global_options(self, options):
        if not is_builtin_mode():
            raise PermissionError("Global aria2 options are read-only in external mode")
        return await self.engine.aria2().change_global_options(options)

    async def status(self, gid: str):
        return await self.engine.aria2().tell_status(str(gid or "").strip())

    async def pause(self, gid: str):
        normalized = await self._require_owned_mutation(gid)
        return await self.engine.aria2().pause(normalized)

    async def resume(self, gid: str):
        normalized = await self._require_owned_mutation(gid)
        return await self.engine.aria2().resume(normalized)

    async def remove_owned(self, gid: str):
        normalized = await self._require_owned_mutation(gid)
        return await self.engine._remove_owned_aria2_gid(normalized)

    async def test(self):
        return await self.engine.test_aria2()

    async def memory_diagnostics(self):
        return await self.engine._aria2_get_memory_diagnostics()

    async def housekeeping(self):
        """Reapply engine tuning without deleting bounded stopped-result state."""
        if not is_builtin_mode():
            return {
                "ok": True,
                "reason": "external aria2 history is daemon-owned",
                "diagnostics": await self.memory_diagnostics(),
            }
        await self.apply_memory_tuning()
        return {
            "ok": True,
            "reason": "bounded built-in aria2 result state retained",
            "diagnostics": await self.memory_diagnostics(),
        }

    async def deep_sync(self):
        """Run the same bounded retry owner used by normal reconciliation."""
        if self.recovery is None:
            return await self.engine.deep_sync_aria2_finished()
        async with self.engine._aria2_state_lock:
            return await self.recovery.run()

    async def disk_guard(self):
        return await self.engine.check_disk_space_guard()

    async def advance_queue(self):
        return await self.engine.advance_aria2_queue()

    async def apply_memory_tuning(self):
        return await self.engine.apply_aria2_memory_tuning()

    @property
    def exclusive(self) -> bool:
        return is_builtin_mode()
