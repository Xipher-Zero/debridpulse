/* aria2 engine-state queue (Settings → Downloads).
 *
 * This surface intentionally exposes the aria2 engine beneath the
 * normal DebridPulse transfer workflow. Engine actions mutate aria2 directly;
 * DebridPulse remains the durable transfer record and reconciles afterward.
 *
 * The card's structure is part of the Settings markup (ui-settings-page.js).
 * This module owns the queue inside it: polling, the job rows it renders, the
 * filter selection, the live metrics, and the direct engine actions.
 */
(function () {
  'use strict';

  const FILTERS = Object.freeze(['all', 'active', 'waiting', 'paused', 'stopped']);
  const STOPPED_STATES = new Set(['complete', 'error', 'removed']);
  const STATUS_WEIGHT = Object.freeze({active: 0, waiting: 1, paused: 2, error: 3, complete: 4, removed: 5});
  const POLL_MS = 5000;
  const QUEUE_TIMEOUT_MS = 20000;
  const ACTION_LABELS = Object.freeze({pause: 'Pausing…', resume: 'Resuming…', remove: 'Removing…'});

  let pollTimer = null;
  let refreshRunning = null;
  let activeFilter = 'all';
  let primedCard = null;

  const root = () => document.getElementById('view-settings');
  const downloadsPanel = () => root()?.querySelector('[data-panel="downloads"]') || null;
  const liveCard = () => root()?.querySelector('[data-dp-aria2-live-card="1"]') || null;
  const queueNode = () => liveCard()?.querySelector('[data-dp-aria2-live-queue="1"]') || null;

  function settingsVisible() {
    return !!root()?.classList.contains('active');
  }

  function downloadsVisible() {
    const panel = downloadsPanel();
    return !!panel && !panel.hidden;
  }

  function shouldRunLiveQueue() {
    return settingsVisible() && downloadsVisible();
  }

  function orderedItems(data) {
    const items = Array.isArray(data?.items) ? data.items.slice() : [];
    return items.sort((a, b) => (STATUS_WEIGHT[a?.status] ?? 9) - (STATUS_WEIGHT[b?.status] ?? 9));
  }

  function filterGroup(status) {
    const value = String(status || '').toLowerCase();
    return STOPPED_STATES.has(value) ? 'stopped' : value;
  }

  function statusBadge(status) {
    const map = {active: 'Downloading', waiting: 'Waiting', paused: 'Paused', complete: 'Complete', error: 'Error', removed: 'Removed'};
    const cls = status === 'active' ? 'downloading' : status === 'complete' ? 'completed' : status === 'error' ? 'error' : status === 'paused' ? 'paused' : 'queued';
    return `<span class="badge badge-${cls}">${esc(map[status] || status || 'Unknown')}</span>`;
  }

  function jobMarkup(job) {
    const gid = esc(job.gid);
    const canPause = job.status === 'active' || job.status === 'waiting';
    const canResume = job.status === 'paused';
    const files = (job.files || []).slice(0, 4).map(file => `
      <div title="${esc(file.path || '')}">
        ${esc(file.name || file.path || 'file')} · ${Math.max(0, file.progress || 0).toFixed(1)}% · ${fmtSize(file.completed_length || 0)} / ${fmtSize(file.length || 0)}
      </div>`).join('');
    const more = (job.files || []).length > 4 ? `<div>+ ${(job.files || []).length - 4} more file(s)</div>` : '';
    const error = job.error_message ? `<div class="aria2-error">${esc((job.error || {}).category || '')} ${esc(job.error_message)}</div>` : '';
    return `
      <div class="aria2-job" data-engine-status="${esc(String(job.status || '').toLowerCase())}">
        <div class="aria2-job-top">
          <div class="aria2-job-title">
            <div class="aria2-job-name" title="${esc(job.name || '')}">${esc(job.name || job.gid || 'aria2 job')}</div>
            <div class="aria2-job-meta" title="${esc(job.path || '')}">${gid}${job.path ? ' · ' + esc(job.path) : ''}</div>
          </div>
          <div class="aria2-actions">
            ${canPause ? `<button class="btn btn-ghost btn-sm" data-aria2-action="pause" data-gid="${gid}">Pause</button>` : ''}
            ${canResume ? `<button class="btn btn-blue btn-sm" data-aria2-action="resume" data-gid="${gid}">Resume</button>` : ''}
            <button class="btn btn-danger btn-sm dp-settings-aria2-live-remove" data-aria2-action="remove" data-gid="${gid}" data-default-label="Remove from aria2" title="Directly remove this GID from the aria2 engine.">Remove from aria2</button>
          </div>
        </div>
        <div>${progress(job.progress || 0, job.status === 'complete' ? 'completed' : 'downloading')}</div>
        <div class="aria2-job-grid">
          <div><div class="aria2-k">Status</div><div class="aria2-v">${statusBadge(job.status)}</div></div>
          <div><div class="aria2-k">Speed</div><div class="aria2-v">${fmtSpeed(job.download_speed || 0)}</div></div>
          <div><div class="aria2-k">Done</div><div class="aria2-v">${fmtSize(job.completed_length || 0)} / ${fmtSize(job.total_length || 0)}</div></div>
          <div><div class="aria2-k">Remaining</div><div class="aria2-v">${fmtSize(job.remaining_length || 0)}</div></div>
        </div>
        ${error}
        ${(files || more) ? `<div class="aria2-file-list">${files}${more}</div>` : ''}
      </div>`;
  }

  function updateMetrics(data) {
    const card = liveCard();
    if (!card) return;
    const summary = data?.summary || {};
    const speedNode = card.querySelector('[data-dp-aria2-live-speed]');
    const remainingNode = card.querySelector('[data-dp-aria2-live-remaining]');
    const speed = Number(summary.download_speed || 0);
    const remaining = Number(summary.remaining_length || 0);
    if (speedNode) speedNode.textContent = typeof fmtSpeed === 'function' ? fmtSpeed(speed) : `${Math.max(0, speed)} B/s`;
    if (remainingNode) {
      const formatted = remaining > 0 && typeof fmtSize === 'function' ? fmtSize(remaining) : '—';
      remainingNode.textContent = `${formatted} Remaining`;
    }
  }

  function updateFilterSelection() {
    liveCard()?.querySelectorAll('[data-engine-filter]').forEach(button => {
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
      const show = activeFilter === 'all' || filterGroup(job.dataset.engineStatus) === activeFilter;
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

  function renderQueue(data) {
    const queue = queueNode();
    if (!queue) return;
    const items = orderedItems(data);
    queue.innerHTML = items.length
      ? items.map(jobMarkup).join('')
      : '<div class="empty">No jobs currently retained by aria2.</div>';
    updateMetrics(data);
    applyFilter();
  }

  function showQueueError(message) {
    const queue = queueNode();
    if (!queue) return;
    const error = document.createElement('div');
    error.className = 'aria2-error';
    error.textContent = `Queue error: ${String(message || 'Unable to load aria2 engine state')}`;
    queue.replaceChildren(error);
    updateMetrics(null);
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
    if (!manual && !force && !shouldRunLiveQueue()) return null;
    if (refreshRunning) return refreshRunning;

    const refresh = liveCard()?.querySelector('[data-dp-aria2-live-refresh]');
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
        renderQueue(data);
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

  async function engineAction(gid, action, button) {
    setButtonPending(button, true, ACTION_LABELS[action] || 'Working…');
    try {
      await api('POST', `/aria2/downloads/${encodeURIComponent(gid)}/${action}`);
      toast(`aria2 ${action} sent`, 'success');
      await refreshQueue(false, true);
      if (typeof loadAria2Runtime === 'function') await loadAria2Runtime().catch(() => {});
      if (shouldRunLiveQueue()) schedulePoll(POLL_MS);
    } catch (error) {
      toast(`aria2 ${action}: ${error.message}`, 'error');
    } finally {
      setButtonPending(button, false);
    }
  }

  function startVisibleQueue() {
    if (!shouldRunLiveQueue()) {
      stopPolling();
      return;
    }
    void refreshQueue(false);
    schedulePoll(POLL_MS);
  }

  /* Bind a freshly rendered card once and prime its queue exactly once. The
     initial read does not depend on Settings/Downloads visibility timing;
     visibility only controls the continuing poll loop. */
  function attachCard(card) {
    if (primedCard === card) return;
    primedCard = card;
    card.querySelector('[data-dp-aria2-live-refresh]')?.addEventListener('click', () => void refreshQueue(true));
    card.addEventListener('click', event => {
      const filter = event.target.closest('[data-engine-filter]');
      if (filter && card.contains(filter)) {
        const next = String(filter.dataset.engineFilter || 'all');
        if (FILTERS.includes(next)) {
          activeFilter = next;
          applyFilter();
        }
        return;
      }
      const action = event.target.closest('[data-aria2-action]');
      if (action && card.contains(action)) void engineAction(action.dataset.gid, action.dataset.aria2Action, action);
    });
    updateFilterSelection();
    void refreshQueue(false, true);
  }

  function syncCard() {
    const card = liveCard();
    if (!card) return;
    attachCard(card);
    startVisibleQueue();
  }

  function attach() {
    const view = root();
    if (!view) return;
    if (view.dataset.dpSettingsAria2LiveBound !== '1') {
      view.dataset.dpSettingsAria2LiveBound = '1';
      view.addEventListener('click', event => {
        if (event.target.closest('.dp-settings-tabs [data-tab]')) queueMicrotask(startVisibleQueue);
      });
    }
    syncCard();
  }

  document.addEventListener('debridpulse:settings-rendered', attach);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopPolling();
    else startVisibleQueue();
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', attach, {once: true});
  else attach();
})();
