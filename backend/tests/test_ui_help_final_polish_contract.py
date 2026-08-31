from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _block(css: str, selector: str) -> str:
    start = css.index(selector)
    end = css.index("\n}", start)
    return css[start:end]


def test_help_download_engine_tab_uses_user_facing_label():
    chrome = _read("ui-help-chrome.js")

    assert "aria2: 'Download Engine'" in chrome
    assert "LABELS[tab.dataset.tab]" in chrome


def test_license_closing_packaging_note_is_centered_and_emphasized():
    css = _read("ui-help-page.css")
    block = _block(css, "body.dp-v11-structural #view-help .dp-help-license-note p:last-child {")

    assert "text-align: center" in block
    assert "font-weight: 650" in block
    assert "color: var(--dp-text-primary)" in block


def test_help_master_header_uses_review_placeholder_flavor_text():
    chrome = _read("ui-help-chrome.js")
    css = _read("ui-help-chrome.css")

    assert "function normalizeMasterHeader(view)" in chrome
    assert "subtitle.className = 'dp-help-header-subtitle'" in chrome
    assert "subtitle.textContent = 'To Be Determined'" in chrome

    subtitle = _block(css, "body.dp-v11-structural #view-help .dp-help-header-subtitle {")
    assert "margin-top: 4px" in subtitle
    assert "font-size: 11px" in subtitle
    assert "line-height: 1.35" in subtitle


def test_help_section_selector_matches_settings_geometry_and_emphasis():
    css = _read("ui-help-chrome.css")

    tabs = _block(css, "body.dp-v11-structural #view-help .dp-help-tabs {")
    assert "grid-column: 2" in tabs
    assert "width: max-content" in tabs
    assert "max-width: 100%" in tabs
    assert "margin: 0" in tabs
    assert "justify-self: center" in tabs

    tab = _block(css, "body.dp-v11-structural #view-help .dp-help-tab {")
    assert "appearance: none" in tab
    assert "min-height: 32px" in tab
    assert "flex: 0 0 auto" in tab
    assert "gap: 7px" in tab
    assert "padding-inline: 10px" in tab
    assert "border: 0" in tab
    assert "font: inherit" in tab

    chip = _block(css, "body.dp-v11-structural #view-help .dp-help-tab-chip {")
    glyph = _block(css, "body.dp-v11-structural #view-help .dp-help-tab-glyph {")
    assert "width: 22px" in chip
    assert "height: 22px" in chip
    assert "border-radius: 6px" in chip
    assert "width: 13px" in glyph
    assert "height: 13px" in glyph
    assert "saturate(1.5)" in glyph
    assert "contrast(1.05)" in glyph
