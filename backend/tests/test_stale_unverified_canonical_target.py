"""DP 1.0.13 stale UNVERIFIED canonical-target liveness correction.

A terminal UNVERIFIED association is terminal only while the exact canonical
artifact that justifies the hold (``equivalence_target_artifact_id``) remains a
valid non-failed target. Once that exact target has terminally failed the
ASSOCIATION -- never the identity question -- is stale: only that association
is cleared, and the ordinary equivalence machinery answers the identity
question again against current canonical truth.

Deterministic, process-free: fake providers/executor/evidence over the real
engine, repository and canonical owner (production transfers 322/323 shapes).
"""
from __future__ import annotations

import asyncio

import pytest

import db.database as database
from test_multi_mirror_general_http_convergence import _UnknownSizeProvider, _build_unknown_size_runtime
from transfers import cohorts
from transfers import _repository_base
from transfers._repository_base import _durable_canonical_targets_for_request
from transfers.errors import Category, Domain, NormalizedError, Stage
from transfers.models import ArtifactFingerprint, FingerprintKind, TransferRequest

pytestmark = pytest.mark.asyncio

ISO = "ubuntu-26.04.1-desktop-amd64.iso"
_DEAD = ArtifactFingerprint(0, "", kind=FingerprintKind.UNAVAILABLE, reason="destination_rejected")


def _failure() -> NormalizedError:
    return NormalizedError(Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE, Stage.EXECUTION,
                           native_code="1", diagnostic="Proxy connection failed.")


async def _request_row(request_id):
    async with database.get_db() as db:
        return dict(await db.fetchone(
            """SELECT id,state,equivalence_disposition,equivalence_reason,equivalence_target_artifact_id,
                equivalence_retry_count,retry_at FROM transfer_requests WHERE id=?""",
            (request_id,),
        ))


async def _artifact_row(artifact_id):
    async with database.get_db() as db:
        return dict(await db.fetchone("SELECT * FROM download_files WHERE id=?", (artifact_id,)))


async def _writers(request_ids) -> list[dict]:
    """Physical writers (own download_files rows that are not attached standbys)."""
    placeholders = ",".join("?" * len(request_ids))
    async with database.get_db() as db:
        rows = await db.fetchall(
            f"""SELECT id,request_id,status FROM download_files WHERE request_id IN ({placeholders})
                AND COALESCE(mirror_state,'')!='standby' AND status!='duplicate'""",
            tuple(request_ids),
        )
    return [dict(row) for row in rows]


async def _parent_status(transfer_id) -> str:
    async with database.get_db() as db:
        row = await db.fetchone("SELECT status FROM torrents WHERE id=?", (transfer_id,))
    return str(row["status"])


async def _refreshed(repository, transfer_id, request_id):
    return next(item for item in await repository.requests(transfer_id) if item.id == request_id)


