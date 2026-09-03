from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'frontend' / 'static'
TESTS = ROOT / 'backend' / 'tests'


def read(path):
    return path.read_text(encoding='utf-8')


def write(path, content):
    path.write_text(content, encoding='utf-8')


def replace_once(text, old, new, label):
    if old not in text:
        raise RuntimeError(f'missing replacement anchor: {label}')
    if text.count(old) != 1:
        raise RuntimeError(f'non-unique replacement anchor: {label} ({text.count(old)})')
    return text.replace(old, new, 1)


def svg(name, geometry, extra=''):
    cls = 'lucide dp-utility-icon' + (f' {extra}' if extra else '')
    return f'<svg class="{cls}" data-dp-lucide="{name}" xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">{geometry}</svg>'

ICONS = {
    'dashboard': '<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/>',
    'download': '<path d="M12 15V3"/><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/>',
    'logs': '<path d="M3 5h1"/><path d="M3 12h1"/><path d="M3 19h1"/><path d="M8 5h1"/><path d="M8 12h1"/><path d="M8 19h1"/><path d="M13 5h8"/><path d="M13 12h8"/><path d="M13 19h8"/>',
    'statistics': '<path d="M5 21v-6"/><path d="M12 21V9"/><path d="M19 21V3"/>',
    'settings': '<path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"/><circle cx="12" cy="12" r="3"/>',
    'help': '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
    'menu': '<path d="M4 5h16"/><path d="M4 12h16"/><path d="M4 19h16"/>',
    'moon': '<path d="M20.985 12.486a9 9 0 1 1-9.473-9.472c.405-.022.617.46.402.803a6 6 0 0 0 8.268 8.268c.344-.215.825-.004.803.401"/>',
    'refresh': '<path d="M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5"/><path d="M4 13a8.1 8.1 0 0 0 15.5 2M20 20v-5h-5"/>',
    'upload': '<path d="M12 16V4"/><path d="m7 9 5-5 5 5"/><path d="M5 20h14"/>',
    'arrowRight': '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
    'chevronDown': '<path d="m6 9 6 6 6-6"/>',
    'pause': '<rect x="14" y="3" width="5" height="18" rx="1"/><rect x="5" y="3" width="5" height="18" rx="1"/>',
    'play': '<path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"/>',
    'trash2': '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="m19 6-1 14H6L5 6"/><path d="M10 11v5"/><path d="M14 11v5"/>',
    'x': '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
}

# ---------------------------------------------------------------------------
# index.html: static shell/page markup owns its final structure.
# ---------------------------------------------------------------------------
index_path = STATIC / 'index.html'
index = read(index_path)
index = index.replace('<html lang="en">', '<html lang="en" data-dp-ui="v1.0.12-canonical">', 1)

nav_icons = {
    'dashboard': 'dashboard', 'torrents': 'download', 'events': 'logs',
    'stats': 'statistics', 'settings': 'settings', 'help': 'help',
}
for view, icon in nav_icons.items():
    pattern = re.compile(r'(<div class="nav-item(?: active)?" data-view="' + re.escape(view) + r'" onclick="nav\(this\)">\s*)<span class="icon">.*?</span>', re.S)
    index, count = pattern.subn(r'\1<span class="icon">' + svg(icon, ICONS[icon]) + '</span>', index, count=1)
    if count != 1:
        raise RuntimeError(f'could not canonicalize nav icon: {view}')

old_theme = '''    <div class="sidebar-theme-control">
      <button type="button" class="theme-toggle" id="theme-toggle" onclick="toggleTheme()"
              title="Switch to light mode" aria-label="Switch to light mode">&#x263E;&#xFE0E;</button>
    </div>
'''
index = replace_once(index, old_theme, '', 'sidebar theme control')
index = replace_once(
    index,
    '<button class="mobile-menu-btn" id="mobile-menu-btn" onclick="toggleSidebar()" aria-label="Menu">☰</button>',
    '<button class="mobile-menu-btn" id="mobile-menu-btn" onclick="toggleSidebar()" aria-label="Menu">' + svg('menu', ICONS['menu']) + '</button>',
    'mobile menu icon',
)
index = replace_once(
    index,
    '    <div id="topbar-actions"></div>\n',
    '    <div id="topbar-actions"></div>\n'
    '    <div class="sidebar-theme-control topbar-theme-control">\n'
    '      <button type="button" class="theme-toggle" id="theme-toggle" onclick="toggleTheme()" title="Switch to light mode" aria-label="Switch to light mode">' + svg('moon', ICONS['moon']) + '</button>\n'
    '    </div>\n',
    'topbar theme control',
)
index = replace_once(index, '<span aria-hidden="true">&#9660;</span><span id="aria2-badge-limit">Unlimited</span>', '<span class="dp-speedcap-arrow" aria-hidden="true">' + svg('chevronDown', ICONS['chevronDown']) + '</span><span id="aria2-badge-limit">Unlimited</span>', 'speed cap chevron')

