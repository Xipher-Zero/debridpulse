/* DebridPulse v1.0.11 Downloads presentation runtime.
 * Keeps the legacy transfer loader/backend behavior intact while normalizing
 * the Downloads card header, filter controls, pagination language, row actions,
 * row detail behavior and empty states.
 */
(function () {
  'use strict';

  const DOWNLOAD_FILTERS = [
    {status: '', label: 'All'},
    {status: 'downloading', label: 'Downloading'},
    {status: 'paused', label: 'Paused'},
    {status: 'processing', label: 'Processing'},
    {status: 'ready', label: 'Ready'},
    {status: 'completed', label: 'Done'},
    {status: 'error', label: 'Error'}
  ];

  let lastTrackedTotal = 0;
  let titleObserver = null;
  let statsObserver = null;
  let downloadsEmptyObserver = null;
  let recentEmptyObserver = null;

  function utilitySvg(name) {
    const paths = {
      chevronLeft: '<path d="m15 18-6-6 6-6"/>',
      chevronRight: '<path d="m9 18 6-6-6-6"/>',
      refresh: '<path d="M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5"/><path d="M4 13a8.1 8.1 0 0 0 15.5 2M20 20v-5h-5"/>',
      pause: '<rect x="14" y="3" width="5" height="18" rx="1"/><rect x="5" y="3" width="5" height="18" rx="1"/>',
      play: '<path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"/>',
      trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="m19 6-1 14H6L5 6"/><path d="M10 11v5"/><path d="M14 11v5"/>',
      x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'
    };
    const geometry = paths[name];
    if (!geometry) return '';
    return '<svg class="lucide dp-utility-icon" xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + geometry + '</svg>';
  }

  function setBulkButtonPresentation(button, label, iconName, semanticClass) {
    if (!button || button.dataset.pending === '1' || button.getAttribute('aria-busy') === 'true') return;
    button.removeAttribute('style');
    button.classList.remove('btn-sm', 'btn-blue', 'btn-ghost', 'btn-danger');
    button.classList.add('btn', 'dp-downloads-bulk-action', semanticClass);
    if (semanticClass === 'dp-downloads-bulk-action--delete') button.classList.add('btn-danger');
    button.dataset.defaultLabel = label;
    const expectedIcon = button.querySelector('[data-dp-bulk-icon="' + iconName + '"]');
    const expectedLabel = button.querySelector('[data-dp-bulk-label]');
    if (expectedIcon && expectedLabel && expectedLabel.textContent === label) return;
    button.innerHTML = utilitySvg(iconName).replace('class="lucide dp-utility-icon"', 'class="lucide dp-utility-icon" data-dp-bulk-icon="' + iconName + '"');
    const span = document.createElement('span');
    span.dataset.dpBulkLabel = '1';
    span.textContent = label;
    button.appendChild(span);
  }

  function syncBulkButtonPresentation(bar) {
    if (!bar) return;
    const buttons = Array.from(bar.querySelectorAll('button'));
    const find = needle => buttons.find(button => (button.getAttribute('onclick') || '').includes(needle));
    setBulkButtonPresentation(find("bulkAction('pause'"), 'Pause', 'pause', 'dp-downloads-bulk-action--pause');
    setBulkButtonPresentation(find("bulkAction('resume'"), 'Resume', 'play', 'dp-downloads-bulk-action--resume');
    setBulkButtonPresentation(find("bulkAction('reset'"), 'Reset', 'refresh', 'dp-downloads-bulk-action--reset');
    setBulkButtonPresentation(find("bulkAction('delete'"), 'Delete', 'trash', 'dp-downloads-bulk-action--delete');
    setBulkButtonPresentation(find('clearSelection()'), 'Clear selection', 'x', 'dp-downloads-bulk-action--clear');
  }

  function decorateBulkSelectionToolbar() {
    const bar = document.getElementById('bulk-bar');
    const count = document.getElementById('bulk-count');
    if (!bar || !count) return;
    if (bar.dataset.dpDownloadsBulk === '1') {
      syncBulkButtonPresentation(bar);
      return;
    }
    const buttons = Array.from(bar.querySelectorAll('button'));
    const find = needle => buttons.find(button => (button.getAttribute('onclick') || '').includes(needle));
    const pause = find("bulkAction('pause'");
    const resume = find("bulkAction('resume'");
    const reset = find("bulkAction('reset'");
    const remove = find("bulkAction('delete'");
    const clear = find('clearSelection()');
    if (![pause, resume, reset, remove, clear].every(Boolean)) return;
    bar.classList.add('dp-card', 'dp-downloads-bulk-card');
    const header = document.createElement('div');
    header.className = 'dp-card__header dp-downloads-bulk-toolbar';
    const actions = document.createElement('div');
    actions.className = 'dp-downloads-bulk-actions';
    const separator = document.createElement('span');
    separator.className = 'dp-downloads-bulk-separator';
    separator.setAttribute('aria-hidden', 'true');
    const status = document.createElement('div');
    status.className = 'dp-downloads-bulk-status';
    count.classList.add('dp-downloads-bulk-count');
    actions.append(pause, resume, reset, separator, remove, clear);
    status.appendChild(count);
    header.append(actions, status);
    bar.replaceChildren(header);
    bar.dataset.dpDownloadsBulk = '1';
    syncBulkButtonPresentation(bar);
    new MutationObserver(function () { syncBulkButtonPresentation(bar); })
      .observe(header, {childList: true, subtree: true, characterData: true});
  }

  function filterStatusFromTab(tab) {
    if (!tab) return '';
    if (tab.dataset && typeof tab.dataset.dpStatus === 'string') return tab.dataset.dpStatus;
    const onclick = tab.getAttribute('onclick') || '';
    const match = onclick.match(/setFilter\(this,'([^']*)'\)/);
    return match ? match[1] : '';
  }

  function activeFilterStatus() {
    const active = document.querySelector('#view-torrents .filter-tabs .ftab.active');
    return filterStatusFromTab(active);
  }

  function ensureDownloadFilters() {
    const rail = document.querySelector('#view-torrents .filter-tabs');
    if (!rail) return;

    const existingActive = rail.querySelector('.ftab.active');
    const activeStatus = filterStatusFromTab(existingActive);
    const alreadyCurrent = rail.dataset.dpFilterContract === 'desktop-v24';

    if (!alreadyCurrent) {
      rail.innerHTML = DOWNLOAD_FILTERS.map(function (filter) {
        const active = filter.status === activeStatus;
        return '<div class="ftab' + (active ? ' active' : '') + '"' +
          ' data-dp-status="' + filter.status + '"' +
          ' onclick="setFilter(this,\'' + filter.status + '\')">' + filter.label + '</div>';
      }).join('');
      rail.dataset.dpFilterContract = 'desktop-v24';
    }

    rail.setAttribute('role', 'tablist');
    rail.setAttribute('aria-label', 'Download status filter');
    rail.querySelectorAll('.ftab').forEach(function (tab) {
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', tab.classList.contains('active') ? 'true' : 'false');
    });
  }

  function activeFilterOrSearch() {
    const search = document.getElementById('torrent-search');
    return activeFilterStatus() !== '' || !!(search && search.value.trim());
  }

  function parseCount(value) {
    const normalized = String(value == null ? '' : value).replace(/,/g, '').trim();
    const match = normalized.match(/\d+/);
    if (!match) return null;
    const parsed = Number(match[0]);
    return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : null;
  }

  function trackedTotal() {
    const dashboardTotal = parseCount(document.getElementById('s-total')?.textContent);
    if (dashboardTotal != null) {
      lastTrackedTotal = dashboardTotal;
      return dashboardTotal;
    }

    const title = document.getElementById('torrent-card-title');
    const legacyCount = parseCount(title?.textContent);
    if (legacyCount != null && (!activeFilterOrSearch() || lastTrackedTotal === 0)) {
      lastTrackedTotal = legacyCount;
    }

    return lastTrackedTotal;
  }

  function trackedCopy(count) {
    return count === 1 ? '1 download tracked' : count + ' downloads tracked';
  }

  function decorateDownloadsHeader() {
    const title = document.getElementById('torrent-card-title');
    if (!title) return;

    const count = trackedTotal();
    const copy = trackedCopy(count);
    const existingSubtitle = title.querySelector('.dp-downloads-subtitle');

    if (title.querySelector('.dp-downloads-heading') && existingSubtitle) {
      if (existingSubtitle.textContent !== copy) existingSubtitle.textContent = copy;
      const icon = title.querySelector('.dp-downloads-title-icon');
      if (icon && !/card-download\.svg/.test(icon.getAttribute('src') || '')) {
        icon.setAttribute('src', '/icons/dp/card-download.svg?v=11');
      }
      title.setAttribute('aria-label', 'All Downloads. ' + copy + '.');
      return;
    }

    title.classList.add('dp-downloads-card-title');
    title.setAttribute('aria-label', 'All Downloads. ' + copy + '.');
    title.innerHTML =
      '<img class="dp-icon dp-icon--lg dp-downloads-title-icon" src="/icons/dp/card-download.svg?v=11" alt="" aria-hidden="true">' +
      '<span class="dp-downloads-heading-copy">' +
        '<span class="dp-downloads-heading">All Downloads</span>' +
        '<span class="dp-downloads-subtitle">' + copy + '</span>' +
      '</span>';
  }

  function downloadsEmptyMessage() {
    const search = document.getElementById('torrent-search');
    if (search && search.value.trim()) return 'No downloads match your search.';

    const active = document.querySelector('#view-torrents .filter-tabs .ftab.active');
    const filtered = !!active && filterStatusFromTab(active) !== '';
    if (filtered) return 'No downloads match your current filters.';

    return 'No downloads yet. Add a link, magnet, or torrent file to get started.';
  }

  function normalizeEmptyState(host, copy) {
    if (!host) return;
    const empty = host.querySelector('.empty');
    if (!empty) return;
    if (empty.dataset.dpEmptyCopy === copy) return;

    empty.innerHTML = '<div class="empty-icon" aria-hidden="true"></div>' + copy;
    empty.dataset.dpEmptyCopy = copy;
  }

  function decorateEmptyStates() {
    normalizeEmptyState(document.getElementById('t-tbody'), downloadsEmptyMessage());
    normalizeEmptyState(
      document.getElementById('dash-tbody'),
      'No downloads yet. Add a link, magnet, or torrent file to get started.'
    );
  }

  function normalizeDownloadBadge(row) {
    const badge = row && row.querySelector('.badge');
    if (!badge) return;
    const text = (badge.textContent || '').replace(/^[^A-Za-z0-9]+/, '').trim();
    const desired = badge.classList.contains('badge-completed') ? 'Done' : text;
    if (desired && badge.textContent !== desired) badge.textContent = desired;
    badge.dataset.dpPresentationNormalized = '1';
  }

  function normalizeDownloadActionButton(button) {
    const onclick = (button.getAttribute('onclick') || '').trim();
    if (onclick.includes('showDetail(')) {
      button.remove();
      return;
    }

    /* Do not overwrite live async feedback such as Retrying… / Pausing… while
       the legacy handler owns the button's pending state. */
    if (button.dataset.pending === '1' || button.getAttribute('aria-busy') === 'true') return;

    let label = '';
    if (onclick.includes('pauseT(') || onclick.includes('pauseTorrent(')) label = 'Pause';
    else if (onclick.includes('resumeT(') || onclick.includes('resumeTorrent(')) label = 'Resume';
    else if (onclick.includes('deleteT(') || onclick.includes('deleteTorrent(')) label = 'Remove';
    else if (onclick.includes('retryT(') || onclick.includes('retryTorrent(')) label = 'Retry';
    else if (onclick.includes('downloadNow(')) label = '⬇ Now';

    if (label) {
      button.dataset.defaultLabel = label;
      if (button.textContent !== label) button.textContent = label;
    }
  }

  function rowTargetIsInteractive(row, target) {
    if (!(target instanceof Element)) return false;
    if (target.closest('button, input, a, select, textarea, label, [role="button"]')) return true;
    const cell = target.closest('td');
    return !!cell && row.cells && cell === row.cells[0];
  }

  function normalizeDownloadRow(row) {
    if (!row || !row.matches('tr[data-torrent-id]')) return;

    normalizeDownloadBadge(row);
    row.querySelectorAll('.actions button').forEach(normalizeDownloadActionButton);

    /* Details is a row-level action. Remove inherited name-only activation and
       retire drag/reorder semantics so the row behaves like Recent Activity. */
    row.querySelectorAll('[onclick*="showDetail("]').forEach(function (target) {
      target.removeAttribute('onclick');
    });
    ['draggable', 'ondragstart', 'ondragover', 'ondragleave', 'ondrop'].forEach(function (attribute) {
      row.removeAttribute(attribute);
    });
    row.classList.add('dp-downloads-detail-row');

    if (row.dataset.dpDetailRowBound === '1') return;
    row.dataset.dpDetailRowBound = '1';
    row.tabIndex = 0;

    row.addEventListener('click', function (event) {
      if (rowTargetIsInteractive(row, event.target)) return;
      const id = row.dataset.torrentId;
      if (id && typeof window.showDetail === 'function') window.showDetail(id);
    });

    row.addEventListener('keydown', function (event) {
      if (event.target !== row || (event.key !== 'Enter' && event.key !== ' ')) return;
      event.preventDefault();
      const id = row.dataset.torrentId;
      if (id && typeof window.showDetail === 'function') window.showDetail(id);
    });
  }

  function normalizeDownloadRowActions() {
    document.querySelectorAll('#t-tbody tr[data-torrent-id]').forEach(normalizeDownloadRow);
  }

  function decorateDownloadsStructure() {
    const card = document.querySelector('#view-torrents > .card');
    if (!card) return;

    const tableWrap = card.querySelector('div[style*="overflow-x:auto"]');
    if (tableWrap) tableWrap.classList.add('dp-downloads-table-wrap');

    const pageSize = document.getElementById('torrent-page-size');
    if (pageSize) {
      const wrapper = pageSize.closest('div');
      if (wrapper && wrapper.parentElement?.id === 'torrent-pagination') wrapper.remove();
      else pageSize.remove();
    }

    const search = document.getElementById('torrent-search');
    if (search && search.placeholder !== 'Search downloads…') search.placeholder = 'Search downloads…';

    const refresh = card.querySelector('.card-header button[onclick*="loadTorrents"]');
    if (refresh) {
      refresh.classList.add('dp-downloads-refresh');
      refresh.setAttribute('aria-label', 'Refresh downloads');
      refresh.setAttribute('title', 'Refresh downloads');
      refresh.dataset.defaultLabel = 'Refresh';
      refresh.innerHTML = utilitySvg('refresh') + '<span>Refresh</span>';
    }

    ensureDownloadFilters();
    normalizeDownloadRowActions();
  }

  function syncFilterState() {
    document.querySelectorAll('#view-torrents .filter-tabs .ftab').forEach(function (tab) {
      tab.setAttribute('aria-selected', tab.classList.contains('active') ? 'true' : 'false');
    });
  }

  function searchPaginationSummary(total, from, to) {
    if (total <= 0) return 'No downloads match your search';
    if (total === 1 && from === 1 && to === 1) return 'Showing 1 matching download';
    if (from === 1 && to === total) return 'Showing all ' + total + ' matching downloads';
    return 'Showing ' + from + '–' + to + ' of ' + total + ' matching downloads';
  }

  function filterPaginationSummary(status, total) {
    if (status === '') {
      if (total <= 0) return 'No Items Added Yet';
      return total === 1 ? 'Showing 1 Added Item' : 'Showing ' + total + ' Added Items';
    }
    if (status === 'downloading') {
      if (total <= 0) return 'No Active Downloads';
      return total === 1 ? '1 Active Download' : total + ' Active Downloads';
    }
    if (status === 'paused') {
      if (total <= 0) return 'No Paused Downloads';
      return total === 1 ? '1 Paused Download' : total + ' Paused Downloads';
    }
    if (status === 'processing') {
      if (total <= 0) return 'No Downloads Currently Processing';
      return total === 1 ? '1 Download Currently Processing' : total + ' Downloads Currently Processing';
    }
    if (status === 'ready') {
      if (total <= 0) return 'No Downloads in Ready State';
      return total === 1 ? '1 Download in Ready State' : total + ' Downloads in Ready State';
    }
    if (status === 'completed') {
      if (total <= 0) return 'No Downloads Completed Yet';
      return total === 1 ? '1 Download Completed' : total + ' Downloads Completed';
    }
    if (status === 'error') {
      if (total <= 0) return 'No Downloads Have Errors';
      return total === 1 ? '1 Download Has Errors' : total + ' Downloads Have Errors';
    }
    return total === 1 ? '1 Download' : total + ' Downloads';
  }

  function paginationSummary(total, from, to) {
    const search = document.getElementById('torrent-search');
    if (search && search.value.trim()) return searchPaginationSummary(total, from, to);
    return filterPaginationSummary(activeFilterStatus(), total);
  }

  function installPaginationRenderer() {
    if (typeof window.renderTorrentPagination !== 'function' || window.renderTorrentPagination.dpDownloadsV11) return;

    window.renderTorrentPagination = function renderTorrentPaginationV11(total, limit, offset) {
      const normalizedTotal = Math.max(0, Number(total) || 0);
      const normalizedLimit = Math.max(1, Number(limit) || 25);
      const normalizedOffset = Math.max(0, Number(offset) || 0);
      const totalPages = Math.max(1, Math.ceil(normalizedTotal / normalizedLimit));
      const cur = Math.min(totalPages, Math.floor(normalizedOffset / normalizedLimit) + 1);

      try { torrentPage = cur; } catch (_) {}

      const info = document.getElementById('torrent-page-info');
      const btns = document.getElementById('torrent-page-btns');
      if (!info || !btns) return;

      const from = normalizedTotal === 0 ? 0 : normalizedOffset + 1;
      const to = Math.min(normalizedOffset + normalizedLimit, normalizedTotal);
      info.textContent = paginationSummary(normalizedTotal, from, to);

      const controls = [];
      if (cur > 1) {
        controls.push(
          '<button type="button" class="dp-pager-btn" aria-label="Previous page"' +
          ' onclick="goToTorrentPage(' + (cur - 1) + ')">' + utilitySvg('chevronLeft') + '</button>'
        );
      }
      controls.push(
        '<button type="button" class="dp-pager-btn dp-pager-current" aria-current="page"' +
        ' aria-label="Page ' + cur + ', current page">' + cur + '</button>'
      );
      if (cur < totalPages) {
        controls.push(
          '<button type="button" class="dp-pager-btn" aria-label="Next page"' +
          ' onclick="goToTorrentPage(' + (cur + 1) + ')">' + utilitySvg('chevronRight') + '</button>'
        );
      }
      btns.innerHTML = controls.join('');
    };

    window.renderTorrentPagination.dpDownloadsV11 = true;
  }

  function installFilterWrapper() {
    if (typeof window.setFilter !== 'function' || window.setFilter.dpDownloadsV11) return;
    const legacySetFilter = window.setFilter;
    window.setFilter = function setFilterV11(el, status) {
      const result = legacySetFilter.apply(this, arguments);
      syncFilterState();
      return result;
    };
    window.setFilter.dpDownloadsV11 = true;
  }

  function observeDynamicCounts() {
    const title = document.getElementById('torrent-card-title');
    if (title && !titleObserver) {
      titleObserver = new MutationObserver(function () {
        if (!title.querySelector('.dp-downloads-heading')) decorateDownloadsHeader();
      });
      titleObserver.observe(title, {childList: true, subtree: true, characterData: true});
    }

    const dashboardTotal = document.getElementById('s-total');
    if (dashboardTotal && !statsObserver) {
      statsObserver = new MutationObserver(function () {
        decorateDownloadsHeader();
      });
      statsObserver.observe(dashboardTotal, {childList: true, subtree: true, characterData: true});
    }
  }

  function observeEmptyStates() {
    const downloadsBody = document.getElementById('t-tbody');
    if (downloadsBody && !downloadsEmptyObserver) {
      downloadsEmptyObserver = new MutationObserver(function () {
        decorateEmptyStates();
        normalizeDownloadRowActions();
      });
      downloadsEmptyObserver.observe(downloadsBody, {childList: true, subtree: true});
    }

    const recentBody = document.getElementById('dash-tbody');
    if (recentBody && !recentEmptyObserver) {
      recentEmptyObserver = new MutationObserver(decorateEmptyStates);
      recentEmptyObserver.observe(recentBody, {childList: true, subtree: true});
    }
  }

  function initializeDownloadsPresentation() {
    installPaginationRenderer();
    installFilterWrapper();
    ensureDownloadFilters();
    decorateBulkSelectionToolbar();
    decorateDownloadsStructure();
    decorateDownloadsHeader();
    decorateEmptyStates();
    normalizeDownloadRowActions();
    syncFilterState();
    observeDynamicCounts();
    observeEmptyStates();
  }

  initializeDownloadsPresentation();
  document.addEventListener('DOMContentLoaded', initializeDownloadsPresentation, {once: true});
})();