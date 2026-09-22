"""Executor applicability is one core-owned claim router over a pre-materialization subject.

Viability, pre-writer evidence sampling, evidence-input continuation, dispatch
and executor-input continuation all select an executor through
``IntegrationRegistry.claimants(ExecutionSubject)`` -- never by URL-scheme
intersection. Proven with executors that share nothing with aria2.
"""
from __future__ import annotations

from dataclasses import fields, replace
import inspect

import pytest
import pytest_asyncio

import db.database as database
from executor_fakes import LedgerExecutor, LedgerProvider, artifact_of, ledger_capabilities, ledger_core, submit_ledger
from fake_integrations import VaultExecutor, VaultProvider
from transfers import mirrors
from transfers.convergence_engine import TransferEngine
from transfers.input_required import InputSubmissionRejected
from transfers.models import (
    Endpoint, ExecutionSubject, ExecutorClaim, IntegrationDescriptor, TransferCandidate, TransferRequest,
    TransferState,
)
from transfers.policy import TransferPolicy
from transfers.recovery_repository import TransferRepository
from transfers.registry import IntegrationRegistry

pytestmark = pytest.mark.asyncio

CONTENT = b"claim-router-payload" * 64
USER, PASSWORD = "claim-user-sentinel", "claim-password-sentinel"


async def _allow(_handle, _action):
    return True


def _subject(kind="ledger", *, endpoints=()):
    return ExecutionSubject(kind, TransferCandidate("item.bin", tuple(endpoints), expected_bytes=4))


async def test_executor_registration_does_not_require_schemes():
    assert "schemes" not in {item.name for item in fields(IntegrationDescriptor)}
    registry = IntegrationRegistry()
    executor = LedgerExecutor(_allow)
    registry.register_executor(executor)
    assert registry.executors == {"ledger-copy": executor}


async def test_executor_selection_uses_subject_claim_not_endpoint_scheme_intersection():
    registry = IntegrationRegistry()
    ledger = LedgerExecutor(_allow)
    registry.register_executor(ledger)
    # An endpoint whose scheme spells the executor's name proves nothing: the
    # claim is over canonical subject facts the executor interprets privately.
    decoy = _subject("tome", endpoints=(Endpoint("ledger", "ledger://item"),))
    assert registry.claimants(decoy) == ()
    assert registry.claimants(_subject("ledger")) == (ledger,)
    assert not hasattr(registry, "eligible_executors")
    assert not hasattr(registry, "executor_for")


async def test_executor_claim_is_pure_and_core_owns_priority_order():
    registry = IntegrationRegistry()
    low = LedgerExecutor(_allow, identity="a-low", priority=1)
    high = LedgerExecutor(_allow, identity="z-high", priority=9)
    tie = LedgerExecutor(_allow, identity="b-tie", priority=1)
    for executor in (low, high, tie):
        registry.register_executor(executor)
    subject = _subject()
    first = registry.claimants(subject)
    assert first == registry.claimants(subject) == (high, low, tie)
    assert registry.executor_for_subject(subject) is high
    assert all(item.jobs == {} and item.calls == [] for item in (low, high, tie))


async def test_two_executors_can_claim_same_subject_and_core_selects_by_neutral_priority():
    registry = IntegrationRegistry()
    first = LedgerExecutor(_allow, identity="first", priority=0)
    second = LedgerExecutor(_allow, identity="second", priority=0)
    registry.register_executor(second)
    registry.register_executor(first)
    assert registry.executor_for_subject(_subject()) is first  # equal priority -> deterministic identity order
    second.descriptor = replace(second.descriptor, priority=3)
    assert registry.executor_for_subject(_subject()) is second


