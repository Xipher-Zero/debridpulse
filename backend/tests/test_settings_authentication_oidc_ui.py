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


def test_oidc_card_groups_origin_and_callback_and_uses_shared_field_datum():
    source = read(OIDC_JS)
    css = read(OIDC_CSS)
    assert "createRow('dp-settings-oidc-row--origin', [publicBase, callback])" in source
    assert "Copy this redirect URI into your identity provider." in source
    assert "field.classList.add('dp-settings-oidc-sandwich')" in source
    assert "window.getComputedStyle(control)" not in source
    assert "paddingInlineStart" not in source
    assert "inset-inline-start: 3px;" in css
    assert "grid-template-columns: minmax(0, 1.85fr) minmax(380px, 1fr);" in css
    assert ".dp-settings-oidc-grouped-card .dp-settings-auth-callback-field" in css
    assert "width: 100%;" in css


def test_oidc_access_control_is_one_section_with_three_parallel_allowlists():
    source = read(OIDC_JS)
    css = read(OIDC_CSS)
    assert "Access Control" in source
    assert "Choose whether any authenticated OIDC identity is accepted or restrict sign-in to the allowlists below." in source
    assert "heading.append(headingTitle, headingCopy, allowAll)" in source
    assert "allowlists.append(subjects, emails, groups)" in source
    assert "Allowed Subjects" in source
    assert "Allowed Emails" in source
    assert "Allowed Groups" in source
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in css


def test_oidc_allow_any_policy_lives_in_access_header_with_title_left_of_toggle():
    source = read(OIDC_JS)
    css = read(OIDC_CSS)
    assert "title.textContent = 'Allow Any Authenticated OIDC Identity'" in source
    assert "info?.querySelector('.td')?.remove();" in source
    assert ".dp-settings-oidc-section-heading > .dp-settings-oidc-allow-all" in css
    assert "display: inline-flex;" in css
    assert "justify-self: end;" in css
    assert ".dp-settings-oidc-allow-all .toggle-info .tl" in css
    assert "white-space: nowrap;" in css


def test_oidc_clear_secret_preserves_clear_on_apply_semantics():
    source = read(OIDC_JS)
    settings = read(SETTINGS_JS)
    assert "#dp-auth-clear-oidc-secret" in source
    assert "Clear Stored Secret" in source
    assert "Remove the saved secret when settings are applied." in source
    assert "clear_oidc_client_secret: !!byId('dp-auth-clear-oidc-secret')?.checked" in settings
    assert "/auth/oidc" not in source


def test_oidc_clear_secret_is_third_credentials_control_on_field_centerline():
    source = read(OIDC_JS)
    css = read(OIDC_CSS)
    assert "const clearSecret = configureClearSecret(secret);" in source
    assert "createRow('dp-settings-oidc-row--credentials', [clientId, secret, clearSecret])" in source
    assert "grid-template-columns: minmax(300px, .8fr) minmax(420px, 1.2fr) auto;" in css
    assert ".dp-settings-oidc-clear-secret-action" in css
    assert "grid-template-rows: auto var(--dp-input-height) auto;" in css
    assert ".dp-settings-oidc-clear-secret-control" in css
    assert "min-height: var(--dp-input-height);" in css
    assert "align-items: center;" in css


def test_oidc_clear_secret_helper_collapses_unused_lower_half_of_input_slot():
    css = read(OIDC_CSS)
    assert "--dp-oidc-clear-copy-line: 13.75px;" in css
    assert "line-height: var(--dp-oidc-clear-copy-line);" in css
    assert "margin-top: calc(7px - ((var(--dp-input-height) - var(--dp-oidc-clear-copy-line)) / 2));" in css


def test_oidc_access_separator_is_strengthened_without_added_weight_or_accent():
    css = read(OIDC_CSS)
    assert "border-top: 1px solid color-mix(in srgb, var(--dp-border, var(--border)) 65%, var(--dp-text-muted) 35%);" in css
    assert "border-top: 2px" not in css


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
