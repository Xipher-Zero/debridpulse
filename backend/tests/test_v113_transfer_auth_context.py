"""DP 1.0.13 generalized transfer authentication (Authentication Input Context).

One canonical owner -- the process-local ``EphemeralInputBroker`` -- holds
USER_SUPPLIED authentication material however it arrived: split out of a
credential-bearing resource at an admission boundary, or answered through the
one INPUT_REQUIRED lifecycle. Consumers (evidence sampling, execution,
provider resolution, discovery) only emit ordinary requirements; the owner
alone matches, validates (single-flight), reuses within a bounded lineage and
target scope, rejects, and destroys it. Everything below runs against the
unrelated ``vault`` fake transport so nothing can be coupled to aria2 or SSH.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import VaultExecutor, neutral_facts
from transfers.applicability import ProviderApplicability
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, TransferError
from transfers.input_required import server_identity_required, username_password
from transfers.models import (
    Capability, Endpoint, ExecutionObservation, ExecutionState, ExecutorCapabilities, TransferProgress, FileManifest, FileManifestEntry, InputFactName, InputMethod,
    IntegrationDescriptor, Ownership, ProviderObservation, ProviderResource, ResolutionResult, ResourceState,
    SourceEntry, SourceIdentity, TransferCandidate, TransferRequest, TransferState,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry
from transfers.requests import normalize_direct_links

pytestmark = pytest.mark.asyncio

CONTENT = b"four"  # the memory copier materializes exactly four bytes
USER, PASSWORD = "vault-user-sentinel", "vault-password-sentinel"
WRONG = "vault-wrong-password-sentinel"
FINGERPRINT = "a" * 40


class LockedSource:
    """A URL-shaped, resolution-only provider.

    ``vault://host/item`` is one exact candidate; ``vault://host/dir/`` is a
    collection whose members come from ``listings`` (a member that is a full
    URL -- another host, or credential-bearing -- is admitted as written). It
    never sees credentials: a credential-bearing payload is refused, exactly
    like the real Network Sources providers."""

    applicability = ProviderApplicability(generic_schemes=frozenset({"vault"}))

    def __init__(self, listings=None):
        self.descriptor = IntegrationDescriptor(
            "locked-source", "Locked source",
            frozenset({Capability.RESOLVE, Capability.RESOURCE_LOOKUP, Capability.FILE_MANIFEST}),
            request_types=frozenset({"vault"}))
        self.listings = dict(listings or {})
        self.seen = []

    def _members(self, resource):
        base = resource.context["base"]
        return [(name, url if "://" in url else base + url) for name, url in self.listings[base]]

    def _observation(self, resource):
        members = self._members(resource)
        return ProviderObservation(resource, ResourceState.AVAILABLE, "collection", file_manifest=FileManifest(
            tuple(FileManifestEntry(name, name, len(CONTENT)) for name, _url in members)))

    async def resolve(self, request):
        self.seen.append(request.payload)
        parts = urlsplit(request.payload)
        if parts.username is not None or parts.password is not None:
            raise TransferError(NormalizedError(Domain.SECURITY, Category.SECURITY_POLICY_REJECTED, Stage.RESOLUTION,
                                                retryability=Retryability.NEVER, integration_id="locked-source"))
        if parts.path.endswith("/"):
            # The durable resource names only the collection; members stay in
            # the provider's own hands (a provider never persists credentials).
            resource = ProviderResource(self.descriptor.id, {"base": request.payload}, Ownership.OBSERVED,
                                        id=f"locked:{request.payload}")
            return ResolutionResult(ResourceState.AVAILABLE, observation=self._observation(resource))
        return ResolutionResult(ResourceState.AVAILABLE, (TransferCandidate(
            request.name or parts.path.rsplit("/", 1)[-1], (Endpoint("vault", request.payload),),
            expected_bytes=len(CONTENT), provider_id=self.descriptor.id,
            source_identity=SourceIdentity("host", parts.hostname),
            accepted_input_methods=(InputMethod.USERNAME_PASSWORD,)),))

    async def observe(self, resource):
        return self._observation(resource)

    async def manifest(self, resource):
        return tuple(SourceEntry(name, len(CONTENT), name, TransferRequest("vault", url, name=name))
                     for name, url in self._members(resource))


class CountingVault(VaultExecutor):
    """``VaultExecutor`` that checks the credential on every input-bearing
    start, counts attempts made with wrong or foreign material, and finishes a
    started job at once so a transfer can complete within a few ticks."""

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, **kwargs)
        self.wrong_attempts = 0
        self.alt_attempts_with_foreign_material = 0

    def _count(self, candidate, submitted):
        from transfers.models import InputField
        host = urlsplit(candidate.endpoints[0].address).hostname
        if submitted.value(InputField.PASSWORD) == WRONG:
            self.wrong_attempts += 1
        if host == "alt.example" and submitted.value(InputField.USERNAME) == USER:
            self.alt_attempts_with_foreign_material += 1
        return host

    async def fingerprint_with_input(self, subject, submitted):
        self._count(subject.candidate, submitted)
        return await super().fingerprint_with_input(subject, submitted)

    async def start(self, request, handle):
        observed = await super().start(request, handle)
        if observed.error is None:
            self.finish(handle)
        return observed

    async def start_with_input(self, request, handle, submitted):
        from transfers.models import InputField
        candidate_id = str(request.work.subject.candidate.id)
        host = self._count(request.work.subject.candidate, submitted)
        accepted = (submitted.value(InputField.USERNAME), submitted.value(InputField.PASSWORD)) == self.locks.get(host)
        if not await self.authorize(handle, "start"):
            # Continuing the challenged, already-started attempt with its answer.
            self.input_starts.append((candidate_id, submitted.value(InputField.USERNAME) if accepted else "rejected"))
            error = None if accepted else NormalizedError(Domain.EXECUTOR, Category.UNMAPPED_EXECUTOR_ERROR,
                                                          Stage.EXECUTION, native_code="vault-auth")
            observed = neutral_facts(ExecutionObservation(
                handle, ExecutionState.RUNNING if accepted else ExecutionState.FAILED, TransferProgress(4, 1, 1), error))
            self.jobs[handle.attempt_id] = observed
            if accepted:
                self.finish(handle)
            return observed
        if not accepted:
            # Wrong material: the transport rejects it exactly like a missing one.
            self.input_starts.append((candidate_id, "rejected"))
            return await VaultExecutor.start(self, request, handle)
        return await super().start_with_input(request, handle, submitted)


class IdentityVault(CountingVault):
    """Vault transport whose host also has a server identity to confirm.

    A locked host answers the neutral SERVER_IDENTITY_REQUIRED requirement
    until the operator confirmed exactly ``FINGERPRINT`` (carried back as the
    challenge's facts), and only then checks the credential."""

    descriptor = IntegrationDescriptor("vault-identity", "Vault identity", frozenset())
    capabilities = ExecutorCapabilities(candidate_sampling=True, per_execution_pause=True, transient_input=True)

    @staticmethod
    def _requirement(host):
        return server_identity_required(username_password(), host=host, algorithm="sha-1", fingerprint=FINGERPRINT)

    def input_requirement(self, candidate, observation):
        requirement = super().input_requirement(candidate, observation)
        if requirement is None:
            return None
        return self._requirement(urlsplit(candidate.endpoints[0].address).hostname)

    async def start_with_input(self, request, handle, submitted):
        facts = {fact.name: fact.value for fact in submitted.facts}
        assert facts.get(InputFactName.SERVER_IDENTITY_FINGERPRINT) == FINGERPRINT, "identity was not confirmed"
        return await CountingVault.start_with_input(self, request, handle, submitted)


@pytest_asyncio.fixture
async def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "auth-context.sqlite3")
    await database.init_db()
    now = [1000.0]
    repository = TransferRepository()
    registry = IntegrationRegistry()
    policy = TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=5)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"), policy=policy,
                            clock=lambda: now[0])
    await engine.initialize()
    objects = {f"{host}/{path}": CONTENT for host in ("locked.example", "alt.example", "open.example")
               for path in ("solo.bin", "dir/a.bin", "dir/b.bin", "dir/c.bin", "dir/d.bin")}
    locks = {"locked.example": (USER, PASSWORD), "alt.example": ("alt-user", "alt-password")}
    provider = LockedSource({
        "vault://locked.example/dir/": [("a.bin", "a.bin"), ("b.bin", "b.bin"), ("c.bin", "c.bin")],
        "vault://locked.example/wide/": [("a.bin", "vault://locked.example/dir/a.bin"),
                                         ("d.bin", "vault://alt.example/dir/d.bin")],
        "vault://open.example/dir/": [("a.bin", "vault://open.example/dir/a.bin"),
                                      ("c.bin", f"vault://{USER}:{PASSWORD}@locked.example/dir/c.bin")],
    })
    registry.register_provider(provider)
    return repository, registry, engine, provider, objects, locks, now


