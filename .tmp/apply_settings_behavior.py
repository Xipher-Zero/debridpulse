from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS_PATH = ROOT / "frontend" / "static" / "ui-settings-page.js"
CSS_PATH = ROOT / "frontend" / "static" / "ui-settings-page.css"
TEST_PATH = ROOT / "backend" / "tests" / "test_settings_behavior_ui.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


js = JS_PATH.read_text(encoding="utf-8")
css = CSS_PATH.read_text(encoding="utf-8")

helpers = r'''
  function captureSettingsViewport() {
    const settingsScroller = root()?.querySelector('.dp-settings-scroll');
    const shellScroller = document.getElementById('content');
    return {
      settingsTop: Number(settingsScroller?.scrollTop || 0),
      shellTop: Number(shellScroller?.scrollTop || 0),
      windowTop: Number(window.scrollY || 0),
    };
  }

  function restoreSettingsViewport(snapshot) {
    if (!snapshot) return;
    const settingsScroller = root()?.querySelector('.dp-settings-scroll');
    const shellScroller = document.getElementById('content');
    if (settingsScroller) settingsScroller.scrollTop = snapshot.settingsTop;
    if (shellScroller) shellScroller.scrollTop = snapshot.shellTop;
    if (typeof window.scrollTo === 'function') {
      try {
        window.scrollTo({top: snapshot.windowTop, left: window.scrollX || 0, behavior: 'auto'});
      } catch (_) {
        window.scrollTo(0, snapshot.windowTop);
      }
    }
  }

  function renderPreservingViewport() {
    const snapshot = captureSettingsViewport();
    render();
    restoreSettingsViewport(snapshot);
    if (typeof window.requestAnimationFrame === 'function') {
      window.requestAnimationFrame(() => restoreSettingsViewport(snapshot));
    }
  }

  async function confirmAction({
    title,
    message,
    confirmLabel = 'Confirm',
    tone = 'warning',
    typedPhrase = '',
  }) {
    return new Promise(resolve => {
      const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      const overlay = document.createElement('div');
      const dialogId = `dp-settings-confirm-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const titleId = `${dialogId}-title`;
      const messageId = `${dialogId}-message`;
      overlay.className = 'dp-settings-confirm-overlay';
      overlay.innerHTML = `
        <section class="dp-settings-confirm-dialog" data-tone="${tone === 'danger' ? 'danger' : 'warning'}"
                 role="alertdialog" aria-modal="true" aria-labelledby="${titleId}" aria-describedby="${messageId}">
          <header class="dp-settings-confirm-header">
            <div class="dp-settings-confirm-title" id="${titleId}"></div>
          </header>
          <div class="dp-settings-confirm-body">
            <p class="dp-settings-confirm-message" id="${messageId}"></p>
            <label class="dp-settings-confirm-typed" ${typedPhrase ? '' : 'hidden'}>
              <span class="form-label"></span>
              <input class="input" type="text" autocomplete="off" spellcheck="false">
            </label>
          </div>
          <footer class="dp-settings-confirm-footer">
            <button class="btn btn-ghost" type="button" data-confirm-cancel>Cancel</button>
            <button class="btn ${tone === 'danger' ? 'btn-danger' : 'btn-primary'}" type="button" data-confirm-accept></button>
          </footer>
        </section>`;

      const dialog = overlay.querySelector('.dp-settings-confirm-dialog');
      const titleEl = overlay.querySelector('.dp-settings-confirm-title');
      const messageEl = overlay.querySelector('.dp-settings-confirm-message');
      const typed = overlay.querySelector('.dp-settings-confirm-typed');
      const typedLabel = typed?.querySelector('.form-label');
      const typedInput = typed?.querySelector('input');
      const cancel = overlay.querySelector('[data-confirm-cancel]');
      const accept = overlay.querySelector('[data-confirm-accept]');

      titleEl.textContent = String(title || 'Confirm action');
      messageEl.textContent = String(message || '');
      accept.textContent = String(confirmLabel || 'Confirm');

      if (typedPhrase) {
        typed.hidden = false;
        typedLabel.textContent = `Type ${typedPhrase} to confirm.`;
        typedInput.placeholder = typedPhrase;
        accept.disabled = true;
        typedInput.addEventListener('input', () => {
          accept.disabled = typedInput.value !== typedPhrase;
        });
        typedInput.addEventListener('keydown', event => {
          if (event.key === 'Enter' && !accept.disabled) {
            event.preventDefault();
            accept.click();
          }
        });
      }

      let settled = false;
      const finish = value => {
        if (settled) return;
        settled = true;
        overlay.remove();
        if (!document.querySelector('.dp-settings-confirm-overlay')) {
          document.body.classList.remove('dp-settings-confirm-open');
        }
        if (previousFocus?.isConnected) {
          try { previousFocus.focus(); } catch (_) {}
        }
        resolve(value);
      };

      cancel.addEventListener('click', () => finish(false));
      accept.addEventListener('click', () => finish(true));
      overlay.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
          event.preventDefault();
          finish(false);
          return;
        }
        if (event.key !== 'Tab') return;
        const focusable = Array.from(dialog.querySelectorAll('button:not([disabled]), input:not([disabled])'));
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      });

      document.body.appendChild(overlay);
      document.body.classList.add('dp-settings-confirm-open');
      if (typeof window.requestAnimationFrame === 'function') {
        window.requestAnimationFrame(() => cancel.focus());
      } else {
        cancel.focus();
      }
    });
  }

'''
js = replace_once(js, "  function syncGlobalSettings(data) {\n", helpers + "  function syncGlobalSettings(data) {\n", "insert viewport/confirmation helpers")

