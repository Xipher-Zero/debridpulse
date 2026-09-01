from pathlib import Path

from packaging.version import Version
from executors.aria2.client import Aria2Service

ROOT = Path(__file__).resolve().parents[2]


def source(rel):
    return (ROOT / rel).read_text()


def test_v105_version_and_instrumentation():
    assert Version((ROOT / "VERSION").read_text().strip()) >= Version("1.0.5")
    perf = source("backend/core/performance.py")
    assert "def snapshot()" in perf and "def observe(" in perf


def test_aria2_multicall_and_auth_are_preserved():
    aria2 = source("backend/executors/aria2/client.py")
    assert '"system.multicall"' in aria2
    assert "async def _multicall(" in aria2
    svc = Aria2Service("http://localhost:6800/jsonrpc", secret="secret-value")
    assert svc._authorized_params(["gid"]) == ["token:secret-value", "gid"]


def test_sqlite_hot_indexes_are_preserved():
    db = source("backend/db/database.py")
    for idx in ("idx_dlfiles_queue", "idx_dlfiles_download_id", "idx_torrents_status_priority"):
        assert idx in db
    assert "asyncpg" not in db.lower()


def test_reconciliation_keeps_snapshot_reuse_and_negative_cache():
    src = source("backend/services/reconciliation_service.py")
    for token in ("aria2.scheduler_snapshot_reuse", "confirmed_missing", "aria2.confirm_gid_cache_hits", "_cycle_snapshot"):
        assert token in src


def test_provider_poll_does_not_nest_download_reconciliation():
    manager = source("backend/services/manager_v2.py")
    sync = manager.split("async def sync_alldebrid_status(self):", 1)[1].split("async def deep_sync_aria2_finished", 1)[0]
    assert "sync_download_clients" not in sync


def test_external_aria2_ownership_cache_remains_durable():
    manager = source("backend/services/manager_v2.py")
    assert "self._aria2_owned_gid_cache: Set[str] = set()" in manager
    assert "self._aria2_owned_gid_cache.add(gid)" in manager
    owned = manager.split("async def _aria2_owned_gids", 1)[1].split("async def _aria2_owned_downloads", 1)[0]
    assert "return set(self._aria2_owned_gid_cache)" in owned
    assert "SELECT gid" not in owned


def test_explicit_architecture_replaces_patch_bootstrap():
    manager = source("backend/services/manager_v2.py")
    service = source("backend/services/transfer_service.py")
    control = source("backend/services/transfer_control.py")
    for name in ("TransferControlService", "ReconciliationService", "NotificationService"):
        assert name in service
    assert "bind_architecture" in manager
    assert "_install_transfer_control(manager)" not in manager
    assert "self.manager.pause_torrent =" not in control
