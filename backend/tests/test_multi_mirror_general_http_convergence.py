"""DP 1.0.12 Section 11/12/13 real-runtime proof.

Deterministic local fixture exercising the *real* General HTTP resolution +
bounded-sampler path (``services.network_safety.sampled_public_artifact_fingerprint``
through the real ``executors.aria2.executor.Aria2Executor.fingerprint``), never a
fake "equivalent=True" hook. Mirrors are distinguished by loopback host address
(``127.0.0.<n>``) rather than port, because ``GeneralHttpProvider`` derives
``SourceIdentity`` from hostname only (``providers/general_http/provider.py``),
so same-host-different-port URLs would collide on source independence.

Requires a real ``aria2c`` binary (skips otherwise, matching the established
convention in ``test_general_http_stage5_runtime.py``).
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import shutil
import socket
from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiohttp import web

import db.database as database
import executors.aria2.executor as aria2_executor_module
import services.network_safety as network_safety
from executors.aria2.client import Aria2Service
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from fake_integrations import MemoryExecutor, ParcelProvider
from providers.general_http.provider import GeneralHttpProvider
from transfers.convergence_engine import TransferEngine
from transfers.models import ArtifactFingerprint, FingerprintKind, SourceIdentity, TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

MIRROR_FILENAME = "ubuntu-24.04.3-desktop-amd64.iso"
SAMPLE_BYTES = 64 * 1024
PAYLOAD = (hashlib.sha256(b"debridpulse-dp-1.0.12-fixture-seed").digest() * (SAMPLE_BYTES // 32 + 1))[:3 * SAMPLE_BYTES]
assert len(PAYLOAD) > SAMPLE_BYTES  # Section 11: large enough to force first+last range sampling.


class MirrorFixtureServer:
    """N independently addressed loopback endpoints with real Range/206 support."""

    def __init__(self):
        self.payloads: dict[str, bytes] = {}
        self.behaviors: dict[str, str] = {}
        self.hits: list[str] = []
        self._runner = None
        self.port = 0

    def route(self, path: str, payload: bytes, *, behavior: str = "normal") -> None:
        self.payloads[path] = payload
        self.behaviors[path] = behavior

    async def start(self):
        app = web.Application()
        app.router.add_route("GET", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]

    async def stop(self):
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        path = request.path
        self.hits.append(path)
        behavior = self.behaviors.get(path, "missing")
        if behavior == "unsupported_status":
            return web.Response(status=500)
        if behavior == "zero_byte_success":
            # DP 1.0.12 canonical lifecycle/recovery/completion rework,
            # Section 5/11: a genuinely degenerate real-server response --
            # 200 OK, Content-Length: 0, empty body -- for what the OTHER
            # sibling proves is a large real payload. GeneralHttpProvider
            # never learned a size at resolution time (it does none), so
            # aria2 completing this transfer natively as SUCCEEDED with
            # total_bytes=0/completed_bytes=0 is indistinguishable, from raw
            # executor evidence alone, from transfer 265's actual production
            # shape -- core must still refuse to treat it as a legitimate
            # empty-file completion.
            return web.Response(status=200, body=b"", headers={"Content-Length": "0"})
        payload = self.payloads.get(path)
        if payload is None:
            return web.Response(status=404)
        total = len(payload)
        range_header = request.headers.get("Range")
        if not range_header:
            return web.Response(body=payload, headers={"Content-Length": str(total)})
        try:
            unit, rng = range_header.split("=", 1)
            start_s, end_s = rng.split("-", 1)
            start = int(start_s)
            end = int(end_s) if end_s.strip() else total - 1
            end = min(end, total - 1)
        except (ValueError, IndexError):
            return web.Response(status=400)
        chunk = payload[start:end + 1]
        return web.Response(
            status=206, body=chunk,
            headers={"Content-Range": f"bytes {start}-{end}/{total}", "Content-Length": str(len(chunk))},
        )

    def url(self, host_index: int, path: str) -> str:
        return f"http://127.0.0.{host_index}:{self.port}{path}"


async def _start_aria2(tmp_path):
    if shutil.which("aria2c") is None:
        pytest.skip("aria2c is required for the DP 1.0.12 multi-mirror runtime proof")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    secret = "dp1012-fixture-secret"
    proc = await asyncio.create_subprocess_exec(
        "aria2c", "--enable-rpc=true", "--rpc-listen-all=false", f"--rpc-listen-port={port}",
        f"--rpc-secret={secret}", "--rpc-allow-origin-all=false",
        f"--dir={tmp_path}", "--max-download-result=40", "--summary-interval=0",
        "--console-log-level=warn", "--auto-file-renaming=false",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    service = Aria2Service(f"http://127.0.0.1:{port}/jsonrpc", secret, 3)
    last = None
    for _ in range(80):
        if proc.returncode is not None:
            stdout, stderr = await proc.communicate()
            raise AssertionError(f"aria2c exited early: {stdout!r} {stderr!r}")
        try:
            await service.test()
            return proc, service
        except Exception as exc:
            last = exc
            await asyncio.sleep(0.05)
    proc.terminate()
    await proc.wait()
    raise AssertionError(f"aria2 RPC did not become ready: {last}")


async def _stop_aria2(proc, service):
    try:
        await service._call("aria2.shutdown")
    except Exception:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except TimeoutError:
        proc.kill()
        await proc.wait()


async def _noop():
    return None


class _Runtime:
    def __init__(self, repository, engine, proc, service, downloads, server):
        self.repository = repository
        self.engine = engine
        self.proc = proc
        self.service = service
        self.downloads = downloads
        self.server = server

    async def close(self):
        await _stop_aria2(self.proc, self.service)
        await self.server.stop()

    async def until(self, predicate, *, label, ticks=400):
        for _ in range(ticks):
            await self.engine.tick()
            value = await predicate()
            if value:
                return value
            await asyncio.sleep(0.02)
        raise AssertionError(f"DP 1.0.12 multi-mirror runtime did not reach: {label}")


async def _build_runtime(tmp_path, monkeypatch) -> _Runtime:
    # Defeat the (correct, production-critical) public-destination guard only
    # for this test's loopback fixture -- Section 18 requires the guard stay
    # intact in production; both call sites patched here are the exact seams
    # the real code uses (services.network_safety for the sampler's own
    # internal resolution/validation, executors.aria2.executor for the
    # executor's separate download-path validation), matching the established
    # pattern in test_general_http_stage5_runtime.py.
    async def _validated(uri: str) -> str:
        return uri

    def _allow_resolution(addresses, *, host):
        return None

    monkeypatch.setattr(network_safety, "validate_resolved_public_destination", _validated)
    monkeypatch.setattr(network_safety, "reject_non_public_resolution", _allow_resolution)
    monkeypatch.setattr(aria2_executor_module, "validate_resolved_public_destination", _validated)

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "dp1012.sqlite3")
    await database.init_db()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    policy = TransferPolicy(retry_delay=0, adoption_stability_seconds=0, max_active_executions=10)
    engine = TransferEngine(repository, registry, download_root=str(downloads), policy=policy)
    await engine.initialize()

    server = MirrorFixtureServer()
    await server.start()

    proc, service = await _start_aria2(downloads)
    egress = SimpleNamespace(ensure_started=_noop, job_options=lambda address, external: {})
    executor = Aria2Executor(service, Aria2Configuration(str(downloads), external=False, confirmation_delay=0),
                             repository.authorize_execution, egress=egress)
    registry.register_provider(GeneralHttpProvider())
    registry.register_executor(executor)
    return _Runtime(repository, engine, proc, service, downloads, server)


async def test_ten_identical_mirrors_converge_within_one_transfer(tmp_path, monkeypatch):
    """DP 1.0.12 corrective Sections 4.1/4.2/4.3/9/21 Example A (real
    runtime, corrected topology).

    Ten independent HTTPS-style loopback mirrors of one identical payload,
    submitted as ONE Quick-Add-shaped batch -- a single engine.submit() call
    admitting ONE transfer with 10 sibling TransferRequests, exactly the
    restored topology in application/service.py submit_links() -- must:
      * remain ONE transfer owning 10 independent durable request lineages
        throughout (4.1/4.2), never fanning out into 10 top-level transfers;
      * converge, through the real GeneralHttpProvider + real bounded
        sampler, to exactly one canonical artifact INSIDE that same
        transfer, exposing 10 durable candidate bindings (4.3);
      * never mark the parent transfer `consolidated` merely because its own
        sibling requests converged (4.3, Invariant 4) -- `consolidated` is
        reserved for genuine cross-transfer contribution (see
        test_later_separate_transfer_cross_transfer_consolidates_into_canonical
        below);
      * record zero same-transfer `artifact_consolidations` rows (Invariant
        9/Example A) -- that table exists for cross-transfer provenance only
        (CanonicalOwnership.attach());
      * do so correctly even though all 10 materialize concurrently, racing
        through the same canonical-ownership machinery (12.8).
    """
    runtime = await _build_runtime(tmp_path, monkeypatch)
    path = "/" + MIRROR_FILENAME
    runtime.server.route(path, PAYLOAD, behavior="normal")
    try:
        requests = tuple(
            TransferRequest("http", runtime.server.url(index, path))
            for index in range(1, 11)
        )
        transfer = await runtime.engine.submit(requests, deduplicate=False)

        async def converged():
            artifacts = await runtime.repository.artifacts(transfer.id)
            if len(artifacts) != 1:
                return None
            bindings = await runtime.engine.canonical.bindings(artifacts[0].id)
            return artifacts[0] if len(bindings) == 10 else None

        canonical_artifact = await runtime.until(converged, label="10-mirror intra-transfer convergence")

        records = await runtime.repository.requests(transfer.id)
        assert len(records) == 10  # Section 4.1/4.2: one transfer, 10 independent request lineages.
        request_ids = {record.id for record in records}
        assert len(request_ids) == 10  # every request keeps its own durable identity.

        bindings = await runtime.engine.canonical.bindings(canonical_artifact.id)
        assert len(bindings) == 10  # Section 4.3/Example A: 10 durable candidate/source bindings.
        all_origin_request_ids = set()
        for binding in bindings:
            for origin in binding["origins"]:
                all_origin_request_ids.add(str(origin["request_id"]))
        assert all_origin_request_ids == request_ids  # every sibling represented via origin provenance.

        source_scopes = {(binding["source_identity"] or {}).get("key") for binding in bindings}
        assert source_scopes == {f"127.0.0.{index}" for index in range(1, 11)}

        async with database.get_db() as db:
            row = await db.fetchone(
                "SELECT COUNT(*) AS n FROM artifact_consolidations WHERE source_transfer_id=?",
                (transfer.id,),
            )
        assert int(row["n"]) == 0  # Invariant 9/Example A: no same-transfer artifact_consolidations rows.

        final_transfer = await runtime.repository.get(transfer.id)
        assert final_transfer.state != TransferState.CONSOLIDATED  # Invariant 4: sibling convergence != transfer consolidation.
    finally:
        await runtime.close()


async def test_later_separate_transfer_cross_transfer_consolidates_into_canonical(tmp_path, monkeypatch):
    """DP 1.0.12 corrective Sections 4.4/21 Example C (real runtime): a
    LATER, genuinely separately admitted Quick Add -- its own distinct
    engine.submit() call, modeling a second user submission -- that resolves
    to a source proven equivalent to an already-established canonical
    artifact must still attach/consolidate through the existing
    cross-transfer CanonicalOwnership.attach() path. This is the one
    legitimate use of cross-transfer consolidation (Section 4.4), distinct
    from the intra-transfer sibling convergence proven above (Section 4.3) --
    the two must not be confused (Section 4.5)."""
    runtime = await _build_runtime(tmp_path, monkeypatch)
    path = "/" + MIRROR_FILENAME
    runtime.server.route(path, PAYLOAD, behavior="normal")
    try:
        first_requests = tuple(
            TransferRequest("http", runtime.server.url(index, path))
            for index in range(1, 4)
        )
        first_transfer = await runtime.engine.submit(first_requests, deduplicate=False)

        async def first_converged():
            artifacts = await runtime.repository.artifacts(first_transfer.id)
            if len(artifacts) != 1:
                return None
            bindings = await runtime.engine.canonical.bindings(artifacts[0].id)
            return artifacts[0] if len(bindings) == 3 else None

        canonical_before = await runtime.until(first_converged, label="first batch intra-transfer convergence")
        assert (await runtime.repository.get(first_transfer.id)).state != TransferState.CONSOLIDATED

        # A genuinely separate LATER user submission -- its own engine.submit()
        # call, its own transfer -- happens to resolve to an equivalent source.
        later_request = TransferRequest("http", runtime.server.url(4, path))
        later_transfer = await runtime.engine.submit((later_request,), deduplicate=False)

        async def cross_transfer_consolidated():
            info = await runtime.engine.canonical.consolidation(later_transfer.id)
            return info if info["state"] == "complete" else None

        info = await runtime.until(cross_transfer_consolidated, label="later transfer cross-transfer consolidation")
        assert info["consolidated_into"] == first_transfer.id

        async with database.get_db() as db:
            row = await db.fetchone(
                "SELECT COUNT(*) AS n FROM artifact_consolidations WHERE source_transfer_id=?",
                (later_transfer.id,),
            )
        assert int(row["n"]) == 1  # Section 4.4: cross-transfer provenance recorded for the later transfer.

        winner_final = await runtime.repository.get(first_transfer.id)
        assert winner_final.state != TransferState.CONSOLIDATED  # the canonical owner remains authoritative.
        bindings_after = await runtime.engine.canonical.bindings(canonical_before.id)
        assert len(bindings_after) == 4  # 3 intra-transfer siblings + 1 genuine cross-transfer contributor.

        # Explicit candidate-origin/provenance assertion for the later
        # transfer's own contribution (Section 4.4) -- not just the
        # artifact_consolidations row and the binding-count growth above.
        later_records = await runtime.repository.requests(later_transfer.id)
        assert len(later_records) == 1
        later_request_id = later_records[0].id
        later_origins = [
            origin for binding in bindings_after for origin in binding["origins"]
            if int(origin["contributing_transfer_id"]) == later_transfer.id
        ]
        assert len(later_origins) == 1  # exactly one candidate-origin/provenance record for the later transfer.
        assert str(later_origins[0]["request_id"]) == later_request_id  # tied back to its own durable request.
    finally:
        await runtime.close()


async def test_same_filename_different_content_siblings_do_not_converge_within_one_transfer(tmp_path, monkeypatch):
    """Section 4.3/12.4/21 (corrected topology): identical logical filename,
    genuinely different payloads, submitted as SIBLING requests of the same
    one-batch transfer, must remain two independent, non-consolidated
    artifacts inside that one transfer -- proving intra-transfer negative
    safety, not merely cross-transfer safety -- even once unknown-size
    pairing is no longer a structural rejection."""
    runtime = await _build_runtime(tmp_path, monkeypatch)
    path = "/" + MIRROR_FILENAME
    payload_b = PAYLOAD[::-1]  # same length, genuinely different bytes.
    assert payload_b != PAYLOAD
    # Two distinct server *behaviors* keyed off distinct paths would be
    # simpler, but Section 12.4 specifically requires the SAME logical
    # filename to still diverge on content, so both requests target the same
    # fixture path while the two hosts serve different bytes for it. aiohttp
    # routes per-app, so give each host its own server/app instance instead
    # of trying to key one handler by client source address.
    other = MirrorFixtureServer()
    try:
        runtime.server.route(path, PAYLOAD, behavior="normal")
        await other.start()
        other.route(path, payload_b, behavior="normal")

        left_request = TransferRequest("http", runtime.server.url(1, path))
        right_request = TransferRequest("http", other.url(2, path))
        transfer = await runtime.engine.submit((left_request, right_request), deduplicate=False)

        async def both_materialized():
            artifacts = await runtime.repository.artifacts(transfer.id)
            return artifacts if len(artifacts) == 2 else None

        await runtime.until(both_materialized, label="both mismatched siblings materialize independently")
        for _ in range(10):
            await runtime.engine.tick()
            await asyncio.sleep(0.02)

        final_transfer = await runtime.repository.get(transfer.id)
        assert final_transfer.state != TransferState.CONSOLIDATED
        # Both siblings must still each own their own (non-standby) artifact
        # row -- a false merge would retire one side's row to
        # mirror_state='standby', dropping it out of repository.artifacts().
        artifacts = await runtime.repository.artifacts(transfer.id)
        assert len(artifacts) == 2
        assert (await runtime.engine.canonical.consolidation(transfer.id))["state"] == "none"
    finally:
        await other.stop()
        await runtime.close()


async def test_real_aria2_zero_byte_success_never_completes_or_delivers(tmp_path, monkeypatch):
    """DP 1.0.12 canonical lifecycle/recovery/completion rework, Section 11
    real-runtime proof: two genuinely different-content siblings of one
    transfer through the real GeneralHttpProvider + real Aria2Executor --
    one serves the real payload (a legitimate positive-size completion), the
    other serves a real HTTP 200 with ``Content-Length: 0`` and an empty
    body (a real aria2 SUCCEEDED execution with total_bytes=0,
    completed_bytes=0 -- transfer 265's exact raw-evidence shape, produced
    here by an actual native download rather than a mocked observation).

    Required outcome: exactly one artifact reaches ``completed`` with the
    real positive size and real bytes on disk; the zero-byte sibling must
    NEVER become ``completed`` and must never be delivered, regardless of
    how many scheduler cycles run afterward."""
    runtime = await _build_runtime(tmp_path, monkeypatch)
    real_path = "/" + MIRROR_FILENAME
    empty_path = "/empty-" + MIRROR_FILENAME
    runtime.server.route(real_path, PAYLOAD, behavior="normal")
    runtime.server.route(empty_path, b"", behavior="zero_byte_success")
    try:
        real_request = TransferRequest("http", runtime.server.url(1, real_path))
        empty_request = TransferRequest("http", runtime.server.url(2, empty_path))
        transfer = await runtime.engine.submit((real_request, empty_request), deduplicate=False)

        async def both_materialized():
            artifacts = await runtime.repository.artifacts(transfer.id)
            return artifacts if len(artifacts) == 2 else None

        await runtime.until(both_materialized, label="both distinct-content siblings materialize independently")

        async def real_one_completed():
            artifacts = await runtime.repository.artifacts(transfer.id)
            return next((item for item in artifacts if item.state == "completed"), None)

        completed = await runtime.until(real_one_completed, label="the real payload's artifact completes")
        assert completed.expected_bytes == len(PAYLOAD)
        assert Path(completed.target).read_bytes() == PAYLOAD

        # Let the degenerate zero-byte sibling run through several more
        # scheduler cycles -- it must never silently become completed later,
        # and never oscillate the parent into a false COMPLETED/consolidated
        # state on the strength of only the OTHER sibling.
        for _ in range(15):
            await runtime.engine.tick()
            await asyncio.sleep(0.02)

        artifacts_by_target = {item.target: item for item in await runtime.repository.artifacts(transfer.id)}
        zero_byte_artifact = next(item for item in artifacts_by_target.values() if item.id != completed.id)
        assert zero_byte_artifact.state != "completed"
        assert zero_byte_artifact.expected_bytes == 0

        async with database.get_db() as db:
            delivered = await db.fetchone(
                """SELECT COUNT(*) AS n FROM execution_attempt_provenance
                    WHERE artifact_id=? AND delivered=1""",
                (zero_byte_artifact.id,),
            )
        assert int(delivered["n"]) == 0  # never counted as delivered provenance.

        final_transfer = await runtime.repository.get(transfer.id)
        assert final_transfer.state != TransferState.CONSOLIDATED
        assert final_transfer.state != TransferState.COMPLETED  # the unsatisfied sibling still votes (Section 5.2/6.4).
    finally:
        await runtime.close()


class _UnknownSizeProvider(ParcelProvider):
    """A fake provider standing in for General HTTP's defining trait: it never
    reports an advance size (expected_bytes stays 0), so this exercises the
    corrected pairing_failure()/​_sample_size_compatible_with_reports() path
    with a fast, process-free fixture. The real aria2+aiohttp proof above
    already covers the actual General HTTP provider end to end; this test's
    job is narrower and specific to Section 12.9: does convergence reached
    under the corrected topology/equivalence rule durably survive a process
    restart."""

    def candidate(self, name="ubuntu.iso", *, payload="parcel"):
        return replace(
            super().candidate(name, payload="shared-iso-content"),
            expected_bytes=0,
            source_identity=SourceIdentity("host", self.descriptor.id),
        )


def _build_unknown_size_runtime(tmp_path, monkeypatch, providers, *, now=None):
    """Fast, process-free stand-in for a converged General-HTTP-shaped
    canonical artifact: unknown-expected_bytes candidates on distinct host
    source identities, a fake sampler standing in for the real bounded
    content sampler (already proven against the genuine
    provider/executor/sampler stack above), a real engine/repository/
    CanonicalOwnership/recovery policy otherwise untouched."""
    repository = TransferRepository()
    registry = IntegrationRegistry()
    executor = MemoryExecutor(repository.authorize_execution)

    async def fingerprint(candidate):
        # 4 bytes to match MemoryExecutor.finish()'s fixed b"done" payload,
        # so a completion-path test (13.7) can adopt the file as stable
        # without a spurious expected/actual size mismatch.
        return ArtifactFingerprint(4, "bounded-shared-iso-content")

    monkeypatch.setattr(executor, "fingerprint", fingerprint)
    for provider in providers:
        registry.register_provider(provider)
    registry.register_executor(executor)
    engine = TransferEngine(
        repository, registry, download_root=str(tmp_path / "payloads"),
        policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                              max_active_executions=8, resolution_concurrency=8),
        clock=(now if now is not None else (lambda: 1000.0)),
    )
    return repository, engine, executor


async def test_restart_preserves_canonical_owner_bindings_and_consolidated_status(tmp_path, monkeypatch):
    """Section 12.9: after convergence, a fresh repository/engine/canonical
    instance re-attached to the same durable SQLite state (simulating a
    process restart) must still report the correct canonical owner, all
    candidate bindings/origins, and the consolidated status of the
    contributor transfers -- with no re-resolution, re-sampling, or provider
    I/O required."""
    db_path = tmp_path / "restart.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()

    def build(providers):
        repository, engine, _executor = _build_unknown_size_runtime(tmp_path, monkeypatch, providers)
        return repository, engine

    providers = tuple(_UnknownSizeProvider(f"unknown-size-{index}") for index in range(1, 4))
    repository, engine = build(providers)
    await engine.initialize()

    transfers = []
    for index, provider in enumerate(providers, start=1):
        transfer = await engine.submit(
            (TransferRequest("parcel", f"mirror-{index}", name="ubuntu.iso",
                             preferred_provider=provider.descriptor.id),),
            name="ubuntu.iso", deduplicate=False,
        )
        transfers.append(transfer)
    transfer_ids = [transfer.id for transfer in transfers]

    for _ in range(6):
        await engine.resolve_pending()

    states = {tid: (await repository.get(tid)).state for tid in transfer_ids}
    consolidated_before = {tid for tid, state in states.items() if state == TransferState.CONSOLIDATED}
    assert len(consolidated_before) == 2
    winner_before = next(tid for tid in transfer_ids if tid not in consolidated_before)
    artifact_before = (await repository.artifacts(winner_before))[0]
    assert len(artifact_before.candidates) == 3
    bindings_before = await engine.canonical.bindings(artifact_before.id)
    assert len(bindings_before) == 3

    # Simulate a process restart: fresh repository/registry/engine/
    # CanonicalOwnership objects, same durable SQLite file, no re-submission.
    repository2, engine2 = build(providers)
    await engine2.initialize()

    states_after = {tid: (await repository2.get(tid)).state for tid in transfer_ids}
    assert {tid for tid, state in states_after.items() if state == TransferState.CONSOLIDATED} == consolidated_before
    artifact_after = (await repository2.artifacts(winner_before))[0]
    assert artifact_after.id == artifact_before.id
    assert len(artifact_after.candidates) == 3

    bindings_after = await engine2.canonical.bindings(artifact_after.id)
    assert len(bindings_after) == 3
    assert {binding["candidate_id"] for binding in bindings_after} == {
        binding["candidate_id"] for binding in bindings_before
    }
    origin_transfer_ids = {
        int(origin["contributing_transfer_id"])
        for binding in bindings_after for origin in binding["origins"]
    }
    assert origin_transfer_ids == set(transfer_ids)

    for tid in consolidated_before:
        info = await engine2.canonical.consolidation(tid)
        assert info["state"] == "complete"
        assert info["consolidated_into"] == winner_before


async def test_mixed_six_proven_four_transient_siblings_stay_one_transfer(tmp_path, monkeypatch):
    """DP 1.0.12 canonical equivalence/lifecycle correction (Section 4/5,
    Root Cause A), superseding this test's original "Example B" contract.

    One Quick-Add-shaped batch of 10 sibling requests where 6 sources obtain
    full/strong proof for one canonical artifact (the original acquisition
    plus 5 attached alternates) while 4 receive transient proof inability
    (range_unsupported / dns_failure) that exhausts the existing bounded
    retry budget (transfers/cohorts.py:_PROOF_RETRY_BUDGET == 2) must:
      * remain ONE transfer throughout -- never split into 6 or 10 top-level
        transfers (Section 5, Invariant 1/2/9);
      * converge the 6 provable siblings onto one canonical artifact;
      * leave the 4 transiently-unprovable siblings HELD, unresolved, and
        durably MATERIALIZING once bounded proof is exhausted -- transient
        inability is never treated as contradictory evidence (Invariant 8)
        AND, per the canonical equivalence/lifecycle correction, retry-budget
        exhaustion is never treated as permission to materialize an
        independent physical artifact either (this is the exact production
        defect transfer 263 exposed: two transiently-unprovable Ubuntu
        mirrors were wrongly allowed to materialize as numbered duplicate
        artifacts once their bounded proof retries were exhausted);
      * leave exact durable equivalence_reason/equivalence_disposition
        explainable for every transient sibling after exhaustion, and never
        allocate them a competing executor writer.
    """
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "mixed-six-four.sqlite3")
    await database.init_db()
    now = [1000.0]
    providers = tuple(_UnknownSizeProvider(f"mixed-mirror-{index}") for index in range(1, 11))
    repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, providers, now=lambda: now[0],
    )
    await engine.initialize()

    range_unsupported_ids = {providers[6].descriptor.id, providers[7].descriptor.id}
    dns_failure_ids = {providers[8].descriptor.id, providers[9].descriptor.id}

    async def mixed_fingerprint(candidate):
        source_key = candidate.provider_id
        if source_key in range_unsupported_ids:
            return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="range_unsupported")
        if source_key in dns_failure_ids:
            raise socket.gaierror("simulated DNS resolution failure")
        return ArtifactFingerprint(4, "bounded-shared-iso-content")

    monkeypatch.setattr(executor, "fingerprint", mixed_fingerprint)

    requests = tuple(
        TransferRequest("parcel", f"mixed-mirror-{index}", name="ubuntu.iso",
                         preferred_provider=provider.descriptor.id)
        for index, provider in enumerate(providers, start=1)
    )
    transfer = await engine.submit(requests, name="ubuntu.iso", deduplicate=False)
    by_payload = {record.request.payload: record for record in await engine.repository.requests(transfer.id)}
    proven_records = [by_payload[f"mixed-mirror-{index}"] for index in range(1, 7)]
    transient_records = [by_payload[f"mixed-mirror-{index}"] for index in range(7, 11)]

    # Establish the 6-source canonical group FIRST and deterministically --
    # sequential (not gathered) _resolve() calls guarantee one of these six
    # becomes the baseline before any transient sibling is ever compared
    # against it (Section 12.1/12.2: deterministic, not race-dependent).
    for record in proven_records:
        await engine._resolve(record)

    artifacts = await engine.repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    canonical_artifact = artifacts[0]
    bindings = await engine.canonical.bindings(canonical_artifact.id)
    assert len(bindings) == 6  # canonical acquisition + 5 attached alternates (Example B: "6 proven sources").

    # Drive each transiently-unprovable sibling through the existing bounded
    # retry budget (2 retries) to exhaustion, sequentially and deterministically.
    for record in transient_records:
        await engine._resolve(record)
        for _ in range(2):
            refreshed = next(
                item for item in await engine.repository.requests(transfer.id) if item.id == record.id
            )
            assert refreshed.state == "materializing"  # still parked pending bounded proof retry.
            now[0] = refreshed.retry_at + 0.01
            await engine._process_request(refreshed)

    final_records = {item.id: item for item in await engine.repository.requests(transfer.id)}
    assert len(final_records) == 10  # Invariant 1/2: one transfer, 10 durable requests, throughout.

    for record in transient_records:
        # Corrected contract: automatic proof retries stopped, but identity
        # remains unresolved -- the request stays durably MATERIALIZING
        # (held), never independently materialized.
        assert final_records[record.id].state == "materializing"

    async with database.get_db() as db:
        for record in transient_records:
            row = await db.fetchone(
                "SELECT equivalence_disposition,equivalence_reason,retry_at FROM transfer_requests WHERE id=?",
                (record.id,),
            )
            assert row["equivalence_disposition"] == "exhausted"
            assert row["equivalence_reason"] in {"range_unsupported", "dns_failure"}
            assert float(row["retry_at"] or 0) == 0

    artifacts_after = await engine.repository.artifacts(transfer.id)
    # Zero competing writers for the unresolved siblings -- only the 6-source
    # canonical exists (DP 1.0.12 Root Cause A: proof exhaustion never
    # authorizes an independent physical writer).
    assert len(artifacts_after) == 1
    assert artifacts_after[0].id == canonical_artifact.id

    # Repeated scheduler ticks after exhaustion must not hot-loop proof
    # acquisition or allocate a writer for a still-unresolved sibling.
    for record in transient_records:
        refreshed = next(
            item for item in await engine.repository.requests(transfer.id) if item.id == record.id
        )
        await engine._process_request(refreshed)
    async with database.get_db() as db:
        for record in transient_records:
            row = await db.fetchone(
                "SELECT equivalence_retry_count,equivalence_disposition FROM transfer_requests WHERE id=?",
                (record.id,),
            )
            assert int(row["equivalence_retry_count"]) == 2
            assert row["equivalence_disposition"] == "exhausted"
    assert len(await engine.repository.artifacts(transfer.id)) == 1

    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT COUNT(*) AS n FROM artifact_consolidations WHERE source_transfer_id=?",
            (transfer.id,),
        )
    assert int(row["n"]) == 0  # Invariant 9: no same-transfer artifact_consolidations rows.

    final_transfer = await engine.repository.get(transfer.id)
    assert final_transfer.state != TransferState.CONSOLIDATED  # Invariant 4: sibling convergence != transfer consolidation.


async def test_five_mirror_production_263_regression(tmp_path, monkeypatch):
    """DP 1.0.12 canonical equivalence/lifecycle correction, Section 11.1: the
    direct five-mirror regression analogue of production transfer 263.

    5 sibling mirror requests for the same logical filename, ONE Quick-Add-
    shaped batch:
      * mirror A -> canonical acquisition (first materialized);
      * mirror B, C -> full-content equivalent, durably attach to A;
      * mirror D -> equivalence sampler persistently returns
        range_unsupported;
      * mirror E -> equivalence sampler persistently raises a DNS resolution
        failure (dns_failure).

    Required outcome: exactly one physical canonical artifact ever exists (no
    "(2)"/"(3)" artifact, no second/third executor writer); B and C durably
    attach to A; D and E remain held/unresolved after their bounded proof
    attempts exhaust (proof counters stop at the budget); the parent is never
    FAILED merely because D/E could not prove identity."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "five-mirror-263.sqlite3")
    await database.init_db()
    now = [1000.0]
    providers = tuple(_UnknownSizeProvider(f"ubuntu-mirror-{index}") for index in range(1, 6))
    repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, providers, now=lambda: now[0],
    )
    await engine.initialize()

    async def fingerprint(candidate):
        if candidate.provider_id == providers[3].descriptor.id:  # mirror D
            return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="range_unsupported")
        if candidate.provider_id == providers[4].descriptor.id:  # mirror E
            raise socket.gaierror("simulated DNS resolution failure for mirror E")
        return ArtifactFingerprint(4, "bounded-shared-iso-content")

    monkeypatch.setattr(executor, "fingerprint", fingerprint)

    requests = tuple(
        TransferRequest("parcel", f"ubuntu-mirror-{index}", name="ubuntu-26.04-desktop-amd64.iso",
                         preferred_provider=provider.descriptor.id)
        for index, provider in enumerate(providers, start=1)
    )
    transfer = await engine.submit(requests, name="ubuntu-26.04-desktop-amd64.iso", deduplicate=False)
    by_payload = {record.request.payload: record for record in await engine.repository.requests(transfer.id)}
    a_record = by_payload["ubuntu-mirror-1"]
    b_record, c_record = by_payload["ubuntu-mirror-2"], by_payload["ubuntu-mirror-3"]
    d_record, e_record = by_payload["ubuntu-mirror-4"], by_payload["ubuntu-mirror-5"]

    # Deterministic, sequential (not gathered) resolution -- mirror A becomes
    # the canonical baseline before any sibling is ever compared against it.
    await engine._resolve(a_record)
    artifacts = await engine.repository.artifacts(transfer.id)
    assert len(artifacts) == 1  # exactly one physical canonical artifact.
    canonical_artifact = artifacts[0]

    await engine._resolve(b_record)
    await engine._resolve(c_record)
    bindings = await engine.canonical.bindings(canonical_artifact.id)
    assert len(bindings) == 3  # A + B + C durably attach to the same canonical artifact.

    for record in (d_record, e_record):
        await engine._resolve(record)
        for _ in range(2):
            refreshed = next(
                item for item in await engine.repository.requests(transfer.id) if item.id == record.id
            )
            assert refreshed.state == "materializing"  # still parked pending bounded proof retry.
            now[0] = refreshed.retry_at + 0.01
            await engine._process_request(refreshed)

    final_records = {item.id: item for item in await engine.repository.requests(transfer.id)}
    for record in (d_record, e_record):
        # D and E remain held/unresolved -- no "(2)"/"(3)" artifact, no
        # second/third executor writer, once bounded proof exhausts.
        assert final_records[record.id].state == "materializing"

    async with database.get_db() as db:
        for record in (d_record, e_record):
            row = await db.fetchone(
                """SELECT equivalence_disposition,equivalence_reason,equivalence_retry_count,retry_at
                    FROM transfer_requests WHERE id=?""",
                (record.id,),
            )
            assert row["equivalence_disposition"] == "exhausted"
            assert int(row["equivalence_retry_count"]) == 2  # proof counters stop at the budget.
            assert row["equivalence_reason"] in {"range_unsupported", "dns_failure"}
            assert float(row["retry_at"] or 0) == 0

    artifacts_after = await engine.repository.artifacts(transfer.id)
    assert len(artifacts_after) == 1  # still exactly one physical canonical artifact.
    assert artifacts_after[0].id == canonical_artifact.id
    starts = [call for call in executor.calls if call[0] == "start"]
    assert len(starts) <= 1  # no second/third executor writer for D or E.

    # The parent is never FAILED because D/E could not prove identity; it
    # remains in a legitimate unresolved/autonomous state while they hold.
    await engine._aggregate(transfer.id)
    transfer_after = await engine.repository.get(transfer.id)
    assert transfer_after.state != TransferState.FAILED


