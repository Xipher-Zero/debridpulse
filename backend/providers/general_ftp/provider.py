"""Resolution-only provider for FTP and SFTP resources.

Source semantics only. Every FTP/SFTP path is classified from authoritative
remote facts through core-run discovery: a trailing ``/`` is evidence of
directory intent, but its absence proves nothing, so no path is assumed to be
a file. What the server proves a regular file becomes one ordinary candidate;
what it proves a directory has its immediate regular files frozen into the
existing neutral file manifest, whose member set is never re-listed on
observation or retry. Children keep their own transport (an FTP member stays
FTP, an SFTP member stays SFTP).

This provider opens no connection, lists nothing, holds no credential and
decides no server identity: discovery is core's, the transport observation is
the executor's, and access input belongs to the universal authentication-input
lifecycle.
"""
from urllib.parse import quote, unquote, urlparse, urlsplit, urlunsplit

from transfers.applicability import ProviderApplicability
from transfers.errors import Category, Confidence, Domain, EvidenceBasis, NormalizedError, Retryability, Stage, TransferError
from transfers.filesystem import safe_name
from transfers.models import (
    Capability, DiscoveryRequest, Endpoint, FileManifest, FileManifestEntry, InputMethod, IntegrationDescriptor,
    Ownership, ProviderObservation, ProviderResource, RemoteObjectKind, ResolutionResult, ResourceState, SourceEntry,
    SourceIdentity, TransferCandidate, TransferRequest,
)
from transfers.requests import direct_link_filename

_ACCEPTED_INPUT = (InputMethod.USERNAME_PASSWORD,)


class GeneralFtpProvider:
    applicability = ProviderApplicability(
        generic_schemes=frozenset({"ftp", "sftp"}),
    )
    descriptor = IntegrationDescriptor(
        "general_ftp", "(S)FTP", frozenset({Capability.RESOLVE, Capability.RESOURCE_LOOKUP, Capability.FILE_MANIFEST}),
        request_types=frozenset({"ftp", "sftp"}),
    )

    def _failure(self, category: Category, *, domain=Domain.REQUEST) -> TransferError:
        return TransferError(NormalizedError(
            domain, category, Stage.RESOLUTION, retryability=Retryability.NEVER,
            integration_id=self.descriptor.id, confidence=Confidence.HIGH,
            evidence_basis=EvidenceBasis.STRUCTURED,
        ))

    def _checked(self, request: TransferRequest) -> tuple[str, str, str]:
        """``(address, scheme, host)`` of one structurally valid FTP/SFTP request."""
        if not isinstance(request, TransferRequest) or request.kind not in self.descriptor.request_types:
            raise self._failure(Category.UNSUPPORTED_REQUEST)
        if not isinstance(request.payload, str):
            raise self._failure(Category.INVALID_REQUEST)

        address = request.payload
        parsed = urlparse(address)
        scheme = parsed.scheme.lower()
        if scheme != request.kind or not parsed.netloc:
            raise self._failure(Category.INVALID_REQUEST)
        try:
            malformed_port = parsed.port == 0  # urlparse raises on a non-numeric or out-of-range port
        except ValueError:
            malformed_port = True
        if malformed_port:
            raise self._failure(Category.INVALID_REQUEST)
        if parsed.username is not None or parsed.password is not None:
            # Core splits credentials out at admission; a provider never sees them.
            raise self._failure(Category.SECURITY_POLICY_REJECTED, domain=Domain.SECURITY)

        host = str(parsed.hostname or "").strip().lower().rstrip(".")
        if not host:
            raise self._failure(Category.INVALID_REQUEST)
        return address, scheme, host

    async def resolve(self, request: TransferRequest) -> ResolutionResult:
        address, scheme, _host = self._checked(request)
        return ResolutionResult(ResourceState.PREPARING, discovery=DiscoveryRequest(
            Endpoint(scheme, address), _ACCEPTED_INPUT))

    async def resolve_discovered(self, request: TransferRequest, discovered) -> ResolutionResult:
        """A proven regular file is one candidate; a proven directory freezes
        its immediate regular files into the durable resource."""
        address, scheme, host = self._checked(request)
        if discovered.kind == RemoteObjectKind.FILE:
            name = safe_name(request.name or direct_link_filename(address)) or direct_link_filename(address)
            return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
                name=name,
                endpoints=(Endpoint(scheme, address),),
                expected_bytes=max(0, int(discovered.expected_bytes or 0)),
                provider_id=self.descriptor.id,
                source_identity=SourceIdentity("host", host),
                accepted_input_methods=_ACCEPTED_INPUT,
            ),))
        members = sorted((entry.name, max(0, int(entry.expected_bytes or 0))) for entry in discovered.entries)
        if not members:
            # An empty directory is a missing source, never an empty download.
            raise TransferError(NormalizedError(
                Domain.RESOLUTION, Category.SOURCE_NOT_FOUND, Stage.RESOLUTION, retryability=Retryability.NEVER,
                integration_id=self.descriptor.id, confidence=Confidence.HIGH,
                evidence_basis=EvidenceBasis.STRUCTURED))
        parts = urlsplit(address)
        path = parts.path if parts.path.endswith("/") else parts.path + "/"
        resource = ProviderResource(self.descriptor.id, {
            "kind": request.kind, "source": urlunsplit((parts.scheme, parts.netloc, path, "", "")),
            "members": [list(item) for item in members],
            "name": unquote(path.rstrip("/").rsplit("/", 1)[-1]) or host,
        }, Ownership.OBSERVED)
        return ResolutionResult(ResourceState.AVAILABLE, observation=self._observation(resource))

    def _observation(self, resource: ProviderResource) -> ProviderObservation:
        return ProviderObservation(resource, ResourceState.AVAILABLE, safe_name(resource.context["name"]),
                                   file_manifest=FileManifest(tuple(
                                       FileManifestEntry(name, name, size)
                                       for name, size in resource.context["members"])))

    async def observe(self, resource: ProviderResource) -> ProviderObservation:
        # The member set was frozen at discovery; observing never re-lists.
        return self._observation(resource)

    async def manifest(self, resource: ProviderResource) -> tuple[SourceEntry, ...]:
        context = resource.context
        return tuple(
            SourceEntry(name, size, name, TransferRequest(
                context["kind"], context["source"] + quote(name, safe=""), name=name))
            for name, size in context["members"])
