from types import SimpleNamespace

import pytest

from auth import oidc
from auth.models import AuthMechanism


def _settings(**updates):
    values = {
        "auth_oidc_enabled": True,
        "oidc_provider_name": "OpenID Connect",
        "oidc_issuer_url": "https://id.example/application/o/downloadpulse",
        "oidc_client_id": "downloadpulse-client",
        "oidc_client_secret": "secret",
        "oidc_scopes": ["openid", "profile", "email"],
        "oidc_allow_all": False,
        "oidc_allowed_subjects": [],
        "oidc_allowed_emails": ["operator@example.com"],
        "oidc_allowed_groups": ["operators"],
        "oidc_group_claim": "groups",
        "public_base_url": "https://pulse.example",
    }
    values.update(updates)
    return SimpleNamespace(**values)


def _config(**updates):
    return oidc.oidc_configuration(_settings(**updates))


def _discovery(config, *, userinfo_endpoint="https://id.example/userinfo"):
    return oidc.OidcDiscovery(
        issuer=config.issuer,
        authorization_endpoint=config.issuer + "/authorize",
        token_endpoint=config.issuer + "/token",
        jwks_uri=config.issuer + "/jwks",
        token_endpoint_auth_methods=("client_secret_basic",),
        signing_algorithms=("RS256",),
        userinfo_endpoint=userinfo_endpoint,
    )


