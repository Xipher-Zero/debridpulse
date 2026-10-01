"""Resolution-only provider for rsync daemon and rsync-over-SSH sources.

Two source forms, one grammar, interpreted exactly once by ``_source``:

* ``rsync://host[:port]/module/path`` -- an rsync daemon. The first path
  segment is the server's named root (its module); ``rsync://host/`` is the
  server itself, whose top level is the set of roots it advertises.
* ``rsync+ssh://host[:port]/path`` -- rsync over SSH. The path is absolute on
  the server; ``/~/path`` starts at the login directory, which the server
  resolves (another user's ``~name`` is refused).

An operator need not know which one a server offers. A plain ``rsync://``
request with a path and no port of its own names its SSH reading as the
alternate interpretation (``DiscoveryRequest.alternate``); core discovers the
daemon reading first, and only when that reading does not reach the path (an
unknown module, a missing path, no daemon listening, or a daemon port that
stays silent past the Connection Timeout) is the SSH reading tried. A daemon
that answers -- even to ask for a login, or to say it is full -- is
authoritative.

Classification is always the server's, through core-run discovery: what it
proves a regular file becomes one ordinary candidate; what it proves a
directory -- with or without a trailing slash, submitted directly or chosen
in the picker alike -- is the tree beneath it to the configured Directory
Depth (``DiscoveryDepth``; by default the whole tree). That tree is frozen into
the existing neutral file manifest: every regular file at its path below the
directory, the directory's own name the transfer's root, the member set never
re-listed on observation or retry. Symbolic links and special files are never
members and never followed. A path segment is always literal: ``%XX`` escapes
are decoded once here and nothing in a path is a pattern.

This provider opens no connection, runs nothing, holds no credential and
decides no server identity: discovery and execution are core's and the
executor's, access input belongs to the universal authentication-input
lifecycle, and the canonical resource never carries credentials.
"""
from dataclasses import replace
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from transfers.applicability import ProviderApplicability
from transfers.errors import Category, Confidence, Domain, EvidenceBasis, NormalizedError, Retryability, Stage, TransferError
from transfers.filesystem import safe_name
from transfers.models import (
    Capability, DiscoveryDepth, DiscoveryRequest, Endpoint, FileManifest, FileManifestEntry, InputMethod, IntegrationDescriptor,
    Ownership, ProviderObservation, ProviderResource, RemoteObjectKind, ResolutionResult,
    ResolverArtifactIdentityEvidence, ResourceState, SourceEntry, SourceIdentity, TransferCandidate, TransferRequest,
)
from transfers.requests import remote_object_coordinate

_DAEMON, _SSH = "rsync", "rsync+ssh"
# A daemon authenticates with its own accounts; over SSH the server's own
# password or key login applies, exactly as for SFTP and SCP.
_ACCEPTED_INPUT = {
    _DAEMON: (InputMethod.USERNAME_PASSWORD,),
    _SSH: (InputMethod.USERNAME_PASSWORD, InputMethod.USERNAME_PRIVATE_KEY),
}
_HOME = "~"


class _Source:
    """One parsed source: its canonical executable address (never with a
    trailing slash, except the daemon server root) and its decoded segments."""

    __slots__ = ("kind", "host", "authority", "segments")

    def __init__(self, kind: str, host: str, authority: str, segments: tuple[str, ...]):
        self.kind, self.host, self.authority, self.segments = kind, host, authority, segments

    def address(self, segments: tuple[str, ...] | None = None, *, directory: bool = False) -> str:
        parts = self.segments if segments is None else segments
        path = "/" + "/".join(quote(item, safe="") for item in parts)
        if directory and parts:
            path += "/"
        return urlunsplit((self.kind, self.authority, path, "", ""))

    @property
    def root_name(self) -> str:
        """The collection root a directory of this source is named after."""
        named = [item for item in self.segments if item != _HOME]
        return named[-1] if named else self.host


def _member_segments(path: str) -> tuple[str, ...] | None:
    """A discovered member path as literal segments, or ``None`` when it could
    not name a file strictly below the discovered directory."""
    if not isinstance(path, str) or not path or path.startswith("/"):
        return None
    segments = tuple(path.split("/"))
    if any(item in {"", ".", ".."} or any(ord(char) < 32 or ord(char) == 127 for char in item)
           for item in segments):
        return None
    return segments


