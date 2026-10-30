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
        # Provider-side work is a state of its own (design-tokens.css), not the
        # decorative accent an alternative palette may recolour.
        "--dp-badge-color: var(--dp-state-processing-badge)",
        ".badge-paused",
        ".badge-ready",
        ".badge-partial",
        "--dp-badge-color: var(--dp-state-caution)",
        ".badge-completed",
        ".badge-error",
        "--dp-badge-color: var(--dp-state-error)",
        ".badge-deleted",
        "--dp-badge-color: var(--dp-text-muted)",
        # The light operational material's ink where a light theme defines it.
        "color: var(--dp-op-ink, var(--dp-badge-color)) !important",
        "min-height: 25px !important",
        "border-radius: 6px !important",
    )
    missing = [fragment for fragment in required if fragment not in css]
    assert not missing, f"transfer status contract is missing: {missing}"
    # Its values are the native accent's, per mode, so DebridPulse renders as before.
    tokens = (TRANSFER.parent / "design-tokens.css").read_text(encoding="utf-8")
    assert "--dp-state-processing-badge: #b45cff;" in tokens
    assert "--dp-state-processing-badge: #9637f5;" in tokens


def test_transfer_actions_share_recent_activity_geometry() -> None:
    css = TRANSFER.read_text(encoding="utf-8")
    required = (
        ":is(#dash-tbody, #t-tbody, .dp-downloads-styled-tbody) .actions .btn",
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
        # Dark: the translucent active treatment; light: the material of its hue.
        "--dp-op-hue: var(--dp-state-active);",
        "background: var(--dp-op-surface, var(--dp-state-active-bg)) !important",
        "border-color: var(--dp-op-edge, color-mix(in srgb, var(--dp-state-active) 34%, transparent)) !important",
        "color: var(--dp-op-ink, var(--dp-state-active)) !important",
        "box-shadow: var(--dp-op-glow, none) !important",
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
    assert ":is(#dash-tbody, #t-tbody, .dp-downloads-styled-tbody) .prog," in css
    assert ":is(#dash-tbody, #t-tbody, .dp-downloads-styled-tbody) .prog-fill" in css
    # 7px unless a handheld Transfer Summary's thin slot sets its own geometry.
    assert "height: var(--dp-row-progress-fill, 7px) !important" in css
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
    # 1.0.14: Added keeps that proportion with a content floor (Name is now
    # the complement), still one declared width summing to exactly 100%.
    assert (
        "--dp-dashboard-date-track: max(21cqw - var(--dp-dashboard-action-track) - 3.57px, 158px);"
        in canonical_calibration
    )
    assert "width: var(--dp-dashboard-date-track) !important" in canonical_calibration
    assert ".t-table th:nth-child(6)" in canonical_calibration
    assert ".t-table td:nth-child(6)" in canonical_calibration
    # The column's own track is the shared action track, narrowed only where
    # the table's compact controls apply (an icon-only action).
    assert "--dp-dashboard-row-action-track: var(--dp-dashboard-action-track);" in canonical_calibration
    assert (
        "width: calc(var(--dp-dashboard-row-action-track) + var(--dp-dashboard-action-inset)) !important"
        in canonical_calibration
    )


def test_transfer_summary_terminal_set_is_the_backends_and_the_summary_reuses_one_progress_projection() -> None:
    # 1.0.14 handheld Transfer Summary: the frontend's terminal presentation
    # set is the backend's own, held equal here, not a second list; and both
    # tables' summaries project the one progress owner (progressParts), whose
    # full renderer is assembled from the same parts.
    import ast
    import re
    backend = (Path(__file__).resolve().parents[1] / "transfers" / "presentation_repository.py").read_text(encoding="utf-8")
    declared = re.search(r"_TERMINAL_PRESENTATION = frozenset\((\{[^}]*\})\)", backend).group(1)
    owner = (STATIC / "ui-processing-presentation.js").read_text(encoding="utf-8")
    mirrored = re.search(r"const TERMINAL_PRESENTATION=Object\.freeze\((\[[^\]]*\])\);", owner).group(1)
    assert set(ast.literal_eval(declared)) == set(ast.literal_eval(mirrored))
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "return parts.track + parts.lane + parts.line + parts.activity;" in app
    assert "function progressSummary(t, presentation) {" in app
    assert "window.DPProcessingPresentation.TERMINAL_PRESENTATION.includes(" in app
    for renderer in ("ui-dashboard-transfer-presentation.js", "ui-downloads.js"):
        source = (STATIC / renderer).read_text(encoding="utf-8")
        assert "progressSummary(t" in source and "statusGlyph(transferDisplayStatus(t)" in source, renderer


def test_provider_short_labels_belong_to_the_identity_owner_and_name_real_integrations() -> None:
    # 1.0.14 handheld Transfer Summary: a provider badge's short label is the
    # identity owner's, keyed by integration id, for exactly the catalog names
    # longer than a handheld badge; each id is a real integration. The badge's
    # text stays the full name (the short form is a data attribute).
    import re
    owner = (STATIC / "ui-transfer-source-presentation.js").read_text(encoding="utf-8")
    table = owner[owner.index("const SHORT_LABELS = Object.freeze({"):]
    table = table[:table.index("});")]
    labels = dict(re.findall(r"(\w+): '([^']+)'", table))
    assert labels == {"alldebrid": "AD", "debridlink": "DL", "premiumize": "PM", "realdebrid": "RD", "torbox": "TB", "media": "Media"}
    backend = Path(__file__).resolve().parents[1]
    for integration in labels:
        assert (backend / "providers" / integration).is_dir() or (backend / "integrations" / integration).is_dir(), integration
    assert "data-short-label" in owner and "providerLabelMarkup" in owner
    for renderer in ("app.js", "ui-root-provider.js"):
        assert "providerLabelMarkup(" in (STATIC / renderer).read_text(encoding="utf-8"), renderer
