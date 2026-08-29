"""Regression coverage for built-in aria2 restart/resume canonical-path recovery."""
from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.dispatch_coordinator import MirrorAwareTransferControlCoordinator
from services.restart_resume_control import (
    RestartResumableTransferControlCoordinator,
    resume_artifact_state,
)
from services.transfer_service import transfer_service


class _Cursor:
    rowcount = 1


class _FakeDb:
    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self.executions: list[tuple[str, tuple]] = []
        self.commits = 0

    async def fetchall(self, _sql, _params=()):
        return [dict(row) for row in self.rows]

    async def execute(self, sql, params=()):
        self.executions.append((sql, tuple(params)))
        return _Cursor()

    async def commit(self):
        self.commits += 1


@asynccontextmanager
async def _db_context(db):
    yield db


def _bare_coordinator(*, pause_intents=None):
    coordinator = RestartResumableTransferControlCoordinator.__new__(
        RestartResumableTransferControlCoordinator
    )
    coordinator.manager = SimpleNamespace(_log_event=AsyncMock())
    coordinator._pause_intents = set(pause_intents or set())
    coordinator._lost_strikes = {}
    coordinator.confirm_gid = AsyncMock(return_value=None)
    coordinator.ensure_initialized = AsyncMock()
    coordinator._schedule_queue = AsyncMock()
    return coordinator


def test_application_binds_restart_resumable_coordinator():
    assert isinstance(
        transfer_service.control.coordinator,
        RestartResumableTransferControlCoordinator,
    )


def test_exact_payload_and_aria2_sidecar_are_detected(tmp_path):
    target = tmp_path / "movie.bin"
    target.write_bytes(b"partial")
    sidecar = tmp_path / "movie.bin.aria2"
    sidecar.write_bytes(b"control")

    assert resume_artifact_state(target) == (True, True)
    sidecar.unlink()
    assert resume_artifact_state(target) == (True, False)


@pytest.mark.asyncio
async def test_stale_paused_gid_is_parked_for_same_path_redispatch(tmp_path):
    target = tmp_path / "archive.part1.rar"
    target.write_bytes(b"partial bytes")
    (tmp_path / "archive.part1.rar.aria2").write_bytes(b"aria2 control")
    row = {
        "file_id": 11,
        "torrent_id": 7,
        "download_id": "old-gid",
        "local_path": str(target),
        "source_url": "https://source.example/file",
        "download_url": "https://expired.example/generated",
        "transfer_source": "direct_link",
    }
    db = _FakeDb([row])
    coordinator = _bare_coordinator()

    with patch("services.restart_resume_control.is_builtin_mode", return_value=True), \
         patch("services.restart_resume_control.get_db", side_effect=lambda: _db_context(db)):
        result = await coordinator._stage_missing_paused_gids(7)

    assert result == {"recovered": 1, "resumable": 1}
    coordinator.confirm_gid.assert_awaited_once_with("old-gid")
    update_sql, params = next(
        (sql, params)
        for sql, params in db.executions
        if "SET download_id=NULL" in sql
    )
    assert "local_path" not in update_sql
    # Direct-link redispatch regenerates the capability from source_url instead
    # of retaining the expired generated URL. The canonical local_path is never
    # sent through a filename allocator.
    assert params == (None, 11, "old-gid")
    coordinator.manager._log_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_restart_recovery_keeps_physical_manifest_and_never_reallocates_path(tmp_path):
    target = tmp_path / "large-file.bin"
    target.write_bytes(b"partial bytes")
    (tmp_path / "large-file.bin.aria2").write_bytes(b"aria2 control")
    row = {
        "file_id": 21,
        "status": "paused",
        "download_id": "lost-gid",
        "local_path": str(target),
        "source_url": "https://source.example/original",
        "download_url": "https://expired.example/generated",
        "transfer_source": "direct_link",
    }
    db = _FakeDb([row])
    coordinator = _bare_coordinator(pause_intents={9})

    inherited = AsyncMock()
    with patch("services.restart_resume_control.is_builtin_mode", return_value=True), \
         patch("services.restart_resume_control.get_settings", return_value=SimpleNamespace(paused=False)), \
         patch("services.restart_resume_control.get_db", side_effect=lambda: _db_context(db)), \
         patch.object(MirrorAwareTransferControlCoordinator, "reset_for_redownload", new=inherited):
        result = await coordinator.reset_for_redownload(
            9,
            "aria2 entry lost (GID lost-gid) — reset for re-download on startup",
        )

    assert result is None
    inherited.assert_not_awaited()
    coordinator.confirm_gid.assert_awaited_once_with("lost-gid")

    file_update_sql, file_params = next(
        (sql, params)
        for sql, params in db.executions
        if "SET status=?, download_id=NULL" in sql
    )
    assert "local_path" not in file_update_sql
    assert file_params == ("paused", None, 21)
    assert not any("DELETE FROM download_files" in sql for sql, _ in db.executions)
    assert not any("local_path=" in sql for sql, _ in db.executions)

    event_sql, event_params = next(
        (sql, params)
        for sql, params in db.executions
        if "INSERT INTO events" in sql
    )
    assert "no filename reallocation" in event_params[2]


def test_resume_paths_preflight_missing_built_in_gids_before_inherited_resume():
    parent_source = inspect.getsource(
        RestartResumableTransferControlCoordinator._resume_parent
    )
    all_source = inspect.getsource(
        RestartResumableTransferControlCoordinator._resume_unintended_paused
    )
    assert "_stage_missing_paused_gids" in parent_source
    assert "super()._resume_parent" in parent_source
    assert "_stage_missing_paused_gids" in all_source
    assert "super()._resume_unintended_paused" in all_source


def test_restart_recovery_does_not_replace_clean_builtin_session_policy():
    source = inspect.getsource(
        RestartResumableTransferControlCoordinator.reset_for_redownload
    )
    assert "local_path" in source
    assert "download_id=NULL" in source
    assert "no filename reallocation" in source
    assert "DELETE FROM download_files" not in source
