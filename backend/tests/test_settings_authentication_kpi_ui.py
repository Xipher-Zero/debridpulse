from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
SETTINGS = STATIC / "ui-settings-page.js"
RESILIENCE = STATIC / "ui-settings-auth-resilience.js"
STYLE = STATIC / "ui-settings-authentication.css"
OIDC_JS = STATIC / "ui-settings-authentication-oidc.js"
OIDC_CSS = STATIC / "ui-settings-authentication-oidc.css"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_authentication_status_uses_four_dashboard_derived_state_kpis():
    js = source(SETTINGS)
    css = source(STYLE)
    block = js[js.index("function authStatusCard"):js.index("function authenticationPanel")]

    assert "['Authentication Mode', modeValue, modeTone]" in block
    assert "['Username & Password', passwordValue, passwordTone]" in block
    assert "['OIDC State', oidcState.primary, oidcState.tone, oidcState.secondary]" in block
    assert "['API Token', tokenValue, tokenTone]" in block
    assert 'class="dash-hero-stat dp-settings-auth-kpi"' in block
    assert "dhs-body" in block and "dhs-label" in block and "dhs-val" in block
    assert "dhs-icon" not in block
    assert "dp-card-spark" not in block
    assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in css
    assert ".dp-settings-auth-kpi.dash-hero-stat::before" in css


def test_authentication_kpi_state_ladders_are_semantic_and_truthful():
    js = source(SETTINGS)
    css = source(STYLE)

    assert "passwordValue = 'Configured & Enabled';" in js
    assert "const oidcState = oidcStatePresentation(a);" in js
    assert "tokenValue = 'Configured & Enabled';" in js
    assert "Configuration Error" in js
    assert 'data-c="${html(tone)}"' in js
    assert '[data-c="green"]' in css and "var(--dp-state-success)" in css
    assert '[data-c="yellow"]' in css and "var(--dp-state-caution)" in css
    assert '[data-c="red"]' in css and "var(--dp-state-error)" in css
    assert '[data-c="neutral"]' in css
    assert '[data-c="green"] .dhs-val' in css
    assert '[data-c="yellow"] .dhs-val' in css
    assert '[data-c="red"] .dhs-val' in css


def test_open_auth_notice_is_removed_as_redundant_with_authentication_mode_kpi():
    js = source(SETTINGS)
    css = source(STYLE)
    assert "No interactive authentication enabled" not in js
    assert ".dp-settings-auth-open-notice" not in css


def test_oidc_minor_copy_and_access_centering_are_locked():
    js = source(OIDC_JS)
    css = source(OIDC_CSS)
    assert "Stored Client Secret Configured. Blank keeps it." in js
    assert ".dp-settings-oidc-section-copy" in css
    assert "position: absolute;" in css
    assert "left: 50%;" in css
    assert "transform: translate(-50%, -50%);" in css
    assert "position: static;" in css

def test_oidc_state_lifecycle_copy_tone_and_untested_line_are_locked():
    js = source(SETTINGS)
    resilience = source(RESILIENCE)
    resolver = js[js.index("function oidcStatePresentation"):js.index("function notify")]
    auth_block = js[js.index("function authStatusCard"):js.index("function authenticationPanel")]

    assert "{primary: 'Disabled', secondary: '', tone: 'neutral'}" in resolver
    assert "{primary: 'Configured', secondary: '', tone: 'yellow'}" in resolver
    assert "primary: 'Configured & Enabled'" in resolver
    assert "secondary: '(Untested)'" in resolver
    assert "tone: 'yellow'" in resolver
    assert "{primary: 'Enabled', secondary: '', tone: 'green'}" in resolver
    assert "Verified · Runtime Unavailable" in resolver
    assert "Runtime Unavailable" in resolver
    assert "Configuration Error" in resolver
    assert "Enabled & Verified" not in resolver
    assert "Not Configured" not in resolver

    assert "secondary = ''" in auth_block
    assert '<br><span class="dp-settings-auth-kpi-secondary">' in auth_block
    assert "window.DPSettingsOidcStatePresentation" in js
    assert "window.DPSettingsOidcStatePresentation" in resilience
    assert "oidcStatePresentation?.resolve?.(auth, available)" in resilience
    assert "renderOidcKpiValue(value, presentation);" in resilience
    assert "Enabled & Verified" not in resilience
    assert "Configured & Enabled" not in resilience

