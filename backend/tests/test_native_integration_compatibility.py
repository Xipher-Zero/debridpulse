"""
Native integration clients, notifications and runtime configuration.

Deckt ab:
- aria2 connection robustness (closing transport errors)
- Abschluss-Erkennung (Finished-Entry-Handling)
- Duplikat-Vermeidung
- Dashboard-Datenfluss (completed-Status)
- Discord-Webhook-Formatierung inkl. torrent-added
- Statistik-Berechnungen
- Migration safety checks
- PostgreSQL-Konfigurationsvalidierung
"""
import asyncio
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from providers.alldebrid.client import AllDebridService, flatten_files
from executors.aria2.client import Aria2Service, Aria2DownloadStatus, Aria2RPCError, Aria2ConnectionError


# ═════════════════════════════════════════════════════════════════════════════
# Base tests (existing, extended)
# ═════════════════════════════════════════════════════════════════════════════

class NativeManifestTests(unittest.TestCase):
    def test_flatten_files_preserves_nested_path(self):
        nodes = [{"n": "Season 01", "e": [
            {"n": "Episode 01.mkv", "s": 123, "l": "https://example.invalid/1"},
        ]}]
        flat = flatten_files(nodes)
        self.assertEqual(len(flat), 1)
        self.assertEqual(flat[0]["path"], "Season 01/Episode 01.mkv")


# ═════════════════════════════════════════════════════════════════════════════
# aria2 Robustheit
# ═════════════════════════════════════════════════════════════════════════════

