"""Debrid-Link resolution implementation; no transfer state or policy ownership.

Debrid-Link runs two acquisition families under one account: the DOWNLOADER,
which turns a supported hoster URL into a download it serves, and the SEEDBOX,
which acquires a magnet or ``.torrent`` into files it stores. This provider
translates both into neutral contracts:

* a hoster URL that is one file resolves to one candidate;
* a hoster URL Debrid-Link answers with several files (a folder) is a
  provider resource whose complete file list is a manifest -- its files are
  members, never mirror candidates of one artifact;
* a torrent is a provider resource: created (never adopted on a guess),
  observed, its complete file list published once every file is stored, and
  each file resolved into fresh HTTP material for the ordinary executors.

Every link Debrid-Link generates is execution material only: its durability
is not documented (a downloader link carries an ``expired`` flag), so it is
transient -- never persisted, regenerated for each execution through
``refresh`` -- and only the request it came from is durable truth. Routing,
failover, retry, placement and file selection are DebridPulse's.
"""
from __future__ import annotations

from dataclasses import replace
from functools import wraps
from urllib.parse import urlsplit

from providers.debridlink.account import HOSTERS
from providers.debridlink.client import (
    MAX_IDS, DebridLinkService, member_address, native_id, parse_member_address,
)
from providers.debridlink.translation import (
    INTEGRATION_ID, LINKS, SEEDBOX, identity, link_members, links_observation, links_resource, protocol_error,
    seedbox_members, seedbox_observation, seedbox_resource, translate_error,
)
from services.network_safety import validate_provider_download_url
from transfers.applicability import ApplicabilityReadiness, ProviderApplicability
from transfers.entitlement import AccountServiceClass, ProviderEntitlements
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
            except ManifestInvalid:
                raise TransferError(NormalizedError(Domain.SECURITY, Category.PATH_POLICY_VIOLATION, stage,
                                                    integration_id=INTEGRATION_ID)) from None
            except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
                raise TransferError(NormalizedError(
                    Domain.PROVIDER, Category.INVALID_ADAPTER_RESPONSE, stage,
                    origin=Origin.PROVIDER, integration_id=INTEGRATION_ID,
                )) from None
            except Exception as exc:
                raise TransferError(translate_error(exc, stage=stage, secrets=self.client.secrets())) from None
        return invoke
    return decorate


def _chunks(ids: tuple[str, ...]):
    for start in range(0, len(ids), MAX_IDS):
        yield ids[start:start + MAX_IDS]


