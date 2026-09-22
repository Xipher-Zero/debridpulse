"""DP 1.0.13 transport evidence sampling below one neutral CandidateSampling.

Deterministic local origins only: an aiohttp Range server, the in-process
passive FTP origin (with real REST semantics) and an in-process asyncssh SFTP
server. Every FTP/SFTP byte crosses the real ``DownloaderEgressGuard`` CONNECT
boundary; the guard's resolver is the only thing redirected to loopback.

What is proven: HTTP(S), FTP and SFTP produce the SAME ``ArtifactFingerprint``
for the same bytes (one digest definition); access requirements surface as the
existing neutral ``InputRequirement``; only characterized, authoritative
evidence becomes one; SFTP host identity is confirmed strictly before any
authentication; reads are bounded windows; nothing leaves the guard.
"""
from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace

import asyncssh
import pytest
import pytest_asyncio
from aiohttp import web

import executors.aria2.executor as executor_module
import services.network_safety as safety
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from services.downloader_egress_guard import DownloaderEgressGuard
from test_v113_egress_guard_route_scope import FtpOrigin, _answer
from transfers.input_required import SubmittedInput
from transfers.models import (
    ArtifactFingerprint, Endpoint, FingerprintKind, InputFact, InputFactName, InputField, InputMethod,
    InputReason, InputRequirement, TransferCandidate,
)

pytestmark = pytest.mark.asyncio

