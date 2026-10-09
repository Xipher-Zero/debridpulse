"""Final-state contracts for cross-page presentation finalization."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")














def test_settings_icon_replacement_does_not_recreate_legacy_icons() -> None:
    page = read("ui-settings-page.js")
    css = read("ui-settings-card-icons.css")
    # Each subsection header emits exactly one glyph, inside its family chip
    # (familyIcon). The bare inner-card icon path is retired, and no legacy
    # icon is created, hidden, or replaced afterwards.
    assert "CARD_ICONS" not in page
    assert "dp-settings-inner-card-icon" not in page and "dp-settings-inner-card-icon" not in css
    for retired in ("dpSettingsReplacedIcon", "dp-settings-replaced-legacy-icon", "child.remove()"):
        assert retired not in page and retired not in css
    assert not (STATIC / "ui-settings-card-icons.js").exists()
