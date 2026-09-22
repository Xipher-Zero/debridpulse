"""DP 1.0.12 canonical architecture correction, Workstream C.

Neutral live executor-runtime bandwidth capability (specification section
4.4): the API surface must be executor-neutral, must not acquire
application-wide maintenance merely because it is persisted, and must expose
configured/effective values distinguishably so an API success never implies a
failed native apply succeeded (specification section 2.7). Existing coverage
this file does not duplicate: the compatibility-edge ``/aria2/global-options``
admission contract (``test_stage10_settings_admission.py``), executor
configuration ownership generally (``test_executor_configuration_ownership.py``).
"""
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "aiofiles" not in sys.modules:
    sys.modules["aiofiles"] = types.SimpleNamespace(open=lambda *a, **kw: None)

from api import routes
from transfers.models import (
    ExecutorCapabilities, ExecutorHealth, ExecutorRuntimeCapability, ExecutorRuntimeControlResult,
)
from transfers.runtime_coordination import ExecutionRuntimeCoordinator
from transfers.runtime_limits import ExecutionRuntimeLimits


@asynccontextmanager
async def _fake_db_context(db=None):
    yield db


class _CeilingExecutor:
    """Any executor that enforces an assigned aggregate ceiling -- the route
    and the core allocator never learn which one."""

    capabilities = ExecutorCapabilities(aggregate_bandwidth_ceiling=True)

    def __init__(self, *, fail=False):
        self.descriptor = SimpleNamespace(id="some-executor", enabled=True, priority=0)
        self.fail = fail
        self.assigned = []

    async def health(self):
        return ExecutorHealth(True, True, frozenset({ExecutorRuntimeCapability.AGGREGATE_BANDWIDTH_CEILING}))

    async def set_bandwidth_ceiling(self, value):
        self.assigned.append(value)
        if self.fail:
            raise RuntimeError("executor runtime unreachable")
        return ExecutorRuntimeControlResult(value, value)


def _application(current, *, executor=None):
    executor = executor or _CeilingExecutor()
    registry = SimpleNamespace(executors={executor.descriptor.id: executor})
    repository = SimpleNamespace(executors_with_live_work=AsyncMock(return_value=frozenset({executor.descriptor.id})))
    coordinator = ExecutionRuntimeCoordinator(lambda: registry, repository)
    coordinator.configure((current.execution_runtime_limits or ExecutionRuntimeLimits()).max_download_bytes_per_second)

    def configure():
        coordinator.configure(current.execution_runtime_limits.max_download_bytes_per_second)

    async def execution_runtime_limits():
        status = await coordinator.converge()
        return {"ok": status.ok, "configured": {"max_download_bytes_per_second": status.configured},
                "effective": {"max_download_bytes_per_second": status.effective},
                "last_apply_error": status.last_apply_error}

    return SimpleNamespace(
        definitions=(), application_operation=lambda: _fake_db_context(None),
        configure=MagicMock(side_effect=configure), execution_runtime_limits=execution_runtime_limits,
        executor=executor,
        integration_admin=MagicMock(side_effect=AssertionError("neutral runtime limits never use an admin surface")),
    )


@pytest.mark.asyncio
async def test_patch_runtime_limits_never_acquires_configuration_admission():
    current = routes.AppSettings()
    application = _application(current)
    saved = {}
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", return_value=current), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings", side_effect=lambda cfg: saved.__setitem__("applied", cfg)):
        result = await routes.patch_execution_runtime_limits(
            {"max_download_bytes_per_second": 5_000_000}, application=application,
        )
    assert result["ok"] is True
    assert result["configured"]["max_download_bytes_per_second"] == 5_000_000
    assert result["effective"]["max_download_bytes_per_second"] == 5_000_000
    assert result["last_apply_error"] is None
    assert application.executor.assigned[-1] == 5_000_000
    assert saved["cfg"].execution_runtime_limits.max_download_bytes_per_second == 5_000_000
    # aria2_max_download_limit is migration input only -- a canonical save
    # must not regenerate it as a persisted mirror (specification sections
    # 4.4, 9.2); it is not even a field of the settings model.
    assert "aria2_max_download_limit" not in saved["cfg"].model_dump()


