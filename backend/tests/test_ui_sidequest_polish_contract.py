"""Reviewed shell sidequest polish contract."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
OVERLAY = STATIC / "style-v11.css"
SIDEQUEST = STATIC / "ui-sidequest-polish.css"
WAVE = STATIC / "icons" / "dp" / "sidebar-wave-accent.svg"
VERSION = ROOT / "VERSION"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_sidequest_layer_loads_after_live_review_accents() -> None:
    overlay = read(OVERLAY)
    live = "@import url('/ui-live-review-batch.css?v=21');"
    sidequest = "@import url('/ui-sidequest-polish.css?v=20');"

    assert live in overlay
    assert sidequest in overlay
    assert overlay.index(live) < overlay.index(sidequest)


def test_global_version_datum_is_text_only_without_chip_surface() -> None:
    css = read(SIDEQUEST)
    selector = "body.dp-v11-structural > #sidebar-version.dp-app-version"
    assert selector in css

    segment = css[css.index(selector):].split('}', 1)[0]
    for declaration in (
        "padding: 0 !important",
        "border: 0 !important",
        "border-radius: 0 !important",
        "outline: 0 !important",
        "background: transparent !important",
        "box-shadow: none !important",
    ):
        assert declaration in segment

    assert "body.dp-v11-structural:not(.light) > #sidebar-version.dp-app-version" in css
    assert "body.light.dp-v11-structural > #sidebar-version.dp-app-version" in css


def test_sidebar_signal_field_rises_left_to_right_in_both_themes() -> None:
    css = read(SIDEQUEST)
    wave = read(WAVE)

    assert "url('/icons/dp/sidebar-wave-accent.svg?v=2')" in css
    assert "height: 300px !important" in css
    assert "opacity: .72 !important" in css
    assert "body.light.dp-v11-structural #sidebar::before" in css
    assert "opacity: .34 !important" in css
    assert "mask-image:" in css

    # The reviewed accent keeps independently shaped crossing paths and particles,
    # but its composition must now climb from the lower left toward the upper right.
    assert wave.count("<path ") >= 7
    assert wave.count("<circle ") >= 28
    assert "linearGradient id=\"purple\"" in wave
    assert "linearGradient id=\"blue\"" in wave
    assert "filter id=\"nodeGlow\"" in wave
    assert "M-20 236 C35 168 74 264 128 197 S224 130 340 72" in wave
    assert "M-20 222 C33 167 73 223 120 178" in wave
    assert "C286 72 315 88 340 56" in wave


def test_sidequest_keeps_backend_version_frozen() -> None:
    assert read(VERSION).strip() == "1.0.10"
