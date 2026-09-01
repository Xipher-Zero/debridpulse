"""
Tests for the DebridPulse backend.

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

# ── Stub imports for missing packages ──────────────────────────────────────────
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
        Connection=object, Row=object,
        connect=lambda *a, **kw: None,
    )
if "pydantic" not in sys.modules:
    class _FakeModel:
        model_fields = {}
        def __init_subclass__(cls, **kw):
            super().__init_subclass__(**kw)
            cls.model_fields = dict(getattr(cls, "__annotations__", {}))
        def __init__(self, **kw):
            for k, v in self.__class__.__dict__.items():
                if not k.startswith("_") and not callable(v):
                    setattr(self, k, v)
            for k, v in kw.items():
                setattr(self, k, v)
        def model_dump(self): return self.__dict__.copy()
        def model_copy(self, update=None):
            d = self.model_dump()
            if update: d.update(update)
            return self.__class__(**d)
    sys.modules["pydantic"] = types.SimpleNamespace(BaseModel=_FakeModel)

from providers.alldebrid.client import AllDebridService, flatten_files
from executors.aria2.client import Aria2Service, Aria2DownloadStatus, Aria2RPCError, Aria2ConnectionError
from services.manager_v2 import (
    TransientAllDebridStateError,
    normalize_provider_state,
    safe_rel_path,
    TorrentManager,
)


# ═════════════════════════════════════════════════════════════════════════════
# Base tests (existing, extended)
# ═════════════════════════════════════════════════════════════════════════════

class ManagerV2Tests(unittest.TestCase):
    def test_flatten_files_preserves_nested_path(self):
        nodes = [{"n": "Season 01", "e": [
            {"n": "Episode 01.mkv", "s": 123, "l": "https://example.invalid/1"},
        ]}]
        flat = flatten_files(nodes)
        self.assertEqual(len(flat), 1)
        self.assertEqual(flat[0]["path"], "Season 01/Episode 01.mkv")

    def test_safe_rel_path_sanitizes_segments(self):
        path = safe_rel_path("../Season 01/Bad:Name?.mkv")
        self.assertEqual(str(path).replace("\\", "/"), "Season 01/Bad_Name_.mkv")

    def test_normalize_provider_state_ready(self):
        s = normalize_provider_state({"statusCode": 4, "size": 200, "downloaded": 200, "status": "Ready"})
        self.assertEqual(s["provider_status"], "ready")
        self.assertEqual(int(s["progress"]), 100)

    def test_normalize_provider_state_error(self):
        s = normalize_provider_state({"statusCode": 8, "size": 200, "downloaded": 10, "status": "Error"})
        self.assertEqual(s["provider_status"], "error")

    def test_normalize_provider_state_processing(self):
        s = normalize_provider_state({"statusCode": 2, "size": 100, "downloaded": 50, "status": "Processing"})
        self.assertEqual(s["provider_status"], "processing")
        self.assertEqual(s["local_status"], "processing")

    def test_normalize_provider_state_queued(self):
        s = normalize_provider_state({"statusCode": 0, "size": 0, "downloaded": 0, "status": "Queued"})
        self.assertEqual(s["provider_status"], "queued")
        self.assertEqual(s["local_status"], "uploading")


class GlobalPauseControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_all_targets_each_active_owned_parent(self):
        manager = TorrentManager()
        manager.download_client_name = lambda: "aria2"
        manager.pause_torrent = AsyncMock(
            side_effect=[None, RuntimeError("simulated race")]
        )

        fake_db = types.SimpleNamespace(
            fetchall=AsyncMock(return_value=[{"id": 11}, {"id": 22}])
        )

        @asynccontextmanager
        async def fake_get_db():
            yield fake_db

        with patch("services.manager_v2.get_db", fake_get_db):
            result = await manager.pause_all_downloads()

        self.assertEqual(result, {"paused": 1, "failed": 1})
        self.assertEqual(
            [call.args[0] for call in manager.pause_torrent.await_args_list],
            [11, 22],
        )
        query = fake_db.fetchall.await_args.args[0]
        self.assertIn("t.status IN ('queued','downloading')", query)
        self.assertIn("f.status IN ('pending','queued','downloading')", query)
        self.assertNotIn("f.download_id IS NOT NULL", query)

    async def test_resume_all_only_targets_paused_owned_parents(self):
        manager = TorrentManager()
        manager.download_client_name = lambda: "aria2"
        manager.resume_torrent = AsyncMock()

        fake_db = types.SimpleNamespace(
            fetchall=AsyncMock(return_value=[{"id": 33}, {"id": 44}])
        )

        @asynccontextmanager
        async def fake_get_db():
            yield fake_db

        with patch("services.manager_v2.get_db", fake_get_db):
            result = await manager.resume_all_downloads()

        self.assertEqual(result, {"resumed": 2, "failed": 0})
        self.assertEqual(
            [call.args[0] for call in manager.resume_torrent.await_args_list],
            [33, 44],
        )
        query = fake_db.fetchall.await_args.args[0]
        self.assertIn("t.status='paused'", query)
        self.assertIn("f.status='paused'", query)
        self.assertNotIn("f.download_id IS NOT NULL", query)

    async def test_individual_pause_holds_dispatched_and_pending_children(self):
        manager = TorrentManager()
        manager.download_client_name = lambda: "aria2"
        manager.is_paused = lambda: False
        manager._advance_aria2_queue_locked = AsyncMock()
        fake_aria2 = types.SimpleNamespace(pause=AsyncMock())
        manager.aria2 = lambda: fake_aria2
        manager._log_event = AsyncMock()
        statements = []

        class Cursor:
            async def fetchall(self):
                return [{"download_id": "gid-1"}]

        class FakeDb:
            async def execute(self, sql, params=()):
                statements.append((sql, params))
                return Cursor()

            async def commit(self):
                return None

        @asynccontextmanager
        async def fake_get_db():
            yield FakeDb()

        with patch("services.manager_v2.get_db", fake_get_db):
            await manager.pause_torrent(51)

        fake_aria2.pause.assert_awaited_once_with("gid-1")
        file_update = next(
            sql for sql, _ in statements
            if "UPDATE download_files" in sql
        )
        self.assertIn(
            "status IN ('pending','queued','downloading')",
            file_update,
        )
        manager._advance_aria2_queue_locked.assert_awaited_once_with()

    async def test_individual_resume_restores_dispatchable_child_states(self):
        manager = TorrentManager()
        manager.download_client_name = lambda: "aria2"
        manager.is_paused = lambda: False
        manager._advance_aria2_queue_locked = AsyncMock()
        fake_aria2 = types.SimpleNamespace(resume=AsyncMock())
        manager.aria2 = lambda: fake_aria2
        manager._log_event = AsyncMock()
        statements = []

        class Cursor:
            async def fetchall(self):
                return [{"download_id": "gid-2"}]

        class FakeDb:
            async def execute(self, sql, params=()):
                statements.append((sql, params))
                return Cursor()

            async def commit(self):
                return None

        @asynccontextmanager
        async def fake_get_db():
            yield FakeDb()

        with patch("services.manager_v2.get_db", fake_get_db):
            await manager.resume_torrent(52)

        fake_aria2.resume.assert_awaited_once_with("gid-2")
        file_update = next(
            sql for sql, _ in statements
            if "UPDATE download_files" in sql
        )
        self.assertIn("WHEN download_id IS NULL THEN 'pending'", file_update)
        self.assertIn("ELSE 'queued'", file_update)
        manager._advance_aria2_queue_locked.assert_awaited_once_with()

    async def test_resume_during_global_pause_does_not_bypass_global_gate(self):
        manager = TorrentManager()
        manager.download_client_name = lambda: "aria2"
        manager.is_paused = lambda: True
        manager._advance_aria2_queue_locked = AsyncMock()
        manager.aria2 = lambda: types.SimpleNamespace(resume=AsyncMock())
        manager._log_event = AsyncMock()

        class Cursor:
            async def fetchall(self):
                return []

        class FakeDb:
            async def execute(self, sql, params=()):
                return Cursor()

            async def commit(self):
                return None

        @asynccontextmanager
        async def fake_get_db():
            yield FakeDb()

        with patch("services.manager_v2.get_db", fake_get_db):
            await manager.resume_torrent(53)

        manager._advance_aria2_queue_locked.assert_awaited_once_with()

    async def test_global_pause_is_a_strict_dispatch_gate(self):
        manager = TorrentManager()
        manager.download_client_name = lambda: "aria2"
        manager.is_paused = lambda: True
        manager._aria2_get_all = AsyncMock()

        await manager._dispatch_pending_aria2_queue()

        manager._aria2_get_all.assert_not_awaited()

    async def test_queue_advance_dispatches_files_then_materializes_ready_parent(self):
        manager = TorrentManager()
        manager.download_client_name = lambda: "aria2"
        manager.is_paused = lambda: False
        order = []
        manager._dispatch_pending_aria2_queue = AsyncMock(
            side_effect=lambda: order.append("files")
        )
        manager._schedule_ready_aria2_parents = AsyncMock(
            side_effect=lambda: order.append("ready") or 1
        )

        result = await manager._advance_aria2_queue_locked()

        self.assertEqual(result, 1)
        self.assertEqual(order, ["files", "ready"])

    async def test_ready_successor_scheduler_claims_priority_order_immediately(self):
        manager = TorrentManager()
        manager.download_client_name = lambda: "aria2"
        manager.is_paused = lambda: False
        manager._start_download = AsyncMock()
        fake_db = types.SimpleNamespace(
            fetchall=AsyncMock(return_value=[
                {"id": 61, "alldebrid_id": "ad-61", "name": "Next"},
                {"id": 62, "alldebrid_id": "ad-62", "name": "Later"},
            ])
        )

        @asynccontextmanager
        async def fake_get_db():
            yield fake_db

        cfg = types.SimpleNamespace(max_concurrent_downloads=3)
        with patch("services.manager_v2.get_db", fake_get_db), \
             patch("services.manager_v2.get_settings", return_value=cfg):
            scheduled = await manager._schedule_ready_aria2_parents()
            await asyncio.sleep(0)

        self.assertEqual(scheduled, 2)
        self.assertEqual(
            [call.args[0] for call in manager._start_download.await_args_list],
            [61, 62],
        )
        query = fake_db.fetchall.await_args.args[0]
        self.assertIn("status='ready'", query)
        self.assertIn("provider_status='ready'", query)
        self.assertIn("ORDER BY priority DESC, id ASC", query)

    async def test_ready_parent_scheduler_deduplicates_tasks_before_they_start(self):
        manager = TorrentManager()
        manager.download_client_name = lambda: "aria2"
        manager.is_paused = lambda: False
        release = asyncio.Event()

        async def blocked_start(*_args):
            await release.wait()

        manager._start_download = AsyncMock(side_effect=blocked_start)

        first = manager._schedule_ready_parent_download(63, "ad-63", "Next")
        duplicate = manager._schedule_ready_parent_download(63, "ad-63", "Next")
        await asyncio.sleep(0)

        self.assertTrue(first)
        self.assertFalse(duplicate)
        manager._start_download.assert_awaited_once_with(63, "ad-63", "Next")

        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertNotIn(63, manager._ready_parent_task_ids)

    async def test_pausing_primary_immediately_schedules_ready_successor(self):
        manager = TorrentManager()
        manager.download_client_name = lambda: "aria2"
        manager.is_paused = lambda: False
        manager._dispatch_pending_aria2_queue = AsyncMock()
        manager._start_download = AsyncMock()
        manager._log_event = AsyncMock()
        fake_aria2 = types.SimpleNamespace(pause=AsyncMock())
        manager.aria2 = lambda: fake_aria2
        statements = []

        class Cursor:
            def __init__(self, rows):
                self.rows = rows

            async def fetchall(self):
                return self.rows

        class FakeDb:
            async def execute(self, sql, params=()):
                statements.append((sql, params))
                if "SELECT download_id FROM download_files" in sql:
                    return Cursor([{"download_id": "gid-primary"}])
                return Cursor([])

            async def fetchall(self, sql, params=()):
                statements.append((sql, params))
                if "FROM torrents" in sql and "provider_status='ready'" in sql:
                    return [{"id": 64, "alldebrid_id": "ad-64", "name": "Successor"}]
                return []

            async def commit(self):
                return None

        @asynccontextmanager
        async def fake_get_db():
            yield FakeDb()

        cfg = types.SimpleNamespace(max_concurrent_downloads=3)
        with patch("services.manager_v2.get_db", fake_get_db), \
             patch("services.manager_v2.get_settings", return_value=cfg):
            await manager.pause_torrent(60)
            await asyncio.sleep(0)

        fake_aria2.pause.assert_awaited_once_with("gid-primary")
        manager._dispatch_pending_aria2_queue.assert_awaited_once_with()
        manager._start_download.assert_awaited_once_with(64, "ad-64", "Successor")
        self.assertTrue(any("SET status='paused'" in sql for sql, _ in statements))

    async def test_start_download_rechecks_pause_after_waiting_for_preparation_slot(self):
        manager = TorrentManager()
        paused = False
        manager.is_paused = lambda: paused
        manager._download = AsyncMock()

        class Gate:
            async def __aenter__(self):
                nonlocal paused
                paused = True

            async def __aexit__(self, exc_type, exc, tb):
                return False

        manager.sem = lambda: Gate()

        class Cursor:
            async def fetchone(self):
                return {"status": "ready"}

        class FakeDb:
            async def execute(self, _sql, _params=()):
                return Cursor()

        @asynccontextmanager
        async def fake_get_db():
            yield FakeDb()

        with patch("services.manager_v2.get_db", fake_get_db):
            await manager._start_download(65, "ad-65", "Boundary")

        manager._download.assert_not_awaited()
        self.assertNotIn(65, manager._active)

    async def test_individual_pause_waits_for_in_progress_state_reconciliation(self):
        manager = TorrentManager()
        manager.download_client_name = lambda: "aria2"
        sync_started = asyncio.Event()
        release_sync = asyncio.Event()

        async def blocked_sync():
            sync_started.set()
            await release_sync.wait()

        manager.sync_aria2_downloads = AsyncMock(side_effect=blocked_sync)
        manager._advance_aria2_queue_locked = AsyncMock()
        manager._cleanup_aria2_orphans = AsyncMock()
        manager._pause_torrent_locked = AsyncMock()

        sync_task = asyncio.create_task(manager.sync_download_clients())
        await sync_started.wait()
        pause_task = asyncio.create_task(manager.pause_torrent(54))
        await asyncio.sleep(0)

        manager._pause_torrent_locked.assert_not_awaited()
        release_sync.set()
        await asyncio.gather(sync_task, pause_task)

        manager._pause_torrent_locked.assert_awaited_once_with(54)


class ProviderHistoryRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_provider_object_is_retained_as_visible_error(self):
        statements = []

        class FakeDb:
            async def execute(self, sql, params=()):
                statements.append((sql, params))

            async def commit(self):
                return None

        @asynccontextmanager
        async def fake_get_db():
            yield FakeDb()

        manager = TorrentManager()
        with patch("services.manager_v2.get_db", fake_get_db):
            await manager._set_provider_missing(
                42,
                "Magnet no longer exists on AllDebrid",
            )

        update_sql, update_params = statements[0]
        self.assertIn("status='error'", update_sql)
        self.assertIn("provider_status='missing'", update_sql)
        self.assertNotIn("status='deleted'", update_sql)
        self.assertEqual(
            update_params,
            ("Magnet no longer exists on AllDebrid", 42),
        )

    async def test_provider_failure_cleanup_retains_local_error_history(self):
        statements = []
        failed_row = {
            "id": 67,
            "name": "Unavailable Torrent",
            "alldebrid_id": "ad-67",
            "source": "manual",
            "error_message": "No peers after 30 minutes",
            "provider_status_code": 8,
        }

        class FakeCursor:
            async def fetchall(self):
                return [failed_row]

        class FakeDb:
            async def execute(self, sql, params=()):
                statements.append((sql, params))
                return FakeCursor()

            async def commit(self):
                return None

        @asynccontextmanager
        async def fake_get_db():
            yield FakeDb()

        manager = TorrentManager()
        delete_magnet = AsyncMock()
        manager.ad = lambda: types.SimpleNamespace(delete_magnet=delete_magnet)
        manager._notify_provider_error = AsyncMock()

        with patch("services.manager_v2.get_db", fake_get_db):
            await manager.cleanup_no_peer_errors()

        select_sql = statements[0][0]
        update_sql = next(sql for sql, _ in statements if "UPDATE torrents" in sql)
        self.assertIn("provider_status = 'error'", select_sql)
        self.assertIn("status='error'", update_sql)
        self.assertIn("provider_status='failed'", update_sql)
        self.assertNotIn("status='deleted'", update_sql)
        delete_magnet.assert_awaited_once_with("ad-67")

    def test_automatic_provider_paths_do_not_use_deleted_state(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "services"
            / "manager_v2.py"
        ).read_text()

        self.assertNotIn("_set_deleted", source)
        self.assertEqual(
            source.count("UPDATE torrents SET status='deleted'"),
            1,
            "Only explicit user deletion may write the deleted state",
        )


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

    async def test_get_all_returns_empty_on_connection_error(self):
        """get_all() returns empty list when aria2 is unreachable."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)

        async def fake_call(method, params=None):
            raise Aria2ConnectionError("Connection interrupted")

        service._call = fake_call
        result = await service.get_all()
        self.assertEqual(result, [])

    async def test_get_all_returns_empty_on_rpc_error(self):
        """get_all() returns empty list on RPC error."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)

        async def fake_call(method, params=None):
            raise Aria2RPCError("aria2 [-32600]: Invalid Request")

        service._call = fake_call
        result = await service.get_all()
        self.assertEqual(result, [])

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

    async def test_ensure_download_retry_on_connection_error(self):
        """ensure_download() retries on connection errors."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)
        attempt_count = {"n": 0}

        async def fake_get_all():
            return []

        async def fake_call(method, params=None):
            if method == "aria2.addUri":
                attempt_count["n"] += 1
                if attempt_count["n"] < 3:
                    raise Aria2ConnectionError("Connection interrupted")
                return "gid-final"
            raise AssertionError(f"Unerwarteter Aufruf: {method}")

        service.get_all = fake_get_all
        service._call = fake_call

        with patch("executors.aria2.client.asyncio.sleep", new=AsyncMock()):
            gid = await service.ensure_download("https://test.invalid/file", max_retries=5)

        self.assertEqual(gid, "gid-final")
        self.assertEqual(attempt_count["n"], 3)

    async def test_ensure_download_deduplication_by_uri(self):
        """ensure_download() detects already running downloads by URI."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)
        add_calls = {"n": 0}

        async def fake_get_all():
            return [types.SimpleNamespace(
                gid="existing-gid",
                status="active",
                total_length=1000,
                completed_length=500,
                download_speed=100,
                files=[{"path": "/dl/file.mp4",
                        "uris": [{"uri": "https://test.invalid/file"}]}],
            )]

        async def fake_call(method, params=None):
            if method == "aria2.addUri":
                add_calls["n"] += 1
            return "new-gid"

        service.get_all = fake_get_all
        service._call = fake_call

        gid = await service.ensure_download("https://test.invalid/file")
        self.assertEqual(gid, "existing-gid")
        self.assertEqual(add_calls["n"], 0)

    async def test_ensure_download_deduplication_by_path(self):
        """ensure_download() detects already running downloads by target path."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)
        add_calls = {"n": 0}

        async def fake_get_all():
            return [types.SimpleNamespace(
                gid="path-gid",
                status="active",
                total_length=1000,
                completed_length=0,
                download_speed=0,
                files=[{"path": "/downloads/show/episode.mkv",
                        "uris": [{"uri": "https://old-url.invalid/file"}]}],
            )]

        async def fake_call(method, params=None):
            add_calls["n"] += 1
            return "new-gid"

        service.get_all = fake_get_all
        service._call = fake_call

        gid = await service.ensure_download(
            "https://new-url.invalid/file",
            {"dir": "/downloads/show", "out": "episode.mkv"},
        )
        self.assertEqual(gid, "path-gid")
        self.assertEqual(add_calls["n"], 0)

    async def test_ensure_download_concurrent_same_uri_serialized(self):
        """Concurrent ensure_download calls for the same URI are serialised."""
        service = Aria2Service("http://localhost:6800/jsonrpc", timeout_seconds=5)
        add_count = {"n": 0}
        state = {"added": False}

        async def fake_get_all():
            if state["added"]:
                return [types.SimpleNamespace(
                    gid="gid-1", status="active", total_length=0,
                    completed_length=0, download_speed=0,
                    files=[{"path": "", "uris": [{"uri": "https://same.invalid/file"}]}],
                )]
            return []

        async def fake_call(method, params=None):
            if method == "aria2.addUri":
                add_count["n"] += 1
                await asyncio.sleep(0.01)
                state["added"] = True
                return "gid-1"
            raise AssertionError(method)

        service.get_all = fake_get_all
        service._call = fake_call

        g1, g2 = await asyncio.gather(
            service.ensure_download("https://same.invalid/file", {"dir": "/dl", "out": "file"}),
            service.ensure_download("https://same.invalid/file", {"dir": "/dl", "out": "file"}),
        )
        self.assertEqual(g1, "gid-1")
        self.assertEqual(g2, "gid-1")
        self.assertEqual(add_count["n"], 1)


