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
    assert "compute_committed_settings(" in function_body
    assert "await application.initialize_from_settings(committed)" in function_body


def test_self_managed_configuration_routes_are_not_double_gated_by_global_mutation_middleware():
    source = _read("backend/main.py")

    paths_start = source.index("_SELF_MAINTAINED_MUTATION_PATHS =")
    paths_end = source.index("_AUTH_MUTATION_PATHS", paths_start)
    paths_block = source[paths_start:paths_end]

    assert '"/api/settings"' in paths_block
    assert '"/api/aria2/global-options"' in paths_block
    assert "if (" in source
    assert "request.url.path not in _SELF_MAINTAINED_MUTATION_PATHS" in source
    assert "get_application(request).application_operation()" in source


def test_aria2_global_options_route_owns_configuration_admission_window():
    source = _read("backend/api/routes.py")

    function_start = source.index("async def aria2_set_global_options(")
    function_end = source.index('\n\n@router.', function_start)
    function_body = source[function_start:function_end]

    assert "async with application.configuration_admission()" in function_body


def test_aria2_global_options_http_request_skips_outer_application_operation(monkeypatch):
    import main as main_module

    class AdmissionProbe:
        def __init__(self):
            self.configuration_entries = 0
            self.application_entries = 0

        @asynccontextmanager
        async def configuration_admission(self):
            self.configuration_entries += 1
            yield

        @asynccontextmanager
        async def application_operation(self):
            self.application_entries += 1
            raise AssertionError("aria2 global-options must not enter outer application_operation")
            yield

    async def passthrough(request, call_next, **_kwargs):
        return await call_next(request)

    probe = AdmissionProbe()
    monkeypatch.setattr(main_module.app.state, "application", probe, raising=False)
    monkeypatch.setattr(main_module, "enforce_authentication", passthrough)
    monkeypatch.setattr(main_module, "enforce_general_web_security", passthrough)

    client = TestClient(main_module.app)
    response = client.post("/api/aria2/global-options", json={})

    assert response.status_code == 400
    assert response.json()["detail"] == "No valid options provided"
    assert probe.configuration_entries == 1
    assert probe.application_entries == 0
