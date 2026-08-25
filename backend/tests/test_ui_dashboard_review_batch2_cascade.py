"""Cascade-precedence checks for Dashboard live-review batch 2."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
STYLE = (STATIC / "style-v11.css").read_text(encoding="utf-8")
FINAL = (STATIC / "ui-dashboard-batch2-final.css").read_text(encoding="utf-8")


def test_batch2_final_cascade_loads_after_batch2() -> None:
    batch2 = STYLE.index("/ui-dashboard-batch2.css?v=20")
    final = STYLE.index("/ui-dashboard-batch2-final.css?v=20")
    assert batch2 < final


def test_topbar_semantic_buttons_outrank_inherited_important_rule() -> None:
    assert "#topbar-actions #btn-pause-all.btn" in FINAL
    assert "#topbar-actions #btn-resume-all.btn" in FINAL
    assert "#topbar-actions #btn-resume-paused.btn" in FINAL
    assert "linear-gradient(135deg" in FINAL
    assert "#ffb3bf" in FINAL
    assert "#9af0bf" in FINAL
    assert "#ffe8eb" in FINAL
    assert "#e4faec" in FINAL


def test_tapered_metric_accent_is_only_colored_top_edge() -> None:
    assert "border-top: 1px solid rgba(105, 119, 181, .23) !important" in FINAL
    assert "border-top-color: #dce1ef !important" in FINAL
