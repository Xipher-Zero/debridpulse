from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

import db.database as database
from application.service import ApplicationService
from fake_applicability_provider import SpecializedFixtureProvider
from integrations.runtime_state import ProviderRuntimeStateStore
from providers.alldebrid.host_runtime import (
    AllDebridHostMaintenance,
    HOST_REFRESH_RETRY_SECONDS,
    HOST_REFRESH_SECONDS,
    HOST_SCHEMA_VERSION,
    HOST_STATE_KEY,
    encode_host_snapshot,
    parse_native_host_snapshot,
)
from providers.alldebrid.provider import AllDebridProvider
from providers.general_http.provider import GeneralHttpProvider
from transfers.applicability import (
    ApplicabilityClass,
    ApplicabilityReadiness,
    ApplicabilityUnresolved,
    HostClaim,
    HostClaimScope,
    ProviderApplicability,
    ProviderApplicabilityInput,
    assess_provider_applicability,
)
from transfers.engine import TransferEngine
from transfers.models import TransferRequest
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


URL = "https://shared.example/file.bin"
GENERIC_URL = "https://ordinary.example/file.bin"


class CountingGeneralHttpProvider(GeneralHttpProvider):
    def __init__(self, *, enabled: bool = True):
        self.calls = 0
        self.descriptor = replace(self.descriptor, enabled=enabled)

    async def resolve(self, request):
        self.calls += 1
        return await super().resolve(request)


class Clock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


class FakeHostClient:
    def __init__(self, response=None):
        self.response = response if response is not None else native_hosts()
        self.error = None
        self.host_calls = 0

    async def get_user_hosts(self):
        self.host_calls += 1
        if self.error is not None:
            raise self.error
        return self.response


def native_hosts():
    return {
        "hosts": {
            "shared": {
                "name": "Shared",
                "type": "premium",
                "domains": ["shared.example"],
                "regexps": [r"https?://shared\.example/.+"],
                "status": True,
                "quota": 10,
                "quotaMax": 100,
                "quotaType": "traffic",
                "limitSimuDl": 2,
            }
        }
    }


def readiness_input(
    identity: str,
    *,
    readiness=ApplicabilityReadiness.READY,
    claims=(),
    enabled=True,
    specialized=True,
):
    return ProviderApplicabilityInput(
        identity,
        frozenset({"http", "https"}),
        enabled,
        ProviderApplicability(
            specialized_hosts=tuple(claims),
            specialized=specialized,
            readiness=readiness,
        ),
    )


def generic_input(identity="generic"):
    return ProviderApplicabilityInput(
        identity,
        frozenset({"http", "https"}),
        True,
        ProviderApplicability(generic_schemes=frozenset({"http", "https"})),
    )


def request(url=URL):
    return TransferRequest("https", url, name="fixture.bin")


def ready_claim():
    return HostClaim(
        "shared.example",
        HostClaimScope.EXACT,
        frozenset({"https"}),
    )


def test_neutral_readiness_is_distinct_from_match_class():
    unresolved = assess_provider_applicability(
        request(),
        (
            readiness_input(
                "specialized",
                readiness=ApplicabilityReadiness.UNRESOLVED,
            ),
            generic_input(),
        ),
    )
    assert unresolved.matches == ()
    assert unresolved.unresolved_specialized == ("specialized",)

    ready_match = assess_provider_applicability(
        request(),
        (
            readiness_input("specialized", claims=(ready_claim(),)),
            generic_input(),
        ),
    )
    assert [(item.provider_id, item.classification) for item in ready_match.matches] == [
        ("specialized", ApplicabilityClass.SPECIALIZED)
    ]
    assert ready_match.unresolved_specialized == ()

    ready_no_match = assess_provider_applicability(
        request(GENERIC_URL),
        (
            readiness_input("specialized"),
            generic_input(),
        ),
    )
    assert [(item.provider_id, item.classification) for item in ready_no_match.matches] == [
        ("generic", ApplicabilityClass.GENERIC)
    ]
    assert ready_no_match.unresolved_specialized == ()


def test_multiple_specialized_readiness_suppresses_only_premature_generic_fallback():
    assessment = assess_provider_applicability(
        request(GENERIC_URL),
        (
            readiness_input("ready-specialized"),
            readiness_input(
                "unresolved-specialized",
                readiness=ApplicabilityReadiness.UNRESOLVED,
            ),
            generic_input(),
        ),
    )
    assert assessment.matches == ()
    assert assessment.unresolved_specialized == ("unresolved-specialized",)

    disabled = assess_provider_applicability(
        request(GENERIC_URL),
        (
            readiness_input("ready-specialized"),
            readiness_input(
                "unresolved-specialized",
                readiness=ApplicabilityReadiness.UNRESOLVED,
                enabled=False,
            ),
            generic_input(),
        ),
    )
    assert [item.provider_id for item in disabled.matches] == ["generic"]
    assert disabled.unresolved_specialized == ()


