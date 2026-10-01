"""Resolution-only provider for WebDAV files and collections.

Source semantics only. Two request forms, one interpretation (``_checked``):

* the explicit aliases ``webdav://`` and ``dav://`` (WebDAV over HTTP) and
  ``webdavs://`` and ``davs://`` (over HTTPS) -- unambiguous WebDAV requests,
  so this provider is their only claimant and a failure to resolve one is an
  ordinary failure, never a reason to try plain HTTP;
* a plain ``http://`` or ``https://`` URL whose path ends in ``/`` and which
  carries no query -- the directory intent a WebDAV collection is written
  with. This claim is
  conditional (``ProviderApplicability.conditional``): when the server
  positively answers that it does not speak WebDAV there, the request is
  declined (``ResolutionResult.declined``) and core continues the same
  competition without it. A plain URL without the slash, or with a query, is
  never claimed.

A query has no WebDAV collection meaning here (members could not carry it),
so an explicit alias with one is refused at this boundary rather than
discovered and failed later.

The alias is request syntax, never a wire transport: everything this provider
names is an ordinary ``http``/``https`` address. The server's own answer,
through core-run discovery, classifies the path: a file becomes one ordinary
candidate owned by this provider; a collection -- listed to the configured
``DiscoveryDepth`` -- has its regular files frozen into the existing neutral
file manifest, never re-listed on observation or retry, each member an
ordinary ``http``/``https`` request that knows nothing of WebDAV.

This provider opens no connection, lists nothing, holds no credential and
decides no trust: discovery is core's, the transport belongs to execution, and
access input belongs to the universal authentication-input lifecycle.
"""
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from transfers.applicability import ProviderApplicability
from transfers.errors import Category, Confidence, Domain, EvidenceBasis, NormalizedError, Retryability, Stage, TransferError
from transfers.filesystem import safe_name
from transfers.models import (
    Capability, DiscoveryDepth, DiscoveryRequest, Endpoint, FileManifest, FileManifestEntry, InputMethod,
    IntegrationDescriptor, Ownership, ProviderObservation, ProviderResource, RemoteObjectKind, ResolutionResult,
    ResourceState, SourceEntry, SourceIdentity, TransferCandidate, TransferRequest,
)

# The WebDAV request syntax and the HTTP(S) transport it is spoken over.
_ALIASES = {"webdav": "http", "dav": "http", "webdavs": "https", "davs": "https"}
_PLAIN = frozenset({"http", "https"})
_ACCEPTED_INPUT = (InputMethod.USERNAME_PASSWORD,)


def _member_segments(path: str) -> tuple[str, ...] | None:
    """A discovered member path as literal segments, or ``None`` when it could
    not name a file strictly below the discovered collection."""
    if not isinstance(path, str) or not path or path.startswith("/"):
        return None
    segments = tuple(path.split("/"))
    if any(item in {"", ".", ".."} or any(ord(char) < 32 or ord(char) == 127 for char in item)
           for item in segments):
        return None
    return segments