class DebridLinkProvider:
    # HTTP(S) applicability is published by this provider's own host
    # maintenance from Debrid-Link's hoster catalogue; until it has a snapshot
    # the provider is an unresolved specialized competitor. Magnet/torrent
    # remain descriptor request types.
    applicability = ProviderApplicability(specialized=True, readiness=ApplicabilityReadiness.UNRESOLVED)

    def __init__(self, client: DebridLinkService):
        self.client = client
        self.descriptor = IntegrationDescriptor(
            INTEGRATION_ID, "Debrid-Link",
            frozenset({Capability.RESOLVE, Capability.REFRESH, Capability.METADATA,
                       Capability.FILE_MANIFEST, Capability.RESOURCE_CREATION,
                       Capability.RESOURCE_LOOKUP, Capability.INVENTORY,
                       Capability.CLEANUP, Capability.HEALTH}),
            request_types=frozenset({"magnet", "torrent", "http", "https"}),
            enabled=client.configured,
        )

    def applicability_for(self, request: TransferRequest) -> ProviderApplicability:
        # Replaced by host maintenance once it is attached.
        return self.applicability

    @property
    def entitlements(self):
        """What the connected account may begin now, kept by its account
        owner (``integrations.account_entitlement``) from Debrid-Link's own
        account semantics (``providers.debridlink.account``); ``None`` -- no
        account dimension -- for an instance built without one."""
        owner = getattr(self, "account", None)
        current = owner.entitlements if owner is not None else None
        if isinstance(current, ProviderEntitlements) and current.service_class == AccountServiceClass.STANDARD:
            # The same per-hoster narrowing ``entitlement_for`` applies, stated
            # for the account as a whole: what a free account can still use.
            surface = getattr(getattr(self, "applicability_for", None), "free_surface", None)
            if callable(surface):
                current = current.with_surface(HOSTERS, surface())
        return current

    def entitlement_for(self, request: TransferRequest) -> bool | None:
        """The account's entitlement, narrowed per hoster: a standard (free)
        account may begin a hoster link only when Debrid-Link's catalogue
        marks that hoster free-usable (``isFree``), so a free account never
        claims work only a premium one can do. Which hoster a link belongs to
        is the host catalogue's structural answer; it never widens what the
        account is entitled to, and unknown account truth stays unknown."""
        current = self.entitlements
        if not isinstance(current, ProviderEntitlements):
            return True
        admitted = current.admits(request.kind)
        if admitted and request.kind in HOSTERS and current.service_class == AccountServiceClass.STANDARD:
            host_free = getattr(getattr(self, "applicability_for", None), "host_free", None)
            free = host_free(request) if callable(host_free) else None
            if free is not None:
                return free
        return admitted

    async def _call(self, operation, *args, stage=Stage.RESOLUTION, **kwargs):
        try:
            return await operation(*args, **kwargs)
        except Exception as exc:
            raise TransferError(translate_error(exc, stage=stage, secrets=self.client.secrets())) from None

    def _download_url(self, value, *, context: str, stage: Stage) -> str:
        try:
            return validate_provider_download_url(value, context=context)
        except Exception as exc:
            raise TransferError(translate_error(exc, stage=stage, secrets=self.client.secrets())) from None

    # -- resolution ---------------------------------------------------------------

    @normalized_boundary(Stage.RESOLUTION)
    async def resolve(self, request: TransferRequest) -> ResolutionResult:
        member = parse_member_address(request.payload) if request.kind == "https" else None
        if member is not None:
            return ResolutionResult(ResourceState.AVAILABLE, (await self._member(request, *member),))
        if request.kind in {"http", "https"}:
            return await self._hoster(request)
        if request.kind == "magnet":
            created = await self.client.add_torrent(magnet=str(request.payload))
        elif request.kind == "torrent" and isinstance(request.payload, bytes):
            created = await self.client.add_torrent(metainfo=request.payload, name=request.name or "")
        else:
            raise TransferError(NormalizedError(Domain.REQUEST, Category.UNSUPPORTED_REQUEST,
                                                Stage.SUBMISSION, Retryability.NEVER,
                                                origin=Origin.USER, integration_id=INTEGRATION_ID))
        torrent = native_id(created.get("id"))
        if torrent is None:
            raise TransferError(protocol_error(Stage.RESOLUTION, "torrent creation without an id"))
        observed = replace(await self.observe(seedbox_resource(torrent, ownership=Ownership.CREATED)),
                           request=request)
        return ResolutionResult(observed.state, observation=observed, error=observed.error)

    async def _hoster(self, request: TransferRequest) -> ResolutionResult:
        """One hoster URL: one file is one candidate; a folder of several
        distinct files is a resource whose files are manifest members."""
        native = await self.client.add_link(str(request.payload))
        records = native if isinstance(native, list) else [native]
        if any(not isinstance(record, dict) for record in records) or not records:
            raise TransferError(protocol_error(Stage.RESOLUTION, "link answer is malformed"))
        if len(records) == 1:
            return ResolutionResult(ResourceState.AVAILABLE, (self._candidate(request, records[0]),))
        ids = tuple(native_id(record.get("id")) or "" for record in records)
        resource_value = links_resource(ids)
        self._folder_requests(records)
        observed = links_observation(records, resource_value, request=request)
        return ResolutionResult(observed.state, observation=observed, error=observed.error)

    def _candidate(self, request: TransferRequest, native: dict) -> TransferCandidate:
        """One generated download: transient execution material only."""
        if native.get("expired") is True:
            raise TransferError(NormalizedError(Domain.RESOLUTION, Category.RESOLUTION_TEMPORARILY_FAILED,
                                                Stage.RESOLUTION, Retryability.BACKOFF, origin=Origin.PROVIDER,
                                                integration_id=INTEGRATION_ID))
        endpoint = self._download_url(native.get("downloadUrl"), context="Debrid-Link download link",
                                      stage=Stage.RESOLUTION)
        raw_size = native.get("size")
        size = raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool) and raw_size > 0 else 0
        name = native.get("name").strip() if isinstance(native.get("name"), str) else ""
        # Resolver-attested identity only from what Debrid-Link itself
        # asserted: an authoritative name AND an exact positive size.
        evidence = ResolverArtifactIdentityEvidence(resolved_name=name, exact_bytes=size) if name and size else None
        return TransferCandidate(
            name or request.name or "download", (Endpoint(urlsplit(endpoint).scheme, endpoint, transient=True),),
            size, provider_id=INTEGRATION_ID, refresh_request=request,
            source_identity=SourceIdentity("host", str(urlsplit(str(request.payload)).hostname or "")
                                           .casefold().removeprefix("www.").rstrip(".")),
            resolver_identity_evidence=evidence,
            # The generated endpoint is Debrid-Link's own delivery capability;
            # the requested hoster is identified by ``source_identity``.
            delivery=DeliveryKind.PROVIDER_ISSUED,
        )

    def _folder_requests(self, records: list) -> tuple[TransferRequest, ...]:
        """The hoster URL of each file of a folder, as the ordinary request
        that member resolves through. Each must be a distinct, safe link: a
        folder whose files cannot be told apart cannot be represented, and
        fails rather than being guessed at."""
        requests = []
        for record in records:
            url = self._download_url(record.get("url"), context="Debrid-Link folder file link",
                                     stage=Stage.CANDIDATE_PREPARATION)
            requests.append(TransferRequest(urlsplit(url).scheme.casefold(), url,
                                            str(record.get("name") or "").strip(),
                                            preferred_provider=INTEGRATION_ID))
        if len({request.payload for request in requests}) != len(requests):
            raise TransferError(NormalizedError(
                Domain.RESOLUTION, Category.RESOLUTION_FAILED, Stage.CANDIDATE_PREPARATION, Retryability.NEVER,
                origin=Origin.PROVIDER, permanence=Permanence.PERMANENT, integration_id=INTEGRATION_ID,
                native_code="indistinct_folder_files"))
        return tuple(requests)

    async def _member(self, request: TransferRequest, torrent_id: str, file_id: str) -> TransferCandidate:
        """Fresh material for one torrent file: the link Debrid-Link answers
        for it now. The durable truth is the member address (torrent, file),
        which yields a new link whenever continuation or refresh needs one."""
        native = await self._call(self.client.torrent, torrent_id, stage=Stage.CANDIDATE_PREPARATION)
        if native is None or native_id(native.get("id")) != torrent_id:
            raise TransferError(NormalizedError(Domain.PROVIDER, Category.RESOURCE_NOT_FOUND,
                                                Stage.CANDIDATE_PREPARATION, Retryability.AFTER_RERESOLUTION,
                                                origin=Origin.PROVIDER, integration_id=INTEGRATION_ID))
        files = [record for record in native.get("files") or () if isinstance(record, dict)
                 and record.get("id") == file_id]
        if len(files) != 1:
            raise TransferError(protocol_error(Stage.CANDIDATE_PREPARATION, "torrent file is not listed"))
        record = files[0]
        percent = record.get("downloadPercent")
        if not (isinstance(percent, (int, float)) and not isinstance(percent, bool) and percent >= 100):
            raise TransferError(NormalizedError(Domain.PROVIDER, Category.RESOLUTION_TEMPORARILY_FAILED,
                                                Stage.CANDIDATE_PREPARATION, Retryability.BACKOFF,
                                                origin=Origin.PROVIDER, integration_id=INTEGRATION_ID))
        endpoint = self._download_url(record.get("downloadUrl"), context="Debrid-Link torrent file link",
                                      stage=Stage.CANDIDATE_PREPARATION)
        raw_size = record.get("size")
        size = raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool) and raw_size > 0 else 0
        return TransferCandidate(
            request.name or "download", (Endpoint(urlsplit(endpoint).scheme, endpoint, transient=True),), size,
            provider_id=INTEGRATION_ID, refresh_request=request,
            source_identity=SourceIdentity("host", urlsplit(str(request.payload)).hostname or ""),
            delivery=DeliveryKind.PROVIDER_ISSUED,
        )

    # -- observation -----------------------------------------------------------------

    @normalized_boundary(Stage.RECONCILIATION)
    async def observe(self, resource_value: ProviderResource) -> ProviderObservation:
        family, ids = identity(resource_value)
        try:
            if family == SEEDBOX:
                native = await self._call(self.client.torrent, ids[0], stage=Stage.RECONCILIATION)
            else:
                native = []
                for chunk in _chunks(ids):
                    native.extend(await self._call(self.client.links, chunk, stage=Stage.RECONCILIATION))
        except TransferError as exc:
            if exc.error.category == Category.RESOURCE_NOT_FOUND:
                return ProviderObservation(resource_value, ResourceState.ABSENT, error=exc.error)
            raise
        if family == LINKS:
            return links_observation(native, resource_value)
        if native is None:
            return ProviderObservation(resource_value, ResourceState.ABSENT)
        if native_id(native.get("id")) != ids[0]:
            raise TransferError(protocol_error(Stage.RECONCILIATION, "torrent identity mismatch"))
        return seedbox_observation(native, resource_value=resource_value)

    # -- executable members ----------------------------------------------------------

    @normalized_boundary(Stage.CANDIDATE_PREPARATION)
    async def manifest(self, resource_value: ProviderResource) -> tuple[SourceEntry, ...]:
        """Every file of the resource, in the SAME collection-root-relative
        coordinates the early manifest published. A torrent file is addressed
        by Debrid-Link's own torrent and file ids, so its material is always
        that exact file; a folder file by its own hoster URL."""
        stage = Stage.CANDIDATE_PREPARATION
        family, ids = identity(resource_value)
        if family == LINKS:
            records = []
            for chunk in _chunks(ids):
                records.extend(await self._call(self.client.links, chunk, stage=stage))
            by_id = {str(record.get("id")): record for record in records if isinstance(record, dict)}
            if any(link_id not in by_id for link_id in ids):
                raise TransferError(protocol_error(stage, "folder links are no longer listed"))
            ordered = [by_id[link_id] for link_id in ids]
            members = link_members(ordered)
            requests = self._folder_requests(ordered)
            return tuple(SourceEntry(member.name, member.expected_bytes, member.relative_path, request)
                         for member, request in zip(members, requests, strict=True))
        native = await self._call(self.client.torrent, ids[0], stage=stage)
        if native is None or native_id(native.get("id")) != ids[0] or native.get("isZip") is True:
            raise TransferError(protocol_error(stage, "torrent files are not available"))
        members = seedbox_members(native)
        if not members or not all(member.complete for member in members):
            raise TransferError(protocol_error(stage, "torrent files are not all stored"))
        return tuple(SourceEntry(member.name, member.expected_bytes, member.relative_path,
                                 TransferRequest("https", member_address(ids[0], member.native_id), member.name,
                                                 preferred_provider=INTEGRATION_ID))
                     for member in members)

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

    # -- account inventory, cleanup and health ----------------------------------------

    @normalized_boundary(Stage.RECONCILIATION)
    async def inventory(self) -> ResourceSnapshot:
        """Every torrent on the account, paged until Debrid-Link says there is
        no next page. A page that is not the documented shape fails the scan;
        it is never read as an empty inventory. Downloader links are not
        resources until a folder answer makes them one, so they are not
        listed here."""
        observations = []
        page = 0
        for _ in range(_MAX_INVENTORY_PAGES):
            batch, following = await self._call(self.client.torrents_page, page, stage=Stage.RECONCILIATION)
            if any(not isinstance(record, dict) for record in batch):
                raise TransferError(protocol_error(Stage.RECONCILIATION, "torrent page is malformed"))
            observations.extend(seedbox_observation(record) for record in batch)
            if following < 0:
                break
            if following <= page:
                raise TransferError(protocol_error(Stage.RECONCILIATION, "torrent pages do not advance"))
            page = following
        else:
            raise TransferError(protocol_error(Stage.RECONCILIATION, "torrent inventory does not end"))
        observations.sort(key=lambda item: str(item.resource.context.get("id")))
        return ResourceSnapshot(tuple(observations), complete=True)

    @normalized_boundary(Stage.CLEANUP)
    async def cleanup(self, directive: CleanupDirective) -> TransferOutcome:
        resource_value = directive.resource
        if (directive.authority == CleanupAuthority.OWNED
                and resource_value.ownership not in {Ownership.CREATED, Ownership.ADOPTED}):
            return TransferOutcome(OutcomeKind.SKIPPED, detail="Observed provider resource retained")
        family, ids = identity(resource_value)
        try:
            if family == SEEDBOX:
                await self._call(self.client.remove_torrent, ids[0], stage=Stage.CLEANUP)
            else:
                for chunk in _chunks(ids):
                    await self._call(self.client.remove_links, chunk, stage=Stage.CLEANUP)
        except TransferError as exc:
            # Already absent is the outcome cleanup wanted.
            if exc.error.category != Category.RESOURCE_NOT_FOUND:
                return TransferOutcome(OutcomeKind.FAILURE, exc.error)
        return TransferOutcome(OutcomeKind.SUCCESS)

    @normalized_boundary(Stage.RESOLUTION)
    async def health(self) -> HealthObservation:
        try:
            await self._call(self.client.account)
        except TransferError as exc:
            return HealthObservation(False, exc.error)
        return HealthObservation(True)
