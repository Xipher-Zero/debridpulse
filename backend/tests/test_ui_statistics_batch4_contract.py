"""Reviewed Statistics Batch 4 presentation contract."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
BATCH_RUNTIME = STATIC / "ui-statistics-batch4.js"
BATCH_CSS = STATIC / "ui-statistics-batch4.css"
FEATURE_ICONS = STATIC / "ui-feature-icon-contract.css"
THEME_BOOTSTRAP = STATIC / "ui-theme-bootstrap.js"
STYLE_V11 = STATIC / "style-v11.css"
VERSION = ROOT / "VERSION"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_batch4_runtime_loads_after_batch3_and_preserves_statistics_api_ownership() -> None:
    bootstrap = read(THEME_BOOTSTRAP)
    runtime = read(BATCH_RUNTIME)

    assert "/ui-statistics-batch3.js?v=1" in bootstrap
    assert "/ui-statistics-batch4.js?v=1" in bootstrap
    assert bootstrap.index("/ui-statistics-batch3.js?v=1") < bootstrap.index("/ui-statistics-batch4.js?v=1")
    assert "data-dp-statistics-batch4" in bootstrap
    assert "previous.dpStatisticsBatch3 !== '1'" in runtime
    assert "previous.dpStatisticsBatch4 === '1'" in runtime
    assert "window.loadDetailedStats = wrapped" in runtime
    assert "api('GET', '/stats/detail" not in runtime
    assert 'api("GET", "/stats/detail' not in runtime


def test_secondary_kpi_band_returns_to_centered_pre_removal_width() -> None:
    css = read(BATCH_CSS)

    assert "width: 83.333% !important" in css
    assert "margin-inline: auto !important" in css
    assert "grid-template-columns: repeat(5, minmax(0, 1fr)) !important" in css
    assert "@media (max-width: 1439px)" in css
    assert "width: 100% !important" in css


def test_secondary_kpi_icons_are_optically_normalized_without_resizing_chips() -> None:
    css = read(BATCH_CSS)

    assert ".dp-stats-kpi-day .dp-kpi-icon > .dp-icon" in css
    assert ".dp-stats-kpi-week .dp-kpi-icon > .dp-icon" in css
    assert ".dp-stats-kpi-success .dp-kpi-icon > .dp-icon" in css
    assert ".dp-stats-kpi-duration .dp-kpi-icon > .dp-icon" in css
    assert ".dp-stats-kpi-size .dp-kpi-icon > .dp-icon" in css
    assert "width: 32px !important" in css
    assert "width: 42px !important" in css
    assert "width: 46px !important" in css
    assert "width: 50px !important" in css
    assert "--dp-icon-frame-size" not in css


def test_feature_icons_share_dashboard_layout_box_with_asset_specific_optical_padding() -> None:
    css = read(FEATURE_ICONS)
    imports = read(STYLE_V11)

    assert "--dp-feature-icon-size: 51px" in css
    for selector in (
        ".dp-dashboard-quick-add .card-title > .dp-icon",
        ".dp-dashboard-activity .card-title > .dp-icon",
        ".dp-activity-title-icon",
        ".dp-downloads-title-icon",
        ".dp-statistics-title-icon",
    ):
        assert selector in css
    assert "#view-events .dp-activity-title-icon {\n  padding: 3px !important" in css
    assert "#view-torrents .dp-downloads-title-icon {\n  padding: 1px !important" in css
    assert "#view-stats .dp-statistics-title-icon {\n  padding: 5px !important" in css
    assert "/ui-feature-icon-contract.css?v=3" in imports


def test_lower_cards_switch_from_single_to_balanced_two_column_top_ten_layout() -> None:
    runtime = read(BATCH_RUNTIME)
    css = read(BATCH_CSS)

    for detail_id in (
        "detail-torrent-status",
        "detail-file-status",
        "detail-event-levels",
        "detail-sources",
    ):
        assert detail_id in runtime
    assert "const MAX_VISIBLE = 10" in runtime
    assert "const TWO_COLUMN_THRESHOLD = 6" in runtime
    assert "const split = Math.ceil(visible.length / 2)" in runtime
    assert "visible.slice(0, split)" in runtime
    assert "visible.slice(split)" in runtime
    assert "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)" in css
    assert ".dp-stats-adaptive-list--columns::before" in css
    assert "top: 8px" in css
    assert "bottom: 8px" in css


def test_more_than_ten_keeps_top_ten_and_opens_full_list_in_existing_modal() -> None:
    runtime = read(BATCH_RUNTIME)
    css = read(BATCH_CSS)

    assert "entries.slice(0, MAX_VISIBLE)" in runtime
    assert "entries.length > MAX_VISIBLE" in runtime
    assert "'+ ' + more + ' more'" in runtime
    assert "dp-stats-overflow-card" in runtime
    assert "document.getElementById('overlay')" in runtime
    assert "document.getElementById('modal-title')" in runtime
    assert "document.getElementById('modal-body')" in runtime
    assert "entries.forEach(function (entry)" in runtime
    assert "overlay.classList.add('open')" in runtime
    assert "const header = card.querySelector(':scope > .card-header')" in runtime
    assert "(header || card).appendChild(indicator)" in runtime
    assert "clearOverflow(card)" in runtime
    assert ".dp-stats-breakdown-grid > .list-card > .card-header" in css
    assert "position: absolute" in css
    assert "right: 10px" in css
    assert ".dp-stats-overflow-list" in css


def test_batch4_keeps_backend_version_frozen() -> None:
    assert read(VERSION).strip() == "1.0.10"
