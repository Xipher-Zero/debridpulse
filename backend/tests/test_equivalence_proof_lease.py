"""DP 1.0.13: a proof-only lease of already-validated credentials for equivalence.

Equivalence proof is pairwise: an incoming candidate is compared with each
canonical member. When the canonical member needs authentication and carries no
retained neutral evidence, the deciding request -- rightly -- may not borrow
another lineage's secret, so the pair stayed unresolved and the contributor
ended ``unverified`` (the six-route SFTP/SSH/SCP runtime case).

The Authentication Input Context owner now lends already-VALID material of the
EXACT authenticated scope (family, host, port; for an SSH server also the
identity that context confirmed) to ONE candidate-sampling operation of the
equivalence owner, and nothing else: the borrowing lineage never holds it, no
writer handoff is made from it, only a definitive credential refusal
invalidates it, and the neutral fingerprint -- never the secret -- is retained.
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit

import pytest

import db.database as database
from test_discovery_validated_first_writer_input import RemoteLogin
from test_v113_transfer_auth_context import CONTENT, FINGERPRINT, PASSWORD, USER, lab  # noqa: F401
from transfers.input_required import (
    EphemeralInputBroker, auth_required, server_identity_required, username_password,
)
from transfers.mirrors import EvidenceContext
from transfers.models import (
    ArtifactFingerprint, Endpoint, FingerprintKind, InputFactName, InputField, InputMethod, TransferCandidate,
    TransferRequest,
)
from transfers.requests import auth_scope

pytestmark = pytest.mark.asyncio

OTHER_FINGERPRINT = "b" * 40
SSH = auth_scope("sftp://locked.example/solo.bin")


def _identity_requirement(fingerprint=FINGERPRINT, host="locked.example"):
    return server_identity_required(username_password(), host=host, algorithm="sha-1", fingerprint=fingerprint)


async def _valid(broker, *, transfer=1, root="owner-root", scope=SSH, identity=FINGERPRINT):
    """Material a lineage validated, and the identity it confirmed."""
    await broker.supply(transfer, root, scope, {InputField.USERNAME: USER, InputField.PASSWORD: PASSWORD},
                        origin="operator")
    resolution = await broker.resolve(transfer, (root,), scope, auth_required(username_password()))
    await broker.settle(resolution.submitted.token, accepted=True)
    if identity is not None:
        broker._context_locked(transfer, root, scope).identity = ("sha-1", identity)
    return resolution.submitted


async def _lease(broker, scope=SSH, requirement=None, methods=(InputMethod.USERNAME_PASSWORD,)):
    return await broker.lease_for_proof(scope, methods, requirement or _identity_requirement())


# ── the broker's proof-lease authorization ───────────────────────────────────

async def test_valid_material_of_the_exact_scope_and_confirmed_identity_is_lent_for_one_proof():
    broker = EphemeralInputBroker()
    await _valid(broker)
    lease = await _lease(broker)
    assert (lease.value(InputField.USERNAME), lease.value(InputField.PASSWORD)) == (USER, PASSWORD)
    assert {fact.name: fact.value for fact in lease.facts}[InputFactName.SERVER_IDENTITY_FINGERPRINT] == FINGERPRINT
    await broker.end_proof_lease(lease, rejected=False)
    # Lent, never given: the lease entered no lineage, handoff or use record.
    assert not await broker.holds(2)
    assert await broker.release_use(2, "borrower", "candidate") is None


@pytest.mark.parametrize("scope,requirement", [
    (auth_scope("sftp://locked.example:2222/solo.bin"), None),                      # another port
    (auth_scope("ftp://locked.example/solo.bin"), auth_required(username_password())),  # another family
    (auth_scope("sftp://alt.example/solo.bin"), _identity_requirement(host="alt.example")),  # another host
    (SSH, _identity_requirement(OTHER_FINGERPRINT)),                                # an unconfirmed identity
])
async def test_nothing_is_lent_outside_the_exact_authenticated_scope_and_identity(scope, requirement):
    broker = EphemeralInputBroker()
    await _valid(broker)
    assert await _lease(broker, scope, requirement) is None


async def test_an_ssh_scope_without_a_confirmed_identity_lends_nothing():
    broker = EphemeralInputBroker()
    await _valid(broker, identity=None)
    assert await _lease(broker) is None


async def test_untested_and_rejected_material_is_never_lent():
    broker = EphemeralInputBroker()
    await broker.supply(1, "owner-root", SSH, {InputField.USERNAME: USER, InputField.PASSWORD: PASSWORD},
                        origin="admission")
    broker._context_locked(1, "owner-root", SSH).identity = ("sha-1", FINGERPRINT)
    assert await _lease(broker) is None  # UNTESTED
    broker2 = EphemeralInputBroker()
    await _valid(broker2)
    await broker2.end_proof_lease(await _lease(broker2), rejected=True)
    assert await _lease(broker2) is None  # REJECTED by the proof's definitive refusal


async def test_a_non_rejecting_outcome_keeps_the_material_valid():
    broker = EphemeralInputBroker()
    await _valid(broker)
    await broker.end_proof_lease(await _lease(broker), rejected=False)
    assert await _lease(broker) is not None


async def test_a_declared_method_mismatch_lends_nothing():
    broker = EphemeralInputBroker()
    await _valid(broker)
    assert await _lease(broker, methods=(InputMethod.USERNAME_PRIVATE_KEY,)) is None


async def test_a_fresh_process_has_nothing_to_lend():
    assert await _lease(EphemeralInputBroker()) is None


# ── the evidence owner's use of a lease ──────────────────────────────────────

class _Auth:
    """The deciding request's authentication-input binding (``_EvidenceAuth``
    shape): it owns only ``own`` and lends proofs from ``broker``."""

    def __init__(self, broker, own=()):
        self.broker, self.own, self.ended = broker, set(own), []

    def owns(self, candidate):
        return str(candidate.id) in self.own

    async def resolve(self, candidate, requirement):
        raise AssertionError("a peer's candidate never uses the deciding lineage")

    async def settle(self, submitted, *, accepted):
        raise AssertionError("a proof lease is never settled as the deciding lineage's input")

    async def proof_lease(self, candidate, requirement):
        return await self.broker.lease_for_proof(auth_scope(candidate.endpoints[0].address),
                                                 candidate.accepted_input_methods, requirement)

    async def end_proof_lease(self, submitted, *, rejected):
        self.ended.append(rejected)
        return await self.broker.end_proof_lease(submitted, rejected=rejected)


class _Sampler:
    """One peer candidate's sampler: needs input, then answers ``outcome``."""

    class capabilities:
        transient_input = True

    class descriptor:
        id = "sampler"

    def __init__(self, outcome):
        self.outcome, self.inputs = outcome, []

    async def fingerprint(self, subject):
        return _identity_requirement()

    async def fingerprint_with_input(self, subject, submitted):
        self.inputs.append(submitted.value(InputField.USERNAME))
        return self.outcome