async def test_production_266_empty_bootstrap_bad_source_first(tmp_path, monkeypatch):
    """DP 1.0.12 CANON-001 follow-up, Required Canonical Regression Test #1:
    direct reproduction of production transfer 266's empty-canonical
    bootstrap-admission hole.

    Same five-mirror shape as test_five_mirror_production_263_regression (3
    good equivalent mirrors, 1 range-unsupported source, 1 DNS-failing
    source shaped like transfer 266's NUS request) but the DNS-failing
    source is deliberately forced to reach ``_materialize()`` FIRST, while
    the canonical set is still genuinely empty -- exactly transfer 266's
    shape, where the DNS-failing source crossed the physical-writer
    boundary before any canonical existed and later accumulated real
    recovery-exhausted artifact state. This must NOT be weakened by
    resolving a known-good canonical first."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "production-266.sqlite3")
    await database.init_db()
    now = [1000.0]
    providers = tuple(_UnknownSizeProvider(f"ubuntu-266-mirror-{index}") for index in range(1, 6))
    repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, providers, now=lambda: now[0],
    )
    await engine.initialize()

    async def fingerprint(candidate):
        if candidate.provider_id == providers[3].descriptor.id:  # mirror D
            return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="range_unsupported")
        if candidate.provider_id == providers[4].descriptor.id:  # mirror E (NUS-shaped)
            raise socket.gaierror("simulated DNS resolution failure for mirror E")
        return ArtifactFingerprint(4, "bounded-shared-iso-content")

    monkeypatch.setattr(executor, "fingerprint", fingerprint)

    requests = tuple(
        TransferRequest("parcel", f"ubuntu-266-mirror-{index}", name="ubuntu-26.04-desktop-amd64.iso",
                         preferred_provider=provider.descriptor.id)
        for index, provider in enumerate(providers, start=1)
    )
    transfer = await engine.submit(requests, name="ubuntu-26.04-desktop-amd64.iso", deduplicate=False)
    by_payload = {record.request.payload: record for record in await engine.repository.requests(transfer.id)}
    a_record = by_payload["ubuntu-266-mirror-1"]
    b_record, c_record = by_payload["ubuntu-266-mirror-2"], by_payload["ubuntu-266-mirror-3"]
    d_record, e_record = by_payload["ubuntu-266-mirror-4"], by_payload["ubuntu-266-mirror-5"]

    # The DNS-failing source reaches the empty-canonical bootstrap decision
    # FIRST, before ANY other sibling has even resolved -- production
    # transfer 266's exact shape. It must hold, never seed.
    await engine._resolve(e_record)
    assert len(await engine.repository.artifacts(transfer.id)) == 0  # no writer merely for being first.
    refreshed_e = next(item for item in await engine.repository.requests(transfer.id) if item.id == e_record.id)
    assert refreshed_e.state == "materializing"  # held pending bounded self-proof retry, not materialized.

    # The good sources resolve next; mirror A must still become the ONE
    # canonical seed, exactly as if E had never run first.
    await engine._resolve(a_record)
    artifacts = await engine.repository.artifacts(transfer.id)
    assert len(artifacts) == 1  # exactly one physical canonical artifact.
    canonical_artifact = artifacts[0]
    assert canonical_artifact.request_id == a_record.id

    await engine._resolve(b_record)
    await engine._resolve(c_record)
    bindings = await engine.canonical.bindings(canonical_artifact.id)
    assert len(bindings) == 3  # A + B + C durably attach to the same canonical artifact.

    # Drive D (fresh) and E (already mid bounded retry) through to bounded
    # exhaustion -- identical budget/shape as the existing 263 regression.
    await engine._resolve(d_record)
    for record in (d_record, e_record):
        for _ in range(2):
            refreshed = next(
                item for item in await engine.repository.requests(transfer.id) if item.id == record.id
            )
            assert refreshed.state == "materializing"  # still parked pending bounded proof retry.
            now[0] = refreshed.retry_at + 0.01
            await engine._process_request(refreshed)

    final_records = {item.id: item for item in await engine.repository.requests(transfer.id)}
    for record in (d_record, e_record):
        # Held/unresolved -- no "(2)"/"(3)" artifact, no second/third
        # executor writer, once bounded proof exhausts.
        assert final_records[record.id].state == "materializing"

    async with database.get_db() as db:
        for record in (d_record, e_record):
            row = await db.fetchone(
                """SELECT equivalence_disposition,equivalence_reason,retry_at
                    FROM transfer_requests WHERE id=?""",
                (record.id,),
            )
            assert row["equivalence_disposition"] == "exhausted"
            assert row["equivalence_reason"] in {"range_unsupported", "dns_failure"}
            assert float(row["retry_at"] or 0) == 0

    artifacts_after = await engine.repository.artifacts(transfer.id)
    assert len(artifacts_after) == 1  # still exactly one physical canonical artifact -- no numbered duplicate.
    assert artifacts_after[0].id == canonical_artifact.id
    starts = [call for call in executor.calls if call[0] == "start"]
    assert len(starts) <= 1  # zero executor attempts for the held transient siblings.

    # The parent is never FAILED merely because D/E could not prove identity.
    await engine._aggregate(transfer.id)
    transfer_after = await engine.repository.get(transfer.id)
    assert transfer_after.state != TransferState.FAILED


async def test_single_member_cohort_materializes_immediately(tmp_path, monkeypatch):
    """Required Canonical Regression Test #2 / Case A: a lone General-HTTP-
    shaped source with no sibling cohort must continue to materialize
    immediately -- the CANON-001 follow-up bootstrap barrier applies only to
    genuinely multi-member same-transfer cohorts, with no artificial
    bootstrap delay or self-probe cost for the single-source case."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "single-member.sqlite3")
    await database.init_db()
    providers = (_UnknownSizeProvider("solo-mirror-1"),)
    repository, engine, executor = _build_unknown_size_runtime(tmp_path, monkeypatch, providers)
    await engine.initialize()

    probed = []

    async def fingerprint(candidate):
        probed.append(candidate.provider_id)
        return ArtifactFingerprint(4, "bounded-shared-iso-content")

    monkeypatch.setattr(executor, "fingerprint", fingerprint)

    transfer = await engine.submit(
        (TransferRequest("parcel", "solo-mirror-1", name="solo.iso",
                          preferred_provider=providers[0].descriptor.id),),
        name="solo.iso", deduplicate=False,
    )
    records = await engine.repository.requests(transfer.id)
    await engine._resolve(records[0])

    artifacts = await engine.repository.artifacts(transfer.id)
    assert len(artifacts) == 1  # immediate materialization, no bootstrap hold.
    refreshed = (await engine.repository.requests(transfer.id))[0]
    assert refreshed.state == "resolved"
    assert not probed  # no bootstrap self-probe fingerprint call for a single-member cohort.


