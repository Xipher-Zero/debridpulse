"""DP 1.0.13 real-runtime proof: rsync participates in DebridPulse-owned
material, continuation, lifecycle, recovery and switching.

Real owners end to end: the convergence ``TransferEngine``, the real
repository/canonical/equivalence owners, the real ``GeneralRsyncProvider`` and
``GeneralHttpProvider``, the real ``RsyncExecutor`` running the installed rsync,
the real ``Aria2Executor`` with a real ``aria2c`` daemon, and the real
``DownloaderEgressGuard`` (only its resolver maps fixture hostnames to
loopback). Origins are a real rsync daemon, a real ``rsync --server`` behind an
SSH origin, and a throttled Range-capable HTTP origin.

The strongest acceptance case: an HTTP/aria2 partial becomes DP-valid
material, the operator switches to an equivalent rsync source and rsync
continues it under DP authority; then the operator switches back and aria2
continues exactly at the boundary rsync's material reached.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import db.database as database
import executors.aria2.executor as aria2_module
import executors.rsync.executor as rsync_module
import services.network_safety as safety
from executors.aria2.client import Aria2Service
from executors.aria2.executor import Aria2Configuration, Aria2Executor
from executors.rsync.executor import RsyncConfiguration, RsyncExecutor
from providers.general_http.provider import GeneralHttpProvider
from providers.general_rsync.provider import GeneralRsyncProvider
from rsync_origins import RsyncDaemon, RsyncSshOrigin, write_tree
from services import transfer_trace
from test_v113_continuation_runtime import BODY, start_origin, writer_starts
from test_v113_ftp_sftp_convergence_runtime import _free_port
from test_v113_transport_evidence_sampling import guard_for
from transfers import material as mat
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category
from transfers.manual_failover import manual_candidate_failover
from transfers.models import ContinuationStrategy, TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio
MIB = 1 << 20
USER, PASSWORD = "rsync-runtime-user", "rsync-runtime-password"
HANG_GUARD_SECONDS = 120.0


class Runtime(SimpleNamespace):
    async def until(self, predicate, *, label, timeout=HANG_GUARD_SECONDS):
        deadline = time.monotonic() + timeout
        while True:
            await self.engine.tick()
            value = await predicate()
            if value:
                return value
            if time.monotonic() > deadline:
                attempts = [(item.handle.executor_id, item.state, item.error and item.error.category)
                            for item in await self.repository.executions()]
                raise AssertionError(f"rsync runtime did not reach {label}: attempts={attempts}")
            await asyncio.sleep(0.05)

    async def completed(self, transfer_id):
        transfer = await self.repository.get(transfer_id)
        return transfer if transfer.state == TransferState.COMPLETED else None

    async def attempts(self, transfer_id):
        async with database.get_db() as db:
            rows = await db.fetchall("SELECT executor_id,state,continuation FROM execution_attempts "
                                     "WHERE transfer_id=? ORDER BY created_at, rowid", (transfer_id,))
        return [(row["executor_id"], row["state"], json.loads(row["continuation"]) if row["continuation"] else None)
                for row in rows]

    async def close(self):
        for owned in list(self.rsync._runs.values()):
            if owned.owned.process.returncode is None:
                owned.owned.process.kill()
        if self.aria2 is not None:
            try:
                await self.aria2_service._call("aria2.shutdown")
                await asyncio.wait_for(self.aria2_proc.wait(), timeout=5)
            except Exception:
                self.aria2_proc.kill()
        await self.guard.stop()
        for close in self.closers:
            result = close()
            if asyncio.iscoroutine(result):
                await result


async def _runtime(tmp_path, monkeypatch, *, aria2=False, active=4, aria2_split=1) -> Runtime:
    async def validated(uri, **_kwargs):
        return uri

    async def local_resolve(self, host, port=0, family=0):
        return [{"hostname": host, "host": "127.0.0.1", "port": port, "family": socket.AF_INET,
                 "proto": 0, "flags": socket.AI_NUMERICHOST}]

    for module in (safety, aria2_module, rsync_module):
        monkeypatch.setattr(module, "validate_resolved_public_destination", validated)
    # In-process HTTP evidence resolves fixture hostnames to loopback too.
    monkeypatch.setattr(safety.PublicDestinationResolver, "resolve", local_resolve)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "rsync-runtime.sqlite3")
    await database.init_db()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    engine = TransferEngine(repository, registry, download_root=str(downloads),
                            policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0,
                                                  max_active_executions=active, material_checkpoint_interval=0.5))
    await engine.initialize()
    guard = guard_for()
    rsync = RsyncExecutor(RsyncConfiguration(str(downloads), str(tmp_path / "rsync-runtime"),
                                             connection_timeout_seconds=15, transfer_timeout_seconds=60),
                          repository.authorize_execution, egress=guard)
    registry.register_provider(GeneralRsyncProvider())
    registry.register_executor(rsync)
    runtime = Runtime(engine=engine, repository=repository, registry=registry, guard=guard, rsync=rsync,
                      aria2=None, downloads=downloads, closers=[])
    if aria2:
        if shutil.which("aria2c") is None:
            pytest.skip("aria2c is required for the cross-executor rsync proof")
        port, secret = _free_port(), "rsync-runtime-secret"
        proc = await asyncio.create_subprocess_exec(
            "aria2c", "--enable-rpc=true", "--rpc-listen-all=false", f"--rpc-listen-port={port}",
            f"--rpc-secret={secret}", f"--dir={downloads}", "--summary-interval=0", "--console-log-level=warn",
            "--max-download-result=100", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        service = Aria2Service(f"http://127.0.0.1:{port}/jsonrpc", secret, 3)
        for _ in range(200):
            try:
                await service.test()
                break
            except Exception:
                await asyncio.sleep(0.05)
        # ``aria2_split`` > 1: aria2 fetches that many segments at once, so
        # its DP-valid material is several ranges rather than one prefix.
        split = {"split": aria2_split, "minimum_split_size": "1M", "connections_per_server": aria2_split}
        runtime.aria2 = Aria2Executor(service, Aria2Configuration(
            str(downloads), confirmation_delay=0, **(split if aria2_split > 1 else {})),
                                      repository.authorize_execution, egress=guard)
        runtime.aria2_proc, runtime.aria2_service = proc, service
        registry.register_provider(GeneralHttpProvider())
        registry.register_executor(runtime.aria2)
    return runtime


# ── FILE and COLLECTION ──────────────────────────────────────────────────────

@pytest.mark.real_runtime
async def test_a_daemon_file_and_a_nested_tree_complete_through_the_real_engine(tmp_path, monkeypatch):
    source = tmp_path / "srv"
    write_tree(source, {"movie.bin": BODY[:5 * MIB], "Album/cover.jpg": b"cover",
                        "Album/Disc 1/01 [a].flac": b"one", "Album/Disc 1/deep/02.flac": b"two"})
    (source / "Album" / "link-out").symlink_to(tmp_path)
    (source / "Album" / "Disc 1" / "link-in").symlink_to("01 [a].flac")
    daemon = RsyncDaemon(tmp_path / "daemon", {"pub": {"path": source}}).start()
    runtime = await _runtime(tmp_path, monkeypatch)
    try:
        single = await runtime.engine.submit((TransferRequest("rsync", daemon.url("/pub/movie.bin")),),
                                             deduplicate=False)
        await runtime.until(lambda: runtime.completed(single.id), label="daemon FILE")
        (artifact,) = await runtime.repository.artifacts(single.id)
        assert Path(artifact.target).read_bytes() == BODY[:5 * MIB]
        (request,) = await runtime.repository.requests(single.id)
        assert await runtime.repository.bound_route_provider(request.id) == "general_rsync"
        assert {executor for executor, _state, _plan in await runtime.attempts(single.id)} == {"rsync"}
        state = await runtime.repository.material_state(artifact.id)
        assert state.valid == ((0, 5 * MIB),)

        tree = await runtime.engine.submit((TransferRequest("rsync", daemon.url("/pub/Album/")),),
                                           deduplicate=False)
        await runtime.until(lambda: runtime.completed(tree.id), label="daemon COLLECTION")
        files = sorted(Path(item.target).relative_to(runtime.downloads).as_posix()
                       for item in await runtime.repository.artifacts(tree.id))
        assert files == ["Album/Disc 1/01 [a].flac", "Album/Disc 1/deep/02.flac", "Album/cover.jpg"]
        # Symlinks -- inside or outside the tree -- were never members and never followed.
        assert not any("link" in path.name for path in runtime.downloads.rglob("*"))
    finally:
        await runtime.close()
        daemon.stop()


@pytest.mark.real_runtime
async def test_rsync_over_ssh_asks_identity_once_then_completes_with_rsync_provenance(tmp_path, monkeypatch):
    origin = await RsyncSshOrigin(tmp_path / "ssh", credentials=(USER, PASSWORD)).start()
    write_tree(origin.root / "files", {"movie.bin": BODY[:3 * MIB]})
    runtime = await _runtime(tmp_path, monkeypatch)
    try:
        url = origin.url(f"{origin.root}/files/movie.bin")
        transfer = await runtime.engine.submit((TransferRequest("rsync+ssh", url),), deduplicate=False)
        challenge = await runtime.until(lambda: runtime.engine.challenges.current(transfer.id), label="challenge")
        assert challenge.reason.value == "server_identity_required" and origin.auth_attempts == []
        # Both generalized SSH sign-in methods are advertised, so the one modal
        # offers password and Keyfile.
        assert [item.method.value for item in challenge.methods] == ["username_password", "username_private_key"]
        facts = {fact.name.value: fact.value for fact in challenge.facts}
        assert facts["server_identity_fingerprint"] == origin.fingerprint()
        await runtime.engine.submit_input(transfer.id, challenge.id, "username_password",
                                          {"username": USER, "password": PASSWORD})
        seen = [challenge.id]

        async def done():
            current = await runtime.engine.challenges.current(transfer.id)
            if current is not None and current.id not in seen:
                seen.append(current.id)
            return await runtime.completed(transfer.id)

        await runtime.until(done, label="rsync+ssh FILE")
        assert seen == [challenge.id]  # asked once, never twice
        (artifact,) = await runtime.repository.artifacts(transfer.id)
        assert Path(artifact.target).read_bytes() == BODY[:3 * MIB]
        (request,) = await runtime.repository.requests(transfer.id)
        assert request.request.payload == url
        assert await runtime.repository.bound_route_provider(request.id) == "general_rsync"
        async with database.get_db() as db:
            durable = json.dumps([dict(row) for table in ("execution_attempts", "transfer_requests", "download_files")
                                  for row in await db.fetchall(f"SELECT * FROM {table}")])  # nosec B608 - fixed names
        assert PASSWORD not in durable
        # The one trace owner diagnoses rsync through its generic fields --
        # provider, executor, plan -- and never carries a secret.
        trace = json.dumps(await transfer_trace.build(transfer.id, SimpleNamespace(engine=runtime.engine, repository=runtime.repository)))
        assert "general_rsync" in trace and '"rsync"' in trace and PASSWORD not in trace
    finally:
        await runtime.close()
        await origin.close()


@pytest.mark.real_runtime
async def test_a_credential_in_an_rsync_link_is_split_at_admission_and_never_stored(tmp_path, monkeypatch):
    write_tree(tmp_path / "priv", {"secret.bin": BODY[:MIB]})
    daemon = RsyncDaemon(tmp_path / "daemon", {"priv": {"path": tmp_path / "priv", "auth": (USER, PASSWORD)}}).start()
    runtime = await _runtime(tmp_path, monkeypatch)
    try:
        link = f"rsync://{USER}:{PASSWORD}@rsync-origin.test:{daemon.port}/priv/secret.bin"
        transfer = await runtime.engine.submit((TransferRequest("rsync", link),), deduplicate=False)
        await runtime.until(lambda: runtime.completed(transfer.id), label="daemon login from the link")
        assert await runtime.engine.challenges.current(transfer.id) is None  # used, never asked for
        (artifact,) = await runtime.repository.artifacts(transfer.id)
        assert Path(artifact.target).read_bytes() == BODY[:MIB]
        (request,) = await runtime.repository.requests(transfer.id)
        assert "@" not in request.request.payload and PASSWORD not in request.request.payload
        async with database.get_db() as db:
            durable = json.dumps([dict(row) for table in ("execution_attempts", "transfer_requests", "download_files",
                                                          "application_events", "torrents")
                                  for row in await db.fetchall(f"SELECT * FROM {table}")])  # nosec B608 - fixed names
        assert PASSWORD not in durable
    finally:
        await runtime.close()
        daemon.stop()


@pytest.mark.real_runtime
async def test_an_encrypted_openssh_key_answers_the_challenge_and_is_never_stored_or_logged(
        tmp_path, monkeypatch, caplog):
    import asyncssh
    import logging
    key = asyncssh.generate_private_key("ssh-ed25519")
    passphrase = "runtime-key-passphrase-sentinel"
    exported = key.export_private_key("openssh", passphrase).decode()
    origin = await RsyncSshOrigin(tmp_path / "ssh", credentials=None, authorized_key=key, key_user=USER).start()
    write_tree(origin.root / "files", {"movie.bin": BODY[:2 * MIB]})
    runtime = await _runtime(tmp_path, monkeypatch)
    caplog.set_level(logging.DEBUG)
    try:
        transfer = await runtime.engine.submit((TransferRequest("rsync+ssh", origin.url(f"{origin.root}/files/movie.bin")),),
                                               deduplicate=False)
        challenge = await runtime.until(lambda: runtime.engine.challenges.current(transfer.id), label="challenge")
        assert "username_private_key" in [item.method.value for item in challenge.methods]
        await runtime.engine.submit_input(transfer.id, challenge.id, "username_private_key",
                                          {"username": USER, "private_key": exported, "passphrase": passphrase})
        await runtime.until(lambda: runtime.completed(transfer.id), label="key login")
        (artifact,) = await runtime.repository.artifacts(transfer.id)
        assert Path(artifact.target).read_bytes() == BODY[:2 * MIB]
        assert ("publickey", USER) in origin.auth_attempts
        async with database.get_db() as db:
            tables = [row["name"] for row in await db.fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            durable = json.dumps([dict(row) for table in tables
                                  for row in await db.fetchall(f'SELECT * FROM "{table}"')],
                                 # Blob columns (the event journal's search index) as their raw
                                 # bytes, so a secret stored in one would still be found.
                                 default=lambda value: value.decode("latin-1"))  # nosec B608
        trace = json.dumps(await transfer_trace.build(
            transfer.id, SimpleNamespace(engine=runtime.engine, repository=runtime.repository)))
        logged = "\n".join(record.getMessage() for record in caplog.records)
        key_body = exported.splitlines()[1]
        for text in (durable, trace, logged):
            assert passphrase not in text and key_body not in text and "PRIVATE KEY" not in text
    finally:
        await runtime.close()
        await origin.close()


# ── Pause / Resume ───────────────────────────────────────────────────────────

@pytest.mark.real_runtime
async def test_a_wrong_passphrase_returns_to_the_challenge_and_never_downgrades_to_a_password(
        tmp_path, monkeypatch, caplog):
    import asyncssh
    import logging
    key = asyncssh.generate_private_key("ssh-ed25519")
    passphrase, wrong = "right-passphrase-sentinel", "wrong-passphrase-sentinel"
    exported = key.export_private_key("openssh", passphrase).decode()
    origin = await RsyncSshOrigin(tmp_path / "ssh", credentials=(USER, PASSWORD), authorized_key=key,
                                  key_user=USER).start()
    write_tree(origin.root / "files", {"movie.bin": BODY[:MIB]})
    runtime = await _runtime(tmp_path, monkeypatch)
    caplog.set_level(logging.DEBUG)
    try:
        transfer = await runtime.engine.submit((TransferRequest("rsync+ssh", origin.url(f"{origin.root}/files/movie.bin")),),
                                               deduplicate=False)
        first = await runtime.until(lambda: runtime.engine.challenges.current(transfer.id), label="challenge")
        await runtime.engine.submit_input(transfer.id, first.id, "username_private_key",
                                          {"username": USER, "private_key": exported, "passphrase": wrong})

        async def asked_again():
            current = await runtime.engine.challenges.current(transfer.id)
            return current if current is not None and current.id != first.id else None

        again = await runtime.until(asked_again, label="asked again after a wrong passphrase")
        # Back through the one lifecycle, both methods still offered; the
        # transfer never failed and no password login was attempted for it.
        assert "username_private_key" in [item.method.value for item in again.methods]
        assert (await runtime.repository.get(transfer.id)).state == TransferState.INPUT_REQUIRED
        assert not [method for method, _user in origin.auth_attempts if method == "password"]
        await runtime.engine.submit_input(transfer.id, again.id, "username_private_key",
                                          {"username": USER, "private_key": exported, "passphrase": passphrase})
        await runtime.until(lambda: runtime.completed(transfer.id), label="key login after correction")
        (artifact,) = await runtime.repository.artifacts(transfer.id)
        assert Path(artifact.target).read_bytes() == BODY[:MIB]
        assert not [method for method, _user in origin.auth_attempts if method == "password"]
        async with database.get_db() as db:
            tables = [row["name"] for row in await db.fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            durable = json.dumps([dict(row) for table in tables
                                  for row in await db.fetchall(f'SELECT * FROM "{table}"')],
                                 # Blob columns (the event journal's search index) as their raw
                                 # bytes, so a secret stored in one would still be found.
                                 default=lambda value: value.decode("latin-1"))  # nosec B608
        logged = "\n".join(record.getMessage() for record in caplog.records)
        for text in (durable, logged):
            for secret in (passphrase, wrong, exported.splitlines()[1]):
                assert secret not in text
    finally:
        await runtime.close()
        await origin.close()


@pytest.mark.real_runtime
async def test_pause_stops_acquisition_and_resume_continues_from_dp_material(tmp_path, monkeypatch):
    daemon = RsyncDaemon(tmp_path / "daemon", {"pub": {"path": tmp_path / "srv"}}, bwlimit=2048)
    write_tree(tmp_path / "srv", {"movie.bin": BODY})
    daemon.start()
    runtime = await _runtime(tmp_path, monkeypatch)
    try:
        transfer = await runtime.engine.submit((TransferRequest("rsync", daemon.url("/pub/movie.bin")),),
                                               deduplicate=False)

        async def partial():
            artifacts = await runtime.repository.artifacts(transfer.id)
            state = await runtime.repository.material_state(artifacts[0].id) if artifacts else None
            return (artifacts[0], state) if state and state.safe_prefix >= 3 * MIB else None

        artifact, _ = await runtime.until(partial, label="committed partial material")
        await runtime.engine.pause(transfer.id)
        paused = await runtime.repository.material_state(artifact.id)
        attempt = artifact.execution.attempt_id
        # Pause retired the writer: its native group is proven gone and no
        # acquisition authority remains while the pause intent stands.
        assert runtime.rsync.processes.alive(attempt) in {None, False}
        size = Path(artifact.target).stat().st_size
        await asyncio.sleep(1.5)
        assert Path(artifact.target).stat().st_size == size
        assert (await runtime.repository.get(transfer.id)).paused
        assert paused.safe_prefix >= 3 * MIB

        await runtime.engine.resume(transfer.id)
        await runtime.until(lambda: runtime.completed(transfer.id), label="completion after resume")
        assert Path(artifact.target).read_bytes() == BODY
        attempts = await runtime.attempts(transfer.id)
        resumed = attempts[-1][2]
        assert len(attempts) == 2 and resumed["executor_id"] == "rsync"
        assert resumed["strategy"] == ContinuationStrategy.CONTIGUOUS_FROM_OFFSET.value
        # Nothing DP held valid was given up: the new process continued at the
        # committed prefix (aligned to the material geometry).
        assert resumed["boundary"] == mat.align_down(paused.safe_prefix, resumed["alignment"])
        trace = json.dumps(await transfer_trace.build(transfer.id, SimpleNamespace(engine=runtime.engine, repository=runtime.repository)))
        # The one trace owner records the retirement at Pause and the plan the
        # resumed writer ran under, through its generic material provenance.
        assert "writer_retired" in trace and "material_audit" in trace and "contiguous_from_offset" in trace
    finally:
        await runtime.close()
        daemon.stop()


# ── Switching and cross-executor continuation ────────────────────────────────

@pytest.mark.real_runtime
async def test_operator_switch_between_equivalent_rsync_sources_continues_portably(tmp_path, monkeypatch):
    write_tree(tmp_path / "a-srv", {"movie.bin": BODY})
    write_tree(tmp_path / "b-srv", {"movie.bin": BODY})
    a = RsyncDaemon(tmp_path / "a", {"pub": {"path": tmp_path / "a-srv"}}, bwlimit=2048).start()
    b = RsyncDaemon(tmp_path / "b", {"pub": {"path": tmp_path / "b-srv"}}, bwlimit=4096).start()
    runtime = await _runtime(tmp_path, monkeypatch)
    try:
        transfer = await runtime.engine.submit((
            TransferRequest("rsync", a.url("/pub/movie.bin", host="mirror-a.test")),
            TransferRequest("rsync", b.url("/pub/movie.bin", host="mirror-b.test")),
        ), name="movie.bin", deduplicate=False)

        async def partial():
            artifacts = await runtime.repository.artifacts(transfer.id)
            if len(artifacts) != 1 or len(artifacts[0].candidates) != 2 or artifacts[0].execution is None:
                return None
            state = await runtime.repository.material_state(artifacts[0].id)
            return artifacts[0] if state and state.safe_prefix >= 2 * MIB else None

        artifact = await runtime.until(partial, label="converged rsync mirrors with committed material")
        current = artifact.candidates[artifact.selected]
        other = next(item for item in artifact.candidates if item.id != current.id)
        result = await manual_candidate_failover(runtime.engine, transfer.id, artifact.id, str(other.id))
        assert not result.get("confirmation_required"), result
        await runtime.until(lambda: runtime.completed(transfer.id), label="completion after rsync switch")
        assert Path(artifact.target).read_bytes() == BODY
        plan = (await runtime.attempts(transfer.id))[-1][2]
        assert plan["executor_id"] == "rsync" and plan["candidate_id"] == str(other.id)
        assert plan["strategy"] == ContinuationStrategy.CONTIGUOUS_FROM_OFFSET.value and plan["boundary"] >= MIB
        assert [item.id for item in await runtime.repository.artifacts(transfer.id)] == [artifact.id]
    finally:
        await runtime.close()
        a.stop()
        b.stop()


@pytest.mark.real_runtime
async def test_http_aria2_partial_continues_under_rsync_and_rsync_material_continues_under_aria2(
        tmp_path, monkeypatch):
    """The strongest acceptance proof, and its reverse, on one artifact: which
    writer the canonical selection starts with is not part of the contract, so
    the operator switches to the other executor and back -- proving both
    aria2 -> rsync and rsync -> aria2 continuation from DebridPulse material."""
    server, port, served = await start_origin(rate=1 * MIB)
    write_tree(tmp_path / "srv", {"movie.bin": BODY})
    daemon = RsyncDaemon(tmp_path / "daemon", {"pub": {"path": tmp_path / "srv"}}, bwlimit=1024).start()
    runtime = await _runtime(tmp_path, monkeypatch, aria2=True)
    runtime.closers.append(server.close)
    try:
        transfer = await runtime.engine.submit((
            TransferRequest("http", f"http://http-origin.test:{port}/pub/movie.bin"),
            TransferRequest("rsync", daemon.url("/pub/movie.bin")),
        ), name="movie.bin", deduplicate=False)

        async def writing(executor_id, at_least):
            artifacts = await runtime.repository.artifacts(transfer.id)
            if len(artifacts) != 1 or len(artifacts[0].candidates) != 2 or artifacts[0].execution is None:
                return None
            if executor_id is not None and artifacts[0].execution.executor_id != executor_id:
                return None
            state = await runtime.repository.material_state(artifacts[0].id)
            return (artifacts[0], state) if state and state.safe_prefix >= at_least else None

        def candidate_for(artifact, executor_id):
            scheme = "rsync" if executor_id == "rsync" else "http"
            return next(item for item in artifact.candidates if item.endpoints[0].scheme == scheme)

        # 1. The first writer produces a partial that becomes DP-valid material.
        artifact, before = await runtime.until(lambda: writing(None, 3 * MIB), label="first writer's material")
        first = artifact.execution.executor_id
        second = "rsync" if first == "aria2" else "aria2"
        directions, boundaries = [], []
        for source, target, need in ((first, second, 3 * MIB), (second, first, None)):
            mark = len(served)
            result = await manual_candidate_failover(runtime.engine, transfer.id, artifact.id,
                                                     str(candidate_for(artifact, target).id))
            assert not result.get("confirmation_required"), result
            if need is not None:
                artifact, after = await runtime.until(lambda: writing(target, before.safe_prefix + need),
                                                      label=f"{target} continues {source}'s material")
            else:
                await runtime.until(lambda: runtime.completed(transfer.id), label=f"completion on {target}")
            plan = next(item[2] for item in reversed(await runtime.attempts(transfer.id)) if item[0] == target)
            assert plan["strategy"] == ContinuationStrategy.CONTIGUOUS_FROM_OFFSET.value
            # Never less than what DP held valid at the switch (aligned).
            assert plan["boundary"] >= mat.align_down(before.safe_prefix, plan["alignment"]) > 0
            if target == "aria2":
                # aria2 continued exactly at the DP boundary, not from zero.
                assert min(writer_starts(served[mark:])) == plan["boundary"]
            directions.append((source, target))
            boundaries.append(plan["boundary"])
            if need is not None:
                before = after
        assert set(directions) == {("aria2", "rsync"), ("rsync", "aria2")}
        assert boundaries[1] > boundaries[0]
        assert Path(artifact.target).read_bytes() == BODY
        assert [item.id for item in await runtime.repository.artifacts(transfer.id)] == [artifact.id]
    finally:
        await runtime.close()
        daemon.stop()


# ── Remote capacity ──────────────────────────────────────────────────────────

async def _collection_under_limit(tmp_path, monkeypatch, parts, limit, *, bwlimit=4096):
    source = tmp_path / "srv"
    write_tree(source, parts)
    daemon = RsyncDaemon(tmp_path / "daemon", {"pub": {"path": source, "max_connections": limit}},
                         bwlimit=bwlimit).start()
    runtime = await _runtime(tmp_path, monkeypatch, active=4)
    try:
        transfer = await runtime.engine.submit((TransferRequest("rsync", daemon.url("/pub/")),), deduplicate=False)
        await runtime.until(lambda: runtime.completed(transfer.id), label=f"collection under a {limit}-slot module",
                            timeout=240)
        assert {Path(item.target).name: Path(item.target).read_bytes()
                for item in await runtime.repository.artifacts(transfer.id)} == parts
        async with database.get_db() as db:
            rows = await db.fetchall("SELECT error FROM execution_attempts WHERE transfer_id=?", (transfer.id,))
            held = await db.fetchall("SELECT equivalence_disposition FROM transfer_requests WHERE transfer_id=? "
                                     "AND equivalence_disposition IN ('exhausted','unverified')", (transfer.id,))
        # The daemon's own record of every connection it refused for its limit,
        # whichever DebridPulse stage (discovery, proof or execution) met it.
        at_limit = (daemon.root / "rsyncd.log").read_text().count(f"max connections ({limit}) reached")
        return [json.loads(row["error"]) for row in rows if row["error"]], held, at_limit
    finally:
        await runtime.close()
        daemon.stop()


@pytest.mark.real_runtime
async def test_a_daemon_connection_limit_below_dp_concurrency_never_becomes_a_false_failure(tmp_path, monkeypatch):
    """Four executions admitted by DebridPulse against a one-connection module:
    the daemon refuses the excess outright (characterized: it never queues),
    every refusal is the neutral remote-capacity fact, and the collection
    completes without a failed member, an operator hold or a second scheduler."""
    parts = {f"part{index}.bin": BODY[:(index + 1) * MIB] for index in range(4)}  # distinct sizes: no duplicate proof
    # A slow daemon (512 KiB/s: 2-8 s a part) keeps each admitted execution
    # running while DebridPulse admits the next, so the one-slot limit is met.
    refused, held, at_limit = await _collection_under_limit(tmp_path, monkeypatch, parts, limit=1, bwlimit=512)
    assert at_limit, "the daemon never refused a connection: the scenario did not exercise its limit"
    assert all((item["domain"], item["category"], item["retryability"]) == ("network", "concurrency_limited", "backoff")
               for item in refused)
    assert held == []


@pytest.mark.real_runtime
async def test_duplicate_proof_under_a_connection_limit_waits_instead_of_becoming_a_hold(tmp_path, monkeypatch):
    """Same-size siblings need a duplicate proof, which samples both members
    at once; a refusal for capacity is no proof attempt, so it never spends the
    bounded proof budget (formerly: an 'exhausted' identity hold)."""
    parts = {f"vol{index}.part": BODY[index * MIB:index * MIB + 2 * MIB] for index in range(3)}
    _refused, held, _at_limit = await _collection_under_limit(tmp_path, monkeypatch, parts, limit=2)
    assert held == []


# ── Security at the connection boundary ──────────────────────────────────────

async def test_a_private_lan_source_is_refused_without_the_operators_policy(tmp_path, monkeypatch):
    runtime = await _runtime(tmp_path, monkeypatch)
    # The real validator, not the loopback fixture: an RFC1918 source with the
    # global policy off is refused before any connection or process exists.
    monkeypatch.undo()
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "rsync-runtime.sqlite3")
    try:
        transfer = await runtime.engine.submit((TransferRequest("rsync", "rsync://192.168.77.1/pub/movie.bin"),),
                                               deduplicate=False)

        async def refused():
            (request,) = await runtime.repository.requests(transfer.id)
            return request if request.error is not None else None

        request = await runtime.until(refused, label="LAN refusal")
        assert request.error.category.value == "destination_blocked"
        assert runtime.rsync._runs == {}  # no rsync process ever started for it
    finally:
        await runtime.close()


@pytest.mark.real_runtime
async def test_equal_size_siblings_on_a_one_connection_daemon_are_proven_and_acquired(tmp_path, monkeypatch):
    """The duplicate proof of two members of one server never needs that server
    to admit two connections at once: under ``max connections = 1`` equal-size
    siblings are still proven (distinct here) and every member is acquired."""
    parts = {f"vol{index}.part": BODY[index * MIB:index * MIB + 2 * MIB] for index in range(3)}
    _refused, held, _at_limit = await _collection_under_limit(tmp_path, monkeypatch, parts, limit=1)
    assert held == []


@pytest.mark.real_runtime
async def test_two_routes_of_one_file_on_a_one_connection_daemon_converge(tmp_path, monkeypatch):
    """Two spellings of one file on ONE daemon must be proven the same from
    material evidence; that proof never needs the one-connection daemon to
    admit two connections at once, so it decides and the file is acquired once."""
    write_tree(tmp_path / "srv", {"movie.bin": BODY[:4 * MIB]})
    daemon = RsyncDaemon(tmp_path / "daemon", {"pub": {"path": tmp_path / "srv", "max_connections": 1}}).start()
    runtime = await _runtime(tmp_path, monkeypatch)
    try:
        transfer = await runtime.engine.submit((
            TransferRequest("rsync", daemon.url("/pub/movie.bin", host="rsync-origin.test")),
            TransferRequest("rsync", daemon.url("/pub/movie.bin", host="RSYNC-ORIGIN.test")),
        ), name="movie.bin", deduplicate=False)
        await runtime.until(lambda: runtime.completed(transfer.id), label="two routes on a 1-slot daemon", timeout=90)
        (artifact,) = await runtime.repository.artifacts(transfer.id)
        assert Path(artifact.target).read_bytes() == BODY[:4 * MIB]
        async with database.get_db() as db:
            rows = await db.fetchall("SELECT equivalence_disposition FROM transfer_requests "
                                     "WHERE transfer_id=? ORDER BY ordinal", (transfer.id,))
        # The second route was DECIDED (``recovered`` keeps the reason it last
        # waited on -- here the daemon's capacity -- beside the decision), and
        # one writer acquired the one artifact. That writer may itself have been
        # refused first while the daemon was still releasing a proof's session:
        # such an attempt is only capacity, and the same writer is retried.
        assert sorted(row["equivalence_disposition"] for row in rows) == ["", "recovered"]
        attempts = [item for item in await runtime.repository.executions() if item.transfer_id == transfer.id]
        assert {(item.handle.executor_id, item.artifact_id) for item in attempts} == {("rsync", artifact.id)}
        assert [item.state for item in attempts].count("succeeded") == 1
        assert all(item.state == "succeeded" or item.error.category == Category.CONCURRENCY_LIMITED
                   for item in attempts)
    finally:
        await runtime.close()
        daemon.stop()
