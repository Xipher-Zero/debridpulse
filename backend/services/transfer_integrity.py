"""Transfer-integrity overrides for DebridPulse V1.

DebridPulse separates three kinds of state that the inherited client conflated:

* the database records transfer history and operator intent;
* the filesystem records whether a local payload is presently available;
* aria2 is authoritative for delivery of anything not already proven local.

A historical ``completed`` row is never proof that the payload still exists. A
local file may be adopted without re-downloading only when it is a regular file,
has the exact size advertised by the current provider manifest, has no aria2
control sidecar, and remains valid across a second stability check before parent
finalisation. Missing, partial, unknown-size, unstable, or resumable files are
staged through aria2. Stopped aria2 history (complete/removed/error) likewise
cannot satisfy a fresh dispatch.

The legacy TorrentManager remains the compatibility implementation for the rest
of V1. This derived engine intentionally overrides only these integrity
boundaries so the policy stays isolated until materialisation is fully extracted
from manager_v2.
"""
from __future__ import annotations

import asyncio
import logging
import stat
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set, Tuple

from core.config import get_settings
from db.database import get_db
from services.aria2 import Aria2Service
from services.aria2_runtime import effective_rpc_config, is_builtin_mode
from services.manager_v2 import (
    TorrentManager,
    _size_sum,
    is_blocked,
    safe_name,
    safe_rel_path,
)

logger = logging.getLogger("debridpulse.transfer_integrity")

_LIVE_ARIA2_STATES = frozenset({"active", "waiting", "paused"})
_EXISTING_PAYLOAD_STABILITY_SECONDS = 0.20


class TransferIntegrityAria2Service(Aria2Service):
    """Never let stopped aria2 history satisfy a fresh transfer request."""

    async def ensure_download(
        self,
        uri: str,
        options: Optional[Dict[str, object]] = None,
        start_paused: bool = False,
        max_retries: int = 5,
        cached_downloads=None,
    ) -> str:
        # Built-in aria2 historically allowed a stopped complete/removed result
        # to be returned as if it were a live transfer. Resolve one snapshot
        # here, then expose only resumable/live states to the inherited
        # deduplication logic. External mode already refuses terminal adoption,
        # but filtering supplied snapshots keeps both modes on the same contract.
        if cached_downloads is None and is_builtin_mode():
            cached_downloads = await self.get_all()
        if cached_downloads is not None:
            cached_downloads = [
                download
                for download in cached_downloads
                if str(getattr(download, "status", "") or "").lower()
                in _LIVE_ARIA2_STATES
            ]
        return await super().ensure_download(
            uri,
            options=options,
            start_paused=start_paused,
            max_retries=max_retries,
            cached_downloads=cached_downloads,
        )


