"""DP 1.0.13 adverse conditions, Pass 1: authentication and canonical access.

Defect A -- transport authentication (ACCEPTED / REJECTED / INDETERMINATE) is a
fact of its own. The moment a transport definitively accepts submitted
credentials the operator's question retires, however long the listing,
fingerprint, equivalence proof or consolidation after it takes, and whatever
else the transfer or the scheduler is doing. A timeout is never "bad
credentials".

Defect B -- a candidate whose access was proven keeps a process-local path to
exactly that accepted access through consolidation into another transfer's
canonical artifact, fenced by candidate identity and provenance, scope
(family, host, port), accepted method, confirmed server identity and lifetime.

Every ordering below is held on events, never timed.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import pytest

import db.database as database
from fake_integrations import VaultExecutor, VaultProvider
from test_discovery_validated_first_writer_input import RemoteLogin, _drive
from test_input_required_lifecycle import base  # noqa: F401  (fixture)
from test_v113_transfer_auth_context import CONTENT, FINGERPRINT, PASSWORD, USER, lab  # noqa: F401  (fixture)
from transfers.applicability import ProviderApplicability
from transfers.input_required import EphemeralInputBroker, auth_required, username_password
from transfers.models import (
    ArtifactFingerprint, Capability, Endpoint, FingerprintKind, InputFact, InputFactName, InputField, InputMethod,
    InputOrigin, IntegrationDescriptor, ResolutionResult, ResourceState, SourceIdentity, TransferCandidate,
    TransferRequest, TransferState,
)
from transfers.requests import auth_scope

pytestmark = pytest.mark.asyncio

EVIDENCE_USER, EVIDENCE_SECRET = "adverse-user-sentinel", "adverse-secret-sentinel"
CHANGED = "c" * 40


def _accept(submitted) -> None:
    """What a transport does at its definitive acceptance boundary."""
    notice = getattr(submitted, "transport_accepted", None)
    if notice is not None:
        notice()


async def _until(predicate, timeout=5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.01)
    return False


async def _facts(transfer_id):
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT kind FROM application_events WHERE transfer_id=? ORDER BY id", (transfer_id,))
    return [row["kind"] for row in rows]


def _cleared(engine, transfer_id):
    async def check():
        return await engine.challenges.current(transfer_id) is None
    return check


# ── Defect A: the evidence-origin question follows the transport's verdict ──

class TransportVault(VaultExecutor):
    """A sampling transport that authenticates first and then reads evidence.

    ``hold`` blocks the evidence read AFTER the verdict (listing, windows,
    fingerprint); ``outcome`` replaces the read with an indeterminate fact."""

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.release = asyncio.Event()
        self.release.set()
        self.holding = asyncio.Event()
        self.accepting = True
        self.outcome = None

    async def fingerprint_with_input(self, subject, submitted):
        host = self._object(subject.candidate).partition("/")[0]
        credentials = (submitted.value(InputField.USERNAME), submitted.value(InputField.PASSWORD))
        if self.accepting and credentials == self.locks.get(host):
            _accept(submitted)
        if not self.release.is_set():
            self.holding.set()
            await self.release.wait()
        if self.outcome is not None:
            outcome, self.outcome = self.outcome, None
            return outcome
        return await super().fingerprint_with_input(subject, submitted)


async def _evidence_question(base, executor=TransportVault, extra_objects=None):
    repository, registry, engine, now = base
    registry.register_provider(VaultProvider())
    vault = executor(repository.authorize_execution, objects={
        "open.example/item.bin": b"same-bytes", "locked.example/item.bin": b"same-bytes", **(extra_objects or {}),
    }, locks={"locked.example": (EVIDENCE_USER, EVIDENCE_SECRET)})
    registry.register_executor(vault)
    await engine.submit((TransferRequest("vault", "open.example/item.bin", name="item.bin"),), deduplicate=False)
    for _ in range(3):
        await engine.tick()
    incoming = await engine.submit((TransferRequest("vault", "locked.example/item.bin", name="item.bin"),),
                                   deduplicate=False)
    for _ in range(3):
        await engine.tick()
    challenge = await engine.challenges.current(incoming.id)
    assert challenge is not None and challenge.origin == InputOrigin.EVIDENCE
    return repository, engine, vault, incoming, challenge, now


async def _answer(engine, transfer_id, challenge, password=EVIDENCE_SECRET):
    await engine.submit_input(transfer_id, challenge.id, "username_password",
                              {"username": EVIDENCE_USER, "password": password})


async def test_transport_acceptance_retires_the_question_while_its_evidence_read_is_still_blocked(base):
    repository, engine, vault, incoming, challenge, _now = await _evidence_question(base)
    vault.release.clear()
    await _answer(engine, incoming.id, challenge)
    decision = asyncio.ensure_future(engine.tick())
    try:
        await asyncio.wait_for(vault.holding.wait(), timeout=10)
        # The evidence read (listing/fingerprint) is still blocked: only the
        # transport's authentication verdict exists so far.
        assert await _until(_cleared(engine, incoming.id)), "question still open after the transport accepted"
        assert "auth_accepted" in await _facts(incoming.id)
        assert (await repository.get(incoming.id)).state != TransferState.INPUT_REQUIRED
        assert not decision.done()
    finally:
        vault.release.set()
    await asyncio.wait_for(decision, timeout=10)
    for _ in range(3):
        await engine.tick()
    # The already-authorized operation kept its material to the end.
    assert (await repository.get(incoming.id)).state == TransferState.CONSOLIDATED


async def test_an_indeterminate_transport_neither_retires_nor_refuses_the_question(base):
    repository, engine, vault, incoming, challenge, _now = await _evidence_question(base)
    vault.accepting = False
    vault.release.clear()
    vault.outcome = ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "timeout")
    await _answer(engine, incoming.id, challenge)
    decision = asyncio.ensure_future(engine.tick())
    try:
        await asyncio.wait_for(vault.holding.wait(), timeout=10)
        # No verdict: nothing is faked in either direction.
        assert await engine.challenges.current(incoming.id) is not None
        facts = await _facts(incoming.id)
        assert "auth_accepted" not in facts and "auth_rejected" not in facts
    finally:
        vault.release.set()
    await asyncio.wait_for(decision, timeout=10)
    facts = await _facts(incoming.id)
    assert "auth_accepted" not in facts and "auth_rejected" not in facts


async def test_a_timeout_after_the_transport_accepted_is_never_bad_credentials(base):
    repository, engine, vault, incoming, challenge, now = await _evidence_question(base)
    vault.outcome = ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "timeout")
    await _answer(engine, incoming.id, challenge)
    await engine.tick()
    facts = await _facts(incoming.id)
    assert "auth_accepted" in facts and "auth_rejected" not in facts
    assert await engine.challenges.current(incoming.id) is None
    # The next proof opportunity reuses the accepted material: never re-asked.
    for _ in range(4):
        now[0] += 5
        await engine.tick()
    assert (await _facts(incoming.id)).count("input_required") == 1
    assert (await repository.get(incoming.id)).state == TransferState.CONSOLIDATED


class SlowProvider:
    """A provider resolution that is still running when the answer arrives."""

    applicability = ProviderApplicability()

    def __init__(self):
        self.descriptor = IntegrationDescriptor("slow-lab", "Slow lab", frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"slow"}))
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def resolve(self, request):
        self.entered.set()
        await self.release.wait()
        return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
            "slow.bin", (Endpoint("vault", "vault://open.example/slow.bin"),), provider_id=self.descriptor.id,
            source_identity=SourceIdentity("host", "open.example")),))


async def test_an_answer_submitted_while_a_cycle_runs_is_served_by_that_cycle(base):
    repository, engine, vault, incoming, challenge, _now = await _evidence_question(
        base, extra_objects={"open.example/slow.bin": b"slow"})
    slow = SlowProvider()
    base[1].register_provider(slow)
    await engine.submit((TransferRequest("slow", "x"),), deduplicate=False)
    cycle = asyncio.ensure_future(engine.resolve_pending())
    try:
        await asyncio.wait_for(slow.entered.wait(), timeout=10)

        async def entered_and_left():
            running = engine._resolution_cycle
            return running is not None and incoming.id in running.retired
        # The running cycle already looked at the questioned transfer.
        assert await _until(entered_and_left)
        await _answer(engine, incoming.id, challenge)
        assert await _until(_cleared(engine, incoming.id)), "the answer waited for the whole cycle to end"
        assert not cycle.done()
    finally:
        slow.release.set()
    await asyncio.wait_for(cycle, timeout=10)


class SiblingHoldVault(TransportVault):
    """A sibling's own evidence read (no input) blocks while armed."""

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.sibling = asyncio.Event()
        self.sibling.set()
        self.sibling_holding = asyncio.Event()

    async def fingerprint(self, subject):
        if self._object(subject.candidate).startswith("slowvault.example/") and not self.sibling.is_set():
            self.sibling_holding.set()
            await self.sibling.wait()
        return await super().fingerprint(subject)


