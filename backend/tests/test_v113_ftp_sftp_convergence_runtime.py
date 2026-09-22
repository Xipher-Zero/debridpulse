"""DP 1.0.13 real-runtime proof: FTP/SFTP/authenticated evidence before writers.

Real owners end to end: ``TransferEngine`` (convergence engine), the real
repository/canonical owners, the real ``GeneralFtpProvider`` and
``GeneralHttpProvider``, the real ``Aria2Executor`` with a real ``aria2c``
daemon, and the real ``DownloaderEgressGuard`` (only its resolver maps the
fixture hostnames to loopback). Origins are deterministic local servers.

The Transfer 312 class is modelled exactly: one direct-link batch of six FTP
mirrors of one filename, two of which expose identical bytes. Before this
workstream every FTP candidate was ``sampler_unsupported`` and the two working
mirrors became two independent writers.
"""
from __future__ import annotations

import asyncio
import hashlib
import shutil
import socket

import pytest

import db.database as database
import executors.aria2.executor as executor_module
import services.network_safety as safety
from executors.aria2.client import Aria2Service
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from providers.general_ftp.provider import GeneralFtpProvider
from providers.general_http.provider import GeneralHttpProvider
from test_v113_egress_guard_route_scope import FtpOrigin
from test_v113_transport_evidence_sampling import HttpOrigin, SftpOrigin, guard_for
from transfers.convergence_engine import TransferEngine
from transfers.manual_failover import manual_candidate_failover
from transfers.models import TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry
from transfers.models import ExecutionSubject

pytestmark = pytest.mark.asyncio

SAMPLE = 64 * 1024
PAYLOAD = (hashlib.sha256(b"transfer-312-payload-x").digest() * (10 * SAMPLE // 32 + 1))[:10 * SAMPLE + 5]
DIFFERENT = bytes(value ^ 0xA5 for value in PAYLOAD)
USER, PASSWORD = "runtime-user-sentinel", "runtime-password-sentinel"


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


async def _start_aria2(root, *, limit=None):
    if shutil.which("aria2c") is None:
        pytest.skip("aria2c is required for the DP 1.0.13 evidence runtime proof")
    port, secret = _free_port(), "dp1013-evidence-secret"
    args = ["aria2c", "--enable-rpc=true", "--rpc-listen-all=false", f"--rpc-listen-port={port}",
            f"--rpc-secret={secret}", "--rpc-allow-origin-all=false", f"--dir={root}",
            "--max-download-result=100", "--summary-interval=0", "--console-log-level=warn",
            "--auto-file-renaming=false"]
    if limit:
        args.append(f"--max-overall-download-limit={limit}")
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    service = Aria2Service(f"http://127.0.0.1:{port}/jsonrpc", secret, 3)
    for _ in range(100):
        try:
            await service.test()
            return proc, service
        except Exception:
            await asyncio.sleep(0.05)
    proc.kill()
    raise AssertionError("aria2 RPC did not become ready")


class Runtime:
    def __init__(self, **values):
        self.__dict__.update(values)

    async def until(self, predicate, *, label, ticks=300):
        for _ in range(ticks):
            await self.engine.tick()
            value = await predicate()
            if value:
                return value
            await asyncio.sleep(0.03)
        raise AssertionError(f"DP 1.0.13 evidence runtime did not reach: {label}")

    async def close(self):
        try:
            await self.service._call("aria2.shutdown")
        except Exception:
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=3)
        except TimeoutError:
            self.proc.kill()
        await self.guard.stop()
        for origin in self.origins:
            await origin.close()


async def _runtime(tmp_path, monkeypatch, *, mapping=None, origins=(), limit=None) -> Runtime:
    async def validated(uri, **_kwargs):
        return uri

    async def local_resolve(self, host, port=0, family=0):
        return [{"hostname": host, "host": "127.0.0.1", "port": port, "family": socket.AF_INET,
                 "proto": 0, "flags": socket.AI_NUMERICHOST}]

    monkeypatch.setattr(safety, "validate_resolved_public_destination", validated)
    monkeypatch.setattr(safety.PublicDestinationResolver, "resolve", local_resolve)
    monkeypatch.setattr(executor_module, "validate_resolved_public_destination", validated)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "dp1013-runtime.sqlite3")
    await database.init_db()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    policy = TransferPolicy(retry_delay=0, adoption_stability_seconds=0, max_active_executions=10)
    engine = TransferEngine(repository, registry, download_root=str(downloads), policy=policy)
    await engine.initialize()
    mapping = dict(mapping or {})
    guard = guard_for(answers=lambda host, port: [mapping.get(host, "127.0.0.1")])
    proc, service = await _start_aria2(downloads, limit=limit)
    executor = Aria2Executor(service, Aria2Configuration(str(downloads), confirmation_delay=0),
                             repository.authorize_execution, egress=guard)
    registry.register_provider(GeneralHttpProvider())
    registry.register_provider(GeneralFtpProvider())
    registry.register_executor(executor)
    return Runtime(repository=repository, registry=registry, engine=engine, executor=executor, guard=guard,
                   proc=proc, service=service, downloads=downloads, origins=list(origins))


