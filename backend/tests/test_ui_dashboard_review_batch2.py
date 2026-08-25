"""Contracts for the second v1.0.11 Dashboard live-review batch."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
STYLE = (STATIC / "style-v11.css").read_text(encoding="utf-8")
BATCH1 = (STATIC / "ui-dashboard-batch1.css").read_text(encoding="utf-8")
BATCH2 = (STATIC / "ui-dashboard-batch2.css").read_text(encoding="utf-8")
LINK_ICON = (STATIC / "icons" / "dp" / "card-link.svg").read_text(encoding="utf-8")
DOWNLOAD_ICON = (STATIC / "icons" / "dp" / "card-download.svg").read_text(encoding="utf-8")


def test_second_review_batch_is_loaded_after_first_review_batch() -> None:
    first = STYLE.index("/ui-dashboard-batch1.css?v=20")
    second = STYLE.index("/ui-dashboard-batch2.css?v=20")
    assert first < second


def test_pause_and_resume_all_are_semantic_gradients() -> None:
    assert "#btn-pause-all" in BATCH2
    assert "#btn-resume-all" in BATCH2
    assert "#btn-resume-paused" in BATCH2
    assert "linear-gradient(135deg" in BATCH2
    assert "#ffb3bf" in BATCH2
    assert "#9af0bf" in BATCH2
    assert "#ffe8eb" in BATCH2
    assert "#e4faec" in BATCH2


def test_selected_navigation_restores_left_rail_and_right_spark() -> None:
    assert ".nav-item.active::before" in BATCH2
    assert "#d19aff" in BATCH2
    assert ".nav-item.active::after" in BATCH2
    assert "radial-gradient(circle at 50% 50%" in BATCH2
    assert "#ffffff 0 1px" in BATCH2
    assert "rgba(126, 48, 239, .58)" in BATCH2


def test_sidebar_and_dashboard_panels_have_directional_depth() -> None:
    assert "13px 0 31px -18px" in BATCH2
    assert "14px 0 32px -18px" in BATCH2
    assert "-7px 9px 24px -14px" in BATCH2
    assert "-8px 10px 25px -14px" in BATCH2


def test_metric_top_edge_stops_before_right_corner_and_sparkline_reaches_edges() -> None:
    assert ".dash-hero-stat::before" in BATCH2
    assert "width: calc(100% - 19px)" in BATCH2
    assert "border-top-left-radius: 12px" in BATCH2
    assert "transparent 100%" in BATCH2
    assert "left: -1px !important" in BATCH2
    assert "right: -1px !important" in BATCH2
    assert "width: calc(100% + 2px) !important" in BATCH2
    assert "opacity: .88 !important" in BATCH2
    assert "opacity: .72 !important" in BATCH2


def test_quick_add_tile_matches_metric_tile_frame() -> None:
    metric_tile = '<rect x="9" y="15" width="94" height="91" rx="20"'
    assert metric_tile in DOWNLOAD_ICON
    assert metric_tile in LINK_ICON
    assert "width: 51px !important" in BATCH2
    assert "height: 51px !important" in BATCH2


def test_dashboard_header_actions_are_tinted_and_recover_stays_yellow() -> None:
    assert "#btn-import-existing" in BATCH2
    assert ".dp-dashboard-activity .card-header .btn" in BATCH2
    assert "#525b7c" in BATCH2
    assert "#687294" in BATCH2
    assert "#btn-recover-all" in BATCH2
    assert BATCH2.count("color: #ffd24f !important") >= 2


def test_light_page_title_and_flair_gain_depth_without_subtitle_shadow() -> None:
    assert "body.light.dp-v11-structural #page-title" in BATCH2
    assert "text-shadow:" in BATCH2
    assert "body.light.dp-v11-structural #page-title::after" in BATCH2
    assert "drop-shadow" in BATCH2
    assert ".dp-page-subtitle" not in BATCH2


def test_batch2_preserves_first_batch_speedcap_contract() -> None:
    assert ".aria2-cap-options button:hover" in BATCH1
    assert "background: var(--surface2) !important" in BATCH1
    assert "#aria2-cap-toggle .dp-speedcap-arrow" in BATCH1