hero = {
    '📦': 'card-download.svg', '✅': 'card-checkmark.svg', '⬇': 'card-play.svg',
    '⚙': 'card-clock.svg', '⚠': 'card-error.svg', '💾': 'card-disk.svg',
}
for glyph, filename in hero.items():
    index = replace_once(index, f'<div class="dhs-icon">{glyph}</div>', f'<div class="dhs-icon"><img src="/icons/dp/{filename}" alt="" aria-hidden="true" class="dp-icon dp-icon--metric"></div>', f'hero {filename}')

old_quick = '''      <div class="card" style="margin-bottom:14px">
        <div class="card-header">
          <div style="display:flex;align-items:baseline;column-gap:14px;row-gap:2px;flex-wrap:wrap;min-width:0">
            <span class="card-title">⬇️ Add Links, Magnets, or Torrent File</span>
            <span style="font-size:11px;font-weight:400;color:var(--text3)">One item per line · Empty + Add opens a .torrent file</span>
          </div>
          <div style="display:flex;gap:6px;margin-left:auto">
            <button class="btn btn-ghost btn-sm" id="btn-import-existing" onclick="importExisting(this)" title="Import all AllDebrid magnets not yet in the local database">⬇ Import</button>
            <button class="btn btn-warn btn-sm" id="btn-recover-all" onclick="recoverAll(this)" title="Reset stuck or errored torrents — re-dispatches from AllDebrid">⟳ Recover All</button>
          </div>
        </div>'''
new_quick = '''      <div class="card dp-dashboard-quick-add" style="margin-bottom:14px">
        <div class="card-header">
          <div class="card-title" data-dp-structural-title="1">
            <img src="/icons/dp/card-link.svg" alt="" aria-hidden="true" class="dp-icon dp-icon--lg">
            <span class="dp-card-heading-copy"><span>Quick Add</span><span class="dp-card-subtitle">Add links, magnets, or torrent files to the queue.</span></span>
          </div>
          <div class="dp-card-header-actions">
            <button class="btn btn-ghost btn-sm" id="btn-import-existing" data-default-label="Import" onclick="importExisting(this)" title="Import all AllDebrid magnets not yet in the local database">''' + svg('upload', ICONS['upload']) + '''<span>Import</span></button>
            <button class="btn btn-warn btn-sm" id="btn-recover-all" data-default-label="Recover All" onclick="recoverAll(this)" title="Reset stuck or errored torrents — re-dispatches from AllDebrid">''' + svg('refresh', ICONS['refresh']) + '''<span>Recover All</span></button>
          </div>
        </div>'''
index = replace_once(index, old_quick, new_quick, 'quick add canonical structure')

old_recent_title = '''      <div class="card" id="dash-activity-card">
        <div class="card-header">
          <span class="card-title">Recent Activity</span>
          <div style="display:flex;align-items:center;gap:8px">
            <span id="dash-activity-count" style="font-size:11px;color:var(--text3)"></span>
            <button class="btn btn-ghost btn-sm" onclick="nav(document.querySelector('[data-view=torrents]'))">View All →</button>
          </div>
        </div>'''
new_recent_title = '''      <div class="card dp-dashboard-activity" id="dash-activity-card">
        <div class="card-header">
          <span class="card-title" data-dp-structural-title="1"><img src="/icons/dp/card-document-stack.svg" alt="" aria-hidden="true" class="dp-icon dp-icon--lg"><span class="dp-card-heading-copy"><span>Recent Activity</span><span id="dash-activity-count" style="font-size:11px;color:var(--text3)">Recent transfer history</span></span></span>
          <div style="display:flex;align-items:center;gap:8px">
            <button class="btn btn-ghost btn-sm" onclick="nav(document.querySelector('[data-view=torrents]'))">View All ''' + svg('arrowRight', ICONS['arrowRight']) + '''</button>
          </div>
        </div>'''
index = replace_once(index, old_recent_title, new_recent_title, 'recent activity canonical header')

old_download_title = '<span class="card-title" id="torrent-card-title">Download Queue</span>'
new_download_title = '<span class="card-title dp-downloads-card-title" id="torrent-card-title" aria-label="Download Queue. 0 downloads tracked. Most of them followed instructions."><img class="dp-icon dp-icon--lg dp-downloads-title-icon" src="/icons/dp/card-download.svg?v=11" alt="" aria-hidden="true"><span class="dp-downloads-heading-copy"><span class="dp-downloads-heading">Download Queue</span><span class="dp-downloads-subtitle">0 downloads tracked. Most of them followed instructions.</span></span></span>'
index = replace_once(index, old_download_title, new_download_title, 'downloads canonical header')
index = replace_once(index, '<button class="btn btn-ghost btn-sm dp-downloads-refresh" onclick="loadTorrents()" aria-label="Refresh downloads" title="Refresh downloads">Refresh</button>', '<button class="btn btn-ghost btn-sm dp-downloads-refresh" data-default-label="Refresh" onclick="loadTorrents()" aria-label="Refresh downloads" title="Refresh downloads">' + svg('refresh', ICONS['refresh']) + '<span>Refresh</span></button>', 'downloads refresh')

