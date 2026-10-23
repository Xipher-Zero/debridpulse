/* DebridPulse — self-hosted multi-provider transfer manager. */

const API = '/api';
let settingsData = {};

// settingsData is the cached GET /settings document. The scheduler's
// concurrency and the download bandwidth cap are each read from their one
// canonical namespace; no flat alias is read, mirrored or written.
// Per-transfer pauses are counted by the backend (GET /stats by_status.paused);
// this is only the last value read from there and is never adjusted locally.
let pausedTransferCount = 0;
// Global processing pause is operational state owned by the backend's durable
// application state -- not a setting. This is a non-persisted projection of it,
// assigned only from server responses (GET /stats and the pause/resume results)
// and never written to, or read from, settingsData.
let processingPaused = false;
async function refreshProviderStatus() {
  if (!window.DPProviderStatus?.refresh) return null;
  return window.DPProviderStatus.refresh();
}

// The caller's authority, from its one owner (auth.js). Every control that a
// role may lack is built only when this says so; the server decides anyway.
function dpCan(capability) {
  return !!(window.debridPulseAuth && window.debridPulseAuth.can(capability));
}

// The installation's display timezone, for every role (Admin's settings carry
// the same value).
function dpDisplayTimezone() {
  return String((settingsData && settingsData.timezone)
    || (window.debridPulseAuth && window.debridPulseAuth.authority.display_timezone) || '').trim() || 'UTC';
}

function renderTopbarActions() {
  const el = document.getElementById('topbar-actions');
  if (!el) return;

  // Create these controls once. Replacing their DOM nodes during live refreshes
  // can swallow pointer-up/click events when an SSE update lands mid-click.
  if (el.dataset.initialized !== '1') {
    const icon = (name) => window.DPIcons && typeof window.DPIcons.svg === 'function' ? window.DPIcons.svg(name) : '';
    el.innerHTML = `
      <button id="btn-resume-all" class="btn btn-primary" data-default-label="Resume All" onclick="resumeProcessing()" style="display:none">${icon('play')}<span>Resume All</span></button>
      <button id="btn-resume-paused" class="btn btn-primary" data-default-label="Resume Paused" onclick="resumePausedDownloads()" style="display:none">${icon('play')}<span>Resume Paused</span></button>
      <button id="btn-pause-all" class="btn btn-ghost" data-default-label="Pause All" onclick="pauseProcessing()">${icon('pause')}<span>Pause All</span></button>
    `;
    el.dataset.initialized = '1';
  }

  const globallyPaused = processingPaused;
  const selectivelyPaused = Math.max(0, Number(pausedTransferCount) || 0);
  const pauseBtn = document.getElementById('btn-pause-all');
  const resumeAllBtn = document.getElementById('btn-resume-all');
  const resumePausedBtn = document.getElementById('btn-resume-paused');

  if (pauseBtn) {
    pauseBtn.style.display = globallyPaused ? 'none' : '';
    pauseBtn.dataset.defaultLabel = 'Pause All';
  }

  if (resumeAllBtn) {
    resumeAllBtn.style.display = globallyPaused ? '' : 'none';
    resumeAllBtn.dataset.defaultLabel = 'Resume All';
  }

  if (resumePausedBtn) {
    resumePausedBtn.style.display =
      !globallyPaused && selectivelyPaused > 0 ? '' : 'none';

    const label = `Resume Paused (${selectivelyPaused})`;
    resumePausedBtn.dataset.defaultLabel = label;

    if (resumePausedBtn.dataset.pending !== '1') {
      const copy = resumePausedBtn.querySelector('span:last-child');
      if (copy) copy.textContent = label;
      else resumePausedBtn.textContent = label;
    }
  }

  window.DPProcessingPresentation?.syncPauseUi?.();
  updateRuntimeStatusBadge({});
}

// ── Nav ────────────────────────────────────────────────────────────────────
/* A sidebar entry chosen by the operator (a click or its keyboard activation)
 * moves the main view region up or down toward it, by its place among the
 * entries this session actually shows; every other call -- start-up, a
 * redirect, a reset, a test -- changes the view in place. The view itself is
 * changed by navigateTo(), the same commit either way, so the destination's
 * entry work (loading, its first page or tab) is exactly as it was. */
function nav(el, event) {
  if (!el) return;
  const entries = [...document.querySelectorAll('#sidebar .nav-item[data-view]')]
    .filter(item => item.getClientRects().length);
  const from = entries.findIndex(item => item.classList.contains('active'));
  const to = entries.indexOf(el);
  const sameView = !!document.getElementById('view-' + el.dataset.view)?.classList.contains('active');
  const chosen = event?.type === 'click' && from >= 0 && to >= 0 && !sameView;
  const motion = window.DPMotion
    ? window.DPMotion.slide({element: document.getElementById('content'), axis: 'y', direction: chosen ? Math.sign(to - from) : 0,
                             distance: Math.abs(to - from)}, () => navigateTo(el))
    : (navigateTo(el), {settled: Promise.resolve()});
  // Whatever measures the destination's layout may do so again once it is still.
  motion.settled.then(() => document.dispatchEvent(new CustomEvent('debridpulse:navigation-settled',
    {detail: {view: document.querySelector('.view.active')?.id?.replace(/^view-/, '') || el.dataset.view}})));
}

function navigateTo(el) {
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  el.classList.add('active');
  const v = el.dataset.view;
  document.querySelectorAll('.view').forEach(x => x.classList.remove('active'));
  const activeView = document.getElementById('view-' + v);
  if (!activeView) { console.error('nav: view not found:', v); return; }
  activeView.classList.add('active');
  const content = document.getElementById('content');
  if (content) {
    content.classList.toggle('dashboard-active', v === 'dashboard');
    content.classList.toggle('settings-active', v === 'settings');
    content.scrollTop = 0;
  }

  const titles = {
    dashboard:'Dashboard', torrents:'Downloads', events:'Activity Log',
    stats:'Statistics', settings:'Settings', help:'Help & License',
  };
  const subtitles = {
    dashboard:'Overview of your download activities and system status.',
    torrents:'Inspect, filter, and control queued and active transfers.',
    events:'Recent transfer activity, decisions, warnings, and errors.',
    stats:'Historical transfer performance and completion metrics.',
    settings:'Configure providers, downloads, notifications, and system behavior.',
    help:'Usage guidance, project information, and licensing.',
  };
  // A sidebar entry may name the page it opens (a User's Settings slot opens
  // their own Notifications).
  const title = el.dataset.title || titles[v] || v;
  document.getElementById('page-title').textContent = title;
  const subtitle = document.getElementById('page-subtitle');
  if (subtitle) subtitle.textContent = el.dataset.subtitle || subtitles[v] || '';
  document.dispatchEvent(new CustomEvent('debridpulse:navigation', {detail:{view:v,title}}));
  syncDashboardSparkPulses();
  if (v === 'dashboard') { loadStats(); loadRecent(); }
  // Every sidebar selection is a new entry: Downloads opens on page 1 (its
  // search, filter and order are kept), Settings on Services, Help on Quick Start.
  if (v === 'torrents')  { clearSelection(); goToTorrentPage(1); }
  if (v === 'events')    window.DPActivityLog.load();
  if (v === 'stats')     loadDetailedStats();
  if (v === 'settings')  loadSettings();
  if (v === 'help')      loadHelp();
  closeSidebar();
}

// ── API ────────────────────────────────────────────────────────────────────
/* The per-transfer private-LAN confirmation, through the canonical dialog
 * owner. Answers only for the submission that asked; it writes no setting. */
async function confirmLocalNetwork(hosts) {
  if (!window.DPSettingsModal || typeof window.DPSettingsModal.confirm !== 'function') return false;
  const named = (Array.isArray(hosts) ? hosts : []).filter(Boolean).join(', ');
  return window.DPSettingsModal.confirm({
    title: 'Connect to a local network address?',
    message: 'This transfer connects to an address on your private network'
      + (named ? ' (' + named + ')' : '') + '. Only continue if you trust the source and intended this '
      + 'connection. Allowing applies to this transfer only; cancelling changes no settings.',
    confirmLabel: 'Allow',
    cancelLabel: 'Cancel',
  });
}

/* One link names an item inside an enclosing collection: the operator says
 * which to acquire, through the canonical dialog owner. The choices and their
 * labels are the claimant's own (``acquisition_scope`` refusal) and carry equal,
 * neutral weight. The dialog is blocking: the backdrop and Escape do nothing,
 * and Cancel Transfer and X choose nothing -- this link is simply not added.
 * Resolves to the chosen scope, or '' when cancelled. */
async function chooseAcquisitionScope(link, choices) {
  if (!window.DPSettingsModal || typeof window.DPSettingsModal.open !== 'function') return '';
  const offered = (Array.isArray(choices) ? choices : [])
    .filter(choice => choice && (choice.scope === 'item' || choice.scope === 'collection'));
  if (offered.length !== 2) return '';
  let chosen = '';
  const fallback = {item: 'This Item', collection: 'Collection'};
  const handle = window.DPSettingsModal.open({
    title: 'Download the item or its collection?',
    dismiss: true,
    blocking: true,
    closeControl: true,
    cancelLabel: 'Cancel Transfer',
    cancelTone: 'danger',
    actions: offered.map(choice => ({id: choice.scope, label: String(choice.label || fallback[choice.scope])})),
    mount(body) {
      const message = document.createElement('p');
      message.className = 'dp-modal-message dp-modal-message--center';
      message.textContent = 'This link points to one item inside a collection. Choose what to download; '
        + 'the choice applies to this link only.';
      const address = document.createElement('code');
      address.className = 'dp-modal-message dp-modal-code';
      address.textContent = String(link || '');
      body.append(message, address);
      return message;
    },
    onAction(id) {
      chosen = String(id);
      handle.close();
    },
  });
  await handle.closed;
  return chosen;
}

/* The application action "open this address outside DebridPulse".
 * DebridPulse's own window is never navigated, framed or replaced: the address
 * opens in the operator's own browser. It is one function so a desktop host
 * that embeds DebridPulse can hand the address to the operating system's
 * default browser instead. Only an https address is ever opened. */
function openExternalUrl(url) {
  let target;
  try {
    target = new URL(String(url || ''));
  } catch (_) {
    return false;
  }
  if (target.protocol !== 'https:') return false;
  window.open(target.href, '_blank', 'noopener,noreferrer');
  return true;
}

async function api(method, path, body, timeoutMs, options) {
  const isFormData = typeof FormData !== 'undefined' && body instanceof FormData;
  // A file sent as itself: the raw body, typed by the file.
  const isFile = !isFormData && typeof Blob !== 'undefined' && body instanceof Blob;
  const opts = {
    method,
    headers: isFormData ? {} : {'Content-Type': isFile ? (body.type || 'application/octet-stream') : 'application/json'}
  };
  if (body) opts.body = isFormData || isFile ? body : JSON.stringify(body);
  const ms = timeoutMs || 8000; // default 8s; callers can pass longer for slow operations
  const controller = new AbortController();
  let timedOut = false;
  const tid = setTimeout(() => { timedOut = true; controller.abort(); }, ms);
  const externalSignal = options && options.signal;
  const abortFromExternal = () => controller.abort();
  if (externalSignal) {
    if (externalSignal.aborted) controller.abort();
    else externalSignal.addEventListener('abort', abortFromExternal, {once:true});
  }
  opts.signal = controller.signal;
  try {
    const r = await window.debridPulseAuth.fetch(API + path, opts);
    clearTimeout(tid);
    if (externalSignal) externalSignal.removeEventListener('abort', abortFromExternal);
    const data = await r.json().catch(() => ({detail: r.statusText}));
    if (!r.ok) {
      // A structured refusal keeps its facts for the caller that asked.
      const detail = data.detail;
      // A request-body refusal is a list of refused values: the first one speaks.
      const first = Array.isArray(detail) ? detail[0] : detail;
      const error = new Error((first && typeof first === 'object' ? first.message || first.msg : first) || r.statusText);
      error.detail = detail;
      error.status = r.status;
      throw error;
    }
    return data;
  } catch(e) {
    clearTimeout(tid);
    if (externalSignal) externalSignal.removeEventListener('abort', abortFromExternal);
    if (e.name === 'AbortError' && timedOut) throw new Error('Request timed out after ' + Math.round(ms/1000) + 's');
    throw e;
  }
}

// Shared row-navigation ownership boundary for Downloads and Dashboard Recent:
// a click that originates from an interactive child control (a button, the
// common-source group launcher, etc.) must perform only that control's own
// action, never also trigger the row's own Details navigation. Fixes the
// Dashboard Recent group-launcher click-ownership bug (DP 1.0.12) by giving
// Recent the same interactive-descendant guard Downloads' row click already
// used; do not stack additional stopPropagation() calls onto individual
// controls instead of this one shared boundary.
function dpIsInteractiveRowTarget(target) {
  return !!(target && target.closest &&
    target.closest('button,input,a,select,textarea,label,[role="button"],[data-dp-group-candidates-trigger]'));
}
window.dpIsInteractiveRowTarget = dpIsInteractiveRowTarget;

// The one application context-menu policy (a trial, deliberately small): the
// browser keeps its own menu wherever it does real work -- an editable text
// field (Cut, Copy, Paste, spelling), a hyperlink (Open, Copy Link) or text the
// operator selected where they right-clicked (Copy) -- and is otherwise
// suppressed on DebridPulse's own surfaces, where the owner of a surface may
// offer its own through the one menu below: Downloads' unfinished rows
// (DPDownloads.contextMenu), their selection checkboxes included, and Details'
// Original Resource when Copy URL is allowed. No other module listens for
// contextmenu.
const DP_EDITABLE_FIELD = 'textarea,input:not([type="checkbox"],[type="radio"],[type="button"],[type="submit"],'
  + '[type="reset"],[type="range"],[type="color"],[type="file"],[type="image"],[type="hidden"])';
function dpKeepsNativeContextMenu(target) {
  if (!(target instanceof Element)) return true;
  if (target.closest(`${DP_EDITABLE_FIELD},a[href]`) || target.isContentEditable) return true;
  const selection = window.getSelection?.();
  if (!selection || selection.isCollapsed || !selection.toString().trim()) return false;
  for (let index = 0; index < selection.rangeCount; index++) {
    if (selection.getRangeAt(index).intersectsNode(target)) return true;
  }
  return false;
}
document.addEventListener('contextmenu', event => {
  if (dpKeepsNativeContextMenu(event.target)) return;
  event.preventDefault();
  dpCloseContextMenu(false);
  if (dpOriginalResourceMenu(event)) return;
  window.DPDownloads?.contextMenu?.(event);
});

// The one application menu: one at a time, at the pointer, or at its anchor
// when the keyboard opened it, always inside the viewport, and inside the
// dialog that holds its anchor (above that dialog, below any over it) or else
// above the page. Escape, Tab, a pointer outside it, scrolling, resizing,
// navigation and closing Details close it; Escape, Tab and a chosen item
// return focus through ``restore``.
let dpContextMenu = null;
function dpCloseContextMenu(restoreFocus) {
  if (!dpContextMenu) return;
  const {node, abort, restore} = dpContextMenu;
  dpContextMenu = null;
  abort.abort();
  node.remove();
  if (restoreFocus) restore?.();
}
function dpOpenContextMenu(event, anchor, {label, items, restore}) {
  dpCloseContextMenu(false);
  const node = document.createElement('div');
  node.className = 'dp-row-menu';
  node.setAttribute('role', 'menu');
  node.setAttribute('aria-label', label);
  const buttons = items.map(({text}) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.setAttribute('role', 'menuitem');
    item.textContent = text;
    node.appendChild(item);
    return item;
  });
  (anchor.closest('#overlay') || document.body).appendChild(node);
  const rect = anchor.getBoundingClientRect();
  const atPointer = event.clientX >= rect.left && event.clientX <= rect.right
    && event.clientY >= rect.top && event.clientY <= rect.bottom;
  const x = atPointer ? event.clientX : rect.left + 16;
  const y = atPointer ? event.clientY : rect.bottom;
  const size = node.getBoundingClientRect();
  node.style.left = `${Math.max(4, Math.min(x, window.innerWidth - size.width - 4))}px`;
  node.style.top = `${Math.max(4, Math.min(y, window.innerHeight - size.height - 4))}px`;
  const abort = new AbortController();
  const signal = abort.signal;
  dpContextMenu = {node, abort, restore};
  buttons.forEach((item, index) => item.addEventListener('click', () => {
    dpCloseContextMenu(true);
    items[index].run();
  }, {signal}));
  node.addEventListener('keydown', e => {
    if (e.key === 'Escape' || e.key === 'Tab') { e.preventDefault(); e.stopPropagation(); dpCloseContextMenu(true); }
  }, {signal});
  document.addEventListener('pointerdown', e => { if (!node.contains(e.target)) dpCloseContextMenu(false); }, {signal, capture: true});
  window.addEventListener('scroll', () => dpCloseContextMenu(false), {signal, capture: true});
  window.addEventListener('resize', () => dpCloseContextMenu(false), {signal});
  document.addEventListener('debridpulse:navigation', () => dpCloseContextMenu(false), {signal});
  document.addEventListener('debridpulse:detail-closed', () => dpCloseContextMenu(false), {signal});
  buttons[0].focus({preventScroll: true});
  return true;
}

