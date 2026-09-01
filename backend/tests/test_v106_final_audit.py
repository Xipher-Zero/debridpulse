import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.serializers import public_payload, public_torrent
from api.routes import _sql_date, _sql_strftime
from core.logging_utils import sanitize_exception
from db.database import DatabaseMaintenanceGate
from executors.aria2.client import Aria2RPCError, Aria2Service
from services.aria2_runtime import BuiltinAria2Runtime
from services.maintenance_gate import ApplicationMaintenanceGate
from services.manager_v2 import _safe_persisted_error
from services.provider_gateway import ProviderGateway


@pytest.mark.asyncio
async def test_application_maintenance_acquisition_cancellation_reopens_gate():
    gate = ApplicationMaintenanceGate()
    hold = asyncio.Event()
    entered = asyncio.Event()

    async def active():
        async with gate.operation():
            entered.set()
            await hold.wait()

    active_task = asyncio.create_task(active())
    await entered.wait()

    async def maintenance():
        async with gate.maintenance():
            raise AssertionError("must not enter while active work is held")

    task = asyncio.create_task(maintenance())
    while not gate.active:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gate.active is False
    assert gate._owner is None

    hold.set()
    await active_task
    async with gate.operation():
        pass


@pytest.mark.asyncio
async def test_database_maintenance_acquisition_cancellation_reopens_gate():
    gate = DatabaseMaintenanceGate()
    hold = asyncio.Event()
    entered = asyncio.Event()

    async def active():
        async with gate.session():
            entered.set()
            await hold.wait()

    active_task = asyncio.create_task(active())
    await entered.wait()

    async def maintenance():
        async with gate.maintenance():
            raise AssertionError("must not enter while active DB session is held")

    task = asyncio.create_task(maintenance())
    while not gate.active:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gate.active is False
    assert gate._owner is None

    hold.set()
    await active_task
    async with gate.session():
        pass


@pytest.mark.asyncio
async def test_provider_quiescence_cancellation_reopens_admission():
    gateway = ProviderGateway(SimpleNamespace())
    hold = asyncio.Event()
    entered = asyncio.Event()

    async def active():
        async with gateway._operation():
            entered.set()
            await hold.wait()

    active_task = asyncio.create_task(active())
    await entered.wait()
    task = asyncio.create_task(gateway.begin_quiescence())
    while not gateway.quiescing:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gateway.quiescing is False

    hold.set()
    await active_task
    async with gateway._operation():
        pass


@pytest.mark.asyncio
async def test_aria2_failure_never_exposes_capability_and_uri_lock_is_released(monkeypatch, caplog):
    import executors.aria2.client as aria2_module

    capability = "https://locked.example.invalid/cap"
    service = Aria2Service("http://127.0.0.1:6800/jsonrpc")
    monkeypatch.setattr(aria2_module, "_is_builtin_mode", lambda: False)

    async def fail(*args, **kwargs):
        raise RuntimeError(f"backend rejected {capability}")

    monkeypatch.setattr(service, "_call", fail)
    with pytest.raises(Aria2RPCError) as raised:
        await service.ensure_download(capability, max_retries=1)

    assert capability not in str(raised.value)
    assert capability not in caplog.text
    assert service._uri_locks == {}


def test_persisted_error_boundary_redacts_capability():
    capability = "https://locked.example.invalid/download/" + ("secret-" * 30)
    error = RuntimeError(f"dispatch failed {capability}")
    safe = _safe_persisted_error(error)
    assert capability not in safe
    assert safe == sanitize_exception(error, max_length=300)


