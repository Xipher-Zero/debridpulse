from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'frontend' / 'static'


def read(rel):
    return (ROOT / rel).read_text()


def write(rel, text):
    (ROOT / rel).write_text(text)


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'{label}: expected one match, found {count}')
    return text.replace(old, new, 1)


def replace_between(text, start, end, replacement, label):
    a = text.find(start)
    if a < 0:
        raise RuntimeError(f'{label}: start marker missing')
    b = text.find(end, a + len(start))
    if b < 0:
        raise RuntimeError(f'{label}: end marker missing')
    return text[:a] + replacement.rstrip() + '\n\n' + text[b:]


settings_path = 'frontend/static/ui-settings-page.js'
text = read(settings_path)

# Generation counters now belong to the page owner instead of an API-wrapper
# sidecar. They prevent stale asynchronous auth/runtime responses from repainting
# a newer Settings navigation/load generation.
text = replace_once(
    text,
    "  const root = () => document.getElementById('view-settings');",
    "  let loadGeneration = 0;\n  let authGeneration = 0;\n\n  const root = () => document.getElementById('view-settings');",
    'settings generation state',
)

# Header-center semantic classes are part of the card contract rather than a
# post-render mutation concern.
text = replace_once(
    text,
    "          ${options.headerCenter ? `<div class=\"dp-settings-card-header-center\">${options.headerCenter}</div>` : ''}",
    "          ${options.headerCenter ? `<div class=\"dp-settings-card-header-center ${options.headerCenterClass || ''}\">${options.headerCenter}</div>` : ''}",
    'card header center class contract',
)

