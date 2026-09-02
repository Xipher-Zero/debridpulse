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
from transfers.storage import DiskCapacity


def configure(application):
    settings = normalize_settings(get_settings(), definitions)
    apply_settings(settings)
    application.definitions = definitions
    registry = IntegrationRegistry()
    register(registry, settings, IntegrationEnvironment(application.repository, settings.download_folder))
    application.engine.registry = registry
    application.engine.root = settings.download_folder
    policy = settings.transfer_policy
    application.engine.configure_policy(replace(application.engine.policy,
        max_attempts=policy.execution_retry_count + 1,
        retry_delay=policy.execution_retry_delay_seconds,
        max_active_executions=policy.max_concurrent_executions,
        resolution_max_attempts=policy.resolution_retry_count + 1,
        resolution_retry_delay=policy.resolution_retry_delay_minutes * 60,
        resolution_concurrency=policy.resolution_concurrency,
        cleanup_after_completion=True,
        stalled_after_seconds=policy.stalled_timeout_hours * 3600,
        resource_poll_interval=policy.provider_poll_interval_seconds))
    application.capacity = DiskCapacity(settings.download_folder, settings.min_free_disk_gb, settings.disk_guard_resume_hysteresis_gb)
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
