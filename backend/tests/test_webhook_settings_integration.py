import sys
import types
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# aiohttp is a real runtime dependency and is installed, so this module must
# not substitute a namespace for it: whichever test file imported first would
# then decide whether every LATER module sees the real package, and one that
# needs `aiohttp.abc` fails to import at all. The cases below patch the ONE
# transport's session (`services.notifications.aiohttp.ClientSession`) instead,
# which reaches no network and leaves the module itself intact.

if "aiofiles" not in sys.modules:
    sys.modules["aiofiles"] = types.SimpleNamespace(open=lambda *a, **kw: None)

if "aiosqlite" not in sys.modules:
    sys.modules["aiosqlite"] = types.SimpleNamespace(
        Connection=object,
        Row=object,
        connect=lambda *a, **kw: None,
    )

if "multipart" not in sys.modules:
    multipart_mod = types.ModuleType("multipart")
    multipart_mod.__version__ = "0.0-test"
    multipart_sub = types.ModuleType("multipart.multipart")
    multipart_sub.parse_options_header = lambda value: ("form-data", {})
    sys.modules["multipart"] = multipart_mod
    sys.modules["multipart.multipart"] = multipart_sub

from api import routes
from core.scheduler import _has_reporting_webhook
from executors.aria2.definition import definition as aria2_definition
import services.notifications as notifications_module
import services.stats as stats_module
from services.stats import send_stats_report


class RouteHelperTests(unittest.TestCase):
    def test_public_base_url_prefers_env_override(self):
        request = SimpleNamespace(
            headers={"host": "internal.local:8080"},
            url=SimpleNamespace(scheme="http"),
        )
        with patch.dict("os.environ", {"PUBLIC_BASE_URL": "https://example.com/base"}, clear=False):
            self.assertEqual(routes._public_base_url(request), "https://example.com/base")

    def test_avatar_reachability_warning_for_private_url(self):
        warning = routes._avatar_reachability_warning("http://127.0.0.1:8080/api/avatar")
        self.assertIn("PUBLIC_BASE_URL", warning)

    def test_avatar_reachability_warning_empty_for_public_url(self):
        warning = routes._avatar_reachability_warning("https://example.com/api/avatar")
        self.assertEqual(warning, "")

class SchedulerWebhookTests(unittest.TestCase):
    def test_reporting_webhook_accepts_discord_fallback(self):
        cfg = SimpleNamespace(
            stats_report_webhook_url="",
            discord_webhook_url="https://discord.com/api/webhooks/test",
        )
        self.assertTrue(_has_reporting_webhook(cfg))

    def test_reporting_webhook_false_when_both_empty(self):
        cfg = SimpleNamespace(stats_report_webhook_url="", discord_webhook_url="")
        self.assertFalse(_has_reporting_webhook(cfg))


class SettingsSaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_settings_sanitises_before_save(self):
        saved = {}

        def fake_save(cfg):
            saved["cfg"] = cfg

        def fake_apply(cfg):
            saved["applied"] = cfg

        with patch("api.routes.save_settings", side_effect=fake_save), \
             patch("api.routes.apply_settings", side_effect=fake_apply), \
             patch.object(routes.aria2_runtime, "ensure_started", AsyncMock()), \
             patch.object(routes.aria2_runtime, "restart", AsyncMock()):
            result = await routes.update_settings(
                routes.SettingsUpdate(discord_avatar_url="data:image/png;base64,abc123"), application=SimpleNamespace(definitions=(), configuration_admission=lambda: _fake_db_context(None), validate_configuration=AsyncMock(), configure=MagicMock(), integration_admin=MagicMock())
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["discord_avatar_url"], "")
        self.assertEqual(saved["cfg"].discord_avatar_url, "")
        self.assertEqual(saved["applied"].discord_avatar_url, "")

    async def test_update_settings_persists_reporting_window(self):
        saved = {}

        def fake_save(cfg):
            saved["cfg"] = cfg

        def fake_apply(cfg):
            saved["applied"] = cfg

        with patch("api.routes.save_settings", side_effect=fake_save), \
             patch("api.routes.apply_settings", side_effect=fake_apply), \
             patch.object(routes.aria2_runtime, "ensure_started", AsyncMock()), \
             patch.object(routes.aria2_runtime, "restart", AsyncMock()):
            result = await routes.update_settings(
                routes.SettingsUpdate(
                    stats_report_interval_hours=12,
                    stats_report_window_hours=168,
                    stats_report_webhook_url="https://discord.com/api/webhooks/test",
                ), application=SimpleNamespace(definitions=(), configuration_admission=lambda: _fake_db_context(None), validate_configuration=AsyncMock(), configure=MagicMock(), integration_admin=MagicMock())
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["stats_report_interval_hours"], 12)
        self.assertEqual(result["stats_report_window_hours"], 168)
        self.assertEqual(result["stats_report_webhook_url"], "")
        self.assertEqual(saved["cfg"].stats_report_interval_hours, 12)
        self.assertEqual(saved["cfg"].stats_report_window_hours, 168)
        self.assertEqual(saved["cfg"].stats_report_webhook_url, "https://discord.com/api/webhooks/test")

    async def test_aria2_global_options_applies_slot_change_to_live_settings(self):
        saved = {}
        from transfers.settings import TransferSettings
        current = routes.AppSettings(transfer_policy=TransferSettings(max_concurrent_executions=1))
        fake_aria2 = SimpleNamespace(change_global_options=AsyncMock(), apply_memory_tuning=AsyncMock())
        application = SimpleNamespace(
            integration_admin=lambda _: fake_aria2,
            # DP 1.0.13 work item A: a canonical configuration mutation wakes the
            # neutral lifecycle/routing maintenance that has to act on it.
            notify_applicability_changed=lambda _identity: None,
            apply_integration_configuration=AsyncMock(return_value=None), definitions=(),
            application_operation=lambda: _fake_db_context(None), configure=MagicMock(),
            reconcile_executions=AsyncMock())

        def fake_save(cfg):
            saved["cfg"] = cfg

        def fake_apply(cfg):
            saved["applied"] = cfg

        with patch("api.routes.get_settings", return_value=current), \
             patch("api.routes.load_settings", return_value=current), \
             patch("api.routes.save_settings", side_effect=fake_save), \
             patch("api.routes.apply_settings", side_effect=fake_apply), \
             patch("api.routes.get_application", return_value=application), \
             patch.object(application, "configure", MagicMock()) as reset_services, \
             patch.object(application, "reconcile_executions", AsyncMock()) as advance:
            result = await routes.aria2_set_global_options({"max_concurrent_downloads": 2}, application=application)

        self.assertEqual(result["applied"]["max-concurrent-downloads"], "2")
        self.assertEqual(saved["cfg"].transfer_policy.max_concurrent_executions, 2)
        # Gate 9 revision-4 rejection finding 5: this route now forwards to
        # the SAME canonical patch_transfer_policy implementation the neutral
        # UI calls directly (specification sections 9.2, 9.7) instead of
        # independently persisting the legacy flat alias itself -- so
        # max_concurrent_downloads/aria2_max_active_downloads are neither a
        # second writable authority NOR dual-written by this write.
        self.assertNotIn("max_concurrent_downloads", saved["cfg"].model_dump())
        self.assertNotIn("aria2_max_active_downloads", saved["cfg"].model_dump())
        self.assertEqual(saved["applied"].transfer_policy.max_concurrent_executions, 2)
        reset_services.assert_called_once()
        advance.assert_awaited_once()
        # Universal Executor Leveling: the canonical route this edge forwards
        # to never projects global concurrency into an executor -- core
        # admission is its only owner.
        fake_aria2.apply_memory_tuning.assert_not_awaited()

    async def test_aria2_global_options_upload_speed_persists_before_native_apply_and_reconfigures(self):
        """Gate 9 revision-5 rejection finding 3: the legacy upload-speed
        compatibility path must migrate the desired value into canonical
        ``integrations.aria2.max_upload_limit`` (not just the legacy flat
        field) AND call ``application.configure()`` so the injected
        ``Aria2RuntimeConfiguration`` snapshot ``Aria2Administration.apply_memory_tuning()``
        consumes is refreshed -- otherwise the next unrelated tuning
        apply/restart would silently revert a live native change back to a
        stale injected value."""
        saved = {}
        current = routes.AppSettings()
        fake_aria2 = SimpleNamespace(change_global_options=AsyncMock())
        application = SimpleNamespace(
            integration_admin=lambda _: fake_aria2, apply_integration_configuration=AsyncMock(return_value=None), definitions=(aria2_definition,),
            notify_applicability_changed=lambda _identity: None,
            application_operation=lambda: _fake_db_context(None), configure=MagicMock(),
            reconcile_executions=AsyncMock(), validate_configuration=AsyncMock(),
        )
        fake_aria2.apply_memory_tuning = AsyncMock()

        def fake_save(cfg):
            saved["cfg"] = cfg

        def fake_apply(cfg):
            saved["applied"] = cfg

        with patch("api.routes.get_settings", return_value=current), \
             patch("api.routes.load_settings", return_value=current), \
             patch("api.routes.save_settings", side_effect=fake_save), \
             patch("api.routes.apply_settings", side_effect=fake_apply), \
             patch("api.routes.get_application", return_value=application), \
             patch("api.routes.aria2_runtime.ensure_started", AsyncMock()):
            result = await routes.aria2_set_global_options({"max_upload_speed": 750_000}, application=application)

        self.assertTrue(result["ok"])
        self.assertEqual(result["applied"]["max-overall-upload-limit"], "750000")
        # Canonical integration namespace received the migrated value.
        self.assertEqual(saved["cfg"].integrations["aria2"].options["max_upload_limit"], 750_000)
        fake_aria2.change_global_options.assert_awaited_once_with({"max-overall-upload-limit": "750000"})
        application.configure.assert_called_once()

    async def test_aria2_global_options_concurrency_change_touches_no_executor_administration(self):
        """Gate 9 revision-6 rejection finding 3 required this legacy edge to
        propagate the canonical route's apply truth. Under Universal Executor
        Leveling that truth is simply the durable policy: global concurrency
        is enforced by core admission alone, so even an unreachable executor
        administration surface is never consulted and cannot fail the call."""
        saved = {}
        from transfers.settings import TransferSettings
        current = routes.AppSettings(transfer_policy=TransferSettings(max_concurrent_executions=1))
        fake_aria2 = SimpleNamespace(
            change_global_options=AsyncMock(),
            apply_memory_tuning=AsyncMock(side_effect=RuntimeError("daemon unreachable")),
        )
        application = SimpleNamespace(
            integration_admin=lambda _: fake_aria2, apply_integration_configuration=AsyncMock(return_value=None), definitions=(),
            notify_applicability_changed=lambda _identity: None,
            application_operation=lambda: _fake_db_context(None), configure=MagicMock(),
            reconcile_executions=AsyncMock(),
        )

        def fake_save(cfg):
            saved["cfg"] = cfg

        def fake_apply(cfg):
            saved["applied"] = cfg

        with patch("api.routes.get_settings", return_value=current), \
             patch("api.routes.load_settings", return_value=current), \
             patch("api.routes.save_settings", side_effect=fake_save), \
             patch("api.routes.apply_settings", side_effect=fake_apply), \
             patch("api.routes.get_application", return_value=application):
            result = await routes.aria2_set_global_options({"max_concurrent_downloads": 2}, application=application)

        self.assertTrue(result["ok"])
        fake_aria2.apply_memory_tuning.assert_not_awaited()
        # The durable desired value still persists regardless of the native
        # apply outcome (specification section 2.7: configured/effective).
        self.assertEqual(saved["cfg"].transfer_policy.max_concurrent_executions, 2)
        self.assertEqual(result["applied"]["max-concurrent-downloads"], "2")


