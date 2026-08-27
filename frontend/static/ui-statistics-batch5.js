/* DebridPulse v1.0.11 Statistics Batch 5 presentation runtime.
 * Presentation only: preserves existing statistics API/Chart.js ownership while
 * applying the final reviewed ordering, wording, surface and branding contract.
 */
(function () {
  'use strict';

  const PRIMARY_ORDER = Object.freeze(['downloads', 'completed', 'progress', 'success', 'data']);
  const HISTORY_ORDER = Object.freeze([
    'i-last-day',
    'i-last-week',
    'i-success-rate',
    'i-avg-duration',
    'i-avg-size',
  ]);
  const BREAKDOWN_IDS = Object.freeze([
    'detail-torrent-status',
    'detail-file-status',
    'detail-event-levels',
    'detail-sources',
  ]);
  const CHART_FLAVOR = Object.freeze({
    '1h': 'Completed downloads in the last hour.',
    '24h': 'Completed downloads in the last 24 hours.',
    '7d': 'Completed downloads in the last 7 days.',
    '30d': 'Completed downloads in the last 30 days.',
    '1y': 'Completed downloads in the last year.',
    'all': 'Completed downloads across all recorded history.',
  });

  function selectedPeriod(explicit) {
    if (explicit) return explicit;
    const active = document.querySelector('#stats-period-tabs .ftab.active');
    return (active && active.dataset.period) || '7d';
  }

  function loadBatchStyles() {
    if (document.querySelector('link[data-dp-statistics-batch5]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/ui-statistics-batch5.css?v=2';
    link.dataset.dpStatisticsBatch5 = '1';
    document.head.appendChild(link);
  }

  function normalizeShellBranding() {
    const logo = document.querySelector('#sidebar .logo-icon');
    if (logo && logo.getAttribute('src') !== '/logo.svg?v=7') {
      logo.setAttribute('src', '/logo.svg?v=7');
    }

    const version = document.getElementById('sidebar-version');
    if (version) {
      version.classList.add('dp-app-version');
      version.setAttribute('aria-label', 'DebridPulse version');
      if (version.parentElement !== document.body) {
        document.body.appendChild(version);
      }
    }

    return Boolean(logo && version);
  }

  function normalizePrimaryOrder() {
    const host = document.getElementById('detail-stat-cards');
    if (!host) return false;
    PRIMARY_ORDER.forEach(function (key) {
      const card = host.querySelector(':scope > [data-dp-stats-metric="' + key + '"]');
      if (card) host.appendChild(card);
    });
    return PRIMARY_ORDER.every(function (key) {
      return !!host.querySelector(':scope > [data-dp-stats-metric="' + key + '"]');
    });
  }

  function historicalCard(valueId) {
    const value = document.getElementById(valueId);
    return value && value.closest('.dash-kpi');
  }

  function normalizeHistoricalOrder() {
    const strip = document.querySelector('#view-stats .dp-stats-history-grid');
    if (!strip) return false;

    HISTORY_ORDER.forEach(function (valueId) {
      const card = historicalCard(valueId);
      if (card) strip.appendChild(card);
    });

    const queue = historicalCard('i-queue-health');
    if (queue) strip.appendChild(queue);
    return HISTORY_ORDER.every(function (valueId) { return !!historicalCard(valueId); });
  }

  function decorateChartHeader(period) {
    const canvas = document.getElementById('daily-chart');
    const card = canvas && canvas.closest('.dp-stats-chart');
    const header = card && card.querySelector(':scope > .scard-header');
    const chartTitle = document.getElementById('chart-title');
    if (!header || !chartTitle) return false;

    if (header.dataset.dpStatisticsBatch5 !== '1') {
      header.textContent = '';

      const icon = document.createElement('img');
      icon.src = '/icons/dp/card-download.svg';
      icon.alt = '';
      icon.setAttribute('aria-hidden', 'true');
      icon.className = 'dp-icon dp-stats-chart-title-icon';

      const copy = document.createElement('span');
      copy.className = 'dp-stats-chart-heading-copy';

      const heading = document.createElement('span');
      heading.className = 'dp-stats-chart-heading';
      heading.textContent = 'Completions';

      chartTitle.className = 'dp-stats-chart-subtitle';
      copy.appendChild(heading);
      copy.appendChild(chartTitle);
      header.appendChild(icon);
      header.appendChild(copy);
      header.dataset.dpStatisticsBatch5 = '1';
    }

    chartTitle.textContent = CHART_FLAVOR[selectedPeriod(period)] || 'Completed downloads in the selected period.';
    return true;
  }

  function applySharedSurfaceClass() {
    const canvas = document.getElementById('daily-chart');
    const chartCard = canvas && canvas.closest('.dp-stats-chart');
    if (chartCard) chartCard.classList.add('dp-list-workspace-surface');

    BREAKDOWN_IDS.forEach(function (id) {
      const body = document.getElementById(id);
      const card = body && body.closest('.list-card');
      if (card) card.classList.add('dp-list-workspace-surface');
    });
  }

  function applyBatch5(period) {
    normalizeShellBranding();
    normalizePrimaryOrder();
    normalizeHistoricalOrder();
    decorateChartHeader(period);
    applySharedSurfaceClass();
  }

  function installDetailedStatsGuard() {
    let attempts = 0;
    const attempt = function () {
      attempts += 1;
      const previous = window.loadDetailedStats;
      if (typeof previous !== 'function' || previous.dpStatisticsBatch4 !== '1') {
        if (attempts < 160) setTimeout(attempt, 50);
        return;
      }
      if (previous.dpStatisticsBatch5 === '1') return;

      const wrapped = async function (period) {
        const resolved = selectedPeriod(period);
        const result = await previous.call(this, resolved);
        applyBatch5(resolved);
        return result;
      };
      wrapped.dpStatisticsBatch5 = '1';
      window.loadDetailedStats = wrapped;
    };
    attempt();
  }

  function initialize() {
    loadBatchStyles();
    normalizeShellBranding();
    installDetailedStatsGuard();

    let attempts = 0;
    const settle = function () {
      attempts += 1;
      const brandingReady = normalizeShellBranding();
      const primaryReady = normalizePrimaryOrder();
      const historyReady = normalizeHistoricalOrder();
      const chartReady = decorateChartHeader();
      applySharedSurfaceClass();
      if ((!brandingReady || !primaryReady || !historyReady || !chartReady) && attempts < 160) {
        setTimeout(settle, 50);
      }
    };
    settle();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, {once: true});
  } else {
    initialize();
  }
})();