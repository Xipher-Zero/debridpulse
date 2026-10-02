"""DP 1.0.13 destination-aware continuation, proven with real writers.

aria2 fetching several segments at once leaves DP-valid material that is not
one prefix -- the shape of real transfer 437. Switching that artifact to an
equivalent rsync source must keep every valid range: rsync reads the untouched
canonical destination as its ordinary delta basis and builds the replacement
in its private temporary tree, and only the verified complete payload becomes
DP material. An interrupted reconstruction leaves the destination and the DP
material map exactly as they were. Durable progress stays DP-valid material
throughout; the reconstruction is reported beside it as execution activity.

Real owners end to end (as ``test_v113_rsync_runtime``): the convergence
engine, the real rsync and aria2 executors and daemons, the egress guard.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import api.operational_downloads as downloads
import executors.process_ownership as ownership
from rsync_origins import RsyncDaemon, write_tree
from test_v113_continuation_runtime import BODY, start_origin
from test_v113_rsync_runtime import _runtime
from transfers import material as mat
from transfers.filesystem import flush_payload
from transfers.manual_failover import manual_candidate_failover, preview_candidate_switch
from transfers.models import ContinuationStrategy, TransferRequest

pytestmark = [pytest.mark.asyncio, pytest.mark.real_runtime]
MIB = 1 << 20


def _digest(target: Path, ranges) -> list[str]:
    """What the destination holds at each range, as digests."""
    with open(target, "rb") as handle:
        out = []
        for start, end in ranges:
            handle.seek(start)
            out.append(hashlib.sha256(handle.read(end - start)).hexdigest())
        return out


def _expected(ranges) -> list[str]:
    return [hashlib.sha256(BODY[start:end]).hexdigest() for start, end in ranges]


async def _switched_to_rsync(tmp_path, monkeypatch):
    """A running destination-aware rsync writer continuing aria2's sparse partial.

    Returns ``(runtime, transfer, artifact, paused_state, preview, spawned)``."""
    server, port, _served = await start_origin(rate=512 * 1024)
    write_tree(tmp_path / "srv", {"movie.bin": BODY})
    daemon = RsyncDaemon(tmp_path / "daemon", {"pub": {"path": tmp_path / "srv"}}, bwlimit=1024).start()
    runtime = await _runtime(tmp_path, monkeypatch, aria2=True, aria2_split=4)
    runtime.closers.append(server.close)
    runtime.closers.append(daemon.stop)
    spawned = []
    real = ownership.asyncio.create_subprocess_exec

    async def recording(*argv, **kwargs):
        spawned.append([str(item) for item in argv])
        return await real(*argv, **kwargs)

    monkeypatch.setattr("executors.process_ownership.asyncio.create_subprocess_exec", recording)
    transfer = await runtime.engine.submit((
        TransferRequest("http", f"http://http-origin.test:{port}/pub/movie.bin"),
        TransferRequest("rsync", daemon.url("/pub/movie.bin")),
    ), name="movie.bin", deduplicate=False)

    def source(artifact, scheme):
        return next(item for item in artifact.candidates if item.endpoints[0].scheme == scheme)

    async def converged():
        artifacts = await runtime.repository.artifacts(transfer.id)
        return artifacts[0] if (len(artifacts) == 1 and len(artifacts[0].candidates) == 2
                                and artifacts[0].execution is not None) else None

    artifact = await runtime.until(converged, label="one artifact with both sources")
    if artifact.execution.executor_id != "aria2":
        # Which writer the canonical selection starts with is not the contract.
        result = await manual_candidate_failover(runtime.engine, transfer.id, artifact.id,
                                                 str(source(artifact, "http").id))
        assert not result.get("confirmation_required"), result

    async def sparse():
        artifacts = await runtime.repository.artifacts(transfer.id)
        if not artifacts or artifacts[0].execution is None or artifacts[0].execution.executor_id != "aria2":
            return None
        state = await runtime.repository.material_state(artifacts[0].id)
        return artifacts[0] if state is not None and len(state.valid) >= 3 else None

    artifact = await runtime.until(sparse, label="aria2's sparse DP-valid material")
    await runtime.engine.pause(transfer.id)
    paused = await runtime.repository.material_state(artifact.id)
    # Several ranges, and a real gap past the prefix: what a prefix plan would discard.
    assert len(paused.valid) >= 3 and paused.valid_bytes > paused.safe_prefix
    rsync_source = source(artifact, "rsync")
    preview = await preview_candidate_switch(runtime.engine, transfer.id, artifact.id, str(rsync_source.id))
    result = await manual_candidate_failover(runtime.engine, transfer.id, artifact.id, str(rsync_source.id))
    assert not result.get("confirmation_required"), result
    await runtime.engine.resume(transfer.id)

    async def reconstructing():
        artifacts = await runtime.repository.artifacts(transfer.id)
        if not artifacts or artifacts[0].execution is None or artifacts[0].execution.executor_id != "rsync":
            return None
        active = next((item for item in await runtime.repository.active() if item.id == transfer.id), None)
        return (artifacts[0], active) if active is not None and (active.active_execution_progress or 0) > 0 else None

    artifact, _active = await runtime.until(reconstructing, label="rsync reconstructing on its basis")
    return runtime, transfer, artifact, paused, preview, spawned


async def _plan(runtime, transfer_id):
    return next(plan for executor, _state, plan in reversed(await runtime.attempts(transfer_id)) if executor == "rsync")


async def test_aria2_sparse_material_continues_under_rsync_without_discarding_any_range(tmp_path, monkeypatch):
    runtime, transfer, artifact, paused, preview, spawned = await _switched_to_rsync(tmp_path, monkeypatch)
    try:
        # The preview the operator saw: nothing is discarded for its geometry.
        assert preview["discarded_bytes"] == 0 and preview["retained_bytes"] == paused.valid_bytes
        plan = await _plan(runtime, transfer.id)
        assert plan["strategy"] == ContinuationStrategy.DESTINATION_AWARE.value
        assert [tuple(item) for item in plan["retained"]] == list(paused.valid) and plan["discarded"] == []
        assert plan["material_generation"] == paused.material_generation
        target = Path(artifact.target)
        durable = paused.valid_bytes / len(BODY) * 100
        samples = []
        for _ in range(2):
            active = next(item for item in await runtime.repository.active() if item.id == transfer.id)
            state = await runtime.repository.material_state(artifact.id)
            details = await runtime.repository.presentation(transfer.id, details=True)
            # The canonical destination is the basis: never truncated to the
            # prefix, never rewritten, every retained range still there.
            assert target.stat().st_size >= paused.valid[-1][1]
            assert _digest(target, paused.valid) == _expected(paused.valid)
            # Durable progress is DP-valid material only, and unchanged; the
            # reconstruction is separate activity (its private temporary
            # output never becomes material).
            assert state.valid == paused.valid and state.material_generation == paused.material_generation
            assert active.progress == pytest.approx(durable) and details["progress"] == pytest.approx(durable)
            assert details["active_execution_progress"] is not None
            # The bounded list carries the same two truths from its one SQL read.
            listed = await downloads.list_operational_torrents(
                status=None, search=None, limit=0, offset=0, order=None,
                application=SimpleNamespace(repository=runtime.repository, definitions=[], engine=runtime.engine))
            item = next(entry for entry in listed["items"] if entry["id"] == transfer.id)
            assert item["progress"] == pytest.approx(durable) and item["active_execution_progress"] is not None
            samples.append(active.active_execution_progress)
            for _ in range(15):  # observations are recorded as the engine observes
                await runtime.engine.tick()
                await asyncio.sleep(0.1)
        assert samples[1] > samples[0] > 0
        delivered_before = runtime.guard.budget("rsync").delivered
        await runtime.until(lambda: runtime.completed(transfer.id), label="rsync reconstruction completes", timeout=180)
        assert target.read_bytes() == BODY
        final = await runtime.repository.material_state(artifact.id)
        assert final.valid == ((0, len(BODY)),)
        details = await runtime.repository.presentation(transfer.id, details=True)
        assert details["progress"] == 100.0 and details["active_execution_progress"] is None
        # rsync reused the basis: what crossed the network is at most the
        # gaps (plus protocol), never the whole payload again.
        assert runtime.guard.budget("rsync").delivered - delivered_before < len(BODY) - paused.valid_bytes
        (argv,) = [item for item in spawned if any(arg.startswith("--temp-dir=") for arg in item)]
        assert "--no-whole-file" in argv
        assert not {"--append", "--inplace"} & set(argv) and not any(arg.startswith("--partial") for arg in argv)
    finally:
        await runtime.close()


async def test_an_interrupted_reconstruction_leaves_the_destination_and_material_exactly_as_they_were(
        tmp_path, monkeypatch):
    runtime, transfer, artifact, paused, _preview, _spawned = await _switched_to_rsync(tmp_path, monkeypatch)
    try:
        target = Path(artifact.target)
        size = target.stat().st_size
        retired = artifact.execution
        before = next(item for item in await runtime.repository.active() if item.id == transfer.id)
        # Interrupt the running reconstruction (rsync has no native pause: its
        # writer is retired, its process stopped).
        await runtime.engine.pause(transfer.id)
        assert runtime.rsync.processes.alive(retired.attempt_id) in {None, False}
        state = await runtime.repository.material_state(artifact.id)
        assert state.valid == paused.valid and state.material_generation == paused.material_generation
        assert target.stat().st_size == size and _digest(target, paused.valid) == _expected(paused.valid)
        # No UNKNOWN range was promoted, and durable progress neither jumped nor rolled back.
        after = next(item for item in await runtime.repository.active() if item.id == transfer.id)
        assert after.progress == before.progress and after.active_execution_progress is None
        # The retired writer can never commit, whatever it claims.
        assert await runtime.repository.commit_material(retired, ((0, len(BODY)),), flush_payload(str(target)),
                                                        now=1.0, forced="completion") is None
        # The same material is reusable: the next writer is planned on it again.
        await runtime.engine.resume(transfer.id)
        await runtime.until(lambda: runtime.completed(transfer.id), label="completion after the interruption",
                            timeout=180)
        assert target.read_bytes() == BODY
        plan = await _plan(runtime, transfer.id)
        assert plan["strategy"] == ContinuationStrategy.DESTINATION_AWARE.value
        assert [tuple(item) for item in plan["retained"]] == list(paused.valid)
        assert (await runtime.repository.material_state(artifact.id)).valid == ((0, len(BODY)),)
        assert mat.total(paused.valid) < len(BODY)
    finally:
        await runtime.close()


async def test_switching_to_an_rsync_over_ssh_source_reconstructs_without_a_challenge_loop(tmp_path, monkeypatch):
    """Real transfer 437's route: aria2's sparse partial switched to an
    rsync-over-SSH source. The switched-to writer may be asked for its sign-in
    (an answer belongs to the lineage that asked), but an answered challenge
    continues that same attempt -- never an endless series of attempts that
    each end ABSENT and ask again."""
    from rsync_origins import RsyncSshOrigin
    from test_v113_rsync_runtime import PASSWORD, USER
    server, port, _served = await start_origin(rate=512 * 1024)
    origin = await RsyncSshOrigin(tmp_path / "ssh", credentials=(USER, PASSWORD)).start()
    write_tree(origin.root / "files", {"movie.bin": BODY})
    runtime = await _runtime(tmp_path, monkeypatch, aria2=True, aria2_split=4)
    runtime.closers.append(server.close)
    runtime.closers.append(origin.close)
    asked = []
    try:
        transfer = await runtime.engine.submit((
            TransferRequest("http", f"http://http-origin.test:{port}/pub/movie.bin"),
            TransferRequest("rsync+ssh", origin.url(f"{origin.root}/files/movie.bin")),
        ), name="movie.bin", deduplicate=False)

        async def answer():
            challenge = await runtime.engine.challenges.current(transfer.id)
            if challenge is not None and challenge.id not in asked:
                asked.append(challenge.id)
                await runtime.engine.submit_input(transfer.id, challenge.id, "username_password",
                                                  {"username": USER, "password": PASSWORD})

        async def sparse():
            await answer()
            artifacts = await runtime.repository.artifacts(transfer.id)
            if len(artifacts) != 1 or len(artifacts[0].candidates) != 2 or artifacts[0].execution is None:
                return None
            if artifacts[0].execution.executor_id != "aria2":
                http = next(item for item in artifacts[0].candidates if item.endpoints[0].scheme == "http")
                await manual_candidate_failover(runtime.engine, transfer.id, artifacts[0].id, str(http.id),
                                                discard_confirmed=True)
                return None
            state = await runtime.repository.material_state(artifacts[0].id)
            return artifacts[0] if state is not None and len(state.valid) >= 3 else None

        artifact = await runtime.until(sparse, label="aria2's sparse material")
        paused_valid = (await runtime.repository.material_state(artifact.id)).valid
        before_switch = len(asked)
        ssh = next(item for item in artifact.candidates if item.endpoints[0].scheme == "rsync+ssh")
        result = await manual_candidate_failover(runtime.engine, transfer.id, artifact.id, str(ssh.id))
        assert not result.get("confirmation_required"), result

        async def completed():
            await answer()
            return await runtime.completed(transfer.id)

        await runtime.until(completed, label="rsync+ssh reconstruction", timeout=180)
        assert Path(artifact.target).read_bytes() == BODY
        attempts = [item for item in await runtime.attempts(transfer.id) if item[0] == "rsync"]
        # One writer (at most one more after a refused, retried start), never a churn.
        assert len(attempts) <= 2 and all(state != "absent" for _executor, state, _plan in attempts), attempts
        assert len(asked) - before_switch <= 1
        plan = attempts[-1][2]
        assert plan["strategy"] == ContinuationStrategy.DESTINATION_AWARE.value
        assert [tuple(item) for item in plan["retained"]] == list(paused_valid)
    finally:
        await runtime.close()
