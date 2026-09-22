"""DP 1.0.12 Section 11/12/13 real-runtime proof.

Deterministic local fixture exercising the *real* General HTTP resolution +
bounded-sampler path (``services.artifact_sampling.sampled_public_artifact_fingerprint``
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
import itertools
from pathlib import Path
import re
import shutil
import socket
from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiohttp import web

import db.database as database
import executors.aria2.executor as aria2_executor_module
import transfers._engine_base as engine_base_module
import services.network_safety as network_safety
from executors.aria2.client import Aria2Service
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from fake_integrations import MemoryExecutor, ParcelProvider
from providers.general_http.provider import GeneralHttpProvider
from test_route_provider_provenance import _canonical_history_runtime
from transfers import cohorts
from transfers.mirrors import EvidenceContext
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Stage, TransferError
from transfers.models import (
    ArtifactFingerprint, ExecutionState, FingerprintKind, SourceIdentity, TransferProgress, TransferRequest,
    TransferState,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry
from transfers.requests import direct_link_filename

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
    egress = SimpleNamespace(ensure_started=_noop, job_options=lambda address, scope=None: {})
    executor = Aria2Executor(service, Aria2Configuration(str(downloads), confirmation_delay=0),
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
        # Consolidation corrective, Remediation 2: the target did not exist
        # before this execution's native start, so the real zero-byte file the
        # real aria2 wrote is execution-owned invalid residue and is retired;
        # the verified sibling's payload is untouched.
        assert not Path(zero_byte_artifact.target).exists()
        assert [item for item in runtime.downloads.rglob("*") if item.is_file() and item.stat().st_size == 0] == []
        assert Path(completed.target).read_bytes() == PAYLOAD

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


async def _durable_tables_mentioning(needle: str) -> list[str]:
    """Every durable table holding ``needle`` in any column."""
    tables_with_hits = []
    async with database.get_db() as db:
        for table in [row["name"] for row in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")]:
            columns = [column["name"] for column in await db.fetchall(f'PRAGMA table_info("{table}")')]
            if not columns:
                continue
            clause = " OR ".join(f'CAST("{column}" AS TEXT) LIKE ?' for column in columns)
            row = await db.fetchone(
                f'SELECT COUNT(*) AS n FROM "{table}" WHERE {clause}', tuple(f"%{needle}%" for _ in columns),
            )
            if int(row["n"]):
                tables_with_hits.append(table)
    return tables_with_hits


async def test_transfer_286_unresolved_alternate_never_gets_a_writer_and_does_not_poison_parent(tmp_path, monkeypatch):
    """Production transfer 286, end to end through the REAL runtime: one
    transfer, ten sibling General HTTP requests for the same logical file --
    nine mirrors serve one identical positive payload, one serves a real HTTP
    200 with ``Content-Length: 0`` and an empty body. The real bounded sampler
    reports that mirror as ``UNAVAILABLE / range_ignored`` (unresolved pairing
    evidence, not retryable). Nothing is monkeypatched below the file's shared
    loopback fixture: real ``GeneralHttpProvider``, real sampler, real
    ``Aria2Executor``, real cohort/lifecycle/repository path.

    Retryability must not decide identity: the unresolved alternate has to be
    durably HELD (``exhausted``) and never receive an independent physical
    writer -- no actionable artifact, no execution, no zero-byte / collision-
    renamed ``(2)`` file on disk, no ``materialization_failed`` outcome -- while
    the good canonical downloads normally and the parent reaches the ordinary
    successful terminal state through the existing completion policy. (The
    late zero-byte verification backstop is proven separately by
    ``test_real_aria2_zero_byte_success_never_completes_or_delivers``; this is
    the stricter, earlier invariant.)"""
    runtime = await _build_runtime(tmp_path, monkeypatch)
    path = "/" + MIRROR_FILENAME
    runtime.server.route(path, PAYLOAD, behavior="normal")
    bad_server = MirrorFixtureServer()
    try:
        await bad_server.start()
        bad_server.route(path, b"", behavior="zero_byte_success")
        good_urls = tuple(runtime.server.url(index, path) for index in range(1, 10))
        bad_url = bad_server.url(10, path)
        submitted = (*good_urls[:4], bad_url, *good_urls[4:])  # the faulty mirror is mid-batch, not first/last.
        # Production Quick Add shape (application/service.py submit_links): every
        # request declares its logical filename through the one real
        # ``direct_link_filename`` derivation.
        transfer = await runtime.engine.submit(
            tuple(TransferRequest("http", url, name=direct_link_filename(url, index))
                  for index, url in enumerate(submitted, 1)),
            source="direct_link", deduplicate=False,
        )
        records = await runtime.repository.requests(transfer.id)
        assert len(records) == 10
        bad_record = next(record for record in records if record.request.payload == bad_url)  # by source, not ordinal.
        good_ids = {record.id for record in records if record.id != bad_record.id}
        assert len(good_ids) == 9

        observed_dispositions = set()

        async def bad_request_held():
            async with database.get_db() as db:
                row = await db.fetchone(
                    """SELECT state,equivalence_disposition,equivalence_reason,retry_at
                        FROM transfer_requests WHERE id=?""",
                    (bad_record.id,),
                )
            observed_dispositions.add(str(row["equivalence_disposition"] or ""))
            assert row["equivalence_disposition"] not in {"independent", "contradictory", "released"}, (
                f"unresolved alternate was released to an independent writer: "
                f"{row['equivalence_disposition']}/{row['equivalence_reason']}"
            )
            return row if row["equivalence_disposition"] == "unverified" else None

        # The bad alternate becomes durably held/unresolved -- never independent.
        held = await runtime.until(bad_request_held, label="faulty alternate durably held as unresolved")
        assert held["equivalence_reason"] == "range_ignored"  # the real sampler's factual shape.
        assert float(held["retry_at"] or 0) == 0
        assert bad_server.hits  # the real bounded sampler genuinely probed the faulty mirror.
        hits_when_held = len(bad_server.hits)

        async def transfer_completed():
            async with database.get_db() as db:
                row = await db.fetchone(
                    "SELECT equivalence_disposition FROM transfer_requests WHERE id=?", (bad_record.id,),
                )
            observed_dispositions.add(str(row["equivalence_disposition"] or ""))
            current = await runtime.repository.get(transfer.id)
            assert current.state != TransferState.FAILED  # never poisoned by the bad alternate.
            return current if current.state == TransferState.COMPLETED else None

        final_transfer = await runtime.until(transfer_completed, label="parent reaches the normal completed state")
        assert final_transfer.state == TransferState.COMPLETED
        assert observed_dispositions <= {"", "pending", "bootstrap_unprovable", "unverified"}
        assert not observed_dispositions & {"independent", "contradictory", "released"}

        for _ in range(10):  # extra scheduler cycles: the held request stays quiescent.
            await runtime.engine.tick()
            await asyncio.sleep(0.02)
        assert len(bad_server.hits) == hits_when_held  # no proof-acquisition hot loop.

        # --- Good canonical path: exactly one physical artifact, all nine good mirrors attached.
        artifacts = await runtime.repository.artifacts(transfer.id)
        assert len(artifacts) == 1
        canonical = artifacts[0]
        assert canonical.state == "completed"
        assert canonical.expected_bytes == len(PAYLOAD) > 0
        assert Path(canonical.target).read_bytes() == PAYLOAD
        bindings = await runtime.engine.canonical.bindings(canonical.id)
        assert len(bindings) == 9
        assert {str(origin["request_id"]) for binding in bindings for origin in binding["origins"]} == good_ids

        # --- Bad alternate: preserved history, zero physical writer.
        async with database.get_db() as db:
            bad_row = await db.fetchone(
                "SELECT state,equivalence_disposition,equivalence_reason,retry_at FROM transfer_requests WHERE id=?",
                (bad_record.id,),
            )
            bad_artifacts = await db.fetchone(
                "SELECT COUNT(*) AS n FROM download_files WHERE request_id=?", (bad_record.id,),
            )
            bad_executions = await db.fetchone(
                "SELECT COUNT(*) AS n FROM execution_attempts WHERE artifact_id IN "
                "(SELECT id FROM download_files WHERE request_id=?)", (bad_record.id,),
            )
            executed_artifacts = {
                int(row["artifact_id"]) for row in await db.fetchall("SELECT artifact_id FROM execution_attempts")
            }
            provenance_artifacts = {
                int(row["artifact_id"])
                for row in await db.fetchall("SELECT artifact_id FROM execution_attempt_provenance")
            }
            bad_origins = await db.fetchone(
                "SELECT COUNT(*) AS n FROM canonical_candidate_origins WHERE request_id=?", (bad_record.id,),
            )
            delivered = await db.fetchone(
                "SELECT COUNT(*) AS n FROM execution_attempt_provenance WHERE artifact_id=? AND delivered=1",
                (canonical.id,),
            )
        assert bad_row["state"] == "materializing"  # history kept; the request was not silently deleted.
        assert bad_row["equivalence_disposition"] == "unverified"
        assert bad_row["equivalence_reason"] == "range_ignored"
        assert float(bad_row["retry_at"] or 0) == 0
        assert await runtime.repository.resolved_candidates(bad_record.id)  # resolution history preserved.
        assert int(bad_artifacts["n"]) == 0  # no actionable download_files row for the bad request.
        assert int(bad_executions["n"]) == 0  # no execution attempt for a bad-source artifact.
        assert executed_artifacts == {canonical.id}  # the canonical is the only artifact ever executed...
        assert provenance_artifacts == {canonical.id}  # ...and the only one with execution provenance.
        assert int(bad_origins["n"]) == 0
        assert int(delivered["n"]) >= 1  # the good canonical is delivered.

        # --- No downstream poisoning by a late materialization_failed outcome.
        assert await _durable_tables_mentioning("materialization_failed") == []

        # --- Filesystem truth: the original defect was externally visible on disk.
        files = [item for item in runtime.downloads.rglob("*") if item.is_file()]
        payload_files = [item for item in files if item.suffix != ".aria2"]
        assert [item.name for item in payload_files] == [Path(canonical.target).name]
        assert [item for item in files if item.stat().st_size == 0] == []  # no zero-byte target.
        assert [item.name for item in files if re.search(r" \(\d+\)", item.name)] == []  # no "(2)" collision target.
    finally:
        await bad_server.stop()
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

    async def fingerprint(subject):
        candidate = subject.candidate
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

    async def mixed_fingerprint(subject):
        candidate = subject.candidate
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
            assert row["equivalence_disposition"] == "unverified"
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
            assert row["equivalence_disposition"] == "unverified"
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

    async def fingerprint(subject):
        candidate = subject.candidate
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
            assert row["equivalence_disposition"] == "unverified"
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

    async def fingerprint(subject):
        candidate = subject.candidate
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
            assert row["equivalence_disposition"] == "unverified"
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


async def test_production_270_exhausted_identity_satisfied_by_completed_canonical_reaches_completed(tmp_path, monkeypatch):
    """DP 1.0.12 CANON-001 exhausted-identity completion policy correction,
    Required production-shaped convergence regression (Section 12): direct
    reproduction of production transfer 270's shape end to end through the
    REAL bootstrap-admission + proof-exhaustion pipeline -- the identical
    five-mirror topology as ``test_production_266_empty_bootstrap_bad_source_first``
    (4 healthy equivalent mirrors converging onto one canonical artifact + 1
    DNS-failing NUS-shaped source that exhausts identity proof) -- but driven
    all the way to physical payload completion.

    Before this correction, the parent stuck at ``QUEUED`` forever even
    though the payload fully delivered, because the durably held, proof-
    exhausted, non-writer sibling was read as an unconditional unsatisfied
    delivery obligation. This proves the parent now reaches ``COMPLETED``
    once the canonical artifact for that same logical slot completes, while
    the DNS-failing source stays truthfully ``materializing``/``exhausted``
    with zero fabricated artifact/execution/canonical provenance -- Section
    16's desired durable state for the transfer-270 shape.
    """
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "production-270.sqlite3")
    await database.init_db()
    now = [1000.0]
    providers = tuple(_UnknownSizeProvider(f"ubuntu-270-mirror-{index}") for index in range(1, 6))
    repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, providers, now=lambda: now[0],
    )
    await engine.initialize()

    async def fingerprint(subject):
        candidate = subject.candidate
        if candidate.provider_id == providers[4].descriptor.id:  # mirror E: NUS-shaped, DNS-failing.
            raise socket.gaierror("simulated DNS resolution failure for mirror E")
        return ArtifactFingerprint(4, "bounded-shared-iso-content")

    monkeypatch.setattr(executor, "fingerprint", fingerprint)

    requests = tuple(
        TransferRequest("parcel", f"ubuntu-270-mirror-{index}", name="ubuntu-26.04-desktop-amd64.iso",
                         preferred_provider=provider.descriptor.id)
        for index, provider in enumerate(providers, start=1)
    )
    transfer = await engine.submit(requests, name="ubuntu-26.04-desktop-amd64.iso", deduplicate=False)
    by_payload = {record.request.payload: record for record in await engine.repository.requests(transfer.id)}
    a_record = by_payload["ubuntu-270-mirror-1"]
    b_record, c_record, d_record = (by_payload[f"ubuntu-270-mirror-{i}"] for i in (2, 3, 4))
    e_record = by_payload["ubuntu-270-mirror-5"]

    # Mirror A seeds the ONE canonical artifact; B/C/D converge onto it as
    # durable mirrors -- four healthy sources reaching "resolved" (Section 4.2).
    await engine._resolve(a_record)
    artifacts = await engine.repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    canonical_artifact = artifacts[0]
    assert canonical_artifact.request_id == a_record.id

    for record in (b_record, c_record, d_record):
        await engine._resolve(record)
    bindings = await engine.canonical.bindings(canonical_artifact.id)
    assert len(bindings) == 4  # A+B+C+D durably attach to the same canonical artifact.

    # E (DNS-failing, NUS-shaped) drives to bounded proof exhaustion -- the
    # identical budget/shape as the existing 263/266 regressions.
    await engine._resolve(e_record)
    for _ in range(2):
        refreshed = next(item for item in await engine.repository.requests(transfer.id) if item.id == e_record.id)
        assert refreshed.state == "materializing"  # still parked pending bounded proof retry.
        now[0] = refreshed.retry_at + 0.01
        await engine._process_request(refreshed)

    final_e = next(item for item in await engine.repository.requests(transfer.id) if item.id == e_record.id)
    assert final_e.state == "materializing"  # held/unresolved -- never a second/third writer.
    async with database.get_db() as db:
        held_row = await db.fetchone(
            "SELECT equivalence_disposition,equivalence_reason,retry_at FROM transfer_requests WHERE id=?",
            (e_record.id,),
        )
    assert held_row["equivalence_disposition"] == "unverified"
    assert held_row["equivalence_reason"] == "dns_failure"
    assert float(held_row["retry_at"] or 0) == 0

    artifacts_before_completion = await engine.repository.artifacts(transfer.id)
    assert len(artifacts_before_completion) == 1  # still exactly one physical canonical artifact -- no duplicate.
    assert artifacts_before_completion[0].id == canonical_artifact.id

    # Dispatch the canonical artifact to its executor, then deliver its
    # payload -- at that moment, production transfer 270 emitted "Transfer
    # queued" instead of "Transfer completed" (Section 4.6); this is the
    # exact point that symptom occurred.
    await engine.reconcile_executions()
    dispatched_artifact = (await engine.repository.artifacts(transfer.id))[0]
    assert dispatched_artifact.execution is not None
    executor.finish(dispatched_artifact.execution)
    await engine.tick()

    transfer_after = await engine.repository.get(transfer.id)
    assert transfer_after.state == TransferState.COMPLETED
    assert transfer_after.progress == 100

    final_artifacts = await engine.repository.artifacts(transfer.id)
    assert len(final_artifacts) == 1
    assert final_artifacts[0].state == "completed"
    assert final_artifacts[0].id == canonical_artifact.id  # no numbered duplicate payload.

    # The DNS-failing source remains truthfully exhausted/unresolved with
    # zero fabricated provenance (Section 16/20).
    async with database.get_db() as db:
        final_e_row = await db.fetchone(
            "SELECT state,equivalence_disposition,equivalence_reason,equivalence_retry_count,retry_at "
            "FROM transfer_requests WHERE id=?",
            (e_record.id,),
        )
        artifact_count = await db.fetchone(
            "SELECT COUNT(*) AS n FROM download_files WHERE request_id=?", (e_record.id,),
        )
        execution_count = await db.fetchone(
            "SELECT COUNT(*) AS n FROM execution_attempts WHERE artifact_id IN "
            "(SELECT id FROM download_files WHERE request_id=?)", (e_record.id,),
        )
        origin_count = await db.fetchone(
            "SELECT COUNT(*) AS n FROM canonical_candidate_origins WHERE request_id=?", (e_record.id,),
        )
        consolidation_count = await db.fetchone(
            "SELECT COUNT(*) AS n FROM artifact_consolidations WHERE source_request_id=?", (e_record.id,),
        )
    assert final_e_row["state"] == "materializing"
    assert final_e_row["equivalence_disposition"] == "unverified"
    assert final_e_row["equivalence_reason"] == "dns_failure"
    assert int(final_e_row["equivalence_retry_count"]) == 2
    assert float(final_e_row["retry_at"] or 0) == 0
    assert int(artifact_count["n"]) == 0
    assert int(execution_count["n"]) == 0
    assert int(origin_count["n"]) == 0
    assert int(consolidation_count["n"]) == 0


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

    async def fingerprint(subject):
        candidate = subject.candidate
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

    async def fingerprint(subject):
        candidate = subject.candidate
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
    assert row["equivalence_disposition"] == "unverified"
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

    async def fingerprint(subject):
        candidate = subject.candidate
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

    async def fingerprint(subject):
        candidate = subject.candidate
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

    async def fingerprint(subject):
        candidate = subject.candidate
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

    async def fingerprint(subject):
        candidate = subject.candidate
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

    async def fingerprint2(subject):
        candidate = subject.candidate
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
    assert row["equivalence_disposition"] == "unverified"
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

    async def fingerprint(subject):
        candidate = subject.candidate
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


async def test_resolved_sibling_reverify_non_retryable_unresolved_holds_without_releasing_cohort(tmp_path, monkeypatch):
    """Transfer 286, resolved-sibling re-verification: during C's collection
    walk an already-attached sibling (B) re-verifies as ``range_ignored`` --
    unresolved pairing evidence that is NOT retryable. That must hold C
    (durable ``exhausted``, writer barrier up, no proof hot loop); it must
    never release the whole cohort to independent materialization and hand C
    a second physical writer."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "resolved-sibling-reverify-unresolved.sqlite3")
    await database.init_db()
    now = [1000.0]
    providers = tuple(_UnknownSizeProvider(f"reverify-unresolved-{label}") for label in "abcd")
    repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, providers, now=lambda: now[0],
    )
    await engine.initialize()

    call_counts = {"a": 0, "b": 0, "c": 0, "d": 0}
    label_by_provider = {provider.descriptor.id: label for provider, label in zip(providers, "abcd")}
    phase = ["bootstrap"]

    async def fingerprint(subject):
        candidate = subject.candidate
        label = label_by_provider[candidate.provider_id]
        call_counts[label] += 1
        if phase[0] == "reverify" and label == "b":
            return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="range_ignored")
        if phase[0] == "attach_full":
            return ArtifactFingerprint(4, "full-shared-content", FingerprintKind.FULL_CONTENT_SAMPLE)
        return ArtifactFingerprint(4, "prefix-shared-content", FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                    "range_ignored", "prefix-shared-content")

    monkeypatch.setattr(executor, "fingerprint", fingerprint)

    requests = tuple(
        TransferRequest("parcel", f"reverify-unresolved-{label}", name="mirror.iso",
                        preferred_provider=provider.descriptor.id)
        for label, provider in zip("abcd", providers)
    )
    transfer = await engine.submit(requests, name="mirror.iso", deduplicate=False)
    by_payload = {record.request.payload: record for record in await engine.repository.requests(transfer.id)}
    record_a, record_b = by_payload["reverify-unresolved-a"], by_payload["reverify-unresolved-b"]
    record_c, record_d = by_payload["reverify-unresolved-c"], by_payload["reverify-unresolved-d"]

    await engine._resolve(record_a)  # A bootstrap-seeds the sole canonical.
    phase[0] = "attach_full"
    await engine._resolve(record_b)  # B and D attach through the immediate FULL fast path,
    await engine._resolve(record_d)  # so the canonical's candidate order is A, B, D.
    artifacts = await engine.repository.artifacts(transfer.id)
    assert len(artifacts) == 1
    canonical = artifacts[0]
    assert len(canonical.candidates) == 3

    phase[0] = "reverify"  # C still matches via A/D (PREFIX); B now re-verifies range_ignored.
    await engine._resolve(record_c)
    for _ in range(2):
        refreshed_c = next(item for item in await engine.repository.requests(transfer.id) if item.id == record_c.id)
        now[0] = max(now[0], refreshed_c.retry_at) + 0.01
        await engine._process_request(refreshed_c)

    async with database.get_db() as db:
        rows = {
            row["id"]: row for row in await db.fetchall(
                "SELECT id,state,equivalence_disposition,equivalence_reason,retry_at FROM transfer_requests WHERE transfer_id=?",
                (transfer.id,),
            )
        }
        c_artifacts = await db.fetchone(
            "SELECT COUNT(*) AS n FROM download_files WHERE request_id=?", (record_c.id,),
        )
    assert rows[record_c.id]["equivalence_disposition"] == "exhausted"  # held, never "independent".
    assert rows[record_c.id]["equivalence_reason"] == "range_ignored"
    assert float(rows[record_c.id]["retry_at"] or 0) == 0
    assert rows[record_c.id]["state"] == "materializing"
    assert int(c_artifacts["n"]) == 0  # no independent artifact for C.
    assert not {row["equivalence_disposition"] for row in rows.values()} & {
        "independent", "contradictory", "released",
    }  # the attached siblings were not released to independence either.

    calls_at_hold = dict(call_counts)
    for _ in range(3):
        refreshed_c = next(item for item in await engine.repository.requests(transfer.id) if item.id == record_c.id)
        await engine._process_request(refreshed_c)
    assert call_counts == calls_at_hold  # quiescent: no proof hot loop while held.

    artifacts_final = await engine.repository.artifacts(transfer.id)
    assert [item.id for item in artifacts_final] == [canonical.id]
    assert len(artifacts_final[0].candidates) == 3


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

    async def fingerprint(subject):
        candidate = subject.candidate
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

    async def fingerprint(subject):
        candidate = subject.candidate
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


