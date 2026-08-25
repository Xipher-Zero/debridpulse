"""Contracts for the v1.0.11 Downloads structural card migration."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
STYLE = STATIC / "style-v11.css"
DOWNLOADS = STATIC / "ui-downloads-structural.css"


def test_downloads_structural_layer_is_loaded_after_dashboard_review_layers() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    assert "/ui-downloads-structural.css?v=18" in overlay
    assert overlay.rfind("/ui-downloads-structural.css?v=18") > overlay.rfind(
        "/ui-dashboard-progress-weight.css?v=18"
    )
    assert "?v=19" not in overlay


def test_downloads_uses_dashboard_card_frame_and_material_contract() -> None:
    css = DOWNLOADS.read_text(encoding="utf-8")
    required = (
        "#view-torrents > .card",
        "border: 1px solid transparent !important",
        "-6px 8px 14px -8px",
        "#view-torrents > .card::after",
        "border-bottom: 0",
        "transparent 98%",
        "#view-torrents > .card > .card-header",
        "min-height: 58px",
        "padding: 0 17px !important",
        "rgba(142,92,225,.14)",
        "div:has(#torrent-search)",
        "padding: 13px 16px 14px !important",
        "#view-torrents .t-table thead th",
        "#f2eff8",
        "#ebe8f3",
        "#torrent-pagination",
        "padding: 12px 16px 13px !important",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"Downloads structural contract is missing: {missing}"


def test_downloads_migration_is_presentation_only() -> None:
    css = DOWNLOADS.read_text(encoding="utf-8")
    forbidden = (
        "display: none",
        "pointer-events: none !important",
        "content: 'All Downloads'",
        "data-status=",
    )
    # The frame pseudo-element is intentionally click-through, but the page's
    # actual controls and content must not be hidden or behaviourally rewritten.
    body_without_frame_pointer_rule = css.replace("pointer-events: none;", "")
    missing = [fragment for fragment in forbidden if fragment in body_without_frame_pointer_rule]
    assert not missing, f"Downloads migration contains behavioural overrides: {missing}"
