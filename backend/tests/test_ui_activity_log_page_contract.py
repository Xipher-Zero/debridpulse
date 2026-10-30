"""Activity Log rebuild and shared desktop-shell regression contracts."""

from pathlib import Path

from ui_chrome_tokens import require_token


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_activity_log_keeps_approved_content_with_direct_structural_owners() -> None:
    css = read("ui-activity-log-page.css")
    index = read("index.html")
    app = read("app.js")
    manifest = read("icons/dp/manifest.json")
    view = index[index.index('id="view-events"'):index.index('<!-- Statistics -->')]
    assert "dp-activity-card" in view
    assert "document.svg" in view
    assert "For the Record" in view
    assert "Everything DebridPulse thought was worth mentioning." in view
    assert "Refresh activity log" in view
    assert 'data-dp-lucide="refresh"' in view
    # app.js holds no Activity Log implementation; navigation calls the owner.
    assert "function loadEvents(" not in app
    assert "function filterEvents(" not in app
    assert "window.DPActivityLog.load()" in app
    assert '"document": "document.svg"' in manifest
    required = (".dp-activity-card", ".dp-activity-card-title", ".dp-activity-search-band", ".dp-activity-list", ".dp-activity-row", ".dp-activity-message", ".dp-activity-transfer", ".dp-activity-time")
    missing = [selector for selector in required if selector not in css]
    assert not missing, f"Activity rebuild is missing structural selectors: {missing}"

def test_auto_refresh_is_one_island_left_of_refresh_on_by_default_and_kept_for_the_tab_session() -> None:
    import re

    index = read("index.html")
    css = read("ui-activity-log-page.css")
    runtime = read("ui-activity-log-runtime.js")
    header = index[index.index('id="view-events"'):index.index('class="dp-activity-search-band"')]
    actions = header[header.index('class="dp-activity-header-actions"'):]
    # The island, then Refresh, side by side in the header's right-hand controls.
    assert actions.index('class="dp-activity-live"') < actions.index("dp-activity-refresh")
    island = actions[actions.index('<label class="dp-activity-live"'):actions.index("</label>")]
    assert re.sub(r"<[^>]+>", "", island.split('<span id="ev-live-status"')[0]).strip() == "Auto Refresh"
    switch = re.search(r"<input[^>]*>", island).group(0)
    assert 'id="ev-live"' in switch and 'type="checkbox"' in switch and 'role="switch"' in switch
    assert "checked" not in switch                         # OFF in the markup ...
    assert 'for="ev-live"' in island and 'class="toggle"' in island and 'class="ttrack"' in island
    # ... and nothing durable remembers it: no local storage, no setting, no saved preference --
    # only the tab session's own choice (below).
    for persisted in ("localStorage", "settingsData.activity_log_auto", "PUT", "PATCH"):
        assert persisted not in runtime
    assert runtime.count("sessionStorage.getItem(") == 1 and runtime.count("sessionStorage.setItem(") == 1   # the one choice
    # On unless switched off: the operator's choice lasts for the tab session (sessionStorage),
    # restored on every entry, and is nothing the server keeps.
    assert "document.addEventListener('debridpulse:navigation',event=>{live=event.detail?.view==='events'&&chosen();" in runtime
    assert "function chosen(){try{return sessionStorage.getItem(LIVE_CHOICE)!=='off';}catch(_){return true;}}" in runtime
    assert "live=toggle.checked;choose(live);" in runtime
    # The island's height is Refresh's, by stretching; its material is the Settings island's.
    block = css[css.index("#view-events .dp-activity-header-actions {"):]
    assert "align-items: stretch;" in block[:block.index("}")]
    live = css[css.index("#view-events .dp-activity-live {"):]
    live = live[:live.index("}")]
    assert "border: 1px solid var(--dp-divider);" in live and "border-radius: 12px;" in live
    assert "height" not in live and "!important" not in live


