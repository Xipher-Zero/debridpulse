"""1.0.13: the SCP Network Sources provider (exact remote files only).

``scp://`` and remote-file ``ssh://`` requests are interpreted once, by one
provider, into one ordinary neutral candidate whose executable endpoint is the
equivalent ``sftp://`` address. The existing aria2 SFTP claim, evidence
acquisition, server-identity challenge and INPUT_REQUIRED lifecycle own
everything after that; nothing here teaches the core about SCP. Directory,
wildcard and home-relative sources, and credentials embedded in the URL, are
refused clearly rather than guessed at.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

from core.config import AppSettings
from integrations.catalog import definitions, register
from integrations.configuration import normalize_settings, public_integrations
from transfers.errors import Category, TransferError
from transfers.models import ExecutionSubject, InputMethod, ResourceState, SourceIdentity, TransferRequest
from transfers.registry import IntegrationRegistry
from transfers.requests import normalize_direct_links

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
SCP = "general_scp"


def _provider():
    return importlib.import_module("providers.general_scp.provider").ScpProvider()


def _request(url: str, kind: str | None = None, name: str = "") -> TransferRequest:
    return TransferRequest(kind or url.split(":", 1)[0], url, name=name)


async def _candidate(url: str, **kwargs):
    result = await _provider().resolve(_request(url, **kwargs))
    assert result.state == ResourceState.AVAILABLE
    assert len(result.candidates) == 1
    return result.candidates[0]


async def _failure(url: str, kind: str | None = None) -> Category:
    with pytest.raises(TransferError) as raised:
        await _provider().resolve(_request(url, kind))
    return raised.value.error.category


# ── 1. Identity and claims ────────────────────────────────────────────────────

def test_one_provider_claims_exactly_scp_and_ssh() -> None:
    provider = _provider()
    assert provider.descriptor.id == SCP
    assert provider.descriptor.name == "SCP"
    assert provider.descriptor.request_types == frozenset({"scp", "ssh"})
    assert provider.applicability.generic_schemes == frozenset({"scp", "ssh"})
    assert {item.value for item in provider.descriptor.capabilities} == {"resolve"}


def test_the_definition_is_a_network_sources_member_named_scp() -> None:
    definition = next(item for item in definitions if item.id == SCP)
    assert definition.kind == "provider"
    assert definition.name == "SCP"
    presentation = definition.presentation
    assert presentation.status_name == "SCP"
    assert presentation.static_status == "healthy"
    assert presentation.status_group == "direct_sources"
    assert presentation.status_group_label == "Network Sources"
    assert presentation.status_tier == "general_family"
    ftp = next(item for item in definitions if item.id == "general_ftp")
    assert ftp.presentation.display_order < presentation.display_order < 1000
    public = public_integrations(normalize_settings(AppSettings(), definitions), definitions)[SCP]
    assert public["name"] == "SCP" and public["enabled"] is True and public["options"] == {}


# ── 2. Exact-file normalization to one SFTP candidate ─────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("url,endpoint,host,name", [
    ("scp://files.example.org/home/xipher/file.bin", "sftp://files.example.org/home/xipher/file.bin",
     "files.example.org", "file.bin"),
    ("ssh://files.example.org/home/xipher/file.bin", "sftp://files.example.org/home/xipher/file.bin",
     "files.example.org", "file.bin"),
    ("SCP://Files.Example.org/data/My%20File.tar.gz", "sftp://files.example.org/data/My%20File.tar.gz",
     "files.example.org", "My File.tar.gz"),
])
async def test_exact_file_becomes_one_ordinary_sftp_candidate_owned_by_scp(url, endpoint, host, name) -> None:
    candidate = await _candidate(url, kind=url.split(":", 1)[0].lower())
    assert [(item.scheme, item.address) for item in candidate.endpoints] == [("sftp", endpoint)]
    assert candidate.provider_id == SCP
    assert candidate.source_identity == SourceIdentity("host", host)
    assert candidate.name == name
    assert candidate.accepted_input_methods == (InputMethod.USERNAME_PASSWORD,)
    assert candidate.resource is None and dict(candidate.context) == {}
    assert candidate.expected_bytes == 0


@pytest.mark.asyncio
async def test_default_port_is_ssh_22_and_an_explicit_port_is_preserved() -> None:
    bare = await _candidate("scp://files.example.org/f.bin")
    assert urlsplit(bare.endpoints[0].address).port is None  # SFTP's own default: 22
    explicit = await _candidate("ssh://files.example.org:2222/home/xipher/file.bin")
    assert explicit.endpoints[0].address == "sftp://files.example.org:2222/home/xipher/file.bin"
    assert urlsplit(explicit.endpoints[0].address).port == 2222


@pytest.mark.asyncio
@pytest.mark.parametrize("url,endpoint", [
    ("scp://files.example.org:/home/user/file.bin", "sftp://files.example.org/home/user/file.bin"),
    ("scp://[2001:db8::1]:/home/user/file.bin", "sftp://[2001:db8::1]/home/user/file.bin"),
])
async def test_scp_colon_before_path_means_default_port_and_absolute_path(url, endpoint) -> None:
    assert (await _candidate(url)).endpoints[0].address == endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("url,endpoint,host", [
    ("scp://[2001:db8::1]/home/xipher/file.bin", "sftp://[2001:db8::1]/home/xipher/file.bin", "2001:db8::1"),
    ("scp://[2001:DB8::1]:2222/home/xipher/file.bin", "sftp://[2001:db8::1]:2222/home/xipher/file.bin",
     "2001:db8::1"),
])
async def test_bracketed_ipv6_authority_is_parsed_by_the_ordinary_uri_parser(url, endpoint, host) -> None:
    candidate = await _candidate(url)
    assert candidate.endpoints[0].address == endpoint
    assert candidate.source_identity == SourceIdentity("host", host)


@pytest.mark.asyncio
async def test_an_explicit_request_name_is_kept() -> None:
    assert (await _candidate("scp://files.example.org/x.bin", name="Chosen.bin")).name == "Chosen.bin"


# ── 3. Refused shapes (exact files only) ──────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "scp://user@files.example.org/f.bin",
    "scp://user:secret@files.example.org/f.bin",
    "ssh://:secret@files.example.org/f.bin",
])
async def test_embedded_credentials_are_refused_as_a_security_policy_failure(url) -> None:
    with pytest.raises(TransferError) as raised:
        await _provider().resolve(_request(url))
    assert raised.value.error.category == Category.SECURITY_POLICY_REJECTED
    assert "secret" not in str(raised.value.error.as_dict())


@pytest.mark.asyncio
@pytest.mark.parametrize("url,kind", [
    ("scp://files.example.org/f.bin", "ssh"),            # kind/scheme mismatch
    ("scp:///f.bin", "scp"),                              # no authority
    ("scp://files.example.org:0/f.bin", "scp"),
    ("scp://files.example.org:99999/f.bin", "scp"),
    ("scp://files.example.org:home/f.bin", "scp"),        # ambiguous SCP-ish relative form
    ("ssh://files.example.org", "ssh"),                   # nothing to retrieve, never a shell
    ("ssh://files.example.org/", "ssh"),
    ("scp://files.example.org/a\tb.bin", "scp"),
    ("files.example.org/f.bin", "scp"),                   # not absolute
])
async def test_malformed_requests_are_invalid(url, kind) -> None:
    assert await _failure(url, kind) == Category.INVALID_REQUEST


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "scp://files.example.org/home/xipher/releases/",      # directory source
    "scp://files.example.org/home/xipher/releases/*.rar",  # wildcard source
    "scp://files.example.org/home/*/release.rar",          # wildcard directory component
    "scp://files.example.org/home/xipher/file?.bin",       # '?' wildcard / query
    "scp://files.example.org/home/[ab].bin",
    "scp://files.example.org/~/file.bin",                  # home-relative: no shell, no guessing
    "ssh://files.example.org/~xipher/file.bin",
    "scp://files.example.org/f.bin#part",
])
async def test_unsupported_source_shapes_fail_closed_and_never_reach_an_executor(url) -> None:
    assert await _failure(url) == Category.UNSUPPORTED_REQUEST


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http", "https", "ftp", "sftp", "magnet"])
async def test_other_request_kinds_are_unsupported(kind) -> None:
    assert await _failure(f"{kind}://files.example.org/f.bin", kind) == Category.UNSUPPORTED_REQUEST


def test_the_provider_does_no_io_no_shell_and_owns_no_trust_or_credentials() -> None:
    source = (ROOT / "backend/providers/general_scp/provider.py").read_text().casefold()
    for forbidden in ("aiohttp", "httpx", "urlopen", "socket", "asyncssh", "paramiko", "subprocess",
                      "os.system", "shlex", "glob", "fnmatch", "expanduser", "expandvars", "executors.",
                      "ssh-host-key", "known_hosts", "ftp-user", "ftp-passwd", "username_private_key",
                      "input_required", "server_identity", "context="):
        assert forbidden not in source, forbidden


# ── 4. Routing, enablement and the existing executor ─────────────────────────

def _settings(**enabled) -> AppSettings:
    settings = normalize_settings(AppSettings(), definitions)
    for identity, value in enabled.items():
        settings.integrations[identity].enabled = value
    return settings


def _registry(tmp_path, settings) -> IntegrationRegistry:
    from executors.aria2.executor import Aria2Configuration, Aria2Executor
    registry = IntegrationRegistry()
    environment = SimpleNamespace(
        aria2=None, download_root=str(tmp_path), aria2_configuration=Aria2Configuration(str(tmp_path)),
    )
    register(registry, settings, environment, selected=tuple(item for item in definitions if item.kind == "provider"))
    registry.register_executor(Aria2Executor(None, Aria2Configuration(str(tmp_path)), AsyncMock(return_value=True),
                                             egress=SimpleNamespace(ensure_started=AsyncMock(),
                                                                    job_options=lambda *a, **k: {})))
    return registry


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["scp://files.example.org/f.bin", "ssh://files.example.org:2222/f.bin"])
async def test_scp_and_ssh_route_to_scp_and_the_candidate_to_the_existing_aria2_claim(tmp_path, url) -> None:
    registry = _registry(tmp_path, _settings())
    provider = registry.provider_for(_request(url))
    assert provider.descriptor.id == SCP
    candidate = (await provider.resolve(_request(url))).candidates[0]
    assert registry.executor_for_subject(ExecutionSubject("scp", candidate)).descriptor.id == "aria2"


def test_sftp_stays_with_its_existing_owner(tmp_path) -> None:
    registry = _registry(tmp_path, _settings())
    assert registry.provider_for(_request("sftp://files.example.org/f.bin")).descriptor.id == "general_ftp"


@pytest.mark.parametrize("url", ["scp://files.example.org/f.bin", "ssh://files.example.org/f.bin"])
def test_a_disabled_scp_provider_claims_nothing_and_nothing_else_steals_it(tmp_path, url) -> None:
    registry = _registry(tmp_path, _settings(general_scp=False))
    with pytest.raises(TransferError) as raised:
        registry.provider_for(_request(url))
    assert raised.value.error.category in {Category.UNSUPPORTED_REQUEST, Category.UNSUPPORTED_CAPABILITY,
                                           Category.PROVIDER_UNAVAILABLE}
    assert registry.provider_for(_request("sftp://files.example.org/f.bin")).descriptor.id == "general_ftp"


def test_no_scp_executor_and_no_native_scp_transport_exist() -> None:
    from executors.aria2.executor import SUPPORTED_SCHEMES
    assert SUPPORTED_SCHEMES == frozenset({"http", "https", "ftp", "sftp"})
    assert [item.id for item in definitions if item.kind in {"executor", "provider_executor"}] == ["usenet", "aria2"]
    # Nothing ever spawns a native scp/ssh client: every process launch in the
    # backend names a program, and none of them is one.
    for path in (ROOT / "backend").rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text()
        if "subprocess" in text:
            for program in ('"scp"', '"ssh"', "'scp'", "'ssh'"):
                assert program not in text, path


@pytest.mark.asyncio
async def test_scp_ssh_and_sftp_spellings_of_one_object_reach_the_identical_sftp_evidence_subject(tmp_path) -> None:
    """Equivalence stays the existing owner's: the three spellings of one remote
    object hand the existing SFTP evidence acquisition the identical endpoint, so
    the identical bytes produce the identical neutral fingerprint."""
    from providers.general_ftp.provider import GeneralFtpProvider
    from executors.aria2.executor import Aria2Executor
    sftp = (await GeneralFtpProvider().resolve(_request("sftp://files.example.org/pub/big.iso"))).candidates[0]
    for url in ("scp://files.example.org/pub/big.iso", "ssh://files.example.org/pub/big.iso",
                "scp://files.example.org:/pub/big.iso"):
        candidate = await _candidate(url)
        assert Aria2Executor._endpoint(candidate) == Aria2Executor._endpoint(sftp)
        assert candidate.accepted_input_methods == sftp.accepted_input_methods
        assert candidate.source_identity == sftp.source_identity
        assert candidate.content_evidence is None and candidate.resolver_identity_evidence is None


# ── 5. Admission ─────────────────────────────────────────────────────────────

def test_direct_link_admission_accepts_scp_and_ssh() -> None:
    links = ["scp://a.invalid/f", "ssh://a.invalid:2222/g", "scp://a.invalid:/h", "scp://[2001:db8::1]/i"]
    assert normalize_direct_links(links) == links


@pytest.mark.parametrize("link", ["scp://user:hunter2@a.invalid/f", "ssh://user@a.invalid/f"])
def test_embedded_credentials_are_refused_clearly_at_admission_without_echoing_them(link) -> None:
    with pytest.raises(ValueError) as raised:
        normalize_direct_links([link])
    message = str(raised.value)
    assert message.startswith("Credentials embedded in URLs are not supported")
    assert "asks" in message  # the operator is told where credentials go instead
    assert "hunter2" not in message and "user" not in message


def test_admission_wording_names_every_accepted_transport() -> None:
    with pytest.raises(ValueError, match="HTTP, HTTPS, FTP, SFTP, SCP or SSH"):
        normalize_direct_links(["rsync://a.invalid/f"])


# ── 6. Truthful provenance ───────────────────────────────────────────────────

def _stored_request(kind, payload, name="file.bin"):
    return {"kind": kind, "payload": payload, "name": name, "fingerprint": "", "preferred_provider": None}


@pytest.mark.parametrize("payload,expected", [
    ("scp://files.example.org/home/xipher/file.bin", "scp://files.example.org/home/xipher/file.bin"),
    ("ssh://files.example.org:2222/home/xipher/file.bin", "ssh://files.example.org:2222/home/xipher/file.bin"),
    ("scp://[2001:db8::1]:22/home/xipher/file.bin", "scp://[2001:db8::1]/home/xipher/file.bin"),
])
def test_details_present_scp_as_provider_and_the_submitted_scp_or_ssh_source(payload, expected) -> None:
    from api.routes import _public_transfer_presentation
    raw = {
        "id": 7, "status": "downloading", "current_provider_id": SCP,
        "request": _stored_request(payload.split(":", 1)[0], payload),
        "route_attempts": [{"provider_id": SCP, "route_location": "sftp://files.example.org/home/xipher/file.bin"}],
    }
    result = _public_transfer_presentation(raw, definitions)
    assert result["current_provider_name"] == "SCP"
    assert result["route_attempts"][0]["provider_name"] == "SCP"
    assert result["original_resource"] == expected


def test_route_history_keeps_the_sftp_execution_endpoint_truthful() -> None:
    from core.presentation_safety import safe_route_endpoint
    assert safe_route_endpoint("sftp://files.example.org:2222/home/xipher/file.bin") == (
        "sftp://files.example.org:2222", "sftp://files.example.org:2222/home/xipher/file.bin")


# ── 7. Operator-facing identity: SCP / FileDown / #A78BFA ─────────────────────

def test_settings_protocol_chip_declares_scp_glyph_and_colour_once() -> None:
    settings = (STATIC / "ui-settings-page.js").read_text(encoding="utf-8")
    table = settings[settings.index("const PROTOCOL_GLYPHS"):]
    table = table[:table.index("});")]
    assert "general_scp: 'file-down'" in table
    copy = settings[settings.index("const SOURCE_BOX_COPY"):]
    copy = copy[:copy.index("});")]
    assert "general_scp:" in copy
    assert "SFTP" not in copy[copy.index("general_scp:"):], "the SCP box must not describe itself as SFTP"
    icons = (STATIC / "ui-settings-card-icons.css").read_text(encoding="utf-8")
    block = icons[icons.index("[data-protocol='general_scp']"):]
    assert block[:block.index("}")].split("{", 1)[1].strip() == "--dp-protocol-color: #A78BFA;"


def test_file_down_is_vendored_from_the_pinned_lucide_commit_in_lavender() -> None:
    asset = (STATIC / "icons" / "lucide" / "file-down.svg").read_text(encoding="utf-8")
    assert "Lucide file-down @ 23f9abc4ed0146cffededd3d7f94c1018bfdf693" in asset
    assert 'viewBox="0 0 24 24"' in asset and 'stroke="#A78BFA"' in asset
    for path in ('d="M12 18v-6"', 'd="m9 15 3 3 3-3"', 'd="M14 2v5a1 1 0 0 0 1 1h5"'):
        assert path in asset


def test_quick_add_accepts_scp_and_ssh_links() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "/^(?:https?|s?ftp|scp|ssh):\\/\\/\\S+$/i" in app
    assert "enter an HTTP(S), FTP, SFTP, SCP or SSH link or a magnet URI" in app
