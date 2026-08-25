/* DebridPulse v1.0.11 structural presentation runtime.
 * Presentation only: preserves the established backend/API and app.js IDs.
 */
(function () {
  'use strict';

  const DP_ICON_BASE = '/icons/dp/';
  const SUBTITLES = {
    Dashboard: 'Overview of your download activities and system status.',
    Downloads: 'Inspect, filter, and control queued and active transfers.',
    'Event Log': 'Operational history, decisions, warnings, and errors.',
    Statistics: 'Historical transfer performance and completion metrics.',
    Settings: 'Configure providers, downloads, notifications, and system behavior.',
    'Help & License': 'Usage guidance, project information, and licensing.'
  };

  function loadV11Styles() {
    let link = document.querySelector('link[data-dp-v11-styles]');
    if (!link) {
      link = document.createElement('link');
      link.rel = 'stylesheet';
      link.dataset.dpV11Styles = '1';
      document.head.appendChild(link);
    }
    if (!/style-v11\.css\?v=12$/.test(link.href)) link.href = '/style-v11.css?v=12';
  }

  function dpImg(filename, className) {
    const img = document.createElement('img');
    img.src = DP_ICON_BASE + filename;
    img.alt = '';
    img.setAttribute('aria-hidden', 'true');
    img.className = ['dp-icon', className || ''].filter(Boolean).join(' ');
    return img;
  }

  function utilitySvg(kind) {
    const paths = {
      upload: '<path d="M12 16V4"/><path d="m7 9 5-5 5 5"/><path d="M5 20h14"/>',
      recover: '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v6h6"/>',
      arrowRight: '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>'
    };
    return '<svg class="dp-utility-icon" xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (paths[kind] || '') + '</svg>';
  }

  function ensurePageHeading() {
    const title = document.getElementById('page-title');
    const topbar = document.getElementById('topbar');
    if (!title || !topbar) return;
    let wrap = title.closest('.dp-page-heading');
    if (!wrap) {
      wrap = document.createElement('div');
      wrap.className = 'dp-page-heading';
      title.parentNode.insertBefore(wrap, title);
      wrap.appendChild(title);
      const subtitle = document.createElement('p');
      subtitle.id = 'page-subtitle';
      subtitle.className = 'dp-page-subtitle';
      wrap.appendChild(subtitle);
    }
    const subtitle = document.getElementById('page-subtitle');
    const sync = function () {
      if (subtitle) subtitle.textContent = SUBTITLES[title.textContent.trim()] || '';
    };
    sync();
    if (!title.dataset.dpHeadingObserved) {
      title.dataset.dpHeadingObserved = '1';
      new MutationObserver(sync).observe(title, {childList: true, characterData: true, subtree: true});
    }
  }

  function makeSparkline(card, index) {
    if (!card || card.querySelector('.dp-card-spark')) return;
    const variants = [
      '0,18 7,17 14,19 21,14 28,16 35,10 42,19 49,15 56,7 63,18 70,12 77,16 84,9 91,18 100,13',
      '0,18 7,15 14,17 21,12 28,18 35,10 42,15 49,16 56,19 63,12 70,17 77,14 84,5 91,11 100,8',
      '0,20 8,17 16,18 24,14 32,17 40,8 48,18 56,19 64,16 72,17 80,11 88,6 96,13 100,11',
      '0,18 8,11 16,19 24,16 32,19 40,18 48,12 56,18 64,19 72,8 80,18 88,16 96,17 100,14',
      '0,19 7,10 14,18 21,14 28,19 35,18 42,20 49,15 56,17 63,11 70,18 77,12 84,16 91,6 100,19',
      '0,17 7,18 14,14 21,17 28,18 35,11 42,19 49,16 56,10 63,17 70,15 77,8 84,19 91,12 100,16'
    ];
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'dp-card-spark');
    svg.setAttribute('viewBox', '0 0 100 24');
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('aria-hidden', 'true');
    svg.innerHTML = '<polyline class="dp-card-spark-line" points="' + variants[index % variants.length] + '"/>';
    card.appendChild(svg);
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
    Object.entries(heroIcons).forEach(function ([valueId, filename], index) {
      const value = document.getElementById(valueId);
      const card = value && value.closest('.dash-hero-stat');
      const holder = card && card.querySelector('.dhs-icon');
      if (holder && holder.dataset.dpDecorated !== '1') {
        holder.textContent = '';
        holder.appendChild(dpImg(filename, 'dp-icon--metric'));
        holder.dataset.dpDecorated = '1';
      }
      makeSparkline(card, index);
    });
  }

  function normalizeButton(button, iconKind) {
    if (!button || button.dataset.dpStructuralButton === '1') return;
    const label = (button.dataset.defaultLabel || button.textContent || '').replace(/^[^A-Za-z0-9]+/, '').trim();
    button.textContent = '';
    button.insertAdjacentHTML('beforeend', utilitySvg(iconKind));
    const span = document.createElement('span');
    span.textContent = label;
    button.appendChild(span);
    button.dataset.defaultLabel = label;
    button.dataset.dpStructuralButton = '1';
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
    normalizeButton(document.getElementById('btn-import-existing'), 'upload');
    normalizeButton(document.getElementById('btn-recover-all'), 'recover');
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
    if (tbody && !tbody.dataset.dpStructuralObserved) {
      tbody.dataset.dpStructuralObserved = '1';
      new MutationObserver(function () {
        updateRecentCount();
        normalizeDashboardBadges();
      }).observe(tbody, {childList: true, subtree: true});
    }
    updateRecentCount();
    normalizeDashboardBadges();
  }

  function makeKpiIcon(filename, tone) {
    const frame = document.createElement('span');
    frame.className = 'dp-icon-frame dp-icon-frame--' + tone + ' dp-kpi-icon';
    frame.appendChild(dpImg(filename, 'dp-icon--lg'));
    return frame;
  }

  function decorateHistoricalKpis(strip) {
    if (!strip || strip.dataset.dpDecorated === '1') return;
    const iconMap = {
      'i-queue-health': ['heartbeat-outline.svg', 'purple'],
      'i-last-day': ['calendar-24.svg', 'blue'],
      'i-last-week': ['calendar-7.svg', 'purple'],
      'i-success-rate': ['verified-badge.svg', 'green'],
      'i-avg-duration': ['clock-outline.svg', 'amber'],
      'i-avg-size': ['cube.svg', 'blue']
    };
    Object.entries(iconMap).forEach(function ([valueId, config]) {
      const value = document.getElementById(valueId);
      const kpi = value && value.closest('.dash-kpi');
      if (!kpi || kpi.querySelector('.dp-kpi-icon')) return;
      kpi.prepend(makeKpiIcon(config[0], config[1]));
    });
    strip.dataset.dpDecorated = '1';
  }

  function moveDashboardKpisToStatistics() {
    const strip = document.querySelector('.dash-kpi-strip--dashboard');
    const statsCards = document.getElementById('detail-stat-cards');
    if (!strip || !statsCards) return;
    strip.classList.add('dp-stats-history-grid');
    strip.classList.remove('dash-kpi-strip--dashboard');
    decorateHistoricalKpis(strip);
    if (strip.previousElementSibling !== statsCards) statsCards.insertAdjacentElement('afterend', strip);
  }

  function initialize() {
    loadV11Styles();
    document.body.classList.add('dp-v11-structural');
    document.documentElement.dataset.dpUi = 'v1.0.11-structural';
    ensurePageHeading();
    decorateDashboardHero();
    decorateQuickAdd();
    decorateRecentActivity();
    moveDashboardKpisToStatistics();
  }

  initialize();
  document.addEventListener('DOMContentLoaded', initialize, {once: true});
})();
