"""Integration discovery and capability routing, independent of concrete plugins."""
from __future__ import annotations

from transfers.applicability import (
    ApplicabilityClass,
    ApplicabilityUnresolved,
    ProviderApplicabilityInput,
    assess_provider_applicability,
)
from transfers.contracts import (
    ApplicabilitySource, CandidateRefresh, CandidateSampling, CandidateSamplingContinuation, Cleanup, Executor,
    ExecutorAcquisitionGate, ExecutorBandwidthControl, ExecutorInputContinuation, ExecutorInputRecovery,
    ExecutorNativeRetry, Health, Inventory, PauseResume, Provider, RequestApplicabilitySource, ResourceLookup,
    Manifest,
)
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.models import (
    Capability, ExecutionSubject, ExecutorCapabilities, ExecutorClaim, ExecutorRuntimeCapability, TransferRequest,
)


_PROVIDER_CAPABILITIES = {
    Capability.REFRESH: CandidateRefresh, Capability.CLEANUP: Cleanup,
    Capability.INVENTORY: Inventory, Capability.HEALTH: Health,
    Capability.RESOURCE_LOOKUP: ResourceLookup,
    Capability.METADATA: Manifest,
    # A neutral early file-manifest requires ordinary resource observation; it
    # does not imply provider ownership of selection policy.
    Capability.FILE_MANIFEST: ResourceLookup,
}

# Every declared executor capability promises its neutral semantic operation(s).
_EXECUTOR_CAPABILITIES = {
    "candidate_sampling": (CandidateSampling,),
    "per_execution_pause": (PauseResume,),
    "acquisition_gate": (ExecutorAcquisitionGate,),
    "aggregate_bandwidth_ceiling": (ExecutorBandwidthControl,),
    "native_assisted_retry": (ExecutorNativeRetry,),
    "transient_input": (ExecutorInputContinuation, ExecutorInputRecovery),
}

# The runtime availability fact that may narrow each static capability.
RUNTIME_CAPABILITY = {
    "acquisition_gate": ExecutorRuntimeCapability.ACQUISITION_GATE,
    "aggregate_bandwidth_ceiling": ExecutorRuntimeCapability.AGGREGATE_BANDWIDTH_CEILING,
    "native_assisted_retry": ExecutorRuntimeCapability.NATIVE_ASSISTED_RETRY,
}


