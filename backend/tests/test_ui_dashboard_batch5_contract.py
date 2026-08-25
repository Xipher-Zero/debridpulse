"""Contracts for the fifth v1.0.11 live Dashboard refinement batch."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "frontend" / "static"
STYLE = STATIC / "style-v11.css"
BATCH = STATIC / "ui-dashboard-batch5.css"


def test_batch5_remains_after_batch4_with_universal_base_preceding_dashboard() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    universal = "/ui-universal-language.css?v=19"
    batch4 = "/ui-dashboard-batch4.css?v=18"
    batch5 = "/ui-dashboard-batch5.css?v=18"
    for layer in (universal, batch4, batch5):
        assert layer in overlay
    assert overlay.index(universal) < overlay.index(batch4) < overlay.index(batch5)


def test_batch5_provider_and_spotlight_contracts() -> None:
    css = BATCH.read_text(encoding="utf-8")
    required = (
        "padding: 2px 0 10px 28px !important",
        "left: 4px !important",
        "max-width: 164px !important",
        "transparent 100%) !important",
        "box-shadow: none !important",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"batch 5 provider/spotlight contract is missing: {missing}"


def test_batch5_transfer_semantics_and_progress_emphasis() -> None:
    css = BATCH.read_text(encoding="utf-8")
    required = (
        'tr[data-status="downloading"] .badge-downloading::before',
        'tr[data-status="downloading"] .badge-partial::before',
        'tr[data-status="paused"] .badge-paused::before',
        'tr[data-status="completed"] .badge-completed::before',
        'tr[data-status="error"] .badge-error::before',
        "content: '↓'",
        "content: 'Ⅱ'",
        "content: '✓'",
        "content: '×'",
        "height: 6px !important",
        "font-size: 15px !important",
        ":has(.badge-partial) .prog-fill",
        ":not(:has(.badge-partial)) .prog-fill",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"batch 5 transfer semantic contract is missing: {missing}"


def test_batch5_action_color_grammar_and_primary_add_depth() -> None:
    css = BATCH.read_text(encoding="utf-8")
    required = (
        "#btn-pause-all",
        'button[onclick*="pauseT("]',
        "#btn-resume-all",
        "#btn-resume-paused",
        'button[onclick*="resumeT("]',
        "#btn-recover-all",
        "#e3c5ff",
        "#btn-add-transfer",
        "#973cf4",
        "inset 0 1px rgba(255,255,255,.24)",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"batch 5 action contract is missing: {missing}"
