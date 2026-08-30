/* DebridPulse v1.0.11 Notifications presentation pass. */
(function () {
  'use strict';

  const MARKER = 'dpNotificationsPolished';
  let scheduled = false;

  function textOf(node) {
    return String(node?.textContent || '').trim();
  }

  function findDiscordCard() {
    const panel = document.querySelector('#view-settings [data-panel="notifications"]');
    if (!panel) return null;
    return Array.from(panel.querySelectorAll('.dp-settings-card')).find(function (card) {
      const title = card.querySelector(':scope > .card-header > .card-title');
      return textOf(title) === 'Discord Notifications';
    }) || null;
  }

  function fieldFor(card, key) {
    return card.querySelector(`[data-setting="${key}"]`)?.closest('.dp-settings-field') || null;
  }

  function toggleFor(card, key) {
    return card.querySelector(`input[data-setting="${key}"]`)?.closest('.dp-settings-toggle') || null;
  }

  function clearFor(card, key) {
    return card.querySelector(`input[data-clear-secret="${key}"]`)?.closest('.dp-settings-clear-secret') || null;
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

  function setToggleCopy(toggle, title, detail) {
    if (!toggle) return false;
    const titleNode = toggle.querySelector('.toggle-info .tl');
    const detailNode = toggle.querySelector('.toggle-info .td');
    if (!titleNode || !detailNode) return false;
    titleNode.textContent = title;
    detailNode.textContent = detail;
    return true;
  }

  function absorbSecretClear(field, clearControl, title, detail) {
    if (!field || !clearControl) return;
    clearControl.classList.add('dp-settings-notifications-clear-secret');
    const strong = clearControl.querySelector('b');
    const small = clearControl.querySelector('small');
    if (strong) strong.textContent = title;
    if (small) small.textContent = detail;
    field.appendChild(clearControl);
  }

  function polishCard(card) {
    if (!card || card.dataset[MARKER] === '1') return;

    const header = card.querySelector(':scope > .card-header');
    const body = card.querySelector(':scope > .card-body');
    const displayField = fieldFor(card, 'discord_username');
    const avatarField = fieldFor(card, 'discord_avatar_url');
    const webhookField = fieldFor(card, 'discord_webhook_url');
    const addedWebhookField = fieldFor(card, 'discord_webhook_added');
    const intervalField = fieldFor(card, 'update_check_interval_hours');
    const avatarActions = card.querySelector('.dp-settings-file-button')?.closest('.dp-settings-actions') || null;
    const avatarPreview = card.querySelector('#dp-settings-avatar-preview');

    const addedToggle = toggleFor(card, 'discord_notify_added');
    const completedToggle = toggleFor(card, 'discord_notify_finished');
    const errorToggle = toggleFor(card, 'discord_notify_error');
    const extractToggle = toggleFor(card, 'discord_notify_extract');
    const updateToggle = toggleFor(card, 'discord_notify_update');

    if (!header || !body || !displayField || !avatarField || !webhookField || !addedWebhookField || !intervalField ||
        !avatarActions || !avatarPreview || !addedToggle || !completedToggle || !errorToggle || !extractToggle || !updateToggle) {
      return;
    }

    const webhookClear = clearFor(card, 'discord_webhook_url');
    const addedWebhookClear = clearFor(card, 'discord_webhook_added');

    // Mark before mutation so our own DOM changes cannot cause repeated work.
    card.dataset[MARKER] = '1';
    card.classList.add('dp-settings-discord-card');

    let headerCopy = header.querySelector('.dp-settings-notifications-header-copy');
    if (!headerCopy) {
      headerCopy = document.createElement('div');
      headerCopy.className = 'dp-settings-card-header-center dp-settings-notifications-header-copy';
      header.appendChild(headerCopy);
    }
    headerCopy.textContent = 'Configure notification identity, delivery destinations, and event alerts.';

    if (!header.querySelector('.dp-settings-notifications-header-spacer')) {
      const spacer = document.createElement('div');
      spacer.className = 'dp-settings-notifications-header-spacer';
      spacer.setAttribute('aria-hidden', 'true');
      header.appendChild(spacer);
    }

    setFieldCopy(displayField, 'Display Name', 'Name shown as the sender of Discord notifications.');
    setFieldCopy(avatarField, 'Avatar URL', 'Image shown with Discord notifications. Paste a direct image URL or upload one.');
    setFieldCopy(webhookField, 'Discord Webhook', 'Primary Discord destination for enabled notifications.');
    setFieldCopy(
      addedWebhookField,
      'Download Added Webhook',
      'Optional destination for new-download notifications. Leave blank to use the primary webhook.'
    );
    setFieldCopy(
      intervalField,
      'Update Check Interval (Hours Between Checks)',
      'How often DebridPulse checks for a newer release.'
    );

    setToggleCopy(addedToggle, 'Download Added', 'Send a notification when a new download is accepted.');
    setToggleCopy(completedToggle, 'Download Completed', 'Send a notification when a download finishes successfully.');
    setToggleCopy(errorToggle, 'Download Error', 'Send a notification when a download fails.');
    setToggleCopy(extractToggle, 'Extraction Result', 'Send a notification when archive extraction completes or fails.');
    setToggleCopy(updateToggle, 'Update Available', 'Send a notification when a newer DebridPulse release is detected.');

    absorbSecretClear(
      webhookField,
      webhookClear,
      'Clear Stored Webhook',
      'Remove the saved primary webhook when Settings are applied.'
    );
    absorbSecretClear(
      addedWebhookField,
      addedWebhookClear,
      'Clear Stored Download Added Webhook',
      'Remove the saved Download Added webhook when Settings are applied.'
    );

    avatarActions.classList.add('dp-settings-avatar-actions');
    avatarPreview.classList.add('dp-settings-avatar-preview--compact');
    avatarField.appendChild(avatarPreview);

    const identityRow = document.createElement('div');
    identityRow.className = 'dp-settings-notifications-identity-row';
    identityRow.append(displayField, avatarField, avatarActions);

    const deliveryRow = document.createElement('div');
    deliveryRow.className = 'dp-settings-notifications-delivery-row';
    deliveryRow.append(webhookField, addedWebhookField, intervalField);

    const primaryToggleRow = document.createElement('div');
    primaryToggleRow.className = 'dp-settings-notifications-toggle-row dp-settings-notifications-toggle-row--primary';
    primaryToggleRow.append(addedToggle, completedToggle, errorToggle);

    const secondaryToggleRow = document.createElement('div');
    secondaryToggleRow.className = 'dp-settings-notifications-toggle-row dp-settings-notifications-toggle-row--secondary';
    secondaryToggleRow.append(extractToggle, updateToggle);

    body.replaceChildren(identityRow, deliveryRow, primaryToggleRow, secondaryToggleRow);
  }

  function polish() {
    scheduled = false;
    const card = findDiscordCard();
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
      const card = findDiscordCard();
      if (card && card.dataset[MARKER] !== '1') schedule();
    });
    observer.observe(view, {childList: true, subtree: true});
  }
})();
