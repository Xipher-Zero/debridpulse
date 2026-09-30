/* DebridPulse Backups manager: the ONE backup-manager state owner.
 *
 * It owns the manager dialog's lifetime, the backup inventory it shows, the
 * single selected backup, action enablement, and the Add / Save / Restore /
 * Remove flows -- each of which refreshes the inventory through this owner's
 * one `load()` and nothing else. The Settings page only opens it (after
 * settling its own pending writes); Run Backup stays on the card.
 *
 * A backup is one DebridPulse restore point. Its member files are the backend
 * owner's (services.backup) business and never appear here. Add and Save move
 * that one portable unit between DebridPulse and the operator's filesystem;
 * the words are filesystem words, whatever wraps the page.
 *
 * Selection is the single-checkmark idiom: every row carries a checkbox, at
 * most one is checked, and the contextual Remove action appears on that row
 * alone. The shared dialog shell (DPSettingsModal) owns focus, Escape, the
 * upper-right close control and settlement.
 */
(function () {
  'use strict';

  const INVENTORY = '/admin/backups';
  const PACKAGE_TYPE = {description: 'DebridPulse backup', accept: {'application/zip': ['.zip']}};

  const state = {
    dialog: null,
    inventory: [],
    selected: '',
    busy: false,
    body: null,
  };

  function request(method, path, body, timeout) {
    if (typeof api !== 'function') throw new Error('Application API client is unavailable');
    return api(method, path, body, timeout);
  }

  function notify(message, kind = 'info') {
    if (typeof toast === 'function') toast(String(message), kind);
  }

  function html(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => (
      {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
  }

  function timezone() {
    try {
      return String((typeof settingsData !== 'undefined' && settingsData && settingsData.timezone) || '').trim() || undefined;
    } catch (_) {
      return undefined;
    }
  }

  /* "Sep 28, 2026 · 10:35 PM" in the operator's configured time zone. */
  function when(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso || '');
    const timeZone = timezone();
    const day = date.toLocaleDateString('en-US', {month: 'short', day: 'numeric', year: 'numeric', timeZone});
    const time = date.toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit', timeZone});
    return `${day} · ${time}`;
  }

  function size(bytes) {
    return typeof fmtSize === 'function' ? fmtSize(Number(bytes) || 0) : `${Number(bytes) || 0} B`;
  }

  function selectedBackup() {
    return state.inventory.find(item => item.id === state.selected) || null;
  }

  // ── rendering ────────────────────────────────────────────────────────────

  function render() {
    const body = state.body;
    if (!body) return;
    const selected = selectedBackup();
    if (!selected) state.selected = '';
    const rows = state.inventory.map(item => {
      const checked = item.id === state.selected;
      return `
        <tr class="dp-backup-row${checked ? ' is-selected' : ''}" data-backup-id="${html(item.id)}">
          <td class="dp-backup-select">
            <input type="checkbox" class="dp-backup-check" data-backup-select="${html(item.id)}"
                   aria-label="Select backup ${html(when(item.created_at))}" ${checked ? 'checked' : ''}>
          </td>
          <td class="dp-backup-when">${html(when(item.created_at))}</td>
          <td class="dp-backup-contents">${html(item.contents)}</td>
          <td class="dp-backup-size">${html(size(item.size_bytes))}</td>
          <td class="dp-backup-row-action">${checked
            ? `<button class="btn btn-danger btn-sm" type="button" data-backup-remove="${html(item.id)}">Remove</button>`
            : ''}</td>
        </tr>`;
    }).join('');
    body.innerHTML = `
      <p class="dp-modal-message dp-backup-instruction">Select one backup to save, restore, or remove.</p>
      <div class="dp-backup-table-wrap">
        ${state.inventory.length ? `
          <table class="dp-backup-table" aria-label="Backups">
            <thead><tr>
              <th scope="col" class="dp-backup-select" aria-label="Selected"></th>
              <th scope="col">Backup</th>
              <th scope="col">Contents</th>
              <th scope="col">Size</th>
              <th scope="col" class="dp-backup-row-action" aria-label="Actions"></th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>` : '<div class="form-hint dp-backup-empty">No backups yet.</div>'}
      </div>
      <div class="dp-backup-selection" data-backup-selection aria-live="polite">${selected ? `
        <div>Selected: ${html(when(selected.created_at))}</div>
        <div class="dp-backup-id">Backup ID: ${html(selected.id)}</div>` : ''}</div>`;
    const dialog = state.dialog;
    if (dialog) {
      dialog.setActionEnabled('add', !state.busy);
      dialog.setActionEnabled('save', !state.busy && !!selected);
      dialog.setActionEnabled('restore', !state.busy && !!selected);
    }
    body.querySelectorAll('[data-backup-remove]').forEach(button => { button.disabled = state.busy; });
    body.querySelectorAll('[data-backup-select]').forEach(box => { box.disabled = state.busy; });
  }

  function select(id) {
    state.selected = state.selected === id ? '' : id;
    render();
    const box = state.body?.querySelector(`[data-backup-select="${CSS.escape(id)}"]`);
    if (box) box.focus();
  }

  async function load(selectId) {
    const result = await request('GET', INVENTORY, undefined, 15000);
    state.inventory = Array.isArray(result.backups) ? result.backups : [];
    if (selectId !== undefined) state.selected = selectId;
    render();
  }

  async function run(work) {
    if (state.busy) return;
    state.busy = true;
    state.dialog?.setBusy(true);
    render();
    try {
      await work();
    } finally {
      state.busy = false;
      state.dialog?.setBusy(false);
      render();
    }
  }

  // ── Add Backup ───────────────────────────────────────────────────────────

  function chooseFile() {
    return new Promise(resolve => {
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = '.zip,application/zip';
      input.hidden = true;
      input.addEventListener('change', () => {
        resolve(input.files && input.files[0] ? input.files[0] : null);
        input.remove();
      }, {once: true});
      input.addEventListener('cancel', () => { resolve(null); input.remove(); }, {once: true});
      document.body.appendChild(input);
      input.click();
    });
  }

  async function addBackup() {
    const file = await chooseFile();
    if (!file) return;
    await run(async () => {
      try {
        // The file itself is the request body, so the backend can refuse it
        // while it streams rather than after it has all arrived.
        const result = await request('POST', INVENTORY, file, 600000);
        await load(result.backup && result.backup.id);
        notify('Backup added', 'success');
      } catch (error) {
        notify(error.message || 'Backup could not be added.', 'error');
      }
    });
  }

  // ── Save Backup ──────────────────────────────────────────────────────────

  function packagePath(id) {
    return `${INVENTORY}/${encodeURIComponent(id)}/package`;
  }

  function packageName(id) {
    return `debridpulse-backup-${id}.zip`;
  }

  async function saveBackup() {
    const selected = selectedBackup();
    if (!selected) return;
    const id = selected.id;
    if (typeof window.showSaveFilePicker === 'function') {
      // The native save dialog: the operator chooses the location. It must be
      // asked for while the click is still the active user gesture.
      let handle;
      try {
        handle = await window.showSaveFilePicker({suggestedName: packageName(id), types: [PACKAGE_TYPE]});
      } catch (error) {
        if (error && error.name === 'AbortError') return;
        handle = null;
      }
      if (handle) {
        await run(async () => {
          try {
            const response = await window.debridPulseAuth.fetch(API + packagePath(id));
            if (!response.ok || !response.body) throw new Error('save');
            const writable = await handle.createWritable();
            await response.body.pipeTo(writable);
            notify('Backup saved', 'success');
          } catch (_) {
            notify('Backup could not be saved.', 'error');
          }
        });
        return;
      }
    }
    // Otherwise the browser's own save flow names the location.
    const link = document.createElement('a');
    link.href = API + packagePath(id);
    link.download = packageName(id);
    link.hidden = true;
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  // ── Restore Backup ───────────────────────────────────────────────────────

  function confirmRestore(item) {
    const dialog = window.DPSettingsModal.open({
      role: 'alertdialog',
      tone: 'success',
      title: 'Restore Backup?',
      acceptLabel: 'Restore Backup',
      cancelLabel: 'Cancel',
      closeControl: true,
      className: 'dp-backup-restore-dialog',
      mount(body) {
        const content = document.createElement('div');
        content.className = 'dp-backup-restore-copy';
        content.innerHTML = `
          <div><div class="form-label">Restore:</div><div class="dp-backup-restore-target">${html(when(item.created_at))}</div></div>
          <p class="dp-modal-message">This will replace the current DebridPulse database and configuration with the selected backup.</p>
          <div>
            <p class="dp-modal-message">Before restore begins:</p>
            <ul class="dp-modal-message dp-backup-restore-steps">
              <li>Processing will be paused</li>
              <li>A safety backup of the current state will be created automatically</li>
              <li>The selected backup will be validated again</li>
            </ul>
          </div>
          <p class="dp-modal-message">If validation fails, the current installation will remain unchanged.</p>`;
        body.appendChild(content);
        return content;
      },
    });
    return dialog.closed.then(result => result.accepted);
  }

  async function restoreBackup() {
    const selected = selectedBackup();
    if (!selected || !(await confirmRestore(selected))) return;
    await run(async () => {
      try {
        await request('POST', `${INVENTORY}/restore`, {id: selected.id}, 900000);
      } catch (error) {
        notify(error.message || 'Backup could not be restored. The current DebridPulse state was left unchanged.', 'error');
        try { await load(state.selected); } catch (_) {}
        return;
      }
      notify('Backup restored. Reloading DebridPulse…', 'success');
      state.dialog?.close();
      // Everything the page holds belongs to the replaced state.
      window.setTimeout(() => window.location.reload(), 1200);
    });
  }

  // ── Remove ───────────────────────────────────────────────────────────────

  async function removeBackup(id) {
    const item = state.inventory.find(entry => entry.id === id);
    if (!item) return;
    const confirmed = await window.DPSettingsModal.confirm({
      title: 'Remove Backup?',
      message: `${when(item.created_at)} will be permanently removed from DebridPulse backup storage.`,
      confirmLabel: 'Remove Backup',
      tone: 'danger',
    });
    if (!confirmed) return;
    await run(async () => {
      try {
        await request('DELETE', `${INVENTORY}/${encodeURIComponent(id)}`, undefined, 30000);
        notify('Backup removed', 'success');
      } catch (error) {
        notify(error.message || 'Backup could not be removed.', 'error');
      }
      try { await load(''); } catch (error) { notify(error.message, 'error'); }
    });
  }

  // ── lifetime ─────────────────────────────────────────────────────────────

  function onAction(id) {
    if (state.busy) return;
    if (id === 'add') void addBackup();
    else if (id === 'save') void saveBackup();
    else if (id === 'restore') void restoreBackup();
  }

  async function open() {
    if (state.dialog && state.dialog.isOpen) return;
    let inventory;
    try {
      const result = await request('GET', INVENTORY, undefined, 15000);
      inventory = Array.isArray(result.backups) ? result.backups : [];
    } catch (error) {
      notify(error.message, 'error');
      return;
    }
    state.inventory = inventory;
    state.selected = '';
    state.busy = false;
    const dialog = window.DPSettingsModal.open({
      title: 'Backups',
      dismiss: true,
      closeControl: true,
      className: 'dp-backup-manager-dialog',
      bodyClassName: 'dp-backup-manager-body',
      actions: [
        {id: 'add', label: 'Add Backup'},
        {id: 'save', label: 'Save Backup', disabled: true},
        {id: 'restore', label: 'Restore Backup', tone: 'success', disabled: true},
      ],
      onAction,
      mount(body) {
        state.body = body;
        body.addEventListener('change', event => {
          const box = event.target.closest('[data-backup-select]');
          if (box && !state.busy) select(box.dataset.backupSelect);
        });
        body.addEventListener('click', event => {
          if (state.busy) return;
          const remove = event.target.closest('[data-backup-remove]');
          if (remove) {
            void removeBackup(remove.dataset.backupRemove);
            return;
          }
          // The whole row selects, exactly as its checkbox does.
          const row = event.target.closest('[data-backup-id]');
          if (row && !event.target.closest('input, button')) select(row.dataset.backupId);
        });
        return null;
      },
    });
    state.dialog = dialog;
    render();
    dialog.closed.then(() => {
      if (state.dialog === dialog) {
        state.dialog = null;
        state.body = null;
        state.selected = '';
      }
    });
  }

  window.DPBackupManager = Object.freeze({open});
})();
