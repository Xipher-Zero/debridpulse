"""Contracts for the fourth v1.0.11 live Dashboard visual-review layer."""

from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
STYLE = STATIC / "style-v11.css"
BATCH = STATIC / "ui-dashboard-batch4.css"
WAVE = STATIC / "icons" / "dp" / "sidebar-wave.svg"


def test_batch4_follows_canonical_dashboard_structure() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    structural = "/ui-dashboard-structural.css?v=23"
    batch4 = "/ui-dashboard-batch4.css?v=20"
    assert structural in overlay
    assert batch4 in overlay
    assert "/ui-dashboard-batch3.css" not in overlay
    assert overlay.index(structural) < overlay.index(batch4)


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
        "background-color: transparent !important",
        "rgba(255,255,255,.99) 0 1px",
        "sidebar-wave.svg?v=18",
        "bottom: 146px !important",
        "height: 500px !important",
        "grid-template-columns: 8px minmax(0, 1fr) !important",
        "justify-self: center !important",
        "width: 36px !important",
        "font-size: 10.5px !important",
        "rgba(255,199,61,.08)",
        "#81788f",
        "#f2eff8",
        "card-download.svg?v=11",
        "radial-gradient(ellipse 130% 110% at 12% 18%",
        "0 0 10px rgba(124,58,237,.18)",
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
