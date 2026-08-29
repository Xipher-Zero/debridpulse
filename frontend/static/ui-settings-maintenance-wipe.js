/* DebridPulse v1.0.11 Data & Maintenance database-wipe presentation pass. */
(function () {
  'use strict';

  const CARD_MARKER = 'dpWipeControlsPolished';
  let scheduled = false;

  function textOf(node) {
    return String(node?.textContent || '').trim();
  }

  function findWipeCard() {
    const panel = document.querySelector('#view-settings [data-panel="maintenance"]');
    if (!panel) return null;
    return Array.from(panel.querySelectorAll('.dp-settings-card')).find(function (card) {
      const title = card.querySelector(':scope > .card-header > .card-title');
      const value = textOf(title);
      return value === 'Database Destructive Actions' || value === 'Database Wipe Controls';
    }) || null;
  }

  function setToggleCopy(toggle, title, detail) {
    if (!toggle) return false;
    const titleNode = toggle.querySelector('.toggle-info .tl');
    const detailNode = toggle.querySelector('.toggle-info .td');
    if (!titleNode || !detailNode) return false;
    titleNode.textContent = title;
    detailNode.textContent = detail;
    return true;
  }

  function polishCard(card) {
    if (!card || card.dataset[CARD_MARKER] === '1') return;

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

    // Mark before mutation so our own DOM changes cannot feed the observer back
    // into the same card. A Settings rerender creates a fresh, unmarked card.
    card.dataset[CARD_MARKER] = '1';
    card.classList.add('dp-settings-database-wipe-card');

    title.textContent = 'Database Wipe Controls';

    let headerCopy = header.querySelector('.dp-settings-database-wipe-header-copy');
    if (!headerCopy) {
      headerCopy = document.createElement('div');
      headerCopy.className = 'dp-settings-card-header-center dp-settings-database-wipe-header-copy';
      header.appendChild(headerCopy);
    }
    headerCopy.textContent = 'Configure wipe safeguards and perform a database wipe.';

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

    const row = document.createElement('div');
    row.className = 'dp-settings-database-wipe-row';
    caution.insertAdjacentElement('afterend', row);

    backupToggle.classList.add('dp-settings-database-wipe-toggle');
    allowToggle.classList.add('dp-settings-database-wipe-toggle');
    wipeActions.classList.add('dp-settings-database-wipe-action');

    // Requested operator order: backup safeguard, explicit unlock, destructive action.
    row.append(backupToggle, allowToggle, wipeActions);
  }

  function polish() {
    scheduled = false;
    const card = findWipeCard();
    if (card) polishCard(card);
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
      const card = findWipeCard();
      if (card && card.dataset[CARD_MARKER] !== '1') schedule();
    });
    observer.observe(view, {childList: true, subtree: true});
  }
})();