SAMPLE = 64 * 1024
PAYLOAD = (hashlib.sha256(b"dp-1.0.13-cross-transport").digest() * (3 * SAMPLE // 32 + 1))[:3 * SAMPLE + 17]
SMALL = b"small-object-fully-sampled\n" * 11
# Same length and same name as PAYLOAD, different bytes in both windows.
TWIN = bytes(value ^ 0x5A for value in PAYLOAD)
USER, PASSWORD = "evidence-user-sentinel", "evidence-password-sentinel"


# ── deterministic origins ────────────────────────────────────────────────

class HttpOrigin:
    def __init__(self, files, *, protected=None, scheme_header='Basic realm="dp"'):
        self.files = files
        self.protected = dict(protected or {})
        self.scheme_header = scheme_header
        self.authorizations = []
        self.requests = []  # (path, Authorization, User-Agent)
        self.runner = None
        self.port = 0

    async def start(self):
        app = web.Application()
        app.router.add_get("/{tail:.*}", self._handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = self.runner.addresses[0][1]
        return self

    async def close(self):
        await self.runner.cleanup()

    async def _handle(self, request):
        self.authorizations.append(request.headers.get("Authorization"))
        self.requests.append((request.path, request.headers.get("Authorization"), request.headers.get("User-Agent", "")))
        expected = self.protected.get(request.path)
        if expected is not None:
            token = "Basic " + base64.b64encode(f"{expected[0]}:{expected[1]}".encode()).decode()
            if request.headers.get("Authorization") != token:
                return web.Response(status=401, headers={"WWW-Authenticate": self.scheme_header})
        payload = self.files.get(request.path)
        if payload is None:
            return web.Response(status=404)
        if "Range" not in request.headers:
            return web.Response(body=payload)
        start, _, end = request.headers["Range"].split("=", 1)[1].partition("-")
        start, end = int(start), min(int(end), len(payload) - 1)
        return web.Response(status=206, body=payload[start:end + 1],
                            headers={"Content-Range": f"bytes {start}-{end}/{len(payload)}"})

    def url(self, path, host="http-origin.test"):
        return f"http://{host}:{self.port}{path}"


class _SshServer(asyncssh.SSHServer):
    def __init__(self, origin):
        self.origin = origin

    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        self.origin.auth_attempts.append(username)
        return (username, password) == self.origin.credentials


class SftpOrigin:
    """In-process SFTP server offering ECDSA, Ed25519 and RSA host keys."""

    def __init__(self, root, credentials=(USER, PASSWORD)):
        self.root = root
        self.credentials = credentials
        self.auth_attempts = []
        self.read_bytes = []
        self.keys = {alg: asyncssh.generate_private_key(alg)
                     for alg in ("ssh-rsa", "ssh-ed25519", "ecdsa-sha2-nistp256")}
        self.server = None
        self.port = 0

    def fingerprint(self, alg):
        return hashlib.sha1(self.keys[alg].public_data).hexdigest()

    async def start(self, algorithms=("ssh-rsa", "ssh-ed25519", "ecdsa-sha2-nistp256")):
        origin = self

        class Files(asyncssh.SFTPServer):
            def __init__(self, chan):
                super().__init__(chan, chroot=str(origin.root))

            def read(self, file_obj, offset, size):
                origin.read_bytes.append(size)
                return super().read(file_obj, offset, size)

        self.server = await asyncssh.listen(
            "127.0.0.1", 0, server_host_keys=[self.keys[alg] for alg in algorithms],
            server_factory=lambda: _SshServer(origin), sftp_factory=Files, allow_scp=False,
        )
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    def url(self, path, host="sftp-origin.test"):
        return f"sftp://{host}:{self.port}{path}"


def guard_for(seen=None, *, public=("127.0.0.1",), answers=None):
    async def resolver(host, port):
        if seen is not None:
            seen.append((host, port))
        chosen = answers(host, port) if answers else ["127.0.0.1"]
        return [_answer(address, port) for address in chosen]

    return DownloaderEgressGuard(resolver=resolver, public_check=lambda address: address in public,
                                 bind_port=0)


def executor_for(tmp_path, guard):
    async def authorize(_handle, _action):
        return True

    return Aria2Executor(SimpleNamespace(url="http://aria2.invalid/jsonrpc"),
                         Aria2Configuration(local_root=str(tmp_path)), authorize, egress=guard)


def candidate(url, *, accepts=True, headers=None, expected=0):
    scheme = url.split(":", 1)[0]
    return TransferCandidate("object.bin", (Endpoint(scheme, url, dict(headers or {})),), expected_bytes=expected,
                             accepted_input_methods=(InputMethod.USERNAME_PASSWORD,) if accepts else ())


def submitted(requirement=None, *, username=USER, password=PASSWORD, facts=None):
    facts = requirement.facts if facts is None and requirement is not None else (facts or ())
    return SubmittedInput("challenge", 1, InputMethod.USERNAME_PASSWORD,
                          {InputField.USERNAME: username, InputField.PASSWORD: password}, facts)


@pytest_asyncio.fixture
async def loopback(monkeypatch):
    """Only name resolution is redirected; every policy check still runs."""
    async def validated(uri, **_kwargs):
        return uri

    async def local_resolve(self, host, port=0, family=0):
        import socket
        return [{"hostname": host, "host": "127.0.0.1", "port": port, "family": socket.AF_INET,
                 "proto": 0, "flags": socket.AI_NUMERICHOST}]

    monkeypatch.setattr(safety, "validate_resolved_public_destination", validated)
    monkeypatch.setattr(safety.PublicDestinationResolver, "resolve", local_resolve)
    monkeypatch.setattr(executor_module, "validate_resolved_public_destination", validated)


@pytest_asyncio.fixture
async def origins(tmp_path, loopback):
    files = {"/pub/object.bin": PAYLOAD, "/pub/small.bin": SMALL, "/pub/twin.bin": TWIN}
    http = await HttpOrigin(files, protected={"/locked/object.bin": (USER, PASSWORD)}).start()
    http.files["/locked/object.bin"] = PAYLOAD
    ftp = await FtpOrigin(files, users={USER: PASSWORD}).start()
    locked_ftp = await FtpOrigin({"/data/object.bin": PAYLOAD}, users={USER: PASSWORD}, anonymous=False).start()
    root = tmp_path / "sftp-root"
    (root / "data").mkdir(parents=True)
    (root / "data" / "object.bin").write_bytes(PAYLOAD)
    (root / "data" / "small.bin").write_bytes(SMALL)
    sftp = await SftpOrigin(root).start()
    seen = []
    guard = guard_for(seen)
    try:
        yield SimpleNamespace(http=http, ftp=ftp, locked_ftp=locked_ftp, sftp=sftp, guard=guard, seen=seen,
                              executor=executor_for(tmp_path, guard))
    finally:
        await guard.stop()
        for origin in (http, ftp, locked_ftp, sftp):
            await origin.close()


def _sampling():
    from services import artifact_sampling
    return artifact_sampling


def full(value):
    assert isinstance(value, ArtifactFingerprint), value
    assert value.kind == FingerprintKind.FULL_CONTENT_SAMPLE, value
    return value


# ── one digest definition ────────────────────────────────────────────────

async def test_http_ftp_sftp_produce_the_identical_fingerprint_for_identical_bytes(origins):
    executor = origins.executor
    http = full(await executor.fingerprint(candidate(origins.http.url("/pub/object.bin"))))
    ftp = full(await executor.fingerprint(candidate(f"ftp://ftp-origin.test:{origins.ftp.port}/pub/object.bin")))
    identity = await executor.fingerprint(candidate(origins.sftp.url("/data/object.bin")))
    sftp = full(await executor.fingerprint_with_input(candidate(origins.sftp.url("/data/object.bin")),
                                                      submitted(identity)))
    assert http == ftp == sftp
    first, last = PAYLOAD[:SAMPLE], PAYLOAD[len(PAYLOAD) - SAMPLE:]
    assert http.total_bytes == len(PAYLOAD)
    assert http.signature == _sampling().digest_full(len(PAYLOAD), first, last)
    assert http.prefix_signature == _sampling().digest_prefix(len(PAYLOAD), first)


async def test_small_objects_keep_full_content_semantics_on_every_transport(origins):
    executor = origins.executor
    http = full(await executor.fingerprint(candidate(origins.http.url("/pub/small.bin"))))
    ftp = full(await executor.fingerprint(candidate(f"ftp://ftp-origin.test:{origins.ftp.port}/pub/small.bin")))
    identity = await executor.fingerprint(candidate(origins.sftp.url("/data/small.bin")))
    sftp = full(await executor.fingerprint_with_input(candidate(origins.sftp.url("/data/small.bin")),
                                                      submitted(identity)))
    assert http == ftp == sftp
    assert http.signature == _sampling().digest_full(len(SMALL), SMALL)


async def test_same_name_and_size_with_different_bytes_is_a_different_fingerprint(origins):
    executor = origins.executor
    original = full(await executor.fingerprint(candidate(origins.http.url("/pub/object.bin"))))
    twin = full(await executor.fingerprint(candidate(f"ftp://ftp-origin.test:{origins.ftp.port}/pub/twin.bin")))
    assert original.total_bytes == twin.total_bytes
    assert original.signature != twin.signature and original.prefix_signature != twin.prefix_signature


async def test_artifact_sampling_is_the_one_bounded_content_sampling_owner():
    """network_safety keeps only destination/redirect/address primitives; no
    other production module builds content fingerprints or reads windows."""
    import ast
    from pathlib import Path
    backend = Path(safety.__file__).resolve().parents[1]
    for name in ("sampled_public_artifact_fingerprint", "SAMPLED_FINGERPRINT_SCHEMES", "_range_request",
                 "_read_exactly", "_content_range", "_digest_full", "_digest_prefix", "digest_full", "digest_prefix"):
        assert not hasattr(safety, name), name
    safety_source = Path(safety.__file__).read_text()
    for token in ("ArtifactFingerprint", "FingerprintKind", "hashlib", "Range", "readexactly"):
        assert token not in safety_source, token
    owners = []
    for path in backend.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
                    "sampled_public_artifact_fingerprint", "ftp_fingerprint", "sftp_fingerprint",
                    "digest_full", "digest_prefix", "_offset_windows", "_range_request"}:
                owners.append(path.relative_to(backend).as_posix())
    assert set(owners) == {"services/artifact_sampling.py"}


# ── HTTP(S) ──────────────────────────────────────────────────────────────

async def test_public_http_sampling_is_unchanged(origins):
    result = await _sampling().sampled_public_artifact_fingerprint(origins.http.url("/pub/object.bin"))
    assert result[2] == FingerprintKind.FULL_CONTENT_SAMPLE and result[3] == ""
    assert origins.http.authorizations == [None, None]  # never an Authorization header on public sampling


async def test_definitive_http_auth_is_a_neutral_requirement_for_an_input_capable_candidate(origins):
    locked = candidate(origins.http.url("/locked/object.bin"))
    requirement = await origins.executor.fingerprint(locked)
    assert isinstance(requirement, InputRequirement)
    assert requirement.reason == InputReason.AUTH_REQUIRED and requirement.facts == ()
    proven = full(await origins.executor.fingerprint_with_input(locked, submitted()))
    public = full(await origins.executor.fingerprint(candidate(origins.http.url("/pub/object.bin"))))
    assert proven == public
    wrong = await origins.executor.fingerprint_with_input(locked, submitted(password="wrong"))
    assert isinstance(wrong, InputRequirement) and wrong.reason == InputReason.AUTH_REQUIRED


async def test_http_auth_never_challenges_a_candidate_without_operator_input(origins):
    """Provider-issued capabilities (e.g. a debrid delivery URL) keep their
    pre-existing fact; they never become operator challenges."""
    result = await origins.executor.fingerprint(candidate(origins.http.url("/locked/object.bin"), accepts=False))
    assert result == ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "range_unsupported")


async def test_provider_issued_authorization_is_never_replaced_by_operator_credentials(origins):
    capability = {"Authorization": "Bearer provider-issued-capability"}
    locked = candidate(origins.http.url("/locked/object.bin"), headers=capability)
    result = await origins.executor.fingerprint(locked)
    assert result == ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "range_unsupported")
    assert await origins.executor.fingerprint_with_input(locked, submitted()) is None
    assert "Basic " not in " ".join(value or "" for value in origins.http.authorizations)


