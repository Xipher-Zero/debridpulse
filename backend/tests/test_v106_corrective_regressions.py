import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_database_maintenance_gate_drains_existing_and_rejects_new_sessions():
    from db.database import DatabaseMaintenanceActive, DatabaseMaintenanceGate

    gate = DatabaseMaintenanceGate()
    reader_started = asyncio.Event()
    release_reader = asyncio.Event()
    maintenance_entered = asyncio.Event()
    release_maintenance = asyncio.Event()

    async def reader():
        async with gate.session():
            reader_started.set()
            await release_reader.wait()

    async def maintainer():
        async with gate.maintenance():
            maintenance_entered.set()
            await release_maintenance.wait()

    reader_task = asyncio.create_task(reader())
    await reader_started.wait()
    maintenance_task = asyncio.create_task(maintainer())
    await asyncio.sleep(0)
    assert not maintenance_entered.is_set()

    release_reader.set()
    await reader_task
    await maintenance_entered.wait()

    async def blocked_session():
        async with gate.session():
            return True

    with pytest.raises(DatabaseMaintenanceActive, match="maintenance"):
        await blocked_session()

    release_maintenance.set()
    await maintenance_task

    async with gate.session():
        pass


@pytest.mark.asyncio
async def test_database_wipe_suspends_scheduler_and_holds_exclusive_gate(monkeypatch):
    import api.routes as routes
    import services.db_maintenance as db_maintenance

    calls = []
    monkeypatch.setattr(
        routes,
        "get_settings",
        lambda: SimpleNamespace(
            db_wipe_enabled=True,
            paused=True,
            db_backup_before_wipe=False,
        ),
    )
    monkeypatch.setattr(routes.scheduler_runtime, "scheduler_running", lambda: True)

    async def stop_scheduler():
        calls.append("scheduler-stop")

    async def start_scheduler(application):
        calls.append("scheduler-start")

    async def quiesce():
        calls.append("transfer-quiesce")
        return {"ok": True}

    async def release():
        calls.append("transfer-release")

    @asynccontextmanager
    async def maintenance():
        calls.append("db-gate-enter")
        try:
            yield
        finally:
            calls.append("db-gate-exit")

    async def wipe_database(*, verified_quiesced=False):
        assert verified_quiesced is True
        calls.append("wipe")
        return {"ok": True, "wiped_tables": []}

    monkeypatch.setattr(routes.scheduler_runtime, "stop_scheduler", stop_scheduler)
    monkeypatch.setattr(routes.scheduler_runtime, "start_scheduler", start_scheduler)
    from services.maintenance_gate import ApplicationMaintenanceGate
    application = SimpleNamespace(database_wipe_admission=ApplicationMaintenanceGate().maintenance, quiesce_for_database_wipe=quiesce, release_database_wipe_quiescence=release)
    monkeypatch.setattr(routes, "database_maintenance", maintenance)
    monkeypatch.setattr(db_maintenance, "wipe_database", wipe_database)

    result = await routes.wipe_database_admin({"confirm": True}, application=application)
    assert result["ok"] is True
    assert calls == [
        "scheduler-stop",
        "transfer-quiesce",
        "db-gate-enter",
        "wipe",
        "db-gate-exit",
        "transfer-release",
        "scheduler-start",
    ]