def _executor(lab, kind=CountingVault):
    repository, registry, _engine, _provider, objects, locks, _now = lab
    executor = kind(repository.authorize_execution, objects=objects, locks=locks)
    registry.register_executor(executor)
    return executor


async def _run(engine, *, until, count=40, now=None):
    challenges = []
    for _ in range(count):
        if now is not None:
            now[0] += 5
        await engine.tick()
        current = await _current(engine, until)
        if current is not None and current.id not in {item.id for item in challenges}:
            challenges.append(current)
        state = (await engine.repository.get(until)).state
        if state in {TransferState.COMPLETED, TransferState.FAILED}:
            break
    return challenges


async def _current(engine, transfer_id):
    return await engine.challenges.current(transfer_id)


async def _db_text():
    async with database.get_db() as db:
        tables = [row["name"] for row in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
                  if not row["name"].startswith("sqlite_")]
        return json.dumps({name: await db.fetchall(f"SELECT * FROM {name}") for name in tables},  # nosec B608
                          sort_keys=True, default=str)


async def _answer(engine, transfer_id, challenge, *, method="username_password", password=PASSWORD):
    values = {} if method == "server_identity" else {"username": USER, "password": password}
    await engine.submit_input(transfer_id, challenge.id, method, values)


# ── RED 1 / 13: admission ingests credentials instead of refusing them ────────

