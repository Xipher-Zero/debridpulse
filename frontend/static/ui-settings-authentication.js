/* DebridPulse v1.0.11 Authentication presentation pass. */
(function () {
  'use strict';

  const USERNAME_PASSWORD_MARKER = 'dpUsernamePasswordPolished';
  const API_ACCESS_MARKER = 'dpApiAccessPolished';
  const AUTH_STATUS_MARKER = 'dpAuthenticationStatusPolished';
  let scheduled = false;

  function textOf(node) {
    return String(node?.textContent || '').trim();
  }

  function panel() {
    return document.querySelector('#view-settings [data-panel="authentication"]');
  }

  function findCard(title) {
    const host = panel();
    if (!host) return null;
    return Array.from(host.querySelectorAll('.dp-settings-card')).find(function (card) {
      return textOf(card.querySelector(':scope > .card-header > .card-title')) === title;
    }) || null;
  }

  function findUsernamePasswordCard() {
    return findCard('Username & Password');
  }

  function findApiAccessCard() {
    return findCard('API Access');
  }

  function findAuthenticationStatusCard() {
    return findCard('Authentication Status');
  }

  function findSessionsSecurityCard() {
    return findCard('Sessions & Security');
  }

  function findOidcCard() {
    return findCard('OpenID Connect');
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

  function addCenteredHeaderCopy(header, copyText, modifier) {
    let copy = header.querySelector('.dp-settings-auth-header-copy');
    if (!copy) {
      copy = document.createElement('div');
      copy.className = 'dp-settings-card-header-center dp-settings-auth-header-copy';
      header.appendChild(copy);
    }
    if (modifier) copy.classList.add(modifier);
    copy.textContent = copyText;
  }

  function moveEnableToHeader(header, enable) {
    const enableInfo = enable.querySelector('.toggle-info');
    const enableTitle = enableInfo?.querySelector('.tl');
    if (enableTitle) enableTitle.textContent = 'Enable';
    enableInfo?.querySelector('.td')?.remove();
    enable.classList.add('dp-settings-auth-header-enable');
    header.appendChild(enable);
  }

  function mechanismLabel(value) {
    const raw = String(value || '').trim();
    if (raw === 'password_session') return 'Password Session';
    if (raw === 'oidc_session') return 'OIDC Session';
    return raw || 'Open / anonymous';
  }

  function polishAuthenticationStatusCard(card) {
    if (!card || card.dataset[AUTH_STATUS_MARKER] === '1') return;

    const body = card.querySelector(':scope > .card-body');
    const statusGrid = body?.querySelector(':scope > .dp-settings-status-grid') || null;
    const sessionsCard = findSessionsSecurityCard();
    const sessionsBody = sessionsCard?.querySelector(':scope > .card-body') || null;
    const sessionStatusGrid = sessionsBody?.querySelector(':scope > .dp-settings-status-grid') || null;
    const oidcCard = findOidcCard();
    const oidcBody = oidcCard?.querySelector(':scope > .card-body') || null;
    const oidcEnable = oidcCard?.querySelector('input[data-setting="auth_oidc_enabled"]')?.closest('.dp-settings-toggle') || null;
    const lifetimeField = fieldFor(sessionsCard, 'auth_session_lifetime_hours');
    const publicBaseField = sessionsCard?.querySelector('#dp-auth-public-base-url')?.closest('.dp-settings-field') || null;
    const logoutActions = sessionsCard?.querySelector('button[data-action="logout-session"]')?.closest('.dp-settings-actions') || null;
    const logoutButton = logoutActions?.querySelector('button[data-action="logout-session"]') || null;

    const sessionStatuses = Array.from(sessionStatusGrid?.querySelectorAll(':scope > .dp-settings-status') || []);
    const activeSessions = sessionStatuses.find(function (item) {
      return textOf(item.querySelector(':scope > b')).toLowerCase() === 'active browser sessions';
    }) || null;
    const currentMechanism = sessionStatuses.find(function (item) {
      return textOf(item.querySelector(':scope > b')).toLowerCase() === 'current mechanism';
    }) || null;

    if (!body || !statusGrid || !sessionsCard || !sessionsBody || !sessionStatusGrid ||
        !oidcCard || !oidcBody || !oidcEnable || !lifetimeField || !publicBaseField ||
        !logoutActions || !logoutButton || !activeSessions || !currentMechanism) return;

    card.dataset[AUTH_STATUS_MARKER] = '1';
    card.classList.add('dp-settings-auth-status-card');
    oidcCard.classList.add('dp-settings-oidc-card');

    // "Current session" and the moved mechanism tile expose the same backend
    // value. Keep the explicit mechanism tile in the new session row and remove
    // the duplicate KPI from the upper status grid.
    Array.from(statusGrid.querySelectorAll(':scope > .dp-settings-status')).forEach(function (item) {
      if (textOf(item.querySelector(':scope > b')).toLowerCase() === 'current session') item.remove();
    });

    const activeTitle = activeSessions.querySelector(':scope > b');
    if (activeTitle) activeTitle.textContent = 'Active Browser Sessions';

    const mechanismTitle = currentMechanism.querySelector(':scope > b');
    const mechanismValue = currentMechanism.querySelector(':scope > span');
    if (mechanismTitle) mechanismTitle.textContent = 'Current Authentication Mechanism';
    if (mechanismValue) mechanismValue.textContent = mechanismLabel(mechanismValue.textContent);

    setFieldCopy(
      lifetimeField,
      'Browser Session Lifetime',
      'How long a browser login remains valid before sign-in is required again.'
    );
    lifetimeField.classList.add('dp-settings-auth-session-lifetime');

    logoutActions.classList.add('dp-settings-auth-session-actions');
    const actionLabel = document.createElement('span');
    actionLabel.className = 'form-label dp-settings-auth-action-label';
    actionLabel.setAttribute('aria-hidden', 'true');
    actionLabel.textContent = '\u00a0';

    const actionControl = document.createElement('span');
    actionControl.className = 'dp-settings-auth-session-action-control';
    actionControl.appendChild(logoutButton);
    logoutActions.replaceChildren(actionLabel, actionControl);

    const sessionRow = document.createElement('div');
    sessionRow.className = 'dp-settings-auth-session-row';
    sessionRow.append(activeSessions, currentMechanism, lifetimeField, logoutActions);
    statusGrid.after(sessionRow);

    const publicBaseLabel = publicBaseField.querySelector(':scope > .form-label');
    if (publicBaseLabel) publicBaseLabel.textContent = 'Public Base URL';
    publicBaseField.classList.add('dp-settings-auth-public-base-field');
    oidcEnable.after(publicBaseField);

    sessionsCard.remove();
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

    addCenteredHeaderCopy(
      header,
      'Configure local credentials for browser sign-in and HTTP Basic API access.',
      'dp-settings-auth-header-copy--credentials'
    );
    moveEnableToHeader(header, enable);

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

  function installApiTokenLanguageBridge() {
    if (!window.__dpSettingsApiTokenConfirmWrapped && typeof window.confirm === 'function') {
      const baseConfirm = window.confirm.bind(window);
      window.confirm = function (message) {
        const source = String(message ?? '');
        if (source === 'Clear the API token? Existing automation using it will immediately lose access.') {
          return baseConfirm('Revoke the API token? Automation and API clients using it will lose access immediately.');
        }
        return baseConfirm(message);
      };
      window.__dpSettingsApiTokenConfirmWrapped = true;
    }

    if (!window.__dpSettingsApiTokenToastWrapped && typeof window.toast === 'function') {
      const baseToast = window.toast;
      window.toast = function (message, ...args) {
        const translated = String(message ?? '') === 'API token cleared' ? 'API token revoked' : message;
        return baseToast.call(this, translated, ...args);
      };
      window.__dpSettingsApiTokenToastWrapped = true;
    }
  }

  function polishApiAccessCard(card) {
    if (!card || card.dataset[API_ACCESS_MARKER] === '1') return;

    const header = card.querySelector(':scope > .card-header');
    const body = card.querySelector(':scope > .card-body');
    const enable = card.querySelector('input[data-setting="api_token_enabled"]')?.closest('.dp-settings-toggle') || null;
    const generateButton = card.querySelector('button[data-action="generate-token"]');
    const revokeButton = card.querySelector('button[data-action="clear-token"]');
    const actions = generateButton?.closest('.dp-settings-actions') || null;
    const status = Array.from(body?.querySelectorAll(':scope > .dp-settings-copy') || []).find(function (node) {
      return textOf(node).startsWith('Stored token state:');
    }) || null;
    const oneTime = body?.querySelector(':scope > .dp-settings-token-once') || null;
    const tokenWarning = oneTime?.querySelector(':scope > b') || null;
    const tokenField = oneTime?.querySelector(':scope > .dp-settings-inline-field') || null;

    if (!header || !body || !enable || !generateButton || !revokeButton || !actions || !status) return;
    if (oneTime && (!tokenWarning || !tokenField)) return;

    card.dataset[API_ACCESS_MARKER] = '1';
    card.classList.add('dp-settings-api-access-card');

    addCenteredHeaderCopy(
      header,
      'Use a dedicated bearer token for automation, monitoring, and API integrations.',
      'dp-settings-auth-header-copy--api'
    );
    moveEnableToHeader(header, enable);

    generateButton.classList.add('dp-settings-api-token-generate');
    revokeButton.classList.add('dp-settings-api-token-revoke');
    revokeButton.textContent = 'Revoke Token';
    revokeButton.addEventListener('click', function () {
      queueMicrotask(function () {
        if (textOf(revokeButton) === 'Clearing…') revokeButton.textContent = 'Revoking…';
      });
    });

    const configured = !revokeButton.disabled;
    const stateValue = document.createElement('b');
    stateValue.textContent = configured ? 'Configured' : 'Not Configured';
    status.replaceChildren(document.createTextNode('Stored Token: '), stateValue);
    status.classList.add('dp-settings-api-token-status');

    actions.classList.add('dp-settings-api-token-actions');

    const layout = document.createElement('div');
    layout.className = 'dp-settings-api-token-layout';
    layout.append(actions, status);

    if (oneTime) {
      layout.classList.add('has-token');
      tokenWarning.textContent = 'Copy this token now. DebridPulse will not display it again.';
      tokenWarning.classList.add('dp-settings-api-token-warning');
      tokenField.classList.add('dp-settings-api-token-field');
      layout.append(tokenWarning, tokenField);
    }

    body.replaceChildren(layout);
  }

  function needsPolish() {
    const credentials = findUsernamePasswordCard();
    const apiAccess = findApiAccessCard();
    const status = findAuthenticationStatusCard();
    const sessions = findSessionsSecurityCard();
    return !!(
      (credentials && credentials.dataset[USERNAME_PASSWORD_MARKER] !== '1') ||
      (apiAccess && apiAccess.dataset[API_ACCESS_MARKER] !== '1') ||
      (status && status.dataset[AUTH_STATUS_MARKER] !== '1') ||
      sessions
    );
  }

  function polish() {
    scheduled = false;
    installApiTokenLanguageBridge();

    const status = findAuthenticationStatusCard();
    if (status) polishAuthenticationStatusCard(status);

    const credentials = findUsernamePasswordCard();
    if (credentials) polishUsernamePasswordCard(credentials);

    const apiAccess = findApiAccessCard();
    if (apiAccess) polishApiAccessCard(apiAccess);
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
      if (needsPolish()) schedule();
    });
    observer.observe(view, {childList: true, subtree: true});
  }
})();