async def test_an_answer_is_served_while_a_sibling_decision_of_the_same_transfer_is_in_flight(base):
    repository, registry, engine, _now = base
    registry.register_provider(VaultProvider())
    vault = SiblingHoldVault(repository.authorize_execution, objects={
        "open.example/item.bin": b"same-bytes", "locked.example/item.bin": b"same-bytes",
        "slowvault.example/item.bin": b"same-bytes",
    }, locks={"locked.example": (EVIDENCE_USER, EVIDENCE_SECRET)})
    registry.register_executor(vault)
    await engine.submit((TransferRequest("vault", "open.example/item.bin", name="item.bin"),), deduplicate=False)
    for _ in range(3):
        await engine.tick()
    vault.sibling.clear()
    transfer = await engine.submit((TransferRequest("vault", "locked.example/item.bin", name="item.bin"),
                                    TransferRequest("vault", "slowvault.example/item.bin", name="item.bin")),
                                   deduplicate=False)
    cycle = asyncio.ensure_future(engine.tick())
    try:
        await asyncio.wait_for(vault.sibling_holding.wait(), timeout=10)

        async def asked():
            return await engine.challenges.current(transfer.id) is not None
        assert await _until(asked)
        challenge = await engine.challenges.current(transfer.id)
        await _answer(engine, transfer.id, challenge)
        assert await _until(_cleared(engine, transfer.id)), "the answer waited behind a sibling's decision"
        assert "auth_accepted" in await _facts(transfer.id)
        assert not cycle.done()
    finally:
        vault.sibling.set()
    await asyncio.wait_for(cycle, timeout=10)


