"""AllDebrid resolution implementation; no transfer state or policy ownership."""
from __future__ import annotations

from dataclasses import replace
from functools import wraps
from urllib.parse import urlsplit

from providers.alldebrid.client import AllDebridService, API_V4, flatten_files
from services.network_safety import validate_provider_download_url
from providers.alldebrid.translation import observation_from_native, resource_from_native, translate_error
from transfers.applicability import ProviderApplicability
from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage, TransferError
from transfers.models import (
    Capability, CleanupAuthority, CleanupDirective, Endpoint, HealthObservation,
    IntegrationDescriptor, OutcomeKind, Ownership, ProviderObservation,
    ProviderResource, ResolutionResult, ResourceSnapshot, ResourceState,
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
    applicability = ProviderApplicability(
        generic_schemes=frozenset({"http", "https"}),
    )

    def __init__(self, api_key: str = "", agent: str = "DebridPulse", *, client=None):
        self.client = client if client is not None else AllDebridService(api_key, agent)
        self._secrets = (api_key,)
        self.descriptor = IntegrationDescriptor(
            "alldebrid", "AllDebrid",
            frozenset({Capability.RESOLVE, Capability.REFRESH, Capability.METADATA,
                       Capability.RESOURCE_CREATION, Capability.RESOURCE_LOOKUP,
                       Capability.INVENTORY, Capability.CLEANUP, Capability.HEALTH}),
            request_types=frozenset({"magnet", "torrent", "http", "https"}),
            enabled=bool(api_key) or client is not None,
        )

    async def _call(self, operation, *args, stage=Stage.RESOLUTION, **kwargs):
        try:
            return await operation(*args, **kwargs)
        except Exception as exc:
            raise TransferError(translate_error(exc, stage=stage, secrets=self._secrets)) from None

    @normalized_boundary(Stage.RESOLUTION)
    async def resolve(self, request: TransferRequest) -> ResolutionResult:
        if request.kind in {"http", "https"}:
            native = await self._call(self.client.unlock_link, str(request.payload))
            try:
                endpoint = validate_provider_download_url(native.get("link"))
                size = max(0, int(native.get("filesize") or native.get("size") or 0))
            except Exception as exc:
                raise TransferError(translate_error(exc, secrets=self._secrets)) from None
            candidate = TransferCandidate(
                str(native.get("filename") or native.get("name") or request.name),
                (Endpoint(urlsplit(endpoint).scheme, endpoint),), size,
                provider_id=self.descriptor.id, refresh_request=request,
                source_identity=SourceIdentity("host", str(urlsplit(str(request.payload)).hostname or "").casefold().removeprefix("www.").rstrip(".")),
            )
            return ResolutionResult(ResourceState.AVAILABLE, (candidate,))
        if request.kind == "magnet":
            native = await self._call(self.client.upload_magnet, str(request.payload))
        elif request.kind == "torrent" and isinstance(request.payload, bytes):
            native = await self._call(self.client.upload_torrent_file, request.payload, request.name)
        else:
            raise TransferError(NormalizedError(Domain.REQUEST, Category.UNSUPPORTED_REQUEST,
                                                Stage.SUBMISSION, Retryability.NEVER,
                                                origin=Origin.USER, integration_id=self.descriptor.id))
        resource = resource_from_native(native, ownership=Ownership.CREATED)
        observation = observation_from_native(native, resource=resource, request=request)
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
        return observation_from_native(matches[0], resource=resource)

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
        native_id = self._native_id(resource)
        records = await self._call(self.client.get_magnet_files, [native_id], stage=Stage.CANDIDATE_PREPARATION)
        entries = []
        try:
            for record in records:
                if str(record.get("id")) != native_id:
                    continue
                for file in flatten_files(record.get("files") or []):
                    source = str(file["link"])
                    request = TransferRequest(urlsplit(source).scheme, source, str(file["name"]),
                                              preferred_provider=self.descriptor.id)
                    entries.append(SourceEntry(str(file["name"]), int(file.get("size") or 0),
                                               str(file.get("path") or file["name"]), request))
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
