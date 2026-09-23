"""DP 1.0.13 post-Usenet corrective pass, work item G.

ONE neutral aggregate download-throughput fact, owned by core, consumed
identically by the topbar and the browser tab.

Gate-1 characterization decided the model. The aria2 executor reports a
truthful rate per execution (``downloadSpeed`` of that gid). The bundled
SABnzbd 5.1.3 does NOT: ``build_queue()`` publishes no per-slot rate at all and
only one service-wide meter (``queue.kbpersec`` from ``BPSMeter.bps``). Per-job
rates must therefore never be fabricated from it, so the smallest neutral
executor-aggregate seam is added -- with an explicit precedence rule that makes
counting the same throughput twice structurally impossible.
"""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from transfers import contracts, models
from transfers.models import ExecutionActivity, ExecutorCapabilities, TransferProgress

BACKEND = Path(__file__).resolve().parents[1]
STATIC = BACKEND.parent / "frontend" / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")

CORE = (BACKEND / "transfers",)


# --- the neutral contract ---------------------------------------------------

def test_a_neutral_executor_aggregate_throughput_contract_exists():
    assert hasattr(models, "ExecutorThroughput")
    assert hasattr(contracts, "ExecutorAggregateThroughput")
    value = models.ExecutorThroughput()
    assert value.bytes_per_second == 0 and value.observed is False
    assert ExecutorCapabilities().aggregate_throughput is False


def test_the_capability_promises_the_operation_at_registration():
    from transfers.registry import _EXECUTOR_CAPABILITIES
    assert _EXECUTOR_CAPABILITIES["aggregate_throughput"] == (contracts.ExecutorAggregateThroughput,)


def test_registration_refuses_an_undeclared_aggregate_throughput_implementation():
    from tests.executor_fakes import LedgerExecutor, ledger_capabilities
    from transfers.registry import IntegrationRegistry

    async def _authorize(*_args):
        return True

    class _Liar(LedgerExecutor):
        aggregate_download_throughput = None

    executor = _Liar(_authorize, capabilities=ledger_capabilities(aggregate_throughput=True))
    with pytest.raises(TypeError):
        IntegrationRegistry().register_executor(executor)

    # ...and an honest one registers.
    honest = LedgerExecutor(_authorize, capabilities=ledger_capabilities(aggregate_throughput=True))
    IntegrationRegistry().register_executor(honest)


def test_the_neutral_seam_carries_no_integration_vocabulary():
    banned = re.compile(r"\bsabnzbd\b|\bsab\b|\busenet\b|\bnzb\b|\bnntp\b", re.I)
    for name in ("models.py", "contracts.py", "registry.py", "runtime_telemetry.py"):
        source = (BACKEND / "transfers" / name).read_text(encoding="utf-8")
        assert not banned.search(source), name


# --- the core-owned meter ---------------------------------------------------

def _meter(**kwargs):
    from transfers.runtime_telemetry import ExecutionThroughputMeter
    return ExecutionThroughputMeter(**kwargs)


def test_an_idle_meter_reports_zero():
    assert _meter().current() == 0


def test_the_meter_sums_every_contributing_executor():
    meter = _meter()
    meter.record({"aria2": 1500, "other": 2500})
    assert meter.current() == 4000


def test_the_meter_is_rebuilt_from_scratch_each_cycle_and_never_retains_a_stale_rate():
    meter = _meter()
    meter.record({"aria2": 9000})
    assert meter.current() == 9000
    meter.record({})
    assert meter.current() == 0


def test_the_meter_reports_zero_once_its_sample_is_older_than_the_permitted_age():
    now = [100.0]
    meter = _meter(clock=lambda: now[0], max_age_seconds=5.0)
    meter.record({"aria2": 4096})
    now[0] = 104.0
    assert meter.current() == 4096
    now[0] = 106.0
    assert meter.current() == 0


