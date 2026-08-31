from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-authentication.js"
STYLE = STATIC / "ui-settings-authentication.css"
LOADER = STATIC / "ui-presentation-loader.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_username_password_header_copy_and_enable_control_are_locked():
    js = source(RUNTIME)
    css = source(STYLE)

    assert "Configure local credentials for browser sign-in and HTTP Basic API access." in js
    assert 'input[data-setting="auth_password_enabled"]' in js
    assert "enableTitle.textContent = 'Enable'" in js
    assert "enableInfo?.querySelector('.td')?.remove();" in js
    assert "header.appendChild(enable);" in js

    assert ".dp-settings-username-password-card > .card-header" in css
    assert "grid-template-columns: minmax(240px, 1fr) minmax(420px, 1.4fr) minmax(240px, 1fr);" in css
    assert ".dp-settings-auth-header-copy" in css
    assert ".dp-settings-auth-header-enable" in css
    assert "justify-self: end;" in css


def test_username_password_fields_and_action_share_one_row():
    js = source(RUNTIME)
    css = source(STYLE)

    assert "fieldFor(card, 'auth_username')" in js
    assert "card.querySelector('#dp-auth-new-password')" in js
    assert 'button[data-action="clear-password"]' in js
    assert "row.append(usernameField, passwordField, actions);" in js
    assert "body.replaceChildren(row);" in js

    assert ".dp-settings-auth-credentials-row" in css
    assert "grid-template-columns: minmax(260px, .9fr) minmax(420px, 1.35fr) auto;" in css
    assert "align-items: start;" in css
    assert ".dp-settings-auth-password-actions" in css
    assert "justify-self: end;" in css


def test_username_password_field_copy_and_input_start_datum_are_locked():
    js = source(RUNTIME)
    css = source(STYLE)

    assert "Username used for browser and HTTP Basic authentication." in js
    assert "Leave blank to keep the current password. Enter a new password to replace it." in js
    assert ".dp-settings-auth-credentials-row > .dp-settings-field > .form-label" in css
    assert ".dp-settings-auth-credentials-row > .dp-settings-field > .form-hint" in css
    assert "inset-inline-start: 3px;" in css
    assert "margin-bottom: 7px;" in css
    assert "margin-top: 7px;" in css


def test_clear_password_button_centerline_tracks_form_controls():
    js = source(RUNTIME)
    css = source(STYLE)

    assert "actionControl.className = 'dp-settings-auth-action-control';" in js
    assert "actionControl.appendChild(clearButton);" in js
    assert "actions.replaceChildren(actionLabel, actionControl);" in js
    assert "grid-template-rows: auto var(--dp-input-height);" in css
    assert ".dp-settings-auth-action-control" in css
    assert "min-height: var(--dp-input-height);" in css
    assert "align-items: center;" in css
    assert "justify-content: flex-end;" in css


def test_authentication_presentation_is_idempotent_and_loaded_after_settings_page():
    js = source(RUNTIME)
    loader = source(LOADER)

    assert "dpUsernamePasswordPolished" in js
    assert "MutationObserver" in js
    assert "mutation.type === 'childList'" in js
    assert "attributes: true" not in js

    css_entry = "'/ui-settings-authentication.css?v=1'"
    js_entry = "'/ui-settings-authentication.js?v=1'"
    settings_entry = "'/ui-settings-page.js?v=4'"
    assert css_entry in loader
    assert js_entry in loader
    assert settings_entry in loader
    assert loader.index(js_entry) > loader.index(settings_entry)
