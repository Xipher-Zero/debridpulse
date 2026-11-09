/* DebridPulse generic INPUT_REQUIRED browser interaction.
 *
 * Challenge metadata is the only authentication-policy input. This runtime
 * contains no protocol, provider, executor, URL-scheme, or native-error logic.
 * A server-identity challenge is the same challenge with non-secret facts to
 * compare; submitting it accepts that identity for this execution only.
 * Authentication values remain in this modal session only and are discarded
 * when the challenge resolves, is cancelled, or the page reloads.
 */
(function () {
  'use strict';

  const STATUS_INPUT_REQUIRED = 'input_required';
  const REASON_AUTH_REQUIRED = 'auth_required';
  const REASON_SERVER_IDENTITY = 'server_identity_required';
  const FACT_SERVER = 'server_host';
  const FACT_ALGORITHM = 'server_identity_algorithm';
  const FACT_FINGERPRINT = 'server_identity_fingerprint';
  const METHOD_PASSWORD = 'username_password';
  const METHOD_PRIVATE_KEY = 'username_private_key';
  // Confirm the observed identity only: the backend already holds usable
  // credentials for this transfer, so none are asked for (or sent) again.
  const METHOD_IDENTITY = 'server_identity';
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
  // Words are the catalog's (auth_required.*). Markup carries its keys, so a
  // language change re-words the open dialog in place (ui-i18n.js render):
  // nothing is rebuilt, and typed values and focus stay.
  const t = (key, values) => window.DPI18n.t(key, values);
  const keyed = key => `data-i18n="${key}">${escapeText(t(key))}`;
  function escapeText(value) {
    return text(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function request(method, path, body, timeout) {
    if (typeof window.api !== 'function') {
      return Promise.reject(new Error('Application API client is unavailable'));
    }
    return window.api(method, path, body, timeout);
  }

  function challengeFact(challenge, name) {
    const facts = Array.isArray(challenge && challenge.facts) ? challenge.facts : [];
    const fact = facts.find(item => item && item.name === name);
    return fact ? text(fact.value).trim() : '';
  }

  function identityChallenge(challenge) {
    return lower(challenge && challenge.reason) === REASON_SERVER_IDENTITY;
  }

  function presentable(challenge) {
    const reason = lower(challenge && challenge.reason);
    if (reason === REASON_AUTH_REQUIRED) return true;
    return reason === REASON_SERVER_IDENTITY
      && [FACT_SERVER, FACT_ALGORITHM, FACT_FINGERPRINT].every(name => challengeFact(challenge, name));
  }

  function isAuthTransfer(item) {
    const challenge = item && item.input_required;
    return !!item
      && lower(item.status) === STATUS_INPUT_REQUIRED
      && challenge
      && presentable(challenge)
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

  // Whether two questions ask about the same source of a transfer (a refused
  // answer or a follow-up requirement), rather than one source's question
  // retiring and another source of the same transfer asking its own.
  function sameSubject(left, right) {
    return text(left && left.subject) === text(right && right.subject);
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

  // ``key``: the message's catalog key ('' for none); kept, so a language
  // change says it again.
  function setInlineError(key, kind = 'error') {
    state.errorKey = key || '';
    const error = state.overlay && state.overlay.querySelector('[data-dp-auth-error]');
    if (!error) return;
    error.textContent = key ? t(key) : '';
    error.hidden = !key;
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
      const identity = !!(state.active && identityChallenge(state.active.challenge));
      continueButton.textContent = t(state.busy
        ? (identity ? 'auth_required.verifying' : 'auth_required.authenticating')
        : (identity ? 'auth_required.verify_continue' : 'auth_required.continue'));
    }
  }

  function allowedModes(challenge) {
    return {
      password: methodAdvertised(challenge, METHOD_PASSWORD),
      key: methodAdvertised(challenge, METHOD_PRIVATE_KEY),
      identity: methodAdvertised(challenge, METHOD_IDENTITY),
    };
  }

  function chooseMode(active) {
    const allowed = allowedModes(active.challenge);
    if (allowed.identity && !allowed.password && !allowed.key) return 'identity';
    if (active.keySelected && allowed.key) return 'key';
    if (allowed.password) return 'password';
    return 'key';
  }

  function selectedKeyLabel(active) {
    return t(active.keySelected ? 'auth_required.key_replace' : 'auth_required.key_select');
  }

  function renderActive({focus = true, capture = true} = {}) {
    const active = state.active;
    if (!active) return;
    if (capture && state.overlay) snapshotFields();

    active.mode = chooseMode(active);
    const allowed = allowedModes(active.challenge);
    const secretLabel = t(active.mode === 'key' ? 'auth_required.passphrase' : 'auth_required.password');
    const secretValue = active.mode === 'key' ? active.passphrase : active.password;
    const secretRequired = active.mode === 'password'
      ? fieldRequired(active.challenge, METHOD_PASSWORD, 'password')
      : fieldRequired(active.challenge, METHOD_PRIVATE_KEY, 'passphrase');
    const showKey = allowed.key;
    const identity = identityChallenge(active.challenge);
    // The server asking, when it is not the source's own (a server the
    // source moved to): the backend's safe origin, shown as text only.
    const authority = text(active.challenge && active.challenge.authority).trim();

    if (!state.overlay) {
      // A challenge presented while the previous one is still fading out
      // inherits that one's return target, not the leaving overlay.
      state.previousFocus = window.DPMotion ? window.DPMotion.focusOrigin()
        : document.activeElement instanceof HTMLElement ? document.activeElement : null;
      const overlay = document.createElement('div');
      overlay.className = 'dp-modal-overlay dp-auth-required-overlay';
      overlay.dataset.dpAuthRequiredOverlay = '1';
      const active = document.activeElement;
      const handedOver = !!(active && active.closest && active.closest('.dp-auth-required-overlay.is-closing'));
      if (window.DPMotion) window.DPMotion.enter(overlay, '.dp-auth-required-overlay');
      document.body.appendChild(overlay);
      if (handedOver) {
        // Taking over from a challenge that held focus while leaving: this
        // one holds it until its own field takes it, so the page never has it.
        overlay.tabIndex = -1;
        overlay.addEventListener('blur', () => overlay.removeAttribute('tabindex'), {once: true});
        overlay.focus({preventScroll: true});
      }
      document.body.classList.add('dp-modal-open');
      document.addEventListener('keydown', handleDocumentKeydown, true);
      state.overlay = overlay;
    }

    const overlay = state.overlay;
    overlay.innerHTML = `
      <section class="dp-modal-dialog dp-auth-required-dialog"
               data-dp-input-required-modal
               data-dp-auth-transfer-id="${active.transferId}"
               role="dialog" aria-modal="true"
               aria-labelledby="dp-auth-required-title"
               aria-describedby="${authority ? 'dp-auth-required-authority ' : ''}${identity ? 'dp-auth-required-identity-note ' : ''}dp-auth-required-error">
        <header class="dp-modal-header dp-auth-required-header">
          <div class="dp-modal-title" id="dp-auth-required-title" ${keyed(identity ? 'auth_required.title_identity' : 'auth_required.title')}</div>
        </header>
        <div class="dp-modal-body dp-auth-required-body">
          ${authority ? `
            <p class="dp-auth-required-identity-note" id="dp-auth-required-authority" data-dp-auth-authority></p>` : ''}
          ${identity ? `
            <div class="dp-auth-required-identity" data-dp-identity>
              <div class="dp-auth-required-identity-server" data-dp-identity-host></div>
              <div class="form-label" data-dp-identity-algorithm></div>
              <code class="dp-auth-required-fingerprint" data-dp-identity-fingerprint></code>
              <p class="dp-auth-required-identity-note" id="dp-auth-required-identity-note" ${keyed('auth_required.identity_note')}</p>
            </div>` : ''}
          ${active.mode === 'identity' ? `
            <p class="dp-auth-required-identity-note" data-dp-auth-credentials-held ${keyed('auth_required.credentials_held')}</p>` : `
          <label class="dp-auth-required-field">
            <span class="form-label" ${keyed('auth_required.username')}</span>
            <input class="input" type="text" autocomplete="off" spellcheck="false"
                   data-dp-auth-username>
          </label>
          <label class="dp-auth-required-field">
            <span class="form-label" data-dp-auth-secret-label>${escapeText(secretLabel)}</span>
            <input class="input" type="password" autocomplete="off"
                   ${secretRequired ? 'required' : ''}
                   data-dp-auth-secret>
          </label>`}
          <div class="dp-auth-required-error" id="dp-auth-required-error"
               data-dp-auth-error role="status" aria-live="polite" hidden></div>
        </div>
        <footer class="dp-modal-footer dp-auth-required-footer">
          ${window.debridPulseAuth?.mayDestroy(active) === true ? `<button class="btn btn-ghost" type="button" data-dp-auth-cancel ${keyed('common.cancel')}</button>` : ''}
          ${showKey ? `
            <button class="btn btn-ghost dp-auth-required-key${active.keySelected ? ' is-selected' : ''}"
                    type="button" data-dp-auth-key
                    aria-pressed="${active.keySelected ? 'true' : 'false'}">
              ${escapeText(selectedKeyLabel(active))}
            </button>` : ''}
          <button class="btn btn-primary" type="button" data-dp-auth-continue>${escapeText(t(identity ? 'auth_required.verify_continue' : 'auth_required.continue'))}</button>
        </footer>
        ${showKey ? '<input type="file" data-dp-auth-key-input hidden>' : ''}
      </section>`;

    const username = overlay.querySelector('[data-dp-auth-username]');
    const secret = overlay.querySelector('[data-dp-auth-secret]');
    if (authority) overlay.querySelector('[data-dp-auth-authority]').textContent = t('auth_required.sign_in_to', {authority});
    if (username) username.value = active.username || '';
    if (secret) secret.value = secretValue || '';
    if (identity) {
      // Challenge facts are server-supplied text: assigned, never parsed as markup.
      overlay.querySelector('[data-dp-identity-host]').textContent = challengeFact(active.challenge, FACT_SERVER);
      overlay.querySelector('[data-dp-identity-algorithm]').textContent =
        t('auth_required.fingerprint_of', {algorithm: challengeFact(active.challenge, FACT_ALGORITHM).toUpperCase()});
      overlay.querySelector('[data-dp-identity-fingerprint]').textContent = challengeFact(active.challenge, FACT_FINGERPRINT);
    }

    // A blocking required-input surface: a click on the backdrop (the whole
    // page outside the dialog) is inert. Only the explicit Cancel control
    // cancels the transfer.
    overlay.onclick = event => {
      if (event.target === overlay) event.preventDefault();
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
    // Escape is held by the dialog -- nothing beneath it reacts -- and is
    // inert: passive dismissal never cancels the transfer. A dialog fading
    // out still holds it until it is gone.
    if (event.key !== 'Escape' || !document.querySelector('.dp-auth-required-overlay')) return;
    event.preventDefault();
    event.stopPropagation();
  }

  /* Enter is the keyboard form of Continue only where the credential intent is
   * unambiguous. In the ordinary username/password form that means BOTH fields
   * hold something; otherwise the existing missing-credential message is shown,
   * nothing is sent and the dialog stays open -- the explicit Continue button
   * remains the deliberate way to proceed with a partial credential set (a
   * passwordless account, say). The private-key form keeps its own required
   * fields, which the one validated submission already enforces. */
  function handleEnter(event) {
    const field = event.target;
    if (!(field instanceof HTMLInputElement) || !field.matches('[data-dp-auth-username], [data-dp-auth-secret]')) return;
    if (event.isComposing || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return;
    event.preventDefault();
    const active = state.active;
    if (!active || state.busy || state.cancelling) return;
    if (active.mode === 'password') {
      snapshotFields();
      if (!text(active.username).trim()) {
        setInlineError('auth_required.error.username_required');
        return;
      }
      if (!active.password) {
        setInlineError('auth_required.error.password_required');
        return;
      }
    }
    void submitActive();
  }

  function handleKeydown(event) {
    if (!state.overlay) return;
    if (event.key === 'Enter') {
      handleEnter(event);
      return;
    }
    if (event.key !== 'Tab') return;
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
    const replacingKey = active.mode === 'key' && active.keySelected;
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
      // read before changing selected-key state. Replacing a key while already
      // in key mode does not require rebuilding the form; keeping the existing
      // DOM prevents in-flight field edits from racing a no-op mode transition.
      snapshotFields();
      active.keyMaterial = material;
      active.keySelected = true;
      active.mode = 'key';
      active.password = '';
      if (!replacingKey) {
        renderActive({focus: false, capture: false});
      }
      setInlineError('');
      state.overlay?.querySelector('[data-dp-auth-secret]')?.focus();
    } catch (error) {
      if (!previous) {
        clearSelectedKey(active);
        if (allowedModes(active.challenge).password) active.mode = 'password';
      }
      setInlineError(
        error && error.message === 'empty'
          ? 'auth_required.error.key_empty'
          : error && error.message === 'large'
            ? 'auth_required.error.key_too_large'
            : error && error.message === 'invalid'
              ? 'auth_required.error.key_invalid'
              : 'auth_required.error.key_unreadable'
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
    if (active.mode === 'identity') {
      return methodAdvertised(active.challenge, METHOD_IDENTITY) ? '' : 'auth_required.error.identity_unavailable';
    }
    const username = text(active.username).trim();
    if (fieldRequired(active.challenge, active.mode === 'key' ? METHOD_PRIVATE_KEY : METHOD_PASSWORD, 'username') && !username) {
      return 'auth_required.error.username_required';
    }
    if (active.mode === 'key') {
      if (!active.keySelected || !active.keyMaterial) return 'auth_required.error.key_missing';
      if (!methodAdvertised(active.challenge, METHOD_PRIVATE_KEY)) return 'auth_required.error.key_unavailable';
      return '';
    }
    if (!methodAdvertised(active.challenge, METHOD_PASSWORD)) return 'auth_required.error.password_unavailable';
    if (fieldRequired(active.challenge, METHOD_PASSWORD, 'password') && !active.password) return 'auth_required.error.password_required';
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
    const method = active.mode === 'identity' ? METHOD_IDENTITY
      : active.mode === 'key' ? METHOD_PRIVATE_KEY : METHOD_PASSWORD;
    const payload = {challenge_id: text(challenge.id), method};
    if (method !== METHOD_IDENTITY) payload.username = active.username;
    if (method === METHOD_IDENTITY) {
      // Nothing secret is sent: the identity decision is the whole answer.
    } else if (method === METHOD_PRIVATE_KEY) {
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
      setInlineError('auth_required.error.failed');
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
        if (!sameSubject(nextChallenge, state.active.challenge)) {
          // The answered question retired; another source asks its own now.
          finishActive();
          scheduleScan(0);
          return;
        }
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
        ? 'auth_required.error.failed'
        : 'auth_required.error.requirements_changed',
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
      setInlineError('auth_required.error.cancel_failed');
    }
  }

  function finishActive() {
    state.session += 1;
    state.busy = false;
    state.cancelling = false;
    if (state.active) clearSecretObject(state.active);
    state.active = null;

    const overlay = state.overlay;
    const previous = state.previousFocus;
    state.overlay = null;
    state.previousFocus = null;
    if (overlay) {
      overlay.onkeydown = null;
      overlay.onclick = null;
      // It fades away in place, still covering the page, holding the keyboard
      // and the page lock; only once it is gone are they released and focus
      // returned, once (ui-motion.js leave/restore). A next challenge shown
      // meanwhile replaces it at once and keeps them.
      const leaving = window.DPMotion
        ? window.DPMotion.leave(overlay, overlay.querySelector('.dp-modal-dialog'), previous) : Promise.resolve();
      leaving.then(() => {
        if (window.DPMotion) window.DPMotion.restore(overlay, previous);
        else {
          overlay.remove();
          if (previous && previous.isConnected) { try { previous.focus(); } catch (_) {} }
        }
        if (!document.querySelector('.dp-modal-overlay')) document.body.classList.remove('dp-modal-open');
        if (!state.overlay) document.removeEventListener('keydown', handleDocumentKeydown, true);
      });
    }
    presentNext();
  }

  function newActive(item) {
    const challenge = item.input_required;
    const allowed = allowedModes(challenge);
    return {
      transferId: Number(item.id),
      challenge,
      access: item.access || {},
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
        if (sameSubject(item.input_required, state.active.challenge)) applyChallengeUpdate(item.input_required);
        else finishActive();
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

  // A language change: the open dialog's keyed words are re-rendered in place
  // (ui-i18n.js); the words that follow its state are said again here. Typed
  // values, focus, the busy state and the answer in flight are untouched.
  document.addEventListener('debridpulse:locale-changed', () => {
    const active = state.active, overlay = state.overlay;
    if (!active || !overlay) return;
    const secretLabel = overlay.querySelector('[data-dp-auth-secret-label]');
    if (secretLabel) secretLabel.textContent = t(active.mode === 'key' ? 'auth_required.passphrase' : 'auth_required.password');
    const keyButton = overlay.querySelector('[data-dp-auth-key]');
    if (keyButton) keyButton.textContent = selectedKeyLabel(active);
    const authority = text(active.challenge && active.challenge.authority).trim();
    const authorityNode = overlay.querySelector('[data-dp-auth-authority]');
    if (authority && authorityNode) authorityNode.textContent = t('auth_required.sign_in_to', {authority});
    const algorithm = overlay.querySelector('[data-dp-identity-algorithm]');
    if (algorithm) algorithm.textContent = t('auth_required.fingerprint_of', {algorithm: challengeFact(active.challenge, FACT_ALGORITHM).toUpperCase()});
    const error = overlay.querySelector('[data-dp-auth-error]');
    if (error && state.errorKey) error.textContent = t(state.errorKey);
    setBusy(state.busy || state.cancelling);
  });

  function bootstrap() {
    // Answering a challenge is an operation; a caller without it is never asked.
    if (!window.debridPulseAuth?.can('transfers.operate')) return;
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