async def test_transfer_286_bad_source_first_is_held_once_a_canonical_exists(tmp_path, monkeypatch):
    """Transfer 286, deterministic bad-source-FIRST ordering (the real runtime
    reproducer's arrival order is scheduler-dependent, so this pins the order
    that reaches the empty-canonical bootstrap decision first). A source whose
    sampler RAN and returned unusable evidence (``range_ignored``: degenerate
    material evidence) waits during bootstrap exactly like any unprovable
    source; but once a good sibling seeds the canonical it must be HELD
    (``exhausted``) -- never handed an independent writer. Contrast
    ``test_bad_first_structural_bootstrap_waits_for_capable_sibling``: a
    sampler that reports NO capability at all keeps the degraded fallback."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "bad-first-range-ignored.sqlite3")
    await database.init_db()
    providers = (_UnknownSizeProvider("range-ignored-bad"), _UnknownSizeProvider("range-ignored-good"))
    repository, engine, executor = _build_unknown_size_runtime(tmp_path, monkeypatch, providers)
    await engine.initialize()

    async def fingerprint(subject):
        candidate = subject.candidate
        if candidate.provider_id == providers[0].descriptor.id:
            return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="range_ignored")
        return ArtifactFingerprint(4, "prefix-sig", FingerprintKind.PREFIX_CONTENT_SAMPLE,
                                    "range_ignored", "prefix-sig")

    monkeypatch.setattr(executor, "fingerprint", fingerprint)
    requests = (
        TransferRequest("parcel", "range-ignored-bad", name="mirror.iso", preferred_provider=providers[0].descriptor.id),
        TransferRequest("parcel", "range-ignored-good", name="mirror.iso", preferred_provider=providers[1].descriptor.id),
    )
    transfer = await engine.submit(requests, name="mirror.iso", deduplicate=False)
    by_payload = {record.request.payload: record for record in await engine.repository.requests(transfer.id)}
    bad_record, good_record = by_payload["range-ignored-bad"], by_payload["range-ignored-good"]

    await engine._resolve(bad_record)  # bad reaches the empty-canonical bootstrap decision first.
    assert len(await engine.repository.artifacts(transfer.id)) == 0
    await engine._resolve(good_record)  # the capable source seeds.
    artifacts = await engine.repository.artifacts(transfer.id)
    assert len(artifacts) == 1 and artifacts[0].request_id == good_record.id

    refreshed_bad = next(item for item in await engine.repository.requests(transfer.id) if item.id == bad_record.id)
    for _ in range(3):
        await engine._process_request(refreshed_bad)
    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT state,equivalence_disposition,equivalence_reason,retry_at FROM transfer_requests WHERE id=?",
            (bad_record.id,),
        )
        bad_artifacts = await db.fetchone(
            "SELECT COUNT(*) AS n FROM download_files WHERE request_id=?", (bad_record.id,),
        )
    assert row["state"] == "materializing"
    assert row["equivalence_disposition"] == "unverified"  # held, never "independent".
    assert row["equivalence_reason"] == "range_ignored"
    assert float(row["retry_at"] or 0) == 0
    assert int(bad_artifacts["n"]) == 0
    assert [item.id for item in await engine.repository.artifacts(transfer.id)] == [artifacts[0].id]


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

    async def fingerprint(subject):
        candidate = subject.candidate
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


async def test_transfer_291_exhausted_incomplete_representation_bootstrap_progresses_with_one_verified_writer(
    tmp_path, monkeypatch,
):
    """Production transfer 291, small real-runtime form: the bounded sampler
    reaches every mirror but answers ``UNAVAILABLE / incomplete_representation``
    (a short response it cannot trust), so no candidate can prove identity and
    no canonical exists. Bounded self-proof exhaustion must not strand the
    transfer waiting for another source: exactly ONE candidate becomes the
    provisional writer, downloads through the real scheduler + real aria2, and
    completes through the ordinary strict material verification (real bytes on
    disk, executor-observed size). Only the sampler seam is pinned -- the
    General HTTP provider never reports a size, so the real sampler cannot emit
    this reason here; everything else is real."""
    runtime = await _build_runtime(tmp_path, monkeypatch)
    path = "/" + MIRROR_FILENAME
    runtime.server.route(path, PAYLOAD, behavior="normal")
    executor = next(iter(runtime.engine.registry.executors.values()))
    probes = {"count": 0}

    async def incomplete_representation(subject):
        _candidate = subject.candidate
        probes["count"] += 1
        return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="incomplete_representation")

    monkeypatch.setattr(executor, "fingerprint", incomplete_representation)
    try:
        urls = (runtime.server.url(1, path), runtime.server.url(2, path))
        transfer = await runtime.engine.submit(
            tuple(TransferRequest("http", url, name=direct_link_filename(url, index))
                  for index, url in enumerate(urls, 1)),
            source="direct_link", deduplicate=False,
        )
        records = await runtime.repository.requests(transfer.id)
        assert len(records) == 2

        async def transfer_completed():
            current = await runtime.repository.get(transfer.id)
            assert current.state != TransferState.FAILED
            return current if current.state == TransferState.COMPLETED else None

        await runtime.until(transfer_completed, label="exhausted-bootstrap transfer completes without waiting for a peer")
        for _ in range(10):  # extra scheduler cycles: nothing more is sampled or started.
            await runtime.engine.tick()
            await asyncio.sleep(0.02)
        probes_after_completion = probes["count"]
        for _ in range(5):
            await runtime.engine.tick()
            await asyncio.sleep(0.02)
        assert probes["count"] == probes_after_completion  # no proof hot loop.

        # --- Exactly one writer: one artifact, one execution, one file.
        artifacts = await runtime.repository.artifacts(transfer.id)
        assert len(artifacts) == 1
        artifact = artifacts[0]
        assert artifact.state == "completed"
        assert artifact.expected_bytes == len(PAYLOAD)  # strict verification against the observed size.
        assert Path(artifact.target).read_bytes() == PAYLOAD
        async with database.get_db() as db:
            executions = await db.fetchone("SELECT COUNT(*) AS n FROM execution_attempts")
            dispositions = {
                row["id"]: str(row["equivalence_disposition"] or "")
                for row in await db.fetchall(
                    "SELECT id,equivalence_disposition FROM transfer_requests WHERE transfer_id=?", (transfer.id,),
                )
            }
        assert int(executions["n"]) == 1
        assert list(dispositions.values()).count("provisional") == 1  # one provisional writer, honestly labelled...
        assert artifact.request_id in {rid for rid, value in dispositions.items() if value == "provisional"}
        assert not set(dispositions.values()) & {"independent", "contradictory", "released"}  # ...never independent.
        files = [item for item in runtime.downloads.rglob("*") if item.is_file() and item.suffix != ".aria2"]
        assert [item.name for item in files] == [Path(artifact.target).name]
        assert [item.name for item in files if re.search(r" \(\d+\)", item.name)] == []
    finally:
        await runtime.close()


# ---------------------------------------------------------------------------
# DP 1.0.12 consolidation corrective, Remediation 1 (production 298/299/300):
# an unrelated canonical's cheap ``logical_pairing_mismatch`` must never mask a
# plausible canonical whose proof was attempted and remains unresolved.
# ---------------------------------------------------------------------------

_WRITER_AUTHORIZING_DISPOSITIONS = {"independent", "contradictory", "released", "provisional"}


class _NamedUnknownSizeProvider(_UnknownSizeProvider):
    """``_UnknownSizeProvider`` whose candidate content key follows the logical
    name, so differently named transfers are genuinely unrelated objects."""

    def candidate(self, name="ubuntu.iso", *, payload="parcel"):
        return replace(super().candidate(name, payload=payload), name=name)


async def _masking_runtime(tmp_path, monkeypatch, *, unrelated: int, incoming_reason: str, db_name: str):
    """One plausible canonical (``ubuntu.iso``) plus ``unrelated`` canonicals
    for other logical objects, all established BEFORE the plausible one so the
    repository's natural canonical order lists the unrelated ones first -- the
    exact ordering that let production USTC escape the writer barrier. The
    incoming source's own proof attempt answers ``incoming_reason``."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / f"{db_name}.sqlite3")
    await database.init_db()
    unrelated_providers = tuple(_NamedUnknownSizeProvider(f"unrelated-{index}") for index in range(unrelated))
    canonical_provider = _NamedUnknownSizeProvider("plausible-canonical")
    incoming_provider = _NamedUnknownSizeProvider("incoming-source")
    providers = (*unrelated_providers, canonical_provider, incoming_provider)
    repository, engine, executor = _build_unknown_size_runtime(tmp_path, monkeypatch, providers)
    await engine.initialize()
    probes = {"incoming": 0}

    async def fingerprint(subject):
        candidate = subject.candidate
        if candidate.provider_id == incoming_provider.descriptor.id:
            probes["incoming"] += 1
            return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason=incoming_reason)
        return ArtifactFingerprint(4, f"full:{candidate.name}", FingerprintKind.FULL_CONTENT_SAMPLE)

    monkeypatch.setattr(executor, "fingerprint", fingerprint)

    async def establish(provider, name):
        transfer = await engine.submit(
            (TransferRequest("parcel", f"{provider.descriptor.id}:{name}", name=name,
                             preferred_provider=provider.descriptor.id),),
            name=name, deduplicate=False,
        )
        (record,) = await repository.requests(transfer.id)
        await engine._resolve(record)
        (artifact,) = await repository.artifacts(transfer.id)
        return transfer, artifact

    unrelated_artifacts = [
        (await establish(provider, f"unrelated-object-{index}.bin"))[1]
        for index, provider in enumerate(unrelated_providers)
    ]
    _canonical_transfer, plausible = await establish(canonical_provider, MIRROR_FILENAME)
    incoming_transfer = await engine.submit(
        (TransferRequest("parcel", "incoming-ubuntu", name=MIRROR_FILENAME,
                         preferred_provider=incoming_provider.descriptor.id),),
        name=MIRROR_FILENAME, deduplicate=False,
    )
    (incoming_record,) = await repository.requests(incoming_transfer.id)
    return SimpleNamespace(
        repository=repository, engine=engine, executor=executor, probes=probes, plausible=plausible,
        unrelated=tuple(unrelated_artifacts), incoming_transfer=incoming_transfer, incoming_record=incoming_record,
    )


