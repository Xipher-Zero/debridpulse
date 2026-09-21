"""Neutral INPUT_REQUIRED/AUTH_REQUIRED lifecycle proof with unrelated integrations."""
import asyncio
import json

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor, ParcelProvider
from test_candidate_provenance_consolidation import admit, p2  # noqa: F401 -- pytest fixture re-export
from transfers import codec
from transfers.applicability import ProviderApplicability
from transfers.convergence_engine import TransferEngine
from transfers.errors import Category, Domain, NormalizedError, Origin, Retryability, Stage
from transfers.input_required import (InputSubmissionRejected, auth_required, username_password, username_private_key, validate_submission)
from transfers.models import (
    Capability, Endpoint, ExecutionHandle, InputChallenge, InputField, InputMethod, InputOrigin, InputReason, IntegrationDescriptor,
    MaterializationAdmission, MaterializationAdmissionKind, ResourceState, ResolutionResult,
    TransferCandidate, TransferRequest, TransferState,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry


class AuthParcelProvider:
    def __init__(self):
        self.descriptor = IntegrationDescriptor("auth-parcel", "Auth parcel", frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"auth-parcel"}))
        self.resolve_calls = 0
        self.continuation_calls = 0

    @property
    def applicability(self):
        return ProviderApplicability()

    async def resolve(self, request):
        self.resolve_calls += 1
        return ResolutionResult(ResourceState.UNKNOWN, input_required=auth_required(username_password()))

    async def resolve_with_input(self, request, submitted):
        self.continuation_calls += 1
        if (submitted.method == InputMethod.USERNAME_PASSWORD
                and submitted.value(InputField.USERNAME) == "provider-user-sentinel"
                and submitted.value(InputField.PASSWORD) == "provider-password-sentinel"):
            candidate = TransferCandidate("parcel.bin", (Endpoint("memory", "memory:parcel"),), expected_bytes=4,
                                          provider_id=self.descriptor.id)
            return ResolutionResult(ResourceState.AVAILABLE, (candidate,))
        return ResolutionResult(ResourceState.UNKNOWN, input_required=auth_required(username_password()))


class StaticProvider:
    def __init__(self, scheme="keymem"):
        self.scheme = scheme
        self.descriptor = IntegrationDescriptor("static-parcel", "Static parcel", frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"key-parcel"}))

    @property
    def applicability(self):
        return ProviderApplicability()

    async def resolve(self, request):
        candidate = TransferCandidate("key.bin", (Endpoint(self.scheme, self.scheme + ":payload"),), expected_bytes=4,
                                      provider_id=self.descriptor.id)
        return ResolutionResult(ResourceState.AVAILABLE, (candidate,))


class KeyExecutor(MemoryExecutor):
    def __init__(self, authorize, *, encrypted):
        super().__init__(authorize)
        self.encrypted = encrypted
        self.prepare_calls = 0
        self.input_calls = 0
        self.descriptor = IntegrationDescriptor("key-copy", "Key copy", frozenset({Capability.PAUSE, Capability.RESUME, Capability.RECONCILE}),
                                                schemes=frozenset({"keymem"}))

    def prepare(self, request):
        self.prepare_calls += 1
        return auth_required(username_private_key())

    def prepare_with_input(self, request, submitted):
        self.input_calls += 1
        accepted = (submitted.method == InputMethod.USERNAME_PRIVATE_KEY
                    and submitted.value(InputField.USERNAME) == "executor-user-sentinel"
                    and submitted.value(InputField.PRIVATE_KEY) == "executor-private-key-sentinel")
        if self.encrypted:
            accepted = accepted and submitted.value(InputField.PASSPHRASE) == "executor-passphrase-sentinel"
        if not accepted:
            return auth_required(username_private_key())
        return ExecutionHandle(self.descriptor.id, {"copy_ticket": request.attempt_id, "destination": request.target}, request.attempt_id)


@pytest_asyncio.fixture
async def base(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    registry = IntegrationRegistry()
    now = [1000.0]
    policy = TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=1)
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"), policy=policy, clock=lambda: now[0])
    await engine.initialize()
    return repository, registry, engine, now


@pytest.mark.asyncio
async def test_canonical_database_initialization_owns_challenge_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "schema.sqlite3")
    await database.init_db()
    async with database.get_db() as db:
        columns = {row["name"] for row in await db.fetchall("PRAGMA table_info(transfer_input_challenges)")}
    assert {"transfer_id", "challenge_id", "generation", "reason", "origin", "integration_id",
            "operation_id", "request_id", "artifact_id", "methods", "created_at", "updated_at"} <= columns


async def db_text():
    async with database.get_db() as db:
        names = [row["name"] for row in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'") if not row["name"].startswith("sqlite_")]
        payload = {}
        for name in names:
            payload[name] = await db.fetchall(f"SELECT * FROM {name}")
    return json.dumps(payload, sort_keys=True, default=str)


@pytest.mark.asyncio
async def test_provider_auth_wait_is_nonterminal_budget_neutral_and_same_transfer_continues(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("auth-parcel", "opaque-source", name="parcel.bin"),))
    await engine.tick()
    waiting = await repository.get(transfer.id)
    challenge = await engine.challenges.current(transfer.id)
    assert waiting.state == TransferState.INPUT_REQUIRED
    assert challenge.reason.value == "auth_required" and challenge.origin.value == "provider"
    assert (await repository.requests(transfer.id))[0].attempts == 0
    for _ in range(3):
        await engine.tick()
    assert provider.resolve_calls == 1
    assert not await repository.live_executions()
    await engine.submit_input(transfer.id, challenge.id, "username_password", {
        "username": "provider-user-sentinel", "password": "provider-password-sentinel"})
    await engine.tick()
    current = await repository.get(transfer.id)
    assert current.id == transfer.id and current.state == TransferState.TRANSFERRING
    artifact = (await repository.artifacts(transfer.id))[0]
    executor.finish(artifact.execution)
    await engine.tick()
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED
    assert await engine.challenges.current(transfer.id) is None