// ── Toast ──────────────────────────────────────────────────────────────────
function esc(s) {
  // Escape HTML special chars to prevent XSS when inserting user-controlled
  // content (torrent names, filenames, labels) into innerHTML.
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function escapeHtmlStrings(value) {
  // Settings and other API payloads are plain data. Escape string leaves
  // before interpolating those payloads into HTML templates; numbers and
  // booleans retain their native types for control-flow and form logic.
  if (Array.isArray(value)) return value.map(escapeHtmlStrings);
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, escapeHtmlStrings(item)])
    );
  }
  return typeof value === 'string' ? esc(value) : value;
}

// What the operator submitted, as two separate facts.
//
// `source` is the submission CHANNEL -- how the work was handed over. The
// canonical request KIND is what was actually submitted, and it lives in the
// durable root requests, which both the Details projection and the bounded
// Downloads projection surface as `request_kinds`.
//
// Before NZB, an uploaded file was always a torrent, so the channel
// `manual_file` could stand in for the kind. It cannot any more: reading the
// channel alone reported a completed NZB transfer as a torrent file. Nothing
// here consults provider identity -- a Usenet-resolved torrent is still a
// torrent, and an AllDebrid-resolved posting is still an NZB.
const UPLOADED_FILE_LABELS = Object.freeze({
  torrent: 'Torrent file',
  torrent_file: 'Torrent file',
  file: 'Torrent file',
  nzb: 'NZB file',
  meta4: 'Metalink file',
});

function sourceLabel(source, requestKinds) {
  const labels = {
    direct_link: 'Direct link',
    manual: 'Magnet link',
    alldebrid_existing: 'Provider inventory',
    import_existing: 'Provider inventory',
    inventory: 'Provider inventory',
    api: 'API'
  };
  const key = String(source || '').trim();
  if (key === 'manual_file') {
    const kinds = Array.isArray(requestKinds) ? requestKinds : [];
    // One unambiguous kind names itself; anything mixed or unrecognised falls
    // back to the truthful channel, never to a guess.
    if (kinds.length === 1 && UPLOADED_FILE_LABELS[kinds[0]]) return UPLOADED_FILE_LABELS[kinds[0]];
    return 'Uploaded file';
  }
  return labels[key] || esc(key) || '—';
}

function transferProviderPresentation(t) {
  // A torrent root's provider is its committed ROOT route (the backend's
  // ``route_provider_id``, present only for one-root torrent transfers): a
  // live route-state indicator, never an origin or a child file's provider.
  if (t && Object.prototype.hasOwnProperty.call(t, 'route_provider_id')) {
    return t.route_provider_id
      ? {label: String(t.route_provider_name || t.route_provider_id), state: 'known', id: t.route_provider_id}
      : {label: 'Pending', state: 'pending'};
  }
  // Every other transfer names the backend's ACTIVE provider projection
  // (``active_provider_id``): the provider executing its work, or the last
  // one that did once nothing is live -- execution authority, never an
  // origin, a delivery majority or a route ordinal. Nothing here derives a
  // provider; an ambiguous projection is Unknown, an absent one Pending
  // until the transfer completed.
  if (t?.active_provider_id) {
    return {label: String(t.active_provider_name || t.active_provider_id), state: 'known', id: t.active_provider_id};
  }
  if (t?.active_provider_basis === 'ambiguous' || String(t?.status || '') === 'completed') {
    return {label: 'Unknown', state: 'unknown'};
  }
  return {label: 'Pending', state: 'pending'};
}

// The provider the row's badge names, by id ('' while none is known).
function providerChipId(t) {
  const provider = transferProviderPresentation(t);
  return provider.state === 'known' ? String(provider.id || '') : '';
}

function providerChip(t, surface) {
  const provider = transferProviderPresentation(t);
  // A root provider route -- a torrent's one root, or a transfer one of whose
  // roots may switch provider (``route_switch_scope``) -- has its one owner.
  if (t && window.DPRootProvider && (Object.prototype.hasOwnProperty.call(t, 'route_provider_id')
      || Object.prototype.hasOwnProperty.call(t, 'route_switch_scope'))) {
    return window.DPRootProvider.badgeMarkup(t, surface, provider);
  }
  const identity = window.DPTransferSourcePresentation.providerBadgeIdentity(
    provider.state === 'known' ? provider.id : '');
  return `<span class="dp-provider-chip" data-provider-state="${provider.state}"${identity.attributes}>`
    + `${identity.glyph}${esc(provider.label)}</span>`;
}

function routeOutcomePresentation(value) {
  const normalized = String(value || '').trim().toLowerCase();
  const labels = {
    completed: 'Completed',
    succeeded: 'Completed',
    failed: 'Failed',
    cancelled: 'Cancelled',
    superseded: 'Superseded',
    resolved: 'Resolved',
    declined: 'Not Applicable',
    active: 'In Progress',
    started: 'In Progress',
    prepared: 'In Progress',
    unknown: 'Unknown',
  };
  return {label: labels[normalized] || 'Unknown', state: normalized || 'unknown'};
}

function renderRouteHistory(t) {
  const attempts = Array.isArray(t?.route_attempts) ? t.route_attempts : [];
  if (!attempts.length) {
    const message = t?.provider_provenance_status === 'unknown_legacy'
      ? 'Provider history was not recorded for this legacy transfer.'
      : 'No provider route has been established yet.';
    return `<div class="dp-detail-route-empty">${esc(message)}</div>`;
  }
  // Relationship is the backend's projection (relation + contributing_transfer_id):
  // which transfer contributed each source. Nothing is derived from an address,
  // a file name, the candidate list or a neighbouring transfer.
  const relationOf = (attempt) => {
    const state = String(attempt.relation || '').trim().toLowerCase();
    const contributor = Number(attempt.contributing_transfer_id);
    const contributed = Number.isInteger(contributor) && contributor > 0 && contributor !== Number(t?.id);
    // One provenance wording for any route another transfer contributed; the
    // status column alone says whether it was verified.
    if (state === 'consolidated' && contributed) return {state, label: `From #${contributor}`};
    if (state === 'unverified' || state === 'failed') return {state, label: contributed ? `From #${contributor}` : 'Original'};
    if (state === 'original') return {state, label: 'Original'};
    return {state: '', label: ''};
  };
  // Where the source came from reads as a parenthetical aside beside the URL;
  // what happened to that route keeps the plain status wording to its right.
  // Parentheses are the only change to the label text.
  const originLabel = (label) => label ? `(${label})` : '';
  return `<div class="dp-detail-route-list">${attempts.map((attempt) => {
    const provider = attempt.provider_name || 'Unknown';
    // An unverified association is the backend's statement about this source; it
    // replaces the route's own outcome label and never reads as a verified candidate.
    const unverified = attempt.verification_state === 'unverified';
    const outcome = unverified ? {label: 'Unverified', state: 'unverified'} : routeOutcomePresentation(attempt.outcome);
    const outcomeTitle = unverified && attempt.unverified_reason ? `Equivalence unproven: ${attempt.unverified_reason}`
      : (attempt.failure_reason ? `Source failed: ${attempt.failure_reason}` : '');
    const relation = relationOf(attempt);
    const identity = attempt.route_identity || '—';
    const identityTitle = attempt.route_location || attempt.route_identity || '';
    return `<div class="dp-detail-route-row" data-route-relation="${esc(relation.state)}">
      <span class="dp-detail-route-order">${esc(attempt.presentation_ordinal ?? attempt.ordinal ?? '')}</span>
      <span class="dp-detail-route-provider">${esc(provider)}</span>
      <span class="dp-detail-route-identity" data-tooltip="${esc(identityTitle)}">${esc(identity)}</span>
      <span class="dp-detail-route-relation">${esc(originLabel(relation.label))}</span>
      <span class="dp-detail-route-outcome" data-route-outcome="${esc(outcome.state)}" data-tooltip="${esc(outcomeTitle)}">${esc(outcome.label)}</span>
    </div>`;
  }).join('')}</div>`;
}

function sanitizeErrorMsg(message) {
  const text = String(message || 'Request failed');
  return text.length > 500 ? text.slice(0, 497) + '...' : text;
}

function toast(msg, type = 'info') {
  if (!window.DPIcons || typeof window.DPIcons.toast !== 'function') {
    throw new Error('DebridPulse icon runtime is unavailable');
  }
  return window.DPIcons.toast(msg, type);
}

function setButtonPending(button, pending, pendingLabel) {
  if (!button) return;

  if (!button.dataset.defaultLabel) {
    button.dataset.defaultLabel = button.textContent;
  }

  if (pending) {
    button.dataset.pending = '1';
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');

    if (pendingLabel) {
      button.textContent = pendingLabel;
    }

    return;
  }

  delete button.dataset.pending;
  button.disabled = false;
  button.removeAttribute('aria-busy');

  if (button.dataset.defaultLabel) {
    button.textContent = button.dataset.defaultLabel;
  }
}

function coalesceAsync(fn) {
  let running = null;
  let trailing = false;

  return function(...args) {
    if (running) {
      trailing = true;
      return running;
    }

    const context = this;

    const run = async () => {
      let result;

      do {
        trailing = false;
        result = await fn.apply(context, args);
      } while (trailing);

      return result;
    };

    running = run().finally(() => {
      running = null;
    });

    return running;
  };
}


// ── Format ─────────────────────────────────────────────────────────────────
function fmtSize(b) {
  if (!b) return '—';
  const u = ['B','KB','MB','GB','TB']; let i = 0;
  while (b >= 1024 && i < u.length-1) {b/=1024; i++;}
  return b.toFixed(1)+' '+u[i];
}
function fmtTransferRate(bps, rollover) {
  const speed = Number(bps);
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = speed / 1024;
  let unit = 0;
  while (Number(value.toFixed(2)) >= rollover && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return value.toFixed(2)+' '+units[unit]+'/s';
}
function fmtSpeed(bps) {
  const speed = Number(bps);
  if (!Number.isFinite(speed) || speed <= 0) return '0 KB/s';
  if (speed < 1024) return '<1 KB/s';
  return fmtTransferRate(speed, 100);
}
function fmtSpeedCap(bps) {
  const speed = Number(bps);
  if (!Number.isFinite(speed) || speed <= 0) return 'Unlimited';
  return fmtTransferRate(speed, 1000);
}
function parseApiDate(d) {
  if (!d) return null;
  let value = d;
  // SQLite CURRENT_TIMESTAMP is canonical UTC but historically serialized as a
  // naive "YYYY-MM-DD HH:MM:SS" string. Treat that legacy form as UTC.
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(value.trim())) {
    value = value.trim().replace(' ', 'T') + 'Z';
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}
function pct(part, total) {
  if (!total) return 0;
  return Math.round((part / total) * 100);
}
function badge(s, detail) {
  if (!window.DPIcons || typeof window.DPIcons.statusBadge !== 'function') {
    throw new Error('DebridPulse icon runtime is unavailable');
  }
  // A backend-projected presentation_status (transfers.presentation_repository
  // .effective_presentation) is canonical lifecycle truth and always wins over
  // the raw status/failure-semantics guess below.
  const projected = String(detail?.presentation_status || '').trim().toLowerCase();
  if (projected) {
    const tone = String(detail?.presentation_badge_status || projected).trim().toLowerCase();
    const label = String(detail?.presentation_label || '').trim();
    const html = window.DPIcons.statusBadge(tone, label || undefined, '');
    return html.replace('<span class="badge ', '<span data-dp-lifecycle-status="' + esc(projected) + '" class="badge ');
  }
  const semantics = window.DPFailureSemantics;
  const category = s === 'error' && semantics ? semantics.classify(detail) : '';
  return window.DPIcons.statusBadge(s, category ? semantics.labels[category] : '', category);
}
// The backend's effective presentation is the one display-status authority; every
// list and detail payload carries it. The raw lifecycle status is only what a
// caller that fabricated a payload without a projection can fall back to --
// no status is derived here from extraction or source-failure fields.
// The Details progress line: the list's rule in words -- verified material
// first; current execution activity, only while the row is running, leads
// while nothing is verified yet, and is never completion. A best-effort
// estimate is never a second figure beside verified material, and a
// processing writer's retained fill is processing, not a percentage.
function dpDetailProgress(t) {
  const status = transferDisplayStatus(t);
  const active = inFlightExecutionPercent(status, t.active_execution_progress);
  const execution = executionProcessing(status, t.active_execution_basis)
    ? 'processing (not yet verified)'
    : (active !== null ? executionProgressWords(active, t.active_execution_basis) + ' (not yet verified)' : '');
  if (t.progress == null || (execution && Number(t.progress) === 0)) {
    return execution || '—' + (t.retained_bytes ? ' · ' + fmtSize(t.retained_bytes) : '');
  }
  const beside = execution && t.active_execution_basis !== 'estimated';
  return Number(t.progress).toFixed(1) + '%' + (beside ? ' · ' + execution : '');
}
// What an execution percentage counts (``active_execution_basis``): acquired
// bytes, completed acquisition units (parts such as media segments), or one
// writer's best-effort estimate of its whole execution -- never logical
// completion.
function executionProgressWords(value, basis) {
  if (basis === 'estimated') return '~' + value.toFixed(1) + '% estimated';
  return 'in progress ' + value.toFixed(1) + '%' + (basis === 'units' ? ' of parts' : '');
}
// The running row's writers are past acquisition (repair, unpack, remux,
// finalization): no acquisition percentage, and nothing yet verified by it.
function executionProcessing(status, basis) {
  return String(status || '').toLowerCase() === 'downloading' && basis === 'processing';
}
function transferDisplayStatus(t) {
  const projected = String(t && t.presentation_status || '').trim().toLowerCase();
  return projected || (t && t.status) || '';
}
// The one active-execution predicate every progress presentation shares:
// in-flight execution activity counts only while the row is actively running,
// anywhere else a lingering value is stale and never shown. The percentage, or
// null when there is none to trust.
function inFlightExecutionPercent(status, activePct) {
  if (String(status || '').toLowerCase() !== 'downloading') return null;
  const raw = activePct === null || activePct === undefined ? NaN : Number(activePct);
  return Number.isFinite(raw) ? Number(Math.min(Math.max(raw, 0), 100).toFixed(1)) : null;
}
// The estimated time remaining of a downloading transfer, beneath its bar:
// the backend's one estimate (``acquisition_eta``: whole seconds, and whether
// it is held while no new data arrives), never one of the browser's own, and
// never counted down between updates. ``HH:MM:SS`` with at least two hour
// digits and no 24-hour wrap; anything that is not a whole number of seconds
// within the backend's ceiling (``MAX_ETA_SECONDS``, 9999:59:59) is "—".
const ACQUISITION_ETA_MAX_SECONDS = 9999 * 3600 + 59 * 60 + 59;
function acquisitionEtaText(seconds) {
  if (!Number.isInteger(seconds) || seconds <= 0 || seconds > ACQUISITION_ETA_MAX_SECONDS) return null;
  const two = value => String(value).padStart(2, '0');
  return two(Math.floor(seconds / 3600)) + ':' + two(Math.floor(seconds / 60) % 60) + ':' + two(seconds % 60);
}
function acquisitionEtaMarkup(eta) {
  const text = acquisitionEtaText(eta?.seconds);
  if (text === null) {
    return '<span class="prog-eta is-unavailable" data-role="transfer-eta" aria-label="Estimated time remaining: not available"' +
      ' data-tooltip="No reliable estimate of the time remaining right now">—</span>';
  }
  const held = eta.held === true;
  const tooltip = held
    ? 'Estimated time remaining, held: no new data for a few seconds'
    : 'Estimated time remaining for the download, a prediction; processing afterwards is not included';
  return '<span class="prog-eta' + (held ? ' is-held' : '') + '" data-role="transfer-eta"' +
    ' aria-label="Estimated time remaining ' + text + (held ? ', held' : '') + '" data-tooltip="' + tooltip + '">' +
    text + '</span>';
}

// ``activePct``: in-flight execution activity on the current source that is
// not yet DP material, from any executor; shown beside, never as, completion.
// ``basis`` (``active_execution_basis``) says what it counts. ``eta``: the
// row's ``acquisition_eta`` where its surface shows one (Downloads), shown
// only while the row is downloading; omitted, no estimate is rendered.
function progress(pct, status, activePct, basis, eta) {
  const state = String(status || '').toLowerCase();
  const done = state === 'completed';
  const failed = state === 'error';
  const active = state === 'downloading';
  // No trustworthy total size: the percentage is unavailable, never 0%.
  const unknown = !done && (pct === null || pct === undefined);
  const raw = Number(pct);
  const actual = done ? 100 : Math.min(Math.max(Number.isFinite(raw) ? raw : 0, 0), 100);
  const activeValue = inFlightExecutionPercent(state, activePct);
  const inFlight = activeValue !== null;
  // While nothing is verified (no verified percentage, or exactly 0%), current
  // execution activity is the most useful truthful progress, so it is the
  // primary bar -- worded as not yet verified, and never completion. Beside a
  // nonzero verified percentage it stays secondary: nothing proves the two
  // share a denominator, so they are never compared, and only verified
  // material is durable across a replaced execution (no high-water mark).
  const activeOnly = inFlight && (unknown || actual === 0);
  const processing = executionProcessing(state, basis);
  // A best-effort estimate of the whole execution is one primary figure, or
  // nothing beside verified material: never a second lane. A processing
  // writer's value is only the fill its acquisition reached.
  const estimated = basis === 'estimated';
  const besideVerified = inFlight && !activeOnly && !estimated && !processing;
  // Running with no primary percentage to show -- an unknown total, nothing
  // verified and no measurable acquisition, or writers past acquisition -- is
  // the one moving indeterminate state: active, never a fabricated figure.
  const showStripe = active && !activeOnly && (unknown || actual === 0);
  const visual = activeOnly ? activeValue : actual;
  let fillStyle = showStripe
    ? 'width:100%;opacity:.35;background:repeating-linear-gradient(90deg,var(--accent) 0,var(--accent) 8px,transparent 8px,transparent 16px)'
    : 'width:' + visual + '%';
  if (failed) {
    fillStyle += ';opacity:1;background:var(--dp-state-error)!important;background-color:var(--dp-state-error)!important;background-image:none!important;box-shadow:0 0 8px color-mix(in srgb,var(--dp-state-error) 88%,transparent),0 0 17px color-mix(in srgb,var(--dp-state-error) 46%,transparent)!important;filter:saturate(1.12) brightness(1.08)';
  }
  const cls = done ? 'done' : (failed ? 'error dp-terminal-error-progress' : (showStripe ? 'is-indeterminate' : ''));
  const trackCls = failed ? 'prog dp-terminal-error-rail' : 'prog';
  const attrs = failed
    ? ' data-dp-actual-progress="' + actual + '" data-dp-visual-progress="' + visual + '"'
    : (activeOnly ? ' data-progress-basis="execution"' : '');
  // Two truths, never one number: the bar and percentage above are DP-valid
  // material; in-flight execution activity that is not yet material (from
  // any executor) gets its own thinner lane and label beneath, and the
  // verified figure says so.
  const label = done ? '100%' : (activeOnly
    ? (processing ? 'processing' : (estimated ? executionProgressWords(activeValue, basis)
      : activeValue.toFixed(1) + '% ' + (basis === 'units' ? 'of parts' : 'in progress')))
    : (showStripe && processing ? 'processing' : (unknown ? '—'
      : actual.toFixed(0) + '%' + (inFlight || processing ? ' verified' : ''))));
  const inFlightTitle = 'In progress on the current source; not yet verified as DebridPulse material';
  const lane = besideVerified
    ? '<div class="prog-lane" data-role="execution-progress-lane" role="progressbar" aria-valuemin="0" aria-valuemax="100"' +
      ' aria-valuenow="' + activeValue + '" aria-label="' + inFlightTitle + '">' +
      '<div class="prog-lane-fill" style="width:' + activeValue + '%"></div></div>'
    : '';
  const activityLabel = activeOnly || (showStripe && processing)
    ? '<span class="prog-activity" data-role="execution-unverified" data-tooltip="' + inFlightTitle + '">not yet verified</span>'
    : (besideVerified
      ? '<span class="prog-activity" data-role="execution-progress" data-tooltip="' + inFlightTitle + '">' +
        executionProgressWords(activeValue, basis) + '</span>'
      : (processing ? '<span class="prog-activity" data-role="execution-processing">processing</span>' : ''));
  // Floated right on the percentage's line, so it sits beneath the bar
  // without moving the label, the lane or the row's height.
  const etaLabel = active && eta !== undefined ? acquisitionEtaMarkup(eta) : '';
  return '<div class="' + trackCls + '"' + (failed ? ' data-dp-actual-progress="' + actual + '"' : '') + '><div class="prog-fill ' + cls + '" style="' + fillStyle + '"' + attrs + '></div></div>' +
         lane + etaLabel + '<span class="prog-pct">' + label + '</span>' + activityLabel;
}

// An extraction failure is announced once per event; it paints nothing. Only
// the publication of the transfer's claimed lifecycle event (lifecycle_event
// "completed": the server's one announcement, as for its Discord notice) says
// it; every other torrent_updated of the same failed transfer -- a control
// action, a later refresh -- only re-renders it. The status badge of every row
// comes from the backend's effective presentation on the authoritative refresh
// that every torrent_updated event triggers.
function notifyExtractionFailure(data) {
  if (data?.lifecycle_event !== 'completed') return;
  if (String(data?.extraction_status || '').trim() !== 'error') return;
  const reason = sanitizeErrorMsg(
    data?.extraction_error || 'Archive extraction failed'
  );
  toast(`Extraction failed: ${reason}`, 'error');
}

function patchProgressOnlyTransferEvent(data) {
  const updates = Array.isArray(data?.items) ? data.items : [];

  if (!data?.progress_only || !updates.length) {
    return false;
  }

  // Status transitions change filters and available action buttons.
  // Those still get an authoritative full refresh.
  if (updates.some(update => !!update?.status_changed)) {
    return false;
  }

  for (const update of updates) {
    const id = Number(update?.id ?? update?.torrent_id);
    const nextProgress = update?.progress == null ? null : Number(update.progress);
    const nextActivity = update?.active_execution_progress == null ? null : Number(update.active_execution_progress);
    const nextBasis = update?.active_execution_basis ?? null;
    const nextEta = update?.acquisition_eta ?? null;

    if (!Number.isFinite(id) || (nextProgress !== null && !Number.isFinite(nextProgress))) {
      continue;
    }

    document
      .querySelectorAll(`tr[data-torrent-id="${id}"]`)
      .forEach(row => {
        // The bar is styled by the presentation status the row was rendered with;
        // the event's raw lifecycle state is never a second source for it.
        const status = String(row.dataset.presentationStatus || row.dataset.status || '');

        const progressCell =
          row.querySelector('[data-role="transfer-progress"]');

        if (progressCell) {
          // Only a surface that renders an estimate (Downloads) gets one: its
          // downloading rows always carry the estimate element, and a status
          // change is never patched here.
          const showsEta = progressCell.querySelector('[data-role="transfer-eta"]') !== null;
          progressCell.innerHTML =
            progress(nextProgress, status, nextActivity, nextBasis, showsEta ? nextEta : undefined);
        }

        const dashFill =
          row.querySelector('.dash-row-bar-fill');

        if (dashFill) {
          const pctValue =
            Math.min(100, Math.max(0, nextProgress));

          dashFill.style.width = `${pctValue}%`;
        }
      });
  }

  return true;
}

// ── Status Bar ─────────────────────────────────────────────────────────────

async function checkConnections() {
  if (!dpCan('administer')) return;  // engine and provider health are Admin's
  const cfg = settingsData || {};
  await refreshProviderStatus();

  // aria2 check — retry once if first attempt fails
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const result = await api('POST', '/settings/test-aria2');
      setDot('aria2', 'ok', `aria2: ${result.version||'online'}`);
      break;
    } catch {
      if (attempt < 3) {
        await new Promise(r => setTimeout(r, attempt * 800));
      } else {
        setDot('aria2', 'error', 'aria2: offline');
      }
    }
  }
}

