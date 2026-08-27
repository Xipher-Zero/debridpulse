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


def test_sidebar_signal_field_uses_diverse_authored_wave_asset_in_both_themes() -> None:
    css = read(SIDEQUEST)
    wave = read(WAVE)

    assert "url('/icons/dp/sidebar-wave-accent.svg?v=1')" in css
    assert "height: 300px !important" in css
    assert "opacity: .72 !important" in css
    assert "body.light.dp-v11-structural #sidebar::before" in css
    assert "opacity: .34 !important" in css
    assert "mask-image:" in css

    # The reviewed accent is intentionally not a repeated sine-wave approximation:
    # multiple independently shaped paths cross through a varied particle field.
    assert wave.count("<path ") >= 7
    assert wave.count("<circle ") >= 28
    assert "linearGradient id=\"purple\"" in wave
    assert "linearGradient id=\"blue\"" in wave
    assert "filter id=\"nodeGlow\"" in wave
    assert "C38 83 82 247 145 151" in wave
    assert "C26 143 58 236 108 173" in wave


def test_sidequest_keeps_backend_version_frozen() -> None:
    assert read(VERSION).strip() == "1.0.10"
