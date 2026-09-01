"""Manifest-preserving retry authority for direct-link collections.

Manual Retry repairs the existing logical collection in place. Completed files
are re-verified from the filesystem, live aria2 jobs are retained, and lost or
failed jobs are redispatched at their original canonical path so aria2 can use an
existing control sidecar. The legacy whole-collection rebuild remains available
only when no physical manifest was ever materialized.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse

from core.config import get_settings
from db.database import get_db
from providers.alldebrid.client import AllDebridAPIError
from services.direct_link_result_guard import DirectLinkResultGuardManager
from services.transfer_integrity import _EXISTING_PAYLOAD_STABILITY_SECONDS
from services.manager_v2 import (
    DIRECT_LINK_SOURCE,
    _direct_link_unlock_failure_prefix,
    _retry_async,
    _safe_persisted_error,
    direct_link_filename,
    normalize_direct_links,
    safe_name,
)


_LIVE_ARIA2_STATES = frozenset({"active", "waiting", "paused"})


class DirectLinkRetryGuardManager(DirectLinkResultGuardManager):
    """Make direct-link Retry a repair operation instead of a fresh transaction."""

    def _direct_link_retry_paused(self, torrent_id: int) -> bool:
        if self.is_paused():
            return True
        architecture = self._architecture
        control = (
            getattr(architecture, "control", None)
            if architecture is not None
            else None
        )
        try:
            return int(torrent_id) in set(
                getattr(control, "pause_intents", set()) or set()
            )
        except Exception:
            return False

    async def _retry_source_outcomes(
        self,
        torrent_id: int,
        rows: list[dict],
        reserved_paths: set[str],
        *,
        paused: bool,
    ) -> tuple[int, int]:
        """Retry pre-dispatch source failures without rebuilding physical siblings."""
        candidates = [
            dict(row)
            for row in rows
            if not str(row.get("local_path") or "").strip()
            and str(row.get("status") or "") in {"error", "missing"}
            and str(row.get("source_url") or "").strip()
            and str(row.get("mirror_state") or "") != "exhausted"
        ]
        if not candidates:
            return 0, 0

        output_root = Path(get_settings().download_folder)
        unlock_sem = asyncio.Semaphore(3)

        async def unlock(row: dict, index: int) -> dict:
            source_url = str(row.get("source_url") or "").strip()
            async with unlock_sem:
                try:
                    result = await _retry_async(
                        self.ad().unlock_link,
                        source_url,
                        retry_if=lambda exc: not (
                            isinstance(exc, AllDebridAPIError)
                            and exc.code == "LINK_DOWN"
                        ),
                    )
                    generated_url = str(result.get("link") or "").strip()
                    parsed = urlparse(generated_url)
                    if (
                        parsed.scheme.lower() not in {"http", "https"}
                        or not parsed.netloc
                    ):
                        raise RuntimeError(
                            "AllDebrid returned no usable download URL"
                        )
                    filename = safe_name(
                        str(
                            result.get("filename")
                            or result.get("name")
                            or row.get("filename")
                            or direct_link_filename(source_url, index)
                        )
                    )
                    size_bytes = max(
                        0,
                        int(
                            result.get("filesize")
                            or result.get("size")
                            or row.get("size_bytes")
                            or 0
                        ),
                    )
                    return {
                        **row,
                        "filename": filename,
                        "size_bytes": size_bytes,
                        "generated_url": generated_url,
                        "error": None,
                        "missing": False,
                        "failure_prefix": "",
                    }
                except Exception as exc:
                    return {
                        **row,
                        "generated_url": "",
                        "error": _safe_persisted_error(exc, source_url),
                        "missing": (
                            isinstance(exc, AllDebridAPIError)
                            and exc.code == "LINK_DOWN"
                        ),
                        "failure_prefix": _direct_link_unlock_failure_prefix(exc),
                    }

        results = await asyncio.gather(
            *(
                unlock(row, index)
                for index, row in enumerate(candidates, start=1)
            )
        )

        recovered = 0
        failed = 0
        async with get_db() as db:
            for result in results:
                file_id = int(result["id"])
                if result["error"]:
                    failed += 1
                    missing = bool(result.get("missing"))
                    reason = (
                        "File is no longer available on the source host"
                        if missing
                        else f"{result['failure_prefix']}: {result['error']}"
                    )
                    await db.execute(
                        """UPDATE download_files
                              SET status=?, blocked=NULL, block_reason=?,
                                  download_url=NULL, download_id=NULL,
                                  updated_at=CURRENT_TIMESTAMP
                            WHERE id=? AND torrent_id=?""",
                        (
                            "missing" if missing else "error",
                            reason,
                            file_id,
                            torrent_id,
                        ),
                    )
                    continue

                local_path = self._unique_direct_link_path(
                    output_root,
                    str(result["filename"]),
                    reserved_paths,
                )
                recovered += 1
                await db.execute(
                    """UPDATE download_files
                          SET filename=?, size_bytes=?, download_url=?,
                              local_path=?, status=?, download_id=NULL,
                              blocked=0, block_reason=NULL, retry_count=0,
                              mirror_group_id=NULL, mirror_state=NULL,
                              updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND torrent_id=?""",
                    (
                        result["filename"],
                        int(result["size_bytes"] or 0),
                        result["generated_url"],
                        str(local_path),
                        "paused" if paused else "pending",
                        file_id,
                        torrent_id,
                    ),
                )
            await db.commit()
        return recovered, failed

    async def retry_direct_link_collection(self, torrent_id: int) -> dict:
        """Repair an existing direct-link collection without changing canonical paths."""
        torrent_id = int(torrent_id)
        await self._normalize_direct_link_source_outcomes(torrent_id)

        async with get_db() as db:
            parent = await db.fetchone(
                "SELECT * FROM torrents WHERE id=?", (torrent_id,)
            )
            if not parent:
                raise ValueError("Transfer not found")
            if str(parent.get("source") or "") != DIRECT_LINK_SOURCE:
                raise ValueError("Transfer is not a direct-link collection")
            try:
                links = normalize_direct_links(
                    json.loads(parent.get("magnet") or "[]")
                )
            except Exception as exc:
                raise ValueError("Stored direct-link payload is invalid") from exc
            rows = await db.fetchall(
                """SELECT id, filename, size_bytes, source_url, download_url,
                          local_path, status, download_id, blocked, block_reason,
                          mirror_group_id, mirror_state
                     FROM download_files
                    WHERE torrent_id=? ORDER BY id""",
                (torrent_id,),
            )

        physical = [
            dict(row)
            for row in rows
            if row.get("blocked") == 0
            and str(row.get("local_path") or "").strip()
        ]
        if not physical:
            # No durable local delivery manifest exists yet. Retain the original
            # provider-generation retry semantics for pre-materialization errors.
            return await super().retry_direct_link_collection(torrent_id)

        paused = self._direct_link_retry_paused(torrent_id)
        snapshot = await self._aria2_get_all()
        by_gid = {
            str(getattr(item, "gid", "") or "").strip(): item
            for item in snapshot
            if str(getattr(item, "gid", "") or "").strip()
        }
        # A bulk snapshot can miss a GID while it changes aria2 state. Confirm
        # every stored miss explicitly before any manifest mutation; an RPC
        # connectivity failure therefore aborts Retry instead of being mistaken
        # for disappearance.
        for row in physical:
            gid = str(row.get("download_id") or "").strip()
            if gid and gid not in by_gid:
                state = await self._aria2_confirm_gid(gid)
                if state is not None:
                    by_gid[gid] = state

        verified = 0
        live = 0
        resumable = 0
        pending = 0
        reserved_paths = {
            str(row.get("local_path") or "").strip().casefold()
            for row in physical
            if str(row.get("local_path") or "").strip()
        }
        async with get_db() as db:
            other_paths = await db.fetchall(
                """SELECT DISTINCT f.local_path
                     FROM download_files f
                     JOIN torrents t ON t.id=f.torrent_id
                    WHERE t.status NOT IN ('completed','deleted','error')
                      AND t.id!=?
                      AND f.local_path IS NOT NULL AND f.local_path!=''""",
                (torrent_id,),
            )
        reserved_paths.update(
            str(row.get("local_path") or "").strip().casefold()
            for row in other_paths
            if str(row.get("local_path") or "").strip()
        )

        complete_candidates = {
            int(row["id"]): True
            for row in physical
            if self._local_payload_matches_manifest(
                Path(str(row["local_path"])),
                int(row.get("size_bytes") or 0),
            )
        }
        if complete_candidates:
            await asyncio.sleep(_EXISTING_PAYLOAD_STABILITY_SECONDS)
            for row in physical:
                file_id = int(row["id"])
                if file_id not in complete_candidates:
                    continue
                if not self._local_payload_matches_manifest(
                    Path(str(row["local_path"])),
                    int(row.get("size_bytes") or 0),
                ):
                    complete_candidates.pop(file_id, None)

        # Redispatch requires durable source identity. Never fall back to the
        # legacy whole-parent rebuild merely because an old database row lacks
        # source_url: that is exactly the operation that can create duplicate
        # physical files. Live and already-verified children do not need it.
        for row in physical:
            file_id = int(row["id"])
            if file_id in complete_candidates:
                continue
            gid = str(row.get("download_id") or "").strip()
            state_name = str(
                getattr(
                    by_gid.get(gid) if gid else None,
                    "status",
                    "",
                )
                or ""
            ).strip().lower()
            if state_name in _LIVE_ARIA2_STATES:
                continue
            if not str(row.get("source_url") or "").strip():
                raise RuntimeError(
                    "Cannot safely retry a legacy direct-link child without "
                    "its preserved source URL"
                )

        async with get_db() as db:
            for row in physical:
                file_id = int(row["id"])
                local_path = Path(str(row["local_path"]))
                gid = str(row.get("download_id") or "").strip()
                state = by_gid.get(gid) if gid else None

                if file_id in complete_candidates:
                    verified += 1
                    await db.execute(
                        """UPDATE download_files
                              SET status='completed', block_reason=NULL,
                                  updated_at=CURRENT_TIMESTAMP
                            WHERE id=? AND torrent_id=?""",
                        (file_id, torrent_id),
                    )
                    continue

                state_name = str(
                    getattr(state, "status", "") or ""
                ).strip().lower()
                if state is not None and state_name in _LIVE_ARIA2_STATES:
                    live += 1
                    desired = {
                        "active": "downloading",
                        "waiting": "queued",
                        "paused": "paused",
                    }[state_name]
                    await db.execute(
                        """UPDATE download_files
                              SET status=?, block_reason=NULL,
                                  updated_at=CURRENT_TIMESTAMP
                            WHERE id=? AND torrent_id=?""",
                        (desired, file_id, torrent_id),
                    )
                    continue

                sidecar = Path(f"{local_path}.aria2")
                if self._directory_contains_name(sidecar):
                    resumable += 1
                else:
                    pending += 1
                await db.execute(
                    """UPDATE download_files
                          SET status=?, download_id=NULL, download_url=NULL,
                              block_reason=NULL, retry_count=0,
                              updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND torrent_id=?""",
                    (
                        "paused" if paused else "pending",
                        file_id,
                        torrent_id,
                    ),
                )
            await db.commit()

        # Move the parent out of its terminal error state before mirror
        # classification. The child manifest is already durable and canonical.
        async with get_db() as db:
            await db.execute(
                """UPDATE torrents
                      SET status=?, provider_status='ready', error_message=NULL,
                          completed_at=NULL, updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status!='deleted'""",
                ("paused" if paused else "queued", torrent_id),
            )
            await db.commit()

        async with self._direct_link_path_planning_lock:
            recovered_sources, failed_sources = await self._retry_source_outcomes(
                torrent_id,
                [dict(row) for row in rows],
                reserved_paths,
                paused=paused,
            )

        # Recovered source rows may be mirrors of an already-present physical
        # artifact. Collapse those identities before calculating parent truth.
        from services.dispatch_coordinator import collapse_direct_link_mirrors

        await collapse_direct_link_mirrors()

        async with get_db() as db:
            counts = await db.fetchone(
                """SELECT
                       SUM(CASE WHEN blocked=0 AND local_path IS NOT NULL THEN 1 ELSE 0 END) AS required_count,
                       SUM(CASE WHEN blocked=0 AND local_path IS NOT NULL AND status='completed' THEN 1 ELSE 0 END) AS completed_count,
                       SUM(CASE WHEN blocked=0 AND local_path IS NOT NULL AND status='downloading' THEN 1 ELSE 0 END) AS downloading_count,
                       SUM(CASE WHEN blocked=0 AND local_path IS NOT NULL AND status!='completed' THEN 1 ELSE 0 END) AS unfinished_count,
                       COALESCE(SUM(CASE WHEN blocked=0 AND local_path IS NOT NULL THEN size_bytes ELSE 0 END), 0) AS total_bytes,
                       COALESCE(SUM(CASE WHEN blocked=0 AND local_path IS NOT NULL AND status='completed' THEN size_bytes ELSE 0 END), 0) AS completed_bytes,
                       SUM(CASE WHEN local_path IS NULL AND download_id IS NULL
                                     AND status IN ('error','missing')
                                     AND COALESCE(mirror_state, '')!='exhausted'
                                THEN 1 ELSE 0 END) AS source_failure_count
                   FROM download_files WHERE torrent_id=?""",
                (torrent_id,),
            )
            counts = counts or {}
            required_count = int(counts.get("required_count") or 0)
            completed_count = int(counts.get("completed_count") or 0)
            unfinished_count = int(counts.get("unfinished_count") or 0)
            downloading_count = int(counts.get("downloading_count") or 0)
            total_bytes = int(counts.get("total_bytes") or 0)
            completed_bytes = int(counts.get("completed_bytes") or 0)
            source_failures = int(counts.get("source_failure_count") or 0)
            progress = (
                round(completed_bytes / total_bytes * 100.0, 1)
                if total_bytes > 0
                else (
                    round(completed_count / required_count * 100.0, 1)
                    if required_count
                    else 0.0
                )
            )
            if unfinished_count:
                progress = min(progress, 99.9)

            if unfinished_count <= 0 and required_count > 0:
                parent_status = "queued"  # finalizer owns terminal completion
            elif paused:
                parent_status = "paused"
            elif downloading_count:
                parent_status = "downloading"
            else:
                parent_status = "queued"

            warning = (
                f"{source_failures} of {len(links)} links could not be generated"
                if source_failures
                else None
            )
            await db.execute(
                """UPDATE torrents
                      SET status=?, provider_status='ready', progress=?, size_bytes=?,
                          error_message=?, completed_at=NULL,
                          updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND status!='deleted'""",
                (
                    parent_status,
                    progress,
                    total_bytes,
                    warning,
                    torrent_id,
                ),
            )
            event = (
                "Manual retry reconciled existing direct-link state: "
                f"{verified} verified complete, {live} live aria2, "
                f"{resumable} resumable, {pending} missing/restart, "
                f"{recovered_sources} source outcome(s) recovered"
            )
            if failed_sources:
                event += (
                    f", {failed_sources} source outcome(s) still unavailable"
                )
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) "
                "VALUES (?, 'info', ?)",
                (torrent_id, event),
            )
            await db.commit()

        await self._broadcast_direct_link_update(
            torrent_id,
            parent_status,
            str(parent.get("name") or "Debrid links"),
            progress,
        )

        if unfinished_count <= 0 and required_count > 0:
            await self._finalize_aria2_torrent(torrent_id)
            new_status = "completed"
        else:
            if not paused:
                await self.advance_aria2_queue()
            new_status = parent_status

        return {
            "ok": True,
            "new_status": new_status,
            "link_count": len(links),
            "verified_complete": verified,
            "live": live,
            "resumable": resumable,
            "pending": pending,
            "recovered_sources": recovered_sources,
            "failed_sources": failed_sources,
        }


manager = DirectLinkRetryGuardManager()
