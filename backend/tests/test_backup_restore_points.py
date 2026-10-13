"""One DebridPulse backup = one restore point, owned by services.backup.

Covers the restore-point abstraction (list, create, add, save, remove), the
validate-before-admission contract, the exact-match schema compatibility
rule, retention independence, and the restore transaction: pre-restore safety
backup, stage-then-validate-then-swap, the native-execution drain boundary,
rollback, and post-restore authority through the normal startup sequence.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import time
import uuid
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import core.config as config
import core.scheduler as scheduler
import db.database as database
from api.routes import router
from application.service import ApplicationService
from fake_integrations import MemoryExecutor, ParcelProvider
from services import backup
from transfers.applicability import ProviderApplicability
from transfers.convergence_engine import TransferEngine
from transfers.models import ExecutionState, TransferState
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

@pytest_asyncio.fixture
async def installation(tmp_path, monkeypatch):
    """A real DB + config + backup folder; nothing in the backup path is faked."""
    data = tmp_path / "data"
    conf = tmp_path / "config"
    data.mkdir()
    conf.mkdir()
    monkeypatch.setattr(database, "DB_PATH", data / "debridpulse.db")
    monkeypatch.setattr(config, "CONFIG_PATH", conf / "config.json")
    # The run lock binds to the first event loop that waits on it; each test
    # runs its own loop, so each gets its own (one) run lock.
    monkeypatch.setattr(backup, "_BACKUP_RUN_LOCK", asyncio.Lock())
    settings = config.AppSettings(backup_folder=str(tmp_path / "backups"), backup_enabled=True, backup_keep_days=7)
    previous = config.get_settings()
    config.apply_settings(settings)
    config.save_settings(settings)
    from db.migrations.v112 import migrate
    await migrate(globally_paused=False)  # a fresh installation, exactly as startup creates it
    yield tmp_path
    config.apply_settings(previous)


def _folder(root: Path) -> Path:
    return root / "backups"


def _visible(root: Path) -> list[str]:
    folder = _folder(root)
    return sorted(entry.name for entry in folder.iterdir()) if folder.exists() else []


def _repack(package: Path, target: Path, *, drop=(), replace_members=None, extra=None) -> Path:
    """Rewrite a real package with deliberate damage."""
    replace_members = replace_members or {}
    with zipfile.ZipFile(package) as source, zipfile.ZipFile(target, "w") as out:
        for info in source.infolist():
            if info.filename in drop:
                continue
            data = replace_members.get(info.filename, source.read(info.filename))
            out.writestr(info.filename, data)
        for name, data in (extra or {}).items():
            out.writestr(name, data)
    return target


async def _chunks(path: Path, size: int = 64 * 1024):
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(size)
            if not chunk:
                return
            yield chunk


async def _saved_copy(point_id: str, destination: Path) -> Path:
    package = backup.package_restore_point(point_id)
    try:
        destination.write_bytes(package.read_bytes())
    finally:
        package.unlink(missing_ok=True)
    return destination


# --------------------------------------------------------------------------- #
# restore-point abstraction
# --------------------------------------------------------------------------- #

async def test_run_backup_is_listed_as_one_restore_point(installation):
    result = await backup.run_backup()
    assert not result["errors"]
    points = backup.list_restore_points()
    assert len(points) == 1
    public = points[0].public()
    assert set(public) == {"id", "created_at", "size_bytes", "contents"}
    assert public["id"] == result["restore_point"]["id"]
    assert public["contents"] == "DP State"
    assert public["size_bytes"] > 0
    # The operator-facing unit never exposes implementation files.
    assert "debridpulse.db" not in json.dumps(public) and "config.json" not in json.dumps(public)


async def test_restore_point_manifest_records_the_exact_schema_version(installation):
    point = await backup.create_restore_point()
    manifest = json.loads((point.path / ".debridpulse-backup.json").read_text())
    assert manifest["schema_version"] == backup.schema_version(database.DB_PATH)
    assert manifest["database"] == "debridpulse.db"
    assert manifest["timestamp"] == point.id


async def test_create_refuses_to_leave_an_incomplete_restore_point(installation, monkeypatch):
    def broken_copy(_source, _destination):
        raise OSError("disk went away")

    monkeypatch.setattr(backup, "_sqlite_backup", broken_copy)
    with pytest.raises(Exception):
        await backup.create_restore_point()
    assert _visible(installation) == []


async def test_looking_inside_a_backup_does_not_break_it(installation):
    """The database member is self-contained: an ordinary read of it (an
    operator inspecting a backup with sqlite3) leaves no sidecar behind that
    would turn the restore point into an unrecognisable one."""
    point = await backup.create_restore_point()
    conn = sqlite3.connect(point.path / "debridpulse.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        conn.execute("SELECT COUNT(*) FROM torrents").fetchone()
    finally:
        conn.close()
    assert sorted(entry.name for entry in point.path.iterdir()) == [
        ".debridpulse-backup.json", "config.json", "debridpulse.db"]
    await backup.validate_restore_point(point.id)


# --------------------------------------------------------------------------- #
# Add Backup: validate completely, then admit
# --------------------------------------------------------------------------- #

async def _real_package(installation, name="saved.zip") -> tuple[str, Path]:
    point = await backup.create_restore_point()
    saved = await _saved_copy(point.id, installation / name)
    backup.remove_restore_point(point.id)
    return point.id, saved


async def test_valid_package_is_admitted_with_its_own_identity(installation):
    point_id, saved = await _real_package(installation)
    assert backup.list_restore_points() == []
    admitted = await backup.add_backup(_chunks(saved))
    assert admitted.id == point_id
    assert [point.id for point in backup.list_restore_points()] == [point_id]


def _legacy(manifest: dict) -> dict:
    """The manifest an earlier v1.0.13 wrote: no ``schema_version``/``database``."""
    return {key: value for key, value in manifest.items() if key not in {"schema_version", "database"}}


def _schema_drifted_database(package: Path, member: str) -> tuple[bytes, str]:
    """Another version's database: a different schema, honestly recorded."""
    with zipfile.ZipFile(package) as source:
        raw = source.read(member)
    path = package.with_suffix(".drift.db")
    path.write_bytes(raw)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE torrents ADD COLUMN drift_column TEXT")
    conn.commit()
    conn.close()
    return path.read_bytes(), backup.schema_version(path)


@pytest.mark.parametrize("damage", [
    "not_a_zip", "traversal_member", "missing_database", "missing_config", "unknown_member",
    "legacy_manifest_other_schema", "schema_mismatch", "manifest_disagrees_with_database", "corrupt_database", "manifest_errors", "unparseable_config",
    "crc_corruption",
])
async def test_invalid_candidate_is_rejected_before_admission(installation, damage):
    _point_id, saved = await _real_package(installation)
    bad = installation / f"{damage}.zip"
    manifest = json.loads(zipfile.ZipFile(saved).read(".debridpulse-backup.json"))
    if damage == "not_a_zip":
        bad.write_bytes(b"this is not a DebridPulse backup")
    elif damage == "traversal_member":
        _repack(saved, bad, extra={"../escape.txt": b"x"})
    elif damage == "missing_database":
        _repack(saved, bad, drop={"debridpulse.db"})
    elif damage == "missing_config":
        _repack(saved, bad, drop={"config.json"})
    elif damage == "unknown_member":
        _repack(saved, bad, extra={"notes.txt": b"x"})
    elif damage == "legacy_manifest_other_schema":
        # A pre-change manifest records no schema: the packaged database decides.
        drifted, _recorded = _schema_drifted_database(saved, "debridpulse.db")
        _repack(saved, bad, replace_members={"debridpulse.db": drifted,
                                             ".debridpulse-backup.json": json.dumps(_legacy(manifest)).encode()})
    elif damage == "schema_mismatch":
        drifted, recorded = _schema_drifted_database(saved, "debridpulse.db")
        _repack(saved, bad, replace_members={"debridpulse.db": drifted, ".debridpulse-backup.json": json.dumps(
            {**manifest, "schema_version": recorded}).encode()})
    elif damage == "manifest_disagrees_with_database":
        drifted, _recorded = _schema_drifted_database(saved, "debridpulse.db")
        _repack(saved, bad, replace_members={"debridpulse.db": drifted})
    elif damage == "corrupt_database":
        _repack(saved, bad, replace_members={"debridpulse.db": b"SQLite format 3\x00" + b"\x00" * 4096})
    elif damage == "manifest_errors":
        _repack(saved, bad, replace_members={".debridpulse-backup.json": json.dumps(
            {**manifest, "errors": ["database: failed"]}).encode()})
    elif damage == "unparseable_config":
        _repack(saved, bad, replace_members={"config.json": b"{not json"})
    elif damage == "crc_corruption":
        raw = bytearray(saved.read_bytes())
        with zipfile.ZipFile(saved) as source:
            info = source.getinfo("config.json")
        # Flip one byte inside the stored member data, after its local header.
        offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra) + 2
        raw[offset] ^= 0xFF
        bad.write_bytes(bytes(raw))
    before = _visible(installation)

    with pytest.raises(backup.BackupRejected) as rejected:
        await backup.add_backup(_chunks(bad))

    assert rejected.value.message.startswith("Backup could not be added.")
    if damage in {"legacy_manifest_other_schema", "schema_mismatch"}:
        assert "unsupported version" in rejected.value.message
    # Nothing was admitted, and no staging residue is left behind.
    assert _visible(installation) == before
    assert backup.list_restore_points() == []


