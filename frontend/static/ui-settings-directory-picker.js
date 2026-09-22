/* Settings directory picker: the modal that browses server directories for the
 * Download Folder and Backup Folder fields.
 *
 * Owns exactly that interaction, as a direct client of the canonical dialog owner
 * (ui-settings-modal.js): it opens its own directory-browser dialog through that
 * owner's API and never touches the shell DOM. The fields and their Browse buttons are part of
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
    browseAriaLabel: 'Browse server directories for Download Folder',
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

  function openDirectoryPicker(config) {
    const field = root()?.querySelector(config.fieldSelector);
    if (!field || typeof api !== 'function') return;

    const originalValue = String(field.value ?? '');
    let currentResponse = null;
    let generation = 0;
    let controller = null;
    let view = null;

    const dialog = window.DPSettingsModal.open({
      title: config.dialogTitle,
      acceptLabel: 'Use This Folder',
      acceptDisabled: true,
      className: 'dp-settings-directory-dialog',
      bodyClassName: 'dp-settings-directory-body',
      mount(body) {
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
        view = {
          notice: body.querySelector('[data-directory-notice]'),
          currentPath: body.querySelector('[data-directory-current-path]'),
          currentState: body.querySelector('[data-directory-current-state]'),
          capacity: body.querySelector('[data-directory-capacity]'),
          up: body.querySelector('[data-directory-up]'),
          loading: body.querySelector('[data-directory-loading]'),
          errorBox: body.querySelector('[data-directory-error]'),
          list: body.querySelector('[data-directory-list]'),
        };
        view.up.addEventListener('click', () => {
          if (currentResponse?.parent == null) return;
          void loadDirectory(currentResponse.parent);
        });
      },
    });

    const setLoading = busy => {
      dialog.setBusy(busy);
      view.loading.textContent = busy ? 'Loading…' : '';
      dialog.setAcceptEnabled(!busy && currentResponse?.current?.selectable === true);
      view.up.disabled = busy || currentResponse?.parent == null;
      view.list.querySelectorAll('[data-directory-row]').forEach(row => {
        row.disabled = busy || row.dataset.accessible !== 'true';
      });
    };

    const render = payload => {
      const current = payload?.current || null;
      currentResponse = payload && current ? payload : null;
      view.errorBox.hidden = true;
      view.errorBox.textContent = '';
      view.list.replaceChildren();

      if (!current) {
        view.currentPath.textContent = '—';
        view.currentState.textContent = 'Not validated';
        view.currentState.dataset.selectable = 'false';
        view.capacity.textContent = 'Capacity unavailable';
        dialog.setAcceptEnabled(false);
        view.up.disabled = true;
        return;
      }

      view.currentPath.textContent = String(current.path ?? '');
      view.currentPath.title = String(current.path ?? '');
      const selectable = current.selectable === true;
      view.currentState.dataset.selectable = selectable ? 'true' : 'false';
      view.currentState.textContent = selectable
        ? config.selectableLabel
        : `Not selectable — ${directoryReasonLabel(current.reason)}`;

      const total = directorySize(current.capacity?.total_bytes);
      const free = directorySize(current.capacity?.free_bytes);
      view.capacity.textContent = total !== null && free !== null
        ? `${free} free of ${total}`
        : 'Capacity unavailable';

      view.up.disabled = payload.parent == null;
      const children = Array.isArray(payload.children) ? payload.children : [];
      if (!children.length) {
        const empty = document.createElement('div');
        empty.className = 'dp-settings-directory-empty';
        empty.textContent = 'No child directories.';
        view.list.appendChild(empty);
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
          view.list.appendChild(row);
        });
      }

      dialog.setAcceptEnabled(selectable);
    };

    const loadDirectory = async (path, {fallbackOnFailure = false} = {}) => {
      const requestGeneration = ++generation;
      if (controller) controller.abort();
      controller = new AbortController();
      view.errorBox.hidden = true;
      view.errorBox.textContent = '';
      setLoading(true);

      const queryParams = {purpose: config.purpose};
      if (path != null) queryParams.path = String(path);
      const query = `?${new URLSearchParams(queryParams).toString()}`;
      try {
        const payload = await api('GET', `/settings/directories${query}`, undefined, 10000, {signal: controller.signal});
        if (requestGeneration !== generation || !dialog.isOpen) return;
        render(payload);
        setLoading(false);
      } catch (error) {
        if (requestGeneration !== generation || error?.name === 'AbortError' || !dialog.isOpen) return;
        if (fallbackOnFailure) {
          view.notice.hidden = false;
          view.notice.textContent = config.fallbackNoticeText;
          void loadDirectory(null);
          return;
        }
        view.errorBox.textContent = directoryErrorMessage(error);
        view.errorBox.hidden = false;
        setLoading(false);
      }
    };

    void dialog.closed.then(({accepted}) => {
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
