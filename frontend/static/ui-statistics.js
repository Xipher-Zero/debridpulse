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
    // The server names an upload by its one recorded root request kind; an
    // upload without one (or with several) keeps the channel's name.
    manual_file: 'Uploaded File',
    nzb_file: 'NZB File',
    torrent_file: 'Torrent File',
    metalink_file: 'Metalink File',
    alldebrid_existing: 'AllDebrid Import',
    import_existing: 'AllDebrid Import',
    api: 'API Submission',
  });
  const PRIMARY_ORDER = Object.freeze(['downloads', 'completed', 'progress', 'success', 'data']);
  const BREAKDOWNS = Object.freeze([
    {id: 'detail-torrent-status', title: 'Transfer Disposition', kind: 'status'},
    {id: 'detail-file-status', title: 'File Status', kind: 'status'},
    {id: 'detail-event-levels', title: 'Monitor Levels', kind: 'status'},
    {id: 'detail-sources', title: 'Most Used Sources', kind: 'source'},
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

  /* Completions, on every period: each bar is the bucket's completion total in
   * the chart's own purple, and inside it a narrower stack of the completions
   * the server attributed to each source family (by the provider that
   * delivered them) -- Premium, then Network, on the same scale. Unattributed
   * completions are only the purple above that stack, never a segment of it. */
  const COMPLETION_ROLES = Object.freeze([
    ['premium', 'Premium Sources'],
    ['network', 'Network Sources'],
  ]);
  const COMPLETIONS_LABEL = 'Daily Completions';

  // The inset stack's presentation: purple kept on each side (a share of the
  // bar's width, within bounds) and at least this much above it.
  const COMPLETION_SIDE_SHARE = 0.18, COMPLETION_SIDE_MIN = 2, COMPLETION_SIDE_MAX = 10;
  const COMPLETION_TOP_INSET = 5;
  const COMPLETION_INNER_RADIUS = 3;

  function familyRgb(role) {
    if (role === 'premium') return document.body.classList.contains('light') ? [194, 5, 212] : [245, 56, 255];
    const hex = completionColor('network').replace('#', '');
    const value = parseInt(hex.length === 3 ? hex.replace(/./g, '$&$&') : hex.slice(0, 6), 16);
    return Number.isFinite(value) ? [value >> 16 & 255, value >> 8 & 255, value & 255] : [23, 216, 255];
  }

  /* One bucket's Premium/Network stack inside its purple bar (`bar`, the
   * total's own drawn element): the stack's shape is first cleared from the
   * purple, so each family's translucent gradient lies over the card itself
   * and never mixes with the purple beneath; only the uppermost family is
   * rounded, at a smaller radius than the bar. The inset keeps purple at both
   * sides and, even when every completion is attributed, a few pixels above.
   * Its heights are shares of the bar as drawn in this very frame, so the stack
   * grows with the bar through Chart.js's own animation and matches the scale
   * once it ends. */
  function drawCompletionFamilies(ctx, bar, bucket) {
    const premium = Math.max(0, bucket.premium || 0), network = Math.max(0, bucket.network || 0);
    if (!(bucket.count > 0) || premium + network <= 0) return;
    const {x, y: top, base, width} = bar.getProps(['x', 'y', 'base', 'width']);
    const height = base - top;
    if (!(height > 0)) return;
    const side = Math.max(COMPLETION_SIDE_MIN, Math.min(COMPLETION_SIDE_MAX, Math.round(width * COMPLETION_SIDE_SHARE)));
    const inner = width - 2 * side;
    const attributedTop = base - height * (premium + network) / bucket.count;
    // Never closer to the bar's top than the inset, never more than a quarter of the stack.
    const stackTop = Math.max(attributedTop, top + Math.min(COMPLETION_TOP_INSET, (base - attributedTop) / 4));
    if (inner < 2 || base - stackTop < 1) return;
    const left = x - inner / 2;
    const premiumTop = premium && network ? Math.max(base - height * premium / bucket.count, stackTop) : stackTop;
    const segments = [['premium', base, premium ? premiumTop : base], ['network', premium ? premiumTop : base, stackTop]]
      .filter(([role, bottom, segmentTop]) => (role === 'premium' ? premium : network) > 0 && bottom - segmentTop > 0);
    const radius = Math.min(COMPLETION_INNER_RADIUS, inner / 2, base - stackTop);
    ctx.save();
    ctx.globalCompositeOperation = 'destination-out';
    ctx.beginPath();
    ctx.roundRect(left, stackTop, inner, base - stackTop, [radius, radius, 0, 0]);
    ctx.fill();
    ctx.globalCompositeOperation = 'source-over';
    segments.forEach(([role, bottom, segmentTop], index) => {
      const [r, g, b] = familyRgb(role);
      // Each family its own gradient: 20% at its bottom, strongest (55%) at its top.
      const gradient = ctx.createLinearGradient(0, bottom, 0, segmentTop);
      gradient.addColorStop(0, `rgba(${r}, ${g}, ${b}, .2)`);
      gradient.addColorStop(1, `rgba(${r}, ${g}, ${b}, .55)`);
      ctx.fillStyle = gradient;
      ctx.beginPath();
      const uppermost = index === segments.length - 1;
      ctx.roundRect(left, segmentTop, inner, bottom - segmentTop, uppermost ? [radius, radius, 0, 0] : 0);
      ctx.fill();
    });
    ctx.restore();
  }

  // Draws every bucket's family stack over the purple bars, within the plot.
  const completionFamiliesPlugin = {
    id: 'dpCompletionFamilies',
    afterDatasetsDraw: function (chart) {
      const dataset = chart.data.datasets[0];
      const meta = chart.getDatasetMeta(0);
      if (!dataset || !dataset.dpFamilies || meta.hidden) return;
      const area = chart.chartArea;
      chart.ctx.save();
      chart.ctx.beginPath();
      chart.ctx.rect(area.left, area.top, area.right - area.left, area.bottom - area.top);
      chart.ctx.clip();
      meta.data.forEach(function (bar, index) { drawCompletionFamilies(chart.ctx, bar, dataset.dpFamilies[index]); });
      chart.ctx.restore();
    },
  };

  function completionColor(role) {
    const isLight = document.body.classList.contains('light');
    if (role === 'premium') return isLight ? 'rgba(194, 5, 212, .86)' : 'rgba(245, 56, 255, .9)';
    if (role === 'network') return getComputedStyle(document.body).getPropertyValue('--dp-accent-cyan').trim() || '#17d8ff';
    return isLight ? 'rgba(126, 75, 187, .72)' : 'rgba(166, 70, 244, .84)';
  }

  function renderCompletionLegend() {
    const legend = document.getElementById('completions-legend');
    if (!legend) return;
    legend.replaceChildren(...[['total', COMPLETIONS_LABEL], ...COMPLETION_ROLES].map(function ([role, text]) {
      const item = document.createElement('span');
      item.className = 'dp-stats-chart-legend-item';
      item.setAttribute('role', 'listitem');
      item.dataset.dpRole = role;
      const swatch = document.createElement('span');
      swatch.className = 'dp-stats-chart-legend-swatch';
      swatch.setAttribute('aria-hidden', 'true');
      item.append(swatch, document.createTextNode(text));
      return item;
    }));
  }

  // Theme-dependent paint of one segment role; the total's is the purple gradient.
  function paintCompletionDataset(dataset) {
    dataset.backgroundColor = dataset.dpRole === 'total'
      ? function (context) { return statisticsPurpleGradient(context.chart); }
      : completionColor(dataset.dpRole);
    dataset.borderColor = completionColor(dataset.dpRole);
    dataset.borderWidth = 1;
    dataset.fill = true;
    return dataset;
  }

  // Repaint the chart's segments and the legend's swatches for the current theme.
  function applyChartPalette() {
    document.querySelectorAll('#completions-legend .dp-stats-chart-legend-swatch').forEach(function (swatch) {
      swatch.style.background = completionColor(swatch.parentElement.dataset.dpRole);
    });
    const chart = document.getElementById('daily-chart')?._ci;
    const datasets = chart && chart.data && chart.data.datasets;
    if (!datasets || !datasets.length) return;
    datasets.forEach(paintCompletionDataset);
    if (typeof chart.update === 'function') chart.update('none');
  }

  // A bucket's tooltip: its total and each family's share, and the
  // unattributed remainder only when there is one -- never a claim that the
  // two families make up the total when they do not.
  function completionTooltipLines(bucket) {
    const lines = ['Total completions: ' + (bucket.count || 0)];
    COMPLETION_ROLES.forEach(function ([role, text]) { lines.push(text + ': ' + (bucket[role] || 0)); });
    if (bucket.unattributed > 0) lines.push('Unattributed: ' + bucket.unattributed);
    return lines;
  }

  /* A bucket's label as the server grouped it -- already wall-clock in the
   * operator's time zone: an HH:MM minute, an HH:00 hour, a YYYY-MM-DD day or a
   * YYYY-MM month -- in the viewer's date preference. Anything else as sent. */
  function completionBucketLabel(value) {
    const text = String(value || '');
    let match = /^(\d{4})-(\d{2})(?:-(\d{2}))?$/.exec(text);
    if (match) return window.downloadsCalendarLabel({year: match[1], month: match[2], day: match[3]});
    match = /^(\d{2}):(\d{2})$/.exec(text);
    if (match) return window.downloadsCalendarLabel({hour: match[1], minute: match[2]});
    return text;
  }

  function renderCompletionChart(daily) {
    const canvas = document.getElementById('daily-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    if (canvas._ci) canvas._ci.destroy();
    const themeStyles = getComputedStyle(document.body);
    const gridColor = themeStyles.getPropertyValue('--border').trim();
    const tickColor = themeStyles.getPropertyValue('--text3').trim();
    renderCompletionLegend();
    // One bar per bucket, the total, as it always was, carrying the buckets the
    // family stack is drawn from.
    const datasets = [paintCompletionDataset({
      label: 'Completions',
      dpRole: 'total',
      data: daily.map(function (item) { return item.count || 0; }),
      borderRadius: 4,
      dpFamilies: daily,
    })];
    canvas._ci = new Chart(canvas, {
      type: 'bar',
      data: {labels: daily.map(function (item) { return completionBucketLabel(item.date); }), datasets: datasets},
      plugins: [completionFamiliesPlugin],
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {display: false},
          tooltip: {
            mode: 'index',
            displayColors: false,
            callbacks: {label: function (context) { return completionTooltipLines(daily[context.dataIndex]); }},
          },
        },
        scales: {
          x: {grid: {color: gridColor}, ticks: {color: tickColor, font: {size: 10}, maxRotation: 45}},
          // Completions are counts: whole-number ticks, never a step below one.
          y: {grid: {color: gridColor}, ticks: {color: tickColor, font: {size: 10}, precision: 0}, beginAtZero: true},
        },
      },
    });
    applyChartPalette();
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