def test_authoritative_specialized_match_can_use_existing_same_class_policy():
    assessment = assess_provider_applicability(
        request(),
        (
            readiness_input("matched", claims=(ready_claim(),)),
            readiness_input(
                "unresolved",
                readiness=ApplicabilityReadiness.UNRESOLVED,
            ),
            generic_input(),
        ),
    )
    assert [item.provider_id for item in assessment.matches] == ["matched"]
    assert assessment.unresolved_specialized == ("unresolved",)


def test_registry_exposes_unresolved_as_neutral_selection_signal():
    specialized = SpecializedFixtureProvider("specialized", host="shared.example")
    specialized.applicability = ProviderApplicability(
        specialized=True,
        readiness=ApplicabilityReadiness.UNRESOLVED,
    )
    generic = CountingGeneralHttpProvider()
    registry = IntegrationRegistry()
    registry.register_provider(specialized)
    registry.register_provider(generic)

    assert registry.eligible_providers(request()) == ()
    with pytest.raises(ApplicabilityUnresolved) as caught:
        registry.provider_for(request())
    assert caught.value.provider_ids == ("specialized",)


async def engine_runtime(tmp_path, monkeypatch, name):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / name)
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    specialized = SpecializedFixtureProvider(
        "specialized",
        host="shared.example",
        scope=HostClaimScope.EXACT,
    )
    ready = replace(
        specialized.applicability,
        specialized=True,
        readiness=ApplicabilityReadiness.READY,
    )
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
    return engine, repository, registry, specialized, generic, ready


@pytest.mark.asyncio
async def test_unresolved_request_stays_same_pending_request_without_phantom_history(
    tmp_path, monkeypatch
):
    engine, repository, _registry, specialized, generic, ready = await engine_runtime(
        tmp_path, monkeypatch, "pending.sqlite3"
    )
    transfer = await engine.submit((request(),), deduplicate=False)
    before = (await repository.requests(transfer.id))[0]

    await engine.resolve_pending()

    pending = (await repository.requests(transfer.id))[0]
    presentation = await repository.presentation(transfer.id, details=True)
    assert pending.id == before.id
    assert pending.state == "pending"
    assert pending.attempts == 0
    assert pending.error is None
    assert await repository.bound_route_provider(pending.id) is None
    assert presentation["route_attempts"] == []
    assert presentation["execution_attempts"] == []
    assert await repository.artifacts(transfer.id) == ()
    assert specialized.calls == []
    assert generic.calls == 0

    specialized.applicability = ready
    await engine.resolve_pending()

    resolved = (await repository.requests(transfer.id))[0]
    presentation = await repository.presentation(transfer.id, details=True)
    assert resolved.id == before.id
    assert len(specialized.calls) == 1
    assert generic.calls == 0
    assert presentation["route_attempts"][0]["provider_id"] == "specialized"


@pytest.mark.asyncio
async def test_unresolved_then_ready_no_match_routes_generic_same_request(
    tmp_path, monkeypatch
):
    engine, repository, _registry, specialized, generic, _ready = await engine_runtime(
        tmp_path, monkeypatch, "generic.sqlite3"
    )
    transfer = await engine.submit((request(GENERIC_URL),), deduplicate=False)
    identity = (await repository.requests(transfer.id))[0].id

    await engine.resolve_pending()
    assert generic.calls == 0

    specialized.applicability = ProviderApplicability(
        specialized=True,
        readiness=ApplicabilityReadiness.READY,
    )
    await engine.resolve_pending()

    record = (await repository.requests(transfer.id))[0]
    presentation = await repository.presentation(transfer.id, details=True)
    assert record.id == identity
    assert generic.calls == 1
    assert specialized.calls == []
    assert presentation["route_attempts"][0]["provider_id"] == "general_http"


@pytest.mark.asyncio
async def test_disabling_unresolved_specialized_provider_removes_readiness_barrier(
    tmp_path, monkeypatch
):
    engine, repository, _registry, specialized, generic, _ready = await engine_runtime(
        tmp_path, monkeypatch, "disable.sqlite3"
    )
    transfer = await engine.submit((request(GENERIC_URL),), deduplicate=False)
    identity = (await repository.requests(transfer.id))[0].id
    await engine.resolve_pending()
    assert generic.calls == 0

    specialized.descriptor = replace(specialized.descriptor, enabled=False)
    await engine.resolve_pending()

    record = (await repository.requests(transfer.id))[0]
    assert record.id == identity
    assert generic.calls == 1


