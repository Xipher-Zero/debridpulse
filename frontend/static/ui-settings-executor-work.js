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
 * this module owns what is inside it: the live re-reads, the rows, the filter
 * selection, the live metrics and the actions.
 *
 * Live while shown, to an Admin: the application's one stream says WHEN the
 * executions changed (debridpulse:execution-activity-changed, bridged by
 * app.js from its transfer and statistics signals, carrying nothing) and keeps
 * time (debridpulse:pulse-heartbeat); what is shown is only ever an accepted
 * GET /executor-work answer, the Admin-only projection the server authorizes
 * on every request. Nothing here is fetched unless this card is on screen to
 * an Admin. At most one request is in flight; re-reads are spaced (measured,
 * below), a signal that arrives meanwhile is one trailing re-read once that
 * request settles, and a reconnected stream re-reads once. An answer that
 * belongs to an earlier arrival on the card is never shown.
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
  const REQUEST_TIMEOUT_MS = 20000;
  // Re-read admission. GET /executor-work observes every owned execution again
  // through its executor (one batch per executor): measured at 12 ms for 2
  // executions, 55 ms for 10, 138 ms for 25 and 280 ms for 50 in DebridPulse's
  // own work (in-memory executors; a real executor's answer adds its own
  // time), while execution activity is signalled up to every 0.5 s. So, as
  // Statistics does: re-reads start at least 2 s apart, or five times the last
  // answer's own time when that is longer (a slow executor spaces itself),
  // never more than 10 s apart.
  const REFRESH_SPACING_MS = 2000, REFRESH_SPACING_MAX_MS = 10000, LATENCY_SPACING_FACTOR = 5;
  // A heartbeat re-reads only when the shown answer is at least this old: a
  // stalled execution signals nothing while its rate decays.
  const AGING_MS = 25000;

  let activeFilter = 'all';
  let boundCard = null;
  let shownNow = false;               // the card was on screen to an Admin at the last check
  let generation = 0;                 // the arrival on the card an answer must belong to
  let inflight = false, queued = null, timer = null;
  let lastStart = 0, lastAccepted = 0, spacing = REFRESH_SPACING_MS;
  let lastShown = '';                 // the accepted answer the rows show, as received

  const root = () => document.getElementById('view-settings');
  const downloadsPanel = () => root()?.querySelector('[data-panel="downloads"]') || null;
  const workCard = () => root()?.querySelector('[data-dp-executor-work-card="1"]') || null;
  const listNode = () => workCard()?.querySelector('[data-dp-executor-work-list="1"]') || null;

  const settingsVisible = () => !!root()?.classList.contains('active');
  const downloadsVisible = () => {
    const panel = downloadsPanel();
    return !!panel && !panel.hidden;
  };
  /* Executor work is the Admin's (the server answers no one else); and it is
   * read only while this card is the one being looked at. */
  const administers = () => typeof dpCan === 'function' && dpCan('administer');
  const shown = () => !!workCard() && administers() && settingsVisible() && downloadsVisible() && !document.hidden;

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
      <div class="dp-executor-work-item" data-attempt="${esc(row.attempt_id)}" data-executor-group="${esc(row.filter_group || 'waiting')}">
        <div class="dp-executor-work-item-top">
          <div class="dp-executor-work-identity">
            <div class="dp-executor-work-name" data-tooltip="${esc(row.name || '')}">${esc(row.name || 'Execution')}</div>
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
  const filtered = item => activeFilter !== 'all' && item.dataset.executorGroup !== activeFilter;

  function applyFilter() {
    const list = listNode();
    if (!list) return;
    updateFilterSelection();
    const items = Array.from(list.querySelectorAll('.dp-executor-work-item'));
    let visible = 0;
    items.forEach(item => {
      item.hidden = filtered(item);
      if (!item.hidden) visible += 1;
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

  /* Brings ``current`` to what ``next`` is, touching only what differs: a
   * node that already says the same is kept as it is -- with its focus, its
   * pending state and its hover -- so a live re-read redraws a row's changed
   * figures, not the row. Returns the node now in place. */
  function patch(current, next) {
    if (current.isEqualNode(next)) return current;
    const same = current.nodeType === Node.ELEMENT_NODE && current.nodeName === next.nodeName
      && current.attributes.length === next.attributes.length
      && Array.from(next.attributes).every(({name, value}) => current.getAttribute(name) === value)
      && current.childNodes.length === next.childNodes.length;
    if (!same) {
      current.replaceWith(next);
      return next;
    }
    Array.from(next.childNodes).forEach((child, index) => patch(current.childNodes[index], child));
    return current;
  }

  /* One accepted answer onto the list: rows are matched by their durable
   * attempt id, kept in the projection's order, patched where they changed,
   * added where new and removed where the projection no longer has them. An
   * answer identical to the one shown changes nothing at all. */
  function render(data) {
    const list = listNode();
    if (!list) return;
    updateMetrics(data);
    const received = JSON.stringify(data?.items || []);
    if (received === lastShown && list.querySelector('.dp-executor-work-item, .empty')) return;
    lastShown = received;
    const items = ordered(data);
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'No download engine activity right now.';
      list.replaceChildren(empty);
      applyFilter();
      return;
    }
    const template = document.createElement('template');
    template.innerHTML = items.map(rowMarkup).join('');
    const kept = new Map();
    Array.from(list.children).forEach(node => {
      if (node.dataset.attempt && !kept.has(node.dataset.attempt)) kept.set(node.dataset.attempt, node);
      else node.remove();
    });
    let before = list.firstElementChild;
    Array.from(template.content.children).forEach(row => {
      row.hidden = filtered(row);
      const node = kept.get(row.dataset.attempt);
      kept.delete(row.dataset.attempt);
      let placed = row;
      if (node) {
        if (node !== before) list.insertBefore(node, before);
        placed = patch(node, row);
      } else {
        list.insertBefore(row, before);
      }
      before = placed.nextElementSibling;
    });
    kept.forEach(node => node.remove());
    applyFilter();
  }

  function showError(message) {
    const list = listNode();
    if (!list) return;
    const node = document.createElement('div');
    node.className = 'dp-executor-work-error';
    node.textContent = `Download engine activity unavailable: ${String(message || 'unknown error')}`;
    list.replaceChildren(node);
    lastShown = '';
    updateMetrics(null);
  }

  // One request; its answer is shown only if it still belongs to the current
  // arrival on the card and the card is still shown.
  async function read() {
    const mine = generation;
    inflight = true;
    lastStart = Date.now();
    const started = performance.now();
    let data = null, failure = null;
    try {
      if (typeof api !== 'function') throw new Error('Application API client is unavailable');
      data = await api('GET', '/executor-work', null, REQUEST_TIMEOUT_MS);
    } catch (error) {
      failure = error;
    }
    inflight = false;
    spacing = Math.min(REFRESH_SPACING_MAX_MS,
      Math.max(REFRESH_SPACING_MS, LATENCY_SPACING_FACTOR * (performance.now() - started)));
    if (mine === generation && shown()) {
      if (data) {
        render(data);
        lastAccepted = Date.now();
      } else {
        showError(failure?.message || failure);
      }
    }
    if (queued) {
      const next = queued;
      queued = null;
      admit(next.explicit);
    }
  }

  /* Admit one re-read: never two in flight, signalled ones spaced, an
   * explicit one (a new card, an operator's action) as soon as nothing is in
   * flight. Anything asked for while one is in flight is ONE trailing re-read. */
  function admit(explicit) {
    if (!shown()) return;
    if (inflight) {
      queued = {explicit: !!(queued?.explicit || explicit)};
      return;
    }
    const wait = explicit ? 0 : lastStart + spacing - Date.now();
    if (wait > 0) {
      if (!timer) timer = setTimeout(() => { timer = null; admit(false); }, wait);
      return;
    }
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    void read();
  }

  // The card left the screen (or the Admin's view of it ended): nothing
  // already asked for may be shown, and nothing more is asked for.
  function retire() {
    generation += 1;
    queued = null;
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  }

  // Arriving on the card re-reads (spaced like any re-read); leaving retires.
  // Says whether it just arrived.
  function sync() {
    const now = shown();
    if (now === shownNow) return false;
    shownNow = now;
    if (now) admit(false);
    else retire();
    return now;
  }

  function invalidated() {
    if (!sync() && shownNow) admit(false);
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
      admit(true);
    } catch (error) {
      toast(`Executor work: ${error.message}`, 'error');
      // Whatever the row showed is no longer trustworthy; re-read the truth.
      admit(true);
    } finally {
      setButtonPending(button, false);
    }
  }

  function attachCard(card) {
    if (boundCard === card) return;
    boundCard = card;
    // A newly rendered card shows nothing yet: whatever was asked for the one
    // it replaced is not shown, and it is read as soon as nothing is in flight.
    retire();
    lastShown = '';
    shownNow = shown();
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
    admit(true);
  }

  function attach() {
    const card = workCard();
    if (!card) return;
    attachCard(card);
    sync();
  }

  // A Settings section chosen by pointer or keyboard: read once the Settings
  // owner has shown it. Listened for on the document, which hears the event
  // after the Settings view's own handler has run.
  for (const type of ['click', 'keydown']) {
    document.addEventListener(type, event => {
      if (event.target?.closest?.('#view-settings')) queueMicrotask(sync);
    });
  }
  document.addEventListener('debridpulse:settings-rendered', attach);
  document.addEventListener('debridpulse:navigation', () => queueMicrotask(sync));
  document.addEventListener('visibilitychange', sync);
  document.addEventListener('debridpulse:execution-activity-changed', invalidated);
  // A reconnected stream may have missed changes: re-read once.
  document.addEventListener('debridpulse:pulse-connected', invalidated);
  document.addEventListener('debridpulse:pulse-heartbeat', () => {
    if (Date.now() - lastAccepted >= AGING_MS) invalidated();
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', attach, {once: true});
  else attach();
})();
