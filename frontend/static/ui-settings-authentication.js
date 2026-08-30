/* DebridPulse v1.0.11 Authentication presentation pass. */
(function () {
  'use strict';

  const USERNAME_PASSWORD_MARKER = 'dpUsernamePasswordPolished';
  let scheduled = false;

  function textOf(node) {
    return String(node?.textContent || '').trim();
  }

  function panel() {
    return document.querySelector('#view-settings [data-panel="authentication"]');
  }

  function findUsernamePasswordCard() {
    const host = panel();
    if (!host) return null;
    return Array.from(host.querySelectorAll('.dp-settings-card')).find(function (card) {
      return textOf(card.querySelector(':scope > .card-header > .card-title')) === 'Username & Password';
    }) || null;
  }

  function fieldFor(card, key) {
    return card?.querySelector(`[data-setting="${key}"]`)?.closest('.dp-settings-field') || null;
  }

  function setFieldCopy(field, label, hint) {
    if (!field) return false;
    const labelNode = field.querySelector(':scope > .form-label');
    if (!labelNode) return false;
    labelNode.textContent = label;

    let hintNode = field.querySelector(':scope > .form-hint');
    if (!hintNode) {
      hintNode = document.createElement('span');
      hintNode.className = 'form-hint';
      field.appendChild(hintNode);
    }
    hintNode.textContent = hint;
    return true;
  }

  function addCenteredHeaderCopy(header) {
    let copy = header.querySelector('.dp-settings-auth-header-copy');
    if (!copy) {
      copy = document.createElement('div');
      copy.className = 'dp-settings-card-header-center dp-settings-auth-header-copy';
      header.appendChild(copy);
    }
    copy.textContent = 'Configure local credentials for browser sign-in and HTTP Basic API access.';
  }

  function polishUsernamePasswordCard(card) {
    if (!card || card.dataset[USERNAME_PASSWORD_MARKER] === '1') return;

    const header = card.querySelector(':scope > .card-header');
    const body = card.querySelector(':scope > .card-body');
    const enable = card.querySelector('input[data-setting="auth_password_enabled"]')?.closest('.dp-settings-toggle') || null;
    const usernameField = fieldFor(card, 'auth_username');
    const passwordField = card.querySelector('#dp-auth-new-password')?.closest('.dp-settings-field') || null;
    const actions = card.querySelector('button[data-action="clear-password"]')?.closest('.dp-settings-actions') || null;
    const clearButton = actions?.querySelector('button[data-action="clear-password"]') || null;

    if (!header || !body || !enable || !usernameField || !passwordField || !actions || !clearButton) return;

    // Mark before mutation so our own DOM changes cannot schedule repeated work.
    card.dataset[USERNAME_PASSWORD_MARKER] = '1';
    card.classList.add('dp-settings-username-password-card');

    addCenteredHeaderCopy(header);

    const enableInfo = enable.querySelector('.toggle-info');
    const enableTitle = enableInfo?.querySelector('.tl');
    if (enableTitle) enableTitle.textContent = 'Enable';
    enableInfo?.querySelector('.td')?.remove();
    enable.classList.add('dp-settings-auth-header-enable');
    header.appendChild(enable);

    setFieldCopy(
      usernameField,
      'Username',
      'Username used for browser and HTTP Basic authentication.'
    );
    setFieldCopy(
      passwordField,
      'New Password',
      'Leave blank to keep the current password. Enter a new password to replace it.'
    );

    actions.classList.add('dp-settings-auth-password-actions');
    const actionLabel = document.createElement('span');
    actionLabel.className = 'form-label dp-settings-auth-action-label';
    actionLabel.setAttribute('aria-hidden', 'true');
    actionLabel.textContent = '\u00a0';

    const actionControl = document.createElement('span');
    actionControl.className = 'dp-settings-auth-action-control';
    actionControl.appendChild(clearButton);
    actions.replaceChildren(actionLabel, actionControl);

    const row = document.createElement('div');
    row.className = 'dp-settings-auth-credentials-row';
    row.append(usernameField, passwordField, actions);
    body.replaceChildren(row);
  }

  function polish() {
    scheduled = false;
    const card = findUsernamePasswordCard();
    if (card) polishUsernamePasswordCard(card);
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
      const card = findUsernamePasswordCard();
      if (card && card.dataset[USERNAME_PASSWORD_MARKER] !== '1') schedule();
    });
    observer.observe(view, {childList: true, subtree: true});
  }
})();
