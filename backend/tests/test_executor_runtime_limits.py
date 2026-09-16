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
from transfers.runtime_limits import ExecutionRuntimeLimits


@asynccontextmanager
async def _fake_db_context(db=None):
    yield db


def _application(current, *, aria2=None, apply_error=None):
    fake_aria2 = aria2 or SimpleNamespace(
        change_global_options=AsyncMock(side_effect=apply_error),
        get_global_options=AsyncMock(return_value={"max-overall-download-limit": "0"}),
    )
    return SimpleNamespace(
        integration_admin=lambda _identity: fake_aria2,
        definitions=(),
        application_operation=lambda: _fake_db_context(None),
        configure=MagicMock(),
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
    assert saved["cfg"].execution_runtime_limits.max_download_bytes_per_second == 5_000_000
    # aria2_max_download_limit is migration input only -- a canonical save
    # must not regenerate it as an authoritative persisted mirror
    # (specification sections 4.4, 9.2).
    assert saved["cfg"].aria2_max_download_limit == 0


@pytest.mark.asyncio
async def test_patch_runtime_limits_persists_desired_value_even_when_native_apply_fails():
    """Specification section 2.7: an apply failure must not silently discard
    durable desired configuration, but must never be reported as an
    effective success either."""
    current = routes.AppSettings()
    failing_aria2 = SimpleNamespace(
        change_global_options=AsyncMock(side_effect=RuntimeError("aria2 rpc unreachable")),
        get_global_options=AsyncMock(return_value={"max-overall-download-limit": "0"}),
    )
    application = _application(current, aria2=failing_aria2)
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
    with patch("api.routes.get_settings", return_value=current):
        result = await routes.get_execution_runtime_limits(application=application)
    assert result["ok"] is True
    assert result["configured"]["max_download_bytes_per_second"] == 3_000_000
    assert result["effective"]["max_download_bytes_per_second"] == 0  # from the mocked live aria2 read
    assert result["last_apply_error"] is None


@pytest.mark.asyncio
async def test_get_runtime_limits_reports_effective_as_unknown_when_live_read_fails():
    """Gate 9 revision-3 rejection finding 6, specification section 2.7:
    effective is UNKNOWN (not silently equal to configured) when the live
    executor read itself fails."""
    current = routes.AppSettings(execution_runtime_limits=ExecutionRuntimeLimits(max_download_bytes_per_second=3_000_000))
    failing_aria2 = SimpleNamespace(
        change_global_options=AsyncMock(),
        get_global_options=AsyncMock(side_effect=RuntimeError("aria2 rpc unreachable")),
    )
    application = _application(current, aria2=failing_aria2)
    with patch("api.routes.get_settings", return_value=current):
        result = await routes.get_execution_runtime_limits(application=application)
    assert result["ok"] is False
    assert result["configured"]["max_download_bytes_per_second"] == 3_000_000
    assert result["effective"]["max_download_bytes_per_second"] is None
    assert result["last_apply_error"]


@pytest.mark.asyncio
async def test_patch_runtime_limits_reinjects_runtime_configuration():
    """Gate 9 revision-3 rejection finding 3: a runtime-limit PATCH must
    reconfigure (reinject the refreshed ``Aria2RuntimeConfiguration`` into)
    the long-lived aria2 runtime/admin singletons -- otherwise a later
    built-in tuning/restart operation in the same process reapplies the OLD
    bandwidth cap from stale injected state."""
    current = routes.AppSettings()
    application = _application(current)
    with patch("api.routes.get_settings", return_value=current), \
         patch("api.routes.load_settings", return_value=current), \
         patch("api.routes.save_settings"), patch("api.routes.apply_settings"):
        await routes.patch_execution_runtime_limits(
            {"max_download_bytes_per_second": 9_000_000}, application=application,
        )
    application.configure.assert_called_once()


def test_runtime_limits_route_source_uses_no_aria2_native_option_names():
    """Specification section 9.6: the canonical route must accept a NEUTRAL
    field; only the aria2 implementation may map it to the native key."""
    source = (Path(__file__).resolve().parents[1] / "api" / "routes.py").read_text(encoding="utf-8")
    start = source.index("async def patch_execution_runtime_limits(")
    end = source.index('\n\n@router.', start)
    body = source[start:end]
    assert "max_download_bytes_per_second" in body
    # aria2's native option name may appear exactly once: the single
    # translation call site mapping the neutral field onto it.
    assert body.count("max-overall-download-limit") == 1
