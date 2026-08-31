/* DebridPulse v1.0.11 OpenID Connect Settings regrouping pass. */
(function () {
  'use strict';

  const OIDC_MARKER = 'dpOidcGrouped';
  let scheduled = false;

  function textOf(node) {
    return String(node?.textContent || '').trim();
  }

  function panel() {
    return document.querySelector('#view-settings [data-panel="authentication"]');
  }

  function cardByTitle(title) {
    const host = panel();
    if (!host) return null;
    return Array.from(host.querySelectorAll('.dp-settings-card')).find(function (card) {
      return textOf(card.querySelector(':scope > .card-header > .card-title')) === title;
    }) || null;
  }

  function oidcCard() {
    return cardByTitle('OpenID Connect');
  }

  function statusCard() {
    return cardByTitle('Authentication Status');
  }

  function fieldFor(card, key) {
    return card?.querySelector(`[data-setting="${key}"]`)?.closest('.dp-settings-field') || null;
  }

  function fieldByLabel(card, label) {
    return Array.from(card?.querySelectorAll('.dp-settings-field') || []).find(function (field) {
      return textOf(field.querySelector(':scope > .form-label')) === label;
    }) || null;
  }

  function setFieldCopy(field, label, hint) {
    if (!field) return;
    const labelNode = field.querySelector(':scope > .form-label');
    if (labelNode && textOf(labelNode) !== label) labelNode.textContent = label;

    let hintNode = field.querySelector(':scope > .form-hint');
    if (!hintNode) {
      hintNode = document.createElement('span');
      hintNode.className = 'form-hint';
      const control = field.querySelector(':scope > input, :scope > textarea, :scope > select');
      if (control) control.insertAdjacentElement('afterend', hintNode);
      else field.appendChild(hintNode);
    }
    if (textOf(hintNode) !== hint) hintNode.textContent = hint;
  }

  function markSandwich(field) {
    if (!field) return;
    field.classList.add('dp-settings-oidc-sandwich');
  }

  function createRow(className, nodes) {
    const row = document.createElement('div');
    row.className = `dp-settings-oidc-row ${className}`;
    nodes.forEach(function (node) { row.appendChild(node); });
    return row;
  }

  function configureHeader(card, enable) {
    const header = card.querySelector(':scope > .card-header');
    if (!header || !enable) return false;

    let copy = header.querySelector(':scope > .dp-settings-auth-header-copy');
    if (!copy) {
      copy = document.createElement('div');
      copy.className = 'dp-settings-card-header-center dp-settings-auth-header-copy dp-settings-oidc-header-copy';
      header.appendChild(copy);
    }
    const headerCopy = 'Configure an external identity provider for browser sign-in.';
    if (textOf(copy) !== headerCopy) copy.textContent = headerCopy;

    const info = enable.querySelector('.toggle-info');
    const title = info?.querySelector('.tl');
    if (title && textOf(title) !== 'Enable') title.textContent = 'Enable';
    info?.querySelector('.td')?.remove();
    enable.classList.add('dp-settings-auth-header-enable', 'dp-settings-oidc-header-enable');
    if (enable.parentElement !== header || enable !== header.lastElementChild) header.appendChild(enable);
    return true;
  }

  function configureClearSecret(secretField) {
    if (!secretField) return null;

    const existingCheckbox = secretField.querySelector('#dp-auth-clear-oidc-secret') || null;
    const existingLabel = existingCheckbox?.closest('label') || null;

    const action = document.createElement('div');
    action.className = 'dp-settings-oidc-clear-secret-action';

    const spacer = document.createElement('span');
    spacer.className = 'form-label dp-settings-oidc-clear-secret-spacer';
    spacer.textContent = 'Clear Stored Secret';

    const control = document.createElement('div');
    control.className = 'dp-settings-oidc-clear-secret-control';

    const hint = document.createElement('small');
    hint.className = 'dp-settings-oidc-clear-secret-hint';

    if (existingCheckbox && existingLabel) {
      existingLabel.className = 'dp-settings-oidc-clear-secret';

      const title = document.createElement('span');
      title.className = 'dp-settings-oidc-clear-secret-copy';
      title.textContent = 'Clear Stored Secret';
      existingLabel.replaceChildren(title, existingCheckbox);
      control.appendChild(existingLabel);
      hint.textContent = 'Remove the saved secret when settings are applied.';
    } else {
      // A cleared or never-configured client secret is a valid OIDC state. The
      // legacy transformer treated the missing clear-secret checkbox as a hard
      // grouping failure after already mutating the card. Its MutationObserver
      // then retried forever, wedging the page. Render an inert control instead
      // so grouping completes once and the serializer still resolves false.
      const disabledLabel = document.createElement('label');
      disabledLabel.className = 'dp-settings-oidc-clear-secret is-disabled';

      const title = document.createElement('span');
      title.className = 'dp-settings-oidc-clear-secret-copy';
      title.textContent = 'Clear Stored Secret';

      const disabledCheckbox = document.createElement('input');
      disabledCheckbox.id = 'dp-auth-clear-oidc-secret';
      disabledCheckbox.type = 'checkbox';
      disabledCheckbox.disabled = true;
      disabledCheckbox.setAttribute('aria-disabled', 'true');

      disabledLabel.append(title, disabledCheckbox);
      control.appendChild(disabledLabel);
      hint.textContent = 'No stored client secret is configured.';
    }

    action.append(spacer, control, hint);
    return action;
  }

  function configureAccessControl(allowAll, subjects, emails, groups) {
    const info = allowAll.querySelector('.toggle-info');
    const title = info?.querySelector('.tl');
    if (title) title.textContent = 'Allow Any Authenticated OIDC Identity';
    info?.querySelector('.td')?.remove();
    allowAll.classList.add('dp-settings-oidc-allow-all');

    const heading = document.createElement('div');
    heading.className = 'dp-settings-oidc-section-heading';

    const headingTitle = document.createElement('span');
    headingTitle.className = 'dp-settings-oidc-section-title';
    headingTitle.textContent = 'Access Control';

    const headingCopy = document.createElement('small');
    headingCopy.className = 'dp-settings-oidc-section-copy';
    headingCopy.textContent = 'Choose whether any authenticated OIDC identity is accepted or restrict sign-in to the allowlists below.';

    heading.append(headingTitle, headingCopy, allowAll);

    const allowlists = document.createElement('div');
    allowlists.className = 'dp-settings-oidc-allowlists';
    allowlists.append(subjects, emails, groups);

    const access = document.createElement('section');
    access.className = 'dp-settings-oidc-access';
    access.append(heading, allowlists);
    return access;
  }

  function moveVerifyToFooter(button, contextActions) {
    if (!button || !contextActions) return false;

    const oldActions = button.closest('.dp-settings-actions');
    button.className = 'btn btn-ghost';
    button.textContent = 'Test OIDC Sign-In';
    button.dataset.contextAction = 'authentication';
    button.hidden = !!panel()?.hidden;
    contextActions.appendChild(button);
    if (oldActions && oldActions.childElementCount === 0) oldActions.remove();
    return true;
  }

  function groupOidc() {
    const card = oidcCard();
    if (!card || card.dataset[OIDC_MARKER] === '1') return false;

    const body = card.querySelector(':scope > .card-body');
    const enable = card.querySelector('input[data-setting="auth_oidc_enabled"]')?.closest('.dp-settings-toggle') || null;
    const publicBase = card.querySelector('#dp-auth-public-base-url')?.closest('.dp-settings-field') || null;
    const callback = fieldByLabel(card, 'OIDC Callback URL') || fieldByLabel(statusCard(), 'OIDC Callback URL');
    const provider = fieldFor(card, 'oidc_provider_name');
    const issuer = fieldFor(card, 'oidc_issuer_url');
    const clientId = fieldFor(card, 'oidc_client_id');
    const secret = card.querySelector('#dp-auth-oidc-secret')?.closest('.dp-settings-field') || null;
    const scopes = fieldFor(card, 'oidc_scopes');
    const groupClaim = fieldFor(card, 'oidc_group_claim');
    const allowAll = card.querySelector('input[data-setting="oidc_allow_all"]')?.closest('.dp-settings-toggle') || null;
    const subjects = fieldFor(card, 'oidc_allowed_subjects');
    const emails = fieldFor(card, 'oidc_allowed_emails');
    const groups = fieldFor(card, 'oidc_allowed_groups');
    const verify = card.querySelector('button[data-action="verify-oidc"]');
    const contextActions = document.querySelector('#view-settings .dp-settings-context-actions');

    if (!body || !enable || !publicBase || !callback || !provider || !issuer || !clientId ||
        !secret || !scopes || !groupClaim || !allowAll || !subjects || !emails || !groups ||
        !verify || !contextActions) {
      return false;
    }

    // Mark before DOM mutation so observers cannot re-enter this grouping pass.
    card.dataset[OIDC_MARKER] = '1';
    card.classList.add('dp-settings-oidc-grouped-card');

    configureHeader(card, enable);

    const publicBaseInput = publicBase.querySelector('input');
    setFieldCopy(
      publicBase,
      'Public Base URL',
      publicBaseInput?.readOnly
        ? 'Managed by PUBLIC_BASE_URL. Used for secure browser sessions and OIDC callback generation.'
        : 'Externally reachable HTTPS address used for secure browser sessions and OIDC callback generation.'
    );
    setFieldCopy(callback, 'OIDC Callback URL', 'Copy this redirect URI into your identity provider.');
    setFieldCopy(provider, 'Provider Name', 'Name shown on the sign-in page.');
    setFieldCopy(issuer, 'Issuer URL', 'OIDC issuer URL published by your identity provider.');
    setFieldCopy(clientId, 'Client ID', 'Client identifier issued by your OIDC provider.');
    setFieldCopy(secret, 'Client Secret', 'Leave blank to keep the stored secret. Enter a new value to replace it.');
    const secretInput = secret.querySelector('#dp-auth-oidc-secret');
    if (secretInput && secret.querySelector('#dp-auth-clear-oidc-secret')) {
      secretInput.placeholder = 'Stored Client Secret Configured. Blank keeps it.';
    }
    setFieldCopy(scopes, 'Scopes', 'Space-separated scopes requested during sign-in.');
    setFieldCopy(groupClaim, 'Group Claim', 'Claim containing group memberships used by group authorization rules.');
    setFieldCopy(subjects, 'Allowed Subjects', 'Authorize matching OIDC subject identifiers, one per line.');
    setFieldCopy(emails, 'Allowed Emails', 'Authorize verified email addresses, one per line. Requires email_verified=true.');
    setFieldCopy(groups, 'Allowed Groups', 'Authorize identities belonging to matching OIDC groups, one per line.');

    const clearSecret = configureClearSecret(secret);
    if (!clearSecret) return false;

    [publicBase, callback, provider, issuer, clientId, secret, scopes, groupClaim, subjects, emails, groups]
      .forEach(markSandwich);

    const originRow = createRow('dp-settings-oidc-row--origin', [publicBase, callback]);
    const identityRow = createRow('dp-settings-oidc-row--identity', [provider, issuer]);
    const credentialsRow = createRow('dp-settings-oidc-row--credentials', [clientId, secret, clearSecret]);
    const protocolRow = createRow('dp-settings-oidc-row--protocol', [scopes, groupClaim]);
    const access = configureAccessControl(allowAll, subjects, emails, groups);

    // Move the live test action before replacing the body so its existing node
    // and event-delegation contract survive the regrouping unchanged.
    moveVerifyToFooter(verify, contextActions);
    body.replaceChildren(originRow, identityRow, credentialsRow, protocolRow, access);
    return true;
  }

  function polish() {
    scheduled = false;
    groupOidc();
  }

  function schedule() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(polish);
  }

  document.addEventListener('debridpulse:settings-rendered', schedule);
})();
