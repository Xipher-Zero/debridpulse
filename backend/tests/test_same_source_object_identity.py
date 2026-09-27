"""DP 1.0.13: source independence is not object identity -- and neither is a path.

Two routes to one server are never independent corroboration of each other
(``non_independent_source`` stays). The provider-asserted canonical remote
coordinate -- server scope (transport family, host, port) plus the absolute
path, stated at candidate construction from the facts classification already
established -- is an authoritative ADDRESS, not immutable identity: a server
may replace the contents at one path, even with the same size. So two
same-source routes to one coordinate only become PAIRABLE; what proves them
one material object is the ordinary bounded material evidence, exactly as for
any other pair. The comparison lives in the one evidence owner
(``transfers.mirrors``); core never reconstructs a coordinate from URLs.
"""
from __future__ import annotations

from urllib.parse import unquote, urlsplit

import pytest

from fake_integrations import VaultExecutor
from transfers.mirrors import EvidenceKind, pairing_failure, shared_evidence
from transfers.models import (
    ArtifactFingerprint, DiscoveryResult, Endpoint, IntegrationDescriptor, RemoteObjectKind,
    ResolverArtifactIdentityEvidence, SourceIdentity, TransferCandidate, TransferRequest,
)
from transfers.registry import IntegrationRegistry
from transfers.requests import remote_object_coordinate

pytestmark = pytest.mark.asyncio


async def _candidate(url, *, size=4, name=""):
    """The candidate the real provider constructs for ``url`` (an FTP/SFTP
    path proven a regular file by discovery; an exact SCP/SSH file)."""
    from providers.general_ftp.provider import GeneralFtpProvider
    from providers.general_scp.provider import ScpProvider
    kind = url.split(":", 1)[0]
    request = TransferRequest(kind, url, name=name)
    if kind in {"scp", "ssh"}:
        [candidate] = (await ScpProvider().resolve(request)).candidates
    else:
        [candidate] = (await GeneralFtpProvider().resolve_discovered(
            request, DiscoveryResult(kind=RemoteObjectKind.FILE, expected_bytes=size))).candidates
    return candidate


class Server(VaultExecutor):
    """One remote server's content as each sample observes it, in order: a
    later observation may see contents the server replaced at the same path."""

    descriptor = IntegrationDescriptor("server-sampler", "Server sampler", frozenset())
    claim_schemes = frozenset({"ftp", "sftp"})

    def __init__(self, *observations):
        super().__init__(lambda *_args: True)
        self.observations = list(observations)
        self.sampled = []

    async def fingerprint(self, subject):
        self.sampled.append(unquote(urlsplit(subject.candidate.endpoints[0].address).path))
        body = self.observations.pop(0) if len(self.observations) > 1 else self.observations[0]
        return self._fingerprint_of(body)

    @staticmethod
    def _fingerprint_of(body):
        import hashlib
        return ArtifactFingerprint(len(body), hashlib.sha256(body).hexdigest())


def _registry(executor=None):
    registry = IntegrationRegistry()
    if executor is not None:
        registry.register_executor(executor)
    return registry


async def _evidence(left, right, executor=None):
    return await shared_evidence(left, right, _registry(executor))


ALIASES = [
    ("scp://files.example/pub/file.bin", "sftp://files.example/pub/file.bin"),
    ("ssh://files.example/pub/file.bin", "scp://files.example/pub/file.bin"),
    ("sftp://Files.Example:22/pub/file.bin", "ssh://files.example/pub/file.bin"),
    ("scp://files.example/pub/My%20File.bin", "sftp://files.example/pub/My File.bin"),
    ("ftp://files.example/pub/one.iso", "ftp://files.example:21/pub/one.iso"),
]


@pytest.mark.parametrize("left,right", ALIASES)
async def test_same_source_routes_to_one_coordinate_become_pairable_for_material_proof(left, right):
    left, right = await _candidate(left), await _candidate(right)
    assert pairing_failure(left, right) == ""
    # Pairable is not proven: with no material evidence there is no match.
    evidence = await _evidence(left, right)
    assert evidence.kind == EvidenceKind.UNAVAILABLE and not evidence.proves_collection_member


@pytest.mark.parametrize("left,right", ALIASES)
async def test_matching_sampled_material_proves_same_coordinate_routes_one_object(left, right):
    server = Server(b"four")
    evidence = await _evidence(await _candidate(left), await _candidate(right), server)
    assert evidence.kind == EvidenceKind.FULL_CONTENT_SAMPLE and evidence.proves_individual
    assert len(server.sampled) == 2  # both routes were actually observed


