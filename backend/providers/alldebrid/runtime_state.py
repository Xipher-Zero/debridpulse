"""Credential-scoped durable runtime-state namespace for AllDebrid.

AllDebrid host inventory is authenticated account state.  The short-term
connection ownership policy deliberately treats the API credential as the
stable binding: a credential change gets a fresh runtime-state namespace and
must refresh account facts rather than inheriting the previous key's LKG.
"""
from __future__ import annotations

from hashlib import sha256


_SCOPE_VERSION = "credential-v1"


def credential_scope(api_key: str) -> str:
    """Return an opaque, non-secret namespace token for one credential value."""
    material = str(api_key or "").encode("utf-8")
    digest = sha256(b"debridpulse:alldebrid:credential:v1\0" + material).hexdigest()
    return f"{_SCOPE_VERSION}-{digest}"


class AllDebridRuntimeStateStore:
    """Scope AllDebrid-owned state keys to the credential that produced them."""

    def __init__(self, store, scope: str) -> None:
        self._store = store
        self._scope = str(scope or "").strip()
        if not self._scope:
            raise ValueError("AllDebrid runtime-state scope must be non-empty")

    def _state_key(self, state_key: str) -> str:
        key = str(state_key or "default").strip() or "default"
        return f"{key}:{self._scope}"

    async def load(self, integration_id: str, state_key: str = "default"):
        return await self._store.load(integration_id, self._state_key(state_key))

    async def replace(self, integration_id: str, payload, *, schema_version: str,
                      state_key: str = "default", observed_at=None, stale_after=None,
                      successful_at=None, expected_generation=None):
        return await self._store.replace(
            integration_id,
            payload,
            schema_version=schema_version,
            state_key=self._state_key(state_key),
            observed_at=observed_at,
            stale_after=stale_after,
            successful_at=successful_at,
            expected_generation=expected_generation,
        )
