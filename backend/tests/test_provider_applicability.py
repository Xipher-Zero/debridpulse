from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import db.database as database
from integrations.runtime_state import ProviderRuntimeStateStore
from providers.general_http.provider import GeneralHttpProvider
from transfers.applicability import (
    ApplicabilityClass,
    HostClaim,
    HostClaimScope,
    ProviderApplicability,
    ProviderApplicabilityInput,
    classify_provider_applicability,
    parse_url_applicability,
)
from transfers.models import Capability, IntegrationDescriptor, ResolutionResult, ResourceState, TransferRequest
from transfers.registry import IntegrationRegistry
from fake_applicability_provider import RuntimeClaimProvider, SpecializedFixtureProvider


class GenericFixtureProvider:
    def __init__(self, identity="generic-fixture", *, priority=0, enabled=True, kinds=("http", "https")):
        self.descriptor = IntegrationDescriptor(
            identity,
            identity,
            frozenset({Capability.RESOLVE}),
            request_types=frozenset(kinds),
            enabled=enabled,
            priority=priority,
        )
        self.applicability = ProviderApplicability(
            generic_schemes=frozenset(kind for kind in kinds if kind in {"http", "https"}),
        )

    async def resolve(self, request):
        return ResolutionResult(ResourceState.AVAILABLE)


class StaticFixtureProvider:
    def __init__(self, identity="static-fixture", *, kinds=("magnet", "torrent"), priority=0):
        self.descriptor = IntegrationDescriptor(
            identity,
            identity,
            frozenset({Capability.RESOLVE}),
            request_types=frozenset(kinds),
            priority=priority,
        )

    async def resolve(self, request):
        return ResolutionResult(ResourceState.AVAILABLE)


def _input(identity, *, generic=(), claims=(), enabled=True, request_types=("http", "https")):
    return ProviderApplicabilityInput(
        identity,
        frozenset(request_types),
        enabled,
        ProviderApplicability(
            generic_schemes=frozenset(generic),
            specialized_hosts=tuple(claims),
        ),
    )


def _classes(request, *providers):
    return [
        (item.provider_id, item.classification)
        for item in classify_provider_applicability(request, providers)
    ]


def test_url_view_normalizes_only_routing_components_and_preserves_endpoint():
    address = "HTTPS://User:Secret@Example.Test.:8443/Case/Sensitive%2FPath?sig=A%2FB&b=2#frag"
    request = TransferRequest("https", address)
    view = parse_url_applicability(request)
    assert view.scheme == "https"
    assert view.hostname == "example.test"
    assert view.port == 8443
    assert not view.is_ip
    assert request.payload == address


def test_exact_host_and_domain_scope_are_distinct_and_boundary_safe():
    exact = _input("exact", claims=(HostClaim("Example.Test", HostClaimScope.EXACT, frozenset({"https"})),))
    domain = _input("domain", claims=(HostClaim("example.test", HostClaimScope.DOMAIN, frozenset({"https"})),))

    assert _classes(TransferRequest("https", "https://EXAMPLE.TEST/x"), exact) == [
        ("exact", ApplicabilityClass.SPECIALIZED)
    ]
    assert not _classes(TransferRequest("https", "https://sub.example.test/x"), exact)
    assert _classes(TransferRequest("https", "https://a.b.example.test/x"), domain) == [
        ("domain", ApplicabilityClass.SPECIALIZED)
    ]
    for hostile in ("evil-example.test", "example.test.evil.test", "notexample.test"):
        assert not _classes(TransferRequest("https", f"https://{hostile}/x"), domain)


def test_specialized_suppresses_generic_but_same_class_keeps_all_matches():
    request = TransferRequest("https", "https://files.example.test/file")
    generic_a = _input("generic-a", generic=("https",))
    generic_b = _input("generic-b", generic=("https",))
    specialized_a = _input(
        "special-a",
        claims=(HostClaim("example.test", HostClaimScope.DOMAIN, frozenset({"https"})),),
    )
    specialized_b = _input(
        "special-b",
        claims=(HostClaim("files.example.test", HostClaimScope.EXACT, frozenset({"https"})),),
    )

    assert _classes(request, generic_a, generic_b) == [
        ("generic-a", ApplicabilityClass.GENERIC),
        ("generic-b", ApplicabilityClass.GENERIC),
    ]
    assert _classes(request, generic_a, specialized_a, generic_b, specialized_b) == [
        ("special-a", ApplicabilityClass.SPECIALIZED),
        ("special-b", ApplicabilityClass.SPECIALIZED),
    ]


