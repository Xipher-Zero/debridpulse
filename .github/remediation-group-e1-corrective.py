from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"
TESTS = ROOT / "backend" / "tests"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    return source.replace(old, new, 1)


def replace_test(source: str, name: str, replacement: str) -> str:
    pattern = re.compile(
        rf"^def {re.escape(name)}\([^\n]*\).*?(?=^def |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    source, count = pattern.subn(replacement.rstrip() + "\n\n", source, count=1)
    if count != 1:
        raise RuntimeError(f"test replacement failed: {name} ({count})")
    return source


# The first E1 focused run exposed one real source omission: Dashboard Recent
# Activity still emitted legacy glyph-prefixed Pause/Resume action labels even
# though the Downloads renderer had already been canonicalized. Own those final
# labels where the Dashboard rows are rendered instead of relying on a runtime
# normalizer that E1 intentionally retires.
app_path = STATIC / "app.js"
app = read(app_path)
old_actions = '''            ${t.status==='downloading' || t.status==='queued' ? `<button class="btn btn-blue btn-sm" onclick="event.stopPropagation();pauseT(${t.id},this)" title="Pause this download">⏸ Pause</button>` : ''}
            ${t.status==='paused' ? `<button class="btn btn-blue btn-sm" onclick="event.stopPropagation();resumeT(${t.id},this)" title="Resume this download">▶ Resume</button>` : ''}'''
new_actions = '''            ${t.status==='downloading' || t.status==='queued' ? `<button class="btn btn-blue btn-sm" data-default-label="Pause" onclick="event.stopPropagation();pauseT(${t.id},this)" title="Pause this download">Pause</button>` : ''}
            ${t.status==='paused' ? `<button class="btn btn-blue btn-sm" data-default-label="Resume" onclick="event.stopPropagation();resumeT(${t.id},this)" title="Resume this download">Resume</button>` : ''}'''
app = replace_once(app, old_actions, new_actions, "Dashboard Recent Activity actions")
write(app_path, app)


# Historical polish tests carried two assertions whose sole purpose was proving
# the now-retired ui-downloads-runtime layer existed. Preserve the independent
# material/icon contracts, but prove the same behavior at the direct app/index
# owners instead.
polish_path = TESTS / "test_ui_downloads_polish_contract.py"
polish = read(polish_path)
polish = polish.replace(
    'RUNTIME = STATIC / "ui-downloads-runtime.js"\n',
    'APP = STATIC / "app.js"\n',
    1,
)
polish = replace_test(
    polish,
    "test_downloads_runtime_carries_header_copy_search_and_empty_language",
    '''def test_downloads_app_carries_header_copy_search_and_empty_language() -> None:
    app = APP.read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    required_app = (
        "download tracked", "downloads tracked", "No downloads yet. Add a link, magnet, or torrent file to get started.",
        "No downloads match your current filters.", "No downloads match your search.",
        "Showing all ", "matching downloads", "dp-pager-current", "chevronLeft", "chevronRight",
        "function renderTorrentPagination(", "function setFilter(",
    )
    missing = [fragment for fragment in required_app if fragment not in app]
    assert not missing, f"Downloads direct app owner is missing: {missing}"
    for fragment in ("card-download.svg?v=11", "Download Queue", "Search downloads…", "Refresh downloads", "dp-downloads-table-wrap"):
        assert fragment in index
    assert "card-document-stack.svg" not in index[index.index('id=\"view-torrents\"'):index.index('<!-- Events -->')]
    assert "green-download-button.svg" not in index
    assert "api('POST'" in app
    assert "api('DELETE'" in app''',
)
polish = replace_test(
    polish,
    "test_downloads_runtime_is_loaded_once_by_the_canonical_document",
    '''def test_downloads_correction_runtime_is_absent_from_canonical_document() -> None:
    operator = OPERATOR.read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert not (STATIC / "ui-downloads-runtime.js").exists()
    assert "/ui-downloads-runtime.js" not in index
    assert "data-dp-downloads-runtime" not in index
    assert "data-dp-downloads-runtime" not in operator
    assert "/ui-downloads-runtime.js" not in operator''',
)
write(polish_path, polish)


# The desktop batch's first test encoded post-render normalization. Keep the
# reviewed filter vocabulary/details behavior but bind it to static markup and
# app.js, where E1 now owns it directly.
desktop_path = TESTS / "test_ui_desktop_downloads_batch_contract.py"
desktop = read(desktop_path)
desktop = replace_test(
    desktop,
    "test_downloads_desktop_filter_contract_and_details_removal",
    '''def test_downloads_desktop_filter_contract_and_details_are_directly_owned():
    index = read("index.html")
    app = read("app.js")
    expected = (
        'data-dp-status=""', 'data-dp-status="downloading"', 'data-dp-status="paused"',
        'data-dp-status="processing"', 'data-dp-status="ready"',
        'data-dp-status="completed"', 'data-dp-status="error"',
    )
    for fragment in expected:
        assert fragment in index
    assert "function setFilter(" in app
    assert "dp-downloads-detail-row" in app
    assert "showDetail(${t.id})" in app
    assert 'draggable="true"' not in app''',
)
write(desktop_path, desktop)


# The v1.0.11.1 canonical test suite contains five assertions that described
# the old runtime split itself. E1 intentionally changes that split. Replace
# only those historical ownership assertions; all independent Settings, Help,
# Statistics, visual, packaging and dead-code contracts remain untouched.
canonical_path = TESTS / "test_v1111_canonical_frontend_contract.py"
canonical = read(canonical_path)
canonical = replace_test(
    canonical,
    "test_statistics_is_final_owner_with_reviewed_copy_default_and_palette",
    '''def test_statistics_is_final_owner_with_reviewed_copy_default_and_palette() -> None:
    source = read(STATS)
    html = read(INDEX)
    style = read(STATIC / "style-v11.css")
    view = html[html.index('<!-- Statistics -->'):html.index('<!-- Help -->')]
    for fragment in (
        "window.loadDetailedStats = loadDetailedStats;",
        "window.DPStatisticsLifecycle = Object.freeze({load: loadDetailedStats, install});",
        "statisticsPurpleGradient", "debridpulse:theme-changed",
    ):
        assert fragment in source
    for fragment in (
        "By the Numbers", "Because vibes are not a performance metric.",
        "dp-statistics-master", "dp-stats-master-header", "dp-stats-master-body",
        "dp-stats-breakdown-grid", "Completed downloads in the last 7 days.",
    ):
        assert fragment in view
    assert "|| '7d'" in source
    assert 'class="ftab active" data-period="7d"' in view
    assert "Completions — last 7 days" not in view
    assert "ensureStatisticsArchitecture" not in source
    assert "decorateChartHeader" not in source
    assert "dash-kpi-strip--dashboard" not in html
    assert not (STATIC / "ui-statistics.css").exists()
    assert "/ui-statistics.css" not in style
    assert "window.loadDetailedStats = wrapped" not in source
    assert not (STATIC / "ui-runtime.js").exists()''',
)
canonical = replace_test(
    canonical,
    "test_runtime_coordination_uses_explicit_events_not_page_convergence_observation",
    '''def test_canonical_coordination_uses_explicit_events_not_page_convergence_observation() -> None:
    app = read(APP)
    operator = read(STATIC / "operator-title.js")
    for event in (
        "debridpulse:navigation", "debridpulse:dashboard-recent-rendered",
        "debridpulse:activity-rendered", "debridpulse:dashboard-stats-rendered",
    ):
        assert event in app
    assert "new MutationObserver" not in operator
    assert "window.loadStats =" not in operator
    assert not (STATIC / "ui-runtime.js").exists()
    assert not (STATIC / "ui-downloads-runtime.js").exists()''',
)
canonical = replace_test(
    canonical,
    "test_downloads_pagination_filtering_are_not_owned_by_app",
    '''def test_downloads_pagination_filtering_are_owned_directly_by_app() -> None:
    app = read(APP)
    assert "function renderTorrentPagination(" in app
    assert "function setFilter(" in app
    assert "renderTorrentPagination" in app
    assert "setFilter" in app
    assert not (STATIC / "ui-downloads-runtime.js").exists()''',
)
canonical = replace_test(
    canonical,
    "test_downloads_static_owner_is_the_accepted_integrated_composition",
    '''def test_downloads_static_and_dynamic_owners_are_the_accepted_integrated_composition() -> None:
    html = read(INDEX)
    app = read(APP)
    page_css = read(STATIC / "ui-downloads-page.css")
    operator = read(STATIC / "operator-title.js")
    view = html[html.index('id="view-torrents"'):html.index('<!-- Events -->')]
    assert 'Download Queue' in view
    assert 'data-dp-filter-contract="desktop-v24"' in view
    assert 'class="dp-card dp-downloads-bulk-card dp-downloads-bulk-integrated" id="bulk-bar"' in view
    assert view.index('id="torrent-search"') < view.index('id="bulk-bar"') < view.index('class="dp-downloads-table-wrap"')
    assert 'id="torrent-page-size"' not in view
    assert 'Most of them followed instructions.' in app
    assert "bar.replaceChildren(header)" not in app
    assert "insertBefore(bar" not in app
    assert "bar.classList.add('dp-card'" not in app
    assert "Canonical integrated multi-selection strip" not in page_css
    assert "data-dp-downloads-runtime" not in operator
    assert '/ui-downloads-runtime.js' not in html
    assert not (STATIC / "ui-downloads-runtime.js").exists()''',
)
canonical = replace_test(
    canonical,
    "test_core_canonical_owners_do_not_reintroduce_historical_wrapper_patterns",
    '''def test_core_canonical_owners_do_not_reintroduce_historical_wrapper_patterns() -> None:
    sources = {
        "app": read(APP), "settings": read(SETTINGS), "help": read(HELP),
        "statistics": read(STATS), "operator": read(STATIC / "operator-title.js"),
    }
    forbidden = (
        "baseRenderSettings", "legacyRender", "previous.apply",
        "window.loadSettings = wrapped", "window.loadDetailedStats = wrapped",
        "window.api = wrapped",
    )
    for name, source in sources.items():
        for fragment in forbidden:
            assert fragment not in source, f"{name} regained wrapper pattern {fragment}"
    assert not (STATIC / "ui-runtime.js").exists()
    assert not (STATIC / "ui-downloads-runtime.js").exists()''',
)
write(canonical_path, canonical)
