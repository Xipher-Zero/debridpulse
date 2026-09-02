from __future__ import annotations

import json

import pytest

import db.database as database
from integrations.runtime_state import ProviderRuntimeStateStore, RuntimeStateRecord
from fake_runtime_state_provider import (
    FakeRuntimeStateIncompatible,
    TelemetryRuntimeProvider,
)


class CounterRuntimeProvider:
    """Second unrelated fake proving payload interpretation stays provider-owned."""

    schema_version = "counter-observation-v1"
    state_key = "calibration"

    def __init__(self, store: ProviderRuntimeStateStore, identity: str = "counter-lab") -> None:
        self.store = store
        self.identity = identity

    @classmethod
    def serialize(cls, value: dict) -> bytes:
        if set(value) != {"counter", "label"}:
            raise ValueError("counter observation fields are invalid")
        if not isinstance(value["counter"], int) or not isinstance(value["label"], str):
            raise ValueError("counter observation values are invalid")
        return json.dumps(value, sort_keys=True).encode("utf-8")

    @classmethod
    def deserialize(cls, record: RuntimeStateRecord) -> dict:
        if record.schema_version != cls.schema_version:
            raise FakeRuntimeStateIncompatible(record.schema_version)
        value = json.loads(record.payload.decode("utf-8"))
        cls.serialize(value)  # provider-owned validation; return value only after it passes
        return value

    async def retain(self, value: dict) -> RuntimeStateRecord:
        return await self.store.replace(
            self.identity,
            self.serialize(value),
            schema_version=self.schema_version,
            state_key=self.state_key,
            observed_at=2000.0,
            stale_after=2100.0,
        )

    async def read(self) -> dict | None:
        record = await self.store.load(self.identity, self.state_key)
        return None if record is None else self.deserialize(record)


@pytest.mark.asyncio
async def test_two_unrelated_fake_providers_share_store_without_collision_or_cross_interpretation(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "provider-isolation.sqlite3")
    store = ProviderRuntimeStateStore()
    telemetry = TelemetryRuntimeProvider(store, identity="telemetry-lab")
    counter = CounterRuntimeProvider(store, identity="counter-lab")

    telemetry_value = {
        "sequence": 42,
        "drift_ppm": 0.125,
        "mode": "locked",
        "samples": [2, 4, 8],
    }
    counter_value = {"counter": 9, "label": "phase-nine"}

    telemetry_record = await telemetry.retain_validated(
        telemetry_value,
        observed_at=1900.0,
        stale_after=2050.0,
    )
    counter_record = await counter.retain(counter_value)

    assert (await telemetry.read())[1] == telemetry_value
    assert await counter.read() == counter_value
    assert telemetry_record.state_key == counter_record.state_key == "calibration"
    assert telemetry_record.integration_id != counter_record.integration_id
    assert telemetry_record.payload != counter_record.payload

    # Even if handed another provider's record directly, the second fake does
    # not reinterpret it: its own schema contract rejects it before payload use.
    with pytest.raises(FakeRuntimeStateIncompatible):
        CounterRuntimeProvider.deserialize(telemetry_record)
    with pytest.raises(FakeRuntimeStateIncompatible):
        TelemetryRuntimeProvider.deserialize(counter_record)
