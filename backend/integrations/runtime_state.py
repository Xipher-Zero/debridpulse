"""Neutral durable operational runtime state for integrations.

The store owns persistence mechanics and neutral metadata only. Integration code
owns payload serialization, validation, schema compatibility, freshness policy,
and interpretation. Runtime state is deliberately independent from user
configuration and transfer lifecycle persistence.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass

from db.database import get_db, validate_runtime_state_schema


_DEFAULT_STATE_KEY = "default"


class RuntimeStateError(RuntimeError):
    """Base error for the neutral integration runtime-state facility."""


class RuntimeStateStorageError(RuntimeStateError):
    """The durable store could not complete a read, write, or schema operation."""


class RuntimeStateCorrupt(RuntimeStateError):
    """Neutral persisted metadata is malformed or internally inconsistent."""


class RuntimeStateConflict(RuntimeStateError):
    """Optimistic replacement was based on an out-of-date generation."""


@dataclass(frozen=True)
class RuntimeStateRecord:
    integration_id: str
    state_key: str
    schema_version: str
    payload: bytes
    observed_at: float
    stale_after: float | None
    successful_at: float
    created_at: float
    updated_at: float
    generation: int

    def is_stale(self, *, now: float | None = None) -> bool:
        """Evaluate provider-declared absolute staleness metadata neutrally."""
        current = time.time() if now is None else float(now)
        if not math.isfinite(current):
            raise ValueError("now must be a finite UTC epoch timestamp")
        return self.stale_after is not None and current >= self.stale_after


class ProviderRuntimeStateStore:
    """SQLite-backed neutral persistence for opaque integration-owned state.

    ``integration_id`` is the canonical integration descriptor identity already
    used by the registry. ``state_key`` is an integration-private namespace.
    Payload bytes and their schema marker are never decoded here.
    """

    def __init__(self) -> None:
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    @staticmethod
    def _identity(value: str, *, field: str, default: str | None = None) -> str:
        normalized = str(value or default or "").strip()
        if not normalized:
            raise ValueError(f"{field} must be non-empty")
        return normalized

    @staticmethod
    def _payload(value: bytes | bytearray | memoryview) -> bytes:
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TypeError("runtime-state payload must be bytes-like")
        return bytes(value)

    @staticmethod
    def _timestamp(value: float, *, field: str) -> float:
        try:
            normalized = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field} must be a finite UTC epoch timestamp") from exc
        if not math.isfinite(normalized):
            raise ValueError(f"{field} must be a finite UTC epoch timestamp")
        return normalized

    @classmethod
    def _record(cls, row) -> RuntimeStateRecord | None:
        if row is None:
            return None
        try:
            integration_id = str(row["integration_id"]).strip()
            state_key = str(row["state_key"]).strip()
            schema_version = str(row["schema_version"]).strip()
            payload = row["payload"]
            if not integration_id or not state_key or not schema_version:
                raise ValueError("blank required metadata")
            if not isinstance(payload, (bytes, bytearray, memoryview)):
                raise TypeError("payload is not a SQLite BLOB")
            observed_at = cls._timestamp(row["observed_at"], field="observed_at")
            stale_after = None if row["stale_after"] is None else cls._timestamp(row["stale_after"], field="stale_after")
            successful_at = cls._timestamp(row["successful_at"], field="successful_at")
            created_at = cls._timestamp(row["created_at"], field="created_at")
            updated_at = cls._timestamp(row["updated_at"], field="updated_at")
            generation = int(row["generation"])
            if generation <= 0:
                raise ValueError("generation must be positive")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise RuntimeStateCorrupt("Integration runtime-state metadata is malformed") from exc
        return RuntimeStateRecord(
            integration_id=integration_id,
            state_key=state_key,
            schema_version=schema_version,
            payload=bytes(payload),
            observed_at=observed_at,
            stale_after=stale_after,
            successful_at=successful_at,
            created_at=created_at,
            updated_at=updated_at,
            generation=generation,
        )

    async def initialize(self) -> None:
        """Verify bootstrap-owned schema without creating or repairing it."""
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            try:
                await validate_runtime_state_schema()
            except Exception as exc:
                raise RuntimeStateStorageError("Could not initialize integration runtime-state persistence") from exc
            self._initialized = True

    async def start(self) -> None:
        """Application lifecycle hook; no provider activity is started here."""
        await self.initialize()

    async def stop(self) -> None:
        return None

    async def maintain(self) -> None:
        """Lifecycle maintenance hook; persistence has no periodic work."""
        return None

    async def load(self, integration_id: str, state_key: str = _DEFAULT_STATE_KEY) -> RuntimeStateRecord | None:
        await self.initialize()
        integration_id = self._identity(integration_id, field="integration_id")
        state_key = self._identity(state_key, field="state_key", default=_DEFAULT_STATE_KEY)
        try:
            async with get_db() as db:
                row = await db.fetchone(
                    "SELECT integration_id,state_key,schema_version,payload,observed_at,stale_after,successful_at,created_at,updated_at,generation "
                    "FROM integration_runtime_state WHERE integration_id=? AND state_key=?",
                    (integration_id, state_key),
                )
        except Exception as exc:
            raise RuntimeStateStorageError("Could not read integration runtime state") from exc
        return self._record(row)

    async def list_for_integration(self, integration_id: str) -> tuple[RuntimeStateRecord, ...]:
        await self.initialize()
        integration_id = self._identity(integration_id, field="integration_id")
        try:
            async with get_db() as db:
                rows = await db.fetchall(
                    "SELECT integration_id,state_key,schema_version,payload,observed_at,stale_after,successful_at,created_at,updated_at,generation "
                    "FROM integration_runtime_state WHERE integration_id=? ORDER BY state_key",
                    (integration_id,),
                )
        except Exception as exc:
            raise RuntimeStateStorageError("Could not list integration runtime state") from exc
        return tuple(self._record(row) for row in rows)

    async def replace(
        self,
        integration_id: str,
        payload: bytes | bytearray | memoryview,
        *,
        schema_version: str,
        state_key: str = _DEFAULT_STATE_KEY,
        observed_at: float | None = None,
        stale_after: float | None = None,
        successful_at: float | None = None,
        expected_generation: int | None = None,
    ) -> RuntimeStateRecord:
        """Atomically replace one caller-validated last-known-good record.

        The caller must validate provider-specific payload content before this
        method is called. ``expected_generation`` is an optional compare-and-
        swap guard so a stale concurrent refresh cannot overwrite newer state.
        """
        await self.initialize()
        integration_id = self._identity(integration_id, field="integration_id")
        state_key = self._identity(state_key, field="state_key", default=_DEFAULT_STATE_KEY)
        schema_version = self._identity(schema_version, field="schema_version")
        payload = self._payload(payload)
        now = self._timestamp(time.time(), field="updated_at")
        observed_at = now if observed_at is None else self._timestamp(observed_at, field="observed_at")
        successful_at = observed_at if successful_at is None else self._timestamp(successful_at, field="successful_at")
        stale_after = None if stale_after is None else self._timestamp(stale_after, field="stale_after")
        if expected_generation is not None and int(expected_generation) < 0:
            raise ValueError("expected_generation must be non-negative")

        try:
            async with get_db() as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    current = await db.fetchone(
                        "SELECT created_at,generation FROM integration_runtime_state WHERE integration_id=? AND state_key=?",
                        (integration_id, state_key),
                    )
                    current_generation = int(current["generation"]) if current else 0
                    if expected_generation is not None and current_generation != int(expected_generation):
                        raise RuntimeStateConflict(
                            f"Runtime-state generation changed from {expected_generation} to {current_generation}"
                        )
                    generation = current_generation + 1
                    created_at = self._timestamp(current["created_at"], field="created_at") if current else now
                    await db.execute(
                        """INSERT INTO integration_runtime_state(
                            integration_id,state_key,schema_version,payload,observed_at,stale_after,
                            successful_at,created_at,updated_at,generation
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(integration_id,state_key) DO UPDATE SET
                            schema_version=excluded.schema_version,
                            payload=excluded.payload,
                            observed_at=excluded.observed_at,
                            stale_after=excluded.stale_after,
                            successful_at=excluded.successful_at,
                            updated_at=excluded.updated_at,
                            generation=excluded.generation""",
                        (
                            integration_id, state_key, schema_version, payload, observed_at, stale_after,
                            successful_at, created_at, now, generation,
                        ),
                    )
                    row = await db.fetchone(
                        "SELECT integration_id,state_key,schema_version,payload,observed_at,stale_after,successful_at,created_at,updated_at,generation "
                        "FROM integration_runtime_state WHERE integration_id=? AND state_key=?",
                        (integration_id, state_key),
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
        except RuntimeStateConflict:
            raise
        except RuntimeStateCorrupt:
            raise
        except Exception as exc:
            raise RuntimeStateStorageError("Could not replace integration runtime state") from exc
        record = self._record(row)
        if record is None:
            raise RuntimeStateStorageError("Runtime-state replacement did not produce a durable record")
        return record

    async def delete(self, integration_id: str, state_key: str = _DEFAULT_STATE_KEY) -> bool:
        """Explicitly purge one runtime-state key; disablement never calls this."""
        await self.initialize()
        integration_id = self._identity(integration_id, field="integration_id")
        state_key = self._identity(state_key, field="state_key", default=_DEFAULT_STATE_KEY)
        try:
            async with get_db() as db:
                cursor = await db.execute(
                    "DELETE FROM integration_runtime_state WHERE integration_id=? AND state_key=?",
                    (integration_id, state_key),
                )
                await db.commit()
                return bool(cursor.rowcount)
        except Exception as exc:
            raise RuntimeStateStorageError("Could not delete integration runtime state") from exc

    async def purge_integration(self, integration_id: str) -> int:
        """Explicitly purge every key owned by one integration identity."""
        await self.initialize()
        integration_id = self._identity(integration_id, field="integration_id")
        try:
            async with get_db() as db:
                cursor = await db.execute(
                    "DELETE FROM integration_runtime_state WHERE integration_id=?",
                    (integration_id,),
                )
                await db.commit()
                return max(0, int(cursor.rowcount))
        except Exception as exc:
            raise RuntimeStateStorageError("Could not purge integration runtime state") from exc
