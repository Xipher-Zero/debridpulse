/* DebridPulse v1.0.12 generic AUTH_REQUIRED browser interaction.
 *
 * Challenge metadata is the only authentication-policy input. This runtime
 * contains no protocol, provider, executor, URL-scheme, or native-error logic.
 * Authentication values remain in this modal session only and are discarded
 * when the challenge resolves, is cancelled, or the page reloads.
 */
(function () {
  'use strict';

  const STATUS_INPUT_REQUIRED = 'input_required';
  const REASON_AUTH_REQUIRED = 'auth_required';
  const METHOD_PASSWORD = 'username_password';
  const METHOD_PRIVATE_KEY = 'username_private_key';
  const SCAN_INTERVAL_MS = 3000;
  const OUTCOME_POLL_MS = 300;
  const MAX_KEY_BYTES = 256 * 1024;

  const state = {
    active: null,
    queue: [],
    overlay: null,
    previousFocus: null,
    busy: false,
    cancelling: false,
    scanPromise: null,
    scanTimer: null,
    scanScheduled: null,
    session: 0,
  };

  const text = value => String(value == null ? '' : value);
  const lower = value => text(value).trim().toLowerCase();

  function request(method, path, body, timeout) {
    if (typeof window.api !== 'function') {
      return Promise.reject(new Error('Application API client is unavailable'));
    }
    return window.api(method, path, body, timeout);
  }

  function isAuthTransfer(item) {
    const challenge = item && item.input_required;
    return !!item
      && lower(item.status) === STATUS_INPUT_REQUIRED
      && challenge
      && lower(challenge.reason) === REASON_AUTH_REQUIRED
      && text(challenge.id).trim();
  }

  function methodDescriptors(challenge) {
    const descriptors = Array.isArray(challenge && challenge.methods) ? challenge.methods : [];
    return new Map(descriptors
      .filter(item => item && typeof item.method === 'string')
      .map(item => [lower(item.method), item]));
  }

  function methodAdvertised(challenge, method) {
    return methodDescriptors(challenge).has(method);
  }

  function fieldRequired(challenge, method, fieldName) {
    const descriptor = methodDescriptors(challenge).get(method);
    const fields = Array.isArray(descriptor && descriptor.fields) ? descriptor.fields : [];
    const field = fields.find(item => lower(item && item.name) === fieldName);
    return !!(field && field.required);
  }

  function currentIdentity(challenge) {
    return `${text(challenge && challenge.id)}:${Number(challenge && challenge.generation || 0)}`;
  }

  function clearSecretObject(target) {
    if (!target) return;
    for (const key of ['username', 'password', 'passphrase', 'keyMaterial']) {
      if (Object.prototype.hasOwnProperty.call(target, key)) target[key] = '';
    }
  }

  function clearPayload(payload) {
    if (!payload) return;
    for (const key of ['username', 'password', 'private_key', 'passphrase']) {
      if (Object.prototype.hasOwnProperty.call(payload, key)) payload[key] = '';
    }
  }

  function clearSelectedKey(active) {
    if (!active) return;
    active.keyMaterial = '';
    active.keySelected = false;
  }

  function snapshotFields() {
    const active = state.active;
    if (!active || !state.overlay) return;
    const username = state.overlay.querySelector('[data-dp-auth-username]');
    const secret = state.overlay.querySelector('[data-dp-auth-secret]');
    active.username = username ? username.value : active.username;
    if (active.mode === 'key') active.passphrase = secret ? secret.value : active.passphrase;
    else active.password = secret ? secret.value : active.password;
  }

  function setInlineError(message, kind = 'error') {
    const error = state.overlay && state.overlay.querySelector('[data-dp-auth-error]');
    if (!error) return;
    error.textContent = text(message);
    error.hidden = !message;
    error.dataset.tone = kind === 'info' ? 'info' : 'error';
  }

  function setBusy(busy) {
    state.busy = !!busy;
    if (!state.overlay) return;
    const controls = state.overlay.querySelectorAll('input, button');
    controls.forEach(control => {
      control.disabled = state.busy || state.cancelling;
    });
    const dialog = state.overlay.querySelector('[data-dp-input-required-modal]');
    if (dialog) dialog.setAttribute('aria-busy', state.busy || state.cancelling ? 'true' : 'false');
    const continueButton = state.overlay.querySelector('[data-dp-auth-continue]');
    if (continueButton) {
      continueButton.textContent = state.busy ? 'Authenticating…' : 'Continue';
    }
  }

  function allowedModes(challenge) {
    return {
      password: methodAdvertised(challenge, METHOD_PASSWORD),
      key: methodAdvertised(challenge, METHOD_PRIVATE_KEY),
    };
  }

  function chooseMode(active) {
    const allowed = allowedModes(active.challenge);
    if (active.keySelected && allowed.key) return 'key';
    if (allowed.password) return 'password';
    return 'key';
  }

  function selectedKeyLabel(active) {
    return active.keySelected ? 'Key supplied · Replace key' : 'Select Keyfile';
  }

  function renderActive({focus = true, capture = true} = {}) {
    const active = state.active;
    if (!active) return;
    if (capture && state.overlay) snapshotFields();

    active.mode = chooseMode(active);
    const allowed = allowedModes(active.challenge);
    const secretLabel = active.mode === 'key' ? 'Passphrase' : 'Password';
    const secretValue = active.mode === 'key' ? active.passphrase : active.password;
    const secretRequired = active.mode === 'password'
      ? fieldRequired(active.challenge, METHOD_PASSWORD, 'password')
      : fieldRequired(active.challenge, METHOD_PRIVATE_KEY, 'passphrase');
    const showKey = allowed.key;

    if (!state.overlay) {
      state.previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      const overlay = document.createElement('div');
      overlay.className = 'dp-settings-confirm-overlay dp-auth-required-overlay';
      overlay.dataset.dpAuthRequiredOverlay = '1';
      document.body.appendChild(overlay);
      document.body.classList.add('dp-settings-confirm-open');
      document.addEventListener('keydown', handleDocumentKeydown, true);
      state.overlay = overlay;
    }

    const overlay = state.overlay;
    overlay.innerHTML = `
      <section class="dp-settings-confirm-dialog dp-auth-required-dialog"
               data-dp-input-required-modal
               data-dp-auth-transfer-id="${active.transferId}"
               role="dialog" aria-modal="true"
               aria-labelledby="dp-auth-required-title"
               aria-describedby="dp-auth-required-error">
        <header class="dp-settings-confirm-header dp-auth-required-header">
          <div class="dp-settings-confirm-title" id="dp-auth-required-title">Authentication Required</div>
        </header>
        <div class="dp-settings-confirm-body dp-auth-required-body">
          <label class="dp-auth-required-field">
            <span class="form-label">Username</span>
            <input class="input" type="text" autocomplete="off" spellcheck="false"
                   data-dp-auth-username>
          </label>
          <label class="dp-auth-required-field">
            <span class="form-label" data-dp-auth-secret-label>${secretLabel}</span>
            <input class="input" type="password" autocomplete="off"
                   ${secretRequired ? 'required' : ''}
                   data-dp-auth-secret>
          </label>
          <div class="dp-auth-required-error" id="dp-auth-required-error"
               data-dp-auth-error role="status" aria-live="polite" hidden></div>
        </div>
        <footer class="dp-settings-confirm-footer dp-auth-required-footer">
          <button class="btn btn-ghost" type="button" data-dp-auth-cancel>Cancel</button>
          ${showKey ? `
            <button class="btn btn-ghost dp-auth-required-key${active.keySelected ? ' is-selected' : ''}"
                    type="button" data-dp-auth-key
                    aria-pressed="${active.keySelected ? 'true' : 'false'}">
              ${selectedKeyLabel(active)}
            </button>` : ''}
          <button class="btn btn-primary" type="button" data-dp-auth-continue>Continue</button>
        </footer>
        ${showKey ? '<input type="file" data-dp-auth-key-input hidden>' : ''}
      </section>`;

    const username = overlay.querySelector('[data-dp-auth-username]');
    const secret = overlay.querySelector('[data-dp-auth-secret]');
    if (username) username.value = active.username || '';
    if (secret) secret.value = secretValue || '';

    overlay.onclick = event => {
      if (event.target === overlay && !state.busy && !state.cancelling) {
        event.preventDefault();
        cancelActive();
      }
    };
    overlay.onkeydown = handleKeydown;

    overlay.querySelector('[data-dp-auth-cancel]')?.addEventListener('click', cancelActive);
    overlay.querySelector('[data-dp-auth-continue]')?.addEventListener('click', submitActive);

    const keyButton = overlay.querySelector('[data-dp-auth-key]');
    const keyInput = overlay.querySelector('[data-dp-auth-key-input]');
    if (keyButton && keyInput) {
      keyButton.addEventListener('click', () => {
        if (state.busy || state.cancelling) return;
        keyInput.value = '';
        keyInput.click();
      });
      keyInput.addEventListener('change', () => ingestKey(keyInput));
    }

    setBusy(state.busy || state.cancelling);
    if (focus) {
      window.requestAnimationFrame(() => {
        const target = username || overlay.querySelector('button:not([disabled])');
        try { target?.focus(); } catch (_) {}
      });
    }
  }

  function handleDocumentKeydown(event) {
    if (event.key !== 'Escape' || !state.overlay) return;
    event.preventDefault();
    event.stopPropagation();
    if (!state.busy && !state.cancelling) cancelActive();
  }

  function handleKeydown(event) {
    if (!state.overlay || event.key !== 'Tab') return;
    const dialog = state.overlay.querySelector('[data-dp-input-required-modal]');
    const focusable = Array.from(dialog?.querySelectorAll(
      'button:not([disabled]), input:not([disabled]):not([type="file"])'
    ) || []).filter(node => !node.hidden);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function structurallyValidPrivateKey(value) {
    const source = text(value).trim();
    if (!source) return false;
    const pemPairs = [
      ['-----BEGIN OPENSSH PRIVATE KEY-----', '-----END OPENSSH PRIVATE KEY-----'],
      ['-----BEGIN RSA PRIVATE KEY-----', '-----END RSA PRIVATE KEY-----'],
      ['-----BEGIN EC PRIVATE KEY-----', '-----END EC PRIVATE KEY-----'],
      ['-----BEGIN DSA PRIVATE KEY-----', '-----END DSA PRIVATE KEY-----'],
      ['-----BEGIN PRIVATE KEY-----', '-----END PRIVATE KEY-----'],
      ['-----BEGIN ENCRYPTED PRIVATE KEY-----', '-----END ENCRYPTED PRIVATE KEY-----'],
      ['---- BEGIN SSH2 ENCRYPTED PRIVATE KEY ----', '---- END SSH2 ENCRYPTED PRIVATE KEY ----'],
    ];
    if (pemPairs.some(([begin, end]) => source.includes(begin) && source.includes(end))) return true;
    return /^PuTTY-User-Key-File-[23]:/m.test(source)
      && /^Private-Lines:\s*\d+/m.test(source)
      && /^Private-MAC:/m.test(source);
  }

  async function ingestKey(input) {
    const active = state.active;
    const file = input && input.files && input.files[0];
    if (!active || !file || state.busy || state.cancelling) return;

    const session = state.session;
    snapshotFields();
    const previous = active.keySelected ? active.keyMaterial : '';
    try {
      if (!Number.isFinite(file.size) || file.size <= 0) {
        throw new Error('empty');
      }
      if (file.size > MAX_KEY_BYTES) {
        throw new Error('large');
      }
      const material = await file.text();
      if (state.active !== active || state.session !== session || state.busy || state.cancelling) return;
      if (!structurallyValidPrivateKey(material)) {
        throw new Error('invalid');
      }
      // File reads are asynchronous. Capture edits made while the key was being
      // read before rebuilding the dialog around the selected key state.
      snapshotFields();
      active.keyMaterial = material;
      active.keySelected = true;
      active.mode = 'key';
      active.password = '';
      renderActive({focus: false, capture: false});
      setInlineError('');
      state.overlay?.querySelector('[data-dp-auth-secret]')?.focus();
    } catch (error) {
      if (!previous) {
        clearSelectedKey(active);
        if (allowedModes(active.challenge).password) active.mode = 'password';
      }
      setInlineError(
        error && error.message === 'empty'
          ? 'The selected key is empty.'
          : error && error.message === 'large'
            ? 'The selected key is too large.'
            : error && error.message === 'invalid'
              ? 'The selected key is not valid.'
              : 'The selected key could not be read.'
      );
      if (previous) {
        active.keyMaterial = previous;
        active.keySelected = true;
      }
    } finally {
      if (input) input.value = '';
    }
  }

  function validationError(active) {
    snapshotFields();
    const username = text(active.username).trim();
    if (fieldRequired(active.challenge, active.mode === 'key' ? METHOD_PRIVATE_KEY : METHOD_PASSWORD, 'username') && !username) {
      return 'Username is required.';
    }
    if (active.mode === 'key') {
      if (!active.keySelected || !active.keyMaterial) return 'Select a private key before continuing.';
      if (!methodAdvertised(active.challenge, METHOD_PRIVATE_KEY)) return 'Private-key authentication is no longer available.';
      return '';
    }
    if (!methodAdvertised(active.challenge, METHOD_PASSWORD)) return 'Password authentication is no longer available.';
    if (fieldRequired(active.challenge, METHOD_PASSWORD, 'password') && !active.password) return 'Password is required.';
    return '';
  }

  async function submitActive() {
    const active = state.active;
    if (!active || state.busy || state.cancelling) return;
    const error = validationError(active);
    if (error) {
      setInlineError(error);
      return;
    }

    const challenge = active.challenge;
    const identity = currentIdentity(challenge);
    const session = state.session;
    const method = active.mode === 'key' ? METHOD_PRIVATE_KEY : METHOD_PASSWORD;
    const payload = {
      challenge_id: text(challenge.id),
      method,
      username: active.username,
    };
    if (method === METHOD_PRIVATE_KEY) {
      payload.private_key = active.keyMaterial;
      if (active.passphrase) payload.passphrase = active.passphrase;
    } else {
      payload.password = active.password;
    }

    setInlineError('');
    setBusy(true);
    try {
      await request('POST', `/torrents/${active.transferId}/input`, payload, 30000);
      clearPayload(payload);
      await waitForOutcome(active.transferId, identity, session);
    } catch (_) {
      clearPayload(payload);
      if (state.session !== session || !state.active) return;
      setBusy(false);
      setInlineError('Authentication failed. Check your credentials and try again.');
      scheduleScan(0);
    }
  }

  async function waitForOutcome(transferId, submittedIdentity, session) {
    while (state.session === session && state.active && state.active.transferId === transferId) {
      let item;
      try {
        item = await request('GET', `/torrents/${transferId}`, null, 8000);
      } catch (_) {
        await new Promise(resolve => window.setTimeout(resolve, OUTCOME_POLL_MS));
        continue;
      }
      if (state.session !== session || !state.active) return;
      if (!isAuthTransfer(item)) {
        finishActive();
        scheduleScan(0);
        return;
      }
      const nextChallenge = item.input_required;
      if (currentIdentity(nextChallenge) !== submittedIdentity) {
        snapshotFields();
        applyChallengeUpdate(nextChallenge, {remoteRejected: true});
        setBusy(false);
        return;
      }
      await new Promise(resolve => window.setTimeout(resolve, OUTCOME_POLL_MS));
    }
  }

  function applyChallengeUpdate(challenge, {remoteRejected = false} = {}) {
    const active = state.active;
    if (!active) return;
    snapshotFields();

    const oldMode = active.mode;
    const oldPassword = active.password;
    const oldPassphrase = active.passphrase;
    const oldKeyMaterial = active.keyMaterial;
    const oldKeySelected = active.keySelected;

    active.challenge = challenge;
    const allowed = allowedModes(challenge);

    if (!allowed.key) clearSelectedKey(active);
    else if (oldKeySelected) {
      active.keyMaterial = oldKeyMaterial;
      active.keySelected = true;
    }

    if (oldMode === 'password' && allowed.password) active.password = oldPassword;
    else if (!allowed.password) active.password = '';

    if (oldMode === 'key' && allowed.key) active.passphrase = oldPassphrase;
    else if (!allowed.key) active.passphrase = '';

    active.mode = chooseMode(active);
    renderActive({focus: false, capture: false});
    setInlineError(
      remoteRejected
        ? 'Authentication failed. Check your credentials and try again.'
        : 'Authentication requirements changed. Review the available method and try again.',
      remoteRejected ? 'error' : 'info'
    );
  }

  async function cancelActive() {
    const active = state.active;
    if (!active || state.busy || state.cancelling) return;
    const session = state.session;
    state.cancelling = true;
    setBusy(true);
    setInlineError('');
    try {
      await request('POST', `/torrents/${active.transferId}/cancel`, null, 30000);
      if (state.session !== session) return;
      finishActive();
      scheduleScan(0);
    } catch (_) {
      if (state.session !== session || !state.active) return;
      state.cancelling = false;
      setBusy(false);
      setInlineError('The authentication request could not be cancelled. Try again.');
    }
  }

  function finishActive() {
    state.session += 1;
    state.busy = false;
    state.cancelling = false;
    if (state.active) clearSecretObject(state.active);
    state.active = null;

    document.removeEventListener('keydown', handleDocumentKeydown, true);
    if (state.overlay) {
      state.overlay.onkeydown = null;
      state.overlay.onclick = null;
      state.overlay.remove();
      state.overlay = null;
    }
    if (!document.querySelector('.dp-settings-confirm-overlay')) {
      document.body.classList.remove('dp-settings-confirm-open');
    }
    const previous = state.previousFocus;
    state.previousFocus = null;
    if (previous && previous.isConnected) {
      try { previous.focus(); } catch (_) {}
    }
    presentNext();
  }

  function newActive(item) {
    const challenge = item.input_required;
    const allowed = allowedModes(challenge);
    return {
      transferId: Number(item.id),
      challenge,
      username: '',
      password: '',
      passphrase: '',
      keyMaterial: '',
      keySelected: false,
      mode: allowed.password ? 'password' : 'key',
    };
  }

  function presentNext() {
    if (state.active || !state.queue.length) return;
    const item = state.queue.shift();
    state.active = newActive(item);
    state.session += 1;
    renderActive();
  }

  function reconcile(items) {
    const eligible = (Array.isArray(items) ? items : [])
      .filter(isAuthTransfer)
      .sort((a, b) => Number(a.id) - Number(b.id));
    const byId = new Map(eligible.map(item => [Number(item.id), item]));

    if (state.active) {
      const item = byId.get(state.active.transferId);
      if (!item) {
        finishActive();
      } else if (!state.busy && currentIdentity(item.input_required) !== currentIdentity(state.active.challenge)) {
        applyChallengeUpdate(item.input_required);
      }
    }

    const activeId = state.active && state.active.transferId;
    state.queue = eligible.filter(item => Number(item.id) !== activeId);
    presentNext();
  }

  async function scanNow() {
    if (state.scanPromise || document.visibilityState === 'hidden') return state.scanPromise;
    state.scanPromise = request('GET', '/torrents?status=input_required&limit=5000', null, 8000)
      .then(result => reconcile(result && result.items))
      .catch(() => {})
      .finally(() => { state.scanPromise = null; });
    return state.scanPromise;
  }

  function scheduleScan(delay = 40) {
    if (state.scanScheduled) window.clearTimeout(state.scanScheduled);
    state.scanScheduled = window.setTimeout(() => {
      state.scanScheduled = null;
      scanNow();
    }, delay);
  }

  function bootstrap() {
    if (typeof window.api !== 'function') {
      window.setTimeout(bootstrap, 50);
      return;
    }
    scheduleScan(0);
    state.scanTimer = window.setInterval(scanNow, SCAN_INTERVAL_MS);
    document.addEventListener('debridpulse:downloads-rendered', () => scheduleScan(0));
    document.addEventListener('debridpulse:dashboard-recent-rendered', () => scheduleScan(0));
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') scheduleScan(0);
    });
    window.DPAuthRequired = Object.freeze({scan: scanNow});
  }

  bootstrap();
})();