# ── Defect A: the provider-origin (discovery) question ──────────────────────

class DiscoveryHoldLogin(RemoteLogin):
    """Discovery authenticates first, then lists; the listing can be held."""

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.release = asyncio.Event()
        self.release.set()
        self.holding = asyncio.Event()
        self.discoveries = 0

    async def discover(self, subject, submitted=None):
        scheme, host = self._where(subject.candidate)
        if (submitted is not None and host in self.locks and self._identity_ok(scheme, host, submitted)
                and (submitted.value(InputField.USERNAME), submitted.value(InputField.PASSWORD)) == self.locks[host]):
            _accept(submitted)
            self.discoveries += 1
            if not self.release.is_set():
                self.holding.set()
                await self.release.wait()
        return await super().discover(subject, submitted)


async def test_discovery_acceptance_retires_the_provider_question_while_the_listing_continues(lab):
    repository, engine, login, locks, now = _ftp_lab(lab, DiscoveryHoldLogin)
    transfer = await engine.submit((TransferRequest("sftp", "sftp://locked.example/solo.bin"),), deduplicate=False)
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    assert challenge is not None and challenge.origin == InputOrigin.PROVIDER
    login.release.clear()
    await engine.submit_input(transfer.id, challenge.id, "username_password", {"username": USER, "password": PASSWORD})
    decision = asyncio.ensure_future(engine.tick())
    try:
        await asyncio.wait_for(login.holding.wait(), timeout=10)
        assert await _until(_cleared(engine, transfer.id)), "question open while the listing ran"
        assert "auth_accepted" in await _facts(transfer.id)

        async def left_input_required():
            return (await repository.get(transfer.id)).state != TransferState.INPUT_REQUIRED
        # Retiring the question and re-aggregating the transfer are two steps of
        # one acceptance. The invariant is that both happen while the listing is
        # still held -- not that a single read lands after the second.
        assert await _until(left_input_required), "the transfer still waits for input while its listing runs"
        # The request is being resolved -- never released for a second resolution.
        (record,) = await repository.requests(transfer.id)
        assert record.state == "resolving"
    finally:
        login.release.set()
    await asyncio.wait_for(decision, timeout=10)
    await _drive(engine, repository, transfer.id, locks, now, answer=False,
                 until=lambda: _state(repository, transfer.id, TransferState.COMPLETED))
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert login.discoveries == 1 and [user for _c, user in login.input_starts] == [USER]
    assert (await _facts(transfer.id)).count("input_required") == 1


async def _state(repository, transfer_id, state):
    return (await repository.get(transfer_id)).state == state


# ── Defect B: a consolidated candidate keeps its proven access ──────────────

