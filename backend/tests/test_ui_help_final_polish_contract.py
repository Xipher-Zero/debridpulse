from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _block(css: str, selector: str) -> str:
    start = css.index(selector)
    end = css.index("\n}", start)
    return css[start:end]




def test_license_closing_packaging_note_is_centered_and_emphasized():
    css = _read("ui-help-page.css")
    block = _block(css, "#view-help .dp-help-license-note p:last-child {")

    assert "text-align: center" in block
    assert "font-weight: 650" in block
    assert "color: var(--dp-text-primary)" in block




def test_help_section_selector_matches_settings_geometry_and_emphasis():
    css = _read("ui-help-chrome.css")

    tabs = _block(css, "#view-help .dp-help-tabs {")
    assert "width: max-content" in tabs
    assert "max-width: 100%" in tabs
    assert "margin: 0" in tabs
    # Centred as Settings' strip is: a trailing space that grows as the copy
    # does; the header is one row through the desktop, never stacked at 1699px.
    spacer = _block(css, "#view-help .dp-help-master-header::after {")
    assert "flex: 1 1 0" in spacer and "margin-left: -16px" in spacer
    assert "@media (max-width: 1699px)" not in css

    tab = _block(css, "#view-help .dp-help-tab {")
    assert "appearance: none" in tab
    assert "min-height: 32px" in tab
    assert "flex: 0 0 auto" in tab
    assert "gap: 7px" in tab
    assert "padding-inline: 10px" in tab
    assert "border: 0" in tab
    assert "font: inherit" in tab

    chip = _block(css, "#view-help .dp-help-tab-chip {")
    glyph = _block(css, "#view-help .dp-help-tab-glyph {")
    assert "width: 22px" in chip
    assert "height: 22px" in chip
    assert "border-radius: 6px" in chip
    assert "width: 13px" in glyph
    assert "height: 13px" in glyph
    assert "saturate(1.5)" in glyph
    assert "contrast(1.05)" in glyph
