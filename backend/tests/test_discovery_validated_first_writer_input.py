"""DP 1.0.13: input validated by core-run discovery reaches the FIRST writer.

Transfer 417: an SFTP file whose discovery confirmed the server identity and
accepted the operator's login started its first writer WITHOUT that input, so
aria2 met its fail-closed sentinel host key and failed; only the executor-input
recovery then found the already-VALID context and started attempt #2.

The one Authentication Input Context owner (``EphemeralInputBroker``) now
leases already-VALID lineage/scope material -- with the identity this lineage
confirmed, as the canonical facts -- to the first writer admitted without an
exact candidate handoff. Nothing is persisted, no identity is invented, the
exact evidence handoff still wins, and every settlement is the existing one.
"""
from __future__ import annotations

from urllib.parse import unquote, urlsplit

import pytest

import db.database as database
from test_v113_transfer_auth_context import (  # noqa: F401
    CONTENT, FINGERPRINT, PASSWORD, USER, CountingVault, lab,
)
from transfers.errors import Category, Domain, NormalizedError, Stage
from transfers.input_required import (
    EphemeralInputBroker, auth_required, server_identity_required, username_password,
)
from transfers.models import (
    DiscoveredEntry, DiscoveryResult, ExecutionObservation, ExecutionState, ExecutorCapabilities, InputFactName,
    InputField, InputMethod, IntegrationDescriptor, RemoteObjectKind, TransferProgress, TransferRequest,
    TransferState,
)
from transfers.requests import auth_scope

pytestmark = pytest.mark.asyncio

OTHER_FINGERPRINT = "b" * 40


# ── the broker operation ──────────────────────────────────────────────────────

SSH = auth_scope("sftp://locked.example/solo.bin")


async def _valid(broker, *, transfer=1, root="root", scope=SSH, identity=True):
    """Material a discovery accepted, plus the identity it confirmed."""
    await broker.supply(transfer, root, scope, {InputField.USERNAME: USER, InputField.PASSWORD: PASSWORD},
                        origin="operator")
    resolution = await broker.resolve(transfer, (root,), scope, auth_required(username_password()))
    await broker.settle(resolution.submitted.token, accepted=True)
    if identity:
        # The confirmed identity lives beside the material (as ``take`` records it).
        broker._context_locked(transfer, root, scope).identity = ("sha-1", FINGERPRINT)


async def _writer(broker, *, transfer=1, chain=("child", "root"), scope=SSH,
                  methods=(InputMethod.USERNAME_PASSWORD,), candidate="candidate-1"):
    return await broker.writer_input(transfer, chain[0], candidate, chain, scope, methods)


async def test_only_already_valid_material_is_leased_to_a_writer_with_the_confirmed_identity():
    broker = EphemeralInputBroker()
    await broker.supply(1, "root", SSH, {InputField.USERNAME: USER, InputField.PASSWORD: PASSWORD}, origin="admission")
    assert await _writer(broker) is None  # UNTESTED material is never offered merely because it exists
    await _valid(broker)
    submitted = await _writer(broker)
    assert submitted.method == InputMethod.USERNAME_PASSWORD
    assert (submitted.value(InputField.USERNAME), submitted.value(InputField.PASSWORD)) == (USER, PASSWORD)
    assert {fact.name: fact.value for fact in submitted.facts} == {
        InputFactName.SERVER_HOST: "locked.example",
        InputFactName.SERVER_IDENTITY_ALGORITHM: "sha-1",
        InputFactName.SERVER_IDENTITY_FINGERPRINT: FINGERPRINT,
    }
    # The admitted writer holds it in use: its execution settles it the existing way.
    assert await broker.release_use(1, "child", "candidate-1") == submitted.token


async def test_without_a_confirmed_identity_no_identity_fact_is_invented():
    broker = EphemeralInputBroker()
    await _valid(broker, identity=False)
    assert (await _writer(broker)).facts == ()


async def test_a_writer_that_declares_another_method_gets_nothing():
    broker = EphemeralInputBroker()
    await _valid(broker)
    assert await _writer(broker, methods=(InputMethod.USERNAME_PRIVATE_KEY,)) is None
    assert await _writer(broker, methods=()) is None