def _peer():
    return TransferCandidate("solo.bin", (Endpoint("sftp", "sftp://locked.example/solo.bin"),), expected_bytes=4,
                             accepted_input_methods=(InputMethod.USERNAME_PASSWORD,))


@pytest.mark.parametrize("outcome,rejected", [
    (ArtifactFingerprint(4, "digest"), False),                                           # proven
    (ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "timeout"), False),         # transport, not auth
    (ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "destination_rejected"), False),  # identity fails closed
    (ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "range_unsupported"), False),
    (_identity_requirement(), True),                                                     # the credential was refused
])
async def test_only_a_definitive_refusal_invalidates_the_lent_material(outcome, rejected):
    broker = EphemeralInputBroker()
    await _valid(broker)
    auth, sampler, peer = _Auth(broker), _Sampler(outcome), _peer()
    context = EvidenceContext()
    context.bind(auth)
    sample = await context.fingerprint(sampler, peer)
    assert sampler.inputs == [USER] and auth.ended == [rejected]
    assert (await _lease(broker) is None) == rejected
    # The lease never becomes this decision's supplied input (no writer handoff).
    assert context.take_supplied() == []
    if isinstance(outcome, ArtifactFingerprint) and outcome.kind != FingerprintKind.UNAVAILABLE:
        assert sample == outcome and context.take_borrowed() == [(str(peer.id), outcome)]
    else:
        assert context.take_borrowed() == []


