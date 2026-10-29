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
 * (re)connected live pulse, a 403, the periodic check) the page reloads under
 * the server's current authority. The server enforces every action regardless. */
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
    syncHeaderSessionUi(data);
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
      <button type="button" class="dp-sidebar-logout" aria-label="Log out of DebridPulse" data-tooltip="Log Out">
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
      <button type="button" class="dp-sidebar-profile" aria-label="My Account" data-tooltip="My Account">
        <span class="nav-label"><span class="dp-sidebar-account-copy"><span class="dp-sidebar-account-name"></span><span class="dp-sidebar-account-role"></span></span></span>
        <span class="icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="8" r="4"></circle><path d="M4 21a8 8 0 0 1 16 0"></path>
          </svg>
        </span>
      </button>`;

    // Log Out is quiet: its label and icon never change, so the row keeps its
    // measure beside the account.
    bindLogout(row.querySelector('.dp-sidebar-logout'));

    const profile = row.querySelector('.dp-sidebar-profile');
    const showAccount = name => {
      const display = String(name || '');
      profile.querySelector('.dp-sidebar-account-name').textContent = display;
      profile.querySelector('.dp-sidebar-account-role').textContent = roleLabel(data.role);
      profile.setAttribute('aria-label', display ? `My account: ${display}, ${roleLabel(data.role)}`
        : `My account, ${roleLabel(data.role)}`);
      // A name too long for the row is shown in full here.
      profile.dataset.tooltip = `${display ? `${display} ` : ''}(${roleLabel(data.role)}): My Account`;
    };
    showAccount('');
    // The account's own Display Name. The session's name is its sign-in (an
    // OIDC ``name`` claim, a username) and is never shown first; it stands in
    // only when the account's profile cannot be read.
    readProfile().then(own => {
      const name = own?.display_name || data.display_name || 'My Account';
      showAccount(name);
      showHeaderAccount(data, name);
    });
    profile.addEventListener('click', () => { void openAccountDialog(data); });
    mount.appendChild(row);
  }

  // A pending logout refuses a second press; a refused or failed one says so
  // and may be pressed again.
  function bindLogout(button) {
    button.addEventListener('click', async () => {
      if (button.getAttribute('aria-disabled') === 'true') return;
      button.setAttribute('aria-disabled', 'true');
      try {
        const ok = await logoutSession();
        if (!ok) throw new Error('Logout failed');
      } catch (_) {
        button.setAttribute('aria-disabled', 'false');
        try { window.toast('Log Out failed. Try again.', 'error'); } catch (_) {}
      }
    });
  }

  /* The handheld shell's session control (below 780px, where the sidebar
   * gives way to the dock; ui-shell-mobile.css): one account button at the
   * start of the header, opening a sheet with the account (Display Name over
   * Role), My Account and Log Out -- the same dialog and the same logout as
   * the sidebar's row. A press beside the sheet's panel closes it without
   * reaching the page; Escape and Tab close it, focus back on the button. */
  const handheld = window.matchMedia('(max-width: 779px)');
  function syncHeaderSessionUi(data) {
    const mount = document.getElementById('topbar-account-mount');
    if (!mount) return;
    if (!data?.authenticated) {
      mount.hidden = true;
      mount.replaceChildren();
      return;
    }
    mount.hidden = false;
    if (mount.querySelector('#topbar-account-button')) return;
    mount.innerHTML = `
      <button type="button" id="topbar-account-button" class="dp-topbar-account-button" popovertarget="topbar-account-sheet" aria-label="My account">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
          <circle cx="12" cy="8" r="4"></circle><path d="M4 21a8 8 0 0 1 16 0"></path>
        </svg>
      </button>
      <div id="topbar-account-sheet" class="dp-shell-sheet dp-shell-sheet--down" popover role="dialog" aria-labelledby="topbar-account-name">
        <div class="dp-shell-sheet-panel">
          <div class="dp-shell-sheet-title dp-topbar-account-copy">
            <span class="dp-topbar-account-name" id="topbar-account-name"></span>
            <span class="dp-topbar-account-role"></span>
          </div>
          <div class="dp-shell-sheet-list">
            <button type="button" class="dp-shell-sheet-item" data-account-action="profile">
              <span class="icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"></circle><path d="M4 21a8 8 0 0 1 16 0"></path></svg></span>
              <span class="dp-shell-label">My Account</span>
            </button>
            <button type="button" class="dp-shell-sheet-item" data-account-action="logout">
              <span class="icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10 5H6a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h4"></path><path d="M14 8l4 4-4 4"></path><path d="M18 12H9"></path></svg></span>
              <span class="dp-shell-label">Log Out</span>
            </button>
          </div>
        </div>
      </div>`;
    const sheet = mount.querySelector('#topbar-account-sheet');
    const close = () => { if (sheet.matches(':popover-open')) sheet.hidePopover(); };
    sheet.addEventListener('click', event => {
      if (event.target !== sheet) return;
      close();
      mount.querySelector('#topbar-account-button').focus();
    });
    sheet.addEventListener('keydown', event => { if (event.key === 'Tab') close(); });
    sheet.addEventListener('toggle', event => {
      if (event.newState === 'open') sheet.querySelector('.dp-shell-sheet-item')?.focus();
    });
    handheld.addEventListener('change', close);
    sheet.querySelector('[data-account-action="profile"]').addEventListener('click', () => {
      close();
      void openAccountDialog(data);
    });
    bindLogout(sheet.querySelector('[data-account-action="logout"]'));
    showHeaderAccount(data, '');
  }

  function showHeaderAccount(data, name) {
    const mount = document.getElementById('topbar-account-mount');
    const button = mount?.querySelector('#topbar-account-button');
    if (!button) return;
    const display = String(name || '');
    mount.querySelector('.dp-topbar-account-name').textContent = display || 'My Account';
    mount.querySelector('.dp-topbar-account-role').textContent = roleLabel(data.role);
    button.setAttribute('aria-label', display ? `My account: ${display}, ${roleLabel(data.role)}`
      : `My account, ${roleLabel(data.role)}`);
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
    return {status: response.status, message: String(detail || 'The change was not saved.')};
  }

  // The account's own profile, read from the account API when the dialog
  // opens. Its email is shown as the bare address: one its identity provider
  // supplied is read-only here; one the account entered itself carries the
  // Usenet server name's Pencil (the same control) to correct it. An account
  // with none may enter one. An entered address is recorded unverified; the
  // server decides every save.
  function emailMarkup(profile, editing) {
    if (!profile.email || editing) {
      return '<label class="form-label" for="dp-account-email-input">Email</label>'
        + '<input class="input" id="dp-account-email-input" data-field="email" type="email" maxlength="254" '
        + 'autocomplete="email" spellcheck="false" placeholder="you@example.com">';
    }
    const pencil = profile.email_self_entered
      ? '<button type="button" class="btn btn-ghost btn-sm dp-usenet-name-edit dp-account-email-edit" '
        + 'data-tooltip="Edit email" aria-label="Edit email"><img src="/icons/lucide/pencil.svg" alt="" aria-hidden="true"></button>'
      : '';
    return '<span class="form-label">Email</span>'
      + `<span class="dp-account-email-value"><span class="dp-account-email-address"></span>${pencil}</span>`;
  }

  /* My Account: ONE dialog of one fixed size with two views. The profile view
   * (the account's DP Username, shown read-only -- an Admin changes it --
   * then Display Name, Actual Name, Email) saves the two names and, while the
   * account has none, an email; Change Password, offered only to an account
   * that holds a local password, swaps the same dialog to the password view, whose Cancel returns to the profile -- with
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
    let emailSlot = null;
    // The Pencil turns the shown address into the field, holding it; Save
    // sends the change, Cancel discards it.
    let editingEmail = false;
    const paintEmail = () => {
      if (!emailSlot) return;
      emailSlot.innerHTML = emailMarkup(profile, editingEmail);
      fields.email = emailSlot.querySelector('[data-field="email"]');
      if (fields.email && editingEmail) fields.email.value = String(profile.email || '');
      const address = emailSlot.querySelector('.dp-account-email-address');
      if (address) address.textContent = String(profile.email || '');
      emailSlot.querySelector('.dp-account-email-edit')?.addEventListener('click', () => {
        editingEmail = true;
        paintEmail();
        fields.email.focus();
      });
    };

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
            <div class="dp-modal-field dp-account-username">
              <span class="form-label">DP Username</span>
              <span class="dp-account-username-value" data-account-username></span>
              <p class="dp-account-username-note">Your DebridPulse account username. Used for local password sign-in when configured. Separate from your display name.</p>
            </div>
            <label class="dp-modal-field"><span class="form-label">Display Name</span>
              <input class="input" data-field="display_name" type="text" maxlength="256" autocomplete="nickname"></label>
            <label class="dp-modal-field"><span class="form-label">Actual Name</span>
              <input class="input" data-field="actual_name" type="text" maxlength="256" autocomplete="name"></label>
            <div class="dp-modal-field dp-account-email" data-account-email></div>
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
        body.querySelector('[data-account-username]').textContent = String(profile.username || '');
        fields.display_name.value = String(profile.display_name || data.display_name || '');
        fields.actual_name.value = String(profile.actual_name || '');
        emailSlot = body.querySelector('[data-account-email]');
        paintEmail();
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
          if (failure) { notice(failure.message); return false; }
          // The server ended every session of this account: sign in again.
          redirectToLogin();
          return true;
        }
        const name = fields.display_name.value.trim();
        if (!name) { notice('A display name is required.'); return false; }
        // Each saved part is the profile's at once, so a retry after a later
        // part's failure sends only what is still unsaved.
        if (name !== String(profile.display_name || '')) {
          const failure = await sendJson('PUT', '/api/account/display-name', {display_name: name});
          if (failure) { notice(failure.message); return false; }
          profile.display_name = name;
        }
        const actual = fields.actual_name.value.trim();
        if (actual !== String(profile.actual_name || '')) {
          const failure = await sendJson('PUT', '/api/account/actual-name', {actual_name: actual});
          if (failure) { notice(failure.message); return false; }
          profile.actual_name = actual;
        }
        // A blank first email stays blank; an entered or corrected address is
        // sent with the address this form showed, so the server refuses (409)
        // a stale form, a concurrent save or an address it does not let this
        // account change -- and the dialog then shows the stored one. An
        // emptied correction is sent too: the server says why it is invalid.
        const email = fields.email ? fields.email.value.trim() : '';
        const shown = String(profile.email || '');
        if (fields.email && (shown ? email !== shown : email)) {
          const failure = await sendJson('PUT', '/api/account/email', {email, expected: shown});
          if (failure) {
            notice(failure.message);
            if (failure.status === 409) {
              const current = await readProfile();
              if (current) Object.assign(profile, current);
              editingEmail = false;
              paintEmail();
            }
            return false;
          }
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

  // The signed-in account's own appearance. DPTheme (ui-theme-bootstrap.js)
  // owns the choices; this owner only carries them to and from the account's
  // record. While sign-in is off DPTheme keeps them in the browser and takes
  // no record.
  window.DPTheme?.connect({
    save: changes => sendJson('PATCH', '/api/account/appearance', changes).then(failure => !failure),
    read: () => debridPulseFetch('/api/account/appearance', {headers: {'Accept': 'application/json'}})
      .then(response => (response.ok ? response.json() : null)),
  });

  refreshSession().catch(() => {});
  window.setInterval(() => refreshSession({force: true}).catch(() => {}), 60000);
  // The live pulse (re)connected. An account_changed is announced only to the
  // streams subscribed at that moment, so one made while this page was loading
  // (before its stream subscribed) or while a dropped stream was reconnecting
  // reached nobody: re-read the session now, which resets the page only if its
  // authority moved.
  document.addEventListener('debridpulse:pulse-connected', () => {
    refreshSession({force: true}).catch(() => {});
  });
})();
