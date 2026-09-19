"""Contracts for the final shared transfer-row presentation language."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
STYLE = STATIC / "style.css"
TRANSFER = STATIC / "ui-transfer-contract.css"
DASHBOARD = STATIC / "ui-dashboard.css"


def test_transfer_contract_is_final_shared_layer_after_page_geometry() -> None:
    overlay = STYLE.read_text(encoding="utf-8")
    dashboard = "/ui-dashboard.css?v=22"
    downloads = "/ui-downloads-page.css?v=30"
    help_page = "/ui-help-page.css?v=23"
    transfer = "/ui-transfer-contract.css?v=33"

    for layer in (dashboard, downloads, help_page, transfer):
        assert layer in overlay
    assert "/ui-dashboard-progress-weight.css" not in overlay
    assert "/ui-dashboard-final.css" not in overlay
    assert (
        overlay.index(dashboard)
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
        ".badge-input_required",
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
    """DP 1.0.12 UI Finishing (Correction 3): the Added/Action column-width
    override that used to be RE-declared a second time after the
    "Consolidated from ui-dashboard-final.css" marker (a later "final
    geometry correction" layered on the same media query/selector/
    specificity as the block above it, differing only by source order) was
    consolidated into the one canonical nth-child(5)/(6) block instead.
    Column 6 (Action) is now pinned to the shared action-track custom
    property rather than an independent percentage, so its center can agree
    with Recover All's and Add's; column 5 (Added) absorbs the remainder via
    calc() so every column's width still sums to exactly 100% at any
    viewport width.
    """
    css = DASHBOARD.read_text(encoding="utf-8")
    marker = "/* Consolidated from ui-dashboard-final.css. */"
    assert marker in css
    final_calibration = css.split(marker, 1)[1]

    for column in (1, 2, 3, 4, 5, 6):
        assert f".t-table th:nth-child({column})" not in final_calibration
        assert f".t-table td:nth-child({column})" not in final_calibration

    canonical_calibration = css.split(marker, 1)[0]
    assert ".t-table th:nth-child(5)" in canonical_calibration
    assert ".t-table td:nth-child(5)" in canonical_calibration
    assert (
        "width: calc(17cqw - var(--dp-dashboard-action-track) - 2.89px) !important"
        in canonical_calibration
    )
    assert ".t-table th:nth-child(6)" in canonical_calibration
    assert ".t-table td:nth-child(6)" in canonical_calibration
    assert (
        "width: calc(var(--dp-dashboard-action-track) + var(--dp-dashboard-action-inset)) !important"
        in canonical_calibration
    )