async def _assert_no_writer(runtime, *, starts_before: int) -> dict:
    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT state,equivalence_disposition,equivalence_reason,retry_at FROM transfer_requests WHERE id=?",
            (runtime.incoming_record.id,),
        )
        artifacts = await db.fetchone(
            "SELECT COUNT(*) AS n FROM download_files WHERE request_id=?", (runtime.incoming_record.id,),
        )
        executions = await db.fetchone(
            "SELECT COUNT(*) AS n FROM execution_attempts WHERE transfer_id=?", (runtime.incoming_transfer.id,),
        )
        targets = await db.fetchone(
            "SELECT COUNT(*) AS n FROM download_files WHERE torrent_id=? AND COALESCE(local_path,'')!=''",
            (runtime.incoming_transfer.id,),
        )
    assert row["equivalence_disposition"] not in _WRITER_AUTHORIZING_DISPOSITIONS, (
        f"unrelated canonical masked the plausible unresolved target: "
        f"{row['equivalence_disposition']}/{row['equivalence_reason']}"
    )
    assert row["state"] == "materializing"  # HOLD / unresolved: the request is never resolved or failed.
    assert int(artifacts["n"]) == 0  # no independent download_files row,
    assert int(targets["n"]) == 0  # no target allocation,
    assert int(executions["n"]) == 0  # no execution attempt,
    assert len([call for call in runtime.executor.calls if call[0] == "start"]) == starts_before  # no materialization.
    return row


@pytest.mark.parametrize("unrelated", [1, 2, 3])
@pytest.mark.parametrize("incoming_reason", ["range_ignored", "range_unsupported"])
async def test_unrelated_pairing_mismatch_never_masks_plausible_unresolved_canonical(
    tmp_path, monkeypatch, unrelated, incoming_reason,
):
    """Production USTC defect class. Canonical A (``ubuntu.iso``) is a
    plausible pairing whose proof is ATTEMPTED and stays unresolved
    (``range_ignored`` / ``range_unsupported``); every other canonical is an
    unrelated object answering only ``logical_pairing_mismatch``. The unrelated
    canonicals precede A in repository order. No number of them may authorize
    an independent writer."""
    runtime = await _masking_runtime(
        tmp_path, monkeypatch, unrelated=unrelated, incoming_reason=incoming_reason,
        db_name=f"masking-{unrelated}-{incoming_reason}",
    )
    starts_before = len([call for call in runtime.executor.calls if call[0] == "start"])
    await runtime.engine._resolve(runtime.incoming_record)
    assert runtime.probes["incoming"] >= 1  # proof against the plausible canonical was genuinely attempted.
    row = await _assert_no_writer(runtime, starts_before=starts_before)
    assert row["equivalence_reason"] == incoming_reason  # the plausible target's factual reason, never the mismatch.
    assert row["equivalence_reason"] != "logical_pairing_mismatch"


async def test_writer_barrier_is_independent_of_canonical_iteration_order(tmp_path, monkeypatch):
    """Arrival/iteration order must not change identity outcome: every
    permutation of {plausible-unresolved A, unrelated B, unrelated C} holds."""
    runtime = await _masking_runtime(
        tmp_path, monkeypatch, unrelated=2, incoming_reason="range_ignored", db_name="masking-permutations",
    )
    canonicals = await runtime.engine.canonical.canonical_artifacts()
    assert {item.id for item in canonicals} == {runtime.plausible.id, *(item.id for item in runtime.unrelated)}
    starts_before = len([call for call in runtime.executor.calls if call[0] == "start"])
    await runtime.engine._resolve(runtime.incoming_record)  # durable resolution history; natural order first.
    natural = await _assert_no_writer(runtime, starts_before=starts_before)
    observed = {(natural["equivalence_disposition"], natural["equivalence_reason"])}
    for ordering in itertools.permutations(canonicals):
        async def permuted(ordering=ordering):
            return tuple(ordering)

        monkeypatch.setattr(runtime.engine.canonical, "canonical_artifacts", permuted)
        async with database.get_db() as db:
            await db.execute(
                """UPDATE transfer_requests SET equivalence_disposition='',equivalence_reason=NULL,
                    equivalence_retry_count=0,retry_at=0 WHERE id=?""",
                (runtime.incoming_record.id,),
            )
            await db.commit()
        (record,) = await runtime.repository.requests(runtime.incoming_transfer.id)
        assert await cohorts.coordinate_collection(
            runtime.engine, record, await runtime.repository.resolved_candidates(record.id),
        ) is True  # HOLD for this ordering.
        row = await _assert_no_writer(runtime, starts_before=starts_before)
        observed.add((row["equivalence_disposition"], row["equivalence_reason"]))
    assert len(observed) == 1  # identical durable outcome for all six orderings.
    assert next(iter(observed))[1] == "range_ignored"


_MAPPING_TABLE = [
    # (per-canonical behaviours, expected outcome, matched label, held, single plausible label)
    (("unresolved", "mismatch", "mismatch"), "plausible_unresolved", None, True, "unresolved-0"),
    (("unresolved_transient", "mismatch"), "plausible_unresolved", None, True, "unresolved_transient-0"),
    (("match", "mismatch", "mismatch"), "match", "match-0", False, None),
    (("match", "unresolved"), "plausible_unresolved", None, True, "unresolved-1"),
    (("match", "match"), "ambiguous", None, False, None),
    (("contradictory", "mismatch"), "contradictory", None, False, None),
    (("contradictory", "unresolved"), "plausible_unresolved", None, True, "unresolved-1"),
    (("contradictory", "unprovable"), "contradictory", None, False, None),
    (("unprovable", "mismatch"), "structurally_unprovable", None, False, None),
    (("match", "unprovable"), "structurally_unprovable", None, False, None),
    (("unprovable", "unresolved"), "plausible_unresolved", None, True, "unresolved-1"),
    (("mismatch", "mismatch"), "nonpairing", None, False, None),
    (("unresolved", "unresolved", "mismatch"), "plausible_unresolved", None, True, None),
]


@pytest.mark.parametrize("behaviours,outcome,matched,held,single_plausible", _MAPPING_TABLE)
async def test_mapping_decision_table_is_order_independent(
    tmp_path, monkeypatch, behaviours, outcome, matched, held, single_plausible,
):
    """The full Remediation 1 decision table, through the real ``_mapping`` +
    ``transfers.mirrors.shared_evidence`` path, for EVERY iteration order of
    the canonical set: same matched target, same HOLD / INDEPENDENT /
    CONTRADICTORY reading, same single plausible (unverified) target."""
    labels = tuple(f"{behaviour}-{index}" for index, behaviour in enumerate(behaviours))
    providers = {label: _NamedUnknownSizeProvider(label) for label in labels}
    incoming_provider = _NamedUnknownSizeProvider("incoming")
    _repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, (*providers.values(), incoming_provider),
    )

    async def fingerprint(subject):
        candidate = subject.candidate
        behaviour = candidate.provider_id.rsplit("-", 1)[0]
        if behaviour == "unresolved":
            return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="range_ignored")
        if behaviour == "unresolved_transient":
            return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="range_unsupported")
        if behaviour == "unprovable":
            return None  # no proof capability for this route: sampler_unsupported.
        signature = "other-content" if behaviour == "contradictory" else "shared-content"
        return ArtifactFingerprint(4, signature, FingerprintKind.FULL_CONTENT_SAMPLE)

    monkeypatch.setattr(executor, "fingerprint", fingerprint)
    canonicals = tuple(
        SimpleNamespace(
            id=index + 1, expected_bytes=0, label=label,
            candidates=(providers[label].candidate(
                "unrelated.bin" if label.startswith("mismatch") else MIRROR_FILENAME),),
        )
        for index, label in enumerate(labels)
    )
    incoming = (incoming_provider.candidate(MIRROR_FILENAME),)

    readings = set()
    for ordering in itertools.permutations(canonicals):
        result = await cohorts._mapping(ordering, incoming, engine.registry)
        readings.add((
            result.outcome,
            result.primary.label if result.matched else None,
            cohorts._must_hold(result),
            cohorts._released_disposition(result) if not result.matched and not cohorts._must_hold(result) else None,
            tuple(item.label for item in result.plausible),
            result.evidence.reason,
        ))
    assert len(readings) == 1  # identical for every canonical iteration order.
    (got_outcome, got_matched, got_held, released, plausible, _reason), = readings
    assert (got_outcome, got_matched, got_held) == (outcome, matched, held)
    if outcome == "contradictory":
        assert released == "contradictory"  # the verified-distinct path is preserved.
    elif not held and matched is None:
        assert released == "independent"  # non-pairing / structurally unprovable / ambiguous degraded fallback.
    assert (plausible[0] if len(plausible) == 1 else None) == single_plausible
    if held:
        assert plausible  # a HOLD always names the plausible target(s) it is holding for.


