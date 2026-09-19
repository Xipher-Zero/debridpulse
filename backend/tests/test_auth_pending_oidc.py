from http.cookies import SimpleCookie

import pytest
from fastapi import Request

from api import auth_config_routes
from auth.models import Principal
from auth.oidc import OIDC_CORRELATION_COOKIE, OidcProtocolError
from auth.oidc_version import (
    authentication_configuration_baseline_version,
    oidc_configuration_version,
)
from auth.pending_oidc import (
    PendingOidcConfiguration,
    _merge_verified_oidc_settings,
    commit_verified_pending_oidc,
    pending_oidc_store,
)
from auth.throttle import oidc_verify_rate_limiter
from core import config as config_module
from core.config import AppSettings


def _settings(**updates):
    values = {
        "auth_password_enabled": True,
        "auth_username": "operator",
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


def _request(path="/api/auth/oidc/verify-config", cookie=""):
    headers = [(b"host", b"pulse.example")]
    if cookie:
        headers.append((b"cookie", cookie.encode("latin-1")))
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST" if path.startswith("/api/") else "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("pulse.example", 443),
        }
    )
    request.state.principal = Principal.password_session("operator")
    return request


def _response_cookie(response, name):
    for key, value in response.raw_headers:
        if key.lower() != b"set-cookie":
            continue
        parsed = SimpleCookie()
        parsed.load(value.decode("latin-1"))
        if name in parsed:
            return parsed[name]
    return None


@pytest.fixture(autouse=True)
def _clear_oidc_verification_rate_limit():
    oidc_verify_rate_limiter.clear()
    yield
    oidc_verify_rate_limiter.clear()


def test_pending_builder_preserves_replace_and_explicit_clear_secret_intent(monkeypatch):
    current = _settings(oidc_client_secret="stored-secret")
    monkeypatch.setattr(auth_config_routes, "get_settings", lambda: current)

    preserved = auth_config_routes._build_proposed_settings(
        auth_config_routes.OidcVerificationRequest()
    )
    assert preserved.oidc_client_secret == "stored-secret"
    assert preserved.oidc_client_secret_clear is False

    replaced = auth_config_routes._build_proposed_settings(
        auth_config_routes.OidcVerificationRequest(oidc_client_secret="new-secret")
    )
    assert replaced.oidc_client_secret == "new-secret"
    assert replaced.oidc_client_secret_clear is False

    cleared = auth_config_routes._build_proposed_settings(
        auth_config_routes.OidcVerificationRequest(clear_oidc_client_secret=True)
    )
    assert cleared.oidc_client_secret == ""
    assert cleared.oidc_client_secret_clear is True


def test_verified_pending_oidc_merge_preserves_newer_live_non_oidc_and_password_state():
    candidate = _settings(
        oidc_client_id="replacement-client",
        db_wipe_enabled=False,
        auth_password_enabled=True,
        auth_username="old-operator",
        auth_password_hash="old-hash",
    )
    live = _settings(
        db_wipe_enabled=True,
        auth_password_enabled=False,
        auth_username="new-operator",
        auth_password_hash="new-hash",
    )
    item = PendingOidcConfiguration(
        settings=candidate,
        configuration_version=oidc_configuration_version(candidate),
        created_at=1.0,
        expires_at=2.0,
        apply_password_enabled=False,
    )

    merged = _merge_verified_oidc_settings(live, item)

    assert merged.oidc_client_id == "replacement-client"
    assert merged.db_wipe_enabled is True
    assert merged.auth_password_enabled is False
    assert merged.auth_username == "new-operator"
    assert merged.auth_password_hash == "new-hash"


def test_verified_pending_oidc_merge_applies_password_enable_only_when_explicitly_requested():
    candidate = _settings(auth_password_enabled=False, oidc_client_id="replacement-client")
    live = _settings(auth_password_enabled=True, auth_password_hash="live-hash")
    item = PendingOidcConfiguration(
        settings=candidate,
        configuration_version=oidc_configuration_version(candidate),
        created_at=1.0,
        expires_at=2.0,
        apply_password_enabled=True,
    )

    merged = _merge_verified_oidc_settings(live, item)

    assert merged.auth_password_enabled is False
    assert merged.auth_password_hash == "live-hash"
    assert merged.oidc_client_id == "replacement-client"


def test_verified_pending_oidc_commit_rejects_changed_auth_baseline(monkeypatch):
    baseline = _settings()
    candidate = _settings(oidc_client_id="replacement-client")
    candidate_version = oidc_configuration_version(candidate)
    newer = _settings(oidc_allowed_groups=["newer-policy"])
    pending_oidc_store.clear()
    pending_oidc_store.stage(
        "pending-state",
        candidate,
        configuration_version=candidate_version,
        baseline_configuration_version=authentication_configuration_baseline_version(baseline),
    )
    monkeypatch.setattr(config_module, "_settings", newer)

    def must_not_save(_settings):
        raise AssertionError("stale pending configuration must not persist")

    monkeypatch.setattr(config_module, "save_settings", must_not_save)
    assert commit_verified_pending_oidc(
        "pending-state",
        expected_configuration_version=candidate_version,
    ) is False
    assert config_module.get_settings() is newer