@asynccontextmanager
async def _fake_db_context(db):
    yield db


# GET /api/torrents collection listing behavior (search/status filters, LIMIT/
# OFFSET gating) is owned by api.operational_downloads.list_operational_torrents
# and covered in tests/test_canonical_http_route_ownership.py. The legacy
# api.routes.list_torrents handler it superseded has been retired.


class ProcessingPauseRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_individual_resume_delegates_to_control_service(self):
        cfg = routes.AppSettings()
        application = SimpleNamespace(resume=AsyncMock(),
            repository=SimpleNamespace(globally_paused=AsyncMock(return_value=False)))
        with patch.object(application, "resume", AsyncMock()) as resume, \
             patch("api.routes.get_settings", return_value=cfg), \
             patch("api.routes.save_settings") as save, \
             patch("api.routes.apply_settings") as apply:
            result = await routes.resume_torrent(73, application=application)

        self.assertEqual(result, {"ok": True, "paused": False})
        resume.assert_awaited_once_with(73)
        save.assert_not_called()
        apply.assert_not_called()


class Aria2LiveStatRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_stat_route_returns_live_rpc_counters(self):
        stat = {
            "download_speed": 42_000_000,
            "upload_speed": 0,
            "active": 2,
            "waiting": 1,
        }
        fake_aria2 = SimpleNamespace(
            get_global_stat=AsyncMock(return_value=stat)
        )
        application = SimpleNamespace(integration_admin=lambda _: fake_aria2)

        result = await routes.aria2_global_stat(application=application)

        self.assertEqual(result, {"ok": True, **stat})
        fake_aria2.get_global_stat.assert_awaited_once_with()


class DatabaseMaintenanceRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_wipe_requires_feature_toggle(self):
        cfg = SimpleNamespace(db_wipe_enabled=False, db_backup_before_wipe=True)
        application = SimpleNamespace(repository=SimpleNamespace(globally_paused=AsyncMock(return_value=True)))
        with patch("api.routes.get_settings", return_value=cfg):
            with self.assertRaises(routes.HTTPException) as exc:
                await routes.wipe_database_admin({"confirm": True}, application=application)
        self.assertEqual(exc.exception.status_code, 400)

    async def test_database_wipe_requires_pause(self):
        cfg = SimpleNamespace(db_wipe_enabled=True, db_backup_before_wipe=True)
        application = SimpleNamespace(repository=SimpleNamespace(globally_paused=AsyncMock(return_value=False)))
        with patch("api.routes.get_settings", return_value=cfg):
            with self.assertRaises(routes.HTTPException) as exc:
                await routes.wipe_database_admin({"confirm": True}, application=application)
        self.assertEqual(exc.exception.status_code, 409)


class DatabaseBackupServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_backup_serializes_datetime_rows(self):
        from services import db_maintenance
        temp_root = Path(__file__).resolve().parent / "_tmp_db_backup"
        if temp_root.exists():
            import shutil
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)

        cfg = SimpleNamespace(
            db_backup_enabled=True,
            db_backup_folder=str(temp_root),
            db_backup_keep_days=7,
        )
        row = {
            "id": 1,
            "created_at": datetime(2026, 4, 21, 12, 34, 56, tzinfo=timezone.utc),
        }

        class _BackupDb:
            async def execute(self, *args):
                pass

            async def fetchall(self, sql, params=()):
                if "sqlite_master" in sql:
                    return [{"name": table} for table in db_maintenance.TABLES]
                return [row]

        @asynccontextmanager
        async def _db_ctx():
            yield _BackupDb()

        try:
            with patch("services.db_maintenance.get_settings", return_value=cfg), \
                 patch("services.db_maintenance.get_db", return_value=_db_ctx()):
                result = await db_maintenance.run_database_backup()

            self.assertEqual(result["errors"], [])
            exported = Path(result["file"]).read_text(encoding="utf-8")
            self.assertIn("2026-04-21T12:34:56+00:00", exported)
        finally:
            if temp_root.exists():
                import shutil
                shutil.rmtree(temp_root)


class _FakeResponse:
    def __init__(self, status=204):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return ""

    async def json(self, content_type=None):
        return {}


class _FakeSession:
    """Stands in for the ONE transport's HTTP session, never Statistics' own."""

    posts: list = []
    status = 204

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json):
        _FakeSession.posts.append({"url": url, "json": json})
        return _FakeResponse(_FakeSession.status)


SUMMARY = {
    "torrents_processed": 5, "completed": 4, "errors": 1, "success_rate": "80%",
    "total_downloaded": "10 GB", "avg_duration": "5m 0s", "total_files": 7,
    "blocked_files": 0, "total_retries": 2,
}


