"""AllDebrid resolution implementation; no transfer state or policy ownership."""
from __future__ import annotations

from dataclasses import replace
from functools import wraps
from urllib.parse import urlsplit

from providers.alldebrid.account import FREE_HOST, HOSTERS, refused_family
from providers.alldebrid.client import AllDebridService, API_V4
from services.network_safety import validate_provider_download_url
from providers.alldebrid.translation import (
    cache_presence_from_upload, collection_root_name, file_manifest_from_files_response,
    native_members, observation_from_native, resource_from_native, translate_error,
)
from transfers.applicability import ProviderApplicability
from transfers.entitlement import AccountServiceClass, ProviderEntitlements
from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage, TransferError
from transfers.models import (
    Capability, CleanupAuthority, CleanupDirective, DeliveryKind, Endpoint, HealthObservation,
    IntegrationDescriptor, OutcomeKind, Ownership, ProviderObservation,
    ProviderResource, ResolutionResult, ResolverArtifactIdentityEvidence, ResourceSnapshot, ResourceState,
    SourceEntry, SourceIdentity, TransferCandidate, TransferOutcome, TransferRequest,
)


def normalized_boundary(stage):
    """Contain malformed native payloads as well as explicit client failures."""
    def decorate(operation):
        @wraps(operation)
        async def invoke(self, *args, **kwargs):
            try:
                return await operation(self, *args, **kwargs)
            except TransferError:
                raise
            except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
                raise TransferError(NormalizedError(
                    Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE, stage,
                    origin=Origin.PROVIDER, integration_id=self.descriptor.id,
                )) from None
            except Exception as exc:
                raise TransferError(translate_error(exc, stage=stage, secrets=self._secrets)) from None
        return invoke
    return decorate


