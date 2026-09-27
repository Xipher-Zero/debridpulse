"""Resolution-only provider for SCP and remote-file SSH sources.

``scp://`` and ``ssh://`` name remote files reached over SSH. This provider
interprets that input exactly once, in the one parser below, and expresses it
in the executable ``sftp://`` transport, so the existing SFTP-capable executor
claims execution and the existing evidence, server-identity, authentication
and equivalence owners apply unchanged. It never connects, lists, trusts a
server or holds a credential itself.

Three source shapes, one grammar:

* an exact absolute file -- one ordinary candidate;
* a directory (trailing ``/``) -- its immediate regular files;
* a pattern in the FINAL path component only (``*`` any run, ``?`` one
  character; ``%XX`` is always a literal) -- the matching immediate files.

A directory or pattern asks core for one read-only remote discovery of the
containing directory; the neutral result becomes the existing provider-neutral
file manifest, whose member set is frozen in the durable resource, so retries
and recovery never re-enumerate. A path under ``~/`` (the SSH URI form for the
login directory) of any shape is also discovered: the discovery owner resolves
it through SFTP itself, and execution only ever receives the concrete path.
SCP/SSH paths carry no query: a raw ``?`` is a pattern character. Another
user's ``~name``, fragments, bracket classes and patterns in directory
components are refused, and ``ssh://`` never means anything but retrieving
files.
"""
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from transfers.applicability import ProviderApplicability
from transfers.errors import Category, Confidence, Domain, EvidenceBasis, NormalizedError, Retryability, Stage, TransferError
from transfers.filesystem import safe_name
from transfers.models import (
    Capability, DiscoveryRequest, Endpoint, FileManifest, FileManifestEntry, InputMethod, IntegrationDescriptor,
    Ownership, ProviderObservation, ProviderResource, ResolutionResult, ResolverArtifactIdentityEvidence, ResourceState,
    SourceEntry, SourceIdentity, TransferCandidate, TransferRequest,
)
from transfers.requests import direct_link_filename, remote_object_coordinate

# The one executable transport an SCP/SSH source is expressed in.
_EXECUTION_SCHEME = "sftp"
_PATTERN_CHARACTERS = frozenset("*?")
_ACCEPTED_INPUT = (InputMethod.USERNAME_PASSWORD,)


class _Source:
    """One parsed SCP/SSH source: the executable address of the file or the
    containing directory, the operator-facing source base, and the pattern."""

    __slots__ = ("kind", "host", "authority", "path", "pattern", "exact")

    def __init__(self, kind: str, host: str, authority: str, path: str, pattern: str | None, exact: str | None):
        self.kind, self.host, self.authority, self.path = kind, host, authority, path
        self.pattern, self.exact = pattern, exact

    @property
    def discovered(self) -> bool:
        """A directory, a pattern, or anything under ``~/`` needs discovery."""
        return self.path.endswith("/")

    def address(self, scheme: str = _EXECUTION_SCHEME, path: str | None = None) -> str:
        return urlunsplit((scheme, self.authority, self.path if path is None else path, "", ""))


def _pattern(raw: str):
    """Compile one final-component pattern: raw ``*``/``?`` are wildcards,
    everything else (including any percent-escaped character) is literal."""
    parts = []
    for token in re.findall(r"%[0-9A-Fa-f]{2}|.", raw, re.S):
        if token == "*":
            parts.append(".*")
        elif token == "?":
            parts.append(".")
        else:
            parts.append(re.escape(unquote(token)))
    return re.compile("".join(parts), re.S)


def _remote_coordinate(address: str, size: int) -> ResolverArtifactIdentityEvidence:
    """The canonical remote coordinate of an exact executable address (a
    concrete absolute path: parsed here, or resolved by the server for a
    home-relative source) -- an address, never identity. No resolver-asserted
    name is claimed."""
    return ResolverArtifactIdentityEvidence("", size, object_coordinate=remote_object_coordinate(address))


