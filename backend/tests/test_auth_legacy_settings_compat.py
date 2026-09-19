from api.routes import (
    SettingsUpdate,
    _AUTH_COMPAT_SETTINGS_FIELDS,
    _merge_secret_settings,
)
from core.config import AppSettings


def _previous():
    cfg = AppSettings(
        auth_password_enabled=True,
        auth_username="operator",
    )
    cfg.auth_password_hash = "stored-argon2-hash"
    return cfg


def test_legacy_settings_blank_password_preserves_stored_hash():
    previous = _previous()
    update = SettingsUpdate(
        auth_password_enabled=True,
        auth_username="operator",
        auth_password="",
    )

    merged = _merge_secret_settings(update, previous)
    candidate = AppSettings(**merged)

    assert candidate.auth_password_hash_clear is False


def test_legacy_settings_explicit_password_clear_carries_destructive_intent():
    previous = _previous()
    update = SettingsUpdate(
        auth_password_enabled=False,
        auth_username="operator",
        auth_password="",
        clear_secrets=["auth_password"],
    )

    merged = _merge_secret_settings(update, previous)
    candidate = AppSettings(**merged)

    assert merged["auth_password"] == ""
    assert merged["auth_password_hash_clear"] is True
    assert candidate.auth_password_hash_clear is True


def test_legacy_partial_settings_update_preserves_authentication_state():
    previous = AppSettings(
        auth_password_enabled=True,
        auth_username="operator",
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
    previous = AppSettings(auth_password_enabled=True, auth_username="operator")
    update = SettingsUpdate(auth_password_enabled=False, auth_username="replacement")

    merged = _merge_secret_settings(update, previous)

    assert merged["auth_password_enabled"] is False
    assert merged["auth_username"] == "replacement"
