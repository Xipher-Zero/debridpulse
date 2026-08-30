from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-authentication.js"
STYLE = STATIC / "ui-settings-authentication.css"
SETTINGS = STATIC / "ui-settings-page.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_api_access_header_copy_and_enable_control_are_locked():
    js = source(RUNTIME)
    css = source(STYLE)

    assert "Use a dedicated bearer token for automation, monitoring, and API integrations." in js
    assert 'input[data-setting="api_token_enabled"]' in js
    assert "enableTitle.textContent = 'Enable'" in js
    assert "enableInfo?.querySelector('.td')?.remove();" in js
    assert "header.appendChild(enable);" in js

    assert ".dp-settings-api-access-card > .card-header" in css
    assert ".dp-settings-auth-header-copy" in css
    assert ".dp-settings-auth-header-enable" in css
    assert "justify-self: end;" in css


def test_api_access_actions_status_and_one_time_token_layout_are_locked():
    js = source(RUNTIME)
    css = source(STYLE)

    assert 'button[data-action="generate-token"]' in js
    assert 'button[data-action="clear-token"]' in js
    assert "revokeButton.textContent = 'Revoke Token';" in js
    assert "status.replaceChildren(document.createTextNode('Stored Token: '), stateValue);" in js
    assert "Copy this token now. DebridPulse will not display it again." in js
    assert "layout.append(actions, status);" in js
    assert "layout.append(tokenWarning, tokenField);" in js
    assert "body.replaceChildren(layout);" in js

    assert ".dp-settings-api-token-layout.has-token" in css
    assert "grid-template-columns: minmax(300px, auto) minmax(520px, 1fr);" in css
    assert ".dp-settings-api-token-actions" in css
    assert ".dp-settings-api-token-status" in css
    assert ".dp-settings-api-token-warning" in css
    assert ".dp-settings-api-token-field" in css


def test_api_access_action_and_token_field_share_control_centerline():
    css = source(STYLE)

    assert "grid-template-rows: auto var(--dp-input-height) auto;" in css
    assert ".dp-settings-api-token-layout.has-token .dp-settings-api-token-actions" in css
    assert "grid-row: 2;" in css
    assert ".dp-settings-api-token-field" in css
    assert "min-height: var(--dp-input-height);" in css
    assert "align-items: center;" in css


def test_generate_rotate_button_width_is_stable_across_resting_and_busy_states():
    js = source(RUNTIME)
    css = source(STYLE)
    settings = source(SETTINGS)

    assert "generateButton.classList.add('dp-settings-api-token-generate');" in js
    assert ".dp-settings-api-token-generate" in css
    assert "inline-size: 136px;" in css
    assert "min-inline-size: 136px;" in css
    assert "state.auth?.api_token_configured ? 'Rotating…' : 'Generating…'" in settings
    assert "${a.api_token_configured ? 'Rotate Token' : 'Generate Token'}" in settings


def test_api_token_revoke_language_is_user_facing_without_reimplementing_token_endpoints():
    js = source(RUNTIME)
    settings = source(SETTINGS)

    assert "Revoke the API token? Automation and API clients using it will lose access immediately." in js
    assert "String(message ?? '') === 'API token cleared' ? 'API token revoked' : message" in js
    assert "textOf(revokeButton) === 'Clearing…'" in js
    assert "revokeButton.textContent = 'Revoking…';" in js

    # Presentation code only translates the established lifecycle language; the
    # clean-room Settings runtime remains the sole owner of token API calls.
    assert "/auth/api-token" not in js
    assert "request('POST', '/auth/api-token'" in settings
    assert "request('DELETE', '/auth/api-token'" in settings


def test_api_access_presentation_reapplies_after_clean_room_settings_rerender():
    js = source(RUNTIME)

    assert "dpApiAccessPolished" in js
    assert "findApiAccessCard()" in js
    assert "needsPolish()" in js
    assert "MutationObserver" in js
    assert "mutation.type === 'childList'" in js
