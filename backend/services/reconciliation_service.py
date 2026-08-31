"""Single authoritative reconciliation service for scheduler and recovery."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import logging

from core.config import get_settings
from core.performance import async_timer, increment
from services.aria2_runtime import is_builtin_mode  # compatibility seam for tests/adapters

logger = logging.getLogger("debridpulse.reconciliation")
_cycle_snapshot: ContextVar[tuple[asyncio.Task, list] | None] = ContextVar(
    "debridpulse_reconcile_snapshot", default=None
)
_cycle_active: ContextVar[bool] = ContextVar("debridpulse_reconcile_active", default=False)


class ReconciliationService:
    def __init__(self, engine, repository, control, dispatch, ownership, recovery=None):
        self.engine = engine
        self.repository = repository
        self.control = control
        self.dispatch = dispatch
        self.ownership = ownership
        self.recovery = recovery
        self.confirmed_missing: set[str] = set()

    async def get_all(self):
        cached = _cycle_snapshot.get()
        current = asyncio.current_task()
        if cached is not None and cached[0] is current:
            increment("aria2.scheduler_snapshot_reuse")
            return list(cached[1])
        increment("aria2.scheduler_snapshot_fetch")
        async with async_timer("reconcile.snapshot"):
            return await self.engine._engine_aria2_get_all()

    async def _raw_snapshot(self):
        increment("aria2.scheduler_snapshot_fetch")
        async with async_timer("reconcile.snapshot"):
            snapshot = await self.engine._engine_aria2_get_all()
        present = {str(item.gid) for item in snapshot if str(getattr(item, "gid", "") or "")}
        recovered = self.confirmed_missing.intersection(present)
        if recovered:
            self.confirmed_missing.difference_update(recovered)
            increment("aria2.confirm_gid_cache_recovered", len(recovered))
        return snapshot

    async def confirm_gid(self, gid: str):
        normalized = str(gid or "").strip()
        increment("aria2.confirm_gid_calls")
        if _cycle_active.get() and normalized in self.confirmed_missing:
            increment("aria2.confirm_gid_cache_hits")
            return None
        try:
            async with async_timer("aria2.confirm_gid"):
                result = await self.control.confirm_gid(normalized)
        except Exception:
            increment("aria2.confirm_gid_errors")
            raise
        if _cycle_active.get():
            if result is None and normalized:
                self.confirmed_missing.add(normalized)
                increment("aria2.confirm_gid_missing")
            elif normalized:
                self.confirmed_missing.discard(normalized)
        return result

    async def reconcile(self):
        if self.engine.download_client_name() != "aria2":
            return
        await self.control.ensure_initialized()
        active_token = _cycle_active.set(True)
        try:
            async with self.engine._aria2_state_lock:
                snapshot = await self._raw_snapshot()
                owner = asyncio.current_task()
                snapshot_token = _cycle_snapshot.set((owner, snapshot)) if owner else None
                try:
                    async with async_timer("reconcile.sync_downloads"):
                        await self.engine.sync_aria2_downloads()
                finally:
                    if snapshot_token is not None:
                        _cycle_snapshot.reset(snapshot_token)

                # Pause All persists intent before it waits for this same state
                # lock. Read the operator gate only after the lock is ours and
                # after status reconciliation, otherwise a cycle that was
                # queued behind Pause All could act on a stale pre-lock value.
                globally_paused = bool(get_settings().paused)
                if globally_paused:
                    async with async_timer("reconcile.global_pause"):
                        await self.control.enforce_global_pause()
                    return

                if self.control.pause_intents:
                    async with async_timer("reconcile.selective_pause"):
                        await self.control.enforce_selective_pauses()
                    snapshot = await self._raw_snapshot()

                owned = await self.ownership.filter_owned(snapshot)
                limit = self.engine._aria2_slot_limit()
                live = [item for item in owned if item.status in {"active", "waiting"}]
                available = max(0, limit - len(live))
                async with async_timer("reconcile.resume_parked"):
                    should_resume = available > 0 and await self.repository.has_unintended_paused_children(
                        self.control.pause_intents
                    )
                    resumed = await self.control.resume_unintended_paused() if should_resume else 0
                if resumed:
                    snapshot = await self._raw_snapshot()

                async with async_timer("reconcile.dispatch"):
                    await self.dispatch.dispatch_queue(snapshot)
                async with async_timer("reconcile.ready_parent"):
                    await self.engine._schedule_ready_aria2_parents()
                if self.recovery is not None:
                    async with async_timer("reconcile.aria2_error_recovery"):
                        await self.recovery.run()
        finally:
            _cycle_active.reset(active_token)

        async with async_timer("reconcile.deferred_provider"):
            await self.engine.resume_deferred_provider_submissions()

        # Stopped result objects are inert, bounded aria2 runtime state. Keep
        # them available to the built-in engine escape hatch after DebridPulse
        # has reconciled the durable transfer record. Active orphan recovery is
        # handled by the normal sync/dispatch paths rather than result deletion.

    async def startup(self):
        await self.engine.reconcile_aria2_on_startup()
        await self.reconcile()

    async def recover(self):
        # Recovery is reconciliation, not a second competing state mutator.
        await self.reconcile()
        return {"reconciled": True}
