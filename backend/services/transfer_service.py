"""DebridPulse application service root.

FastAPI and scheduler code depend on this object. The inherited TorrentManager is
retained as the V1 materialization implementation, but every application-visible
operation is explicit: there is no transparent fallback into the legacy engine.
"""
from __future__ import annotations

from services.direct_link_retry_guard import manager as engine
from services.provider_gateway import ProviderGateway
from services.transfer_repository import TransferRepository
from services.aria2_gateway import Aria2Gateway
from services.aria2_error_recovery import Aria2ErrorRecovery
from services.ownership_ledger import OwnershipLedger
from services.transfer_state_machine import TransferStateMachine
from services.transfer_control_service import TransferControlService
from services.dispatch_coordinator import DispatchCoordinator
from services.reconciliation_service import ReconciliationService
from services.extraction_service import ExtractionService
from services.notification_service import NotificationService
from services.maintenance_gate import ApplicationMaintenanceGate


class TransferService:
    def __init__(self, materialization_engine):
        self._engine = materialization_engine
        self.repository = TransferRepository()
        self.provider = ProviderGateway(materialization_engine)
        self.ownership = OwnershipLedger(materialization_engine)
        self.aria2_error_recovery = Aria2ErrorRecovery(
            materialization_engine, self.ownership
        )
        self.aria2 = Aria2Gateway(
            materialization_engine,
            self.ownership,
            self.aria2_error_recovery,
        )
        self.state_machine = TransferStateMachine(materialization_engine, self.repository)
        self.control = TransferControlService(materialization_engine, self.repository, self.state_machine)
        self.state_machine.bind_control(self.control)
        self.dispatch = DispatchCoordinator(materialization_engine, self.control, self.ownership)
        self.reconciliation = ReconciliationService(
            materialization_engine,
            self.repository,
            self.control,
            self.dispatch,
            self.ownership,
            self.aria2_error_recovery,
        )
        self.extraction = ExtractionService()
        self.notifications = NotificationService()
        self._application_maintenance = ApplicationMaintenanceGate()
        materialization_engine.bind_architecture(self)

    # ----- operator control -----

    async def pause_torrent(self, transfer_id: int):
        async with self._application_maintenance.operation():
            return await self.control.pause_transfer(transfer_id)

    async def resume_torrent(self, transfer_id: int):
        async with self._application_maintenance.operation():
            return await self.control.resume_transfer(transfer_id)

    async def pause_all_downloads(self):
        async with self._application_maintenance.operation():
            return await self.control.pause_all()

    async def resume_all_downloads(self):
        async with self._application_maintenance.operation():
            return await self.control.resume_all()

    async def control_aria2_gid(self, *args, **kwargs):
        async with self._application_maintenance.operation():
            return await self.control.control_gid(*args, **kwargs)

    async def owned_aria2_downloads(self, downloads):
        return await self.ownership.filter_owned(downloads)

    # ----- provider boundary -----

    async def sync_alldebrid_status(self):
        async with self._application_maintenance.operation():
            return await self.provider.sync_status()

    async def reconcile_provider_inventory(self):
        async with self._application_maintenance.operation():
            return await self.provider.reconcile_inventory()

    async def import_existing_magnets(self):
        async with self._application_maintenance.operation():
            return await self.provider.import_existing()

    async def full_alldebrid_sync(self):
        async with self._application_maintenance.operation():
            return await self.provider.full_sync()

    async def add_magnet_direct(self, magnet: str, source: str = "manual"):
        async with self._application_maintenance.operation():
            return await self.provider.add_magnet(magnet, source=source)

    async def add_torrent_file_direct(self, *args, **kwargs):
        async with self._application_maintenance.operation():
            return await self.provider.add_torrent_file(*args, **kwargs)

    async def add_direct_links(self, links):
        async with self._application_maintenance.operation():
            return await self.provider.add_direct_links(links)

    async def retry_direct_link_collection(self, transfer_id: int):
        async with self._application_maintenance.operation():
            return await self.provider.retry_direct_link_collection(transfer_id)

    async def cleanup_no_peer_errors(self):
        async with self._application_maintenance.operation():
            return await self.provider.cleanup_no_peer_errors()

    async def cleanup_alldebrid_orphans(self):
        async with self._application_maintenance.operation():
            return await self.provider.cleanup_orphans()

    async def cleanup_stuck_downloads(self):
        async with self._application_maintenance.operation():
            return await self.provider.cleanup_stuck()

    # ----- aria2 observation/maintenance -----

    async def advance_aria2_queue(self):
        async with self._application_maintenance.operation():
            return await self.aria2.advance_queue()

    async def apply_aria2_memory_tuning(self):
        async with self._application_maintenance.operation():
            return await self.aria2.apply_memory_tuning()

    async def test_aria2(self):
        return await self.aria2.test()

    async def _aria2_get_memory_diagnostics(self):
        """Compatibility name for the existing runtime diagnostics route."""
        return await self.aria2.memory_diagnostics()

    async def run_aria2_housekeeping(self):
        async with self._application_maintenance.operation():
            return await self.aria2.housekeeping()

    async def deep_sync_aria2_finished(self):
        async with self._application_maintenance.operation():
            return await self.aria2.deep_sync()

    async def check_disk_space_guard(self):
        async with self._application_maintenance.operation():
            return await self.aria2.disk_guard()

    # ----- explicit materialization/lifecycle compatibility -----

    async def _start_download(self, *args, **kwargs):
        """Existing startup recovery entrypoint routed through control authority."""
        async with self._application_maintenance.operation():
            return await self.control.start_download(*args, **kwargs)

    async def delete_torrent(self, *args, **kwargs):
        """Delete remains a materialization-engine operation in V1, explicitly exposed."""
        async with self._application_maintenance.operation():
            return await self._engine.delete_torrent(*args, **kwargs)

    def application_operation(self):
        """Admit one state-changing application operation unless maintenance owns admission."""
        return self._application_maintenance.operation()

    def database_wipe_admission(self):
        """Close application mutation/execution admission for destructive maintenance."""
        return self._application_maintenance.maintenance()

    async def quiesce_for_database_wipe(self):
        """Quiesce provider, materialization and owned aria2 work before DB wipe."""
        self._engine.set_materialization_quiescing(True)
        provider_quiesced = False
        try:
            await self.provider.begin_quiescence()
            provider_quiesced = True
            pause_result = await self.pause_all_downloads()
            failed = int((pause_result or {}).get("failed") or 0)
            if failed:
                raise RuntimeError(f"Could not confirm pause for {failed} transfer(s)")
            await self._engine.wait_for_materialization_idle()
            await self.aria2.test()
            owned = await self.aria2.get_owned()
            live = [item for item in owned if item.status in {"active", "waiting"}]
            if live:
                raise RuntimeError(
                    f"Database wipe refused: {len(live)} owned aria2 job(s) are still live"
                )
            return {
                "pause": pause_result,
                "owned_checked": len(owned),
                "provider_operations_drained": True,
                "materialization_drained": True,
            }
        except BaseException:
            if provider_quiesced:
                await self.provider.end_quiescence()
            self._engine.set_materialization_quiescing(False)
            raise

    async def release_database_wipe_quiescence(self):
        await self.provider.end_quiescence()
        self._engine.set_materialization_quiescing(False)

    def reset_services(self):
        self.control.reset_runtime_state()
        return self._engine.reset_services()


transfer_service = TransferService(engine)
