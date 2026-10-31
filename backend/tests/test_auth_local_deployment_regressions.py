from pathlib import Path

from fastapi import Request, Response
import pytest
import pytest_asyncio

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
    method="POST",
    path="/login",
    body=b"",
):
    headers = [(b"host", host.encode())] if host is not None else []
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
        "method": method,
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("192.168.226.10", 54321),
        "server": server,
    }, _body_stream(body))


def _body_stream(body):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return receive


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


@pytest.mark.asyncio
async def test_auth_pages_use_reviewed_debridpulse_login_palettes(monkeypatch):
    from services import account_appearance

    async def native():
        return {"mode": "dark", "palette": "debridpulse-dark"}

    monkeypatch.setattr(account_appearance, "login", native)
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
    response = await _state_free_auth_page(message="Try again shortly.", status_code=429, retry_after=60)
    assert 'class="card"' in response.body.decode()
    assert "style-src 'unsafe-inline'" in response.headers["Content-Security-Policy"]


def test_baseline_referrer_policy_preserves_same_origin_form_origin():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    assert 'setdefault("Referrer-Policy", "same-origin")' in source
    assert 'setdefault("Referrer-Policy", "no-referrer")' not in source



def test_authentication_session_assets_are_packaged_without_settings_or_help_augmentation():
    static = Path(__file__).resolve().parents[2] / "frontend" / "static"
    bootstrap = (static / "auth.js").read_text()
    settings = (static / "ui-settings-page.js").read_text()
    ux_style = (static / "auth-ux.css").read_text()

    # No injected Help augmentation: the Help page owns its own content.
    assert "auth-help" not in bootstrap
    assert not (static / "auth-help.js").exists()
    # The sidebar session stylesheet is part of the CSS graph, not injected by script.
    assert "auth-ux" not in bootstrap and "createElement('link')" not in bootstrap
    assert "/auth-ux.css?v=3" in (static / "style.css").read_text()
    assert "/auth-settings.js" not in bootstrap
    assert "/auth-ux.js" not in bootstrap
    assert "sidebar-bottom-stack" in ux_style
    assert "window.DPSettingsPage = Object.freeze({load});" in settings


# ── Open mode: the standalone Admin answers only at this installation's own
# authority (PA-AUTHZ-1). A page rebound to a foreign name reaches the private
# socket without any credential; it must read nothing. The one authority that
# decides mutations decides these reads, so the two can never disagree. ──────

SENTINEL = "WS2A-ARCHIVE-PW-ONE\nWS2A-ARCHIVE-PW-TWO"
SOCKET = "http://192.168.226.200:8081"  # the transport authority: the socket's own IP, as uvicorn reports it
LEGITIMATE_HOSTS = [
    "192.168.226.200:8081", "127.0.0.1:8081", "[::1]:8081", "[fd00::50]:8081", "localhost:8081",
    "debridpulse.local:8081", "nas.home.arpa:8081", "dp.internal:8081", "app.localhost:8081",
]
FOREIGN_HOSTS = [
    "rebind.attacker.example:8081",   # the DNS-rebinding name
    "debridpulse:8081",               # single-label, e.g. a Compose service name: not trusted
    "dp.lan:8081", "nas.mydomain.com:8081",          # ordinary or split-DNS aliases: not trusted
    "local:8081", "debridpulse.local.attacker.example:8081", "attacker-local:8081",
    "bad host:8081", "debridpulse.local:notaport",   # malformed
]
SENSITIVE_READS = ["/api/settings/extraction-passwords", "/api/settings", "/api/accounts", "/api/admin/backups",
                   "/api/torrents", "/api/auth/config", "/", "/app.js"]


