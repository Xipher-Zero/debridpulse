"""Native administration preserves canonical intent and shared-daemon isolation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_aria2_executor_contract import execution
from executors.aria2.admin import Aria2Administration
from executors.aria2.client import Aria2DownloadStatus
from executors.aria2.definition import Aria2Options
from executors.aria2.runtime import Aria2RuntimeConfiguration


@pytest.mark.asyncio
async def test_low_level_pause_and_resume_use_core_transfer_identity(execution):
    attempt = SimpleNamespace(handle=execution.handle, transfer_id=47, artifact_id=9)
    repository = SimpleNamespace(executions=AsyncMock(return_value=(attempt,)), authorize_execution=execution.executor.authorize)
    application = SimpleNamespace(pause=AsyncMock(), resume=AsyncMock())
    admin = Aria2Administration(execution.executor, repository, application, Aria2RuntimeConfiguration())
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
    admin = Aria2Administration(execution.executor, repository, None, Aria2RuntimeConfiguration())
    forged = Aria2DownloadStatus(execution.handle.context["gid"], "active", 4, 1, 1, files=[{"path": "/foreign/file"}])
    assert await admin.filter_owned([forged]) == []


@pytest.mark.asyncio
async def test_external_daemon_global_mutations_remain_blocked(execution):
    """Specification section 9.3: mode is read from the injected
    ``Aria2RuntimeConfiguration``, never a global-settings-backed helper --
    no monkeypatch of any ``core.config.get_settings()``-derived function is
    needed to exercise this."""
    config = Aria2RuntimeConfiguration(options=Aria2Options(mode="external"))
    admin = Aria2Administration(execution.executor, None, None, config)
    with pytest.raises(PermissionError):
        await admin.change_global_options({"max-concurrent-downloads": "9"})
    assert not execution.daemon.calls


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
async def test_apply_memory_tuning_projects_universal_concurrency_into_builtin_native_option():
    """Gate 9 revision-5 rejection finding 4: ``PATCH /transfer-policy`` now
    calls this executor-owned administration entry point after persisting a
    concurrency change so the running built-in daemon's native
    ``max-concurrent-downloads`` tracks the just-persisted universal value,
    rather than staying pinned at whatever cap it was started with
    (specification sections 4.1, 9.7 -- the native option is a derived
    implementation projection of the one universal authority, never a
    second persisted policy)."""
    client = _FakeGlobalOptionsClient()
    config = Aria2RuntimeConfiguration(
        options=Aria2Options(mode="builtin"), max_concurrent_executions=10,
    )
    admin = Aria2Administration(SimpleNamespace(client=client), None, None, config)
    await admin.apply_memory_tuning()
    assert client.calls[-1]["max-concurrent-downloads"] == "10"


@pytest.mark.asyncio
async def test_apply_memory_tuning_is_a_no_op_for_external_shared_daemon():
    """External mode: universal concurrency governs only DebridPulse's own
    scheduler; a shared external daemon's global option must never be
    mutated by a canonical transfer-policy change (specification section
    9.7)."""
    client = _FakeGlobalOptionsClient()
    config = Aria2RuntimeConfiguration(
        options=Aria2Options(mode="external"), max_concurrent_executions=10,
    )
    admin = Aria2Administration(SimpleNamespace(client=client), None, None, config)
    result = await admin.apply_memory_tuning()
    assert result == {"ok": True, "skipped": True, "reason": "External daemon policy is read-only"}
    assert not client.calls


@pytest.mark.asyncio
async def test_waiting_count_prevents_restart_even_when_snapshot_window_is_empty(execution, monkeypatch):
    """Specification section 9.3: ``Aria2Administration`` consumes its
    injected ``Aria2RuntimeConfiguration`` directly -- it never calls
    ``core.config.get_settings()`` itself, so this test injects tuning
    through the constructor instead of monkeypatching settings lookups."""
    import executors.aria2.admin as module

    config = Aria2RuntimeConfiguration(options=Aria2Options(
        mode="builtin", purge_interval_minutes=0, restart_interval_hours=1,
    ))
    runtime = SimpleNamespace(_started_at=1, restart=AsyncMock(), ensure_log_rotation=AsyncMock())
    monkeypatch.setattr(module, "runtime", runtime)
    monkeypatch.setattr(module.time, "time", lambda: 5000)
    execution.daemon.get_global_stat = AsyncMock(return_value={"active": 0, "waiting": 1})
    execution.daemon.get_all = AsyncMock(return_value=[])
    admin = Aria2Administration(execution.executor, None, None, config)
    await admin.maintain()
    runtime.restart.assert_not_awaited()
    execution.daemon.get_all.assert_not_awaited()
