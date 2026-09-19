from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
SETTINGS = STATIC / "ui-settings-page.js"
STYLE = STATIC / "ui-settings-page.css"
MODAL_STYLE = STATIC / "ui-modal-contract.css"
MODAL = STATIC / "ui-settings-modal.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def block(js: str, start: str, end: str) -> str:
    return js[js.index(start):js.index(end, js.index(start))]


def test_settings_apply_rerenders_without_losing_viewport():
    js = source(SETTINGS)
    assert "function captureSettingsViewport()" in js
    assert "function restoreSettingsViewport(snapshot)" in js
    assert "function renderPreservingViewport()" in js
    assert "root()?.querySelector('.dp-settings-scroll')" in js
    assert "document.getElementById('content')" in js
    assert "window.requestAnimationFrame(() => restoreSettingsViewport(snapshot))" in js

    non_auth = block(js, "async function persistNonAuth", "function authValue")
    auth = block(js, "async function persistAuth", "async function saveCurrent")
    assert "renderPreservingViewport();" in non_auth
    assert "renderPreservingViewport();" in auth
    assert "notify('Settings saved', 'success')" in non_auth
    assert "notify(successMessage, 'success')" in auth


def test_settings_uses_first_party_confirmation_dialog_not_browser_dialogs():
    js = source(SETTINGS)
    modal = source(MODAL)
    css = source(STYLE)
    modal_css = source(MODAL_STYLE)
    # The shell (role, focus trap, Escape, restoration) is owned by the canonical dialog module, not Settings.
    assert "role: 'alertdialog'" in modal
    assert 'aria-modal="true"' in modal
    assert "event.key === 'Escape'" in modal
    assert "event.key === 'Tab'" in modal
    assert "resolveFocusTarget(origin)" in modal
    assert "cancel.focus()" in modal
    assert "window.confirm" not in js and "window.confirm" not in modal
    assert "window.prompt" not in js and "window.prompt" not in modal
    assert ".dp-modal-overlay" not in css
    assert "--dp-panel-surface" not in css
    assert "box-shadow: var(--dp-panel-shadow)" not in css
    assert ".dp-modal-overlay" in modal_css
    assert ".dp-modal-dialog" in modal_css
    assert 'data-tone="warning"' in modal_css
    assert 'data-tone="danger"' in modal_css


def test_destructive_settings_actions_share_confirmation_primitive():
    js = source(SETTINGS)
    persist_auth = block(js, "async function persistAuth", "async function saveCurrent")
    wipe = block(js, "async function wipeDatabaseClean", "async function clearPassword")
    password = block(js, "async function clearPassword", "async function setApiTokenEnabled")
    token = block(js, "async function clearToken", "async function copyToken")

    assert "await window.DPSettingsModal.confirm" in persist_auth
    assert "Continue to Open Mode" in persist_auth
    assert "!payload.confirm_open_mode" in persist_auth
    assert "if (!confirmed) return false;" in persist_auth
    assert persist_auth.index("if (!confirmed) return false;") < persist_auth.index("payload.confirm_open_mode = true")

    assert "await window.DPSettingsModal.confirm" in password
    assert password.count("await window.DPSettingsModal.confirm") == 1
    assert "entersOpenMode" in password
    assert "payload.confirm_open_mode = true" in password
    assert "if (!confirmed) return;" in password
    assert password.index("if (!confirmed) return;") < password.index("payload.auth_password_enabled = false")

    assert "await window.DPSettingsModal.confirm" in token
    assert "Revoke API token?" in token
    assert "Revoke Token" in token
    assert "if (!confirmed) return;" in token
    assert token.index("if (!confirmed) return;") < token.index("request('DELETE', '/auth/api-token'")

    assert "await window.DPSettingsModal.confirm" in wipe
    assert "typedPhrase: 'WIPE'" in wipe
    assert "Wipe Database" in wipe
    assert "if (!confirmed) return;" in wipe
    assert wipe.index("if (!confirmed) return;") < wipe.index("request('POST', '/admin/database/wipe'")


def test_typed_confirmation_gates_destructive_action_until_exact_phrase():
    confirm = block(source(MODAL), "function confirm(", "window.DPSettingsModal")
    assert "acceptDisabled: !!typedPhrase" in confirm
    assert "handle.setAcceptEnabled(typedInput.value === typedPhrase)" in confirm
    # Enter in the typed field submits only once the accept action is enabled (shell-owned key handling).
    assert "event.key === 'Enter'" in source(MODAL)
    assert "!entry.accept.disabled" in source(MODAL)


def test_settings_action_rerenders_use_viewport_preserving_refresh():
    js = source(SETTINGS)
    for start, end in [
        ("async function sendReport", "async function runBackup"),
        ("async function wipeDatabaseClean", "async function clearPassword"),
        ("async function setApiTokenEnabled", "async function generateToken"),
        ("async function generateToken", "async function clearToken"),
        ("async function clearToken", "async function copyToken"),
        ("async function finishOidc", "function armOidc"),
    ]:
        section = block(js, start, end)
        if "render" in section:
            assert "renderPreservingViewport();" in section
            assert "\n      render();" not in section