bulk_replacements = {
    '<button class="btn dp-downloads-bulk-action dp-downloads-bulk-action--pause" onclick="bulkAction(\'pause\',this)">Pause</button>': '<button class="btn dp-downloads-bulk-action dp-downloads-bulk-action--pause" data-default-label="Pause" onclick="bulkAction(\'pause\',this)">' + svg('pause', ICONS['pause']) + '<span>Pause</span></button>',
    '<button class="btn dp-downloads-bulk-action dp-downloads-bulk-action--resume" onclick="bulkAction(\'resume\',this)">Resume</button>': '<button class="btn dp-downloads-bulk-action dp-downloads-bulk-action--resume" data-default-label="Resume" onclick="bulkAction(\'resume\',this)">' + svg('play', ICONS['play']) + '<span>Resume</span></button>',
    '<button class="btn dp-downloads-bulk-action dp-downloads-bulk-action--reset" onclick="bulkAction(\'reset\',this)">Reset</button>': '<button class="btn dp-downloads-bulk-action dp-downloads-bulk-action--reset" data-default-label="Reset" onclick="bulkAction(\'reset\',this)">' + svg('refresh', ICONS['refresh']) + '<span>Reset</span></button>',
    '<button class="btn btn-danger dp-downloads-bulk-action dp-downloads-bulk-action--delete" onclick="bulkAction(\'delete\',this)">Delete</button>': '<button class="btn btn-danger dp-downloads-bulk-action dp-downloads-bulk-action--delete" data-default-label="Delete" onclick="bulkAction(\'delete\',this)">' + svg('trash2', ICONS['trash2']) + '<span>Delete</span></button>',
    '<button class="btn dp-downloads-bulk-action dp-downloads-bulk-action--clear" onclick="clearSelection()">Clear Selections</button>': '<button class="btn dp-downloads-bulk-action dp-downloads-bulk-action--clear" data-default-label="Clear Selections" onclick="clearSelection()">' + svg('x', ICONS['x']) + '<span>Clear Selections</span></button>',
}
for old, new in bulk_replacements.items():
    index = replace_once(index, old, new, 'bulk action')

old_events = '''    <div class="view" id="view-events">
      <div class="card">
        <div class="card-header">
          <span class="card-title">Event Log</span>
          <button class="btn btn-ghost btn-sm" onclick="loadEvents()">↻ Refresh</button>
        </div>
        <div style="padding:10px 14px 0">
          <div class="ev-search-row">
            <input class="input" id="ev-search" placeholder="Search by torrent name or message…" oninput="filterEvents()"/>
            <select class="input" id="ev-level" onchange="filterEvents()" style="max-width:120px">'''
new_events = '''    <div class="view" id="view-events">
      <div class="card dp-activity-card">
        <div class="card-header">
          <span class="card-title dp-activity-card-title" data-dp-structural-title="1"><img src="/icons/dp/document.svg" alt="" aria-hidden="true" class="dp-icon dp-activity-title-icon"><span class="dp-activity-heading-copy"><span class="dp-activity-heading">Activity Log</span><span class="dp-activity-subtitle">Everything DebridPulse thought was worth mentioning.</span></span></span>
          <button class="btn btn-ghost btn-sm dp-activity-refresh" data-default-label="Refresh" onclick="loadEvents()" aria-label="Refresh activity log" title="Refresh activity log">''' + svg('refresh', ICONS['refresh']) + '''<span>Refresh</span></button>
        </div>
        <div class="dp-activity-search-band">
          <div class="ev-search-row dp-activity-search-row">
            <input class="input" id="ev-search" placeholder="Search by torrent name or message…" oninput="filterEvents()"/>
            <select class="input" id="ev-level" onchange="filterEvents()">'''
index = replace_once(index, old_events, new_events, 'activity canonical structure')
index = replace_once(index, '<div id="event-list"><div class="empty">Loading…</div></div>', '<div id="event-list" class="dp-activity-list"><div class="empty">Loading…</div></div>', 'activity list class')

