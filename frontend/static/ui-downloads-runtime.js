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
      refresh: '<path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M3 21v-5h5"/><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>'
    };
    const geometry = paths[name];
    if (!geometry) return '';
    return '<svg class="lucide dp-utility-icon" xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + geometry + '</svg>';
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
      refresh.innerHTML = utilitySvg('refresh');
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

      const pages = [];
      if (totalPages <= 7) {
        for (let i = 1; i <= totalPages; i += 1) pages.push(i);
      } else {
        pages.push(1);
        const start = Math.max(2, cur - 2);
        const end = Math.min(totalPages - 1, cur + 2);
        if (start > 2) pages.push('...');
        for (let i = start; i <= end; i += 1) pages.push(i);
        if (end < totalPages - 1) pages.push('...');
        pages.push(totalPages);
      }

      const previous =
        '<button type="button" class="dp-pager-btn" aria-label="Previous page"' +
        (cur <= 1 ? ' disabled' : '') +
        ' onclick="goToTorrentPage(' + (cur - 1) + ')">' + utilitySvg('chevronLeft') + '</button>';

      const numbered = pages.map(function (page) {
        if (page === '...') return '<span class="dp-pager-ellipsis" aria-hidden="true">…</span>';
        const current = page === cur;
        return '<button type="button" class="dp-pager-btn' + (current ? ' dp-pager-current' : '') + '"' +
          (current ? ' aria-current="page"' : '') +
          ' aria-label="Page ' + page + '" onclick="goToTorrentPage(' + page + ')">' + page + '</button>';
      }).join('');

      const next =
        '<button type="button" class="dp-pager-btn" aria-label="Next page"' +
        (cur >= totalPages ? ' disabled' : '') +
        ' onclick="goToTorrentPage(' + (cur + 1) + ')">' + utilitySvg('chevronRight') + '</button>';

      btns.innerHTML = previous + numbered + next;
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