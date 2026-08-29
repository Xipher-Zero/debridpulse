/* DebridPulse v1.0.11 Settings completion runtime.
 *
 * The clean Settings renderer remains authoritative for values and persistence.
 * This idempotent layer finishes Downloads/Sources presentation details and the
 * Extraction editing experience after that renderer paints the persistent root.
 * It does not own the Apply Settings commit boundary.
 */
(function () {
  'use strict';

  const CONFIGURED_SECRET_MASK = '••••••••••••••••••••••••••••••••••••••••••••••••';
  const EXTRACTION_HEADER_COPY = 'Automatically extract supported archives after a download completes.';
  const EXTRACTION_PASSWORD_HINT = 'Passwords DebridPulse should try when extracting protected archives. Add, edit, or remove entries as needed.';

  const RECOVERY_COPY = Object.freeze([
    [
      'min_free_disk_gb',
      'Minimum Free Disk Space (GB)',
      'Stops new downloads from starting when free disk space falls below this amount. Set to 0 to disable the disk-space guard.',
    ],
    [
      'disk_guard_resume_hysteresis_gb',
      'Resume Free Space Buffer (GB)',
      'Extra free space required above the minimum before DebridPulse starts downloads again. Helps prevent repeated stop/start behavior near the limit.',
    ],
    [
      'stuck_download_timeout_hours',
      'Stalled Download Timeout (hours)',
      'How long a download can remain stalled before DebridPulse attempts automatic recovery. Set to 0 to disable stalled-download recovery.',
    ],
    [
      'aria2_error_retry_count',
      'Download Error Retries',
      'How many times DebridPulse retries a download after aria2 reports an error. Set to 0 to disable automatic retries.',
    ],
    [
      'aria2_error_retry_delay_seconds',
      'Retry Delay (seconds)',
      'How long DebridPulse waits before retrying a download after an aria2 error. Set to 0 to retry immediately.',
    ],
  ]);

  const extractionPasswords = {
    loaded: false,
    failed: false,
    loading: null,
    values: [],
    activeIndex: -1,
    revealAll: false,
  };

  const root = () => document.getElementById('view-settings');

  function directChild(element, selector) {
    if (!element) return null;
    return Array.from(element.children).find(child => child.matches(selector)) || null;
  }

  function cardByTitle(panel, title) {
    if (!panel) return null;
    return Array.from(panel.children).find(card => {
      if (!card.classList.contains('dp-settings-card')) return false;
      const header = directChild(card, '.card-header');
      const titleNode = header?.querySelector('.card-title');
      return String(titleNode?.textContent || '').trim() === title;
    }) || null;
  }

  function setFieldCopy(panel, key, label, hint) {
    const control = panel?.querySelector(`[data-setting="${key}"]`);
    const field = control?.closest('.dp-settings-field');
    if (!field) return;

    const labelNode = directChild(field, '.form-label');
    if (labelNode && labelNode.textContent !== label) labelNode.textContent = label;

    let hintNode = directChild(field, '.form-hint');
    if (!hintNode) {
      hintNode = document.createElement('span');
      hintNode.className = 'form-hint';
      field.appendChild(hintNode);
    }
    if (hintNode.textContent !== hint) hintNode.textContent = hint;
  }

  function ensureRecoveryIdentity(recovery) {
    const header = directChild(recovery, '.card-header');
    const titleNode = header?.querySelector('.card-title');
    if (!titleNode) return;

    titleNode.classList.add('dp-settings-card-title--with-icon');
    if (titleNode.querySelector('.dp-settings-download-recovery-icon')) return;

    const icon = document.createElement('span');
    icon.className = 'dp-settings-download-recovery-icon';
    icon.setAttribute('aria-hidden', 'true');

    const image = document.createElement('img');
    image.src = '/icons/dp/download-safety-recovery.svg?v=1';
    image.alt = '';
    image.decoding = 'async';
    icon.appendChild(image);
    titleNode.prepend(icon);
  }

  function expandConfiguredSecretMasks(view) {
    view.querySelectorAll(
      '[data-panel="sources"] input[type="password"], [data-panel="downloads"] input[type="password"]'
    ).forEach(input => {
      const placeholder = String(input.getAttribute('placeholder') || '');
      if (placeholder && /^•+$/u.test(placeholder) && placeholder !== CONFIGURED_SECRET_MASK) {
        input.setAttribute('placeholder', CONFIGURED_SECRET_MASK);
      }
    });
  }

  function applyDownloads(view) {
    const panel = view.querySelector('[data-panel="downloads"]');
    if (!panel) return;

    const recovery = cardByTitle(panel, 'Download Safety & Recovery');
    if (recovery) {
      recovery.classList.add('dp-settings-download-recovery-card');
      ensureRecoveryIdentity(recovery);
    }

    for (const [key, label, hint] of RECOVERY_COPY) {
      setFieldCopy(panel, key, label, hint);
    }

    /* UI-only retirement: keep the loaded controls intact so Apply Settings
       preserves legacy values until their backend/config pruning pass. */
    const fileFilters = cardByTitle(panel, 'File Filters');
    if (fileFilters) {
      fileFilters.classList.add('dp-settings-file-filters-retired');
      fileFilters.setAttribute('aria-hidden', 'true');
      fileFilters.inert = true;
    }
  }

  function normalizePasswordLines(raw) {
    const text = String(raw || '').replace(/\r\n?/g, '\n');
    if (!text) return [''];
    const lines = text.split('\n').map(value => value.trim()).filter(Boolean);
    return lines.length ? lines : [''];
  }

  function serializedPasswords() {
    return extractionPasswords.values
      .map(value => String(value || '').trim())
      .filter(Boolean)
      .join('\n');
  }

  function passwordMask(value) {
    return '*'.repeat(String(value || '').length);
  }

  function extractionSource(panel) {
    return panel?.querySelector('[data-setting="extraction_password"]') || null;
  }

  function extractionEditor(panel) {
    return panel?.querySelector('.dp-settings-extraction-password-editor') || null;
  }

  function extractionClearCompat(panel) {
    return panel?.querySelector('[data-dp-extraction-clear-compat="1"]') || null;
  }

  function syncExtractionPasswordSource(panel) {
    if (!extractionPasswords.loaded) return;
    const source = extractionSource(panel);
    if (!source) return;

    const value = serializedPasswords();
    source.value = value;

    const clear = extractionClearCompat(panel);
    if (clear) clear.checked = value.length === 0;
  }

  function rowInput(editor, index) {
    return editor?.querySelector(`input[data-password-index="${index}"]`) || null;
  }

  function setRowVisibility(input, index, reveal) {
    if (!input) return;
    const value = String(extractionPasswords.values[index] || '');
    const next = reveal ? value : passwordMask(value);
    if (input.value !== next) input.value = next;
  }

  function setRevealAll(editor, reveal) {
    extractionPasswords.revealAll = reveal;
    editor?.classList.toggle('is-revealing-all', reveal);
    editor?.querySelector('.dp-settings-password-eye')?.classList.toggle('is-open', reveal);
    editor?.querySelectorAll('input[data-password-index]').forEach(input => {
      const index = Number(input.dataset.passwordIndex);
      setRowVisibility(input, index, reveal || index === extractionPasswords.activeIndex);
    });
  }

  function activatePasswordLine(editor, index) {
    if (extractionPasswords.activeIndex !== index) {
      const previous = rowInput(editor, extractionPasswords.activeIndex);
      if (previous && !extractionPasswords.revealAll) {
        setRowVisibility(previous, extractionPasswords.activeIndex, false);
      }
    }
    extractionPasswords.activeIndex = index;
    setRowVisibility(rowInput(editor, index), index, true);
  }

  function commitPasswordLine(panel, input) {
    const index = Number(input.dataset.passwordIndex);
    if (!Number.isInteger(index) || index < 0) return;
    extractionPasswords.values[index] = String(input.value || '');
    syncExtractionPasswordSource(panel);
  }

  function eyeSvg() {
    return `
      <svg class="dp-settings-password-eye-closed" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M3 3l18 18"></path>
        <path d="M10.6 10.7a2 2 0 002.8 2.8"></path>
        <path d="M9.9 4.2A10.8 10.8 0 0112 4c5.5 0 9 5 9 5a16.7 16.7 0 01-2 2.6"></path>
        <path d="M6.6 6.6C4.4 8 3 10 3 10s3.5 5 9 5a10.6 10.6 0 004-.8"></path>
      </svg>
      <svg class="dp-settings-password-eye-open" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12z"></path>
        <circle cx="12" cy="12" r="3"></circle>
      </svg>`;
  }

  function buildPasswordEditor(panel, field, source) {
    const editor = document.createElement('div');
    editor.className = 'input dp-settings-extraction-password-editor';
    editor.setAttribute('role', 'group');
    editor.setAttribute('aria-label', 'Archive passwords');

    const rows = document.createElement('div');
    rows.className = 'dp-settings-password-rows';
    editor.appendChild(rows);

    const eye = document.createElement('button');
    eye.type = 'button';
    eye.className = 'dp-settings-password-eye';
    eye.setAttribute('aria-label', 'Hold to reveal all archive passwords');
    eye.title = 'Hold to reveal all archive passwords';
    eye.innerHTML = eyeSvg();
    editor.appendChild(eye);

    const hiddenClear = document.createElement('input');
    hiddenClear.type = 'checkbox';
    hiddenClear.hidden = true;
    hiddenClear.tabIndex = -1;
    hiddenClear.dataset.clearSecret = 'extraction_password';
    hiddenClear.dataset.dpExtractionClearCompat = '1';
    field.appendChild(hiddenClear);

    const stopReveal = () => setRevealAll(editor, false);
    eye.addEventListener('pointerdown', event => {
      event.preventDefault();
      try { eye.setPointerCapture(event.pointerId); } catch (_) {}
      setRevealAll(editor, true);
    });
    eye.addEventListener('pointerup', event => {
      event.preventDefault();
      try { eye.releasePointerCapture(event.pointerId); } catch (_) {}
      stopReveal();
    });
    eye.addEventListener('pointercancel', stopReveal);
    eye.addEventListener('pointerleave', stopReveal);
    eye.addEventListener('blur', stopReveal);
    eye.addEventListener('keydown', event => {
      if (event.key !== ' ' && event.key !== 'Enter') return;
      event.preventDefault();
      setRevealAll(editor, true);
    });
    eye.addEventListener('keyup', event => {
      if (event.key !== ' ' && event.key !== 'Enter') return;
      event.preventDefault();
      stopReveal();
    });

    source.classList.add('dp-settings-extraction-password-source');
    source.setAttribute('aria-hidden', 'true');
    source.tabIndex = -1;
    source.insertAdjacentElement('afterend', editor);

    renderPasswordRows(panel, editor);
    syncExtractionPasswordSource(panel);
    return editor;
  }

  function renderPasswordRows(panel, editor, focusIndex = null) {
    const rows = editor.querySelector('.dp-settings-password-rows');
    if (!rows) return;
    rows.replaceChildren();

    if (!extractionPasswords.values.length) extractionPasswords.values = [''];

    extractionPasswords.values.forEach((value, index) => {
      const input = document.createElement('input');
      input.type = 'text';
      input.className = 'dp-settings-password-line';
      input.dataset.passwordIndex = String(index);
      input.autocomplete = 'off';
      input.spellcheck = false;
      input.setAttribute('aria-label', `Archive password ${index + 1}`);
      input.placeholder = index === 0 && !value ? 'Add an archive password' : '';
      input.value = extractionPasswords.revealAll || index === extractionPasswords.activeIndex
        ? String(value || '')
        : passwordMask(value);

      input.addEventListener('pointerdown', () => activatePasswordLine(editor, index));
      input.addEventListener('focus', () => activatePasswordLine(editor, index));
      input.addEventListener('input', () => commitPasswordLine(panel, input));
      input.addEventListener('blur', () => {
        commitPasswordLine(panel, input);
        queueMicrotask(() => {
          const active = document.activeElement;
          if (active?.closest('.dp-settings-extraction-password-editor') !== editor) {
            extractionPasswords.activeIndex = -1;
          }
          if (!extractionPasswords.revealAll) {
            setRowVisibility(input, index, index === extractionPasswords.activeIndex);
          }
        });
      });
      input.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
          event.preventDefault();
          commitPasswordLine(panel, input);
          extractionPasswords.values.splice(index + 1, 0, '');
          extractionPasswords.activeIndex = index + 1;
          renderPasswordRows(panel, editor, index + 1);
          syncExtractionPasswordSource(panel);
          return;
        }
        if (event.key === 'Backspace' && input.value === '' && extractionPasswords.values.length > 1) {
          event.preventDefault();
          extractionPasswords.values.splice(index, 1);
          const next = Math.max(0, index - 1);
          extractionPasswords.activeIndex = next;
          renderPasswordRows(panel, editor, next);
          syncExtractionPasswordSource(panel);
          return;
        }
        if (event.altKey && (event.key === 'ArrowUp' || event.key === 'ArrowDown')) {
          const target = event.key === 'ArrowUp' ? index - 1 : index + 1;
          if (target < 0 || target >= extractionPasswords.values.length) return;
          event.preventDefault();
          commitPasswordLine(panel, input);
          const current = extractionPasswords.values[index];
          extractionPasswords.values[index] = extractionPasswords.values[target];
          extractionPasswords.values[target] = current;
          extractionPasswords.activeIndex = target;
          renderPasswordRows(panel, editor, target);
          syncExtractionPasswordSource(panel);
        }
      });
      input.addEventListener('paste', event => {
        const paste = event.clipboardData?.getData('text') || '';
        if (!/[\r\n]/.test(paste)) return;
        event.preventDefault();
        commitPasswordLine(panel, input);
        const incoming = paste.replace(/\r\n?/g, '\n').split('\n');
        const before = String(extractionPasswords.values[index] || '').slice(0, input.selectionStart ?? 0);
        const after = String(extractionPasswords.values[index] || '').slice(input.selectionEnd ?? input.value.length);
        incoming[0] = before + incoming[0];
        incoming[incoming.length - 1] = incoming[incoming.length - 1] + after;
        extractionPasswords.values.splice(index, 1, ...incoming);
        extractionPasswords.activeIndex = index + incoming.length - 1;
        renderPasswordRows(panel, editor, extractionPasswords.activeIndex);
        syncExtractionPasswordSource(panel);
      });

      rows.appendChild(input);
    });

    if (Number.isInteger(focusIndex)) {
      const target = rowInput(editor, focusIndex);
      if (target) {
        requestAnimationFrame(() => {
          target.focus();
          const end = target.value.length;
          try { target.setSelectionRange(end, end); } catch (_) {}
        });
      }
    }
  }

  async function loadExtractionPasswords() {
    if (extractionPasswords.loaded || extractionPasswords.failed || extractionPasswords.loading) {
      return extractionPasswords.loading;
    }
    if (typeof api !== 'function') {
      extractionPasswords.failed = true;
      return null;
    }

    extractionPasswords.loading = (async () => {
      try {
        const payload = await api('GET', '/settings/extraction-passwords');
        extractionPasswords.values = normalizePasswordLines(payload?.passwords || '');
        extractionPasswords.loaded = true;
        extractionPasswords.failed = false;
        scheduleApply();
      } catch (error) {
        extractionPasswords.failed = true;
        console.error('[DebridPulse Settings] unable to load archive password list for editing');
      } finally {
        extractionPasswords.loading = null;
      }
    })();
    return extractionPasswords.loading;
  }

  function ensureExtractionIdentity(card) {
    const header = directChild(card, '.card-header');
    const titleNode = header?.querySelector('.card-title');
    if (!header || !titleNode) return;

    card.classList.add('dp-settings-extraction-card');
    titleNode.classList.add('dp-settings-card-title--with-icon');

    if (!titleNode.querySelector('.dp-settings-extraction-icon')) {
      const icon = document.createElement('span');
      icon.className = 'dp-settings-extraction-icon';
      icon.setAttribute('aria-hidden', 'true');
      const image = document.createElement('img');
      image.src = '/icons/dp/automatic-extraction.svg?v=1';
      image.alt = '';
      image.decoding = 'async';
      icon.appendChild(image);
      titleNode.prepend(icon);
    }

    let center = directChild(header, '.dp-settings-card-header-center');
    if (!center) {
      center = document.createElement('div');
      center.className = 'dp-settings-card-header-center';
      header.appendChild(center);
    }
    if (!center.querySelector('.dp-settings-extraction-header-copy')) {
      const copy = document.createElement('span');
      copy.className = 'dp-settings-extraction-header-copy';
      copy.textContent = EXTRACTION_HEADER_COPY;
      center.replaceChildren(copy);
    }

    let enable = directChild(header, '.dp-settings-extraction-enable');
    const enableInput = card.querySelector('#dp-settings-field-extract-enabled');
    if (!enable && enableInput) {
      const oldRow = enableInput.closest('.dp-settings-toggle');
      const toggleControl = oldRow?.querySelector('.toggle');
      enable = document.createElement('label');
      enable.className = 'dp-settings-extraction-enable';
      enable.htmlFor = enableInput.id;
      const label = document.createElement('span');
      label.className = 'form-label';
      label.textContent = 'Enable';
      enable.appendChild(label);
      if (toggleControl) enable.appendChild(toggleControl);
      header.appendChild(enable);
      if (oldRow && oldRow !== enable) oldRow.remove();
    }
  }

  function arrangeExtractionControls(panel, card) {
    const body = directChild(card, '.card-body');
    if (!body) return;

    directChild(body, '.dp-settings-copy')?.remove();

    setFieldCopy(
      panel,
      'extract_max_concurrent',
      'Concurrent Extractions',
      'Maximum number of extraction jobs DebridPulse can run at the same time.'
    );

    const concurrentInput = panel.querySelector('[data-setting="extract_max_concurrent"]');
    const concurrentField = concurrentInput?.closest('.dp-settings-field');
    const deleteInput = panel.querySelector('[data-setting="extract_delete_archive"]');
    const deleteRow = deleteInput?.closest('.dp-settings-toggle');
    const passwordSource = extractionSource(panel);
    const passwordField = passwordSource?.closest('.dp-settings-field');

    if (deleteRow) {
      deleteRow.classList.add('dp-settings-extraction-delete');
      const title = deleteRow.querySelector('.tl');
      const detail = deleteRow.querySelector('.td');
      if (title && title.textContent !== 'Delete Archives After Extraction') {
        title.textContent = 'Delete Archives After Extraction';
      }
      if (detail && detail.textContent !== 'Remove original archive files only after extraction completes successfully.') {
        detail.textContent = 'Remove original archive files only after extraction completes successfully.';
      }
    }

    let controls = directChild(body, '.dp-settings-extraction-controls-row');
    if (!controls && concurrentField && deleteRow) {
      controls = document.createElement('div');
      controls.className = 'dp-settings-extraction-controls-row';
      if (passwordField) body.insertBefore(controls, passwordField);
      else body.prepend(controls);
      controls.appendChild(concurrentField);
      controls.appendChild(deleteRow);
    }

    if (passwordField) {
      passwordField.classList.add('dp-settings-extraction-password-field');
      const label = directChild(passwordField, '.form-label');
      if (label && label.textContent !== 'Archive Passwords (one per line)') {
        label.textContent = 'Archive Passwords (one per line)';
      }
      let hint = directChild(passwordField, '.form-hint');
      if (!hint) {
        hint = document.createElement('span');
        hint.className = 'form-hint';
        passwordField.appendChild(hint);
      }
      if (hint.textContent !== EXTRACTION_PASSWORD_HINT) hint.textContent = EXTRACTION_PASSWORD_HINT;

      panel.querySelectorAll('[data-clear-secret="extraction_password"]:not([data-dp-extraction-clear-compat="1"])')
        .forEach(control => control.closest('label')?.remove());

      if (extractionPasswords.loaded) {
        if (!extractionEditor(panel)) buildPasswordEditor(panel, passwordField, passwordSource);
        else syncExtractionPasswordSource(panel);
      }
    }
  }

  function applyExtraction(view) {
    const panel = view?.querySelector('[data-panel="extraction"]');
    if (!panel) return;
    const card = cardByTitle(panel, 'Automatic Extraction');
    if (!card) return;

    ensureExtractionIdentity(card);
    arrangeExtractionControls(panel, card);
    if (!extractionPasswords.loaded && !extractionPasswords.failed) void loadExtractionPasswords();
  }

  function apply() {
    const view = root();
    if (!view) return;
    applyDownloads(view);
    applyExtraction(view);
    expandConfiguredSecretMasks(view);
  }

  let scheduled = false;
  let observer = null;

  function observe(view) {
    if (!observer || !view) return;
    observer.observe(view, {childList: true, subtree: true});
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

  function attach() {
    const view = root();
    if (!view) return;
    if (view.dataset.dpSettingsDownloadsCompletionBound === '1') {
      applyWithoutSelfObservation();
      return;
    }

    view.dataset.dpSettingsDownloadsCompletionBound = '1';
    observer = new MutationObserver(scheduleApply);
    observe(view);
    applyWithoutSelfObservation();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', attach, {once: true});
  } else {
    attach();
  }
})();
