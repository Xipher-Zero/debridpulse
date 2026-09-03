"""Integration discovery and capability routing, independent of concrete plugins."""
from __future__ import annotations

from transfers.applicability import (
    ProviderApplicabilityInput, classify_provider_applicability,
)
from transfers.contracts import (
    ApplicabilitySource, CandidateRefresh, Cleanup, Executor, Health, Inventory, PauseResume, Provider,
    RequestApplicabilitySource, ResourceLookup, Manifest,
)
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage, TransferError
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
        if not descriptor.id or descriptor.id in self.providers or descriptor.id in self.executors:
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
        if not descriptor.id or descriptor.id in self.executors or descriptor.id in self.providers or not descriptor.schemes:
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

    @staticmethod
    def _provider_selection_key(provider: Provider, request: TransferRequest):
        """Established neutral same-class provider ordering."""
        return (
            provider.descriptor.id != request.preferred_provider,
            -provider.descriptor.priority,
            provider.descriptor.id,
        )

    @staticmethod
    def _applicability_for(provider: Provider, request: TransferRequest):
        # A request-aware source handles genuine provider-native semantics
        # (for example path-sensitive support) locally and exposes only the
        # neutral applicability value. Static sources retain the Item 6
        # snapshot contract; providers with neither use request_types only.
        if isinstance(provider, RequestApplicabilitySource):
            return provider.applicability_for(request)
        if isinstance(provider, ApplicabilitySource):
            return provider.applicability
        return None

    def eligible_providers(self, request: TransferRequest, *, capability: Capability = Capability.RESOLVE) -> tuple[Provider, ...]:
        # Existing health semantics are a routing precondition: disabled,
        # unhealthy, incapable, or request-type-incompatible providers never
        # participate in applicability class construction.
        candidates = [
            provider for provider in self.providers.values()
            if provider.descriptor.enabled
            and provider.descriptor.id not in self._unhealthy
            and capability in provider.descriptor.capabilities
            and request.kind in provider.descriptor.request_types
        ]

        inputs = tuple(
            ProviderApplicabilityInput(
                provider.descriptor.id,
                provider.descriptor.request_types,
                provider.descriptor.enabled,
                self._applicability_for(provider, request),
            )
            for provider in candidates
        )
        applicable_ids = {
            match.provider_id for match in classify_provider_applicability(request, inputs)
        }

        # Item 6 returns only one applicable class: SPECIALIZED when any such
        # match exists, otherwise GENERIC (or STATIC for non-URL request types).
        # Filter first so generic providers never enter ordinary selection when
        # a specialized set exists. Only the resulting same-class set reaches
        # the established neutral preference/priority/stable-identity policy.
        applicable = [
            provider for provider in candidates
            if provider.descriptor.id in applicable_ids
        ]
        applicable.sort(key=lambda provider: self._provider_selection_key(provider, request))
        return tuple(applicable)

    def provider_for(self, request: TransferRequest) -> Provider:
        providers = self.eligible_providers(request)
        if not providers:
            raise TransferError(NormalizedError(
                Domain.REQUEST, Category.UNSUPPORTED_REQUEST, Stage.RESOLUTION,
                retryability=Retryability.NEVER,
            ))
        return providers[0]

    def _provider_for_bound_owner(self, provider_id: str, request: TransferRequest, *, require_health: bool) -> Provider:
        provider = self.providers.get(provider_id)
        if (provider is None or Capability.RESOLVE not in provider.descriptor.capabilities
                or request.kind not in provider.descriptor.request_types):
            raise TransferError(NormalizedError(
                Domain.REQUEST, Category.UNSUPPORTED_CAPABILITY, Stage.RESOLUTION,
                retryability=Retryability.NEVER, recovery=Recovery.FAIL, integration_id=provider_id,
            ))
        if not provider.descriptor.enabled:
            # Administrative disablement is an explicit admitted-work hard stop;
            # it never reopens provider competition for an existing route.
            raise TransferError(NormalizedError(
                Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION,
                retryability=Retryability.NEVER, recovery=Recovery.FAIL,
                integration_id=provider_id,
            ))
        if require_health and provider_id in self._unhealthy:
            raise TransferError(NormalizedError(
                Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION,
                retryability=Retryability.BACKOFF, recovery=Recovery.BACKOFF,
                integration_id=provider_id,
            ))
        return provider

    def provider_for_bound_route(self, provider_id: str, request: TransferRequest) -> Provider:
        """Recover an existing route without reopening global provider selection."""
        return self._provider_for_bound_owner(provider_id, request, require_health=True)

    def provider_for_bound_continuation(self, provider_id: str, request: TransferRequest) -> Provider:
        """Continue provider-owned interaction through its persisted route owner.

        Applicability and transient health can change while a human supplies input.
        Neither fact is route ownership. Administrative disablement remains an
        explicit hard stop, but no replacement provider is ever selected here.
        """
        return self._provider_for_bound_owner(provider_id, request, require_health=False)

    def eligible_executors(self, candidate: TransferCandidate) -> tuple[Executor, ...]:
        schemes = {endpoint.scheme for endpoint in candidate.endpoints}
        matches = [executor for executor in self.executors.values()
                   if executor.descriptor.enabled and executor.descriptor.id not in self._unhealthy
                   and schemes & executor.descriptor.schemes]
        return tuple(sorted(matches, key=lambda item: (-item.descriptor.priority, item.descriptor.id)))

    def executor_for(self, candidate: TransferCandidate) -> Executor:
        matches = self.eligible_executors(candidate)
        if not matches:
            raise TransferError(NormalizedError(
                Domain.REQUEST, Category.UNSUPPORTED_CAPABILITY, Stage.QUEUE,
                retryability=Retryability.NEVER,
            ))
        return matches[0]