async def test_retained_neutral_evidence_is_used_before_any_lease():
    broker = EphemeralInputBroker()
    await _valid(broker)
    retained = ArtifactFingerprint(4, "digest")
    auth, sampler = _Auth(broker), _Sampler(ArtifactFingerprint(4, "other"))
    peer = _peer()
    from dataclasses import replace
    peer = replace(peer, content_evidence=retained)
    context = EvidenceContext()
    context.bind(auth)
    assert await context.fingerprint(sampler, peer) == retained
    assert sampler.inputs == [] and auth.ended == []


# ── the engine: authenticated canonical members proven for other transfers ───

class LiveRemote(RemoteLogin):
    """``RemoteLogin`` whose writers keep running until ``release`` (so a
    canonical artifact stays live while contributors prove themselves) and
    whose samples follow the real SFTP/FTP evidence rules: without input a
    locked host only answers its requirement; with input a changed identity
    fails closed and a refused login is a requirement again."""

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.holding, self.held, self.proofs = True, [], []

    def finish(self, handle, **kwargs):
        if self.holding:
            self.held.append(handle)
            return
        super().finish(handle, **kwargs)

    def release(self):
        self.holding = False
        for handle in self.held:
            super().finish(handle)
        self.held = []

    def _body(self, candidate):
        return self.objects[self._object(candidate)]

    def _evidence_of(self, candidate):
        body = self._body(candidate)
        return ArtifactFingerprint(len(body), hashlib.sha256(body).hexdigest())

    async def fingerprint(self, subject):
        scheme, host = self._where(subject.candidate)
        if host in self.locks:
            return self._requirement(scheme, host)
        return self._evidence_of(subject.candidate)

    async def fingerprint_with_input(self, subject, submitted):
        scheme, host = self._where(subject.candidate)
        self.proofs.append((self._object(subject.candidate), submitted.value(InputField.USERNAME)))
        if not self._identity_ok(scheme, host, submitted):
            return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, "destination_rejected")
        if (submitted.value(InputField.USERNAME), submitted.value(InputField.PASSWORD)) != self.locks.get(host):
            return self._requirement(scheme, host)
        return self._evidence_of(subject.candidate)


def _setup(lab):
    from providers.general_ftp.provider import GeneralFtpProvider
    from providers.general_http.provider import GeneralHttpProvider
    from providers.general_scp.provider import ScpProvider
    repository, registry, engine, _provider, objects, locks, now = lab
    for provider in (GeneralFtpProvider(), ScpProvider(), GeneralHttpProvider()):
        registry.register_provider(provider)
    executor = LiveRemote(repository.authorize_execution, objects=objects, locks=locks)
    registry.register_executor(executor)
    return repository, engine, executor, now


