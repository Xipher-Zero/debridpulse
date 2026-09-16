from contextlib import asynccontextmanager
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_settings_route_owns_single_configuration_admission_window():
    source = _read("backend/api/routes.py")

    function_start = source.index("async def update_settings(")
    function_end = source.index('\n\n@router.', function_start)
    function_body = source[function_start:function_end]

    assert "async with application.configuration_admission()" in function_body
    assert "application.application_operation()" not in function_body


def test_self_managed_configuration_routes_are_not_double_gated_by_global_mutation_middleware():
    source = _read("backend/main.py")

    paths_start = source.index("_SELF_MAINTAINED_MUTATION_PATHS =")
    paths_end = source.index("_AUTH_MUTATION_PATHS", paths_start)
    paths_block = source[paths_start:paths_end]

    assert '"/api/settings"' in paths_block
    # DP 1.0.12 canonical architecture correction, Workstream C (specification
    # sections 2.6, 9.6): a live bandwidth/concurrency mutation is not an
    # application-wide invariant. /api/aria2/global-options no longer owns a
    # stronger maintenance admission, so it must NOT be excluded here -- the
    # ordinary middleware-level application_operation() wrap applies to it
    # exactly like every other mutation route.
    assert '"/api/aria2/global-options"' not in paths_block
    assert "request.url.path not in _SELF_MAINTAINED_MUTATION_PATHS" in source
    assert "get_application(request).application_operation()" in source


def test_aria2_global_options_route_never_acquires_application_wide_maintenance():
    """Specification sections 2.6, 4.4, 6, 9.6, 13.3: neither a live bandwidth
    change nor a universal-concurrency change may acquire
    ``ApplicationMaintenanceGate`` -- it would otherwise wait behind an
    outstanding resolution/execution operation and, while waiting, reject
    unrelated new mutations for the whole application (the speed-cap
    collision regression)."""
    source = _read("backend/api/routes.py")

    function_start = source.index("async def aria2_set_global_options(")
    function_end = source.index('\n\n@router.', function_start)
    function_body = source[function_start:function_end]

    assert "async with application.application_operation()" in function_body
    assert "application.configuration_admission()" not in function_body


def test_execution_runtime_limits_route_never_acquires_application_wide_maintenance():
    """The neutral runtime-limit mutation surface (specification section 4.4)
    must uphold the same invariant as its compatibility-edge predecessor."""
    source = _read("backend/api/routes.py")

    function_start = source.index("async def patch_execution_runtime_limits(")
    function_end = source.index('\n\n@router.', function_start)
    function_body = source[function_start:function_end]

    assert "async with application.application_operation()" in function_body
    assert "application.configuration_admission()" not in function_body


def test_aria2_global_options_http_request_does_not_deadlock_behind_maintenance(monkeypatch):
    """Regression proof for the speed-cap collision (specification section
    13.3): with an unrelated application-wide maintenance window active, an
    aria2 global-options mutation must be rejected promptly (503, ordinary
    ``ApplicationMaintenanceActive`` admission behavior) rather than the
    request handler entering a SEPARATE, stronger admission that could wait
    on itself across Starlette tasks."""
    import main as main_module
    from services.maintenance_gate import ApplicationMaintenanceActive

    class AdmissionProbe:
        def __init__(self):
            self.application_entries = 0

        @asynccontextmanager
        async def application_operation(self):
            self.application_entries += 1
            raise ApplicationMaintenanceActive("Application maintenance is in progress")
            yield

    async def passthrough(request, call_next, **_kwargs):
        return await call_next(request)

    probe = AdmissionProbe()
    monkeypatch.setattr(main_module.app.state, "application", probe, raising=False)
    monkeypatch.setattr(main_module, "enforce_authentication", passthrough)
    monkeypatch.setattr(main_module, "enforce_general_web_security", passthrough)

    client = TestClient(main_module.app)
    response = client.post("/api/aria2/global-options", json={"max_download_speed": 1000})

    assert response.status_code == 503
    # The middleware's own admission rejects it before the handler runs at
    # all -- exactly one entry attempt, not a second nested one racing it.
    assert probe.application_entries == 1
