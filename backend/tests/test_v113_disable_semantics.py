"""1.0.13 Gate-9 rev-5, item 1: what "disabled" means for a durable execution.

The canonical product rule, for EVERY executor:

    integration disabled
      -> no NEW provider/executor participation
      -> no automatic cancellation
      -> no automatic pause intent
      -> an already-durable execution continues through its BOUND executor
         under ordinary lifecycle/recovery semantics

The claim router (`claimants`) selects NEW work and must keep excluding a
disabled executor. A durable execution is not new work: it is resolved from
``ExecutionHandle.executor_id``, and that lookup must keep finding the bound
executor whatever the routing toggle says.

Proven here with the neutral ledger executor (an aria2-style, core-registered
executor) and with the Usenet/SAB executor, so the semantics are core-owned
rather than integration-specific.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from executor_fakes import (
    LedgerExecutor, ledger_core, settle, submit_ledger, artifact_of,
)
from transfers.models import ExecutionState, ExecutionSubject


def disable(executor):
    """Flip exactly the canonical enabled state, the way composition does."""
    executor.descriptor = replace(executor.descriptor, enabled=False)


def enable(executor):
    executor.descriptor = replace(executor.descriptor, enabled=True)


async def running_execution(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    transfer = await submit_ledger(core)
    await settle(core.engine)
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution is not None, "the fixture must own a durable execution"
    core.executor.run(artifact.execution, completed=10)
    await settle(core.engine)
    return core, transfer, (await artifact_of(core, transfer.id)).execution


# --- the routing half: disabled means no NEW participation ----------------

@pytest.mark.asyncio
async def test_a_disabled_executor_is_not_claimed_for_new_work(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    subject = ExecutionSubject.of((await artifact_of(core, (await submit_ledger(core)).id)).candidates[0])
    assert core.registry.claimants(subject), "sanity: enabled executor claims"
    disable(core.executor)
    assert core.registry.claimants(subject) == (), "a disabled executor must not take NEW work"


@pytest.mark.asyncio
async def test_new_work_submitted_while_disabled_is_not_routed(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    disable(core.executor)
    transfer = await submit_ledger(core)
    await settle(core.engine, rounds=6)
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution is None, "new work must not reach a disabled executor"
    assert not core.executor.jobs, "no native job may be created while disabled"


# --- the durable half: an owned execution continues ------------------------

@pytest.mark.asyncio
async def test_the_bound_executor_stays_resolvable_while_disabled(tmp_path, monkeypatch):
    """The decisive seam: durable work resolves by executor_id, not by claim."""
    core, _, execution = await running_execution(tmp_path, monkeypatch)
    disable(core.executor)
    assert core.registry.executor_for_handle(execution) is core.executor


@pytest.mark.asyncio
async def test_disabling_does_not_pause_a_running_execution(tmp_path, monkeypatch):
    core, transfer, execution = await running_execution(tmp_path, monkeypatch)
    before = execution.attempt_id
    disable(core.executor)
    await settle(core.engine, rounds=6)
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution is not None, "the execution must not be discarded"
    assert artifact.execution.attempt_id == before, "the SAME attempt must continue"
    job = core.executor.job_for(artifact.execution)
    assert job.state == ExecutionState.RUNNING, f"disable must not pause; got {job.state}"
    assert "pause" not in [call[0] for call in core.executor.calls], "no automatic pause intent"


@pytest.mark.asyncio
async def test_disabling_does_not_cancel_a_running_execution(tmp_path, monkeypatch):
    core, transfer, _ = await running_execution(tmp_path, monkeypatch)
    disable(core.executor)
    await settle(core.engine, rounds=6)
    assert "cancel" not in [call[0] for call in core.executor.calls], "no automatic cancellation"
    assert (await artifact_of(core, transfer.id)).execution is not None


@pytest.mark.asyncio
async def test_a_disabled_executors_execution_still_reaches_completion(tmp_path, monkeypatch):
    """Ordinary lifecycle semantics: it finishes, and it finishes as itself."""
    core, transfer, execution = await running_execution(tmp_path, monkeypatch)
    before = execution.attempt_id
    disable(core.executor)
    core.executor.finish_file(execution, (await artifact_of(core, transfer.id)).target)
    await settle(core.engine, rounds=8)
    artifact = await artifact_of(core, transfer.id)
    assert artifact.state == "completed", artifact.state
    assert artifact.execution.attempt_id == before


@pytest.mark.asyncio
async def test_reconciliation_after_a_restart_while_disabled_keeps_the_attempt(tmp_path, monkeypatch):
    """A restart is where a stranded execution would actually be lost."""
    core, transfer, execution = await running_execution(tmp_path, monkeypatch)
    before = execution.attempt_id
    disable(core.executor)
    await core.engine.initialize()          # the restart
    await settle(core.engine, rounds=6)
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution is not None, "the restart must not strand the execution"
    assert artifact.execution.attempt_id == before, "reconciliation must keep the SAME attempt"
    assert core.executor.job_for(artifact.execution).state == ExecutionState.RUNNING


@pytest.mark.asyncio
async def test_reenabling_creates_no_second_attempt_and_no_duplicate_job(tmp_path, monkeypatch):
    core, transfer, execution = await running_execution(tmp_path, monkeypatch)
    before = execution.attempt_id
    native_jobs = len(core.executor.jobs)
    disable(core.executor)
    await settle(core.engine, rounds=4)
    enable(core.executor)
    await settle(core.engine, rounds=6)
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution.attempt_id == before, "re-enabling must not start a new attempt"
    assert len(core.executor.jobs) == native_jobs, "re-enabling must not duplicate the native job"


# --- the same semantics for a second, differently-shaped executor ----------

@pytest.mark.asyncio
async def test_disabling_one_executor_leaves_another_free_to_take_new_work(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch, executors=lambda auth: (
        LedgerExecutor(auth, identity="ledger-a", priority=10),
        LedgerExecutor(auth, identity="ledger-b", priority=0),
    ))
    first, second = core.executors
    transfer = await submit_ledger(core)
    await settle(core.engine)
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution.executor_id == "ledger-a"
    first.run(artifact.execution, completed=5)
    await settle(core.engine)

    disable(first)
    later = await submit_ledger(core, payload="ledger-second")
    await settle(core.engine, rounds=6)
    routed = await artifact_of(core, later.id)
    assert routed.execution is not None and routed.execution.executor_id == "ledger-b"
    # ...and the first executor's durable execution is untouched.
    held = await artifact_of(core, transfer.id)
    assert held.execution.executor_id == "ledger-a"
    assert first.job_for(held.execution).state == ExecutionState.RUNNING


# --- the seam has exactly one implementation -------------------------------

def test_durable_execution_lookups_all_go_through_the_one_seam():
    """No core path may resolve an owned execution by poking the registry dict.

    ``claimants``/``executor_for_subject`` select NEW work and exclude disabled
    executors; a raw ``registry.executors[...]`` lookup happens to work today
    but is unnamed, so nothing stops it being "tidied" into the claim router
    later and stranding live executions. One named seam makes the difference
    between new work and owned work explicit.
    """
    import re
    from pathlib import Path

    core_dir = Path(__file__).resolve().parents[1] / "transfers"
    offenders = []
    for path in core_dir.rglob("*.py"):
        if path.name == "registry.py":
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"registry\.executors(\.get\(|\[)", line):
                offenders.append(f"{path.name}:{number}")
    assert not offenders, f"resolve owned executions via executor_for_handle: {offenders}"


def test_the_claim_router_still_excludes_disabled_executors():
    """The other half of the rule: this seam must NOT be relaxed."""
    import inspect

    from transfers.registry import IntegrationRegistry

    source = inspect.getsource(IntegrationRegistry.claimants)
    assert "descriptor.enabled" in source
    handle_seam = inspect.getsource(IntegrationRegistry.executor_for_handle)
    assert "enabled" not in handle_seam.split('"""')[-1], (
        "the bound-execution seam must not consult the routing toggle"
    )


