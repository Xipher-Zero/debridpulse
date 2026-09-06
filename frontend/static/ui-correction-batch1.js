/* DebridPulse 1.0.12 UI Correction Batch 1.
 * Cross-surface correction owner for presentation-only Batch 1 contracts.
 * Transfer lifecycle, selection, filtering, routing and state authority remain canonical.
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
  const DESKTOP_QUERY = '(min-width: 701px)';
  let datePreference = loadDatePreference();
  let capacityTimer = null;
  let resizeObserver = null;
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

  function sourceSvg(kind) {
    const paths = {
      link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
      magnet: '<path d="M6 4v7a6 6 0 0 0 12 0V4"/><path d="M6 8h4"/><path d="M14 8h4"/>',
      torrent_file: '<path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/><path d="M12 12v6"/><path d="m9 15 3 3 3-3"/>',
    };
    return `<svg class="lucide dp-source-fallback" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${paths[kind] || paths.link}</svg>`;
  }

  function sourceIconMarkup(identity) {
    const kind = String(identity?.kind || 'link').trim().toLowerCase();
    if (kind === 'magnet') return sourceSvg('magnet');
    if (kind === 'torrent_file') return sourceSvg('torrent_file');
    if (kind === 'host') {
      const asset = hostAsset(identity?.host);
      if (asset) return `<img class="dp-source-host-logo" src="${esc(asset)}" alt="" aria-hidden="true">`;
    }
    return sourceSvg('link');
  }

  function sourceSlot(identity) {
    const label = identity?.kind === 'host' && identity?.host
      ? String(identity.host)
      : identity?.kind === 'magnet'
        ? 'Magnet source'
        : identity?.kind === 'torrent_file'
          ? 'Torrent file source'
          : 'Link source';
    return `<span class="dp-source-icon-slot" title="${esc(label)}" aria-label="${esc(label)}">${sourceIconMarkup(identity)}</span>`;
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
    const text = String(message || '').trim();
    const words = text ? text.split(/\s+/).length : 0;
    const chars = text.length;
    const severity = String(type || 'info').toLowerCase();
    const floor = ['warn', 'warning', 'error'].includes(severity) ? 4500 : 3500;
    const reading = 3000 + Math.round(words * 230) + Math.min(1700, chars * 7);
    return Math.max(floor, Math.min(12000, reading));
  }

  function adaptiveToast(message, type = 'info') {
    const msg = correctedToastMessage(message);
    const root = document.getElementById('toasts');
    if (!root) return null;

    const node = document.createElement('div');
    node.className = `toast ${String(type || 'info')}`;
    node.tabIndex = 0;
    node.setAttribute('role', ['warn', 'warning', 'error'].includes(String(type).toLowerCase()) ? 'alert' : 'status');

    const body = document.createElement('span');
    body.className = 'dp-toast-message dp-toast-copy';
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
    const remove = () => {
      if (timer != null) window.clearTimeout(timer);
      timer = null;
      if (node.isConnected) node.remove();
    };
    const start = () => {
      if (remaining <= 0) return remove();
      started = performance.now();
      timer = window.setTimeout(remove, remaining);
    };
    const pause = () => {
      if (timer == null) return;
      window.clearTimeout(timer);
      timer = null;
      remaining = Math.max(0, remaining - (performance.now() - started));
    };
    const resume = () => {
      if (timer == null && remaining > 0 && node.isConnected) start();
    };

    close.addEventListener('click', remove);
    node.addEventListener('mouseenter', pause);
    node.addEventListener('mouseleave', resume);
    node.addEventListener('focusin', pause);
    node.addEventListener('focusout', event => {
      if (!node.contains(event.relatedTarget)) resume();
    });
    start();
    return node;
  }

  try { toast = adaptiveToast; } catch (_) {}
  window.DPToastDuration = toastDuration;

  async function correctedLoadRecent() {
    try {
      const recentLimit = typeof dashboardRecentLimit === 'function' ? dashboardRecentLimit() : 6;
      if (typeof _dashboardRecentFitLimit !== 'undefined') _dashboardRecentFitLimit = recentLimit;
      const response = await api('GET', `/torrents?limit=${recentLimit}`);
      const items = Array.isArray(response?.items) ? response.items : [];
      const tb = document.getElementById('dash-tbody');
      if (!tb) return response;

      if (!items.length) {
        tb.innerHTML = '<tr><td colspan="6"><div class="empty"><div class="empty-icon" aria-hidden="true"></div>No downloads yet. Add a link, magnet, or torrent file to get started.</div></td></tr>';
        const countEl = document.getElementById('dash-activity-count');
        if (countEl) countEl.textContent = 'Recent transfer history';
        document.dispatchEvent(new CustomEvent('debridpulse:dashboard-recent-rendered'));
        return response;
      }

      const countEl = document.getElementById('dash-activity-count');
      if (countEl) countEl.textContent = items.length + ' most recent download' + (items.length === 1 ? '' : 's');

      tb.innerHTML = items.map(t => {
        const pctValue = t.progress != null ? Math.round(t.progress) : 0;
        const showProgress = String(t.status || '').toLowerCase() === 'downloading';
        return `<tr data-torrent-id="${t.id}" data-status="${esc(t.status)}" onclick="showDetail(${t.id})" style="cursor:pointer">
          <td>
            <div class="t-name" title="${esc(t.name) || ''}">${esc(t.name) || '(unnamed)'}</div>
            <div class="dash-row-bar-slot" aria-hidden="true">
              <div class="dash-row-bar${showProgress ? '' : ' is-empty'}"><div class="dash-row-bar-fill" style="width:${Math.max(0, Math.min(100, pctValue))}%;background:var(--blue)"></div></div>
            </div>
            <div class="dp-transfer-provider-meta">${sourceSlot(t.current_source_identity)}${providerChip(t)}</div>
          </td>
          <td data-role="transfer-status">${badge(transferDisplayStatus(t), t)}</td>
          <td data-role="transfer-progress">${progress(t.progress, t.status)}</td>
          <td class="sz">${fmtSize(t.size_bytes)}</td>
          <td class="sz">${fmtDate(t.created_at)}</td>
          <td onclick="event.stopPropagation()">
            <div class="actions">
              ${t.status === 'downloading' || t.status === 'queued' ? `<button class="btn btn-blue btn-sm" data-default-label="Pause" onclick="event.stopPropagation();pauseT(${t.id},this)" title="Pause this download">Pause</button>` : ''}
              ${t.status === 'paused' ? `<button class="btn btn-blue btn-sm" data-default-label="Resume" onclick="event.stopPropagation();resumeT(${t.id},this)" title="Resume this download">Resume</button>` : ''}
            </div>
          </td>
        </tr>`;
      }).join('');

      requestAnimationFrame(() => {
        if (!document.getElementById('view-dashboard')?.classList.contains('active')) return;
        if (typeof dashboardRecentLimit !== 'function') return;
        const fitted = dashboardRecentLimit();
        if (typeof _dashboardRecentFitLimit !== 'undefined' && fitted !== _dashboardRecentFitLimit) {
          _dashboardRecentFitLimit = fitted;
          loadRecent().catch(() => {});
        }
      });
      document.dispatchEvent(new CustomEvent('debridpulse:dashboard-recent-rendered'));
      return response;
    } catch (error) {
      console.error(error);
      document.dispatchEvent(new CustomEvent('debridpulse:dashboard-recent-rendered'));
      return null;
    }
  }

  try {
    loadRecent = typeof coalesceAsync === 'function' ? coalesceAsync(correctedLoadRecent) : correctedLoadRecent;
  } catch (_) {}

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

  function applicationTimeZone() {
    try {
      const value = String(settingsData?.timezone || '').trim();
      return value || 'UTC';
    } catch (_) {
      return 'UTC';
    }
  }

  function dateParts(date) {
    const formatter = new Intl.DateTimeFormat('en-US', {
      timeZone: applicationTimeZone(),
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
      hour12: datePreference.hour12,
    });
    return Object.fromEntries(
      formatter.formatToParts(date).filter(part => part.type !== 'literal').map(part => [part.type, part.value])
    );
  }

  function dateKey(parts) {
    return `${parts.year}-${parts.month}-${parts.day}`;
  }

  function previousDateKey(parts) {
    const day = new Date(Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day)) - 86400000);
    return `${day.getUTCFullYear()}-${String(day.getUTCMonth() + 1).padStart(2, '0')}-${String(day.getUTCDate()).padStart(2, '0')}`;
  }

  function clockText(parts) {
    if (datePreference.hour12) return `${parts.hour}:${parts.minute} ${parts.dayPeriod || ''}`.trim();
    return `${parts.hour}:${parts.minute}`;
  }

  function formatDownloadsDate(value) {
    if (!value) return '—';
    const date = typeof parseApiDate === 'function' ? parseApiDate(value) : new Date(value);
    if (!date || Number.isNaN(date.getTime())) return String(value);
    const parts = dateParts(date);
    const clock = clockText(parts);
    if (datePreference.format === 'us') return `${parts.month}/${parts.day}/${parts.year} ${clock}`;
    if (datePreference.format === 'international') return `${parts.day}/${parts.month}/${parts.year} ${clock}`;
    if (datePreference.format === 'iso') return `${parts.year}-${parts.month}-${parts.day} ${clock}`;

    const now = dateParts(new Date());
    if (dateKey(parts) === dateKey(now)) return `Today ${clock}`;
    if (dateKey(parts) === previousDateKey(now)) return `Yesterday ${clock}`;
    const month = new Intl.DateTimeFormat('en-US', {timeZone: applicationTimeZone(), month: 'short'}).format(date);
    return Number(parts.year) === Number(now.year)
      ? `${month} ${Number(parts.day)} ${clock}`
      : `${month} ${Number(parts.day)}, ${parts.year} ${clock}`;
  }

  function exactDownloadsDate(value) {
    if (!value) return '';
    const date = typeof parseApiDate === 'function' ? parseApiDate(value) : new Date(value);
    if (!date || Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat('en-US', {
      timeZone: applicationTimeZone(),
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      timeZoneName: 'short', hour12: false,
    }).format(date);
  }

  function downloadsDateMarkup(value) {
    return `<span class="dp-downloads-date-value" tabindex="0" title="${esc(exactDownloadsDate(value))}">${esc(formatDownloadsDate(value))}</span>`;
  }

  function ensureDateMenu() {
    const table = document.querySelector('#view-torrents .dp-downloads-table-wrap table');
    const th = table ? Array.from(table.querySelectorAll('thead th')).find(node => node.textContent.trim() === 'Added' || node.textContent.trim() === 'Date') : null;
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
      ${[['friendly', 'Friendly'], ['us', 'US'], ['international', 'International'], ['iso', 'ISO']]
        .map(([id, text]) => `<button type="button" role="menuitemradio" data-date-format="${id}">${text}</button>`).join('')}
      <div class="dp-date-menu-title">Time style</div>
      <button type="button" role="menuitemradio" data-hour12="false">24-hour</button>
      <button type="button" role="menuitemradio" data-hour12="true">12-hour</button>`;

    const sync = () => {
      menu.querySelectorAll('[data-date-format]').forEach(button => {
        button.setAttribute('aria-checked', button.dataset.dateFormat === datePreference.format ? 'true' : 'false');
      });
      menu.querySelectorAll('[data-hour12]').forEach(button => {
        button.setAttribute('aria-checked', String(datePreference.hour12) === button.dataset.hour12 ? 'true' : 'false');
      });
    };

    const close = () => {
      menu.hidden = true;
      trigger.setAttribute('aria-expanded', 'false');
    };

    th.append(label, trigger, menu);
    sync();

    trigger.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      menu.hidden = !menu.hidden;
      trigger.setAttribute('aria-expanded', menu.hidden ? 'false' : 'true');
      if (!menu.hidden) menu.querySelector('[aria-checked="true"]')?.focus();
    });

    menu.addEventListener('click', event => {
      const button = event.target.closest('button');
      if (!button) return;
      if (button.dataset.dateFormat) datePreference.format = button.dataset.dateFormat;
      if (button.dataset.hour12 != null) datePreference.hour12 = button.dataset.hour12 === 'true';
      saveDatePreference();
      sync();
      close();
      loadTorrents();
    });

    menu.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        close();
        trigger.focus();
      }
    });

    document.addEventListener('click', event => {
      if (!th.contains(event.target)) close();
    });
  }

  function downloadPageSizeFromGeometry() {
    if (!window.matchMedia(DESKTOP_QUERY).matches) return null;
    const view = document.getElementById('view-torrents');
    const wrap = view?.querySelector('.dp-downloads-table-wrap');
    const table = wrap?.querySelector('table');
    const head = table?.querySelector('thead');
    const rows = Array.from(table?.querySelectorAll('tbody tr[data-torrent-id]') || []);
    const pagination = document.getElementById('torrent-pagination');
    const content = document.getElementById('content');
    if (!view || !wrap || !table || !head || !rows.length || !pagination || !content) return null;

    const heights = rows
      .map(row => row.getBoundingClientRect().height)
      .filter(height => Number.isFinite(height) && height > 0);
    if (!heights.length) return null;

    const rowHeight = Math.max(...heights);
    const wrapRect = wrap.getBoundingClientRect();
    const contentRect = content.getBoundingClientRect();
    const paginationHeight = pagination.getBoundingClientRect().height;
    const viewportBottom = Math.min(window.innerHeight, contentRect.bottom);
    const tableBudget = Math.max(0, viewportBottom - wrapRect.top - paginationHeight - 4);
    const bodyBudget = Math.max(0, tableBudget - head.getBoundingClientRect().height);
    const fitted = Math.floor(bodyBudget / rowHeight);
    return Math.max(1, Math.min(100, fitted || 1));
  }

  function setMeasuredPageSize(measured) {
    const currentSize = Math.max(1, Number(torrentPageSize) || 1);
    if (!measured || measured === currentSize) return false;
    const oldOffset = Math.max(0, (Math.max(1, Number(torrentPage) || 1) - 1) * currentSize);
    torrentPageSize = measured;
    torrentPage = Math.floor(oldOffset / measured) + 1;
    return true;
  }

  function scheduleCapacityCheck() {
    if (capacityTimer != null) window.clearTimeout(capacityTimer);
    capacityTimer = window.setTimeout(async () => {
      capacityTimer = null;
      if (capacityBusy || !document.getElementById('view-torrents')?.classList.contains('active')) return;
      const measured = downloadPageSizeFromGeometry();
      if (!setMeasuredPageSize(measured)) return;
      capacityBusy = true;
      try {
        await loadTorrents();
      } finally {
        capacityBusy = false;
      }
    }, 120);
  }

  function correctedRenderTorrentPagination(total, limit, offset) {
    const normalizedTotal = Math.max(0, Number(total) || 0);
    const normalizedLimit = Math.max(1, Number(limit) || 1);
    const normalizedOffset = Math.max(0, Number(offset) || 0);
    const totalPages = Math.max(1, Math.ceil(normalizedTotal / normalizedLimit));
    const current = Math.min(totalPages, Math.floor(normalizedOffset / normalizedLimit) + 1);
    torrentPage = current;

    const info = document.getElementById('torrent-page-info');
    const buttons = document.getElementById('torrent-page-btns');
    if (!info || !buttons) return;

    const from = normalizedTotal === 0 ? 0 : normalizedOffset + 1;
    const to = Math.min(normalizedOffset + normalizedLimit, normalizedTotal);
    info.textContent = typeof downloadPaginationSummary === 'function'
      ? downloadPaginationSummary(normalizedTotal, from, to)
      : `${from}–${to} of ${normalizedTotal}`;

    const icon = name => window.DPIcons && typeof window.DPIcons.svg === 'function' ? window.DPIcons.svg(name) : '';
    const previous = current > 1
      ? `<button type="button" class="dp-pager-btn" aria-label="Previous page" onclick="goToTorrentPage(${current - 1})">${icon('chevronLeft')}</button>`
      : '<span class="dp-pager-placeholder" aria-hidden="true"></span>';
    const next = current < totalPages
      ? `<button type="button" class="dp-pager-btn" aria-label="Next page" onclick="goToTorrentPage(${current + 1})">${icon('chevronRight')}</button>`
      : '<span class="dp-pager-placeholder" aria-hidden="true"></span>';

    buttons.innerHTML = `<span class="dp-pager-slot">${previous}</span><span class="dp-pager-current" aria-current="page" aria-label="Page ${current}, current page">${current}</span><span class="dp-pager-slot">${next}</span>`;
  }

  try { renderTorrentPagination = correctedRenderTorrentPagination; } catch (_) {}

  try {
    onPageSizeChange = function (value) {
      const nextSize = Math.min(Math.max(parseInt(value, 10) || 1, 1), 100);
      const oldSize = Math.max(1, Number(torrentPageSize) || 1);
      const oldOffset = Math.max(0, (Math.max(1, Number(torrentPage) || 1) - 1) * oldSize);
      if (nextSize !== oldSize) clearSelection();
      torrentPageSize = nextSize;
      torrentPage = Math.floor(oldOffset / nextSize) + 1;
      loadTorrents();
    };
  } catch (_) {}

  try {
    const canonicalLoadTorrents = loadTorrents;
    loadTorrents = async function () {
      const previousFmtDate = fmtDate;
      fmtDate = downloadsDateMarkup;
      try {
        return await canonicalLoadTorrents.apply(this, arguments);
      } finally {
        fmtDate = previousFmtDate;
        ensureDateMenu();
        scheduleCapacityCheck();
      }
    };
  } catch (_) {}

  function pauseGlyph() {
    return '<svg class="lucide" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>';
  }

  function ensurePauseUi() {
    const quickHeader = document.querySelector('#view-dashboard .dp-dashboard-quick-add > .card-header');
    if (quickHeader && !quickHeader.querySelector('.dp-global-pause-center')) {
      const center = document.createElement('div');
      center.className = 'dp-global-pause-center';
      center.innerHTML = `<div class="dp-global-pause-title">${pauseGlyph()}<span>PROCESSING PAUSED</span></div><div class="dp-global-pause-copy">New downloads can still be added. They will remain queued until processing is resumed.</div>`;
      const actions = quickHeader.querySelector('.dp-card-header-actions');
      quickHeader.insertBefore(center, actions || null);
    }

    const downloadsCard = document.querySelector('#view-torrents > .card');
    if (downloadsCard && !downloadsCard.querySelector('.dp-downloads-pause-shim')) {
      const shim = document.createElement('div');
      shim.className = 'dp-downloads-pause-shim';
      shim.textContent = 'Processing paused. Queued and newly added downloads will not start until processing is resumed.';
      const header = downloadsCard.querySelector(':scope > .card-header');
      if (header) header.insertAdjacentElement('afterend', shim);
    }
  }

  function removeImportAction() {
    const button = document.getElementById('btn-import-existing');
    if (button) button.remove();
    document.querySelectorAll('#view-dashboard button[onclick*="importExisting"]').forEach(node => node.remove());
    const recover = document.getElementById('btn-recover-all');
    if (recover) recover.title = 'Check transfers for recoverable work';
    const add = document.getElementById('btn-add-transfer');
    if (add) add.title = 'Submit links and magnets for provider routing; when empty, choose a .torrent file';
  }

  function syncPauseUi() {
    ensurePauseUi();
    removeImportAction();
    let paused = false;
    try { paused = !!settingsData?.paused; } catch (_) {}
    document.querySelector('.dp-global-pause-center')?.classList.toggle('is-visible', paused);
    document.querySelector('.dp-downloads-pause-shim')?.classList.toggle('is-visible', paused);
    scheduleCapacityCheck();
  }

  try {
    const canonicalRenderTopbarActions = renderTopbarActions;
    renderTopbarActions = function () {
      const result = canonicalRenderTopbarActions.apply(this, arguments);
      syncPauseUi();
      return result;
    };
  } catch (_) {}

  function patchSettingsTerminology() {
    document.querySelectorAll('#view-settings .card-title').forEach(title => {
      if (title.textContent.trim() === 'General Sources' || title.textContent.trim() === 'General Downloads') {
        title.textContent = 'Direct Sources';
      }
    });
  }

  function observeDownloadsGeometry() {
    const card = document.querySelector('#view-torrents > .card');
    if (!card || !('ResizeObserver' in window)) return;
    resizeObserver?.disconnect();
    resizeObserver = new ResizeObserver(scheduleCapacityCheck);
    [
      card,
      card.querySelector('.dp-downloads-table-wrap'),
      document.getElementById('bulk-bar'),
      document.querySelector('.dp-downloads-pause-shim'),
      document.getElementById('torrent-pagination'),
    ].filter(Boolean).forEach(node => resizeObserver.observe(node));
  }

  function init() {
    ensurePauseUi();
    removeImportAction();
    patchSettingsTerminology();
    ensureDateMenu();
    syncPauseUi();
    observeDownloadsGeometry();
    window.addEventListener('resize', scheduleCapacityCheck, {passive: true});
    if (window.visualViewport) window.visualViewport.addEventListener('resize', scheduleCapacityCheck, {passive: true});
    document.addEventListener('debridpulse:settings-rendered', patchSettingsTerminology);
    document.addEventListener('debridpulse:dashboard-recent-rendered', removeImportAction);
    document.addEventListener('debridpulse:downloads-selection-changed', scheduleCapacityCheck);
    document.addEventListener('debridpulse:navigation', event => {
      if (event.detail?.view === 'torrents') {
        observeDownloadsGeometry();
        scheduleCapacityCheck();
      }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
  else init();

  window.DPUICorrectionBatch1 = Object.freeze({
    hostAsset,
    sourceIconMarkup,
    formatDownloadsDate,
    toastDuration,
    recalculateDownloadsCapacity: scheduleCapacityCheck,
  });
})();
