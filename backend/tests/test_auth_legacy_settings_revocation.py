from auth.models import AuthMechanism, Principal
from auth.passwords import basic_verification_cache, hash_password
from auth.sessions import session_store
from core.config import AppSettings


def _settings(*, password="secret", username="operator", password_enabled=True, issuer="https://id.example/app", oidc_enabled=True):
    return AppSettings(
        auth_password_enabled=password_enabled,
        auth_username=username,
        auth_password_hash=hash_password(password),
        auth_oidc_enabled=oidc_enabled,
        oidc_provider_name="OIDC",
        oidc_issuer_url=issuer,
        oidc_client_id="client",
        oidc_client_secret="secret",
        oidc_scopes=["openid", "email"],
        oidc_allow_all=True,
        public_base_url="https://pulse.example",
    )


def test_legacy_settings_password_change_clears_basic_cache_and_revokes_password_sessions():
    from api import routes

    previous = _settings(password="old-secret")
    current = previous.model_copy(deep=True)
    current.auth_password_hash = hash_password("new-secret")

    basic_verification_cache.clear()
    basic_verification_cache.remember("operator", "old-secret", previous.auth_password_hash)
    session_store.clear()
    token, _ = session_store.create(
        Principal.password_session("operator"),
        lifetime_seconds=3600,
        credential_version=routes.password_credential_version(previous.auth_password_hash),
    )
    assert session_store.resolve(token) is not None
    assert basic_verification_cache.size == 1

    routes._revoke_stale_authentication_state(previous, current)
    assert session_store.resolve(token) is None
    assert basic_verification_cache.size == 0


def test_legacy_settings_oidc_change_revokes_oidc_sessions():
    from api import routes

    previous = _settings(issuer="https://id.example/app")
    current = previous.model_copy(update={"oidc_issuer_url": "https://id.example/new-app"}, deep=True)
    session_store.clear()
    token, _ = session_store.create(
        Principal.oidc_session("https://id.example/app|user-1"),
        lifetime_seconds=3600,
        credential_version=routes.oidc_configuration_version(previous),
    )
    assert session_store.resolve(token) is not None

    routes._revoke_stale_authentication_state(previous, current)
    assert session_store.resolve(token) is None


def test_legacy_settings_unrelated_change_does_not_revoke_auth_sessions():
    from api import routes

    previous = _settings()
    current = previous.model_copy(update={"full_sync_interval_minutes": previous.full_sync_interval_minutes + 1}, deep=True)
    session_store.clear()
    password_token, _ = session_store.create(
        Principal.password_session("operator"),
        lifetime_seconds=3600,
        credential_version=routes.password_credential_version(previous.auth_password_hash),
    )
    oidc_token, _ = session_store.create(
        Principal.oidc_session("https://id.example/app|user-1"),
        lifetime_seconds=3600,
        credential_version=routes.oidc_configuration_version(previous),
    )

    routes._revoke_stale_authentication_state(previous, current)
    assert session_store.resolve(password_token) is not None
    assert session_store.resolve(oidc_token) is not None
