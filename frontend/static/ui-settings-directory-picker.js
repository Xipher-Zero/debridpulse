/* Settings directory picker: the modal that browses server directories for the
 * Download Folder and Backup Folder fields.
 *
 * Owns exactly that interaction, as a direct client of the canonical dialog owner
 * (ui-settings-modal.js): it opens its own directory-browser dialog through that
 * owner's API and never touches the shell DOM. The fields and their Browse buttons are part of
 * the Settings markup emitted by ui-settings-page.js, which calls
 * DPSettingsDirectoryPicker.open(purpose) from the button; this module reads the
 * field's current value and writes the chosen one back, and touches no other
 * Settings markup; a caller may name its own field instead
 * (DPSettingsDirectoryPicker.open(purpose, {field})), whose value it sets and
 * whose commit stays that caller's. Backup purpose is directory navigation only (see backend
 * GET /settings/directories?purpose=backup); it never calls Download Storage
 * validation, so its selectable wording does not reuse "Selectable as Download
 * Storage".
 */
(function () {
  'use strict';

  // Words are the catalog's (directory_picker.*), by the server's codes; an
  // unknown code is shown as its own words.
  const t = (key, values) => window.DPI18n.t(key, values);
  const REASONS = new Set(['none', 'low_space', 'capacity_exhausted', 'quota_exhausted', 'read_only', 'missing', 'invalid_path',
    'inaccessible', 'stat_failed', 'io_error', 'sqlite_io_error', 'sqlite_open_failed']);
  const ERRORS = new Set(['invalid_path', 'relative_path', 'not_directory', 'symlink_loop', 'path_inaccessible', 'path_unavailable',
    'browser_unavailable']);

  const DOWNLOAD_DIRECTORY_PICKER = Object.freeze({
    purpose: 'download',
    fieldSelector: '[data-setting="download_folder"]',
    browseAction: 'browse-download-folder',
  });

  const BACKUP_DIRECTORY_PICKER = Object.freeze({
    purpose: 'backup',
    fieldSelector: '[data-setting="backup_folder"]',
    browseAction: 'browse-backup-folder',
  });

  const PICKERS = Object.freeze({download: DOWNLOAD_DIRECTORY_PICKER, backup: BACKUP_DIRECTORY_PICKER});
  const root = () => document.getElementById('view-settings');

  function directoryReasonLabel(reason) {
    const key = String(reason || '').trim();
    if (REASONS.has(key)) return t(`directory_picker.reason.${key}`);
    return key ? key.replaceAll('_', ' ') : t('directory_picker.reason.missing');
  }

  function directoryErrorMessage(error) {
    const detail = error?.detail && typeof error.detail === 'object' ? error.detail : null;
    const code = String(error?.code || detail?.code || '').trim();
    if (ERRORS.has(code)) return t(`directory_picker.error.${code}`);
    if (typeof detail?.message === 'string' && detail.message.trim()) return detail.message.trim();
    const message = String(error?.message || '').trim();
    if (message && message !== '[object Object]' && message.length <= 240) return message;
    return t('directory_picker.error.default');
  }

  function directorySize(value) {
    if (value == null) return null;
    const number = Number(value);
    if (!Number.isFinite(number) || number < 0) return null;
    if (number === 0) return t('format.size.b', {value: window.DPI18n.number(0)});
    if (typeof fmtSize === 'function') return fmtSize(number);
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let unit = 0;
    let scaled = number;
    while (scaled >= 1024 && unit < units.length - 1) {
      scaled /= 1024;
      unit += 1;
    }
    return t(`format.size.${units[unit].toLowerCase()}`, {value: window.DPI18n.number(scaled, 1)});
  }

  function openDirectoryPicker(config, own = null) {
    // The field it chooses for: the Settings field, or one its caller owns
    // (the setup wizard's Download Directory), which it commits nothing for.
    const field = own instanceof HTMLInputElement ? own : root()?.querySelector(config.fieldSelector);
    if (!field || typeof api !== 'function') return;

    const originalValue = String(field.value ?? '');
    let currentResponse = null;
    let generation = 0;
    let controller = null;
    let view = null;

    const words = key => t(`directory_picker.${config.purpose}.${key}`);
    let lastPayload = null;   // said again on a language change
    let errorWords = null;    // {key} or {text}: the error shown, if any
    let noticeShown = false;
    const dialog = window.DPSettingsModal.open({
      title: words('title'),
      acceptLabel: t('directory_picker.accept'),
      keys: {title: `directory_picker.${config.purpose}.title`, accept: 'directory_picker.accept', cancel: 'common.cancel'},
      acceptDisabled: true,
      className: 'dp-settings-directory-dialog',
      bodyClassName: 'dp-settings-directory-body',
      mount(body) {
        body.innerHTML = `
          <div class="dp-settings-directory-browser">
            <div class="dp-settings-directory-notice" data-directory-notice hidden></div>
            <div class="dp-settings-directory-current">
              <span class="dp-settings-directory-current-label" data-i18n="directory_picker.current">${esc(t('directory_picker.current'))}</span>
              <code class="dp-settings-directory-current-path" data-directory-current-path>—</code>
              <div class="dp-settings-directory-current-meta">
                <span class="dp-settings-directory-current-state" data-directory-current-state data-selectable="false">${esc(t('directory_picker.not_validated'))}</span>
                <span data-directory-capacity>${esc(t('directory_picker.reason.stat_failed'))}</span>
              </div>
            </div>
            <div class="dp-settings-directory-toolbar">
              <button class="btn btn-ghost btn-sm" type="button" data-directory-up disabled data-i18n="directory_picker.up">${esc(t('directory_picker.up'))}</button>
              <span class="dp-settings-directory-loading" data-directory-loading role="status" aria-live="polite"></span>
            </div>
            <div class="dp-settings-directory-error" data-directory-error role="alert" hidden></div>
            <div class="dp-settings-directory-list" data-directory-list aria-label="${esc(t('directory_picker.list_label'))}" data-i18n-attr="aria-label:directory_picker.list_label"></div>
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

    let loadingNow = false;
    const setLoading = busy => {
      loadingNow = busy;
      dialog.setBusy(busy);
      view.loading.textContent = busy ? t('common.loading') : '';
      dialog.setAcceptEnabled(!busy && currentResponse?.current?.selectable === true);
      view.up.disabled = busy || currentResponse?.parent == null;
      view.list.querySelectorAll('[data-directory-row]').forEach(row => {
        row.disabled = busy || row.dataset.accessible !== 'true';
      });
    };

    // The one error presentation: whatever ``errorWords`` holds, in the page's language.
    const paintError = () => {
      view.errorBox.textContent = errorWords ? directoryErrorMessage(errorWords.error) : '';
      view.errorBox.hidden = !errorWords;
    };

    // The listing only (current folder, its state and capacity, its folders);
    // it never touches the error, which is the operation's state, not the listing's.
    const paintListing = payload => {
      const current = payload?.current || null;
      lastPayload = payload;
      currentResponse = payload && current ? payload : null;
      view.list.replaceChildren();

      if (!current) {
        view.currentPath.textContent = '—';
        view.currentState.textContent = t('directory_picker.not_validated');
        view.currentState.dataset.selectable = 'false';
        view.capacity.textContent = t('directory_picker.reason.stat_failed');
        dialog.setAcceptEnabled(false);
        view.up.disabled = true;
        return;
      }

      view.currentPath.textContent = String(current.path ?? '');
      view.currentPath.dataset.tooltip = String(current.path ?? '');
      const selectable = current.selectable === true;
      view.currentState.dataset.selectable = selectable ? 'true' : 'false';
      view.currentState.textContent = selectable
        ? words('selectable')
        : t('directory_picker.not_selectable', {reason: directoryReasonLabel(current.reason)});

      const total = directorySize(current.capacity?.total_bytes);
      const free = directorySize(current.capacity?.free_bytes);
      view.capacity.textContent = total !== null && free !== null
        ? t('storage.capacity.free_of', {free, total})
        : t('directory_picker.reason.stat_failed');

      view.up.disabled = payload.parent == null;
      const children = Array.isArray(payload.children) ? payload.children : [];
      if (!children.length) {
        const empty = document.createElement('div');
        empty.className = 'dp-settings-directory-empty';
        empty.textContent = t('directory_picker.empty');
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
          row.dataset.tooltip = String(child?.path ?? '');

          const name = document.createElement('span');
          name.className = 'dp-settings-directory-name';
          name.dataset.directoryName = '1';
          name.textContent = String(child?.name ?? child?.path ?? t('directory_picker.directory_fallback'));

          const hint = document.createElement('span');
          hint.className = 'dp-settings-directory-row-hint';
          hint.textContent = child?.accessible === true
            ? t('directory_picker.open_to_validate')
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

    // A new answer: its listing, and no error.
    const render = payload => {
      errorWords = null;
      paintListing(payload);
      paintError();
    };

    const loadDirectory = async (path, {fallbackOnFailure = false} = {}) => {
      const requestGeneration = ++generation;
      if (controller) controller.abort();
      controller = new AbortController();
      errorWords = null;
      paintError();
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
          noticeShown = true;
          view.notice.textContent = words('fallback_notice');
          void loadDirectory(null);
          return;
        }
        errorWords = {error};
        paintError();
        setLoading(false);
      }
    };

    // A language change while it is open: everything it shows, said again from
    // what it already holds (no request); a focused folder keeps focus by its path.
    const relabel = () => {
      if (!dialog.isOpen || !view) return;
      const focusedPath = document.activeElement?.matches?.('[data-directory-row]') ? document.activeElement.dataset.path : null;
      const upFocused = document.activeElement === view.up;
      if (lastPayload) paintListing(lastPayload);   // the error shown stays shown
      paintError();
      if (noticeShown) view.notice.textContent = words('fallback_notice');
      setLoading(loadingNow);           // the load in flight, if any, stays in flight
      if (focusedPath != null) Array.from(view.list.querySelectorAll('[data-directory-row]')).find(row => row.dataset.path === focusedPath)?.focus({preventScroll: true});
      else if (upFocused) view.up.focus({preventScroll: true});
    };
    document.addEventListener('debridpulse:locale-changed', relabel);

    void dialog.closed.then(({accepted}) => {
      document.removeEventListener('debridpulse:locale-changed', relabel);
      generation += 1;
      if (controller) controller.abort();
      controller = null;
      if (!accepted || currentResponse?.current?.selectable !== true) return;
      const liveField = own ? (field.isConnected ? field : null) : document.getElementById(field.id);
      if (!liveField) return;
      const canonicalPath = String(currentResponse.current.path ?? '');
      liveField.value = canonicalPath;
      liveField.dispatchEvent(new Event('input', {bubbles: true}));
      liveField.dispatchEvent(new Event('change', {bubbles: true}));
      /* Browse is a non-destructive VALUE-SELECTION action: it chooses what the
       * field holds and then commits it exactly as the operator typing it and
       * leaving would, through the one canonical field owner. It is not a
       * second save path -- it names no endpoint, carries no payload and owns
       * no rollback -- and it never waits for a deferred Apply. A field this
       * page has not migrated declares no commit key and keeps its existing
       * semantics untouched; a cancelled dialog returned above and mutates
       * nothing at all. */
      if (liveField.dataset?.commitKey) window.DPSettingsPersistence?.commit(liveField);
    });

    if (originalValue.length) void loadDirectory(originalValue, {fallbackOnFailure: true});
    else void loadDirectory(null);
  }

  window.DPSettingsDirectoryPicker = Object.freeze({
    open(purpose, {field = null} = {}) {
      const config = PICKERS[purpose];
      if (config) openDirectoryPicker(config, field);
    },
  });
})();
