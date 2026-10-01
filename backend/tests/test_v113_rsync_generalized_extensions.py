"""DP 1.0.13 rsync integration: the neutral extensions it needed, proven neutrally.

rsync required four generalized DebridPulse surfaces; each is proven here
without rsync, so another executor can rely on the same contract:

1. recursive remote discovery (``DiscoveryRequest.depth`` UNLIMITED /
   ``DiscoveredEntry.relative_path``) -- through the REAL engine and the real
   rsync provider, with an in-memory tree-listing executor;
2. remote source capacity (``policy.remote_source_capacity``) -- a source
   server's own concurrency refusal never spends a budget, a provider account's
   limit keeps its ordinary one;
3. attempt-owned native process groups (``executors.process_ownership``) --
   with a plain process, not rsync;
4. and the core stays neutral: no universal ``transfers`` module names rsync.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest
import pytest_asyncio

import db.database as database
from executors.process_ownership import ProcessGroupAlive, ProcessOwnership
from fake_integrations import VaultExecutor
from providers.general_rsync.provider import GeneralRsyncProvider
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.mirrors import REMOTE_CAPACITY_REASON
from transfers.input_required import auth_required, username_password, username_private_key
from transfers.models import (
    DiscoveredEntry, DiscoveryDepth, DiscoveryResult, ExecutionSubject, ExecutorCapabilities, InputField, IntegrationDescriptor,
    RemoteObjectKind, TransferRequest, TransferState,
)
from transfers.policy import (
    RecoveryAction, RecoveryContext, TransferPolicy, interpretation_absent, remote_source_capacity,
)
from transfers.requests import auth_scope
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

TRANSFERS = Path(__file__).resolve().parents[1] / "transfers"


# ── 1. recursive discovery through the real engine ───────────────────────────

class TreeListing(VaultExecutor):
    """In-memory executor claiming rsync endpoints that can list a tree."""

    descriptor = IntegrationDescriptor("tree-memory", "Tree memory", frozenset())
    capabilities = ExecutorCapabilities(candidate_sampling=True, per_execution_pause=True, transient_input=True,
                                        remote_discovery=True)
    claim_schemes = frozenset({"rsync"})

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.discoveries: list[tuple[str, bool]] = []

    @staticmethod
    def _object(candidate):
        return unquote(candidate.endpoints[0].address.removeprefix("rsync://"))

    async def discover(self, subject, submitted=None, *, depth=DiscoveryDepth.CURRENT):
        recursive = depth.unlimited
        key = self._object(subject.candidate)
        self.discoveries.append((key, recursive))
        if key in self.objects:
            return DiscoveryResult(kind=RemoteObjectKind.FILE, expected_bytes=len(self.objects[key]))
        prefix = key.rstrip("/") + "/"
        members = [(name[len(prefix):], len(data)) for name, data in self.objects.items() if name.startswith(prefix)]
        if not recursive:
            members = [(path, size) for path, size in members if "/" not in path]
        return DiscoveryResult(tuple(DiscoveredEntry(path.rsplit("/", 1)[-1], size, relative_path=path)
                                     for path, size in members))

    async def start(self, request, handle):
        observed = await super().start(request, handle)
        if observed.error is None:
            self.finish(handle)  # a started in-memory copy finishes at once
        return observed


@pytest_asyncio.fixture
async def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "extensions.sqlite3")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "downloads"),
                            policy=TransferPolicy(retry_delay=0, adoption_stability_seconds=0, max_active_executions=5))
    await engine.initialize()
    registry.register_provider(GeneralRsyncProvider())
    return engine, repository, registry, tmp_path


@pytest.mark.asyncio
async def test_a_recursive_tree_becomes_nested_dp_owned_members_through_the_real_engine(engine):
    engine, repository, registry, tmp_path = engine
    # The in-memory copy materializes exactly four bytes per member.
    objects = {"h.example/pub/Album/cover.jpg": b"done", "h.example/pub/Album/Disc 1/01.flac": b"done",
               "h.example/pub/Album/Disc 1/deep/02.flac": b"done"}
    executor = TreeListing(repository.authorize_execution, objects=objects)
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("rsync", "rsync://h.example/pub/Album"),), deduplicate=False)
    for _ in range(80):
        await engine.tick()
        if (await repository.get(transfer.id)).state in {TransferState.COMPLETED, TransferState.FAILED}:
            break
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert ("h.example/pub/Album", True) in executor.discoveries
    targets = sorted(Path(artifact.target).relative_to(tmp_path / "downloads").as_posix()
                     for artifact in await repository.artifacts(transfer.id))
    assert targets == ["Album/Disc 1/01.flac", "Album/Disc 1/deep/02.flac", "Album/cover.jpg"]
    rows = await repository.requests(transfer.id)
    assert {row.request.payload for row in rows if row.parent_id} == {
        "rsync://h.example/pub/Album/cover.jpg", "rsync://h.example/pub/Album/Disc%201/01.flac",
        "rsync://h.example/pub/Album/Disc%201/deep/02.flac"}


class CapacityRefusingTree(TreeListing):
    """Same tree, but the source server refuses the first proof connections
    for capacity -- more often than the bounded proof budget would allow."""

    descriptor = IntegrationDescriptor("tree-capacity-memory", "Tree capacity memory", frozenset())

    def __init__(self, authorize, *, refusals, **kwargs):
        super().__init__(authorize, **kwargs)
        self.refusals = refusals

    async def fingerprint(self, subject):
        if self.refusals > 0:
            self.refusals -= 1
            from transfers.models import ArtifactFingerprint, FingerprintKind
            return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, REMOTE_CAPACITY_REASON)
        return await super().fingerprint(subject)


@pytest.mark.asyncio
async def test_a_proof_the_source_refused_for_capacity_spends_no_proof_budget(engine):
    from transfers.cohorts import _PROOF_RETRY_BUDGET
    engine, repository, registry, tmp_path = engine
    # Same-size siblings need a duplicate proof; distinct bytes prove them distinct.
    objects = {"h.example/pub/set/a.part": b"aaaa", "h.example/pub/set/b.part": b"bbbb"}
    refusals = 4 * (_PROOF_RETRY_BUDGET + 1)
    executor = CapacityRefusingTree(repository.authorize_execution, refusals=refusals, objects=objects)
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("rsync", "rsync://h.example/pub/set/"),), deduplicate=False)
    for _ in range(400):
        await engine.tick()
        if (await repository.get(transfer.id)).state in {TransferState.COMPLETED, TransferState.FAILED}:
            break
        await asyncio.sleep(0.01)
    assert executor.refusals == 0, "the scenario never exhausted its capacity refusals"
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    async with database.get_db() as db:
        held = await db.fetchall("SELECT equivalence_disposition FROM transfer_requests WHERE transfer_id=? "
                                 "AND equivalence_disposition IN ('exhausted','unverified')", (transfer.id,))
    assert held == []


class OneSlotSource(TreeListing):
    """A source server admitting ONE connection at a time: a second evidence
    read that overlaps the first is refused for capacity, exactly as an rsync
    daemon with ``max connections = 1`` refuses it."""

    descriptor = IntegrationDescriptor("one-slot-memory", "One slot memory", frozenset())

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.in_flight = 0
        self.refused = 0

    async def fingerprint(self, subject):
        from transfers.models import ArtifactFingerprint, FingerprintKind
        if self.in_flight:
            self.refused += 1
            return ArtifactFingerprint(0, "", FingerprintKind.UNAVAILABLE, REMOTE_CAPACITY_REASON)
        self.in_flight += 1
        try:
            await asyncio.sleep(0.02)  # the read holds the one slot for a while
            return await super().fingerprint(subject)
        finally:
            self.in_flight -= 1


@pytest.mark.asyncio
async def test_two_routes_of_one_file_on_a_one_connection_source_converge_instead_of_waiting_forever(engine):
    """Two routes of ONE source to the same file must be proven the same from
    material evidence. That proof must never need the source to admit two
    connections at once: on a one-connection server a concurrent pair proof
    is refused for capacity on every retry and never decides."""
    engine, repository, registry, tmp_path = engine
    executor = OneSlotSource(repository.authorize_execution, objects={"h.example/pub/f.bin": b"done"})
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("rsync", "rsync://h.example/pub/f.bin"),
                                    TransferRequest("rsync", "rsync://H.EXAMPLE/pub/f.bin")),
                                   name="f.bin", deduplicate=False)
    for _ in range(400):
        await engine.tick()
        if (await repository.get(transfer.id)).state in {TransferState.COMPLETED, TransferState.FAILED}:
            break
        await asyncio.sleep(0.01)
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    (artifact,) = await repository.artifacts(transfer.id)
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT payload,state,equivalence_disposition,equivalence_reason FROM transfer_requests "
                                 "WHERE transfer_id=? ORDER BY ordinal", (transfer.id,))
    # The second route was PROVEN the same artifact from material evidence
    # (never a second writer, never a held or guessed identity).
    assert [(row["state"], row["equivalence_disposition"], row["equivalence_reason"]) for row in rows] == [
        ("resolved", "", None), ("resolved", "recovered", "full_content_sample")]
    async with database.get_db() as db:
        held = await db.fetchall("SELECT equivalence_disposition FROM transfer_requests WHERE transfer_id=? "
                                 "AND equivalence_disposition IN ('exhausted','unverified')", (transfer.id,))
    assert held == []


@pytest.mark.asyncio
async def test_an_executor_that_cannot_list_a_tree_refuses_rather_than_flatten(tmp_path):
    from executors.aria2.executor import Aria2Configuration, Aria2Executor
    from test_v113_transport_evidence_sampling import candidate
    executor = Aria2Executor(None, Aria2Configuration(str(tmp_path)), None)
    with pytest.raises(TransferError) as raised:
        await executor.discover(ExecutionSubject.of(candidate("sftp://h.example/dir/")), depth=DiscoveryDepth.UNLIMITED)
    assert raised.value.error.category == Category.UNSUPPORTED_CAPABILITY


# ── 2. remote source capacity ────────────────────────────────────────────────

def _capacity(domain=Domain.NETWORK, stage=Stage.EXECUTION):
    return NormalizedError(domain, Category.CONCURRENCY_LIMITED, stage, retryability=Retryability.BACKOFF)


def test_a_source_servers_capacity_refusal_never_spends_the_recovery_budget():
    policy = TransferPolicy(retry_delay=5, max_retry_delay=300)
    error = _capacity()
    assert remote_source_capacity(error)
    delays = []
    for failures in (0, 1, 2, 3, 10, 10_000):
        decision = policy.recover(error, RecoveryContext(consecutive_no_progress_failures=failures,
                                                         same_signature_failures=failures), 0.0)
        assert (decision.action, decision.reason) == (RecoveryAction.BACKOFF, "remote_capacity_wait")
        assert decision.automatic and decision.quiescence_reason == "retry_backoff"
        delays.append(decision.retry_at)
    assert delays == sorted(delays) and max(delays) == 300
    moved = policy.recover(error, RecoveryContext(consecutive_no_progress_failures=2, has_alternate=True), 0.0)
    assert moved.action == RecoveryAction.TRY_ALTERNATE_CANDIDATE
    # The resolution stage (discovery against a full server) waits the same way.
    resolution = _capacity(stage=Stage.RESOLUTION)
    assert all(policy.retry(resolution, attempts, 0.0).automatic for attempts in (1, 3, 50, 5000))


def test_a_provider_accounts_concurrency_limit_keeps_its_ordinary_budget():
    policy = TransferPolicy(retry_delay=5, max_attempts=3)
    account = _capacity(domain=Domain.PROVIDER, stage=Stage.RESOLUTION)
    assert not remote_source_capacity(account)
    assert policy.retry(account, 1, 0.0).automatic and not policy.retry(account, 3, 0.0).automatic
    exhausted = policy.recover(_capacity(domain=Domain.PROVIDER),
                               RecoveryContext(consecutive_no_progress_failures=5), 0.0)
    assert exhausted.action == RecoveryAction.WAIT_FOR_OPERATOR


# ── 3. attempt-owned native process groups ───────────────────────────────────

@pytest.mark.asyncio
async def test_one_attempt_owns_one_process_group_proven_by_its_inherited_lock(tmp_path):
    owner = ProcessOwnership(tmp_path / "runtime")
    owned = await owner.spawn("attempt", [sys.executable, "-c", "import time; time.sleep(60)"],
                              env=owner.environment())
    try:
        assert owner.alive("attempt") is True
        with pytest.raises(ProcessGroupAlive):
            await owner.spawn("attempt", [sys.executable, "-c", "pass"], env=owner.environment())
        # A new owner over the same state (a restarted DebridPulse) still sees
        # the live group, and can stop it by what the lock proves.
        restarted = ProcessOwnership(tmp_path / "runtime")
        assert restarted.alive("attempt") is True and restarted.recorded_group("attempt") == owned.group
        assert await restarted.terminate(None, "attempt", grace=2) is True
        assert restarted.alive("attempt") is False
        restarted.forget("attempt")
        assert restarted.alive("attempt") is None
    finally:
        if owned.process.returncode is None:
            owned.process.kill()
        await owned.process.wait()


@pytest.mark.asyncio
async def test_secrets_reach_a_process_only_through_one_shot_pipes(tmp_path, monkeypatch):
    owner = ProcessOwnership(tmp_path / "runtime")
    seen = []
    real = asyncio.create_subprocess_exec

    async def record(*argv, **kwargs):
        seen.append((argv, kwargs.get("env")))
        return await real(*argv, **kwargs)

    monkeypatch.setattr("executors.process_ownership.asyncio.create_subprocess_exec", record)
    script = "import os,sys; sys.stdout.write(os.read(int(sys.argv[1]), 64).decode() + '|' + sys.stdin.read())"
    owned = await owner.spawn("secret-attempt", [], env=owner.environment(), secrets={"key": b"pipe-secret"},
                              stdin=b"stdin-secret",
                              fd_argv=lambda fds: [sys.executable, "-c", script, str(fds["key"])])
    output, _ = await owned.process.communicate()
    assert output == b"pipe-secret|stdin-secret"
    argv, env = seen[0]
    assert "pipe-secret" not in repr(argv) + repr(env) and "stdin-secret" not in repr(argv) + repr(env)
    assert set(env) == {"PATH", "LC_ALL", "LANG", "HOME"}
    assert not any(path.is_file() and (b"pipe-secret" in path.read_bytes() or b"stdin-secret" in path.read_bytes())
                   for path in (tmp_path / "runtime").rglob("*"))


# ── 4. the core stays neutral ────────────────────────────────────────────────

def test_no_universal_core_module_names_rsync():
    for path in sorted(TRANSFERS.glob("*.py")):
        if path.name == "requests.py":
            continue  # request-kind validation legitimately names submitted schemes
        lowered = path.read_text().lower()
        assert "rsync" not in lowered, path.name
    assert "general_rsync" not in (TRANSFERS / "requests.py").read_text()



# ── 5. one private-key import for every SSH consumer ─────────────────────────

@pytest.mark.parametrize("fmt,passphrase", [
    ("openssh", ""), ("openssh", "correct horse"), ("pkcs8-pem", "correct horse"), ("pkcs8-pem", ""),
])
def test_the_canonical_ssh_owner_imports_every_supported_private_key_format(fmt, passphrase):
    import asyncssh
    from services.artifact_sampling import client_key
    key = asyncssh.generate_private_key("ssh-ed25519")
    text = key.export_private_key(fmt, passphrase or None).decode()
    assert client_key(text, passphrase).public_data == key.public_data
    if passphrase:
        # A wrong (or missing) passphrase is one refusal that never carries the key.
        for wrong in ("wrong", ""):
            with pytest.raises(ValueError) as refused:
                client_key(text, wrong)
            assert text not in str(refused.value) and passphrase not in str(refused.value)
    with pytest.raises(ValueError):
        client_key("not a key", "")


# ── 6. one operator request, alternate interpretation ────────────────────────

class Interpretations(TreeListing):
    """In-memory executor claiming both readings of an rsync source. Each
    reading (scheme) may fail discovery with a configured server answer, and
    one may ask for a login until input arrives."""

    descriptor = IntegrationDescriptor("interpretations-memory", "Interpretations memory", frozenset())
    claim_schemes = frozenset({"rsync", "rsync+ssh"})

    def __init__(self, authorize, *, failures=None, login=None, **kwargs):
        super().__init__(authorize, **kwargs)
        self.failures = dict(failures or {})
        self.login = login
        self.readings: list[tuple[str, str | None]] = []

    @staticmethod
    def _object(candidate):
        return unquote(candidate.endpoints[0].address)

    async def discover(self, subject, submitted=None, *, depth=DiscoveryDepth.CURRENT):
        scheme = subject.candidate.endpoints[0].scheme
        self.readings.append((scheme, submitted.value(InputField.USERNAME) if submitted is not None else None))
        if scheme in self.failures:
            raise TransferError(self.failures[scheme])
        if scheme == self.login and submitted is None:
            return auth_required(username_password(), username_private_key())
        return await super().discover(subject, submitted, depth=depth)


def _answer(category, domain=Domain.RESOLUTION, retryability=Retryability.NEVER):
    return NormalizedError(domain, category, Stage.RESOLUTION, retryability=retryability,
                           integration_id="interpretations-memory")


ABSENT = _answer(Category.SOURCE_NOT_FOUND)
REFUSED = _answer(Category.CONNECTION_REFUSED, Domain.NETWORK, Retryability.BACKOFF)
UNANSWERED = _answer(Category.CONNECTION_TIMEOUT, Domain.NETWORK, Retryability.BACKOFF)
SSH_FILE = "rsync+ssh://h.example/home/user/file.iso"


async def _settle(engine, repository, transfer_id, *, until=None):
    for _ in range(80):
        await engine.tick()
        state = (await repository.get(transfer_id)).state
        if until is not None and await until():
            return state
        if state in {TransferState.COMPLETED, TransferState.FAILED}:
            return state
    return (await repository.get(transfer_id)).state


async def _resolution_failed(engine, repository, transfer_id):
    """The root request's own terminal resolution outcome."""
    for _ in range(80):
        await engine.tick()
        (request,) = await repository.requests(transfer_id)
        if request.state == "failed":
            return request
    raise AssertionError(f"resolution never failed: {request.state}")


