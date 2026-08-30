/* DebridPulse v1.0.11 Authentication mini-polish. */
(function () {
  'use strict';

  const OIDC_CALLBACK_HINT = "Copy this exact URL into your identity provider's redirect/callback URI configuration.";
  let scheduled = false;

  function textOf(node) {
    return String(node?.textContent || '').trim();
  }

  function authenticationPanel() {
    return document.querySelector('#view-settings [data-panel="authentication"]');
  }

  function cardByTitle(title) {
    const panel = authenticationPanel();
    if (!panel) return null;
    return Array.from(panel.querySelectorAll('.dp-settings-card')).find(function (card) {
      return textOf(card.querySelector(':scope > .card-header > .card-title')) === title;
    }) || null;
  }

  function authenticationStatusCard() {
    return cardByTitle('Authentication Status');
  }

  function oidcCard() {
    return cardByTitle('OpenID Connect');
  }

  function fieldByLabel(card, label) {
    return Array.from(card?.querySelectorAll('.dp-settings-field') || []).find(function (field) {
      return textOf(field.querySelector(':scope > .form-label')) === label;
    }) || null;
  }

  function ensureHint(field, copy) {
    if (!field) return;
    let hint = field.querySelector(':scope > .form-hint');
    if (!hint) {
      hint = document.createElement('span');
      hint.className = 'form-hint';
      field.appendChild(hint);
    }
    if (textOf(hint) !== copy) hint.textContent = copy;
  }

  function alignSandwichToInputText(field) {
    const control = field?.querySelector('input, textarea, select') || null;
    if (!field || !control) return;
    const style = window.getComputedStyle(control);
    const inset = style.paddingInlineStart || style.paddingLeft || '0px';
    field.style.setProperty('--dp-settings-auth-input-text-inset', inset);
  }

  function polishLifetime(card) {
    const input = card?.querySelector('input[data-setting="auth_session_lifetime_hours"]') || null;
    const field = input?.closest('.dp-settings-field') || null;
    if (!input || !field) return false;

    field.classList.add('dp-settings-auth-session-lifetime-polished');
    input.setAttribute('aria-label', 'Browser Session Lifetime in hours');

    if (!input.closest('.dp-settings-auth-duration-control')) {
      const control = document.createElement('span');
      control.className = 'dp-settings-auth-duration-control';
      input.before(control);
      control.appendChild(input);

      const unit = document.createElement('span');
      unit.className = 'dp-settings-auth-duration-unit';
      unit.setAttribute('aria-hidden', 'true');
      unit.textContent = 'hours';
      control.appendChild(unit);
    }

    alignSandwichToInputText(field);
    return true;
  }

  function polishCallback(statusCard) {
    const oidc = oidcCard();
    const publicBaseField = oidc?.querySelector('#dp-auth-public-base-url')?.closest('.dp-settings-field') || null;
    const field = fieldByLabel(statusCard, 'OIDC Callback URL') || fieldByLabel(oidc, 'OIDC Callback URL');
    if (!oidc || !publicBaseField || !field) return false;

    field.classList.add('dp-settings-auth-callback-field');
    ensureHint(field, OIDC_CALLBACK_HINT);

    if (field.parentElement !== publicBaseField.parentElement || field.previousElementSibling !== publicBaseField) {
      publicBaseField.after(field);
    }

    alignSandwichToInputText(field);
    return true;
  }

  function polish() {
    scheduled = false;
    const card = authenticationStatusCard();
    if (!card) return;
    polishLifetime(card);
    polishCallback(card);
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
      schedule();
    });
    observer.observe(view, {childList: true, subtree: true});
  }
})();
