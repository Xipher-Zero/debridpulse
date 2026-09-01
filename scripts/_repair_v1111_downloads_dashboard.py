from pathlib import Path
import re

root = Path('.')
static = root / 'frontend' / 'static'

# index.html: final static Downloads composition + removal of inherited
# dashboard startup status/debug surfaces.
index_path = static / 'index.html'
index = index_path.read_text(encoding='utf-8')
status_start = index.index('      <div id="debug-status"')
status_end = index.index('      <!-- ── Unified transfer submission', status_start)
removed_status = index[status_start:status_end]
assert 'dash-health-bar' in removed_status
assert 'dash-health-recovery' in removed_status
index = index[:status_start] + index[status_end:]

downloads_start = index.index('    <!-- Downloads -->\n    <div class="view" id="view-torrents">')
downloads_end = index.index('\n    <!-- Events -->', downloads_start)
canonical_downloads = '''    <!-- Downloads -->
    <div class="view" id="view-torrents">
      <div class="card">
        <div class="card-header">
          <span class="card-title" id="torrent-card-title">Download Queue</span>
          <div class="filter-tabs" data-dp-filter-contract="desktop-v24" role="tablist" aria-label="Download status filter">
            <div class="ftab active" data-dp-status="" role="tab" aria-selected="true" onclick="setFilter(this,'')">All</div>
            <div class="ftab" data-dp-status="downloading" role="tab" aria-selected="false" onclick="setFilter(this,'downloading')">Downloading</div>
            <div class="ftab" data-dp-status="paused" role="tab" aria-selected="false" onclick="setFilter(this,'paused')">Paused</div>
            <div class="ftab" data-dp-status="processing" role="tab" aria-selected="false" onclick="setFilter(this,'processing')">Processing</div>
            <div class="ftab" data-dp-status="ready" role="tab" aria-selected="false" onclick="setFilter(this,'ready')">Ready</div>
            <div class="ftab" data-dp-status="completed" role="tab" aria-selected="false" onclick="setFilter(this,'completed')">Done</div>
            <div class="ftab" data-dp-status="error" role="tab" aria-selected="false" onclick="setFilter(this,'error')">Error</div>
          </div>
          <button class="btn btn-ghost btn-sm dp-downloads-refresh" onclick="loadTorrents()" aria-label="Refresh downloads" title="Refresh downloads">Refresh</button>
        </div>
        <div style="padding:10px 14px 0">
          <div class="ev-search-row">
            <input class="input" id="torrent-search" placeholder="Search downloads…" oninput="onTorrentSearchInput()"/>
          </div>
        </div>
        <div class="bulk-bar dp-downloads-bulk-card dp-downloads-bulk-integrated" id="bulk-bar">
          <div class="dp-card__header dp-downloads-bulk-toolbar">
            <div class="dp-downloads-bulk-actions">
              <button class="btn dp-downloads-bulk-action dp-downloads-bulk-action--pause" onclick="bulkAction('pause',this)">Pause</button>
              <button class="btn dp-downloads-bulk-action dp-downloads-bulk-action--resume" onclick="bulkAction('resume',this)">Resume</button>
              <button class="btn dp-downloads-bulk-action dp-downloads-bulk-action--reset" onclick="bulkAction('reset',this)">Reset</button>
              <span class="dp-downloads-bulk-separator" aria-hidden="true"></span>
              <button class="btn btn-danger dp-downloads-bulk-action dp-downloads-bulk-action--delete" onclick="bulkAction('delete',this)">Delete</button>
            </div>
            <div class="dp-downloads-bulk-status">
              <span id="bulk-count" class="dp-downloads-bulk-count"></span>
              <button class="btn dp-downloads-bulk-action dp-downloads-bulk-action--clear" onclick="clearSelection()">Clear Selections</button>
            </div>
          </div>
        </div>
        <div class="dp-downloads-table-wrap" style="overflow-x:auto;-webkit-overflow-scrolling:touch">
        <table class="t-table" style="min-width:500px">
          <thead><tr><th style="width:32px"><input type="checkbox" id="chk-all" onchange="toggleAllCheckboxes(this)"/></th><th>Name</th><th>Source / Label</th><th>Status</th><th>Progress</th><th class="sz">Size</th><th class="sz">Added</th><th>Actions</th></tr></thead>
          <tbody id="t-tbody"><tr><td colspan="8" class="empty">Loading…</td></tr></tbody>
        </table>
        </div>
        <div id="torrent-pagination" style="display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-top:1px solid var(--border);flex-wrap:wrap;gap:8px">
          <div id="torrent-page-btns" style="display:flex;gap:4px;align-items:center"></div>
          <div id="torrent-page-info" style="font-size:12px;color:var(--text3)"></div>
        </div>
      </div>
    </div>
'''
index = index[:downloads_start] + canonical_downloads + index[downloads_end:]
for old, new in (
    ('/app.js?v=16', '/app.js?v=17'),
    ('/operator-title.js?v=23', '/operator-title.js?v=24'),
    ('/ui-downloads-runtime.js?v=23', '/ui-downloads-runtime.js?v=24'),
    ('/style-v11.css?v=24', '/style-v11.css?v=25'),
):
    assert old in index, old
    index = index.replace(old, new, 1)
