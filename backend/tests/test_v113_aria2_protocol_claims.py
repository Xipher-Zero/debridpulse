"""1.0.13 first pass: truthful aria2 transport claims through existing machinery.

The aria2 executor's endpoint-scheme claim is a *positive allowlist*. Expanding
it to FTP and SFTP must be understood by the Universal Transfer Core through the
registry it already owns -- not because the core learned anything about FTP or
SFTP. These tests pin that boundary from both sides: the claim is exactly the
four transports, and every hardened invariant that guarded HTTP(S) still guards
the new schemes through the same single validator, guard, and executor path.
"""
from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import services.artifact_sampling as sampling
import services.network_safety as safety
import executors.aria2.executor as executor_module
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from services.downloader_egress_guard import DownloaderEgressGuard
from execution_requests import file_request
from transfers.errors import Category, TransferError
from transfers.models import (
    Endpoint, ExecutionRequest, TransferCandidate, new_identity,
)
from transfers.registry import IntegrationRegistry
from transfers.models import ExecutionSubject


CLAIMED = ("http", "https", "ftp", "sftp")
UNCLAIMED = ("scp", "rsync", "ftps", "webdav", "webdavs", "metalink", "magnet", "file")


def _candidate(*schemes: str) -> TransferCandidate:
    return TransferCandidate(
        "payload.bin",
        tuple(Endpoint(scheme, f"{scheme}://provider.example/payload.bin") for scheme in schemes),
    )


def _executor(tmp_path: Path, *, egress=None) -> Aria2Executor:
    return Aria2Executor(
        None,
        Aria2Configuration(str(tmp_path)),
        AsyncMock(return_value=True),
        egress=egress or SimpleNamespace(ensure_started=AsyncMock(), job_options=lambda address, scope=None: {}),
    )


# ── 1. Descriptor truth ───────────────────────────────────────────────────────

def test_descriptor_claims_exactly_the_four_supported_transports() -> None:
    assert executor_module.SUPPORTED_SCHEMES == frozenset({"http", "https", "ftp", "sftp"})
    assert "schemes" not in vars(Aria2Executor.descriptor)


def test_descriptor_claim_is_a_positive_allowlist_with_no_negative_list() -> None:
    """Unsupported is expressed by absence; no executor carries a denial list."""
    descriptor = Aria2Executor.descriptor
    assert not any(
        "unsupported" in name or "denied" in name or "excluded" in name
        for name in vars(descriptor)
    )
    for scheme in UNCLAIMED:
        assert scheme not in executor_module.SUPPORTED_SCHEMES


# ── 2. Registry truth (no scheme-specific core policy) ────────────────────────

@pytest.mark.parametrize("scheme", CLAIMED)
def test_registry_selects_aria2_for_every_claimed_scheme(tmp_path, scheme) -> None:
    registry = IntegrationRegistry()
    executor = _executor(tmp_path)
    registry.register_executor(executor)
    subject = ExecutionSubject.of(_candidate(scheme))
    assert registry.claimants(subject) == (executor,)
    assert registry.executor_for_subject(subject) is executor


@pytest.mark.parametrize("scheme", UNCLAIMED)
def test_unclaimed_schemes_simply_have_no_eligible_executor(tmp_path, scheme) -> None:
    registry = IntegrationRegistry()
    registry.register_executor(_executor(tmp_path))
    subject = ExecutionSubject.of(_candidate(scheme))
    assert registry.claimants(subject) == ()
    with pytest.raises(TransferError) as raised:
        registry.executor_for_subject(subject)
    assert raised.value.error.category == Category.UNSUPPORTED_CAPABILITY


def test_registry_carries_no_scheme_specific_policy() -> None:
    """Selection is a claim over the subject; the core names no transport."""
    source = (Path(safety.__file__).resolve().parents[1] / "transfers" / "registry.py").read_text()
    lowered = source.lower()
    for scheme in ("ftp", "sftp", "http"):
        assert f'"{scheme}"' not in lowered and f"'{scheme}'" not in lowered


# ── 3. Torrent / magnet / Metalink remain outside the executor contract ───────

def test_executor_claims_no_torrent_magnet_or_metalink_execution() -> None:
    descriptor = Aria2Executor.descriptor
    assert "magnet" not in executor_module.SUPPORTED_SCHEMES
    assert "metalink" not in executor_module.SUPPORTED_SCHEMES
    assert descriptor.request_types == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", CLAIMED)
