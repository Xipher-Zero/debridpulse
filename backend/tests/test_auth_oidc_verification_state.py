from pathlib import Path

import pytest
from fastapi import Request

from api import auth_config_routes
from auth.models import Principal
from auth.oidc_verification import OidcVerificationStore
from auth.oidc_version import (
    authentication_configuration_baseline_version,
    oidc_configuration_version,
)
from auth import pending_oidc as pending_module
from auth.pending_oidc import commit_verified_pending_oidc, pending_oidc_store
from core import config as config_module
from core.config import AppSettings


def _settings(**updates):
    values = {
        "auth_password_enabled": True,
        "auth_username": "operator",
        "auth_password_hash": "stored-hash",
        "auth_oidc_enabled": True,
        "oidc_provider_name": "OpenID Connect",
        "oidc_issuer_url": "https://id.example/application/o/debridpulse",
        "oidc_client_id": "debridpulse-client",
        "oidc_client_secret": "secret",
        "oidc_scopes": ["openid", "profile", "email"],
        "oidc_allow_all": True,
        "oidc_allowed_subjects": [],
        "oidc_allowed_emails": [],
        "oidc_allowed_groups": [],
        "oidc_group_claim": "groups",
        "public_base_url": "https://pulse.example",
    }
    values.update(updates)
    return AppSettings(**values)


def _request():
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/api/auth/config",
            "raw_path": b"/api/auth/config",
            "query_string": b"",
            "headers": [(b"host", b"pulse.example")],
            "client": ("127.0.0.1", 12345),
            "server": ("pulse.example", 443),
        }
    )
    request.state.principal = Principal.anonymous()
    return request


def test_oidc_verification_store_is_exact_durable_and_private(tmp_path: Path):
    path = tmp_path / "oidc-verification.json"
    store = OidcVerificationStore(path)
    version = "a" * 64

    verified_at = store.record(version)

    matched = store.status(version)
    assert matched.verified is True
    assert matched.verified_at == verified_at
    assert store.status("b" * 64).verified is False
    assert (path.stat().st_mode & 0o777) == 0o600

    # A new store instance proves the state is durable across process lifetime.
    reloaded = OidcVerificationStore(path)
    assert reloaded.status(version).verified is True
    assert reloaded.status(version).verified_at == verified_at


def test_oidc_verification_store_fails_closed_for_malformed_state(tmp_path: Path):
    path = tmp_path / "oidc-verification.json"
    path.write_text('{"configuration_version":"not-a-digest","verified_at":"yesterday"}', encoding="utf-8")
    store = OidcVerificationStore(path)

    assert store.status("a" * 64).verified is False
    assert store.status("a" * 64).verified_at == ""


@pytest.mark.asyncio
async def test_auth_payload_retains_exact_proof_while_disabled_and_stales_on_change(monkeypatch, tmp_path: Path):
    cfg = _settings(auth_oidc_enabled=False)
    enabled_copy = cfg.model_copy(update={"auth_oidc_enabled": True}, deep=True)
    version = oidc_configuration_version(enabled_copy)
    assert version

    store = OidcVerificationStore(tmp_path / "oidc-verification.json")
    verified_at = store.record(version)
    monkeypatch.setattr(auth_config_routes, "oidc_verification_store", store)
    monkeypatch.setattr(auth_config_routes, "get_settings", lambda: cfg)

    payload = await auth_config_routes._authentication_payload(_request())
    assert payload["oidc_enabled"] is False
    assert payload["oidc_configured"] is True
    assert payload["oidc_available"] is None
    assert payload["oidc_verified"] is True
    assert payload["oidc_verified_at"] == verified_at

    changed = cfg.model_copy(update={"oidc_client_id": "different-client"}, deep=True)
    monkeypatch.setattr(auth_config_routes, "get_settings", lambda: changed)
    stale = await auth_config_routes._authentication_payload(_request())
    assert stale["oidc_verified"] is False
    assert stale["oidc_verified_at"] == ""


def test_verified_pending_commit_records_proof_before_configuration_persistence(monkeypatch, tmp_path: Path):
    current = _settings()
    candidate = _settings(oidc_client_id="replacement-client")
    version = oidc_configuration_version(candidate)
    assert version

    store = OidcVerificationStore(tmp_path / "oidc-verification.json")
    monkeypatch.setattr(pending_module, "oidc_verification_store", store)
    monkeypatch.setattr(config_module, "_settings", current)

    pending_oidc_store.clear()
    pending_oidc_store.stage(
        "pending-state",
        candidate,
        configuration_version=version,
        baseline_configuration_version=authentication_configuration_baseline_version(current),
    )

    saved = []

    def fake_save(value):
        assert store.status(version).verified is True
        saved.append(value)

    monkeypatch.setattr(config_module, "save_settings", fake_save)

    assert commit_verified_pending_oidc(
        "pending-state",
        expected_configuration_version=version,
    ) is True
    assert saved and saved[0].oidc_client_id == "replacement-client"
    assert store.status(version).verified is True
