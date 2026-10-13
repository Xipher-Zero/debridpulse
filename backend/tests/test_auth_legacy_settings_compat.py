import pytest
from fastapi import HTTPException

from api.routes import (
    SettingsUpdate,
    _AUTH_COMPAT_SETTINGS_FIELDS,
    _merge_secret_settings,
)
from core.config import AppSettings


def test_legacy_settings_document_carries_no_local_credential():
    """Local credentials are account state: the broad settings document can
    neither set nor clear one (``auth.accounts`` owns them)."""
    previous = AppSettings(auth_password_enabled=True)
    update = SettingsUpdate(auth_password_enabled=True, auth_username="operator", auth_password="typed")

    merged = _merge_secret_settings(update, previous)

    assert not {"auth_username", "auth_password", "auth_password_hash"} & set(merged)
    with pytest.raises(HTTPException) as refused:
        _merge_secret_settings(SettingsUpdate(clear_secrets=["auth_password"]), previous)
    assert refused.value.status_code == 400


def test_legacy_partial_settings_update_preserves_authentication_state():
    previous = AppSettings(
        auth_password_enabled=True,
        auth_session_lifetime_hours=24,
        auth_oidc_enabled=True,
        oidc_provider_name="Authentik",
        oidc_issuer_url="https://id.example/application/o/debridpulse/",
        oidc_client_id="debridpulse-client",
        oidc_scopes=["openid", "profile"],
        oidc_allow_all=False,
        oidc_allowed_subjects=["https://id.example/application/o/debridpulse/|user-1"],
        oidc_allowed_emails=[],
        oidc_allowed_groups=["debridpulse-operators"],
        oidc_group_claim="groups",
        public_base_url="https://pulse.example",
    )
    update = SettingsUpdate(full_sync_interval_minutes=7)

    merged = _merge_secret_settings(update, previous)

    assert merged["full_sync_interval_minutes"] == 7
    for field in _AUTH_COMPAT_SETTINGS_FIELDS:
        assert merged[field] == getattr(previous, field)


def test_legacy_explicit_authentication_fields_still_override_previous_state():
    previous = AppSettings(auth_password_enabled=True, auth_session_lifetime_hours=12)
    update = SettingsUpdate(auth_password_enabled=False, auth_session_lifetime_hours=24)

    merged = _merge_secret_settings(update, previous)

    assert merged["auth_password_enabled"] is False
    assert merged["auth_session_lifetime_hours"] == 24
