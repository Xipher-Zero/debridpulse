import sys
import types
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = types.SimpleNamespace(
        ClientTimeout=lambda *a, **kw: None,
        ClientSession=object,
        TCPConnector=lambda **kw: None,
        FormData=object,
        ClientError=Exception,
        ServerDisconnectedError=Exception,
        ClientConnectorError=Exception,
        ClientOSError=Exception,
    )

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
        current = routes.AppSettings(
            max_concurrent_downloads=1,
            aria2_max_active_downloads=1,
        )
        fake_aria2 = SimpleNamespace(change_global_options=AsyncMock())
        application = SimpleNamespace(integration_admin=lambda _: fake_aria2, definitions=(), configuration_admission=lambda: _fake_db_context(None), configure=MagicMock(), reconcile_executions=AsyncMock())

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
        self.assertEqual(saved["cfg"].max_concurrent_downloads, 2)
        self.assertEqual(saved["cfg"].aria2_max_active_downloads, 2)
        self.assertEqual(saved["applied"].max_concurrent_downloads, 2)
        reset_services.assert_called_once()
        advance.assert_awaited_once()


class _FakeDb:
    def __init__(self, rows=None, total=0):
        self.rows = rows or []
        self.total = total
        self.fetchall_calls = []
        self.fetchone_calls = []

    async def fetchall(self, sql, params=()):
        self.fetchall_calls.append((sql, list(params)))
        return self.rows

    async def fetchone(self, sql, params=()):
        self.fetchone_calls.append((sql, list(params)))
        return {"cnt": self.total}

    async def execute(self, sql, params=()):
        return None

    async def commit(self):
        return None


@asynccontextmanager
async def _fake_db_context(db):
    yield db


class TorrentListingRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_torrents_uses_search_and_status_filters_without_limit_clause(self):
        db = _FakeDb(rows=[{"id": 1, "name": "Example"}], total=1)

        with patch("api.routes.get_db", return_value=_fake_db_context(db)):
            result = await routes.list_torrents(
                status="completed",
                search="Example",
                limit=0,
                offset=0,
                application=SimpleNamespace(
                    repository=SimpleNamespace(presentation=AsyncMock(return_value=db.rows[0])),
                    definitions=(),
                ),
            )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"], [{"id": 1, "name": "Example"}])
        sql, params = db.fetchall_calls[0]
        self.assertIn("t.status = ?", sql)
        self.assertIn("LOWER(COALESCE(t.name, '')) LIKE ?", sql)
        self.assertNotIn("LIMIT ? OFFSET ?", sql)
        self.assertEqual(
            params,
            ["completed", "%example%", "%example%", "%example%", "%example%", "%example%"],
        )
        total_sql, total_params = db.fetchone_calls[0]
        self.assertIn("SELECT COUNT(*) AS cnt FROM torrents t WHERE", total_sql)
        self.assertEqual(total_params, params)

    async def test_list_torrents_appends_limit_and_offset_when_requested(self):
        db = _FakeDb(rows=[], total=0)

        with patch("api.routes.get_db", return_value=_fake_db_context(db)):
            await routes.list_torrents(status=None, search=None, limit=250, offset=25)

        sql, params = db.fetchall_calls[0]
        self.assertIn("LIMIT ? OFFSET ?", sql)
        self.assertEqual(params[-2:], [250, 25])


class ProcessingPauseRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_individual_resume_delegates_to_control_service(self):
        cfg = routes.AppSettings(paused=False)
        application = SimpleNamespace(resume=AsyncMock())
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
    async def test_global_stat_route_returns_live_rpc_counters_in_builtin_mode(self):
        stat = {
            "download_speed": 42_000_000,
            "upload_speed": 0,
            "active": 2,
            "waiting": 1,
        }
        fake_aria2 = SimpleNamespace(
            get_global_stat=AsyncMock(return_value=stat)
        )
        cfg = SimpleNamespace(aria2_mode="builtin")
        application = SimpleNamespace(integration_admin=lambda _: fake_aria2)

        with patch("api.routes.get_settings", return_value=cfg), \
             patch("api.routes.get_application", return_value=application):
            result = await routes.aria2_global_stat(application=application)

        self.assertEqual(
            result,
            {
                "ok": True,
                "mode": "builtin",
                "external_control": False,
                **stat,
            },
        )
        fake_aria2.get_global_stat.assert_awaited_once_with()

    async def test_global_stat_route_filters_external_daemon_to_owned_jobs(self):
        foreign = SimpleNamespace(
            gid="foreign-gid",
            download_speed=90_000_000,
        )
        owned = SimpleNamespace(
            gid="owned-gid",
            download_speed=12_500_000,
        )

        fake_aria2 = SimpleNamespace(
            get_active=AsyncMock(return_value=[foreign, owned])
        )
        ownership_filter = AsyncMock(return_value=[owned])
        cfg = SimpleNamespace(aria2_mode="external")
        fake_aria2.filter_owned = ownership_filter
        application = SimpleNamespace(integration_admin=lambda _: fake_aria2)

        with patch("api.routes.get_settings", return_value=cfg), \
             patch("api.routes.get_application", return_value=application), \
             patch.object(
                 fake_aria2,
                 "filter_owned",
                 ownership_filter,
             ):
            result = await routes.aria2_global_stat(application=application)

        self.assertEqual(
            result,
            {
                "ok": True,
                "mode": "external",
                "external_control": True,
                "download_speed": 12_500_000,
                "upload_speed": 0,
                "active": 1,
                "waiting": 0,
            },
        )
        fake_aria2.get_active.assert_awaited_once_with()
        ownership_filter.assert_awaited_once_with([foreign, owned])


class DatabaseMaintenanceRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_wipe_requires_feature_toggle(self):
        cfg = SimpleNamespace(db_wipe_enabled=False, paused=True, db_backup_before_wipe=True)
        with patch("api.routes.get_settings", return_value=cfg):
            with self.assertRaises(routes.HTTPException) as exc:
                await routes.wipe_database_admin({"confirm": True})
        self.assertEqual(exc.exception.status_code, 400)

    async def test_database_wipe_requires_pause(self):
        cfg = SimpleNamespace(db_wipe_enabled=True, paused=False, db_backup_before_wipe=True)
        with patch("api.routes.get_settings", return_value=cfg):
            with self.assertRaises(routes.HTTPException) as exc:
                await routes.wipe_database_admin({"confirm": True})
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
    def __init__(self, payload_store):
        self.status = 204
        self._payload_store = payload_store

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return ""


class _FakeSession:
    last_json = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json):
        _FakeSession.last_json = {"url": url, "json": json}
        return _FakeResponse(_FakeSession.last_json)


class StatsWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_stats_report_falls_back_to_main_discord_webhook(self):
        summary = {
            "torrents_processed": 5,
            "completed": 4,
            "errors": 1,
            "success_rate": "80%",
            "total_downloaded": "10 GB",
            "avg_duration": "5m 0s",
            "total_files": 7,
            "blocked_files": 0,
            "total_retries": 2,
        }
        cfg = SimpleNamespace(
            stats_report_webhook_url="",
            discord_webhook_url="https://discord.com/api/webhooks/test",
        )
        with patch("services.stats._cfg", return_value=cfg), \
             patch("services.stats.generate_report", AsyncMock(return_value={"report": {"summary": summary}, "raw": {}})), \
             patch("services.notifications._get_discord_identity", return_value=("Webhook Bot", "")), \
             patch("services.stats.aiohttp.ClientSession", _FakeSession):
            result = await send_stats_report(hours=24, triggered_by="manual")

        self.assertTrue(result["ok"])
        self.assertTrue(result["discord"])
        self.assertEqual(_FakeSession.last_json["url"], "https://discord.com/api/webhooks/test")
        self.assertEqual(_FakeSession.last_json["json"]["username"], "Webhook Bot")
        self.assertNotIn("avatar_url", _FakeSession.last_json["json"])


if __name__ == "__main__":
    unittest.main()