class HeldOpenLogin(RemoteLogin):
    """The canonical writer on the open host keeps running (it is switched
    away from, never finished)."""

    async def start(self, request, handle):
        if urlsplit(request.work.subject.candidate.endpoints[0].address).hostname == "open.example":
            return await VaultExecutor.start(self, request, handle)
        return await super().start(request, handle)


def _ftp_lab(lab, executor_class):
    from providers.general_ftp.provider import GeneralFtpProvider
    repository, registry, engine, _provider, objects, locks, now = lab
    registry.register_provider(GeneralFtpProvider())
    executor = executor_class(repository.authorize_execution, objects=objects, locks=locks)
    registry.register_executor(executor)
    return repository, engine, executor, locks, now


async def _consolidated_pair(lab):
    repository, engine, executor, locks, now = _ftp_lab(lab, HeldOpenLogin)
    canonical = await engine.submit((TransferRequest("sftp", "sftp://open.example/solo.bin"),), deduplicate=False)

    async def writing():
        artifacts = await repository.artifacts(canonical.id)
        return bool(artifacts) and artifacts[0].execution is not None and artifacts[0].state == "downloading"
    await _drive(engine, repository, canonical.id, locks, now, until=writing)
    assert await writing()
    contributor = await engine.submit((TransferRequest("sftp", "sftp://locked.example/solo.bin"),), deduplicate=False)
    asked = await _drive(engine, repository, contributor.id, locks, now,
                         until=lambda: _state(repository, contributor.id, TransferState.CONSOLIDATED))
    assert (await repository.get(contributor.id)).state == TransferState.CONSOLIDATED
    assert len(asked) == 1
    (artifact,) = await repository.artifacts(canonical.id)
    index = next(i for i, item in enumerate(artifact.candidates)
                 if urlsplit(item.endpoints[0].address).hostname == "locked.example")
    return repository, engine, executor, locks, now, canonical, contributor, artifact, index


async def _switch(engine, repository, canonical, artifact, index, locks, now):
    result = await engine.activate_candidate_command(canonical.id, artifact.id, index)
    assert result is not None and result.committed, result
    candidate_id = str(artifact.candidates[index].id)

    async def settled():
        current = (await repository.artifacts(canonical.id))[0]
        return (await engine.challenges.current(canonical.id)) is not None or (
            current.execution is not None and str(current.candidates[current.selected].id) == candidate_id
            and current.state in {"downloading", "completed", "error"})
    return candidate_id, await _drive(engine, repository, canonical.id, locks, now, answer=False, until=settled,
                                      count=12)


async def test_a_consolidated_candidate_is_switched_to_with_its_proven_access_and_identity(lab):
    repository, engine, executor, locks, now, canonical, _contributor, artifact, index = await _consolidated_pair(lab)
    # The settled contributor holds nothing any more; the adopting canonical
    # owner holds exactly the adopted candidate's proven access.
    assert not await engine.inputs.holds(_contributor.id) and await engine.inputs.holds(canonical.id)
    candidate_id, asked = await _switch(engine, repository, canonical, artifact, index, locks, now)
    assert asked == [], "the canonical owner asked again for access its adopted candidate already proved"
    assert await engine.challenges.current(canonical.id) is None
    assert candidate_id not in executor.plain_starts  # never a doomed start without its access
    assert (candidate_id, USER) in executor.input_starts  # the exact accepted material, identity included
    assert "proven_access_used" in await _facts(canonical.id)


async def test_an_unrelated_candidate_on_the_same_host_never_inherits_the_proven_access(lab):
    repository, engine, executor, locks, now, *_rest = await _consolidated_pair(lab)
    lab[4]["locked.example/dir/a.bin"] = b"else"  # another object on the same host
    unrelated = await engine.submit((TransferRequest("sftp", "sftp://locked.example/dir/a.bin"),), deduplicate=False)
    asked = await _drive(engine, repository, unrelated.id, locks, now, answer=False, count=6,
                         until=lambda: _asked(engine, unrelated.id))
    assert len(asked) == 1 and asked[0].origin == InputOrigin.PROVIDER
    assert not [item for item in executor.input_starts if item[1] == USER and item[0] not in _candidates(asked)]


async def _asked(engine, transfer_id):
    return await engine.challenges.current(transfer_id) is not None


def _candidates(asked):
    return {item.operation_id for item in asked}


