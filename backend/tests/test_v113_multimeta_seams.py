"""DP 1.0.13 Multimeta: the three neutral seams it feeds, proven without it.

Each extension lives in an existing canonical owner and means something to
any provider:

* routing -- ``ProviderApplicability.specific``: a claim recognized from the
  request itself (one resource format) competes before a scheme-wide claim of
  its class, after a conditional one;
* the manifest -- ``SourceEntry.alternates``/``integrity``: a member's
  further ordinary requests become sibling children, its declared integrity
  rides with it, and an entry without either is exactly what it always was;
* discovery -- ``DiscoveryRequest.content_limit``: the complete content of one
  small file, read by the executor that claims it, or refused.
"""
from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

import pytest

from test_v113_rsync_executor import _candidate as rsync_candidate, _executor as rsync_executor
from test_v113_transport_evidence_sampling import candidate, executor_for, guard_for
from transfers import codec
from transfers._repository_base import manifest_child_identity, manifest_member_requests
from transfers.applicability import ProviderApplicability
from transfers.errors import Category, TransferError
from transfers.models import (
    Capability, DiscoveryRequest, Endpoint, ExecutionSubject, IntegrationDescriptor, IntegrityMetadata,
    SourceEntry, TransferRequest,
)
from transfers.registry import IntegrationRegistry


class _Source:
    def __init__(self, identity, facts):
        self.descriptor = IntegrationDescriptor(identity, identity, frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"vault"}))
        self.facts = facts

    def applicability_for(self, request):
        return self.facts(request.payload)

    async def resolve(self, request):  # pragma: no cover -- ordering only
        raise AssertionError("never resolved here")


def _ids(registry, address):
    return [item.descriptor.id for item in registry.eligible_providers(TransferRequest("vault", address))]


def test_a_specific_claim_competes_before_a_scheme_wide_one_and_after_a_conditional_one():
    registry = IntegrationRegistry()
    vault = frozenset({"vault"})
    # Identities are chosen so that identity order alone would put the
    # scheme-wide claim first: only the applicability facts can explain order.
    registry.register_provider(_Source("a-scheme-wide", lambda _payload: ProviderApplicability(generic_schemes=vault)))
    registry.register_provider(_Source("z-format", lambda payload: ProviderApplicability(
        generic_schemes=vault, specific=True) if payload.endswith(".fmt") else ProviderApplicability()))
    registry.register_provider(_Source("y-probe", lambda payload: ProviderApplicability(
        generic_schemes=vault, conditional=True) if payload.endswith("/") else ProviderApplicability()))
    assert _ids(registry, "vault://host/doc.fmt") == ["z-format", "a-scheme-wide"]
    assert _ids(registry, "vault://host/dir/") == ["y-probe", "a-scheme-wide"]
    assert _ids(registry, "vault://host/file.bin") == ["a-scheme-wide"]


def test_production_routing_gives_a_descriptor_address_to_multimeta_and_nothing_else():
    from providers.general_http.provider import GeneralHttpProvider
    from providers.general_webdav.provider import GeneralWebdavProvider
    from providers.multimeta.provider import MultimetaProvider
    registry = IntegrationRegistry()
    for provider in (GeneralHttpProvider(), GeneralWebdavProvider(), MultimetaProvider()):
        registry.register_provider(provider)

    def first(kind, payload):
        return registry.provider_for(TransferRequest(kind, payload)).descriptor.id

    assert first("https", "https://host.example/pub/Release.META4?sig=1") == "multimeta"
    assert first("http", "http://host.example/release.meta4") == "multimeta"
    # Every other HTTP(S) address keeps its existing owner, unprobed.
    assert first("https", "https://host.example/release.iso") == "general_http"
    assert first("https", "https://host.example/release.meta4.iso") == "general_http"
    assert first("https", "https://host.example/folder/") == "general_webdav"
    assert first("meta4", b"<metalink/>") == "multimeta"


def test_a_member_without_alternates_keeps_its_one_child_identity():
    assert manifest_child_identity("parent", "disc/a.bin") == uuid5(NAMESPACE_URL, "request:parent:disc/a.bin").hex
    identities = {manifest_child_identity("parent", "disc/a.bin", alternate) for alternate in range(4)}
    assert len(identities) == 4
    entry = SourceEntry("a.bin", 4, "disc/a.bin", TransferRequest("https", "https://one.example/a.bin"),
                        alternates=(TransferRequest("ftp", "ftp://two.example/a.bin"),),
                        integrity=(IntegrityMetadata("sha256", "c0" * 32),))
    assert [(index, request.payload) for index, request in manifest_member_requests(entry)] == [
        (0, "https://one.example/a.bin"), (1, "ftp://two.example/a.bin")]
    assert codec.entry(codec.load(codec.dump(entry))) == entry
    # A child row persisted before the fields existed decodes unchanged.
    legacy = {"name": "a.bin", "expected_bytes": 4, "relative_path": "disc/a.bin",
              "request": codec.load(codec.dump(entry.request))}
    assert codec.entry(legacy) == SourceEntry("a.bin", 4, "disc/a.bin", entry.request)


def test_a_content_read_is_bounded_and_positive():
    with pytest.raises(ValueError):
        DiscoveryRequest(Endpoint("https", "https://host.example/x"), content_limit=0)
    assert DiscoveryRequest(Endpoint("https", "https://host.example/x")).content_limit is None


@pytest.mark.asyncio
async def test_an_executor_reads_content_only_where_it_can(tmp_path):
    aria2 = executor_for(tmp_path, guard_for())
    for address in ("ftp://host.example/x.meta4", "sftp://host.example/x.meta4"):
        with pytest.raises(TransferError) as raised:
            await aria2.discover(ExecutionSubject.of(candidate(address)), content_limit=1024)
        assert raised.value.error.category == Category.UNSUPPORTED_CAPABILITY
    (tmp_path / "rsync").mkdir()
    rsync = rsync_executor(tmp_path / "rsync", guard_for())
    with pytest.raises(TransferError) as raised:
        await rsync.discover(ExecutionSubject.of(rsync_candidate("rsync://host.example/m/x.meta4")), content_limit=1024)
    assert raised.value.error.category == Category.UNSUPPORTED_CAPABILITY
