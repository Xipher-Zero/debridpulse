/* DebridPulse — canonical Downloads controller/renderer owner
 * (DP 1.0.12 canonical flattening, CANON-001 closure).
 *
 * Sole owner of: list page state, the bounded /torrents fetch, final row
 * rendering, filtering/search, pagination, date mode/menu, measured
 * capacity/page sizing, list selection state, bulk actions, refresh, and
 * page-specific lifecycle/event wiring. Every row is emitted in final form
 * by a single render pass -- this file never replaces api/loadTorrents/
 * renderTorrentPagination/fmtDate on another owner, and never re-renders an
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
let currentTorrentSearch = '';
let torrentPage = 1;
let torrentPageSize = 25;
let torrentTotal = 0;
let _torrentSearchTimer = null;
let _selectedIds = new Set();
// Bumped by every authoritative state mutation that changes the requested
// list projection (page, filter, search, page size -- including a
// capacity-derived page-size transition). A fetch snapshots this value when
// it starts; if it no longer matches on completion, a newer intent has
// superseded it and the response must be discarded rather than rendered --
// an in-flight response for an older view state must never overwrite newer
// user/application intent.
let requestGeneration = 0;

// ── Date presentation preference ────────────────────────────────────────
let pref = loadPref();
function loadPref() {
  try {
    const p = JSON.parse(localStorage.getItem(DATE_PREF_KEY) || '{}');
    return {format: DATE_FORMATS.has(p.format) ? p.format : 'friendly', hour12: p.hour12 === true};
  } catch (_) { return {format: 'friendly', hour12: false}; }
}
function savePref() { try { localStorage.setItem(DATE_PREF_KEY, JSON.stringify(pref)); } catch (_) {} }
function timeZone() { try { return String(settingsData?.timezone || '').trim() || 'UTC'; } catch (_) { return 'UTC'; } }
function parts(date) {
  return Object.fromEntries(
    new Intl.DateTimeFormat('en-US', {timeZone: timeZone(), year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: pref.hour12})
      .formatToParts(date).filter(p => p.type !== 'literal').map(p => [p.type, p.value])
  );
}
function dateKey(p) { return `${p.year}-${p.month}-${p.day}`; }
function previousKey(p) {
  const d = new Date(Date.UTC(+p.year, +p.month - 1, +p.day) - 86400000);
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`;
}
function clock(p) { return pref.hour12 ? `${p.hour}:${p.minute} ${p.dayPeriod || ''}`.trim() : `${p.hour}:${p.minute}`; }
function formatDownloadsDate(value) {
  if (!value) return '—';
  const d = typeof parseApiDate === 'function' ? parseApiDate(value) : new Date(value);
  if (!d || Number.isNaN(d.getTime())) return String(value);
  const p = parts(d), c = clock(p);
  if (pref.format === 'us') return `${p.month}/${p.day}/${p.year} ${c}`;
  if (pref.format === 'international') return `${p.day}/${p.month}/${p.year} ${c}`;
  if (pref.format === 'iso') return `${p.year}-${p.month}-${p.day} ${c}`;
  const now = parts(new Date());
  if (dateKey(p) === dateKey(now)) return `Today ${c}`;
  if (dateKey(p) === previousKey(now)) return `Yesterday ${c}`;
  const month = new Intl.DateTimeFormat('en-US', {timeZone: timeZone(), month: 'short'}).format(d);
  return +p.year === +now.year ? `${month} ${+p.day} ${c}` : `${month} ${+p.day}, ${p.year} ${c}`;
}
function exactDate(value) {
  if (!value) return '';
  const d = typeof parseApiDate === 'function' ? parseApiDate(value) : new Date(value);
  if (!d || Number.isNaN(d.getTime())) return String(value);
  return new Intl.DateTimeFormat('en-US', {timeZone: timeZone(), year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short', hour12: false}).format(d);
}
function dateMarkup(value) {
  return `<span class="dp-downloads-date-value" tabindex="0" title="${esc(exactDate(value))}">${esc(formatDownloadsDate(value))}</span>`;
}

function ensureDateMenu() {
  const table = document.querySelector('#view-torrents .dp-downloads-table-wrap table');
  const heading = table ? Array.from(table.querySelectorAll('thead th')).find(n => ['Added', 'Date'].includes(n.textContent.trim())) : null;
  if (!heading || heading.querySelector('.dp-date-menu-trigger')) return;
  heading.textContent = '';
  const label = document.createElement('span');
  label.textContent = 'Date';
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
  trigger.addEventListener('click', e => { e.preventDefault(); e.stopPropagation(); menu.hidden = !menu.hidden; trigger.setAttribute('aria-expanded', menu.hidden ? 'false' : 'true'); });
  menu.addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.dateFormat) pref.format = b.dataset.dateFormat;
    if (b.dataset.hour12 != null) pref.hour12 = b.dataset.hour12 === 'true';
    savePref(); sync(); close(); loadTorrents();
  });
  menu.addEventListener('keydown', e => { if (e.key === 'Escape') { e.preventDefault(); close(); trigger.focus(); } });
  document.addEventListener('click', e => { if (!heading.contains(e.target)) close(); });
}

// ── Measured desktop capacity ───────────────────────────────────────────
const DESKTOP_QUERY = '(min-width: 701px)';
let capacityTimer = null;
let capacityObserver = null;
let busy = false;

function measuredSize() {
  if (!window.matchMedia(DESKTOP_QUERY).matches) return null;
  const view = document.getElementById('view-torrents');
  const wrap = view?.querySelector('.dp-downloads-table-wrap');
  const table = wrap?.querySelector('table');
  const head = table?.querySelector('thead');
  const rows = Array.from(table?.querySelectorAll('tbody tr[data-torrent-id]') || []);
  const pager = document.getElementById('torrent-pagination');
  if (!view || !wrap || !head || !rows.length || !pager) return null;
  const heights = rows.map(r => r.getBoundingClientRect().height).filter(h => h > 0);
  if (!heights.length) return null;
  const budget = Math.max(0, window.innerHeight - wrap.getBoundingClientRect().top - pager.getBoundingClientRect().height - head.getBoundingClientRect().height - 4);
  return Math.max(1, Math.min(100, Math.floor(budget / Math.max(...heights)) || 1));
}
function applySize(size) {
  const old = Math.max(1, Number(torrentPageSize) || 1);
  if (!size || size === old) return false;
  const offset = Math.max(0, (Math.max(1, Number(torrentPage) || 1) - 1) * old);
  torrentPageSize = size;
  torrentPage = Math.floor(offset / size) + 1;
  requestGeneration++;
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
function scheduleCapacityCheck() {
  if (capacityTimer != null) clearTimeout(capacityTimer);
  capacityTimer = setTimeout(() => { capacityTimer = null; loadTorrents().catch(() => {}); }, 120);
}
function observeResize() {
  const card = document.querySelector('#view-torrents > .card');
  if (!card || !('ResizeObserver' in window)) return;
  capacityObserver?.disconnect();
  capacityObserver = new ResizeObserver(scheduleCapacityCheck);
  [card, card.querySelector('.dp-downloads-table-wrap'), document.getElementById('bulk-bar'), document.querySelector('.dp-downloads-pause-shim'), document.getElementById('torrent-pagination')]
    .filter(Boolean).forEach(n => capacityObserver.observe(n));
}

// ── Filter / pagination / empty-state copy ──────────────────────────────
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
function updateDownloadsTrackedCopy(total) {
  const count = Math.max(0, Number(total) || 0);
  const copy = count === 1 ? '1 download tracked. It followed instructions.' : count + ' downloads tracked. Most of them followed instructions.';
  const title = document.getElementById('torrent-card-title');
  const subtitle = title?.querySelector('.dp-downloads-subtitle');
  if (subtitle) subtitle.textContent = copy;
  if (title) title.setAttribute('aria-label', 'On the Books. ' + copy);
}
function downloadEmptyMessage() {
  const search = document.getElementById('torrent-search');
  if (search && search.value.trim()) return 'No downloads match your search.';
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
  const icon = name => window.DPIcons?.svg?.(name) || '';
  const prev = current > 1
    ? `<button type="button" class="btn btn-ghost btn-sm dp-pager-btn" aria-label="Previous page" onclick="goToTorrentPage(${current - 1})">${icon('chevronLeft')}</button>`
    : '<span class="dp-pager-placeholder" aria-hidden="true"></span>';
  const next = current < pages
    ? `<button type="button" class="btn btn-ghost btn-sm dp-pager-btn" aria-label="Next page" onclick="goToTorrentPage(${current + 1})">${icon('chevronRight')}</button>`
    : '<span class="dp-pager-placeholder" aria-hidden="true"></span>';
  buttons.innerHTML = `<span class="dp-pager-slot">${prev}</span><span class="btn btn-primary btn-sm dp-pager-current" aria-current="page">${current}</span><span class="dp-pager-slot">${next}</span>`;
}
function setFilter(element, status) {
  document.querySelectorAll('#view-torrents .filter-tabs .ftab').forEach(tab => {
    tab.classList.remove('active');
    tab.setAttribute('aria-selected', 'false');
  });
  if (element) { element.classList.add('active'); element.setAttribute('aria-selected', 'true'); }
  currentFilter = status;
  torrentPage = 1;
  requestGeneration++;
  clearSelection();
  loadTorrents();
}
function onTorrentSearchInput() {
  const nextSearch = (document.getElementById('torrent-search')?.value || '').trim();
  if (nextSearch !== currentTorrentSearch) clearSelection();
  currentTorrentSearch = nextSearch;
  torrentPage = 1;
  requestGeneration++;
  if (_torrentSearchTimer) clearTimeout(_torrentSearchTimer);
  _torrentSearchTimer = setTimeout(() => { _torrentSearchTimer = null; loadTorrents().catch(() => {}); }, 250);
}
function goToTorrentPage(p) {
  const nextPage = Math.max(1, p);
  if (nextPage !== torrentPage) clearSelection();
  torrentPage = nextPage;
  requestGeneration++;
  loadTorrents();
}
function onPageSizeChange(v) {
  const nextSize = Math.min(Math.max(parseInt(v) || 25, 1), 100);
  if (nextSize !== torrentPageSize || torrentPage !== 1) clearSelection();
  torrentPageSize = nextSize;
  torrentPage = 1;
  requestGeneration++;
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
  if (_selectedIds.size > 0) {
    bar?.classList.add('visible');
    if (count) count.textContent = _selectedIds.size + ' Selected';
  } else {
    bar?.classList.remove('visible');
    if (count) count.textContent = '';
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

// ── Row rendering (one pass: presentation status drives badge/progress/
// action visibility directly, exactly as Dashboard Recent already does) ──
function rowMarkup(t) {
  const presentation = window.DPProcessingPresentation
    ? window.DPProcessingPresentation.presentationStatus(t, t.status)
    : (t.status || '');
  const showRetry = t.status === 'error' && !AUTO_RECOVERING_PRESENTATION.has(presentation);
  const sourceMarkup = window.DPTransferSourcePresentation ? window.DPTransferSourcePresentation.sourceSlot(t.current_source_identity) : '';
  const fileSelectionMarkup = window.DPFileSelection?.chipMarkup?.(t) || '';
  const groupMarkup = window.DPGroupCandidates?.launcherMarkup?.(t, 'compact', 'downloads') || '';
  return `<tr class="dp-downloads-detail-row" data-torrent-id="${t.id}" data-status="${esc(t.status)}" data-presentation-status="${esc(presentation)}" tabindex="0" onclick="if(!dpIsInteractiveRowTarget(event.target))showDetail(${t.id})" onkeydown="if(event.target===this&&(event.key==='Enter'||event.key===' ')){event.preventDefault();showDetail(${t.id})}">
    <td onclick="event.stopPropagation()"><input type="checkbox" class="t-chk" data-id="${t.id}"${_selectedIds.has(stableDownloadId(t.id)) ? ' checked' : ''} onchange="onCheckboxChange(this)"/></td>
    <td>
      <div class="t-name">${esc(t.display_name || t.name) || '(unnamed)'}</div>
      <div class="t-hash">${(t.hash || '').substring(0, 16)}${t.hash ? '…' : ''}</div>
    </td>
    <td class="sz dp-downloads-provider-cell">
      <span class="dp-downloads-provider-block">
        <span class="dp-downloads-provider-line">${sourceMarkup}${providerChip(t)}${fileSelectionMarkup}${groupMarkup}</span>
        <span class="dp-transfer-source-label">${sourceLabel(t.source, t.request_kinds)}</span>
      </span>
      ${t.label ? `<span class="lbl-badge">🏷 ${esc(t.label)}</span>` : ''}
    </td>
    <td data-role="transfer-status">${badge(transferDisplayStatus(t), t)}</td>
    <td data-role="transfer-progress">${progress(t.progress, presentation)}</td>
    <td class="sz">${fmtSize(t.size_bytes)}</td>
    <td class="sz">${dateMarkup(t.created_at)}</td>
    <td onclick="event.stopPropagation()">
      <div class="actions">
        ${PAUSEABLE_PRESENTATION.has(presentation) ? `<button class="btn btn-blue btn-sm" data-default-label="Pause" onclick="event.stopPropagation();pauseT(${t.id},this)">Pause</button>` : ''}
        ${presentation === 'paused' ? `<button class="btn btn-blue btn-sm" data-default-label="Resume" onclick="event.stopPropagation();resumeT(${t.id},this)">Resume</button>` : ''}
        ${showRetry ? `<button class="btn btn-blue btn-sm" data-default-label="Retry" onclick="event.stopPropagation();retryT(${t.id},this)">Retry</button>` : ''}
        <button class="btn btn-danger btn-sm" data-default-label="Remove" onclick="event.stopPropagation();deleteT(${t.id},event,this)">Remove</button>
      </div>
    </td>
  </tr>`;
}

// ── Bounded fetch + render ──────────────────────────────────────────────
async function fetchAndRenderTorrents() {
  // Immutable snapshot of the request this call represents. Captured before
  // the await so a later authoritative-state mutation (page/filter/search/
  // page-size change) cannot retroactively change what this in-flight
  // request "was for".
  const _requestGeneration = requestGeneration;
  const _limit = Math.min(Math.max(parseInt(torrentPageSize) || 25, 1), 100);
  const _offset = (torrentPage - 1) * _limit;
  try {
    const params = new URLSearchParams();
    params.set('limit', String(_limit));
    params.set('offset', String(_offset));
    if (currentFilter) params.set('status', currentFilter);
    if (currentTorrentSearch) params.set('search', currentTorrentSearch);
    const {items, total} = await api('GET', '/torrents?' + params.toString());
    // A newer authoritative intent superseded this request while it was in
    // flight: this response is stale and must not become truth. Do not
    // render it, do not mutate torrentPage, do not replace rows, do not
    // reconcile selection, do not overwrite pagination. The coalesced
    // trailing run already queued behind this one will fetch and render the
    // current state.
    if (_requestGeneration !== requestGeneration) return;
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
      requestGeneration++;
      await fetchAndRenderTorrents();
      return;
    }
    torrentTotal = _total;
    const tb = document.getElementById('t-tbody');
    renderTorrentPagination(torrentTotal, _limit, _offset);
    reconcileDownloadSelection(items);
    if (!items.length) {
      tb.innerHTML = `<tr><td colspan="8"><div class="empty"><div class="empty-icon" aria-hidden="true"></div>${downloadEmptyMessage()}</div></td></tr>`;
      syncDownloadSelectionUi();
    } else {
      tb.innerHTML = items.map(rowMarkup).join('');
      syncDownloadSelectionUi();
    }
  } catch (e) {
    if (_requestGeneration !== requestGeneration) return;
    toast(sanitizeErrorMsg(e.message), 'error');
  }
  document.dispatchEvent(new CustomEvent('debridpulse:downloads-rendered'));
}

async function loadTorrents() {
  await fetchAndRenderTorrents();
  ensureDateMenu();
  if (!busy) {
    const size = measuredSize();
    if (applySize(size)) {
      const focus = captureFocus();
      busy = true;
      try { await fetchAndRenderTorrents(); ensureDateMenu(); }
      finally { busy = false; restoreFocus(focus); }
    }
  }
}
loadTorrents = coalesceAsync(loadTorrents);

// ── Row/bulk actions ─────────────────────────────────────────────────────
// A confirmed removal re-renders the list, so the control that started it is gone or replaced. Once the
// operation settles, focus goes to a deliberate surviving control and never stays on <body>: the retry
// button after a failure, otherwise the list toolbar's search field. The successor is deliberately NOT a
// row -- rows are replaced wholesale by every refresh (including the debounced capacity refresh that
// follows a shrinking table), the toolbar is not. Focus the operator moved elsewhere in the meantime is
// left alone.
function settleRemovalFocus(button) {
  const active = document.activeElement;
  if (active && active !== document.body && active.isConnected) return;
  const retry = button?.isConnected && !button.disabled && button.getClientRects().length ? button : null;
  (retry || document.getElementById('torrent-search'))?.focus();
}
async function deleteT(id, eventObj, button) {
  eventObj?.stopPropagation();
  const confirmedIds = await confirmDownloadRemoval([id]);
  if (!confirmedIds) return;
  const targetId = confirmedIds[0];
  setButtonPending(button, true, 'Deleting…');
  try {
    await api('DELETE', `/torrents/${targetId}?from_alldebrid=true`);
    toast('Deleted', 'success');
    await loadTorrents();
    loadStats();
  } catch (e) { toast(sanitizeErrorMsg(e.message), 'error'); }
  finally { setButtonPending(button, false); settleRemovalFocus(button); }
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
    confirmLabel: 'Remove', cancelLabel: 'Cancel', tone: 'danger',
  });
  return confirmed ? stableIds : null;
}
async function bulkAction(action, button) {
  if (!_selectedIds.size) return;
  let ids = [..._selectedIds];
  if (action === 'delete') {
    const confirmedIds = await confirmDownloadRemoval(ids);
    if (!confirmedIds) return;
    ids = confirmedIds;
  }
  const pendingLabels = {delete: 'Deleting…', reset: 'Resetting…', pause: 'Pausing…', resume: 'Resuming…'};
  setButtonPending(button, true, pendingLabels[action] || 'Working…');
  try {
    const r = await api('POST', '/torrents/bulk', {ids, action});
    toast(`Done: ${r.ok} ok, ${r.failed} failed`, r.failed ? 'warn' : 'success');
    if (action === 'delete') { await loadTorrents(); } else { clearSelection(); loadTorrents(); }
    loadStats();
  } catch (e) { toast(e.message, 'error'); }
  finally {
    setButtonPending(button, false);
    if (action === 'delete') settleRemovalFocus(button);
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
    await api('PUT', `/torrents/${id}/label`, {label: label.trim(), priority: 0});
    toast('Label updated', 'success');
    loadTorrents();
  } catch (e) { toast(e.message, 'error'); }
}

// ── Init/wiring ──────────────────────────────────────────────────────────
function init() {
  ensureDateMenu();
  observeResize();
  window.addEventListener('resize', scheduleCapacityCheck, {passive: true});
  document.addEventListener('debridpulse:navigation', e => {
    if (e.detail?.view === 'torrents') { observeResize(); scheduleCapacityCheck(); }
  });
}

window.loadTorrents = loadTorrents;
window.updateDownloadsTrackedCopy = updateDownloadsTrackedCopy;
window.setFilter = setFilter;
window.onTorrentSearchInput = onTorrentSearchInput;
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
// the pause shim toggles). No other member is exposed here -- there is no
// other real runtime consumer of Downloads' internal state.
window.DPDownloads = Object.freeze({scheduleCapacityCheck});

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
else init();
})();