async def test_non_basic_http_challenges_are_not_reinterpreted_as_authentication(tmp_path, loopback):
    origin = await HttpOrigin({"/locked/x": PAYLOAD}, protected={"/locked/x": ("a", "b")},
                              scheme_header='Digest realm="dp", nonce="n"').start()
    try:
        result = await executor_for(tmp_path, guard_for()).fingerprint(candidate(origin.url("/locked/x")))
        assert result == ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "range_unsupported")
    finally:
        await origin.close()


async def test_http_generic_failures_keep_their_existing_reasons(origins):
    missing = await origins.executor.fingerprint(candidate(origins.http.url("/pub/missing.bin")))
    assert missing == ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "range_unsupported")


# ── FTP ──────────────────────────────────────────────────────────────────

async def test_anonymous_ftp_is_sampled_in_bounded_windows_through_the_guard(origins):
    ftp = origins.ftp
    result = full(await origins.executor.fingerprint(candidate(f"ftp://ftp-origin.test:{ftp.port}/pub/object.bin")))
    assert result.total_bytes == len(PAYLOAD)
    assert ftp.logins == [("anonymous", True)]
    assert ftp.transfer_types == ["I"] and set(ftp.data_modes) == {"passive"}
    assert ftp.retrieved == ["/pub/object.bin", "/pub/object.bin"]  # exactly two windows, never the whole file
    assert ftp.rest_offsets == [len(PAYLOAD) - SAMPLE]  # only the last window restarts
    hosts = {host for host, _port in origins.seen}
    assert hosts == {"ftp-origin.test"}
    assert len({port for _host, port in origins.seen}) >= 2  # control + server-selected data ports, all guarded


