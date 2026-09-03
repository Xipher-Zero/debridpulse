from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

import db.database as database
from integrations.runtime_state import ProviderRuntimeStateStore
from providers.alldebrid.host_runtime import (
    AllDebridHostMaintenance,
    AllDebridHostSnapshotError,
    HOST_REFRESH_RETRY_SECONDS,
    HOST_REFRESH_SECONDS,
    HOST_SCHEMA_VERSION,
    HOST_STATE_KEY,
    decode_host_snapshot,
    encode_host_snapshot,
    parse_native_host_snapshot,
)
from providers.alldebrid.provider import AllDebridProvider
from providers.general_http.provider import GeneralHttpProvider
from transfers.applicability import HostClaimScope, ProviderApplicability
from transfers.models import (
    Capability,
    IntegrationDescriptor,
    ResolutionResult,
    ResourceState,
    TransferRequest,
)
from transfers.registry import IntegrationRegistry


def native_hosts(*, second_domain="files.example.test", status=False):
    return {
        "hosts": {
            "example-service": {
                "name": "example-service",
                "type": "premium",
                "domains": ["Example.Test.", second_domain],
                "regexps": [
                    r"https?://(?:www\.)?example\.test/[A-Za-z0-9/_-]+",
                    r"https?://files\.example\.test/.+",
                ],
                "status": status,
                "quota": 123,
                "quotaMax": 1000,
                "quotaType": "traffic",
                "limitSimuDl": 2,
                "futureField": {"ignored": True},
            }
        },
        "futureTopLevelField": True,
    }


class Clock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


class FakeClient:
    def __init__(self, response=None):
        self.response = response if response is not None else native_hosts()
        self.error = None
        self.host_calls = 0
        self.delay = False

    async def get_user_hosts(self):
        self.host_calls += 1
        if self.delay:
            await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return self.response

    async def _post(self, base, endpoint, data=None):
        assert endpoint == "user/hosts"
        return await self.get_user_hosts()


class StaticFixtureProvider:
    def __init__(self, identity="other-static"):
        self.descriptor = IntegrationDescriptor(
            identity,
            identity,
            frozenset({Capability.RESOLVE}),
            request_types=frozenset({"magnet", "torrent"}),
        )

    async def resolve(self, request):
        return ResolutionResult(ResourceState.AVAILABLE)


async def runtime_store(tmp_path, monkeypatch, name):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / name)
    await database.init_db()
    store = ProviderRuntimeStateStore()
    await store.start()
    return store


def test_native_parser_keeps_structural_availability_and_quota_distinct():
    snapshot = parse_native_host_snapshot(native_hosts(status=False))
    assert len(snapshot.hosts) == 1
    host = snapshot.hosts[0]

    assert host.domains == ("example.test", "files.example.test")
    assert host.available is False
    assert host.quota == 123
    assert host.quota_max == 1000
    assert host.quota_type == "traffic"
    assert host.simultaneous_downloads_remaining == 2
    assert len(host.regexps) == 2

    assert {claim.host for claim in snapshot.claims} == {
        "example.test",
        "files.example.test",
    }
    assert all(claim.scope == HostClaimScope.EXACT for claim in snapshot.claims)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"hosts": {}},
        {"hosts": {"broken": []}},
        {
            "hosts": {
                "broken": {
                    "name": "broken",
                    "type": "premium",
                    "domains": ["evil.example/path"],
                    "regexps": [r"https://evil\.example/.+"],
                }
            }
        },
        {
            "hosts": {
                "broken": {
                    "name": "broken",
                    "type": "premium",
                    "domains": ["example.test"],
                    "regexps": ["("],
                }
            }
        },
    ],
)
def test_native_parser_rejects_empty_malformed_or_unsafe_snapshots(payload):
    with pytest.raises(AllDebridHostSnapshotError):
        parse_native_host_snapshot(payload)


def test_native_parser_accepts_documented_singular_regexp_compatibility():
    payload = native_hosts()
    record = payload["hosts"]["example-service"]
    record["regexp"] = record["regexps"][0]
    del record["regexps"]
    snapshot = parse_native_host_snapshot(payload)
    assert snapshot.hosts[0].regexps == (record["regexp"],)


def test_snapshot_round_trip_is_provider_owned_and_contains_no_credentials():
    snapshot = parse_native_host_snapshot(native_hosts())
    payload = encode_host_snapshot(snapshot)
    lowered = payload.lower()
    assert b"example.test" in payload
    assert b"apikey" not in lowered
    assert b"authorization" not in lowered
    assert decode_host_snapshot(payload) == snapshot


