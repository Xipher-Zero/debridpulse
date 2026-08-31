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
    el.innerHTML = `
      <button id="btn-resume-all" class="btn btn-primary" onclick="resumeProcessing()" style="display:none">Resume All</button>
      <button id="btn-resume-paused" class="btn btn-primary" onclick="resumePausedDownloads()" style="display:none">Resume Paused</button>
      <button id="btn-pause-all" class="btn btn-ghost" onclick="pauseProcessing()">Pause All</button>
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
      resumePausedBtn.textContent = label;
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
    dashboard:'Dashboard',
    torrents:'Downloads',
    events:'Event Log',
    stats:'Statistics',
    settings:'Settings',
    help:'Help & License',
  };
  document.getElementById('page-title').textContent = titles[v] || v;
  if (v === 'dashboard') { loadStats(); loadRecent(); }
  if (v === 'torrents')  loadTorrents();
  if (v === 'events')    loadEvents();
  if (v === 'stats')     loadDetailedStats();
  if (v === 'settings')  loadSettings();
  if (v === 'aria2queue') loadAria2QueueView();
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
    alldebrid_existing: 'AllDebrid import',
    import_existing: 'AllDebrid import',
    api: 'API'
  };
  const key = String(source || '').trim();
  return labels[key] || esc(key) || '—';
}

function sanitizeErrorMsg(message) {
  const text = String(message || 'Request failed');
  return text.length > 500 ? text.slice(0, 497) + '...' : text;
}

function toast(msg, type = 'info') {
  const icons = {success:'✅',error:'❌',warn:'⚠️',info:'ℹ️'};
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  const icon = document.createElement('span');
  icon.textContent = icons[type] || '·';
  const text = document.createElement('span');
  text.textContent = String(msg ?? '');
  el.append(icon, text);
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => el.style.opacity = '0', 3000);
  setTimeout(() => el.remove(), 3400);
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
function badge(s) {
  const m = {pending:'⏳ Pending',uploading:'⬆ Uploading',processing:'⚙ Processing',extracting:'📦 Extracting',
    queued:'🕓 Queued',paused:'⏸ Paused',downloading:'⬇ Downloading',ready:'✓ Ready',completed:'✅ Done',
    downloading_with_errors:'⬇ Downloading',
    completed_with_errors:'⚠ Completed with errors',
    error:'❌ Error',missing:'❌ Missing file',provider_failed:'❌ Provider download failed',
    provider_missing:'❌ Removed from provider',failed:'❌ Provider download failed',
    deleted:'🗑 Deleted',imported:'📋 Imported',partial:'⚠ Partial'};
  const key = String(s || '');
  const requestedCls = key === 'missing' || key === 'provider_failed' || key === 'provider_missing' || key === 'failed'
    ? 'error'
    : key === 'completed_with_errors' || key === 'downloading_with_errors'
      ? 'partial'
      : key;
  const cls = /^[a-z0-9_-]+$/i.test(requestedCls) ? requestedCls : 'unknown';
  return `<span class="badge badge-${cls}">${esc(m[key] || key || 'Unknown')}</span>`;
}
function transferDisplayStatus(t) {
  if (t && String(t.extraction_status || '').trim() === 'extracting') {
    return 'extracting';
  }
  if (
    t &&
    t.status === 'completed' &&
    String(t.extraction_status || '').trim() === 'error'
  ) {
    return 'completed_with_errors';
  }
  if (
    t &&
    t.source === 'direct_link' &&
    t.status === 'downloading' &&
    String(t.error_message || '').trim()
  ) {
    return 'downloading_with_errors';
  }
  if (
    t &&
    t.source === 'direct_link' &&
    t.status === 'completed' &&
    String(t.error_message || '').trim()
  ) {
    return 'completed_with_errors';
  }
  if (t && t.source === 'direct_link' && t.status === 'error' && t.provider_status === 'missing') {
    return 'missing';
  }
  if (t && t.status === 'error' && t.provider_status === 'missing') {
    return 'provider_missing';
  }
  if (t && t.status === 'error' && ['error', 'failed'].includes(t.provider_status)) {
    return 'provider_failed';
  }
  return (t && t.status) || '';
}
function providerDisplayStatus(t) {
  if (t && t.source !== 'direct_link' && t.provider_status === 'missing') {
    return 'provider_missing';
  }
  if (t && t.provider_status === 'failed') {
    return 'provider_failed';
  }
  return (t && t.provider_status) || '';
}
function progress(pct, status) {
  const done   = status === 'completed';
  const active = status === 'downloading';
  let pctVal = done ? 100 : Math.min(Math.max(pct || 0, 0), 100);
  // Show a thin "in progress" stripe when downloading but no percentage yet
  const showStripe = active && pctVal === 0;
  const fillStyle = showStripe
    ? 'width:100%;opacity:.35;background:repeating-linear-gradient(90deg,var(--accent) 0,var(--accent) 8px,transparent 8px,transparent 16px)'
    : `width:${pctVal}%`;
  const cls = done ? 'done' : '';
  const label = done ? '100%' : showStripe ? '…' : `${pctVal.toFixed(0)}%`;
  return `<div class="prog"><div class="prog-fill ${cls}" style="${fillStyle}"></div></div>
          <span class="prog-pct">${label}</span>`;
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

async function checkConnections() {
  // AllDebrid dot is already set by loadStats() — skip duplicate /stats call
  const cfg = settingsData || {};

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


async function checkPremiumStatus() {
  try {
    const cfg = settingsData;
    if (!cfg || !cfg.alldebrid_api_key_configured) return;
    const r = await api('POST', '/settings/test-alldebrid');
    _updatePremiumLabel(r);
    setDot('api', 'ok', `AllDebrid: ${r.username||'online'}`);
  } catch { /* silent — dot already set by checkConnections */ }
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
  const active = Math.max(0, parseInt(stats?.operator_active_downloads, 10) || 0);
  const value = Number(stats?.operator_active_progress_pct);

  _operatorTitleState.active = active;
  _operatorTitleState.progress = Number.isFinite(value)
    ? Math.min(100, Math.max(0, Math.round(value)))
    : 0;
  renderOperatorTitle();
}

async function loadStats() {
  // Retry up to 5 times — server may be slow on first request after container start
  for (let attempt = 1; attempt <= 5; attempt++) {
    try {
      const s = await api('GET', '/stats');
      updateOperatorTitle(s);
      // ── populate sidebar version ────────────────────────────────────────
      const versionEl = document.getElementById('sidebar-version');
      if (versionEl) versionEl.textContent = s.version ? `v${s.version}` : 'v—';
      if (settingsData) settingsData.paused = !!s.paused;
      const bs = s.by_status || {};
      pausedTransferCount = Math.max(0, Number(bs.paused) || 0);
      renderTopbarActions();
      setDot('api', 'ok', 'AllDebrid: online');
      // ── stat cards ─────────────────────────────────────────────────────
      // Soft-deleted rows remain in /stats for diagnostics/duplicate revival,
      // but they are intentionally absent from the normal All Downloads view.
      // User-facing totals and Queue Health therefore use the same visible universe.
      const total = Object.entries(bs)
        .filter(([status]) => status !== 'deleted')
        .reduce((sum, [, count]) => sum + (Number(count) || 0), 0);
      const completed = s.completed_count ?? bs.completed ?? 0;
      const queuePct = pct(completed, total || 0);
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
      const healthEl = document.getElementById('i-queue-health');
      if (healthEl) {
        healthEl.textContent = `${queuePct}%`;
        healthEl.style.color = queuePct >= 90 ? 'var(--green)' : queuePct >= 70 ? 'var(--accent)' : 'var(--red)';
      }
      document.getElementById('i-queue-copy').textContent = `${s.active_operations ?? s.active_downloads ?? 0} active / ${s.queued_downloads||0} queued`;
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


function renderTorrentPagination(total, limit, offset) {
  var totalPages = Math.max(1, Math.ceil(total / limit));
  var cur = Math.floor(offset / limit) + 1;
  torrentPage = cur;
  var info = document.getElementById('torrent-page-info');
  var btns = document.getElementById('torrent-page-btns');
  if (!info || !btns) return;
  var from = total === 0 ? 0 : offset + 1;
  var to   = Math.min(offset + limit, total);
  info.textContent = total > 0 ? from + '–' + to + ' of ' + total : 'No results';
  var pages = [];
  if (totalPages <= 7) { for (var i=1;i<=totalPages;i++) pages.push(i); }
  else {
    pages = [1];
    var s = Math.max(2, cur-2), e = Math.min(totalPages-1, cur+2);
    if (s > 2) pages.push('...');
    for (var i=s;i<=e;i++) pages.push(i);
    if (e < totalPages-1) pages.push('...');
    pages.push(totalPages);
  }
  btns.innerHTML =
    '<button class="btn btn-ghost btn-sm"'+(cur<=1?' disabled':'')+' onclick="goToTorrentPage('+(cur-1)+')">&#8249;</button>' +
    pages.map(function(p){ return p==='...'
      ? '<span style="padding:0 4px;color:var(--text3)">…</span>'
      : '<button class="btn '+(p===cur?'btn-primary':'btn-ghost')+' btn-sm" onclick="goToTorrentPage('+p+')">'+p+'</button>';
    }).join('') +
    '<button class="btn btn-ghost btn-sm"'+(cur>=totalPages?' disabled':'')+' onclick="goToTorrentPage('+(cur+1)+')">&#8250;</button>';
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


async function loadDetailedStats(period) {
  period = period || (document.querySelector('#stats-period-tabs .ftab.active')||{}).dataset?.period || '24h';

  // Chart-Titel Mapping
  var chartTitles = {
    '1h':  'Completions — last hour',
    '24h': 'Completions — last 24 hours',
    '7d':  'Completions — last 7 days',
    '30d': 'Completions — last 30 days',
    '1y':  'Completions — last year',
    'all': 'All-time completions'
  };
  var chartTitleEl = document.getElementById('chart-title');
  if (chartTitleEl) chartTitleEl.textContent = chartTitles[period] || 'Completions';

  // Period label for subtext
  var periodLabels = {
    '1h':'last hour','24h':'last 24h','7d':'last 7 days',
    '30d':'last 30 days','1y':'last year','all':'all time'
  };
  var pLabel = periodLabels[period] || period;

  try {
    var stats = await api('GET', '/stats/detail?period=' + encodeURIComponent(period));
    var t = stats.totals || {};
    document.getElementById('detail-stat-cards').innerHTML =
      '<div class="metric-card"><div class="metric-label">Downloads</div><div class="metric-value">'+(t.torrent_total||0)+'</div><div class="metric-sub">Added in '+pLabel+'.</div></div>' +
      '<div class="metric-card"><div class="metric-label">Completed Size</div><div class="metric-value">'+fmtSize(t.completed_size||0)+'</div><div class="metric-sub">Completed in '+pLabel+'.</div></div>' +
      '<div class="metric-card"><div class="metric-label">Completed</div><div class="metric-value">'+(t.completed_count||0)+'</div><div class="metric-sub">Finished in '+pLabel+'.</div></div>' +
      '<div class="metric-card"><div class="metric-label">In Progress</div><div class="metric-value">'+(t.partial_total||0)+'</div><div class="metric-sub">Currently downloading or processing.</div></div>' +
      (t.success_rate_pct!=null ? '<div class="metric-card"><div class="metric-label">Success Rate</div><div class="metric-value">'+t.success_rate_pct+'%</div><div class="metric-sub">Completed vs. completed+error.</div></div>' : '');

    document.getElementById('detail-torrent-status').innerHTML = renderKvMap(stats.torrent_status);
    document.getElementById('detail-file-status').innerHTML   = renderKvMap(stats.file_status, function(v){return v.count??v;});
    document.getElementById('detail-event-levels').innerHTML  = renderKvMap(stats.event_levels);
    var srcEl = document.getElementById('detail-sources');
    if (srcEl) {
      var srcs = stats.sources||[];
      srcEl.innerHTML = srcs.length
        ? srcs.map(function(s){ return '<div class="kv-row"><span class="kv-key">'+esc(s.source||'(none)')+'</span><span class="kv-val">'+s.count+'</span></div>'; }).join('')
        : '<div class="empty">No data.</div>';
    }

    // Chart — data already period-filtered from backend
    var daily = stats.daily_completions || [];
    var ctx = document.getElementById('daily-chart');
    if (ctx && typeof Chart !== 'undefined') {
      if (ctx._ci) ctx._ci.destroy();
      var themeStyles = getComputedStyle(document.body);
      var gridColor = themeStyles.getPropertyValue('--border').trim();
      var tickColor = themeStyles.getPropertyValue('--text3').trim();
      ctx._ci = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: daily.map(function(d){ return d.date||''; }),
          datasets: [{
            label: 'Completions', data: daily.map(function(d){ return d.count||0; }),
            backgroundColor: 'rgba(56,210,125,.48)', borderColor: '#38d27d',
            borderWidth: 1, borderRadius: 4
          }]
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { grid:{color:gridColor}, ticks:{color:tickColor,font:{size:10},maxRotation:45} },
            y: { grid:{color:gridColor}, ticks:{color:tickColor,font:{size:10}}, beginAtZero:true, precision:0 }
          }
        }
      });
    }
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
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
      tb.innerHTML = '<tr><td colspan="6"><div class="empty"><div class="empty-icon">⬇️</div>No transfers yet. Add a magnet, torrent file, or debrid link to start.</div></td></tr>';
      return;
    }
    // Update activity count
    const countEl = document.getElementById('dash-activity-count');
    if (countEl) countEl.textContent = items.length + ' most recent';
    tb.innerHTML = items.map(t => {
      const pct_val = t.progress != null ? Math.round(t.progress) : 0;
      const is_active = ['downloading','queued'].includes(t.status);
      return `<tr data-torrent-id="${t.id}" data-status="${esc(t.status)}" onclick="showDetail(${t.id})" style="cursor:pointer">
        <td>
          <div class="t-name" title="${esc(t.name)||''}">${esc(t.name)||'(unnamed)'}</div>
          ${is_active ? `<div class="dash-row-bar"><div class="dash-row-bar-fill" style="width:${pct_val}%;background:var(--blue)"></div></div>` : ''}
          ${t.alldebrid_id ? `<div class="t-hash" style="font-size:10px;color:var(--text3)" title="AllDebrid ID">AD: ${esc(t.alldebrid_id)}</div>` : ''}
          ${t.source === 'direct_link' ? `<div class="t-hash" style="font-size:10px;color:var(--text3)" title="Direct debrid link transfer">🔗 Direct link</div>` : ''}
        </td>
        <td data-role="transfer-status">${badge(transferDisplayStatus(t))}</td>
        <td data-role="transfer-progress">${progress(t.progress,t.status)}</td>
        <td class="sz">${fmtSize(t.size_bytes)}</td>
        <td class="sz">${fmtDate(t.created_at)}</td>
        <td onclick="event.stopPropagation()">
          <div class="actions">
            ${t.status==='downloading' || t.status==='queued' ? `<button class="btn btn-blue btn-sm" onclick="event.stopPropagation();pauseT(${t.id},this)" title="Pause this download">⏸ Pause</button>` : ''}
            ${t.status==='paused' ? `<button class="btn btn-blue btn-sm" onclick="event.stopPropagation();resumeT(${t.id},this)" title="Resume this download">▶ Resume</button>` : ''}
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
function setFilter(el, status) {
  document.querySelectorAll('#view-torrents .filter-tabs .ftab').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  currentFilter = status; torrentPage = 1;
  if (_torrentSearchTimer) {
    clearTimeout(_torrentSearchTimer);
    _torrentSearchTimer = null;
  }
  loadTorrents();
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
    const title = document.getElementById('torrent-card-title');
    if (title) title.textContent = `All Downloads (${torrentTotal})`;
    renderTorrentPagination(torrentTotal, _limit, _offset);
    if (!items.length) {
      tb.innerHTML = `<tr><td colspan="8"><div class="empty"><div class="empty-icon">⬇️</div>${currentTorrentSearch || currentFilter ? 'No downloads match the current filter or search.' : 'No downloads found.'}</div></td></tr>`;
      return;
    }
    tb.innerHTML = items.map(t => `<tr data-torrent-id="${t.id}" data-status="${esc(t.status)}" draggable="true" ondragstart="onTorrentDragStart(event,${t.id})" ondragend="onTorrentDragEnd(event)" ondragover="onTorrentDragOver(event,${t.id})" ondrop="onTorrentDrop(event,${t.id})">
      <td onclick="event.stopPropagation()"><input type="checkbox" class="t-chk" data-id="${t.id}" onchange="onCheckboxChange()"/></td>
      <td onclick="showDetail(${t.id})" style="cursor:pointer">
        <div class="t-name">${esc(t.name)||'(unnamed)'}</div>
        <div class="t-hash">${(t.hash||'').substring(0,16)}${t.hash?'…':''}</div>
      </td>
      <td class="sz">
        <div>${sourceLabel(t.source)}</div>
        ${t.label?`<span class="lbl-badge">🏷 ${esc(t.label)}</span>`:''}
      </td>
      <td data-role="transfer-status">${badge(transferDisplayStatus(t))}</td>
      <td data-role="transfer-progress">${progress(t.progress,t.status)}</td>
      <td class="sz">${fmtSize(t.size_bytes)}</td>
      <td class="sz">${fmtDate(t.created_at)}</td>
      <td>
        <div class="actions">
          <button class="btn btn-ghost btn-sm" onclick="showDetail(${t.id})">Details</button>
          ${t.status==='ready' || t.status==='pending' ? `<button class="btn btn-primary btn-sm" onclick="event.stopPropagation();downloadNow(${t.id},this)" title="Move to front of queue">⬇ Now</button>` : ''}
          ${t.status==='downloading' || t.status==='queued' ? `<button class="btn btn-blue btn-sm" onclick="pauseT(${t.id},this)">⏸</button>` : ''}
          ${t.status==='paused' ? `<button class="btn btn-blue btn-sm" onclick="resumeT(${t.id},this)">▶</button>` : ''}
          ${t.status==='error'?`<button class="btn btn-blue btn-sm" onclick="retryT(${t.id},this)">↻</button>`:''}
          <button class="btn btn-danger btn-sm" onclick="deleteT(${t.id},event,this)">✕</button>
        </div>
      </td>
    </tr>`).join('');
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
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

    if (modalBody) modalBody.innerHTML = `
      <div class="detail-grid">
        <div><div class="dk">Status</div><div class="dv">${badge(transferDisplayStatus(t))}</div></div>
        <div><div class="dk">Provider</div><div class="dv">${t.provider_status ? badge(providerDisplayStatus(t)) : '—'}</div></div>
        <div><div class="dk">Progress</div><div class="dv">${(t.progress||0).toFixed(1)}%</div></div>
        <div><div class="dk">Size</div><div class="dv">${fmtSize(t.size_bytes)}</div></div>
        <div><div class="dk">Source</div><div class="dv">${sourceLabel(t.source)}</div></div>
        <div><div class="dk">Downloader</div><div class="dv">${esc(t.download_client||'aria2')}</div></div>
        <div><div class="dk">Added</div><div class="dv">${fmtDate(t.created_at)}</div></div>
        <div><div class="dk">Completed</div><div class="dv">${fmtDate(t.completed_at)}</div></div>
        <div style="grid-column:1/-1"><div class="dk">AllDebrid ID</div><div class="dv">${esc(t.alldebrid_id||'—')}</div></div>
        <div style="grid-column:1/-1"><div class="dk">Hash</div><div class="dv" style="font-size:11px">${esc(t.hash||'—')}</div></div>
        ${t.local_path?`<div style="grid-column:1/-1"><div class="dk">Local Path</div><div class="dv" style="font-size:11px">${esc(t.local_path)}</div></div>`:''}
        ${t.error_message?`<div style="grid-column:1/-1"><div class="dk">Error</div><div class="dv" style="color:var(--red)">${esc(t.error_message)}</div></div>`:''}
        ${t.extraction_status?`<div><div class="dk">Extraction</div><div class="dv">${esc(t.extraction_status)}</div></div>`:''}
        ${t.extraction_error?`<div style="grid-column:1/-1"><div class="dk">Extraction Error</div><div class="dv" style="color:var(--red)">${esc(t.extraction_error)}</div></div>`:''}
      </div>
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
                <td>${badge(f.status)}</td>
              </tr>`).join('')}</tbody>
            </table>
          </div>
        </div>
      `:''}
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
}

function updateThemeToggle(isLight) {
  const btn = document.getElementById('theme-toggle');
  if (!btn) return;
  const action = isLight ? 'Switch to dark mode' : 'Switch to light mode';
  btn.textContent = isLight ? '☀︎' : '☾';
  btn.title = action;
  btn.setAttribute('aria-label', action);
  const chart = document.getElementById('daily-chart')?._ci;
  if (chart) {
    const styles = getComputedStyle(document.body);
    const gridColor = styles.getPropertyValue('--border').trim();
    const tickColor = styles.getPropertyValue('--text3').trim();
    chart.options.scales.x.grid.color = gridColor;
    chart.options.scales.y.grid.color = gridColor;
    chart.options.scales.x.ticks.color = tickColor;
    chart.options.scales.y.ticks.color = tickColor;
    chart.update('none');
  }
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
  if (!evs.length) { el.innerHTML='<div class="empty">No events match the filter.</div>'; return; }
  el.innerHTML = evs.map(ev=>`
    <div class="event-item">
      <div class="elevel ${esc(ev.level)}"></div>
      <div><div class="emsg">${esc(ev.message)}</div>${ev.torrent_name?`<div class="ename">${esc(ev.torrent_name)}</div>`:''}</div>
      <div class="etime">${fmtDate(ev.created_at)}</div>
    </div>`).join('');
}

// ── Settings ───────────────────────────────────────────────────────────────

async function loadChangelog() {
  const el = document.getElementById('changelog-content');
  el.innerHTML = '<p style="color:var(--text3)">Loading…</p>';
  try {
    const data = await api('GET', '/changelog');
    const md = data.content || 'No changelog content found.';
    el.innerHTML = renderMarkdown(md);
  } catch(e) {
    el.innerHTML = `<p style="color:#f87171">Failed to load changelog: ${esc(e.message)}</p>`;
    toast(e.message, 'error');
  }
}

function renderMarkdown(md) {
  const escape = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  // Inline formatting: **bold**, `code`, [text](url)
  const inline = s => escape(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const lines = md.split('\n');
  let html = '';
  let inList = false;
  let inCode = false;
  for (let raw of lines) {
    const line = raw.trimEnd();
    if (line.startsWith('```')) {
      if (inList) { html += '</ul>'; inList = false; }
      if (inCode) { html += '</code></pre>'; inCode = false; }
      else { html += '<pre><code>'; inCode = true; }
      continue;
    }
    if (inCode) { html += escape(line) + '\n'; continue; }
    if (/^#{3,}\s/.test(line)) {
      if (inList) { html += '</ul>'; inList = false; }
      html += `<h3>${inline(line.replace(/^#+\s*/,''))}</h3>`;
    } else if (/^#{2}\s/.test(line)) {
      if (inList) { html += '</ul>'; inList = false; }
      html += `<h2>${inline(line.replace(/^#+\s*/,''))}</h2>`;
    } else if (/^#\s/.test(line)) {
      if (inList) { html += '</ul>'; inList = false; }
      html += `<h1>${inline(line.replace(/^#+\s*/,''))}</h1>`;
    } else if (/^[-*]\s/.test(line)) {
      if (!inList) { html += '<ul>'; inList = true; }
      html += `<li>${inline(line.replace(/^[-*]\s+/,''))}</li>`;
    } else if (line === '---') {
      if (inList) { html += '</ul>'; inList = false; }
      html += '<hr>';
    } else if (line === '') {
      if (inList) { html += '</ul>'; inList = false; }
    } else {
      if (inList) { html += '</ul>'; inList = false; }
      html += `<p>${inline(line)}</p>`;
    }
  }
  if (inCode) html += '</code></pre>';
  if (inList) html += '</ul>';
  return html;
}

