/* DebridPulse 1.0.12 UI Correction Batch 1.
 * Canonical post-app runtime for the cross-surface correction contract.
 * Loaded by the provider-status bootstrap after app.js has established the
 * shared transfer/settings bindings. It replaces the relevant global owners
 * rather than running a second transfer state machine.
 */
(function () {
  'use strict';

  const HOST_ASSETS = Object.freeze([
    ['1fichier.com', '1fichier.png'], ['4shared.com', '4shared.png'],
    ['alfafile.net', 'alfafile.png'], ['fastbit.cc', 'fastbit.png'],
    ['file-upload.com', 'file-upload.png'], ['fileal.com', 'fileal.png'],
    ['filedot.to', 'filedot.png'], ['filefactory.com', 'filefactory.png'],
    ['filespace.com', 'filespace.png'], ['gigapeta.com', 'gigapeta.png'],
    ['hexupload.net', 'hexupload.png'], ['hitfile.net', 'hitfile.png'],
    ['isra.cloud', 'isra-cloud.png'], ['katfile.com', 'katfile.png'],
    ['mediafire.com', 'mediafire.png'], ['mega.nz', 'mega.svg'],
    ['modsbase.com', 'modsbase.png'], ['mp4upload.com', 'mp4upload.png'],
    ['prefiles.com', 'prefiles.png'], ['rapidgator.net', 'rapidgator.png'],
    ['scribd.com', 'scribd.png'], ['sendit.cloud', 'sendit.png'],
    ['simfileshare.net', 'simfileshare.png'], ['streamtape.com', 'streamtape.png'],
    ['turbobit.net', 'turbobit.png'], ['upload42.com', 'upload42.png'],
    ['uploadhaven.com', 'uploadhaven.png'], ['uploadrar.com', 'uploadrar.png'],
    ['world-files.com', 'world-files.png'],
  ]);

  const DATE_PREF_KEY = 'debridpulse.downloads.date-presentation.v1';
  const DATE_FORMATS = new Set(['friendly', 'us', 'international', 'iso']);
  let datePreference = loadDatePreference();
  let capacityTimer = null;
  let resizeObserver = null;
  let mutationObserver = null;
  let capacityBusy = false;

  function normalizeHost(value) {
    return String(value || '').trim().toLowerCase().replace(/^www\./, '').replace(/\.$/, '');
  }

  function domainMatches(host, domain) {
    return host === domain || host.endsWith('.' + domain);
  }

  function hostAsset(host) {
    const normalized = normalizeHost(host);
    const match = HOST_ASSETS.find(([domain]) => domainMatches(normalized, domain));
    return match ? `/icons/hosts/${match[1]}` : '';
  }

  function lucideSource(kind) {
    const paths = {
      link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
      magnet: '<path d="m6 15-4-4 4-4"/><path d="m18 15 4-4-4-4"/><path d="M2 11h5a5 5 0 0 1 5 5v0a5 5 0 0 0 5 5h5"/>',
      torrent_file: '<path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/><path d="M12 18v-6"/><path d="m9 15 3 3 3-3"/>',
    };
    const markup = paths[kind] || paths.link;
    return `<svg class="lucide dp-source-fallback" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${markup}</svg>`;
  }

  function sourceIconMarkup(identity) {
    const kind = String(identity?.kind || 'link').toLowerCase();
    if (kind === 'magnet') return lucideSource('magnet');
    if (kind === 'torrent_file') return lucideSource('torrent_file');
    if (kind === 'host') {
      const asset = hostAsset(identity.host);
      if (asset) return `<img class="dp-source-host-logo" src="${esc(asset)}" alt="" aria-hidden="true">`;
    }
    return lucideSource('link');
  }

  function sourceSlot(identity) {
    return `<span class="dp-source-icon-slot" title="${esc(identity?.host || identity?.kind || 'Source')}">${sourceIconMarkup(identity)}</span>`;
  }

  function correctedToastMessage(message) {
    const text = String(message ?? '');
    if (/^Line \d+: enter an HTTP\(S\) link or magnet URI$/i.test(text)) {
      return 'DebridPulse stared at that for a moment. It is not a link, magnet, or torrent.';
    }
    if (text === 'Checking AllDebrid for ready torrents…' || text === 'Checking AllDebrid for ready torrents...') {
      return 'Checking transfers for recoverable work…';
    }
    return text;
  }

  function toastDuration(message, type = 'info') {
    const words = String(message || '').trim().split(/\s+/).filter(Boolean).length;
    const chars = String(message || '').length;
    const floor = ['warn', 'warning', 'error'].includes(String(type).toLowerCase()) ? 4500 : 3500;
    const reading = Math.round((words / 210) * 60000 + Math.min(1600, chars * 9));
    return Math.max(floor, Math.min(12000, reading));
  }

  function adaptiveToast(message, type = 'info') {
    const msg = correctedToastMessage(message);
    const root = document.getElementById('toasts');
    if (!root) return;
    const node = document.createElement('div');
    node.className = `toast ${String(type || 'info')}`;
    node.tabIndex = 0;
    node.setAttribute('role', ['error', 'warn', 'warning'].includes(type) ? 'alert' : 'status');
    const body = document.createElement('span');
    body.className = 'dp-toast-message';
    body.textContent = msg;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'dp-toast-close';
    close.setAttribute('aria-label', 'Dismiss notification');
    close.textContent = '×';
    node.append(body, close);
    root.appendChild(node);

    let remaining = toastDuration(msg, type);
    let started = performance.now();
    let timer = null;
    const remove = () => { if (node.isConnected) node.remove(); };
    const start = () => {
      started = performance.now();
      timer = window.setTimeout(remove, remaining);
    };
    const pause = () => {
      if (timer == null) return;
      window.clearTimeout(timer);
      timer = null;
      remaining = Math.max(0, remaining - (performance.now() - started));
    };
    const resume = () => { if (timer == null && remaining > 0) start(); };
    close.addEventListener('click', remove);
    node.addEventListener('mouseenter', pause);
    node.addEventListener('mouseleave', resume);
    node.addEventListener('focusin', pause);
    node.addEventListener('focusout', event => {
      if (!node.contains(event.relatedTarget)) resume();
    });
    start();
  }

  try { toast = adaptiveToast; } catch (_) {}
  window.DPToastDuration = toastDuration;

  function recentRowMarkup(t) {
    const progress = Number(t.progress) || 0;
    const active = ['downloading', 'queued'].includes(String(t.status || '').toLowerCase());
    const provider = typeof providerChip === 'function' ? providerChip(t) : '';
    return `
      <div class="dash-row recent-row" data-transfer-id="${esc(t.id)}">
        <div class="t-icon">${typeof iconFor === 'function' ? iconFor(t.status) : ''}</div>
        <div class="t-main">
          <div class="t-name" title="${esc(t.display_name || t.filename || '')}">${esc(t.display_name || t.filename || 'Transfer')}</div>
          <div class="dash-row-bar-slot" aria-hidden="true">
            <div class="dash-row-bar${active ? '' : ' is-empty'}"><div style="width:${Math.max(0, Math.min(100, progress))}%"></div></div>
          </div>
          <div class="dp-transfer-provider-meta">${sourceSlot(t.current_source_identity)}${provider}</div>
        </div>
        <div class="t-status">${typeof badge === 'function' ? badge(t.status) : esc(t.status || '')}</div>
        <div class="t-actions">${typeof actionButtons === 'function' ? actionButtons(t, true) : ''}</div>
      </div>`;
  }

  async function correctedLoadRecent() {
    try {
      const rows = await api('GET', '/torrents?limit=8');
      const list = Array.isArray(rows) ? rows : (rows?.items || []);
      const node = document.getElementById('recent-list');
      if (!node) return list;
      node.innerHTML = list.length ? list.map(recentRowMarkup).join('') : '<div class="empty-state">No recent downloads</div>';
      document.dispatchEvent(new CustomEvent('debridpulse:dashboard-recent-rendered', {detail:{count:list.length}}));
      return list;
    } catch (error) {
      const node = document.getElementById('recent-list');
      if (node) node.innerHTML = `<div class="empty-state">${esc(error.message || 'Unable to load recent downloads')}</div>`;
      return null;
    }
  }

  try { loadRecent = typeof coalesceAsync === 'function' ? coalesceAsync(correctedLoadRecent) : correctedLoadRecent; } catch (_) {}

  function loadDatePreference() {
    try {
      const parsed = JSON.parse(localStorage.getItem(DATE_PREF_KEY) || '{}');
      return {
        format: DATE_FORMATS.has(parsed.format) ? parsed.format : 'friendly',
        hour12: parsed.hour12 === true,
      };
    } catch (_) {
      return {format: 'friendly', hour12: false};
    }
  }

  function saveDatePreference() {
    try { localStorage.setItem(DATE_PREF_KEY, JSON.stringify(datePreference)); } catch (_) {}
  }

  function timeZone() {
    try { return String(settingsData?.timezone || 'UTC'); } catch (_) { return 'UTC'; }
  }

  function parts(date) {
    const formatter = new Intl.DateTimeFormat('en-US', {
      timeZone: timeZone(), year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: datePreference.hour12,
    });
    return Object.fromEntries(formatter.formatToParts(date).filter(p => p.type !== 'literal').map(p => [p.type, p.value]));
  }

  function dateKey(p) { return `${p.year}-${p.month}-${p.day}`; }

  function yesterdayKey(nowParts) {
    const d = new Date(Date.UTC(Number(nowParts.year), Number(nowParts.month) - 1, Number(nowParts.day)) - 86400000);
    return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`;
  }

  function clockText(p) {
    if (datePreference.hour12) return `${p.hour}:${p.minute} ${p.dayPeriod || ''}`.trim();
    return `${p.hour}:${p.minute}`;
  }

  function formatDownloadsDate(value) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    const p = parts(date);
    const clock = clockText(p);
    if (datePreference.format === 'us') return `${p.month}/${p.day}/${p.year} ${clock}`;
    if (datePreference.format === 'international') return `${p.day}/${p.month}/${p.year} ${clock}`;
    if (datePreference.format === 'iso') return `${p.year}-${p.month}-${p.day} ${clock}`;
    const now = parts(new Date());
    if (dateKey(p) === dateKey(now)) return `Today ${clock}`;
    if (dateKey(p) === yesterdayKey(now)) return `Yesterday ${clock}`;
    const month = new Intl.DateTimeFormat('en-US', {timeZone: timeZone(), month: 'short'}).format(date);
    return Number(p.year) === Number(now.year) ? `${month} ${Number(p.day)} ${clock}` : `${month} ${Number(p.day)}, ${p.year} ${clock}`;
  }

  function exactDownloadsDate(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat('en-US', {
      timeZone: timeZone(), year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short', hour12: false,
    }).format(date);
  }

  function ensureDateMenu() {
    const th = Array.from(document.querySelectorAll('#view-torrents th')).find(node => node.textContent.trim().startsWith('Date'));
    if (!th || th.querySelector('.dp-date-menu-trigger')) return;
    th.textContent = '';
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
    menu.innerHTML = `
      <div class="dp-date-menu-title">Date format</div>
      ${[['friendly','Friendly'],['us','US'],['international','International'],['iso','ISO']].map(([id,labelText]) => `<button type="button" role="menuitemradio" data-date-format="${id}">${labelText}</button>`).join('')}
      <div class="dp-date-menu-title">Time style</div>
      <button type="button" role="menuitemradio" data-hour12="false">24-hour</button>
      <button type="button" role="menuitemradio" data-hour12="true">12-hour</button>`;
    th.append(label, trigger, menu);
    const sync = () => {
      menu.querySelectorAll('[data-date-format]').forEach(btn => btn.setAttribute('aria-checked', btn.dataset.dateFormat === datePreference.format ? 'true' : 'false'));
      menu.querySelectorAll('[data-hour12]').forEach(btn => btn.setAttribute('aria-checked', String(datePreference.hour12) === btn.dataset.hour12 ? 'true' : 'false'));
    };
    sync();
    trigger.addEventListener('click', event => {
      event.stopPropagation();
      menu.hidden = !menu.hidden;
      trigger.setAttribute('aria-expanded', menu.hidden ? 'false' : 'true');
    });
    menu.addEventListener('click', event => {
      const button = event.target.closest('button');
      if (!button) return;
      if (button.dataset.dateFormat) datePreference.format = button.dataset.dateFormat;
      if (button.dataset.hour12 != null) datePreference.hour12 = button.dataset.hour12 === 'true';
      saveDatePreference(); sync(); menu.hidden = true; trigger.setAttribute('aria-expanded', 'false');
      try { loadTorrents(); } catch (_) {}
    });
    document.addEventListener('click', event => {
      if (!th.contains(event.target)) { menu.hidden = true; trigger.setAttribute('aria-expanded', 'false'); }
    });
  }

  function pageSizeFromGeometry() {
    if (!window.matchMedia('(min-width: 701px)').matches) return null;
    const wrap = document.querySelector('#view-torrents .table-wrap, #view-torrents .downloads-table-wrap');
    const table = wrap?.querySelector('table');
    const row = table?.querySelector('tbody tr');
    const head = table?.querySelector('thead');
    if (!wrap || !row || !head) return null;
    const rowHeight = row.getBoundingClientRect().height;
    if (!(rowHeight > 0)) return null;
    const bodyBudget = wrap.getBoundingClientRect().height - head.getBoundingClientRect().height - 2;
    return Math.max(1, Math.min(100, Math.floor(bodyBudget / rowHeight)));
  }

  function scheduleCapacityCheck() {
    if (capacityTimer != null) window.clearTimeout(capacityTimer);
    capacityTimer = window.setTimeout(async () => {
      capacityTimer = null;
      if (capacityBusy || !document.getElementById('view-torrents')?.classList.contains('active')) return;
      const measured = pageSizeFromGeometry();
      if (!measured || measured === torrentPageSize) return;
      capacityBusy = true;
      const oldOffset = Math.max(0, (torrentPage - 1) * torrentPageSize);
      torrentPageSize = measured;
      torrentPage = Math.floor(oldOffset / measured) + 1;
      try { await loadTorrents(); } finally { capacityBusy = false; }
    }, 120);
  }

  function correctedPagination() {
    const node = document.getElementById('torrent-pagination');
    if (!node) return;
    const pages = Math.max(1, Math.ceil((Number(torrentTotal) || 0) / Math.max(1, torrentPageSize)));
    torrentPage = Math.min(Math.max(1, torrentPage), pages);
    const prev = torrentPage > 1 ? `<button type="button" class="btn btn-ghost btn-sm" onclick="torrentPage--;loadTorrents()" aria-label="Previous page">‹</button>` : '<span class="dp-pager-placeholder" aria-hidden="true"></span>';
    const next = torrentPage < pages ? `<button type="button" class="btn btn-ghost btn-sm" onclick="torrentPage++;loadTorrents()" aria-label="Next page">›</button>` : '<span class="dp-pager-placeholder" aria-hidden="true"></span>';
    node.innerHTML = `<span class="dp-pager-slot">${prev}</span><span class="dp-pager-current" aria-current="page">${torrentPage}</span><span class="dp-pager-slot">${next}</span>`;
  }

  try { renderTorrentPagination = correctedPagination; } catch (_) {}

  async function correctedLoadTorrents() {
    const body = document.getElementById('torrent-tbody');
    if (!body) return null;
    const limit = Math.max(1, Math.min(100, Number(torrentPageSize) || 25));
    const offset = Math.max(0, (Math.max(1, Number(torrentPage) || 1) - 1) * limit);
    const params = new URLSearchParams({limit:String(limit), offset:String(offset)});
    if (currentFilter) params.set('status', currentFilter);
    if (currentTorrentSearch) params.set('search', currentTorrentSearch);
    try {
      const result = await api('GET', `/torrents?${params.toString()}`);
      const items = Array.isArray(result) ? result : (result.items || []);
      torrentTotal = Number(result?.total ?? items.length) || 0;
      reconcileSelectedDownloadIds(items);
      body.innerHTML = items.map(t => {
        const name = t.display_name || t.filename || 'Transfer';
        const created = t.created_at || t.added_at;
        return `<tr data-transfer-id="${esc(t.id)}">
          <td class="select-col"><input type="checkbox" class="download-select" data-id="${esc(t.id)}" ${selectedDownloadIds.has(Number(t.id)) ? 'checked' : ''} aria-label="Select ${esc(name)}"></td>
          <td class="name-col"><div class="file-name" title="${esc(name)}">${esc(name)}</div><div class="dp-transfer-provider-meta">${typeof providerChip === 'function' ? providerChip(t) : ''}</div></td>
          <td>${typeof badge === 'function' ? badge(t.status) : esc(t.status || '')}</td>
          <td class="progress-col"><div class="progress"><div style="width:${Math.max(0, Math.min(100, Number(t.progress) || 0))}%"></div></div><span>${Math.round(Number(t.progress) || 0)}%</span></td>
          <td class="date-col" title="${esc(exactDownloadsDate(created))}" tabindex="0">${esc(formatDownloadsDate(created))}</td>
          <td class="actions-col">${typeof actionButtons === 'function' ? actionButtons(t, false) : ''}</td>
        </tr>`;
      }).join('');
      if (!items.length) body.innerHTML = '<tr><td colspan="6" class="empty-state">No downloads found</td></tr>';
      syncDownloadSelectionUi();
      correctedPagination();
      ensureDateMenu();
      scheduleCapacityCheck();
      return result;
    } catch (error) {
      body.innerHTML = `<tr><td colspan="6" class="empty-state">${esc(error.message || 'Unable to load downloads')}</td></tr>`;
      return null;
    }
  }

  try { loadTorrents = typeof coalesceAsync === 'function' ? coalesceAsync(correctedLoadTorrents) : correctedLoadTorrents; } catch (_) {}

  function pauseGlyph() {
    return '<svg class="lucide" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>';
  }

  function ensurePauseUi() {
    const quickHeader = document.querySelector('#view-dashboard .quick-add .card-header, #view-dashboard [data-quick-add] .card-header');
    if (quickHeader && !quickHeader.querySelector('.dp-global-pause-center')) {
      const center = document.createElement('div');
      center.className = 'dp-global-pause-center';
      center.innerHTML = `<div class="dp-global-pause-title">${pauseGlyph()}<span>PROCESSING PAUSED</span></div><div class="dp-global-pause-copy">New downloads can still be added. They will remain queued until processing is resumed.</div>`;
      const actions = quickHeader.querySelector('.card-actions, .header-actions');
      quickHeader.insertBefore(center, actions || null);
    }

    const downloadsCard = document.querySelector('#view-torrents .card');
    if (downloadsCard && !downloadsCard.querySelector('.dp-downloads-pause-shim')) {
      const shim = document.createElement('div');
      shim.className = 'dp-downloads-pause-shim';
      shim.textContent = 'Processing paused. Queued and newly added downloads will not start until processing is resumed.';
      const header = downloadsCard.querySelector(':scope > .card-header');
      if (header) header.insertAdjacentElement('afterend', shim);
    }
  }

  function removeImportAction() {
    document.querySelectorAll('#view-dashboard button').forEach(button => {
      const label = button.textContent.trim().toLowerCase();
      const handler = String(button.getAttribute('onclick') || '').toLowerCase();
      if (label === 'import' || handler.includes('importexisting')) button.remove();
    });
  }

  function syncPauseUi() {
    ensurePauseUi(); removeImportAction();
    let paused = false;
    try { paused = !!settingsData?.paused; } catch (_) {}
    document.querySelector('.dp-global-pause-center')?.classList.toggle('is-visible', paused);
    document.querySelector('.dp-downloads-pause-shim')?.classList.toggle('is-visible', paused);
    scheduleCapacityCheck();
  }

  try {
    const originalTopbar = renderTopbarActions;
    renderTopbarActions = function () { const result = originalTopbar.apply(this, arguments); syncPauseUi(); return result; };
  } catch (_) {}

  function patchSettingsTerminology() {
    document.querySelectorAll('.dp-settings-group-card > .card-header .card-title').forEach(title => {
      if (title.textContent.trim() === 'General Sources') title.textContent = 'Direct Sources';
    });
  }

  document.addEventListener('debridpulse:settings-rendered', patchSettingsTerminology);

  function explicitProviderHeading() {
    const footer = document.querySelector('.sidebar-footer');
    if (!footer || footer.querySelector('.dp-provider-status-heading')) return;
    const heading = document.createElement('div');
    heading.className = 'dp-provider-status-heading';
    heading.textContent = 'Provider Status';
    footer.insertBefore(heading, document.getElementById('premium-row') || footer.firstChild);
  }

  function observeDownloadsGeometry() {
    const card = document.querySelector('#view-torrents .card');
    if (!card) return;
    if ('ResizeObserver' in window) {
      resizeObserver?.disconnect();
      resizeObserver = new ResizeObserver(scheduleCapacityCheck);
      resizeObserver.observe(card);
    }
    mutationObserver?.disconnect();
    mutationObserver = new MutationObserver(scheduleCapacityCheck);
    mutationObserver.observe(card, {attributes:true, childList:true, subtree:true, attributeFilter:['class','style','hidden']});
  }

  function init() {
    explicitProviderHeading();
    ensurePauseUi();
    removeImportAction();
    patchSettingsTerminology();
    ensureDateMenu();
    syncPauseUi();
    observeDownloadsGeometry();
    window.addEventListener('resize', scheduleCapacityCheck, {passive:true});
    document.addEventListener('debridpulse:dashboard-recent-rendered', removeImportAction);
    document.addEventListener('debridpulse:downloads-selection-changed', scheduleCapacityCheck);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once:true});
  else init();

  window.DPUICorrectionBatch1 = Object.freeze({
    hostAsset, sourceIconMarkup, formatDownloadsDate, toastDuration,
    recalculateDownloadsCapacity: scheduleCapacityCheck,
  });
})();