function setDot(id, state, label) {
  const d = document.getElementById('dot-'+id);
  const l = document.getElementById('lbl-'+id);
  if (!d || !l) return;  // element not in DOM yet
  d.className = 'dot' + (state ? ' '+state : '');
  l.textContent = label;
}

async function pauseProcessing() {
  const button =
    document.getElementById('btn-pause-all');

  setButtonPending(button, true, 'Pausing…');

  try {
    await api('POST', '/processing/pause');
    processingPaused = true;
    renderTopbarActions();
    toast('Processing paused','warn');
    loadStats();
    loadRecent();

    if (
      document
        .getElementById('view-torrents')
        .classList.contains('active')
    ) {
      loadTorrents();
    }
  } catch(e) {
    toast(sanitizeErrorMsg(e.message),'error');
  } finally {
    setButtonPending(button, false);
    renderTopbarActions();
  }
}

async function resumeProcessing() {
  const button =
    document.getElementById('btn-resume-all');

  setButtonPending(button, true, 'Resuming…');

  try {
    await api('POST', '/processing/resume');
    processingPaused = false;
    renderTopbarActions();
    toast('Processing resumed','success');
    loadStats();
    loadRecent();

    if (
      document
        .getElementById('view-torrents')
        .classList.contains('active')
    ) {
      loadTorrents();
    }
  } catch(e) {
    toast(sanitizeErrorMsg(e.message),'error');
  } finally {
    setButtonPending(button, false);
    renderTopbarActions();
  }
}

async function resumePausedDownloads() {
  const button =
    document.getElementById('btn-resume-paused');

  setButtonPending(button, true, 'Resuming…');

  try {
    await api('POST', '/processing/resume');
    processingPaused = false;
    renderTopbarActions();
    toast('Paused downloads resumed','success');
    loadStats();
    loadRecent();

    if (
      document
        .getElementById('view-torrents')
        .classList.contains('active')
    ) {
      loadTorrents();
    }
  } catch(e) {
    toast(sanitizeErrorMsg(e.message),'error');
  } finally {
    setButtonPending(button, false);
    renderTopbarActions();
  }
}

// ── Dashboard ──────────────────────────────────────────────────────────────
function fmtDuration(secs) {
  if (!secs || secs <= 0) return '—';
  if (secs < 60) return secs + 's';
  if (secs < 3600) return Math.round(secs/60) + 'm';
  return (secs/3600).toFixed(1) + 'h';
}

var _operatorTitleState = {active: 0, progress: 0};

function renderOperatorTitle() {
  if (_operatorTitleState.active === 0) {
    document.title = 'DebridPulse';
    return;
  }

  const liveBps = (_runtimeStatusState && Number(_runtimeStatusState.liveBps)) || 0;
  const speed = fmtTransferRate(Math.max(0, liveBps), 100).replace(/\s+/g, '');
  document.title = `DP | ${speed} (${_operatorTitleState.progress}%)`;
}

function updateOperatorTitle(stats) {
  const byStatus = stats && stats.by_status && typeof stats.by_status === 'object' ? stats.by_status : null;
  const nonNegativeCount = value => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.max(0, Math.trunc(parsed)) : 0;
  };
  const logicalActive = byStatus
    ? nonNegativeCount(byStatus.downloading) + nonNegativeCount(byStatus.queued)
    : nonNegativeCount(stats && stats.operator_active_downloads);

  updateOperatorTitle._latestLogicalActive = logicalActive;

  const cancelIdle = () => {
    if (updateOperatorTitle._idleTimer != null) {
      clearTimeout(updateOperatorTitle._idleTimer);
      updateOperatorTitle._idleTimer = null;
    }
  };

  if (stats && stats.paused) {
    cancelIdle();
    _operatorTitleState.active = 0;
    _operatorTitleState.progress = 0;
    renderOperatorTitle();
    return;
  }

  if (logicalActive > 0) {
    cancelIdle();
    _operatorTitleState.active = logicalActive;
    const rawProgress = stats && stats.operator_active_progress_pct;
    const value = rawProgress == null ? NaN : Number(rawProgress);
    if (Number.isFinite(value)) {
      _operatorTitleState.progress = Math.min(100, Math.max(0, Math.round(value)));
    }
    renderOperatorTitle();
    return;
  }

  if (_operatorTitleState.active === 0) {
    cancelIdle();
    renderOperatorTitle();
    return;
  }

  if (updateOperatorTitle._idleTimer == null) {
    updateOperatorTitle._idleTimer = setTimeout(() => {
      updateOperatorTitle._idleTimer = null;
      if (updateOperatorTitle._latestLogicalActive === 0) {
        _operatorTitleState.active = 0;
        _operatorTitleState.progress = 0;
        renderOperatorTitle();
      }
    }, 1500);
  }
  renderOperatorTitle();
}


/* Dashboard KPI sparklines are decorative artwork, not telemetry: the six
 * cards' lines form one virtual ECG strip. Its route follows the cards as
 * rendered -- rows top to bottom, each row right to left -- with the gaps
 * between cards and rows travelled at their real distance, unseen. One owner
 * starts the heartbeats; every heartbeat travels the whole strip right to left
 * at one fixed speed, so several may be on it at once. Each card's single line
 * is the signal: a flat baseline deformed into the ECG complex wherever a
 * heartbeat is, its stroke gradient bright through the complex's leading QRS
 * and fading behind it back into the dim baseline -- a fade measured in strip
 * distance, at most 416 px long and always ending just before the next
 * heartbeat reaches the same place, so a slow cadence leaves the strip quiet
 * between heartbeats. Beneath the line, the card's accent glows down toward
 * the card's floor: the area under the very same path, masked by the very same
 * stroke gradient, so it rests faint and brightens and fades with the signal.
 *
 * The cadence is ambient: 3.5 BPM at rest rising linearly to 90 BPM as the
 * aggregate download throughput (the core meter's own 4 s window) reaches the
 * highest sustained rate this installation has shown; with no sustained peak
 * recorded yet it stays at 3.5. The cadence eases continuously toward that
 * target with a 4 s time constant, and a heartbeat clock advances with it: the
 * next heartbeat comes when its phase completes. The clock is brought up to
 * date at each heartbeat and each throughput reading, and the pending
 * heartbeat re-timed from the phase it has reached, so a change of throughput
 * moves it earlier or later at once without resetting the clock, bunching,
 * doubling or catching up beats, and never later than one 3.5 BPM interval;
 * beats already travelling keep their place and speed. Heartbeats are drawn
 * only while the Dashboard is the active view of a visible document, and run
 * only while motion is not reduced.
 *
 * Each heartbeat is drawn once, at its birth, with its own slightly different
 * complex -- overall amplitude, R and S depths, the smaller deflections and its
 * width varying within fixed bounds, the sequence, polarity and baseline kept --
 * and keeps that shape across every card until it leaves the strip.
 *
 * Global Pause All (processingPaused, the projection of the backend's durable
 * pause state that loadStats reads) holds the strip: no heartbeat is started,
 * and the signal settles where it stands -- every complex on the strip eases
 * flat to the baseline over 700 ms while it travels on, the last one keeping
 * the trail it had (laid out for the heartbeat then due) -- and once it is flat
 * every line becomes the same flat red, breathing in step (still, under reduced
 * motion). Released before it is flat, it rises again from where it was.
 * Resuming restores the cards' colours and starts a heartbeat at once -- but
 * not before the last one's trail leaves it its clearance, nor within one 90
 * BPM interval of it -- and the cadence carries on from the throughput. */
const DASHBOARD_HEARTBEAT_BPM = [3.5, 90];
const DASHBOARD_HEARTBEAT_EASE_MS = 4000;      // time constant of the cadence's approach to its target
const DASHBOARD_HEARTBEAT_SETTLE_MS = 700;     // Global Pause All: the signal settles flat to its baseline over this
const DASHBOARD_HEARTBEAT_SPEED = 0.24;        // px per ms along the strip
const DASHBOARD_HEARTBEAT_COMPLEX = [          // [x px from the leading edge, y in the 24-unit viewBox]
  [0, 16], [6, 16], [9, 13.5], [12, 16], [16, 16], [18, 17.5], [22, 2], [26, 23],
  [29, 14], [31, 15], [36, 9], [41, 16], [44, 14.5], [47, 16], [48, 16]
];
const DASHBOARD_HEARTBEAT_WIDTH = 48;          // px
const DASHBOARD_HEARTBEAT_CLEARANCE = 16;      // px of resting baseline left before the following heartbeat
const DASHBOARD_HEARTBEAT_TRAIL = 416;         // px: the longest a trail fades over, however far behind the next heartbeat is
const DASHBOARD_HEARTBEAT_VIEW_HEIGHT = 29.42; // viewBox units in the 38 px line box: 24 units per 31 px, the baseline 16 units down
const _dashboardHeartbeat = {running: false, suspended: false, held: false, timer: 0, frame: 0, beats: [], route: null,
  bytesPerSecond: 0, sustainedPeak: 0, bpm: DASHBOARD_HEARTBEAT_BPM[0], phase: 0, advancedAt: 0, nextAt: 0, earliest: 0,
  settle: null};                               // the signal's height easing (Global Pause All): {at, from, to, span}

/* One heartbeat's own complex, drawn at its birth, every bound holding however
 * the draws combine. Height: each deflection from the y=16 baseline scales by
 * an overall factor (+-15%) times its own -- R +-15% (0.72-1.32), S +-18%
 * (0.70-1.36), each smaller hump +-22% (0.66-1.40), so R always stands well
 * above T. Length: the P, QRS and T-U stretches of the complex scale by their
 * own factors (+-18%, +-10%, +-22%) times an overall one (+-12%), so the
 * complex runs 0.72-1.32 of its 48 px. Points stay in order, every deflection
 * keeps its direction, and both ends rest on the baseline; S at its deepest,
 * with its glow, still clears the card's floor. */
