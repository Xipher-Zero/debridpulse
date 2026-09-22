"""Managed-executor lifecycle/administration is registered generically.

Composition discovers integration-owned lifecycle components and
administration surfaces through one generic seam into the existing
``ApplicationService.lifecycle`` / ``admins`` owners; adding a managed
executor needs no concrete composition branch, and neutral runtime limits
never use an executor's administration surface.
"""
from __future__ import annotations

import inspect

import pytest

from application import composition
from application.service import ApplicationService
from executor_fakes import LedgerExecutor
from integrations.definition import AdministeredIntegration, IntegrationLifecycle, ManagedIntegration
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio


async def _allow(_handle, _action):
    return True


class Lifecycle:
    def __init__(self, log):
        self.log = log

    async def start(self):
        self.log.append("start")

    async def stop(self):
        self.log.append("stop")

    async def maintain(self):
        self.log.append("maintain")


class ManagedLedger(LedgerExecutor):
    def __init__(self, authorize, log, **kwargs):
        super().__init__(authorize, **kwargs)
        self.lifecycle = Lifecycle(log)
        self.administration = self.lifecycle


async def test_managed_executor_lifecycle_is_discovered_generically():
    log = []
    registry = IntegrationRegistry()
    managed = ManagedLedger(_allow, log)
    registry.register_executor(managed)
    registry.register_executor(LedgerExecutor(_allow, identity="unmanaged"))
    assert isinstance(managed, ManagedIntegration) and isinstance(managed, AdministeredIntegration)
    assert isinstance(managed.lifecycle, IntegrationLifecycle)
    lifecycle, admins = composition.integration_surfaces(registry)
    assert lifecycle == (managed.lifecycle,)
    assert admins == {"ledger-copy": managed.administration}


async def test_adding_fake_managed_executor_requires_no_application_composition_branch():
    source = inspect.getsource(composition)
    for forbidden in ("aria2", "Aria2", "from executors", "import executors", "Administration(",
                      "RuntimeConfiguration"):
        assert forbidden not in source
    log = []
    registry = IntegrationRegistry()
    registry.register_executor(ManagedLedger(_allow, log, identity="brand-new-managed"))
    lifecycle, admins = composition.integration_surfaces(registry)
    assert len(lifecycle) == 1 and set(admins) == {"brand-new-managed"}


async def test_application_lifecycle_remains_single_owner():
    log = []

    class Engine:
        repository = None

    service = ApplicationService(Engine(), lifecycle=(Lifecycle(log),))
    await service.start_integrations()
    await service.stop_integrations()
    assert log == ["start", "stop"]
    source = inspect.getsource(composition.configure)
    assert source.count("application.lifecycle =") == 1
    assert source.count("application.admins =") == 1
    assert not [name for name in vars(ApplicationService) if "lifecycle" in name.lower() and name != "lifecycle"
                and not name.startswith("_")]


async def test_neutral_runtime_limits_do_not_use_executor_admin_surface():
    from api import routes
    for handler in (routes.get_execution_runtime_limits, routes.patch_execution_runtime_limits):
        source = inspect.getsource(handler)
        assert "integration_admin" not in source and "admins" not in source
    service_source = inspect.getsource(ApplicationService)
    method = inspect.getsource(ApplicationService.execution_runtime_limits)
    assert "integration_admin" not in method and "admins" not in method
    assert "execution_runtime_limits" in service_source
