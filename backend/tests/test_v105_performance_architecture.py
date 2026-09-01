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