index_path.write_text(index, encoding='utf-8')

# app.js: remove startup/debug presentation and unused health-bar writer while
# preserving explicit recovery behavior and startup retry semantics.
app_path = static / 'app.js'
app = app_path.read_text(encoding='utf-8')
health_start = app.index('// ── System Health Bar (Dashboard)')
health_end = app.index('// ── Drag & Drop Priority Reordering', health_start)
health_block = app[health_start:health_end]
assert 'function updateHealthBar()' in health_block
assert "'/recovery/run'" in health_block
app = app[:health_start] + app[health_end:]
helper_start = app.index('  // ── Debug helper — shows status in UI (removed in production)')
helper_end = app.index("\n\n  dbg('Script gestartet');", helper_start)
helper = app[helper_start:helper_end]
assert 'function dbg(msg)' in helper and 'debug-status' in helper
app = app[:helper_start] + app[helper_end + 2:]
app, dbg_calls = re.subn(r'(?ms)^[ \t]+dbg\(.*?\);\n', '', app)
assert dbg_calls >= 8, dbg_calls
success_debug_pattern = re.compile(
    r"  if \(statsLoaded\) \{\s*"
    r"setTimeout\(\(\) => \{\s*"
    r"const el =\s*document\.getElementById\(\s*'debug-status'\s*\);\s*"
    r"if \(el\) \{\s*el\.style\.display = 'none';\s*\}\s*"
    r"\}, 5000\);\s*"
    r"\} else \{",
    re.S,
)
app, success_blocks = success_debug_pattern.subn('  if (!statsLoaded) {', app, count=1)
assert success_blocks == 1, success_blocks
for fragment in ('debug-status', 'function dbg(', 'dbg(', 'function updateHealthBar('):
    assert fragment not in app, fragment
assert 'function runRecovery(' in app
assert app.count("'/recovery/run'") >= 1
app_path.write_text(app, encoding='utf-8')

