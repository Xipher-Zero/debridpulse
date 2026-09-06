/* DebridPulse 1.0.12 P4 corrective presentation runtime.
 *
 * Repairs only the two reviewed P4 presentation defects that escaped the first
 * qualification pass: Activity Log filter grouping/options and the Archive
 * Passwords reveal control. Existing server filtering and line-editor state
 * remain owned by ui-correction-batch1-final.js.
 */
(function () {
  'use strict';

  const TIMEFRAMES = Object.freeze([
    ['1h', 'Last hour'],
    ['12h', 'Last 12 hours'],
    ['24h', 'Last day'],
    ['72h', 'Last 3 days'],
    ['7d', 'Last week'],
    ['30d', 'Last 30 days'],
    ['all', 'Available history'],
  ]);
  const SEVERITIES = Object.freeze([
    ['', 'All'],
    ['info', 'Info'],
    ['warning', 'Warning'],
    ['error', 'Error'],
  ]);

  let scheduled = false;

  function injectStyles() {
    if (document.getElementById('dp-ui-correction-p4-repair-style')) return;
    const style = document.createElement('style');
    style.id = 'dp-ui-correction-p4-repair-style';
    style.textContent = `
      body.dp-v11-structural #view-events .dp-activity-search-row {
        display: flex !important;
        align-items: center !important;
        gap: 12px !important;
      }
      body.dp-v11-structural #view-events .dp-activity-search-row > #ev-search {
        flex: 1 1 360px !important;
        width: auto !important;
        min-width: 260px !important;
        min-height: 38px !important;
        height: 38px !important;
        margin: 0 !important;
      }
      body.dp-v11-structural #view-events .dp-activity-filter-field {
        display: inline-flex !important;
        flex: 0 0 auto !important;
        flex-direction: row !important;
        align-items: center !important;
        gap: 8px !important;
        width: auto !important;
        min-width: 0 !important;
        max-width: none !important;
        margin: 0 !important;
      }
      body.dp-v11-structural #view-events .dp-activity-filter-label {
        display: inline-flex !important;
        flex: 0 0 auto !important;
        align-items: center !important;
        justify-content: center !important;
        margin: 0 !important;
        color: var(--dp-text-secondary, var(--text2)) !important;
        font-size: 11px !important;
        line-height: 1.1 !important;
        font-weight: 600 !important;
      }
      body.dp-v11-structural #view-events .dp-activity-filter-field--time .dp-activity-filter-label {
        width: 42px !important;
        flex-direction: column !important;
        text-align: center !important;
        white-space: normal !important;
      }
      body.dp-v11-structural #view-events .dp-activity-filter-field--severity .dp-activity-filter-label {
        white-space: nowrap !important;
      }
      body.dp-v11-structural #view-events .dp-activity-filter-field--time .dp-dropdown-shell {
        width: 174px !important;
        min-width: 174px !important;
        max-width: 174px !important;
        flex: 0 0 174px !important;
      }
      body.dp-v11-structural #view-events .dp-activity-filter-field--severity .dp-dropdown-shell {
        width: 116px !important;
        min-width: 116px !important;
        max-width: 116px !important;
        flex: 0 0 116px !important;
      }
      body.dp-v11-structural #view-events .dp-activity-filter-field .dp-dropdown__trigger {
        width: 100% !important;
        min-width: 0 !important;
        max-width: none !important;
        min-height: 38px !important;
        height: 38px !important;
        box-sizing: border-box !important;
      }
      body.dp-v11-structural #view-events .dp-activity-filter-field select:not(.dp-native-select--enhanced) {
        min-height: 38px !important;
        height: 38px !important;
      }
      body.dp-v11-structural #view-events #ev-reset {
        flex: 0 0 auto !important;
        align-self: center !important;
        white-space: nowrap !important;
      }

      body.dp-v11-structural #view-settings .dp-settings-extraction-password-editor {
        max-height: none !important;
        padding: 8px 11px 50px !important;
        overflow: visible !important;
      }
      body.dp-v11-structural #view-settings .dp-settings-password-eye.dp-settings-password-eye--ghost {
        appearance: none !important;
        position: absolute !important;
        right: 9px !important;
        bottom: 9px !important;
        width: auto !important;
        min-width: 88px !important;
        max-width: none !important;
        height: 32px !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
        gap: 7px !important;
        padding: 5px 10px !important;
        border: 1px solid color-mix(in srgb, var(--dp-border-strong) 64%, transparent) !important;
        border-radius: 8px !important;
        background: transparent !important;
        color: var(--dp-text-secondary) !important;
        font-family: var(--dp-font-sans) !important;
        font-size: 11px !important;
        line-height: 1 !important;
        font-weight: 600 !important;
        white-space: nowrap !important;
        cursor: pointer !important;
        box-shadow: none !important;
        transition: color .14s ease, border-color .14s ease, background-color .14s ease, box-shadow .14s ease !important;
      }
      body.dp-v11-structural #view-settings .dp-settings-password-eye.dp-settings-password-eye--ghost:hover {
        color: var(--dp-text-primary) !important;
        border-color: var(--dp-border-strong) !important;
        background: color-mix(in srgb, var(--dp-surface-2) 58%, transparent) !important;
      }
      body.dp-v11-structural #view-settings .dp-settings-password-eye.dp-settings-password-eye--ghost:focus-visible {
        color: var(--dp-text-primary) !important;
        border-color: color-mix(in srgb, var(--dp-accent-purple-bright) 58%, var(--dp-border-strong)) !important;
        background: color-mix(in srgb, var(--dp-surface-2) 48%, transparent) !important;
        outline: none !important;
        box-shadow: var(--dp-focus-ring) !important;
      }
      body.dp-v11-structural #view-settings .dp-settings-password-eye.dp-settings-password-eye--ghost.is-open {
        color: var(--dp-accent-purple-bright) !important;
        border-color: color-mix(in srgb, var(--dp-accent-purple-bright) 42%, var(--dp-border-default)) !important;
        background: color-mix(in srgb, var(--dp-accent-purple) 8%, transparent) !important;
      }
      body.dp-v11-structural #view-settings .dp-settings-password-eye.dp-settings-password-eye--ghost svg {
        width: 16px !important;
        height: 16px !important;
        flex: 0 0 16px !important;
        display: block !important;
        fill: none !important;
        stroke: currentColor !important;
        color: currentColor !important;
      }
      body.dp-v11-structural #view-settings .dp-settings-password-eye-label {
        display: inline-block !important;
        color: currentColor !important;
      }
    `;
    document.head.appendChild(style);
  }

  function setOptions(select, definitions, fallback) {
    if (!select) return;
    const current = select.value;
    const expected = definitions.map(([, label]) => label);
    const actual = Array.from(select.options || []).map(option => option.textContent || '');
    const exact = expected.length === actual.length && expected.every((label, index) => actual[index] === label);
    if (!exact) {
      select.replaceChildren();
      definitions.forEach(([value, label]) => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = label;
        select.appendChild(option);
      });
    }
    const valid = definitions.some(([value]) => value === current);
    select.value = valid ? current : fallback;
    select.dataset.dpOptions = '1';
    select.dataset.dpP4Options = '1';
  }

  function projectedShell(select) {
    if (!select) return null;
    if (select._dpDropdownShell?.isConnected) return select._dpDropdownShell;
    if (select.nextElementSibling?.classList?.contains('dp-dropdown-shell')) return select.nextElementSibling;
    return null;
  }

  function ensureTimeLabel(field) {
    let label = field.querySelector(':scope > .dp-activity-filter-label');
    if (!label) {
      label = document.createElement('span');
      label.className = 'dp-activity-filter-label';
      field.prepend(label);
    }
    const lines = Array.from(label.children).map(node => node.textContent || '');
    if (lines.length !== 2 || lines[0] !== 'Time' || lines[1] !== 'Window') {
      label.replaceChildren();
      const first = document.createElement('span');
      first.textContent = 'Time';
      const second = document.createElement('span');
      second.textContent = 'Window';
      label.append(first, second);
    }
  }

  function ensureSeverityLabel(field) {
    let label = field.querySelector(':scope > .dp-activity-filter-label');
    if (!label) {
      label = document.createElement('span');
      label.className = 'dp-activity-filter-label';
      field.prepend(label);
    }
    if (label.textContent !== 'Severity' || label.children.length) label.textContent = 'Severity';
  }

  function repairFilterField(select, kind) {
    if (!select) return null;
    let field = select.closest('.dp-activity-filter-field');
    if (!field) {
      field = document.createElement('label');
      field.className = 'dp-activity-filter-field';
      select.before(field);
      field.appendChild(select);
    }
    field.classList.toggle('dp-activity-filter-field--time', kind === 'time');
    field.classList.toggle('dp-activity-filter-field--severity', kind === 'severity');
    if (kind === 'time') ensureTimeLabel(field);
    else ensureSeverityLabel(field);

    const shell = projectedShell(select);
    if (shell && shell.parentElement !== field) field.appendChild(shell);
    else if (shell && shell.previousElementSibling !== select) field.append(select, shell);
    return field;
  }

  function repairActivity() {
    const row = document.querySelector('#view-events .dp-activity-search-row');
    const search = document.getElementById('ev-search');
    const timeframe = document.getElementById('ev-timeframe');
    const severity = document.getElementById('ev-level');
    const reset = document.getElementById('ev-reset');
    if (!row || !search || !timeframe || !severity) return;

    setOptions(timeframe, TIMEFRAMES, 'all');
    setOptions(severity, SEVERITIES, '');
    timeframe.setAttribute('aria-label', 'Time window');
    severity.setAttribute('aria-label', 'Severity');

    if (window.DPDropdowns?.refresh) window.DPDropdowns.refresh();

    const timeField = repairFilterField(timeframe, 'time');
    const severityField = repairFilterField(severity, 'severity');
    if (!timeField || !severityField) return;

    row.append(search, timeField, severityField);
    if (reset) row.appendChild(reset);
    row.dataset.dpP4Presentation = '1';
  }

  function lucideEye(hidden) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    svg.dataset.lucide = hidden ? 'eye-off' : 'eye';

    const path = value => {
      const node = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      node.setAttribute('d', value);
      svg.appendChild(node);
    };
    if (hidden) {
      path('m2 2 20 20');
      path('M6.71 6.71C4.58 8.13 3 10.25 3 12c0 0 3 7 9 7 1.86 0 3.47-.67 4.79-1.67');
      path('M10.73 10.73a2 2 0 0 0 2.83 2.83');
      path('M9.88 4.24A8.44 8.44 0 0 1 12 4c6 0 9 7 9 8a10.22 10.22 0 0 1-1.67 2.68');
    } else {
      path('M2.062 12.348a1 1 0 0 1 0-.696C3.49 7.742 7.24 5 12 5c4.76 0 8.51 2.742 9.938 6.652a1 1 0 0 1 0 .696C20.51 16.258 16.76 19 12 19c-4.76 0-8.51-2.742-9.938-6.652');
      const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circle.setAttribute('cx', '12');
      circle.setAttribute('cy', '12');
      circle.setAttribute('r', '3');
      svg.appendChild(circle);
    }
    return svg;
  }

  function repairArchiveButton() {
    const editor = document.querySelector('#view-settings .dp-settings-extraction-password-editor');
    const button = editor?.querySelector('.dp-settings-password-eye');
    if (!editor || !button) return;

    const revealed = button.getAttribute('aria-pressed') === 'true';
    const visible = revealed ? 'Hide all' : 'Show all';
    const action = revealed ? 'Hide all passwords' : 'Show all passwords';
    const icon = revealed ? 'eye-off' : 'eye';
    const currentIcon = button.querySelector('svg')?.dataset?.lucide || '';
    const currentLabel = button.querySelector('.dp-settings-password-eye-label')?.textContent || '';

    button.classList.add('dp-settings-password-eye--ghost');
    button.setAttribute('aria-label', action);
    button.title = action;
    button.dataset.dpP4GhostButton = '1';

    if (currentIcon !== icon || currentLabel !== visible || button.querySelector('img')) {
      const label = document.createElement('span');
      label.className = 'dp-settings-password-eye-label';
      label.textContent = visible;
      button.replaceChildren(lucideEye(revealed), label);
    }
  }

  function apply() {
    injectStyles();
    repairActivity();
    repairArchiveButton();
  }

  function scheduleApply() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => {
      scheduled = false;
      apply();
    });
  }

  function observe() {
    const root = document.body;
    if (!root) return;
    new MutationObserver(scheduleApply).observe(root, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['aria-pressed'],
    });
    document.addEventListener('debridpulse:settings-rendered', scheduleApply);
    document.addEventListener('debridpulse:navigation', scheduleApply);
  }

  window.DPUICorrectionP4Repair = Object.freeze({
    apply,
    timeframes: TIMEFRAMES.map(([value]) => value),
    severities: SEVERITIES.map(([value]) => value),
  });

  apply();
  observe();
  window.setTimeout(scheduleApply, 0);
})();