async def _drive(engine, repository, transfer_ids, executor, now, *, select=None, count=40):
    """Tick; answer every new question of these transfers once with the
    host's current login; confirm the one-file selection when offered."""
    answered = set()
    for _ in range(count):
        now[0] += 5
        await engine.tick()
        for transfer_id in transfer_ids:
            current = await engine.challenges.current(transfer_id)
            if current is not None and current.id not in answered:
                answered.add(current.id)
                record = next(item for item in await repository.requests(transfer_id) if item.id == current.request_id)
                user, password = executor.locks[urlsplit(record.request.payload).hostname]
                await engine.submit_input(transfer_id, current.id, "username_password",
                                          {"username": user, "password": password})
            if select is not None:
                view = await repository.file_selection_presentation(transfer_id, now=now[0])
                if view and view.get("decision") == "pending":
                    chosen = [entry["entry_id"] for entry in view["entries"] if entry["name"] == select]
                    await repository.confirm_file_selection(transfer_id, view["manifest_id"], chosen, now=now[0])
    return answered


async def _requests(transfer_ids):
    async with database.get_db() as db:
        marks = ",".join("?" for _ in transfer_ids)
        return await db.fetchall(
            f"SELECT * FROM transfer_requests WHERE transfer_id IN ({marks}) ORDER BY transfer_id",  # nosec B608
            tuple(transfer_ids))


async def _material(transfer_ids):
    async with database.get_db() as db:
        marks = ",".join("?" for _ in transfer_ids)
        return await db.fetchall(
            f"SELECT * FROM download_files WHERE torrent_id IN ({marks}) "  # nosec B608
            "AND COALESCE(mirror_state,'')!='standby'", tuple(transfer_ids))


async def _origin_requests(engine, artifact_id):
    return {str(origin["request_id"]) for binding in await engine.canonical.bindings(artifact_id)
            for origin in binding["origins"]}


async def test_an_authenticated_canonical_member_is_proven_for_another_transfer_by_a_proof_lease(lab):
    repository, engine, executor, now = _setup(lab)
    owner = await engine.submit((TransferRequest("sftp", "sftp://locked.example/solo.bin"),), deduplicate=False)
    await _drive(engine, repository, [owner.id], executor, now, count=8)
    [canonical] = await _material([owner.id])
    later = await engine.submit((TransferRequest("ssh", "ssh://locked.example/solo.bin"),), deduplicate=False)
    await _drive(engine, repository, [owner.id, later.id], executor, now, count=20)
    [record] = [row for row in await _requests([later.id])]
    # Verified membership, proven from material -- not an unverified association.
    assert (record["equivalence_disposition"], record["equivalence_reason"]) == ("recovered", "full_content_sample")
    assert record["id"] in await _origin_requests(engine, canonical["id"])
    assert len([call for call in executor.calls if call[0] == "start"]) == 1  # one physical writer
    assert len(await _material([owner.id, later.id])) == 1
    # The owner's canonical member was sampled with the owner's lent login.
    assert ("locked.example/solo.bin", USER) in executor.proofs


async def test_a_proof_lease_leaves_the_borrowing_transfer_without_material_or_handoff(lab):
    repository, engine, executor, now = _setup(lab)
    executor.objects["open.example/solo.bin"] = CONTENT
    owner = await engine.submit((TransferRequest("sftp", "sftp://locked.example/solo.bin"),), deduplicate=False)
    await _drive(engine, repository, [owner.id], executor, now, count=8)
    [canonical] = await _material([owner.id])
    # An open mirror on another server: it needs no input of its own.
    later = await engine.submit((TransferRequest("ftp", "ftp://open.example/solo.bin"),), deduplicate=False)
    await _drive(engine, repository, [owner.id, later.id], executor, now, count=20)
    [record] = await _requests([later.id])
    assert record["equivalence_disposition"] == "recovered"
    assert record["id"] in await _origin_requests(engine, canonical["id"])
    assert not await engine.inputs.holds(later.id)  # never received the owner's secret
    assert await engine.inputs.take_handoff(later.id, record["id"], "any", "remote-login") is None
    # The owner's own lineage still holds its (still valid) material.
    owner_record = (await repository.requests(owner.id))[0]
    assert await engine.inputs.valid_for(owner.id, (owner_record.id,), auth_scope(owner_record.request.payload))