async def test_candidate_order_within_a_canonical_never_masks_unresolved_proof(tmp_path, monkeypatch):
    """Candidate order, like canonical order, must not change the outcome: a
    cheap ``non_independent_source`` rejection against one member of a
    canonical cannot mask the attempted-but-unresolved proof against another
    member of that SAME canonical, in either candidate order."""
    member = _NamedUnknownSizeProvider("unresolved-member")
    incoming_provider = _NamedUnknownSizeProvider("incoming")
    unrelated = _NamedUnknownSizeProvider("mismatch-0")
    _repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, (member, incoming_provider, unrelated),
    )

    async def fingerprint(subject):
        candidate = subject.candidate
        if candidate.provider_id == member.descriptor.id:
            return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="range_ignored")
        return ArtifactFingerprint(4, "shared-content", FingerprintKind.FULL_CONTENT_SAMPLE)

    monkeypatch.setattr(executor, "fingerprint", fingerprint)
    same_source = incoming_provider.candidate(MIRROR_FILENAME)  # same source scope as the incoming route.
    unresolved = member.candidate(MIRROR_FILENAME)
    incoming = (incoming_provider.candidate(MIRROR_FILENAME),)
    other = SimpleNamespace(id=2, expected_bytes=0, candidates=(unrelated.candidate("unrelated.bin"),))

    readings = set()
    for members in itertools.permutations((same_source, unresolved)):
        plausible = SimpleNamespace(id=1, expected_bytes=0, candidates=tuple(members))
        for ordering in itertools.permutations((plausible, other)):
            result = await cohorts._mapping(ordering, incoming, engine.registry)
            readings.add((result.outcome, cohorts._must_hold(result), result.evidence.reason,
                          tuple(item.id for item in result.plausible)))
    assert readings == {("plausible_unresolved", True, "range_ignored", (1,))}


# ---------------------------------------------------------------------------
# DP 1.0.12 consolidation corrective, Remediation 2: invalid material left by a
# nominally successful execution is retired ONLY under positive, durable
# execution ownership of the exact target -- never because verification failed.
# ---------------------------------------------------------------------------

async def _nominal_success_runtime(tmp_path, monkeypatch, *, db_name: str, before_start=None):
    """One request materialized and dispatched through the real engine; the
    fake executor then reports ``SUCCEEDED`` for whatever the test left on disk.
    ``before_start(target, sidecar)`` runs after the canonical target is
    allocated and BEFORE native start authority -- i.e. pre-existing material."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / f"{db_name}.sqlite3")
    await database.init_db()
    provider = _NamedUnknownSizeProvider("nominal-success")
    repository, engine, executor = _build_unknown_size_runtime(tmp_path, monkeypatch, (provider,))
    await engine.initialize()
    transfer = await engine.submit(
        (TransferRequest("parcel", "nominal-success", name=MIRROR_FILENAME,
                         preferred_provider=provider.descriptor.id),),
        name=MIRROR_FILENAME, deduplicate=False,
    )
    (record,) = await repository.requests(transfer.id)
    await engine._resolve(record)
    (artifact,) = await repository.artifacts(transfer.id)
    target = Path(artifact.target)
    sidecar = Path(executor.sidecar(artifact.target))
    target.parent.mkdir(parents=True, exist_ok=True)
    if before_start is not None:
        before_start(target, sidecar)
    await engine.reconcile_executions()  # final execution admission + native start.
    (artifact,) = await repository.artifacts(transfer.id)
    assert artifact.execution is not None
    return SimpleNamespace(repository=repository, engine=engine, executor=executor, transfer=transfer,
                           artifact=artifact, target=target, sidecar=sidecar)


async def _report_success(runtime, *, total: int):
    handle = runtime.artifact.execution
    runtime.executor.jobs[handle.attempt_id] = replace(
        runtime.executor.jobs[handle.attempt_id], state=ExecutionState.SUCCEEDED,
        progress=TransferProgress(total, total),
    )
    await runtime.engine.reconcile_executions()
    (artifact,) = await runtime.repository.artifacts(runtime.transfer.id)
    async with database.get_db() as db:
        attempt = await db.fetchone("SELECT * FROM execution_attempts WHERE id=?", (handle.attempt_id,))
        outcomes = await db.fetchall(
            "SELECT kind,payload FROM transfer_outcomes WHERE transfer_id=? AND attempt_id=?",
            (runtime.transfer.id, handle.attempt_id),
        )
    return artifact, attempt, outcomes


def _assert_verification_failure_recorded(artifact, outcomes):
    assert artifact.state != "completed"  # nominal success never completes.
    assert artifact.state == "error"
    assert artifact.error is not None
    assert (artifact.error.domain, artifact.error.category, artifact.error.stage) == (
        Domain.INTEGRITY, Category.MATERIALIZATION_FAILED, Stage.VERIFICATION,
    )
    assert [row["kind"] for row in outcomes] == ["failure"]  # failure provenance is kept.
    assert "materialization_failed" in outcomes[0]["payload"]


def _adjacent_unrelated_files(target: Path) -> dict[Path, bytes]:
    """Legitimately unrelated files at colliding / adjacent paths."""
    files = {
        target.with_name(f"{target.stem} (2){target.suffix}"): b"someone else's (2) payload",
        target.with_name(target.name + ".bak"): b"operator backup",
        target.with_name(target.name + ".other-progress"): b"another tool's sidecar",
        target.with_name("." + target.name): b"hidden neighbour",
        target.parent / "unrelated.bin": b"unrelated payload",
    }
    for path, content in files.items():
        path.write_bytes(content)
    return files


async def test_execution_owned_zero_byte_success_is_retired_with_its_sidecar(tmp_path, monkeypatch):
    runtime = await _nominal_success_runtime(tmp_path, monkeypatch, db_name="owned-zero-byte")
    neighbours = _adjacent_unrelated_files(runtime.target)
    runtime.target.write_bytes(b"")  # the production USTC shape: a real zero-byte "success".
    runtime.sidecar.write_bytes(b"resume-state")
    artifact, attempt, outcomes = await _report_success(runtime, total=0)
    _assert_verification_failure_recorded(artifact, outcomes)
    assert not runtime.target.exists()  # the execution-owned invalid target is removed,
    assert not runtime.sidecar.exists()  # and so is its execution-owned resumable sidecar.
    assert int(attempt["target_initially_absent"]) == 1  # durable fact, captured at execution admission.
    for path, content in neighbours.items():  # colliding / adjacent unrelated files are never touched.
        assert path.read_bytes() == content


@pytest.mark.parametrize("preexisting", ["target", "sidecar", "both"])
async def test_preexisting_target_material_is_never_deleted_on_verification_failure(tmp_path, monkeypatch, preexisting):
    def before_start(target, sidecar):
        if preexisting in {"target", "both"}:
            target.write_bytes(b"")  # present before native start -- even though it is zero bytes.
        if preexisting in {"sidecar", "both"}:
            sidecar.write_bytes(b"legitimate prior resume state")

    runtime = await _nominal_success_runtime(
        tmp_path, monkeypatch, db_name=f"preexisting-{preexisting}", before_start=before_start,
    )
    neighbours = _adjacent_unrelated_files(runtime.target)
    existed = {path: path.read_bytes() for path in (runtime.target, runtime.sidecar) if path.exists()}
    artifact, attempt, outcomes = await _report_success(runtime, total=0)
    _assert_verification_failure_recorded(artifact, outcomes)
    assert int(attempt["target_initially_absent"]) == 0  # no ownership authority: fail closed, delete nothing.
    for path, content in {**existed, **neighbours}.items():
        assert path.read_bytes() == content


async def test_historical_attempt_without_ownership_fact_never_deletes(tmp_path, monkeypatch):
    """A pre-change execution row carries NULL: ownership is unknown, so the
    invalid material is left exactly where it is."""
    runtime = await _nominal_success_runtime(tmp_path, monkeypatch, db_name="historical-null")
    async with database.get_db() as db:
        await db.execute(
            "UPDATE execution_attempts SET target_initially_absent=NULL,material_owner_attempt_id=NULL WHERE id=?",
            (runtime.artifact.execution.attempt_id,),
        )
        await db.commit()
    runtime.target.write_bytes(b"")
    runtime.sidecar.write_bytes(b"resume-state")
    artifact, _attempt, outcomes = await _report_success(runtime, total=0)
    _assert_verification_failure_recorded(artifact, outcomes)
    assert runtime.target.read_bytes() == b"" and runtime.sidecar.read_bytes() == b"resume-state"


async def test_verified_success_is_unchanged_by_ownership_cleanup(tmp_path, monkeypatch):
    runtime = await _nominal_success_runtime(tmp_path, monkeypatch, db_name="verified-success")
    neighbours = _adjacent_unrelated_files(runtime.target)
    runtime.target.write_bytes(b"done")
    artifact, attempt, outcomes = await _report_success(runtime, total=4)
    assert artifact.state == "completed" and artifact.expected_bytes == 4
    assert runtime.target.read_bytes() == b"done"
    assert int(attempt["target_initially_absent"]) == 1
    assert [row["kind"] for row in outcomes if row["kind"] == "failure"] == []
    for path, content in neighbours.items():
        assert path.read_bytes() == content


async def test_cleanup_failure_never_rewrites_verification_history(tmp_path, monkeypatch):
    runtime = await _nominal_success_runtime(tmp_path, monkeypatch, db_name="cleanup-failure")
    runtime.target.write_bytes(b"")

    retired = []

    def failing_retire(root, plan, footprint, *, owned):
        retired.append((str(plan.target), tuple(str(item) for item in footprint.transient_paths)))
        raise TransferError(NormalizedError(Domain.LOCAL_RESOURCE, Category.LOCAL_CLEANUP_FAILED, Stage.CLEANUP))

    monkeypatch.setattr(engine_base_module, "retire_materialization", failing_retire)
    artifact, _attempt, outcomes = await _report_success(runtime, total=0)
    _assert_verification_failure_recorded(artifact, outcomes)  # still MATERIALIZATION_FAILED, never completed.
    # The one hardened primitive was asked for exactly this execution's target and declared sidecar -- nothing else.
    assert retired == [(str(runtime.target), (str(runtime.sidecar),))]
    assert runtime.target.exists()


# ---------------------------------------------------------------------------
# DP 1.0.12 consolidation corrective, Remediation 3: one coordination decision
# never re-acquires evidence it already holds, stops at decisive proof, and
# keeps nothing for the next decision. Deterministic call counts only.
# ---------------------------------------------------------------------------

async def _established_canonical(tmp_path, monkeypatch, *, members: int, db_name: str, incoming_behaviour):
    """A canonical with ``members`` verified candidates, plus one not-yet
    coordinated incoming request. ``incoming_behaviour(candidate)`` answers the
    incoming route's fingerprint once the canonical is established."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / f"{db_name}.sqlite3")
    await database.init_db()
    member_providers = tuple(_NamedUnknownSizeProvider(f"member-{index}") for index in range(members))
    incoming_provider = _NamedUnknownSizeProvider("incoming-source")
    repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, (*member_providers, incoming_provider),
    )
    await engine.initialize()
    calls = []
    phase = ["establish"]

    async def fingerprint(subject):
        candidate = subject.candidate
        if phase[0] == "count":
            calls.append(candidate.provider_id)
        if candidate.provider_id == incoming_provider.descriptor.id:
            return incoming_behaviour(candidate)
        return ArtifactFingerprint(4, "shared-content", FingerprintKind.FULL_CONTENT_SAMPLE)

    monkeypatch.setattr(executor, "fingerprint", fingerprint)
    transfer = await engine.submit(
        tuple(TransferRequest("parcel", f"member-{index}", name=MIRROR_FILENAME,
                              preferred_provider=provider.descriptor.id)
              for index, provider in enumerate(member_providers)),
        name=MIRROR_FILENAME, deduplicate=False,
    )
    for record in await repository.requests(transfer.id):
        await engine._resolve(record)
    (canonical,) = await repository.artifacts(transfer.id)
    assert len(canonical.candidates) == members
    incoming_transfer = await engine.submit(
        (TransferRequest("parcel", "incoming", name=MIRROR_FILENAME,
                         preferred_provider=incoming_provider.descriptor.id),),
        name=MIRROR_FILENAME, deduplicate=False,
    )
    (incoming_record,) = await repository.requests(incoming_transfer.id)
    phase[0] = "count"
    return SimpleNamespace(repository=repository, engine=engine, executor=executor, calls=calls,
                           canonical=canonical, incoming_transfer=incoming_transfer,
                           incoming_record=incoming_record, incoming_id=incoming_provider.descriptor.id)