async def test_a_changed_server_identity_fails_closed_after_consolidation(lab):
    repository, engine, executor, locks, now, canonical, _contributor, artifact, index = await _consolidated_pair(lab)
    executor.identities["locked.example"] = CHANGED
    candidate_id, asked = await _switch(engine, repository, canonical, artifact, index, locks, now)
    # Never a credentialed success against the changed identity, never an
    # automatic confirmation of it.
    assert (candidate_id, USER) not in executor.input_starts
    for question in asked:
        facts = {fact.name: fact.value for fact in question.facts}
        assert facts.get(InputFactName.SERVER_IDENTITY_FINGERPRINT, CHANGED) == CHANGED


# ── Defect B: the broker's proven-access fences ─────────────────────────────

SSH = auth_scope("rsync+ssh://locked.example/solo.bin")
IDENTITY = (InputFact(InputFactName.SERVER_HOST, "locked.example"),
            InputFact(InputFactName.SERVER_IDENTITY_ALGORITHM, "sha-1"),
            InputFact(InputFactName.SERVER_IDENTITY_FINGERPRINT, FINGERPRINT))
METHODS = (InputMethod.USERNAME_PASSWORD,)


async def _proven(broker, *, candidate="cand-B", transfer=2, request="req-B"):
    """Transfer 2's request proved candidate B over its confirmed identity."""
    from transfers.input_required import AccessProof
    await broker.supply(transfer, request, SSH, {InputField.USERNAME: USER, InputField.PASSWORD: PASSWORD},
                        origin="operator")
    broker._context_locked(transfer, request, SSH).identity = ("sha-1", FINGERPRINT)
    resolution = await broker.resolve(transfer, (request,), SSH, auth_required(username_password()))
    await broker.settle(resolution.submitted.token, accepted=True, proof=AccessProof(candidate, request))


async def _adopted(broker, *, candidate="cand-B", scope=SSH, origin=(2, "req-B"), methods=METHODS, transfer=1):
    return await broker.adopted_input(transfer, "req-A", candidate, scope, methods, origin=origin)


async def test_the_proven_access_of_an_adopted_candidate_is_its_exact_material_and_identity():
    broker = EphemeralInputBroker()
    await _proven(broker)
    submitted = await _adopted(broker)
    assert (submitted.value(InputField.USERNAME), submitted.value(InputField.PASSWORD)) == (USER, PASSWORD)
    assert tuple(submitted.facts) == IDENTITY
    # Held in use by the adopting writer: its execution settles it the one way.
    assert await broker.release_use(1, "req-A", "cand-B") == submitted.token


@pytest.mark.parametrize("change", [
    {"scope": auth_scope("rsync+ssh://locked.example:2222/solo.bin")},   # another port / service
    {"scope": auth_scope("rsync+ssh://alt.example/solo.bin")},           # another host
    {"scope": auth_scope("ftp://locked.example/solo.bin")},              # another auth family
    {"scope": auth_scope("rsync://locked.example/solo.bin")},            # another auth family (daemon)
    {"candidate": "cand-C"},                                             # no provenance relation
    {"origin": (3, "req-B")},                                            # another contributing transfer
    {"origin": (2, "req-other")},                                        # another contributing request
    {"origin": None},                                                    # no provenance at all
    {"methods": (InputMethod.USERNAME_PRIVATE_KEY,)},                    # another accepted method
])
async def test_proven_access_never_leaves_its_candidate_provenance_scope_or_method(change):
    broker = EphemeralInputBroker()
    await _proven(broker)
    assert await _adopted(broker, **change) is None


async def test_rejected_proven_access_is_destroyed_and_never_offered_again():
    broker = EphemeralInputBroker()
    await _proven(broker)
    first = await _adopted(broker)
    await broker.settle(await broker.release_use(1, "req-A", "cand-B"), accepted=False)
    assert first.token is not None and await _adopted(broker) is None


async def test_proven_access_lives_exactly_as_long_as_a_transfer_that_holds_the_candidate():
    broker = EphemeralInputBroker()
    await _proven(broker)
    await broker.discard_transfer(2, adopted={"cand-B": 1})  # consolidated into transfer 1
    assert await _adopted(broker) is not None
    await broker.release_use(1, "req-A", "cand-B")
    await broker.discard_transfer(1)  # the adopting canonical transfer ends
    assert await _adopted(broker) is None

    broker = EphemeralInputBroker()
    await _proven(broker)
    await broker.discard_transfer(2)  # ended without any adoption
    assert await _adopted(broker) is None


# ── Defect A at the real transports: the verdict is reported at the handshake ─