async def test_same_credentials_never_imply_equivalence(lab):
    repository, engine, executor, now = _setup(lab)
    executor.objects["open.example/solo.bin"] = b"diff"  # the same name, other bytes
    owner = await engine.submit((TransferRequest("sftp", "sftp://locked.example/solo.bin"),), deduplicate=False)
    await _drive(engine, repository, [owner.id], executor, now, count=8)
    later = await engine.submit((TransferRequest("ftp", "ftp://open.example/solo.bin"),), deduplicate=False)
    await _drive(engine, repository, [owner.id, later.id], executor, now, count=20)
    [record] = await _requests([later.id])
    assert ("locked.example/solo.bin", USER) in executor.proofs  # the lease was used...
    assert record["equivalence_disposition"] != "recovered"      # ...and proved them different
    assert len(await _material([owner.id, later.id])) == 2


SIX = ["sftp://locked.example/dir/b.bin", "sftp://locked.example/dir/", "ssh://locked.example/dir/b.bin",
       "ssh://locked.example/dir/", "scp://locked.example/dir/b.bin", "scp://locked.example/dir/"]


async def test_six_routes_to_one_authenticated_file_are_verified_members_of_one_canonical_artifact(lab):
    repository, engine, executor, now = _setup(lab)
    transfers = []
    for url in SIX:
        interactive = url.endswith("/")
        transfers.append(await engine.submit((TransferRequest(
            url.split(":", 1)[0], url, selection_mode="interactive" if interactive else "all"),), deduplicate=False))
        await _drive(engine, repository, [item.id for item in transfers], executor, now, select="b.bin", count=12)
    ids = [item.id for item in transfers]
    [canonical] = await _material(ids)
    assert len([call for call in executor.calls if call[0] == "start"]) == 1  # one physical writer
    # Every route to the file: three exact roots and three selected directory children.
    leaves = [row for row in await _requests(ids)
              if json.loads(row["payload"])["payload"].endswith("/dir/b.bin")]
    assert len(leaves) == 6
    contributors = [row for row in leaves if row["transfer_id"] != canonical["torrent_id"]]
    assert len(contributors) == 5
    assert {row["equivalence_disposition"] for row in contributors} == {"recovered"}  # none unverified
    assert {row["id"] for row in leaves} <= await _origin_requests(engine, canonical["id"])
    # Both provider routes (general_ftp, general_scp) are retained on the canonical artifact for failover.
    assert {binding["provider_id"] for binding in await engine.canonical.bindings(canonical["id"])} == {
        "general_ftp", "general_scp"}


async def test_simultaneously_admitted_authenticated_routes_never_seed_two_writers(lab):
    """Two separately admitted exact routes to one authenticated file resolve in
    the same cycle, before either lineage holds validated input, so neither
    can yet be proven against the other. The later one must not seed a second
    writer beside a still-deciding lower contender; it re-decides against the
    contender's canonical artifact through the ordinary cohort semantics."""
    repository, engine, executor, now = _setup(lab)
    first = await engine.submit((TransferRequest("ssh", "ssh://locked.example/solo.bin"),), deduplicate=False)
    second = await engine.submit((TransferRequest("scp", "scp://locked.example/solo.bin"),), deduplicate=False)
    await _drive(engine, repository, [first.id, second.id], executor, now, count=20)
    [canonical] = await _material([first.id, second.id])
    assert canonical["torrent_id"] == first.id
    assert len([call for call in executor.calls if call[0] == "start"]) == 1  # one physical writer
    [later] = await _requests([second.id])
    # Associated with the one canonical artifact: verified when the owner's
    # validated login could lend its proof in time, otherwise held unverified
    # beside it -- never an independent second writer.
    assert later["equivalence_disposition"] in {"recovered", "unverified"}
    if later["equivalence_disposition"] == "unverified":
        assert later["equivalence_target_artifact_id"] == canonical["id"]
    else:
        assert later["id"] in await _origin_requests(engine, canonical["id"])