async def test_one_viable_one_transient_bootstrap_does_not_deadlock(tmp_path, monkeypatch):
    """Required Canonical Regression Test #3 / Case E: a two-member cohort
    with one persistently transient (DNS-failing) source and one genuinely
    viable source must not deadlock -- exactly one physical writer must
    still be created, even though the transient source reaches the
    empty-canonical bootstrap decision first. Two healthy mirrors are never
    required before a writer can exist."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "one-viable-one-transient.sqlite3")
    await database.init_db()
    now = [1000.0]
    providers = (_UnknownSizeProvider("viable-mirror"), _UnknownSizeProvider("transient-mirror"))
    repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, providers, now=lambda: now[0],
    )
    await engine.initialize()

    async def fingerprint(candidate):
        if candidate.provider_id == providers[1].descriptor.id:
            raise socket.gaierror("simulated persistent DNS resolution failure")
        return ArtifactFingerprint(4, "bounded-shared-iso-content")

    monkeypatch.setattr(executor, "fingerprint", fingerprint)

    requests = (
        TransferRequest("parcel", "transient-mirror", name="mirror.iso", preferred_provider=providers[1].descriptor.id),
        TransferRequest("parcel", "viable-mirror", name="mirror.iso", preferred_provider=providers[0].descriptor.id),
    )
    transfer = await engine.submit(requests, name="mirror.iso", deduplicate=False)
    by_payload = {record.request.payload: record for record in await engine.repository.requests(transfer.id)}
    transient_record = by_payload["transient-mirror"]
    viable_record = by_payload["viable-mirror"]

    # The transient/unreachable source reaches the empty-canonical bootstrap
    # decision FIRST -- it must hold, not seed, and must not block the
    # viable source's own later turn.
    await engine._resolve(transient_record)
    assert len(await engine.repository.artifacts(transfer.id)) == 0
    refreshed_transient = next(
        item for item in await engine.repository.requests(transfer.id) if item.id == transient_record.id
    )
    assert refreshed_transient.state == "materializing"

    await engine._resolve(viable_record)
    artifacts = await engine.repository.artifacts(transfer.id)
    assert len(artifacts) == 1  # exactly one physical writer -- no deadlock.
    canonical_artifact = artifacts[0]
    assert canonical_artifact.request_id == viable_record.id

    for _ in range(2):
        refreshed = next(
            item for item in await engine.repository.requests(transfer.id) if item.id == transient_record.id
        )
        assert refreshed.state == "materializing"
        now[0] = refreshed.retry_at + 0.01
        await engine._process_request(refreshed)

    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT equivalence_disposition,equivalence_reason FROM transfer_requests WHERE id=?",
            (transient_record.id,),
        )
    assert row["equivalence_disposition"] == "exhausted"
    assert row["equivalence_reason"] == "dns_failure"

    artifacts_after = await engine.repository.artifacts(transfer.id)
    assert len(artifacts_after) == 1
    assert artifacts_after[0].id == canonical_artifact.id


async def test_all_transient_cohort_creates_no_writer_and_bounds_retry(tmp_path, monkeypatch):
    """Required Canonical Regression Test #4 / Case F: when every source in
    a same-transfer cohort currently lacks sufficient evidence for safe
    bootstrap, no physical writer may be created merely to "make progress" --
    the cohort must settle into the existing bounded exhausted/HOLD
    semantics after the proof budget, without hot-looping."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "all-transient.sqlite3")
    await database.init_db()
    now = [1000.0]
    providers = tuple(_UnknownSizeProvider(f"all-transient-{index}") for index in range(1, 4))
    repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, providers, now=lambda: now[0],
    )
    await engine.initialize()

    async def fingerprint(candidate):
        raise socket.gaierror("simulated DNS resolution failure for every source")

    monkeypatch.setattr(executor, "fingerprint", fingerprint)

    requests = tuple(
        TransferRequest("parcel", f"all-transient-{index}", name="mirror.iso", preferred_provider=provider.descriptor.id)
        for index, provider in enumerate(providers, start=1)
    )
    transfer = await engine.submit(requests, name="mirror.iso", deduplicate=False)
    records = await engine.repository.requests(transfer.id)

    for record in records:
        await engine._resolve(record)

    assert len(await engine.repository.artifacts(transfer.id)) == 0  # no writer merely to make progress.

    for record in records:
        for _ in range(2):
            refreshed = next(item for item in await engine.repository.requests(transfer.id) if item.id == record.id)
            assert refreshed.state == "materializing"
            now[0] = refreshed.retry_at + 0.01
            await engine._process_request(refreshed)

    async with database.get_db() as db:
        for record in records:
            row = await db.fetchone(
                """SELECT equivalence_disposition,equivalence_reason,equivalence_retry_count,retry_at
                    FROM transfer_requests WHERE id=?""",
                (record.id,),
            )
            assert row["equivalence_disposition"] == "exhausted"
            assert row["equivalence_reason"] == "dns_failure"
            assert int(row["equivalence_retry_count"]) == 2
            assert float(row["retry_at"] or 0) == 0

    assert len(await engine.repository.artifacts(transfer.id)) == 0  # still zero writers.

    # Repeated ticks after exhaustion must not hot-loop proof acquisition.
    for record in records:
        refreshed = next(item for item in await engine.repository.requests(transfer.id) if item.id == record.id)
        await engine._process_request(refreshed)
    async with database.get_db() as db:
        for record in records:
            row = await db.fetchone("SELECT equivalence_retry_count FROM transfer_requests WHERE id=?", (record.id,))
            assert int(row["equivalence_retry_count"]) == 2
    assert len(await engine.repository.artifacts(transfer.id)) == 0