async def test_decisive_proof_stops_traversal_and_acquires_each_fingerprint_once(tmp_path, monkeypatch):
    runtime = await _established_canonical(
        tmp_path, monkeypatch, members=5, db_name="decisive-early-stop",
        incoming_behaviour=lambda candidate: ArtifactFingerprint(4, "shared-content", FingerprintKind.FULL_CONTENT_SAMPLE),
    )
    await runtime.engine._resolve(runtime.incoming_record)
    info = await runtime.engine.canonical.consolidation(runtime.incoming_transfer.id)
    assert info["state"] == "complete"  # same semantics: consolidated on the first decisive proof.
    assert len(await runtime.engine.canonical.bindings(runtime.canonical.id)) == 6
    # One full proof against the first verified member is decisive: the incoming route and that one member are
    # each fingerprinted exactly once; the other four members are never sampled (was 5 + 5 acquisitions).
    assert sorted(runtime.calls) == sorted([runtime.incoming_id, "member-0"])


async def test_undecisive_decision_acquires_each_candidate_once_and_next_decision_reacquires(tmp_path, monkeypatch):
    runtime = await _established_canonical(
        tmp_path, monkeypatch, members=5, db_name="memoized-unresolved",
        incoming_behaviour=lambda candidate: ArtifactFingerprint(
            0, "", kind=FingerprintKind.UNAVAILABLE, reason="range_unsupported"),
    )
    await runtime.engine._resolve(runtime.incoming_record)  # decision 1: unresolved against all five members.
    first = list(runtime.calls)
    assert first.count(runtime.incoming_id) == 1  # not once per canonical member (was 5).
    assert sorted(first) == sorted([runtime.incoming_id, *(f"member-{index}" for index in range(5))])
    async with database.get_db() as db:
        row = await db.fetchone(
            "SELECT equivalence_disposition,equivalence_reason,retry_at FROM transfer_requests WHERE id=?",
            (runtime.incoming_record.id,),
        )
    assert (row["equivalence_disposition"], row["equivalence_reason"]) == ("pending", "range_unsupported")

    # A later, independent scheduler decision starts with no remembered evidence.
    (record,) = await runtime.repository.requests(runtime.incoming_transfer.id)
    runtime.engine.clock = lambda: float(row["retry_at"]) + 0.01
    await runtime.engine._process_request(record)
    second = runtime.calls[len(first):]
    assert sorted(second) == sorted(first)  # the same acquisitions again: nothing survived the first decision.


@pytest.mark.parametrize("behaviours,outcome,matched,held,single_plausible", _MAPPING_TABLE)
async def test_evidence_context_never_changes_mapping_semantics(
    tmp_path, monkeypatch, behaviours, outcome, matched, held, single_plausible,
):
    """The memoized model and the uncached model read identically for the
    whole decision table -- the context changes acquisition cost only."""
    labels = tuple(f"{behaviour}-{index}" for index, behaviour in enumerate(behaviours))
    providers = {label: _NamedUnknownSizeProvider(label) for label in labels}
    incoming_provider = _NamedUnknownSizeProvider("incoming")
    _repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, (*providers.values(), incoming_provider),
    )
    acquisitions = []

    async def fingerprint(subject):
        candidate = subject.candidate
        acquisitions.append(str(candidate.id))
        behaviour = candidate.provider_id.rsplit("-", 1)[0]
        if behaviour == "unresolved":
            return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="range_ignored")
        if behaviour == "unresolved_transient":
            return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="range_unsupported")
        if behaviour == "unprovable":
            return None
        signature = "other-content" if behaviour == "contradictory" else "shared-content"
        return ArtifactFingerprint(4, signature, FingerprintKind.FULL_CONTENT_SAMPLE)

    monkeypatch.setattr(executor, "fingerprint", fingerprint)
    canonicals = tuple(
        SimpleNamespace(id=index + 1, expected_bytes=0, label=label, candidates=(providers[label].candidate(
            "unrelated.bin" if label.startswith("mismatch") else MIRROR_FILENAME),))
        for index, label in enumerate(labels)
    )
    incoming = (incoming_provider.candidate(MIRROR_FILENAME),)

    def reading(result):
        return (result.outcome, result.primary.label if result.matched else None, cohorts._must_hold(result),
                tuple(item.label for item in result.plausible), result.evidence.kind, result.evidence.reason,
                result.cardinality)

    uncached = reading(await cohorts._mapping(canonicals, incoming, engine.registry))
    uncached_acquisitions = len(acquisitions)
    acquisitions.clear()
    memoized = reading(await cohorts._mapping(canonicals, incoming, engine.registry, EvidenceContext()))
    assert memoized == uncached
    assert (memoized[0], memoized[1], memoized[2]) == (outcome, matched, held)
    assert len(acquisitions) == len(set(acquisitions))  # no candidate fingerprinted twice in one decision.
    assert len(acquisitions) <= uncached_acquisitions


# ---------------------------------------------------------------------------
# DP 1.0.12 consolidation corrective, Remediation 4: a terminal ``unverified``
# association is a non-writer lifecycle/presentation fact -- never membership.
# ---------------------------------------------------------------------------

async def _request_row(request_id):
    async with database.get_db() as db:
        return await db.fetchone(
            """SELECT state,equivalence_disposition,equivalence_reason,equivalence_retry_count,
                equivalence_target_artifact_id,retry_at FROM transfer_requests WHERE id=?""",
            (request_id,),
        )


async def _membership_counts(request_id, transfer_id) -> dict:
    async with database.get_db() as db:
        counts = {}
        for label, sql, params in (
            ("download_files", "SELECT COUNT(*) AS n FROM download_files WHERE request_id=?", (request_id,)),
            ("execution_attempts", "SELECT COUNT(*) AS n FROM execution_attempts WHERE transfer_id=?", (transfer_id,)),
            ("origins", "SELECT COUNT(*) AS n FROM canonical_candidate_origins WHERE request_id=?", (request_id,)),
            ("bindings", """SELECT COUNT(*) AS n FROM canonical_candidate_bindings b
                JOIN canonical_candidate_origins o ON o.binding_id=b.id WHERE o.request_id=?""", (request_id,)),
            ("consolidations", "SELECT COUNT(*) AS n FROM artifact_consolidations WHERE source_request_id=?",
             (request_id,)),
        ):
            counts[label] = int((await db.fetchone(sql, params))["n"])
    return counts


@pytest.mark.parametrize("incoming_reason", ["range_ignored", "range_unsupported"])
async def test_exhausted_single_plausible_target_settles_unverified_without_membership(
    tmp_path, monkeypatch, incoming_reason,
):
    """Non-retryable evidence is unverified at once; retryable evidence only
    after the bounded budget. Either way: one plausible target, no writer, no
    membership of any kind, and the single-leaf parent settles."""
    runtime = await _masking_runtime(
        tmp_path, monkeypatch, unrelated=2, incoming_reason=incoming_reason, db_name=f"unverified-{incoming_reason}",
    )
    now = [1000.0]
    runtime.engine.clock = lambda: now[0]
    bindings_before = await runtime.engine.canonical.bindings(runtime.plausible.id)
    starts_before = len([call for call in runtime.executor.calls if call[0] == "start"])
    await runtime.engine._resolve(runtime.incoming_record)
    for _ in range(3):  # the bounded proof-retry budget for retryable evidence.
        (record,) = await runtime.repository.requests(runtime.incoming_transfer.id)
        now[0] = max(now[0], record.retry_at) + 0.01
        await runtime.engine._process_request(record)

    row = await _request_row(runtime.incoming_record.id)
    assert row["equivalence_disposition"] == "unverified"
    assert row["equivalence_reason"] == incoming_reason  # the factual unresolved reason.
    assert int(row["equivalence_target_artifact_id"]) == runtime.plausible.id  # the ONE plausible canonical.
    assert row["state"] == "materializing" and float(row["retry_at"] or 0) == 0
    assert "unverified" in cohorts._HELD_DISPOSITIONS
    assert "unverified" not in cohorts._INDEPENDENT_DISPOSITIONS  # never authorizes a writer.
    assert await _membership_counts(runtime.incoming_record.id, runtime.incoming_transfer.id) == {
        "download_files": 0, "execution_attempts": 0, "origins": 0, "bindings": 0, "consolidations": 0,
    }
    assert len([call for call in runtime.executor.calls if call[0] == "start"]) == starts_before

    # Candidate count / failover selection see verified candidates only.
    assert await runtime.engine.canonical.bindings(runtime.plausible.id) == bindings_before
    (canonical,) = [item for item in await runtime.engine.canonical.canonical_artifacts()
                    if item.id == runtime.plausible.id]
    assert len(canonical.candidates) == 1
    incoming_candidate_ids = {str(item.id) for item in await runtime.repository.resolved_candidates(
        runtime.incoming_record.id)}
    assert not incoming_candidate_ids & {str(item.id) for item in canonical.candidates}

    # The parent has no writer-capable work left: it settles instead of staying materializing forever.
    parent = await runtime.repository.get(runtime.incoming_transfer.id)
    assert parent.state == TransferState.CONSOLIDATED
    info = await runtime.engine.canonical.consolidation(runtime.incoming_transfer.id)
    assert info["artifact_mappings"] == []  # settled, yet never recorded as a verified consolidation.

    probes = runtime.probes["incoming"]
    for _ in range(3):  # terminal: no proof hot loop afterwards.
        await runtime.engine.tick()
    assert runtime.probes["incoming"] == probes
    assert (await _request_row(runtime.incoming_record.id))["equivalence_disposition"] == "unverified"


async def test_several_plausible_targets_are_never_guessed_as_unverified(tmp_path, monkeypatch):
    """Two distinct canonical objects share the incoming logical name and both
    stay unresolved: no target may be guessed, so the hold remains
    ``exhausted`` with no association and the parent does not settle."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "several-plausible.sqlite3")
    await database.init_db()
    first, second, incoming_provider = (_NamedUnknownSizeProvider(name) for name in ("object-one", "object-two", "incoming"))
    repository, engine, executor = _build_unknown_size_runtime(tmp_path, monkeypatch, (first, second, incoming_provider))
    await engine.initialize()

    async def fingerprint(subject):
        candidate = subject.candidate
        if candidate.provider_id == incoming_provider.descriptor.id:
            return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="range_ignored")
        return ArtifactFingerprint(4, f"content:{candidate.provider_id}", FingerprintKind.FULL_CONTENT_SAMPLE)

    monkeypatch.setattr(executor, "fingerprint", fingerprint)
    for provider in (first, second):  # same name, proven-different content: two canonical objects.
        transfer = await engine.submit(
            (TransferRequest("parcel", provider.descriptor.id, name=MIRROR_FILENAME,
                             preferred_provider=provider.descriptor.id),), name=MIRROR_FILENAME, deduplicate=False)
        (record,) = await repository.requests(transfer.id)
        await engine._resolve(record)
    assert len(await engine.canonical.canonical_artifacts()) == 2
    incoming = await engine.submit(
        (TransferRequest("parcel", "incoming", name=MIRROR_FILENAME,
                         preferred_provider=incoming_provider.descriptor.id),), name=MIRROR_FILENAME, deduplicate=False)
    (record,) = await repository.requests(incoming.id)
    await engine._resolve(record)
    row = await _request_row(record.id)
    assert row["equivalence_disposition"] == "exhausted"  # held, but not associated with a guessed target.
    assert row["equivalence_target_artifact_id"] is None
    assert (await _membership_counts(record.id, incoming.id))["download_files"] == 0
    assert (await repository.get(incoming.id)).state != TransferState.CONSOLIDATED


@pytest.mark.parametrize("later,disposition,writer", [
    ("equivalent", "recovered", False),
    ("distinct", "contradictory", True),
    ("unresolved", "unverified", False),
])
async def test_reconsidered_unverified_request_transitions_through_the_ordinary_paths(
    tmp_path, monkeypatch, later, disposition, writer,
):
    """While its parent is still unsettled an ``unverified`` request may be
    reconsidered. Affirmative proof takes the ordinary attach / independent
    path and the association target is cleared in the same disposition write;
    still-unresolved proof leaves it unverified."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / f"reconsidered-{later}.sqlite3")
    await database.init_db()
    canonical_provider, incoming_provider, other_provider = (
        _NamedUnknownSizeProvider(name) for name in ("canonical", "incoming", "other"))
    repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, (canonical_provider, incoming_provider, other_provider))
    await engine.initialize()
    phase = ["unresolved"]

    async def fingerprint(subject):
        candidate = subject.candidate
        if candidate.provider_id == incoming_provider.descriptor.id:
            if phase[0] == "unresolved":
                return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="range_ignored")
            signature = f"full:{candidate.name}" if phase[0] == "equivalent" else "different-content"
            return ArtifactFingerprint(4, signature, FingerprintKind.FULL_CONTENT_SAMPLE)
        return ArtifactFingerprint(4, f"full:{candidate.name}", FingerprintKind.FULL_CONTENT_SAMPLE)

    monkeypatch.setattr(executor, "fingerprint", fingerprint)
    owner = await engine.submit(
        (TransferRequest("parcel", "canonical", name=MIRROR_FILENAME,
                         preferred_provider=canonical_provider.descriptor.id),), name=MIRROR_FILENAME, deduplicate=False)
    (owner_record,) = await repository.requests(owner.id)
    await engine._resolve(owner_record)
    (canonical,) = await repository.artifacts(owner.id)

    # A second, unrelated leaf keeps the submitting parent unsettled.
    submitted = await engine.submit(
        (TransferRequest("parcel", "incoming", name=MIRROR_FILENAME, preferred_provider=incoming_provider.descriptor.id),
         TransferRequest("parcel", "other", name="unrelated-object.bin", preferred_provider=other_provider.descriptor.id)),
        name="submitted", deduplicate=False)
    by_payload = {item.request.payload: item for item in await repository.requests(submitted.id)}
    await engine._resolve(by_payload["other"])
    await engine._resolve(by_payload["incoming"])
    held = await _request_row(by_payload["incoming"].id)
    assert held["equivalence_disposition"] == "unverified"
    assert int(held["equivalence_target_artifact_id"]) == canonical.id
    assert (await repository.get(submitted.id)).state != TransferState.CONSOLIDATED

    phase[0] = later
    async with database.get_db() as db:  # the request is reconsidered (the established reset shape).
        await db.execute(
            "UPDATE transfer_requests SET equivalence_disposition='',equivalence_retry_count=0,retry_at=0 WHERE id=?",
            (by_payload["incoming"].id,),
        )
        await db.commit()
    record = next(item for item in await repository.requests(submitted.id) if item.id == by_payload["incoming"].id)
    await engine._process_request(record)

    row = await _request_row(record.id)
    assert row["equivalence_disposition"] == disposition
    counts = await _membership_counts(record.id, submitted.id)
    if later == "equivalent":
        assert row["state"] == "resolved" and row["equivalence_target_artifact_id"] is None
        assert counts["consolidations"] == 1 and counts["origins"] == 1  # ordinary canonical attach.
        assert len(await engine.canonical.bindings(canonical.id)) == 2
    elif later == "distinct":
        assert row["equivalence_target_artifact_id"] is None  # association cleared with the transition.
        assert counts["consolidations"] == 0 and counts["origins"] == 0
    else:
        assert int(row["equivalence_target_artifact_id"]) == canonical.id
        assert counts["consolidations"] == 0 and counts["origins"] == 0
    own_artifact = [item for item in await repository.artifacts(submitted.id) if item.request_id == record.id]
    assert bool(own_artifact) is writer  # a writer exists only for the proven-distinct object.