def declared_runtime_capabilities(capabilities: ExecutorCapabilities) -> frozenset[ExecutorRuntimeCapability]:
    """The runtime capabilities an executor may ever report available."""
    return frozenset(value for name, value in RUNTIME_CAPABILITY.items() if getattr(capabilities, name))


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
            raise TypeError("Executor must implement the generalized execution contract")
        descriptor = executor.descriptor
        if not descriptor.id or descriptor.id in self.executors or descriptor.id in self.providers:
            raise ValueError("Executor requires a unique identity")
        capabilities = executor.capabilities
        if not isinstance(capabilities, ExecutorCapabilities):
            raise TypeError("Executor must declare neutral executor capabilities")
        for name, protocols in _EXECUTOR_CAPABILITIES.items():
            if getattr(capabilities, name) and not all(isinstance(executor, item) for item in protocols):
                raise TypeError(f"Executor declares an unimplemented capability: {name}")
        if (capabilities.candidate_sampling and capabilities.transient_input
                and not isinstance(executor, CandidateSamplingContinuation)):
            raise TypeError("Executor declares input-continued sampling without implementing it")
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

    def _provider_selection(
        self,
        request: TransferRequest,
        *,
        capability: Capability = Capability.RESOLVE,
    ):
        # Existing health semantics are a routing precondition: disabled,
        # unhealthy, incapable, or request-type-incompatible providers never
        # participate in applicability class or readiness construction.
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
        assessment = assess_provider_applicability(request, inputs)
        applicable_ids = {match.provider_id for match in assessment.matches}

        # The classifier returns only one selectable class: SPECIALIZED when an
        # authoritative specialized match exists, otherwise GENERIC/STATIC.
        # When specialized applicability is unresolved it returns no generic
        # matches, making accidental fallback impossible at this boundary.
        applicable = [
            provider for provider in candidates
            if provider.descriptor.id in applicable_ids
        ]
        applicable.sort(key=lambda provider: self._provider_selection_key(provider, request))
        return tuple(applicable), assessment

    def collection_provider_for(self, requests: tuple[TransferRequest, ...]) -> Provider | None:
        """Select one specialized route owner for a logical request collection.

        Collection affinity is deliberately a higher-level decision than the
        single-request classifier. Each request still contributes only the
        provider-neutral applicability facts already used by normal routing.
        An authoritative specialized match anywhere closes generic competition
        for the collection; unresolved specialized readiness blocks generic
        work only when no authoritative specialized owner can yet be selected.
        """
        represented: dict[str, Provider] = {}
        unresolved: set[str] = set()
        preferred_ids = {
            request.preferred_provider for request in requests
            if request.preferred_provider
        }
        for request in requests:
            providers, assessment = self._provider_selection(request)
            unresolved.update(assessment.unresolved_specialized)
            specialized_ids = {
                match.provider_id for match in assessment.matches
                if match.classification == ApplicabilityClass.SPECIALIZED
            }
            for provider in providers:
                if provider.descriptor.id in specialized_ids:
                    represented[provider.descriptor.id] = provider

        if represented:
            return min(
                represented.values(),
                key=lambda provider: (
                    provider.descriptor.id not in preferred_ids,
                    -provider.descriptor.priority,
                    provider.descriptor.id,
                ),
            )
        if unresolved:
            raise ApplicabilityUnresolved(sorted(unresolved))
        return None

    def eligible_providers(self, request: TransferRequest, *, capability: Capability = Capability.RESOLVE) -> tuple[Provider, ...]:
        providers, _assessment = self._provider_selection(request, capability=capability)
        return providers

    def provider_for(self, request: TransferRequest) -> Provider:
        providers, assessment = self._provider_selection(request)
        if not providers:
            if assessment.unresolved_specialized:
                raise ApplicabilityUnresolved(assessment.unresolved_specialized)
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
                retryability=Retryability.NEVER, integration_id=provider_id,
            ))
        if not provider.descriptor.enabled:
            # Administrative disablement is an explicit admitted-work hard stop;
            # it never reopens provider competition for an existing route.
            raise TransferError(NormalizedError(
                Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION,
                retryability=Retryability.NEVER,
                integration_id=provider_id,
            ))
        if require_health and provider_id in self._unhealthy:
            raise TransferError(NormalizedError(
                Domain.PROVIDER, Category.PROVIDER_UNAVAILABLE, Stage.RESOLUTION,
                retryability=Retryability.BACKOFF,
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

    def claimants(self, subject: ExecutionSubject) -> tuple[Executor, ...]:
        """THE executor applicability router, used before and after
        materialization alike: viability, pre-writer evidence sampling and its
        input continuation, dispatch, executor-input continuation and recovery.

        Each enabled, healthy executor answers a pure claim over canonical
        subject facts; anything but an ``ExecutorClaim`` is no claim. The
        subject's neutral materialization shape must be one the executor
        declares. Ordering is core-owned: configured priority, then identity."""
        kind = subject.candidate.materialization
        matches = []
        for executor in self.executors.values():
            if not executor.descriptor.enabled or executor.descriptor.id in self._unhealthy:
                continue
            if kind not in executor.capabilities.materialization_kinds:
                continue
            try:
                claim = executor.claim(subject)
            except Exception:
                continue
            if isinstance(claim, ExecutorClaim) and claim.supported is True:
                matches.append(executor)
        return tuple(sorted(matches, key=lambda item: (-item.descriptor.priority, item.descriptor.id)))

    def executor_for_subject(self, subject: ExecutionSubject) -> Executor:
        matches = self.claimants(subject)
        if not matches:
            raise TransferError(NormalizedError(
                Domain.REQUEST, Category.UNSUPPORTED_CAPABILITY, Stage.QUEUE,
                retryability=Retryability.NEVER,
            ))
        return matches[0]

    def executor_for_handle(self, handle) -> Executor | None:
        """The ONE bound-execution seam: which executor owns work that ALREADY
        exists.

        Deliberately NOT ``claimants``. That router selects NEW work and
        therefore excludes disabled and unhealthy executors. An execution that
        is already durable is not new work -- it is bound to the executor named
        in its handle, and only that executor can observe, control, recover or
        cancel it. Resolving it through the claim router instead would make an
        integration's enabled toggle, or a transient health blip, silently
        strand executions that are mid-flight.

        Disabling an integration therefore means "no NEW participation"; it
        never cancels an owned execution, never implies pause intent, and never
        hides the owner. Returns ``None`` only when no such executor is
        registered at all, which callers treat as an ownership fault rather
        than as absence of work.
        """
        identity = getattr(handle, "executor_id", None) or ""
        return self.executors.get(identity)
