from pathlib import Path

# Regression contract for the Authentication Status mini-polish layer.
ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_auth_status_polish_load_order():
    loader = read("ui-presentation-loader.js")
    assert loader.index("/ui-settings-authentication.css?v=1") < loader.index("/ui-settings-authentication-polish.css?v=1")
    assert loader.index("/ui-settings-authentication.js?v=1") < loader.index("/ui-settings-authentication-polish.js?v=1")


def test_session_lifetime_has_inline_hours_unit():
    js = read("ui-settings-authentication-polish.js")
    css = read("ui-settings-authentication-polish.css")
    assert 'input[data-setting="auth_session_lifetime_hours"]' in js
    assert "unit.textContent = 'hours';" in js
    assert "Browser Session Lifetime in hours" in js
    assert ".dp-settings-auth-duration-unit" in css


def test_callback_url_is_centered_sandwich():
    js = read("ui-settings-authentication-polish.js")
    css = read("ui-settings-authentication-polish.css")
    assert "fieldByLabel(card, 'OIDC Callback URL')" in js
    assert "Redirect URI to configure with your OpenID Connect provider." in js
    assert "width: clamp(420px, 30%, 640px);" in css
    assert "margin: 18px auto 0;" in css


def test_auth_status_sandwich_copy_uses_control_edge():
    css = read("ui-settings-authentication-polish.css")
    assert ".dp-settings-auth-session-lifetime-polished > .form-label" in css
    assert ".dp-settings-auth-callback-field > .form-hint" in css
    assert "inset-inline-start: 0;" in css
