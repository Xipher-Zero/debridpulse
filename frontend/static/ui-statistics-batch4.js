/* DebridPulse v1.0.11 Statistics Batch 4 presentation contract.
 *
 * Presentation only. The existing statistics API and Batch 3 label semantics
 * remain authoritative. This layer adds adaptive lower-card composition and a
 * bounded full-list overflow view after those renderers finish.
 */
(function () {
  'use strict';

  const BREAKDOWNS = Object.freeze([
    {id: 'detail-torrent-status', title: 'Download Status'},
    {id: 'detail-file-status', title: 'File Status'},
    {id: 'detail-event-levels', title: 'Monitor Levels'},
    {id: 'detail-sources', title: 'Top Sources'},
  ]);

  const MAX_VISIBLE = 10;
  const TWO_COLUMN_THRESHOLD = 6;

  function loadBatchStyles() {
    if (document.querySelector('link[data-dp-statistics-batch4]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/ui-statistics-batch4.css?v=1';
    link.dataset.dpStatisticsBatch4 = '1';
    document.head.appendChild(link);
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

  function configureOverflow(card, title, entries, body) {
    const overflow = entries.length > MAX_VISIBLE;
    card._dpStatsOverflowEntries = entries.map(function (entry) { return Object.assign({}, entry); });
    card.dataset.dpStatsOverflowTitle = title;
    bindOverflowCard(card);

    card.classList.toggle('dp-stats-overflow-card', overflow);
    if (overflow) {
      const more = entries.length - MAX_VISIBLE;
      card.setAttribute('role', 'button');
      card.tabIndex = 0;
      card.setAttribute('aria-label', title + '. Showing top 10. View ' + more + ' more.');
      const indicator = document.createElement('div');
      indicator.className = 'dp-stats-more';
      indicator.textContent = '+ ' + more + ' more';
      body.appendChild(indicator);
    } else {
      card.removeAttribute('role');
      card.removeAttribute('tabindex');
      card.removeAttribute('aria-label');
    }
  }

  function applyAdaptiveBreakdown(definition) {
    const body = document.getElementById(definition.id);
    if (!body) return false;

    /* A marker means this exact DOM generation is already normalized. Legacy
       loadDetailedStats replaces the body contents on the next refresh, which
       removes the marker and lets us capture the new complete result set. */
    if (body.querySelector(':scope > .dp-stats-adaptive-list')) return true;

    const entries = captureEntries(body);
    if (!entries.length) return false;

    const card = body.closest('.list-card');
    if (!card) return false;

    body.replaceChildren(buildVisibleList(entries));
    configureOverflow(card, definition.title, entries, body);
    return true;
  }

  function applyAdaptiveBreakdowns() {
    BREAKDOWNS.forEach(applyAdaptiveBreakdown);
  }

  function installDetailedStatsGuard() {
    let attempts = 0;
    const attempt = function () {
      attempts += 1;
      const previous = window.loadDetailedStats;
      if (typeof previous !== 'function' || previous.dpStatisticsBatch3 !== '1') {
        if (attempts < 160) setTimeout(attempt, 50);
        return;
      }
      if (previous.dpStatisticsBatch4 === '1') return;

      const wrapped = async function () {
        const result = await previous.apply(this, arguments);
        applyAdaptiveBreakdowns();
        return result;
      };
      wrapped.dpStatisticsBatch4 = '1';
      window.loadDetailedStats = wrapped;
    };
    attempt();
  }

  function initialize() {
    loadBatchStyles();
    installDetailedStatsGuard();

    let attempts = 0;
    const settle = function () {
      attempts += 1;
      applyAdaptiveBreakdowns();
      const ready = BREAKDOWNS.every(function (definition) {
        const body = document.getElementById(definition.id);
        return !!body && (!!body.querySelector(':scope > .dp-stats-adaptive-list') || !!body.querySelector('.empty'));
      });
      if (!ready && attempts < 160) setTimeout(settle, 50);
    };
    settle();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, {once: true});
  } else {
    initialize();
  }
})();
