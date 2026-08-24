"""Slot-aware download dispatch coordination and logical mirror collapse."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from urllib.parse import urlsplit

from db.database import get_db
from services.manager_v2 import DIRECT_LINK_SOURCE
from services.transfer_control import TransferControlCoordinator


logger = logging.getLogger("debridpulse.dispatch")


_PRIMARY_STATUS_PRIORITY = {
    "completed": 0,
    "downloading": 1,
    "queued": 2,
    "paused": 3,
    "pending": 4,
}

_SUPPRESSIBLE_MIRROR_STATUSES = frozenset({"pending", "queued", "paused"})
_MAX_MIRROR_SIZE_DELTA_BYTES = 512 * 1024 * 1024
_MAX_MIRROR_SIZE_DELTA_PER_MILLE = 1  # 0.1%


def _source_host(value: object) -> str:
    """Return a stable host label for mirror comparison without retaining URLs."""
    try:
        host = str(urlsplit(str(value or "")).hostname or "").rstrip(".").casefold()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _mirror_metadata(row: dict) -> tuple[str, int] | None:
    """Return conservative provider metadata for one resolved file.

    AllDebrid direct-link metadata does not provide a content checksum. Mirror
    collapsing therefore requires a resolved filename and a known, non-zero
    byte size. Size equivalence is evaluated separately because hosters can
    report small metadata differences for the same payload.
    """
    filename = str(row.get("filename") or "").strip().casefold()
    try:
        size_bytes = int(row.get("size_bytes") or 0)
    except (TypeError, ValueError):
        return None
    if not filename or size_bytes <= 0:
        return None
    return filename, size_bytes


def _mirror_size_variance(left: int, right: int) -> tuple[int, float]:
    """Return absolute byte delta and percent delta relative to the larger size."""
    left = int(left)
    right = int(right)
    delta = abs(left - right)
    larger = max(left, right)
    percent = (delta / larger * 100.0) if larger > 0 else 0.0
    return delta, percent


def _mirror_sizes_match(left: int, right: int) -> bool:
    """Return True only for tightly equivalent non-zero provider sizes.

    Real hosters may report slightly different metadata for an identical file.
    The relative <=0.1% bound is authoritative and scales with file size. A much
    larger absolute <=512 MiB ceiling exists only as a catastrophe guard for
    very large same-name payloads; it is not intended to govern normal matching.
    """
    try:
        left = int(left)
        right = int(right)
    except (TypeError, ValueError):
        return False
    if left <= 0 or right <= 0:
        return False
    delta = abs(left - right)
    if delta == 0:
        return True
    larger = max(left, right)
    return (
        delta <= _MAX_MIRROR_SIZE_DELTA_BYTES
        and delta * 1000 <= larger * _MAX_MIRROR_SIZE_DELTA_PER_MILLE
    )


def plan_direct_link_mirror_suppression(rows) -> list[tuple[dict, dict]]:
    """Choose unresolved cross-hoster mirrors that must not become jobs.

    A logical payload is collapsed only when different source hosts resolve to
    the same filename and tightly equivalent known sizes. An already-running or
    completed child is preferred as the primary so this routine never cancels
    work merely to enforce deduplication. Same-hoster URL variants remain
    distinct because provider metadata alone is not strong enough to prove that
    they are mirrors rather than separate objects.
    """
    grouped: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for raw in rows or []:
        row = dict(raw)
        metadata = _mirror_metadata(row)
        host = _source_host(row.get("source_url"))
        if metadata is None or not host:
            continue
        try:
            torrent_id = int(row.get("torrent_id"))
        except (TypeError, ValueError):
            continue
        row["_mirror_host"] = host
        row["_mirror_size"] = int(metadata[1])
        grouped[(torrent_id, metadata[0])].append(row)

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

        # Keep separate primaries for materially different same-name payloads.
        # Every candidate is compared against a primary, never chained through
        # another duplicate, so tolerance cannot compound across a group.
        primaries: list[dict] = []
        for candidate in group:
            matched_primary = None
            for primary in primaries:
                if candidate["_mirror_host"] == primary["_mirror_host"]:
                    continue
                if _mirror_sizes_match(
                    candidate["_mirror_size"],
                    primary["_mirror_size"],
                ):
                    matched_primary = primary
                    break

            if matched_primary is None:
                primaries.append(candidate)
                continue

            if str(candidate.get("status") or "") not in _SUPPRESSIBLE_MIRROR_STATUSES:
                primaries.append(candidate)
                continue
            if str(candidate.get("download_id") or "").strip():
                primaries.append(candidate)
                continue

            suppress.append((candidate, matched_primary))

    return suppress


async def collapse_direct_link_mirrors() -> int:
    """Classify resolved mirrors before any new aria2 job is created.

    The original submitted source row is retained for operator visibility and
    retry/history. Duplicate rows become a non-dispatchable ``duplicate`` state
    instead of being deleted. ``blocked`` is stored as NULL deliberately: all
    physical-transfer queries require ``blocked=0``, while the UI treats NULL as
    not user-filter-blocked and therefore does not render a misleading BLOCKED
    warning beside a mirror that succeeded upstream.
    """
    async with get_db() as db:
        rows = await db.fetchall(
            """SELECT f.id AS file_id, f.torrent_id, f.filename,
                      f.size_bytes, f.source_url, f.status, f.download_id,
                      f.mirror_group_id, f.mirror_state
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

        classified_by_parent: dict[int, int] = defaultdict(int)
        groups_by_parent: dict[int, set[tuple[str, int]]] = defaultdict(set)
        for duplicate, primary in plan:
            primary_host = str(primary.get("_mirror_host") or "")
            primary_size = int(primary.get("size_bytes") or 0)
            duplicate_size = int(duplicate.get("size_bytes") or 0)
            delta_bytes, delta_percent = _mirror_size_variance(
                primary_size,
                duplicate_size,
            )
            reason = (
                f"Duplicate mirror of {primary_host}; matching resolved filename; "
                f"size variance {delta_bytes} bytes ({delta_percent:.4f}%)"
                if primary_host
                else (
                    "Duplicate cross-hoster mirror; matching resolved filename; "
                    f"size variance {delta_bytes} bytes ({delta_percent:.4f}%)"
                )
            )
            group_id = int(primary.get("mirror_group_id") or primary["file_id"])
            await db.execute(
                """UPDATE download_files
                      SET mirror_group_id=?,
                          mirror_state=CASE
                              WHEN COALESCE(mirror_state, '') IN ('', 'standby') THEN 'active'
                              ELSE mirror_state
                          END,
                          updated_at=CURRENT_TIMESTAMP
                    WHERE id=?""",
                (group_id, int(primary["file_id"])),
            )
            cursor = await db.execute(
                """UPDATE download_files
                      SET status='duplicate', blocked=NULL, block_reason=?,
                          mirror_group_id=?, mirror_state='standby',
                          download_url=NULL, local_path=NULL,
                          updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                      AND status IN ('pending','queued','paused')
                      AND blocked=0 AND download_id IS NULL""",
                (reason, group_id, int(duplicate["file_id"])),
            )
            if int(getattr(cursor, "rowcount", 0) or 0) <= 0:
                continue

            torrent_id = int(duplicate["torrent_id"])
            classified_by_parent[torrent_id] += 1
            groups_by_parent[torrent_id].add(
                (
                    str(primary.get("filename") or "").casefold(),
                    primary_size,
                )
            )
            logger.info(
                "direct-link mirror duplicate: transfer=%s file=%s size=%s "
                "primary_host=%s alternate_host=%s alternate_size=%s "
                "delta_bytes=%s delta_percent=%.4f",
                torrent_id,
                str(primary.get("filename") or "")[:120],
                primary_size,
                primary_host,
                duplicate.get("_mirror_host"),
                duplicate_size,
                delta_bytes,
                delta_percent,
            )

        for torrent_id, classified in classified_by_parent.items():
            if classified <= 0:
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
                    f"Classified {classified} cross-hoster mirror link(s) as "
                    f"duplicates for {logical_files} logical file(s); one copy will be downloaded; "
                    "alternates retained as automatic failover standbys",
                ),
            )
        await db.commit()
        return sum(classified_by_parent.values())


