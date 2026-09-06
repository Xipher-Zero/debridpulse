/* DebridPulse 1.0.12 UI Correction Batch 1 — final interaction corrections.
 * Owns the last two review deltas only: archive-password interaction and
 * server-backed Activity Log filtering. Canonical Settings/API owners remain
 * unchanged.
 */
(function () {
  'use strict';

  const EVENT_LIMIT = 500;
  const SEARCH_DEBOUNCE_MS = 275;
  const TIMEFRAMES = Object.freeze([
    ['1h', 'Last hour'],
    ['12h', 'Last 12 hours'],
    ['24h', 'Last day'],
    ['72h', 'Last 3 days'],
    ['7d', 'Last week'],
    ['30d', 'Last 30 days'],
    ['all', 'Available history'],
  ]);

  let eventGeneration = 0;
  let searchTimer = null;
  let archiveRows = null;
  let archiveKeySerial = 0;
  let archiveRevealAll = false;
  let archiveActiveKey = null;
  let archiveRenderBusy = false;

  function injectStyles() {
    if (document.getElementById('dp-ui-correction-batch1-final-style')) return;
    const style = document.createElement('style');
    style.id = 'dp-ui-correction-batch1-final-style';
    style.textContent = `
      body.dp-v11-structural #view-events .dp-activity-search-row {
        align-items: end;
        gap: 12px;
      }
      body.dp-v11-structural #view-events .dp-activity-search-row > #ev-search {
        min-height: 38px;
      }
      body.dp-v11-structural #view-events .dp-activity-filter-field {
        display: grid;
        grid-template-rows: auto auto;
        gap: 5px;
        flex: 0 0 164px;
        width: 164px;
        min-width: 0;
      }
      body.dp-v11-structural #view-events .dp-activity-filter-label {
        color: var(--dp-text-secondary, var(--text2));
        font-size: 11px;
        line-height: 1;
        font-weight: 700;
      }
      body.dp-v11-structural #view-events .dp-activity-filter-field .input,
      body.dp-v11-structural #view-events .dp-activity-filter-field .dp-dropdown-shell {
        width: 100% !important;
        max-width: 100% !important;
        flex: 1 1 auto !important;
      }
      body.dp-v11-structural #view-events .dp-activity-result-note {
        margin: 10px 16px 0;
        padding: 8px 10px;
        border: 1px solid color-mix(in srgb, var(--dp-accent-purple, var(--accent)) 24%, var(--border));
        border-radius: 7px;
        color: var(--dp-text-muted, var(--text2));
        background: color-mix(in srgb, var(--dp-accent-purple, var(--accent)) 6%, transparent);
        font-size: 11px;
        line-height: 1.4;
      }
      body.dp-v11-structural #view-events .dp-activity-result-note[hidden] { display: none; }
      body.dp-v11-structural #view-settings .dp-settings-extraction-password-field > .form-hint {
        display: block;
        margin-top: 7px;
        max-width: 760px;
      }
      @media (max-width: 900px) {
        body.dp-v11-structural #view-events .dp-activity-search-row { flex-wrap: wrap; align-items: stretch; }
        body.dp-v11-structural #view-events .dp-activity-search-row > #ev-search { flex: 1 0 100%; width: 100%; }
        body.dp-v11-structural #view-events .dp-activity-filter-field { flex: 1 1 180px; width: auto; }
      }
    `;
    document.head.appendChild(style);
  }

  function sanitizeText(value) {
    return String(value ?? '');
  }

  function configuredTimeZone() {
    try {
      const zone = String(settingsData?.timezone || '').trim();
      return zone || 'UTC';
    } catch (_) {
      return 'UTC';
    }
  }

  function parseApiTimestamp(value) {
    if (!value) return null;
    try {
      if (typeof parseApiDate === 'function') return parseApiDate(value);
    } catch (_) {}
    const raw = String(value);
    const normalized = /[zZ]|[+-]\d\d:\d\d$/.test(raw) ? raw : `${raw.replace(' ', 'T')}Z`;
    const date = new Date(normalized);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatActivityTimestamp(value) {
    const date = parseApiTimestamp(value);
    if (!date) return sanitizeText(value) || '—';
    try {
      return new Intl.DateTimeFormat('en-US', {
        timeZone: configuredTimeZone(),
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      }).format(date);
    } catch (_) {
      return date.toLocaleString('en-US', {
        year: 'numeric', month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      });
    }
  }

  function visibleSeverity(level) {
    const normalized = String(level || 'info').trim().toLowerCase();
    if (normalized === 'warn' || normalized === 'warning') return 'Warning';
    if (normalized === 'error') return 'Error';
    return 'Info';
  }

  function severityClass(level) {
    const normalized = String(level || 'info').trim().toLowerCase();
    if (normalized === 'warning') return 'warn';
    return ['info', 'warn', 'error'].includes(normalized) ? normalized : 'info';
  }

  function renderActivity(events) {
    const list = document.getElementById('event-list');
    if (!list) return;
    const items = Array.isArray(events) ? events : [];
    list.replaceChildren();
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'No events match the current filters.';
      list.appendChild(empty);
      document.dispatchEvent(new CustomEvent('debridpulse:activity-rendered'));
      return;
    }

    items.forEach(event => {
      const row = document.createElement('div');
      row.className = 'dp-activity-row';

      const level = document.createElement('div');
      level.className = `elevel dp-activity-level ${severityClass(event.level)}`;
      level.setAttribute('aria-label', visibleSeverity(event.level));
      level.title = visibleSeverity(event.level);

      const copy = document.createElement('div');
      copy.className = 'dp-activity-copy';
      const message = document.createElement('div');
      message.className = 'emsg dp-activity-message';
      message.textContent = sanitizeText(event.message);
      copy.appendChild(message);
      if (event.torrent_name) {
        const transfer = document.createElement('div');
        transfer.className = 'ename dp-activity-transfer';
        transfer.textContent = sanitizeText(event.torrent_name);
        copy.appendChild(transfer);
      }

      const time = document.createElement('div');
      time.className = 'etime dp-activity-time';
      time.textContent = formatActivityTimestamp(event.created_at);
      time.title = sanitizeText(event.created_at);

      row.append(level, copy, time);
      list.appendChild(row);
    });
    document.dispatchEvent(new CustomEvent('debridpulse:activity-rendered'));
  }

  function ensureActivityControls() {
    const row = document.querySelector('#view-events .dp-activity-search-row');
    const search = document.getElementById('ev-search');
    const severity = document.getElementById('ev-level');
    if (!row || !search || !severity) return null;

    search.removeAttribute('oninput');
    search.setAttribute('aria-label', 'Search activity messages or download names');

    let timeframe = document.getElementById('ev-timeframe');
    if (!timeframe) {
      const field = document.createElement('label');
      field.className = 'dp-activity-filter-field';
      const label = document.createElement('span');
      label.className = 'dp-activity-filter-label';
      label.textContent = 'Timeframe';
      timeframe = document.createElement('select');
      timeframe.className = 'input';
      timeframe.id = 'ev-timeframe';
      timeframe.setAttribute('aria-label', 'Timeframe');
      TIMEFRAMES.forEach(([value, text]) => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = text;
        timeframe.appendChild(option);
      });
      timeframe.value = 'all';
      field.append(label, timeframe);
      row.insertBefore(field, severity);
    }

    let severityField = severity.closest('.dp-activity-filter-field');
    if (!severityField) {
      severityField = document.createElement('label');
      severityField.className = 'dp-activity-filter-field';
      const label = document.createElement('span');
      label.className = 'dp-activity-filter-label';
      label.textContent = 'Severity';
      row.insertBefore(severityField, severity);
      severityField.append(label, severity);
    }
    severity.removeAttribute('onchange');
    severity.setAttribute('aria-label', 'Severity');
    severity.replaceChildren();
    [['', 'All'], ['info', 'Info'], ['warn', 'Warning'], ['error', 'Error']].forEach(([value, text]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = text;
      severity.appendChild(option);
    });

    const card = document.querySelector('#view-events > .dp-activity-card');
    let note = document.getElementById('dp-activity-result-note');
    if (!note && card) {
      note = document.createElement('div');
      note.id = 'dp-activity-result-note';
      note.className = 'dp-activity-result-note';
      note.setAttribute('role', 'status');
      note.hidden = true;
      const list = document.getElementById('event-list');
      card.insertBefore(note, list || null);
    }

    if (search.dataset.dpServerFilter !== '1') {
      search.dataset.dpServerFilter = '1';
      search.addEventListener('input', () => {
        if (searchTimer !== null) window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(() => {
          searchTimer = null;
          void loadActivityEvents();
        }, SEARCH_DEBOUNCE_MS);
      });
    }
    if (timeframe.dataset.dpServerFilter !== '1') {
      timeframe.dataset.dpServerFilter = '1';
      timeframe.addEventListener('change', () => void loadActivityEvents());
    }
    if (severity.dataset.dpServerFilter !== '1') {
      severity.dataset.dpServerFilter = '1';
      severity.addEventListener('change', () => void loadActivityEvents());
    }
    return {search, timeframe, severity, note};
  }

  function updateActivityNotice(note, payload) {
    if (!note) return;
    if (payload?.truncated === true) {
      note.textContent = `Showing the latest ${EVENT_LIMIT} matching events. Narrow the timeframe, severity, or search filters to see a more specific result set.`;
      note.hidden = false;
      return;
    }
    note.hidden = true;
    note.textContent = '';
  }

  async function loadActivityEvents() {
    const controls = ensureActivityControls();
    if (!controls || typeof api !== 'function') return null;
    const generation = ++eventGeneration;
    const params = new URLSearchParams();
    params.set('limit', String(EVENT_LIMIT));
    params.set('timeframe', controls.timeframe.value || 'all');
    const severity = controls.severity.value || '';
    const search = controls.search.value || '';
    if (severity) params.set('level', severity);
    if (search.trim()) params.set('search', search.trim());

    try {
      const payload = await api('GET', `/events?${params.toString()}`);
      if (generation !== eventGeneration) return null;
      const items = Array.isArray(payload) ? payload : (Array.isArray(payload?.items) ? payload.items : []);
      renderActivity(items);
      updateActivityNotice(controls.note, Array.isArray(payload) ? {truncated: false} : payload);
      return payload;
    } catch (error) {
      if (generation !== eventGeneration) return null;
      try { toast(sanitizeErrorMsg(error?.message || error), 'error'); } catch (_) {}
      return null;
    }
  }

  function bindActivityRuntime() {
    ensureActivityControls();
    try { loadEvents = loadActivityEvents; } catch (_) {}
    try { filterEvents = loadActivityEvents; } catch (_) {}
    window.loadEvents = loadActivityEvents;
    window.filterEvents = loadActivityEvents;
  }

  function archiveSource() {
    return document.querySelector('#view-settings [data-panel="extraction"] [data-setting="extraction_password"]');
  }

  function archiveEditor() {
    return document.querySelector('#view-settings [data-panel="extraction"] .dp-settings-extraction-password-editor');
  }

  function nextArchiveKey() {
    const key = `new:${archiveKeySerial}`;
    archiveKeySerial += 1;
    return key;
  }

  function normalizeArchiveRows(values) {
    const rows = Array.isArray(values) ? values : [];
    while (rows.length > 1 && rows[rows.length - 1].value === '' && rows[rows.length - 2].value === '') {
      rows.pop();
    }
    if (!rows.length || rows[rows.length - 1].value !== '') rows.push({key: nextArchiveKey(), value: ''});
    return rows;
  }

  function initializeArchiveRows(source) {
    if (archiveRows) return;
    const values = String(source?.value || '')
      .replace(/\r\n?/g, '\n')
      .split('\n')
      .map(value => value.trim())
      .filter(Boolean);
    archiveRows = values.map(value => ({key: nextArchiveKey(), value}));
    normalizeArchiveRows(archiveRows);
  }

  function syncArchiveSource(source) {
    if (!source || !archiveRows) return;
    source.value = archiveRows
      .map(row => String(row.value || '').trim())
      .filter(Boolean)
      .join('\n');
    const panel = source.closest('[data-panel="extraction"]');
    const clear = panel?.querySelector('[data-clear-secret="extraction_password"]');
    if (clear) clear.checked = source.value.length === 0;
  }

  function archiveEyeSvg(revealed) {
    if (revealed) {
      return '<svg class="lucide dp-utility-icon" data-dp-lucide="eye-off" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="m2 2 20 20"/><path d="M6.7 6.7C4.4 8.2 3 10.5 3 12c0 0 3.5 6 9 6 1.4 0 2.7-.4 3.8-1"/><path d="M10.7 10.7a2 2 0 0 0 2.6 2.6"/><path d="M9.9 4.2A10 10 0 0 1 12 4c5.5 0 9 6 9 8 0 .7-.3 1.5-.8 2.3"/></svg>';
    }
    return '<svg class="lucide dp-utility-icon" data-dp-lucide="eye" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M2.1 12s3.5-7 9.9-7 9.9 7 9.9 7-3.5 7-9.9 7-9.9-7-9.9-7Z"/><circle cx="12" cy="12" r="3"/></svg>';
  }

  function setArchiveEyeState(button) {
    if (!button) return;
    const label = archiveRevealAll ? 'Hide all passwords' : 'Show all passwords';
    button.setAttribute('aria-pressed', archiveRevealAll ? 'true' : 'false');
    button.setAttribute('aria-label', label);
    button.title = label;
    button.classList.toggle('is-open', archiveRevealAll);
    button.innerHTML = archiveEyeSvg(archiveRevealAll);
  }

  function focusArchiveKey(editor, key) {
    if (!editor || !key) return;
    requestAnimationFrame(() => {
      const input = Array.from(editor.querySelectorAll('.dp-settings-password-line'))
        .find(node => node.dataset.passwordKey === key);
      if (!input) return;
      input.focus();
      const end = input.value.length;
      try { input.setSelectionRange(end, end); } catch (_) {}
    });
  }

  function renderArchiveEditor(editor, source, focusKey = null) {
    if (!editor || !source || archiveRenderBusy) return;
    archiveRenderBusy = true;
    try {
      normalizeArchiveRows(archiveRows);
      let rowsHost = editor.querySelector('.dp-settings-password-rows');
      if (!rowsHost) {
        rowsHost = document.createElement('div');
        rowsHost.className = 'dp-settings-password-rows';
        editor.prepend(rowsHost);
      }
      rowsHost.replaceChildren();

      archiveRows.forEach((row, index) => {
        const input = document.createElement('input');
        input.className = 'dp-settings-password-line';
        input.type = archiveRevealAll || archiveActiveKey === row.key ? 'text' : 'password';
        input.value = row.value;
        input.dataset.passwordKey = row.key;
        input.dataset.passwordIndex = String(index);
        input.autocomplete = 'off';
        input.spellcheck = false;
        input.setAttribute('aria-label', `Archive password ${index + 1}`);
        if (index === archiveRows.length - 1 && !row.value) input.placeholder = 'Add an archive password';

        input.addEventListener('focus', () => {
          archiveActiveKey = row.key;
          input.type = 'text';
          if (!archiveRevealAll) {
            rowsHost.querySelectorAll('.dp-settings-password-line').forEach(other => {
              if (other !== input) other.type = 'password';
            });
          }
        });
        input.addEventListener('blur', () => {
          queueMicrotask(() => {
            const active = document.activeElement;
            if (active?.closest('.dp-settings-extraction-password-editor') !== editor) {
              archiveActiveKey = null;
              if (!archiveRevealAll) {
                rowsHost.querySelectorAll('.dp-settings-password-line').forEach(other => { other.type = 'password'; });
              }
            }
          });
        });
        input.addEventListener('input', () => {
          row.value = input.value;
          syncArchiveSource(source);
          const wasTail = index === archiveRows.length - 1;
          if (wasTail && row.value !== '') {
            normalizeArchiveRows(archiveRows);
            renderArchiveEditor(editor, source, row.key);
          }
        });
        input.addEventListener('keydown', event => {
          if (event.key === 'Enter') {
            event.preventDefault();
            row.value = input.value;
            syncArchiveSource(source);
            const inserted = {key: nextArchiveKey(), value: ''};
            archiveRows.splice(index + 1, 0, inserted);
            normalizeArchiveRows(archiveRows);
            archiveActiveKey = inserted.key;
            renderArchiveEditor(editor, source, inserted.key);
            return;
          }
          if (event.key === 'Backspace' && input.value === '' && archiveRows.length > 1) {
            event.preventDefault();
            const removedKey = row.key;
            archiveRows.splice(index, 1);
            if (archiveActiveKey === removedKey) archiveActiveKey = null;
            normalizeArchiveRows(archiveRows);
            const target = archiveRows[Math.max(0, Math.min(index - 1, archiveRows.length - 1))];
            archiveActiveKey = target?.key || null;
            syncArchiveSource(source);
            renderArchiveEditor(editor, source, target?.key || null);
            return;
          }
          if (event.altKey && (event.key === 'ArrowUp' || event.key === 'ArrowDown')) {
            const targetIndex = event.key === 'ArrowUp' ? index - 1 : index + 1;
            if (targetIndex < 0 || targetIndex >= archiveRows.length) return;
            event.preventDefault();
            row.value = input.value;
            [archiveRows[index], archiveRows[targetIndex]] = [archiveRows[targetIndex], archiveRows[index]];
            normalizeArchiveRows(archiveRows);
            archiveActiveKey = row.key;
            syncArchiveSource(source);
            renderArchiveEditor(editor, source, row.key);
          }
        });
        input.addEventListener('paste', event => {
          const text = event.clipboardData?.getData('text') || '';
          if (!/[\r\n]/.test(text)) return;
          event.preventDefault();
          const normalized = text.replace(/\r\n?/g, '\n');
          const incoming = normalized.split('\n');
          const start = input.selectionStart ?? input.value.length;
          const end = input.selectionEnd ?? input.value.length;
          const before = input.value.slice(0, start);
          const after = input.value.slice(end);
          incoming[0] = before + incoming[0];
          incoming[incoming.length - 1] = incoming[incoming.length - 1] + after;
          const replacements = incoming.map(value => ({key: nextArchiveKey(), value}));
          archiveRows.splice(index, 1, ...replacements);
          normalizeArchiveRows(archiveRows);
          const target = replacements[replacements.length - 1];
          archiveActiveKey = target.key;
          syncArchiveSource(source);
          renderArchiveEditor(editor, source, target.key);
        });

        rowsHost.appendChild(input);
      });

      let eye = editor.querySelector('.dp-settings-password-eye');
      if (eye && eye.dataset.dpLatchedReveal !== '1') {
        const replacement = eye.cloneNode(false);
        replacement.type = 'button';
        replacement.className = eye.className;
        replacement.dataset.dpLatchedReveal = '1';
        eye.replaceWith(replacement);
        eye = replacement;
        eye.addEventListener('click', () => {
          archiveRevealAll = !archiveRevealAll;
          setArchiveEyeState(eye);
          rowsHost.querySelectorAll('.dp-settings-password-line').forEach(input => {
            input.type = archiveRevealAll || input.dataset.passwordKey === archiveActiveKey ? 'text' : 'password';
          });
        });
      }
      setArchiveEyeState(eye);
      editor.dataset.dpLatchedReveal = '1';
      syncArchiveSource(source);
      if (focusKey) focusArchiveKey(editor, focusKey);
    } finally {
      archiveRenderBusy = false;
    }
  }

  function applyArchivePasswordContract() {
    const source = archiveSource();
    const editor = archiveEditor();
    if (!source || !editor) return;
    initializeArchiveRows(source);

    const field = source.closest('.dp-settings-extraction-password-field');
    const hint = field?.querySelector(':scope > .form-hint');
    if (hint) {
      hint.textContent = 'Add one password per line. Passwords stay hidden unless selected. Use the eye to show or hide all passwords.';
    }

    if (editor.dataset.dpLatchedReveal !== '1') renderArchiveEditor(editor, source);
  }

  function observeArchiveEditor() {
    const settings = document.getElementById('view-settings');
    if (!settings) return;
    const observer = new MutationObserver(() => applyArchivePasswordContract());
    observer.observe(settings, {childList: true, subtree: true});
    document.addEventListener('debridpulse:settings-rendered', applyArchivePasswordContract);
    applyArchivePasswordContract();
  }

  function init() {
    injectStyles();
    bindActivityRuntime();
    observeArchiveEditor();
    if (document.getElementById('view-events')?.classList.contains('active')) void loadActivityEvents();
  }

  window.DPUICorrectionBatch1Final = Object.freeze({
    loadActivityEvents,
    formatActivityTimestamp,
    timeframes: TIMEFRAMES.map(([value]) => value),
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
  else init();
})();