async def test_metadata_following_stays_disabled_for_every_claimed_scheme(
    tmp_path, monkeypatch, scheme
) -> None:
    async def validated(uri, **_kwargs):
        return uri

    monkeypatch.setattr(executor_module, "validate_resolved_public_destination", validated)
    executor = _executor(tmp_path)
    request = file_request(_candidate(scheme), str(tmp_path / "payload.bin"), new_identity())
    _address, options = await executor._options(request, executor.prepare(request))
    assert options["follow-torrent"] == "false"
    assert options["follow-metalink"] == "false"
    assert options["no-netrc"] == "true"
    assert options["max-tries"] == "1"


# ── 4. One neutral CandidateSampling; transport I/O below it ─────────────────
# 1.0.13 universal evidence acquisition reversed the first pass's "FTP/SFTP have
# no executor sample" expectation (Transfer 312): every claimed transport now
# yields the same neutral evidence through one capability, while the HTTP(S)
# Range sampler itself still speaks only HTTP(S).

def test_sampler_declares_the_schemes_it_can_actually_sample() -> None:
    assert sampling.SAMPLED_FINGERPRINT_SCHEMES == frozenset({"http", "https"})


def test_aria2_exposes_one_neutral_sampling_capability_and_its_continuation() -> None:
    from transfers.contracts import CandidateSampling, CandidateSamplingContinuation
    executor = _executor(Path("/tmp"))
    assert isinstance(executor, CandidateSampling)
    assert isinstance(executor, CandidateSamplingContinuation)


def _transport_samplers(monkeypatch):
    calls = []

    async def http(address, **kwargs):
        calls.append(("http", address))
        return (10, "sig", sampling.FingerprintKind.FULL_CONTENT_SAMPLE, "", "sig")

    async def ftp(address, **kwargs):
        calls.append(("ftp", address))
        return (10, "sig", sampling.FingerprintKind.FULL_CONTENT_SAMPLE, "", "sig")

    async def sftp(address, **kwargs):
        calls.append(("sftp", address))
        from services.artifact_sampling import AccessRequired
        return AccessRequired("ab" * 20)

    async def validated(uri, **_kwargs):
        return uri

    monkeypatch.setattr(executor_module, "sampled_public_artifact_fingerprint", http)
    monkeypatch.setattr(executor_module, "ftp_fingerprint", ftp)
    monkeypatch.setattr(executor_module, "sftp_fingerprint", sftp)
    monkeypatch.setattr(executor_module, "validate_resolved_public_destination", validated)
    return calls


@pytest.mark.asyncio
async def test_every_claimed_transport_is_sampled_through_its_own_evidence_reader(tmp_path, monkeypatch) -> None:
    from transfers.models import InputMethod, InputReason, InputRequirement
    calls = _transport_samplers(monkeypatch)
    executor = _executor(tmp_path)
    for scheme in ("http", "https", "ftp"):
        result = await executor.fingerprint(ExecutionSubject.of(_candidate(scheme)))
        assert result is not None and result.total_bytes == 10
    sftp = TransferCandidate("payload.bin", (Endpoint("sftp", "sftp://provider.example/payload.bin"),),
                             accepted_input_methods=(InputMethod.USERNAME_PASSWORD,))
    requirement = await executor.fingerprint(ExecutionSubject.of(sftp))
    assert isinstance(requirement, InputRequirement) and requirement.reason == InputReason.SERVER_IDENTITY_REQUIRED
    assert [kind for kind, _address in calls] == ["http", "http", "ftp", "sftp"]  # FTP/SFTP never reach the HTTP sampler


@pytest.mark.asyncio
async def test_a_mixed_candidate_samples_the_endpoint_execution_uses(tmp_path, monkeypatch) -> None:
    calls = _transport_samplers(monkeypatch)
    assert await _executor(tmp_path).fingerprint(ExecutionSubject.of(_candidate("ftp", "https"))) is not None
    assert calls == [("ftp", "ftp://provider.example/payload.bin")]


def test_core_names_no_transport_and_holds_no_sampling_scheme_list() -> None:
    """Fingerprintability is a capability of the executor boundary; no universal
    core module lists sampleable schemes or imports a transport reader."""
    transfers = Path(safety.__file__).resolve().parents[1] / "transfers"
    # Request-kind validation (requests.py) and request-kind presentation
    # (presentation_repository.py) legitimately name submitted schemes; every
    # equivalence, cohort, canonical, recovery, lifecycle and challenge owner
    # must not.
    for path in sorted(transfers.glob("*.py")):
        if path.name in {"requests.py", "presentation_repository.py"}:
            continue
        text = path.read_text()
        lowered = text.lower()
        for scheme in ("ftp", "sftp", "http", "https"):
            assert f'"{scheme}"' not in lowered and f"'{scheme}'" not in lowered, (path.name, scheme)
        assert "SAMPLED_FINGERPRINT_SCHEMES" not in text and "artifact_sampling" not in text, path.name


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ("ftp", "sftp"))
async def test_the_sampler_itself_fails_closed_on_an_unsampleable_scheme(scheme) -> None:
    total, signature, kind, reason, _prefix = await sampling.sampled_public_artifact_fingerprint(
        f"{scheme}://provider.example/payload.bin"
    )
    assert (total, signature, kind) == (0, "", sampling.FingerprintKind.UNAVAILABLE)
    assert reason == "destination_rejected"