@pytest.mark.parametrize("category,domain,absent", [
    (Category.SOURCE_NOT_FOUND, Domain.RESOLUTION, True),
    (Category.CONNECTION_REFUSED, Domain.NETWORK, True),
    (Category.AUTHENTICATION_FAILED, Domain.RESOLUTION, False),
    (Category.CREDENTIAL_MISSING, Domain.RESOLUTION, False),
    (Category.AUTHORIZATION_FAILED, Domain.RESOLUTION, False),
    (Category.CONCURRENCY_LIMITED, Domain.NETWORK, False),
    (Category.CONNECTION_FAILED, Domain.NETWORK, False),
    (Category.CONNECTION_TIMEOUT, Domain.NETWORK, False),
    (Category.READ_TIMEOUT, Domain.NETWORK, False),
    (Category.DNS_FAILURE, Domain.NETWORK, False),
    (Category.REMOTE_RESET, Domain.NETWORK, False),
    (Category.PROTOCOL_ERROR, Domain.NETWORK, False),
    (Category.HOST_KEY_FAILURE, Domain.SECURITY, False),
    (Category.EGRESS_POLICY_VIOLATION, Domain.SECURITY, False),
    (Category.SOURCE_NOT_FOUND, Domain.PROVIDER, False),
])
def test_only_a_positive_absence_lets_core_try_another_interpretation(category, domain, absent):
    assert interpretation_absent(_answer(category, domain)) is absent