async def test_duplicate_restore_point_is_not_admitted_twice(installation):
    point = await backup.create_restore_point()
    saved = await _saved_copy(point.id, installation / "dup.zip")
    with pytest.raises(backup.BackupRejected):
        await backup.add_backup(_chunks(saved))
    assert [item.id for item in backup.list_restore_points()] == [point.id]


# --------------------------------------------------------------------------- #
# Remove + retention
# --------------------------------------------------------------------------- #

async def test_remove_deletes_exactly_the_selected_restore_point(installation):
    first = await backup.create_restore_point()
    second = await backup.create_restore_point()
    keep_days = config.get_settings().backup_keep_days
    backup.remove_restore_point(first.id)
    assert [point.id for point in backup.list_restore_points()] == [second.id]
    assert config.get_settings().backup_keep_days == keep_days
    with pytest.raises(backup.BackupRejected):
        backup.remove_restore_point(first.id)


async def test_remove_refuses_anything_that_is_not_a_restore_point(installation):
    folder = _folder(installation)
    folder.mkdir(parents=True, exist_ok=True)
    stranger = folder / ("20260101_000000_" + "0" * 32)
    stranger.mkdir()
    (stranger / "keep.txt").write_text("not ours")
    with pytest.raises(backup.BackupRejected):
        backup.remove_restore_point(stranger.name)
    with pytest.raises(backup.BackupRejected):
        backup.remove_restore_point("../outside")
    assert (stranger / "keep.txt").exists()


async def test_retention_keeps_pruning_independently(installation):
    old = await backup.create_restore_point()
    stale = time.time() - 30 * 86400
    os.utime(old.path, (stale, stale))
    added_id, saved = await _real_package(installation, "kept.zip")
    admitted = await backup.add_backup(_chunks(saved))
    # Add Backup applies retention once it has admitted the copy.
    assert old.id not in {point.id for point in backup.list_restore_points()}
    result = await backup.run_backup()
    remaining = {point.id for point in backup.list_restore_points()}
    assert result["rotated"] == 0
    # Admission time, not the package's age, starts an added backup's retention.
    assert admitted.id == added_id and added_id in remaining


# --------------------------------------------------------------------------- #
# restore transaction (HTTP, real package, real SQLite, fake executor)
# --------------------------------------------------------------------------- #

def _universe(tmp_path, *, executor=None):
    repository = TransferRepository()
    registry = IntegrationRegistry()
    provider = ParcelProvider()
    provider.descriptor = replace(provider.descriptor,
                                  request_types=frozenset({"parcel", "http", "https", "magnet", "torrent"}))
    executor = executor or MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "files"),
                            policy=TransferPolicy(adoption_stability_seconds=0))
    service = ApplicationService(engine, lifecycle=(Lifecycle("a"), Lifecycle("b"), Lifecycle("c")))
    service.test_executor = executor
    return service


class Lifecycle:
    """An integration lifecycle that counts what was asked of it."""

    def __init__(self, name):
        self.name, self.starts, self.stops, self.fail_stop = name, 0, 0, False

    async def start(self):
        self.starts += 1

    async def stop(self):
        self.stops += 1
        if self.fail_stop:
            raise RuntimeError(f"{self.name} would not stop")


def _counts(service):
    return {item.name: (item.stops, item.starts) for item in service.lifecycle}