def test_the_meter_never_reports_a_negative_rate():
    meter = _meter()
    meter.record({"aria2": -10})
    assert meter.current() == 0


# --- precedence: one contribution per executor, never two -------------------

class _Observation:
    def __init__(self, rate, network_active=True):
        self.progress = TransferProgress(100, 10, rate)
        self.activity = ExecutionActivity(network_active=network_active)


class _PerExecution:
    def __init__(self):
        self.capabilities = ExecutorCapabilities()


class _Aggregating:
    def __init__(self, value=None, error=None):
        # Constructed lazily so the module still imports before the neutral
        # capability exists (this file is written RED).
        self.capabilities = ExecutorCapabilities(aggregate_throughput=True)
        self._value, self._error = value, error
        self.calls = 0

    async def aggregate_download_throughput(self):
        self.calls += 1
        if self._error:
            raise self._error
        return self._value


async def _contribution(executor, observations):
    from transfers._engine_base import TransferEngine
    return await TransferEngine._executor_throughput(executor, observations)


@pytest.mark.asyncio
async def test_a_per_execution_executor_contributes_the_sum_of_its_active_executions():
    assert await _contribution(_PerExecution(), [_Observation(1000), _Observation(2000)]) == 3000


@pytest.mark.asyncio
async def test_a_per_execution_executor_never_counts_an_idle_or_paused_execution():
    observations = [_Observation(1000), _Observation(5000, network_active=False)]
    assert await _contribution(_PerExecution(), observations) == 1000


@pytest.mark.asyncio
async def test_an_aggregating_executor_is_counted_exactly_once_whatever_its_job_count():
    executor = _Aggregating(models.ExecutorThroughput(7000, True))
    observations = [_Observation(1000), _Observation(2000), _Observation(3000)]
    assert await _contribution(executor, observations) == 7000
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_an_unreachable_aggregating_executor_contributes_zero_not_its_last_value():
    assert await _contribution(_Aggregating(error=RuntimeError("unreachable")), [_Observation(9)]) == 0
    assert await _contribution(_Aggregating(models.ExecutorThroughput(5000, False)), [_Observation(9)]) == 0


@pytest.mark.asyncio
async def test_an_aggregating_executor_that_answers_with_the_wrong_shape_contributes_zero():
    assert await _contribution(_Aggregating("fast"), [_Observation(9)]) == 0


# --- the bundled acquisition executor ---------------------------------------

def test_the_usenet_backed_executor_declares_the_neutral_aggregate_capability():
    from executors.sabnzbd.executor import SabnzbdExecutor
    assert SabnzbdExecutor.capabilities.aggregate_throughput is True


def test_the_direct_transfer_executor_keeps_its_truthful_per_execution_rates():
    from executors.aria2.executor import Aria2Executor
    assert Aria2Executor.capabilities.aggregate_throughput is False


@pytest.mark.asyncio
async def test_the_acquisition_client_normalizes_the_native_unit_once():
    from executors.sabnzbd.client import SabnzbdClient, SabEndpoint

    class _Session:
        def __init__(self, payload):
            self.payload = payload

        def __call__(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, *args, **kwargs):
            payload = self.payload

            class _Response:
                status = 200

                async def text(self):
                    return payload

                async def __aenter__(self_inner):
                    return self_inner

                async def __aexit__(self_inner, *exc):
                    return False
            return _Response()

    client = SabnzbdClient(SabEndpoint("http://127.0.0.1:8090", "k"),
                           session_factory=_Session('{"queue": {"kbpersec": "1024.00"}}'))
    assert await client.download_throughput() == 1024 * 1024


