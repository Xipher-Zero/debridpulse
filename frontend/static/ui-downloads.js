/* DebridPulse — canonical Downloads controller/renderer owner
 * (DP 1.0.12 canonical flattening, CANON-001 closure).
 *
 * Sole owner of: list page state, the bounded /torrents fetch, final row
 * rendering, filtering, pagination, the Priority/Newest order, the
 * operator queue reorder (Name-cell drag, keyboard move, cross-page
 * navigation while one is held, the restart confirmation), date mode/menu,
 * measured capacity/page sizing, list selection state, bulk actions,
 * refresh, and page-specific lifecycle/event wiring. The search box and every
 * request's identity belong to the shared search
 * lifecycle (DPSearch, ui-search.js), as on the Activity Log. Every row is emitted in final form
 * by a single render pass -- this file never replaces api/loadTorrents/
 * renderTorrentPagination on another owner, and never re-renders an
 * inherited Downloads implementation and repairs its DOM afterward.
 */
(function () {
'use strict';

const DATE_PREF_KEY = 'debridpulse.downloads.date-presentation.v1';
const DATE_FORMATS = new Set(['friendly', 'us', 'international', 'iso']);
// The one canonical pause-eligible presentation-status set (also used by
// Dashboard Recent) is owned by ui-processing-presentation.js, a required
// explicit dependency that loads before this file (see index.html) --
// there is no fallback copy here. A missing owner is a visible load-order
// bug, not something to paper over with a second definition.
const PAUSEABLE_PRESENTATION = new Set(window.DPProcessingPresentation.PAUSEABLE_PRESENTATION);
// The subset of PAUSEABLE_PRESENTATION that reflects automatic recovery in
// progress -- Retry (a user-initiated action) never shows while the
// universal transfer core is already retrying on its own.
const AUTO_RECOVERING_PRESENTATION = new Set(
  ['recovering', 'waiting_for_retry', 'waiting_for_provider', 'waiting_for_storage', 'waiting_for_executor']
);

// ── List/page state ─────────────────────────────────────────────────────
let currentFilter = '';
// 'queue': the operator queue, then settled history (the server orders the
// whole collection before it cuts the page). 'newest': chronological.
let currentOrder = 'queue';
let torrentPage = 1;
let torrentPageSize = 25;
let torrentTotal = 0;
// The page on screen, and a page turn the pager asked for: only that turn
// slides (ui-motion.js); a filter, search, size, order or live refresh renders
// in place. The turn slides at once, before its page is fetched, to its
// destination drawn as a placeholder (or, already answered, as itself); the
// authoritative page then crossfades into it.
let renderedTorrentPage = 1;
let torrentPageTurn = null;   // {page, source, motion, shown, paint, ended}
const torrentTableWrap = () => document.querySelector('#view-torrents .dp-downloads-table-wrap');
function beginTorrentTurn(page) {
  const tb = document.getElementById('t-tbody');
  const turn = {page, source: tb ? tb.innerHTML : '', motion: null, shown: false, paint: null, ended: false};
  const distance = page - renderedTorrentPage;
  torrentPageTurn = turn;
  turn.motion = window.DPMotion.slide({element: torrentTableWrap(), axis: 'x', direction: Math.sign(distance), distance: Math.abs(distance)}, () => {
    if (turn.ended && !turn.paint) return;
    if (turn.paint) turn.paint();
    else { paintTorrentPlaceholder(page); turn.shown = true; }
  });
}
// The turn is over without its page being shown here: a newer intent paints
// its own answer, or the turn failed. A slide not yet begun is skipped.
function endTorrentTurn(turn) {
  if (!turn || turn.ended) return;
  turn.ended = true;
  if (torrentPageTurn === turn) torrentPageTurn = null;
  if (!turn.shown) turn.motion?.skip();
}
// Rows shaped like the destination's, nothing in them: as many as that page
// holds (the server's total and the page size say so), never more than the
// table shows at once, each the height of a row on screen, every column the
// width it is drawn now.
function paintTorrentPlaceholder(page) {
  const tb = document.getElementById('t-tbody'), wrap = torrentTableWrap();
  if (!tb) return;
  const limit = Math.min(Math.max(parseInt(torrentPageSize) || 25, 1), 100);
  const expected = Math.max(1, Math.min(limit, torrentTotal - (page - 1) * limit));
  const height = tb.querySelector('tr.dp-downloads-detail-row')?.getBoundingClientRect().height || 56;
  const head = wrap?.querySelector('thead')?.getBoundingClientRect().height || 0;
  const fits = wrap ? Math.max(1, Math.ceil((wrap.clientHeight - head) / height)) : expected;
  const widths = Array.from(wrap?.querySelectorAll('thead th') || [], th => `width:${Math.round(th.getBoundingClientRect().width)}px`);
  const cell = (index, className, shapes) => `<td${className ? ` class="${className}"` : ''} style="${widths[index] || ''}">${shapes}</td>`;
  const operable = dpCan('transfers.operate');
  const row = `<tr class="dp-page-placeholder-row" style="height:${Math.round(height)}px">
    ${cell(0, 'dp-downloads-name-cell', '<span class="dp-ph dp-ph--title"></span><span class="dp-ph dp-ph--icon"></span><span class="dp-ph dp-ph--chip"></span>')}
    ${cell(1, 'dp-downloads-status-cell', '<span class="dp-ph dp-ph--badge"></span>')}
    ${cell(2, 'dp-downloads-progress-cell', '<span class="dp-ph dp-ph--bar"></span><span class="dp-ph dp-ph--sub"></span>')}
    ${cell(3, 'sz dp-downloads-size-cell', '<span class="dp-ph dp-ph--number"></span>')}
    ${cell(4, 'sz dp-downloads-date-cell', '<span class="dp-ph dp-ph--number"></span>')}
    ${operable ? cell(5, 'dp-downloads-action-cell', '<span class="dp-ph dp-ph--button"></span>') + cell(6, 'dp-downloads-select-cell', '<span class="dp-ph dp-ph--box"></span>') : ''}
  </tr>`;
  tb.dataset.dpPlaceholder = String(expected);
  tb.setAttribute('aria-hidden', 'true');
  tb.innerHTML = row.repeat(Math.min(expected, fits));
}
// Whatever replaces a placeholder on screen crossfades into it.
function replaceTorrentRows(paint) {
  const tb = document.getElementById('t-tbody');
  const placeholder = tb && 'dpPlaceholder' in tb.dataset;
  const commit = () => {
    if (tb) { delete tb.dataset.dpPlaceholder; tb.removeAttribute('aria-hidden'); }
    paint();
  };
  if (placeholder && window.DPMotion) window.DPMotion.resolve({region: tb, host: torrentTableWrap(), content: () => [tb]}, commit);
  else commit();
}
let _selectedIds = new Set();
// Each shown row's server-given access (``access.destroy``, ``access.own_requests``), by id.
const _rowAccess = new Map();
// Every authoritative state mutation that changes the requested list
// projection (page, filter, order, page size -- including a capacity-derived
// page-size transition -- and a committed search) supersedes the requests in
// flight; each fetch takes its own ticket and a response that is no longer
// the newest is discarded rather than rendered -- an in-flight response for
// an older view state must never overwrite newer user/application intent.
let search = null;
function searchControl() {
  if (!search) {
    search = window.DPSearch.attach({
      input: document.getElementById('torrent-search'),
      noun: 'downloads',
      onCommit() { clearSelection(); torrentPage = 1; loadTorrents().catch(() => {}); },
    });
  }
  return search;
}

// ── Date presentation preference ────────────────────────────────────────
let pref = loadPref();
function loadPref() {
  try {
    const p = JSON.parse(localStorage.getItem(DATE_PREF_KEY) || '{}');
    return {format: DATE_FORMATS.has(p.format) ? p.format : 'friendly', hour12: p.hour12 === true};
  } catch (_) { return {format: 'friendly', hour12: false}; }
}
function savePref() { try { localStorage.setItem(DATE_PREF_KEY, JSON.stringify(pref)); } catch (_) {} }
function timeZone() { try { return dpDisplayTimezone(); } catch (_) { return 'UTC'; } }
function parts(date) {
  return Object.fromEntries(
    new Intl.DateTimeFormat('en-US', {timeZone: timeZone(), year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'})
      .formatToParts(date).filter(p => p.type !== 'literal').map(p => [p.type, p.value])
  );
}
function dateKey(p) { return `${p.year}-${p.month}-${p.day}`; }
function previousKey(p) {
  const d = new Date(Date.UTC(+p.year, +p.month - 1, +p.day) - 86400000);
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`;
}
// A 24-hour wall-clock reading in the preferred time style.
function clock(p) {
  if (!pref.hour12) return `${p.hour}:${p.minute}`;
  return `${String((+p.hour + 11) % 12 + 1).padStart(2, '0')}:${p.minute} ${+p.hour < 12 ? 'AM' : 'PM'}`;
}
/* One calendar reading in the preferred format. Its fields are wall-clock
 * values already in the preference's time zone: a date with or without its
 * time, a month, or a time of day -- never shifted, never given a field it
 * does not have. */
function formatCalendar(p) {
  const c = p.hour == null ? '' : clock(p);
  if (p.year == null) return c;
  const timed = date => c ? `${date} ${c}` : date;
  const now = parts(new Date());
  const month = new Intl.DateTimeFormat('en-US', {timeZone: 'UTC', month: 'short'}).format(Date.UTC(+p.year, +p.month - 1, 1));
  if (p.day == null) {
    if (pref.format === 'iso') return `${p.year}-${p.month}`;
    if (pref.format === 'us' || pref.format === 'international') return `${p.month}/${p.year}`;
    return +p.year === +now.year ? month : `${month} ${p.year}`;
  }
  if (pref.format === 'us') return timed(`${p.month}/${p.day}/${p.year}`);
  if (pref.format === 'international') return timed(`${p.day}/${p.month}/${p.year}`);
  if (pref.format === 'iso') return timed(`${p.year}-${p.month}-${p.day}`);
  if (dateKey(p) === dateKey(now)) return timed('Today');
  if (dateKey(p) === previousKey(now)) return timed('Yesterday');
  return timed(+p.year === +now.year ? `${month} ${+p.day}` : `${month} ${+p.day}, ${p.year}`);
}
function formatDownloadsDate(value) {
  if (!value) return '—';
  const d = typeof parseApiDate === 'function' ? parseApiDate(value) : new Date(value);
  if (!d || Number.isNaN(d.getTime())) return String(value);
  return formatCalendar(parts(d));
}
// An instant's calendar day alone, in the same preference and time zone.
function formatDownloadsDay(date) {
  if (!(date instanceof Date) || Number.isNaN(date.getTime())) return '';
  const {year, month, day} = parts(date);
  return formatCalendar({year, month, day});
}
function exactDate(value) {
  if (!value) return '';
  const d = typeof parseApiDate === 'function' ? parseApiDate(value) : new Date(value);
  if (!d || Number.isNaN(d.getTime())) return String(value);
  return new Intl.DateTimeFormat('en-US', {timeZone: timeZone(), year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short', hour12: false}).format(d);
}
function dateMarkup(value) {
  return `<span class="dp-downloads-date-value" tabindex="0" data-tooltip="${esc(exactDate(value))}">${esc(formatDownloadsDate(value))}</span>`;
}

/* Every list whose date column presents formatDownloadsDate: Downloads and
 * Dashboard Recent Activity. Each heading carries the ONE menu below, writing
 * the ONE preference above, so a choice in either list re-renders both. */
const DATE_HEADINGS = Object.freeze([
  ['#view-torrents .dp-downloads-table-wrap table', ['Added', 'Date'], 'Date'],
  ['#view-dashboard .dash-activity-table-wrap table', ['Date Added'], 'Date Added'],
]);
const dateMenuSyncs = [];
function ensureDateMenus() { DATE_HEADINGS.forEach(heading => ensureDateMenu(...heading)); }
function ensureDateMenu(tableSelector, headings, text) {
  const table = document.querySelector(tableSelector);
  const heading = table ? Array.from(table.querySelectorAll('thead th')).find(n => headings.includes(n.textContent.trim())) : null;
  if (!heading || heading.querySelector('.dp-date-menu-trigger')) return;
  heading.textContent = '';
  const label = document.createElement('span');
  label.textContent = text;
  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'dp-date-menu-trigger';
  trigger.setAttribute('aria-label', 'Date presentation options');
  trigger.setAttribute('aria-haspopup', 'menu');
  trigger.setAttribute('aria-expanded', 'false');
  trigger.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
  const menu = document.createElement('div');
  menu.className = 'dp-date-menu';
  menu.hidden = true;
  menu.setAttribute('role', 'menu');
  menu.innerHTML = `<div class="dp-date-menu-title">Date format</div>${[['friendly', 'Friendly'], ['us', 'US'], ['international', 'International'], ['iso', 'ISO']].map(([v, l]) => `<button type="button" role="menuitemradio" data-date-format="${v}">${l}</button>`).join('')}<div class="dp-date-menu-title">Time style</div><button type="button" role="menuitemradio" data-hour12="false">24-hour</button><button type="button" role="menuitemradio" data-hour12="true">12-hour</button>`;
  const sync = () => {
    menu.querySelectorAll('[data-date-format]').forEach(b => b.setAttribute('aria-checked', b.dataset.dateFormat === pref.format ? 'true' : 'false'));
    menu.querySelectorAll('[data-hour12]').forEach(b => b.setAttribute('aria-checked', String(pref.hour12) === b.dataset.hour12 ? 'true' : 'false'));
  };
  const close = () => { menu.hidden = true; trigger.setAttribute('aria-expanded', 'false'); };
  heading.append(label, trigger, menu);
  sync();
  dateMenuSyncs.push(sync);
  trigger.addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); menu.hidden = !menu.hidden; trigger.setAttribute('aria-expanded', menu.hidden ? 'false' : 'true'); });
  menu.addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.dateFormat) pref.format = b.dataset.dateFormat;
    if (b.dataset.hour12 != null) pref.hour12 = b.dataset.hour12 === 'true';
    savePref(); dateMenuSyncs.forEach(each => each()); close(); loadTorrents(); loadRecent();
  });
  menu.addEventListener('keydown', e => { if (e.key === 'Escape') { e.preventDefault(); close(); trigger.focus(); } });
  document.addEventListener('click', e => { if (!heading.contains(e.target)) close(); });
}

// ── Measured capacity ───────────────────────────────────────────────────
const DESKTOP_QUERY = '(min-width: 780px)';
let capacityTimer = null;
let capacityObserver = null;
let busy = false;

/* How many complete rows the table viewport holds: the wrapper's own client
 * height (the workspace gives it what the card's header, toolbar and pager
 * leave, less any scrollbar) beneath its sticky heading, in rows as tall as
 * the tallest rendered -- never the window's height, which knows nothing of
 * the content inset, the shell's gutter or the card's own chrome. A row that
 * would not fit whole belongs to the next page. */
function measuredSize() {
  const view = document.getElementById('view-torrents');
  const wrap = view?.querySelector('.dp-downloads-table-wrap');
  const table = wrap?.querySelector('table');
  const head = table?.querySelector('thead');
  const rows = Array.from(table?.querySelectorAll('tbody tr[data-torrent-id]') || []);
  if (!view || !wrap || !head || !rows.length || !wrap.clientHeight) return null;
  const heights = rows.map(r => r.getBoundingClientRect().height).filter(h => h > 0);
  if (!heights.length) return null;
  let room = wrap.clientHeight;
  if (!window.matchMedia(DESKTOP_QUERY).matches) {
    // Below 780px the card fills the workspace (one screen of it) but may grow
    // past it, so its table viewport is not the measure: a page holds the
    // whole rows that one screen of the workspace shows beneath the card's
    // header, toolbar, bulk bar and pause notice, above its pager -- none of
    // which depends on how many rows are drawn, so the measure is stable.
    const content = document.getElementById('content');
    const card = view.querySelector(':scope > .card');
    if (!content || !card) return null;
    const space = getComputedStyle(content);
    const chrome = card.getBoundingClientRect().height - wrap.getBoundingClientRect().height;
    room = content.clientHeight - parseFloat(space.paddingTop) - parseFloat(space.paddingBottom) - chrome;
  }
  const budget = Math.max(0, room - head.getBoundingClientRect().height);
  return Math.max(1, Math.min(100, Math.floor((budget + 0.5) / Math.max(...heights)) || 1));
}
function applySize(size) {
  const old = Math.max(1, Number(torrentPageSize) || 1);
  if (!size || size === old) return false;
  const offset = Math.max(0, (Math.max(1, Number(torrentPage) || 1) - 1) * old);
  torrentPageSize = size;
  torrentPage = Math.floor(offset / size) + 1;
  searchControl().invalidate();
  return true;
}
function captureFocus() {
  const active = document.activeElement;
  const row = active?.closest?.('[data-torrent-id]');
  if (!active || active === document.body || !document.getElementById('view-torrents')?.contains(active)) return null;
  return {id: active.id || '', torrentId: row?.dataset?.torrentId || '', label: active.getAttribute('data-default-label') || '', aria: active.getAttribute('aria-label') || ''};
}
function restoreFocus(s) {
  if (!s) return;
  let target = s.id ? document.getElementById(s.id) : null;
  if (!target && s.torrentId) {
    const row = document.querySelector(`#view-torrents [data-torrent-id="${CSS.escape(s.torrentId)}"]`);
    if (row && s.label) target = Array.from(row.querySelectorAll('[data-default-label]')).find(n => n.getAttribute('data-default-label') === s.label);
    if (!target && row && s.aria) target = Array.from(row.querySelectorAll('[aria-label]')).find(n => n.getAttribute('aria-label') === s.aria);
  }
  target?.focus?.({preventScroll: true});
}
/* A resize fetches again only when it changes how many rows fit: a page
 * whose own rendering (or anything else) moved the layout without changing
 * its capacity keeps the rows it has -- otherwise every refresh that shifts a
 * pixel would schedule the next one. A held reorder, a selection or a
 * measurement already in progress keeps the page as loadTorrents does. */
function scheduleCapacityCheck() {
  if (capacityTimer != null) clearTimeout(capacityTimer);
  capacityTimer = setTimeout(() => {
    capacityTimer = null;
    if (reorder || busy || _selectedIds.size) return;
    if (applySize(measuredSize())) loadTorrents().catch(() => {});
  }, 120);
}
// Fonts that finish loading change the rows' height, not the viewport's.
document.fonts?.addEventListener?.('loadingdone', () => scheduleCapacityCheck());
function observeResize() {
  const card = document.querySelector('#view-torrents > .card');
  if (!card || !('ResizeObserver' in window)) return;
  capacityObserver?.disconnect();
  capacityObserver = new ResizeObserver(scheduleCapacityCheck);
  // The workspace too: below 780px a page fits one screen of it, which a
  // window's height alone changes.
  [card, card.querySelector('.dp-downloads-table-wrap'), document.getElementById('bulk-bar'), document.querySelector('.dp-downloads-pause-shim'), document.getElementById('torrent-pagination'), document.getElementById('content')]
    .filter(Boolean).forEach(n => capacityObserver.observe(n));
}

// ── Filter / pagination / empty-state copy ──────────────────────────────
function activeDownloadFilterStatus() {
  return document.querySelector('#view-torrents .filter-tabs .ftab.active')?.dataset.dpStatus || '';
}
function downloadPaginationSummary(total, from, to) {
  if (searchControl().accepted()) {
    if (total <= 0) return 'No downloads match your search';
    if (total === 1 && from === 1 && to === 1) return 'Showing 1 matching download';
    if (from === 1 && to === total) return 'Showing all ' + total + ' matching downloads';
    return 'Showing ' + from + '–' + to + ' of ' + total + ' matching downloads';
  }
  const status = activeDownloadFilterStatus();
  const language = {
    '': ['No Retained Items', 'Showing 1 Retained Item', n => 'Showing ' + n + ' Retained Items'],
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
/* One count, four presentations: the full hint, "N downloads tracked",
 * "N downloads", and the number alone in the title's place. The header's own
 * width shows one of them (ui-downloads-page.css); the full wording stays the
 * title's name, and its DP tooltip while the full hint is not shown. */
function updateDownloadsTrackedCopy(total) {
  const count = Math.max(0, Number(total) || 0);
  const copy = count === 1 ? '1 download tracked. It behaved.' : count + ' downloads tracked. Most of them behaved.';
  const downloads = count === 1 ? '1 download' : count + ' downloads';
  const title = document.getElementById('torrent-card-title');
  if (!title) return;
  const text = {'.dp-downloads-subtitle': copy, '[data-dp-copy="tracked"]': downloads + ' tracked',
    '[data-dp-copy="downloads"]': downloads, '.dp-downloads-count': String(count)};
  for (const [selector, value] of Object.entries(text)) {
    const node = title.querySelector(selector);
    if (node) node.textContent = value;
  }
  title.setAttribute('aria-label', 'On the Books. ' + copy);
  title.setAttribute('data-tooltip', 'On the Books. ' + copy);
}
/* The title is a tab stop from 780px up, where a reduced header shows its full
 * wording in DP's tooltip on keyboard focus. Mobile keeps its own tab order:
 * no stop on the title. */
const titleFocusMedia = window.matchMedia(DESKTOP_QUERY);
function syncTitleFocus() {
  const title = document.getElementById('torrent-card-title');
  if (!title) return;
  if (titleFocusMedia.matches) title.setAttribute('tabindex', '0');
  else title.removeAttribute('tabindex');
}
titleFocusMedia.addEventListener('change', syncTitleFocus);
syncTitleFocus();
function downloadEmptyMessage() {
  if (searchControl().accepted()) return 'No downloads match your search.';
  if (activeDownloadFilterStatus()) return 'No downloads match your current filters.';
  return 'No downloads yet. Add a link, magnet, or torrent file to get started.';
}
function renderTorrentPagination(total, limit, offset) {
  // Pure projector: authoritative (total, limit, offset) -> DOM. Holds no
  // state-transition authority of its own -- callers (fetchAndRenderTorrents)
  // own deciding what is authoritative, including clamping an out-of-range
  // page, and must only ever call this with an offset that is already
  // guaranteed to be in range for the given total/limit.
  const n = Math.max(0, +total || 0), l = Math.max(1, +limit || +torrentPageSize || 1), o = Math.max(0, +offset || 0);
  const pages = Math.max(1, Math.ceil(n / l)), current = Math.min(pages, Math.floor(o / l) + 1);
  const info = document.getElementById('torrent-page-info');
  const buttons = document.getElementById('torrent-page-btns');
  if (!info || !buttons) return;
  const from = n === 0 ? 0 : o + 1, to = Math.min(o + l, n);
  info.textContent = downloadPaginationSummary(n, from, to);
  // The shared DP pager presents; this owner keeps the page arithmetic.
  window.DPPager.render(buttons, {page: current, following: pages - current, actions: {
    first: () => goToTorrentPage(1, {turn: true}),
    previous: () => goToTorrentPage(current - 1, {turn: true}),
    next: () => goToTorrentPage(current + 1, {turn: true}),
    last: () => goToTorrentPage(pages, {turn: true}),
  }});
}
function setFilter(element, status) {
  document.querySelectorAll('#view-torrents .filter-tabs .ftab').forEach(tab => {
    tab.classList.remove('active');
    tab.setAttribute('aria-selected', 'false');
  });
  if (element) { element.classList.add('active'); element.setAttribute('aria-selected', 'true'); }
  currentFilter = status;
  torrentPage = 1;
  searchControl().invalidate();
  clearSelection();
  loadTorrents();
}
function onTorrentOrderChange(value) {
  const next = value === 'newest' ? 'newest' : 'queue';
  if (next === currentOrder) return;
  currentOrder = next;
  torrentPage = 1;
  searchControl().invalidate();
  clearSelection();
  loadTorrents();
}
function goToTorrentPage(p, {turn = false} = {}) {
  const nextPage = Math.max(1, p);
  if (nextPage !== torrentPage) clearSelection();
  torrentPage = nextPage;
  // The turn answers at once: it slides to its destination while that page
  // is fetched.
  endTorrentTurn(torrentPageTurn);
  if (turn && nextPage !== renderedTorrentPage && window.DPMotion) beginTorrentTurn(nextPage);
  searchControl().invalidate();
  // Inert until the requested page is rendered (or its request fails).
  window.DPPager?.pending(document.getElementById('torrent-page-btns'), true);
  loadTorrents();
}
function onPageSizeChange(v) {
  const nextSize = Math.min(Math.max(parseInt(v) || 25, 1), 100);
  if (nextSize !== torrentPageSize || torrentPage !== 1) clearSelection();
  torrentPageSize = nextSize;
  torrentPage = 1;
  searchControl().invalidate();
  loadTorrents();
}

// ── Selection ────────────────────────────────────────────────────────────
function stableDownloadId(value) {
  const id = Number(value);
  return Number.isFinite(id) ? id : null;
}
function reconcileDownloadSelection(items) {
  const presentIds = new Set((Array.isArray(items) ? items : []).map(item => stableDownloadId(item?.id)).filter(id => id !== null));
  for (const id of [..._selectedIds]) { if (!presentIds.has(id)) _selectedIds.delete(id); }
}
function syncDownloadSelectionUi() {
  const checkboxes = [...document.querySelectorAll('.t-chk')];
  let selectedVisible = 0;
  checkboxes.forEach(checkbox => {
    const id = stableDownloadId(checkbox.dataset.id);
    checkbox.checked = id !== null && _selectedIds.has(id);
    if (checkbox.checked) selectedVisible += 1;
  });
  const all = document.getElementById('chk-all');
  if (all) {
    all.checked = checkboxes.length > 0 && selectedVisible === checkboxes.length;
    all.indeterminate = selectedVisible > 0 && selectedVisible < checkboxes.length;
  }
  const bar = document.getElementById('bulk-bar');
  const count = document.getElementById('bulk-count');
  const top = bar?.querySelector('.dp-downloads-bulk-action--top');
  if (top) top.hidden = !selectedQueueMembers().length;
  syncBulkDelete(bar);
  if (_selectedIds.size > 0) {
    bar?.classList.add('visible');
    if (count) count.textContent = _selectedIds.size + ' Selected';
  } else {
    bar?.classList.remove('visible');
    if (count) count.textContent = '';
  }
}
/* Bulk Delete exists only while this caller may destroy every selected
 * transfer: always for an Admin, otherwise while each selected row's
 * server-given ``access.destroy`` allows it. It is mounted from its template,
 * or removed -- never disabled. The server re-checks every target when the
 * delete commits. */
function syncBulkDelete(bar) {
  const actions = bar?.querySelector('.dp-downloads-bulk-actions');
  const template = document.getElementById('dp-bulk-delete-template');
  if (!actions || !template) return;
  const allowed = dpCan('administer')
    || (_selectedIds.size > 0 && [..._selectedIds].every(id => _rowAccess.get(id)?.destroy === true));
  const mounted = actions.querySelector('.dp-downloads-bulk-action--delete');
  if (allowed && !mounted) {
    actions.appendChild(template.content.cloneNode(true));
  } else if (!allowed && mounted) {
    mounted.previousElementSibling?.classList.contains('dp-downloads-bulk-separator') && mounted.previousElementSibling.remove();
    mounted.remove();
  }
}
function onCheckboxChange(checkbox) {
  const id = stableDownloadId(checkbox?.dataset?.id);
  if (id === null) { syncDownloadSelectionUi(); return; }
  if (checkbox.checked) _selectedIds.add(id); else _selectedIds.delete(id);
  syncDownloadSelectionUi();
}
function toggleAllCheckboxes(el) {
  document.querySelectorAll('.t-chk').forEach(checkbox => {
    const id = stableDownloadId(checkbox.dataset.id);
    if (id === null) return;
    if (el.checked) _selectedIds.add(id); else _selectedIds.delete(id);
  });
  syncDownloadSelectionUi();
}
function clearSelection() {
  _selectedIds.clear();
  syncDownloadSelectionUi();
}
/* The selected transfers this page shows as unfinished queue members: the
 * ones Move to Top asks to move (the server decides again). */
function selectedQueueMembers() {
  return [...document.querySelectorAll('#t-tbody tr[data-torrent-id][data-queue-member="true"]')]
    .map(rowId).filter(id => id !== null && _selectedIds.has(id));
}

// ── Row rendering (one pass: presentation status drives badge/progress/
// action visibility directly, exactly as Dashboard Recent already does) ──
// The queue can be rearranged only where the page shows a contiguous stretch
// of the whole queue: the Priority order with no filter and no search.
function queueEditable() {
  return currentOrder === 'queue' && !currentFilter && !searchControl().accepted();
}
function rowMarkup(t) {
  // What this caller may do here: ordinary operation by role, destruction by
  // the row's own server-given access (another account's demand withholds it).
  const operable = dpCan('transfers.operate');
  const destroyable = window.debridPulseAuth?.mayDestroy(t) === true;
  const rowKey = stableDownloadId(t.id);
  if (rowKey !== null) _rowAccess.set(rowKey, {...(t.access || {}), destroy: destroyable});
  const presentation = window.DPProcessingPresentation
    ? window.DPProcessingPresentation.presentationStatus(t, t.status)
    : (t.status || '');
  const showRetry = t.status === 'error' && !AUTO_RECOVERING_PRESENTATION.has(presentation);
  const sourceMarkup = window.DPTransferSourcePresentation ? window.DPTransferSourcePresentation.sourceSlot(t.current_source_identity,
    {providerId: providerChipId(t)}) : '';
  const fileSelectionMarkup = window.DPFileSelection?.chipMarkup?.(t) || '';
  const groupMarkup = window.DPGroupCandidates?.launcherMarkup?.(t, 'compact', 'downloads') || '';
  const name = esc(t.display_name || t.name) || '(unnamed)';
  const movable = operable && queueEditable() && t.queue_member === true;
  // Admission found this promoted transfer waiting only on restarts nobody
  // confirmed: the operator may reaffirm its place (reaffirmQueue).
  const consent = operable && t.queue_member === true && t.queue_restart_consent === true
    ? `<button class="btn btn-warn btn-sm dp-queue-consent" data-default-label="Start Now" data-tooltip="Start Now: lower-ranked transfers must restart for this one to start" onclick="event.stopPropagation();reaffirmQueue(${t.id},this)">${window.DPIcons.iconLabel('play', 'Start Now')}</button>`
    : '';
  // The name's secondary bar, as Download Activity draws it: verified
  // progress while the transfer is still on its way (app.js patches its fill
  // on a live progress update, as it does there).
  const pct = t.progress != null ? Math.round(t.progress) : 0;
  const bar = pct > 0 && !['completed', 'consolidated', 'deleted'].includes(presentation);
  // The handheld Transfer Summary (below 780px): the same projection's
  // primary value and track (app.js progressSummary), the status glyph
  // opening the metadata line, the size closing it.
  const summary = progressSummary(t, presentation);
  const nameCell = movable
    ? `<td class="dp-downloads-name-cell" data-queue-drag="true" tabindex="0" aria-label="${name}" aria-describedby="dp-queue-help"><div class="dp-downloads-name-line">
      <span class="dp-queue-grip" aria-hidden="true">${window.DPIcons?.svg?.('grip-vertical') || ''}</span>`
    : '<td class="dp-downloads-name-cell"><div class="dp-downloads-name-line">';
  // One Name column, as Download Activity draws it: the name, its secondary
  // bar, then the source, the provider and the candidate controls (and a
  // label, when set) -- closed by the size, which shows there only where the
  // table moves Size out of its own column (ui-downloads-page.css).
  return `<tr class="dp-downloads-detail-row" data-torrent-id="${t.id}" data-queue-member="${t.queue_member === true}" data-status="${esc(t.status)}" data-presentation-status="${esc(presentation)}" tabindex="0" onclick="onDownloadRowClick(event,${t.id})" onkeydown="if(event.target===this&&(event.key==='Enter'||event.key===' ')){event.preventDefault();showDetail(${t.id})}">
    ${nameCell}
      <div class="dp-downloads-name-copy">
        ${summary.value}<div class="t-name" data-tooltip="${name}">${name}</div>
        <div class="dash-row-bar-slot" aria-hidden="true"><div class="dash-row-bar${bar ? '' : ' is-empty'}"><div class="dash-row-bar-fill" style="width:${Math.max(0, Math.min(100, pct))}%;background:var(--blue)"></div></div>${summary.track}</div>
        <div class="dp-transfer-provider-meta"><span class="dp-transfer-meta-main">${statusGlyph(transferDisplayStatus(t), t)}${sourceMarkup}${providerChip(t, 'downloads')}${fileSelectionMarkup}${groupMarkup}${t.label ? `<span class="lbl-badge">🏷 ${esc(t.label)}</span>` : ''}</span><span class="dp-downloads-size">${fmtSize(t.size_bytes)}</span></div>
      </div>
    </div></td>
    <td class="dp-downloads-status-cell" data-role="transfer-status">${badge(transferDisplayStatus(t), t)}${consent}</td>
    <td class="dp-downloads-progress-cell" data-role="transfer-progress">${progress(t.progress, presentation, t.active_execution_progress, t.active_execution_basis, t.acquisition_eta ?? null)}</td>
    <td class="sz dp-downloads-size-cell">${fmtSize(t.size_bytes)}</td>
    <td class="sz dp-downloads-date-cell">${dateMarkup(t.created_at)}</td>
    ${operable ? `<td class="dp-downloads-action-cell" onclick="event.stopPropagation()">
      <div class="actions">
        ${consent ? consent.replace('class="btn btn-warn btn-sm dp-queue-consent"', 'class="btn btn-warn btn-sm dp-queue-consent-action"') : ''}
        ${PAUSEABLE_PRESENTATION.has(presentation) ? `<button class="btn btn-blue btn-sm" data-default-label="Pause" onclick="event.stopPropagation();pauseT(${t.id},this)" data-tooltip="Pause this download">${window.DPIcons.iconLabel('pause', 'Pause')}</button>` : ''}
        ${presentation === 'paused' ? `<button class="btn btn-blue btn-sm" data-default-label="Resume" onclick="event.stopPropagation();resumeT(${t.id},this)" data-tooltip="Resume this download">${window.DPIcons.iconLabel('play', 'Resume')}</button>` : ''}
        ${showRetry ? `<button class="btn btn-blue btn-sm" data-default-label="Retry" onclick="event.stopPropagation();retryT(${t.id},this)" data-tooltip="Retry this download">${window.DPIcons.iconLabel('refresh', 'Retry')}</button>` : ''}
        ${destroyable ? `<button class="btn btn-danger btn-sm dp-downloads-remove" data-default-label="Remove" onclick="event.stopPropagation();deleteT(${t.id},event,this)" data-tooltip="Remove this download">${window.DPIcons.iconLabel('trash2', 'Remove')}</button>` : ''}
      </div>
    </td>
    <td class="dp-downloads-select-cell" onclick="event.stopPropagation()"><input type="checkbox" class="t-chk" data-id="${t.id}" aria-label="Select ${name}"${_selectedIds.has(stableDownloadId(t.id)) ? ' checked' : ''} onchange="onCheckboxChange(this)"/></td>` : ''}
  </tr>`;
}

// ── Bounded fetch + render ──────────────────────────────────────────────
async function fetchAndRenderTorrents() {
  // This request's identity, taken before the await so a later
  // authoritative-state mutation (page/filter/search/page-size change)
  // cannot retroactively change what this in-flight request "was for".
  const ticket = searchControl().request();
  const _limit = Math.min(Math.max(parseInt(torrentPageSize) || 25, 1), 100);
  const _offset = (torrentPage - 1) * _limit;
  try {
    const params = new URLSearchParams();
    params.set('limit', String(_limit));
    params.set('offset', String(_offset));
    if (currentFilter) params.set('status', currentFilter);
    if (ticket.query) params.set('search', ticket.query);
    if (currentOrder === 'queue') params.set('order', 'queue');
    const {items, total, search: searched} = await api('GET', '/torrents?' + params.toString(), null, 0, {signal: ticket.signal});
    // A newer authoritative intent superseded this request while it was in
    // flight: this response is stale and must not become truth. Do not
    // render it, do not mutate torrentPage, do not replace rows, do not
    // reconcile selection, do not overwrite pagination. The coalesced
    // trailing run already queued behind this one will fetch and render the
    // current state. (A superseded request is cancelled by its ticket, so it
    // normally ends in the catch below instead.)
    if (!searchControl().settle(ticket, searched)) return;
    const _total = total ?? items.length;
    // The requested page can fall out of range while this request was in
    // flight (e.g. the last item(s) on the last page were just deleted).
    // Clamping is a canonical state transition owned here, at the
    // fetch/controller boundary -- never inside the renderer, and never by
    // projecting this out-of-range response's (necessarily empty-for-that-
    // offset) rows as though they belonged to the clamped page. Establish
    // the clamp, then fetch the clamped page's own authoritative projection
    // instead of rendering this obsolete payload.
    const _pages = Math.max(1, Math.ceil(_total / _limit));
    const _requestedPage = Math.floor(_offset / _limit) + 1;
    const _clampedPage = Math.min(_requestedPage, _pages);
    if (_clampedPage !== _requestedPage) {
      torrentPage = _clampedPage;
      searchControl().invalidate();
      await fetchAndRenderTorrents();
      return;
    }
    torrentTotal = _total;
    const paint = () => {
      const tb = document.getElementById('t-tbody');
      renderTorrentPagination(torrentTotal, _limit, _offset);
      // A page shown while a reorder is held is a preview: selection is kept.
      if (!reorder) reconcileDownloadSelection(items);
      if (!items.length) {
        tb.innerHTML = `<tr><td colspan="${dpCan('transfers.operate') ? 7 : 5}"><div class="empty"><div class="empty-icon" aria-hidden="true"></div>${downloadEmptyMessage()}</div></td></tr>`;
        syncDownloadSelectionUi();
      } else {
        tb.innerHTML = items.map(rowMarkup).join('');
        fitTransferSummaryLines();
        syncDownloadSelectionUi();
      }
      if (reorder) paintReorder();
      renderedTorrentPage = _requestedPage;
    };
    // The page turn the pager asked for is already sliding to this page: its
    // placeholder crossfades into it, or -- not yet drawn -- the slide draws
    // it. Anything else is painted in place.
    const turn = torrentPageTurn;
    if (turn && turn.page === _requestedPage) {
      torrentPageTurn = null;
      turn.ended = true;
      if (turn.shown) replaceTorrentRows(paint);
      else {
        turn.paint = () => { if (searchControl().current(ticket)) replaceTorrentRows(paint); };
        await turn.motion.done;
      }
    } else {
      endTorrentTurn(turn);
      replaceTorrentRows(paint);
    }
  } catch (e) {
    if (!searchControl().current(ticket)) return;
    // The turn did not happen: the page on screen before it comes back, and
    // stays the page asked for.
    const turn = torrentPageTurn;
    if (turn) {
      endTorrentTurn(turn);
      torrentPage = renderedTorrentPage;
      if (turn.shown) replaceTorrentRows(() => {
        document.getElementById('t-tbody').innerHTML = turn.source;
        fitTransferSummaryLines();
        syncDownloadSelectionUi();
      });
    }
    window.DPPager?.pending(document.getElementById('torrent-page-btns'), false);
    if (searchControl().fail(ticket, e) === 'retried') return;
    toast(sanitizeErrorMsg(e.message), 'error');
  }
  document.dispatchEvent(new CustomEvent('debridpulse:downloads-rendered'));
}

async function loadTorrents() {
  // A held reorder (or its confirmation) owns the rows until it ends; the
  // one authoritative refresh that follows it shows whatever changed.
  if (reorder) { reorder.refreshPending = true; return; }
  await fetchAndRenderTorrents();
  ensureDateMenus();
  // A selection holds the page it was made on: the bulk toolbar it shows or
  // reshapes takes room from the table, which scrolls within it, and never
  // repaginates the rows being selected. Clearing it measures again.
  if (!busy && !_selectedIds.size) {
    const size = measuredSize();
    if (applySize(size)) {
      const focus = captureFocus();
      busy = true;
      try { await fetchAndRenderTorrents(); ensureDateMenus(); }
      finally { busy = false; restoreFocus(focus); }
    }
  }
}
loadTorrents = coalesceAsync(loadTorrents);

// ── Operator queue reorder ──────────────────────────────────────────────
// One session at a time: a Name-cell pointer drag or a keyboard move. It is
// identified by the transfer it moves and survives page replacement -- a page
// shown while it is held is navigation only -- and it commits once, on drop,
// as the transfer's new neighbours in the complete queue, never as page
// positions. Nothing changes before the server accepts the move; a move that
// would restart active downloads is confirmed first (409 restart_required).
const DRAG_THRESHOLD = 6;
const PAGE_DWELL_MS = 650;
let reorder = null;
let pressed = null;
// The click that ends a drag never opens Details.
let suppressClick = false;

function onDownloadRowClick(event, id) {
  if (suppressClick) { suppressClick = false; return; }
  if (!dpIsInteractiveRowTarget(event.target)) showDetail(id);
}
function announce(text) {
  const status = document.getElementById('dp-queue-status');
  if (status) status.textContent = text;
}
function rowId(row) { return stableDownloadId(row?.dataset?.torrentId); }
function rowName(row) { return row?.querySelector('.t-name')?.textContent || 'Transfer'; }
function pageCount() { return Math.max(1, Math.ceil(torrentTotal / Math.max(1, torrentPageSize))); }
/* The page's queue rows other than the held one, and how many of them
 * precede the held one when it is on this page (-1 when it is not). */
function candidates() {
  const rows = [...document.querySelectorAll('#t-tbody tr[data-torrent-id][data-queue-member="true"]')];
  return {rows: rows.filter(row => rowId(row) !== reorder.id), own: rows.findIndex(row => rowId(row) === reorder.id)};
}
/* ``reorder.gap`` is an insertion point among ``candidates().rows``. */
function anchors() {
  const {rows, own} = candidates();
  const gap = reorder.gap;
  if (gap === null || gap === undefined || gap < 0 || gap > rows.length) return null;
  if (own >= 0 && gap === own) return 'unchanged';
  const before = rows[gap] ? rowId(rows[gap]) : null;
  const after = rows[gap - 1] ? rowId(rows[gap - 1]) : null;
  return before === null && after === null ? null : {before_id: before, after_id: after};
}
function queueContinuesAfterPage() {
  const last = document.querySelector('#t-tbody tr[data-torrent-id]:last-child');
  return !!last && last.dataset.queueMember === 'true' && torrentPage < pageCount();
}
function zoneRects() {
  const head = document.querySelector('#view-torrents .dp-downloads-table-wrap thead');
  const pager = document.getElementById('torrent-pagination');
  return {
    previous: torrentPage > 1 && head ? head.getBoundingClientRect() : null,
    next: queueContinuesAfterPage() && pager ? pager.getBoundingClientRect() : null,
  };
}
function paintReorder() {
  document.querySelectorAll('#t-tbody [data-drop], #t-tbody .dp-queue-drag-source').forEach(node => {
    node.removeAttribute('data-drop');
    node.classList.remove('dp-queue-drag-source');
  });
  document.querySelector(`#t-tbody tr[data-torrent-id="${reorder.id}"]`)?.classList.add('dp-queue-drag-source');
  const {rows} = candidates();
  const target = anchors();
  if (target && target !== 'unchanged') {
    if (rows[reorder.gap]) rows[reorder.gap].dataset.drop = 'before';
    else rows[rows.length - 1]?.setAttribute('data-drop', 'after');
  }
  const rects = reorder.mode === 'pointer' ? zoneRects() : {};
  for (const direction of ['previous', 'next']) {
    const zone = reorder.zones?.[direction];
    const rect = rects[direction];
    if (!zone) continue;
    zone.hidden = !rect;
    if (rect) Object.assign(zone.style, {left: rect.left + 'px', top: rect.top + 'px', width: rect.width + 'px', height: Math.max(32, rect.height) + 'px'});
    zone.classList.toggle('is-armed', reorder.dwellDirection === direction);
  }
}
function beginReorder(id, mode) {
  searchControl().invalidate();
  const row = document.querySelector(`#t-tbody tr[data-torrent-id="${id}"]`);
  reorder = {id, mode, name: rowName(row), page: torrentPage, originPage: torrentPage, gap: null,
             refreshPending: false, navigating: false, committing: false, dwell: null, dwellDirection: '',
             zones: null, ghost: null, pointer: null};
  reorder.gap = candidates().own;
  document.getElementById('t-tbody')?.classList.add('dp-queue-dragging');
  if (mode === 'pointer') {
    const ghost = document.createElement('div');
    ghost.className = 'dp-queue-drag-ghost';
    ghost.setAttribute('aria-hidden', 'true');
    ghost.innerHTML = `${window.DPIcons?.svg?.('grip-vertical') || ''}<span></span>`;
    ghost.querySelector('span').textContent = reorder.name;
    document.body.appendChild(ghost);
    reorder.ghost = ghost;
    reorder.zones = {};
    for (const [direction, label, icon] of [['previous', 'Previous page', 'chevronLeft'], ['next', 'Next page', 'chevronRight']]) {
      const zone = document.createElement('div');
      zone.className = 'dp-queue-page-zone';
      zone.dataset.direction = direction;
      zone.hidden = true;
      zone.innerHTML = `${window.DPIcons?.svg?.(icon) || ''}<span>${label}</span>`;
      document.body.appendChild(zone);
      reorder.zones[direction] = zone;
    }
  }
  document.addEventListener('keydown', onReorderKey, true);
  announce(mode === 'keyboard'
    ? `Picked up ${reorder.name}. Use the arrow keys to move it, Enter or Space to drop it, Escape to cancel.`
    : `Moving ${reorder.name}.`);
  paintReorder();
}
function disarmDwell() {
  if (reorder?.dwell) clearTimeout(reorder.dwell);
  if (reorder) { reorder.dwell = null; reorder.dwellDirection = ''; }
}
function endReorder() {
  if (!reorder) return;
  disarmDwell();
  const ended = reorder;
  reorder = null;
  ended.ghost?.remove();
  Object.values(ended.zones || {}).forEach(zone => zone.remove());
  document.removeEventListener('keydown', onReorderKey, true);
  document.getElementById('t-tbody')?.classList.remove('dp-queue-dragging');
  document.querySelectorAll('#t-tbody [data-drop], #t-tbody .dp-queue-drag-source').forEach(node => {
    node.removeAttribute('data-drop');
    node.classList.remove('dp-queue-drag-source');
  });
  // Selection belongs to the page it was made on.
  if (ended.page !== ended.originPage) clearSelection();
  const focus = captureFocus();
  loadTorrents().then(() => restoreFocus(focus)).catch(() => {});
}
async function navigateReorder(delta) {
  const target = torrentPage + delta;
  if (!reorder || reorder.navigating || target < 1 || target > pageCount()) return false;
  reorder.navigating = true;
  torrentPage = target;
  searchControl().invalidate();
  try { await fetchAndRenderTorrents(); } finally { if (reorder) reorder.navigating = false; }
  if (!reorder) return false;
  reorder.page = torrentPage;
  announce(`Page ${torrentPage}.`);
  return true;
}
function armDwell(direction) {
  if (reorder.dwellDirection === direction) return;
  disarmDwell();
  reorder.dwellDirection = direction;
  reorder.dwell = setTimeout(async () => {
    if (!reorder || reorder.dwellDirection !== direction) return;
    reorder.dwell = null;
    reorder.dwellDirection = '';
    await navigateReorder(direction === 'previous' ? -1 : 1);
    // Still held over the edge: the next page follows after another dwell.
    if (reorder?.pointer) trackPointer(reorder.pointer.x, reorder.pointer.y);
  }, PAGE_DWELL_MS);
  paintReorder();
}
function trackPointer(x, y) {
  reorder.pointer = {x, y};
  if (reorder.ghost) Object.assign(reorder.ghost.style, {left: x + 14 + 'px', top: y + 10 + 'px'});
  const rects = zoneRects();
  const inside = rect => rect && x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
  const zone = inside(rects.previous) ? 'previous' : inside(rects.next) ? 'next' : '';
  if (zone) {
    reorder.gap = null;
    armDwell(zone);
    return;
  }
  disarmDwell();
  const row = document.elementFromPoint(x, y)?.closest?.('#t-tbody tr[data-torrent-id]');
  const {rows, own} = candidates();
  if (!row) reorder.gap = null;
  else if (rowId(row) === reorder.id) reorder.gap = own;
  else if (row.dataset.queueMember === 'true') {
    const box = row.getBoundingClientRect();
    reorder.gap = rows.indexOf(row) + (y > box.top + box.height / 2 ? 1 : 0);
  } else reorder.gap = rows.length ? rows.length : null;    // settled history: the end of the queue
  paintReorder();
}
function releasePointer() {
  pressed = null;
  document.removeEventListener('pointermove', onPointerMove);
  document.removeEventListener('pointerup', onPointerUp);
  document.removeEventListener('pointercancel', onPointerCancel);
}
function onNamePointerDown(event) {
  const cell = event.target.closest?.('td[data-queue-drag="true"]');
  if (!cell || reorder || pressed || event.button !== 0 || !event.isPrimary) return;
  // The name's own controls (the provider selector, file selection, the
  // candidate launcher) act on their own: pressing one never picks the row up.
  if (dpIsInteractiveRowTarget(event.target)) return;
  // A touch on the name scrolls the table; the grip is the touch handle.
  if (event.pointerType === 'touch' && !event.target.closest('.dp-queue-grip')) return;
  suppressClick = false;
  pressed = {id: rowId(cell.closest('tr')), x: event.clientX, y: event.clientY, pointerId: event.pointerId};
  document.addEventListener('pointermove', onPointerMove);
  document.addEventListener('pointerup', onPointerUp);
  document.addEventListener('pointercancel', onPointerCancel);
}
function onPointerMove(event) {
  if (!pressed || event.pointerId !== pressed.pointerId) return;
  if (!reorder) {
    if (Math.hypot(event.clientX - pressed.x, event.clientY - pressed.y) < DRAG_THRESHOLD) return;
    beginReorder(pressed.id, 'pointer');
  }
  if (reorder.mode !== 'pointer' || reorder.committing) return;
  event.preventDefault();
  trackPointer(event.clientX, event.clientY);
}
function onPointerUp(event) {
  if (!pressed || event.pointerId !== pressed.pointerId) return;
  releasePointer();
  if (!reorder || reorder.mode !== 'pointer' || reorder.committing) return;
  suppressClick = true;
  setTimeout(() => { suppressClick = false; }, 0);
  dropReorder();
}
function onPointerCancel() {
  releasePointer();
  if (reorder && reorder.mode === 'pointer' && !reorder.committing) {
    announce('Move cancelled. Nothing was moved.');
    endReorder();
  }
}
function onNameKeyDown(event) {
  const cell = event.target.closest?.('td[data-queue-drag="true"]');
  if (!cell || reorder || event.target !== cell || (event.key !== ' ' && event.key !== 'Enter')) return;
  event.preventDefault();
  event.stopPropagation();
  beginReorder(rowId(cell.closest('tr')), 'keyboard');
}
async function stepReorder(delta) {
  let {rows, own} = candidates();
  let gap = (reorder.gap ?? own) + delta;
  if (gap < 0) {
    if (await navigateReorder(-1)) gap = Math.max(0, candidates().rows.length - 1);
    else gap = 0;
  } else if (gap > rows.length) {
    if (queueContinuesAfterPage() && await navigateReorder(1)) gap = Math.min(1, candidates().rows.length);
    else gap = rows.length;
  }
  if (!reorder) return;
  reorder.gap = gap;
  ({rows} = candidates());
  paintReorder();
  const target = rows[gap] || rows[gap - 1];
  target?.querySelector('td[data-queue-drag="true"]')?.focus({preventScroll: false});
  const place = anchors();
  announce(place === 'unchanged' ? `${reorder.name}: its current place.`
    : rows[gap] ? `${reorder.name}: before ${rowName(rows[gap])}.` : `${reorder.name}: after ${rowName(rows[gap - 1])}.`);
}
function onReorderKey(event) {
  if (!reorder || reorder.committing) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    announce('Move cancelled. Nothing was moved.');
    endReorder();
    return;
  }
  if (reorder.mode !== 'keyboard') return;
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    if (!reorder.navigating) stepReorder(event.key === 'ArrowDown' ? 1 : -1);
  } else if (event.key === ' ' || event.key === 'Enter') {
    event.preventDefault();
    event.stopPropagation();
    dropReorder();
  }
}
async function dropReorder() {
  disarmDwell();
  const place = anchors();
  if (!place || place === 'unchanged') {
    announce(place ? `${reorder.name} was not moved.` : 'Move cancelled. Nothing was moved.');
    endReorder();
    return;
  }
  reorder.committing = true;
  reorder.ghost?.remove();
  Object.values(reorder.zones || {}).forEach(zone => { zone.hidden = true; });
  await commitMove(reorder.id, place);
  endReorder();
}
function restartProgress(item) {
  const total = Number(item?.total_bytes) || 0;
  const done = Math.max(0, Number(item?.completed_bytes) || 0);
  const estimate = item?.estimated_percentage;
  if (total > 0) return `${fmtSize(done)} of ${fmtSize(total)} downloaded`;
  if (typeof estimate === 'number' && Number.isFinite(estimate)) return `About ${Math.floor(estimate)}% downloaded (estimated)`;
  return done > 0 ? `${fmtSize(done)} downloaded` : '';
}
/* The destructive-preemption confirmation, through the canonical dialog
 * owner. Resolves to every attempt the operator confirmed, or null. */
async function confirmRestart(transfers) {
  const modal = window.DPSettingsModal;
  if (!modal || typeof modal.open !== 'function') {
    toast('Reorder confirmation is unavailable. Nothing was moved.', 'error');
    return null;
  }
  const one = transfers.length === 1;
  const handle = modal.open({
    role: 'alertdialog', tone: 'warning', title: 'Transfer Restart Required',
    acceptLabel: 'Confirm Reorder', acceptTone: 'warning', cancelLabel: 'Cancel',
    className: 'dp-queue-restart-dialog',
    mount(body) {
      const hint = text => {
        const node = document.createElement('p');
        node.className = 'dp-modal-message dp-modal-message--center';
        node.textContent = text;
        return node;
      };
      const top = hint(one
        ? 'Reordering the queue will interrupt 1 active transfer that cannot resume its current download.'
        : `Reordering the queue will interrupt ${transfers.length} active transfers that cannot resume their current downloads.`);
      const list = document.createElement('ul');
      list.className = 'dp-queue-restart-list';
      for (const item of transfers) {
        const entry = document.createElement('li');
        entry.className = 'dp-queue-restart-item';
        const name = document.createElement('strong');
        name.className = 'dp-queue-restart-name';
        name.textContent = name.dataset.tooltip = String(item?.display_name || item?.name || 'Transfer');
        entry.appendChild(name);
        const progress = restartProgress(item);
        if (progress) {
          const line = document.createElement('span');
          line.className = 'dp-queue-restart-progress';
          line.textContent = progress;
          entry.appendChild(line);
        }
        const consequence = document.createElement('span');
        consequence.className = 'dp-queue-restart-consequence';
        consequence.textContent = 'This transfer will restart downloading from the beginning when it becomes eligible again.';
        entry.appendChild(consequence);
        list.appendChild(entry);
      }
      body.append(top, list, hint(one
        ? 'Downloaded progress for this transfer will be lost. Are you sure you want to proceed?'
        : 'Downloaded progress for these transfers will be lost. Are you sure you want to proceed?'));
      return top;
    },
  });
  const result = await handle.closed;
  return result.accepted ? transfers.flatMap(item => (item?.attempt_ids || []).map(String)) : null;
}
async function commitMove(id, place, confirmed) {
  const body = {before_id: place.before_id, after_id: place.after_id};
  if (confirmed) body.confirm_restart = confirmed;
  try {
    const result = await api('PATCH', `/torrents/${id}/priority`, body);
    announce(result?.outcome === 'preempting'
      ? `${reorder?.name || 'Transfer'} moved. It starts once lower-ranked work has stopped.`
      : `${reorder?.name || 'Transfer'} moved.`);
  } catch (e) {
    if (e.status === 409 && e.detail?.confirmation === 'restart_required' && Array.isArray(e.detail.transfers)) {
      const attempts = await confirmRestart(e.detail.transfers);
      if (attempts) return commitMove(id, place, attempts);
      announce('Reorder cancelled. Nothing was moved.');
      return;
    }
    toast(sanitizeErrorMsg(e.message), e.status === 409 ? 'warn' : 'error');
  }
}
/* The transfer keeps its place and renews the promotion it holds there,
 * through the same restart confirmation a move asks for: nothing changes
 * unless the operator confirms the restarts the server names now. */
async function reaffirmQueue(id, button) {
  const name = button?.closest('tr')?.querySelector('.t-name')?.textContent || 'Transfer';
  setButtonPending(button, true, 'Checking…');
  try {
    let confirmed = null;
    for (;;) {
      try {
        const result = await api('PATCH', `/torrents/${id}/priority`,
          confirmed ? {reaffirm: true, confirm_restart: confirmed} : {reaffirm: true});
        announce(result?.outcome === 'preempting'
          ? `${name} starts once lower-ranked work has stopped.`
          : `${name} keeps its place. No restart needs confirming now.`);
        break;
      } catch (e) {
        if (e.status === 409 && e.detail?.confirmation === 'restart_required' && Array.isArray(e.detail.transfers)) {
          confirmed = await confirmRestart(e.detail.transfers);
          if (confirmed) continue;
          announce('Cancelled. Nothing changed.');
          return;
        }
        toast(sanitizeErrorMsg(e.message), e.status === 409 ? 'warn' : 'error');
        return;
      }
    }
    await loadTorrents();
  } finally { setButtonPending(button, false); }
}

// ── Move to Top ─────────────────────────────────────────────────────────
// One command behind the row menu and the bulk action: the server moves the
// unfinished transfers among ``ids`` to the front of the whole queue, in
// their current queue order, as one move -- whatever page, filter, search or
// order this view shows. A restart it would cause is confirmed first, once
// for all of them, through the same confirmation a drag asks for.
let topPending = false;
async function moveToTop(ids, {button = null, name = '', focusId = null, selection = false} = {}) {
  if (topPending || !ids.length) return;
  topPending = true;
  setButtonPending(button, true, 'Moving…');
  try {
    let confirmed = null;
    let result = null;
    for (;;) {
      try {
        result = await api('POST', '/torrents/move-to-top', confirmed ? {ids, confirm_restart: confirmed} : {ids});
        break;
      } catch (e) {
        if (e.status === 409 && e.detail?.confirmation === 'restart_required' && Array.isArray(e.detail.transfers)) {
          confirmed = await confirmRestart(e.detail.transfers);
          if (confirmed) continue;
          announce('Move to Top cancelled. Nothing was moved.');
          return;
        }
        toast(sanitizeErrorMsg(e.message), e.status === 409 ? 'warn' : 'error');
        return;
      }
    }
    const moved = Number(result?.moved) || 0;
    const subject = ids.length === 1 ? (name || 'Transfer') : `${moved} transfer${moved === 1 ? '' : 's'}`;
    const message = result?.outcome === 'unchanged'
      ? `${ids.length === 1 ? subject : 'The selection'} already leads the queue.`
      : `${subject} moved to the top of the queue.${result?.outcome === 'preempting' ? ' Lower-ranked work stops to make room.' : ''}`;
    announce(message);
    // The unfiltered Priority view shows the new top on its first page; any
    // other view stays as the operator chose it and says what happened. A
    // completed bulk Move to Top ends the selection it acted on.
    if (!queueEditable()) toast(message, 'success');
    if (selection) clearSelection();
    if (queueEditable() && torrentPage !== 1) goToTorrentPage(1);
    else await loadTorrents();
    if (focusId !== null && (!document.activeElement || document.activeElement === document.body)) {
      document.querySelector(`#t-tbody tr[data-torrent-id="${focusId}"]`)?.focus();
    }
  } finally {
    topPending = false;
    setButtonPending(button, false);
    if (selection) settleRemovalFocus(button);
  }
}
function moveSelectionToTop(button) {
  moveToTop(selectedQueueMembers(), {button, selection: true});
}

// The row menu: one command, Move to Top, for an unfinished transfer's row,
// opened by the application's one context-menu policy (app.js) once it has
// left the browser its own menu wherever that does real work, and shown by
// its one menu (dpOpenContextMenu), which closes it back to its row.
function contextMenu(event) {
  const row = event.target.closest?.('#t-tbody tr[data-torrent-id]');
  if (!dpCan('transfers.operate') || !row || row.dataset.queueMember !== 'true' || reorder) return false;
  const id = rowId(row);
  const name = rowName(row);
  return dpOpenContextMenu(event, row, {
    label: `${name} actions`,
    items: [{text: 'Move to Top', run: () => moveToTop([id], {name, focusId: id})}],
    restore: () => document.querySelector(`#t-tbody tr[data-torrent-id="${id}"]`)?.focus(),
  });
}

// ── Row/bulk actions ─────────────────────────────────────────────────────
// A confirmed removal re-renders the list, so the control that started it is gone or replaced. Once the
// operation settles, focus goes to a deliberate surviving control and never stays on <body>: the retry
// button after a failure, otherwise the list toolbar's search field. The successor is deliberately NOT a
// row -- rows are replaced wholesale by every refresh (including the debounced capacity refresh that
// follows a shrinking table), the toolbar is not. Focus the operator moved elsewhere in the meantime is
// left alone. A confirmation returns focus on its own clock, once it has gone, and to a neighbour of its
// opener when the opener is busy; where it put focus (``restored``: its ``gone``) is the dialog's choice,
// not the operator's, so it never stands in for a deliberate surviving control.
async function settleRemovalFocus(button, restored = null) {
  const returned = restored ? await restored : null;
  const active = document.activeElement;
  if (active && active !== document.body && active.isConnected && active !== returned) return;
  const retry = button?.isConnected && !button.disabled && button.getClientRects().length ? button : null;
  (retry || document.getElementById('torrent-search'))?.focus();
}
async function deleteT(id, eventObj, button) {
  eventObj?.stopPropagation();
  const confirmedIds = await confirmDownloadRemoval([id]);
  if (!confirmedIds) return;
  const restored = removalConfirmationGone;
  const targetId = confirmedIds[0];
  setButtonPending(button, true, 'Deleting…');
  try {
    await api('DELETE', `/torrents/${targetId}?from_alldebrid=true`);
    toast('Deleted', 'success');
    await loadTorrents();
    loadStats();
  } catch (e) { toast(sanitizeErrorMsg(e.message), 'error'); }
  finally { setButtonPending(button, false); await settleRemovalFocus(button, restored); }
}
async function retryT(id, button) {
  setButtonPending(button, true, 'Retrying…');
  try {
    await api('POST', `/torrents/${id}/retry`);
    toast('Queued for retry', 'success');
    loadTorrents();
  } catch (e) { toast(sanitizeErrorMsg(e.message), 'error'); }
  finally { setButtonPending(button, false); }
}
async function confirmDownloadRemoval(ids) {
  const stableIds = [...new Set((Array.isArray(ids) ? ids : []).map(stableDownloadId).filter(id => id !== null))];
  if (!stableIds.length) return null;
  const modal = window.DPSettingsModal;
  if (!modal || typeof modal.confirm !== 'function') {
    toast('Removal confirmation is unavailable. No downloads were removed.', 'error');
    return null;
  }
  const count = stableIds.length;
  const confirmed = await modal.confirm({
    title: count === 1 ? 'Remove download?' : `Remove ${count} downloads?`,
    message: count === 1 ? 'Remove this download from Downloads?' : `Remove these ${count} downloads from Downloads?`,
    confirmLabel: 'Remove', cancelLabel: 'Cancel', tone: 'danger', withExit: true,
  });
  removalConfirmationGone = confirmed.accepted ? confirmed.gone : null;
  return confirmed.accepted ? stableIds : null;
}
// The last accepted removal confirmation's ``gone`` (ui-settings-modal.js), read by its caller at once:
// the dialog is modal, so no other confirmation settles in between.
let removalConfirmationGone = null;
async function bulkAction(action, button) {
  if (!_selectedIds.size) return;
  let ids = [..._selectedIds];
  let restored = null;
  if (action === 'delete') {
    const confirmedIds = await confirmDownloadRemoval(ids);
    if (!confirmedIds) return;
    ids = confirmedIds;
    restored = removalConfirmationGone;
  }
  const pendingLabels = {delete: 'Deleting…', reset: 'Resetting…', pause: 'Pausing…', resume: 'Resuming…'};
  setButtonPending(button, true, pendingLabels[action] || 'Working…');
  try {
    const r = await api('POST', '/torrents/bulk', {ids, action});
    toast(`Done: ${r.ok} ok, ${r.failed} failed`, r.failed ? 'warn' : 'success');
    // A completed action ends the selection it acted on, and the list shows
    // what happened. When nothing completed the selection stays to retry.
    if (r.ok > 0 || !r.failed) clearSelection();
    await loadTorrents();
    loadStats();
  } catch (e) { toast(e.message, 'error'); }
  finally {
    setButtonPending(button, false);
    // Settled means focus has settled too: the notification follows it.
    await settleRemovalFocus(button, restored);
    document.dispatchEvent(new CustomEvent('debridpulse:downloads-bulk-action-settled', {detail: {action}}));
  }
}
async function setLabel(id) {
  // The canonical application dialog owner, never a browser-native prompt.
  // Semantics are unchanged: cancel is a no-op, blank clears the label.
  const label = await window.DPSettingsModal.prompt({
    title: 'Set Label',
    label: 'Label',
    hint: 'Leave empty to clear the label.',
    acceptLabel: 'Save',
  });
  if (label === null) return;
  try {
    // A label edit never carries a priority: the queue position stays.
    await api('PUT', `/torrents/${id}/label`, {label: label.trim()});
    toast('Label updated', 'success');
    loadTorrents();
  } catch (e) { toast(e.message, 'error'); }
}

// ── Init/wiring ──────────────────────────────────────────────────────────
function init() {
  searchControl();
  ensureDateMenus();
  observeResize();
  const body = document.getElementById('t-tbody');
  body?.addEventListener('pointerdown', onNamePointerDown);
  body?.addEventListener('keydown', onNameKeyDown);
  window.addEventListener('resize', scheduleCapacityCheck, {passive: true});
  document.addEventListener('debridpulse:navigation', e => {
    if (e.detail?.view === 'torrents') { observeResize(); scheduleCapacityCheck(); }
  });
}

window.loadTorrents = loadTorrents;
// The one date cell markup, also drawn by Dashboard Recent Activity.
window.downloadsDateMarkup = dateMarkup;
// The same preference applied to calendar fields the server already bucketed (Statistics).
window.downloadsCalendarLabel = formatCalendar;
// The same, for one instant's day (a premium account's expiry).
window.downloadsDayLabel = formatDownloadsDay;
window.updateDownloadsTrackedCopy = updateDownloadsTrackedCopy;
window.setFilter = setFilter;
window.onTorrentOrderChange = onTorrentOrderChange;
window.onDownloadRowClick = onDownloadRowClick;
window.reaffirmQueue = reaffirmQueue;
window.moveSelectionToTop = moveSelectionToTop;
window.goToTorrentPage = goToTorrentPage;
window.onPageSizeChange = onPageSizeChange;
window.toggleAllCheckboxes = toggleAllCheckboxes;
window.onCheckboxChange = onCheckboxChange;
window.clearSelection = clearSelection;
window.bulkAction = bulkAction;
window.setLabel = setLabel;
window.deleteT = deleteT;
window.retryT = retryT;
// scheduleCapacityCheck is a real production consumer
// (ui-processing-presentation.js's syncPauseUi() re-measures capacity after
// the pause shim toggles); contextMenu is the row menu the application's one
// context-menu policy (app.js) offers. No other member is exposed here --
// there is no other real runtime consumer of Downloads' internal state.
window.DPDownloads = Object.freeze({scheduleCapacityCheck, contextMenu});

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
else init();
})();