@pytest_asyncio.fixture
async def open_mode(tmp_path, monkeypatch):
    """The real application and its whole middleware stack, in the shipped
    default open mode, holding sentinel archive passwords and one Integration
    token."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import httpx

    import core.config as config
    import db.database as database
    import main
    from auth import accounts
    from auth.accounts import DEFAULT_INTEGRATION_ID

    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "open.db")
    await database.init_db()
    monkeypatch.setattr(config, "_settings", config.AppSettings(extraction_password=SENTINEL))

    @asynccontextmanager
    async def operation():
        yield

    monkeypatch.setattr(main.app.state, "application", SimpleNamespace(application_operation=operation),
                        raising=False)
    _record, token = await accounts.create_token(DEFAULT_INTEGRATION_ID, "rebinding-probe")
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=SOCKET) as client:
        yield client, token


@pytest.mark.asyncio
@pytest.mark.parametrize("host", LEGITIMATE_HOSTS)
async def test_open_mode_reads_answer_at_this_installations_own_authority(open_mode, host):
    client, _token = open_mode
    passwords = await client.get("/api/settings/extraction-passwords", headers={"Host": host})
    assert passwords.status_code == 200 and passwords.json() == {"passwords": SENTINEL}
    shell = await client.get("/", headers={"Host": host})
    asset = await client.get("/app.js", headers={"Host": host})
    assert shell.status_code == 200 and "<html" in shell.text.casefold()
    assert asset.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("host", FOREIGN_HOSTS)
async def test_a_rebound_or_foreign_host_reads_nothing_in_open_mode(open_mode, host):
    client, _token = open_mode
    for path in SENSITIVE_READS:
        response = await client.get(path, headers={"Host": host})
        assert response.status_code == 403, (host, path, response.status_code)
        assert response.content == b"Forbidden authority", (host, path)
        assert b"WS2A-ARCHIVE" not in response.content


@pytest.mark.asyncio
async def test_a_forwarded_host_header_neither_grants_nor_removes_authority(open_mode):
    client, _token = open_mode
    spoofed = await client.get("/api/settings/extraction-passwords", headers={
        "Host": "rebind.attacker.example:8081", "X-Forwarded-Host": "debridpulse.local:8081",
        "Forwarded": "host=debridpulse.local:8081"})
    assert spoofed.status_code == 403 and b"WS2A-ARCHIVE" not in spoofed.content
    direct = await client.get("/api/settings/extraction-passwords", headers={
        "Host": "192.168.226.200:8081", "X-Forwarded-Host": "rebind.attacker.example"})
    assert direct.status_code == 200 and direct.json() == {"passwords": SENTINEL}


@pytest.mark.asyncio
async def test_public_paths_and_integration_tokens_are_not_authority_bound(open_mode):
    client, token = open_mode
    for path in ("/api/health", "/api/version", "/api/auth/status"):
        assert (await client.get(path, headers={"Host": "rebind.attacker.example:8081"})).status_code == 200, path
    # An *arr or script container calling by its Compose service name proves a token, never the standalone Admin.
    import auth.middleware as middleware
    seen = []

    async def downstream(request):
        seen.append(request.state.principal)
        return Response(status_code=200)

    request = _request(method="GET", path="/api/requests", host="debridpulse:8080", fetch_site=None,
                       server=("172.18.0.4", 8080), extra_headers={"Authorization": f"Bearer {token}"})
    assert (await middleware.enforce_authentication(request, downstream)).status_code == 200
    assert [principal.role for principal in seen] == ["integration"]
    refused = await client.get("/api/settings/extraction-passwords",
                               headers={"Host": "debridpulse:8080", "Authorization": f"Bearer {token}"})
    assert refused.status_code == 403 and b"WS2A-ARCHIVE" not in refused.content  # the Integration role's own refusal


@pytest.mark.asyncio
async def test_a_configured_https_public_base_url_keeps_reverse_proxy_reads(open_mode, monkeypatch):
    client, _token = open_mode
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://download.example.com")
    proxied = await client.get("/api/settings/extraction-passwords", headers={"Host": "download.example.com"})
    assert proxied.status_code == 200 and proxied.json() == {"passwords": SENTINEL}
    other = await client.get("/api/settings/extraction-passwords", headers={"Host": "other.example.com"})
    assert other.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("host", [None, "", "bad host", "debridpulse.local:0", "user@debridpulse.local"])
async def test_a_missing_or_malformed_host_is_refused_before_open_mode_admission(host, monkeypatch):
    import auth.middleware as middleware
    from core.config import AppSettings

    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(middleware, "get_settings", lambda: AppSettings())
    reached = False

    async def downstream(_request):
        nonlocal reached
        reached = True
        return Response(status_code=200)

    request = _request(method="GET", path="/api/settings/extraction-passwords", host=host, fetch_site=None,
                       server=("192.168.226.200", 8081))
    response = await middleware.enforce_authentication(request, downstream)
    assert response.status_code == 403 and response.body == b"Forbidden authority"
    assert reached is False


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["debridpulse.local:8081", "nas.home.arpa:8081", "dp.internal:8081",
                                  "app.localhost:8081"])
async def test_a_same_origin_write_at_a_local_only_name_is_admitted(host, monkeypatch):
    """Intentional (WS2a decision A): the one authority now admits these
    names for writes as well as reads; Origin must still equal the Host."""
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    same = _request(host=host, origin=f"http://{host}", server=("192.168.226.200", 8081))
    assert (await enforce_general_web_security(same, _ok)).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(("host", "origin", "fetch_site", "body"), [
    ("debridpulse:8081", "http://debridpulse:8081", "same-origin", b"Forbidden authority"),
    ("dp.lan:8081", "http://dp.lan:8081", "same-origin", b"Forbidden authority"),
    ("rebind.attacker.example:8081", "http://rebind.attacker.example:8081", "same-origin", b"Forbidden authority"),
    ("debridpulse.local:8081", "http://rebind.attacker.example:8081", "same-origin", b"Forbidden origin"),
    ("debridpulse.local:8081", "https://debridpulse.local:8081", "same-origin", b"Forbidden origin"),
    ("debridpulse.local:8081", "null", "same-origin", b"Forbidden origin"),
    ("debridpulse.local:8081", "http://rebind.attacker.example:8081", "cross-site", b"Forbidden request context"),
])
async def test_untrusted_or_cross_origin_writes_stay_refused(host, origin, fetch_site, body, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    request = _request(host=host, origin=origin, fetch_site=fetch_site, server=("192.168.226.200", 8081))
    response = await enforce_general_web_security(request, _ok)
    assert response.status_code == 403 and response.body == body


@pytest.mark.asyncio
async def test_open_mode_reverse_proxy_bootstrap_still_reaches_its_route(monkeypatch):
    """A fresh reverse-proxy deployment's one bootstrap mutation is admitted by
    the authentication boundary exactly as by the security boundary."""
    import auth.middleware as middleware
    from core.config import AppSettings

    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(middleware, "get_settings", lambda: AppSettings())
    proxy = "download.example.com"
    bootstrap = _request(method="PUT", path="/api/auth/config", host=proxy, origin=f"https://{proxy}",
                         server=("0.0.0.0", 8080), body=f'{{"public_base_url": "https://{proxy}"}}'.encode())
    assert (await enforce_general_web_security(bootstrap, _ok)).status_code == 200
    assert (await middleware.enforce_authentication(bootstrap, _ok)).status_code == 200
    read = _request(method="GET", path="/api/auth/config", host=proxy, server=("0.0.0.0", 8080))
    assert (await middleware.enforce_authentication(read, _ok)).status_code == 403


@pytest.mark.asyncio
async def test_authenticated_modes_answer_a_foreign_host_with_their_own_challenge(monkeypatch):
    """The authority bound applies to the credential-less standalone Admin
    only; password mode keeps refusing an unauthenticated request as before."""
    import auth.middleware as middleware
    from core.config import AppSettings

    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(middleware, "get_settings", lambda: AppSettings(auth_password_enabled=True))
    request = _request(method="GET", path="/api/settings/extraction-passwords", host="rebind.attacker.example",
                       fetch_site=None, server=("192.168.226.200", 8081))
    assert (await middleware.enforce_authentication(request, _ok)).status_code == 401