@pytest.mark.asyncio
async def test_the_acquisition_executor_reports_unobserved_when_the_service_cannot_answer():
    from executors.sabnzbd.executor import SabnzbdConfiguration, SabnzbdExecutor
    from tests.sab_fakes import FakeSab

    sab = FakeSab()
    executor = SabnzbdExecutor(sab, SabnzbdConfiguration("/d", "/d/w", "/d/c"), lambda *a: asyncio.sleep(0, True))
    sab.download_bytes_per_second = 2048
    assert (await executor.aggregate_download_throughput()) == models.ExecutorThroughput(2048, True)
    sab.reachable = False
    assert (await executor.aggregate_download_throughput()).observed is False
    assert (await executor.aggregate_download_throughput()).bytes_per_second == 0


# --- the neutral presentation route -----------------------------------------

@pytest.mark.asyncio
async def test_a_neutral_application_owned_runtime_status_route_exists():
    from api import routes

    application = SimpleNamespace(execution_runtime_status=lambda: _status())
    result = await routes.get_execution_runtime_status(application=application)
    assert result["ok"] is True
    assert result["download_bytes_per_second"] == 4096
    assert result["active_execution_slots"] == 2
    assert result["max_download_bytes_per_second"] == 1048576


async def _status():
    return {"download_bytes_per_second": 4096, "active_execution_slots": 2,
            "max_download_bytes_per_second": 1048576}


@pytest.mark.asyncio
async def test_the_application_projects_each_value_from_its_one_existing_owner():
    from application.service import ApplicationService

    engine = SimpleNamespace(
        throughput=SimpleNamespace(current=lambda: 8192),
        policy=SimpleNamespace(max_active_executions=4, resolution_concurrency=2),
        runtime=SimpleNamespace(configured=2097152),
        clock=lambda: 1000.0,
        repository=SimpleNamespace(occupied_execution_slots=_slots),
    )
    service = ApplicationService(engine)
    status = await service.execution_runtime_status()
    assert status == {"download_bytes_per_second": 8192, "active_execution_slots": 3,
                      "max_download_bytes_per_second": 2097152}


async def _slots(now, **kwargs):
    assert now == 1000.0
    return 3


# --- the browser consumes ONE neutral fact ----------------------------------

def test_generic_presentation_no_longer_polls_an_executor_specific_route():
    assert "/aria2/global-stat" not in APP_JS
    assert "/aria2/global-options" not in APP_JS
    assert "/execution/runtime-status" in APP_JS


def test_no_generic_presentation_state_owner_is_named_after_an_executor():
    assert "_aria2BadgeState" not in APP_JS
    assert "updateAria2TopbarBadge" not in APP_JS
    assert "loadAria2TopbarStat" not in APP_JS
    for identifier in ("aria2-speed-badge", "aria2-badge-active", "aria2-badge-speed",
                       "aria2-badge-max", "aria2-badge-limit", "aria2-cap-menu", "aria2-cap-toggle"):
        assert identifier not in INDEX, identifier
        assert identifier not in APP_JS, identifier


def test_the_topbar_and_the_browser_tab_read_the_same_value_from_the_same_owner():
    title = APP_JS[APP_JS.index("function renderOperatorTitle("):]
    title = title[:title.index("\n}") + 2]
    assert "_runtimeStatusState" in title
    badge = APP_JS[APP_JS.index("function updateRuntimeStatusBadge("):]
    badge = badge[:badge.index("\n}") + 2]
    assert "_runtimeStatusState" in badge
    # Exactly one writer of the shared state.
    assert APP_JS.count("Object.assign(_runtimeStatusState") == 1


def test_the_speed_cap_display_reads_the_neutral_runtime_limit_owner():
    loader = APP_JS[APP_JS.index("async function loadRuntimeStatus("):]
    loader = loader[:loader.index("\n}") + 2]
    assert "max_download_bytes_per_second" in loader
    # Writes already went to the neutral surface and still do.
    assert "'/execution/runtime-limits'" in APP_JS


def test_executor_diagnostics_are_not_deleted_merely_because_presentation_moved():
    from api import routes
    paths = {route.path for route in routes.router.routes}
    assert "/aria2/global-stat" in paths
    assert "/aria2/global-options" in paths
    assert "/aria2/runtime" in paths