class GeneralRsyncProvider:
    applicability = ProviderApplicability(
        generic_schemes=frozenset({_DAEMON, _SSH}),
    )
    descriptor = IntegrationDescriptor(
        "general_rsync", "rsync",
        frozenset({Capability.RESOLVE, Capability.RESOURCE_LOOKUP, Capability.FILE_MANIFEST}),
        request_types=frozenset({_DAEMON, _SSH}),
    )

    def __init__(self, depth: DiscoveryDepth = DiscoveryDepth.UNLIMITED):
        self.depth = depth

    def _failure(self, category: Category, *, domain=Domain.REQUEST) -> TransferError:
        return TransferError(NormalizedError(
            domain, category, Stage.RESOLUTION, retryability=Retryability.NEVER,
            integration_id=self.descriptor.id, confidence=Confidence.HIGH,
            evidence_basis=EvidenceBasis.STRUCTURED,
        ))

    def _source(self, request: TransferRequest) -> _Source:
        """THE one canonical interpretation of an rsync source."""
        if not isinstance(request, TransferRequest) or request.kind not in self.descriptor.request_types:
            raise self._failure(Category.UNSUPPORTED_REQUEST)
        address = request.payload
        if not isinstance(address, str) or any(ord(char) <= 32 or ord(char) == 127 for char in address):
            raise self._failure(Category.INVALID_REQUEST)
        scheme, separator, rest = address.partition("://")
        if not separator or scheme.lower() != request.kind:
            raise self._failure(Category.INVALID_REQUEST)
        authority, _slash, remainder = rest.partition("/")
        # A query or fragment is no part of an rsync resource; a literal "?" or
        # "#" in a name is written percent-escaped.
        if not authority or "?" in rest or "#" in rest:
            raise self._failure(Category.INVALID_REQUEST)
        parsed = urlsplit(f"{request.kind}://{authority}")
        try:
            port = parsed.port  # raises on a non-numeric or out-of-range port
        except ValueError:
            raise self._failure(Category.INVALID_REQUEST) from None
        if port == 0:
            raise self._failure(Category.INVALID_REQUEST)
        if parsed.username is not None or parsed.password is not None:
            # Core splits credentials out at admission; a provider never sees them.
            raise self._failure(Category.SECURITY_POLICY_REJECTED, domain=Domain.SECURITY)
        hostname = str(parsed.hostname or "")
        if not hostname.strip("."):
            raise self._failure(Category.INVALID_REQUEST)

        raw = remainder.split("/") if remainder else []
        if raw and raw[-1] == "":
            raw.pop()  # a trailing slash is directory intent, never a different resource
        segments = tuple(unquote(item) for item in raw)
        if any(not item or item in {".", ".."} or "/" in item
               or any(ord(char) < 32 or ord(char) == 127 for char in item) for item in segments):
            raise self._failure(Category.INVALID_REQUEST)
        if request.kind == _SSH:
            if not segments or segments == (_HOME,):
                # A whole server filesystem or a bare login directory is not a source.
                raise self._failure(Category.INVALID_REQUEST)
            if segments[0].startswith(_HOME) and (segments[0] != _HOME or raw[0] != _HOME):
                raise self._failure(Category.UNSUPPORTED_REQUEST)
        executable = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None:
            executable = f"{executable}:{port}"
        return _Source(request.kind, hostname.strip().lower().rstrip("."), executable, segments)

    def _alternate(self, request: TransferRequest, source: _Source) -> TransferRequest | None:
        """The same plain ``rsync://`` request read as rsync over SSH, when it
        can mean that: a path on the server and no port of its own (a port
        names one service). Core discovers it only if the daemon reading does
        not reach the path (``policy.alternate_interpretation_progresses``)."""
        if source.kind != _DAEMON or not source.segments or urlsplit(request.payload).port is not None:
            return None
        alternate = replace(request, kind=_SSH, payload=_SSH + request.payload[len(_DAEMON):])
        try:
            self._source(alternate)
        except TransferError:
            return None  # a path SSH cannot name (for example a bare "~") has no SSH reading
        return alternate

    async def resolve(self, request: TransferRequest) -> ResolutionResult:
        source = self._source(request)
        # The server proves what the path is; a directory is listed to the
        # configured depth (by default its whole tree; a file ignores it).
        return ResolutionResult(ResourceState.PREPARING, discovery=DiscoveryRequest(
            Endpoint(source.kind, source.address()), _ACCEPTED_INPUT[source.kind], depth=self.depth,
            alternate=self._alternate(request, source)))

    async def resolve_discovered(self, request: TransferRequest, discovered) -> ResolutionResult:
        """A proven regular file is one candidate; a proven directory freezes
        every regular file discovery listed into the durable resource."""
        source = self._source(request)
        if discovered.kind == RemoteObjectKind.FILE:
            if not source.segments or (source.kind == _DAEMON and len(source.segments) == 1):
                # A server or a named root is never one file.
                raise self._failure(Category.PROTOCOL_ERROR, domain=Domain.RESOLUTION)
            address = source.address()
            leaf = source.segments[-1]
            name = safe_name(request.name or leaf) or safe_name(leaf) or leaf
            size = max(0, int(discovered.expected_bytes or 0))
            return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
                name=name,
                endpoints=(Endpoint(source.kind, address),),
                expected_bytes=size,
                provider_id=self.descriptor.id,
                source_identity=SourceIdentity("host", source.host),
                # The server just proved this exact path a regular file: its
                # canonical remote coordinate (an address, never identity).
                resolver_identity_evidence=ResolverArtifactIdentityEvidence(
                    "", size, object_coordinate=remote_object_coordinate(address)),
                accepted_input_methods=_ACCEPTED_INPUT[source.kind],
            ),))
        members = []
        for entry in discovered.entries:
            path = entry.relative_path or entry.name
            if _member_segments(path) is None:
                raise self._failure(Category.PROTOCOL_ERROR, domain=Domain.RESOLUTION)
            members.append((path, max(0, int(entry.expected_bytes or 0))))
        if not members:
            # An empty tree is a missing source, never an empty download.
            raise TransferError(NormalizedError(
                Domain.RESOLUTION, Category.SOURCE_NOT_FOUND, Stage.RESOLUTION, retryability=Retryability.NEVER,
                integration_id=self.descriptor.id, confidence=Confidence.HIGH,
                evidence_basis=EvidenceBasis.STRUCTURED))
        resource = ProviderResource(self.descriptor.id, {
            "kind": source.kind, "source": source.address(directory=True),
            "members": [list(item) for item in sorted(set(members))],
            "name": source.root_name,
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
            entries.append(SourceEntry(leaf, size, path, TransferRequest(context["kind"], address, name=leaf)))
        return tuple(entries)