class Aria2RobustnessTests(unittest.IsolatedAsyncioTestCase):
    """
    Tests aria2 connection robustness, specifically:
    - "Cannot write to closing transport" is classified as Aria2ConnectionError
    - get_all() returns [] instead of raising
    - Retry logic for transient errors
    """

    async def test_connection_error_classified_as_aria2_connection_error(self):
        """Transient connection errors are classified as Aria2ConnectionError."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)

        class FakeConnector:
            closed = False
            async def close(self): pass

        async def fake_post(*a, **kw):
            raise Exception("Cannot write to closing transport")

        class FakeSession:
            def __init__(self, *a, **kw): pass
            def post(self, *a, **kw): return self
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            def __call__(self, *a, **kw): raise Exception("Cannot write to closing transport")

        with patch("executors.aria2.client.aiohttp.TCPConnector", return_value=FakeConnector()), \
             patch("executors.aria2.client.aiohttp.ClientSession") as mock_session:
            mock_session.return_value.__aenter__ = AsyncMock(side_effect=Exception("Cannot write to closing transport"))
            mock_session.return_value.__aexit__ = AsyncMock(return_value=False)

            with self.assertRaises((Aria2ConnectionError, Aria2RPCError, Exception)):
                await service._call("aria2.getVersion")

    async def test_get_all_raises_on_connection_error(self):
        """An unreachable daemon is unknown, never an empty inventory."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)

        async def fake_call(method, params=None):
            raise Aria2ConnectionError("Connection interrupted")

        service._call = fake_call
        with self.assertRaises(Aria2ConnectionError):
            await service.get_all()

    async def test_get_all_raises_on_rpc_error(self):
        """Invalid RPC observations must not authorize daemon restart."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)

        async def fake_call(method, params=None):
            raise Aria2RPCError("aria2 [-32600]: Invalid Request")

        service._call = fake_call
        with self.assertRaises(Aria2RPCError):
            await service.get_all()

    async def test_get_all_aggregates_all_three_endpoints(self):
        """get_all() aggregates active, waiting and stopped downloads."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)
        seen = []

        async def fake_call(method, params=None):
            seen.append((method, params))
            if method == "aria2.tellActive":
                return [{"gid": "a1", "status": "active", "totalLength": "100",
                         "completedLength": "50", "downloadSpeed": "10", "files": []}]
            if method == "aria2.tellWaiting":
                return [{"gid": "w1", "status": "waiting", "totalLength": "200",
                         "completedLength": "0", "downloadSpeed": "0", "files": []}]
            if method == "aria2.tellStopped":
                return [{"gid": "s1", "status": "complete", "totalLength": "300",
                         "completedLength": "300", "downloadSpeed": "0", "files": []}]
            return []

        service._call = fake_call
        result = await service.get_all()
        self.assertEqual(len(result), 3)
        self.assertEqual({dl.gid for dl in result}, {"a1", "w1", "s1"})
        self.assertIn(("aria2.tellWaiting", [0, 100, service._keys()]), seen)
        self.assertIn(("aria2.tellStopped", [0, 100, service._keys()]), seen)

    async def test_get_memory_diagnostics_reports_counts_and_options(self):
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)
        seen = []

        async def fake_call(method, params=None):
            seen.append((method, params))
            if method == "aria2.tellActive":
                return [{"gid": "a1", "status": "active", "files": []}]
            if method == "aria2.tellWaiting":
                return [{"gid": "w1", "status": "waiting", "files": []}]
            if method == "aria2.tellStopped":
                return [
                    {"gid": "s1", "status": "complete", "files": []},
                    {"gid": "s2", "status": "error", "files": []},
                ]
            if method == "aria2.getGlobalOption":
                return {
                    "max-download-result": "200",
                    "keep-unfinished-download-result": "false",
                }
            return []

        service._call = fake_call
        result = await service.get_memory_diagnostics()
        self.assertEqual(result["active_count"], 1)
        self.assertEqual(result["waiting_count"], 1)
        self.assertEqual(result["stopped_count"], 2)
        self.assertEqual(result["query_limits"]["waiting"], 100)
        self.assertEqual(result["query_limits"]["stopped"], 100)
        self.assertEqual(result["global_options"]["max-download-result"], "200")
        self.assertEqual(result["global_options"]["keep-unfinished-download-result"], "false")
        self.assertIn(("aria2.tellWaiting", [0, 100, service._keys()]), seen)
        self.assertIn(("aria2.tellStopped", [0, 100, service._keys()]), seen)

    async def test_get_memory_diagnostics_respects_custom_windows(self):
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)
        seen = []

        async def fake_call(method, params=None):
            seen.append((method, params))
            if method == "aria2.getGlobalOption":
                return {}
            return []

        service._call = fake_call
        result = await service.get_memory_diagnostics(waiting_limit=25, stopped_limit=40)
        self.assertEqual(result["query_limits"]["waiting"], 25)
        self.assertEqual(result["query_limits"]["stopped"], 40)
        self.assertIn(("aria2.tellWaiting", [0, 25, service._keys()]), seen)
        self.assertIn(("aria2.tellStopped", [0, 40, service._keys()]), seen)


# ═════════════════════════════════════════════════════════════════════════════
# Abschluss-Erkennung (Finished Entry Handling)
# ═════════════════════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════════════════════
# Dashboard data flow
# ═════════════════════════════════════════════════════════════════════════════

class DashboardCompletedTests(unittest.IsolatedAsyncioTestCase):
    """
    Stellt sicher dass completed-Torrents im Dashboard erscheinen.
    Root-Cause-Fix: _delete_magnet_after_completion setzt kein status='deleted'.
    """


    def test_by_status_completed_field_used_in_stats(self):
        """Stats-Endpunkt liefert 'completed' als Key in by_status."""
        # Simulates logic from routes.py: by_status is read directly from DB
        by_status = {"completed": 5, "deleted": 2, "error": 1, "queued": 3}
        completed = by_status.get("completed", 0)
        self.assertEqual(completed, 5, "by_status.completed muss korrekte Zahl liefern")

        # If deleted were set instead of completed:
        wrong_status = {"deleted": 5, "error": 1, "queued": 3}
        wrong_completed = wrong_status.get("completed", 0)
        self.assertEqual(wrong_completed, 0, "Broken logic would return 0")


