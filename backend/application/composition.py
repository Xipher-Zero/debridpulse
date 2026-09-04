"""Production integration composition. Concrete imports terminate here."""
from dataclasses import replace

from application.service import ApplicationService
from core.config import get_settings, save_settings, apply_settings
from integrations.catalog import definitions, register
from integrations.configuration import normalize_settings
from integrations.definition import IntegrationEnvironment
from integrations.runtime_state import ProviderRuntimeStateStore
from transfers.engine import TransferEngine
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository
from transfers.storage import DiskCapacity, register_storage_health


def configure(application):
    settings = normalize_settings(get_settings(), definitions)
    apply_settings(settings)
    application.definitions = definitions
    registry = IntegrationRegistry()
    register(registry, settings, IntegrationEnvironment(application.repository, settings.download_folder))
    application.engine.registry = registry
    application.engine.root = settings.download_folder
    policy = settings.transfer_policy

    def contain_local_resource_failure(error):
        """Bridge neutral local-resource failures into canonical storage health."""
        fault = application._record_download_storage_fault(error)
        if fault is None:
            return False
        application.engine.dispatch_permitted = False
        return True

    application.engine.configure_policy(replace(application.engine.policy,
        max_attempts=policy.execution_retry_count + 1,
        retry_delay=policy.execution_retry_delay_seconds,
        max_active_executions=policy.max_concurrent_executions,
        resolution_max_attempts=policy.resolution_retry_count + 1,
        resolution_retry_delay=policy.resolution_retry_delay_minutes * 60,
        resolution_concurrency=policy.resolution_concurrency,
        cleanup_after_completion=True,
        stalled_after_seconds=policy.stalled_timeout_hours * 3600,
        resource_poll_interval=policy.provider_poll_interval_seconds,
        local_resource_failure_handler=contain_local_resource_failure))
    # DiskCapacity is the canonical storage owner. Reconfigure the existing
    # instance so transition identity is not replaced on every Settings apply.
    from db import database
    capacity = getattr(application, "capacity", None)
    if isinstance(capacity, DiskCapacity):
        capacity.configure(
            settings.download_folder,
            settings.min_free_disk_gb,
            settings.disk_guard_resume_hysteresis_gb,
            application_path=database.DB_PATH,
        )
    else:
        capacity = DiskCapacity(
            settings.download_folder,
            settings.min_free_disk_gb,
            settings.disk_guard_resume_hysteresis_gb,
            application_path=database.DB_PATH,
        )
        application.capacity = capacity
    register_storage_health(capacity)
    # Establish real initial state immediately; the periodic guard owns later
    # recovery probes. This probe does not write to either filesystem.
    initial_health = capacity.check()
    application.engine.dispatch_permitted = capacity.application_storage_permitted and not initial_health["active"]
    application.execution_poll_interval = policy.execution_poll_interval_seconds
    from postprocessors.archive.processor import ArchivePostProcessor
    application.engine.postprocessors = (ArchivePostProcessor(),) if settings.extract_enabled else ()
    from executors.aria2.admin import Aria2Administration
    administration = Aria2Administration(registry.executors["aria2"], application.repository, application)
    application.admins = {"aria2": administration}
    runtime_state = getattr(application, "runtime_state", None)
    if runtime_state is None:
        runtime_state = ProviderRuntimeStateStore()
        application.runtime_state = runtime_state

    # AllDebrid's supported-host inventory is provider maintenance, not request
    # routing. Keep one coordinator across registry rebuilds so enable/disable
    # transitions are observed while each new provider instance is rebound.
    from providers.alldebrid.host_runtime import AllDebridHostMaintenance
    host_maintenance = getattr(application, "alldebrid_host_maintenance", None)
    initial_host_binding = host_maintenance is None
    if host_maintenance is None:
        host_maintenance = AllDebridHostMaintenance(runtime_state)
        application.alldebrid_host_maintenance = host_maintenance
    host_maintenance.bind(
        registry.providers.get("alldebrid"),
        initial=initial_host_binding,
        notify=application.notify_applicability_changed,
    )

    application.lifecycle = (runtime_state, host_maintenance, administration)
    from application.observability import Observability
    application.observability = Observability(application.repository)


def compose():
    settings = get_settings()
    repository = TransferRepository()
    engine = TransferEngine(repository, IntegrationRegistry(), download_root=settings.download_folder, policy=TransferPolicy())
    def pause_changed(paused):
        settings = get_settings().model_copy(update={"paused": paused})
        save_settings(settings)
        apply_settings(settings)
    service = ApplicationService(engine, configure=configure, pause_changed=pause_changed)
    configure(service)
    return service


application = compose()