async def test_replaced_content_at_the_same_coordinate_and_size_never_consolidates():
    # Same source, same canonical coordinate, same reported size -- but the
    # server replaced the bytes between the two observations.
    left = await _candidate("scp://files.example/pub/file.bin")
    right = await _candidate("sftp://files.example/pub/file.bin", size=4)
    evidence = await _evidence(left, right, Server(b"four", b"FOUR"))
    assert evidence.kind == EvidenceKind.UNAVAILABLE and evidence.reason == "sample_mismatch"
    assert not evidence.proves_individual and not evidence.proves_collection_member


async def test_the_address_is_never_resolver_attested_identity():
    # A stated coordinate never makes the resolver-attested (independent
    # name+size) path apply to two routes of one source.
    left = await _candidate("sftp://files.example/pub/file.bin", size=4)
    right = await _candidate("ftp://files.example/pub/file.bin", size=4)
    other_host = await _candidate("sftp://mirror.example/pub/file.bin", size=4)
    assert (await _evidence(left, other_host)).kind != EvidenceKind.RESOLVER_ATTESTED
    assert pairing_failure(left, right) == "non_independent_source"


async def test_the_object_identity_is_stated_by_the_provider_not_reconstructed():
    candidate = await _candidate("scp://files.example/pub/file.bin")
    assert candidate.resolver_identity_evidence.object_coordinate == "ssh://files.example:22/pub/file.bin"
    # No resolver-asserted NAME: this never enables name+size attestation across hosts.
    assert candidate.resolver_identity_evidence.resolved_name == ""


@pytest.mark.parametrize("left,right", [
    ("sftp://files.example/a/file.iso", "sftp://files.example/b/file.iso"),     # same host, other object
    ("ftp://files.example/pub/file.iso", "sftp://files.example/pub/file.iso"),  # other transport, other root
    ("sftp://files.example:2222/pub/file.iso", "scp://files.example/pub/file.iso"),  # other server port
])
async def test_the_same_server_with_a_different_coordinate_is_never_paired_or_sampled(left, right):
    server = Server(b"four")  # identical bytes everywhere: still never a pair
    left, right = await _candidate(left), await _candidate(right)
    assert pairing_failure(left, right) == "non_independent_source"
    assert (await _evidence(left, right, server)).reason == "non_independent_source"
    assert server.sampled == []


async def test_disagreeing_known_sizes_of_one_coordinate_are_not_pairable():
    left = await _candidate("sftp://files.example/pub/file.iso", size=4)
    changed = await _candidate("sftp://files.example/pub/file.iso", size=5)
    assert pairing_failure(left, changed) == "size_disagreement"


def _bare(name, size, coordinate=None):
    evidence = None if coordinate is None else ResolverArtifactIdentityEvidence("", 0, object_coordinate=coordinate)
    return TransferCandidate(name, (Endpoint("sftp", f"sftp://files.example/{name}"),), expected_bytes=size,
                             source_identity=SourceIdentity("host", "files.example"),
                             resolver_identity_evidence=evidence)


@pytest.mark.parametrize("left,right", [
    (_bare("file.iso", 4), _bare("file.iso", 4)),                  # host, basename and size alone
    (_bare("file.iso", 4, ""), _bare("file.iso", 4, "")),          # no stated coordinate
])
async def test_insufficient_same_source_identity_is_never_merged_by_host_name_or_size(left, right):
    server = Server(b"four")  # even identical sampled bytes may not stand in for a stated coordinate
    assert pairing_failure(left, right) == "non_independent_source"
    assert (await _evidence(left, right, server)).reason == "non_independent_source"
    assert server.sampled == []


@pytest.mark.parametrize("address,coordinate", [
    ("sftp://files.example/pub/file.bin", "ssh://files.example:22/pub/file.bin"),
    ("scp://[2001:db8::1]:2200/x/y.bin", "ssh://[2001:db8::1]:2200/x/y.bin"),
    ("ftp://FILES.example./pub/a%20b", "ftp://files.example:21/pub/a%20b"),
    ("ftp://files.example/pub/a%2Fb", ""),          # an encoded separator: transports decode it differently
    ("sftp://files.example/~/file.bin", ""),        # home-relative: not a canonical coordinate
    ("sftp://files.example/pub/./file.bin", ""),    # dot segments are never normalized by guessing
    ("sftp://files.example/pub/", ""),              # a directory is not an object coordinate
    ("ftp://files.example/pub/file.bin?type=i", ""),
    ("https://files.example/pub/file.bin", ""),     # only the remote file transports state one
])
def test_the_coordinate_is_canonical_or_absent(address, coordinate):
    assert remote_object_coordinate(address) == coordinate
