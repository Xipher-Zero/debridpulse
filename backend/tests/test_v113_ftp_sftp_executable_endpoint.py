"""1.0.13: the (S)FTP provider hands execution the canonical executable
coordinate, never the operator's raw spelling.

Transfer 456: ``sftp://HOST:/absolute/path`` (an empty port delimiter, which
URI parsing reads as host HOST, default port, path ``/absolute/path``) was
discovered, fingerprinted and proven equivalent -- and then every aria2 start
was refused (aria2 1.37.0 ``addUri``: "No URI to download."), because the
candidate endpoint carried the raw operator string. The source-semantics owner
parses the request once and every later consumer (discovery, resolver identity
evidence, the candidate endpoint and so execution) receives the same canonical
URI rebuilt from those parts: scheme, canonical host (IPv6 bracketed), the
port only when one was given, and the path/query/fragment exactly as
submitted (percent-encoding included).
"""
from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from providers.general_ftp.provider import GeneralFtpProvider
from providers.general_scp.provider import ScpProvider
from transfers.errors import Category, TransferError
from transfers.models import DiscoveredEntry, DiscoveryResult, RemoteObjectKind, TransferRequest
from transfers.requests import auth_scope, remote_object_coordinate

pytestmark = pytest.mark.asyncio


def _request(url: str, name: str = "") -> TransferRequest:
    return TransferRequest(url.split(":", 1)[0].lower(), url, name=name)


async def _file(url: str, size: int = 4096):
    """(discovery endpoint, candidate) for a path discovery proved a regular file."""
    provider, request = GeneralFtpProvider(), _request(url)
    discovery = (await provider.resolve(request)).discovery
    result = await provider.resolve_discovered(request, DiscoveryResult(kind=RemoteObjectKind.FILE,
                                                                       expected_bytes=size))
    (candidate,) = result.candidates
    return discovery.endpoint, candidate


@pytest.mark.parametrize("plain,empty_port", [
    ("sftp://example.test/path/file.iso", "sftp://example.test:/path/file.iso"),
    ("ftp://example.test/pub/file.iso", "ftp://example.test:/pub/file.iso"),
    ("sftp://192.0.2.9/srv/My%20File.iso", "sftp://192.0.2.9:/srv/My%20File.iso"),
])
async def test_default_port_spellings_resolve_to_one_executable_coordinate(plain, empty_port) -> None:
    plain_discovery, plain_candidate = await _file(plain)
    spelled_discovery, spelled_candidate = await _file(empty_port)
    (endpoint,) = spelled_candidate.endpoints
    assert endpoint.address == plain, "the raw empty-port delimiter reached the executable endpoint"
    assert urlsplit(endpoint.address).netloc == urlsplit(plain).netloc
    assert spelled_candidate.endpoints == plain_candidate.endpoints
    assert spelled_discovery == plain_discovery == endpoint
    assert (spelled_candidate.resolver_identity_evidence.object_coordinate
            == plain_candidate.resolver_identity_evidence.object_coordinate
            == remote_object_coordinate(endpoint.address) != "")
    assert auth_scope(endpoint.address) == auth_scope(plain)


@pytest.mark.parametrize("url,port", [
    ("sftp://example.test:2222/path/file.iso", 2222),
    ("ftp://example.test:2121/pub/file.iso", 2121),
    ("sftp://example.test:22/path/file.iso", 22),
])
async def test_an_explicit_port_stays_explicit(url, port) -> None:
    discovery, candidate = await _file(url)
    (endpoint,) = candidate.endpoints
    assert urlsplit(endpoint.address).port == port and discovery == endpoint
    assert auth_scope(endpoint.address).port == port
    assert f":{port}/" in candidate.resolver_identity_evidence.object_coordinate


async def test_a_custom_port_never_collapses_to_the_default_coordinate() -> None:
    _d, custom = await _file("sftp://example.test:2222/path/file.iso")
    _d, default = await _file("sftp://example.test:/path/file.iso")
    assert custom.endpoints != default.endpoints
    assert (custom.resolver_identity_evidence.object_coordinate
            != default.resolver_identity_evidence.object_coordinate)


@pytest.mark.parametrize("url,expected", [
    ("sftp://[2001:db8::5]:/data/f.bin", "sftp://[2001:db8::5]/data/f.bin"),
    ("sftp://[2001:db8::5]/data/f.bin", "sftp://[2001:db8::5]/data/f.bin"),
    ("sftp://[2001:DB8::5]:2222/data/f.bin", "sftp://[2001:db8::5]:2222/data/f.bin"),
])
async def test_ipv6_stays_bracketed_and_unambiguous(url, expected) -> None:
    discovery, candidate = await _file(url)
    assert candidate.endpoints[0].address == expected and discovery.address == expected
    parts = urlsplit(expected)
    assert parts.hostname == "2001:db8::5"
    assert candidate.source_identity.key == "2001:db8::5"


@pytest.mark.parametrize("url,expected", [
    ("SFTP://Mirror.Example.ORG:/Data/File.ISO", "sftp://mirror.example.org/Data/File.ISO"),
    ("ftp://files.example.org:2121/archive.zip?type=i", "ftp://files.example.org:2121/archive.zip?type=i"),
    ("ftp://files.example.org:/pub/a;type=i", "ftp://files.example.org/pub/a;type=i"),
    ("sftp://example.test:/a%2Fb/c%23d.bin", "sftp://example.test/a%2Fb/c%23d.bin"),
])
async def test_host_is_canonical_and_path_query_keep_their_exact_spelling(url, expected) -> None:
    _discovery, candidate = await _file(url)
    assert candidate.endpoints[0].address == expected


@pytest.mark.parametrize("url", [
    "sftp://user:secret@example.test:/f.bin",
    "sftp://user@example.test:/f.bin",
    "ftp://:secret@example.test:/f.bin",
])
async def test_credential_bearing_authorities_stay_rejected(url) -> None:
    with pytest.raises(TransferError) as raised:
        await GeneralFtpProvider().resolve(_request(url))
    assert raised.value.error.category == Category.SECURITY_POLICY_REJECTED


async def test_direct_sftp_and_the_scp_derived_route_name_the_same_executable_object() -> None:
    """The working control of transfer 456: the SCP provider's executable SFTP
    URI for the same file. Provider provenance legitimately differs; the
    executable coordinate a writer receives does not."""
    _discovery, direct = await _file("sftp://192.0.2.9:/srv/iso/image.iso", size=6482409472)
    (derived,) = (await ScpProvider().resolve(TransferRequest("scp", "scp://192.0.2.9/srv/iso/image.iso"))).candidates
    assert direct.endpoints[0].address == derived.endpoints[0].address == "sftp://192.0.2.9/srv/iso/image.iso"
    assert (direct.resolver_identity_evidence.object_coordinate
            == derived.resolver_identity_evidence.object_coordinate)


async def test_a_discovered_directory_freezes_canonical_member_addresses() -> None:
    provider, request = GeneralFtpProvider(), _request("sftp://example.test:/pub/dir/")
    discovery = (await provider.resolve(request)).discovery
    assert discovery.endpoint.address == "sftp://example.test/pub/dir/"
    result = await provider.resolve_discovered(request, DiscoveryResult((DiscoveredEntry("a b.bin", 3),)))
    (entry,) = await provider.manifest(result.observation.resource)
    assert entry.request.payload == "sftp://example.test/pub/dir/a%20b.bin"
