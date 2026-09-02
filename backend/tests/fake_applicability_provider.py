"""Deterministic provider-neutral fixtures for applicability and runtime-derived claims."""
from __future__ import annotations

import json
from dataclasses import replace

from integrations.runtime_state import ProviderRuntimeStateStore
from transfers.applicability import HostClaim, HostClaimScope, ProviderApplicability
from transfers.models import (
    Capability, Endpoint, IntegrationDescriptor, ResolutionResult, ResourceState,
    TransferCandidate,
)


class SpecializedFixtureProvider:
    def __init__(
        self,
        identity: str = "routing-fixture",
        *,
        host: str = "example.test",
        scope: HostClaimScope = HostClaimScope.DOMAIN,
        schemes=frozenset({"https"}),
        priority: int = 0,
    ):
        self.descriptor = IntegrationDescriptor(
            identity,
            "Routing fixture",
            frozenset({Capability.RESOLVE}),
            request_types=frozenset({"http", "https"}),
            priority=priority,
        )
        self.applicability = ProviderApplicability(
            specialized_hosts=(HostClaim(host, scope, frozenset(schemes)),),
        )
        self.calls = []

    async def resolve(self, request):
        self.calls.append(request)
        return ResolutionResult(
            ResourceState.AVAILABLE,
            (
                TransferCandidate(
                    request.name or "fixture.bin",
                    (Endpoint(request.kind, str(request.payload)),),
                    provider_id=self.descriptor.id,
                ),
            ),
        )


class RuntimeClaimProvider(SpecializedFixtureProvider):
    """Own opaque runtime decoding/freshness and expose only canonical claims."""

    schema_version = "routing-lab-v1"
    state_key = "routing-snapshot"

    def __init__(
        self,
        store: ProviderRuntimeStateStore,
        identity: str = "runtime-routing-fixture",
        *,
        priority: int = 0,
    ):
        super().__init__(identity, priority=priority)
        self.store = store
        self.applicability = ProviderApplicability()

    @staticmethod
    def _serialize(value: dict) -> bytes:
        if (
            not isinstance(value, dict)
            or set(value) != {"domains", "usable"}
            or not isinstance(value["domains"], list)
            or not all(isinstance(item, str) and item.strip() for item in value["domains"])
            or not isinstance(value["usable"], bool)
        ):
            raise ValueError("routing fixture state is invalid")
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def _deserialize(cls, record) -> dict:
        if record.schema_version != cls.schema_version:
            raise ValueError("routing fixture schema is incompatible")
        try:
            value = json.loads(record.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("routing fixture payload is malformed") from exc
        cls._serialize(value)
        return value

    async def retain(
        self,
        domains,
        *,
        usable: bool = True,
        observed_at: float,
        stale_after: float | None = None,
    ):
        return await self.store.replace(
            self.descriptor.id,
            self._serialize({"domains": list(domains), "usable": usable}),
            schema_version=self.schema_version,
            state_key=self.state_key,
            observed_at=observed_at,
            stale_after=stale_after,
            successful_at=observed_at,
        )

    async def refresh_applicability(self, *, now: float) -> None:
        record = await self.store.load(self.descriptor.id, self.state_key)
        if record is None:
            self.applicability = ProviderApplicability()
            return
        value = self._deserialize(record)
        if record.is_stale(now=now) or not value["usable"]:
            self.applicability = ProviderApplicability()
            return
        self.applicability = ProviderApplicability(
            specialized_hosts=tuple(
                HostClaim(domain, HostClaimScope.DOMAIN, frozenset({"https"}))
                for domain in value["domains"]
            ),
        )

    def set_enabled(self, enabled: bool) -> None:
        self.descriptor = replace(self.descriptor, enabled=enabled)
