from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import db.database as database
from application.service import ApplicationService
from fake_applicability_provider import SpecializedFixtureProvider
from integrations.runtime_state import ProviderRuntimeStateStore
from providers.alldebrid.host_runtime import AllDebridHostMaintenance
from providers.alldebrid.provider import AllDebridProvider
from providers.general_http.provider import GeneralHttpProvider
from transfers.applicability import ApplicabilityReadiness, ProviderApplicability
from transfers.engine import TransferEngine
from transfers.models import TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


SPECIALIZED_URL = "https://shared.example/file.bin"
GENERIC_URL = "https://ordinary.example/file.bin"


class CountingGeneralHttpProvider(GeneralHttpProvider):
    def __init__(self):
        self.calls = 0

    async def resolve(self, request):
        self.calls += 1
        return await super().resolve(request)


class FakeHostClient:
    def __init__(self):
        self.host_calls = 0

    async def get_user_hosts(self):
        self.host_calls += 1
        return {
            "hosts": {
                "shared": {
                    "name": "Shared",
                    "type": "premium",
                    "domains": ["shared.example"],
                    "regexps": [r"https?://shared\.example/.+"],
                    "status": True,
                }
            }
        }


def request(url):
    return TransferRequest("https", url, name="fixture.bin")


async def unresolved_runtime(tmp_path, monkeypatch, name):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / name)
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    specialized = SpecializedFixtureProvider("specialized", host="shared.example")
    specialized.applicability = ProviderApplicability(
        specialized=True,
        readiness=ApplicabilityReadiness.UNRESOLVED,
    )
    generic = CountingGeneralHttpProvider()
    registry.register_provider(specialized)
    registry.register_provider(generic)
    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "downloads"),
        policy=TransferPolicy(adoption_stability_seconds=0),
    )
    await engine.initialize()
    return engine, repository, specialized, generic


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "eventual_match", "expected_provider"),
    (
        (SPECIALIZED_URL, True, "specialized"),
        (GENERIC_URL, False, "general_http"),
    ),
)
async def test_early_and_late_submission_converge_on_same_route(
    tmp_path, monkeypatch, url, eventual_match, expected_provider
):
    engine, repository, specialized, generic = await unresolved_runtime(
        tmp_path, monkeypatch, f"timing-{eventual_match}.sqlite3"
    )

    early = await engine.submit((request(url),), deduplicate=False)
    early_record = (await repository.requests(early.id))[0]
    await engine.resolve_pending()

    assert early_record.id == (await repository.requests(early.id))[0].id
    assert await repository.bound_route_provider(early_record.id) is None
    assert specialized.calls == []
    assert generic.calls == 0

    if eventual_match:
        specialized.applicability = ProviderApplicability(
            specialized_hosts=specialized.applicability.specialized_hosts
            or SpecializedFixtureProvider(
                "claim-source", host="shared.example"
            ).applicability.specialized_hosts,
            specialized=True,
            readiness=ApplicabilityReadiness.READY,
        )
    else:
        specialized.applicability = ProviderApplicability(
            specialized=True,
            readiness=ApplicabilityReadiness.READY,
        )

    await engine.resolve_pending()
    early_provider = await repository.bound_route_provider(early_record.id)

    late = await engine.submit((request(url),), deduplicate=False)
    late_record = (await repository.requests(late.id))[0]
    await engine.resolve_pending()
    late_provider = await repository.bound_route_provider(late_record.id)

    assert early_provider == expected_provider
    assert late_provider == expected_provider
    assert early_provider == late_provider
    if eventual_match:
        assert generic.calls == 0
        assert len(specialized.calls) >= 2
    else:
        assert specialized.calls == []
        assert generic.calls >= 2


