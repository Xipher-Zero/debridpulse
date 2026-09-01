/* DebridPulse v1.0.11 structural presentation runtime.
 * Presentation only: preserves the established backend/API and app.js IDs.
 */
(function () {
  'use strict';

  const DP_ICON_BASE = '/icons/dp/';
  const SUBTITLES = {
    Dashboard: 'Overview of your download activities and system status.',
    Downloads: 'Inspect, filter, and control queued and active transfers.',
    'Event Log': 'Recent transfer activity, decisions, warnings, and errors.',
    'Activity Log': 'Recent transfer activity, decisions, warnings, and errors.',
    Statistics: 'Historical transfer performance and completion metrics.',
    Settings: 'Configure providers, downloads, notifications, and system behavior.',
    'Help & License': 'Usage guidance, project information, and licensing.'
  };



  function dpImg(filename, className) {
    const img = document.createElement('img');
    img.src = DP_ICON_BASE + filename;
    img.alt = '';
    img.setAttribute('aria-hidden', 'true');
    img.className = ['dp-icon', className || ''].filter(Boolean).join(' ');
    return img;
  }

  function utilitySvg(kind) {
    return window.DPIcons && typeof window.DPIcons.svg === 'function'
      ? window.DPIcons.svg(kind)
      : '';
  }

  function ensurePageHeading() {
    const title = document.getElementById('page-title');
    const subtitle = document.getElementById('page-subtitle');
    if (!title || !subtitle) return;
    subtitle.textContent = SUBTITLES[title.textContent.trim()] || '';
  }

  function decorateDashboardHero() {
    const heroIcons = {
      's-total': 'card-download.svg',
      's-completed': 'card-checkmark.svg',
      's-active': 'card-play.svg',
      's-processing': 'card-clock.svg',
      's-error': 'card-error.svg',
      's-size': 'card-disk.svg'
    };
    Object.entries(heroIcons).forEach(function ([valueId, filename]) {
      const value = document.getElementById(valueId);
      const card = value && value.closest('.dash-hero-stat');
      const holder = card && card.querySelector('.dhs-icon');
      if (holder && holder.dataset.dpDecorated !== '1') {
        holder.textContent = '';
        holder.appendChild(dpImg(filename, 'dp-icon--metric'));
        holder.dataset.dpDecorated = '1';
      }
    });
  }

  function normalizeSpeedCapArrow() {
    const arrow = document.querySelector('#aria2-cap-toggle span[aria-hidden="true"]');
    if (!arrow || arrow.querySelector('[data-dp-lucide="chevronDown"]')) return;
    arrow.innerHTML = utilitySvg('chevronDown');
    arrow.classList.add('dp-speedcap-arrow');
  }

  function buttonLabel(button) {
    return (button.dataset.defaultLabel || button.textContent || '')
      .replace(/^[^A-Za-z0-9]+/, '')
      .trim();
  }

  function appendButtonLabel(button, label) {
    const span = document.createElement('span');
    span.textContent = label;
    button.appendChild(span);
    button.dataset.defaultLabel = label;
    button.dataset.dpStructuralButton = '1';
  }

  function normalizeUtilityButton(button, iconKind) {
    if (!button || button.dataset.dpStructuralButton === '1') return;
    const label = buttonLabel(button);
    button.textContent = '';
    button.insertAdjacentHTML('beforeend', utilitySvg(iconKind));
    appendButtonLabel(button, label);
  }

  function normalizeDpButton(button, filename) {
    if (!button || button.dataset.dpStructuralButton === '1') return;
    const label = buttonLabel(button);
    button.textContent = '';
    button.appendChild(dpImg(filename, 'dp-icon--sm'));
    appendButtonLabel(button, label);
  }

  function decorateQuickAdd() {
    const input = document.getElementById('q-transfer-input');
    const card = input && input.closest('.card');
    if (!card) return;
    card.classList.add('dp-dashboard-quick-add');
    const header = card.querySelector('.card-header');
    const title = card.querySelector('.card-title');
    if (title && title.dataset.dpStructuralTitle !== '1') {
      title.textContent = '';
      title.appendChild(dpImg('card-link.svg', 'dp-icon--lg'));
      const copy = document.createElement('span');
      copy.className = 'dp-card-heading-copy';
      copy.innerHTML = '<span>Quick Add</span><span class="dp-card-subtitle">Add links, magnets, or torrent files to the queue.</span>';
      title.appendChild(copy);
      title.dataset.dpStructuralTitle = '1';
    }
    if (header) {
      const legacyCopy = Array.from(header.querySelectorAll('span')).find(function (el) {
        return el !== title && /One item per line/.test(el.textContent || '');
      });
      if (legacyCopy) legacyCopy.style.display = 'none';
      const importButton = document.getElementById('btn-import-existing');
      const actionWrap = importButton && importButton.parentElement;
      if (actionWrap) actionWrap.classList.add('dp-card-header-actions');
    }
    normalizeUtilityButton(document.getElementById('btn-import-existing'), 'upload');
    normalizeUtilityButton(document.getElementById('btn-recover-all'), 'refresh');
  }

  function updateRecentCount() {
    const count = document.getElementById('dash-activity-count');
    const tbody = document.getElementById('dash-tbody');
    if (!count || !tbody) return;
    const rows = Array.from(tbody.querySelectorAll('tr')).filter(function (tr) {
      return !tr.querySelector('td.empty') && tr.querySelectorAll('td').length > 1;
    }).length;
    count.textContent = rows ? rows + ' most recent download' + (rows === 1 ? '' : 's') : 'Recent transfer history';
  }

  function normalizeDashboardBadges() {
    document.querySelectorAll('#dash-tbody .badge').forEach(function (badge) {
      if (badge.querySelector('.dp-status-icon')) {
        badge.dataset.dpPresentationNormalized = '1';
        return;
      }
      if (badge.dataset.dpPresentationNormalized === '1') return;
      const text = (badge.textContent || '').replace(/^[^A-Za-z0-9]+/, '').trim();
      const desired = badge.classList.contains('badge-completed') ? 'Done' : text;
      if (desired && badge.textContent !== desired) badge.textContent = desired;
      badge.dataset.dpPresentationNormalized = '1';
    });
  }

  function decorateRecentActivity() {
    const card = document.getElementById('dash-activity-card');
    if (!card) return;
    card.classList.add('dp-dashboard-activity');
    const title = card.querySelector('.card-title');
    if (title && title.dataset.dpStructuralTitle !== '1') {
      title.textContent = '';
      title.appendChild(dpImg('card-document-stack.svg', 'dp-icon--lg'));
      const copy = document.createElement('span');
      copy.className = 'dp-card-heading-copy';
      copy.innerHTML = '<span>Recent Activity</span>';
      const count = document.getElementById('dash-activity-count');
      if (count) copy.appendChild(count);
      title.appendChild(copy);
      title.dataset.dpStructuralTitle = '1';
    }
    const viewAll = card.querySelector('.card-header .btn');
    if (viewAll && viewAll.dataset.dpStructuralButton !== '1') {
      viewAll.textContent = 'View All';
      viewAll.insertAdjacentHTML('beforeend', utilitySvg('arrowRight'));
      viewAll.dataset.dpStructuralButton = '1';
    }
    const tbody = document.getElementById('dash-tbody');
    updateRecentCount();
    normalizeDashboardBadges();
  }

  function normalizeActivityRows() {
    const list = document.getElementById('event-list');
    if (!list) return;
    Array.from(list.children).forEach(function (row) {
      if (!row.classList || (!row.classList.contains('event-item') && !row.classList.contains('dp-activity-row'))) return;
      row.classList.add('dp-activity-row');
      row.classList.remove('event-item');

      const level = row.querySelector('.elevel');
      if (level) level.classList.add('dp-activity-level');
      const message = row.querySelector('.emsg');
      if (message) message.classList.add('dp-activity-message');
      const transfer = row.querySelector('.ename');
      if (transfer) transfer.classList.add('dp-activity-transfer');
      const time = row.querySelector('.etime');
      if (time) time.classList.add('dp-activity-time');

      if (level && level.nextElementSibling && level.nextElementSibling !== time) {
        level.nextElementSibling.classList.add('dp-activity-copy');
      }
    });
  }

  function decorateActivityLog() {
    const card = document.querySelector('#view-events > .card');
    if (!card) return;
    card.classList.add('dp-activity-card');

    const title = card.querySelector('.card-title');
    if (title && title.dataset.dpStructuralTitle !== '1') {
      title.textContent = '';
      title.classList.add('dp-activity-card-title');
      title.appendChild(dpImg('document.svg', 'dp-activity-title-icon'));
      const copy = document.createElement('span');
      copy.className = 'dp-activity-heading-copy';
      copy.innerHTML = '<span class="dp-activity-heading">Activity Log</span><span class="dp-activity-subtitle">Everything DebridPulse thought was worth mentioning.</span>';
      title.appendChild(copy);
      title.dataset.dpStructuralTitle = '1';
    }

    const refresh = card.querySelector('.card-header button[onclick*="loadEvents"]');
    if (refresh) {
      refresh.classList.add('dp-activity-refresh');
      refresh.setAttribute('aria-label', 'Refresh activity log');
      refresh.title = 'Refresh activity log';
      normalizeUtilityButton(refresh, 'refresh');
    }

    const search = document.getElementById('ev-search');
    const searchRow = search && search.closest('.ev-search-row');
    const searchBand = searchRow && searchRow.parentElement;
    if (searchRow) searchRow.classList.add('dp-activity-search-row');
    if (searchBand && searchBand.parentElement === card) {
      searchBand.classList.add('dp-activity-search-band');
      searchBand.removeAttribute('style');
    }

    const levelFilter = document.getElementById('ev-level');
    if (levelFilter) levelFilter.removeAttribute('style');

    const list = document.getElementById('event-list');
    if (list) {
      list.classList.add('dp-activity-list');
    }
    normalizeActivityRows();
  }

  function initialize() {
    document.body.classList.add('dp-v11-structural');
    document.documentElement.dataset.dpUi = 'v1.0.11-structural';
    ensurePageHeading();
    decorateDashboardHero();
    normalizeSpeedCapArrow();
    decorateQuickAdd();
    decorateRecentActivity();
    decorateActivityLog();
  }

  initialize();
  document.addEventListener('DOMContentLoaded', initialize, {once: true});
  document.addEventListener('debridpulse:navigation', ensurePageHeading);
  document.addEventListener('debridpulse:dashboard-recent-rendered', function () { updateRecentCount(); normalizeDashboardBadges(); });
  document.addEventListener('debridpulse:activity-rendered', normalizeActivityRows);
})();
