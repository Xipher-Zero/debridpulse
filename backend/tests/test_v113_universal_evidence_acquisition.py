"""DP 1.0.13 universal pre-writer evidence acquisition, proven without aria2.

The evidence lifecycle is core-owned and transport-neutral: a CandidateSampling
capability may answer with the existing neutral ``InputRequirement``; the one
existing INPUT_REQUIRED lifecycle (``transfer_input_challenges`` + the one
``EphemeralInputBroker``) carries it; the SAME acquisition continues with the
transient input; equivalence decides BEFORE any writer is admitted; and a
writer admitted for exactly the challenged candidate receives the transient
input once through the existing executor continuation. Everything here runs
against ``VaultExecutor`` -- an unrelated fake transport -- so nothing below can
be secretly coupled to aria2, HTTP, FTP or SFTP.
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import VaultExecutor, VaultProvider
from transfers.input_required import InputSubmissionRejected, public_challenge
from transfers.convergence_engine import TransferEngine
from transfers.models import IntegrationDescriptor, TransferRequest, TransferState
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry
from transfers.models import ExecutionSubject

pytestmark = pytest.mark.asyncio

CONTENT = b"universal-evidence-payload" * 97
OTHER = b"different-bytes-same-name!" * 97
USER, PASSWORD = "vault-user-sentinel", "vault-password-sentinel"


@pytest_asyncio.fixture
async def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "evidence.sqlite3")
    await database.init_db()
    now = [1000.0]
    repository = TransferRepository()
    registry = IntegrationRegistry()
    policy = TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=5)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"), policy=policy,
                            clock=lambda: now[0])
    await engine.initialize()
    executor = VaultExecutor(repository.authorize_execution, objects={
        "open.example/payload.bin": CONTENT,
        "locked.example/payload.bin": CONTENT,
        "other.example/payload.bin": OTHER,
        "locked.example/solo.bin": CONTENT,
        "vault-a.example/payload.bin": CONTENT,
        "vault-b.example/payload.bin": CONTENT,
        "vault-c.example/payload.bin": bytes(value ^ 1 for value in CONTENT),  # same size, other bytes
        "alt.example/solo.bin": OTHER + b"alt",
    }, locks={"locked.example": (USER, PASSWORD), "vault-a.example": ("owner-a", "secret-a"),
              "vault-b.example": ("owner-b", "secret-b"), "vault-c.example": ("owner-c", "secret-c"),
              "alt.example": ("owner-alt", "secret-alt")})
    registry.register_provider(VaultProvider())
    registry.register_executor(executor)
    return repository, registry, engine, executor, now, tmp_path


async def _ticks(engine, count=4):
    for _ in range(count):
        await engine.tick()


async def _challenged(engine, repository, transfer_id):
    await _ticks(engine)
    challenge = await engine.challenges.current(transfer_id)
    assert challenge is not None, "evidence acquisition must surface the neutral INPUT_REQUIRED challenge"
    assert (await repository.get(transfer_id)).state == TransferState.INPUT_REQUIRED
    return challenge


async def _submit(engine, transfer_id, challenge, username=USER, password=PASSWORD):
    await engine.submit_input(transfer_id, challenge.id, "username_password",
                              {"username": username, "password": password})


async def _db_dump():
    async with database.get_db() as db:
        tables = [row["name"] for row in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
                  if not row["name"].startswith("sqlite_")]
        return json.dumps({name: await db.fetchall(f"SELECT * FROM {name}") for name in tables},
                          sort_keys=True, default=str)


async def _canonical_seed(engine, repository):
    """An existing canonical artifact whose candidate samples without input."""
    seed = await engine.submit((TransferRequest("vault", "open.example/payload.bin", name="payload.bin"),),
                               deduplicate=False)
    await _ticks(engine, 3)
    artifacts = await repository.artifacts(seed.id)
    assert len(artifacts) == 1
    return seed, artifacts[0]


async def test_immediate_fingerprint_needs_no_challenge(lab):
    repository, _registry, engine, executor, _now, _ = lab
    seed, _artifact = await _canonical_seed(engine, repository)
    same = await engine.submit((TransferRequest("vault", "other.example/payload.bin", name="payload.bin"),),
                               deduplicate=False)
    await _ticks(engine)
    assert await engine.challenges.current(same.id) is None
    assert all(username is None for _candidate, username in executor.samples)
    assert len(await repository.artifacts(same.id)) == 1  # proven distinct bytes -> its own writer


async def test_evidence_requirement_is_one_existing_challenge_before_any_writer(lab):
    repository, _registry, engine, _executor, _now, _ = lab
    seed, canonical = await _canonical_seed(engine, repository)
    incoming = await engine.submit((TransferRequest("vault", "locked.example/payload.bin", name="payload.bin"),),
                                   deduplicate=False)
    challenge = await _challenged(engine, repository, incoming.id)
    record = (await repository.requests(incoming.id))[0]
    candidate = (await repository.resolved_candidates(record.id))[0]

    assert challenge.origin.value == "evidence"
    assert challenge.reason.value == "auth_required"
    assert challenge.request_id == record.id
    assert challenge.operation_id == str(candidate.id)
    assert challenge.integration_id == "vault-copy"
    assert challenge.artifact_id is None
    assert record.state == "materializing"  # never a request-level input_required: siblings stay in the cohort
    assert await repository.artifacts(incoming.id) == ()  # no writer while evidence waits on input
    assert set(public_challenge(challenge)) == {"id", "generation", "reason", "origin", "methods", "facts"}
    async with database.get_db() as db:
        challenge_tables = {row["name"] for row in await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%challenge%'")}
        rows = await db.fetchall("SELECT origin,artifact_id FROM transfer_input_challenges")
    assert challenge_tables == {"transfer_input_challenges"}
    assert [dict(row) for row in rows] == [{"origin": "evidence", "artifact_id": None}]
    assert (await repository.artifacts(seed.id))[0].id == canonical.id


async def test_submitted_input_continues_the_same_acquisition_and_attaches_before_any_writer(lab):
    repository, _registry, engine, executor, _now, _ = lab
    seed, canonical = await _canonical_seed(engine, repository)
    incoming = await engine.submit((TransferRequest("vault", "locked.example/payload.bin", name="payload.bin"),),
                                   deduplicate=False)
    challenge = await _challenged(engine, repository, incoming.id)
    await _submit(engine, incoming.id, challenge)
    await _ticks(engine)

    assert (await repository.get(incoming.id)).state == TransferState.CONSOLIDATED
    assert await repository.artifacts(incoming.id) == ()  # equivalence decided first: no independent writer ever
    bindings = await engine.canonical.bindings(canonical.id)
    assert len(bindings) == 2
    assert await engine.challenges.current(incoming.id) is None
    assert (str((await repository.resolved_candidates((await repository.requests(incoming.id))[0].id))[0].id), USER) \
        in executor.samples
    # Attached as an alternate of another request's artifact: nothing is handed to execution.
    assert executor.input_starts == []
    assert USER not in await _db_dump() and PASSWORD not in await _db_dump()


async def test_wrong_input_replaces_the_challenge_generation_and_fences_the_old_one(lab):
    repository, _registry, engine, _executor, _now, _ = lab
    await _canonical_seed(engine, repository)
    incoming = await engine.submit((TransferRequest("vault", "locked.example/payload.bin", name="payload.bin"),),
                                   deduplicate=False)
    first = await _challenged(engine, repository, incoming.id)
    await _submit(engine, incoming.id, first, password="wrong-password-sentinel")
    await _ticks(engine)
    second = await engine.challenges.current(incoming.id)
    assert second is not None and second.id != first.id and second.generation == first.generation + 1
    assert second.origin.value == "evidence" and second.operation_id == first.operation_id
    assert await repository.artifacts(incoming.id) == ()  # wrong input never admits a writer
    with pytest.raises(InputSubmissionRejected):
        await _submit(engine, incoming.id, first)
    await _submit(engine, incoming.id, second)
    await _ticks(engine)
    assert (await repository.get(incoming.id)).state == TransferState.CONSOLIDATED
    assert "wrong-password-sentinel" not in await _db_dump()


async def test_changed_candidate_generation_rejects_the_stale_challenge(lab):
    repository, _registry, engine, _executor, _now, _ = lab
    await _canonical_seed(engine, repository)
    incoming = await engine.submit((TransferRequest("vault", "locked.example/payload.bin", name="payload.bin"),),
                                   deduplicate=False)
    challenge = await _challenged(engine, repository, incoming.id)
    record = (await repository.requests(incoming.id))[0]
    # A newer resolution of the same request carries a new candidate identity.
    async with database.get_db() as db:
        row = await db.fetchone("SELECT id,result FROM resolution_attempts WHERE request_id=? AND state='succeeded'",
                                (record.id,))
        await db.execute("UPDATE resolution_attempts SET result=? WHERE id=?",
                         (row["result"].replace(challenge.operation_id, "f" * 32), row["id"]))
        await db.commit()
    with pytest.raises(InputSubmissionRejected):
        await _submit(engine, incoming.id, challenge)
    assert await engine.challenges.current(incoming.id) is None
    assert await repository.artifacts(incoming.id) == ()


async def test_changed_sampling_integration_rejects_the_stale_challenge(lab):
    repository, registry, engine, _executor, _now, _ = lab
    await _canonical_seed(engine, repository)
    incoming = await engine.submit((TransferRequest("vault", "locked.example/payload.bin", name="payload.bin"),),
                                   deduplicate=False)
    challenge = await _challenged(engine, repository, incoming.id)
    successor = VaultExecutor(repository.authorize_execution)
    successor.descriptor = replace(VaultExecutor.descriptor, id="vault-successor", priority=10)
    registry.register_executor(successor)
    with pytest.raises(InputSubmissionRejected):
        await _submit(engine, incoming.id, challenge)
    assert await engine.challenges.current(incoming.id) is None


async def test_integration_change_after_submission_fails_closed_in_the_continuation(lab):
    repository, registry, engine, executor, _now, _ = lab
    await _canonical_seed(engine, repository)
    incoming = await engine.submit((TransferRequest("vault", "locked.example/payload.bin", name="payload.bin"),),
                                   deduplicate=False)
    challenge = await _challenged(engine, repository, incoming.id)
    await _submit(engine, incoming.id, challenge)
    registry.executors["vault-copy"].descriptor = replace(VaultExecutor.descriptor, enabled=False)
    await engine.resolve_pending()
    assert all(username != USER for _candidate, username in executor.samples)
    assert await engine.challenges.current(incoming.id) is None
    assert await repository.artifacts(incoming.id) == ()


async def _writer_through_evidence(lab):
    """A locked candidate proven DISTINCT from the canonical: its own writer is
    admitted by the evidence decision for exactly the challenged candidate."""
    repository, _registry, engine, _executor, _now, _ = lab
    await _canonical_seed(engine, repository)
    incoming = await engine.submit((TransferRequest("vault", "locked.example/solo.bin", name="payload.bin"),),
                                   deduplicate=False)
    # solo.bin holds the same bytes; make them differ so the decision admits a writer.
    lab[3].objects["locked.example/solo.bin"] = OTHER + b"!"
    challenge = await _challenged(engine, repository, incoming.id)
    await _submit(engine, incoming.id, challenge)
    return incoming, challenge


async def test_evidence_input_reaches_the_existing_execution_continuation_exactly_once(lab):
    repository, _registry, engine, executor, _now, _ = lab
    incoming, challenge = await _writer_through_evidence(lab)
    await _ticks(engine, 3)
    artifacts = await repository.artifacts(incoming.id)
    assert len(artifacts) == 1
    assert executor.input_starts == [(challenge.operation_id, USER)]  # no second prompt, handed off once
    assert await engine.challenges.current(incoming.id) is None
    assert artifacts[0].state in {"downloading", "queued"}
    await _ticks(engine, 3)
    assert executor.input_starts == [(challenge.operation_id, USER)]
    record = (await repository.requests(incoming.id))[0]
    assert await engine.inputs.take_handoff(incoming.id, record.id, challenge.operation_id, "vault-copy") is None


async def test_handoff_is_bound_to_its_canonical_identities(lab):
    repository, _registry, engine, _executor, _now, _ = lab
    incoming, challenge = await _writer_through_evidence(lab)
    await engine.resolve_pending()  # evidence decision + writer admission, before any dispatch
    record = (await repository.requests(incoming.id))[0]
    for key in (
        (incoming.id, "another-request", challenge.operation_id, "vault-copy"),
        (incoming.id + 1, record.id, challenge.operation_id, "vault-copy"),
    ):
        assert await engine.inputs.take_handoff(*key) is None
    handed = await engine.inputs.take_handoff(incoming.id, record.id, challenge.operation_id, "vault-copy")
    assert handed is not None and handed.secret_values() == (USER, PASSWORD)
    assert await engine.inputs.take_handoff(incoming.id, record.id, challenge.operation_id, "vault-copy") is None
    assert "values=<redacted>" in repr(handed)
    # Admitting a DIFFERENT candidate or integration for the same request means
    # the selected candidate was replaced: the stale handoff is discarded, not reused.
    for replacement in (("0" * 32, "vault-copy"), (challenge.operation_id, "another-executor")):
        await engine.inputs.hand_off(incoming.id, record.id, challenge.operation_id, "vault-copy", handed)
        assert await engine.inputs.take_handoff(incoming.id, record.id, *replacement) is None
        assert await engine.inputs.take_handoff(incoming.id, record.id, challenge.operation_id, "vault-copy") is None
        assert handed.secret_values() == ()


async def test_handoff_is_discarded_when_the_transfer_is_cancelled(lab):
    repository, _registry, engine, executor, _now, _ = lab
    incoming, challenge = await _writer_through_evidence(lab)
    await engine.resolve_pending()
    record = (await repository.requests(incoming.id))[0]
    await engine.cancel(incoming.id)
    assert await engine.inputs.take_handoff(incoming.id, record.id, challenge.operation_id, "vault-copy") is None
    await _ticks(engine, 2)
    assert executor.input_starts == []


async def test_handoff_expires_with_the_broker_lifetime(lab):
    repository, _registry, engine, executor, now, _ = lab
    incoming, challenge = await _writer_through_evidence(lab)
    await engine.resolve_pending()
    now[0] += engine.inputs.lifetime_seconds + 1
    record = (await repository.requests(incoming.id))[0]
    assert await engine.inputs.take_handoff(incoming.id, record.id, challenge.operation_id, "vault-copy") is None


async def test_restart_recovers_no_secret_and_protected_execution_asks_again(lab):
    repository, registry, engine, executor, now, tmp_path = lab
    incoming, challenge = await _writer_through_evidence(lab)
    await engine.resolve_pending()  # handoff retained in THIS process only
    dump = await _db_dump()
    assert USER not in dump and PASSWORD not in dump
    restarted = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                               policy=engine.policy, clock=lambda: now[0])
    await restarted.initialize()
    await restarted.reconcile_executions()
    assert executor.input_starts == []  # nothing reconstructed the secret
    challenge_after = await restarted.challenges.current(incoming.id)
    assert challenge_after is not None and challenge_after.origin.value == "executor"
    assert (await repository.get(incoming.id)).state == TransferState.INPUT_REQUIRED


async def test_restart_while_evidence_input_pending_requires_new_submission(lab):
    repository, registry, engine, executor, now, tmp_path = lab
    await _canonical_seed(engine, repository)
    incoming = await engine.submit((TransferRequest("vault", "locked.example/payload.bin", name="payload.bin"),),
                                   deduplicate=False)
    challenge = await _challenged(engine, repository, incoming.id)
    await _submit(engine, incoming.id, challenge)
    restarted = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                               policy=engine.policy, clock=lambda: now[0])
    await restarted.initialize()
    for _ in range(3):
        await restarted.tick()
    assert all(username != USER for _candidate, username in executor.samples)
    current = await restarted.challenges.current(incoming.id)
    assert current is not None and current.id == challenge.id  # durable, non-secret challenge survives
    assert await repository.artifacts(incoming.id) == ()
    await _submit(restarted, incoming.id, current)
    for _ in range(3):
        await restarted.tick()
    assert (await repository.get(incoming.id)).state == TransferState.CONSOLIDATED


async def test_unsupported_sampler_still_fails_closed_without_a_challenge(lab):
    repository, registry, engine, _executor, _now, _ = lab
    await _canonical_seed(engine, repository)

    class NoSampling(VaultExecutor):
        # A runtime-checkable protocol member explicitly set to None is absent:
        # this executor has no evidence capability at all.
        fingerprint = None
        fingerprint_with_input = None
        capabilities = replace(VaultExecutor.capabilities, candidate_sampling=False)
        descriptor = IntegrationDescriptor("vault-plain", "Vault plain", frozenset(), priority=20)

    registry.register_executor(NoSampling(repository.authorize_execution, locks={"locked.example": (USER, PASSWORD)}))
    incoming = await engine.submit((TransferRequest("vault", "locked.example/payload.bin", name="payload.bin"),),
                                   deduplicate=False)
    await _ticks(engine)
    # No evidence capability: the existing structural fallback decides, and no
    # evidence challenge is ever invented; execution keeps its own lifecycle.
    current = await engine.challenges.current(incoming.id)
    assert current is None or current.origin.value == "executor"
    async with database.get_db() as db:
        row = await db.fetchone("SELECT equivalence_reason FROM transfer_requests WHERE transfer_id=?", (incoming.id,))
    assert row["equivalence_reason"] == "sampler_unsupported"


# ── Protected canonical: durable neutral evidence, never borrowed input ──────

async def _protected_canonical(lab):
    """Request A (protected) proves bytes X with its own input and becomes a
    canonical writer; its transient input is then gone."""
    repository, _registry, engine, executor, _now, _ = lab
    first = await engine.submit((
        TransferRequest("vault", "other.example/payload.bin", name="payload.bin"),
        TransferRequest("vault", "vault-a.example/payload.bin", name="payload.bin"),
    ), deduplicate=False)
    challenge = await _challenged(engine, repository, first.id)
    await _submit(engine, first.id, challenge, username="owner-a", password="secret-a")
    await _ticks(engine, 3)
    artifacts = await repository.artifacts(first.id)
    record_a = next(item for item in await repository.requests(first.id) if "vault-a" in item.request.payload)
    owned = next(item for item in artifacts if item.request_id == record_a.id)
    evidence = owned.candidates[owned.selected].content_evidence
    assert evidence is not None and evidence.total_bytes == len(CONTENT)
    return first, owned, evidence


async def _restart(lab):
    repository, registry, engine, _executor, now, tmp_path = lab
    restarted = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                               policy=engine.policy, clock=lambda: now[0])
    await restarted.initialize()
    return restarted


async def test_later_protected_mirror_converges_on_retained_evidence_after_restart(lab):
    repository, _registry, _engine, executor, _now, _ = lab
    first, canonical, evidence = await _protected_canonical(lab)
    engine = await _restart(lab)  # every transient input from A's process is gone
    samples_before = list(executor.samples)
    later = await engine.submit((TransferRequest("vault", "vault-b.example/payload.bin", name="payload.bin"),),
                                deduplicate=False)
    challenge = await _challenged(engine, repository, later.id)
    assert challenge.origin.value == "evidence"
    await _submit(engine, later.id, challenge, username="owner-b", password="secret-b")
    await _ticks(engine)

    assert (await repository.get(later.id)).state == TransferState.CONSOLIDATED
    assert await repository.artifacts(later.id) == ()  # one canonical artifact, one writer
    bindings = await engine.canonical.bindings(canonical.id)
    assert {(binding["source_identity"] or {}).get("key") for binding in bindings} >= {"vault-a.example",
                                                                                         "vault-b.example"}
    a_candidate = str(canonical.candidates[canonical.selected].id)
    new_samples = executor.samples[len(samples_before):]
    # A's source was only ever asked WITHOUT input after restart; B used only B's input.
    assert all(username is None for candidate_id, username in new_samples if candidate_id == a_candidate)
    assert {username for _candidate_id, username in new_samples if username} == {"owner-b"}
    dump = await _db_dump()
    assert evidence.signature in dump
    for secret in ("secret-a", "secret-b", "owner-a", "owner-b"):
        assert secret not in dump


async def test_later_protected_mirror_with_different_bytes_is_proven_distinct(lab):
    repository, _registry, _engine, _executor, _now, _ = lab
    first, canonical, _evidence = await _protected_canonical(lab)
    engine = await _restart(lab)
    later = await engine.submit((TransferRequest("vault", "vault-c.example/payload.bin", name="payload.bin"),),
                                deduplicate=False)
    challenge = await _challenged(engine, repository, later.id)
    await _submit(engine, later.id, challenge, username="owner-c", password="secret-c")
    await _ticks(engine)
    assert (await repository.get(later.id)).state != TransferState.CONSOLIDATED
    assert len(await repository.artifacts(later.id)) == 1  # its own writer: proven distinct
    async with database.get_db() as db:
        row = await db.fetchone("SELECT equivalence_disposition,equivalence_reason FROM transfer_requests "
                                "WHERE transfer_id=?", (later.id,))
    # Proven distinct under the existing contradictory class (the fake executor's
    # reported progress also makes the canonical side's size known).
    assert row["equivalence_disposition"] == "contradictory"
    assert row["equivalence_reason"] in {"sample_mismatch", "size_disagreement"}


async def test_retained_evidence_never_replaces_a_live_acquisition_that_needs_no_input(lab):
    """Retention is a fallback for input the deciding request cannot hold --
    a candidate that samples live is always sampled live."""
    from dataclasses import replace as dc_replace
    from transfers.mirrors import EvidenceContext
    from transfers.models import ArtifactFingerprint
    repository, _registry, engine, executor, _now, _ = lab
    seed, artifact = await _canonical_seed(engine, repository)
    stale = ArtifactFingerprint(1, "stale-signature")
    candidate = dc_replace(artifact.candidates[0], content_evidence=stale)
    live = await EvidenceContext().fingerprint(executor, candidate)
    assert live != stale and live.total_bytes == len(CONTENT)


# ── Evidence -> execution handoff is serialized with pause ───────────────────
# The handoff has exactly one disposition: consumed by the admitted writer,
# retained while a paused writer is still current, or discarded once the
# transfer/candidate generation is gone. "Absent because pause raced start"
# is not a state: every pause-intent write takes the same admission lock that
# covers prepare_execution -> start_with_input.

import asyncio  # noqa: E402


async def _admitted_but_not_started(lab):
    """Evidence decided a writer for the challenged candidate; nothing dispatched."""
    repository, _registry, engine, executor, _now, _ = lab
    incoming, challenge = await _writer_through_evidence(lab)
    await engine.resolve_pending()
    record = (await repository.requests(incoming.id))[0]
    executor.calls.clear()  # only this writer's native actions count from here (the seed already started)
    return incoming, challenge, record


async def _handoff_present(engine, incoming, record, challenge):
    """Non-destructive probe: take and immediately return the same handoff."""
    handed = await engine.inputs.take_handoff(incoming.id, record.id, challenge.operation_id, "vault-copy")
    if handed is None:
        return False
    await engine.inputs.hand_off(incoming.id, record.id, challenge.operation_id, "vault-copy", handed)
    return True


async def test_pause_arriving_between_prepare_and_start_is_ordered_after_the_consuming_start(lab, monkeypatch):
    repository, _registry, engine, executor, _now, _ = lab
    incoming, challenge, record = await _admitted_but_not_started(lab)
    order = []
    pause_task = None
    original_prepare = repository.prepare_execution

    async def prepare_then_pause(artifact, handle, **kwargs):
        nonlocal pause_task
        prepared = await original_prepare(artifact, handle, **kwargs)
        if prepared and pause_task is None:
            # The operator pauses in the exact window between durable
            # preparation and the native start.
            pause_task = asyncio.create_task(engine.pause(incoming.id))
            for _ in range(20):
                await asyncio.sleep(0)
            order.append(("pause_done_before_start", pause_task.done()))
        return prepared

    original_start = executor.start_with_input

    async def recording_start(request, handle, submitted):
        order.append(("start_with_input", await repository.authorize_execution(handle, "start")))
        return await original_start(request, handle, submitted)

    monkeypatch.setattr(repository, "prepare_execution", prepare_then_pause)
    monkeypatch.setattr(executor, "start_with_input", recording_start)
    await engine.reconcile_executions()
    await pause_task
    assert order == [("pause_done_before_start", False), ("start_with_input", True)]  # never started while paused
    assert executor.input_starts == [(challenge.operation_id, USER)]  # consumed exactly once
    starts = [call for call in executor.calls if call[0] == "start"]
    assert len(starts) == 1  # exactly one native execution
    artifact = (await repository.artifacts(incoming.id))[0]
    assert (await repository.get(incoming.id)).paused
    assert executor.jobs[artifact.execution.attempt_id].state.value == "paused"
    await engine.resume(incoming.id)
    for _ in range(3):
        await engine.tick()
    artifact_after = (await repository.artifacts(incoming.id))[0]
    assert artifact_after.id == artifact.id and artifact_after.request_id == record.id
    assert artifact_after.candidates[artifact_after.selected].id == artifact.candidates[artifact.selected].id
    assert executor.input_starts == [(challenge.operation_id, USER)]
    assert len([call for call in executor.calls if call[0] == "start"]) == 1
    assert await engine.challenges.current(incoming.id) is None  # no duplicate challenge from the race


async def test_pause_before_admission_retains_the_handoff_until_resume_consumes_it(lab):
    repository, _registry, engine, executor, _now, _ = lab
    incoming, challenge, record = await _admitted_but_not_started(lab)
    await engine.pause(incoming.id)
    for _ in range(3):
        await engine.reconcile_executions()
    assert executor.input_starts == [] and [call for call in executor.calls if call[0] == "start"] == []
    assert await _handoff_present(engine, incoming, record, challenge)  # retained while paused and current
    await engine.resume(incoming.id)
    for _ in range(3):
        await engine.tick()
    assert executor.input_starts == [(challenge.operation_id, USER)]
    assert len([call for call in executor.calls if call[0] == "start"]) == 1
    assert await engine.challenges.current(incoming.id) is None
    assert not await _handoff_present(engine, incoming, record, challenge)


async def test_pause_then_cancel_discards_the_handoff(lab):
    repository, _registry, engine, executor, _now, _ = lab
    incoming, challenge, record = await _admitted_but_not_started(lab)
    await engine.pause(incoming.id)
    await engine.cancel(incoming.id)
    assert not await _handoff_present(engine, incoming, record, challenge)
    await engine.tick()
    assert executor.input_starts == []


async def test_pause_then_delete_discards_the_handoff(lab):
    repository, _registry, engine, executor, _now, _ = lab
    incoming, challenge, record = await _admitted_but_not_started(lab)
    await engine.pause(incoming.id)
    await engine.delete(incoming.id)
    assert not await _handoff_present(engine, incoming, record, challenge)
    await engine.tick()
    assert executor.input_starts == []


async def test_pause_then_candidate_replacement_discards_the_handoff_instead_of_reusing_it(lab):
    from transfers.manual_failover import manual_candidate_failover
    repository, _registry, engine, executor, _now, _ = lab
    await _canonical_seed(engine, repository)
    lab[3].objects["locked.example/solo.bin"] = OTHER + b"!"
    incoming = await engine.submit((TransferRequest(
        "vault", "locked.example/solo.bin|alt.example/solo.bin", name="payload.bin"),), deduplicate=False)
    challenge = await _challenged(engine, repository, incoming.id)
    await _submit(engine, incoming.id, challenge)
    await engine.resolve_pending()
    artifact = (await repository.artifacts(incoming.id))[0]
    record = (await repository.requests(incoming.id))[0]
    assert str(artifact.candidates[artifact.selected].id) == challenge.operation_id and len(artifact.candidates) == 2
    await engine.pause(incoming.id)
    replacement = next(item for item in artifact.candidates if str(item.id) != challenge.operation_id)
    await manual_candidate_failover(engine, incoming.id, artifact.id, str(replacement.id))
    await engine.resume(incoming.id)
    for _ in range(3):
        await engine.tick()
    # The replacement candidate started without the other candidate's input,
    # and the stale handoff is gone rather than lingering for reuse.
    assert executor.input_starts == []
    assert not await _handoff_present(engine, incoming, record, challenge)
    current = await engine.challenges.current(incoming.id)
    assert current is None or current.origin.value == "executor"  # the replacement asks for its own input
