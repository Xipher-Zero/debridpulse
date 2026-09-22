"""Core-owned global bandwidth allocation and global execution concurrency.

``execution_runtime_limits.max_download_bytes_per_second`` is split equally
across the executors that currently hold a bandwidth reservation; each
executor only enforces the ceiling core assigns it. Global concurrency is a
single core admission count across every executor.
"""
from __future__ import annotations

from dataclasses import replace
import inspect

import pytest

from executor_fakes import LedgerExecutor, artifact_of, ledger_capabilities, ledger_core, submit_ledger
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage
from transfers.models import ExecutorRuntimeCapability, TransferRequest
from transfers.policy import TransferPolicy

pytestmark = pytest.mark.asyncio

CAP = 30_000_000


def _pair(log, *, a_caps=None, b_caps=None, c=False):
    def build(authorize):
        executors = [
            LedgerExecutor(authorize, identity="exec-a", kinds=("ledger",), log=log,
                           capabilities=a_caps or ledger_capabilities(aggregate_bandwidth_ceiling=True)),
            LedgerExecutor(authorize, identity="exec-b", kinds=("tome",), log=log,
                           capabilities=b_caps or ledger_capabilities(aggregate_bandwidth_ceiling=True)),
        ]
        if c:
            executors.append(LedgerExecutor(authorize, identity="exec-c", kinds=("scroll",), log=log,
                                             capabilities=ledger_capabilities(aggregate_bandwidth_ceiling=True)))
        return tuple(executors)
    return build


async def _core(tmp_path, monkeypatch, log, *, cap=CAP, slots=6, **kwargs):
    from executor_fakes import LedgerProvider

    class Scrolls(LedgerProvider):
        def __init__(self):
            super().__init__("scroll-lab")
            self.descriptor = replace(self.descriptor, request_types=frozenset({"scroll"}))

    core = await ledger_core(tmp_path, monkeypatch, executors=_pair(log, **kwargs),
                             providers=(LedgerProvider(), Scrolls()),
                             policy=TransferPolicy(retry_delay=1, adoption_stability_seconds=0,
                                                   max_active_executions=slots))
    core.engine.configure_runtime_limits(cap)
    return core


def _ceilings(log, identity):
    return [entry[2] for entry in log if entry[0] == identity and entry[1] == "ceiling"]


async def test_unlimited_cap_requires_no_executor_ceiling_capability(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log, cap=0, a_caps=ledger_capabilities())
    transfer = await submit_ledger(core, "item")
    assert (await artifact_of(core, transfer.id)).execution is not None
    assert not _ceilings(log, "exec-a")
    status = await core.engine.converge_runtime_limits()
    assert status.ok and status.effective == 0 and status.configured == 0


