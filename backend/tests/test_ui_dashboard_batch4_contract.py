"""Contracts for the fourth v1.0.11 live Dashboard visual-review batch."""

from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
STYLE = STATIC / "style-v11.css"
BATCH = STATIC / "ui-dashboard-batch4.css"
WAVE = STATIC / "icons" / "dp" / "sidebar-wave.svg"


def test_batch4_is_last_visual_override_layer() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    assert "/ui-dashboard-batch4.css?v=18" in overlay
    assert overlay.rfind("/ui-dashboard-batch4.css?v=18") > overlay.rfind(
        "/ui-dashboard-batch3.css?v=17"
    )


def test_batch4_captures_reviewed_dashboard_refinements() -> None:
    css = BATCH.read_text(encoding="utf-8")
    required = (
        "width: calc(100% - 13px) !important",
        "border-top-left-radius: 12px !important",
        "#000 74%",
        "ellipse 105% 88% at 1% -8%",
        "transparent 98%",
        "stroke-width: 2.25 !important",
        "right: -17px !important",
        "sidebar-wave.svg?v=18",
        "height: 470px !important",
        "width: 36px !important",
        "width: 145px !important",
        "#81788f",
        "#f2eff8",
        "card-download.svg?v=11",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"batch 4 contract is missing: {missing}"


def test_batch4_sidebar_wave_remains_true_vector_and_taller() -> None:
    text = WAVE.read_text(encoding="utf-8")
    root = ET.fromstring(text)
    assert root.tag.endswith("svg")
    assert root.attrib.get("viewBox") == "0 0 240 420"
    assert "<image" not in text.lower()
    assert "data:image" not in text.lower()
