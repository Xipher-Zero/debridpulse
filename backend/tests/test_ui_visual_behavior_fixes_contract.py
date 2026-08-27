"""Contracts for first-paint topbar hydration and theme action semantics."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_theme_bootstrap_loads_visual_behavior_corrections() -> None:
    bootstrap = read("ui-theme-bootstrap.js")
    assert "/ui-visual-behavior-fixes.js?v=22" in bootstrap
    assert "data-dp-visual-behavior-fixes" in bootstrap


def test_aria2_topbar_is_css_visible_at_first_desktop_paint() -> None:
    entry = read("style-v11.css")
    css = read("ui-topbar-first-paint.css")
    assert "/ui-topbar-first-paint.css?v=20" in entry
    assert "@media (min-width: 900px)" in css
    assert "body.dp-v11-structural:not(.dp-aria2-hydrated) #aria2-speed-badge" in css
    assert "display: flex !important" in css
    assert "body.dp-v11-structural:not(.dp-aria2-hydrated) #aria2-badge-max::after" in css
    assert "content: '0'" in css


def test_aria2_placeholder_state_is_neutral_before_runtime_hydration() -> None:
    runtime = read("ui-visual-behavior-fixes.js")
    assert "window._aria2BadgeState.maxDl = '0'" in runtime
    assert "active.textContent = '0'" in runtime
    assert "max.textContent = '0'" in runtime
    assert "speed.textContent = '0 KB/s'" in runtime
    assert "limit.textContent = 'Unlimited'" in runtime
    assert "badge.style.display = 'flex'" in runtime


def test_aria2_runtime_hydrates_as_soon_as_settings_are_available() -> None:
    runtime = read("ui-visual-behavior-fixes.js")
    assert "Object.keys(settingsData).length > 0" in runtime
    assert "setTimeout(attempt, 100)" in runtime
    assert "window.loadAria2Runtime" in runtime
    assert "document.body.classList.add('dp-aria2-hydrated')" in runtime


def test_theme_icon_represents_destination_with_visible_lucide_geometry() -> None:
    runtime = read("ui-visual-behavior-fixes.js")
    assert "const iconName = isLight ? 'moon' : 'sun'" in runtime
    assert "isLight ? 'Switch to dark mode' : 'Switch to light mode'" in runtime
    assert "data-dp-lucide" in runtime
    assert "THEME_GLYPHS" in runtime
    assert "button.innerHTML = themeSvg(iconName)" in runtime
    assert "attributeFilter: ['class']" in runtime
    assert "observer.observe(button" in runtime
