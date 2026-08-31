/* DebridPulse v1.0.11 Settings built-in engine-state escape hatch.
 *
 * This surface intentionally exposes the built-in aria2 engine beneath the
 * normal DebridPulse transfer workflow. Engine actions mutate aria2 directly;
 * DebridPulse remains the durable transfer record and reconciles afterward.
 */
(function () {
  'use strict';

  const TAB_ORDER = Object.freeze([
    'sources',
    'downloads',
    'extraction',
    'authentication',
    'notifications',
    'maintenance',
  ]);
  const FILTERS = Object.freeze(['all', 'active', 'waiting', 'paused', 'stopped']);
  const STOPPED_STATES = new Set(['complete', 'error', 'removed']);
  const POLL_MS = 5000;
  const QUEUE_TIMEOUT_MS = 20000;
  const QUEUE_ID = 'dp-settings-aria2-downloads';

  let observer = null;
  let scheduled = false;
  let pollTimer = null;
  let refreshRunning = null;
  let activeFilter = 'all';

  const root = () => document.getElementById('view-settings');
  const downloadsPanel = () => root()?.querySelector('[data-panel="downloads"]') || null;
  const modeControl = () => root()?.querySelector('[data-setting="aria2_mode"]') || null;
  const liveCard = () => root()?.querySelector('[data-dp-aria2-live-card="1"]') || null;
  const queueNode = () => liveCard()?.querySelector('[data-dp-aria2-live-queue="1"]') || null;

  function currentMode() {
    const control = modeControl();
    if (control) return String(control.value || 'builtin');
    try {
      return String((settingsData && settingsData.aria2_mode) || 'builtin');
    } catch (_) {
      return 'builtin';
    }
  }

  function settingsVisible() {
    return !!root()?.classList.contains('active');
  }

  function downloadsVisible() {
    const panel = downloadsPanel();
    return !!panel && !panel.hidden;
  }

  function shouldRunLiveQueue() {
    return settingsVisible() && downloadsVisible() && currentMode() === 'builtin';
  }

  function reorderTabs(view) {
    const tablist = view?.querySelector('.dp-settings-tabs');
    if (!tablist) return;

    const buttons = new Map(
      Array.from(tablist.querySelectorAll(':scope > [data-tab]'))
        .map(button => [String(button.dataset.tab || ''), button])
    );
    if (!TAB_ORDER.every(id => buttons.has(id))) return;

    const current = Array.from(tablist.querySelectorAll(':scope > [data-tab]'))
      .map(button => String(button.dataset.tab || ''));
    if (current.join('|') === TAB_ORDER.join('|')) return;

    for (const id of TAB_ORDER) tablist.appendChild(buttons.get(id));
  }

  function filterMarkup() {
    const labels = {
      all: 'All',
      active: 'Active',
      waiting: 'Waiting',
      paused: 'Paused',
      stopped: 'Stopped',
    };
    return FILTERS.map(id => `
      <button type="button"
              class="ftab${id === activeFilter ? ' active' : ''}"
              role="tab"
              aria-selected="${id === activeFilter ? 'true' : 'false'}"
              data-engine-filter="${id}">${labels[id]}</button>`).join('');
  }

  function cardMarkup() {
    return `
      <div class="card-header">
        <span class="card-title dp-settings-card-title--with-icon">
          <span class="dp-settings-aria2-live-icon" aria-hidden="true">
            <img src="/icons/dp/card-download.svg?v=1" alt="" decoding="async">
          </span>
          <span class="dp-settings-card-title-text">Built-In Download Engine State</span>
        </span>
        <div class="dp-settings-card-header-center">
          <span class="dp-settings-aria2-live-copy">Inspect and control the built-in aria2 engine.</span>
        </div>
        <div class="dp-settings-aria2-live-header-actions">
          <button type="button" class="btn btn-ghost btn-sm" data-dp-aria2-live-refresh>Refresh</button>
        </div>
      </div>
      <div class="card-body" data-dp-aria2-live-body>
        <div class="dp-settings-aria2-live-context">
          This reflects temporary aria2 runtime state, not transfer history. DebridPulse Downloads remains the historical record.
        </div>
        <div class="dp-settings-aria2-live-control-row">
          <div class="dp-settings-aria2-live-note">
            <div class="dp-settings-aria2-live-note-title">Direct Engine Controls</div>
            <div class="dp-settings-aria2-live-note-text">Bypasses normal DebridPulse transfer controls. Use for troubleshooting or recovery.</div>
          </div>
          <div class="dp-settings-aria2-live-tools">
            <div class="dp-settings-aria2-live-metrics" aria-label="Built-in aria2 engine metrics">
              <span data-dp-aria2-live-speed>0 KB/s</span>
              <span data-dp-aria2-live-remaining>— Remaining</span>
            </div>
            <div class="filter-tabs dp-settings-aria2-live-filters" role="tablist" aria-label="Filter built-in aria2 engine jobs">
              ${filterMarkup()}
            </div>
          </div>
        </div>
        <div id="${QUEUE_ID}" data-dp-aria2-live-queue="1" class="dp-settings-aria2-live-queue" aria-live="polite">
          <div class="empty">Loading built-in aria2 engine state…</div>
        </div>
      </div>`;
  }

  function ensureCard(panel) {
    if (!panel) return null;
    let card = panel.querySelector('[data-dp-aria2-live-card="1"]');
    if (card) return card;

    card = document.createElement('section');
    card.className = 'card dp-settings-card dp-settings-aria2-live-card';
    card.dataset.dpAria2LiveCard = '1';
    card.setAttribute('aria-label', 'Built-In Download Engine State');
    card.innerHTML = cardMarkup();

    const refresh = card.querySelector('[data-dp-aria2-live-refresh]');
    refresh?.addEventListener('click', () => void refreshQueue(true));

    card.addEventListener('click', event => {
      const filter = event.target.closest('[data-engine-filter]');
      if (!filter || !card.contains(filter)) return;
      const next = String(filter.dataset.engineFilter || 'all');
      if (!FILTERS.includes(next)) return;
      activeFilter = next;
      applyFilter();
    });

    panel.appendChild(card);

    /* Prime the queue exactly once when this Settings card is materialized.
       The initial read must not depend on Settings/Downloads visibility timing;
       visibility only controls the continuing poll loop. */
    void refreshQueue(false, true);
    return card;
  }

  function relabelEngineActions() {
    const queue = queueNode();
    if (!queue) return;

    queue.querySelectorAll('.aria2-actions button').forEach(button => {
      const label = String(button.textContent || '').trim();
      const fallback = String(button.dataset.defaultLabel || '').trim();
      if (label !== 'Remove' && fallback !== 'Remove') return;

      button.textContent = 'Remove from aria2';
      button.dataset.defaultLabel = 'Remove from aria2';
      button.title = 'Directly remove this GID from the built-in aria2 engine.';
      button.classList.add('dp-settings-aria2-live-remove');
    });
  }

  function orderedItems(data) {
    const items = Array.isArray(data?.items) ? data.items.slice() : [];
    const weight = {active: 0, waiting: 1, paused: 2, error: 3, complete: 4, removed: 5};
    return items.sort((a, b) => (weight[a?.status] ?? 9) - (weight[b?.status] ?? 9));
  }

  function filterGroup(status) {
    const value = String(status || '').toLowerCase();
    if (STOPPED_STATES.has(value)) return 'stopped';
    if (value === 'active' || value === 'waiting' || value === 'paused') return value;
    return value;
  }

  function updateMetrics(data) {
    const card = liveCard();
    if (!card) return;
    const summary = data?.summary || {};
    const speedNode = card.querySelector('[data-dp-aria2-live-speed]');
    const remainingNode = card.querySelector('[data-dp-aria2-live-remaining]');
    const speed = Number(summary.download_speed || 0);
    const remaining = Number(summary.remaining_length || 0);

    if (speedNode) {
      speedNode.textContent = typeof fmtSpeed === 'function' ? fmtSpeed(speed) : `${Math.max(0, speed)} B/s`;
    }
    if (remainingNode) {
      const formatted = remaining > 0 && typeof fmtSize === 'function' ? fmtSize(remaining) : '—';
      remainingNode.textContent = `${formatted} Remaining`;
    }
  }

  function updateFilterSelection() {
    const card = liveCard();
    if (!card) return;
    card.querySelectorAll('[data-engine-filter]').forEach(button => {
      const selected = String(button.dataset.engineFilter || '') === activeFilter;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-selected', selected ? 'true' : 'false');
    });
  }

  function applyFilter() {
    const queue = queueNode();
    if (!queue) return;
    updateFilterSelection();

    const jobs = Array.from(queue.querySelectorAll('.aria2-job'));
    let visible = 0;
    jobs.forEach(job => {
      const group = filterGroup(job.dataset.engineStatus);
      const show = activeFilter === 'all' || group === activeFilter;
      job.hidden = !show;
      if (show) visible += 1;
    });

    let filteredEmpty = queue.querySelector('[data-dp-aria2-filter-empty]');
    if (!jobs.length || visible > 0 || activeFilter === 'all') {
      filteredEmpty?.remove();
      return;
    }

    if (!filteredEmpty) {
      filteredEmpty = document.createElement('div');
      filteredEmpty.className = 'empty dp-settings-aria2-filter-empty';
      filteredEmpty.dataset.dpAria2FilterEmpty = '1';
      queue.appendChild(filteredEmpty);
    }
    const label = activeFilter.charAt(0).toUpperCase() + activeFilter.slice(1);
    filteredEmpty.textContent = `No ${label.toLowerCase()} jobs currently retained by aria2.`;
  }

  function postProcessQueue(data) {
    const queue = queueNode();
    if (!queue) return;

    queue.querySelector('.aria2-summary')?.remove();

    const items = orderedItems(data);
    const jobs = Array.from(queue.querySelectorAll('.aria2-job'));
    jobs.forEach((job, index) => {
      job.dataset.engineStatus = String(items[index]?.status || '').toLowerCase();
    });

    if (!items.length) {
      const empty = queue.querySelector('.empty');
      if (empty) empty.textContent = 'No jobs currently retained by aria2.';
    }

    updateMetrics(data);
    applyFilter();
  }

  function showQueueError(message) {
    const queue = queueNode();
    if (!queue) return;
    const error = document.createElement('div');
    error.className = 'aria2-error';
    error.textContent = `Queue error: ${String(message || 'Unable to load built-in aria2 engine state')}`;
    queue.replaceChildren(error);
    updateMetrics(null);
  }

  function renderIntoSettingsQueue(data) {
    const queue = queueNode();
    if (!queue) throw new Error('Settings aria2 queue target is unavailable');
    if (typeof renderAria2Downloads !== 'function') {
      throw new Error('aria2 queue renderer is unavailable');
    }

    /* The inherited renderer is intentionally reused, but it historically
       resolves a global #aria2-downloads node. Give it the Settings queue as
       an explicit temporary target and displace any stale legacy target for
       the duration of the render. */
    const displaced = Array.from(document.querySelectorAll('[id="aria2-downloads"]'))
      .filter(element => element !== queue);
    displaced.forEach((element, index) => {
      element.id = `dp-legacy-aria2-downloads-${index}`;
    });

    const originalId = queue.id;
    queue.id = 'aria2-downloads';
    try {
      renderAria2Downloads(data);
    } finally {
      queue.id = originalId || QUEUE_ID;
      displaced.forEach(element => {
        element.id = 'aria2-downloads';
      });
    }

    postProcessQueue(data);
  }

  function stopPolling() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
  }

  function schedulePoll(delay = POLL_MS) {
    stopPolling();
    if (!shouldRunLiveQueue()) return;
    pollTimer = setTimeout(async () => {
      pollTimer = null;
      await refreshQueue(false);
      if (shouldRunLiveQueue()) schedulePoll(POLL_MS);
    }, Math.max(0, delay));
  }

  async function refreshQueue(manual, force = false) {
    const builtin = currentMode() === 'builtin';
    if (!builtin) return null;
    if (!manual && !force && !shouldRunLiveQueue()) return null;
    if (refreshRunning) return refreshRunning;

    const card = liveCard();
    const refresh = card?.querySelector('[data-dp-aria2-live-refresh]');

    if (typeof api !== 'function') {
      showQueueError('Application API client is unavailable');
      return null;
    }

    refreshRunning = (async () => {
      if (manual && refresh) {
        refresh.disabled = true;
        refresh.textContent = 'Refreshing…';
      }
      try {
        const data = await api('GET', '/aria2/downloads', null, QUEUE_TIMEOUT_MS);
        renderIntoSettingsQueue(data);
        relabelEngineActions();
        return data;
      } catch (error) {
        showQueueError(error?.message || error);
        return null;
      } finally {
        if (manual && refresh) {
          refresh.disabled = false;
          refresh.textContent = 'Refresh';
        }
        refreshRunning = null;
      }
    })();

    return refreshRunning;
  }

  function startVisibleQueue() {
    if (!shouldRunLiveQueue()) {
      stopPolling();
      return;
    }
    void refreshQueue(false);
    schedulePoll(POLL_MS);
  }

  function syncCardForMode(panel = downloadsPanel()) {
    const builtin = currentMode() === 'builtin';
    if (!builtin) {
      stopPolling();
      liveCard()?.remove();
      return null;
    }

    const card = ensureCard(panel);
    if (card) startVisibleQueue();
    return card;
  }

  function apply() {
    const view = root();
    if (!view) return;

    reorderTabs(view);
    syncCardForMode(downloadsPanel());
  }

  function observe(view) {
    if (!observer || !view) return;
    /* The clean Settings renderer replaces the root's direct child on render.
       Queue updates happen deeper in the subtree and must not retrigger this
       structural observer every five seconds. */
    observer.observe(view, {childList: true, subtree: false});
  }

  function applyWithoutSelfObservation() {
    const view = root();
    if (!view) return;
    if (observer) observer.disconnect();
    try {
      apply();
    } finally {
      observe(view);
    }
  }

  function scheduleApply() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => {
      scheduled = false;
      applyWithoutSelfObservation();
    });
  }

  function bindInteractions(view) {
    if (!view || view.dataset.dpSettingsAria2LiveBound === '1') return;
    view.dataset.dpSettingsAria2LiveBound = '1';

    view.addEventListener('change', event => {
      if (!event.target.matches('[data-setting="aria2_mode"]')) return;
      queueMicrotask(() => {
        syncCardForMode(downloadsPanel());
      });
    });

    view.addEventListener('click', event => {
      if (!event.target.closest('.dp-settings-tabs [data-tab]')) return;
      queueMicrotask(startVisibleQueue);
    });
  }

  function wrapEngineActionRefresh() {
    if (document.documentElement.dataset.dpAria2LiveActionWrapped === '1') return;
    if (typeof aria2DownloadAction !== 'function') return;

    const original = aria2DownloadAction;
    window.aria2DownloadAction = async function (...args) {
      const result = await original.apply(this, args);
      if (shouldRunLiveQueue()) {
        await refreshQueue(false);
        schedulePoll(POLL_MS);
      }
      return result;
    };
    document.documentElement.dataset.dpAria2LiveActionWrapped = '1';
  }

  function attach() {
    const view = root();
    if (!view) return;

    bindInteractions(view);
    wrapEngineActionRefresh();

    if (!observer) observer = new MutationObserver(scheduleApply);
    observe(view);
    applyWithoutSelfObservation();

    document.addEventListener('visibilitychange', () => {
      if (document.hidden) stopPolling();
      else startVisibleQueue();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', attach, {once: true});
  } else {
    attach();
  }
})();