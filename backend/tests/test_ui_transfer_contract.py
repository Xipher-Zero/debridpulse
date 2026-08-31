"""Contracts for the final shared transfer-row presentation language."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
STYLE = STATIC / "style-v11.css"
TRANSFER = STATIC / "ui-transfer-contract.css"
DASHBOARD = STATIC / "ui-dashboard-consistency.css"


def test_transfer_contract_is_final_shared_layer_after_page_geometry() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    dashboard_fix = "/ui-dashboard-consistency.css?v=23"
    downloads = "/ui-downloads-page.css?v=27"
    help_page = "/ui-help-page.css?v=22"
    transfer = "/ui-transfer-contract.css?v=31"

    for layer in (dashboard_fix, downloads, help_page, transfer):
        assert layer in overlay
    assert "/ui-dashboard-progress-weight.css" not in overlay
    assert (
        overlay.index(dashboard_fix)
        < overlay.index(downloads)
        < overlay.index(help_page)
        < overlay.index(transfer)
    )


def test_transfer_status_badges_restore_theme_aware_semantic_states() -> None:
    css = TRANSFER.read_text(encoding="utf-8")
    required = (
        ".badge-downloading",
        "--dp-badge-color: var(--dp-state-success)",
        ".badge-uploading",
        ".badge-queued",
        "--dp-badge-color: var(--dp-state-active)",
        ".badge-processing",
        ".badge-extracting",
        "--dp-badge-color: var(--dp-accent-purple-bright)",
        ".badge-paused",
        ".badge-ready",
        ".badge-partial",
        "--dp-badge-color: var(--dp-state-caution)",
        ".badge-completed",
        ".badge-error",
        "--dp-badge-color: var(--dp-state-error)",
        ".badge-deleted",
        "--dp-badge-color: var(--dp-text-muted)",
        "color: var(--dp-badge-color) !important",
        "min-height: 25px !important",
        "border-radius: 6px !important",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"transfer status contract is missing: {missing}"


def test_transfer_actions_share_recent_activity_geometry() -> None:
    css = TRANSFER.read_text(encoding="utf-8")
    required = (
        ":is(#dash-tbody, #t-tbody) .actions .btn",
        "width: 72px !important",
        "min-width: 72px !important",
        "min-height: 36px !important",
        "height: 36px !important",
        "padding: 0 8px !important",
        "border-radius: 8px !important",
        "font-size: 11.5px !important",
        "[onclick*=\"pauseTorrent(\"]",
        "[onclick*=\"resumeTorrent(\"]",
        "[onclick*=\"retryT(\"]",
        "[onclick*=\"retryTorrent(\"]",
        "background: var(--dp-state-active-bg) !important",
        "border-color: color-mix(in srgb, var(--dp-state-active) 34%, transparent) !important",
        "color: var(--dp-state-active) !important",
        "box-shadow: none !important",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"shared transfer action contract is missing: {missing}"


def test_transfer_percentage_uses_original_geometry_with_weight_only() -> None:
    css = TRANSFER.read_text(encoding="utf-8")
    required = (
        'tr[data-status] .prog-pct',
        "display: block !important",
        "margin-top: 3px !important",
        "font-family: var(--dp-font-mono) !important",
        "font-size: 10px !important",
        "font-weight: 700 !important",
        "letter-spacing: normal !important",
        "color: var(--dp-text-secondary) !important",
        "text-shadow: none !important",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"transfer percentage contract is missing: {missing}"
    assert "font-size: 15px" not in css
    assert "#34d382" not in css
    assert "#f2bd3f" not in css


def test_transfer_track_and_fill_share_active_weight() -> None:
    css = TRANSFER.read_text(encoding="utf-8")
    assert ":is(#dash-tbody, #t-tbody) .prog," in css
    assert ":is(#dash-tbody, #t-tbody) .prog-fill" in css
    assert "height: 7px !important" in css
    assert "3.5px" not in css


def test_recent_activity_reclaims_only_added_column_slack_for_actions() -> None:
    css = DASHBOARD.read_text(encoding="utf-8")
    assert "@media (min-width: 1440px)" in css
    assert ".t-table th:nth-child(5)" in css
    assert ".t-table td:nth-child(5)" in css
    assert "width: 8% !important" in css
    assert ".t-table th:nth-child(6)" in css
    assert ".t-table td:nth-child(6)" in css
    assert "width: 9% !important" in css
    for column in range(1, 5):
        assert f".t-table th:nth-child({column})" not in css
        assert f".t-table td:nth-child({column})" not in css