# ── 5. One generalized destination validator ──────────────────────────────────

def test_one_validator_owns_both_scheme_sets() -> None:
    assert safety.PROVIDER_LINK_SCHEMES == frozenset({"http", "https"})
    assert safety.PUBLIC_DESTINATION_SCHEMES == frozenset({"http", "https", "ftp", "sftp"})
    assert safety.PROVIDER_LINK_SCHEMES < safety.PUBLIC_DESTINATION_SCHEMES


def test_guarded_transport_set_matches_the_executor_claim() -> None:
    """The guarded-transport set and the executor claim may never drift apart."""
    assert safety.PUBLIC_DESTINATION_SCHEMES == executor_module.SUPPORTED_SCHEMES


@pytest.mark.parametrize("scheme", CLAIMED)
def test_validator_accepts_every_claimed_public_transport(scheme) -> None:
    url = f"{scheme}://provider.example/payload.bin"
    assert safety.validate_provider_download_url(
        url, schemes=safety.PUBLIC_DESTINATION_SCHEMES
    ) == url


def test_provider_link_boundary_still_rejects_the_new_transports() -> None:
    """Expanding executor transports must not widen the provider link contract."""
    for scheme in ("ftp", "sftp"):
        with pytest.raises(safety.UnsafeDestinationError):
            safety.validate_provider_download_url(f"{scheme}://provider.example/f.bin")


@pytest.mark.parametrize("scheme", UNCLAIMED)
def test_validator_still_rejects_unclaimed_schemes(scheme) -> None:
    with pytest.raises(safety.UnsafeDestinationError):
        safety.validate_provider_download_url(
            f"{scheme}://provider.example/f.bin", schemes=safety.PUBLIC_DESTINATION_SCHEMES
        )


@pytest.mark.parametrize("scheme", ("ftp", "sftp"))
@pytest.mark.parametrize("suffix", [
    "://",                                  # hostname required
    "://provider.example:0/f.bin",          # malformed port
    "://provider.example:99999/f.bin",      # malformed port
    "://user:pass@provider.example/f.bin",  # embedded credentials
    "://localhost/f.bin",
    "://host.local/f.bin",
    "://127.0.0.1/f.bin",
    "://10.0.0.1/f.bin",
    "://169.254.169.254/f.bin",
    "://[::1]/f.bin",
    "://2130706433/f.bin",
])
def test_every_hardened_url_invariant_survives_on_the_new_transports(scheme, suffix) -> None:
    with pytest.raises(safety.UnsafeDestinationError):
        safety.validate_provider_download_url(
            scheme + suffix, schemes=safety.PUBLIC_DESTINATION_SCHEMES
        )


def test_validator_performs_no_destructive_url_rewriting() -> None:
    url = "ftp://provider.example:2121/deep/path/payload.bin?token=opaque#frag"
    assert safety.validate_provider_download_url(
        url, schemes=safety.PUBLIC_DESTINATION_SCHEMES
    ) == url


def test_one_canonical_default_port_table() -> None:
    assert safety.DEFAULT_DESTINATION_PORTS == {"http": 80, "https": 443, "ftp": 21, "sftp": 22}
    for scheme, port in safety.DEFAULT_DESTINATION_PORTS.items():
        assert safety.default_destination_port(scheme) == port


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme,port", [("http", 80), ("https", 443), ("ftp", 21), ("sftp", 22)])
async def test_resolution_uses_the_correct_per_scheme_default_port(monkeypatch, scheme, port) -> None:
    seen = []

    async def getaddrinfo(host, service, **kwargs):
        seen.append((host, service))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", service))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    url = f"{scheme}://provider.example/payload.bin"
    assert await safety.validate_resolved_public_destination(url) == url
    assert seen == [("provider.example", port)]


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ("ftp", "sftp"))
async def test_dns_answer_sets_containing_a_private_address_are_blocked(monkeypatch, scheme) -> None:
    async def getaddrinfo(host, service, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", service)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", service)),
        ]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    with pytest.raises(safety.UnsafeDestinationError):
        await safety.validate_resolved_public_destination(f"{scheme}://provider.example/f.bin")