async def test_executor_claim_cannot_select_itself_or_mutate_core_policy():
    assert [item.name for item in fields(ExecutorClaim)] == ["supported"]

    class Greedy(LedgerExecutor):
        def claim(self, subject):
            return {"supported": True, "priority": 10_000, "executor": self.descriptor.id}

    class Tuple(LedgerExecutor):
        def claim(self, subject):
            return (True, 10_000)

    registry = IntegrationRegistry()
    honest = LedgerExecutor(_allow, identity="honest")
    for executor in (Greedy(_allow, identity="greedy", priority=50), Tuple(_allow, identity="tuple", priority=40),
                     honest):
        registry.register_executor(executor)
    assert registry.claimants(_subject()) == (honest,)
    assert registry.executors["greedy"].descriptor.priority == 50  # claiming never rewrites core ordering


async def test_subject_claim_is_available_before_materialization_plan_exists():
    assert [item.name for item in fields(ExecutionSubject)] == ["request_kind", "candidate"]
    registry = IntegrationRegistry()
    executor = LedgerExecutor(_allow)
    registry.register_executor(executor)
    candidate = TransferCandidate("item.bin", (), expected_bytes=4, request_kind="ledger")
    assert ExecutionSubject.of(candidate) == ExecutionSubject("ledger", candidate)
    assert registry.executor_for_subject(ExecutionSubject.of(candidate)) is executor


async def test_candidate_without_endpoint_can_be_claimed_by_non_scheme_executor(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer = await submit_ledger(core, "ledger-item")
    artifact = await artifact_of(core, transfer.id)
    candidate = artifact.candidates[artifact.selected]
    assert candidate.endpoints == () and candidate.request_kind == "ledger"
    assert artifact.execution is not None and artifact.execution.executor_id == "ledger-copy"
    assert [call for call in core.executor.calls if call[0] == "start"]


async def test_prewriter_sampling_uses_same_subject_claim_router_as_dispatch(tmp_path, monkeypatch):
    def executors(authorize):
        return (
            LedgerExecutor(authorize, identity="sampler-low", priority=1,
                           capabilities=ledger_capabilities(candidate_sampling=True)),
            LedgerExecutor(authorize, identity="sampler-high", priority=7,
                           capabilities=ledger_capabilities(candidate_sampling=True)),
        )

    core = await ledger_core(tmp_path, monkeypatch, executors=executors)
    low, high = core.executors
    candidate = TransferCandidate("item.bin", (), expected_bytes=4, request_kind="ledger")
    evidence = await mirrors.self_evidence(candidate, core.registry)
    assert evidence.total_bytes == 4
    assert high.samples and not low.samples  # sampled by the claimant core selected
    transfer = await submit_ledger(core, "ledger-item")
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution.executor_id == "sampler-high"  # dispatch selected the same claimant
    source = inspect.getsource(mirrors)
    assert "executor_for(" not in source and "executor_for_subject(" in source


async def test_evidence_target_does_not_use_legacy_scheme_router():
    from transfers import _engine_base
    source = inspect.getsource(_engine_base.TransferEngine._evidence_target)
    assert "executor_for_subject" in source
    assert "eligible_executors" not in source and "descriptor.schemes" not in source


@pytest_asyncio.fixture
async def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fence.sqlite3")
    await database.init_db()
    now = [1000.0]
    repository = TransferRepository()
    registry = IntegrationRegistry()
    engine = TransferEngine(repository, registry, download_root=str(tmp_path / "payloads"),
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0, max_active_executions=5),
                            clock=lambda: now[0])
    await engine.initialize()
    objects = {"open.example/payload.bin": CONTENT, "locked.example/payload.bin": CONTENT}
    locks = {"locked.example": (USER, PASSWORD)}
    executor = VaultExecutor(repository.authorize_execution, objects=objects, locks=locks)
    registry.register_provider(VaultProvider())
    registry.register_executor(executor)
    seed = await engine.submit((TransferRequest("vault", "open.example/payload.bin", name="payload.bin"),),
                               deduplicate=False)
    for _ in range(3):
        await engine.tick()
    assert len(await repository.artifacts(seed.id)) == 1
    return repository, registry, engine, executor, objects, locks


async def _challenge_for(engine, transfer_id):
    for _ in range(4):
        await engine.tick()
    challenge = await engine.challenges.current(transfer_id)
    assert challenge is not None
    return challenge


