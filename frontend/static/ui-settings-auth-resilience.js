/* DebridPulse v1.0.11 Settings authentication failure containment.
 *
 * Settings rendering is owned by ui-settings-page.js. This runtime keeps the
 * authentication enrichment path out of the page bootstrap critical path:
 * /settings is sufficient to paint the Settings workspace, while the local
 * /auth/config payload and live OIDC runtime status are layered in afterward.
 */
(function () {
  'use strict';

  const baseLoadSettings = window.loadSettings;
  const baseApi = window.api;
  if (typeof baseLoadSettings !== 'function' || typeof baseApi !== 'function') return;

  let loadGeneration = 0;
  let authGeneration = 0;
  let latestSettings = null;
  let latestAuth = null;

  const text = value => String(value ?? '');
  const oidcStatePresentation = window.DPSettingsOidcStatePresentation;

  function settingsActive() {
    return document.getElementById('view-settings')?.classList.contains('active') === true;
  }

  function oidcIdentity(auth) {
    return [
      auth?.oidc_enabled ? '1' : '0',
      text(auth?.oidc_issuer_url),
      text(auth?.oidc_client_id),
      text(auth?.public_base_url_effective || auth?.public_base_url),
    ].join('|');
  }

  function fallbackAuthFromSettings(settings) {
    const passwordEnabled = !!settings?.auth_password_enabled;
    const username = text(settings?.auth_username).trim();
    const oidcEnabled = !!settings?.auth_oidc_enabled;
    const issuer = text(settings?.oidc_issuer_url).trim();
    const clientId = text(settings?.oidc_client_id).trim();
    const publicBase = text(settings?.public_base_url).trim();
    const oidcConfigured = oidcEnabled || !!(issuer && clientId && publicBase);
    let mode = 'No authentication';
    if (passwordEnabled && oidcEnabled) mode = 'Username & Password + OIDC';
    else if (passwordEnabled) mode = 'Username & Password';
    else if (oidcEnabled) mode = 'OIDC';

    return {
      mode,
      authentication_required: passwordEnabled || oidcEnabled,
      password_enabled: passwordEnabled,
      password_ready: passwordEnabled && !!username,
      password_configured: passwordEnabled,
      username,
      session_lifetime_hours: Number(settings?.auth_session_lifetime_hours || 12),
      oidc_enabled: oidcEnabled,
      oidc_configured: oidcConfigured,
      oidc_ready: oidcEnabled && oidcConfigured,
      oidc_available: null,
      oidc_verified: false,
      oidc_verified_at: null,
      oidc_provider_name: text(settings?.oidc_provider_name || 'OpenID Connect'),
      oidc_issuer_url: issuer,
      oidc_client_id: clientId,
      oidc_client_secret_configured: false,
      oidc_scopes: Array.isArray(settings?.oidc_scopes) ? settings.oidc_scopes.slice() : [],
      oidc_allow_all: !!settings?.oidc_allow_all,
      oidc_allowed_subjects: Array.isArray(settings?.oidc_allowed_subjects) ? settings.oidc_allowed_subjects.slice() : [],
      oidc_allowed_emails: Array.isArray(settings?.oidc_allowed_emails) ? settings.oidc_allowed_emails.slice() : [],
      oidc_allowed_groups: Array.isArray(settings?.oidc_allowed_groups) ? settings.oidc_allowed_groups.slice() : [],
      oidc_group_claim: text(settings?.oidc_group_claim || 'groups'),
      public_base_url: publicBase,
      public_base_url_effective: publicBase,
      public_base_url_env_override: false,
      oidc_callback_url: '',
      api_token_enabled: false,
      api_token_configured: false,
      current_session_mechanism: null,
      session_count: 0,
    };
  }

  async function renderWithSnapshots(settings, auth) {
    if (!settingsActive()) return;
    const previousApi = window.api;
    const scopedApi = function (method, path, body, timeout, options) {
      if (method === 'GET' && path === '/settings') return Promise.resolve(settings);
      if (method === 'GET' && path === '/auth/config') return Promise.resolve(auth);
      return previousApi(method, path, body, timeout, options);
    };

    window.api = scopedApi;
    try { api = scopedApi; } catch (_) {}
    try {
      await baseLoadSettings();
    } finally {
      if (window.api === scopedApi) window.api = previousApi;
      try { if (api === scopedApi) api = previousApi; } catch (_) {}
    }
  }

  function removeAuthUnavailableNotice() {
    document.querySelector('#view-settings [data-panel="authentication"] .dp-settings-auth-unavailable')?.remove();
  }

  function markAuthUnavailable(error) {
    if (!settingsActive()) return;
    const panel = document.querySelector('#view-settings [data-panel="authentication"]');
    if (!panel) return;
    let notice = panel.querySelector('.dp-settings-auth-unavailable');
    if (!notice) {
      notice = document.createElement('div');
      notice.className = 'dp-settings-caution dp-settings-auth-unavailable';
      panel.prepend(notice);
    }
    notice.replaceChildren();
    const title = document.createElement('b');
    title.textContent = 'Authentication status unavailable';
    const detail = document.createElement('span');
    detail.textContent = text(error?.message || error || 'The local authentication status request failed. Other Settings remain available.');
    notice.append(title, detail);
  }

  function renderOidcKpiValue(value, presentation) {
    value.replaceChildren(document.createTextNode(presentation.primary));
    if (!presentation.secondary) return;
    value.appendChild(document.createElement('br'));
    const secondary = document.createElement('span');
    secondary.className = 'dp-settings-auth-kpi-secondary';
    secondary.textContent = presentation.secondary;
    value.appendChild(secondary);
  }

  function applyOidcRuntimeStatus(auth, available) {
    if (!settingsActive() || !auth?.oidc_enabled || !auth?.oidc_configured) return;
    const kpi = Array.from(document.querySelectorAll('#view-settings .dp-settings-auth-kpi')).find(node => {
      return text(node.querySelector('.dhs-label')?.textContent).trim() === 'OIDC State';
    });
    if (!kpi) return;
    const value = kpi.querySelector('.dhs-val');
    if (!value) return;

    const presentation = oidcStatePresentation?.resolve?.(auth, available);
    if (!presentation) {
      // Normal-state copy remains exclusively owned by the Settings resolver.
      // Keep only the resilience-critical provider-unavailable fallback here so
      // a missing presentation asset cannot make an unavailable runtime look healthy.
      if (available === false) {
        value.textContent = auth.oidc_verified ? 'Verified · Runtime Unavailable' : 'Runtime Unavailable';
        kpi.dataset.c = 'red';
      }
      return;
    }
    renderOidcKpiValue(value, presentation);
    kpi.dataset.c = presentation.tone;
  }

  async function probeOidcRuntime(auth, generation) {
    if (!auth?.oidc_enabled || !auth?.oidc_configured) return;
    const identity = oidcIdentity(auth);
    try {
      const status = await baseApi('GET', '/auth/oidc/runtime-status', undefined, 5000);
      if (generation !== authGeneration || identity !== oidcIdentity(latestAuth)) return;
      latestAuth = {...latestAuth, oidc_available: status?.oidc_available};
      applyOidcRuntimeStatus(latestAuth, status?.oidc_available);
    } catch (_) {
      if (generation !== authGeneration || identity !== oidcIdentity(latestAuth)) return;
      latestAuth = {...latestAuth, oidc_available: false};
      applyOidcRuntimeStatus(latestAuth, false);
    }
  }

  function acceptAuth(auth) {
    latestAuth = auth;
    authGeneration += 1;
    removeAuthUnavailableNotice();
    void probeOidcRuntime(auth, authGeneration);
  }

  async function observedApi(method, path, body, timeout, options) {
    const result = await baseApi(method, path, body, timeout, options);
    if (path === '/auth/config' && (method === 'GET' || method === 'PUT')) {
      acceptAuth(result);
    }
    return result;
  }

  async function resilientLoadSettings() {
    const generation = ++loadGeneration;
    const settingsPromise = baseApi('GET', '/settings', undefined, 10000);
    const authPromise = baseApi('GET', '/auth/config', undefined, 7000);

    let settings;
    try {
      settings = await settingsPromise;
    } catch (error) {
      if (generation !== loadGeneration || !settingsActive()) return;
      const view = document.getElementById('view-settings');
      if (view) {
        view.classList.add('dp-settings-clean-view');
        view.innerHTML = '<div class="dp-settings-load-error"><b>Settings could not be loaded.</b><span></span></div>';
        const detail = view.querySelector('.dp-settings-load-error span');
        if (detail) detail.textContent = text(error?.message || error);
      }
      return;
    }

    if (generation !== loadGeneration || !settingsActive()) return;
    latestSettings = settings;

    // The Settings workspace paints from the local /settings snapshot. The
    // authentication request has already started, but it is deliberately not
    // awaited here and therefore cannot hold the shell or navigation hostage.
    await renderWithSnapshots(settings, fallbackAuthFromSettings(settings));
    if (generation !== loadGeneration || !settingsActive()) return;

    authPromise.then(async auth => {
      if (generation !== loadGeneration) return;
      acceptAuth(auth);
      if (!settingsActive()) return;
      await renderWithSnapshots(latestSettings, auth);
      if (!settingsActive()) return;
      applyOidcRuntimeStatus(latestAuth, latestAuth?.oidc_available);
    }).catch(error => {
      if (generation !== loadGeneration) return;
      markAuthUnavailable(error);
    });
  }

  window.api = observedApi;
  try { api = observedApi; } catch (_) {}
  window.loadSettings = resilientLoadSettings;
  try { loadSettings = resilientLoadSettings; } catch (_) {}

  window.DPSettingsAuthResilience = Object.freeze({
    load: resilientLoadSettings,
  });
})();
