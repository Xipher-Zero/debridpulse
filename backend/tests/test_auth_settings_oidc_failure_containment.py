import json
from pathlib import Path

import pytest
from fastapi import Request

from api import auth_config_routes
from auth.models import Principal
from auth.oidc import OidcError
from auth.passwords import hash_password
from auth.transitions import settings_transition_rejection
from core.config import AppSettings


ROOT = Path(__file__).resolve().parents[2]
RESILIENCE_JS = ROOT / "frontend" / "static" / "ui-settings-auth-resilience.js"
PRESENTATION_LOADER_JS = ROOT / "frontend" / "static" / "ui-presentation-loader.js"
TEST_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
AUTH_ROUTES = ROOT / "backend" / "api" / "auth_config_routes.py"


def _settings(**updates):
    values = {
        "auth_password_enabled": False,
        "auth_username": "",
        "auth_oidc_enabled": True,
        "oidc_provider_name": "Authentik",
        "oidc_issuer_url": "https://id.example/application/o/debridpulse",
        "oidc_client_id": "client",
        "oidc_client_secret": "stored-secret",
        "oidc_scopes": ["openid", "profile", "email"],
        "oidc_allow_all": True,
        "oidc_allowed_subjects": [],
        "oidc_allowed_emails": [],
        "oidc_allowed_groups": [],
        "oidc_group_claim": "groups",
        "public_base_url": "https://pulse.example",
        "auth_session_lifetime_hours": 12,
    }
    values.update(updates)
    return AppSettings(**values)


def _request(path="/api/auth/config", payload=None, *, method="PUT", principal=None):
    body = json.dumps(payload or {}).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("pulse.example", 443),
        },
        receive=receive,
    )
    request.state.principal = principal or Principal.password_session("operator")
    return request


def _payload(response):
    return json.loads(response.body.decode("utf-8"))


def _install_persistence_stubs(monkeypatch, current):
    saved = []
    applied = []
    monkeypatch.setattr(auth_config_routes, "get_settings", lambda: current)
    monkeypatch.setattr(auth_config_routes, "save_settings", lambda cfg: saved.append(cfg.model_copy(deep=True)))
    monkeypatch.setattr(auth_config_routes, "apply_settings", lambda cfg: applied.append(cfg.model_copy(deep=True)))
    monkeypatch.setattr(auth_config_routes.basic_verification_cache, "clear", lambda: None)
    monkeypatch.setattr(auth_config_routes.session_store, "revoke_mechanism", lambda _mechanism: 0)
    return saved, applied


@pytest.mark.asyncio
async def test_auth_config_get_is_local_when_configured_provider_is_unreachable(monkeypatch):
    cfg = _settings()
    monkeypatch.setattr(auth_config_routes, "get_settings", lambda: cfg)

    async def forbidden_discovery(_config):
        raise AssertionError("GET /auth/config must not contact the provider")

    monkeypatch.setattr(auth_config_routes, "discover_oidc", forbidden_discovery)
    response = await auth_config_routes.get_authentication_config(
        _request(method="GET", payload={})
    )
    data = _payload(response)

    assert response.status_code == 200
    assert data["oidc_enabled"] is True
    assert data["oidc_configured"] is True
    assert data["oidc_available"] is None


@pytest.mark.asyncio
async def test_runtime_status_contains_unreachable_provider_failure(monkeypatch):
    cfg = _settings()
    monkeypatch.setattr(auth_config_routes, "get_settings", lambda: cfg)

    async def failed_discovery(_config):
        raise OidcError("provider unavailable")

    monkeypatch.setattr(auth_config_routes, "discover_oidc", failed_discovery)
    response = await auth_config_routes.get_oidc_runtime_status()
    data = _payload(response)

    assert response.status_code == 200
    assert data == {
        "oidc_enabled": True,
        "oidc_configured": True,
        "oidc_available": False,
    }


@pytest.mark.asyncio
async def test_oidc_enabled_to_disabled_and_cleared_applies_without_discovery(monkeypatch):
    current = _settings()
    saved, applied = _install_persistence_stubs(monkeypatch, current)

    async def forbidden_discovery(_config):
        raise AssertionError("PUT /auth/config must not contact the provider")

    monkeypatch.setattr(auth_config_routes, "discover_oidc", forbidden_discovery)
    update = auth_config_routes.AuthenticationConfigUpdate(
        auth_oidc_enabled=False,
        oidc_provider_name="",
        oidc_issuer_url="",
        oidc_client_id="",
        clear_oidc_client_secret=True,
        oidc_scopes=[],
        oidc_allow_all=False,
        oidc_allowed_subjects=[],
        oidc_allowed_emails=[],
        oidc_allowed_groups=[],
        oidc_group_claim="groups",
        public_base_url="",
        confirm_open_mode=True,
    )
    response = await auth_config_routes.update_authentication_config(
        _request(payload=update.model_dump()), update
    )
    data = _payload(response)

    assert response.status_code == 200
    assert len(saved) == len(applied) == 1
    assert saved[0].auth_oidc_enabled is False
    assert saved[0].oidc_issuer_url == ""
    assert saved[0].oidc_client_id == ""
    assert saved[0].oidc_client_secret == ""
    assert data["oidc_enabled"] is False
    assert data["oidc_configured"] is False
    assert data["oidc_available"] is None