async def test_bad_ordinal_zero_cannot_win_seed_authority_by_arrival(tmp_path, monkeypatch):
    """Required Canonical Regression Test #5 / Case D: submission ordering
    alone must never grant writer authority. The transient/unreachable
    source is submitted FIRST (ordinal 0) and is also forced to reach
    ``_materialize()`` first -- it must still not seed the cohort's
    canonical artifact; the later-ordinal, later-arriving genuinely viable
    source must become the seed instead."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "bad-ordinal-zero.sqlite3")
    await database.init_db()
    now = [1000.0]
    providers = (_UnknownSizeProvider("ordinal-zero-bad"), _UnknownSizeProvider("ordinal-one-good"))
    repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, providers, now=lambda: now[0],
    )
    await engine.initialize()

    async def fingerprint(candidate):
        if candidate.provider_id == providers[0].descriptor.id:
            raise socket.gaierror("simulated DNS resolution failure for ordinal 0")
        return ArtifactFingerprint(4, "bounded-shared-iso-content")

    monkeypatch.setattr(executor, "fingerprint", fingerprint)

    requests = (
        TransferRequest("parcel", "ordinal-zero-bad", name="mirror.iso", preferred_provider=providers[0].descriptor.id),
        TransferRequest("parcel", "ordinal-one-good", name="mirror.iso", preferred_provider=providers[1].descriptor.id),
    )
    transfer = await engine.submit(requests, name="mirror.iso", deduplicate=False)
    records = await engine.repository.requests(transfer.id)
    bad_record = next(item for item in records if item.request.payload == "ordinal-zero-bad")
    good_record = next(item for item in records if item.request.payload == "ordinal-one-good")

    async with database.get_db() as db:
        bad_row = await db.fetchone("SELECT ordinal FROM transfer_requests WHERE id=?", (bad_record.id,))
        good_row = await db.fetchone("SELECT ordinal FROM transfer_requests WHERE id=?", (good_record.id,))
    assert int(bad_row["ordinal"] or 0) < int(good_row["ordinal"] or 0)  # genuinely ordinal 0 vs 1.

    # Ordinal 0 reaches _materialize() first, deliberately, while the
    # canonical set is still empty.
    await engine._resolve(bad_record)
    assert len(await engine.repository.artifacts(transfer.id)) == 0

    await engine._resolve(good_record)
    artifacts = await engine.repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    assert artifacts[0].request_id == good_record.id  # the later-ordinal source seeded, never ordinal 0.


async def test_all_distinct_healthy_sources_still_materialize_independently(tmp_path, monkeypatch):
    """Required Canonical Regression Test #6 / Case G: genuinely different
    healthy sources submitted together into one empty-canonical cohort must
    still each materialize as their own independent artifact once the
    existing evidence model establishes they are distinct -- the bootstrap
    barrier must not collapse unrelated files or deadlock all-distinct
    collections."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "all-distinct.sqlite3")
    await database.init_db()
    providers = tuple(_UnknownSizeProvider(f"distinct-{index}") for index in range(1, 4))
    repository, engine, executor = _build_unknown_size_runtime(tmp_path, monkeypatch, providers)
    await engine.initialize()

    signatures = {
        providers[0].descriptor.id: "content-alpha",
        providers[1].descriptor.id: "content-beta",
        providers[2].descriptor.id: "content-gamma",
    }

    async def fingerprint(candidate):
        return ArtifactFingerprint(4, signatures[candidate.provider_id])

    monkeypatch.setattr(executor, "fingerprint", fingerprint)

    requests = tuple(
        TransferRequest("parcel", f"distinct-{index}", name=f"distinct-{index}.bin",
                         preferred_provider=provider.descriptor.id)
        for index, provider in enumerate(providers, start=1)
    )
    transfer = await engine.submit(requests, name="distinct-bundle", deduplicate=False)
    for record in await engine.repository.requests(transfer.id):
        await engine._resolve(record)

    artifacts = await engine.repository.artifacts(transfer.id)
    assert len(artifacts) == 3  # each genuinely distinct source materialized independently.
    assert len({artifact.request_id for artifact in artifacts}) == 3

    final_transfer = await engine.repository.get(transfer.id)
    assert final_transfer.state != TransferState.CONSOLIDATED


