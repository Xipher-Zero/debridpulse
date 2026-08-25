"""Contracts for the final v1.0.11 progress-line weight refinement."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
STYLE = STATIC / "style-v11.css"
PROGRESS = STATIC / "ui-dashboard-progress-weight.css"


def test_progress_weight_layer_finishes_dashboard_calibration_before_pages() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    control = "/ui-dashboard-control-polish.css?v=18"
    progress = "/ui-dashboard-progress-weight.css?v=18"
    downloads = "/ui-downloads-page.css?v=19"
    for layer in (control, progress, downloads):
        assert layer in overlay
    assert overlay.index(control) < overlay.index(progress) < overlay.index(downloads)


def test_progress_weight_refinement_changes_only_physical_height() -> None:
    css = PROGRESS.read_text(encoding="utf-8")
    required = (
        "#dash-tbody .prog",
        "#t-tbody .prog",
        "height: 3.5px !important",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"progress-weight contract is missing: {missing}"
    assert "box-shadow" not in css
    assert "filter" not in css
    assert ".prog-fill" not in css
