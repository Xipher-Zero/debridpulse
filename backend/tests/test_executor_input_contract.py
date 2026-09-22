"""Executor preparation and pre-writer sampling share the ONE INPUT_REQUIRED lifecycle.

``transient_input`` / ``candidate_sampling`` only declare participation in the
existing ``InputRequirement`` / ``InputChallengeStore`` /
``EphemeralInputBroker`` machinery; no executor gets a second challenge store
or secret broker, and a submitted secret dies with the executor call it was
lent to.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import db.database as database
from executor_fakes import LedgerExecutor, LedgerProvider, artifact_of, ledger_capabilities, ledger_core
from transfers.input_required import auth_required, username_password
from transfers.models import InputField, InputMethod, TransferRequest, TransferState

pytestmark = pytest.mark.asyncio

BACKEND = Path(__file__).resolve().parents[1]
USER, PASSWORD = "ledger-user-sentinel", "ledger-password-sentinel"


class AskingLedger(LedgerExecutor):
    """Needs operator input to prepare, and to sample, one ledger subject."""

    def __init__(self, authorize, **kwargs):
        super().__init__(authorize, capabilities=ledger_capabilities(transient_input=True, candidate_sampling=True),
                         **kwargs)
        self.lent = []

    def prepare(self, request):
        return auth_required(username_password())

    def prepare_with_input(self, request, submitted):
        self.lent.append(submitted)
        assert submitted.value(InputField.USERNAME) == USER
        return super().prepare(request)

    def input_requirement(self, candidate, observation):
        return None

    async def start_with_input(self, request, handle, submitted):
        self.lent.append(submitted)
        return await self.start(request, handle)

    async def fingerprint(self, subject):
        return auth_required(username_password())

    async def fingerprint_with_input(self, subject, submitted):
        self.lent.append(submitted)
        return await super().fingerprint(subject)


async def _challenge_tables():
    async with database.get_db() as db:
        return {row["name"] for row in await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE '%challenge%' OR name LIKE '%secret%'"
            " OR name LIKE '%input%')")}


async def test_executor_prepare_input_uses_existing_single_input_required_store(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch, executors=lambda authorize: (AskingLedger(authorize),),
                             providers=(LedgerProvider(input_methods=(InputMethod.USERNAME_PASSWORD,)),))
    transfer = await core.engine.submit((TransferRequest("ledger", "item", name="item"),))
    for _ in range(3):
        await core.engine.tick()
    challenge = await core.engine.challenges.current(transfer.id)
    assert challenge is not None and challenge.origin.value == "executor"
    assert challenge.integration_id == "ledger-copy"
    assert await _challenge_tables() == {"transfer_input_challenges"}
    await core.engine.submit_input(transfer.id, challenge.id, "username_password",
                                   {"username": USER, "password": PASSWORD})
    await core.engine.reconcile_executions()
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution is not None
    assert await core.engine.challenges.current(transfer.id) is None


async def test_candidate_sampling_input_uses_existing_single_input_required_store(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch, executors=lambda authorize: (AskingLedger(authorize),),
                             providers=(LedgerProvider(input_methods=(InputMethod.USERNAME_PASSWORD,)),))
    from transfers import mirrors
    from transfers.models import ExecutionSubject, TransferCandidate
    candidate = TransferCandidate("item", (), expected_bytes=4, request_kind="ledger",
                                  accepted_input_methods=(InputMethod.USERNAME_PASSWORD,))
    context = mirrors.EvidenceContext()
    evidence = await mirrors.self_evidence(candidate, core.registry, context)
    assert evidence.reason == "input_required"
    found = context.requirement_for((candidate,))
    assert found is not None and found[1] == "ledger-copy"
    assert await _challenge_tables() == {"transfer_input_challenges"}
    del ExecutionSubject


async def test_no_second_executor_secret_broker_exists():
    owners = {}
    for path in BACKEND.rglob("*.py"):
        if any(part in {"tests", ".venv", "__pycache__"} for part in path.parts):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and ("Broker" in node.name or "ChallengeStore" in node.name):
                owners.setdefault(node.name, []).append(str(path.relative_to(BACKEND)))
    assert owners == {"EphemeralInputBroker": ["transfers/input_required.py"],
                      "InputChallengeStore": ["transfers/input_required.py"]}


async def test_submitted_secret_dies_after_exact_executor_call_scope(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch, executors=lambda authorize: (AskingLedger(authorize),),
                             providers=(LedgerProvider(input_methods=(InputMethod.USERNAME_PASSWORD,)),))
    transfer = await core.engine.submit((TransferRequest("ledger", "item", name="item"),))
    for _ in range(3):
        await core.engine.tick()
    challenge = await core.engine.challenges.current(transfer.id)
    await core.engine.submit_input(transfer.id, challenge.id, "username_password",
                                   {"username": USER, "password": PASSWORD})
    await core.engine.reconcile_executions()
    assert core.executor.lent and all(item.secret_values() == () for item in core.executor.lent)
    await core.engine.reconcile_executions()
    assert (await core.repository.get(transfer.id)).state != TransferState.INPUT_REQUIRED
