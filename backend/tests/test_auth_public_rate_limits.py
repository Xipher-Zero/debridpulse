import pytest
from fastapi import Request

from account_support import account
from api import auth_config_routes, auth_routes
from auth.models import Principal
from auth.throttle import DualWindowRateLimiter
from core.config import AppSettings


def _request(path: str, *, peer: str = "127.0.0.1") -> Request:
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST" if path.startswith("/api/") else "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"pulse.example")],
            "client": (peer, 12345),
            "server": ("pulse.example", 443),
        }
    )
    return request


def _oidc_settings():
    return AppSettings(
        auth_password_enabled=True,
        auth_oidc_enabled=True,
        oidc_provider_name="OpenID Connect",
        oidc_issuer_url="https://id.example/application/o/debridpulse",
        oidc_client_id="debridpulse-client",
        oidc_client_secret="secret",
        oidc_scopes=["openid", "profile", "email"],
        oidc_allow_all=True,
        oidc_allowed_subjects=[],
        oidc_allowed_emails=[],
        oidc_allowed_groups=[],
        oidc_group_claim="groups",
        public_base_url="https://pulse.example",
        auth_session_lifetime_hours=12,
    )


def test_dual_window_rate_limiter_enforces_peer_global_and_recovery():
    now = [100.0]
    limiter = DualWindowRateLimiter(
        per_peer_limit=2,
        global_limit=3,
        window_seconds=10,
        clock=lambda: now[0],
    )
    assert limiter.allow("peer-a") is True
    assert limiter.allow("peer-a") is True
    assert limiter.allow("peer-a") is False
    assert limiter.allow("peer-b") is True
    assert limiter.allow("peer-c") is False

    now[0] = 111.0
    assert limiter.allow("peer-a") is True
    assert limiter.allow("peer-c") is True


@pytest.mark.asyncio
async def test_oidc_start_rate_limit_fails_before_discovery(monkeypatch):
    cfg = _oidc_settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: cfg)
    limiter = DualWindowRateLimiter(per_peer_limit=1, global_limit=1, window_seconds=60)
    monkeypatch.setattr(auth_routes, "oidc_start_rate_limiter", limiter)
    calls = []

    async def fake_begin(_cfg, *, return_to):
        calls.append(return_to)
        return "https://id.example/authorize?state=opaque", "correlation"

    monkeypatch.setattr(auth_routes, "begin_oidc_login", fake_begin)
    first = await auth_routes.oidc_start(_request("/auth/oidc/start"), next="/")
    second = await auth_routes.oidc_start(_request("/auth/oidc/start"), next="/")

    assert first.status_code == 303
    assert second.status_code == 429
    assert second.headers["retry-after"] == "60"
    assert calls == ["/"]


async def _native_login_appearance():
    return {"mode": "dark", "palette": "debridpulse-dark"}


@pytest.mark.asyncio
async def test_login_challenge_limit_rejects_without_allocating_csrf_state(monkeypatch):
    cfg = _oidc_settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: cfg)
    monkeypatch.setattr(auth_routes.account_appearance, "login", _native_login_appearance)
    limiter = DualWindowRateLimiter(per_peer_limit=1, global_limit=1, window_seconds=60)
    monkeypatch.setattr(auth_routes, "login_challenge_rate_limiter", limiter)

    issued = []
    monkeypatch.setattr(
        auth_routes.login_csrf_store,
        "issue",
        lambda: issued.append(True) or ("nonce", "token"),
    )
    first = await auth_routes._issue_login_page(_request("/login"), return_to="/")
    second = await auth_routes._issue_login_page(_request("/login"), return_to="/")

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["retry-after"] == "60"
    assert issued == [True]


@pytest.mark.asyncio
async def test_oidc_verification_rate_limit_fails_before_provider_work(monkeypatch):
    cfg = _oidc_settings()
    monkeypatch.setattr(auth_config_routes, "get_settings", lambda: cfg)
    limiter = DualWindowRateLimiter(per_peer_limit=1, global_limit=1, window_seconds=60)
    monkeypatch.setattr(auth_config_routes, "oidc_verify_rate_limiter", limiter)
    calls = []

    async def fake_begin(*_args, **_kwargs):
        calls.append(True)
        return "https://id.example/authorize?state=opaque", "correlation"

    monkeypatch.setattr(auth_config_routes, "begin_oidc_login", fake_begin)
    proposed = auth_config_routes.OidcVerificationRequest()

    first_request = _request("/api/auth/oidc/verify-config")
    first_request.state.principal = Principal.password_session(account("operator"))
    first = await auth_config_routes.verify_pending_oidc_configuration(first_request, proposed)

    second_request = _request("/api/auth/oidc/verify-config")
    second_request.state.principal = Principal.password_session(account("operator"))
    second = await auth_config_routes.verify_pending_oidc_configuration(second_request, proposed)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["retry-after"] == "60"
    assert len(calls) == 1
