/* DebridPulse v1.0.11 live OIDC callback draft relationship. */
(function () {
  'use strict';

  const CALLBACK_PATH = '/auth/oidc/callback';
  const EMPTY_COPY = 'Set Public Base URL to display the Callback URL.';
  const CALLBACK_HINT = "Copy this exact URL into your identity provider's redirect/callback URI configuration.";
  let scheduled = false;

  function textOf(node) {
    return String(node?.textContent || '').trim();
  }

  function authenticationPanel() {
    return document.querySelector('#view-settings [data-panel="authentication"]');
  }

  function fieldByLabel(host, label) {
    return Array.from(host?.querySelectorAll('.dp-settings-field') || []).find(function (field) {
      return textOf(field.querySelector(':scope > .form-label')) === label;
    }) || null;
  }

  function publicBaseInput() {
    return authenticationPanel()?.querySelector('#dp-auth-public-base-url') || null;
  }

  function callbackField() {
    return fieldByLabel(authenticationPanel(), 'OIDC Callback URL');
  }

  function callbackFromPublicBase(value) {
    const raw = String(value ?? '').trim();
    if (!raw) return '';

    let parsed;
    try {
      parsed = new URL(raw);
    } catch (_) {
      return '';
    }

    if (parsed.protocol !== 'https:' || !parsed.hostname) return '';
    if (parsed.username || parsed.password || parsed.search || parsed.hash) return '';
    if (parsed.pathname !== '/' && parsed.pathname !== '') return '';

    // Match the backend's effective_public_base_url()/oidc_callback_url()
    // contract exactly: validate the origin, preserve the operator's literal
    // origin spelling/port, remove one optional trailing slash, append the
    // application-owned callback route.
    const origin = raw.endsWith('/') ? raw.slice(0, -1) : raw;
    return origin + CALLBACK_PATH;
  }

  function notify(message, kind) {
    if (typeof window.toast === 'function') {
      window.toast(String(message), kind || 'info');
      return;
    }
    try {
      if (typeof toast === 'function') toast(String(message), kind || 'info');
    } catch (_) {}
  }

  function ensureHint(field) {
    let hint = field?.querySelector(':scope > .form-hint') || null;
    if (!field) return;
    if (!hint) {
      hint = document.createElement('span');
      hint.className = 'form-hint';
      field.appendChild(hint);
    }
    if (textOf(hint) !== CALLBACK_HINT) hint.textContent = CALLBACK_HINT;
  }

  function ensureCallbackControl(field) {
    if (!field) return null;

    let input = field.querySelector('#dp-auth-oidc-callback') || field.querySelector(':scope > input.input');
    if (!input) input = field.querySelector('input.input');
    if (!input) return null;

    input.id = 'dp-auth-oidc-callback';
    input.readOnly = true;
    input.setAttribute('aria-readonly', 'true');
    input.setAttribute('autocomplete', 'off');

    const label = field.querySelector(':scope > .form-label');
    if (label) label.htmlFor = input.id;

    let control = input.closest('.dp-settings-inline-field');
    if (!control || control.parentElement !== field) {
      control = document.createElement('div');
      control.className = 'dp-settings-inline-field dp-settings-oidc-callback-control';
      input.before(control);
      control.appendChild(input);
    } else {
      control.classList.add('dp-settings-oidc-callback-control');
    }

    let button = control.querySelector('button[data-action="copy-oidc-callback"]');
    if (!button) {
      button = document.createElement('button');
      button.className = 'btn btn-ghost btn-sm';
      button.type = 'button';
      button.dataset.action = 'copy-oidc-callback';
      button.setAttribute('aria-label', 'Copy OIDC Callback URL');
      button.textContent = 'Copy';
      control.appendChild(button);
    }

    field.classList.add('dp-settings-auth-callback-field', 'dp-settings-oidc-sandwich');
    ensureHint(field);
    return {input, button};
  }

  function updatePreview() {
    const source = publicBaseInput();
    const field = callbackField();
    if (!source || !field) return false;

    const control = ensureCallbackControl(field);
    if (!control) return false;

    const callback = callbackFromPublicBase(source.value);
    if (callback) {
      if (control.input.value !== callback) control.input.value = callback;
      if (control.input.placeholder) control.input.placeholder = '';
      control.button.disabled = false;
      field.classList.remove('is-callback-unavailable');
    } else {
      if (control.input.value) control.input.value = '';
      if (control.input.placeholder !== EMPTY_COPY) control.input.placeholder = EMPTY_COPY;
      control.button.disabled = true;
      field.classList.add('is-callback-unavailable');
    }

    ensureHint(field);
    return true;
  }

  function bindPublicBaseDraft(source) {
    if (!source || source.dataset.dpOidcCallbackDraftBound === '1') return;
    source.dataset.dpOidcCallbackDraftBound = '1';
    source.addEventListener('input', updatePreview);
    source.addEventListener('change', updatePreview);
  }

  async function copyCallback() {
    const source = publicBaseInput();
    const field = callbackField();
    const input = field?.querySelector('#dp-auth-oidc-callback') || null;
    const callback = callbackFromPublicBase(source?.value);
    if (!input || !callback) return;

    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard API unavailable');
      await navigator.clipboard.writeText(callback);
      notify('OIDC callback URL copied', 'success');
      return;
    } catch (_) {
      try {
        input.focus();
        input.select();
        if (document.execCommand('copy')) {
          notify('OIDC callback URL copied', 'success');
          return;
        }
      } catch (_) {}
    }

    input.focus();
    input.select();
    notify('Select and copy the callback URL manually', 'info');
  }

  function polish() {
    scheduled = false;
    const source = publicBaseInput();
    const field = callbackField();
    if (!source || !field) return;
    bindPublicBaseDraft(source);
    updatePreview();
  }

  function schedule() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(polish);
  }

  const view = document.getElementById('view-settings');
  if (!view) return;

  if (view.dataset.dpOidcCallbackCopyBound !== '1') {
    view.dataset.dpOidcCallbackCopyBound = '1';
    view.addEventListener('click', function (event) {
      const button = event.target.closest('button[data-action="copy-oidc-callback"]');
      if (!button || !view.contains(button) || button.disabled) return;
      void copyCallback();
    });
  }

  const observer = new MutationObserver(function (mutations) {
    if (!mutations.some(function (mutation) { return mutation.type === 'childList'; })) return;
    schedule();
  });
  observer.observe(view, {childList: true, subtree: true});

  schedule();
})();