async def test_bootstrap_restart_reentry_no_duplicate_writer(tmp_path, monkeypatch):
    """Required Canonical Regression Test #7 / Case I: interrupting and
    re-entering mid-bootstrap (fresh engine/repository/canonical instances
    re-attached to the same durable SQLite state, simulating a process
    restart) must reconstruct identical decisions from durable state alone
    -- no duplicate writer appears, and bounded retry/exhaustion remains
    idempotent."""
    db_path = tmp_path / "bootstrap-restart.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    await database.init_db()
    now = [1000.0]
    providers = (_UnknownSizeProvider("restart-transient"), _UnknownSizeProvider("restart-viable"))

    def build():
        return _build_unknown_size_runtime(tmp_path, monkeypatch, providers, now=lambda: now[0])

    repository, engine, executor = build()
    await engine.initialize()

    async def fingerprint(candidate):
        if candidate.provider_id == providers[0].descriptor.id:
            raise socket.gaierror("simulated persistent DNS resolution failure")
        return ArtifactFingerprint(4, "bounded-shared-iso-content")

    monkeypatch.setattr(executor, "fingerprint", fingerprint)

    requests = (
        TransferRequest("parcel", "restart-transient", name="mirror.iso", preferred_provider=providers[0].descriptor.id),
        TransferRequest("parcel", "restart-viable", name="mirror.iso", preferred_provider=providers[1].descriptor.id),
    )
    transfer = await engine.submit(requests, name="mirror.iso", deduplicate=False)
    by_payload = {record.request.payload: record for record in await engine.repository.requests(transfer.id)}
    transient_record = by_payload["restart-transient"]

    # Interrupt mid-bootstrap: only the transient source has reached its
    # first held self-probe attempt; nothing has materialized yet.
    await engine._resolve(transient_record)
    assert len(await engine.repository.artifacts(transfer.id)) == 0

    # Simulate a process restart: fresh repository/registry/engine/executor,
    # same durable SQLite file, no re-submission.
    repository2, engine2, executor2 = build()
    await engine2.initialize()

    async def fingerprint2(candidate):
        if candidate.provider_id == providers[0].descriptor.id:
            raise socket.gaierror("simulated persistent DNS resolution failure")
        return ArtifactFingerprint(4, "bounded-shared-iso-content")

    monkeypatch.setattr(executor2, "fingerprint", fingerprint2)

    records_after_restart = await repository2.requests(transfer.id)
    assert len(records_after_restart) == 2  # durable requests survive restart.
    viable_record_after = next(item for item in records_after_restart if item.request.payload == "restart-viable")
    transient_record_after = next(item for item in records_after_restart if item.request.payload == "restart-transient")
    assert transient_record_after.state == "materializing"  # bootstrap hold survived restart.

    await engine2._resolve(viable_record_after)
    artifacts = await repository2.artifacts(transfer.id)
    assert len(artifacts) == 1  # exactly one writer after restart, no duplicate.
    canonical_artifact = artifacts[0]
    assert canonical_artifact.request_id == viable_record_after.id

    for _ in range(2):
        refreshed = next(item for item in await repository2.requests(transfer.id) if item.id == transient_record.id)
        assert refreshed.state == "materializing"
        now[0] = refreshed.retry_at + 0.01
        await engine2._process_request(refreshed)

    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT equivalence_disposition,equivalence_retry_count FROM transfer_requests WHERE id=?",
            (transient_record.id,),
        )
    assert row["equivalence_disposition"] == "exhausted"
    assert int(row["equivalence_retry_count"]) == 2  # bounded retry counter survives restart, stays idempotent.

    artifacts_after = await repository2.artifacts(transfer.id)
    assert len(artifacts_after) == 1
    assert artifacts_after[0].id == canonical_artifact.id


