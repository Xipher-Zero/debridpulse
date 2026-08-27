"""Contracts for first-paint topbar hydration and theme action semantics."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_theme_bootstrap_loads_visual_behavior_corrections() -> None:
    bootstrap = read("ui-theme-bootstrap.js")
    assert "/ui-visual-behavior-fixes.js?v=21" in bootstrap
    assert "data-dp-visual-behavior-fixes" in bootstrap


def test_aria2_topbar_exists_before_runtime_hydration() -> None:
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


def test_theme_icon_represents_destination_not_current_theme() -> None:
    runtime = read("ui-visual-behavior-fixes.js")
    assert "const icon = isLight ? '☾' : '☀︎'" in runtime
    assert "isLight ? 'Switch to dark mode' : 'Switch to light mode'" in runtime
    assert "attributeFilter: ['class']" in runtime
    assert "observer.observe(button" in runtime