@pytest.mark.asyncio
async def test_no_snapshot_refresh_persists_claims_and_routes_specialized_over_generic(
    tmp_path, monkeypatch
):
    store = await runtime_store(tmp_path, monkeypatch, "hosts.sqlite3")
    clock = Clock()
    client = FakeClient()
    alldebrid = AllDebridProvider(client=client)
    maintenance = AllDebridHostMaintenance(store, clock=clock)
    maintenance.bind(alldebrid, initial=True)

    await maintenance.start()
    assert client.host_calls == 0
    assert alldebrid.applicability.specialized_hosts == ()

    await maintenance.maintain()
    assert client.host_calls == 1

    retained = await store.load("alldebrid", HOST_STATE_KEY)
    assert retained is not None
    assert retained.schema_version == HOST_SCHEMA_VERSION
    assert retained.stale_after == pytest.approx(clock() + HOST_REFRESH_SECONDS)

    generic = GeneralHttpProvider()
    registry = IntegrationRegistry()
    registry.register_provider(generic)
    registry.register_provider(alldebrid)

    supported = TransferRequest("https", "https://example.test/file")
    unsupported = TransferRequest("https", "https://ordinary.test/file")
    false_boundary = TransferRequest("https", "https://evil-example.test/file")
    assert registry.eligible_providers(supported) == (alldebrid,)
    assert registry.eligible_providers(unsupported) == (generic,)
    assert registry.eligible_providers(false_boundary) == (generic,)

    registry.mark_health("alldebrid", healthy=False)
    assert registry.eligible_providers(supported) == (generic,)
    registry.mark_health("alldebrid", healthy=True)
    assert registry.eligible_providers(supported) == (alldebrid,)


@pytest.mark.asyncio
async def test_restart_restores_fresh_snapshot_without_network_fetch(tmp_path, monkeypatch):
    store = await runtime_store(tmp_path, monkeypatch, "restart.sqlite3")
    clock = Clock()
    client = FakeClient()

    first = AllDebridProvider(client=client)
    first_maintenance = AllDebridHostMaintenance(store, clock=clock)
    first_maintenance.bind(first, initial=True)
    await first_maintenance.start()
    await first_maintenance.maintain()
    assert client.host_calls == 1

    restarted = AllDebridProvider(client=client)
    second_maintenance = AllDebridHostMaintenance(store, clock=clock)
    second_maintenance.bind(restarted, initial=True)
    await second_maintenance.start()

    assert client.host_calls == 1
    assert {claim.host for claim in restarted.applicability.specialized_hosts} == {
        "example.test",
        "files.example.test",
    }
    await second_maintenance.maintain()
    assert client.host_calls == 1


@pytest.mark.asyncio
async def test_stale_submission_is_network_free_then_maintenance_refreshes(
    tmp_path, monkeypatch
):
    store = await runtime_store(tmp_path, monkeypatch, "stale.sqlite3")
    clock = Clock()
    client = FakeClient()
    alldebrid = AllDebridProvider(client=client)
    maintenance = AllDebridHostMaintenance(store, clock=clock)
    maintenance.bind(alldebrid, initial=True)
    await maintenance.start()
    await maintenance.maintain()
    assert client.host_calls == 1

    clock.advance(HOST_REFRESH_SECONDS + 1)
    registry = IntegrationRegistry()
    generic = GeneralHttpProvider()
    registry.register_provider(generic)
    registry.register_provider(alldebrid)

    assert registry.provider_for(
        TransferRequest("https", "https://example.test/stale")
    ) is alldebrid
    assert registry.provider_for(
        TransferRequest("https", "https://ordinary.test/direct")
    ) is generic
    assert client.host_calls == 1

    await maintenance.maintain()
    assert client.host_calls == 2


@pytest.mark.asyncio
async def test_failed_and_malformed_refresh_preserve_last_known_good(
    tmp_path, monkeypatch
):
    store = await runtime_store(tmp_path, monkeypatch, "lkg.sqlite3")
    clock = Clock()
    client = FakeClient()
    alldebrid = AllDebridProvider(client=client)
    maintenance = AllDebridHostMaintenance(
        store, clock=clock, retry_seconds=HOST_REFRESH_RETRY_SECONDS
    )
    maintenance.bind(alldebrid, initial=True)
    await maintenance.start()
    await maintenance.maintain()
    original = await store.load("alldebrid", HOST_STATE_KEY)
    original_claims = alldebrid.applicability
    assert original.generation == 1

    clock.advance(HOST_REFRESH_SECONDS + 1)
    client.error = RuntimeError("provider temporarily unavailable")
    await maintenance.maintain()
    after_failure = await store.load("alldebrid", HOST_STATE_KEY)
    assert after_failure == original
    assert alldebrid.applicability == original_claims

    clock.advance(HOST_REFRESH_RETRY_SECONDS + 1)
    client.error = None
    client.response = {"hosts": {}}
    await maintenance.maintain()
    after_malformed = await store.load("alldebrid", HOST_STATE_KEY)
    assert after_malformed == original
    assert alldebrid.applicability == original_claims

    clock.advance(HOST_REFRESH_RETRY_SECONDS + 1)
    client.response = native_hosts(second_domain="new.example.test", status=True)
    await maintenance.maintain()
    replacement = await store.load("alldebrid", HOST_STATE_KEY)
    assert replacement.generation == 2
    assert "new.example.test" in {
        claim.host for claim in alldebrid.applicability.specialized_hosts
    }