@pytest.mark.real_runtime
async def test_rsync_over_ssh_reports_acceptance_before_the_remote_command_runs(tmp_path, monkeypatch):
    import asyncssh
    import executors.rsync.executor as rsync_module

    async def validated(uri, **_kwargs):
        return uri
    monkeypatch.setattr(rsync_module, "validate_resolved_public_destination", validated)
    from rsync_origins import RsyncSshOrigin, write_tree
    from test_v113_rsync_executor import _candidate, _executor, _identity, _password
    from test_v113_transport_evidence_sampling import guard_for
    from transfers.models import ExecutionSubject

    origin = await RsyncSshOrigin(tmp_path / "ssh", credentials=(USER, PASSWORD),
                                  authorized_key=asyncssh.generate_private_key("ssh-ed25519"), key_user=USER).start()
    write_tree(origin.root / "files", {"payload.bin": b"payload-bytes"})
    guard = guard_for()
    try:
        executor = _executor(tmp_path, guard)
        subject = ExecutionSubject.of(_candidate(origin.url(f"{origin.root}/files/payload.bin")))
        confirmed = _identity("rsync-ssh-origin.test", origin.fingerprint())
        origin.exec_gate = asyncio.Event()
        submitted = _password(USER, PASSWORD, facts=confirmed)
        listing = asyncio.ensure_future(executor.discover(subject, submitted))

        async def accepted():
            return bool(getattr(submitted, "accepted_by_transport", False))
        try:
            assert await _until(accepted, timeout=15), "acceptance was only known once the listing finished"
            assert not listing.done()
        finally:
            origin.exec_gate.set()
        found = await asyncio.wait_for(listing, timeout=30)
        assert found.expected_bytes == len(b"payload-bytes")

        origin.exec_gate = None
        refused = _password(USER, "wrong-sentinel", facts=confirmed)
        answer = await executor.discover(subject, refused)
        assert answer.reason.value == "auth_required"
        assert not getattr(refused, "accepted_by_transport", False)
    finally:
        await origin.close()
        await guard.stop()


async def test_sftp_evidence_reports_acceptance_before_the_evidence_is_read(tmp_path, monkeypatch):
    from test_v113_transport_evidence_sampling import SftpOrigin, candidate, executor_for, guard_for
    from transfers.models import ExecutionSubject
    import executors.aria2.executor as executor_module
    import services.network_safety as safety

    async def validated(uri, **_kwargs):
        return uri

    async def local_resolve(self, host, port=0, family=0):
        import socket
        return [{"hostname": host, "host": "127.0.0.1", "port": port, "family": socket.AF_INET,
                 "proto": 0, "flags": socket.AI_NUMERICHOST}]

    monkeypatch.setattr(safety, "validate_resolved_public_destination", validated)
    monkeypatch.setattr(safety.PublicDestinationResolver, "resolve", local_resolve)
    monkeypatch.setattr(executor_module, "validate_resolved_public_destination", validated)
    root = tmp_path / "sftp-root"
    (root / "data").mkdir(parents=True)
    (root / "data" / "object.bin").write_bytes(b"x" * 200_000)
    from test_v113_transport_evidence_sampling import PASSWORD as SFTP_PASSWORD, USER as SFTP_USER, submitted
    origin = await SftpOrigin(root, credentials=(SFTP_USER, SFTP_PASSWORD)).start()
    guard = guard_for()
    try:
        executor = executor_for(tmp_path, guard)
        subject = ExecutionSubject.of(candidate(origin.url("/data/object.bin")))
        identity = await executor.fingerprint(subject)
        origin.stat_gate = asyncio.Event()
        answer = submitted(identity)
        sampling = asyncio.ensure_future(executor.fingerprint_with_input(subject, answer))

        async def accepted():
            return bool(getattr(answer, "accepted_by_transport", False))
        try:
            assert await _until(accepted, timeout=15), "acceptance was only known once the evidence was read"
            assert not sampling.done()
        finally:
            origin.stat_gate.set()
        sample = await asyncio.wait_for(sampling, timeout=30)
        assert sample.kind == FingerprintKind.FULL_CONTENT_SAMPLE

        origin.stat_gate = None
        wrong = submitted(identity, password="wrong-sentinel")
        refused = await executor.fingerprint_with_input(subject, wrong)
        assert refused.reason.value == "server_identity_required" or refused.reason.value == "auth_required"
        assert not getattr(wrong, "accepted_by_transport", False)
    finally:
        await origin.close()
        await guard.stop()