async def _request_rows(transfer_id):
    async with database.get_db() as db:
        rows = await db.fetchall(
            "SELECT id,payload,state,equivalence_disposition,equivalence_reason FROM transfer_requests "
            "WHERE transfer_id=? ORDER BY ordinal", (transfer_id,))
    return [dict(row) for row in rows]


async def _aria2_uris(runtime):
    jobs = await runtime.service._call("aria2.tellStopped", [0, 100, ["files"]])
    jobs += await runtime.service._call("aria2.tellActive", [["files"]])
    jobs += await runtime.service._call("aria2.tellWaiting", [0, 100, ["files"]])
    return [uri["uri"] for job in jobs for item in job["files"] for uri in item.get("uris", [])]


async def _completed_bytes(runtime, transfer_id):
    artifacts = await runtime.repository.artifacts(transfer_id)
    if len(artifacts) == 1 and artifacts[0].state == "completed":
        with open(artifacts[0].target, "rb") as handle:
            return handle.read()
    return None


async def test_transfer_312_shape_equivalent_ftp_mirrors_converge_to_one_canonical_writer(tmp_path, monkeypatch):
    mirror_a = await FtpOrigin({"/pub/payload.iso": PAYLOAD}).start()
    mirror_b = await FtpOrigin({"/pub/payload.iso": PAYLOAD}).start()
    closed_c, closed_d = _free_port(), _free_port()
    runtime = await _runtime(tmp_path, monkeypatch, origins=(mirror_a, mirror_b),
                             mapping={"mirror-e.test": "10.0.0.5"}, limit="120K")
    urls = [
        f"ftp://mirror-a.test:{mirror_a.port}/pub/payload.iso",   # A: valid, payload X
        f"ftp://mirror-b.test:{mirror_b.port}/pub/payload.iso",   # B: valid, payload X
        f"ftp://mirror-c.test:{closed_c}/pub/payload.iso",        # C: connection failure
        f"ftp://mirror-d.test:{closed_d}/pub/payload.iso",        # D: connection failure
        f"ftp://mirror-e.test:{mirror_a.port}/pub/payload.iso",   # E: controlled unavailable (guard-rejected)
        f"ftp://mirror-f.test:{mirror_a.port}/gone/payload.iso",  # F: missing path
    ]
    try:
        transfer = await runtime.engine.submit(tuple(TransferRequest("ftp", url) for url in urls),
                                               name="payload.iso", deduplicate=False)

        async def converged():
            artifacts = await runtime.repository.artifacts(transfer.id)
            if len(artifacts) != 1:
                return None
            bindings = await runtime.engine.canonical.bindings(artifacts[0].id)
            keys = {(binding["source_identity"] or {}).get("key") for binding in bindings}
            return artifacts[0] if {"mirror-a.test", "mirror-b.test"} <= keys else None

        canonical = await runtime.until(converged, label="Transfer-312 FTP convergence")
        rows = await _request_rows(transfer.id)
        assert len(rows) == 6  # one transfer, six durable request lineages
        assert all(row["equivalence_reason"] != "sampler_unsupported" for row in rows)

        # A and B produce the same neutral fingerprint; filename alone never did this.
        a = await runtime.repository.resolved_candidates(rows[0]["id"])
        b = await runtime.repository.resolved_candidates(rows[1]["id"])
        fingerprint_a = await runtime.executor.fingerprint(ExecutionSubject.of(a[0]))
        fingerprint_b = await runtime.executor.fingerprint(ExecutionSubject.of(b[0]))
        assert fingerprint_a == fingerprint_b and fingerprint_a.total_bytes == len(PAYLOAD)

        bindings = await runtime.engine.canonical.bindings(canonical.id)
        origin_requests = {str(origin["request_id"]) for binding in bindings for origin in binding["origins"]}
        assert {rows[0]["id"], rows[1]["id"]} <= origin_requests  # both sources durably provenanced

        async def failed_sources_settled():
            current = await _request_rows(transfer.id)
            return all(row["equivalence_disposition"] in {"unverified", "exhausted"} for row in current[2:])

        await runtime.until(failed_sources_settled, label="failed FTP sources settled without writers")
        assert len(await runtime.repository.artifacts(transfer.id)) == 1  # no writer for C-F

        # Candidate switch within the one canonical artifact, then pause/resume on it.
        current = (await runtime.repository.artifacts(transfer.id))[0]
        other = next(item for index, item in enumerate(current.candidates) if index != current.selected)
        await manual_candidate_failover(runtime.engine, transfer.id, current.id, str(other.id))
        switched = (await runtime.repository.artifacts(transfer.id))[0]
        assert switched.id == canonical.id and switched.candidates[switched.selected].id == other.id
        await runtime.engine.pause(transfer.id)
        await runtime.engine.tick()
        assert (await runtime.repository.get(transfer.id)).paused
        assert [item.id for item in await runtime.repository.artifacts(transfer.id)] == [canonical.id]
        await runtime.engine.resume(transfer.id)

        final = await runtime.until(lambda: _completed_bytes(runtime, transfer.id), label="canonical completion",
                                    ticks=600)
        assert final == PAYLOAD
        assert [item.id for item in await runtime.repository.artifacts(transfer.id)] == [canonical.id]
    finally:
        await runtime.close()


