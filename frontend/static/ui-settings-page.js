/* DebridPulse v1.0.11 clean-room Settings page.
 *
 * This runtime deliberately does not consume the inherited Settings renderer,
 * serializer, tab lifecycle, authentication augmentation, or Settings DOM.
 * The backend APIs are the only migration contract.
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
      <section class="card dp-settings-card ${options.className || ''}">
        <div class="card-header">
          <span class="${titleClass}">${titleMarkup}</span>
          ${options.headerCenter ? `<div class="dp-settings-card-header-center">${options.headerCenter}</div>` : ''}
          ${options.action || ''}
        </div>
        <div class="card-body">${body}</div>
      </section>`;
  }

  function groupCard(title, body, options = {}) {
    return `
      <section class="card dp-settings-group-card ${options.className || ''}">
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

  function sourcesPanel(s) {
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
    });

    return groupCard('Debrid Services', provider, {
      className: 'dp-settings-source-group dp-settings-debrid-services',
    });
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
            type: 'number', min: 1, max: 100,
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
              type: 'number', min: 1, max: 64,
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
        type: 'number', min: 0, hint: '0 disables automatic stalled-download recovery.'
      })}
      ${input('aria2_error_retry_count', 'aria2 Error Retries', s.aria2_error_retry_count ?? 3, {
        type: 'number', min: 0, max: 20
      })}
      ${input('aria2_error_retry_delay_seconds', 'aria2 Retry Delay (seconds)', s.aria2_error_retry_delay_seconds ?? 60, {
        type: 'number', min: 0, max: 3600
      })}
    `);

    const filters = card('File Filters', `
      ${toggle('filters_enabled', 'Enable File Filters', 'Apply extension, keyword, sample, extras, and size rules.', s.filters_enabled)}
      <div class="dp-settings-filter-fields ${s.filters_enabled ? '' : 'is-disabled'}">
        ${textarea('blocked_extensions', 'Blocked Extensions (one per line)', (s.blocked_extensions || []).join('\n'), {rows: 5})}
        ${textarea('blocked_keywords', 'Blocked Keywords (one per line)', (s.blocked_keywords || []).join('\n'), {rows: 3})}
        ${input('min_file_size_mb', 'Minimum File Size (MB)', s.min_file_size_mb ?? 0, {type: 'number', min: 0})}
        ${toggle('block_samples', 'Block Samples / Trailers', 'Skip sample, trailer, and teaser files.', s.block_samples)}
        ${toggle('block_extras', 'Block Extras / Featurettes', 'Skip common extras and featurette folders.', s.block_extras)}
      </div>
      ${input('torrent_labels_raw', 'Download Labels', (s.torrent_labels || []).join(', '), {
        hint: 'Comma-separated labels available for downloads.'
      })}
    `);

    return delivery + recovery + filters;
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

  function authStatusCard(a) {
    const callback = a.oidc_callback_url || 'Configure External Base URL to derive callback';
    const modeRaw = String(a.mode || 'Unknown');
    const modeValue = modeRaw === 'OIDC'
      ? 'OpenID Connect'
      : modeRaw === 'No authentication'
        ? 'No Authentication'
        : modeRaw;

    const passwordOperational = !!a.password_enabled && !!a.password_ready;
    const oidcOperational = !!a.oidc_enabled && !!a.oidc_ready && a.oidc_available !== false;

    let modeTone = 'neutral';
    if (a.authentication_required) {
      modeTone = passwordOperational || oidcOperational ? 'green' : 'red';
    }

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

    let oidcValue = 'Not Configured';
    let oidcTone = 'neutral';
    if (a.oidc_configured) {
      if (!a.oidc_enabled) {
        oidcValue = 'Configured';
        oidcTone = 'yellow';
      } else if (a.oidc_available === false) {
        oidcValue = a.oidc_verified ? 'Verified · Runtime Unavailable' : 'Runtime Unavailable';
        oidcTone = 'red';
      } else if (a.oidc_verified) {
        oidcValue = 'Enabled & Verified';
        oidcTone = 'green';
      } else if (a.oidc_ready) {
        oidcValue = 'Configured & Enabled';
        oidcTone = 'yellow';
      } else {
        oidcValue = 'Configuration Error';
        oidcTone = 'red';
      }
    } else if (a.oidc_enabled) {
      oidcValue = 'Configuration Error';
      oidcTone = 'red';
    }

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
      ['OIDC State', oidcValue, oidcTone],
      ['API Token', tokenValue, tokenTone],
    ];
    return card('Authentication Status', `
      ${!a.authentication_required ? `
        <div class="dp-settings-caution dp-settings-auth-open-notice">
          <span><b>No interactive authentication enabled</b> — supported standalone/LAN mode; application and API are intentionally open.</span>
        </div>` : ''}
      <div class="dp-settings-status-grid dp-settings-auth-kpi-grid">
        ${items.map(([label, value, tone]) => `
          <div class="dash-hero-stat dp-settings-auth-kpi" data-c="${html(tone)}">
            <div class="dhs-body">
              <div class="dhs-label">${html(label)}</div>
              <div class="dhs-val">${html(value)}</div>
            </div>
          </div>`).join('')}
      </div>
      <div class="dp-settings-field">
        <label class="form-label">OIDC Callback URL</label>
        <input class="input" value="${html(callback)}" readonly>
      </div>
    `);
  }

  function authenticationPanel(a) {
    const externalBase = a.public_base_url_env_override ? (a.public_base_url_effective || '') : (a.public_base_url || '');
    const readonly = a.public_base_url_env_override ? 'readonly' : '';
    const provider = a.oidc_provider_name || 'OpenID Connect';
    const scopes = Array.isArray(a.oidc_scopes) ? a.oidc_scopes.join(' ') : '';
    const lines = values => Array.isArray(values) ? values.join('\n') : '';

    return authStatusCard(a) +
      card('Username & Password', `
        ${toggle('auth_password_enabled', 'Enable Username & Password', 'Local browser login plus HTTP Basic for API clients.', a.password_enabled)}
        ${input('auth_username', 'Username', a.username || '', {autocomplete: 'username', placeholder: 'operator'})}
        <div class="dp-settings-field">
          <label class="form-label" for="dp-auth-new-password">New Password</label>
          <input class="input" id="dp-auth-new-password" type="password" maxlength="4096" autocomplete="new-password"
                 placeholder="${html(a.password_configured ? 'Stored password configured — blank keeps it' : 'Set a password before enabling')}">
          <span class="form-hint">Entering a value replaces the stored Argon2id credential.</span>
        </div>
        <div class="dp-settings-actions">
          <button class="btn btn-danger btn-sm" type="button" data-action="clear-password" ${a.password_configured ? '' : 'disabled'}>Clear Stored Password</button>
        </div>
      `) +
      card('OpenID Connect', `
        ${toggle('auth_oidc_enabled', 'Enable OpenID Connect', 'Provider-neutral Authorization Code + PKCE login.', a.oidc_enabled)}
        ${input('oidc_provider_name', 'Provider Display Name', provider)}
        ${input('oidc_issuer_url', 'Issuer URL', a.oidc_issuer_url || '', {placeholder: 'https://id.example/application/o/debridpulse'})}
        ${input('oidc_client_id', 'Client ID', a.oidc_client_id || '')}
        <div class="dp-settings-field">
          <label class="form-label" for="dp-auth-oidc-secret">Client Secret</label>
          <input class="input" id="dp-auth-oidc-secret" type="password" autocomplete="off"
                 placeholder="${html(a.oidc_client_secret_configured ? 'Stored client secret configured — blank keeps it' : 'Optional for public clients')}">
          ${a.oidc_client_secret_configured ? `<label class="dp-settings-inline-check"><input id="dp-auth-clear-oidc-secret" type="checkbox"> Clear stored client secret</label>` : ''}
        </div>
        ${input('oidc_scopes', 'Scopes', scopes, {placeholder: 'openid profile email'})}
        ${toggle('oidc_allow_all', 'Allow Any Authenticated OIDC Identity', 'When off, configured subject/email/group rules authorize access.', a.oidc_allow_all)}
        ${textarea('oidc_allowed_subjects', 'Allowed Subjects (one per line)', lines(a.oidc_allowed_subjects), {rows: 3})}
        ${textarea('oidc_allowed_emails', 'Allowed Emails (one per line)', lines(a.oidc_allowed_emails), {rows: 3, hint: 'Email authorization requires email_verified=true.'})}
        ${textarea('oidc_allowed_groups', 'Allowed Groups (one per line)', lines(a.oidc_allowed_groups), {rows: 3})}
        ${input('oidc_group_claim', 'Group Claim', a.oidc_group_claim || 'groups')}
        <div class="dp-settings-actions">
          <button class="btn btn-blue" type="button" data-action="verify-oidc">Verify Sign-In</button>
        </div>
      `) +
      card('API Access', `
        ${toggle('api_token_enabled', 'Enable Bearer API Token', 'Use a dedicated token for automation and API clients.', a.api_token_enabled)}
        <p class="dp-settings-copy">Stored token state: <b>${a.api_token_configured ? 'Configured' : 'Not configured'}</b>.</p>
        <div class="dp-settings-actions">
          <button class="btn btn-blue btn-sm" type="button" data-action="generate-token">${a.api_token_configured ? 'Rotate Token' : 'Generate Token'}</button>
          <button class="btn btn-danger btn-sm" type="button" data-action="clear-token" ${a.api_token_configured ? '' : 'disabled'}>Clear Token</button>
        </div>
        ${state.oneTimeToken ? `
          <div class="dp-settings-token-once">
            <b>Copy this token now — it will not be shown again.</b>
            <div class="dp-settings-inline-field">
              <input class="input" id="dp-settings-api-token-once" readonly value="${html(state.oneTimeToken)}">
              <button class="btn btn-ghost btn-sm" type="button" data-action="copy-token">Copy</button>
            </div>
          </div>` : ''}
      `) +
      card('Sessions & Security', `
        <div class="dp-settings-field">
          <label class="form-label" for="dp-auth-public-base-url">External Base URL (Canonical Origin)</label>
          <input class="input" id="dp-auth-public-base-url" value="${html(externalBase)}" placeholder="https://download.example.com" ${readonly}>
          <span class="form-hint">${a.public_base_url_env_override
            ? 'Managed by the PUBLIC_BASE_URL environment variable.'
            : 'Canonical externally reachable HTTPS origin used for reverse-proxy origin validation, secure cookies, and OIDC callback construction.'}</span>
        </div>
        ${input('auth_session_lifetime_hours', 'Browser Session Lifetime (hours)', a.session_lifetime_hours || 12, {
          type: 'number', min: 1, max: 168
        })}
        <div class="dp-settings-status-grid">
          <div class="dp-settings-status"><b>Active browser sessions</b><span>${html(a.session_count ?? 0)}</span></div>
          <div class="dp-settings-status"><b>Current mechanism</b><span>${html(a.current_session_mechanism || 'Open / anonymous')}</span></div>
        </div>
        <div class="dp-settings-actions">
          <button class="btn btn-ghost btn-sm" type="button" data-action="logout-session">Log Out Current Session</button>
        </div>
      `);
  }

  function maintenancePanel(s) {
    return card('Backups & Retention', `
      ${toggle('backup_enabled', 'Enable Backups', 'Automatically back up configuration and database state.', s.backup_enabled !== false)}
      ${input('backup_folder', 'Backup Folder', s.backup_folder || '/app/data/backups')}
      ${input('backup_interval_hours', 'Backup Interval (hours)', s.backup_interval_hours ?? 24, {type: 'number', min: 1, max: 168})}
      ${input('backup_keep_days', 'Keep Backups (days)', s.backup_keep_days ?? 7, {type: 'number', min: 1, max: 90})}
      ${input('stats_snapshot_interval_minutes', 'Statistics Snapshot Interval (minutes)', s.stats_snapshot_interval_minutes ?? 60, {type: 'number', min: 1})}
      ${input('stats_snapshot_keep_days', 'Keep Statistics Snapshots (days)', s.stats_snapshot_keep_days ?? 30, {type: 'number', min: 1})}
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
              <div class="dp-settings-header-title">Settings</div>
              <div class="dp-settings-header-subtitle">Configure providers, downloads, automation, authentication, and maintenance.</div>
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
          </div>
          <button class="btn btn-primary" type="button" data-action="save">Apply Settings</button>
        </div>
      </section>`;

    activateTab(state.activeTab);
    bindEvents(view);
    updateModeState();
    updateFilterState();
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

    view.addEventListener('change', event => {
      if (event.target.matches(`[data-setting="aria2_mode"]`)) updateModeState();
      if (event.target.matches(`[data-setting="filters_enabled"]`)) updateFilterState();
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

  function updateFilterState() {
    const enabled = boolOf('filters_enabled');
    root()?.querySelectorAll('.dp-settings-filter-fields').forEach(el => {
      el.classList.toggle('is-disabled', !enabled);
      el.querySelectorAll('input, textarea, select').forEach(control => {
        control.disabled = !enabled;
      });
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

  function linesOf(key) {
    return valueOf(key).split('\n').map(item => item.trim()).filter(Boolean);
  }

  function clearSecrets() {
    return Array.from(root()?.querySelectorAll('[data-clear-secret]:checked') || [])
      .map(input => input.dataset.clearSecret)
      .filter(Boolean);
  }

  function nonAuthPayload() {
    const current = state.settings || {};
    const maxDownloads = intOf('aria2_max_active_downloads', Number(current.max_concurrent_downloads ?? 3));
    return {
      ...current,
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
      filters_enabled: boolOf('filters_enabled'),
      blocked_extensions: linesOf('blocked_extensions'),
      blocked_keywords: linesOf('blocked_keywords'),
      min_file_size_mb: intOf('min_file_size_mb', 0),
      block_samples: boolOf('block_samples'),
      block_extras: boolOf('block_extras'),
      torrent_labels: valueOf('torrent_labels_raw').split(',').map(item => item.trim()).filter(Boolean),

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
      render();
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
    if (!payload.auth_password_enabled && !payload.auth_oidc_enabled && state.auth?.authentication_required) {
      if (!window.confirm('Disable all interactive authentication and place DebridPulse in open mode?')) return false;
      payload.confirm_open_mode = true;
    }

    setBusy(button, true, 'Saving…');
    try {
      state.auth = await request('PUT', '/auth/config', payload, 15000);
      syncAuthIntoSettings(state.auth);
      state.activeTab = 'authentication';
      render();
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
      render();
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
    if (!window.confirm('This will remove all database rows. Continue?')) return;
    if (window.prompt('Type WIPE to confirm database wipe') !== 'WIPE') return;

    setBusy(button, true, 'Wiping…');
    try {
      const result = await request('POST', '/admin/database/wipe', {confirm: true}, 60000);
      notify(result.backup && !result.backup.skipped ? 'Database wiped. Pre-wipe backup created.' : 'Database wiped.', 'success');
      try { if (typeof loadStats === 'function') loadStats(); } catch (_) {}
      try { if (typeof loadRecent === 'function') loadRecent(); } catch (_) {}
      try {
        if (document.getElementById('view-torrents')?.classList.contains('active') && typeof loadTorrents === 'function') loadTorrents();
      } catch (_) {}
      render();
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(button, false);
    }
  }

  async function clearPassword(button) {
    if (!window.confirm('Clear the stored local password? Username & Password authentication will also be disabled.')) return;
    const payload = authPayload();
    payload.auth_password_enabled = false;
    payload.auth_password = '';
    payload.clear_password = true;
    if (!payload.auth_oidc_enabled && state.auth?.authentication_required) {
      if (!window.confirm('This also leaves no interactive authentication. Continue into open mode?')) return;
      payload.confirm_open_mode = true;
    }
    await persistAuth(button, payload, 'Stored password cleared');
  }

  async function setApiTokenEnabled(inputEl) {
    const desired = !!inputEl.checked;
    inputEl.disabled = true;
    try {
      const result = await request('PUT', '/auth/api-token', {enabled: desired}, 10000);
      state.auth.api_token_enabled = !!result.enabled;
      state.auth.api_token_configured = !!result.configured;
      render();
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
      render();
      notify(result.rotated ? 'API token rotated' : 'API token generated', 'success');
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(button, false);
    }
  }

  async function clearToken(button) {
    if (!window.confirm('Clear the API token? Existing automation using it will immediately lose access.')) return;
    setBusy(button, true, 'Clearing…');
    try {
      await request('DELETE', '/auth/api-token', undefined, 10000);
      state.auth.api_token_enabled = false;
      state.auth.api_token_configured = false;
      state.oneTimeToken = '';
      render();
      notify('API token cleared', 'success');
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
      state.auth = await request('GET', '/auth/config', undefined, 7000);
      syncAuthIntoSettings(state.auth);
      state.activeTab = 'authentication';
      render();
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
    state.loading = (async () => {
      const view = root();
      if (!view) return;
      view.classList.add('dp-settings-clean-view');
      view.innerHTML = '<div class="dp-settings-loading">Loading Settings…</div>';

      try {
        const [settings, authData] = await Promise.all([
          request('GET', '/settings', undefined, 10000),
          request('GET', '/auth/config', undefined, 7000),
        ]);
        syncGlobalSettings(settings);
        state.auth = authData;
        syncAuthIntoSettings(authData);
        render();
      } catch (error) {
        view.innerHTML = `<div class="dp-settings-load-error"><b>Settings could not be loaded.</b><span>${html(error.message)}</span></div>`;
        notify(error.message, 'error');
      }
    })().finally(() => {
      state.loading = null;
    });
    return state.loading;
  }

  // Transitional navigation hook only: app.js owns generic navigation and still
  // calls loadSettings(). Replace that one entry point with the clean-room page.
  // No inherited Settings renderer, serializer, tab lifecycle, or action function
  // is invoked by this runtime.
  window.loadSettings = load;
  try { loadSettings = load; } catch (_) {}

  window.DPSettingsPage = Object.freeze({load});
})();