function dashboardHeartbeatShape(random = Math.random) {
  const draw = () => random() * 2 - 1;
  const height = 1 + 0.15 * draw(), length = 1 + 0.12 * draw();
  const factor = y => height * (1 + (y === 2 ? 0.15 : y === 23 ? 0.18 : 0.22) * draw());   // R, S, the rest
  // Where each base x lands: the P (0-16), QRS (16-29) and T-U (29-48) stretches each scaled.
  const stretches = [[16, 1 + 0.18 * draw()], [13, 1 + 0.10 * draw()], [19, 1 + 0.22 * draw()]];
  const place = dx => {
    let from = 0, to = 0;
    for (const [span, scale] of stretches) {
      if (dx <= from + span) return length * (to + (dx - from) * scale);
      from += span; to += span * scale;
    }
    return length * to;
  };
  const points = DASHBOARD_HEARTBEAT_COMPLEX.map(([dx, y]) =>
    Object.freeze([place(dx), y === 16 ? 16 : 16 + (y - 16) * factor(y)]));
  return Object.freeze({points: Object.freeze(points), width: points[points.length - 1][0],
    qrs: points[7][0]});                                   // where S, the end of the bright P-QRS lead, lands
}

/* Global Pause All holds the strip; once nothing is left on it, the flat red lines. */
function syncDashboardHeartbeatFlatline() {
  document.getElementById('dash-hero')?.classList.toggle('dash-hero--flatline',
    processingPaused === true && !_dashboardHeartbeat.beats.length);
}
const _dashboardSparkReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

/* Whether the strip is on screen: the Dashboard is the active view of a visible
 * document. Only then is anything painted; leaving the Dashboard for another
 * view never stops the heartbeat clock. */
function dashboardHeartbeatShown() {
  return !document.hidden && !!document.getElementById('view-dashboard')?.classList.contains('active');
}

/* The live throughput facts, from their one poll (loadRuntimeSpeed). The
 * heartbeat clock runs up to now on the facts it had, then the pending
 * heartbeat is re-timed (none is pending while the strip is held). */
function setDashboardHeartbeatThroughput(bytesPerSecond, sustainedPeak) {
  const state = _dashboardHeartbeat;
  const now = performance.now();
  if (state.running) advanceDashboardHeartbeat(now);
  state.bytesPerSecond = Math.max(0, Number(bytesPerSecond) || 0);
  state.sustainedPeak = Math.max(0, Number(sustainedPeak) || 0);
  if (state.running && !state.held && !state.suspended) scheduleDashboardHeartbeat(now);
}

/* The authoritative global pause state, as loadStats has just read it: hold
 * the strip (stop starting heartbeats; those on it travel on) or release it
 * (a heartbeat is due at once). */
function syncDashboardHeartbeatPause() {
  const state = _dashboardHeartbeat;
  const held = processingPaused === true;
  if (state.running && held !== state.held) {
    // Unseen in a hidden document, the heartbeats due before the hold are born first.
    if (held && state.suspended) advanceDashboardHeartbeat(performance.now());
    state.held = held;
    const last = state.beats[state.beats.length - 1];
    if (held) {
      clearTimeout(state.timer);
      state.timer = 0;
      if (last) last.until = state.nextAt;          // its trail stays as laid out for the heartbeat then due
      settleDashboardHeartbeat(performance.now(), 0);   // it settles flat from where it stands
    } else {
      const now = performance.now();
      settleDashboardHeartbeat(now, 1);             // not yet flat: it rises again from where it was
      easeDashboardHeartbeat(now);                  // none was due while held
      state.phase = 1;
      // Not before the held trail leaves the next heartbeat its clearance.
      state.earliest = last
        ? last.born + (dashboardHeartbeatTrail(last, Infinity) + last.shape.width + DASHBOARD_HEARTBEAT_CLEARANCE) / DASHBOARD_HEARTBEAT_SPEED
        : 0;
      if (!state.suspended) scheduleDashboardHeartbeat(now);
    }
  }
  syncDashboardHeartbeatFlatline();
}

function dashboardHeartbeatTargetBpm() {
  const [low, high] = DASHBOARD_HEARTBEAT_BPM;
  const {bytesPerSecond, sustainedPeak} = _dashboardHeartbeat;
  if (!sustainedPeak) return low;
  return low + (high - low) * Math.min(1, Math.max(0, bytesPerSecond / sustainedPeak));
}

/* The strip as rendered: each card's line, where the heartbeat's leading edge
 * reaches its right edge (entry, px along the strip), and where it leaves the
 * last card (end). */
function measureDashboardHeartbeatRoute() {
  const cards = [...document.querySelectorAll('#view-dashboard .dp-card-spark')].map(svg => {
    const box = svg.getBoundingClientRect();
    return {svg, line: svg.querySelector('.dp-card-spark-line'), fill: svg.querySelector('.dp-card-spark-fill'),
      gradient: svg.querySelector('linearGradient[id^="dp-card-spark-signal-"]'),
      left: box.left, right: box.right, top: box.top, middle: (box.top + box.bottom) / 2, width: box.width, d: '', paint: ''};
  }).filter(card => card.width > 0);
  const rows = [];
  cards.sort((a, b) => a.top - b.top).forEach(card => {
    const row = rows[rows.length - 1];
    if (row && Math.abs(card.middle - row[0].middle) < 8) row.push(card);
    else rows.push([card]);
  });
  const route = [];
  let entry = 0;
  rows.forEach(row => row.sort((a, b) => b.right - a.right).forEach(card => {
    const previous = route[route.length - 1];
    card.row = row;
    if (previous) entry += previous.row === row
      ? previous.right - card.right
      : previous.width + Math.hypot(card.right - previous.left, card.middle - previous.middle);
    card.entry = entry;
    card.svg.setAttribute('viewBox', `0 0 ${card.width} ${DASHBOARD_HEARTBEAT_VIEW_HEIGHT}`);
    route.push(card);
  }));
  const last = route[route.length - 1];
  return {cards: route, end: last ? last.entry + last.width : 0};
}

/* A heartbeat's trail: the strip distance to the heartbeat that follows it
 * (born at `following`, or due then) -- or, if the strip was held, to the one
 * that was due then -- less its own complex and the clearance, and never
 * longer than DASHBOARD_HEARTBEAT_TRAIL. */
function dashboardHeartbeatTrail(beat, following) {
  const spacing = DASHBOARD_HEARTBEAT_SPEED * (Math.min(following, beat.until ?? Infinity) - beat.born);
  return Math.min(DASHBOARD_HEARTBEAT_TRAIL, Math.max(0, spacing - beat.shape.width - DASHBOARD_HEARTBEAT_CLEARANCE));
}

/* The heartbeats on the strip at `now`, with how far each has travelled and
 * its trail; those whose trail has left the strip are dropped. */
function liveDashboardHeartbeats(now) {
  const state = _dashboardHeartbeat;
  const beats = state.beats.map((beat, i) => ({beat, shape: beat.shape,
    travelled: (now - beat.born) * DASHBOARD_HEARTBEAT_SPEED,
    trail: dashboardHeartbeatTrail(beat, i + 1 < state.beats.length ? state.beats[i + 1].born : state.nextAt)}));
  const live = beats.filter(({shape, travelled, trail}) => travelled - shape.width - trail <= state.route.end);
  state.beats = live.map(({beat}) => beat);
  return live;
}

/* The signal's height at `now` (1 full, 0 flat on the baseline): from where
 * it was toward where Global Pause All sends it, over a span in proportion to
 * the distance (700 ms from full to flat) -- moving at once, then easing to
 * rest (an ease-out). */
function dashboardHeartbeatHeight(now) {
  const settle = _dashboardHeartbeat.settle;
  if (!settle) return 1;
  const p = Math.min(1, Math.max(0, (now - settle.at) / settle.span));
  return settle.from + (settle.to - settle.from) * (1 - (1 - p) ** 2);
}
function settleDashboardHeartbeat(now, to) {
  const state = _dashboardHeartbeat;
  // Already settled flat, though unseen (no frame ran): the strip is clear.
  if (state.settle?.to === 0 && now - state.settle.at >= state.settle.span) Object.assign(state, {settle: null, beats: []});
  const from = dashboardHeartbeatHeight(now);
  state.settle = from === to || !state.beats.length ? null
    : {at: now, from, to, span: DASHBOARD_HEARTBEAT_SETTLE_MS * Math.abs(to - from)};
  if (!state.settle && to === 0) state.beats = [];   // nothing standing to settle
}

/* Draw every card's line for the heartbeats on the strip at `now`. */
function renderDashboardHeartbeat(now) {
  const {cards} = _dashboardHeartbeat.route;
  const fmt = value => Number(value.toFixed(2));
  const beats = liveDashboardHeartbeats(now);
  for (const card of cards) {
    const width = card.width;
    const here = beats
      .map(({shape, travelled, trail}) => ({x: width - (travelled - card.entry), shape, trail}))
      .filter(({x, shape, trail}) => x - 4 < width && x + shape.width + trail > 0)
      .sort((a, b) => a.x - b.x);
    const complexes = here.filter(({x, shape}) => x < width && x + shape.width > 0);
    const start = fmt(Math.min(0, complexes[0]?.x ?? 0));
    let d = `M ${start} 16`;
    const height = dashboardHeartbeatHeight(now);
    for (const {x, shape} of complexes) d += shape.points.map(([dx, y]) => ` L ${fmt(x + dx)} ${fmt(16 + (y - 16) * height)}`).join('');
    const last = complexes[complexes.length - 1];
    const finish = fmt(Math.max(width, last ? last.x + last.shape.width : 0));
    d += ` L ${finish} 16`;
    if (d !== card.d) {
      card.d = d;
      card.line.setAttribute('d', d);
      // The glow: the area under the same line, down to the line box's floor.
      card.fill.setAttribute('d', `${d} V ${DASHBOARD_HEARTBEAT_VIEW_HEIGHT} H ${start} Z`);
    }
    // Per heartbeat: rest before its leading edge, bright through P-QRS, the
    // body colour at the complex's end, then fading through the tail to rest
    // where its trail ends.
    const groups = here.map(({x, shape, trail}) => {
      const tail = x + shape.width;
      return [x - 4, x + 4, x + shape.qrs, tail, tail + trail * 0.3, tail + trail];
    });
    const from = Math.min(0, ...groups.map(group => group[0]));
    const to = Math.max(width, ...groups.map(group => group[5]));
    const paint = `${fmt(from)} ${fmt(to)} ${groups.flat().map(fmt).join(' ')}`;
    if (paint === card.paint) continue;
    card.paint = paint;
    card.gradient.setAttribute('x1', String(fmt(from)));
    card.gradient.setAttribute('x2', String(fmt(to)));
    const stops = card.gradient.children;
    while (stops.length < 1 + 6 * groups.length) {
      ['rest', 'lead', 'lead', 'body', 'tail', 'rest'].forEach(role => {
        const stop = document.createElementNS('http://www.w3.org/2000/svg', 'stop');
        stop.setAttribute('class', `dp-card-spark-${role}`);
        stop.setAttribute('offset', '1');
        card.gradient.appendChild(stop);
      });
    }
    for (let group = 0; 1 + 6 * group < stops.length; group++) {
      const positions = groups[group];
      for (let i = 0; i < 6; i++) {
        stops[1 + 6 * group + i].setAttribute('offset', positions ? String((positions[i] - from) / (to - from)) : '1');
      }
    }
  }
}

function dashboardHeartbeatFrame(now) {
  const state = _dashboardHeartbeat;
  state.frame = 0;
  renderDashboardHeartbeat(now);
  const settle = state.settle;
  if (settle && now - settle.at >= settle.span) {
    state.settle = null;
    // Settled flat: the strip is clear, drawn as the plain baseline.
    if (settle.to === 0) { state.beats = []; renderDashboardHeartbeat(now); }
  }
  if (state.beats.length) state.frame = requestAnimationFrame(dashboardHeartbeatFrame);
  else syncDashboardHeartbeatFlatline();             // a held strip, now clear
}

/* Run the heartbeat clock up to `now`: the cadence eases toward its target
 * and the phase advances by the beats that eased cadence made (its integral).
 * In a hidden document the clock's timer is suspended, so each heartbeat it
 * comes due for meanwhile is born here instead, at the very instant its timer
 * would have fired -- the strip shown again is the one that was never hidden.
 * Every due instant is stepped through exactly (the cadence carries on from
 * each), but only a heartbeat that can still be on the strip at `now`, trail
 * and all, is kept and given its complex: a day hidden at 90 BPM is about
 * 130,000 steps of this arithmetic alone, a few milliseconds. */
function advanceDashboardHeartbeat(now) {
  const state = _dashboardHeartbeat;
  if (state.suspended && !state.held) {
    const last = state.beats[state.beats.length - 1];
    let born = last ? last.born : -Infinity;
    // Born before this, a heartbeat has left the strip by `now` (its complex is at most 1.32 of 48 px).
    const kept = state.route
      ? now - (state.route.end + 2 * DASHBOARD_HEARTBEAT_WIDTH + DASHBOARD_HEARTBEAT_TRAIL) / DASHBOARD_HEARTBEAT_SPEED
      : -Infinity;
    for (;;) {
      const due = state.advancedAt + dashboardHeartbeatWait(state.advancedAt, born);
      if (due > now) { state.nextAt = due; break; }
      easeDashboardHeartbeat(due);
      state.phase = 0;
      state.earliest = 0;
      born = due;
      if (due >= kept) state.beats.push({born: due, shape: dashboardHeartbeatShape()});
    }
    if (state.route) liveDashboardHeartbeats(now);   // only those still on the strip
  }
  easeDashboardHeartbeat(now);
}
function easeDashboardHeartbeat(now) {
  const state = _dashboardHeartbeat;
  const target = dashboardHeartbeatTargetBpm();
  const elapsed = Math.max(0, now - state.advancedAt);
  const settled = 1 - Math.exp(-elapsed / DASHBOARD_HEARTBEAT_EASE_MS);
  state.phase += (target * elapsed + (state.bpm - target) * DASHBOARD_HEARTBEAT_EASE_MS * settled) / 60000;
  state.bpm += (target - state.bpm) * settled;
  state.advancedAt = now;
}

/* How long after `now` the pending heartbeat is due: when the phase completes
 * at the current cadence, never sooner than one 90 BPM interval after the last
 * one (born at `lastBorn`), nor before the clearance a held trail needs
 * (earliest, after a resume). */
function dashboardHeartbeatWait(now, lastBorn = _dashboardHeartbeat.beats[_dashboardHeartbeat.beats.length - 1]?.born ?? -Infinity) {
  const state = _dashboardHeartbeat;
  return Math.max((1 - state.phase) * 60000 / state.bpm,
    lastBorn + 60000 / DASHBOARD_HEARTBEAT_BPM[1] - now, state.earliest - now, 0);
}

/* (Re)time the pending heartbeat. */
function scheduleDashboardHeartbeat(now) {
  const state = _dashboardHeartbeat;
  const wait = dashboardHeartbeatWait(now);
  clearTimeout(state.timer);
  state.nextAt = now + wait;
  state.timer = setTimeout(dashboardHeartbeat, wait);
}

function dashboardHeartbeat() {
  const state = _dashboardHeartbeat;
  if (processingPaused === true) return syncDashboardHeartbeatPause();   // held since this was timed
  const now = performance.now();
  advanceDashboardHeartbeat(now);
  state.phase = 0;                                  // a completed phase is one heartbeat, never more
  state.earliest = 0;
  state.beats.push({born: now, shape: dashboardHeartbeatShape()});
  scheduleDashboardHeartbeat(now);
  if (!dashboardHeartbeatShown()) liveDashboardHeartbeats(now);   // off screen: only those still on the strip are kept
  else if (!state.frame) state.frame = requestAnimationFrame(dashboardHeartbeatFrame);
}

/* The heartbeat's one lifecycle, on every view change, document visibility
 * change and motion-preference change.
 *
 * Its clock starts where the Dashboard is first seen and then runs for the
 * life of the page: another view being selected stops only the painting, so
 * heartbeats are born on time and travel on unseen, and returning shows the
 * strip exactly as it stands now, at once, before the next paint. A hidden
 * document suspends the clock's timer (frames stop with it); every advance of
 * the clock meanwhile -- each throughput reading, and the return -- births the
 * heartbeats it came due for at their own instants (advanceDashboardHeartbeat),
 * so the first frame back draws each where its age has carried it: the strip
 * that was never hidden, nothing replayed, caught up or started afresh, and
 * those whose trails have left the strip gone. Reduced motion stops it and
 * leaves the flat baselines, as it always has. Global Pause All needs nothing
 * here: its state reaches syncDashboardHeartbeatPause on every view, so a
 * strip held while unseen has drained by the time it is shown. */
function syncDashboardSparkPulses() {
  const state = _dashboardHeartbeat;
  if (_dashboardSparkReducedMotion.matches) {
    if (!state.running) return;
    clearTimeout(state.timer);
    cancelAnimationFrame(state.frame);
    Object.assign(state, {running: false, suspended: false, held: false, timer: 0, frame: 0, beats: [], settle: null});
    renderDashboardHeartbeat(performance.now());   // back to the flat baselines
    syncDashboardHeartbeatFlatline();
    return;
  }
  const shown = dashboardHeartbeatShown();
  const now = performance.now();
  if (!state.running) {
    if (!shown) return;
    const held = processingPaused === true;        // entering a held strip: the flat red lines at once
    Object.assign(state, {running: true, suspended: false, held, route: measureDashboardHeartbeatRoute(),
      bpm: dashboardHeartbeatTargetBpm(), phase: 0, advancedAt: now, nextAt: Infinity, earliest: 0});
    if (!held) scheduleDashboardHeartbeat(now);
  } else if (document.hidden) {
    if (state.suspended) return;
    clearTimeout(state.timer);
    cancelAnimationFrame(state.frame);
    Object.assign(state, {suspended: true, timer: 0, frame: 0});
    return;
  } else if (state.suspended) {
    advanceDashboardHeartbeat(now);                // the heartbeats due while hidden, born on time
    state.suspended = false;
    if (!state.held) scheduleDashboardHeartbeat(now);
  }
  cancelAnimationFrame(state.frame);
  state.frame = 0;
  if (!shown) return;
  // Shown: this very frame is the strip as it stands now, then the frames go on.
  state.route = measureDashboardHeartbeatRoute();
  dashboardHeartbeatFrame(now);
}

