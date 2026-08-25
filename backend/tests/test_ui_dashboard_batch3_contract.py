"""Contracts for the third v1.0.11 live Dashboard visual-review batch."""

from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
STYLE = STATIC / "style-v11.css"
BATCH = STATIC / "ui-dashboard-batch3.css"
WAVE = STATIC / "icons" / "dp" / "sidebar-wave.svg"
MANIFEST = STATIC / "icons" / "dp" / "manifest.json"


def test_batch3_follows_batch2_visual_override_layer() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    assert "/ui-dashboard-batch3.css?v=17" in overlay
    assert overlay.rfind("/ui-dashboard-batch3.css?v=17") > overlay.rfind(
        "/ui-dashboard-batch2-final.css?v=16"
    )


def test_batch3_captures_reviewed_dashboard_and_sidebar_contracts() -> None:
    css = BATCH.read_text(encoding="utf-8")
    required = (
        "#f7f7fb",
        "sidebar-wave.svg?v=17",
        ".nav-item.active::before",
        "width: 6px !important",
        ".nav-item.active::after",
        "Activity Log",
        "width: calc(100% - 24px) !important",
        "transparent 100%",
        "border-bottom: 0",
        "transparent 80%",
        ".dp-dashboard-quick-add .card-header",
        ".dp-dashboard-activity .card-header",
        "#dash-tbody tr:not([data-torrent-id])",
        "Provider Status",
        "AllDebrid: Connected",
        "crown.svg?v=11",
        ":has(#dot-aria2.warn)",
        ":has(#dot-db.error)",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"batch 3 contract is missing: {missing}"


def test_sidebar_wave_is_true_vector_and_registered() -> None:
    text = WAVE.read_text(encoding="utf-8")
    root = ET.fromstring(text)
    assert root.tag.endswith("svg")
    assert "<image" not in text.lower()
    assert "data:image" not in text.lower()
    assert "sidebarWave" in MANIFEST.read_text(encoding="utf-8")
