from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import aiosqlite
import pytest

import db.database as database
from integrations.runtime_state import (
    ProviderRuntimeStateStore,
    RuntimeStateConflict,
    RuntimeStateCorrupt,
    RuntimeStateStorageError,
)
from transfers.models import TransferRequest
from transfers.registry import IntegrationRegistry
from fake_integrations import ParcelProvider
from fake_runtime_state_provider import (
    FakeRuntimeStateIncompatible,
    FakeRuntimeStateInvalid,
    TelemetryRuntimeProvider,
)


@pytest.fixture
def isolated_database(tmp_path, monkeypatch):
    path = tmp_path / "runtime-state.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", path)
    return path


def telemetry(sequence=1, *, mode="tracking", drift=0.25, samples=None):
    return {
        "sequence": sequence,
        "drift_ppm": drift,
        "mode": mode,
        "samples": list(samples or [3, 5, 8]),
    }


@pytest.mark.asyncio
async def test_store_round_trip_metadata_namespaces_and_provider_isolation(isolated_database):
    store = ProviderRuntimeStateStore()
    first = await store.replace(
        "parcel-lab",
        b"opaque-A",
        schema_version="parcel-schema-7",
        state_key="calibration",
        observed_at=100.0,
        stale_after=160.0,
        successful_at=101.0,
    )
    sibling = await store.replace(
        "parcel-lab",
        b"opaque-B",
        schema_version="parcel-schema-8",
        state_key="counters",
        observed_at=110.0,
    )
    other = await store.replace(
        "weather-fixture",
        b"opaque-C",
        schema_version="wx-1",
        state_key="calibration",
        observed_at=120.0,
    )

    loaded = await store.load("parcel-lab", "calibration")
    assert loaded == first
    assert loaded.payload == b"opaque-A"
    assert loaded.schema_version == "parcel-schema-7"
    assert loaded.observed_at == 100.0
    assert loaded.stale_after == 160.0
    assert loaded.successful_at == 101.0
    assert loaded.generation == 1
    assert not loaded.is_stale(now=159.99)
    assert loaded.is_stale(now=160.0)

    parcel_rows = await store.list_for_integration("parcel-lab")
    assert parcel_rows == (first, sibling)
    assert await store.load("weather-fixture", "calibration") == other
    assert await store.load("missing-provider", "calibration") is None


@pytest.mark.asyncio
async def test_restart_reconstruction_preserves_opaque_state(isolated_database):
    store = ProviderRuntimeStateStore()
    provider = TelemetryRuntimeProvider(store)
    state = telemetry(sequence=17, mode="locked", drift=0.03125)
    written = await provider.retain_validated(state, observed_at=200.0, stale_after=260.0)

    reconstructed_store = ProviderRuntimeStateStore()
    reconstructed_provider = TelemetryRuntimeProvider(reconstructed_store)
    recovered_record, recovered_state = await reconstructed_provider.read()

    assert recovered_state == state
    assert recovered_record.payload == written.payload
    assert recovered_record.schema_version == provider.schema_version
    assert recovered_record.observed_at == 200.0
    assert recovered_record.stale_after == 260.0
    assert recovered_record.generation == 1


@pytest.mark.asyncio
async def test_provider_owns_schema_compatibility_and_payload_validation(isolated_database):
    store = ProviderRuntimeStateStore()
    provider = TelemetryRuntimeProvider(store)

    await store.replace(
        provider.identity,
        provider.serialize(telemetry()),
        schema_version="telemetry-calibration-v1",
        state_key=provider.state_key,
        observed_at=300.0,
    )
    with pytest.raises(FakeRuntimeStateIncompatible):
        await provider.read()

    await store.replace(
        provider.identity,
        b"{not-provider-json",
        schema_version=provider.schema_version,
        state_key=provider.state_key,
        observed_at=301.0,
    )
    stored = await store.load(provider.identity, provider.state_key)
    assert stored.payload == b"{not-provider-json"
    with pytest.raises(FakeRuntimeStateInvalid):
        await provider.read()