def test_unrelated_host_leaves_generic_provider_eligible():
    request = TransferRequest("https", "https://unrelated.test/file")
    generic = _input("generic", generic=("https",))
    specialized = _input(
        "special",
        claims=(HostClaim("example.test", HostClaimScope.DOMAIN, frozenset({"https"})),),
    )
    assert _classes(request, specialized, generic) == [
        ("generic", ApplicabilityClass.GENERIC)
    ]


def test_disabled_providers_do_not_contribute_applicability():
    request = TransferRequest("https", "https://files.example.test/file")
    specialized = _input(
        "special",
        claims=(HostClaim("example.test", HostClaimScope.DOMAIN, frozenset({"https"})),),
        enabled=False,
    )
    generic = _input("generic", generic=("https",))
    assert _classes(request, specialized, generic) == [
        ("generic", ApplicabilityClass.GENERIC)
    ]


def test_port_userinfo_case_and_trailing_dot_do_not_change_host_matching():
    specialized = _input(
        "special",
        claims=(HostClaim("example.test", HostClaimScope.DOMAIN, frozenset({"https"})),),
    )
    request = TransferRequest("https", "https://name:secret@SUB.Example.Test.:9443/file")
    assert _classes(request, specialized) == [
        ("special", ApplicabilityClass.SPECIALIZED)
    ]
    view = parse_url_applicability(request)
    assert view.hostname == "sub.example.test"
    assert view.port == 9443
    assert "secret" not in repr(request)


def test_ipv4_and_ipv6_never_use_domain_suffix_semantics():
    ip_domain = _input(
        "ip-domain",
        claims=(HostClaim("192.0.2.1", HostClaimScope.DOMAIN, frozenset({"https"})),),
    )
    ip_exact = _input(
        "ip-exact",
        claims=(HostClaim("192.0.2.1", HostClaimScope.EXACT, frozenset({"https"})),),
    )
    assert not _classes(TransferRequest("https", "https://192.0.2.1/file"), ip_domain)
    assert _classes(TransferRequest("https", "https://192.0.2.1/file"), ip_exact)

    ipv6 = _input(
        "ipv6",
        claims=(HostClaim("2001:db8::1", HostClaimScope.EXACT, frozenset({"https"})),),
    )
    view = parse_url_applicability(TransferRequest("https", "https://[2001:0db8::1]:8443/file"))
    assert view.hostname == "2001:db8::1"
    assert view.is_ip
    assert _classes(TransferRequest("https", "https://[2001:db8::1]/file"), ipv6)


def test_idna_unicode_and_punycode_are_canonicalized_to_same_hostname():
    unicode_request = TransferRequest("https", "https://bücher.example/file")
    ascii_request = TransferRequest("https", "https://xn--bcher-kva.example/file")
    assert parse_url_applicability(unicode_request).hostname == "xn--bcher-kva.example"
    assert parse_url_applicability(ascii_request).hostname == "xn--bcher-kva.example"

    claim = _input(
        "idna",
        claims=(HostClaim("bücher.example", HostClaimScope.EXACT, frozenset({"https"})),),
    )
    assert _classes(ascii_request, claim) == [
        ("idna", ApplicabilityClass.SPECIALIZED)
    ]


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("https", "https:///missing-host"),
        ("https", "https://[2001:db8::1/file"),
        ("https", "https://example.test:notaport/file"),
        ("https", "ftp://example.test/file"),
        ("https", ""),
        ("https", "not-a-url"),
    ],
)
def test_malformed_url_inputs_are_bounded_and_do_not_route(kind, payload):
    generic = _input("generic", generic=("https",))
    request = TransferRequest(kind, payload)
    assert parse_url_applicability(request) is None
    assert classify_provider_applicability(request, (generic,)) == ()


def test_non_url_request_sent_to_parser_is_not_misclassified():
    request = TransferRequest("parcel", "opaque-payload")
    static = _input("parcel", request_types=("parcel",))
    assert parse_url_applicability(request) is None
    assert _classes(request, static) == [
        ("parcel", ApplicabilityClass.STATIC)
    ]


@pytest.mark.parametrize(
    "transfer_request",
    [
        TransferRequest("magnet", "magnet:?xt=urn:btih:" + "a" * 40),
        TransferRequest("torrent", b"d4:infod4:name1:xee"),
    ],
)
def test_magnet_and_torrent_remain_static_capability_routing(transfer_request):
    provider = ProviderApplicabilityInput(
        "static",
        frozenset({"magnet", "torrent"}),
        True,
        ProviderApplicability(
            generic_schemes=frozenset({"https"}),
            specialized_hosts=(HostClaim("example.test", HostClaimScope.DOMAIN),),
        ),
    )
    assert _classes(transfer_request, provider) == [
        ("static", ApplicabilityClass.STATIC)
    ]


