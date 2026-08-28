/* DebridPulse application-session bootstrap. Loaded before app.js. */
(() => {
  'use strict';

  const nativeFetch = window.fetch.bind(window);
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

  function syncSidebarSessionUi(data) {
    const footer = document.querySelector('.sidebar-footer');
    if (!footer) return;

    footer.querySelector('a[href="https://alldebrid.com"]')?.closest('.conn-row')?.remove();
    document.getElementById('aria2ng-row')?.remove();

    let row = document.getElementById('sidebar-auth-row');
    let stack = document.getElementById('sidebar-bottom-stack');
    if (!data?.authenticated) {
      row?.remove();
      if (stack) {
        if (footer.parentElement === stack) {
          stack.insertAdjacentElement('beforebegin', footer);
        }
        stack.remove();
      }
      return;
    }

    if (!stack) {
      stack = document.createElement('div');
      stack.id = 'sidebar-bottom-stack';
      stack.className = 'sidebar-bottom-stack';
      footer.insertAdjacentElement('beforebegin', stack);
      stack.appendChild(footer);
    } else if (footer.parentElement !== stack) {
      stack.prepend(footer);
    }

    if (!row) {
      row = document.createElement('div');
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
    }

    if (row.parentElement !== stack) stack.appendChild(row);
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
        redirectToLogin();
        return null;
      }
      if (!response.ok) return sessionState;
      const data = await response.json();
      sessionState = data;
      csrfToken = String(data && data.csrf_token || '');
      syncSidebarSessionUi(data);
      return data;
    }).catch(() => sessionState).finally(() => {
      sessionRequest = null;
    });
    return sessionRequest;
  }

  window.fetch = async function debridPulseFetch(input, init) {
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
      csrfToken = '';
      sessionState = null;
      syncSidebarSessionUi(null);
      redirectToLogin();
    }
    return response;
  };

  async function logoutSession() {
    const response = await window.fetch('/api/auth/logout', {method: 'POST'});
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
    refreshSession,
    session: () => sessionState,
    logout: logoutSession,
  });

  refreshSession().catch(() => {});
  window.setInterval(() => refreshSession({force: true}).catch(() => {}), 60000);

  // Authentication-specific Settings ownership now lives entirely in the
  // clean-room ui-settings-page.js runtime. Keep only independent auth assets:
  // auth-ux.css owns the authenticated sidebar stack and auth-help.js augments
  // Help documentation. The retired Settings augmentation runtimes are not loaded.
  if (!document.querySelector('link[data-debridpulse-auth-ux]')) {
    const style = document.createElement('link');
    style.rel = 'stylesheet';
    style.href = '/auth-ux.css?v=1';
    style.dataset.debridpulseAuthUx = 'true';
    document.head.appendChild(style);
  }

  const script = document.createElement('script');
  script.src = '/auth-help.js?v=1';
  script.async = false;
  document.head.appendChild(script);
})();