async def test_authenticated_http_mirror_is_challenged_before_any_writer_and_converges(tmp_path, monkeypatch):
    origin = await HttpOrigin({"/pub/big.iso": PAYLOAD, "/locked/big.iso": PAYLOAD},
                              protected={"/locked/big.iso": (USER, PASSWORD)}).start()
    runtime = await _runtime(tmp_path, monkeypatch, origins=(origin,))
    try:
        transfer = await runtime.engine.submit((
            TransferRequest("http", origin.url("/pub/big.iso", host="open-http.test")),
            TransferRequest("http", origin.url("/locked/big.iso", host="locked-http.test")),
        ), name="big.iso", deduplicate=False)

        async def challenged():
            return await runtime.engine.challenges.current(transfer.id)

        first = await runtime.until(challenged, label="evidence challenge")
        assert first.origin.value == "evidence" and first.reason.value == "auth_required"
        assert (await runtime.repository.get(transfer.id)).state == TransferState.INPUT_REQUIRED
        assert len(await runtime.repository.artifacts(transfer.id)) == 1  # only the public seed writer
        await runtime.engine.submit_input(transfer.id, first.id, "username_password",
                                          {"username": USER, "password": "wrong-password-sentinel"})

        async def replaced():
            current = await runtime.engine.challenges.current(transfer.id)
            return current if current and current.id != first.id else None

        second = await runtime.until(replaced, label="replacement challenge after wrong credentials")
        assert second.generation == first.generation + 1
        assert len(await runtime.repository.artifacts(transfer.id)) == 1  # wrong credentials never admit a writer
        await runtime.engine.submit_input(transfer.id, second.id, "username_password",
                                          {"username": USER, "password": PASSWORD})

        async def converged():
            artifacts = await runtime.repository.artifacts(transfer.id)
            if len(artifacts) != 1:
                return None
            return artifacts[0] if len(await runtime.engine.canonical.bindings(artifacts[0].id)) == 2 else None

        await runtime.until(converged, label="authenticated HTTP convergence")
        final = await runtime.until(lambda: _completed_bytes(runtime, transfer.id), label="completion")
        assert final == PAYLOAD
        assert not any("locked" in uri for uri in await _aria2_uris(runtime))  # no unauthorized writer
    finally:
        await runtime.close()