# Replace the entire authentication presentation region with direct final-state
# markup plus the resilience/callback behavior that genuinely belongs to this
# page owner.
auth_block = r'''  function fieldClass(markup, ...classes) {
    const className = classes.filter(Boolean).join(' ');
    if (!className) return markup;
    return markup.replace('class="dp-settings-field"', `class="dp-settings-field ${className}"`);
  }

  function authHeaderToggle(key, value, extraClass = '') {
    const id = fieldId(key);
    return `
      <label class="toggle-row dp-settings-toggle dp-settings-auth-header-enable ${html(extraClass)}" for="${id}">
        <span class="toggle-info"><span class="tl">Enable</span></span>
        <span class="toggle">
          <input id="${id}" data-setting="${html(key)}" type="checkbox" ${checked(value)}>
          <span class="ttrack"></span>
        </span>
      </label>`;
  }

  function oidcPolicyToggle(value) {
    const id = fieldId('oidc_allow_all');
    return `
      <label class="toggle-row dp-settings-toggle dp-settings-oidc-allow-all" for="${id}">
        <span class="toggle-info"><span class="tl">Allow Any Authenticated OIDC Identity</span></span>
        <span class="toggle">
          <input id="${id}" data-setting="oidc_allow_all" type="checkbox" ${checked(value)}>
          <span class="ttrack"></span>
        </span>
      </label>`;
  }

  function mechanismLabel(value) {
    const raw = String(value || '').trim();
    if (raw === 'password_session') return 'Password Session';
    if (raw === 'oidc_session') return 'OIDC Session';
    return raw || 'Open / anonymous';
  }

  function settingsActive() {
    return root()?.classList.contains('active') === true;
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

  function removeAuthUnavailableNotice() {
    root()?.querySelector('[data-panel="authentication"] .dp-settings-auth-unavailable')?.remove();
  }

  function markAuthUnavailable(error) {
    if (!settingsActive()) return;
    const panel = root()?.querySelector('[data-panel="authentication"]');
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

  function callbackFromPublicBase(value) {
    const raw = String(value ?? '').trim();
    if (!raw) return '';
    let parsed;
    try { parsed = new URL(raw); } catch (_) { return ''; }
    if (parsed.protocol !== 'https:' || !parsed.hostname) return '';
    if (parsed.username || parsed.password || parsed.search || parsed.hash) return '';
    if (parsed.pathname !== '/' && parsed.pathname !== '') return '';
    const origin = raw.endsWith('/') ? raw.slice(0, -1) : raw;
    return origin + '/auth/oidc/callback';
  }

  function updateOidcCallbackPreview() {
    const source = byId('dp-auth-public-base-url');
    const input = byId('dp-auth-oidc-callback');
    const button = root()?.querySelector('button[data-action="copy-oidc-callback"]');
    const field = input?.closest('.dp-settings-auth-callback-field');
    if (!source || !input || !button || !field) return;

    const callback = callbackFromPublicBase(source.value);
    input.value = callback;
    input.placeholder = callback ? '' : 'Set Public DebridPulse Base URL to display the Callback URL.';
    button.disabled = !callback;
    field.classList.toggle('is-callback-unavailable', !callback);
  }

  async function copyOidcCallback() {
    const source = byId('dp-auth-public-base-url');
    const input = byId('dp-auth-oidc-callback');
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

  function applyOidcRuntimeStatus(auth, available) {
    if (!settingsActive() || !auth?.oidc_enabled || !auth?.oidc_configured) return;
    const kpi = Array.from(root()?.querySelectorAll('.dp-settings-auth-kpi') || []).find(node => {
      return text(node.querySelector('.dhs-label')?.textContent).trim() === 'OIDC State';
    });
    const value = kpi?.querySelector('.dhs-val');
    if (!kpi || !value) return;
    const presentation = oidcStatePresentation(auth, available);
    value.replaceChildren(document.createTextNode(presentation.primary));
    if (presentation.secondary) {
      value.appendChild(document.createElement('br'));
      const secondary = document.createElement('span');
      secondary.className = 'dp-settings-auth-kpi-secondary';
      secondary.textContent = presentation.secondary;
      value.appendChild(secondary);
    }
    kpi.dataset.c = presentation.tone;
  }

  async function probeOidcRuntime(auth, generation) {
    if (!auth?.oidc_enabled || !auth?.oidc_configured) return;
    const identity = oidcIdentity(auth);
    try {
      const status = await request('GET', '/auth/oidc/runtime-status', undefined, 5000);
      if (generation !== authGeneration || identity !== oidcIdentity(state.auth)) return;
      state.auth = {...state.auth, oidc_available: status?.oidc_available};
      applyOidcRuntimeStatus(state.auth, status?.oidc_available);
    } catch (_) {
      if (generation !== authGeneration || identity !== oidcIdentity(state.auth)) return;
      state.auth = {...state.auth, oidc_available: false};
      applyOidcRuntimeStatus(state.auth, false);
    }
  }

  function acceptAuth(auth, {probe = true} = {}) {
    state.auth = auth;
    syncAuthIntoSettings(auth);
    authGeneration += 1;
    removeAuthUnavailableNotice();
    if (probe) void probeOidcRuntime(auth, authGeneration);
    return authGeneration;
  }

  function authStatusCard(a) {
    const modeRaw = String(a.mode || 'Unknown');
    const modeValue = modeRaw === 'OIDC'
      ? 'OpenID Connect'
      : modeRaw === 'No authentication'
        ? 'No Authentication'
        : modeRaw;

    const passwordOperational = !!a.password_enabled && !!a.password_ready;
    const oidcOperational = !!a.oidc_enabled && !!a.oidc_ready && a.oidc_available !== false;
    let modeTone = 'neutral';
    if (a.authentication_required) modeTone = passwordOperational || oidcOperational ? 'green' : 'red';

    let passwordValue = 'Not Configured';
    let passwordTone = 'neutral';
    if (a.password_configured) {
      if (!a.password_enabled) {
        passwordValue = 'Configured';
        passwordTone = 'yellow';
      } else if (a.password_ready) {
        passwordValue = 'Configured & Enabled';
        passwordTone = 'green';
      } else {
        passwordValue = 'Configuration Error';
        passwordTone = 'red';
      }
    } else if (a.password_enabled) {
      passwordValue = 'Configuration Error';
      passwordTone = 'red';
    }

    const oidcState = oidcStatePresentation(a);
    let tokenValue = 'Not Configured';
    let tokenTone = 'neutral';
    if (a.api_token_configured) {
      if (a.api_token_enabled) {
        tokenValue = 'Configured & Enabled';
        tokenTone = 'green';
      } else {
        tokenValue = 'Configured';
        tokenTone = 'yellow';
      }
    } else if (a.api_token_enabled) {
      tokenValue = 'Configuration Error';
      tokenTone = 'red';
    }

    const items = [
      ['Authentication Mode', modeValue, modeTone],
      ['Username & Password', passwordValue, passwordTone],
      ['OIDC State', oidcState.primary, oidcState.tone, oidcState.secondary],
      ['API Token', tokenValue, tokenTone],
    ];
    const lifetimeId = fieldId('auth_session_lifetime_hours');

    return card('Authentication Status', `
      <div class="dp-settings-status-grid dp-settings-auth-kpi-grid">
        ${items.map(([label, value, tone, secondary = '']) => `
          <div class="dash-hero-stat dp-settings-auth-kpi" data-c="${html(tone)}">
            <div class="dhs-body">
              <div class="dhs-label">${html(label)}</div>
              <div class="dhs-val">${html(value)}${secondary ? `<br><span class="dp-settings-auth-kpi-secondary">${html(secondary)}</span>` : ''}</div>
            </div>
          </div>`).join('')}
      </div>
      <div class="dp-settings-auth-session-row">
        <div class="dp-settings-status"><b>Active Browser Sessions</b><span>${html(a.session_count ?? 0)}</span></div>
        <div class="dp-settings-status"><b>Current Authentication Mechanism</b><span>${html(mechanismLabel(a.current_session_mechanism))}</span></div>
        <div class="dp-settings-field dp-settings-auth-session-lifetime dp-settings-auth-session-lifetime-polished">
          <label class="form-label" for="${lifetimeId}">Browser Session Lifetime</label>
          <span class="dp-settings-auth-duration-control">
            <input class="input" id="${lifetimeId}" data-setting="auth_session_lifetime_hours" type="number"
                   min="1" max="168" value="${html(a.session_lifetime_hours || 12)}" aria-label="Browser Session Lifetime in hours">
            <span class="dp-settings-auth-duration-unit" aria-hidden="true">hours</span>
          </span>
          <span class="form-hint">How long a browser login remains valid before sign-in is required again.</span>
        </div>
        <div class="dp-settings-actions dp-settings-auth-session-actions">
          <span class="form-label dp-settings-auth-action-label" aria-hidden="true">&nbsp;</span>
          <span class="dp-settings-auth-session-action-control">
            <button class="btn btn-ghost btn-sm" type="button" data-action="logout-session">Log Out Current Session</button>
          </span>
        </div>
      </div>
    `, {className: 'dp-settings-auth-status-card'});
  }

  function authenticationPanel(a) {
    const externalBase = a.public_base_url_env_override ? (a.public_base_url_effective || '') : (a.public_base_url || '');
    const publicBaseReadonly = !!a.public_base_url_env_override;
    const provider = a.oidc_provider_name || 'OpenID Connect';
    const scopes = Array.isArray(a.oidc_scopes) ? a.oidc_scopes.join(' ') : '';
    const lines = values => Array.isArray(values) ? values.join('\n') : '';
    const callback = callbackFromPublicBase(externalBase) || '';
    const usernameField = fieldClass(input('auth_username', 'Username', a.username || '', {
      autocomplete: 'username',
      placeholder: 'operator',
      hint: 'Username used for browser and HTTP Basic authentication.',
    }), 'dp-settings-auth-username-field');

    const credentials = card('Username & Password', `
      <div class="dp-settings-auth-credentials-row">
        ${usernameField}
        <div class="dp-settings-field">
          <label class="form-label" for="dp-auth-new-password">New Password</label>
          <input class="input" id="dp-auth-new-password" type="password" maxlength="4096" autocomplete="new-password"
                 placeholder="${html(a.password_configured ? 'Stored password configured. Blank keeps it.' : 'Set a password before enabling')}">
          <span class="form-hint">Leave blank to keep the current password. Enter a new password to replace it.</span>
        </div>
        <div class="dp-settings-actions dp-settings-auth-password-actions">
          <span class="form-label dp-settings-auth-action-label" aria-hidden="true">&nbsp;</span>
          <span class="dp-settings-auth-action-control">
            <button class="btn btn-danger btn-sm" type="button" data-action="clear-password" ${a.password_configured ? '' : 'disabled'}>Clear Stored Password</button>
          </span>
        </div>
      </div>
    `, {
      className: 'dp-settings-username-password-card',
      headerCenter: 'Configure local credentials for browser sign-in and HTTP Basic API access.',
      headerCenterClass: 'dp-settings-auth-header-copy dp-settings-auth-header-copy--credentials',
      action: authHeaderToggle('auth_password_enabled', a.password_enabled),
    });

    const publicBase = `
      <div class="dp-settings-field dp-settings-auth-public-base-field dp-settings-oidc-sandwich">
        <label class="form-label" for="dp-auth-public-base-url">Public DebridPulse Base URL</label>
        <input class="input" id="dp-auth-public-base-url" value="${html(externalBase)}"
               placeholder="https://download.example.com" ${publicBaseReadonly ? 'readonly' : ''}>
        <span class="form-hint">${publicBaseReadonly
          ? 'Managed by PUBLIC_BASE_URL. Used for secure browser sessions and OIDC callback generation.'
          : 'Externally reachable HTTPS address used for secure browser sessions and OIDC callback generation.'}</span>
      </div>`;

    const callbackField = `
      <div class="dp-settings-field dp-settings-auth-callback-field dp-settings-oidc-sandwich ${callback ? '' : 'is-callback-unavailable'}">
        <label class="form-label" for="dp-auth-oidc-callback">OIDC Callback URL</label>
        <div class="dp-settings-inline-field dp-settings-oidc-callback-control">
          <input class="input" id="dp-auth-oidc-callback" value="${html(callback)}" readonly aria-readonly="true" autocomplete="off"
                 placeholder="${callback ? '' : 'Set Public DebridPulse Base URL to display the Callback URL.'}">
          <button class="btn btn-ghost btn-sm" type="button" data-action="copy-oidc-callback"
                  aria-label="Copy OIDC Callback URL" ${callback ? '' : 'disabled'}>Copy</button>
        </div>
        <span class="form-hint">Copy this exact URL into your identity provider's redirect/callback URI configuration.</span>
      </div>`;

    const providerField = fieldClass(input('oidc_provider_name', 'Provider Name', provider, {
      hint: 'Name shown on the sign-in page.',
    }), 'dp-settings-oidc-sandwich');
    const issuerField = fieldClass(input('oidc_issuer_url', 'Issuer URL', a.oidc_issuer_url || '', {
      placeholder: 'https://id.example/application/o/debridpulse',
      hint: 'OIDC issuer URL published by your identity provider.',
    }), 'dp-settings-oidc-sandwich');
    const clientIdField = fieldClass(input('oidc_client_id', 'Client ID', a.oidc_client_id || '', {
      hint: 'Client identifier issued by your OIDC provider.',
    }), 'dp-settings-oidc-sandwich');
    const scopesField = fieldClass(input('oidc_scopes', 'Scopes', scopes, {
      placeholder: 'openid profile email',
      hint: 'Space-separated scopes requested during sign-in.',
    }), 'dp-settings-oidc-sandwich');
    const groupClaimField = fieldClass(input('oidc_group_claim', 'Group Claim', a.oidc_group_claim || 'groups', {
      hint: 'Claim containing group memberships used by group authorization rules.',
    }), 'dp-settings-oidc-sandwich');

    const secretFieldMarkup = `
      <div class="dp-settings-field dp-settings-oidc-sandwich">
        <label class="form-label" for="dp-auth-oidc-secret">Client Secret</label>
        <input class="input" id="dp-auth-oidc-secret" type="password" autocomplete="off"
               placeholder="${html(a.oidc_client_secret_configured ? 'Stored Client Secret Configured. Blank keeps it.' : 'Optional for public clients')}">
        <span class="form-hint">Leave blank to keep the stored secret. Enter a new value to replace it.</span>
      </div>`;

    const clearSecret = `
      <div class="dp-settings-oidc-clear-secret-action">
        <span class="form-label dp-settings-oidc-clear-secret-spacer">Clear Stored Secret</span>
        <div class="dp-settings-oidc-clear-secret-control">
          <label class="dp-settings-oidc-clear-secret ${a.oidc_client_secret_configured ? '' : 'is-disabled'}">
            <span class="dp-settings-oidc-clear-secret-copy">Clear Stored Secret</span>
            <input id="dp-auth-clear-oidc-secret" type="checkbox"
                   ${a.oidc_client_secret_configured ? '' : 'disabled aria-disabled="true"'}>
          </label>
        </div>
        <small class="dp-settings-oidc-clear-secret-hint">${a.oidc_client_secret_configured
          ? 'Remove the saved secret when settings are applied.'
          : 'No stored client secret is configured.'}</small>
      </div>`;

    const subjects = fieldClass(textarea('oidc_allowed_subjects', 'Allowed Subjects', lines(a.oidc_allowed_subjects), {
      rows: 3,
      hint: 'Authorize matching OIDC subject identifiers, one per line.',
    }), 'dp-settings-oidc-sandwich');
    const emails = fieldClass(textarea('oidc_allowed_emails', 'Allowed Emails', lines(a.oidc_allowed_emails), {
      rows: 3,
      hint: 'Authorize verified email addresses, one per line. Requires email_verified=true.',
    }), 'dp-settings-oidc-sandwich');
    const groups = fieldClass(textarea('oidc_allowed_groups', 'Allowed Groups', lines(a.oidc_allowed_groups), {
      rows: 3,
      hint: 'Authorize identities belonging to matching OIDC groups, one per line.',
    }), 'dp-settings-oidc-sandwich');

    const oidc = card('OpenID Connect', `
      <div class="dp-settings-oidc-row dp-settings-oidc-row--origin">${publicBase}${callbackField}</div>
      <div class="dp-settings-oidc-row dp-settings-oidc-row--identity">${providerField}${issuerField}</div>
      <div class="dp-settings-oidc-row dp-settings-oidc-row--credentials">${clientIdField}${secretFieldMarkup}${clearSecret}</div>
      <div class="dp-settings-oidc-row dp-settings-oidc-row--protocol">${scopesField}${groupClaimField}</div>
      <section class="dp-settings-oidc-access">
        <div class="dp-settings-oidc-section-heading">
          <span class="dp-settings-oidc-section-title">Access Control</span>
          <small class="dp-settings-oidc-section-copy">Choose whether any authenticated OIDC identity is accepted or restrict sign-in to the allowlists below.</small>
          ${oidcPolicyToggle(a.oidc_allow_all)}
        </div>
        <div class="dp-settings-oidc-allowlists">${subjects}${emails}${groups}</div>
      </section>
    `, {
      className: 'dp-settings-oidc-card dp-settings-oidc-grouped-card',
      headerCenter: 'Configure an external identity provider for browser sign-in.',
      headerCenterClass: 'dp-settings-auth-header-copy dp-settings-oidc-header-copy',
      action: authHeaderToggle('auth_oidc_enabled', a.oidc_enabled, 'dp-settings-oidc-header-enable'),
    });

    const configured = !!a.api_token_configured;
    const tokenLayoutClass = state.oneTimeToken ? 'dp-settings-api-token-layout has-token' : 'dp-settings-api-token-layout';
    const apiAccess = card('API Access', `
      <div class="${tokenLayoutClass}">
        <div class="dp-settings-actions dp-settings-api-token-actions">
          <button class="btn btn-blue btn-sm dp-settings-api-token-generate" type="button" data-action="generate-token">${configured ? 'Rotate Token' : 'Generate Token'}</button>
          <button class="btn btn-danger btn-sm dp-settings-api-token-revoke" type="button" data-action="clear-token" ${configured ? '' : 'disabled'}>Revoke Token</button>
        </div>
        <p class="dp-settings-copy dp-settings-api-token-status">Stored Token: <b>${configured ? 'Configured' : 'Not Configured'}</b></p>
        ${state.oneTimeToken ? `
          <b class="dp-settings-api-token-warning">Copy this token now. DebridPulse will not display it again.</b>
          <div class="dp-settings-inline-field dp-settings-api-token-field">
            <input class="input" id="dp-settings-api-token-once" readonly value="${html(state.oneTimeToken)}">
            <button class="btn btn-ghost btn-sm" type="button" data-action="copy-token">Copy</button>
          </div>` : ''}
      </div>
    `, {
      className: 'dp-settings-api-access-card',
      headerCenter: 'Use a dedicated bearer token for automation, monitoring, and API integrations.',
      headerCenterClass: 'dp-settings-auth-header-copy dp-settings-auth-header-copy--api',
      action: authHeaderToggle('api_token_enabled', a.api_token_enabled),
    });

    return authStatusCard(a) + credentials + oidc + apiAccess;
  }'''

