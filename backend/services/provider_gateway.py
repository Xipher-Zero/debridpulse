"""Provider boundary for the V1 AllDebrid implementation.

Provider-specific network/materialization behavior still lives in the inherited
engine, but every application-visible provider operation is enumerated here so
callers cannot bypass the provider boundary through a transparent fallback.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from db.database import get_db


class ProviderGateway:
    def __init__(self, engine):
        self.engine = engine
        self._activity = asyncio.Condition()
        self._active_operations = 0
        self._quiescing = False

    @asynccontextmanager
    async def _operation(self):
        async with self._activity:
            if self._quiescing:
                raise RuntimeError("Provider operations are quiesced for database maintenance")
            self._active_operations += 1
        try:
            yield
        finally:
            async with self._activity:
                self._active_operations -= 1
                if self._active_operations == 0:
                    self._activity.notify_all()

    async def _prime_deleted_transfer_tombstones(self) -> None:
        """Restore durable Delete authority after a process restart.

        The runtime guard uses an in-process tombstone/event to stop work that
        was already in flight when Delete was requested.  No such old task can
        survive a process restart, but provider inventory reconciliation can
        still encounter the persisted deleted row and the inherited importer
        historically revived it.  Prime the guard from SQLite immediately
        before inventory/import work so persisted ``status='deleted'`` remains
        authoritative across restarts.
        """
        deleted_ids: list[int] = []
        async with get_db() as db:
            rows = await db.fetchall(
                "SELECT id FROM torrents WHERE status='deleted'"
            )
        for row in rows:
            try:
                deleted_ids.append(int(row["id"]))
            except (KeyError, TypeError, ValueError):
                continue

        tombstones = getattr(self.engine, "_deleted_transfer_ids", None)
        delete_event = getattr(self.engine, "_delete_event", None)
        if tombstones is None or delete_event is None:
            return
        tombstones.update(deleted_ids)
        for transfer_id in deleted_ids:
            delete_event(transfer_id).set()

    async def begin_quiescence(self) -> None:
        """Block new provider operations and wait for existing operations to drain."""
        claimed = False
        try:
            async with self._activity:
                if self._quiescing:
                    raise RuntimeError("Provider quiescence is already active")
                self._quiescing = True
                claimed = True
                while self._active_operations:
                    await self._activity.wait()
        except BaseException:
            if claimed:
                async with self._activity:
                    self._quiescing = False
                    self._activity.notify_all()
            raise

    async def end_quiescence(self) -> None:
        async with self._activity:
            self._quiescing = False
            self._activity.notify_all()

    @property
    def quiescing(self) -> bool:
        return self._quiescing

    async def get_magnet_files(self, magnet_ids):
        async with self._operation():
            return await self.engine.ad().get_magnet_files(magnet_ids)

    async def sync_status(self):
        async with self._operation():
            return await self.engine.sync_alldebrid_status()

    async def reconcile_inventory(self):
        async with self._operation():
            await self._prime_deleted_transfer_tombstones()
            return await self.engine.reconcile_provider_inventory()

    async def import_existing(self):
        async with self._operation():
            await self._prime_deleted_transfer_tombstones()
            return await self.engine.import_existing_magnets()

    async def full_sync(self):
        async with self._operation():
            return await self.engine.full_alldebrid_sync()

    async def add_magnet(self, magnet: str, source: str = "manual"):
        async with self._operation():
            return await self.engine.add_magnet_direct(magnet, source=source)

    async def add_torrent_file(self, *args, **kwargs):
        async with self._operation():
            return await self.engine.add_torrent_file_direct(*args, **kwargs)

    async def add_direct_links(self, links):
        async with self._operation():
            return await self.engine.add_direct_links(links)

    async def retry_direct_link_collection(self, transfer_id: int):
        async with self._operation():
            return await self.engine.retry_direct_link_collection(int(transfer_id))

    async def cleanup_no_peer_errors(self):
        async with self._operation():
            return await self.engine.cleanup_no_peer_errors()

    async def cleanup_orphans(self):
        async with self._operation():
            return await self.engine.cleanup_alldebrid_orphans()

    async def cleanup_stuck(self):
        async with self._operation():
            return await self.engine.cleanup_stuck_downloads()

    async def test(self):
        async with self._operation():
            return await self.engine.ad().get_user()