@pytest_asyncio.fixture
async def served(installation, monkeypatch):
    monkeypatch.setattr(
        ParcelProvider, "applicability",
        property(lambda _provider: ProviderApplicability(generic_schemes=frozenset({"http", "https"}))),
    )
    scheduler_calls = []

    async def start_scheduler(service=None):
        scheduler_calls.append(("start", service))

    async def stop_scheduler():
        scheduler_calls.append(("stop", None))

    monkeypatch.setattr("core.scheduler.start_scheduler", start_scheduler)
    monkeypatch.setattr("core.scheduler.stop_scheduler", stop_scheduler)
    monkeypatch.setattr("core.scheduler.scheduler_running", lambda: True)
    composed = []

    def compose():
        service = _universe(installation)
        composed.append(service)
        return service

    application = _universe(installation)
    await application.engine.initialize()
    app = FastAPI()
    app.state.application = application
    app.state.compose = compose
    app.include_router(router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield app, client, composed, scheduler_calls


async def _submit(app, client, name):
    response = await client.post("/api/links/add", json={"links": [f"https://fake.example/{name}"]})
    assert response.status_code == 200, response.text
    service = app.state.application
    await service.resolve_pending()
    await service.reconcile_executions()
    return response.json()["id"]


async def _ids(app):
    return sorted(item.id for item in await app.state.application.repository.active())


async def test_restore_replaces_state_and_keeps_the_safety_backup(served):
    app, client, composed, _calls = served
    kept = await _submit(app, client, "kept")
    point_b = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    later = await _submit(app, client, "later")
    assert await _ids(app) == sorted([kept, later])
    before_restore = app.state.application

    response = await client.post("/api/admin/backups/restore", json={"id": point_b})
    assert response.status_code == 200, response.text
    report = response.json()

    # B is the sole authority now, served by a freshly composed universe.
    assert app.state.application is composed[-1] and app.state.application is not before_restore
    assert await _ids(app) == [kept]
    # The safety backup of A exists, is listed, and restores A.
    listed = (await client.get("/api/admin/backups")).json()["backups"]
    safety = report["safety_backup"]["id"]
    assert safety in {item["id"] for item in listed} and point_b in {item["id"] for item in listed}
    again = await client.post("/api/admin/backups/restore", json={"id": safety})
    assert again.status_code == 200, again.text
    assert await _ids(app) == sorted([kept, later])


async def test_restore_drains_every_native_execution_before_the_swap(served, monkeypatch):
    app, client, _composed, _calls = served
    transfer_id = await _submit(app, client, "active")
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    old = app.state.application
    attempt = (await old.repository.artifacts(transfer_id))[0].execution
    assert old.test_executor.jobs[attempt.attempt_id].state == ExecutionState.RUNNING
    at_swap = []
    original = backup.StagedRestore.activate

    def observing_activate(staged):
        at_swap.append(len(database_live_rows()))
        return original(staged)

    def database_live_rows():
        with sqlite3.connect(database.DB_PATH) as conn:
            return conn.execute(
                "SELECT e.id FROM execution_attempts e JOIN download_files f ON f.execution_attempt_id=e.id "
                "WHERE e.authorized=1").fetchall()

    monkeypatch.setattr(backup.StagedRestore, "activate", observing_activate)
    response = await client.post("/api/admin/backups/restore", json={"id": point})
    assert response.status_code == 200, response.text
    drain = response.json()["drain"]
    assert drain["live_before"] == 1 and drain["live_after"] == 0
    assert at_swap == [0]
    # The pre-restore native job was stopped by its own executor ...
    assert old.test_executor.jobs[attempt.attempt_id].state == ExecutionState.CANCELLED
    # ... and the safety backup holds the transfer as paused, never cancelled.
    safety = backup.restore_point(response.json()["safety_backup"]["id"])
    with sqlite3.connect(safety.path / "debridpulse.db") as conn:
        status = conn.execute("SELECT status FROM torrents WHERE id=?", (transfer_id,)).fetchone()[0]
    assert status != "cancelled"


async def test_restored_active_history_is_reconciled_by_normal_startup(served):
    app, client, composed, calls = served
    transfer_id = await _submit(app, client, "historical")
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    old = app.state.application
    old_attempt = (await old.repository.artifacts(transfer_id))[0].execution

    response = await client.post("/api/admin/backups/restore", json={"id": point})
    assert response.status_code == 200, response.text

    fresh = app.state.application
    assert fresh is composed[-1]
    # No pre-restore handle crossed: the fresh executor never saw the old job.
    # The restore ran no recovery pass of its own; normal startup
    # reconciliation -- the execution loop's first cycle once integrations
    # have started (the scheduler is stubbed here) -- is what observes it.
    assert old_attempt.attempt_id not in fresh.test_executor.jobs
    assert ("observe", old_attempt) not in fresh.test_executor.calls
    await fresh.integrations_started()
    await fresh.reconcile_executions()
    assert ("observe", old_attempt) in fresh.test_executor.calls
    assert ("start", fresh) in calls
    assert (await fresh.repository.get(transfer_id)).state != TransferState.CANCELLED


async def test_invalid_restore_point_leaves_everything_unchanged(served):
    app, client, _composed, calls = served
    kept = await _submit(app, client, "kept")
    point = backup.restore_point((await client.post("/api/admin/backup")).json()["restore_point"]["id"])
    (point.path / "debridpulse.db").write_bytes(b"corrupted")
    later = await _submit(app, client, "later")
    before = app.state.application
    calls.clear()

    response = await client.post("/api/admin/backups/restore", json={"id": point.id})

    assert response.status_code == 400
    assert response.json()["detail"].startswith("Backup could not be restored.")
    assert "left unchanged" in response.json()["detail"]
    assert app.state.application is before
    assert await _ids(app) == sorted([kept, later])
    assert not await before.repository.globally_paused()
    assert calls == []  # nothing was even quiesced
    assert len(backup.list_restore_points()) == 1  # no safety backup for a refused restore


async def test_staging_failure_after_quiescence_leaves_state_unchanged(served, monkeypatch):
    app, client, _composed, calls = served
    kept = await _submit(app, client, "kept")
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    later = await _submit(app, client, "later")
    before = app.state.application

    def refuse(_staged):
        raise backup.BackupRejected("restore", "staged database failed validation")

    monkeypatch.setattr(backup.StagedRestore, "validate", refuse)
    response = await client.post("/api/admin/backups/restore", json={"id": point})

    assert response.status_code == 400
    assert "left unchanged" in response.json()["detail"]
    assert app.state.application is before
    assert await _ids(app) == sorted([kept, later])
    # Processing is returned to the state it was in, and the scheduler runs again.
    assert not await before.repository.globally_paused()
    assert calls[-1] == ("start", before)
    assert not list(database.DB_PATH.parent.glob(".dp-restore-*"))


async def test_restored_integration_readiness_never_holds_the_restore(served):
    # The restored universe starts through the same lifetime owner: its core
    # state is proven before the swap is accepted, but a restored integration
    # that is still starting is neither a reason to wait nor to roll back.
    app, client, composed, _calls = served
    kept = await _submit(app, client, "kept")
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    await _submit(app, client, "later")
    release, starting = asyncio.Event(), asyncio.Event()
    compose = app.state.compose

    def compose_with_a_slow_integration():
        service = compose()
        slow = service.lifecycle[0]

        async def held_start():
            starting.set()
            await release.wait()
            slow.starts += 1

        slow.start = held_start
        return service

    app.state.compose = compose_with_a_slow_integration
    response = await client.post("/api/admin/backups/restore", json={"id": point})

    assert response.status_code == 200, response.text
    restored = app.state.application
    assert restored is composed[-1]
    await asyncio.wait_for(starting.wait(), timeout=5)
    assert await _ids(app) == [kept]
    assert _counts(restored)["a"] == (0, 0)  # still converging, and not reported started
    release.set()
    await restored.integrations_started()
    assert _counts(restored) == {"a": (0, 1), "b": (0, 1), "c": (0, 1)}


async def test_failed_restored_startup_rolls_back_to_the_pre_restore_state(served, monkeypatch):
    app, client, composed, _calls = served
    kept = await _submit(app, client, "kept")
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    later = await _submit(app, client, "later")

    import application.lifetime as lifetime
    original = lifetime.start_application
    attempts = []

    async def failing_once(service):
        attempts.append(service)
        if len(attempts) == 1:
            raise RuntimeError("restored startup failed")
        return await original(service)

    monkeypatch.setattr(lifetime, "start_application", failing_once)
    response = await client.post("/api/admin/backups/restore", json={"id": point})

    assert response.status_code == 400
    assert "left unchanged" in response.json()["detail"]
    assert await _ids(app) == sorted([kept, later])
    assert app.state.application is composed[-1] and app.state.application is attempts[-1]
    assert not list(database.DB_PATH.parent.glob(".dp-restore-*"))


async def test_restore_refuses_to_swap_while_a_native_execution_cannot_be_released(served):
    app, client, _composed, _calls = served
    transfer_id = await _submit(app, client, "stuck")
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    later = await _submit(app, client, "later")
    service = app.state.application
    executor = service.test_executor

    async def unconfirmed(handle):
        from transfers.models import ExecutionObservation
        return ExecutionObservation(handle, ExecutionState.UNKNOWN)

    executor.cancel = unconfirmed
    response = await client.post("/api/admin/backups/restore", json={"id": point})

    assert response.status_code == 409
    assert "left unchanged" in response.json()["detail"]
    assert await _ids(app) == sorted([transfer_id, later])
    assert app.state.application is service


async def test_an_interrupted_swap_is_reversed_before_startup(installation):
    point = await backup.create_restore_point()
    _set_user_version(7)  # live state diverges from the backup after it was taken
    kept_config = config.CONFIG_PATH.read_bytes()
    staged = await backup.stage_restore(point.id)
    staged.validate()
    staged.activate()  # the process "dies" here: no commit, no rollback
    assert _user_version() == 0

    assert backup.recover_interrupted_restore() is True

    assert _user_version() == 7
    assert config.CONFIG_PATH.read_bytes() == kept_config
    assert not list(database.DB_PATH.parent.glob(".dp-restore-*"))
    assert backup.recover_interrupted_restore() is False


def _set_user_version(value: int) -> None:
    conn = sqlite3.connect(database.DB_PATH)
    conn.execute(f"PRAGMA user_version={int(value)}")
    conn.commit()
    conn.close()


def _user_version() -> int:
    conn = sqlite3.connect(database.DB_PATH)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


async def test_save_endpoint_returns_the_same_portable_unit_add_accepts(served):
    _app, client, _composed, _calls = served
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    saved = await client.get(f"/api/admin/backups/{point}/package")
    assert saved.status_code == 200
    assert f"debridpulse-backup-{point}.zip" in saved.headers["content-disposition"]
    removed = await client.delete(f"/api/admin/backups/{point}")
    assert removed.status_code == 200
    added = await client.post("/api/admin/backups",
                              content=saved.content, headers={"Content-Type": "application/zip"})
    assert added.status_code == 200, added.text
    assert added.json()["backup"]["id"] == point
    rejected = await client.post("/api/admin/backups", content=b"nope", headers={"Content-Type": "application/zip"})
    assert rejected.status_code == 400
    assert rejected.json()["detail"] == ("Backup could not be added. "
                                         "The selected file is not a valid DebridPulse backup.")


async def test_staging_abandoned_before_any_swap_is_dropped_at_startup(installation):
    point = await backup.create_restore_point()
    staged = await backup.stage_restore(point.id)  # the process "dies" before activation
    assert list(database.DB_PATH.parent.glob(".dp-restore-*"))
    live = backup.schema_version(database.DB_PATH)

    assert backup.recover_interrupted_restore() is False  # nothing was swapped

    assert not list(database.DB_PATH.parent.glob(".dp-restore-*"))
    assert not list(config.CONFIG_PATH.parent.glob(".dp-restore-*"))
    assert backup.schema_version(database.DB_PATH) == live
    del staged



# --------------------------------------------------------------------------- #
# Gate 10 corrections: pre-change backups, refusal lifecycle, size contract
# --------------------------------------------------------------------------- #

async def test_a_pre_change_backup_saves_removes_adds_and_restores(served):
    """A restore point written before manifests recorded ``schema_version`` and
    ``database`` -- with the WAL-mode database copy that code produced -- is
    judged by its own database: compatible, so the whole lifecycle works."""
    app, client, _composed, _calls = served
    kept = await _submit(app, client, "kept")
    point = backup.restore_point((await client.post("/api/admin/backup")).json()["restore_point"]["id"])
    manifest_path = point.path / ".debridpulse-backup.json"
    manifest_path.write_text(json.dumps(_legacy(json.loads(manifest_path.read_text()))))
    conn = sqlite3.connect(point.path / "debridpulse.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    later = await _submit(app, client, "later")

    saved = await client.get(f"/api/admin/backups/{point.id}/package")
    assert saved.status_code == 200
    assert (await client.delete(f"/api/admin/backups/{point.id}")).status_code == 200
    added = await client.post("/api/admin/backups",
                              content=saved.content, headers={"Content-Type": "application/zip"})
    assert added.status_code == 200, added.text
    assert added.json()["backup"]["id"] == point.id
    restored = await client.post("/api/admin/backups/restore", json={"id": point.id})
    assert restored.status_code == 200, restored.text
    assert await _ids(app) == [kept]
    assert later not in await _ids(app)


async def test_a_drain_refusal_touches_no_integration_lifecycle(served):
    app, client, _composed, calls = served
    await _submit(app, client, "stuck")
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    service = app.state.application

    async def unconfirmed(handle):
        from transfers.models import ExecutionObservation
        return ExecutionObservation(handle, ExecutionState.UNKNOWN)

    service.test_executor.cancel = unconfirmed
    calls.clear()
    response = await client.post("/api/admin/backups/restore", json={"id": point})

    assert response.status_code == 409
    # Integrations were never stopped, so nothing is started again: no
    # startup sequence runs for an application whose state was not replaced.
    assert _counts(service) == {"a": (0, 0), "b": (0, 0), "c": (0, 0)}
    assert calls == [("stop", None), ("start", service)]
    assert not await service.repository.globally_paused()
    assert app.state.application is service


async def test_a_partial_integration_stop_restarts_only_what_was_stopped(served):
    app, client, _composed, calls = served
    kept = await _submit(app, client, "kept")
    point = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    later = await _submit(app, client, "later")
    service = app.state.application
    a, b, c = service.lifecycle
    b.fail_stop = True  # stops run c, b, a: c stops, b fails, a is never reached
    calls.clear()
    points = [item.id for item in backup.list_restore_points()]

    response = await client.post("/api/admin/backups/restore", json={"id": point})

    assert response.status_code == 409
    assert "left unchanged" in response.json()["detail"]
    assert _counts(service) == {"a": (0, 0), "b": (1, 0), "c": (1, 1)}
    assert calls == [("stop", None), ("start", service)]
    assert app.state.application is service
    assert await _ids(app) == sorted([kept, later])
    assert not await service.repository.globally_paused()
    assert [item.id for item in backup.list_restore_points()] == points  # no safety backup was taken


async def test_a_refusal_after_every_integration_stopped_restarts_each_once(served):
    app, client, _composed, calls = served
    await _submit(app, client, "kept")
    point = backup.restore_point((await client.post("/api/admin/backup")).json()["restore_point"]["id"])
    service = app.state.application
    last = service.lifecycle[0]  # stops run c, b, a

    async def stop_then_damage():
        last.stops += 1
        # The restore point breaks after pre-validation passed, so the refusal
        # comes at revalidation -- once every integration is already stopped.
        (point.path / "debridpulse.db").write_bytes(b"corrupted")

    last.stop = stop_then_damage
    calls.clear()

    response = await client.post("/api/admin/backups/restore", json={"id": point.id})

    assert response.status_code == 400
    assert "left unchanged" in response.json()["detail"]
    assert _counts(service) == {"a": (1, 1), "b": (1, 1), "c": (1, 1)}
    assert calls == [("stop", None), ("start", service)]
    assert not await service.repository.globally_paused()
    assert app.state.application is service


async def test_add_backup_has_no_size_ceiling_of_its_own_but_is_bounded_by_storage(installation, monkeypatch):
    import main

    # Whatever Save Backup produces is admissible again: neither the backup
    # owner nor the HTTP body limit holds a fixed package size.
    assert not hasattr(backup, "MAX_PACKAGE_BYTES")
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["path"])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    guard = main.RequestBodyLimitMiddleware(inner, max_bytes=1024)
    huge = str(8 * 1024 ** 3).encode()
    statuses = {}
    for path in ("/api/admin/backups", "/api/links/add"):
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        await guard({"type": "http", "method": "POST", "path": path,
                     "headers": [(b"content-length", huge)]}, receive, send)
        statuses[path] = sent[0]["status"]
    assert statuses == {"/api/admin/backups": 204, "/api/links/add": 413}

    # After ingress, what the package unpacks to must fit too: storage with
    # room for the (compressed) zip but not for its declared members.
    _point_id, saved = await _real_package(installation)
    with zipfile.ZipFile(saved) as package:
        declared = sum(info.file_size for info in package.infolist())
    assert declared > 2 * saved.stat().st_size
    real = backup.shutil.disk_usage

    def cramped(path):
        return real(path)._replace(free=2 * saved.stat().st_size)

    monkeypatch.setattr(backup.shutil, "disk_usage", cramped)
    with pytest.raises(backup.BackupRejected) as rejected:
        await backup.add_backup(_chunks(saved))
    assert rejected.value.reason == "space"
    assert backup.list_restore_points() == []


async def test_add_backup_refuses_a_stream_beyond_the_available_storage_while_it_arrives(served, monkeypatch):
    """The ingress budget is the Backup Folder's free space: an upload larger
    than that is refused while it streams, before the filesystem fills."""
    _app, client, _composed, _calls = served
    existing = (await client.post("/api/admin/backup")).json()["restore_point"]["id"]
    before = sorted(entry.name for entry in backup.restore_point(existing).path.iterdir())
    folder = backup.restore_point(existing).path.parent
    budget = 1024 * 1024
    real = backup.shutil.disk_usage
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda path: real(path)._replace(free=budget))
    chunk, total, sent = b"\0" * 65536, 64 * 1024 * 1024, []

    async def body():
        while sum(sent) < total:
            sent.append(len(chunk))
            yield chunk

    response = await client.post("/api/admin/backups", content=body(),
                                 headers={"Content-Type": "application/zip"})

    assert response.status_code == 507
    assert response.json()["detail"] == ("Backup could not be added. "
                                         "There is not enough free space in the Backup Folder for this backup.")
    # Refused early: the stream stopped being read at the budget, far short of the upload.
    assert budget <= sum(sent) <= budget + 2 * len(chunk) < total
    # No incomplete staging, and the managed backups are exactly as they were.
    assert [entry.name for entry in folder.iterdir() if entry.name.startswith(".dp-")] == []
    assert [point.id for point in backup.list_restore_points()] == [existing]
    assert sorted(entry.name for entry in backup.restore_point(existing).path.iterdir()) == before