async def test_protected_ftp_requires_authentication_then_samples_with_input(origins):
    locked = candidate(f"ftp://locked-ftp.test:{origins.locked_ftp.port}/data/object.bin")
    requirement = await origins.executor.fingerprint(locked)
    assert isinstance(requirement, InputRequirement) and requirement.reason == InputReason.AUTH_REQUIRED
    proven = full(await origins.executor.fingerprint_with_input(locked, submitted()))
    assert proven == full(await origins.executor.fingerprint(candidate(origins.http.url("/pub/object.bin"))))
    wrong = await origins.executor.fingerprint_with_input(locked, submitted(password="wrong"))
    assert isinstance(wrong, InputRequirement)
    assert origins.locked_ftp.logins == [("anonymous", False), (USER, True), (USER, False)]


async def test_ftp_missing_path_is_not_authentication(origins):
    result = await origins.executor.fingerprint(candidate(f"ftp://ftp-origin.test:{origins.ftp.port}/pub/none.bin"))
    assert isinstance(result, ArtifactFingerprint) and result.kind == FingerprintKind.UNAVAILABLE
    assert result.reason == "range_unsupported"


async def test_ftp_login_rejection_never_challenges_a_candidate_without_input(origins):
    locked = candidate(f"ftp://locked-ftp.test:{origins.locked_ftp.port}/data/object.bin", accepts=False)
    result = await origins.executor.fingerprint(locked)
    assert result == ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "range_unsupported")