async def test_resolved_sibling_reverify_bounds_retry_without_hot_loop(tmp_path, monkeypatch):
    """Gate 9 review follow-up (cohorts.py's ``sibling.state == 'resolved'``
    re-verification branch, added to fix the Section 8.1.5 mismatch-escape
    bug): re-verifying an already-resolved sibling against the canonical it
    belongs to must reuse the SAME bounded proof-retry budget/quiescence as
    every other evidence acquisition in this module, never an unbounded
    per-tick re-sample. Four same-transfer members share one canonical
    (A, B, D all already consolidated); C's own mapping succeeds via D's
    still-healthy candidate while A's and B's candidates have gone flaky --
    re-verifying A and B during C's collection walk must hold C (not release
    the cohort, not create a duplicate writer for C) for exactly
    ``_PROOF_RETRY_BUDGET`` bounded attempts, then go quiescent without
    hot-looping A/B's fingerprint call count on further ticks."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "resolved-sibling-reverify.sqlite3")
    await database.init_db()
    now = [1000.0]
    providers = tuple(_UnknownSizeProvider(f"reverify-{label}") for label in "abcd")
    provider_a, provider_b, provider_c, provider_d = providers
    repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, providers, now=lambda: now[0],
    )
    await engine.initialize()

    call_counts = {"a": 0, "b": 0, "c": 0, "d": 0}
    label_by_provider = {
        provider_a.descriptor.id: "a", provider_b.descriptor.id: "b",
        provider_c.descriptor.id: "c", provider_d.descriptor.id: "d",
    }
    phase = ["bootstrap"]

    async def fingerprint(candidate):
        label = label_by_provider[candidate.provider_id]
        call_counts[label] += 1
        if phase[0] == "reverify" and label in {"a", "b"}:
            raise socket.gaierror(f"simulated transient flakiness for {label} during reverification")
        if phase[0] == "attach_full":
            return ArtifactFingerprint(4, "full-shared-content", FingerprintKind.FULL_CONTENT_SAMPLE)
        return ArtifactFingerprint(4, "prefix-shared-content", FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                    "range_ignored", "prefix-shared-content")

    monkeypatch.setattr(executor, "fingerprint", fingerprint)

    requests = tuple(
        TransferRequest("parcel", f"reverify-{label}", name="mirror.iso", preferred_provider=provider.descriptor.id)
        for label, provider in zip("abcd", providers)
    )
    transfer = await engine.submit(requests, name="mirror.iso", deduplicate=False)
    by_payload = {record.request.payload: record for record in await engine.repository.requests(transfer.id)}
    record_a, record_b = by_payload["reverify-a"], by_payload["reverify-b"]
    record_c, record_d = by_payload["reverify-c"], by_payload["reverify-d"]

    phase[0] = "bootstrap"
    await engine._resolve(record_a)  # A bootstrap-seeds the sole canonical (PREFIX self-evidence).
    assert len(await engine.repository.artifacts(transfer.id)) == 1

    # B and D each attach via the immediate FULL-evidence individual fast
    # path (never the collection walk), so the canonical reaches 3
    # candidates without ever touching the pre-existing, out-of-scope
    # same-name ambiguous_mapping defect (memory: weak-prefix-identical-
    # mirror-ambiguity) that the collection-walk completion path would hit.
    phase[0] = "attach_full"
    await engine._resolve(record_b)
    await engine._resolve(record_d)
    artifacts = await engine.repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    canonical = artifacts[0]
    assert len(canonical.candidates) == 3

    # C matches genuinely via D's still-healthy candidate (PREFIX), entering
    # the collection walk, while A's and B's candidates are now flaky.
    phase[0] = "reverify"
    calls_before = dict(call_counts)
    await engine._resolve(record_c)
    assert len(await engine.repository.artifacts(transfer.id)) == 1  # no duplicate writer for C.
    refreshed_c = next(item for item in await engine.repository.requests(transfer.id) if item.id == record_c.id)
    assert refreshed_c.state == "materializing"  # held, not released to independence.

    for _ in range(2):
        refreshed_c = next(item for item in await engine.repository.requests(transfer.id) if item.id == record_c.id)
        assert refreshed_c.state == "materializing"
        now[0] = refreshed_c.retry_at + 0.01
        await engine._process_request(refreshed_c)

    async with database.get_db() as db:
        row = await db.fetchone(
            """SELECT equivalence_disposition,equivalence_retry_count,retry_at
                FROM transfer_requests WHERE id=?""",
            (record_c.id,),
        )
    assert row["equivalence_disposition"] == "exhausted"
    assert int(row["equivalence_retry_count"]) == 2
    assert float(row["retry_at"] or 0) == 0

    # Quiescent: further ticks must not keep re-sampling A/B on C's behalf.
    calls_at_exhaustion = dict(call_counts)
    for _ in range(3):
        refreshed_c = next(item for item in await engine.repository.requests(transfer.id) if item.id == record_c.id)
        await engine._process_request(refreshed_c)
    assert call_counts == calls_at_exhaustion  # zero additional fingerprint calls once exhausted.
    assert call_counts["a"] > calls_before["a"]  # the bounded window did genuinely re-probe A...
    assert call_counts["b"] > calls_before["b"]  # ...and B, at least once, before bounding.

    # The pre-existing 3-candidate consolidation is completely undisturbed.
    artifacts_final = await engine.repository.artifacts(transfer.id)
    assert len(artifacts_final) == 1
    assert artifacts_final[0].id == canonical.id
    assert len(artifacts_final[0].candidates) == 3
    assert len(await engine.repository.artifacts(record_c.transfer_id)) == 1


async def test_bad_first_structural_bootstrap_waits_for_capable_sibling(tmp_path, monkeypatch):
    """Gate 9 review follow-up (Finding 1, revision 2): a candidate whose own
    self-evidence is structurally, permanently unobtainable (its sampler
    returns no fingerprint at all for this route -- not a transient
    timeout/DNS failure) must NOT win bootstrap seed authority merely by
    reaching the empty-canonical decision first. It must hold while a
    genuinely capable sibling has not yet had its own turn, exactly like the
    transient/DNS case (Case D applies identically to structural failures).
    Only once the viable sibling seeds does the structural source fall back
    to the existing, unchanged steady-state 'independent' precedent."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "bad-first-structural.sqlite3")
    await database.init_db()
    providers = (_UnknownSizeProvider("structural-bad"), _UnknownSizeProvider("viable-good"))
    repository, engine, executor = _build_unknown_size_runtime(tmp_path, monkeypatch, providers)
    await engine.initialize()

    async def fingerprint(candidate):
        if candidate.provider_id == providers[0].descriptor.id:
            return None  # structurally unroutable for this candidate -- never transient.
        return ArtifactFingerprint(4, "prefix-sig", FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                    "range_ignored", "prefix-sig")

    monkeypatch.setattr(executor, "fingerprint", fingerprint)

    requests = (
        TransferRequest("parcel", "structural-bad", name="mirror.iso", preferred_provider=providers[0].descriptor.id),
        TransferRequest("parcel", "viable-good", name="mirror.iso", preferred_provider=providers[1].descriptor.id),
    )
    transfer = await engine.submit(requests, name="mirror.iso", deduplicate=False)
    by_payload = {record.request.payload: record for record in await engine.repository.requests(transfer.id)}
    bad_record = by_payload["structural-bad"]
    good_record = by_payload["viable-good"]

    # The structurally-incapable source reaches the empty-canonical bootstrap
    # decision FIRST while its sibling is still unresolved -- it must hold,
    # never seed, and must not be durably marked "independent" while the
    # sibling remains capable.
    await engine._resolve(bad_record)
    assert len(await engine.repository.artifacts(transfer.id)) == 0
    refreshed_bad = next(
        item for item in await engine.repository.requests(transfer.id) if item.id == bad_record.id
    )
    assert refreshed_bad.state == "materializing"
    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT equivalence_disposition FROM transfer_requests WHERE id=?", (bad_record.id,),
        )
    assert row["equivalence_disposition"] == "bootstrap_unprovable"

    await engine._resolve(good_record)
    artifacts = await engine.repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    assert artifacts[0].request_id == good_record.id  # the capable source seeded, never the structural one.

    # The structural source's own later turn now sees a real canonical and
    # falls back to the unchanged, already-approved steady-state precedent:
    # it cannot sample either side of the pairing, so it independently
    # materializes as its own separate artifact -- never blocking, never
    # incorrectly merging into, the good source's canonical.
    refreshed_bad = next(
        item for item in await engine.repository.requests(transfer.id) if item.id == bad_record.id
    )
    await engine._process_request(refreshed_bad)
    artifacts_after = await engine.repository.artifacts(transfer.id)
    assert len(artifacts_after) == 2
    assert {item.request_id for item in artifacts_after} == {bad_record.id, good_record.id}


