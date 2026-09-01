import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import AppSettings
from core.scheduler import _coerce_int_setting, _stats_report_window_hours
import providers.alldebrid.rate_limit as rate_limit


class SchedulerSettingsTests(unittest.TestCase):
    def test_coerce_int_setting_preserves_zero(self):
        self.assertEqual(_coerce_int_setting(0, 10), 0)

    def test_coerce_int_setting_uses_default_for_none(self):
        self.assertEqual(_coerce_int_setting(None, 10), 10)

    def test_coerce_int_setting_uses_default_for_invalid(self):
        self.assertEqual(_coerce_int_setting("invalid", 10), 10)

    def test_stats_report_window_uses_configured_value(self):
        self.assertEqual(_stats_report_window_hours(types.SimpleNamespace(stats_report_window_hours=168)), 168)

    def test_stats_report_window_falls_back_to_default(self):
        self.assertEqual(_stats_report_window_hours(types.SimpleNamespace(stats_report_window_hours=None)), 24)


class AllDebridRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        rate_limit._alldebrid_rate_limiter = rate_limit.TokenBucketRateLimiter(rate=60, window=60.0)

    async def test_rate_limit_zero_means_effectively_unlimited(self):
        cfg = types.SimpleNamespace(alldebrid_rate_limit_per_minute=0)
        with patch("providers.alldebrid.rate_limit.get_settings", return_value=cfg):
            limiter = await rate_limit.get_alldebrid_rate_limiter()
        self.assertGreaterEqual(limiter._rate, 1_000_000)

    async def test_rate_limit_positive_value_is_respected(self):
        cfg = types.SimpleNamespace(alldebrid_rate_limit_per_minute=12)
        with patch("providers.alldebrid.rate_limit.get_settings", return_value=cfg):
            limiter = await rate_limit.get_alldebrid_rate_limiter()
        self.assertEqual(limiter._rate, 12)


class SQLiteOnlySettingsTests(unittest.TestCase):
    def test_server_database_fields_are_removed(self):
        cfg = AppSettings()
        for field in ("db_type", "postgres_host", "postgres_port", "postgres_db",
                      "postgres_user", "postgres_password", "postgres_schema",
                      "postgres_ssl", "postgres_application_name"):
            self.assertFalse(hasattr(cfg, field), field)

    def test_delivery_is_aria2_only(self):
        cfg = AppSettings()
        self.assertEqual(cfg.download_client, "aria2")
        self.assertFalse(hasattr(cfg, "symlink_path"))


class SettingsFrontendContractTests(unittest.TestCase):
    @staticmethod
    def settings_js():
        return (
            Path(__file__).resolve().parents[2]
            / "frontend"
            / "static"
            / "ui-settings-page.js"
        ).read_text()

    def test_active_settings_tab_lookup_is_owned_by_clean_state(self):
        js = self.settings_js()
        self.assertIn("activeTab: 'sources'", js)
        self.assertIn("function activateTab(name)", js)
        self.assertIn("state.activeTab = name;", js)
        self.assertNotIn("document.querySelector('.stab.active')?.dataset.tab", js)

    def test_database_scope_is_sqlite_only(self):
        js = self.settings_js()
        self.assertIn("Data & Maintenance", js)
        self.assertIn("Database Destructive Actions", js)
        for stale in (
            "postgres_host",
            "postgres_password",
            "btn-test-postgres",
            "PostgreSQL (external)",
            "docs/postgresql.md",
        ):
            self.assertNotIn(stale, js)

    def test_stalled_download_recovery_remains_download_scoped(self):
        js = self.settings_js()
        downloads = js.split("function downloadsPanel", 1)[1].split("function extractionPanel", 1)[0]
        sources = js.split("function sourcesPanel", 1)[1].split("function downloadsPanel", 1)[0]
        self.assertNotIn("stuck_download_timeout_hours", sources)
        self.assertIn("stuck_download_timeout_hours", downloads)
        self.assertIn("Download Safety & Recovery", downloads)


if __name__ == "__main__":
    unittest.main()