async def test_ftp_without_restart_support_reports_only_prefix_evidence(tmp_path, loopback):
    origin = await FtpOrigin({"/x.bin": PAYLOAD}, rest=False).start()
    guard = guard_for()
    try:
        result = await executor_for(tmp_path, guard).fingerprint(candidate(f"ftp://norest.test:{origin.port}/x.bin"))
        assert result.kind == FingerprintKind.PREFIX_CONTENT_SAMPLE and result.reason == "range_unsupported"
        assert result.signature == _sampling().digest_prefix(len(PAYLOAD), PAYLOAD[:SAMPLE])
    finally:
        await guard.stop()
        await origin.close()


async def test_ftp_evidence_uses_aria2_path_semantics(tmp_path, loopback):
    origin = await FtpOrigin({"/home/sp ace/f#.bin": SMALL}).start()
    guard = guard_for()
    try:
        url = f"ftp://paths.test:{origin.port}/home/sp%20ace/f%23.bin"
        full(await executor_for(tmp_path, guard).fingerprint(candidate(url)))
        assert origin.retrieved == ["/home/sp ace/f#.bin"]
    finally:
        await guard.stop()
        await origin.close()


@pytest.mark.parametrize("answers", [
    lambda host, port: ["10.0.0.5"],
    lambda host, port: ["127.0.0.1", "10.0.0.5"],
])
async def test_ftp_control_connection_rejects_private_or_mixed_resolution(tmp_path, loopback, answers):
    origin = await FtpOrigin({"/x.bin": PAYLOAD}).start()
    guard = guard_for(answers=answers)
    try:
        result = await executor_for(tmp_path, guard).fingerprint(candidate(f"ftp://mixed.test:{origin.port}/x.bin"))
        assert result.kind == FingerprintKind.UNAVAILABLE and result.reason == "destination_rejected"
        assert origin.control_connections == 0
    finally:
        await guard.stop()
        await origin.close()


async def test_ftp_passive_data_rebinding_is_rejected(tmp_path, loopback):
    origin = await FtpOrigin({"/x.bin": PAYLOAD}).start()
    guard = guard_for(answers=lambda host, port: ["127.0.0.1"] if port == origin.port else ["10.0.0.9"])
    try:
        result = await executor_for(tmp_path, guard).fingerprint(candidate(f"ftp://rebind.test:{origin.port}/x.bin"))
        assert result.kind == FingerprintKind.UNAVAILABLE
        assert origin.control_connections == 1 and origin.data_connections == 0
    finally:
        await guard.stop()
        await origin.close()


# ── SFTP ─────────────────────────────────────────────────────────────────

async def test_first_sftp_access_is_a_server_identity_challenge_before_any_authentication(origins):
    sftp = origins.sftp
    requirement = await origins.executor.fingerprint(candidate(sftp.url("/data/object.bin")))
    assert isinstance(requirement, InputRequirement)
    assert requirement.reason == InputReason.SERVER_IDENTITY_REQUIRED
    facts = {fact.name: fact.value for fact in requirement.facts}
    assert facts == {
        InputFactName.SERVER_HOST: "sftp-origin.test",
        InputFactName.SERVER_IDENTITY_ALGORITHM: "sha-1",
        # The key the packaged libssh2 negotiates (ECDSA before Ed25519 before RSA), so
        # the identity the operator confirms is exactly the one execution verifies.
        InputFactName.SERVER_IDENTITY_FINGERPRINT: sftp.fingerprint("ecdsa-sha2-nistp256"),
    }
    assert sftp.auth_attempts == []


async def test_sftp_host_key_order_follows_the_native_executor_preference(tmp_path, loopback):
    root = tmp_path / "order"
    root.mkdir()
    sftp = await SftpOrigin(root).start(algorithms=("ssh-rsa", "ssh-ed25519"))
    guard = guard_for()
    try:
        requirement = await executor_for(tmp_path, guard).fingerprint(candidate(sftp.url("/x")))
        facts = {fact.name: fact.value for fact in requirement.facts}
        assert facts[InputFactName.SERVER_IDENTITY_FINGERPRINT] == sftp.fingerprint("ssh-ed25519")
    finally:
        await guard.stop()
        await sftp.close()


