"""Final-state contracts for cross-page presentation finalization."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")














def test_settings_icon_replacement_does_not_recreate_legacy_icons() -> None:
    source = read("ui-settings-card-icons.js")
    css = read("ui-settings-card-icons.css")
    assert "child.dataset.dpSettingsReplacedIcon = '1'" in source
    assert "child.classList.add('dp-settings-replaced-legacy-icon')" in source
    assert "child.remove()" not in source
    assert ".dp-settings-replaced-legacy-icon" in css
    assert "display: none !important" in css
