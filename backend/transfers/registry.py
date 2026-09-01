"""Integration discovery and capability routing, independent of concrete plugins."""
from __future__ import annotations

from transfers.contracts import (
    CandidateRefresh, Cleanup, Executor, Health, Inventory, PauseResume, Provider,
    ResourceLookup, Manifest,
)
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.models import Capability, TransferCandidate, TransferRequest


_PROVIDER_CAPABILITIES = {
    Capability.REFRESH: CandidateRefresh, Capability.CLEANUP: Cleanup,
    Capability.INVENTORY: Inventory, Capability.HEALTH: Health,
    Capability.RESOURCE_LOOKUP: ResourceLookup,
    Capability.METADATA: Manifest,
}


class IntegrationRegistry:
    def __init__(self):
        self.providers: dict[str, Provider] = {}
        self.executors: dict[str, Executor] = {}
        self._unhealthy: set[str] = set()

    def register_provider(self, provider: Provider) -> None:
        if not isinstance(provider, Provider):
            raise TypeError("Provider must implement resolution and descriptor contracts")
        descriptor = provider.descriptor
        if not descriptor.id or descriptor.id in self.providers:
            raise ValueError("Provider identity is missing or already registered")
        if Capability.RESOLVE not in descriptor.capabilities or not descriptor.request_types:
            raise ValueError("Provider must declare resolution and supported request types")
        for capability, protocol in _PROVIDER_CAPABILITIES.items():
            if capability in descriptor.capabilities and not isinstance(provider, protocol):
                raise TypeError(f"Provider declares an unimplemented capability: {capability}")
        self.providers[descriptor.id] = provider

    def register_executor(self, executor: Executor) -> None:
        if not isinstance(executor, Executor):
            raise TypeError("Executor must implement execution and descriptor contracts")
        descriptor = executor.descriptor
        if not descriptor.id or descriptor.id in self.executors or not descriptor.schemes:
            raise ValueError("Executor requires a unique identity and supported schemes")
        if ({Capability.PAUSE, Capability.RESUME} & descriptor.capabilities
                and not isinstance(executor, PauseResume)):
            raise TypeError("Executor declares unimplemented pause/resume capabilities")
        self.executors[descriptor.id] = executor

    def mark_health(self, integration_id: str, *, healthy: bool) -> None:
        if healthy:
            self._unhealthy.discard(integration_id)
        else:
            self._unhealthy.add(integration_id)

    def eligible_providers(self, request: TransferRequest, *, capability: Capability = Capability.RESOLVE) -> tuple[Provider, ...]:
        matches = [provider for provider in self.providers.values()
                   if provider.descriptor.enabled and provider.descriptor.id not in self._unhealthy
                   and request.kind in provider.descriptor.request_types
                   and capability in provider.descriptor.capabilities]
        return tuple(sorted(matches, key=lambda provider: (
            provider.descriptor.id != request.preferred_provider,
            -provider.descriptor.priority, provider.descriptor.id,
        )))

    def provider_for(self, request: TransferRequest) -> Provider:
        providers = self.eligible_providers(request)
        if not providers:
            raise TransferError(NormalizedError(
                Domain.REQUEST, Category.UNSUPPORTED_REQUEST, Stage.RESOLUTION,
                retryability=Retryability.NEVER,
            ))
        return providers[0]

    def executor_for(self, candidate: TransferCandidate) -> Executor:
        schemes = {endpoint.scheme for endpoint in candidate.endpoints}
        matches = [executor for executor in self.executors.values()
                   if executor.descriptor.enabled and executor.descriptor.id not in self._unhealthy
                   and schemes & executor.descriptor.schemes]
        if not matches:
            raise TransferError(NormalizedError(
                Domain.REQUEST, Category.UNSUPPORTED_CAPABILITY, Stage.QUEUE,
                retryability=Retryability.NEVER,
            ))
        return sorted(matches, key=lambda item: (-item.descriptor.priority, item.descriptor.id))[0]