async function loadSettings() {
  try {
    settingsData = await api('GET','/settings');
    renderSettings();
  } catch(e) { toast(sanitizeErrorMsg(e.message),'error'); }
  // Activate first tab by default
  switchSettingsTab('tab-general');
  // Avatar preview: show if a custom avatar URL is set
  const avatarUrl = settingsData.discord_avatar_url || '';
  if (avatarUrl && !avatarUrl.includes('github') && !avatarUrl.includes('_DEFAULT')) {
    showAvatarPreview(avatarUrl, 'Custom avatar', 0);
  }
}

function renderSettings() {
  const _settingsScrollTop = document.getElementById('settings-form')?.scrollTop || 0;
  const s = escapeHtmlStrings(settingsData || {});
  const aria2BuiltIn = (s.aria2_mode || 'builtin') === 'builtin';

  // Define tabs
  const tabs = [
    { id:'tab-general',       label:'⚡ General' },
    { id:'tab-download',      label:'⬇️ Download' },
    { id:'tab-extract',       label:'📦 Extract' },
    { id:'tab-notifications', label:'🔔 Notifications' },
    { id:'tab-database',      label:'🗄 Database' },
    { id:'tab-advanced',      label:'🛠️ Advanced' },
  ];
  document.getElementById('settings-tabs').innerHTML = tabs.map((t,i)=>
    `<div class="stab${i===0?' active':''}" data-tab="${t.id}" onclick="switchSettingsTab('${t.id}')">${t.label}</div>`
  ).join('');
  const _sf = document.getElementById('settings-form');
  _sf.innerHTML = '';
  _sf.insertAdjacentHTML('beforeend', `<div class="stab-panel  active" id="tab-general">
      <div class="scard">
        <div class="scard-header">🔑 AllDebrid</div>
      <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Required. Your AllDebrid API key — get it at <a href="https://alldebrid.com/apikeys/" target="_blank" style="color:var(--accent)">alldebrid.com/apikeys</a>.</p>
        <div class="scard-body">
          <div class="form-group">
            <label class="form-label">API Key</label>
            <div class="test-row">
              <input class="input" type="password" id="s-alldebrid_api_key" value="${s.alldebrid_api_key||''}" placeholder="Your AllDebrid API key"/>
              <button class="btn btn-blue btn-sm" onclick="testAD(this)">Test</button>
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">Agent Name</label>
            <input class="input" id="s-alldebrid_agent" value="${s.alldebrid_agent||'DebridPulse'}"/>
          </div>
        </div>
      </div>

      <div class="scard">
      <div class="scard-header">🔐 Access Control</div>
      <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Optional HTTP Basic Auth. Username remains visible; the saved password is never returned. Enter a password to set or replace it, or explicitly clear it in Stored Secrets below.</p>
      <div class="scard-body">
        <div class="form-group">
          <label class="form-label">Username</label>
          <input class="input" id="s-auth_username" value="${s.auth_username||''}" placeholder="Leave empty to disable auth"/>
        </div>
        <div class="form-group">
          <label class="form-label">Password</label>
          <input class="input" type="password" id="s-auth_password" value="" placeholder="${s.auth_password_configured?'Stored password configured — leave blank to keep':'Enter password to enable auth'}"/>
          <span class="form-hint">Save settings and reload the page after changing credentials. Use Stored Secrets below to explicitly clear the saved password.</span>
        </div>
      </div>
    </div>

      <div class="scard">
        <div class="scard-header">🧹 Stored Secrets</div>
        <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Redacted secrets are preserved when their input is left blank. Check a configured item here to explicitly erase it on Save.</p>
        <div class="scard-body">
          ${[
            ['alldebrid_api_key','AllDebrid API key'],
            ['aria2_secret','aria2 RPC secret'],
            ['discord_webhook_url','Discord webhook'],
            ['discord_webhook_added','Torrent-added webhook'],
            ['stats_report_webhook_url','Reporting webhook'],
            ['auth_password','Basic Auth password'],
            ['extraction_password','Extraction password']
          ].filter(([field])=>s[field+'_configured']).map(([field,label])=>`
            <label class="toggle-row" style="cursor:pointer">
              <div class="toggle-info"><div class="tl">${label}</div><div class="ts">Erase the stored value on Save</div></div>
              <input type="checkbox" id="s-clear-${field}">
            </label>`).join('') || '<div class="form-hint">No stored secrets are currently configured.</div>'}
        </div>
      </div>

      <div class="scard">
        <div class="scard-header">💾 Disk Space Guard</div>
        <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">
          Allows active downloads to finish and <b>defers new dispatches</b> when free space drops below
          the threshold. Resumes automatically when space recovers (with hysteresis to prevent flapping).
          Works on all filesystems: ext4, XFS, ZFS, Btrfs, <b>Unraid (FUSE/shfs)</b>, NFS.
        </p>
        <div class="scard-body">
          <div class="form-group">
            <label class="form-label">Minimum Free Disk Space (GB, 0 = disabled)</label>
            <input class="input" type="number" id="s-min_free_disk_gb" value="${s.min_free_disk_gb??0}" min="0" step="0.5"/>
            <span class="form-hint">New dispatches are deferred when free space drops below this; active transfers continue. Deferred work starts automatically when space recovers.</span>
          </div>
          <div class="form-group">
            <label class="form-label">Resume Hysteresis (GB above threshold)</label>
            <input class="input" type="number" id="s-disk_guard_resume_hysteresis_gb" value="${s.disk_guard_resume_hysteresis_gb??0.5}" min="0" step="0.1"/>
            <span class="form-hint">Downloads only resume when free space exceeds threshold + this value. Prevents rapid pause/resume cycles.</span>
          </div>
          <div class="form-group">
            <label class="form-label">Disk Check Interval (seconds)</label>
            <input class="input" type="number" id="s-disk_guard_interval_seconds" value="${s.disk_guard_interval_seconds??60}" min="10" max="3600"/>
            <span class="form-hint">How often to check free disk space. 30–120 s is recommended. Lower values increase FUSE/NFS stat() calls.</span>
          </div>
        </div>
      </div>

      <div class="scard">
        <div class="scard-header">🚦 AllDebrid Rate Limit</div>
      <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Controls AllDebrid API call rate, background sync interval, and automatic retry settings.</p>
        <div class="scard-body">
          <div class="form-group">
            <label class="form-label">API calls per minute</label>
            <input class="input" type="number" id="s-alldebrid_rate_limit_per_minute" value="${s.alldebrid_rate_limit_per_minute??60}" min="0" max="300"/>
            <span class="form-hint">Default: 60 req/min. Set to 0 for unlimited.</span>
          </div>
          <div class="form-group">
            <label class="form-label">Full AllDebrid Sync Interval (minutes)</label>
            <input class="input" type="number" id="s-full_sync_interval_minutes" value="${s.full_sync_interval_minutes??5}" min="0" max="1440"/>
            <span class="form-hint">Reconciles all known AllDebrid magnets. 0 = disabled.</span>
          </div>
          <div class="form-group">
            <label class="form-label">aria2 Retry Count</label>
            <input class="input" type="number" id="s-aria2_error_retry_count" value="${s.aria2_error_retry_count??3}" min="0" max="20"/>
            <span class="form-hint">How often a failed aria2 file is retried. 0 = disabled.</span>
          </div>
          <div class="form-group">
            <label class="form-label">aria2 Retry Delay (seconds)</label>
            <input class="input" type="number" id="s-aria2_error_retry_delay_seconds" value="${s.aria2_error_retry_delay_seconds??60}" min="0" max="3600"/>
            <span class="form-hint">Delay before retrying a failed aria2 file.</span>
          </div>
        </div>
      </div>
    </div>
    </div>`);
  _sf.insertAdjacentHTML('beforeend', `<div class="stab-panel" id="tab-download">
      <div class="scard">
      <div class="scard-header">⬇️ Download Client</div>
      <div class="scard-body">
        <div class="form-group">
          <label class="form-label">Delivery Mode</label>
          <input class="input" id="s-download_client" value="aria2" disabled/>
          <span class="form-hint">DebridPulse delivers unlocked provider links through aria2.</span>
          <details class="info-details">
            <summary>How do the delivery modes work?</summary>
            <div class="info-details-body">
              <div class="info-mode">
                <div class="info-mode-title">⚡ aria2</div>
                <div class="info-mode-desc">
                  The app unlocks each AllDebrid link and hands the resulting URL to aria2 via JSON-RPC.
                  aria2 then handles the actual download entirely on its own — it decides how many connections to open,
                  where to write the file, whether to segment the transfer, and when it is complete.
                  The app only monitors aria2's reported status (<code>active / waiting / complete / error</code>)
                  and updates its internal state accordingly. When aria2 reports a download as complete,
                  the app marks the file done, removes the entry from aria2, and — once all files of a torrent
                  are finished — deletes the torrent from AllDebrid.
                </div>
                <div class="info-mode-pros">✔ Faster multi-connection downloads · resumable · aria2 manages bandwidth &amp; concurrency · works across Docker volumes</div>
                <div class="info-mode-cons">✖ Requires a running aria2 instance with RPC enabled · needs correct RPC URL and optional secret configured below</div>
              </div>
            </div>
          </details>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Mode</label>
          <select class="input" id="s-aria2_mode" onchange="const activeTab=getActiveSettingsTab(); settingsData.aria2_mode=this.value; renderSettings(); switchSettingsTab(activeTab); loadAria2Runtime().catch(()=>{});">
            <option value="external" ${(s.aria2_mode||'external')==='external'?'selected':''}>External aria2</option>
            <option value="builtin" ${(s.aria2_mode||'external')==='builtin'?'selected':''}>Built-in aria2</option>
          </select>
          <span class="form-hint">Built-in aria2 is managed by this container and only receives AllDebrid HTTP(S) links.</span>
        </div>

        <div class="settings-subsection ${aria2BuiltIn ? '' : 'settings-subsection-inactive'}">
          <div class="settings-subsection-title">Built-in aria2</div>

          <div class="toggle-row">
            <div class="toggle-info">
              <div class="tl">Auto-start Built-in aria2</div>
              <div class="td">Starts the internal aria2 daemon when the app starts in built-in mode.</div>
            </div>
            <label class="toggle"><input type="checkbox" id="s-aria2_builtin_auto_start" ${s.aria2_builtin_auto_start!==false?'checked':''} ${aria2BuiltIn?'':'disabled'}><div class="ttrack"></div></label>
          </div>

          <div class="form-group">
            <label class="form-label">Built-in aria2 Port</label>
            <input class="input" type="number" id="s-aria2_builtin_port" value="${s.aria2_builtin_port??6800}" min="1" max="65535" ${aria2BuiltIn?'':'disabled'}/>
            <span class="form-hint">The internal RPC secret is managed by the app and cannot be changed from the UI.</span>
          </div>

          <div class="form-group">
            <label class="form-label">Built-in aria2 Log Rotation Size (MB)</label>
            <input class="input" type="number" id="s-aria2_builtin_log_max_mb" value="${s.aria2_builtin_log_max_mb??25}" min="1" max="1024" ${aria2BuiltIn?'':'disabled'}/>
            <span class="form-hint">When the aria2 log reaches this size, the client rotates it. Running built-in aria2 is restarted only when needed so the new log file is used.</span>
          </div>

          <div class="form-group">
            <label class="form-label">Built-in aria2 Log Backups</label>
            <input class="input" type="number" id="s-aria2_builtin_log_backups" value="${s.aria2_builtin_log_backups??3}" min="0" max="20" ${aria2BuiltIn?'':'disabled'}/>
            <span class="form-hint">How many rotated aria2 log files to keep. 0 truncates the log instead of keeping backups.</span>
          </div>
        </div>

        <div class="settings-subsection ${aria2BuiltIn ? 'settings-subsection-inactive' : ''}">
          <div class="settings-subsection-title">External aria2 Connection</div>

          <div class="form-group">
            <label class="form-label">aria2 RPC URL</label>
            <div class="test-row">
              <input class="input" id="s-aria2_url" value="${aria2BuiltIn ? 'http://127.0.0.1:'+(s.aria2_builtin_port||6800)+'/jsonrpc' : (s.aria2_url||'http://127.0.0.1:6800/jsonrpc')}" placeholder="http://127.0.0.1:6800/jsonrpc" ${aria2BuiltIn?'disabled':''}/>
              <button class="btn btn-blue btn-sm" onclick="testAria2(this)" ${aria2BuiltIn?'disabled':''}>Test</button>
            </div>
          </div>

          <div class="form-group">
            <label class="form-label">aria2 Secret</label>
            <input class="input" type="password" id="s-aria2_secret" value="${aria2BuiltIn ? 'managed-internally' : (s.aria2_secret||'')}" placeholder="Optional RPC secret" ${aria2BuiltIn?'disabled':''}/>
            <span class="form-hint">Used only when aria2 Mode is set to External aria2.</span>
          </div>
        </div>

        <div class="settings-subsection">
          <div class="settings-subsection-title">aria2 Download Path</div>

          <details class="info-details settings-path-help">
            <summary>How do aria2 download paths work?</summary>
            <div class="info-details-body">
              <div class="info-mode">
                <div class="info-mode-title">Built-in aria2 Download Folder</div>
                <div class="info-mode-desc">
                  Built-in aria2 runs inside DebridPulse and uses the filesystem path visible
                  inside the DebridPulse container.
                </div>
              </div>

              <div class="info-mode">
                <div class="info-mode-title">External aria2 Download Path</div>
                <div class="info-mode-desc">
                  External aria2 may see the same physical storage at a different
                  filesystem path. Configure this path as it appears to the external
                  aria2 daemon.
                </div>
              </div>

              <div class="info-mode">
                <div class="info-mode-title">Example</div>
                <div class="info-mode-desc">
                  <code>DebridPulse / Built-in aria2: /download</code><br>
                  <code>External aria2: /Volumes/SABnzbdDATA/AriaNG Downloads</code>
                </div>
              </div>
            </div>
          </details>

          <div class="form-group">
            <label class="form-label">Built-in aria2 Download Folder</label>
            <input class="input" id="s-download_folder" value="${s.download_folder||''}"/>
            <span class="form-hint">Path used by the DebridPulse-managed aria2 daemon.</span>
          </div>

          <div class="form-group ${aria2BuiltIn ? 'settings-field-inactive' : ''}">
            <label class="form-label">External aria2 Download Path</label>
            <input class="input" id="s-aria2_download_path" value="${s.aria2_download_path||''}" placeholder="Optional external aria2 path" ${aria2BuiltIn?'disabled':''}/>
            <span class="form-hint">Path to the download location as seen by the external aria2 daemon.</span>
          </div>
        </div>

        <div class="scard" style="margin-bottom:0">
          <div class="scard-header">aria2 Runtime</div>
          <div class="scard-body">
            <div id="aria2-runtime-status" class="form-hint" style="line-height:1.6">Runtime status not loaded yet.</div>
            <div class="input-row">
              <button class="btn btn-blue btn-sm" onclick="loadAria2Runtime()">Refresh</button>
              <button class="btn btn-ghost btn-sm" onclick="aria2RuntimeAction('start',this)" ${aria2BuiltIn?'':'disabled'}>Start</button>
              <button class="btn btn-ghost btn-sm" onclick="aria2RuntimeAction('restart',this)" ${aria2BuiltIn?'':'disabled'}>Restart</button>
              <button class="btn btn-danger btn-sm" onclick="aria2RuntimeAction('stop',this)" ${aria2BuiltIn?'':'disabled'}>Stop</button>
              <button class="btn btn-ghost btn-sm" onclick="aria2RuntimeAction('apply',this)" ${aria2BuiltIn?'':'disabled'}>Apply</button>
            </div>
          </div>
        </div>

        <div class="scard" style="margin-bottom:0">
          <div class="scard-header">aria2 Live Downloads</div>
          <div class="scard-body">
            <div class="aria2-queue-head">
              <div class="form-hint">Live aria2 queue with progress, speed, status, and basic controls.</div>
              <div class="input-row">
                <button class="btn btn-blue btn-sm" onclick="refreshAria2Downloads(this)">Refresh Queue</button>
                <button class="btn btn-ghost btn-sm" onclick="runAria2Housekeeping(this)">Purge Results</button>
              </div>
            </div>
            <div id="aria2-downloads" class="aria2-queue">
              <div class="empty">Queue not loaded yet.</div>
            </div>
          </div>
        </div>

        <div class="settings-subsection-title settings-subsection-title-spaced">Download Control / Advanced</div>
        <div class="form-group">
          <label class="form-label">aria2 Timeout (seconds)</label>
          <input class="input" type="number" id="s-aria2_operation_timeout_seconds" value="${s.aria2_operation_timeout_seconds??15}" min="5" max="120"/>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Simultaneous Downloads</label>
          <input class="input" type="number" id="s-aria2_max_active_downloads" value="${s.aria2_max_active_downloads??s.max_concurrent_downloads??3}" min="1" max="50"/>
          <span class="form-hint">Only this many files are handed to aria2 at once. Remaining files stay pending until a slot becomes free.</span>
        </div>
        <div class="form-group">
          <label class="form-label">Deep Filesystem Sync Interval (minutes)</label>
          <input class="input" type="number" id="s-aria2_deep_sync_interval_minutes" value="${s.aria2_deep_sync_interval_minutes??10}" min="0" max="1440"/>
          <span class="form-hint">
            Periodically checks if downloaded files exist on disk — independent of aria2 GID or status.
            Resolves stuck downloads where aria2 lost track of the entry or the same filename appears in different folders.
            <b>0 = disabled.</b> Default: 10 minutes.
          </span>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Poll Interval (seconds)</label>
          <input class="input" type="number" id="s-aria2_poll_interval_seconds" value="${s.aria2_poll_interval_seconds??5}" min="2" max="300"/>
          <span class="form-hint">How often the client refreshes aria2 download state.</span>
        </div>
        <div class="form-group">
          <button class="btn btn-ghost" onclick="triggerFullSync(this)">🔄 Full AllDebrid Sync Now</button>
          <button class="btn btn-ghost" onclick="runDeepSync()">🔍 Run Deep Sync Now</button>
          <span class="form-hint" style="margin-top:6px;display:block">Immediately checks all pending aria2 files on disk and marks completed ones.</span>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Result Purge Interval (minutes)</label>
          <input class="input" type="number" id="s-aria2_purge_interval_minutes" value="${s.aria2_purge_interval_minutes??15}" min="0" max="1440"/>
          <span class="form-hint">Automatically purges stopped result entries from aria2. 0 = disabled.</span>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 max-download-result</label>
          <input class="input" type="number" id="s-aria2_max_download_result" value="${s.aria2_max_download_result??50}" min="10" max="5000"/>
          <span class="form-hint">Lower values reduce how many stopped results aria2 keeps in memory.</span>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Split Connections</label>
          <input class="input" type="number" id="s-aria2_split" value="${s.aria2_split??16}" min="1" max="64"/>
          <span class="form-hint">Parallel connections per file. Default: 8. Higher = faster single-file downloads. Capped by <em>Max connections per server</em>.</span>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Min Split Size</label>
          <input class="input" id="s-aria2_min_split_size" value="${s.aria2_min_split_size||'10M'}" placeholder="10M"/>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Max Connections per Server</label>
          <input class="input" type="number" id="s-aria2_max_connection_per_server" value="${s.aria2_max_connection_per_server??16}" min="1" max="32"/>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Disk Cache</label>
          <input class="input" id="s-aria2_disk_cache" value="${s.aria2_disk_cache||'64M'}" placeholder="64M"/>
          <span class="form-hint">Write buffer size. Format: <code>0</code>, <code>64M</code>, <code>128M</code>. Default: 64M. A small cache reduces disk I/O on HDD or FUSE mounts.</span>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 File Allocation</label>
          <select class="input" id="s-aria2_file_allocation">
            ${['none','prealloc','trunc','falloc'].map(v=>'<option value="'+v+'" '+((s.aria2_file_allocation||'falloc')===v?'selected':'')+'>'+v+'</option>').join('')}
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Lowest Speed Limit</label>
          <input class="input" id="s-aria2_lowest_speed_limit" value="${s.aria2_lowest_speed_limit||'0'}" placeholder="0"/>
          <span class="form-hint">Drop downloads below this speed (e.g. <code>100K</code>). 0 = disabled.</span>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Max Upload Limit (0 = unlimited, bytes/s)</label>
          <input class="input" type="number" id="s-aria2_max_upload_limit" value="${s.aria2_max_upload_limit??0}" min="0"/>
          <span class="form-hint">Caps aria2 upload bandwidth. 0 = unlimited.</span>
        </div>
        <div class="toggle-row">
          <div class="toggle-info">
            <div class="tl">Continue Partial Downloads</div>
            <div class="td">Allows aria2 to resume partial HTTP downloads when possible.</div>
          </div>
          <label class="toggle"><input type="checkbox" id="s-aria2_continue_downloads" ${s.aria2_continue_downloads!==false?'checked':''}><div class="ttrack"></div></label>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Waiting Query Window</label>
          <input class="input" type="number" id="s-aria2_waiting_window" value="${s.aria2_waiting_window??100}" min="10" max="1000"/>
          <span class="form-hint">How many waiting jobs the client asks aria2 for per sync cycle. Lower values reduce RPC payload size and state pressure.</span>
        </div>
        <div class="form-group">
          <label class="form-label">aria2 Stopped Query Window</label>
          <input class="input" type="number" id="s-aria2_stopped_window" value="${s.aria2_stopped_window??100}" min="10" max="1000"/>
          <span class="form-hint">How many stopped jobs the client inspects per sync cycle and diagnostics call.</span>
        </div>
        <div class="toggle-row">
          <div class="toggle-info">
            <div class="tl">Keep Unfinished Download Results</div>
            <div class="td">Usually best disabled to avoid large unfinished result history in aria2 memory.</div>
          </div>
          <label class="toggle"><input type="checkbox" id="s-aria2_keep_unfinished_download_result" ${s.aria2_keep_unfinished_download_result?'checked':''}><div class="ttrack"></div></label>
        </div>
        <div class="form-group">
          <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
            <button class="btn btn-ghost" onclick="runAria2Housekeeping(this)">Run aria2 Cleanup Now</button>
            <button class="btn btn-ghost" onclick="showMemoryInfo()" title="Shows real RAM vs kernel page cache">&#128202; Memory Info</button>
            <button class="btn btn-ghost" onclick="dropPageCache()" title="Release kernel page cache for all downloaded files">&#129522; Drop Page Cache</button>
          </div>
        </div>
        <div id="aria2-memory-diagnostics" class="form-hint" style="line-height:1.6"></div>
        <div id="aria2-memory-info" class="form-hint" style="line-height:1.6;margin-top:6px;display:none"></div>
        <div class="toggle-row">
          <div class="toggle-info">
            <div class="tl">Start aria2 Jobs Paused</div>
            <div class="td">Queue the job in aria2 first and resume it manually from the API/UI workflow.</div>
          </div>
          <label class="toggle"><input type="checkbox" id="s-aria2_start_paused" ${s.aria2_start_paused?'checked':''}><div class="ttrack"></div></label>
        </div>
      </div>
    </div>
    <div class="scard">
      <div class="scard-header">⚠️ Auto-Recover Stalled Downloads</div>
      <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Downloads that remain queued or downloading without a state update beyond this threshold are reset so DebridPulse can retry them, regardless of transfer source.</p>
      <div class="scard-body">
        <div class="form-group">
          <label class="form-label">Stalled download timeout (hours)</label>
          <input class="input" type="number" id="s-stuck_download_timeout_hours" value="${s.stuck_download_timeout_hours??6}" min="0" max="168"/>
          <span class="form-hint">Set to 0 to disable timed recovery. Downloads with no transfer records may still be repaired immediately.</span>
        </div>
      </div>
    </div>
    <div class="scard">
      <div class="scard-header">&#9889; Upload Retry</div>
      <div class="scard-body">
        <div class="form-group">
          <label class="form-label">AllDebrid Upload Retry Count</label>
          <input class="input" type="number" id="s-upload_fail_retry_count" value="${s.upload_fail_retry_count??3}" min="0" max="10"/>
          <span class="form-hint">How often to re-queue when AllDebrid reports "Upload failed" (statusCode 5). 0 = disabled.</span>
        </div>
        <div class="form-group">
          <label class="form-label">Upload Retry Delay (minutes)</label>
          <input class="input" type="number" id="s-upload_fail_retry_delay_minutes" value="${s.upload_fail_retry_delay_minutes??5}" min="1" max="60"/>
          <span class="form-hint">Minutes to wait before re-uploading. Default: 5.</span>
        </div>
      </div>
    </div>
    </div>`);
  _sf.insertAdjacentHTML('beforeend', `<div class="stab-panel" id="tab-extract">
      <div class="scard">
        <div class="scard-header">&#128230; Auto-Extraction</div>
        <div class="scard-body">
          <div class="form-group">
            <label class="form-label toggle-label"><span>Enable Auto-Extraction</span>
              <label class="tswitch"><input type="checkbox" id="s-extract_enabled" ${s.extract_enabled?'checked':''}/><span class="tslider"></span></label>
            </label>
            <span class="form-hint">Automatically extract archives (.zip .rar .7z .tar.gz .tar.bz2 .tar.xz and more) after download completes. p7zip-full (7-Zip) is included in the Docker image; RAR extraction uses the same preflight-capable 7z path.</span>
          </div>
          <div class="form-group">
            <label class="form-label toggle-label"><span>Delete Archive After Extraction</span>
              <label class="tswitch"><input type="checkbox" id="s-extract_delete_archive" ${s.extract_delete_archive!==false?'checked':''}/><span class="tslider"></span></label>
            </label>
            <span class="form-hint">Remove the source archive after successful extraction. Enabled by default.</span>
          </div>
          <div class="form-group">
            <label class="form-label">Max Concurrent Extractions</label>
            <input class="input" type="number" id="s-extract_max_concurrent" value="${s.extract_max_concurrent??1}" min="1" max="10"/>
            <span class="form-hint">Maximum number of archives extracted in parallel. Default: 1 to keep the app responsive on NAS and Unraid systems.</span>
          </div>
          <div class="form-group">
            <label class="form-label toggle-label"><span>Discord Notification on Extraction</span>
              <label class="tswitch"><input type="checkbox" id="s-discord_notify_extract" ${s.discord_notify_extract!==false?'checked':''}/><span class="tslider"></span></label>
            </label>
            <span class="form-hint">Send a Discord webhook on extraction completion or failure.</span>
          </div>
        </div>
      </div>

      <div class="scard">
        <div class="scard-header">🔐 Archive Passwords</div>
        <div class="scard-body">
          <div class="form-group">
            <label class="form-label">Extraction passwords</label>
            <div id="extraction-pw-list" style="display:flex;flex-direction:column;gap:6px;margin-bottom:8px"></div>
            <button class="btn btn-ghost btn-sm" onclick="addExtractionPassword()" type="button" style="margin-top:2px">+ Add password</button>
            <span class="form-hint" style="display:block;margin-top:8px">
              Applied to all 7z and RAR extractions (<code>-p</code> flag). Each password is tried in order.
              Leave empty if archives are not password-protected.
            </span>
            <input type="hidden" id="s-extraction_password" value="${esc(s.extraction_password||'')}"/>
          </div>
        </div>
      </div>

    </div>`);
  _sf.insertAdjacentHTML('beforeend', `<div class="stab-panel" id="tab-notifications">
      <div class="scard">
        <div class="scard-header">🔔 Discord Notifications</div>
        <div class="scard-body">
          <p class="form-hint" style="margin:0 0 10px">Receive Discord notifications when torrents are added, complete, or fail.</p>
          <div class="form-group">
            <label class="form-label">Bot Name <span style="font-weight:400;color:var(--muted)">(shown as sender in Discord)</span></label>
            <input class="input" id="s-discord_username" value="${s.discord_username||'DebridPulse'}" placeholder="DebridPulse"/>
          </div>
          <div class="form-group">
            <label class="form-label">Bot Avatar <span style="font-weight:400;color:var(--muted)">(PNG/JPG/WEBP only — no SVG)</span></label>
            <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
              <input class="input" id="s-discord_avatar_url" value="${s.discord_avatar_url||''}" placeholder="https://…/avatar.png" style="flex:1"/>
              <label class="btn btn-ghost btn-sm" style="cursor:pointer;white-space:nowrap">
                📎 Upload
                <input type="file" accept="image/png,image/jpeg,image/gif,image/webp"
                  style="display:none" onchange="uploadDiscordAvatar(this)"/>
              </label>
            </div>
            <div id="avatar-preview" style="display:none;align-items:center;gap:8px;font-size:12px;color:var(--text2)">
              <img id="avatar-preview-img" src="" style="width:40px;height:40px;border-radius:50%;object-fit:cover;border:2px solid var(--border)"/>
              <span id="avatar-preview-label"></span>
              <button class="btn btn-ghost btn-sm" onclick="clearDiscordAvatar()" style="font-size:11px">✕ Remove</button>
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">Main Webhook URL</label>
            <div class="test-row">
              <input class="input" id="s-discord_webhook_url" value="${s.discord_webhook_url||''}" placeholder="https://discord.com/api/webhooks/…"/>
              <button class="btn btn-blue btn-sm" onclick="testDiscord(this)">Test</button>
            </div>
          </div>
          <div class="form-group">
            <label class="form-label">Webhook URL — Torrent Added <span style="font-weight:400;color:var(--muted)">(optional)</span></label>
            <input class="input" id="s-discord_webhook_added" value="${s.discord_webhook_added||''}" placeholder="https://discord.com/api/webhooks/…"/>
          </div>
          <div class="toggle-row">
            <div class="toggle-info"><div class="tl">Notify on Added</div></div>
            <label class="toggle"><input type="checkbox" id="s-discord_notify_added" ${s.discord_notify_added?'checked':''}><div class="ttrack"></div></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-info"><div class="tl">Notify on Finished</div></div>
            <label class="toggle"><input type="checkbox" id="s-discord_notify_finished" ${s.discord_notify_finished?'checked':''}><div class="ttrack"></div></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-info"><div class="tl">Notify on Error</div></div>
            <label class="toggle"><input type="checkbox" id="s-discord_notify_error" ${s.discord_notify_error?'checked':''}><div class="ttrack"></div></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-info"><div class="tl">Notify on new version</div><div class="ts">Send a webhook when a newer release is available on GitHub.</div></div>
            <label class="toggle"><input type="checkbox" id="s-discord_notify_update" ${s.discord_notify_update!==false?'checked':''}><div class="ttrack"></div></label>
          </div>
          <div class="form-group">
            <label class="form-label">Version check interval (hours) <span style="font-size:9px;color:var(--text3);font-weight:400">0 = disabled</span></label>
            <input class="input" id="s-update_check_interval_hours" type="number" min="0" max="168" style="width:90px" value="${s.update_check_interval_hours??12}"/>
            <span class="form-hint">How often GitHub is polled for a new release. Default: 12 h.</span>
          </div>
        </div>
      </div>
      <div class="scard" style="border-color:rgba(59,130,246,.3)">
      <div class="scard-header">ℹ️ About Reporting</div>
      <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Periodic statistics snapshots and optional Discord-based statistics reports.</p>
      <div class="scard-body">
        <div style="font-size:12px;line-height:1.6;color:var(--text2)">
          The reporting module captures <b>comprehensive metrics</b> across all client activity automatically.<br><br>
          <b>Snapshots</b> are periodic point-in-time captures stored in the database for trend analysis.
          Set an interval to enable automatic snapshots (recommended: 60 min).<br><br>
          <b>Export</b> downloads a full JSON report for the selected time window — useful for external analysis or archiving.<br><br>
          <b>Time windows</b>: select the period in the dropdown below, then click <i>Load Report</i>.
        </div>
      </div>
    </div>

    <div class="scard">
      <div class="scard-header">📊 Statistics Reporting</div>
      <div class="scard-body">
        <div class="form-group">
          <label class="form-label">Snapshot Interval (minutes, 0 = disabled)</label>
          <input class="input" type="number" id="s-stats_snapshot_interval_minutes" value="${s.stats_snapshot_interval_minutes??60}" min="0"/>
          <span class="form-hint">How often to capture a statistics snapshot. Default: 60.</span>
        </div>
        <div class="form-group">
          <label class="form-label">Keep Snapshots (days)</label>
          <input class="input" type="number" id="s-stats_snapshot_keep_days" value="${s.stats_snapshot_keep_days??30}" min="1"/>
        </div>
        <div class="form-group">
          <label class="form-label">Event Log Retention (days, 0 = keep forever)</label>
          <input class="input" type="number" id="s-events_keep_days" value="${s.events_keep_days??30}" min="0"/>
          <span class="form-hint">Events older than this are deleted daily. Torrent rows are never deleted — duplicate download prevention is not affected.</span>
        </div>
        <div class="form-group">
          <label class="form-label">Reporting Webhook URL <span style="font-weight:400;color:var(--text2)">(optional — uses main Discord webhook if empty)</span></label>
          <input class="input" id="s-stats_report_webhook_url" value="${s.stats_report_webhook_url||''}" placeholder="Leave empty to use main Discord webhook"/>
          <span class="form-hint">Receives structured reporting payloads as Discord embeds. Falls back to Settings → Discord → Webhook URL when empty.</span>
        </div>
        <div class="form-group">
          <label class="form-label">Automatic Report Interval (hours, 0 = disabled)</label>
          <input class="input" type="number" id="s-stats_report_interval_hours" value="${s.stats_report_interval_hours??0}" min="0" max="168"/>
          <span class="form-hint">How often the report is sent automatically. 0 = disabled.</span>
        </div>
        <div class="form-group">
          <label class="form-label">Report Window (hours)</label>
          <input class="input" type="number" id="s-stats_report_window_hours" value="${s.stats_report_window_hours??24}" min="1" max="8760"/>
          <span class="form-hint">Time window covered by each automatic report (default: 24h).</span>
        </div>
        <div class="form-group">
          <label class="form-label">Time Window</label>
          <select class="input" id="stats-report-hours" onchange="loadComprehensiveStats()">
            <option value="24"${Number(s.stats_report_window_hours ?? 24)===24?' selected':''}>Last 24 hours</option>
            <option value="168"${Number(s.stats_report_window_hours ?? 24)===168?' selected':''}>Last 7 days</option>
            <option value="720"${Number(s.stats_report_window_hours ?? 24)===720?' selected':''}>Last 30 days</option>
            <option value="8760"${Number(s.stats_report_window_hours ?? 24)===8760?' selected':''}>All time (~1 year)</option>
          </select>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:4px">
          <button class="btn btn-blue btn-sm" onclick="loadComprehensiveStats()">📊 Load Report</button>
          <button class="btn btn-ghost btn-sm" onclick="exportStats()">⬇ Export JSON</button>
          <button class="btn btn-ghost btn-sm" onclick="triggerStatsSnapshot(this)">📸 Snapshot Now</button>
          <button class="btn btn-ghost btn-sm" onclick="sendStatsReport(this)">📨 Send Webhook Now</button>
        </div>
        <div id="comprehensive-stats" style="margin-top:14px"></div>
      </div>
    </div>

    </div>`);
  _sf.insertAdjacentHTML('beforeend', `<div class="stab-panel" id="tab-advanced">
      <div class="scard">
        <div class="scard-header">🏷 Labels</div>
        <div class="scard-body">
          <div class="form-group">
            <label class="form-label">Predefined Labels <span style="font-weight:400;color:var(--text2)">(comma-separated)</span></label>
            <input class="input" id="s-torrent_labels_raw" value="${(s.torrent_labels||[]).join(', ')}" placeholder="Movies, Series, 4K, Anime"/>
            <span class="form-hint">Leave empty — labels are optional per torrent.</span>
          </div>
        </div>
      </div>
      <div class="scard">
      <div class="scard-header">🚫 File Filters</div>
      <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Skip unwanted files by extension, keyword, or minimum file size.</p>
      <div class="scard-body">
        <div class="toggle-row">
          <div class="toggle-info">
            <div class="tl">Enable File Filters</div>
            <div class="td">When off, all files are downloaded regardless of extension, keyword or size rules.</div>
          </div>
          <label class="toggle"><input type="checkbox" id="s-filters_enabled" ${s.filters_enabled!==false?'checked':''} onchange="toggleFilterFields()"><div class="ttrack"></div></label>
        </div>
        <div id="filter-fields" style="${s.filters_enabled===false?'opacity:.4;pointer-events:none':''}">
          <div class="form-group">
            <label class="form-label">Blocked Extensions (one per line)</label>
            <textarea class="input" id="s-blocked_extensions" rows="6">${(s.blocked_extensions||[]).join('\n')}</textarea>
            <span class="form-hint">e.g. .jpg · .png · .nfo — images are blocked by default</span>
          </div>
          <div class="form-group">
            <label class="form-label">Blocked Keywords (one per line)</label>
            <textarea class="input" id="s-blocked_keywords" rows="3">${(s.blocked_keywords||[]).join('\n')}</textarea>
          </div>
          <div class="form-group">
            <label class="form-label">Minimum File Size (MB, 0 = no limit)</label>
            <input class="input" type="number" id="s-min_file_size_mb" value="${s.min_file_size_mb??0}" min="0"/>
          </div>
          <div class="toggle-row" style="margin-top:10px">
            <div class="toggle-info">
              <div class="tl">Block sample / trailer files</div>
              <div class="td">Automatically skip files matching sample, trailer, or teaser patterns.</div>
            </div>
            <label class="toggle"><input type="checkbox" id="s-block_samples" ${s.block_samples?'checked':''}/><span class="slider"></span></label>
          </div>
          <div class="toggle-row">
            <div class="toggle-info">
              <div class="tl">Block extras / featurettes</div>
              <div class="td">Automatically skip files in /Extras/, /Featurettes/, /Behind the Scenes/ sub-folders.</div>
            </div>
            <label class="toggle"><input type="checkbox" id="s-block_extras" ${s.block_extras?'checked':''}/><span class="slider"></span></label>
          </div>
        </div>
      </div>
    </div>

      <div class="scard">
      <div class="scard-header">⏱ Polling Intervals</div>
      <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">How often AllDebrid is checked for new activity.</p>
      <div class="scard-body">
        <div class="form-group">
          <label class="form-label">AllDebrid Poll Interval (seconds)</label>
            <input class="input" type="number" id="s-poll_interval_seconds" value="${s.poll_interval_seconds??30}" min="10"/>
          <span class="form-hint">How often to ask AllDebrid for torrent status. Default: 30 s. Minimum: 10 s. Lower = faster detection but more API calls.</span>
        </div>
      </div>
    </div>
      <div class="scard">
        <div class="scard-header">💾 Automatic Backups</div>
      <p class="form-hint" style="padding:4px 14px 6px;margin:0;font-size:11px;color:var(--text3)">Automatically create periodic database backups to prevent data loss.</p>
        <div class="scard-body">
          <div class="toggle-row">
            <div class="toggle-info"><div class="tl">Enable Backups</div><div class="ts">Automatically back up config and database</div></div>
            <label class="toggle"><input type="checkbox" id="s-backup_enabled" ${s.backup_enabled!==false?'checked':''}><div class="ttrack"></div></label>
          </div>
          <div class="form-group">
            <label class="form-label">Backup Folder</label>
            <input class="input" id="s-backup_folder" value="${s.backup_folder||'/app/data/backups'}"/>
          </div>
          <div class="form-group">
            <label class="form-label">Interval (hours)</label>
            <input class="input" type="number" id="s-backup_interval_hours" value="${s.backup_interval_hours??24}" min="1" max="168"/>
            <span class="form-hint">Default: 24h. Backup runs once per interval.</span>
          </div>
          <div class="form-group">
            <label class="form-label">Keep backups for (days)</label>
            <input class="input" type="number" id="s-backup_keep_days" value="${s.backup_keep_days??7}" min="1" max="90"/>
            <span class="form-hint">Default: 7 days. Older backups are deleted automatically.</span>
          </div>
          <div style="display:flex;gap:8px;margin-top:4px">
            <button class="btn btn-ghost" onclick="triggerBackup()">💾 Run Backup Now</button>
            <button class="btn btn-ghost" onclick="loadBackupList()">📋 List Backups</button>
          </div>
          <div id="backup-list" style="margin-top:10px;font-size:12px;color:var(--text2)"></div>
        </div>
      </div>
    </div>

    <div class="stab-panel" id="tab-database">
    <div class="scard">
      <div class="scard-header">🛠️ Database Maintenance</div>
      <div class="scard-body">
        <div class="form-group">
          <label class="form-label">Database Backup Folder</label>
          <input class="input" id="s-db_backup_folder" value="${s.db_backup_folder||'/app/data/db-backups'}"/>
        </div>
        <div class="toggle-row">
          <div class="toggle-info"><div class="tl">Enable Database Backups</div><div class="ts">Create JSON snapshots of the database only</div></div>
          <label class="toggle"><input type="checkbox" id="s-db_backup_enabled" ${s.db_backup_enabled!==false?'checked':''}><div class="ttrack"></div></label>
        </div>
        <div class="form-group">
          <label class="form-label">Keep database backups for (days)</label>
          <input class="input" type="number" id="s-db_backup_keep_days" value="${s.db_backup_keep_days??7}" min="1" max="365"/>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-ghost btn-sm" onclick="triggerDatabaseBackup()">💽 Run DB Backup Now</button>
          <button class="btn btn-ghost btn-sm" onclick="loadDatabaseBackupList()">📋 List DB Backups</button>
        </div>
        <div id="db-backup-list" style="margin-top:10px;font-size:12px;color:var(--text2)"></div>
        <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">
          <div class="toggle-row">
            <div class="toggle-info"><div class="tl">Allow Database Wipe</div><div class="ts">Required before the wipe action can run</div></div>
            <label class="toggle"><input type="checkbox" id="s-db_wipe_enabled" ${s.db_wipe_enabled?'checked':''}><div class="ttrack"></div></label>
          </div>
          <div class="toggle-row" style="margin-top:10px">
            <div class="toggle-info"><div class="tl">Backup Before Wipe</div><div class="ts">Run a DB backup automatically before deleting rows</div></div>
            <label class="toggle"><input type="checkbox" id="s-db_backup_before_wipe" ${s.db_backup_before_wipe!==false?'checked':''}><div class="ttrack"></div></label>
          </div>
          <div class="form-hint" style="margin-top:10px">Pause processing first. Wipe clears torrents, files, events, and stats snapshots.</div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
            <button class="btn btn-danger btn-sm" onclick="wipeDatabase(this)">🗑️ Wipe Database</button>
          </div>
        </div>
      </div>
    </div>
    </div>`);
  requestAnimationFrame(() => {
    _sf.scrollTop = _settingsScrollTop;
  });
}

