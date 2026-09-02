from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_one(path, old, new):
    p = ROOT / path
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected one match in {path}, found {count}: {old[:120]!r}")
    p.write_text(text.replace(old, new, 1))


# Existing external executions must remain observable while one sibling path is
# INPUT_REQUIRED. The challenge blocks only new dispatch/refresh and separately
# controls its own executor continuation.
replace_one(
    "backend/transfers/engine.py",
    '''            grouped = {}\n            for transfer in transfers:\n                if challenges[transfer.id]:\n                    continue\n                for artifact in artifacts_by_transfer[transfer.id]:\n                    if artifact.execution and artifact.state in {"queued", "downloading", "unknown", "verifying", "paused"}:\n                        grouped.setdefault(artifact.execution.executor_id, []).append(artifact.execution)\n''',
    '''            grouped = {}\n            for transfer in transfers:\n                for artifact in artifacts_by_transfer[transfer.id]:\n                    if artifact.execution and artifact.state in {"queued", "downloading", "unknown", "verifying", "paused"}:\n                        grouped.setdefault(artifact.execution.executor_id, []).append(artifact.execution)\n''',
)
replace_one(
    "backend/transfers/engine.py",
    '''            for transfer in transfers:\n                challenge = challenges[transfer.id]\n                if challenge:\n                    if challenge.origin == InputOrigin.EXECUTOR and await self._live(transfer.id, admission=True):\n                        await self._continue_executor_input(challenge, artifacts_by_transfer[transfer.id])\n                    continue\n                await self._process_executions(transfer.id, artifacts_by_transfer[transfer.id], observations)\n\n    async def _process_executions(self, transfer_id, artifacts, observations):\n''',
    '''            for transfer in transfers:\n                challenge = challenges[transfer.id]\n                await self._process_executions(transfer.id, artifacts_by_transfer[transfer.id], observations,\n                                               dispatch_allowed=challenge is None)\n                if challenge and challenge.origin == InputOrigin.EXECUTOR and await self._live(transfer.id, admission=True):\n                    await self._continue_executor_input(challenge, await self.repository.artifacts(transfer.id))\n\n    async def _process_executions(self, transfer_id, artifacts, observations, *, dispatch_allowed=True):\n''',
)
replace_one(
    "backend/transfers/engine.py",
    '''                elif artifact.state == "queued" and artifact.retry_at <= self.clock():\n                    await self._dispatch(artifact)\n                elif artifact.state == "refresh_pending" and artifact.retry_at <= self.clock():\n                    await self._refresh(artifact)\n''',
    '''                elif dispatch_allowed and artifact.state == "queued" and artifact.retry_at <= self.clock():\n                    await self._dispatch(artifact)\n                elif dispatch_allowed and artifact.state == "refresh_pending" and artifact.retry_at <= self.clock():\n                    await self._refresh(artifact)\n''',
)

# CANCELLED is terminal for scheduling. Explicit operator retry may still
# requeue it through the ordinary retry command, but background work cannot.
replace_one(
    "backend/transfers/repository.py",
    "                WHERE t.status NOT IN ('completed','deleted')\n",
    "                WHERE t.status NOT IN ('completed','deleted','cancelled')\n",
)
replace_one(
    "backend/transfers/policy.py",
    "_TERMINAL = {TransferState.COMPLETED, TransferState.DELETED}\n",
    "_TERMINAL = {TransferState.COMPLETED, TransferState.DELETED, TransferState.CANCELLED}\n",
)
replace_one(
    "backend/transfers/policy.py",
    '''    if current == TransferState.COMPLETED:\n        return target == TransferState.DELETED or (operator and target in {TransferState.ACCEPTED, TransferState.QUEUED})\n    if target == TransferState.DELETED:\n''',
    '''    if current == TransferState.COMPLETED:\n        return target == TransferState.DELETED or (operator and target in {TransferState.ACCEPTED, TransferState.QUEUED})\n    if current == TransferState.CANCELLED:\n        return target == TransferState.DELETED or (operator and target in {TransferState.ACCEPTED, TransferState.QUEUED})\n    if target == TransferState.DELETED:\n''',
)

# Expand the deterministic neutral lifecycle proofs.
p = ROOT / "backend/tests/test_input_required_lifecycle.py"
text = p.read_text()
text = text.replace("import json\n", "import asyncio\nimport json\n", 1)
text = text.replace(
    "from fake_integrations import MemoryExecutor\n",
    "from fake_integrations import MemoryExecutor, ParcelProvider\n",
    1,
)
text = text.replace(
    "from transfers.input_required import auth_required, username_password, username_private_key\n",
    "from transfers.input_required import (InputSubmissionRejected, auth_required, username_password, username_private_key, validate_submission)\n",
    1,
)
text = text.replace(
    "    InputField, InputMethod, IntegrationDescriptor, ResourceState, ResolutionResult,\n",
    "    InputChallenge, InputField, InputMethod, InputOrigin, InputReason, IntegrationDescriptor, ResourceState, ResolutionResult,\n",
    1,
)
append = r'''

@pytest.mark.asyncio
async def test_existing_sibling_execution_remains_observed_while_provider_challenge_waits(base):
    repository, registry, engine, _ = base

    class MixedProvider:
        def __init__(self):
            self.descriptor = IntegrationDescriptor("mixed", "Mixed", frozenset({Capability.RESOLVE}),
                                                    request_types=frozenset({"mixed"}))
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
'''
p.write_text(text + append)

print("Item 3 scheduling/control hardening applied")
