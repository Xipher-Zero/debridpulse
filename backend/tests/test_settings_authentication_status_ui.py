import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
RUNTIME = STATIC / "ui-settings-authentication.js"
STYLE = STATIC / "ui-settings-authentication.css"
SETTINGS = STATIC / "ui-settings-page.js"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")










def test_session_information_and_control_form_one_centred_bounded_island():
    """Both session readings sit in ONE island beneath the KPI row.

    Same relationship container and the same padding density as the accepted
    Extraction behaviour island, so the two surfaces speak one visual language.
    Content-bounded and centred, never a band stretched across the card.
    """
    css = source(STYLE)
    island = css[css.index("#view-settings .dp-settings-auth-status-card .dp-settings-auth-session-row {"):]
    island = island[:island.index("}")]

    assert "width: max-content;" in island
    assert "max-width: min(100%, 880px);" in island
    assert "margin: 14px auto 0;" in island
    assert "border: 1px solid var(--dp-divider);" in island
    assert "border-radius: 12px;" in island
    assert "padding: 16px 22px 17px;" in island
    assert "gap: 18px 44px;" in island

    # Stacked, the island stops being content-bounded: there is one column.
    narrow = css[css.index("@media (max-width: 900px)"):]
    assert ".dp-settings-auth-session-row" in narrow
    assert "flex-direction: column;" in narrow


def test_log_out_lives_inside_the_sessions_field_as_a_flex_sibling_of_the_count():
    """No reserved width stands in for the button.

    The count and the action are siblings in the one shared inline grammar, so
    the space the button occupies is real at any font metric rather than a
    constant this stylesheet guessed.
    """
    js = source(SETTINGS)
    css = source(STYLE)

    session = js[js.index("<div class=\"dp-settings-auth-session-row\""):js.index("`, {className: 'dp-settings-auth-status-card'})")]
    assert "inlineField('', 'Active Browser Sessions'" in session
    assert "className: 'dp-settings-auth-session-count'" in session
    assert 'data-action="logout-session"' in session
    assert "btn-ghost" in session
    assert "data-auth-session-count" in session
    # One logout path, and it is the existing one.
    assert js.count('data-action="logout-session"') == 1
    assert "window.debridPulseAuth.logout()" in js

    assert "dp-settings-auth-session-actions" not in css
    # No declared width stands in for the button's real, font-decided size.
    declarations = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert "reserve" not in declarations.lower()
