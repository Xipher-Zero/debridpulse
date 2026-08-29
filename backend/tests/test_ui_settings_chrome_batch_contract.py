"""Reviewed Settings chrome batch: identity, tabs, provider art, and action glyphs."""

from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
CHROME = STATIC / "ui-settings-chrome.css"
RUNTIME = STATIC / "ui-settings-page.js"
FEATURE = STATIC / "ui-feature-icon-contract.css"
STYLE = STATIC / "style-v11.css"
MANIFEST = STATIC / "icons" / "dp" / "manifest.json"
LUCIDE = STATIC / "icons" / "lucide"
PROVIDERS = STATIC / "icons" / "providers"
PIN = "23f9abc4ed0146cffededd3d7f94c1018bfdf693"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_settings_chrome_is_a_scoped_presentation_layer_after_settings_geometry() -> None:
    overlay = read(STYLE)
    chrome = read(CHROME)

    settings = overlay.index("/ui-settings-page.css?v=2")
    settings_chrome = overlay.index("/ui-settings-chrome.css?v=2")
    help_page = overlay.index("/ui-help-page.css?v=22")
    feature = overlay.index("/ui-feature-icon-contract.css?v=4")
    assert settings < settings_chrome < help_page < feature

    assert "#view-settings" in chrome
    assert "#sidebar" not in chrome
    assert "#main" not in chrome
    assert "#topbar" not in chrome
    assert ".dp-settings-master-card::after" not in chrome
    assert ".dp-settings-card::after" not in chrome
    assert "--dp-panel-frame" not in chrome
    assert "--dp-panel-surface" not in chrome


def test_settings_master_header_uses_supplied_feature_asset_and_true_centered_tabs() -> None:
    chrome = read(CHROME)
    feature = read(FEATURE)
    manifest = json.loads(read(MANIFEST))

    assert "grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);" in chrome
    assert ".dp-settings-header-copy" in chrome
    assert "grid-column: 1;" in chrome
    assert ".dp-settings-tabs" in chrome
    assert "grid-column: 2;" in chrome
    assert "justify-self: center;" in chrome
    assert "@media (max-width: 1180px)" in chrome

    # The explicit query generation prevents a browser-cached synthetic glyph
    # from masking the supplied Settings asset after a staging image update.
    assert "url('/icons/dp/settings-header.svg?v=2')" in chrome
    assert "background-size: 51px 51px;" in chrome
    assert manifest["icons"]["settingsHeader"] == "settings-header.svg"
    gear = read(STATIC / "icons" / "dp" / "settings-header.svg")
    root = ET.fromstring(gear)
    assert root.tag.endswith("svg")
    assert root.attrib.get("viewBox") == "0 0 2048 2048"
    assert gear.count("<path") > 20
    assert "#b053ec" in gear
    assert "#fce6fd" in gear
    assert "#080116" in gear
    assert "<image" not in gear.lower()
    assert "data:image" not in gear.lower()

    assert "#view-settings .dp-settings-header-icon" in feature
    assert "--dp-feature-icon-size: 51px" in feature
    assert "--dp-feature-icon-glow: #a95bff;" in feature
    assert "var(--dp-feature-icon-glow) 62%" in feature
    assert "var(--dp-feature-icon-glow) 70%" in feature

    assert "content: 'To Be Determined';" in chrome


def test_settings_tabs_use_reviewed_lucide_glyphs_with_theme_specific_glyph_glow() -> None:
    chrome = read(CHROME)
    runtime = read(RUNTIME)
    expected = {
        "sources": ("zap.svg", "#b866f5"),
        "downloads": ("download.svg", "#4c8fff"),
        "extraction": ("package-open.svg", "#e0a02b"),
        "notifications": ("bell.svg", "#39c6e8"),
        "authentication": ("shield-check.svg", "#48c77e"),
        "maintenance": ("database-backup.svg", "#3ab8a8"),
        "advanced": ("sliders-horizontal.svg", "#b866f5"),
    }

    assert 'class="dp-settings-tab-chip"' in runtime
    assert 'class="dp-settings-tab-glyph"' in runtime
    assert 'src="/icons/lucide/${html(icon)}.svg"' in runtime
    assert ".dp-settings-tab-chip" in chrome
    assert "width: 22px;" in chrome
    assert "height: 22px;" in chrome
    assert ".dp-settings-tab-glyph" in chrome
    assert "width: 13px;" in chrome
    assert "height: 13px;" in chrome
    assert "gap: 7px;" in chrome

    dark_glyph = chrome.split(".dp-settings-tab-glyph {", 1)[1].split("}", 1)[0]
    light_glyph = chrome.split("body.light.dp-v11-structural #view-settings .dp-settings-tab-glyph {", 1)[1].split("}", 1)[0]
    chip = chrome.split(".dp-settings-tab-chip {", 1)[1].split("}", 1)[0]
    assert "saturate(1.5)" in dark_glyph
    assert "drop-shadow" in dark_glyph
    assert "saturate(2.15)" in light_glyph
    assert light_glyph.count("drop-shadow") == 3
    assert "filter:" not in chip

    for tab, (filename, color) in expected.items():
        assert f".stab[data-tab='{tab}']" in chrome
        assert color in chrome
        assert f"'{Path(filename).stem}'" in runtime

        raw = read(LUCIDE / filename)
        icon_root = ET.fromstring(raw)
        assert icon_root.tag.endswith("svg")
        assert icon_root.attrib.get("viewBox") == "0 0 24 24"
        assert PIN in raw
        assert "<image" not in raw.lower()