@pytest.mark.parametrize("link", ["scp://alice:secret@host.example/f.bin", "ssh://alice:secret@host.example/d/",
                                  "https://user:password@example.org/file", "sftp://u:p@host.example/f"])
async def test_admission_accepts_credential_bearing_links(link):
    assert normalize_direct_links([link]) == [link]


# ── RED 2: credentials supplied before the requirement satisfy it ─────────────

async def test_credentials_supplied_before_the_requirement_satisfy_it_without_a_prompt(lab):
    repository, _registry, engine, provider, *_rest, now = lab
    executor = _executor(lab)
    transfer = await engine.submit((TransferRequest("vault", f"vault://{USER}:{PASSWORD}@locked.example/solo.bin"),),
                                   deduplicate=False)
    challenges = await _run(engine, until=transfer.id, now=now)
    assert challenges == []
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert [user for _candidate, user in executor.input_starts] == [USER]
    assert all("@" not in payload for payload in provider.seen), "a provider saw the credential-bearing resource"
    text = await _db_text()
    assert PASSWORD not in text and f"{USER}:" not in text


# ── RED 3: one interactive answer serves every sibling of the lineage ─────────

async def test_one_interactive_answer_serves_every_directory_sibling(lab):
    repository, _registry, engine, *_rest, now = lab
    _executor(lab)
    transfer = await engine.submit((TransferRequest("vault", "vault://locked.example/dir/"),), deduplicate=False)
    answered = []
    for _ in range(60):
        now[0] += 5
        await engine.tick()
        current = await _current(engine, transfer.id)
        if current is not None and current.id not in answered:
            answered.append(current.id)
            await _answer(engine, transfer.id, current)
        if (await repository.get(transfer.id)).state == TransferState.COMPLETED:
            break
    assert len(answered) == 1, f"{len(answered)} prompts for one directory"
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED


