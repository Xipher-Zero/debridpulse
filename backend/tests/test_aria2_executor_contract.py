"""Exercise the execution boundary, including ambiguous RPC outcomes and ownership."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from executors.aria2.client import Aria2ConnectionError, Aria2DownloadStatus, Aria2RPCError, Aria2Service
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from executors.aria2.translation import native_failure, observation
from transfers.errors import Category, Domain, Recovery, Retryability, TransferError
from transfers.models import (
    Endpoint, ExecutionHandle, ExecutionRequest, ExecutionState, OutcomeKind,
    TransferCandidate,
)
from transfers.registry import IntegrationRegistry


class NativeDaemon:
    def __init__(self):
        self.jobs = {}
        self.calls = []
        self.lookups = 0
        self.lost_ack = False
        self.ignore_pause = False

    async def tell_status(self, gid):
        self.lookups += 1
        if gid not in self.jobs:
            raise Aria2RPCError(f"aria2 [1]: GID {gid} is not found", code=1)
        return self.jobs[gid]

    async def _call(self, method, params):
        self.calls.append((method, params))
        if method == "aria2.addUri":
            options = params[1]
            gid = options["gid"]
            self.jobs[gid] = Aria2DownloadStatus(
                gid, "paused" if options["pause"] == "true" else "active", 100, 20, 2,
                files=[{"path": str(Path(options["dir"]) / options["out"])}],
            )
            if self.lost_ack:
                raise Aria2ConnectionError("connection lost after acceptance")
            return gid
        gid = params[0]
        if method == "aria2.forcePause" and not self.ignore_pause:
            self.jobs[gid].status = "paused"
        elif method == "aria2.unpause":
            self.jobs[gid].status = "active"
        elif method == "aria2.forceRemove":
            self.jobs[gid].status = "removed"
        elif method == "aria2.removeDownloadResult":
            del self.jobs[gid]
        return gid


@pytest.fixture
def execution(tmp_path, monkeypatch):
    async def validated(address):
        return address
    monkeypatch.setattr("executors.aria2.executor.validate_resolved_public_destination", validated)
    daemon = NativeDaemon()
    grants = {}

    async def authorize(handle, action):
        return grants.get(handle.attempt_id) == handle

    egress = SimpleNamespace(ensure_started=AsyncMock(), job_options=lambda address, external: {"all-proxy": "http://guard:8888"})
    executor = Aria2Executor(daemon, Aria2Configuration(str(tmp_path), confirmation_delay=0), authorize, egress=egress)
    candidate = TransferCandidate("payload", (Endpoint("https", "https://download.example/file?s=secret", {"X-Capability": "opaque-header-value"}),), expected_bytes=100)
    request = ExecutionRequest(candidate, str(tmp_path / "file"), "durable-attempt")
    handle = executor.prepare(request)
    grants[handle.attempt_id] = handle
    return SimpleNamespace(executor=executor, daemon=daemon, request=request, handle=handle, grants=grants, egress=egress)


def test_executor_is_registered_by_contract_and_scheme(execution):
    registry = IntegrationRegistry()
    registry.register_executor(execution.executor)
    assert registry.executor_for(execution.request.candidate) is execution.executor
    assert execution.executor.prepare(execution.request) == execution.handle
    assert "secret" not in repr(execution.handle)
    assert not execution.daemon.calls


@pytest.mark.asyncio
async def test_start_preserves_connection_guard_and_metadata_safety(execution):
    state = await execution.executor.start(execution.request, execution.handle)
    assert state.state == ExecutionState.QUEUED
    options = execution.daemon.calls[0][1][1]
    assert options["gid"] == execution.handle.context["gid"]
    assert options["all-proxy"] == "http://guard:8888"
    assert options["follow-torrent"] == options["follow-metalink"] == "false"
    assert options["max-http-redirection"] == "0"
    assert options["check-certificate"] == "true"
    assert options["auto-file-renaming"] == "false"
    assert (await execution.executor.observe(execution.handle)).state == ExecutionState.TRANSFERRING


@pytest.mark.asyncio
async def test_lost_ack_is_reconciled_without_second_job(execution):
    execution.daemon.lost_ack = True
    uncertain = await execution.executor.start(execution.request, execution.handle)
    assert uncertain.state == ExecutionState.UNKNOWN
    assert uncertain.error.recovery == Recovery.RECONCILE
    recovered = await execution.executor.observe(execution.handle)
    assert recovered.state == ExecutionState.TRANSFERRING
    assert recovered.handle.attempt_id == execution.request.attempt_id
    assert len(execution.daemon.calls) == 1


@pytest.mark.asyncio
async def test_collision_never_adopts_or_mutates_preexisting_job(execution):
    gid = execution.handle.context["gid"]
    execution.daemon.jobs[gid] = Aria2DownloadStatus(gid, "active", 100, 30, 0)
    result = await execution.executor.start(execution.request, execution.handle)
    assert result.error.category == Category.OWNERSHIP_CONFLICT
    assert execution.daemon.calls == []


@pytest.mark.asyncio
async def test_unowned_handles_are_rejected_before_native_io(execution):
    execution.grants.clear()
    started = await execution.executor.start(execution.request, execution.handle)
    observed = await execution.executor.observe(execution.handle)
    cancelled = await execution.executor.cancel(execution.handle)
    for result in (started, observed, cancelled):
        assert result.error.category == Category.OWNERSHIP_CONFLICT
    assert execution.daemon.lookups == 0
    assert execution.daemon.calls == []


@pytest.mark.asyncio
async def test_absence_requires_repeated_explicit_missing_responses(execution):
    result = await execution.executor.observe(execution.handle)
    assert result.state == ExecutionState.ABSENT
    assert result.error is None
    assert execution.daemon.lookups == 3


@pytest.mark.asyncio
async def test_transport_failure_during_confirmation_preserves_uncertainty(execution):
    execution.daemon.tell_status = AsyncMock(side_effect=[
        Aria2RPCError(f"GID {execution.handle.context['gid']} is not found"),
        Aria2ConnectionError("unavailable"),
    ])
    result = await execution.executor.observe(execution.handle)
    assert result.state == ExecutionState.UNKNOWN
    assert result.error.category == Category.EXECUTOR_UNAVAILABLE


@pytest.mark.asyncio
async def test_rpc_rejection_is_not_evidence_of_absence(execution):
    execution.daemon.tell_status = AsyncMock(side_effect=Aria2RPCError("authorization failed", code=1))
    result = await execution.executor.observe(execution.handle)
    assert result.state == ExecutionState.UNKNOWN
    assert result.error.retryability == Retryability.UNKNOWN


@pytest.mark.asyncio
async def test_pause_resume_and_cancel_confirm_native_state(execution):
    await execution.executor.start(execution.request, execution.handle)
    assert (await execution.executor.pause(execution.handle)).state == ExecutionState.PAUSED
    assert (await execution.executor.resume(execution.handle)).state == ExecutionState.TRANSFERRING
    result = await execution.executor.cancel(execution.handle)
    assert result.kind == OutcomeKind.CANCELLED
    assert execution.daemon.jobs[execution.handle.context["gid"]].status == "removed"
    assert all(method not in {"aria2.removeDownloadResult", "aria2.purgeDownloadResult", "aria2.changeGlobalOption"} for method, _ in execution.daemon.calls)


@pytest.mark.asyncio
async def test_pause_ack_without_state_change_is_not_success(execution):
    await execution.executor.start(execution.request, execution.handle)
    execution.daemon.ignore_pause = True
    result = await execution.executor.pause(execution.handle)
    assert result.state == ExecutionState.UNKNOWN
    assert result.error.category == Category.RECONCILIATION_FAILED


@pytest.mark.asyncio
async def test_guard_failure_never_dispatches(execution):
    execution.egress.ensure_started.side_effect = RuntimeError("guard unavailable")
    result = await execution.executor.start(execution.request, execution.handle)
    assert result.error.domain == Domain.SECURITY
    assert result.error.retryability == Retryability.NEVER
    assert execution.daemon.calls == []


@pytest.mark.asyncio
async def test_observation_diagnostics_redact_candidate_header_values(execution):
    await execution.executor.start(execution.request, execution.handle)
    native = execution.daemon.jobs[execution.handle.context["gid"]]
    native.status = "error"
    native.error_code = "987654"
    native.error_message = "unexpected opaque-header-value https://download.example/file?s=secret"
    result = await execution.executor.observe(execution.handle)
    assert result.error.category == Category.UNMAPPED_EXECUTOR_ERROR
    assert result.error.retryability == Retryability.UNKNOWN
    assert "opaque-header-value" not in str(result.error.as_dict(diagnostics=True))
    assert "download.example" not in str(result.error.as_dict(diagnostics=True))


def test_path_escape_is_security_failure(execution, tmp_path):
    outside = tmp_path.parent / "outside"
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    request = replace(execution.request, target=str(tmp_path / "escape" / "file"))
    with pytest.raises(TransferError) as failure:
        execution.executor.prepare(request)
    assert failure.value.error.category == Category.PATH_POLICY_VIOLATION


@pytest.mark.parametrize("code,category,retryability", [
    ("9", Category.DISK_FULL, Retryability.AFTER_RESOURCE_CHANGE),
    ("19", Category.DNS_FAILURE, Retryability.BACKOFF),
    ("24", Category.AUTHENTICATION_FAILED, Retryability.AFTER_REAUTH),
    ("23", Category.UNSAFE_REDIRECT, Retryability.NEVER),
    ("32", Category.CHECKSUM_MISMATCH, Retryability.AFTER_RERESOLUTION),
    ("1", Category.UNMAPPED_EXECUTOR_ERROR, Retryability.UNKNOWN),
    ("500000", Category.UNMAPPED_EXECUTOR_ERROR, Retryability.UNKNOWN),
])
def test_error_translation_has_explicit_recovery_semantics(code, category, retryability):
    error = native_failure(code)
    assert error.category == category
    assert error.retryability == retryability


def test_removed_and_unknown_are_not_ordinary_failures():
    handle = ExecutionHandle("aria2", {"gid": "0123456789abcdef"})
    native = Aria2DownloadStatus("0123456789abcdef", "removed", 0, 0, 0)
    result = observation(handle, native)
    assert result.state == ExecutionState.CANCELLED
    assert result.error is None
    native.status = "new-future-native-state"
    result = observation(handle, native)
    assert result.state == ExecutionState.UNKNOWN
    assert result.error.retryability == Retryability.UNKNOWN


@pytest.mark.asyncio
async def test_bulk_observation_filters_foreign_jobs_and_confirms_missing_handles(execution):
    daemon = execution.daemon
    daemon._keys = Aria2Service._keys
    daemon._normalize = Aria2Service._normalize.__get__(daemon)
    daemon._multicall = AsyncMock(return_value=[[{
        "gid": "foreign-native-id", "status": "active", "files": [{"path": "/foreign/file"}],
    }], [], []])
    snapshot = await execution.executor.observe_many((execution.handle,))
    assert snapshot.error is None
    assert len(snapshot.observations) == 1
    assert snapshot.observations[0].state == ExecutionState.ABSENT
    assert daemon.lookups == 3
    assert daemon._multicall.await_count == 1
    assert not daemon.calls


@pytest.mark.asyncio
async def test_bulk_failure_remains_unknown_without_per_job_retry_storm(execution):
    daemon = execution.daemon
    daemon._keys = Aria2Service._keys
    daemon._multicall = AsyncMock(side_effect=Aria2ConnectionError("daemon unavailable"))
    snapshot = await execution.executor.observe_many((execution.handle,))
    assert snapshot.observations == ()
    assert snapshot.error.category == Category.EXECUTOR_UNAVAILABLE
    assert daemon.lookups == 0


@pytest.mark.asyncio
async def test_private_literal_is_explicit_nonretryable_security_failure(execution, monkeypatch):
    from services.network_safety import validate_resolved_public_destination
    monkeypatch.setattr("executors.aria2.executor.validate_resolved_public_destination", validate_resolved_public_destination)
    request = replace(execution.request, candidate=replace(execution.request.candidate, endpoints=(Endpoint("http", "http://127.0.0.1/file"),)))
    handle = execution.executor.prepare(request)
    execution.grants[handle.attempt_id] = handle
    result = await execution.executor.start(request, handle)
    assert result.error.category == Category.DESTINATION_BLOCKED
    assert result.error.retryability == Retryability.NEVER
    assert not execution.daemon.calls


@pytest.mark.asyncio
async def test_sampling_resolver_rejects_changed_private_answer_at_connection(monkeypatch):
    import asyncio
    import socket
    from services.network_safety import PublicDestinationResolver, UnsafeDestinationError
    loop = asyncio.get_running_loop()
    answers = AsyncMock(side_effect=[
        [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))],
        [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))],
    ])
    monkeypatch.setattr(loop, "getaddrinfo", answers)
    resolver = PublicDestinationResolver()
    public = await resolver.resolve("changing.example", 443)
    assert public[0]["host"] == "8.8.8.8"
    assert public[0]["hostname"] == "changing.example"
    with pytest.raises(UnsafeDestinationError):
        await resolver.resolve("changing.example", 443)