function getFormSettings() {
  const g = id => document.getElementById('s-'+id);
  const t = id => g(id)?.value?.trim() || '';
  const n = (id, fallback = 0) => {
    const raw = g(id)?.value;
    if (raw == null || raw === '') return fallback;
    const parsed = parseInt(raw, 10);
    return Number.isNaN(parsed) ? fallback : parsed;
  };
  const c = id => g(id)?.checked||false;
  const l = id => g(id)?.value?.split('\n').map(x=>x.trim()).filter(Boolean)||[];
  const reportHoursRaw = document.getElementById('stats-report-hours')?.value;
  const reportWindowHours = (() => {
    if (reportHoursRaw == null || reportHoursRaw === '') return Number(settingsData.stats_report_window_hours ?? 24);
    const parsed = parseInt(reportHoursRaw, 10);
    return Number.isNaN(parsed) ? Number(settingsData.stats_report_window_hours ?? 24) : parsed;
  })();
  const maxConcurrentDownloads = n(
    'aria2_max_active_downloads',
    Number(settingsData.max_concurrent_downloads ?? 3),
  );
  const secretFields = [
    'alldebrid_api_key', 'aria2_secret', 'discord_webhook_url',
    'discord_webhook_added', 'stats_report_webhook_url',
    'auth_password', 'extraction_password'
  ];
  const clearSecrets = secretFields.filter(field =>
    document.getElementById(`s-clear-${field}`)?.checked
  );
  return {
    ...settingsData,
    clear_secrets: clearSecrets,
    alldebrid_api_key: t('alldebrid_api_key'),
    auth_username: t('auth_username'),
    auth_password: t('auth_password'),
    alldebrid_agent:   t('alldebrid_agent')||'DebridPulse',
    download_folder: t('download_folder'), max_concurrent_downloads: maxConcurrentDownloads,
    max_speed_mbps: (settingsData && settingsData.max_speed_mbps != null)
                   ? settingsData.max_speed_mbps : 0,
    download_client: t('download_client') || (settingsData && settingsData.download_client) || 'aria2',
    aria2_mode: t('aria2_mode') || 'builtin',
    aria2_url: (t('aria2_mode') || 'external') === 'builtin' ? (settingsData.aria2_url || 'http://127.0.0.1:6800/jsonrpc') : t('aria2_url'),
    aria2_secret: (t('aria2_mode') || 'external') === 'builtin' ? (settingsData.aria2_secret || '') : t('aria2_secret'),
    aria2_download_path: t('aria2_download_path'),
    aria2_builtin_auto_start: c('aria2_builtin_auto_start'),
    aria2_builtin_port: n('aria2_builtin_port', 6800),
    aria2_builtin_log_max_mb: n('aria2_builtin_log_max_mb', 25),
    aria2_builtin_log_backups: n('aria2_builtin_log_backups', 3),
    aria2_operation_timeout_seconds: n('aria2_operation_timeout_seconds', 15),
    aria2_max_active_downloads: maxConcurrentDownloads,
    aria2_start_paused: c('aria2_start_paused'),
    aria2_poll_interval_seconds: n('aria2_poll_interval_seconds', 5),
    aria2_purge_interval_minutes: n('aria2_purge_interval_minutes', 15),
    aria2_max_download_result: n('aria2_max_download_result', 50),
    aria2_waiting_window: n('aria2_waiting_window', 100),
    aria2_stopped_window: n('aria2_stopped_window', 100),
    aria2_keep_unfinished_download_result: c('aria2_keep_unfinished_download_result'),
    aria2_split: n('aria2_split', 16),
    aria2_min_split_size: t('aria2_min_split_size') || '10M',
    aria2_max_connection_per_server: n('aria2_max_connection_per_server', 16),
    aria2_disk_cache: t('aria2_disk_cache') || '64M',
    aria2_file_allocation: t('aria2_file_allocation') || 'falloc',
    aria2_continue_downloads: c('aria2_continue_downloads'),
    aria2_lowest_speed_limit: t('aria2_lowest_speed_limit') || '0',
    aria2_max_upload_limit: n('aria2_max_upload_limit', 0),
    discord_username: t('discord_username') || 'DebridPulse',
    discord_avatar_url: t('discord_avatar_url'),
    discord_webhook_url: t('discord_webhook_url'),
    discord_webhook_added: t('discord_webhook_added'),
    discord_notify_added: c('discord_notify_added'), discord_notify_finished: c('discord_notify_finished'),
    discord_notify_error: c('discord_notify_error'),
    discord_notify_update: c('discord_notify_update'),
    update_check_interval_hours: n('update_check_interval_hours', 12),
    torrent_labels: (t('torrent_labels_raw')||'').split(',').map(s=>s.trim()).filter(Boolean),
    stuck_download_timeout_hours: n('stuck_download_timeout_hours'),
    alldebrid_rate_limit_per_minute: n('alldebrid_rate_limit_per_minute'),
    full_sync_interval_minutes: n('full_sync_interval_minutes'),
    backup_enabled: c('backup_enabled'), backup_folder: t('backup_folder'),
    backup_interval_hours: n('backup_interval_hours'), backup_keep_days: n('backup_keep_days'),
    db_backup_enabled: c('db_backup_enabled'), db_backup_folder: t('db_backup_folder'),
    db_backup_keep_days: n('db_backup_keep_days'),
    db_wipe_enabled: c('db_wipe_enabled'), db_backup_before_wipe: c('db_backup_before_wipe'),
    blocked_extensions: l('blocked_extensions'), blocked_keywords: l('blocked_keywords'),
    min_file_size_mb: n('min_file_size_mb'), poll_interval_seconds: n('poll_interval_seconds', 30),
    filters_enabled: c('filters_enabled'),
    aria2_deep_sync_interval_minutes: n('aria2_deep_sync_interval_minutes'),
    aria2_error_retry_count:           n('aria2_error_retry_count'),
      upload_fail_retry_count:         n('upload_fail_retry_count', 3),
      upload_fail_retry_delay_minutes: n('upload_fail_retry_delay_minutes', 5),
      extract_enabled:          c('extract_enabled'),
      extract_delete_archive:   c('extract_delete_archive', true),
      extract_max_concurrent:   n('extract_max_concurrent', 1),
      discord_notify_extract:   c('discord_notify_extract', true),
    aria2_error_retry_delay_seconds: n('aria2_error_retry_delay_seconds'),
    stats_snapshot_interval_minutes: n('stats_snapshot_interval_minutes'),
    stats_snapshot_keep_days: n('stats_snapshot_keep_days'),
    stats_report_interval_hours: n('stats_report_interval_hours'),
    stats_report_window_hours: reportWindowHours,
    stats_report_webhook_url: t('stats_report_webhook_url'),
    // Disk space guard
    min_free_disk_gb: parseFloat(g('min_free_disk_gb')?.value || '0') || 0,
    disk_guard_interval_seconds: n('disk_guard_interval_seconds', 60),
    disk_guard_resume_hysteresis_gb: parseFloat(g('disk_guard_resume_hysteresis_gb')?.value || '0.5') || 0.5,
    // Extraction: filter empty entries on save, join with newline for backend
    extraction_password: _extractionPasswords.filter(function(p){ return p.trim(); }).join('\n'),
  };
}

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
  const daysLabel = days > 0 ? `${days} days` : 'expired';
  lbl.innerHTML = `Premium until ${dd}.${mm}.${yyyy} (${daysLabel})`;
  row.style.display = '';
}