async def test_all_structurally_unprovable_cohort_reaches_degraded_fallback_without_deadlock(tmp_path, monkeypatch):
    """Gate 9 review follow-up (Finding 1, revision 2): when EVERY member of
    a same-transfer cohort is structurally, permanently unable to produce
    self-evidence, holding forever would deadlock the cohort (Case E) for no
    safety benefit -- no amount of waiting ever produces a capable sibling.
    The last member to reach its own bootstrap turn, once every other
    sibling has already durably confirmed the identical structural
    incapability, must fall back to a deterministic degraded materialization
    instead of holding indefinitely."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "all-structural.sqlite3")
    await database.init_db()
    providers = tuple(_UnknownSizeProvider(f"all-structural-{index}") for index in range(1, 4))
    repository, engine, executor = _build_unknown_size_runtime(tmp_path, monkeypatch, providers)
    await engine.initialize()

    async def fingerprint(candidate):
        return None  # structurally unroutable for every source -- never transient.

    monkeypatch.setattr(executor, "fingerprint", fingerprint)

    requests = tuple(
        TransferRequest("parcel", f"all-structural-{index}", name="mirror.iso",
                         preferred_provider=provider.descriptor.id)
        for index, provider in enumerate(providers, start=1)
    )
    transfer = await engine.submit(requests, name="mirror.iso", deduplicate=False)
    records = await engine.repository.requests(transfer.id)

    # Resolve two of the three -- with a capable-looking (still-unresolved)
    # third sibling outstanding, neither may seed yet.
    await engine._resolve(records[0])
    assert len(await engine.repository.artifacts(transfer.id)) == 0
    await engine._resolve(records[1])
    assert len(await engine.repository.artifacts(transfer.id)) == 0
    async with database.get_db() as db:
        for record in records[:2]:
            row = await db.fetchone(
                "SELECT equivalence_disposition FROM transfer_requests WHERE id=?", (record.id,),
            )
            assert row["equivalence_disposition"] == "bootstrap_unprovable"

    # The third and final sibling now finds no remaining capable sibling --
    # every other member has already durably confirmed the same structural
    # incapability -- so it materializes as the deterministic degraded
    # fallback rather than holding forever.
    await engine._resolve(records[2])
    artifacts = await engine.repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    assert artifacts[0].request_id == records[2].id

    # The two that held now independently fall back on their own later turn
    # (the unchanged steady-state precedent), never a duplicate/merged writer.
    for record in records[:2]:
        refreshed = next(item for item in await engine.repository.requests(transfer.id) if item.id == record.id)
        await engine._process_request(refreshed)
    artifacts_final = await engine.repository.artifacts(transfer.id)
    assert len(artifacts_final) == 3  # every member ends up its own independent writer -- no deadlock.
    assert {item.request_id for item in artifacts_final} == {record.id for record in records}


async def test_bad_first_structural_bootstrap_treats_unnamed_sibling_as_unknown(tmp_path, monkeypatch):
    """Gate 9 review follow-up (Finding 1, revision 3): two tightly related
    edge cases in the structural bootstrap branch.

    First, an UNRESOLVED sibling with no declared pre-resolution name has an
    UNKNOWN logical identity, not a known-different one -- it must still
    count as a potential competitor (Case D), never let the structural
    source read "no name yet" as "definitely a different file" and take the
    degraded fallback early.

    Second, once this record's own structural incapability is durably
    recorded (``equivalence_disposition='bootstrap_unprovable'``), repeated
    scheduler passes while the sibling remains unresolved must NOT
    re-fingerprint the permanently-unsampleable candidate again -- only the
    sibling-capability question is re-evaluated (quiescence)."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "bad-first-unnamed-sibling.sqlite3")
    await database.init_db()
    providers = (_UnknownSizeProvider("structural-bad-unnamed"), _UnknownSizeProvider("unnamed-good"))
    repository, engine, executor = _build_unknown_size_runtime(tmp_path, monkeypatch, providers)
    await engine.initialize()

    call_counts = {"bad": 0, "good": 0}
    label_by_provider = {providers[0].descriptor.id: "bad", providers[1].descriptor.id: "good"}

    async def fingerprint(candidate):
        call_counts[label_by_provider[candidate.provider_id]] += 1
        if label_by_provider[candidate.provider_id] == "bad":
            return None  # structurally unroutable -- never transient.
        return ArtifactFingerprint(4, "prefix-sig", FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                    "range_ignored", "prefix-sig")

    monkeypatch.setattr(executor, "fingerprint", fingerprint)

    requests = (
        TransferRequest("parcel", "structural-bad-unnamed", name="mirror.iso",
                         preferred_provider=providers[0].descriptor.id),
        # Deliberately no ``name`` -- a real, unresolved request whose
        # eventual logical identity is genuinely not yet known.
        TransferRequest("parcel", "unnamed-good", preferred_provider=providers[1].descriptor.id),
    )
    transfer = await engine.submit(requests, name="mixed-bundle", deduplicate=False)
    by_payload = {record.request.payload: record for record in await engine.repository.requests(transfer.id)}
    bad_record = by_payload["structural-bad-unnamed"]
    good_record = by_payload["unnamed-good"]
    assert good_record.request.name == ""  # genuinely no declared name yet.
    assert good_record.state == "pending"  # deliberately left unresolved throughout the hold.

    await engine._resolve(bad_record)
    assert len(await engine.repository.artifacts(transfer.id)) == 0
    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT equivalence_disposition FROM transfer_requests WHERE id=?", (bad_record.id,),
        )
    assert row["equivalence_disposition"] == "bootstrap_unprovable"
    calls_after_determination = dict(call_counts)
    assert calls_after_determination["bad"] >= 1

    # Repeatedly re-process the held structural request while the unnamed
    # sibling stays unresolved. It must keep holding (unknown identity still
    # counts as a potential competitor) WITHOUT any further fingerprint call
    # (the structural fact is already durable -- quiescence).
    for _ in range(4):
        refreshed_bad = next(
            item for item in await engine.repository.requests(transfer.id) if item.id == bad_record.id
        )
        assert refreshed_bad.state == "materializing"
        await engine._process_request(refreshed_bad)
    assert len(await engine.repository.artifacts(transfer.id)) == 0  # still no arrival-order writer.
    assert call_counts == calls_after_determination  # zero additional fingerprint calls while holding.

    # The previously-unnamed sibling now resolves (to its own, unrelated
    # identity) and -- being the only remaining capable member -- seeds the
    # cohort's canonical normally.
    await engine._resolve(good_record)
    artifacts = await engine.repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    assert artifacts[0].request_id == good_record.id

    # The structural source's own later turn now falls back through the
    # unchanged steady-state precedent, independently materializing.
    refreshed_bad = next(item for item in await engine.repository.requests(transfer.id) if item.id == bad_record.id)
    await engine._process_request(refreshed_bad)
    artifacts_after = await engine.repository.artifacts(transfer.id)
    assert len(artifacts_after) == 2
    assert {item.request_id for item in artifacts_after} == {bad_record.id, good_record.id}


