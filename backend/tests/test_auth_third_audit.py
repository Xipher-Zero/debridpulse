import asyncio
import base64
import json
import os
import stat
import threading
from types import SimpleNamespace

import pytest
import pytest_asyncio
from pydantic import ValidationError
from fastapi import Request, Response

from account_support import account, account_database, admin_with_password
from api.auth_config_routes import AuthenticationConfigUpdate
from auth import oidc, passwords, transitions
from auth.models import Principal
from auth.sessions import session_store
from core import secure_files


def _request(body: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "PUT", "scheme": "https", "path": "/api/auth/config",
        "raw_path": b"/api/auth/config", "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345), "server": ("pulse.example", 443),
    }, receive=receive)


def _password_only_settings():
    return SimpleNamespace(auth_password_enabled=True, auth_oidc_enabled=False)


@pytest_asyncio.fixture
async def operator(tmp_path, monkeypatch):
    """The default Admin, signing in as operator/old-secret."""
    await account_database(tmp_path, monkeypatch)
    return await admin_with_password("operator", "old-secret")


@pytest.mark.asyncio
async def test_transition_guard_matches_pydantic_false_string_coercion_for_open_mode():
    body = json.dumps({"auth_password_enabled": "false", "auth_oidc_enabled": "false"}).encode()
    # Settings Batch 1, Item 3: the route's body model is strict, so the string
    # is refused (422) before any change; the transition guard, which runs
    # before it on the raw body, still reads the string exactly as before.
    with pytest.raises(ValidationError):
        AuthenticationConfigUpdate.model_validate_json(body)
    rejection = await transitions.settings_transition_rejection(
        _request(body), Principal.password_session(account("operator")), _password_only_settings()
    )
    assert rejection is not None and rejection.status_code == 409


@pytest.mark.asyncio
async def test_transition_guard_matches_pydantic_true_string_for_password_clear(operator):
    body = json.dumps({
        "auth_password_enabled": True, "auth_oidc_enabled": False, "clear_password": "true"
    }).encode()
    # Settings Batch 1, Item 3: the route's body model is strict, so the string
    # is refused (422) before any change; the transition guard, which runs
    # before it on the raw body, still reads the string exactly as before.
    with pytest.raises(ValidationError):
        AuthenticationConfigUpdate.model_validate_json(body)
    rejection = await transitions.settings_transition_rejection(
        _request(body), Principal.password_session(operator), _password_only_settings()
    )
    assert rejection is not None and rejection.status_code == 409


def test_oidc_email_allowlist_requires_verified_email_and_requests_userinfo_completion():
    cfg = oidc.OidcConfiguration(
        issuer="https://id.example/application/o/debridpulse", client_id="client",
        client_secret="", scopes=("openid", "email"),
        callback_url="https://pulse.example/auth/oidc/callback", provider_name="OIDC",
        allow_all=False, allowed_subjects=(), allowed_emails=("operator@example.com",),
        allowed_groups=(), group_claim="groups",
    )
    claims = {"iss": cfg.issuer, "sub": "user-1", "email": "operator@example.com"}
    assert oidc._claims_need_userinfo(cfg, claims) is True
    with pytest.raises(oidc.OidcAuthorizationError):
        oidc.authorize_oidc_claims(cfg, claims)
    verified = {**claims, "email_verified": True}
    assert oidc._claims_need_userinfo(cfg, verified) is False
    assert oidc.authorize_oidc_claims(cfg, verified).authenticated is True