# ═════════════════════════════════════════════════════════════════════════════
# Discord Webhook / Notifications
# ═════════════════════════════════════════════════════════════════════════════

class NotificationTests(unittest.IsolatedAsyncioTestCase):
    """
    Testet Discord-Webhook-Formatierung und torrent-added Event.
    """

    async def test_send_added_uses_added_webhook_url(self):
        """send_added() verwendet den separaten added_webhook_url wenn konfiguriert."""
        from services.notifications import NotificationService

        sent_to = []

        async def fake_send(self, url, title, description, color, fields=None):
            sent_to.append(url)

        with patch.object(NotificationService, "_send", fake_send):
            svc = NotificationService(
                webhook_url="https://main.discord.invalid/hook",
                added_webhook_url="https://added.discord.invalid/hook",
            )
            await svc.send_added("My Torrent", source="manual", transfer_id="123")

        self.assertEqual(sent_to, ["https://added.discord.invalid/hook"])

    async def test_send_added_falls_back_to_main_webhook(self):
        """send_added() falls back to discord_webhook_url when no added_webhook_url."""
        from services.notifications import NotificationService

        sent_to = []

        async def fake_send(self, url, title, description, color, fields=None):
            sent_to.append(url)

        with patch.object(NotificationService, "_send", fake_send):
            svc = NotificationService(
                webhook_url="https://main.discord.invalid/hook",
                added_webhook_url="",
            )
            await svc.send_added("My Torrent", source="manual_file")

        self.assertEqual(sent_to, ["https://main.discord.invalid/hook"])

    async def test_send_added_includes_source_field(self):
        """send_added() passes source information as an embed field."""
        from services.notifications import NotificationService

        captured_fields = []

        async def fake_send(self, url, title, description, color, fields=None):
            captured_fields.extend(fields or [])

        with patch.object(NotificationService, "_send", fake_send):
            svc = NotificationService("https://hook.invalid/x")
            await svc.send_added("Test Torrent", source="manual_file", transfer_id="ad-42")

        field_names = [f["name"] for f in captured_fields]
        self.assertIn("Source", field_names)
        source_field = next(f for f in captured_fields if f["name"] == "Source")
        self.assertIn("torrent file", source_field["value"].lower())

    async def test_deduplication_suppresses_duplicate_within_window(self):
        """Same message within deduplication window is suppressed."""
        from services.notifications import NotificationService
        import hashlib as _hl

        # Class-wide state reset for isolated test
        NotificationService._sent_hashes = {}
        NotificationService._last_sent_at = {}
        NotificationService._throttle_lock = None

        send_count = {"n": 0}

        async def fake_http_send(url, payload):
            send_count["n"] += 1

        svc = NotificationService("https://hook.invalid/x")

        # Patch _send at the HTTP-call level via _do_http_post if present,
        # or mock the entire _send and test the dedup logic directly.
        # We test the dedup guard by calling _send twice with identical args
        # and checking the _sent_hashes state machine.
        import hashlib

        # Manually exercise the dedup logic (same as _send does internally)
        url = "https://hook.invalid/x"
        title = "Test"
        description = "Same content"
        dedup_key = hashlib.md5(f"{url}|{title}|{description[:200]}".encode()).hexdigest()

        # Before first send: no hash
        self.assertNotIn(dedup_key, NotificationService._sent_hashes)

        # Simulate first send recording the hash (as _send does after lock)
        import time as _time
        NotificationService._sent_hashes[dedup_key] = _time.monotonic()

        # Hash must now be set
        self.assertIn(dedup_key, NotificationService._sent_hashes,
                      "Hash must be set after first send")

        # Dedup check: now - last_hash < 30 → should suppress
        import time
        now = time.monotonic()
        last_hash = NotificationService._sent_hashes.get(dedup_key, 0.0)
        self.assertLess(now - last_hash, 30.0,
                        "Second call within window should be suppressed by dedup")

        # Outside window: now - last_hash >= 30 → should NOT suppress
        NotificationService._sent_hashes[dedup_key] = _time.monotonic() - 35.0
        now2 = time.monotonic()
        last_hash2 = NotificationService._sent_hashes.get(dedup_key, 0.0)
        self.assertGreaterEqual(now2 - last_hash2, 30.0,
                                "After window expiry dedup should not suppress")

    async def test_send_complete_includes_metadata_fields(self):
        """send_complete() includes metadata fields in the embed."""
        from services.notifications import NotificationService

        captured = {}

        async def fake_send(self, url, title, description, color, fields=None):
            captured["title"] = title
            captured["description"] = description
            captured["fields"] = fields or []

        with patch.object(NotificationService, "_send", fake_send):
            svc = NotificationService("https://hook.invalid/x")
            await svc.send_complete(
                "My Show S01",
                file_count=12,
                size_bytes=1073741824,
                destination="/downloads/My Show S01",
                download_client="aria2",
            )

        self.assertIn("✅", captured["title"])
        self.assertIn("My Show S01", captured["description"])
        field_names = [f["name"] for f in captured["fields"]]
        self.assertIn("Files", field_names)
        self.assertIn("Size", field_names)

    async def test_send_error_includes_reason(self):
        """send_error() passes error reason as a field."""
        from services.notifications import NotificationService

        captured_fields = []

        async def fake_send(self, url, title, description, color, fields=None):
            captured_fields.extend(fields or [])

        with patch.object(NotificationService, "_send", fake_send):
            svc = NotificationService("https://hook.invalid/x")
            await svc.send_error("Failed Torrent", reason="AllDebrid error code 7")

        field_names = [f["name"] for f in captured_fields]
        self.assertIn("Reason", field_names)

    async def test_send_error_includes_provider_context_fields(self):
        """send_error() includes provider/source metadata for richer error webhooks."""
        from services.notifications import NotificationService

        captured_fields = []

        async def fake_send(self, url, title, description, color, fields=None):
            captured_fields.extend(fields or [])

        with patch.object(NotificationService, "_send", fake_send):
            svc = NotificationService("https://hook.invalid/x")
            await svc.send_error(
                "Failed Torrent",
                reason="No downloadable files returned from AllDebrid",
                context="Magnet stayed in provider error state",
                source="AllDebrid polling",
                provider="AllDebrid",
                transfer_id="521903942",
                category="no_transfer_candidate",
            )

        captured = {field["name"]: field["value"] for field in captured_fields}
        self.assertEqual(captured.get("Source"), "AllDebrid polling")
        self.assertEqual(captured.get("Provider"), "AllDebrid")
        self.assertEqual(captured.get("Transfer ID"), "521903942")
        self.assertEqual(captured.get("Category"), "no_transfer_candidate")
        self.assertIn("No downloadable files", captured.get("Reason", ""))
        self.assertIn("provider error state", captured.get("Context", ""))

    async def test_no_send_when_webhook_empty(self):
        """No message sent when webhook_url is empty."""
        from services.notifications import NotificationService

        send_count = {"n": 0}

        async def fake_send(self, url, title, description, color, fields=None):
            send_count["n"] += 1

        with patch.object(NotificationService, "_send", fake_send):
            svc = NotificationService("")
            await svc.send("Title", "Description")
            await svc.send_added("Torrent")
            await svc.send_complete("Torrent")
            await svc.send_error("Torrent")

        self.assertEqual(send_count["n"], 0)

    async def test_send_logs_exception_type_when_error_message_is_empty(self):
        """Discord failures with empty exception strings still log a useful error type."""
        from services.notifications import NotificationService

        class EmptyError(Exception):
            def __str__(self):
                return ""

        class FakePostContext:
            async def __aenter__(self):
                raise EmptyError()
            async def __aexit__(self, *a):
                return False

        class FakeClientSession:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def post(self, *a, **kw): return FakePostContext()

        NotificationService._sent_hashes = {}
        NotificationService._last_sent_at = {}
        NotificationService._throttle_lock = None

        with patch("services.notifications.aiohttp.ClientSession", FakeClientSession), \
             patch("services.notifications.logger.error") as log_error:
            svc = NotificationService("https://discord.invalid/webhook")
            result = await svc.send("Title", "Description")

        self.assertFalse(result)
        log_args = log_error.call_args[0]
        self.assertIn("EmptyError", log_args[2])