def test_auto_refresh_follows_the_one_stream_and_reads_rows_only_from_the_journal() -> None:
    app = read("app.js")
    runtime = read("ui-activity-log-runtime.js")
    # One application EventSource; it forwards the hint, it renders nothing.
    assert app.count("new EventSource(") == 1
    signal = app[app.index("'activity_journal_changed'"):]
    signal = signal[:signal.index("es.addEventListener(")]
    assert "debridpulse:activity-journal-changed" in signal and "DPActivityLog" not in signal
    assert "event-list" not in signal
    # The owner opens no stream and runs no timer loop of its own; a hint
    # becomes the ordinary newest-page request, coalesced.
    assert "EventSource" not in runtime and "setInterval" not in runtime
    assert "document.addEventListener('debridpulse:activity-journal-changed',hint);" in runtime
    assert "const kept={cursor,request,nav};restart();void fetchPage({quiet:kept});" in runtime
    # A failed Auto Refresh restores the page's snapshot, cursors and pager and says nothing.
    assert "if(quiet){cursor=quiet.cursor;request=quiet.request;nav=quiet.nav;" in runtime
    # A row is only ever rendered from a GET /events answer.
    assert runtime.count("render(payload,c);") == 1     # the one call, in fetchPage
    assert "event?.detail?.latest" in runtime and "items" not in runtime[runtime.index("function hint("):
                                                                           runtime.index("document.addEventListener('debridpulse:activity-journal-changed'")]
    # An older page or a newest page read below its top is never replaced.
    assert "function following(){" in runtime and "nav.page===1" in runtime and "scrollTop<=TOP_SLACK" in runtime
    # A lost stream is said, not hidden; a reconnect is caught up from the journal.
    assert "debridpulse:pulse-lost" in runtime and "debridpulse:pulse-connected" in runtime
    assert "dataset.dpPulse = 'lost'" in app and "dataset.dpPulse = 'connected'" in app


def test_activity_log_fills_shell_and_scrolls_only_rebuilt_event_viewport() -> None:
    css = read("ui-activity-log-page.css")

    assert "#content:has(#view-events.active)" in css
    assert "overflow-y: hidden" in css
    assert "#view-events:is(.active, [data-dp-motion-outgoing])" in css
    assert "display: flex !important" in css
    assert "height: 100% !important" in css
    assert "min-height: 0" in css
    assert "overflow: visible" in css
    assert "margin-bottom: 0 !important" in css
    assert ".dp-activity-list" in css
    assert "flex: 1 1 auto" in css
    assert "overflow-y: auto" in css
    assert "scrollbar-gutter: stable" in css
    assert "100vh" not in css
    assert "calc(100vh" not in css


def test_activity_rebuild_uses_downloads_search_band_recipe() -> None:
    activity = read("ui-activity-log-page.css")
    downloads = read("ui-downloads-page.css")

    shared_fragments = (
        "padding: 13px 16px 14px !important;",
        "border-bottom: 1px solid var(--dp-divider);",
        "color-mix(in srgb, var(--dp-accent-purple) 5.5%, transparent)",
        "color-mix(in srgb, var(--dp-accent-purple) 4.5%, transparent)",
    )
    for fragment in shared_fragments:
        assert fragment in activity
        assert fragment in downloads


def test_activity_filter_control_keeps_one_fixed_footprint_and_no_projected_selects() -> None:
    # 1.0.14: the three projected selects (Time Window, Severity, Event Type)
    # and Reset gave way to one control beside Search -- Filter Results or Clear
    # Filters -- whose one fixed footprint holds the wider of the two, so Search
    # never moves when it changes function. Nothing of the old fields remains.
    page = read("ui-activity-log-page.css")
    controls = read("ui-activity-log-controls.css")
    index = read("index.html")
    view = index[index.index('id="view-events"'):index.index('<!-- Statistics -->')]

    trigger = controls.split("#view-events .dp-activity-filter-trigger {", 1)[1].split("}", 1)[0]
    for declaration in ("flex: 0 0 140px;", "width: 140px;", "height: 38px;"):
        assert declaration in trigger
    for retired in ("#ev-level", "#ev-timeframe", "#ev-category", "#ev-reset", "dp-activity-filter-field", "dp-activity-filter-label"):
        assert retired not in page and retired not in controls and retired not in view
    assert "<select" not in view
    assert view.count('type="radio"') == 7 + 4 + 14


