"""Focused contract tests for the internal direct/debrid-link workflow."""

import asyncio
import struct
import sys
import tempfile
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The scratch verification environment intentionally has no runtime wheels.
# Match the upstream suite's lightweight import stubs.
if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = types.SimpleNamespace(
        ClientTimeout=lambda *args, **kwargs: None,
        ClientSession=object,
        TCPConnector=lambda **kwargs: None,
        FormData=object,
        ClientError=Exception,
        ServerDisconnectedError=Exception,
        ClientConnectorError=Exception,
        ClientOSError=Exception,
    )
if "aiofiles" not in sys.modules:
    sys.modules["aiofiles"] = types.SimpleNamespace(open=lambda *args, **kwargs: None)
if "aiosqlite" not in sys.modules:
    sys.modules["aiosqlite"] = types.SimpleNamespace(
        Connection=object,
        Row=object,
        connect=lambda *args, **kwargs: None,
    )

from db.database import _SCHEMA_COLUMNS_FILES
from services.alldebrid import AllDebridAPIError, AllDebridService
from services.manager_v2 import (
    TorrentManager,
    _retry_async,
    direct_link_collection_name,
    direct_link_filename,
    normalize_direct_links,
)


class DirectLinkInputTests(unittest.TestCase):
    def test_normalizes_deduplicates_and_preserves_order(self):
        links = normalize_direct_links(
            [
                " https://host.invalid/a ",
                "http://host.invalid/b",
                "https://host.invalid/a",
                "",
            ]
        )
        self.assertEqual(
            links,
            ["https://host.invalid/a", "http://host.invalid/b"],
        )

    def test_rejects_non_http_input(self):
        with self.assertRaisesRegex(ValueError, "Invalid debrid link"):
            normalize_direct_links(["magnet:?xt=urn:btih:abc"])

    def test_caps_each_batch_at_one_hundred_links(self):
        with self.assertRaisesRegex(ValueError, "maximum of 100"):
            normalize_direct_links(
                [f"https://host.invalid/file-{index}" for index in range(101)]
            )

    def test_derives_safe_filename(self):
        self.assertEqual(
            direct_link_filename("https://host.invalid/files/My%20File?.zip"),
            "My File",
        )
        self.assertEqual(direct_link_filename("https://host.invalid"), "host.invalid")
        self.assertEqual(
            direct_link_filename(
                "https://1fichier.com/?AbCdEf123&af=2701919"
            ),
            "1fichier.com - AbCdEf123",
        )
        self.assertEqual(
            direct_link_filename("https://host.invalid/?token=secret&auth=value"),
            "host.invalid",
        )

    def test_collection_name_uses_resolved_multipart_base(self):
        links = [
            "https://1fichier.com/?part1&af=2701919",
            "https://1fichier.com/?part2&af=2701919",
            "https://1fichier.com/?part3&af=2701919",
        ]
        self.assertEqual(
            direct_link_collection_name(
                ["sc44610-Dispatc.part3.rar"],
                links,
            ),
            "sc44610-Dispatc (3 links)",
        )

    def test_collection_name_does_not_invent_common_name(self):
        links = [
            "https://host.invalid/a",
            "https://host.invalid/b",
            "https://host.invalid/c",
        ]
        self.assertEqual(
            direct_link_collection_name(
                ["alpha.mkv", "beta.srt"],
                links,
            ),
            "alpha.mkv + 2 more",
        )

    def test_collection_name_falls_back_to_source_identifier(self):
        links = [
            "https://1fichier.com/?xo3nibyjy94ymn937127&af=2701919",
            "https://1fichier.com/?y4eawl85julqc81h1xq0&af=2701919",
        ]
        self.assertEqual(
            direct_link_collection_name([], links),
            "1fichier.com - xo3nibyjy94ymn937127 + 1 more",
        )

    def test_schema_migrates_original_source_url(self):
        self.assertIn(("source_url", "TEXT"), _SCHEMA_COLUMNS_FILES)

    def test_removed_source_can_reuse_its_original_target_path(self):
        manager = TorrentManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            existing = root / "archive.zip"
            existing.write_bytes(b"previous download")

            protected = manager._unique_direct_link_path(
                root, "archive.zip", set()
            )
            reusable = manager._unique_direct_link_path(
                root,
                "archive.zip",
                set(),
                reuse_existing=True,
            )

        self.assertEqual(protected.name, "archive (2).zip")
        self.assertEqual(reusable.name, "archive.zip")

    def test_live_owner_blocks_deleted_history_path_reuse(self):
        manager = TorrentManager()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            existing = root / "archive.zip"
            existing.write_bytes(b"current transfer payload")
            live_paths = {str(existing).lower()}

            selected = manager._unique_direct_link_path(
                root,
                "archive.zip",
                live_paths,
                reuse_existing=True,
            )

        self.assertEqual(selected.name, "archive (2).zip")

    def test_direct_link_preparation_seeds_non_deleted_live_paths(self):
        repo_backend = Path(__file__).resolve().parents[1]
        manager_source = (repo_backend / "services/manager_v2.py").read_text()
        self.assertIn("protected_live_paths: Set[str] = set()", manager_source)
        self.assertIn("WHERE t.status!='deleted'", manager_source)
        self.assertIn("AND t.id!=?", manager_source)
        self.assertIn(
            "reserved_paths: Set[str] = set(protected_live_paths)",
            manager_source,
        )


class DelayedAllDebridTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_immediate_unlocked_link(self):
        service = AllDebridService("test-key")
        service._post = AsyncMock(
            return_value={"link": "https://download.invalid/file", "filename": "file"}
        )
        result = await service.unlock_link("https://host.invalid/file")
        self.assertEqual(result["link"], "https://download.invalid/file")
        self.assertEqual(service._post.await_count, 1)

    async def test_polls_delayed_generation_at_documented_interval(self):
        service = AllDebridService("test-key")
        service._post = AsyncMock(
            side_effect=[
                {"delayed": "job-42", "filename": "archive.zip", "filesize": 123},
                {"status": 1},
                {"status": 2, "link": "https://download.invalid/archive.zip"},
            ]
        )
        with patch("services.alldebrid.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await service.unlock_link("https://host.invalid/archive")

        self.assertEqual(result["filename"], "archive.zip")
        self.assertEqual(result["filesize"], 123)
        self.assertEqual(result["link"], "https://download.invalid/archive.zip")
        self.assertEqual(sleep.await_args_list[0].args, (5,))
        self.assertEqual(service._post.await_count, 3)


class MissingDirectLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_link_down_keeps_code_and_is_not_retried(self):
        calls = 0

        async def missing_link():
            nonlocal calls
            calls += 1
            raise AllDebridAPIError(
                "LINK_DOWN",
                "This link is not available on the file hoster website",
            )

        with self.assertRaises(AllDebridAPIError) as caught:
            await _retry_async(
                missing_link,
                attempts=3,
                retry_if=lambda exc: not (
                    isinstance(exc, AllDebridAPIError)
                    and exc.code == "LINK_DOWN"
                ),
            )

        self.assertEqual(calls, 1)
        self.assertEqual(caught.exception.code, "LINK_DOWN")
        self.assertIn("LINK_DOWN", str(caught.exception))


class DirectLinkTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_submission_persists_parent_and_schedules_generation(self):
        class FakeDb:
            def __init__(self):
                self.statements = []

            async def execute_returning_id(self, sql, params=()):
                self.statements.append((sql, params))
                return 42

            async def execute(self, sql, params=()):
                self.statements.append((sql, params))

            async def fetchone(self, sql, params=()):
                return {
                    "id": 42,
                    "name": "sample.zip",
                    "status": "processing",
                    "source": "direct_link",
                }

            async def commit(self):
                return None

        fake_db = FakeDb()

        @asynccontextmanager
        async def fake_get_db():
            yield fake_db

        manager = TorrentManager()
        settings = SimpleNamespace(paused=False, alldebrid_api_key="configured")
        with patch("services.manager_v2.get_settings", return_value=settings), patch(
            "services.manager_v2.get_db", fake_get_db
        ), patch.object(
            manager, "_broadcast_direct_link_update", new=AsyncMock()
        ), patch.object(
            manager, "_schedule_direct_link_collection", new=MagicMock()
        ) as schedule:
            result = await manager.add_direct_links(
                ["https://host.invalid/sample.zip"]
            )

        self.assertEqual(result["accepted_links"], 1)
        self.assertEqual(result["source"], "direct_link")
        insert_sql, insert_params = fake_db.statements[0]
        self.assertIn("INSERT INTO torrents", insert_sql)
        self.assertIn("direct_link", insert_params)
        schedule.assert_called_once_with(42, ["https://host.invalid/sample.zip"])


class DashboardContractTests(unittest.TestCase):
    def test_dashboard_and_downloads_page_match_unified_transfer_ui(self):
        repo_root = Path(__file__).resolve().parents[2]
        html = (repo_root / "frontend/static/index.html").read_text()
        js = (repo_root / "frontend/static/app.js").read_text()
        unified_heading = "⬇️ Add Links, Magnets, or Torrent File"
        self.assertIn(unified_heading, html)
        self.assertIn('id="q-transfer-input" rows="2"', html)
        self.assertIn('id="btn-add-transfer"', html)
        self.assertNotIn('id="q-debrid-links"', html)
        self.assertNotIn('id="q-magnet"', html)
        self.assertNotIn('data-view="aria2queue"', html)
        self.assertNotIn('id="t-magnet"', html)
        self.assertIn('<span class="nav-label">Downloads</span>', html)
        self.assertIn('id="torrent-card-title">All Downloads</span>', html)
        self.assertRegex(
            html,
            r'<script src="/app\.js\?v=\d+" defer></script>',
        )
        self.assertIn("function classifyDashboardEntries", js)
        self.assertIn("async function addDashboardEntries()", js)
        self.assertIn("'/links/add'", js)
        self.assertIn("'/torrents/add-magnet'", js)
        self.assertIn("setButtonPending(button, true, 'Adding…')", js)
        self.assertIn("🔗 Direct link", js)
        self.assertIn("torrents:'Downloads'", js)
        self.assertIn("`All Downloads (${torrentTotal})`", js)
        self.assertIn("function sourceLabel(source)", js)
        self.assertIn("function transferDisplayStatus(t)", js)
        self.assertIn("missing:'❌ Missing file'", js)
        self.assertIn("downloading_with_errors:'⬇ Downloading'", js)
        self.assertIn("completed_with_errors:'⚠ Completed with errors'", js)
        self.assertIn("t.status === 'downloading'", js)
        self.assertIn("t.status === 'completed'", js)
        self.assertIn("String(t.error_message || '').trim()", js)
        manager_source = (repo_root / "backend/services/manager_v2.py").read_text()
        self.assertIn("File is no longer available on the source host", manager_source)
        self.assertIn("AND f.status != 'missing'", manager_source)
        self.assertIn("blocked=0 AND status!='missing'", manager_source)
        self.assertIn("required_count == 0 and missing_count > 0", manager_source)

    def test_sidebar_and_settings_match_refined_navigation(self):
        repo_root = Path(__file__).resolve().parents[2]
        html = (repo_root / "frontend/static/index.html").read_text()
        js = (repo_root / "frontend/static/app.js").read_text()
        css = (repo_root / "frontend/static/style.css").read_text()

        sidebar_nav = html.split("<nav>", 1)[1].split("</nav>", 1)[0]
        for removed_view in ("learning", "saved-searches", "changelog", "support"):
            self.assertNotIn(f'data-view="{removed_view}"', sidebar_nav)
        self.assertIn('data-view="help"', sidebar_nav)
        self.assertNotIn('<div class="nav-group">Project</div>', sidebar_nav)
        self.assertIn('class="sidebar-theme-control"', sidebar_nav)
        self.assertIn('aria-label="Switch to light mode"', sidebar_nav)
        self.assertIn('aria-label="Open the DebridPulse changelog"', html)
        self.assertNotIn("data-view=changelog", html)

        sidebar_footer = html.split('<div class="sidebar-footer">', 1)[1].split("</aside>", 1)[0]
        self.assertNotIn('id="dot-jackett"', sidebar_footer)
        self.assertNotIn('id="lbl-jackett"', sidebar_footer)

        tabs_start = js.index("  const tabs = [", js.index("function renderSettings()"))
        tabs_end = js.index("  ];", tabs_start)
        visible_tabs = js[tabs_start:tabs_end]
        for removed_tab in ("tab-services", "tab-indexers", "tab-flexget", "tab-automation"):
            self.assertNotIn(removed_tab, visible_tabs)
        for retained_tab in ("tab-general", "tab-download", "tab-extract", "tab-notifications", "tab-database", "tab-advanced"):
            self.assertIn(retained_tab, visible_tabs)

        self.assertIn("if (!requestedTab) id = 'tab-general';", js)
        self.assertIn("function updateThemeToggle(isLight)", js)
        self.assertIn("btn.setAttribute('aria-label', action);", js)
        self.assertIn(".sidebar-theme-control", css)
        self.assertNotIn("position:fixed; bottom:16px; right:16px", css)
        self.assertRegex(
            html,
            r'<link rel="stylesheet" href="/style\.css\?v=\d+">',
        )

    def test_theme_branding_and_semantic_colors_are_separated(self):
        repo_root = Path(__file__).resolve().parents[2]
        html = (repo_root / "frontend/static/index.html").read_text()
        landing_html = (repo_root / "index.html").read_text()
        css = (repo_root / "frontend/static/style.css").read_text()
        logo = (repo_root / "frontend/static/logo.svg").read_text()
        favicon = (repo_root / "frontend/static/favicon.svg").read_text()

        self.assertIn("<title>DebridPulse</title>", html)
        logo_block = html.split('<div class="logo">', 1)[1].split("</div>\n  </div>", 1)[0]
        self.assertIn("Debrid<span>Pulse</span>", logo_block)
        self.assertNotIn("ACDC", logo_block)
        self.assertIn('href="/favicon.svg?v=4"', html)
        self.assertIn('href="/favicon-32.png?v=4"', html)
        self.assertIn('href="/apple-touch-icon.png?v=4"', html)
        self.assertIn('href="./docs/logo.svg"', landing_html)
        self.assertIn('src="./docs/logo.svg"', landing_html)

        for glyph in ("&#x25EB;&#xFE0E;", "&#x25BD;&#xFE0E;", "&#x2263;&#xFE0E;",
                      "&#x2206;&#xFE0E;", "&#x2699;&#xFE0E;", "&#x003F;&#xFE0E;"):
            self.assertIn(glyph, html)

        for semantic_token in (
            "--status-total:      #ff8c42;",
            "--status-completed:  #38d27d;",
            "--status-active:     #63a4ff;",
            "--status-processing: #f5c451;",
            "--status-error:      #ff6b6b;",
            "--status-downloaded: #b587ff;",
        ):
            self.assertIn(semantic_token, css)
        self.assertIn('.dash-hero-stat[data-c="orange"] { --c: var(--status-total); }', css)
        self.assertIn('.stat-card[data-c="orange"] { --c: var(--status-total); }', css)
        self.assertIn("--primary-gradient:", css)
        self.assertIn(".btn-primary { background: var(--primary-gradient);", css)
        self.assertIn("font-variant-emoji:text", css)
        self.assertNotIn("--accent:   #ff8c42;", css)
        for light_theme_token in (
            "--bg:       #e9eff6;",
            "--surface:  #ffffff;",
            "--surface2: #f1f5f9;",
            "--border:   #cfd9e5;",
            "--nav-active: rgba(49,95,174,.11);",
        ):
            self.assertIn(light_theme_token, css)
        self.assertIn("body.light .dash-hero-stat,", css)
        self.assertIn("body.light .dash-kpi-strip {", css)
        self.assertIn("background: linear-gradient(180deg, #ffffff, #fbfcfe);", css)

        self.assertIn('viewBox="0 0 512 512"', logo)
        self.assertIn("violet and blue download arrow entering a bucket", logo)
        self.assertEqual((repo_root / "docs/logo.svg").read_text(), logo)
        self.assertIn('viewBox="0 0 64 64"', favicon)

        for name, expected_size in (
            ("favicon-32.png", (32, 32)),
            ("apple-touch-icon.png", (180, 180)),
            ("favicon.png", (512, 512)),
        ):
            data = (repo_root / "frontend/static" / name).read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", data[16:24]), expected_size)

    def test_dashboard_is_a_fixed_at_a_glance_view(self):
        repo_root = Path(__file__).resolve().parents[2]
        html = (repo_root / "frontend/static/index.html").read_text()
        js = (repo_root / "frontend/static/app.js").read_text()
        css = (repo_root / "frontend/static/style.css").read_text()

        self.assertIn('<div id="content" class="dashboard-active">', html)
        self.assertIn('id="dash-activity-card"', html)
        self.assertIn('class="dash-activity-table-wrap"', html)
        self.assertIn("content.classList.toggle('dashboard-active', v === 'dashboard');", js)
        self.assertIn("function dashboardRecentLimit()", js)
        self.assertIn("window.matchMedia('(max-width: 700px)').matches ? 4 : 6", js)
        self.assertIn("api('GET', `/torrents?limit=${recentLimit}`)", js)
        self.assertIn("#content.dashboard-active { overflow-y: hidden; }", css)
        self.assertIn("#view-dashboard.active {", css)
        self.assertIn("#dash-activity-card {", css)

if __name__ == "__main__":
    unittest.main()