# Correction runtimes are retired; canonical icon owner must load before app.js.
index = index.replace('<script src="/app.js?v=18" defer></script>\n<script src="/operator-title.js?v=24" defer></script>\n<script src="/ui-runtime.js?v=26" defer data-dp-ui-runtime="1"></script>\n<script src="/ui-downloads-runtime.js?v=24" defer data-dp-downloads-runtime="1"></script>', '<script src="/operator-title.js?v=25" defer></script>\n<script src="/app.js?v=19" defer></script>')
if 'ui-runtime.js' in index or 'ui-downloads-runtime.js' in index:
    raise RuntimeError('correction runtime import remains in index')
write(index_path, index)

# ---------------------------------------------------------------------------
# operator-title.js: retain only canonical icon/toast/status helpers.
# ---------------------------------------------------------------------------
icons_path = STATIC / 'operator-title.js'
icons_source = read(icons_path)
marker = '  function decorateNavigation() {'
pos = icons_source.find(marker)
if pos < 0:
    raise RuntimeError('operator-title decorator marker missing')
icons_source = icons_source[:pos] + '''  function renderThemeGlyph(isLight) {
    const button = document.getElementById('theme-toggle');
    if (!button) return;
    button.innerHTML = lucideSvg(isLight ? 'sun' : 'moon');
  }
})();
'''
if 'MutationObserver' in icons_source or 'ui-runtime.js' in icons_source or "createElement('script')" in icons_source:
    raise RuntimeError('operator-title correction loader/observer survived')
write(icons_path, icons_source)

# ---------------------------------------------------------------------------
# app.js: direct shell and Downloads ownership.
# ---------------------------------------------------------------------------
app_path = STATIC / 'app.js'
app = read(app_path)

old_titles = '''  const titles = {
    dashboard:'Dashboard',
    torrents:'Downloads',
    events:'Event Log',
    stats:'Statistics',
    settings:'Settings',
    help:'Help & License',
  };
  document.getElementById('page-title').textContent = titles[v] || v;
  document.dispatchEvent(new CustomEvent('debridpulse:navigation', {detail:{view:v,title:titles[v]||v}}));'''
new_titles = '''  const titles = {
    dashboard:'Dashboard', torrents:'Downloads', events:'Event Log',
    stats:'Statistics', settings:'Settings', help:'Help & License',
  };
  const subtitles = {
    dashboard:'Overview of your download activities and system status.',
    torrents:'Inspect, filter, and control queued and active transfers.',
    events:'Recent transfer activity, decisions, warnings, and errors.',
    stats:'Historical transfer performance and completion metrics.',
    settings:'Configure providers, downloads, notifications, and system behavior.',
    help:'Usage guidance, project information, and licensing.',
  };
  document.getElementById('page-title').textContent = titles[v] || v;
  const subtitle = document.getElementById('page-subtitle');
  if (subtitle) subtitle.textContent = subtitles[v] || '';
  document.dispatchEvent(new CustomEvent('debridpulse:navigation', {detail:{view:v,title:titles[v]||v}}));'''
app = replace_once(app, old_titles, new_titles, 'nav subtitle ownership')

old_topbar_html = '''    el.innerHTML = `
      <button id="btn-resume-all" class="btn btn-primary" onclick="resumeProcessing()" style="display:none">Resume All</button>
      <button id="btn-resume-paused" class="btn btn-primary" onclick="resumePausedDownloads()" style="display:none">Resume Paused</button>
      <button id="btn-pause-all" class="btn btn-ghost" onclick="pauseProcessing()">Pause All</button>
    `;'''
new_topbar_html = '''    const icon = (name) => window.DPIcons && typeof window.DPIcons.svg === 'function' ? window.DPIcons.svg(name) : '';
    el.innerHTML = `
      <button id="btn-resume-all" class="btn btn-primary" data-default-label="Resume All" onclick="resumeProcessing()" style="display:none">${icon('play')}<span>Resume All</span></button>
      <button id="btn-resume-paused" class="btn btn-primary" data-default-label="Resume Paused" onclick="resumePausedDownloads()" style="display:none">${icon('play')}<span>Resume Paused</span></button>
      <button id="btn-pause-all" class="btn btn-ghost" data-default-label="Pause All" onclick="pauseProcessing()">${icon('pause')}<span>Pause All</span></button>
    `;'''
app = replace_once(app, old_topbar_html, new_topbar_html, 'topbar direct icon rendering')

# renderTopbarActions must preserve the icon while changing Resume Paused count.
app = replace_once(app, '''    const label = `Resume Paused (${selectivelyPaused})`;
    resumePausedBtn.dataset.defaultLabel = label;

    if (resumePausedBtn.dataset.pending !== '1') {
      resumePausedBtn.textContent = label;
    }''', '''    const label = `Resume Paused (${selectivelyPaused})`;
    resumePausedBtn.dataset.defaultLabel = label;

    if (resumePausedBtn.dataset.pending !== '1') {
      const copy = resumePausedBtn.querySelector('span:last-child');
      if (copy) copy.textContent = label;
      else resumePausedBtn.textContent = label;
    }''', 'resume paused label')