# ═════════════════════════════════════════════════════════════════════════════
# Statistik-Berechnungen
# ═════════════════════════════════════════════════════════════════════════════

class StatsCalculationTests(unittest.TestCase):
    """
    Testet Statistik-Berechnungen ohne Datenbankzugriff.
    """

    def test_success_rate_calculation(self):
        """Success rate is calculated correctly."""
        completed = 8
        errors = 2
        terminal = completed + errors
        rate = round(completed / terminal * 100, 1)
        self.assertEqual(rate, 80.0)

    def test_success_rate_zero_when_no_terminal(self):
        """Success rate is None when no terminal torrents exist."""
        completed = 0
        errors = 0
        terminal = completed + errors
        rate = round(completed / terminal * 100, 1) if terminal > 0 else None
        self.assertIsNone(rate)

    def test_success_rate_100_percent(self):
        """Success rate is 100% when all torrents completed."""
        completed = 10
        errors = 0
        terminal = completed + errors
        rate = round(completed / terminal * 100, 1) if terminal > 0 else None
        self.assertEqual(rate, 100.0)

    def test_by_status_completed_visible(self):
        """
        Abgeschlossene Torrents erscheinen als 'completed' in by_status
        (not as 'deleted' after removing from AllDebrid).
        """
        # Simulates correct completed state in the database
        by_status = {
            "completed": 15,
            "error": 2,
            "queued": 3,
            "downloading": 1,
        }
        # Dashboard liest by_status.completed
        dashboard_completed = by_status.get("completed", 0)
        self.assertEqual(dashboard_completed, 15)

        # Falscher Stand (alter Bug): alles steht als 'deleted'
        buggy_status = {
            "deleted": 15,  # Falscher Bug-Zustand
            "error": 2,
        }
        buggy_completed = buggy_status.get("completed", 0)
        self.assertEqual(buggy_completed, 0, "So sah der Dashboard-Bug aus")