class GeneralWebdavProvider:
    descriptor = IntegrationDescriptor(
        "general_webdav", "WebDAV",
        frozenset({Capability.RESOLVE, Capability.RESOURCE_LOOKUP, Capability.FILE_MANIFEST}),
        request_types=frozenset({*_ALIASES, *_PLAIN}),
    )

    def __init__(self, depth: DiscoveryDepth = DiscoveryDepth.CURRENT):
        self.depth = depth

    def applicability_for(self, request: TransferRequest) -> ProviderApplicability:
        """An alias is this provider's alone; a slash-terminated plain URL is
        its claim only if discovery proves WebDAV there; nothing else is."""
        kind = str(getattr(request, "kind", "") or "").casefold()
        if kind in _ALIASES:
            return ProviderApplicability(generic_schemes=frozenset({kind}))
        if kind in _PLAIN and isinstance(request.payload, str):
            try:
                parts = urlsplit(request.payload)
            except ValueError:
                return ProviderApplicability()
            if parts.path.endswith("/") and not parts.query:
                return ProviderApplicability(generic_schemes=frozenset({kind}), conditional=True)
        return ProviderApplicability()

    def _failure(self, category: Category, *, domain=Domain.REQUEST, diagnostic: str = "") -> TransferError:
        return TransferError(NormalizedError(
            domain, category, Stage.RESOLUTION, retryability=Retryability.NEVER,
            integration_id=self.descriptor.id, confidence=Confidence.HIGH,
            evidence_basis=EvidenceBasis.STRUCTURED, diagnostic=diagnostic,
        ))

    def _checked(self, request: TransferRequest) -> tuple[str, str]:
        """``(address, host)`` of one structurally valid WebDAV request.

        THE one interpretation: ``address`` is the ordinary HTTP(S) URL the
        request names -- the alias's transport, the host (IPv6 bracketed), the
        port only when one was given and the path (``/`` when empty). A query is
        refused (``UNSUPPORTED_REQUEST``): discovery could not represent it and
        no member could carry it. A fragment never reaches a server and is
        dropped."""
        if not isinstance(request, TransferRequest) or request.kind not in self.descriptor.request_types:
            raise self._failure(Category.UNSUPPORTED_REQUEST)
        if not isinstance(request.payload, str) or any(ord(char) <= 32 or ord(char) == 127
                                                       for char in request.payload):
            raise self._failure(Category.INVALID_REQUEST)
        parsed = urlsplit(request.payload)
        scheme = parsed.scheme.lower()
        if scheme != request.kind or not parsed.netloc:
            raise self._failure(Category.INVALID_REQUEST)
        try:
            port = parsed.port  # raises on a non-numeric or out-of-range port
        except ValueError:
            raise self._failure(Category.INVALID_REQUEST) from None
        if port == 0:
            raise self._failure(Category.INVALID_REQUEST)
        if parsed.username is not None or parsed.password is not None:
            # Core splits credentials out at admission; a provider never sees them.
            raise self._failure(Category.SECURITY_POLICY_REJECTED, domain=Domain.SECURITY)
        if parsed.query:
            raise self._failure(Category.UNSUPPORTED_REQUEST, diagnostic="query_not_supported")
        hostname = str(parsed.hostname or "")
        host = hostname.strip().lower().rstrip(".")
        if not host:
            raise self._failure(Category.INVALID_REQUEST)
        authority = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None:
            authority = f"{authority}:{port}"
        address = urlunsplit((_ALIASES.get(scheme, scheme), authority, parsed.path or "/", "", ""))
        return address, host

    def _located(self, address: str, discovered) -> tuple[str, str, str]:
        """``(scheme, address, host)`` where the server finally described the
        path: the request's own address, or where it moved it."""
        location = str(getattr(discovered, "location", "") or "") or address
        parsed = urlsplit(location)
        host = str(parsed.hostname or "").strip().lower().rstrip(".")
        if parsed.scheme not in _PLAIN or not host or parsed.username is not None or parsed.password is not None:
            raise self._failure(Category.PROTOCOL_ERROR, domain=Domain.RESOLUTION)
        return parsed.scheme, location, host

    async def resolve(self, request: TransferRequest) -> ResolutionResult:
        address, _host = self._checked(request)
        scheme = urlsplit(address).scheme
        # The server proves what the path is; a collection is listed to the
        # configured depth (a file ignores it).
        return ResolutionResult(ResourceState.PREPARING, discovery=DiscoveryRequest(
            Endpoint(scheme, address), _ACCEPTED_INPUT, depth=self.depth))

    async def resolve_discovered(self, request: TransferRequest, discovered) -> ResolutionResult:
        """A path the server answered without WebDAV is declined for a plain
        URL and fails an explicit alias; a proven file is one candidate; a
        proven collection freezes its regular files into the durable resource."""
        address, _host = self._checked(request)
        if discovered.kind == RemoteObjectKind.OPAQUE:
            if request.kind in _PLAIN:
                return ResolutionResult(ResourceState.UNKNOWN, declined=True)
            raise self._failure(Category.PROTOCOL_ERROR, domain=Domain.RESOLUTION, diagnostic="not_webdav")
        scheme, location, host = self._located(address, discovered)
        path = urlsplit(location).path
        if discovered.kind == RemoteObjectKind.FILE:
            leaf = unquote(path.rstrip("/").rsplit("/", 1)[-1])
            name = safe_name(request.name or leaf) or safe_name(leaf) or host
            return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
                name=name,
                endpoints=(Endpoint(scheme, location),),
                expected_bytes=max(0, int(discovered.expected_bytes or 0)),
                provider_id=self.descriptor.id,
                source_identity=SourceIdentity("host", host),
                accepted_input_methods=_ACCEPTED_INPUT,
            ),))
        members = []
        for entry in discovered.entries:
            member = entry.relative_path or entry.name
            if _member_segments(member) is None:
                raise self._failure(Category.PROTOCOL_ERROR, domain=Domain.RESOLUTION)
            members.append((member, max(0, int(entry.expected_bytes or 0))))
        if len({member for member, _size in members}) != len(members):
            raise self._failure(Category.PROTOCOL_ERROR, domain=Domain.RESOLUTION)
        if not members:
            # An empty collection is a missing source, never an empty download.
            raise TransferError(NormalizedError(
                Domain.RESOLUTION, Category.SOURCE_NOT_FOUND, Stage.RESOLUTION, retryability=Retryability.NEVER,
                integration_id=self.descriptor.id, confidence=Confidence.HIGH,
                evidence_basis=EvidenceBasis.STRUCTURED))
        parts = urlsplit(location)
        base = parts.path if parts.path.endswith("/") else parts.path + "/"
        resource = ProviderResource(self.descriptor.id, {
            "kind": scheme, "source": urlunsplit((parts.scheme, parts.netloc, base, "", "")),
            "members": [list(item) for item in sorted(members)],
            "name": unquote(base.rstrip("/").rsplit("/", 1)[-1]) or host,
        }, Ownership.OBSERVED)
        return ResolutionResult(ResourceState.AVAILABLE, observation=self._observation(resource))

    def _observation(self, resource: ProviderResource) -> ProviderObservation:
        return ProviderObservation(resource, ResourceState.AVAILABLE, safe_name(resource.context["name"]),
                                   file_manifest=FileManifest(tuple(
                                       FileManifestEntry(path.rsplit("/", 1)[-1], path, size)
                                       for path, size in resource.context["members"])))

    async def observe(self, resource: ProviderResource) -> ProviderObservation:
        # The member set was frozen at discovery; observing never re-lists.
        return self._observation(resource)

    async def manifest(self, resource: ProviderResource) -> tuple[SourceEntry, ...]:
        context = resource.context
        entries = []
        for path, size in context["members"]:
            leaf = path.rsplit("/", 1)[-1]
            address = context["source"] + "/".join(quote(item, safe="") for item in path.split("/"))
            # An ordinary HTTP(S) request: what serves it is decided by the
            # same routing every other link meets.
            entries.append(SourceEntry(leaf, size, path, TransferRequest(context["kind"], address, name=leaf)))
        return tuple(entries)