async def test_confirmed_sftp_identity_and_credentials_sample_bounded_offsets(origins):
    sftp = origins.sftp
    locked = candidate(sftp.url("/data/object.bin"))
    requirement = await origins.executor.fingerprint(locked)
    proven = full(await origins.executor.fingerprint_with_input(locked, submitted(requirement)))
    assert proven.total_bytes == len(PAYLOAD)
    assert sftp.auth_attempts == [USER]
    assert sum(sftp.read_bytes) <= 2 * SAMPLE  # two bounded windows, never the whole object


async def test_wrong_sftp_password_yields_no_fingerprint_and_asks_again(origins):
    sftp = origins.sftp
    locked = candidate(sftp.url("/data/object.bin"))
    requirement = await origins.executor.fingerprint(locked)
    again = await origins.executor.fingerprint_with_input(locked, submitted(requirement, password="wrong"))
    assert isinstance(again, InputRequirement)
    assert again.reason == InputReason.SERVER_IDENTITY_REQUIRED and set(again.facts) == set(requirement.facts)
    assert sftp.read_bytes == []


async def test_changed_sftp_host_key_fails_closed_before_authentication(origins):
    sftp = origins.sftp
    locked = candidate(sftp.url("/data/object.bin"))
    requirement = await origins.executor.fingerprint(locked)
    forged = tuple(
        InputFact(fact.name, "ab" * 20) if fact.name == InputFactName.SERVER_IDENTITY_FINGERPRINT else fact
        for fact in requirement.facts
    )
    result = await origins.executor.fingerprint_with_input(locked, submitted(requirement, facts=forged))
    assert result == ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "destination_rejected")
    assert sftp.auth_attempts == []  # the password was never offered to a changed host


async def test_sftp_identity_for_another_host_is_refused(origins):
    sftp = origins.sftp
    requirement = await origins.executor.fingerprint(candidate(sftp.url("/data/object.bin")))
    other = candidate(sftp.url("/data/object.bin", host="elsewhere.test"))
    result = await origins.executor.fingerprint_with_input(other, submitted(requirement))
    assert result == ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "destination_rejected")
    assert sftp.auth_attempts == []


async def test_sftp_missing_path_and_directory_are_not_authentication(origins):
    sftp = origins.sftp
    for path in ("/data/none.bin", "/data"):
        locked = candidate(sftp.url(path))
        requirement = await origins.executor.fingerprint(locked)
        result = await origins.executor.fingerprint_with_input(locked, submitted(requirement))
        assert isinstance(result, ArtifactFingerprint) and result.kind == FingerprintKind.UNAVAILABLE
        assert result.reason == "range_unsupported"


async def test_sftp_connects_only_through_the_guard(tmp_path, loopback):
    root = tmp_path / "guarded"
    root.mkdir()
    sftp = await SftpOrigin(root).start()
    guard = guard_for(answers=lambda host, port: ["10.0.0.7"])
    try:
        result = await executor_for(tmp_path, guard).fingerprint(candidate(sftp.url("/x")))
        assert result == ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "destination_rejected")
    finally:
        await guard.stop()
        await sftp.close()


async def test_sftp_evidence_writes_no_local_material(origins, tmp_path):
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    locked = candidate(origins.sftp.url("/data/object.bin"))
    requirement = await origins.executor.fingerprint(locked)
    full(await origins.executor.fingerprint_with_input(locked, submitted(requirement)))
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


async def test_sftp_candidate_without_operator_input_has_no_sample(origins):
    assert await origins.executor.fingerprint(candidate(origins.sftp.url("/data/object.bin"), accepts=False)) is None
    assert origins.sftp.auth_attempts == []


async def test_evidence_errors_never_carry_submitted_secrets(origins, caplog):
    locked = candidate(f"ftp://locked-ftp.test:{origins.locked_ftp.port}/data/object.bin")
    with caplog.at_level("DEBUG"):
        await origins.executor.fingerprint_with_input(locked, submitted(password="wrong-secret-sentinel"))
        sftp_locked = candidate(origins.sftp.url("/data/object.bin"))
        requirement = await origins.executor.fingerprint(sftp_locked)
        await origins.executor.fingerprint_with_input(sftp_locked, submitted(requirement, password="wrong-secret-sentinel"))
    assert "wrong-secret-sentinel" not in caplog.text
    assert USER not in caplog.text