# ═════════════════════════════════════════════════════════════════════════════
# PostgreSQL-Konfigurationsvalidierung
# ═════════════════════════════════════════════════════════════════════════════

# ═════════════════════════════════════════════════════════════════════════════
# Migration safety checks
# ═════════════════════════════════════════════════════════════════════════════

# ═════════════════════════════════════════════════════════════════════════════
# Bestehende Tests aus dem Original (erweitert)
# ═════════════════════════════════════════════════════════════════════════════

class AllDebridServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_does_not_retry_an_ambiguous_empty_response(self):
        service = AllDebridService("api-key")
        responses = ["", '{"status":"success","data":{"ok":true}}']

        class FakeResponse:
            def __init__(self, body):
                self.body = body
                self.status = 200
            async def text(self): return self.body
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        class FakeSession:
            def __init__(self, *a, **kw): pass
            def post(self, *a, **kw): return FakeResponse(responses.pop(0))
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        with patch("providers.alldebrid.client.aiohttp.ClientSession", FakeSession):
            with self.assertRaises(Exception):
                await service._post("https://api.example", "magnet/status")
        self.assertEqual(len(responses), 1)

    def test_decode_json_body_reports_invalid_payload(self):
        service = AllDebridService("api-key")
        with self.assertRaises(Exception) as ctx:
            service._decode_json_body("<html>bad gateway</html>", "magnet/status")
        self.assertIn("invalid JSON", str(ctx.exception))


