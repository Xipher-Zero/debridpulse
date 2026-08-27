/* DebridPulse v1.0.11 live-review behavior corrections.
 *
 * Scope is intentionally narrow:
 *   1. Keep the aria2 topbar control structurally present at first paint with
 *      neutral values, then hydrate it asynchronously once settings/runtime are
 *      available.
 *   2. Make the theme icon describe the action/destination: sun while dark,
 *      moon while light, while remaining compatible with the canonical Lucide
 *      decorator in operator-title.js.
 *   3. Build the Statistics presentation shell around the existing controls and
 *      data nodes, select the reviewed 7-day default, and recolor only the
 *      existing Chart.js completion dataset.
 *
 * Core transfer behavior, statistics I/O and aria2 ownership remain in app.js
 * and backend code.
 */
(function () {
  'use strict';

  const THEME_GLYPHS = Object.freeze({
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
    moon: '<path d="M20.985 12.486a9 9 0 1 1-9.473-9.472c.405-.022.617.46.402.803a6 6 0 0 0 8.268 8.268c.344-.215.825-.004.803.401"/>'
  });

  const STATISTICS_SUBTITLE = 'Historical transfer performance and completion metrics.';

  function themeSvg(name) {
    return '<svg class="lucide dp-utility-icon" data-dp-lucide="' + name + '" xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + THEME_GLYPHS[name] + '</svg>';
  }

  function syncThemeActionIcon() {
    const button = document.getElementById('theme-toggle');
    if (!button) return;

    const isLight = document.body.classList.contains('light');
    const action = isLight ? 'Switch to dark mode' : 'Switch to light mode';
    const iconName = isLight ? 'moon' : 'sun';

    if (!button.querySelector('[data-dp-lucide="' + iconName + '"]')) {
      button.innerHTML = themeSvg(iconName);
    }
    if (button.title !== action) button.title = action;
    if (button.getAttribute('aria-label') !== action) {
      button.setAttribute('aria-label', action);
    }
  }

  function installThemeActionSemantics() {
    const button = document.getElementById('theme-toggle');
    if (!button || button.dataset.dpActionSemantics === '1') return;

    button.dataset.dpActionSemantics = '1';
    syncThemeActionIcon();

    const observer = new MutationObserver(function () {
      syncThemeActionIcon();
      applyStatisticsChartPalette();
    });
    observer.observe(document.body, {
      attributes: true,
      attributeFilter: ['class'],
    });
    observer.observe(button, {
      childList: true,
      characterData: true,
      subtree: true,
    });
  }

  function initializeAria2TopbarPlaceholder() {
    const badge = document.getElementById('aria2-speed-badge');
    if (!badge) return;

    /* app.js owns this state via `var`, so it is safe to seed neutral values
       here. Using string "0" for maxDl preserves a visible zero through the
       legacy `s.maxDl || '—'` renderer until real runtime data replaces it. */
    if (typeof window._aria2BadgeState === 'object' && window._aria2BadgeState) {
      window._aria2BadgeState.active = 0;
      window._aria2BadgeState.maxDl = '0';
      window._aria2BadgeState.liveBps = 0;
      window._aria2BadgeState.limitBps = 0;
      window._aria2BadgeState.externalControl = false;
    }

    const active = document.getElementById('aria2-badge-active');
    const max = document.getElementById('aria2-badge-max');
    const speed = document.getElementById('aria2-badge-speed');
    const limit = document.getElementById('aria2-badge-limit');

    if (active) active.textContent = '0';
    if (max) max.textContent = '0';
    if (speed) speed.textContent = '0 KB/s';
    if (limit) limit.textContent = 'Unlimited';

    badge.style.display = 'flex';
    badge.dataset.dpInitialPlaceholder = '1';
  }

  function hydrateAria2TopbarSoon() {
    let attempts = 0;
    const maxAttempts = 40;

    const attempt = function () {
      attempts += 1;

      let hasSettings = false;
      try {
        hasSettings =
          typeof settingsData !== 'undefined' &&
          settingsData &&
          Object.keys(settingsData).length > 0;
      } catch (_) {
        hasSettings = false;
      }

      if (!hasSettings) {
        if (attempts < maxAttempts) setTimeout(attempt, 100);
        return;
      }

      if (typeof window.loadAria2Runtime === 'function') {
        Promise.resolve(window.loadAria2Runtime())
          .catch(function () {})
          .finally(function () {
            document.body.classList.add('dp-aria2-hydrated');
          });
      }
    };

    setTimeout(attempt, 0);
  }

  function statisticsTitleBlock() {
    const title = document.createElement('div');
    title.className = 'dp-stats-master-title';

    const icon = document.createElement('img');
    icon.src = '/icons/dp/statistics.svg';
    icon.alt = '';
    icon.setAttribute('aria-hidden', 'true');
    icon.className = 'dp-icon dp-statistics-title-icon';

    const copy = document.createElement('div');
    copy.className = 'dp-stats-heading-copy';

    const heading = document.createElement('span');
    heading.className = 'dp-stats-heading';
    heading.textContent = 'Statistics';

    const subtitle = document.createElement('span');
    subtitle.className = 'dp-stats-subtitle';
    subtitle.textContent = STATISTICS_SUBTITLE;

    copy.appendChild(heading);
    copy.appendChild(subtitle);
    title.appendChild(icon);
    title.appendChild(copy);
    return title;
  }

  function ensureStatisticsBreakdownGrid(body) {
    if (!body || body.querySelector('.dp-stats-breakdown-grid')) return;

    const splitGrids = Array.from(body.children).filter(function (node) {
      return node.classList && node.classList.contains('split-grid');
    });
    if (!splitGrids.length) return;

    const cards = [];
    splitGrids.forEach(function (grid) {
      grid.querySelectorAll(':scope > .list-card').forEach(function (card) {
        cards.push(card);
      });
    });
    if (!cards.length) return;

    const breakdown = document.createElement('div');
    breakdown.className = 'dp-stats-breakdown-grid';
    splitGrids[0].parentNode.insertBefore(breakdown, splitGrids[0]);
    cards.forEach(function (card) { breakdown.appendChild(card); });
    splitGrids.forEach(function (grid) { grid.remove(); });
  }

  function ensureStatisticsArchitecture() {
    const view = document.getElementById('view-stats');
    const tabs = document.getElementById('stats-period-tabs');
    if (!view || !tabs) return;

    view.classList.add('card', 'dp-statistics-master');

    const header = tabs.parentElement;
    if (!header) return;
    header.classList.add('card-header', 'dp-stats-master-header');
    header.removeAttribute('style');

    if (!header.querySelector('.dp-stats-master-title')) {
      header.insertBefore(statisticsTitleBlock(), header.firstChild);
    }

    const periodLabel = Array.from(header.children).find(function (node) {
      return node.tagName === 'SPAN' && (node.textContent || '').trim() === 'Period:';
    });
    if (periodLabel) periodLabel.classList.add('dp-stats-period-label');

    let body = view.querySelector(':scope > .dp-stats-master-body');
    if (!body) {
      body = document.createElement('div');
      body.className = 'card-body dp-stats-master-body';
      while (header.nextSibling) body.appendChild(header.nextSibling);
      view.appendChild(body);
    }

    const chart = body.querySelector('#daily-chart');
    const chartCard = chart && chart.closest('.scard');
    if (chartCard) chartCard.classList.add('dp-stats-chart');

    ensureStatisticsBreakdownGrid(body);
  }

  function setStatisticsSevenDayDefault() {
    const tabs = document.getElementById('stats-period-tabs');
    if (!tabs || tabs.dataset.dpDefaultPeriod === '7d') return;

    const items = Array.from(tabs.querySelectorAll('.ftab'));
    const sevenDay = items.find(function (item) {
      return item.dataset.period === '7d';
    });
    if (!sevenDay) return;

    items.forEach(function (item) {
      const selected = item === sevenDay;
      item.classList.toggle('active', selected);
      item.setAttribute('aria-selected', selected ? 'true' : 'false');
    });
    tabs.dataset.dpDefaultPeriod = '7d';
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

  function applyStatisticsChartPalette() {
    const canvas = document.getElementById('daily-chart');
    const chart = canvas && canvas._ci;
    const dataset = chart && chart.data && chart.data.datasets && chart.data.datasets[0];
    if (!dataset) return;

    const isLight = document.body.classList.contains('light');
    dataset.backgroundColor = function (context) {
      return statisticsPurpleGradient(context.chart);
    };
    dataset.borderColor = isLight
      ? 'rgba(126, 75, 187, .72)'
      : 'rgba(166, 70, 244, .84)';
    dataset.borderWidth = 1;
    if (typeof chart.update === 'function') chart.update('none');
  }

  function installStatisticsDetailedStatsGuard() {
    let attempts = 0;
    const maxAttempts = 40;

    const attempt = function () {
      attempts += 1;
      const legacy = window.loadDetailedStats;
      if (typeof legacy !== 'function') {
        if (attempts < maxAttempts) setTimeout(attempt, 50);
        return;
      }
      if (legacy.dpStatisticsBatch1 === '1') return;

      const wrapped = async function (period) {
        const active = document.querySelector('#stats-period-tabs .ftab.active');
        const resolved = period || (active && active.dataset.period) || '7d';
        const result = await legacy.call(this, resolved);
        applyStatisticsChartPalette();
        return result;
      };
      wrapped.dpStatisticsBatch1 = '1';
      window.loadDetailedStats = wrapped;
    };

    attempt();
  }

  function initializeStatisticsBatch() {
    ensureStatisticsArchitecture();
    setStatisticsSevenDayDefault();
    installStatisticsDetailedStatsGuard();
    applyStatisticsChartPalette();
  }

  function initialize() {
    installThemeActionSemantics();
    initializeAria2TopbarPlaceholder();
    hydrateAria2TopbarSoon();
    initializeStatisticsBatch();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, {once: true});
  } else {
    initialize();
  }
})();
