"""Resolution-only provider for Usenet (NZB) sources.

Deliberately small. It validates and normalizes NZB input into one canonical
COLLECTION candidate carrying only neutral, non-secret facts. It never selects
or calls an executor, never learns a native job identity, and owns no retry,
recovery, execution or extraction. Any NZB-claiming executor can run its
output.
"""
from __future__ import annotations

import base64

from providers.usenet.nzb import InvalidNzb, parse
from transfers.applicability import ProviderApplicability
from transfers.errors import (
    Category, Confidence, Domain, EvidenceBasis, NormalizedError, Retryability, Stage, TransferError,
)
from transfers.filesystem import safe_name
from transfers.models import (
    Capability, IntegrationDescriptor, MaterializationKind, ResolutionResult, ResourceState,
    SourceIdentity, TransferCandidate, TransferRequest,
)

# The canonical request class this provider resolves.
REQUEST_KIND = "nzb"
# The neutral, non-secret representation of the posted manifest carried on the
# candidate so any executor can submit it after a restart. Base64 ASCII keeps
# it a plain JSON string through the existing canonical persistence codec.
CONTEXT_MANIFEST = "nzb_base64"
CONTEXT_DECLARED_BYTES = "nzb_declared_bytes"


class UsenetProvider:
    applicability = ProviderApplicability()
    descriptor = IntegrationDescriptor(
        "usenet", "Usenet", frozenset({Capability.RESOLVE}),
        request_types=frozenset({REQUEST_KIND}),
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
        payload = request.payload
        if isinstance(payload, str):
            payload = payload.encode("utf-8", "strict")
        if not isinstance(payload, (bytes, bytearray)):
            raise self._failure(Category.INVALID_REQUEST)

        try:
            manifest = parse(bytes(payload), fallback_name=request.name)
        except InvalidNzb as exc:
            raise self._failure(Category.INVALID_REQUEST) from exc

        name = safe_name(manifest.name) or safe_name(request.name) or "usenet-download"
        candidate = TransferCandidate(
            name=name,
            # An NZB is not an addressable endpoint: none is invented.
            endpoints=(),
            expected_bytes=manifest.declared_bytes,
            provider_id=self.descriptor.id,
            materialization=MaterializationKind.COLLECTION,
            context={
                CONTEXT_MANIFEST: base64.b64encode(bytes(payload)).decode("ascii"),
                CONTEXT_DECLARED_BYTES: manifest.declared_bytes,
            },
            # Usenet is one logical posted resource, not a host-addressed one.
            source_identity=SourceIdentity("usenet", name.casefold()),
        )
        return ResolutionResult(ResourceState.AVAILABLE, (candidate,))