class TransferIntegrityManager(TorrentManager):
    """TorrentManager with explicit filesystem/aria2 delivery authority."""

    def aria2(self) -> Aria2Service:
        if self._aria2 is None:
            cfg = get_settings()
            url, secret = effective_rpc_config(cfg)
            self._aria2 = TransferIntegrityAria2Service(
                url,
                secret,
                cfg.aria2_operation_timeout_seconds,
            )
        return self._aria2

    @staticmethod
    def _local_payload_matches_manifest(local_path: Path, expected_size: int) -> bool:
        """Return True only for a complete, non-resumable local payload.

        Provider size is the strongest inexpensive proof currently available for
        individual files. Unknown sizes are intentionally not adopted. A .aria2
        control sidecar means aria2 owns resumable state and must decide how to
        continue that file instead of DebridPulse declaring it complete.
        """
        expected_size = int(expected_size or 0)
        if expected_size <= 0:
            return False
        try:
            if Path(f"{local_path}.aria2").exists():
                return False
            info = local_path.lstat()
        except (FileNotFoundError, OSError):
            return False
        if not stat.S_ISREG(info.st_mode):
            return False
        return int(info.st_size) == expected_size

    async def _engine_download(self, torrent_id: int, ad_id: str, name: str):
        cfg = get_settings()
        client_name = self.download_client_name()
        initial_status = "queued"  # aria2 is the only non-symlink client

        # ── Disk-space guard ────────────────────────────────────────────────
        min_free_gb = float(getattr(cfg, "min_free_disk_gb", 0) or 0)
        if min_free_gb > 0:
            if self._disk_guard_active:
                logger.info(
                    "disk_guard: deferring torrent %s dispatch — guard is active (low disk), "
                    "resetting to 'ready'",
                    torrent_id,
                )
                async with get_db() as db:
                    await db.execute(
                        "UPDATE torrents SET status='ready', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (torrent_id,),
                    )
                    await db.commit()
                return
            free_gb = self._get_free_gb(str(cfg.download_folder or "/download"))
            if free_gb >= 0 and free_gb < min_free_gb:
                logger.info(
                    "Disk-space guard: torrent %s uploaded to AllDebrid but aria2 download "
                    "deferred (%.1f GB free, %.1f GB required)",
                    torrent_id,
                    free_gb,
                    min_free_gb,
                )
                self._disk_guard_active = True
                async with get_db() as db:
                    await db.execute(
                        "UPDATE torrents SET status='ready', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (torrent_id,),
                    )
                    await db.commit()
                return

        # Cancel any live aria2 jobs previously associated with this transaction
        # before rebuilding its manifest. Stopped results are deliberately
        # preserved by the existing external-daemon ownership policy.
        try:
            async with get_db() as db:
                old_gids = await (
                    await db.execute(
                        "SELECT download_id FROM download_files "
                        "WHERE torrent_id=? AND download_client='aria2' "
                        "AND download_id IS NOT NULL AND status NOT IN ('completed','error','blocked')",
                        (torrent_id,),
                    )
                ).fetchall()
            for row in old_gids:
                gid = str(row["download_id"] or "")
                if not gid:
                    continue
                try:
                    await self._remove_owned_aria2_gid(gid)
                    logger.debug(
                        "integrity materializer: cancelled stale aria2 GID %s for torrent %s",
                        gid,
                        torrent_id,
                    )
                except Exception:
                    pass
        except Exception as exc:
            logger.debug(
                "integrity materializer: stale aria2 cleanup skipped: %s", exc
            )

        async with get_db() as db:
            await db.execute(
                "DELETE FROM download_files WHERE torrent_id=?", (torrent_id,)
            )
            await db.execute(
                "UPDATE torrents SET status=?, download_client=?, error_message=NULL, "
                "completed_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (initial_status, client_name, torrent_id),
            )
            await db.commit()

        flat_files = await self._fetch_ready_files(ad_id)
        if not flat_files:
            raise Exception("No downloadable files returned from AllDebrid")

        destination_root = Path(cfg.download_folder) / safe_name(name)
        total_files = len(flat_files)
        blocked_items: List[dict] = []
        existing_items: List[dict] = []
        queued_items: List[dict] = []
        failed_items: List[dict] = []
        seen_queue_keys: Set[Tuple[str, str]] = set()
        manifest_rows: List[tuple] = []

        for file_info in flat_files:
            relative_path = (
                file_info.get("path")
                or file_info.get("name")
                or "download.bin"
            )
            display_name = str(PurePosixPath(relative_path.replace("\\", "/")))
            file_size = int(file_info.get("size", 0) or 0)
            blocked, reason = is_blocked(display_name, cfg, file_size)
            source_link = file_info["link"]

            relative_target = safe_rel_path(display_name)
            torrent_root = safe_name(name)
            if relative_target.parts and relative_target.parts[0] == torrent_root:
                remaining_parts = relative_target.parts[1:]
                relative_target = (
                    Path(*remaining_parts)
                    if remaining_parts
                    else Path("download.bin")
                )

            local_path = destination_root / relative_target
            dedupe_key = (display_name.lower(), source_link.strip())
            if dedupe_key in seen_queue_keys:
                logger.info(
                    "Skipping duplicate AllDebrid file entry for %s", display_name
                )
                continue
            seen_queue_keys.add(dedupe_key)

            if blocked:
                blocked_items.append(
                    {
                        "filename": display_name,
                        "size_bytes": file_size,
                        "reason": reason,
                    }
                )
                manifest_rows.append(
                    (
                        torrent_id,
                        display_name,
                        file_size,
                        source_link,
                        source_link,
                        str(local_path),
                        "blocked",
                        client_name,
                        1,
                        reason,
                    )
                )
                continue

            item = {
                "filename": display_name,
                "size_bytes": file_size,
                "source_link": source_link,
                "local_path": local_path,
            }
            if self._local_payload_matches_manifest(local_path, file_size):
                existing_items.append(item)
                manifest_status = "completed"
            else:
                queued_items.append(item)
                manifest_status = "pending"

            manifest_rows.append(
                (
                    torrent_id,
                    display_name,
                    file_size,
                    source_link,
                    source_link,
                    str(local_path),
                    manifest_status,
                    "aria2",
                    0,
                    None,
                )
            )

        if manifest_rows:
            async with get_db() as db:
                await db.executemany(
                    """INSERT INTO download_files
                       (torrent_id, filename, size_bytes, source_url,
                        download_url, local_path, status, download_id,
                        download_client, blocked, block_reason, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, CURRENT_TIMESTAMP)""",
                    manifest_rows,
                )
                await db.commit()

        # Do not authorize completion from one filesystem observation. Existing
        # candidates must remain exact and stable after the manifest is durable.
        # One batch delay avoids adding per-file latency for multi-file torrents.
        if existing_items:
            await asyncio.sleep(_EXISTING_PAYLOAD_STABILITY_SECONDS)
            unstable_items: List[dict] = []
            stable_items: List[dict] = []
            for item in existing_items:
                if self._local_payload_matches_manifest(
                    item["local_path"], item["size_bytes"]
                ):
                    stable_items.append(item)
                else:
                    unstable_items.append(item)

            if unstable_items:
                async with get_db() as db:
                    for item in unstable_items:
                        await db.execute(
                            """UPDATE download_files
                               SET status='pending', updated_at=CURRENT_TIMESTAMP
                               WHERE torrent_id=? AND source_url=? AND local_path=?
                                 AND status='completed' AND blocked=0""",
                            (
                                torrent_id,
                                item["source_link"],
                                str(item["local_path"]),
                            ),
                        )
                    await db.commit()
                queued_items.extend(unstable_items)
                logger.warning(
                    "integrity materializer: %d existing file(s) changed or disappeared "
                    "during validation for torrent %s; routing them through aria2",
                    len(unstable_items),
                    torrent_id,
                )
            existing_items = stable_items

        blocked_count = len(blocked_items)
        failed_count = len(failed_items)
        completed_count = len(existing_items)
        queued_count = len(queued_items)
        total_size_bytes = _size_sum(
            blocked_items + existing_items + queued_items + failed_items
        )

        if blocked_count == total_files and total_files > 0 and failed_count == 0:
            final_status = "completed"
        elif queued_count > 0:
            final_status = "queued"
        elif failed_count == 0 and completed_count > 0:
            final_status = "completed"
        else:
            final_status = "error"

        if final_status == "queued" and self.is_paused():
            final_status = "paused"
            async with get_db() as db:
                await db.execute(
                    """UPDATE download_files
                       SET status='paused', updated_at=CURRENT_TIMESTAMP
                       WHERE torrent_id=? AND blocked=0 AND status='pending'""",
                    (torrent_id,),
                )
                await db.commit()

        async with get_db() as db:
            source_row = await db.fetchone(
                "SELECT source FROM torrents WHERE id=?", (torrent_id,)
            )
            transfer_source = source_row.get("source") if source_row else None
            await db.execute(
                "UPDATE torrents SET status=?, local_path=?, size_bytes=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (final_status, str(destination_root), total_size_bytes, torrent_id),
            )
            if final_status == "completed":
                await db.execute(
                    "UPDATE torrents SET completed_at=CURRENT_TIMESTAMP WHERE id=?",
                    (torrent_id,),
                )

            if blocked_count == total_files and total_files > 0:
                event_message = (
                    f"All {blocked_count} file(s) filtered/blocked — marked completed, "
                    "removed from AllDebrid"
                )
                event_level = "info"
            elif final_status == "completed" and completed_count > 0:
                event_message = (
                    f"Verified {completed_count} existing local file(s) against the "
                    "provider manifest; no aria2 transfer required"
                )
                event_level = "info"
            else:
                details = [f"{queued_count} file(s) prepared for aria2"]
                if completed_count:
                    details.append(f"{completed_count} existing file(s) verified")
                if blocked_count:
                    details.append(f"{blocked_count} filtered")
                event_message = f"Download {final_status}: " + ", ".join(details)
                event_level = (
                    "info"
                    if final_status in {"queued", "paused"}
                    else "warn"
                )
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                (torrent_id, event_level, event_message),
            )
            await db.commit()

        await self._send_partial_summary(
            torrent_id,
            name,
            flat_files,
            blocked_items,
            existing_items + queued_items,
            failed_items,
        )

        if final_status == "completed":
            await self._delete_magnet_after_completion(
                torrent_id, ad_id, transfer_source
            )
            await self._mark_finished(torrent_id, name=name)
            if (
                cfg.discord_notify_finished
                and blocked_count < total_files
                and completed_count > 0
            ):
                await self.notify().send_complete(
                    name,
                    file_count=completed_count,
                    destination=str(destination_root),
                    download_client="aria2",
                )
        elif final_status in {"queued", "paused"}:
            await self._log_event(
                torrent_id,
                "info",
                "Prepared for aria2-authoritative delivery",
            )
            await self.advance_aria2_queue()
        else:
            await self._notify_provider_error(
                name,
                reason="Kept on AllDebrid for inspection",
                context=(
                    "No required provider file could be verified locally or staged "
                    "for aria2 delivery."
                ),
                alldebrid_id=str(ad_id or ""),
            )


manager = TransferIntegrityManager()
