"""1.0.13 Usenet/NZB provider: canonical facts only, no executor knowledge.

The provider is deliberately small. It validates and normalizes NZB input into
one canonical COLLECTION candidate and must remain usable by ANY NZB-claiming
executor -- proven here against the non-SAB ``LedgerExecutor``.
"""
from __future__ import annotations

import pytest

from sab_fakes import staged_store

from transfers.errors import TransferError
from transfers.models import (
    Capability, ExecutionSubject, MaterializationKind, ResourceState, TransferRequest,
)
from transfers.registry import IntegrationRegistry

from test_universal_lifecycle import core  # noqa: F401  (shared fixtures)


VALID_NZB = b"""<?xml version="1.0" encoding="iso-8859-1" ?>
<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
 <file poster="p@e.net" date="1700000000" subject="thing [1/1] - &quot;thing.bin&quot; yEnc (1/2)">
  <groups><group>alt.binaries.test</group></groups>
  <segments>
   <segment bytes="1000" number="1">a@e.net</segment>
   <segment bytes="500" number="2">b@e.net</segment>
  </segments>
 </file>
</nzb>
"""


def build_provider():
    from providers.usenet.provider import UsenetProvider
    return UsenetProvider(staged_input=staged_store())


def test_descriptor_declares_resolution_and_the_canonical_nzb_kind():
    provider = build_provider()
    assert provider.descriptor.id == "usenet"
    assert Capability.RESOLVE in provider.descriptor.capabilities
    assert provider.descriptor.request_types == frozenset({"nzb"})


@pytest.mark.asyncio
async def test_valid_nzb_resolves_to_one_collection_candidate():
    provider = build_provider()
    result = await provider.resolve(TransferRequest("nzb", VALID_NZB, name="thing.nzb"))
    assert result.state == ResourceState.AVAILABLE
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.materialization == MaterializationKind.COLLECTION
    assert candidate.provider_id == "usenet"
    # Declared segment bytes are the provider's neutral size fact.
    assert candidate.expected_bytes == 1500


@pytest.mark.asyncio
async def test_malformed_nzb_is_rejected():
    provider = build_provider()
    for payload in (b"", b"not xml at all", b"<nzb></nzb>", b"<nzb><file></file></nzb>"):
        with pytest.raises(TransferError):
            await provider.resolve(TransferRequest("nzb", payload, name="x.nzb"))


@pytest.mark.asyncio
async def test_unsupported_request_kind_is_rejected():
    provider = build_provider()
    with pytest.raises(TransferError):
        await provider.resolve(TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40))


@pytest.mark.asyncio
async def test_provider_output_carries_no_executor_or_native_state():
    provider = build_provider()
    result = await provider.resolve(TransferRequest("nzb", VALID_NZB, name="thing.nzb"))
    candidate = result.candidates[0]
    # No endpoint is invented: an NZB is not an HTTP resource.
    assert candidate.endpoints == ()
    blob = repr(candidate.context) + repr(candidate.source_identity) + repr(candidate.integrity)
    for forbidden in ("sab", "nzo", "aria2", "executor"):
        assert forbidden not in blob.lower()


@pytest.mark.asyncio
async def test_provider_routes_to_a_fake_non_sab_nzb_executor():
    """The provider must be usable by any NZB-claiming executor."""
    from executor_fakes import LedgerExecutor, ledger_capabilities
    provider = build_provider()
    result = await provider.resolve(TransferRequest("nzb", VALID_NZB, name="thing.nzb"))
    candidate = result.candidates[0]

    class NzbCapableExecutor(LedgerExecutor):
        def claim(self, subject):
            from transfers.models import ExecutorClaim
            return ExecutorClaim(subject.request_kind == "nzb")

    async def authorize(handle, action):
        return True

    executor = NzbCapableExecutor(
        authorize, identity="nzb-lab", kinds=("nzb",),
        capabilities=ledger_capabilities(
            materialization_kinds=frozenset({MaterializationKind.COLLECTION})))
    registry = IntegrationRegistry()
    registry.register_provider(provider)
    registry.register_executor(executor)

    from dataclasses import replace
    subject = ExecutionSubject.of(replace(candidate, request_kind="nzb"))
    assert registry.executor_for_subject(subject) is executor