@pytest.mark.asyncio
async def test_failed_candidate_cannot_replace_last_known_good(isolated_database):
    store = ProviderRuntimeStateStore()
    provider = TelemetryRuntimeProvider(store)
    state_a = telemetry(sequence=1, mode="tracking")
    record_a = await provider.retain_validated(state_a, observed_at=400.0, stale_after=450.0)

    invalid_b = {"sequence": 2, "drift_ppm": "bad", "mode": "tracking", "samples": [1]}
    with pytest.raises(FakeRuntimeStateInvalid):
        await provider.retain_validated(
            invalid_b,
            observed_at=410.0,
            stale_after=460.0,
            expected_generation=record_a.generation,
        )

    retained, retained_state = await provider.read()
    assert retained_state == state_a
    assert retained.generation == record_a.generation
    assert retained.successful_at == record_a.successful_at

    state_c = telemetry(sequence=3, mode="locked", drift=0.01, samples=[13, 21])
    record_c = await provider.retain_validated(
        state_c,
        observed_at=420.0,
        stale_after=500.0,
        expected_generation=record_a.generation,
    )
    assert record_c.generation == 2
    assert (await provider.read())[1] == state_c


@pytest.mark.asyncio
async def test_stale_concurrent_writer_cannot_overwrite_newer_success(isolated_database):
    store_a = ProviderRuntimeStateStore()
    provider_a = TelemetryRuntimeProvider(store_a)
    initial = await provider_a.retain_validated(telemetry(sequence=1), observed_at=500.0, stale_after=550.0)

    store_b = ProviderRuntimeStateStore()
    provider_b = TelemetryRuntimeProvider(store_b)
    snapshot_b, _ = await provider_b.read()
    assert snapshot_b.generation == initial.generation

    winner = await provider_a.retain_validated(
        telemetry(sequence=2, mode="locked"),
        observed_at=510.0,
        stale_after=570.0,
        expected_generation=initial.generation,
    )
    assert winner.generation == 2

    with pytest.raises(RuntimeStateConflict):
        await provider_b.retain_validated(
            telemetry(sequence=99, mode="cold"),
            observed_at=511.0,
            stale_after=580.0,
            expected_generation=snapshot_b.generation,
        )
    assert (await provider_a.read())[1]["sequence"] == 2


@pytest.mark.asyncio
async def test_parallel_unconditional_updates_remain_whole_records(isolated_database):
    store = ProviderRuntimeStateStore()

    async def write(sequence):
        provider = TelemetryRuntimeProvider(ProviderRuntimeStateStore())
        return await provider.retain_validated(
            telemetry(sequence=sequence, samples=[sequence, sequence + 1]),
            observed_at=600.0 + sequence,
            stale_after=900.0,
        )

    results = await asyncio.gather(*(write(sequence) for sequence in range(1, 9)))
    record, state = await TelemetryRuntimeProvider(store).read()
    assert record.generation == 8
    assert state in [telemetry(sequence=i, samples=[i, i + 1]) for i in range(1, 9)]
    assert sorted(item.generation for item in results) == list(range(1, 9))


@pytest.mark.asyncio
async def test_disable_restart_reenable_retains_state_and_routing_uses_existing_enablement(isolated_database):
    store = ProviderRuntimeStateStore()
    provider_state = TelemetryRuntimeProvider(store, identity="parcel-runtime")
    await provider_state.retain_validated(telemetry(sequence=7), observed_at=700.0, stale_after=800.0)

    registry = IntegrationRegistry()
    parcel = ParcelProvider("parcel-runtime")
    registry.register_provider(parcel)
    request = TransferRequest("parcel", "box-7")
    assert registry.eligible_providers(request) == (parcel,)

    parcel.descriptor = replace(parcel.descriptor, enabled=False)
    assert registry.eligible_providers(request) == ()
    assert (await provider_state.read())[1]["sequence"] == 7

    restarted = TelemetryRuntimeProvider(ProviderRuntimeStateStore(), identity="parcel-runtime")
    assert (await restarted.read())[1]["sequence"] == 7

    parcel.descriptor = replace(parcel.descriptor, enabled=True)
    assert registry.eligible_providers(request) == (parcel,)
    assert (await restarted.read())[1]["sequence"] == 7