# --- the same rule, proven with the Usenet/SAB executor --------------------

VALID_NZB = (b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
             b'<file poster="p@e.net" date="1700000000" subject="x [1/1] - &quot;x.bin&quot; yEnc (1/1)">'
             b"<groups><group>alt.binaries.test</group></groups>"
             b'<segments><segment bytes="1024" number="1">a@e.net</segment></segments>'
             b"</file></nzb>")


async def usenet_core(tmp_path, monkeypatch):
    import db.database as database
    from executors.sabnzbd.executor import SabnzbdConfiguration, SabnzbdExecutor
    from providers.usenet.provider import UsenetProvider
    from sab_fakes import FakeSab, staged_store
    from transfers.convergence_engine import TransferEngine
    from transfers.models import TransferRequest
    from transfers.policy import TransferPolicy
    from transfers.recovery_repository import TransferRepository
    from transfers.registry import IntegrationRegistry
    from types import SimpleNamespace

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "state.db")
    await database.init_db()
    repository = TransferRepository()
    root = tmp_path / "payloads"
    (root / ".dpwork" / "complete").mkdir(parents=True)
    (root / ".dpwork" / "incomplete").mkdir(parents=True)
    sab = FakeSab(complete_dir=str(root / ".dpwork" / "complete"),
                  download_dir=str(root / ".dpwork" / "incomplete"))
    registry = IntegrationRegistry()
    registry.register_provider(UsenetProvider(staged_input=staged_store()))
    executor = SabnzbdExecutor(
        sab, SabnzbdConfiguration(local_root=str(root),
                                  working_directory=str(root / ".dpwork"),
                                  complete_directory=str(root / ".dpwork" / "complete")),
        repository.authorize_execution, staged_input=staged_store())
    registry.register_executor(executor)
    engine = TransferEngine(repository, registry, download_root=str(root),
                            policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                                                  max_active_executions=2),
                            clock=lambda: 1000.0)
    await engine.initialize()
    transfer = await engine.submit((TransferRequest("nzb", VALID_NZB, name="posting.nzb"),),
                                   name="posting", deduplicate=False)
    await settle(engine, rounds=8)
    return SimpleNamespace(engine=engine, repository=repository, registry=registry,
                           executor=executor, sab=sab, transfer=transfer)


