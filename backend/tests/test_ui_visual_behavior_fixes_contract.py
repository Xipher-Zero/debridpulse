"""Contracts for first-paint topbar hydration and theme action semantics."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")




def test_aria2_topbar_is_css_visible_at_first_desktop_paint() -> None:
    entry = read("style-v11.css")
    css = read("ui-topbar-first-paint.css")
    assert "/ui-topbar-first-paint.css?v=20" in entry
    assert "@media (min-width: 900px)" in css
    assert "body.dp-v11-structural:not(.dp-aria2-hydrated) #aria2-speed-badge" in css
    assert "display: flex !important" in css
    assert "body.dp-v11-structural:not(.dp-aria2-hydrated) #aria2-badge-max::after" in css
    assert "content: '0'" in css