text = replace_between(
    text,
    '  function authStatusCard(a) {',
    '  function maintenancePanel(s) {',
    auth_block,
    'canonical authentication region',
)

# OIDC verification is a contextual Settings action in the final layout.
text = replace_once(
    text,
    '            <button class="btn btn-ghost" type="button" data-context-action="notifications" data-action="test-discord">Test Discord</button>\n',
    '            <button class="btn btn-ghost" type="button" data-context-action="notifications" data-action="test-discord">Test Discord</button>\n'
    '            <button class="btn btn-ghost" type="button" data-context-action="authentication" data-action="verify-oidc">Test OIDC Sign-In</button>\n',
    'authentication contextual action',
)

# The callback relationship is live form behavior owned by the page; no
# post-render callback sidecar is necessary.
text = replace_once(
    text,
    "    view.addEventListener('change', event => {\n      if (event.target.matches(`[data-setting=\"aria2_mode\"]`)) updateModeState();",
    "    view.addEventListener('input', event => {\n"
    "      if (event.target.id === 'dp-auth-public-base-url') updateOidcCallbackPreview();\n"
    "    });\n\n"
    "    view.addEventListener('change', event => {\n"
    "      if (event.target.matches(`[data-setting=\"aria2_mode\"]`)) updateModeState();\n"
    "      if (event.target.id === 'dp-auth-public-base-url') updateOidcCallbackPreview();",
    'callback draft lifecycle',
)
text = replace_once(
    text,
    "      else if (action === 'copy-token') copyToken();\n      else if (action === 'logout-session') logoutSession(button);",
    "      else if (action === 'copy-token') copyToken();\n"
    "      else if (action === 'copy-oidc-callback') copyOidcCallback();\n"
    "      else if (action === 'logout-session') logoutSession(button);",
    'callback copy action',
)
text = replace_once(
    text,
    "    updateModeState();\n    document.dispatchEvent(new CustomEvent('debridpulse:settings-rendered', {detail:{tab: state.activeTab}}));",
    "    updateModeState();\n"
    "    updateOidcCallbackPreview();\n"
    "    document.dispatchEvent(new CustomEvent('debridpulse:settings-rendered', {detail:{tab: state.activeTab}}));",
    'callback initial render',
)