# ── RED 4 / 11: single-flight validation; rejected material is never auto-retried

async def test_wrong_supplied_material_is_tried_once_then_one_prompt_then_reused(lab):
    repository, _registry, engine, *_rest, now = lab
    executor = _executor(lab)
    transfer = await engine.submit((TransferRequest("vault", f"vault://{USER}:{WRONG}@locked.example/dir/"),),
                                   deduplicate=False)
    answered = []
    for _ in range(80):
        now[0] += 5
        await engine.tick()
        current = await _current(engine, transfer.id)
        if current is not None and current.id not in answered:
            answered.append(current.id)
            await _answer(engine, transfer.id, current)
        if (await repository.get(transfer.id)).state == TransferState.COMPLETED:
            break
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert len(answered) == 1, "the rejection must coalesce into exactly one normal prompt"
    assert executor.wrong_attempts == 1, "rejected material was validated more than once"
    events = await _events(transfer.id)
    assert events.count("auth_rejected") == 1


async def _events(transfer_id):
    async with database.get_db() as db:
        rows = await db.fetchall("SELECT kind FROM application_events WHERE transfer_id=?", (transfer_id,))
    return [row["kind"] for row in rows]


# ── RED 7: server identity and credentials are independent requirements ───────

async def test_supplied_credentials_leave_only_the_identity_to_confirm(lab):
    repository, _registry, engine, *_rest, now = lab
    executor = _executor(lab, IdentityVault)
    transfer = await engine.submit((TransferRequest("vault", f"vault://{USER}:{PASSWORD}@locked.example/solo.bin"),),
                                   deduplicate=False)
    answered = []
    for _ in range(40):
        now[0] += 5
        await engine.tick()
        current = await _current(engine, transfer.id)
        if current is not None and current.id not in {item.id for item in answered}:
            answered.append(current)
            assert current.reason.value == "server_identity_required"
            # Credentials are already held: the challenge asks for the identity only.
            assert [item.method.value for item in current.methods] == ["server_identity"]
            await _answer(engine, transfer.id, current, method="server_identity")
        if (await repository.get(transfer.id)).state == TransferState.COMPLETED:
            break
    assert len(answered) == 1
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert [user for _candidate, user in executor.input_starts] == [USER]


async def test_credentials_never_bypass_an_unconfirmed_identity(lab):
    repository, _registry, engine, *_rest, now = lab
    _executor(lab, IdentityVault)
    transfer = await engine.submit((TransferRequest("vault", f"vault://{USER}:{PASSWORD}@locked.example/solo.bin"),),
                                   deduplicate=False)
    challenges = await _run(engine, until=transfer.id, now=now)
    assert len(challenges) == 1 and challenges[0].reason.value == "server_identity_required"
    assert (await repository.get(transfer.id)).state != TransferState.COMPLETED


# ── RED 9: cancellation destroys the context ──────────────────────────────────

async def test_cancellation_destroys_the_secret_bearing_context_immediately(lab):
    _repository, _registry, engine, *_rest, now = lab
    _executor(lab)
    transfer = await engine.submit((TransferRequest("vault", f"vault://{USER}:{PASSWORD}@locked.example/dir/"),),
                                   deduplicate=False)
    assert await engine.inputs.holds(transfer.id)
    await engine.cancel(transfer.id)
    assert not await engine.inputs.holds(transfer.id)


