"""1.0.13: aria2 native FTP/SFTP authentication and fail-closed SFTP host identity.

Every evidence rule below is the conjunction characterized against the packaged
aria2 1.37.0 / libssh2 1.11.1 (Gate 2), never a native code alone:

* FTP login rejection: FTP candidate + accepts USERNAME_PASSWORD + code ``21`` +
  exactly ``The response status is not successful. status=530``;
* SFTP password rejection: SFTP candidate + accepts USERNAME_PASSWORD + code
  ``1`` + exactly ``SSH authentication failure: Authentication failed
  (username/password)``;
* SFTP host identity: SFTP candidate + code ``1`` + exactly ``Unexpected SSH
  host key: expected <sentinel>, actual <40 hex>`` -> neutral
  SERVER_IDENTITY_REQUIRED carrying non-secret facts.

The executor keeps one ``_options`` owner, one ``start`` path, one
``start_with_input`` path and one observation path.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import executors.aria2.executor as executor_module
from executors.aria2.client import Aria2DownloadStatus, Aria2RPCError, Aria2Service
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from executors.aria2.translation import native_failure
from services.downloader_egress_guard import RouteScope
from execution_requests import file_request
from transfers.input_required import validate_submission
from transfers.models import (
    Endpoint, ExecutionObservation, ExecutionRequest, ExecutionState, InputChallenge, InputFact,
    InputFactName, InputMethod, InputOrigin, InputReason, TransferCandidate,
)

SENTINEL = "0" * 40
ACTUAL = "208c2653f8ed2c0d7b62d69b304e8016e4151f60"
FTP_530 = "The response status is not successful. status=530"
SSH_AUTH = "SSH authentication failure: Authentication failed (username/password)"
HOST_MISMATCH = f"Unexpected SSH host key: expected {SENTINEL}, actual {ACTUAL}"
ROOT = Path(__file__).resolve().parents[2]


def _candidate(url: str, *, accepts: bool = True) -> TransferCandidate:
    return TransferCandidate(
        "payload.bin", (Endpoint(url.split(":", 1)[0], url),),
        accepted_input_methods=(InputMethod.USERNAME_PASSWORD,) if accepts else (),
    )


def _failed(code: str, message: str) -> ExecutionObservation:
    handle = SimpleNamespace()
    return ExecutionObservation(handle, ExecutionState.FAILED, error=native_failure(code, message))


def _executor(tmp_path, client=None, *, scopes=None) -> Aria2Executor:
    def job_options(address, *, scope=RouteScope.ENDPOINT):
        if scopes is not None:
            scopes.append((address, scope))
        return {"all-proxy": "http://guard:1"}

    return Aria2Executor(
        client, Aria2Configuration(str(tmp_path), confirmation_delay=0),
        AsyncMock(return_value=True), egress=SimpleNamespace(ensure_started=AsyncMock(), job_options=job_options),
    )


# ── 1. Evidence rules (pure) ──────────────────────────────────────────────────

@pytest.mark.parametrize("url,code,message", [
    ("ftp://files.example.org/f.bin", "21", FTP_530),
    ("sftp://files.example.org/f.bin", "1", SSH_AUTH),
    ("https://files.example.org/f.bin", "24", "Authorization failed."),
    ("http://files.example.org/f.bin", "24", ""),
])
def test_characterized_auth_evidence_is_a_neutral_username_password_challenge(tmp_path, url, code, message) -> None:
    requirement = _executor(tmp_path).input_requirement(_candidate(url), _failed(code, message))
    assert requirement is not None
    assert requirement.reason == InputReason.AUTH_REQUIRED
    assert [item.method for item in requirement.methods] == [InputMethod.USERNAME_PASSWORD]
    assert requirement.facts == ()


@pytest.mark.parametrize("url,code,message", [
    # FTP: code 21 is not authentication evidence on its own.
    ("ftp://files.example.org/f.bin", "21", ""),
    ("ftp://files.example.org/f.bin", "21", "The response status is not successful. status=550"),
    ("ftp://files.example.org/f.bin", "21", "The response status is not successful. status=332"),
    ("ftp://files.example.org/f.bin", "21", "The response status is not successful. status=5301"),
    ("ftp://files.example.org/f.bin", "21", FTP_530 + " extra"),
    ("ftp://files.example.org/f.bin", "3", "Resource not found"),
    ("ftp://files.example.org/f.bin", "1", FTP_530),
    ("ftp://files.example.org/f.bin", "1", SSH_AUTH),
    ("ftp://files.example.org/f.bin", "24", ""),
    # SFTP: code 1 means nothing on its own.
    ("sftp://files.example.org/f.bin", "1", ""),
    ("sftp://files.example.org/f.bin", "1", "SSH opening SFTP path /f.bin failed: Failed opening remote file"),
    ("sftp://files.example.org/f.bin", "1", "No URI available."),
    ("sftp://files.example.org/f.bin", "1", SSH_AUTH + "!"),
    ("sftp://files.example.org/f.bin", "21", FTP_530),
    ("sftp://files.example.org/f.bin", "24", ""),
    # HTTP never interprets FTP/SSH evidence.
    ("https://files.example.org/f.bin", "21", FTP_530),
    ("https://files.example.org/f.bin", "1", SSH_AUTH),
    ("https://files.example.org/f.bin", "1", HOST_MISMATCH),
])
def test_uncharacterized_evidence_is_never_an_input_requirement(tmp_path, url, code, message) -> None:
    assert _executor(tmp_path).input_requirement(_candidate(url), _failed(code, message)) is None


@pytest.mark.parametrize("url,code,message", [
    ("ftp://files.example.org/f.bin", "21", FTP_530),
    ("sftp://files.example.org/f.bin", "1", SSH_AUTH),
    ("sftp://files.example.org/f.bin", "1", HOST_MISMATCH),
    ("https://files.example.org/f.bin", "24", ""),
])
def test_candidates_that_do_not_accept_input_never_challenge(tmp_path, url, code, message) -> None:
    assert _executor(tmp_path).input_requirement(_candidate(url, accepts=False), _failed(code, message)) is None


def test_sentinel_host_key_mismatch_becomes_one_combined_server_identity_challenge(tmp_path) -> None:
    requirement = _executor(tmp_path).input_requirement(
        _candidate("sftp://Files.Example.org:2222/f.bin"), _failed("1", HOST_MISMATCH))
    assert requirement.reason == InputReason.SERVER_IDENTITY_REQUIRED
    assert [item.method for item in requirement.methods] == [InputMethod.USERNAME_PASSWORD]
    assert {fact.name: fact.value for fact in requirement.facts} == {
        InputFactName.SERVER_HOST: "files.example.org",
        InputFactName.SERVER_IDENTITY_ALGORITHM: "sha-1",
        InputFactName.SERVER_IDENTITY_FINGERPRINT: ACTUAL,
    }
    assert SENTINEL not in str(requirement)


@pytest.mark.parametrize("message", [
    f"Unexpected SSH host key: expected {'1' * 40}, actual {ACTUAL}",     # a confirmed key changed
    f"Unexpected SSH host key: expected {SENTINEL}, actual {ACTUAL.upper()}",
    f"Unexpected SSH host key: expected {SENTINEL}, actual {ACTUAL[:-1]}",
    f"Unexpected SSH host key: expected {SENTINEL}, actual {SENTINEL}",
    f"Unexpected SSH host key: expected {SENTINEL}, actual {ACTUAL} ",
])
def test_host_key_evidence_is_parsed_strictly_and_a_changed_key_fails_closed(tmp_path, message) -> None:
    assert _executor(tmp_path).input_requirement(_candidate("sftp://files.example.org/f"), _failed("1", message)) is None


# ── 2. One option owner: scheme-correct native translation ───────────────────

async def _options(tmp_path, monkeypatch, url, submitted=None, **kwargs):
    async def validated(address):
        return address
    monkeypatch.setattr(executor_module, "validate_resolved_public_destination", validated)
    scopes: list = []
    executor = _executor(tmp_path, scopes=scopes)
    request = file_request(_candidate(url), str(tmp_path / "payload.bin"), "attempt")
    address, options = await executor._options(request, executor.prepare(request), submitted, **kwargs)
    return address, options, scopes


@pytest.mark.asyncio
async def test_initial_sftp_job_carries_the_fail_closed_sentinel_and_no_credentials(tmp_path, monkeypatch) -> None:
    _address, options, scopes = await _options(tmp_path, monkeypatch, "sftp://files.example.org/f.bin")
    assert options["ssh-host-key-md"] == f"sha-1={SENTINEL}"
    assert options["ftp-reuse-connection"] == "false"
    assert options["ftp-user"] == "anonymous" and options["ftp-passwd"] == "ARIA2USER@"
    assert options["http-user"] == "" and options["http-passwd"] == ""
    assert "ftp-pasv" not in options and "ftp-type" not in options
    assert scopes == [("sftp://files.example.org/f.bin", RouteScope.ENDPOINT)]


@pytest.mark.asyncio
async def test_initial_ftp_job_is_anonymous_same_host_scoped_and_never_reuses_connections(tmp_path, monkeypatch) -> None:
    _address, options, scopes = await _options(tmp_path, monkeypatch, "ftp://files.example.org/f.bin")
    assert options["ftp-user"] == "anonymous" and options["ftp-passwd"] == "ARIA2USER@"
    assert options["ftp-reuse-connection"] == "false"
    # Passive is the guarded FTP transport contract; binary keeps bytes exact.
    assert options["ftp-pasv"] == "true"
    assert options["ftp-type"] == "binary"
    assert "ssh-host-key-md" not in options
    assert options["no-netrc"] == "true"
    assert scopes == [("ftp://files.example.org/f.bin", RouteScope.SAME_HOST)]


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_http_jobs_keep_the_exact_endpoint_scope_and_unchanged_options(tmp_path, monkeypatch, scheme) -> None:
    _address, options, scopes = await _options(tmp_path, monkeypatch, f"{scheme}://files.example.org/f.bin")
    assert scopes == [(f"{scheme}://files.example.org/f.bin", RouteScope.ENDPOINT)]
    for native in ("ssh-host-key-md", "ftp-user", "ftp-passwd", "ftp-reuse-connection", "ftp-pasv", "ftp-type"):
        assert native not in options
    assert options["http-auth-challenge"] == "true" and options["http-user"] == "" and options["http-passwd"] == ""


# ── 3. Continuation through the one start_with_input path ────────────────────

class Daemon:
    """Scriptable aria2: each addUri pops the next terminal (code, message)."""

    def __init__(self):
        self.jobs: dict[str, Aria2DownloadStatus] = {}
        self.options: dict[str, dict] = {}
        self.outcomes: list[tuple[str, str]] = []
        self.calls: list = []
        self.option_reads: list = []

    async def tell_status(self, gid):
        if gid not in self.jobs:
            raise Aria2RPCError(f"aria2 [1]: GID {gid} is not found", code=1)
        return self.jobs[gid]

    async def get_option(self, gid, name):
        self.option_reads.append((gid, name))
        if gid not in self.options:
            raise Aria2RPCError(f"aria2 [1]: GID {gid} is not found", code=1)
        return str(self.options[gid].get(name, ""))

    async def _call(self, method, params):
        self.calls.append((method, params))
        if method == "aria2.addUri":
            options = dict(params[1])
            gid = options["gid"]
            code, message = self.outcomes.pop(0) if self.outcomes else ("", "")
            path = str(Path(options["dir"]) / options["out"])
            self.options[gid] = options
            self.jobs[gid] = Aria2DownloadStatus(gid, "error" if code else "active", 0, 0, 0, code, message,
                                                 files=[{"path": path}])
            return gid
        if method == "aria2.removeDownloadResult":
            self.jobs.pop(params[0], None)
            self.options.pop(params[0], None)
            return "OK"
        raise AssertionError(method)


def _submitted(requirement, *, username="operator", password="s3cret-pass", facts=None):
    challenge = InputChallenge("challenge", 1, 1, requirement.reason, InputOrigin.EXECUTOR, "aria2", "attempt",
                               requirement.methods, facts=requirement.facts if facts is None else facts)
    return validate_submission(challenge, InputMethod.USERNAME_PASSWORD.value, {"username": username, "password": password})


async def _started(tmp_path, monkeypatch, url, outcomes):
    async def validated(address):
        return address
    monkeypatch.setattr(executor_module, "validate_resolved_public_destination", validated)
    daemon = Daemon()
    daemon.outcomes = list(outcomes)
    executor = _executor(tmp_path, daemon)
    request = file_request(_candidate(url), str(tmp_path / "payload.bin"), "attempt")
    handle = executor.prepare(request)
    started = await executor.start(request, handle)
    assert started.error is None
    observed = await executor.observe(handle)
    return daemon, executor, request, handle, observed


def _added(daemon):
    return [params[1] for method, params in daemon.calls if method == "aria2.addUri"]


@pytest.mark.asyncio
async def test_ftp_530_continuation_resubmits_the_same_attempt_with_ftp_credentials(tmp_path, monkeypatch) -> None:
    daemon, executor, request, handle, observed = await _started(
        tmp_path, monkeypatch, "ftp://files.example.org/f.bin", [("21", FTP_530)])
    requirement = executor.input_requirement(request.work.subject.candidate, observed)
    assert requirement.reason == InputReason.AUTH_REQUIRED
    result = await executor.start_with_input(request, handle, _submitted(requirement))
    assert result.error is None and result.state == ExecutionState.QUEUED
    first, second = _added(daemon)
    assert second["gid"] == first["gid"] == handle.native["gid"]
    assert second["ftp-user"] == "operator" and second["ftp-passwd"] == "s3cret-pass"
    assert second["http-user"] == "" and second["http-passwd"] == ""
    assert second["ftp-reuse-connection"] == "false"
    assert second["ftp-pasv"] == "true" and second["ftp-type"] == "binary"
    assert [method for method, _ in daemon.calls] == ["aria2.addUri", "aria2.removeDownloadResult", "aria2.addUri"]


@pytest.mark.asyncio
async def test_sftp_identity_confirmation_resubmits_with_the_confirmed_key_and_credentials(tmp_path, monkeypatch) -> None:
    daemon, executor, request, handle, observed = await _started(
        tmp_path, monkeypatch, "sftp://files.example.org/f.bin", [("1", HOST_MISMATCH)])
    requirement = executor.input_requirement(request.work.subject.candidate, observed)
    assert requirement.reason == InputReason.SERVER_IDENTITY_REQUIRED
    result = await executor.start_with_input(request, handle, _submitted(requirement))
    assert result.error is None
    first, second = _added(daemon)
    assert first["ssh-host-key-md"] == f"sha-1={SENTINEL}"
    assert second["ssh-host-key-md"] == f"sha-1={ACTUAL}"
    assert second["ftp-user"] == "operator" and second["ftp-passwd"] == "s3cret-pass"
    assert second["gid"] == handle.native["gid"]


@pytest.mark.asyncio
@pytest.mark.parametrize("facts", [
    (),
    (InputFact(InputFactName.SERVER_HOST, "files.example.org"),
     InputFact(InputFactName.SERVER_IDENTITY_ALGORITHM, "sha-1"),
     InputFact(InputFactName.SERVER_IDENTITY_FINGERPRINT, "1" * 40)),
    (InputFact(InputFactName.SERVER_HOST, "other.example.org"),
     InputFact(InputFactName.SERVER_IDENTITY_ALGORITHM, "sha-1"),
     InputFact(InputFactName.SERVER_IDENTITY_FINGERPRINT, ACTUAL)),
])
async def test_identity_acceptance_must_match_the_live_observed_identity(tmp_path, monkeypatch, facts) -> None:
    daemon, executor, request, handle, observed = await _started(
        tmp_path, monkeypatch, "sftp://files.example.org/f.bin", [("1", HOST_MISMATCH)])
    requirement = executor.input_requirement(request.work.subject.candidate, observed)
    submitted = _submitted(requirement, facts=facts) if facts else _submitted(
        SimpleNamespace(reason=InputReason.AUTH_REQUIRED, methods=requirement.methods, facts=()))
    result = await executor.start_with_input(request, handle, submitted)
    assert result.state in {ExecutionState.FAILED, ExecutionState.UNKNOWN}
    assert len(_added(daemon)) == 1


@pytest.mark.asyncio
async def test_sftp_credential_retry_recovers_and_reuses_the_confirmed_host_key(tmp_path, monkeypatch) -> None:
    daemon, executor, request, handle, observed = await _started(
        tmp_path, monkeypatch, "sftp://files.example.org/f.bin", [("1", HOST_MISMATCH), ("1", SSH_AUTH)])
    identity = executor.input_requirement(request.work.subject.candidate, observed)
    await executor.start_with_input(request, handle, _submitted(identity, password="wrong"))
    failed = await executor.observe(handle)
    retry = executor.input_requirement(request.work.subject.candidate, failed)
    assert retry.reason == InputReason.AUTH_REQUIRED
    result = await executor.start_with_input(request, handle, _submitted(retry, password="right"))
    assert result.error is None
    third = _added(daemon)[2]
    assert third["ssh-host-key-md"] == f"sha-1={ACTUAL}"
    assert third["ftp-passwd"] == "right"
    assert daemon.option_reads == [(handle.native["gid"], "ssh-host-key-md")]


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, "", f"sha-1={SENTINEL}", "sha-1=zz", f"md5={ACTUAL[:32]}", f"sha-256={'a' * 64}"])
async def test_unrecoverable_confirmed_identity_fails_closed_without_resubmitting(tmp_path, monkeypatch, stored) -> None:
    daemon, executor, request, handle, observed = await _started(
        tmp_path, monkeypatch, "sftp://files.example.org/f.bin", [("1", HOST_MISMATCH), ("1", SSH_AUTH)])
    identity = executor.input_requirement(request.work.subject.candidate, observed)
    await executor.start_with_input(request, handle, _submitted(identity))
    gid = handle.native["gid"]
    if stored is None:
        daemon.options.pop(gid)
    else:
        daemon.options[gid]["ssh-host-key-md"] = stored
    retry = executor.input_requirement(request.work.subject.candidate, await executor.observe(handle))
    result = await executor.start_with_input(request, handle, _submitted(retry))
    assert result.state in {ExecutionState.FAILED, ExecutionState.UNKNOWN}
    assert len(_added(daemon)) == 2
    assert all(method != "aria2.removeDownloadResult" for method, _ in daemon.calls[3:])


@pytest.mark.asyncio
async def test_generic_failures_cannot_be_continued_with_input(tmp_path, monkeypatch) -> None:
    daemon, executor, request, handle, observed = await _started(
        tmp_path, monkeypatch, "ftp://files.example.org/f.bin", [("21", "The response status is not successful. status=550")])
    fake = SimpleNamespace(reason=InputReason.AUTH_REQUIRED,
                           methods=executor.input_requirement(_candidate("ftp://x"), _failed("21", FTP_530)).methods, facts=())
    result = await executor.start_with_input(request, handle, _submitted(fake))
    assert result.state in {ExecutionState.FAILED, ExecutionState.UNKNOWN}
    assert len(_added(daemon)) == 1


@pytest.mark.asyncio
async def test_http_code_24_continuation_is_unchanged(tmp_path, monkeypatch) -> None:
    daemon, executor, request, handle, observed = await _started(
        tmp_path, monkeypatch, "https://files.example.org/f.bin", [("24", "Authorization failed.")])
    requirement = executor.input_requirement(request.work.subject.candidate, observed)
    await executor.start_with_input(request, handle, _submitted(requirement))
    second = _added(daemon)[1]
    assert second["http-user"] == "operator" and second["http-passwd"] == "s3cret-pass"
    assert second["http-auth-challenge"] == "false"
    for native in ("ftp-user", "ftp-passwd", "ssh-host-key-md"):
        assert native not in second


# ── 4. The narrow host-key readback never retains other option values ────────

@pytest.mark.asyncio
async def test_client_option_read_returns_one_value_and_discards_the_plaintext_map(monkeypatch) -> None:
    service = Aria2Service("http://127.0.0.1:1/jsonrpc", "rpc")
    response = {"ssh-host-key-md": f"sha-1={ACTUAL}", "ftp-passwd": "plaintext", "http-passwd": "plaintext2"}
    seen = []

    async def call(method, params=None, **_kwargs):
        seen.append((method, params))
        return response

    monkeypatch.setattr(service, "_call", call)
    assert await service.get_option("abcdef0123456789", "ssh-host-key-md") == f"sha-1={ACTUAL}"
    assert seen == [("aria2.getOption", ["abcdef0123456789"])]
    assert response == {}


# ── 5. Architecture: one executor path, no private-key SFTP ──────────────────

def test_one_option_owner_and_one_path_per_operation() -> None:
    source = inspect.getsource(executor_module)
    for name in ("def _options(", "def start(", "def start_with_input(", "def observe(", "def input_requirement("):
        assert source.count(name) == 1
    for forbidden in ("_http_executor", "_ftp_executor", "_sftp_executor", "USERNAME_PRIVATE_KEY", "username_private_key",
                      "known_hosts", "keyscan", "StrictHostKeyChecking"):
        assert forbidden not in source
    assert 'options["ftp-reuse-connection"] = "false"' in source


# ── 5b. Evidence-origin input reaching a freshly prepared attempt ─────────────
# Pre-writer evidence acquisition may already have proven a candidate with the
# operator's input; the writer admitted for it then starts through the SAME
# start_with_input continuation. The executor still enforces its own security
# facts: the confirmed SFTP identity becomes the exact ssh-host-key-md aria2
# verifies before authenticating, and nothing unconfirmed ever starts a job.

def _evidence_input(host="files.example.org", fingerprint=ACTUAL, algorithm="sha-1"):
    from transfers.input_required import server_identity_required, username_password
    requirement = server_identity_required(username_password(), host=host, algorithm=algorithm,
                                           fingerprint=fingerprint)
    challenge = InputChallenge("challenge", 1, 1, requirement.reason, InputOrigin.EVIDENCE, "aria2", "candidate",
                               requirement.methods, facts=requirement.facts)
    return validate_submission(challenge, InputMethod.USERNAME_PASSWORD.value,
                               {"username": "operator", "password": "s3cret-pass"})


async def _fresh(tmp_path, monkeypatch, url):
    async def validated(address):
        return address
    monkeypatch.setattr(executor_module, "validate_resolved_public_destination", validated)
    daemon = Daemon()
    executor = _executor(tmp_path, daemon)
    request = file_request(_candidate(url), str(tmp_path / "payload.bin"), "fresh-attempt")
    return daemon, executor, request, executor.prepare(request)


@pytest.mark.asyncio
async def test_evidence_confirmed_identity_is_the_first_jobs_exact_host_key(tmp_path, monkeypatch) -> None:
    daemon, executor, request, handle = await _fresh(tmp_path, monkeypatch, "sftp://files.example.org/f.bin")
    result = await executor.start_with_input(request, handle, _evidence_input())
    assert result.error is None and result.state == ExecutionState.QUEUED
    (only,) = _added(daemon)
    assert only["ssh-host-key-md"] == f"sha-1={ACTUAL}"  # never the probe sentinel
    assert only["ftp-user"] == "operator" and only["ftp-passwd"] == "s3cret-pass"
    assert only["gid"] == handle.native["gid"]
    assert [method for method, _ in daemon.calls] == ["aria2.addUri"]


@pytest.mark.asyncio
@pytest.mark.parametrize("facts", [
    {"host": "other.example.org"},
    {"fingerprint": SENTINEL},
    {"algorithm": "md5"},
])
async def test_unconfirmed_or_foreign_identity_never_starts_a_job(tmp_path, monkeypatch, facts) -> None:
    daemon, executor, request, handle = await _fresh(tmp_path, monkeypatch, "sftp://files.example.org/f.bin")
    submitted = _evidence_input(**facts)
    result = await executor.start_with_input(request, handle, submitted)
    assert result.state == ExecutionState.FAILED and result.error.domain.value == "security"
    assert _added(daemon) == []
    assert "s3cret-pass" not in repr(result.error.as_dict(diagnostics=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("url,user_key,password_key", [
    ("https://files.example.org/f.bin", "http-user", "http-passwd"),
    ("ftp://files.example.org/f.bin", "ftp-user", "ftp-passwd"),
])
async def test_evidence_credentials_start_http_and_ftp_writers_directly(tmp_path, monkeypatch, url, user_key,
                                                                        password_key) -> None:
    from transfers.input_required import auth_required, username_password
    daemon, executor, request, handle = await _fresh(tmp_path, monkeypatch, url)
    requirement = auth_required(username_password())
    challenge = InputChallenge("challenge", 1, 1, requirement.reason, InputOrigin.EVIDENCE, "aria2", "candidate",
                               requirement.methods)
    submitted = validate_submission(challenge, InputMethod.USERNAME_PASSWORD.value,
                                    {"username": "operator", "password": "s3cret-pass"})
    result = await executor.start_with_input(request, handle, submitted)
    assert result.error is None
    (only,) = _added(daemon)
    assert only[user_key] == "operator" and only[password_key] == "s3cret-pass"
    if url.startswith("ftp"):
        assert only["ftp-pasv"] == "true" and only["ftp-type"] == "binary" and only["ftp-reuse-connection"] == "false"
    else:
        assert only["http-auth-challenge"] == "false"


@pytest.mark.asyncio
async def test_a_started_job_always_takes_the_challenge_continuation_path(tmp_path, monkeypatch) -> None:
    daemon, executor, request, handle, observed = await _started(
        tmp_path, monkeypatch, "sftp://files.example.org/f.bin", [("1", HOST_MISMATCH)])
    # Even an evidence-shaped submission cannot turn a live challenged job into a fresh start.
    result = await executor.start_with_input(request, handle, _evidence_input(fingerprint="1" * 40))
    assert result.state == ExecutionState.FAILED
    assert len(_added(daemon)) == 1


# ── 6. Real aria2 through the real guard: FTP 530 -> challenge -> transfer ────

async def _real(tmp_path, monkeypatch, origin, *, daemon_globals=()):
    from test_v1111_aria2_security_boundary import _answer, _start_aria2
    from services.downloader_egress_guard import DownloaderEgressGuard

    async def resolver(host, port):
        return [_answer("127.0.0.1", port)]

    async def validated(address):
        return address

    monkeypatch.setattr(executor_module, "validate_resolved_public_destination", validated)
    guard = DownloaderEgressGuard(resolver=resolver, public_check=lambda a: a == "127.0.0.1", bind_port=0)
    proc, service = await _start_aria2(tmp_path, extra_args=tuple(daemon_globals))
    executor = Aria2Executor(service, Aria2Configuration(str(tmp_path), confirmation_delay=0.02),
                             AsyncMock(return_value=True), egress=guard)
    url = f"ftp://files.test:{origin.port}/pub/file.bin"
    request = file_request(_candidate(url), str(tmp_path / "file.bin"), "real-attempt")
    return guard, proc, service, executor, request


async def _terminal(executor, handle):
    import asyncio
    for _ in range(200):
        observed = await executor.observe(handle)
        if observed.state in {ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED}:
            return observed
        await asyncio.sleep(0.05)
    raise AssertionError(observed)


@pytest.mark.asyncio
async def test_real_aria2_anonymous_ftp_transfers_through_the_executor(tmp_path, monkeypatch) -> None:
    from test_v113_egress_guard_route_scope import FtpOrigin
    from test_v1111_aria2_security_boundary import _stop_aria2

    origin = await FtpOrigin({"/pub/file.bin": b"anonymous-bytes"}).start()
    guard, proc, service, executor, request = await _real(tmp_path, monkeypatch, origin)
    try:
        handle = executor.prepare(request)
        assert (await executor.start(request, handle)).error is None
        observed = await _terminal(executor, handle)
        assert observed.state == ExecutionState.SUCCEEDED, observed.error
        assert (tmp_path / "file.bin").read_bytes() == b"anonymous-bytes"
        assert origin.logins == [("anonymous", True)]
    finally:
        await _stop_aria2(proc, service)
        await guard.stop()
        await origin.close()


@pytest.mark.asyncio
async def test_real_aria2_protected_ftp_challenges_then_continues_with_a_fresh_login(tmp_path, monkeypatch) -> None:
    from test_v113_egress_guard_route_scope import FtpOrigin
    from test_v1111_aria2_security_boundary import _stop_aria2

    origin = await FtpOrigin({"/pub/file.bin": b"protected-bytes"}, users={"operator": "right"}, anonymous=False).start()
    guard, proc, service, executor, request = await _real(tmp_path, monkeypatch, origin)
    try:
        handle = executor.prepare(request)
        assert (await executor.start(request, handle)).error is None
        observed = await _terminal(executor, handle)
        assert (observed.error.native_code, observed.error.diagnostic) == ("21", FTP_530)
        requirement = executor.input_requirement(request.work.subject.candidate, observed)
        assert requirement.reason == InputReason.AUTH_REQUIRED

        await executor.start_with_input(request, handle, _submitted(requirement, username="operator", password="wrong"))
        rejected = await _terminal(executor, handle)
        assert executor.input_requirement(request.work.subject.candidate, rejected).reason == InputReason.AUTH_REQUIRED

        await executor.start_with_input(request, handle, _submitted(requirement, username="operator", password="right"))
        observed = await _terminal(executor, handle)
        assert observed.state == ExecutionState.SUCCEEDED, observed.error
        assert (tmp_path / "file.bin").read_bytes() == b"protected-bytes"
        # Every attempt authenticated freshly: no pooled session stood in.
        assert origin.logins == [("anonymous", False), ("operator", False), ("operator", True)]
    finally:
        await _stop_aria2(proc, service)
        await guard.stop()
        await origin.close()


# ── 7. Owned FTP jobs never inherit a daemon's global FTP transport semantics ─
# Characterized on the packaged aria2: a daemon started with --ftp-pasv=false
# makes an unpinned job use active mode, which cannot cross the guard; one
# started with --ftp-type=ascii lets a converting server rewrite line endings.

PAYLOAD = b"line1\nline2\r\nline3\n\x00\xff\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("daemon_globals", [("--ftp-pasv=false",), ("--ftp-type=ascii",), ("--ftp-pasv=false", "--ftp-type=ascii")])
async def test_real_aria2_owned_ftp_job_stays_passive_and_binary_under_hostile_daemon_globals(tmp_path, monkeypatch, daemon_globals) -> None:
    from test_v113_egress_guard_route_scope import FtpOrigin
    from test_v1111_aria2_security_boundary import _stop_aria2

    origin = await FtpOrigin({"/pub/file.bin": PAYLOAD}).start()
    guard, proc, service, executor, request = await _real(tmp_path, monkeypatch, origin, daemon_globals=daemon_globals)
    try:
        handle = executor.prepare(request)
        assert (await executor.start(request, handle)).error is None
        observed = await _terminal(executor, handle)
        assert observed.state == ExecutionState.SUCCEEDED, observed.error
        assert (tmp_path / "file.bin").read_bytes() == PAYLOAD
        assert origin.data_modes and set(origin.data_modes) == {"passive"}
        assert origin.transfer_types and all(kind.startswith("I") for kind in origin.transfer_types)
        job = await service._call("aria2.getOption", [handle.native["gid"]])
        assert (job["ftp-pasv"], job["ftp-type"]) == ("true", "binary")
    finally:
        await _stop_aria2(proc, service)
        await guard.stop()
        await origin.close()


@pytest.mark.asyncio
async def test_the_test_origin_really_rewrites_ascii_transfers(tmp_path) -> None:
    """Oracle check: without the per-job pin, a hostile ascii daemon alters bytes."""
    from test_v113_egress_guard_route_scope import FtpOrigin, _aria2_ftp, _guard

    origin = await FtpOrigin({"/pub/file.bin": PAYLOAD}).start()
    guard = _guard()
    await guard.ensure_started()
    try:
        uri = f"ftp://files.test:{origin.port}/pub/file.bin"
        options = {**guard.job_options(uri, scope=RouteScope.SAME_HOST), "ftp-type": "ascii"}
        status = await _aria2_ftp(tmp_path, uri, options, "ascii.bin")
        assert status["status"] == "complete", status
        assert (tmp_path / "ascii.bin").read_bytes() != PAYLOAD
        assert origin.transfer_types == ["A"]
    finally:
        await guard.stop()
        await origin.close()