@pytest.mark.asyncio
async def test_oidc_disabled_with_all_configuration_fields_cleared_is_valid(monkeypatch):
    current = _settings(
        auth_oidc_enabled=False,
        oidc_provider_name="OpenID Connect",
        oidc_issuer_url="",
        oidc_client_id="",
        oidc_client_secret="",
        oidc_scopes=[],
        oidc_allow_all=False,
        public_base_url="",
    )
    saved, applied = _install_persistence_stubs(monkeypatch, current)
    update = auth_config_routes.AuthenticationConfigUpdate(
        auth_oidc_enabled=False,
        oidc_provider_name="",
        oidc_issuer_url="",
        oidc_client_id="",
        clear_oidc_client_secret=True,
        oidc_scopes=[],
        oidc_allow_all=False,
        oidc_allowed_subjects=[],
        oidc_allowed_emails=[],
        oidc_allowed_groups=[],
        oidc_group_claim="groups",
        public_base_url="",
    )

    response = await auth_config_routes.update_authentication_config(
        _request(payload=update.model_dump()), update
    )

    assert response.status_code == 200
    assert len(saved) == len(applied) == 1
    assert _payload(response)["oidc_configured"] is False


@pytest.mark.asyncio
async def test_disabled_oidc_accepts_partial_incomplete_local_configuration(monkeypatch):
    current = _settings(auth_oidc_enabled=False)
    saved, _applied = _install_persistence_stubs(monkeypatch, current)
    update = auth_config_routes.AuthenticationConfigUpdate(
        auth_oidc_enabled=False,
        oidc_provider_name="OpenID Connect",
        oidc_issuer_url="",
        oidc_client_id="partial-client-id",
        clear_oidc_client_secret=True,
        oidc_scopes=[],
        public_base_url="",
    )

    response = await auth_config_routes.update_authentication_config(
        _request(payload=update.model_dump()), update
    )

    assert response.status_code == 200
    assert saved[0].auth_oidc_enabled is False
    assert saved[0].oidc_issuer_url == ""
    assert saved[0].oidc_client_id == "partial-client-id"


@pytest.mark.asyncio
async def test_enabled_oidc_with_required_fields_cleared_is_rejected_before_save(monkeypatch):
    current = _settings()
    saved, applied = _install_persistence_stubs(monkeypatch, current)
    update = auth_config_routes.AuthenticationConfigUpdate(
        auth_oidc_enabled=True,
        oidc_issuer_url="",
        oidc_client_id="",
        public_base_url="",
    )

    response = await auth_config_routes.update_authentication_config(
        _request(payload=update.model_dump()), update
    )

    assert response.status_code == 400
    assert "local configuration is complete" in _payload(response)["detail"]
    assert saved == []
    assert applied == []


@pytest.mark.asyncio
async def test_valid_oidc_configuration_saves_even_when_provider_discovery_would_fail(monkeypatch):
    current = _settings(oidc_provider_name="Old Provider")
    saved, _applied = _install_persistence_stubs(monkeypatch, current)

    async def forbidden_discovery(_config):
        raise AssertionError("provider discovery is not part of the save transaction")

    monkeypatch.setattr(auth_config_routes, "discover_oidc", forbidden_discovery)
    update = auth_config_routes.AuthenticationConfigUpdate(
        auth_oidc_enabled=True,
        oidc_provider_name="Renamed Provider",
    )
    response = await auth_config_routes.update_authentication_config(
        _request(payload=update.model_dump()), update
    )

    assert response.status_code == 200
    assert saved[0].oidc_provider_name == "Renamed Provider"
    assert _payload(response)["oidc_available"] is None


@pytest.mark.asyncio
async def test_oidc_only_can_transition_to_confirmed_open_mode_and_save_locally(monkeypatch):
    current = _settings()
    update = auth_config_routes.AuthenticationConfigUpdate(
        auth_oidc_enabled=False,
        oidc_issuer_url="",
        oidc_client_id="",
        clear_oidc_client_secret=True,
        public_base_url="",
        confirm_open_mode=True,
    )
    principal = Principal.oidc_session("issuer|subject")
    request = _request(payload=update.model_dump(), principal=principal)
    rejected = await settings_transition_rejection(request, principal, current)
    assert rejected is None

    saved, _applied = _install_persistence_stubs(monkeypatch, current)
    response = await auth_config_routes.update_authentication_config(request, update)
    assert response.status_code == 200
    assert saved[0].auth_oidc_enabled is False