async def _same_transfer_mirrors(tmp_path, monkeypatch, *, db_name, mirrors=3):
    """Transfer 322 shape: mirror 0 seeds the one canonical writer A, then its
    upstream becomes unreachable so every later sibling's pairwise proof
    against A is ``destination_rejected`` and they are durably held
    UNVERIFIED against A (never granted a writer)."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / f"{db_name}.sqlite3")
    await database.init_db()
    now = [1000.0]
    providers = tuple(_UnknownSizeProvider(f"{db_name}-m{index}") for index in range(mirrors))
    repository, engine, executor = _build_unknown_size_runtime(tmp_path, monkeypatch, providers,
                                                               now=lambda: now[0])
    await engine.initialize()
    state = {"dead": False, "probes": 0}

    async def fingerprint(subject):
        state["probes"] += 1
        if subject.candidate.provider_id == providers[0].descriptor.id and state["dead"]:
            return _DEAD
        return ArtifactFingerprint(4, "shared-iso", FingerprintKind.FULL_CONTENT_SAMPLE)

    executor.fingerprint = fingerprint
    transfer = await engine.submit(
        tuple(TransferRequest("parcel", f"{db_name}-m{index}", name=ISO, preferred_provider=p.descriptor.id)
              for index, p in enumerate(providers)),
        name=ISO, deduplicate=False,
    )
    records = {record.request.payload: record for record in await repository.requests(transfer.id)}
    ordered = [records[f"{db_name}-m{index}"] for index in range(mirrors)]
    await engine._resolve(ordered[0])
    (anchor,) = await repository.artifacts(transfer.id)
    assert anchor.request_id == ordered[0].id
    state["dead"] = True
    for record in ordered[1:]:
        await engine._resolve(record)
    for record in ordered[1:]:
        row = await _request_row(record.id)
        assert (row["state"], row["equivalence_disposition"], row["equivalence_reason"],
                row["equivalence_target_artifact_id"]) == ("materializing", "unverified", "destination_rejected",
                                                           anchor.id)
    assert [item["id"] for item in await _writers([r.id for r in ordered])] == [anchor.id]
    return repository, engine, executor, transfer, ordered, anchor, state, now


async def _fail_anchor(engine, repository, anchor):
    """A's writer terminally fails (recovery exhausted) -- the durable fact."""
    await engine.reconcile_executions()
    await repository.artifact_state(anchor.id, "error", error=_failure())


# ---------------------------------------------------------------- Case A / H


async def test_case_a_dead_first_canonical_viable_siblings_replace_and_parent_completes(tmp_path, monkeypatch):
    repository, engine, executor, transfer, (a, b, c), anchor, _state, _now = await _same_transfer_mirrors(
        tmp_path, monkeypatch, db_name="case-a")
    # Before A fails: B and C stay held, no duplicate writer, no reproof.
    for _ in range(2):
        await engine.tick()
    for record in (b, c):
        assert (await _request_row(record.id))["equivalence_target_artifact_id"] == anchor.id
    assert [item["id"] for item in await _writers([a.id, b.id, c.id])] == [anchor.id]
    # A bounded budget spent against A belongs to the stale association.
    async with database.get_db() as db:
        await db.execute("UPDATE transfer_requests SET equivalence_retry_count=2 WHERE id IN (?,?)", (b.id, c.id))
        await db.commit()

    await _fail_anchor(engine, repository, anchor)
    await engine._aggregate(transfer.id)
    # The parent must not terminalize while stale-target holds await reconsideration.
    assert await _parent_status(transfer.id) not in {"error", "completed", "consolidated"}

    for _ in range(3):
        await engine.tick()
    writers = [item for item in await _writers([b.id, c.id])]
    assert len(writers) == 1, writers  # exactly one replacement writer.
    replacement = writers[0]
    other = c if replacement["request_id"] == b.id else b
    replaced = b if other is c else c
    replaced_row, other_row = await _request_row(replaced.id), await _request_row(other.id)
    assert replaced_row["equivalence_target_artifact_id"] is None
    assert replaced_row["equivalence_disposition"] not in {"unverified", "independent", "released"}
    assert int(replaced_row["equivalence_retry_count"]) == 0
    assert other_row["equivalence_disposition"] == "recovered"  # C attached to the replacement B.
    assert replacement["id"] in await _durable_canonical_targets_for_request_db(other.id)

    await engine.reconcile_executions()
    current = next(item for item in await repository.artifacts(transfer.id) if item.id == replacement["id"])
    assert current.execution is not None
    executor.finish(current.execution)
    for _ in range(2):
        await engine.tick()

    assert await _parent_status(transfer.id) == "completed"
    failed = await _artifact_row(anchor.id)
    assert failed["status"] == "error" and failed["normalized_error"]  # history stays visible.
    assert not failed["blocked"] and (failed["mirror_state"] or "") != "standby"
    assert failed["mirror_group_id"] in (None, anchor.id)
    targets = await _durable_canonical_targets_for_request_db(a.id)
    assert replacement["id"] not in targets  # no A<->B equivalence binding.
    async with database.get_db() as db:
        consolidations = await db.fetchone(
            "SELECT COUNT(*) AS n FROM artifact_consolidations WHERE source_request_id=? OR contributing_artifact_id=?",
            (a.id, anchor.id))
    assert int(consolidations["n"]) == 0
    a_row = await _request_row(a.id)
    assert a_row["equivalence_target_artifact_id"] is None and a_row["equivalence_disposition"] != "unverified"


