/* Settings directory picker: the modal that browses server directories for the
 * Download Folder and Backup Folder fields.
 *
 * Owns exactly that interaction. The fields and their Browse buttons are part of
 * the Settings markup emitted by ui-settings-page.js, which calls
 * DPSettingsDirectoryPicker.open(purpose) from the button; this module reads the
 * field's current value and writes the chosen one back, and touches no other
 * Settings markup. Backup purpose is directory navigation only (see backend
 * GET /settings/directories?purpose=backup); it never calls Download Storage
 * validation, so its selectable wording does not reuse "Selectable as Download
 * Storage".
 */
(function () {
  'use strict';

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

  const DOWNLOAD_DIRECTORY_PICKER = Object.freeze({
    purpose: 'download',
    fieldSelector: '[data-setting="download_folder"]',
    browseAction: 'browse-download-folder',
    browseAriaLabel: 'Browse server directories for Built-in Download Folder',
    dialogTitle: 'Choose Download Folder',
    selectableLabel: 'Selectable as Download Storage',
    fallbackNoticeText: 'The current Download Folder cannot be browsed. Showing the server fallback location instead; the Settings field has not been changed.',
  });

  const BACKUP_DIRECTORY_PICKER = Object.freeze({
    purpose: 'backup',
    fieldSelector: '[data-setting="backup_folder"]',
    browseAction: 'browse-backup-folder',
    browseAriaLabel: 'Browse server directories for Backup Folder',
    dialogTitle: 'Choose Backup Folder',
    selectableLabel: 'Selectable as Backup Folder',
    fallbackNoticeText: 'The current Backup Folder cannot be browsed. Showing the server fallback location instead; the Settings field has not been changed.',
  });

  const PICKERS = Object.freeze({download: DOWNLOAD_DIRECTORY_PICKER, backup: BACKUP_DIRECTORY_PICKER});
  const root = () => document.getElementById('view-settings');

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

  function openDirectoryPicker(config, _origin) {
    const field = root()?.querySelector(config.fieldSelector);
    const modalApi = window.DPSettingsModal;
    if (!field || typeof api !== 'function' || !modalApi || typeof modalApi.confirm !== 'function') return;

    const originalValue = String(field.value ?? '');
    let currentResponse = null;
    let generation = 0;
    let controller = null;

    const confirmation = modalApi.confirm({
      title: config.dialogTitle,
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
        ? config.selectableLabel
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

      const queryParams = {purpose: config.purpose};
      if (path != null) queryParams.path = String(path);
      const query = `?${new URLSearchParams(queryParams).toString()}`;
      try {
        const payload = await api('GET', `/settings/directories${query}`, undefined, 10000, {signal: controller.signal});
        if (requestGeneration !== generation || !overlay.isConnected) return;
        render(payload);
        setLoading(false);
      } catch (error) {
        if (requestGeneration !== generation || error?.name === 'AbortError' || !overlay.isConnected) return;
        if (fallbackOnFailure) {
          notice.hidden = false;
          notice.textContent = config.fallbackNoticeText;
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

  window.DPSettingsDirectoryPicker = Object.freeze({
    open(purpose) {
      const config = PICKERS[purpose];
      if (config) openDirectoryPicker(config);
    },
  });
})();
