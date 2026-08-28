/* DebridPulse v1.0.11 Statistics Batch 5 presentation runtime.
 * Presentation only: preserves existing statistics API/Chart.js ownership while
 * applying the final reviewed ordering, wording and surface contract. Batch 5
 * is the sole owner of historical KPI labels, icons and DOM order.
 */
(function () {
  'use strict';

  const PRIMARY_ORDER = Object.freeze(['downloads', 'completed', 'progress', 'success', 'data']);
  const HISTORY_ORDER = Object.freeze([
    'i-last-day',
    'i-last-week',
    'i-avg-duration',
    'i-success-rate',
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
  const SUCCESS_FLAVOR = Object.freeze({
    '1h': 'Share of finished downloads completed successfully during the last hour.',
    '24h': 'Share of finished downloads completed successfully during the last 24 hours.',
    '7d': 'Share of finished downloads completed successfully during the last 7 days.',
    '30d': 'Share of finished downloads completed successfully during the last 30 days.',
    '1y': 'Share of finished downloads completed successfully during the last year.',
    'all': 'Share of finished downloads completed successfully across all recorded history.',
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
    link.href = '/ui-statistics-batch5.css?v=3';
    link.dataset.dpStatisticsBatch5 = '1';
    document.head.appendChild(link);
  }

  function primaryCards(host) {
    if (!host) return [];
    return PRIMARY_ORDER.map(function (key) {
      return host.querySelector(':scope > [data-dp-stats-metric="' + key + '"]');
    });
  }

  function normalizePrimaryOrder() {
    const host = document.getElementById('detail-stat-cards');
    if (!host) return false;

    const ordered = primaryCards(host);
    if (ordered.some(function (card) { return !card; })) return false;

    const current = Array.from(host.children).filter(function (card) {
      return card.hasAttribute('data-dp-stats-metric');
    });
    const alreadyOrdered = ordered.every(function (card, index) {
      return current[index] === card;
    });

    if (!alreadyOrdered) ordered.forEach(function (card) { host.appendChild(card); });
    return true;
  }

  function historicalCard(valueId) {
    const value = document.getElementById(valueId);
    return value && value.closest('.dash-kpi');
  }

  function historicalCopy(valueId, labelText, subText, className) {
    const card = historicalCard(valueId);
    if (!card) return null;
    const label = card.querySelector('.dash-kpi-lbl');
    const sub = card.querySelector('.dash-kpi-sub');
    if (label) label.textContent = labelText;
    if (sub) sub.textContent = subText;
    if (className) card.classList.add(className);
    return card;
  }

  function suppressQueueHealth() {
    const queue = historicalCard('i-queue-health');
    if (!queue) return true;

    queue.classList.add('dp-stats-history-compat');
    queue.hidden = true;
    queue.setAttribute('aria-hidden', 'true');
    queue.style.setProperty('display', 'none', 'important');
    return true;
  }

  function normalizeHistoricalOrder() {
    const strip = document.querySelector('#view-stats .dp-stats-history-grid');
    if (!strip) return false;

    const ordered = HISTORY_ORDER.map(historicalCard);
    if (ordered.some(function (card) { return !card; })) return false;

    const current = Array.from(strip.children).filter(function (node) {
      return node.classList && node.classList.contains('dash-kpi') && !node.hidden;
    });
    const alreadyOrdered = ordered.every(function (card, index) {
      return current[index] === card;
    });

    if (!alreadyOrdered) ordered.forEach(function (card) { strip.appendChild(card); });
    suppressQueueHealth();
    return true;
  }

  function normalizeSuccessRateCopy(period) {
    const resolved = selectedPeriod(period);
    const host = document.getElementById('detail-stat-cards');
    const primary = host && host.querySelector(':scope > [data-dp-stats-metric="success"]');
    const primarySub = primary && primary.querySelector('.metric-sub, .stat-sub');
    if (primarySub) {
      primarySub.textContent = SUCCESS_FLAVOR[resolved] ||
        'Share of finished downloads completed successfully during the selected period.';
    }

    const day = historicalCopy(
      'i-last-day',
      'Last 24 Hours',
      'Completed downloads over the last 24 hours.',
      'dp-stats-kpi-day'
    );
    const week = historicalCopy(
      'i-last-week',
      'Last 7 Days',
      'Completed downloads over the last 7 days.',
      'dp-stats-kpi-week'
    );
    const duration = historicalCopy(
      'i-avg-duration',
      'MEAN DOWNLOAD TIME',
      'Average completion time for downloads to finish.',
      'dp-stats-kpi-duration'
    );
    const historical = historicalCopy(
      'i-success-rate',
      'LIFE-TIME SUCCESS RATE',
      'Share of all recorded finished downloads completed successfully.',
      'dp-stats-kpi-success'
    );
    const size = historicalCopy(
      'i-avg-size',
      'MEAN DOWNLOAD SIZE',
      'Average size of completed downloads.',
      'dp-stats-kpi-size'
    );

    const icon = historical && historical.querySelector('.dp-kpi-icon .dp-icon');
    if (icon && icon.getAttribute('src') !== '/icons/dp/heartbeat-outline.svg') {
      icon.src = '/icons/dp/heartbeat-outline.svg';
    }

    return Boolean(primarySub && day && week && duration && historical && size);
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
    normalizePrimaryOrder();
    normalizeHistoricalOrder();
    normalizeSuccessRateCopy(period);
    decorateChartHeader(period);
    applySharedSurfaceClass();
  }

  function initialize() {
    loadBatchStyles();
    applyBatch5();
    document.addEventListener('debridpulse:statistics-rendered', function (event) {
      applyBatch5(event.detail && event.detail.period);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, {once: true});
  } else {
    initialize();
  }
})();