def test_activity_refresh_consumes_dashboard_recover_control_recipe() -> None:
    controls = read("ui-utility-controls.css")

    assert "#view-dashboard #btn-recover-all," in controls
    assert "#view-events .dp-activity-refresh" in controls
    assert "height: var(--dp-utility-control-block, 36px) !important;" in controls   # 36px; Quick Add's handheld header alone sets 44px
    assert "border-radius: 9px !important;" in controls
    # The reviewed light recipe is the utility chrome tokens' native values.
    assert "background: var(--dp-chrome-utility-surface) !important;" in controls
    assert "border-color: var(--dp-chrome-utility-edge) !important;" in controls
    assert "color: var(--dp-chrome-utility-text) !important;" in controls
    require_token(controls, "--dp-chrome-utility-surface", "rgba(151,87,203,.018)")
    require_token(controls, "--dp-chrome-utility-edge", "#c5a3dc")
    require_token(controls, "--dp-chrome-utility-text", "#714790")
    assert ".dp-activity-refresh:hover" in controls


def test_transfer_row_actions_have_equal_footprint_and_shared_pause_material() -> None:
    transfer = read("ui-transfer-contract.css")
    controls = read("ui-utility-controls.css")

    assert "width: 72px !important;" in transfer
    assert "min-width: 72px !important;" in transfer
    assert "height: 36px !important;" in transfer
    # Row and topbar Pause share one light material: the caution hue's.
    shared_pause = "--dp-op-hue: var(--dp-state-caution);"
    shared_pause_hover = "background: var(--dp-op-surface-hover) !important;"
    assert shared_pause in transfer
    assert shared_pause in controls
    assert shared_pause_hover in transfer
    assert shared_pause_hover in controls


def test_desktop_provider_status_bottom_datum_is_shell_owned() -> None:
    shell = read("ui-shell-structural.css")
    activity = read("ui-activity-log-page.css")

    # The desktop datum is the shell's from its one 780px mode change.
    assert "@media (min-width: 780px) {\n  .sidebar-footer {\n    bottom: 24px !important;" in shell
    assert ":has(#view-torrents.active) .sidebar-footer" not in shell
    assert ".sidebar-footer" not in activity


def test_activity_log_layer_follows_canonical_shell_and_reference_stack() -> None:
    overlay = read("style.css")

    shell = overlay.index("/ui-shell-structural.css?v=31")
    dashboard = overlay.index("/ui-dashboard.css?v=22")
    controls = overlay.index("/ui-utility-controls.css?v=25")
    stats = overlay.index("/ui-statistics-page.css?v=23")
    activity = overlay.index("/ui-activity-log-page.css?v=32")
    downloads = overlay.index("/ui-downloads-page.css?v=30")
    transfer = overlay.index("/ui-transfer-contract.css?v=33")

    assert shell < dashboard < controls < stats < activity < downloads < transfer


def test_activity_log_workspace_holds_from_780px_and_keeps_its_configured_page() -> None:
    # 1.0.14 four-fix: one full-height workspace from the desktop down to the
    # tablet floor (780px); the 62vh list is handheld only. The list keeps its
    # configured page (50/100/250) and scrolls it -- never trimmed to the view.
    css = read("ui-activity-log-page.css")
    assert "@media (min-width: 901px)" not in css and "@media (max-width: 900px)" not in css
    workspace = css[css.index("@media (min-width: 780px) {\n  #content:has(#view-events.active) {"):]
    workspace = workspace[:workspace.index("\n}\n")]
    assert "height: 100% !important;" in workspace and "overflow-y: hidden;" in workspace
    handheld = css[css.index("@media (max-width: 779px) {\n  #view-events:is(.active, [data-dp-motion-outgoing]) {"):]
    handheld = handheld[:handheld.index("\n}\n")]
    assert "max-height: 62vh;" in handheld and css.count("62vh") == 1
    runtime = read("ui-activity-log-runtime.js")
    assert "const PAGE_SIZES=[50,100,250],DEFAULT_PAGE_SIZE=100;" in runtime
    assert "params.set('limit',String(pageSize()));" in runtime and "measuredSize" not in runtime
