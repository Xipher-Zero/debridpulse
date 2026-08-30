from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-authentication.js"
STYLE = STATIC / "ui-settings-authentication.css"
SETTINGS = STATIC / "ui-settings-page.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_session_state_moves_under_authentication_status_and_removes_legacy_card():
    js = source(RUNTIME)

    assert "dpAuthenticationStatusPolished" in js
    assert "findCard('Authentication Status')" in js
    assert "findCard('Sessions & Security')" in js
    assert "sessionRow.className = 'dp-settings-auth-session-row';" in js
    assert "sessionRow.append(activeSessions, currentMechanism, lifetimeField, logoutActions);" in js
    assert "statusGrid.after(sessionRow);" in js
    assert "sessionsCard.remove();" in js


def test_session_status_copy_and_mechanism_presentation_are_locked():
    js = source(RUNTIME)

    assert "activeTitle.textContent = 'Active Browser Sessions';" in js
    assert "mechanismTitle.textContent = 'Current Authentication Mechanism';" in js
    assert "if (raw === 'password_session') return 'Password Session';" in js
    assert "if (raw === 'oidc_session') return 'OIDC Session';" in js
    assert "=== 'current session') item.remove();" in js


def test_browser_session_lifetime_uses_settings_sandwich_and_logout_centerline():
    js = source(RUNTIME)
    css = source(STYLE)

    assert "fieldFor(sessionsCard, 'auth_session_lifetime_hours')" in js
    assert "'Browser Session Lifetime'," in js
    assert "'How long a browser login remains valid before sign-in is required again.'" in js
    assert "actionControl.className = 'dp-settings-auth-session-action-control';" in js
    assert "actionControl.appendChild(logoutButton);" in js

    assert ".dp-settings-auth-session-lifetime > .form-label" in css
    assert ".dp-settings-auth-session-lifetime > .form-hint" in css
    assert "inset-inline-start: 3px;" in css
    assert ".dp-settings-auth-session-actions" in css
    assert "grid-template-rows: auto var(--dp-input-height);" in css
    assert ".dp-settings-auth-session-action-control" in css
    assert "min-height: var(--dp-input-height);" in css
    assert "align-items: center;" in css


def test_public_base_url_moves_to_oidc_without_recreating_the_control():
    js = source(RUNTIME)
    settings = source(SETTINGS)

    assert "findCard('OpenID Connect')" in js
    assert "sessionsCard?.querySelector('#dp-auth-public-base-url')" in js
    assert "publicBaseLabel.textContent = 'Public Base URL';" in js
    assert "oidcEnable.after(publicBaseField);" in js

    # The clean-room Settings runtime remains the single owner of the actual
    # public-base input and logout action; the presentation layer only reparents.
    assert settings.count('id="dp-auth-public-base-url"') == 1
    assert settings.count('data-action="logout-session"') == 1
    assert 'data-setting="auth_session_lifetime_hours"' not in settings
    assert "input('auth_session_lifetime_hours'" in settings


def test_authentication_status_session_row_has_desktop_and_mobile_layout_contracts():
    css = source(STYLE)

    assert ".dp-settings-auth-status-card .dp-settings-auth-session-row" in css
    assert "grid-template-columns: minmax(220px, 1fr) minmax(270px, 1.15fr) minmax(260px, .9fr) auto;" in css
    assert "@media (max-width: 900px)" in css
    assert "grid-template-columns: minmax(0, 1fr);" in css