def test_pending_oidc_commit_rejects_mismatched_proof_before_persistence(monkeypatch):
    current = _settings()
    candidate = _settings(oidc_client_id="replacement-client")
    candidate_version = oidc_configuration_version(candidate)
    pending_oidc_store.clear()
    pending_oidc_store.stage(
        "pending-state",
        candidate,
        configuration_version=candidate_version,
        baseline_configuration_version=authentication_configuration_baseline_version(current),
    )
    monkeypatch.setattr(config_module, "_settings", current)
    saves = []
    monkeypatch.setattr(config_module, "save_settings", lambda value: saves.append(value))

    assert commit_verified_pending_oidc(
        "pending-state",
        expected_configuration_version="proof-from-different-config",
    ) is False
    assert saves == []
    assert config_module.get_settings() is current


@pytest.mark.asyncio
async def test_verify_config_stages_only_and_sets_secure_browser_correlation(monkeypatch):
    current = _settings()
    pending_oidc_store.clear()
    monkeypatch.setattr(auth_config_routes, "get_settings", lambda: current)

    async def fake_begin(candidate, *, return_to):
        assert candidate.oidc_client_id == "replacement-client"
        assert return_to == "/settings"
        return "https://id.example/authorize?state=pending-state", "browser-correlation"

    monkeypatch.setattr(auth_config_routes, "begin_oidc_login", fake_begin)
    monkeypatch.setattr(auth_config_routes, "oidc_configuration_version", lambda _candidate: "version-1")
    monkeypatch.setattr(
        auth_config_routes,
        "oidc_callback_url",
        lambda _candidate: "https://pulse.example/auth/oidc/callback",
    )

    proposed = auth_config_routes.OidcVerificationRequest(
        oidc_client_id="replacement-client",
        return_to="/settings",
    )
    response = await auth_config_routes.verify_pending_oidc_configuration(
        _request(),
        proposed,
    )
    assert response.status_code == 200
    assert pending_oidc_store.has("pending-state") is True
    assert current.oidc_client_id == "debridpulse-client"

    cookie = _response_cookie(response, OIDC_CORRELATION_COOKIE)
    assert cookie is not None
    assert cookie["secure"] is True
    assert cookie["httponly"] is True
    assert cookie["samesite"].lower() == "lax"


@pytest.mark.asyncio
async def test_failed_pending_callback_never_commits_proposed_config(monkeypatch):
    candidate = _settings(oidc_client_id="replacement-client")
    pending_oidc_store.clear()
    pending_oidc_store.stage(
        "pending-state",
        candidate,
        configuration_version=oidc_configuration_version(candidate),
    )
    calls = []

    async def failed_complete(**_kwargs):
        raise OidcProtocolError("bad token")

    monkeypatch.setattr(auth_config_routes, "complete_oidc_login", failed_complete)
    monkeypatch.setattr(
        auth_config_routes,
        "commit_verified_pending_oidc",
        lambda _state, **_kwargs: calls.append("commit") or True,
    )

    response = await auth_config_routes.pending_aware_oidc_callback(
        _request(
            path="/auth/oidc/callback",
            cookie=f"{OIDC_CORRELATION_COOKIE}=browser-correlation",
        ),
        state="pending-state",
        code="code",
    )
    assert response.status_code == 401
    assert calls == []
    assert pending_oidc_store.has("pending-state") is False


@pytest.mark.asyncio
async def test_successful_pending_callback_commits_before_new_session(monkeypatch):
    candidate = _settings(oidc_client_id="replacement-client")
    version = oidc_configuration_version(candidate)
    pending_oidc_store.clear()
    pending_oidc_store.stage(
        "pending-state",
        candidate,
        configuration_version=version,
    )
    events = []
    principal = Principal.oidc_session(
        "https://id.example|user-1",
        credential_version=version,
    )

    async def successful_complete(**_kwargs):
        events.append("verified")
        return principal, "/settings"

    def fake_commit(state, *, expected_configuration_version):
        assert state == "pending-state"
        assert expected_configuration_version == version
        events.append("committed")
        pending_oidc_store.discard(state)
        return True

    class FakeSessionStore:
        def revoke(self, _token):
            events.append("revoked-old")
            return True

        def create(self, created_principal, *, lifetime_seconds, credential_version=""):
            assert created_principal is principal
            assert lifetime_seconds > 0
            assert credential_version == version
            events.append("new-session")
            return "new-token", object()

    monkeypatch.setattr(auth_config_routes, "complete_oidc_login", successful_complete)
    monkeypatch.setattr(auth_config_routes, "commit_verified_pending_oidc", fake_commit)
    monkeypatch.setattr(auth_config_routes, "session_store", FakeSessionStore())
    monkeypatch.setattr(auth_config_routes, "get_settings", lambda: candidate)

    response = await auth_config_routes.pending_aware_oidc_callback(
        _request(
            path="/auth/oidc/callback",
            cookie=(
                f"{OIDC_CORRELATION_COOKIE}=browser-correlation; "
                "__Host-debridpulse-session=old-token"
            ),
        ),
        state="pending-state",
        code="code",
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert events.index("verified") < events.index("committed") < events.index("new-session")
    issued = _response_cookie(response, "__Host-debridpulse-session")
    assert issued is not None
    assert issued.value == "new-token"
