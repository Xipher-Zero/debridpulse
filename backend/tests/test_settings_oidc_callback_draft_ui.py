from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
CALLBACK_JS = STATIC / "ui-settings-authentication-callback.js"
POLISH_JS = STATIC / "ui-settings-authentication-polish.js"
LOADER = STATIC / "ui-presentation-loader.js"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_callback_runtime_loads_after_oidc_regrouping():
    loader = read(LOADER)
    assert "/ui-settings-authentication-callback.js?v=1" in loader
    assert loader.index("/ui-settings-authentication-oidc.js?v=1") < loader.index(
        "/ui-settings-authentication-callback.js?v=1"
    )


def test_callback_is_derived_from_live_unsaved_public_base_url():
    source = read(CALLBACK_JS)
    assert "const CALLBACK_PATH = '/auth/oidc/callback';" in source
    assert "Set Public DebridPulse Base URL to display the Callback URL." in source
    assert "function callbackFromPublicBase(value)" in source
    assert "parsed = new URL(raw);" in source
    assert "parsed.protocol !== 'https:' || !parsed.hostname" in source
    assert "parsed.username || parsed.password || parsed.search || parsed.hash" in source
    assert "parsed.pathname !== '/' && parsed.pathname !== ''" in source
    assert "return origin + CALLBACK_PATH;" in source
    assert "source.addEventListener('input', updatePreview);" in source
    assert "source.addEventListener('change', updatePreview);" in source


def test_callback_remains_read_only_and_copyable():
    source = read(CALLBACK_JS)
    assert "input.id = 'dp-auth-oidc-callback';" in source
    assert "input.readOnly = true;" in source
    assert "button.dataset.action = 'copy-oidc-callback';" in source
    assert "button.textContent = 'Copy';" in source
    assert "navigator.clipboard?.writeText" in source
    assert "Copy this exact URL into your identity provider's redirect/callback URI configuration." in source


def test_callback_draft_runtime_does_not_persist_or_probe_configuration():
    source = read(CALLBACK_JS)
    assert "/auth/config" not in source
    assert "/auth/oidc/verify-config" not in source
    assert "fetch(" not in source
    assert "XMLHttpRequest" not in source
    assert "state.auth?.oidc_callback_url" not in source
    assert "state.auth.oidc_callback_url" not in source


def test_callback_observers_share_one_helper_copy_to_prevent_dom_ping_pong():
    callback = read(CALLBACK_JS)
    polish = read(POLISH_JS)
    helper = "Copy this exact URL into your identity provider's redirect/callback URI configuration."
    assert helper in callback
    assert helper in polish
    assert "ensureHint(field, OIDC_CALLBACK_HINT);" in polish
    assert "Redirect URI to configure with your OpenID Connect provider." not in polish


def test_callback_runtime_is_in_frontend_syntax_gate():
    workflow = read(WORKFLOW)
    assert "node --check frontend/static/ui-settings-authentication-callback.js" in workflow