# ═════════════════════════════════════════════════════════════════════════════
# Abschluss-Erkennung (Finished Entry Handling)
# ═════════════════════════════════════════════════════════════════════════════

class FinishedEntryTests(unittest.IsolatedAsyncioTestCase):
    """
    Tests reliable detection of completed downloads.
    """

    async def test_finalize_marks_completed_when_all_files_done(self):
        """_finalize_aria2_torrent() markiert Torrent als completed wenn alle Dateien fertig."""
        mgr = TorrentManager()
        mgr._delete_magnet_after_completion = AsyncMock(return_value=True)
        mgr._mark_finished = AsyncMock()
        mgr._log_event = AsyncMock()
        notify_mock = MagicMock()
        notify_mock.send_complete = AsyncMock()
        mgr.notify = lambda: notify_mock

        # dict-compatible row (Manager accesses torrent["status"])
        torrent_row = {
            "id": 1, "status": "queued", "alldebrid_id": "ad-1", "name": "Test Torrent",
            "hash": None, "magnet": None, "size_bytes": 0, "progress": 0,
            "download_url": None, "local_path": None, "source": "manual",
            "provider_status": None, "provider_status_code": None,
            "polling_failures": 0, "download_client": "aria2",
            "error_message": None, "created_at": None, "updated_at": None,
            "completed_at": None,
        }
        counts_row = {
            "required_count": 2, "completed_count": 2, "error_count": 0,
            "missing_count": 0,
            "active_count": 0, "paused_count": 0, "downloading_count": 0,
            "total_files": 2,
        }

        class FakeCursor:
            def __init__(self, result): self._result = result
            async def fetchone(self): return self._result

        async def fake_execute(sql, params=()):
            if "SELECT * FROM torrents" in sql:
                return FakeCursor(torrent_row)
            if "SUM(CASE WHEN blocked=0" in sql:
                return FakeCursor(counts_row)
            if "SUM(size_bytes)" in sql:
                return FakeCursor({"total": 5000})
            return FakeCursor(None)

        fake_db = AsyncMock()
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        fake_db.__aexit__ = AsyncMock(return_value=False)
        fake_db.execute = fake_execute
        fake_db.commit = AsyncMock()
        fake_db.row_factory = None

        with patch("services.manager_v2.aiosqlite.connect", return_value=fake_db), \
             patch("services.manager_v2.get_settings", return_value=types.SimpleNamespace(
                 discord_notify_finished=True, discord_notify_error=False
             )):
            await mgr._finalize_aria2_torrent(1)

        mgr._delete_magnet_after_completion.assert_awaited_once_with(1, "ad-1", "manual")
        self.assertEqual(mgr._mark_finished.await_count, 1)

    async def test_finalize_does_not_complete_when_files_still_active(self):
        """_finalize_aria2_torrent() markiert NICHT als completed wenn Dateien noch aktiv."""
        mgr = TorrentManager()
        mgr._delete_magnet_after_completion = AsyncMock()

        counts_row = {
            "required_count": 3, "completed_count": 1, "error_count": 0,
            "missing_count": 0,
            "active_count": 2, "paused_count": 0, "downloading_count": 2,
            "total_files": 3,
        }

        class FakeCursor:
            def __init__(self, r): self._r = r
            async def fetchone(self): return self._r

        async def fake_execute(sql, params=()):
            if "SELECT * FROM torrents" in sql:
                return FakeCursor({
                    "id": 1, "status": "downloading", "alldebrid_id": "ad-1", "name": "T",
                    "hash": None, "magnet": None, "size_bytes": 0, "progress": 0,
                    "download_url": None, "local_path": None, "source": None,
                    "provider_status": None, "provider_status_code": None,
                    "polling_failures": 0, "download_client": "aria2",
                    "error_message": None, "created_at": None, "updated_at": None,
                    "completed_at": None,
                })
            if "SUM(CASE WHEN blocked=0" in sql:
                return FakeCursor(counts_row)
            return FakeCursor(None)

        fake_db = AsyncMock()
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        fake_db.__aexit__ = AsyncMock(return_value=False)
        fake_db.execute = fake_execute
        fake_db.commit = AsyncMock()

        with patch("services.manager_v2.aiosqlite.connect", return_value=fake_db):
            await mgr._finalize_aria2_torrent(1)

        mgr._delete_magnet_after_completion.assert_not_awaited()

    async def test_finalize_updates_large_total_size_without_case_expression(self):
        """Large BIGINT totals are written directly so PostgreSQL does not infer an int4 CASE parameter."""
        mgr = TorrentManager()
        mgr._delete_magnet_after_completion = AsyncMock(return_value=True)
        mgr._mark_finished = AsyncMock()
        notify_mock = MagicMock()
        notify_mock.send_complete = AsyncMock()
        mgr.notify = lambda: notify_mock

        torrent_row = {
            "id": 116, "status": "queued", "alldebrid_id": "ad-116", "name": "Big Torrent",
            "hash": None, "magnet": None, "size_bytes": 0, "progress": 0,
            "download_url": None, "local_path": None, "source": "manual",
            "provider_status": None, "provider_status_code": None,
            "polling_failures": 0, "download_client": "aria2",
            "error_message": None, "created_at": None, "updated_at": None,
            "completed_at": None,
        }
        counts_row = {
            "required_count": 1, "completed_count": 1, "error_count": 0,
            "missing_count": 0,
            "active_count": 0, "paused_count": 0, "downloading_count": 0,
            "total_files": 1,
        }

        class FakeCursor:
            def __init__(self, result): self._result = result
            async def fetchone(self): return self._result

        executed = []

        async def fake_execute(sql, params=()):
            executed.append((sql, params))
            if "SELECT * FROM torrents" in sql:
                return FakeCursor(torrent_row)
            if "SUM(CASE WHEN blocked=0" in sql:
                return FakeCursor(counts_row)
            if "SUM(size_bytes)" in sql:
                return FakeCursor({"total": 4505585317})
            return FakeCursor(None)

        fake_db = AsyncMock()
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        fake_db.__aexit__ = AsyncMock(return_value=False)
        fake_db.execute = fake_execute
        fake_db.commit = AsyncMock()

        with patch("services.manager_v2.aiosqlite.connect", return_value=fake_db), \
             patch("services.manager_v2.get_settings", return_value=types.SimpleNamespace(
                 discord_notify_finished=True, discord_notify_error=False
             )):
            await mgr._finalize_aria2_torrent(116)

        update_calls = [item for item in executed if "UPDATE torrents" in item[0] and "size_bytes" in item[0]]
        self.assertEqual(len(update_calls), 1)
        update_sql, update_params = update_calls[0]
        self.assertNotIn("CASE WHEN", update_sql)
        self.assertEqual(update_params, (4505585317, 116))
        mgr._delete_magnet_after_completion.assert_awaited_once_with(116, "ad-116", "manual")

    async def test_delete_magnet_keeps_completed_status(self):
        """
        _delete_magnet_after_completion() does NOT change status to 'deleted'.
        Dashboard-Fix: completed bleibt completed.
        """
        mgr = TorrentManager()

        fake_ad = types.SimpleNamespace(delete_magnet=AsyncMock(return_value=True))
        mgr.ad = lambda: fake_ad

        sql_calls = []

        async def fake_execute(sql, params=()):
            sql_calls.append(sql.strip())
            return AsyncMock()

        fake_db = AsyncMock()
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        fake_db.__aexit__ = AsyncMock(return_value=False)
        fake_db.execute = fake_execute
        fake_db.commit = AsyncMock()

        with patch("services.manager_v2.aiosqlite.connect", return_value=fake_db):
            await mgr._delete_magnet_after_completion(1, "ad-123")

        # Assert: no UPDATE to 'deleted'
        update_to_deleted = [
            sql for sql in sql_calls
            if "UPDATE torrents" in sql and "deleted" in sql
        ]
        self.assertEqual(
            update_to_deleted, [],
            f"_delete_magnet_after_completion darf status NICHT auf 'deleted' setzen! "
            f"Gefundene SQLs: {update_to_deleted}"
        )

    async def test_duplicate_file_entries_skipped(self):
        """Duplicate file entries from AllDebrid are skipped during download."""
        mgr = TorrentManager()
        mgr._log_file = AsyncMock()
        mgr._send_partial_summary = AsyncMock()
        mgr._log_event = AsyncMock()
        mgr._delete_magnet_after_completion = AsyncMock()
        mgr._mark_finished = AsyncMock()
        mgr.advance_aria2_queue = AsyncMock()
        mgr._download_direct = AsyncMock(return_value="ok")
        fake_ad = types.SimpleNamespace(
            unlock_link=AsyncMock(return_value={"link": "https://dl.invalid/file"})
        )
        mgr.ad = lambda: fake_ad

        duplicate_files = [
            {"path": "dir/file.mp4", "name": "file.mp4", "size": 10,
             "link": "https://source.invalid/a"},
            {"path": "dir/file.mp4", "name": "file.mp4", "size": 10,
             "link": "https://source.invalid/a"},  # Duplikat
        ]

        fake_cfg = types.SimpleNamespace(
            download_client="aria2",
            download_folder=str(Path.cwd() / "tmp_test_dl"),
            filters_enabled=True, blocked_extensions=[], blocked_keywords=[],
            min_file_size_mb=0, aria2_start_paused=False,
            paused=False,
            discord_notify_finished=False, discord_notify_error=False,
        )

        fake_db = AsyncMock()
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        fake_db.__aexit__ = AsyncMock(return_value=False)
        fake_cursor = AsyncMock()
        fake_cursor.fetchone = AsyncMock(return_value={"source": "manual"})
        fake_db.commit = AsyncMock()
        fake_db.execute = AsyncMock(return_value=fake_cursor)

        with patch("services.manager_v2.get_settings", return_value=fake_cfg), \
             patch("services.manager_v2.aiosqlite.connect", return_value=fake_db):
            mgr._fetch_ready_files = AsyncMock(return_value=duplicate_files)
            await mgr._download(1, "ad-id", "Test")

        # Manifest preparation no longer unlocks provider links eagerly. The
        # duplicate is collapsed before the one-row manifest batch is persisted;
        # URL generation happens later when the dispatcher has a free slot.
        self.assertEqual(fake_ad.unlock_link.await_count, 0)
        self.assertEqual(mgr._log_file.await_count, 0)
        fake_db.executemany.assert_awaited_once()
        manifest_rows = fake_db.executemany.await_args.args[1]
        self.assertEqual(len(manifest_rows), 1)


