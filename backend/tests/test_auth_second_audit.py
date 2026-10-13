import base64
from types import SimpleNamespace

import pytest
from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError

from account_support import account_database, admin_with_password
from auth import middleware
from auth.accounts import set_own_local_credential
from auth.manager import PasswordAttemptAdmission, PasswordAuthenticationBusy
from auth.models import Principal
from auth.sessions import HTTP_SESSION_COOKIE, session_store


def _request(method="GET", path="/api/stats", *, headers=None, scheme="https", body=b""):
    raw_headers = [
        (str(key).lower().encode("latin-1"), str(value).encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
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
            "method": method,
            "scheme": scheme,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("pulse.example", 443 if scheme == "https" else 80),
        },
        receive=receive,
    )


async def _ok(_request):
    return Response(status_code=204)


def _password_settings():
    return SimpleNamespace(auth_password_enabled=True, auth_oidc_enabled=False)


def _basic(username="operator", password="secret"):
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


@pytest.mark.asyncio
async def test_browser_mutation_origin_requires_same_scheme_and_authority():
    downgraded = _request(
        "POST",
        headers={
            "Host": "pulse.example",
            "Origin": "http://pulse.example",
            "Sec-Fetch-Site": "same-site",
        },
        scheme="https",
    )
    response = await middleware.enforce_general_web_security(downgraded, _ok)
    assert response.status_code == 403

    same_origin = _request(
        "POST",
        headers={
            "Host": "pulse.example",
            "Origin": "https://pulse.example",
            "Sec-Fetch-Site": "same-origin",
        },
        scheme="https",
    )
    assert (await middleware.enforce_general_web_security(same_origin, _ok)).status_code == 204


@pytest.mark.asyncio
async def test_explicit_cors_origin_remains_scheme_exact():
    allowed = _request(
        "POST",
        headers={
            "Host": "pulse.example",
            "Origin": "https://automation.example",
            "Sec-Fetch-Site": "cross-site",
        },
        scheme="https",
    )
    assert (
        await middleware.enforce_general_web_security(
            allowed,
            _ok,
            allowed_origins=["https://automation.example"],
        )
    ).status_code == 204

    wrong_scheme = _request(
        "POST",
        headers={
            "Host": "pulse.example",
            "Origin": "http://automation.example",
            "Sec-Fetch-Site": "cross-site",
        },
        scheme="https",
    )
    assert (
        await middleware.enforce_general_web_security(
            wrong_scheme,
            _ok,
            allowed_origins=["https://automation.example"],
        )
    ).status_code == 403


def test_password_attempt_admission_bounds_global_and_per_peer_waiters():
    admission = PasswordAttemptAdmission(max_pending=3, max_per_peer=2)
    assert admission.acquire("peer-a") is True
    assert admission.acquire("peer-a") is True
    assert admission.acquire("peer-a") is False
    assert admission.acquire("peer-b") is True
    assert admission.acquire("peer-c") is False
    assert admission.pending == 3

    admission.release("peer-a")
    assert admission.acquire("peer-c") is True
    assert admission.pending == 3

    admission.release("peer-a")
    admission.release("peer-b")
    admission.release("peer-c")
    assert admission.pending == 0


@pytest.mark.asyncio
async def test_basic_auth_saturation_fails_fast_with_retry_after(monkeypatch):
    cfg = _password_settings()
    monkeypatch.setattr(middleware, "get_settings", lambda: cfg)

    async def busy(*_args, **_kwargs):
        raise PasswordAuthenticationBusy

    monkeypatch.setattr(middleware, "verify_local_credentials", busy)
    request = _request(
        "GET",
        headers={"Host": "pulse.example", "Authorization": _basic()},
    )
    response = await middleware.enforce_authentication(request, _ok)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "2"


@pytest.mark.asyncio
async def test_password_session_is_invalid_after_username_change(monkeypatch, tmp_path):
    await account_database(tmp_path, monkeypatch)
    operator = await admin_with_password("old-operator")
    monkeypatch.setattr(middleware, "get_settings", _password_settings)
    session_store.clear()
    token, _record = session_store.create(Principal.password_session(operator), lifetime_seconds=3600)
    await set_own_local_credential(operator.id, username="new-operator")
    request = _request(
        "GET",
        path="/",
        headers={
            "Host": "pulse.example",
            "Accept": "text/html",
            "Cookie": f"{HTTP_SESSION_COOKIE}={token}",
        },
    )
    response = await middleware.enforce_authentication(request, _ok)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")
    assert session_store.resolve(token) is None


@pytest.mark.asyncio
async def test_validation_error_handler_never_reflects_rejected_secret():
    import main

    marker = "SUPER-SECRET-VALIDATION-MARKER"
    exc = RequestValidationError(
        [
            {
                "type": "string_too_long",
                "loc": ("body", "oidc_client_secret"),
                "msg": "String should have at most 8192 characters",
                "input": marker,
                "ctx": {"max_length": 8192},
            }
        ]
    )
    response = await main.request_validation_error_handler(None, exc)
    assert response.status_code == 422
    assert marker.encode() not in response.body
    assert b'"input"' not in response.body
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_auth_body_limit_rejects_chunked_payload_before_downstream_buffering():
    import main

    reached_response = False

    async def downstream(scope, receive, send):
        nonlocal reached_response
        while True:
            message = await receive()
            if message.get("type") != "http.request" or not message.get("more_body", False):
                break
        reached_response = True
        await Response(status_code=204)(scope, receive, send)

    limiter = main.RequestBodyLimitMiddleware(downstream, max_bytes=2 * 1024 * 1024)
    chunks = [
        {"type": "http.request", "body": b"a" * (700 * 1024), "more_body": True},
        {"type": "http.request", "body": b"b" * (400 * 1024), "more_body": False},
    ]
    sent = []

    async def receive():
        return chunks.pop(0)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "https",
        "path": "/api/auth/config",
        "raw_path": b"/api/auth/config",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("pulse.example", 443),
    }
    await limiter(scope, receive, send)
    starts = [message for message in sent if message.get("type") == "http.response.start"]
    assert starts and starts[0]["status"] == 413
    assert reached_response is False


def test_body_limiter_is_registered_outside_authentication_and_inside_web_security():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    auth_pos = source.index("async def authentication_boundary_middleware")
    limiter_pos = source.index("app.add_middleware(RequestBodyLimitMiddleware", auth_pos)
    web_pos = source.index("async def general_web_security_middleware", limiter_pos)
    request_id_pos = source.index("async def request_id_middleware", web_pos)
    assert auth_pos < limiter_pos < web_pos < request_id_pos
    assert '"/api/auth/config"' in source
    assert '"/api/auth/oidc/verify-config"' in source