# Downloads runtime: dynamic icon/pending decoration only for the static bar.
runtime_path = static / 'ui-downloads-runtime.js'
runtime = runtime_path.read_text(encoding='utf-8')
bulk_start = runtime.index('  function decorateBulkSelectionToolbar() {')
bulk_end = runtime.index('  function filterStatusFromTab(tab) {', bulk_start)
runtime = runtime[:bulk_start] + '''  function decorateBulkSelectionToolbar() {
    const bar = document.getElementById('bulk-bar');
    const count = document.getElementById('bulk-count');
    if (!bar || !count) return;
    bar.dataset.dpDownloadsBulk = '1';
    syncBulkButtonPresentation(bar);
  }

''' + runtime[bulk_end:]
old_copy = "  function trackedCopy(count) {\n    return count === 1 ? '1 download tracked' : count + ' downloads tracked';\n  }"
assert old_copy in runtime
runtime = runtime.replace(old_copy, "  function trackedCopy(count) {\n    return count === 1\n      ? '1 download tracked. It followed instructions.'\n      : count + ' downloads tracked. Most of them followed instructions.';\n  }", 1)
old_aria = "title.setAttribute('aria-label', 'All Downloads. ' + copy + '.');"
assert runtime.count(old_aria) == 2, runtime.count(old_aria)
runtime = runtime.replace(old_aria, "title.setAttribute('aria-label', 'Download Queue. ' + copy);", 2)
old_heading = "'<span class=\"dp-downloads-heading\">All Downloads</span>'"
assert runtime.count(old_heading) == 1
runtime = runtime.replace(old_heading, "'<span class=\"dp-downloads-heading\">Download Queue</span>'", 1)
old_structure = '''    const tableWrap = card.querySelector('div[style*="overflow-x:auto"]');
    if (tableWrap) tableWrap.classList.add('dp-downloads-table-wrap');

    const pageSize = document.getElementById('torrent-page-size');
    if (pageSize) {
      const wrapper = pageSize.closest('div');
      if (wrapper && wrapper.parentElement?.id === 'torrent-pagination') wrapper.remove();
      else pageSize.remove();
    }
'''
assert old_structure in runtime
runtime = runtime.replace(old_structure, "    const tableWrap = card.querySelector('.dp-downloads-table-wrap');\n    if (!tableWrap) throw new Error('Canonical Downloads table viewport is unavailable');\n", 1)
for fragment in ("bar.replaceChildren(header)", "bar.classList.add('dp-card'", 'insertBefore(bar'):
    assert fragment not in runtime, fragment
runtime_path.write_text(runtime, encoding='utf-8')

# One canonical CSS section; no appended integrated correction block.
css_path = static / 'ui-downloads-page.css'
css = css_path.read_text(encoding='utf-8')
section_start = css.index('/* ── Contextual multi-selection toolbar')
section_end = css.index('/* ── Search band', section_start)
canonical_css = '''/* ── Contextual multi-selection toolbar ─────────────────────────────── */
/* Native band inside the Downloads card, between Search and the table. */
body.dp-v11-structural #view-torrents #bulk-bar.dp-downloads-bulk-card {
  display: none;
  flex: 0 0 auto;
  margin: 0 !important;
  padding: 0 !important;
  gap: 0 !important;
  border: 0 !important;
  border-bottom: 1px solid var(--dp-divider) !important;
  border-radius: 0 !important;
  box-shadow: none !important;
  overflow: visible;
  color: var(--dp-text-primary) !important;
  font-size: inherit !important;
  font-weight: inherit !important;
}
body.dp-v11-structural #view-torrents #bulk-bar.dp-downloads-bulk-card.visible {
  display: block !important;
}
body.dp-v11-structural #view-torrents .dp-downloads-bulk-toolbar {
  width: 100%;
  min-height: 54px;
  justify-content: space-between;
  gap: 16px;
  border-radius: 0 !important;
}
body.dp-v11-structural #view-torrents .dp-downloads-bulk-actions {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}
body.dp-v11-structural #view-torrents .dp-downloads-bulk-separator {
  width: 1px;
  height: 24px;
  flex: 0 0 1px;
  margin: 0 3px;
  background: var(--dp-divider);
}
body.dp-v11-structural #view-torrents .dp-downloads-bulk-action {
  min-height: 36px !important;
  height: 36px !important;
  padding: 0 12px !important;
  border-radius: 8px !important;
  font-size: 11.5px !important;
  line-height: 1 !important;
  font-weight: 600 !important;
}
body.dp-v11-structural #view-torrents .dp-downloads-bulk-action .dp-utility-icon {
  width: 16px;
  height: 16px;
  stroke-width: 2;
}
body.dp-v11-structural #view-torrents .dp-downloads-bulk-action--clear {
  color: var(--dp-text-secondary);
}
body.dp-v11-structural #view-torrents .dp-downloads-bulk-status {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-left: auto;
  padding-left: 12px;
  color: var(--dp-text-muted);
  font-size: 12px;
  line-height: 1;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

'''
css = css[:section_start] + canonical_css + css[section_end:]
appended = '\n\n/* Canonical integrated multi-selection strip. */'
assert appended in css
css = css[:css.index(appended)] + '\n'
assert 'Canonical integrated multi-selection strip' not in css
css_path.write_text(css, encoding='utf-8')