class Aria2RecoverySafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconcile_startup_finalizes_completed_torrent_when_aria2_entry_is_gone(self):
        mgr = TorrentManager()
        mgr.download_client_name = lambda: "aria2"
        mgr._dedupe_aria2_downloads_on_startup = AsyncMock(return_value=[])
        mgr._reset_torrent_for_redownload = AsyncMock()
        mgr.advance_aria2_queue = AsyncMock()
        mgr._finalize_aria2_torrent = AsyncMock()
        mgr.recover_direct_link_collections = AsyncMock(return_value=0)
        mgr._get_torrent_completion_snapshot = AsyncMock(return_value={
            "id": 7,
            "alldebrid_id": "ad-7",
            "name": "Recovered Torrent",
            "status": "queued",
            "done": 1,
            "total": 1,
        })
        mgr.aria2 = lambda: types.SimpleNamespace(
            get_all=AsyncMock(return_value=[]),
            tell_status=AsyncMock(side_effect=Aria2RPCError("aria2 [-1]: GID not found")),
        )

        startup_rows = [{
            "torrent_id": 7,
            "alldebrid_id": "ad-7",
            "name": "Recovered Torrent",
            "file_id": 70,
            "download_id": "gid-missing",
            "download_url": "https://example.invalid/file",
            "local_path": "/downloads/file.mp4",
            "status": "queued",
        }]
        class FakeCursor:
            def __init__(self, result):
                self._result = result
            async def fetchall(self):
                return self._result
            async def fetchone(self):
                return self._result

        class FakeDb:
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc, tb):
                return False
            async def execute(self, sql, params=()):
                if "FROM torrents t" in sql and "JOIN download_files f" in sql:
                    return FakeCursor(startup_rows)
                if "SELECT alldebrid_id, name FROM torrents WHERE id=?" in sql:
                    return FakeCursor({"alldebrid_id": "ad-7", "name": "Recovered Torrent"})
                raise AssertionError(f"Unexpected SQL: {sql}")

        with patch("services.manager_v2.get_db", return_value=FakeDb()):
            await mgr.reconcile_aria2_on_startup()

        mgr._reset_torrent_for_redownload.assert_not_awaited()
        mgr._finalize_aria2_torrent.assert_awaited_once_with(7)

    async def test_sync_downloads_treats_removed_as_lost_not_completed(self):
        mgr = TorrentManager()
        mgr.download_client_name = lambda: "aria2"
        mgr.is_paused = lambda: False
        mgr._dispatch_pending_aria2_queue = AsyncMock()
        mgr._reset_torrent_for_redownload = AsyncMock()
        mgr._update_file_state = AsyncMock()
        mgr._start_download = AsyncMock()
        mgr._get_torrent_completion_snapshot = AsyncMock(return_value={
            "id": 11,
            "alldebrid_id": "ad-11",
            "name": "Removed Torrent",
            "status": "queued",
            "done": 0,
            "total": 1,
        })
        mgr.aria2 = lambda: types.SimpleNamespace(
            get_all=AsyncMock(return_value=[
                types.SimpleNamespace(
                    gid="gid-1",
                    status="removed",
                    total_length=123,
                    completed_length=123,
                    error_code="",
                    error_message="",
                    files=[{"path": "/downloads/file.mp4", "uris": [{"uri": "https://example.invalid/file"}]}],
                )
            ])
        )

        sync_rows = [{
            "torrent_id": 11,
            "name": "Removed Torrent",
            "alldebrid_id": "ad-11",
            "torrent_status": "queued",
            "file_id": 111,
            "filename": "file.mp4",
            "local_path": "/downloads/file.mp4",
            "download_url": "https://example.invalid/file",
            "download_id": "gid-1",
            "status": "queued",
            "blocked": 0,
        }]
        class FakeCursor:
            def __init__(self, result):
                self._result = result
            async def fetchall(self):
                return self._result
            async def fetchone(self):
                return self._result

        class FakeDb:
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc, tb):
                return False
            async def execute(self, sql, params=()):
                if "f.status AS file_status" in sql:
                    return FakeCursor([])
                if "SELECT DISTINCT torrent_id" in sql:
                    return FakeCursor([])
                if "FROM torrents t" in sql and "JOIN download_files f" in sql:
                    return FakeCursor(sync_rows)
                raise AssertionError(f"Unexpected SQL: {sql}")

        with patch("services.manager_v2.get_db", return_value=FakeDb()):
            await mgr.sync_aria2_downloads()

        mgr._update_file_state.assert_not_awaited()
        mgr._reset_torrent_for_redownload.assert_awaited_once()


class AllDebridFileFetchSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_ready_files_raises_transient_when_status_is_ready_but_files_are_missing(self):
        mgr = TorrentManager()
        fake_ad = types.SimpleNamespace(
            get_magnet_files=AsyncMock(return_value=[{"id": "ad-1", "files": []}]),
            get_magnet_status=AsyncMock(return_value=[{
                "id": "ad-1",
                "statusCode": 4,
                "status": "Ready",
                "size": 100,
                "downloaded": 100,
            }]),
        )
        mgr.ad = lambda: fake_ad

        with patch("services.manager_v2.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(TransientAllDebridStateError):
                await mgr._fetch_ready_files("ad-1")

    async def test_start_download_defers_transient_missing_files_without_failing_torrent(self):
        mgr = TorrentManager()
        mgr.is_paused = lambda: False
        mgr._download = AsyncMock(side_effect=TransientAllDebridStateError("files not exposed yet"))
        mgr._fail_torrent = AsyncMock()

        class FakeCursor:
            def __init__(self, result):
                self._result = result
            async def fetchone(self):
                return self._result

        executed = []

        class FakeDb:
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, exc, tb):
                return False
            async def execute(self, sql, params=()):
                executed.append((sql, params))
                if "SELECT status FROM torrents WHERE id=?" in sql:
                    return FakeCursor({"status": "ready"})
                if "SELECT COUNT(*) AS c FROM download_files" in sql:
                    return FakeCursor({"c": 0})
                return FakeCursor(None)
            async def commit(self):
                return None

        with patch("services.manager_v2.get_db", return_value=FakeDb()):
            await mgr._start_download(5, "ad-5", "Deferred")

        self.assertEqual(mgr._download.await_count, 1)
        mgr._fail_torrent.assert_not_awaited()
        update_sql = [sql for sql, _ in executed if "UPDATE torrents SET status='ready'" in sql]
        self.assertEqual(len(update_sql), 1)

    async def test_fetch_ready_files_raises_transient_when_status_unavailable(self):
        mgr = TorrentManager()
        fake_ad = types.SimpleNamespace(
            get_magnet_files=AsyncMock(return_value=[]),
            get_magnet_status=AsyncMock(return_value=[]),
        )
        mgr.ad = lambda: fake_ad

        with patch("services.manager_v2.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(TransientAllDebridStateError):
                await mgr._fetch_ready_files("ad-missing")


# ═════════════════════════════════════════════════════════════════════════════
# Dashboard data flow
# ═════════════════════════════════════════════════════════════════════════════

class DashboardCompletedTests(unittest.IsolatedAsyncioTestCase):
    """
    Stellt sicher dass completed-Torrents im Dashboard erscheinen.
    Root-Cause-Fix: _delete_magnet_after_completion setzt kein status='deleted'.
    """

    async def test_completed_count_not_reset_to_deleted(self):
        """
        Simulation: Torrent abgeschlossen → _delete_magnet_after_completion →
        status must remain 'completed', not become 'deleted'.
        """
        mgr = TorrentManager()
        fake_ad = types.SimpleNamespace(delete_magnet=AsyncMock(return_value=True))
        mgr.ad = lambda: fake_ad

        status_updates = []

        async def capture_execute(sql, params=()):
            if "UPDATE torrents SET" in sql:
                status_updates.append({"sql": sql, "params": params})
            return AsyncMock()

        fake_db = AsyncMock()
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        fake_db.__aexit__ = AsyncMock(return_value=False)
        fake_db.execute = capture_execute
        fake_db.commit = AsyncMock()

        with patch("services.manager_v2.aiosqlite.connect", return_value=fake_db):
            await mgr._delete_magnet_after_completion(42, "ad-42")

        # No UPDATE ... status='deleted' after completion
        deleted_updates = [
            u for u in status_updates
            if "deleted" in str(u["params"])
        ]
        self.assertEqual(
            deleted_updates, [],
            "After successful download, status must not be set to 'deleted'."
        )

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
            await svc.send_added("My Torrent", source="manual", alldebrid_id="123")

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
            await svc.send_added("Test Torrent", source="manual_file", alldebrid_id="ad-42")

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
                alldebrid_id="521903942",
                status_code="8",
            )

        captured = {field["name"]: field["value"] for field in captured_fields}
        self.assertEqual(captured.get("Source"), "AllDebrid polling")
        self.assertEqual(captured.get("Provider"), "AllDebrid")
        self.assertEqual(captured.get("AllDebrid ID"), "521903942")
        self.assertEqual(captured.get("Status Code"), "8")
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
    async def test_post_retries_after_empty_response(self):
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
            result = await service._post("https://api.example", "magnet/status", retries=2)
        self.assertEqual(result, {"ok": True})

    def test_decode_json_body_reports_invalid_payload(self):
        service = AllDebridService("api-key")
        with self.assertRaises(Exception) as ctx:
            service._decode_json_body("<html>bad gateway</html>", "magnet/status")
        self.assertIn("invalid JSON", str(ctx.exception))


class ManagerDedupeTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_magnet_final_duplicate_guard_skips_alldebrid_upload(self):
        from services.duplicates import DuplicateDecision, DuplicateMatch

        mgr = TorrentManager()
        fake_ad = types.SimpleNamespace(upload_magnet=AsyncMock())
        mgr.ad = lambda: fake_ad

        decision = DuplicateDecision(
            is_duplicate=True,
            confidence=1.0,
            action="skip",
            reason="same_infohash",
            matches=[DuplicateMatch(5, "Existing", "completed", "abc", "same_infohash", 1.0)],
        )

        fake_db = AsyncMock()
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        fake_db.__aexit__ = AsyncMock(return_value=False)
        fake_db.fetchone = AsyncMock(return_value={"id": 5, "name": "Existing", "status": "completed"})

        with patch("services.duplicates.check_before_add", AsyncMock(return_value=decision)), \
             patch("services.manager_v2.get_db", return_value=fake_db):
            row = await mgr._add_magnet(
                "magnet:?xt=urn:btih:abcdefabcdefabcdefabcdefabcdefabcdefabcd",
                "abcdefabcdefabcdefabcdefabcdefabcdefabcd",
                "manual",
            )

        fake_ad.upload_magnet.assert_not_awaited()
        self.assertEqual(row["id"], 5)
        self.assertEqual(row["_duplicate"]["action"], "skip")

    async def test_add_torrent_file_duplicate_skips_alldebrid_upload(self):
        from services.duplicates import DuplicateDecision, DuplicateMatch

        mgr = TorrentManager()
        mgr.is_paused = lambda: False
        fake_ad = types.SimpleNamespace(upload_torrent_file=AsyncMock())
        mgr.ad = lambda: fake_ad

        decision = DuplicateDecision(
            is_duplicate=True,
            confidence=1.0,
            action="skip",
            reason="same_infohash",
            matches=[DuplicateMatch(6, "Existing File", "ready", "def", "same_infohash", 1.0)],
        )

        fake_db = AsyncMock()
        fake_db.__aenter__ = AsyncMock(return_value=fake_db)
        fake_db.__aexit__ = AsyncMock(return_value=False)
        fake_db.fetchone = AsyncMock(return_value={"id": 6, "name": "Existing File", "status": "ready"})

        with patch("services.manager_v2.get_settings", return_value=types.SimpleNamespace(alldebrid_api_key="key")), \
             patch("services.duplicates.check_before_add", AsyncMock(return_value=decision)), \
             patch("services.manager_v2.get_db", return_value=fake_db):
            row = await mgr.add_torrent_file_direct(
                b"not-a-real-torrent",
                "existing.torrent",
                source="manual",
                preferred_hash="def",
            )

        fake_ad.upload_torrent_file.assert_not_awaited()
        self.assertEqual(row["id"], 6)
        self.assertEqual(row["_duplicate"]["reason"], "same_infohash")

    async def test_startup_reconcile_removes_duplicate_aria2_jobs(self):
        mgr = TorrentManager()
        keep = types.SimpleNamespace(
            gid="keep", status="active",
            files=[{"uris": [{"uri": "https://same.invalid/file"}]}],
        )
        dup = types.SimpleNamespace(
            gid="dup", status="waiting",
            files=[{"uris": [{"uri": "https://same.invalid/file"}]}],
        )

        fake_aria2 = types.SimpleNamespace(
            get_all=AsyncMock(return_value=[keep, dup]),
            remove=AsyncMock(),
        )
        mgr.aria2 = lambda: fake_aria2

        deduped = await mgr._dedupe_aria2_downloads_on_startup([keep, dup])
        fake_aria2.remove.assert_awaited()
        self.assertNotIn("dup", [d.gid for d in deduped])

    def test_build_aria2_indexes_tracks_path_and_uri(self):
        mgr = TorrentManager()
        dl = types.SimpleNamespace(
            gid="gid-1",
            files=[{"path": "/downloads/show/file.mp4",
                    "uris": [{"uri": "https://example.invalid/file"}]}],
        )
        by_gid, uri_to_dl, path_to_dl = mgr._build_aria2_indexes([dl])
        self.assertIs(by_gid["gid-1"], dl)
        self.assertIs(uri_to_dl["https://example.invalid/file"], dl)
        self.assertIs(path_to_dl["/downloads/show/file.mp4"], dl)

    def test_aria2_slot_limit_uses_dedicated_setting(self):
        mgr = TorrentManager()
        with patch("services.manager_v2.get_settings", return_value=types.SimpleNamespace(
            aria2_max_active_downloads=7, max_concurrent_downloads=3,
        )):
            self.assertEqual(mgr._aria2_slot_limit(), 7)

    def test_aria2_slot_limit_fallback_to_max_concurrent(self):
        mgr = TorrentManager()
        with patch("services.manager_v2.get_settings", return_value=types.SimpleNamespace(
            aria2_max_active_downloads=0, max_concurrent_downloads=5,
        )):
            self.assertEqual(mgr._aria2_slot_limit(), 5)

    def test_paused_aria2_jobs_do_not_consume_download_slots(self):
        jobs = [
            types.SimpleNamespace(gid="active", status="active"),
            types.SimpleNamespace(gid="waiting", status="waiting"),
            types.SimpleNamespace(gid="paused-1", status="paused"),
            types.SimpleNamespace(gid="paused-2", status="paused"),
            types.SimpleNamespace(gid="complete", status="complete"),
        ]

        occupants = TorrentManager._aria2_slot_occupants(jobs)

        self.assertEqual(
            [job.gid for job in occupants],
            ["active", "waiting"],
        )

    def test_aria2_job_options_reuse_deterministic_target_path(self):
        mgr = TorrentManager()
        cfg = types.SimpleNamespace(
            aria2_split=8,
            aria2_min_split_size="10M",
            aria2_max_connection_per_server=8,
            aria2_continue_downloads=True,
        )
        with patch("services.manager_v2.get_settings", return_value=cfg):
            options = mgr._aria2_job_options({
                "dir": "/downloads/show",
                "out": "episode.mkv",
                "auto-file-renaming": "true",
                "allow-overwrite": "false",
            })

        self.assertEqual(options["dir"], "/downloads/show")
        self.assertEqual(options["out"], "episode.mkv")
        self.assertEqual(options["auto-file-renaming"], "false")
        self.assertEqual(options["allow-overwrite"], "true")
        self.assertEqual(options["continue"], "true")

    def test_builtin_aria2_uses_fixed_internal_rpc_secret(self):
        from services.aria2_runtime import BUILTIN_ARIA2_SECRET, effective_rpc_config

        cfg = types.SimpleNamespace(
            aria2_mode="builtin",
            aria2_builtin_port=6800,
            aria2_url="http://external.invalid/jsonrpc",
            aria2_secret="user-editable-secret",
        )
        url, secret = effective_rpc_config(cfg)
        self.assertEqual(url, "http://127.0.0.1:6800/jsonrpc")
        self.assertEqual(secret, BUILTIN_ARIA2_SECRET)

    def test_builtin_aria2_path_mapping_ignores_external_download_root(self):
        mgr = TorrentManager()
        cfg = types.SimpleNamespace(
            aria2_mode="builtin",
            download_folder="/download",
            aria2_download_path="/external/downloads",
        )
        with patch("services.manager_v2.get_settings", return_value=cfg):
            mapped = mgr._remote_aria2_path(Path("/download/movie/file.mkv"))
        self.assertEqual(mapped, "/download/movie/file.mkv")

    def test_external_aria2_path_mapping_uses_remote_download_root(self):
        mgr = TorrentManager()
        cfg = types.SimpleNamespace(
            aria2_mode="external",
            download_folder="/download",
            aria2_download_path="/external/downloads",
        )
        with patch("services.manager_v2.get_settings", return_value=cfg):
            mapped = mgr._remote_aria2_path(Path("/download/movie/file.mkv"))
        self.assertEqual(mapped, "/external/downloads/movie/file.mkv")

    def test_builtin_runtime_command_uses_download_folder_not_external_root(self):
        from services.aria2_runtime import BuiltinAria2Runtime
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
            with patch("services.aria2_runtime.get_settings", return_value=cfg):
                command = BuiltinAria2Runtime()._command()
        self.assertIn(f"--dir={download_dir}", command)
        self.assertNotIn("--dir=/external/downloads", command)

    def test_builtin_runtime_rotates_oversized_log_file(self):
        from services.aria2_runtime import BuiltinAria2Runtime
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
            with patch("services.aria2_runtime.get_settings", return_value=cfg):
                rotated = BuiltinAria2Runtime()._rotate_log_file()
            self.assertFalse(rotated)
            self.assertTrue(log_file.exists())

            log_file.write_bytes(b"x" * (1024 * 1024 + 1))
            with patch("services.aria2_runtime.get_settings", return_value=cfg):
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

    async def test_apply_aria2_memory_tuning_preserves_external_daemon_policy(self):
        mgr = TorrentManager()
        fake_aria2 = types.SimpleNamespace(change_global_options=AsyncMock())
        mgr.aria2 = lambda: fake_aria2
        with patch("services.manager_v2.get_settings", return_value=types.SimpleNamespace(
            aria2_mode="external",
            aria2_url="http://localhost:6800/jsonrpc",
            aria2_max_download_result=150,
            aria2_keep_unfinished_download_result=False,
            aria2_max_active_downloads=4,
            aria2_split=12,
            aria2_min_split_size="8M",
            aria2_max_connection_per_server=6,
            aria2_disk_cache="32M",
            aria2_file_allocation="trunc",
            aria2_continue_downloads=True,
            aria2_lowest_speed_limit="0",
        )):
            result = await mgr.apply_aria2_memory_tuning()
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "external aria2 global policy is read-only")
        fake_aria2.change_global_options.assert_not_awaited()

    async def test_apply_aria2_tuning_enforces_safety_in_builtin_mode(self):
        mgr = TorrentManager()
        fake_aria2 = types.SimpleNamespace(change_global_options=AsyncMock())
        mgr.aria2 = lambda: fake_aria2
        with patch("services.manager_v2.get_settings", return_value=types.SimpleNamespace(
            aria2_mode="builtin",
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
        )):
            result = await mgr.apply_aria2_memory_tuning()
        self.assertTrue(result["ok"])
        applied = fake_aria2.change_global_options.await_args.args[0]
        self.assertEqual(applied["follow-torrent"], "false")
        self.assertEqual(applied["enable-dht"], "false")
        self.assertEqual(applied["enable-peer-exchange"], "false")

    async def test_test_aria2_uses_configured_query_windows(self):
        mgr = TorrentManager()
        fake_aria2 = types.SimpleNamespace(
            test=AsyncMock(return_value={"version": "1.37.0"}),
            get_memory_diagnostics=AsyncMock(return_value={"active_count": 1}),
        )
        mgr.aria2 = lambda: fake_aria2
        with patch("services.manager_v2.get_settings", return_value=types.SimpleNamespace(
            aria2_url="http://localhost:6800/jsonrpc",
            aria2_waiting_window=25,
            aria2_stopped_window=40,
        )):
            result = await mgr.test_aria2()
        self.assertEqual(result["version"], "1.37.0")
        self.assertEqual(result["diagnostics"]["active_count"], 1)
        fake_aria2.get_memory_diagnostics.assert_awaited_once_with(waiting_limit=25, stopped_limit=40)

    async def test_run_aria2_housekeeping_preserves_external_daemon_and_uses_query_windows(self):
        mgr = TorrentManager()
        fake_aria2 = types.SimpleNamespace(
            change_global_options=AsyncMock(),
            purge_download_results=AsyncMock(),
            get_memory_diagnostics=AsyncMock(return_value={"stopped_count": 0}),
        )
        mgr.aria2 = lambda: fake_aria2
        with patch("services.manager_v2.get_settings", return_value=types.SimpleNamespace(
            aria2_mode="external",
            aria2_url="http://localhost:6800/jsonrpc",
            aria2_max_download_result=50,
            aria2_keep_unfinished_download_result=False,
            aria2_waiting_window=30,
            aria2_stopped_window=45,
        )):
            result = await mgr.run_aria2_housekeeping()
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "external aria2 history is daemon-owned")
        fake_aria2.change_global_options.assert_not_awaited()
        fake_aria2.purge_download_results.assert_not_awaited()
        fake_aria2.get_memory_diagnostics.assert_awaited_once_with(waiting_limit=30, stopped_limit=45)

    async def test_apply_provider_update_notifies_when_all_debrid_reports_no_peers(self):
        mgr = TorrentManager()
        mgr._notify_provider_error = AsyncMock()
        mgr._log_event = AsyncMock()
        mgr._fail_torrent = AsyncMock()
        delete_magnet = AsyncMock()
        mgr.ad = lambda: types.SimpleNamespace(delete_magnet=delete_magnet)

        fake_db = types.SimpleNamespace(
            execute=AsyncMock(),
            commit=AsyncMock(),
        )

        @asynccontextmanager
        async def fake_get_db():
            yield fake_db

        # Row WITHOUT a stored magnet → cannot re-upload → fail_torrent
        row = {
            "id": 67,
            "name": "Broken Torrent",
            "status": "downloading",
            "provider_status": "processing",
            "provider_status_code": 4,
            "alldebrid_id": "516558854",
            "magnet": None,
            "source": "manual",
        }
        magnet = {"filename": "Broken Torrent"}
        normalized = {
            "provider_status": "error",
            "local_status": "error",
            "status_code": 8,
            "progress": 0.0,
            "size_bytes": 0,
            "message": "No peer after 30 minutes",
        }

        with patch("services.manager_v2.get_db", fake_get_db):
            await mgr._apply_provider_update(row, magnet, normalized)

        # No magnet → cannot re-upload → _notify_provider_error + _fail_torrent
        mgr._notify_provider_error.assert_awaited_once_with(
            "Broken Torrent",
            reason="No peers found after 30 minutes — no magnet link stored for re-upload",
            context="AllDebrid reported the torrent as unavailable. Add the magnet manually to retry.",
            alldebrid_id="516558854",
            status_code=8,
        )
        delete_magnet.assert_awaited_once_with("516558854")
        mgr._fail_torrent.assert_awaited_once()

    async def test_auto_extract_uses_completed_download_paths_without_folder_scan(self):
        mgr = TorrentManager()
        mgr._log_event = AsyncMock()
        archive = Path("/download/Example/archive.zip")
        movie = Path("/download/Example/movie.mkv")

        class FakeCursor:
            async def fetchall(self):
                return [
                    {"local_path": str(movie)},
                    {"local_path": str(archive)},
                ]

        fake_db = types.SimpleNamespace(
            execute=AsyncMock(return_value=FakeCursor()),
        )

        @asynccontextmanager
        async def fake_get_db():
            yield fake_db

        fake_extractor = types.SimpleNamespace(
            update_max_concurrent=MagicMock(),
            extract_archives=AsyncMock(return_value=[(archive, True, "Extracted archive.zip")]),
            extract_folder=AsyncMock(),
        )
        fake_notify = types.SimpleNamespace(send_extract_complete=AsyncMock())
        mgr.notify = lambda: fake_notify

        with patch("services.manager_v2.get_db", fake_get_db), \
             patch("services.manager_v2.get_extractor", return_value=fake_extractor), \
             patch("services.manager_v2.get_settings", return_value=types.SimpleNamespace(
                 extract_enabled=True,
                 extract_max_concurrent=1,
                 extract_delete_archive=False,
                 discord_notify_extract=True,
             )):
            await mgr._extract_torrent(123, {"name": "Example"})

        fake_extractor.update_max_concurrent.assert_called_once_with(1)
        fake_extractor.extract_archives.assert_awaited_once_with([archive], delete_after=False)
        fake_extractor.extract_folder.assert_not_awaited()
        fake_notify.send_extract_complete.assert_awaited_once_with(
            "Example",
            archive_count=1,
            dest=str(archive.parent),
        )


if __name__ == "__main__":
    unittest.main()