@pytest.mark.asyncio
async def test_patch_runtime_limits_persists_desired_value_even_when_native_apply_fails():
    """Specification section 2.7: an apply failure must not silently discard
    durable desired configuration, but must never be reported as an
    effective success either."""
    current = routes.AppSettings()
    application = _application(current, executor=_CeilingExecutor(fail=True))
    saved = {}
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", return_value=current), \
         patch("api.routes.save_settings", side_effect=lambda cfg: saved.__setitem__("cfg", cfg)), \
         patch("api.routes.apply_settings", side_effect=lambda cfg: saved.__setitem__("applied", cfg)):
        result = await routes.patch_execution_runtime_limits(
            {"max_download_bytes_per_second": 7_000_000}, application=application,
        )
    assert result["ok"] is False
    assert result["configured"]["max_download_bytes_per_second"] == 7_000_000
    assert result["effective"]["max_download_bytes_per_second"] is None
    assert result["last_apply_error"]
    # The durable desired value is still written so the next convergence/
    # restart may retry application (specification section 9.4).
    assert saved["cfg"].execution_runtime_limits.max_download_bytes_per_second == 7_000_000


@pytest.mark.asyncio
async def test_patch_runtime_limits_rejects_missing_or_invalid_body():
    application = _application(routes.AppSettings())
    with pytest.raises(routes.HTTPException) as excinfo:
        await routes.patch_execution_runtime_limits({}, application=application)
    assert excinfo.value.status_code == 400

    with pytest.raises(routes.HTTPException) as excinfo:
        await routes.patch_execution_runtime_limits(
            {"max_download_bytes_per_second": "not-a-number"}, application=application,
        )
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_get_runtime_limits_reports_configured_and_effective():
    current = routes.AppSettings(execution_runtime_limits=ExecutionRuntimeLimits(max_download_bytes_per_second=3_000_000))
    application = _application(current)
    result = await routes.get_execution_runtime_limits(application=application)
    assert result["ok"] is True
    assert result["configured"]["max_download_bytes_per_second"] == 3_000_000
    assert result["effective"]["max_download_bytes_per_second"] == 3_000_000  # proven across the reserved set
    assert result["last_apply_error"] is None


@pytest.mark.asyncio
async def test_get_runtime_limits_reports_effective_as_unknown_when_live_read_fails():
    """Gate 9 revision-3 rejection finding 6, specification section 2.7:
    effective is UNKNOWN (not silently equal to configured) when enforcement
    cannot be proven on a reserved executor."""
    current = routes.AppSettings(execution_runtime_limits=ExecutionRuntimeLimits(max_download_bytes_per_second=3_000_000))
    application = _application(current, executor=_CeilingExecutor(fail=True))
    result = await routes.get_execution_runtime_limits(application=application)
    assert result["ok"] is False
    assert result["configured"]["max_download_bytes_per_second"] == 3_000_000
    assert result["effective"]["max_download_bytes_per_second"] is None
    assert result["last_apply_error"]


@pytest.mark.asyncio
async def test_patch_runtime_limits_reinjects_runtime_configuration():
    """Gate 9 revision-3 rejection finding 3: a runtime-limit PATCH must
    reinject canonical settings (``application.configure()`` -> the core
    runtime owner) before converging, so no later operation in the same
    process enforces a stale cap."""
    current = routes.AppSettings()
    application = _application(current)
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", return_value=current), \
         patch("api.routes.save_settings"), patch("api.routes.apply_settings"):
        await routes.patch_execution_runtime_limits(
            {"max_download_bytes_per_second": 9_000_000}, application=application,
        )
    application.configure.assert_called_once()
    assert application.executor.assigned[-1] == 9_000_000


def test_runtime_limits_route_source_uses_no_aria2_native_option_names():
    """Specification section 9.6 / Universal Executor Leveling: the neutral
    route accepts a NEUTRAL field and names no executor option at all; only an
    executor implementation maps its assigned ceiling to a native key."""
    source = (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text(encoding="utf-8")
    start = source.index("async def patch_execution_runtime_limits(")
    end = source.index('\n\n@router.', start)
    body = source[start:end]
    assert "max_download_bytes_per_second" in body
    assert body.count("max-overall-download-limit") == 0
    assert "integration_admin" not in body