async def _durable_canonical_targets_for_request_db(request_id):
    async with database.get_db() as db:
        return await _durable_canonical_targets_for_request(db, request_id)


@pytest.mark.parametrize("disposition", ["contradictory", "independent"])
async def test_proven_distinct_failed_artifact_keeps_voting_despite_same_slot_success(
        tmp_path, monkeypatch, disposition):
    """Negative control for the slot-delivery voting rule: a failed artifact
    whose own request was affirmatively proven distinct keeps its FAILED vote
    even though a same-slot artifact completed."""
    repository, engine, executor, transfer, (a, b, c), anchor, _state, _now = await _same_transfer_mirrors(
        tmp_path, monkeypatch, db_name=f"distinct-{disposition}")
    async with database.get_db() as db:
        await db.execute("UPDATE transfer_requests SET equivalence_disposition=? WHERE id=?", (disposition, a.id))
        await db.commit()
    await _fail_anchor(engine, repository, anchor)
    for _ in range(3):
        await engine.tick()
    (replacement,) = await _writers([b.id, c.id])
    await engine.reconcile_executions()
    current = next(item for item in await repository.artifacts(transfer.id) if item.id == replacement["id"])
    executor.finish(current.execution)
    for _ in range(2):
        await engine.tick()
    assert (await _artifact_row(replacement["id"]))["status"] == "completed"
    assert await _parent_status(transfer.id) == "error"


async def test_case_h_two_stale_siblings_woken_concurrently_admit_one_writer(tmp_path, monkeypatch):
    repository, engine, _executor, transfer, (a, b, c), anchor, _state, _now = await _same_transfer_mirrors(
        tmp_path, monkeypatch, db_name="case-h")
    await _fail_anchor(engine, repository, anchor)
    await asyncio.gather(
        engine._process_request(await _refreshed(repository, transfer.id, c.id)),
        engine._process_request(await _refreshed(repository, transfer.id, b.id)),
    )
    writers = await _writers([b.id, c.id])
    assert len(writers) == 1, writers
    for record in (b, c):
        assert (await _request_row(record.id))["equivalence_target_artifact_id"] is None


# ------------------------------------------------------------------ Case B


