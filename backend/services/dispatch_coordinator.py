"""Slot-aware download dispatch coordinator."""
from __future__ import annotations

import logging
from collections import defaultdict
from urllib.parse import urlsplit

from db.database import get_db
from services.manager_v2 import DIRECT_LINK_SOURCE


logger = logging.getLogger("debridpulse.dispatch")


_PRIMARY_STATUS_PRIORITY = {
    "completed": 0,
    "downloading": 1,
    "queued": 2,
    "paused": 3,
    "pending": 4,
}


def _source_host(value: object) -> str:
    """Return a stable host label for mirror comparison without retaining URLs."""
    try:
        host = str(urlsplit(str(value or "")).hostname or "").rstrip(".").casefold()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _mirror_identity(row: dict) -> tuple[str, int] | None:
    """Return conservative provider-metadata identity for one resolved file.

    AllDebrid direct-link metadata does not provide a content checksum. Mirror
    collapsing therefore requires both the resolved filename and an exact,
    non-zero byte size. Unknown-size files are intentionally never deduplicated.
    """
    filename = str(row.get("filename") or "").strip().casefold()
    try:
        size_bytes = int(row.get("size_bytes") or 0)
    except (TypeError, ValueError):
        return None
    if not filename or size_bytes <= 0:
        return None
    return filename, size_bytes


def plan_direct_link_mirror_suppression(rows) -> list[tuple[dict, dict]]:
    """Choose pending cross-hoster mirrors that must not become separate jobs.

    A logical payload is collapsed only when different source hosts resolved to
    the same filename and exact non-zero byte size. An already-running or
    completed child is preferred as the primary so this routine never cancels
    work merely to enforce deduplication. Same-hoster URL variants remain
    distinct because provider metadata alone is not strong enough to prove that
    they are mirrors rather than separate objects.
    """
    grouped: dict[tuple[int, str, int], list[dict]] = defaultdict(list)
    for raw in rows or []:
        row = dict(raw)
        identity = _mirror_identity(row)
        host = _source_host(row.get("source_url"))
        if identity is None or not host:
            continue
        try:
            torrent_id = int(row.get("torrent_id"))
        except (TypeError, ValueError):
            continue
        row["_mirror_host"] = host
        grouped[(torrent_id, identity[0], identity[1])].append(row)

    suppress: list[tuple[dict, dict]] = []
    for group in grouped.values():
        if len({row["_mirror_host"] for row in group}) < 2:
            continue
        group.sort(
            key=lambda row: (
                _PRIMARY_STATUS_PRIORITY.get(str(row.get("status") or ""), 99),
                int(row.get("file_id") or 0),
            )
        )
        primary = group[0]
        primary_host = primary["_mirror_host"]
        for candidate in group[1:]:
            if candidate["_mirror_host"] == primary_host:
                continue
            if str(candidate.get("status") or "") != "pending":
                continue
            if str(candidate.get("download_id") or "").strip():
                continue
            suppress.append((candidate, primary))
    return suppress


class DispatchCoordinator:
    def __init__(self, engine, control, ownership):
        self.engine = engine
        self.control = control
        self.ownership = ownership

    async def _collapse_direct_link_mirrors(self) -> int:
        """Collapse resolved mirror URLs before any new aria2 job is created.

        The parent retains the original submitted URL list, so retry/history
        still knows every mirror. Duplicate child rows are removed only while
        they are still pending and have no GID. This makes the database model
        one logical downloadable file rather than one file per working hoster.
        """
        async with get_db() as db:
            rows = await db.fetchall(
                """SELECT f.id AS file_id, f.torrent_id, f.filename,
                          f.size_bytes, f.source_url, f.status, f.download_id
                     FROM download_files f
                     JOIN torrents t ON t.id=f.torrent_id
                    WHERE t.source=?
                      AND t.status NOT IN ('completed','deleted','error')
                      AND f.blocked=0
                      AND f.status IN ('pending','queued','downloading','paused','completed')
                    ORDER BY f.torrent_id ASC, f.id ASC""",
                (DIRECT_LINK_SOURCE,),
            )
            plan = plan_direct_link_mirror_suppression(rows)
            if not plan:
                return 0

            removed_by_parent: dict[int, int] = defaultdict(int)
            groups_by_parent: dict[int, set[tuple[str, int]]] = defaultdict(set)
            for duplicate, primary in plan:
                cursor = await db.execute(
                    """DELETE FROM download_files
                         WHERE id=? AND status='pending' AND blocked=0
                           AND download_id IS NULL""",
                    (int(duplicate["file_id"]),),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) <= 0:
                    continue

                torrent_id = int(duplicate["torrent_id"])
                removed_by_parent[torrent_id] += 1
                groups_by_parent[torrent_id].add(
                    (
                        str(primary.get("filename") or "").casefold(),
                        int(primary.get("size_bytes") or 0),
                    )
                )
                logger.info(
                    "direct-link mirror suppressed: transfer=%s file=%s size=%s "
                    "primary_host=%s alternate_host=%s",
                    torrent_id,
                    str(primary.get("filename") or "")[:120],
                    int(primary.get("size_bytes") or 0),
                    primary.get("_mirror_host"),
                    duplicate.get("_mirror_host"),
                )

            for torrent_id, removed in removed_by_parent.items():
                if removed <= 0:
                    continue
                size_row = await db.fetchone(
                    """SELECT COALESCE(SUM(size_bytes), 0) AS total
                         FROM download_files
                        WHERE torrent_id=? AND blocked=0""",
                    (torrent_id,),
                )
                logical_size = int((size_row or {}).get("total") or 0)
                await db.execute(
                    """UPDATE torrents
                          SET size_bytes=?, updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND status NOT IN ('completed','deleted','error')""",
                    (logical_size, torrent_id),
                )
                logical_files = len(groups_by_parent[torrent_id])
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                    (
                        torrent_id,
                        f"Suppressed {removed} cross-hoster mirror link(s) for "
                        f"{logical_files} logical file(s); one copy will be downloaded",
                    ),
                )
            await db.commit()
            return sum(removed_by_parent.values())

    async def dispatch_queue(self, snapshot=None):
        await self._collapse_direct_link_mirrors()
        return await self.control.coordinator.dispatch_queue(snapshot)

    async def advance_queue_locked(self, *args, **kwargs):
        return await self.control.coordinator.advance_queue_locked(*args, **kwargs)

    def schedule_ready_parent(self, *args, **kwargs):
        return self.control.coordinator.schedule_ready_parent(*args, **kwargs)
