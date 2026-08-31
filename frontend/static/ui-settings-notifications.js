/* DebridPulse v1.0.11 Notifications presentation + contextual actions pass. */
(function () {
  'use strict';

  const DISCORD_MARKER = 'dpNotificationsPolished';
  const REPORTING_MARKER = 'dpStatisticsReportingPolished';
  let scheduled = false;

  function textOf(node) {
    return String(node?.textContent || '').trim();
  }

  function panel() {
    return document.querySelector('#view-settings [data-panel="notifications"]');
  }

  function findCard(titles) {
    const host = panel();
    if (!host) return null;
    return Array.from(host.querySelectorAll('.dp-settings-card')).find(function (card) {
      const title = textOf(card.querySelector(':scope > .card-header > .card-title'));
      return titles.includes(title);
    }) || null;
  }

  function findDiscordCard() {
    return findCard(['Discord Notifications']);
  }

  function findReportingCard() {
    return findCard(['Statistics Reports', 'Statistics Reporting']);
  }

  function fieldFor(card, key) {
    return card?.querySelector(`[data-setting="${key}"]`)?.closest('.dp-settings-field') || null;
  }

  function inputFor(key) {
    return panel()?.querySelector(`[data-setting="${key}"]`) || null;
  }

  function toggleFor(card, key) {
    return card?.querySelector(`input[data-setting="${key}"]`)?.closest('.dp-settings-toggle') || null;
  }

  function clearFor(card, key) {
    return card?.querySelector(`input[data-clear-secret="${key}"]`)?.closest('.dp-settings-clear-secret') || null;
  }

  function valueOf(key, fallback = '') {
    const input = inputFor(key);
    return input ? String(input.value ?? '').trim() : fallback;
  }

  function clearChecked(key) {
    return !!panel()?.querySelector(`input[data-clear-secret="${key}"]`)?.checked;
  }

  function intValueOf(key, fallback) {
    const parsed = Number.parseInt(valueOf(key), 10);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function notify(message, kind = 'info') {
    if (typeof toast === 'function') {
      toast(String(message), kind);
    } else {
      console[kind === 'error' ? 'error' : 'log']('[DebridPulse Settings]', message);
    }
  }

  async function request(method, path, body, timeout) {
    if (typeof api !== 'function') throw new Error('Application API client is unavailable');
    return api(method, path, body, timeout);
  }

  function setActionBusy(button, busy, label) {
    if (!button) return;
    if (busy) {
      if (!button.dataset.dpIdleHtml) button.dataset.dpIdleHtml = button.innerHTML;
      button.disabled = true;
      button.textContent = label || 'Working…';
      return;
    }
    button.disabled = false;
    if (button.dataset.dpIdleHtml) {
      button.innerHTML = button.dataset.dpIdleHtml;
      delete button.dataset.dpIdleHtml;
    }
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

  function addCenteredHeaderCopy(card, className, text) {
    const header = card?.querySelector(':scope > .card-header');
    if (!header) return false;

    let copy = header.querySelector(`.${className}`);
    if (!copy) {
      copy = document.createElement('div');
      copy.className = `dp-settings-card-header-center ${className}`;
      header.appendChild(copy);
    }
    copy.textContent = text;

    if (!header.querySelector('.dp-settings-notifications-header-spacer')) {
      const spacer = document.createElement('div');
      spacer.className = 'dp-settings-notifications-header-spacer';
      spacer.setAttribute('aria-hidden', 'true');
      header.appendChild(spacer);
    }
    return true;
  }

  function glyphMarkup(src, label) {
    return `<span class="dp-settings-action-icon"><img class="dp-settings-action-glyph" src="${src}" alt=""></span><span>${label}</span>`;
  }

  function bindDraftAction(button, handler) {
    if (!button || button.dataset.dpNotificationsDraftBound === '1') return;
    button.dataset.dpNotificationsDraftBound = '1';
    button.addEventListener('click', function (event) {
      event.preventDefault();
      event.stopPropagation();
      void handler(button);
    });
  }

  async function testDiscordDraft(button) {
    setActionBusy(button, true, 'Testing…');
    try {
      await request('POST', '/settings/validate-discord', {
        webhook_url: valueOf('discord_webhook_url'),
        clear_webhook: clearChecked('discord_webhook_url'),
        username: valueOf('discord_username'),
        avatar_url: valueOf('discord_avatar_url'),
      }, 20000);
      notify('Discord notification sent', 'success');
    } catch (error) {
      notify(`Discord: ${error.message}`, 'error');
    } finally {
      setActionBusy(button, false);
    }
  }

  async function sendReportNow(button) {
    setActionBusy(button, true, 'Sending…');
    try {
      const hours = Math.max(1, intValueOf('stats_report_window_hours', 24));
      const result = await request('POST', '/settings/send-stats-report', {
        hours,
        stats_report_webhook_url: valueOf('stats_report_webhook_url'),
        clear_stats_report_webhook: clearChecked('stats_report_webhook_url'),
        discord_webhook_url: valueOf('discord_webhook_url'),
        clear_discord_webhook: clearChecked('discord_webhook_url'),
      }, 20000);
      notify(`Report sent (${result.hours || hours}h)`, 'success');
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setActionBusy(button, false);
    }
  }

  function polishFooter(sendReportButton) {
    const saveHint = document.querySelector('#view-settings .dp-settings-master-footer .dp-settings-save-hint');
    if (saveHint) saveHint.textContent = 'Changes remain unsaved until Apply Settings is selected.';

    const testDownloadEngine = document.querySelector('#view-settings button[data-context-action="downloads"][data-action="test-aria2"]');
    if (testDownloadEngine) testDownloadEngine.textContent = 'Test Download Engine';

    const host = panel();
    if (!host) return;

    const testDiscord = document.querySelector('#view-settings button[data-context-action="notifications"][data-action="test-discord"], #view-settings button[data-context-action="notifications"][data-action="test-discord-draft"]');
    if (!testDiscord) return;

    testDiscord.dataset.action = 'test-discord-draft';
    testDiscord.className = 'btn btn-ghost';
    testDiscord.innerHTML = glyphMarkup('/icons/lucide/flask-conical.svg', 'Test Discord');
    bindDraftAction(testDiscord, testDiscordDraft);

    let reportButton = sendReportButton || document.querySelector('#view-settings button[data-context-action="notifications"][data-action="send-report-draft"]');
    if (!reportButton) return;

    reportButton.dataset.action = 'send-report-draft';
    reportButton.dataset.contextAction = 'notifications';
    reportButton.className = 'btn btn-ghost';
    reportButton.innerHTML = glyphMarkup('/icons/lucide/send.svg', 'Send Report Now');
    reportButton.hidden = testDiscord.hidden;
    bindDraftAction(reportButton, sendReportNow);

    testDiscord.insertAdjacentElement('afterend', reportButton);
  }

  function polishDiscordCard(card) {
    if (!card || card.dataset[DISCORD_MARKER] === '1') return;

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

    if (!body || !displayField || !avatarField || !webhookField || !addedWebhookField || !intervalField ||
        !avatarActions || !avatarPreview || !addedToggle || !completedToggle || !errorToggle || !extractToggle || !updateToggle) {
      return;
    }

    const webhookClear = clearFor(card, 'discord_webhook_url');
    const addedWebhookClear = clearFor(card, 'discord_webhook_added');

    // Mark before mutation so our own DOM changes cannot cause repeated work.
    card.dataset[DISCORD_MARKER] = '1';
    card.classList.add('dp-settings-discord-card');

    addCenteredHeaderCopy(
      card,
      'dp-settings-notifications-header-copy',
      'Configure notification identity, delivery destinations, and event alerts.'
    );

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
      'Set how often DebridPulse checks for a newer release. Enter 0 to disable update checks.'
    );

    const updateIntervalInput = intervalField.querySelector('[data-setting="update_check_interval_hours"]');
    if (updateIntervalInput) {
      updateIntervalInput.setAttribute('min', '0');
      updateIntervalInput.setAttribute('max', '168');
      updateIntervalInput.setAttribute('step', '1');
    }

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

  function polishReportingCard(card) {
    if (!card || card.dataset[REPORTING_MARKER] === '1') return;

    const header = card.querySelector(':scope > .card-header');
    const title = header?.querySelector(':scope > .card-title');
    const body = card.querySelector(':scope > .card-body');
    const webhookField = fieldFor(card, 'stats_report_webhook_url');
    const intervalField = fieldFor(card, 'stats_report_interval_hours');
    const windowField = fieldFor(card, 'stats_report_window_hours');
    const sendButton = card.querySelector('button[data-action="send-report"]');

    if (!header || !title || !body || !webhookField || !intervalField || !windowField || !sendButton) return;

    const webhookClear = clearFor(card, 'stats_report_webhook_url');

    card.dataset[REPORTING_MARKER] = '1';
    card.classList.add('dp-settings-statistics-reporting-card');
    title.textContent = 'Statistics Reporting';

    addCenteredHeaderCopy(
      card,
      'dp-settings-statistics-reporting-header-copy',
      'Configure where reports are sent, how often they are delivered, and how much activity they summarize.'
    );

    setFieldCopy(
      webhookField,
      'Reporting Webhook',
      'Optional destination for statistics reports. Leave blank to use the primary Discord webhook.'
    );
    setFieldCopy(
      intervalField,
      'Automatic Report Interval (Hours Between Reports)',
      'Set how often DebridPulse sends statistics reports. Enter 0 to disable automatic reports.'
    );
    setFieldCopy(
      windowField,
      'Report Window',
      'Choose how much recent activity each statistics report includes.'
    );

    const reportIntervalInput = intervalField.querySelector('[data-setting="stats_report_interval_hours"]');
    if (reportIntervalInput) {
      reportIntervalInput.setAttribute('min', '0');
      reportIntervalInput.setAttribute('max', '168');
      reportIntervalInput.setAttribute('step', '1');
    }

    absorbSecretClear(
      webhookField,
      webhookClear,
      'Clear Stored Reporting Webhook',
      'Remove the saved reporting webhook when Settings are applied.'
    );

    const reportingRow = document.createElement('div');
    reportingRow.className = 'dp-settings-statistics-reporting-row';
    reportingRow.append(webhookField, intervalField, windowField);

    // Move the real report action out of the card before replacing its body.
    polishFooter(sendButton);
    body.replaceChildren(reportingRow);
  }

  function polish() {
    scheduled = false;
    const discord = findDiscordCard();
    const reporting = findReportingCard();
    if (discord) polishDiscordCard(discord);
    if (reporting) polishReportingCard(reporting);
    polishFooter(null);
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
      const discord = findDiscordCard();
      const reporting = findReportingCard();
      if ((discord && discord.dataset[DISCORD_MARKER] !== '1') ||
          (reporting && reporting.dataset[REPORTING_MARKER] !== '1')) {
        schedule();
      }
    });
    observer.observe(view, {childList: true, subtree: true});
  }
})();