async def test_evidence_input_reaches_real_aria2_without_a_second_prompt(tmp_path, monkeypatch):
    origin = await HttpOrigin({"/pub/big.iso": PAYLOAD, "/locked/big.iso": DIFFERENT},
                              protected={"/locked/big.iso": (USER, PASSWORD)}).start()
    runtime = await _runtime(tmp_path, monkeypatch, origins=(origin,))
    try:
        transfer = await runtime.engine.submit((
            TransferRequest("http", origin.url("/pub/big.iso", host="open-http.test")),
            TransferRequest("http", origin.url("/locked/big.iso", host="locked-http.test")),
        ), name="big.iso", deduplicate=False)
        challenge = await runtime.until(lambda: runtime.engine.challenges.current(transfer.id), label="challenge")
        await runtime.engine.submit_input(transfer.id, challenge.id, "username_password",
                                          {"username": USER, "password": PASSWORD})

        async def both_complete():
            artifacts = await runtime.repository.artifacts(transfer.id)
            return artifacts if len(artifacts) == 2 and all(item.state == "completed" for item in artifacts) else None

        seen = []

        async def watch():
            current = await runtime.engine.challenges.current(transfer.id)
            if current is not None:
                seen.append(current.origin.value)
            return await both_complete()

        artifacts = await runtime.until(watch, label="both distinct writers complete")
        payloads = sorted(open(item.target, "rb").read() for item in artifacts)
        assert payloads == sorted([PAYLOAD, DIFFERENT])
        assert "executor" not in seen  # execution never re-asked: the evidence input was handed off
    finally:
        await runtime.close()


async def test_authenticated_ftp_mirror_converges_with_an_anonymous_mirror(tmp_path, monkeypatch):
    public = await FtpOrigin({"/pub/big.iso": PAYLOAD}).start()
    locked = await FtpOrigin({"/pub/big.iso": PAYLOAD}, users={USER: PASSWORD}, anonymous=False).start()
    runtime = await _runtime(tmp_path, monkeypatch, origins=(public, locked))
    try:
        transfer = await runtime.engine.submit((
            TransferRequest("ftp", f"ftp://open-ftp.test:{public.port}/pub/big.iso"),
            TransferRequest("ftp", f"ftp://locked-ftp.test:{locked.port}/pub/big.iso"),
        ), name="big.iso", deduplicate=False)
        challenge = await runtime.until(lambda: runtime.engine.challenges.current(transfer.id), label="challenge")
        assert challenge.origin.value == "evidence" and challenge.reason.value == "auth_required"
        await runtime.engine.submit_input(transfer.id, challenge.id, "username_password",
                                          {"username": USER, "password": PASSWORD})
        final = await runtime.until(lambda: _completed_bytes(runtime, transfer.id), label="completion")
        assert final == PAYLOAD
        artifact = (await runtime.repository.artifacts(transfer.id))[0]
        assert len(await runtime.engine.canonical.bindings(artifact.id)) == 2
    finally:
        await runtime.close()