/* A reflow of the shown strip re-measures it under the heartbeats already on
 * it (hiding the Dashboard collapses it, which is no reflow of the strip). */
function remeasureDashboardHeartbeat() {
  if (!_dashboardHeartbeat.running || !dashboardHeartbeatShown()) return;
  _dashboardHeartbeat.route = measureDashboardHeartbeatRoute();
  renderDashboardHeartbeat(performance.now());
}

async function loadStats() {
  // Retry up to 5 times — server may be slow on first request after container start
  for (let attempt = 1; attempt <= 5; attempt++) {
    try {
      const s = await api('GET', '/stats');
      updateOperatorTitle(s);
      document.dispatchEvent(new CustomEvent('debridpulse:dashboard-stats-rendered', {detail:s}));
      // ── populate sidebar version ────────────────────────────────────────
      const versionEl = document.getElementById('sidebar-version');
      if (versionEl) versionEl.textContent = s.version ? `v${s.version}` : 'v—';
      processingPaused = !!s.paused;
      syncDashboardHeartbeatPause();
      const bs = s.by_status || {};
      pausedTransferCount = Math.max(0, Number(bs.paused) || 0);
      renderTopbarActions();
      // ── stat cards ─────────────────────────────────────────────────────
      // Soft-deleted rows remain in /stats for diagnostics/duplicate revival,
      // but they are intentionally absent from the normal Downloads view.
      // User-facing totals and Queue Health therefore use the same visible universe.
      const total = Object.entries(bs)
        .filter(([status]) => status !== 'deleted')
        .reduce((sum, [, count]) => sum + (Number(count) || 0), 0);
      updateDownloadsTrackedCopy(total);
      const completed = s.completed_count ?? bs.completed ?? 0;
      document.getElementById('s-total').textContent = total;
      document.getElementById('s-completed').textContent = completed;
      document.getElementById('s-active').textContent = s.active_operations ?? s.active_downloads ?? 0;
      document.getElementById('s-processing').textContent = s.paused ? 'Paused' : (bs.processing||0)+(bs.uploading||0);
      const errCount = s.error_count ?? bs.error ?? 0;
      document.getElementById('s-error').textContent = errCount;
      const errCard = document.getElementById('dash-error-card');
      if (errCard) errCard.classList.toggle('dash-hero-stat--quiet', !(errCount > 0));
      document.getElementById('s-size').textContent = fmtSize(s.total_completed_bytes);
      document.getElementById('s-blocked').textContent = `${s.total_blocked_files||0} blocked files`;
      document.getElementById('i-last-day').textContent = s.completed_last_24h||0;
      document.getElementById('i-last-week').textContent = s.completed_last_7d||0;
      document.getElementById('i-success-rate').textContent = s.success_rate_pct != null ? s.success_rate_pct+'%' : '—';
      document.getElementById('i-avg-duration').textContent = fmtDuration(s.avg_download_duration_seconds);
      document.getElementById('i-avg-size').textContent = s.avg_torrent_size_bytes ? fmtSize(s.avg_torrent_size_bytes) : '—';
      // The Downloads badge: distinct transfers still in the ordinary download
      // lifecycle (one torrents row each, whatever their artifacts or
      // attempts). Failed transfers awaiting the operator and terminal ones
      // are not counted.
      const unfinished = ['pending', 'processing', 'input_required', 'ready', 'queued', 'downloading',
        'paused', 'verifying', 'extracting'].reduce((sum, status) => sum + (Number(bs[status]) || 0), 0);
      const nb = document.getElementById('nb-active');
      if (nb) { nb.textContent = unfinished; nb.style.display = unfinished > 0 ? '' : 'none'; }
      // The topbar's active count is NOT written here: it has exactly one
      // writer, the neutral runtime-status poll, whose number is DebridPulse's
      // canonical execution-admission occupancy rather than this aggregate
      // lifecycle count. Two writers for one visible fact is how the indicator
      // could disagree with itself.
      // ── DB info + dot ──────────────────────────────────────────────────
      // Database status remains in the persistent lower-left status rail.
      setDot('db', 'ok', 'DB: SQLite');
      return true; // signal success to caller
    } catch(e) {
      console.warn('loadStats attempt', attempt, 'failed:', e.message);
      if (attempt < 5) {
        await new Promise(r => setTimeout(r, 500 * attempt));
        continue;
      }
      return false;
    }
  }
  return false;
}


function setStatsPeriod(el) {
  document.querySelectorAll('#stats-period-tabs .ftab').forEach(function(t){t.classList.remove('active');});
  el.classList.add('active');
  loadDetailedStats(el.dataset.period);
}
let _dashboardRecentFitLimit = null;
let _dashboardRecentResizeTimer = null;

function dashboardRecentLimit() {
  const mobile = window.matchMedia('(max-width: 700px)').matches;
  const fallback = window.matchMedia('(max-width: 700px)').matches ? 4 : 6;
  if (mobile) return fallback;

  const wrap = document.querySelector('#dash-activity-card .dash-activity-table-wrap');
  const head = wrap?.querySelector('thead');
  const rows = Array.from(document.querySelectorAll('#dash-tbody tr[data-torrent-id]'));

  if (!wrap || !head || !rows.length) {
    return _dashboardRecentFitLimit || fallback;
  }

  const rowHeights = rows
    .map(row => row.getBoundingClientRect().height)
    .filter(height => Number.isFinite(height) && height > 0);

  if (!rowHeights.length) {
    return _dashboardRecentFitLimit || fallback;
  }

  const rowHeight = Math.max(...rowHeights);
  const available = Math.max(
    0,
    wrap.clientHeight - head.getBoundingClientRect().height - 4
  );
  const fitted = Math.floor(available / rowHeight);
  return Math.max(1, Math.min(32, fitted || 1));
}

// Dashboard Recent Items has exactly one canonical renderer: renderRecent() in
// ui-dashboard-transfer-presentation.js (a bounded presentation owner that is
// lazy-loaded after app.js). This is the stable delegation entrypoint for it:
// it is wrapped once by coalesceAsync (below) and never rebound afterwards, so
// every loadRecent() trigger — startup, nav, SSE, polling, resize, pause/resume,
// submission/import — routes through here for the life of the page.
//
// Calls made before the canonical owner has registered collapse into a single
// owed refresh that fires once, at registration. This entrypoint never renders
// rows and never emits the dashboard-recent-rendered event — the canonical
// owner is the sole producer of both.
let _recentRenderer = null;
let _recentRefreshOwed = false;

async function loadRecent() {
  if (typeof _recentRenderer !== 'function') {
    _recentRefreshOwed = true;
    return;
  }
  return _recentRenderer();
}

// One-time registration hook for the canonical Dashboard Recent Items renderer.
window.__dpRegisterRecentRenderer = function registerRecentRenderer(renderer) {
  if (typeof renderer !== 'function' || _recentRenderer === renderer) return;
  _recentRenderer = renderer;
  if (_recentRefreshOwed) {
    _recentRefreshOwed = false;
    loadRecent().catch(() => {});
  }
};

function openTorrentFilePicker() {
  const input = document.getElementById('torrent-file-input');
  if (!input) {
    toast('Torrent file selector is unavailable', 'error');
    return;
  }
  input.value = '';
  input.click();
}

/* One picker. A structured transfer file's extension chooses its canonical
 * submission endpoint; any other file is offered as a list of links, which the
 * server reads by its content, never its name. The browser never learns how
 * anything is acquired. */
const TRANSFER_UPLOADS = Object.freeze({
  '.torrent': {endpoint: '/torrents/add-file', label: 'Torrent file', interactive: true},
  '.nzb':     {endpoint: '/usenet/add-file',   label: 'NZB',          interactive: false,
               // A posting's manifest is routinely tens or hundreds of
               // megabytes and is streamed straight to durable storage, so it
               // is not judged by a metafile's ceiling. This mirrors the one
               // server-side ceiling (transfers/staged_input.py).
               maxBytes: 1024 * 1024 * 1024, maxLabel: '1 GB'},
  // A Metalink file describes files and their sources: one that describes
  // several offers the file selector like any other collection.
  '.meta4':   {endpoint: '/multimeta/add-file', label: 'Metalink file', interactive: true},
});
// Mirrors the server's link file ceiling (transfers/requests.py).
const LINK_FILE_UPLOAD = Object.freeze(
  {endpoint: '/links/add-file', label: 'Link file', interactive: true, maxBytes: 1024 * 1024, maxLabel: '1 MB'});

function uploadKindFor(name) {
  const lower = String(name || '').toLowerCase();
  const key = Object.keys(TRANSFER_UPLOADS).find(ext => lower.endsWith(ext));
  return key ? TRANSFER_UPLOADS[key] : null;
}

async function uploadTransferFile(input) {
  const file = input && input.files ? input.files[0] : null;
  if (!file) return;

  const kind = uploadKindFor(file.name) || LINK_FILE_UPLOAD;
  const maxBytes = kind.maxBytes || 16 * 1024 * 1024;
  if (file.size > maxBytes) {
    toast(`${kind.label} exceeds the ${kind.maxLabel || '16 MB'} upload limit`, 'error');
    input.value = '';
    return;
  }

  const form = new FormData();
  form.append('file', file, file.name);
  // The built-in browser is an interactive client: opt every torrent/magnet
  // submission into the interactive file-selection lifecycle. Historical /
  // headless callers that omit this keep the ALL default (correction §6).
  // A posting is acquired whole, so it has no per-file selection step.
  if (kind.interactive) form.append('selection_mode', 'interactive');

  try {
    // A large posting takes longer to upload than a metafile. The allowance
    // scales with the file so a slow link is not mistaken for a failure,
    // never below the ordinary one.
    const timeout = Math.max(60000, Math.round(file.size / 128));
    let res;
    try {
      res = await api('POST', kind.endpoint, form, timeout);
    } catch (error) {
      // The same per-submission private-LAN confirmation as Quick Add: Allow
      // resubmits THIS file with consent; neither answer changes a setting.
      if (error?.detail?.confirmation === 'acquisition_scope') {
        // A file carries no per-link answers: it is declined, never guessed.
        toast('Some links in this file point to an item inside a collection. '
          + 'Add those links through Quick Add to choose what to download.', 'warn');
        return;
      }
      if (error?.detail?.confirmation !== 'local_network') throw error;
      if (!await confirmLocalNetwork(error.detail.hosts)) {
        toast('Local network transfer not added', 'info');
        return;
      }
      form.append('allow_local_network', 'true');
      res = await api('POST', kind.endpoint, form, timeout);
    }
    if (res && res._duplicate && res._duplicate.action === 'skip') {
      toast('Already in queue: ' + (res.name || res._duplicate.reason), 'warn');
    } else if (res && res._duplicate && res._duplicate.action === 'warn') {
      toast(`${kind.label} added (possible duplicate)`, 'warn');
    } else if (res && res._deferred) {
      toast(`${kind.label} added · processing is paused`, 'success');
    } else {
      toast(`${kind.label} added!`, 'success');
    }
    loadStats();
    loadRecent();
    if (document.getElementById('view-torrents').classList.contains('active')) {
      loadTorrents();
    }
  } catch(e) {
    toast(sanitizeErrorMsg(e.message), 'error');
  } finally {
    input.value = '';
  }
}

function resizeDebridLinkInput(input) {
  if (!input) return;
  const styles = window.getComputedStyle(input);
  const lineHeight = parseFloat(styles.lineHeight) || 18;
  // scrollHeight counts content and padding but not the border, while the
  // field's height is its border box: the border is added back below.
  const border = (parseFloat(styles.borderTopWidth) || 0) + (parseFloat(styles.borderBottomWidth) || 0);
  const chrome = (parseFloat(styles.paddingTop) || 0) +
    (parseFloat(styles.paddingBottom) || 0) + border;
  const minimum = Math.ceil((lineHeight * 2) + chrome);
  const maximum = Math.ceil((lineHeight * 5) + chrome);
  // An empty field is as tall as its placeholder needs: the placeholder is
  // measured as the field's own text (browsers differ on whether scrollHeight
  // counts a placeholder), so a hint that wraps on a narrow screen is shown
  // whole instead of losing its last line below the edge.
  const placeholder = !input.value && input.placeholder;
  if (placeholder) input.value = input.placeholder;
  input.style.height = `${minimum}px`;
  const needed = input.scrollHeight + border;
  if (placeholder) input.value = '';
  const target = Math.max(minimum, Math.min(needed, maximum));
  input.style.height = `${target}px`;
  input.style.overflowY = !placeholder && needed > maximum ? 'auto' : 'hidden';
}

function classifyDashboardEntries(raw) {
  const seen = new Set();
  const direct = [];
  const magnets = [];
  const invalid = [];
  const directLink = /^(?:https?|s?ftp|scp|ssh|rsync(?:\+ssh)?|(?:web)?davs?):\/\/\S+$/i;
  String(raw || '').split(/\r?\n/).forEach((rawValue, index) => {
    // A pasted row of TAB-separated links is ONE item with alternate
    // sources, kept intact for the backend; empty cells are dropped.
    const cells = [...new Set(rawValue.split('\t').map(cell => cell.trim()).filter(Boolean))];
    const value = cells.join('\t');
    if (!value || seen.has(value)) return;
    seen.add(value);
    const entry = {value, line: index + 1};
    // Direct-source transports; the backend admission owner remains the authority.
    if (cells.every(cell => directLink.test(cell))) direct.push(entry);
    else if (cells.length === 1 && /^magnet:\?/i.test(value)) magnets.push(entry);
    else invalid.push(entry);
  });
  return {direct, magnets, invalid};
}

async function mapWithConcurrency(items, concurrency, worker) {
  const results = new Array(items.length);
  let cursor = 0;
  async function run() {
    while (cursor < items.length) {
      const index = cursor++;
      try {
        results[index] = {ok: true, value: await worker(items[index])};
      } catch (error) {
        results[index] = {ok: false, error};
      }
    }
  }
  const workers = Array.from(
    {length: Math.min(Math.max(1, concurrency), Math.max(1, items.length))},
    () => run()
  );
  await Promise.all(workers);
  return results;
}

async function addDashboardEntries() {
  const input = document.getElementById('q-transfer-input');
  const button = document.getElementById('btn-add-transfer');
  const raw = input?.value || '';
  if (!raw.trim()) {
    openTorrentFilePicker();
    return;
  }

  const {direct, magnets, invalid} = classifyDashboardEntries(raw);
  if (invalid.length) {
    const first = invalid[0];
    toast(`Line ${first.line}: enter an HTTP(S), FTP, SFTP, SCP, SSH, rsync or WebDAV link or a magnet URI`, 'error');
    input?.focus();
    return;
  }
  if (!direct.length && !magnets.length) {
    toast('Enter at least one HTTP(S), FTP, SFTP, SCP, SSH, rsync or WebDAV link or a magnet URI', 'warn');
    input?.focus();
    return;
  }

  setButtonPending(button, true, 'Adding…');
  const failed = [];
  const kept = [];
  let handled = 0;
  let deferred = 0;
  let scopeKept = 0;
  try {
    if (direct.length) {
      try {
        // The built-in browser is an interactive client: a source that proves
        // to be a multi-file collection offers the file selector; a single
        // file is unaffected. Headless callers that omit this keep ALL.
        const submission = {links: direct.map(entry => entry.value), selection_mode: 'interactive'};
        const scopes = {};
        let result;
        for (let asked = 0; direct.length && asked < 3; asked += 1) {
          try {
            submission.links = direct.map(entry => entry.value);
            const answered = Object.fromEntries(Object.entries(scopes)
              .filter(([link]) => direct.some(entry => entry.value.split('\t').includes(link))));
            if (Object.keys(answered).length) submission.acquisition_scopes = answered;
            else delete submission.acquisition_scopes;
            result = await api('POST', '/links/add', submission, 30000);
            break;
          } catch (error) {
            const confirmation = error?.detail?.confirmation;
            if (confirmation === 'acquisition_scope') {
              // Nothing was admitted: each link naming an item inside its
              // collection is asked about on its own, one at a time, in input
              // order. A cancelled link is kept in the input; every other
              // line is still submitted. No answer is ever a default.
              const cells = direct.flatMap(entry => entry.value.split('\t').map(cell => ({entry, cell})));
              const cancelled = new Set();
              for (const ask of Array.isArray(error.detail.links) ? error.detail.links : []) {
                const target = cells[Number(ask.index)];
                if (!target) throw error;
                if (cancelled.has(target.entry)) continue;
                const scope = await chooseAcquisitionScope(target.cell, ask.choices);
                if (scope) scopes[target.cell] = scope;
                else cancelled.add(target.entry);
              }
              if (cancelled.size) {
                kept.push(...cancelled);
                scopeKept += cancelled.size;
                const remaining = direct.filter(entry => !cancelled.has(entry));
                direct.length = 0;
                direct.push(...remaining);
              }
              continue;
            }
            // A private-LAN destination with confirmation required: nothing was
            // admitted. Allow applies to THIS submission only; neither answer
            // changes a setting (Skip Local Connection Confirmation is the one
            // persistent owner of "do not ask").
            if (confirmation !== 'local_network') throw error;
            if (!await confirmLocalNetwork(error.detail.hosts)) {
              kept.push(...direct);
              direct.length = 0;
            } else {
              submission.allow_local_network = true;
            }
          }
        }
        if (direct.length && !result) throw new Error('The links could not be submitted');
        if (direct.length) {
          handled += direct.length;
          if (result && result._deferred) deferred += direct.length;
        }
      } catch (error) {
        direct.forEach(entry => failed.push({...entry, error}));
      }
    }

    if (magnets.length) {
      const results = await mapWithConcurrency(
        magnets,
        3,
        entry => api('POST', '/torrents/add-magnet', {magnet: entry.value, selection_mode: 'interactive'}, 30000)
      );
      results.forEach((result, index) => {
        if (result.ok) {
          handled += 1;
          if (result.value && result.value._deferred) deferred += 1;
        } else failed.push({...magnets[index], error: result.error});
      });
    }

    const remaining = [...failed, ...kept].sort((a, b) => a.line - b.line);
    input.value = remaining.map(entry => entry.value).join('\n');
    resizeDebridLinkInput(input);
    input.focus();

    if (failed.length) {
      const failureMessages = [...new Set(
        failed.map(entry => String(entry.error?.message || 'Request failed'))
      )];
      if (!handled && failureMessages.length === 1) {
        toast(sanitizeErrorMsg(failureMessages[0]), 'error');
      } else {
        toast(`${handled} handled · ${failed.length} failed`, handled ? 'warn' : 'error');
      }
    } else if (!handled && kept.length) {
      toast(scopeKept === kept.length ? 'Link not added' : 'Local network transfer not added', 'info');
    } else if (handled && deferred === handled) {
      toast(`${handled} added · processing is paused`, 'success');
    } else if (deferred) {
      toast(`${handled} handled · ${deferred} waiting for Resume All`, 'success');
    } else {
      toast(`${handled} item${handled === 1 ? '' : 's'} submitted`, 'success');
    }

    if (handled) {
      loadStats();
      loadRecent();
      if (document.getElementById('view-torrents')?.classList.contains('active')) {
        loadTorrents();
      }
    }
  } finally {
    setButtonPending(button, false);
  }
}

