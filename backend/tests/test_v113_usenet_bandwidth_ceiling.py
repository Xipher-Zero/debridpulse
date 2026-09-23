"""1.0.13 Gate-9 remediation, finding 6: the neutral aggregate-bandwidth seam.

The global cap C is divided equally by the number N of executors currently
holding a bandwidth reservation; each gets C/N and distributes it internally.
0 means unlimited. Unused share inside the active set is never borrowed.

Core already owns that policy (`transfers/runtime_coordination.py`); these
tests prove the SAB-backed executor participates through the existing neutral
seam and reports SAB's real, characterized limits truthfully.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from transfers.models import ExecutorRuntimeCapability, ExecutorRuntimeControlResult
from transfers.runtime_coordination import ExecutionRuntimeCoordinator

from sab_fakes import FakeSab
from test_v113_sabnzbd_executor import build, lab  # noqa: F401


# --- the executor's own side of the seam ---------------------------------

def test_executor_declares_the_aggregate_ceiling_capability(lab):
    assert lab.executor.capabilities.aggregate_bandwidth_ceiling is True


@pytest.mark.asyncio
async def test_health_reports_the_ceiling_available_while_reachable(lab):
    health = await lab.executor.health()
    assert ExecutorRuntimeCapability.AGGREGATE_BANDWIDTH_CEILING in health.available_runtime_capabilities
    lab.sab.reachable = False
    assert (await lab.executor.health()).available_runtime_capabilities == frozenset()


@pytest.mark.asyncio
async def test_zero_means_unlimited_and_is_confirmed(lab):
    result = await lab.executor.set_bandwidth_ceiling(0)
    assert result.error is None
    assert result.effective_bytes_per_second == 0
    assert lab.sab.speedlimit_abs == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [101, 1024, 512_000, 10 * 1024 * 1024, 30 * 1024 * 1024])
async def test_a_representable_ceiling_is_applied_and_read_back(lab, value):
    result = await lab.executor.set_bandwidth_ceiling(value)
    assert result.error is None
    assert result.effective_bytes_per_second == value
    assert lab.sab.speedlimit_abs == value


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [1, 15, 30, 100])
async def test_an_unrepresentable_ceiling_is_reported_unproven_not_silently_ignored(lab, value):
    """SAB reinterprets 1..100 as a PERCENTAGE and applies no absolute limit.

    Claiming enforcement there would be a lie, so the executor reports the
    effective value as unproven and core fails closed.
    """
    result = await lab.executor.set_bandwidth_ceiling(value)
    assert result.requested_bytes_per_second == value
    assert result.effective_bytes_per_second is None
    assert result.error is not None


@pytest.mark.asyncio
async def test_an_unreachable_service_cannot_confirm_a_ceiling(lab):
    lab.sab.reachable = False
    result = await lab.executor.set_bandwidth_ceiling(1024)
    assert result.effective_bytes_per_second is None
    assert result.error is not None


@pytest.mark.asyncio
async def test_a_percentage_base_never_changes_how_dp_expresses_a_ceiling(lab):
    """Characterized against the dedicated service, with and without a
    configured line rate (`bandwidth_max`):

        bandwidth_max unset   50 -> 0 (percent of nothing)   1024 -> 1024
        bandwidth_max 10M     50 -> 5242880 (50%)            1024 -> 1024

    A value of 101 or more is absolute in BOTH modes, so DebridPulse's
    encoding is unambiguous however the service is configured; only 1..100
    remains inexpressible, which is reported unproven.
    """
    for base in ("", "10M"):
        lab.sab.bandwidth_max = base
        assert (await lab.executor.set_bandwidth_ceiling(1024)).effective_bytes_per_second == 1024
        assert (await lab.executor.set_bandwidth_ceiling(0)).effective_bytes_per_second == 0
        assert (await lab.executor.set_bandwidth_ceiling(50)).effective_bytes_per_second is None


def test_debridpulse_never_configures_a_percentage_base():
    """The runtime must not set a line rate: DP assigns absolute ceilings."""
    from executors.sabnzbd import runtime
    import inspect
    assert "bandwidth_max" not in inspect.getsource(runtime)


# --- the core division: C/N ----------------------------------------------

class _Acceptor:
    """A second executor that can enforce any assigned ceiling."""
    def __init__(self, identity):
        from transfers.models import ExecutorCapabilities, ExecutorHealth, IntegrationDescriptor
        self.descriptor = IntegrationDescriptor(identity, identity, frozenset())
        self.capabilities = ExecutorCapabilities(aggregate_bandwidth_ceiling=True)
        self.assigned = None
        self._health = ExecutorHealth(
            True, True, frozenset({ExecutorRuntimeCapability.AGGREGATE_BANDWIDTH_CEILING}))

    async def health(self):
        return self._health

    async def set_bandwidth_ceiling(self, bytes_per_second: int):
        self.assigned = bytes_per_second
        return ExecutorRuntimeControlResult(bytes_per_second, bytes_per_second)


def coordinator_for(executors, cap):
    registry = SimpleNamespace(executors={e.descriptor.id: e for e in executors})

    class _Repo:
        async def executors_with_live_work(self):
            return frozenset()

    owner = ExecutionRuntimeCoordinator(lambda: registry, _Repo())
    owner.configure(cap)
    return owner


@pytest.mark.asyncio
@pytest.mark.parametrize("count,expected", [(1, 30), (2, 15), (3, 10)])
async def test_the_cap_is_divided_equally_among_reserved_executors(count, expected):
    """30/1 = 30, 30/2 = 15 each, 30/3 = 10 each."""
    executors = [_Acceptor(f"executor-{index}") for index in range(count)]
    owner = coordinator_for(executors, 30)
    for executor in executors:
        assert await owner.admit(executor) is True
    assert [e.assigned for e in executors] == [expected] * count
    assert sum(e.assigned for e in executors) <= 30


@pytest.mark.asyncio
async def test_shares_are_recomputed_as_reservations_are_acquired_and_released():
    one, two, three = (_Acceptor("e1"), _Acceptor("e2"), _Acceptor("e3"))
    owner = coordinator_for([one, two, three], 30)

    assert await owner.admit(one) is True
    assert one.assigned == 30                      # 30/1
    assert await owner.admit(two) is True
    assert (one.assigned, two.assigned) == (15, 15)  # 30/2
    assert await owner.admit(three) is True
    assert (one.assigned, two.assigned, three.assigned) == (10, 10, 10)  # 30/3

    # Releasing one recomputes the split for the survivors; the freed share is
    # redistributed by the owner, never borrowed unilaterally.
    await owner.release_absent(frozenset({"e1", "e2"}))
    assert (one.assigned, two.assigned) == (15, 15)


@pytest.mark.asyncio
async def test_an_unlimited_cap_assigns_zero_to_everyone():
    one, two = _Acceptor("e1"), _Acceptor("e2")
    owner = coordinator_for([one, two], 0)
    assert await owner.admit(one) is True
    assert await owner.admit(two) is True
    assert one.assigned in (None, 0) and two.assigned in (None, 0)


@pytest.mark.asyncio
async def test_the_sab_executor_participates_in_the_real_division(lab):
    """A representable cap is divided and genuinely enforced through SAB."""
    other = _Acceptor("other")
    owner = coordinator_for([lab.executor, other], 30 * 1024 * 1024)
    assert await owner.admit(lab.executor) is True
    assert lab.sab.speedlimit_abs == 30 * 1024 * 1024          # 30M / 1
    assert await owner.admit(other) is True
    assert lab.sab.speedlimit_abs == 15 * 1024 * 1024          # 30M / 2
    assert other.assigned == 15 * 1024 * 1024
