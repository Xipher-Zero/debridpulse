/* DebridPulse v1.0.11 Statistics Batch 3 presentation contract.
 *
 * Presentation only: the existing /stats and /stats/detail APIs remain the
 * source of truth. This layer normalizes reviewed primary-metric wording and
 * breakdown labels after the legacy renderer finishes. Historical KPI ordering,
 * labels, icons and copy are owned exclusively by Batch 5.
 */
(function () {
  'use strict';

  const SOURCE_LABELS = Object.freeze({
    direct_link: 'Debrid Link',
    manual: 'Magnet Link',
    manual_file: 'Torrent File',
    alldebrid_existing: 'AllDebrid Import',
    import_existing: 'AllDebrid Import',
    api: 'API Submission',
  });

  const PRIMARY_METRICS = Object.freeze([
    {key: 'downloads', aliases: ['Downloads', 'Downloads Added'], label: 'Downloads Added'},
    {key: 'data', aliases: ['Completed Size', 'Total Data Downloaded'], label: 'Total Data Downloaded'},
    {key: 'completed', aliases: ['Completed', 'Downloads Completed'], label: 'Downloads Completed'},
    {key: 'progress', aliases: ['In Progress'], label: 'In Progress'},
    {key: 'success', aliases: ['Success Rate'], label: 'Success Rate'},
  ]);

  function selectedPeriod(explicit) {
    if (explicit) return explicit;
    const active = document.querySelector('#stats-period-tabs .ftab.active');
    return (active && active.dataset.period) || '7d';
  }

  function periodPhrase(period) {
    return ({
      '1h': 'during the last hour',
      '24h': 'during the last 24 hours',
      '7d': 'during the last 7 days',
      '30d': 'during the last 30 days',
      '1y': 'during the last year',
      'all': 'across all recorded history',
    })[period] || 'during the selected period';
  }

  function primaryDescription(key, period) {
    const phrase = periodPhrase(period);
    if (key === 'downloads') return 'Downloads added ' + phrase + '.';
    if (key === 'data') return 'Total data downloaded ' + phrase + '.';
    if (key === 'completed') return 'Downloads completed ' + phrase + '.';
    if (key === 'progress') return 'Downloads still in progress that were added ' + phrase + '.';
    if (key === 'success') return 'Share of finished downloads completed successfully for this period.';
    return '';
  }

  function normalizedText(value) {
    return String(value || '').trim().toLowerCase();
  }

  function titleCaseCode(value) {
    const key = String(value || '').trim();
    if (!key) return 'Unknown';
    return key
      .replace(/[_-]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim()
      .replace(/\b\w/g, function (ch) { return ch.toUpperCase(); });
  }

  function statusLabel(value) {
    const key = normalizedText(value);
    const labels = {
      completed: 'Completed',
      deleted: 'Deleted',
      error: 'Error',
      missing: 'Missing',
      duplicate: 'Duplicate',
      pending: 'Pending',
      uploading: 'Uploading',
      processing: 'Processing',
      ready: 'Ready',
      queued: 'Queued',
      downloading: 'Downloading',
      paused: 'Paused',
      partial: 'Partial',
      blocked: 'Blocked',
      extracting: 'Extracting',
      info: 'Info',
      warn: 'Warning',
      warning: 'Warning',
    };
    return labels[key] || titleCaseCode(key);
  }

  function statisticsSourceLabel(value) {
    const key = String(value || '').trim();
    return SOURCE_LABELS[key] || 'Unknown Source';
  }

  function loadBatchStyles() {
    if (document.querySelector('link[data-dp-statistics-batch3]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/ui-statistics-batch3.css?v=1';
    link.dataset.dpStatisticsBatch3 = '1';
    document.head.appendChild(link);
  }

  function installCentralSourceLabels() {
    let attempts = 0;
    const attempt = function () {
      attempts += 1;
      const previous = window.sourceLabel;
      if (typeof previous !== 'function') {
        if (attempts < 120) setTimeout(attempt, 50);
        return;
      }
      if (previous.dpStatisticsBatch3 === '1') return;

      const wrapped = function (source) {
        const key = String(source || '').trim();
        if (Object.prototype.hasOwnProperty.call(SOURCE_LABELS, key)) {
          return SOURCE_LABELS[key];
        }
        return previous.apply(this, arguments);
      };
      wrapped.dpStatisticsBatch3 = '1';
      window.sourceLabel = wrapped;
    };
    attempt();
  }

  function findMetricCard(host, definition) {
    const cards = Array.from(host.querySelectorAll(':scope > .metric-card, :scope > .stat-card'));
    return cards.find(function (card) {
      if (card.dataset.dpStatsMetric === definition.key) return true;
      const label = card.querySelector('.metric-label, .stat-label');
      const text = normalizedText(label && label.textContent);
      return definition.aliases.some(function (alias) {
        return normalizedText(alias) === text;
      });
    }) || null;
  }

  function createMetricCard() {
    const card = document.createElement('div');
    card.className = 'metric-card';
    card.innerHTML = '<div class="metric-label"></div><div class="metric-value">—</div><div class="metric-sub"></div>';
    return card;
  }

  function primaryMetricsHaveRenderedData(host) {
    if (!host) return false;
    const cards = Array.from(host.querySelectorAll(':scope > .metric-card, :scope > .stat-card'));
    if (!cards.length) return false;
    if (cards.length === 1) {
      const label = cards[0].querySelector('.metric-label, .stat-label');
      if (normalizedText(label && label.textContent) === 'library') return false;
    }
    return true;
  }

  function normalizePrimaryMetrics(period) {
    const host = document.getElementById('detail-stat-cards');
    if (!primaryMetricsHaveRenderedData(host)) return false;
    const resolved = selectedPeriod(period);

    PRIMARY_METRICS.forEach(function (definition) {
      let card = findMetricCard(host, definition);
      if (!card) {
        card = createMetricCard();
        host.appendChild(card);
      }
      card.dataset.dpStatsMetric = definition.key;

      const label = card.querySelector('.metric-label, .stat-label');
      const value = card.querySelector('.metric-value, .stat-value');
      const sub = card.querySelector('.metric-sub, .stat-sub');
      if (label) label.textContent = definition.label;
      if (value && !String(value.textContent || '').trim()) value.textContent = '—';
      if (sub) sub.textContent = primaryDescription(definition.key, resolved);
    });
    return PRIMARY_METRICS.every(function (definition) {
      return !!host.querySelector(':scope > [data-dp-stats-metric="' + definition.key + '"]');
    });
  }

  function observePrimaryMetrics() {
    const host = document.getElementById('detail-stat-cards');
    if (!host || host.dataset.dpStatsBatch3Observed === '1') return false;
    host.dataset.dpStatsBatch3Observed = '1';

    new MutationObserver(function () {
      normalizePrimaryMetrics();
    }).observe(host, {childList: true});
    return true;
  }

  function normalizeBreakdownRows(container, kind) {
    if (!container) return;
    container.querySelectorAll('.kv-row').forEach(function (row) {
      const label = row.querySelector('.kv-key') || row.firstElementChild;
      if (!label) return;
      if (!label.dataset.dpStatsRaw) label.dataset.dpStatsRaw = String(label.textContent || '').trim();
      const raw = label.dataset.dpStatsRaw;
      const display = kind === 'source' ? statisticsSourceLabel(raw) : statusLabel(raw);
      if (label.textContent !== display) label.textContent = display;
    });
  }

  function normalizeBreakdowns() {
    normalizeBreakdownRows(document.getElementById('detail-torrent-status'), 'status');
    normalizeBreakdownRows(document.getElementById('detail-file-status'), 'status');
    normalizeBreakdownRows(document.getElementById('detail-event-levels'), 'status');
    normalizeBreakdownRows(document.getElementById('detail-sources'), 'source');
  }

  function applyBatch3(period) {
    normalizePrimaryMetrics(period);
    normalizeBreakdowns();
  }

  function installDetailedStatsGuard() {
    let attempts = 0;
    const attempt = function () {
      attempts += 1;
      const previous = window.loadDetailedStats;
      if (typeof previous !== 'function') {
        if (attempts < 120) setTimeout(attempt, 50);
        return;
      }
      if (previous.dpStatisticsBatch3 === '1') return;

      const wrapped = async function (period) {
        const resolved = selectedPeriod(period);
        const result = await previous.call(this, resolved);
        applyBatch3(resolved);
        return result;
      };
      wrapped.dpStatisticsBatch3 = '1';
      window.loadDetailedStats = wrapped;
    };
    attempt();
  }

  function observeBreakdownBodies() {
    ['detail-torrent-status', 'detail-file-status', 'detail-event-levels', 'detail-sources'].forEach(function (id) {
      const node = document.getElementById(id);
      if (!node || node.dataset.dpStatsBatch3Observed === '1') return;
      node.dataset.dpStatsBatch3Observed = '1';
      new MutationObserver(normalizeBreakdowns).observe(node, {childList: true, subtree: true});
    });
  }

  function initialize() {
    loadBatchStyles();
    installCentralSourceLabels();
    installDetailedStatsGuard();
    observePrimaryMetrics();
    observeBreakdownBodies();

    let attempts = 0;
    const settle = function () {
      attempts += 1;
      normalizeBreakdowns();
      const primaryReady = normalizePrimaryMetrics();
      if (!primaryReady && attempts < 160) setTimeout(settle, 50);
    };
    settle();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, {once: true});
  } else {
    initialize();
  }
})();
