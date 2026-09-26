"""The two Notifications operations, against the SAVED configuration.

These routes used to resolve an unsaved Settings draft -- a typed webhook, a
blank-means-stored secret, an explicit clear checkbox -- because nothing on the
page was durable until Apply. Every Notifications field now commits at its own
boundary and the browser settles pending writes before acting, so a draft no
longer exists: each operation exercises exactly what is stored, through the one
effective-destination owner, and records durable evidence about that material.

An operation is not a persistence path. It writes no configuration; the only
thing it may write is the OUTCOME of a test about configuration that is already
saved.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import settings_validation_routes as routes
from core.config import AppSettings
from services import notification_service as notifications


PRIMARY = "https://discord.com/api/webhooks/1/primary"
REPORT = "https://discord.com/api/webhooks/2/report"
GENERIC = "https://hooks.example.com/services/abc"


def _state(**fields):
    return AppSettings(**fields)


class DiscordOperationTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, cfg, sender=None, recorded=None):
        recorded = recorded if recorded is not None else []

        async def record(subject, fingerprint, ok):
            recorded.append((subject, fingerprint, ok))
            return notifications.notification_state(cfg)

        with patch.object(routes, "get_settings", return_value=cfg), \
             patch.object(routes, "_send_discord_test", new=sender or AsyncMock()), \
             patch.object(routes, "_record_notification_outcome", new=record):
            return await routes.validate_discord(), recorded

    async def test_the_saved_primary_webhook_is_what_gets_tested(self):
        cfg = _state(discord_webhook_url=PRIMARY, discord_username="Bot")
        sender = AsyncMock()
        result, recorded = await self._run(cfg, sender)

        sender.assert_awaited_once_with(PRIMARY)
        self.assertTrue(result["ok"])
        # It carries no draft of any kind -- the route takes no request body.
        self.assertEqual(routes.validate_discord.__code__.co_argcount, 0)

    async def test_success_records_evidence_for_exactly_the_saved_material(self):
        cfg = _state(discord_webhook_url=PRIMARY, discord_username="Bot")
        _result, recorded = await self._run(cfg)

        expected = notifications.verification_fingerprints(cfg)[notifications.DISCORD_SUBJECT]
        self.assertEqual(recorded, [(notifications.DISCORD_SUBJECT, expected, True)])

    async def test_failure_retires_the_proof_and_reports_the_real_error(self):
        cfg = _state(discord_webhook_url=PRIMARY)
        sender = AsyncMock(side_effect=RuntimeError("Discord webhook returned HTTP 404"))
        with self.assertRaises(HTTPException) as raised:
            await self._run(cfg, sender)
        self.assertEqual(raised.exception.status_code, 502)

        recorded = []
        with patch.object(routes, "get_settings", return_value=cfg), \
             patch.object(routes, "_send_discord_test", new=sender), \
             patch.object(routes, "_record_notification_outcome",
                          new=AsyncMock(side_effect=lambda s, f, ok: recorded.append((s, ok)))):
            with self.assertRaises(HTTPException):
                await routes.validate_discord()
        self.assertEqual(recorded, [(notifications.DISCORD_SUBJECT, False)])

    async def test_an_unconfigured_section_is_refused_before_anything_is_recorded(self):
        sender = AsyncMock()
        recorded = []
        with patch.object(routes, "get_settings", return_value=_state()), \
             patch.object(routes, "_send_discord_test", new=sender), \
             patch.object(routes, "_record_notification_outcome",
                          new=AsyncMock(side_effect=lambda *a: recorded.append(a))):
            with self.assertRaises(HTTPException) as raised:
                await routes.validate_discord()
        self.assertEqual(raised.exception.status_code, 400)
        sender.assert_not_awaited()
        self.assertEqual(recorded, [])

    async def test_a_disabled_section_can_still_be_tested(self):
        """Participation and configuration are separate facts, so proving a
        switched-off but configured destination is exactly the point."""
        cfg = _state(discord_webhook_url=PRIMARY, discord_notifications_enabled=False)
        sender = AsyncMock()
        result, _recorded = await self._run(cfg, sender)
        sender.assert_awaited_once_with(PRIMARY)
        self.assertTrue(result["ok"])

    async def test_a_non_discord_webhook_uses_the_ordinary_notification_client(self):
        cfg = _state(discord_webhook_url=GENERIC)
        client = AsyncMock()
        client.test = AsyncMock(return_value=True)
        with patch.object(routes, "get_settings", return_value=cfg), \
             patch.object(routes, "NotificationService", return_value=client), \
             patch.object(routes, "_record_notification_outcome",
                          new=AsyncMock(return_value={})):
            result = await routes.validate_discord()
        client.test.assert_awaited_once()
        self.assertTrue(result["ok"])

    async def test_the_test_posts_as_the_saved_identity(self):
        """No draft identity exists any more: the sender reads the canonical
        one through the notification client's own accessor."""
        source = Path(routes.__file__).read_text(encoding="utf-8")
        sender = source[source.index("async def _send_discord_test("):]
        sender = sender[:sender.index("\n@router")]
        self.assertIn("_get_discord_identity()", sender)
        self.assertNotIn("username", sender.split("payload = {")[0])


class StatisticsReportOperationTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, cfg, sender=None):
        recorded = []

        async def record(subject, fingerprint, ok):
            recorded.append((subject, fingerprint, ok))
            return notifications.notification_state(cfg)

        sender = sender or AsyncMock(return_value={"ok": True, "hours": 24, "triggered_by": "manual"})
        with patch.object(routes, "get_settings", return_value=cfg), \
             patch("services.stats.send_stats_report", new=sender), \
             patch.object(routes, "_record_notification_outcome", new=record):
            return await routes.send_statistics_report(), recorded, sender

    async def test_the_dedicated_reporting_webhook_and_saved_window_are_used(self):
        cfg = _state(stats_report_webhook_url=REPORT, discord_webhook_url=PRIMARY,
                     stats_report_window_hours=168)
        result, _recorded, sender = await self._run(cfg)

        sender.assert_awaited_once_with(hours=168, webhook_url=REPORT, triggered_by="manual")
        self.assertTrue(result["ok"])

    async def test_it_falls_back_to_the_primary_discord_webhook(self):
        cfg = _state(discord_webhook_url=PRIMARY, stats_report_window_hours=24)
        _result, _recorded, sender = await self._run(cfg)
        sender.assert_awaited_once_with(hours=24, webhook_url=PRIMARY, triggered_by="manual")

    async def test_success_records_evidence_for_the_effective_destination(self):
        cfg = _state(discord_webhook_url=PRIMARY)
        _result, recorded, _sender = await self._run(cfg)
        expected = notifications.verification_fingerprints(cfg)[notifications.REPORTING_SUBJECT]
        self.assertEqual(recorded, [(notifications.REPORTING_SUBJECT, expected, True)])

    async def test_failure_retires_the_proof_and_reports_the_real_error(self):
        cfg = _state(discord_webhook_url=PRIMARY)
        sender = AsyncMock(side_effect=RuntimeError("Reporting webhook returned HTTP 500"))
        recorded = []
        with patch.object(routes, "get_settings", return_value=cfg), \
             patch("services.stats.send_stats_report", new=sender), \
             patch.object(routes, "_record_notification_outcome",
                          new=AsyncMock(side_effect=lambda s, f, ok: recorded.append((s, ok)))):
            with self.assertRaises(HTTPException) as raised:
                await routes.send_statistics_report()
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(recorded, [(notifications.REPORTING_SUBJECT, False)])

    async def test_no_effective_destination_is_refused(self):
        with patch.object(routes, "get_settings", return_value=_state()):
            with self.assertRaises(HTTPException) as raised:
                await routes.send_statistics_report()
        self.assertEqual(raised.exception.status_code, 400)

    async def test_a_disabled_section_can_still_send_one_report(self):
        cfg = _state(discord_webhook_url=PRIMARY, stats_reporting_enabled=False)
        result, _recorded, sender = await self._run(cfg)
        sender.assert_awaited_once()
        self.assertTrue(result["ok"])

    async def test_an_out_of_range_saved_window_is_clamped_not_rejected(self):
        cfg = _state(discord_webhook_url=PRIMARY)
        object.__setattr__(cfg, "stats_report_window_hours", 0)
        _result, _recorded, sender = await self._run(cfg)
        self.assertEqual(sender.await_args.kwargs["hours"], 1)


class OperationsNeverPersistConfigurationTests(unittest.TestCase):
    def test_the_retired_draft_machinery_is_gone(self):
        source = Path(routes.__file__).read_text(encoding="utf-8")
        for retired in ("_resolve_secret_candidate", "DiscordValidationRequest",
                        "StatisticsReportDraftRequest", "_draft_discord_identity",
                        "clear_webhook", "clear_stats_report_webhook", "clear_discord_webhook"):
            self.assertNotIn(retired, source, retired)

    def test_the_legacy_second_discord_test_route_is_gone(self):
        """One UI action, one route. The old ``/settings/test-discord`` had no
        UI owner and recorded no evidence, so it was a second, silently
        divergent way to test the same thing."""
        api_routes = (Path(routes.__file__).parent / "routes.py").read_text(encoding="utf-8")
        self.assertNotIn("/settings/test-discord", api_routes)
        self.assertNotIn("async def test_discord(", api_routes)

    def test_verification_payloads_disclose_no_secret(self):
        cfg = _state(discord_webhook_url=PRIMARY, stats_report_webhook_url=REPORT)
        published = notifications.notification_state(cfg)
        self.assertEqual(set(published), {
            "discord_notifications_configured", "discord_notifications_verified",
            "stats_reporting_configured", "stats_reporting_verified"})
        self.assertNotIn(PRIMARY, str(published))
        self.assertNotIn(REPORT, str(published))
