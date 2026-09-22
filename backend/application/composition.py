"""Production integration composition. Concrete imports terminate here."""
from dataclasses import replace

from application.consolidation_events import ConsolidationEvents
from application.service import ApplicationService
from core.config import get_settings, apply_settings
from integrations.catalog import definitions, register
from integrations.configuration import normalize_settings
from integrations.definition import (
    AdministeredIntegration, IntegrationEnvironment, IntegrationLifecycle, ManagedIntegration,
)
from integrations.runtime_state import ProviderRuntimeStateStore
from transfers.convergence_engine import TransferEngine
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.recovery_repository import TransferRepository
from transfers.storage import DiskCapacity, register_storage_health


def integration_surfaces(registry) -> tuple[tuple, dict]:
    """Generic discovery of integration-owned lifecycle components and
    administration surfaces -- the one seam through which a managed
    integration participates in the application lifecycle. No integration is
    named here; adding one needs no composition change."""
    implementations = (*registry.providers.values(), *registry.executors.values())
    lifecycle = tuple(item.lifecycle for item in implementations
                      if isinstance(item, ManagedIntegration) and isinstance(item.lifecycle, IntegrationLifecycle))
    admins = {item.descriptor.id: item.administration for item in implementations
              if isinstance(item, AdministeredIntegration)}
    return lifecycle, admins


def configure(application):
    settings = normalize_settings(get_settings(), definitions)
    apply_settings(settings)
    application.definitions = definitions
    registry = IntegrationRegistry()
    register(registry, settings, IntegrationEnvironment(application.repository, settings.download_folder,
                                                        commands=application))
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
    from db import database
    capacity = getattr(application, "capacity", None)
    if isinstance(capacity, DiskCapacity):
        capacity.configure(settings.download_folder, settings.min_free_disk_gb,
            settings.disk_guard_resume_hysteresis_gb, application_path=database.DB_PATH)
    else:
        capacity = DiskCapacity(settings.download_folder, settings.min_free_disk_gb,
            settings.disk_guard_resume_hysteresis_gb, application_path=database.DB_PATH)
        application.capacity = capacity
    register_storage_health(capacity)
    initial_health = capacity.check()
    application.engine.dispatch_permitted = capacity.application_storage_permitted and not initial_health["active"]
    application.execution_poll_interval = policy.execution_poll_interval_seconds
    from postprocessors.archive.processor import ArchivePostProcessor
    application.engine.postprocessors = (ArchivePostProcessor(),) if settings.extract_enabled else ()
    from transfers.runtime_limits import ExecutionRuntimeLimits
    # The canonical global runtime limit is injected into its one core owner;
    # executors receive only the ceiling that owner assigns them.
    limits = settings.execution_runtime_limits or ExecutionRuntimeLimits()
    application.engine.configure_runtime_limits(limits.max_download_bytes_per_second)
    integration_lifecycle, admins = integration_surfaces(registry)
    application.admins = admins
    runtime_state = getattr(application, "runtime_state", None)
    if runtime_state is None:
        runtime_state = ProviderRuntimeStateStore()
        application.runtime_state = runtime_state

    from providers.alldebrid.host_runtime import AllDebridHostMaintenance
    from providers.alldebrid.runtime_state import AllDebridRuntimeStateStore, credential_scope
    alldebrid_options = settings.integrations["alldebrid"].options
    host_scope = credential_scope(alldebrid_options.get("api_key", ""))
    previous_scope = getattr(application, "alldebrid_host_scope", None)
    host_maintenance = getattr(application, "alldebrid_host_maintenance", None)
    initial_host_binding = host_maintenance is None or previous_scope != host_scope
    if initial_host_binding:
        host_maintenance = AllDebridHostMaintenance(
            AllDebridRuntimeStateStore(runtime_state, host_scope)
        )
        application.alldebrid_host_maintenance = host_maintenance
        application.alldebrid_host_scope = host_scope
    host_maintenance.bind(registry.providers.get("alldebrid"), initial=initial_host_binding,
        notify=application.notify_applicability_changed)

    application.lifecycle = (runtime_state, host_maintenance, *integration_lifecycle)
    from application.observability import Observability
    application.observability = Observability(application.repository, application.consolidation_events)


def compose():
    settings = get_settings()
    repository = TransferRepository()
    engine = TransferEngine(repository, IntegrationRegistry(), download_root=settings.download_folder, policy=TransferPolicy())
    consolidation_events = ConsolidationEvents(repository)
    # The canonical owner announces a committed attachment through an injected
    # callback; nothing wraps or proxies it.
    engine.canonical.on_attached = consolidation_events.stage
    service = ApplicationService(engine, configure=configure)
    service.consolidation_events = consolidation_events
    configure(service)
    return service


application = compose()