@pytest.mark.asyncio
async def test_rejected_provider_auth_supersedes_generation_and_stale_submission_is_rejected(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    transfer = await engine.submit((TransferRequest("auth-parcel", "opaque-source"),))
    await engine.tick()
    first = await engine.challenges.current(transfer.id)
    await engine.submit_input(transfer.id, first.id, "username_password", {"username": "wrong", "password": "wrong"})
    await engine.tick()
    second = await engine.challenges.current(transfer.id)
    assert second.id != first.id and second.generation == first.generation + 1
    assert (await repository.requests(transfer.id))[0].attempts == 0
    with pytest.raises(ValueError):
        await engine.submit_input(transfer.id, first.id, "username_password", {"username": "stale", "password": "stale"})


@pytest.mark.asyncio
async def test_private_key_passphrase_is_optional_in_challenge_and_unencrypted_key_continues_without_it(base):
    repository, registry, engine, _ = base
    registry.register_provider(StaticProvider())
    executor = KeyExecutor(repository.authorize_execution, encrypted=False)
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("key-parcel", "opaque-source"),))
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    descriptor = challenge.methods[0]
    fields = {field.name: field.required for field in descriptor.fields}
    assert descriptor.method == InputMethod.USERNAME_PRIVATE_KEY
    assert fields[InputField.USERNAME] is True and fields[InputField.PRIVATE_KEY] is True
    assert fields[InputField.PASSPHRASE] is False
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.retries == 0 and not await repository.live_executions()
    await engine.submit_input(transfer.id, challenge.id, "username_private_key", {
        "username": "executor-user-sentinel", "private_key": "executor-private-key-sentinel"})
    await engine.tick()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.execution is not None and artifact.retries == 1
    executor.finish(artifact.execution)
    await engine.tick()
    assert (await repository.get(transfer.id)).state == TransferState.COMPLETED


@pytest.mark.asyncio
async def test_executor_input_continuation_never_reaches_native_prepare_under_hold(base):
    """Gate 9 revision-2 rejection finding, specification section 7.3, 7.6:
    ``_continue_executor_input``'s ``prepare_with_input``/``start`` native
    calls are executor side effects exactly like ``_dispatch()``'s
    ``executor.prepare()`` -- they must stop under HOLD too, not only the
    ordinary dispatch path. HOLD is ordinary waiting state: the submitted
    input is not consumed, the challenge is not cleared, and a later PROCEED
    tick still completes normally."""
    repository, registry, engine, _ = base
    registry.register_provider(StaticProvider())
    executor = KeyExecutor(repository.authorize_execution, encrypted=False)
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("key-parcel", "opaque-source"),))
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    await engine.submit_input(transfer.id, challenge.id, "username_private_key", {
        "username": "executor-user-sentinel", "private_key": "executor-private-key-sentinel"})

    real_admission = repository.materialization_authorization
    monkeypatch_target = MaterializationAdmission(MaterializationAdmissionKind.HOLD, authority_generation="forced-generation")

    async def forced_hold(_artifact):
        return monkeypatch_target

    repository.materialization_authorization = forced_hold
    try:
        await engine.tick()
    finally:
        repository.materialization_authorization = real_admission

    assert executor.input_calls == 0, "prepare_with_input must never run while HOLD applies"
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.state == "input_required"
    assert artifact.execution is None
    # HOLD is ordinary waiting state -- the submitted input was not consumed
    # or discarded, and the same challenge is still current.
    assert await engine.challenges.current(transfer.id) == challenge

    # A later PROCEED tick (real admission restored) completes normally.
    await engine.tick()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.execution is not None and executor.input_calls == 1


@pytest.mark.asyncio
async def test_executor_input_continuation_retires_under_stale_instead_of_completing_native_prepare(base):
    """Gate 9 revision-2 rejection finding, specification section 7.5:
    STALE must retire the pending continuation through the same canonical
    machinery ``_dispatch()`` uses -- never let ``prepare_with_input``/
    ``start`` commit native work for a generation that is no longer
    current."""
    repository, registry, engine, _ = base
    registry.register_provider(StaticProvider())
    executor = KeyExecutor(repository.authorize_execution, encrypted=False)
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("key-parcel", "opaque-source"),))
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    await engine.submit_input(transfer.id, challenge.id, "username_private_key", {
        "username": "executor-user-sentinel", "private_key": "executor-private-key-sentinel"})

    async def forced_stale(_artifact):
        return MaterializationAdmission(MaterializationAdmissionKind.STALE, authority_generation="forced-generation")

    repository.materialization_authorization = forced_stale
    await engine.tick()

    assert executor.input_calls == 0, "prepare_with_input must never run for a STALE generation"
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.state == "unresolved"
    async with database.get_db() as db:
        row = await db.fetchone("SELECT state FROM transfer_requests WHERE id=?", (artifact.request_id,))
    assert row["state"] == "pending"


