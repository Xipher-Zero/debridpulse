"""Neutral INPUT_REQUIRED/AUTH_REQUIRED lifecycle proof with unrelated integrations."""
import json
from dataclasses import replace

import pytest
import pytest_asyncio

import db.database as database
from fake_integrations import MemoryExecutor
from transfers import codec
from transfers.engine import TransferEngine
from transfers.input_required import auth_required, username_password, username_private_key
from transfers.models import (
    Capability, Endpoint, ExecutionHandle, ExecutionObservation, ExecutionRequest, ExecutionState,
    InputField, InputMethod, IntegrationDescriptor, ResourceState, ResolutionResult,
    TransferCandidate, TransferProgress, TransferRequest, TransferState,
)
from transfers.policy import TransferPolicy
from transfers.registry import IntegrationRegistry
from transfers.repository import TransferRepository


class AuthParcelProvider:
    def __init__(self):
        self.descriptor = IntegrationDescriptor("auth-parcel", "Auth parcel", frozenset({Capability.RESOLVE}),
                                                request_types=frozenset({"auth-parcel"}))
        self.resolve_calls = 0
        self.continuation_calls = 0

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
