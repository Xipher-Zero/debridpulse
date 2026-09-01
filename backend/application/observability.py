"""Deliver canonical lifecycle events to browser and notification adapters."""
from core.config import get_settings
from services.event_bus import publish
from services.notification_service import NotificationService
from services.notifications import COLOR_PARTIAL


class Observability:
    def __init__(self, repository):
        self.repository = repository

    async def deliver(self):
        for event in await self.repository.pending_events():
            item = await self.repository.presentation(event["transfer_id"], details=True)
            if item is None or not await self.repository.claim_event(event["id"]):
                continue
            # Claim before external delivery. An interrupted webhook response
            # cannot establish whether the recipient accepted it; never repeat
            # that ambiguous delivery automatically.
            await publish("torrent_updated", item)
            await publish("stats_changed", {})
            cfg = get_settings()
            notify = NotificationService().client()
            kind = event["kind"]
            if kind == "accepted" and cfg.discord_notify_added:
                await notify.send_added(item["name"], source=item["source"], transfer_id=str(item["id"]))
            elif kind == "completed":
                if cfg.discord_notify_finished:
                    if item["source_failure_count"]:
                        await notify.send("Completed with source warnings", f"{item['name']}\n{item['source_failure_count']} source request(s) failed; selected payloads completed.", color=COLOR_PARTIAL)
                    elif item["blocked_count"]:
                        await notify.send_partial(item["name"], total_files=item["file_count"],
                            downloaded_files=sum(file["status"] == "completed" for file in item["files"]),
                            blocked_files=item["blocked_count"], downloaded_size=item["size_bytes"])
                    else:
                        await notify.send_complete(item["name"], file_count=sum(file["status"] == "completed" for file in item["files"]),
                            size_bytes=item["size_bytes"], destination=item["local_path"] or "", download_client=", ".join(item["executors"]))
                if cfg.discord_notify_extract and item["extraction_status"] == "error":
                    await notify.send_extract_failed(item["name"], reason=item["extraction_error"] or "Post-processing failed")
                elif cfg.discord_notify_extract and item["extraction_status"] == "completed":
                    await notify.send_extract_complete(item["name"], dest=item["local_path"] or "")
            elif kind == "error" and cfg.discord_notify_error:
                await notify.send_error(item["name"], reason=event["detail"] or "Transfer requires attention", source=item["source"],
                    provider=", ".join(item["providers"]), transfer_id=str(item["id"]),
                    category=(item.get("error") or {}).get("category", ""))
