from pathlib import Path

from fastapi import Request, Response
import pytest

from api.auth_routes import _AUTH_PAGE_STYLE, _state_free_auth_page
from auth.middleware import enforce_general_web_security
from auth.sessions import request_is_secure


def _request(*, scheme="http", host="debridpulse.local:8081", origin=None, fetch_site="same-origin"):
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if fetch_site is not None:
        headers.append((b"sec-fetch-site", fetch_site.encode()))
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": scheme,
        "path": "/login",
        "raw_path": b"/login",
        "query_string": b"",
        "headers": headers,
        "client": ("192.168.226.10", 54321),
        "server": ("debridpulse.local", 8081),
    })


async def _ok(_request):
    return Response(content="ok", status_code=200)


@pytest.mark.asyncio
async def test_external_https_origin_is_accepted_behind_http_proxy(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://download.xipherzero.com")
    request = _request(
        scheme="http",
        host="download.xipherzero.com",
        origin="https://download.xipherzero.com",
        fetch_site="same-origin",
    )
    assert request_is_secure(request) is True
    assert (await enforce_general_web_security(request, _ok)).status_code == 200


@pytest.mark.asyncio
async def test_direct_lan_http_remains_same_origin_with_external_base_configured(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://download.xipherzero.com")
    request = _request(
        scheme="http",
        host="192.168.226.200:8081",
        origin="http://192.168.226.200:8081",
        fetch_site="same-origin",
    )
    assert request_is_secure(request) is False
    assert (await enforce_general_web_security(request, _ok)).status_code == 200


@pytest.mark.asyncio
async def test_same_origin_fetch_metadata_does_not_bypass_origin_mismatch(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://download.xipherzero.com")
    request = _request(
        scheme="http",
        host="internal.example:8080",
        origin="https://different.example",
        fetch_site="same-origin",
    )
    assert (await enforce_general_web_security(request, _ok)).status_code == 403


@pytest.mark.asyncio
async def test_null_origin_is_not_accepted_as_same_origin(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://download.xipherzero.com")
    request = _request(
        scheme="http",
        host="download.xipherzero.com",
        origin="null",
        fetch_site="same-origin",
    )
    response = await enforce_general_web_security(request, _ok)
    assert response.status_code == 403
    assert response.body == b"Forbidden origin"


@pytest.mark.asyncio
async def test_cross_site_login_mutation_is_still_rejected(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://download.xipherzero.com")
    request = _request(
        host="download.xipherzero.com",
        origin="https://evil.example",
        fetch_site="cross-site",
    )
    assert (await enforce_general_web_security(request, _ok)).status_code == 403


def test_invalid_public_base_path_is_not_trusted_for_secure_classification(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://download.xipherzero.com/not-an-origin")
    request = _request(host="debridpulse.local:8081")
    assert request_is_secure(request) is False


def test_auth_pages_use_reviewed_debridpulse_login_palettes():
    # Login Batch 1 remains its own composition, but its card, field, border,
    # typography, and accent materials now consume the canonical application
    # dark/light language instead of a parallel auth-only palette.
    assert "--bg:#050814" in _AUTH_PAGE_STYLE
    assert "--bg2:#080d1c" in _AUTH_PAGE_STYLE
    assert "--card:#10182c" in _AUTH_PAGE_STYLE
    assert "--card2:#0b1224" in _AUTH_PAGE_STYLE
    assert "--field:#0d1427" in _AUTH_PAGE_STYLE
    assert "--border:#26324d" in _AUTH_PAGE_STYLE
    assert "--text:#f7f8ff" in _AUTH_PAGE_STYLE
    assert "--accent:#b45cff" in _AUTH_PAGE_STYLE
    assert "--accent2:#3d94ff" in _AUTH_PAGE_STYLE
    assert ':root[data-theme="light"]' in _AUTH_PAGE_STYLE
    assert "--bg:#f4f7fc" in _AUTH_PAGE_STYLE
    assert "--card:#f8faff" in _AUTH_PAGE_STYLE
    assert "--card2:#ffffff" in _AUTH_PAGE_STYLE
    assert "--field:#fbfcff" in _AUTH_PAGE_STYLE
    assert "--border:#dce4f1" in _AUTH_PAGE_STYLE
    assert "--text:#111a34" in _AUTH_PAGE_STYLE
    assert "--accent:#9637f5" in _AUTH_PAGE_STYLE
    assert "--accent2:#2f86ff" in _AUTH_PAGE_STYLE
    assert "#f08a24" not in _AUTH_PAGE_STYLE
    response = _state_free_auth_page(message="Try again shortly.", status_code=429, retry_after=60)
    assert 'class="card"' in response.body.decode()
    assert "style-src 'unsafe-inline'" in response.headers["Content-Security-Policy"]


def test_baseline_referrer_policy_preserves_same_origin_form_origin():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert 'setdefault("Referrer-Policy", "same-origin")' in source
    assert 'setdefault("Referrer-Policy", "no-referrer")' not in source


def test_auth_settings_present_external_base_as_general_security_setting():
    source = (Path(__file__).resolve().parents[2] / "frontend" / "static" / "auth-settings.js").read_text()
    assert "External Base URL (Canonical Origin)" in source
    assert "reverse-proxy origin validation" in source
    assert "PUBLIC_BASE_URL environment variable" in source


def test_authentication_ux_assets_are_packaged_and_loaded():
    static = Path(__file__).resolve().parents[2] / "frontend" / "static"
    bootstrap = (static / "auth.js").read_text()
    ux_script = (static / "auth-ux.js").read_text()
    ux_style = (static / "auth-ux.css").read_text()

    assert "/auth-ux.js?v=1" in bootstrap
    assert "/auth-ux.css?v=1" in bootstrap
    assert "External Authentication Origin" in ux_script
    assert "Authorization & Claim Mapping" in ux_script
    assert "#settings-form .stab-panel.active" in ux_style
    assert "max-width: none" in ux_style