class _FakeProcess:
    def __init__(self):
        self.returncode = None
        self.stdout = None
        self.stderr = None
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.mark.asyncio
async def test_failed_builtin_aria2_start_is_transactional(monkeypatch):
    import services.aria2_runtime as runtime_module

    runtime = BuiltinAria2Runtime()
    process = _FakeProcess()

    monkeypatch.setattr(runtime_module, "is_builtin_mode", lambda cfg=None: True)
    monkeypatch.setattr(runtime_module.shutil, "which", lambda name: "/usr/bin/aria2c")
    monkeypatch.setattr(runtime, "_rotate_log_file", lambda: False)
    monkeypatch.setattr(runtime, "_command", lambda: ["aria2c"])
    monkeypatch.setattr(
        runtime_module,
        "get_settings",
        lambda: SimpleNamespace(aria2_mode="builtin", aria2_builtin_auto_start=True),
    )

    async def spawn(*args, **kwargs):
        return process

    async def unhealthy():
        raise RuntimeError("RPC never became healthy")

    async def status():
        return {
            "running": runtime._is_process_alive(),
            "process_running": runtime._is_process_alive(),
            "last_error": runtime._last_error,
        }

    monkeypatch.setattr(runtime_module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(runtime, "_wait_until_healthy", unhealthy)
    monkeypatch.setattr(runtime, "status", status)

    result = await runtime.start()
    assert result["running"] is False
    assert process.terminated or process.killed
    assert runtime._process is None
    assert runtime._stdout_task is None
    assert runtime._stderr_task is None
    assert runtime._started_at == 0.0
    assert "RPC never became healthy" in runtime._last_error


def test_api_timestamps_are_explicit_utc_and_sql_buckets_are_local():
    row = public_torrent({"id": 1, "created_at": "2026-08-21 02:43:00"})
    assert row["created_at"] == "2026-08-21T02:43:00Z"
    nested = public_payload({"event": {"created_at": "2026-08-21 02:43:00"}})
    assert nested["event"]["created_at"] == "2026-08-21T02:43:00Z"
    assert _sql_date("created_at") == "DATE(created_at, 'localtime')"
    assert _sql_strftime("%H", "created_at") == "strftime('%H', created_at, 'localtime')"


def test_frontend_uses_configured_timezone_for_legacy_and_explicit_utc():
    source = (Path(__file__).parents[2] / "frontend" / "static" / "app.js").read_text()
    assert "function parseApiDate" in source
    assert "value.trim().replace(' ', 'T') + 'Z'" in source
    assert "settingsData.timezone" in source
    assert "dateOptions.timeZone = timeZone" in source
    assert "timeOptions.timeZone = timeZone" in source


def test_backup_ids_are_unique_and_backward_compatible():
    from services import backup, db_maintenance

    generated = {
        f"20260820_200000_{__import__('uuid').uuid4().hex}"
        for _ in range(40)
    }
    assert len(generated) == 40
    assert all(backup._BACKUP_DIR_RE.fullmatch(value) for value in generated)
    assert all(db_maintenance._BACKUP_DIR_RE.fullmatch(value) for value in generated)
    assert backup._BACKUP_DIR_RE.fullmatch("20260820_200000")
    assert db_maintenance._BACKUP_DIR_RE.fullmatch("20260820_200000")
    assert hasattr(backup, "_BACKUP_RUN_LOCK")
    assert hasattr(db_maintenance, "_BACKUP_RUN_LOCK")


def test_v1_metadata_does_not_claim_multiple_providers():
    root = Path(__file__).parents[2]
    inspected = [
        (root / "NOTICE").read_text(),
        (root / "backend" / "core" / "branding.py").read_text(),
        (root / ".github" / "workflows" / "fork-image.yml").read_text(),
    ]
    joined = "\n".join(inspected)
    assert "Multi-provider Debrid Download Manager" not in joined
    assert "Multi-provider debrid download manager" not in joined


def test_disk_guard_comment_matches_runtime_contract():
    root = Path(__file__).parents[2]
    source = (root / "backend" / "core" / "config.py").read_text()
    assert "Transfers already active in aria2 are allowed to finish" in source
    assert "Active aria2 downloads are PAUSED automatically" not in source


def test_scheduler_stop_intent_is_marked_before_interruptible_await():
    root = Path(__file__).parents[2]
    source = (root / "backend" / "api" / "routes.py").read_text()
    initial = source.index("scheduler_stopped = False")
    intent = source.index("scheduler_stopped = True", initial)
    stop = source.index("await scheduler_runtime.stop_scheduler()", intent)
    assert initial < intent < stop
