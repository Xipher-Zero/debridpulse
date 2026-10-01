"""Resolution-only provider for Multimeta: descriptor documents that describe
files and the ordinary sources each one can be acquired from.

The first format it reads is Metalink4 (RFC 5854): an uploaded ``.meta4`` file
(request class ``meta4``), or an ``http``/``https`` address whose path names a
``.meta4`` document. That claim is recognized from the request alone, without
I/O (``ProviderApplicability.specific``); no other HTTP(S) address is ever
claimed or probed.

A decomposer and nothing else. A remote document is read through core-run
discovery (``DiscoveryRequest.content_limit``) -- the executor that claims the
address performs the read, under the one destination, redirect,
authentication-input and private-network owners -- and an uploaded one through
the neutral staged-input owner. The document's files become the existing
neutral file manifest; each file's ``<url>`` references become ordinary
requests of that member (``SourceEntry.alternates``, in the publisher's order
of preference) that core routes like any other link, so every candidate,
choice between sources, failover, retry, continuation, consolidation and
verification of the declared size and whole-file hashes belongs to the
existing lifecycle. This provider opens no connection, holds no credential,
keeps no source state and calls no other provider.

A file with no source this provider can name (only ``<metaurl>`` metadata
references, which are never followed, or no usable ``<url>``) is still a
member of the manifest: its request (``meta4-file``) is answered here with the
ordinary resolution failure that says why, so it is reported where the
operator sees every other member, while the files that can be acquired are.
"""
from __future__ import annotations

from urllib.parse import unquote, urlsplit, urlunsplit

from providers.multimeta.metalink import MAX_DESCRIPTOR_BYTES, DescribedFile, InvalidDescriptor, parse
from transfers.applicability import ProviderApplicability
from transfers.errors import (
    Category, Confidence, Domain, EvidenceBasis, NormalizedError, Retryability, Stage, TransferError,
)
from transfers.filesystem import safe_name
from transfers.models import (
    Capability, DiscoveryRequest, Endpoint, FileManifest, FileManifestEntry, InputMethod, IntegrationDescriptor,
    IntegrityMetadata, Ownership, ProviderObservation, ProviderResource, RemoteObjectKind, ResolutionResult,
    ResourceState, SourceEntry, TransferRequest,
)
from transfers.staged_input import StagedInputError, StagedPayload

# An uploaded descriptor document.
REQUEST_KIND = "meta4"
# A described file with no source this provider can name.
MEMBER_KIND = "meta4-file"
_PLAIN = frozenset({"http", "https"})
_SUFFIX = ".meta4"
_ACCEPTED_INPUT = (InputMethod.USERNAME_PASSWORD,)
# What each refused document means, in the existing normalized vocabulary.
_INVALID = {
    "malformed": (Domain.REQUEST, Category.CONTENT_INVALID, "descriptor_malformed"),
    "unsupported": (Domain.REQUEST, Category.UNSUPPORTED_REQUEST, "descriptor_unsupported"),
    "too_large": (Domain.REQUEST, Category.UNSUPPORTED_REQUEST, "descriptor_too_large"),
    "unsafe_path": (Domain.SECURITY, Category.PATH_POLICY_VIOLATION, "unsafe_path"),
}


def names_descriptor(address: str) -> bool:
    """Whether an address's path names a Metalink4 document."""
    try:
        return urlsplit(address).path.casefold().endswith(_SUFFIX)
    except ValueError:
        return False


