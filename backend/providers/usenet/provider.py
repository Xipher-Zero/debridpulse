"""Resolution-only provider for Usenet (NZB) sources.

Deliberately small. It validates and normalizes NZB input into one canonical
COLLECTION candidate carrying only neutral, non-secret facts. It never selects
or calls an executor, never learns a native job identity, and owns no retry,
recovery, execution or extraction. Any NZB-claiming executor can run its
output.
"""
from __future__ import annotations

from providers.usenet.nzb import InvalidNzb, parse, read
from transfers.applicability import ProviderApplicability
from transfers.errors import (
    Category, Confidence, Domain, EvidenceBasis, NormalizedError, Retryability, Stage, TransferError,
)
from transfers.filesystem import safe_name
from transfers.models import (
    Capability, IntegrationDescriptor, MaterializationKind, ResolutionResult, ResourceState,
    SourceIdentity, TransferCandidate, TransferRequest,
)
from transfers.staged_input import StagedInputError, StagedPayload

# The canonical request class this provider resolves.
REQUEST_KIND = "nzb"
# Where the candidate carries its input. NOT the manifest itself: a durable,
# non-secret reference to it, held by the neutral staged-input owner, so any
# executor can obtain the exact original bytes after a restart without the
# payload ever entering candidate JSON. A real manifest is routinely tens or
# hundreds of megabytes; base64 in a context field cost ~358 MB of text for a
# 256 MiB posting, persisted on every candidate.
CONTEXT_STAGED_INPUT = "staged_input"
CONTEXT_DECLARED_BYTES = "nzb_declared_bytes"


class UsenetProvider:
    applicability = ProviderApplicability()
    descriptor = IntegrationDescriptor(
        "usenet", "Usenet", frozenset({Capability.RESOLVE}),
        request_types=frozenset({REQUEST_KIND}),
    )

    def __init__(self, staged_input=None):
        # The neutral durable-input owner. Injected, never constructed here:
        # the provider borrows it to READ a submitted payload and owns none of
        # its lifecycle.
        self.staged_input = staged_input

    def _failure(self, category: Category, *, domain=Domain.REQUEST) -> TransferError:
        return TransferError(NormalizedError(
            domain, category, Stage.RESOLUTION, retryability=Retryability.NEVER,
            integration_id=self.descriptor.id, confidence=Confidence.HIGH,
            evidence_basis=EvidenceBasis.STRUCTURED,
        ))

    async def resolve(self, request: TransferRequest) -> ResolutionResult:
        if not isinstance(request, TransferRequest) or request.kind not in self.descriptor.request_types:
            raise self._failure(Category.UNSUPPORTED_REQUEST)
        staged, manifest = await self._manifest(request)

        name = safe_name(manifest.name) or safe_name(request.name) or "usenet-download"
        candidate = TransferCandidate(
            name=name,
            # An NZB is not an addressable endpoint: none is invented.
            endpoints=(),
            expected_bytes=manifest.declared_bytes,
            provider_id=self.descriptor.id,
            materialization=MaterializationKind.COLLECTION,
            context={
                CONTEXT_STAGED_INPUT: staged.as_context(),
                CONTEXT_DECLARED_BYTES: manifest.declared_bytes,
            },
            # Usenet is one logical posted resource, not a host-addressed one.
            source_identity=SourceIdentity("usenet", name.casefold()),
        )
        return ResolutionResult(ResourceState.AVAILABLE, (candidate,))

    async def _manifest(self, request: TransferRequest):
        """The staged input for this request, and the facts it asserts.

        A request already carrying a durable reference is read by streaming. An
        inline payload -- a small or programmatic submission -- is staged first,
        so exactly one representation reaches the candidate either way and the
        executor has one way to obtain the original bytes.
        """
        payload = request.payload
        if isinstance(payload, StagedPayload):
            if self.staged_input is None:
                raise self._failure(Category.INVALID_REQUEST)
            try:
                with self.staged_input.opened(payload) as stream:
                    return payload, read(stream, fallback_name=request.name)
            except StagedInputError as exc:
                raise self._failure(Category.INVALID_REQUEST) from exc
            except InvalidNzb as exc:
                raise self._failure(Category.INVALID_REQUEST) from exc

        if isinstance(payload, str):
            payload = payload.encode("utf-8", "strict")
        if not isinstance(payload, (bytes, bytearray)):
            raise self._failure(Category.INVALID_REQUEST)
        try:
            manifest = parse(bytes(payload), fallback_name=request.name)
        except InvalidNzb as exc:
            raise self._failure(Category.INVALID_REQUEST) from exc
        if self.staged_input is None:
            raise self._failure(Category.INVALID_REQUEST)
        async def one_chunk():
            yield bytes(payload)
        try:
            return await self.staged_input.stage(one_chunk()), manifest
        except StagedInputError as exc:
            raise self._failure(Category.INVALID_REQUEST) from exc