@pytest.mark.parametrize("transfer,chain,address", [
    (1, ("child", "root"), "sftp://alt.example/solo.bin"),          # another host
    (1, ("child", "root"), "sftp://locked.example:2222/solo.bin"),  # the same host, another server port
    (1, ("child", "root"), "ftp://locked.example/solo.bin"),        # another transport family
    (1, ("unrelated",), "sftp://locked.example/solo.bin"),         # another lineage of the same transfer
    (2, ("child", "root"), "sftp://locked.example/solo.bin"),       # another transfer
])
async def test_valid_material_never_leaves_its_lineage_and_scope(transfer, chain, address):
    broker = EphemeralInputBroker()
    await _valid(broker)
    assert await _writer(broker, transfer=transfer, chain=chain, scope=auth_scope(address)) is None


async def test_material_rejected_by_a_writer_is_never_offered_again():
    broker = EphemeralInputBroker()
    await _valid(broker)
    submitted = await _writer(broker)
    await broker.settle(await broker.release_use(1, "child", "candidate-1"), accepted=False)
    assert submitted.token is not None
    assert await _writer(broker, candidate="candidate-2") is None


async def test_a_fresh_process_holds_nothing_to_reuse():
    assert await _writer(EphemeralInputBroker()) is None


# ── the engine: discovery validated -> first writer ───────────────────────────

class RemoteLogin(CountingVault):
    """An in-memory SFTP/FTP server pair: an SFTP host presents an identity to
    confirm, a locked host requires a login -- for discovery and execution
    alike. A start without input on a locked host fails exactly like aria2's
    fail-closed sentinel; a start whose identity fact differs from the host's
    current identity fails as a host-key mismatch."""

    descriptor = IntegrationDescriptor("remote-login", "Remote login", frozenset())
    capabilities = ExecutorCapabilities(candidate_sampling=True, per_execution_pause=True, transient_input=True,
                                        remote_discovery=True)
    claim_schemes = frozenset({"sftp", "ftp"})

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.identities = {"locked.example": FINGERPRINT, "alt.example": FINGERPRINT}
        self.plain_starts = []
        self.after_discovery = None

    @staticmethod
    def _object(candidate):
        parts = urlsplit(candidate.endpoints[0].address)
        return f"{parts.hostname}{unquote(parts.path)}"

    @staticmethod
    def _where(candidate):
        parts = urlsplit(candidate.endpoints[0].address)
        return parts.scheme, parts.hostname

    def _identity_ok(self, scheme, host, submitted):
        if scheme != "sftp":
            return True
        facts = {fact.name: fact.value for fact in submitted.facts}
        return (facts.get(InputFactName.SERVER_HOST) == host
                and facts.get(InputFactName.SERVER_IDENTITY_FINGERPRINT) == self.identities[host])

    def _requirement(self, scheme, host):
        if scheme == "sftp":
            return server_identity_required(username_password(), host=host, algorithm="sha-1",
                                            fingerprint=self.identities[host])
        return auth_required(username_password())

    async def discover(self, subject, submitted=None):
        scheme, host = self._where(subject.candidate)
        path = unquote(urlsplit(subject.candidate.endpoints[0].address).path)
        if host in self.locks:
            if submitted is None or not self._identity_ok(scheme, host, submitted):
                return self._requirement(scheme, host)
            if (submitted.value(InputField.USERNAME), submitted.value(InputField.PASSWORD)) != self.locks[host]:
                return auth_required(username_password())
        prefix = f"{host}{path.rstrip('/')}/"
        members = sorted(key[len(prefix):] for key in self.objects if key.startswith(prefix))
        result = (DiscoveryResult(tuple(DiscoveredEntry(name, len(CONTENT)) for name in members), path) if members
                  else DiscoveryResult(kind=RemoteObjectKind.FILE, expected_bytes=len(CONTENT)))
        if self.after_discovery is not None:
            self.after_discovery(self)
            self.after_discovery = None
        return result

    async def start(self, request, handle):
        if not self._unlocked:
            self.plain_starts.append(str(request.work.subject.candidate.id))
        return await super().start(request, handle)

    async def start_with_input(self, request, handle, submitted):
        scheme, host = self._where(request.work.subject.candidate)
        if not self._identity_ok(scheme, host, submitted):
            self.input_starts.append((str(request.work.subject.candidate.id), "identity_mismatch"))
            observed = ExecutionObservation(handle, ExecutionState.FAILED, TransferProgress(0, 0, 0), NormalizedError(
                Domain.EXECUTOR, Category.UNMAPPED_EXECUTOR_ERROR, Stage.EXECUTION, native_code="vault-host-key"))
            self.jobs[handle.attempt_id] = observed
            return observed
        return await super().start_with_input(request, handle, submitted)

    def input_requirement(self, candidate, observation):
        if (observation.state == ExecutionState.FAILED and observation.error is not None
                and observation.error.native_code in {"vault-auth", "vault-host-key"}):
            return self._requirement(*self._where(candidate))
        return None


