/* DebridPulse v1.0.11 Statistics Batch 4 presentation contract.
 *
 * Presentation only. The existing statistics API and Batch 3 label semantics
 * remain authoritative. This layer adds adaptive lower-card composition and a
 * bounded full-list overflow view after the canonical Statistics render event.
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

  function initialize() {
    loadBatchStyles();
    applyAdaptiveBreakdowns();
    document.addEventListener('debridpulse:statistics-rendered', function () {
      applyAdaptiveBreakdowns();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, {once: true});
  } else {
    initialize();
  }
})();
