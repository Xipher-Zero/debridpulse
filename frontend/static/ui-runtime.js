/* DebridPulse v1.0.11 presentation runtime.
 * Presentation-only DOM decoration/recomposition for the UI overhaul.
 * Backend state and lifecycle behavior remain owned by app.js/backend APIs.
 */
(function () {
  'use strict';

  const DP_ICON_BASE = '/icons/dp/';

  function dpImg(filename, className) {
    const img = document.createElement('img');
    img.src = DP_ICON_BASE + filename;
    img.alt = '';
    img.setAttribute('aria-hidden', 'true');
    img.className = ['dp-icon', className || ''].filter(Boolean).join(' ');
    return img;
  }

  function prependIconOnce(host, filename, className, marker) {
    if (!host || host.querySelector('[data-dp-ui-icon="' + marker + '"]')) return;
    const img = dpImg(filename, className);
    img.dataset.dpUiIcon = marker;
    host.prepend(img);
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
      if (!holder || holder.dataset.dpDecorated === '1') return;
      holder.textContent = '';
      holder.appendChild(dpImg(filename, 'dp-icon--metric'));
      holder.dataset.dpDecorated = '1';
    });
  }

  function decorateQuickAdd() {
    const input = document.getElementById('q-transfer-input');
    const card = input && input.closest('.card');
    if (!card) return;
    card.classList.add('dp-dashboard-quick-add');

    const title = card.querySelector('.card-title');
    if (title && title.dataset.dpDecorated !== '1') {
      title.textContent = 'Quick Add';
      prependIconOnce(title, 'card-link.svg', 'dp-icon--lg', 'quick-add');
      title.dataset.dpDecorated = '1';
    }

    const recover = document.getElementById('btn-recover-all');
    if (recover && recover.dataset.dpDecorated !== '1') {
      const label = recover.textContent.replace(/^\s*[⟳↻]\s*/, '').trim() || 'Recover All';
      recover.textContent = '';
      recover.appendChild(dpImg('retry-borderless.svg', 'dp-icon--sm'));
      const span = document.createElement('span');
      span.textContent = label;
      recover.appendChild(span);
      recover.dataset.defaultLabel = label;
      recover.dataset.dpDecorated = '1';
    }
  }

  function decorateRecentActivity() {
    const card = document.getElementById('dash-activity-card');
    if (!card) return;
    card.classList.add('dp-dashboard-activity');
    const title = card.querySelector('.card-title');
    if (title) prependIconOnce(title, 'card-document-stack.svg', 'dp-icon--lg', 'recent-activity');
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

    if (strip.parentElement !== statsCards.parentElement || strip.previousElementSibling !== statsCards) {
      statsCards.insertAdjacentElement('afterend', strip);
    }
  }

  function decorateDashboard() {
    decorateDashboardHero();
    decorateQuickAdd();
    decorateRecentActivity();
    moveDashboardKpisToStatistics();
  }

  function initialize() {
    decorateDashboard();
  }

  initialize();
  document.addEventListener('DOMContentLoaded', initialize, {once: true});
})();
