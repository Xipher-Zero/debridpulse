"""DP 1.0.13 rsync provider: source semantics only, no native I/O.

The provider parses and normalizes rsync daemon and rsync-over-SSH sources,
asks core for one recursive discovery, and turns the neutral result into an
ordinary candidate or a frozen, provider-neutral file manifest. These cases
also pin its registration and presentation identity.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from executors.rsync.definition import RsyncOptions, definition as executor_definition
from integrations.catalog import definitions
from providers.general_rsync.definition import definition as provider_definition
from providers.general_rsync.provider import GeneralRsyncProvider
from transfers.errors import Category, Domain, TransferError
from transfers.models import (
    DiscoveredEntry, DiscoveryDepth, DiscoveryResult, InputMethod, RemoteObjectKind, ResourceState, TransferRequest,
)
from transfers.registry import IntegrationRegistry
from transfers.requests import auth_scope, remote_object_coordinate

pytestmark = pytest.mark.asyncio
PROVIDER_SOURCE = Path(__file__).resolve().parents[1] / "providers" / "general_rsync" / "provider.py"


def _request(url: str, **extra) -> TransferRequest:
    return TransferRequest(url.split("://", 1)[0], url, **extra)


def _tree(*members) -> DiscoveryResult:
    return DiscoveryResult(tuple(DiscoveredEntry(path.rsplit("/", 1)[-1], size, relative_path=path)
                                 for path, size in members))


# ── identity and registration ────────────────────────────────────────────────

async def test_provider_identity_request_kinds_and_presentation():
    provider = GeneralRsyncProvider()
    assert provider.descriptor.id == "general_rsync" and provider.descriptor.name == "rsync"
    assert provider.descriptor.request_types == frozenset({"rsync", "rsync+ssh"})
    assert provider.applicability.generic_schemes == frozenset({"rsync", "rsync+ssh"})
    assert provider_definition.name == "rsync" and provider_definition.presentation.status_name == "rsync"
    presentation = provider_definition.presentation
    assert (presentation.status_group, presentation.display_order) == ("direct_sources", 913)
    assert executor_definition.id == "rsync" and executor_definition.kind == "executor"
    # Distinct durable identities, both registered; one operator label.
    assert [item.id for item in definitions][-5:] == ["general_webdav", "multimeta", "media", "aria2", "rsync"]
    registry = IntegrationRegistry()
    registry.register_provider(provider)
    with pytest.raises(ValueError):
        registry.register_provider(type("Clash", (), {"descriptor": provider.descriptor.__class__(
            "general_rsync", "rsync", provider.descriptor.capabilities, provider.descriptor.request_types),
            "resolve": provider.resolve})())


async def test_the_executor_options_are_exactly_the_five_tunables():
    assert set(RsyncOptions.model_fields) == {
        "partial_transfers", "compression", "preserve_modification_time",
        "connection_timeout_seconds", "transfer_timeout_seconds"}
    defaults = RsyncOptions()
    assert (defaults.partial_transfers, defaults.compression, defaults.preserve_modification_time) == (
        True, False, True)
    assert (defaults.connection_timeout_seconds, defaults.transfer_timeout_seconds) == (30, 300)


async def test_the_provider_performs_no_native_io():
    tree = ast.parse(PROVIDER_SOURCE.read_text())
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {(node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not imported & {"socket", "subprocess", "asyncio", "asyncssh", "aiohttp", "os", "services", "executors"}


# ── parsing and normalization ────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "rsync://files.example.org/pub/a.bin", "rsync://files.example.org:8873/pub/dir/",
    "rsync://files.example.org/", "rsync+ssh://files.example.org/srv/a.bin",
    "rsync+ssh://files.example.org:2222/~/data/", "rsync://[2001:db8::1]/pub/a.bin",
])
async def test_supported_sources_ask_core_for_one_recursive_discovery(url):
    result = await GeneralRsyncProvider().resolve(_request(url))
    assert result.state == ResourceState.PREPARING and result.discovery.depth == DiscoveryDepth.UNLIMITED
    assert result.discovery.endpoint.scheme == url.split("://", 1)[0]
    expected = {InputMethod.USERNAME_PASSWORD}
    if url.startswith("rsync+ssh"):
        expected.add(InputMethod.USERNAME_PRIVATE_KEY)
    assert set(result.discovery.accepted_input_methods) == expected


@pytest.mark.parametrize("url,category", [
    ("rsync://user:secret@files.example.org/pub/a.bin", Category.SECURITY_POLICY_REJECTED),
    ("rsync+ssh://user@files.example.org/srv/a.bin", Category.SECURITY_POLICY_REJECTED),
    ("rsync://files.example.org/pub/../etc/passwd", Category.INVALID_REQUEST),
    ("rsync://files.example.org/pub/./a.bin", Category.INVALID_REQUEST),
    ("rsync://files.example.org/pub//a.bin", Category.INVALID_REQUEST),
    ("rsync://files.example.org/pub/a.bin?x=1", Category.INVALID_REQUEST),
    ("rsync://files.example.org/pub/a.bin#frag", Category.INVALID_REQUEST),
    ("rsync://files.example.org/pub/a%2Fb.bin", Category.INVALID_REQUEST),
    ("rsync://files.example.org/pub/a%0Ab.bin", Category.INVALID_REQUEST),
    ("rsync://files.example.org/pub/a b.bin", Category.INVALID_REQUEST),
    ("rsync://files.example.org:0/pub/a.bin", Category.INVALID_REQUEST),
    ("rsync://files.example.org:99999/pub/a.bin", Category.INVALID_REQUEST),
    ("rsync:///pub/a.bin", Category.INVALID_REQUEST),
    ("rsync+ssh://files.example.org/", Category.INVALID_REQUEST),
    ("rsync+ssh://files.example.org/~", Category.INVALID_REQUEST),
    ("rsync+ssh://files.example.org/~other/a.bin", Category.UNSUPPORTED_REQUEST),
    ("rsync+ssh://files.example.org/%7E/a.bin", Category.UNSUPPORTED_REQUEST),
    ("files.example.org:/srv/a.bin", Category.INVALID_REQUEST),
])
async def test_malformed_or_credential_bearing_sources_are_refused(url, category):
    kind = url.split("://", 1)[0] if "://" in url else "rsync"
    with pytest.raises(TransferError) as raised:
        await GeneralRsyncProvider().resolve(TransferRequest(kind, url))
    assert raised.value.error.category == category


async def test_an_option_shaped_or_patterned_name_is_just_a_literal_segment():
    provider = GeneralRsyncProvider()
    result = await provider.resolve(_request("rsync://files.example.org/pub/-e%20sh%20x/st%2Ar%5B1%5D.bin"))
    assert result.discovery.endpoint.address == "rsync://files.example.org/pub/-e%20sh%20x/st%2Ar%5B1%5D.bin"


# ── FILE and COLLECTION ──────────────────────────────────────────────────────

async def test_a_proven_regular_file_is_one_candidate_with_host_identity_and_coordinate():
    provider = GeneralRsyncProvider()
    request = _request("rsync://Files.Example.org:8873/pub/My%20File.bin")
    result = await provider.resolve_discovered(request, DiscoveryResult(kind=RemoteObjectKind.FILE,
                                                                        expected_bytes=123))
    (candidate,) = result.candidates
    assert candidate.name == "My File.bin" and candidate.expected_bytes == 123
    assert candidate.provider_id == "general_rsync"
    assert (candidate.source_identity.scope, candidate.source_identity.key) == ("host", "files.example.org")
    assert candidate.endpoints[0].address == "rsync://files.example.org:8873/pub/My%20File.bin"
    assert candidate.resolver_identity_evidence.object_coordinate == \
        "rsync://files.example.org:8873/pub/My%20File.bin"
    assert candidate.accepted_input_methods == (InputMethod.USERNAME_PASSWORD,)


async def test_rsync_over_ssh_keeps_rsync_provenance_and_shares_the_ssh_scope():
    provider = GeneralRsyncProvider()
    request = _request("rsync+ssh://files.example.org/srv/a.bin")
    (candidate,) = (await provider.resolve_discovered(request, DiscoveryResult(kind=RemoteObjectKind.FILE,
                                                                              expected_bytes=5))).candidates
    # The provider is rsync; the executable transport is still the rsync+ssh URL,
    # never an SSH/SFTP address, and no command line appears anywhere.
    assert candidate.provider_id == "general_rsync" and candidate.endpoints[0].scheme == "rsync+ssh"
    # Same server scope as SFTP/SCP for identity and credentials; same coordinate.
    assert auth_scope(candidate.endpoints[0].address) == auth_scope("sftp://files.example.org/srv/a.bin")
    assert candidate.resolver_identity_evidence.object_coordinate == \
        remote_object_coordinate("sftp://files.example.org/srv/a.bin")
    assert auth_scope("rsync://files.example.org/m/a").family == "rsync"


async def test_a_directory_is_its_whole_tree_frozen_into_the_neutral_manifest():
    provider = GeneralRsyncProvider()
    result = await provider.resolve_discovered(_request("rsync://h.example/pub/Album"),
                                               _tree(("Disc 1/01 a.flac", 10), ("cover.jpg", 3),
                                                     ("Disc 2/extra/[x].txt", 1)))
    resource = result.observation.resource
    assert result.observation.name == "Album" and resource.provider_id == "general_rsync"
    manifest = result.observation.file_manifest.entries
    assert {(entry.relative_path, entry.name, entry.expected_bytes) for entry in manifest} == {
        ("Disc 1/01 a.flac", "01 a.flac", 10), ("cover.jpg", "cover.jpg", 3),
        ("Disc 2/extra/[x].txt", "[x].txt", 1)}
    entries = await provider.manifest(resource)
    assert {(entry.relative_path, entry.request.kind, entry.request.payload) for entry in entries} == {
        ("Disc 1/01 a.flac", "rsync", "rsync://h.example/pub/Album/Disc%201/01%20a.flac"),
        ("cover.jpg", "rsync", "rsync://h.example/pub/Album/cover.jpg"),
        ("Disc 2/extra/[x].txt", "rsync", "rsync://h.example/pub/Album/Disc%202/extra/%5Bx%5D.txt")}
    # Frozen: observing never re-lists and returns the same member set.
    assert (await provider.observe(resource)).file_manifest == result.observation.file_manifest


async def test_directory_selection_is_independent_of_a_trailing_slash():
    provider = GeneralRsyncProvider()
    tree = _tree(("a.bin", 1), ("sub/b.bin", 2))
    with_slash = await provider.resolve_discovered(_request("rsync+ssh://h.example/srv/data/"), tree)
    without = await provider.resolve_discovered(_request("rsync+ssh://h.example/srv/data"), tree)
    assert with_slash.observation.resource.context == without.observation.resource.context
    assert with_slash.observation.name == "data"


async def test_a_daemon_root_and_a_named_root_are_collections_named_for_what_was_chosen():
    provider = GeneralRsyncProvider()
    server = await provider.resolve_discovered(_request("rsync://h.example/"),
                                               _tree(("pub/a.bin", 1), ("priv/b.bin", 2)))
    assert server.observation.name == "h.example"
    assert {entry.request.payload for entry in await provider.manifest(server.observation.resource)} == {
        "rsync://h.example/pub/a.bin", "rsync://h.example/priv/b.bin"}
    module = await provider.resolve_discovered(_request("rsync://h.example/pub"), _tree(("a.bin", 1)))
    assert module.observation.name == "pub"
    with pytest.raises(TransferError):
        await provider.resolve_discovered(_request("rsync://h.example/pub"),
                                          DiscoveryResult(kind=RemoteObjectKind.FILE, expected_bytes=1))


async def test_an_empty_tree_is_a_missing_source_and_unsafe_members_are_refused():
    provider = GeneralRsyncProvider()
    with pytest.raises(TransferError) as raised:
        await provider.resolve_discovered(_request("rsync://h.example/pub/empty"), _tree())
    assert (raised.value.error.domain, raised.value.error.category) == (Domain.RESOLUTION, Category.SOURCE_NOT_FOUND)
    for bad in ("../escape.bin", "/abs.bin", "a//b.bin", "a/./b.bin", "a/\x07.bin"):
        with pytest.raises(TransferError):
            await provider.resolve_discovered(_request("rsync://h.example/pub/d"), _tree((bad, 1)))


async def test_home_relative_ssh_collections_keep_their_login_directory_form():
    provider = GeneralRsyncProvider()
    result = await provider.resolve_discovered(_request("rsync+ssh://h.example/~/data/"), _tree(("x.bin", 1)))
    (entry,) = await provider.manifest(result.observation.resource)
    assert entry.request.payload == "rsync+ssh://h.example/~/data/x.bin"
    (candidate,) = (await provider.resolve_discovered(_request("rsync+ssh://h.example/~/data/x.bin"),
                                                      DiscoveryResult(kind=RemoteObjectKind.FILE,
                                                                      expected_bytes=1))).candidates
    # The server resolves the login directory; no absolute coordinate is guessed.
    assert candidate.resolver_identity_evidence.object_coordinate == ""


# ── one operator source, the server decides the transport ────────────────────

@pytest.mark.parametrize("url,alternate", [
    ("rsync://h.example/home/user/file.iso", "rsync+ssh://h.example/home/user/file.iso"),
    ("rsync://h.example/pub/dir", "rsync+ssh://h.example/pub/dir"),
    ("rsync://h.example/pub/dir/", "rsync+ssh://h.example/pub/dir/"),
    ("RSYNC://h.example/~/mine.bin", "rsync+ssh://h.example/~/mine.bin"),
])
async def test_a_plain_rsync_path_names_its_ssh_reading_as_the_alternate_interpretation(url, alternate):
    request = TransferRequest("rsync", url, name="chosen")
    result = await GeneralRsyncProvider().resolve(request)
    # The daemon reading is discovered first; SSH is only its named alternate.
    assert result.discovery.endpoint.scheme == "rsync"
    assert result.discovery.alternate == TransferRequest("rsync+ssh", alternate, name="chosen")
    assert result.discovery.alternate.kind == "rsync+ssh" and not result.candidates
    # The alternate is an ordinary rsync+ssh source: its reading names no further alternate.
    reread = await GeneralRsyncProvider().resolve(result.discovery.alternate)
    assert reread.discovery.endpoint.scheme == "rsync+ssh" and reread.discovery.alternate is None
    assert set(reread.discovery.accepted_input_methods) == {
        InputMethod.USERNAME_PASSWORD, InputMethod.USERNAME_PRIVATE_KEY}


@pytest.mark.parametrize("url", [
    "rsync+ssh://h.example/home/user/file.iso",  # explicit: SSH only, no daemon reading
    "rsync://h.example:8873/pub/file.iso",       # a port names one service
    "rsync://h.example/",                        # the daemon's own list of roots
    "rsync://h.example/~",                       # SSH cannot name a bare login directory
])
async def test_explicit_or_daemon_only_sources_name_no_alternate(url):
    result = await GeneralRsyncProvider().resolve(_request(url))
    assert result.discovery.alternate is None