@pytest.mark.asyncio
async def test_disable_retains_state_and_reenable_reuses_then_refreshes(
    tmp_path, monkeypatch
):
    store = await runtime_store(tmp_path, monkeypatch, "enable.sqlite3")
    clock = Clock()
    client = FakeClient()
    enabled = AllDebridProvider(client=client)
    maintenance = AllDebridHostMaintenance(store, clock=clock)
    maintenance.bind(enabled, initial=True)
    await maintenance.start()
    await maintenance.maintain()
    retained = await store.load("alldebrid", HOST_STATE_KEY)
    assert client.host_calls == 1

    disabled = AllDebridProvider(client=client)
    disabled.descriptor = replace(disabled.descriptor, enabled=False)
    maintenance.bind(disabled)
    await maintenance.maintain()
    assert client.host_calls == 1
    assert await store.load("alldebrid", HOST_STATE_KEY) == retained

    reenabled = AllDebridProvider(client=client)
    maintenance.bind(reenabled)
    await maintenance.maintain()
    assert client.host_calls == 2
    assert reenabled.applicability.specialized_hosts


@pytest.mark.asyncio
async def test_concurrent_maintenance_performs_one_authoritative_refresh(
    tmp_path, monkeypatch
):
    store = await runtime_store(tmp_path, monkeypatch, "concurrent.sqlite3")
    clock = Clock()
    client = FakeClient()
    client.delay = True
    alldebrid = AllDebridProvider(client=client)
    maintenance = AllDebridHostMaintenance(store, clock=clock)
    maintenance.bind(alldebrid, initial=True)
    await maintenance.start()

    await asyncio.gather(maintenance.maintain(), maintenance.maintain())
    assert client.host_calls == 1
    assert (await store.load("alldebrid", HOST_STATE_KEY)).generation == 1


@pytest.mark.asyncio
async def test_incompatible_or_corrupt_snapshot_exposes_no_claims_until_refresh(
    tmp_path, monkeypatch
):
    store = await runtime_store(tmp_path, monkeypatch, "corrupt.sqlite3")
    clock = Clock()
    await store.replace(
        "alldebrid",
        b"{not-json",
        schema_version="alldebrid-supported-hosts-v0",
        state_key=HOST_STATE_KEY,
        observed_at=clock(),
        stale_after=clock() + HOST_REFRESH_SECONDS,
        successful_at=clock(),
        expected_generation=0,
    )
    client = FakeClient()
    alldebrid = AllDebridProvider(client=client)
    maintenance = AllDebridHostMaintenance(store, clock=clock)
    maintenance.bind(alldebrid, initial=True)

    await maintenance.start()
    assert alldebrid.applicability.specialized_hosts == ()
    assert client.host_calls == 0
    await maintenance.maintain()
    assert client.host_calls == 1
    assert alldebrid.applicability.specialized_hosts
    assert (await store.load("alldebrid", HOST_STATE_KEY)).schema_version == HOST_SCHEMA_VERSION


def test_magnet_and_torrent_are_neutral_provider_declared_static_capabilities():
    alldebrid = AllDebridProvider(client=FakeClient())
    alldebrid.applicability = ProviderApplicability()
    other = StaticFixtureProvider()
    registry = IntegrationRegistry()
    registry.register_provider(alldebrid)
    registry.register_provider(other)

    magnet = TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40)
    torrent = TransferRequest("torrent", b"d4:infod4:name1:xee")
    for request in (magnet, torrent):
        assert {provider.descriptor.id for provider in registry.eligible_providers(request)} == {
            "alldebrid",
            "other-static",
        }


def test_all_debrid_host_semantics_do_not_leak_into_neutral_layers():
    backend = Path(__file__).resolve().parents[1]
    classifier = (backend / "transfers" / "applicability.py").read_text(
        encoding="utf-8"
    ).casefold()
    registry = (backend / "transfers" / "registry.py").read_text(
        encoding="utf-8"
    ).casefold()
    runtime_state = (backend / "integrations" / "runtime_state.py").read_text(
        encoding="utf-8"
    ).casefold()
    database_source = (backend / "db" / "database.py").read_text(
        encoding="utf-8"
    ).casefold()

    for neutral_source in (classifier, registry, runtime_state):
        assert "providers.alldebrid" not in neutral_source
        assert "alldebrid-supported-hosts" not in neutral_source
    assert "alldebrid_hosts" not in database_source
    assert "alldebrid_host_cache" not in database_source
