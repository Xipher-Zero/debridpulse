/* DebridPulse Statistics canonical runtime.
 *
 * The accepted Statistics composition is encoded directly in index.html and
 * ui-statistics-page.css. This runtime owns only Statistics data hydration,
 * dynamic list adaptation, Chart.js rendering, and theme-aware chart paint.
 * It must not construct, reparent, or converge the page shell at runtime.
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
  const PRIMARY_ORDER = Object.freeze(['downloads', 'completed', 'progress', 'success', 'data']);
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
    if (key === 'completed') return 'Downloads completed ' + phrase + '.';
    if (key === 'progress') return 'Downloads still in progress that were added ' + phrase + '.';
    if (key === 'success') return SUCCESS_FLAVOR[period] || 'Share of finished downloads completed successfully during the selected period.';
    if (key === 'data') return 'Total data downloaded ' + phrase + '.';
    return '';
  }

  function titleCaseCode(value) {
    const key = String(value || '').trim();
    if (!key) return 'Unknown';
    return key.replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim().replace(/\b\w/g, function (ch) { return ch.toUpperCase(); });
  }

  function statusLabel(value) {
    const key = String(value || '').trim().toLowerCase();
    const labels = {
      completed: 'Completed', deleted: 'Deleted', error: 'Error', missing: 'Missing',
      duplicate: 'Duplicate', pending: 'Pending', uploading: 'Uploading', processing: 'Processing',
      ready: 'Ready', queued: 'Queued', downloading: 'Downloading', paused: 'Paused',
      partial: 'Partial', blocked: 'Blocked', extracting: 'Extracting', info: 'Info',
      warn: 'Warning', warning: 'Warning',
    };
    return labels[key] || titleCaseCode(key);
  }

  function statisticsSourceLabel(value) {
    const key = String(value || '').trim();
    return SOURCE_LABELS[key] || 'Unknown Source';
  }

  function metricCard(key) {
    return document.querySelector('#detail-stat-cards > [data-dp-stats-metric="' + key + '"]');
  }

  function updatePrimaryMetrics(totals, period) {
    const values = {
      downloads: totals.torrent_total || 0,
      completed: totals.completed_count || 0,
      progress: totals.partial_total || 0,
      success: totals.success_rate_pct != null ? totals.success_rate_pct + '%' : '—',
      data: fmtSize(totals.completed_size || 0),
    };
    PRIMARY_ORDER.forEach(function (key) {
      const card = metricCard(key);
      if (!card) return;
      const value = card.querySelector('.metric-value, .stat-value');
      const sub = card.querySelector('.metric-sub, .stat-sub');
      if (value) value.textContent = values[key];
      if (sub) sub.textContent = primaryDescription(key, period);
    });
  }

  function entryValue(item) {
    if (item && typeof item === 'object') {
      if (item.count != null) return item.count;
      if (item.value != null) return item.value;
    }
    return item;
  }

  function dataEntries(input, kind) {
    if (!input) return [];
    const rawEntries = Array.isArray(input)
      ? input.map(function (item) {
          if (item && typeof item === 'object') {
            const raw = item.status ?? item.level ?? item.source ?? Object.keys(item).find(function (key) { return key !== 'count'; }) ?? 'unknown';
            return [raw, item];
          }
          return [String(item), item];
        })
      : Object.entries(input);
    return rawEntries.map(function (pair) {
      const raw = String(pair[0] ?? '').trim();
      return {
        raw: raw,
        label: kind === 'source' ? statisticsSourceLabel(raw) : statusLabel(raw),
        value: String(entryValue(pair[1]) ?? '—'),
      };
    });
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
    card._dpStatsOverflowEntries = entries.map(function (entry) { return Object.assign({}, entry); });
    card.dataset.dpStatsOverflowTitle = title;
    bindOverflowCard(card);
    if (entries.length <= MAX_VISIBLE) return;
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

  function renderBreakdown(definition, input) {
    const body = document.getElementById(definition.id);
    if (!body) return;
    const card = body.closest('.list-card');
    const entries = dataEntries(input, definition.kind);
    clearOverflow(card);
    if (!entries.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'No data available.';
      body.replaceChildren(empty);
      return;
    }
    body.replaceChildren(buildVisibleList(entries));
    configureOverflow(card, definition.title, entries);
  }

  function statisticsPurpleGradient(chart) {
    const isLight = document.body.classList.contains('light');
    if (!chart || !chart.ctx || !chart.chartArea) {
      return isLight ? 'rgba(139, 91, 203, .46)' : 'rgba(100, 39, 165, .64)';
    }
    const area = chart.chartArea;
    const gradient = chart.ctx.createLinearGradient(0, area.bottom, 0, area.top);
    if (isLight) {
      gradient.addColorStop(0, 'rgba(210, 195, 239, .28)');
      gradient.addColorStop(0.52, 'rgba(171, 137, 221, .42)');
      gradient.addColorStop(1, 'rgba(139, 91, 203, .58)');
    } else {
      gradient.addColorStop(0, 'rgba(45, 19, 84, .46)');
      gradient.addColorStop(0.52, 'rgba(91, 38, 151, .60)');
      gradient.addColorStop(1, 'rgba(166, 70, 244, .72)');
    }
    return gradient;
  }

  function applyChartPalette() {
    const chart = document.getElementById('daily-chart')?._ci;
    const dataset = chart && chart.data && chart.data.datasets && chart.data.datasets[0];
    if (!dataset) return;
    const isLight = document.body.classList.contains('light');
    dataset.backgroundColor = function (context) { return statisticsPurpleGradient(context.chart); };
    dataset.borderColor = isLight ? 'rgba(126, 75, 187, .72)' : 'rgba(166, 70, 244, .84)';
    dataset.borderWidth = 1;
    dataset.fill = true;
    if (typeof chart.update === 'function') chart.update('none');
  }

  function renderCompletionChart(daily) {
    const canvas = document.getElementById('daily-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    if (canvas._ci) canvas._ci.destroy();
    const themeStyles = getComputedStyle(document.body);
    const gridColor = themeStyles.getPropertyValue('--border').trim();
    const tickColor = themeStyles.getPropertyValue('--text3').trim();
    const isLight = document.body.classList.contains('light');
    canvas._ci = new Chart(canvas, {
      type: 'bar',
      data: {
        labels: daily.map(function (item) { return item.date || ''; }),
        datasets: [{
          label: 'Completions',
          data: daily.map(function (item) { return item.count || 0; }),
          backgroundColor: function (context) { return statisticsPurpleGradient(context.chart); },
          borderColor: isLight ? 'rgba(126, 75, 187, .72)' : 'rgba(166, 70, 244, .84)',
          borderWidth: 1,
          borderRadius: 4,
          fill: true,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {legend: {display: false}},
        scales: {
          x: {grid: {color: gridColor}, ticks: {color: tickColor, font: {size: 10}, maxRotation: 45}},
          y: {grid: {color: gridColor}, ticks: {color: tickColor, font: {size: 10}}, beginAtZero: true, precision: 0},
        },
      },
    });
  }

  async function loadDetailedStats(period) {
    const resolved = selectedPeriod(period);
    const chartTitle = document.getElementById('chart-title');
    if (chartTitle) chartTitle.textContent = CHART_FLAVOR[resolved] || 'Completed downloads in the selected period.';
    try {
      const stats = await api('GET', '/stats/detail?period=' + encodeURIComponent(resolved));
      updatePrimaryMetrics(stats.totals || {}, resolved);
      renderBreakdown(BREAKDOWNS[0], stats.torrent_status);
      renderBreakdown(BREAKDOWNS[1], stats.file_status);
      renderBreakdown(BREAKDOWNS[2], stats.event_levels);
      renderBreakdown(BREAKDOWNS[3], stats.sources);
      renderCompletionChart(stats.daily_completions || []);
      applyChartPalette();
      document.dispatchEvent(new CustomEvent('debridpulse:statistics-rendered', {detail: {period: resolved}}));
      return stats;
    } catch (error) {
      toast(sanitizeErrorMsg(error.message), 'error');
      return null;
    }
  }

  function install() {
    window.loadDetailedStats = loadDetailedStats;
    window.fmtDuration = formatCompactDuration;
    return true;
  }

  window.DPStatisticsLifecycle = Object.freeze({load: loadDetailedStats, install});
  install();
  document.addEventListener('debridpulse:theme-changed', applyChartPalette);
})();
