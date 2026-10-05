"""TorBox resolution implementation; no transfer state or policy ownership.

TorBox runs three parallel remote acquisition families under one account --
torrents, web downloads and Usenet downloads -- and every one of them ends in
files TorBox stores, each of which yields a short-lived download link on
request. This provider translates all three into the neutral provider-resource
lifecycle: create (or adopt) the remote object, persist its family-scoped
identity, observe it, publish its file manifest, and resolve each member into
fresh HTTP material for the ordinary executors. Routing, failover, retry,
placement and file selection are DebridPulse's.
"""
from __future__ import annotations

from dataclasses import replace
from functools import wraps
import re
import time
from urllib.parse import urlsplit

from providers.torbox.account import refused_family
from providers.torbox.client import (
    LIST_PAGE_LIMIT, TORRENT, USENET, WEBDL, TorBoxAPIError, TorBoxService, member_address, member_source_host,
    parse_member_address,
)
from providers.torbox.translation import (
    INTEGRATION_ID, identity, native_members, observation, protocol_error, resource, translate_error,
    webdl_source_host,
)
from services.network_safety import validate_provider_download_url
from transfers.applicability import ApplicabilityReadiness, ProviderApplicability
from transfers.errors import (
    Category, Domain, NormalizedError, Origin, Retryability, Stage, TransferError,
)
from transfers.file_selection import ManifestInvalid
from transfers.models import (
    BITTORRENT_REQUEST_KINDS, AvailabilityState, CachePresence, Capability, CleanupAuthority, CleanupDirective, DeliveryKind, Endpoint, HealthObservation,
    IntegrationDescriptor, OutcomeKind, Ownership, ProviderObservation, ProviderResource,
    ResolutionResult, ResourceSnapshot, ResourceState, SourceEntry, SourceIdentity, TransferCandidate,
    TransferOutcome, TransferRequest,
)
from transfers.staged_input import StagedInputError, StagedPayload

_FAMILIES = (TORRENT, WEBDL, USENET)
# TorBox opens a generated link for three hours; it is refreshed well before.
_LINK_LIFETIME_SECONDS = 3 * 60 * 60 - 10 * 60
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
                raise TransferError(translate_error(exc, stage=stage, secrets=self.client.secrets())) from None
        return invoke
    return decorate


# A v1 BitTorrent info-hash, as admission records it (``transfers.requests.extract_hash``).
_INFO_HASH = re.compile(r"[0-9a-f]{40}")


