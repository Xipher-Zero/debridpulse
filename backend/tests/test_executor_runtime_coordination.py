"""Core-owned global bandwidth allocation and global execution concurrency.

``execution_runtime_limits.max_download_bytes_per_second`` is a maximum for the
aggregate, allocated work-conservingly from observed demand across the
executors that currently hold a bandwidth reservation; each executor only
enforces the ceiling core assigns it. Global concurrency is a
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


FLOOR = CAP // 32


class _Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


def _clocked(core) -> _Clock:
    """The engine's throughput meter (and so the bandwidth owner) on a test clock."""
    clock = _Clock()
    core.engine.throughput.clock = clock
    return clock


async def _flow(core, clock, demands, *, seconds=12.0, step=1.0):
    """Run reconcile passes while each [executor, handle, demand] acquires at
    min(demand, the ceiling it currently enforces) -- None = not acquiring."""
    for _ in range(int(seconds / step)):
        clock.now += step
        for executor, handle, demand in demands:
            job = executor.job_for(handle)
            if demand is None:
                executor.run(handle, network_active=False)
                continue
            ceiling = executor.ceilings[-1] if executor.ceilings else 0
            rate = min(demand, ceiling) if ceiling else demand
            executor.run(handle, completed=int(job.progress.completed_bytes + rate * step))
        await core.engine.reconcile_executions()


def _in_effect_never_exceeds(log, cap):
    """Replaying every ceiling call in order (a failed one counted as applied),
    the ceilings executors may hold never add up to more than ``cap``."""
    current = {}
    for identity, kind, *value in log:
        if kind != "ceiling":
            continue
        current[identity] = value[0] or float("inf")
        assert sum(current.values()) <= cap, (identity, value[0], dict(current))


async def _saturating(core, transfers):
    """[executor, handle, demand] with a demand far above the cap, per transfer id."""
    demands = []
    for transfer_id in transfers:
        artifact = await artifact_of(core, transfer_id)
        executor = next(item for item in core.executors if item.descriptor.id == artifact.execution.executor_id)
        demands.append([executor, artifact.execution, CAP * 4])
    return demands


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