@pytest.mark.parametrize("category,domain,progresses", [
    (Category.SOURCE_NOT_FOUND, Domain.RESOLUTION, True),
    (Category.CONNECTION_REFUSED, Domain.NETWORK, True),
    (Category.CONNECTION_TIMEOUT, Domain.NETWORK, True),
    (Category.AUTHENTICATION_FAILED, Domain.RESOLUTION, False),
    (Category.CREDENTIAL_MISSING, Domain.RESOLUTION, False),
    (Category.AUTHORIZATION_FAILED, Domain.RESOLUTION, False),
    (Category.CONCURRENCY_LIMITED, Domain.NETWORK, False),
    (Category.CONNECTION_FAILED, Domain.NETWORK, False),
    (Category.READ_TIMEOUT, Domain.NETWORK, False),
    (Category.DNS_FAILURE, Domain.NETWORK, False),
    (Category.EGRESS_POLICY_VIOLATION, Domain.SECURITY, False),
    (Category.HOST_KEY_FAILURE, Domain.SECURITY, False),
    (Category.CONNECTION_TIMEOUT, Domain.PROVIDER, False),
])
def test_a_reading_whose_endpoint_never_answered_advances_an_ambiguous_request(category, domain, progresses):
    """A bounded connect timeout advances to a provider-named alternate reading,
    and is still never absence: timeout keeps its ordinary meaning everywhere else."""
    from transfers.policy import alternate_interpretation_progresses
    error = _answer(category, domain)
    assert alternate_interpretation_progresses(error) is progresses
    if category == Category.CONNECTION_TIMEOUT:
        assert interpretation_absent(error) is False


