/* DebridPulse — self-hosted multi-provider transfer manager. */

const API = '/api';
let settingsData = {};

// settingsData is the cached GET /settings document. aria2 configuration, the
// scheduler's concurrency and the download bandwidth cap are each read from
// their one canonical namespace; no flat alias is read, mirrored or written.
function aria2Options() {
  var entry = settingsData && settingsData.integrations && settingsData.integrations.aria2;
  return (entry && entry.options) || {};
}
function aria2Mode() {
  return aria2Options().mode || 'builtin';
}
// Per-transfer pauses are counted by the backend (GET /stats by_status.paused);
// this is only the last value read from there and is never adjusted locally.
let pausedTransferCount = 0;
// Global processing pause is operational state owned by the backend's durable
// application state -- not a setting. This is a non-persisted projection of it,
// assigned only from server responses (GET /stats and the pause/resume results)
// and never written to, or read from, settingsData.
let processingPaused = false;
function invalidateProviderStatus() {
  return window.DPProviderStatus?.invalidate?.();
}

async function refreshProviderStatus() {
  if (!window.DPProviderStatus?.refresh) return null;
  return window.DPProviderStatus.refresh();
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
  updateAria2TopbarBadge({});
}

// ── Nav ────────────────────────────────────────────────────────────────────
function nav(el) {
  if (!el) return;
  // Navigation establishes a new presentation generation. An HTTP response
  // initiated by an older surface may finish later, but may not become truth.
  invalidateProviderStatus();
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
  document.getElementById('page-title').textContent = titles[v] || v;
  const subtitle = document.getElementById('page-subtitle');
  if (subtitle) subtitle.textContent = subtitles[v] || '';
  document.dispatchEvent(new CustomEvent('debridpulse:navigation', {detail:{view:v,title:titles[v]||v}}));
  if (v === 'dashboard') { loadStats(); loadRecent(); }
  if (v === 'torrents')  { clearSelection(); loadTorrents(); }
  if (v === 'events')    window.DPActivityLog.load();
  if (v === 'stats')     loadDetailedStats();
  if (v === 'settings')  loadSettings();
  if (v === 'help')      loadHelp();
  closeSidebar();
}

