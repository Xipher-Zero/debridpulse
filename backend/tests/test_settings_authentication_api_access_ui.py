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

    # DP 1.0.13: status is coloured TEXT on the card's shared operational rail
    # -- the same node, vocabulary and tones every provider card uses. A badge
    # of its own would have been a second status grammar.
    badge = js[js.index("function tokenReadyStatus(a)"):]
    badge = badge[:badge.index("\n  }")]
    assert "a?.api_token_configured" in badge
    assert "tone: 'success'" in badge
    assert "state.oneTimeToken" not in badge
    assert "paintCardStatus(view, '.dp-settings-api-access-card', tokenReadyStatus(a));" in js
    assert "dp-settings-auth-token-ready" not in js and "dp-settings-auth-token-ready" not in css

    # The former body-level reading is gone; the header carries it.
    assert "Stored Token" not in js
    assert "dp-settings-api-token-status" not in js and "dp-settings-api-token-status" not in css

    # Plain text: no capsule, no border, no background of its own.
    assert '.dp-settings-provider-config-status[data-tone="success"]' in source(STATIC / "ui-provider-state.css")
    for capsule in ("border-radius: 999px", "dp-settings-auth-token-ready"):
        assert capsule not in css


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

    block = css[css.index("#view-settings .dp-settings-api-token-disclosure {"):]
    block = block[:block.index("}")]
    assert "var(--dp-state-caution)" in block
    assert "var(--dp-state-caution-bg)" in block
    assert "var(--dp-state-error)" not in block


def test_token_lifecycle_actions_follow_what_the_card_body_actually_holds():
    """Two states, one layout owner.

    With nothing else in the body the lifecycle pair IS the content, so it sits
    centred. While a one-time token is being disclosed the disclosure is the
    content and the pair becomes the actions beside it -- stacked to the right,
    occupying the disclosure's own vertical span rather than being pushed below
    it.
    """
    css = source(STYLE)
    js = source(SETTINGS)

    actions = css[css.index("#view-settings .dp-settings-api-token-actions {"):]
    actions = actions[:actions.index("}")]
    assert "justify-content: center;" in actions
    assert "min-height: var(--dp-input-height);" in actions

    disclosing = css[css.index("#view-settings .dp-settings-api-token-layout.is-disclosing > .dp-settings-api-token-actions {"):]
    disclosing = disclosing[:disclosing.index("}")]
    assert "flex-direction: column;" in disclosing
    assert "justify-content: center;" in disclosing

    # The state is declared by the markup owner, from the one ephemeral value.
    assert "state.oneTimeToken ? ' is-disclosing' : ''" in js
    # Rotate is rendered before Revoke, so the column stacks them in that order.
    assert js.index('data-action="generate-token"') < js.index('data-action="clear-token"')