async def test_http_and_sftp_mirrors_of_the_same_bytes_converge(tmp_path, monkeypatch):
    http = await HttpOrigin({"/pub/big.iso": PAYLOAD}).start()
    root = tmp_path / "sftp"
    (root / "pub").mkdir(parents=True)
    (root / "pub" / "big.iso").write_bytes(PAYLOAD)
    sftp = await SftpOrigin(root, credentials=(USER, PASSWORD)).start()
    runtime = await _runtime(tmp_path, monkeypatch, origins=(http, sftp))
    try:
        transfer = await runtime.engine.submit((
            TransferRequest("http", http.url("/pub/big.iso", host="open-http.test")),
            TransferRequest("sftp", sftp.url("/pub/big.iso", host="sftp-mirror.test")),
        ), name="big.iso", deduplicate=False)
        challenge = await runtime.until(lambda: runtime.engine.challenges.current(transfer.id), label="challenge")
        assert challenge.origin.value == "evidence" and challenge.reason.value == "server_identity_required"
        assert sftp.auth_attempts == []  # identity is confirmed strictly before authentication
        await runtime.engine.submit_input(transfer.id, challenge.id, "username_password",
                                          {"username": USER, "password": PASSWORD})

        async def converged():
            artifacts = await runtime.repository.artifacts(transfer.id)
            if len(artifacts) != 1:
                return None
            return artifacts[0] if len(await runtime.engine.canonical.bindings(artifacts[0].id)) == 2 else None

        await runtime.until(converged, label="HTTP/SFTP convergence")
        assert await runtime.until(lambda: _completed_bytes(runtime, transfer.id), label="completion") == PAYLOAD
    finally:
        await runtime.close()


async def _sftp_writer_through_evidence(tmp_path, monkeypatch):
    http = await HttpOrigin({"/pub/big.iso": PAYLOAD}).start()
    root = tmp_path / "sftp"
    (root / "pub").mkdir(parents=True)
    (root / "pub" / "big.iso").write_bytes(DIFFERENT)
    sftp = await SftpOrigin(root, credentials=(USER, PASSWORD)).start()
    runtime = await _runtime(tmp_path, monkeypatch, origins=(http, sftp))
    transfer = await runtime.engine.submit((
        TransferRequest("http", http.url("/pub/big.iso", host="open-http.test")),
        TransferRequest("sftp", sftp.url("/pub/big.iso", host="sftp-mirror.test")),
    ), name="big.iso", deduplicate=False)
    challenge = await runtime.until(lambda: runtime.engine.challenges.current(transfer.id), label="challenge")
    assert challenge.origin.value == "evidence" and challenge.reason.value == "server_identity_required"
    assert sftp.auth_attempts == []
    await runtime.engine.submit_input(transfer.id, challenge.id, "username_password",
                                      {"username": USER, "password": PASSWORD})
    return runtime, transfer, challenge, sftp


async def test_sftp_identity_confirmed_by_evidence_is_enforced_by_real_aria2(tmp_path, monkeypatch):
    runtime, transfer, challenge, sftp = await _sftp_writer_through_evidence(tmp_path, monkeypatch)
    try:
        seen = []

        async def both_complete():
            current = await runtime.engine.challenges.current(transfer.id)
            if current is not None:
                seen.append(current.origin.value)
            artifacts = await runtime.repository.artifacts(transfer.id)
            return artifacts if len(artifacts) == 2 and all(item.state == "completed" for item in artifacts) else None

        artifacts = await runtime.until(both_complete, label="SFTP writer completes with the confirmed identity")
        assert sorted(open(item.target, "rb").read() for item in artifacts) == sorted([PAYLOAD, DIFFERENT])
        assert "executor" not in seen
        assert sftp.auth_attempts.count(USER) == 2  # evidence once, execution once -- never re-prompted
    finally:
        await runtime.close()


async def test_sftp_host_key_change_after_evidence_fails_closed_in_execution(tmp_path, monkeypatch):
    runtime, transfer, challenge, sftp = await _sftp_writer_through_evidence(tmp_path, monkeypatch)
    try:
        await runtime.engine.resolve_pending()  # evidence proven, writer admitted, not yet dispatched
        # Same address, different host identity, before execution starts.
        replacement = SftpOrigin(sftp.root, credentials=(USER, PASSWORD))
        await sftp.close()
        runtime.origins.remove(sftp)
        runtime.origins.append(await _listen_on(replacement, sftp.port))

        async def failed():
            artifacts = await runtime.repository.artifacts(transfer.id)
            sftp_artifact = next((item for item in artifacts
                                  if item.candidates[item.selected].endpoints[0].scheme == "sftp"), None)
            return sftp_artifact if sftp_artifact is not None and sftp_artifact.error is not None else None

        artifact = await runtime.until(failed, label="changed host identity fails the execution")
        assert artifact.state != "completed"
        current = await runtime.engine.challenges.current(transfer.id)
        assert current is None or current.origin.value != "executor"  # never re-presents the new key
        assert replacement.auth_attempts == []  # no password was offered to the changed host
    finally:
        await runtime.close()


