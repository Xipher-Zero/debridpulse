"""Regression tests for durable pause/resume semantics through v1.0.5 services."""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from executors.aria2.client import Aria2DownloadStatus, Aria2RPCError
from services.reconciliation_service import ReconciliationService
from services.transfer_control import TransferControlCoordinator
from services.transfer_control_service import TransferControlService
from services.transfer_service import transfer_service

# Tests must patch the engine the application actually bound, not the legacy
# compatibility singleton left exported by manager_v2.
manager = transfer_service._engine


def _status(gid: str, status: str) -> Aria2DownloadStatus:
    return Aria2DownloadStatus(gid=gid, status=status, total_length=100,
                               completed_length=25, download_speed=1, files=[])


def _coordinator() -> TransferControlCoordinator:
    return transfer_service.control.coordinator


def test_reliability_layer_is_explicitly_bound_without_runtime_patching():
    coordinator = _coordinator()
    assert isinstance(transfer_service.control, TransferControlService)
    assert manager._architecture is transfer_service
    assert not hasattr(manager, "_dp_transfer_control")
    with pytest.raises(RuntimeError, match="Runtime method patching was removed"):
        coordinator.install()


@pytest.mark.asyncio
async def test_pause_rpc_failure_is_not_reported_as_success():
    coordinator = _coordinator()
    fake = SimpleNamespace(tell_status=AsyncMock(return_value=_status("g1", "active")),
                           _call=AsyncMock(side_effect=Aria2RPCError("pause rejected")))
    with patch.object(manager, "aria2", return_value=fake):
        with pytest.raises(Aria2RPCError, match="pause rejected"):
            await coordinator._strict_pause_gid("g1")
    fake._call.assert_awaited_once_with("aria2.pause", ["g1"])


@pytest.mark.asyncio
async def test_resume_rpc_failure_is_not_reported_as_success():
    coordinator = _coordinator()
    fake = SimpleNamespace(tell_status=AsyncMock(return_value=_status("g2", "paused")),
                           _call=AsyncMock(side_effect=Aria2RPCError("resume rejected")))
    with patch.object(manager, "aria2", return_value=fake):
        with pytest.raises(Aria2RPCError, match="resume rejected"):
            await coordinator._strict_resume_gid("g2")
    fake._call.assert_awaited_once_with("aria2.unpause", ["g2"])


@pytest.mark.asyncio
async def test_missing_gid_requires_repeated_explicit_not_found():
    coordinator = _coordinator()
    fake = SimpleNamespace(tell_status=AsyncMock(side_effect=[
        Aria2RPCError("aria2 [-1]: GID#g3 is not found"),
        Aria2RPCError("aria2 [-1]: GID#g3 is not found"),
        Aria2RPCError("aria2 [-1]: GID#g3 is not found"),
    ]))
    with patch.object(manager, "aria2", return_value=fake), \
         patch("services.transfer_control.asyncio.sleep", new=AsyncMock()) as sleep:
        result = await coordinator.confirm_gid("g3", attempts=3, delay=.01)
    assert result is None
    assert fake.tell_status.await_count == 3
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_non_missing_rpc_error_never_becomes_missing_gid():
    coordinator = _coordinator()
    fake = SimpleNamespace(tell_status=AsyncMock(
        side_effect=Aria2RPCError("aria2 [1]: unauthorized request")))
    with patch.object(manager, "aria2", return_value=fake):
        with pytest.raises(Aria2RPCError, match="unauthorized"):
            await coordinator.confirm_gid("g-auth")
    assert fake.tell_status.await_count == 1


@pytest.mark.asyncio
async def test_unexpected_aria2_state_is_not_claimed_paused():
    coordinator = _coordinator()
    fake = SimpleNamespace(tell_status=AsyncMock(return_value=_status("g4", "unknown")),
                           _call=AsyncMock())
    with patch.object(manager, "aria2", return_value=fake):
        with pytest.raises(Aria2RPCError, match="cannot be paused"):
            await coordinator._strict_pause_gid("g4")
    fake._call.assert_not_awaited()


def test_selective_pause_blocks_provider_ready_scheduling_immediately():
    coordinator = _coordinator()
    torrent_id = 987654321
    coordinator._pause_intents.add(torrent_id)
    try:
        assert coordinator.schedule_ready_parent(torrent_id, "ad-id", "item") is False
    finally:
        coordinator._pause_intents.discard(torrent_id)


@pytest.mark.asyncio
async def test_pause_arriving_during_magnet_materialization_is_reapplied():
    coordinator = _coordinator()
    torrent_id = 987654322
    original = coordinator._orig_download

    async def materialize(*_args):
        coordinator._pause_intents.add(torrent_id)

    try:
        coordinator._pause_intents.discard(torrent_id)
        coordinator._orig_download = AsyncMock(side_effect=materialize)
        with patch.object(coordinator, "_pause_parent", new=AsyncMock(return_value={})) as pause:
            await coordinator.download(torrent_id, "ad-id", "magnet item")
        pause.assert_awaited_once_with(torrent_id, strict=False)
    finally:
        coordinator._orig_download = original
        coordinator._pause_intents.discard(torrent_id)


def test_individual_resume_global_pause_semantics_live_in_control_service():
    source = inspect.getsource(TransferControlService.resume_transfer)
    assert "_persist_transition" in source
    assert "_set_global_paused(False)" in source
    assert "_set_global_paused(True)" in source
    assert "_schedule_queue()" in source


def test_dispatcher_grandfathers_over_limit_jobs_instead_of_removing_gids():
    source = inspect.getsource(TransferControlCoordinator.dispatch_queue)
    assert "grandfathering them without removing GIDs" in source
    assert "_remove_owned_aria2_gid" not in source
    assert "_orig_dispatch(snapshot)" in source


def test_provider_source_is_preserved_before_generated_url_overwrite():
    source = inspect.getsource(TransferControlCoordinator._preserve_pending_sources)
    assert "SET source_url=download_url" in source
    assert "t.source!='direct_link'" in source
    assert "f.status='pending'" in source


def test_queue_refill_is_deferred_outside_operator_request_path():
    advance = inspect.getsource(TransferControlCoordinator.advance_queue_locked)
    sync = inspect.getsource(TransferControlCoordinator.sync_clients)
    assert "_schedule_queue()" in advance
    assert "_orig_dispatch" not in advance
    assert "await self.manager.sync_aria2_downloads()" in sync
    assert "self._schedule_queue()" in sync


def test_lost_gid_recovery_preserves_completed_siblings_when_source_known():
    source = inspect.getsource(TransferControlCoordinator.reset_for_redownload)
    assert "without rebuilding completed siblings" in source
    assert "download_id=NULL" in source
    assert "DELETE FROM download_files" not in source
    assert "strikes < 2" in source


def test_reconciliation_confirms_gid_before_caching_missing_state():
    source = inspect.getsource(ReconciliationService.confirm_gid)
    assert "await self.control.confirm_gid" in source
    assert "self.confirmed_missing.add" in source
    assert "aria2.confirm_gid_cache_hits" in source


def test_pause_intent_table_keeps_resume_tombstone_for_restart_semantics():
    source = inspect.getsource(TransferControlCoordinator._set_intent)
    init = inspect.getsource(TransferControlCoordinator.ensure_initialized)
    assert "paused=excluded.paused" in source
    assert "p.torrent_id IS NULL" in init
    assert "TIMESTAMP DEFAULT CURRENT_TIMESTAMP" in init