// Prevent SSE bursts and manual actions from stacking duplicate full renders.
// loadTorrents is the Downloads owner's own self-wrap (ui-downloads.js).
loadStats = coalesceAsync(loadStats);
loadRecent = coalesceAsync(loadRecent);

/* Recover All: a broad, provider- and executor-neutral recovery effort.
 *
 * It asks the backend to examine everything currently non-terminal and let the
 * canonical recovery owner make whatever progress is legal right now. It is
 * not an override -- every policy still holds -- so what it reports is what
 * the canonical owners actually DID, never how much work exists.
 *
 * Three outcomes, and only three: nothing was legal to do, something was
 * applied, or something failed. */
function recoveryOutcomeSummary(result) {
  const parts = [];
  const recovered = Number(result?.recovered || 0);
  const imported = Number(result?.imported || 0);
  if (recovered > 0) parts.push(`${recovered} recovery action${recovered === 1 ? '' : 's'} applied`);
  if (imported > 0) parts.push(`${imported} transfer${imported === 1 ? '' : 's'} imported`);
  return parts.join(' · ');
}

async function recoverAll(button) {
  setButtonPending(button, true, 'Recovering…');

  try {
    toast(
      'Checking transfers for recoverable work…',
      'info'
    );

    const r =
      await api('POST','/torrents/recover-all');

    const errors = Array.isArray(r.errors) ? r.errors.length : 0;
    const summary = recoveryOutcomeSummary(r);

    if (errors > 0) {
      toast(`Recovery completed with ${errors} error(s)`, 'error');
    } else if (summary) {
      toast(`Recovery complete · ${summary}`, 'success');
    } else {
      toast('Recovery check complete · no recoverable work found', 'warn');
    }

    loadStats();
    loadRecent();

    if (
      document
        .getElementById('view-torrents')
        .classList.contains('active')
    ) {
      loadTorrents();
    }
  } catch(e) {
    toast(
      sanitizeErrorMsg(e.message),
      'error'
    );
  } finally {
    setButtonPending(button, false);
  }
}

async function pauseT(id, button) {
  setButtonPending(button, true, 'Pausing…');

  try {
    await api('POST',`/torrents/${id}/pause`);
    toast('Transfer paused','warn');
    loadTorrents();
    loadStats();
    loadRecent();
  } catch(e) {
    toast(sanitizeErrorMsg(e.message),'error');
  } finally {
    setButtonPending(button, false);
  }
}

async function resumeT(id, button) {
  setButtonPending(button, true, 'Resuming…');

  try {
    const result =
      await api('POST',`/torrents/${id}/resume`);

    if (typeof result.paused === 'boolean') {
      processingPaused = result.paused;

      renderTopbarActions();
    }

    toast('Transfer resumed','success');
    loadTorrents();
    loadStats();
    loadRecent();
  } catch(e) {
    toast(sanitizeErrorMsg(e.message),'error');
  } finally {
    setButtonPending(button, false);
  }
}

// ── Shared modal coordinator ───────────────────────────────────────────────
// app.js is the single owner of the shared #overlay/#modal shell. Bounded
// presentation owners (Details candidates, file selection) drive it through
// this contract and never wrap window.closeModal or window.showDetail.
// `contain: true` declares a blocking dialog: focus moves onto the dialog when
// it opens and Tab / Shift+Tab stay inside it until it closes.
const DPModal = (function () {
  const FOCUSABLE = 'button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])';
  let activeMode = null;            // 'details' | 'file-selection' | null
  let onCloseCallback = null;
  let focusReturnTarget = null;
  let containFocus = false;
  let exits = 0;                    // the exit in progress; a reopening supersedes it

  function containTab(event) {
    const modal = document.getElementById('modal');
    if (!containFocus || event.key !== 'Tab' || !modal) return;
    const controls = Array.from(modal.querySelectorAll(FOCUSABLE))
      .filter(control => !control.disabled && control.getClientRects().length > 0);
    if (!controls.length) { event.preventDefault(); modal.focus(); return; }
    const first = controls[0];
    const last = controls[controls.length - 1];
    const active = document.activeElement;
    if (!modal.contains(active) || active === modal) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    } else if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function open(options) {
    const opts = options || {};
    const overlay = document.getElementById('overlay');
    const modal = document.getElementById('modal');
    const titleEl = document.getElementById('modal-title');
    const footer = document.getElementById('modal-footer');
    const closeBtn = modal ? modal.querySelector('.modal-close') : null;

    activeMode = opts.mode || 'details';
    onCloseCallback = typeof opts.onClose === 'function' ? opts.onClose : null;
    // Opened while a dialog is still fading out, it inherits that one's return target.
    focusReturnTarget = opts.focusReturn || (window.DPMotion ? window.DPMotion.focusOrigin()
      : document.activeElement instanceof HTMLElement ? document.activeElement : null);
    exits += 1;                     // a closing shell reopened: it fades back from where it is
    if (overlay) overlay.classList.remove('is-closing');
    if (modal) {
      modal.inert = false;
      modal.style.height = '';      // a held shell (reveal({hold})) is released with a new opening
      // `pending`: the shell waits, unseen, for its content (reveal()).
      if (opts.pending === true) modal.dataset.dpModalPending = '';
      else delete modal.dataset.dpModalPending;
    }

    if (overlay) { overlay.classList.add('open'); overlay.dataset.dpModalMode = activeMode; }
    if (modal) modal.dataset.dpModalMode = activeMode;
    if (titleEl && opts.title != null) titleEl.textContent = opts.title;
    if (closeBtn) {
      const label = opts.closeLabel || 'Close details';
      closeBtn.setAttribute('aria-label', label);
      closeBtn.dataset.tooltip = label;
    }
    if (footer) { footer.hidden = true; footer.innerHTML = ''; }
    containFocus = opts.contain === true && Boolean(modal);
    if (containFocus) {
      modal.tabIndex = -1;
      document.addEventListener('keydown', containTab);
      modal.focus({preventScroll: true});
    } else {
      document.removeEventListener('keydown', containTab);
      if (modal) modal.removeAttribute('tabindex');
    }
    return activeMode;
  }

  function requestModalClose(reason) {
    const callback = onCloseCallback;
    if (callback && callback(reason) === false) return false;   // owner vetoed
    finishClose(reason);
    return true;
  }

  function finishClose(reason) {
    const overlay = document.getElementById('overlay');
    const modal = document.getElementById('modal');
    const footer = document.getElementById('modal-footer');
    const closedMode = activeMode;
    if (overlay && !overlay.classList.contains('open')) return;   // already closed (or closing)

    if (overlay) { overlay.classList.remove('open'); delete overlay.dataset.dpModalMode; }
    if (modal) delete modal.dataset.dpModalMode;
    activeMode = null;
    onCloseCallback = null;
    const returnTarget = focusReturnTarget;
    focusReturnTarget = null;
    // The shell fades away in place, still covering the page and holding the
    // keyboard; only once it is gone is it put away, Tab containment released
    // and focus returned, once -- unless it was opened again meanwhile.
    const exit = ++exits;
    const leaving = window.DPMotion ? window.DPMotion.leave(overlay, modal, returnTarget) : Promise.resolve();
    leaving.then(() => {
      if (exit !== exits) return;
      if (overlay) overlay.classList.remove('is-closing');
      if (modal) { modal.inert = false; delete modal.dataset.dpModalPending; modal.style.height = ''; }
      if (footer) { footer.hidden = true; footer.innerHTML = ''; }
      if (containFocus) {
        containFocus = false;
        document.removeEventListener('keydown', containTab);
        if (modal) modal.removeAttribute('tabindex');
      }
      if (window.DPMotion) window.DPMotion.restore(overlay, returnTarget, {remove: false});
      else if (returnTarget && typeof returnTarget.focus === 'function') {
        try { returnTarget.focus({preventScroll: true}); } catch (_) {}
      }
    });

    if (closedMode === 'details') {
      document.dispatchEvent(new CustomEvent('debridpulse:detail-closed',
        {detail: {reason: reason || null}}));
    }
  }

  // The pending shell's content has arrived (or is slow): it fades in now.
  // `hold`: shown before its content, it takes its full height -- the shell's
  // own max-height -- and keeps it, so the content arriving never resizes it.
  function reveal({hold = false} = {}) {
    const modal = document.getElementById('modal');
    if (!modal || !('dpModalPending' in modal.dataset)) return;
    if (hold) modal.style.height = getComputedStyle(modal).maxHeight;
    delete modal.dataset.dpModalPending;
  }

  return {
    open,
    reveal,
    requestModalClose,
    finishClose,
    footer: function () { return document.getElementById('modal-footer'); },
    get mode() { return activeMode; },
  };
})();
window.DPModal = DPModal;

// ── Detail Modal ───────────────────────────────────────────────────────────
// Original Resource: the backend's complete safe URL, shown on hover or focus,
// and -- only when the backend allows it -- Copy URL in the application's one
// menu, which copies exactly that value. The menu acts only on the rendering
// it was opened from: any other rendering or a closed Details leaves nothing
// to copy.
let dpDetailResource = null;
function dpOriginalResourceMenu(event) {
  const snapshot = dpDetailResource;
  const node = event.target.closest?.('.dp-detail-resource');
  if (!snapshot || !node || node !== snapshot.node || !node.isConnected) return false;
  return dpOpenContextMenu(event, node, {
    label: 'Original Resource actions',
    items: [{text: 'Copy URL', run: () => dpCopyOriginalResource(snapshot)}],
    restore: () => { if (snapshot.node.isConnected) snapshot.node.focus({preventScroll: true}); },
  });
}
async function dpCopyOriginalResource(snapshot) {
  if (dpDetailResource !== snapshot || !snapshot.node.isConnected) {
    toast('Details changed. Nothing was copied.', 'warn');
    return;
  }
  try {
    if (!navigator.clipboard?.writeText) throw new Error('Clipboard API unavailable');
    await navigator.clipboard.writeText(snapshot.url);
  } catch (_) {
    toast('Could not copy the URL.', 'error');
    return;
  }
  toast('URL copied.', 'success');
}
function dpRenderOriginalResource(modalBody, t) {
  const full = typeof t.original_resource_sanitized_full === 'string' ? t.original_resource_sanitized_full : '';
  const field = modalBody?.querySelector('.dp-detail-original-resource .dv');
  if (!field) return;
  if (!full) { field.textContent = t.original_resource || '—'; return; }
  // The compact value; the complete safe URL is its tooltip (ui-tooltip.js:
  // on hover or keyboard focus, bounded and scrolling however long the URL,
  // hidden by Escape or by opening its Copy URL menu).
  const value = document.createElement('span');
  value.className = 'dp-detail-resource';
  value.tabIndex = 0;
  value.dataset.tooltip = full;
  value.textContent = t.original_resource || full;
  field.replaceChildren(value);
  if (t.original_resource_copy_allowed === true) dpDetailResource = {url: full, node: value};
}
document.addEventListener('debridpulse:detail-closed', () => { dpDetailResource = null; });

/* Details' ERROR: the message as it always was, and -- when the backend
 * discloses facts of that very error (error_disclosure, bound to it by the
 * Details route) -- the same message as a collapsed disclosure revealing
 * them. Every value is text; nothing here interprets a provider or an error. */
const DP_ERROR_FACTS = Object.freeze([
  ['integration_id', 'Reported by'], ['origin', 'Origin'], ['domain', 'Domain'], ['diagnostic', 'Diagnostic'],
  ['native_code', 'Native code'], ['stage', 'Stage'], ['permanence', 'Permanence'], ['retryability', 'Retry'],
]);
// Classifications read as words; a diagnostic, native code or identifier is shown exactly.
const DP_ERROR_VERBATIM = new Set(['diagnostic', 'native_code', 'integration_id']);
function dpRenderErrorDisclosure(modalBody, t) {
  const field = modalBody?.querySelector('.dp-detail-error .dv');
  const facts = t.error_disclosure && typeof t.error_disclosure === 'object' ? t.error_disclosure : null;
  if (!field || !facts) return;
  const rows = [];
  for (const [key, label] of DP_ERROR_FACTS) {
    const raw = facts[key];
    if (typeof raw !== 'string' || !raw) continue;
    let value = DP_ERROR_VERBATIM.has(key) ? raw : raw.replace(/_/g, ' ');
    if (key === 'integration_id' && typeof facts.integration_name === 'string' && facts.integration_name && facts.integration_name !== raw) {
      value = `${facts.integration_name} (${raw})`;
    }
    const term = document.createElement('dt');
    term.textContent = label;
    const detail = document.createElement('dd');
    detail.textContent = value;
    rows.push(term, detail);
  }
  if (!rows.length) return;
  const disclosure = document.createElement('details');
  disclosure.className = 'dp-detail-error-disclosure';
  const summary = document.createElement('summary');
  summary.textContent = field.textContent;
  const list = document.createElement('dl');
  list.append(...rows);
  disclosure.append(summary, list);
  field.replaceChildren(disclosure);
}

/* Details opens its backdrop at once and its shell with its content: the
 * shell stays unseen until the transfer is rendered into it, so it never
 * appears small and then grows. Only an answer slower than
 * DETAIL_LOADING_GRACE_MS shows the loading line first -- inside the shell at
 * its full height, which the content then fills without resizing it.
 * Each opening is its own: an answer for an earlier one, or one arriving after
 * the shell was closed or given to another view, is never shown. */
