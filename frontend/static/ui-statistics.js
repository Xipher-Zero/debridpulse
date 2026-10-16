/* DebridPulse Statistics canonical runtime.
 *
 * The accepted Statistics composition is encoded directly in index.html and
 * ui-statistics-page.css. This runtime owns only Statistics data hydration,
 * dynamic list adaptation, Chart.js rendering, and theme-aware chart paint.
 * It must not construct, reparent, or converge the page shell at runtime.
 *
 * Live while shown: every figure, list and the chart come from ONE accepted
 * GET /stats/detail answer for the selected period. The application's one
 * stream says when to re-read (debridpulse:statistics-invalidated) and keeps
 * time (debridpulse:pulse-heartbeat, at least every 30 s), never what to show.
 * At most one request is in flight; re-reads are spaced by REFRESH_SPACING_MS
 * (measured), a re-read asked for meanwhile runs once that one settles, and a
 * period choice or re-entry runs as soon as nothing is in flight. An answer
 * for a period, view or generation no longer current is never shown. A live
 * re-read updates the existing chart in place, so a bar moves from its drawn
 * height; a period choice or re-entry draws it anew.
 */
(function () {
  'use strict';

  // Events Severity reads the journal's stored severities, always in this
  // order whatever the counts or the query's order.
  const SEVERITIES = Object.freeze([['error', 'Errors'], ['warning', 'Warnings'], ['info', 'Informational']]);
  const PRIMARY_ORDER = Object.freeze(['downloads', 'completed', 'progress', 'success', 'data']);
  const BREAKDOWNS = Object.freeze([
    {id: 'detail-torrent-status', title: 'Transfer Disposition', kind: 'status'},
    {id: 'detail-file-status', title: 'File Status', kind: 'status'},
    {id: 'detail-event-levels', title: 'Events Severity', kind: 'severity'},
    // The server names each acquisition method (its integrations declare it).
    {id: 'detail-sources', title: 'Transfer Methods', kind: 'method'},
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
      partial: 'Partial', blocked: 'Blocked', extracting: 'Extracting',
    };
    return labels[key] || titleCaseCode(key);
  }

  function severityEntries(input) {
    const counts = new Map((Array.isArray(input) ? input : []).map(function (item) {
      return [String(item.level || '').trim().toLowerCase(), item.count];
    }));
    return SEVERITIES.filter(function (pair) { return counts.has(pair[0]); }).map(function (pair) {
      return {raw: pair[0], label: pair[1], value: String(counts.get(pair[0]) ?? '—')};
    });
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
    if (kind === 'severity') return severityEntries(input);
    const rawEntries = Array.isArray(input)
      ? input.map(function (item) {
          if (item && typeof item === 'object') {
            const raw = item.status ?? item.method ?? Object.keys(item).find(function (key) { return key !== 'count'; }) ?? 'unknown';
            return [raw, item];
          }
          return [String(item), item];
        })
      : Object.entries(input);
    return rawEntries.map(function (pair) {
      const raw = String(pair[0] ?? '').trim();
      return {
        raw: raw,
        label: kind === 'method' ? raw : statusLabel(raw),
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
    // A card that still overflows keeps its role and tab stop (so keyboard
    // focus stays on it across a live re-read); only its counts change.
    if (entries.length <= MAX_VISIBLE || !card.classList.contains('dp-stats-overflow-card')) clearOverflow(card);
    card._dpStatsOverflowEntries = entries.map(function (entry) { return Object.assign({}, entry); });
    card.dataset.dpStatsOverflowTitle = title;
    bindOverflowCard(card);
    if (entries.length <= MAX_VISIBLE) return;
    const more = entries.length - MAX_VISIBLE;
    card.classList.add('dp-stats-overflow-card');
    card.setAttribute('role', 'button');
    card.tabIndex = 0;
    card.setAttribute('aria-label', title + '. Showing top 10. View ' + more + ' more.');
    const header = card.querySelector(':scope > .card-header');
    let indicator = card.querySelector('.dp-stats-more');
    if (!indicator) {
      indicator = document.createElement('span');
      indicator.className = 'dp-stats-more';
      indicator.setAttribute('aria-hidden', 'true');
      (header || card).appendChild(indicator);
    }
    indicator.textContent = '+ ' + more + ' more';
  }

  function renderBreakdown(definition, input) {
    const body = document.getElementById(definition.id);
    if (!body) return;
    const card = body.closest('.list-card');
    const entries = dataEntries(input, definition.kind);
    if (!entries.length) {
      clearOverflow(card);
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

  const LIVE_TRANSITION_MS = 350;
  const reducedMotion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : {matches: false};

  /* A live re-read of the same period, applied to the chart already drawn:
   * buckets are matched by their server key, so a bucket that stays keeps its
   * bar and moves from its drawn height to the new one, a new bucket grows
   * from the baseline in its place and one that left is removed (Chart.js
   * follows splices of the data array). Only when the surviving buckets'
   * order changed is the data replaced outright, without a transition. */
  function updateCompletionChart(chart, daily) {
    const dataset = chart.data.datasets[0];
    const keys = daily.map(function (item) { return String(item.date); });
    const counts = daily.map(function (item) { return item.count || 0; });
    const held = (dataset.dpFamilies || []).map(function (item) { return String(item.date); });
    const wanted = new Set(keys);
    const surviving = held.filter(function (key) { return wanted.has(key); });
    const inOrder = surviving.join('\u0000') === keys.filter(function (key) { return held.includes(key); }).join('\u0000');
    const animate = inOrder && !reducedMotion.matches;
    if (inOrder) {
      for (let index = held.length - 1; index >= 0; index--) {
        if (!wanted.has(held[index])) { held.splice(index, 1); dataset.data.splice(index, 1); chart.data.labels.splice(index, 1); }
      }
      keys.forEach(function (key, index) {
        if (held[index] === key) { dataset.data[index] = counts[index]; chart.data.labels[index] = completionBucketLabel(key); return; }
        held.splice(index, 0, key);
        dataset.data.splice(index, 0, counts[index]);
        chart.data.labels.splice(index, 0, completionBucketLabel(key));
      });
    } else {
      chart.data.labels = keys.map(completionBucketLabel);
      dataset.data = counts;
    }
    dataset.dpFamilies = daily;
    if (animate) {
      chart.options.animation = {duration: LIVE_TRANSITION_MS, easing: 'easeOutCubic'};
      chart.update();
    } else {
      chart.update('none');
    }
  }

  function renderCompletionChart(daily, live) {
    const canvas = document.getElementById('daily-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    if (live && canvas._ci) { updateCompletionChart(canvas._ci, daily); return; }
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
            // The bucket the chart holds NOW: a live re-read replaces it in place.
            callbacks: {label: function (context) { return completionTooltipLines(datasets[0].dpFamilies[context.dataIndex] || {}); }},
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

  // Re-read admission. /stats/detail measured 5-12 ms on a 700-transfer,
  // 20k-event history and at most ~320 ms (1 Year) on 20k transfers and a
  // million events: re-reads start at least 2 s apart, or five times the last
  // answer's own time when that is longer (a slow host spaces itself), never
  // more than 10 s apart.
  const REFRESH_SPACING_MS = 2000, REFRESH_SPACING_MAX_MS = 10000, LATENCY_SPACING_FACTOR = 5;
  // A heartbeat re-reads only when the shown answer is at least this old.
  const AGING_MS = 25000;
  let currentPeriod = null;
  let generation = 0;                 // the period choice / entry an answer must belong to
  let inflight = false, queued = null, timer = null;
  let lastStart = 0, lastAccepted = 0, spacing = REFRESH_SPACING_MS;

  const shown = function () { return Boolean(document.getElementById('view-stats')?.classList.contains('active')); };

  function render(stats, period, live) {
    updatePrimaryMetrics(stats.totals || {}, period);
    renderBreakdown(BREAKDOWNS[0], stats.torrent_status);
    renderBreakdown(BREAKDOWNS[1], stats.file_status);
    renderBreakdown(BREAKDOWNS[2], stats.event_levels);
    renderBreakdown(BREAKDOWNS[3], stats.transfer_methods);
    renderCompletionChart(stats.daily_completions || [], live);
    document.dispatchEvent(new CustomEvent('debridpulse:statistics-rendered', {detail: {period: period, live: live}}));
  }

  // One request; its answer is shown only if it is still the current one.
  async function read(explicit, waiters) {
    const mine = generation, period = currentPeriod;
    inflight = true;
    lastStart = Date.now();
    const started = performance.now();
    let stats = null, failure = null;
    try {
      stats = await api('GET', '/stats/detail?period=' + encodeURIComponent(period));
    } catch (error) {
      failure = error;
    }
    inflight = false;
    spacing = Math.min(REFRESH_SPACING_MAX_MS, Math.max(REFRESH_SPACING_MS, LATENCY_SPACING_FACTOR * (performance.now() - started)));
    const current = mine === generation && (explicit || shown());
    let accepted = null;
    if (current && stats) {
      render(stats, period, !explicit);
      lastAccepted = Date.now();
      accepted = stats;
    } else if (current && failure && explicit) {
      // A load the operator asked for says why it failed; a live re-read that
      // fails keeps the last answer on screen and says nothing.
      toast(sanitizeErrorMsg(failure.message), 'error');
    }
    waiters.forEach(function (resolve) { resolve(accepted); });
    if (queued) {
      const next = queued;
      queued = null;
      admit(next.explicit, next.waiters);
    }
    return accepted;
  }

  // Admit one re-read: never two in flight, live ones spaced, explicit ones
  // as soon as nothing is in flight.
  function admit(explicit, waiters) {
    waiters = waiters || [];
    if (!explicit && !shown()) { waiters.forEach(function (resolve) { resolve(null); }); return; }
    if (inflight) {
      queued = queued || {explicit: false, waiters: []};
      queued.explicit = queued.explicit || explicit;
      queued.waiters.push.apply(queued.waiters, waiters);
      return;
    }
    const wait = explicit ? 0 : lastStart + spacing - Date.now();
    if (wait > 0) {
      if (!timer) timer = setTimeout(function () { timer = null; admit(false); }, wait);
      waiters.forEach(function (resolve) { resolve(null); });
      return;
    }
    if (timer) { clearTimeout(timer); timer = null; }
    void read(explicit, waiters);
  }

  function invalidated() {
    if (currentPeriod !== null && shown()) admit(false);
  }

  // The view or its period changed: nothing already asked for may be shown.
  function retire() {
    generation++;
    if (timer) { clearTimeout(timer); timer = null; }
    if (queued) { queued.waiters.forEach(function (resolve) { resolve(null); }); queued = null; }
  }

  function loadDetailedStats(period) {
    const resolved = selectedPeriod(period);
    const chartTitle = document.getElementById('chart-title');
    if (chartTitle) chartTitle.textContent = CHART_FLAVOR[resolved] || 'Completed downloads in the selected period.';
    retire();
    currentPeriod = resolved;
    return new Promise(function (resolve) { admit(true, [resolve]); });
  }

  function install() {
    window.loadDetailedStats = loadDetailedStats;
    window.fmtDuration = formatCompactDuration;
    return true;
  }

  window.DPStatisticsLifecycle = Object.freeze({load: loadDetailedStats, install});
  install();
  document.addEventListener('debridpulse:theme-changed', applyChartPalette);
  document.addEventListener('debridpulse:statistics-invalidated', invalidated);
  // A reconnected stream may have missed changes: re-read once.
  document.addEventListener('debridpulse:pulse-connected', invalidated);
  document.addEventListener('debridpulse:pulse-heartbeat', function () {
    if (Date.now() - lastAccepted >= AGING_MS) invalidated();
  });
  document.addEventListener('debridpulse:navigation', function (event) {
    if (event.detail?.view !== 'stats') retire();
  });
})();
