"""Roadmap Item 10 Settings maintenance-admission regression contract."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_settings_put_is_not_double_admitted_across_starlette_task_boundary():
    main = (ROOT / "backend/main.py").read_text()
    routes = (ROOT / "backend/api/routes.py").read_text()
    assert '_SELF_MAINTAINED_MUTATION_PATHS = {"/api/settings"}' in main
    middleware = main.split("async def application_mutation_admission_middleware", 1)[1].split("@app.exception_handler", 1)[0]
    assert "request.url.path not in _SELF_MAINTAINED_MUTATION_PATHS" in middleware
    settings_route = routes.split('@router.put("/settings")', 1)[1].split("# ── Avatar", 1)[0]
    assert "async with application.configuration_admission():" in settings_route
    assert "application.application_operation()" not in settings_route
