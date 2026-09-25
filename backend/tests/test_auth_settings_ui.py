import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Request

from api import auth_config_routes
from auth.models import Principal
from auth.transitions import settings_transition_rejection
from core import config as config_module
from core.config import AppSettings
from core.config_validator import validate_and_sanitise


ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PAGE_JS = ROOT / "frontend" / "static" / "ui-settings-page.js"
AUTH_BOOTSTRAP_JS = ROOT / "frontend" / "static" / "auth.js"


def _settings(**updates):
    values = {
        "auth_password_enabled": True,
        "auth_username": "operator",
        "auth_password_hash": "$argon2id$v=19$m=65536,t=3,p=4$placeholder$placeholder",
        "auth_oidc_enabled": True,
        "oidc_provider_name": "Authentik",
        "oidc_issuer_url": "https://id.example/application/o/debridpulse",
        "oidc_client_id": "client",
        "oidc_client_secret": "super-private-oidc-value",
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


def _request(path, payload, *, principal=None):
    body = json.dumps(payload).encode()
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
            "method": "PUT",
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


def test_authentication_settings_are_owned_by_clean_settings_runtime():
    bootstrap = AUTH_BOOTSTRAP_JS.read_text(encoding="utf-8")
    module = SETTINGS_PAGE_JS.read_text(encoding="utf-8")

    assert "/auth-settings.js" not in bootstrap
    assert "/auth-ux.js" not in bootstrap
    assert "function authenticationPanel(" in module
    assert "window.DPSettingsPage = Object.freeze({load});" in module
    assert "baseRenderSettings" not in module
    assert "removeLegacyAuthenticationControls" not in module

    # Authentication is a field-boundary tab: every control commits through the
    # canonical persistence owner's auth-config scope, and one write carries one
    # field. There is no whole-form authentication payload and no deferred Apply
    # path left to build or replay one.
    assert "function authPayload()" not in module
    assert "function persistAuth(" not in module
    assert "FIELD_BOUNDARY_TABS = new Set(['downloads', 'extraction', 'authentication'])" in module
    assert "persistence.defineScope('auth-config'" in module
    assert "function writeAuthentication(changes)" in module




def test_auth_secret_clear_intents_are_transient_and_not_serialized():
    cfg = AppSettings(
        auth_password_hash_clear=True,
        oidc_client_secret_clear=True,
        auth_password_hash="stored-hash",
        oidc_client_secret="stored-secret",
    )
    dumped = cfg.model_dump()
    assert "auth_password_hash_clear" not in dumped
    assert "oidc_client_secret_clear" not in dumped
    assert "auth_password_hash" not in dumped
    assert "oidc_client_secret" not in dumped


def test_validation_rebuild_preserves_hidden_auth_secret_state():
    cfg = AppSettings(
        discord_avatar_url="data:image/png;base64,invalid",
        auth_password_hash_clear=True,
        oidc_client_secret_clear=True,
    )
    cfg.auth_password_hash = "stored-password-hash"
    cfg.oidc_client_secret = "stored-oidc-secret"

    validated = validate_and_sanitise(cfg)

    assert validated.discord_avatar_url == ""
    assert validated.auth_password_hash == "stored-password-hash"
    assert validated.oidc_client_secret == "stored-oidc-secret"
    assert validated.auth_password_hash_clear is True
    assert validated.oidc_client_secret_clear is True


def test_save_settings_explicitly_clears_password_hash_and_consumes_secret_intent(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    previous = AppSettings(auth_password_enabled=True, auth_username="operator")
    previous.auth_password_hash = "stored-hash"
    previous.oidc_client_secret = "stored-oidc-secret"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config_module, "_settings", previous)

    candidate = previous.model_copy(deep=True)
    candidate.auth_password_hash_clear = True
    candidate.oidc_client_secret_clear = True
    config_module.save_settings(candidate)

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["auth_password_hash"] == ""
    assert persisted["oidc_client_secret"] == ""
    assert candidate.auth_password_hash == ""
    assert candidate.oidc_client_secret == ""
    assert candidate.auth_password_hash_clear is False
    assert candidate.oidc_client_secret_clear is False


@pytest.mark.asyncio
async def test_dedicated_auth_config_uses_same_password_disable_lockout_rule():
    current = _settings()
    response = await settings_transition_rejection(
        _request(
            "/api/auth/config",
            {"auth_password_enabled": False, "auth_oidc_enabled": True},
            principal=Principal.password_session("operator"),
        ),
        Principal.password_session("operator"),
        current,
    )
    assert response is not None
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_dedicated_auth_config_requires_open_mode_confirmation():
    current = _settings()
    rejected = await settings_transition_rejection(
        _request(
            "/api/auth/config",
            {"auth_password_enabled": False, "auth_oidc_enabled": False},
        ),
        Principal.password_session("operator"),
        current,
    )
    assert rejected is not None
    assert rejected.status_code == 409

    accepted = await settings_transition_rejection(
        _request(
            "/api/auth/config",
            {
                "auth_password_enabled": False,
                "auth_oidc_enabled": False,
                "confirm_open_mode": True,
            },
        ),
        Principal.password_session("operator"),
        current,
    )
    assert accepted is None


@pytest.mark.asyncio
async def test_partial_auth_config_write_still_requires_open_mode_confirmation():
    """The field-boundary shape must hit the same gate as the whole payload.

    Settings now writes ONE field per request, so the mutation that disables the
    last interactive mechanism names only that field -- the other mechanism's
    key is absent, not False. The guard resolves an absent key from current
    state, so this is still an open-mode transition and is still refused without
    explicit proof. A frontend that drops the confirmation is therefore rejected
    rather than silently opening the installation.
    """
    # Password is the ONLY enabled mechanism, so disabling it opens the
    # installation even though the request never mentions OIDC.
    password_only = _settings(auth_oidc_enabled=False)

    rejected = await settings_transition_rejection(
        _request("/api/auth/config", {"auth_password_enabled": False}),
        Principal.password_session("operator"),
        password_only,
    )
    assert rejected is not None
    assert rejected.status_code == 409

    accepted = await settings_transition_rejection(
        _request(
            "/api/auth/config",
            {"auth_password_enabled": False, "confirm_open_mode": True},
        ),
        Principal.password_session("operator"),
        password_only,
    )
    assert accepted is None

    # The mirror image: OIDC as the last mechanism, in the same partial shape.
    oidc_only = _settings(auth_password_enabled=False)
    assert (
        await settings_transition_rejection(
            _request("/api/auth/config", {"auth_oidc_enabled": False}),
            Principal.oidc_session("operator"),
            oidc_only,
        )
    ).status_code == 409
    assert await settings_transition_rejection(
        _request(
            "/api/auth/config",
            {"auth_oidc_enabled": False, "confirm_open_mode": True},
        ),
        Principal.oidc_session("operator"),
        oidc_only,
    ) is None

    # And disabling ONE of two enabled mechanisms is not an open-mode
    # transition at all, so it needs no proof.
    assert await settings_transition_rejection(
        _request("/api/auth/config", {"auth_oidc_enabled": False}),
        Principal.password_session("operator"),
        _settings(),
    ) is None


@pytest.mark.asyncio
async def test_authentication_payload_is_secret_free(monkeypatch):
    cfg = _settings()
    monkeypatch.setattr(auth_config_routes, "get_settings", lambda: cfg)
    monkeypatch.setattr(auth_config_routes, "_oidc_runtime_available", lambda *_args: _false_async())
    monkeypatch.setattr(
        auth_config_routes,
        "api_token_store",
        SimpleNamespace(enabled=True, configured=True),
    )

    request = _request("/api/auth/config", {})
    request.state.principal = Principal.oidc_session("issuer|subject")
    payload = await auth_config_routes._authentication_payload(request)
    encoded = json.dumps(payload)

    assert payload["mode"] == "Username & Password + OIDC"
    assert payload["password_configured"] is True
    assert payload["oidc_client_secret_configured"] is True
    assert payload["api_token_configured"] is True
    assert payload["current_session_mechanism"] == "oidc_session"
    assert "super-private-oidc-value" not in encoded
    assert "placeholder$placeholder" not in encoded
    assert "oidc_client_secret" not in payload
    assert "auth_password_hash" not in payload


async def _false_async():
    return False
