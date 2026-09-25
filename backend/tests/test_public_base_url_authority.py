"""Public Base URL authority: env override, persisted fallback, and bootstrap.

``PUBLIC_BASE_URL`` is an optional deployment override, never a requirement.
When it is absent the persisted Settings value is authoritative -- for OIDC
callback generation and for the browser-security trusted origin alike.

That created a deadlock behind a reverse proxy: with neither source set there is
no trusted authority, so every browser mutation is refused, so the operator can
never save the value that would establish one. These cases pin the narrow way
out and, more importantly, pin how narrow it is.
"""

import pytest
from types import SimpleNamespace

from auth import middleware
from auth.policy import (
    configured_public_base_url,
    configured_public_origin,
    trusted_request_origin,
)
from core.config import AppSettings


PROXY = "https://dp.example.com"
PROXY_HOST = "dp.example.com"


def _request(*, host=PROXY_HOST, origin=PROXY, method="PUT", path="/api/auth/config",
             fetch_site="same-origin", scheme="https"):
    headers = {"host": host}
    if origin:
        headers["origin"] = origin
    if fetch_site:
        headers["sec-fetch-site"] = fetch_site
    scope = {
        "type": "http", "method": method, "path": path, "scheme": scheme,
        "server": ("0.0.0.0", 8080), "query_string": b"", "state": {},
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    from fastapi import Request

    return Request(scope)


# --- Case A: environment override ----------------------------------------

def test_environment_override_wins_over_persisted(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://env.example.com")
    settings = AppSettings(public_base_url="https://persisted.example.com")

    assert configured_public_base_url(settings) == "https://env.example.com"
    assert configured_public_origin(settings) == ("https", "env.example.com", 443)

    from auth.oidc import effective_public_base_url

    assert effective_public_base_url(settings) == "https://env.example.com"


def test_environment_override_is_the_trusted_authority(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", PROXY)
    assert trusted_request_origin(_request(), settings=AppSettings()) == ("https", PROXY_HOST, 443)


# --- Case B: persisted fallback ------------------------------------------

def test_persisted_value_is_authoritative_when_environment_is_absent(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    settings = AppSettings(public_base_url=PROXY)

    assert configured_public_base_url(settings) == PROXY
    assert trusted_request_origin(_request(), settings=settings) == ("https", PROXY_HOST, 443)

    from auth.oidc import effective_public_base_url, oidc_callback_url

    assert effective_public_base_url(settings) == PROXY
    assert oidc_callback_url(settings).startswith(PROXY)


def test_persisted_authority_still_rejects_a_different_origin(monkeypatch):
    """Authority established means ordinary enforcement, not a wildcard."""
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    settings = AppSettings(public_base_url=PROXY)
    assert trusted_request_origin(_request(host="attacker.example"), settings=settings) is None


# --- Case C: fresh reverse-proxy bootstrap --------------------------------

def _bootstrap_allowed(request, settings):
    from auth.policy import public_base_url_bootstrap_origin

    return public_base_url_bootstrap_origin(request, settings) is not None


def test_fresh_proxy_deployment_has_no_authority_at_all(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    settings = AppSettings(public_base_url="")
    # This is the deadlock: nothing trusts this request yet.
    assert trusted_request_origin(_request(), settings=settings) is None
    # ... and this is the single way out.
    assert _bootstrap_allowed(_request(), settings) is True


def test_bootstrap_closes_once_authority_exists(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert _bootstrap_allowed(_request(), AppSettings(public_base_url=PROXY)) is False


def test_bootstrap_closes_when_the_environment_supplies_authority(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", PROXY)
    assert _bootstrap_allowed(_request(), AppSettings(public_base_url="")) is False


# --- Case D: the allowance is narrow -------------------------------------

@pytest.mark.parametrize(
    "kwargs",
    [
        {"path": "/api/settings"},                 # another route
        {"path": "/api/auth/api-token"},           # another auth route
        {"method": "POST"},                        # another method
        {"fetch_site": "cross-site"},              # another site
        {"origin": "http://dp.example.com"},       # not HTTPS
        {"origin": "https://elsewhere.example"},   # Origin is not the Host
        {"origin": "not-a-url"},                   # unparseable
    ],
)
def test_bootstrap_refuses_everything_that_is_not_the_one_mutation(monkeypatch, kwargs):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert _bootstrap_allowed(_request(**kwargs), AppSettings(public_base_url="")) is False


def _update(**fields):
    from api.auth_config_routes import AuthenticationConfigUpdate

    return AuthenticationConfigUpdate(**fields)


def _rejection(update, monkeypatch=None):
    """Drive the route's enforcement with a request the predicate admits."""
    from api import auth_config_routes

    request = _request()
    stored = AppSettings(public_base_url="")
    original = auth_config_routes.get_settings
    auth_config_routes.get_settings = lambda: stored
    try:
        import os

        previous = os.environ.pop("PUBLIC_BASE_URL", None)
        try:
            return auth_config_routes._bootstrap_rejection(request, update)
        finally:
            if previous is not None:
                os.environ["PUBLIC_BASE_URL"] = previous
    finally:
        auth_config_routes.get_settings = original


def test_bootstrap_payload_may_carry_nothing_but_the_base_url():
    assert _rejection(_update(public_base_url=PROXY)) is None


@pytest.mark.parametrize("smuggled", [
    {"auth_password_enabled": True},
    {"auth_oidc_enabled": True},
    {"auth_username": "operator"},
    {"auth_password": "hunter2"},
    {"oidc_client_secret": "secret"},
    {"oidc_allow_all": True},
    {"clear_password": True},
    {"clear_oidc_client_secret": True},
    {"confirm_open_mode": True},
    {"auth_session_lifetime_hours": 99},
])
def test_bootstrap_cannot_smuggle_another_mutation(smuggled):
    rejected = _rejection(_update(public_base_url=PROXY, **smuggled))
    assert rejected is not None and rejected.status_code == 403


@pytest.mark.parametrize("value", ["", "http://dp.example.com", "not-a-url",
                                   "https://dp.example.com/path", "ftp://dp.example.com"])
def test_bootstrap_rejects_a_value_that_is_not_a_sane_https_origin(value):
    rejected = _rejection(_update(public_base_url=value))
    assert rejected is not None and rejected.status_code in {400, 403}


def test_bootstrap_cannot_nominate_a_third_party_authority():
    """The operator may only establish the origin they are standing on."""
    rejected = _rejection(_update(public_base_url="https://attacker.example"))
    assert rejected is not None and rejected.status_code == 403


def test_a_trusted_request_is_not_restricted_by_the_bootstrap_path(monkeypatch):
    """A local operator with no base URL set keeps ordinary semantics.

    `trusted_request_origin` already trusts a literal/localhost authority, so
    the allowance must not apply -- otherwise having no Public Base URL would
    silently forbid every other Authentication write.
    """
    from api import auth_config_routes

    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    stored = AppSettings(public_base_url="")
    monkeypatch.setattr(auth_config_routes, "get_settings", lambda: stored)
    local = _request(host="127.0.0.1:8080", origin="http://127.0.0.1:8080", scheme="http")
    assert auth_config_routes._bootstrap_rejection(
        local, _update(auth_password_enabled=True)) is None


# --- Case C, end to end through the real middleware chain -----------------

def _bootstrap_app():
    """The real boundary, in front of the real auth-config route.

    The deadlock lived in the interaction between the security middleware and
    the route, so the proof has to exercise both together rather than each
    helper on its own.
    """
    from fastapi import FastAPI

    from api import auth_config_routes

    app = FastAPI()
    app.include_router(auth_config_routes.router)

    @app.middleware("http")
    async def _security(request, call_next):
        return await middleware.enforce_general_web_security(request, call_next)

    async def behind_a_proxy(scope, receive, send):
        # The deployment fact that creates the deadlock: the transport listens
        # on the container's own socket, so the browser's Host is nothing the
        # server can vouch for. TestClient would otherwise make its own
        # base_url the transport authority and trust the request for free.
        if scope["type"] == "http":
            scope = {**scope, "server": ("0.0.0.0", 8080)}
        await app(scope, receive, send)

    return behind_a_proxy


@pytest.fixture
def proxy_app(monkeypatch, tmp_path):
    from core import config

    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    stored = AppSettings(public_base_url="")
    monkeypatch.setattr(config, "get_settings", lambda: stored)

    from api import auth_config_routes

    monkeypatch.setattr(auth_config_routes, "get_settings", lambda: stored)
    monkeypatch.setattr(auth_config_routes, "save_settings", lambda cfg: None)
    monkeypatch.setattr(auth_config_routes, "apply_settings", lambda cfg: None)
    monkeypatch.setattr(middleware, "get_settings", lambda: stored)
    return _bootstrap_app(), stored


def test_fresh_proxy_can_establish_its_base_url_and_nothing_else(proxy_app):
    from fastapi.testclient import TestClient

    app, _stored = proxy_app
    client = TestClient(app, base_url=PROXY)
    headers = {"Origin": PROXY, "Host": PROXY_HOST, "Sec-Fetch-Site": "same-origin"}

    # The deadlock: an ordinary auth mutation from this untrusted authority is
    # refused -- by the route's one-purpose check once the boundary has admitted
    # the request as a bootstrap candidate. Either way nothing else gets through.
    blocked = client.put("/api/auth/config", json={"auth_password_enabled": True}, headers=headers)
    assert blocked.status_code == 403
    assert "Public Base URL" in blocked.text

    # The way out, carrying nothing else.
    accepted = client.put("/api/auth/config", json={"public_base_url": PROXY}, headers=headers)
    assert accepted.status_code == 200, accepted.text

    # The allowance does not extend to riding along with something else.
    smuggled = client.put(
        "/api/auth/config",
        json={"public_base_url": PROXY, "auth_password_enabled": True},
        headers=headers,
    )
    assert smuggled.status_code == 403


def test_bootstrap_is_unavailable_from_a_foreign_origin(proxy_app):
    from fastapi.testclient import TestClient

    app, _stored = proxy_app
    client = TestClient(app, base_url=PROXY)
    foreign = {"Origin": "https://attacker.example", "Host": PROXY_HOST,
               "Sec-Fetch-Site": "cross-site"}
    refused = client.put("/api/auth/config", json={"public_base_url": PROXY}, headers=foreign)
    assert refused.status_code == 403
