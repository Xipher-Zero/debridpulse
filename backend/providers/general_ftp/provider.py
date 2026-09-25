"""Resolution-only provider for FTP and SFTP resources.

Resolution is purely structural: the submitted URL becomes one neutral candidate.
No DNS lookup, connection, credential probe or server-identity inspection happens
here; execution, authentication and host identity belong to the executor and the
universal INPUT_REQUIRED lifecycle.
"""
from urllib.parse import urlparse

from transfers.applicability import ProviderApplicability
from transfers.errors import Category, Confidence, Domain, EvidenceBasis, NormalizedError, Retryability, Stage, TransferError
from transfers.filesystem import safe_name
from transfers.models import (
    Capability, Endpoint, InputMethod, IntegrationDescriptor, ResolutionResult, ResourceState,
    SourceIdentity, TransferCandidate, TransferRequest,
)
from transfers.requests import direct_link_filename


class GeneralFtpProvider:
    applicability = ProviderApplicability(
        generic_schemes=frozenset({"ftp", "sftp"}),
    )
    descriptor = IntegrationDescriptor(
        "general_ftp", "(S)FTP", frozenset({Capability.RESOLVE}),
        request_types=frozenset({"ftp", "sftp"}),
    )

    def _failure(self, category: Category, *, domain=Domain.REQUEST) -> TransferError:
        return TransferError(NormalizedError(
            domain, category, Stage.RESOLUTION, retryability=Retryability.NEVER,
            integration_id=self.descriptor.id, confidence=Confidence.HIGH,
            evidence_basis=EvidenceBasis.STRUCTURED,
        ))

    async def resolve(self, request: TransferRequest) -> ResolutionResult:
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
            raise self._failure(Category.SECURITY_POLICY_REJECTED, domain=Domain.SECURITY)

        host = str(parsed.hostname or "").strip().lower().rstrip(".")
        if not host:
            raise self._failure(Category.INVALID_REQUEST)

        name = safe_name(request.name or direct_link_filename(address))
        if not name:
            name = direct_link_filename(address)
        candidate = TransferCandidate(
            name=name,
            endpoints=(Endpoint(scheme, address),),
            provider_id=self.descriptor.id,
            source_identity=SourceIdentity("host", host),
            accepted_input_methods=(InputMethod.USERNAME_PASSWORD,),
        )
        return ResolutionResult(ResourceState.AVAILABLE, (candidate,))
