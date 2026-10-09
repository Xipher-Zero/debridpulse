"""The Media Downloads provider: web media recognized by an explicit yt-dlp
extractor, resolved into one planned FILE or a complete bounded collection.

A provider of facts only. Applicability is the installed extractor registry's
own pure answer (``plan.explicit_extractor``); ``GenericIE`` never claims, so
an ordinary file URL stays with ordinary HTTP. A claim speaks only for the
medium it names (``ProviderApplicability.collection_authority`` is ``False``):
links pasted beside it keep their own claimants.

Resolution reads the medium through the sandboxed worker (``extract``,
injected by the integration so this provider never imports its executor) and
plans it (``providers.media.plan``). A single medium becomes one FILE
candidate whose durable context is the plan -- extractor and native id, the
selected native format ids, the chosen subtitle, the final container and the
preferences it was made from; never a media, manifest or subtitle URL. A
playlist becomes a manifest of independent members: its first
``COLLECTION_BOUND`` entries in source order, read once and kept as the
resource's snapshot (whether the source holds more, and its stated total, kept
beside it), every member planned before the manifest exists (so each member's
file name, extension included, is final before core commits it) or carrying
its own outcome; core owns the operator's explicit selection and fan-out.
Every failure is a fact about the medium or this machine
(``integrations.media.outcomes``), so a claimed medium never falls through to
another provider.

A link naming one video AND its enclosing playlist
(``plan.names_item_in_collection``) declares the two acquisition scopes; the
operator's answer is the request's ``acquisition_scope``. ``item`` reads only
that medium (yt-dlp's own single-item reading) and plans it at its own page
address; ``collection`` reads the playlist. Without an answer it is refused,
never guessed.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from integrations.media.outcomes import MediaFailure, outcome_error
from providers.media import plan as planning
from transfers.applicability import AcquisitionScope, HostClaim, ProviderApplicability, parse_url_applicability
from transfers.errors import Stage, TransferError
from transfers.filesystem import safe_name
from transfers.models import (
    Capability, FileManifest, FileManifestEntry, IntegrationDescriptor, MaterializationKind, Ownership,
    ProviderObservation, ProviderResource, ResolutionResult, ResourceState, SourceEntry, SourceIdentity,
    TransferCandidate, TransferRequest,
)

PROVIDER_ID = "media"
# A collection member: its payload is the member's page address (or, for a
# member that could not be planned, its outcome) and its name the file the
# manifest committed to.
MEMBER_KIND = "media-member"
# The candidate-context key both halves of the integration agree on.
PLAN_KEY = "media_plan"
_PLAIN = frozenset({"http", "https"})
_LIVE = frozenset({"is_live", "is_upcoming", "post_live"})


def _failure(code: str, detail: str = "") -> TransferError:
    return TransferError(outcome_error(code, Stage.RESOLUTION, detail=detail, integration_id=PROVIDER_ID))


class MediaProvider:
    descriptor = IntegrationDescriptor(
        PROVIDER_ID, "Media Downloads",
        frozenset({Capability.RESOLVE, Capability.RESOURCE_LOOKUP, Capability.FILE_MANIFEST}),
        request_types=frozenset({*_PLAIN, MEMBER_KIND}),
    )

    def __init__(self, extract, *, target_resolution: str = "best", video_quality: str = "high",
                 video_codec: str = "auto", subtitle_language: str = "en"):
        # ``extract(url, selection=..., collection_bound=..., single_item=...)``:
        # the worker's read-only extraction, raising ``MediaFailure``.
        self.extract = extract
        self.target_resolution = target_resolution
        self.video_quality = video_quality
        self.video_codec = video_codec
        self.subtitle_language = subtitle_language

    # -- applicability ----------------------------------------------------------------

    def applicability_for(self, request: TransferRequest) -> ProviderApplicability:
        """Pure: a member is this provider's alone; an HTTP(S) address only
        when an explicit installed extractor recognizes it."""
        kind = str(getattr(request, "kind", "") or "").casefold()
        if kind == MEMBER_KIND:
            return ProviderApplicability(collection_authority=False)
        view = parse_url_applicability(request) if kind in _PLAIN else None
        if view is None or planning.explicit_extractor(str(request.payload)) is None:
            return ProviderApplicability()
        return ProviderApplicability(
            specialized_hosts=(HostClaim(view.hostname, schemes=frozenset({kind})),), collection_authority=False,
            acquisition_scopes=(planning.ACQUISITION_SCOPES
                                if planning.names_item_in_collection(str(request.payload)) else ()))

    # -- resolution -------------------------------------------------------------------

    @staticmethod
    def _address(request: TransferRequest) -> str:
        payload = request.payload
        if not isinstance(payload, str) or any(ord(char) <= 32 or ord(char) == 127 for char in payload):
            raise _failure("unsupported")
        try:
            parsed = urlsplit(payload)
            parsed.port  # noqa: B018 -- raises for a malformed port
        except ValueError:
            raise _failure("unsupported") from None
        if parsed.scheme.casefold() not in _PLAIN or not parsed.hostname or parsed.username is not None \
                or parsed.password is not None:
            raise _failure("unsupported")
        address = urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path or "/", parsed.query, ""))
        if planning.explicit_extractor(address) is None:
            raise _failure("unsupported")
        return address

    async def _facts(self, address: str, *, single_item: bool = False) -> dict:
        try:
            return await self.extract(address, selection=planning.selection(self.target_resolution),
                                      collection_bound=planning.COLLECTION_BOUND, single_item=single_item)
        except MediaFailure as exc:
            raise _failure(exc.code, exc.detail) from None

    def _plan(self, facts: dict, address: str) -> dict:
        if str(facts.get("live_status") or "") in _LIVE:
            raise MediaFailure("live_unsupported", "live media")
        try:
            return planning.plan(facts, url=address, target=self.target_resolution,
                                 subtitle_language=self.subtitle_language, video_quality=self.video_quality,
                                 video_codec=self.video_codec)
        except ValueError as exc:
            raise MediaFailure(str(exc)) from None

    async def resolve(self, request: TransferRequest) -> ResolutionResult:
        if not isinstance(request, TransferRequest) or request.kind not in self.descriptor.request_types:
            raise _failure("unsupported")
        member = request.kind == MEMBER_KIND
        if member and not str(request.payload or "").startswith(("http://", "https://")):
            # A member the manifest could not plan answers with its own outcome.
            raise _failure(str(request.payload or "unavailable"))
        address = self._address(request)
        scope = str(getattr(request, "acquisition_scope", "") or "")
        if member:
            # A member is one medium by definition, read as itself.
            scope = AcquisitionScope.ITEM
        elif planning.names_item_in_collection(address):
            if scope not in set(AcquisitionScope):
                raise _failure("scope_choice_required")
        elif scope:
            # An answer to a question this link never asked is not an answer.
            raise _failure("unsupported", "no acquisition scope applies to this link")
        single = scope == AcquisitionScope.ITEM
        facts = await self._facts(address, single_item=single)
        if facts.get("kind") == "collection":
            if single:
                raise _failure("unsupported", "nested collection")
            return self._collection(facts)
        if single and not member:
            # The chosen item is planned at its own page address: the executor's
            # later re-read names that medium alone, never the playlist.
            page = str(facts.get("webpage_url") or "")
            if not page.startswith(("http://", "https://")) or planning.explicit_extractor(page) is None:
                raise _failure("unsupported", "the item has no address of its own")
            address = page
        try:
            planned = self._plan(facts, address)
        except MediaFailure as exc:
            raise _failure(exc.code, exc.detail) from None
        name = planning.file_name(str(facts.get("title") or ""), planned["id"], planned["container"])
        if member:
            # The manifest already committed this member's file: a fresh plan
            # that would produce another container is not that file.
            if PurePosixPath(str(request.name or "")).suffix[1:].casefold() != planned["container"]:
                raise _failure("plan_changed", "the member's container changed since its manifest")
            name = str(request.name)
        candidate = TransferCandidate(
            name=name,
            # Nothing addressable is handed to core: the executor re-reads the
            # medium itself, under the plan, immediately before acquiring it.
            endpoints=(),
            provider_id=self.descriptor.id,
            context={PLAN_KEY: planned},
            source_identity=SourceIdentity("media", f"{planned['extractor']}:{planned['id']}"),
            materialization=MaterializationKind.FILE,
        )
        return ResolutionResult(ResourceState.AVAILABLE, (candidate,))

    # -- collections ------------------------------------------------------------------

    def _collection(self, facts: dict) -> ResolutionResult:
        members, seen_ids, seen_paths = [], set(), set()
        for item in facts.get("members") or ():
            media_id = str(item.get("id") or "")
            if media_id and media_id in seen_ids:
                continue
            seen_ids.add(media_id)
            title = str(item.get("title") or "")
            address = str(item.get("url") or "")
            outcome = str(item.get("outcome") or "")
            container = ""
            if not outcome:
                if not address.startswith(("http://", "https://")) or planning.explicit_extractor(address) is None:
                    outcome = "unsupported"
                else:
                    try:
                        container = self._plan(item, address)["container"]
                    except MediaFailure as exc:
                        outcome = exc.code
            leaf = planning.file_name(title, media_id, "" if outcome else container)
            if leaf.casefold() in seen_paths:
                stem, dot, extension = leaf.rpartition(".") if not outcome else (leaf, "", "")
                leaf = f"{stem} ({len(members) + 1}){dot}{extension}"
            seen_paths.add(leaf.casefold())
            members.append([leaf, "" if outcome else address, outcome])
        if not members:
            raise _failure("unavailable", "the collection is empty")
        name = safe_name(str(facts.get("title") or facts.get("id") or "Media"))
        total = facts.get("total")
        resource = ProviderResource(self.descriptor.id, {
            "name": name, "members": members, "truncated": facts.get("truncated") is True,
            "total": total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else None,
        }, Ownership.OBSERVED)
        return ResolutionResult(ResourceState.AVAILABLE, observation=self._observation(resource))

    def _observation(self, resource: ProviderResource) -> ProviderObservation:
        context = resource.context
        return ProviderObservation(resource, ResourceState.AVAILABLE, safe_name(context["name"]),
                                   file_manifest=FileManifest(
                                       tuple(FileManifestEntry(leaf, leaf, 0)
                                             for leaf, _address, _outcome in context["members"]),
                                       independent_members=True,
                                       source_truncated=context.get("truncated") is True,
                                       source_total=context.get("total")))

    async def observe(self, resource: ProviderResource) -> ProviderObservation:
        # The collection was read once -- its bounded snapshot -- and observing
        # never reads it again: a restart or a later look never rediscovers it.
        return self._observation(resource)

    async def manifest(self, resource: ProviderResource) -> tuple[SourceEntry, ...]:
        return tuple(SourceEntry(leaf, 0, leaf, TransferRequest(MEMBER_KIND, address or outcome, name=leaf))
                     for leaf, address, outcome in resource.context["members"])
