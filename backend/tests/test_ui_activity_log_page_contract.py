"""Activity Log rebuild and shared desktop-shell regression contracts."""

from pathlib import Path


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

def test_auto_refresh_is_one_island_left_of_refresh_off_by_default_and_never_saved() -> None:
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
    # ... and nothing remembers it: no storage, no setting, no saved preference.
    for persisted in ("localStorage", "sessionStorage", "settingsData.activity_log_auto", "PUT", "PATCH"):
        assert persisted not in runtime
    assert "document.addEventListener('debridpulse:navigation',()=>{live=false;" in runtime
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
    assert "#view-events.active" in css
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


def test_activity_projected_level_filter_keeps_compact_reviewed_footprint() -> None:
    css = read("ui-activity-log-page.css")

    native = css.split("#view-events #ev-level {", 1)[1].split("}", 1)[0]
    projected = css.split("#view-events #ev-level + .dp-dropdown-shell {", 1)[1].split("}", 1)[0]
    for block in (native, projected):
        assert "width: 160px;" in block
        assert "max-width: 160px;" in block
        assert "flex: 0 0 160px;" in block


def test_activity_refresh_consumes_dashboard_recover_control_recipe() -> None:
    controls = read("ui-utility-controls.css")

    assert "#view-dashboard #btn-recover-all," in controls
    assert "#view-events .dp-activity-refresh" in controls
    assert "height: 36px !important;" in controls
    assert "border-radius: 9px !important;" in controls
    assert "background: rgba(151,87,203,.018) !important;" in controls
    assert "border-color: #c5a3dc !important;" in controls
    assert "color: #714790 !important;" in controls
    assert ".dp-activity-refresh:hover" in controls


def test_transfer_row_actions_have_equal_footprint_and_shared_pause_material() -> None:
    transfer = read("ui-transfer-contract.css")
    controls = read("ui-utility-controls.css")

    assert "width: 72px !important;" in transfer
    assert "min-width: 72px !important;" in transfer
    assert "height: 36px !important;" in transfer
    shared_pause = "linear-gradient(180deg, rgba(255,251,236,.54) 0%, rgba(250,234,186,.22) 100%)"
    shared_pause_hover = "linear-gradient(180deg, rgba(255,249,226,.68) 0%, rgba(249,229,168,.30) 100%)"
    assert shared_pause in transfer
    assert shared_pause in controls
    assert shared_pause_hover in transfer
    assert shared_pause_hover in controls


def test_desktop_provider_status_bottom_datum_is_shell_owned() -> None:
    shell = read("ui-shell-structural.css")
    activity = read("ui-activity-log-page.css")

    assert "@media (min-width: 901px)" in shell
    assert ".sidebar-footer" in shell
    assert "bottom: 24px !important" in shell
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