async def test_deletion_destroys_the_secret_bearing_context_immediately(lab):
    _repository, _registry, engine, *_rest, now = lab
    _executor(lab)
    transfer = await engine.submit((TransferRequest("vault", f"vault://{USER}:{PASSWORD}@locked.example/dir/"),),
                                   deduplicate=False)
    assert await engine.inputs.holds(transfer.id)
    await engine.delete(transfer.id, remote=False)
    assert not await engine.inputs.holds(transfer.id)


# ── RED 10: a descendant in another target scope never reuses the parent's auth

async def test_cross_scope_descendant_does_not_reuse_parent_auth(lab):
    repository, _registry, engine, *_rest, now = lab
    executor = _executor(lab)
    transfer = await engine.submit((TransferRequest("vault", f"vault://{USER}:{PASSWORD}@locked.example/wide/"),),
                                   deduplicate=False)
    challenges = await _run(engine, until=transfer.id, now=now, count=40)
    # The same-scope member used the supplied material; the alt.example member
    # asked for its own authentication instead of receiving it.
    assert [user for _candidate, user in executor.input_starts] == [USER]
    assert len(challenges) == 1
    assert challenges[0].reason.value == "auth_required"
    assert executor.alt_attempts_with_foreign_material == 0


# ── RED 12: a decomposed child's credentials are captured at the same boundary ─

async def test_credential_bearing_child_is_ingested_at_the_admission_boundary(lab):
    repository, _registry, engine, provider, *_rest, now = lab
    executor = _executor(lab)
    transfer = await engine.submit((TransferRequest("vault", "vault://open.example/dir/"),), deduplicate=False)
    challenges = await _run(engine, until=transfer.id, now=now)
    assert challenges == []
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert [user for _candidate, user in executor.input_starts] == [USER]
    assert all("@" not in payload for payload in provider.seen)
    assert PASSWORD not in await _db_text()


# ── 43.8: independent submissions never share material ────────────────────────

async def test_independent_same_host_submissions_never_share_material(lab):
    repository, _registry, engine, *_rest, now = lab
    executor = _executor(lab)
    first = await engine.submit((TransferRequest("vault", f"vault://{USER}:{PASSWORD}@locked.example/solo.bin"),),
                                deduplicate=False)
    assert await _run(engine, until=first.id, now=now) == []
    second = await engine.submit((TransferRequest("vault", "vault://locked.example/dir/a.bin"),), deduplicate=False)
    challenges = await _run(engine, until=second.id, now=now, count=10)
    assert len(challenges) == 1, "an unrelated submission silently reused another transfer's credentials"
    assert len(executor.input_starts) == 1


# ── 43.6: restart keeps semantics, never secrets ──────────────────────────────

async def test_restart_forgets_supplied_material_and_asks_normally(lab, tmp_path):
    repository, registry, engine, *_rest, now = lab
    _executor(lab)
    transfer = await engine.submit((TransferRequest("vault", f"vault://{USER}:{PASSWORD}@locked.example/solo.bin"),),
                                   deduplicate=False)
    restarted = TransferEngine(TransferRepository(), registry, download_root=str(tmp_path / "payloads"),
                               policy=engine.policy, clock=lambda: now[0])
    await restarted.initialize()
    challenges = await _run(restarted, until=transfer.id, now=now)
    assert len(challenges) == 1 and challenges[0].reason.value == "auth_required"
    assert PASSWORD not in await _db_text()


# ── RED 13 / 43.14: every transfer-auth consumer uses the one owner ───────────

class UrlVault(CountingVault):
    """The counting vault reached over the real Network Sources transports."""

    descriptor = IntegrationDescriptor("url-vault", "URL vault", frozenset())
    claim_schemes = frozenset({"http", "https", "ftp", "sftp"})

    @staticmethod
    def _object(candidate):
        parts = urlsplit(candidate.endpoints[0].address)
        return f"{parts.hostname}{parts.path}"


