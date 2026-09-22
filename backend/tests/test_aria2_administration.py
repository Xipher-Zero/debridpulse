"""Native administration preserves canonical intent and shared-daemon isolation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_aria2_executor_contract import execution
from executors.aria2.admin import Aria2Administration
from executors.aria2.client import Aria2DownloadStatus
from executors.aria2.definition import Aria2Options
from executors.aria2.runtime import NATIVE_ACTIVE_DOWNLOADS


def _runtime(**options):
    return SimpleNamespace(options=Aria2Options(**options))


@pytest.mark.asyncio
async def test_low_level_pause_and_resume_use_core_transfer_identity(execution):
    attempt = SimpleNamespace(handle=execution.handle, transfer_id=47, artifact_id=9)
    repository = SimpleNamespace(executions=AsyncMock(return_value=(attempt,)), authorize_execution=execution.executor.authorize)
    application = SimpleNamespace(pause=AsyncMock(), resume=AsyncMock())
    admin = Aria2Administration(execution.executor, repository, application, _runtime())
    await admin.control(execution.handle.native["gid"], "pause")
    await admin.control(execution.handle.native["gid"], "resume")
    application.pause.assert_awaited_once_with(47)
    application.resume.assert_awaited_once_with(47)
    with pytest.raises(PermissionError):
        await admin.control("foreign", "pause")
    assert not execution.daemon.calls


@pytest.mark.asyncio
async def test_owned_gid_with_foreign_path_is_excluded_from_native_admin(execution):
    attempt = SimpleNamespace(handle=execution.handle)
    repository = SimpleNamespace(executions=AsyncMock(return_value=(attempt,)), authorize_execution=execution.executor.authorize)
    admin = Aria2Administration(execution.executor, repository, None, _runtime())
    forged = Aria2DownloadStatus(execution.handle.native["gid"], "active", 4, 1, 1, files=[{"path": "/foreign/file"}])
    assert await admin.filter_owned([forged]) == []


class _FakeGlobalOptionsClient:
    """Minimal RPC-client double exposing only ``change_global_options`` --
    ``Aria2Administration.apply_memory_tuning()`` needs nothing else from
    ``self.executor.client``."""

    def __init__(self):
        self.calls = []

    async def change_global_options(self, options):
        self.calls.append(options)
        return "OK"


@pytest.mark.asyncio
async def test_apply_memory_tuning_never_mirrors_global_policy_into_native_options():
    """Universal Executor Leveling: DebridPulse global concurrency and global
    bandwidth have one core owner each. aria2 tuning reapplies only aria2-owned
    options: its native queue width is an executor-local constant (never the
    DP global concurrency), and its download ceiling is changed only through
    the core-assigned ceiling operation, never from stale configuration."""
    client = _FakeGlobalOptionsClient()
    admin = Aria2Administration(SimpleNamespace(client=client), None, None, _runtime())
    await admin.apply_memory_tuning()
    applied = client.calls[-1]
    assert applied["max-concurrent-downloads"] == str(NATIVE_ACTIVE_DOWNLOADS)
    assert "max-overall-download-limit" not in applied


@pytest.mark.asyncio
async def test_waiting_count_prevents_restart_even_when_snapshot_window_is_empty(execution, monkeypatch):
    """Specification section 9.3: ``Aria2Administration`` consumes the
    injected aria2 runtime directly -- it never calls
    ``core.config.get_settings()`` itself, so this test injects tuning
    through the constructor instead of monkeypatching settings lookups."""
    import executors.aria2.admin as module

    runtime = SimpleNamespace(options=Aria2Options(purge_interval_minutes=0, restart_interval_hours=1),
                              _started_at=1, restart=AsyncMock(), ensure_log_rotation=AsyncMock())
    monkeypatch.setattr(module.time, "time", lambda: 5000)
    execution.daemon.get_global_stat = AsyncMock(return_value={"active": 0, "waiting": 1})
    execution.daemon.get_all = AsyncMock(return_value=[])
    admin = Aria2Administration(execution.executor, None, None, runtime)
    await admin.maintain()
    runtime.restart.assert_not_awaited()
    execution.daemon.get_all.assert_not_awaited()