async def test_case_b_failed_target_with_healthy_alternate_canonical_attaches(tmp_path, monkeypatch):
    """Transfer 323 shape: R is held UNVERIFIED against A; a healthy canonical
    B (another transfer) exists; A fails; fresh evidence proves R ~ B, so R
    attaches to B through the ordinary attach machinery. A's failure itself
    is never the evidence."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "case-b.sqlite3")
    await database.init_db()
    providers = tuple(_UnknownSizeProvider(name) for name in ("anchor", "healthy", "incoming", "other"))
    repository, engine, executor = _build_unknown_size_runtime(tmp_path, monkeypatch, providers)
    await engine.initialize()
    phase = {"anchor_dead": False, "healthy_sig": "healthy-old"}
    signatures = {"anchor": "iso", "incoming": "iso", "other": "other-object"}

    async def fingerprint(subject):
        provider_id = subject.candidate.provider_id
        if provider_id == "anchor" and phase["anchor_dead"]:
            return _DEAD
        signature = phase["healthy_sig"] if provider_id == "healthy" else signatures[provider_id]
        return ArtifactFingerprint(4, signature, FingerprintKind.FULL_CONTENT_SAMPLE)

    executor.fingerprint = fingerprint

    async def single(provider_id, name):
        transfer = await engine.submit((TransferRequest("parcel", provider_id, name=name,
                                                        preferred_provider=provider_id),), name=name,
                                       deduplicate=False)
        (record,) = await repository.requests(transfer.id)
        return transfer, record

    anchor_transfer, anchor_record = await single("anchor", ISO)
    await engine._resolve(anchor_record)
    (anchor,) = await repository.artifacts(anchor_transfer.id)
    healthy_transfer, healthy_record = await single("healthy", ISO)
    await engine._resolve(healthy_record)  # at this moment its samples do not pair with A.
    (healthy,) = await repository.artifacts(healthy_transfer.id)
    phase["anchor_dead"] = True

    incoming_transfer = await engine.submit(
        (TransferRequest("parcel", "incoming", name=ISO, preferred_provider="incoming"),
         TransferRequest("parcel", "other", name="unrelated-object.bin", preferred_provider="other")),
        name=ISO, deduplicate=False,
    )
    by_payload = {record.request.payload: record for record in await repository.requests(incoming_transfer.id)}
    incoming, other = by_payload["incoming"], by_payload["other"]
    await engine._resolve(other)
    await engine._resolve(incoming)
    row = await _request_row(incoming.id)
    assert (row["equivalence_disposition"], row["equivalence_target_artifact_id"]) == ("unverified", anchor.id)

    await _fail_anchor(engine, repository, anchor)
    phase["healthy_sig"] = "iso"  # fresh evidence: incoming ~ healthy.
    await engine._aggregate(incoming_transfer.id)
    assert await _parent_status(incoming_transfer.id) not in {"error", "completed", "consolidated"}
    await engine._process_request(await _refreshed(repository, incoming_transfer.id, incoming.id))

    row = await _request_row(incoming.id)
    assert row["equivalence_target_artifact_id"] is None
    assert row["equivalence_disposition"] == "recovered"
    assert await _durable_canonical_targets_for_request_db(incoming.id) == {healthy.id}
    assert await _writers([incoming.id]) == []  # no third writer.
    assert anchor.id not in await _durable_canonical_targets_for_request_db(incoming.id)


# ------------------------------------------------------------------ Case C


async def test_case_c_live_target_remains_hard_hold(tmp_path, monkeypatch):
    repository, engine, _executor, transfer, (_a, b, c), anchor, state, now = await _same_transfer_mirrors(
        tmp_path, monkeypatch, db_name="case-c")
    async with database.get_db() as db:
        await db.execute("UPDATE transfer_requests SET equivalence_retry_count=2 WHERE id=?", (b.id,))
        await db.commit()
    before = {record.id: await _request_row(record.id) for record in (b, c)}
    probes = state["probes"]
    for _ in range(4):
        now[0] += 60
        await engine.tick()
        for record in (b, c):
            await engine._process_request(await _refreshed(repository, transfer.id, record.id))
    assert {record.id: await _request_row(record.id) for record in (b, c)} == before
    assert state["probes"] == probes  # no background reproof merely because the scheduler ticks.
    assert [item["id"] for item in await _writers([b.id, c.id, anchor.request_id])] == [anchor.id]
    async with database.get_db() as db:
        assert await _repository_base.failed_unverified_target(db, b.id) is None


# ------------------------------------------------------------------ Case D


async def test_case_d_failed_target_never_implies_independence(tmp_path, monkeypatch):
    """No current evidence either way: R's own sampling times out. The stale
    association is cleared and R follows the existing bootstrap hold/retry
    semantics -- it never becomes independent/released because A failed."""
    repository, engine, executor, transfer, (_a, b), anchor, state, now = await _same_transfer_mirrors(
        tmp_path, monkeypatch, db_name="case-d", mirrors=2)

    async def fingerprint(subject):
        state["probes"] += 1
        raise TimeoutError

    executor.fingerprint = fingerprint
    await _fail_anchor(engine, repository, anchor)
    seen = []
    for _ in range(6):
        await engine._process_request(await _refreshed(repository, transfer.id, b.id))
        row = await _request_row(b.id)
        seen.append(row["equivalence_disposition"])
        assert row["equivalence_target_artifact_id"] is None
        now[0] = max(now[0], float(row["retry_at"] or 0)) + 0.01
    assert not {"independent", "released", "contradictory", "provisional"} & set(seen)
    assert seen[-1] == "exhausted" and (await _request_row(b.id))["state"] == "materializing"
    assert await _writers([b.id]) == []


# ------------------------------------------------------------------ Case E


async def test_case_e_restart_rederives_stale_target_from_durable_truth(tmp_path, monkeypatch):
    repository, engine, _executor, transfer, (_a, b, c), anchor, _state, now = await _same_transfer_mirrors(
        tmp_path, monkeypatch, db_name="case-e")
    # Crash after A's durable terminal failure, before any aggregation or
    # invalidation ran; no in-memory event survives.
    await engine.reconcile_executions()
    async with database.get_db() as db:
        await db.execute("UPDATE download_files SET status='error',execution_attempt_id=NULL WHERE id=?",
                         (anchor.id,))
        await db.commit()
    del repository, engine
    providers = tuple(_UnknownSizeProvider(f"case-e-m{index}") for index in range(3))
    repository, engine, executor = _build_unknown_size_runtime(tmp_path, monkeypatch, providers,
                                                               now=lambda: now[0])

    async def fingerprint(subject):
        if subject.candidate.provider_id == providers[0].descriptor.id:
            return _DEAD
        return ArtifactFingerprint(4, "shared-iso", FingerprintKind.FULL_CONTENT_SAMPLE)

    executor.fingerprint = fingerprint
    await engine.initialize()
    await engine._aggregate(transfer.id)
    assert await _parent_status(transfer.id) not in {"error", "completed", "consolidated"}
    for _ in range(3):
        await engine.tick()
    for record in (b, c):
        assert (await _request_row(record.id))["equivalence_target_artifact_id"] is None
    assert len(await _writers([b.id, c.id])) == 1


# ------------------------------------------------------------------ Case F


async def test_case_f_completed_target_is_not_reopened(tmp_path, monkeypatch):
    repository, engine, executor, transfer, (_a, b, c), anchor, _state, _now = await _same_transfer_mirrors(
        tmp_path, monkeypatch, db_name="case-f")
    await engine.reconcile_executions()
    current = next(item for item in await repository.artifacts(transfer.id) if item.id == anchor.id)
    executor.finish(current.execution)
    for _ in range(2):
        await engine.tick()
    assert await _parent_status(transfer.id) == "completed"
    for record in (b, c):
        await engine._process_request(await _refreshed(repository, transfer.id, record.id))
        row = await _request_row(record.id)
        assert (row["equivalence_disposition"], row["equivalence_target_artifact_id"]) == ("unverified", anchor.id)
        async with database.get_db() as db:
            assert await _repository_base.failed_unverified_target(db, record.id) is None
    assert [item["id"] for item in await _writers([b.id, c.id, anchor.request_id])] == [anchor.id]


# ------------------------------------------------------------------ Case G


async def test_case_g_stale_target_invalidation_is_exact_target_cas(tmp_path, monkeypatch):
    repository, engine, _executor, transfer, (_a, b, c), anchor, _state, _now = await _same_transfer_mirrors(
        tmp_path, monkeypatch, db_name="case-g")
    await _fail_anchor(engine, repository, anchor)
    async with database.get_db() as db:
        assert await _repository_base.failed_unverified_target(db, b.id) == anchor.id
        # Worker 2 legitimately re-decides B against a newer target, and C
        # leaves MATERIALIZING, after worker 1 observed the stale pair.
        await db.execute("""INSERT INTO download_files(torrent_id,request_id,filename,size_bytes,status)
            VALUES(?,?,?,4,'queued')""", (transfer.id, None, "newer-target.iso"))
        newer = int((await db.fetchone("SELECT MAX(id) AS id FROM download_files"))["id"])
        await db.execute("UPDATE transfer_requests SET equivalence_target_artifact_id=? WHERE id=?", (newer, b.id))
        await db.execute("UPDATE transfer_requests SET state='resolved' WHERE id=?", (c.id,))
        await db.commit()
    before_b, before_c = await _request_row(b.id), await _request_row(c.id)
    assert await cohorts._invalidate_stale_target(b.id, anchor.id) is False
    assert await cohorts._invalidate_stale_target(c.id, anchor.id) is False
    assert await _request_row(b.id) == before_b
    assert await _request_row(c.id) == before_c
    # A newer disposition is never cleared either.
    async with database.get_db() as db:
        await db.execute("""UPDATE transfer_requests SET state='materializing',equivalence_disposition='recovered',
            equivalence_target_artifact_id=NULL WHERE id=?""", (c.id,))
        await db.commit()
    assert await cohorts._invalidate_stale_target(c.id, anchor.id) is False
    assert (await _request_row(c.id))["equivalence_disposition"] == "recovered"
    # The exact stale pair clears once, idempotently.
    async with database.get_db() as db:
        await db.execute("UPDATE transfer_requests SET equivalence_target_artifact_id=? WHERE id=?", (anchor.id, b.id))
        await db.commit()
    assert await cohorts._invalidate_stale_target(b.id, anchor.id) is True
    assert await cohorts._invalidate_stale_target(b.id, anchor.id) is False
    row = await _request_row(b.id)
    assert (row["equivalence_disposition"], row["equivalence_target_artifact_id"], row["equivalence_retry_count"],
            row["equivalence_reason"]) == ("", None, 0, None)


async def test_replacement_proven_distinct_from_failed_anchor_never_satisfies_its_slot(tmp_path, monkeypatch):
    """Negative control through the REAL mapping path: distinction is recorded
    on the later incoming request B, never on the seed A. A same-slot B that
    was affirmatively proven distinct from A must not suppress A's FAILED
    vote once A fails and B completes."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "distinct-replacement.sqlite3")
    await database.init_db()
    providers = (_UnknownSizeProvider("seed-a"), _UnknownSizeProvider("distinct-b"))
    repository, engine, executor = _build_unknown_size_runtime(tmp_path, monkeypatch, providers)
    await engine.initialize()

    async def fingerprint(subject):
        # Same size, different full-content samples: proof says B != A.
        return ArtifactFingerprint(4, f"content-{subject.candidate.provider_id}", FingerprintKind.FULL_CONTENT_SAMPLE)

    executor.fingerprint = fingerprint
    transfer = await engine.submit(
        tuple(TransferRequest("parcel", p.descriptor.id, name=ISO, preferred_provider=p.descriptor.id)
              for p in providers),
        name=ISO, deduplicate=False,
    )
    by_payload = {record.request.payload: record for record in await repository.requests(transfer.id)}
    a, b = by_payload["seed-a"], by_payload["distinct-b"]
    await engine._resolve(a)
    (anchor,) = await repository.artifacts(transfer.id)
    await engine._resolve(b)

    assert (await _request_row(b.id))["equivalence_disposition"] == "contradictory"
    assert (await _request_row(a.id))["equivalence_disposition"] not in {"contradictory", "independent"}
    (distinct,) = await _writers([b.id])
    artifacts = {item.id: item for item in await repository.artifacts(transfer.id)}
    slot = _repository_base._logical_slot_key_for_artifact(artifacts[anchor.id])
    assert slot and _repository_base._logical_slot_key_for_artifact(artifacts[distinct["id"]]) == slot

    await _fail_anchor(engine, repository, anchor)
    current = next(item for item in await repository.artifacts(transfer.id) if item.id == distinct["id"])
    assert current.execution is not None
    executor.finish(current.execution)
    for _ in range(2):
        await engine.tick()

    assert (await _artifact_row(distinct["id"]))["status"] == "completed"
    assert (await _artifact_row(anchor.id))["status"] == "error"
    assert await _parent_status(transfer.id) == "error"