# --------------------------------------------------------------------------- #
# the pinned pre-change restore point
# --------------------------------------------------------------------------- #

# Written by the pinned baseline's own backup service (6b691ef7), before the
# transfer-owned selection-intent tables existed.
BASELINE_BACKUP = Path(__file__).parent / "fixtures" / "backup-1.0.13-6b691ef7.zip"


def _managed_database(point) -> Path:
    return point.path / json.loads((point.path / ".debridpulse-backup.json").read_text())["database"]


def _digest(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def test_the_pinned_pre_change_backup_is_upgraded_on_a_private_copy_and_restored(served):
    """T12: admitted and validated by its own recorded schema, upgraded only
    on the private staged copy through the canonical bootstrap, which then
    equals the running schema; the managed backup is never modified."""
    app, client, _composed, _calls = served
    live = backup.schema_version(database.DB_PATH)
    added = await client.post("/api/admin/backups", content=BASELINE_BACKUP.read_bytes(),
                              headers={"Content-Type": "application/zip"})
    assert added.status_code == 200, added.text
    point = backup.restore_point(added.json()["backup"]["id"])
    managed = _managed_database(point)
    before = _digest(managed)
    with backup._open_frozen(managed) as conn:
        assert backup._fingerprint(conn) != live
    restored = await client.post("/api/admin/backups/restore", json={"id": point.id})
    assert restored.status_code == 200, restored.text
    assert _digest(managed) == before and sorted(entry.name for entry in point.path.iterdir()) == sorted(
        [".debridpulse-backup.json", "config.json", managed.name])
    assert backup.schema_version(database.DB_PATH) == live
    conn = sqlite3.connect(database.DB_PATH)
    try:
        assert [row[0] for row in conn.execute("SELECT id FROM torrents")] == [1]
        assert conn.execute("SELECT COUNT(*) FROM transfer_file_selection_intents").fetchone()[0] == 0
        assert conn.execute("SELECT decision FROM transfer_file_selections").fetchall() == [("explicit",)]
    finally:
        conn.close()


# Written by the pinned baseline's own backup service (8b009d04), immediately
# before the event journal existed: one transfer and its legacy "events" row.
PRE_JOURNAL_BACKUP = Path(__file__).parent / "fixtures" / "backup-1.0.13-8b009d04.zip"


async def test_the_pinned_pre_journal_backup_restores_an_empty_journal_holding_only_its_restore(served):
    """Validated by its own recorded schema, upgraded on the private staged
    copy only, and activated: the restored journal starts empty -- the legacy
    row is kept, never converted -- the replaced database's history is gone
    with it, and the restore itself is recorded only in the restored database,
    after activation."""
    from db import event_journal

    app, client, _composed, _calls = served
    live = backup.schema_version(database.DB_PATH)
    await event_journal.record_now(event_journal.JournalEvent(
        "administration", "administration.marker", "info", "recorded before the restore", "installation"))
    added = await client.post("/api/admin/backups", content=PRE_JOURNAL_BACKUP.read_bytes(),
                              headers={"Content-Type": "application/zip"})
    assert added.status_code == 200, added.text
    point = backup.restore_point(added.json()["backup"]["id"])
    with backup._open_frozen(_managed_database(point)) as conn:
        assert backup._fingerprint(conn) in backup._UPGRADABLE_SCHEMAS and backup._fingerprint(conn) != live
    restored = await client.post("/api/admin/backups/restore", json={"id": point.id})
    assert restored.status_code == 200, restored.text
    assert restored.json()["journal_recorded"] is True
    assert backup.schema_version(database.DB_PATH) == live
    conn = sqlite3.connect(database.DB_PATH)
    try:
        assert conn.execute("SELECT message FROM events").fetchall() == [("Transfer accepted",)]
        assert conn.execute("SELECT event_type, subject_id FROM event_journal").fetchall() == [
            ("administration.backup_restored", point.id)]
        assert conn.execute("SELECT name FROM torrents").fetchall() == [("pre-journal.bin",)]
    finally:
        conn.close()


# Written by the pinned baseline's own backup service (52012d4d), immediately
# before transfers carried an operator queue promotion: one transfer and its
# execution attempt.
PRE_QUEUE_BACKUP = Path(__file__).parent / "fixtures" / "backup-1.0.13-52012d4d.zip"


async def test_the_pinned_pre_queue_backup_restores_with_no_queue_promotion(served):
    """Validated by its own recorded schema, upgraded on the private staged
    copy only through the canonical bootstrap, and activated: its transfer and
    attempt are kept unchanged, its queue priority stays as recorded, and the
    transfer holds no promotion it never had."""
    app, client, _composed, _calls = served
    live = backup.schema_version(database.DB_PATH)
    added = await client.post("/api/admin/backups", content=PRE_QUEUE_BACKUP.read_bytes(),
                              headers={"Content-Type": "application/zip"})
    assert added.status_code == 200, added.text
    point = backup.restore_point(added.json()["backup"]["id"])
    managed = _managed_database(point)
    before = _digest(managed)
    with backup._open_frozen(managed) as conn:
        assert backup._fingerprint(conn) in backup._UPGRADABLE_SCHEMAS and backup._fingerprint(conn) != live
    restored = await client.post("/api/admin/backups/restore", json={"id": point.id})
    assert restored.status_code == 200, restored.text
    assert _digest(managed) == before
    assert backup.schema_version(database.DB_PATH) == live
    conn = sqlite3.connect(database.DB_PATH)
    try:
        assert conn.execute("SELECT name, priority, queue_promotion FROM torrents").fetchall() == [
            ("pre-queue.bin", 0, None)]
        assert conn.execute("SELECT COUNT(*) FROM execution_attempts").fetchone() == (1,)
    finally:
        conn.close()


# Written by the pinned baseline's own backup service (03123a72), immediately
# before accounts existed: its configuration holds that installation's one
# local credential (operator / pre-account-secret), with password sign-in on.
PRE_ACCOUNT_BACKUP = Path(__file__).parent / "fixtures" / "backup-1.0.13-03123a72.zip"


async def test_the_pinned_pre_account_backup_restores_its_operator_as_the_default_admin(served):
    """Upgraded on the private staged copy only, then started: the backup's
    configuration-file credential becomes the default Admin's and leaves the
    file, the replaced accounts are gone with the replaced database, and no
    session issued before the restore survives it."""
    from auth import accounts
    from auth.models import Principal
    from auth.passwords import verify_password
    from auth.sessions import session_store

    app, client, _composed, _calls = served
    live = backup.schema_version(database.DB_PATH)
    replaced = await accounts.create_local_account("replaced-admin", "secret", role="admin")
    token, _ = session_store.create(Principal.password_session(replaced), lifetime_seconds=3600)
    added = await client.post("/api/admin/backups", content=PRE_ACCOUNT_BACKUP.read_bytes(),
                              headers={"Content-Type": "application/zip"})
    assert added.status_code == 200, added.text
    point = backup.restore_point(added.json()["backup"]["id"])
    with backup._open_frozen(_managed_database(point)) as conn:
        assert backup._fingerprint(conn) in backup._UPGRADABLE_SCHEMAS and backup._fingerprint(conn) != live

    restored = await client.post("/api/admin/backups/restore", json={"id": point.id})

    assert restored.status_code == 200, restored.text
    assert backup.schema_version(database.DB_PATH) == live
    assert session_store.resolve(token) is None
    assert await accounts.get_account(replaced.id) is None
    operator, password_hash = await accounts.local_credential("operator")
    assert operator.id == accounts.DEFAULT_ADMIN_ID and operator.role is accounts.Role.ADMIN
    assert verify_password(password_hash, "pre-account-secret")
    persisted = json.loads(config.CONFIG_PATH.read_text(encoding="utf-8"))
    assert persisted["auth_password_enabled"] is True
    assert not {"auth_username", "auth_password", "auth_password_hash"} & set(persisted)


async def test_a_backup_that_would_leave_no_usable_admin_is_refused_before_anything_changes(served, tmp_path):
    """A restore replaces every account and authentication setting. One whose
    restored state no Admin could sign in to -- here a pre-account OIDC-only
    installation, whose operator is bound to no account -- is refused before
    anything is quiesced, backed up or replaced."""
    app, client, _composed, calls = served
    with zipfile.ZipFile(PRE_ACCOUNT_BACKUP) as package:
        document = json.loads(package.read("config.json"))
    for key in ("auth_username", "auth_password_hash"):
        document.pop(key)
    document.update(auth_password_enabled=False, auth_oidc_enabled=True, oidc_issuer_url="https://id.example/",
                    oidc_client_id="debridpulse", oidc_allow_all=True, public_base_url="https://pulse.example")
    package = _repack(PRE_ACCOUNT_BACKUP, tmp_path / "oidc-only.zip",
                      replace_members={"config.json": json.dumps(document)})
    added = await client.post("/api/admin/backups", content=package.read_bytes(),
                              headers={"Content-Type": "application/zip"})
    assert added.status_code == 200, added.text
    before, live = app.state.application, backup.schema_version(database.DB_PATH)
    calls.clear()

    restored = await client.post("/api/admin/backups/restore", json={"id": added.json()["backup"]["id"]})

    assert restored.status_code == 400
    assert backup.NO_USABLE_ADMIN in restored.json()["detail"]
    assert "left unchanged" in restored.json()["detail"]
    assert app.state.application is before and calls == []
    assert backup.schema_version(database.DB_PATH) == live
    assert len(backup.list_restore_points()) == 1  # no safety backup for a refused restore


async def test_a_failed_upgrade_of_the_staged_copy_leaves_the_live_state_and_the_backup_untouched(
        served, monkeypatch):
    app, client, _composed, _calls = served
    kept = await _submit(app, client, "kept")
    added = await client.post("/api/admin/backups", content=BASELINE_BACKUP.read_bytes(),
                              headers={"Content-Type": "application/zip"})
    point = backup.restore_point(added.json()["backup"]["id"])
    before = _digest(_managed_database(point))

    async def broken(path=None):
        raise RuntimeError("upgrade failed")

    monkeypatch.setattr(database, "init_db", broken)
    response = await client.post("/api/admin/backups/restore", json={"id": point.id})
    assert response.status_code == 400
    assert await _ids(app) == [kept]
    assert _digest(_managed_database(point)) == before
    assert not list(database.DB_PATH.parent.glob(".dp-restore-*"))
    assert not (database.DB_PATH.parent / ".dp-restore-journal.json").exists()


# --------------------------------------------------------------------------- #
# scheduled cadence (durable, restart-safe) + age OR count retention
# --------------------------------------------------------------------------- #

class _Stopped(Exception):
    """The end of one scheduler lifetime in a test."""


def _clock(monkeypatch) -> list[float]:
    """The backup owner's wall clock, under the test's control."""
    clock = [time.time()]
    monkeypatch.setattr(backup, "time", SimpleNamespace(time=lambda: clock[0]), raising=False)
    monkeypatch.setattr(backup, "_schedule_retry_at", 0.0, raising=False)
    return clock


async def _scheduler_lifetime(monkeypatch, clock) -> None:
    """One process start of the real backup loop: its readiness delay and its
    first pass. The next sleep ends that lifetime, as a restart would."""
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 1:
            raise _Stopped
        clock[0] += seconds

    monkeypatch.setattr(scheduler, "application", None)
    monkeypatch.setattr(scheduler, "asyncio", SimpleNamespace(sleep=sleep))
    with pytest.raises(_Stopped):
        await scheduler.backup_loop()


def _policy(**values) -> None:
    """Persist backup settings as the operator would (the fixture restores)."""
    settings = config.get_settings().model_copy(update=values)
    config.apply_settings(settings)
    config.save_settings(settings)


def _age(point_path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(point_path, (stamp, stamp))


async def _seed(count: int, age_of) -> list[str]:
    """``count`` valid managed restore points, copies of one real one, each
    ``age_of(index)`` seconds old. Returns their ids, newest first."""
    origin = await backup.create_restore_point()
    folder = origin.path.parent
    now = time.time()
    seeded = []
    for index in range(count):
        stamp = now - age_of(index)
        point_id = f"{datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}"
        target = folder / point_id
        shutil.copytree(origin.path, target)
        manifest = target / ".debridpulse-backup.json"
        manifest.write_text(json.dumps({**json.loads(manifest.read_text()), "timestamp": point_id}))
        os.utime(target, (stamp, stamp))
        seeded.append((stamp, point_id))
    backup.remove_restore_point(origin.id)
    return [point_id for _stamp, point_id in sorted(seeded, reverse=True)]


def _listed() -> list[str]:
    return [point.id for point in backup.list_restore_points()]


async def test_restarts_inside_the_backup_interval_take_no_scheduled_backup(installation, monkeypatch):
    clock = _clock(monkeypatch)
    await _scheduler_lifetime(monkeypatch, clock)  # a fresh installation: its first scheduled backup
    first = _listed()
    assert len(first) == 1
    for _restart in range(4):  # restarted ten minutes later, again and again
        clock[0] += 600
        await _scheduler_lifetime(monkeypatch, clock)
    assert _listed() == first
    clock[0] += 24 * 3600  # 24 hours after the scheduled backup, not after a start
    await _scheduler_lifetime(monkeypatch, clock)
    assert len(_listed()) == 2


async def test_manual_imported_and_safety_backups_never_move_the_scheduled_clock(installation, monkeypatch):
    clock = _clock(monkeypatch)
    _imported_id, saved = await _real_package(installation, "imported.zip")
    await _scheduler_lifetime(monkeypatch, clock)  # the scheduled backup at T
    clock[0] += 23 * 3600
    await backup.run_backup()  # Run Backup
    await backup.add_backup(_chunks(saved))  # Add Backup
    await backup.create_restore_point()  # a mandatory pre-restore / pre-reset safety backup
    count = len(_listed())
    await _scheduler_lifetime(monkeypatch, clock)  # T + 23 h: not due
    assert len(_listed()) == count
    clock[0] += 3600
    await _scheduler_lifetime(monkeypatch, clock)  # T + 24 h: due, however recent the others are
    assert len(_listed()) == count + 1


async def test_forty_backups_from_two_days_converge_to_the_newest_seven_without_a_new_backup(
        installation, monkeypatch):
    _policy(backup_enabled=False, backup_keep_days=7, backup_max_retained=7)
    seeded = await _seed(40, lambda index: 2 * 86400 - index * 60)  # every one within age retention
    clock = _clock(monkeypatch)
    await _scheduler_lifetime(monkeypatch, clock)  # a start: retention only, creation is disabled
    assert _listed() == seeded[:7]


@pytest.mark.parametrize("outcome", ["restored", "refused", "rolled_back"])
async def test_a_restore_keeps_its_source_and_safety_backup_until_it_resolves_under_a_one_backup_limit(
        served, monkeypatch, outcome):
    app, client, _composed, _calls = served
    import application.lifetime as lifetime
    if outcome == "refused":
        def refused(_staged):
            raise RuntimeError("staged state is not valid")
        monkeypatch.setattr(backup.StagedRestore, "validate", refused)
    elif outcome == "rolled_back":
        original_start, attempts = lifetime.start_application, []

        async def failing_once(service):
            attempts.append(service)
            if len(attempts) == 1:
                raise RuntimeError("restored startup failed")
            return await original_start(service)

        monkeypatch.setattr(lifetime, "start_application", failing_once)
    _policy(backup_enabled=False, backup_max_retained=1)
    clock = _clock(monkeypatch)
    extra = await backup.create_restore_point()
    source = await backup.create_restore_point()
    _age(extra.path, 300)
    _age(source.path, 200)
    original = backup.stage_restore
    during = []

    async def staged_while_a_retention_pass_lands(point_id):
        await _scheduler_lifetime(monkeypatch, clock)
        during.append(set(_listed()))
        return await original(point_id)

    monkeypatch.setattr(backup, "stage_restore", staged_while_a_retention_pass_lands)
    response = await client.post("/api/admin/backups/restore", json={"id": source.id})
    assert response.status_code == (200 if outcome == "restored" else 400), response.text
    safety = next(point_id for point_id in during[0] if point_id not in {source.id, extra.id})
    assert {source.id, safety} <= during[0]  # both still needed: neither is pruned
    assert extra.id not in during[0]  # nothing protects it from the count limit
    assert _listed() == [safety]  # the restore resolved: the ordinary policy converges


def _record() -> dict:
    return json.loads((database.DB_PATH.parent / ".dp-backup-schedule.json").read_text())


async def test_a_fresh_installation_records_its_first_scheduled_backup_beside_the_live_database(
        installation, monkeypatch):
    clock = _clock(monkeypatch)
    await _scheduler_lifetime(monkeypatch, clock)
    record_path = database.DB_PATH.parent / ".dp-backup-schedule.json"
    assert record_path.is_file() and (record_path.stat().st_mode & 0o777) == 0o600
    assert _record() == {"last_scheduled": clock[0], "pending": None}
    assert len(_listed()) == 1
    assert not _folder(installation).joinpath(".dp-backup-schedule.json").exists()


async def test_disable_reenable_and_interval_changes_follow_the_last_scheduled_backup(installation, monkeypatch):
    clock = _clock(monkeypatch)
    await _scheduler_lifetime(monkeypatch, clock)  # scheduled at T
    start = clock[0]
    _policy(backup_enabled=False)
    clock[0] = start + 25 * 3600
    await _scheduler_lifetime(monkeypatch, clock)
    assert len(_listed()) == 1  # disabled: due, but never created
    assert _record()["last_scheduled"] == start
    _policy(backup_enabled=True, backup_interval_hours=48)
    await _scheduler_lifetime(monkeypatch, clock)  # re-enabled, lengthened: T + 26 h is not due
    assert len(_listed()) == 1
    _policy(backup_interval_hours=6)
    await _scheduler_lifetime(monkeypatch, clock)  # shortened: due at once, exactly once
    await _scheduler_lifetime(monkeypatch, clock)
    assert len(_listed()) == 2
    second = _record()["last_scheduled"]
    clock[0] = second + 5 * 3600
    await _scheduler_lifetime(monkeypatch, clock)
    assert len(_listed()) == 2
    clock[0] = second + 6 * 3600
    await _scheduler_lifetime(monkeypatch, clock)
    assert len(_listed()) == 3


async def test_a_failed_scheduled_backup_records_no_success_and_is_retried_on_a_bounded_cadence(
        installation, monkeypatch):
    clock = _clock(monkeypatch)
    real_backup, attempts = backup._sqlite_backup, []

    def full_disk(source, destination):
        attempts.append(destination)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(backup, "_sqlite_backup", full_disk)
    await _scheduler_lifetime(monkeypatch, clock)
    assert _listed() == [] and len(attempts) == 1
    assert _record()["last_scheduled"] is None  # no false success
    assert not [entry for entry in _folder(installation).iterdir() if entry.name.startswith(".dp-staging-")]
    for _pass in range(3):  # the loop keeps evaluating every few minutes...
        clock[0] += 300
        await _scheduler_lifetime(monkeypatch, clock)
    assert len(attempts) == 1  # ...but a failure is not retried immediately and continuously
    monkeypatch.setattr(backup, "_sqlite_backup", real_backup)
    clock[0] += 3600
    await _scheduler_lifetime(monkeypatch, clock)
    assert len(_listed()) == 1
    assert _record() == {"last_scheduled": clock[0], "pending": None}


async def test_an_upgraded_installation_bootstraps_its_clock_once_from_its_newest_backup(installation, monkeypatch):
    seeded = await _seed(5, lambda index: 3600 * (index + 1))  # backups 1..5 hours old, no record yet
    clock = _clock(monkeypatch)
    await _scheduler_lifetime(monkeypatch, clock)  # the first start of the new build
    assert _listed() == seeded  # no extra copy
    newest = backup.restore_point(seeded[0]).path.stat().st_mtime
    assert _record()["last_scheduled"] == newest
    await backup.run_backup()  # later manual backups never re-derive the clock
    clock[0] = newest + 24 * 3600 + 1
    await _scheduler_lifetime(monkeypatch, clock)
    assert len(_listed()) == 7


@pytest.mark.parametrize("damage", ["missing", "malformed", "future", "symlink"])
async def test_a_missing_damaged_or_future_record_never_floods(installation, monkeypatch, damage):
    clock = _clock(monkeypatch)
    await _scheduler_lifetime(monkeypatch, clock)
    record_path = database.DB_PATH.parent / ".dp-backup-schedule.json"
    if damage == "missing":
        record_path.unlink()
    elif damage == "malformed":
        record_path.write_text('{"last_scheduled": "yesterday"')
    elif damage == "future":
        record_path.write_text(json.dumps({"last_scheduled": clock[0] + 10 * 86400, "pending": None}))
    else:
        elsewhere = installation / "elsewhere.json"
        elsewhere.write_text(json.dumps({"last_scheduled": 0, "pending": None}))
        record_path.unlink()
        record_path.symlink_to(elsewhere)
    for _restart in range(3):
        clock[0] += 600
        await _scheduler_lifetime(monkeypatch, clock)
    assert len(_listed()) == 1
    assert not record_path.is_symlink()
    clock[0] += 24 * 3600
    await _scheduler_lifetime(monkeypatch, clock)
    assert len(_listed()) == 2  # one interval later, never never


async def test_changing_the_backup_folder_does_not_reset_the_scheduled_clock(installation, monkeypatch):
    clock = _clock(monkeypatch)
    await _scheduler_lifetime(monkeypatch, clock)
    recorded = _record()
    _policy(backup_folder=str(installation / "elsewhere"))
    clock[0] += 3600
    await _scheduler_lifetime(monkeypatch, clock)
    assert _listed() == []  # nothing new in the new folder: not due
    assert _record() == recorded


async def test_the_record_is_no_restore_point_and_survives_restore_even_inside_the_data_directory(
        installation, monkeypatch):
    _policy(backup_folder=str(database.DB_PATH.parent), backup_max_retained=1, backup_keep_days=1)
    clock = _clock(monkeypatch)
    await _scheduler_lifetime(monkeypatch, clock)
    record_path = database.DB_PATH.parent / ".dp-backup-schedule.json"
    recorded = record_path.read_bytes()
    point = backup.list_restore_points()[0]
    assert ".dp-backup-schedule.json" not in point.public()["id"]
    assert ".dp-backup-schedule.json" not in json.loads((point.path / ".debridpulse-backup.json").read_text())["files"]
    old = time.time() - 30 * 86400
    os.utime(record_path, (old, old))
    assert await backup.apply_retention() == (0, 0)  # never counted, never aged out
    staged = await backup.stage_restore(point.id)
    staged.validate()
    staged.activate()
    assert all(".dp-backup-schedule.json" not in move for pair in staged.moves for move in pair)
    assert backup.recover_interrupted_restore() is True  # an interrupted restore, reversed
    staged = await backup.stage_restore(point.id)
    staged.validate()
    staged.activate()
    staged.commit()  # and a completed one
    assert record_path.read_bytes() == recorded


async def test_a_crash_between_the_restore_point_and_its_record_takes_no_second_backup(installation, monkeypatch):
    clock = _clock(monkeypatch)

    class Crash(BaseException):
        pass

    real_write = backup._write_schedule

    def crashing_settlement(last, pending=None):
        if pending is None and last is not None:
            raise Crash  # the process dies after the restore point, before its record
        real_write(last, pending)

    monkeypatch.setattr(backup, "_write_schedule", crashing_settlement)
    with pytest.raises(Crash):
        await backup.run_scheduled_backup()
    monkeypatch.setattr(backup, "_write_schedule", real_write)
    created = _listed()
    assert len(created) == 1 and _record()["pending"] == created[0]
    clock[0] += 600
    await _scheduler_lifetime(monkeypatch, clock)  # the restart settles it as the success it was
    assert _listed() == created and _record()["pending"] is None


async def test_a_crash_inside_a_scheduled_backup_is_never_a_success(installation, monkeypatch):
    clock = _clock(monkeypatch)

    class Crash(BaseException):
        pass

    real_backup = backup._sqlite_backup

    def dies(source, destination):
        raise Crash

    monkeypatch.setattr(backup, "_sqlite_backup", dies)
    with pytest.raises(Crash):
        await backup.run_scheduled_backup()
    monkeypatch.setattr(backup, "_sqlite_backup", real_backup)
    assert _listed() == [] and _record()["pending"] is not None
    clock[0] += 600
    await _scheduler_lifetime(monkeypatch, clock)
    assert len(_listed()) == 1  # due, exactly once
    assert _record()["pending"] is None


async def test_concurrent_scheduled_passes_take_one_backup_for_one_due_interval(installation, monkeypatch):
    _clock(monkeypatch)
    await asyncio.gather(backup.run_scheduled_backup(), backup.run_scheduled_backup(), backup.run_backup())
    scheduled = _record()
    assert len(_listed()) == 2  # one scheduled, one Run Backup
    await backup.run_scheduled_backup()
    assert len(_listed()) == 2 and _record() == scheduled


async def test_a_backup_older_than_the_age_limit_is_pruned_below_the_count_limit(installation, monkeypatch):
    _policy(backup_enabled=False, backup_keep_days=7, backup_max_retained=7)
    seeded = await _seed(3, lambda index: [3600, 7200, 8 * 86400][index])
    assert await backup.apply_retention() == (1, 0)
    assert _listed() == seeded[:2]


async def test_every_kind_of_restore_point_counts_and_an_added_one_ranks_by_admission(installation, monkeypatch):
    imported_id, saved = await _real_package(installation, "imported.zip")  # the OLDEST identity
    clock = _clock(monkeypatch)
    await _scheduler_lifetime(monkeypatch, clock)
    scheduled = _listed()[0]
    manual = (await backup.run_backup())["timestamp"]
    safety = (await backup.create_restore_point()).id
    for point_id, seconds in ((scheduled, 400), (manual, 300), (safety, 200)):
        _age(backup.restore_point(point_id).path, seconds)
    _policy(backup_max_retained=2)
    admitted = await backup.add_backup(_chunks(saved))  # admitted now: the newest by admission
    assert admitted.id == imported_id
    assert set(_listed()) == {imported_id, safety}


async def test_a_tie_in_creation_time_is_broken_by_identity(installation, monkeypatch):
    _policy(backup_enabled=False, backup_max_retained=1)
    seeded = await _seed(2, lambda index: 3600)
    stamp = time.time() - 3600
    for point_id in seeded:
        os.utime(backup.restore_point(point_id).path, (stamp, stamp))
    assert await backup.apply_retention() == (0, 1)
    assert _listed() == [max(seeded)]


async def test_retention_never_touches_or_counts_anything_it_does_not_own(installation, monkeypatch):
    _policy(backup_enabled=False, backup_keep_days=1, backup_max_retained=1)
    seeded = await _seed(2, lambda index: [3600, 7200][index])
    folder = _folder(installation)
    outside = installation / "outside-point"
    shutil.copytree(backup.restore_point(seeded[0]).path, outside)
    strangers = {
        "20200101_000000": "dir",  # restore-point name, no manifest
        "20200101_000001_" + "1" * 32: "foreign-manifest",
        "20200101_000002_" + "2" * 32: "symlink",
        "20200101_000003": "file",
        ".dp-staging-" + "3" * 32: "dir",
        ".dp-package-" + "4" * 32 + ".zip": "file",
        "debridpulse-backup-20200101_000004.zip": "file",
        "notes.txt": "file",
    }
    for name, kind in strangers.items():
        entry = folder / name
        if kind == "dir":
            entry.mkdir()
            (entry / "keep.txt").write_text("keep")
        elif kind == "foreign-manifest":
            entry.mkdir()
            (entry / ".debridpulse-backup.json").write_text(json.dumps({"kind": "someone-else"}))
        elif kind == "symlink":
            entry.symlink_to(outside, target_is_directory=True)
        else:
            entry.write_text("keep")
        old = time.time() - 365 * 86400
        os.utime(entry, (old, old), follow_symlinks=False)
    assert await backup.apply_retention() == (0, 1)  # only the older of its own two
    assert _listed() == seeded[:1]
    assert {entry.name for entry in folder.iterdir()} == set(strangers) | {seeded[0]}
    assert (outside / ".debridpulse-backup.json").exists()


async def test_an_upgrade_prunes_thirty_daily_backups_to_the_newest_seven_at_its_first_start(
        installation, monkeypatch, caplog):
    _policy(backup_keep_days=30)
    document = json.loads(config.CONFIG_PATH.read_text())
    document.pop("backup_max_retained", None)  # written before the setting existed
    config.CONFIG_PATH.write_text(json.dumps(document))
    upgraded = config.load_settings()
    assert upgraded.backup_max_retained == 7 and upgraded.backup_keep_days == 30
    config.apply_settings(upgraded)
    seeded = await _seed(30, lambda index: index * 86400 + 3600)  # daily, all within 30 days
    clock = _clock(monkeypatch)
    caplog.set_level("INFO", logger="debridpulse.backup")
    await _scheduler_lifetime(monkeypatch, clock)  # no confirmation, no new backup
    assert _listed() == seeded[:7]
    summaries = [record.getMessage() for record in caplog.records if "retention removed" in record.getMessage()]
    assert summaries == ["Backup retention removed 23 backup(s): 0 older than 30 day(s), 23 beyond the newest 7"]


async def test_the_count_setting_defaults_validates_and_round_trips(installation):
    from core.config_validator import validate_and_sanitise
    from pydantic import ValidationError

    assert config.AppSettings().backup_max_retained == 7
    assert validate_and_sanitise(config.AppSettings(backup_max_retained=0)).backup_max_retained == 1
    assert validate_and_sanitise(config.AppSettings(backup_max_retained=1000)).backup_max_retained == 365
    with pytest.raises(ValidationError):
        config.AppSettings(backup_max_retained="many")
    _policy(backup_max_retained=12)
    assert config.load_settings().backup_max_retained == 12
    assert config.AppSettings().stats_snapshot_keep_days == 30  # statistics retention is its own


async def test_an_interrupted_restore_keeps_both_points_until_startup_recovery_resolves_it(
        installation, monkeypatch):
    """Gate 5: a scheduled backup, restarts without duplicates, then a restore
    under a one-backup limit that dies after its safety backup -- before the
    swap and, separately, after it -- and the next start."""
    clock = _clock(monkeypatch)
    await _scheduler_lifetime(monkeypatch, clock)
    clock[0] += 600
    await _scheduler_lifetime(monkeypatch, clock)
    source = _listed()
    assert len(source) == 1  # no duplicate after the restart
    folder = _folder(installation)
    (folder / "notes.txt").write_text("keep")
    _policy(backup_max_retained=1)
    _age(backup.restore_point(source[0]).path, 120)

    for crash in ("before_swap", "after_swap"):
        # The restore, in a process that dies after its safety backup; what it
        # held in memory dies with it.
        safety = await backup.create_restore_point()
        staged = await backup.stage_restore(source[0])
        if crash == "after_swap":
            staged.validate()
            staged.activate()
            # Even out of order, an undecided swap prunes nothing.
            assert await backup.apply_retention() == (0, 0)
        assert set(_listed()) == {source[0], safety.id}
        # The next start: interrupted-restore recovery first...
        assert backup.recover_interrupted_restore() is (crash == "after_swap")
        assert set(_listed()) == {source[0], safety.id}
        # ...and only then the scheduler's first pass reconciles retention.
        clock[0] += 600
        await _scheduler_lifetime(monkeypatch, clock)
        assert _listed() == [safety.id]
        assert (folder / "notes.txt").exists()
        source = [safety.id]
        _age(backup.restore_point(safety.id).path, 120)
    lifespan = (Path(__file__).parents[1] / "main.py").read_text()
    assert lifespan.index("recover_interrupted_restore()") < lifespan.index("await start_application(")


def _interleaved_journal(monkeypatch) -> list[tuple[str, str, str | None]]:
    """The backup journal, recording what each operation reported. The first
    operation to report waits there until the second has reported, so the
    second runs to completion -- creation and retention -- in between."""
    reported, second = [], asyncio.Event()

    async def journal(event_type, message, point_id, detail=None):
        reported.append((event_type, point_id, detail))
        if len(reported) == 1:
            await asyncio.wait_for(second.wait(), 10)
        else:
            second.set()
        return True

    monkeypatch.setattr(backup, "journal_backup", journal)
    return reported


@pytest.mark.parametrize("first", ["manual", "scheduled"])
async def test_concurrent_manual_and_scheduled_backups_report_truthfully_under_a_one_backup_limit(
        installation, monkeypatch, first):
    _policy(backup_max_retained=1)
    _clock(monkeypatch)
    backup._write_schedule(None)  # a scheduled backup has never been taken: it is due
    reported = _interleaved_journal(monkeypatch)
    operations = {"manual": backup.run_backup, "scheduled": backup.run_scheduled_backup}
    order = [first, "scheduled" if first == "manual" else "manual"]
    done = dict(zip(order, await asyncio.gather(*(operations[name]() for name in order))))

    manual, scheduled = done["manual"], done["scheduled"]
    assert manual["restore_point"]["id"] == manual["timestamp"]
    assert manual["backed_up"] == sorted(["config.json", database.DB_PATH.name])
    assert scheduled is not None and scheduled.id != manual["timestamp"]
    # Exactly what each operation reported, and one point is left: the newer.
    later = order[1]
    later_id = manual["timestamp"] if later == "manual" else scheduled.id
    earlier_id = scheduled.id if later == "manual" else manual["timestamp"]
    assert _listed() == [later_id]
    pruned_detail = "1 older backup(s) removed by retention (0 by age, 1 by count)"
    assert sorted(reported) == sorted([
        ("administration.backup_created", earlier_id, None),
        ("administration.backup_created", later_id, pruned_detail),
    ])
    assert manual["rotated"] == (1 if later == "manual" else 0)


async def test_add_backup_and_a_concurrent_scheduled_backup_report_truthfully_under_a_one_backup_limit(
        installation, monkeypatch):
    _imported_id, saved = await _real_package(installation, "imported.zip")
    _policy(backup_max_retained=1)
    _clock(monkeypatch)
    backup._write_schedule(None)
    reported = _interleaved_journal(monkeypatch)
    admitted, scheduled = await asyncio.gather(backup.add_backup(_chunks(saved)), backup.run_scheduled_backup())
    assert admitted.size_bytes > 0 and admitted.id == _imported_id
    assert _listed() == [scheduled.id]
    assert sorted(reported) == sorted([
        ("administration.backup_added", admitted.id, None),
        ("administration.backup_created", scheduled.id,
         "1 older backup(s) removed by retention (0 by age, 1 by count)"),
    ])