# ---------------------------------------------------------------------------
# DP 1.0.12 consolidation corrective, Gate 6: the production 298/299/300 shape,
# integrated, with an unrelated canonical object present so the original
# false-independent masking bug is genuinely exercised.
# ---------------------------------------------------------------------------

async def test_production_298_299_300_shape_consolidates_with_unverified_sources_and_one_writer(tmp_path, monkeypatch):
    a_hosts = ("releases.ubuntu.com", "mirrors.mit.edu", "mirror.pilotfiber.com")
    b_hosts = ("mirrors.tuna.tsinghua.edu.cn", "ubuntu-releases.mirrorservice.org", "mirror.sg.gs")
    c_good = ("mirror.serversaustralia.com.au", "mirrors.163.com")
    ustc, aliyun = "mirrors.ustc.edu.cn", "mirrors.aliyun.com"
    runtime = await _canonical_history_runtime(
        tmp_path, monkeypatch, ("unrelated.example", *a_hosts, *b_hosts, *c_good, ustc, aliyun),
        # USTC: attempted, non-retryable unresolved. Aliyun: attempted, retryable, exhausts the bounded budget.
        unresolved={ustc: "range_ignored", aliyun: "range_unsupported"},
    )
    repository, engine, executor = runtime.repository, runtime.engine, runtime.executor

    async def decide(record):
        """One coordination decision; returns the fingerprint acquisitions it made."""
        before = len(runtime.probes)
        await engine._process_request(record)
        return runtime.probes[before:]

    # An unrelated canonical object is established FIRST: it precedes the Ubuntu canonical in repository
    # order, which is exactly what let ``logical_pairing_mismatch`` mask the plausible unresolved target.
    unrelated = await runtime.submit("unrelated.example", name="unrelated-object.bin")
    transfer_a = await runtime.submit(*a_hosts)
    (canonical,) = await repository.artifacts(transfer_a.id)
    transfer_b = await runtime.submit(*b_hosts)

    transfer_c = await engine.submit(
        tuple(TransferRequest("parcel", f"{host}/{MIRROR_FILENAME}", name=MIRROR_FILENAME,
                              preferred_provider=f"mirror:{host}") for host in (*c_good, ustc, aliyun)),
        name=MIRROR_FILENAME, deduplicate=False,
    )
    by_host = {record.request.payload.split("/", 1)[0]: record for record in await repository.requests(transfer_c.id)}
    decisions = {}
    for host, record in by_host.items():
        before = len(runtime.probes)
        await engine._resolve(record)
        decisions[host] = [runtime.probes[before:]]
    for _ in range(3):  # Aliyun's bounded proof-retry budget.
        refreshed = next(item for item in await repository.requests(transfer_c.id) if item.id == by_host[aliyun].id)
        runtime.now[0] = max(runtime.now[0], refreshed.retry_at) + 0.01
        decisions[aliyun].append(await decide(refreshed))

    # --- Gate 3 bounded proof-call behaviour (no wall clock): a decisive full proof costs exactly one
    # acquisition of the incoming route and one of the first verified member; an unresolved decision acquires
    # each candidate at most once, however many verified candidates the canonical already holds.
    for host in c_good:
        assert decisions[host] == [[a_hosts[0], host]] or decisions[host] == [[host, a_hosts[0]]]
    for host in (ustc, aliyun):
        for acquisitions in decisions[host]:
            if acquisitions:  # a quiescent (already terminal) decision acquires nothing.
                assert acquisitions.count(host) == 1
                assert len(acquisitions) == len(set(acquisitions)) <= 1 + len(a_hosts) + len(b_hosts) + len(c_good)
    assert runtime.probes.count(ustc) == 1  # non-retryable: one attempted proof, then terminal.
    assert runtime.probes.count(aliyun) == 3  # initial attempt + the two bounded retries, never more.

    # --- B and C settle consolidated; A remains the one canonical material transfer.
    assert (await repository.get(transfer_b.id)).state == TransferState.CONSOLIDATED
    assert (await repository.get(transfer_c.id)).state == TransferState.CONSOLIDATED
    assert (await repository.get(transfer_a.id)).state not in {TransferState.CONSOLIDATED, TransferState.FAILED}
    assert await repository.artifacts(transfer_b.id) == ()
    assert await repository.artifacts(transfer_c.id) == ()

    # --- A holds ALL verified candidate bindings, and only those.
    bindings = await engine.canonical.bindings(canonical.id)
    assert {binding["source_identity"]["key"] for binding in bindings} == {*a_hosts, *b_hosts, *c_good}
    assert len(bindings) == 8

    # --- The unverified C sources: associated, never members, never writers, never failover candidates.
    for host, reason in ((ustc, "range_ignored"), (aliyun, "range_unsupported")):
        row = await _request_row(by_host[host].id)
        assert (row["equivalence_disposition"], row["equivalence_reason"]) == ("unverified", reason)
        assert int(row["equivalence_target_artifact_id"]) == canonical.id
        assert await _membership_counts(by_host[host].id, transfer_c.id) == {
            "download_files": 0, "execution_attempts": 0, "origins": 0, "bindings": 0, "consolidations": 0,
        }
    (current,) = [item for item in await engine.canonical.canonical_artifacts() if item.id == canonical.id]
    assert len(current.candidates) == 8  # failover selection sees verified candidates only.

    # --- A is the only material writer for this object; the payload completes and nothing else is written.
    await engine.reconcile_executions()
    async with database.get_db() as db:
        executed = {int(row["artifact_id"]) for row in await db.fetchall("SELECT artifact_id FROM execution_attempts")}
    (unrelated_artifact,) = await repository.artifacts(unrelated.id)
    assert executed == {canonical.id, unrelated_artifact.id}
    for artifact in (await repository.artifacts(transfer_a.id))[0], (await repository.artifacts(unrelated.id))[0]:
        executor.finish(artifact.execution)
    await engine.reconcile_executions()
    await engine.tick()
    (completed,) = await repository.artifacts(transfer_a.id)
    assert completed.state == "completed"
    files = sorted(item for item in (tmp_path / "payloads").rglob("*") if item.is_file())
    assert [item.name for item in files] == sorted([MIRROR_FILENAME, "unrelated-object.bin"])
    assert [item for item in files if item.stat().st_size == 0] == []  # no zero-byte invalid payload.
    assert [item.name for item in files if re.search(r" \(\d+\)", item.name)] == []  # no duplicate payload.
    assert len([call for call in executor.calls if call[0] == "start"]) == 2  # one writer per object, ever.

    # --- Details for A: original + consolidated + unverified source history, verified count unchanged.
    presentation = await repository.presentation(transfer_a.id, details=True)
    assert [(row["route_identity"], row["relation"], row["verification_state"], row["contributing_transfer_id"])
            for row in presentation["route_attempts"]] == [
        *((f"https://{host}", "original", "verified", transfer_a.id) for host in a_hosts),
        *((f"https://{host}", "consolidated", "verified", transfer_b.id) for host in b_hosts),
        *((f"https://{host}", "consolidated", "verified", transfer_c.id) for host in c_good),
        (f"https://{ustc}", "unverified", "unverified", transfer_c.id),
        (f"https://{aliyun}", "unverified", "unverified", transfer_c.id),
    ]
    assert len(presentation["candidate_bindings"]) == 8  # "8 Candidates": verified only.
    assert "unrelated.example" not in str(presentation["route_attempts"])


# ---------------------------------------------------------------------------
# DP 1.0.12 consolidation corrective: both schema additions are additive-only,
# nullable, idempotent, and need no backfill against an existing 1.0.12 database.
# ---------------------------------------------------------------------------

_NEW_COLUMNS = (("transfer_requests", "equivalence_target_artifact_id"), ("execution_attempts", "target_initially_absent"),
                ("execution_attempts", "material_owner_attempt_id"))


