import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import Request

from account_support import account, account_database, admin_with_password
from auth.middleware import session_record_valid
from auth.models import AuthMechanism, Principal
from auth.oidc_version import oidc_configuration_version
from auth.pending_oidc import PendingOidcConfigurationStore
from auth.transitions import oidc_critical_change, settings_transition_rejection


def _settings(**updates):
    values = {
        "auth_password_enabled": True,
        "auth_oidc_enabled": True,
        "oidc_provider_name": "Authentik",
        "oidc_issuer_url": "https://id.example/application/o/debridpulse",
        "oidc_client_id": "debridpulse-client",
        "oidc_client_secret": "secret",
        "oidc_scopes": ["openid", "profile", "email"],
        "oidc_allow_all": False,
        "oidc_allowed_subjects": [],
        "oidc_allowed_emails": [],
        "oidc_allowed_groups": ["operators"],
        "oidc_group_claim": "groups",
        "public_base_url": "https://pulse.example",
    }
    values.update(updates)
    return SimpleNamespace(**values)


@pytest_asyncio.fixture
async def operator(tmp_path, monkeypatch):
    """The default Admin, holding a local password."""
    await account_database(tmp_path, monkeypatch)
    return await admin_with_password()


def _request(payload):
    body = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "PUT",
            "scheme": "https",
            "path": "/api/settings",
            "raw_path": b"/api/settings",
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


@pytest.mark.asyncio
async def test_password_session_cannot_make_unverified_oidc_sole_auth():
    current = _settings()
    request = _request({"auth_password_enabled": False, "auth_oidc_enabled": True})
    response = await settings_transition_rejection(
        request,
        Principal.password_session(account("operator")),
        current,
    )
    assert response is not None
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_current_oidc_session_can_disable_password_when_oidc_config_is_unchanged():
    current = _settings()
    request = _request({"auth_password_enabled": False, "auth_oidc_enabled": True})
    response = await settings_transition_rejection(
        request,
        Principal.oidc_session("https://id.example|user-1"),
        current,
    )
    assert response is None


@pytest.mark.asyncio
async def test_even_oidc_session_must_verify_critical_change_before_disabling_password():
    current = _settings()
    request = _request(
        {
            "auth_password_enabled": False,
            "auth_oidc_enabled": True,
            "oidc_client_id": "replacement-client",
        }
    )
    response = await settings_transition_rejection(
        request,
        Principal.oidc_session("https://id.example|user-1"),
        current,
    )
    assert response is not None
    assert response.status_code == 409
    assert b"Verify" in response.body


@pytest.mark.asyncio
async def test_oidc_only_critical_change_requires_pending_verification():
    current = _settings(auth_password_enabled=False)
    request = _request({"oidc_allowed_groups": ["new-operators"]})
    response = await settings_transition_rejection(
        request,
        Principal.oidc_session("https://id.example|user-1"),
        current,
    )
    assert response is not None
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_oidc_only_change_is_allowed_when_same_update_restores_usable_password_fallback(operator):
    current = _settings(auth_password_enabled=False)
    request = _request(
        {
            "auth_password_enabled": True,
            "auth_username": "operator",
            "oidc_allowed_groups": ["new-operators"],
        }
    )
    response = await settings_transition_rejection(
        request,
        Principal.oidc_session("https://id.example|user-1"),
        current,
    )
    assert response is None


@pytest.mark.asyncio
async def test_intentional_open_mode_requires_explicit_confirmation():
    current = _settings()
    principal = Principal.password_session(account("operator"))

    rejected = await settings_transition_rejection(
        _request({"auth_password_enabled": False, "auth_oidc_enabled": False}),
        principal,
        current,
    )
    assert rejected is not None
    assert rejected.status_code == 409

    accepted = await settings_transition_rejection(
        _request(
            {
                "auth_password_enabled": False,
                "auth_oidc_enabled": False,
                "confirm_open_mode": True,
            }
        ),
        principal,
        current,
    )
    assert accepted is None


def test_oidc_critical_change_set_excludes_provider_display_label():
    current = _settings()
    assert oidc_critical_change({"oidc_provider_name": "Keycloak"}, current) is False
    assert oidc_critical_change({"oidc_client_id": "new-client"}, current) is True
    assert oidc_critical_change({"oidc_allowed_emails": ["USER@EXAMPLE.COM"]}, current) is True


def test_oidc_session_version_changes_with_critical_policy_but_not_display_label():
    current = _settings()
    version = oidc_configuration_version(current)
    assert version
    assert oidc_configuration_version(_settings(oidc_provider_name="Keycloak")) == version
    assert oidc_configuration_version(_settings(oidc_allowed_groups=["different"])) != version


def test_oidc_session_admission_rejects_stale_critical_configuration(monkeypatch):
    import auth.middleware as middleware

    current = _settings()
    version = oidc_configuration_version(current)
    record = SimpleNamespace(
        principal=Principal.oidc_session("https://id.example|user-1").bound_to(account("user-1")),
        credential_version=version,
    )
    monkeypatch.setattr(middleware, "_oidc_ready", lambda _cfg: True)
    assert session_record_valid(record, current) is True
    assert session_record_valid(record, _settings(oidc_client_id="changed")) is False


def test_pending_config_store_is_bounded_ephemeral_and_exact_version_bound():
    now = [10.0]
    store = PendingOidcConfigurationStore(ttl_seconds=60, max_entries=1, clock=lambda: now[0])
    first = _settings()
    second = _settings(oidc_client_id="second")
    store.stage("one", first, configuration_version=oidc_configuration_version(first))
    store.stage("two", second, configuration_version=oidc_configuration_version(second))
    assert store.has("one") is False
    assert store.has("two") is True

    # Mutating the staged settings after state creation invalidates the proposal.
    second.oidc_client_id = "tampered"
    assert store.consume_verified("two") is None

    third = _settings(oidc_client_id="third")
    store.stage("three", third, configuration_version=oidc_configuration_version(third))
    now[0] = 71.0
    assert store.consume_verified("three") is None


def test_application_registers_pending_callback_before_normal_callback():
    root = Path(__file__).resolve().parents[1]
    main_source = (root / "main.py").read_text()
    config_routes = (root / "api/auth_config_routes.py").read_text()
    normal_routes = (root / "api/auth_routes.py").read_text()

    assert '@router.get("/auth/oidc/callback", include_in_schema=False)' in config_routes
    assert '@router.get("/auth/oidc/callback")' in normal_routes
    assert main_source.index("app.include_router(auth_config_router)") < main_source.index(
        "app.include_router(auth_router)"
    )
