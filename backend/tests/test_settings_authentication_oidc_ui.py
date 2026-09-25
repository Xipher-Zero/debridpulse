from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
AUTH_CSS = STATIC / "ui-settings-authentication.css"
SETTINGS_JS = STATIC / "ui-settings-page.js"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_clearing_the_stored_oidc_secret_is_one_confirmed_action_not_a_deferred_checkbox():
    """Erasing a credential is an explicit confirmed act, with ONE path.

    The former checkbox armed a clear that only happened at Apply, which meant a
    destructive intent could sit latent in the page and be carried by an
    unrelated save. Both the checkbox and its clear-on-save payload are gone.
    """
    css = read(AUTH_CSS)
    js = read(SETTINGS_JS)

    assert "dp-settings-oidc-clear-secret" not in css
    assert "--dp-oidc-clear-copy-line" not in css
    assert "dp-auth-clear-oidc-secret" not in js
    assert "clear-on-save" not in js

    # One button, one canonical confirmation, one canonical clear.
    assert 'data-action="clear-oidc-secret"' in js
    assert "async function clearOidcSecret(button)" in js
    assert js.count("async function clearOidcSecret(") == 1
    assert "window.DPSettingsModal.confirm({" in js
    assert "writeAuthentication({clear_oidc_client_secret: true})" in js
    assert js.count("clear_oidc_client_secret") == 1


def test_oidc_access_separator_is_strengthened_without_added_weight_or_accent():
    css = read(AUTH_CSS)
    assert "border-top: 1px solid color-mix(in srgb, var(--dp-border, var(--border)) 65%, var(--dp-text-muted) 35%);" in css
    assert "border-top: 2px" not in css


def test_oidc_stylesheet_is_not_a_separate_runtime_owner():
    assert not (STATIC / "ui-settings-authentication-oidc.css").exists()