async def _table_snapshot(db) -> dict:
    """Every table's rows (by rowid) restricted to the pre-change columns, plus the schema object inventory."""
    snapshot = {}
    names = [row["name"] for row in await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for table in names:
        columns = [row["name"] for row in await db.fetchall(f'PRAGMA table_info("{table}")')
                   if (table, row["name"]) not in _NEW_COLUMNS]
        listed = ",".join(f'"{column}"' for column in columns)
        rows = await db.fetchall(f'SELECT rowid AS _rowid,{listed} FROM "{table}" ORDER BY rowid')
        snapshot[table] = [tuple(row[key] for key in ("_rowid", *columns)) for row in rows]
    snapshot["__objects__"] = sorted(
        (row["type"], row["name"]) for row in await db.fetchall("SELECT type,name FROM sqlite_master"))
    return snapshot


async def test_schema_additions_upgrade_a_pre_change_database_idempotently_without_data_loss(tmp_path, monkeypatch):
    """A faithful pre-change ``1.0.12`` database -- populated through the real
    engine with requests, artifacts, executions and a held ``exhausted``
    request, then stripped of exactly the two new columns -- is initialized by
    the corrected code TWICE. Both fields appear; nothing is lost, rebuilt,
    backfilled or migrated twice; startup validation passes each time."""
    runtime = await _nominal_success_runtime(tmp_path, monkeypatch, db_name="schema-upgrade")
    runtime.target.write_bytes(b"done")
    await _report_success(runtime, total=4)  # a completed historical execution.
    async with database.get_db() as db:
        await db.execute(
            """UPDATE transfer_requests SET equivalence_disposition='exhausted',equivalence_reason='dns_failure',
                equivalence_retry_count=2""")
        await db.commit()
        # Reproduce the deployed schema: the two additions are the ONLY schema difference.
        for table, column in _NEW_COLUMNS:
            await db.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        await db.commit()
        for table, column in _NEW_COLUMNS:
            assert column not in {row["name"] for row in await db.fetchall(f'PRAGMA table_info("{table}")')}
        before = await _table_snapshot(db)
    assert before["execution_attempts"] and before["transfer_requests"] and before["download_files"]
    with pytest.raises(RuntimeError):  # the corrected runtime refuses the pre-change schema until bootstrap runs.
        await database.validate_transfer_repository_schema()

    for _ in range(2):  # repeated startup/initialization.
        await database.init_db()
        await database.validate_transfer_repository_schema()
        async with database.get_db() as db:
            assert await _table_snapshot(db) == before  # no data loss, no destructive rebuild, no new/lost objects.
            for table, column in _NEW_COLUMNS:
                info = [row for row in await db.fetchall(f'PRAGMA table_info("{table}")') if row["name"] == column]
                assert len(info) == 1  # present exactly once: a repeated migration neither fails nor duplicates.
                assert int(info[0]["notnull"]) == 0 and info[0]["dflt_value"] is None  # nullable, no default.
                values = await db.fetchall(f'SELECT DISTINCT "{column}" AS value FROM "{table}"')
                assert [row["value"] for row in values] == [None]  # historical rows stay NULL: no backfill.

    # Historical NULLs read safely: the held request is still simply held, and an old execution owns nothing.
    assert (await _request_row((await runtime.repository.requests(runtime.transfer.id))[0].id))[
        "equivalence_disposition"] == "exhausted"
    assert await runtime.repository.execution_owns_target(runtime.artifact.execution) is False
    assert (await runtime.repository.presentation(runtime.transfer.id, details=True))["route_attempts"]


# ---------------------------------------------------------------------------
# DP 1.0.12 consolidation corrective, Gate 9 continuation: existing lifecycle /
# cohort consumers of the terminal UNVERIFIED state introduced by Remediation 4.
# ---------------------------------------------------------------------------

async def _mixed_parent_runtime(tmp_path, monkeypatch, *, db_name, unresolved_reason="range_ignored"):
    """A parent that owns ordinary material of its own AND one leaf that is a
    terminal UNVERIFIED association to a canonical artifact owned by a
    DIFFERENT transfer -- the mixed topology the all-cross-transfer integrated
    regression does not cover."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / f"{db_name}.sqlite3")
    await database.init_db()
    external = _NamedUnknownSizeProvider("external-canonical")
    associated = _NamedUnknownSizeProvider("associated-source")
    local = _NamedUnknownSizeProvider("local-material")
    repository, engine, executor = _build_unknown_size_runtime(tmp_path, monkeypatch, (external, associated, local))
    await engine.initialize()

    async def fingerprint(subject):
        candidate = subject.candidate
        if candidate.provider_id == associated.descriptor.id:
            return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason=unresolved_reason)
        return ArtifactFingerprint(4, f"full:{candidate.name}", FingerprintKind.FULL_CONTENT_SAMPLE)

    monkeypatch.setattr(executor, "fingerprint", fingerprint)
    owner = await engine.submit(
        (TransferRequest("parcel", "external", name=MIRROR_FILENAME, preferred_provider=external.descriptor.id),),
        name=MIRROR_FILENAME, deduplicate=False,
    )
    (owner_record,) = await repository.requests(owner.id)
    await engine._resolve(owner_record)
    (canonical,) = await repository.artifacts(owner.id)

    parent = await engine.submit(
        (TransferRequest("parcel", "associated", name=MIRROR_FILENAME, preferred_provider=associated.descriptor.id),
         TransferRequest("parcel", "local", name="local-object.bin", preferred_provider=local.descriptor.id)),
        name="mixed-parent", deduplicate=False,
    )
    by_payload = {record.request.payload: record for record in await repository.requests(parent.id)}
    for payload in ("local", "associated"):
        await engine._resolve(by_payload[payload])
    return SimpleNamespace(repository=repository, engine=engine, executor=executor, canonical=canonical,
                           owner=owner, parent=parent, by_payload=by_payload)


async def _complete_local_material(runtime):
    """Drive the parent's own ordinary artifact to a real verified completion."""
    await runtime.engine.reconcile_executions()
    (local_artifact,) = await runtime.repository.artifacts(runtime.parent.id)
    runtime.executor.finish(local_artifact.execution)
    for _ in range(3):
        await runtime.engine.reconcile_executions()
        await runtime.engine.tick()
    return next(item for item in await runtime.repository.artifacts(runtime.parent.id) if item.id == local_artifact.id)


async def test_mixed_parent_with_terminal_unverified_leaf_settles(tmp_path, monkeypatch):
    """Gate 9 continuation, Finding 1. A parent that owns one ordinary material
    artifact AND one terminal UNVERIFIED association must reach its terminal
    lifecycle state: the unverified leaf is settled, writer-forbidden truth,
    not an outstanding materialization obligation. RED before the correction:
    the parent stays QUEUED forever because the held leaf's logical slot is
    only excused by a completed canonical in its OWN transfer."""
    runtime = await _mixed_parent_runtime(tmp_path, monkeypatch, db_name="mixed-parent-settles")
    associated = runtime.by_payload["associated"]
    held = await _request_row(associated.id)
    assert held["equivalence_disposition"] == "unverified"
    assert int(held["equivalence_target_artifact_id"]) == runtime.canonical.id

    local_artifact = await _complete_local_material(runtime)
    assert local_artifact.state == "completed"  # the parent's own material is delivered normally.
    assert Path(local_artifact.target).read_bytes() == b"done"

    parent = await runtime.repository.get(runtime.parent.id)
    assert parent.state == TransferState.COMPLETED, (
        f"mixed parent did not settle: {parent.state} -- a terminal UNVERIFIED leaf is not an outstanding obligation"
    )
    assert await _membership_counts(associated.id, runtime.parent.id) == {
        "download_files": 0, "execution_attempts": 1, "origins": 0, "bindings": 0, "consolidations": 0,
    }  # the one execution is the parent's OWN local material; the unverified leaf has no writer.
    assert (await _request_row(associated.id))["equivalence_disposition"] == "unverified"
    assert len((await runtime.engine.canonical.bindings(runtime.canonical.id))) <= 1  # never a canonical member.


async def test_mixed_parent_with_nonterminal_unresolved_leaf_still_blocks(tmp_path, monkeypatch):
    """The control: an unresolved leaf that is NOT terminal (no single
    plausible target, so it stays ``exhausted`` with no association) keeps the
    same mixed parent unsettled. Terminality is what settles, never mere
    absence of proof."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "mixed-parent-blocks.sqlite3")
    await database.init_db()
    first, second = (_NamedUnknownSizeProvider(name) for name in ("object-one", "object-two"))
    associated = _NamedUnknownSizeProvider("associated-source")
    local = _NamedUnknownSizeProvider("local-material")
    repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, (first, second, associated, local))
    await engine.initialize()

    async def fingerprint(subject):
        candidate = subject.candidate
        if candidate.provider_id == associated.descriptor.id:
            return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="range_ignored")
        return ArtifactFingerprint(4, f"content:{candidate.provider_id}", FingerprintKind.FULL_CONTENT_SAMPLE)

    monkeypatch.setattr(executor, "fingerprint", fingerprint)
    for provider in (first, second):  # two distinct canonical objects sharing the incoming logical name.
        transfer = await engine.submit(
            (TransferRequest("parcel", provider.descriptor.id, name=MIRROR_FILENAME,
                             preferred_provider=provider.descriptor.id),), name=MIRROR_FILENAME, deduplicate=False)
        (record,) = await repository.requests(transfer.id)
        await engine._resolve(record)
    parent = await engine.submit(
        (TransferRequest("parcel", "associated", name=MIRROR_FILENAME, preferred_provider=associated.descriptor.id),
         TransferRequest("parcel", "local", name="local-object.bin", preferred_provider=local.descriptor.id)),
        name="mixed-parent-blocks", deduplicate=False)
    by_payload = {record.request.payload: record for record in await repository.requests(parent.id)}
    for payload in ("local", "associated"):
        await engine._resolve(by_payload[payload])
    held = await _request_row(by_payload["associated"].id)
    assert held["equivalence_disposition"] == "exhausted"  # no single plausible target: nonterminal.
    assert held["equivalence_target_artifact_id"] is None

    await engine.reconcile_executions()
    (local_artifact,) = await repository.artifacts(parent.id)
    executor.finish(local_artifact.execution)
    for _ in range(3):
        await engine.reconcile_executions()
        await engine.tick()
    assert (await repository.get(parent.id)).state not in {
        TransferState.COMPLETED, TransferState.CONSOLIDATED,
    }  # unresolved-but-nonterminal identity still blocks settlement.


async def _prefix_cohort_runtime(tmp_path, monkeypatch, *, db_name, decidable_unresolved=False):
    """A canonical artifact owned by another transfer, plus a two-member
    same-transfer cohort whose evidence against it is WEAK PREFIX -- the
    collection-walk topology the FULL-evidence production regression never
    exercises. Sibling ``held`` always answers unresolved; sibling
    ``decidable`` answers weak PREFIX unless ``decidable_unresolved``."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / f"{db_name}.sqlite3")
    await database.init_db()
    canonical_provider = _NamedUnknownSizeProvider("prefix-canonical")
    decidable = _NamedUnknownSizeProvider("prefix-decidable")
    held = _NamedUnknownSizeProvider("prefix-held")
    repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, (canonical_provider, decidable, held))
    await engine.initialize()
    phase = ["seed"]

    async def fingerprint(subject):
        candidate = subject.candidate
        if candidate.provider_id == held.descriptor.id:
            return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="range_ignored")
        if candidate.provider_id == decidable.descriptor.id and decidable_unresolved:
            return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="range_ignored")
        if phase[0] == "seed" and candidate.provider_id == canonical_provider.descriptor.id:
            # The canonical seeds itself through ordinary self-evidence.
            return ArtifactFingerprint(4, "full:seed", FingerprintKind.FULL_CONTENT_SAMPLE)
        # Weak, collection-grade evidence only: a prefix sample both sides share.
        return ArtifactFingerprint(4, "prefix-shared", FingerprintKind.PREFIX_CONTENT_SAMPLE, "range_ignored",
                                   "prefix-shared")

    monkeypatch.setattr(executor, "fingerprint", fingerprint)
    owner = await engine.submit(
        (TransferRequest("parcel", "canonical", name=MIRROR_FILENAME,
                         preferred_provider=canonical_provider.descriptor.id),),
        name=MIRROR_FILENAME, deduplicate=False,
    )
    (owner_record,) = await repository.requests(owner.id)
    await engine._resolve(owner_record)
    (canonical,) = await repository.artifacts(owner.id)
    phase[0] = "cohort"
    cohort = await engine.submit(
        (TransferRequest("parcel", "decidable", name=MIRROR_FILENAME, preferred_provider=decidable.descriptor.id),
         TransferRequest("parcel", "held", name=MIRROR_FILENAME, preferred_provider=held.descriptor.id)),
        name=MIRROR_FILENAME, deduplicate=False,
    )
    by_payload = {record.request.payload: record for record in await repository.requests(cohort.id)}
    return SimpleNamespace(repository=repository, engine=engine, executor=executor, canonical=canonical,
                           owner=owner, cohort=cohort, by_payload=by_payload)


async def _drive_prefix_cohort(runtime, order):
    """Resolve the cohort in ``order``, then run further bounded scheduler
    cycles so any pending proof retry has its chance."""
    for payload in order:
        await runtime.engine._resolve(runtime.by_payload[payload])
    now = [1000.0]
    runtime.engine.clock = lambda: now[0]
    for _ in range(4):
        for record in await runtime.repository.requests(runtime.cohort.id):
            now[0] = max(now[0], record.retry_at) + 0.01
            await runtime.engine._process_request(record)
    return {payload: await _request_row(record.id) for payload, record in runtime.by_payload.items()}


@pytest.mark.parametrize("order", [("held", "decidable"), ("decidable", "held")])
async def test_terminal_unverified_sibling_does_not_poison_a_decidable_prefix_sibling(tmp_path, monkeypatch, order):
    """Gate 9 continuation, Finding 2, Case A + Case C (order independence).
    A weak-PREFIX cohort in which one sibling reaches terminal UNVERIFIED must
    still let its decidable sibling complete the existing collection/attach
    path. RED before the correction: the held sibling is counted as a pending
    proof opportunity forever, so the decidable sibling never attaches."""
    runtime = await _prefix_cohort_runtime(tmp_path, monkeypatch, db_name=f"prefix-cohort-{'-'.join(order)}")
    starts_before = len([call for call in runtime.executor.calls if call[0] == "start"])
    rows = await _drive_prefix_cohort(runtime, order)

    assert rows["held"]["equivalence_disposition"] == "unverified"  # terminal, non-writer.
    assert int(rows["held"]["equivalence_target_artifact_id"]) == runtime.canonical.id
    assert rows["decidable"]["state"] == "resolved", (
        "decidable sibling never attached: it stayed pending only because its sibling is terminally held"
    )
    assert rows["decidable"]["equivalence_disposition"] == "recovered"
    bindings = await runtime.engine.canonical.bindings(runtime.canonical.id)
    assert len(bindings) == 2  # the canonical's own candidate plus the decidable sibling: no duplicate writer.
    assert await _membership_counts(runtime.by_payload["held"].id, runtime.cohort.id) == {
        "download_files": 0, "execution_attempts": 0, "origins": 0, "bindings": 0, "consolidations": 0,
    }  # the terminally-held sibling is never a canonical member and never a writer.
    assert await _membership_counts(runtime.by_payload["decidable"].id, runtime.cohort.id) == {
        "download_files": 1, "execution_attempts": 0, "origins": 1, "bindings": 1, "consolidations": 1,
    }  # the decidable sibling is a standby contribution, never an independent writer.
    assert len([call for call in runtime.executor.calls if call[0] == "start"]) == starts_before
    assert not {rows[payload]["equivalence_disposition"] for payload in rows} & {
        "independent", "contradictory", "released",
    }  # no cohort-wide release.


async def test_terminal_unverified_sibling_never_lets_an_unresolved_sibling_guess(tmp_path, monkeypatch):
    """Gate 9 continuation, Finding 2, Case B. When the other sibling's own
    identity is also genuinely unresolved, excluding the terminally-held
    sibling must not convert that into permission to materialize: it holds on
    its own evidence."""
    runtime = await _prefix_cohort_runtime(
        tmp_path, monkeypatch, db_name="prefix-cohort-ambiguous", decidable_unresolved=True)
    starts_before = len([call for call in runtime.executor.calls if call[0] == "start"])
    rows = await _drive_prefix_cohort(runtime, ("held", "decidable"))

    for payload in ("held", "decidable"):
        assert rows[payload]["equivalence_disposition"] not in {"independent", "contradictory", "released", "provisional"}
        assert rows[payload]["state"] == "materializing"
        assert (await _membership_counts(runtime.by_payload[payload].id, runtime.cohort.id))["download_files"] == 0
    assert len([call for call in runtime.executor.calls if call[0] == "start"]) == starts_before
    # Nothing attached and nothing guessed: neither cohort member contributed candidate provenance.
    assert [origin for binding in await runtime.engine.canonical.bindings(runtime.canonical.id)
            for origin in binding["origins"]
            if origin["request_id"] in {record.id for record in runtime.by_payload.values()}] == []


# ---------------------------------------------------------------------------
# DP 1.0.12 consolidation corrective, Round 3: explicit OPERATOR-DRIVEN
# reconsideration of a terminal UNVERIFIED association whose source parent has
# already settled. Remediation 4 always promised this path; only AUTOMATIC
# background reconsideration was deferred.
# ---------------------------------------------------------------------------

