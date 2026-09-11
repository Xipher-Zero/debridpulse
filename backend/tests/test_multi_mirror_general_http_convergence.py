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
from transfers.engine import TransferEngine
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
    """DP 1.0.12 corrective Section 5/21 Example B (deterministic, fast,
    process-free fixture modeling the live runtime capture that motivated
    this correction): one Quick-Add-shaped batch of 10 sibling requests where
    6 sources obtain full/strong proof for one canonical artifact (the
    original acquisition plus 5 attached alternates) while 4 receive
    transient proof inability (range_unsupported / dns_failure) that
    exhausts the existing bounded retry budget
    (transfers/cohorts.py:_PROOF_RETRY_BUDGET == 2) must:
      * remain ONE transfer throughout -- never split into 6 or 10 top-level
        transfers (Section 5, Invariant 1/2/9);
      * converge the 6 provable siblings onto one canonical artifact;
      * let the 4 transiently-unprovable siblings independently materialize
        as their own artifacts, INSIDE that same transfer, once bounded
        proof is exhausted -- transient inability is never treated as
        contradictory evidence (Invariant 8);
      * leave exact durable equivalence_reason/equivalence_disposition
        explainable for every transient sibling after exhaustion.
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
        assert final_records[record.id].state != "materializing"  # independently materialized after exhaustion.

    async with database.get_db() as db:
        for record in transient_records:
            row = await db.fetchone(
                "SELECT equivalence_disposition,equivalence_reason FROM transfer_requests WHERE id=?",
                (record.id,),
            )
            assert row["equivalence_disposition"] == "exhausted"
            assert row["equivalence_reason"] in {"range_unsupported", "dns_failure"}

    artifacts_after = await engine.repository.artifacts(transfer.id)
    assert len(artifacts_after) == 5  # 1 canonical (6 proven sources) + 4 independent (Example B topology).
    non_canonical = [item for item in artifacts_after if item.id != canonical_artifact.id]
    assert len(non_canonical) == 4
    for artifact in non_canonical:
        assert len(artifact.candidates) == 1  # each transient sibling kept its own single, unmerged candidate.

    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT COUNT(*) AS n FROM artifact_consolidations WHERE source_transfer_id=?",
            (transfer.id,),
        )
    assert int(row["n"]) == 0  # Invariant 9: no same-transfer artifact_consolidations rows.

    final_transfer = await engine.repository.get(transfer.id)
    assert final_transfer.state != TransferState.CONSOLIDATED  # Invariant 4: sibling convergence != transfer consolidation.


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