# Auth writes and OIDC completion use the same owner state/probe path.
text = replace_once(
    text,
    "      state.auth = await request('PUT', '/auth/config', payload, 15000);\n      syncAuthIntoSettings(state.auth);\n      state.activeTab = 'authentication';\n      renderPreservingViewport();",
    "      const auth = await request('PUT', '/auth/config', payload, 15000);\n"
    "      const generation = acceptAuth(auth, {probe: false});\n"
    "      state.activeTab = 'authentication';\n"
    "      renderPreservingViewport();\n"
    "      void probeOidcRuntime(auth, generation);",
    'persist auth owner state',
)
text = replace_once(
    text,
    "      state.auth = await request('GET', '/auth/config', undefined, 7000);\n      syncAuthIntoSettings(state.auth);\n      state.activeTab = 'authentication';\n      renderPreservingViewport();",
    "      const auth = await request('GET', '/auth/config', undefined, 7000);\n"
    "      const generation = acceptAuth(auth, {probe: false});\n"
    "      state.activeTab = 'authentication';\n"
    "      renderPreservingViewport();\n"
    "      void probeOidcRuntime(auth, generation);",
    'OIDC completion owner state',
)

load_block = r'''  async function load() {
    document.getElementById('content')?.classList.remove('settings-active');

    if (state.loading) return state.loading;
    const generation = ++loadGeneration;
    state.loading = (async () => {
      const view = root();
      if (!view) return;
      view.classList.add('dp-settings-clean-view');
      view.innerHTML = '<div class="dp-settings-loading">Loading Settings…</div>';

      const settingsPromise = request('GET', '/settings', undefined, 10000);
      const authPromise = request('GET', '/auth/config', undefined, 7000);

      let settings;
      try {
        settings = await settingsPromise;
      } catch (error) {
        if (generation !== loadGeneration || !settingsActive()) return;
        view.innerHTML = `<div class="dp-settings-load-error"><b>Settings could not be loaded.</b><span>${html(error.message)}</span></div>`;
        notify(error.message, 'error');
        return;
      }

      if (generation !== loadGeneration || !settingsActive()) return;
      syncGlobalSettings(settings);
      state.auth = fallbackAuthFromSettings(settings);
      syncAuthIntoSettings(state.auth);
      render();

      void authPromise.then(auth => {
        if (generation !== loadGeneration || !settingsActive()) return;
        const authGen = acceptAuth(auth, {probe: false});
        state.activeTab = state.activeTab || 'sources';
        renderPreservingViewport();
        void probeOidcRuntime(auth, authGen);
      }).catch(error => {
        if (generation !== loadGeneration) return;
        markAuthUnavailable(error);
      });
    })().finally(() => {
      state.loading = null;
    });
    return state.loading;
  }'''
