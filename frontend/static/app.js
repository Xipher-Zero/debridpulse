/* DebridPulse — AllDebrid + aria2 download manager */

const API = '/api';
let currentFilter = '';
let currentTorrentSearch = '';
let torrentPage = 1;
let torrentPageSize = 25;
let torrentTotal = 0;
let _torrentSearchTimer = null;
let settingsData = {};
let aria2DownloadsTimer = null;
let pausedTransferCount = 0;

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

  const globallyPaused = !!settingsData.paused;
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
}

// ── Nav ────────────────────────────────────────────────────────────────────
function nav(el) {
  if (!el) return;
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
  if (v === 'torrents')  loadTorrents();
  if (v === 'events')    loadEvents();
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
    const r = await fetch(API + path, opts);
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
  return `<div class="dp-detail-route-list">${attempts.map((attempt, index) => {
    const provider = attempt.provider_name || 'Unknown';
    const outcome = routeOutcomePresentation(attempt.outcome);
    return `<div class="dp-detail-route-row">
      <span class="dp-detail-route-order">${index + 1}</span>
      <span class="dp-detail-route-provider">${esc(provider)}</span>
      <span class="dp-detail-route-outcome" data-route-outcome="${esc(outcome.state)}">${esc(outcome.label)}</span>
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
function fmtEta(secs) {
  if (!secs || secs <= 0) return '';
  if (secs < 60)   return secs + 's';
  if (secs < 3600) return Math.floor(secs/60) + 'm ' + (secs%60) + 's';
  var h = Math.floor(secs/3600);
  var m = Math.floor((secs%3600)/60);
  return h + 'h ' + m + 'm';
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
function renderKvMap(arr, formatter) {
  // arr is an array of {status/level, count} objects from the API
  if (!arr || !arr.length) return '<div class="empty">No data available.</div>';
  const entries = Array.isArray(arr)
    ? arr.map(item => {
        const key = item.status ?? item.level ?? item.source ?? Object.keys(item).find(k => k !== 'count') ?? '?';
        return [key, item];
      })
    : Object.entries(arr);
  return `<div class="kv-list">${entries.map(([key, value]) => {
    const rendered = formatter
      ? formatter(value, key)
      : (value && typeof value === 'object' ? value.count ?? '—' : value);
    return `
    <div class="kv-row">
      <span>${esc(key)}</span>
      <strong>${esc(rendered)}</strong>
    </div>`;
  }).join('')}</div>`;
}
function badge(s, detail) {
  if (!window.DPIcons || typeof window.DPIcons.statusBadge !== 'function') {
    throw new Error('DebridPulse icon runtime is unavailable');
  }
  const semantics = window.DPFailureSemantics;
  const category = s === 'error' && semantics ? semantics.classify(detail) : '';
  return window.DPIcons.statusBadge(s, category ? semantics.labels[category] : '', category);
}
function transferDisplayStatus(t) {
  if (t && String(t.extraction_status || '').trim() === 'extracting') return 'extracting';
  if (t && t.status === 'completed' && (t.extraction_status === 'error' || Number(t.source_failure_count) > 0)) return 'completed_with_errors';
  if (t && t.status === 'downloading' && Number(t.source_failure_count) > 0) return 'downloading_with_errors';
  return (t && t.status) || '';
}
function providerDisplayStatus(t) {
  return (t && t.resources || []).map(resource => resource.state).join(', ');
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

function patchExtractionTransferEvent(data) {
  const id = Number(data?.id ?? data?.torrent_id);
  const extractionStatus = String(data?.extraction_status || '').trim();

  if (!Number.isFinite(id) || !extractionStatus) {
    return false;
  }

  let displayStatus = 'completed';
  if (extractionStatus === 'extracting') {
    displayStatus = 'extracting';
  } else if (extractionStatus === 'error') {
    displayStatus = 'completed_with_errors';
  }

  document
    .querySelectorAll(`tr[data-torrent-id="${id}"]`)
    .forEach(row => {
      const statusCell = row.querySelector('[data-role="transfer-status"]');
      if (statusCell) statusCell.innerHTML = badge(displayStatus);
    });

  if (extractionStatus === 'error') {
    const reason = sanitizeErrorMsg(
      data?.extraction_error || 'Archive extraction failed'
    );
    toast(`Extraction failed: ${reason}`, 'error');
  }

  return true;
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
    const nextStatus = String(update?.status || '');

    if (!Number.isFinite(id) || !Number.isFinite(nextProgress)) {
      continue;
    }

    document
      .querySelectorAll(`tr[data-torrent-id="${id}"]`)
      .forEach(row => {
        const currentStatus = String(row.dataset.status || '');
        const status = nextStatus || currentStatus;

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
  const aria2Url = (settingsData || {}).aria2_url || '';
  const row  = document.getElementById('aria2ng-row');
  const link = document.getElementById('aria2ng-link');
  if (!row || !link) return;
  if (aria2Url) {
    link.href = getAria2ngUrl(aria2Url) || '#';
    row.style.display = 'flex';
  } else {
    row.style.display = 'none';
  }
}

function renderAllDebridStatus(status) {
  const state = String(status?.state || 'unknown');
  const username = String(status?.username || '').trim();
  const presentation = {
    disabled:      ['warn',  'AllDebrid: disabled'],
    unconfigured:  ['warn',  'AllDebrid: not configured'],
    auth_required: ['error', 'AllDebrid: authentication required'],
    healthy:       ['ok',    `AllDebrid: ${username || 'online'}`],
    unhealthy:     ['error', 'AllDebrid: unavailable'],
    unknown:       ['check', 'AllDebrid: status unknown'],
  }[state] || ['check', 'AllDebrid: status unknown'];
  setDot('api', presentation[0], presentation[1]);
  if (state === 'healthy') _updatePremiumLabel(status);
  else _updatePremiumLabel(null);
}

async function loadAllDebridStatus() {
  try {
    const status = await api('GET', '/integration-status/alldebrid');
    renderAllDebridStatus(status);
    return status;
  } catch (_) {
    // Failure of the generic application/API path cannot establish provider
    // failure. Preserve that distinction by rendering a neutral unknown state.
    renderAllDebridStatus({state:'unknown'});
    return null;
  }
}

async function checkConnections() {
  const cfg = settingsData || {};
  await loadAllDebridStatus();

  // aria2 check — retry once if first attempt fails
  if (cfg.aria2_url || cfg.aria2_mode === 'builtin') {
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

function getActiveSettingsTab() {
  return document.querySelector('#settings-tabs .stab.active')?.dataset.tab || 'tab-general';
}

async function pauseProcessing() {
  const button =
    document.getElementById('btn-pause-all');

  setButtonPending(button, true, 'Pausing…');

  try {
    await api('POST', '/processing/pause');
    settingsData.paused = true;
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
    settingsData.paused = false;
    pausedTransferCount = 0;
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
    settingsData.paused = false;
    pausedTransferCount = 0;
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
  's-total':      {key: 'total',      label: 'Total downloads'},
  's-completed':  {key: 'completed',  label: 'Completed'},
  's-active':     {key: 'active',     label: 'Active now'},
  's-processing': {key: 'processing', label: 'Processing'},
  's-error':      {key: 'errors',     label: 'Errors'},
  's-size':       {key: 'downloaded', label: 'Total downloaded'}
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

    const values = samples.map(sample => dashboardMetricNumber(sample[metric.key]));
    const line = svg.querySelector('.dp-card-spark-line');
    const fill = svg.querySelector('.dp-card-spark-fill');
    const point = svg.querySelector('.dp-card-spark-point');
    if (!line || !fill || !point) return;

    const coordinates = dashboardSparkCoordinates(values);
    const path = dashboardMonotoneSparkPath(coordinates);
    card.dataset.dpMetric = metric.key;
    card.title = `${metric.label} — sparkline shows recent live samples of this exact card metric.`;

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
      if (settingsData) settingsData.paused = !!s.paused;
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


function goToTorrentPage(p) { torrentPage = Math.max(1,p); loadTorrents(); }
function onPageSizeChange(v) { torrentPageSize=Math.min(Math.max(parseInt(v)||25,15),100); torrentPage=1; loadTorrents(); }

async function checkForUpdate() {
  try {
    const data = await api('GET', '/version/check');
    const badge = document.getElementById('update-badge');
    const badgeV = document.getElementById('update-badge-version');
    if (!badge) return;
    if (data.update_available && data.latest) {
      if (badgeV) badgeV.textContent = 'v' + data.latest;
      badge.style.display = 'flex';
    } else {
      badge.style.display = 'none';
    }
  } catch (_) {}
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

async function loadRecent() {
  try {
    const recentLimit = dashboardRecentLimit();
    _dashboardRecentFitLimit = recentLimit;
    const {items} = await api('GET', `/torrents?limit=${recentLimit}`);
    const tb = document.getElementById('dash-tbody');
    if (!items.length) {
      tb.innerHTML = '<tr><td colspan="6"><div class="empty"><div class="empty-icon" aria-hidden="true"></div>No downloads yet. Add a link, magnet, or torrent file to get started.</div></td></tr>';
      const countEl = document.getElementById('dash-activity-count');
      if (countEl) countEl.textContent = 'Recent transfer history';
      return;
    }
    // Update activity count
    const countEl = document.getElementById('dash-activity-count');
    if (countEl) countEl.textContent = items.length + ' most recent download' + (items.length === 1 ? '' : 's');
    tb.innerHTML = items.map(t => {
      const pct_val = t.progress != null ? Math.round(t.progress) : 0;
      const is_active = ['downloading','queued'].includes(t.status);
      return `<tr data-torrent-id="${t.id}" data-status="${esc(t.status)}" onclick="showDetail(${t.id})" style="cursor:pointer">
        <td>
          <div class="t-name" title="${esc(t.name)||''}">${esc(t.name)||'(unnamed)'}</div>
          ${is_active ? `<div class="dash-row-bar"><div class="dash-row-bar-fill" style="width:${pct_val}%;background:var(--blue)"></div></div>` : ''}
          <div class="dp-transfer-provider-meta">${providerChip(t)}</div>
        </td>
        <td data-role="transfer-status">${badge(transferDisplayStatus(t), t)}</td>
        <td data-role="transfer-progress">${progress(t.progress,t.status)}</td>
        <td class="sz">${fmtSize(t.size_bytes)}</td>
        <td class="sz">${fmtDate(t.created_at)}</td>
        <td onclick="event.stopPropagation()">
          <div class="actions">
            ${t.status==='downloading' || t.status==='queued' ? `<button class="btn btn-blue btn-sm" data-default-label="Pause" onclick="event.stopPropagation();pauseT(${t.id},this)" title="Pause this download">Pause</button>` : ''}
            ${t.status==='paused' ? `<button class="btn btn-blue btn-sm" data-default-label="Resume" onclick="event.stopPropagation();resumeT(${t.id},this)" title="Resume this download">Resume</button>` : ''}
          </div>
        </td>
      </tr>`;
    }).join('');

    requestAnimationFrame(() => {
      if (!document.getElementById('view-dashboard')?.classList.contains('active')) return;
      const fittedLimit = dashboardRecentLimit();
      if (fittedLimit !== _dashboardRecentFitLimit) {
        _dashboardRecentFitLimit = fittedLimit;
        loadRecent().catch(() => {});
      }
    });
  } catch(e) { console.error(e); }
  document.dispatchEvent(new CustomEvent('debridpulse:dashboard-recent-rendered'));
}

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
        entry => api('POST', '/torrents/add-magnet', {magnet: entry.value}, 30000)
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

// ── Torrents ───────────────────────────────────────────────────────────────

function activeDownloadFilterStatus() {
  return document.querySelector('#view-torrents .filter-tabs .ftab.active')?.dataset.dpStatus || '';
}

function downloadPaginationSummary(total, from, to) {
  const search = document.getElementById('torrent-search');
  if (search && search.value.trim()) {
    if (total <= 0) return 'No downloads match your search';
    if (total === 1 && from === 1 && to === 1) return 'Showing 1 matching download';
    if (from === 1 && to === total) return 'Showing all ' + total + ' matching downloads';
    return 'Showing ' + from + '–' + to + ' of ' + total + ' matching downloads';
  }
  const status = activeDownloadFilterStatus();
  const language = {
    '': ['No Items Added Yet', 'Showing 1 Added Item', n => 'Showing ' + n + ' Added Items'],
    downloading: ['No Active Downloads', '1 Active Download', n => n + ' Active Downloads'],
    paused: ['No Paused Downloads', '1 Paused Download', n => n + ' Paused Downloads'],
    processing: ['No Downloads Currently Processing', '1 Download Currently Processing', n => n + ' Downloads Currently Processing'],
    ready: ['No Downloads in Ready State', '1 Download in Ready State', n => n + ' Downloads in Ready State'],
    completed: ['No Downloads Completed Yet', '1 Download Completed', n => n + ' Downloads Completed'],
    error: ['No Downloads Have Errors', '1 Download Has Errors', n => n + ' Downloads Have Errors'],
  }[status];
  if (!language) return total === 1 ? '1 Download' : total + ' Downloads';
  return total <= 0 ? language[0] : total === 1 ? language[1] : language[2](total);
}

function renderTorrentPagination(total, limit, offset) {
  const normalizedTotal = Math.max(0, Number(total) || 0);
  const normalizedLimit = Math.max(1, Number(limit) || 25);
  const normalizedOffset = Math.max(0, Number(offset) || 0);
  const totalPages = Math.max(1, Math.ceil(normalizedTotal / normalizedLimit));
  const current = Math.min(totalPages, Math.floor(normalizedOffset / normalizedLimit) + 1);
  torrentPage = current;
  const info = document.getElementById('torrent-page-info');
  const buttons = document.getElementById('torrent-page-btns');
  if (!info || !buttons) return;
  const from = normalizedTotal === 0 ? 0 : normalizedOffset + 1;
  const to = Math.min(normalizedOffset + normalizedLimit, normalizedTotal);
  info.textContent = downloadPaginationSummary(normalizedTotal, from, to);
  const icon = name => window.DPIcons && typeof window.DPIcons.svg === 'function' ? window.DPIcons.svg(name) : '';
  const controls = [];
  if (current > 1) controls.push('<button type="button" class="dp-pager-btn" aria-label="Previous page" onclick="goToTorrentPage(' + (current - 1) + ')">' + icon('chevronLeft') + '</button>');
  controls.push('<button type="button" class="dp-pager-btn dp-pager-current" aria-current="page" aria-label="Page ' + current + ', current page">' + current + '</button>');
  if (current < totalPages) controls.push('<button type="button" class="dp-pager-btn" aria-label="Next page" onclick="goToTorrentPage(' + (current + 1) + ')">' + icon('chevronRight') + '</button>');
  buttons.innerHTML = controls.join('');
}

function setFilter(element, status) {
  document.querySelectorAll('#view-torrents .filter-tabs .ftab').forEach(tab => {
    tab.classList.remove('active');
    tab.setAttribute('aria-selected', 'false');
  });
  if (element) {
    element.classList.add('active');
    element.setAttribute('aria-selected', 'true');
  }
  currentFilter = status;
  torrentPage = 1;
  clearSelection();
  loadTorrents();
}

function updateDownloadsTrackedCopy(total) {
  const count = Math.max(0, Number(total) || 0);
  const copy = count === 1
    ? '1 download tracked. It followed instructions.'
    : count + ' downloads tracked. Most of them followed instructions.';
  const title = document.getElementById('torrent-card-title');
  const subtitle = title?.querySelector('.dp-downloads-subtitle');
  if (subtitle) subtitle.textContent = copy;
  if (title) title.setAttribute('aria-label', 'Download Queue. ' + copy);
}

function downloadEmptyMessage() {
  const search = document.getElementById('torrent-search');
  if (search && search.value.trim()) return 'No downloads match your search.';
  if (activeDownloadFilterStatus()) return 'No downloads match your current filters.';
  return 'No downloads yet. Add a link, magnet, or torrent file to get started.';
}

function onTorrentSearchInput() {
  currentTorrentSearch = (document.getElementById('torrent-search')?.value || '').trim();
  torrentPage = 1;
  if (_torrentSearchTimer) clearTimeout(_torrentSearchTimer);
  _torrentSearchTimer = setTimeout(() => {
    _torrentSearchTimer = null;
    loadTorrents().catch(()=>{});
  }, 250);
}

async function loadTorrents() {
  try {
    const params = new URLSearchParams();
    const _limit = Math.min(Math.max(parseInt(torrentPageSize)||25,15),100);
    const _offset = (torrentPage - 1) * _limit;
    params.set('limit', String(_limit));
    params.set('offset', String(_offset));
    if (currentFilter) params.set('status', currentFilter);
    if (currentTorrentSearch) params.set('search', currentTorrentSearch);
    const {items, total} = await api('GET', '/torrents?'+params.toString());
    torrentTotal = total ?? items.length;
    const tb = document.getElementById('t-tbody');
    renderTorrentPagination(torrentTotal, _limit, _offset);
    clearSelection();
    if (!items.length) {
      tb.innerHTML = `<tr><td colspan="8"><div class="empty"><div class="empty-icon" aria-hidden="true"></div>${downloadEmptyMessage()}</div></td></tr>`;
      return;
    }
    const icon = name => window.DPIcons && typeof window.DPIcons.svg === 'function' ? window.DPIcons.svg(name) : '';
    tb.innerHTML = items.map(t => `<tr class="dp-downloads-detail-row" data-torrent-id="${t.id}" data-status="${esc(t.status)}" tabindex="0" onclick="if(!event.target.closest('button,input,a,select,textarea,label,[role=button]'))showDetail(${t.id})" onkeydown="if(event.target===this&&(event.key==='Enter'||event.key===' ')){event.preventDefault();showDetail(${t.id})}">
      <td onclick="event.stopPropagation()"><input type="checkbox" class="t-chk" data-id="${t.id}" onchange="onCheckboxChange()"/></td>
      <td>
        <div class="t-name">${esc(t.name)||'(unnamed)'}</div>
        <div class="t-hash">${(t.hash||'').substring(0,16)}${t.hash?'…':''}</div>
      </td>
      <td class="sz dp-downloads-provider-cell">
        ${providerChip(t)}
        <span class="dp-transfer-source-label">${sourceLabel(t.source)}</span>
        ${t.label?`<span class="lbl-badge">🏷 ${esc(t.label)}</span>`:''}
      </td>
      <td data-role="transfer-status">${badge(transferDisplayStatus(t), t)}</td>
      <td data-role="transfer-progress">${progress(t.progress,t.status)}</td>
      <td class="sz">${fmtSize(t.size_bytes)}</td>
      <td class="sz">${fmtDate(t.created_at)}</td>
      <td onclick="event.stopPropagation()">
        <div class="actions">
          ${t.status==='ready' || t.status==='pending' ? `<button class="btn btn-primary btn-sm" data-default-label="Now" onclick="event.stopPropagation();downloadNow(${t.id},this)" title="Move to front of queue">${icon('download')}<span>Now</span></button>` : ''}
          ${t.status==='downloading' || t.status==='queued' ? `<button class="btn btn-blue btn-sm" data-default-label="Pause" onclick="event.stopPropagation();pauseT(${t.id},this)">Pause</button>` : ''}
          ${t.status==='paused' ? `<button class="btn btn-blue btn-sm" data-default-label="Resume" onclick="event.stopPropagation();resumeT(${t.id},this)">Resume</button>` : ''}
          ${t.status==='error'?`<button class="btn btn-blue btn-sm" data-default-label="Retry" onclick="event.stopPropagation();retryT(${t.id},this)">Retry</button>`:''}
          <button class="btn btn-danger btn-sm" data-default-label="Remove" onclick="event.stopPropagation();deleteT(${t.id},event,this)">Remove</button>
        </div>
      </td>
    </tr>`).join('');
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
  document.dispatchEvent(new CustomEvent('debridpulse:downloads-rendered'));
}

// Prevent SSE bursts and manual actions from stacking duplicate full renders.
loadStats = coalesceAsync(loadStats);
loadRecent = coalesceAsync(loadRecent);
loadTorrents = coalesceAsync(loadTorrents);

async function addMagnet() {
  const input = document.getElementById('t-magnet');
  const v = input.value.trim();
  if (!v) {
    openTorrentFilePicker();
    return;
  }
  try {
    const res = await api('POST','/torrents/add-magnet',{magnet:v}, 30000);
    if (res && res._duplicate && res._duplicate.action === 'skip') {
      toast('Already in queue: ' + (res.name || res._duplicate.reason), 'warn');
    } else if (res && res._duplicate && res._duplicate.action === 'warn') {
      toast('Added (possible duplicate)', 'warn');
    } else {
      toast('Magnet added!', 'success');
    }
    input.value = '';
    input.focus();
    loadTorrents();
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
}

async function importExisting(button) {
  setButtonPending(button, true, 'Importing…');

  try {
    const r =
      await api('POST','/torrents/import-existing');

    toast(
      `Imported ${r.imported} magnets from AllDebrid`,
      'success'
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

async function deleteT(id, eventObj, button) {
  eventObj?.stopPropagation();

  if (!confirm('Delete from AllDebrid and remove from list?')) {
    return;
  }

  setButtonPending(button, true, 'Deleting…');

  try {
    await api(
      'DELETE',
      `/torrents/${id}?from_alldebrid=true`
    );

    toast('Deleted','success');
    loadTorrents();
    loadStats();
  } catch(e) {
    toast(sanitizeErrorMsg(e.message),'error');
  } finally {
    setButtonPending(button, false);
  }
}

async function retryT(id, button) {
  setButtonPending(button, true, 'Retrying…');

  try {
    await api('POST',`/torrents/${id}/retry`);
    toast('Queued for retry','success');
    loadTorrents();
  } catch(e) {
    toast(sanitizeErrorMsg(e.message),'error');
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
      settingsData.paused = result.paused;

      if (!result.paused) {
        pausedTransferCount =
          Math.max(0, pausedTransferCount - 1);
      }

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

// ── Detail Modal ───────────────────────────────────────────────────────────
async function showDetail(id) {
  const overlay = document.getElementById('overlay');
  const modalTitle = document.getElementById('modal-title');
  const modalBody = document.getElementById('modal-body');

  if (modalTitle) {
    modalTitle.textContent = 'Loading…';
  }

  if (modalBody) {
    modalBody.innerHTML =
      '<div class="empty" style="padding:24px">Loading transfer details…</div>';
  }

  if (overlay) {
    overlay.classList.add('open');
  }

  try {
    const t = await api('GET',`/torrents/${id}`);

    if (modalTitle) {
      modalTitle.textContent =
        t.name || 'Torrent Details';
    }

    const providerPresentation = transferProviderPresentation(t);
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
      ${t.files&&t.files.length?`
        <div class="card dp-detail-section-card dp-detail-files-card">
          <div class="card-header">
            <span class="card-title">Files (${t.files.length})</span>
          </div>
          <div class="dp-detail-table-wrap">
            <table class="t-table">
              <thead><tr><th>Filename</th><th>Size</th><th>Status</th></tr></thead>
              <tbody>${t.files.map(f=>`<tr>
                <td class="dp-detail-filename">${esc(f.filename)}
                  ${f.blocked
                    ? `<span class="badge badge-error" style="font-size:9px;margin-left:6px">BLOCKED: ${esc(f.block_reason)}</span>`
                    : (f.block_reason ? `<div style="font-size:10px;color:var(--red);margin-top:4px">${esc(f.block_reason)}</div>` : '')}
                </td>
                <td class="sz">${fmtSize(f.size_bytes)}</td>
                <td>${badge(f.status, f)}</td>
              </tr>`).join('')}</tbody>
            </table>
          </div>
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
  } catch(e) {
    if (modalBody) {
      modalBody.innerHTML =
        `<div class="empty" style="padding:24px">Failed to load details: ${esc(sanitizeErrorMsg(e.message))}</div>`;
    }

    toast(sanitizeErrorMsg(e.message),'error');
  }
}

function closeModal(e) {
  if (!e || e.target === document.getElementById('overlay'))
    document.getElementById('overlay').classList.remove('open');
}

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
    if (settingsData && (settingsData.aria2_mode||'builtin')==='builtin') {
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

// ── Bulk selection ────────────────────────────────────────────────────────────
let _selectedIds = new Set();

function onCheckboxChange() {
  _selectedIds = new Set(
    [...document.querySelectorAll('.t-chk:checked')].map(el => parseInt(el.dataset.id))
  );
  const bar = document.getElementById('bulk-bar');
  const cnt = document.getElementById('bulk-count');
  if (_selectedIds.size > 0) {
    bar.classList.add('visible');
    cnt.textContent = _selectedIds.size + ' Selected';
  } else {
    bar.classList.remove('visible');
  }
  const all = document.getElementById('chk-all');
  const total = document.querySelectorAll('.t-chk').length;
  if (all) all.indeterminate = _selectedIds.size > 0 && _selectedIds.size < total;
}

function toggleAllCheckboxes(el) {
  document.querySelectorAll('.t-chk').forEach(c => {
    c.checked = el.checked;
  });
  onCheckboxChange();
}

function clearSelection() {
  _selectedIds.clear();
  document.querySelectorAll('.t-chk').forEach(c => c.checked = false);
  const all = document.getElementById('chk-all');
  if (all) { all.checked = false; all.indeterminate = false; }
  document.getElementById('bulk-bar').classList.remove('visible');
}

async function bulkAction(action, button) {
  if (!_selectedIds.size) return;

  const ids = [..._selectedIds];

  if (
    action === 'delete' &&
    !confirm(`Delete ${ids.length} torrents?`)
  ) {
    return;
  }

  const pendingLabels = {
    delete: 'Deleting…',
    reset: 'Resetting…',
    pause: 'Pausing…',
    resume: 'Resuming…',
  };

  setButtonPending(
    button,
    true,
    pendingLabels[action] || 'Working…'
  );

  try {
    const r =
      await api(
        'POST',
        '/torrents/bulk',
        {ids, action}
      );

    toast(
      `Done: ${r.ok} ok, ${r.failed} failed`,
      r.failed ? 'warn' : 'success'
    );

    clearSelection();
    loadTorrents();
    loadStats();
  } catch(e) {
    toast(e.message, 'error');
  } finally {
    setButtonPending(button, false);
    document.dispatchEvent(new CustomEvent('debridpulse:downloads-bulk-action-settled', {detail:{action}}));
  }
}

async function setLabel(id) {
  const label = prompt('Label (leave empty to clear):') ?? null;
  if (label === null) return;
  try {
    await api('PUT', `/torrents/${id}/label`, {label: label.trim(), priority: 0});
    toast('Label updated', 'success');
    loadTorrents();
  } catch(e) { toast(e.message, 'error'); }
}

// ── Events ─────────────────────────────────────────────────────────────────
let _allEvents = [];

async function loadEvents() {
  try {
    _allEvents = await api('GET','/events?limit=500');
    filterEvents();
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
}

function filterEvents() {
  const el = document.getElementById('event-list');
  const q   = (document.getElementById('ev-search')?.value || '').toLowerCase();
  const lvl = document.getElementById('ev-level')?.value || '';
  const evs = _allEvents.filter(ev => {
    if (lvl && ev.level !== lvl) return false;
    if (!q) return true;
    return (ev.message||'').toLowerCase().includes(q) ||
           (ev.torrent_name||'').toLowerCase().includes(q);
  });
  if (!evs.length) {
    el.innerHTML='<div class="empty">No events match the filter.</div>';
    document.dispatchEvent(new CustomEvent('debridpulse:activity-rendered'));
    return;
  }
  el.innerHTML = evs.map(ev=>`
    <div class="dp-activity-row">
      <div class="elevel dp-activity-level ${esc(ev.level)}"></div>
      <div class="dp-activity-copy"><div class="emsg dp-activity-message">${esc(ev.message)}</div>${ev.torrent_name?`<div class="ename dp-activity-transfer">${esc(ev.torrent_name)}</div>`:''}</div>
      <div class="etime dp-activity-time">${fmtDate(ev.created_at)}</div>
    </div>`).join('');
  document.dispatchEvent(new CustomEvent('debridpulse:activity-rendered'));
}

// ── Settings ───────────────────────────────────────────────────────────────

function toggleFilterFields() {
  const enabled = document.getElementById('s-filters_enabled')?.checked;
  const fields = document.getElementById('filter-fields');
  if (fields) {
    fields.style.opacity = enabled ? '' : '0.4';
    fields.style.pointerEvents = enabled ? '' : 'none';
  }
}

async function triggerFullSync(button) {
  setButtonPending(
    button,
    true,
    'Syncing…'
  );

  try {
    const r =
      await api(
        'POST',
        '/admin/full-sync'
      );

    toast(
      'Full sync: ' +
        r.updated +
        ' torrent(s) updated',
      r.updated > 0
        ? 'success'
        : 'info'
    );

    setTimeout(() => {
      loadStats();
      loadRecent();
    }, 1500);
  } catch(e) {
    toast(
      e.message,
      'error'
    );
  } finally {
    setButtonPending(button, false);
  }
}

async function saveSettings(button) {
  setButtonPending(button, true, 'Saving…');

  try {
    const activeTab =
      getActiveSettingsTab();

    const d =
      getFormSettings();

    settingsData =
      await api('PUT','/settings',d);

    renderSettings();
    switchSettingsTab(activeTab);
    updateAria2ngLink();

    toast(
      'Settings saved!',
      'success'
    );

    checkConnections();
    loadAria2SpeedLimit();
  } catch(e) {
    toast(
      sanitizeErrorMsg(e.message),
      'error'
    );
  } finally {
    setButtonPending(button, false);
  }
}

async function testDiscord(button) {
  const activeTab =
    getActiveSettingsTab();

  let settingsApplied = false;
  let rendered = false;

  setButtonPending(
    button,
    true,
    'Testing…'
  );

  try {
    const current =
      getFormSettings();

    settingsData =
      await api(
        'PUT',
        '/settings',
        current
      );

    settingsApplied = true;

    await api(
      'POST',
      '/settings/test-discord'
    );

    renderSettings();
    switchSettingsTab(activeTab);
    rendered = true;

    toast(
      'Discord notification sent ✓',
      'success'
    );
  } catch(e) {
    toast(
      'Discord: ' + e.message,
      'error'
    );
  } finally {
    setButtonPending(button, false);

    if (settingsApplied && !rendered) {
      renderSettings();
      switchSettingsTab(activeTab);
    }
  }
}

async function testAD(button) {
  setButtonPending(
    button,
    true,
    'Testing…'
  );

  try {
    const r =
      await api(
        'POST',
        '/settings/test-alldebrid'
      );

    toast(
      `AllDebrid: connected as ${r.username} ${r.isPremium?'(Premium)':'(Free)'}✓`,
      'success'
    );

    setDot(
      'api',
      'ok',
      `AllDebrid: ${r.username}`
    );

    _updatePremiumLabel(r);
  } catch(e) {
    toast(
      'AllDebrid: ' + e.message,
      'error'
    );

    setDot(
      'api',
      'error',
      'AllDebrid: error'
    );
  } finally {
    setButtonPending(button, false);
  }
}

function _updatePremiumLabel(r) {
  const row = document.getElementById('premium-row');
  const lbl = document.getElementById('lbl-premium');
  if (!row || !lbl) return;
  if (!r || !r.isPremium) { row.style.display = 'none'; return; }
  // AllDebrid user object has premiumUntil as unix timestamp
  const until = r.premiumUntil || r.premium_until || 0;
  if (!until) { row.style.display = 'none'; return; }
  const d = new Date(until * 1000);
  const dd = String(d.getDate()).padStart(2,'0');
  const mm = String(d.getMonth()+1).padStart(2,'0');
  const yyyy = d.getFullYear();
  const days = Math.ceil((d - Date.now()) / 86400000);
  const daysLabel = days > 0 ? `(${days} days remaining)` : '(expired)';
  lbl.innerHTML = `<span class="dp-provider-premium-until">AllDebrid Premium until ${dd}.${mm}.${yyyy}</span><span class="dp-provider-premium-days">${daysLabel}</span>`;
  row.style.display = '';
}



async function testAria2(button) {
  const activeTab =
    getActiveSettingsTab();

  let settingsApplied = false;
  let rendered = false;

  setButtonPending(
    button,
    true,
    'Testing…'
  );

  try {
    const current =
      getFormSettings();

    settingsData =
      await api(
        'PUT',
        '/settings',
        current
      );

    settingsApplied = true;

    const r =
      await api(
        'POST',
        '/settings/test-aria2'
      );

    renderSettings();
    switchSettingsTab(activeTab);
    rendered = true;

    renderAria2Diagnostics(
      r.diagnostics || null
    );

    toast(
      `aria2: ${r.version||'online'} ✓`,
      'success'
    );

    setDot(
      'aria2',
      'ok',
      `aria2: ${r.version||'online'}`
    );
  } catch(e) {
    toast(
      'aria2: ' + e.message,
      'error'
    );

    setDot(
      'aria2',
      'error',
      'aria2: error'
    );
  } finally {
    setButtonPending(button, false);

    if (settingsApplied && !rendered) {
      renderSettings();
      switchSettingsTab(activeTab);
    }
  }
}

function renderAria2Diagnostics(diag) {
  const el = document.getElementById('aria2-memory-diagnostics');
  if (!el) return;
  if (!diag) {
    el.textContent = '';
    return;
  }
  const opts = diag.global_options || {};
  const limits = diag.query_limits || {};
  el.innerHTML =
    `<b>aria2 memory diagnostics</b><br>` +
    `Active: ${diag.active_count ?? 0} · Waiting: ${diag.waiting_count ?? 0} · Stopped: ${diag.stopped_count ?? 0}<br>` +
    `max-download-result: ${esc(opts['max-download-result'] || 'n/a')} · keep-unfinished-download-result: ${esc(opts['keep-unfinished-download-result'] || 'n/a')}<br>` +
    `query window — waiting: ${limits.waiting ?? 'n/a'} · stopped: ${limits.stopped ?? 'n/a'}`;
}

function renderAria2Runtime(data) {
  const el = document.getElementById('aria2-runtime-status');
  if (!el) return;
  if (!data) {
    el.textContent = 'Runtime status not loaded yet.';
    return;
  }
  const mode = data.mode || 'external';
  const state = data.running ? 'Running' : (mode === 'builtin' ? 'Stopped' : 'External');
  const rpc = data.rpc_ok ? 'RPC online' : (mode === 'builtin' ? 'RPC offline' : 'External RPC');
  const version = data.version ? ` · v${data.version}` : '';
  const uptime = data.uptime_seconds ? ` · uptime ${Math.floor(data.uptime_seconds / 60)}m` : '';
  const secret = data.secret_managed ? ' · internal secret managed' : '';
  const dir = data.download_dir ? `<br>Download folder: ${esc(data.download_dir)}` : '';
  const diag = data.diagnostics || {};
  const counts = diag && !diag.error
    ? `<br>Active: ${diag.active_count ?? 0} · Waiting: ${diag.waiting_count ?? 0} · Stopped: ${diag.stopped_count ?? 0}`
    : '';
  const err = data.last_error ? `<br><span style="color:var(--red)">${esc(data.last_error)}</span>` : '';
  el.innerHTML = `<b>${esc(state)}</b> · ${esc(mode)} · ${esc(rpc)}${esc(version)}${esc(uptime)}${secret}<br>${esc(data.rpc_url || '')}${counts}${err}`;
  el.innerHTML += dir;
  if (data.last_output) el.innerHTML += `<br><small>${esc(data.last_output)}</small>`;
  renderAria2Diagnostics(diag && !diag.error ? diag : null);
}

function aria2StatusLabel(status) {
  const map = {active:'Downloading', waiting:'Waiting', paused:'Paused', complete:'Complete', error:'Error', removed:'Removed'};
  const cls = status === 'active' ? 'downloading' : status === 'complete' ? 'completed' : status === 'error' ? 'error' : status === 'paused' ? 'paused' : 'queued';
  return `<span class="badge badge-${cls}">${esc(map[status] || status || 'Unknown')}</span>`;
}

function renderAria2Downloads(data) {
  const el = document.getElementById('aria2-downloads');
  if (!el) return;
  if (!data || !Array.isArray(data.items)) {
    el.innerHTML = '<div class="empty">Queue not loaded yet.</div>';
    return;
  }
  const summary = data.summary || {};
  const items = data.items || [];
  const ordered = items.slice().sort((a,b) => {
    const weight = {active:0, waiting:1, paused:2, error:3, complete:4};
    return (weight[a.status] ?? 9) - (weight[b.status] ?? 9);
  });
  const header = `
    <div class="aria2-summary">
      <span class="aria2-chip">Active: ${summary.active ?? 0}</span>
      <span class="aria2-chip">Waiting: ${summary.waiting ?? 0}</span>
      <span class="aria2-chip">Stopped: ${summary.stopped ?? 0}</span>
      <span class="aria2-chip">Speed: ${fmtSpeed(summary.download_speed || 0)}</span>
      <span class="aria2-chip">Remaining: ${fmtSize(summary.remaining_length || 0)}</span>
    </div>`;
  if (!ordered.length) {
    el.innerHTML = header + '<div class="empty">No aria2 jobs currently visible.</div>';
    return;
  }
  el.innerHTML = header + ordered.map(job => {
    const canPause = job.status === 'active' || job.status === 'waiting';
    const canResume = job.status === 'paused';
    const files = (job.files || []).slice(0, 4).map(file => `
      <div title="${esc(file.path || '')}">
        ${esc(file.name || file.path || 'file')} · ${Math.max(0, file.progress || 0).toFixed(1)}% · ${fmtSize(file.completed_length || 0)} / ${fmtSize(file.length || 0)}
      </div>`).join('');
    const more = (job.files || []).length > 4 ? `<div>+ ${(job.files || []).length - 4} more file(s)</div>` : '';
    const error = job.error_message ? `<div class="aria2-error">${esc((job.error || {}).category || '')} ${esc(job.error_message)}</div>` : '';
    return `
      <div class="aria2-job">
        <div class="aria2-job-top">
          <div class="aria2-job-title">
            <div class="aria2-job-name" title="${esc(job.name || '')}">${esc(job.name || job.gid || 'aria2 job')}</div>
            <div class="aria2-job-meta" title="${esc(job.path || '')}">${esc(job.gid || '')}${job.path ? ' · ' + esc(job.path) : ''}</div>
          </div>
          <div class="aria2-actions">
            ${canPause ? `<button class="btn btn-ghost btn-sm" onclick="aria2DownloadAction('${esc(job.gid)}','pause',this)">Pause</button>` : ''}
            ${canResume ? `<button class="btn btn-blue btn-sm" onclick="aria2DownloadAction('${esc(job.gid)}','resume',this)">Resume</button>` : ''}
            <button class="btn btn-danger btn-sm" onclick="aria2DownloadAction('${esc(job.gid)}','remove',this)">Remove</button>
          </div>
        </div>
        <div>${progress(job.progress || 0, job.status === 'complete' ? 'completed' : 'downloading')}</div>
        <div class="aria2-job-grid">
          <div><div class="aria2-k">Status</div><div class="aria2-v">${aria2StatusLabel(job.status)}</div></div>
          <div><div class="aria2-k">Speed</div><div class="aria2-v">${fmtSpeed(job.download_speed || 0)}</div></div>
          <div><div class="aria2-k">Done</div><div class="aria2-v">${fmtSize(job.completed_length || 0)} / ${fmtSize(job.total_length || 0)}</div></div>
          <div><div class="aria2-k">Remaining</div><div class="aria2-v">${fmtSize(job.remaining_length || 0)}</div></div>
        </div>
        ${error}
        ${(files || more) ? `<div class="aria2-file-list">${files}${more}</div>` : ''}
      </div>`;
  }).join('');
}

async function loadAria2Downloads() {
  try {
    const data =
      await api(
        'GET',
        '/aria2/downloads'
      );

    renderAria2Downloads(data);

    return data;
  } catch(e) {
    const el =
      document.getElementById(
        'aria2-downloads'
      );

    if (el) {
      el.innerHTML =
        `<div class="aria2-error">Queue error: ${esc(e.message)}</div>`;
    }

    throw e;
  }
}

loadAria2Downloads =
  coalesceAsync(loadAria2Downloads);

async function refreshAria2Downloads(button) {
  setButtonPending(
    button,
    true,
    'Refreshing…'
  );

  try {
    await loadAria2Downloads();
  } catch (_) {
    // loadAria2Downloads owns its visible error state.
  } finally {
    setButtonPending(button, false);
  }
}

async function aria2DownloadAction(gid, action, button) {
  const pendingLabels = {
    pause: 'Pausing…',
    resume: 'Resuming…',
    remove: 'Removing…',
  };

  setButtonPending(
    button,
    true,
    pendingLabels[action] || 'Working…'
  );

  try {
    await api(
      'POST',
      `/aria2/downloads/${encodeURIComponent(gid)}/${action}`
    );

    toast(
      `aria2 ${action} sent`,
      'success'
    );

    await loadAria2Downloads();
    await loadAria2Runtime();
  } catch(e) {
    toast(
      `aria2 ${action}: ${e.message}`,
      'error'
    );
  } finally {
    setButtonPending(button, false);
    document.dispatchEvent(new CustomEvent('debridpulse:aria2-engine-action-settled', {detail:{gid, action}}));
  }
}

async function loadAria2Runtime() {
  try {
    const data = await api('GET', '/aria2/runtime');
    renderAria2Runtime(data);
    const badge = document.getElementById('aria2-speed-badge');
    if (badge) {
      const isBuiltin = (data.mode || '') === 'builtin';

      if (settingsData) {
        _aria2BadgeState.limitBps =
          parseInt(settingsData.aria2_max_download_limit) || 0;
        _aria2BadgeState.maxDl =
          parseInt(
            settingsData.max_concurrent_downloads ??
            settingsData.aria2_max_active_downloads
          ) || 3;
      }

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
        updateAria2TopbarBadge({
          maxDl: _aria2BadgeState.maxDl,
          externalControl: true,
        });
        loadAria2TopbarStat().catch(function(){});
      }
    }
    return data;
  } catch(e) {
    const el = document.getElementById('aria2-runtime-status');
    if (el) el.innerHTML = `<span style="color:var(--red)">Runtime error: ${esc(e.message)}</span>`;
    throw e;
  }
}

async function aria2RuntimeAction(action, button) {
  const pendingLabels = {
    start: 'Starting…',
    restart: 'Restarting…',
    stop: 'Stopping…',
    apply: 'Applying…',
  };

  setButtonPending(
    button,
    true,
    pendingLabels[action] || 'Working…'
  );

  try {
    const current =
      getFormSettings();

    settingsData =
      await api(
        'PUT',
        '/settings',
        current
      );

    const data =
      await api(
        'POST',
        `/aria2/runtime/${action}`
      );

    renderAria2Runtime(data);
    loadAria2Downloads().catch(()=>{});

    toast(
      `aria2 ${action} complete`,
      'success'
    );
  } catch(e) {
    toast(
      `aria2 ${action}: ${e.message}`,
      'error'
    );

    loadAria2Runtime().catch(()=>{});
  } finally {
    setButtonPending(button, false);
  }
}

async function runAria2Housekeeping(button) {
  setButtonPending(
    button,
    true,
    'Cleaning…'
  );

  try {
    const current =
      getFormSettings();

    settingsData =
      await api(
        'PUT',
        '/settings',
        current
      );

    const r =
      await api(
        'POST',
        '/settings/aria2-housekeeping'
      );

    renderAria2Diagnostics(
      r.diagnostics || null
    );

    toast(
      'aria2 cleanup finished',
      'success'
    );
  } catch(e) {
    toast(
      e.message,
      'error'
    );
  } finally {
    setButtonPending(button, false);
  }
}

async function uploadDiscordAvatar(input) {
  const file = input.files[0];
  if (!file) return;
  const formData = new FormData();
  formData.append('file', file);
  try {
    const resp = await fetch('/api/settings/upload-avatar', {method:'POST', body: formData});
    const data = await resp.json();
    if (!resp.ok) { toast(data.detail || 'Upload failed', 'error'); return; }
    // Discord requires a real HTTP URL, not a data URI
    // The server saves the file and returns the public URL
    document.getElementById('s-discord_avatar_url').value = data.url;
    showAvatarPreview(data.url, file.name, data.size_bytes);
    toast('Avatar uploaded — URL: ' + data.url, 'success');
    if (data.warning) toast(data.warning, 'warn');
  } catch(e) { toast(e.message, 'error'); }
  input.value = '';
}

function showAvatarPreview(src, name, bytes) {
  const preview = document.getElementById('avatar-preview');
  const img = document.getElementById('avatar-preview-img');
  const lbl = document.getElementById('avatar-preview-label');
  if (!preview) return;
  img.src = src;
  lbl.textContent = (name || 'Custom avatar') + (bytes > 0 ? ' (' + Math.round(bytes/1024) + ' KB)' : '');
  preview.style.display = 'flex';
}

function clearDiscordAvatar() {
  document.getElementById('s-discord_avatar_url').value = '';
  const preview = document.getElementById('avatar-preview');
  if (preview) preview.style.display = 'none';
}

async function runDeepSync() {
  try {
    toast('Running deep sync…', 'info');
    const r = await api('POST', '/admin/deep-sync');
    toast(`Deep sync done in ${r.elapsed_seconds}s ✓`, 'success');
    loadTorrents(); loadStats();
  } catch(e) { toast(e.message, 'error'); }
}

async function triggerBackup() {
  try {
    toast('Running backup…', 'info');
    const r = await api('POST', '/admin/backup');
    if (r.skipped) { toast('Backup disabled in settings', 'warn'); return; }
    toast(`Backup done: ${r.backed_up.join(', ')} (${r.rotated} old removed)`, 'success');
    loadBackupList();
  } catch(e) { toast(e.message, 'error'); }
}

async function loadBackupList() {
  try {
    const r = await api('GET', '/admin/backups');
    const el = document.getElementById('backup-list');
    if (!el) return;
    if (!r.backups.length) { el.textContent = 'No backups found.'; return; }
    el.innerHTML = r.backups.map(b =>
      `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)">
        <span>${esc(b.name)}</span>
        <span style="color:var(--text3)">${esc((b.files||[]).join(', '))} — ${Math.round(Number(b.size_bytes||0)/1024)} KB</span>
      </div>`
    ).join('');
  } catch(e) { toast(e.message, 'error'); }
}

async function triggerDatabaseBackup() {
  try {
    toast('Running database backup…', 'info');
    const r = await api('POST', '/admin/database/backup');
    if (r.skipped) { toast('Database backup disabled in settings', 'warn'); return; }
    toast(`Database backup done (${Object.values(r.tables || {}).reduce((a, b) => a + b, 0)} rows exported)`, 'success');
    loadDatabaseBackupList();
  } catch(e) { toast(e.message, 'error'); }
}

async function loadDatabaseBackupList() {
  try {
    const r = await api('GET', '/admin/database/backups');
    const el = document.getElementById('db-backup-list');
    if (!el) return;
    if (!r.backups.length) { el.textContent = 'No database backups found.'; return; }
    el.innerHTML = r.backups.map(b =>
      `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)">
        <span>${esc(b.name)}</span>
        <span style="color:var(--text3)">${esc((b.files||[]).join(', '))} — ${Math.round(Number(b.size_bytes||0)/1024)} KB</span>
      </div>`
    ).join('');
  } catch(e) { toast(e.message, 'error'); }
}

async function wipeDatabase(button) {
  const enabled =
    document
      .getElementById(
        's-db_wipe_enabled'
      )
      ?.checked;

  if (!enabled) {
    toast(
      'Enable database wipe in settings first',
      'warn'
    );
    return;
  }

  if (
    !confirm(
      'This will remove all database rows. Continue?'
    )
  ) {
    return;
  }

  const confirmText =
    prompt(
      'Type WIPE to confirm database wipe'
    );

  if (confirmText !== 'WIPE') return;

  // Start pending state only after explicit operator confirmation.
  setButtonPending(
    button,
    true,
    'Wiping…'
  );

  try {
    toast(
      'Wiping database…',
      'warn'
    );

    const r =
      await api(
        'POST',
        '/admin/database/wipe',
        {confirm: true}
      );

    if (
      r.backup &&
      !r.backup.skipped
    ) {
      toast(
        'Database wiped. Pre-wipe backup created.',
        'success'
      );
    } else {
      toast(
        'Database wiped.',
        'success'
      );
    }

    loadDatabaseBackupList();
    loadStats().catch(()=>{});
    loadRecent().catch(()=>{});

    if (
      document
        .getElementById('view-torrents')
        ?.classList.contains('active')
    ) {
      loadTorrents().catch(()=>{});
    }
  } catch(e) {
    toast(
      e.message,
      'error'
    );
  } finally {
    setButtonPending(button, false);
  }
}

async function sendStatsReport(button) {
  const hours = parseInt(document.getElementById('stats-report-hours')?.value || '24', 10);

  setButtonPending(
    button,
    true,
    'Sending…'
  );

  try {
    const r = await api('POST', `/stats/report/send?hours=${hours}`);

    toast(
      `Report sent via webhook (${r.hours}h) ✓`,
      'success'
    );
  } catch(e) {
    toast(e.message, 'error');
  } finally {
    setButtonPending(button, false);
  }
}

async function loadComprehensiveStats() {
  const el = document.getElementById('comprehensive-stats');
  if (!el) return;
  const hours = parseInt(document.getElementById('stats-report-hours')?.value || '24');
  el.innerHTML = '<div style="color:var(--text2);font-size:12px">⏳ Loading…</div>';
  try {
    const r = await api('GET', `/stats/comprehensive?hours=${hours}`);
    const t = r.torrents || {};
    const d = r.downloads || {};
    const f = r.files || {};
    const ev = r.events || {};
    const fmtBytes = b => b > 1e9 ? (b/1e9).toFixed(2)+' GB' : b > 1e6 ? (b/1e6).toFixed(1)+' MB' : (b/1024).toFixed(0)+' KB';
    const fmtDur = s => s > 3600 ? `${(s/3600).toFixed(1)}h` : s > 60 ? `${Math.floor(s/60)}m` : s+'s';
    el.innerHTML = `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px">
        ${[
          ['Total Downloads', t.total||0, ''],
          ['Completed', t.completed||0, 'var(--green)'],
          ['Errors', t.errors||0, 'var(--red)'],
          ['Success Rate', t.success_rate_pct != null ? t.success_rate_pct+'%' : '—', 'var(--accent)'],
          ['Downloaded', fmtBytes(d.total_bytes||0), 'var(--blue)'],
          ['Avg Size', fmtBytes(d.avg_bytes||0), ''],
          ['Avg Duration', fmtDur(d.avg_duration_sec||0), ''],
          ['Total Files', f.total||0, ''],
          ['Blocked Files', f.blocked||0, 'var(--yellow)'],
          ['Total Retries', f.retry_total||0, ''],
          ['Error Events', ev.error||0, 'var(--red)'],
          ['Warn Events', ev.warn||0, 'var(--yellow)'],
        ].map(([k,v,c]) => `<div style="background:var(--surface2);padding:8px 10px;border-radius:6px">
          <div style="font-size:9px;text-transform:uppercase;color:var(--text2);font-weight:700">${k}</div>
          <div style="font-size:20px;font-weight:800;color:${c||'var(--text)'}">${v}</div>
        </div>`).join('')}
      </div>
      ${r.daily_trend?.length ? `<div style="font-size:11px;color:var(--text2);margin-top:8px"><b>Daily completions (last ${Math.min(14, hours/24|0)} days):</b><br>${r.daily_trend.map(d=>`${esc(d.date)}: ${Number(d.cnt)||0}`).join(' · ')}</div>` : ''}
      ${Object.keys(t.sources||{}).length ? `<div style="font-size:11px;color:var(--text2);margin-top:6px"><b>Sources:</b> ${Object.entries(t.sources||{}).map(([k,v])=>`${esc(k)}: ${Number(v)||0}`).join(', ')}</div>` : ''}
    `;
  } catch(e) {
    el.innerHTML = `<span style="color:var(--red)">✗ ${esc(e.message)}</span>`;
  }
}

async function exportStats() {
  const hours = parseInt(document.getElementById('stats-report-hours')?.value || '24');
  window.open(`/api/stats/export?hours=${hours}`, '_blank');
}

async function triggerStatsSnapshot(button) {
  setButtonPending(
    button,
    true,
    'Taking…'
  );

  try {
    await api(
      'POST',
      '/stats/snapshot'
    );

    toast(
      'Stats snapshot taken',
      'success'
    );
  } catch(e) {
    toast(e.message, 'error');
  } finally {
    setButtonPending(button, false);
  }
}



// ── Init ───────────────────────────────────────────────────────────────────
(async()=>{
  setDot('api',   'check', 'AllDebrid: checking…');
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
  // checkConnections() owns the provider-specific status surface.

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

            const patchedExtraction =
              patchExtractionTransferEvent(payload);

            const patchedProgress =
              patchedExtraction
                ? false
                : patchProgressOnlyTransferEvent(payload);

            if (!patchedExtraction && !patchedProgress) {
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
            } else if (!progressStatsTimer) {
              progressStatsTimer = setTimeout(
                ()=>{
                  progressStatsTimer = null;
                  loadStats().catch(()=>{});
                  if (patchedExtraction && payload.extraction_status !== 'extracting') {
                    if (document.getElementById('view-torrents')?.classList.contains('active')) {
                      loadTorrents().catch(()=>{});
                    }
                    if (document.getElementById('view-dashboard')?.classList.contains('active')) {
                      loadRecent().catch(()=>{});
                    }
                  }
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


// ── Extraction Password List ─────────────────────────────────────────────────
// Internal state: real array, may contain empty strings during editing.
// Only filtered on save (saveSettings reads the hidden field which is kept in sync).
var _extractionPasswords = [];

function _extractionPasswordsFromHidden() {
  var hidden = document.getElementById('s-extraction_password');
  if (!hidden || !hidden.value.trim()) return [];
  return hidden.value.split('\n').map(function(p) { return p.trim(); });
}

function _extractionPasswordsSyncToHidden() {
  var hidden = document.getElementById('s-extraction_password');
  if (hidden) hidden.value = _extractionPasswords.join('\n');
}

function renderExtractionPasswordList() {
  var list = document.getElementById('extraction-pw-list');
  if (!list) return;
  if (!_extractionPasswords.length) {
    list.innerHTML = '<div style="color:var(--text3);font-size:12px;padding:4px 0">No passwords configured.</div>';
    return;
  }
  list.innerHTML = _extractionPasswords.map(function(pw, i) {
    return '<div style="display:flex;gap:6px;align-items:center;margin-bottom:4px">' +
      '<input class="input" style="flex:1;font-size:13px" value="' + esc(pw) + '" ' +
        'oninput="updateExtractionPassword(' + i + ',this.value)" placeholder="password"/>' +
      '<button class="btn btn-danger btn-sm" onclick="removeExtractionPassword(' + i + ')" ' +
        'type="button" title="Remove" style="flex-shrink:0">✕</button>' +
    '</div>';
  }).join('');
}

function addExtractionPassword() {
  _extractionPasswords.push('');
  _extractionPasswordsSyncToHidden();
  renderExtractionPasswordList();
  // Focus the new (last) input after DOM update
  setTimeout(function() {
    var inputs = document.querySelectorAll('#extraction-pw-list input');
    if (inputs.length) inputs[inputs.length - 1].focus();
  }, 30);
}

function removeExtractionPassword(idx) {
  _extractionPasswords.splice(idx, 1);
  _extractionPasswordsSyncToHidden();
  renderExtractionPasswordList();
}

function updateExtractionPassword(idx, val) {
  _extractionPasswords[idx] = val;
  _extractionPasswordsSyncToHidden();
}

function initExtractionPasswordList() {
  // Called when the Extract tab is activated or settings are loaded.
  // Loads existing passwords from the hidden field into the array state.
  _extractionPasswords = _extractionPasswordsFromHidden();
  renderExtractionPasswordList();
}

// ── Priority Queue ─────────────────────────────────────────────────────────

async function setTorrentPriority(torrentId, priority) {
  try {
    await api('PATCH', `/torrents/${torrentId}/priority`, {priority});
    loadTorrents();
  } catch(e) { toast(e.message, 'error'); }
}

// ── AllDebrid Orphan Cleanup ───────────────────────────────────────────────────

async function cleanupAlldebridOrphans() {
  var btn = document.getElementById('btn-cleanup-orphans');
  if (btn) { btn.disabled = true; btn.textContent = 'Cleaning…'; }
  try {
    var res = await api('POST', '/admin/cleanup-alldebrid-orphans', {}, 60000);
    toast(
      res.deleted > 0
        ? res.deleted + ' orphan magnet(s) removed from AllDebrid'
        : 'No orphaned magnets found on AllDebrid',
      res.deleted > 0 ? 'success' : 'info'
    );
    loadTorrents();
  } catch(e) { toast(sanitizeErrorMsg(e.message), 'error'); }
  finally {
    if (btn) { btn.disabled = false; btn.textContent = '🧹 Clean AD Orphans'; }
  }
}

// ── Download Now / Priority Queue ────────────────────────────────────────────

async function downloadNow(torrentId, button) {
  // Set priority very high so this torrent is dispatched next
  setButtonPending(button, true, 'Queuing…');

  try {
    await api(
      'PATCH',
      '/torrents/' + torrentId + '/priority',
      {priority: 100},
      10000
    );

    toast('Moved to front of queue', 'success');
    loadTorrents();
  } catch(e) {
    toast(sanitizeErrorMsg(e.message), 'error');
  } finally {
    setButtonPending(button, false);
  }
}

async function setTorrentPriority(torrentId, priority) {
  try {
    await api('PATCH', '/torrents/' + torrentId + '/priority', {priority: parseInt(priority)||0}, 10000);
    loadTorrents();
  } catch(e) { toast(sanitizeErrorMsg(e.message), 'error'); }
}



// ── Drag & Drop Priority Reordering ───────────────────────────────────────────

var _dragSrcId = null;

function onTorrentDragStart(e, torrentId) {
  _dragSrcId = torrentId;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', String(torrentId));
  e.currentTarget.style.opacity = '0.5';
}

function onTorrentDragEnd(e) {
  e.currentTarget.style.opacity = '';
  document.querySelectorAll('#t-tbody tr').forEach(function(r) {
    r.classList.remove('drag-over');
  });
}

function onTorrentDragOver(e, torrentId) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  document.querySelectorAll('#t-tbody tr').forEach(function(r) {
    r.classList.remove('drag-over');
  });
  e.currentTarget.classList.add('drag-over');
}

async function onTorrentDrop(e, targetId) {
  e.preventDefault();
  e.currentTarget.classList.remove('drag-over');
  if (!_dragSrcId || _dragSrcId === targetId) return;
  // Move dragged item above the target: boost its priority by 1 relative to target
  try {
    // Get current rows to compute new priority
    var rows = Array.from(document.querySelectorAll('#t-tbody tr[data-torrent-id]'));
    var srcIdx  = rows.findIndex(function(r) { return parseInt(r.dataset.torrentId) === _dragSrcId; });
    var tgtIdx  = rows.findIndex(function(r) { return parseInt(r.dataset.torrentId) === targetId; });
    var newPriority = tgtIdx < srcIdx ? 10 : -10;
    await api('PATCH', '/torrents/' + _dragSrcId + '/priority', {priority: newPriority}, 10000);
    loadTorrents();
  } catch(e) {
    toast(sanitizeErrorMsg(e.message), 'error');
  }
  _dragSrcId = null;
}

// ── Auto-Recovery ─────────────────────────────────────────────────────────────

async function runRecovery() {
  try {
    var res = await api('POST', '/recovery/run', {}, 30000);
    var r = res.result || {};
    toast(
      'Recovery done — ' +
      r.orphaned_queued_files + ' orphaned, ' +
      r.missed_completions + ' completions fixed, ' +
      (r.deadlock_reset ? 'deadlock reset' : 'no deadlock'),
      'success'
    );
  } catch(e) { toast(sanitizeErrorMsg(e.message), 'error'); }
}

// ── Speed Limit ───────────────────────────────────────────────────────────────

async function loadAria2SpeedLimit() {
  try {
    var data = await api('GET', '/aria2/global-options', null, 10000);
    var externalControl = !!data.global_options_read_only;
    var bps   = parseInt(data.max_download_speed || 0);
    var maxDl = parseInt(data.max_concurrent_downloads || 0)
                || (settingsData && settingsData.aria2_max_active_downloads)
                || 3;

    // ── Sync settingsData so PUT /settings uses the live value ───────────
    if (settingsData) {
      settingsData.aria2_max_active_downloads = maxDl;
      settingsData.max_concurrent_downloads   = maxDl;
      if (!externalControl) {
        settingsData.aria2_max_download_limit = bps;
      }
    }
    // ── Sync Settings-page inputs (Downloads → Settings, bidirectional) ──
    var inMad = document.getElementById('s-aria2_max_active_downloads');
    if (inMad) inMad.value = maxDl;

    // ── Sync speed preset in Downloads panel ─────────────────────────────
    var sel = document.getElementById('aria2-speed-preset');
    var st  = document.getElementById('aria2-speed-status');
    if (sel) {
      var found = false;
      for (var i = 0; i < sel.options.length; i++) {
        if (sel.options[i].value !== 'custom' && parseInt(sel.options[i].value || 0) === bps) {
          sel.value = sel.options[i].value;
          found = true; break;
        }
      }
      if (!found) {
        sel.value = 'custom';
        var ci = document.getElementById('aria2-speed-custom');
        var cb = document.getElementById('aria2-speed-apply');
        if (ci) { ci.style.display = ''; ci.value = Math.round(bps / 1024); }
        if (cb)   cb.style.display = '';
      }
      if (st) st.textContent = '(' + fmtSpeedCap(bps) + ')';
    }

    // ── Sync Max DL preset in Downloads panel ─────────────────────────────
    var msel = document.getElementById('aria2-maxdl-preset');
    if (msel) {
      var mfound = false;
      for (var j = 0; j < msel.options.length; j++) {
        if (parseInt(msel.options[j].value) === maxDl) {
          msel.value = msel.options[j].value;
          mfound = true; break;
        }
      }
      if (!mfound) msel.value = '3';
    }

    // ── Update topbar badge ───────────────────────────────────────────────
    updateAria2TopbarBadge({
      limitBps: bps,
      maxDl: maxDl,
      externalControl: externalControl,
    });

  } catch (e) { /* aria2 not connected — silently ignore */ }
}

async function applyAria2SpeedPreset(val) {
  var ci = document.getElementById('aria2-speed-custom');
  var cb = document.getElementById('aria2-speed-apply');
  if (val === 'custom') {
    if (ci) ci.style.display=''; if (cb) cb.style.display=''; return;
  }
  if (ci) ci.style.display='none'; if (cb) cb.style.display='none';
  await _setAria2Speed(parseInt(val||0));
}

async function applyAria2SpeedCustom() {
  var ci = document.getElementById('aria2-speed-custom');
  var kbps = parseInt((ci&&ci.value)||0);
  await _setAria2Speed(kbps * 1024);
}

async function _setAria2Speed(bps) {
  var st = document.getElementById('aria2-speed-status');

  if (settingsData && (settingsData.aria2_mode || 'builtin') !== 'builtin') {
    if (st) {
      st.style.color = 'var(--text2)';
      st.textContent = 'Externally Controlled';
    }
    updateAria2TopbarBadge({externalControl: true});
    return false;
  }

  if (st) { st.style.color='var(--text2)'; st.textContent='Applying…'; }
  try {
    await api('POST', '/aria2/global-options', {max_download_speed: bps});
    // Keep settingsData in sync so subsequent PUT /settings calls don't
    // overwrite this value with the stale cached number.
    if (settingsData) settingsData.aria2_max_download_limit = bps;
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
function updateAria2Badge(activeCount) {
  var badge = document.getElementById('nb-aria2-active');
  if (!badge) return;
  badge.textContent = activeCount;
  badge.style.display = activeCount > 0 ? '' : 'none';
}

// Topbar badge: live active count, speed cap, and max concurrent
var _aria2BadgeState = {
  active: 0,
  limitBps: 0,
  maxDl: 3,
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
      maxDl: Number(
        settingsData.max_concurrent_downloads ??
        settingsData.aria2_max_active_downloads
      ) || 3,
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
  if (elMax)    elMax.textContent    = s.maxDl || '—';
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

async function applyAria2MaxDlPreset(val) {
  var n = parseInt(val) || 3;
  var st = document.getElementById('aria2-maxdl-status');
  if (st) { st.style.color='var(--text2)'; st.textContent='Applying…'; }
  try {
    // Apply live via RPC — POST /aria2/global-options also persists to settings.json
    await api('POST', '/aria2/global-options', {max_concurrent_downloads: n});
    // Keep settingsData in sync so subsequent PUT /settings calls don't
    // overwrite this value with the stale cached number.
    if (settingsData) {
      // Keep BOTH config fields in sync so a subsequent PUT /settings and a
      // Manager Semaphore reset both use the updated value.
      settingsData.aria2_max_active_downloads = n;
      settingsData.max_concurrent_downloads   = n;
    }
    // Sync Settings-page inputs so a subsequent Save Settings does not clobber.
    var maxDlInput2 = document.getElementById('s-aria2_max_active_downloads');
    if (maxDlInput2) maxDlInput2.value = n;
    if (st) { st.style.color='var(--green)'; st.textContent=n+' active'; }
    setTimeout(function(){ if(st) st.style.color='var(--text2)'; st.textContent=''; }, 3000);
    updateAria2TopbarBadge({maxDl: n});
  } catch(e) {
    if (st) { st.style.color='var(--red)'; st.textContent='Error'; }
    toast('Max downloads error: '+e.message, 'error');
  }
}


function switchHelpTab(el) {
  if (!el) return;
  const tabId = el.dataset.htab;
  document.querySelectorAll('#help-tabs .stab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.help-panel').forEach(p => p.classList.remove('active'));
  el.classList.add('active');
  const panel = document.getElementById('htab-' + tabId);
  if (panel) panel.classList.add('active');
}


async function showMemoryInfo() {
  var el = document.getElementById('aria2-memory-info');
  if (!el) return;
  el.style.display = '';
  el.innerHTML = '<span style="color:var(--text2)">Loading&#8230;</span>';
  try {
    var d = await api('GET', '/admin/memory-info');
    el.innerHTML =
      '<b>&#128202; System Memory</b><br>' +
      'Total: <b>' + d.total + '</b> &nbsp; ' +
      'Really used: <b>' + d.really_used + '</b> &nbsp; ' +
      'Page cache: <b style="color:var(--accent)">' + d.page_cache + '</b> &nbsp; ' +
      'Available: <b style="color:var(--green)">' + d.available + '</b><br>' +
      '<span style="font-size:11px;color:var(--text2)">' +
      'Page cache = kernel file cache shown as \"used\" in Unraid dashboard, ' +
      'but reclaimed automatically when needed. ' +
      'If large, click \"Drop Page Cache\" to release it immediately.' +
      '</span>';
  } catch(e) {
    el.innerHTML = '<span style="color:var(--red)">Error: ' + esc(e.message) + '</span>';
  }
}

async function dropPageCache() {
  var el = document.getElementById('aria2-memory-info');
  if (el) { el.style.display = ''; el.innerHTML = '<span style="color:var(--text2)">Releasing page cache&#8230;</span>'; }
  try {
    var d = await api('POST', '/admin/drop-page-cache');
    toast('Page cache released for ' + d.cache_released + '/' + d.files_processed + ' files', 'success');
    if (el) el.innerHTML =
      '<b style="color:var(--green)">&#10003; ' + esc(d.message) + '</b><br>' +
      '<span style="font-size:11px;color:var(--text2)">Run Memory Info again to see updated RAM usage.</span>';
    // refresh memory info after 1s
    setTimeout(showMemoryInfo, 1200);
  } catch(e) {
    toast('Drop page cache failed: ' + e.message, 'error');
    if (el) el.innerHTML = '<span style="color:var(--red)">Error: ' + esc(e.message) + '</span>';
  }
}
