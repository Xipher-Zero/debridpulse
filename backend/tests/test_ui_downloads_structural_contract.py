"""Contracts for the v1.0.11 Downloads page after universal-language consolidation."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
STYLE = STATIC / "style-v11.css"
UNIVERSAL = STATIC / "ui-universal-language.css"
DOWNLOADS = STATIC / "ui-downloads-page.css"


def test_universal_language_loads_before_reference_and_page_layers() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    universal = "/ui-universal-language.css?v=20"
    dashboard = "/ui-dashboard.css?v=20"
    statistics = "/ui-statistics-page.css?v=21"
    downloads = "/ui-downloads-page.css?v=27"
    for layer in (universal, dashboard, statistics, downloads):
        assert layer in overlay
    assert overlay.index(universal) < overlay.index(dashboard) < overlay.index(statistics) < overlay.index(downloads)


def test_downloads_uses_shared_card_field_table_and_tab_material() -> None:
    universal = UNIVERSAL.read_text(encoding="utf-8")
    required = (
        ".dp-card, .card, .scard, .list-card",
        "--dp-panel-surface",
        ".dp-field, .input",
        "--dp-field-surface",
        ".dp-tabs, .filter-tabs, .stabs",
        "--dp-segment-active-surface",
        ".dp-table, .t-table",
        "--dp-table-head-surface",
        ".dp-btn, .btn",
        "--dp-primary-surface",
    )
    missing = [fragment for fragment in required if fragment not in universal]
    assert not missing, f"Universal component language is missing: {missing}"
    assert "#view-torrents" not in universal
    assert "#view-dashboard" not in universal


def test_downloads_page_layer_owns_geometry_not_copied_material() -> None:
    css = DOWNLOADS.read_text(encoding="utf-8")
    required = (
        "#view-torrents.active",
        ".dp-downloads-table-wrap",
        "position: sticky",
        "min-height: 70px",
        "width: 51px",
        "div:has(#torrent-search)",
        "grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr)",
        "height: 100% !important",
        "margin-bottom: 0 !important",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"Downloads page geometry contract is missing: {missing}"
    assert "calc(100vh - var(--dp-shell-header)" not in css

    copied_material = (
        "#7440bb",
        "#6336a9",
        "#553291",
        "#f2eff8",
        "#ebe8f3",
        "rgba(14, 19, 44, .94)",
        "-6px 8px 14px -8px rgba(0,0,0,.66)",
        "border: 1px solid transparent",
        "#view-torrents > .card::after",
    )
    present = [fragment for fragment in copied_material if fragment in css]
    assert not present, f"Downloads page copied universal material: {present}"


def test_downloads_migration_is_presentation_only() -> None:
    css = DOWNLOADS.read_text(encoding="utf-8")
    forbidden = (
        "content: 'All Downloads'",
        "data-status=",
        "onclick=",
        "fetch(",
        "api(",
    )
    present = [fragment for fragment in forbidden if fragment in css]
    assert not present, f"Downloads page CSS contains behavioral overrides: {present}"