text = replace_between(
    text,
    '  async function load() {',
    '  // Transitional navigation hook only:',
    load_block,
    'resilient settings load owner',
)

# Architecture comments should describe the final owner, not a migration stack.
text = text.replace(
    ' * This runtime deliberately does not consume the inherited Settings renderer,\n * serializer, tab lifecycle, authentication augmentation, or Settings DOM.\n * The backend APIs are the only migration contract.\n',
    ' * This runtime owns the Settings shell, forms, serialization, authentication\n * presentation/resilience, OIDC verification, and Settings interaction lifecycle.\n * Backend APIs are the page contract; no legacy Settings renderer is involved.\n',
    1,
)
text = text.replace(
    '  // Transitional navigation hook only: app.js owns generic navigation and still\n  // calls loadSettings(). Replace that one entry point with the clean-room page.\n  // No inherited Settings renderer, serializer, tab lifecycle, or action function\n  // is invoked by this runtime.\n',
    '  // app.js owns generic navigation and calls this canonical Settings entry point.\n',
    1,
)

# Normalize whitespace produced by region replacement.
text = '\n'.join(line.rstrip() for line in text.splitlines()) + '\n'
write(settings_path, text)

# Consolidate authentication mini-polish CSS into its canonical component owner.
auth_css_path = 'frontend/static/ui-settings-authentication.css'
polish_css_path = ROOT / 'frontend/static/ui-settings-authentication-polish.css'
auth_css = read(auth_css_path).rstrip() + '\n\n'
polish_css = polish_css_path.read_text()
polish_css = polish_css.replace('/* DebridPulse v1.0.11 Authentication mini-polish. */', '/* Canonical authentication field/duration details. */', 1)
polish_css = polish_css.replace('var(--dp-settings-auth-input-text-inset, 0px)', 'var(--dp-settings-auth-input-text-inset, 3px)')
auth_css += polish_css.strip() + '\n'
write(auth_css_path, auth_css)