class MirrorAwareTransferControlCoordinator(TransferControlCoordinator):
    """Make mirror classification part of every authoritative physical dispatch."""

    def __init__(self, manager) -> None:
        super().__init__(manager)
        self._mirror_dispatch_lock = asyncio.Lock()
        # Wrap the captured physical dispatch function itself so scheduler,
        # startup, resume and explicit queue kicks all cross the same
        # mirror-classification boundary.
        self._physical_dispatch = self._orig_dispatch
        self._orig_dispatch = self._dispatch_after_mirror_classification

    async def _dispatch_after_mirror_classification(self, all_downloads=None):
        async with self._mirror_dispatch_lock:
            await collapse_direct_link_mirrors()
            return await self._physical_dispatch(all_downloads)

    async def advance_queue_locked(self) -> int:
        # Direct-link preparation calls this even when Pause All is active. Run
        # classification immediately so duplicates are visible and parent size
        # is logical before the eventual resume/dispatch pass.
        async with self._mirror_dispatch_lock:
            await collapse_direct_link_mirrors()
        return await super().advance_queue_locked()


class DispatchCoordinator:
    def __init__(self, engine, control, ownership):
        self.engine = engine
        self.control = control
        self.ownership = ownership

    async def dispatch_queue(self, snapshot=None):
        return await self.control.coordinator.dispatch_queue(snapshot)

    async def advance_queue_locked(self, *args, **kwargs):
        return await self.control.coordinator.advance_queue_locked(*args, **kwargs)

    def schedule_ready_parent(self, *args, **kwargs):
        return self.control.coordinator.schedule_ready_parent(*args, **kwargs)
