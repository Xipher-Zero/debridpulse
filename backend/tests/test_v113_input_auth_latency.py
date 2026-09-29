"""DP 1.0.13 INPUT_REQUIRED authentication latency: the operator's question
follows the AUTHENTICATION outcome, never the materialization outcome.

An answered evidence challenge continues the same acquisition inside one
ordinary materialization decision. The moment the challenged candidate proves
(or definitively refuses) the submitted material, the durable question must
converge -- cleared when accepted, reissued as its next generation when
refused -- while the rest of that decision (peer evidence, equivalence,
attach/allocate) is still running. Every later step is deliberately held on an
``asyncio.Event`` here, so the ordering is proven, never timed.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import db.database as database
from fake_integrations import MemoryExecutor, VaultExecutor, VaultProvider
from test_input_required_lifecycle import base  # noqa: F401  (fixture)
from transfers.input_required import server_identity_required, username_password
from transfers.models import (
    ArtifactFingerprint, FingerprintKind, InputFactName, InputField, InputOrigin, TransferRequest, TransferState,
)

pytestmark = pytest.mark.asyncio
USER, SECRET = "evidence-user", "evidence-secret"


class HeldVault(VaultExecutor):
    """Records every milestone of the challenged acquisition, and holds every
    sampling that happens after it (the rest of the decision) until released."""

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.validated = asyncio.Event()      # the challenged candidate answered
        self.held = asyncio.Event()           # later decision work is blocked
        self.release = asyncio.Event()
        self.milestones = []
        self.passwords = []

    def _mark(self, name):
        self.milestones.append((name, time.monotonic()))

    async def start_with_input(self, request, handle, submitted):
        """The writer admitted for a candidate: records what it was handed."""
        self.input_starts.append((str(request.work.subject.candidate.id), submitted.value(InputField.USERNAME)))
        return await MemoryExecutor.start(self, request, handle)

    async def fingerprint(self, subject):
        if self.validated.is_set() and not self.release.is_set():
            self._mark("broader_work_blocked")
            self.held.set()
            await self.release.wait()
            self._mark("broader_work_released")
        return await super().fingerprint(subject)

    async def fingerprint_with_input(self, subject, submitted):
        self._mark("challenged_candidate_validating")
        self.passwords.append(submitted.value(InputField.PASSWORD))
        sample = await super().fingerprint_with_input(subject, submitted)
        self._mark("challenged_candidate_answered")
        self.validated.set()
        return sample


async def _challenged(base, incoming_payload="locked.example/item.bin", *, executor=None, locked=b"same-bytes"):
    repository, registry, engine, _now = base
    registry.register_provider(VaultProvider())
    vault = (executor or HeldVault)(repository.authorize_execution, objects={
        "open.example/item.bin": b"same-bytes", "locked.example/item.bin": locked,
        "other.example/item.bin": b"same-bytes",
    }, locks={"locked.example": (USER, SECRET), "other.example": ("other-user", "other-secret")})
    vault.release.set()  # nothing is held while the challenge is being raised
    registry.register_executor(vault)
    await engine.submit((TransferRequest("vault", "open.example/item.bin", name="item.bin"),), deduplicate=False)
    for _ in range(3):
        await engine.tick()
    incoming = await engine.submit((TransferRequest("vault", incoming_payload, name="item.bin"),),
                                   deduplicate=False)
    for _ in range(3):
        await engine.tick()
    challenge = await engine.challenges.current(incoming.id)
    assert challenge is not None and challenge.origin == InputOrigin.EVIDENCE
    vault.release.clear()
    return repository, engine, vault, incoming, challenge


async def _auth_facts(transfer_id):
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT kind FROM application_events WHERE transfer_id=? ORDER BY id", (transfer_id,))
    return [row["kind"] for row in rows]


async def test_an_accepted_answer_closes_the_question_while_materialization_is_still_held(base):
    repository, engine, vault, incoming, challenge = await _challenged(base)
    await engine.submit_input(incoming.id, challenge.id, "username_password", {"username": USER, "password": SECRET})
    decision = asyncio.ensure_future(engine.tick())
    try:
        await asyncio.wait_for(vault.held.wait(), timeout=10)
        # The broader decision is blocked AFTER the challenged candidate
        # proved the answer: the durable question is already gone.
        assert "auth_accepted" in await _auth_facts(incoming.id)
        assert await engine.challenges.current(incoming.id) is None
        closed_at = time.monotonic()
        answered_at = dict(vault.milestones)["challenged_candidate_answered"]
        detail = await repository.presentation(incoming.id, details=True)
        # The transfer itself no longer presents the question either.
        assert detail["input_required"] is None and detail["status"] != "input_required"
        assert (await repository.get(incoming.id)).state != TransferState.INPUT_REQUIRED
    finally:
        vault.release.set()
    await asyncio.wait_for(decision, timeout=10)
    # The SAME decision still finishes its materialization decision correctly.
    for _ in range(3):
        await engine.tick()
    assert (await repository.get(incoming.id)).state == TransferState.CONSOLIDATED
    assert closed_at - answered_at < 1.0


async def test_a_refused_answer_reissues_the_question_while_materialization_is_still_held(base):
    repository, engine, vault, incoming, challenge = await _challenged(
        base, "locked.example/item.bin|other.example/item.bin")
    await engine.submit_input(incoming.id, challenge.id, "username_password", {"username": USER, "password": "wrong"})
    decision = asyncio.ensure_future(engine.tick())
    try:
        await asyncio.wait_for(vault.held.wait(), timeout=10)
        assert "auth_rejected" in await _auth_facts(incoming.id)
        current = await engine.challenges.current(incoming.id)
        # The next generation is already the question the operator sees.
        assert current is not None and current.generation == challenge.generation + 1
        assert current.operation_id == challenge.operation_id and current.origin == InputOrigin.EVIDENCE
    finally:
        vault.release.set()
    await asyncio.wait_for(decision, timeout=10)
    # The decision's own end never issues the same question a second time.
    after = await engine.challenges.current(incoming.id)
    assert after is not None and after.id == current.id and after.generation == current.generation
    # The refused material is never offered again: one attempt with it.
    assert vault.passwords.count("wrong") == 1
    # ...and the reissued generation is answerable to completion.
    vault.release.set()
    await engine.submit_input(incoming.id, after.id, "username_password", {"username": USER, "password": SECRET})
    for _ in range(4):
        await engine.tick()
    assert (await repository.get(incoming.id)).state == TransferState.CONSOLIDATED
    assert (await engine.challenges.current(incoming.id)) is None


async def test_the_accepted_answer_still_reaches_the_writer_admitted_for_exactly_that_candidate(base):
    """Closing the question does not end the answer's legitimate life: the
    decision that proved it hands it, once, to the writer it admits."""
    repository, engine, vault, incoming, challenge = await _challenged(base, locked=b"different-bytes")
    await engine.submit_input(incoming.id, challenge.id, "username_password", {"username": USER, "password": SECRET})
    decision = asyncio.ensure_future(engine.tick())
    try:
        await asyncio.wait_for(vault.held.wait(), timeout=10)
        assert await engine.challenges.current(incoming.id) is None
    finally:
        vault.release.set()
    await asyncio.wait_for(decision, timeout=10)
    # The one execution slot is the seed's until its writer finishes.
    for artifact in await repository.artifacts(1):
        if artifact.execution is not None:
            vault.finish(artifact.execution)
    for _ in range(4):
        await engine.tick()
    (artifact,) = await repository.artifacts(incoming.id)
    assert artifact.execution is not None and artifact.state == "downloading"
    # Proven once by evidence, handed once to its writer; never asked again.
    assert [user for _candidate, user in vault.input_starts] == [USER]
    assert vault.passwords == [SECRET] and await engine.challenges.current(incoming.id) is None


FINGERPRINT = "b" * 40


class HeldIdentityVault(HeldVault):
    """The locked host also has a server identity: sampling asks for it (with
    the credential) until the answer carries exactly that identity."""

    def _requirement(self):
        return server_identity_required(username_password(), host="locked.example", algorithm="sha-1",
                                        fingerprint=FINGERPRINT)

    async def fingerprint(self, subject):
        sample = await super().fingerprint(subject)
        return self._requirement() if "locked.example" in subject.candidate.endpoints[0].address else sample

    async def fingerprint_with_input(self, subject, submitted):
        facts = {fact.name: fact.value for fact in submitted.facts}
        if facts.get(InputFactName.SERVER_IDENTITY_FINGERPRINT) != FINGERPRINT:
            self.validated.set()
            return self._requirement()
        return await super().fingerprint_with_input(subject, submitted)


async def test_a_confirmed_server_identity_closes_the_question_at_the_same_boundary(base):
    repository, engine, vault, incoming, challenge = await _challenged(base, executor=HeldIdentityVault)
    assert challenge.reason.value == "server_identity_required"
    await engine.submit_input(incoming.id, challenge.id, "username_password", {"username": USER, "password": SECRET})
    decision = asyncio.ensure_future(engine.tick())
    try:
        await asyncio.wait_for(vault.held.wait(), timeout=10)
        facts = await _auth_facts(incoming.id)
        assert "server_identity_confirmed" in facts and "auth_accepted" in facts
        assert await engine.challenges.current(incoming.id) is None
    finally:
        vault.release.set()
    await asyncio.wait_for(decision, timeout=10)
    for _ in range(3):
        await engine.tick()
    assert (await repository.get(incoming.id)).state == TransferState.CONSOLIDATED


class UnreachableVault(HeldVault):
    """The answer cannot be judged: the transport never got far enough."""

    async def fingerprint_with_input(self, subject, submitted):
        self.passwords.append(submitted.value(InputField.PASSWORD))
        self.validated.set()
        return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "connection_timeout")


async def test_an_answer_the_transport_could_not_judge_is_neither_accepted_nor_refused(base):
    """A timeout or network failure is not a verdict on the credential: nothing
    is settled, the question is never reissued as 'authentication failed', and
    the decision ends exactly as it did before (characterized, unchanged)."""
    repository, engine, vault, incoming, challenge = await _challenged(base, executor=UnreachableVault)
    vault.release.set()
    await engine.submit_input(incoming.id, challenge.id, "username_password", {"username": USER, "password": SECRET})
    await engine.tick()
    facts = await _auth_facts(incoming.id)
    assert "auth_accepted" not in facts and "auth_rejected" not in facts
    assert facts.count("input_required") == 1  # never reissued as a refusal
    current = await engine.challenges.current(incoming.id)
    assert current is None or current.generation == challenge.generation
    assert engine._evidence_answers == {} and engine._evidence_reissued == {}
    (record,) = await repository.requests(incoming.id)
    assert record.state == "materializing"


async def test_an_outcome_never_moves_a_question_that_was_already_replaced_or_retired(base):
    """Stale-generation fencing: the outcome of an answer to generation G can
    neither clear nor reissue a question that is no longer G."""
    repository, engine, vault, incoming, challenge = await _challenged(base)
    vault.release.set()
    newer = await engine.challenges.replace(challenge, challenge_requirement(challenge))
    record = (await repository.requests(incoming.id))[0]
    candidate = next(item for item in await repository.resolved_candidates(record.id)
                     if str(item.id) == challenge.operation_id)

    class Answer:
        token = 424242

    for accepted in (True, False):
        engine._evidence_answers[Answer.token] = (challenge, candidate)
        await engine._answered_evidence_outcome(Answer(), accepted=accepted, requirement=challenge_requirement(challenge))
        current = await engine.challenges.current(incoming.id)
        assert current.id == newer.id and current.generation == newer.generation
    assert engine._evidence_reissued == {}


def challenge_requirement(challenge):
    from transfers.models import InputRequirement
    return InputRequirement(challenge.reason, challenge.methods, challenge.facts)