@pytest.mark.asyncio
async def test_sab_disable_keeps_the_same_attempt_and_the_same_native_job(tmp_path, monkeypatch):
    core = await usenet_core(tmp_path, monkeypatch)
    artifact = (await core.repository.artifacts(core.transfer.id))[0]
    assert artifact.execution is not None
    before, jobs = artifact.execution.attempt_id, len(core.sab.submissions)

    disable(core.executor)
    await settle(core.engine, rounds=6)

    artifact = (await core.repository.artifacts(core.transfer.id))[0]
    assert artifact.execution is not None, "disabling must not strand the execution"
    assert artifact.execution.attempt_id == before
    assert len(core.sab.submissions) == jobs, "no new native submission while disabled"
    assert artifact.execution.native["nzo_id"] in core.sab.queue, "the native job must survive"


@pytest.mark.asyncio
async def test_sab_disable_issues_no_pause_and_no_cancel(tmp_path, monkeypatch):
    core = await usenet_core(tmp_path, monkeypatch)
    artifact = (await core.repository.artifacts(core.transfer.id))[0]
    nzo = artifact.execution.native["nzo_id"]
    assert core.sab.queue[nzo].status != "Paused", "sanity: the job starts unpaused"

    disable(core.executor)
    await settle(core.engine, rounds=6)

    assert nzo in core.sab.queue, "no automatic cancellation: the native job must remain"
    assert core.sab.queue[nzo].status != "Paused", (
        f"no automatic pause intent; native status became {core.sab.queue[nzo].status}")


@pytest.mark.asyncio
async def test_sab_bound_executor_resolves_while_disabled(tmp_path, monkeypatch):
    core = await usenet_core(tmp_path, monkeypatch)
    artifact = (await core.repository.artifacts(core.transfer.id))[0]
    disable(core.executor)
    assert core.registry.executor_for_handle(artifact.execution) is core.executor
    assert core.registry.claimants(ExecutionSubject.of(artifact.candidates[0])) == ()


@pytest.mark.asyncio
async def test_sab_restart_then_reenable_makes_no_second_attempt(tmp_path, monkeypatch):
    core = await usenet_core(tmp_path, monkeypatch)
    artifact = (await core.repository.artifacts(core.transfer.id))[0]
    before, jobs = artifact.execution.attempt_id, len(core.sab.submissions)

    disable(core.executor)
    await core.engine.initialize()          # restart while disabled
    await settle(core.engine, rounds=6)
    artifact = (await core.repository.artifacts(core.transfer.id))[0]
    assert artifact.execution.attempt_id == before, "the restart must reconcile the same attempt"

    enable(core.executor)
    await settle(core.engine, rounds=6)
    artifact = (await core.repository.artifacts(core.transfer.id))[0]
    assert artifact.execution.attempt_id == before, "re-enabling must not start a new attempt"
    assert len(core.sab.submissions) == jobs, "re-enabling must not duplicate the native job"
