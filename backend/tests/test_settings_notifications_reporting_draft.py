import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import settings_validation_routes as routes


class SecretCandidateResolutionTests(unittest.TestCase):
    def test_typed_value_wins_over_stored_value(self):
        self.assertEqual(
            routes._resolve_secret_candidate(" typed ", "stored", clear=False),
            "typed",
        )

    def test_blank_value_preserves_stored_secret(self):
        self.assertEqual(
            routes._resolve_secret_candidate("", " stored ", clear=False),
            "stored",
        )

    def test_explicit_clear_wins_over_typed_and_stored_values(self):
        self.assertEqual(
            routes._resolve_secret_candidate("typed", "stored", clear=True),
            "",
        )


class DiscordDraftValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_test_discord_uses_stored_redacted_webhook_and_draft_identity(self):
        cfg = SimpleNamespace(discord_webhook_url="https://discord.com/api/webhooks/stored")
        payload = routes.DiscordValidationRequest(
            webhook_url="",
            clear_webhook=False,
            username="Draft Display Name",
            avatar_url="https://example.com/avatar.png",
        )

        with patch.object(routes, "get_settings", return_value=cfg), \
             patch.object(routes, "_send_discord_draft_test", new=AsyncMock()) as sender:
            result = await routes.validate_discord(payload)

        self.assertEqual(result, {"ok": True})
        sender.assert_awaited_once_with(
            "https://discord.com/api/webhooks/stored",
            "Draft Display Name",
            "https://example.com/avatar.png",
        )

    async def test_test_discord_respects_explicit_webhook_clear(self):
        cfg = SimpleNamespace(discord_webhook_url="https://discord.com/api/webhooks/stored")
        payload = routes.DiscordValidationRequest(
            webhook_url="https://discord.com/api/webhooks/typed",
            clear_webhook=True,
            username="Draft",
            avatar_url="",
        )

        with patch.object(routes, "get_settings", return_value=cfg):
            with self.assertRaises(HTTPException) as raised:
                await routes.validate_discord(payload)

        self.assertEqual(raised.exception.status_code, 400)


class StatisticsReportDraftTests(unittest.IsolatedAsyncioTestCase):
    async def _send(self, payload, cfg):
        mocked = AsyncMock(return_value={"ok": True, "hours": payload.hours, "triggered_by": "manual"})
        with patch.object(routes, "get_settings", return_value=cfg), \
             patch("services.stats.send_stats_report", new=mocked):
            result = await routes.send_stats_report_from_draft(payload)
        return result, mocked

    async def test_typed_reporting_webhook_wins_without_persisting(self):
        cfg = SimpleNamespace(
            stats_report_webhook_url="https://discord.com/api/webhooks/stored-report",
            discord_webhook_url="https://discord.com/api/webhooks/stored-primary",
        )
        payload = routes.StatisticsReportDraftRequest(
            hours=168,
            stats_report_webhook_url="https://discord.com/api/webhooks/draft-report",
            discord_webhook_url="https://discord.com/api/webhooks/draft-primary",
        )

        result, mocked = await self._send(payload, cfg)

        self.assertTrue(result["ok"])
        mocked.assert_awaited_once_with(
            hours=168,
            webhook_url="https://discord.com/api/webhooks/draft-report",
            triggered_by="manual",
        )
        self.assertEqual(cfg.stats_report_webhook_url, "https://discord.com/api/webhooks/stored-report")
        self.assertEqual(cfg.discord_webhook_url, "https://discord.com/api/webhooks/stored-primary")

    async def test_blank_redacted_reporting_webhook_uses_stored_reporting_secret(self):
        cfg = SimpleNamespace(
            stats_report_webhook_url="https://discord.com/api/webhooks/stored-report",
            discord_webhook_url="https://discord.com/api/webhooks/stored-primary",
        )
        payload = routes.StatisticsReportDraftRequest(hours=24)

        _, mocked = await self._send(payload, cfg)

        mocked.assert_awaited_once_with(
            hours=24,
            webhook_url="https://discord.com/api/webhooks/stored-report",
            triggered_by="manual",
        )

    async def test_cleared_reporting_webhook_falls_back_to_typed_primary_discord_webhook(self):
        cfg = SimpleNamespace(
            stats_report_webhook_url="https://discord.com/api/webhooks/stored-report",
            discord_webhook_url="https://discord.com/api/webhooks/stored-primary",
        )
        payload = routes.StatisticsReportDraftRequest(
            hours=720,
            clear_stats_report_webhook=True,
            discord_webhook_url="https://discord.com/api/webhooks/draft-primary",
        )

        _, mocked = await self._send(payload, cfg)

        mocked.assert_awaited_once_with(
            hours=720,
            webhook_url="https://discord.com/api/webhooks/draft-primary",
            triggered_by="manual",
        )

    async def test_cleared_reporting_webhook_falls_back_to_stored_primary_when_unchanged(self):
        cfg = SimpleNamespace(
            stats_report_webhook_url="https://discord.com/api/webhooks/stored-report",
            discord_webhook_url="https://discord.com/api/webhooks/stored-primary",
        )
        payload = routes.StatisticsReportDraftRequest(
            hours=24,
            clear_stats_report_webhook=True,
        )

        _, mocked = await self._send(payload, cfg)

        mocked.assert_awaited_once_with(
            hours=24,
            webhook_url="https://discord.com/api/webhooks/stored-primary",
            triggered_by="manual",
        )

    async def test_explicit_clear_of_both_destinations_prevents_send(self):
        cfg = SimpleNamespace(
            stats_report_webhook_url="https://discord.com/api/webhooks/stored-report",
            discord_webhook_url="https://discord.com/api/webhooks/stored-primary",
        )
        payload = routes.StatisticsReportDraftRequest(
            clear_stats_report_webhook=True,
            clear_discord_webhook=True,
        )

        with patch.object(routes, "get_settings", return_value=cfg):
            with self.assertRaises(HTTPException) as raised:
                await routes.send_stats_report_from_draft(payload)

        self.assertEqual(raised.exception.status_code, 400)


class DraftIdentityTests(unittest.TestCase):
    def test_draft_identity_uses_app_name_and_filters_unsupported_avatar(self):
        name, avatar = routes._draft_discord_identity("", "https://example.com/avatar.svg")
        self.assertEqual(name, "DebridPulse")
        self.assertEqual(avatar, "")
