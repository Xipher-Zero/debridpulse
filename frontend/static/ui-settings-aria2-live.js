/* DebridPulse v1.0.11 Settings aria2 operator escape hatch.
 *
 * This surface intentionally exposes the built-in aria2 engine beneath the
 * normal DebridPulse transfer workflow. Engine actions mutate aria2 only;
 * DebridPulse records are left for the normal reconciliation machinery to
 * observe afterward.
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
  const POLL_MS = 5000;
  const QUEUE_TIMEOUT_MS = 20000;
  const QUEUE_ID = 'dp-settings-aria2-downloads';

  let observer = null;
  let scheduled = false;
  let pollTimer = null;
  let refreshRunning = null;

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

  function cardMarkup() {
    return `
      <div class="card-header">
        <span class="card-title dp-settings-card-title--with-icon">
          <span class="dp-settings-aria2-live-icon" aria-hidden="true">
            <img src="/icons/dp/card-download.svg?v=1" alt="" decoding="async">
          </span>
          <span class="dp-settings-card-title-text">aria2 Live Downloads</span>
        </span>
        <div class="dp-settings-card-header-center">
          <span class="dp-settings-aria2-live-copy">Inspect and directly control DebridPulse's built-in aria2 queue.</span>
        </div>
        <div class="dp-settings-aria2-live-header-actions">
          <span class="dp-settings-aria2-live-status" data-dp-aria2-live-status>Built-in engine</span>
          <button type="button" class="btn btn-ghost btn-sm" data-dp-aria2-live-refresh>Refresh</button>
        </div>
      </div>
      <div class="card-body" data-dp-aria2-live-body>
        <div class="dp-settings-aria2-live-note">
          <b>Engine-level controls:</b> actions here change built-in aria2 directly. DebridPulse transfer records are not rewritten by this control surface.
        </div>
        <div id="${QUEUE_ID}" data-dp-aria2-live-queue="1" class="dp-settings-aria2-live-queue" aria-live="polite">
          <div class="empty">Loading built-in aria2 queue…</div>
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
    card.setAttribute('aria-label', 'aria2 Live Downloads');
    card.innerHTML = cardMarkup();

    const refresh = card.querySelector('[data-dp-aria2-live-refresh]');
    refresh?.addEventListener('click', () => void refreshQueue(true));

    panel.appendChild(card);
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
      button.title = 'Directly remove this GID from built-in aria2. DebridPulse transfer state is not changed by this action.';
      button.classList.add('dp-settings-aria2-live-remove');
    });
  }

  function setHeaderStatus(card, data) {
    const status = card?.querySelector('[data-dp-aria2-live-status]');
    if (!status) return;

    if (currentMode() !== 'builtin') {
      status.textContent = 'Built-in queue unavailable in External mode';
      return;
    }

    const summary = data?.summary || {};
    if (data && typeof data === 'object') {
      status.textContent = `Built-in · ${Number(summary.active || 0)} active · ${Number(summary.waiting || 0)} waiting`;
    } else {
      status.textContent = 'Built-in engine';
    }
  }

  function showQueueError(message) {
    const queue = queueNode();
    if (!queue) return;
    const error = document.createElement('div');
    error.className = 'aria2-error';
    error.textContent = `Queue error: ${String(message || 'Unable to load built-in aria2 queue')}`;
    queue.replaceChildren(error);
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

  async function refreshQueue(manual) {
    const builtin = currentMode() === 'builtin';
    if (!builtin) return null;
    if (!manual && !shouldRunLiveQueue()) return null;
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
        setHeaderStatus(card, data);
        return data;
      } catch (error) {
        showQueueError(error?.message || error);
        setHeaderStatus(card, null);
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

  function applyMode(card) {
    if (!card) return;
    const builtin = currentMode() === 'builtin';
    const body = card.querySelector('[data-dp-aria2-live-body]');
    const refresh = card.querySelector('[data-dp-aria2-live-refresh]');

    card.classList.toggle('is-external', !builtin);
    card.setAttribute('aria-disabled', builtin ? 'false' : 'true');
    if (body) body.hidden = !builtin;
    if (refresh) refresh.hidden = !builtin;
    setHeaderStatus(card, null);

    if (!builtin) {
      stopPolling();
      return;
    }

    startVisibleQueue();
  }

  function apply() {
    const view = root();
    if (!view) return;

    reorderTabs(view);
    const card = ensureCard(downloadsPanel());
    applyMode(card);
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
        const card = liveCard();
        applyMode(card);
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
