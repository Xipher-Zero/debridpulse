"""DP 1.0.13 adverse conditions, Pass 3: portable sparse material.

DP-valid material stays DP-valid: a downstream executor's inability to consume
it is a capability limitation reported as such, never a silent redefinition of
the material as invalid. aria2 was characterized (1.37.0) to import arbitrary
whole-piece sparse material through its own control-file format with no
verification of its own, and to refuse a control file whose piece length or
total length disagrees -- so a fresh aria2 job keeps every DP-valid range
(``IMPORT_SPARSE_MATERIAL``) on exactly the trust basis the contiguous prefix
import already uses. rsync's destination-aware reconstruction stays what it
is: a bandwidth-saving reconstruction whose private work is never DP material.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from transfers import material as mat
from transfers.continuation import plan_continuation
from transfers.models import (
    ContinuationCapability, ContinuationStrategy, ExecutorCapabilities, MaterializationKind, TransferCandidate,
)

MIB = 1 << 20
SIZE = 24 * MIB + 777
# The real geometry class (transfer 437): a small prefix and large valid islands later.
SPARSE = ((0, 2 * MIB), (6 * MIB, 9 * MIB), (15 * MIB, 20 * MIB))


def _state(valid=SPARSE, *, expected=SIZE):
    return mat.MaterialState(7, 3, mat.GEOMETRY_VERSION, mat.normalize(valid), "/d/x", 2, expected)


def _candidate(size=SIZE):
    return TransferCandidate("x", (), expected_bytes=size, materialization=MaterializationKind.FILE)


def _sparse_capability():
    return getattr(ContinuationCapability, "IMPORT_SPARSE_MATERIAL", None)


def _caps(*extra, alignment=MIB):
    base = {ContinuationCapability.FULL_RESTART, ContinuationCapability.CONTIGUOUS_FROM_OFFSET,
            ContinuationCapability.IMPORT_EXISTING_MATERIAL, ContinuationCapability.EXPORT_MATERIAL_RANGES}
    return ExecutorCapabilities(continuation=frozenset({*base, *[item for item in extra if item is not None]}),
                                continuation_alignment=alignment)


# ── the one planner: four distinct material states ──────────────────────────

def test_an_executor_that_imports_sparse_material_retains_every_dp_valid_range():
    plan = plan_continuation(_state(), candidate=_candidate(), executor_id="aria2",
                             capabilities=_caps(_sparse_capability()), reason="user_candidate_switch")
    assert plan.strategy.value == "sparse_import"
    assert plan.retained == SPARSE and plan.discarded == () and plan.unusable == ()
    assert plan.retained_bytes == mat.total(SPARSE)


def test_valid_material_an_executor_cannot_consume_is_reported_as_unusable_not_silently_invalid():
    # Unknown total size: aria2 cannot import (its control file needs the total),
    # so only the prefix is usable -- and the plan says exactly why the rest goes.
    plan = plan_continuation(_state(expected=None), candidate=_candidate(0), executor_id="aria2",
                             capabilities=_caps(_sparse_capability()), reason="user_candidate_switch")
    assert plan.strategy == ContinuationStrategy.CONTIGUOUS_FROM_OFFSET and plan.retained == ((0, 2 * MIB),)
    assert plan.unusable == SPARSE[1:]
    # It is overwritten by this writer (inside its authorized region): invalidated
    # only as the explicit, reported consequence of that capability limitation.
    assert plan.discarded == SPARSE[1:]
    # Coarser import geometry: ranges that do not cover a whole piece are unusable.
    coarse = plan_continuation(_state(), candidate=_candidate(), executor_id="aria2",
                               capabilities=_caps(_sparse_capability(), alignment=4 * MIB),
                               reason="user_candidate_switch")
    assert coarse.retained == ((16 * MIB, 20 * MIB),) or coarse.retained_bytes >= 4 * MIB
    assert mat.union(coarse.retained, coarse.unusable) == SPARSE


def test_destination_aware_reconstruction_keeps_every_range_in_place():
    plan = plan_continuation(_state(), candidate=_candidate(), executor_id="rsync",
                             capabilities=_caps(ContinuationCapability.DESTINATION_AWARE_CONTINUATION),
                             reason="user_candidate_switch")
    assert plan.strategy == ContinuationStrategy.DESTINATION_AWARE
    assert plan.retained == SPARSE and plan.discarded == () and plan.unusable == ()


# ── aria2 itself, with a real daemon ────────────────────────────────────────

def _free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest_asyncio.fixture
async def aria2_daemon(tmp_path, monkeypatch):
    if shutil.which("aria2c") is None:
        pytest.skip("aria2c is required")
    import executors.aria2.executor as aria2_module
    import services.network_safety as safety
    from executors.aria2.client import Aria2Service
    from executors.aria2.executor import Aria2Configuration, Aria2Executor
    from test_v113_transport_evidence_sampling import guard_for

    async def validated(uri, **_kwargs):
        return uri
    monkeypatch.setattr(aria2_module, "validate_resolved_public_destination", validated)
    monkeypatch.setattr(safety, "validate_resolved_public_destination", validated)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    port, secret = _free_port(), "sparse-secret"
    proc = await asyncio.create_subprocess_exec(
        "aria2c", "--enable-rpc=true", "--rpc-listen-all=false", f"--rpc-listen-port={port}",
        f"--rpc-secret={secret}", f"--dir={downloads}", "--summary-interval=0", "--console-log-level=warn",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    service = Aria2Service(f"http://127.0.0.1:{port}/jsonrpc", secret, 3)
    for _ in range(200):
        try:
            await service.test()
            break
        except Exception:
            await asyncio.sleep(0.05)
    guard = guard_for()

    async def authorize(_handle, _action):
        return True
    executor = Aria2Executor(service, Aria2Configuration(str(downloads), confirmation_delay=0), authorize,
                             egress=guard)
    try:
        yield SimpleNamespace(executor=executor, downloads=downloads)
    finally:
        proc.terminate()
        await proc.wait()
        await guard.stop()


def _islands(target: Path, body: bytes, islands):
    """DP-valid islands hold the real bytes; every gap holds garbage."""
    target.write_bytes(b"\xAA" * len(body))
    with open(target, "r+b") as handle:
        for start, end in islands:
            handle.seek(start)
            handle.write(body[start:end])


def _sparse_plan(candidate_id, executor_id, size, retained):
    from transfers.models import ContinuationPlan
    strategy = getattr(ContinuationStrategy, "SPARSE_IMPORT", ContinuationStrategy.CONTIGUOUS_FROM_OFFSET)
    return ContinuationPlan(1, 1, mat.GEOMETRY_VERSION, candidate_id, executor_id, strategy, mat.contiguous_prefix(
        retained), retained, (), ((0, size),), size, "user_candidate_switch", alignment=MIB)


async def _settle(executor, handle, timeout=60):
    from transfers.models import ExecutionState
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = await executor.observe(handle)
        if observed.state not in {ExecutionState.RUNNING, ExecutionState.QUEUED}:
            return observed
        await asyncio.sleep(0.05)
    raise AssertionError("aria2 job never settled")


@pytest.mark.asyncio
async def test_a_fresh_aria2_job_imports_sparse_dp_material_over_http_byte_exact(aria2_daemon):
    from execution_requests import file_request
    from test_v113_continuation_runtime import BODY, start_origin
    from transfers.models import Endpoint, ExecutionState
    server, port, served = await start_origin(rate=64 * MIB)
    try:
        body = BODY
        islands = ((0, 2 * MIB), (6 * MIB, 9 * MIB), (15 * MIB, 20 * MIB))
        url = f"http://http-origin.test:{port}/pub/movie.bin"
        candidate = TransferCandidate("movie.bin", (Endpoint("http", url),), expected_bytes=len(body))
        target = aria2_daemon.downloads / "movie.bin"
        _islands(target, body, islands)
        base = file_request(candidate, str(target), "attempt-sparse", root=aria2_daemon.downloads)
        request = type(base)(base.work, base.attempt_id, base.paused,
                             _sparse_plan(str(candidate.id), "aria2", len(body), islands))
        handle = aria2_daemon.executor.prepare(request)
        started = await aria2_daemon.executor.start(request, handle)
        assert started.state != ExecutionState.FAILED, started.error
        done = await _settle(aria2_daemon.executor, handle)
        assert done.state == ExecutionState.SUCCEEDED, done.error
        assert hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(body).digest()
        # Only what DP did not hold was fetched: no ranged request re-read any
        # byte of a DP-valid island (aria2's first, unranged probe aside).
        fetched = mat.normalize((item.start, item.start + item.sent) for item in served if item.ranged)
        assert mat.intersect(fetched, islands) == ()
        assert mat.union(fetched, islands) == ((0, len(body)),) or mat.total(fetched) <= len(body) - mat.total(islands)
    finally:
        server.close()


@pytest.mark.asyncio
async def test_a_sparse_import_whose_retained_ranges_are_not_physically_present_fails_closed(aria2_daemon):
    from execution_requests import file_request
    from transfers.models import Endpoint, ExecutionState
    body = os.urandom(8 * MIB)
    candidate = TransferCandidate("gone.bin", (Endpoint("http", "http://http-origin.test:9/gone.bin"),),
                                  expected_bytes=len(body))
    target = aria2_daemon.downloads / "gone.bin"
    target.write_bytes(body[:3 * MIB])  # the island at [5, 7) MiB is not there
    base = file_request(candidate, str(target), "attempt-gone", root=aria2_daemon.downloads)
    request = type(base)(base.work, base.attempt_id, base.paused,
                         _sparse_plan(str(candidate.id), "aria2", len(body), ((0, 2 * MIB), (5 * MIB, 7 * MIB))))
    handle = aria2_daemon.executor.prepare(request)
    observed = await aria2_daemon.executor.start(request, handle)
    assert observed.state == ExecutionState.FAILED
    assert target.read_bytes() == body[:3 * MIB]  # nothing was touched
    assert not Path(str(target) + ".aria2").exists()


@pytest.mark.asyncio
async def test_a_fresh_aria2_job_imports_sparse_dp_material_over_sftp(aria2_daemon, tmp_path):
    from execution_requests import file_request
    from test_v113_transport_evidence_sampling import PASSWORD as SFTP_PASSWORD, USER as SFTP_USER, SftpOrigin
    from transfers.input_required import SubmittedInput
    from transfers.models import Endpoint, ExecutionState, InputFact, InputFactName, InputField, InputMethod
    body = os.urandom(10 * MIB + 333)
    root = tmp_path / "sftp-root"
    (root / "data").mkdir(parents=True)
    (root / "data" / "object.bin").write_bytes(body)
    origin = await SftpOrigin(root).start(algorithms=("ecdsa-sha2-nistp256",))
    try:
        url = origin.url("/data/object.bin")
        candidate = TransferCandidate("object.bin", (Endpoint("sftp", url),), expected_bytes=len(body),
                                      accepted_input_methods=(InputMethod.USERNAME_PASSWORD,))
        islands = ((0, MIB), (4 * MIB, 7 * MIB))
        target = aria2_daemon.downloads / "object.bin"
        _islands(target, body, islands)
        base = file_request(candidate, str(target), "attempt-sftp", root=aria2_daemon.downloads)
        request = type(base)(base.work, base.attempt_id, base.paused,
                             _sparse_plan(str(candidate.id), "aria2", len(body), islands))
        facts = (InputFact(InputFactName.SERVER_HOST, "sftp-origin.test"),
                 InputFact(InputFactName.SERVER_IDENTITY_ALGORITHM, "sha-1"),
                 InputFact(InputFactName.SERVER_IDENTITY_FINGERPRINT, origin.fingerprint("ecdsa-sha2-nistp256")))
        submitted = SubmittedInput("c", 1, InputMethod.USERNAME_PASSWORD,
                                   {InputField.USERNAME: SFTP_USER, InputField.PASSWORD: SFTP_PASSWORD}, facts)
        handle = aria2_daemon.executor.prepare(request)
        started = await aria2_daemon.executor.start_with_input(request, handle, submitted)
        assert started.state != ExecutionState.FAILED, started.error
        done = await _settle(aria2_daemon.executor, handle)
        assert done.state == ExecutionState.SUCCEEDED, done.error
        assert hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(body).digest()
        assert sum(origin.read_bytes) < len(body) - mat.total(islands) + 2 * MIB
    finally:
        await origin.close()


# ── the real round trip: HTTP (sparse aria2) -> rsync -> HTTP ───────────────

@pytest.mark.asyncio
async def test_http_rsync_http_round_trip_keeps_every_dp_valid_range_and_switches_in_seconds(tmp_path, monkeypatch):
    from test_v113_rsync_destination_aware_runtime import _switched_to_rsync
    from transfers.manual_failover import manual_candidate_failover, preview_candidate_switch
    from test_v113_continuation_runtime import BODY
    runtime, transfer, artifact, paused, _preview, _spawned = await _switched_to_rsync(tmp_path, monkeypatch)
    try:
        before = await runtime.repository.material_state(artifact.id)
        assert before.valid == paused.valid  # rsync's private reconstruction is not DP material
        http = next(item for item in artifact.candidates if item.endpoints[0].scheme == "http")
        back = await preview_candidate_switch(runtime.engine, transfer.id, artifact.id, str(http.id))
        # Nothing DP holds valid is lost for switching away from rsync; what is
        # ended is rsync's unverified private reconstruction, stated as such.
        assert back["discarded_bytes"] == 0 and back["retained_bytes"] == before.valid_bytes
        assert back["unusable_bytes"] == 0 and back["abandoned_bytes"] > 0
        started = time.monotonic()
        result = await manual_candidate_failover(runtime.engine, transfer.id, artifact.id, str(http.id))
        switched_in = time.monotonic() - started
        assert not result.get("confirmation_required"), result
        # Interactive: planning and handoff never wait for rsync's reconstruction.
        assert switched_in < 10.0

        async def writing():
            artifacts = await runtime.repository.artifacts(transfer.id)
            current = artifacts[0] if artifacts else None
            return current if current is not None and current.execution is not None and \
                current.execution.executor_id == "aria2" else None
        await runtime.until(writing, label="aria2 writing again")
        after = await runtime.repository.material_state(artifact.id)
        assert mat.subtract(before.valid, after.valid) == ()  # zero valid-range loss
        assert after.material_generation == before.material_generation  # no invalidation happened
        plan = next(plan for executor, _state, plan in reversed(await runtime.attempts(transfer.id))
                    if executor == "aria2")
        assert plan["strategy"] == "sparse_import"
        assert [tuple(item) for item in plan["retained"]] == list(before.valid) and plan["discarded"] == []
        await runtime.until(lambda: runtime.completed(transfer.id), label="completed from DP material")
        (final,) = await runtime.repository.artifacts(transfer.id)
        assert Path(final.target).read_bytes() == BODY
        writers = [executor for executor, _state, _plan in await runtime.attempts(transfer.id)]
        assert writers.count("rsync") == 1  # one writer at a time; never two
    finally:
        await runtime.close()