old_persist_auth = r'''  async function persistAuth(button, payload = authPayload(), successMessage = 'Authentication settings saved') {
    if (!payload.auth_password_enabled && !payload.auth_oidc_enabled && state.auth?.authentication_required) {
      if (!window.confirm('Disable all interactive authentication and place DebridPulse in open mode?')) return false;
      payload.confirm_open_mode = true;
    }

    setBusy(button, true, 'Saving…');
    try {
      state.auth = await request('PUT', '/auth/config', payload, 15000);
      syncAuthIntoSettings(state.auth);
      state.activeTab = 'authentication';
      render();
      notify(successMessage, 'success');
      return true;
'''
new_persist_auth = r'''  async function persistAuth(button, payload = authPayload(), successMessage = 'Authentication settings saved') {
    if (!payload.auth_password_enabled && !payload.auth_oidc_enabled && state.auth?.authentication_required && !payload.confirm_open_mode) {
      const confirmed = await confirmAction({
        title: 'Disable interactive authentication?',
        message: 'Username & Password and OpenID Connect will both be disabled. DebridPulse and its API will be intentionally open.',
        confirmLabel: 'Continue to Open Mode',
        tone: 'warning',
      });
      if (!confirmed) return false;
      payload.confirm_open_mode = true;
    }

    setBusy(button, true, 'Saving…');
    try {
      state.auth = await request('PUT', '/auth/config', payload, 15000);
      syncAuthIntoSettings(state.auth);
      state.activeTab = 'authentication';
      renderPreservingViewport();
      notify(successMessage, 'success');
      return true;
'''
js = replace_once(js, old_persist_auth, new_persist_auth, "replace persistAuth confirmation/render")

old_non_auth = r'''    if (renderAfter) {
      state.activeTab = active;
      render();
    }
'''
new_non_auth = r'''    if (renderAfter) {
      state.activeTab = active;
      renderPreservingViewport();
    }
'''
js = replace_once(js, old_non_auth, new_non_auth, "preserve non-auth Apply viewport")

old_wipe = r'''    if (!window.confirm('This will remove all database rows. Continue?')) return;
    if (window.prompt('Type WIPE to confirm database wipe') !== 'WIPE') return;

    setBusy(button, true, 'Wiping…');
'''
new_wipe = r'''    const confirmed = await confirmAction({
      title: 'Wipe database?',
      message: 'Processing must be paused. This permanently removes all database rows. If Backup Before Wipe is enabled, DebridPulse will create the required backup first.',
      confirmLabel: 'Wipe Database',
      tone: 'danger',
      typedPhrase: 'WIPE',
    });
    if (!confirmed) return;

    setBusy(button, true, 'Wiping…');
'''
js = replace_once(js, old_wipe, new_wipe, "replace database native confirmation")

old_clear_password = r'''  async function clearPassword(button) {
    if (!window.confirm('Clear the stored local password? Username & Password authentication will also be disabled.')) return;
    const payload = authPayload();
    payload.auth_password_enabled = false;
    payload.auth_password = '';
    payload.clear_password = true;
    if (!payload.auth_oidc_enabled && state.auth?.authentication_required) {
      if (!window.confirm('This also leaves no interactive authentication. Continue into open mode?')) return;
      payload.confirm_open_mode = true;
    }
    await persistAuth(button, payload, 'Stored password cleared');
  }
'''
new_clear_password = r'''  async function clearPassword(button) {
    const payload = authPayload();
    const entersOpenMode = !payload.auth_oidc_enabled && state.auth?.authentication_required;
    const confirmed = await confirmAction({
      title: 'Clear stored password?',
      message: entersOpenMode
        ? 'The stored local password will be removed and Username & Password authentication will be disabled. Because OpenID Connect is also disabled, DebridPulse will enter open mode.'
        : 'The stored local password will be removed and Username & Password authentication will be disabled.',
      confirmLabel: 'Clear Password',
      tone: 'danger',
    });
    if (!confirmed) return;

    payload.auth_password_enabled = false;
    payload.auth_password = '';
    payload.clear_password = true;
    if (entersOpenMode) payload.confirm_open_mode = true;
    await persistAuth(button, payload, 'Stored password cleared');
  }
'''
js = replace_once(js, old_clear_password, new_clear_password, "collapse clear-password confirmations")

