"""Deliver canonical lifecycle events to browser and notification adapters."""
import asyncio
import logging

from application.consolidation_events import ConsolidationEvents
from core.config import get_settings
from core.logging_utils import sanitize_exception
from services import account_notifications
from services.event_bus import publish
from services.notification_service import NotificationService
from services.notifications import COLOR_PARTIAL

logger = logging.getLogger("debridpulse.notify")


class Observability:
    def __init__(self, repository, consolidation_events=None):
        self.repository = repository
        self.consolidation_events = consolidation_events

    async def deliver(self):
        if self.consolidation_events is not None:
            # Hidden post-commit markers become public only after the incoming
            # logical submission has a stable complete/partial disposition.
            await self.consolidation_events.finalize_pending()
        for event in await self.repository.pending_events():
            if event["kind"] == "file_selection_available":
                # A new automatically presentable multi-file selection offer.
                # No provider identity is sent to the browser for selection
                # policy; the browser reads authoritative state from the
                # dedicated file-selection API.
                if await self.repository.claim_event(event["id"]):
                    await publish("file_selection_available", {"transfer_id": int(event["transfer_id"])})
                continue
            item = await self.repository.presentation(event["transfer_id"], details=True)
            if item is None or not await self.repository.claim_event(event["id"]):
                continue
            # Claim before external delivery. An interrupted webhook response
            # cannot establish whether the recipient accepted it; never repeat
            # that ambiguous delivery automatically.
            await publish("torrent_updated", item)
            await publish("stats_changed", {})
            kind = event["kind"]
            if kind == "duplicate_consolidated":
                payload = ConsolidationEvents.public_payload(event.get("detail"))
                if payload is not None:
                    await publish("duplicate_consolidated", payload)

            cfg = get_settings()
            # The transfer as the operator knows it: the canonical presentation's
            # derived display name when one exists at dispatch, else the durable
            # submission name (which stays untouched).
            name = item.get("display_name") or item["name"]
            if kind != "demand_accepted":
                # The global delivery: once per event, instance-wide, unchanged.
                await _notify(NotificationService().client(), cfg, kind, item, event, name)
            try:
                await self._deliver_personal(kind, item, event, name)
            except Exception as exc:
                logger.error("Personal notification fanout failed (%s): %s",
                             kind, sanitize_exception(exc, max_length=200))

    async def _deliver_personal(self, kind, item, event, name):
        """The same event, to each eligible User whose OWN outstanding demand it
        concerns, through that User's own configuration and client.

        Ownership is the demand ledger's at dispatch: a withdrawn demand, a
        disabled or removed account and an unattributed request reach nobody.
        Every recipient is independent -- one failing or slow webhook neither
        stops the others nor the global delivery that already happened -- and
        the claim above means no event is delivered to anyone twice."""
        if kind == "demand_accepted":
            demand = await self.repository.demand(event.get("detail") or "")
            if demand is None or int(demand["transfer_id"]) != int(event["transfer_id"]):
                return
            rows = [demand]
        elif kind in ("completed", "error"):
            rows = await self.repository.outstanding_demand(event["transfer_id"])
        else:
            return
        recipients = await account_notifications.recipients(rows)
        # Every recipient is delivered to; only how many posts are in flight at
        # once is bounded.
        in_flight = asyncio.Semaphore(account_notifications.MAX_CONCURRENT_SENDS)

        async def send(account_id, settings):
            async with in_flight:
                try:
                    await _notify(account_notifications.client(settings), settings,
                                  "accepted" if kind == "demand_accepted" else kind, item, event, name)
                except Exception as exc:
                    logger.error("Personal notification failed for one account (%s): %s",
                                 kind, sanitize_exception(exc, max_length=200))

        if recipients:
            await asyncio.gather(*(send(account_id, settings) for account_id, settings in recipients))


async def _notify(notify, choices, kind, item, event, name):
    """One event, rendered once, through one client honouring one set of
    event choices -- the global configuration or a User's own."""
    if kind == "accepted" and choices.discord_notify_added:
        await notify.send_added(name, source=item["source"], transfer_id=str(item["id"]))
    elif kind == "completed":
        if choices.discord_notify_finished:
            if item["source_failure_count"]:
                await notify.send("Completed with source warnings", f"{name}\n{item['source_failure_count']} source request(s) failed; selected payloads completed.", color=COLOR_PARTIAL)
            elif item["blocked_count"]:
                await notify.send_partial(name, total_files=item["file_count"],
                    downloaded_files=sum(file["status"] == "completed" for file in item["files"]),
                    blocked_files=item["blocked_count"], downloaded_size=item["size_bytes"])
            else:
                await notify.send_complete(name, file_count=sum(file["status"] == "completed" for file in item["files"]),
                    size_bytes=item["size_bytes"], destination=item["local_path"] or "", download_client=", ".join(item["executors"]))
        if choices.discord_notify_extract and item["extraction_status"] == "error":
            await notify.send_extract_failed(name, reason=item["extraction_error"] or "Post-processing failed")
        elif choices.discord_notify_extract and item["extraction_status"] == "completed":
            await notify.send_extract_complete(name, dest=item["local_path"] or "")
    elif kind == "error" and choices.discord_notify_error:
        await notify.send_error(name, reason=event["detail"] or "Transfer requires attention", source=item["source"],
            provider=", ".join(item["providers"]), transfer_id=str(item["id"]),
            category=(item.get("error") or {}).get("category", ""))