def test_database_wipe_route_releases_quiescence_in_finally():
    routes = (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text()
    block = routes.split('async def wipe_database_admin', 1)[1].split('# ── Statistics & Reporting', 1)[0]
    assert "scheduler_runtime.stop_scheduler" in block
    assert "database_maintenance()" in block
    assert "quiesce_for_database_wipe" in block
    assert "finally:" in block
    assert "release_database_wipe_quiescence" in block
    assert "scheduler_runtime.start_scheduler" in block


def test_settings_secret_merge_preserve_replace_clear():
    from api.routes import SettingsUpdate, _merge_secret_settings
    from core.config import AppSettings

    previous = AppSettings(alldebrid_api_key="old-key", auth_username="old-user", auth_password="old-pass")

    payload = previous.model_dump()
    payload.update(alldebrid_api_key="", auth_password="")
    preserve = SettingsUpdate(**payload)
    merged = _merge_secret_settings(preserve, previous)
    assert merged["alldebrid_api_key"] == "old-key"
    assert merged["auth_password"] == "old-pass"

    payload = previous.model_dump()
    payload.update(auth_username="new-user", auth_password="new-pass")
    replace = SettingsUpdate(**payload)
    merged = _merge_secret_settings(replace, previous)
    assert merged["auth_username"] == "new-user"
    assert merged["auth_password"] == "new-pass"

    payload = previous.model_dump()
    payload.update(auth_password="", clear_secrets=["auth_password"])
    clear = SettingsUpdate(**payload)
    merged = _merge_secret_settings(clear, previous)
    assert merged["auth_password"] == ""

    payload = previous.model_dump()
    payload.update(clear_secrets=["not_a_secret"])
    with pytest.raises(Exception):
        _merge_secret_settings(SettingsUpdate(**payload), previous)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_frontend_escape_helpers_execute_against_malicious_payload():
    root = Path(__file__).resolve().parents[2]
    script = r"""
const fs = require('fs');
const source = fs.readFileSync('frontend/static/app.js', 'utf8');
const start = source.indexOf('function esc(s)');
const end = source.indexOf('function sanitizeErrorMsg', start);
if (start < 0 || end < 0) throw new Error('escape helper block not found');
eval(source.slice(start, end));
const payload = '"><img src=x onerror=globalThis.__xss=1>';
const escaped = esc(payload);
if (escaped.includes('<img') || escaped.includes('">')) {
  throw new Error('esc() left executable markup');
}
const settings = escapeHtmlStrings({
  auth_username: payload,
  nested: {path: payload},
  list: [payload],
  enabled: true,
  count: 7,
});
for (const value of [settings.auth_username, settings.nested.path, settings.list[0]]) {
  if (value.includes('<img') || value.includes('">')) {
    throw new Error('escapeHtmlStrings() left executable markup');
  }
}
if (settings.enabled !== true || settings.count !== 7) {
  throw new Error('escapeHtmlStrings() changed non-string types');
}
if (sourceLabel(payload).includes('<img')) {
  throw new Error('sourceLabel() returned raw unknown source');
}
"""
    subprocess.run(["node", "-e", script], cwd=root, check=True)


def test_bulk_actions_are_schema_limited():
    from api.routes import BulkAction
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BulkAction(ids=[1], action="nonsense")


def test_codeql_covers_browser_javascript():
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/codeql.yml").read_text()
    assert "javascript-typescript" in workflow


def test_dependency_docs_match_removed_runtime_components():
    root = Path(__file__).resolve().parents[2]
    docs = (root / "docs/DEPENDENCY_LICENSES.md").read_text()
    requirements = (root / "backend/requirements.txt").read_text().lower()
    dockerfile = (root / "Dockerfile").read_text().lower()
    assert "asyncpg" not in requirements
    assert "| asyncpg |" not in docs
    assert "unrar-free" not in dockerfile
    assert "| unrar-free |" not in docs
    assert "Chart.js | 4.5.1, vendored" in docs
    assert (root / "licenses/Chart.js-MIT.txt").is_file()
    assert (root / "frontend/static/vendor/chart.umd.min.js").is_file()


def test_sqlite_default_source_is_not_legacy_watch_authority():
    database = (Path(__file__).resolve().parents[1] / "db" / "database.py").read_text()
    assert "source TEXT DEFAULT 'watch'" not in database
    assert "source TEXT DEFAULT ''" in database


@pytest.mark.asyncio
async def test_application_maintenance_gate_drains_admitted_work_and_rejects_new_work():
    from services.maintenance_gate import ApplicationMaintenanceActive, ApplicationMaintenanceGate

    gate = ApplicationMaintenanceGate()
    started = asyncio.Event()
    release = asyncio.Event()
    entered = asyncio.Event()
    release_maintenance = asyncio.Event()

    async def admitted_operation():
        async with gate.operation():
            started.set()
            # Reentrant work in the already-admitted task must be allowed to finish.
            async with gate.operation():
                await release.wait()

    async def maintainer():
        async with gate.maintenance():
            entered.set()
            await release_maintenance.wait()

    operation_task = asyncio.create_task(admitted_operation())
    await started.wait()
    maintenance_task = asyncio.create_task(maintainer())
    await asyncio.sleep(0)
    assert not entered.is_set()

    release.set()
    await operation_task
    await entered.wait()

    with pytest.raises(ApplicationMaintenanceActive, match="maintenance"):
        async with gate.operation():
            pass

    release_maintenance.set()
    await maintenance_task


@pytest.mark.asyncio
async def test_database_wipe_rechecks_pause_after_application_admission_drain(monkeypatch):
    import api.routes as routes

    calls = []
    state = SimpleNamespace(paused=True)
    monkeypatch.setattr(
        routes,
        "get_settings",
        lambda: SimpleNamespace(
            db_wipe_enabled=True,
            paused=state.paused,
            db_backup_before_wipe=False,
        ),
    )
    monkeypatch.setattr(routes.scheduler_runtime, "scheduler_running", lambda: True)

    @asynccontextmanager
    async def application_gate():
        calls.append("app-gate-enter")
        # Simulate a Resume that was admitted just before maintenance closed.
        state.paused = False
        try:
            yield
        finally:
            calls.append("app-gate-exit")

    application = SimpleNamespace(database_wipe_admission=application_gate)
    monkeypatch.setattr(routes.scheduler_runtime, "stop_scheduler", AsyncMock())
    monkeypatch.setattr(routes.scheduler_runtime, "start_scheduler", AsyncMock())

    with pytest.raises(Exception) as exc:
        await routes.wipe_database_admin({"confirm": True}, application=application)
    assert getattr(exc.value, "status_code", None) == 409
    routes.scheduler_runtime.stop_scheduler.assert_not_awaited()
    routes.scheduler_runtime.start_scheduler.assert_not_awaited()
    assert calls == ["app-gate-enter", "app-gate-exit"]


def test_mutating_http_requests_share_application_maintenance_admission():
    main = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    block = main.split("async def application_mutation_admission_middleware", 1)[1].split("@app.exception_handler", 1)[0]
    assert '_MUTATING_HTTP_METHODS = {"POST", "PUT", "PATCH", "DELETE"}' in main
    assert '_DATABASE_WIPE_PATH = "/api/admin/database/wipe"' in main
    assert "request.url.path != _DATABASE_WIPE_PATH" in block
    assert "get_application(request).application_operation()" in block
    assert "ApplicationMaintenanceActive" in block
    assert 'status_code=503' in block


@pytest.mark.asyncio
async def test_database_wipe_refreshes_disabled_setting_after_admission_drain(monkeypatch):
    import api.routes as routes

    state = SimpleNamespace(enabled=True, paused=True)
    monkeypatch.setattr(
        routes,
        "get_settings",
        lambda: SimpleNamespace(
            db_wipe_enabled=state.enabled,
            paused=state.paused,
            db_backup_before_wipe=False,
        ),
    )
    monkeypatch.setattr(routes.scheduler_runtime, "scheduler_running", lambda: True)

    @asynccontextmanager
    async def application_gate():
        state.enabled = False
        yield

    application = SimpleNamespace(database_wipe_admission=application_gate)
    monkeypatch.setattr(routes.scheduler_runtime, "stop_scheduler", AsyncMock())
    monkeypatch.setattr(routes.scheduler_runtime, "start_scheduler", AsyncMock())

    with pytest.raises(Exception) as exc:
        await routes.wipe_database_admin({"confirm": True}, application=application)
    assert getattr(exc.value, "status_code", None) == 400
    routes.scheduler_runtime.stop_scheduler.assert_not_awaited()
    routes.scheduler_runtime.start_scheduler.assert_not_awaited()