@pytest.mark.asyncio
async def test_a_daemon_that_provides_the_path_is_authoritative_and_ssh_is_never_probed(engine):
    engine, repository, registry, tmp_path = engine
    executor = Interpretations(repository.authorize_execution, objects={"rsync://h.example/pub/file.iso": b"done"})
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("rsync", "rsync://h.example/pub/file.iso"),), deduplicate=False)
    assert await _settle(engine, repository, transfer.id) == TransferState.COMPLETED
    assert [scheme for scheme, _ in executor.readings] == ["rsync"]
    (request,) = await repository.requests(transfer.id)
    assert request.interpretation is None and request.resolvable == request.request
    (artifact,) = await repository.artifacts(transfer.id)
    assert [endpoint.scheme for endpoint in artifact.candidates[0].endpoints] == ["rsync"]


@pytest.mark.asyncio
@pytest.mark.parametrize("primary", [ABSENT, REFUSED, UNANSWERED],
                         ids=["unknown-module-or-path", "no-daemon-listening", "daemon-port-silent"])
async def test_a_positively_absent_daemon_reading_resolves_over_ssh_once_and_durably(engine, primary):
    engine, repository, registry, tmp_path = engine
    executor = Interpretations(repository.authorize_execution, objects={SSH_FILE: b"done"},
                               failures={"rsync": primary}, login="rsync+ssh")
    registry.register_executor(executor)
    typed = "rsync://h.example/home/user/file.iso"
    transfer = await engine.submit((TransferRequest("rsync", typed),), deduplicate=False)

    async def asked():
        return await engine.challenges.current(transfer.id) is not None

    await _settle(engine, repository, transfer.id, until=asked)
    challenge = await engine.challenges.current(transfer.id)
    # The SSH reading asks through the one lifecycle, with both SSH sign-in methods.
    assert [item.method.value for item in challenge.methods] == ["username_password", "username_private_key"]
    (request,) = await repository.requests(transfer.id)
    # The interpretation is durable from the moment it was reached; the
    # operator's own input is untouched.
    assert request.request == TransferRequest("rsync", typed)
    assert request.interpretation == TransferRequest("rsync+ssh", SSH_FILE)
    await engine.submit_input(transfer.id, challenge.id, "username_password",
                              {"username": "operator", "password": "ssh-password-sentinel"})
    assert await _settle(engine, repository, transfer.id) == TransferState.COMPLETED
    # The daemon was asked exactly once and never received input; the answer
    # reached only the SSH reading that asked for it.
    assert executor.readings == [("rsync", None), ("rsync+ssh", None), ("rsync+ssh", "operator")]
    # The answer is SSH-scope material: never the daemon's.
    chain = (request.id,)
    assert await engine.inputs.valid_for(transfer.id, chain, auth_scope(SSH_FILE)) is True
    assert await engine.inputs.valid_for(transfer.id, chain, auth_scope(typed)) is False
    (artifact,) = await repository.artifacts(transfer.id)
    (candidate,) = artifact.candidates
    assert [endpoint.address for endpoint in candidate.endpoints] == [SSH_FILE]
    assert candidate.request_kind == "rsync+ssh" and candidate.provider_id == "general_rsync"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    _answer(Category.CONCURRENCY_LIMITED, Domain.NETWORK, Retryability.BACKOFF),
    _answer(Category.CONNECTION_FAILED, Domain.NETWORK, Retryability.BACKOFF),
    _answer(Category.AUTHORIZATION_FAILED),
    _answer(Category.AUTHENTICATION_FAILED, retryability=Retryability.AFTER_REAUTH),
    _answer(Category.EGRESS_POLICY_VIOLATION, Domain.SECURITY),
], ids=["capacity", "network", "access-denied", "auth-rejected", "policy-denied"])
async def test_a_daemon_answer_that_is_not_absence_never_falls_through_to_ssh(engine, failure):
    engine, repository, registry, tmp_path = engine
    executor = Interpretations(repository.authorize_execution, objects={SSH_FILE: b"done"},
                               failures={"rsync": failure})
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("rsync", "rsync://h.example/home/user/file.iso"),),
                                   deduplicate=False)
    for _ in range(6):
        await engine.tick()
    assert {scheme for scheme, _ in executor.readings} == {"rsync"}
    (request,) = await repository.requests(transfer.id)
    assert request.interpretation is None
    assert request.error is not None and request.error.category == failure.category


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [UNANSWERED, ABSENT, REFUSED], ids=["timeout", "absent", "refused"])
async def test_a_source_with_its_own_port_names_one_service_and_never_advances(engine, failure):
    engine, repository, registry, tmp_path = engine
    executor = Interpretations(repository.authorize_execution, objects={SSH_FILE: b"done"},
                               failures={"rsync": failure})
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("rsync", "rsync://h.example:873/home/user/file.iso"),),
                                   deduplicate=False)
    for _ in range(6):
        await engine.tick()
    assert {scheme for scheme, _ in executor.readings} == {"rsync"}
    (request,) = await repository.requests(transfer.id)
    assert request.interpretation is None
    assert request.error is not None and request.error.category == failure.category


