"""1.0.13 Gate-9 rev-6: terminalization must not depend on routing enablement.

The integration Enable control means **eligibility for NEW provider/executor
participation**. It is not a veto on finishing delivery work that is already
durably satisfied.

Instrumented on the rejected rev-5 tree, the exact blocking fact was NOT a
request state (the request sat at ``resolved``) and NOT ``aggregate_lifecycle``
(which already answered ``should_complete=True`` throughout). It was the
continuation gate in ``convergence_engine._process_executions``: with the
candidate's provider disabled it ``continue``d past the only tick-path call to
``_aggregate``, so the parent was never asked to terminalize. The same pass
also routed an ALREADY-COMPLETED artifact into ``recover_artifact`` 11 times.

The correction is the canonical distinction, for every provider/executor
pairing:

* provider-side work STILL REQUIRED -> administrative disablement may park it;
* delivery obligation ALREADY DURABLY SATISFIED by a completed canonical
  artifact -> current enablement must not prevent parent completion.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from executor_fakes import (
    LedgerExecutor, LedgerProvider, artifact_of, ledger_core, settle, submit_ledger,
)
from transfers.models import ExecutionState, ExecutionSubject, TransferState


def disable(*implementations):
    for item in implementations:
        item.descriptor = replace(item.descriptor, enabled=False)


def enable(*implementations):
    for item in implementations:
        item.descriptor = replace(item.descriptor, enabled=True)


async def running_ledger_transfer(tmp_path, monkeypatch):
    """A neutral, non-Usenet pairing with one durable execution in flight."""
    core = await ledger_core(tmp_path, monkeypatch)
    transfer = await submit_ledger(core)
    await settle(core.engine)
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution is not None, "the fixture must own a durable execution"
    core.executor.run(artifact.execution, completed=2)
    await settle(core.engine)
    return core, transfer


# --- 1. paired provider/executor: parent completes WITHOUT re-enable -------

@pytest.mark.asyncio
async def test_parent_completes_while_disabled_after_the_artifact_succeeds(tmp_path, monkeypatch):
    core, transfer = await running_ledger_transfer(tmp_path, monkeypatch)
    artifact = await artifact_of(core, transfer.id)
    attempt = artifact.execution.attempt_id

    disable(core.provider, core.executor)
    core.executor.finish_file(artifact.execution, artifact.target)
    await settle(core.engine, rounds=10)

    artifact = await artifact_of(core, transfer.id)
    assert artifact.state == "completed", artifact.state
    record = await core.repository.get(transfer.id)
    assert record.state == TransferState.COMPLETED, (
        f"parent must terminalize while disabled; got {record.state}"
    )
    assert artifact.execution.attempt_id == attempt, "the same attempt throughout"


@pytest.mark.asyncio
async def test_a_completed_artifact_is_never_routed_back_into_recovery(tmp_path, monkeypatch):
    """The rev-5 pass called recover_artifact 11 times on a completed artifact."""
    core, transfer = await running_ledger_transfer(tmp_path, monkeypatch)
    artifact = await artifact_of(core, transfer.id)
    disable(core.provider, core.executor)
    core.executor.finish_file(artifact.execution, artifact.target)

    seen = []
    original = core.engine.recover_artifact

    async def watched(item, **kwargs):
        if getattr(item, "state", "") == "completed":
            seen.append(item.id)
        return await original(item, **kwargs)

    core.engine.recover_artifact = watched
    await settle(core.engine, rounds=10)
    assert not seen, f"completed artifact routed into recovery: {seen}"


# --- 2. restart while still disabled --------------------------------------

@pytest.mark.asyncio
async def test_a_restart_while_disabled_converges_the_parent(tmp_path, monkeypatch):
    core, transfer = await running_ledger_transfer(tmp_path, monkeypatch)
    artifact = await artifact_of(core, transfer.id)
    attempt = artifact.execution.attempt_id
    jobs = len(core.executor.jobs)

    disable(core.provider, core.executor)
    core.executor.finish_file(artifact.execution, artifact.target)
    await core.engine.initialize()              # the restart, still disabled
    await settle(core.engine, rounds=10)

    record = await core.repository.get(transfer.id)
    assert record.state == TransferState.COMPLETED, record.state
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution.attempt_id == attempt, "no new attempt"
    assert len(core.executor.jobs) == jobs, "no duplicate native job"
    assert len(await core.repository.executions(transfer.id)) == 1, "exactly one attempt"


# --- 3. provider work genuinely still required stays parked ---------------

@pytest.mark.asyncio
async def test_unsatisfied_delivery_never_completes_while_disabled(tmp_path, monkeypatch):
    """Nothing here may manufacture completion: the obligation is unmet."""
    core, transfer = await running_ledger_transfer(tmp_path, monkeypatch)
    disable(core.provider, core.executor)
    await settle(core.engine, rounds=10)

    record = await core.repository.get(transfer.id)
    assert record.state != TransferState.COMPLETED, "an unsatisfied obligation must not complete"
    artifact = await artifact_of(core, transfer.id)
    assert artifact.state != "completed"
    assert artifact.execution is not None, "the in-flight execution is not discarded"


@pytest.mark.asyncio
async def test_an_in_flight_execution_is_not_parked_by_disabling_its_provider(tmp_path, monkeypatch):
    """Delivery already UNDERWAY needs nothing further from the provider.

    Parking here is what stopped the execution from ever finishing while
    disabled, so scenario 1 could not even be reached.
    """
    core, transfer = await running_ledger_transfer(tmp_path, monkeypatch)
    artifact = await artifact_of(core, transfer.id)
    disable(core.provider, core.executor)
    await settle(core.engine, rounds=10)

    artifact = await artifact_of(core, transfer.id)
    assert artifact.state in {"queued", "downloading", "verifying"}, artifact.state
    job = core.executor.job_for(artifact.execution)
    assert job.state == ExecutionState.RUNNING, f"in-flight execution was parked: {job.state}"
    assert "pause" not in [call[0] for call in core.executor.calls], "no automatic pause intent"


@pytest.mark.asyncio
async def test_provider_work_that_is_genuinely_required_stays_parked(tmp_path, monkeypatch):
    """An artifact with NO execution to continue still needs a provider, so
    administrative disablement parks it -- and picks no one else."""
    core = await ledger_core(
        tmp_path, monkeypatch,
        providers=(LedgerProvider("ledger-primary"), LedgerProvider("ledger-secondary")),
    )
    disable(core.providers[0], core.providers[1], core.executor)
    transfer = await submit_ledger(core, payload="needs-a-provider")
    await settle(core.engine, rounds=10)

    artifacts = await core.repository.artifacts(transfer.id)
    assert all(item.execution is None for item in artifacts), "nothing may be dispatched"
    assert not core.executor.jobs, "no native job while parked"
    record = await core.repository.get(transfer.id)
    assert record.state != TransferState.COMPLETED, "a parked obligation must not complete"


@pytest.mark.asyncio
async def test_a_parked_continuation_does_not_select_an_alternate_provider(tmp_path, monkeypatch):
    core = await ledger_core(
        tmp_path, monkeypatch,
        providers=(LedgerProvider("ledger-primary"), LedgerProvider("ledger-secondary")),
    )
    transfer = await submit_ledger(core)
    await settle(core.engine)
    artifact = await artifact_of(core, transfer.id)
    chosen = artifact.candidates[artifact.selected].provider_id
    core.executor.run(artifact.execution, completed=2)
    await settle(core.engine)

    disable(core.providers[0], core.providers[1], core.executor)
    await settle(core.engine, rounds=10)

    artifact = await artifact_of(core, transfer.id)
    assert artifact.candidates[artifact.selected].provider_id == chosen, "no alternate provider"
    record = await core.repository.get(transfer.id)
    assert record.state != TransferState.COMPLETED


# --- 4. new work is still not routed while disabled -----------------------

@pytest.mark.asyncio
async def test_new_work_while_disabled_is_still_not_routed(tmp_path, monkeypatch):
    core = await ledger_core(tmp_path, monkeypatch)
    disable(core.provider, core.executor)
    transfer = await submit_ledger(core, payload="later-item")
    await settle(core.engine, rounds=8)
    artifact = await artifact_of(core, transfer.id) if (await core.repository.artifacts(transfer.id)) else None
    assert artifact is None or artifact.execution is None, "disabled must not take NEW work"
    assert not core.executor.jobs, "no native job may be created while disabled"


def test_the_claim_router_and_provider_routing_are_untouched():
    """The correction must not relax NEW-work eligibility anywhere."""
    import inspect

    from transfers.registry import IntegrationRegistry

    assert "descriptor.enabled" in inspect.getsource(IntegrationRegistry.claimants)
    # provider_for() delegates its eligibility to the selection helper, which is
    # where a disabled provider is excluded from routing.
    assert "provider.descriptor.enabled" in inspect.getsource(IntegrationRegistry._provider_selection)
    assert "descriptor.enabled" in inspect.getsource(IntegrationRegistry._provider_for_bound_owner)


# --- 5. the same rule for the Usenet/SAB pairing that exposed it ----------

async def usenet_transfer(tmp_path, monkeypatch):
    from test_v113_disable_semantics import usenet_core

    core = await usenet_core(tmp_path, monkeypatch)
    core.provider = core.registry.providers["usenet"]
    return core


@pytest.mark.asyncio
async def test_usenet_parent_completes_while_disabled(tmp_path, monkeypatch):
    core = await usenet_transfer(tmp_path, monkeypatch)
    artifact = (await core.repository.artifacts(core.transfer.id))[0]
    attempt = artifact.execution.attempt_id
    nzo = artifact.execution.native["nzo_id"]

    disable(core.provider, core.executor)
    core.sab.finish(nzo)
    await settle(core.engine, rounds=12)

    artifact = (await core.repository.artifacts(core.transfer.id))[0]
    assert artifact.state == "completed", artifact.state
    record = await core.repository.get(core.transfer.id)
    assert record.state == TransferState.COMPLETED, (
        f"parent must terminalize while disabled; got {record.state}"
    )
    assert artifact.execution.attempt_id == attempt
    assert len(core.sab.submissions) == 1, "no duplicate native job"


@pytest.mark.asyncio
async def test_usenet_restart_while_disabled_converges_the_parent(tmp_path, monkeypatch):
    core = await usenet_transfer(tmp_path, monkeypatch)
    artifact = (await core.repository.artifacts(core.transfer.id))[0]
    attempt = artifact.execution.attempt_id

    disable(core.provider, core.executor)
    core.sab.finish(artifact.execution.native["nzo_id"])
    await core.engine.initialize()
    await settle(core.engine, rounds=12)

    record = await core.repository.get(core.transfer.id)
    assert record.state == TransferState.COMPLETED, record.state
    assert len(core.sab.submissions) == 1, "no duplicate native job"
    assert len(await core.repository.executions(core.transfer.id)) == 1, "exactly one attempt"
    artifact = (await core.repository.artifacts(core.transfer.id))[0]
    assert artifact.execution.attempt_id == attempt
