"""Resolution-only provider for SCP and remote-file SSH sources.

``scp://`` and ``ssh://`` name one remote file reached over SSH. This provider
interprets that input exactly once and returns one ordinary neutral candidate
whose executable endpoint is the equivalent ``sftp://`` address, so the
existing SFTP-capable executor claims execution and the existing evidence,
host-identity and credential owners apply unchanged. Resolution is purely
structural: no DNS lookup, connection, listing or credential work happens here.

Only an exact absolute file path is a source. A directory, a pattern, a
home-relative path or a query names something that cannot be resolved without
remote discovery or shell semantics, so it is refused rather than guessed at;
``ssh://`` never means anything but retrieval of that one file.
"""
from urllib.parse import urlsplit, urlunsplit

from transfers.applicability import ProviderApplicability
from transfers.errors import Category, Confidence, Domain, EvidenceBasis, NormalizedError, Retryability, Stage, TransferError
from transfers.filesystem import safe_name
from transfers.models import (
    Capability, Endpoint, InputMethod, IntegrationDescriptor, ResolutionResult, ResourceState,
    SourceIdentity, TransferCandidate, TransferRequest,
)
from transfers.requests import direct_link_filename

# The one executable transport an SCP/SSH source is expressed in.
_EXECUTION_SCHEME = "sftp"
# Path characters that make a request a pattern rather than one named file.
_PATTERN_CHARACTERS = frozenset("*[]")


class ScpProvider:
    applicability = ProviderApplicability(
        generic_schemes=frozenset({"scp", "ssh"}),
    )
    descriptor = IntegrationDescriptor(
        "general_scp", "SCP", frozenset({Capability.RESOLVE}),
        request_types=frozenset({"scp", "ssh"}),
    )

    def _failure(self, category: Category, *, domain=Domain.REQUEST) -> TransferError:
        return TransferError(NormalizedError(
            domain, category, Stage.RESOLUTION, retryability=Retryability.NEVER,
            integration_id=self.descriptor.id, confidence=Confidence.HIGH,
            evidence_basis=EvidenceBasis.STRUCTURED,
        ))

    def _execution_address(self, request: TransferRequest) -> tuple[str, str]:
        """The one canonical interpretation: ``(sftp address, host)``.

        The authority comes from the ordinary URI parser, which already tells
        ``host:2222/path`` (explicit port) from the SCP-style ``host:/path``
        (default port, absolute path) and parses bracketed IPv6. The path is
        carried exactly as submitted -- percent-encoding included -- so the
        executor decodes it once, as it does for every SFTP address.
        """
        address = request.payload
        if any(ord(char) <= 32 or ord(char) == 127 for char in address):
            raise self._failure(Category.INVALID_REQUEST)
        parsed = urlsplit(address)
        if parsed.scheme.lower() != request.kind or not parsed.netloc:
            raise self._failure(Category.INVALID_REQUEST)
        try:
            port = parsed.port  # raises on a non-numeric or out-of-range port
        except ValueError:
            raise self._failure(Category.INVALID_REQUEST) from None
        if port == 0:
            raise self._failure(Category.INVALID_REQUEST)
        if parsed.username is not None or parsed.password is not None:
            raise self._failure(Category.SECURITY_POLICY_REJECTED, domain=Domain.SECURITY)
        hostname = str(parsed.hostname or "")
        if not hostname.strip("."):
            raise self._failure(Category.INVALID_REQUEST)

        path = parsed.path
        if not path.startswith("/") or path == "/":
            raise self._failure(Category.INVALID_REQUEST)
        if ("?" in address or "#" in address or path.endswith("/") or path.startswith("/~")
                or _PATTERN_CHARACTERS.intersection(path)):
            raise self._failure(Category.UNSUPPORTED_REQUEST)

        authority = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None:
            authority = f"{authority}:{port}"
        return urlunsplit((_EXECUTION_SCHEME, authority, path, "", "")), hostname.strip().lower().rstrip(".")

    async def resolve(self, request: TransferRequest) -> ResolutionResult:
        if not isinstance(request, TransferRequest) or request.kind not in self.descriptor.request_types:
            raise self._failure(Category.UNSUPPORTED_REQUEST)
        if not isinstance(request.payload, str):
            raise self._failure(Category.INVALID_REQUEST)
        address, host = self._execution_address(request)

        name = safe_name(request.name or direct_link_filename(request.payload))
        if not name:
            name = direct_link_filename(request.payload)
        candidate = TransferCandidate(
            name=name,
            endpoints=(Endpoint(_EXECUTION_SCHEME, address),),
            provider_id=self.descriptor.id,
            source_identity=SourceIdentity("host", host),
            accepted_input_methods=(InputMethod.USERNAME_PASSWORD,),
        )
        return ResolutionResult(ResourceState.AVAILABLE, (candidate,))
