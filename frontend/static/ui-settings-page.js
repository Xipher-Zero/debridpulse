/* DebridPulse v1.0.11 authoritative Settings page.
 *
 * Settings is rebuilt directly from settingsData into the same shared page/card
 * language used by Dashboard, Downloads and Activity Log. This runtime owns the
 * final Settings DOM, tab lifecycle and serializer boundary. It deliberately
 * does NOT call the inherited Settings renderer, reparent inherited cards, infer
 * render completion from DOM mutations, or preserve removed controls through
 * hidden DOM.
 */
(function () {
  'use strict';

  const TABS = Object.freeze([
    ['tab-general', 'Sources & Providers'],
    ['tab-download', 'Downloads'],
    ['tab-extract', 'Extraction'],
    ['tab-notifications', 'Notifications'],
    ['tab-authentication', 'Authentication'],
    ['tab-database', 'Data & Maintenance'],
    ['tab-advanced', 'Advanced'],
  ]);

  let authViewData = null;
  let authViewBusy = false;

  function html(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function checked(value) {
    return value ? ' checked' : '';
  }

  function disabled(value) {
    return value ? ' disabled' : '';
  }

  function readonly(value) {
    return value ? ' readonly' : '';
  }

  function numberValue(source, key, fallback) {
    const value = source && source[key] != null ? source[key] : fallback;
    return Number.isFinite(Number(value)) ? Number(value) : fallback;
  }

  function textValue(source, key, fallback = '') {
    const value = source && source[key] != null ? source[key] : fallback;
    return String(value == null ? '' : value);
  }

  function field(id, label, value, options = {}) {
    const type = options.type || 'text';
    const attrs = [
      `class="input"`,
      `id="${html(id)}"`,
      `type="${html(type)}"`,
      `value="${html(value)}"`,
    ];
    if (options.placeholder) attrs.push(`placeholder="${html(options.placeholder)}"`);
    if (options.min != null) attrs.push(`min="${html(options.min)}"`);
    if (options.max != null) attrs.push(`max="${html(options.max)}"`);
    if (options.step != null) attrs.push(`step="${html(options.step)}"`);
    if (options.autocomplete) attrs.push(`autocomplete="${html(options.autocomplete)}"`);
    if (options.readonly) attrs.push('readonly');
    if (options.disabled) attrs.push('disabled');
    if (options.onchange) attrs.push(`onchange="${html(options.onchange)}"`);

    return `<div class="form-group dp-settings-field">
      <label class="form-label" for="${html(id)}">${html(label)}</label>
      <input ${attrs.join(' ')}>
      ${options.hint ? `<div class="form-hint">${options.hint}</div>` : ''}
    </div>`;
  }

  function textarea(id, label, value, options = {}) {
    return `<div class="form-group dp-settings-field">
      <label class="form-label" for="${html(id)}">${html(label)}</label>
      <textarea class="input" id="${html(id)}" rows="${Number(options.rows || 3)}"${options.placeholder ? ` placeholder="${html(options.placeholder)}"` : ''}>${html(value)}</textarea>
      ${options.hint ? `<div class="form-hint">${options.hint}</div>` : ''}
    </div>`;
  }

  function selectField(id, label, value, choices, options = {}) {
    const optionHtml = choices.map(function (choice) {
      const optionValue = String(choice[0]);
      return `<option value="${html(optionValue)}"${String(value) === optionValue ? ' selected' : ''}>${html(choice[1])}</option>`;
    }).join('');
    return `<div class="form-group dp-settings-field">
      <label class="form-label" for="${html(id)}">${html(label)}</label>
      <select class="input" id="${html(id)}"${options.disabled ? ' disabled' : ''}${options.onchange ? ` onchange="${html(options.onchange)}"` : ''}>${optionHtml}</select>
      ${options.hint ? `<div class="form-hint">${options.hint}</div>` : ''}
    </div>`;
  }

  function toggle(id, label, value, hint = '', options = {}) {
    return `<div class="toggle-row dp-settings-toggle">
      <div class="toggle-info">
        <div class="tl">${html(label)}</div>
        ${hint ? `<div class="ts">${hint}</div>` : ''}
      </div>
      <label class="toggle">
        <input type="checkbox" id="${html(id)}"${checked(value)}${disabled(options.disabled)}${options.onchange ? ` onchange="${html(options.onchange)}"` : ''}>
        <div class="ttrack"></div>
      </label>
    </div>`;
  }

  function clearSecret(fieldName, label, configured) {
    if (!configured) return '';
    const id = `s-clear-${fieldName}`;
    return `<label class="dp-settings-clear-secret" for="${html(id)}">
      <span><b>${html(label)}</b><small>Erase the stored value when Settings are saved.</small></span>
      <input class="dp-check" type="checkbox" id="${html(id)}">
    </label>`;
  }

  function actionRow(contents) {
    return `<div class="dp-settings-actions">${contents}</div>`;
  }

  function card(title, copy, body, key = '') {
    return `<section class="card dp-settings-section-card"${key ? ` data-dp-settings-card="${html(key)}"` : ''}>
      <div class="card-header"><span class="card-title">${html(title)}</span></div>
      <div class="card-body">
        ${copy ? `<p class="dp-settings-section-copy">${copy}</p>` : ''}
        ${body}
      </div>
    </section>`;
  }

  function providerStatusText() {
    const api = document.getElementById('lbl-api')?.textContent?.trim();
    const premium = document.getElementById('lbl-premium')?.textContent?.trim();
    return [api, premium].filter(Boolean).join(' · ') || 'Status unavailable';
  }

  function sourcesPanel(s) {
    const configured = !!s.alldebrid_api_key_configured;
    const connection = `
      <div class="dp-settings-status-grid">
        <div class="dp-settings-status-item"><b>Stored API key</b><span>${configured ? 'Configured' : 'Not configured'}</span></div>
        <div class="dp-settings-status-item"><b>Provider status</b><span>${html(providerStatusText())}</span></div>
      </div>
      ${field('s-alldebrid_api_key', 'AllDebrid API Key', '', {
        type: 'password',
        autocomplete: 'off',
        placeholder: configured ? 'Stored API key configured — blank keeps the current key' : 'Enter AllDebrid API key',
      })}
      ${clearSecret('alldebrid_api_key', 'Clear Stored API Key', configured)}`;

    const advanced = [
      field('s-alldebrid_rate_limit_per_minute', 'API calls per minute', numberValue(s, 'alldebrid_rate_limit_per_minute', 60), {
        type: 'number', min: 0, max: 300,
        hint: 'Set to 0 for unlimited.',
      }),
      field('s-poll_interval_seconds', 'AllDebrid Poll Interval (seconds)', numberValue(s, 'poll_interval_seconds', 30), {
        type: 'number', min: 10,
        hint: 'Lower values detect provider-state changes sooner but increase API traffic.',
      }),
      field('s-full_sync_interval_minutes', 'Full AllDebrid Sync Interval (minutes)', numberValue(s, 'full_sync_interval_minutes', 5), {
        type: 'number', min: 0, max: 1440,
        hint: 'Reconciles all known AllDebrid magnets. 0 disables periodic full reconciliation.',
      }),
      field('s-upload_fail_retry_count', 'Upload Retry Count', numberValue(s, 'upload_fail_retry_count', 3), {
        type: 'number', min: 0, max: 10,
        hint: 'Retries provider Upload Failed responses. 0 disables automatic retry.',
      }),
      field('s-upload_fail_retry_delay_minutes', 'Upload Retry Delay (minutes)', numberValue(s, 'upload_fail_retry_delay_minutes', 5), {
        type: 'number', min: 1, max: 60,
      }),
    ].join('');

    return card(
      'AllDebrid — Connection',
      'AllDebrid is the current V1 acquisition provider. Credentials and provider-specific behavior live here.',
      connection,
      'provider-alldebrid-connection'
    ) + card(
      'AllDebrid — Advanced Provider Behavior',
      'Provider-specific rate, polling, reconciliation, and submission recovery controls.',
      advanced,
      'provider-alldebrid-advanced'
    );
  }

  function downloadsPanel(s) {
    const builtIn = (s.aria2_mode || 'builtin') === 'builtin';
    const ariaSecretConfigured = !!s.aria2_secret_configured;
    const filtersEnabled = s.filters_enabled !== false;
    const modeChange = "const activeTab=getActiveSettingsTab(); settingsData.aria2_mode=this.value; renderSettings(); switchSettingsTab(activeTab); loadAria2Runtime().catch(()=>{});";

    const engine = selectField('s-aria2_mode', 'aria2 Mode', s.aria2_mode || 'builtin', [
      ['builtin', 'Built-in aria2'],
      ['external', 'External aria2'],
    ], {
      onchange: modeChange,
      hint: 'Built-in aria2 is managed by DebridPulse. External mode connects to an independently managed aria2 daemon.',
    });

    const external = field('s-aria2_url', 'aria2 RPC URL', builtIn ? `http://127.0.0.1:${numberValue(s, 'aria2_builtin_port', 6800)}/jsonrpc` : textValue(s, 'aria2_url', 'http://127.0.0.1:6800/jsonrpc'), {
      disabled: builtIn,
      placeholder: 'http://127.0.0.1:6800/jsonrpc',
    }) + field('s-aria2_secret', 'aria2 RPC Secret', '', {
      type: 'password',
      autocomplete: 'off',
      disabled: builtIn,
      placeholder: ariaSecretConfigured ? 'Stored RPC secret configured — blank keeps it' : 'Optional RPC secret',
    }) + clearSecret('aria2_secret', 'Clear Stored aria2 RPC Secret', ariaSecretConfigured && !builtIn);

    const paths = field('s-download_folder', 'Built-in aria2 Download Folder', textValue(s, 'download_folder', ''), {
      hint: 'Filesystem path used by the DebridPulse-managed aria2 daemon.',
    }) + field('s-aria2_download_path', 'External aria2 Download Path', textValue(s, 'aria2_download_path', ''), {
      disabled: builtIn,
      placeholder: 'Optional external aria2 path',
      hint: 'The same destination as seen by the external aria2 daemon.',
    });

    const concurrency = field('s-aria2_max_active_downloads', 'Maximum Concurrent Downloads', numberValue(s, 'aria2_max_active_downloads', numberValue(s, 'max_concurrent_downloads', 3)), {
      type: 'number', min: 1, max: 50,
      hint: 'Maximum files DebridPulse hands to aria2 concurrently.',
    });

    const storage = field('s-min_free_disk_gb', 'Minimum Free Disk Space (GB, 0 = disabled)', numberValue(s, 'min_free_disk_gb', 0), {
      type: 'number', min: 0, step: 0.5,
      hint: 'New dispatches wait below this threshold while active transfers are allowed to finish.',
    }) + field('s-disk_guard_resume_hysteresis_gb', 'Resume Hysteresis (GB above threshold)', numberValue(s, 'disk_guard_resume_hysteresis_gb', 0.5), {
      type: 'number', min: 0, step: 0.1,
      hint: 'Prevents dispatch from flapping around the free-space threshold.',
    });

    const recovery = field('s-stuck_download_timeout_hours', 'Stalled Download Timeout (hours)', numberValue(s, 'stuck_download_timeout_hours', 6), {
      type: 'number', min: 0, max: 168,
      hint: '0 disables timed recovery.',
    }) + field('s-aria2_error_retry_count', 'aria2 Retry Count', numberValue(s, 'aria2_error_retry_count', 3), {
      type: 'number', min: 0, max: 20,
    }) + field('s-aria2_error_retry_delay_seconds', 'aria2 Retry Delay (seconds)', numberValue(s, 'aria2_error_retry_delay_seconds', 60), {
      type: 'number', min: 0, max: 3600,
    });

    const blockedExtensions = Array.isArray(s.blocked_extensions) ? s.blocked_extensions.join('\n') : '';
    const blockedKeywords = Array.isArray(s.blocked_keywords) ? s.blocked_keywords.join('\n') : '';
    const selection = toggle(
      's-filters_enabled',
      'Enable File Filters',
      filtersEnabled,
      'When disabled, DebridPulse materializes every file returned by the provider.',
      {onchange: 'toggleFilterFields()'}
    ) + `<div id="filter-fields" class="dp-settings-filter-fields"${filtersEnabled ? '' : ' style="opacity:.4;pointer-events:none"'}>
      ${textarea('s-blocked_extensions', 'Blocked Extensions (one per line)', blockedExtensions, {rows: 5, hint: 'Examples: .jpg, .png, .nfo'})}
      ${textarea('s-blocked_keywords', 'Blocked Keywords (one per line)', blockedKeywords, {rows: 3})}
      ${field('s-min_file_size_mb', 'Minimum File Size (MB, 0 = no limit)', numberValue(s, 'min_file_size_mb', 0), {type: 'number', min: 0})}
      ${toggle('s-block_samples', 'Block sample / trailer files', !!s.block_samples, 'Skip files matching common sample, trailer, or teaser patterns.')}
      ${toggle('s-block_extras', 'Block extras / featurettes', !!s.block_extras, 'Skip files located in common Extras / Featurettes directories.')}
    </div>`;

    const organization = field('s-torrent_labels_raw', 'Predefined Labels', (Array.isArray(s.torrent_labels) ? s.torrent_labels : []).join(', '), {
      placeholder: 'Movies, Series, 4K, Anime',
      hint: 'Comma-separated labels available for local organization.',
    });

    return card('Download Engine', 'Choose whether DebridPulse manages its built-in aria2 instance or connects to an external aria2 service.', engine, 'download-engine') +
      card('External aria2', 'Connection settings used only when External aria2 is selected.', external, 'external-aria2') +
      card('Paths', 'Download locations as seen by DebridPulse and, when used, the external aria2 daemon.', paths, 'download-paths') +
      card('Concurrency', 'Controls how many downloads DebridPulse allows to be active at once.', concurrency, 'download-concurrency') +
      card('Storage Protection', 'Protect the destination from exhausting free space while allowing already-active transfers to finish.', storage, 'storage-protection') +
      card('Recovery', 'Provider-neutral stalled-download recovery plus delivery-engine retry policy.', recovery, 'download-recovery') +
      card('File Selection', 'Choose which files DebridPulse should materialize after content has been acquired.', selection, 'file-selection') +
      card('Organization', 'Local labels used to organize downloads inside DebridPulse.', organization, 'download-organization');
  }

  function extractionPanel(s) {
    const configured = !!s.extraction_password_configured || !!s.extraction_password;
    const behavior = toggle('s-extract_enabled', 'Enable Auto-Extraction', !!s.extract_enabled, 'Automatically extract supported archives after download completion.') +
      toggle('s-extract_delete_archive', 'Delete Archive After Extraction', s.extract_delete_archive !== false, 'Remove source archives after successful extraction.') +
      field('s-extract_max_concurrent', 'Maximum Concurrent Extractions', numberValue(s, 'extract_max_concurrent', 1), {
        type: 'number', min: 1, max: 10,
        hint: 'Default: 1 to keep NAS and server workloads responsive.',
      });

    const passwords = `<div class="form-group dp-settings-field">
      <label class="form-label">Extraction passwords</label>
      <div id="extraction-pw-list" class="dp-settings-password-list"></div>
      <button class="btn btn-ghost btn-sm" onclick="addExtractionPassword()" type="button">+ Add password</button>
      <div class="form-hint">Passwords are tried in order for supported encrypted archives.</div>
      <input type="hidden" id="s-extraction_password" value="${html(textValue(s, 'extraction_password', ''))}">
    </div>${clearSecret('extraction_password', 'Clear Stored Archive Passwords', configured)}`;

    return card('Auto-Extraction', 'Automatic archive extraction behavior after a download completes.', behavior, 'extraction-behavior') +
      card('Archive Passwords', 'Passwords are tried in order for supported encrypted archives.', passwords, 'extraction-passwords');
  }

  function notificationsPanel(s) {
    const mainConfigured = !!s.discord_webhook_url_configured;
    const addedConfigured = !!s.discord_webhook_added_configured;
    const reportConfigured = !!s.stats_report_webhook_url_configured;
    const avatar = textValue(s, 'discord_avatar_url', '');

    const discord = field('s-discord_username', 'Sender Name', textValue(s, 'discord_username', 'DebridPulse'), {
      placeholder: 'DebridPulse',
    }) + `<div class="form-group dp-settings-field">
      <label class="form-label" for="s-discord_avatar_url">Sender Avatar</label>
      <div class="dp-settings-inline-field">
        <input class="input" id="s-discord_avatar_url" value="${html(avatar)}" placeholder="https://…/avatar.png">
        <label class="btn btn-ghost btn-sm dp-settings-upload">Upload<input type="file" accept="image/png,image/jpeg,image/gif,image/webp" hidden onchange="uploadDiscordAvatar(this)"></label>
      </div>
      <div id="avatar-preview" class="dp-settings-avatar-preview" style="display:none">
        <img id="avatar-preview-img" src="" alt="Discord avatar preview">
        <span id="avatar-preview-label"></span>
        <button class="btn btn-ghost btn-sm" type="button" onclick="clearDiscordAvatar()">Remove</button>
      </div>
    </div>` + field('s-discord_webhook_url', 'Main Webhook URL', '', {
      type: 'password', autocomplete: 'off',
      placeholder: mainConfigured ? 'Stored webhook configured — blank keeps it' : 'https://discord.com/api/webhooks/…',
    }) + field('s-discord_webhook_added', 'Torrent Added Webhook URL', '', {
      type: 'password', autocomplete: 'off',
      placeholder: addedConfigured ? 'Stored webhook configured — blank keeps it' : 'Optional separate webhook',
    }) + clearSecret('discord_webhook_url', 'Clear Stored Main Webhook', mainConfigured) +
      clearSecret('discord_webhook_added', 'Clear Stored Added-Event Webhook', addedConfigured);

    const events = toggle('s-discord_notify_added', 'Notify on Added', !!s.discord_notify_added) +
      toggle('s-discord_notify_finished', 'Notify on Finished', !!s.discord_notify_finished) +
      toggle('s-discord_notify_error', 'Notify on Error', !!s.discord_notify_error) +
      toggle('s-discord_notify_extract', 'Notify on Extraction', s.discord_notify_extract !== false);

    const updates = toggle('s-discord_notify_update', 'Notify on new version', s.discord_notify_update !== false, 'Send a webhook when a newer DebridPulse release is available.') +
      field('s-update_check_interval_hours', 'Version Check Interval (hours, 0 = disabled)', numberValue(s, 'update_check_interval_hours', 12), {
        type: 'number', min: 0, max: 168,
      });

    const reportWindow = numberValue(s, 'stats_report_window_hours', 24);
    const reports = field('s-stats_report_webhook_url', 'Reporting Webhook URL', '', {
      type: 'password', autocomplete: 'off',
      placeholder: reportConfigured ? 'Stored reporting webhook configured — blank keeps it' : 'Leave blank to use the main Discord webhook',
    }) + field('s-stats_report_interval_hours', 'Automatic Report Interval (hours, 0 = disabled)', numberValue(s, 'stats_report_interval_hours', 0), {
      type: 'number', min: 0, max: 168,
    }) + selectField('stats-report-hours', 'Report Window', reportWindow, [
      ['24', 'Last 24 hours'],
      ['168', 'Last 7 days'],
      ['720', 'Last 30 days'],
      ['8760', 'All time (~1 year)'],
    ]) + clearSecret('stats_report_webhook_url', 'Clear Stored Reporting Webhook', reportConfigured) +
      actionRow('<button class="btn btn-ghost btn-sm" type="button" onclick="sendStatsReport(this)">Send Test Report</button>');

    return card('Discord', 'Sender identity and webhook destinations for Discord notifications.', discord, 'notifications-discord') +
      card('Event Notifications', 'Choose which DebridPulse events produce notifications.', events, 'notifications-events') +
      card('Update Notification', 'Control new-version notifications and how often DebridPulse checks for a release.', updates, 'notifications-updates') +
      card('Scheduled Statistics Reports', 'Configure automatic statistics delivery. Interactive statistics browsing stays on the Statistics page.', reports, 'notifications-reports');
  }

  function dataMaintenancePanel(s) {
    const backups = toggle('s-backup_enabled', 'Enable Backups', s.backup_enabled !== false, 'Automatically back up configuration and database state.') +
      field('s-backup_folder', 'Backup Folder', textValue(s, 'backup_folder', '/app/data/backups')) +
      field('s-backup_interval_hours', 'Backup Interval (hours)', numberValue(s, 'backup_interval_hours', 24), {type: 'number', min: 1, max: 168}) +
      field('s-backup_keep_days', 'Keep Backups (days)', numberValue(s, 'backup_keep_days', 7), {type: 'number', min: 1, max: 90}) +
      actionRow('<button class="btn btn-ghost btn-sm" type="button" onclick="triggerBackup(this)">Run Backup Now</button><button class="btn btn-ghost btn-sm" type="button" onclick="loadBackupList()">List Backups</button>') +
      '<div id="backup-list" class="dp-settings-result-list"></div>';

    const retention = field('s-stats_snapshot_interval_minutes', 'Statistics Snapshot Interval (minutes, 0 = disabled)', numberValue(s, 'stats_snapshot_interval_minutes', 60), {type: 'number', min: 0}) +
      field('s-stats_snapshot_keep_days', 'Keep Statistics Snapshots (days)', numberValue(s, 'stats_snapshot_keep_days', 30), {type: 'number', min: 1}) +
      field('s-events_keep_days', 'Event Log Retention (days, 0 = keep forever)', numberValue(s, 'events_keep_days', 30), {type: 'number', min: 0});

    const danger = toggle('s-db_wipe_enabled', 'Allow Database Wipe', !!s.db_wipe_enabled, 'Required before the destructive wipe action can run.') +
      toggle('s-db_backup_before_wipe', 'Backup Before Wipe', s.db_backup_before_wipe !== false, 'Create a database backup before deleting rows.') +
      actionRow('<button class="btn btn-danger btn-sm" type="button" onclick="wipeDatabase(this)">Wipe Database</button>');

    return card('DebridPulse Backups', 'Automatic application backups covering configuration, database state, and managed data.', backups, 'data-backups') +
      card('Data Retention', 'Controls how long DebridPulse retains statistics snapshots and Event Log history.', retention, 'data-retention') +
      card('Danger Zone', 'Destructive database maintenance. These controls intentionally require an explicit safety gate.', danger, 'danger-zone');
  }

  function advancedPanel(s) {
    const tuning = field('s-aria2_split', 'aria2 Split Connections', numberValue(s, 'aria2_split', 16), {type: 'number', min: 1, max: 64}) +
      field('s-aria2_min_split_size', 'aria2 Minimum Split Size', textValue(s, 'aria2_min_split_size', '10M'), {placeholder: '10M'}) +
      field('s-aria2_max_connection_per_server', 'aria2 Maximum Connections per Server', numberValue(s, 'aria2_max_connection_per_server', 16), {type: 'number', min: 1, max: 32}) +
      field('s-aria2_disk_cache', 'aria2 Disk Cache', textValue(s, 'aria2_disk_cache', '64M'), {placeholder: '64M'}) +
      selectField('s-aria2_file_allocation', 'aria2 File Allocation', textValue(s, 'aria2_file_allocation', 'falloc'), [
        ['none', 'none'], ['prealloc', 'prealloc'], ['trunc', 'trunc'], ['falloc', 'falloc'],
      ]) +
      field('s-aria2_lowest_speed_limit', 'aria2 Lowest Speed Limit', textValue(s, 'aria2_lowest_speed_limit', '0'), {placeholder: '0'}) +
      toggle('s-aria2_continue_downloads', 'Continue Partial Downloads', s.aria2_continue_downloads !== false, 'Allow aria2 to resume partial HTTP downloads when possible.');

    return card('Transfer Engine Tuning', 'Low-level aria2 transfer behavior for unusual storage or network constraints.', tuning, 'transfer-engine-tuning');
  }

  function authFallbackData(s) {
    return {
      authentication_required: !!(s.auth_password_enabled || s.auth_oidc_enabled),
      mode: s.auth_oidc_enabled ? 'OIDC' : (s.auth_password_enabled ? 'Username & Password' : 'Open'),
      password_enabled: !!s.auth_password_enabled,
      password_configured: !!s.auth_password_configured,
      username: s.auth_username || '',
      oidc_enabled: !!s.auth_oidc_enabled,
      oidc_configured: !!s.auth_oidc_enabled && !!s.oidc_issuer_url && !!s.oidc_client_id,
      oidc_available: null,
      oidc_provider_name: s.oidc_provider_name || 'OpenID Connect',
      oidc_issuer_url: s.oidc_issuer_url || '',
      oidc_client_id: s.oidc_client_id || '',
      oidc_client_secret_configured: !!s.oidc_client_secret_configured,
      oidc_scopes: Array.isArray(s.oidc_scopes) ? s.oidc_scopes : ['openid', 'profile', 'email'],
      oidc_allow_all: !!s.oidc_allow_all,
      oidc_allowed_subjects: Array.isArray(s.oidc_allowed_subjects) ? s.oidc_allowed_subjects : [],
      oidc_allowed_emails: Array.isArray(s.oidc_allowed_emails) ? s.oidc_allowed_emails : [],
      oidc_allowed_groups: Array.isArray(s.oidc_allowed_groups) ? s.oidc_allowed_groups : [],
      oidc_group_claim: s.oidc_group_claim || 'groups',
      public_base_url: s.public_base_url || '',
      public_base_url_effective: s.public_base_url || '',
      public_base_url_env_override: false,
      oidc_callback_url: '',
      api_token_enabled: false,
      api_token_configured: false,
      current_session_mechanism: 'unknown',
      session_lifetime_hours: numberValue(s, 'auth_session_lifetime_hours', 12),
      session_count: 0,
    };
  }

  function authState(value) {
    if (value === true) return '<span class="dp-settings-state dp-settings-state--ok">Available</span>';
    if (value === false) return '<span class="dp-settings-state dp-settings-state--error">Unavailable</span>';
    return '<span class="dp-settings-state">Not active</span>';
  }

  function authenticationCards(a) {
    const passwordConfigured = a.password_configured ? 'Configured' : 'Not configured';
    const tokenConfigured = a.api_token_configured ? 'Configured' : 'Not configured';
    const oidcSecret = a.oidc_client_secret_configured ? 'Stored client secret configured — blank keeps it' : 'Optional / public client';
    const externalBase = a.public_base_url_env_override ? (a.public_base_url_effective || '') : (a.public_base_url || '');

    const status = `${!a.authentication_required ? '<div class="dp-settings-caution"><b>No authentication enabled</b><span>This is a supported standalone/LAN configuration. The application and API are intentionally open.</span></div>' : ''}
      <div class="dp-settings-status-grid">
        <div class="dp-settings-status-item"><b>Effective mode</b><span>${html(a.mode || 'Unknown')}</span></div>
        <div class="dp-settings-status-item"><b>OIDC configured</b><span>${a.oidc_configured ? 'Yes' : 'No'}</span></div>
        <div class="dp-settings-status-item"><b>OIDC runtime</b><span>${authState(a.oidc_available)}</span></div>
        <div class="dp-settings-status-item"><b>Provider</b><span>${html(a.oidc_provider_name || 'OpenID Connect')}</span></div>
        <div class="dp-settings-status-item"><b>API token</b><span>${html(tokenConfigured)}</span></div>
        <div class="dp-settings-status-item"><b>Current mechanism</b><span>${html(a.current_session_mechanism || 'open / anonymous')}</span></div>
      </div>
      ${field('auth-oidc-callback-display', 'Public OIDC Callback URL', a.oidc_callback_url || 'Configure Public Base URL to derive callback', {readonly: true})}`;

    const password = toggle('auth-password-enabled', 'Enable Username & Password', !!a.password_enabled, 'Local interactive login plus HTTP Basic for API clients.') +
      field('auth-username', 'Username', a.username || '', {autocomplete: 'username', placeholder: 'operator'}) +
      field('auth-new-password', 'New Password', '', {
        type: 'password', autocomplete: 'new-password',
        placeholder: a.password_configured ? 'Stored password configured — blank keeps it' : 'Set a password before enabling',
        hint: `Stored state: <b>${html(passwordConfigured)}</b>. Entering a value replaces the stored password.`,
      }) + actionRow(`<button class="btn btn-danger btn-sm" type="button" onclick="clearAuthenticationPassword(this)"${a.password_configured ? '' : ' disabled'}>Clear Stored Password</button>`);

    const oidc = toggle('auth-oidc-enabled', 'Enable OpenID Connect', !!a.oidc_enabled, 'OIDC is preferred on the unified login page when both mechanisms are enabled.') +
      field('auth-oidc-provider', 'Provider Display Name', a.oidc_provider_name || 'OpenID Connect') +
      field('auth-oidc-issuer', 'Issuer URL', a.oidc_issuer_url || '', {placeholder: 'https://id.example/application/o/debridpulse'}) +
      field('auth-oidc-client-id', 'Client ID', a.oidc_client_id || '') +
      field('auth-oidc-client-secret', 'Client Secret', '', {type: 'password', autocomplete: 'off', placeholder: oidcSecret}) +
      `<label class="dp-settings-clear-secret" for="auth-clear-oidc-secret"><span><b>Clear Stored Client Secret</b><small>Explicitly erase the persisted OIDC client secret on save.</small></span><input class="dp-check" type="checkbox" id="auth-clear-oidc-secret"></label>` +
      field('auth-oidc-scopes', 'Scopes', (Array.isArray(a.oidc_scopes) ? a.oidc_scopes : []).join(' '), {placeholder: 'openid profile email'}) +
      field('auth-oidc-callback-derived', 'Derived Callback URL', a.oidc_callback_url || 'Configure External Base URL to derive callback', {readonly: true}) +
      toggle('auth-oidc-allow-all', 'Allow any authenticated OIDC identity', !!a.oidc_allow_all, 'When disabled, configured subjects, emails, or groups become authorization requirements.') +
      textarea('auth-oidc-subjects', 'Allowed Subjects (one per line)', (Array.isArray(a.oidc_allowed_subjects) ? a.oidc_allowed_subjects : []).join('\n'), {rows: 3, hint: 'Use issuer-qualified identities in the form &lt;issuer&gt;|&lt;sub&gt;.'}) +
      textarea('auth-oidc-emails', 'Allowed Emails (one per line)', (Array.isArray(a.oidc_allowed_emails) ? a.oidc_allowed_emails : []).join('\n'), {rows: 3, hint: 'Email authorization requires email_verified=true.'}) +
      textarea('auth-oidc-groups', 'Allowed Groups (one per line)', (Array.isArray(a.oidc_allowed_groups) ? a.oidc_allowed_groups : []).join('\n'), {rows: 3}) +
      field('auth-oidc-group-claim', 'Group Claim', a.oidc_group_claim || 'groups') +
      actionRow('<button class="btn btn-primary btn-sm" type="button" onclick="verifyOidcSignIn(this)">Verify Sign-In</button>');

    const apiAccess = toggle('auth-api-token-enabled', 'Enable API token', !!a.api_token_enabled, 'Supplemental machine credential for automation and API clients.', {onchange: 'setApiTokenEnabled(this)'}) +
      `<div class="form-hint dp-settings-token-state">Stored state: <b>${html(tokenConfigured)}</b>. The full token is shown only when generated or rotated.</div>` +
      actionRow(`<button class="btn btn-primary btn-sm" type="button" onclick="generateApiToken(this)">${a.api_token_configured ? 'Rotate Token' : 'Generate Token'}</button><button class="btn btn-danger btn-sm" type="button" onclick="clearApiToken(this)"${a.api_token_configured ? '' : ' disabled'}>Clear Token</button>`) +
      `<div id="auth-api-token-once" class="dp-settings-token-once" style="display:none"><b>Copy this token now — it will not be shown again.</b><div class="dp-settings-inline-field"><input class="input" id="auth-api-token-value" readonly><button class="btn btn-ghost btn-sm" type="button" onclick="copyApiToken()">Copy</button></div></div>`;

    const sessions = field('auth-public-base-url', 'External Base URL (Canonical Origin)', externalBase, {
      readonly: !!a.public_base_url_env_override,
      placeholder: 'https://download.example.com',
      hint: a.public_base_url_env_override
        ? 'Effective value is supplied by the PUBLIC_BASE_URL deployment environment variable.'
        : 'Canonical externally reachable HTTPS origin used for origin validation, secure cookies, and OIDC callback construction.',
    }) + field('auth-session-hours', 'Browser Session Lifetime (hours)', numberValue(a, 'session_lifetime_hours', 12), {type: 'number', min: 1, max: 168}) +
      `<div class="form-hint">Current mechanism: <b>${html(a.current_session_mechanism || 'unknown')}</b> · Active in-process sessions: <b>${Number(a.session_count || 0)}</b></div>` +
      actionRow('<button class="btn btn-ghost btn-sm" type="button" onclick="logoutAuthenticationSession(this)">Log Out</button>');

    return card('Authentication Status', '', status, 'authentication-status') +
      card('Username & Password', 'Browser users sign in through the DebridPulse login page. REST clients may also use HTTP Basic with the same credentials.', password, 'authentication-password') +
      card('OpenID Connect', 'Provider-neutral Authorization Code + PKCE login. Verify Sign-In performs the real provider flow.', oidc, 'authentication-oidc') +
      card('API Access', 'Bearer tokens are intended for automation, scripts, monitoring, and high-frequency API access.', apiAccess, 'authentication-api') +
      card('Sessions & Security', '', sessions, 'authentication-sessions');
  }

  function authenticationPanel(s) {
    return authenticationCards(authViewData || authFallbackData(s));
  }

  function visibleOneTimeApiToken() {
    const box = document.getElementById('auth-api-token-once');
    const tokenField = document.getElementById('auth-api-token-value');
    if (!box || !tokenField || box.style.display === 'none') return '';
    return String(tokenField.value || '');
  }

  function restoreOneTimeApiToken(token) {
    if (!token) return;
    const box = document.getElementById('auth-api-token-once');
    const tokenField = document.getElementById('auth-api-token-value');
    if (!box || !tokenField) return;
    tokenField.value = token;
    box.style.display = '';
  }

  async function refreshAuthenticationView() {
    if (authViewBusy) return;
    authViewBusy = true;
    try {
      authViewData = await api('GET', '/auth/config', null, 5000);
      // generateApiToken() renders first and reveals the one-time token while
      // this request can still be in flight. Capture immediately before the
      // panel replacement so an auth-status refresh cannot erase that token.
      const oneTimeToken = visibleOneTimeApiToken();
      const panel = document.getElementById('tab-authentication');
      if (panel) {
        panel.innerHTML = authenticationCards(authViewData);
        restoreOneTimeApiToken(oneTimeToken);
      }
    } catch (error) {
      const panel = document.getElementById('tab-authentication');
      if (panel && !panel.querySelector('.dp-settings-auth-load-error')) {
        panel.insertAdjacentHTML('afterbegin', `<div class="dp-settings-auth-load-error">Authentication status refresh failed: ${html(error.message || error)}</div>`);
      }
    } finally {
      authViewBusy = false;
    }
  }

  function panelHtml(id, contents) {
    return `<div class="stab-panel" id="${html(id)}" role="tabpanel" aria-labelledby="settings-tab-${html(id)}">${contents}</div>`;
  }

  function renderTabs(activeTab) {
    return TABS.map(function (tab) {
      const id = tab[0];
      const label = tab[1];
      const authRefresh = id === 'tab-authentication' ? ';refreshSettingsAuthenticationView()' : '';
      const selected = id === activeTab;
      return `<button type="button" id="settings-tab-${html(id)}" class="stab${selected ? ' active' : ''}" data-tab="${id}" role="tab" aria-controls="${html(id)}" aria-selected="${selected ? 'true' : 'false'}" tabindex="${selected ? '0' : '-1'}" onclick="switchSettingsTab('${id}')${authRefresh}">${html(label)}</button>`;
    }).join('');
  }

  function switchSettingsTabOwned(id) {
    const activeTab = TABS.some(tab => tab[0] === id) ? id : 'tab-general';

    document.querySelectorAll('#settings-tabs .stab').forEach(function (tab) {
      const active = tab.dataset.tab === activeTab;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
      tab.tabIndex = active ? 0 : -1;
    });

    document.querySelectorAll('#settings-form .stab-panel').forEach(function (panel) {
      const active = panel.id === activeTab;
      panel.classList.toggle('active', active);
      panel.hidden = !active;
    });

    document.querySelectorAll('[data-settings-test-tab]').forEach(function (button) {
      button.hidden = button.dataset.settingsTestTab !== activeTab;
    });

    // Extraction password editing is still a functional helper from app.js.
    // It is the only inherited Settings-side behavior intentionally invoked by
    // tab activation; queue polling and database-list loading are not tab work.
    if (activeTab === 'tab-extract' && typeof initExtractionPasswordList === 'function') {
      initExtractionPasswordList();
    }

    return activeTab;
  }

  function activeTabBeforeRender() {
    try {
      const active = getActiveSettingsTab();
      if (TABS.some(tab => tab[0] === active)) return active;
    } catch (_) {}
    return 'tab-general';
  }

  function renderSettingsPage() {
    const view = document.getElementById('view-settings');
    if (!view) return;

    const activeTab = activeTabBeforeRender();
    const s = (typeof settingsData !== 'undefined' && settingsData) ? settingsData : {};

    view.classList.add('dp-settings-page');
    view.innerHTML = `
      <section class="card dp-settings-master" aria-label="Settings configuration">
        <div class="card-header dp-settings-master-header">
          <div class="dp-settings-master-identity">
            <div class="dp-settings-title-icon" aria-hidden="true">
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.38a2 2 0 0 0-.73-2.73l-.15-.09a2 2 0 0 1-1-1.74v-.51a2 2 0 0 1 1-1.72l.15-.1a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2Z"/><circle cx="12" cy="12" r="3"/></svg>
            </div>
            <div class="dp-settings-heading-copy">
              <div class="dp-settings-heading">Settings</div>
              <div class="dp-settings-subtitle">Configure providers, downloads, notifications, and system behavior.</div>
            </div>
          </div>
          <div class="stabs dp-settings-tabs" id="settings-tabs" role="tablist" aria-label="Settings sections">${renderTabs(activeTab)}</div>
          <div class="dp-settings-master-balance" aria-hidden="true"></div>
        </div>
        <div class="card-body dp-settings-master-body">
          <div id="settings-form">
            ${panelHtml('tab-general', sourcesPanel(s))}
            ${panelHtml('tab-download', downloadsPanel(s))}
            ${panelHtml('tab-extract', extractionPanel(s))}
            ${panelHtml('tab-notifications', notificationsPanel(s))}
            ${panelHtml('tab-authentication', authenticationPanel(s))}
            ${panelHtml('tab-database', dataMaintenancePanel(s))}
            ${panelHtml('tab-advanced', advancedPanel(s))}
          </div>
        </div>
      </section>
      <section class="card save-bar dp-settings-footer" aria-label="Settings actions">
        <span class="save-hint">Settings are applied immediately after saving.</span>
        <div class="settings-context-actions">
          <button class="btn btn-ghost" id="btn-test-alldebrid" data-settings-test-tab="tab-general" onclick="testAD(this)">🔑 Test AllDebrid</button>
          <button class="btn btn-ghost" id="btn-test-aria2" data-settings-test-tab="tab-download" onclick="testAria2(this)" hidden>⬇️ Test Aria2</button>
          <button class="btn btn-ghost" id="btn-test-discord" data-settings-test-tab="tab-notifications" onclick="testDiscord(this)" hidden>🔔 Test Discord</button>
        </div>
        <button class="btn btn-primary" id="btn-save-settings" onclick="saveSettings(this)">💾 Save Settings</button>
      </section>`;

    switchSettingsTabOwned(activeTab);
    if (activeTab === 'tab-authentication') void refreshAuthenticationView();

    const avatarUrl = textValue(s, 'discord_avatar_url', '');
    if (avatarUrl && !avatarUrl.includes('github') && !avatarUrl.includes('_DEFAULT')) {
      showAvatarPreview(avatarUrl, 'Custom avatar', 0);
    }
  }

  function value(id) {
    return String(document.getElementById(id)?.value || '').trim();
  }

  function number(id, fallback = 0) {
    const raw = document.getElementById(id)?.value;
    if (raw == null || raw === '') return fallback;
    const parsed = parseInt(raw, 10);
    return Number.isNaN(parsed) ? fallback : parsed;
  }

  function floatNumber(id, fallback = 0) {
    const raw = document.getElementById(id)?.value;
    if (raw == null || raw === '') return fallback;
    const parsed = parseFloat(raw);
    return Number.isNaN(parsed) ? fallback : parsed;
  }

  function bool(id, fallback = false) {
    const element = document.getElementById(id);
    return element ? !!element.checked : fallback;
  }

  function lines(id) {
    return value(id).split('\n').map(item => item.trim()).filter(Boolean);
  }

  function getSettingsFormData() {
    const current = (typeof settingsData !== 'undefined' && settingsData) ? settingsData : {};
    const mode = value('s-aria2_mode') || current.aria2_mode || 'builtin';
    const maxConcurrent = number('s-aria2_max_active_downloads', Number(current.max_concurrent_downloads ?? current.aria2_max_active_downloads ?? 3));
    const reportWindow = number('stats-report-hours', Number(current.stats_report_window_hours ?? 24));
    const clearSecrets = [
      'alldebrid_api_key',
      'aria2_secret',
      'discord_webhook_url',
      'discord_webhook_added',
      'stats_report_webhook_url',
      'extraction_password',
    ].filter(fieldName => document.getElementById(`s-clear-${fieldName}`)?.checked);

    const data = {
      ...current,
      clear_secrets: clearSecrets,
      alldebrid_api_key: value('s-alldebrid_api_key'),
      aria2_mode: mode,
      aria2_url: mode === 'builtin' ? (current.aria2_url || 'http://127.0.0.1:6800/jsonrpc') : value('s-aria2_url'),
      aria2_secret: mode === 'builtin' ? (current.aria2_secret || '') : value('s-aria2_secret'),
      download_folder: value('s-download_folder'),
      aria2_download_path: value('s-aria2_download_path'),
      aria2_max_active_downloads: maxConcurrent,
      max_concurrent_downloads: maxConcurrent,
      min_free_disk_gb: floatNumber('s-min_free_disk_gb', Number(current.min_free_disk_gb ?? 0)),
      disk_guard_resume_hysteresis_gb: floatNumber('s-disk_guard_resume_hysteresis_gb', Number(current.disk_guard_resume_hysteresis_gb ?? 0.5)),
      stuck_download_timeout_hours: number('s-stuck_download_timeout_hours', Number(current.stuck_download_timeout_hours ?? 6)),
      aria2_error_retry_count: number('s-aria2_error_retry_count', Number(current.aria2_error_retry_count ?? 3)),
      aria2_error_retry_delay_seconds: number('s-aria2_error_retry_delay_seconds', Number(current.aria2_error_retry_delay_seconds ?? 60)),
      filters_enabled: bool('s-filters_enabled', current.filters_enabled !== false),
      blocked_extensions: lines('s-blocked_extensions'),
      blocked_keywords: lines('s-blocked_keywords'),
      min_file_size_mb: number('s-min_file_size_mb', Number(current.min_file_size_mb ?? 0)),
      block_samples: bool('s-block_samples', !!current.block_samples),
      block_extras: bool('s-block_extras', !!current.block_extras),
      torrent_labels: value('s-torrent_labels_raw').split(',').map(item => item.trim()).filter(Boolean),
      extract_enabled: bool('s-extract_enabled', !!current.extract_enabled),
      extract_delete_archive: bool('s-extract_delete_archive', current.extract_delete_archive !== false),
      extract_max_concurrent: number('s-extract_max_concurrent', Number(current.extract_max_concurrent ?? 1)),
      extraction_password: value('s-extraction_password'),
      discord_username: value('s-discord_username') || 'DebridPulse',
      discord_avatar_url: value('s-discord_avatar_url'),
      discord_webhook_url: value('s-discord_webhook_url'),
      discord_webhook_added: value('s-discord_webhook_added'),
      discord_notify_added: bool('s-discord_notify_added', !!current.discord_notify_added),
      discord_notify_finished: bool('s-discord_notify_finished', !!current.discord_notify_finished),
      discord_notify_error: bool('s-discord_notify_error', !!current.discord_notify_error),
      discord_notify_extract: bool('s-discord_notify_extract', current.discord_notify_extract !== false),
      discord_notify_update: bool('s-discord_notify_update', current.discord_notify_update !== false),
      update_check_interval_hours: number('s-update_check_interval_hours', Number(current.update_check_interval_hours ?? 12)),
      stats_report_webhook_url: value('s-stats_report_webhook_url'),
      stats_report_interval_hours: number('s-stats_report_interval_hours', Number(current.stats_report_interval_hours ?? 0)),
      stats_report_window_hours: reportWindow,
      backup_enabled: bool('s-backup_enabled', current.backup_enabled !== false),
      backup_folder: value('s-backup_folder'),
      backup_interval_hours: number('s-backup_interval_hours', Number(current.backup_interval_hours ?? 24)),
      backup_keep_days: number('s-backup_keep_days', Number(current.backup_keep_days ?? 7)),
      stats_snapshot_interval_minutes: number('s-stats_snapshot_interval_minutes', Number(current.stats_snapshot_interval_minutes ?? 60)),
      stats_snapshot_keep_days: number('s-stats_snapshot_keep_days', Number(current.stats_snapshot_keep_days ?? 30)),
      events_keep_days: number('s-events_keep_days', Number(current.events_keep_days ?? 30)),
      db_wipe_enabled: bool('s-db_wipe_enabled', !!current.db_wipe_enabled),
      db_backup_before_wipe: bool('s-db_backup_before_wipe', current.db_backup_before_wipe !== false),
      aria2_split: number('s-aria2_split', Number(current.aria2_split ?? 16)),
      aria2_min_split_size: value('s-aria2_min_split_size') || current.aria2_min_split_size || '10M',
      aria2_max_connection_per_server: number('s-aria2_max_connection_per_server', Number(current.aria2_max_connection_per_server ?? 16)),
      aria2_disk_cache: value('s-aria2_disk_cache') || current.aria2_disk_cache || '64M',
      aria2_file_allocation: value('s-aria2_file_allocation') || current.aria2_file_allocation || 'falloc',
      aria2_lowest_speed_limit: value('s-aria2_lowest_speed_limit') || current.aria2_lowest_speed_limit || '0',
      aria2_continue_downloads: bool('s-aria2_continue_downloads', current.aria2_continue_downloads !== false),
      alldebrid_rate_limit_per_minute: number('s-alldebrid_rate_limit_per_minute', Number(current.alldebrid_rate_limit_per_minute ?? 60)),
      poll_interval_seconds: number('s-poll_interval_seconds', Number(current.poll_interval_seconds ?? 30)),
      full_sync_interval_minutes: number('s-full_sync_interval_minutes', Number(current.full_sync_interval_minutes ?? 5)),
      upload_fail_retry_count: number('s-upload_fail_retry_count', Number(current.upload_fail_retry_count ?? 3)),
      upload_fail_retry_delay_minutes: number('s-upload_fail_retry_delay_minutes', Number(current.upload_fail_retry_delay_minutes ?? 5)),
    };

    // Authentication is owned by /api/auth/config. Preserve that server state
    // during ordinary Settings saves rather than letting the broad /settings
    // serializer implicitly rewrite authentication fields.
    data.auth_password_enabled = !!current.auth_password_enabled;
    data.auth_username = String(current.auth_username || '');
    data.auth_password = '';
    data.auth_oidc_enabled = !!current.auth_oidc_enabled;
    data.oidc_provider_name = current.oidc_provider_name || 'OpenID Connect';
    data.oidc_issuer_url = current.oidc_issuer_url || '';
    data.oidc_client_id = current.oidc_client_id || '';
    data.oidc_scopes = Array.isArray(current.oidc_scopes) ? current.oidc_scopes : ['openid', 'profile', 'email'];
    data.oidc_allow_all = !!current.oidc_allow_all;
    data.oidc_allowed_subjects = current.oidc_allowed_subjects || [];
    data.oidc_allowed_emails = current.oidc_allowed_emails || [];
    data.oidc_allowed_groups = current.oidc_allowed_groups || [];
    data.oidc_group_claim = current.oidc_group_claim || 'groups';
    data.public_base_url = current.public_base_url || '';

    return data;
  }

  function installAuthoritativeSettingsPage() {
    if (window.renderSettings?.dpSettingsPage === '1') return;

    const render = function () {
      renderSettingsPage();
    };
    render.dpSettingsPage = '1';
    window.renderSettings = render;

    const serialize = function () {
      return getSettingsFormData();
    };
    serialize.dpSettingsPage = '1';
    window.getFormSettings = serialize;

    switchSettingsTabOwned.dpSettingsPage = '1';
    window.switchSettingsTab = switchSettingsTabOwned;
    window.refreshSettingsAuthenticationView = refreshAuthenticationView;

    if (typeof settingsData !== 'undefined' && settingsData && document.getElementById('view-settings')?.classList.contains('active')) {
      renderSettingsPage();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', installAuthoritativeSettingsPage, {once: true});
  } else {
    installAuthoritativeSettingsPage();
  }
})();