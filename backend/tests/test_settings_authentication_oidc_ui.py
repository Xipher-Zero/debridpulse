from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
OIDC_JS = STATIC / "ui-settings-authentication-oidc.js"
OIDC_CSS = STATIC / "ui-settings-authentication-oidc.css"
LOADER = STATIC / "ui-presentation-loader.js"
SETTINGS_JS = STATIC / "ui-settings-page.js"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_oidc_presentation_assets_are_loaded_after_authentication_polish():
    source = read(LOADER)
    assert "/ui-settings-authentication-oidc.css?v=1" in source
    assert "/ui-settings-authentication-oidc.js?v=1" in source
    assert source.index("/ui-settings-authentication-polish.css?v=1") < source.index("/ui-settings-authentication-oidc.css?v=1")
    assert source.index("/ui-settings-authentication-polish.js?v=1") < source.index("/ui-settings-authentication-oidc.js?v=1")


def test_oidc_card_uses_master_header_and_grouped_provider_rows():
    source = read(OIDC_JS)
    assert "Configure an external identity provider for browser sign-in." in source
    assert "title.textContent = 'Enable'" in source
    assert "dp-settings-oidc-row--origin" in source
    assert "dp-settings-oidc-row--identity" in source
    assert "dp-settings-oidc-row--credentials" in source
    assert "dp-settings-oidc-row--protocol" in source
    assert "'Provider Name', 'Name shown on the sign-in page.'" in source
    assert "'Scopes', 'Space-separated scopes requested during sign-in.'" in source


def test_oidc_card_groups_origin_and_callback_and_aligns_to_input_text():
    source = read(OIDC_JS)
    css = read(OIDC_CSS)
    assert "createRow('dp-settings-oidc-row--origin', [publicBase, callback])" in source
    assert "Copy this redirect URI into your identity provider." in source
    assert "window.getComputedStyle(control)" in source
    assert "paddingInlineStart" in source
    assert "--dp-settings-oidc-input-text-inset" in source
    assert "grid-template-columns: minmax(0, 1.85fr) minmax(380px, 1fr);" in css
    assert ".dp-settings-oidc-grouped-card .dp-settings-auth-callback-field" in css
    assert "width: 100%;" in css


def test_oidc_access_control_is_one_section_with_three_parallel_allowlists():
    source = read(OIDC_JS)
    css = read(OIDC_CSS)
    assert "Access Control" in source
    assert "Choose who can sign in through this provider." in source
    assert "Allow any successful OIDC sign-in. Turn off to enforce the allowlists below." in source
    assert "allowlists.append(subjects, emails, groups)" in source
    assert "Allowed Subjects" in source
    assert "Allowed Emails" in source
    assert "Allowed Groups" in source
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in css


def test_oidc_clear_secret_preserves_clear_on_apply_semantics():
    source = read(OIDC_JS)
    settings = read(SETTINGS_JS)
    assert "#dp-auth-clear-oidc-secret" in source
    assert "Clear Stored Secret" in source
    assert "Remove the saved secret when settings are applied." in source
    assert "clear_oidc_client_secret: !!byId('dp-auth-clear-oidc-secret')?.checked" in settings
    assert "/auth/oidc" not in source


def test_oidc_sign_in_test_moves_to_authentication_context_footer_without_redefining_behavior():
    source = read(OIDC_JS)
    settings = read(SETTINGS_JS)
    assert "button.textContent = 'Test OIDC Sign-In'" in source
    assert "button.dataset.contextAction = 'authentication'" in source
    assert "contextActions.appendChild(button)" in source
    assert "button[data-action=\"verify-oidc\"]" in source
    assert "else if (action === 'verify-oidc') verifyOidc(button);" in settings
    assert "const payload = authPayload();" in settings
    assert "'/auth/oidc/verify-config'" in settings
