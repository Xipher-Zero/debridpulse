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
from providers.alldebrid.client import AllDebridAPIError, AllDebridService
from transfers.requests import direct_link_collection_name
from transfers.requests import direct_link_filename
from transfers.requests import normalize_direct_links


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
        with self.assertRaisesRegex(ValueError, "Every link must be an absolute HTTP or HTTPS URL"):
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
        with patch("providers.alldebrid.client.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await service.unlock_link("https://host.invalid/archive")

        self.assertEqual(result["filename"], "archive.zip")
        self.assertEqual(result["filesize"], 123)
        self.assertEqual(result["link"], "https://download.invalid/archive.zip")
        self.assertEqual(sleep.await_args_list[0].args, (5,))
        self.assertEqual(service._post.await_count, 3)






class DashboardContractTests(unittest.TestCase):



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
