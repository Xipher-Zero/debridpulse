"""Reviewed Settings chrome batch: identity, tabs, provider art, and action glyphs."""

from __future__ import annotations

import json
from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
CHROME = STATIC / "ui-settings-chrome.css"
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
    settings_chrome = overlay.index("/ui-settings-chrome.css?v=1")
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


def test_settings_master_header_uses_feature_asset_and_true_centered_tabs() -> None:
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

    assert "url('/icons/dp/settings-header.svg')" in chrome
    assert manifest["icons"]["settingsHeader"] == "settings-header.svg"
    gear = read(STATIC / "icons" / "dp" / "settings-header.svg")
    assert "linearGradient" in gear
    assert "#A95BFF" in gear
    assert "<image" not in gear.lower()

    assert "#view-settings .dp-settings-header-icon" in feature
    assert "--dp-feature-icon-size: 51px" in feature
    assert "--dp-feature-icon-glow: #a95bff;" in feature
    assert "var(--dp-feature-icon-glow) 62%" in feature
    assert "var(--dp-feature-icon-glow) 70%" in feature

    assert "content: 'To Be Determined';" in chrome


def test_settings_tabs_use_reviewed_lucide_glyphs_with_optically_small_chips() -> None:
    chrome = read(CHROME)
    expected = {
        "sources": ("zap.svg", "#b866f5"),
        "downloads": ("download.svg", "#4c8fff"),
        "extraction": ("package-open.svg", "#e0a02b"),
        "notifications": ("bell.svg", "#39c6e8"),
        "authentication": ("shield-check.svg", "#48c77e"),
        "maintenance": ("database-backup.svg", "#3ab8a8"),
        "advanced": ("sliders-horizontal.svg", "#9d7bea"),
    }

    assert "width: 22px;" in chrome
    assert "height: 22px;" in chrome
    assert "background-size: 13px 13px;" in chrome
    assert "gap: 7px;" in chrome

    for tab, (filename, color) in expected.items():
        assert f".stab[data-tab='{tab}']" in chrome
        assert f"url('/icons/lucide/{filename}')" in chrome
        assert color in chrome

        raw = read(LUCIDE / filename)
        root = ET.fromstring(raw)
        assert root.tag.endswith("svg")
        assert root.attrib.get("viewBox") == "0 0 24 24"
        assert PIN in raw
        assert "<image" not in raw.lower()


def test_alldebrid_card_uses_supplied_provider_art_on_neutral_chip() -> None:
    chrome = read(CHROME)
    raw = read(PROVIDERS / "alldebrid.svg")
    root = ET.fromstring(raw)

    assert ":has([data-setting='alldebrid_api_key'])" in chrome
    assert "url('/icons/providers/alldebrid.svg')" in chrome
    assert "width: 38px;" in chrome
    assert "height: 38px;" in chrome
    assert "linear-gradient(145deg, #d8d9df 0%, #9c9faa 52%, #777a86 100%)" in chrome

    assert root.tag.endswith("svg")
    assert root.attrib.get("viewBox") == "0 0 2048 2048"
    assert "<image" not in raw.lower()
    assert "data:image" not in raw.lower()
    assert "rgb(25,25,25)" in raw
    assert "rgb(250,250,249)" in raw


def test_alldebrid_test_action_uses_flaskconical_chip_without_becoming_primary() -> None:
    chrome = read(CHROME)
    raw = read(LUCIDE / "flask-conical.svg")

    assert "button[data-action='test-alldebrid']" in chrome
    assert "url('/icons/lucide/flask-conical.svg')" in chrome
    assert "background-size: 13px 13px;" in chrome
    assert PIN in raw
    assert "<path" in raw
    assert ".btn-primary" not in chrome