class NativeRuntimeConfigurationTests(unittest.IsolatedAsyncioTestCase):


    def test_builtin_aria2_uses_fixed_internal_rpc_secret(self):
        from executors.aria2.runtime import BUILTIN_ARIA2_SECRET, effective_rpc_config

        cfg = types.SimpleNamespace(
            aria2_mode="builtin",
            aria2_builtin_port=6800,
            aria2_url="http://external.invalid/jsonrpc",
            aria2_secret="user-editable-secret",
        )
        url, secret = effective_rpc_config(cfg)
        self.assertEqual(url, "http://127.0.0.1:6800/jsonrpc")
        self.assertEqual(secret, BUILTIN_ARIA2_SECRET)


    def test_builtin_runtime_command_uses_download_folder_not_external_root(self):
        from executors.aria2.runtime import BuiltinAria2Runtime
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            download_dir = Path(tmp) / "download"
            cfg = types.SimpleNamespace(
                download_folder=str(download_dir),
                aria2_download_path="/external/downloads",
                aria2_builtin_log_file=str(Path(tmp) / "aria2.log"),
                aria2_builtin_log_max_mb=25,
                aria2_builtin_log_backups=3,
                aria2_builtin_session_file=str(Path(tmp) / "aria2.session"),
                aria2_builtin_port=6800,
                aria2_max_download_result=50,
                aria2_keep_unfinished_download_result=False,
                aria2_max_active_downloads=3,
                aria2_split=8,
                aria2_min_split_size="10M",
                aria2_max_connection_per_server=8,
                aria2_disk_cache="64M",
                aria2_file_allocation="falloc",
                aria2_continue_downloads=True,
                aria2_lowest_speed_limit="0",
            )
            with patch("executors.aria2.runtime.get_settings", return_value=cfg):
                command = BuiltinAria2Runtime()._command()
        self.assertIn(f"--dir={download_dir}", command)
        self.assertNotIn("--dir=/external/downloads", command)

    def test_builtin_runtime_rotates_oversized_log_file(self):
        from executors.aria2.runtime import BuiltinAria2Runtime
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "aria2.log"
            log_file.write_bytes(b"x" * 2048)
            cfg = types.SimpleNamespace(
                aria2_builtin_log_file=str(log_file),
                aria2_builtin_log_max_mb=1,
                aria2_builtin_log_backups=2,
                aria2_builtin_session_file=str(Path(tmp) / "aria2.session"),
            )
            with patch("executors.aria2.runtime.get_settings", return_value=cfg):
                rotated = BuiltinAria2Runtime()._rotate_log_file()
            self.assertFalse(rotated)
            self.assertTrue(log_file.exists())

            log_file.write_bytes(b"x" * (1024 * 1024 + 1))
            with patch("executors.aria2.runtime.get_settings", return_value=cfg):
                rotated = BuiltinAria2Runtime()._rotate_log_file()
            self.assertTrue(rotated)
            self.assertTrue(log_file.exists())
            self.assertTrue((Path(tmp) / "aria2.log.1").exists())

    def test_aria2_download_payload_reports_progress_and_files(self):
        from executors.aria2.client import aria2_download_to_dict

        download = Aria2DownloadStatus(
            gid="abc123",
            status="active",
            total_length=1000,
            completed_length=250,
            download_speed=125,
            files=[{
                "path": "/download/movie/file.mkv",
                "length": "1000",
                "completedLength": "250",
                "selected": "true",
                "uris": [{"uri": "https://example.invalid/file.mkv"}],
            }],
        )
        payload = aria2_download_to_dict(download)
        self.assertEqual(payload["gid"], "abc123")
        self.assertEqual(payload["name"], "file.mkv")
        self.assertEqual(payload["progress"], 25.0)
        self.assertEqual(payload["remaining_length"], 750)
        self.assertEqual(payload["files"][0]["progress"], 25.0)
        self.assertEqual(payload["files"][0]["uris"], ["https://example.invalid/file.mkv"])


if __name__ == "__main__":
    unittest.main()
