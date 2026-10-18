from types import SimpleNamespace

import pytest
from fastapi import Request, Response

from auth.middleware import enforce_authentication


def _request(headers=None):
    # Every HTTP/1.1 request names its authority: the one this scope serves.
    raw_headers = [(b"host", b"pulse.example")] + [
        (str(key).lower().encode("latin-1"), str(value).encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/stats",
            "raw_path": b"/api/stats",
            "query_string": b"",
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("pulse.example", 80),
        }
    )


async def _ok(_request):
    return Response(status_code=204)


def _cfg(*, password=False, oidc=False):
    return SimpleNamespace(
        auth_password_enabled=password,
        auth_username="",
        auth_password_hash="",
        auth_oidc_enabled=oidc,
        oidc_issuer_url="https://id.example/application/o/debridpulse" if oidc else "",
        oidc_client_id="client" if oidc else "",
        oidc_client_secret="",
        oidc_scopes=["openid"],
        oidc_provider_name="OpenID Connect",
        oidc_allow_all=True,
        oidc_allowed_subjects=[],
        oidc_allowed_emails=[],
        oidc_allowed_groups=[],
        oidc_group_claim="groups",
        public_base_url="https://pulse.example" if oidc else "",
    )


@pytest.mark.asyncio
async def test_incidental_basic_header_does_not_close_deliberate_open_mode(monkeypatch):
    import auth.middleware as middleware

    monkeypatch.setattr(middleware, "get_settings", lambda: _cfg())
    request = _request({"Authorization": "Basic !!!invalid!!!"})
    response = await enforce_authentication(request, _ok)
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_basic_header_cannot_bypass_oidc_only_mode(monkeypatch):
    import auth.middleware as middleware

    monkeypatch.setattr(middleware, "get_settings", lambda: _cfg(oidc=True))
    request = _request({"Authorization": "Basic Zm9vOmJhcg=="})
    response = await enforce_authentication(request, _ok)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic realm=")