# Remove obsolete duplicate dynamic Downloads loader; index.html is authoritative.
operator_path = static / 'operator-title.js'
operator = operator_path.read_text(encoding='utf-8')
loader_marker = "\n(function () {\n  'use strict';\n  if (document.querySelector('script[data-dp-downloads-runtime]')) return;"
loader_start = operator.rfind(loader_marker)
assert loader_start >= 0
loader = operator[loader_start:]
assert "script.src = '/ui-downloads-runtime.js?v=22';" in loader
operator = operator[:loader_start].rstrip() + '\n'
assert 'data-dp-downloads-runtime' not in operator
operator_path.write_text(operator, encoding='utf-8')

style_path = static / 'style-v11.css'
style = style_path.read_text(encoding='utf-8')
assert '/ui-downloads-page.css?v=27' in style
style = style.replace('/ui-downloads-page.css?v=27', '/ui-downloads-page.css?v=28', 1)
style_path.write_text(style, encoding='utf-8')

# Migrate stale tests from pre-final layered ownership to final owners.
test_path = root / 'backend' / 'tests' / 'test_ui_downloads_correction_batch_contract.py'
test = test_path.read_text(encoding='utf-8')
fn_start = test.index('def test_bulk_selection_is_header_only_card_with_reviewed_action_order() -> None:')
fn_end = test.index('\n\n\ndef test_batch_cache_generations_are_explicit()', fn_start)
new_test = '''def test_bulk_selection_is_integrated_static_band_with_reviewed_action_order() -> None:
    runtime = read("ui-downloads-runtime.js")
    icons = read("operator-title.js")
    page = read("ui-downloads-page.css")
    transfer = read("ui-transfer-contract.css")
    index = read("index.html")
    downloads = index[index.index('id="view-torrents"'):index.index('<!-- Events -->')]
    assert 'class="bulk-bar dp-downloads-bulk-card dp-downloads-bulk-integrated" id="bulk-bar"' in downloads
    assert downloads.index('id="torrent-search"') < downloads.index('id="bulk-bar"') < downloads.index('class="dp-downloads-table-wrap"')
    assert downloads.index("bulkAction('pause',this)") < downloads.index("bulkAction('resume',this)") < downloads.index("bulkAction('reset',this)") < downloads.index("bulkAction('delete',this)")
    assert 'class="dp-downloads-bulk-status"' in downloads
    assert 'id="bulk-count" class="dp-downloads-bulk-count"' in downloads
    assert 'onclick="clearSelection()">Clear Selections</button>' in downloads
    bulk_owner = runtime[runtime.index('function decorateBulkSelectionToolbar'):runtime.index('function filterStatusFromTab')]
    assert "bar.replaceChildren(header)" not in runtime
    assert "bar.classList.add('dp-card'" not in runtime
    assert "insertBefore(bar" not in runtime
    assert "document.createElement('div')" not in bulk_owner
    for icon_name in ("pause:", "play:", "refresh:", "trash2:", "x:"):
        assert icon_name in icons
    for usage in ("'Pause', 'pause'", "'Resume', 'play'", "'Reset', 'refresh'", "'Delete', 'trash2'", "'Clear Selections', 'x'"):
        assert usage in runtime
    assert "const paths =" not in runtime
    assert "#bulk-bar.dp-downloads-bulk-card.visible" in page
    assert "Canonical integrated multi-selection strip" not in page
    assert "dp-downloads-bulk-separator" in page
    assert "dp-downloads-bulk-status" in page
    assert "dp-downloads-bulk-action--pause" in transfer
    assert "dp-downloads-bulk-action--resume" in transfer
    assert "dp-downloads-bulk-action--reset" in transfer
'''
test = test[:fn_start] + new_test + test[fn_end:]
test = test.replace('assert "/style-v11.css?v=24" in index', 'assert "/style-v11.css?v=25" in index')
test = test.replace('assert "/ui-downloads-page.css?v=27" in style', 'assert "/ui-downloads-page.css?v=28" in style')
test = test.replace('assert "/ui-downloads-runtime.js?v=22" in operator', 'assert "data-dp-downloads-runtime" not in operator\n    assert "/ui-downloads-runtime.js?v=24" in index')
test = test.replace('assert "/operator-title.js?v=23" in index', 'assert "/operator-title.js?v=24" in index')
test_path.write_text(test, encoding='utf-8')

