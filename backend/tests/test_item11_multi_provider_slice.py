"""Roadmap Item 11 cross-slice multi-provider qualification scenarios.

These tests intentionally cross the production ownership seams that Items 5-10
established instead of replacing their narrower canonical regression suites.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import db.database as database
from api.routes import _public_transfer_presentation
from fake_integrations import MemoryExecutor
from integrations.catalog import definitions
from integrations.runtime_state import ProviderRuntimeStateStore
from providers.alldebrid.host_runtime import (
    AllDebridHostMaintenance,
    HOST_REFRESH_SECONDS,
    HOST_STATE_KEY,
)
from providers.alldebrid.provider import AllDebridProvider
from providers.general_http.provider import GeneralHttpProvider
from transfers.engine import TransferEngine
from transfers.models import TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository

pytestmark = pytest.mark.asyncio


def native_hosts(*domains: str, status: bool = True):
    domains = domains or ("example.test",)
    escaped = [domain.replace(".", r"\.") for domain in domains]
    return {
        "hosts": {
            "item11-fixture": {
                "name": "item11-fixture",
                "type": "premium",
                "domains": list(domains),
                "regexps": [rf"https?://{domain}/.+" for domain in escaped],
                "status": status,
                "quota": 10,
                "quotaMax": 100,
                "quotaType": "traffic",
                "limitSimuDl": 2,
            }
        }
    }


class Clock:
    def __init__(self, value: float = 1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds: float):
        self.value += float(seconds)


class Item11Client:
    def __init__(self, response=None):
        self.response = response if response is not None else native_hosts("example.test")
        self.error = None
        self.host_calls = 0
        self.unlock_calls = 0

    async def get_user_hosts(self):
        self.host_calls += 1
        if self.error is not None:
            raise self.error
        return self.response

    async def unlock_link(self, link):
        self.unlock_calls += 1
        return {
            "link": "https://example.org/item11.bin",
            "filename": "item11.bin",
            "filesize": 4,
        }


class HttpMemoryExecutor(MemoryExecutor):
    descriptor = replace(
        MemoryExecutor.descriptor,
        id="item11-http-memory",
        name="Item 11 deterministic HTTP executor",
        schemes=frozenset({"http", "https"}),
    )


async def build_core(tmp_path, monkeypatch, name: str, *, client=None):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / name)
    await database.init_db()

    repository = TransferRepository()
    registry = IntegrationRegistry()
    client = client or Item11Client()
    alldebrid = AllDebridProvider(client=client)
    general = GeneralHttpProvider()
    executor = HttpMemoryExecutor(repository.authorize_execution)
    registry.register_provider(alldebrid)
    registry.register_provider(general)
    registry.register_executor(executor)

    engine = TransferEngine(
        repository,
        registry,
        download_root=str(tmp_path / "downloads"),
        policy=TransferPolicy(),
    )
    await engine.initialize()

    store = ProviderRuntimeStateStore()
    await store.start()
    clock = Clock()
    maintenance = AllDebridHostMaintenance(store, clock=clock)
    maintenance.bind(alldebrid, initial=True)
    await maintenance.start()

    return SimpleNamespace(
        repository=repository,
        registry=registry,
        client=client,
        alldebrid=alldebrid,
        general=general,
        executor=executor,
        engine=engine,
        store=store,
        clock=clock,
        maintenance=maintenance,
    )


async def complete(core, request: TransferRequest):
    transfer = await core.engine.submit((request,), name=request.name, deduplicate=False)
    await core.engine.tick()
    artifacts = await core.repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    assert artifacts[0].execution is not None
    core.executor.finish(artifacts[0].execution)
    await core.engine.tick()
    completed = await core.repository.get(transfer.id)
    assert completed is not None
    assert completed.state == TransferState.COMPLETED
    return completed


async def test_supported_url_crosses_runtime_routing_execution_provenance_ui_and_restart(
    tmp_path, monkeypatch
):
    client = Item11Client(native_hosts("example.test", status=False))
    core = await build_core(tmp_path, monkeypatch, "supported.sqlite3", client=client)

    # Runtime inventory is populated by maintenance, not admission/resolution.
    await core.maintenance.maintain()
    assert client.host_calls == 1

    request = TransferRequest(
        "https", "https://example.test/supported.bin?token=secret", name="supported.bin"
    )
    completed = await complete(core, request)

    # status=False is native current availability, not loss of structural support.
    # The already-qualified Item 7 policy therefore still exposes SPECIALIZED.
    assert client.unlock_calls == 1
    assert client.host_calls == 1

    raw = await core.repository.presentation(completed.id, details=True)
    assert raw["delivering_provider_id"] == "alldebrid"
    assert raw["current_provider_id"] == "alldebrid"
    assert [attempt["provider_id"] for attempt in raw["route_attempts"]] == ["alldebrid"]
    assert raw["execution_attempts"][0]["provider_id"] == "alldebrid"

    public = _public_transfer_presentation(raw, definitions)
    assert public["delivering_provider_name"] == "AllDebrid"
    assert public["current_provider_name"] == "AllDebrid"
    assert public["original_resource"] == "https://example.test/supported.bin?…"
    assert "token=secret" not in str(public)

    # Fresh provider runtime state and historical provenance both survive restart
    # without a startup/submission inventory fetch.
    restarted_provider = AllDebridProvider(client=client)
    restarted_maintenance = AllDebridHostMaintenance(core.store, clock=core.clock)
    restarted_maintenance.bind(restarted_provider, initial=True)
    await restarted_maintenance.start()
    assert client.host_calls == 1

    restarted_registry = IntegrationRegistry()
    restarted_registry.register_provider(GeneralHttpProvider())
    restarted_registry.register_provider(restarted_provider)
    assert restarted_registry.provider_for(request) is restarted_provider
    assert client.host_calls == 1

    restarted_repository = TransferRepository()
    await restarted_repository.initialize()
    restarted_raw = await restarted_repository.presentation(completed.id, details=True)
    assert restarted_raw["delivering_provider_id"] == "alldebrid"
    restarted_public = _public_transfer_presentation(restarted_raw, definitions)
    assert restarted_public["delivering_provider_name"] == "AllDebrid"


async def test_direct_route_history_survives_later_alldebrid_claim_change(tmp_path, monkeypatch):
    client = Item11Client(native_hosts("example.test"))
    core = await build_core(tmp_path, monkeypatch, "direct-history.sqlite3", client=client)
    await core.maintenance.maintain()
    assert client.host_calls == 1

    request = TransferRequest(
        "https", "https://host-change.example/direct.bin", name="direct.bin"
    )
    completed = await complete(core, request)
    assert client.unlock_calls == 0
    assert client.host_calls == 1

    before = await core.repository.presentation(completed.id, details=True)
    assert before["delivering_provider_id"] == "general_http"
    before_public = _public_transfer_presentation(before, definitions)
    assert before_public["delivering_provider_name"] == "HTTP & HTTPS"

    # The same URL becomes structurally specialized later. Current applicability
    # changes routing for new work only; durable history must not be rewritten.
    core.clock.advance(HOST_REFRESH_SECONDS + 1)
    client.response = native_hosts("example.test", "host-change.example")
    await core.maintenance.maintain()
    assert client.host_calls == 2
    assert core.registry.provider_for(request) is core.alldebrid

    restarted_repository = TransferRepository()
    await restarted_repository.initialize()
    after = await restarted_repository.presentation(completed.id, details=True)
    assert after["delivering_provider_id"] == "general_http"
    assert [attempt["provider_id"] for attempt in after["route_attempts"]] == ["general_http"]
    after_public = _public_transfer_presentation(after, definitions)
    assert after_public["delivering_provider_name"] == "HTTP & HTTPS"


async def test_initial_host_refresh_failure_keeps_generic_http_operational(tmp_path, monkeypatch):
    client = Item11Client(native_hosts("example.test"))
    client.error = RuntimeError("deterministic initial host refresh failure")
    core = await build_core(tmp_path, monkeypatch, "initial-refresh-failure.sqlite3", client=client)

    request = TransferRequest("https", "https://example.test/fallback.bin", name="fallback.bin")
    assert client.host_calls == 0
    assert core.registry.provider_for(request) is core.general
    assert client.host_calls == 0

    # Maintenance owns the failed network attempt. It must not manufacture a
    # valid LKG snapshot or poison generic HTTP routing.
    await core.maintenance.maintain()
    assert client.host_calls == 1
    assert await core.store.load("alldebrid", HOST_STATE_KEY) is None
    assert core.registry.provider_for(request) is core.general

    completed = await complete(core, request)
    assert client.host_calls == 1
    assert client.unlock_calls == 0
    raw = await core.repository.presentation(completed.id, details=True)
    assert raw["delivering_provider_id"] == "general_http"
    public = _public_transfer_presentation(raw, definitions)
    assert public["delivering_provider_name"] == "HTTP & HTTPS"
