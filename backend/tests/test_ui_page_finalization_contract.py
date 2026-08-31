"""Final-state contracts for cross-page presentation finalization."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_page_finalization_loads_after_established_page_components() -> None:
    loader = read("ui-presentation-loader.js")
    assert "/ui-settings-card-icons.css?v=2" in loader
    assert "/ui-settings-card-icons.js?v=2" in loader
    assert "/ui-page-finalization.css?v=1" in loader
    assert "/ui-page-finalization.js?v=1" in loader
    assert loader.index("/ui-settings-card-icons.css?v=2") < loader.index("/ui-page-finalization.css?v=1")
    assert loader.index("/ui-error-semantics.js?v=21") < loader.index("/ui-page-finalization.js?v=1")


def test_page_finalization_keeps_accepted_master_card_copy() -> None:
    source = read("ui-page-finalization.js")
    expected = (
        "Download Queue",
        "downloads tracked. Most of them followed instructions.",
        "Activity Log",
        "Everything DebridPulse thought was worth mentioning.",
        "By the Numbers",
        "Because vibes are not a performance metric.",
        "Tuning Deck",
        "Your rules, your defaults.",
        "Field Manual",
        "When intuition fails.",
    )
    for text in expected:
        assert text in source


def test_settings_and_help_keep_accepted_surface_hierarchy() -> None:
    source = read("ui-page-finalization.js")
    treatment = read("ui-panel-surface-treatment.css")
    assert ".dp-settings-master-card')?.classList.add('dp-list-workspace-surface')" in source
    assert ".dp-help-master-card')?.classList.add('dp-list-workspace-surface')" in source
    assert "view.querySelectorAll('.dp-settings-card, .dp-settings-group-card')" in source
    assert "view.querySelectorAll('.dp-help-section-card')" in source
    assert "card.classList.add('dp-large-panel-surface')" in source
    assert ".dp-large-panel-surface" in treatment
    assert ".dp-list-workspace-surface" in treatment


def test_downloads_bulk_actions_remain_integrated_above_table() -> None:
    source = read("ui-page-finalization.js")
    css = read("ui-page-finalization.css")
    assert "bar.classList.add('dp-downloads-bulk-integrated')" in source
    assert "card.insertBefore(bar, tableWrap)" in source
    assert "border-radius: 0 !important" in css
    assert "box-shadow: none !important" in css
    assert "border-bottom: 1px solid var(--dp-divider) !important" in css


def test_settings_subtitle_and_help_icon_keep_accepted_presentation() -> None:
    css = read("ui-page-finalization.css")
    assert "#view-settings .dp-settings-header-subtitle" in css
    assert "font-size: 11px !important" in css
    assert "#view-settings .dp-settings-header-subtitle::after" in css
    assert "content: none !important" in css
    assert "#view-help .dp-help-title-icon" in css
    assert "--dp-feature-icon-glow: #4c8fff" in css
    assert "drop-shadow(0 0 5px" in css
    assert "drop-shadow(0 0 11px" in css


def test_settings_icon_replacement_does_not_recreate_legacy_icons() -> None:
    source = read("ui-settings-card-icons.js")
    css = read("ui-settings-card-icons.css")
    assert "child.dataset.dpSettingsReplacedIcon = '1'" in source
    assert "child.classList.add('dp-settings-replaced-legacy-icon')" in source
    assert "child.remove()" not in source
    assert ".dp-settings-replaced-legacy-icon" in css
    assert "display: none !important" in css
