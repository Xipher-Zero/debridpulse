"""Final desktop Downloads consistency contracts after E1 canonicalization."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_downloads_rows_use_row_level_details_and_retire_drag_semantics() -> None:
    # DP 1.0.12 Post-Classifier-Fix UI Corrections: the row-navigation
    # interactive-descendant guard moved from an inline literal selector into
    # the shared ``dpIsInteractiveRowTarget`` helper so Dashboard Recent could
    # reuse the exact same ownership boundary (fixing a Recent-only click-
    # ownership bug where the common-source group launcher's click also fired
    # row-level Details navigation). The guard's match set is unchanged and
    # now additionally covers the group-candidates trigger explicitly.
    # DP 1.0.12 canonical flattening: ui-downloads.js is the sole owner of
    # the Downloads row template; app.js keeps only the shared
    # dpIsInteractiveRowTarget guard that both Downloads and Dashboard
    # Recent call. 1.0.13 operator queue: the row click goes through the one
    # Downloads row-click owner, which applies the same guard and also
    # swallows the single click that ends a Name-cell drag.
    app = read("app.js")
    downloads = read("ui-downloads.js")
    required = (
        "dp-downloads-detail-row", 'tabindex="0"',
        "event.key==='Enter'", "showDetail(${t.id})",
        'onclick="onDownloadRowClick(event,${t.id})"',
        "if (!dpIsInteractiveRowTarget(event.target)) showDetail(id);",
    )
    missing = [fragment for fragment in required if fragment not in downloads]
    assert not missing, f"row detail contract is missing: {missing}"
    assert "function dpIsInteractiveRowTarget(target)" in app
    assert "button,input,a,select,textarea,label,[role=\"button\"],[data-dp-group-candidates-trigger]" in app
    for forbidden in ('draggable="true"', "ondragstart=", "ondragover=", "ondrop="):
        assert forbidden not in downloads


def test_downloads_rows_emit_final_status_and_action_language() -> None:
    downloads = read("ui-downloads.js")
    transfer = read("ui-transfer-contract.css")
    desktop = read("ui-downloads-page.css")
    for fragment in (
        'data-default-label="Pause"', 'data-default-label="Resume"',
        'data-default-label="Remove"', 'data-default-label="Retry"',
        "pauseT(${t.id},this)", "resumeT(${t.id},this)",
        "deleteT(${t.id},event,this)", "retryT(${t.id},this)",
    ):
        assert fragment in downloads
    for obsolete in ("⏸ Pause", "▶ Resume", "✕ Remove", "↻ Retry"):
        assert obsolete not in downloads
    assert 'button[onclick*="retryT("]' not in desktop
    assert "font-size: 0 !important" not in desktop
    assert "content: 'Retry'" not in desktop
    assert '[onclick*="retryT("]' in transfer
    assert "background: var(--dp-op-surface, var(--dp-state-active-bg)) !important" in transfer
    assert "border-color: var(--dp-op-edge, color-mix(in srgb, var(--dp-state-active) 34%, transparent)) !important" in transfer
    assert "color: var(--dp-op-ink, var(--dp-state-active)) !important" in transfer
    assert "box-shadow: var(--dp-op-glow, none) !important" in transfer
    required_geometry = (
        "min-height: 25px !important", "padding: 0 9px !important",
        "border-radius: 6px !important", "font-size: 10.5px !important",
        "width: 72px !important", "min-width: 72px !important",
        "min-height: 36px !important", "height: 36px !important",
        "padding: 0 8px !important", "border-radius: 8px !important",
        "font-size: 11.5px !important",
    )
    missing = [fragment for fragment in required_geometry if fragment not in transfer]
    assert not missing, f"shared row language is missing: {missing}"


def test_downloads_footer_language_tracks_selected_filter() -> None:
    app = read("ui-downloads.js")
    matrix = (
        "No Retained Items", "Showing 1 Retained Item", "Retained Items",
        "No Active Downloads", "1 Active Download", "Active Downloads",
        "No Paused Downloads", "1 Paused Download", "Paused Downloads",
        "No Downloads Currently Processing", "1 Download Currently Processing", "Downloads Currently Processing",
        "No Downloads in Ready State", "1 Download in Ready State", "Downloads in Ready State",
        "No Downloads Completed Yet", "1 Download Completed", "Downloads Completed",
        "No Downloads Have Errors", "1 Download Has Errors", "Downloads Have Errors",
    )
    missing = [fragment for fragment in matrix if fragment not in app]
    assert not missing, f"filter footer language is missing: {missing}"
    assert "downloadPaginationSummary(n, from, to)" in app


def test_downloads_header_uses_download_art_not_recent_activity_art() -> None:
    index = read("index.html")
    section = index[index.index('id="view-torrents"'):index.index('<!-- Events -->')]
    assert "/icons/dp/card-download.svg?v=11" in section
    assert "card-document-stack.svg" not in section


def test_downloads_desktop_columns_preserve_provider_identity_status_progress_and_actions() -> None:
    # DP 1.0.12 canonical flattening: ui-downloads-desktop.css was folded
    # into ui-downloads-page.css. 1.0.13 operator queue: Name is the first
    # column and the selection column the last, after Actions. 1.0.14 unified
    # table: one composition from 780px up, Download Activity's -- the
    # provider identity joins the Name column, every track is a length
    # against the table's own width (a size container), and the tracks always
    # sum to the table (Name is the complement), so the fixed layout never
    # takes width from another column and the table never outgrows its
    # viewport. Status holds every badge (180px) and Added its widest one-line
    # value (178px); Actions holds every button its rows show together (two
    # 72px slots and their 7px gap, three where a row shows three); Progress
    # yields to Name's 280px floor and never steps aside. The compact table
    # (narrower than 1166px) sets Added aside and narrows Status and each
    # action to its glyph; narrower than 763px Size moves beside the name.
    import re

    css = read("ui-downloads-page.css")
    # The action-slot counts (desktop: real actions only -- the handheld Start
    # Now copy is not one; handheld: every action the Actions cell holds).
    assert "nth-child" not in css.replace(":is(#t-tbody, .dp-downloads-styled-tbody) .actions > :nth-child(3 of :not(.dp-queue-consent-action))", "").replace(
        ":is(#t-tbody, .dp-downloads-styled-tbody) .actions > :nth-child(3)", "")
    for retired in ("(width <= 1050px)", "(width > 1050px) and (width < 1180px)",
                    "(min-width: 1180px) and (width < 1390px)", "(min-width: 901px) and (width < 1390px)"):
        assert retired not in css

    def block(query, owner=""):
        # The block for ``query`` that names ``owner`` (the header and the
        # selection toolbar have their own 780px blocks).
        return next(part.split("\n}", 1)[0] for part in css.split(query + " {")[1:] if owner in part.split("\n}", 1)[0])

    # 1.0.14 four-fix: the one composition holds at every width (the table
    # never outgrows its viewport, handheld included), so it is no longer
    # inside a 780px block.
    full = css[css.index("/* ── Table composition, at every width"):css.index("/* Compact row controls.")]
    assert "@media" not in full.split("*/", 1)[1]
    assert "container: dp-downloads-table / inline-size;" in full
    assert "--dp-downloads-status-track: 180px;" in full and "--dp-downloads-date-track: 178px;" in full
    assert "--dp-downloads-size-track: max(84px, 7cqw);" in full
    assert "--dp-downloads-action-slot: 72px;" in full and "--dp-downloads-action-slots: 2;" in full
    assert "--dp-downloads-select-track: 36px;" in full and "* 7px + var(--dp-downloads-action-allowance, 28px))" in full and "gap: 7px;" in full
    assert ":has(> :is(#t-tbody, .dp-downloads-styled-tbody) .actions > :nth-child(3 of :not(.dp-queue-consent-action)))" in full and "--dp-downloads-action-slots: 3;" in full
    assert "min(22cqw, 100cqw - 280px - var(--dp-downloads-fixed-tracks))" in full
    assert "width: calc(100cqw - var(--dp-downloads-progress-track) - var(--dp-downloads-fixed-tracks));" in full
    assert re.search(r"\.dp-downloads-status-cell \{\s*width: var\(--dp-downloads-status-track\);\s*text-align: center;", full)
    assert re.search(r"\.dp-downloads-action-cell \{\s*width: var\(--dp-downloads-action-track\);\s*text-align: center;", full)

    compact = block("@container dp-downloads-table (width < 1166px)")
    assert "--dp-downloads-status-track: 63px;" in compact and "--dp-downloads-action-slot: 36px;" in compact
    assert "--dp-downloads-date-track: 0px;" in compact
    assert re.search(r"\.dp-downloads-date-cell \{\s*display: none;", compact)
    relocated = block("@container dp-downloads-table (width < 763px)")
    assert "--dp-downloads-size-track: 0px;" in relocated
    assert re.search(r"\.dp-downloads-size-cell \{\s*display: none;", relocated)
    assert re.search(r":is\(#t-tbody, \.dp-downloads-styled-tbody\) \.dp-downloads-size \{\s*display: block;", relocated)
    # 1.0.14 handheld (779px and below): the Transfer Summary, Actions and the
    # selection column. Status, Progress, Size and Date step aside -- columns
    # and tracks -- after the compact steps; the name keeps its thin slot and
    # its one-line metadata closed by the size (the shared summary owner,
    # ui-dashboard-transfer-presentation.css), which no longer wraps.
    hide = ".t-table :is(.dp-downloads-status-cell, .dp-downloads-progress-cell, .dp-downloads-size-cell, .dp-downloads-date-cell) { display: none; }"
    handheld = block("@media (max-width: 779px)", "--dp-downloads-progress-track: 0px;")
    assert css.count(hide) == 1 and hide in handheld
    for track in ("status", "progress", "size", "date"):
        assert f"--dp-downloads-{track}-track: 0px;" in handheld, track
    assert ":has(> :is(#t-tbody, .dp-downloads-styled-tbody) .actions > :nth-child(3))" in handheld
    assert ":is(.dash-row-bar-slot, .dp-downloads-size) { display: none; }" not in css
    # 1.0.14 six fixes (C): between the handheld columns each cell keeps 4px
    # (Actions' track is its buttons and that 8px; the checkbox stays centred in
    # its 36px track); the former narrow-table allowance exception is folded in.
    assert "--dp-downloads-action-allowance: 8px;" in handheld
    assert ".dp-downloads-name-cell:not(:last-child) { padding-right: 4px; }" in handheld
    assert ".dp-downloads-action-cell { padding-inline: 4px; }" in handheld
    assert ".dp-downloads-select-cell { padding-inline: 0; }" in handheld
    assert "--dp-downloads-action-allowance: 10px;" not in css and "padding-inline: 5px;" not in css
    # Where the table is narrower than 336px the summary's metadata gaps close from 6px to 4px.
    narrow = handheld[handheld.index("@container dp-downloads-table (width < 336px) {"):]
    assert ".dp-transfer-meta-main { gap: 4px; }" in narrow
    # Last of all, there only: a short-name line with a candidate launcher
    # narrows its route launcher's padding; the provider chip's own stays.
    assert ('.dp-transfer-provider-meta[data-dp-fit="short"]:has(.dp-candidate-chip) .dp-root-provider-launcher'
            ' { padding-inline: 4px; gap: 4px; }') in narrow
    assert ".dp-transfer-provider-meta { flex-wrap: wrap; }" not in css
    assert css.index("@container dp-downloads-table (width < 763px)") < css.index("--dp-downloads-progress-track: 0px;")
    # No inline floor beneath the composition: the table is as wide as its viewport.
    assert '<table class="t-table" style="min-width:500px">' not in read("index.html")


def test_downloads_uses_shell_height_and_has_no_legacy_card_bottom_margin() -> None:
    css = read("ui-downloads-page.css")
    assert "height: 100% !important" in css
    assert "margin-bottom: 0 !important" in css
    assert "calc(100vh - var(--dp-shell-header)" not in css


def test_e1_removes_post_render_downloads_owner() -> None:
    index = read("index.html")
    assert not (STATIC / "ui-downloads-runtime.js").exists()
    assert "ui-downloads-runtime.js" not in index


def test_downloads_workspace_holds_from_780px_and_pages_the_rows_its_viewport_holds() -> None:
    # 1.0.14 four-fix: one full-height workspace from the desktop down to the
    # tablet floor (780px), as Statistics. 1.0.14 six fixes (D): the same flex
    # workspace at every width -- exactly the content box from 780px, at least
    # it on a handheld (the page scrolls only where the card's chrome cannot
    # fit) -- and no 62vh viewport cap.
    css = read("ui-downloads-page.css")
    assert "@media (min-width: 901px) {\n  #view-torrents:is(.active, [data-dp-motion-outgoing])" not in css
    assert "@media (max-width: 900px)" not in css
    workspace = css[css.index("@media (min-width: 780px) {\n  #view-torrents:is(.active, [data-dp-motion-outgoing]) {"):]
    workspace = workspace[:workspace.index("\n}\n")]
    assert "height: 100% !important;" in workspace
    assert "#view-torrents > .card {\n  display: flex;\n  flex: 1 1 auto;" in css
    assert "#view-torrents .dp-downloads-table-wrap {\n  flex: 1 1 auto;\n  min-height: 0;\n  overflow: auto !important;" in css
    handheld = css[css.index("@media (max-width: 779px) {\n  /* Handheld: the workspace above"):]
    handheld = handheld[:handheld.index("\n}\n")]
    assert "min-height: 100%;" in handheld
    assert "62vh" not in css
    # Capacity is the table viewport's own: its client height beneath the
    # sticky heading, never the window's height.
    downloads = read("ui-downloads.js")
    size = downloads[downloads.index("function measuredSize() {"):downloads.index("function applySize(")]
    assert "window.innerHeight" not in size
    # 1.0.14 handheld: the same owner measures below 780px too -- the room is
    # the table viewport's (no taller than its cap) and one screen of the
    # workspace beneath the card's own chrome; the desktop room is unchanged.
    assert "let room = wrap.clientHeight;" in size and "if (!window.matchMedia(DESKTOP_QUERY).matches) {" in size
    assert "room = content.clientHeight - parseFloat(space.paddingTop) - parseFloat(space.paddingBottom) - chrome;" in size
    assert "maxHeight" not in size
    assert "const budget = Math.max(0, room - head.getBoundingClientRect().height);" in size
    assert "Math.floor((budget + 0.5) / Math.max(...heights))" in size
    assert "document.fonts?.addEventListener?.('loadingdone', () => scheduleCapacityCheck());" in downloads