class TorBoxProvider:
    # HTTP(S) applicability is published by this provider's own host
    # maintenance from TorBox's supported-host catalogue; until it has a
    # snapshot the provider is an unresolved specialized competitor.
    applicability = ProviderApplicability(specialized=True, readiness=ApplicabilityReadiness.UNRESOLVED)

    def __init__(self, client: TorBoxService, *, usenet: bool = False, staged_input=None,
                 clock=time.time):
        self.client = client
        self.staged_input = staged_input
        self._clock = clock
        kinds = {"magnet", "torrent", "http", "https"}
        # "Usenet via TorBox" is TorBox's own participation in NZB work and
        # nothing more: it never disables, inspects or replaces native Usenet.
        # When both claim an NZB the one canonical competition prefers TorBox
        # and keeps native Usenet as the next provider if TorBox is exhausted.
        if usenet:
            kinds.add("nzb")
        self.descriptor = IntegrationDescriptor(
            INTEGRATION_ID, "TorBox",
            frozenset({Capability.RESOLVE, Capability.AVAILABILITY, Capability.REFRESH, Capability.METADATA,
                       Capability.FILE_MANIFEST, Capability.RESOURCE_CREATION,
                       Capability.RESOURCE_LOOKUP, Capability.INVENTORY,
                       Capability.CLEANUP, Capability.HEALTH}),
            request_types=frozenset(kinds),
            enabled=client.configured,
        )

    def applicability_for(self, request: TransferRequest) -> ProviderApplicability:
        # Replaced by host maintenance once it is attached.
        return self.applicability

    @property
    def entitlements(self):
        """What the connected account may begin now, kept by its account
        owner (``integrations.account_entitlement``) from TorBox's own plan
        semantics (``providers.torbox.account``); ``None`` -- no account
        dimension at all -- for an instance built without one."""
        owner = getattr(self, "account", None)
        return owner.entitlements if owner is not None else None

    async def _refused(self, exc: Exception, kind: str) -> None:
        """A creation TorBox refused because the plan excludes the feature
        contracts exactly that family for this account; any other refusal is
        an ordinary failure."""
        family = refused_family(exc, kind)
        owner = getattr(self, "account", None)
        if family and owner is not None:
            await owner.contract(family)

    async def _call(self, operation, *args, stage=Stage.RESOLUTION, **kwargs):
        try:
            return await operation(*args, **kwargs)
        except Exception as exc:
            raise TransferError(translate_error(exc, stage=stage, secrets=self.client.secrets())) from None

    # -- resolution ---------------------------------------------------------------

    @normalized_boundary(Stage.RESOLUTION)
    async def resolve(self, request: TransferRequest) -> ResolutionResult:
        member = parse_member_address(request.payload) if request.kind == "https" else None
        if member is not None:
            return ResolutionResult(ResourceState.AVAILABLE, (await self._member(request, *member),))
        try:
            if request.kind in {"http", "https"}:
                family, native_id, ownership = WEBDL, await self._webdl(str(request.payload)), Ownership.CREATED
            elif request.kind in {"magnet", "torrent"}:
                family, (native_id, ownership) = TORRENT, await self._torrent(request)
            elif request.kind == "nzb" and request.kind in self.descriptor.request_types:
                family, native_id, ownership = USENET, await self._usenet(request), Ownership.CREATED
            else:
                raise TransferError(NormalizedError(Domain.REQUEST, Category.UNSUPPORTED_REQUEST,
                                                    Stage.SUBMISSION, Retryability.NEVER,
                                                    origin=Origin.USER, integration_id=INTEGRATION_ID))
        except TorBoxAPIError as exc:
            # The refusal still fails this route the ordinary way (provider
            # exhaustion and failover are the core's); it only also tells the
            # account owner what this plan excludes.
            await self._refused(exc, request.kind)
            raise
        observed = replace(await self.observe(resource(family, native_id, ownership=ownership)), request=request)
        return ResolutionResult(observed.state, observation=observed, error=observed.error)

    async def _webdl(self, link: str) -> str:
        """Create the web download for ``link`` -- from TorBox's cache when it
        holds the link (``add_only_if_cached``: no hoster acquisition at all),
        otherwise as an ordinary download. A link the cache answered for but
        no longer holds is a retryable failure, never a silent productive
        creation: the next resolution asks the cache again."""
        if link not in await self.client.webdl_cached((link,)):
            return await self.client.create_webdl(link)
        native_id = await self.client.create_webdl(link, cached_only=True)
        if native_id is None:
            raise TransferError(NormalizedError(Domain.RESOLUTION, Category.RESOLUTION_TEMPORARILY_FAILED,
                                                Stage.RESOLUTION, Retryability.BACKOFF, origin=Origin.PROVIDER,
                                                integration_id=INTEGRATION_ID))
        return native_id

    @staticmethod
    def _webdl_link(request: TransferRequest) -> str | None:
        """The hoster link a root web-download request submits, if it is one."""
        if request.kind not in {"http", "https"} or parse_member_address(request.payload) is not None:
            return None
        return str(request.payload)

    @normalized_boundary(Stage.RESOLUTION)
    async def cache_presence(self, requests: tuple[TransferRequest, ...]) -> tuple[CachePresence, ...]:
        """Whether TorBox's web-download cache holds each request's link, in
        one batched read that creates nothing. Only a root web-download
        request has a cache answer; anything else is ``UNKNOWN``. A cached
        entry says the address was fetched before -- never what it holds."""
        links = [self._webdl_link(request) for request in requests]
        cached = await self._call(self.client.webdl_cached, tuple({link for link in links if link}))
        return tuple(CachePresence.UNKNOWN if link is None else
                     CachePresence.HIT if link in cached else CachePresence.MISS for link in links)

    @staticmethod
    def _torrent_hash(request: TransferRequest) -> str | None:
        """The v1 info-hash a BitTorrent-class root names, if it names one."""
        value = str(request.fingerprint or "").casefold()
        return value if request.kind in BITTORRENT_REQUEST_KINDS and _INFO_HASH.fullmatch(value) else None

    @normalized_boundary(Stage.RESOLUTION)
    async def availability(self, requests: tuple[TransferRequest, ...]) -> tuple[AvailabilityState, ...]:
        """Whether TorBox can deliver each request now without acquiring it:
        a torrent its torrent cache holds (by info-hash), or a root web
        download its web-download cache holds -- two separate batched reads
        that create nothing. ``NOT_READY`` is TorBox's own answer without the
        request; anything it cannot be asked about is ``UNKNOWN``."""
        hashes = [self._torrent_hash(request) for request in requests]
        links = [None if digest else self._webdl_link(request) for request, digest in zip(requests, hashes)]
        held = (await self._call(self.client.torrents_cached, tuple(h for h in hashes if h))
                if any(hashes) else frozenset())
        cached = (await self._call(self.client.webdl_cached, tuple({link for link in links if link}))
                  if any(links) else {})
        return tuple(
            (AvailabilityState.READY if digest in held else AvailabilityState.NOT_READY) if digest
            else (AvailabilityState.READY if link in cached else AvailabilityState.NOT_READY) if link
            else AvailabilityState.UNKNOWN
            for digest, link in zip(hashes, links))

    @normalized_boundary(Stage.RESOLUTION)
    async def resolve_cached(self, request: TransferRequest) -> ResolutionResult | None:
        """``resolve`` for a root web-download request only from TorBox's cache
        (``add_only_if_cached``); ``None`` -- nothing created -- when TorBox
        does not hold it now, and for anything that is not such a request."""
        link = self._webdl_link(request)
        if link is None:
            return None
        try:
            native_id = await self.client.create_webdl(link, cached_only=True)
        except TorBoxAPIError as exc:
            await self._refused(exc, request.kind)
            raise
        if native_id is None:
            return None
        observed = replace(await self.observe(resource(WEBDL, native_id, ownership=Ownership.CREATED)),
                           request=request)
        return ResolutionResult(observed.state, observation=observed, error=observed.error)

    async def _torrent(self, request: TransferRequest) -> tuple[str, Ownership]:
        try:
            if request.kind == "magnet":
                return await self.client.create_torrent(magnet=str(request.payload)), Ownership.CREATED
            if not isinstance(request.payload, bytes):
                raise TransferError(NormalizedError(Domain.REQUEST, Category.INVALID_REQUEST, Stage.SUBMISSION,
                                                    Retryability.NEVER, origin=Origin.USER,
                                                    integration_id=INTEGRATION_ID))
            # TorBox always acquires the WHOLE torrent -- it has no upstream
            # file selection. DebridPulse's own file selection decides which of
            # the resulting files it materializes.
            return (await self.client.create_torrent(metainfo=request.payload, name=request.name or ""),
                    Ownership.CREATED)
        except TorBoxAPIError as exc:
            if exc.error.upper() != "DUPLICATE_ITEM":
                raise
            return await self._already_present(request, exc), Ownership.ADOPTED

    async def _already_present(self, request: TransferRequest, exc: TorBoxAPIError) -> str:
        """Adopt the account's existing torrent when TorBox refuses a duplicate
        -- only when the authoritative info-hash names exactly one. Anything
        less certain is the refusal itself, unchanged."""
        fingerprint = str(request.fingerprint or "").casefold()
        if not fingerprint:
            raise exc
        matches = [item for item in (await self.inventory()).observations
                   if identity(item.resource)[0] == TORRENT and item.fingerprint == fingerprint]
        if len(matches) != 1:
            raise exc
        return identity(matches[0].resource)[1]

    async def _usenet(self, request: TransferRequest) -> str:
        """Submit the canonical staged NZB as a file. The posting is the bytes
        DebridPulse already holds: however it arrived -- upload, inline bytes
        or a link fetched at ingress -- nothing here can tell, refetch or
        route by its origin."""
        name = request.name or "posting.nzb"
        payload = request.payload
        if isinstance(payload, StagedPayload):
            if self.staged_input is None:
                raise TransferError(NormalizedError(Domain.REQUEST, Category.INVALID_REQUEST, Stage.RESOLUTION,
                                                    Retryability.NEVER, integration_id=INTEGRATION_ID))
            try:
                with self.staged_input.opened(payload) as stream:
                    return await self.client.create_usenet(stream, name=name)
            except StagedInputError:
                raise TransferError(NormalizedError(Domain.REQUEST, Category.INVALID_REQUEST, Stage.RESOLUTION,
                                                    Retryability.NEVER, integration_id=INTEGRATION_ID)) from None
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            raise TransferError(NormalizedError(Domain.REQUEST, Category.INVALID_REQUEST, Stage.RESOLUTION,
                                                Retryability.NEVER, integration_id=INTEGRATION_ID))
        return await self.client.create_usenet(bytes(payload), name=name)

    # -- observation -----------------------------------------------------------------

    @normalized_boundary(Stage.RECONCILIATION)
    async def observe(self, resource_value: ProviderResource) -> ProviderObservation:
        family, native_id = identity(resource_value)
        try:
            native = await self._call(self.client.item, family, native_id, stage=Stage.RECONCILIATION)
        except TransferError as exc:
            if exc.error.category == Category.RESOURCE_NOT_FOUND:
                return ProviderObservation(resource_value, ResourceState.ABSENT, error=exc.error)
            raise
        if str(native.get("id")) != native_id:
            raise TransferError(protocol_error(Stage.RECONCILIATION, f"{family} object identity mismatch"))
        return observation(family, native, resource_value=resource_value)

    # -- executable members ----------------------------------------------------------

    @normalized_boundary(Stage.CANDIDATE_PREPARATION)
    async def manifest(self, resource_value: ProviderResource) -> tuple[SourceEntry, ...]:
        """Every file of the object, in the SAME collection-root-relative
        coordinates the early manifest published, each addressed by TorBox's
        own file id -- so a member's material is always that exact file."""
        stage = Stage.CANDIDATE_PREPARATION
        family, native_id = identity(resource_value)
        native = await self._call(self.client.item, family, native_id, stage=stage)
        if str(native.get("id")) != native_id or native.get("download_present") is not True:
            raise TransferError(protocol_error(stage, f"{family} object files are not available"))
        try:
            members = native_members(native)
        except ManifestInvalid:
            raise TransferError(NormalizedError(Domain.SECURITY, Category.PATH_POLICY_VIOLATION, stage,
                                                integration_id=INTEGRATION_ID)) from None
        # A web download's members remember the hoster it came from, so the
        # links TorBox generates for them keep naming that hoster as source.
        source_host = webdl_source_host(native) if family == WEBDL else None
        return tuple(SourceEntry(member.name, member.expected_bytes, member.relative_path,
                                 TransferRequest("https", member_address(family, native_id, member.file_id,
                                                                         source_host=source_host),
                                                 member.name, preferred_provider=INTEGRATION_ID))
                     for member in members)

    async def _member(self, request: TransferRequest, family: str, native_id: str, file_id: str
                      ) -> TransferCandidate:
        """Fresh material for one member: a download link generated now.

        The link is execution material only -- the durable truth is the
        member's address (family, object, file), which yields a new link
        whenever continuation or refresh needs one.

        A web download's source is the hoster it was submitted for, named by
        the member address -- never TorBox's own delivery host; with no
        hoster known it names no source rather than TorBox. A torrent's or
        NZB's member keeps TorBox's address as its source: their logical
        source is the root request's own protocol."""
        # TorBox may embed the account's API token in the link it issues: the
        # link is therefore transient execution material -- never durable,
        # never observable, regenerated for each execution by ``refresh`` --
        # and only the member address above is durable truth.
        link = await self.client.requestdl(family, native_id, file_id)
        try:
            endpoint = validate_provider_download_url(link, context="TorBox download link")
        except Exception as exc:
            raise TransferError(translate_error(exc, stage=Stage.CANDIDATE_PREPARATION,
                                                secrets=self.client.secrets())) from None
        return TransferCandidate(
            request.name or "download", (Endpoint(urlsplit(endpoint).scheme, endpoint, transient=True),), 0,
            provider_id=INTEGRATION_ID, refresh_request=request,
            expires_at=float(self._clock()) + _LINK_LIFETIME_SECONDS,
            source_identity=self._member_source(request, family),
            delivery=DeliveryKind.PROVIDER_ISSUED,
        )

    @staticmethod
    def _member_source(request: TransferRequest, family: str) -> SourceIdentity | None:
        if family != WEBDL:
            return SourceIdentity("host", urlsplit(str(request.payload)).hostname or "")
        host = member_source_host(request.payload)
        return SourceIdentity("host", host) if host else None

    @normalized_boundary(Stage.CANDIDATE_PREPARATION)
    async def refresh(self, candidate: TransferCandidate) -> ResolutionResult:
        if (candidate.provider_id != INTEGRATION_ID or candidate.refresh_request is None
                or parse_member_address(candidate.refresh_request.payload) is None):
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
        """Every object on the account, all three families, paged locally until
        complete. TorBox's separate queue of not-yet-started submissions holds
        no object yet, so nothing there can be observed or adopted."""
        observations = []
        for family in _FAMILIES:
            offset = 0
            for _page in range(_MAX_INVENTORY_PAGES):
                batch = await self._call(self.client.items, family, offset, stage=Stage.RECONCILIATION)
                if any(not isinstance(record, dict) for record in batch):
                    raise TransferError(protocol_error(Stage.RECONCILIATION, f"{family} page is malformed"))
                observations.extend(observation(family, record) for record in batch)
                if len(batch) < LIST_PAGE_LIMIT:
                    break
                offset += len(batch)
            else:
                raise TransferError(protocol_error(Stage.RECONCILIATION, f"{family} inventory does not end"))
        observations.sort(key=lambda item: (identity(item.resource)[0], int(identity(item.resource)[1])))
        return ResourceSnapshot(tuple(observations), complete=True)

    @normalized_boundary(Stage.CLEANUP)
    async def cleanup(self, directive: CleanupDirective) -> TransferOutcome:
        resource_value = directive.resource
        if (directive.authority == CleanupAuthority.OWNED
                and resource_value.ownership not in {Ownership.CREATED, Ownership.ADOPTED}):
            return TransferOutcome(OutcomeKind.SKIPPED, detail="Observed provider resource retained")
        family, native_id = identity(resource_value)
        try:
            await self._call(self.client.delete, family, native_id, stage=Stage.CLEANUP)
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