class StatsReportTransportTests(unittest.IsolatedAsyncioTestCase):
    """Statistics reports travel on the SAME webhook transport every other
    notification uses. Each case patches ``services.notifications``' session --
    the one transport -- because after the correction there is no other."""

    def setUp(self):
        _FakeSession.posts = []
        _FakeSession.status = 204

    async def _send(self, cfg):
        with patch("services.stats._cfg", return_value=cfg), \
             patch("services.stats.generate_report",
                   AsyncMock(return_value={"report": {"summary": SUMMARY}, "raw": {}})), \
             patch("services.notifications._get_discord_identity", return_value=("Webhook Bot", "")), \
             patch("services.notifications.aiohttp.ClientSession", _FakeSession):
            return await send_stats_report(hours=24, triggered_by="manual")

    async def test_report_falls_back_to_the_primary_discord_webhook(self):
        cfg = SimpleNamespace(stats_report_webhook_url="",
                              discord_webhook_url="https://discord.com/api/webhooks/test")
        result = await self._send(cfg)

        self.assertTrue(result["ok"])
        self.assertEqual(len(_FakeSession.posts), 1)
        sent = _FakeSession.posts[0]
        self.assertEqual(sent["url"], "https://discord.com/api/webhooks/test")
        # Shaped by the shared transport, exactly as any Discord notification is.
        self.assertEqual(sent["json"]["username"], "Webhook Bot")
        self.assertNotIn("avatar_url", sent["json"])
        self.assertIn("embeds", sent["json"])
        self.assertEqual(sent["json"]["embeds"][0]["title"], "📊 Statistics Report — Last 24h")

    async def test_a_dedicated_reporting_webhook_wins_over_the_fallback(self):
        cfg = SimpleNamespace(stats_report_webhook_url="https://discord.com/api/webhooks/report",
                              discord_webhook_url="https://discord.com/api/webhooks/primary")
        await self._send(cfg)
        self.assertEqual(_FakeSession.posts[0]["url"], "https://discord.com/api/webhooks/report")

    async def test_a_generic_endpoint_receives_the_shared_generic_shape(self):
        """The Fluxer case. It works for the same reason an ordinary
        notification does: one sender decided the dialect."""
        cfg = SimpleNamespace(stats_report_webhook_url="https://fluxer.example.com/hooks/abc",
                              discord_webhook_url="")
        await self._send(cfg)

        payload = _FakeSession.posts[0]["json"]
        # The transport's own neutral envelope...
        for key in ("event", "event_key", "severity", "app", "description", "fields", "embed"):
            self.assertIn(key, payload)
        self.assertEqual(payload["fields"]["Torrents"], "5")
        self.assertEqual(payload["fields"]["Triggered"], "manual")
        # ...and not the retired Statistics-only envelope.
        for retired in ("report", "raw", "source", "triggered_by"):
            self.assertNotIn(retired, payload)

    async def test_the_same_sender_serves_an_ordinary_notification(self):
        """Parity, without a provider branch anywhere: an ordinary event and a
        report reach the same generic endpoint through the same code."""
        from services.notifications import NotificationService as Client

        with patch("services.notifications._get_discord_identity", return_value=("Webhook Bot", "")), \
             patch("services.notifications.aiohttp.ClientSession", _FakeSession):
            await Client("https://fluxer.example.com/hooks/abc").send_complete("payload.bin")
        ordinary = _FakeSession.posts[0]["json"]

        _FakeSession.posts = []
        await self._send(SimpleNamespace(stats_report_webhook_url="https://fluxer.example.com/hooks/abc",
                                         discord_webhook_url=""))
        report = _FakeSession.posts[0]["json"]
        self.assertEqual(set(ordinary), set(report))

    async def test_a_refused_delivery_surfaces_the_transport_result(self):
        _FakeSession.status = 500
        cfg = SimpleNamespace(stats_report_webhook_url="",
                              discord_webhook_url="https://discord.com/api/webhooks/test")
        with self.assertRaises(RuntimeError) as raised:
            await self._send(cfg)
        # Sanitised by the sender; the destination is never echoed back.
        self.assertNotIn("discord.com", str(raised.exception))

    async def test_no_reporting_destination_is_refused_before_any_send(self):
        cfg = SimpleNamespace(stats_report_webhook_url="", discord_webhook_url="")
        with patch("services.stats._cfg", return_value=cfg):
            with self.assertRaises(ValueError):
                await send_stats_report(hours=24)
        self.assertEqual(_FakeSession.posts, [])


class StatsOwnsNoTransportTests(unittest.TestCase):
    def test_statistics_holds_no_http_client_or_endpoint_classifier(self):
        source = Path(stats_module.__file__).read_text(encoding="utf-8")
        # No transport: no client, no endpoint classifier, no HTTP handling.
        for retired in ("aiohttp", "ClientSession", "_is_discord_webhook", "urlparse",
                        "session.post", "avatar_url", "embeds", "response.status"):
            self.assertNotIn(retired, source, retired)
        # ...and none of the retired Statistics-only wire envelope.
        for retired in ('"event": "stats_report"', '"source": "debridpulse"',
                        '"triggered_by": triggered_by,'):
            self.assertNotIn(retired, source, retired)
        # Report CONTENT generation stays here; only delivery moved.
        self.assertIn("async def generate_report(", source)
        self.assertIn('"raw": metrics,', source)
        self.assertIn("NotificationService(url).send(", source)

    def test_exactly_one_webhook_transport_owner_posts(self):
        """Only the transport opens a session for a webhook."""
        notifications = Path(notifications_module.__file__).read_text(encoding="utf-8")
        self.assertEqual(notifications.count("aiohttp.ClientSession("), 1)
        self.assertNotIn("aiohttp", Path(stats_module.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
