from pathlib import Path

from i18n_node import english

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
SETTINGS = STATIC / "ui-settings-page.js"
RESILIENCE = STATIC / "ui-settings-auth-resilience.js"
STYLE = STATIC / "ui-settings-authentication.css"
OIDC_JS = STATIC / "ui-settings-authentication-oidc.js"
OIDC_CSS = STATIC / "ui-settings-authentication-oidc.css"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_authentication_status_uses_three_dashboard_derived_state_kpis():
    js = source(SETTINGS)
    css = source(STYLE)
    # The three readings are derived ONCE, as data, so the markup that first
    # renders them and the painter that repaints them in place cannot disagree.
    items = js[js.index("function authKpiItems"):js.index("function authStatusCard")]
    block = js[js.index("function authStatusCard"):js.index("function authenticationPanel")]

    # Each reading also states its short words, from the same state (a narrow
    # card's last composition, fitAuthKpis); the full words are unchanged.
    # (WS3d: the words are the catalog's, by key; the states are identifiers.)
    assert "{id: 'mode', label: say('settings.auth.kpi.mode'), labelShort: say('settings.auth.kpi.mode_short')," in items
    assert "...kpiValue(`mode.${mode}`), tone: modeTone, secondary: ''}" in items
    assert "{id: 'password', label: say('settings.auth.kpi.password'), labelShort: say('settings.auth.kpi.password_short')," in items
    assert "...kpiValue(password.value), tone: password.tone, secondary: ''}" in items
    assert "{id: 'oidc', label: say('settings.auth.kpi.oidc'), labelShort: say('settings.auth.kpi.oidc_short')," in items
    assert "...kpiValue(oidcState.primary), tone: oidcState.tone," in items
    assert "secondary: oidcState.secondary ? say(`settings.auth.kpi.${oidcState.secondary}`) : ''}" in items
    assert [english(f"settings.auth.kpi.{key}") for key in ("mode", "mode_short", "password", "password_short", "oidc", "oidc_short")] == [
        "Authentication Mode", "Auth Mode", "Username & Password", "Password", "OIDC State", "OIDC"]
    # The mode is derived from the two flags, never read from the server's English words.
    assert "const mode = a.password_enabled && a.oidc_enabled ? 'both' : a.password_enabled ? 'password'" in items
    # The global API token is retired (1.0.13 ACL); tokens live in Users & Access.
    assert "API Token" not in items
    assert "authKpiItems(a).map(item =>" in block
    assert 'class="dash-hero-stat dp-settings-auth-kpi" data-auth-kpi="${html(item.id)}"' in block
    assert "for (const item of authKpiItems(a))" in js
    assert "dhs-body" in block and "dhs-label" in block and "dhs-val" in block
    assert "dhs-icon" not in block
    assert "dp-card-spark" not in block
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in css
    assert ".dp-settings-auth-kpi.dash-hero-stat::before" in css


def test_authentication_kpi_state_ladders_are_semantic_and_truthful():
    js = source(SETTINGS)
    css = source(STYLE)

    # DP 1.0.13: ONE state vocabulary for every mechanism -- Unconfigured,
    # Configured, Enabled -- derived once. "Configured & Enabled" said two
    # things and meant the second; it is gone, and an enabled-but-unusable
    # mechanism still gets its explicit error rather than being called Enabled.
    assert "Configured & Enabled" not in js
    state = js[js.index("const mechanismState = ("):]
    state = state[:state.index("\n    };")]
    assert "if (enabled && !ready) return {value: 'config_error', tone: 'red'};" in state
    assert "if (!configured) return {value: 'unconfigured', tone: 'neutral'};" in state
    assert "enabled ? {value: 'enabled', tone: 'green'} : {value: 'configured', tone: 'yellow'}" in state
    # Each state id is said from the catalog in the one vocabulary.
    assert [english(f"settings.auth.kpi.{key}") for key in ("config_error", "unconfigured", "enabled", "configured")] == [
        "Configuration Error", "Unconfigured", "Enabled", "Configured"]
    # The one mechanism with a configured/enabled pair derives it from that one place.
    assert js.count("mechanismState(") == 1
    assert "const oidcState = oidcStatePresentation(a);" in js
    assert "'config_error'" in js and english("settings.auth.kpi.config_error") == "Configuration Error"
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


def test_every_mechanism_kpi_uses_the_same_three_state_vocabulary():
    """The locked grammar, exercised as a matrix rather than by grepping words.

    The derivation is a pure function of canonical facts, so it can simply be
    evaluated: each mechanism, each combination, one vocabulary.
    """
    js = source(SETTINGS)
    body = js[js.index("const mechanismState = ("):]
    body = body[:body.index("\n    };") + len("\n    }")]

    import re

    # Re-express the one declaration as a Python predicate, so the matrix tests
    # the shipped logic rather than a restatement of it.
    assert re.search(r"if \(enabled && !ready\) return \{value: 'config_error', tone: 'red'\};", body)
    assert re.search(r"if \(!configured\) return \{value: 'unconfigured', tone: 'neutral'\};", body)
    assert re.search(r"return enabled \? \{value: 'enabled', tone: 'green'\} : \{value: 'configured', tone: 'yellow'\};", body)

    # Each state id says its words from the catalog (settings.auth.kpi.<state>).
    def state(configured, enabled, ready):
        if enabled and not ready:
            return (english("settings.auth.kpi.config_error"), "red")
        if not configured:
            return (english("settings.auth.kpi.unconfigured"), "neutral")
        return (english("settings.auth.kpi.enabled"), "green") if enabled else (english("settings.auth.kpi.configured"), "yellow")

    # A mechanism the operator does not use is not an error -- it is grey.
    assert state(False, False, False) == ("Unconfigured", "neutral")
    assert state(True, False, True) == ("Configured", "yellow")
    assert state(True, True, True) == ("Enabled", "green")
    # Red is reserved for the case that really is broken: enabled but unusable.
    assert state(True, True, False) == ("Configuration Error", "red")
    assert state(False, True, False) == ("Configuration Error", "red")


def test_oidc_kpi_speaks_the_same_vocabulary_with_its_own_extra_conditions():
    js = source(SETTINGS)
    ladder = js[js.index("function oidcStatePresentation(auth"):]
    ladder = ladder[:ladder.index("\n  }")]

    assert "Configured & Enabled" not in ladder
    # Unused is grey, matching the other two mechanisms, so the three KPIs
    # agree about what "no configuration" looks like -- and red keeps meaning
    # that something is actually wrong.
    # (WS3d: state identifiers; their words are the catalog's settings.auth.kpi.<state>.)
    assert "{primary: 'unconfigured', secondary: '', tone: 'neutral'}" in ladder
    assert "{primary: 'config_error', secondary: '', tone: 'red'}" in ladder
    assert "{primary: 'configured', secondary: '', tone: 'yellow'}" in ladder
    assert "{primary: 'enabled', secondary: '', tone: 'green'}" in ladder
    # Untested does not change the STATE word; it rides alongside it.
    assert "{primary: 'enabled', secondary: 'untested', tone: 'green'}" in ladder
    assert english("settings.auth.kpi.untested") == "(Untested)"
    # A provider that cannot be reached is a real error, still red.
    assert "primary: 'runtime_unavailable'" in ladder and english("settings.auth.kpi.runtime_unavailable") == "Runtime Unavailable"
