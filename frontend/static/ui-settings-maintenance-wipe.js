/* DebridPulse v1.0.11 Data & Maintenance presentation pass. */
(function () {
  'use strict';

  const WIPE_MARKER = 'dpWipeControlsPolished';
  const BACKUPS_MARKER = 'dpBackupsRetentionPolished';
  let scheduled = false;

  function textOf(node) {
    return String(node?.textContent || '').trim();
  }

  function maintenancePanel() {
    return document.querySelector('#view-settings [data-panel="maintenance"]');
  }

  function findCard(titles) {
    const panel = maintenancePanel();
    if (!panel) return null;
    const accepted = new Set(titles);
    return Array.from(panel.querySelectorAll('.dp-settings-card')).find(function (card) {
      const title = card.querySelector(':scope > .card-header > .card-title');
      return accepted.has(textOf(title));
    }) || null;
  }

  function findWipeCard() {
    return findCard(['Database Destructive Actions', 'Database Wipe Controls']);
  }

  function findBackupsCard() {
    return findCard(['Backups & Retention']);
  }

  function setToggleCopy(toggle, title, detail) {
    if (!toggle) return false;
    const titleNode = toggle.querySelector('.toggle-info .tl');
    if (!titleNode) return false;
    titleNode.textContent = title;

    const detailNode = toggle.querySelector('.toggle-info .td');
    if (detail) {
      if (detailNode) {
        detailNode.textContent = detail;
      } else {
        const created = document.createElement('span');
        created.className = 'td';
        created.textContent = detail;
        toggle.querySelector('.toggle-info')?.appendChild(created);
      }
    } else if (detailNode) {
      detailNode.remove();
    }
    return true;
  }

  function ensureHeaderCopy(header, className, copy) {
    let node = header.querySelector('.' + className);
    if (!node) {
      node = document.createElement('div');
      node.className = 'dp-settings-card-header-center ' + className;
      header.appendChild(node);
    }
    node.textContent = copy;
    return node;
  }

  function setFieldPresentation(card, key, title, hint) {
    const control = card.querySelector('[data-setting="' + key + '"]');
    const field = control?.closest('.dp-settings-field');
    const label = field?.querySelector('.form-label');
    if (!field || !label) return null;

    label.textContent = title;
    let hintNode = field.querySelector(':scope > .form-hint');
    if (!hintNode) {
      hintNode = document.createElement('span');
      hintNode.className = 'form-hint';
      field.appendChild(hintNode);
    }
    hintNode.textContent = hint;
    return field;
  }

  function polishWipeCard(card) {
    if (!card || card.dataset[WIPE_MARKER] === '1') return;

    const header = card.querySelector(':scope > .card-header');
    const title = header?.querySelector(':scope > .card-title');
    const caution = card.querySelector(':scope > .card-body > .dp-settings-caution');
    const backupInput = card.querySelector('input[data-setting="db_backup_before_wipe"]');
    const allowInput = card.querySelector('input[data-setting="db_wipe_enabled"]');
    const backupToggle = backupInput?.closest('.dp-settings-toggle');
    const allowToggle = allowInput?.closest('.dp-settings-toggle');
    const wipeButton = card.querySelector('button[data-action="wipe-database"]');
    const wipeActions = wipeButton?.closest('.dp-settings-actions');

    if (!header || !title || !caution || !backupToggle || !allowToggle || !wipeButton || !wipeActions) return;

    card.dataset[WIPE_MARKER] = '1';
    card.classList.add('dp-settings-database-wipe-card');
    title.textContent = 'Database Wipe Controls';

    ensureHeaderCopy(
      header,
      'dp-settings-database-wipe-header-copy',
      'Configure database safeguards. Perform a destructive database reset when required.'
    );

    const cautionTitle = caution.querySelector('b');
    const cautionBody = caution.querySelector('span');
    if (cautionTitle) cautionTitle.textContent = 'Database Wipe is Destructive';
    if (cautionBody) {
      cautionBody.textContent = 'Processing must be paused before the database can be wiped. A backup can be created automatically before the wipe begins.';
    }

    if (!setToggleCopy(
      backupToggle,
      'Backup Database Before Wipe',
      'Create a backup before wiping the database. The wipe is aborted if the backup fails.'
    )) return;
    if (!setToggleCopy(
      allowToggle,
      'Allow Database Wipe',
      'Unlock the database wipe action.'
    )) return;

    let row = card.querySelector('.dp-settings-database-wipe-row');
    if (!row) {
      row = document.createElement('div');
      row.className = 'dp-settings-database-wipe-row';
      caution.insertAdjacentElement('afterend', row);
    }

    backupToggle.classList.add('dp-settings-database-wipe-toggle');
    allowToggle.classList.add('dp-settings-database-wipe-toggle');
    wipeActions.classList.add('dp-settings-database-wipe-action');

    // Operator order: backup safeguard, explicit unlock, destructive action.
    row.append(backupToggle, allowToggle, wipeActions);
  }

  function polishBackupsCard(card) {
    if (!card || card.dataset[BACKUPS_MARKER] === '1') return;

    const header = card.querySelector(':scope > .card-header');
    const body = card.querySelector(':scope > .card-body');
    const title = header?.querySelector(':scope > .card-title');
    const enabledInput = card.querySelector('input[data-setting="backup_enabled"]');
    const enabledToggle = enabledInput?.closest('.dp-settings-toggle');
    const runButton = card.querySelector('button[data-action="run-backup"]');
    const listButton = card.querySelector('button[data-action="list-backups"]');
    const actions = runButton?.closest('.dp-settings-actions');

    if (!header || !body || !title || !enabledToggle || !runButton || !listButton || !actions) return;

    const fieldSpecs = [
      ['backup_folder', 'Backup Folder', 'Choose where DebridPulse stores database and configuration backups.'],
      ['backup_interval_hours', 'Backup Interval (Hours Between Backups)', 'Set how often an automatic backup is created.'],
      ['backup_keep_days', 'Backup Retention (Days to Keep)', 'Delete backup files older than the configured number of days.'],
      ['stats_snapshot_interval_minutes', 'Statistics Snapshot Interval (Minutes Between Snapshots)', 'Set how often DebridPulse records a statistics snapshot.'],
      ['stats_snapshot_keep_days', 'Statistics Snapshot Retention (Days to Keep)', 'Delete statistics snapshots older than the configured number of days.'],
      ['events_keep_days', 'Event Log Retention (Days to Keep)', 'Delete event log entries older than the configured number of days.'],
    ];

    const fields = fieldSpecs.map(function (spec) {
      return setFieldPresentation(card, spec[0], spec[1], spec[2]);
    });
    if (fields.some(function (field) { return !field; })) return;

    card.dataset[BACKUPS_MARKER] = '1';
    card.classList.add('dp-settings-backups-retention-card');

    ensureHeaderCopy(
      header,
      'dp-settings-backups-header-copy',
      'Configure automated backups and retention for backups, statistics snapshots, and event logs.'
    );

    setToggleCopy(enabledToggle, 'Enable', '');
    enabledToggle.classList.add('dp-settings-backups-header-toggle');
    header.appendChild(enabledToggle);

    let grid = card.querySelector('.dp-settings-backups-field-grid');
    if (!grid) {
      grid = document.createElement('div');
      grid.className = 'dp-settings-backups-field-grid';
      body.prepend(grid);
    }
    fields.forEach(function (field) { grid.appendChild(field); });

    actions.classList.add('dp-settings-backups-actions');
    grid.insertAdjacentElement('afterend', actions);
  }

  function polish() {
    scheduled = false;
    polishBackupsCard(findBackupsCard());
    polishWipeCard(findWipeCard());
  }

  function schedule() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(polish);
  }

  schedule();

  const view = document.getElementById('view-settings');
  if (view) {
    const observer = new MutationObserver(function (mutations) {
      if (!mutations.some(function (mutation) { return mutation.type === 'childList'; })) return;
      const backups = findBackupsCard();
      const wipe = findWipeCard();
      if ((backups && backups.dataset[BACKUPS_MARKER] !== '1') ||
          (wipe && wipe.dataset[WIPE_MARKER] !== '1')) {
        schedule();
      }
    });
    observer.observe(view, {childList: true, subtree: true});
  }
})();
