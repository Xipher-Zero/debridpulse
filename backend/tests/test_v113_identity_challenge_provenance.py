"""DP 1.0.13 repeated server-identity questions (real transfer 438).

Transfer 438 held two rsync-over-SSH roots of one host. Its trace showed two
``input_required`` events in the same second, then one per root. The second
root asking for itself is the current design: an answer serves the lineage
that gave it, never a sibling root (``AuthScope`` + lineage; unchanged here).
The same-second pair was not: both roots reached the question at once, both
passed the check-then-act "one question at a time" test, and the second root's
question silently overwrote the first's durable question row. The first root
was re-asked later -- one semantic question per root, but an extra durable
question, and a generation the operator might already have been answering.

The durable boundary now refuses to overwrite another request's outstanding
question; the loser is held, exactly as a request that arrived second is.
Real rsync executor and SSH origin; the simultaneity is forced by a barrier.
"""
from __future__ import annotations

import asyncio

import pytest

import db.database as database
from rsync_origins import RsyncSshOrigin, write_tree
from test_v113_rsync_runtime import PASSWORD, USER, _runtime
from transfers.models import TransferRequest

pytestmark = [pytest.mark.asyncio, pytest.mark.real_runtime]


async def _two_roots_asking_at_once(tmp_path, monkeypatch):
    origin = await RsyncSshOrigin(tmp_path / "ssh", credentials=(USER, PASSWORD)).start()
    write_tree(origin.root / "files", {"a.bin": b"A" * 4096, "b.bin": b"B" * 4096})
    runtime = await _runtime(tmp_path, monkeypatch)
    real = runtime.rsync.discover
    arrived, both = [], asyncio.Event()

    async def together(subject, submitted=None, **kwargs):
        # Both roots' first (unanswered) discoveries return only once both
        # have their requirement: they reach the question step together.
        outcome = await real(subject, submitted, **kwargs)
        if submitted is None and len(arrived) < 2:
            arrived.append(subject)
            if len(arrived) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), timeout=10)
        return outcome

    monkeypatch.setattr(runtime.rsync, "discover", together)
    transfer = await runtime.engine.submit((
        TransferRequest("rsync+ssh", origin.url(f"{origin.root}/files/a.bin")),
        TransferRequest("rsync+ssh", origin.url(f"{origin.root}/files/b.bin")),
    ), name="pair", deduplicate=False)
    return runtime, origin, transfer, arrived


async def _kinds(transfer_id, *kinds):
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT kind FROM application_events WHERE transfer_id=? ORDER BY id", (transfer_id,))
    return [row["kind"] for row in rows if row["kind"] in kinds]


async def test_two_roots_asking_at_once_are_asked_one_at_a_time_without_overwriting(tmp_path, monkeypatch):
    runtime, origin, transfer, arrived = await _two_roots_asking_at_once(tmp_path, monkeypatch)
    try:
        asked = []

        async def answer():
            current = await runtime.engine.challenges.current(transfer.id)
            if current is not None and (current.id, current.generation) not in {(a[0], a[1]) for a in asked}:
                asked.append((current.id, current.generation, current.request_id))
                await runtime.engine.submit_input(transfer.id, current.id, "username_password",
                                                  {"username": USER, "password": PASSWORD})
            return await runtime.completed(transfer.id)

        await runtime.until(answer, label="both roots answered", timeout=60)
        assert len(arrived) == 2  # the simultaneity really happened
        # One operator question per root lineage (the design), each the first
        # question its transfer ever showed for that root: nothing overwritten.
        assert len(asked) == 2 and len({request for _id, _generation, request in asked}) == 2
        assert [generation for _id, generation, _request in asked] == [1, 1]
        # The durable record matches the semantic questions exactly.
        assert await _kinds(transfer.id, "input_required") == ["input_required", "input_required"]
        assert await _kinds(transfer.id, "server_identity_confirmed") == ["server_identity_confirmed"] * 2
    finally:
        await runtime.close()
        await origin.close()


async def test_a_root_whose_lineage_confirmed_the_identity_is_never_asked_it_again(tmp_path, monkeypatch):
    """Same request, same host, same key: after its confirmation, its own later
    discovery and its writer reuse it -- the one question stays one."""
    origin = await RsyncSshOrigin(tmp_path / "ssh", credentials=(USER, PASSWORD)).start()
    write_tree(origin.root / "files", {"a.bin": b"A" * 4096})
    runtime = await _runtime(tmp_path, monkeypatch)
    try:
        transfer = await runtime.engine.submit((
            TransferRequest("rsync+ssh", origin.url(f"{origin.root}/files/a.bin")),), deduplicate=False)
        asked = []

        async def answer():
            current = await runtime.engine.challenges.current(transfer.id)
            if current is not None and current.id not in asked:
                asked.append(current.id)
                await runtime.engine.submit_input(transfer.id, current.id, "username_password",
                                                  {"username": USER, "password": PASSWORD})
            return await runtime.completed(transfer.id)

        await runtime.until(answer, label="one root", timeout=60)
        assert len(asked) == 1
        assert await _kinds(transfer.id, "input_required") == ["input_required"]
    finally:
        await runtime.close()
        await origin.close()