@pytest.mark.asyncio
async def test_oidc_can_be_removed_while_retaining_password_authentication(monkeypatch):
    current = _settings(
        auth_password_enabled=True,
        auth_username="operator",
        auth_password_hash=hash_password("test-password"),
    )
    update = auth_config_routes.AuthenticationConfigUpdate(
        auth_password_enabled=True,
        auth_username="operator",
        auth_oidc_enabled=False,
        oidc_issuer_url="",
        oidc_client_id="",
        clear_oidc_client_secret=True,
        public_base_url="",
    )
    principal = Principal.password_session("operator")
    request = _request(payload=update.model_dump(), principal=principal)
    rejected = await settings_transition_rejection(request, principal, current)
    assert rejected is None

    saved, _applied = _install_persistence_stubs(monkeypatch, current)
    response = await auth_config_routes.update_authentication_config(request, update)
    data = _payload(response)

    assert response.status_code == 200
    assert saved[0].auth_password_enabled is True
    assert saved[0].auth_oidc_enabled is False
    assert data["password_enabled"] is True
    assert data["oidc_enabled"] is False


def test_configuration_payload_and_mutation_paths_do_not_own_live_discovery():
    source = AUTH_ROUTES.read_text(encoding="utf-8")
    payload_block = source.split("async def _authentication_payload", 1)[1].split("\ndef _build_proposed_settings", 1)[0]
    get_block = source.split('@router.get("/api/auth/config")', 1)[1].split('@router.get("/api/auth/oidc/runtime-status")', 1)[0]
    put_block = source.split('@router.put("/api/auth/config")', 1)[1].split('@router.get("/api/auth/api-token")', 1)[0]
    runtime_block = source.split('@router.get("/api/auth/oidc/runtime-status")', 1)[1].split('@router.put("/api/auth/config")', 1)[0]

    assert "discover_oidc" not in payload_block
    assert "_oidc_runtime_available" not in payload_block
    assert '"oidc_available": None' in payload_block
    assert "_oidc_runtime_available" not in get_block
    assert "discover_oidc" not in put_block
    assert "_authentication_payload(request, cfg=clean)" in put_block
    assert "_oidc_runtime_available" in runtime_block


def test_settings_bootstrap_renders_from_settings_before_auth_enrichment():
    source = RESILIENCE_JS.read_text(encoding="utf-8")
    loader = PRESENTATION_LOADER_JS.read_text(encoding="utf-8")

    assert "/ui-settings-page.js?v=4" in loader
    assert "/ui-settings-auth-resilience.js?v=1" in loader
    assert loader.index("/ui-settings-page.js?v=4") < loader.index("/ui-settings-auth-resilience.js?v=1")

    assert "const settingsPromise = baseApi('GET', '/settings', undefined, 10000);" in source
    assert "const authPromise = baseApi('GET', '/auth/config', undefined, 7000);" in source
    assert "settings = await settingsPromise;" in source
    assert "await renderWithSnapshots(settings, fallbackAuthFromSettings(settings));" in source
    assert "authPromise.then(async auth =>" in source
    assert source.index("await renderWithSnapshots(settings, fallbackAuthFromSettings(settings));") < source.index("authPromise.then(async auth =>")


def test_reload_after_removing_oidc_uses_local_settings_snapshot_without_auth_wait():
    source = RESILIENCE_JS.read_text(encoding="utf-8")

    assert "const oidcEnabled = !!settings?.auth_oidc_enabled;" in source
    assert "const oidcConfigured = oidcEnabled ||" in source
    assert "oidc_available: null" in source
    assert "await renderWithSnapshots(settings, fallbackAuthFromSettings(settings));" in source
    bootstrap = source.split("async function resilientLoadSettings()", 1)[1].split("window.api = observedApi", 1)[0]
    before_render = bootstrap.split("await renderWithSnapshots(settings, fallbackAuthFromSettings(settings));", 1)[0]
    assert "await authPromise" not in before_render


def test_auth_enrichment_failure_is_contained_and_navigation_away_cannot_repaint():
    source = RESILIENCE_JS.read_text(encoding="utf-8")

    assert "if (generation !== loadGeneration || !settingsActive()) return;" in source
    assert "if (!settingsActive()) return;" in source
    assert "markAuthUnavailable(error);" in source
    assert "Authentication status unavailable" in source
    assert "Other Settings remain available." in source
    assert "view.innerHTML = '<div class=\"dp-settings-load-error\"" in source


def test_oidc_runtime_status_is_independent_and_failure_only_degrades_kpi():
    source = RESILIENCE_JS.read_text(encoding="utf-8")

    assert "baseApi('GET', '/auth/oidc/runtime-status', undefined, 5000)" in source
    assert "applyOidcRuntimeStatus(latestAuth, false);" in source
    assert "Runtime Unavailable" in source
    assert "kpi.dataset.c = 'red';" in source
    assert "if (!auth?.oidc_enabled || !auth?.oidc_configured) return;" in source


def test_auth_resilience_runtime_is_covered_by_frontend_syntax_gate():
    workflow = TEST_WORKFLOW.read_text(encoding="utf-8")
    assert "node --check frontend/static/ui-settings-auth-resilience.js" in workflow
