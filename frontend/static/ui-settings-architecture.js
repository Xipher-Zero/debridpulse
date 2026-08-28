/* DebridPulse v1.0.11 Settings information-architecture pass.
 *
 * UI only: reorganizes the existing Settings controls after the inherited
 * renderer (and the dedicated Authentication augmentation) have rendered.
 * Existing element IDs remain authoritative so the current serializer/API
 * behavior is preserved. Controls intentionally removed from normal UI are
 * retained in a hidden preservation container until backend pruning occurs.
 *
 * Lifecycle contract: app.js owns renderSettings. This runtime installs one
 * explicit post-render hook; it never infers Settings completion by observing
 * DOM mutations on the form it rearranges.
 */
(function () {
  'use strict';

  const TAB_LABELS = Object.freeze({
    'tab-general': 'Sources & Providers',
    'tab-download': 'Downloads',
    'tab-extract': 'Extraction',
    'tab-notifications': 'Notifications',
    'tab-authentication': 'Authentication',
    'tab-database': 'Data & Maintenance',
    'tab-advanced': 'Advanced',
  });

  const TAB_ORDER = Object.freeze([
    'tab-general',
    'tab-download',
    'tab-extract',
    'tab-notifications',
    'tab-authentication',
    'tab-database',
    'tab-advanced',
  ]);

  function installStyles() {
    if (document.getElementById('dp-settings-architecture-style')) return;
    const style = document.createElement('style');
    style.id = 'dp-settings-architecture-style';
    style.textContent = `
      #settings-tabs { flex-wrap: wrap; }
      #settings-tabs .stab { white-space: nowrap; }
      #settings-form .dp-settings-preserved { display: none !important; }
      #settings-form .dp-settings-ia-card .scard-body > :first-child { margin-top: 0; }
      #settings-form .dp-settings-ia-card .scard-body > :last-child { margin-bottom: 0; }
      #settings-form .dp-settings-section-copy {
        margin: 0;
        padding: 4px 14px 8px;
        color: var(--text3);
        font-size: 11px;
        line-height: 1.5;
      }
      #settings-form .dp-settings-status {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 8px;
        margin: 0 0 12px;
      }
      #settings-form .dp-settings-status > div {
        padding: 9px 10px;
        border: 1px solid var(--border);
        border-radius: 7px;
        background: var(--surface2);
        color: var(--text2);
        font-size: 11px;
        line-height: 1.45;
      }
      #settings-form .dp-settings-status b { color: var(--text1); }
      #settings-form .dp-settings-context-clear {
        margin-top: 8px;
        padding-top: 8px;
        border-top: 1px solid var(--border);
      }
      #settings-form .dp-settings-inline-actions {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-top: 8px;
      }
    `;
    document.head.appendChild(style);
  }

  function panel(id) {
    return document.getElementById(id);
  }

  function currentSetting(name, fallback) {
    try {
      if (typeof settingsData !== 'undefined' && settingsData && settingsData[name] != null) {
        return settingsData[name];
      }
    } catch (_) {
      /* app.js may not have established its global lexical settings state yet. */
    }
    return fallback;
  }

  function control(id) {
    return document.getElementById(id);
  }

  function unitFor(id) {
    const el = control(id);
    if (!el) return null;
    return el.closest('.form-group, .toggle-row') || el;
  }

  function preservationContainer() {
    const form = document.getElementById('settings-form');
    if (!form) return null;
    let hidden = document.getElementById('dp-settings-preserved-controls');
    if (!hidden) {
      hidden = document.createElement('div');
      hidden.id = 'dp-settings-preserved-controls';
      hidden.className = 'dp-settings-preserved';
      hidden.hidden = true;
      hidden.setAttribute('aria-hidden', 'true');
      form.appendChild(hidden);
    }
    return hidden;
  }

  function createCard(targetPanel, key, title, copy) {
    if (!targetPanel) return null;
    let card = targetPanel.querySelector(`[data-dp-settings-card="${key}"]`);
    if (card) return card.querySelector('.scard-body');

    card = document.createElement('div');
    card.className = 'scard dp-settings-ia-card';
    card.dataset.dpSettingsCard = key;

    const header = document.createElement('div');
    header.className = 'scard-header';
    header.textContent = title;
    card.appendChild(header);

    if (copy) {
      const hint = document.createElement('p');
      hint.className = 'dp-settings-section-copy';
      hint.textContent = copy;
      card.appendChild(hint);
    }

    const body = document.createElement('div');
    body.className = 'scard-body';
    card.appendChild(body);
    targetPanel.appendChild(card);
    return body;
  }

  function moveUnit(id, destination) {
    const unit = unitFor(id);
    if (!unit || !destination) return null;
    destination.appendChild(unit);
    return unit;
  }

  function moveNode(node, destination) {
    if (!node || !destination) return null;
    destination.appendChild(node);
    return node;
  }

  function setLabel(id, text) {
    const el = control(id);
    const label = el && (el.closest('.form-group, .toggle-row') || el.parentElement)?.querySelector('.form-label, .tl');
    if (label) label.textContent = text;
  }

  function contextualClear(field, destination, label) {
    const checkbox = control(`s-clear-${field}`);
    if (!checkbox || !destination) return;
    const row = checkbox.closest('label.toggle-row') || checkbox.closest('.toggle-row') || checkbox.parentElement;
    if (!row) return;
    row.classList.add('dp-settings-context-clear');
    const title = row.querySelector('.tl');
    const sub = row.querySelector('.ts');
    if (title) title.textContent = label;
    if (sub) sub.textContent = 'Erase the stored value when Settings are saved.';
    destination.appendChild(row);
  }

  function moveAction(selector, destination, newLabel) {
    const button = document.querySelector(selector);
    if (!button || !destination) return null;
    if (newLabel) {
      button.textContent = newLabel;
      button.dataset.defaultLabel = newLabel;
    }
    let actions = destination.querySelector('.dp-settings-inline-actions');
    if (!actions) {
      actions = document.createElement('div');
      actions.className = 'dp-settings-inline-actions';
      destination.appendChild(actions);
    }
    actions.appendChild(button);
    return button;
  }

  function hideOriginalCards(targetPanel) {
    if (!targetPanel) return;
    const hidden = preservationContainer();
    if (!hidden) return;
    Array.from(targetPanel.children).forEach(child => {
      if (child.classList?.contains('scard') && !child.dataset.dpSettingsCard) {
        hidden.appendChild(child);
      }
    });
  }

  function preserveLooseUnits(ids) {
    const hidden = preservationContainer();
    if (!hidden) return;
    ids.forEach(id => {
      const unit = unitFor(id);
      if (unit && !hidden.contains(unit)) hidden.appendChild(unit);
    });
  }

  function normalizeTabs() {
    const tabs = document.getElementById('settings-tabs');
    const form = document.getElementById('settings-form');
    if (!tabs || !form) return;

    TAB_ORDER.forEach(id => {
      const tab = tabs.querySelector(`.stab[data-tab="${id}"]`);
      if (tab) {
        tab.textContent = TAB_LABELS[id];
        tabs.appendChild(tab);
      }
      const tabPanel = panel(id);
      if (tabPanel) form.insertBefore(tabPanel, preservationContainer());
    });

    const allDebridFooterTest = document.getElementById('btn-test-alldebrid');
    if (allDebridFooterTest) allDebridFooterTest.dataset.settingsTestTab = 'tab-general';
    const aria2FooterTest = document.getElementById('btn-test-aria2');
    if (aria2FooterTest) aria2FooterTest.dataset.settingsTestTab = 'tab-download';
    const discordFooterTest = document.getElementById('btn-test-discord');
    if (discordFooterTest) discordFooterTest.dataset.settingsTestTab = 'tab-notifications';
  }

  function buildSourcesAndProviders() {
    const target = panel('tab-general');
    if (!target) return;

    const connection = createCard(
      target,
      'provider-alldebrid-connection',
      'AllDebrid — Connection',
      'AllDebrid is the current V1 acquisition provider. Credentials and provider-specific behavior live here.'
    );

    const apiKeyUnit = moveUnit('s-alldebrid_api_key', connection);
    if (apiKeyUnit) {
      setLabel('s-alldebrid_api_key', 'AllDebrid API Key');
      const input = control('s-alldebrid_api_key');
      if (input && currentSetting('alldebrid_api_key_configured', false)) {
        input.placeholder = 'Stored API key configured — blank keeps the current key';
      }
    }

    let status = connection?.querySelector('.dp-settings-status');
    if (!status && connection) {
      status = document.createElement('div');
      status.className = 'dp-settings-status';
      status.innerHTML = `
        <div><b>Stored API key</b><br><span id="dp-settings-ad-key-state"></span></div>
        <div><b>Provider status</b><br><span id="dp-settings-ad-runtime-state">Checking current connection…</span></div>`;
      connection.insertBefore(status, connection.firstChild);
    }
    const keyState = status?.querySelector('#dp-settings-ad-key-state');
    if (keyState) {
      keyState.textContent = currentSetting('alldebrid_api_key_configured', false) ? 'Configured' : 'Not configured';
    }
    contextualClear('alldebrid_api_key', connection, 'Clear Stored API Key');

    const advanced = createCard(
      target,
      'provider-alldebrid-advanced',
      'AllDebrid — Advanced Provider Behavior',
      'Provider-specific rate, polling, reconciliation, and submission recovery controls. These remain grouped here while later pruning determines which should stay operator-facing.'
    );
    moveUnit('s-alldebrid_rate_limit_per_minute', advanced);
    moveUnit('s-poll_interval_seconds', advanced);
    moveUnit('s-full_sync_interval_minutes', advanced);
    moveUnit('s-upload_fail_retry_count', advanced);
    moveUnit('s-upload_fail_retry_delay_minutes', advanced);

    hideOriginalCards(target);
  }

  function buildDownloads() {
    const target = panel('tab-download');
    if (!target) return;

    const engine = createCard(
      target,
      'download-engine',
      'Download Engine',
      'Choose whether DebridPulse manages its built-in aria2 instance or connects to an external aria2 service.'
    );
    moveUnit('s-aria2_mode', engine);

    const external = createCard(
      target,
      'external-aria2',
      'External aria2',
      'Connection settings used only when External aria2 is selected.'
    );
    moveUnit('s-aria2_url', external);
    moveUnit('s-aria2_secret', external);
    contextualClear('aria2_secret', external, 'Clear Stored aria2 RPC Secret');

    const paths = createCard(
      target,
      'download-paths',
      'Paths',
      'Download locations as seen by the DebridPulse-managed engine and, when used, the external aria2 daemon.'
    );
    moveUnit('s-download_folder', paths);
    moveUnit('s-aria2_download_path', paths);

    const concurrency = createCard(
      target,
      'download-concurrency',
      'Concurrency',
      'Controls how many downloads DebridPulse allows to be active at once.'
    );
    moveUnit('s-aria2_max_active_downloads', concurrency);
    setLabel('s-aria2_max_active_downloads', 'Maximum Concurrent Downloads');

    const storage = createCard(
      target,
      'storage-protection',
      'Storage Protection',
      'Protect the destination from exhausting free space while allowing already-active transfers to finish.'
    );
    moveUnit('s-min_free_disk_gb', storage);
    moveUnit('s-disk_guard_resume_hysteresis_gb', storage);

    const recovery = createCard(
      target,
      'download-recovery',
      'Recovery',
      'Provider-neutral stalled-download recovery plus delivery-engine retry policy.'
    );
    moveUnit('s-stuck_download_timeout_hours', recovery);
    moveUnit('s-aria2_error_retry_count', recovery);
    moveUnit('s-aria2_error_retry_delay_seconds', recovery);

    const selection = createCard(
      target,
      'file-selection',
      'File Selection',
      'Choose which files DebridPulse should materialize after content has been acquired.'
    );
    moveUnit('s-filters_enabled', selection);
    moveNode(control('filter-fields'), selection);

    const organization = createCard(
      target,
      'download-organization',
      'Organization',
      'Local labels used to organize downloads inside DebridPulse.'
    );
    moveUnit('s-torrent_labels_raw', organization);

    hideOriginalCards(target);
  }

  function buildExtraction() {
    const target = panel('tab-extract');
    if (!target) return;

    const behavior = createCard(
      target,
      'extraction-behavior',
      'Auto-Extraction',
      'Automatic archive extraction behavior after a download completes.'
    );
    moveUnit('s-extract_enabled', behavior);
    moveUnit('s-extract_delete_archive', behavior);
    moveUnit('s-extract_max_concurrent', behavior);

    const passwords = createCard(
      target,
      'extraction-passwords',
      'Archive Passwords',
      'Passwords are tried in order for supported encrypted archives.'
    );
    moveUnit('s-extraction_password', passwords);
    contextualClear('extraction_password', passwords, 'Clear Stored Archive Passwords');

    hideOriginalCards(target);
  }

  function buildNotifications() {
    const target = panel('tab-notifications');
    if (!target) return;

    const discord = createCard(
      target,
      'notifications-discord',
      'Discord',
      'Sender identity and webhook destinations for Discord notifications.'
    );
    moveUnit('s-discord_username', discord);
    moveUnit('s-discord_avatar_url', discord);
    moveUnit('s-discord_webhook_url', discord);
    moveUnit('s-discord_webhook_added', discord);
    contextualClear('discord_webhook_url', discord, 'Clear Stored Main Webhook');
    contextualClear('discord_webhook_added', discord, 'Clear Stored Added-Event Webhook');

    const events = createCard(
      target,
      'notifications-events',
      'Event Notifications',
      'Choose which DebridPulse events produce notifications.'
    );
    moveUnit('s-discord_notify_added', events);
    moveUnit('s-discord_notify_finished', events);
    moveUnit('s-discord_notify_error', events);
    moveUnit('s-discord_notify_extract', events);

    const updates = createCard(
      target,
      'notifications-updates',
      'Update Notification',
      'Control new-version notifications and how often DebridPulse checks for a release.'
    );
    moveUnit('s-discord_notify_update', updates);
    moveUnit('s-update_check_interval_hours', updates);

    const reports = createCard(
      target,
      'notifications-reports',
      'Scheduled Statistics Reports',
      'Configure automatic statistics delivery. Interactive statistics browsing stays on the Statistics page.'
    );
    moveUnit('s-stats_report_webhook_url', reports);
    moveUnit('s-stats_report_interval_hours', reports);
    const reportWindow = unitFor('stats-report-hours');
    if (reportWindow) {
      const label = reportWindow.querySelector('.form-label');
      if (label) label.textContent = 'Report Window';
      reports.appendChild(reportWindow);
    }
    contextualClear('stats_report_webhook_url', reports, 'Clear Stored Reporting Webhook');
    moveAction('button[onclick^="sendStatsReport"]', reports, 'Send Test Report');

    hideOriginalCards(target);
  }

  function buildDataMaintenance() {
    const target = panel('tab-database');
    if (!target) return;

    const backups = createCard(
      target,
      'data-backups',
      'DebridPulse Backups',
      'Automatic application backups covering configuration, database state, and managed data.'
    );
    moveUnit('s-backup_enabled', backups);
    moveUnit('s-backup_folder', backups);
    moveUnit('s-backup_interval_hours', backups);
    moveUnit('s-backup_keep_days', backups);
    moveAction('button[onclick^="triggerBackup"]', backups, 'Run Backup Now');
    moveAction('button[onclick^="loadBackupList"]', backups, 'List Backups');
    moveNode(control('backup-list'), backups);

    const retention = createCard(
      target,
      'data-retention',
      'Data Retention',
      'Controls how long DebridPulse retains statistics snapshots and Event Log history.'
    );
    moveUnit('s-stats_snapshot_interval_minutes', retention);
    moveUnit('s-stats_snapshot_keep_days', retention);
    moveUnit('s-events_keep_days', retention);

    const danger = createCard(
      target,
      'danger-zone',
      'Danger Zone',
      'Destructive database maintenance. These controls intentionally require an explicit safety gate.'
    );
    moveUnit('s-db_wipe_enabled', danger);
    moveUnit('s-db_backup_before_wipe', danger);
    moveAction('button[onclick^="wipeDatabase"]', danger, 'Wipe Database');

    hideOriginalCards(target);
  }

  function buildAdvanced() {
    const target = panel('tab-advanced');
    if (!target) return;

    const tuning = createCard(
      target,
      'transfer-engine-tuning',
      'Transfer Engine Tuning',
      'Low-level aria2 transfer behavior that may be useful for operators with unusual storage or network constraints.'
    );
    moveUnit('s-aria2_split', tuning);
    moveUnit('s-aria2_min_split_size', tuning);
    moveUnit('s-aria2_max_connection_per_server', tuning);
    moveUnit('s-aria2_disk_cache', tuning);
    moveUnit('s-aria2_file_allocation', tuning);
    moveUnit('s-aria2_lowest_speed_limit', tuning);
    moveUnit('s-aria2_continue_downloads', tuning);

    hideOriginalCards(target);
  }

  function preserveInternalAndOperationalControls() {
    preserveLooseUnits([
      's-alldebrid_agent',
      's-download_client',
      's-aria2_builtin_auto_start',
      's-aria2_builtin_port',
      's-aria2_builtin_log_max_mb',
      's-aria2_builtin_log_backups',
      's-disk_guard_interval_seconds',
      's-aria2_operation_timeout_seconds',
      's-aria2_deep_sync_interval_minutes',
      's-aria2_poll_interval_seconds',
      's-aria2_purge_interval_minutes',
      's-aria2_max_download_result',
      's-aria2_waiting_window',
      's-aria2_stopped_window',
      's-aria2_keep_unfinished_download_result',
      's-aria2_max_upload_limit',
      's-aria2_start_paused',
      's-stats_report_window_hours',
      's-db_backup_folder',
      's-db_backup_enabled',
      's-db_backup_keep_days',
    ]);
  }

  function syncProviderStatus() {
    const runtime = document.getElementById('dp-settings-ad-runtime-state');
    if (!runtime) return;
    const label = document.getElementById('lbl-api')?.textContent?.trim();
    const premium = document.getElementById('lbl-premium')?.textContent?.trim();
    runtime.textContent = [label, premium].filter(Boolean).join(' · ') || 'Status unavailable';
  }

  function observeProviderStatus() {
    ['lbl-api', 'lbl-premium'].forEach(id => {
      const el = document.getElementById(id);
      if (!el || el.dataset.dpSettingsStatusObserved === '1') return;
      el.dataset.dpSettingsStatusObserved = '1';
      new MutationObserver(syncProviderStatus).observe(el, {childList: true, characterData: true, subtree: true});
    });
  }

  function applyArchitecture() {
    const form = document.getElementById('settings-form');
    const tabs = document.getElementById('settings-tabs');
    if (!form || !tabs || !panel('tab-general')) return false;

    installStyles();
    preservationContainer();
    normalizeTabs();
    buildSourcesAndProviders();
    buildDownloads();
    buildExtraction();
    buildNotifications();
    buildDataMaintenance();
    buildAdvanced();
    preserveInternalAndOperationalControls();
    normalizeTabs();
    syncProviderStatus();
    observeProviderStatus();
    form.dataset.dpSettingsArchitecture = '1';
    return true;
  }

  function installSettingsRenderHook() {
    const previous = window.renderSettings;
    if (typeof previous !== 'function') {
      console.error('[DebridPulse] Settings architecture not installed: renderSettings is unavailable.');
      return false;
    }
    if (previous.dpSettingsArchitecture === '1') return true;

    const wrapped = function () {
      const result = previous.apply(this, arguments);
      applyArchitecture();
      return result;
    };
    wrapped.dpSettingsArchitecture = '1';
    window.renderSettings = wrapped;
    return true;
  }

  function initialize() {
    installStyles();
    installSettingsRenderHook();
    /* Settings may already have been rendered before the post-core presentation
       loader reaches this runtime. Normalize that generation exactly once now;
       future generations flow through the explicit renderSettings hook. */
    applyArchitecture();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, {once: true});
  } else {
    initialize();
  }
})();