@pytest.mark.asyncio
async def test_explicit_purge_is_separate_from_disablement(isolated_database):
    store = ProviderRuntimeStateStore()
    await store.replace("parcel-lab", b"a", schema_version="1", state_key="one")
    await store.replace("parcel-lab", b"b", schema_version="1", state_key="two")
    await store.replace("other-lab", b"c", schema_version="1", state_key="one")

    assert await store.delete("parcel-lab", "one")
    assert await store.load("parcel-lab", "one") is None
    assert await store.load("parcel-lab", "two") is not None
    assert await store.purge_integration("parcel-lab") == 1
    assert await store.list_for_integration("parcel-lab") == ()
    assert await store.load("other-lab", "one") is not None


@pytest.mark.asyncio
async def test_malformed_neutral_metadata_is_bounded_without_touching_transfer_state(isolated_database):
    store = ProviderRuntimeStateStore()
    await store.replace("parcel-lab", b"opaque", schema_version="1", observed_at=800.0)
    async with aiosqlite.connect(isolated_database) as db:
        await db.execute(
            "UPDATE integration_runtime_state SET stale_after='not-a-timestamp' WHERE integration_id='parcel-lab'"
        )
        await db.commit()
    with pytest.raises(RuntimeStateCorrupt):
        await store.load("parcel-lab")


@pytest.mark.asyncio
async def test_fresh_upgrade_and_idempotent_schema_preserve_existing_application_data(isolated_database):
    async with aiosqlite.connect(isolated_database) as db:
        await db.execute("CREATE TABLE legacy_sentinel(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        await db.execute("INSERT INTO legacy_sentinel(value) VALUES('preserve-me')")
        await db.commit()

    first = ProviderRuntimeStateStore()
    second = ProviderRuntimeStateStore()
    await first.initialize()
    await second.initialize()
    await first.initialize()

    async with aiosqlite.connect(isolated_database) as db:
        sentinel = await (await db.execute("SELECT value FROM legacy_sentinel")).fetchone()
        count = await (await db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='integration_runtime_state'"
        )).fetchone()
        indexes = await (await db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_integration_runtime_state_updated'"
        )).fetchone()
    assert sentinel == ("preserve-me",)
    assert count == (1,)
    assert indexes == (1,)


@pytest.mark.asyncio
async def test_schema_failure_rolls_back_without_false_partial_success(isolated_database):
    class BrokenStore(ProviderRuntimeStateStore):
        @staticmethod
        def _schema_statements():
            return ProviderRuntimeStateStore._schema_statements() + ("THIS IS NOT VALID SQL",)

    with pytest.raises(RuntimeStateStorageError):
        await BrokenStore().initialize()

    async with aiosqlite.connect(isolated_database) as db:
        present = await (await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='integration_runtime_state'"
        )).fetchone()
    assert present is None


@pytest.mark.asyncio
async def test_database_open_failure_is_reported_as_bounded_storage_error(tmp_path, monkeypatch):
    directory = tmp_path / "not-a-database"
    directory.mkdir()
    monkeypatch.setattr(database, "DB_PATH", directory)
    with pytest.raises(RuntimeStateStorageError):
        await ProviderRuntimeStateStore().initialize()


@pytest.mark.asyncio
async def test_schema_is_generic_and_payload_does_not_leak_into_settings_or_transfer_core(isolated_database):
    store = ProviderRuntimeStateStore()
    await store.initialize()
    async with aiosqlite.connect(isolated_database) as db:
        row = await (await db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='integration_runtime_state'"
        )).fetchone()
    schema = row[0].lower()
    assert "alldebrid" not in schema
    assert "realdebrid" not in schema
    assert "host" not in schema
    assert "integration_id" in schema
    assert "payload blob" in schema

    backend = Path(__file__).resolve().parents[1]
    neutral_source = (backend / "integrations" / "runtime_state.py").read_text(encoding="utf-8").lower()
    assert "providers." not in neutral_source
    assert "alldebrid" not in neutral_source
    assert "realdebrid" not in neutral_source

    for relative in (
        "transfers/engine.py",
        "transfers/registry.py",
        "transfers/repository.py",
        "core/config.py",
    ):
        source = (backend / relative).read_text(encoding="utf-8").lower()
        assert "integration_runtime_state" not in source
        assert "provider_runtime_state" not in source
