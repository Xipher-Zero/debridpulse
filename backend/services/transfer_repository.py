"""Persistence boundary for transfer orchestration."""
from __future__ import annotations

from typing import Iterable

from db.database import get_db


class TransferRepository:
    async def get_transfer(self, transfer_id: int):
        async with get_db() as db:
            return await db.fetchone("SELECT * FROM torrents WHERE id=?", (int(transfer_id),))

    async def paused_sibling_ids(self, transfer_id: int) -> list[int]:
        async with get_db() as db:
            rows = await db.fetchall(
                "SELECT id FROM torrents WHERE status='paused' AND id!=? ORDER BY id",
                (int(transfer_id),),
            )
        return [int(row["id"]) for row in rows]

    async def parent_progress_rows(self):
        """Return only rows that can represent physical aria2 progress.

        A retained pre-dispatch error with neither a local target nor an aria2
        GID is a source/provider outcome, not physical delivery work.  Excluding
        it here keeps the bound state-machine path on the same contract as the
        direct-link result guard.  Genuine aria2 failures retain a local target
        and/or GID and therefore remain in the physical denominator.
        """
        async with get_db() as db:
            return await db.fetchall(
                """SELECT t.id AS torrent_id, t.status AS torrent_status,
                          t.progress AS torrent_progress, f.id AS file_id,
                          f.status AS file_status, f.size_bytes, f.download_id
                     FROM torrents t JOIN download_files f ON f.torrent_id=t.id
                    WHERE t.download_client='aria2'
                      AND t.status IN ('queued','downloading','paused')
                      AND f.download_client='aria2' AND f.blocked=0
                      AND f.status!='missing'
                      AND NOT (
                          f.status='error'
                          AND f.local_path IS NULL
                          AND f.download_id IS NULL
                      )
                    ORDER BY t.id,f.id"""
            )

    async def persist_parent_progress(self, updates) -> None:
        """Persist derived parent progress/status updates as one batch."""
        updates = list(updates or [])
        if not updates:
            return
        async with get_db() as db:
            await db.executemany(
                """UPDATE torrents SET progress=?,status=?,updated_at=CURRENT_TIMESTAMP
                     WHERE id=? AND status IN ('queued','downloading','paused')""",
                updates,
            )
            await db.commit()

    async def persist_pause_transition(
        self,
        target_id: int,
        sibling_ids: Iterable[int],
        *,
        target_paused: bool,
    ) -> None:
        async with get_db() as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS transfer_pause_intents (
                       torrent_id INTEGER PRIMARY KEY,
                       paused INTEGER NOT NULL DEFAULT 1,
                       updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                   )"""
            )
            for sibling_id in sibling_ids:
                await db.execute(
                    """INSERT INTO transfer_pause_intents (torrent_id, paused, updated_at)
                       VALUES (?, 1, CURRENT_TIMESTAMP)
                       ON CONFLICT(torrent_id) DO UPDATE SET paused=1, updated_at=CURRENT_TIMESTAMP""",
                    (int(sibling_id),),
                )
            await db.execute(
                """INSERT INTO transfer_pause_intents (torrent_id, paused, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(torrent_id) DO UPDATE SET paused=excluded.paused,
                       updated_at=CURRENT_TIMESTAMP""",
                (int(target_id), 1 if target_paused else 0),
            )
            await db.commit()

    async def has_unintended_paused_children(self, pause_intents: set[int]) -> bool:
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT DISTINCT f.torrent_id
                     FROM download_files f
                     JOIN torrents t ON t.id=f.torrent_id
                    WHERE f.download_client='aria2'
                      AND f.blocked=0
                      AND f.status='paused'
                      AND t.status NOT IN ('completed','deleted','error')"""
            )
        return any(int(row["torrent_id"]) not in pause_intents for row in rows)
