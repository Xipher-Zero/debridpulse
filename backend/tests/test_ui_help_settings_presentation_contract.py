from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_help_chrome_loads_after_clean_help_runtime():
    loader = read("ui-presentation-loader.js")
    assert "/ui-help-chrome.css?v=1" in loader
    assert loader.index("/ui-help-page.js?v=1") < loader.index("/ui-help-chrome.js?v=1")


def test_help_tabs_use_settings_style_topical_lucide_chips():
    js = read("ui-help-chrome.js")
    css = read("ui-help-chrome.css")

    expected = {
        "quickstart": "rocket",
        "howitworks": "workflow",
        "aria2": "download",
        "integrations": "plug",
        "settings": "settings",
        "trouble": "wrench",
        "license": "scroll-text",
    }
    for tab, icon in expected.items():
        assert f"{tab}: '{icon}'" in js
        assert (STATIC / "icons" / "lucide" / f"{icon}.svg").is_file()

    assert "dp-help-tab-chip" in js
    assert "dp-help-tab-glyph" in js
    assert "width: 22px;" in css
    assert "width: 13px;" in css
    assert "drop-shadow" in css


def test_help_content_uses_full_width_settings_style_card_structure():
    js = read("ui-help-chrome.js")
    css = read("ui-help-chrome.css")

    assert "card-header dp-help-section-card-header" in js
    assert "card-body dp-help-section-card-body" in js
    assert "section.classList.add('card', 'dp-help-section-card')" in js
    assert "section.dataset.dpHelpCard = '1'" in js
    assert "new MutationObserver" not in js

    assert ".dp-help-document.dp-help-section-card" in css
    assert "max-width: none;" in css
    assert "margin: 0;" in css
    assert "padding: 12px 12px 16px;" in css


def test_oidc_public_origin_label_names_debridpulse_explicitly():
    js = read("ui-settings-authentication-callback.js")
    assert "const PUBLIC_BASE_LABEL = 'Public DebridPulse Base URL';" in js
    assert "Set Public DebridPulse Base URL to display the Callback URL." in js