async def _listen_on(origin, port):
    import asyncssh
    from test_v113_transport_evidence_sampling import _SshServer

    class Files(asyncssh.SFTPServer):
        def __init__(self, chan):
            super().__init__(chan, chroot=str(origin.root))

    origin.server = await asyncssh.listen(
        "127.0.0.1", port, server_host_keys=list(origin.keys.values()),
        server_factory=lambda: _SshServer(origin), sftp_factory=Files, allow_scp=False,
    )
    origin.port = port
    return origin


async def test_protected_seed_and_a_public_sibling_converge_on_retained_evidence(tmp_path, monkeypatch):
    """A protected mirror that seeds the canonical keeps its neutral evidence (never
    its credential), so a later public sibling converges instead of downloading twice."""
    origin = await HttpOrigin({"/pub/big.iso": PAYLOAD, "/locked/big.iso": PAYLOAD},
                              protected={"/locked/big.iso": (USER, PASSWORD)}).start()
    runtime = await _runtime(tmp_path, monkeypatch, origins=(origin,))
    try:
        transfer = await runtime.engine.submit((
            TransferRequest("http", origin.url("/locked/big.iso", host="locked-http.test")),
            TransferRequest("http", origin.url("/pub/big.iso", host="open-http.test")),
        ), name="big.iso", deduplicate=False)
        challenge = await runtime.until(lambda: runtime.engine.challenges.current(transfer.id), label="challenge")
        await runtime.engine.submit_input(transfer.id, challenge.id, "username_password",
                                          {"username": USER, "password": PASSWORD})

        async def converged():
            artifacts = await runtime.repository.artifacts(transfer.id)
            if len(artifacts) != 1:
                return None
            return artifacts[0] if len(await runtime.engine.canonical.bindings(artifacts[0].id)) == 2 else None

        await runtime.until(converged, label="protected seed + public sibling convergence")
        assert await runtime.until(lambda: _completed_bytes(runtime, transfer.id), label="completion") == PAYLOAD
    finally:
        await runtime.close()


