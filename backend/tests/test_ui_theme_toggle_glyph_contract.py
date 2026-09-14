"""DP 1.0.12 UI Finishing (Correction 8): the theme toggle glyph must show
the TARGET theme (what clicking switches to), not the current theme.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_static_default_dark_markup_shows_sun_not_moon() -> None:
    html = read("index.html")
    start = html.index('id="theme-toggle"')
    button = html[start:html.index("</button>", start)]
    assert 'data-dp-lucide="sun"' in button
    assert 'data-dp-lucide="moon"' not in button
    assert 'title="Switch to light mode"' in button
    assert 'aria-label="Switch to light mode"' in button


def test_runtime_glyph_owner_renders_target_theme_not_current_theme() -> None:
    js = read("operator-title.js")
    assert "lucideSvg(isLight ? 'moon' : 'sun')" in js
    assert "lucideSvg(isLight ? 'sun' : 'moon')" not in js


def test_app_js_does_not_duplicate_icon_state_logic() -> None:
    js = read("app.js")
    assert "function renderThemeGlyph" not in js
    assert "isLight ? 'sun' : 'moon'" not in js
    assert "isLight ? 'moon' : 'sun'" not in js
    assert "window.DPIcons.renderThemeGlyph(!!isLight)" in js
