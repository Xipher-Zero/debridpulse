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
  //
  // The session's ONE sidebar row: Log Out at its start, and at its end the
  // signed-in account (Display Name over Role) that opens My Account. Two
  // separate controls in one row, with the elastic space between them
  // belonging to neither.
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

    const row = document.createElement('div');
    row.id = 'sidebar-auth-row';
    row.className = 'nav-item dp-sidebar-session-row';
    row.style.flexShrink = '0';
    row.innerHTML = `
      <button type="button" class="dp-sidebar-logout" aria-label="Log out of DebridPulse" title="Log Out">
        <span class="icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M10 5H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h4"></path>
            <path d="M14 8l4 4-4 4"></path>
            <path d="M18 12H9"></path>
          </svg>
        </span>
        <span class="nav-label">Log Out</span>
      </button>
      <span class="dp-sidebar-session-space" aria-hidden="true"></span>
      <button type="button" class="dp-sidebar-profile" title="My Account">
        <span class="nav-label"><span class="dp-sidebar-account-copy"><span class="dp-sidebar-account-name"></span><span class="dp-sidebar-account-role"></span></span></span>
        <span class="icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="8" r="4"></circle><path d="M4 21a8 8 0 0 1 16 0"></path>
          </svg>
        </span>
      </button>`;

    const logout = row.querySelector('.dp-sidebar-logout');
    logout.addEventListener('click', async () => {
      if (logout.getAttribute('aria-disabled') === 'true') return;
      logout.setAttribute('aria-disabled', 'true');
      const label = logout.querySelector('.nav-label');
      if (label) label.textContent = 'Logging out…';
      try {
        const ok = await logoutSession();
        if (!ok) throw new Error('Logout failed');
      } catch (_) {
        logout.setAttribute('aria-disabled', 'false');
        if (label) label.textContent = 'Log Out';
      }
    });

    const profile = row.querySelector('.dp-sidebar-profile');
    const showAccount = name => {
      const display = String(name || '');
      profile.querySelector('.dp-sidebar-account-name').textContent = display;
      profile.querySelector('.dp-sidebar-account-role').textContent = roleLabel(data.role);
      profile.setAttribute('aria-label', display ? `My account: ${display}, ${roleLabel(data.role)}`
        : `My account, ${roleLabel(data.role)}`);
      // A name too long for the row is shown in full here.
      profile.title = `${display ? `${display} ` : ''}(${roleLabel(data.role)}): My Account`;
    };
    showAccount('');
    // The account's own Display Name. The session's name is its sign-in (an
    // OIDC ``name`` claim, a username) and is never shown first; it stands in
    // only when the account's profile cannot be read.
    readProfile().then(own => showAccount(own?.display_name || data.display_name || 'My Account'));
    profile.addEventListener('click', () => { void openAccountDialog(data); });
    mount.appendChild(row);
  }

  async function readProfile() {
    try {
      const response = await debridPulseFetch('/api/account', {headers: {'Accept': 'application/json'}});
      return response.ok ? await response.json() : null;
    } catch (_) {
      return null;
    }
  }

  async function sendJson(method, url, body) {
    const response = await debridPulseFetch(url, {
      method, headers: {'Content-Type': 'application/json', 'Accept': 'application/json'}, body: JSON.stringify(body),
    });
    if (response.ok) return null;
    const payload = await response.json().catch(() => ({}));
    const detail = Array.isArray(payload.detail) ? payload.detail[0]?.msg : payload.detail;
    return String(detail || 'The change was not saved.');
  }

  // The account's own profile, read from the account API when the dialog
  // opens: the email (and the verification its identity provider stated) is
  // shown, never edited here.
  const VERIFICATION = {verified: 'Verified', unverified: 'Not verified', unknown: 'Verification not stated'};

  /* My Account: ONE dialog of one fixed size with two views. The profile view
   * (Display Name, Actual Name, Email) saves the two names; Change Password,
   * offered only to an account that holds a local password, swaps the same
   * dialog to the password view, whose Cancel returns to the profile -- with
   * its unsaved edits intact -- and whose Save changes only the password. A
   * failed save keeps the dialog and says why. */
  async function openAccountDialog(data) {
    const modal = window.DPSettingsModal;
    if (!modal) return;
    const own = await readProfile();
    const profile = own || {display_name: data.display_name || '', actual_name: '', email: '',
      email_verification: null, has_password: false};
    const fields = {};
    const views = {};
    let view = 'profile';
    const notice = text => window.toast(text, 'error');

    const show = (next, handle) => {
      view = next;
      views.profile.hidden = next !== 'profile';
      views.password.hidden = next !== 'password';
      handle.setActionHidden('password', next !== 'profile');
      if (next === 'profile') {
        // Secrets never outlive their view.
        ['current', 'next', 'confirm'].forEach(name => { fields[name].value = ''; });
        fields.display_name.focus();
      } else {
        fields.current.focus();
      }
    };

    modal.open({
      title: 'My Account',
      acceptLabel: 'Save',
      cancelLabel: 'Cancel',
      className: 'dp-account-dialog',
      actions: profile.has_password ? [{id: 'password', label: 'Change Password', placement: 'leading'}] : [],
      onAction(id) {
        if (id === 'password') show('password', this.handle);
      },
      mount(body, handle) {
        this.handle = handle;
        body.innerHTML = `
          <div class="dp-account-view" data-account-view="profile">
            <label class="dp-modal-field"><span class="form-label">Display Name</span>
              <input class="input" data-field="display_name" type="text" maxlength="256" autocomplete="nickname"></label>
            <label class="dp-modal-field"><span class="form-label">Actual Name</span>
              <input class="input" data-field="actual_name" type="text" maxlength="256" autocomplete="name"></label>
            <div class="dp-modal-field dp-account-email"><span class="form-label">Email</span>
              <span class="dp-account-email-value"></span></div>
          </div>
          <div class="dp-account-view" data-account-view="password" hidden>
            <label class="dp-modal-field"><span class="form-label">Current Password</span>
              <input class="input" data-field="current" type="password" maxlength="4096" autocomplete="current-password"></label>
            <label class="dp-modal-field"><span class="form-label">New Password</span>
              <input class="input" data-field="next" type="password" maxlength="4096" autocomplete="new-password"></label>
            <label class="dp-modal-field"><span class="form-label">Confirm New Password</span>
              <input class="input" data-field="confirm" type="password" maxlength="4096" autocomplete="new-password"></label>
          </div>`;
        body.querySelectorAll('[data-account-view]').forEach(node => { views[node.dataset.accountView] = node; });
        body.querySelectorAll('[data-field]').forEach(input => { fields[input.dataset.field] = input; });
        fields.display_name.value = String(profile.display_name || data.display_name || '');
        fields.actual_name.value = String(profile.actual_name || '');
        const email = body.querySelector('.dp-account-email-value');
        email.textContent = profile.email
          ? `${profile.email}${VERIFICATION[profile.email_verification] ? ` (${VERIFICATION[profile.email_verification]})` : ''}`
          : 'None provided';
        return null;
      },
      onCancel(handle) {
        if (view !== 'password') return true;
        show('profile', handle);
        return false;
      },
      async onAccept() {
        if (view === 'password') {
          if (!fields.current.value || !fields.next.value) { notice('Enter your current and new password.'); return false; }
          if (fields.next.value !== fields.confirm.value) { notice('The new passwords do not match.'); return false; }
          const failure = await sendJson('PUT', '/api/account/password',
            {current_password: fields.current.value, new_password: fields.next.value});
          if (failure) { notice(failure); return false; }
          // The server ended every session of this account: sign in again.
          redirectToLogin();
          return true;
        }
        const name = fields.display_name.value.trim();
        if (!name) { notice('A display name is required.'); return false; }
        if (name !== String(profile.display_name || '')) {
          const failure = await sendJson('PUT', '/api/account/display-name', {display_name: name});
          if (failure) { notice(failure); return false; }
        }
        const actual = fields.actual_name.value.trim();
        if (actual !== String(profile.actual_name || '')) {
          const failure = await sendJson('PUT', '/api/account/actual-name', {actual_name: actual});
          if (failure) { notice(failure); return false; }
        }
        window.location.reload();
        return true;
      },
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