async def test_the_438_shape_settles_once_with_one_writer_and_one_question_per_root(tmp_path, monkeypatch):
    """Cross-issue: an established canonical copy (rsync daemon), then one
    transfer with two rsync-over-SSH roots of the same bytes on one host whose
    first questions collide. Every question follows the characterized
    semantics, both roots consolidate into the one canonical writer with
    provenance, and no stale materialization mutates or resurrects anything."""
    from rsync_origins import RsyncDaemon
    body = b"438" * (1 << 20)  # 3 MiB at 64 KiB/s: the canonical copy is still downloading
    write_tree(tmp_path / "srv", {"iso.bin": body})
    daemon = RsyncDaemon(tmp_path / "daemon", {"pub": {"path": tmp_path / "srv"}}, bwlimit=64).start()
    origin = await RsyncSshOrigin(tmp_path / "ssh", credentials=(USER, PASSWORD)).start()
    write_tree(origin.root / "a", {"iso.bin": body})
    write_tree(origin.root / "b", {"iso.bin": body})
    runtime = await _runtime(tmp_path, monkeypatch)
    real = runtime.rsync.discover
    arrived, both = [], asyncio.Event()

    async def together(subject, submitted=None, **kwargs):
        outcome = await real(subject, submitted, **kwargs)
        if submitted is None and subject.candidate.endpoints[0].scheme == "rsync+ssh" and len(arrived) < 2:
            arrived.append(subject)
            if len(arrived) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), timeout=10)
        return outcome

    monkeypatch.setattr(runtime.rsync, "discover", together)
    try:
        canonical = await runtime.engine.submit((TransferRequest("rsync", daemon.url("/pub/iso.bin")),),
                                                name="iso.bin", deduplicate=False)

        async def writing():
            artifacts = await runtime.repository.artifacts(canonical.id)
            return artifacts[0] if artifacts and artifacts[0].execution is not None else None

        primary = await runtime.until(writing, label="canonical writer")
        pair = await runtime.engine.submit((
            TransferRequest("rsync+ssh", origin.url(f"{origin.root}/a/iso.bin")),
            TransferRequest("rsync+ssh", origin.url(f"{origin.root}/b/iso.bin")),
        ), name="iso.bin", deduplicate=False)
        asked = []

        async def settled():
            current = await runtime.engine.challenges.current(pair.id)
            if current is not None and (current.id, current.generation) not in {(a, g) for a, g, _r in asked}:
                asked.append((current.id, current.generation, current.request_id))
                await runtime.engine.submit_input(pair.id, current.id, "username_password",
                                                  {"username": USER, "password": PASSWORD})
            requests = await runtime.repository.requests(pair.id)
            return requests if all(item.state == "resolved" for item in requests) else None

        requests = await runtime.until(settled, label="both roots settled", timeout=90)
        for _ in range(10):
            await runtime.engine.tick()
        assert len(arrived) == 2
        assert [generation for _id, generation, _request in asked] == [1, 1]
        assert await _kinds(pair.id, "input_required") == ["input_required", "input_required"]
        async with database.get_db() as db:
            rows = [dict(row) for row in await db.fetchall(
                "SELECT id,request_id,mirror_state,mirror_group_id FROM download_files WHERE torrent_id=?", (pair.id,))]
            consolidated = await db.fetchall(
                "SELECT source_request_id,canonical_artifact_id FROM artifact_consolidations WHERE source_transfer_id=?",
                (pair.id,))
            failures = await db.fetchall("SELECT error FROM transfer_requests WHERE transfer_id=? AND error IS NOT NULL",
                                         (pair.id,))
            events = await db.fetchall("SELECT message FROM events WHERE torrent_id=?", (pair.id,))
        # Both roots are standby contributions of the one canonical writer.
        assert {row["mirror_state"] for row in rows} == {"standby"} and len(rows) == 2
        assert {row["mirror_group_id"] for row in rows} == {primary.id}
        assert {row["canonical_artifact_id"] for row in consolidated} == {primary.id}
        assert {row["source_request_id"] for row in consolidated} == {item.id for item in requests}
        assert failures == [] and not any("StopIteration" in (row["message"] or "") for row in events)
        assert all(item.state == "resolved" and item.error is None for item in await runtime.repository.requests(pair.id))
        executions = [item for item in await runtime.repository.executions() if item.handle.executor_id == "rsync"]
        assert {item.artifact_id for item in executions} == {primary.id}  # one canonical writer only
    finally:
        await runtime.close()
        await origin.close()
        daemon.stop()
