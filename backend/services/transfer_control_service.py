"""Explicit durable operator-control service; no runtime method replacement."""
from __future__ import annotations

import logging

from core.config import apply_settings, get_settings, save_settings
from core.logging_utils import sanitize_exception
from services.dispatch_coordinator import MirrorAwareTransferControlCoordinator

logger = logging.getLogger("debridpulse.control")


class TransferControlService:
    def __init__(self, engine, repository, state_machine):
        self.engine = engine
        self.repository = repository
        self.coordinator = MirrorAwareTransferControlCoordinator(engine)
        self.coordinator._orig_parent_progress = state_machine.aggregate_parent_progress

    @property
    def pause_intents(self) -> set[int]:
        return self.coordinator._pause_intents

    async def ensure_initialized(self):
        return await self.coordinator.ensure_initialized()

    def reset_runtime_state(self) -> None:
        """Drop cached intent state so the next use reloads the authoritative DB."""
        self.coordinator._pause_intents.clear()
        self.coordinator._lost_strikes.clear()
        self.coordinator._initialized = False

    def _set_global_paused(self, paused: bool) -> None:
        cfg = get_settings()
        if bool(cfg.paused) == bool(paused):
            return
        new_cfg = cfg.model_copy(update={"paused": bool(paused)})
        save_settings(new_cfg)
        apply_settings(new_cfg)

    async def _persist_transition(self, transfer_id: int, sibling_ids: list[int], *, target_paused: bool):
        await self.repository.persist_pause_transition(
            transfer_id, sibling_ids, target_paused=target_paused
        )
        self.pause_intents.update(sibling_ids)
        if target_paused:
            self.pause_intents.add(int(transfer_id))
        else:
            self.pause_intents.discard(int(transfer_id))

    async def pause_transfer(self, transfer_id: int):
        return await self.coordinator.pause_torrent(int(transfer_id))

    async def resume_transfer(self, transfer_id: int):
        transfer_id = int(transfer_id)
        await self.ensure_initialized()
        if not bool(get_settings().paused):
            result = await self.coordinator.resume_torrent(transfer_id)
            await self.engine.resume_deferred_provider_submissions()
            return result

        released_while_waiting = False
        sibling_ids: list[int] = []
        result = None
        async with self.engine._aria2_state_lock:
            if not bool(get_settings().paused):
                released_while_waiting = True
            else:
                target = await self.repository.get_transfer(transfer_id)
                if not target:
                    raise ValueError("Transfer not found")
                if str(target.get("status") or "") != "paused":
                    raise ValueError("Transfer is not paused")
                sibling_ids = await self.repository.paused_sibling_ids(transfer_id)
                await self._persist_transition(transfer_id, sibling_ids, target_paused=False)
                try:
                    self._set_global_paused(False)
                except Exception:
                    await self._persist_transition(transfer_id, sibling_ids, target_paused=True)
                    raise
                try:
                    result = await self.coordinator._resume_parent(transfer_id)
                except Exception:
                    await self._persist_transition(transfer_id, sibling_ids, target_paused=True)
                    try:
                        await self.coordinator._pause_parent(transfer_id, strict=False)
                    except Exception as pause_exc:
                        logger.warning("Could not re-park transfer %s: %s", transfer_id,
                                       sanitize_exception(pause_exc, max_length=180))
                    try:
                        self._set_global_paused(True)
                    except Exception as restore_exc:
                        logger.error("Could not restore Pause All: %s",
                                     sanitize_exception(restore_exc, max_length=180))
                    raise
        if released_while_waiting:
            return await self.coordinator.resume_torrent(transfer_id)
        await self.engine._log_event(
            transfer_id, "info",
            "Global Pause All converted to selective pause; resumed this transfer while "
            f"{len(sibling_ids)} other paused transfer(s) remain parked",
        )
        await self.engine.resume_deferred_provider_submissions()
        self.coordinator._schedule_queue()
        return result

    async def pause_all(self):
        self._set_global_paused(True)
        return await self.coordinator.pause_all_downloads()

    async def resume_all(self):
        result = await self.coordinator.resume_all_downloads()
        self._set_global_paused(False)
        await self.engine.resume_deferred_provider_submissions()
        self.coordinator._schedule_queue()
        return result

    async def control_gid(self, *args, **kwargs):
        return await self.coordinator.control_aria2_gid(*args, **kwargs)

    async def confirm_gid(self, gid: str):
        return await self.coordinator.confirm_gid(gid)

    async def start_download(self, *args, **kwargs):
        return await self.coordinator.start_download(*args, **kwargs)

    async def download(self, *args, **kwargs):
        return await self.coordinator.download(*args, **kwargs)

    async def reset_for_redownload(self, *args, **kwargs):
        return await self.coordinator.reset_for_redownload(*args, **kwargs)

    async def update_parent_progress(self, *args, **kwargs):
        return await self.coordinator.update_parent_progress(*args, **kwargs)

    async def enforce_global_pause(self):
        return await self.coordinator._enforce_global_pause()

    async def enforce_selective_pauses(self):
        return await self.coordinator._enforce_selective_pauses()

    async def resume_unintended_paused(self):
        return await self.coordinator._resume_unintended_paused()
