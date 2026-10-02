"""Real-Debrid resolution implementation; no transfer state or policy ownership."""
from __future__ import annotations

from dataclasses import replace
from functools import wraps
from urllib.parse import urlsplit

from providers.realdebrid.account import refused_family
from providers.realdebrid.client import RealDebridAPIError, RealDebridService
from providers.realdebrid.translation import (
    AWAITING_SELECTION, CONVERTING, INTEGRATION_ID, native_members, native_name,
    observation_from_native, protocol_error, resource_from_native, translate_error,
    unrestricted_matches,
)
from services.network_safety import validate_provider_download_url
from transfers.applicability import ApplicabilityReadiness, ProviderApplicability
from transfers.errors import (
    Category, Domain, NormalizedError, Origin, Permanence, Retryability, Stage, TransferError,
)
from transfers.file_selection import ManifestInvalid
from transfers.models import (
    Capability, CleanupAuthority, CleanupDirective, DeliveryKind, Endpoint, HealthObservation,
    IntegrationDescriptor, OutcomeKind, Ownership, ProviderObservation, ProviderResource,
    ResolutionResult, ResolverArtifactIdentityEvidence, ResourceSnapshot, ResourceState, SourceEntry,
    SourceIdentity, TransferCandidate, TransferOutcome, TransferRequest,
)

# Real-Debrid's own "this torrent is already on the account" refusal.
_ALREADY_ACTIVE = 33
# Native parameter refusals: the only ones a not-yet-converted magnet can give
# a file selection, because it has no file list yet.
_SELECTION_NOT_READY_CODES = frozenset({1, 2})
# Inventory pages a complete scan may take before it is called malformed.
_MAX_INVENTORY_PAGES = 200


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
                    origin=Origin.PROVIDER, integration_id=INTEGRATION_ID,
                )) from None
            except Exception as exc:
                raise TransferError(translate_error(exc, stage=stage, secrets=self._secrets())) from None
        return invoke
    return decorate