old_clear_token = r'''  async function clearToken(button) {
    if (!window.confirm('Clear the API token? Existing automation using it will immediately lose access.')) return;
    setBusy(button, true, 'Clearing…');
'''
new_clear_token = r'''  async function clearToken(button) {
    const confirmed = await confirmAction({
      title: 'Revoke API token?',
      message: 'Existing automation using this token will immediately lose access.',
      confirmLabel: 'Revoke Token',
      tone: 'danger',
    });
    if (!confirmed) return;
    setBusy(button, true, 'Clearing…');
'''
js = replace_once(js, old_clear_token, new_clear_token, "replace API token native confirmation")

# Every post-action Settings rerender should preserve the operator's position.
replacements = [
    ("      notify(`Report sent (${result.hours || hours}h)`, 'success');\n      render();\n", "      notify(`Report sent (${result.hours || hours}h)`, 'success');\n      renderPreservingViewport();\n", "report rerender"),
    ("      } catch (_) {}\n      render();\n    } catch (error) {\n", "      } catch (_) {}\n      renderPreservingViewport();\n    } catch (error) {\n", "wipe rerender"),
    ("      state.auth.api_token_configured = !!result.configured;\n      render();\n      notify(`API token ${result.enabled ? 'enabled' : 'disabled'}`, 'success');\n", "      state.auth.api_token_configured = !!result.configured;\n      renderPreservingViewport();\n      notify(`API token ${result.enabled ? 'enabled' : 'disabled'}`, 'success');\n", "token toggle rerender"),
    ("      state.oneTimeToken = text(result.token);\n      render();\n      notify(result.rotated ? 'API token rotated' : 'API token generated', 'success');\n", "      state.oneTimeToken = text(result.token);\n      renderPreservingViewport();\n      notify(result.rotated ? 'API token rotated' : 'API token generated', 'success');\n", "token generation rerender"),
    ("      state.oneTimeToken = '';\n      render();\n      notify('API token cleared', 'success');\n", "      state.oneTimeToken = '';\n      renderPreservingViewport();\n      notify('API token cleared', 'success');\n", "token revoke rerender"),
    ("      state.activeTab = 'authentication';\n      render();\n    } catch (_) {}\n\n    const ok = !!result?.ok;\n", "      state.activeTab = 'authentication';\n      renderPreservingViewport();\n    } catch (_) {}\n\n    const ok = !!result?.ok;\n", "OIDC result rerender"),
]
for old, new, label in replacements:
    js = replace_once(js, old, new, label)

if "window.confirm" in js or "window.prompt" in js:
    raise RuntimeError("native browser confirmation/prompt remains in clean Settings runtime")

