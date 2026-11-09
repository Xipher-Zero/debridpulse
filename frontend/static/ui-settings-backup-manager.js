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
 *
 * A confirmed restore is shown by the shell's non-dismissible progress shape
 * until the backend answers. A refusal returns to the manager; success -- the
 * backend's answer, sent only once the restored state has started -- leaves
 * a one-shot marker in this browser session and reloads, and the reloaded
 * page consumes the marker exactly once to say so.
 */
(function () {
  'use strict';

  const INVENTORY = '/admin/backups';
  // The native save dialog's file type: its description is said when the dialog is asked for.
  const packageType = () => ({description: t('backups.package_type'), accept: {'application/zip': ['.zip']}});
  // Presentation only: "the restore this page just asked for succeeded".
  const RESTORED_MARKER = 'dp.backupRestored';

  const state = {
    dialog: null,
    inventory: [],
    selected: '',
    busy: false,
    body: null,
  };

  // The catalog's words (ui-i18n.js). ``say`` is said words that keep their key
  // (DPSettingsModal names its node with it, so an open dialog is re-said);
  // ``keyed`` is the same key as markup attributes, for DPI18n.render().
  const t = (key, values) => window.DPI18n.t(key, values);
  const say = (key, values) => ({key, values: values || null, toString: () => t(key, values)});
  function keyed(key, values, attrs = {}) {
    const parts = key ? [`data-i18n="${html(key)}"`] : [];
    const pairs = Object.entries(attrs).map(([name, attrKey]) => `${name}:${attrKey}`);
    Object.entries(attrs).forEach(([name, attrKey]) => parts.push(`${name}="${html(t(attrKey, values))}"`));
    if (pairs.length) parts.push(`data-i18n-attr="${html(pairs.join(';'))}"`);
    if (values) parts.push(`data-i18n-values="${html(JSON.stringify(values))}"`);
    return ` ${parts.join(' ')}`;
  }
  // An element's words, keyed.
  const words = (tag, key, values, extra = '') => `<${tag}${extra}${keyed(key, values)}>${html(t(key, values))}</${tag}>`;
  // Dates in the shown language (a pseudo locale formats as English).
  const dateLocale = () => window.DPI18n?.intlLocale?.() || 'en-US';

  function request(method, path, body, timeout) {
    if (typeof api !== 'function') throw new Error(t('backups.api_unavailable'));
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
    const day = date.toLocaleDateString(dateLocale(), {month: 'short', day: 'numeric', year: 'numeric', timeZone});
    const time = date.toLocaleTimeString(dateLocale(), {hour: 'numeric', minute: '2-digit', timeZone});
    return `${day} · ${time}`;
  }

  /* The same moment shortened for a narrow inventory -- "9/28/26" and
   * "10:35 PM", in the same configured time zone -- as two parts that may
   * stack. */
  function whenShort(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return [String(iso || '')];
    const timeZone = timezone();
    return [date.toLocaleDateString(dateLocale(), {month: 'numeric', day: 'numeric', year: '2-digit', timeZone}),
      date.toLocaleTimeString(dateLocale(), {hour: 'numeric', minute: '2-digit', timeZone})];
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
    const rows = state.inventory.map((item, index) => {
      const checked = item.id === state.selected;
      const full = when(item.created_at);
      return `
        <tr class="dp-backup-row${checked ? ' is-selected' : ''}" data-backup-id="${html(item.id)}">
          <td class="dp-backup-select">
            <input type="checkbox" class="dp-backup-check" data-backup-select="${html(item.id)}"
                   ${keyed('', {when: full}, {'aria-label': 'backups.select'})} aria-describedby="dp-backup-contents-${index}"
                   ${checked ? 'checked' : ''}>
          </td>
          <td class="dp-backup-when"><span class="dp-backup-when-full">${html(full)}</span><span class="dp-backup-when-short"
              aria-hidden="true">${whenShort(item.created_at).map(part => `<span>${html(part)}</span>`).join(' ')}</span></td>
          <td class="dp-backup-contents" id="dp-backup-contents-${index}">${html(item.contents)}</td>
          <td class="dp-backup-size">${html(size(item.size_bytes))}</td>
          <td class="dp-backup-row-action">${checked
            ? words('button', 'backups.remove', null, ` class="btn btn-danger btn-sm" type="button" data-backup-remove="${html(item.id)}"`)
            : ''}</td>
        </tr>`;
    }).join('');
    body.innerHTML = `
      ${words('p', 'backups.instruction', null, ' class="dp-modal-message dp-backup-instruction"')}
      <div class="dp-backup-table-wrap">
        ${state.inventory.length ? `
          <table class="dp-backup-table"${keyed('', null, {'aria-label': 'backups.title'})}>
            <thead><tr>
              <th scope="col" class="dp-backup-select"${keyed('', null, {'aria-label': 'backups.column.selected'})}></th>
              ${words('th', 'backups.column.backup', null, ' scope="col"')}
              ${words('th', 'backups.column.contents', null, ' scope="col" class="dp-backup-contents"')}
              ${words('th', 'backups.column.size', null, ' scope="col"')}
              <th scope="col" class="dp-backup-row-action"${keyed('', null, {'aria-label': 'backups.column.actions'})}></th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>` : words('div', 'backups.empty', null, ' class="form-hint dp-backup-empty"')}
      </div>
      <div class="dp-backup-selection" data-backup-selection aria-live="polite">${selected ? `
        ${words('div', 'backups.selected', {when: when(selected.created_at)})}
        ${words('div', 'backups.selected_contents', {contents: String(selected.contents ?? '')}, ' class="dp-backup-selected-contents"')}
        ${words('div', 'backups.selected_id', {id: String(selected.id)}, ' class="dp-backup-id"')}` : ''}</div>`;
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
        notify(t('backups.added'), 'success');
      } catch (error) {
        notify(error.message || t('backups.add_failed'), 'error');
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
        handle = await window.showSaveFilePicker({suggestedName: packageName(id), types: [packageType()]});
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
            notify(t('backups.saved'), 'success');
          } catch (_) {
            notify(t('backups.save_failed'), 'error');
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
      title: say('backups.restore_title'),
      acceptLabel: say('backups.restore'),
      cancelLabel: say('common.cancel'),
      closeControl: true,
      className: 'dp-backup-restore-dialog',
      mount(body) {
        const content = document.createElement('div');
        content.className = 'dp-backup-restore-copy';
        content.innerHTML = `
          <div>${words('div', 'backups.restore_label', null, ' class="form-label"')}<div class="dp-backup-restore-target" data-backup-when="${html(item.created_at)}">${html(when(item.created_at))}</div></div>
          ${words('p', 'backups.restore_replaces', null, ' class="dp-modal-message"')}
          <div>
            ${words('p', 'backups.restore_before', null, ' class="dp-modal-message"')}
            <ul class="dp-modal-message dp-backup-restore-steps">
              ${[1, 2, 3].map(step => words('li', `backups.restore_step_${step}`)).join('')}
            </ul>
          </div>
          ${words('p', 'backups.restore_unchanged', null, ' class="dp-modal-message"')}`;
        body.appendChild(content);
        return content;
      },
    });
    return dialog.closed.then(result => result.accepted);
  }

  function markRestored() {
    try { window.sessionStorage.setItem(RESTORED_MARKER, '1'); } catch (_) {}
  }

  /* Consumed on the first page load after a successful restore, then gone. */
  function announceRestored() {
    let restored = false;
    try {
      restored = window.sessionStorage.getItem(RESTORED_MARKER) === '1';
      window.sessionStorage.removeItem(RESTORED_MARKER);
    } catch (_) {}
    if (restored) notify(t('backups.restored'), 'success');
  }

  async function restoreBackup() {
    const selected = selectedBackup();
    if (!selected || !(await confirmRestore(selected))) return;
    const progress = window.DPSettingsModal.progress({
      title: say('backups.restoring'),
      lines: [say('backups.restoring_paused'), say('backups.restoring_restart')],
      className: 'dp-backup-restore-progress',
    });
    let failure = null;
    await run(async () => {
      try {
        await request('POST', `${INVENTORY}/restore`, {id: selected.id}, 900000);
      } catch (error) {
        failure = error;
        try { await load(state.selected); } catch (_) {}
      }
    });
    if (!failure) {
      // The restored state is running. Everything this page holds belongs to
      // the replaced one, so the progress stays until the page is replaced.
      markRestored();
      window.location.reload();
      return;
    }
    progress.close();
    notify(failure.message || t('backups.restore_failed'), 'error');
  }

  // ── Remove ───────────────────────────────────────────────────────────────

  async function removeBackup(id) {
    const item = state.inventory.find(entry => entry.id === id);
    if (!item) return;
    const asked = window.DPSettingsModal.confirm({
      title: say('backups.remove_title'),
      message: say('backups.remove_message', {when: when(item.created_at)}),
      confirmLabel: say('backups.remove_accept'),
      tone: 'danger',
    });
    // The open confirmation's sentence (mounted as confirm() returns) names its
    // date from the backup's own timestamp, so a language change formats it
    // again (see the listener).
    const sentence = document.querySelector('.dp-modal-dialog [data-i18n="backups.remove_message"]:not([data-backup-when])');
    if (sentence) sentence.dataset.backupWhen = String(item.created_at ?? '');
    const confirmed = await asked;
    if (!confirmed) return;
    await run(async () => {
      try {
        await request('DELETE', `${INVENTORY}/${encodeURIComponent(id)}`, undefined, 30000);
        notify(t('backups.removed'), 'success');
      } catch (error) {
        notify(error.message || t('backups.remove_failed'), 'error');
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
      title: say('backups.title'),
      dismiss: true,
      closeControl: true,
      className: 'dp-backup-manager-dialog',
      bodyClassName: 'dp-backup-manager-body',
      actions: [
        {id: 'add', label: say('backups.add')},
        {id: 'save', label: say('backups.save'), disabled: true},
        {id: 'restore', label: say('backups.restore'), tone: 'success', disabled: true},
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

  /* A language change: an open manager draws its inventory again, its dates
   * formatted in the shown language. Its selection, busy state and the focused
   * control (by what it is) are kept; nothing is read or sent. */
  document.addEventListener('debridpulse:locale-changed', () => {
    // An open restore or remove confirmation: its date, formatted again from
    // the stored timestamp (the authority); a keyed sentence keeps its key.
    document.querySelectorAll('.dp-modal-dialog [data-backup-when]').forEach(node => {
      const shown = when(node.dataset.backupWhen);
      if (!node.dataset.i18n) { node.textContent = shown; return; }
      node.dataset.i18nValues = JSON.stringify({when: shown});
      node.textContent = t(node.dataset.i18n, {when: shown});
    });
    const body = state.body;
    if (!body || !state.dialog) return;
    const active = body.contains(document.activeElement) ? document.activeElement : null;
    const select = active?.dataset.backupSelect;
    const remove = active?.dataset.backupRemove;
    render();
    const again = select !== undefined ? body.querySelector(`[data-backup-select="${CSS.escape(select)}"]`)
      : remove !== undefined ? body.querySelector(`[data-backup-remove="${CSS.escape(remove)}"]`) : null;
    again?.focus();
  });

  announceRestored();

  window.DPBackupManager = Object.freeze({open});
})();