@pytest.mark.asyncio
async def test_cancelled_password_request_does_not_release_live_worker_slot(monkeypatch):
    semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(passwords, "_PASSWORD_VERIFY_SLOTS", semaphore)
    release_first = threading.Event()
    release_second = threading.Event()
    first_started = threading.Event()
    second_started = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def blocking_verify(*_args, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            first_started.set()
            release_first.wait(timeout=5)
        else:
            second_started.set()
            release_second.wait(timeout=5)
        return False

    monkeypatch.setattr(passwords, "verify_password_candidate", blocking_verify)
    first = asyncio.create_task(passwords.verify_password_candidate_async("hash", "pw", use_configured_hash=False))
    assert await asyncio.to_thread(first_started.wait, 1.0) is True

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    # Repeated cancellation of the already-cancelled request must not affect
    # ownership of the still-running worker's semaphore slot.
    first.cancel()

    second = asyncio.create_task(passwords.verify_password_candidate_async("hash", "pw2", use_configured_hash=False))
    await asyncio.sleep(0.1)
    assert second_started.is_set() is False
    assert semaphore.locked() is True

    release_first.set()
    assert await asyncio.to_thread(second_started.wait, 1.0) is True
    release_second.set()
    assert await second is False


def test_sensitive_atomic_json_is_0600_before_payload_write_even_without_chmod(tmp_path, monkeypatch):
    target = tmp_path / "config" / "secret.json"
    observed_modes = []
    real_dump = secure_files.json.dump

    def inspect_dump(payload, handle, **kwargs):
        observed_modes.append(stat.S_IMODE(os.fstat(handle.fileno()).st_mode))
        return real_dump(payload, handle, **kwargs)

    def chmod_unavailable(*_args, **_kwargs):
        raise OSError("chmod unavailable")

    monkeypatch.setattr(secure_files.json, "dump", inspect_dump)
    monkeypatch.setattr(secure_files.os, "chmod", chmod_unavailable)
    monkeypatch.setattr(secure_files.os, "fchmod", chmod_unavailable)
    old_umask = os.umask(0)
    try:
        secure_files.atomic_write_json(target, {"secret": "value"}, indent=2)
    finally:
        os.umask(old_umask)
    assert observed_modes == [0o600]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_sensitive_config_writer_uses_secure_atomic_helper():
    root = os.path.dirname(os.path.dirname(__file__))
    config_source = open(os.path.join(root, "core", "config.py"), encoding="utf-8").read()
    assert "atomic_write_json(CONFIG_PATH, data, indent=2)" in config_source


@pytest.mark.asyncio
async def test_transition_guard_matches_pydantic_true_string_for_open_mode_confirmation():
    body = json.dumps({
        "auth_password_enabled": "false",
        "auth_oidc_enabled": "false",
        "confirm_open_mode": "true",
    }).encode()
    # Settings Batch 1, Item 3: the route's body model is strict, so the string
    # is refused (422) before any change; the transition guard, which runs
    # before it on the raw body, still reads the string exactly as before.
    with pytest.raises(ValidationError):
        AuthenticationConfigUpdate.model_validate_json(body)
    rejection = await transitions.settings_transition_rejection(
        _request(body),
        Principal.password_session(account("operator")),
        _password_only_settings(),
    )
    assert rejection is None


def test_prospective_oidc_readiness_does_not_treat_false_string_as_allow_all():
    current = SimpleNamespace(
        auth_oidc_enabled=False,
        oidc_provider_name="OIDC",
        oidc_issuer_url="https://id.example/application/o/debridpulse/",
        oidc_client_id="client",
        oidc_client_secret="",
        oidc_scopes=["openid", "email"],
        public_base_url="https://pulse.example",
        oidc_allow_all=False,
        oidc_allowed_subjects=[],
        oidc_allowed_emails=[],
        oidc_allowed_groups=[],
        oidc_group_claim="groups",
    )
    payload = {
        "auth_oidc_enabled": "true",
        "oidc_allow_all": "false",
    }
    assert transitions._prospective_oidc_ready(payload, current) is False



def _basic_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


def _password_cfg(*, enabled: bool = True):
    return SimpleNamespace(auth_password_enabled=enabled, auth_oidc_enabled=False, auth_session_lifetime_hours=12)


def _verify_then_rotate(module, monkeypatch):
    """The real verification, during which the password is replaced."""
    real_verify = module.verify_local_credentials

    async def verify_then_rotate(*args, **kwargs):
        proven = await real_verify(*args, **kwargs)
        assert proven is not None
        await admin_with_password("operator", "new-secret")
        return proven

    monkeypatch.setattr(module, "verify_local_credentials", verify_then_rotate)


def _generic_request(method: str, path: str, *, headers=None, body: bytes = b""):
    sent = False
    raw_headers = [
        (str(key).lower().encode("latin-1"), str(value).encode("latin-1"))
        for key, value in (headers or {}).items()
    ]

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "https", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": raw_headers,
        "client": ("127.0.0.1", 12345), "server": ("pulse.example", 443),
    }, receive=receive)


@pytest.mark.asyncio
async def test_http_basic_rejects_password_proof_that_became_stale_during_verification(operator, monkeypatch):
    import auth.middleware as middleware

    monkeypatch.setattr(middleware, "get_settings", _password_cfg)
    _verify_then_rotate(middleware, monkeypatch)
    called = False

    async def admitted(_request):
        nonlocal called
        called = True
        return Response(content="ok")

    request = _generic_request(
        "GET",
        "/api/stats",
        headers={"Host": "pulse.example", "Authorization": _basic_header("operator", "old-secret")},
    )
    response = await middleware.enforce_authentication(request, admitted)
    assert response.status_code == 401
    assert called is False
    assert request.state.principal.authenticated is False


@pytest.mark.asyncio
async def test_password_login_does_not_issue_session_from_stale_verified_snapshot(operator, monkeypatch):
    from urllib.parse import urlencode
    from api import auth_routes

    monkeypatch.setattr(auth_routes, "get_settings", _password_cfg)
    monkeypatch.setattr(auth_routes.login_csrf_store, "consume", lambda *_args, **_kwargs: True)
    _verify_then_rotate(auth_routes, monkeypatch)
    session_store.clear()
    form = urlencode({
        "username": "operator",
        "password": "old-secret",
        "csrf_token": "valid",
        "next": "/",
    }).encode()
    request = _generic_request(
        "POST",
        "/login",
        headers={
            "Host": "pulse.example",
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(form)),
        },
        body=form,
    )
    response = await auth_routes.password_login(request)
    assert response.status_code == 409
    assert session_store.size == 0