def test_provider_identity_strings_do_not_affect_classification():
    request = TransferRequest("https", "https://node.example.test/file")
    for identity in ("alpha", "zzz", "provider-7", "randomized-name"):
        specialized = _input(
            identity,
            claims=(HostClaim("example.test", HostClaimScope.DOMAIN, frozenset({"https"})),),
        )
        generic = _input("other-" + identity, generic=("https",))
        result = classify_provider_applicability(request, (generic, specialized))
        assert len(result) == 1
        assert result[0].provider_id == identity
        assert result[0].classification == ApplicabilityClass.SPECIALIZED


def test_registry_preserves_neutral_preference_priority_and_specialized_precedence():
    registry = IntegrationRegistry()
    generic = GenericFixtureProvider("generic", priority=100)
    low = SpecializedFixtureProvider("special-low", priority=1)
    high = SpecializedFixtureProvider("special-high", priority=5)
    for provider in (generic, low, high):
        registry.register_provider(provider)

    request = TransferRequest("https", "https://files.example.test/file")
    assert registry.eligible_providers(request) == (high, low)
    preferred = replace(request, preferred_provider="special-low")
    assert registry.eligible_providers(preferred) == (low, high)


def test_existing_health_filter_precedes_applicability_competition():
    registry = IntegrationRegistry()
    specialized = SpecializedFixtureProvider("special")
    generic = GenericFixtureProvider("generic")
    registry.register_provider(specialized)
    registry.register_provider(generic)
    request = TransferRequest("https", "https://files.example.test/file")

    assert registry.eligible_providers(request) == (specialized,)
    registry.mark_health("special", healthy=False)
    assert registry.eligible_providers(request) == (generic,)


def test_real_general_http_is_generic_and_is_suppressed_by_neutral_specialized_provider():
    registry = IntegrationRegistry()
    http = GeneralHttpProvider()
    specialized = SpecializedFixtureProvider("routing-fixture")
    registry.register_provider(http)
    registry.register_provider(specialized)

    generic_request = TransferRequest("https", "https://ordinary.test/file")
    assert registry.eligible_providers(generic_request) == (http,)

    specialized_request = TransferRequest("https", "https://files.example.test/file")
    assert registry.eligible_providers(specialized_request) == (specialized,)

    specialized.descriptor = replace(specialized.descriptor, enabled=False)
    assert registry.eligible_providers(specialized_request) == (http,)


@pytest.mark.asyncio
async def test_runtime_state_is_interpreted_by_provider_before_classifier_consumes_claims(tmp_path, monkeypatch):
    db_path = tmp_path / "applicability-runtime.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    store = ProviderRuntimeStateStore()
    provider = RuntimeClaimProvider(store)
    generic = GeneralHttpProvider()
    registry = IntegrationRegistry()
    registry.register_provider(provider)
    registry.register_provider(generic)
    request = TransferRequest("https", "https://cdn.example.test/file")

    await provider.retain(("example.test",), observed_at=100.0, stale_after=200.0)
    raw = await store.load(provider.descriptor.id, provider.state_key)
    assert b"example.test" in raw.payload
    assert provider.applicability.specialized_hosts == ()

    await provider.refresh_applicability(now=150.0)
    assert registry.eligible_providers(request) == (provider,)

    provider.set_enabled(False)
    assert registry.eligible_providers(request) == (generic,)
    assert await store.load(provider.descriptor.id, provider.state_key) == raw

    provider.set_enabled(True)
    await provider.refresh_applicability(now=250.0)
    assert provider.applicability.specialized_hosts == ()
    assert registry.eligible_providers(request) == (generic,)


def test_classifier_source_has_no_provider_or_runtime_state_knowledge():
    backend = Path(__file__).resolve().parents[1]
    source = (backend / "transfers" / "applicability.py").read_text(encoding="utf-8").casefold()
    assert "providers." not in source
    assert "alldebrid" not in source
    assert "real-debrid" not in source
    assert "realdebrid" not in source
    assert "torbox" not in source
    assert "runtime_state" not in source
    assert "aiohttp" not in source
    assert "httpx" not in source
    assert "urlopen" not in source
    assert "requests." not in source


def test_no_network_calls_are_needed_for_classification(monkeypatch):
    generic = _input("generic", generic=("https",))
    specialized = _input(
        "special",
        claims=(HostClaim("example.test", HostClaimScope.DOMAIN, frozenset({"https"})),),
    )
    request = TransferRequest("https", "https://files.example.test/file")
    assert _classes(request, generic, specialized) == [
        ("special", ApplicabilityClass.SPECIALIZED)
    ]
