"""Premiumize resolution implementation; no transfer state or policy ownership.

Premiumize resolves a source in one of two ways, and this provider prefers the
first:

* IMMEDIATE: ``transfer/directdl`` states the source's files with fresh links
  and creates nothing on the account. Its result is frozen into a resource
  that owns nothing on Premiumize -- each member's exact path and size, never
  a link -- and each member is resolved again by proving that exact identity
  against a new ``directdl`` answer.
* CLOUD: ``transfer/create`` acquires the source into the account's cloud. The
  transfer id is the durable resource; its files are addressed by their stable
  Premiumize file ids, whose links are generated on demand.

Both end in ordinary HTTP material for the existing executors. Routing,
failover, retry, placement and file selection are DebridPulse's; Premiumize's
cloud is never managed here beyond the transfer DebridPulse created.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import wraps
import re
from urllib.parse import urlsplit

from core.presentation_safety import safe_public_host
from providers.premiumize.client import (
    API_HOST, CLOUD_MEMBER, PremiumizeAPIError, PremiumizeService, cloud_member_address,
    immediate_member_address, native_id, parse_member_address,
)
from providers.premiumize.translation import (
    IMMEDIATE, INTEGRATION_ID, NativeMember, cloud_members, named_cloud_members, cloud_resource, creation_error, file_manifest,
    immediate_members, immediate_resource, observation, protocol_error, resource_mode, translate_error,
)
from services.network_safety import validate_provider_download_url
from transfers.applicability import ApplicabilityReadiness, ProviderApplicability
from transfers.errors import (
    Category, Domain, NormalizedError, Origin, Retryability, Stage, TransferError,
)
from transfers import nzb
from transfers.file_selection import ManifestInvalid, SelectionUnprovable, coordinate_correspondence
from transfers.models import (
    BITTORRENT_REQUEST_KINDS, CachePresence, Capability, CleanupAuthority, CleanupDirective, DeliveryKind,
    Endpoint, HealthObservation, IntegrationDescriptor, OutcomeKind, Ownership, ProviderObservation,
    ProviderResource, ResolutionResult, ResolverArtifactIdentityEvidence, ResourceSnapshot, ResourceState,
    SourceEntry, SourceIdentity, TORRENT_FILE_REQUEST_KINDS, TransferCandidate, TransferOutcome, TransferRequest,
)
from transfers.requests import TorrentMetainfoRejected, torrent_identity, torrent_member_tree
from transfers.staged_input import StagedInputError, StagedPayload

# "Use Premiumize Before Usenet": Premiumize's ordering priority for NZB
# requests only, against native Usenet's ordinary priority (0). Off ranks it
# after native Usenet, on before it; every other kind keeps the integration's
# ordinary priority. Either way both stay in the competition.
NZB_AFTER_USENET, NZB_BEFORE_USENET = -1, 1
# A cloud folder tree is enumerated completely or not at all, within bounds a
# real transfer never reaches.
_MAX_FOLDER_DEPTH = 32
_MAX_CLOUD_FILES = 50_000
# The most folders one tree may have: each is one ``folder/list`` call, so a
# wide or repeating folder graph ends here rather than in unbounded reads.
_MAX_CLOUD_FOLDERS = 2_000
# Premiumize refusals of an immediate result that say nothing about the
# source -- the account or credential itself -- and so never fall through to a
# productive cloud acquisition.
_NO_FALLBACK = frozenset({"authentication_failed", "permission_denied", "rate_limit_reached",
                          "account_limit_reached", "credential_missing"})
# A v1 BitTorrent info-hash, as admission records it (``transfers.requests.extract_hash``).
_INFO_HASH = re.compile(r"[0-9a-f]{40}")


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


class _ImmediateUnavailable(Exception):
    """``directdl`` gave no complete, usable result for this source: nothing
    was created, and a cloud acquisition may still produce it."""

    def __init__(self, error: NormalizedError):
        super().__init__(error.category)
        self.error = error


class PremiumizeProvider:
    # HTTP(S) applicability is published by this provider's own host
    # maintenance from Premiumize's service catalogue; until it has a snapshot
    # the provider is an unresolved specialized competitor.
    applicability = ProviderApplicability(specialized=True, readiness=ApplicabilityReadiness.UNRESOLVED)

    def __init__(self, client: PremiumizeService, *, use_before_usenet: bool = False, staged_input=None,
                 prepare_backup_torrents: bool = False):
        self.client = client
        self.staged_input = staged_input
        self._prepare_backup_torrents = bool(prepare_backup_torrents)
        self.descriptor = IntegrationDescriptor(
            INTEGRATION_ID, "Premiumize",
            frozenset({Capability.RESOLVE, Capability.REFRESH, Capability.METADATA, Capability.FILE_MANIFEST,
                       Capability.RESOURCE_CREATION, Capability.RESOURCE_LOOKUP, Capability.INVENTORY,
                       Capability.CLEANUP, Capability.HEALTH}),
            request_types=frozenset({"http", "https", "magnet", "torrent", "nzb"}),
            enabled=client.configured,
            request_priority=(("nzb", NZB_BEFORE_USENET if use_before_usenet else NZB_AFTER_USENET),),
        )

    def applicability_for(self, request: TransferRequest) -> ProviderApplicability:
        # Replaced by host maintenance once it is attached.
        return self.applicability

    @property
    def entitlements(self):
        """What the connected account may begin now, kept by its account
        owner (``integrations.account_entitlement``) from Premiumize's own
        account facts (``providers.premiumize.account``); ``None`` -- no
        account dimension -- for an instance built without one."""
        owner = getattr(self, "account", None)
        return owner.entitlements if owner is not None else None

    def speculative_preparation_allowed(self, request: TransferRequest) -> bool:
        """Only a torrent or magnet, and only while the operator allows
        "Prepare Backup Torrents": a backup may be a cloud transfer on the
        account. Nothing else Premiumize does is a backup."""
        return self._prepare_backup_torrents and request.kind in BITTORRENT_REQUEST_KINDS

    async def _call(self, operation, *args, stage=Stage.RESOLUTION, **kwargs):
        try:
            return await operation(*args, **kwargs)
        except Exception as exc:
            raise TransferError(translate_error(exc, stage=stage, secrets=self.client.secrets())) from None

    def _capable(self, group: str, request: TransferRequest) -> bool | None:
        """Whether the service catalogue says Premiumize can serve a hoster
        link through ``group``; ``None`` while there is no catalogue."""
        snapshot = getattr(getattr(self, "hosts", None), "snapshot", None)
        host = urlsplit(str(request.payload)).hostname
        if snapshot is None or not host:
            return None
        return snapshot.capable(group, host.casefold().rstrip("."))

    @staticmethod
    def _info_hash(request: TransferRequest) -> str | None:
        value = str(request.fingerprint or "").casefold()
        return value if request.kind in BITTORRENT_REQUEST_KINDS and _INFO_HASH.fullmatch(value) else None

    def _immediate_source(self, request: TransferRequest) -> str | None:
        """What ``directdl`` is asked about for a root request: the hoster link
        or magnet itself, or the magnet of a torrent file's info-hash. An NZB
        has no immediate form."""
        if request.kind in {"http", "https", "magnet"}:
            return str(request.payload)
        digest = self._info_hash(request)
        return f"magnet:?xt=urn:btih:{digest}" if request.kind == "torrent" and digest else None

    # -- resolution ---------------------------------------------------------------

    @normalized_boundary(Stage.RESOLUTION)
    async def resolve(self, request: TransferRequest) -> ResolutionResult:
        member = parse_member_address(request.payload) if request.kind == "https" else None
        if member is not None:
            return ResolutionResult(ResourceState.AVAILABLE, (await self._member(request, member),))
        if request.kind not in self.descriptor.request_types:
            raise TransferError(NormalizedError(Domain.REQUEST, Category.UNSUPPORTED_REQUEST, Stage.SUBMISSION,
                                                Retryability.NEVER, origin=Origin.USER, integration_id=INTEGRATION_ID))
        try:
            immediate = await self._try_immediate(request)
        except _ImmediateUnavailable as unavailable:
            if request.kind in {"http", "https"} and self._capable("queue", request) is False:
                raise TransferError(unavailable.error) from None
            immediate = None
        if immediate is not None:
            return immediate
        if request.kind in {"http", "https"} and self._capable("queue", request) is False:
            raise TransferError(NormalizedError(Domain.REQUEST, Category.UNSUPPORTED_REQUEST, Stage.RESOLUTION,
                                                Retryability.NEVER, origin=Origin.PROVIDER,
                                                integration_id=INTEGRATION_ID))
        nzb_name = await self._nzb_name(request) if request.kind == "nzb" else ""
        return await self._owned(cloud_resource(await self._create(request), nzb_name=nzb_name), request)

    async def _try_immediate(self, request: TransferRequest) -> ResolutionResult | None:
        """The immediate result, ``None`` when there is no basis to ask for
        one, or ``_ImmediateUnavailable`` when Premiumize gave none. A torrent
        is asked about only on its cache's evidence; a hoster link only when
        the catalogue does not exclude immediate resolution for its service."""
        source = self._immediate_source(request)
        if source is None:
            return None
        if request.kind in BITTORRENT_REQUEST_KINDS:
            digest = self._info_hash(request)
            if digest is None:
                return None
            try:
                (held,) = await self.client.cache_check((digest,))
            except Exception:
                return None             # a cache read proves nothing either way
            if not held:
                return None
        elif self._capable("directdl", request) is False:
            raise _ImmediateUnavailable(NormalizedError(
                Domain.REQUEST, Category.UNSUPPORTED_REQUEST, Stage.RESOLUTION, Retryability.NEVER,
                origin=Origin.PROVIDER, integration_id=INTEGRATION_ID))
        return await self._immediate(request, source)

    async def _immediate(self, request: TransferRequest, source: str) -> ResolutionResult:
        """One ``directdl`` read. A complete result -- every member with a safe
        path, an exact size and a safe link -- is frozen into an immediate
        resource; anything less is no immediate result at all."""
        try:
            content = await self.client.directdl(source)
        except PremiumizeAPIError as exc:
            error = translate_error(exc, secrets=self.client.secrets())
            if exc.code.casefold() in _NO_FALLBACK:
                raise TransferError(error) from None
            raise _ImmediateUnavailable(error) from None
        try:
            members = self._declared_coordinates(request, immediate_members(content, root_name=request.name or ""))
            for record in content:
                validate_provider_download_url(record.get("link"), context="Premiumize download link")
        except (ManifestInvalid, TypeError, ValueError):
            raise _ImmediateUnavailable(protocol_error(Stage.RESOLUTION, "immediate result is incomplete")) from None
        except Exception as exc:
            raise TransferError(translate_error(exc, secrets=self.client.secrets())) from None
        name = members[0].name if len(members) == 1 else (self._declared_name(request) or request.name or "")
        resource_value = immediate_resource(source, name, members)
        observed = ProviderObservation(resource_value, ResourceState.AVAILABLE, name, request=request,
                                       file_manifest=file_manifest(members))
        return ResolutionResult(ResourceState.AVAILABLE, observation=observed)

    def _declared_name(self, request: TransferRequest) -> str:
        """An uploaded ``.torrent``'s own collection name -- its validated
        ``info.name``, from the request's persisted bytes, whose info-hash is
        the one asked about -- or ``""``: a magnet's display name or an upload's
        file name is never the torrent's name."""
        payload, digest = request.payload, self._info_hash(request)
        if request.kind not in TORRENT_FILE_REQUEST_KINDS or not digest or not isinstance(payload, (bytes, bytearray)):
            return ""
        try:
            identity = torrent_identity(payload)
        except TorrentMetainfoRejected:
            return ""
        return (identity.name or "") if identity.info_hash == digest else ""

    def _declared_coordinates(self, request: TransferRequest, members: tuple[NativeMember, ...]):
        """An uploaded ``.torrent``'s immediate members at the torrent's OWN
        collection-relative coordinates, when its verified member tree proves
        them: the torrent's metainfo (the request's persisted bytes, whose
        info-hash is the one asked about) and Premiumize's complete stated
        list correspond one-to-one under exactly one bounded interpretation
        (``coordinate_correspondence``). Each member keeps the path Premiumize
        stated (``native_path``) for every later request. Otherwise -- a
        magnet, unreadable metadata, no single complete correspondence -- the
        members are exactly as stated, never guessed."""
        payload = request.payload
        digest = self._info_hash(request)
        if request.kind not in TORRENT_FILE_REQUEST_KINDS or not digest or not isinstance(payload, (bytes, bytearray)):
            return members
        tree = torrent_member_tree(payload)
        if tree is None or tree.info_hash != digest:
            return members
        try:
            proven = coordinate_correspondence(list(tree.members),
                                               [(member.native_path, member.expected_bytes) for member in members])
        except SelectionUnprovable:
            return members
        declared = {native: path for path, native in proven.mapping.items()}
        return tuple(replace(member, name=declared[member.native_path].rsplit("/", 1)[-1],
                             relative_path=declared[member.native_path]) for member in members)

    async def _create(self, request: TransferRequest) -> str:
        """Create the cloud transfer: a productive mutation with no idempotency
        key, attempted once. Premiumize's own complete refusal says nothing was
        created; anything else that leaves no transfer id may have created one
        (``creation_error``) and is never repeated here."""
        try:
            if request.kind in {"http", "https", "magnet"}:
                return await self.client.create_transfer(source=str(request.payload))
            if request.kind == "torrent":
                if not isinstance(request.payload, (bytes, bytearray)) or not request.payload:
                    raise self._invalid()
                return await self.client.create_transfer(upload=bytes(request.payload),
                                                         name=request.name or "upload.torrent")
            return await self._create_nzb(request)
        except TransferError:
            raise
        except Exception as exc:
            raise TransferError(creation_error(exc, secrets=self.client.secrets())) from None

    @staticmethod
    def _invalid() -> TransferError:
        return TransferError(NormalizedError(Domain.REQUEST, Category.INVALID_REQUEST, Stage.RESOLUTION,
                                             Retryability.NEVER, integration_id=INTEGRATION_ID))

    async def _nzb_name(self, request: TransferRequest) -> str:
        """The posting's useful work name, read once from the canonical staged
        NZB through the one NZB reader (``transfers.nzb``) before anything is
        created: the naming evidence its cloud files are named by. ``""`` when
        it names none -- the cloud's own names then stand."""
        payload = request.payload

        def posted_name() -> str:
            if isinstance(payload, StagedPayload):
                if self.staged_input is None:
                    return ""
                with self.staged_input.opened(payload) as stream:
                    return nzb.read(stream, fallback_name=request.name or "").name
            data = payload.encode("utf-8") if isinstance(payload, str) else payload
            return nzb.parse(data, fallback_name=request.name or "").name if isinstance(
                data, (bytes, bytearray)) and data else ""

        try:
            return nzb.useful_name(await asyncio.to_thread(posted_name))
        except (nzb.InvalidNzb, StagedInputError, OSError):
            return ""

    async def _create_nzb(self, request: TransferRequest) -> str:
        """Upload the canonical staged NZB -- the bytes DebridPulse already
        holds, however it arrived; nothing here refetches it."""
        name = request.name or "posting.nzb"
        payload = request.payload
        if isinstance(payload, StagedPayload):
            if self.staged_input is None:
                raise self._invalid()
            try:
                with self.staged_input.opened(payload) as stream:
                    return await self.client.create_transfer(upload=stream, name=name)
            except StagedInputError:
                raise self._invalid() from None
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            raise self._invalid()
        return await self.client.create_transfer(upload=bytes(payload), name=name)

    async def _owned(self, value: ProviderResource, request: TransferRequest) -> ResolutionResult:
        """The resolution of a transfer Premiumize just created. Its id is
        known, so it reaches DebridPulse's durable provider-resource boundary
        even when the first observation fails: that failure is no proof the
        transfer is gone. It is handed over unready (``UNKNOWN``) and the
        ordinary observation of a bound resource decides what happens next."""
        try:
            observed = replace(await self.observe(value), request=request)
        except TransferError as exc:
            unknown = ProviderObservation(value, ResourceState.UNKNOWN, error=exc.error, request=request)
            return ResolutionResult(ResourceState.UNKNOWN, observation=unknown)
        return ResolutionResult(observed.state, observation=observed, error=observed.error)

    # -- explicit alternative groups: reads and non-productive resolution ---------

    @normalized_boundary(Stage.RESOLUTION)
    async def cache_presence(self, requests: tuple[TransferRequest, ...]) -> tuple[CachePresence, ...]:
        """Whether Premiumize's cache holds each request -- a torrent by its
        info-hash, a hoster link whose service the catalogue lists for cache
        lookups -- in one batched read that creates nothing. Anything else
        (an NZB, a member) is ``UNKNOWN``. A hit is evidence only."""
        items = []
        for request in requests:
            digest = self._info_hash(request)
            if digest:
                items.append(digest)
            elif (request.kind in {"http", "https"} and parse_member_address(request.payload) is None
                  and self._capable("cache", request)):
                items.append(str(request.payload))
            else:
                items.append(None)
        asked = tuple(dict.fromkeys(item for item in items if item))
        held = dict(zip(asked, await self._call(self.client.cache_check, asked))) if asked else {}
        return tuple(CachePresence.UNKNOWN if item is None else
                     CachePresence.HIT if held[item] else CachePresence.MISS for item in items)

    @normalized_boundary(Stage.RESOLUTION)
    async def resolve_cached(self, request: TransferRequest) -> ResolutionResult | None:
        """``resolve`` without any cloud acquisition: the immediate result
        only, or ``None`` -- nothing created -- when there is none now."""
        if parse_member_address(request.payload) is not None or request.kind not in self.descriptor.request_types:
            return None
        try:
            return await self._try_immediate(request)
        except _ImmediateUnavailable:
            return None

    # -- members ------------------------------------------------------------------------

    async def _member(self, request: TransferRequest, member: tuple[str, ...]) -> TransferCandidate:
        """Fresh material for one member: a link generated now, from the
        member's durable identity -- a cloud file id, or an immediate member's
        exact native path and size proven against a new ``directdl`` answer.
        The link is execution material only and is never durable."""
        if member[0] == CLOUD_MEMBER:
            details = await self.client.item_details(member[1])
            if native_id(details.get("id")) != member[1]:
                raise TransferError(protocol_error(Stage.CANDIDATE_PREPARATION, "cloud file identity mismatch"))
            name, size, link = details.get("name"), details.get("size"), details.get("link")
            source = API_HOST
        else:
            _kind, source_link, path, size_text = member
            content = await self.client.directdl(source_link)
            try:
                members = immediate_members(content)
            except (ManifestInvalid, TypeError, ValueError):
                raise TransferError(protocol_error(Stage.CANDIDATE_PREPARATION,
                                                   "immediate result is incomplete")) from None
            matches = [index for index, item in enumerate(members)
                       if item.native_path == path and item.expected_bytes == int(size_text)]
            if len(matches) != 1:
                # Never by position, never the only link because it is the
                # only one: the exact member, uniquely, or nothing.
                raise TransferError(protocol_error(
                    Stage.CANDIDATE_PREPARATION,
                    "immediate member is not in the result" if not matches else "immediate member is ambiguous"))
            name, size, link = members[matches[0]].name, members[matches[0]].expected_bytes, content[matches[0]]["link"]
            source = urlsplit(source_link).hostname if urlsplit(source_link).scheme in {"http", "https"} else API_HOST
        try:
            endpoint = validate_provider_download_url(link, context="Premiumize download link")
        except Exception as exc:
            raise TransferError(translate_error(exc, stage=Stage.CANDIDATE_PREPARATION,
                                                secrets=self.client.secrets())) from None
        name = name.strip() if isinstance(name, str) else ""
        size = size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else 0
        return TransferCandidate(
            name or request.name or "download", (Endpoint(urlsplit(endpoint).scheme, endpoint, transient=True),),
            size, provider_id=INTEGRATION_ID, refresh_request=request,
            source_identity=SourceIdentity("host", safe_public_host(source or "") or API_HOST),
            resolver_identity_evidence=(ResolverArtifactIdentityEvidence(resolved_name=name, exact_bytes=size)
                                        if name and size > 0 else None),
            delivery=DeliveryKind.PROVIDER_ISSUED,
        )

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

    # -- observation and the executable manifest -----------------------------------------

    async def _transfer(self, transfer_id: str, *, stage: Stage) -> dict | None:
        """The transfer's current record, or ``None`` when Premiumize no
        longer lists it. Premiumize offers no per-transfer read."""
        for record in await self._call(self.client.list_transfers, stage=stage):
            if isinstance(record, dict) and native_id(record.get("id")) == transfer_id:
                return record
        return None

    async def _cloud_files(self, native: dict, resource_value: ProviderResource, *,
                           stage: Stage) -> tuple[NativeMember, ...]:
        """Every file of a finished transfer, from its one file or its whole
        folder tree, with exact paths, sizes and stable file ids -- complete
        or not at all -- logically named (``named_cloud_members``) from the
        naming evidence the resource retained."""
        nzb_name = resource_value.context.get("nzb_name")
        file_id = native_id(native.get("file_id"))
        if file_id:
            details = await self._call(self.client.item_details, file_id, stage=stage)
            if native_id(details.get("id")) != file_id:
                raise TransferError(protocol_error(stage, "cloud file identity mismatch"))
            return named_cloud_members(cloud_members([([details.get("name")], details.get("size"), file_id)]),
                                       nzb_name)
        files, pending, seen = [], [((), native_id(native.get("folder_id")), 0)], set()
        while pending:
            prefix, folder_id, depth = pending.pop()
            if folder_id is None or depth > _MAX_FOLDER_DEPTH:
                raise TransferError(protocol_error(stage, "cloud folder tree is not enumerable"))
            if folder_id in seen:
                # A folder met twice is a cycle or a shared folder: never read again.
                raise TransferError(protocol_error(stage, "cloud folder graph repeats a folder"))
            if len(seen) >= _MAX_CLOUD_FOLDERS:
                raise TransferError(protocol_error(stage, "cloud folder tree has too many folders"))
            seen.add(folder_id)
            listing = await self._call(self.client.folder_list, folder_id, stage=stage)
            if native_id(listing.get("folder_id")) != folder_id:
                # Not the folder asked for: none of its contents is consumed.
                raise TransferError(protocol_error(stage, "cloud folder identity mismatch"))
            for entry in listing["content"]:
                if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                    raise TransferError(protocol_error(stage, "cloud folder entry is malformed"))
                if entry.get("type") == "folder":
                    pending.append(((*prefix, entry["name"]), native_id(entry.get("id")), depth + 1))
                elif entry.get("type") == "file":
                    files.append(([*prefix, entry["name"]], entry.get("size"), native_id(entry.get("id"))))
                else:
                    raise TransferError(protocol_error(stage, "cloud folder entry is malformed"))
                if len(files) > _MAX_CLOUD_FILES:
                    raise TransferError(protocol_error(stage, "cloud folder tree is too large"))
        return named_cloud_members(cloud_members(sorted(files, key=lambda item: item[0])), nzb_name)

    @normalized_boundary(Stage.RECONCILIATION)
    async def observe(self, resource_value: ProviderResource) -> ProviderObservation:
        if resource_mode(resource_value) == IMMEDIATE:
            # Frozen when Premiumize stated it; observing never asks again.
            context = resource_value.context
            members = tuple(NativeMember(path.rsplit("/", 1)[-1], path, size, native_path=native_path)
                            for path, size, native_path in context["members"])
            return ProviderObservation(resource_value, ResourceState.AVAILABLE, str(context["name"]),
                                       file_manifest=file_manifest(members))
        native = await self._transfer(resource_value.context["transfer_id"], stage=Stage.RECONCILIATION)
        if native is None:
            return ProviderObservation(resource_value, ResourceState.ABSENT, error=NormalizedError(
                Domain.PROVIDER, Category.RESOURCE_NOT_FOUND, Stage.RECONCILIATION,
                Retryability.AFTER_RERESOLUTION, origin=Origin.PROVIDER, integration_id=INTEGRATION_ID))
        observed = observation(native, resource_value)
        if observed.state != ResourceState.AVAILABLE:
            return observed
        try:
            members = await self._cloud_files(native, resource_value, stage=Stage.RECONCILIATION)
        except (ManifestInvalid, TypeError, ValueError):
            raise TransferError(protocol_error(Stage.RECONCILIATION, "cloud files are not a safe tree")) from None
        return replace(observed, file_manifest=file_manifest(members))

    @normalized_boundary(Stage.CANDIDATE_PREPARATION)
    async def manifest(self, resource_value: ProviderResource) -> tuple[SourceEntry, ...]:
        """Every file of the resource, in the SAME collection-root-relative
        coordinates its observation published, each addressed by its durable
        Premiumize identity -- never by a link."""
        stage = Stage.CANDIDATE_PREPARATION
        if resource_mode(resource_value) == IMMEDIATE:
            context = resource_value.context
            return tuple(SourceEntry(path.rsplit("/", 1)[-1], size, path, TransferRequest(
                "https", immediate_member_address(context["source"], native_path, size),
                path.rsplit("/", 1)[-1], preferred_provider=INTEGRATION_ID))
                for path, size, native_path in context["members"])
        native = await self._transfer(resource_value.context["transfer_id"], stage=stage)
        if native is None or observation(native, resource_value).state != ResourceState.AVAILABLE:
            raise TransferError(protocol_error(stage, "transfer files are not available"))
        try:
            members = await self._cloud_files(native, resource_value, stage=stage)
        except ManifestInvalid:
            raise TransferError(NormalizedError(Domain.SECURITY, Category.PATH_POLICY_VIOLATION, stage,
                                                integration_id=INTEGRATION_ID)) from None
        return tuple(SourceEntry(member.name, member.expected_bytes, member.relative_path, TransferRequest(
            "https", cloud_member_address(member.file_id), member.name, preferred_provider=INTEGRATION_ID))
            for member in members)

    # -- account inventory, cleanup and health ----------------------------------------

    @normalized_boundary(Stage.RECONCILIATION)
    async def inventory(self) -> ResourceSnapshot:
        """Every transfer on the account (``transfer/list``) -- transfers,
        never the cloud's files, which are not DebridPulse's resources."""
        observations = []
        for record in await self._call(self.client.list_transfers, stage=Stage.RECONCILIATION):
            transfer_id = native_id(record.get("id")) if isinstance(record, dict) else None
            if transfer_id is None:
                raise TransferError(protocol_error(Stage.RECONCILIATION, "transfer list is malformed"))
            observations.append(observation(record, cloud_resource(transfer_id, ownership=Ownership.OBSERVED)))
        observations.sort(key=lambda item: item.resource.context["transfer_id"])
        return ResourceSnapshot(tuple(observations), complete=True)

    @normalized_boundary(Stage.CLEANUP)
    async def cleanup(self, directive: CleanupDirective) -> TransferOutcome:
        """Only the transfer DebridPulse created or adopted, through
        ``transfer/delete``. An immediate result owns nothing on Premiumize,
        and no cloud file or folder is ever deleted here."""
        resource_value = directive.resource
        if resource_mode(resource_value) == IMMEDIATE:
            return TransferOutcome(OutcomeKind.SKIPPED, detail="Immediate result owns no Premiumize resource")
        if (directive.authority == CleanupAuthority.OWNED
                and resource_value.ownership not in {Ownership.CREATED, Ownership.ADOPTED}):
            return TransferOutcome(OutcomeKind.SKIPPED, detail="Observed provider resource retained")
        try:
            await self._call(self.client.delete_transfer, resource_value.context["transfer_id"], stage=Stage.CLEANUP)
        except TransferError as exc:
            if exc.error.category != Category.RESOURCE_NOT_FOUND:
                return TransferOutcome(OutcomeKind.FAILURE, exc.error)
        return TransferOutcome(OutcomeKind.SUCCESS)

    @normalized_boundary(Stage.RESOLUTION)
    async def health(self) -> HealthObservation:
        try:
            await self._call(self.client.account_info)
        except TransferError as exc:
            return HealthObservation(False, exc.error)
        return HealthObservation(True)