# Recent Activity final count is owned where rows are rendered.
app = replace_once(app, "if (countEl) countEl.textContent = items.length + ' most recent';", "if (countEl) countEl.textContent = items.length + ' most recent download' + (items.length === 1 ? '' : 's');", 'recent count copy')
app = replace_once(app, '''      tb.innerHTML = '<tr><td colspan="6"><div class="empty"><div class="empty-icon">⬇️</div>No transfers yet. Add a magnet, torrent file, or debrid link to start.</div></td></tr>';
      return;''', '''      tb.innerHTML = '<tr><td colspan="6"><div class="empty"><div class="empty-icon" aria-hidden="true"></div>No downloads yet. Add a link, magnet, or torrent file to get started.</div></td></tr>';
      const countEl = document.getElementById('dash-activity-count');
      if (countEl) countEl.textContent = 'Recent transfer history';
      return;''', 'recent empty state')

# Activity rows are emitted in their final component structure, not normalized later.
old_event_render = '''  el.innerHTML = evs.map(ev=>`
    <div class="event-item">
      <div class="elevel ${esc(ev.level)}"></div>
      <div><div class="emsg">${esc(ev.message)}</div>${ev.torrent_name?`<div class="ename">${esc(ev.torrent_name)}</div>`:''}</div>
      <div class="etime">${fmtDate(ev.created_at)}</div>
    </div>`).join('');'''
new_event_render = '''  el.innerHTML = evs.map(ev=>`
    <div class="dp-activity-row">
      <div class="elevel dp-activity-level ${esc(ev.level)}"></div>
      <div class="dp-activity-copy"><div class="emsg dp-activity-message">${esc(ev.message)}</div>${ev.torrent_name?`<div class="ename dp-activity-transfer">${esc(ev.torrent_name)}</div>`:''}</div>
      <div class="etime dp-activity-time">${fmtDate(ev.created_at)}</div>
    </div>`).join('');'''
app = replace_once(app, old_event_render, new_event_render, 'activity row direct render')

# Insert canonical Downloads helpers before search input handler.
anchor = '// ── Torrents ───────────────────────────────────────────────────────────────\n\nfunction onTorrentSearchInput()'
if anchor not in app:
    raise RuntimeError('Downloads helper insertion anchor missing')
download_helpers = r'''// ── Torrents ───────────────────────────────────────────────────────────────

function activeDownloadFilterStatus() {
  return document.querySelector('#view-torrents .filter-tabs .ftab.active')?.dataset.dpStatus || '';
}

function downloadPaginationSummary(total, from, to) {
  const search = document.getElementById('torrent-search');
  if (search && search.value.trim()) {
    if (total <= 0) return 'No downloads match your search';
    if (total === 1 && from === 1 && to === 1) return 'Showing 1 matching download';
    if (from === 1 && to === total) return 'Showing all ' + total + ' matching downloads';
    return 'Showing ' + from + '–' + to + ' of ' + total + ' matching downloads';
  }
  const status = activeDownloadFilterStatus();
  const language = {
    '': ['No Items Added Yet', 'Showing 1 Added Item', n => 'Showing ' + n + ' Added Items'],
    downloading: ['No Active Downloads', '1 Active Download', n => n + ' Active Downloads'],
    paused: ['No Paused Downloads', '1 Paused Download', n => n + ' Paused Downloads'],
    processing: ['No Downloads Currently Processing', '1 Download Currently Processing', n => n + ' Downloads Currently Processing'],
    ready: ['No Downloads in Ready State', '1 Download in Ready State', n => n + ' Downloads in Ready State'],
    completed: ['No Downloads Completed Yet', '1 Download Completed', n => n + ' Downloads Completed'],
    error: ['No Downloads Have Errors', '1 Download Has Errors', n => n + ' Downloads Have Errors'],
  }[status];
  if (!language) return total === 1 ? '1 Download' : total + ' Downloads';
  return total <= 0 ? language[0] : total === 1 ? language[1] : language[2](total);
}

function renderTorrentPagination(total, limit, offset) {
  const normalizedTotal = Math.max(0, Number(total) || 0);
  const normalizedLimit = Math.max(1, Number(limit) || 25);
  const normalizedOffset = Math.max(0, Number(offset) || 0);
  const totalPages = Math.max(1, Math.ceil(normalizedTotal / normalizedLimit));
  const current = Math.min(totalPages, Math.floor(normalizedOffset / normalizedLimit) + 1);
  torrentPage = current;
  const info = document.getElementById('torrent-page-info');
  const buttons = document.getElementById('torrent-page-btns');
  if (!info || !buttons) return;
  const from = normalizedTotal === 0 ? 0 : normalizedOffset + 1;
  const to = Math.min(normalizedOffset + normalizedLimit, normalizedTotal);
  info.textContent = downloadPaginationSummary(normalizedTotal, from, to);
  const icon = name => window.DPIcons && typeof window.DPIcons.svg === 'function' ? window.DPIcons.svg(name) : '';
  const controls = [];
  if (current > 1) controls.push('<button type="button" class="dp-pager-btn" aria-label="Previous page" onclick="goToTorrentPage(' + (current - 1) + ')">' + icon('chevronLeft') + '</button>');
  controls.push('<button type="button" class="dp-pager-btn dp-pager-current" aria-current="page" aria-label="Page ' + current + ', current page">' + current + '</button>');
  if (current < totalPages) controls.push('<button type="button" class="dp-pager-btn" aria-label="Next page" onclick="goToTorrentPage(' + (current + 1) + ')">' + icon('chevronRight') + '</button>');
  buttons.innerHTML = controls.join('');
}

function setFilter(element, status) {
  document.querySelectorAll('#view-torrents .filter-tabs .ftab').forEach(tab => {
    tab.classList.remove('active');
    tab.setAttribute('aria-selected', 'false');
  });
  if (element) {
    element.classList.add('active');
    element.setAttribute('aria-selected', 'true');
  }
  currentFilter = status;
  torrentPage = 1;
  clearSelection();
  loadTorrents();
}

function updateDownloadsTrackedCopy(total) {
  const count = Math.max(0, Number(total) || 0);
  const copy = count === 1
    ? '1 download tracked. It followed instructions.'
    : count + ' downloads tracked. Most of them followed instructions.';
  const title = document.getElementById('torrent-card-title');
  const subtitle = title?.querySelector('.dp-downloads-subtitle');
  if (subtitle) subtitle.textContent = copy;
  if (title) title.setAttribute('aria-label', 'Download Queue. ' + copy);
}

function downloadEmptyMessage() {
  const search = document.getElementById('torrent-search');
  if (search && search.value.trim()) return 'No downloads match your search.';
  if (activeDownloadFilterStatus()) return 'No downloads match your current filters.';
  return 'No downloads yet. Add a link, magnet, or torrent file to get started.';
}

function onTorrentSearchInput()'''
app = app.replace(anchor, download_helpers, 1)