@pytest.mark.asyncio
async def test_encrypted_key_missing_passphrase_rechallenges_then_accepts_optional_passphrase(base):
    repository, registry, engine, _ = base
    registry.register_provider(StaticProvider())
    executor = KeyExecutor(repository.authorize_execution, encrypted=True)
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("key-parcel", "opaque-source"),))
    await engine.tick()
    first = await engine.challenges.current(transfer.id)
    await engine.submit_input(transfer.id, first.id, "username_private_key", {
        "username": "executor-user-sentinel", "private_key": "executor-private-key-sentinel"})
    await engine.tick()
    second = await engine.challenges.current(transfer.id)
    assert second.generation == first.generation + 1
    assert next(field for field in second.methods[0].fields if field.name == InputField.PASSPHRASE).required is False
    await engine.submit_input(transfer.id, second.id, "username_private_key", {
        "username": "executor-user-sentinel", "private_key": "executor-private-key-sentinel",
        "passphrase": "executor-passphrase-sentinel"})
    await engine.tick()
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.execution is not None


@pytest.mark.asyncio
async def test_pause_preserves_input_required_and_delays_transient_continuation(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    transfer = await engine.submit((TransferRequest("auth-parcel", "opaque-source"),))
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    await engine.pause(transfer.id)
    paused = await repository.get(transfer.id)
    assert paused.paused and paused.state == TransferState.INPUT_REQUIRED
    await engine.submit_input(transfer.id, challenge.id, "username_password", {
        "username": "provider-user-sentinel", "password": "provider-password-sentinel"})
    await engine.tick()
    assert provider.continuation_calls == 0
    await engine.resume(transfer.id)
    await engine.tick()
    assert provider.continuation_calls == 1


@pytest.mark.asyncio
async def test_restart_restores_challenge_but_not_submitted_values(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    transfer = await engine.submit((TransferRequest("auth-parcel", "opaque-source"),))
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    restarted = TransferEngine(TransferRepository(), registry, download_root=engine.root, policy=engine.policy, clock=engine.clock)
    await restarted.initialize()
    restored = await restarted.challenges.current(transfer.id)
    assert restored.id == challenge.id and (await restarted.repository.get(transfer.id)).state == TransferState.INPUT_REQUIRED
    await restarted.tick()
    assert provider.continuation_calls == 0
    await restarted.submit_input(transfer.id, restored.id, "username_password", {
        "username": "provider-user-sentinel", "password": "provider-password-sentinel"})
    await restarted.tick()
    assert provider.continuation_calls == 1


@pytest.mark.asyncio
async def test_delete_and_cancel_invalidate_waiting_challenge(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    deleted = await engine.submit((TransferRequest("auth-parcel", "delete-source"),), deduplicate=False)
    await engine.tick()
    old = await engine.challenges.current(deleted.id)
    await engine.delete(deleted.id, remote=False)
    assert await engine.challenges.current(deleted.id) is None
    with pytest.raises(ValueError):
        await engine.submit_input(deleted.id, old.id, "username_password", {"username": "x", "password": "y"})
    cancelled = await engine.submit((TransferRequest("auth-parcel", "cancel-source"),), deduplicate=False)
    await engine.tick()
    old2 = await engine.challenges.current(cancelled.id)
    await engine.cancel(cancelled.id)
    assert (await repository.get(cancelled.id)).state == TransferState.CANCELLED
    with pytest.raises(ValueError):
        await engine.submit_input(cancelled.id, old2.id, "username_password", {"username": "x", "password": "y"})


@pytest.mark.asyncio
async def test_duplicate_submission_rejected_and_transient_values_never_reach_persistence(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    transfer = await engine.submit((TransferRequest("auth-parcel", "opaque-source"),))
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    values = {"username": "provider-user-sentinel", "password": "provider-password-sentinel"}
    await engine.submit_input(transfer.id, challenge.id, "username_password", values)
    with pytest.raises(ValueError):
        await engine.submit_input(transfer.id, challenge.id, "username_password", values)
    encoded = await db_text()
    assert "provider-user-sentinel" not in encoded and "provider-password-sentinel" not in encoded
    submitted = await engine.inputs.take(challenge)
    assert "sentinel" not in repr(submitted)
    with pytest.raises(TypeError):
        codec.dump(submitted)
    submitted.discard()


@pytest.mark.asyncio
async def test_existing_sibling_execution_remains_observed_while_provider_challenge_waits(base):
    repository, registry, engine, _ = base

    class MixedProvider:
        def __init__(self):
            self.descriptor = IntegrationDescriptor("mixed", "Mixed", frozenset({Capability.RESOLVE}),
                                                    request_types=frozenset({"mixed"}))

        @property
        def applicability(self):
            return ProviderApplicability()

        async def resolve(self, request):
            if request.payload == "needs-auth":
                return ResolutionResult(ResourceState.UNKNOWN, input_required=auth_required(username_password()))
            return ResolutionResult(ResourceState.AVAILABLE, (
                TransferCandidate("running.bin", (Endpoint("memory", "memory:running"),), expected_bytes=4,
                                  provider_id=self.descriptor.id),))
        async def resolve_with_input(self, request, submitted):
            return ResolutionResult(ResourceState.AVAILABLE, (
                TransferCandidate("auth.bin", (Endpoint("memory", "memory:auth"),), expected_bytes=4,
                                  provider_id=self.descriptor.id),))

    provider = MixedProvider()
    executor = MemoryExecutor(repository.authorize_execution)
    registry.register_provider(provider)
    registry.register_executor(executor)
    transfer = await engine.submit((
        TransferRequest("mixed", "running", name="running.bin"),
        TransferRequest("mixed", "needs-auth", name="auth.bin"),
    ))
    records = await repository.requests(transfer.id)
    await engine._resolve(records[0])
    artifact = (await repository.artifacts(transfer.id))[0]
    await engine._dispatch(artifact)
    artifact = (await repository.artifacts(transfer.id))[0]
    assert artifact.execution is not None
    await engine._resolve(records[1])
    challenge = await engine.challenges.current(transfer.id)
    assert challenge and (await repository.get(transfer.id)).state == TransferState.INPUT_REQUIRED
    executor.finish(artifact.execution)
    before_observes = len([item for item in executor.calls if item[0] == "observe"])
    await engine.reconcile_executions()
    after_observes = len([item for item in executor.calls if item[0] == "observe"])
    refreshed = next(item for item in await repository.artifacts(transfer.id) if item.id == artifact.id)
    assert after_observes > before_observes
    assert refreshed.state == "completed"
    assert (await repository.get(transfer.id)).state == TransferState.INPUT_REQUIRED
    assert await engine.challenges.current(transfer.id) == challenge


@pytest.mark.asyncio
async def test_global_pause_preserves_wait_and_defers_submitted_input(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    transfer = await engine.submit((TransferRequest("auth-parcel", "opaque-source"),))
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    await engine.pause_all()
    assert await repository.globally_paused()
    assert (await repository.get(transfer.id)).state == TransferState.INPUT_REQUIRED
    await engine.submit_input(transfer.id, challenge.id, "username_password", {
        "username": "provider-user-sentinel", "password": "provider-password-sentinel"})
    await engine.tick()
    assert provider.continuation_calls == 0
    await engine.resume_all()
    await engine.tick()
    assert provider.continuation_calls == 1


@pytest.mark.asyncio
async def test_waiting_executor_input_uses_no_slot_and_submission_waits_for_capacity(base):
    repository, registry, engine, _ = base
    parcel = ParcelProvider()
    key_provider = StaticProvider()
    memory = MemoryExecutor(repository.authorize_execution)
    key_executor = KeyExecutor(repository.authorize_execution, encrypted=False)
    registry.register_provider(parcel)
    registry.register_provider(key_provider)
    registry.register_executor(memory)
    registry.register_executor(key_executor)

    occupying = await engine.submit((TransferRequest("parcel", "occupying", name="occupied.bin"),), deduplicate=False)
    await engine.tick()
    occupied_artifact = (await repository.artifacts(occupying.id))[0]
    assert occupied_artifact.execution is not None

    waiting = await engine.submit((TransferRequest("key-parcel", "needs-key", name="key.bin"),), deduplicate=False)
    await engine.tick()
    challenge = await engine.challenges.current(waiting.id)
    waiting_artifact = (await repository.artifacts(waiting.id))[0]
    assert challenge and waiting_artifact.execution is None and waiting_artifact.retries == 0
    assert len([item for item in await repository.live_executions() if item.state in {"prepared", "queued", "transferring", "unknown"}]) == 1

    await engine.submit_input(waiting.id, challenge.id, "username_private_key", {
        "username": "executor-user-sentinel", "private_key": "executor-private-key-sentinel"})
    await engine.tick()
    assert key_executor.input_calls == 0
    assert await engine.inputs.has(challenge)

    memory.finish(occupied_artifact.execution)
    await engine.tick()
    waiting_artifact = (await repository.artifacts(waiting.id))[0]
    assert key_executor.input_calls == 1
    assert waiting_artifact.execution is not None


@pytest.mark.asyncio
async def test_expired_transient_submission_is_discarded_without_changing_challenge(base):
    repository, registry, engine, now = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    transfer = await engine.submit((TransferRequest("auth-parcel", "opaque-source"),))
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    await engine.submit_input(transfer.id, challenge.id, "username_password", {
        "username": "provider-user-sentinel", "password": "provider-password-sentinel"})
    now[0] += 121
    await engine.tick()
    assert provider.continuation_calls == 0
    assert await engine.challenges.current(transfer.id) == challenge
    assert not await engine.inputs.has(challenge)
    await engine.submit_input(transfer.id, challenge.id, "username_password", {
        "username": "provider-user-sentinel", "password": "provider-password-sentinel"})
    await engine.tick()
    assert provider.continuation_calls == 1


def test_auth_challenge_accepts_alternatives_and_rejects_invalid_or_incomplete_submissions():
    requirement = auth_required(username_password(), username_private_key())
    challenge = InputChallenge("challenge", 7, 1, InputReason.AUTH_REQUIRED, InputOrigin.PROVIDER,
                               "neutral-provider", "operation", requirement.methods, request_id="request")
    password = validate_submission(challenge, "username_password", {"username": "user", "password": "secret"})
    assert password.method == InputMethod.USERNAME_PASSWORD
    password.discard()
    key = validate_submission(challenge, "username_private_key", {"username": "user", "private_key": "key"})
    assert key.method == InputMethod.USERNAME_PRIVATE_KEY and key.value(InputField.PASSPHRASE) is None
    key.discard()
    with pytest.raises(InputSubmissionRejected):
        validate_submission(challenge, "passphrase", {"passphrase": "secret"})
    with pytest.raises(InputSubmissionRejected):
        validate_submission(challenge, "username_password", {"username": "user"})
    with pytest.raises(InputSubmissionRejected):
        validate_submission(challenge, "username_password", {"username": "user", "password": "secret", "private_key": "extra"})


@pytest.mark.asyncio
async def test_concurrent_duplicate_submission_has_exactly_one_winner(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    transfer = await engine.submit((TransferRequest("auth-parcel", "opaque-source"),))
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)

    async def submit_once():
        try:
            await engine.submit_input(transfer.id, challenge.id, "username_password", {
                "username": "provider-user-sentinel", "password": "provider-password-sentinel"})
            return "accepted"
        except ValueError:
            return "rejected"

    results = await asyncio.gather(submit_once(), submit_once())
    assert sorted(results) == ["accepted", "rejected"]
    await engine.tick()
    assert provider.continuation_calls == 1


@pytest.mark.asyncio
async def test_challenge_scoping_prevents_cross_transfer_credential_use(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    first = await engine.submit((TransferRequest("auth-parcel", "one"),), deduplicate=False)
    second = await engine.submit((TransferRequest("auth-parcel", "two"),), deduplicate=False)
    await engine.tick()
    first_challenge = await engine.challenges.current(first.id)
    second_challenge = await engine.challenges.current(second.id)
    assert first_challenge.id != second_challenge.id
    with pytest.raises(ValueError):
        await engine.submit_input(second.id, first_challenge.id, "username_password", {
            "username": "first-only", "password": "first-only"})
    await engine.submit_input(first.id, first_challenge.id, "username_password", {
        "username": "provider-user-sentinel", "password": "provider-password-sentinel"})
    await engine.tick()
    assert await engine.challenges.current(first.id) is None
    assert await engine.challenges.current(second.id) == second_challenge


@pytest.mark.asyncio
async def test_cancelled_transfer_is_terminal_for_scheduler_inventory(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    transfer = await engine.submit((TransferRequest("auth-parcel", "cancel-me"),), deduplicate=False)
    await engine.tick()
    await engine.cancel(transfer.id)
    assert (await repository.get(transfer.id)).state == TransferState.CANCELLED
    assert transfer.id not in {item.id for item in await repository.active()}
    before = provider.resolve_calls + provider.continuation_calls
    for _ in range(3):
        await engine.tick()
    assert provider.resolve_calls + provider.continuation_calls == before


@pytest.mark.asyncio
async def test_provider_continuation_reacquires_resolution_capacity_before_consuming_secret(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    transfer = await engine.submit((TransferRequest("auth-parcel", "opaque-source"),))
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    await engine.submit_input(transfer.id, challenge.id, "username_password", {
        "username": "provider-user-sentinel", "password": "provider-password-sentinel"})

    await engine._resolution_slots.acquire()
    task = asyncio.create_task(engine.resolve_pending())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert provider.continuation_calls == 0
    assert await engine.inputs.has(challenge)
    engine._resolution_slots.release()
    await task
    assert provider.continuation_calls == 1


# ---------------------------------------------------------------------------
# DP 1.0.12 leveling remediation (FUNC-001): canonical settlement retires
# stale pause-intent / INPUT_REQUIRED side state. transition_allowed()
# intentionally keeps FAILED reopenable, so a settled generation's auxiliary
# state must be retired transactionally at settlement rather than relying on
# a later compensating read -- proven here for every side-state-retiring
# target and for the direct-UPDATE paths that intentionally bypass
# _write_lifecycle_transition (cancel_with_execution_cleanup, delete,
# CanonicalOwnership._finalize_transfer).
# ---------------------------------------------------------------------------


async def _aux_state_rows(transfer_id: int):
    async with database.get_db() as db:
        pause_rows = await db.fetchall(
            "SELECT * FROM transfer_pause_intents WHERE torrent_id=?", (transfer_id,))
        challenge_rows = await db.fetchall(
            "SELECT * FROM transfer_input_challenges WHERE transfer_id=?", (transfer_id,))
    return list(pause_rows), list(challenge_rows)


async def _challenged_transfer_with_stale_pause_intent(base):
    repository, registry, engine, _ = base
    provider = AuthParcelProvider()
    registry.register_provider(provider)
    registry.register_executor(MemoryExecutor(repository.authorize_execution))
    transfer = await engine.submit((TransferRequest("auth-parcel", "opaque-source"),))
    await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    assert challenge is not None
    await repository.pause_intent(transfer.id, True)
    pause_rows, challenge_rows = await _aux_state_rows(transfer.id)
    assert len(pause_rows) == 1 and len(challenge_rows) == 1
    return repository, engine, transfer, challenge


@pytest.mark.asyncio
async def test_completed_settlement_retires_pause_intent_and_input_challenge(base):
    repository, engine, transfer, challenge = await _challenged_transfer_with_stale_pause_intent(base)

    assert await repository.state(transfer.id, TransferState.COMPLETED, operator=True, verified=True)

    pause_rows, challenge_rows = await _aux_state_rows(transfer.id)
    assert pause_rows == [] and challenge_rows == []
    assert await engine.challenges.current(transfer.id) is None
    with pytest.raises(ValueError):
        await engine.submit_input(transfer.id, challenge.id, "username_password", {
            "username": "provider-user-sentinel", "password": "provider-password-sentinel"})


@pytest.mark.asyncio
async def test_permanent_failure_settlement_retires_pause_intent_and_input_challenge(base):
    """A permanent ``FAILED``/error settlement -- e.g. the recovery engine's
    FAIL_PERMANENTLY decision -- must retire stale side state exactly like
    every other settled target, even though FAILED remains operator-
    reopenable (transition_allowed)."""
    repository, engine, transfer, challenge = await _challenged_transfer_with_stale_pause_intent(base)

    error = NormalizedError(
        Domain.REQUEST, Category.CREDENTIAL_MISSING, Stage.QUEUE,
        retryability=Retryability.NEVER, origin=Origin.CORE,
    )
    assert await repository.state(transfer.id, TransferState.FAILED, error=error)

    pause_rows, challenge_rows = await _aux_state_rows(transfer.id)
    assert pause_rows == [] and challenge_rows == []
    assert await engine.challenges.current(transfer.id) is None
    with pytest.raises(ValueError):
        await engine.submit_input(transfer.id, challenge.id, "username_password", {
            "username": "provider-user-sentinel", "password": "provider-password-sentinel"})


@pytest.mark.asyncio
async def test_cancel_retires_pause_intent_in_the_same_transaction_as_settlement(base):
    """``TransferRepository.cancel_with_execution_cleanup`` intentionally
    bypasses ``_write_lifecycle_transition``, so it must invoke the same
    auxiliary-state retirement directly. Challenge retirement on cancel
    already had coverage (test_delete_and_cancel_invalidate_waiting_challenge);
    this proves the pause-intent side specifically."""
    repository, engine, transfer, _challenge = await _challenged_transfer_with_stale_pause_intent(base)

    assert await engine.cancel(transfer.id) == ()
    assert (await repository.get(transfer.id)).state == TransferState.CANCELLED

    pause_rows, challenge_rows = await _aux_state_rows(transfer.id)
    assert pause_rows == [] and challenge_rows == []


@pytest.mark.asyncio
async def test_delete_retires_pause_intent_and_input_challenge_in_the_same_transaction(base):
    """``TransferRepository.delete`` intentionally bypasses
    ``_write_lifecycle_transition``, so it must invoke the same
    auxiliary-state retirement directly."""
    repository, engine, transfer, challenge = await _challenged_transfer_with_stale_pause_intent(base)

    await engine.delete(transfer.id, remote=False)
    assert (await repository.get(transfer.id)).state == TransferState.DELETED

    pause_rows, challenge_rows = await _aux_state_rows(transfer.id)
    assert pause_rows == [] and challenge_rows == []
    with pytest.raises(ValueError):
        await engine.submit_input(transfer.id, challenge.id, "username_password", {
            "username": "provider-user-sentinel", "password": "provider-password-sentinel"})


@pytest.mark.asyncio
async def test_consolidated_settlement_retires_pause_intent_and_input_challenge(p2):
    """``CanonicalOwnership._finalize_transfer`` intentionally bypasses
    ``_write_lifecycle_transition`` too -- CONSOLIDATED was previously
    missing from the narrower INPUT_REQUIRED staleness set entirely (see
    ``input_required.SIDE_STATE_RETIRING_TRANSFER_STATES``), so this is also
    the regression proof for that specific gap."""
    canonical_transfer = await admit(p2, p2.a, "submission-a")
    await p2.engine.resolve_pending()
    source_transfer = await admit(p2, p2.b, "submission-b")

    # A pause intent is deliberately NOT seeded here: pause blocks resolution
    # admission (_live()), which would prevent this second resolve_pending
    # from ever reaching consolidation at all. Pause-intent retirement on
    # settlement is already proven generically by the COMPLETED/FAILED
    # (_write_lifecycle_transition) and CANCELLED/DELETED (direct-UPDATE
    # bypass) cases above; this test's unique value is proving the THIRD
    # bypass site (CanonicalOwnership._finalize_transfer) and the CONSOLIDATED
    # gap specifically, for which the challenge alone is sufficient proof.
    async with database.get_db() as db:
        await db.execute(
            "INSERT INTO transfer_input_challenges"
            "(transfer_id, challenge_id, generation, reason, origin, integration_id, operation_id, methods, created_at, updated_at) "
            "VALUES (?, 'chal-stale', 1, 'auth_required', 'provider', 'provider-b', 'op-stale', '[]', 0, 0)",
            (source_transfer.id,),
        )
        await db.commit()

    await p2.engine.resolve_pending()

    assert (await p2.repository.get(source_transfer.id)).state == TransferState.CONSOLIDATED
    assert canonical_transfer.id != source_transfer.id
    _pause_rows, challenge_rows = await _aux_state_rows(source_transfer.id)
    assert challenge_rows == []


@pytest.mark.asyncio
async def test_operator_reopen_after_settlement_does_not_resurrect_prior_generation_side_state(base):
    """A settled transfer's retired challenge is not resurrected by reopening
    it, a stale submission against the retired challenge id is rejected, and
    a fresh challenge in the new generation behaves like any other challenge."""
    repository, engine, transfer, stale_challenge = await _challenged_transfer_with_stale_pause_intent(base)

    assert await repository.state(transfer.id, TransferState.COMPLETED, operator=True, verified=True)
    assert await engine.challenges.current(transfer.id) is None

    # Operator reopen: transition_allowed() permits COMPLETED -> ACCEPTED
    # under operator authority. The old challenge/pause intent were already
    # retired at settlement above, so reopening must not resurrect them.
    assert await repository.state(transfer.id, TransferState.ACCEPTED, operator=True)
    assert await engine.challenges.current(transfer.id) is None

    with pytest.raises(ValueError):
        await engine.submit_input(transfer.id, stale_challenge.id, "username_password", {
            "username": "provider-user-sentinel", "password": "provider-password-sentinel"})

    # Force the new lifecycle generation's request back to resolvable (the
    # same durable primitive a real reopen/re-resolve path uses per-request)
    # and prove a fresh challenge in the new generation behaves normally.
    record = (await repository.requests(transfer.id))[0]
    await repository.retry_requests(transfer.id, request_id=record.id, reset_budget=True)
    await engine.tick()
    fresh = await engine.challenges.current(transfer.id)
    assert fresh is not None and fresh.id != stale_challenge.id
    await engine.submit_input(transfer.id, fresh.id, "username_password", {
        "username": "provider-user-sentinel", "password": "provider-password-sentinel"})
    await engine.tick()
    assert (await repository.get(transfer.id)).state == TransferState.TRANSFERRING


# --------------------------------------------------------------------------- #
# 1.0.13: neutral SERVER_IDENTITY_REQUIRED reason with durable non-secret facts.
# One challenge carries the identity facts AND the credential method; facts are
# durable, public and generation-fenced; values stay transient.
# --------------------------------------------------------------------------- #

from transfers.input_required import public_challenge, server_identity_required  # noqa: E402
from transfers.models import ExecutionObservation, ExecutionState, InputFact, InputFactName, InputRequirement  # noqa: E402

IDENTITY_FACTS = (
    InputFact(InputFactName.SERVER_HOST, "server.neutral.example"),
    InputFact(InputFactName.SERVER_IDENTITY_ALGORITHM, "sha-1"),
    InputFact(InputFactName.SERVER_IDENTITY_FINGERPRINT, "ab" * 20),
)


class IdentityExecutor(MemoryExecutor):
    """Neutral executor whose first start reports a server-identity requirement."""

    def __init__(self, authorize):
        super().__init__(authorize)
        self.descriptor = IntegrationDescriptor("identity-copy", "Identity copy",
                                                frozenset({Capability.PAUSE, Capability.RESUME, Capability.RECONCILE}),
                                                schemes=frozenset({"keymem"}))
        self.start_errors = [NormalizedError(Domain.EXECUTOR, Category.UNMAPPED_EXECUTOR_ERROR, Stage.EXECUTION,
                                             native_code="neutral-identity")]
        self.continued = []

    def input_requirement(self, candidate, observation):
        if observation.state == ExecutionState.FAILED and observation.error and observation.error.native_code == "neutral-identity":
            return server_identity_required(username_password(), host="server.neutral.example",
                                            algorithm="sha-1", fingerprint="ab" * 20)
        return None

    async def start_with_input(self, request, handle, submitted):
        self.continued.append((submitted.method, dict((fact.name, fact.value) for fact in submitted.facts),
                               submitted.value(InputField.USERNAME), submitted.secret_values()))
        result = ExecutionObservation(handle, ExecutionState.TRANSFERRING)
        self.jobs[handle.attempt_id] = result
        return result


def test_server_identity_requirement_is_a_neutral_reason_with_exact_facts():
    requirement = server_identity_required(username_password(), host="server.neutral.example",
                                           algorithm="sha-1", fingerprint="ab" * 20)
    assert requirement.reason == InputReason.SERVER_IDENTITY_REQUIRED
    assert requirement.reason.value == "server_identity_required"
    assert requirement.facts == IDENTITY_FACTS
    assert set(InputReason) == {InputReason.AUTH_REQUIRED, InputReason.SERVER_IDENTITY_REQUIRED}
    assert not any("sftp" in item.value or "ssh" in item.value or "ftp" in item.value for item in InputReason)
    assert not any("sftp" in item.value or "ssh" in item.value for item in InputFactName)


@pytest.mark.parametrize("facts", [
    (),
    IDENTITY_FACTS[:2],
    IDENTITY_FACTS + (IDENTITY_FACTS[0],),
])
def test_server_identity_requirement_rejects_missing_or_duplicate_facts(facts):
    with pytest.raises(ValueError):
        InputRequirement(InputReason.SERVER_IDENTITY_REQUIRED, (username_password(),), facts)


def test_auth_requirement_carries_no_facts_and_facts_are_bounded_text():
    with pytest.raises(ValueError):
        InputRequirement(InputReason.AUTH_REQUIRED, (username_password(),), IDENTITY_FACTS)
    for bad in ("", "x" * 1025, "line\nbreak", "nul\x00"):
        with pytest.raises(ValueError):
            InputFact(InputFactName.SERVER_HOST, bad)
    with pytest.raises((TypeError, ValueError)):
        InputFact("server_host", "server.neutral.example")


def test_submitted_input_exposes_challenge_facts_without_treating_them_as_secrets():
    requirement = server_identity_required(username_password(), host="server.neutral.example",
                                           algorithm="sha-1", fingerprint="ab" * 20)
    challenge = InputChallenge("challenge", 7, 3, requirement.reason, InputOrigin.EXECUTOR, "neutral", "operation",
                               requirement.methods, facts=requirement.facts)
    submitted = validate_submission(challenge, "username_password", {"username": "user", "password": "secret"})
    assert submitted.facts == IDENTITY_FACTS
    assert submitted.secret_values() == ("user", "secret")
    assert "ab" * 20 not in submitted.secret_values()
    with pytest.raises(TypeError):
        codec.dump(submitted)
    submitted.discard()
    assert submitted.facts == IDENTITY_FACTS and submitted.secret_values() == ()


@pytest.mark.asyncio
async def test_canonical_schema_owns_one_nonsecret_facts_column(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "facts.sqlite3")
    await database.init_db()
    async with database.get_db() as db:
        tables = [row["name"] for row in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%challenge%'")]
        columns = {row["name"] for row in await db.fetchall("PRAGMA table_info(transfer_input_challenges)")}
    assert tables == ["transfer_input_challenges"]
    assert "facts" in columns and "facts" in database._INPUT_CHALLENGE_COLUMNS


@pytest.mark.asyncio
async def test_pre_facts_challenge_table_is_migrated_in_place(tmp_path, monkeypatch):
    import aiosqlite

    path = tmp_path / "legacy.sqlite3"
    async with aiosqlite.connect(path) as legacy:
        await legacy.execute("""CREATE TABLE transfer_input_challenges (
            transfer_id INTEGER PRIMARY KEY, challenge_id TEXT NOT NULL UNIQUE,
            generation INTEGER NOT NULL CHECK(generation > 0), reason TEXT NOT NULL, origin TEXT NOT NULL,
            integration_id TEXT NOT NULL, operation_id TEXT NOT NULL, request_id TEXT, artifact_id INTEGER,
            methods TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        await legacy.commit()
    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()
    async with database.get_db() as db:
        columns = {row["name"] for row in await db.fetchall("PRAGMA table_info(transfer_input_challenges)")}
    assert "facts" in columns


@pytest.mark.asyncio
async def test_server_identity_challenge_is_durable_public_fenced_and_continues_with_facts(base):
    repository, registry, engine, _ = base
    registry.register_provider(StaticProvider())
    executor = IdentityExecutor(repository.authorize_execution)
    registry.register_executor(executor)
    transfer = await engine.submit((TransferRequest("key-parcel", "opaque-source"),))
    for _ in range(3):
        await engine.tick()
    challenge = await engine.challenges.current(transfer.id)
    assert challenge.reason == InputReason.SERVER_IDENTITY_REQUIRED
    assert challenge.origin == InputOrigin.EXECUTOR
    assert challenge.facts == IDENTITY_FACTS
    assert (await repository.get(transfer.id)).state == TransferState.INPUT_REQUIRED

    public = public_challenge(challenge)
    assert public["reason"] == "server_identity_required"
    assert public["facts"] == [
        {"name": "server_host", "value": "server.neutral.example"},
        {"name": "server_identity_algorithm", "value": "sha-1"},
        {"name": "server_identity_fingerprint", "value": "ab" * 20},
    ]
    detail = await repository.presentation(transfer.id, details=True)
    assert detail["input_required"]["facts"] == public["facts"]

    # Facts survive restart while pending.
    restarted = TransferEngine(TransferRepository(), registry, download_root=engine.root, policy=engine.policy, clock=engine.clock)
    await restarted.initialize()
    restored = await restarted.challenges.current(transfer.id)
    assert restored.facts == IDENTITY_FACTS and restored.id == challenge.id

    # A stale challenge identity is still rejected.
    with pytest.raises(ValueError):
        await engine.submit_input(transfer.id, "stale-" + challenge.id, "username_password",
                                  {"username": "identity-user-sentinel", "password": "identity-password-sentinel"})
    await engine.submit_input(transfer.id, restored.id, "username_password",
                              {"username": "identity-user-sentinel", "password": "identity-password-sentinel"})
    encoded = await db_text()
    assert "identity-user-sentinel" not in encoded and "identity-password-sentinel" not in encoded
    await engine.tick()
    assert executor.continued and executor.continued[0][0] == InputMethod.USERNAME_PASSWORD
    assert executor.continued[0][1] == {fact.name: fact.value for fact in IDENTITY_FACTS}
    assert executor.continued[0][2] == "identity-user-sentinel"
    assert "ab" * 20 not in executor.continued[0][3]
    async with database.get_db() as db:
        events = await db.fetchall("SELECT detail FROM application_events WHERE transfer_id=? AND kind='input_required'", (transfer.id,))
        messages = await db.fetchall("SELECT message FROM events WHERE torrent_id=?", (transfer.id,))
    assert [row["detail"] for row in events] == ["server_identity_required"]
    assert any("server identity" in row["message"].lower() for row in messages)


@pytest.mark.asyncio
async def test_replacement_generation_rewrites_facts_and_reason_together(base):
    repository, registry, engine, _ = base
    registry.register_provider(StaticProvider())
    registry.register_executor(IdentityExecutor(repository.authorize_execution))
    transfer = await engine.submit((TransferRequest("key-parcel", "opaque-source"),))
    for _ in range(3):
        await engine.tick()
    first = await engine.challenges.current(transfer.id)
    replaced = await engine.challenges.replace(first, auth_required(username_password()))
    assert replaced.generation == first.generation + 1 and replaced.facts == ()
    current = await engine.challenges.current(transfer.id)
    assert current.reason == InputReason.AUTH_REQUIRED and current.facts == ()
