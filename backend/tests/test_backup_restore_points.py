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
import sqlite3
import time
import zipfile
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import core.config as config
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
    result = await backup.run_backup()
    remaining = {point.id for point in backup.list_restore_points()}
    assert result["rotated"] == 1
    assert old.id not in remaining
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