css_addition = r'''

/* ── Settings confirmation dialog ─────────────────────────────────────── */
body.dp-settings-confirm-open {
  overflow: hidden;
}

.dp-settings-confirm-overlay {
  position: fixed;
  inset: 0;
  z-index: 5000;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgba(5, 8, 20, .74);
  backdrop-filter: blur(8px) saturate(.92);
}

body.light .dp-settings-confirm-overlay {
  background: rgba(42, 34, 60, .28);
  backdrop-filter: blur(8px) saturate(.88);
}

.dp-settings-confirm-dialog {
  width: min(520px, 100%);
  overflow: hidden;
  border: 1px solid var(--dp-border-subtle);
  border-radius: var(--dp-radius-lg);
  background: var(--dp-panel-surface);
  box-shadow: var(--dp-panel-shadow), 0 24px 70px rgba(0, 0, 12, .42);
}

body.light .dp-settings-confirm-dialog {
  box-shadow: var(--dp-panel-shadow), 0 22px 58px rgba(45, 39, 68, .18);
}

.dp-settings-confirm-dialog[data-tone="warning"] {
  border-color: color-mix(in srgb, var(--dp-state-caution) 48%, var(--dp-border-subtle));
}

.dp-settings-confirm-dialog[data-tone="danger"] {
  border-color: color-mix(in srgb, var(--dp-state-error) 52%, var(--dp-border-subtle));
}

.dp-settings-confirm-header {
  min-height: 58px;
  display: flex;
  align-items: center;
  padding: 0 18px;
  border-bottom: 1px solid var(--dp-panel-header-border);
  background: var(--dp-panel-header-surface);
}

.dp-settings-confirm-dialog[data-tone="warning"] .dp-settings-confirm-header {
  box-shadow: inset 0 2px 0 color-mix(in srgb, var(--dp-state-caution) 74%, transparent);
}

.dp-settings-confirm-dialog[data-tone="danger"] .dp-settings-confirm-header {
  box-shadow: inset 0 2px 0 color-mix(in srgb, var(--dp-state-error) 78%, transparent);
}

.dp-settings-confirm-title {
  color: var(--dp-text-primary);
  font-family: var(--dp-font-sans);
  font-size: 15px;
  line-height: 1.25;
  font-weight: 700;
}

.dp-settings-confirm-body {
  display: grid;
  gap: 18px;
  padding: 20px 20px 18px;
}

.dp-settings-confirm-message {
  margin: 0;
  color: var(--dp-text-secondary);
  font-family: var(--dp-font-sans);
  font-size: 13px;
  line-height: 1.55;
}

.dp-settings-confirm-typed {
  display: grid;
  gap: 7px;
}

.dp-settings-confirm-typed[hidden] {
  display: none;
}

.dp-settings-confirm-typed .input {
  width: 100%;
}

.dp-settings-confirm-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 14px 18px 18px;
  border-top: 1px solid var(--dp-border-subtle);
}

.dp-settings-confirm-footer .btn {
  min-width: 104px;
}

@media (max-width: 620px) {
  .dp-settings-confirm-overlay {
    padding: 12px;
  }

  .dp-settings-confirm-footer {
    flex-direction: column-reverse;
  }

  .dp-settings-confirm-footer .btn {
    width: 100%;
  }
}
'''
if ".dp-settings-confirm-overlay" in css:
    raise RuntimeError("confirmation dialog CSS already exists")
css = css.rstrip() + css_addition + "\n"

TEST_PATH.write_text(r'''from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
SETTINGS = STATIC / "ui-settings-page.js"
STYLE = STATIC / "ui-settings-page.css"


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
    css = source(STYLE)
    assert "async function confirmAction" in js
    assert 'role="alertdialog"' in js
    assert 'aria-modal="true"' in js
    assert "event.key === 'Escape'" in js
    assert "event.key !== 'Tab'" in js
    assert "previousFocus?.isConnected" in js
    assert "cancel.focus()" in js
    assert "window.confirm" not in js
    assert "window.prompt" not in js
    assert ".dp-settings-confirm-overlay" in css
    assert ".dp-settings-confirm-dialog" in css
    assert 'data-tone="warning"' in css
    assert 'data-tone="danger"' in css


def test_destructive_settings_actions_share_confirmation_primitive():
    js = source(SETTINGS)
    persist_auth = block(js, "async function persistAuth", "async function saveCurrent")
    wipe = block(js, "async function wipeDatabaseClean", "async function clearPassword")
    password = block(js, "async function clearPassword", "async function setApiTokenEnabled")
    token = block(js, "async function clearToken", "async function copyToken")

    assert "await confirmAction" in persist_auth
    assert "Continue to Open Mode" in persist_auth
    assert "!payload.confirm_open_mode" in persist_auth

    assert "await confirmAction" in password
    assert password.count("await confirmAction") == 1
    assert "entersOpenMode" in password
    assert "payload.confirm_open_mode = true" in password

    assert "await confirmAction" in token
    assert "Revoke API token?" in token
    assert "Revoke Token" in token

    assert "await confirmAction" in wipe
    assert "typedPhrase: 'WIPE'" in wipe
    assert "Wipe Database" in wipe


def test_typed_confirmation_gates_destructive_action_until_exact_phrase():
    js = source(SETTINGS)
    confirm = block(js, "async function confirmAction", "function syncGlobalSettings")
    assert "accept.disabled = true" in confirm
    assert "accept.disabled = typedInput.value !== typedPhrase" in confirm
    assert "event.key === 'Enter' && !accept.disabled" in confirm


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
''', encoding="utf-8")

JS_PATH.write_text(js, encoding="utf-8")
CSS_PATH.write_text(css, encoding="utf-8")

# Lightweight validation that does not depend on project Python dependencies.
assert "window.confirm" not in js
assert "window.prompt" not in js
assert js.count("async function confirmAction") == 1
assert "typedPhrase: 'WIPE'" in js
assert "renderPreservingViewport();" in js
assert ".dp-settings-confirm-overlay" in css
print("settings behavior patch applied")
