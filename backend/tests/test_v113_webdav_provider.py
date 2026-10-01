"""DP 1.0.13 WebDAV provider: request forms, applicability and source semantics.

``general_webdav`` answers only "what does this WebDAV resource mean, what
files does it contain, and what ordinary resources satisfy it". Routing,
authentication, selection, execution and presentation stay with the
existing owners; these tests pin exactly that boundary.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from transfers.errors import Category, TransferError
from transfers.models import (
    Capability, DiscoveredEntry, DiscoveryDepth, DiscoveryResult, InputMethod, RemoteObjectKind, ResourceState,
    SourceIdentity, TransferRequest,
)
from transfers.registry import IntegrationRegistry
from transfers.requests import auth_scope, direct_link_host, normalize_direct_links

ROOT = Path(__file__).resolve().parents[2]


def _provider(depth=DiscoveryDepth.CURRENT):
    from providers.general_webdav.provider import GeneralWebdavProvider
    return GeneralWebdavProvider(depth)


def _request(url, **kwargs):
    return TransferRequest(url.split(":", 1)[0], url, **kwargs)


def _registry():
    from providers.general_http.provider import GeneralHttpProvider
    registry = IntegrationRegistry()
    registry.register_provider(GeneralHttpProvider())
    registry.register_provider(_provider())
    return registry


def _discovered(kind="directory", entries=(), size=0, location=""):
    return DiscoveryResult(tuple(DiscoveredEntry(path.rsplit("/", 1)[-1], bytes_, path if "/" in path else "")
                                 for path, bytes_ in entries), kind=RemoteObjectKind(kind),
                           expected_bytes=size, location=location)


# ── identity and capabilities ─────────────────────────────────────────────────

def test_the_provider_declares_only_what_it_implements():
    from transfers.contracts import DiscoveryResolution, Manifest, RequestApplicabilitySource, ResourceLookup
    provider = _provider()
    assert provider.descriptor.id == "general_webdav" and provider.descriptor.name == "WebDAV"
    assert provider.descriptor.capabilities == {Capability.RESOLVE, Capability.RESOURCE_LOOKUP,
                                                Capability.FILE_MANIFEST}
    assert isinstance(provider, (DiscoveryResolution)) and isinstance(provider, ResourceLookup)
    assert isinstance(provider, Manifest) and isinstance(provider, RequestApplicabilitySource)
    IntegrationRegistry().register_provider(provider)  # every declared capability is implemented


def test_the_integration_is_a_network_source_with_a_current_directory_default():
    from integrations.catalog import definitions
    from providers.general_webdav.definition import GeneralWebdavOptions, build
    definition = next(item for item in definitions if item.id == "general_webdav")
    assert definition.kind == "provider" and definition.name == "WebDAV"
    presentation = definition.presentation
    assert (presentation.status_group, presentation.status_name, presentation.static_status) == (
        "direct_sources", "WebDAV", "healthy")
    assert GeneralWebdavOptions().directory_depth == "current"
    built = {value: build(GeneralWebdavOptions(directory_depth=value), None).depth
             for value in ("current", "1", "2", "3", "all")}
    assert built == {"current": DiscoveryDepth.CURRENT, "1": DiscoveryDepth.of(1), "2": DiscoveryDepth.of(2),
                     "3": DiscoveryDepth.of(3), "all": DiscoveryDepth.UNLIMITED}
    with pytest.raises(ValueError):
        GeneralWebdavOptions(directory_depth="infinity")


# ── applicability: aliases are WebDAV's alone; plain URLs only with a slash ───

@pytest.mark.parametrize("url", ["webdav://h.example/dav/", "webdavs://h.example/dav/file.bin",
                                 "dav://h.example:8080/x", "davs://h.example/"])
def test_an_explicit_alias_is_claimed_by_webdav_alone_and_unconditionally(url):
    registry = _registry()
    request = _request(url)
    assert [item.descriptor.id for item in registry.eligible_providers(request)] == ["general_webdav"]
    assert registry.conditional_claim(registry.providers["general_webdav"], request) is False


@pytest.mark.parametrize("url", ["https://h.example/dav/", "http://h.example/", "https://h.example/a/b/"])
def test_a_slash_terminated_plain_url_gives_webdav_its_probe_first(url):
    registry = _registry()
    request = _request(url)
    assert [item.descriptor.id for item in registry.eligible_providers(request)] == [
        "general_webdav", "general_http"]
    assert registry.conditional_claim(registry.providers["general_webdav"], request) is True


@pytest.mark.parametrize("url", ["https://h.example/dav/file.bin", "http://h.example", "https://h.example/a?b=/",
                                 "https://bucket.s3.amazonaws.com/object.zip?X-Amz-Signature=abc",
                                 # A query: WebDAV could not represent it, so it never claims it.
                                 "https://h.example/a/b/?x=1", "http://h.example/dir/?C=M;O=A"])
def test_a_plain_url_without_a_trailing_slash_is_exactly_general_http_as_before(url):
    assert [item.descriptor.id for item in _registry().eligible_providers(_request(url))] == ["general_http"]


def test_disabling_webdav_leaves_plain_urls_with_general_http_and_aliases_unsupported():
    from dataclasses import replace
    registry = _registry()
    provider = registry.providers["general_webdav"]
    provider.descriptor = replace(provider.descriptor, enabled=False)
    assert [item.descriptor.id for item in registry.eligible_providers(_request("https://h.example/dav/"))] == [
        "general_http"]
    with pytest.raises(TransferError) as raised:
        registry.provider_for(_request("webdavs://h.example/dav/"))
    assert raised.value.error.category == Category.UNSUPPORTED_REQUEST


# ── resolve: one discovery of the ordinary HTTP(S) address ────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("url,scheme,address", [
    ("webdav://H.example/dav/", "http", "http://h.example/dav/"),
    ("dav://h.example:8080/dav/My%20Dir/", "http", "http://h.example:8080/dav/My%20Dir/"),
    ("webdavs://h.example/dav/file.bin", "https", "https://h.example/dav/file.bin"),
    ("davs://[2001:db8::1]:8443/x#frag", "https", "https://[2001:db8::1]:8443/x"),
    ("webdavs://h.example", "https", "https://h.example/"),
    ("dav://h.example/dir/?", "http", "http://h.example/dir/"),  # an empty query is no query
])
async def test_resolution_asks_core_to_discover_the_ordinary_address(url, scheme, address):
    result = await _provider(DiscoveryDepth.of(2)).resolve(_request(url))
    assert result.state == ResourceState.PREPARING and result.candidates == () and result.observation is None
    assert (result.discovery.endpoint.scheme, result.discovery.endpoint.address) == (scheme, address)
    assert result.discovery.accepted_input_methods == (InputMethod.USERNAME_PASSWORD,)
    assert result.discovery.depth == DiscoveryDepth.of(2)


@pytest.mark.asyncio
@pytest.mark.parametrize("url,category", [
    ("webdavs://user:secret@h.example/dav/", Category.SECURITY_POLICY_REJECTED),
    ("webdavs://h.example:0/dav/", Category.INVALID_REQUEST),
    ("webdavs://h.example:99999/dav/", Category.INVALID_REQUEST),
    ("webdavs:///dav/", Category.INVALID_REQUEST),
    ("webdavs://h.example/a b/", Category.INVALID_REQUEST),
    # A query has no WebDAV meaning here: refused before any discovery.
    ("webdav://h.example/dav/?view=1", Category.UNSUPPORTED_REQUEST),
    ("webdavs://h.example/f.bin?token=x", Category.UNSUPPORTED_REQUEST),
    ("davs://h.example/dir/?a=b#frag", Category.UNSUPPORTED_REQUEST),
])
async def test_a_malformed_or_credential_bearing_request_fails_normally(url, category):
    with pytest.raises(TransferError) as raised:
        await _provider().resolve(_request(url))
    assert raised.value.error.category == category


# ── resolve_discovered: file, collection, decline ─────────────────────────────

@pytest.mark.asyncio
async def test_a_proven_file_is_one_ordinary_candidate_owned_by_webdav():
    result = await _provider().resolve_discovered(_request("webdavs://H.example/dav/My%20File.bin"),
                                                  _discovered("file", size=77))
    [candidate] = result.candidates
    assert [(item.scheme, item.address) for item in candidate.endpoints] == [
        ("https", "https://h.example/dav/My%20File.bin")]
    assert candidate.provider_id == "general_webdav" and candidate.name == "My File.bin"
    assert candidate.expected_bytes == 77 and candidate.source_identity == SourceIdentity("host", "h.example")
    assert candidate.accepted_input_methods == (InputMethod.USERNAME_PASSWORD,)
    assert candidate.resolver_identity_evidence is None  # nothing WebDAV states is identity evidence


@pytest.mark.asyncio
async def test_a_moved_file_executes_where_the_server_described_it():
    result = await _provider().resolve_discovered(
        _request("webdav://h.example/old/f.bin"), _discovered("file", size=3, location="http://h.example/new/f.bin"))
    assert result.candidates[0].endpoints[0].address == "http://h.example/new/f.bin"


@pytest.mark.asyncio
async def test_a_proven_collection_freezes_ordinary_http_members():
    provider = _provider()
    result = await provider.resolve_discovered(_request("davs://h.example/dav/Album"), _discovered(entries=[
        ("b.flac", 10), ("Disc 1/a.flac", 20), ("cover.jpg", 0)]))
    observation = result.observation
    assert observation.state == ResourceState.AVAILABLE and observation.name == "Album"
    assert [(entry.relative_path, entry.expected_bytes) for entry in observation.file_manifest.entries] == [
        ("Disc 1/a.flac", 20), ("b.flac", 10), ("cover.jpg", 0)]
    assert (await provider.observe(observation.resource)).file_manifest == observation.file_manifest
    members = await provider.manifest(observation.resource)
    assert [(item.request.kind, item.request.payload, item.relative_path) for item in members] == [
        ("https", "https://h.example/dav/Album/Disc%201/a.flac", "Disc 1/a.flac"),
        ("https", "https://h.example/dav/Album/b.flac", "b.flac"),
        ("https", "https://h.example/dav/Album/cover.jpg", "cover.jpg")]
    # Members are plain requests: no depth, no WebDAV flag, no origin marker.
    for item in members:
        assert "webdav" not in repr(item.request).casefold() and "depth" not in repr(item.request).casefold()
    assert "depth" not in repr(observation.resource.context).casefold()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../escape.bin", "/abs.bin", "a//b.bin", "a/./b.bin", "x\x00.bin"])
async def test_an_unsafe_member_path_is_a_protocol_error(path):
    with pytest.raises(TransferError) as raised:
        await _provider().resolve_discovered(_request("webdav://h.example/dav/"), _discovered(entries=[(path, 1)]))
    assert raised.value.error.category == Category.PROTOCOL_ERROR


@pytest.mark.asyncio
async def test_duplicate_member_paths_are_a_protocol_error():
    with pytest.raises(TransferError) as raised:
        await _provider().resolve_discovered(_request("webdav://h.example/dav/"),
                                             _discovered(entries=[("a.bin", 1), ("a.bin", 2)]))
    assert raised.value.error.category == Category.PROTOCOL_ERROR


@pytest.mark.asyncio
async def test_an_empty_collection_is_a_missing_source():
    with pytest.raises(TransferError) as raised:
        await _provider().resolve_discovered(_request("webdav://h.example/dav/empty/"), _discovered())
    assert raised.value.error.category == Category.SOURCE_NOT_FOUND


@pytest.mark.asyncio
async def test_an_opaque_answer_declines_a_plain_url_but_fails_an_explicit_alias():
    declined = await _provider().resolve_discovered(_request("https://h.example/dir/"), _discovered("opaque"))
    assert declined.declined is True and declined.candidates == () and declined.observation is None
    with pytest.raises(TransferError) as raised:
        await _provider().resolve_discovered(_request("webdavs://h.example/dir/"), _discovered("opaque"))
    assert raised.value.error.category == Category.PROTOCOL_ERROR


# ── auth scope, admission and presentation edges ──────────────────────────────

def test_aliases_share_the_http_and_https_authentication_scope_families():
    assert auth_scope("webdavs://h.example/dav/") == auth_scope("https://h.example/dav/a.bin")
    assert auth_scope("davs://h.example/") == auth_scope("https://h.example:443/x")
    assert auth_scope("webdav://h.example/dav/") == auth_scope("http://h.example/dav/a.bin")
    assert auth_scope("dav://h.example:8080/") == auth_scope("http://h.example:8080/x")
    # Authority stays narrow: host, port and the HTTP/HTTPS family all count.
    assert auth_scope("webdavs://h.example/") != auth_scope("https://other.example/")
    assert auth_scope("webdavs://h.example/") != auth_scope("http://h.example/")
    assert auth_scope("webdavs://h.example/") != auth_scope("https://h.example:8443/")


def test_submission_admits_the_aliases_with_their_lan_host():
    links = ["webdav://NAS.local/share/", "webdavs://h.example/f.bin", "dav://10.0.0.5:8080/x/",
             "davs://h.example/y/", "https://h.example/z"]
    assert normalize_direct_links(links) == links
    assert [direct_link_host(link) for link in links] == ["nas.local", "h.example", "10.0.0.5", "h.example",
                                                          "h.example"]
    with pytest.raises(ValueError, match="or a WebDAV URL"):
        normalize_direct_links(["webdav:///no-host"])


def test_the_safe_original_resource_names_the_alias_without_secrets():
    from api.routes import _safe_original_resource
    from transfers import codec
    request = codec.dump(TransferRequest("webdavs", "webdavs://alice:pw@h.example:443/dav/Dir/?token=sig#x"))
    assert _safe_original_resource(codec.load(request)) == "webdavs://h.example/dav/Dir/?…"


def test_the_provider_does_no_transport_or_credential_work():
    source = (ROOT / "backend/providers/general_webdav/provider.py").read_text().casefold()
    for forbidden in ("aiohttp", "socket", "propfind", "authorization", "input_required", "submitted", "etag",
                      "general_http", "registry", "executor"):
        assert forbidden not in source, forbidden
