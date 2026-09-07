"""Contracts for canonical topbar first-paint ownership."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_aria2_topbar_first_paint_is_owned_by_shell_markup() -> None:
    entry = read("style-v11.css")
    index = read("index.html")

    assert "/ui-topbar-first-paint.css" not in entry
    assert not (STATIC / "ui-topbar-first-paint.css").exists()
    assert 'id="aria2-speed-badge"' in index
    assert 'id="aria2-badge-active">0</span>' in index
    assert 'id="aria2-badge-max">0</span>' in index
