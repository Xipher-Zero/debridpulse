"""Explicit aria2 administration boundary, separate from transfer policy."""
from __future__ import annotations

import time

from core.config import get_settings
from executors.aria2.runtime import runtime, is_builtin_mode, aria2_global_options


class Aria2Administration:
    def __init__(self, executor, repository, application):
        self.executor = executor
        self.repository = repository
        self.application = application
        self._last_housekeeping = 0.0
        self._last_rotation = 0.0

    @property
    def client(self):
        return self.executor.client

    async def get_global_stat(self):
        return await self.client.get_global_stat()

    async def get_active(self):
        return await self.client.get_active()

    async def get_all(self, *args, **kwargs):
        return await self.client.get_all(*args, **kwargs)

    async def _owned(self):
        result = {}
        for attempt in await self.repository.executions():
            handle = attempt.handle
            if (handle.executor_id == self.executor.descriptor.id
                    and handle.context.get("binding") == self.executor.binding
                    and await self.repository.authorize_execution(handle, "observe")):
                result[str(handle.context.get("gid") or "")] = attempt
        return result

    async def filter_owned(self, downloads):
        owned = await self._owned()
        result = []
        for download in downloads:
            attempt = owned.get(download.gid)
            if attempt:
                try:
                    self.executor._observation(attempt.handle, download)
                except Exception:
                    continue
                result.append(download)
        return result

    async def control(self, gid, action):
        attempt = (await self._owned()).get(str(gid))
        if attempt is None:
            raise PermissionError("Execution is not owned by this application")
        # Translate the native UI identity once. Durable pause/resume and
        # cancellation intent remain commands of the universal owner.
        if action == "pause":
            return await self.application.pause(attempt.transfer_id)
        if action == "resume":
            return await self.application.resume(attempt.transfer_id)
        if action == "remove":
            async with self.application.application_operation():
                await self.application.engine.cancel_artifact(attempt.transfer_id, attempt.artifact_id)
                await self.application._publish(attempt.transfer_id)
            return {"ok": True}
        raise ValueError("Unsupported execution action")

    def rpc_metrics(self):
        return self.client.rpc_metrics()

    async def get_global_options(self):
        return await self.client.get_global_options()

    async def change_global_options(self, options):
        if not is_builtin_mode():
            raise PermissionError("Global aria2 options are read-only in external mode")
        return await self.client.change_global_options(options)

    async def memory_diagnostics(self):
        cfg = self.executor.configuration
        return await self.client.get_memory_diagnostics(waiting_limit=cfg.waiting_window, stopped_limit=cfg.stopped_window)

    async def test(self):
        return {**await self.client.test(), "diagnostics": await self.memory_diagnostics()}

    async def apply_memory_tuning(self):
        if not is_builtin_mode():
            return {"ok": True, "skipped": True, "reason": "External daemon policy is read-only"}
        options = aria2_global_options(get_settings(), include_safety=True)
        await self.client.change_global_options(options)
        return {"ok": True, "applied": options}

    async def housekeeping(self):
        await self.apply_memory_tuning()
        return {"ok": True, "reason": "Execution result history retained", "diagnostics": await self.memory_diagnostics()}

    async def start(self):
        await runtime.ensure_started()

    async def stop(self):
        await runtime.stop()

    async def maintain(self):
        cfg = get_settings()
        now = time.time()
        housekeeping_interval = max(0, cfg.aria2_purge_interval_minutes) * 60
        if housekeeping_interval and now - self._last_housekeeping >= housekeeping_interval:
            await self.housekeeping()
            self._last_housekeeping = now
        if not is_builtin_mode(cfg):
            return
        if now - self._last_rotation >= 900:
            await runtime.ensure_log_rotation()
            self._last_rotation = now
        interval = max(0, cfg.aria2_restart_interval_hours) * 3600
        if interval and runtime._started_at > 0 and now - runtime._started_at >= interval:
            # Windowed waiting/stopped lists cannot prove daemon idleness.
            stat = await self.client.get_global_stat()
            if stat["active"] or stat["waiting"]:
                return
            downloads = await self.client.get_all()
            if not any(item.status in {"active", "waiting"} for item in downloads):
                await runtime.restart()
