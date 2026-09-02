"""Deterministic fake integration consumer for runtime-state persistence tests.

Its payload is deliberately unrelated to debrid services, hosts, URLs, or
routing. The fake owns serialization, validation, schema compatibility, and
interpretation; the neutral store sees bytes and metadata only.
"""
from __future__ import annotations

import json

from integrations.runtime_state import ProviderRuntimeStateStore, RuntimeStateRecord


class FakeRuntimeStateInvalid(ValueError):
    pass


class FakeRuntimeStateIncompatible(ValueError):
    pass


class TelemetryRuntimeProvider:
    schema_version = "telemetry-calibration-v2"
    state_key = "calibration"

    def __init__(self, store: ProviderRuntimeStateStore, identity: str = "parcel-lab") -> None:
        self.store = store
        self.identity = identity

    @classmethod
    def serialize(cls, value: dict) -> bytes:
        cls.validate(value)
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def validate(value: dict) -> None:
        if not isinstance(value, dict):
            raise FakeRuntimeStateInvalid("telemetry state must be an object")
        required = {"sequence", "drift_ppm", "mode", "samples"}
        if set(value) != required:
            raise FakeRuntimeStateInvalid("telemetry fields are incomplete")
        if not isinstance(value["sequence"], int) or value["sequence"] < 0:
            raise FakeRuntimeStateInvalid("sequence is invalid")
        if not isinstance(value["drift_ppm"], (int, float)):
            raise FakeRuntimeStateInvalid("drift is invalid")
        if value["mode"] not in {"cold", "tracking", "locked"}:
            raise FakeRuntimeStateInvalid("mode is invalid")
        if not isinstance(value["samples"], list) or not all(isinstance(item, int) for item in value["samples"]):
            raise FakeRuntimeStateInvalid("samples are invalid")

    @classmethod
    def deserialize(cls, record: RuntimeStateRecord) -> dict:
        if record.schema_version != cls.schema_version:
            raise FakeRuntimeStateIncompatible(record.schema_version)
        try:
            value = json.loads(record.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FakeRuntimeStateInvalid("telemetry payload is malformed") from exc
        cls.validate(value)
        return value

    async def read(self) -> tuple[RuntimeStateRecord, dict] | None:
        record = await self.store.load(self.identity, self.state_key)
        if record is None:
            return None
        return record, self.deserialize(record)

    async def retain_validated(
        self,
        value: dict,
        *,
        observed_at: float,
        stale_after: float | None,
        expected_generation: int | None = None,
    ) -> RuntimeStateRecord:
        payload = self.serialize(value)
        return await self.store.replace(
            self.identity,
            payload,
            schema_version=self.schema_version,
            state_key=self.state_key,
            observed_at=observed_at,
            stale_after=stale_after,
            successful_at=observed_at,
            expected_generation=expected_generation,
        )