@pytest.mark.asyncio
async def test_a_daemon_asking_for_its_own_login_is_answered_as_the_daemon(engine):
    engine, repository, registry, tmp_path = engine
    executor = Interpretations(repository.authorize_execution, objects={"rsync://h.example/priv/file.iso": b"done"},
                               login="rsync")
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("rsync", "rsync://h.example/priv/file.iso"),), deduplicate=False)

    async def asked():
        return await engine.challenges.current(transfer.id) is not None

    await _settle(engine, repository, transfer.id, until=asked)
    assert {scheme for scheme, _ in executor.readings} == {"rsync"}
    (request,) = await repository.requests(transfer.id)
    assert request.interpretation is None


@pytest.mark.asyncio
async def test_an_explicit_ssh_source_never_probes_a_daemon(engine):
    engine, repository, registry, tmp_path = engine
    executor = Interpretations(repository.authorize_execution, objects={SSH_FILE: b"done"})
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("rsync+ssh", SSH_FILE),), deduplicate=False)
    assert await _settle(engine, repository, transfer.id) == TransferState.COMPLETED
    assert [scheme for scheme, _ in executor.readings] == ["rsync+ssh"]
    (request,) = await repository.requests(transfer.id)
    assert request.interpretation is None


@pytest.mark.asyncio
@pytest.mark.parametrize("primary,secondary,reported", [
    (ABSENT, ABSENT, "primary"),      # both servers answered: the operator's own reading
    (REFUSED, ABSENT, "secondary"),   # a server that answered outranks a port that refused
    (ABSENT, REFUSED, "primary"),
    (REFUSED, REFUSED, "primary"),
    (UNANSWERED, ABSENT, "secondary"),     # the SSH server answered; the silent daemon did not
    (UNANSWERED, REFUSED, "primary"),
    (UNANSWERED, UNANSWERED, "primary"),   # neither endpoint answered: nothing is established
])
async def test_when_neither_reading_provides_the_source_one_stable_answer_is_reported(
        engine, primary, secondary, reported):
    engine, repository, registry, tmp_path = engine
    marked = {"primary": NormalizedError(primary.domain, primary.category, primary.stage,
                                         retryability=Retryability.NEVER, integration_id="daemon-reading"),
              "secondary": NormalizedError(secondary.domain, secondary.category, secondary.stage,
                                           retryability=Retryability.NEVER, integration_id="ssh-reading")}
    executor = Interpretations(repository.authorize_execution,
                               failures={"rsync": marked["primary"], "rsync+ssh": marked["secondary"]})
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("rsync", "rsync://h.example/home/user/file.iso"),),
                                   deduplicate=False)
    request = await _resolution_failed(engine, repository, transfer.id)
    assert request.error.integration_id == marked[reported].integration_id
    assert request.error.category == marked[reported].category
    # Neither reading was established: nothing is recorded as the interpretation.
    assert request.interpretation is None
    assert [scheme for scheme, _ in executor.readings] == ["rsync", "rsync+ssh"]


