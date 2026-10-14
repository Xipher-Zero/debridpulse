/* DebridPulse application-session bootstrap and owner. Loaded before app.js.
 *
 * Owns the browser session and the authenticated same-origin request path.
 * It publishes that path as debridPulseAuth.fetch (session cookie, CSRF header
 * on mutations, session revalidation on 401 and 403); it does not replace the
 * native fetch. Every application HTTP call goes through debridPulseAuth.fetch.
 *
 * It is also the interface's ONE source of authority. The server prepends the
 * caller's authority to this bundle (window.debridPulseAuthority: role,
 * capabilities, account generation, display timezone -- the projection
 * /api/auth/session serves), and before any later script runs this owner
 * mounts every shell part declared as <template data-requires="capability">
 * whose capability the caller holds; every other one is discarded, never
 * constructed. Other owners ask debridPulseAuth.can(capability). Whenever the
 * session's role or generation stops matching it (an account_changed event, a
 * 403, the periodic check) the page reloads under the server's current
 * authority. The server enforces every action regardless. */
(() => {
  'use strict';

  const nativeFetch = window.fetch.bind(window);   // native API, never reassigned
  const mutatingMethods = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
  let csrfToken = '';
  let sessionState = null;
  let sessionRequest = null;
  let redirecting = false;
  const granted = window.debridPulseAuthority || {};
  const held = new Set(granted.capabilities || []);
  const authority = Object.freeze({...granted, capabilities: Object.freeze([...held])});

  function can(capability) {
    return held.has(String(capability || ''));
  }

  // May this caller destroy a transfer's work (remove it, cancel it, switch
  // or narrow its route)? The server's policy, read from what it issued: an
  // Admin may destroy any transfer; anyone else only where the transfer's own
  // server-given ``access.destroy`` says so. The server re-checks every act.
  function mayDestroy(item) {
    return can('administer') || item?.access?.destroy === true;
  }

  // Mount each declared shell part the caller may have; discard the rest.
  // A mounted part may itself declare parts, found on the next pass.
  function composeAuthorized(root = document) {
    let part = root.querySelector('template[data-requires]');
    while (part) {
      if (can(part.dataset.requires)) part.replaceWith(part.content.cloneNode(true));
      else part.remove();
      part = root.querySelector('template[data-requires]');
    }
  }

  // The authority this page was composed for no longer holds: start over.
  function authorityChanged(data) {
    if (!data || !authority.account_id) return false;
    return data.account_id !== authority.account_id
      || data.role !== authority.role
      || (data.account_generation || null) !== (authority.account_generation || null);
  }

  function roleLabel(role) {
    return {admin: 'Admin', user: 'User', readonly: 'ReadOnly User', integration: 'Integration'}[role] || '';
  }

  function currentReturnPath() {
    return window.location.pathname + window.location.search + window.location.hash;
  }

  function redirectToLogin() {
    if (redirecting || window.location.pathname === '/login') return;
    redirecting = true;
    window.location.assign('/login?next=' + encodeURIComponent(currentReturnPath()));
  }

  function isSameOrigin(input) {
    try {
      const raw = input instanceof Request ? input.url : String(input || '');
      return new URL(raw, window.location.href).origin === window.location.origin;
    } catch (_) {
      return false;
    }
  }

  // The shell provides #sidebar-bottom-stack (which wraps the sidebar footer) and
  // an empty #sidebar-auth-mount. This owner populates its own mount and marks the
  // stack's session state; it does not move or restyle any other owner's markup.
  function syncSidebarSessionUi(data) {
    const stack = document.getElementById('sidebar-bottom-stack');
    const mount = document.getElementById('sidebar-auth-mount');
    if (!stack || !mount) return;

    if (!data?.authenticated) {
      delete stack.dataset.session;
      mount.hidden = true;
      mount.replaceChildren();
      return;
    }

    stack.dataset.session = 'authenticated';
    mount.hidden = false;
    if (mount.querySelector('#sidebar-auth-row')) return;
    mount.appendChild(accountRow(data));

    const row = document.createElement('div');
    row.id = 'sidebar-auth-row';
    row.className = 'nav-item';
    row.setAttribute('role', 'button');
    row.setAttribute('tabindex', '0');
    row.setAttribute('aria-label', 'Log out of DebridPulse');
    row.style.flexShrink = '0';
    row.innerHTML = `
      <span class="icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <path d="M10 5H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h4"></path>
          <path d="M14 8l4 4-4 4"></path>
          <path d="M18 12H9"></path>
        </svg>
      </span>
      <span class="nav-label">Log Out</span>`;

    const activate = async () => {
      if (row.getAttribute('aria-disabled') === 'true') return;
      row.setAttribute('aria-disabled', 'true');
      const label = row.querySelector('.nav-label');
      if (label) label.textContent = 'Logging out…';
      try {
        const ok = await logoutSession();
        if (!ok) throw new Error('Logout failed');
      } catch (_) {
        row.setAttribute('aria-disabled', 'false');
        if (label) label.textContent = 'Log Out';
      }
    };

    row.addEventListener('click', activate);
    row.addEventListener('keydown', event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      activate();
    });
    mount.appendChild(row);
  }

  // The signed-in account and role, and the one way to the account's own
  // display name and password. It is the account's own surface, not Settings.
  function accountRow(data) {
    const row = document.createElement('div');
    row.id = 'sidebar-account-row';
    row.className = 'nav-item dp-sidebar-account';
    row.setAttribute('role', 'button');
    row.setAttribute('tabindex', '0');
    row.innerHTML = `
      <span class="icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="8" r="4"></circle><path d="M4 21a8 8 0 0 1 16 0"></path>
        </svg>
      </span>
      <span class="nav-label"><span class="dp-sidebar-account-copy"><span class="dp-sidebar-account-name"></span><span class="dp-sidebar-account-role"></span></span></span>`;
    row.querySelector('.dp-sidebar-account-name').textContent = String(data.display_name || 'My Account');
    row.querySelector('.dp-sidebar-account-role').textContent = roleLabel(data.role);
    row.setAttribute('aria-label', `My account: ${data.display_name || ''}, ${roleLabel(data.role)}`);
    const open = () => openAccountDialog(data);
    row.addEventListener('click', open);
    row.addEventListener('keydown', event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      open();
    });
    return row;
  }

  async function sendJson(method, url, body) {
    const response = await debridPulseFetch(url, {
      method, headers: {'Content-Type': 'application/json', 'Accept': 'application/json'}, body: JSON.stringify(body),
    });
    if (response.ok) return null;
    const payload = await response.json().catch(() => ({}));
    return String(payload.detail || 'The change was not saved.');
  }

  // The account's own profile, read from the account API when the dialog
  // opens: the email (and the verification its identity provider stated) is
  // shown, never edited here.
  const VERIFICATION = {verified: 'Verified', unverified: 'Not verified', unknown: 'Verification not stated'};

  async function openAccountDialog(data) {
    const modal = window.DPSettingsModal;
    if (!modal) return;
    const fields = {};
    let profile = {display_name: data.display_name || '', actual_name: '', email: '', email_verification: null};
    try {
      const response = await debridPulseFetch('/api/account', {headers: {'Accept': 'application/json'}});
      if (response.ok) profile = await response.json();
    } catch (_) { /* the session's own name still opens the dialog */ }
    const dialog = modal.open({
      title: 'My Account',
      acceptLabel: 'Save',
      className: 'dp-account-dialog',
      mount(body) {
        body.innerHTML = `
          <p class="dp-modal-message dp-account-identity"></p>
          <label class="dp-modal-field"><span class="form-label">Display Name</span>
            <input class="input" data-field="display_name" type="text" maxlength="256" autocomplete="nickname"></label>
          <label class="dp-modal-field"><span class="form-label">Actual Name</span>
            <input class="input" data-field="actual_name" type="text" maxlength="256" autocomplete="name"></label>
          <div class="dp-modal-field dp-account-email"><span class="form-label">Email</span>
            <span class="dp-account-email-value"></span></div>
          <fieldset class="dp-account-password">
            <legend class="form-label">Change Password</legend>
            <label class="dp-modal-field"><span class="form-label">Current Password</span>
              <input class="input" data-field="current" type="password" maxlength="4096" autocomplete="current-password"></label>
            <label class="dp-modal-field"><span class="form-label">New Password</span>
              <input class="input" data-field="next" type="password" maxlength="4096" autocomplete="new-password"></label>
            <label class="dp-modal-field"><span class="form-label">Confirm New Password</span>
              <input class="input" data-field="confirm" type="password" maxlength="4096" autocomplete="new-password"></label>
          </fieldset>`;
        const identity = body.querySelector('.dp-account-identity');
        identity.textContent = `Signed in as ${data.display_name || 'this account'} (${roleLabel(data.role)}). `
          + 'Changing your password signs you out everywhere.';
        body.querySelectorAll('[data-field]').forEach(input => { fields[input.dataset.field] = input; });
        fields.display_name.value = String(profile.display_name || data.display_name || '');
        fields.actual_name.value = String(profile.actual_name || '');
        const email = body.querySelector('.dp-account-email-value');
        email.textContent = profile.email
          ? `${profile.email}${VERIFICATION[profile.email_verification] ? ` (${VERIFICATION[profile.email_verification]})` : ''}`
          : 'None provided';
        return identity;
      },
    });
    dialog.closed.then(async result => {
      if (!result.accepted) return;
      const notice = text => window.toast(text, 'error');
      const name = fields.display_name.value.trim();
      if (name && name !== String(profile.display_name || data.display_name || '')) {
        const failure = await sendJson('PUT', '/api/account/display-name', {display_name: name});
        if (failure) return notice(failure);
      }
      const actual = fields.actual_name.value.trim();
      if (actual !== String(profile.actual_name || '')) {
        const failure = await sendJson('PUT', '/api/account/actual-name', {actual_name: actual});
        if (failure) return notice(failure);
      }
      if (fields.next.value || fields.current.value) {
        if (fields.next.value !== fields.confirm.value) return notice('The new passwords do not match.');
        const failure = await sendJson('PUT', '/api/account/password',
          {current_password: fields.current.value, new_password: fields.next.value});
        if (failure) return notice(failure);
        redirectToLogin();
        return;
      }
      window.location.reload();
    });
  }

  async function refreshSession({force = false} = {}) {
    if (sessionRequest && !force) return sessionRequest;
    sessionRequest = nativeFetch('/api/auth/session', {
      method: 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {'Accept': 'application/json'}
    }).then(async response => {
      if (response.status === 401) {
        csrfToken = '';
        sessionState = null;
        syncSidebarSessionUi(null);
        document.dispatchEvent(new CustomEvent('debridpulse:session-changed'));
        redirectToLogin();
        return null;
      }
      if (!response.ok) return sessionState;
      const data = await response.json();
      if (authorityChanged(data)) {
        window.location.reload();
        return data;
      }
      sessionState = data;
      csrfToken = String(data && data.csrf_token || '');
      syncSidebarSessionUi(data);
      document.dispatchEvent(new CustomEvent('debridpulse:session-changed'));
      return data;
    }).catch(() => sessionState).finally(() => {
      sessionRequest = null;
    });
    return sessionRequest;
  }

  async function debridPulseFetch(input, init) {
    const options = {...(init || {})};
    const requestMethod = input instanceof Request ? input.method : 'GET';
    const method = String(options.method || requestMethod || 'GET').toUpperCase();
    const sameOrigin = isSameOrigin(input);

    if (sameOrigin) {
      options.credentials = options.credentials || 'same-origin';
    }

    if (sameOrigin && mutatingMethods.has(method)) {
      await refreshSession();
      if (csrfToken) {
        const inherited = input instanceof Request ? input.headers : undefined;
        const headers = new Headers(options.headers || inherited || {});
        headers.set('X-CSRF-Token', csrfToken);
        options.headers = headers;
      }
    }

    const response = await nativeFetch(input, options);
    if (sameOrigin && (response.status === 401 || response.status === 403) && window.location.pathname !== '/login') {
      // A 401 from an application endpoint is not, by itself, proof that the
      // DebridPulse browser session expired, nor a 403 that the account's role
      // changed. Confirm against the canonical session endpoint: it signs out
      // an ended session and reloads under a changed authority.
      await refreshSession({force: true});
    }
    return response;
  }

  async function logoutSession() {
    const response = await debridPulseFetch('/api/auth/logout', {method: 'POST'});
    if (response.ok) {
      csrfToken = '';
      sessionState = null;
      syncSidebarSessionUi(null);
      window.location.assign('/login');
      return true;
    }
    return false;
  }

  window.debridPulseAuth = Object.freeze({
    fetch: debridPulseFetch,
    refreshSession,
    session: () => sessionState,
    logout: logoutSession,
    can,
    mayDestroy,
    authority,
  });

  composeAuthorized();

  refreshSession().catch(() => {});
  window.setInterval(() => refreshSession({force: true}).catch(() => {}), 60000);
})();
