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
    # The four readings are derived ONCE, as data, so the markup that first
    # renders them and the painter that repaints them in place cannot disagree.
    items = js[js.index("function authKpiItems"):js.index("function authStatusCard")]
    block = js[js.index("function authStatusCard"):js.index("function authenticationPanel")]

    assert "id: 'mode', label: 'Authentication Mode', value: modeValue, tone: modeTone" in items
    assert "id: 'password', label: 'Username & Password', value: passwordValue, tone: passwordTone" in items
    assert "id: 'oidc', label: 'OIDC State', value: oidcState.primary, tone: oidcState.tone, secondary: oidcState.secondary" in items
    assert "id: 'token', label: 'API Token', value: tokenValue, tone: tokenTone" in items
    assert "authKpiItems(a).map(item =>" in block
    assert 'class="dash-hero-stat dp-settings-auth-kpi" data-auth-kpi="${html(item.id)}"' in block
    assert "for (const item of authKpiItems(a))" in js
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
    assert 'data-c="${html(item.tone)}"' in js
    assert "kpi.dataset.c = item.tone;" in js
    assert '[data-c="green"]' in css and "var(--dp-state-success)" in css
    assert '[data-c="yellow"]' in css and "var(--dp-state-caution)" in css
    assert '[data-c="red"]' in css and "var(--dp-state-error)" in css
    assert '[data-c="neutral"]' in css
    assert '[data-c="green"] .dhs-val' in css
    assert '[data-c="yellow"] .dhs-val' in css
    assert '[data-c="red"] .dhs-val' in css


def test_current_authentication_mechanism_reading_is_removed_as_redundant_with_the_mode_kpi():
    js = source(SETTINGS)
    assert "Current Authentication Mechanism" not in js
    assert "function mechanismLabel(" not in js
    assert "'Password Session'" not in js


def test_open_auth_notice_is_removed_as_redundant_with_authentication_mode_kpi():
    js = source(SETTINGS)
    css = source(STYLE)
    assert "No interactive authentication enabled" not in js
    assert ".dp-settings-auth-open-notice" not in css
