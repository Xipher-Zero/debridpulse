"""Integration discovery and capability routing, independent of concrete plugins."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import json
import logging

from transfers.applicability import (
    ApplicabilityClass,
    ApplicabilityUnresolved,
    ProviderApplicabilityInput,
    assess_provider_applicability,
)
from transfers.contracts import (
    ApplicabilitySource, AvailabilitySource, CandidateRefresh, EntitlementSource, RequestEntitlementSource, CandidateSampling, CandidateSamplingContinuation, Cleanup,
    ContinuationBoundaryDiscovery, Executor,
    ExecutorAcquisitionGate, ExecutorAggregateThroughput, ExecutorBandwidthControl, ExecutorInputContinuation,
    ExecutorInputRecovery, RemoteDiscovery,
    ExecutorNativeRetry, Health, Inventory, PauseResume, Provider, RequestApplicabilitySource, SpeculativePreparation,
    ResourceLookup, Manifest,
)
from transfers.entitlement import ProviderEntitlements
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.models import (
    BITTORRENT_REQUEST_KINDS, AvailabilityState, Capability, ContinuationCapability, ExecutionSubject, ExecutorCapabilities, ExecutorClaim,
    ExecutorRuntimeCapability, TransferRequest,
)


_PROVIDER_CAPABILITIES = {
    Capability.REFRESH: CandidateRefresh, Capability.CLEANUP: Cleanup,
    Capability.AVAILABILITY: AvailabilitySource,
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
    "aggregate_throughput": (ExecutorAggregateThroughput,),
    "native_assisted_retry": (ExecutorNativeRetry,),
    "transient_input": (ExecutorInputContinuation, ExecutorInputRecovery),
    "remote_discovery": (RemoteDiscovery,),
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


logger = logging.getLogger(__name__)


class RoutingDisposition(StrEnum):
    """Why one provider the selector considered did or did not take a request:
    the facts ``_provider_selection`` itself consumed, in its own order."""
    SELECTED = "selected"
    APPLICABLE_NOT_SELECTED = "applicable_not_selected"
    ENTITLEMENT_UNRESOLVED = "entitlement_unresolved"
    NOT_ENTITLED = "not_entitled"
    DISABLED = "disabled"
    UNHEALTHY = "unhealthy"
    EXHAUSTED = "exhausted"
    DECLINED = "declined"
    NOT_APPLICABLE = "not_applicable"
    APPLICABILITY_UNRESOLVED = "applicability_unresolved"
    HELD_BY_SPECIALIZED_AUTHORITY = "held_by_specialized_authority"


class RoutingOutcome(StrEnum):
    SELECTED = "selected"
    # Premature: unresolved specialized applicability or the first claimant's
    # unknown account entitlement (``ApplicabilityUnresolved``).
    HELD = "held"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ProviderDisposition:
    provider_id: str
    disposition: RoutingDisposition
    classification: ApplicabilityClass | None = None
    # The read-only availability the decision consumed for this provider;
    # ``None`` when none was observed for the request.
    availability: AvailabilityState | None = None


@dataclass(frozen=True)
class RoutingDecision:
    """One decision of the canonical selector, recorded as it was made: a
    neutral disposition per provider it considered (a provider that does not
    offer the request's class takes no part and is not listed). Bounded by
    the registered providers; never a provider-native or account fact."""
    outcome: RoutingOutcome
    providers: tuple[ProviderDisposition, ...]

    def encode(self) -> str:
        return json.dumps({"v": 1, "outcome": self.outcome.value, "providers": [
            {"provider_id": item.provider_id, "disposition": item.disposition.value,
             **({"class": item.classification.value} if item.classification else {}),
             **({"availability": item.availability.value} if item.availability else {})}
            for item in self.providers]}, separators=(",", ":"))


@dataclass(frozen=True)
class ProviderRoute:
    """The first provider of the canonical competition for one request, or
    why there is none, with the decision that produced it (``None`` for a
    bound route, which is never re-decided, or when it could not be
    recorded)."""
    provider: Provider | None
    decision: RoutingDecision | None = None
    unresolved: tuple[str, ...] = ()

    def require(self) -> Provider:
        if self.provider is not None:
            return self.provider
        if self.unresolved:
            raise ApplicabilityUnresolved(self.unresolved)
        raise TransferError(NormalizedError(
            Domain.REQUEST, Category.UNSUPPORTED_REQUEST, Stage.RESOLUTION,
            retryability=Retryability.NEVER,
        ))


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
        if (ContinuationCapability.BOUNDARY_DISCOVERY in capabilities.continuation
                and not isinstance(executor, ContinuationBoundaryDiscovery)):
            raise TypeError("Executor declares boundary discovery without implementing it")
        # Native quiesce and native private resume ARE the per-execution
        # pause/resume operations (validated above).
        if ((ContinuationCapability.NATIVE_QUIESCE in capabilities.continuation
             or ContinuationCapability.NATIVE_PRIVATE_RESUME in capabilities.continuation)
                and not capabilities.per_execution_pause):
            raise TypeError("Executor declares native quiesce or private resume without per-execution pause")
        self.executors[descriptor.id] = executor

    def mark_health(self, integration_id: str, *, healthy: bool) -> None:
        if healthy:
            self._unhealthy.discard(integration_id)
        else:
            self._unhealthy.add(integration_id)

    @staticmethod
    def _provider_selection_key(provider: Provider, request: TransferRequest, *, conditional: bool = False,
                                specific: bool = False):
        """Established neutral same-class provider ordering. A conditional
        claim (``ProviderApplicability.conditional``) competes before the
        unconditional claims of its class: they can never yield to it, so
        after them it would never get its probe. A specific claim
        (``ProviderApplicability.specific``) then competes before the
        scheme-wide ones, which would otherwise always win over it. Priority
        is the provider's for this request's kind
        (``IntegrationDescriptor.priority_for``): it only orders the providers
        already admitted here."""
        return (
            provider.descriptor.id != request.preferred_provider,
            not conditional,
            not specific,
            -provider.descriptor.priority_for(request.kind),
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

    @staticmethod
    def entitlement_for(provider: Provider, request: TransferRequest) -> bool | None:
        """Whether ``provider``'s CURRENT account may begin ``request``:
        ``True``/``False`` once its account truth is resolved, ``None`` while it
        is unknown. A provider with no account entitlement dimension is
        ``True`` -- its behaviour is exactly what it was."""
        if isinstance(provider, RequestEntitlementSource):
            return provider.entitlement_for(request)
        entitlements = provider.entitlements if isinstance(provider, EntitlementSource) else None
        if isinstance(entitlements, ProviderEntitlements):
            return entitlements.admits(request.kind)
        return True

    def _provider_selection(
        self,
        request: TransferRequest,
        *,
        capability: Capability = Capability.RESOLVE,
        declined: frozenset[str] = frozenset(),
        exhausted: frozenset[str] = frozenset(),
        acquisition: bool = True,
        generic_closed: bool = False,
        dispositions: dict | None = None,
        ready: frozenset[str] = frozenset(),
    ):
        # ``dispositions``, when given, receives every provider this decision
        # removed before classification and why -- the values it computes
        # below anyway, never by asking any provider again.
        # Existing health semantics are a routing precondition: disabled,
        # unhealthy, incapable, or request-type-incompatible providers never
        # participate in applicability class or readiness construction.
        # Neither does a provider already exhausted for this request in its
        # current routing campaign: it has had its whole route, so the class
        # is judged again among the providers that remain -- a generic
        # provider competes once no remaining specialized one claims. Nor,
        # for new ACQUISITION, does a provider whose current account is known
        # not to be entitled to the request: it cleanly yields exactly like an
        # exhausted one. A provider whose entitlement is still unknown stays
        # in the competition (``unresolved``), so no lower fallback can win
        # merely because its account truth has not arrived yet. A member of a
        # route that already exists is not new acquisition: entitlement never
        # touches it. A request whose collection specialized authority owns
        # (``generic_closed``) has had generic competition closed by that
        # authority: its selection -- first or after an exhaustion or a
        # decline -- judges only its own remaining specialized claimants,
        # never reopens a generic one.
        entitlement = {}
        candidates = []
        for provider in self.providers.values():
            if not (provider.descriptor.enabled
                    and provider.descriptor.id not in self._unhealthy
                    and provider.descriptor.id not in exhausted
                    and capability in provider.descriptor.capabilities
                    and request.kind in provider.descriptor.request_types):
                if (dispositions is not None and capability in provider.descriptor.capabilities
                        and request.kind in provider.descriptor.request_types):
                    dispositions[provider.descriptor.id] = (
                        RoutingDisposition.DISABLED if not provider.descriptor.enabled
                        else RoutingDisposition.UNHEALTHY if provider.descriptor.id in self._unhealthy
                        else RoutingDisposition.EXHAUSTED)
                continue
            entitled = self.entitlement_for(provider, request) if acquisition else True
            if entitled is False:
                if dispositions is not None:
                    dispositions[provider.descriptor.id] = RoutingDisposition.NOT_ENTITLED
                continue
            entitlement[provider.descriptor.id] = entitled
            candidates.append(provider)

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
        matches = tuple(match for match in assessment.matches
                        if not (generic_closed and match.classification == ApplicabilityClass.GENERIC))
        conditional = {match.provider_id: match.conditional for match in matches}
        specific = {match.provider_id: match.specific for match in matches}

        # The classifier returns only one selectable class: SPECIALIZED when an
        # authoritative specialized match exists, otherwise GENERIC/STATIC.
        # When specialized applicability is unresolved it returns no generic
        # matches, making accidental fallback impossible at this boundary.
        # A provider that positively declined this request after its probe
        # leaves the competition it was in; the class itself never changes.
        applicable = [
            provider for provider in candidates
            if provider.descriptor.id in conditional and provider.descriptor.id not in declined
        ]
        applicable.sort(key=lambda provider: self._provider_selection_key(
            provider, request, conditional=conditional[provider.descriptor.id],
            specific=specific[provider.descriptor.id]))
        # Read-only availability may only PREFER: where it orders at all
        # (``availability_orders``), the READY competitors whose entitlement is
        # established come first, each group keeping the established order
        # (a stable sort); no READY changes nothing. It never moves a provider
        # whose entitlement is still unknown ahead of anyone, and when the
        # established winner's own entitlement is unknown the decision stays
        # exactly as premature as it was.
        if (ready and applicable and self.availability_orders(request)
                and entitlement[applicable[0].descriptor.id] is not None):
            prefer = frozenset(provider_id for provider_id in ready if entitlement.get(provider_id) is True)
            applicable.sort(key=lambda provider: provider.descriptor.id not in prefer)
        unresolved = frozenset(provider.descriptor.id for provider in applicable
                               if entitlement[provider.descriptor.id] is None)
        return tuple(applicable), assessment, unresolved

    @staticmethod
    def speculative_preparation_allowed(provider: Provider, request: TransferRequest) -> bool:
        """Whether ``provider`` allows ``request`` to be prepared
        speculatively. Fail-closed: only the provider's own ``True`` allows it;
        a provider without the contract, any other answer, or an answer that
        raises is not eligible."""
        if not isinstance(provider, SpeculativePreparation):
            return False
        try:
            return provider.speculative_preparation_allowed(request) is True
        except Exception:
            return False

    @staticmethod
    def availability_orders(request: TransferRequest) -> bool:
        """Whether read-only availability may change the provider order for
        ``request``: only for a BitTorrent-class root, where every claimant's
        READY answer means the same thing (the swarm's content is already
        held). A hoster root keeps its established winner: one provider being
        able to read a cache while another has no such read is no evidence
        that the other cannot deliver."""
        return request.kind in BITTORRENT_REQUEST_KINDS

    def conditional_claim(self, provider: Provider, request: TransferRequest) -> bool:
        """Whether ``provider``'s claim on ``request`` is conditional -- the
        only kind of claim a provider may decline after its probe."""
        facts = self._applicability_for(provider, request)
        return bool(getattr(facts, "conditional", False))

    def _decision(self, request, route, providers, assessment, unknown, removed, declined,
                  generic_closed, availability=None) -> RoutingDecision:
        """Describe the decision ``_provider_selection`` just made, from its
        own results: the providers it removed before classification, the
        classifier's assessment, and the ordered competition. Pure -- no
        provider is asked anything."""
        classified = {match.provider_id: match.classification for match in assessment.matches}
        competing = {provider_id for provider_id, classification in classified.items()
                     if not (generic_closed and classification == ApplicabilityClass.GENERIC)}
        held = set(assessment.held_generic) | (set(classified) - competing)
        found = dict(removed)
        for provider in self.providers.values():
            provider_id = provider.descriptor.id
            if (provider_id in found or Capability.RESOLVE not in provider.descriptor.capabilities
                    or request.kind not in provider.descriptor.request_types):
                continue
            found[provider_id] = (
                (RoutingDisposition.DECLINED, classified[provider_id])
                if provider_id in competing and provider_id in declined
                else RoutingDisposition.HELD_BY_SPECIALIZED_AUTHORITY if provider_id in held
                else RoutingDisposition.APPLICABILITY_UNRESOLVED if provider_id in assessment.unresolved_specialized
                else RoutingDisposition.NOT_APPLICABLE)
        for index, provider in enumerate(providers):
            provider_id = provider.descriptor.id
            found[provider_id] = (
                RoutingDisposition.ENTITLEMENT_UNRESOLVED if provider_id in unknown
                else RoutingDisposition.SELECTED if index == 0
                else RoutingDisposition.APPLICABLE_NOT_SELECTED, classified[provider_id])
        outcome = (RoutingOutcome.SELECTED if route.provider is not None
                   else RoutingOutcome.HELD if route.unresolved else RoutingOutcome.UNSUPPORTED)
        # The competition in its own order, then everyone else by identity.
        order = {provider.descriptor.id: index for index, provider in enumerate(providers)}
        entries = []
        for provider_id in sorted(found, key=lambda item: (order.get(item, len(order)), item)):
            value = found[provider_id]
            disposition, classification = value if isinstance(value, tuple) else (value, None)
            entries.append(ProviderDisposition(provider_id, disposition, classification,
                                               (availability or {}).get(provider_id)))
        return RoutingDecision(outcome, tuple(entries))

    def collection_route_authority(self, requests: tuple[TransferRequest, ...]) -> bool:
        """Whether specialized authority owns the routing of a logical request
        collection -- a fact about the whole submission, never about one
        provider.

        An authoritative specialized claimant of ANY root closes generic
        competition for EVERY root -- unless its claim declares that it speaks
        only for the object it names (``ProviderApplicability
        .collection_authority``), in which case it routes that root and leaves
        the others to their own claimants. Which specialized provider takes a root
        stays that root's own competition (``provider_route`` with
        ``generic_closed``): a per-root union, so no provider has to claim the
        whole collection, a provider that cannot claim one root still competes
        for the others, and a root no specialized provider claims is
        unsupported. Each request contributes only the provider-neutral facts
        normal routing uses. Unresolved specialized readiness, or a claimant
        whose account entitlement is not yet known, keeps the decision
        premature only while no root has an entitled authoritative claimant.
        """
        pending: set[str] = set()
        for request in requests:
            providers, assessment, unknown = self._provider_selection(request)
            specialized_ids = {
                match.provider_id for match in assessment.matches
                if match.classification == ApplicabilityClass.SPECIALIZED and match.collection_authority
            }
            claimants = {provider.descriptor.id for provider in providers} & specialized_ids
            if claimants - unknown:
                return True
            pending.update(claimants)
            pending.update(assessment.unresolved_specialized)
        if pending:
            raise ApplicabilityUnresolved(sorted(pending))
        return False

    def eligible_providers(self, request: TransferRequest, *, capability: Capability = Capability.RESOLVE,
                           declined: frozenset[str] = frozenset(),
                           exhausted: frozenset[str] = frozenset(),
                           acquisition: bool = True, generic_closed: bool = False) -> tuple[Provider, ...]:
        """Every provider still in the competition for ``request``, in order --
        including one whose account entitlement is not yet known: it remains
        a possible owner, so the request waits for it rather than ending."""
        providers, _assessment, _unknown = self._provider_selection(
            request, capability=capability, declined=declined, exhausted=exhausted, acquisition=acquisition,
            generic_closed=generic_closed)
        return providers

    def provider_for(self, request: TransferRequest, *, declined: frozenset[str] = frozenset(),
                     exhausted: frozenset[str] = frozenset(), acquisition: bool = True,
                     generic_closed: bool = False) -> Provider:
        """The first provider of the one established competition for
        ``request``, without the providers that positively declined it or
        were exhausted for it in its current routing campaign. When that
        first provider's account entitlement is still unknown the decision
        is premature, exactly like unresolved specialized applicability."""
        return self._route(request, declined=declined, exhausted=exhausted, acquisition=acquisition,
                           generic_closed=generic_closed).require()

    def provider_route(self, request: TransferRequest, *, declined: frozenset[str] = frozenset(),
                       exhausted: frozenset[str] = frozenset(), acquisition: bool = True,
                       generic_closed: bool = False,
                       availability: dict[str, AvailabilityState] | None = None) -> ProviderRoute:
        """``provider_for``'s one decision, not raised: its provider (or why
        there is none) and the routing decision that produced it.
        ``availability`` -- the read-only answers of the providers competing
        for ``request`` -- may prefer a READY competitor where
        ``availability_orders``; it never adds, removes or exhausts one."""
        return self._route(request, declined=declined, exhausted=exhausted, acquisition=acquisition,
                           generic_closed=generic_closed, record=True, availability=availability)

    def _route(self, request: TransferRequest, *, declined, exhausted, acquisition, generic_closed,
               record: bool = False, availability: dict[str, AvailabilityState] | None = None) -> ProviderRoute:
        dispositions = {} if record else None
        providers, assessment, unknown = self._provider_selection(
            request, declined=declined, exhausted=exhausted, acquisition=acquisition,
            generic_closed=generic_closed, dispositions=dispositions,
            ready=frozenset(provider_id for provider_id, state in (availability or {}).items()
                            if state == AvailabilityState.READY))
        if providers and providers[0].descriptor.id in unknown:
            route = ProviderRoute(None, unresolved=(providers[0].descriptor.id,))
        elif not providers:
            route = ProviderRoute(None, unresolved=tuple(assessment.unresolved_specialized) or self.unhealthy_claimants(
                request, declined=declined, exhausted=exhausted, acquisition=acquisition,
                generic_closed=generic_closed))
        else:
            route = ProviderRoute(providers[0])
        if dispositions is None:
            return route
        try:
            return replace(route, decision=self._decision(request, route, providers, assessment, unknown,
                                                          dispositions, declined, generic_closed, availability))
        except Exception as exc:  # visibility must never change the decision
            logger.debug("routing decision could not be described: %s", type(exc).__name__)
            return route

    def unhealthy_claimants(self, request: TransferRequest, *, declined: frozenset[str] = frozenset(),
                            exhausted: frozenset[str] = frozenset(), acquisition: bool = True,
                            generic_closed: bool = False) -> tuple[str, ...]:
        """The specialized claimants of ``request`` that only health keeps out
        of its competition, when collection route authority closed generic
        competition: they still remain for it, so the request waits for them
        (held, like any premature decision) instead of being judged
        unsupported -- health is transient, and no generic provider may take
        the request meanwhile. Route selection and the exhaustion handoff
        both ask this one question. Without that authority, nothing changes:
        ``()``."""
        if not generic_closed:
            return ()
        waiting = tuple(
            provider for provider in self.providers.values()
            if provider.descriptor.id in self._unhealthy
            and provider.descriptor.enabled
            and provider.descriptor.id not in exhausted
            and provider.descriptor.id not in declined
            and Capability.RESOLVE in provider.descriptor.capabilities
            and request.kind in provider.descriptor.request_types
            and not (acquisition and self.entitlement_for(provider, request) is False))
        if not waiting:
            return ()
        assessment = assess_provider_applicability(request, tuple(
            ProviderApplicabilityInput(provider.descriptor.id, provider.descriptor.request_types,
                                       provider.descriptor.enabled, self._applicability_for(provider, request))
            for provider in waiting))
        return tuple(sorted(match.provider_id for match in assessment.matches
                            if match.classification == ApplicabilityClass.SPECIALIZED))

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