const DETAIL_LOADING_GRACE_MS = 300;
let dpDetailOpening = 0;
async function showDetail(id) {
  const modalTitle = document.getElementById('modal-title');
  const modalBody = document.getElementById('modal-body');
  dpDetailResource = null;
  dpCloseContextMenu(false);
  const opening = ++dpDetailOpening;
  const current = () => opening === dpDetailOpening && DPModal.mode === 'details';

  DPModal.open({mode: 'details', title: 'Loading…', closeLabel: 'Close details', pending: true});
  const slow = setTimeout(() => { if (current()) DPModal.reveal({hold: true}); }, DETAIL_LOADING_GRACE_MS);
  const traceButton = document.querySelector('#modal .dp-detail-trace');
  if (traceButton) traceButton.dataset.dpTransferId = String(id);

  if (modalBody) {
    modalBody.innerHTML =
      '<div class="empty" style="padding:24px">Loading transfer details…</div>';
  }

  try {
    const t = await api('GET',`/torrents/${id}`);
    if (!current()) return;

    if (modalTitle) {
      modalTitle.textContent =
        t.display_name || t.name || 'Torrent Details';
    }

    const providerPresentation = transferProviderPresentation(t);
    // The file-selection affordance (DP 1.0.12 Workstream B) can be valid
    // before any artifact exists yet (manifest-pending window, §6.5), so the
    // Files-section header must be able to render even when t.files is
    // still empty -- gated on the durable torrent/magnet source-kind fact,
    // never on filename shape or provider identity.
    const dpMayHaveFileSelection = ['magnet', 'torrent_file'].includes(
      String(t.current_source_identity && t.current_source_identity.kind || '').toLowerCase());
    // Which collection the Files card shows is decided by its one row owner:
    // the canonical-object presentation when the backend projects it, the
    // physical collection otherwise. The title and the rows read the same list.
    const dpDisplayFiles = window.DPDetailCandidates.displayRows(t);
    const dpShowFilesCard = Boolean(dpDisplayFiles.length || dpMayHaveFileSelection);
    if (modalBody) modalBody.innerHTML = `
      <div class="detail-grid">
        ${dpDetailFactCells(t, providerPresentation)}
        <div class="dp-detail-original-resource" style="grid-column:${t.requested_by ? '1' : '1/-1'}"><div class="dk">Original Resource</div><div class="dv"></div></div>
        ${dpRequestedByMarkup(t.requested_by)}
        <div style="grid-column:1/-1"><div class="dk">Transfer ID</div><div class="dv">${t.id}</div></div>
        <div style="grid-column:1/-1"><div class="dk">Hash</div><div class="dv" style="font-size:11px">${esc(t.hash||'—')}</div></div>
        ${t.local_path?`<div style="grid-column:1/-1"><div class="dk">Local Path</div><div class="dv" style="font-size:11px">${esc(t.local_path)}</div></div>`:''}
        ${t.error_message?`<div class="dp-detail-error" style="grid-column:1/-1"><div class="dk">Error</div><div class="dv" style="color:var(--red)">${esc(t.error_message)}</div></div>`:''}
        ${t.extraction_status?`<div><div class="dk">Extraction</div><div class="dv">${esc(t.extraction_status)}</div></div>`:''}
        ${t.extraction_error?`<div style="grid-column:1/-1"><div class="dk">Extraction Error</div><div class="dv" style="color:var(--red)">${esc(t.extraction_error)}</div></div>`:''}
      </div>
      <div class="card dp-detail-section-card dp-detail-route-history">
        <div class="card-header"><span class="card-title">Route History</span></div>
        <div class="dp-detail-route-body">${renderRouteHistory(t)}</div>
      </div>
      <details class="dp-detail-advanced">
        <summary>Advanced acquisition details</summary>
        <div class="dp-detail-advanced-grid">
          <div><span>Executor</span><strong>${esc((t.executors || []).join(', ') || '—')}</strong></div>
          <div><span>Origin Provider ID</span><strong>${esc(t.origin_provider_id || '—')}</strong></div>
          <div><span>Current Provider ID</span><strong>${esc(t.current_provider_id || '—')}</strong></div>
          <div><span>Delivering Provider ID</span><strong>${esc(t.delivering_provider_id || '—')}</strong></div>
        </div>
      </details>
      ${dpShowFilesCard?`
        <div class="card dp-detail-section-card dp-detail-files-card">
          <div class="card-header dp-detail-files-header">
            <span class="card-title">Files${dpDisplayFiles.length?` (${dpDisplayFiles.length})`:''}</span>
            <span class="dp-detail-files-header-actions">
              <span class="dp-detail-files-provider-slot" data-dp-root-provider-mount data-dp-transfer-id="${t.id}"></span>
              <span class="dp-detail-files-group-slot" data-dp-group-candidates-mount data-dp-transfer-id="${t.id}"></span>
              <span class="dp-detail-files-selection-slot" data-dp-file-selection-mount data-dp-transfer-id="${t.id}"></span>
            </span>
          </div>
          ${dpDisplayFiles.length?`
          <div class="dp-detail-table-wrap">
            <table class="t-table">
              <thead><tr><th>Filename</th><th>Size</th><th>Status</th></tr></thead>
              <tbody>${window.DPDetailCandidates.rowsMarkup(dpDisplayFiles, t)}</tbody>
            </table>
          </div>
          `:''}
        </div>
      `:''}
      ${t.source_outcomes && t.source_outcomes.length ? `
        <div class="card dp-detail-section-card">
          <div class="card-header"><span class="card-title">Source Warnings (${t.source_outcomes.length})</span></div>
          <div class="dp-detail-table-wrap"><table class="t-table"><tbody>
            ${t.source_outcomes.map(source => `<tr><td>${esc(source.name)}</td><td>${badge('error', source)}</td></tr>`).join('')}
          </tbody></table></div>
        </div>
      ` : ''}
      ${t.events&&t.events.length?`
        <div class="card dp-detail-section-card dp-detail-events-card">
          <div class="card-header">
            <span class="card-title">Events</span>
          </div>
          <div class="dp-detail-events-list">
            ${t.events.map(ev=>`
              <div class="event-item">
                <div class="elevel ${esc(ev.level)}"></div>
                <div class="emsg">${esc(ev.message)}</div>
                <div class="etime">${window.downloadsDateMarkup(ev.created_at)}</div>
              </div>`).join('')}
          </div>
        </div>
      `:''}
    `;
    dpRenderOriginalResource(modalBody, t);
    dpRenderErrorDisclosure(modalBody, t);
    dpRenderWithdrawal(t);

    document.dispatchEvent(new CustomEvent('debridpulse:detail-rendered',
      {detail: {transferId: Number(id), transfer: t}}));
  } catch(e) {
    if (!current()) return;
    if (modalBody) {
      modalBody.innerHTML =
        `<div class="empty" style="padding:24px">Failed to load details: ${esc(sanitizeErrorMsg(e.message))}</div>`;
    }

    toast(sanitizeErrorMsg(e.message),'error');
    document.dispatchEvent(new CustomEvent('debridpulse:detail-rendered',
      {detail: {transferId: Number(id), transfer: null, error: true}}));
  } finally {
    clearTimeout(slow);
    if (current()) DPModal.reveal();
  }
}

// Requested By: the requests this transfer's work serves, exactly as the
// server's demand ledger projects them (requested_by) -- one entry per
// request, oldest first, withdrawn ones in place -- as one field of Details'
// metadata grid beside Original Resource. It says who ASKED; it is not who
// owns or runs the transfer. The earliest request is always shown; every
// later one waits behind Details' disclosure chevron, which counts them.
// The facts above Original Resource, in two columns read row by row: the
// left column Status, Progress, Submitted As, Completed; the right column
// Provider, Size, Added -- and, for a Usenet transfer, the servers its own
// execution evidence names, second, beneath Provider.
function dpDetailFactCells(t, providerPresentation) {
  const cell = (label, value, cls) => `<div${cls ? ` class="${cls}"` : ''}><div class="dk">${label}</div><div class="dv">${value}</div></div>`;
  const status = cell('Status', badge(transferDisplayStatus(t), t));
  const provider = cell('Provider', esc(providerPresentation.label), 'dp-detail-provider');
  const progress = cell('Progress', dpDetailProgress(t));
  const size = cell('Size', fmtSize(t.size_bytes));
  const submitted = cell('Submitted As', sourceLabel(t.source, t.request_kinds));
  const added = cell('Added', window.downloadsDateMarkup(t.created_at));
  const completed = cell('Completed', window.downloadsDateMarkup(t.completed_at));
  const servers = dpUsenetServersCell(t.usenet_servers);
  return (servers
    ? [status, provider, progress, servers, submitted, size, completed, added]
    : [status, provider, progress, size, submitted, added, completed]).join('');
}
// Recorded evidence only: a server the job's own report names, never the
// configured list. Unknown is said, not guessed.
function dpUsenetServersCell(evidence) {
  if (!evidence || typeof evidence !== 'object') return '';
  const servers = Array.isArray(evidence.servers) ? evidence.servers : [];
  const label = servers.length > 1 ? 'Usenet Servers' : 'Usenet Server';
  let value;
  if (evidence.state === 'known' && servers.length) {
    value = servers.map(server => {
      const notes = [];
      if (server.reported) notes.push('about ' + server.reported + ' of articles delivered');
      if (!server.attributed) notes.push('reported by name only; not matched to one configured server');
      if (server.removed) notes.push('no longer configured');
      const name = esc(server.name || server.recorded_name || 'Unnamed server') + (server.removed ? ' (removed)' : '');
      return `<span class="dp-detail-usenet-server"${notes.length ? ` data-tooltip="${esc(notes.join('; '))}"` : ''}>${name}</span>`;
    }).join(', ');
  } else if (evidence.state === 'none') {
    value = '<span class="dp-detail-usenet-server is-unknown" data-tooltip="The job\'s report names no server that delivered articles">None reported</span>';
  } else {
    value = '<span class="dp-detail-usenet-server is-unknown" data-tooltip="No server report has been recorded for this transfer">Not available</span>';
  }
  return `<div class="dp-detail-usenet-servers"><div class="dk">${label}</div><div class="dv">${value}</div></div>`;
}
function dpRequestedByMarkup(requested) {
  if (!requested) return '';
  const standing = {disabled: 'Disabled', deleted: 'Deleted', merged: 'Merged'};
  const entry = item => {
    const facts = [];
    if (item.kind === 'integration') facts.push(item.token_label ? `Integration · token “${esc(item.token_label)}”` : 'Integration');
    if (standing[item.standing]) facts.push(`${standing[item.standing]} account`);
    if (item.via_transfer_id != null) facts.push(`via consolidated transfer #${esc(item.via_transfer_id)}`);
    facts.push(`requested ${window.downloadsDateMarkup(item.requested_at)}`);
    if (item.withdrawn_at) facts.push(`withdrawn ${window.downloadsDateMarkup(item.withdrawn_at)}`);
    return `<li class="dp-detail-requester${item.withdrawn_at ? ' is-withdrawn' : ''}">
        <span class="dp-detail-requester-name">${esc(item.name)}${item.reference ? ` <span class="dp-detail-requester-ref">#${esc(item.reference)}</span>` : ''}</span>
        <span class="dp-detail-requester-facts">${facts.join(' · ')}</span>
      </li>`;
  };
  const requests = Array.isArray(requested.requests) ? requested.requests : [];
  const [first, ...later] = requests;
  const withdrawn = later.filter(item => item.withdrawn_at).length;
  const summary = `${later.length} more request${later.length === 1 ? '' : 's'}${withdrawn ? ` (${withdrawn} withdrawn)` : ''}`;
  return `
        <div class="dp-detail-requested-by"><div class="dk">Requested By</div><div class="dv">
          ${first
            ? `<ul class="dp-detail-requesters">${entry(first)}</ul>`
            : '<div class="dp-detail-requesters-empty">No requests recorded.</div>'}
          ${later.length ? `
          <details class="dp-detail-requesters-more">
            <summary>${summary}</summary>
            <ul class="dp-detail-requesters">${later.map(entry).join('')}</ul>
          </details>` : ''}
        </div></div>`;
}

// The caller's own outstanding requests that this transfer serves (the
// server's access projection, directly or through consolidation) can be
// withdrawn here, in the Requested By field that names them. An Admin already
// withdraws any account's request from Users & Access, so this is the
// non-administering account's control. The server decides what withdrawal
// stops; this only asks.
function dpRenderWithdrawal(t) {
  const field = document.querySelector('#modal-body .dp-detail-requested-by > .dv');
  const own = Array.isArray(t.access?.own_requests) ? t.access.own_requests : [];
  if (!field || !own.length || dpCan('administer')) return;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn btn-ghost btn-sm dp-detail-withdraw';
  button.textContent = own.length > 1 ? 'Withdraw My Requests' : 'Withdraw My Request';
  button.addEventListener('click', () => dpWithdrawOwnRequests(t, own, button));
  field.appendChild(button);
}

async function dpWithdrawOwnRequests(t, own, button) {
  const confirmed = await window.DPSettingsModal.confirm({
    title: own.length > 1 ? 'Withdraw your requests?' : 'Withdraw your request?',
    message: 'This transfer stops only if no other outstanding request still needs it. '
      + 'Completed downloads and history are kept.',
    confirmLabel: 'Withdraw',
  });
  if (!confirmed) return;
  setButtonPending(button, true, 'Withdrawing…');
  // Each request's outcome is what the server answered for it: withdrawn now,
  // already withdrawn, refused (with the server's own reason), or unknown when
  // no answer arrived. Nothing is inferred beyond that.
  const outcome = {withdrawn: 0, already: 0, refused: [], unknown: 0};
  for (const demand of own) {
    try {
      const result = await api('POST', `/requests/${encodeURIComponent(demand)}/withdraw`);
      if (result && result.already_withdrawn) outcome.already += 1; else outcome.withdrawn += 1;
    } catch (error) {
      if (error && error.status) outcome.refused.push(sanitizeErrorMsg(error.message));
      else outcome.unknown += 1;
    }
  }
  const parts = [];
  if (outcome.withdrawn) parts.push(`${outcome.withdrawn} withdrawn`);
  if (outcome.already) parts.push(`${outcome.already} already withdrawn`);
  if (outcome.refused.length) parts.push(`${outcome.refused.length} refused (${[...new Set(outcome.refused)].join('; ')})`);
  if (outcome.unknown) parts.push(`${outcome.unknown} not confirmed — no response; check this transfer again`);
  const failed = outcome.refused.length + outcome.unknown;
  const message = own.length > 1 ? `Requests: ${parts.join(', ')}.`
    : outcome.withdrawn ? 'Request withdrawn.'
    : outcome.already ? 'This request was already withdrawn.'
    : outcome.refused.length ? `Request not withdrawn: ${outcome.refused[0]}`
    : 'Withdrawal not confirmed: no response from the server. Check this transfer again.';
  toast(message, !failed ? 'success' : (outcome.withdrawn || outcome.already ? 'warning' : 'error'));
  // Whatever happened, Details shows the server's state now.
  if (DPModal.mode === 'details') showDetail(t.id);
  else setButtonPending(button, false);
}

// Transfer Trace Log: the backend builds and sanitizes the trace
// (GET /torrents/{id}/trace); this only hands the returned file to the browser.
async function downloadTransferTrace(button) {
  const id = button && button.dataset.dpTransferId;
  if (!id) return;
  setButtonPending(button, true, 'Generating…');
  try {
    const r = await window.debridPulseAuth.fetch(`${API}/torrents/${encodeURIComponent(id)}/trace`);
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      // The same reading of a refusal as api(): a structured detail speaks
      // through its message, never as "[object Object]".
      const detail = data.detail;
      throw new Error((detail && typeof detail === 'object' ? detail.message : detail)
        || 'The trace log could not be downloaded.');
    }
    const named = /filename="([^"]+)"/.exec(r.headers.get('Content-Disposition') || '');
    const url = URL.createObjectURL(await r.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = named ? named[1] : `debridpulse-transfer-${id}-trace.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  } catch(e) {
    toast(sanitizeErrorMsg(e.message),'error');
  } finally {
    setButtonPending(button, false);
  }
}
window.downloadTransferTrace = downloadTransferTrace;

// Thin global entry retained for the inline #overlay / close-button handlers.
// The shared modal coordinator owns the actual close decision and lifecycle
// events; this is not a wrapper layer.
function closeModal(e) {
  if (e && e.target !== document.getElementById('overlay')) return;
  DPModal.requestModalClose(e ? 'backdrop' : 'button');
}
window.closeModal = closeModal;
window.showDetail = showDetail;

// ── Theme toggle ─────────────────────────────────────────────────────────────
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('mobile-overlay').classList.toggle('open');
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('mobile-overlay').classList.remove('open');
}

function toggleTheme() {
  const isLight = document.body.classList.toggle('light');
  localStorage.setItem('theme', isLight ? 'light' : 'dark');
  updateThemeToggle(isLight);
  document.dispatchEvent(new CustomEvent('debridpulse:theme-changed', {detail:{light:isLight}}));
}

function updateThemeToggle(isLight) {
  const btn = document.getElementById('theme-toggle');
  if (!btn) return;
  if (!window.DPIcons || typeof window.DPIcons.renderThemeGlyph !== 'function') {
    throw new Error('DebridPulse icon runtime is unavailable');
  }
  const action = isLight ? 'Switch to dark mode' : 'Switch to light mode';
  btn.dataset.tooltip = action;
  btn.setAttribute('aria-label', action);
  window.DPIcons.renderThemeGlyph(!!isLight);
}
document.addEventListener('DOMContentLoaded', () => {
  loadRuntimeSpeed().catch(()=>{});
  loadRuntimeStatus().catch(()=>{});
  setInterval(function() {
    loadRuntimeSpeed().catch(()=>{});
  }, RUNTIME_SPEED_INTERVAL_MS);
  setInterval(function() {
    loadRuntimeStatus().catch(()=>{});
  }, RUNTIME_STATUS_INTERVAL_MS);
  document.addEventListener('click', function(event) {
    if (!event.target.closest('.runtime-cap-control')) closeSpeedCapMenu();
  });
  document.addEventListener('keydown', function(event) {
    if (event.key === 'Escape') closeSpeedCapMenu();
  });
  window.addEventListener('resize', function() {
    clearTimeout(_dashboardRecentResizeTimer);
    _dashboardRecentResizeTimer = setTimeout(function() {
      if (!document.getElementById('view-dashboard')?.classList.contains('active')) return;
      const fittedLimit = dashboardRecentLimit();
      if (fittedLimit !== _dashboardRecentFitLimit) {
        _dashboardRecentFitLimit = fittedLimit;
        loadRecent().catch(() => {});
      }
    }, 120);
  });
  _dashboardSparkReducedMotion.addEventListener('change', syncDashboardSparkPulses);
  document.addEventListener('visibilitychange', syncDashboardSparkPulses);
  new ResizeObserver(remeasureDashboardHeartbeat).observe(document.getElementById('dash-hero'));
  // Quick Add sizes to its text, or to its placeholder while empty, and both
  // wrap with the field's width: size it once now and again whenever that
  // width changes (a resize, or the Dashboard first being shown). Its own
  // height changes are ignored, so sizing never re-triggers itself.
  const quickAdd = document.getElementById('q-transfer-input');
  if (quickAdd) {
    let quickAddWidth = -1;
    new ResizeObserver(() => {
      const width = quickAdd.clientWidth;
      if (width === quickAddWidth) return;
      quickAddWidth = width;
      resizeDebridLinkInput(quickAdd);
    }).observe(quickAdd);
  }
  // Returning to the Dashboard: the route is read again from the settled cards
  // (the heartbeat clock and the beats on the strip are untouched).
  document.addEventListener('debridpulse:navigation-settled', function(event) {
    if (event.detail?.view === 'dashboard') remeasureDashboardHeartbeat();
  });
  syncDashboardSparkPulses();
  const isLight = localStorage.getItem('theme') === 'light';
  document.body.classList.toggle('light', isLight);
  updateThemeToggle(isLight);
  document.dispatchEvent(new CustomEvent('debridpulse:theme-changed', {detail:{light:isLight}}));
});


