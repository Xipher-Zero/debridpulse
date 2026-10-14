from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
STYLE = STATIC / "ui-settings-authentication.css"
SETTINGS = STATIC / "ui-settings-page.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_retired_global_api_token_has_no_settings_surface():
    """1.0.13 ACL: API tokens belong to Integration accounts (Users & Access).

    The single global token and its endpoints are retired server-side, so the
    API Access card, its KPI and its persistence scope are removed at their
    owner rather than left as a misleading parallel credential."""
    js = source(SETTINGS)
    css = source(STYLE)
    assert "card('API Access'" not in js
    assert "/auth/api-token" not in js
    assert "api_token_enabled" not in js
    assert "oneTimeToken" not in js
    assert "'api-token'" not in js
    assert "dp-settings-api-" not in js and "dp-settings-api-" not in css


def test_integration_token_disclosure_is_one_time_and_never_page_state():
    js = source(SETTINGS)
    css = source(STYLE)

    issue = js[js.index("async function issueToken(account)"):js.index("async function revokeToken(")]
    # The raw value lives only in the disclosure modal's own input: never in
    # page state, never a setting, never re-rendered after the modal closes.
    assert "field.value = issued.token;" in issue
    assert "state." not in issue
    assert "data-setting" not in issue and "data-commit" not in issue
    assert "readonly" in issue
    assert "Copy this token now. DebridPulse will not display it again." in issue
    assert "embeddedActionField(" in issue
    assert "request('POST', `/accounts/${encodeURIComponent(account.id)}/tokens`" in issue

    block = css[css.index(".dp-token-disclosure {"):]
    block = block[:block.index("}")]
    assert "var(--dp-state-caution)" in block
    assert "var(--dp-state-caution-bg)" in block
    assert "var(--dp-state-error)" not in block