canonical_test_path = root / 'backend' / 'tests' / 'test_v1111_canonical_frontend_contract.py'
canonical_test = canonical_test_path.read_text(encoding='utf-8')
insertion = '''

def test_downloads_static_owner_is_the_accepted_integrated_composition() -> None:
    html = read(INDEX)
    source = read(DOWNLOADS)
    page_css = read(STATIC / "ui-downloads-page.css")
    operator = read(STATIC / "operator-title.js")
    view = html[html.index('id="view-torrents"'):html.index('<!-- Events -->')]
    assert 'Download Queue' in view
    assert 'data-dp-filter-contract="desktop-v24"' in view
    assert 'class="bulk-bar dp-downloads-bulk-card dp-downloads-bulk-integrated" id="bulk-bar"' in view
    assert view.index('id="torrent-search"') < view.index('id="bulk-bar"') < view.index('class="dp-downloads-table-wrap"')
    assert 'id="torrent-page-size"' not in view
    assert 'Most of them followed instructions.' in source
    assert "bar.replaceChildren(header)" not in source
    assert "insertBefore(bar" not in source
    assert "bar.classList.add('dp-card'" not in source
    assert "Canonical integrated multi-selection strip" not in page_css
    assert "data-dp-downloads-runtime" not in operator
    assert '/ui-downloads-runtime.js?v=24' in html


def test_dashboard_has_no_inherited_startup_status_surface_or_writer() -> None:
    html = read(INDEX)
    app = read(APP)
    for fragment in ('debug-status', 'dash-health-bar', 'dash-health-recovery', 'dash-health-deadlock', 'dash-health-aging'):
        assert fragment not in html
        assert fragment not in app
    assert 'function dbg(' not in app
    assert 'dbg(' not in app
    assert 'function updateHealthBar(' not in app
    assert 'function runRecovery(' in app
    assert "'/recovery/run'" in app
'''
anchor = '\ndef test_release_surfaces_follow_v1111_without_advancing_production_state() -> None:'
assert anchor in canonical_test
canonical_test = canonical_test.replace(anchor, insertion + anchor, 1)
canonical_test_path.write_text(canonical_test, encoding='utf-8')

# Final guarded invariants.
index = index_path.read_text(encoding='utf-8')
runtime = runtime_path.read_text(encoding='utf-8')
app = app_path.read_text(encoding='utf-8')
assert index.count('id="bulk-bar"') == 1
assert index.index('id="torrent-search"') < index.index('id="bulk-bar"') < index.index('class="dp-downloads-table-wrap"')
assert 'debug-status' not in index and 'dash-health-bar' not in index
assert 'debug-status' not in app and 'dash-health-bar' not in app
assert 'insertBefore(bar' not in runtime