async def test_evidence_input_continuation_is_fenced_to_exact_executor_identity(vault):
    repository, registry, engine, owner, objects, locks = vault
    incoming = await engine.submit((TransferRequest("vault", "locked.example/payload.bin", name="payload.bin"),),
                                   deduplicate=False)
    challenge = await _challenge_for(engine, incoming.id)
    assert challenge.origin.value == "evidence" and challenge.integration_id == "vault-copy"
    await engine.submit_input(incoming.id, challenge.id, "username_password", {"username": USER, "password": PASSWORD})

    # After the input was accepted, a DIFFERENT executor becomes the selected
    # claimant for the same subject (higher neutral priority).
    successor = VaultExecutor(repository.authorize_execution, objects=objects, locks=locks)
    successor.descriptor = replace(successor.descriptor, id="vault-successor", priority=10)
    registry.register_executor(successor)
    await engine.resolve_pending()

    # The submitted credential never crossed to the successor, nor was it used
    # by the former owner once it stopped being the selected claimant.
    assert all(username != USER for _candidate, username in successor.samples)
    assert all(username != USER for _candidate, username in owner.samples)
    assert successor.input_starts == [] and owner.input_starts == []
    assert await repository.artifacts(incoming.id) == ()
    record = (await repository.requests(incoming.id))[0]
    assert await engine.inputs.take_handoff(incoming.id, record.id, challenge.operation_id, "vault-successor") is None

    # Ordinary routing re-entered: the new claimant asks through the one
    # INPUT_REQUIRED lifecycle under ITS identity.
    for _ in range(3):
        await engine.tick()
    current = await engine.challenges.current(incoming.id)
    assert current is not None and current.id != challenge.id
    assert current.integration_id == "vault-successor" and current.origin.value == "evidence"
    with pytest.raises(InputSubmissionRejected):
        await engine.submit_input(incoming.id, challenge.id, "username_password",
                                  {"username": USER, "password": PASSWORD})


async def test_route_change_discards_executor_specific_input_handoff(vault):
    repository, registry, engine, owner, objects, locks = vault
    objects["locked.example/solo.bin"] = b"distinct-bytes" * 50
    incoming = await engine.submit((TransferRequest("vault", "locked.example/solo.bin", name="payload.bin"),),
                                   deduplicate=False)
    challenge = await _challenge_for(engine, incoming.id)
    await engine.submit_input(incoming.id, challenge.id, "username_password", {"username": USER, "password": PASSWORD})
    await engine.resolve_pending()  # evidence proves distinct bytes -> writer admitted, input handed to the owner
    record = (await repository.requests(incoming.id))[0]

    successor = VaultExecutor(repository.authorize_execution, objects=objects, locks=locks)
    successor.descriptor = replace(successor.descriptor, id="vault-successor", priority=10)
    registry.register_executor(successor)
    await engine.reconcile_executions()  # dispatch now routes to the successor

    assert successor.input_starts == [] and owner.input_starts == []
    # The owner-bound handoff was discarded when a different claimant was admitted.
    assert await engine.inputs.take_handoff(incoming.id, record.id, challenge.operation_id, "vault-copy") is None


async def test_viable_path_and_dispatch_use_same_generalized_claim_mechanism(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer = await core.engine.submit((TransferRequest("tome", "unclaimed-item", name="unclaimed"),))
    await core.engine.resolve_pending()
    assert not await core.engine._has_viable_path(transfer.id)  # no claimant for a "tome" subject
    core.executor.kinds = frozenset({"ledger", "tome"})
    assert await core.engine._has_viable_path(transfer.id)
    await core.engine.reconcile_executions()
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution is not None and artifact.execution.executor_id == "ledger-copy"
    from transfers import _engine_base
    for method in (_engine_base.TransferEngine._has_viable_path, _engine_base.TransferEngine._dispatch):
        source = inspect.getsource(method)
        assert "executor_for_subject" in source or "claimants" in source