# The OIDC CSS is semantically scoped and remains a component owner; only its
# iterative-era header wording is retired.
oidc_css_path = 'frontend/static/ui-settings-authentication-oidc.css'
oidc_css = read(oidc_css_path).replace(
    '/* DebridPulse v1.0.11 OpenID Connect Settings regrouping pass. */',
    '/* DebridPulse OpenID Connect Settings component. */',
    1,
)
write(oidc_css_path, oidc_css)

style_path = 'frontend/static/style-v11.css'
style = read(style_path)
style = style.replace("@import url('/ui-settings-authentication-polish.css?v=2');\n", '')
write(style_path, style)

# Remove superseded authentication sidecars from the loader and product tree.
index_path = 'frontend/static/index.html'
index = read(index_path)
for script in (
    'ui-settings-auth-resilience.js',
    'ui-settings-authentication.js',
    'ui-settings-authentication-polish.js',
    'ui-settings-authentication-oidc.js',
    'ui-settings-authentication-callback.js',
):
    pattern = re.compile(r'^\s*<script[^>]+src="/' + re.escape(script) + r'[^>]*></script>\s*\n?', re.M)
    index, count = pattern.subn('', index, count=1)
    if count != 1:
        raise RuntimeError(f'expected loaded sidecar exactly once: {script}')