class RealDebridProvider:
    # HTTP(S) applicability is published by this provider's own host maintenance
    # from Real-Debrid's validated host inventory; until it has a snapshot the
    # provider is an unresolved specialized competitor. Magnet/torrent remain
    # descriptor request types.
    applicability = ProviderApplicability(specialized=True, readiness=ApplicabilityReadiness.UNRESOLVED)

    def __init__(self, client: RealDebridService):
        self.client = client
        self.descriptor = IntegrationDescriptor(
            INTEGRATION_ID, "Real-Debrid",
            frozenset({Capability.RESOLVE, Capability.REFRESH, Capability.METADATA,
                       Capability.FILE_MANIFEST, Capability.RESOURCE_CREATION,
                       Capability.RESOURCE_LOOKUP, Capability.INVENTORY,
                       Capability.CLEANUP, Capability.HEALTH}),
            request_types=frozenset({"magnet", "torrent", "http", "https"}),
            enabled=client.configured,
        )

    def applicability_for(self, request: TransferRequest) -> ProviderApplicability:
        # Replaced by host maintenance once it holds a snapshot.
        return self.applicability

    @property
    def entitlements(self):
        """What the connected account may begin now, kept by its account
        owner (``integrations.account_entitlement``) from Real-Debrid's own
        account semantics (``providers.realdebrid.account``); ``None`` -- no
        account dimension -- for an instance built without one."""
        owner = getattr(self, "account", None)
        return owner.entitlements if owner is not None else None

    def _secrets(self) -> tuple[str, ...]:
        return self.client.secrets()

    async def _call(self, operation, *args, stage=Stage.RESOLUTION, **kwargs):
        try:
            return await operation(*args, **kwargs)
        except Exception as exc:
            raise TransferError(translate_error(exc, stage=stage, secrets=self._secrets())) from None

    # -- direct hoster links -----------------------------------------------------

    async def _unrestricted(self, link: str, *, stage: Stage) -> dict:
        native = await self._call(self.client.unrestrict_link, link, stage=stage)
        alternatives = native.get("alternative")
        if isinstance(alternatives, list) and alternatives:
            # Several generated outputs (for example the qualities of a video
            # page) are different content, never mirrors of one artifact.
            raise TransferError(NormalizedError(
                Domain.RESOLUTION, Category.RESOLUTION_FAILED, stage, Retryability.NEVER,
                origin=Origin.PROVIDER, permanence=Permanence.PERMANENT,
                integration_id=INTEGRATION_ID, native_code="multiple_outputs"))
        return native

    def _candidate(self, request: TransferRequest, native: dict) -> TransferCandidate:
        try:
            endpoint = validate_provider_download_url(native.get("download"), context="unrestricted download link")
        except Exception as exc:
            raise TransferError(translate_error(exc, secrets=self._secrets())) from None
        raw_size = native.get("filesize")
        size = raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool) and raw_size > 0 else 0
        native_name_value = native.get("filename") if isinstance(native.get("filename"), str) else ""
        # Resolver-attested identity only from what Real-Debrid itself asserted:
        # an authoritative name AND an exact positive size, never a fallback name.
        evidence = (ResolverArtifactIdentityEvidence(resolved_name=native_name_value, exact_bytes=size)
                    if native_name_value and size > 0 else None)
        return TransferCandidate(
            native_name_value or request.name, (Endpoint(urlsplit(endpoint).scheme, endpoint),), size,
            provider_id=INTEGRATION_ID, refresh_request=request,
            source_identity=SourceIdentity("host", str(urlsplit(str(request.payload)).hostname or "")
                                           .casefold().removeprefix("www.").rstrip(".")),
            resolver_identity_evidence=evidence,
            # The unrestricted endpoint is Real-Debrid's own delivery capability;
            # the requested hoster is identified by ``source_identity``.
            delivery=DeliveryKind.PROVIDER_ISSUED,
        )

    # -- resolution ---------------------------------------------------------------

    @normalized_boundary(Stage.RESOLUTION)
    async def resolve(self, request: TransferRequest) -> ResolutionResult:
        if request.kind in {"http", "https"}:
            native = await self._unrestricted(str(request.payload), stage=Stage.RESOLUTION)
            return ResolutionResult(ResourceState.AVAILABLE, (self._candidate(request, native),))
        if request.kind == "magnet":
            create = (self.client.add_magnet, str(request.payload))
        elif request.kind == "torrent" and isinstance(request.payload, bytes):
            create = (self.client.add_torrent, request.payload)
        else:
            raise TransferError(NormalizedError(Domain.REQUEST, Category.UNSUPPORTED_REQUEST,
                                                Stage.SUBMISSION, Retryability.NEVER,
                                                origin=Origin.USER, integration_id=INTEGRATION_ID))
        try:
            created = await create[0](create[1])
            resource = resource_from_native(created, ownership=Ownership.CREATED)
        except RealDebridAPIError as exc:
            if exc.error_code != _ALREADY_ACTIVE:
                # A refusal of the torrent feature itself still fails this
                # route the ordinary way; it only also tells the account owner.
                family, owner = refused_family(exc, request.kind), getattr(self, "account", None)
                if family and owner is not None:
                    await owner.contract(family)
                raise
            resource = await self._already_active(request, exc)
        await self._bootstrap(resource)
        observation = replace(await self.observe(resource), request=request)
        return ResolutionResult(observation.state, observation=observation, error=observation.error)

    async def _already_active(self, request: TransferRequest, exc: RealDebridAPIError) -> ProviderResource:
        """Adopt the account's existing torrent when Real-Debrid refuses a
        duplicate -- only when the authoritative info-hash names exactly one.
        Anything less certain is the refusal itself, unchanged."""
        fingerprint = str(request.fingerprint or "").casefold()
        if not fingerprint:
            raise exc
        matches = [observation for observation in (await self.inventory()).observations
                   if observation.fingerprint == fingerprint]
        if len(matches) != 1:
            raise exc
        return replace(matches[0].resource, ownership=Ownership.ADOPTED)

    async def _bootstrap(self, resource: ProviderResource) -> None:
        """Real-Debrid's own start action: select every file upstream.

        Provider bootstrap only -- DebridPulse's file selection decides what is
        materialized locally and is never synchronized back. 204 selected the
        files and 202 says they already were; both are success.

        The one refusal deferred to observation is a magnet Real-Debrid has
        not converted yet: a parameter refusal (native 1 or 2) while
        Real-Debrid itself still reports ``magnet_conversion``, so there is no
        file list to select. The observation that later sees
        ``waiting_files_selection`` selects it then. Every other refusal is
        Real-Debrid's answer and propagates."""
        native_id = self._native_id(resource)
        try:
            await self.client.select_files(native_id, "all")
        except RealDebridAPIError as exc:
            if exc.error_code not in _SELECTION_NOT_READY_CODES:
                raise
            info = await self.client.torrent_info(native_id)
            if not isinstance(info, dict) or info.get("status") != CONVERTING:
                raise exc from None

    def _native_id(self, resource: ProviderResource) -> str:
        if resource.provider_id != INTEGRATION_ID or not resource.context.get("id"):
            raise TransferError(NormalizedError(Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE,
                                                Stage.RECONCILIATION, integration_id=INTEGRATION_ID))
        return str(resource.context["id"])

    @normalized_boundary(Stage.RECONCILIATION)
    async def observe(self, resource: ProviderResource) -> ProviderObservation:
        native_id = self._native_id(resource)
        try:
            native = await self._call(self.client.torrent_info, native_id, stage=Stage.RECONCILIATION)
        except TransferError as exc:
            if exc.error.category == Category.RESOURCE_NOT_FOUND:
                return ProviderObservation(resource, ResourceState.ABSENT, error=exc.error)
            raise
        if str(native.get("id") or "") != native_id:
            raise TransferError(protocol_error(Stage.RECONCILIATION, "torrent identity mismatch"))
        if native.get("status") == AWAITING_SELECTION:
            await self._call(self.client.select_files, native_id, "all", stage=Stage.RECONCILIATION)
        return observation_from_native(native, resource=resource)

    # -- executable members -------------------------------------------------------

    @normalized_boundary(Stage.CANDIDATE_PREPARATION)
    async def manifest(self, resource: ProviderResource) -> tuple[SourceEntry, ...]:
        """Executable members, in the SAME collection-root-relative coordinate
        system the early ``FileManifest`` published.

        ``links[k]`` belongs to the k-th SELECTED file in native ``files[]``
        order. That pairing is the one thing here Real-Debrid does not state
        outright, so every pair is proven before it becomes executable: the
        restricted link is unrestricted and the returned identity must be the
        member's. A count that does not reconcile, or one contradicted member,
        fails the whole manifest -- a link is never executed for the wrong file
        and never re-paired by searching its neighbours."""
        stage = Stage.CANDIDATE_PREPARATION
        native_id = self._native_id(resource)
        native = await self._call(self.client.torrent_info, native_id, stage=stage)
        if str(native.get("id") or "") != native_id:
            raise TransferError(protocol_error(stage, "torrent identity mismatch"))
        links = native.get("links")
        if not isinstance(links, list) or any(not isinstance(link, str) for link in links):
            raise TransferError(protocol_error(stage, "torrent links are malformed"))
        try:
            members = native_members(native.get("files"), root_name=native_name(native))
        except ManifestInvalid:
            raise TransferError(NormalizedError(Domain.SECURITY, Category.PATH_POLICY_VIOLATION, stage,
                                                integration_id=INTEGRATION_ID)) from None
        selected = [member for member in members if member.selected]
        if not selected or len(selected) != len(links):
            raise TransferError(protocol_error(stage, "selected files and links do not reconcile"))
        entries = []
        for member, link in zip(selected, links, strict=True):
            try:
                restricted = validate_provider_download_url(link, context="torrent member link")
            except Exception as exc:
                raise TransferError(translate_error(exc, stage=stage, secrets=self._secrets())) from None
            unrestricted = await self._unrestricted(restricted, stage=stage)
            if not unrestricted_matches(member, unrestricted):
                raise TransferError(protocol_error(stage, "a member link does not match its file"))
            request = TransferRequest(urlsplit(restricted).scheme, restricted, member.name,
                                      preferred_provider=INTEGRATION_ID)
            entries.append(SourceEntry(member.name, member.expected_bytes, member.relative_path, request))
        return tuple(entries)

    @normalized_boundary(Stage.CANDIDATE_PREPARATION)
    async def refresh(self, candidate: TransferCandidate) -> ResolutionResult:
        if candidate.provider_id != INTEGRATION_ID or candidate.refresh_request is None:
            raise TransferError(NormalizedError(Domain.RESOLUTION, Category.UNSUPPORTED_CAPABILITY,
                                                Stage.CANDIDATE_PREPARATION, Retryability.NEVER,
                                                integration_id=INTEGRATION_ID))
        result = await self.resolve(candidate.refresh_request)
        return replace(result, candidates=tuple(
            replace(item, relative_path=candidate.relative_path, resource=candidate.resource,
                    id=candidate.id if index == 0 else item.id)
            for index, item in enumerate(result.candidates)))

    # -- account inventory, cleanup and health ------------------------------------

    @normalized_boundary(Stage.RECONCILIATION)
    async def inventory(self) -> ResourceSnapshot:
        """Every torrent on the account, paged locally until complete. A page
        that is not the documented shape fails the scan; it is never read as
        an empty inventory."""
        records: list[dict] = []
        page = 1
        while True:
            if page > _MAX_INVENTORY_PAGES:
                raise TransferError(protocol_error(Stage.RECONCILIATION, "torrent inventory does not end"))
            batch, total = await self._call(self.client.torrents_page, page, stage=Stage.RECONCILIATION)
            if any(not isinstance(record, dict) or not str(record.get("id") or "").strip() for record in batch):
                raise TransferError(protocol_error(Stage.RECONCILIATION, "torrent page is malformed"))
            records.extend(batch)
            if not batch or (total is not None and len(records) >= total):
                break
            page += 1
        if total is not None and len(records) != total:
            raise TransferError(protocol_error(Stage.RECONCILIATION, "torrent inventory is incomplete"))
        records.sort(key=lambda record: str(record["id"]))
        return ResourceSnapshot(tuple(observation_from_native(record) for record in records), complete=True)

    @normalized_boundary(Stage.CLEANUP)
    async def cleanup(self, directive: CleanupDirective) -> TransferOutcome:
        resource = directive.resource
        if (directive.authority == CleanupAuthority.OWNED
                and resource.ownership not in {Ownership.CREATED, Ownership.ADOPTED}):
            return TransferOutcome(OutcomeKind.SKIPPED, detail="Observed provider resource retained")
        native_id = self._native_id(resource)
        try:
            await self._call(self.client.delete_torrent, native_id, stage=Stage.CLEANUP)
        except TransferError as exc:
            if exc.error.category != Category.RESOURCE_NOT_FOUND:
                return TransferOutcome(OutcomeKind.FAILURE, exc.error)
        return TransferOutcome(OutcomeKind.SUCCESS)

    @normalized_boundary(Stage.RESOLUTION)
    async def health(self) -> HealthObservation:
        try:
            await self._call(self.client.user)
        except TransferError as exc:
            return HealthObservation(False, exc.error)
        return HealthObservation(True)