@pytest.mark.asyncio
async def test_bound_route_survives_restart_when_applicability_becomes_unresolved(
    tmp_path, monkeypatch
):
    engine, repository, registry, specialized, _generic, ready = await engine_runtime(
        tmp_path, monkeypatch, "bound.sqlite3"
    )
    specialized.applicability = ready
    transfer = await engine.submit((request(),), deduplicate=False)
    await engine.resolve_pending()
    record = (await repository.requests(transfer.id))[0]
    assert await repository.bound_route_provider(record.id) == "specialized"

    specialized.applicability = ProviderApplicability(
        specialized=True,
        readiness=ApplicabilityReadiness.UNRESOLVED,
    )
    restarted_repository = TransferRepository()
    await restarted_repository.initialize()
    assert await restarted_repository.bound_route_provider(record.id) == "specialized"
    assert registry.provider_for_bound_route("specialized", record.request) is specialized


async def runtime_store(tmp_path, monkeypatch, name):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / name)
    await database.init_db()
    store = ProviderRuntimeStateStore()
    await store.start()
    return store


@pytest.mark.asyncio
async def test_alldebrid_no_snapshot_is_unresolved_then_refresh_publishes_ready(
    tmp_path, monkeypatch
):
    store = await runtime_store(tmp_path, monkeypatch, "hosts-unresolved.sqlite3")
    clock = Clock()
    client = FakeHostClient()
    provider = AllDebridProvider(client=client)
    changes = []
    maintenance = AllDebridHostMaintenance(store, clock=clock)
    maintenance.bind(provider, initial=True, notify=changes.append)

    await maintenance.start()
    assert provider.applicability.readiness == ApplicabilityReadiness.UNRESOLVED
    assert provider.applicability.specialized is True
    assert client.host_calls == 0

    await maintenance.maintain()
    assert client.host_calls == 1
    assert provider.applicability.readiness == ApplicabilityReadiness.READY
    assert provider.applicability.specialized_hosts
    assert "alldebrid" in changes


@pytest.mark.asyncio
async def test_alldebrid_stale_valid_lkg_is_ready_and_survives_refresh_failure(
    tmp_path, monkeypatch
):
    store = await runtime_store(tmp_path, monkeypatch, "hosts-lkg.sqlite3")
    clock = Clock()
    snapshot = parse_native_host_snapshot(native_hosts())
    await store.replace(
        "alldebrid",
        encode_host_snapshot(snapshot),
        schema_version=HOST_SCHEMA_VERSION,
        state_key=HOST_STATE_KEY,
        observed_at=clock(),
        successful_at=clock(),
        stale_after=clock() - 1,
        expected_generation=0,
    )
    client = FakeHostClient()
    client.error = RuntimeError("offline")
    provider = AllDebridProvider(client=client)
    maintenance = AllDebridHostMaintenance(store, clock=clock)
    maintenance.bind(provider, initial=True)

    await maintenance.start()
    assert client.host_calls == 0
    assert provider.applicability.readiness == ApplicabilityReadiness.READY
    assert provider.applicability.specialized_hosts

    await maintenance.maintain()
    assert client.host_calls == 1
    assert provider.applicability.readiness == ApplicabilityReadiness.READY
    assert provider.applicability.specialized_hosts


@pytest.mark.asyncio
async def test_alldebrid_fresh_persisted_snapshot_is_ready_without_network_refresh(
    tmp_path, monkeypatch
):
    store = await runtime_store(tmp_path, monkeypatch, "hosts-fresh.sqlite3")
    clock = Clock()
    snapshot = parse_native_host_snapshot(native_hosts())
    await store.replace(
        "alldebrid",
        encode_host_snapshot(snapshot),
        schema_version=HOST_SCHEMA_VERSION,
        state_key=HOST_STATE_KEY,
        observed_at=clock(),
        successful_at=clock(),
        stale_after=clock() + HOST_REFRESH_SECONDS,
        expected_generation=0,
    )
    client = FakeHostClient()
    client.error = RuntimeError("offline")
    provider = AllDebridProvider(client=client)
    maintenance = AllDebridHostMaintenance(store, clock=clock)
    maintenance.bind(provider, initial=True)

    await maintenance.start()
    assert provider.applicability.readiness == ApplicabilityReadiness.READY
    assert client.host_calls == 0
    await maintenance.maintain()
    assert client.host_calls == 0
    assert provider.applicability.readiness == ApplicabilityReadiness.READY