def _setup(lab):
    from providers.general_ftp.provider import GeneralFtpProvider
    from providers.general_scp.provider import ScpProvider
    repository, registry, engine, _provider, objects, locks, now = lab
    registry.register_provider(GeneralFtpProvider())
    registry.register_provider(ScpProvider())
    executor = RemoteLogin(repository.authorize_execution, objects=objects, locks=locks)
    registry.register_executor(executor)
    return repository, engine, executor, locks, now


async def _drive(engine, repository, transfer_id, locks, now, *, until, answer=True, count=60):
    """Tick; answer each new question once with the challenged host's login."""
    asked = []
    for _ in range(count):
        now[0] += 5
        await engine.tick()
        current = await engine.challenges.current(transfer_id)
        if current is not None and current.id not in {item.id for item in asked}:
            asked.append(current)
            if answer:
                record = next(item for item in await repository.requests(transfer_id)
                              if item.id == current.request_id)
                user, password = locks[urlsplit(record.request.payload).hostname]
                await engine.submit_input(transfer_id, current.id, "username_password",
                                          {"username": user, "password": password})
        if await until():
            break
    return asked


async def _attempts(transfer_id):
    async with database.get_db() as db:
        return await db.fetchall("SELECT * FROM execution_attempts WHERE transfer_id=? ORDER BY created_at",
                                 (transfer_id,))


def _completed(repository, transfer_id):
    async def check():
        return (await repository.get(transfer_id)).state == TransferState.COMPLETED
    return check


async def test_transfer_417_an_exact_sftp_file_starts_its_first_writer_with_the_validated_context(lab):
    repository, engine, executor, locks, now = _setup(lab)
    transfer = await engine.submit((TransferRequest("sftp", "sftp://locked.example/solo.bin"),), deduplicate=False)
    asked = await _drive(engine, repository, transfer.id, locks, now, until=_completed(repository, transfer.id))
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert [(item.origin.value, item.reason.value) for item in asked] == [("provider", "server_identity_required")]
    assert executor.plain_starts == []  # no doomed start without the confirmed identity
    assert [user for _candidate, user in executor.input_starts] == [USER]
    assert len(await _attempts(transfer.id)) == 1


@pytest.mark.parametrize("url,provider", [("sftp://locked.example/dir/", "general_ftp"),
                                          ("scp://locked.example/dir/", "general_scp")])
async def test_a_selected_directory_child_starts_its_first_writer_with_the_validated_context(lab, url, provider):
    repository, engine, executor, locks, now = _setup(lab)
    transfer = await engine.submit((TransferRequest(url.split(":", 1)[0], url, selection_mode="interactive"),),
                                   deduplicate=False)

    async def pending():
        view = await repository.file_selection_presentation(transfer.id, now=now[0])
        return bool(view) and view.get("decision") == "pending"

    asked = await _drive(engine, repository, transfer.id, locks, now, until=pending)
    view = await repository.file_selection_presentation(transfer.id, now=now[0])
    chosen = [entry["entry_id"] for entry in view["entries"] if entry["name"] == "b.bin"]
    await repository.confirm_file_selection(transfer.id, view["manifest_id"], chosen, now=now[0])
    asked += await _drive(engine, repository, transfer.id, locks, now, until=_completed(repository, transfer.id))
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert len(asked) == 1  # one question, answered during discovery; none for the child
    assert executor.plain_starts == []
    assert [user for _candidate, user in executor.input_starts] == [USER]
    assert len(await _attempts(transfer.id)) == 1
    [child] = [record for record in await repository.requests(transfer.id) if record.parent_id]
    assert child.request.payload.endswith("/b.bin") and child.request.kind == url.split(":", 1)[0]
    assert await repository.bound_route_provider(child.id) == provider


