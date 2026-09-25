from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-authentication.js"
STYLE = STATIC / "ui-settings-authentication.css"
SETTINGS = STATIC / "ui-settings-page.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")






def test_token_ready_is_derived_from_durable_stored_token_state_alone():
    """The header status answers "is a token stored", not "is one on screen".

    Reading it off the disclosure block would make the badge disappear on the
    next render, and reading it off a visible token value would make it true
    for a token that was never saved. Only ``api_token_configured`` -- what the
    server says it holds -- decides it, in the markup and in the painter alike.
    """
    js = source(SETTINGS)
    css = source(STYLE)

    badge = js[js.index("function tokenReadyBadge(a)"):]
    badge = badge[:badge.index("\n  }")]
    assert "a?.api_token_configured ? '' : ' hidden'" in badge
    assert "state.oneTimeToken" not in badge
    assert "tokenReady.hidden = !a.api_token_configured;" in js

    # The former body-level reading is gone; the header carries it.
    assert "Stored Token" not in js
    assert "dp-settings-api-token-status" not in js and "dp-settings-api-token-status" not in css

    assert ".dp-settings-auth-token-ready" in css
    assert "color: var(--dp-state-success);" in css
    assert ".dp-settings-auth-token-ready[hidden]" in css


def test_one_time_token_disclosure_is_ephemeral_and_never_configuration_state():
    js = source(SETTINGS)
    css = source(STYLE)

    disclosure = js[js.index("const disclosure = state.oneTimeToken"):js.index("const apiAccess = card('API Access'")]
    # A disclosure, not a setting: invisible to the persistence owner, to every
    # baseline, and to every payload this page builds.
    assert 'id="dp-settings-api-token-once"' in disclosure
    assert "data-setting" not in disclosure
    assert "data-commit" not in disclosure
    assert "readonly" in disclosure
    # Exact warning copy, in the Settings gold language, inside one container.
    assert "Copy this token now. DebridPulse will not display it again." in disclosure
    assert "dp-settings-api-token-disclosure" in disclosure
    assert "embeddedActionField(" in disclosure

    assert "api_token_once" not in js  # never a settings key
    # Re-entering Settings is not the act that produced it.
    assert "state.oneTimeToken = '';" in js[js.index("async function load()"):]

    block = css[css.index(".dp-settings-api-token-disclosure {"):]
    block = block[:block.index("}")]
    assert "var(--dp-state-caution)" in block
    assert "var(--dp-state-caution-bg)" in block
    assert "var(--dp-state-error)" not in block


def test_token_lifecycle_actions_sit_on_the_right_of_the_action_area():
    css = source(STYLE)
    actions = css[css.index("#view-settings .dp-settings-api-token-actions {"):]
    actions = actions[:actions.index("}")]
    assert "justify-content: flex-end;" in actions
    assert "min-height: var(--dp-input-height);" in actions

    js = source(SETTINGS)
    assert 'data-action="generate-token"' in js
    assert 'data-action="clear-token"' in js
