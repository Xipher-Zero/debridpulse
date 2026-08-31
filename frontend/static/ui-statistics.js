/* DebridPulse Statistics presentation owner.
 *
 * Presentation only. app.js and the backend retain statistics data, API, and
 * Chart.js ownership. This runtime owns the accepted Statistics DOM contract.
 */
(function () {
  'use strict';

  const EVENT_NAME = 'debridpulse:statistics-rendered';
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
  const PRIMARY_ORDER = Object.freeze(['downloads', 'completed', 'progress', 'success', 'data']);
  const HISTORY_ORDER = Object.freeze([
    'i-last-day',
    'i-last-week',
    'i-avg-duration',
    'i-success-rate',
    'i-avg-size',
  ]);
  const BREAKDOWNS = Object.freeze([
    {id: 'detail-torrent-status', title: 'Download Status', kind: 'status'},
    {id: 'detail-file-status', title: 'File Status', kind: 'status'},
    {id: 'detail-event-levels', title: 'Monitor Levels', kind: 'status'},
    {id: 'detail-sources', title: 'Top Sources', kind: 'source'},
  ]);
  const MAX_VISIBLE = 10;
  const TWO_COLUMN_THRESHOLD = 6;
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

  function formatCompactDuration(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value <= 0) return '—';

    const totalMinutes = Math.max(1, Math.round(value / 60));
    const days = Math.floor(totalMinutes / 1440);
    const hours = Math.floor((totalMinutes % 1440) / 60);
    const minutes = totalMinutes % 60;
    const parts = [];

    if (days) parts.push(days + 'D');
    if (hours) parts.push(hours + 'H');
    if (minutes) parts.push(minutes + 'M');
    return parts.join(' ');
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
    BREAKDOWNS.forEach(function (definition) {
      normalizeBreakdownRows(document.getElementById(definition.id), definition.kind);
    });
  }

  function rowEntry(row) {
    if (!row) return null;
    const label = row.querySelector('.kv-key') || row.firstElementChild;
    const value = row.querySelector('.kv-val') || row.lastElementChild;
    if (!label || !value || label === value) return null;
    return {
      raw: String(label.dataset.dpStatsRaw || label.textContent || '').trim(),
      label: String(label.textContent || '').trim(),
      value: String(value.textContent || '').trim(),
    };
  }

  function captureEntries(body) {
    return Array.from(body.querySelectorAll('.kv-row'))
      .map(rowEntry)
      .filter(Boolean);
  }

  function makeRow(entry) {
    const row = document.createElement('div');
    row.className = 'kv-row';

    const label = document.createElement('span');
    label.className = 'kv-key';
    label.textContent = entry.label;
    label.dataset.dpStatsRaw = entry.raw;

    const value = document.createElement('span');
    value.className = 'kv-val';
    value.textContent = entry.value;

    row.append(label, value);
    return row;
  }

  function buildVisibleList(entries) {
    const visible = entries.slice(0, MAX_VISIBLE);
    const list = document.createElement('div');
    list.className = 'dp-stats-adaptive-list';
    list.dataset.dpStatsAdaptive = '1';

    if (visible.length < TWO_COLUMN_THRESHOLD) {
      list.classList.add('dp-stats-adaptive-list--single');
      visible.forEach(function (entry) { list.appendChild(makeRow(entry)); });
      return list;
    }

    list.classList.add('dp-stats-adaptive-list--columns');
    const left = document.createElement('div');
    const right = document.createElement('div');
    left.className = 'dp-stats-adaptive-column';
    right.className = 'dp-stats-adaptive-column';

    const split = Math.ceil(visible.length / 2);
    visible.slice(0, split).forEach(function (entry) { left.appendChild(makeRow(entry)); });
    visible.slice(split).forEach(function (entry) { right.appendChild(makeRow(entry)); });
    list.append(left, right);
    return list;
  }

  function openFullList(card) {
    const entries = card && card._dpStatsOverflowEntries;
    if (!Array.isArray(entries) || entries.length <= MAX_VISIBLE) return;

    const overlay = document.getElementById('overlay');
    const title = document.getElementById('modal-title');
    const body = document.getElementById('modal-body');
    if (!overlay || !title || !body) return;

    title.textContent = (card.dataset.dpStatsOverflowTitle || 'Statistics') + ' — Full List';
    const list = document.createElement('div');
    list.className = 'dp-stats-overflow-list';
    entries.forEach(function (entry) { list.appendChild(makeRow(entry)); });
    body.replaceChildren(list);
    overlay.classList.add('open');
  }

  function bindOverflowCard(card) {
    if (!card || card.dataset.dpStatsOverflowBound === '1') return;
    card.dataset.dpStatsOverflowBound = '1';

    card.addEventListener('click', function (event) {
      if (!card.classList.contains('dp-stats-overflow-card')) return;
      if (event.target instanceof Element && event.target.closest('button, a, input, select, textarea')) return;
      openFullList(card);
    });

    card.addEventListener('keydown', function (event) {
      if (!card.classList.contains('dp-stats-overflow-card')) return;
      if (event.target !== card || (event.key !== 'Enter' && event.key !== ' ')) return;
      event.preventDefault();
      openFullList(card);
    });
  }

  function clearOverflow(card) {
    if (!card) return;
    card.classList.remove('dp-stats-overflow-card');
    card.removeAttribute('role');
    card.removeAttribute('tabindex');
    card.removeAttribute('aria-label');
    card.querySelectorAll('.dp-stats-more').forEach(function (node) { node.remove(); });
    card._dpStatsOverflowEntries = [];
    delete card.dataset.dpStatsOverflowTitle;
  }

  function configureOverflow(card, title, entries) {
    clearOverflow(card);
    const overflow = entries.length > MAX_VISIBLE;
    card._dpStatsOverflowEntries = entries.map(function (entry) { return Object.assign({}, entry); });
    card.dataset.dpStatsOverflowTitle = title;
    bindOverflowCard(card);

    if (!overflow) return;

    const more = entries.length - MAX_VISIBLE;
    card.classList.add('dp-stats-overflow-card');
    card.setAttribute('role', 'button');
    card.tabIndex = 0;
    card.setAttribute('aria-label', title + '. Showing top 10. View ' + more + ' more.');

    const indicator = document.createElement('span');
    indicator.className = 'dp-stats-more';
    indicator.textContent = '+ ' + more + ' more';
    indicator.setAttribute('aria-hidden', 'true');
    const header = card.querySelector(':scope > .card-header');
    (header || card).appendChild(indicator);
  }

  function applyAdaptiveBreakdown(definition) {
    const body = document.getElementById(definition.id);
    if (!body) return false;

    const card = body.closest('.list-card');
    if (!card) return false;
    if (body.querySelector(':scope > .dp-stats-adaptive-list')) return true;

    const entries = captureEntries(body);
    if (!entries.length) {
      clearOverflow(card);
      return false;
    }

    body.replaceChildren(buildVisibleList(entries));
    configureOverflow(card, definition.title, entries);
    return true;
  }

  function applyAdaptiveBreakdowns() {
    BREAKDOWNS.forEach(applyAdaptiveBreakdown);
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

    if (header.dataset.dpStatisticsHeader !== '1') {
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
      header.dataset.dpStatisticsHeader = '1';
    }

    chartTitle.textContent = CHART_FLAVOR[selectedPeriod(period)] || 'Completed downloads in the selected period.';
    return true;
  }

  function applySharedSurfaceClass() {
    const canvas = document.getElementById('daily-chart');
    const chartCard = canvas && canvas.closest('.dp-stats-chart');
    if (chartCard) chartCard.classList.add('dp-list-workspace-surface');

    BREAKDOWNS.forEach(function (definition) {
      const body = document.getElementById(definition.id);
      const card = body && body.closest('.list-card');
      if (card) card.classList.add('dp-list-workspace-surface');
    });
  }

  function applyPresentation(period) {
    normalizePrimaryMetrics(period);
    normalizeBreakdowns();
    applyAdaptiveBreakdowns();
    normalizePrimaryOrder();
    normalizeHistoricalOrder();
    normalizeSuccessRateCopy(period);
    decorateChartHeader(period);
    applySharedSurfaceClass();
  }

  function install() {
    const previous = window.loadDetailedStats;
    if (typeof previous !== 'function') {
      console.error('[DebridPulse] Statistics presentation not installed: loadDetailedStats is unavailable.');
      return false;
    }
    if (previous.dpStatisticsPresentation === '1') return true;

    const wrapped = async function (period) {
      const resolved = selectedPeriod(period);
      const result = await previous.call(this, resolved);
      document.dispatchEvent(new CustomEvent(EVENT_NAME, {
        detail: {period: resolved}
      }));
      return result;
    };
    wrapped.dpStatisticsPresentation = '1';
    window.loadDetailedStats = wrapped;
    window.fmtDuration = formatCompactDuration;
    return true;
  }

  function initialize() {
    applyPresentation();
    document.addEventListener(EVENT_NAME, function (event) {
      applyPresentation(event.detail && event.detail.period);
    });
  }

  window.DPStatisticsLifecycle = Object.freeze({
    event: EVENT_NAME,
    install: install,
  });

  install();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, {once: true});
  } else {
    initialize();
  }
})();