@pytest.mark.asyncio
async def test_http_redirect_targets_are_still_confined_to_provider_link_schemes() -> None:
    """A redirect must never escalate an HTTP download into another transport."""
    with pytest.raises(safety.UnsafeDestinationError):
        safety.validate_provider_download_url("ftp://provider.example/f.bin", context="redirect target")


# ── 6. One egress guard covering the new transports ───────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("scheme,port", [("http", 80), ("https", 443), ("ftp", 21), ("sftp", 22)])
async def test_guard_scopes_its_credential_to_the_correct_default_port(scheme, port) -> None:
    guard = DownloaderEgressGuard(bind_port=0)
    await guard.ensure_started()
    try:
        options = guard.job_options(f"{scheme}://provider.example/f.bin")
        assert options["all-proxy-passwd"] == guard._token("provider.example", port)
    finally:
        await guard.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", CLAIMED)
async def test_guard_forecloses_every_per_protocol_proxy_override(scheme) -> None:
    """aria2 lets --ftp-proxy override --all-proxy, so every per-protocol proxy
    preference a daemon could carry globally must be pinned at the guard."""
    guard = DownloaderEgressGuard(bind_port=0)
    await guard.ensure_started()
    try:
        proxy = f"http://127.0.0.1:{guard.bound_port}"
        options = guard.job_options(f"{scheme}://provider.example/f.bin")
        assert options["all-proxy"] == proxy
        for family in ("all", "http", "https", "ftp"):
            assert options[f"{family}-proxy"] == proxy
            assert options[f"{family}-proxy-user"] == "debridpulse"
            assert options[f"{family}-proxy-passwd"] == options["all-proxy-passwd"]
        assert options["no-proxy"] == ""
        assert options["proxy-method"] == "tunnel"
    finally:
        await guard.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ("ftp", "sftp"))
async def test_guard_refuses_a_non_public_target_on_the_new_transports(scheme) -> None:
    guard = DownloaderEgressGuard(bind_port=0)
    await guard.ensure_started()
    try:
        with pytest.raises(safety.UnsafeDestinationError):
            guard.job_options(f"{scheme}://127.0.0.1/f.bin")
    finally:
        await guard.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ("ftp", "sftp"))
async def test_the_new_transports_cannot_bypass_the_guard(tmp_path, monkeypatch, scheme) -> None:
    """No FTP/SFTP job may ever be submitted without guard options attached."""
    async def validated(uri, **_kwargs):
        return uri

    monkeypatch.setattr(executor_module, "validate_resolved_public_destination", validated)
    guard = DownloaderEgressGuard(bind_port=0)
    await guard.ensure_started()
    try:
        executor = _executor(tmp_path, egress=guard)
        request = file_request(_candidate(scheme), str(tmp_path / "payload.bin"), new_identity())
        _address, options = await executor._options(request, executor.prepare(request))
        assert options["all-proxy"] == f"http://127.0.0.1:{guard.bound_port}"
        assert options["ftp-proxy"] == options["all-proxy"]
        assert options["no-proxy"] == ""
        assert options["proxy-method"] == "tunnel"
    finally:
        await guard.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ("ftp", "sftp"))
async def test_a_blocked_new_transport_destination_is_a_security_failure(tmp_path, scheme) -> None:
    executor = _executor(tmp_path)
    request = file_request(
        TransferCandidate("payload.bin", (Endpoint(scheme, f"{scheme}://127.0.0.1/payload.bin"),)),
        str(tmp_path / "payload.bin"),
        new_identity(),
    )
    with pytest.raises(TransferError) as raised:
        await executor._options(request, executor.prepare(request))
    assert raised.value.error.category == Category.DESTINATION_BLOCKED


# ── 7. Authentication boundary stays at the native adapter ────────────────────
# The FTP & SFTP provider pass made FTP/SFTP authentication and host identity
# live (test_v113_aria2_ftp_sftp_auth.py). What remains pinned here: HTTP code
# 24 is unchanged, and native credential/host-key option names exist in
# production code only at the aria2 boundary.

def test_native_auth_option_names_are_confined_to_the_aria2_boundary() -> None:
    source = Path(executor_module.__file__).read_text()
    assert '"24"' in source
    backend = Path(executor_module.__file__).resolve().parents[2]
    for path in backend.rglob("*.py"):
        if "tests" in path.parts or path.parent.name == "aria2":
            continue
        text = path.read_text()
        for native in ("ssh-host-key-md", "ftp-user", "ftp-passwd", "ftp-reuse-connection"):
            assert native not in text, (path, native)