function switchSettingsTab(id) {
  const requestedTab = document.querySelector(`#settings-tabs .stab[data-tab="${id}"]`);
  if (!requestedTab) id = 'tab-general';
  document.querySelectorAll('#settings-tabs .stab').forEach(t => t.classList.toggle('active', t.dataset.tab === id));
  document.querySelectorAll('#settings-form .stab-panel').forEach(p => p.classList.toggle('active', p.id === id));
  updateSettingsFooterActions(id);
  if (aria2DownloadsTimer) {
    clearInterval(aria2DownloadsTimer);
    aria2DownloadsTimer = null;
  }
  if (id === 'tab-advanced') {
    loadDatabaseBackupList();
  }
  if (id === 'tab-extract') {
    initExtractionPasswordList();
  }
  if (id === 'tab-download') {
    loadAria2Runtime().catch(()=>{});
    loadAria2Downloads().catch(()=>{});
    aria2DownloadsTimer = setInterval(() => {
      const panel = document.getElementById('tab-download');
      if (panel && panel.classList.contains('active')) loadAria2Downloads().catch(()=>{});
    }, 5000);
  }
}

function updateSettingsFooterActions(activeTab) {
  document.querySelectorAll('[data-settings-test-tab]').forEach(button => {
    let visible = button.dataset.settingsTestTab === activeTab;
    button.hidden = !visible;
  });
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
    const error = job.error_message ? `<div class="aria2-error">${esc(job.error_code || '')} ${esc(job.error_message)}</div>` : '';
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
  // ── Debug helper — shows status in UI (removed in production) ──────────────
  function dbg(msg) {
    const el = document.getElementById('debug-status');
    if (!el) return;
    el.style.display = 'block';
    const row = document.createElement('div');
    row.textContent = new Date().toLocaleTimeString() + ' — ' + String(msg ?? '');
    el.appendChild(row);
  }

  dbg('Script gestartet');
  setDot('api',   'check', 'AllDebrid: checking…');
  setDot('aria2', 'check', 'aria2: checking…');
  setDot('db',    'check', 'DB: checking…');

  // Load settings
  dbg('Lade Settings…');
  try {
    settingsData = await api('GET', '/settings');
    dbg('Settings OK');
  } catch(e) {
    dbg('Settings ERROR: ' + e.message);
  }

  renderTopbarActions();
  updateAria2ngLink();

  // Load stats with visible retry
  dbg('Starte loadStats…');
  let statsLoaded = false;
  let statsAttempt = 0;

  while (!statsLoaded) {
    statsAttempt++;
    dbg('loadStats Versuch ' + statsAttempt);

    statsLoaded = await loadStats();

    if (!statsLoaded) {
      const delay =
        Math.min(
          400 + statsAttempt * 400,
          3000
        );

      dbg(
        'Error — retrying in ' +
        delay +
        'ms…'
      );

      await new Promise(
        r => setTimeout(r, delay)
      );

      if (statsAttempt >= 10) {
        dbg(
          'Aufgegeben nach 10 Versuchen'
        );
        break;
      }
    }
  }

  // Start background tasks immediately — do not wait for stats
  loadRecent().catch(() => {});
  checkConnections().catch(() => {});
  checkPremiumStatus().catch(() => {});

  if (statsLoaded) {
    dbg('Stats loaded ✓');

    setTimeout(() => {
      const el =
        document.getElementById(
          'debug-status'
        );

      if (el) {
        el.style.display = 'none';
      }
    }, 5000);
  } else {
    dbg(
      'Stats failed to load. Please reload the page.'
    );

    setDot(
      'api',
      'error',
      'AllDebrid: Error'
    );
  }

  setInterval(
    checkPremiumStatus,
    12 * 60 * 60 * 1000
  );

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


// ── Downloads View (aria2 Queue) ─────────────────────────────────────────────

var _aria2qTimer = null;

// Consecutive error counter for the aria2 download panel — used to
// back off the polling interval after repeated failures so we don't
// flood logs when aria2 is restarting or temporarily unreachable.
var _aria2qErrCount = 0;

async function loadAria2QueueView() {
  clearTimeout(_aria2qTimer);
  var isActive = !!(document.getElementById('view-aria2queue')
                    ?.classList.contains('active'));

  // Event-delegation for action buttons (register once per tbody lifetime)
  var tb2 = document.getElementById('aria2q-tbody');
  if (tb2 && !tb2._delegated) {
    tb2._delegated = true;
    tb2.addEventListener('click', function(e) {
      var btn = e.target.closest('[data-gid]');
      if (!btn) return;
      var gid = decodeURIComponent(btn.getAttribute('data-gid') || '');
      var act = btn.getAttribute('data-act') || '';
      if (gid && act) aria2QueueAction(gid, act, btn);
    });
  }

  try {
    // Use a 20 s timeout — aria2 can be slow when it has many completed items.
    // The default 8 s is too short and causes spurious "Request timed out" errors
    // that blank the panel and stop the poll loop.
    var data = await api('GET', '/aria2/downloads', null, 20000);
    _aria2qErrCount = 0;                 // reset error streak on success

    // Hide error banner (if shown from a previous failure)
    var errBanner = document.getElementById('aria2q-err-banner');
    if (errBanner) errBanner.style.display = 'none';

    renderAria2QueueView(data);

    // Refresh sidebar badge
    var badge = document.getElementById('nb-aria2-active');
    var cnt   = (data.summary || {}).active || 0;
    if (badge) { badge.textContent = cnt; badge.style.display = cnt > 0 ? '' : 'none'; }

    // Update topbar badge: active count + live speed
    updateAria2TopbarBadge({
      active:  cnt,
      liveBps: (data.summary || {}).download_speed || 0,
    });

    // Sync speed / max-concurrent controls
    loadAria2SpeedLimit();

  } catch(e) {
    _aria2qErrCount++;

    // Show a non-destructive error banner above the existing table rows
    // so previously fetched rows stay visible during a temporary outage.
    var errBanner = document.getElementById('aria2q-err-banner');
    if (errBanner) {
      errBanner.style.display = '';
      errBanner.innerHTML =
        '<span style="color:var(--red)">&#9888; aria2 unreachable</span>'
        + ' <span style="color:var(--text2);font-size:11px">('
        + esc(e.message || 'unknown error') + ')'
        + ' — retrying in ' + (_aria2qErrCount > 3 ? '10' : '3') + ' s</span>';
    } else {
      // Fallback: only replace tbody when it contains no real rows yet
      var tb = document.getElementById('aria2q-tbody');
      var hasRows = tb && tb.querySelector('tr[data-gid]');
      if (tb && !hasRows) {
        tb.innerHTML =
          '<tr><td colspan="7" style="text-align:center;padding:32px">'
          + '<div style="color:var(--red);margin-bottom:6px">&#9888; aria2 unreachable</div>'
          + '<div style="color:var(--text2);font-size:12px">' + esc(e.message || '') + '</div>'
          + '<div style="color:var(--text3);font-size:11px;margin-top:6px">Retrying automatically…</div>'
          + '</td></tr>';
      }
    }
  }

  // Always reschedule while the view is active — even after errors.
  // Use an exponential back-off: 2 s on success, 3 s after 1–3 errors,
  // 10 s after 4+ consecutive errors (aria2 likely restarting).
  if (isActive) {
    var delay = _aria2qErrCount === 0 ? 2000
               : _aria2qErrCount < 4  ? 3000
               :                        10000;
    _aria2qTimer = setTimeout(loadAria2QueueView, delay);
  }
}

function renderAria2QueueView(data) {
  var summary = data.summary || {};
  var items   = data.items   || [];

  // Summary bar
  var sb = document.getElementById('aria2q-summary');
  if (sb) {
    sb.innerHTML =
      '<span class="aria2-chip" style="font-size:12px"><b>' + (summary.active||0) + '</b>&nbsp;active</span>' +
      '<span class="aria2-chip" style="font-size:12px"><b>' + (summary.waiting||0) + '</b>&nbsp;waiting</span>' +
      '<span class="aria2-chip" style="font-size:12px"><b>' + (summary.stopped||0) + '</b>&nbsp;stopped</span>' +
      '<span class="aria2-chip" style="font-size:12px">&#9660;&nbsp;' + fmtSpeed(summary.download_speed||0) + '</span>' +
      '<span class="aria2-chip" style="font-size:12px">Remaining:&nbsp;' + fmtSize(summary.remaining_length||0) + '</span>';
  }

  var tb = document.getElementById('aria2q-tbody');
  if (!tb) return;

  if (!items.length) {
    tb.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:32px;color:var(--text2)">No downloads in aria2 queue.</td></tr>';
    return;
  }

  // Sort: active first, then waiting/paused, then stopped
  items = items.slice().sort(function(a,b) {
    var w = {active:0, waiting:1, paused:2, error:3, complete:4, removed:5};
    return ((w[a.status]||9) - (w[b.status]||9));
  });

  tb.innerHTML = items.map(function(job) {
    var pct = Math.min(100, Math.max(0, job.progress||0));
    var isActive  = job.status === 'active';
    var isPaused  = job.status === 'paused';
    var isWaiting = job.status === 'waiting';
    var canPause  = isActive || isWaiting;
    var canResume = isPaused;
    var canRemove = job.status !== 'complete';
    var barColor  = job.status === 'error' ? 'var(--red)' : isActive ? 'var(--accent)' : isPaused ? 'var(--text3)' : 'var(--green)';

    var firstFile = (job.files||[])[0] || {};
    var name = job.name || firstFile.name || job.gid || '—';
    var fileCount = (job.files||[]).length;
    var nameLabel = fileCount > 1 ? esc(name) + ' <span style="color:var(--text2);font-size:11px">(+' + (fileCount-1) + ' more)</span>' : esc(name);

    var statusDot = isActive ? '<span style="color:var(--accent)">&#9679;</span>' :
                   isPaused  ? '<span style="color:var(--text3)">&#9646;</span>' :
                   job.status === 'error' ? '<span style="color:var(--red)">&#10007;</span>' :
                   job.status === 'complete' ? '<span style="color:var(--green)">&#10003;</span>' :
                   '<span style="color:var(--text3)">&#9675;</span>';

    var progressBar =
      '<div style="width:100%;background:var(--surface2);border-radius:3px;height:5px;overflow:hidden">' +
        '<div style="width:'+pct+'%;background:'+barColor+';height:100%;border-radius:3px;transition:width .4s"></div>' +
      '</div>' +
      '<div style="font-size:10px;color:var(--text2);margin-top:2px">' + pct.toFixed(1) + '%</div>';

    var gEsc = encodeURIComponent(job.gid);
    var actions =
      (canPause  ? '<button class="btn btn-ghost btn-sm" style="padding:2px 7px;font-size:11px" data-gid="'+gEsc+'" data-act="pause"   title="Pause"  >&#9646;&#9646;</button>' : '') +
      (canResume ? '<button class="btn btn-ghost btn-sm" style="padding:2px 7px;font-size:11px" data-gid="'+gEsc+'" data-act="resume"  title="Resume" >&#9654;</button>' : '') +
      (canRemove ? '<button class="btn btn-ghost btn-sm" style="padding:2px 7px;font-size:11px;color:var(--red)" data-gid="'+gEsc+'" data-act="remove"  title="Remove" >&#128465;</button>' : '');

    return '<tr>' +
      '<td style="text-align:center">' + statusDot + '</td>' +
      '<td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(job.name||'') + '">' + nameLabel + '</td>' +
      '<td>' + progressBar + '</td>' +
      '<td style="font-size:12px;white-space:nowrap">' + fmtSize(job.total_length||0) + '</td>' +
      '<td style="font-size:12px;white-space:nowrap;color:var(--accent)">' + (isActive ? (fmtSpeed(job.download_speed||0) + (job.eta_seconds ? '<br><span style="font-size:10px;color:var(--text2)">ETA ' + fmtEta(job.eta_seconds) + '</span>' : '')) : '—') + '</td>' +
      '<td style="font-size:12px;white-space:nowrap">' + aria2StatusLabel(job.status) + '</td>' +
      '<td style="white-space:nowrap">' + actions + '</td>' +
    '</tr>';
  }).join('');
}

async function aria2QueueAction(gid, action, button) {
  const pendingLabel =
    action === 'pause'
      ? 'Pausing…'
      : action === 'resume'
        ? 'Resuming…'
        : 'Removing…';

  setButtonPending(button, true, pendingLabel);

  try {
    await api(
      'POST',
      '/aria2/downloads/' +
        encodeURIComponent(gid) +
        '/' +
        action
    );

    toast('aria2: ' + action + ' sent', 'success');
    await loadAria2QueueView();
  } catch(e) {
    toast(
      'aria2 ' + action + ': ' + e.message,
      'error'
    );
  } finally {
    setButtonPending(button, false);
  }
}


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



// ── System Health Bar (Dashboard) ─────────────────────────────────────────────

async function updateHealthBar() {
  var bar = document.getElementById('dash-health-bar');
  if (!bar) return;
  try {
    var res = await api('POST', '/recovery/run', {}, 15000);
    var r = res.result || {};
    var items = [];
    if (r.orphaned_queued_files)  items.push('🔧 ' + r.orphaned_queued_files + ' orphaned file(s) reset');
    if (r.missed_completions)     items.push('✅ ' + r.missed_completions + ' completion(s) recovered');
    if (r.deadlock_reset)         items.push('⚡ Queue deadlock cleared');
    if (items.length) {
      bar.style.display = '';
      document.getElementById('dash-health-recovery').innerHTML = items.join(' &nbsp;·&nbsp; ');
    } else {
      bar.style.display = 'none';
    }
  } catch(e) { /* silently ignore */ }
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