async def test_a_locked_ftp_directory_starts_every_member_writer_with_the_validated_login(lab):
    repository, engine, executor, locks, now = _setup(lab)
    transfer = await engine.submit((TransferRequest("ftp", "ftp://locked.example/dir/"),), deduplicate=False)
    asked = await _drive(engine, repository, transfer.id, locks, now, until=_completed(repository, transfer.id))
    assert [(item.origin.value, item.reason.value) for item in asked] == [("provider", "auth_required")]
    assert executor.plain_starts == []
    assert [user for _candidate, user in executor.input_starts] == [USER] * 4
    assert len(await _attempts(transfer.id)) == 4


async def test_anonymous_ftp_manufactures_no_input(lab):
    repository, engine, executor, locks, now = _setup(lab)
    transfer = await engine.submit((TransferRequest("ftp", "ftp://open.example/dir/"),), deduplicate=False)
    asked = await _drive(engine, repository, transfer.id, locks, now, until=_completed(repository, transfer.id))
    assert asked == [] and executor.input_starts == []
    assert len(executor.plain_starts) == 4 and len(await _attempts(transfer.id)) == 4


async def test_credentials_rejected_by_the_writer_are_invalidated_and_asked_for_again(lab):
    repository, engine, executor, _locks, now = _setup(lab)

    def rotate(server):  # the server changes the password between discovery and execution
        server.locks["locked.example"] = (USER, "rotated-password-sentinel")
    executor.after_discovery = rotate
    transfer = await engine.submit((TransferRequest("sftp", "sftp://locked.example/solo.bin"),), deduplicate=False)
    # The operator answers with the server's CURRENT login.
    asked = await _drive(engine, repository, transfer.id, executor.locks, now,
                         until=_completed(repository, transfer.id))
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert executor.plain_starts == []
    # The discovery-validated login was tried once by the first writer, rejected
    # through the existing settlement, never offered again; the operator was asked.
    assert [user for _candidate, user in executor.input_starts] == ["rejected", USER]
    assert [(item.origin.value, item.reason.value) for item in asked] == [
        ("provider", "server_identity_required"), ("executor", "server_identity_required")]


async def test_an_identity_changed_after_discovery_fails_closed_without_a_prompt(lab):
    repository, engine, executor, locks, now = _setup(lab)

    def replace_host_key(server):
        server.identities["locked.example"] = OTHER_FINGERPRINT
    executor.after_discovery = replace_host_key
    transfer = await engine.submit((TransferRequest("sftp", "sftp://locked.example/solo.bin"),), deduplicate=False)

    async def failed():
        artifacts = await repository.artifacts(transfer.id)
        return bool(artifacts) and artifacts[0].error is not None

    asked = await _drive(engine, repository, transfer.id, locks, now, until=failed)
    assert len(asked) == 1  # the discovery question only: a changed identity is never asked as if unknown
    assert executor.plain_starts == []
    assert executor.input_starts == [(executor.input_starts[0][0], "identity_mismatch")]
    [artifact] = await repository.artifacts(transfer.id)
    assert artifact.error.category == Category.HOST_KEY_FAILURE


async def test_an_exact_evidence_handoff_still_wins_over_lineage_reuse(lab):
    repository, engine, executor, locks, now = _setup(lab)
    transfer = await engine.submit((TransferRequest("sftp", "sftp://locked.example/solo.bin"),), deduplicate=False)
    consulted = []
    reuse = engine.inputs.writer_input

    async def spy(*args, **kwargs):
        consulted.append(args)
        return await reuse(*args, **kwargs)

    engine.inputs.writer_input = spy
    original_take = engine.inputs.take_handoff

    async def handed(transfer_id, request_id, candidate_id, integration_id):
        # An exact handoff exists for exactly this candidate and executor.
        taken = await original_take(transfer_id, request_id, candidate_id, integration_id)
        if taken is None:
            chain = await engine._lineage(transfer_id, request_id)
            [record] = await repository.requests(transfer_id)
            taken = (await engine.inputs.resolve(transfer_id, chain, auth_scope(record.request.payload),
                                                 server_identity_required(username_password(), host="locked.example",
                                                                          algorithm="sha-1",
                                                                          fingerprint=FINGERPRINT))).submitted
        return taken

    engine.inputs.take_handoff = handed
    await _drive(engine, repository, transfer.id, locks, now, until=_completed(repository, transfer.id))
    assert consulted == []  # lineage reuse is only ever the fallback
    assert len(await _attempts(transfer.id)) == 1