@pytest.mark.asyncio
async def test_unbound_pending_request_survives_restart_with_identity_and_no_phantom_route(
    tmp_path, monkeypatch
):
    engine, repository, specialized, generic = await unresolved_runtime(
        tmp_path, monkeypatch, "restart-pending.sqlite3"
    )
    transfer = await engine.submit((request(GENERIC_URL),), deduplicate=False)
    before = (await repository.requests(transfer.id))[0]

    await engine.resolve_pending()
    pending = (await repository.requests(transfer.id))[0]
    assert pending.id == before.id
    assert pending.state == "pending"
    assert pending.attempts == 0
    assert await repository.bound_route_provider(pending.id) is None
    assert specialized.calls == []
    assert generic.calls == 0

    restarted_repository = TransferRepository()
    restarted_registry = IntegrationRegistry()
    restarted_specialized = SpecializedFixtureProvider(
        "specialized", host="shared.example"
    )
    restarted_specialized.applicability = ProviderApplicability(
        specialized=True,
        readiness=ApplicabilityReadiness.READY,
    )
    restarted_generic = CountingGeneralHttpProvider()
    restarted_registry.register_provider(restarted_specialized)
    restarted_registry.register_provider(restarted_generic)
    restarted_engine = TransferEngine(
        restarted_repository,
        restarted_registry,
        download_root=str(tmp_path / "downloads"),
        policy=TransferPolicy(adoption_stability_seconds=0),
    )
    await restarted_engine.initialize()
    await restarted_engine.resolve_pending()

    after = (await restarted_repository.requests(transfer.id))[0]
    assert after.id == before.id
    assert await restarted_repository.bound_route_provider(after.id) == "general_http"
    assert restarted_specialized.calls == []
    assert restarted_generic.calls == 1


@pytest.mark.asyncio
async def test_provider_disablement_transition_wakes_blocked_resolution_without_refresh():
    class Engine:
        repository = object()

    application = ApplicationService(Engine())
    maintenance = AllDebridHostMaintenance(object())
    enabled = AllDebridProvider(client=object())
    maintenance.bind(
        enabled,
        initial=True,
        notify=application.notify_applicability_changed,
    )
    application.resolution_wakeup.clear()
    application.integration_wakeup.clear()

    disabled = AllDebridProvider(client=object())
    disabled.descriptor = replace(disabled.descriptor, enabled=False)
    maintenance.bind(disabled, notify=application.notify_applicability_changed)

    assert application.resolution_wakeup.is_set()
    assert application.integration_wakeup.is_set()


@pytest.mark.asyncio
async def test_reenable_without_retained_snapshot_is_unresolved_then_refreshes_immediately(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "reenable-empty.sqlite3")
    await database.init_db()
    store = ProviderRuntimeStateStore()
    await store.start()
    client = FakeHostClient()
    maintenance = AllDebridHostMaintenance(store)

    disabled = AllDebridProvider(client=client)
    disabled.descriptor = replace(disabled.descriptor, enabled=False)
    maintenance.bind(disabled, initial=True)
    await maintenance.start()

    enabled = AllDebridProvider(client=client)
    maintenance.bind(enabled)
    assert enabled.applicability.readiness == ApplicabilityReadiness.UNRESOLVED
    assert enabled.applicability.specialized_hosts == ()
    assert client.host_calls == 0

    await maintenance.maintain()
    assert client.host_calls == 1
    assert enabled.applicability.readiness == ApplicabilityReadiness.READY
    assert enabled.applicability.specialized_hosts


@pytest.mark.asyncio
async def test_resolution_wakeup_set_after_cycle_is_not_lost(monkeypatch):
    import core.scheduler as scheduler

    class Policy:
        resource_poll_interval = 3600

    class Engine:
        policy = Policy()

    class Runtime:
        def __init__(self):
            self.resolution_wakeup = asyncio.Event()
            self.engine = Engine()
            self.calls = 0

        def application_storage_permitted(self):
            return True

        async def resolve_pending(self):
            self.calls += 1
            if self.calls == 1:
                asyncio.get_running_loop().call_soon(self.resolution_wakeup.set)
                return
            raise asyncio.CancelledError

    runtime = Runtime()
    monkeypatch.setattr(scheduler, "application", runtime)

    with pytest.raises(asyncio.CancelledError):
        await scheduler.sync_status_loop()
    assert runtime.calls == 2


def test_ws3_uses_one_neutral_wakeup_and_no_readiness_specific_queue():
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    service = (backend / "application/service.py").read_text(encoding="utf-8")
    scheduler = (backend / "core/scheduler.py").read_text(encoding="utf-8")
    engine = (backend / "transfers/engine.py").read_text(encoding="utf-8")
    registry = (backend / "transfers/registry.py").read_text(encoding="utf-8")
    applicability = (backend / "transfers/applicability.py").read_text(encoding="utf-8")

    assert "resolution_wakeup" in service
    assert "resolution_wakeup" in scheduler
    assert "readiness_wait_queue" not in engine
    assert "alldebrid_pending_queue" not in engine
    assert "on_alldebrid_hosts_ready" not in scheduler
    assert 'provider_id == "alldebrid"' not in registry
    assert "providers.alldebrid" not in applicability