async def test_single_reserved_executor_receives_full_cap(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    await submit_ledger(core, "item")
    assert _ceilings(log, "exec-a") == [CAP]
    assert log.index(("exec-a", "ceiling", CAP)) < log.index(("exec-a", "start"))
    assert core.engine.runtime.reserved == frozenset({"exec-a"})


async def test_two_reserved_executors_receive_equal_halves(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    await submit_ledger(core, "item")
    await submit_ledger(core, "other", kind="tome")
    assert _ceilings(log, "exec-a")[-1] == CAP // 2
    assert _ceilings(log, "exec-b")[-1] == CAP // 2
    assert core.engine.runtime.reserved == frozenset({"exec-a", "exec-b"})


async def test_transfer_count_within_executor_does_not_change_share(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    for index in range(3):
        await submit_ledger(core, f"item-{index}")
    await submit_ledger(core, "other", kind="tome")
    assert _ceilings(log, "exec-a")[-1] == CAP // 2 == _ceilings(log, "exec-b")[-1]


async def test_third_reserved_executor_rebalances_equal_thirds(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log, c=True)
    await submit_ledger(core, "item")
    await submit_ledger(core, "other", kind="tome")
    await submit_ledger(core, "third", kind="scroll")
    for identity in ("exec-a", "exec-b", "exec-c"):
        assert _ceilings(log, identity)[-1] == CAP // 3


async def test_unused_share_is_not_borrowed(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    first = await submit_ledger(core, "item")
    await submit_ledger(core, "other", kind="tome")
    idle = await artifact_of(core, first.id)
    core.executors[0].run(idle.execution, network_active=False)  # reserved but momentarily idle
    for _ in range(3):
        await core.engine.reconcile_executions()
    assert _ceilings(log, "exec-b")[-1] == CAP // 2
    assert CAP not in _ceilings(log, "exec-b")


async def test_adding_executor_reduces_existing_ceiling_before_new_native_start(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    await submit_ledger(core, "item")
    log.clear()
    await submit_ledger(core, "other", kind="tome")
    shrink = log.index(("exec-a", "ceiling", CAP // 2))
    newcomer = log.index(("exec-b", "ceiling", CAP // 2))
    start = log.index(("exec-b", "start"))
    assert shrink < newcomer < start


async def test_removing_executor_raises_remaining_ceiling_only_after_positive_release_truth(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    await submit_ledger(core, "item")
    other = await submit_ledger(core, "other", kind="tome")
    b_artifact = await artifact_of(core, other.id)
    executor_b = core.executors[1]
    executor_b.observe_failure = NormalizedError(Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE,
                                                 Stage.RECONCILIATION, retryability=Retryability.BACKOFF)
    executor_b.cancel_mode = "confirm"
    executor_b.job_for(b_artifact.execution).state = __import__("transfers.models", fromlist=["x"]).ExecutionState.CANCELLED
    await core.engine.reconcile_executions()
    assert _ceilings(log, "exec-a")[-1] == CAP // 2  # no positive truth yet
    executor_b.observe_failure = None
    executor_b.finish_file(b_artifact.execution, b_artifact.target)
    for _ in range(3):
        await core.engine.reconcile_executions()
    assert core.engine.runtime.reserved == frozenset({"exec-a"})
    assert _ceilings(log, "exec-a")[-1] == CAP


async def test_failed_observation_retains_previous_executor_reservation(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    await submit_ledger(core, "item")
    await submit_ledger(core, "other", kind="tome")
    core.executors[1].observe_failure = NormalizedError(Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE,
                                                        Stage.RECONCILIATION, retryability=Retryability.BACKOFF)
    for _ in range(3):
        await core.engine.reconcile_executions()
    assert core.engine.runtime.reserved == frozenset({"exec-a", "exec-b"})
    assert _ceilings(log, "exec-a")[-1] == CAP // 2


async def test_unreachable_executor_share_is_not_redistributed(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    await submit_ledger(core, "item")
    other = await submit_ledger(core, "other", kind="tome")
    b_artifact = await artifact_of(core, other.id)
    core.executors[1].jobs.clear()  # native work may be gone, but core cannot observe that
    core.executors[1].observe_failure = NormalizedError(Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE,
                                                        Stage.RECONCILIATION, retryability=Retryability.BACKOFF)
    core.engine.configure_runtime_limits(CAP * 2)
    status = await core.engine.converge_runtime_limits()
    assert _ceilings(log, "exec-a")[-1] == CAP  # the new cap is still split in two
    assert "exec-b" in core.engine.runtime.reserved
    assert (await artifact_of(core, other.id)).execution.attempt_id == b_artifact.execution.attempt_id
    del status


async def test_finite_cap_blocks_new_work_on_executor_without_aggregate_ceiling(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log, a_caps=ledger_capabilities())
    transfer = await submit_ledger(core, "item")
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution is None and ("exec-a", "start") not in log
    assert artifact.error is not None and artifact.error.category == Category.UNSUPPORTED_CAPABILITY


async def test_finite_cap_blocks_new_work_when_runtime_ceiling_capability_is_unavailable(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    core.executors[0].runtime_available = frozenset()
    transfer = await submit_ledger(core, "item")
    artifact = await artifact_of(core, transfer.id)
    assert artifact.execution is None and ("exec-a", "start") not in log
    assert artifact.state == "queued"  # deferred, not failed: availability is a runtime fact
    assert not core.engine.runtime.status().ok
    core.executors[0].runtime_available = frozenset(ExecutorRuntimeCapability)
    await core.engine.reconcile_executions()
    assert (await artifact_of(core, transfer.id)).execution is not None


async def test_active_executor_losing_runtime_ceiling_capability_keeps_reserved_share_and_degrades_truth(
        tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    await submit_ledger(core, "item")
    core.executors[0].runtime_available = frozenset()
    core.engine.configure_runtime_limits(CAP // 3)
    status = await core.engine.converge_runtime_limits()
    assert not status.ok and status.effective is None and status.last_apply_error
    assert core.engine.runtime.reserved == frozenset({"exec-a"})
    starts = log.count(("exec-a", "start"))
    await submit_ledger(core, "second")
    assert log.count(("exec-a", "start")) == starts  # no further uncontrolled acquisition


async def test_failed_native_ceiling_apply_reports_configured_effective_divergence(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    await submit_ledger(core, "item")
    core.executors[0].ceiling_failure = True
    core.engine.configure_runtime_limits(CAP // 2)
    status = await core.engine.converge_runtime_limits()
    assert status.configured == CAP // 2
    assert not status.ok and status.effective is None and status.last_apply_error


async def test_runtime_limit_api_contains_no_aria2_native_option_name():
    from api import routes
    from transfers import runtime_coordination
    for handler in (routes.get_execution_runtime_limits, routes.patch_execution_runtime_limits):
        source = inspect.getsource(handler)
        assert "max-overall-download-limit" not in source
        assert "integration_admin" not in source and "aria2" not in source
    source = inspect.getsource(runtime_coordination)
    assert "aria2" not in source.lower() and "max-overall" not in source


async def test_core_execution_capacity_is_global_across_multiple_executors(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log, cap=0, slots=2)
    for index in range(2):
        await submit_ledger(core, f"item-{index}")
    await submit_ledger(core, "other", kind="tome")
    assert log.count(("exec-a", "start")) + log.count(("exec-b", "start")) == 2


async def test_changing_global_concurrency_changes_core_admission_only(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log, cap=0, slots=1)
    for index in range(3):
        await core.engine.submit((TransferRequest("ledger", f"item-{index}", name=f"item-{index}"),))
    await core.engine.tick()
    assert log.count(("exec-a", "start")) == 1
    core.engine.configure_policy(replace(core.engine.policy, max_active_executions=3))
    await core.engine.reconcile_executions()
    assert log.count(("exec-a", "start")) == 3
    assert {entry[1] for entry in log} == {"start"}  # nothing concurrency-shaped reached the executor


async def test_aria2_native_configuration_is_not_rewritten_from_global_concurrency():
    from application import composition
    from executors.aria2 import admin, runtime
    from executors.aria2.definition import Aria2Options
    from api import routes

    options = runtime.build_aria2_global_options(Aria2Options())
    assert "max-overall-download-limit" not in options
    native = options["max-concurrent-downloads"]
    assert int(native) >= 20  # never undercuts DP's supported global range
    assert "max_concurrent_executions" not in {item for item in runtime.Aria2RuntimeConfiguration.__dataclass_fields__}
    assert "max_download_bytes_per_second" not in runtime.Aria2RuntimeConfiguration.__dataclass_fields__
    assert "max_concurrent_executions" not in inspect.getsource(admin)
    assert "max_concurrent_executions" not in inspect.getsource(runtime)
    assert "apply_memory_tuning" not in inspect.getsource(routes.patch_transfer_policy)
    assert "Aria2RuntimeConfiguration" not in inspect.getsource(composition)
    assert runtime.build_aria2_global_options(Aria2Options())["max-concurrent-downloads"] == native


async def test_multiple_executors_cannot_each_consume_the_full_global_slot_count(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log, cap=0, slots=2)
    for index in range(3):
        await core.engine.submit((TransferRequest("ledger", f"a-{index}", name=f"a-{index}"),))
        await core.engine.submit((TransferRequest("tome", f"b-{index}", name=f"b-{index}"),))
    for _ in range(3):
        await core.engine.tick()
    assert log.count(("exec-a", "start")) + log.count(("exec-b", "start")) == 2


async def test_unprovable_reserved_ceiling_engages_available_acquisition_gate(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log, a_caps=ledger_capabilities(
        aggregate_bandwidth_ceiling=True, acquisition_gate=True))
    await submit_ledger(core, "item")
    executor = core.executors[0]
    executor.ceiling_failure = True
    core.engine.configure_runtime_limits(CAP // 2)
    status = await core.engine.converge_runtime_limits()
    assert not status.ok and executor.gate == [True]  # uncontrolled acquisition stopped, nothing cancelled
    assert core.engine.runtime.gated == frozenset({"exec-a"})
    await core.engine.pause_all()
    await core.engine.resume_all()
    assert executor.gate[-1] is True  # global resume never releases a cap-held gate
    executor.ceiling_failure = False
    status = await core.engine.converge_runtime_limits()
    assert status.ok and executor.gate[-1] is False and core.engine.runtime.gated == frozenset()
    assert not [call for call in executor.calls if call[0] == "cancel"]


async def test_positive_cap_below_executor_count_never_becomes_unlimited(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log, cap=1)
    await submit_ledger(core, "item")
    assert _ceilings(log, "exec-a") == [1]
    blocked = await submit_ledger(core, "other", kind="tome")
    assert (await artifact_of(core, blocked.id)).execution is None  # fails closed: no positive share exists
    assert ("exec-b", "start") not in log
    assert 0 not in _ceilings(log, "exec-a") + _ceilings(log, "exec-b")
    assert not core.engine.runtime.status().ok


async def test_cap_reduced_below_reserved_count_degrades_and_gates_instead_of_unlimited(tmp_path, monkeypatch):
    log = []
    caps = ledger_capabilities(aggregate_bandwidth_ceiling=True, acquisition_gate=True)
    core = await _core(tmp_path, monkeypatch, log, a_caps=caps, b_caps=caps)
    await submit_ledger(core, "item")
    await submit_ledger(core, "other", kind="tome")
    core.engine.configure_runtime_limits(1)
    status = await core.engine.converge_runtime_limits()
    assert not status.ok and status.effective is None
    assert 0 not in _ceilings(log, "exec-a") + _ceilings(log, "exec-b")
    assert core.executors[0].gate[-1] is True and core.executors[1].gate[-1] is True
    assert core.engine.runtime.reserved == frozenset({"exec-a", "exec-b"})


@pytest.mark.parametrize("cap", [0, CAP])
async def test_executor_that_is_not_ready_is_never_admitted(tmp_path, monkeypatch, cap):
    log = []
    core = await _core(tmp_path, monkeypatch, log, cap=cap)
    core.executors[0].ready = False
    transfer = await submit_ledger(core, "item")
    assert (await artifact_of(core, transfer.id)).execution is None and ("exec-a", "start") not in log
    core.executors[0].ready = True
    await core.engine.reconcile_executions()
    assert (await artifact_of(core, transfer.id)).execution is not None


async def test_capability_loss_with_unchanged_assigned_share_blocks_start_and_resume(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    first = await submit_ledger(core, "item")
    artifact = await artifact_of(core, first.id)
    await core.engine.pause(first.id)
    assert core.executors[0].job_for(artifact.execution).state.value == "paused"
    core.executors[0].runtime_available = frozenset()  # the share (CAP) is unchanged and still "assigned"
    starts = log.count(("exec-a", "start"))
    second = await submit_ledger(core, "second")
    assert (await artifact_of(core, second.id)).execution is None
    assert log.count(("exec-a", "start")) == starts
    await core.engine.resume(first.id)
    await core.engine.reconcile_executions()
    assert ("exec-a", "resume") not in log
    assert core.executors[0].job_for(artifact.execution).state.value == "paused"


async def test_failed_newcomer_admission_restores_the_unchanged_reserved_split(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    await submit_ledger(core, "item")
    assert _ceilings(log, "exec-a") == [CAP]
    core.executors[1].ceiling_failure = True
    blocked = await submit_ledger(core, "other", kind="tome")
    assert (await artifact_of(core, blocked.id)).execution is None
    assert ("exec-b", "start") not in log
    assert core.engine.runtime.reserved == frozenset({"exec-a"})
    assert _ceilings(log, "exec-a")[-2:] == [CAP // 2, CAP]  # shrunk for the newcomer, then restored
    status = core.engine.runtime.status()
    assert status.ok and status.effective == CAP  # the current reserved set is still provably enforced
