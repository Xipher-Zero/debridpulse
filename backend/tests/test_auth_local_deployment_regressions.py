from pathlib import Path

from fastapi import Request, Response
import pytest

from api.auth_routes import _AUTH_PAGE_STYLE, _state_free_auth_page
from auth.middleware import enforce_general_web_security
from auth.sessions import request_is_secure


def _request(
    *,
    scheme="http",
    host="debridpulse.local:8081",
    origin=None,
    fetch_site="same-origin",
    server=("debridpulse.local", 8081),
    extra_headers=None,
):
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if fetch_site is not None:
        headers.append((b"sec-fetch-site", fetch_site.encode()))
    for key, value in (extra_headers or {}).items():
        headers.append((str(key).lower().encode("latin-1"), str(value).encode("latin-1")))
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
        "server": server,
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
async def test_hostile_dns_host_cannot_define_trusted_open_mode_authority(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    reached = False

    async def downstream(_request):
        nonlocal reached
        reached = True
        return Response(status_code=204)

    request = _request(
        scheme="http",
        host="attacker.example",
        origin="http://attacker.example",
        fetch_site="same-origin",
        # The request reached the private DebridPulse socket; Host is not the
        # transport-owned server authority and cannot bootstrap its own trust.
        server=("192.168.226.200", 8081),
    )
    response = await enforce_general_web_security(request, downstream)
    assert response.status_code == 403
    assert response.body == b"Forbidden authority"
    assert reached is False


@pytest.mark.asyncio
async def test_untrusted_forwarded_host_cannot_create_application_authority(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    request = _request(
        scheme="http",
        host="attacker.example",
        origin="http://attacker.example",
        server=("192.168.226.200", 8081),
        extra_headers={"X-Forwarded-Host": "debridpulse.local:8081"},
    )
    assert (await enforce_general_web_security(request, _ok)).status_code == 403


@pytest.mark.asyncio
async def test_spoofed_forwarded_host_does_not_override_valid_direct_authority(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    request = _request(
        scheme="http",
        host="192.168.226.200:8081",
        origin="http://192.168.226.200:8081",
        server=("192.168.226.200", 8081),
        extra_headers={"X-Forwarded-Host": "attacker.example"},
    )
    assert (await enforce_general_web_security(request, _ok)).status_code == 200


@pytest.mark.asyncio
async def test_configured_public_authority_rejects_other_proxy_host(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://download.xipherzero.com")
    request = _request(
        scheme="http",
        host="attacker.example",
        origin="http://attacker.example",
        server=("172.18.0.4", 8080),
        extra_headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "download.xipherzero.com"},
    )
    assert (await enforce_general_web_security(request, _ok)).status_code == 403


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scheme", "host", "origin", "server", "expected"),
    [
        ("https", "pulse.example", "https://pulse.example", ("pulse.example", 443), 200),
        ("https", "pulse.example:443", "https://pulse.example", ("pulse.example", 443), 200),
        ("http", "192.168.226.200:8081", "http://192.168.226.200:8081", ("192.168.226.200", 8081), 200),
        ("http", "pulse.example:notaport", "http://pulse.example", ("pulse.example", 80), 403),
    ],
)
async def test_trusted_authority_port_normalization(scheme, host, origin, server, expected, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    request = _request(scheme=scheme, host=host, origin=origin, server=server)
    assert (await enforce_general_web_security(request, _ok)).status_code == expected


@pytest.mark.asyncio
async def test_ipv6_literal_authority_is_parsed_without_colon_ambiguity(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    request = _request(
        scheme="http",
        host="[2001:db8::1]:8081",
        origin="http://[2001:db8::1]:8081",
        server=("2001:db8::1", 8081),
    )
    assert (await enforce_general_web_security(request, _ok)).status_code == 200


def test_invalid_public_base_path_is_not_trusted_for_secure_classification(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://download.xipherzero.com/not-an-origin")
    request = _request(host="debridpulse.local:8081")
    assert request_is_secure(request) is False


def test_auth_pages_use_reviewed_debridpulse_login_palettes():
    assert "--bg:#030612" in _AUTH_PAGE_STYLE
    assert "--bg2:#081126" in _AUTH_PAGE_STYLE
    assert "--card:rgba(10,16,33,.55)" in _AUTH_PAGE_STYLE
    assert "--card2:rgba(7,12,27,.40)" in _AUTH_PAGE_STYLE
    assert "--field:rgba(7,13,29,.52)" in _AUTH_PAGE_STYLE
    assert "--text:#f7f8ff" in _AUTH_PAGE_STYLE
    assert "--accent:#b45cff" in _AUTH_PAGE_STYLE
    assert "--accent2:#3d94ff" in _AUTH_PAGE_STYLE
    assert "--icon-accent:#b0a3ff" in _AUTH_PAGE_STYLE
    assert "--glass-top:rgba(255,255,255,.13)" in _AUTH_PAGE_STYLE
    assert "--glass-purple:rgba(180,92,255,.18)" in _AUTH_PAGE_STYLE
    assert "--glass-blue:rgba(42,148,255,.15)" in _AUTH_PAGE_STYLE
    assert ':root[data-theme="light"]' in _AUTH_PAGE_STYLE
    assert "--bg:#f8f9ff" in _AUTH_PAGE_STYLE
    assert "--bg2:#eef4ff" in _AUTH_PAGE_STYLE
    assert "--card:rgba(255,255,255,.55)" in _AUTH_PAGE_STYLE
    assert "--card2:rgba(249,251,255,.35)" in _AUTH_PAGE_STYLE
    assert "--field:rgba(255,255,255,.50)" in _AUTH_PAGE_STYLE
    assert "--text:#111a34" in _AUTH_PAGE_STYLE
    assert "--accent:#9637f5" in _AUTH_PAGE_STYLE
    assert "--accent2:#2f86ff" in _AUTH_PAGE_STYLE
    assert "--icon-accent:#7868e4" in _AUTH_PAGE_STYLE
    assert "--glass-top:rgba(255,255,255,.88)" in _AUTH_PAGE_STYLE
    assert "--glass-purple:rgba(161,91,255,.13)" in _AUTH_PAGE_STYLE
    assert "--glass-blue:rgba(66,150,255,.14)" in _AUTH_PAGE_STYLE
    assert "backdrop-filter:blur(30px) saturate(165%)" in _AUTH_PAGE_STYLE
    assert "border-radius:12px" in _AUTH_PAGE_STYLE
    assert "body::before" in _AUTH_PAGE_STYLE
    assert "body::after" in _AUTH_PAGE_STYLE
    assert "#f08a24" not in _AUTH_PAGE_STYLE
    response = _state_free_auth_page(message="Try again shortly.", status_code=429, retry_after=60)
    assert 'class="card"' in response.body.decode()
    assert "style-src 'unsafe-inline'" in response.headers["Content-Security-Policy"]


def test_baseline_referrer_policy_preserves_same_origin_form_origin():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert 'setdefault("Referrer-Policy", "same-origin")' in source
    assert 'setdefault("Referrer-Policy", "no-referrer")' not in source



def test_authentication_session_and_help_assets_are_packaged_without_settings_augmentation():
    static = Path(__file__).resolve().parents[2] / "frontend" / "static"
    bootstrap = (static / "auth.js").read_text()
    settings = (static / "ui-settings-page.js").read_text()
    ux_style = (static / "auth-ux.css").read_text()

    assert "/auth-help.js?v=1" in bootstrap
    assert "/auth-ux.css?v=1" in bootstrap
    assert "/auth-settings.js" not in bootstrap
    assert "/auth-ux.js" not in bootstrap
    assert "sidebar-bottom-stack" in ux_style
    assert "window.DPSettingsPage = Object.freeze({load});" in settings
