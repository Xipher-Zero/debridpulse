"""DP 1.0.13: (S)FTP remote-path classification and directory discovery.

A trailing ``/`` is evidence of directory intent, but its absence is not proof
of a file: every FTP/SFTP source is classified from authoritative remote facts
through the one core-run discovery (provider asks, core orchestrates, the
executor observes over its own transport, trust and authentication owners).
A regular file becomes one ordinary candidate; a directory's immediate regular
files are frozen into the existing neutral file manifest. ``general_ftp``
never connects, lists, trusts or holds a credential itself.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from test_v113_egress_guard_route_scope import FtpOrigin
from test_v113_transport_evidence_sampling import (  # noqa: F401
    PASSWORD, USER, SftpOrigin, candidate, executor_for, guard_for, loopback, submitted,
)
from transfers.errors import Category, TransferError
from transfers.models import (
    Capability, ExecutionSubject, InputMethod, InputReason, ResourceState, SourceIdentity, TransferRequest,
)

pytestmark = pytest.mark.asyncio

ROOT = Path(__file__).resolve().parents[2]
FILES = {"/pub/iso/one.iso": b"one" * 40, "/pub/iso/two.iso": b"two!" * 25, "/pub/iso/nested/deep.iso": b"deep",
         "/pub/lone.bin": b"lone-file"}


def _provider():
    from providers.general_ftp.provider import GeneralFtpProvider
    return GeneralFtpProvider()


def _request(url, name=""):
    return TransferRequest(url.split(":", 1)[0], url, name=name)


def _discovered(kind="directory", entries=(), size=0, directory=""):
    from transfers.models import DiscoveredEntry, DiscoveryResult, RemoteObjectKind
    return DiscoveryResult(tuple(DiscoveredEntry(name, bytes_) for name, bytes_ in entries), directory,
                           kind=RemoteObjectKind(kind), expected_bytes=size)


# ── provider: source semantics only ───────────────────────────────────────────

def test_the_provider_is_manifest_capable_and_still_named_sftp_ftp():
    provider = _provider()
    assert provider.descriptor.id == "general_ftp" and provider.descriptor.name == "(S)FTP"
    assert {Capability.RESOLVE, Capability.RESOURCE_LOOKUP, Capability.FILE_MANIFEST} <= set(provider.descriptor.capabilities)


@pytest.mark.parametrize("url", [
    "ftp://files.example.org/pub/iso/", "sftp://files.example.org:2222/pub/iso/",   # directory intent
    "ftp://files.example.org/pub/iso", "sftp://files.example.org/pub/iso",          # ambiguous
    "ftp://files.example.org/pub/lone.bin", "sftp://files.example.org/pub/lone.bin",
    "ftp://files.example.org/",
])
async def test_every_ftp_or_sftp_path_is_classified_by_core_run_discovery(url):
    result = await _provider().resolve(_request(url))
    assert result.candidates == () and result.observation is None
    assert result.discovery.endpoint.address == url
    assert result.discovery.endpoint.scheme == url.split(":", 1)[0]
    assert result.discovery.accepted_input_methods == (InputMethod.USERNAME_PASSWORD,)


@pytest.mark.parametrize("url,name,address", [
    ("ftp://files.example.org/pub/lone.bin", "lone.bin", "ftp://files.example.org/pub/lone.bin"),
    ("sftp://Mirror.Example.org:2222/data/My%20File.tar.gz", "My File.tar.gz",
     "sftp://mirror.example.org:2222/data/My%20File.tar.gz"),
    ("ftp://files.example.org:2121/archive.zip?type=i", "archive.zip", "ftp://files.example.org:2121/archive.zip?type=i"),
])
async def test_a_path_proven_to_be_a_regular_file_is_one_ordinary_candidate(url, name, address):
    result = await _provider().resolve_discovered(_request(url), _discovered("file", size=77))
    [candidate_] = result.candidates
    # The canonical executable coordinate (test_v113_ftp_sftp_executable_endpoint.py).
    assert [(item.scheme, item.address) for item in candidate_.endpoints] == [(url.split(":", 1)[0], address)]
    assert candidate_.name == name and candidate_.expected_bytes == 77
    assert candidate_.provider_id == "general_ftp"
    assert candidate_.accepted_input_methods == (InputMethod.USERNAME_PASSWORD,)
    assert candidate_.source_identity == SourceIdentity("host", url.split("/")[2].split(":")[0].lower())


@pytest.mark.parametrize("url,base", [
    ("ftp://files.example.org/pub/iso", "ftp://files.example.org/pub/iso/"),
    ("sftp://files.example.org:2222/pub/iso/", "sftp://files.example.org:2222/pub/iso/"),
])
async def test_a_proven_directory_freezes_its_regular_files_into_the_neutral_manifest(url, base):
    provider = _provider()
    result = await provider.resolve_discovered(_request(url), _discovered(entries=[("two.iso", 100), ("one.iso", 120),
                                                                                   ("a b.iso", 3)]))
    observation = result.observation
    assert observation.state == ResourceState.AVAILABLE
    assert [entry.relative_path for entry in observation.file_manifest.entries] == ["a b.iso", "one.iso", "two.iso"]
    assert (await provider.observe(observation.resource)).file_manifest == observation.file_manifest  # never re-lists
    members = await provider.manifest(observation.resource)
    scheme = url.split(":", 1)[0]
    assert [(entry.request.kind, entry.request.payload) for entry in members] == [
        (scheme, base + "a%20b.iso"), (scheme, base + "one.iso"), (scheme, base + "two.iso")]


async def test_an_empty_directory_is_a_missing_source():
    with pytest.raises(TransferError) as raised:
        await _provider().resolve_discovered(_request("ftp://files.example.org/pub/empty/"), _discovered())
    assert raised.value.error.category == Category.SOURCE_NOT_FOUND


def test_the_provider_still_does_no_transport_work():
    source = (ROOT / "backend/providers/general_ftp/provider.py").read_text().casefold()
    for forbidden in ("aiohttp", "socket", "asyncssh", "ftplib", "subprocess", "open_connection", "known_hosts",
                      "ssh-host-key", "ftp-user", "ftp-passwd", "input_required", "nlst", "mlsd", "cwd"):
        assert forbidden not in source, forbidden


# ── executor: authoritative remote classification over the real transports ────

@pytest_asyncio.fixture
async def remote(tmp_path, loopback):
    ftp = await FtpOrigin(dict(FILES), users={USER: PASSWORD}).start()
    plain_ftp = await FtpOrigin(dict(FILES), users={USER: PASSWORD}, mlsd=False).start()
    locked_ftp = await FtpOrigin(dict(FILES), users={USER: PASSWORD}, anonymous=False).start()
    root = tmp_path / "sftp-root"
    for path, body in FILES.items():
        (root / path.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
        (root / path.lstrip("/")).write_bytes(body)
    (root / "pub" / "iso" / "link.iso").symlink_to(root / "pub" / "iso" / "one.iso")
    sftp = await SftpOrigin(root).start()
    guard = guard_for()
    try:
        yield ftp, plain_ftp, locked_ftp, sftp, executor_for(tmp_path, guard)
    finally:
        await guard.stop()
        for origin in (ftp, plain_ftp, locked_ftp, sftp):
            await origin.close()


async def _discover(executor, url, input_=None):
    return await executor.discover(ExecutionSubject.of(candidate(url)), input_)


@pytest.mark.parametrize("origin_index", [0, 1])  # MLSD, then a server without MLSD (NLST + SIZE)
@pytest.mark.parametrize("path", ["/pub/iso/", "/pub/iso"])
async def test_anonymous_ftp_directory_lists_only_immediate_regular_files(remote, origin_index, path):
    ftp, executor = remote[origin_index], remote[4]
    result = await _discover(executor, f"ftp://ftp-origin.test:{ftp.port}{path}")
    assert result.kind.value == "directory"
    assert [(entry.name, entry.expected_bytes) for entry in result.entries] == [("one.iso", 120), ("two.iso", 100)]


async def test_an_ftp_path_that_is_a_file_is_reported_as_one_file(remote):
    ftp, executor = remote[0], remote[4]
    result = await _discover(executor, f"ftp://ftp-origin.test:{ftp.port}/pub/lone.bin")
    assert result.kind.value == "file" and result.expected_bytes == len(FILES["/pub/lone.bin"])


async def test_a_missing_ftp_path_is_a_normalized_missing_source(remote):
    ftp, executor = remote[0], remote[4]
    with pytest.raises(TransferError) as raised:
        await _discover(executor, f"ftp://ftp-origin.test:{ftp.port}/pub/nothing-here")
    assert raised.value.error.category == Category.SOURCE_NOT_FOUND


async def test_a_login_protected_ftp_directory_asks_for_the_generalized_login_then_lists(remote):
    locked, executor = remote[2], remote[4]
    url = f"ftp://ftp-origin.test:{locked.port}/pub/iso/"
    requirement = await _discover(executor, url)
    assert requirement.reason == InputReason.AUTH_REQUIRED
    assert [item.method for item in requirement.methods] == [InputMethod.USERNAME_PASSWORD]
    wrong = await _discover(executor, url, submitted(password="wrong-password-sentinel"))
    assert wrong.reason == InputReason.AUTH_REQUIRED
    listed = await _discover(executor, url, submitted())
    assert [entry.name for entry in listed.entries] == ["one.iso", "two.iso"]


async def test_sftp_classification_confirms_identity_before_any_credential(remote):
    sftp, executor = remote[3], remote[4]
    url = f"sftp://sftp-origin.test:{sftp.port}/pub/iso"
    requirement = await _discover(executor, url)
    assert requirement.reason == InputReason.SERVER_IDENTITY_REQUIRED
    assert sftp.auth_attempts == []
    listed = await _discover(executor, url, submitted(requirement))
    assert listed.kind.value == "directory"
    # Links are never followed and a subdirectory is never entered.
    assert [entry.name for entry in listed.entries] == ["one.iso", "two.iso"]
    single = await _discover(executor, f"sftp://sftp-origin.test:{sftp.port}/pub/lone.bin", submitted(requirement))
    assert single.kind.value == "file" and single.expected_bytes == len(FILES["/pub/lone.bin"])


async def test_a_missing_sftp_path_is_a_normalized_missing_source(remote):
    sftp, executor = remote[3], remote[4]
    requirement = await _discover(executor, f"sftp://sftp-origin.test:{sftp.port}/pub/nothing-here")
    with pytest.raises(TransferError) as raised:
        await _discover(executor, f"sftp://sftp-origin.test:{sftp.port}/pub/nothing-here", submitted(requirement))
    assert raised.value.error.category == Category.SOURCE_NOT_FOUND
