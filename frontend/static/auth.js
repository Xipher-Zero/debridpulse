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

  /* Whose account the shell offers, from the server's own session answer:
   * "authenticated" -- a signed-in browser session (Log Out and My Account);
   * "standalone" -- no interactive authentication, where the server admits
   * every request as the installation's default Admin (auth/models.py
   * Principal.standalone: bound to that account, authenticating nothing, so
   * no mechanism); otherwise none. The client never claims a session. */
  function sessionKind(data) {
    if (data?.authenticated === true) return 'authenticated';
    if (data && data.authenticated === false && !data.mechanism && data.account_id) return 'standalone';
    return '';
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
  // belonging to neither. Without interactive authentication there is no
  // session to leave: the row holds My Account alone, in the same place,
  // opening the default Admin's own account.
  function syncSidebarSessionUi(data) {
    syncHeaderSessionUi(data);
    const stack = document.getElementById('sidebar-bottom-stack');
    const mount = document.getElementById('sidebar-auth-mount');
    if (!stack || !mount) return;

    const kind = sessionKind(data);
    if (!kind) {
      delete stack.dataset.session;
      mount.hidden = true;
      mount.replaceChildren();
      return;
    }

    stack.dataset.session = kind;
    mount.hidden = false;
    if (mount.querySelector(`#sidebar-auth-row[data-session="${kind}"]`)) return;
    mount.replaceChildren();
    if (kind === 'standalone') {
      mount.appendChild(standaloneSidebarRow(data));
      return;
    }

    const row = document.createElement('div');
    row.id = 'sidebar-auth-row';
    row.className = 'nav-item dp-sidebar-session-row';
    row.dataset.session = 'authenticated';
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

  // The standalone row: My Account alone, an ordinary sidebar entry -- its
  // glyph on the icon rail and its label after it, as Log Out sits in the
  // signed-in row -- named only My Account: no session, so no sign-in name.
  function standaloneSidebarRow(data) {
    const row = document.createElement('div');
    row.id = 'sidebar-auth-row';
    row.className = 'nav-item dp-sidebar-session-row';
    row.dataset.session = 'standalone';
    row.style.flexShrink = '0';
    row.innerHTML = `
      <button type="button" class="dp-sidebar-profile" aria-label="My Account" data-tooltip="My Account">
        <span class="icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="8" r="4"></circle><path d="M4 21a8 8 0 0 1 16 0"></path>
          </svg>
        </span>
        <span class="nav-label">My Account</span>
      </button>`;
    row.querySelector('.dp-sidebar-profile').addEventListener('click', () => { void openAccountDialog(data); });
    return row;
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
   * reaching the page; Escape and Tab close it, focus back on the button.
   * Without interactive authentication the same button opens My Account
   * itself: there is no session to log out of, so no sheet. */
  const handheld = window.matchMedia('(max-width: 779px)');
  function syncHeaderSessionUi(data) {
    const mount = document.getElementById('topbar-account-mount');
    if (!mount) return;
    const kind = sessionKind(data);
    if (!kind) {
      mount.hidden = true;
      mount.replaceChildren();
      return;
    }
    mount.hidden = false;
    if (mount.querySelector(`#topbar-account-button[data-session="${kind}"]`)) return;
    if (kind === 'standalone') {
      mount.innerHTML = `
      <button type="button" id="topbar-account-button" class="dp-topbar-account-button" data-session="standalone" aria-label="My Account">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
          <circle cx="12" cy="8" r="4"></circle><path d="M4 21a8 8 0 0 1 16 0"></path>
        </svg>
      </button>`;
      mount.querySelector('#topbar-account-button').addEventListener('click', () => { void openAccountDialog(data); });
      return;
    }
    mount.innerHTML = `
      <button type="button" id="topbar-account-button" class="dp-topbar-account-button" data-session="authenticated" aria-haspopup="dialog" aria-controls="topbar-account-sheet" aria-expanded="false" aria-label="My account">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">
          <circle cx="12" cy="8" r="4"></circle><path d="M4 21a8 8 0 0 1 16 0"></path>
        </svg>
      </button>
      <div id="topbar-account-sheet" class="dp-shell-sheet dp-shell-sheet--down" popover="manual" role="dialog" aria-labelledby="topbar-account-name">
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
    const button = mount.querySelector('#topbar-account-button');
    // Opened and closed through the shell's one sheet lifetime (app.js
    // DPShellSheet): closing, it rolls back up to the header before it hides.
    const close = options => window.DPShellSheet.close(sheet, options);
    button.addEventListener('click', () => window.DPShellSheet.toggle(sheet, {opener: button}));
    sheet.addEventListener('click', event => {
      if (event.target !== sheet) return;
      close();
      button.focus();
    });
    sheet.addEventListener('keydown', event => { if (event.key === 'Tab') close(); });
    sheet.addEventListener('toggle', event => {
      // Into the sheet without scrolling it while its panel travels in.
      if (event.newState === 'open') sheet.querySelector('.dp-shell-sheet-item')?.focus({preventScroll: true});
    });
    handheld.addEventListener('change', () => close({immediate: true}));
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
   * account has none, an email; the password action swaps the same dialog to
   * the password view, whose Cancel returns to the profile -- with its unsaved
   * edits intact -- and whose Save changes only the password. An account that
   * holds a local password is offered Change Password (Current, New,
   * Confirm); one whose own profile says it has none -- it signs in through
   * its identity provider -- is offered Set Password, its first one (New and
   * Confirm only, under the username it already has, for a password manager).
   * Neither is offered when the profile could not be read; the server decides
   * either way. A failed save keeps the dialog and says why.
   *
   * On a Standalone installation (the deployment type the server states) the
   * profile has no DP Username line, and beneath the title the header says
   * whether signing in needs a password now -- the server's own reading
   * (``sign_in_required``), and nothing while it is unread. A password-
   * protected Standalone installation's signed-in Administrator is offered,
   * beside Change Password, Clear Password: a third view, destructive, that
   * proves the current password and, once confirmed, removes it and turns
   * password sign-in off together (the server does both, or neither).
   *
   * At the header's far end, for every account that owns a presentation, is
   * Easy | Advanced (presentationSelector): a choice that applies at once and
   * is saved on its own, never by Save, and whose Cancel undoes nothing. */
  // What clearing a Standalone installation's one password does.
  const CLEAR_CONSEQUENCE = 'Clearing your password turns off password sign-in. Anyone who can reach DebridPulse '
    + 'can then access this installation without a password, until you set a new one in My Account.';

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
    // Set Password: the account's own read says it has no local password.
    const first = Boolean(own) && own.has_password === false;
    const passwordAction = profile.has_password ? 'Change Password' : first ? 'Set Password' : '';
    const standalone = (own?.deployment_type || data.deployment_type) === 'standalone';
    // A Standalone installation's open default Admin: its first password also
    // turns on password sign-in (the server does both, or neither).
    const standaloneFirst = first && standalone && sessionKind(data) === 'standalone';
    const required = typeof own?.sign_in_required === 'boolean' ? own.sign_in_required : null;
    const statusHint = !standalone || required === null ? ''
      : required ? 'Password required to sign in' : 'No password required to sign in';
    // Clear Password: the password-protected Standalone installation's own
    // signed-in Administrator only.
    const clearable = standalone && required === true && own?.has_password === true
      && sessionKind(data) === 'authenticated';
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
      Object.entries(views).forEach(([name, node]) => { node.hidden = name !== next; });
      ['password', 'clear'].forEach(id => handle.setActionHidden(id, next !== 'profile'));
      // Clear Password's view is destructive: the red treatment, and its own accept.
      handle.setTone(next === 'clear' ? 'danger' : '');
      handle.setAcceptLabel(next === 'clear' ? 'Clear Password' : 'Save');
      if (next === 'profile') {
        // Secrets never outlive their view.
        ['current', 'next', 'confirm', 'clear_current'].forEach(name => { if (fields[name]) fields[name].value = ''; });
        fields.display_name.focus();
      } else if (next === 'clear') {
        fields.clear_current.focus();
      } else {
        (fields.current || fields.next).focus();
      }
    };
    const actions = [];
    if (passwordAction) actions.push({id: 'password', label: passwordAction, placement: 'leading'});
    if (clearable) actions.push({id: 'clear', label: 'Clear Password', tone: 'danger', placement: 'leading'});

    // The header's far end: My Account's Help, then Easy | Advanced for every
    // account that owns a presentation. Help explains what this dialog offers
    // this account, from the facts it was opened with, over this dialog.
    const selector = presentation.owned ? presentationSelector() : null;
    const headerEnd = document.createElement('div');
    headerEnd.className = 'dp-account-header-end';
    headerEnd.innerHTML = '<button type="button" class="dp-settings-help-chip" aria-label="Help: My Account" '
      + `data-tooltip="Help: My Account">${window.DPIcons?.svg ? window.DPIcons.svg('help', 'dp-settings-help-glyph') : '?'}</button>`;
    headerEnd.querySelector('button').addEventListener('click', () => window.DPSettingsHelp?.open('my-account', {account: {
      standalone, open: sessionKind(data) === 'standalone', signInRequired: required, hasPassword: own?.has_password === true,
      passwordAction, first, standaloneFirst, clearable, showsUsername: !standalone,
      email: Boolean(profile.email), emailSelfEntered: profile.email_self_entered === true,
      presentation: presentation.owned,
    }}));
    if (selector) headerEnd.append(selector.element);
    const dialog = modal.open({
      title: 'My Account',
      titleHint: statusHint,
      headerEnd,
      acceptLabel: 'Save',
      cancelLabel: 'Cancel',
      className: 'dp-account-dialog',
      actions,
      onAction(id) {
        if (id === 'password' || id === 'clear') show(id, this.handle);
      },
      mount(body, handle) {
        this.handle = handle;
        body.innerHTML = `
          <div class="dp-account-view" data-account-view="profile">${standalone ? '' : `
            <div class="dp-modal-field dp-account-username">
              <span class="form-label">DP Username</span>
              <span class="dp-account-username-value" data-account-username></span>
              <p class="dp-account-username-note">Your DebridPulse account username. Used for local password sign-in when configured. Separate from your display name.</p>
            </div>`}
            <label class="dp-modal-field"><span class="form-label">Display Name</span>
              <input class="input" data-field="display_name" type="text" maxlength="256" autocomplete="nickname"></label>
            <label class="dp-modal-field"><span class="form-label">Actual Name</span>
              <input class="input" data-field="actual_name" type="text" maxlength="256" autocomplete="name"></label>
            <div class="dp-modal-field dp-account-email" data-account-email></div>
          </div>
          <div class="dp-account-view" data-account-view="password" hidden>${first ? `
            <input type="text" data-account-password-username autocomplete="username" readonly hidden>` : `
            <label class="dp-modal-field"><span class="form-label">Current Password</span>
              <input class="input" data-field="current" type="password" maxlength="4096" autocomplete="current-password"></label>`}
            <label class="dp-modal-field"><span class="form-label">New Password</span>
              <input class="input" data-field="next" type="password" maxlength="4096" autocomplete="new-password"></label>
            <label class="dp-modal-field"><span class="form-label">Confirm New Password</span>
              <input class="input" data-field="confirm" type="password" maxlength="4096" autocomplete="new-password"></label>${first ? `
            <p class="dp-account-username-note">${standaloneFirst
              ? 'Saving it turns on password sign-in: DebridPulse then asks for this password.'
              : 'Local password sign-in works only if your administrator has enabled it.'}</p>` : ''}
          </div>${clearable ? `
          <div class="dp-account-view" data-account-view="clear" hidden>
            <label class="dp-modal-field"><span class="form-label">Current Password</span>
              <input class="input" data-field="clear_current" type="password" maxlength="4096" autocomplete="current-password"></label>
            <p class="dp-account-clear-warning">${CLEAR_CONSEQUENCE}</p>
          </div>` : ''}`;
        body.querySelectorAll('[data-account-view]').forEach(node => { views[node.dataset.accountView] = node; });
        body.querySelectorAll('[data-field]').forEach(input => { fields[input.dataset.field] = input; });
        const username = body.querySelector('[data-account-username]');
        if (username) username.textContent = String(profile.username || '');
        // The account the new password belongs to, for a password manager only:
        // never sent, never chosen here.
        const passwordUsername = body.querySelector('[data-account-password-username]');
        if (passwordUsername) passwordUsername.value = String(profile.username || '');
        fields.display_name.value = String(profile.display_name || data.display_name || '');
        fields.actual_name.value = String(profile.actual_name || '');
        emailSlot = body.querySelector('[data-account-email]');
        paintEmail();
        return null;
      },
      onCancel(handle) {
        if (view === 'profile') return true;
        show('profile', handle);
        return false;
      },
      async onAccept() {
        if (view === 'clear') {
          if (!fields.clear_current.value) { notice('Enter your current password.'); return false; }
          const confirmed = await modal.confirm({tone: 'danger', title: 'Clear Password?',
            confirmLabel: 'Clear Password', message: CLEAR_CONSEQUENCE});
          if (!confirmed) return false;
          const failure = await sendJson('POST', '/api/account/clear-password',
            {current_password: fields.clear_current.value});
          if (failure) { notice(failure.message); return false; }
          // The server ended every session: the installation is open now.
          window.location.assign('/');
          return true;
        }
        if (view === 'password' && first) {
          if (!fields.next.value) { notice('Enter a new password.'); return false; }
          if (fields.next.value !== fields.confirm.value) { notice('The new passwords do not match.'); return false; }
          const failure = await sendJson('POST', '/api/account/first-password', {new_password: fields.next.value});
          if (failure) { notice(failure.message); return false; }
          // The server ended every session of this account: sign in again.
          redirectToLogin();
          return true;
        }
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
    if (selector) dialog.closed.then(selector.release);
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

  /* The account's own presentation: Advanced -- the whole interface its role
   * and the installation's deployment type allow -- or Easy, that same
   * interface without the parts the Easy list of that deployment type names.
   * It changes what is shown, never what the account may do: the server
   * enforces that either way. The server states it before this bundle runs
   * (window.debridPulsePresentation: whether the request has an account to
   * own one, and that account's saved choice, null when it could not be read
   * in time), so an Easy page never shows an Advanced-only part first; an
   * unread choice is Advanced until it is read. The choice is the account's
   * own record (PUT /api/account/presentation) -- the default Admin's while
   * sign-in is off -- the same in every browser; the browser keeps none.
   *
   * One Easy list per deployment type, each the complete list of what Easy
   * omits there; both name the same parts for now, and each may change
   * without the other. Owners ask omits(part) when they compose; a shell part
   * declares itself with data-presentation and is placed or withdrawn here
   * (composePresented). A choice applies at once; its saves are sent one at a
   * time, the newest choice last, so an older answer never overrides a newer
   * choice; a refused or failed save returns to the last saved choice and
   * says so. */
  const EASY_PASS_1 = ['activity-log', 'provider-additional-settings', 'download-behavior-advanced',
    'transfer-method-settings', 'disk-space-recovery', 'download-engine-activity', 'username-password',
    'openid-connect', 'deployment-type', 'statistics-reporting', 'event-logging', 'backup-additional-settings',
    'database-reset'];
  const EASY = Object.freeze({
    server: Object.freeze([...EASY_PASS_1]),
    standalone: Object.freeze([...EASY_PASS_1]),
  });
  const PRESENTATION_MODES = ['easy', 'advanced'];
  const stated = window.debridPulsePresentation || {};
  const presentation = {
    owned: stated.owned === true,
    // The last choice the server confirmed (null: not read), and the one shown.
    saved: PRESENTATION_MODES.includes(stated.mode) ? stated.mode : null,
    mode: stated.mode === 'easy' ? 'easy' : 'advanced',
    // The newest choice not yet sent, and the save on its way.
    wanted: null,
    writing: null,
    listeners: new Set(),
  };
  // Every shell part that declares itself, with the place it is returned to.
  const presentedParts = [];

  function omits(part, deployment = authority.deployment_type) {
    if (presentation.mode !== 'easy') return false;
    return EASY[deployment === 'standalone' ? 'standalone' : 'server'].includes(String(part || ''));
  }

  // Place every declared shell part this presentation shows and withdraw the
  // rest; returns the parts it withdrew now.
  function composePresented() {
    const withdrawn = [];
    presentedParts.forEach(({node, anchor}) => {
      const omitted = omits(node.dataset.presentation);
      if (omitted && node.isConnected) { node.remove(); withdrawn.push(node); }
      else if (!omitted && !node.isConnected && anchor.isConnected) anchor.after(node);
    });
    return withdrawn;
  }

  function presentationState() {
    if (!presentation.owned) return 'none';
    if (presentation.writing) return 'saving';
    return presentation.saved ? 'saved' : 'unavailable';
  }

  function announcePresentation(detail = {}) {
    presentation.listeners.forEach(listener => { try { listener(detail); } catch (_) {} });
  }

  function present(mode) {
    if (mode === presentation.mode) return;
    presentation.mode = mode;
    const withdrawn = composePresented();
    document.dispatchEvent(new CustomEvent('debridpulse:presentation-changed', {detail: {mode, withdrawn}}));
  }

  async function savePresentation() {
    let outcome = {};
    while (presentation.wanted) {
      const mode = presentation.wanted;
      presentation.wanted = null;
      let accepted = null;
      let message = 'Your presentation could not be saved.';
      try {
        const response = await debridPulseFetch('/api/account/presentation', {
          method: 'PUT', headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
          body: JSON.stringify({presentation_mode: mode}),
        });
        const payload = await response.json().catch(() => ({}));
        if (response.ok && PRESENTATION_MODES.includes(payload?.presentation_mode)) accepted = payload.presentation_mode;
        else if (typeof payload?.detail === 'string' && payload.detail) message = `Not saved: ${payload.detail}`;
      } catch (_) {}
      if (accepted) presentation.saved = accepted;
      // A newer choice is on its way: its answer, not this one, decides.
      if (presentation.wanted) continue;
      if (accepted === mode) {
        outcome = {saved: mode};
      } else {
        present(presentation.saved || 'advanced');
        outcome = {failed: mode};
        window.toast?.(message, 'error');
      }
    }
    presentation.writing = null;
    announcePresentation(outcome);
  }

  function choosePresentation(mode) {
    if (!presentation.owned || !PRESENTATION_MODES.includes(mode)) return Promise.resolve(false);
    present(mode);
    presentation.wanted = mode;
    if (!presentation.writing) presentation.writing = savePresentation();
    announcePresentation();
    return presentation.writing.then(() => presentation.saved === mode);
  }

  // An unread choice is read again where it is offered (My Account).
  async function readPresentation() {
    if (!presentation.owned || presentation.saved || presentation.writing) return;
    try {
      const response = await debridPulseFetch('/api/account/presentation', {headers: {'Accept': 'application/json'}});
      const payload = response.ok ? await response.json() : null;
      if (PRESENTATION_MODES.includes(payload?.presentation_mode) && !presentation.saved && !presentation.writing) {
        presentation.saved = payload.presentation_mode;
        present(payload.presentation_mode);
      }
    } catch (_) {}
    announcePresentation();
  }

  /* My Account's Easy | Advanced: one control of two compartments, a radio
   * group. While the choice is unread neither compartment is chosen; while a
   * save is on its way the group is busy; its status line says what the
   * server last answered. */
  function presentationSelector() {
    const group = document.createElement('div');
    group.className = 'dp-presentation-switch';
    group.setAttribute('role', 'radiogroup');
    group.setAttribute('aria-label', 'Presentation');
    group.innerHTML = PRESENTATION_MODES.map(mode => `<button type="button" class="dp-presentation-option" role="radio" `
      + `data-presentation-mode="${mode}">${mode === 'easy' ? 'Easy' : 'Advanced'}</button>`).join('')
      + '<span class="dp-presentation-status" role="status"></span>';
    const options = [...group.querySelectorAll('[data-presentation-mode]')];
    const status = group.querySelector('.dp-presentation-status');
    const paint = (detail = {}) => {
      const state = presentationState();
      const chosen = state === 'unavailable' ? null : presentation.mode;
      group.dataset.state = state;
      group.setAttribute('aria-busy', state === 'saving' ? 'true' : 'false');
      options.forEach(option => {
        const checked = option.dataset.presentationMode === chosen;
        option.setAttribute('aria-checked', String(checked));
        option.tabIndex = checked || (!chosen && option === options[0]) ? 0 : -1;
      });
      const name = mode => (mode === 'easy' ? 'Easy' : 'Advanced');
      if (detail.failed) status.textContent = `${name(detail.failed)} was not saved.`;
      else if (state === 'saving') status.textContent = `Saving ${name(presentation.mode)}…`;
      else if (detail.saved) status.textContent = `${name(detail.saved)} saved.`;
      else if (state === 'unavailable') status.textContent = 'Presentation unavailable.';
    };
    const choose = option => {
      void choosePresentation(option.dataset.presentationMode);
      option.focus();
    };
    options.forEach((option, index) => {
      option.addEventListener('click', () => choose(option));
      option.addEventListener('keydown', event => {
        const step = {ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1}[event.key];
        if (!step) return;
        event.preventDefault();
        choose(options[(index + step + options.length) % options.length]);
      });
    });
    presentation.listeners.add(paint);
    paint();
    void readPresentation();
    return {element: group, release: () => presentation.listeners.delete(paint)};
  }

  window.debridPulseAuth = Object.freeze({
    fetch: debridPulseFetch,
    refreshSession,
    session: () => sessionState,
    logout: logoutSession,
    can,
    mayDestroy,
    authority,
    presentation: Object.freeze({
      mode: () => presentation.mode,
      omits,
      choose: choosePresentation,
      state: presentationState,
      // The interface this page composes, as the explanations of it read it:
      // the role's capabilities, the deployment type and the presentation,
      // with the Easy list's omissions -- this owner's facts, nothing more.
      context: () => Object.freeze({
        role: String(authority.role || ''),
        deployment: authority.deployment_type === 'standalone' ? 'standalone' : 'server',
        standalone: authority.deployment_type === 'standalone',
        server: authority.deployment_type !== 'standalone',
        mode: presentation.mode,
        easy: presentation.mode === 'easy',
        advanced: presentation.mode !== 'easy',
        admin: can('administer'),
        personal: can('notifications.self'),
        submit: can('transfers.submit'),
        operate: can('transfers.operate'),
        omits: part => omits(part),
        shows: part => !omits(part),
      }),
    }),
  });

  composeAuthorized();
  document.querySelectorAll('[data-presentation]').forEach(node => {
    const anchor = document.createComment(` ${node.dataset.presentation} `);
    node.before(anchor);
    presentedParts.push({node, anchor});
  });
  composePresented();

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