// ── API ────────────────────────────────────────────────────────────────────
async function api(method, path, body, timeoutMs, options) {
  const isFormData = typeof FormData !== 'undefined' && body instanceof FormData;
  const opts = {
    method,
    headers: isFormData ? {} : {'Content-Type':'application/json'}
  };
  if (body) opts.body = isFormData ? body : JSON.stringify(body);
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
    if (!r.ok) throw new Error(data.detail || r.statusText);
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

function sourceLabel(source) {
  const labels = {
    direct_link: 'Direct link',
    manual: 'Magnet link',
    manual_file: 'Torrent file',
    alldebrid_existing: 'Provider inventory',
    import_existing: 'Provider inventory',
    inventory: 'Provider inventory',
    api: 'API'
  };
  const key = String(source || '').trim();
  return labels[key] || esc(key) || '—';
}

function transferProviderPresentation(t) {
  const completed = String(t?.status || '') === 'completed';
  const name = completed ? t?.delivering_provider_name : t?.current_provider_name;
  if (name) return {label: String(name), state: 'known'};
  if (completed && t?.provider_provenance_status === 'unknown_legacy') {
    return {label: 'Unknown', state: 'unknown'};
  }
  if (!completed && !t?.current_provider_id) {
    return {label: 'Pending', state: 'pending'};
  }
  return {label: 'Unknown', state: 'unknown'};
}

function providerChip(t) {
  const provider = transferProviderPresentation(t);
  return `<span class="dp-provider-chip" data-provider-state="${provider.state}">${esc(provider.label)}</span>`;
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
    if (state === 'consolidated' && contributed) return {state, label: `Consolidated from #${contributor}`};
    if (state === 'unverified') return {state, label: contributed ? `From #${contributor}` : 'Original'};
    if (state === 'original') return {state, label: 'Original'};
    return {state: '', label: ''};
  };
  return `<div class="dp-detail-route-list">${attempts.map((attempt) => {
    const provider = attempt.provider_name || 'Unknown';
    // An unverified association is the backend's statement about this source; it
    // replaces the route's own outcome label and never reads as a verified candidate.
    const unverified = attempt.verification_state === 'unverified';
    const outcome = unverified ? {label: 'Unverified', state: 'unverified'} : routeOutcomePresentation(attempt.outcome);
    const outcomeTitle = unverified && attempt.unverified_reason ? `Equivalence unproven: ${attempt.unverified_reason}` : '';
    const relation = relationOf(attempt);
    const identity = attempt.route_identity || '—';
    const identityTitle = attempt.route_location || attempt.route_identity || '';
    return `<div class="dp-detail-route-row" data-route-relation="${esc(relation.state)}">
      <span class="dp-detail-route-order">${esc(attempt.presentation_ordinal ?? attempt.ordinal ?? '')}</span>
      <span class="dp-detail-route-provider">${esc(provider)}</span>
      <span class="dp-detail-route-identity" title="${esc(identityTitle)}">${esc(identity)}</span>
      <span class="dp-detail-route-outcome" data-route-outcome="${esc(outcome.state)}" title="${esc(outcomeTitle)}">${esc(outcome.label)}</span>
      <span class="dp-detail-route-relation">${esc(relation.label)}</span>
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
function fmtDate(d) {
  const x = parseApiDate(d);
  if (!x) return '—';
  const timeZone = String((settingsData && settingsData.timezone) || '').trim() || undefined;
  const dateOptions = {day:'2-digit',month:'2-digit'};
  const timeOptions = {hour:'2-digit',minute:'2-digit',hour12:false};
  if (timeZone) {
    dateOptions.timeZone = timeZone;
    timeOptions.timeZone = timeZone;
  }
  // Use en-GB for consistent DD.MM HH:MM format regardless of browser locale.
  const dateStr = x.toLocaleDateString('en-GB',dateOptions).replace('/','.').replace('/','.');
  const timeStr = x.toLocaleTimeString('en-GB',timeOptions);
  return dateStr + ' ' + timeStr;
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
function transferDisplayStatus(t) {
  const projected = String(t && t.presentation_status || '').trim().toLowerCase();
  return projected || (t && t.status) || '';
}
function progress(pct, status) {
  const state = String(status || '').toLowerCase();
  const done = state === 'completed';
  const failed = state === 'error';
  const active = state === 'downloading';
  const raw = Number(pct);
  const actual = done ? 100 : Math.min(Math.max(Number.isFinite(raw) ? raw : 0, 0), 100);
  const showStripe = active && actual === 0;
  const visual = actual;
  let fillStyle = showStripe
    ? 'width:100%;opacity:.35;background:repeating-linear-gradient(90deg,var(--accent) 0,var(--accent) 8px,transparent 8px,transparent 16px)'
    : 'width:' + visual + '%';
  if (failed) {
    fillStyle += ';opacity:1;background:var(--dp-state-error)!important;background-color:var(--dp-state-error)!important;background-image:none!important;box-shadow:0 0 8px color-mix(in srgb,var(--dp-state-error) 88%,transparent),0 0 17px color-mix(in srgb,var(--dp-state-error) 46%,transparent)!important;filter:saturate(1.12) brightness(1.08)';
  }
  const cls = done ? 'done' : (failed ? 'error dp-terminal-error-progress' : '');
  const trackCls = failed ? 'prog dp-terminal-error-rail' : 'prog';
  const label = done ? '100%' : (showStripe ? '…' : actual.toFixed(0) + '%');
  const attrs = failed
    ? ' data-dp-actual-progress="' + actual + '" data-dp-visual-progress="' + visual + '"'
    : '';
  return '<div class="' + trackCls + '"' + (failed ? ' data-dp-actual-progress="' + actual + '"' : '') + '><div class="prog-fill ' + cls + '" style="' + fillStyle + '"' + attrs + '></div></div>' +
         '<span class="prog-pct">' + label + '</span>';
}

// An extraction failure is announced once per event; it paints nothing. The
// status badge of every row comes from the backend's effective presentation on
// the authoritative refresh that every torrent_updated event triggers.
function notifyExtractionFailure(data) {
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
    const nextProgress = Number(update?.progress);

    if (!Number.isFinite(id) || !Number.isFinite(nextProgress)) {
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
          progressCell.innerHTML =
            progress(nextProgress, status);
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

function getAria2ngUrl(aria2Url) {
  // Derive aria2ng URL from aria2 JSON-RPC URL.
  // Example: http://192.168.1.100:6800/jsonrpc → http://192.168.1.100:6880/
  if (!aria2Url) return '';
  try {
    const u = new URL(aria2Url);
    u.port = '6880';
    u.pathname = '/';
    u.search = '';
    return u.toString();
  } catch(e) {
    return '';
  }
}

function updateAria2ngLink() {
  const aria2Url = aria2Options().url || '';
  const row  = document.getElementById('aria2ng-row');
  const link = document.getElementById('aria2ng-link');
  if (!row || !link) return;
  // The aria2 web UI link is not offered inside an authenticated session.
  const authenticated = !!(window.debridPulseAuth && window.debridPulseAuth.session()
    && window.debridPulseAuth.session().authenticated);
  if (aria2Url && !authenticated) {
    link.href = getAria2ngUrl(aria2Url) || '#';
    row.style.display = 'flex';
  } else {
    row.style.display = 'none';
  }
}
document.addEventListener('debridpulse:session-changed', updateAria2ngLink);

async function checkConnections() {
  const cfg = settingsData || {};
  await refreshProviderStatus();

  // aria2 check — retry once if first attempt fails
  if (aria2Options().url || aria2Mode() === 'builtin') {
    let aria2Ok = false;
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        const result = await api('POST', '/settings/test-aria2');
        setDot('aria2', 'ok', `aria2: ${result.version||'online'}`);
        aria2Ok = true;
        break;
      } catch {
        if (attempt < 3) {
          await new Promise(r => setTimeout(r, attempt * 800));
        } else {
          setDot('aria2', 'error', 'aria2: offline');
        }
      }
    }
  } else {
    setDot('aria2', 'warn', 'aria2: not configured');
  }
  updateAria2ngLink();

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

  const liveBps = (_aria2BadgeState && Number(_aria2BadgeState.liveBps)) || 0;
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


const DASHBOARD_METRIC_HISTORY_KEY = 'debridpulse.dashboard.metric-history.v2';
const DASHBOARD_METRIC_HISTORY_LIMIT = 30;
const DASHBOARD_METRIC_SAMPLE_INTERVAL_MS = 15000;
const DASHBOARD_HERO_METRICS = {
  's-total':      {key: 'total',      label: 'Total downloads',  kind: 'cumulative',    hint: 'recent downloads added per sample interval'},
  's-completed':  {key: 'completed',  label: 'Completed',        kind: 'cumulative',    hint: 'recent completions per sample interval'},
  's-active':     {key: 'active',     label: 'Active now',       kind: 'instantaneous', hint: 'recent sampled active-transfer count'},
  's-processing': {key: 'processing', label: 'Processing',       kind: 'instantaneous', hint: 'recent sampled processing count'},
  's-error':      {key: 'errors',     label: 'Errors',           kind: 'instantaneous', hint: 'recent sampled error count'},
  's-size':       {key: 'downloaded', label: 'Total downloaded', kind: 'cumulative',    hint: 'bytes completed per sample interval'}
};

function dashboardMetricNumber(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(0, parsed) : 0;
}

function readDashboardMetricHistory() {
  try {
    const parsed = JSON.parse(localStorage.getItem(DASHBOARD_METRIC_HISTORY_KEY) || '[]');
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(sample => sample && Number.isFinite(Number(sample.ts)))
      .slice(-DASHBOARD_METRIC_HISTORY_LIMIT);
  } catch (_) {
    return [];
  }
}

function writeDashboardMetricHistory(samples) {
  try {
    localStorage.setItem(
      DASHBOARD_METRIC_HISTORY_KEY,
      JSON.stringify(samples.slice(-DASHBOARD_METRIC_HISTORY_LIMIT))
    );
  } catch (_) {
    // Storage can be unavailable in hardened/private browser contexts.
  }
}

function dashboardCumulativeDeltas(values) {
  return values.map((value, index) => {
    if (index === 0) return 0;
    const previous = values[index - 1];
    return value >= previous ? value - previous : 0;
  });
}

function dashboardSparkCoordinates(values) {
  if (!Array.isArray(values) || values.length < 2) return [];
  const clean = values.map(dashboardMetricNumber);
  const min = Math.min(...clean);
  const max = Math.max(...clean);
  const span = max - min;
  return clean.map((value, index) => ({
    x: (index / (clean.length - 1)) * 100,
    y: span === 0 ? 12 : 20 - ((value - min) / span) * 16
  }));
}

function dashboardMonotoneSparkPath(points) {
  if (!Array.isArray(points) || points.length < 2) return '';
  const fmt = value => Number(value).toFixed(2);
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const intervals = points.slice(0, -1).map((point, index) => {
    const next = points[index + 1];
    const width = next.x - point.x;
    return {
      width,
      slope: width > 0 ? (next.y - point.y) / width : 0
    };
  });
  const tangents = new Array(points.length).fill(0);

  if (points.length === 2) {
    tangents[0] = intervals[0].slope;
    tangents[1] = intervals[0].slope;
  } else {
    const endpointTangent = (nearWidth, farWidth, nearSlope, farSlope) => {
      if (!nearWidth || !farWidth || !nearSlope) return 0;
      let tangent = (
        ((2 * nearWidth + farWidth) * nearSlope) -
        (nearWidth * farSlope)
      ) / (nearWidth + farWidth);
      if (tangent * nearSlope <= 0) return 0;
      if (
        nearSlope * farSlope < 0 &&
        Math.abs(tangent) > Math.abs(3 * nearSlope)
      ) {
        tangent = 3 * nearSlope;
      }
      return tangent;
    };

    tangents[0] = endpointTangent(
      intervals[0].width,
      intervals[1].width,
      intervals[0].slope,
      intervals[1].slope
    );
    tangents[tangents.length - 1] = endpointTangent(
      intervals[intervals.length - 1].width,
      intervals[intervals.length - 2].width,
      intervals[intervals.length - 1].slope,
      intervals[intervals.length - 2].slope
    );

    for (let index = 1; index < points.length - 1; index++) {
      const left = intervals[index - 1];
      const right = intervals[index];
      if (!left.slope || !right.slope || left.slope * right.slope <= 0) {
        tangents[index] = 0;
        continue;
      }
      const leftWeight = 2 * right.width + left.width;
      const rightWeight = right.width + 2 * left.width;
      tangents[index] = (leftWeight + rightWeight) / (
        (leftWeight / left.slope) + (rightWeight / right.slope)
      );
    }
  }

  let path = `M ${fmt(points[0].x)} ${fmt(points[0].y)}`;
  for (let index = 0; index < points.length - 1; index++) {
    const start = points[index];
    const end = points[index + 1];
    const width = end.x - start.x;
    const lowY = Math.min(start.y, end.y);
    const highY = Math.max(start.y, end.y);
    const cp1 = {
      x: start.x + width / 3,
      y: clamp(start.y + (tangents[index] * width) / 3, lowY, highY)
    };
    const cp2 = {
      x: end.x - width / 3,
      y: clamp(end.y - (tangents[index + 1] * width) / 3, lowY, highY)
    };
    path += ` C ${fmt(cp1.x)} ${fmt(cp1.y)}, ${fmt(cp2.x)} ${fmt(cp2.y)}, ${fmt(end.x)} ${fmt(end.y)}`;
  }
  return path;
}

function renderDashboardMetricHistory(samples) {
  Object.entries(DASHBOARD_HERO_METRICS).forEach(([valueId, metric]) => {
    const value = document.getElementById(valueId);
    const card = value?.closest('.dash-hero-stat');
    const svg = card?.querySelector('.dp-card-spark');
    if (!card || !svg) return;

    const rawValues = samples.map(sample => dashboardMetricNumber(sample[metric.key]));
    const values = metric.kind === 'cumulative'
      ? dashboardCumulativeDeltas(rawValues)
      : rawValues;
    const line = svg.querySelector('.dp-card-spark-line');
    const fill = svg.querySelector('.dp-card-spark-fill');
    const point = svg.querySelector('.dp-card-spark-point');
    if (!line || !fill || !point) return;

    const coordinates = dashboardSparkCoordinates(values);
    const path = dashboardMonotoneSparkPath(coordinates);
    card.dataset.dpMetric = metric.key;
    card.title = `${metric.label} — ${metric.hint}.`;

    if (path) {
      line.setAttribute('d', path);
      fill.setAttribute('d', `${path} L 100 24 L 0 24 Z`);
      point.setAttribute('opacity', '0');
    } else if (values.length === 1) {
      line.setAttribute('d', '');
      fill.setAttribute('d', '');
      point.setAttribute('cx', '50');
      point.setAttribute('cy', '12');
      point.setAttribute('opacity', '1');
    } else {
      line.setAttribute('d', '');
      fill.setAttribute('d', '');
      point.setAttribute('opacity', '0');
    }
  });
}

function recordDashboardMetricHistory(metrics) {
  if (!metrics || typeof metrics !== 'object') return;
  const snapshot = {
    ts: Date.now(),
    total: dashboardMetricNumber(metrics.total),
    completed: dashboardMetricNumber(metrics.completed),
    active: dashboardMetricNumber(metrics.active),
    processing: dashboardMetricNumber(metrics.processing),
    errors: dashboardMetricNumber(metrics.errors),
    downloaded: dashboardMetricNumber(metrics.downloaded)
  };
  const samples = readDashboardMetricHistory();
  const last = samples[samples.length - 1];
  const changed = !last || Object.values(DASHBOARD_HERO_METRICS).some(metric =>
    dashboardMetricNumber(last[metric.key]) !== snapshot[metric.key]
  );
  const due = !last || snapshot.ts - dashboardMetricNumber(last.ts) >= DASHBOARD_METRIC_SAMPLE_INTERVAL_MS;

  if (changed || due) {
    samples.push(snapshot);
    while (samples.length > DASHBOARD_METRIC_HISTORY_LIMIT) samples.shift();
    writeDashboardMetricHistory(samples);
  }
  renderDashboardMetricHistory(samples);
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
      if (errCard) errCard.style.opacity = errCount > 0 ? '1' : '.6';
      document.getElementById('s-size').textContent = fmtSize(s.total_completed_bytes);
      document.getElementById('s-blocked').textContent = `${s.total_blocked_files||0} blocked files`;
      recordDashboardMetricHistory({
        total,
        completed,
        active: s.active_operations ?? s.active_downloads ?? 0,
        processing: (Number(bs.processing) || 0) + (Number(bs.uploading) || 0),
        errors: errCount,
        downloaded: s.total_completed_bytes
      });
      document.getElementById('i-last-day').textContent = s.completed_last_24h||0;
      document.getElementById('i-last-week').textContent = s.completed_last_7d||0;
      document.getElementById('i-success-rate').textContent = s.success_rate_pct != null ? s.success_rate_pct+'%' : '—';
      document.getElementById('i-avg-duration').textContent = fmtDuration(s.avg_download_duration_seconds);
      document.getElementById('i-avg-size').textContent = s.avg_torrent_size_bytes ? fmtSize(s.avg_torrent_size_bytes) : '—';
      const active = s.active_operations ?? s.active_downloads ?? 0;
      const nb = document.getElementById('nb-active');
      if (nb) { nb.textContent = active; nb.style.display = active > 0 ? '' : 'none'; }
      // Topbar aria2 badge: active download count (if aria2 badge visible)
      updateAria2TopbarBadge({active: s.active_downloads||0});
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

async function uploadTorrentFile(input) {
  const file = input && input.files ? input.files[0] : null;
  if (!file) return;

  if (!file.name.toLowerCase().endsWith('.torrent')) {
    toast('Choose a .torrent file', 'error');
    input.value = '';
    return;
  }
  if (file.size > 16 * 1024 * 1024) {
    toast('Torrent file exceeds the 16 MB upload limit', 'error');
    input.value = '';
    return;
  }

  const form = new FormData();
  form.append('file', file, file.name);
  // The built-in browser is an interactive client: opt every torrent/magnet
  // submission into the interactive file-selection lifecycle. Historical /
  // headless callers that omit this keep the ALL default (correction §6).
  form.append('selection_mode', 'interactive');

  try {
    const res = await api('POST', '/torrents/add-file', form, 60000);
    if (res && res._duplicate && res._duplicate.action === 'skip') {
      toast('Already in queue: ' + (res.name || res._duplicate.reason), 'warn');
    } else if (res && res._duplicate && res._duplicate.action === 'warn') {
      toast('Torrent file added (possible duplicate)', 'warn');
    } else if (res && res._deferred) {
      toast('Torrent file added · processing is paused', 'success');
    } else {
      toast('Torrent file added!', 'success');
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
  const chrome = (parseFloat(styles.paddingTop) || 0) +
    (parseFloat(styles.paddingBottom) || 0) +
    (parseFloat(styles.borderTopWidth) || 0) +
    (parseFloat(styles.borderBottomWidth) || 0);
  const minimum = Math.ceil((lineHeight * 2) + chrome);
  const maximum = Math.ceil((lineHeight * 5) + chrome);
  input.style.height = `${minimum}px`;
  const target = Math.max(minimum, Math.min(input.scrollHeight, maximum));
  input.style.height = `${target}px`;
  input.style.overflowY = input.scrollHeight > maximum ? 'auto' : 'hidden';
}

function classifyDashboardEntries(raw) {
  const seen = new Set();
  const direct = [];
  const magnets = [];
  const invalid = [];
  String(raw || '').split(/\r?\n/).forEach((rawValue, index) => {
    const value = rawValue.trim();
    if (!value || seen.has(value)) return;
    seen.add(value);
    const entry = {value, line: index + 1};
    if (/^https?:\/\/\S+$/i.test(value)) direct.push(entry);
    else if (/^magnet:\?/i.test(value)) magnets.push(entry);
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
    toast(`Line ${first.line}: enter an HTTP(S) link or magnet URI`, 'error');
    input?.focus();
    return;
  }
  if (!direct.length && !magnets.length) {
    toast('Enter at least one HTTP(S) link or magnet URI', 'warn');
    input?.focus();
    return;
  }

  setButtonPending(button, true, 'Adding…');
  const failed = [];
  let handled = 0;
  let deferred = 0;
  try {
    if (direct.length) {
      try {
        const result = await api('POST', '/links/add', {links: direct.map(entry => entry.value)}, 30000);
        handled += direct.length;
        if (result && result._deferred) deferred += direct.length;
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

    failed.sort((a, b) => a.line - b.line);
    input.value = failed.map(entry => entry.value).join('\n');
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

async function recoverAll(button) {
  setButtonPending(button, true, 'Recovering…');

  try {
    toast(
      'Checking AllDebrid for ready torrents…',
      'info'
    );

    const r =
      await api('POST','/torrents/recover-all');

    const msg =
      `Recovery: reset ${r.reset} stuck, checked ${r.checked}, started ${r.started}`;

    toast(
      msg,
      r.started > 0 || r.reset > 0
        ? 'success'
        : 'warn'
    );

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
    toast('aria2 queue paused','warn');
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

    toast('aria2 queue resumed','success');
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
const DPModal = (function () {
  let activeMode = null;            // 'details' | 'file-selection' | null
  let onCloseCallback = null;
  let focusReturnTarget = null;

  function open(options) {
    const opts = options || {};
    const overlay = document.getElementById('overlay');
    const modal = document.getElementById('modal');
    const titleEl = document.getElementById('modal-title');
    const footer = document.getElementById('modal-footer');
    const closeBtn = modal ? modal.querySelector('.modal-close') : null;

    activeMode = opts.mode || 'details';
    onCloseCallback = typeof opts.onClose === 'function' ? opts.onClose : null;
    focusReturnTarget = opts.focusReturn ||
      (document.activeElement instanceof HTMLElement ? document.activeElement : null);

    if (overlay) { overlay.classList.add('open'); overlay.dataset.dpModalMode = activeMode; }
    if (modal) modal.dataset.dpModalMode = activeMode;
    if (titleEl && opts.title != null) titleEl.textContent = opts.title;
    if (closeBtn) {
      const label = opts.closeLabel || 'Close details';
      closeBtn.setAttribute('aria-label', label);
      closeBtn.title = label;
    }
    if (footer) { footer.hidden = true; footer.innerHTML = ''; }
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

    if (overlay) { overlay.classList.remove('open'); delete overlay.dataset.dpModalMode; }
    if (modal) delete modal.dataset.dpModalMode;
    if (footer) { footer.hidden = true; footer.innerHTML = ''; }

    activeMode = null;
    onCloseCallback = null;
    const returnTarget = focusReturnTarget;
    focusReturnTarget = null;

    if (closedMode === 'details') {
      document.dispatchEvent(new CustomEvent('debridpulse:detail-closed',
        {detail: {reason: reason || null}}));
    }
    if (returnTarget && typeof returnTarget.focus === 'function') {
      try { returnTarget.focus({preventScroll: true}); } catch (_) {}
    }
  }

  return {
    open,
    requestModalClose,
    finishClose,
    footer: function () { return document.getElementById('modal-footer'); },
    get mode() { return activeMode; },
  };
})();
window.DPModal = DPModal;

// ── Detail Modal ───────────────────────────────────────────────────────────
async function showDetail(id) {
  const modalTitle = document.getElementById('modal-title');
  const modalBody = document.getElementById('modal-body');

  DPModal.open({mode: 'details', title: 'Loading…', closeLabel: 'Close details'});

  if (modalBody) {
    modalBody.innerHTML =
      '<div class="empty" style="padding:24px">Loading transfer details…</div>';
  }

  try {
    const t = await api('GET',`/torrents/${id}`);

    if (modalTitle) {
      modalTitle.textContent =
        t.name || 'Torrent Details';
    }

    const providerPresentation = transferProviderPresentation(t);
    // The file-selection affordance (DP 1.0.12 Workstream B) can be valid
    // before any artifact exists yet (manifest-pending window, §6.5), so the
    // Files-section header must be able to render even when t.files is
    // still empty -- gated on the durable torrent/magnet source-kind fact,
    // never on filename shape or provider identity.
    const dpMayHaveFileSelection = ['magnet', 'torrent_file'].includes(
      String(t.current_source_identity && t.current_source_identity.kind || '').toLowerCase());
    const dpShowFilesCard = Boolean((t.files && t.files.length) || dpMayHaveFileSelection);
    if (modalBody) modalBody.innerHTML = `
      <div class="detail-grid">
        <div><div class="dk">Status</div><div class="dv">${badge(transferDisplayStatus(t), t)}</div></div>
        <div class="dp-detail-provider"><div class="dk">Provider</div><div class="dv">${esc(providerPresentation.label)}</div></div>
        <div><div class="dk">Progress</div><div class="dv">${(t.progress||0).toFixed(1)}%</div></div>
        <div><div class="dk">Size</div><div class="dv">${fmtSize(t.size_bytes)}</div></div>
        <div><div class="dk">Submitted As</div><div class="dv">${sourceLabel(t.source)}</div></div>
        <div><div class="dk">Added</div><div class="dv">${fmtDate(t.created_at)}</div></div>
        <div><div class="dk">Completed</div><div class="dv">${fmtDate(t.completed_at)}</div></div>
        <div class="dp-detail-original-resource" style="grid-column:1/-1"><div class="dk">Original Resource</div><div class="dv">${esc(t.original_resource || '—')}</div></div>
        <div style="grid-column:1/-1"><div class="dk">Transfer ID</div><div class="dv">${t.id}</div></div>
        <div style="grid-column:1/-1"><div class="dk">Hash</div><div class="dv" style="font-size:11px">${esc(t.hash||'—')}</div></div>
        ${t.local_path?`<div style="grid-column:1/-1"><div class="dk">Local Path</div><div class="dv" style="font-size:11px">${esc(t.local_path)}</div></div>`:''}
        ${t.error_message?`<div style="grid-column:1/-1"><div class="dk">Error</div><div class="dv" style="color:var(--red)">${esc(t.error_message)}</div></div>`:''}
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
          <div><span>Current Provider ID</span><strong>${esc(t.current_provider_id || '—')}</strong></div>
          <div><span>Delivering Provider ID</span><strong>${esc(t.delivering_provider_id || '—')}</strong></div>
        </div>
      </details>
      ${dpShowFilesCard?`
        <div class="card dp-detail-section-card dp-detail-files-card">
          <div class="card-header dp-detail-files-header">
            <span class="card-title">Files${t.files&&t.files.length?` (${t.files.length})`:''}</span>
            <span class="dp-detail-files-header-actions">
              <span class="dp-detail-files-group-slot" data-dp-group-candidates-mount data-dp-transfer-id="${t.id}"></span>
              <span class="dp-detail-files-selection-slot" data-dp-file-selection-mount data-dp-transfer-id="${t.id}"></span>
            </span>
          </div>
          ${t.files&&t.files.length?`
          <div class="dp-detail-table-wrap">
            <table class="t-table">
              <thead><tr><th>Filename</th><th>Size</th><th>Status</th></tr></thead>
              <tbody>${window.DPDetailCandidates.rowsMarkup(t.files)}</tbody>
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
                <div class="etime">${fmtDate(ev.created_at)}</div>
              </div>`).join('')}
          </div>
        </div>
      `:''}
    `;

    document.dispatchEvent(new CustomEvent('debridpulse:detail-rendered',
      {detail: {transferId: Number(id), transfer: t}}));
  } catch(e) {
    if (modalBody) {
      modalBody.innerHTML =
        `<div class="empty" style="padding:24px">Failed to load details: ${esc(sanitizeErrorMsg(e.message))}</div>`;
    }

    toast(sanitizeErrorMsg(e.message),'error');
    document.dispatchEvent(new CustomEvent('debridpulse:detail-rendered',
      {detail: {transferId: Number(id), transfer: null, error: true}}));
  }
}

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
  btn.title = action;
  btn.setAttribute('aria-label', action);
  window.DPIcons.renderThemeGlyph(!!isLight);
}
document.addEventListener('DOMContentLoaded', () => {
  setInterval(function() {
    if (settingsData && aria2Mode()==='builtin') {
      loadAria2Runtime().catch(()=>{});
    }
  }, 5000);
  setInterval(function() {
    loadAria2TopbarStat().catch(()=>{});
  }, 1000);
  document.addEventListener('click', function(event) {
    if (!event.target.closest('.aria2-cap-control')) closeAria2SpeedCapMenu();
  });
  document.addEventListener('keydown', function(event) {
    if (event.key === 'Escape') closeAria2SpeedCapMenu();
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
  const isLight = localStorage.getItem('theme') === 'light';
  document.body.classList.toggle('light', isLight);
  updateThemeToggle(isLight);
  document.dispatchEvent(new CustomEvent('debridpulse:theme-changed', {detail:{light:isLight}}));
});


// ── aria2 runtime badge ─────────────────────────────────────────────────────

async function loadAria2Runtime() {
  const data = await api('GET', '/aria2/runtime');
  const badge = document.getElementById('aria2-speed-badge');
  if (badge) {
    const isBuiltin = (data.mode || '') === 'builtin';
    if (isBuiltin && !data.running) {
      badge.style.display = 'none';
    } else if (isBuiltin) {
      badge.style.display = 'flex';
      updateAria2TopbarBadge({
        active: Number(data.active) || 0,
        liveBps: Number(data.download_speed) || 0,
        externalControl: false,
      });
      loadAria2SpeedLimit().catch(function(){});
    } else {
      badge.style.display = 'flex';
      updateAria2TopbarBadge({externalControl: true});
      loadAria2TopbarStat().catch(function(){});
    }
  }
  return data;
}


// ── Init ───────────────────────────────────────────────────────────────────
(async()=>{
  setDot('aria2', 'check', 'aria2: checking…');
  setDot('db',    'check', 'DB: checking…');

  // Load settings
  try {
    settingsData = await api('GET', '/settings');
  } catch(e) {
  }

  renderTopbarActions();
  updateAria2ngLink();

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

            // Cold-load / reconnect recovery hook for bounded owners that must
            // re-query authoritative state (e.g. file-selection offers, §40).
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

        es.addEventListener(
          'ping',
          function() {}
        );

        es.onerror = function() {
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


// ── Speed Limit ───────────────────────────────────────────────────────────────

async function loadAria2SpeedLimit() {
  try {
    var data = await api('GET', '/aria2/global-options', null, 10000);
    // The native effective cap is what this badge displays. Scheduler capacity
    // is universal transfer policy and is never taken from an aria2 response.
    updateAria2TopbarBadge({
      limitBps: parseInt(data.max_download_speed || 0),
      externalControl: !!data.global_options_read_only,
    });
  } catch (e) { /* aria2 not connected — silently ignore */ }
}

async function _setAria2Speed(bps) {
  var st = document.getElementById('aria2-speed-status');

  if (aria2Mode() !== 'builtin') {
    if (st) {
      st.style.color = 'var(--text2)';
      st.textContent = 'Externally Controlled';
    }
    updateAria2TopbarBadge({externalControl: true});
    return false;
  }

  if (st) { st.style.color='var(--text2)'; st.textContent='Applying…'; }
  try {
    // DP 1.0.12 canonical architecture correction: the neutral runtime-limit
    // surface, not the aria2-specific route, is the write authority for live
    // bandwidth (specification section 9.6).
    var limitResult = await api('PATCH', '/execution/runtime-limits', {max_download_bytes_per_second: bps});
    if (limitResult && limitResult.ok === false) {
      throw new Error(limitResult.last_apply_error || 'Bandwidth limit could not be applied');
    }
    // The canonical cache follows the value the runtime-limits surface accepted.
    if (settingsData) {
      settingsData.execution_runtime_limits = Object.assign({}, settingsData.execution_runtime_limits,
        {max_download_bytes_per_second: bps});
    }
    if (st) { st.style.color='var(--green)'; st.textContent = bps > 0 ? 'Set: ' + fmtSpeedCap(bps) : 'Unlimited'; }
    setTimeout(function(){ if(st) st.style.color='var(--text2)'; }, 3000);
    updateAria2TopbarBadge({limitBps: bps});
    return true;
  } catch(e) {
    if (st) { st.style.color='var(--red)'; st.textContent='Error: '+e.message; }
    toast('Speed limit error: '+e.message, 'error');
    return false;
  }
}

// Update Downloads badge from loadStats

// Topbar badge: live active count, speed cap, and max concurrent
var _aria2BadgeState = {
  active: 0,
  limitBps: 0,
  liveBps: 0,
  externalControl: false,
};
var _aria2TopbarStatBusy = false;

async function loadAria2TopbarStat() {
  if (_aria2TopbarStatBusy || !settingsData) return;
  _aria2TopbarStatBusy = true;
  try {
    const data = await api('GET', '/aria2/global-stat', null, 3000);
    updateAria2TopbarBadge({
      active: Number(data.active) || 0,
      liveBps: Number(data.download_speed) || 0,
      externalControl: !!data.external_control,
    });
  } finally {
    _aria2TopbarStatBusy = false;
  }
}

function updateAria2TopbarBadge(patch) {
  Object.assign(_aria2BadgeState, patch);
  var s = _aria2BadgeState;
  var topBadge = document.getElementById('aria2-speed-badge');
  var elActive = document.getElementById('aria2-badge-active');
  var elMax    = document.getElementById('aria2-badge-max');
  var elSpeed  = document.getElementById('aria2-badge-speed');
  var elLimit  = document.getElementById('aria2-badge-limit');
  var toggle   = document.getElementById('aria2-cap-toggle');
  if (!topBadge) return;

  var externalControl = !!s.externalControl;

  if (elActive) elActive.textContent = s.active;
  // The denominator is DebridPulse scheduler capacity: the canonical
  // transfer policy, never a value reported by the aria2 daemon.
  var maxDl = window.DPProcessingPresentation
    ? window.DPProcessingPresentation.configuredMaxConcurrency() : null;
  if (elMax)    elMax.textContent    = maxDl || '—';
  if (elSpeed)  elSpeed.textContent  = fmtSpeed(s.liveBps || 0);

  if (elLimit) {
    elLimit.textContent = externalControl
      ? 'Externally Controlled'
      : fmtSpeedCap(s.limitBps);
  }

  topBadge.classList.toggle('external-control', externalControl);

  if (toggle) {
    toggle.setAttribute(
      'aria-disabled',
      externalControl ? 'true' : 'false'
    );
    toggle.title = externalControl
      ? 'Bandwidth cap is controlled by the external aria2 daemon'
      : 'Set download speed cap';
    toggle.style.cursor = externalControl ? 'default' : '';

    var capArrow = toggle.querySelector('span[aria-hidden="true"]');
    if (capArrow) {
      capArrow.style.display = externalControl ? 'none' : '';
    }
  }

  topBadge.title = externalControl
    ? 'Active / max — DebridPulse-owned live speed — bandwidth externally controlled'
    : 'Active / max — live speed — download speed cap';

  if (externalControl) {
    topBadge.style.display = 'flex';
    closeAria2SpeedCapMenu();
  }

  renderOperatorTitle();

  document.querySelectorAll('#aria2-cap-menu [data-cap-bps]').forEach(function(button) {
    button.classList.toggle(
      'active',
      !externalControl &&
      Number(button.dataset.capBps) === Number(s.limitBps || 0)
    );
  });
}

function toggleAria2SpeedCapMenu(event) {
  if (event) event.stopPropagation();
  if (_aria2BadgeState.externalControl) return;
  var menu = document.getElementById('aria2-cap-menu');
  var toggle = document.getElementById('aria2-cap-toggle');
  if (!menu || !toggle) return;
  var opening = menu.hidden;
  menu.hidden = !opening;
  toggle.setAttribute('aria-expanded', opening ? 'true' : 'false');
  if (opening) {
    var custom = document.getElementById('aria2-cap-custom-mbps');
    if (custom && _aria2BadgeState.limitBps > 0) {
      custom.value = (_aria2BadgeState.limitBps / 1048576).toFixed(1).replace(/\.0$/, '');
    }
  }
}

function closeAria2SpeedCapMenu() {
  var menu = document.getElementById('aria2-cap-menu');
  var toggle = document.getElementById('aria2-cap-toggle');
  if (menu) menu.hidden = true;
  if (toggle) toggle.setAttribute('aria-expanded', 'false');
}

async function applyAria2TopbarSpeedCap(bps) {
  var applied = await _setAria2Speed(Math.max(0, Number(bps) || 0));
  if (applied) closeAria2SpeedCapMenu();
}

async function applyAria2TopbarCustomSpeedCap() {
  var input = document.getElementById('aria2-cap-custom-mbps');
  var raw = input ? input.value.trim() : '';
  var mbps = raw === '' ? NaN : Number(raw);
  if (!Number.isFinite(mbps) || mbps < 0) {
    toast('Enter a speed cap of 0 MB/s or greater', 'error');
    return;
  }
  await applyAria2TopbarSpeedCap(Math.round(mbps * 1048576));
}