@pytest.mark.parametrize("link", [
    f"https://{USER}:{PASSWORD}@locked.example/solo.bin",
    f"http://{USER}:{PASSWORD}@locked.example/solo.bin",
    f"ftp://{USER}:{PASSWORD}@locked.example/solo.bin",
    f"sftp://{USER}:{PASSWORD}@locked.example/solo.bin",
    f"scp://{USER}:{PASSWORD}@locked.example/solo.bin",
    f"ssh://{USER}:{PASSWORD}@locked.example:22/solo.bin",
])
async def test_every_network_source_consumes_supplied_material_through_the_one_owner(lab, link):
    from providers.general_ftp.provider import GeneralFtpProvider
    from providers.general_http.provider import GeneralHttpProvider
    from providers.general_scp.provider import ScpProvider
    repository, registry, engine, *_rest, now = lab
    for provider in (GeneralHttpProvider(), GeneralFtpProvider(), ScpProvider()):
        registry.register_provider(provider)
    executor = _executor(lab, UrlVault)
    kind = link.split(":", 1)[0]
    transfer = await engine.submit((TransferRequest(kind, link, name="solo.bin"),), deduplicate=False)
    challenges = await _run(engine, until=transfer.id, now=now)
    assert challenges == []
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert [user for _candidate, user in executor.input_starts] == [USER]
    assert PASSWORD not in await _db_text()


async def test_an_interactive_https_answer_is_matched_like_any_other(lab):
    from providers.general_http.provider import GeneralHttpProvider
    repository, registry, engine, *_rest, now = lab
    registry.register_provider(GeneralHttpProvider())
    executor = _executor(lab, UrlVault)
    transfer = await engine.submit((TransferRequest("https", "https://locked.example/solo.bin", name="solo.bin"),),
                                   deduplicate=False)
    answered = []
    for _ in range(40):
        now[0] += 5
        await engine.tick()
        current = await _current(engine, transfer.id)
        if current is not None and current.id not in answered:
            answered.append(current.id)
            await _answer(engine, transfer.id, current)
        if (await repository.get(transfer.id)).state == TransferState.COMPLETED:
            break
    assert len(answered) == 1 and [user for _candidate, user in executor.input_starts] == [USER]


# ── Lifetime contract (frozen): FAILED keeps material for operator retry ──────

async def test_failed_transfers_keep_material_within_the_24_hour_ceiling_only():
    """Semantic lifetime is primary; this freezes the one deliberate exception:
    a FAILED transfer is not terminal (an operator may retry it), so its
    ephemeral material stays resident -- in memory only -- until the 24-hour
    safety ceiling, while cancel, delete and restart destroy it at once (proven
    above)."""
    from transfers.input_required import EphemeralInputBroker
    from transfers.models import InputField
    from transfers.policy import TERMINAL_TRANSFER_STATES
    from transfers.requests import auth_scope
    assert TransferState.FAILED not in TERMINAL_TRANSFER_STATES
    now = [0.0]
    broker = EphemeralInputBroker(clock=lambda: now[0])
    assert broker.context_ceiling_seconds == 24 * 3600
    await broker.supply(7, "root", auth_scope("sftp://h/f"), {InputField.USERNAME: "u", InputField.PASSWORD: "p"},
                        origin="admission")
    now[0] = 24 * 3600 - 1
    assert await broker.holds(7)
    now[0] = 24 * 3600
    assert not await broker.holds(7)


async def test_a_failed_transfer_is_not_destroyed_by_aggregation(lab):
    _repository, _registry, engine, *_rest, _now = lab
    _executor(lab)
    transfer = await engine.submit((TransferRequest("vault", f"vault://{USER}:{PASSWORD}@locked.example/solo.bin"),),
                                   deduplicate=False)
    await engine.repository.state(transfer.id, TransferState.FAILED)
    assert (await engine.repository.get(transfer.id)).state == TransferState.FAILED
    await engine._aggregate(transfer.id)
    assert await engine.inputs.holds(transfer.id)
