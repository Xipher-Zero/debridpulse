"""Contracts for the first v1.0.11 live Dashboard review batch."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
ENTRY = STATIC / "style-v11.css"
BATCH = STATIC / "ui-dashboard-batch1.css"
RUNTIME = STATIC / "ui-runtime.js"
SHELL_RUNTIME = STATIC / "operator-title.js"


def test_review_batch_is_loaded_after_regression_layer() -> None:
    entry = ENTRY.read_text(encoding="utf-8")
    regression = entry.index("/ui-regression-fixes.css?v=20")
    batch = entry.index("/ui-dashboard-batch1.css?v=20")
    assert batch > regression


def test_speed_cap_hover_keeps_surface_and_reuses_green_arrow() -> None:
    css = BATCH.read_text(encoding="utf-8")
    runtime = RUNTIME.read_text(encoding="utf-8")
    shell = SHELL_RUNTIME.read_text(encoding="utf-8")

    assert ".aria2-cap-options button:hover" in css
    assert "background: var(--surface2) !important" in css
    assert "dp-speedcap-arrow" in css
    assert "color: var(--green)" in css
    assert "arrow.innerHTML = utilitySvg('chevronDown')" in runtime
    assert "chevronDown:" in shell
    assert "arrow.textContent = '▼'" not in runtime


def test_title_spacing_and_selected_navigation_match_review() -> None:
    css = BATCH.read_text(encoding="utf-8")

    assert "#page-title::after" in css
    assert "margin-left: 8px !important" in css
    assert ".nav-item.active::after" in css
    assert "right: -1px" in css
    assert "border-left-color: transparent !important" in css
    assert ".nav-item.active::before" in css
    assert "background: transparent !important" in css


def test_metric_cards_use_semantic_top_edge_and_internal_wash() -> None:
    css = BATCH.read_text(encoding="utf-8")

    assert "border-top: 3px solid var(--c) !important" in css
    assert "color-mix(in srgb, var(--c) 12%, transparent)" in css
    assert "color-mix(in srgb, var(--c) 9%, white)" in css


def test_sparklines_are_live_metric_samples_not_decorative_variants() -> None:
    runtime = RUNTIME.read_text(encoding="utf-8")
    css = BATCH.read_text(encoding="utf-8")

    assert "METRIC_HISTORY_KEY" in runtime
    assert "dashboardMetricSnapshot" in runtime
    assert "recordDashboardMetricHistory" in runtime
    assert "installMetricHistoryHook" in runtime
    assert "recent live samples of this exact card metric" in runtime
    assert "const variants = [" not in runtime
    assert "dp-card-spark-fill" in runtime
    assert "linearGradient" in runtime
    assert ".dp-card-spark-fill" in css


def test_section_icons_and_header_actions_have_reviewed_legibility() -> None:
    css = BATCH.read_text(encoding="utf-8")

    assert ".dp-dashboard-quick-add .card-title > .dp-icon" in css
    assert ".dp-dashboard-activity .card-title > .dp-icon" in css
    assert "width: 51px !important" in css
    assert "#btn-import-existing" in css
    assert "#btn-recover-all" in css
    assert "font-weight: 700 !important" in css