async def _converge_three_unknown_size_mirrors(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "failover.sqlite3")
    await database.init_db()
    providers = tuple(_UnknownSizeProvider(f"unknown-size-{index}") for index in range(1, 4))
    now = [1000.0]
    repository, engine, executor = _build_unknown_size_runtime(tmp_path, monkeypatch, providers, now=lambda: now[0])
    await engine.initialize()

    transfer_ids = []
    for index, provider in enumerate(providers, start=1):
        transfer = await engine.submit(
            (TransferRequest("parcel", f"mirror-{index}", name="ubuntu.iso",
                             preferred_provider=provider.descriptor.id),),
            name="ubuntu.iso", deduplicate=False,
        )
        transfer_ids.append(transfer.id)

    for _ in range(6):
        await engine.resolve_pending()
    consolidated = {
        tid for tid in transfer_ids
        if (await repository.get(tid)).state == TransferState.CONSOLIDATED
    }
    assert len(consolidated) == 2
    winner_id = next(tid for tid in transfer_ids if tid not in consolidated)
    return repository, engine, executor, transfer_ids, winner_id, now


async def test_manual_failover_switches_among_converged_general_http_candidates(tmp_path, monkeypatch):
    """Section 13.1, 13.3, 13.4, 13.6 (real machinery, DP 1.0.12 topology).

    Once independent General-HTTP-shaped sources have converged to one
    canonical artifact, the EXISTING provider-neutral manual candidate-switch
    owner (transfers/manual_failover.py, unchanged by this correction) must:
      * expose the same per-candidate presentation structure the group/
        manual-failover UI already reads (13.1/13.3);
      * successfully activate a different already-bound candidate without
        creating a new artifact or transfer (13.4);
      * keep every candidate's provenance/origin intact across the switch
        (13.6).
    No new HTTP-specific switch endpoint is exercised or required."""
    from transfers.manual_failover import manual_candidate_failover

    repository, engine, _executor, transfer_ids, winner_id, _now = await _converge_three_unknown_size_mirrors(
        tmp_path, monkeypatch,
    )
    await engine.reconcile_executions()
    artifact = (await repository.artifacts(winner_id))[0]
    assert len(artifact.candidates) == 3
    assert artifact.selected == 0
    original_candidate = artifact.candidates[0]
    target_candidate = artifact.candidates[1]

    presentation_before = await repository.presentation(winner_id, details=True)
    candidate_rows = {
        row["candidate_id"]: row for row in presentation_before["files"][0]["acquisition_candidates"]
    }
    assert len(candidate_rows) == 3  # 13.1/13.3: same per-file candidate presentation structure.
    assert candidate_rows[str(original_candidate.id)]["is_active"] is True
    assert candidate_rows[str(target_candidate.id)]["switch_eligible"] is True

    result = await manual_candidate_failover(engine, winner_id, artifact.id, str(target_candidate.id))
    assert result["candidate_id"] == str(target_candidate.id)

    switched = (await repository.artifacts(winner_id))[0]
    assert switched.id == artifact.id  # 13.4: same artifact, no new artifact/transfer created.
    assert switched.transfer_id == winner_id
    assert switched.selected == 1

    # 13.6: provenance for every original source remains durable and correct
    # after the switch -- nothing was inferred from the endpoint URL.
    for candidate in artifact.candidates:
        origin = await engine.canonical.origin_for(switched, candidate)
        assert origin is not None
        assert origin.contributing_transfer_id in transfer_ids

    presentation_after = await repository.presentation(winner_id, details=True)
    rows_after = {
        row["candidate_id"]: row for row in presentation_after["files"][0]["acquisition_candidates"]
    }
    assert rows_after[str(target_candidate.id)]["is_active"] is True
    assert rows_after[str(original_candidate.id)]["switch_eligible"] is True


async def test_automatic_failover_reuses_stored_alternate_candidate(tmp_path, monkeypatch):
    """Section 13.5: a remote-source execution failure on the currently
    selected General-HTTP-shaped candidate must be recovered by the EXISTING
    neutral recovery policy automatically activating one of the other 9 (here
    2) already-bound candidates -- never by creating a second transfer or
    requiring a fresh user submission. This is the exact production
    mechanism proven generically in
    test_universal_lifecycle.py::test_mirrors_share_one_artifact_and_failover_retires_partial_bytes;
    this test's job is to confirm it also fires correctly for the
    corrected cross-transfer, unknown-size-origin topology."""
    from transfers.errors import Category, Domain, NormalizedError, Recovery, Retryability, Stage
    from transfers.models import ExecutionState

    repository, engine, executor, transfer_ids, winner_id, now = await _converge_three_unknown_size_mirrors(
        tmp_path, monkeypatch,
    )
    await engine.reconcile_executions()
    artifact = (await repository.artifacts(winner_id))[0]
    assert artifact.selected == 0
    assert artifact.execution is not None
    original_candidate_id = artifact.candidates[0].id

    error = NormalizedError(Domain.NETWORK, Category.REMOTE_READ_FAILED, Stage.EXECUTION,
                            Retryability.BACKOFF, Recovery.TRY_ALTERNATE_CANDIDATE)
    executor.jobs[artifact.execution.attempt_id] = replace(
        executor.jobs[artifact.execution.attempt_id], state=ExecutionState.FAILED, error=error,
    )

    switched_selected = None
    for _ in range(20):
        await engine.tick()
        current = (await repository.artifacts(winner_id))[0]
        if current.selected != 0:
            switched_selected = current.selected
            break
        executor.start_errors = [error]
        now[0] += 5  # retry_at gating requires the clock to actually advance between ticks.

    assert switched_selected is not None  # Section 13.5: automatic candidate rotation occurred.
    recovered = (await repository.artifacts(winner_id))[0]
    assert recovered.id == artifact.id  # same artifact/transfer -- no new transfer created.
    assert recovered.transfer_id == winner_id
    new_candidate_id = recovered.candidates[recovered.selected].id
    assert new_candidate_id != original_candidate_id

    origin = await engine.canonical.origin_for(recovered, recovered.candidates[recovered.selected])
    assert origin is not None  # 13.6: recovery execution points at correct provenance-backed candidate.


async def test_completion_preserves_consolidation_and_hides_dead_switch_controls(tmp_path, monkeypatch):
    """Section 13.7: once the canonical artifact for a converged General-
    HTTP-shaped source group completes, the canonical artifact remains the
    actual delivered artifact, the two contributing transfers remain
    consolidated/history-bearing, candidate history stays available, and the
    presentation layer's own switch_eligible flag (which the Details UI
    treats as the sole authority for whether the interactive chooser is
    live) goes false for every candidate -- reusing the exact
    completed/terminal-non-actionable semantics fixed in the immediately
    preceding UI pass (see CLAUDE.md Sec. 3, 2026-09-11 commit 5b55df6c)
    rather than inventing a new one."""
    repository, engine, executor, transfer_ids, winner_id, _now = await _converge_three_unknown_size_mirrors(
        tmp_path, monkeypatch,
    )
    await engine.reconcile_executions()
    artifact = (await repository.artifacts(winner_id))[0]
    executor.finish(artifact.execution)
    await engine.reconcile_executions()

    completed_transfer = await repository.get(winner_id)
    assert completed_transfer.state == TransferState.COMPLETED

    for tid in transfer_ids:
        if tid == winner_id:
            continue
        assert (await repository.get(tid)).state == TransferState.CONSOLIDATED
        info = await engine.canonical.consolidation(tid)
        assert info["state"] == "complete"
        assert info["consolidated_into"] == winner_id

    view = await repository.presentation(winner_id, details=True)
    assert view["status"] == "completed"
    candidate_rows = view["files"][0]["acquisition_candidates"]
    assert len(candidate_rows) == 3  # candidate history remains available.
    assert all(row["switch_eligible"] is False for row in candidate_rows)  # no dead interactive controls.
