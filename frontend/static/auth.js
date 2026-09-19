/* DebridPulse application-session bootstrap and owner. Loaded before app.js.
 *
 * Owns the browser session and the authenticated same-origin request path.
 * It publishes that path as debridPulseAuth.fetch (session cookie, CSRF header
 * on mutations, session revalidation on 401); it does not replace the native
 * fetch. Every application HTTP call goes through debridPulseAuth.fetch. */
(() => {
  'use strict';

  const nativeFetch = window.fetch.bind(window);   // native API, never reassigned
  const mutatingMethods = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
  let csrfToken = '';
  let sessionState = null;
  let sessionRequest = null;
  let redirecting = false;

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
    if (sameOrigin && response.status === 401 && window.location.pathname !== '/login') {
      // A 401 from an application endpoint is not, by itself, proof that the
      // DebridPulse browser session expired. Confirm against the canonical
      // session endpoint before deciding whether navigation to /login is valid.
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
  });

  refreshSession().catch(() => {});
  window.setInterval(() => refreshSession({force: true}).catch(() => {}), 60000);
})();
