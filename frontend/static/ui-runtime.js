/* DebridPulse v1.0.11 structural presentation runtime.
 * Presentation only: preserves the established backend/API and app.js IDs.
 */
(function () {
  'use strict';

  const DP_ICON_BASE = '/icons/dp/';
  const METRIC_HISTORY_KEY = 'debridpulse.dashboard.metric-history.v1';
  const METRIC_HISTORY_LIMIT = 30;
  const METRIC_SAMPLE_INTERVAL_MS = 15000;
  const HERO_METRICS = {
    's-total':      {key: 'total',      label: 'Total downloads'},
    's-completed':  {key: 'completed',  label: 'Completed'},
    's-active':     {key: 'active',     label: 'Active now'},
    's-processing': {key: 'processing', label: 'Processing'},
    's-error':      {key: 'errors',     label: 'Errors'},
    's-size':       {key: 'downloaded', label: 'Total downloaded'}
  };
  const SUBTITLES = {
    Dashboard: 'Overview of your download activities and system status.',
    Downloads: 'Inspect, filter, and control queued and active transfers.',
    'Event Log': 'Recent transfer activity, decisions, warnings, and errors.',
    'Activity Log': 'Recent transfer activity, decisions, warnings, and errors.',
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
    if (!/style-v11\.css\?v=21$/.test(link.href)) link.href = '/style-v11.css?v=21';
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
      arrowRight: '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
      refresh: '<path d="M20 11a8.1 8.1 0 0 0-15.5-2M4 4v5h5"/><path d="M4 13a8.1 8.1 0 0 0 15.5 2M20 20v-5h-5"/>'
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

  function numeric(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.max(0, parsed) : 0;
  }

  function dashboardMetricSnapshot(stats) {
    const byStatus = stats && stats.by_status && typeof stats.by_status === 'object'
      ? stats.by_status
      : {};
    const total = Object.values(byStatus).reduce(function (sum, value) {
      return sum + numeric(value);
    }, 0);
    return {
      ts: Date.now(),
      total: total,
      completed: numeric(stats && (stats.completed_count ?? byStatus.completed)),
      active: numeric(stats && (stats.active_operations ?? stats.active_downloads)),
      processing: numeric(byStatus.processing) + numeric(byStatus.uploading),
      errors: numeric(stats && (stats.error_count ?? byStatus.error)),
      downloaded: numeric(stats && stats.total_completed_bytes)
    };
  }

  function readMetricHistory() {
    try {
      const parsed = JSON.parse(localStorage.getItem(METRIC_HISTORY_KEY) || '[]');
      if (!Array.isArray(parsed)) return [];
      return parsed.filter(function (sample) {
        return sample && Number.isFinite(Number(sample.ts));
      }).slice(-METRIC_HISTORY_LIMIT);
    } catch (_) {
      return [];
    }
  }

  function writeMetricHistory(samples) {
    try {
      localStorage.setItem(METRIC_HISTORY_KEY, JSON.stringify(samples.slice(-METRIC_HISTORY_LIMIT)));
    } catch (_) {
      /* Storage can be unavailable in hardened/private browser contexts. */
    }
  }

  function makeSparkline(card, index) {
    if (!card || card.querySelector('.dp-card-spark')) return;
    const gradientId = 'dp-card-spark-fill-' + index;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'dp-card-spark');
    svg.setAttribute('viewBox', '0 0 100 24');
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('aria-hidden', 'true');
    svg.innerHTML =
      '<defs><linearGradient id="' + gradientId + '" x1="0" y1="0" x2="0" y2="1">' +
        '<stop offset="0%" stop-color="currentColor" stop-opacity=".34"/>' +
        '<stop offset="100%" stop-color="currentColor" stop-opacity="0"/>' +
      '</linearGradient></defs>' +
      '<polygon class="dp-card-spark-fill" fill="url(#' + gradientId + ')" points=""/>' +
      '<polyline class="dp-card-spark-line" points=""/>' +
      '<circle class="dp-card-spark-point" cx="50" cy="12" r="1.45" opacity="0"/>';
    card.appendChild(svg);
  }

  function sparklinePoints(values) {
    if (!Array.isArray(values) || values.length < 2) return '';
    const clean = values.map(numeric);
    const min = Math.min.apply(null, clean);
    const max = Math.max.apply(null, clean);
    const span = max - min;
    return clean.map(function (value, index) {
      const x = clean.length === 1 ? 50 : (index / (clean.length - 1)) * 100;
      const y = span === 0 ? 12 : 20 - ((value - min) / span) * 16;
      return x.toFixed(2) + ',' + y.toFixed(2);
    }).join(' ');
  }

  function renderDashboardMetricHistory(samples) {
    Object.entries(HERO_METRICS).forEach(function ([valueId, metric]) {
      const value = document.getElementById(valueId);
      const card = value && value.closest('.dash-hero-stat');
      const svg = card && card.querySelector('.dp-card-spark');
      if (!card || !svg) return;

      const values = samples.map(function (sample) {
        return numeric(sample[metric.key]);
      });
      const line = svg.querySelector('.dp-card-spark-line');
      const fill = svg.querySelector('.dp-card-spark-fill');
      const point = svg.querySelector('.dp-card-spark-point');
      const points = sparklinePoints(values);

      card.dataset.dpMetric = metric.key;
      card.title = metric.label + ' — sparkline shows recent live samples of this exact card metric.';

      if (values.length >= 2 && points) {
        line.setAttribute('points', points);
        fill.setAttribute('points', '0,24 ' + points + ' 100,24');
        point.setAttribute('opacity', '0');
      } else if (values.length === 1) {
        line.setAttribute('points', '');
        fill.setAttribute('points', '');
        point.setAttribute('cx', '50');
        point.setAttribute('cy', '12');
        point.setAttribute('opacity', '1');
      } else {
        line.setAttribute('points', '');
        fill.setAttribute('points', '');
        point.setAttribute('opacity', '0');
      }
    });
  }

  function recordDashboardMetricHistory(stats) {
    if (!stats || typeof stats !== 'object') return;
    const snapshot = dashboardMetricSnapshot(stats);
    const samples = readMetricHistory();
    const last = samples[samples.length - 1];
    const metricKeys = Object.values(HERO_METRICS).map(function (metric) { return metric.key; });
    const changed = !last || metricKeys.some(function (key) {
      return numeric(last[key]) !== numeric(snapshot[key]);
    });
    const due = !last || snapshot.ts - numeric(last.ts) >= METRIC_SAMPLE_INTERVAL_MS;

    if (changed || due) {
      samples.push(snapshot);
      while (samples.length > METRIC_HISTORY_LIMIT) samples.shift();
      writeMetricHistory(samples);
    }
    renderDashboardMetricHistory(samples);
  }

  function installMetricHistoryHook() {
    const previous = window.updateOperatorTitle;
    if (typeof previous !== 'function' || previous.dpDashboardMetricHook === '1') return;
    const wrapped = function (stats) {
      recordDashboardMetricHistory(stats);
      return previous.apply(this, arguments);
    };
    wrapped.dpDashboardMetricHook = '1';
    window.updateOperatorTitle = wrapped;

    if (document.documentElement.dataset.dpDashboardMetricSeeded !== '1') {
      document.documentElement.dataset.dpDashboardMetricSeeded = '1';
      setTimeout(function () {
        if (typeof window.loadStats === 'function') window.loadStats();
      }, 0);
    }
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
    renderDashboardMetricHistory(readMetricHistory());
  }

  function normalizeSpeedCapArrow() {
    const arrow = document.querySelector('#aria2-cap-toggle span[aria-hidden="true"]');
    if (!arrow) return;
    arrow.textContent = '▼';
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
    normalizeDpButton(document.getElementById('btn-recover-all'), 'retry-borderless.svg');
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
      copy.innerHTML = '<span class="dp-activity-heading">Activity Log</span><span class="dp-activity-subtitle">Recent transfer activity, decisions, warnings, and errors.</span>';
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
      if (!list.dataset.dpActivityObserved) {
        list.dataset.dpActivityObserved = '1';
        new MutationObserver(normalizeActivityRows).observe(list, {childList: true, subtree: true});
      }
    }
    normalizeActivityRows();
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
    normalizeSpeedCapArrow();
    installMetricHistoryHook();
    decorateQuickAdd();
    decorateRecentActivity();
    decorateActivityLog();
    moveDashboardKpisToStatistics();
  }

  initialize();
  document.addEventListener('DOMContentLoaded', initialize, {once: true});
})();