async def test_later_protected_mirror_converges_on_a_protected_canonical_after_restart(tmp_path, monkeypatch):
    """Blocker B, real runtime: A proves bytes X with A's credential and writes;
    the process restarts (every transient input gone); B, a different protected
    mirror with B's own credential, proves X and converges onto A -- no second
    writer, and A's credential never reaches the origin again."""
    origin = await HttpOrigin({"/pub/big.iso": DIFFERENT, "/a/big.iso": PAYLOAD, "/b/big.iso": PAYLOAD},
                              protected={"/a/big.iso": ("owner-a", "secret-a"),
                                         "/b/big.iso": ("owner-b", "secret-b")}).start()
    runtime = await _runtime(tmp_path, monkeypatch, origins=(origin,), limit="40K")
    try:
        first = await runtime.engine.submit((
            TransferRequest("http", origin.url("/pub/big.iso", host="open-http.test")),
            TransferRequest("http", origin.url("/a/big.iso", host="mirror-a.test")),
        ), name="big.iso", deduplicate=False)
        challenge = await runtime.until(lambda: runtime.engine.challenges.current(first.id), label="A challenge")
        await runtime.engine.submit_input(first.id, challenge.id, "username_password",
                                          {"username": "owner-a", "password": "secret-a"})

        async def a_writing():
            artifacts = await runtime.repository.artifacts(first.id)
            owned = [item for item in artifacts if "/a/" in item.candidates[item.selected].endpoints[0].address]
            return owned[0] if owned and owned[0].state == "downloading" else None

        canonical = await runtime.until(a_writing, label="A writer running")
        # Restart: a new engine over the same database; the old broker is gone.
        runtime.engine = TransferEngine(runtime.repository, runtime.registry, download_root=str(runtime.downloads),
                                        policy=runtime.engine.policy)
        await runtime.engine.initialize()
        requests_before = len(origin.requests)
        later = await runtime.engine.submit((TransferRequest("http", origin.url("/b/big.iso", host="mirror-b.test")),),
                                            name="big.iso", deduplicate=False)
        challenge = await runtime.until(lambda: runtime.engine.challenges.current(later.id), label="B challenge")
        assert challenge.origin.value == "evidence"
        await runtime.engine.submit_input(later.id, challenge.id, "username_password",
                                          {"username": "owner-b", "password": "secret-b"})

        async def consolidated():
            return (await runtime.repository.get(later.id)).state == TransferState.CONSOLIDATED

        await runtime.until(consolidated, label="B converges onto A after restart")
        assert await runtime.repository.artifacts(later.id) == ()
        bindings = await runtime.engine.canonical.bindings(canonical.id)
        assert {(item["source_identity"] or {}).get("key") for item in bindings} >= {"mirror-a.test", "mirror-b.test"}
        a_token = "Basic " + __import__("base64").b64encode(b"owner-a:secret-a").decode()
        evidence_after_restart = [item for item in origin.requests[requests_before:] if "aria2" not in item[2]]
        assert evidence_after_restart  # B's evidence was acquired live
        assert all(auth != a_token for _path, auth, _agent in evidence_after_restart)  # never A's input
        assert all(path != "/a/big.iso" or auth is None for path, auth, _agent in evidence_after_restart)
        async with database.get_db() as db:
            dump = " ".join(str(dict(row)) for row in await db.fetchall("SELECT candidates FROM download_files"))
        assert "content_evidence" in dump
        for secret in ("secret-a", "secret-b", "owner-a", "owner-b"):
            assert secret not in dump
    finally:
        await runtime.close()


async def test_automatic_failover_moves_between_converged_ftp_candidates(tmp_path, monkeypatch):
    """The existing neutral recovery machinery -- no FTP-specific path -- moves
    the one canonical artifact to the other converged FTP candidate when the
    selected origin disappears mid-download, and completes the same bytes."""
    # Paced origins keep the transfer live long enough to sever it mid-download.
    mirror_a = await FtpOrigin({"/pub/payload.iso": PAYLOAD}, pace=0.05).start()
    mirror_b = await FtpOrigin({"/pub/payload.iso": PAYLOAD}, pace=0.05).start()
    runtime = await _runtime(tmp_path, monkeypatch, origins=(mirror_a, mirror_b))
    try:
        transfer = await runtime.engine.submit((
            TransferRequest("ftp", f"ftp://mirror-a.test:{mirror_a.port}/pub/payload.iso"),
            TransferRequest("ftp", f"ftp://mirror-b.test:{mirror_b.port}/pub/payload.iso"),
        ), name="payload.iso", deduplicate=False)

        async def downloading():
            artifacts = await runtime.repository.artifacts(transfer.id)
            if len(artifacts) != 1 or len(artifacts[0].candidates) != 2 or artifacts[0].state != "downloading":
                return None
            return artifacts[0]

        artifact = await runtime.until(downloading, label="converged FTP download in progress")
        selected = artifact.candidates[artifact.selected]
        dying = mirror_a if f":{mirror_a.port}/" in selected.endpoints[0].address else mirror_b
        await dying.abort()

        async def switched():
            current = (await runtime.repository.artifacts(transfer.id))[0]
            return current if current.candidates[current.selected].id != selected.id else None

        await runtime.until(switched, label="automatic candidate failover", ticks=600)
        final = await runtime.until(lambda: _completed_bytes(runtime, transfer.id), label="completion after failover",
                                    ticks=900)
        assert final == PAYLOAD
        assert [item.id for item in await runtime.repository.artifacts(transfer.id)] == [artifact.id]
    finally:
        await runtime.close()
