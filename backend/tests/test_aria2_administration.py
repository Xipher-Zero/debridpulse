"""Native administration preserves canonical intent and shared-daemon isolation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_aria2_executor_contract import execution
from executors.aria2.admin import Aria2Administration
from executors.aria2.client import Aria2DownloadStatus


@pytest.mark.asyncio
async def test_low_level_pause_and_resume_use_core_transfer_identity(execution):
    attempt = SimpleNamespace(handle=execution.handle, transfer_id=47, artifact_id=9)
    repository = SimpleNamespace(executions=AsyncMock(return_value=(attempt,)), authorize_execution=execution.executor.authorize)
    application = SimpleNamespace(pause=AsyncMock(), resume=AsyncMock())
    admin = Aria2Administration(execution.executor, repository, application)
    await admin.control(execution.handle.context["gid"], "pause")
    await admin.control(execution.handle.context["gid"], "resume")
    application.pause.assert_awaited_once_with(47)
    application.resume.assert_awaited_once_with(47)
    with pytest.raises(PermissionError):
        await admin.control("foreign", "pause")
    assert not execution.daemon.calls


@pytest.mark.asyncio
async def test_owned_gid_with_foreign_path_is_excluded_from_native_admin(execution):
    attempt = SimpleNamespace(handle=execution.handle)
    repository = SimpleNamespace(executions=AsyncMock(return_value=(attempt,)), authorize_execution=execution.executor.authorize)
    admin = Aria2Administration(execution.executor, repository, None)
    forged = Aria2DownloadStatus(execution.handle.context["gid"], "active", 4, 1, 1, files=[{"path": "/foreign/file"}])
    assert await admin.filter_owned([forged]) == []


@pytest.mark.asyncio
async def test_external_daemon_global_mutations_remain_blocked(execution, monkeypatch):
    monkeypatch.setattr("executors.aria2.admin.is_builtin_mode", lambda *_: False)
    admin = Aria2Administration(execution.executor, None, None)
    with pytest.raises(PermissionError):
        await admin.change_global_options({"max-concurrent-downloads": "9"})
    assert not execution.daemon.calls


@pytest.mark.asyncio
async def test_waiting_count_prevents_restart_even_when_snapshot_window_is_empty(execution, monkeypatch):
    import executors.aria2.admin as module
    cfg = SimpleNamespace(aria2_purge_interval_minutes=0, aria2_restart_interval_hours=1)
    runtime = SimpleNamespace(_started_at=1, restart=AsyncMock(), ensure_log_rotation=AsyncMock())
    monkeypatch.setattr(module, "get_settings", lambda: cfg)
    monkeypatch.setattr(module, "is_builtin_mode", lambda *_: True)
    monkeypatch.setattr(module, "runtime", runtime)
    monkeypatch.setattr(module.time, "time", lambda: 5000)
    execution.daemon.get_global_stat = AsyncMock(return_value={"active": 0, "waiting": 1})
    execution.daemon.get_all = AsyncMock(return_value=[])
    admin = Aria2Administration(execution.executor, None, None)
    await admin.maintain()
    runtime.restart.assert_not_awaited()
    execution.daemon.get_all.assert_not_awaited()
