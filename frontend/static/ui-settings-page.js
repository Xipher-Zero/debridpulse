/* DebridPulse v1.0.11 clean-room Settings page.
 *
 * This runtime owns the Settings shell, forms, serialization, authentication
 * presentation/resilience, OIDC verification, and Settings interaction lifecycle.
 * Backend APIs are the page contract; no legacy Settings renderer is involved.
 */
(function () {
  'use strict';

  const TABS = Object.freeze([
    ['sources', 'Sources & Providers', 'zap'],
    ['downloads', 'Downloads', 'download'],
    ['extraction', 'Extraction', 'package-open'],
    ['notifications', 'Notifications', 'bell'],
    ['authentication', 'Authentication', 'shield-check'],
    ['maintenance', 'Data & Maintenance', 'database-backup'],
  ]);

  const state = {
    settings: null,
    auth: null,
    activeTab: 'sources',
    oneTimeToken: '',
    loading: null,
    oidc: {
      popup: null,
      button: null,
      channel: null,
      poll: null,
      messageHandler: null,
      completed: false,
    },
  };

  let loadGeneration = 0;
  let authGeneration = 0;

  const root = () => document.getElementById('view-settings');
  const byId = id => document.getElementById(id);
  const text = value => String(value ?? '');
  const html = value => text(value).replace(/[&<>"']/g, ch => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[ch]);
  const checked = value => value ? 'checked' : '';
  const selected = (value, expected) => String(value) === String(expected) ? 'selected' : '';

  function oidcStatePresentation(auth, available = auth?.oidc_available) {
    if (!auth?.oidc_configured) {
      return auth?.oidc_enabled
        ? {primary: 'Configuration Error', secondary: '', tone: 'red'}
        : {primary: 'Disabled', secondary: '', tone: 'neutral'};
    }
    if (!auth?.oidc_enabled) {
      return {primary: 'Configured', secondary: '', tone: 'yellow'};
    }
    if (available === false) {
      return {
        primary: auth?.oidc_verified ? 'Verified · Runtime Unavailable' : 'Runtime Unavailable',
        secondary: '',
        tone: 'red',
      };
    }
    if (auth?.oidc_verified) {
      return {primary: 'Enabled', secondary: '', tone: 'green'};
    }
    if (auth?.oidc_ready) {
      return {
        primary: 'Configured & Enabled',
        secondary: '(Untested)',
        tone: 'yellow',
      };
    }
    return {primary: 'Configuration Error', secondary: '', tone: 'red'};
  }

  window.DPSettingsOidcStatePresentation = Object.freeze({resolve: oidcStatePresentation});

  function notify(message, kind = 'info') {
    if (typeof toast === 'function') {
      toast(String(message), kind);
    } else {
      console[kind === 'error' ? 'error' : 'log']('[DebridPulse Settings]', message);
    }
  }

  async function request(method, path, body, timeout) {
    if (typeof api !== 'function') throw new Error('Application API client is unavailable');
    return api(method, path, body, timeout);
  }

  function setBusy(button, busy, label) {
    if (!button) return;
    if (busy) {
      if (!button.dataset.dpSettingsMarkup) button.dataset.dpSettingsMarkup = button.innerHTML;
      button.disabled = true;
      if (label) button.textContent = label;
      return;
    }
    button.disabled = false;
    if (button.dataset.dpSettingsMarkup) {
      button.innerHTML = button.dataset.dpSettingsMarkup;
      delete button.dataset.dpSettingsMarkup;
    }
  }


  function captureSettingsViewport() {
    const settingsScroller = root()?.querySelector('.dp-settings-scroll');
    const shellScroller = document.getElementById('content');
    return {
      settingsTop: Number(settingsScroller?.scrollTop || 0),
      shellTop: Number(shellScroller?.scrollTop || 0),
      windowTop: Number(window.scrollY || 0),
    };
  }

  function restoreSettingsViewport(snapshot) {
    if (!snapshot) return;
    const settingsScroller = root()?.querySelector('.dp-settings-scroll');
    const shellScroller = document.getElementById('content');
    if (settingsScroller) settingsScroller.scrollTop = snapshot.settingsTop;
    if (shellScroller) shellScroller.scrollTop = snapshot.shellTop;
    if (typeof window.scrollTo === 'function') {
      try {
        window.scrollTo({top: snapshot.windowTop, left: window.scrollX || 0, behavior: 'auto'});
      } catch (_) {
        window.scrollTo(0, snapshot.windowTop);
      }
    }
  }

  function renderPreservingViewport() {
    const snapshot = captureSettingsViewport();
    render();
    restoreSettingsViewport(snapshot);
    if (typeof window.requestAnimationFrame === 'function') {
      window.requestAnimationFrame(() => restoreSettingsViewport(snapshot));
    }
  }

  async function confirmAction({
    title,
    message,
    confirmLabel = 'Confirm',
    tone = 'warning',
    typedPhrase = '',
  }) {
    return new Promise(resolve => {
      const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      const overlay = document.createElement('div');
      const dialogId = `dp-settings-confirm-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const titleId = `${dialogId}-title`;
      const messageId = `${dialogId}-message`;
      overlay.className = 'dp-settings-confirm-overlay';
      overlay.innerHTML = `
        <section class="dp-settings-confirm-dialog" data-tone="${tone === 'danger' ? 'danger' : 'warning'}"
                 role="alertdialog" aria-modal="true" aria-labelledby="${titleId}" aria-describedby="${messageId}">
          <header class="dp-settings-confirm-header">
            <div class="dp-settings-confirm-title" id="${titleId}"></div>
          </header>
          <div class="dp-settings-confirm-body">
            <p class="dp-settings-confirm-message" id="${messageId}"></p>
            <label class="dp-settings-confirm-typed" ${typedPhrase ? '' : 'hidden'}>
              <span class="form-label"></span>
              <input class="input" type="text" autocomplete="off" spellcheck="false">
            </label>
          </div>
          <footer class="dp-settings-confirm-footer">
            <button class="btn btn-ghost" type="button" data-confirm-cancel>Cancel</button>
            <button class="btn ${tone === 'danger' ? 'btn-danger' : 'btn-primary'}" type="button" data-confirm-accept></button>
          </footer>
        </section>`;

      const dialog = overlay.querySelector('.dp-settings-confirm-dialog');
      const titleEl = overlay.querySelector('.dp-settings-confirm-title');
      const messageEl = overlay.querySelector('.dp-settings-confirm-message');
      const typed = overlay.querySelector('.dp-settings-confirm-typed');
      const typedLabel = typed?.querySelector('.form-label');
      const typedInput = typed?.querySelector('input');
      const cancel = overlay.querySelector('[data-confirm-cancel]');
      const accept = overlay.querySelector('[data-confirm-accept]');

      titleEl.textContent = String(title || 'Confirm action');
      messageEl.textContent = String(message || '');
      accept.textContent = String(confirmLabel || 'Confirm');

      if (typedPhrase) {
        typed.hidden = false;
        typedLabel.textContent = `Type ${typedPhrase} to confirm.`;
        typedInput.placeholder = typedPhrase;
        accept.disabled = true;
        typedInput.addEventListener('input', () => {
          accept.disabled = typedInput.value !== typedPhrase;
        });
        typedInput.addEventListener('keydown', event => {
          if (event.key === 'Enter' && !accept.disabled) {
            event.preventDefault();
            accept.click();
          }
        });
      }

      let settled = false;
      const finish = value => {
        if (settled) return;
        settled = true;
        overlay.remove();
        if (!document.querySelector('.dp-settings-confirm-overlay')) {
          document.body.classList.remove('dp-settings-confirm-open');
        }
        if (previousFocus?.isConnected) {
          try { previousFocus.focus(); } catch (_) {}
        }
        resolve(value);
      };

      cancel.addEventListener('click', () => finish(false));
      accept.addEventListener('click', () => finish(true));
      overlay.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
          event.preventDefault();
          finish(false);
          return;
        }
        if (event.key !== 'Tab') return;
        const focusable = Array.from(dialog.querySelectorAll('button:not([disabled]), input:not([disabled])'));
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
      });

      document.body.appendChild(overlay);
      document.body.classList.add('dp-settings-confirm-open');
      if (typeof window.requestAnimationFrame === 'function') {
        window.requestAnimationFrame(() => cancel.focus());
      } else {
        cancel.focus();
      }
    });
  }

  function syncGlobalSettings(data) {
    state.settings = data;
    try { settingsData = data; } catch (_) {}
  }

  function syncAuthIntoSettings(authData) {
    if (!state.settings || !authData) return;
    Object.assign(state.settings, {
      auth_password_enabled: !!authData.password_enabled,
      auth_username: text(authData.username),
      auth_session_lifetime_hours: Number(authData.session_lifetime_hours || 12),
      auth_oidc_enabled: !!authData.oidc_enabled,
      oidc_provider_name: text(authData.oidc_provider_name || 'OpenID Connect'),
      oidc_issuer_url: text(authData.oidc_issuer_url),
      oidc_client_id: text(authData.oidc_client_id),
      oidc_scopes: Array.isArray(authData.oidc_scopes) ? authData.oidc_scopes.slice() : [],
      oidc_allow_all: !!authData.oidc_allow_all,
      oidc_allowed_subjects: Array.isArray(authData.oidc_allowed_subjects) ? authData.oidc_allowed_subjects.slice() : [],
      oidc_allowed_emails: Array.isArray(authData.oidc_allowed_emails) ? authData.oidc_allowed_emails.slice() : [],
      oidc_allowed_groups: Array.isArray(authData.oidc_allowed_groups) ? authData.oidc_allowed_groups.slice() : [],
      oidc_group_claim: text(authData.oidc_group_claim || 'groups'),
      public_base_url: text(authData.public_base_url),
    });
    syncGlobalSettings(state.settings);
  }

  function fieldId(key) {
    return `dp-settings-field-${key.replaceAll('_', '-')}`;
  }

  function input(key, label, value, options = {}) {
    const id = fieldId(key);
    const type = options.type || 'text';
    const attrs = [
      options.min != null ? `min="${html(options.min)}"` : '',
      options.max != null ? `max="${html(options.max)}"` : '',
      options.step != null ? `step="${html(options.step)}"` : '',
      options.placeholder ? `placeholder="${html(options.placeholder)}"` : '',
      options.readonly ? 'readonly' : '',
      options.autocomplete ? `autocomplete="${html(options.autocomplete)}"` : '',
    ].filter(Boolean).join(' ');
    return `
      <div class="dp-settings-field">
        <label class="form-label" for="${id}">${html(label)}</label>
        <input class="input" id="${id}" data-setting="${html(key)}" type="${html(type)}" value="${html(value)}" ${attrs}>
        ${options.hint ? `<span class="form-hint">${options.hint}</span>` : ''}
      </div>`;
  }

  function textarea(key, label, value, options = {}) {
    const id = fieldId(key);
    return `
      <div class="dp-settings-field">
        <label class="form-label" for="${id}">${html(label)}</label>
        <textarea class="input" id="${id}" data-setting="${html(key)}" rows="${options.rows || 3}" ${options.placeholder ? `placeholder="${html(options.placeholder)}"` : ''}>${html(value)}</textarea>
        ${options.hint ? `<span class="form-hint">${options.hint}</span>` : ''}
      </div>`;
  }

  function toggle(key, label, detail, value) {
    const id = fieldId(key);
    return `
      <label class="toggle-row dp-settings-toggle" for="${id}">
        <span class="toggle-info">
          <span class="tl">${html(label)}</span>
          ${detail ? `<span class="td">${html(detail)}</span>` : ''}
        </span>
        <span class="toggle">
          <input id="${id}" data-setting="${html(key)}" type="checkbox" ${checked(value)}>
          <span class="ttrack"></span>
        </span>
      </label>`;
  }

  function selectField(key, label, value, choices, hint = '') {
    const id = fieldId(key);
    return `
      <div class="dp-settings-field">
        <label class="form-label" for="${id}">${html(label)}</label>
        <select class="input" id="${id}" data-setting="${html(key)}">
          ${choices.map(([v, labelText]) => `<option value="${html(v)}" ${selected(value, v)}>${html(labelText)}</option>`).join('')}
        </select>
        ${hint ? `<span class="form-hint">${hint}</span>` : ''}
      </div>`;
  }

  function card(title, body, options = {}) {
    const titleClass = options.titlePrefix ? 'card-title dp-settings-card-title--with-icon' : 'card-title';
    const titleMarkup = options.titlePrefix
      ? `${options.titlePrefix}<span class="dp-settings-card-title-text">${html(title)}</span>`
      : html(title);
    return `
      <section class="card dp-settings-card dp-large-panel-surface ${options.className || ''}">
        <div class="card-header">
          <span class="${titleClass}">${titleMarkup}</span>
          ${options.headerCenter ? `<div class="dp-settings-card-header-center ${options.headerCenterClass || ''}">${options.headerCenter}</div>` : ''}
          ${options.action || ''}
        </div>
        <div class="card-body">${body}</div>
      </section>`;
  }

  function groupCard(title, body, options = {}) {
    return `
      <section class="card dp-settings-group-card dp-large-panel-surface ${options.className || ''}">
        <div class="card-header">
          <span class="card-title">${html(title)}</span>
          ${options.action || ''}
        </div>
        <div class="card-body dp-settings-group-body">${body}</div>
      </section>`;
  }

  function secretField(key, label, configured, placeholder, hint) {
    return `
      ${input(key, label, '', {
        type: 'password',
        placeholder: configured ? `${placeholder || label} configured — blank keeps current value` : (placeholder || label),
        autocomplete: 'off',
        hint,
      })}
      ${configured ? `
        <label class="dp-settings-clear-secret">
          <span>
            <b>Clear stored ${html(label)}</b>
            <small>Erase the stored value when Settings are saved.</small>
          </span>
          <input type="checkbox" data-clear-secret="${html(key)}">
        </label>` : ''}`;
  }

  function allDebridApiKeyField(configured) {
    const key = 'alldebrid_api_key';
    const id = fieldId(key);
    const masked = '••••••••••••••••';
    const hint = configured
      ? 'Enter a new API key to replace the stored key when you click Apply Settings. Leave this field blank to keep the current key.'
      : 'Enter your AllDebrid API key. It will be saved only when you click Apply Settings.';
    return `
      <div class="dp-settings-alldebrid-key-row ${configured ? 'is-configured' : ''}">
        <div class="dp-settings-field dp-settings-alldebrid-key-field">
          <label class="form-label" for="${id}">API Key</label>
          <input class="input" id="${id}" data-setting="${key}" type="password" value=""
                 placeholder="${configured ? masked : 'Your AllDebrid API key'}" autocomplete="off">
          <div class="dp-settings-alldebrid-key-meta">
            <span class="form-hint">${hint}</span>
            ${configured ? '<span class="form-hint dp-settings-key-present">Key present</span>' : ''}
          </div>
        </div>
        ${configured ? `
          <label class="dp-settings-clear-secret dp-settings-clear-secret--alldebrid">
            <span>
              <b>Clear stored API Key</b>
              <small>Remove the saved API key when you click Apply Settings.</small>
            </span>
            <input type="checkbox" data-clear-secret="${key}">
          </label>` : ''}
      </div>`;
  }

  function aria2RpcSecretFields(configured) {
    const key = 'aria2_secret';
    const id = fieldId(key);
    const masked = '••••••••••••••••';
    const hint = configured
      ? 'Enter a new RPC secret to replace the stored secret when you click Apply Settings. Leave this field blank to keep the current secret.'
      : 'Enter the RPC secret used by your external aria2 server. It will be saved only when you click Apply Settings.';
    return `
      <div class="dp-settings-field dp-settings-aria2-secret-field">
        <label class="form-label" for="${id}">aria2 RPC Secret</label>
        <input class="input" id="${id}" data-setting="${key}" type="password" value=""
               placeholder="${configured ? masked : 'Optional RPC secret'}" autocomplete="off">
        <div class="dp-settings-aria2-secret-meta">
          <span class="form-hint">${hint}</span>
          ${configured ? '<span class="form-hint dp-settings-key-present">Secret Present</span>' : ''}
        </div>
      </div>
      ${configured ? `
        <label class="dp-settings-clear-secret dp-settings-clear-secret--aria2">
          <span class="form-label">Clear stored aria2 RPC Secret</span>
          <span class="dp-settings-clear-secret-control" aria-hidden="true">
            <input type="checkbox" data-clear-secret="${key}" aria-label="Clear stored aria2 RPC Secret">
          </span>
          <small>Remove the saved RPC secret when you click Apply Settings.</small>
        </label>` : ''}`;
  }

  function tuningToggle(key, label, detail, value) {
    const id = fieldId(key);
    return `
      <div class="dp-settings-field dp-settings-engine-tuning-toggle-field">
        <label class="form-label" for="${id}">${html(label)}</label>
        <div class="dp-settings-engine-tuning-toggle-control">
          <span class="toggle">
            <input id="${id}" data-setting="${html(key)}" type="checkbox" ${checked(value)}>
            <span class="ttrack"></span>
          </span>
        </div>
        <span class="form-hint">${html(detail)}</span>
      </div>`;
  }

  function integrationHeaderToggle(identity, value, displayName, extraClass = '') {
    const safeIdentity = String(identity || '').replace(/[^a-z0-9_-]/gi, '-');
    const id = `dp-settings-integration-${safeIdentity}-enabled`;
    return `
      <label class="toggle-row dp-settings-toggle dp-settings-auth-header-enable dp-settings-integration-header-enable ${html(extraClass)}" for="${id}">
        <span class="toggle-info"><span class="tl">Enable</span></span>
        <span class="toggle">
          <input id="${id}" data-integration-enabled="${html(identity)}" type="checkbox" ${checked(value)}
                 aria-label="Enable ${html(displayName)} provider route">
          <span class="ttrack"></span>
        </span>
      </label>`;
  }

  function sourcesPanel(s) {
    const integrations = s.integrations || {};
    const allDebrid = integrations.alldebrid || {};
    const generalHttp = integrations.general_http || {};
    const providerIdentity = `
      <span class="dp-settings-provider-chip dp-settings-provider-chip--alldebrid" aria-hidden="true">
        <img class="dp-settings-provider-logo dp-settings-provider-logo--alldebrid" src="/icons/providers/alldebrid.svg" alt="">
      </span>`;
    const provider = card('AllDebrid', `
      <p class="dp-settings-copy">Connect DebridPulse to AllDebrid for direct links, magnets, and torrent files.</p>
      ${allDebridApiKeyField(!!s.alldebrid_api_key_configured)}
      <details class="dp-settings-additional">
        <summary><span>Additional Settings</span></summary>
        <div class="dp-settings-additional-body">
          ${input('alldebrid_rate_limit_per_minute', 'API Calls per Minute', s.alldebrid_rate_limit_per_minute ?? 60, {
            type: 'number', min: 0, max: 300,
            hint: 'Limits how many requests DebridPulse sends to AllDebrid each minute. Set to 0 for no local limit.'
          })}
          ${input('poll_interval_seconds', 'Provider Poll Interval (seconds)', s.poll_interval_seconds ?? 30, {
            type: 'number', min: 10,
            hint: 'How often DebridPulse checks AllDebrid for updates to active transfers. Shorter intervals provide faster status updates but increase API traffic.'
          })}
          ${input('full_sync_interval_minutes', 'Full Sync Interval (minutes)', s.full_sync_interval_minutes ?? 5, {
            type: 'number', min: 0, max: 1440,
            hint: 'How often DebridPulse performs a complete reconciliation with AllDebrid. Set to 0 to disable scheduled full syncs.'
          })}
          ${input('upload_fail_retry_count', 'Upload Failure Retries', s.upload_fail_retry_count ?? 3, {
            type: 'number', min: 0, max: 20,
            hint: 'How many times DebridPulse retries a failed provider upload before giving up. Set to 0 to disable retries.'
          })}
          ${input('upload_fail_retry_delay_minutes', 'Retry Delay (minutes)', s.upload_fail_retry_delay_minutes ?? 5, {
            type: 'number', min: 0, max: 1440,
            hint: 'How long DebridPulse waits between failed upload attempts. Set to 0 to retry immediately.'
          })}
        </div>
      </details>
    `, {
      className: 'dp-settings-provider-card dp-settings-provider-card--alldebrid',
      titlePrefix: providerIdentity,
      action: integrationHeaderToggle('alldebrid', allDebrid.enabled !== false, 'AllDebrid', 'dp-settings-provider-header-enable'),
    });

    const generalHttpCard = card('HTTP & HTTPS', `
      <p class="dp-settings-copy dp-settings-provider-minimal-copy">Direct downloads from standard HTTP and HTTPS URLs.</p>
    `, {
      className: 'dp-settings-provider-card dp-settings-provider-card--general-http',
      action: integrationHeaderToggle('general_http', generalHttp.enabled !== false, 'HTTP & HTTPS', 'dp-settings-provider-header-enable'),
    });

    const debridServices = groupCard('Debrid Services', provider, {
      className: 'dp-settings-source-group dp-settings-debrid-services',
    });
    const generalSources = groupCard('General Sources', generalHttpCard, {
      className: 'dp-settings-source-group dp-settings-general-sources',
    });
    return debridServices + generalSources;
  }

  function downloadsPanel(s) {
    const builtIn = (s.aria2_mode || 'builtin') === 'builtin';
    const engineIdentity = `
      <span class="dp-settings-download-engine-icon" aria-hidden="true">
        <img src="/icons/dp/download-engine.svg?v=1" alt="">
      </span>`;
    const engineCopy = 'Choose where DebridPulse sends downloads. Built-in aria2 runs with DebridPulse; External aria2 uses your existing aria2 server.';
    const modeSelection = `
      <div class="dp-settings-download-engine-mode">
        ${selectField('aria2_mode', 'Mode Selection', s.aria2_mode || 'builtin', [
          ['builtin', 'Built-in aria2'],
          ['external', 'External aria2'],
        ])}
      </div>`;
    const delivery = card('Download Engine', `
      <div class="dp-settings-mode-external dp-settings-external-connection-row ${s.aria2_secret_configured ? 'is-secret-configured' : ''}" ${builtIn ? 'hidden' : ''}>
        ${input('aria2_url', 'External RPC URL', s.aria2_url || 'http://127.0.0.1:6800/jsonrpc', {
          placeholder: 'http://aria2:6800/jsonrpc',
          hint: 'JSON-RPC endpoint DebridPulse uses to connect to your external aria2 server.'
        })}
        ${aria2RpcSecretFields(!!s.aria2_secret_configured)}
      </div>
      <div class="dp-settings-download-engine-row">
        <div class="dp-settings-download-path-stack" data-download-path-mode="builtin" ${builtIn ? '' : 'hidden'}>
          ${input('download_folder', 'Built-in Download Folder', s.download_folder || '/download', {
            hint: 'Where DebridPulse saves downloads.'
          })}
        </div>
        <div class="dp-settings-download-path-stack" data-download-path-mode="external" ${builtIn ? 'hidden' : ''}>
          ${input('aria2_download_path', 'External aria2 Download Path', s.aria2_download_path || '', {
            hint: 'Path your external aria2 server uses for the shared download folder on that server.'
          })}
        </div>
        <div class="dp-settings-download-limit">
          ${input('aria2_max_active_downloads', 'Maximum Concurrent Downloads', s.max_concurrent_downloads ?? s.aria2_max_active_downloads ?? 3, {
            type: 'number', min: 1, max: 20,
            hint: 'Maximum number of downloads DebridPulse can run at the same time.'
          })}
        </div>
      </div>
      <details class="dp-settings-additional dp-settings-engine-tuning" ${builtIn ? '' : 'hidden'}>
        <summary><span>Additional Engine Tuning</span></summary>
        <div class="dp-settings-additional-body">
          <div class="dp-settings-engine-tuning-grid">
            ${input('aria2_lowest_speed_limit', 'Lowest Speed Limit', s.aria2_lowest_speed_limit || '0', {
              hint: 'Stops a slow HTTP/HTTPS/FTP connection when its speed falls at or below this value. Set to 0 to disable the limit.'
            })}
            ${tuningToggle(
              'aria2_continue_downloads',
              'Continue Partial Downloads',
              'Resume existing partial files when possible instead of restarting them from the beginning.',
              s.aria2_continue_downloads !== false
            )}
            ${input('aria2_split', 'Segments per File', s.aria2_split ?? 16, {
              type: 'number', min: 1, max: 64,
              hint: 'Controls how many parallel segments aria2 can use for a single file. Actual connections may be limited by the server and split-size settings.'
            })}
            ${input('aria2_max_connection_per_server', 'Connections per Server', s.aria2_max_connection_per_server ?? 16, {
              type: 'number', min: 1, max: 32,
              hint: 'Maximum number of connections a single download can open to the same server.'
            })}
            ${input('aria2_min_split_size', 'Minimum Split Size', s.aria2_min_split_size || '10M', {
              hint: 'Controls how small file sections can become when aria2 splits a download. Larger values create fewer parallel segments.'
            })}
            ${input('aria2_disk_cache', 'Disk Cache', s.aria2_disk_cache || '64M', {
              hint: 'Amount of memory aria2 can use as a shared download cache to reduce disk I/O. Set to 0 to disable the cache.'
            })}
          </div>
          <div class="dp-settings-engine-file-allocation">
            ${selectField('aria2_file_allocation', 'File Allocation', s.aria2_file_allocation || 'falloc', [
              ['trunc', 'Truncate'],
              ['falloc', 'Fallocate'],
              ['prealloc', 'Preallocate'],
              ['none', 'None'],
            ], 'Controls how aria2 prepares disk space for new files.')}
          </div>
        </div>
      </details>
    `, {
      className: 'dp-settings-download-engine-card',
      titlePrefix: engineIdentity,
      headerCenter: `<span class="dp-settings-download-engine-header-copy">${html(engineCopy)}</span>`,
      action: modeSelection,
    });

    const recovery = card('Download Safety & Recovery', `
      ${input('min_free_disk_gb', 'Minimum Free Disk Space (GB)', s.min_free_disk_gb ?? 0, {
        type: 'number', min: 0, step: 0.5, hint: '0 disables the disk-space dispatch guard.'
      })}
      ${input('disk_guard_resume_hysteresis_gb', 'Resume Hysteresis (GB)', s.disk_guard_resume_hysteresis_gb ?? 0.5, {
        type: 'number', min: 0, step: 0.1
      })}
      ${input('stuck_download_timeout_hours', 'Stalled Download Timeout (hours)', s.stuck_download_timeout_hours ?? 6, {
        type: 'number', min: 0, max: 168, hint: '0 disables automatic stalled-download recovery.'
      })}
      ${input('aria2_error_retry_count', 'aria2 Error Retries', s.aria2_error_retry_count ?? 3, {
        type: 'number', min: 0, max: 20
      })}
      ${input('aria2_error_retry_delay_seconds', 'aria2 Retry Delay (seconds)', s.aria2_error_retry_delay_seconds ?? 60, {
        type: 'number', min: 0, max: 3600
      })}
    `);

    return delivery + recovery;
  }

  function extractionPanel(s) {
    return card('Automatic Extraction', `
      <p class="dp-settings-copy">Extract supported archives after the physical download completes.</p>
      ${toggle('extract_enabled', 'Enable Automatic Extraction', 'Run extraction for supported completed archives.', s.extract_enabled)}
      ${toggle('extract_delete_archive', 'Delete Archive After Extraction', 'Remove archive files only after successful extraction.', s.extract_delete_archive !== false)}
      ${input('extract_max_concurrent', 'Concurrent Extractions', s.extract_max_concurrent ?? 1, {
        type: 'number', min: 1, max: 8
      })}
      ${textarea('extraction_password', 'Archive Passwords (one per line)', '', {
        rows: 4,
        placeholder: s.extraction_password_configured ? 'Stored password list configured — blank keeps it' : 'Optional archive passwords',
        hint: 'Entering values replaces the stored password list. Blank preserves the stored value.'
      })}
      ${s.extraction_password_configured ? `
        <label class="dp-settings-clear-secret">
          <span><b>Clear stored archive passwords</b><small>Erase the stored extraction password list on Save.</small></span>
          <input type="checkbox" data-clear-secret="extraction_password">
        </label>` : ''}
    `);
  }

  function notificationsPanel(s) {
    const discord = card('Discord Notifications', `
      ${input('discord_username', 'Display Name', s.discord_username || 'DebridPulse')}
      ${input('discord_avatar_url', 'Avatar URL', s.discord_avatar_url || '', {
        placeholder: 'https://example.com/avatar.png'
      })}
      <div class="dp-settings-actions">
        <label class="btn btn-ghost btn-sm dp-settings-file-button">
          Upload Avatar
          <input id="dp-settings-avatar-file" type="file" accept="image/png,image/jpeg,image/gif,image/webp" hidden>
        </label>
        <button class="btn btn-ghost btn-sm" type="button" data-action="clear-avatar">Clear Avatar</button>
      </div>
      <div id="dp-settings-avatar-preview" class="dp-settings-avatar-preview" ${s.discord_avatar_url ? '' : 'hidden'}>
        ${s.discord_avatar_url ? `<img src="${html(s.discord_avatar_url)}" alt="Discord avatar preview">` : ''}
        <span>${s.discord_avatar_url ? html(s.discord_avatar_url) : ''}</span>
      </div>
      ${secretField('discord_webhook_url', 'Discord Webhook', !!s.discord_webhook_url_configured, 'Primary Discord webhook', 'Used for normal notification delivery.')}
      ${secretField('discord_webhook_added', 'Added-event Webhook', !!s.discord_webhook_added_configured, 'Optional added-event webhook', 'Blank falls back according to server notification policy.')}
      ${toggle('discord_notify_added', 'Download Added', 'Notify when work is accepted.', s.discord_notify_added)}
      ${toggle('discord_notify_finished', 'Download Completed', 'Notify when a download finishes.', s.discord_notify_finished)}
      ${toggle('discord_notify_error', 'Download Error', 'Notify on transfer failures.', s.discord_notify_error)}
      ${toggle('discord_notify_extract', 'Extraction Result', 'Notify when extraction completes or fails.', s.discord_notify_extract)}
      ${toggle('discord_notify_update', 'Update Available', 'Notify when a DebridPulse update is detected.', s.discord_notify_update)}
      ${input('update_check_interval_hours', 'Update Check Interval (hours)', s.update_check_interval_hours ?? 12, {
        type: 'number', min: 1, max: 168
      })}
    `);

    const reports = card('Statistics Reports', `
      ${secretField('stats_report_webhook_url', 'Reporting Webhook', !!s.stats_report_webhook_url_configured, 'Optional reporting webhook', 'Blank uses the configured fallback when supported.')}
      ${input('stats_report_interval_hours', 'Automatic Report Interval (hours)', s.stats_report_interval_hours ?? 0, {
        type: 'number', min: 0, max: 8760, hint: '0 disables automatic reports.'
      })}
      ${selectField('stats_report_window_hours', 'Report Window', s.stats_report_window_hours ?? 24, [
        [24, '24 hours'],
        [168, '7 days'],
        [720, '30 days'],
        [8760, '1 year'],
      ])}
      <div class="dp-settings-actions">
        <button class="btn btn-ghost btn-sm" type="button" data-action="send-report">Send Test Report</button>
      </div>
    `);

    return discord + reports;
  }

  function fieldClass(markup, ...classes) {
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
  }

  function maintenancePanel(s) {
    return card('Backups & Retention', `
      ${toggle('backup_enabled', 'Enable Backups', 'Automatically back up configuration and database state.', s.backup_enabled !== false)}
      ${input('backup_folder', 'Backup Folder', s.backup_folder || '/app/data/backups')}
      ${input('backup_interval_hours', 'Backup Interval (hours)', s.backup_interval_hours ?? 24, {type: 'number', min: 1, max: 168})}
      ${input('backup_keep_days', 'Keep Backups (days)', s.backup_keep_days ?? 7, {type: 'number', min: 1, max: 90})}
      ${input('stats_snapshot_interval_minutes', 'Statistics Snapshot Interval (minutes)', s.stats_snapshot_interval_minutes ?? 60, {
        type: 'number', min: 0, max: 1440, hint: '0 disables automatic statistics snapshots.'
      })}
      ${input('stats_snapshot_keep_days', 'Keep Statistics Snapshots (days)', s.stats_snapshot_keep_days ?? 30, {type: 'number', min: 1, max: 365})}
      ${input('events_keep_days', 'Keep Event Log (days)', s.events_keep_days ?? 30, {type: 'number', min: 1})}
      <div class="dp-settings-actions">
        <button class="btn btn-ghost btn-sm" type="button" data-action="run-backup">Run Backup Now</button>
        <button class="btn btn-ghost btn-sm" type="button" data-action="list-backups">List Backups</button>
      </div>
      <div id="dp-settings-backup-list" class="dp-settings-result-list"></div>
    `) + card('Database Destructive Actions', `
      <div class="dp-settings-caution">
        <b>Database wipe is destructive</b>
        <span>Processing must be paused. DebridPulse can require a database backup before rows are removed.</span>
      </div>
      ${toggle('db_wipe_enabled', 'Allow Database Wipe', 'Required before the wipe endpoint will run.', s.db_wipe_enabled)}
      ${toggle('db_backup_before_wipe', 'Backup Before Wipe', 'Abort the wipe if the required pre-wipe backup fails.', s.db_backup_before_wipe !== false)}
      <div class="dp-settings-actions">
        <button class="btn btn-danger btn-sm" type="button" data-action="wipe-database">Wipe Database</button>
      </div>
    `);
  }

  function panel(name, body) {
    return `<section class="dp-settings-panel" data-panel="${html(name)}" role="tabpanel" ${state.activeTab === name ? '' : 'hidden'}>${body}</section>`;
  }

  function render() {
    const view = root();
    if (!view || !state.settings || !state.auth) return;

    // The inherited navigation still adds a Settings-only shell state before
    // invoking loadSettings(). The clean page explicitly rejects that legacy
    // state before its DOM is painted.
    document.getElementById('content')?.classList.remove('settings-active');

    view.classList.add('dp-settings-clean-view');

    const tabs = TABS.map(([id, label, icon]) => `
      <button class="stab ${state.activeTab === id ? 'active' : ''}" type="button"
              data-tab="${html(id)}" role="tab"
              aria-selected="${state.activeTab === id ? 'true' : 'false'}"
              tabindex="${state.activeTab === id ? '0' : '-1'}">
        <span class="dp-settings-tab-chip" aria-hidden="true">
          <img class="dp-settings-tab-glyph" src="/icons/lucide/${html(icon)}.svg" alt="">
        </span>
        <span class="dp-settings-tab-label">${html(label)}</span>
      </button>
    `).join('');

    view.innerHTML = `
      <section class="card dp-settings-master-card" aria-label="Settings">
        <div class="card-header dp-settings-master-header">
          <div class="dp-settings-header-copy">
            <div class="dp-settings-header-icon" aria-hidden="true"></div>
            <div>
              <div class="dp-settings-header-title">Tuning Deck</div>
              <div class="dp-settings-header-subtitle">Your rules, your defaults.</div>
            </div>
          </div>
          <div class="stabs dp-settings-tabs" role="tablist" aria-label="Settings sections">${tabs}</div>
        </div>

        <div class="dp-settings-master-body">
          <div class="dp-settings-scroll">
            <div class="dp-settings-panels">
              ${panel('sources', sourcesPanel(state.settings))}
              ${panel('downloads', downloadsPanel(state.settings))}
              ${panel('extraction', extractionPanel(state.settings))}
              ${panel('notifications', notificationsPanel(state.settings))}
              ${panel('authentication', authenticationPanel(state.auth))}
              ${panel('maintenance', maintenancePanel(state.settings))}
            </div>
          </div>
        </div>

        <div class="dp-settings-master-footer" aria-label="Settings actions">
          <span class="dp-settings-save-hint">Changes are applied after saving.</span>
          <div class="dp-settings-context-actions">
            <button class="btn btn-ghost" type="button" data-context-action="sources" data-action="test-alldebrid">
              <span class="dp-settings-action-chip" aria-hidden="true">
                <img class="dp-settings-action-glyph" src="/icons/lucide/flask-conical.svg" alt="">
              </span>
              <span>Test AllDebrid</span>
            </button>
            <button class="btn btn-ghost" type="button" data-context-action="downloads" data-action="test-aria2">Test aria2</button>
            <button class="btn btn-ghost" type="button" data-context-action="notifications" data-action="test-discord">Test Discord</button>
            <button class="btn btn-ghost" type="button" data-context-action="authentication" data-action="verify-oidc">Test OIDC Sign-In</button>
          </div>
          <button class="btn btn-primary" type="button" data-action="save">Apply Settings</button>
        </div>
      </section>`;

    activateTab(state.activeTab);
    bindEvents(view);
    updateModeState();
    updateOidcCallbackPreview();
    document.dispatchEvent(new CustomEvent('debridpulse:settings-rendered', {detail:{tab: state.activeTab}}));
  }

  function activateTab(name) {
    if (!TABS.some(([id]) => id === name)) name = 'sources';
    state.activeTab = name;

    root()?.querySelectorAll('[data-tab]').forEach(tab => {
      const active = tab.dataset.tab === name;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
      tab.tabIndex = active ? '0' : '-1';
    });

    root()?.querySelectorAll('[data-panel]').forEach(section => {
      section.hidden = section.dataset.panel !== name;
    });

    root()?.querySelectorAll('[data-context-action]').forEach(button => {
      button.hidden = button.dataset.contextAction !== name;
    });
  }

  function bindEvents(view) {
    if (view.dataset.dpSettingsEventsBound === '1') return;
    view.dataset.dpSettingsEventsBound = '1';

    view.addEventListener('keydown', event => {
      const current = event.target.closest('.dp-settings-tabs [data-tab]');
      if (!current || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const buttons = Array.from(view.querySelectorAll('.dp-settings-tabs [data-tab]'));
      let index = buttons.indexOf(current);
      if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = buttons.length - 1;
      else index = (index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
      const next = buttons[index];
      activateTab(next.dataset.tab);
      next.focus();
    });

    view.addEventListener('input', event => {
      if (event.target.id === 'dp-auth-public-base-url') updateOidcCallbackPreview();
    });

    view.addEventListener('change', event => {
      if (event.target.matches(`[data-setting="aria2_mode"]`)) updateModeState();
      if (event.target.id === 'dp-auth-public-base-url') updateOidcCallbackPreview();
      if (event.target.id === 'dp-settings-avatar-file') uploadAvatar(event.target);
      if (event.target.matches(`[data-setting="api_token_enabled"]`)) setApiTokenEnabled(event.target);
    });

    view.addEventListener('click', event => {
      const tab = event.target.closest('.dp-settings-tabs [data-tab]');
      if (tab) {
        activateTab(tab.dataset.tab);
        return;
      }

      const button = event.target.closest('button[data-action]');
      if (!button) return;
      const action = button.dataset.action;
      if (action === 'save') saveCurrent(button);
      else if (action === 'test-alldebrid') testConnection('alldebrid', button);
      else if (action === 'test-aria2') testConnection('aria2', button);
      else if (action === 'test-discord') testConnection('discord', button);
      else if (action === 'clear-avatar') clearAvatar();
      else if (action === 'send-report') sendReport(button);
      else if (action === 'run-backup') runBackup(button);
      else if (action === 'list-backups') listBackups(button);
      else if (action === 'wipe-database') wipeDatabaseClean(button);
      else if (action === 'clear-password') clearPassword(button);
      else if (action === 'verify-oidc') verifyOidc(button);
      else if (action === 'generate-token') generateToken(button);
      else if (action === 'clear-token') clearToken(button);
      else if (action === 'copy-token') copyToken();
      else if (action === 'copy-oidc-callback') copyOidcCallback();
      else if (action === 'logout-session') logoutSession(button);
    });
  }

  function updateModeState() {
    const mode = valueOf('aria2_mode') || 'builtin';
    root()?.querySelectorAll('.dp-settings-mode-external').forEach(el => {
      el.hidden = mode !== 'external';
    });
    root()?.querySelectorAll('[data-download-path-mode]').forEach(el => {
      el.hidden = el.dataset.downloadPathMode !== mode;
    });
    root()?.querySelectorAll('.dp-settings-engine-tuning').forEach(el => {
      el.hidden = mode !== 'builtin';
    });
  }

  function fieldFor(key) {
    return byId(fieldId(key));
  }

  function valueOf(key, fallback = '') {
    const field = fieldFor(key);
    return field ? String(field.value ?? '').trim() : fallback;
  }

  function intOf(key, fallback = 0) {
    const raw = valueOf(key, '');
    if (raw === '') return fallback;
    const value = parseInt(raw, 10);
    return Number.isNaN(value) ? fallback : value;
  }

  function floatOf(key, fallback = 0) {
    const raw = valueOf(key, '');
    if (raw === '') return fallback;
    const value = parseFloat(raw);
    return Number.isNaN(value) ? fallback : value;
  }

  function boolOf(key) {
    return !!fieldFor(key)?.checked;
  }

  function clearSecrets() {
    return Array.from(root()?.querySelectorAll('[data-clear-secret]:checked') || [])
      .map(input => input.dataset.clearSecret)
      .filter(Boolean);
  }

  function integrationPayload(current) {
    const result = {};
    const currentIntegrations = current?.integrations || {};
    for (const [identity, entry] of Object.entries(currentIntegrations)) {
      const options = Object.fromEntries(
        Object.entries(entry?.options || {}).filter(([key]) => !key.endsWith('_configured'))
      );
      result[identity] = {
        enabled: entry?.enabled !== false,
        priority: Number(entry?.priority || 0),
        options,
      };
    }
    for (const identity of ['alldebrid', 'general_http']) {
      const input = root()?.querySelector(`[data-integration-enabled="${identity}"]`);
      const previous = result[identity] || {enabled: true, priority: 0, options: {}};
      result[identity] = {
        ...previous,
        enabled: input ? !!input.checked : previous.enabled !== false,
      };
    }
    return result;
  }

  function nonAuthPayload() {
    const current = state.settings || {};
    const maxDownloads = intOf('aria2_max_active_downloads', Number(current.max_concurrent_downloads ?? 3));
    return {
      ...current,
      integrations: integrationPayload(current),
      clear_secrets: clearSecrets(),
      alldebrid_api_key: valueOf('alldebrid_api_key'),
      alldebrid_rate_limit_per_minute: intOf('alldebrid_rate_limit_per_minute', 60),
      poll_interval_seconds: intOf('poll_interval_seconds', 30),
      full_sync_interval_minutes: intOf('full_sync_interval_minutes', 5),
      upload_fail_retry_count: intOf('upload_fail_retry_count', 3),
      upload_fail_retry_delay_minutes: intOf('upload_fail_retry_delay_minutes', 5),

      aria2_mode: valueOf('aria2_mode', current.aria2_mode || 'builtin'),
      aria2_url: valueOf('aria2_url', current.aria2_url || 'http://127.0.0.1:6800/jsonrpc'),
      aria2_secret: valueOf('aria2_secret'),
      download_folder: valueOf('download_folder', current.download_folder || '/download'),
      aria2_download_path: valueOf('aria2_download_path'),
      max_concurrent_downloads: maxDownloads,
      aria2_max_active_downloads: maxDownloads,
      min_free_disk_gb: floatOf('min_free_disk_gb', 0),
      disk_guard_resume_hysteresis_gb: floatOf('disk_guard_resume_hysteresis_gb', 0.5),
      stuck_download_timeout_hours: intOf('stuck_download_timeout_hours', 6),
      aria2_error_retry_count: intOf('aria2_error_retry_count', 3),
      aria2_error_retry_delay_seconds: intOf('aria2_error_retry_delay_seconds', 60),
      extract_enabled: boolOf('extract_enabled'),
      extract_delete_archive: boolOf('extract_delete_archive'),
      extract_max_concurrent: intOf('extract_max_concurrent', 1),
      extraction_password: valueOf('extraction_password'),

      discord_username: valueOf('discord_username', 'DebridPulse'),
      discord_avatar_url: valueOf('discord_avatar_url'),
      discord_webhook_url: valueOf('discord_webhook_url'),
      discord_webhook_added: valueOf('discord_webhook_added'),
      discord_notify_added: boolOf('discord_notify_added'),
      discord_notify_finished: boolOf('discord_notify_finished'),
      discord_notify_error: boolOf('discord_notify_error'),
      discord_notify_extract: boolOf('discord_notify_extract'),
      discord_notify_update: boolOf('discord_notify_update'),
      update_check_interval_hours: intOf('update_check_interval_hours', 12),
      stats_report_webhook_url: valueOf('stats_report_webhook_url'),
      stats_report_interval_hours: intOf('stats_report_interval_hours', 0),
      stats_report_window_hours: intOf('stats_report_window_hours', Number(current.stats_report_window_hours ?? 24)),

      backup_enabled: boolOf('backup_enabled'),
      backup_folder: valueOf('backup_folder', current.backup_folder || '/app/data/backups'),
      backup_interval_hours: intOf('backup_interval_hours', 24),
      backup_keep_days: intOf('backup_keep_days', 7),
      stats_snapshot_interval_minutes: intOf('stats_snapshot_interval_minutes', 60),
      stats_snapshot_keep_days: intOf('stats_snapshot_keep_days', 30),
      events_keep_days: intOf('events_keep_days', 30),
      db_wipe_enabled: boolOf('db_wipe_enabled'),
      db_backup_before_wipe: boolOf('db_backup_before_wipe'),

      aria2_split: intOf('aria2_split', 16),
      aria2_min_split_size: valueOf('aria2_min_split_size', '10M'),
      aria2_max_connection_per_server: intOf('aria2_max_connection_per_server', 16),
      aria2_disk_cache: valueOf('aria2_disk_cache', '64M'),
      aria2_file_allocation: valueOf('aria2_file_allocation', 'falloc'),
      aria2_lowest_speed_limit: valueOf('aria2_lowest_speed_limit', '0'),
      aria2_continue_downloads: boolOf('aria2_continue_downloads'),
    };
  }

  async function persistNonAuth({renderAfter = true, quiet = false} = {}) {
    const active = state.activeTab;
    const result = await request('PUT', '/settings', nonAuthPayload(), 15000);
    syncGlobalSettings(result);
    if (renderAfter) {
      state.activeTab = active;
      renderPreservingViewport();
    }
    if (!quiet) notify('Settings saved', 'success');
    try { if (typeof updateAria2ngLink === 'function') updateAria2ngLink(); } catch (_) {}
    try { if (typeof checkConnections === 'function') checkConnections(); } catch (_) {}
    try { if (typeof loadAria2SpeedLimit === 'function') loadAria2SpeedLimit(); } catch (_) {}
    return result;
  }

  function authValue(key, fallback = '') {
    const el = fieldFor(key);
    return el ? String(el.value ?? '').trim() : fallback;
  }

  function authLines(key) {
    return authValue(key).split('\n').map(item => item.trim()).filter(Boolean);
  }

  function authPayload() {
    const scopeValues = authValue('oidc_scopes').split(/[\s,]+/).map(item => item.trim()).filter(Boolean);
    return {
      auth_password_enabled: boolOf('auth_password_enabled'),
      auth_username: authValue('auth_username'),
      auth_password: text(byId('dp-auth-new-password')?.value),
      auth_session_lifetime_hours: Math.max(1, Math.min(168, intOf('auth_session_lifetime_hours', 12))),
      auth_oidc_enabled: boolOf('auth_oidc_enabled'),
      oidc_provider_name: authValue('oidc_provider_name', 'OpenID Connect'),
      oidc_issuer_url: authValue('oidc_issuer_url'),
      oidc_client_id: authValue('oidc_client_id'),
      oidc_client_secret: text(byId('dp-auth-oidc-secret')?.value),
      clear_oidc_client_secret: !!byId('dp-auth-clear-oidc-secret')?.checked,
      oidc_scopes: scopeValues,
      oidc_allow_all: boolOf('oidc_allow_all'),
      oidc_allowed_subjects: authLines('oidc_allowed_subjects'),
      oidc_allowed_emails: authLines('oidc_allowed_emails'),
      oidc_allowed_groups: authLines('oidc_allowed_groups'),
      oidc_group_claim: authValue('oidc_group_claim', 'groups'),
      public_base_url: state.auth?.public_base_url_env_override ? undefined : text(byId('dp-auth-public-base-url')?.value).trim(),
    };
  }

  async function persistAuth(button, payload = authPayload(), successMessage = 'Authentication settings saved') {
    if (!payload.auth_password_enabled && !payload.auth_oidc_enabled && state.auth?.authentication_required && !payload.confirm_open_mode) {
      const confirmed = await confirmAction({
        title: 'Disable interactive authentication?',
        message: 'Username & Password and OpenID Connect will both be disabled. DebridPulse and its API will be intentionally open.',
        confirmLabel: 'Continue to Open Mode',
        tone: 'warning',
      });
      if (!confirmed) return false;
      payload.confirm_open_mode = true;
    }

    setBusy(button, true, 'Saving…');
    try {
      const auth = await request('PUT', '/auth/config', payload, 15000);
      const generation = acceptAuth(auth, {probe: false});
      state.activeTab = 'authentication';
      renderPreservingViewport();
      void probeOidcRuntime(auth, generation);
      notify(successMessage, 'success');
      return true;
    } catch (error) {
      notify(error.message, 'error');
      return false;
    } finally {
      setBusy(button, false);
    }
  }

  async function saveCurrent(button) {
    if (state.activeTab === 'authentication') {
      await persistAuth(button);
      return;
    }

    setBusy(button, true, 'Saving…');
    try {
      await persistNonAuth();
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(button, false);
    }
  }

  function connectionTestPayload(kind) {
    const clears = new Set(clearSecrets());
    if (kind === 'alldebrid') {
      return {
        api_key: valueOf('alldebrid_api_key'),
        clear_api_key: clears.has('alldebrid_api_key'),
      };
    }
    if (kind === 'aria2') {
      return {
        mode: valueOf('aria2_mode', state.settings?.aria2_mode || 'builtin'),
        url: valueOf('aria2_url'),
        secret: valueOf('aria2_secret'),
        clear_secret: clears.has('aria2_secret'),
      };
    }
    if (kind === 'discord') {
      return {
        webhook_url: valueOf('discord_webhook_url'),
        clear_webhook: clears.has('discord_webhook_url'),
      };
    }
    throw new Error(`Unsupported connection test: ${kind}`);
  }

  async function testConnection(kind, button) {
    const endpoints = {
      alldebrid: '/settings/validate-alldebrid',
      aria2: '/settings/validate-aria2',
      discord: '/settings/validate-discord',
    };
    const labels = {alldebrid: 'AllDebrid', aria2: 'aria2', discord: 'Discord'};
    setBusy(button, true, 'Testing…');
    try {
      const result = await request('POST', endpoints[kind], connectionTestPayload(kind), 20000);
      if (kind === 'alldebrid') {
        notify(`AllDebrid connected${result.username ? ` as ${result.username}` : ''}`, 'success');
      } else if (kind === 'aria2') {
        notify(`aria2 ${result.version ? `v${result.version}` : 'online'}`, 'success');
      } else {
        notify('Discord notification sent', 'success');
      }
    } catch (error) {
      notify(`${labels[kind]}: ${error.message}`, 'error');
    } finally {
      setBusy(button, false);
    }
  }

  async function uploadAvatar(inputEl) {
    const file = inputEl.files?.[0];
    if (!file) return;
    const body = new FormData();
    body.append('file', file);
    try {
      const result = await request('POST', '/settings/upload-avatar', body, 20000);
      const avatar = fieldFor('discord_avatar_url');
      if (avatar) avatar.value = result.url || '';
      const preview = byId('dp-settings-avatar-preview');
      if (preview) {
        preview.hidden = false;
        preview.innerHTML = `<img src="${html(result.url || '')}" alt="Discord avatar preview"><span>${html(file.name)}</span>`;
      }
      notify('Avatar uploaded', 'success');
      if (result.warning) notify(result.warning, 'warn');
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      inputEl.value = '';
    }
  }

  function clearAvatar() {
    const avatar = fieldFor('discord_avatar_url');
    if (avatar) avatar.value = '';
    const preview = byId('dp-settings-avatar-preview');
    if (preview) {
      preview.hidden = true;
      preview.textContent = '';
    }
  }

  async function sendReport(button) {
    setBusy(button, true, 'Sending…');
    try {
      const hours = intOf('stats_report_window_hours', 24);
      const result = await request('POST', `/stats/report/send?hours=${hours}`, undefined, 20000);
      notify(`Report sent (${result.hours || hours}h)`, 'success');
      renderPreservingViewport();
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(button, false);
    }
  }

  async function runBackup(button) {
    setBusy(button, true, 'Running…');
    try {
      const result = await request('POST', '/admin/backup', undefined, 30000);
      if (result.skipped) notify('Backup is disabled in Settings', 'warn');
      else notify('Backup completed', 'success');
      await listBackups(null);
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(button, false);
    }
  }

  async function listBackups(button) {
    setBusy(button, true, 'Loading…');
    try {
      const result = await request('GET', '/admin/backups', undefined, 15000);
      const target = byId('dp-settings-backup-list');
      if (!target) return;
      const backups = Array.isArray(result.backups) ? result.backups : [];
      target.innerHTML = backups.length
        ? backups.map(item => `<div class="dp-settings-result-row"><span>${html(item.name || 'backup')}</span><span>${html((item.files || []).join(', '))}</span></div>`).join('')
        : '<div class="form-hint">No backups found.</div>';
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(button, false);
    }
  }

  async function wipeDatabaseClean(button) {
    if (!state.settings?.db_wipe_enabled) {
      notify("Apply 'Allow Database Wipe' before running a wipe", 'warn');
      return;
    }
    if (!boolOf('db_wipe_enabled')) {
      notify('Database wipe is disabled in the current draft', 'warn');
      return;
    }
    const confirmed = await confirmAction({
      title: 'Wipe database?',
      message: 'Processing must be paused. This permanently removes all database rows. If Backup Before Wipe is enabled, DebridPulse will create the required backup first.',
      confirmLabel: 'Wipe Database',
      tone: 'danger',
      typedPhrase: 'WIPE',
    });
    if (!confirmed) return;

    setBusy(button, true, 'Wiping…');
    try {
      const result = await request('POST', '/admin/database/wipe', {confirm: true}, 60000);
      notify(result.backup && !result.backup.skipped ? 'Database wiped. Pre-wipe backup created.' : 'Database wiped.', 'success');
      try { if (typeof loadStats === 'function') loadStats(); } catch (_) {}
      try { if (typeof loadRecent === 'function') loadRecent(); } catch (_) {}
      try {
        if (document.getElementById('view-torrents')?.classList.contains('active') && typeof loadTorrents === 'function') loadTorrents();
      } catch (_) {}
      renderPreservingViewport();
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(button, false);
    }
  }

  async function clearPassword(button) {
    const payload = authPayload();
    const entersOpenMode = !payload.auth_oidc_enabled && state.auth?.authentication_required;
    const confirmed = await confirmAction({
      title: 'Clear stored password?',
      message: entersOpenMode
        ? 'The stored local password will be removed and Username & Password authentication will be disabled. Because OpenID Connect is also disabled, DebridPulse will enter open mode.'
        : 'The stored local password will be removed and Username & Password authentication will be disabled.',
      confirmLabel: 'Clear Password',
      tone: 'danger',
    });
    if (!confirmed) return;

    payload.auth_password_enabled = false;
    payload.auth_password = '';
    payload.clear_password = true;
    if (entersOpenMode) payload.confirm_open_mode = true;
    await persistAuth(button, payload, 'Stored password cleared');
  }

  async function setApiTokenEnabled(inputEl) {
    const desired = !!inputEl.checked;
    inputEl.disabled = true;
    try {
      const result = await request('PUT', '/auth/api-token', {enabled: desired}, 10000);
      state.auth.api_token_enabled = !!result.enabled;
      state.auth.api_token_configured = !!result.configured;
      renderPreservingViewport();
      notify(`API token ${result.enabled ? 'enabled' : 'disabled'}`, 'success');
    } catch (error) {
      inputEl.checked = !desired;
      notify(error.message, 'error');
    } finally {
      inputEl.disabled = false;
    }
  }

  async function generateToken(button) {
    setBusy(button, true, state.auth?.api_token_configured ? 'Rotating…' : 'Generating…');
    try {
      const result = await request('POST', '/auth/api-token', undefined, 10000);
      state.auth.api_token_enabled = true;
      state.auth.api_token_configured = true;
      state.oneTimeToken = text(result.token);
      renderPreservingViewport();
      notify(result.rotated ? 'API token rotated' : 'API token generated', 'success');
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(button, false);
    }
  }

  async function clearToken(button) {
    const confirmed = await confirmAction({
      title: 'Revoke API token?',
      message: 'Existing automation using this token will immediately lose access.',
      confirmLabel: 'Revoke Token',
      tone: 'danger',
    });
    if (!confirmed) return;
    setBusy(button, true, 'Clearing…');
    try {
      await request('DELETE', '/auth/api-token', undefined, 10000);
      state.auth.api_token_enabled = false;
      state.auth.api_token_configured = false;
      state.oneTimeToken = '';
      renderPreservingViewport();
      notify('API token revoked', 'success');
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(button, false);
    }
  }

  async function copyToken() {
    if (!state.oneTimeToken) return;
    try {
      await navigator.clipboard.writeText(state.oneTimeToken);
      notify('API token copied', 'success');
    } catch (_) {
      const inputEl = byId('dp-settings-api-token-once');
      inputEl?.select();
      notify('Select and copy the token manually', 'info');
    }
  }

  function clearOidcResources({closePopup = true} = {}) {
    const oidc = state.oidc;
    if (oidc.poll) {
      window.clearInterval(oidc.poll);
      oidc.poll = null;
    }
    if (oidc.channel) {
      try { oidc.channel.close(); } catch (_) {}
      oidc.channel = null;
    }
    if (oidc.messageHandler) {
      window.removeEventListener('message', oidc.messageHandler);
      oidc.messageHandler = null;
    }
    if (closePopup && oidc.popup && !oidc.popup.closed) {
      try { oidc.popup.close(); } catch (_) {}
    }
    oidc.popup = null;
    setBusy(oidc.button, false);
    oidc.button = null;
  }

  async function finishOidc(result) {
    const oidc = state.oidc;
    if (oidc.completed) return;
    oidc.completed = true;
    clearOidcResources();

    try {
      if (window.debridPulseAuth) await window.debridPulseAuth.refreshSession({force: true});
      const auth = await request('GET', '/auth/config', undefined, 7000);
      const generation = acceptAuth(auth, {probe: false});
      state.activeTab = 'authentication';
      renderPreservingViewport();
      void probeOidcRuntime(auth, generation);
    } catch (_) {}

    const ok = !!result?.ok;
    notify(
      result?.message || (ok
        ? 'OIDC verification successful — provider sign-in and authorization completed.'
        : 'OIDC verification failed — provider sign-in or authorization did not complete successfully.'),
      ok ? 'success' : 'error'
    );
  }

  function armOidc(popup, button) {
    const oidc = state.oidc;
    oidc.completed = false;
    oidc.popup = popup;
    oidc.button = button;

    if ('BroadcastChannel' in window) {
      try {
        oidc.channel = new BroadcastChannel('debridpulse-oidc-verification');
        oidc.channel.onmessage = event => {
          if (event?.data?.type === 'debridpulse-oidc-verification') finishOidc(event.data);
        };
      } catch (_) {
        oidc.channel = null;
      }
    }

    oidc.messageHandler = event => {
      if (event.origin !== window.location.origin) return;
      if (event?.data?.type === 'debridpulse-oidc-verification') finishOidc(event.data);
    };
    window.addEventListener('message', oidc.messageHandler);

    oidc.poll = window.setInterval(() => {
      if (!oidc.popup || !oidc.popup.closed || oidc.completed) return;
      window.clearInterval(oidc.poll);
      oidc.poll = null;
      window.setTimeout(() => {
        if (!oidc.completed) finishOidc({
          ok: false,
          message: 'OIDC verification did not complete — the verification window was closed.',
        });
      }, 250);
    }, 300);
  }

  function renderOidcWaiting(popup) {
    try {
      popup.document.open();
      popup.document.write('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Testing OIDC · DebridPulse</title></head><body style="font-family:system-ui;background:#090812;color:#f4f1ff;display:grid;place-items:center;min-height:100vh;margin:0"><main style="text-align:center"><h1 style="font-size:20px">Testing OpenID Connect…</h1><p>Waiting for the provider sign-in flow.</p></main></body></html>');
      popup.document.close();
    } catch (_) {}
  }

  async function verifyOidc(button) {
    const oidc = state.oidc;
    if (oidc.popup && !oidc.popup.closed) {
      try { oidc.popup.focus(); } catch (_) {}
      return;
    }

    const width = 520;
    const height = 680;
    const left = Math.max(0, Math.round((window.screenX || 0) + ((window.outerWidth || screen.width) - width) / 2));
    const top = Math.max(0, Math.round((window.screenY || 0) + ((window.outerHeight || screen.height) - height) / 2));
    const popup = window.open('', 'debridpulse-oidc-verification', `popup=yes,width=${width},height=${height},left=${left},top=${top},resizable=yes,scrollbars=yes`);
    if (!popup) {
      notify('OIDC verification could not start because the verification window was blocked by the browser.', 'error');
      return;
    }

    renderOidcWaiting(popup);
    armOidc(popup, button);
    setBusy(button, true, 'Testing…');

    const payload = authPayload();
    const verification = {
      oidc_provider_name: payload.oidc_provider_name,
      oidc_issuer_url: payload.oidc_issuer_url,
      oidc_client_id: payload.oidc_client_id,
      oidc_client_secret: payload.oidc_client_secret,
      clear_oidc_client_secret: payload.clear_oidc_client_secret,
      oidc_scopes: payload.oidc_scopes,
      oidc_allow_all: payload.oidc_allow_all,
      oidc_allowed_subjects: payload.oidc_allowed_subjects,
      oidc_allowed_emails: payload.oidc_allowed_emails,
      oidc_allowed_groups: payload.oidc_allowed_groups,
      oidc_group_claim: payload.oidc_group_claim,
      public_base_url: payload.public_base_url,
      return_to: '/oidc-verify-complete.html',
    };

    try {
      const result = await request('POST', '/auth/oidc/verify-config', verification, 10000);
      if (!result.authorization_url) throw new Error('OIDC verification did not return an authorization URL');
      if (popup.closed) throw new Error('OIDC verification window was closed before provider sign-in began');
      popup.location.replace(result.authorization_url);
    } catch (error) {
      oidc.completed = true;
      clearOidcResources();
      notify(error.message, 'error');
    }
  }

  async function logoutSession(button) {
    setBusy(button, true, 'Logging out…');
    try {
      if (!window.debridPulseAuth) throw new Error('No browser application session is available');
      await window.debridPulseAuth.logout();
    } catch (error) {
      notify(error.message, 'error');
      setBusy(button, false);
    }
  }

  async function load() {
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
  }

  // app.js owns generic navigation and calls this canonical Settings entry point.
  window.loadSettings = load;
  try { loadSettings = load; } catch (_) {}

  window.DPSettingsPage = Object.freeze({load});
})();