def test_discovery_accepts_optional_https_userinfo_and_rejects_insecure_userinfo():
    cfg = _config()
    base = {
        "issuer": cfg.issuer,
        "authorization_endpoint": cfg.issuer + "/authorize",
        "token_endpoint": cfg.issuer + "/token",
        "jwks_uri": cfg.issuer + "/jwks",
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    parsed = oidc._parse_discovery(
        cfg,
        {**base, "userinfo_endpoint": "https://id.example/userinfo"},
    )
    assert parsed.userinfo_endpoint == "https://id.example/userinfo"

    without = oidc._parse_discovery(cfg, base)
    assert without.userinfo_endpoint == ""

    with pytest.raises(oidc.OidcProtocolError, match="non-HTTPS"):
        oidc._parse_discovery(
            cfg,
            {**base, "userinfo_endpoint": "http://id.example/userinfo"},
        )


def test_userinfo_is_only_required_for_missing_authorization_claims():
    cfg = _config()
    complete = {
        "iss": cfg.issuer,
        "sub": "user-1",
        "email": "operator@example.com",
        "email_verified": True,
        "groups": ["operators"],
    }
    assert oidc._claims_need_userinfo(cfg, complete) is False
    assert oidc._claims_need_userinfo(cfg, {"sub": "user-1"}) is True

    allow_all = _config(
        oidc_allow_all=True,
        oidc_allowed_emails=["operator@example.com"],
        oidc_allowed_groups=["operators"],
    )
    assert oidc._claims_need_userinfo(allow_all, {"sub": "user-1"}) is False


def test_userinfo_verified_email_pair_replaces_unverified_id_token_email_as_a_pair():
    cfg = _config()
    merged = oidc._merge_userinfo_claims(
        cfg,
        {
            "iss": cfg.issuer,
            "sub": "user-1",
            "email": "unverified-id-token@example.com",
        },
        {
            "sub": "user-1",
            "email": "operator@example.com",
            "email_verified": True,
            "groups": ["operators"],
            "iss": "https://evil.example",
        },
    )
    assert merged["iss"] == cfg.issuer
    assert merged["sub"] == "user-1"
    assert merged["email"] == "operator@example.com"
    assert merged["email_verified"] is True
    assert merged["groups"] == ["operators"]


def test_userinfo_never_rebinds_verification_to_a_different_id_token_email():
    cfg = _config(oidc_allowed_emails=["claimed@example.com"], oidc_allowed_groups=[])
    merged = oidc._merge_userinfo_claims(
        cfg,
        {
            "iss": cfg.issuer,
            "sub": "user-1",
            "email": "claimed@example.com",
        },
        {
            "sub": "user-1",
            "email": "different-verified@example.com",
            "email_verified": True,
        },
    )
    assert merged["email"] == "different-verified@example.com"
    assert merged["email_verified"] is True
    with pytest.raises(oidc.OidcAuthorizationError):
        oidc.authorize_oidc_claims(cfg, merged)


def test_userinfo_does_not_override_an_already_verified_id_token_email_pair():
    cfg = _config(oidc_allowed_emails=["id-token@example.com"], oidc_allowed_groups=[])
    merged = oidc._merge_userinfo_claims(
        cfg,
        {
            "iss": cfg.issuer,
            "sub": "user-1",
            "email": "id-token@example.com",
            "email_verified": True,
        },
        {
            "sub": "user-1",
            "email": "userinfo@example.com",
            "email_verified": True,
        },
    )
    assert merged["email"] == "id-token@example.com"
    assert merged["email_verified"] is True
    assert oidc.authorize_oidc_claims(cfg, merged).authenticated is True


@pytest.mark.asyncio
async def test_complete_login_fetches_needed_userinfo_and_discards_token(monkeypatch):
    cfg = _config()
    disc = _discovery(cfg)
    oidc.oidc_transaction_store.clear()
    oidc.oidc_transaction_store.create(
        state="state",
        correlation="browser",
        nonce="nonce",
        code_verifier="verifier",
        return_to="/settings",
        config=cfg,
        discovery=disc,
    )

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def fetch_token(self, url, **kwargs):
            assert url == disc.token_endpoint
            return {"id_token": "encoded", "access_token": "short-lived-access"}

        async def aclose(self):
            return None

    async def fake_validate(_token_response, transaction):
        return {
            "iss": transaction.config.issuer,
            "sub": "user-1",
            "name": "Operator",
        }

    calls = []

    async def fake_userinfo(endpoint, access_token, expected_subject):
        calls.append((endpoint, access_token, expected_subject))
        return {
            "sub": "user-1",
            "email": "operator@example.com",
            "email_verified": True,
            "groups": ["operators"],
        }

    monkeypatch.setattr(oidc, "AsyncOAuth2Client", FakeClient)
    monkeypatch.setattr(oidc, "validate_id_token", fake_validate)
    monkeypatch.setattr(oidc, "_fetch_userinfo", fake_userinfo)

    principal, return_to = await oidc.complete_oidc_login(
        state="state",
        code="code",
        correlation="browser",
    )
    assert principal.mechanism is AuthMechanism.OIDC_SESSION
    assert principal.subject == f"{cfg.issuer}|user-1"
    assert return_to == "/settings"
    assert calls == [(disc.userinfo_endpoint, "short-lived-access", "user-1")]
    assert principal.claims["email"] == "operator@example.com"
    assert principal.claims["groups"] == ["operators"]


@pytest.mark.asyncio
async def test_userinfo_subject_mismatch_is_rejected(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"sub": "different-user", "email": "operator@example.com"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None):
            assert url == "https://id.example/userinfo"
            assert headers["Authorization"] == "Bearer access"
            return FakeResponse()

    monkeypatch.setattr(oidc.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    with pytest.raises(oidc.OidcProtocolError, match="subject"):
        await oidc._fetch_userinfo(
            "https://id.example/userinfo",
            "access",
            "user-1",
        )


def test_userinfo_can_complete_verified_email_when_id_token_has_only_verification_flag():
    cfg = _config(oidc_allowed_emails=["operator@example.com"], oidc_allowed_groups=[])
    id_claims = {
        "iss": cfg.issuer,
        "sub": "user-1",
        "email_verified": True,
    }
    assert oidc._claims_need_userinfo(cfg, id_claims) is True
    merged = oidc._merge_userinfo_claims(
        cfg,
        id_claims,
        {
            "sub": "user-1",
            "email": "operator@example.com",
            "email_verified": True,
        },
    )
    assert merged["email"] == "operator@example.com"
    assert merged["email_verified"] is True
    assert oidc.authorize_oidc_claims(cfg, merged).authenticated is True