class ScpProvider:
    applicability = ProviderApplicability(
        generic_schemes=frozenset({"scp", "ssh"}),
    )
    descriptor = IntegrationDescriptor(
        "general_scp", "SCP", frozenset({Capability.RESOLVE, Capability.RESOURCE_LOOKUP, Capability.FILE_MANIFEST}),
        request_types=frozenset({"scp", "ssh"}),
    )

    def _failure(self, category: Category, *, domain=Domain.REQUEST) -> TransferError:
        return TransferError(NormalizedError(
            domain, category, Stage.RESOLUTION, retryability=Retryability.NEVER,
            integration_id=self.descriptor.id, confidence=Confidence.HIGH,
            evidence_basis=EvidenceBasis.STRUCTURED,
        ))

    def _source(self, request: TransferRequest) -> _Source:
        """THE one canonical interpretation of an SCP/SSH source.

        The authority comes from the ordinary URI parser, which tells
        ``host:2222/path`` (explicit port) from the SCP-style ``host:/path``
        (default port, absolute path) and parses bracketed IPv6. The path is
        everything after the authority, carried exactly as submitted --
        percent-encoding included -- so the executor decodes it once."""
        if not isinstance(request, TransferRequest) or request.kind not in self.descriptor.request_types:
            raise self._failure(Category.UNSUPPORTED_REQUEST)
        address = request.payload
        if not isinstance(address, str) or any(ord(char) <= 32 or ord(char) == 127 for char in address):
            raise self._failure(Category.INVALID_REQUEST)
        scheme, separator, rest = address.partition("://")
        if not separator or scheme.lower() != request.kind:
            raise self._failure(Category.INVALID_REQUEST)
        authority, slash, remainder = rest.partition("/")
        if not authority or any(char in authority for char in "?#"):
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

        path = "/" + remainder if slash else ""
        if path in {"", "/"}:
            raise self._failure(Category.INVALID_REQUEST)
        segments = path.split("/")
        directories, final = segments[1:-1], segments[-1]
        home = len(segments) > 2 and segments[1] == "~" or path == "/~"
        if ("#" in path or "[" in path or "]" in path or (segments[1].startswith("~") and not home)
                or any(_PATTERN_CHARACTERS.intersection(segment) for segment in directories)):
            raise self._failure(Category.UNSUPPORTED_REQUEST)
        pattern = exact = None
        if _PATTERN_CHARACTERS.intersection(final):
            pattern, path = final, path[:len(path) - len(final)]
        elif home and final:
            # A home-relative file: its directory is resolved by discovery.
            exact, path = final, path[:len(path) - len(final)]
        elif path == "/~":
            path = "/~/"

        executable = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None:
            executable = f"{executable}:{port}"
        return _Source(request.kind, hostname.strip().lower().rstrip("."), executable, path, pattern, exact)

    async def resolve(self, request: TransferRequest) -> ResolutionResult:
        source = self._source(request)
        if source.discovered:
            # A directory or pattern needs the directory's members: core lists
            # it through the executor that would execute them.
            return ResolutionResult(ResourceState.PREPARING, discovery=DiscoveryRequest(
                Endpoint(_EXECUTION_SCHEME, source.address()), _ACCEPTED_INPUT))
        name = safe_name(request.name or direct_link_filename(request.payload))
        if not name:
            name = direct_link_filename(request.payload)
        candidate = TransferCandidate(
            name=name,
            endpoints=(Endpoint(_EXECUTION_SCHEME, source.address()),),
            provider_id=self.descriptor.id,
            source_identity=SourceIdentity("host", source.host),
            resolver_identity_evidence=_remote_coordinate(source.address(), 0),
            accepted_input_methods=_ACCEPTED_INPUT,
        )
        return ResolutionResult(ResourceState.AVAILABLE, (candidate,))

    async def resolve_discovered(self, request: TransferRequest, discovered) -> ResolutionResult:
        """Freeze the discovered member set into the durable resource, at the
        concrete directory the server resolved for a home-relative request."""
        source = self._source(request)
        directory = source.path
        if source.path.startswith("/~/") and discovered.directory.startswith("/"):
            directory = quote(discovered.directory.rstrip("/") + "/", safe="/")
        elif source.path.startswith("/~/"):
            raise self._failure(Category.PROTOCOL_ERROR, domain=Domain.RESOLUTION)
        if source.exact is not None:
            size = next((entry.expected_bytes for entry in discovered.entries if entry.name == unquote(source.exact)),
                        None)
            if size is None:
                raise TransferError(NormalizedError(
                    Domain.RESOLUTION, Category.SOURCE_NOT_FOUND, Stage.RESOLUTION, retryability=Retryability.NEVER,
                    integration_id=self.descriptor.id, confidence=Confidence.HIGH,
                    evidence_basis=EvidenceBasis.STRUCTURED))
            name = safe_name(request.name or unquote(source.exact)) or unquote(source.exact)
            address = source.address(path=directory + source.exact)
            size = max(0, int(size or 0))
            return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
                name=name,
                endpoints=(Endpoint(_EXECUTION_SCHEME, address),),
                expected_bytes=size,
                provider_id=self.descriptor.id,
                source_identity=SourceIdentity("host", source.host),
                resolver_identity_evidence=_remote_coordinate(address, size),
                accepted_input_methods=_ACCEPTED_INPUT,
            ),))
        matcher = _pattern(source.pattern) if source.pattern is not None else None
        members = sorted((entry.name, max(0, int(entry.expected_bytes or 0))) for entry in discovered.entries
                         if matcher is None or matcher.fullmatch(entry.name))
        if not members:
            # An empty directory or a pattern that matches nothing is a missing
            # source, never a literal pattern handed to a writer.
            raise TransferError(NormalizedError(
                Domain.RESOLUTION, Category.SOURCE_NOT_FOUND, Stage.RESOLUTION, retryability=Retryability.NEVER,
                integration_id=self.descriptor.id, confidence=Confidence.HIGH,
                evidence_basis=EvidenceBasis.STRUCTURED))
        resource = ProviderResource(self.descriptor.id, {
            "kind": source.kind, "source": source.address(source.kind, directory),
            "members": [list(item) for item in members],
            "name": unquote(directory.rstrip("/").rsplit("/", 1)[-1]) or source.host,
        }, Ownership.OBSERVED)
        return ResolutionResult(ResourceState.AVAILABLE, observation=self._observation(resource))

    def _observation(self, resource: ProviderResource) -> ProviderObservation:
        members = resource.context["members"]
        return ProviderObservation(resource, ResourceState.AVAILABLE, safe_name(resource.context["name"]),
                                   file_manifest=FileManifest(tuple(
                                       FileManifestEntry(name, name, size) for name, size in members)))

    async def observe(self, resource: ProviderResource) -> ProviderObservation:
        # The member set was frozen at discovery; observing never re-lists.
        return self._observation(resource)

    async def manifest(self, resource: ProviderResource) -> tuple[SourceEntry, ...]:
        context = resource.context
        return tuple(
            SourceEntry(name, size, name, TransferRequest(
                context["kind"], context["source"] + quote(name, safe=""), name=name))
            for name, size in context["members"])