@pytest.mark.asyncio
async def test_an_empty_daemon_directory_is_the_daemons_answer_never_an_ssh_fallback(engine):
    engine, repository, registry, tmp_path = engine
    # The daemon lists the directory (it exists, with nothing in it): its own
    # answer, even though the provider then calls it a missing source.
    executor = Interpretations(repository.authorize_execution,
                               objects={"rsync+ssh://h.example/pub/empty/f.bin": b"done"})
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("rsync", "rsync://h.example/pub/empty"),), deduplicate=False)
    request = await _resolution_failed(engine, repository, transfer.id)
    assert [scheme for scheme, _ in executor.readings] == ["rsync"]
    assert request.interpretation is None and request.error.category == Category.SOURCE_NOT_FOUND


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["rsync://h.example/home/user/Album", "rsync://h.example/home/user/Album/"])
async def test_a_directory_means_one_tree_through_the_ssh_reading_with_or_without_a_slash(engine, typed):
    engine, repository, registry, tmp_path = engine
    objects = {"rsync+ssh://h.example/home/user/Album/cover.jpg": b"done",
               "rsync+ssh://h.example/home/user/Album/Disc 1/01.flac": b"done"}
    executor = Interpretations(repository.authorize_execution, objects=objects, failures={"rsync": ABSENT})
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("rsync", typed),), deduplicate=False)
    assert await _settle(engine, repository, transfer.id) == TransferState.COMPLETED
    targets = sorted(Path(artifact.target).relative_to(tmp_path / "downloads").as_posix()
                     for artifact in await repository.artifacts(transfer.id))
    assert targets == ["Album/Disc 1/01.flac", "Album/cover.jpg"]
    rows = await repository.requests(transfer.id)
    # Members are ordinary SSH-reading requests of the frozen tree.
    assert {row.request.payload for row in rows if row.parent_id} == {
        "rsync+ssh://h.example/home/user/Album/cover.jpg", "rsync+ssh://h.example/home/user/Album/Disc%201/01.flac"}
