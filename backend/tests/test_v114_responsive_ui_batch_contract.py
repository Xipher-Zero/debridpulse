"""DP 1.0.14 responsive UI batch: the source-level invariants.

The behaviour itself is proved in the browser (settings-responsive-batch.spec.js,
executor-work.spec.js, downloads-selection-overflow.spec.js); these pin the
owners each fix lives in, so a later edit cannot quietly reopen one.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"
PAGE = (STATIC / "ui-settings-page.js").read_text(encoding="utf-8")
PAGE_CSS = (STATIC / "ui-settings-page.css").read_text(encoding="utf-8")
COMPLETION_CSS = (STATIC / "ui-settings-downloads-completion.css").read_text(encoding="utf-8")
DOWNLOADS_CSS = (STATIC / "ui-downloads-page.css").read_text(encoding="utf-8")
WORK = (STATIC / "ui-settings-executor-work.js").read_text(encoding="utf-8")
APP = (STATIC / "app.js").read_text(encoding="utf-8")


def _function(name: str) -> str:
    return PAGE.split(f"function {name}(", 1)[1].split("\n  }\n", 1)[0]


def _keys(block: str) -> list[str]:
    return re.findall(r"(?:input|selectField|tuningToggle)\(\s*'(\w+)'|(youtubePlayerClientsField)\(options\)", block)


def test_the_downloads_viewport_reserves_its_scrollbar_room():
    # Item 1: the table's tracks are sized from the viewport's 100cqw, which a
    # scrollbar appearing later (a selection's toolbar taking height) does not
    # shrink -- so its room is reserved, not hidden.
    wrap = DOWNLOADS_CSS.split("/* ── Data viewport", 1)[1].split("#view-torrents .dp-downloads-table-wrap {", 1)[1]
    wrap = wrap.split("}", 1)[0]
    assert "scrollbar-gutter: stable;" in wrap
    assert "overflow-x: hidden" not in DOWNLOADS_CSS


def test_the_two_bounded_islands_share_one_measured_stacking_owner():
    # Items 2 and 9: one owner, read from the island laid out as a row.
    for island in ("dp-settings-download-engine-row dp-settings-island", "dp-settings-extraction-behavior dp-settings-island"):
        assert PAGE.count(f'<div class="{island}"') == 1, island
    fit = _function("fitIslands")
    assert "root()?.querySelectorAll('.dp-settings-island')" in fit
    assert "boxes.some((box, index) => index && box.left < boxes[index - 1].right)" in fit
    assert "islands.forEach(island => islandObserver.observe(island.parentElement));" in fit
    assert PAGE.count("fitIslands();") == 1 and PAGE.count("islandFit?.();") == 1
    stacked = PAGE_CSS.split("@media (min-width: 780px) {\n  #view-settings .dp-settings-island[data-island='stacked'] {", 1)[1]
    assert [line.strip() for line in stacked.split("}", 1)[0].strip().splitlines()] == ["flex-direction: column;", "align-items: stretch;"]


def test_the_locked_tunable_orders():
    # Items 3, 4, 5 and 10: the canonical rendered order, never a CSS `order`.
    assert [key or alias for key, alias in _keys(_function("usenetTuning"))] == [
        "usenet_direct_write", "usenet_article_cache_megabytes",
        "usenet_max_acquisition_retries", "usenet_operation_timeout_seconds"]
    assert [key or alias for key, alias in _keys(_function("mediaTuning"))] == [
        "youtubePlayerClientsField", "media_target_resolution", "media_video_quality", "media_video_codec",
        "preferred_subtitle_language", "media_concurrent_fragment_downloads"]
    recovery = _function("downloadsPanel").split("const recovery = card('Disk Space & Recovery'", 1)[1]
    assert [key for key, _ in _keys(recovery)] == [
        "stuck_download_timeout_hours", "min_free_disk_gb", "disk_guard_resume_hysteresis_gb",
        "aria2_error_retry_count", "aria2_error_retry_delay_seconds"]
    extraction = _function("extractionPanel")
    assert [key for key, _ in _keys(extraction)][:2] == ["extract_delete_archive", "extract_max_concurrent"]
    for css in (PAGE_CSS, COMPLETION_CSS):
        assert not re.search(r"(?:usenet|media|recovery|extraction)[^{}]*\{[^}]*\border\s*:", css)


def test_retry_delay_keeps_every_fact_in_fewer_words():
    # Item 6: one description, about a third shorter.
    old = ("How long DebridPulse waits before checking or re-resolving a download after an error, and before trying a "
           "provider again after a temporary failure. A plain retry doubles the wait after each failure but never waits "
           "more than 5 minutes. Set to 0 to retry immediately.")
    new = ("Wait before checking or re-resolving a download after an error, or retrying a temporarily failed provider. "
           "Plain retries double the delay, up to 5 minutes. Set to 0 to retry immediately.")
    assert f"hint: '{new}'" in PAGE and old not in PAGE
    assert len(new) <= 0.72 * len(old)


def test_automatic_extraction_takes_the_hinted_header_grammar():
    # Item 8: the shared card() grammar and fitter, Help then Enable in its rail.
    extraction = _function("extractionPanel")
    assert "help: 'automatic-extraction',\n      hinted: true," in extraction
    assert "headerCenter: '<span class=\"dp-settings-extraction-header-copy\">Automatically extract supported archives after a download completes.</span>'" in extraction
    card = _function("card")
    assert "}${help}${\n          options.headerAction" in card and "${\n          options.action || ''}</div>`" in card
    # Its private header grid is retired, not overridden.
    assert ".dp-settings-extraction-card > .card-header" not in COMPLETION_CSS
    assert ".dp-settings-extraction-card > .card-header" not in PAGE_CSS


def test_download_engine_activity_follows_the_one_stream_by_admission():
    # Item 7: no Refresh, no poll, no second stream; one admission owner.
    for static in STATIC.glob("*.js"):
        assert "data-dp-executor-work-refresh" not in static.read_text(encoding="utf-8"), static.name
    for retired in ("POLL_MS", "schedulePoll", "refreshRunning", "setInterval", "EventSource", "new WebSocket"):
        assert retired not in WORK, retired
    assert "const shown = () => !!workCard() && administers() && settingsVisible() && downloadsVisible() && !document.hidden;" in WORK
    assert "const administers = () => typeof dpCan === 'function' && dpCan('administer');" in WORK
    assert "const REFRESH_SPACING_MS = 2000, REFRESH_SPACING_MAX_MS = 10000, LATENCY_SPACING_FACTOR = 5;" in WORK
    assert "if (mine === generation && shown()) {" in WORK
    for signal in ("'debridpulse:execution-activity-changed', invalidated", "'debridpulse:pulse-connected', invalidated",
                   "'debridpulse:pulse-heartbeat'"):
        assert f"document.addEventListener({signal}" in WORK, signal
    # The bridge: one payload-free document event from signals every connection
    # already receives; the application still opens exactly one EventSource.
    assert APP.count("new EventSource(") == 1
    assert APP.count("executionActivityChanged();") == 2
    assert "document.dispatchEvent(new CustomEvent('debridpulse:execution-activity-changed'));" in APP