# Keep all-download header copy tied to generic stats, not filtered totals.
app = replace_once(app, '''      const total = Object.entries(bs)
        .filter(([status]) => status !== 'deleted')
        .reduce((sum, [, count]) => sum + (Number(count) || 0), 0);
      const completed =''', '''      const total = Object.entries(bs)
        .filter(([status]) => status !== 'deleted')
        .reduce((sum, [, count]) => sum + (Number(count) || 0), 0);
      updateDownloadsTrackedCopy(total);
      const completed =''', 'downloads tracked copy from stats')

# Direct final Downloads row rendering.
row_pattern = re.compile(r'''    if \(!items\.length\) \{\n      tb\.innerHTML = `.*?`;\n      return;\n    \}\n    tb\.innerHTML = items\.map\(t => `<tr data-torrent-id=.*?\n    </tr>`\)\.join\(''\);''', re.S)
row_replacement = r'''    clearSelection();
    if (!items.length) {
      tb.innerHTML = `<tr><td colspan="8"><div class="empty"><div class="empty-icon" aria-hidden="true"></div>${downloadEmptyMessage()}</div></td></tr>`;
      return;
    }
    const icon = name => window.DPIcons && typeof window.DPIcons.svg === 'function' ? window.DPIcons.svg(name) : '';
    tb.innerHTML = items.map(t => `<tr class="dp-downloads-detail-row" data-torrent-id="${t.id}" data-status="${esc(t.status)}" tabindex="0" onclick="if(!event.target.closest('button,input,a,select,textarea,label,[role=button]'))showDetail(${t.id})" onkeydown="if(event.target===this&&(event.key==='Enter'||event.key===' ')){event.preventDefault();showDetail(${t.id})}">
      <td onclick="event.stopPropagation()"><input type="checkbox" class="t-chk" data-id="${t.id}" onchange="onCheckboxChange()"/></td>
      <td>
        <div class="t-name">${esc(t.name)||'(unnamed)'}</div>
        <div class="t-hash">${(t.hash||'').substring(0,16)}${t.hash?'…':''}</div>
      </td>
      <td class="sz dp-downloads-provider-cell">
        ${providerChip(t)}
        <span class="dp-transfer-source-label">${sourceLabel(t.source)}</span>
        ${t.label?`<span class="lbl-badge">🏷 ${esc(t.label)}</span>`:''}
      </td>
      <td data-role="transfer-status">${badge(transferDisplayStatus(t), t)}</td>
      <td data-role="transfer-progress">${progress(t.progress,t.status)}</td>
      <td class="sz">${fmtSize(t.size_bytes)}</td>
      <td class="sz">${fmtDate(t.created_at)}</td>
      <td onclick="event.stopPropagation()">
        <div class="actions">
          ${t.status==='ready' || t.status==='pending' ? `<button class="btn btn-primary btn-sm" data-default-label="Now" onclick="event.stopPropagation();downloadNow(${t.id},this)" title="Move to front of queue">${icon('download')}<span>Now</span></button>` : ''}
          ${t.status==='downloading' || t.status==='queued' ? `<button class="btn btn-blue btn-sm" data-default-label="Pause" onclick="event.stopPropagation();pauseT(${t.id},this)">Pause</button>` : ''}
          ${t.status==='paused' ? `<button class="btn btn-blue btn-sm" data-default-label="Resume" onclick="event.stopPropagation();resumeT(${t.id},this)">Resume</button>` : ''}
          ${t.status==='error'?`<button class="btn btn-blue btn-sm" data-default-label="Retry" onclick="event.stopPropagation();retryT(${t.id},this)">Retry</button>`:''}
          <button class="btn btn-danger btn-sm" data-default-label="Remove" onclick="event.stopPropagation();deleteT(${t.id},event,this)">Remove</button>
        </div>
      </td>
    </tr>`).join('');'''
