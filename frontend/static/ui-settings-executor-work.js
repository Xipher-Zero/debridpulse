/* Executor Work (Settings → Downloads).
 *
 * The operational view of what DebridPulse's executors are doing with the work
 * DebridPulse gave them, for the moment something is not converging -- and the
 * legal DebridPulse-owned controls for pausing, resuming or terminating it.
 *
 * This renderer knows about NO executor. It reads one neutral projection
 * (`/executor-work`) whose rows are DebridPulse facts -- a durable attempt id,
 * a neutral execution state, progress, an executor display identity and the
 * controls that are legal right now -- and it renders exactly those. It never
 * sees a GID, an NZO id, a native status name or a native action URL, so an
 * executor registered tomorrow appears here by existing, with no change to this
 * file.
 *
 * Actions post the generic action for a durable attempt id. The backend
 * resolves current ownership, re-takes the observation, verifies the action is
 * still legal and dispatches the canonical DebridPulse command. Nothing here
 * decides legality and nothing here may assume the row it last drew is still
 * true.
 *
 * The card's structure is part of the Settings markup (ui-settings-page.js);
 * this module owns what is inside it: the bounded poll, the rows, the filter
 * selection, the live metrics and the actions.
 */
(function () {
  'use strict';

  const FILTERS = Object.freeze(['all', 'active', 'waiting', 'paused', 'stopped']);
  // Ordering is by the NEUTRAL group, so what an operator is looking for --
  // work that is running, then work that is waiting -- is what they see first.
  const GROUP_WEIGHT = Object.freeze({active: 0, waiting: 1, paused: 2, stopped: 3});
  const STATE_LABELS = Object.freeze({
    queued: 'Queued', running: 'Running', paused: 'Paused', succeeded: 'Completed',
    failed: 'Failed', cancelled: 'Cancelled', absent: 'Absent', unknown: 'Unknown',
  });
  const STATE_BADGE = Object.freeze({
    queued: 'queued', running: 'downloading', paused: 'paused', succeeded: 'completed',
    failed: 'error', cancelled: 'error', absent: 'queued', unknown: 'queued',
  });
  const ACTION_LABELS = Object.freeze({pause: 'Pause', resume: 'Resume', cancel: 'Terminate'});
  const ACTION_PENDING = Object.freeze({pause: 'Pausing…', resume: 'Resuming…', cancel: 'Terminating…'});
  const POLL_MS = 5000;
  const REQUEST_TIMEOUT_MS = 20000;

  let pollTimer = null;
  let refreshRunning = null;
  let activeFilter = 'all';
  let boundCard = null;

  const root = () => document.getElementById('view-settings');
  const downloadsPanel = () => root()?.querySelector('[data-panel="downloads"]') || null;
  const workCard = () => root()?.querySelector('[data-dp-executor-work-card="1"]') || null;
  const listNode = () => workCard()?.querySelector('[data-dp-executor-work-list="1"]') || null;

  const settingsVisible = () => !!root()?.classList.contains('active');
  const downloadsVisible = () => {
    const panel = downloadsPanel();
    return !!panel && !panel.hidden;
  };
  /* Bounded polling: only while this surface is the one being looked at. */
  const shouldPoll = () => settingsVisible() && downloadsVisible() && !document.hidden;

  const esc = value => String(value ?? '').replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);

  const size = value => (typeof fmtSize === 'function' ? fmtSize(Math.max(0, Number(value) || 0))
    : `${Math.max(0, Number(value) || 0)} B`);
  const speed = value => (typeof fmtSpeed === 'function' ? fmtSpeed(Math.max(0, Number(value) || 0))
    : `${Math.max(0, Number(value) || 0)} B/s`);

  function ordered(data) {
    const items = Array.isArray(data?.items) ? data.items.slice() : [];
    return items.sort((a, b) =>
      (GROUP_WEIGHT[a?.filter_group] ?? 9) - (GROUP_WEIGHT[b?.filter_group] ?? 9)
      || String(a?.executor_name || '').localeCompare(String(b?.executor_name || ''))
      || String(a?.name || '').localeCompare(String(b?.name || '')));
  }

  /* An unknown value is shown AS unknown. Nothing here invents a total, a
   * remaining figure or a rate that the projection did not state. */
  const unknown = '—';

  function stateBadge(state) {
    const key = String(state || 'unknown');
    return `<span class="badge badge-${esc(STATE_BADGE[key] || 'queued')}">${esc(STATE_LABELS[key] || key)}</span>`;
  }

  function actionButton(row, action) {
    const danger = action === 'cancel';
    return `<button class="btn ${danger ? 'btn-danger' : 'btn-ghost'} btn-sm"
      data-executor-action="${esc(action)}" data-attempt="${esc(row.attempt_id)}"
      data-subject="${esc(row.name || '')}">${esc(ACTION_LABELS[action] || action)}</button>`;
  }

  function rowMarkup(row) {
    const controls = Array.isArray(row.controls) ? row.controls : [];
    const error = row.error
      ? `<div class="dp-executor-work-error">${esc(row.error.message || row.error.category || 'Execution error')}</div>`
      : '';
    return `
      <div class="dp-executor-work-item" data-executor-group="${esc(row.filter_group || 'waiting')}">
        <div class="dp-executor-work-item-top">
          <div class="dp-executor-work-identity">
            <div class="dp-executor-work-name" title="${esc(row.name || '')}">${esc(row.name || 'Execution')}</div>
            <div class="dp-executor-work-owner">${esc(row.executor_name || row.executor_id || '')}</div>
          </div>
          <div class="dp-executor-work-actions">${controls.map(action => actionButton(row, action)).join('')}</div>
        </div>
        <div>${typeof progress === 'function'
          ? progress(Number(row.progress) || 0, row.filter_group === 'stopped' ? 'completed' : 'downloading')
          : ''}</div>
        <div class="dp-executor-work-facts">
          <div><div class="dp-executor-work-k">State</div><div class="dp-executor-work-v">${stateBadge(row.state)}</div></div>
          <div><div class="dp-executor-work-k">Speed</div><div class="dp-executor-work-v">${
            row.bytes_per_second === null || row.bytes_per_second === undefined ? unknown : esc(speed(row.bytes_per_second))}</div></div>
          <div><div class="dp-executor-work-k">Done</div><div class="dp-executor-work-v">${
            esc(size(row.completed_bytes))}${row.total_bytes ? ` / ${esc(size(row.total_bytes))}` : ''}</div></div>
          <div><div class="dp-executor-work-k">Remaining</div><div class="dp-executor-work-v">${
            row.remaining_bytes === null || row.remaining_bytes === undefined ? unknown : esc(size(row.remaining_bytes))}</div></div>
        </div>
        ${error}
      </div>`;
  }

  function updateMetrics(data) {
    const card = workCard();
    if (!card) return;
    const summary = data?.summary || {};
    const speedNode = card.querySelector('[data-dp-executor-work-speed]');
    const remainingNode = card.querySelector('[data-dp-executor-work-remaining]');
    if (speedNode) speedNode.textContent = speed(summary.download_speed || 0);
    if (remainingNode) {
      // A remaining total exists only when EVERY execution knows its own size.
      const remaining = summary.remaining_bytes;
      remainingNode.textContent = `${remaining === null || remaining === undefined ? unknown : size(remaining)} Remaining`;
    }
  }

  function updateFilterSelection() {
    workCard()?.querySelectorAll('[data-executor-filter]').forEach(button => {
      const selected = String(button.dataset.executorFilter || '') === activeFilter;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-selected', selected ? 'true' : 'false');
    });
  }

  /* Filtering is over the NEUTRAL group the projection stated. */
  function applyFilter() {
    const list = listNode();
    if (!list) return;
    updateFilterSelection();
    const items = Array.from(list.querySelectorAll('.dp-executor-work-item'));
    let visible = 0;
    items.forEach(item => {
      const show = activeFilter === 'all' || item.dataset.executorGroup === activeFilter;
      item.hidden = !show;
      if (show) visible += 1;
    });

    let empty = list.querySelector('[data-dp-executor-work-filter-empty]');
    if (!items.length || visible > 0 || activeFilter === 'all') {
      empty?.remove();
      return;
    }
    if (!empty) {
      empty = document.createElement('div');
      empty.className = 'empty dp-executor-work-filter-empty';
      empty.dataset.dpExecutorWorkFilterEmpty = '1';
      list.appendChild(empty);
    }
    empty.textContent = `No ${activeFilter} download engine activity right now.`;
  }

  function render(data) {
    const list = listNode();
    if (!list) return;
    const items = ordered(data);
    list.innerHTML = items.length ? items.map(rowMarkup).join('')
      : '<div class="empty">No download engine activity right now.</div>';
    updateMetrics(data);
    applyFilter();
  }

  function showError(message) {
    const list = listNode();
    if (!list) return;
    const node = document.createElement('div');
    node.className = 'dp-executor-work-error';
    node.textContent = `Download engine activity unavailable: ${String(message || 'unknown error')}`;
    list.replaceChildren(node);
    updateMetrics(null);
  }

  function stopPolling() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
  }

  function schedulePoll(delay = POLL_MS) {
    stopPolling();
    if (!shouldPoll()) return;
    pollTimer = setTimeout(async () => {
      pollTimer = null;
      await refresh(false);
      if (shouldPoll()) schedulePoll(POLL_MS);
    }, Math.max(0, delay));
  }

  async function refresh(manual, force = false) {
    if (!manual && !force && !shouldPoll()) return null;
    if (refreshRunning) return refreshRunning;
    if (typeof api !== 'function') {
      showError('Application API client is unavailable');
      return null;
    }
    const button = workCard()?.querySelector('[data-dp-executor-work-refresh]');
    refreshRunning = (async () => {
      if (manual && button) {
        button.disabled = true;
        button.textContent = 'Refreshing…';
      }
      try {
        const data = await api('GET', '/executor-work', null, REQUEST_TIMEOUT_MS);
        render(data);
        return data;
      } catch (error) {
        showError(error?.message || error);
        return null;
      } finally {
        if (manual && button) {
          button.disabled = false;
          button.textContent = 'Refresh';
        }
        refreshRunning = null;
      }
    })();
    return refreshRunning;
  }

  /* Terminating an execution is an operational DESTRUCTIVE act, so it is asked
   * by the ONE canonical Settings confirmation -- the same dialog every other
   * destructive Settings action uses. There is no second modal and no
   * browser-native confirm here. */
  async function confirmTermination(subject) {
    if (!window.DPSettingsModal || typeof window.DPSettingsModal.confirm !== 'function') return false;
    return window.DPSettingsModal.confirm({
      tone: 'danger',
      title: 'Terminate this executor work?',
      message: `${subject ? `"${subject}" ` : ''}will be stopped and its execution released. `
        + 'DebridPulse keeps the transfer and may start it again under its normal recovery rules.',
      confirmLabel: 'Terminate',
    });
  }

  async function perform(button) {
    const action = String(button.dataset.executorAction || '');
    const attempt = String(button.dataset.attempt || '');
    if (!action || !attempt) return;
    if (action === 'cancel' && !await confirmTermination(button.dataset.subject)) return;

    setButtonPending(button, true, ACTION_PENDING[action] || 'Working…');
    try {
      await api('POST', `/executor-work/${encodeURIComponent(attempt)}/${encodeURIComponent(action)}`,
                null, REQUEST_TIMEOUT_MS);
      toast(`Executor work: ${ACTION_LABELS[action] || action} accepted`, 'success');
      await refresh(false, true);
      if (shouldPoll()) schedulePoll(POLL_MS);
    } catch (error) {
      toast(`Executor work: ${error.message}`, 'error');
      // Whatever the row showed is no longer trustworthy; re-read the truth.
      await refresh(false, true);
    } finally {
      setButtonPending(button, false);
    }
  }

  function startVisible() {
    if (!shouldPoll()) {
      stopPolling();
      return;
    }
    void refresh(false);
    schedulePoll(POLL_MS);
  }

  function attachCard(card) {
    if (boundCard === card) return;
    boundCard = card;
    card.querySelector('[data-dp-executor-work-refresh]')?.addEventListener('click', () => void refresh(true));
    card.addEventListener('click', event => {
      const filter = event.target.closest('[data-executor-filter]');
      if (filter && card.contains(filter)) {
        const next = String(filter.dataset.executorFilter || 'all');
        if (FILTERS.includes(next)) {
          activeFilter = next;
          applyFilter();
        }
        return;
      }
      const action = event.target.closest('[data-executor-action]');
      if (action && card.contains(action)) void perform(action);
    });
    updateFilterSelection();
    void refresh(false, true);
  }

  function attach() {
    const view = root();
    if (!view) return;
    if (view.dataset.dpExecutorWorkBound !== '1') {
      view.dataset.dpExecutorWorkBound = '1';
      view.addEventListener('click', event => {
        if (event.target.closest('.dp-settings-tabs [data-tab]')) queueMicrotask(startVisible);
      });
    }
    const card = workCard();
    if (!card) return;
    attachCard(card);
    startVisible();
  }

  document.addEventListener('debridpulse:settings-rendered', attach);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopPolling();
    else startVisible();
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', attach, {once: true});
  else attach();
})();