async def _settled_unverified_runtime(tmp_path, monkeypatch, *, db_name):
    """Canonical artifact A owned by a healthy transfer, plus a single-leaf
    source transfer whose only leaf is a terminal UNVERIFIED association to A
    -- so that source parent settles CONSOLIDATED, exactly the shape Round 2
    introduced."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / f"{db_name}.sqlite3")
    await database.init_db()
    owner_provider = _NamedUnknownSizeProvider("canonical-owner")
    source_provider = _NamedUnknownSizeProvider("associated-source")
    repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, (owner_provider, source_provider))
    await engine.initialize()
    phase = ["unresolved"]

    async def fingerprint(subject):
        candidate = subject.candidate
        if candidate.provider_id == source_provider.descriptor.id:
            if phase[0] == "unresolved":
                return ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="range_ignored")
            if phase[0] == "distinct":
                return ArtifactFingerprint(4, "different-content", FingerprintKind.FULL_CONTENT_SAMPLE)
            return ArtifactFingerprint(4, f"full:{candidate.name}", FingerprintKind.FULL_CONTENT_SAMPLE)
        return ArtifactFingerprint(4, f"full:{candidate.name}", FingerprintKind.FULL_CONTENT_SAMPLE)

    monkeypatch.setattr(executor, "fingerprint", fingerprint)
    owner = await engine.submit(
        (TransferRequest("parcel", "canonical", name=MIRROR_FILENAME,
                         preferred_provider=owner_provider.descriptor.id),),
        name=MIRROR_FILENAME, deduplicate=False,
    )
    (owner_record,) = await repository.requests(owner.id)
    await engine._resolve(owner_record)
    (canonical,) = await repository.artifacts(owner.id)

    source = await engine.submit(
        (TransferRequest("parcel", "associated", name=MIRROR_FILENAME,
                         preferred_provider=source_provider.descriptor.id),),
        name=MIRROR_FILENAME, deduplicate=False,
    )
    (record,) = await repository.requests(source.id)
    await engine._resolve(record)

    held = await _request_row(record.id)
    assert held["equivalence_disposition"] == "unverified"
    assert int(held["equivalence_target_artifact_id"]) == canonical.id
    assert (await repository.get(source.id)).state == TransferState.CONSOLIDATED
    return SimpleNamespace(repository=repository, engine=engine, executor=executor, canonical=canonical,
                           owner=owner, source=source, record=record, phase=phase)


async def _canonical_facts(runtime):
    async with database.get_db() as db:
        row = await db.fetchone(
            """SELECT (SELECT COUNT(*) FROM canonical_candidate_bindings WHERE canonical_artifact_id=?) AS bindings,
                (SELECT COUNT(*) FROM canonical_candidate_origins o
                    JOIN canonical_candidate_bindings b ON b.id=o.binding_id
                    WHERE b.canonical_artifact_id=?) AS origins,
                (SELECT COUNT(*) FROM canonical_candidate_origins WHERE request_id=?) AS own_origins,
                (SELECT COUNT(DISTINCT b.id) FROM canonical_candidate_bindings b
                    JOIN canonical_candidate_origins o ON o.binding_id=b.id
                    WHERE b.canonical_artifact_id=? AND o.request_id=?) AS own_bindings,
                (SELECT COUNT(*) FROM download_files WHERE request_id=?) AS own_artifacts,
                (SELECT COUNT(*) FROM execution_attempts WHERE transfer_id=?) AS own_executions,
                (SELECT COUNT(*) FROM artifact_consolidations WHERE source_request_id=?) AS own_consolidations""",
            (runtime.canonical.id, runtime.canonical.id, runtime.record.id,
             runtime.canonical.id, runtime.record.id, runtime.record.id,
             runtime.source.id, runtime.record.id),
        )
    facts = dict(row)
    (current,) = [item for item in await runtime.engine.canonical.canonical_artifacts()
                  if item.id == runtime.canonical.id] or [None]
    facts["candidate_count"] = len(current.candidates) if current is not None else None
    return facts


async def _operator_reconsider(runtime, *, ticks=4):
    """The one existing operator-facing action (POST /torrents/{id}/retry ->
    ApplicationService.retry -> TransferEngine.retry)."""
    accepted = await runtime.engine.retry(runtime.source.id)
    now = [2000.0]
    runtime.engine.clock = lambda: now[0]
    for _ in range(ticks):
        for item in await runtime.repository.requests(runtime.source.id):
            now[0] = max(now[0], item.retry_at) + 0.01
        await runtime.engine.tick()
    return accepted


async def test_operator_reconsideration_after_settlement_attaches_proven_equivalent_source(tmp_path, monkeypatch):
    """Round 3 RED/GREEN, Case 1. A terminal UNVERIFIED association whose
    parent has settled must still be reconsiderable by the EXISTING operator
    action, and later affirmative proof must traverse the ordinary canonical
    attach path. Remediation 4 promised exactly this; only automatic
    background reconsideration was deferred."""
    runtime = await _settled_unverified_runtime(tmp_path, monkeypatch, db_name="reconsider-equivalent")
    before = await _canonical_facts(runtime)
    assert before["candidate_count"] == 1 and before["own_origins"] == 0

    runtime.phase[0] = "equivalent"  # later evidence now proves equivalence.
    accepted = await _operator_reconsider(runtime)
    assert accepted is True, "the existing operator action refused to reconsider a settled UNVERIFIED association"

    row = await _request_row(runtime.record.id)
    after = await _canonical_facts(runtime)
    assert row["equivalence_disposition"] == "recovered"  # ordinary canonical attach path.
    assert row["equivalence_target_artifact_id"] is None  # no stale UNVERIFIED association remains.
    assert row["state"] == "resolved"
    assert after["candidate_count"] == before["candidate_count"] + 1  # N -> N+1, only after proof.
    assert (before["own_bindings"], after["own_bindings"]) == (0, 1)  # exactly one binding for this source...
    assert (before["own_origins"], after["own_origins"]) == (0, 1)  # ...with exactly one origin, never duplicated.
    # The canonical now carries its own candidate plus this one, and nothing else.
    assert after["bindings"] == after["candidate_count"] == 2
    assert after["own_consolidations"] == 1  # ordinary cross-transfer consolidation provenance.
    assert after["own_artifacts"] == 1  # the standby contribution row, never an independent writer.
    assert after["own_executions"] == 0  # no execution, no duplicate payload.
    assert len([call for call in runtime.executor.calls if call[0] == "start"]) == 1  # only the canonical owner ever wrote.


async def test_operator_reconsideration_after_settlement_releases_a_proven_distinct_source(tmp_path, monkeypatch):
    """Round 3, Case 2: prior association was uncertainty, never membership."""
    runtime = await _settled_unverified_runtime(tmp_path, monkeypatch, db_name="reconsider-distinct")
    before = await _canonical_facts(runtime)
    runtime.phase[0] = "distinct"
    await _operator_reconsider(runtime)

    row = await _request_row(runtime.record.id)
    after = await _canonical_facts(runtime)
    assert row["equivalence_disposition"] not in {"unverified", "recovered"}
    assert row["equivalence_target_artifact_id"] is None  # the old association is cleared.
    assert after["bindings"] == before["bindings"]  # no binding to the old artifact was invented.
    assert after["own_origins"] == 0 and after["own_consolidations"] == 0


async def test_operator_reconsideration_that_stays_unresolved_returns_to_unverified(tmp_path, monkeypatch):
    """Round 3, Case 3: still unresolved goes back to terminal UNVERIFIED --
    non-writer, non-member, no hot loop."""
    runtime = await _settled_unverified_runtime(tmp_path, monkeypatch, db_name="reconsider-unresolved")
    before = await _canonical_facts(runtime)
    await _operator_reconsider(runtime)  # phase stays "unresolved".

    row = await _request_row(runtime.record.id)
    after = await _canonical_facts(runtime)
    assert row["equivalence_disposition"] == "unverified"
    assert int(row["equivalence_target_artifact_id"]) == runtime.canonical.id  # one plausible target retained.
    assert after["bindings"] == before["bindings"]
    assert after["own_origins"] == 0 and after["own_consolidations"] == 0 and after["own_executions"] == 0
    assert len([call for call in runtime.executor.calls if call[0] == "start"]) == 1


async def test_settled_unverified_association_is_quiescent_without_operator_action(tmp_path, monkeypatch):
    """Round 3, Case 4: no automatic reconsideration. Restart the engine and
    run ordinary ticks -- nothing is sampled, nothing changes."""
    runtime = await _settled_unverified_runtime(tmp_path, monkeypatch, db_name="reconsider-quiescent")
    probes = {"count": 0}
    inner = runtime.executor.fingerprint

    async def counting(subject):
        candidate = subject.candidate
        probes["count"] += 1
        return await inner(candidate)

    monkeypatch.setattr(runtime.executor, "fingerprint", counting)
    runtime.phase[0] = "equivalent"  # even though proof WOULD now succeed, nothing may ask for it.
    before = await _canonical_facts(runtime)
    restarted = TransferEngine(
        runtime.repository, runtime.engine.registry, download_root=runtime.engine.root,
        policy=runtime.engine.policy, clock=lambda: 3000.0,
    )
    await restarted.initialize()
    for _ in range(5):
        await restarted.tick()

    assert probes["count"] == 0  # no automatic proof acquisition,
    row = await _request_row(runtime.record.id)
    assert row["equivalence_disposition"] == "unverified"  # no scheduler-driven reopening,
    assert int(row["equivalence_target_artifact_id"]) == runtime.canonical.id
    after = await _canonical_facts(runtime)
    # Nothing about THIS association changes: no membership, no writer, no
    # execution. (The canonical owner's own candidate binding may be
    # formalized by the ordinary P1 provenance scan any engine start runs --
    # long-standing behaviour unrelated to this association, so the
    # association-scoped facts are what quiescence is asserted on.)
    assert {key: after[key] for key in ("own_origins", "own_artifacts", "own_executions", "own_consolidations")} == {
        "own_origins": 0, "own_artifacts": 0, "own_executions": 0, "own_consolidations": 0,
    }
    assert after["candidate_count"] == before["candidate_count"]  # no new verified candidate appears.
    assert (await runtime.repository.get(runtime.source.id)).state == TransferState.CONSOLIDATED


async def test_operator_retry_still_refuses_an_ordinary_consolidated_transfer(tmp_path, monkeypatch):
    """Round 3, Regression E: settled transfers are NOT broadly reopened. A
    consolidated transfer whose leaves are all VERIFIED canonical members
    holds no terminal UNVERIFIED association, so the operator action keeps
    refusing exactly as before and nothing about it is mutated."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "ordinary-consolidated.sqlite3")
    await database.init_db()
    owner_provider = _NamedUnknownSizeProvider("canonical-owner")
    source_provider = _NamedUnknownSizeProvider("verified-source")
    repository, engine, executor = _build_unknown_size_runtime(
        tmp_path, monkeypatch, (owner_provider, source_provider))
    await engine.initialize()

    async def fingerprint(subject):
        candidate = subject.candidate
        return ArtifactFingerprint(4, f"full:{candidate.name}", FingerprintKind.FULL_CONTENT_SAMPLE)

    monkeypatch.setattr(executor, "fingerprint", fingerprint)
    owner = await engine.submit(
        (TransferRequest("parcel", "canonical", name=MIRROR_FILENAME,
                         preferred_provider=owner_provider.descriptor.id),),
        name=MIRROR_FILENAME, deduplicate=False)
    (owner_record,) = await repository.requests(owner.id)
    await engine._resolve(owner_record)
    source = await engine.submit(
        (TransferRequest("parcel", "verified", name=MIRROR_FILENAME,
                         preferred_provider=source_provider.descriptor.id),),
        name=MIRROR_FILENAME, deduplicate=False)
    (record,) = await repository.requests(source.id)
    await engine._resolve(record)

    transfer = await repository.get(source.id)
    assert transfer.state == TransferState.CONSOLIDATED  # ordinary verified consolidation.
    row_before = await _request_row(record.id)
    assert row_before["equivalence_disposition"] == "recovered"

    assert await engine.retry(source.id) is False  # unchanged refusal.
    assert (await repository.get(source.id)).state == TransferState.CONSOLIDATED
    assert (await repository.get(source.id)).epoch == transfer.epoch  # no lifecycle transition occurred.
    assert await _request_row(record.id) == row_before  # nothing about the settled transfer was mutated.


async def test_operator_reconsideration_resettles_the_parent_through_the_existing_owner(tmp_path, monkeypatch):
    """Round 3: after an explicit reconsideration the parent is live again
    only until the ordinary machinery settles it once more -- through the
    existing ``_finalize_transfer``/aggregation owners, with no second
    settlement path."""
    runtime = await _settled_unverified_runtime(tmp_path, monkeypatch, db_name="reconsider-resettle")
    runtime.phase[0] = "equivalent"
    assert await _operator_reconsider(runtime) is True
    assert (await runtime.repository.get(runtime.source.id)).state == TransferState.CONSOLIDATED
    assert (await _request_row(runtime.record.id))["equivalence_disposition"] == "recovered"

    # A reconsideration that stays unresolved likewise returns to settlement.
    other = await _settled_unverified_runtime(tmp_path, monkeypatch, db_name="reconsider-resettle-unresolved")
    assert await _operator_reconsider(other) is True  # phase stays "unresolved".
    assert (await other.repository.get(other.source.id)).state == TransferState.CONSOLIDATED
    assert (await _request_row(other.record.id))["equivalence_disposition"] == "unverified"