app, count = row_pattern.subn(row_replacement, app, count=1)
if count != 1:
    raise RuntimeError(f'could not replace Downloads row renderer ({count})')

write(app_path, app)

# Retire correction runtimes physically.
for retired in ('ui-runtime.js', 'ui-downloads-runtime.js'):
    path = STATIC / retired
    if not path.exists():
        raise RuntimeError(f'expected correction runtime missing before retirement: {retired}')
    path.unlink()

# ---------------------------------------------------------------------------
# Rewrite contracts that previously required the correction layers.
# ---------------------------------------------------------------------------
arch = TESTS / 'test_ui_runtime_architecture_contract.py'
source = read(arch)
source += r'''


def test_shell_and_downloads_have_direct_canonical_owners() -> None:
    index = read("index.html")
    app = read("app.js")
    icons = read("operator-title.js")
    for retired in ("ui-runtime.js", "ui-downloads-runtime.js"):
        assert not (STATIC / retired).exists()
        assert retired not in index
        assert retired not in icons
    assert "new MutationObserver" not in icons
    assert "function renderTorrentPagination(" in app
    assert "function setFilter(" in app
    assert "function updateDownloadsTrackedCopy(" in app
    assert "class=\"dp-downloads-detail-row\"" in app
    assert "draggable=\"true\"" not in app
    assert "data-dp-ui=\"v1.0.12-canonical\"" in index
'''
write(arch, source)

correction = TESTS / 'test_ui_downloads_correction_batch_contract.py'
write(correction, r'''"""Canonical Dashboard / Downloads / Activity ownership contracts."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"

def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")

def test_downloads_refresh_and_bulk_controls_are_final_in_static_markup() -> None:
    index = read("index.html")
    icons = read("operator-title.js")
    assert 'class="btn btn-ghost btn-sm dp-downloads-refresh"' in index
    assert 'data-default-label="Refresh"' in index
    assert 'data-dp-lucide="refresh"' in index
    for label, icon in (("Pause", "pause"), ("Resume", "play"), ("Reset", "refresh"), ("Delete", "trash2"), ("Clear Selections", "x")):
        assert f'data-default-label="{label}"' in index
        assert f'data-dp-lucide="{icon}"' in index
    assert 'M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5' in icons

def test_bulk_selection_is_integrated_static_band_with_reviewed_action_order() -> None:
    index = read("index.html")
    downloads = index[index.index('id="view-torrents"'):index.index('<!-- Events -->')]
    assert 'class="dp-card dp-downloads-bulk-card dp-downloads-bulk-integrated" id="bulk-bar"' in downloads
    assert downloads.index('id="torrent-search"') < downloads.index('id="bulk-bar"') < downloads.index('class="dp-downloads-table-wrap"')
    assert downloads.index("bulkAction('pause',this)") < downloads.index("bulkAction('resume',this)") < downloads.index("bulkAction('reset',this)") < downloads.index("bulkAction('delete',this)")
    assert 'id="bulk-count" class="dp-downloads-bulk-count"' in downloads

def test_downloads_behavior_is_owned_directly_by_app() -> None:
    app = read("app.js")
    for fragment in (
        "function renderTorrentPagination(", "function setFilter(",
        "function updateDownloadsTrackedCopy(", "function downloadEmptyMessage(",
        "No Downloads Currently Processing", "No Downloads Completed Yet",
        'class="dp-downloads-detail-row"', 'data-default-label="Pause"',
        'data-default-label="Resume"', 'data-default-label="Retry"',
        'data-default-label="Remove"',
    ):
        assert fragment in app
    assert "draggable=\"true\"" not in app
    assert "ondragstart=" not in app

def test_correction_runtimes_are_retired() -> None:
    index = read("index.html")
    for name in ("ui-runtime.js", "ui-downloads-runtime.js"):
        assert not (STATIC / name).exists()
        assert name not in index
''')