write(index_path, index)

for rel in (
    'frontend/static/ui-settings-auth-resilience.js',
    'frontend/static/ui-settings-authentication.js',
    'frontend/static/ui-settings-authentication-polish.js',
    'frontend/static/ui-settings-authentication-oidc.js',
    'frontend/static/ui-settings-authentication-callback.js',
    'frontend/static/ui-settings-authentication-polish.css',
):
    p = ROOT / rel
    if not p.exists():
        raise RuntimeError(f'expected superseded file missing before retirement: {rel}')
    p.unlink()

# Static ownership guards.
index = read(index_path)
settings = read(settings_path)
style = read(style_path)
for forbidden in (
    'ui-settings-auth-resilience.js',
    'ui-settings-authentication.js',
    'ui-settings-authentication-polish.js',
    'ui-settings-authentication-oidc.js',
    'ui-settings-authentication-callback.js',
):
    if forbidden in index:
        raise RuntimeError(f'superseded auth sidecar remains loaded: {forbidden}')
if 'ui-settings-authentication-polish.css' in style:
    raise RuntimeError('superseded authentication polish CSS remains imported')

required = (
    'function fallbackAuthFromSettings(settings)',
    'function callbackFromPublicBase(value)',
    'function authenticationPanel(a)',
    'dp-settings-oidc-row--credentials',
    'data-action="copy-oidc-callback"',
    'data-context-action="authentication" data-action="verify-oidc"',
    'function probeOidcRuntime(auth, generation)',
)
for token in required:
    if token not in settings:
        raise RuntimeError(f'canonical Settings auth ownership token missing: {token}')

for match in re.finditer(r"@import url\('/([^?']+)", style):
    if not (STATIC / match.group(1)).exists():
        raise RuntimeError(f'broken style import after auth consolidation: {match.group(1)}')

print('canonical Settings authentication ownership applied')