class AllDebridProvider:
    # HTTP(S) applicability is runtime-derived from AllDebrid's own validated
    # supported-host snapshot. Magnet/torrent remain descriptor request types.
    applicability = ProviderApplicability()

    def __init__(self, api_key: str = "", agent: str = "DebridPulse", *, client=None,
                 rate_limit_per_minute: int = 60):
        self.client = client if client is not None else AllDebridService(
            api_key, agent, rate_limit_per_minute=rate_limit_per_minute,
        )
        self._secrets = (api_key,)
        self.descriptor = IntegrationDescriptor(
            "alldebrid", "AllDebrid",
            frozenset({Capability.RESOLVE, Capability.REFRESH, Capability.METADATA,
                       Capability.FILE_MANIFEST, Capability.RESOURCE_CREATION,
                       Capability.RESOURCE_LOOKUP, Capability.INVENTORY,
                       Capability.CLEANUP, Capability.HEALTH}),
            request_types=frozenset({"magnet", "torrent", "http", "https"}),
            enabled=bool(api_key) or client is not None,
        )

    async def _call(self, operation, *args, stage=Stage.RESOLUTION, **kwargs):
        try:
            return await operation(*args, **kwargs)
        except Exception as exc:
            raise TransferError(translate_error(exc, stage=stage, secrets=self._secrets)) from None

    @property
    def entitlements(self):
        """What the connected account may begin now, kept by its account
        owner (``integrations.account_entitlement``) from AllDebrid's own
        account semantics (``providers.alldebrid.account``); ``None`` -- no
        account dimension -- for an instance built without one."""
        owner = getattr(self, "account", None)
        current = owner.entitlements if owner is not None else None
        if isinstance(current, ProviderEntitlements) and current.service_class == AccountServiceClass.STANDARD:
            # The same per-host narrowing ``entitlement_for`` applies, stated
            # for the account as a whole: what a non-premium account can still use.
            surface = getattr(getattr(self, "applicability_for", None), "free_surface", None)
            if callable(surface):
                current = current.with_surface(HOSTERS, surface())
        return current

    def entitlement_for(self, request: TransferRequest) -> bool | None:
        """The account's entitlement, narrowed per host: a non-premium
        account may unlock only links of hosts AllDebrid types ``free``.
        Which host a link belongs to is the host inventory's structural
        answer; it never widens what the account is entitled to."""
        current = self.entitlements
        if not isinstance(current, ProviderEntitlements):
            return True
        admitted = current.admits(request.kind)
        if admitted and request.kind in HOSTERS and current.service_class == AccountServiceClass.STANDARD:
            host_type = getattr(getattr(self, "applicability_for", None), "host_type", None)
            matched = host_type(request) if callable(host_type) else None
            if matched is not None:
                return matched == FREE_HOST
        return admitted

    @normalized_boundary(Stage.RESOLUTION)
    async def resolve(self, request: TransferRequest) -> ResolutionResult:
        if request.kind in {"http", "https"}:
            native = await self._call(self.client.unlock_link, str(request.payload))
            try:
                endpoint = validate_provider_download_url(native.get("link"))
                size = max(0, int(native.get("filesize") or native.get("size") or 0))
            except Exception as exc:
                raise TransferError(translate_error(exc, secrets=self._secrets)) from None
            # Resolver-attested identity evidence (DP 1.0.12 canonical
            # architecture correction, Workstream B): only populated from a
            # name AllDebrid's resolution response itself asserted, never the
            # submitted-URL/request-name fallback below -- an unlock response
            # lacking a native filename carries no resolver identity fact.
            native_name = native.get("filename") or native.get("name")
            # Evidence requires BOTH a resolver-asserted name AND an exact
            # positive size (specification section 8.1: "exact positive byte
            # size"); mirrors.py's consumer already refuses a non-positive
            # size as proof, so a name-only emission here never currently
            # causes a false consolidation -- but the evidence object itself
            # must not overstate what the resolver actually asserted.
            resolver_evidence = (
                ResolverArtifactIdentityEvidence(resolved_name=str(native_name), exact_bytes=size)
                if native_name and size > 0 else None
            )
            candidate = TransferCandidate(
                str(native_name or request.name),
                (Endpoint(urlsplit(endpoint).scheme, endpoint),), size,
                provider_id=self.descriptor.id, refresh_request=request,
                source_identity=SourceIdentity("host", str(urlsplit(str(request.payload)).hostname or "").casefold().removeprefix("www.").rstrip(".")),
                resolver_identity_evidence=resolver_evidence,
                # The unlocked endpoint is AllDebrid's own delivery capability;
                # the requested hoster is identified by ``source_identity``.
                delivery=DeliveryKind.PROVIDER_ISSUED,
            )
            return ResolutionResult(ResourceState.AVAILABLE, (candidate,))
        try:
            if request.kind == "magnet":
                native = await self._call(self.client.upload_magnet, str(request.payload))
            elif request.kind == "torrent" and isinstance(request.payload, bytes):
                native = await self._call(self.client.upload_torrent_file, request.payload, request.name)
            else:
                native = None
        except TransferError as exc:
            # A refusal of the torrent feature itself still fails this route
            # the ordinary way; it only also tells the account owner.
            family, owner = refused_family(exc.error.native_code, request.kind), getattr(self, "account", None)
            if family and owner is not None:
                await owner.contract(family)
            raise
        if native is None:
            raise TransferError(NormalizedError(Domain.REQUEST, Category.UNSUPPORTED_REQUEST,
                                                Stage.SUBMISSION, Retryability.NEVER,
                                                origin=Origin.USER, integration_id=self.descriptor.id))
        resource = resource_from_native(native, ownership=Ownership.CREATED)
        observation = replace(observation_from_native(native, resource=resource, request=request),
                              cache_presence=cache_presence_from_upload(native))
        return ResolutionResult(observation.state, observation=observation, error=observation.error)

    def _native_id(self, resource: ProviderResource) -> str:
        if resource.provider_id != self.descriptor.id or not resource.context.get("id"):
            raise TransferError(NormalizedError(Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE,
                                                Stage.RECONCILIATION, integration_id=self.descriptor.id))
        return str(resource.context["id"])

    @normalized_boundary(Stage.RECONCILIATION)
    async def observe(self, resource: ProviderResource) -> ProviderObservation:
        native_id = self._native_id(resource)
        try:
            records = await self._call(self.client.get_magnet_status, native_id, stage=Stage.RECONCILIATION)
        except TransferError as exc:
            if exc.error.category == Category.RESOURCE_NOT_FOUND:
                return ProviderObservation(resource, ResourceState.ABSENT, error=exc.error)
            raise
        matches = [record for record in records if str(record.get("id")) == native_id]
        if not matches:
            if records:
                raise TransferError(NormalizedError(Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE,
                                                    Stage.RECONCILIATION, integration_id=self.descriptor.id))
            return ProviderObservation(resource, ResourceState.ABSENT)
        observation = observation_from_native(matches[0], resource=resource)
        # Provider-local fallback: an AVAILABLE status without an inline file tree
        # can still yield the complete neutral manifest from the existing file
        # endpoint. Core never learns which endpoint produced the facts.
        if observation.state == ResourceState.AVAILABLE and observation.file_manifest is None:
            try:
                files = await self._call(self.client.get_magnet_files, [native_id],
                                         stage=Stage.CANDIDATE_PREPARATION)
            except TransferError:
                files = None
            tree = file_manifest_from_files_response(files, native_id,
                                                     root_name=observation.name)
            if tree is not None:
                observation = replace(observation, file_manifest=tree)
        return observation

    @normalized_boundary(Stage.RECONCILIATION)
    async def inventory(self) -> ResourceSnapshot:
        records = await self._call(self.client.get_magnet_status, stage=Stage.RECONCILIATION)
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise TransferError(NormalizedError(Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE,
                                                Stage.RECONCILIATION, integration_id=self.descriptor.id))
        # Ordering and the provider's limited bulk window terminate here.
        records = sorted(records, key=lambda item: int(item.get("id") or 0))
        return ResourceSnapshot(tuple(observation_from_native(record) for record in records), complete=False)

    @normalized_boundary(Stage.CANDIDATE_PREPARATION)
    async def manifest(self, resource: ProviderResource) -> tuple[SourceEntry, ...]:
        """Executable members, in the SAME collection-root-relative coordinate
        system the early ``FileManifest`` already published.

        ``/v4/magnet/files`` carries no name fact of its own, so the
        authoritative root name comes from this provider's own resource
        context, which its observations enrich (``translation.with_root_name``).
        """
        native_id = self._native_id(resource)
        root_name = collection_root_name(resource)
        records = await self._call(self.client.get_magnet_files, [native_id], stage=Stage.CANDIDATE_PREPARATION)
        entries = []
        try:
            for record in records:
                if str(record.get("id")) != native_id:
                    continue
                for member in native_members(record.get("files") or [], root_name=root_name,
                                             require_link=True):
                    request = TransferRequest(urlsplit(member.link).scheme, member.link, member.name,
                                              preferred_provider=self.descriptor.id)
                    entries.append(SourceEntry(member.name, member.expected_bytes,
                                               member.relative_path, request))
        except Exception as exc:
            raise TransferError(translate_error(exc, stage=Stage.CANDIDATE_PREPARATION,
                                                secrets=self._secrets)) from None
        return tuple(entries)

    @normalized_boundary(Stage.CANDIDATE_PREPARATION)
    async def refresh(self, candidate: TransferCandidate) -> ResolutionResult:
        if candidate.provider_id != self.descriptor.id or candidate.refresh_request is None:
            raise TransferError(NormalizedError(Domain.RESOLUTION, Category.UNSUPPORTED_CAPABILITY,
                                                Stage.CANDIDATE_PREPARATION, Retryability.NEVER,
                                                integration_id=self.descriptor.id))
        result = await self.resolve(candidate.refresh_request)
        return replace(result, candidates=tuple(replace(item, relative_path=candidate.relative_path,
                                                        resource=candidate.resource,
                                                        id=candidate.id if index == 0 else item.id) for index, item in enumerate(result.candidates)))

    @normalized_boundary(Stage.CLEANUP)
    async def cleanup(self, directive: CleanupDirective) -> TransferOutcome:
        resource = directive.resource
        if (directive.authority == CleanupAuthority.OWNED
                and resource.ownership not in {Ownership.CREATED, Ownership.ADOPTED}):
            return TransferOutcome(OutcomeKind.SKIPPED, detail="Observed provider resource retained")
        native_id = self._native_id(resource)
        try:
            await self._call(self.client._post, API_V4, "magnet/delete", {"id": native_id}, stage=Stage.CLEANUP)
        except TransferError as exc:
            if exc.error.category != Category.RESOURCE_NOT_FOUND:
                return TransferOutcome(OutcomeKind.FAILURE, exc.error)
        return TransferOutcome(OutcomeKind.SUCCESS)

    @normalized_boundary(Stage.RESOLUTION)
    async def health(self) -> HealthObservation:
        try:
            await self._call(self.client.get_user)
        except TransferError as exc:
            return HealthObservation(False, exc.error)
        return HealthObservation(True)
