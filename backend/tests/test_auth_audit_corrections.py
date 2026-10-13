import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.responses import Response

from account_support import account_database
from auth import middleware, oidc
from auth.models import Principal
from auth.oidc_version import oidc_configuration_version_from_config
from auth.passwords import hash_password, verify_password
from core import config as config_module


ROOT = Path(__file__).resolve().parents[2]


def _request(method, path, *, payload=None, headers=None, scheme="https"):
    body = json.dumps(payload).encode() if payload is not None else b""
    raw_headers = [
        (str(key).lower().encode("latin-1"), str(value).encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    if payload is not None:
        raw_headers.extend(
            [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ]
        )
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


def _open_settings():
    return SimpleNamespace(
        auth_password_enabled=False,
        auth_oidc_enabled=False,
        oidc_provider_name="OpenID Connect",
        oidc_issuer_url="",
        oidc_client_id="",
        oidc_client_secret="",
        oidc_scopes=["openid", "profile", "email"],
        oidc_allow_all=False,
        oidc_allowed_subjects=[],
        oidc_allowed_emails=[],
        oidc_allowed_groups=[],
        oidc_group_claim="groups",
        public_base_url="",
    )


def _protected_password_settings():
    cfg = _open_settings()
    cfg.auth_password_enabled = True
    return cfg


@pytest.mark.asyncio
async def test_open_mode_still_enforces_auth_transition_guard(monkeypatch, tmp_path):
    await account_database(tmp_path, monkeypatch)  # no account holds a password
    monkeypatch.setattr(middleware, "get_settings", _open_settings)
    request = _request(
        "PUT",
        "/api/settings",
        payload={"auth_password_enabled": True, "auth_username": "operator"},
        headers={"Host": "pulse.example"},
    )
    reached = False

    async def call_next(_request):
        nonlocal reached
        reached = True
        return Response(status_code=204)

    response = await middleware.enforce_authentication(request, call_next)
    assert response.status_code == 409
    assert reached is False


@pytest.mark.asyncio
async def test_authentication_allows_cors_preflight_to_reach_cors_layer(monkeypatch):
    cfg = _protected_password_settings()
    monkeypatch.setattr(middleware, "get_settings", lambda: cfg)
    request = _request(
        "OPTIONS",
        "/api/stats",
        headers={
            "Host": "pulse.example",
            "Origin": "https://automation.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    reached = False

    async def call_next(_request):
        nonlocal reached
        reached = True
        return Response(status_code=204)

    response = await middleware.enforce_authentication(request, call_next)
    assert response.status_code == 204
    assert reached is True


def test_existing_corrupt_config_fails_closed(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"auth_password_enabled": true,', encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    with pytest.raises(RuntimeError, match="could not be read safely"):
        config_module.load_settings()

    path.write_text('["not", "an", "object"]', encoding="utf-8")
    with pytest.raises(RuntimeError, match="could not be read safely"):
        config_module.load_settings()


def test_legacy_plaintext_replaces_existing_hash(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    old_hash = hash_password("old-secret")
    path.write_text(
        json.dumps(
            {
                "auth_password_enabled": True,
                "auth_username": "operator",
                "auth_password_hash": old_hash,
                "auth_password": "new-secret",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    monkeypatch.setattr(config_module, "_settings", config_module.AppSettings())
    monkeypatch.setitem(config_module._legacy_startup_inputs, "credential", None)

    config_module.load_settings()
    persisted = json.loads(path.read_text(encoding="utf-8"))
    username, pending_hash = config_module.legacy_credential_input()

    # The pending credential stays in the file as a verifier until the account
    # owner installs it on the default Admin; the plaintext never survives.
    assert username == "operator"
    assert verify_password(pending_hash, "new-secret") is True
    assert verify_password(pending_hash, "old-secret") is False
    assert persisted["auth_password_hash"] == pending_hash != old_hash
    assert "auth_password" not in persisted


def test_legacy_password_migration_persistence_failure_aborts_startup(monkeypatch, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"auth_username": "operator", "auth_password": "legacy-secret"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    monkeypatch.setitem(config_module._legacy_startup_inputs, "credential", None)

    def fail_save(_settings):
        raise OSError("read-only configuration volume")

    monkeypatch.setattr(config_module, "save_settings", fail_save)
    with pytest.raises(RuntimeError, match="migration could not be persisted safely"):
        config_module.load_settings()


def test_oidc_issuer_identifier_is_exact_and_trailing_slash_is_preserved():
    issuer = "https://id.example/application/o/debridpulse/"
    settings = SimpleNamespace(
        auth_oidc_enabled=True,
        oidc_provider_name="Authentik",
        oidc_issuer_url=issuer,
        oidc_client_id="debridpulse-client",
        oidc_client_secret="secret",
        oidc_scopes=["openid", "profile", "email"],
        oidc_allow_all=True,
        oidc_allowed_subjects=[],
        oidc_allowed_emails=[],
        oidc_allowed_groups=[],
        oidc_group_claim="groups",
        public_base_url="https://pulse.example",
    )
    config = oidc.oidc_configuration(settings)
    assert config.issuer == issuer
    assert oidc._discovery_url(config.issuer) == (
        "https://id.example/application/o/debridpulse/.well-known/openid-configuration"
    )

    discovered = oidc._parse_discovery(
        config,
        {
            "issuer": issuer,
            "authorization_endpoint": issuer + "authorize",
            "token_endpoint": issuer + "token",
            "jwks_uri": issuer + "jwks",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "id_token_signing_alg_values_supported": ["RS256"],
        },
    )
    assert discovered.issuer == issuer
    with pytest.raises(oidc.OidcProtocolError, match="issuer"):
        oidc._parse_discovery(
            config,
            {
                "issuer": issuer.rstrip("/"),
                "authorization_endpoint": issuer + "authorize",
                "token_endpoint": issuer + "token",
                "jwks_uri": issuer + "jwks",
            },
        )

    principal = oidc.authorize_oidc_claims(
        config,
        {"iss": issuer, "sub": "user-1", "name": "Operator"},
    )
    assert principal.subject == issuer + "|user-1"
    assert principal.credential_version == oidc_configuration_version_from_config(config)


def test_oidc_direct_jose_dependency_is_explicitly_owned():
    requirements = (ROOT / "backend" / "requirements.in").read_text(encoding="utf-8")
    assert "joserfc==1.7.4" in requirements.splitlines()


def test_uvicorn_access_log_remains_suppressed_for_oidc_query_credentials():
    source = (ROOT / "backend" / "core" / "logging_utils.py").read_text(encoding="utf-8")
    assert '"uvicorn.access"' in source
    assert "setLevel(logging.WARNING)" in source