async def test_two_saturated_executors_share_the_cap_equally(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    clock = _clocked(core)
    first = await submit_ledger(core, "item")
    other = await submit_ledger(core, "other", kind="tome")
    await _flow(core, clock, await _saturating(core, [first.id, other.id]))
    assert _ceilings(log, "exec-a")[-1] == CAP // 2
    assert _ceilings(log, "exec-b")[-1] == CAP // 2
    assert core.engine.runtime.reserved == frozenset({"exec-a", "exec-b"})
    _in_effect_never_exceeds(log, CAP)


async def test_transfer_count_within_executor_does_not_change_share(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    clock = _clocked(core)
    transfers = [await submit_ledger(core, f"item-{index}") for index in range(3)]
    transfers.append(await submit_ledger(core, "other", kind="tome"))
    await _flow(core, clock, await _saturating(core, [transfer.id for transfer in transfers]))
    assert _ceilings(log, "exec-a")[-1] == CAP // 2 == _ceilings(log, "exec-b")[-1]


async def test_third_saturated_executor_rebalances_equal_thirds(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log, c=True)
    clock = _clocked(core)
    transfers = [await submit_ledger(core, "item"), await submit_ledger(core, "other", kind="tome"),
                 await submit_ledger(core, "third", kind="scroll")]
    await _flow(core, clock, await _saturating(core, [transfer.id for transfer in transfers]))
    for identity in ("exec-a", "exec-b", "exec-c"):
        assert _ceilings(log, identity)[-1] == CAP // 3
    _in_effect_never_exceeds(log, CAP)


async def test_unused_share_is_lent_and_reclaimed_when_its_demand_returns(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    clock = _clocked(core)
    first = await submit_ledger(core, "item")
    other = await submit_ledger(core, "other", kind="tome")
    demands = await _saturating(core, [first.id, other.id])
    demands[0][2] = None                                   # reserved but idle
    await _flow(core, clock, demands)
    # The idle executor keeps only the floor it enforces; the busy one borrows the rest.
    assert _ceilings(log, "exec-a")[-1] == FLOOR
    assert _ceilings(log, "exec-b")[-1] == CAP - FLOOR
    # Its demand returns: it runs at its floor, is seen saturated, and the
    # borrower is reduced (and confirmed) before it is raised.
    log.clear()
    demands[0][2] = CAP * 4
    await _flow(core, clock, demands)
    assert _ceilings(log, "exec-a")[-1] == CAP // 2 == _ceilings(log, "exec-b")[-1]
    assert log.index(("exec-b", "ceiling", CAP // 2)) < log.index(("exec-a", "ceiling", CAP // 2))
    _in_effect_never_exceeds([("exec-a", "ceiling", FLOOR), ("exec-b", "ceiling", CAP - FLOOR)] + log, CAP)


async def test_low_demand_executor_keeps_its_need_and_the_rest_is_lent(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    clock = _clocked(core)
    first = await submit_ledger(core, "item")
    other = await submit_ledger(core, "other", kind="tome")
    demands = await _saturating(core, [first.id, other.id])
    demands[0][2] = CAP // 10                              # a consumer that can only use a tenth
    await _flow(core, clock, demands, seconds=20)
    low, busy = _ceilings(log, "exec-a")[-1], _ceilings(log, "exec-b")[-1]
    assert CAP // 10 <= low <= CAP // 10 * 1.25 + 1       # its measured need, with headroom
    assert busy == CAP - low                               # nothing stranded, nothing over
    # Its source speeds up: it is seen saturated and grows to a fair share.
    demands[0][2] = CAP * 4
    await _flow(core, clock, demands, seconds=20)
    assert _ceilings(log, "exec-a")[-1] == CAP // 2 == _ceilings(log, "exec-b")[-1]
    _in_effect_never_exceeds(log, CAP)


async def test_adding_executor_reduces_existing_ceiling_before_new_native_start(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    await submit_ledger(core, "item")
    assert _ceilings(log, "exec-a") == [CAP]
    log.clear()
    await submit_ledger(core, "other", kind="tome")
    (shrunk,) = _ceilings(log, "exec-a")
    (granted,) = _ceilings(log, "exec-b")
    assert shrunk < CAP and shrunk + granted <= CAP and granted >= FLOOR
    shrink = log.index(("exec-a", "ceiling", shrunk))
    newcomer = log.index(("exec-b", "ceiling", granted))
    start = log.index(("exec-b", "start"))
    assert shrink < newcomer < start


async def test_removing_executor_raises_remaining_ceiling_only_after_positive_release_truth(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    clock = _clocked(core)
    first = await submit_ledger(core, "item")
    other = await submit_ledger(core, "other", kind="tome")
    await _flow(core, clock, await _saturating(core, [first.id, other.id]))
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
    clock = _clocked(core)
    first = await submit_ledger(core, "item")
    other = await submit_ledger(core, "other", kind="tome")
    demands = await _saturating(core, [first.id, other.id])
    await _flow(core, clock, demands)
    core.executors[1].observe_failure = NormalizedError(Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE,
                                                        Stage.RECONCILIATION, retryability=Retryability.BACKOFF)
    await _flow(core, clock, [entry for entry in demands if entry[0] is core.executors[0]])
    assert core.engine.runtime.reserved == frozenset({"exec-a", "exec-b"})
    # Nothing is lent from what the unobservable executor may hold.
    assert _ceilings(log, "exec-a")[-1] == CAP // 2 == _ceilings(log, "exec-b")[-1]


async def test_unreachable_executor_ceiling_is_retained_never_lent(tmp_path, monkeypatch):
    log = []
    core = await _core(tmp_path, monkeypatch, log)
    clock = _clocked(core)
    first = await submit_ledger(core, "item")
    other = await submit_ledger(core, "other", kind="tome")
    await _flow(core, clock, await _saturating(core, [first.id, other.id]))
    b_artifact = await artifact_of(core, other.id)
    calls = len(_ceilings(log, "exec-b"))
    core.executors[1].jobs.clear()  # native work may be gone, but core cannot observe that
    core.executors[1].observe_failure = NormalizedError(Domain.EXECUTOR, Category.EXECUTOR_UNAVAILABLE,
                                                        Stage.RECONCILIATION, retryability=Retryability.BACKOFF)
    await core.engine.reconcile_executions()
    core.engine.configure_runtime_limits(CAP * 2)
    await core.engine.converge_runtime_limits()
    # exec-b keeps the ceiling it may hold; the raise for exec-a fits beside it.
    assert len(_ceilings(log, "exec-b")) == calls and _ceilings(log, "exec-b")[-1] == CAP // 2
    assert _ceilings(log, "exec-a")[-1] == CAP * 2 - CAP // 2
    assert "exec-b" in core.engine.runtime.reserved
    assert (await artifact_of(core, other.id)).execution.attempt_id == b_artifact.execution.attempt_id


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
    cancels = lambda: len([call for call in executor.calls if call[0] == "cancel"])  # noqa: E731
    assert cancels() == 0  # the gate stopped acquisition without cancelling anything
    await core.engine.pause_all()
    fenced = cancels()  # DebridPulse pause fences the one writer: the only cancellation
    assert fenced == 1
    await core.engine.resume_all()
    assert executor.gate[-1] is True  # global resume never releases a cap-held gate
    executor.ceiling_failure = False
    status = await core.engine.converge_runtime_limits()
    assert status.ok and executor.gate[-1] is False and core.engine.runtime.gated == frozenset()
    assert cancels() == fenced  # the runtime-limit owner itself never cancels a writer


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
    assert (await artifact_of(core, first.id)).execution is None  # DebridPulse pause fenced the writer
    core.executors[0].runtime_available = frozenset()  # the share (CAP) is unchanged and still "assigned"
    starts = log.count(("exec-a", "start"))
    second = await submit_ledger(core, "second")
    assert (await artifact_of(core, second.id)).execution is None
    assert log.count(("exec-a", "start")) == starts
    await core.engine.resume(first.id)
    await core.engine.reconcile_executions()
    # Neither a native resume nor a new writer: admission is blocked.
    assert ("exec-a", "resume") not in log
    assert log.count(("exec-a", "start")) == starts
    assert (await artifact_of(core, first.id)).execution is None


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
    shrunk, restored = _ceilings(log, "exec-a")[-2:]
    assert shrunk < CAP and restored == CAP  # shrunk for the newcomer, then restored
    status = core.engine.runtime.status()
    assert status.ok and status.effective == CAP  # the current reserved set is still provably enforced