class MultimetaProvider:
    descriptor = IntegrationDescriptor(
        "multimeta", "Multimeta",
        frozenset({Capability.RESOLVE, Capability.RESOURCE_LOOKUP, Capability.FILE_MANIFEST}),
        request_types=frozenset({*_PLAIN, REQUEST_KIND, MEMBER_KIND}),
    )

    def __init__(self, staged_input=None):
        # The neutral durable-input owner, borrowed to READ an uploaded
        # document; this provider owns no part of its lifecycle.
        self.staged_input = staged_input

    def applicability_for(self, request: TransferRequest) -> ProviderApplicability:
        """An upload (and a described file) is this provider's alone; an
        HTTP(S) address only when its path names a ``.meta4`` document."""
        kind = str(getattr(request, "kind", "") or "").casefold()
        if kind in _PLAIN and isinstance(request.payload, str) and names_descriptor(request.payload):
            return ProviderApplicability(generic_schemes=frozenset({kind}), specific=True)
        return ProviderApplicability()

    def _failure(self, category: Category, *, domain=Domain.REQUEST, diagnostic: str = "") -> TransferError:
        return TransferError(NormalizedError(
            domain, category, Stage.RESOLUTION, retryability=Retryability.NEVER,
            integration_id=self.descriptor.id, confidence=Confidence.HIGH,
            evidence_basis=EvidenceBasis.STRUCTURED, diagnostic=diagnostic,
        ))

    def _invalid(self, reason: str) -> TransferError:
        domain, category, diagnostic = _INVALID[reason]
        return self._failure(category, domain=domain, diagnostic=diagnostic)

    def _address(self, request: TransferRequest) -> str:
        """The validated HTTP(S) address of a remote document request."""
        if not isinstance(request.payload, str) or any(ord(char) <= 32 or ord(char) == 127
                                                       for char in request.payload):
            raise self._failure(Category.INVALID_REQUEST)
        try:
            parsed = urlsplit(request.payload)
            parsed.port  # noqa: B018 -- raises for a malformed port
        except ValueError:
            raise self._failure(Category.INVALID_REQUEST) from None
        if parsed.scheme.casefold() != request.kind or not parsed.hostname:
            raise self._failure(Category.INVALID_REQUEST)
        if parsed.username is not None or parsed.password is not None:
            # Core splits credentials out at admission; a provider never sees them.
            raise self._failure(Category.SECURITY_POLICY_REJECTED, domain=Domain.SECURITY)
        return urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path or "/", parsed.query, ""))

    async def resolve(self, request: TransferRequest) -> ResolutionResult:
        if not isinstance(request, TransferRequest) or request.kind not in self.descriptor.request_types:
            raise self._failure(Category.UNSUPPORTED_REQUEST)
        if request.kind == MEMBER_KIND:
            raise self._failure(Category.NO_TRANSFER_CANDIDATE, domain=Domain.RESOLUTION,
                                diagnostic=str(request.payload or "no_usable_url"))
        if request.kind == REQUEST_KIND:
            # An upload has no address of its own, so no relative reference
            # in it can name a source.
            return self._decomposed(request, await self._uploaded(request), base=None)
        address = self._address(request)
        return ResolutionResult(ResourceState.PREPARING, discovery=DiscoveryRequest(
            Endpoint(request.kind, address), _ACCEPTED_INPUT, content_limit=MAX_DESCRIPTOR_BYTES))

    async def resolve_discovered(self, request: TransferRequest, discovered) -> ResolutionResult:
        """The document core read: relative references resolve against the
        validated address that finally served it."""
        address = self._address(request)
        if discovered.kind != RemoteObjectKind.FILE or not isinstance(discovered.content, bytes):
            raise self._failure(Category.PROTOCOL_ERROR, domain=Domain.RESOLUTION)
        return self._decomposed(request, discovered.content, base=str(discovered.location or "") or address)

    async def _uploaded(self, request: TransferRequest) -> bytes:
        payload = request.payload
        if isinstance(payload, StagedPayload):
            if self.staged_input is None:
                raise self._failure(Category.INVALID_REQUEST)
            if payload.byte_length > MAX_DESCRIPTOR_BYTES:
                raise self._invalid("too_large")
            try:
                with self.staged_input.opened(payload) as stream:
                    return stream.read(MAX_DESCRIPTOR_BYTES + 1)
            except StagedInputError as exc:
                raise self._failure(Category.INVALID_REQUEST) from exc
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload)
        raise self._failure(Category.INVALID_REQUEST)

    def _decomposed(self, request: TransferRequest, document: bytes, *, base: str | None) -> ResolutionResult:
        try:
            files = parse(document, base=base)
        except InvalidDescriptor as exc:
            raise self._invalid(exc.reason) from None
        if all(item.unusable for item in files):
            raise self._failure(Category.NO_TRANSFER_CANDIDATE, domain=Domain.RESOLUTION,
                                diagnostic="no_usable_file")
        if len(files) == 1:
            name = files[0].path.rsplit("/", 1)[-1]
        else:
            name = self._document_name(request)
        resource = ProviderResource(self.descriptor.id, {
            "name": name,
            "files": [[item.path, item.size, [list(pair) for pair in item.hashes], list(item.sources), item.unusable]
                      for item in files],
        }, Ownership.OBSERVED)
        return ResolutionResult(ResourceState.AVAILABLE, observation=self._observation(resource))

    @staticmethod
    def _document_name(request: TransferRequest) -> str:
        """A multi-file document's own name: the upload's or the address's
        file name, without the descriptor suffix."""
        name = str(request.name or "")
        if not name and isinstance(request.payload, str):
            name = unquote(urlsplit(request.payload).path.rsplit("/", 1)[-1])
        if name.casefold().endswith(_SUFFIX):
            name = name[:-len(_SUFFIX)]
        return safe_name(name) if name.strip() else "Multimeta"

    @staticmethod
    def _files(resource: ProviderResource) -> tuple[DescribedFile, ...]:
        return tuple(DescribedFile(path, int(size), tuple(tuple(pair) for pair in hashes), tuple(sources), unusable)
                     for path, size, hashes, sources, unusable in resource.context["files"])

    def _observation(self, resource: ProviderResource) -> ProviderObservation:
        return ProviderObservation(resource, ResourceState.AVAILABLE, safe_name(resource.context["name"]),
                                   file_manifest=FileManifest(tuple(
                                       FileManifestEntry(item.path.rsplit("/", 1)[-1], item.path, item.size)
                                       for item in self._files(resource))))

    async def observe(self, resource: ProviderResource) -> ProviderObservation:
        # The document was read once; observing never reads it again.
        return self._observation(resource)

    async def manifest(self, resource: ProviderResource) -> tuple[SourceEntry, ...]:
        entries = []
        for item in self._files(resource):
            leaf = item.path.rsplit("/", 1)[-1]
            if item.unusable:
                requests = (TransferRequest(MEMBER_KIND, item.unusable, name=leaf),)
            else:
                # Ordinary requests: what serves each one is decided by the
                # same routing every other link meets.
                requests = tuple(TransferRequest(urlsplit(source).scheme.casefold(), source, name=leaf)
                                 for source in item.sources)
            entries.append(SourceEntry(
                leaf, item.size, item.path, requests[0], alternates=requests[1:],
                integrity=tuple(IntegrityMetadata(algorithm, digest) for algorithm, digest in item.hashes)))
        return tuple(entries)