// ── Init ───────────────────────────────────────────────────────────────────
(async()=>{
  setDot('aria2', 'check', 'aria2: checking…');
  setDot('db',    'check', 'DB: checking…');

  // Load settings (Admin's: the configuration surface is Admin's alone)
  if (dpCan('administer')) {
    try {
      settingsData = await api('GET', '/settings');
    } catch(e) {
    }
  }

  renderTopbarActions();

  // Load stats with visible retry
  let statsLoaded = false;
  let statsAttempt = 0;

  while (!statsLoaded) {
    statsAttempt++;

    statsLoaded = await loadStats();

    if (!statsLoaded) {
      const delay =
        Math.min(
          400 + statsAttempt * 400,
          3000
        );


      await new Promise(
        r => setTimeout(r, delay)
      );

      if (statsAttempt >= 10) {
        break;
      }
    }
  }

  // Start background tasks immediately — do not wait for stats
  loadRecent().catch(() => {});
  checkConnections().catch(() => {});

  // Generic statistics availability says nothing about provider health.
  // The neutral provider-status runtime owns provider presentation.

  // ── Server-Sent Events — live updates without 15 s polling ──────────────
  // Falls back to polling if SSE is unavailable (proxy, browser quirk, etc.)
  (function initSSE() {
    if (
      typeof EventSource === 'undefined'
    ) {
      return startPolling();
    }

    var es;
    var sseOk = false;
    var fallbackTimer = null;
    // Set when a connected stream is lost (it ended or failed): events sent
    // meanwhile never arrive, so the next connection re-reads this module's
    // live views rather than showing them as they were.
    var streamLost = false;

    function rereadLiveViews() {
      loadStats().catch(()=>{});
      if (document.getElementById('view-dashboard')?.classList.contains('active')) {
        loadRecent().catch(()=>{});
      }
      if (document.getElementById('view-torrents')?.classList.contains('active')) {
        loadTorrents().catch(()=>{});
      }
    }

    function connect() {
      try {
        es =
          new EventSource(
            '/api/events/stream'
          );

        es.addEventListener(
          'connected',
          function() {
            sseOk = true;

            if (fallbackTimer) {
              clearInterval(
                fallbackTimer
              );

              fallbackTimer = null;
            }

            if (streamLost) {
              streamLost = false;
              rereadLiveViews();
            }

            // Cold-load / reconnect recovery hook for bounded owners that must
            // re-query authoritative state (e.g. file-selection offers, §40).
            // The stream's state is also readable (data-dp-pulse) by an owner
            // that binds after this event.
            document.documentElement.dataset.dpPulse = 'connected';
            document.dispatchEvent(new CustomEvent('debridpulse:pulse-connected'));
          }
        );

        // Neutral file-selection availability signal (§39). Carries only
        // { transfer_id }; the owner then GETs authoritative state (§40, §48).
        es.addEventListener(
          'file_selection_available',
          function(e) {
            let payload = {};
            try { payload = JSON.parse(e.data || '{}'); } catch (_) {}
            document.dispatchEvent(new CustomEvent('debridpulse:file-selection-available',
              {detail: payload}));
          }
        );

        // The Activity Log's journal changed. Carries only the newest committed
        // id ({latest}); the Activity Log owner decides whether to re-read the
        // journal itself. The server sends it only to a role that may read it.
        es.addEventListener(
          'activity_journal_changed',
          function(e) {
            let payload = {};
            try { payload = JSON.parse(e.data || '{}'); } catch (_) {}
            document.dispatchEvent(new CustomEvent('debridpulse:activity-journal-changed',
              {detail: payload}));
          }
        );

        // Statistics' own inputs changed (the journal). Carries nothing; the
        // server sends it only to a role that may read Statistics.
        es.addEventListener(
          'statistics_changed',
          function() {
            document.dispatchEvent(new CustomEvent('debridpulse:statistics-invalidated'));
          }
        );

        // Neutral integration-status invalidation. Carries nothing; the
        // provider-status owner re-observes each canonical status endpoint.
        es.addEventListener(
          'integration_status_changed',
          function() {
            document.dispatchEvent(new CustomEvent('debridpulse:integration-status-changed'));
          }
        );

        // Duplicate-consolidation notice. This module owns the one application
        // EventSource, so the event is registered here; operator-title.js owns
        // only the copy (consolidationToastCopy) and the toast presentation.
        // The payload carries public counts/ids only and is never replayed
        // client-side after a reload.
        es.addEventListener(
          'duplicate_consolidated',
          function(e) {
            try {
              const copy = window.DPIcons.consolidationToastCopy(JSON.parse(e.data));
              if (copy) window.DPIcons.toast(copy, 'success');
            } catch (_) {
              // Invalid public event data is ignored rather than rendered.
            }
          }
        );

        es.addEventListener(
          'stats_changed',
          function() {
            loadStats().catch(()=>{});
            // The Statistics owner decides whether it is shown and when to re-read.
            document.dispatchEvent(new CustomEvent('debridpulse:statistics-invalidated'));

            if (
              document
                .getElementById(
                  'view-dashboard'
                )
                ?.classList.contains(
                  'active'
                )
            ) {
              loadRecent().catch(()=>{});
            }
          }
        );

        var progressStatsTimer = null;

        es.addEventListener(
          'torrent_updated',
          function(e) {
            let payload = {};

            try {
              payload = JSON.parse(e.data || '{}');
            } catch (_) {}

            notifyExtractionFailure(payload);

            const patchedProgress =
              patchProgressOnlyTransferEvent(payload);

            if (!patchedProgress) {
              if (
                document
                  .getElementById('view-torrents')
                  ?.classList.contains('active')
              ) {
                loadTorrents().catch(()=>{});
              }

              if (
                document
                  .getElementById('view-dashboard')
                  ?.classList.contains('active')
              ) {
                loadRecent().catch(()=>{});
              }

              loadStats().catch(()=>{});

              // One minimal generic local signal that THIS transfer's semantic
              // presentation changed (never file-selection-specific, never
              // emitted for a progress-only patch) so a bounded owner with an
              // already-open, transfer-scoped surface (e.g. Details'
              // file-selection mount) can re-read authoritative state without
              // requiring the user to click a now-invalid control first.
              const semanticTransferId =
                Number(payload?.id ?? payload?.torrent_id);

              if (Number.isFinite(semanticTransferId)) {
                document.dispatchEvent(new CustomEvent('debridpulse:transfer-updated',
                  {detail: {transferId: semanticTransferId}}));
              }
            } else if (!progressStatsTimer) {
              progressStatsTimer = setTimeout(
                ()=>{
                  progressStatsTimer = null;
                  loadStats().catch(()=>{});
                },
                1500
              );
            }
          }
        );

        // The heartbeat, at least every 30 s however busy the stream: the
        // clock for figures that change with time alone.
        es.addEventListener(
          'ping',
          function() {
            // Statistics' Overall & Rolling row is this owner's (/stats): its
            // last-24-hours and last-7-days figures age while the page is open.
            if (document.getElementById('view-stats')?.classList.contains('active')) {
              loadStats().catch(()=>{});
            }
            document.dispatchEvent(new CustomEvent('debridpulse:pulse-heartbeat'));
          }
        );

        // The account's role changed: its session continues under the new
        // authority, so start over from what the server now allows.
        es.addEventListener('account_changed', function() {
          window.location.reload();
        });

        // The session ended (disabled, deleted, credentials changed): the
        // session owner confirms and signs the browser out.
        es.addEventListener('session_ended', function() {
          es.close();
          document.documentElement.dataset.dpPulse = 'lost';
          document.dispatchEvent(new CustomEvent('debridpulse:pulse-lost'));
          window.debridPulseAuth.refreshSession({force: true}).catch(()=>{});
        });

        es.onerror = function() {
          if (sseOk) streamLost = true;
          // Owners that present live state stop claiming it until the next
          // debridpulse:pulse-connected.
          document.documentElement.dataset.dpPulse = 'lost';
          document.dispatchEvent(new CustomEvent('debridpulse:pulse-lost'));
          // A refused reconnect is often an ended session or a changed role.
          window.debridPulseAuth.refreshSession({force: true}).catch(()=>{});
          if (!sseOk) {
            startPolling();
          }

          es.close();

          setTimeout(
            connect,
            10000
          );
        };
      } catch(err) {
        startPolling();
      }
    }

    function startPolling() {
      if (fallbackTimer) return;

      fallbackTimer =
        setInterval(()=>{
          loadStats().catch(()=>{});

          if (
            document
              .getElementById(
                'view-dashboard'
              )
              ?.classList.contains(
                'active'
              )
          ) {
            loadRecent()
              .catch(()=>{});
          }

          if (
            document
              .getElementById(
                'view-torrents'
              )
              ?.classList.contains(
                'active'
              )
          ) {
            loadTorrents()
              .catch(()=>{});
          }
        }, 15000);
    }

    connect();

    // Still refresh stats every 60 s as a safety net even with SSE
    setInterval(
      ()=>{
        loadStats().catch(()=>{});
      },
      60000
    );
  })();

  setInterval(
    ()=>checkConnections().catch(()=>{}),
    60000
  );
})();


// ── Download speed cap ────────────────────────────────────────────────────────

async function _setDownloadSpeedCap(bps) {
  try {
    // DP 1.0.12 canonical architecture correction: the neutral runtime-limit
    // surface, not the aria2-specific route, is the write authority for live
    // bandwidth (specification section 9.6).
    var limitResult = await api('PATCH', '/execution/runtime-limits', {max_download_bytes_per_second: bps});
    // Saved first, then applied: a value the server now holds that downloads
    // could not take yet is said as such (its error stays in the server log).
    if (limitResult && limitResult.ok === false) {
      toast(limitResult.configured?.max_download_bytes_per_second === bps
        ? 'Speed limit saved, but running downloads could not take it yet.'
        : 'Speed limit could not be applied.', 'error');
      return false;
    }
    // The canonical cache follows the value the runtime-limits surface accepted.
    if (settingsData) {
      settingsData.execution_runtime_limits = Object.assign({}, settingsData.execution_runtime_limits,
        {max_download_bytes_per_second: bps});
    }
    updateRuntimeStatusBadge({limitBps: bps});
    return true;
  } catch(e) {
    // A refusal is already a sentence about the speed limit; anything else
    // (a timeout, a lost connection) is said with what failed.
    toast(e.status === 400 ? sanitizeErrorMsg(e.message)
      : 'Speed limit could not be changed: ' + sanitizeErrorMsg(e.message), 'error');
    return false;
  }
}

// ── Neutral runtime status (topbar indicator + browser tab) ──────────────────
//
// ONE state object, ONE writer, ONE neutral backend fact. The topbar and the
// browser-tab title both read `_runtimeStatusState`, so they can never disagree,
// and nothing here polls an executor: `/execution/throughput` reads the core
// throughput owner, which aggregates every currently acquiring executor. A
// future executor contributes by implementing the neutral executor contracts,
// without either of these surfaces changing.
//
// The facts arrive at their own cadence: the volatile speed (and the cap it
// is shown against) from `/execution/throughput`, a pure in-memory read of the
// core throughput fact; the occupancy count from `/execution/runtime-status`,
// which also reads the repository. Each fact has exactly one source, both land
// in the one state through the one writer, and neither poll waits for the
// other -- a slow occupancy read can never stretch the speed cadence.
var _runtimeStatusState = {
  active: 0,
  limitBps: 0,
  liveBps: 0,
};
var _runtimeSpeedBusy = false;
var _runtimeStatusBusy = false;
var RUNTIME_SPEED_INTERVAL_MS = 500;
var RUNTIME_STATUS_INTERVAL_MS = 1000;

async function loadRuntimeSpeed() {
  if (_runtimeSpeedBusy) return;
  _runtimeSpeedBusy = true;
  try {
    const data = await api('GET', '/execution/throughput', null, 3000);
    setDashboardHeartbeatThroughput(data.download_bytes_per_second, data.sustained_peak_bytes_per_second);
    updateRuntimeStatusBadge({
      liveBps: Number(data.download_bytes_per_second) || 0,
      limitBps: Number(data.max_download_bytes_per_second) || 0,
    });
  } finally {
    _runtimeSpeedBusy = false;
  }
}

async function loadRuntimeStatus() {
  if (_runtimeStatusBusy || !dpCan('administer')) return;
  _runtimeStatusBusy = true;
  try {
    const data = await api('GET', '/execution/runtime-status', null, 3000);
    updateRuntimeStatusBadge({
      // DebridPulse's own execution-admission occupancy, never a native queue count.
      active: Number(data.active_execution_slots) || 0,
    });
  } finally {
    _runtimeStatusBusy = false;
  }
}

function updateRuntimeStatusBadge(patch) {
  Object.assign(_runtimeStatusState, patch);
  var s = _runtimeStatusState;
  var topBadge = document.getElementById('runtime-speed-badge');
  var elActive = document.getElementById('runtime-badge-active');
  var elMax    = document.getElementById('runtime-badge-max');
  var elSpeed  = document.getElementById('runtime-badge-speed');
  var elLimit  = document.getElementById('runtime-badge-limit');
  if (!topBadge) return;

  if (elActive) elActive.textContent = s.active;
  // The denominator is DebridPulse scheduler capacity: the canonical
  // transfer policy, never a value reported by any download engine.
  var maxDl = window.DPProcessingPresentation
    ? window.DPProcessingPresentation.configuredMaxConcurrency() : null;
  if (elMax)    elMax.textContent    = maxDl || '—';
  if (elSpeed)  elSpeed.textContent  = fmtSpeed(s.liveBps || 0);

  if (elLimit) elLimit.textContent = fmtSpeedCap(s.limitBps);

  // Occupancy and capacity are administrative runtime facts: a caller who
  // cannot read them sees only the live speed, never a stand-in zero.
  topBadge.dataset.tooltip = dpCan('administer') ? 'Active / max — live speed — download speed cap' : 'Live download speed';

  renderOperatorTitle();

  document.querySelectorAll('#runtime-cap-menu [data-cap-bps]').forEach(function(button) {
    button.classList.toggle(
      'active',
      Number(button.dataset.capBps) === Number(s.limitBps || 0)
    );
  });
}

function toggleSpeedCapMenu(event) {
  if (event) event.stopPropagation();
  var menu = document.getElementById('runtime-cap-menu');
  var toggle = document.getElementById('runtime-cap-toggle');
  if (!menu || !toggle) return;
  var opening = menu.hidden;
  menu.hidden = !opening;
  toggle.setAttribute('aria-expanded', opening ? 'true' : 'false');
  if (opening) {
    var custom = document.getElementById('runtime-cap-custom-mbps');
    if (custom && _runtimeStatusState.limitBps > 0) {
      custom.value = (_runtimeStatusState.limitBps / 1048576).toFixed(1).replace(/\.0$/, '');
    }
  }
}

function closeSpeedCapMenu() {
  var menu = document.getElementById('runtime-cap-menu');
  var toggle = document.getElementById('runtime-cap-toggle');
  if (menu) menu.hidden = true;
  if (toggle) toggle.setAttribute('aria-expanded', 'false');
}

async function applyTopbarSpeedCap(bps) {
  var applied = await _setDownloadSpeedCap(Math.max(0, Number(bps) || 0));
  if (applied) closeSpeedCapMenu();
}

async function applyTopbarCustomSpeedCap() {
  var input = document.getElementById('runtime-cap-custom-mbps');
  var raw = input ? input.value.trim() : '';
  var mbps = raw === '' ? NaN : Number(raw);
  if (!Number.isFinite(mbps) || mbps < 0) {
    toast('Enter a speed cap of 0 MB/s or greater', 'error');
    return;
  }
  await applyTopbarSpeedCap(Math.round(mbps * 1048576));
}