@pytest.mark.asyncio
async def test_alldebrid_incompatible_snapshot_without_lkg_is_unresolved(
    tmp_path, monkeypatch
):
    store = await runtime_store(tmp_path, monkeypatch, "hosts-corrupt.sqlite3")
    clock = Clock()
    await store.replace(
        "alldebrid",
        b"{not-json",
        schema_version="alldebrid-supported-hosts-v0",
        state_key=HOST_STATE_KEY,
        observed_at=clock(),
        successful_at=clock(),
        stale_after=clock() + HOST_REFRESH_SECONDS,
        expected_generation=0,
    )
    client = FakeHostClient()
    provider = AllDebridProvider(client=client)
    maintenance = AllDebridHostMaintenance(store, clock=clock)
    maintenance.bind(provider, initial=True)

    await maintenance.start()
    assert provider.applicability.readiness == ApplicabilityReadiness.UNRESOLVED
    assert provider.applicability.specialized_hosts == ()
    assert client.host_calls == 0


@pytest.mark.asyncio
async def test_alldebrid_refresh_failure_without_lkg_remains_unresolved_with_backoff(
    tmp_path, monkeypatch
):
    store = await runtime_store(tmp_path, monkeypatch, "hosts-failure.sqlite3")
    clock = Clock()
    client = FakeHostClient()
    client.error = RuntimeError("offline")
    provider = AllDebridProvider(client=client)
    maintenance = AllDebridHostMaintenance(
        store,
        clock=clock,
        retry_seconds=HOST_REFRESH_RETRY_SECONDS,
    )
    maintenance.bind(provider, initial=True)
    await maintenance.start()

    await maintenance.maintain()
    assert client.host_calls == 1
    assert provider.applicability.readiness == ApplicabilityReadiness.UNRESOLVED
    clock.advance(HOST_REFRESH_RETRY_SECONDS / 2)
    await maintenance.maintain()
    assert client.host_calls == 1
    assert provider.applicability.readiness == ApplicabilityReadiness.UNRESOLVED


@pytest.mark.asyncio
async def test_alldebrid_reenable_reuses_retained_lkg_before_network_refresh(
    tmp_path, monkeypatch
):
    store = await runtime_store(tmp_path, monkeypatch, "hosts-reenable.sqlite3")
    clock = Clock()
    client = FakeHostClient()
    enabled = AllDebridProvider(client=client)
    changes = []
    maintenance = AllDebridHostMaintenance(store, clock=clock)
    maintenance.bind(enabled, initial=True, notify=changes.append)
    await maintenance.start()
    await maintenance.maintain()
    assert client.host_calls == 1
    assert enabled.applicability.readiness == ApplicabilityReadiness.READY

    disabled = AllDebridProvider(client=client)
    disabled.descriptor = replace(disabled.descriptor, enabled=False)
    maintenance.bind(disabled, notify=changes.append)
    reenabled = AllDebridProvider(client=client)
    maintenance.bind(reenabled, notify=changes.append)

    assert client.host_calls == 1
    assert reenabled.applicability.readiness == ApplicabilityReadiness.READY
    assert reenabled.applicability.specialized_hosts
    assert changes.count("alldebrid") >= 2

    await maintenance.maintain()
    assert client.host_calls == 2


def test_application_applicability_notification_wakes_existing_owners():
    class Engine:
        repository = object()

    application = ApplicationService(Engine())
    application.notify_applicability_changed("fixture")
    assert application.resolution_wakeup.is_set()
    assert application.integration_wakeup.is_set()


@pytest.mark.asyncio
async def test_integration_maintenance_runs_immediately_without_startup_sleep(monkeypatch):
    import core.scheduler as scheduler

    class Runtime:
        def __init__(self):
            self.integration_wakeup = asyncio.Event()
            self.calls = 0

        def application_storage_permitted(self):
            return True

        async def maintain_integrations(self):
            self.calls += 1

    runtime = Runtime()
    monkeypatch.setattr(scheduler, "application", runtime)

    async def stop_after_first_wait(_event, _timeout):
        raise asyncio.CancelledError

    monkeypatch.setattr(scheduler, "_wait_for_work", stop_after_first_wait)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.integration_maintenance_loop()
    assert runtime.calls == 1


@pytest.mark.asyncio
async def test_resolution_wakeup_during_cycle_causes_prompt_rerun(monkeypatch):
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
                self.resolution_wakeup.set()
                return
            raise asyncio.CancelledError

    runtime = Runtime()
    monkeypatch.setattr(scheduler, "application", runtime)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.sync_status_loop()
    assert runtime.calls == 2


def test_neutral_core_contains_no_alldebrid_readiness_branch():
    backend = Path(__file__).resolve().parents[1]
    for relative in (
        "transfers/applicability.py",
        "transfers/registry.py",
        "transfers/engine.py",
        "application/service.py",
        "core/scheduler.py",
    ):
        source = (backend / relative).read_text(encoding="utf-8").casefold()
        assert "providers.alldebrid" not in source
        assert "provider_id == \"alldebrid\"" not in source
