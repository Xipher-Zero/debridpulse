"""Post-download extraction orchestration for completed transfers."""
from __future__ import annotations

from pathlib import Path

from core.config import get_settings
from db.database import get_db
from services.event_bus import publish
from services.extractor import archive_paths_from_downloads, get_extractor
from services.notifications import NotificationService


class ExtractionService:
    async def extract_archive(self, *args, **kwargs):
        return await get_extractor().extract_archive(*args, **kwargs)

    async def _publish_state(
        self,
        torrent_id: int,
        name: str,
        extraction_status: str,
        extraction_error: str | None = None,
    ) -> None:
        try:
            await publish(
                "torrent_updated",
                {
                    "id": int(torrent_id),
                    "status": "completed",
                    "name": str(name or ""),
                    "extraction_status": extraction_status,
                    "extraction_error": extraction_error,
                },
            )
            await publish("stats_changed", {})
        except Exception:
            pass

    async def extract_completed_transfer(
        self,
        torrent_id: int,
        torrent_dict: dict | None = None,
    ) -> dict:
        """Extract known completed archive children without changing transport truth."""
        cfg = get_settings()
        if not bool(getattr(cfg, "extract_enabled", False)):
            return {"attempted": False, "reason": "disabled"}

        torrent_id = int(torrent_id)
        async with get_db() as db:
            parent = await db.fetchone(
                "SELECT * FROM torrents WHERE id=?",
                (torrent_id,),
            )
            rows = await db.fetchall(
                """SELECT local_path FROM download_files
                   WHERE torrent_id=? AND status='completed'
                     AND local_path IS NOT NULL""",
                (torrent_id,),
            )

        if not parent or str(parent.get("status") or "") != "completed":
            return {"attempted": False, "reason": "not-completed"}

        archives = archive_paths_from_downloads(
            [row.get("local_path") for row in rows if row.get("local_path")]
        )
        if not archives:
            async with get_db() as db:
                await db.execute(
                    """UPDATE torrents
                       SET extraction_status='skipped', extraction_error=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='completed'""",
                    (torrent_id,),
                )
                await db.execute(
                    "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                    (torrent_id, "Auto-extract: Not attempted · no supported archive detected"),
                )
                await db.commit()
            return {"attempted": False, "reason": "no-archives"}

        name = str(
            parent.get("name")
            or (torrent_dict or {}).get("name")
            or f"transfer {torrent_id}"
        )
        async with get_db() as db:
            await db.execute(
                """UPDATE torrents
                   SET extraction_status='extracting', extraction_error=NULL,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status='completed'""",
                (torrent_id,),
            )
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                (
                    torrent_id,
                    f"Auto-extract: Attempted · {len(archives)} archive(s) detected",
                ),
            )
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, 'info', ?)",
                (torrent_id, "Extraction status: Extracting"),
            )
            await db.commit()

        await self._publish_state(torrent_id, name, "extracting")

        extractor = get_extractor()
        extractor.update_max_concurrent(
            max(1, int(getattr(cfg, "extract_max_concurrent", 1) or 1))
        )
        results = await extractor.extract_archives(
            archives,
            delete_after=bool(getattr(cfg, "extract_delete_archive", True)),
        )
        failures = [(path, msg) for path, ok, msg in results if not ok]
        successes = [(path, msg) for path, ok, msg in results if ok]

        if not results:
            failures = [
                (
                    archives[0],
                    "Auto-extract produced no result for a detected archive",
                )
            ]

        if failures:
            detail = "; ".join(str(msg) for _path, msg in failures[:3])
            if len(failures) > 3:
                detail += f"; +{len(failures) - 3} more extraction failure(s)"
            detail = detail[:1000]
            final_state = "error"
            event_level = "error"
            event_message = (
                f"Extraction status: Failed · {len(failures)}/{len(archives)} archive(s) failed · {detail}"
            )[:1200]
        else:
            detail = ""
            final_state = "completed"
            event_level = "info"
            event_message = (
                f"Extraction status: Completed · {len(successes)}/{len(archives)} archive(s) extracted"
            )

        async with get_db() as db:
            await db.execute(
                """UPDATE torrents
                   SET extraction_status=?, extraction_error=?,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status!='deleted'""",
                (final_state, detail or None, torrent_id),
            )
            await db.execute(
                "INSERT INTO events (torrent_id, level, message) VALUES (?, ?, ?)",
                (torrent_id, event_level, event_message),
            )
            await db.commit()

        await self._publish_state(
            torrent_id,
            name,
            final_state,
            detail or None,
        )

        if bool(getattr(cfg, "discord_notify_extract", False)):
            notify = NotificationService(
                webhook_url=str(getattr(cfg, "discord_webhook_url", "") or ""),
                added_webhook_url=str(getattr(cfg, "discord_webhook_added", "") or ""),
            )
            if successes:
                await notify.send_extract_complete(
                    name,
                    archive_count=len(successes),
                    dest=str(Path(successes[0][0]).parent),
                )
            for _path, message in failures:
                await notify.send_extract_failed(name, reason=str(message))

        return {
            "attempted": True,
            "status": final_state,
            "archives": len(archives),
            "succeeded": len(successes),
            "failed": len(failures),
            "error": detail or None,
        }
