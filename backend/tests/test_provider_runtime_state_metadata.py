from __future__ import annotations

import math

import pytest

import db.database as database
from integrations.runtime_state import ProviderRuntimeStateStore


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("observed_at", math.nan),
        ("observed_at", math.inf),
        ("successful_at", -math.inf),
        ("stale_after", math.nan),
    ),
)
async def test_non_finite_neutral_timestamp_is_rejected_before_replacement(tmp_path, monkeypatch, field, value):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / f"non-finite-{field}.sqlite3")
    await database.init_db()
    store = ProviderRuntimeStateStore()
    original = await store.replace(
        "parcel-lab",
        b"known-good",
        schema_version="parcel-v1",
        observed_at=100.0,
        successful_at=101.0,
        stale_after=200.0,
    )

    kwargs = {
        "observed_at": 150.0,
        "successful_at": 151.0,
        "stale_after": 250.0,
    }
    kwargs[field] = value
    with pytest.raises(ValueError, match="finite UTC epoch timestamp"):
        await store.replace(
            "parcel-lab",
            b"must-not-commit",
            schema_version="parcel-v2",
            expected_generation=original.generation,
            **kwargs,
        )

    retained = await store.load("parcel-lab")
    assert retained.payload == b"known-good"
    assert retained.schema_version == "parcel-v1"
    assert retained.generation == 1


def test_staleness_check_rejects_non_finite_clock_value():
    from integrations.runtime_state import RuntimeStateRecord

    record = RuntimeStateRecord(
        integration_id="parcel-lab",
        state_key="default",
        schema_version="parcel-v1",
        payload=b"opaque",
        observed_at=100.0,
        stale_after=200.0,
        successful_at=100.0,
        created_at=100.0,
        updated_at=100.0,
        generation=1,
    )
    with pytest.raises(ValueError, match="finite UTC epoch timestamp"):
        record.is_stale(now=math.nan)
