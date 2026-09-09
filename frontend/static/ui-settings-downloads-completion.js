/* DebridPulse v1.0.12 Settings completion runtime.
 *
 * The clean Settings renderer remains authoritative for values and persistence.
 * This idempotent layer finishes Downloads/Sources presentation details and owns
 * the Built-in Download Folder browse interaction. The Automatic Extraction
 * archive-password editor is owned solely by ui-settings-archive-passwords.js;
 * this file only arranges the surrounding Extraction card layout. It does not
 * own the Apply Settings commit boundary.
 */
(function () {
  'use strict';

  const CONFIGURED_SECRET_MASK = '••••••••••••••••••••••••••••••••••••••••••••••••';
  const EXTRACTION_HEADER_COPY = 'Automatically extract supported archives after a download completes.';

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

  const DIRECTORY_REASON_LABELS = Object.freeze({
    none: 'Ready to use',
    low_space: 'Low free space',
    capacity_exhausted: 'No free space',
    quota_exhausted: 'Storage quota exhausted',
    read_only: 'Read-only',
    missing: 'Unavailable',
    invalid_path: 'Invalid path',
    inaccessible: 'Inaccessible',
    stat_failed: 'Capacity unavailable',
    io_error: 'Storage I/O unavailable',
    sqlite_io_error: 'Storage I/O unavailable',
    sqlite_open_failed: 'Storage unavailable',
  });

  const DIRECTORY_ERROR_LABELS = Object.freeze({
    invalid_path: 'The requested directory path is invalid.',
    relative_path: 'Only absolute server paths can be browsed.',
    not_directory: 'The requested path is not a directory.',
    symlink_loop: 'The requested path cannot be resolved safely.',
    path_inaccessible: 'The requested directory is not accessible.',
    path_unavailable: 'The requested directory is currently unavailable.',
    browser_unavailable: 'The server filesystem browser is currently unavailable.',
  });

  const root = () => document.getElementById('view-settings');

  function ensureDirectoryBrowserStyles() {
    if (document.querySelector('link[data-dp-settings-directory-browser-style="1"]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/ui-settings-directory-browser.css?v=1';
    link.dataset.dpSettingsDirectoryBrowserStyle = '1';
    document.head.appendChild(link);
  }

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

  function directoryReasonLabel(reason) {
    const key = String(reason || '').trim();
    if (Object.prototype.hasOwnProperty.call(DIRECTORY_REASON_LABELS, key)) {
      return DIRECTORY_REASON_LABELS[key];
    }
    return key ? key.replaceAll('_', ' ') : 'Unavailable';
  }

  function directoryErrorMessage(error) {
    const detail = error?.detail && typeof error.detail === 'object' ? error.detail : null;
    const code = String(error?.code || detail?.code || '').trim();
    if (Object.prototype.hasOwnProperty.call(DIRECTORY_ERROR_LABELS, code)) {
      return DIRECTORY_ERROR_LABELS[code];
    }
    if (typeof detail?.message === 'string' && detail.message.trim()) return detail.message.trim();
    const message = String(error?.message || '').trim();
    if (message && message !== '[object Object]' && message.length <= 240) return message;
    return 'This directory cannot be browsed right now.';
  }

  function directorySize(value) {
    if (value == null) return null;
    const number = Number(value);
    if (!Number.isFinite(number) || number < 0) return null;
    if (number === 0) return '0 B';
    if (typeof fmtSize === 'function') return fmtSize(number);
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let unit = 0;
    let scaled = number;
    while (scaled >= 1024 && unit < units.length - 1) {
      scaled /= 1024;
      unit += 1;
    }
    return `${scaled.toFixed(1)} ${units[unit]}`;
  }

  function ensureDownloadFolderBrowse(panel) {
    const fieldInput = panel?.querySelector('[data-setting="download_folder"]');
    const field = fieldInput?.closest('.dp-settings-field');
    if (!fieldInput || !field) return;
    field.classList.add('dp-settings-download-folder-field');

    let control = directChild(field, '.dp-settings-download-folder-control');
    if (!control) {
      control = document.createElement('div');
      control.className = 'dp-settings-download-folder-control';
      field.insertBefore(control, fieldInput);
      control.appendChild(fieldInput);
    }

    if (control.querySelector('[data-action="browse-download-folder"]')) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-ghost btn-sm dp-settings-download-folder-browse';
    button.dataset.action = 'browse-download-folder';
    button.textContent = 'Browse';
    button.setAttribute('aria-label', 'Browse server directories for Built-in Download Folder');
    button.addEventListener('click', () => browseDownloadFolder(button));
    control.appendChild(button);
  }

  function browseDownloadFolder(origin) {
    const field = root()?.querySelector('[data-setting="download_folder"]');
    const modalApi = window.DPSettingsModal;
    if (!field || typeof api !== 'function' || !modalApi || typeof modalApi.confirm !== 'function') return;

    const originalValue = String(field.value ?? '');
    let currentResponse = null;
    let generation = 0;
    let controller = null;

    const confirmation = modalApi.confirm({
      title: 'Choose Download Folder',
      message: '',
      confirmLabel: 'Use This Folder',
      tone: 'warning',
    });
    const overlays = Array.from(document.querySelectorAll('.dp-settings-confirm-overlay'));
    const overlay = overlays[overlays.length - 1];
    const dialog = overlay?.querySelector('.dp-settings-confirm-dialog');
    const body = overlay?.querySelector('.dp-settings-confirm-body');
    const cancel = overlay?.querySelector('[data-confirm-cancel]');
    const accept = overlay?.querySelector('[data-confirm-accept]');
    if (!overlay || !dialog || !body || !cancel || !accept) return;

    dialog.classList.add('dp-settings-directory-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.removeAttribute('aria-describedby');
    dialog.removeAttribute('data-tone');
    body.classList.add('dp-settings-directory-body');
    cancel.dataset.directoryCancel = '1';
    accept.dataset.directoryConfirm = '1';
    accept.disabled = true;
    cancel.focus();

    body.innerHTML = `
      <div class="dp-settings-directory-browser">
        <div class="dp-settings-directory-notice" data-directory-notice hidden></div>
        <div class="dp-settings-directory-current">
          <span class="dp-settings-directory-current-label">Current server path</span>
          <code class="dp-settings-directory-current-path" data-directory-current-path>—</code>
          <div class="dp-settings-directory-current-meta">
            <span class="dp-settings-directory-current-state" data-directory-current-state data-selectable="false">Not validated</span>
            <span data-directory-capacity>Capacity unavailable</span>
          </div>
        </div>
        <div class="dp-settings-directory-toolbar">
          <button class="btn btn-ghost btn-sm" type="button" data-directory-up disabled>Up</button>
          <span class="dp-settings-directory-loading" data-directory-loading role="status" aria-live="polite"></span>
        </div>
        <div class="dp-settings-directory-error" data-directory-error role="alert" hidden></div>
        <div class="dp-settings-directory-list" data-directory-list aria-label="Directories"></div>
      </div>`;

    const notice = body.querySelector('[data-directory-notice]');
    const currentPath = body.querySelector('[data-directory-current-path]');
    const currentState = body.querySelector('[data-directory-current-state]');
    const capacity = body.querySelector('[data-directory-capacity]');
    const up = body.querySelector('[data-directory-up]');
    const loading = body.querySelector('[data-directory-loading]');
    const errorBox = body.querySelector('[data-directory-error]');
    const list = body.querySelector('[data-directory-list]');

    const setLoading = busy => {
      dialog.setAttribute('aria-busy', busy ? 'true' : 'false');
      loading.textContent = busy ? 'Loading…' : '';
      accept.disabled = busy || currentResponse?.current?.selectable !== true;
      up.disabled = busy || currentResponse?.parent == null;
      list.querySelectorAll('[data-directory-row]').forEach(row => {
        row.disabled = busy || row.dataset.accessible !== 'true';
      });
    };

    const render = payload => {
      const current = payload?.current || null;
      currentResponse = payload && current ? payload : null;
      errorBox.hidden = true;
      errorBox.textContent = '';
      list.replaceChildren();

      if (!current) {
        currentPath.textContent = '—';
        currentState.textContent = 'Not validated';
        currentState.dataset.selectable = 'false';
        capacity.textContent = 'Capacity unavailable';
        accept.disabled = true;
        up.disabled = true;
        return;
      }

      currentPath.textContent = String(current.path ?? '');
      currentPath.title = String(current.path ?? '');
      const selectable = current.selectable === true;
      currentState.dataset.selectable = selectable ? 'true' : 'false';
      currentState.textContent = selectable
        ? 'Selectable as Download Storage'
        : `Not selectable — ${directoryReasonLabel(current.reason)}`;

      const total = directorySize(current.capacity?.total_bytes);
      const free = directorySize(current.capacity?.free_bytes);
      capacity.textContent = total !== null && free !== null
        ? `${free} free of ${total}`
        : 'Capacity unavailable';

      up.disabled = payload.parent == null;
      const children = Array.isArray(payload.children) ? payload.children : [];
      if (!children.length) {
        const empty = document.createElement('div');
        empty.className = 'dp-settings-directory-empty';
        empty.textContent = 'No child directories.';
        list.appendChild(empty);
      } else {
        children.forEach(child => {
          const row = document.createElement('button');
          row.type = 'button';
          row.className = 'dp-settings-directory-row';
          row.dataset.directoryRow = '1';
          row.dataset.path = String(child?.path ?? '');
          row.dataset.accessible = child?.accessible === true ? 'true' : 'false';
          row.disabled = child?.accessible !== true;
          row.title = String(child?.path ?? '');

          const name = document.createElement('span');
          name.className = 'dp-settings-directory-name';
          name.dataset.directoryName = '1';
          name.textContent = String(child?.name ?? child?.path ?? 'Directory');

          const hint = document.createElement('span');
          hint.className = 'dp-settings-directory-row-hint';
          hint.textContent = child?.accessible === true
            ? 'Open to validate'
            : directoryReasonLabel(child?.reason);

          row.append(name, hint);
          row.addEventListener('click', () => {
            if (row.dataset.accessible !== 'true') return;
            void loadDirectory(row.dataset.path);
          });
          list.appendChild(row);
        });
      }

      accept.disabled = !selectable;
    };

    const loadDirectory = async (path, {fallbackOnFailure = false} = {}) => {
      const requestGeneration = ++generation;
      if (controller) controller.abort();
      controller = new AbortController();
      errorBox.hidden = true;
      errorBox.textContent = '';
      setLoading(true);

      const query = path == null ? '' : `?${new URLSearchParams({path: String(path)}).toString()}`;
      try {
        const payload = await api('GET', `/settings/directories${query}`, undefined, 10000, {signal: controller.signal});
        if (requestGeneration !== generation || !overlay.isConnected) return;
        render(payload);
        setLoading(false);
      } catch (error) {
        if (requestGeneration !== generation || error?.name === 'AbortError' || !overlay.isConnected) return;
        if (fallbackOnFailure) {
          notice.hidden = false;
          notice.textContent = 'The current Download Folder cannot be browsed. Showing the server fallback location instead; the Settings field has not been changed.';
          void loadDirectory(null);
          return;
        }
        errorBox.textContent = directoryErrorMessage(error);
        errorBox.hidden = false;
        setLoading(false);
      }
    };

    up.addEventListener('click', () => {
      if (currentResponse?.parent == null) return;
      void loadDirectory(currentResponse.parent);
    });

    void confirmation.then(accepted => {
      generation += 1;
      if (controller) controller.abort();
      controller = null;
      if (!accepted || currentResponse?.current?.selectable !== true) return;
      const liveField = document.getElementById(field.id);
      if (!liveField) return;
      const canonicalPath = String(currentResponse.current.path ?? '');
      liveField.value = canonicalPath;
      liveField.dispatchEvent(new Event('input', {bubbles: true}));
      liveField.dispatchEvent(new Event('change', {bubbles: true}));
    });

    if (originalValue.length) void loadDirectory(originalValue, {fallbackOnFailure: true});
    else void loadDirectory(null);
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

    ensureDownloadFolderBrowse(panel);
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
    const passwordField = panel.querySelector('[data-setting="extraction_password"]')?.closest('.dp-settings-field');

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

    // The archive-password field label, hint, editor, reveal control, and
    // persistence sync are owned entirely by ui-settings-archive-passwords.js.
  }

  function applyExtraction(view) {
    const panel = view?.querySelector('[data-panel="extraction"]');
    if (!panel) return;
    const card = cardByTitle(panel, 'Automatic Extraction');
    if (!card) return;

    ensureExtractionIdentity(card);
    arrangeExtractionControls(panel, card);
  }

  function apply() {
    const view = root();
    if (!view) return;
    applyDownloads(view);
    applyExtraction(view);
    expandConfiguredSecretMasks(view);
  }
  let scheduled = false;

  function scheduleApply() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => {
      scheduled = false;
      apply();
    });
  }

  ensureDirectoryBrowserStyles();
  document.addEventListener('debridpulse:settings-rendered', scheduleApply);
})();
