"""Resolution-only provider for ordinary HTTP and HTTPS resources."""
from urllib.parse import urlparse

from transfers.applicability import ProviderApplicability
from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage, TransferError
from transfers.filesystem import safe_name
from transfers.models import (
    Capability, Endpoint, IntegrationDescriptor, ResolutionResult, ResourceState,
    SourceIdentity, TransferCandidate, TransferRequest,
)
from transfers.requests import direct_link_filename


class GeneralHttpProvider:
    applicability = ProviderApplicability(
        generic_schemes=frozenset({"http", "https"}),
    )
    descriptor = IntegrationDescriptor(
        "general_http", "HTTP & HTTPS", frozenset({Capability.RESOLVE}),
        request_types=frozenset({"http", "https"}),
    )

    def _failure(self, category: Category, *, domain=Domain.REQUEST) -> TransferError:
        return TransferError(NormalizedError(
            domain, category, Stage.RESOLUTION, retryability=Retryability.NEVER,
            recovery=Recovery.FAIL, integration_id=self.descriptor.id,
        ))

    async def resolve(self, request: TransferRequest) -> ResolutionResult:
        if not isinstance(request, TransferRequest) or request.kind not in self.descriptor.request_types:
            raise self._failure(Category.UNSUPPORTED_REQUEST)
        if not isinstance(request.payload, str):
            raise self._failure(Category.INVALID_REQUEST)

        address = request.payload
        parsed = urlparse(address)
        scheme = parsed.scheme.lower()
        if scheme != request.kind or scheme not in self.descriptor.request_types or not parsed.netloc:
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
            context={"accepted_input_methods": ("username_password",)},
            source_identity=SourceIdentity("host", host),
        )
        return ResolutionResult(ResourceState.AVAILABLE, (candidate,))
