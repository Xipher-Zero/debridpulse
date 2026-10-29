import base64
import hashlib
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import Request, Response
from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey

from auth import oidc
from auth.middleware import enforce_authentication
from auth.models import AuthMechanism


def _settings(**updates):
    values = {
        "auth_password_enabled": False,
        "auth_username": "",
        "auth_password_hash": "",
        "auth_oidc_enabled": True,
        "oidc_provider_name": "Authentik",
        "oidc_issuer_url": "https://id.example/application/o/debridpulse",
        "oidc_client_id": "debridpulse-client",
        "oidc_client_secret": "secret",
        "oidc_scopes": ["profile", "email"],
        "oidc_allow_all": False,
        "oidc_allowed_subjects": [],
        "oidc_allowed_emails": [],
        "oidc_allowed_groups": ["debridpulse-operators"],
        "oidc_group_claim": "groups",
        "public_base_url": "https://pulse.example",
        "auth_session_lifetime_hours": 12,
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _config(**updates):
    return oidc.oidc_configuration(_settings(**updates))


def _discovery(config=None, **updates):
    cfg = config or _config()
    values = {
        "issuer": cfg.issuer,
        "authorization_endpoint": cfg.issuer + "/authorize",
        "token_endpoint": cfg.issuer + "/token",
        "jwks_uri": cfg.issuer + "/jwks",
        "token_endpoint_auth_methods": ("client_secret_basic",),
        "signing_algorithms": ("RS256",),
    }
    values.update(updates)
    return oidc.OidcDiscovery(**values)


def _request(method="GET", path="/api/stats", headers=None):
    raw_headers = [
        (str(key).lower().encode("latin-1"), str(value).encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("pulse.example", 80),
        }
    )


async def _ok(_request):
    return Response(content="ok", status_code=200)


def _transaction(config=None, discovery=None, nonce="nonce-value"):
    cfg = config or _config()
    disc = discovery or _discovery(cfg)
    return oidc.OidcTransaction(
        nonce=nonce,
        code_verifier="verifier",
        correlation_fingerprint=b"fingerprint",
        created_at=1.0,
        expires_at=999999999.0,
        return_to="/settings",
        config=cfg,
        discovery=disc,
    )


def test_oidc_configuration_requires_canonical_https_external_origin(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    cfg = _config()
    assert cfg.callback_url == "https://pulse.example/auth/oidc/callback"
    assert cfg.scopes[0] == "openid"

    for invalid in (
        "http://pulse.example",
        "https://user@pulse.example",
        "https://pulse.example/path",
        "https://pulse.example/?query=yes",
        "https://pulse.example/#fragment",
    ):
        with pytest.raises(oidc.OidcConfigurationError):
            oidc.oidc_configuration(_settings(public_base_url=invalid))


def test_public_base_url_environment_override_wins(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://external.example")
    cfg = oidc.oidc_configuration(_settings(public_base_url="https://stored.example"))
    assert cfg.callback_url == "https://external.example/auth/oidc/callback"


def test_discovery_is_issuer_bound_https_and_pkce_capable():
    cfg = _config()
    valid = oidc._parse_discovery(
        cfg,
        {
            "issuer": cfg.issuer,
            "authorization_endpoint": cfg.issuer + "/authorize",
            "token_endpoint": cfg.issuer + "/token",
            "jwks_uri": cfg.issuer + "/jwks",
            "response_types_supported": ["code", "id_token"],
            "code_challenge_methods_supported": ["S256"],
            "id_token_signing_alg_values_supported": ["RS256", "none"],
        },
    )
    assert valid.signing_algorithms == ("RS256",)
    assert valid.token_endpoint_auth_methods == ("client_secret_basic",)

    with pytest.raises(oidc.OidcProtocolError, match="issuer"):
        oidc._parse_discovery(cfg, {"issuer": "https://other.example"})

    bad_endpoint = {
        "issuer": cfg.issuer,
        "authorization_endpoint": "http://id.example/authorize",
        "token_endpoint": cfg.issuer + "/token",
        "jwks_uri": cfg.issuer + "/jwks",
    }
    with pytest.raises(oidc.OidcProtocolError, match="non-HTTPS"):
        oidc._parse_discovery(cfg, bad_endpoint)

    no_pkce = {
        "issuer": cfg.issuer,
        "authorization_endpoint": cfg.issuer + "/authorize",
        "token_endpoint": cfg.issuer + "/token",
        "jwks_uri": cfg.issuer + "/jwks",
        "response_types_supported": ["code"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "code_challenge_methods_supported": ["plain"],
    }
    with pytest.raises(oidc.OidcProtocolError, match="PKCE S256"):
        oidc._parse_discovery(cfg, no_pkce)

    no_code = {
        "issuer": cfg.issuer,
        "authorization_endpoint": cfg.issuer + "/authorize",
        "token_endpoint": cfg.issuer + "/token",
        "jwks_uri": cfg.issuer + "/jwks",
        "response_types_supported": ["id_token"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    with pytest.raises(oidc.OidcProtocolError, match="Authorization Code"):
        oidc._parse_discovery(cfg, no_code)

    missing_signing_metadata = {
        "issuer": cfg.issuer,
        "authorization_endpoint": cfg.issuer + "/authorize",
        "token_endpoint": cfg.issuer + "/token",
        "jwks_uri": cfg.issuer + "/jwks",
        "response_types_supported": ["code"],
    }
    with pytest.raises(oidc.OidcProtocolError, match="id_token_signing_alg_values_supported"):
        oidc._parse_discovery(cfg, missing_signing_metadata)


def test_oidc_transaction_store_is_bounded_correlated_expiring_and_one_time():
    now = [100.0]
    store = oidc.OidcTransactionStore(ttl_seconds=60, max_entries=2, clock=lambda: now[0])
    cfg = _config()
    disc = _discovery(cfg)

    for state in ("s1", "s2", "s3"):
        store.create(
            state=state,
            correlation=state + "-browser",
            nonce=state + "-nonce",
            code_verifier=state + "-verifier",
            return_to="/stats",
            config=cfg,
            discovery=disc,
        )
    assert store.size == 2
    assert store.consume("s1", "s1-browser") is None

    # A wrong browser correlation consumes the state rather than leaving a
    # replayable transaction available for repeated guessing.
    assert store.consume("s2", "wrong") is None
    assert store.consume("s2", "s2-browser") is None

    now[0] = 161.0
    assert store.consume("s3", "s3-browser") is None
    assert store.size == 0


@pytest.mark.asyncio
async def test_oidc_start_uses_state_nonce_and_pkce_s256(monkeypatch):
    cfg = _config()
    disc = _discovery(cfg)

    async def fake_discover(_config):
        return disc

    monkeypatch.setattr(oidc, "discover_oidc", fake_discover)
    oidc.oidc_transaction_store.clear()
    url, correlation = await oidc.begin_oidc_login(_settings(), return_to="/stats?window=24")
    params = parse_qs(urlsplit(url).query)
    assert params["response_type"] == ["code"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"][0]
    assert params["nonce"][0]
    assert "openid" in params["scope"][0].split()

    transaction = oidc.oidc_transaction_store.consume(params["state"][0], correlation)
    assert transaction is not None
    assert transaction.nonce == params["nonce"][0]
    assert transaction.return_to == "/stats?window=24"
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(transaction.code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    assert params["code_challenge"] == [expected]


def test_oidc_allow_all_bypasses_configured_identity_lists():
    cfg = _config(
        oidc_allow_all=True,
        oidc_allowed_subjects=["https://wrong.example|other"],
        oidc_allowed_emails=["other@example.com"],
        oidc_allowed_groups=["other-group"],
    )
    principal = oidc.authorize_oidc_claims(
        cfg,
        {"iss": cfg.issuer, "sub": "user-1", "name": "Operator"},
    )
    assert principal.mechanism is AuthMechanism.OIDC_SESSION


def test_oidc_subject_authorization_is_issuer_qualified():
    cfg = _config(oidc_allowed_groups=[])
    composite = f"{cfg.issuer}|user-1"
    allowed = _config(oidc_allowed_subjects=[composite], oidc_allowed_groups=[])
    principal = oidc.authorize_oidc_claims(
        allowed,
        {"iss": allowed.issuer, "sub": "user-1"},
    )
    assert principal.subject == composite

    bare = _config(oidc_allowed_subjects=["user-1"], oidc_allowed_groups=[])
    with pytest.raises(oidc.OidcAuthorizationError):
        oidc.authorize_oidc_claims(bare, {"iss": bare.issuer, "sub": "user-1"})


@pytest.mark.parametrize(
    ("config_updates", "claims_updates", "allowed"),
    [
        ({"oidc_allow_all": True, "oidc_allowed_groups": []}, {}, True),
        ({"oidc_allowed_subjects": ["https://id.example/application/o/debridpulse|user-1"], "oidc_allowed_groups": []}, {}, True),
        ({"oidc_allowed_subjects": ["https://id.example/application/o/debridpulse|other"], "oidc_allowed_groups": []}, {}, False),
        ({"oidc_allowed_emails": ["User@Example.COM"], "oidc_allowed_groups": []}, {"email": "user@example.com", "email_verified": True}, True),
        ({"oidc_allowed_emails": ["user@example.com"], "oidc_allowed_groups": []}, {"email": "user@example.com", "email_verified": False}, False),
        ({"oidc_allowed_groups": ["debridpulse-operators"]}, {"groups": ["other", "debridpulse-operators"]}, True),
        ({"oidc_allowed_groups": ["debridpulse-operators"]}, {"groups": None}, False),
        ({"oidc_allowed_subjects": ["https://id.example/application/o/debridpulse|user-1"], "oidc_allowed_groups": ["debridpulse-operators"]}, {"groups": ["other"]}, False),
        ({"oidc_allowed_subjects": [], "oidc_allowed_emails": [], "oidc_allowed_groups": [], "oidc_allow_all": False}, {}, False),
    ],
)
def test_oidc_authorization_policy(config_updates, claims_updates, allowed):
    cfg = _config(**config_updates)
    claims = {
        "iss": cfg.issuer,
        "sub": "user-1",
        "name": "Operator",
        **claims_updates,
    }
    if allowed:
        principal = oidc.authorize_oidc_claims(cfg, claims)
        assert principal.mechanism is AuthMechanism.OIDC_SESSION
        assert principal.subject == f"{cfg.issuer}|user-1"
    else:
        with pytest.raises(oidc.OidcAuthorizationError):
            oidc.authorize_oidc_claims(cfg, claims)


@pytest.mark.asyncio
async def test_valid_id_token_is_signature_issuer_audience_nonce_and_expiry_checked(monkeypatch):
    key = RSAKey.generate_key(2048, {"use": "sig"})
    key.ensure_kid()
    cfg = _config(oidc_allow_all=True, oidc_allowed_groups=[])
    disc = _discovery(cfg)
    transaction = _transaction(cfg, disc, nonce="expected-nonce")
    now = int(time.time())

    async def fake_jwks(_url):
        return {"keys": [key.as_dict(private=False)]}

    monkeypatch.setattr(oidc, "_fetch_json", fake_jwks)

    def encoded(**updates):
        claims = {
            "iss": cfg.issuer,
            "sub": "user-1",
            "aud": cfg.client_id,
            "exp": now + 300,
            "iat": now,
            "nonce": "expected-nonce",
            **updates,
        }
        return jwt.encode(
            {"alg": "RS256", "kid": key.kid},
            claims,
            key,
            algorithms=["RS256"],
        )

    valid = await oidc.validate_id_token({"id_token": encoded()}, transaction)
    assert valid["sub"] == "user-1"

    for token in (
        encoded(iss="https://wrong.example"),
        encoded(aud="wrong-client"),
        encoded(nonce="wrong-nonce"),
        encoded(exp=now - 120),
    ):
        with pytest.raises(oidc.OidcProtocolError):
            await oidc.validate_id_token({"id_token": token}, transaction)

    other_key = RSAKey.generate_key(2048, {"use": "sig"})
    other_key.ensure_kid()
    forged = jwt.encode(
        {"alg": "RS256", "kid": key.kid},
        {
            "iss": cfg.issuer,
            "sub": "user-1",
            "aud": cfg.client_id,
            "exp": now + 300,
            "iat": now,
            "nonce": "expected-nonce",
        },
        other_key,
        algorithms=["RS256"],
    )
    with pytest.raises(oidc.OidcProtocolError):
        await oidc.validate_id_token({"id_token": forged}, transaction)


@pytest.mark.asyncio
async def test_hmac_id_token_uses_client_secret_without_fetching_jwks(monkeypatch):
    cfg = _config(
        oidc_client_secret="authentik-generated-client-secret-with-sufficient-entropy",
        oidc_allow_all=True,
        oidc_allowed_groups=[],
    )
    disc = _discovery(cfg, signing_algorithms=("HS256",))
    transaction = _transaction(cfg, disc, nonce="expected-nonce")
    now = int(time.time())
    key = OctKey.import_key(cfg.client_secret, {"use": "sig"})

    def encoded(audience=None):
        claims = {
            "iss": cfg.issuer,
            "sub": "user-1",
            "aud": audience if audience is not None else cfg.client_id,
            "exp": now + 300,
            "iat": now,
            "nonce": "expected-nonce",
        }
        if isinstance(claims["aud"], list):
            claims["azp"] = cfg.client_id
        return jwt.encode(
            {"alg": "HS256"},
            claims,
            key,
            algorithms=["HS256"],
        )

    async def unexpected_jwks_fetch(_url):
        raise AssertionError("MAC-signed ID token must use client_secret, not JWKS")

    monkeypatch.setattr(oidc, "_fetch_json", unexpected_jwks_fetch)
    valid = await oidc.validate_id_token({"id_token": encoded()}, transaction)
    assert valid["sub"] == "user-1"

    with pytest.raises(oidc.OidcProtocolError, match="single audience"):
        await oidc.validate_id_token(
            {"id_token": encoded([cfg.client_id, "other-audience"])},
            transaction,
        )


@pytest.mark.asyncio
async def test_id_token_without_nonce_empty_mac_key_unsigned_or_signature_stripped_is_rejected(monkeypatch):
    """The ID-token checks Authlib 1.9 tightened hold in DP's own validator.

    A token missing the transaction's nonce, a MAC-signed token when no client
    secret is configured (an empty HMAC key would let anyone forge it), an
    unsigned token, and a signed token whose signature was stripped are each
    refused -- never accepted on a provider's say-so.
    """
    secret = "authentik-generated-client-secret-with-sufficient-entropy"
    cfg = _config(oidc_client_secret=secret, oidc_allow_all=True, oidc_allowed_groups=[])
    disc = _discovery(cfg, signing_algorithms=("HS256",))
    transaction = _transaction(cfg, disc, nonce="expected-nonce")
    now = int(time.time())
    key = OctKey.import_key(secret, {"use": "sig"})

    async def unexpected_jwks_fetch(_url):
        raise AssertionError("MAC-signed ID token must use client_secret, not JWKS")

    monkeypatch.setattr(oidc, "_fetch_json", unexpected_jwks_fetch)

    def claims(**updates):
        values = {"iss": cfg.issuer, "sub": "user-1", "aud": cfg.client_id,
                  "exp": now + 300, "iat": now, "nonce": "expected-nonce", **updates}
        return {name: value for name, value in values.items() if value is not None}

    def signed(**updates):
        return jwt.encode({"alg": "HS256"}, claims(**updates), key, algorithms=["HS256"])

    # The same token is accepted with its nonce, refused without it.
    assert (await oidc.validate_id_token({"id_token": signed()}, transaction))["sub"] == "user-1"
    with pytest.raises(oidc.OidcProtocolError):
        await oidc.validate_id_token({"id_token": signed(nonce=None)}, transaction)

    # No client secret: a MAC-signed token is refused, never verified with an empty key.
    keyless = _transaction(_config(oidc_client_secret="", oidc_allow_all=True, oidc_allowed_groups=[]),
                           disc, nonce="expected-nonce")
    with pytest.raises(oidc.OidcProtocolError, match="requires a client secret"):
        await oidc.validate_id_token({"id_token": signed()}, keyless)

    def segment(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    unsigned = ".".join((segment(json.dumps({"alg": "none"}).encode()),
                         segment(json.dumps(claims()).encode()), ""))
    stripped = signed().rsplit(".", 1)[0] + "."
    for token in (unsigned, stripped):
        with pytest.raises(oidc.OidcProtocolError):
            await oidc.validate_id_token({"id_token": token}, transaction)


@pytest.mark.asyncio
async def test_complete_oidc_login_consumes_transaction_and_issues_authorized_principal(monkeypatch):
    cfg = _config(oidc_allow_all=True, oidc_allowed_groups=[])
    disc = _discovery(cfg)
    oidc.oidc_transaction_store.clear()
    oidc.oidc_transaction_store.create(
        state="state",
        correlation="browser",
        nonce="nonce",
        code_verifier="verifier",
        return_to="/downloads?filter=ready",
        config=cfg,
        discovery=disc,
    )

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def fetch_token(self, url, **kwargs):
            assert url == disc.token_endpoint
            assert kwargs["code"] == "code"
            assert kwargs["code_verifier"] == "verifier"
            return {"id_token": "encoded", "access_token": "access"}

        async def aclose(self):
            return None

    async def fake_validate(_token, transaction):
        assert transaction.nonce == "nonce"
        return {"iss": cfg.issuer, "sub": "user-1", "name": "Operator"}

    monkeypatch.setattr(oidc, "AsyncOAuth2Client", FakeClient)
    monkeypatch.setattr(oidc, "validate_id_token", fake_validate)
    principal, return_to = await oidc.complete_oidc_login(
        state="state",
        code="code",
        correlation="browser",
    )
    assert principal.mechanism is AuthMechanism.OIDC_SESSION
    assert return_to == "/downloads?filter=ready"
    with pytest.raises(oidc.OidcProtocolError):
        await oidc.complete_oidc_login(
            state="state",
            code="code",
            correlation="browser",
        )


@pytest.mark.asyncio
async def test_oidc_only_protects_api_and_invalid_configuration_never_fails_open(monkeypatch):
    import auth.middleware as middleware

    valid = _settings()
    monkeypatch.setattr(middleware, "get_settings", lambda: valid)
    request = _request("GET", "/api/stats", {"Host": "pulse.example"})
    response = await enforce_authentication(request, _ok)
    assert response.status_code == 401

    invalid = _settings(public_base_url="http://pulse.example")
    monkeypatch.setattr(middleware, "get_settings", lambda: invalid)
    unavailable = _request("GET", "/api/stats", {"Host": "pulse.example"})
    response = await enforce_authentication(unavailable, _ok)
    assert response.status_code == 503

    open_cfg = _settings(auth_oidc_enabled=False)
    monkeypatch.setattr(middleware, "get_settings", lambda: open_cfg)
    open_request = _request("GET", "/api/stats", {"Host": "pulse.example"})
    response = await enforce_authentication(open_request, _ok)
    assert response.status_code == 200