final_contract = TESTS / 'test_ui_downloads_final_contract.py'
source = read(final_contract)
source = source.replace('runtime = read("ui-downloads-runtime.js")', 'runtime = read("app.js")')
source = source.replace('assert "row.classList.add(\'dp-downloads-detail-row\')"', 'assert \'class="dp-downloads-detail-row"\'')
source = source.replace('"row.removeAttribute(attribute)",\n        "[\'draggable\', \'ondragstart\', \'ondragover\', \'ondragleave\', \'ondrop\']",\n        "row.addEventListener(\'click\'",\n        "window.showDetail(id)",\n        "rowTargetIsInteractive",\n        "row.tabIndex = 0",', '"if(!event.target.closest(\'button,input,a,select,textarea,label,[role=button]\'))showDetail",\n        "tabindex=\\\"0\\\"",\n        "event.key===\'Enter\'",')
source = source.replace('"onclick.includes(\'pauseT(\')",\n        "onclick.includes(\'resumeT(\')",\n        "onclick.includes(\'deleteT(\')",\n        "onclick.includes(\'retryT(\')",\n        "label = \'Pause\'",\n        "label = \'Resume\'",\n        "label = \'Remove\'",\n        "label = \'Retry\'",\n        "button.dataset.defaultLabel = label",\n        "button.dataset.pending === \'1\'",\n        "button.getAttribute(\'aria-busy\') === \'true\'",', '"data-default-label=\\\"Pause\\\"",\n        "data-default-label=\\\"Resume\\\"",\n        "data-default-label=\\\"Remove\\\"",\n        "data-default-label=\\\"Retry\\\"",')
source = source.replace('for obsolete in ("label = \'⏸ Pause\'", "label = \'▶ Resume\'", "label = \'✕ Remove\'", "label = \'↻ Retry\'"):', 'for obsolete in ("⏸ Pause", "▶ Resume", "✕ Remove", "↻ Retry"):')
source = source.replace('assert "replace(/^[^A-Za-z0-9]+/" in runtime\n    assert "badge.classList.contains(\'badge-completed\') ? \'Done\'" in runtime', 'assert "statusBadge" not in runtime or "badge(transferDisplayStatus(t), t)" in runtime')
write(final_contract, source)

canonical = TESTS / 'test_v1111_canonical_frontend_contract.py'
source = read(canonical)
source = source.replace('RUNTIME = STATIC / "ui-runtime.js"\nDOWNLOADS = STATIC / "ui-downloads-runtime.js"\n', '')
source = source.replace('        script_position(html, "ui-runtime.js"),\n        script_position(html, "ui-downloads-runtime.js"),\n', '')
source = source.replace('        "function renderTorrentPagination(",\n        "function setFilter(",\n', '')
source = source.replace('    for name in retired:\n', '    retired = retired + ("ui-runtime.js", "ui-downloads-runtime.js")\n    for name in retired:\n', 1)
write(canonical, source)

# New focused ownership test: no execution-order fallback can resurrect E1 layers.
ownership = TESTS / 'test_uiarch001_e1_ownership.py'
write(ownership, r'''from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"

def read(name):
    return (STATIC / name).read_text(encoding="utf-8")

def test_e1_correction_layers_are_physically_absent_and_unreferenced():
    joined = "\n".join(path.read_text(encoding="utf-8") for path in STATIC.glob("*.js"))
    index = read("index.html")
    for retired in ("ui-runtime.js", "ui-downloads-runtime.js"):
        assert not (STATIC / retired).exists()
        assert retired not in index
        assert retired not in joined

def test_icon_owner_has_no_loader_observer_or_dom_reparenting():
    icons = read("operator-title.js")
    for forbidden in ("MutationObserver", "createElement('script')", "appendChild(script)", "bindThemeToggle", "decorateNavigation"):
        assert forbidden not in icons

def test_shell_structure_is_static_and_download_rows_are_final_at_render_time():
    index = read("index.html")
    app = read("app.js")
    assert 'data-dp-ui="v1.0.12-canonical"' in index
    assert 'topbar-theme-control' in index
    assert 'dp-dashboard-quick-add' in index
    assert 'dp-dashboard-activity' in index
    assert 'dp-activity-card' in index
    assert 'dp-downloads-card-title' in index
    assert 'class="dp-downloads-detail-row"' in app
    assert 'draggable="true"' not in app
    assert 'ondragstart=' not in app
    assert 'function renderTorrentPagination(' in app
    assert 'function setFilter(' in app
''')