def test_sources_panel_has_debrid_services_master_group_with_provider_and_recovery_nested() -> None:
    runtime = read(RUNTIME)
    sources = runtime[runtime.index("function sourcesPanel"):runtime.index("function downloadsPanel")]

    assert "function groupCard(" in runtime
    assert "groupCard('Debrid Services', provider + recovery" in sources
    assert "dp-settings-source-group dp-settings-debrid-services" in sources
    assert "dp-settings-provider-card dp-settings-provider-card--alldebrid" in sources
    assert "dp-settings-provider-recovery-card" in sources
    assert "Debrid Services" in sources
    # The source-type group intentionally has no invented icon yet.
    assert "dp-settings-debrid-services-icon" not in sources


def test_alldebrid_card_uses_supplied_provider_art_on_brand_gold_chip_and_larger_logo() -> None:
    chrome = read(CHROME)
    runtime = read(RUNTIME)
    raw = read(PROVIDERS / "alldebrid.svg")
    root = ET.fromstring(raw)

    assert "dp-settings-provider-chip--alldebrid" in runtime
    assert "dp-settings-provider-logo--alldebrid" in runtime
    assert 'src="/icons/providers/alldebrid.svg"' in runtime
    assert "#dc9e0e" in chrome
    assert "#5f4306" in chrome
    assert "#f2c14b" in chrome

    logo = chrome.split(".dp-settings-provider-logo--alldebrid {", 1)[1].split("}", 1)[0]
    assert "width: 34px;" in logo
    assert "height: 34px;" in logo
    assert "transform: translateY(1px);" in logo
    assert "drop-shadow(0 0 3px rgba(220,158,14,.92))" in logo
    assert "drop-shadow(0 0 7px rgba(220,158,14,.52))" in logo

    light_logo = chrome.split("body.light.dp-v11-structural #view-settings .dp-settings-provider-logo--alldebrid {", 1)[1].split("}", 1)[0]
    assert "drop-shadow(0 0 8px rgba(220,158,14,.62))" in light_logo

    assert root.tag.endswith("svg")
    assert root.attrib.get("viewBox") == "0 0 2048 2048"
    assert "<image" not in raw.lower()
    assert "data:image" not in raw.lower()
    assert "rgb(25,25,25)" in raw
    assert "rgb(250,250,249)" in raw


def test_alldebrid_test_action_uses_flaskconical_glyph_glow_and_apply_label() -> None:
    chrome = read(CHROME)
    runtime = read(RUNTIME)
    raw = read(LUCIDE / "flask-conical.svg")

    assert 'data-action="test-alldebrid"' in runtime
    assert 'class="dp-settings-action-chip"' in runtime
    assert 'class="dp-settings-action-glyph" src="/icons/lucide/flask-conical.svg"' in runtime
    action_owner = chrome.split("button[data-action='test-alldebrid'] {", 1)[1].split("}", 1)[0]
    action_glyph = chrome.split(".dp-settings-action-glyph {", 1)[1].split("}", 1)[0]
    light_action = chrome.split("body.light.dp-v11-structural #view-settings .dp-settings-action-glyph {", 1)[1].split("}", 1)[0]
    action_chip = chrome.split(".dp-settings-action-chip {", 1)[1].split("}", 1)[0]
    assert "--dp-settings-action-color: #b866f5;" in action_owner
    assert "saturate(1.5)" in action_glyph
    assert "drop-shadow" in action_glyph
    assert "saturate(2.15)" in light_action
    assert light_action.count("drop-shadow") == 3
    assert "filter:" not in action_chip
    assert ">Apply Settings</button>" in runtime
    assert ">Save Settings</button>" not in runtime
    assert PIN in raw
    assert "<path" in raw
    assert ".btn-primary" not in chrome
