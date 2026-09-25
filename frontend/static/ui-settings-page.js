/* DebridPulse v1.0.11 clean-room Settings page.
 *
 * This runtime owns the Settings shell, forms, serialization, authentication
 * presentation/resilience, OIDC verification, and Settings interaction lifecycle.
 * Backend APIs are the page contract; no legacy Settings renderer is involved.
 */
(function () {
  'use strict';

  const TABS = Object.freeze([
    // Presentation only: the internal tab key stays `sources`.
    ['sources', 'Services', 'zap'],
    ['downloads', 'Downloads', 'download'],
    ['extraction', 'Extraction', 'package-open'],
    ['authentication', 'Authentication', 'shield-check'],
    ['notifications', 'Notifications', 'bell'],
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

  // The Settings document is rendered from, and saved to, the canonical
  // namespaces: integrations.<id>.options, transfer_policy. Form controls keep
  // their local names (data-setting="aria2_split", ...) but those names never
  // imply a flat settings field.
  const aria2Of = s => s?.integrations?.aria2?.options || {};
  const allDebridOf = s => s?.integrations?.alldebrid?.options || {};
  const policyOf = s => s?.transfer_policy || {};
  const usenetOf = s => s?.integrations?.usenet?.options || {};
  // A form control whose stored value is an integration-owned secret is
  // written, and cleared, through that integration's own scoped surface.
  // ``converge`` is the row that has to change because the ACCEPTED state
  // changed what it shows -- whether there is a stored value to clear at all.
  const INTEGRATION_SECRET_CONTROLS = Object.freeze({
    alldebrid_api_key: {integration: 'alldebrid', option: 'api_key',
                        converge: dispatched => renderAllDebridCredential(dispatched)},
  });

  /* Every ordinary Settings control this page commits at a FIELD boundary.
   *
   * One declaration of which canonical namespace owns each ordinary control,
   * which option inside it the control is, and -- where it is not the ordinary
   * changed-blur -- which commit class it belongs to.
   * ui-settings-persistence.js owns every generic behaviour (baseline, dirty
   * comparison, scope dispatch, serialization, stale-response protection,
   * convergence, rollback) for BOTH classes; this table is the page DECLARING
   * what a control commits to, never a second implementation of the machinery.
   *
   * ``commit: 'immediate'`` is for an ordinary, reversible boolean whose only
   * draft state is the state it already shows: flipping it IS the edit, so
   * waiting for a blur would only delay it. It is still an ordinary VALUE of
   * its namespace and goes through the same scope -- it is emphatically not
   * participation, which is a different question about a different subject and
   * keeps its own operational owner (providerEnableChanged).
   *
   * Destructive confirmations and participation toggles are deliberately
   * absent: erasing a credential is an explicit confirmed action and a
   * participation toggle is immediate because of what they ARE, not because of
   * the page they appear on. A control absent from this table keeps the page's
   * existing deferred (Apply Settings) semantics until its own page is
   * migrated.
   *
   * A credential is NOT absent. Entering or replacing one is an ordinary value
   * change: it commits on changed blur, through the same scoped mutation every
   * other control of that namespace uses. What makes it a secret is what the
   * SCOPE does with the accepted value -- see registerCommitScopes(). */
  // Every integration namespace a declared control can belong to. Each one is
  // written by the SAME generic scope; nothing about them differs here.
  const INTEGRATION_SCOPES = Object.freeze(['alldebrid', 'aria2', 'usenet']);

  const COMMIT_FIELDS = Object.freeze({
    // Services
    alldebrid_api_key: {scope: 'integration:alldebrid', option: 'api_key'},
    alldebrid_rate_limit_per_minute: {scope: 'integration:alldebrid', option: 'rate_limit_per_minute'},
    poll_interval_seconds: {scope: 'transfer-policy', option: 'provider_poll_interval_seconds'},
    upload_fail_retry_count: {scope: 'transfer-policy', option: 'resolution_retry_count'},
    upload_fail_retry_delay_minutes: {scope: 'transfer-policy', option: 'resolution_retry_delay_minutes'},
    full_sync_interval_minutes: {scope: 'settings-document', option: 'full_sync_interval_minutes'},

    // Downloads -> Network Sources tuning
    aria2_max_connection_per_server: {scope: 'integration:aria2', option: 'max_connection_per_server'},
    aria2_split: {scope: 'integration:aria2', option: 'split'},
    aria2_min_split_size: {scope: 'integration:aria2', option: 'min_split_size'},
    aria2_continue_downloads: {scope: 'integration:aria2', option: 'continue_downloads', commit: 'immediate'},
    aria2_lowest_speed_limit: {scope: 'integration:aria2', option: 'lowest_speed_limit'},
    aria2_disk_cache: {scope: 'integration:aria2', option: 'disk_cache'},
    aria2_file_allocation: {scope: 'integration:aria2', option: 'file_allocation'},

    // Downloads -> Usenet tuning
    usenet_article_cache_megabytes: {scope: 'integration:usenet', option: 'article_cache_megabytes'},
    usenet_max_acquisition_retries: {scope: 'integration:usenet', option: 'max_acquisition_retries'},
    usenet_operation_timeout_seconds: {scope: 'integration:usenet', option: 'operation_timeout_seconds'},
    usenet_direct_write: {scope: 'integration:usenet', option: 'direct_write', commit: 'immediate'},

    // Downloads -> global admission and safety/recovery policy
    aria2_max_active_downloads: {scope: 'transfer-policy', option: 'max_concurrent_executions'},
    aria2_error_retry_count: {scope: 'transfer-policy', option: 'execution_retry_count'},
    aria2_error_retry_delay_seconds: {scope: 'transfer-policy', option: 'execution_retry_delay_seconds'},
    stuck_download_timeout_hours: {scope: 'transfer-policy', option: 'stalled_timeout_hours'},

    // Downloads -> settings-document values
    download_folder: {scope: 'settings-document', option: 'download_folder'},
    min_free_disk_gb: {scope: 'settings-document', option: 'min_free_disk_gb'},
    disk_guard_resume_hysteresis_gb: {scope: 'settings-document', option: 'disk_guard_resume_hysteresis_gb'},

    // Extraction -- ordinary settings-document values, on exactly the same two
    // boundaries every other Settings control uses. ``redacted`` says only that
    // the canonical ECHO of this option is blank by design (the whole-settings
    // projection redacts it), so the echo cannot describe what was accepted; it
    // is not a different commit class and not a different write.
    extract_enabled: {scope: 'settings-document', option: 'extract_enabled', commit: 'immediate'},
    extract_delete_archive: {scope: 'settings-document', option: 'extract_delete_archive', commit: 'immediate'},
    extract_max_concurrent: {scope: 'settings-document', option: 'extract_max_concurrent'},
    extraction_password: {scope: 'settings-document', option: 'extraction_password', redacted: true},
  });

  /* The commit attributes a declared control carries, or nothing at all for a
   * control this page has not migrated. One place decides it, so every kind of
   * control -- input, select, toggle, directory field -- declares its class the
   * same way and none of them re-derives it. */
  function commitAttributes(key) {
    const declared = COMMIT_FIELDS[key];
    if (!declared) return '';
    return `data-commit="${html(declared.commit || 'changed-blur')}" `
      + `data-commit-key="${html(key)}" data-commit-scope="${html(declared.scope)}"`;
  }

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

  // An accepted confirmation whose operation re-renders Settings can remove or
  // disable the control that started it. Each such flow names its own surviving
  // successor (first enabled match wins) instead of leaving focus on <body>.
  function focusSurvivor(...selectors) {
    for (const selector of selectors) {
      const target = root()?.querySelector(selector);
      if (target && !target.disabled && target.getClientRects().length) {
        target.focus();
        return true;
      }
    }
    return false;
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

  /* The ONE Settings synchronisation owner.
   *
   * There are two references to the Settings document -- this page's
   * ``state.settings`` and the application-wide ``settingsData`` that the
   * provider-status renderer reads -- and exactly one function that moves
   * either of them. An accepted scoped mutation that updated only the first
   * left the status panel serving pre-mutation state until an unrelated Apply
   * Settings happened to perform a fresh GET, which is why every acceptance
   * helper publishes through here rather than assigning its own copy. */
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
    // The control declares its commit class; the canonical persistence owner
    // supplies the behaviour (COMMIT_FIELDS).
    const attrs = [
      commitAttributes(key),
      options.min != null ? `min="${html(options.min)}"` : '',
      options.max != null ? `max="${html(options.max)}"` : '',
      options.step != null ? `step="${html(options.step)}"` : '',
      options.placeholder ? `placeholder="${html(options.placeholder)}"` : '',
      options.readonly ? 'readonly' : '',
      options.autocomplete ? `autocomplete="${html(options.autocomplete)}"` : '',
    ].filter(Boolean).join(' ');
    const control =
      `<input class="input" id="${id}" data-setting="${html(key)}" type="${html(type)}" value="${html(value)}" ${attrs}>`;
    // The SAME control, in whichever of the two Settings field grammars the
    // caller asked for. Nothing about the control -- its bounds, its commit
    // class, its identity -- differs between them.
    if (options.inline) {
      return inlineField(id, label, options.hint, control + (options.after || ''),
        {className: options.className, action: options.action});
    }
    return `
      <div class="dp-settings-field">
        <label class="form-label" for="${id}">${html(label)}</label>
        ${control}
        ${options.hint ? `<span class="form-hint">${options.hint}</span>` : ''}${options.after || ''}
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

  // The archive-password field is rendered complete: the form-field textarea that
  // carries the persisted value (hidden from assistive tech and the tab order), the
  // editor container the ui-settings-archive-passwords owner fills, its reveal button
  // and the hint. The owner adds behavior only.
  function archivePasswordField(configured) {
    const id = fieldId('extraction_password');
    const placeholder = configured ? 'Stored password list configured — blank keeps it' : 'Optional archive passwords';
    return `
      <div class="dp-settings-field dp-settings-extraction-password-field">
        <label class="form-label" for="${id}">Archive Passwords (one per line)</label>
        <textarea class="input dp-settings-extraction-password-source" id="${id}" data-setting="extraction_password" rows="4" placeholder="${html(placeholder)}" aria-hidden="true" tabindex="-1" ${commitAttributes('extraction_password')}></textarea>
        <div class="input dp-settings-extraction-password-editor" role="group" aria-label="Archive passwords"><div class="dp-settings-password-rows"></div><div class="dp-settings-password-footer"><p class="form-hint dp-settings-password-guidance" aria-hidden="true">One password per line. Passwords are saved as you finish editing. Use Show all to reveal them, and Clear Passwords to erase the stored list.</p><div class="dp-settings-password-actions"><button type="button" class="btn btn-danger btn-sm dp-settings-password-clear" data-action="clear-archive-passwords" aria-label="Clear the stored archive passwords">Clear Passwords</button><button type="button" class="dp-settings-password-eye"></button></div></div></div>
      </div>`;
  }

  function toggle(key, label, detail, value, extraClass = '') {
    const id = fieldId(key);
    return `
      <label class="toggle-row dp-settings-toggle${extraClass ? ` ${extraClass}` : ''}" for="${id}">
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
        <select class="input" id="${id}" data-setting="${html(key)}" ${commitAttributes(key)}>
          ${choices.map(([v, labelText]) => `<option value="${html(v)}" ${selected(value, v)}>${html(labelText)}</option>`).join('')}
        </select>
        ${hint ? `<span class="form-hint">${hint}</span>` : ''}
      </div>`;
  }

  /* The ONE compact inline Settings field.
   *
   *   [ stacked title + hint ]   [ control ]   [ optional action ]
   *
   * The title and its hint are ONE informational block, and the control is
   * centred against that block rather than stacked beneath it -- which is what
   * removes the extra help/status band each of these rows used to carry. Every
   * surface that reads this way renders THIS, so the grammar is declared once
   * and no caller states a geometry of its own.
   *
   * What a caller does own is how much room its control deserves: a path, a
   * credential and a job count are not the same control, and nothing here
   * forces them to a common width. */
  function inlineField(id, label, hint, control, {className = '', action = ''} = {}) {
    return `
      <div class="dp-settings-inline-field${className ? ` ${className}` : ''}">
        <div class="dp-settings-inline-field-info">
          <label class="form-label" for="${id}">${html(label)}</label>
          ${hint ? `<span class="form-hint">${hint}</span>` : ''}
        </div>
        <div class="dp-settings-inline-field-control">${control}</div>${
          action ? `<div class="dp-settings-inline-field-action">${action}</div>` : ''}
      </div>`;
  }

  const CONFIGURED_SECRET_MASK = '•'.repeat(48);

  // Inner-card title icons, keyed by the card's title.
  const CARD_ICONS = Object.freeze({
    'Download Location & Limits': ['downloads', '/icons/dp/settings/download-engine.svg?v=1'],
    'Disk Space & Recovery': ['downloads', '/icons/dp/settings/download-safety-recovery.svg?v=1'],
    'Download Engine Activity': ['downloads', '/icons/dp/settings/download-engine-state.svg?v=1'],
    'Automatic Extraction': ['extraction', '/icons/dp/settings/automatic-extraction.svg?v=1'],
    'Authentication Status': ['authentication', '/icons/dp/settings/authentication-status.svg?v=1'],
    'Username & Password': ['authentication', '/icons/dp/settings/username-password.svg?v=1'],
    'OpenID Connect': ['authentication', '/icons/dp/settings/openid-connect.svg?v=1'],
    'API Access': ['authentication', '/icons/dp/settings/api-access.svg?v=1'],
    'Discord Notifications': ['notifications', '/icons/dp/settings/discord-notifications.svg?v=1'],
    'Statistics Reporting': ['notifications', '/icons/dp/settings/statistics-reporting.svg?v=1'],
    'Backups & Retention': ['maintenance', '/icons/dp/settings/backups-retention.svg?v=1'],
    'Database Reset Controls': ['maintenance', '/icons/dp/settings/database-reset-controls.svg?v=1'],
  });

  function card(title, body, options = {}) {
    const icon = CARD_ICONS[title];
    let titleClass = 'card-title';
    let titleAttrs = '';
    let titleMarkup = html(title);
    if (icon) {
      titleClass = 'card-title dp-settings-card-title--with-icon dp-settings-inner-card-title';
      titleAttrs = ` data-dp-settings-icon-section="${icon[0]}"`;
      titleMarkup = `<span class="dp-settings-inner-card-icon" aria-hidden="true" data-section="${icon[0]}"><img src="${icon[1]}" alt="" decoding="async"></span>${
        options.wrapTitle ? `<span class="dp-settings-card-title-text">${html(title)}</span>` : html(title)}`;
    } else if (options.titlePrefix) {
      titleClass = 'card-title dp-settings-card-title--with-icon';
      titleMarkup = `${options.titlePrefix}<span class="dp-settings-card-title-text">${html(title)}</span>`;
    }
    const centerClass = ['dp-settings-card-header-center', options.headerCenterClass].filter(Boolean).join(' ');
    return `
      <section class="card dp-settings-card dp-large-panel-surface ${options.className || ''}">
        <div class="card-header">
          <span class="${titleClass}"${titleAttrs}>${titleMarkup}</span>
          ${options.headerCenter ? `<div class="${centerClass}">${options.headerCenter}</div>` : ''}
          ${options.action || ''}
        </div>
        <div class="card-body">${body}</div>
      </section>`;
  }
  /* A group card. ``collapsible`` is the smallest generic opt-in there can be:
   * it renders the SAME canonical disclosure chip in the same place a provider
   * card does, closed, addressing the body through ``aria-controls`` -- so the
   * one disclosure behaviour already bound in bindEvents() serves it without
   * knowing it is a group. No second component, no duplicated markup, and no
   * group becomes collapsible without asking. */
  function groupCard(title, body, options = {}) {
    const titleMarkup = options.titlePrefix
      ? `<span class="card-title dp-settings-card-title--with-icon">${options.titlePrefix}<span class="dp-settings-card-title-text">${html(title)}</span></span>`
      : `<span class="card-title">${html(title)}</span>`;
    const safe = String(options.groupId || title).replace(/[^a-z0-9_-]/gi, '-');
    const bodyId = `dp-settings-group-body-${safe}`;
    // A group may declare what it should look like on FIRST render; after that
    // the operator's own choice is what it looks like.
    const persistKey = options.collapsible ? `group:${safe}` : '';
    const expanded = options.collapsible && disclosureOpen(persistKey, options.expanded);
    const disclosure = options.collapsible
      ? settingsDisclosure(bodyId, expanded, `${title} sources`, persistKey) : '';
    const header = options.action || disclosure;
    return `
      <section class="card dp-settings-group-card dp-large-panel-surface ${options.className || ''}"${
        options.groupId ? ` data-integration-group="${html(options.groupId)}"` : ''}>
        <div class="card-header${header ? ' dp-settings-card-header' : ''}">
          ${header ? `<span class="dp-settings-card-title-group">${titleMarkup}${disclosure}</span>` : titleMarkup}
          ${header ? `<div class="dp-settings-card-header-center"></div>
          <div class="dp-settings-card-header-controls">${options.action || ''}</div>` : ''}
        </div>
        <div class="card-body dp-settings-group-body"${options.collapsible ? ` id="${bodyId}"${expanded ? '' : ' hidden'}` : ''}>${body}</div>
      </section>`;
  }

  function secretField(key, label, configured, placeholder, hint, options = {}) {
    const clear = configured ? `
        <label class="dp-settings-clear-secret${options.clearClass ? ` ${options.clearClass}` : ''}">
          <span>
            <b>${html(options.clearTitle || `Clear stored ${label}`)}</b>
            <small>${html(options.clearDetail || 'Erase the stored value when Settings are saved.')}</small>
          </span>
          <input type="checkbox" data-clear-secret="${html(key)}">
        </label>` : '';
    const field = input(key, options.label || label, '', {
      type: 'password',
      placeholder: configured ? `${placeholder || label} configured — blank keeps current value` : (placeholder || label),
      autocomplete: 'off',
      hint,
      after: options.clearInside ? clear : '',
    });
    return options.clearInside ? field : field + clear;
  }

  // Directory-valued field with its Browse control (the picker itself is owned
  // by ui-settings-directory-picker.js, opened from the Browse button).
  function directoryField(key, label, value, {hint, browseAction, browseLabel, inline, className}) {
    const id = fieldId(key);
    const control = `<div class="dp-settings-directory-field-control"><input class="input" id="${id}" data-setting="${html(key)}" type="text" value="${html(value)}" ${commitAttributes(key)}><button type="button" class="btn btn-ghost btn-sm dp-settings-directory-field-browse" data-action="${browseAction}" aria-label="${html(browseLabel)}">Browse</button></div>`;
    if (inline) {
      return inlineField(id, label, hint, control,
        {className: ['dp-settings-directory-field', className].filter(Boolean).join(' ')});
    }
    return `
      <div class="dp-settings-field dp-settings-directory-field">
        <label class="form-label" for="${id}">${html(label)}</label>
        ${control}
        <span class="form-hint">${hint}</span>
      </div>`;
  }

  /* The AllDebrid credential row.
   *
   * Entering or replacing the key is an ordinary value change and commits on
   * changed blur through the existing `integration:alldebrid` scope; the
   * browser holds no secret afterwards, because the accepted presentation of
   * one is blank. ERASING the stored key is destructive, so it is an explicit
   * action behind its own confirmation -- never a commit boundary, and never
   * something that also saves a replacement.
   *
   * The clear group occupies the INPUT's own grid row (ui-settings-page.css),
   * so it is centred against the control itself rather than against the
   * label + hint stack, and it takes horizontal room from the field instead of
   * adding a taller action band. */
  /* The three parts of the row that DEPEND on whether a key is stored. They
   * are declared once and used both to render the row and to converge it, so
   * the row has exactly one markup owner in either direction. */
  const ALLDEBRID_KEY_PLACEHOLDER = configured =>
    configured ? CONFIGURED_SECRET_MASK : 'Your AllDebrid API key';

  const ALLDEBRID_KEY_HINT = configured => configured
    ? 'Enter a new API key to replace the stored key. Leave this field blank to keep the current key.'
    : 'Enter your AllDebrid API key.';

  /* The stored-key state, INSIDE the field's trailing edge.
   *
   * It is a status the field carries, not a control and not credential text:
   * it takes no pointer events and no selection, so it can neither be clicked,
   * dragged over, focused nor mistaken for something the operator typed, and
   * the field reserves trailing room for it (ui-settings-page.css) so entered
   * or masked content can never render underneath it. */
  const ALLDEBRID_KEY_PRESENT =
    '<span class="dp-settings-key-present" role="status">Key present</span>';

  const ALLDEBRID_KEY_CLEAR = `
            <button type="button" class="btn btn-danger btn-sm" data-action="clear-alldebrid-key"
                    aria-label="Clear the stored AllDebrid API key">Clear Stored API Key</button>`;

  function allDebridApiKeyField(configured) {
    const key = 'alldebrid_api_key';
    const id = fieldId(key);
    return input(key, 'API Key', '', {
      type: 'password',
      autocomplete: 'off',
      placeholder: ALLDEBRID_KEY_PLACEHOLDER(configured),
      hint: ALLDEBRID_KEY_HINT(configured),
      inline: true,
      className: `dp-settings-alldebrid-key-row ${configured ? 'is-configured' : ''}`,
      after: configured ? ALLDEBRID_KEY_PRESENT : '',
      action: configured ? ALLDEBRID_KEY_CLEAR : '',
    });
  }

  /* The ONE fixed tuning-set collection.
   *
   * Every tuning region renders the same thing: equal-width invisible layout
   * lanes spanning the usable width, one lane per cell of the fixed set, with
   * a bounded card centred in each lane (ui-settings-page.css). The only thing
   * a region contributes is its own CARDINALITY -- declared here, derived from
   * the cells it passed -- so no region states a column count, a breakpoint
   * matrix or a geometry of its own, and none is measured in JavaScript. When
   * the set can no longer hold its lanes at their accepted minimum the lane
   * count drops on its own and the cells left-fill the rows that remain.
   *
   * A boolean and a selector are cells of the same shape as a number; none of
   * them is a special layout.
   *
   * ``tuningGroup`` states a RELATIONSHIP between adjacent cells and nothing
   * else -- it is never a cell, so it contributes its MEMBERS to the lane
   * count rather than one lane. The cell remains the layout unit: at the one
   * width where the whole set holds its lanes the group is drawn as a light
   * outline over exactly the lanes its members occupy (a subgrid, so it owns
   * no track of its own), and at every narrower width it stops being a box at
   * all (`display: contents`) so its cells rejoin the lanes as ordinary cells
   * and the outline disappears entirely rather than splitting across rows.
   * No geometry is measured and no node is ever re-parented. */
  function tuningCells(...cells) {
    const members = cells.flat();
    const lanes = members.reduce((total, markup) => {
      const span = /data-tuning-span="(\d+)"/.exec(markup);
      return total + (span ? Number(span[1]) : 1);
    }, 0);
    return `<div class="dp-settings-tuning-grid" data-tuning-lanes="${lanes}">${members.join('')}</div>`;
  }

  function tuningGroup(...cells) {
    const members = cells.flat();
    return `<div class="dp-settings-tuning-group" data-tuning-span="${members.length}">${members.join('')}</div>`;
  }

  function tuningToggle(key, label, detail, value, options = {}) {
    const id = fieldId(key);
    const boolean = `
          <span class="toggle">
            <input id="${id}" data-setting="${html(key)}" type="checkbox" ${commitAttributes(key)} ${checked(value)}>
            <span class="ttrack"></span>
          </span>`;
    if (options.inline) {
      // A toggle's visible hit target is its TRACK, and the checkbox behind it
      // is visually hidden -- so the track has to be label-associated or the
      // control is operable only through its title. The other inline controls
      // are their own hit target and need no such wrapper.
      return inlineField(id, label, html(detail),
        `<label class="dp-settings-inline-toggle" for="${id}">${boolean}</label>`,
        {className: options.className});
    }
    return `
      <div class="dp-settings-field dp-settings-engine-tuning-toggle-field">
        <label class="form-label" for="${id}">${html(label)}</label>
        <label class="dp-settings-engine-tuning-toggle-control" for="${id}">
          <span class="toggle">
            <input id="${id}" data-setting="${html(key)}" type="checkbox" ${commitAttributes(key)} ${checked(value)}>
            <span class="ttrack"></span>
          </span>
        </label>
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

  /* The aggregate participation gate for one integration GROUP.
   *
   * Deliberately the same control, the same classes and the same immediate
   * discipline as a member's Enable: to the operator these are the same kind
   * of thing, and the only difference is what the mutation is scoped to. */
  /* The ONE Settings protocol identity chip.
   *
   * Six appearances (Network Sources, HTTP(S), (S)FTP and Usenet on
   * Services; Network Sources and Usenet on Downloads) render this
   * and nothing else, so the chip's whole treatment is declared once in CSS
   * and a protocol contributes nothing but its canonical colour. */
  const PROTOCOL_GLYPHS = Object.freeze({
    direct_sources: 'globe',
    general_http: 'globe',
    general_ftp: 'arrow-up-down',
    usenet: 'newspaper',
  });

  function protocolIcon(protocol) {
    const glyph = PROTOCOL_GLYPHS[protocol];
    if (!glyph) return '';
    return `<span class="dp-settings-protocol-chip" aria-hidden="true" data-protocol="${html(protocol)}">`
      + `<img src="/icons/lucide/${glyph}.svg" alt="" decoding="async"></span>`;
  }

  /* What enabling one Network Source allows, as the two lines its protocol box
   * presents. COPY ONLY: the box's identity, label, order and enable state all
   * come from the integration's own published metadata, so this adds no second
   * display-name system and declares no protocol that does not exist. A member
   * with nothing to say here simply says nothing. */
  const SOURCE_BOX_COPY = Object.freeze({
    general_http: ['Direct downloads from', 'HTTP and HTTPS URLs.'],
    general_ftp: ['Direct downloads from', 'FTP and SFTP URLs.'],
  });

  function groupHeaderToggle(groupId, label, value) {
    const safeGroup = String(groupId || '').replace(/[^a-z0-9_-]/gi, '-');
    const id = `dp-settings-integration-group-${safeGroup}-enabled`;
    return `
      <label class="toggle-row dp-settings-toggle dp-settings-auth-header-enable dp-settings-integration-header-enable dp-settings-provider-header-enable" for="${id}">
        <span class="toggle-info"><span class="tl">Enable</span></span>
        <span class="toggle">
          <input id="${id}" data-integration-group-enabled="${html(groupId)}" type="checkbox" ${checked(value)}
                 aria-label="Enable ${html(label)}">
          <span class="ttrack"></span>
        </span>
      </label>`;
  }

  /* The ONE canonical Settings disclosure control.
   *
   * Services cards and Downloads -> Executor Tuning cards had two
   * independently styled disclosures in two different places (a ghost chip at
   * the far right beside Enable, and a naked chevron beside the title). This
   * is the single component both now render: a compact ghost chip sitting
   * immediately after the card title, so the control reads as belonging to the
   * title while the operational controls stay independent on the right. The
   * card header itself is never clickable. */
  function settingsDisclosure(bodyId, expanded, subject, persistKey = '') {
    const label = `${expanded ? 'Collapse' : 'Expand'} ${subject}`;
    return `<button type="button" class="dp-settings-disclosure" data-disclosure-subject="${html(subject)}"${
            persistKey ? ` data-disclosure-persist="${html(persistKey)}"` : ''}
            aria-controls="${html(bodyId)}" aria-expanded="${expanded}" title="${html(label)}"
            aria-label="${html(label)}"><span aria-hidden="true">&rsaquo;</span></button>`;
  }

  /* Disclosure state the operator has CHOSEN, for the sections that keep it.
   *
   * Most Settings disclosures deliberately render closed every time: arriving
   * at a page is not an opinion about what should be open. A section may
   * instead declare a starting state AND a persistence key -- and then the one
   * disclosure owner below remembers what the operator last did with it, so a
   * canonical refresh/re-render does not silently reopen something they closed
   * (or close something they opened). Enable/disable is a different question
   * about a different subject and is untouched by this.
   *
   * Page-lifetime only, and only for sections that ask. */
  const disclosureChoices = new Map();

  const disclosureOpen = (persistKey, fallback) =>
    (persistKey && disclosureChoices.has(persistKey))
      ? disclosureChoices.get(persistKey) : !!fallback;

  // Provider card: the title (with its premium mark), the configuration status,
  // the collapse control and the Enable toggle are all part of the card's own
  // markup. Behavior (collapse / status refresh) is bound by bindEvents().
  /* The provider header's CONFIGURATION report, and only that.
   *
   * The Enable toggle immediately to its right already says whether the
   * provider participates, so this never repeats it. What it adds is the pair
   * of things the toggle cannot say: whether there is a usable SAVED
   * configuration at all, and whether that exact saved configuration has been
   * proven to work.
   *
   *   Unconfigured  nothing usable is saved, and the operator has admitted the
   *                 provider -- so it cannot do the job it was just admitted
   *                 for. A provider that is switched off and unconfigured is
   *                 not a problem and says nothing.
   *   Unverified    saved and usable, but the current saved configuration has
   *                 no successful test behind it. Saved is not proven, and the
   *                 report says which of the two it is.
   *   Verified      the current saved configuration IS covered by successful
   *                 test evidence. Durable canonical truth from the backend;
   *                 this function only reports it. */
  function providerStatus(enabled, configured, verified) {
    if (configured) {
      return verified ? {text: 'Verified', tone: 'success'} : {text: 'Unverified', tone: 'warning'};
    }
    return enabled ? {text: 'Unconfigured', tone: 'error'} : {text: '', tone: 'none'};
  }

  /* The provider-level Test control.
   *
   * Test is a PROVIDER-level action, so it belongs to the card's operational
   * header rail beside the state it proves and the participation control it is
   * about -- never in a credential row (it saves nothing) and never in an
   * optional-tuning disclosure (it is not tuning, and its position must not
   * depend on whether that is open).
   *
   * The grammar is the CARD's, not any one provider's: one declaration, one
   * appearance, one place, for every provider that has something to prove. */
  function providerTestAction(action) {
    return `
          <button class="btn btn-ghost btn-sm dp-settings-provider-test" type="button" data-action="${html(action)}">
            <span class="dp-settings-action-chip" aria-hidden="true">
              <img class="dp-settings-action-glyph" src="/icons/lucide/flask-conical.svg" alt="">
            </span>
            <span>Test</span>
          </button>`;
  }

  /* The card's operational header rail.
   *
   * The right-hand region of the ONE canonical Settings card header reads, in
   * this order: what the provider's configuration currently IS, the
   * provider-level action that can prove it, then whether the provider
   * participates at all. `headerAction` is the neutral slot in the middle --
   * neutral because the grammar is the card's, not any one provider's, so an
   * auth/integration card that acquires a provider-level action later renders
   * it in the same place without a second layout. A card that has no such
   * action renders no slot.
   *
   * Everything about the CONFIGURATION -- credentials, optional tuning behind
   * its disclosure -- stays in the body. The rail therefore never moves when
   * the body grows, shrinks, opens or closes, because it is not in it. */
  function providerCard(identity, title, body, entry, {className, titlePrefix = '', displayName, headerCopy = '', headerAction = ''}) {
    const enabled = entry.enabled !== false;
    const configured = !!entry.configured;
    const verified = !!entry.verified;
    const premium = !!entry.presentation?.premium;
    const safe = String(identity).replace(/[^a-z0-9_-]/gi, '-');
    const enable = integrationHeaderToggle(identity, enabled, displayName, 'dp-settings-provider-header-enable');
    const crown = premium ? '<span class="dp-provider-premium" role="img" title="Premium provider" aria-label="Premium provider"></span>' : '';
    const titleMarkup = titlePrefix
      ? `<span class="card-title dp-settings-card-title--with-icon">${titlePrefix}<span class="dp-settings-card-title-text">${html(title)}</span>${crown}</span>`
      : `<span class="card-title">${html(title)}${crown}</span>`;
    const bodyId = `dp-settings-provider-body-${safe}`;
    // Every expandable card renders CLOSED. Arriving at Services is
    // not an opinion about what should be open, and enabled/configured/verified
    // state is canonical truth about the provider, never about the card.
    const disclosure = settingsDisclosure(bodyId, false, 'provider configuration');
    const status = providerStatus(enabled, configured, verified);
    return `
      <section class="card dp-settings-card dp-large-panel-surface ${className} dp-settings-provider-card--collapsed" data-provider-configured="${configured}">
        <div class="card-header dp-settings-card-header">
          <span class="dp-settings-card-title-group">${titleMarkup}${disclosure}</span>
          <div class="dp-settings-card-header-center">${headerCopy ? `<p class="dp-settings-provider-header-copy">${html(headerCopy)}</p>` : ''}</div>
          <div class="dp-settings-card-header-controls"><div class="dp-settings-provider-config-status" role="status" aria-live="polite" data-tone="${status.tone}"${status.text ? '' : ' hidden'}>${html(status.text)}</div>${
            headerAction ? `<div class="dp-settings-header-action">${headerAction}</div>` : ''}${enable}</div>
        </div>
        <div class="card-body" id="${bodyId}" hidden>${body}</div>
      </section>`;
  }

  /* The ONE Network Source child box.
   *
   * A compact bounded protocol entry: the canonical protocol chip top-left, the
   * canonical presentation label at the top, the canonical immediate Enable in
   * the middle, and the two lines that say what enabling it allows. The toggle
   * is the only action -- a network source holds no credential, so there is
   * nothing to configure, test or save, and therefore no disclosure, no status
   * region and no footer.
   *
   * Neither this nor the grid that arranges it knows which protocols exist:
   * identity, label, order and enable state all come from the integration's own
   * published metadata, so a newly registered member appears by existing. */
  function sourceProtocolBox(identity, label, lines, entry) {
    const enabled = entry.enabled !== false;
    // The provider-card class family is hyphenated, so the durable identity's
    // separators are normalized for the CLASS only; the identity itself, which
    // every control and mutation addresses, is untouched.
    const slug = String(identity).replace(/[^a-z0-9]+/gi, '-');
    return `
      <section class="card dp-settings-card dp-settings-provider-card dp-settings-source-box dp-settings-provider-card--${slug}" data-provider-configured="${!!entry.configured}">
        <div class="dp-settings-source-box-head">
          ${protocolIcon(identity)}
          <span class="card-title"><span class="dp-settings-card-title-text">${html(label)}</span></span>
        </div>
        ${integrationHeaderToggle(identity, enabled, label, 'dp-settings-source-box-enable')}
        <p class="dp-settings-source-box-copy">${lines.map(line => `<span>${html(line)}</span>`).join('')}</p>
      </section>`;
  }


  // --- Usenet ---------------------------------------------------------------
  // Markup owner only. The dynamic server collection's BEHAVIOR (add / remove /
  // save / test / derived display name) belongs to ui-settings-usenet-servers.js;
  // this renders the collection's initial state and its containers exactly once.

  /* One server card.
   *
   * Erasing a stored credential is destructive, so it is an explicit action
   * behind the ONE canonical Settings confirmation. The button is rendered on
   * every card and the group is hidden while the server has nothing stored to
   * clear, so the behaviour owner toggles STATE rather than markup.
   *
   * Test and Remove are rendered exactly once, inside the Advanced grid. Their
   * placement in BOTH disclosure states belongs to that grid
   * (ui-settings-usenet-servers.css); nothing clones, moves or re-parents
   * them. */
  /* One server's participation control.
   *
   * It is an ORDINARY reversible boolean of this record, so it commits
   * immediately through the same `usenet-server` scope, lane and rollback
   * every other field of the card uses -- never through the top-level
   * integration participation owner, which is a different question about a
   * different subject.
   *
   * Its PLACEMENT is the Advanced rail's own far-right structural slot
   * (ui-settings-usenet-servers.css). The rail is a three-track grid whose
   * third track exists in both disclosure states, so Enable stays beside
   * Advanced when Test/Remove drop to their own row. Nothing measures,
   * moves or re-parents it. */
  function usenetEnableControl(value) {
    return `
          <label class="dp-usenet-enable toggle-row">
            <span class="tl">Enable</span>
            <span class="toggle">
              <input type="checkbox" data-usenet-field="enabled"
                     data-commit="immediate" data-commit-scope="usenet-server" data-commit-key="enabled" ${checked(value)}>
              <span class="ttrack"></span>
            </span>
          </label>`;
  }

  function usenetServerCard(server, index) {
    const advancedId = `dp-usenet-advanced-${String(server.id || `new-${index}`).replace(/[^a-z0-9_-]/gi, '-')}`;
    const derived = String(server.host || '').trim();
    const override = String(server.display_name || '').trim();
    const name = override || derived || 'New server';
    const configured = !!server.password_configured;
    return `
      <div class="dp-usenet-server" data-usenet-server-id="${html(server.id || '')}"
           data-commit-instance="${html(server.id || '')}"
           data-usenet-password-configured="${server.password_configured ? '1' : '0'}"
           data-usenet-name-override="${override ? '1' : '0'}">
        <div class="dp-usenet-server-head">
          <span class="dp-usenet-server-name" data-usenet-display-name>${html(name)}</span>
          <button type="button" class="btn btn-ghost btn-sm dp-usenet-name-edit" data-usenet-action="rename"
                  title="Edit display name" aria-label="Edit display name for ${html(name)}">
            <img src="/icons/lucide/pencil.svg" alt="" aria-hidden="true">
          </button>
        </div>
        <div class="dp-usenet-row dp-usenet-row--host">
          <label class="dp-usenet-field dp-usenet-field--host">
            <span class="form-label">Host</span>
            <input class="input" type="text" data-usenet-field="host" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="host" value="${html(server.host || '')}"
                   autocomplete="off" placeholder="news.example.com">
          </label>
          <label class="dp-usenet-field dp-usenet-field--port">
            <span class="form-label">Port</span>
            <input class="input" type="number" min="1" max="65535" data-usenet-field="port" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="port"
                   value="${html(String(server.port ?? 563))}">
          </label>
          <label class="dp-usenet-ssl toggle-row">
            <span class="tl">SSL</span>
            <span class="toggle">
              <input type="checkbox" data-usenet-field="ssl" data-commit="immediate" ${checked(server.ssl !== false)}>
              <span class="ttrack"></span>
            </span>
          </label>
        </div>
        <div class="dp-usenet-row">
          <label class="dp-usenet-field dp-usenet-field--wide">
            <span class="form-label">Username</span>
            <input class="input" type="text" data-usenet-field="username" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="username" value="${html(server.username || '')}" autocomplete="off">
          </label>
        </div>
        <div class="dp-usenet-row dp-usenet-row--password">
          <label class="dp-usenet-field dp-usenet-field--password">
            <span class="form-label">Password</span>
            <input class="input" type="password" data-usenet-field="password" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="password" value=""
                   autocomplete="off" placeholder="${configured ? 'Password configured — blank keeps current value' : 'Password'}">
          </label>
          <div class="dp-usenet-clear-password"${configured ? '' : ' hidden'}>
            <button type="button" class="btn btn-danger btn-sm" data-usenet-action="clear-password"
                    aria-label="Clear the stored password for this server">Clear Password</button>
          </div>
        </div>
        <div class="dp-usenet-advanced" data-usenet-advanced>
          <button type="button" class="dp-usenet-advanced-toggle" data-usenet-advanced-toggle
                  aria-controls="${advancedId}" aria-expanded="false"
                  title="Show advanced acquisition settings"
                  aria-label="Show advanced acquisition settings">
            <span class="dp-usenet-advanced-label">Advanced</span>
            <span class="dp-usenet-advanced-chevron" aria-hidden="true">&rsaquo;</span>
          </button>
          <div class="dp-usenet-advanced-body" id="${advancedId}" hidden>
            <div class="dp-usenet-row dp-usenet-row--tuning">
              <label class="dp-usenet-field">
                <span class="form-label">Connections</span>
                <input class="input" type="number" min="1" max="500" data-usenet-field="connections" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="connections"
                       value="${html(String(server.connections ?? 8))}">
              </label>
              <div class="dp-usenet-field dp-usenet-field--priority">
                <label class="dp-usenet-field-control">
                  <span class="form-label">Priority</span>
                  <input class="input" type="number" min="0" max="99" data-usenet-field="priority" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="priority"
                         value="${html(String(server.priority ?? 0))}">
                </label>
                <span class="form-hint dp-usenet-priority-hint">Lower values have priority.</span>
              </div>
            </div>
            <div class="dp-usenet-row dp-usenet-row--tuning">
              <label class="dp-usenet-field">
                <span class="form-label">Articles per Request</span>
                <input class="input" type="number" min="1" max="20" data-usenet-field="articles_per_request" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="articles_per_request"
                       value="${html(String(server.articles_per_request ?? 2))}">
              </label>
              <label class="dp-usenet-field">
                <span class="form-label">Server Timeout (seconds)</span>
                <input class="input" type="number" min="20" max="240" data-usenet-field="timeout_seconds" data-commit="changed-blur" data-commit-scope="usenet-server" data-commit-key="timeout_seconds"
                       value="${html(String(server.timeout_seconds ?? 60))}">
              </label>
            </div>
            <p class="dp-usenet-advanced-hint">Articles per Request asks this server for several articles without waiting for each reply; Server Timeout is how long to wait for it to answer.</p>
          </div>
          <div class="dp-usenet-actions">
            <button type="button" class="btn btn-ghost btn-sm" data-usenet-action="test">Test</button>
            <button type="button" class="btn btn-ghost btn-sm dp-usenet-remove" data-usenet-action="remove">Remove</button>
          </div>
          ${usenetEnableControl(server.enabled !== false)}
        </div>
        <p class="dp-usenet-field-validation" role="alert" data-usenet-validation hidden></p>
      </div>`;
  }

  function usenetAddTile() {
    return `
      <button type="button" class="dp-usenet-add" data-usenet-action="add" aria-label="Add Server">
        <span class="dp-usenet-add-inner">
          <img class="dp-usenet-add-glyph" src="/icons/lucide/plus.svg" alt="" aria-hidden="true">
          <span class="dp-usenet-add-label">Add Server</span>
        </span>
      </button>`;
  }

  function usenetBody(s, entry) {
    const options = usenetOf(s);
    const servers = Array.isArray(options.servers) ? options.servers : [];
    // Usenet acquisition runs inside DebridPulse, so there is no service
    // address or key to configure -- only the news servers to acquire from.
    return `
      <p class="dp-settings-copy">Add the news servers DebridPulse should download from.</p>
      <div class="dp-usenet-servers" data-usenet-collection>
        ${servers.map(usenetServerCard).join('')}
        ${usenetAddTile()}
      </div>`;
  }

  /* A mild grouping rule between the reserved Usenet card and the debrid
   * providers beneath it. Inset, partial-width and inside the content bounds:
   * it groups, and it is emphatically not a second section, header or
   * category. Its whole treatment is one rule in ui-settings-page.css. */
  const PREMIUM_SEPARATOR = '<div class="dp-settings-group-separator" role="presentation"></div>';

  function sourcesPanel(s) {
    const integrations = s.integrations || {};
    const allDebrid = integrations.alldebrid || {};
    const generalHttp = integrations.general_http || {};
    const providerIdentity = `
      <span class="dp-settings-provider-chip dp-settings-provider-chip--alldebrid" aria-hidden="true">
        <img class="dp-settings-provider-logo dp-settings-provider-logo--alldebrid" src="/icons/providers/alldebrid.svg" alt="">
      </span>`;
    const providerTest = providerTestAction('test-alldebrid');
    const provider = providerCard('alldebrid', 'AllDebrid', `
      <p class="dp-settings-copy">Connect DebridPulse to AllDebrid for direct links, magnets, and torrent files.</p>
      ${allDebridApiKeyField(!!allDebridOf(s).api_key_configured)}
      <details class="dp-settings-additional">
        <summary><span>Additional Settings</span></summary>
        <div class="dp-settings-additional-body">
          ${tuningCells(
            input('alldebrid_rate_limit_per_minute', 'API Calls per Minute', allDebridOf(s).rate_limit_per_minute ?? 60, {
              type: 'number', min: 0, max: 300,
              hint: 'Limits how many requests DebridPulse sends to AllDebrid each minute. Set to 0 for no local limit.'
            }),
            input('poll_interval_seconds', 'Provider Poll Interval (seconds)', policyOf(s).provider_poll_interval_seconds ?? 30, {
              type: 'number', min: 10,
              hint: 'How often DebridPulse checks AllDebrid for updates to active transfers. Shorter intervals provide faster status updates but increase API traffic.'
            }),
            input('full_sync_interval_minutes', 'Full Sync Interval (minutes)', s.full_sync_interval_minutes ?? 5, {
              type: 'number', min: 0, max: 1440,
              hint: 'How often DebridPulse performs a complete reconciliation with AllDebrid. Set to 0 to disable scheduled full syncs.'
            }),
            tuningGroup(
              input('upload_fail_retry_count', 'Upload Failure Retries', policyOf(s).resolution_retry_count ?? 3, {
                type: 'number', min: 0, max: 20,
                hint: 'How many times DebridPulse retries a failed provider upload before giving up. Set to 0 to disable retries.'
              }),
              input('upload_fail_retry_delay_minutes', 'Retry Delay (minutes)', policyOf(s).resolution_retry_delay_minutes ?? 5, {
                type: 'number', min: 0, max: 1440,
                hint: 'How long DebridPulse waits between failed upload attempts. Set to 0 to retry immediately.'
              }),
            ),
          )}
        </div>
      </details>
`, allDebrid, {
      className: 'dp-settings-provider-card dp-settings-provider-card--alldebrid',
      titlePrefix: providerIdentity,
      displayName: 'AllDebrid',
      headerCopy: 'Resolve supported links and torrents through your AllDebrid account.',
      headerAction: providerTest,
    });

    // Absent means OFF for Usenet: it participates only once an operator turns
    // it on. Without this, providerCard's "absent == enabled" default would
    // render the toggle checked and a Save would persist enabled: true.
    const usenet = integrations.usenet || {enabled: false};
    const usenetCard = providerCard('usenet', 'Usenet', usenetBody(s, usenet), usenet, {
      className: 'dp-settings-provider-card dp-settings-provider-card--usenet',
      titlePrefix: protocolIcon('usenet'),
      displayName: 'Usenet',
      headerCopy: 'Download NZB content from configured Usenet news servers.',
      headerAction: providerTestAction('test-usenet'),
    });

    // Usenet is the first card under Premium Services, above the debrid
    // providers. This order is deliberately INDEPENDENT of the Provider Status
    // panel's, which reports Usenet last: the operator configures the service
    // they most often add first, and the panel reads debrid-then-Usenet. One is
    // never derived from the other.
    const premiumServices = groupCard('Premium Services',
      usenetCard + PREMIUM_SEPARATOR + provider, {
      className: 'dp-settings-source-group dp-settings-debrid-services',
    });
    // Group identity, label and gate all come from metadata the members
    // already publish -- there is no second list of who is in this family.
    const groupId = generalHttp.presentation?.status_group || '';
    const groupLabel = (s.integration_groups?.[groupId]?.label)
      || generalHttp.presentation?.status_group_label || 'Network Sources';
    const groupEnabled = s.integration_groups?.[groupId]?.enabled !== false;
    // The members ARE whoever currently declares this group, in the one
    // ordering authority's order. Nothing here enumerates protocols, so a
    // protocol that does not exist yet cannot be rendered as though it did.
    const members = groupId ? Object.entries(integrations)
      .filter(([, entry]) => entry?.presentation?.status_group === groupId)
      .sort(([leftId, left], [rightId, right]) =>
        ((left.presentation?.display_order ?? 0) - (right.presentation?.display_order ?? 0))
        || leftId.localeCompare(rightId)) : [];
    const generalSources = groupCard(groupLabel,
      `<div class="dp-settings-source-box-grid">${members.map(([id, entry]) =>
        sourceProtocolBox(id, entry.presentation?.status_name || id, SOURCE_BOX_COPY[id] || [], entry)).join('')}</div>`, {
      className: 'dp-settings-source-group dp-settings-general-sources',
      titlePrefix: protocolIcon(groupId),
      action: groupId ? groupHeaderToggle(groupId, groupLabel, groupEnabled) : '',
      groupId,
      collapsible: true,
      // The network sources are what an operator arriving at Services most
      // often needs to see; a later manual collapse is theirs and survives
      // every canonical refresh.
      expanded: true,
    });
    return premiumServices + generalSources;
  }

  // Neutral state filters, mapped by the backend from ExecutionState alone.
  const EXECUTOR_WORK_FILTERS = Object.freeze([['all', 'All'], ['active', 'Active'], ['waiting', 'Waiting'], ['paused', 'Paused'], ['stopped', 'Stopped']]);

  /* Executor Work: markup owner only.
   *
   * The rows, the polling and the actions belong to
   * ui-settings-executor-work.js, which reads ONE neutral projection covering
   * every registered executor. Nothing about this card names an executor. */
  function executorWorkCard() {
    return `
      <section class="card dp-settings-card dp-executor-work-card" data-dp-executor-work-card="1" aria-label="Download Engine Activity">
        <div class="card-header">
          <span class="card-title dp-settings-card-title--with-icon dp-settings-inner-card-title" data-dp-settings-icon-section="downloads"><span class="dp-settings-inner-card-icon" aria-hidden="true" data-section="downloads"><img src="${CARD_ICONS['Download Engine Activity'][1]}" alt="" decoding="async"></span><span class="dp-settings-card-title-text">Download Engine Activity</span></span>
          <div class="dp-settings-card-header-center">
            <span class="dp-executor-work-copy">View current download engine jobs and intervene when something is stuck.</span>
          </div>
          <div class="dp-executor-work-header-actions">
            <button type="button" class="btn btn-ghost btn-sm" data-dp-executor-work-refresh>Refresh</button>
          </div>
        </div>
        <div class="card-body" data-dp-executor-work-body>
          <div class="dp-executor-work-context">
            This is an advanced recovery surface. Use Downloads for normal management, and these controls only for troubleshooting or recovery.
          </div>
          <div class="dp-executor-work-control-row">
            <div class="dp-executor-work-metrics" aria-label="Executor work totals">
              <span data-dp-executor-work-speed>0 KB/s</span>
              <span data-dp-executor-work-remaining>— Remaining</span>
            </div>
            <div class="filter-tabs dp-executor-work-filters" role="tablist" aria-label="Filter executor work">
              ${EXECUTOR_WORK_FILTERS.map(([id, label]) => `
              <button type="button" class="ftab${id === 'all' ? ' active' : ''}" role="tab" aria-selected="${id === 'all'}" data-executor-filter="${id}">${label}</button>`).join('')}
            </div>
          </div>
          <div data-dp-executor-work-list="1" class="dp-executor-work-list" aria-live="polite">
            <div class="empty">Loading download engine activity…</div>
          </div>
        </div>
      </section>`;
  }

  // --- Downloads ------------------------------------------------------------
  // Three top-level master cards: Global Download Settings, Executor Tuning
  // (one collapsed child card per executor/capability domain) and the existing
  // Download Safety and Recovery. Operator-facing labels name capabilities,
  // never daemon implementations.

  function executorTuningCard(id, label, copy, body, protocol = '') {
    const bodyId = `dp-executor-tuning-${id}`;
    const identity = protocolIcon(protocol);
    return `
      <section class="card dp-settings-card dp-executor-tuning-card" data-executor-tuning="${html(id)}">
        <div class="card-header dp-settings-card-header">
          <span class="dp-settings-card-title-group">
            <span class="card-title${identity ? ' dp-settings-card-title--with-icon' : ''}">${identity}${
              identity ? `<span class="dp-settings-card-title-text">${html(label)}</span>` : html(label)}</span>
            ${settingsDisclosure(bodyId, false, `${label} tuning`)}
          </span>
          <div class="dp-settings-card-header-center"><span class="dp-settings-download-engine-header-copy">${html(copy)}</span></div>
        </div>
        <div class="card-body" id="${bodyId}" hidden>${body}</div>
      </section>`;
  }

  function directTransfersTuning(s) {
    const aria2 = aria2Of(s);
    // Three relationships: how a transfer is divided across connections, what
    // happens to a transfer already in progress, and how the destination file
    // is prepared on disk.
    return tuningCells(
      tuningGroup(
        input('aria2_max_connection_per_server', 'Connections per Server', aria2.max_connection_per_server ?? 16, {
          type: 'number', min: 1, max: 32,
          hint: 'Maximum number of connections a single download can open to the same server.'
        }),
        input('aria2_split', 'Segments per File', aria2.split ?? 16, {
          type: 'number', min: 1, max: 64,
          hint: 'Controls how many parallel segments a single file can use. Actual connections may be limited by the server and split-size settings.'
        }),
        input('aria2_min_split_size', 'Minimum Split Size', aria2.min_split_size || '10M', {
          hint: 'Controls how small file sections can become when a download is split. Larger values create fewer parallel segments.'
        }),
      ),
      tuningGroup(
        tuningToggle(
          'aria2_continue_downloads',
          'Continue Partial Downloads',
          'Resume existing partial files when possible instead of restarting them from the beginning.',
          aria2.continue_downloads !== false
        ),
        input('aria2_lowest_speed_limit', 'Lowest Speed Limit', aria2.lowest_speed_limit || '0', {
          hint: 'Stops a slow connection when its speed falls at or below this value. Set to 0 to disable the limit.'
        }),
      ),
      tuningGroup(
        input('aria2_disk_cache', 'Disk Cache', aria2.disk_cache || '64M', {
          hint: 'Amount of memory usable as a shared download cache to reduce disk I/O. Set to 0 to disable the cache.'
        }),
        selectField('aria2_file_allocation', 'File Allocation', aria2.file_allocation || 'falloc', [
          ['trunc', 'Truncate'],
          ['falloc', 'Fallocate'],
          ['prealloc', 'Preallocate'],
          ['none', 'None'],
        ], 'Controls how disk space is prepared for new files.'),
      ),
    );
  }

  function usenetTuning(s) {
    // Executor-wide acquisition behaviour only. Global admission (Maximum
    // Concurrent Downloads) and the global download speed cap stay where they
    // already live -- neither is duplicated here -- and nothing about unpacking
    // or folder layout belongs here at all: DebridPulse owns both.
    const options = usenetOf(s);
    // Retries and timeout are the two halves of one question -- what happens to
    // a single article request -- so they stay adjacent; the cache limit and
    // Direct Write are each about writing, but neither depends on the other.
    return tuningCells(
      input('usenet_article_cache_megabytes', 'Article Cache Limit (MB)',
        options.article_cache_megabytes ?? 1024, {
        type: 'number', min: 0, max: 4096,
        hint: 'Memory DebridPulse may use to hold downloaded article data before it is written to disk. Set to 0 to disable the cache.'
      }),
      tuningGroup(
        input('usenet_max_acquisition_retries', 'Maximum Retries',
          options.max_acquisition_retries ?? 3, {
          type: 'number', min: 2, max: 25,
          hint: 'How many times DebridPulse retries a single article on a news server before giving up on that server. This is Usenet acquisition retry only; it is not the DebridPulse download retry count under Disk Space &amp; Recovery.'
        }),
        input('usenet_operation_timeout_seconds', 'Request Timeout (seconds)',
          options.operation_timeout_seconds ?? 30, {
          type: 'number', min: 5, max: 300,
          hint: 'How long DebridPulse waits for the Usenet download service to answer a request.'
        }),
      ),
      tuningToggle(
        'usenet_direct_write',
        'Direct Write',
        'Write article data straight to the destination file instead of buffering it in memory first. Reduces disk I/O when articles arrive in order.',
        options.direct_write !== false
      ),
    ) + `
      <p class="dp-settings-tuning-footer">
        Per-server acquisition tuning belongs to each news server under
        Services.
      </p>`;
  }

  function downloadsPanel(s) {
    const policy = policyOf(s);
    const globalCopy = 'Where DebridPulse saves downloads and how many it runs at once.';
    const delivery = card('Download Location & Limits', `
      <div class="dp-settings-download-engine-row">
        <div class="dp-settings-download-path-stack">
          ${directoryField('download_folder', 'Download Folder', s.download_folder || '/download', {
            hint: 'Where DebridPulse saves downloads.',
            browseAction: 'browse-download-folder',
            browseLabel: 'Browse server directories for Download Folder',
            inline: true, className: 'dp-settings-download-folder-field',
          })}
        </div>
        <div class="dp-settings-download-limit">
          ${input('aria2_max_active_downloads', 'Maximum Concurrent Downloads', policy.max_concurrent_executions ?? 3, {
            type: 'number', min: 1, max: 20,
            hint: 'Maximum downloads DebridPulse runs at once.',
            inline: true, className: 'dp-settings-download-limit-field',
          })}
        </div>
      </div>
    `, {
      className: 'dp-settings-download-engine-card',
      wrapTitle: true,
      headerCenter: `<span class="dp-settings-download-engine-header-copy">${html(globalCopy)}</span>`,
    });

    const tuning = groupCard('Transfer Method Settings',
      // The operator-facing family name matches Services exactly; the executor
      // id stays 'direct', because nothing about the executor changed and
      // renaming it would only churn durable identities.
      executorTuningCard('direct', 'Network Sources',
        'How DebridPulse handles downloads from direct network sources.',
        directTransfersTuning(s), 'direct_sources') +
      executorTuningCard('usenet', 'Usenet',
        'Global download behavior shared by all Usenet servers.', usenetTuning(s), 'usenet'), {
      className: 'dp-settings-source-group dp-executor-tuning-group',
    });

    // Two relationships -- the disk-space guard's threshold and its buffer, and
    // the error-retry count and its delay -- plus one standalone control.
    const recovery = card('Disk Space & Recovery', tuningCells(
      tuningGroup(
        input('min_free_disk_gb', 'Minimum Free Disk Space (GB)', s.min_free_disk_gb ?? 0, {
          type: 'number', min: 0, step: 0.5,
          hint: 'Stops new downloads from starting when free disk space falls below this amount. Set to 0 to disable the disk-space guard.'
        }),
        input('disk_guard_resume_hysteresis_gb', 'Resume Free Space Buffer (GB)', s.disk_guard_resume_hysteresis_gb ?? 0.5, {
          type: 'number', min: 0, step: 0.1,
          hint: 'Extra free space required above the minimum before DebridPulse starts downloads again. Helps prevent repeated stop/start behavior near the limit.'
        }),
      ),
      tuningGroup(
        input('aria2_error_retry_count', 'Download Error Retries', policy.execution_retry_count ?? 3, {
          type: 'number', min: 0, max: 20,
          hint: 'How many times DebridPulse retries a download after an error. Set to 0 to disable automatic retries.'
        }),
        input('aria2_error_retry_delay_seconds', 'Retry Delay (seconds)', policy.execution_retry_delay_seconds ?? 60, {
          type: 'number', min: 0, max: 3600,
          hint: 'How long DebridPulse waits before retrying a download after an error. Set to 0 to retry immediately.'
        }),
      ),
      input('stuck_download_timeout_hours', 'Stalled Download Timeout (hours)', policy.stalled_timeout_hours ?? 6, {
        type: 'number', min: 0, max: 168,
        hint: 'How long a download can remain stalled before DebridPulse attempts automatic recovery. Set to 0 to disable stalled-download recovery.'
      }),
    ), {className: 'dp-settings-download-recovery-card'});

    return delivery + tuning + recovery + executorWorkCard();
  }

  /* The two extraction behaviour controls are ONE group.
   *
   * Two lanes of the ordinary label / control / help rhythm under a single
   * subtle outline -- not two cards, and not two floating controls. The
   * boolean is rendered by the SAME composer the tuning cells use, because a
   * boolean presented as label-over-control-over-help is one shape, not two:
   * only where its control sits inside that stack is stated by this group
   * (ui-settings-downloads-completion.css). */
  function extractionPanel(s) {
    const enableId = fieldId('extract_enabled');
    return card('Automatic Extraction', `
      <div class="dp-settings-extraction-behavior" role="group" aria-label="Extraction behavior">
        ${input('extract_max_concurrent', 'Concurrent Extractions', s.extract_max_concurrent ?? 1, {
          type: 'number', min: 1, max: 8,
          hint: 'Maximum extraction jobs DebridPulse runs at once.',
          inline: true, className: 'dp-settings-extraction-concurrency-field',
        })}
        ${tuningToggle('extract_delete_archive', 'Delete Archives After Extraction',
          'Remove original archive files only after extraction completes successfully.',
          s.extract_delete_archive !== false,
          {inline: true, className: 'dp-settings-extraction-delete-field'})}
      </div>
      ${archivePasswordField(s.extraction_password_configured)}
    `, {
      className: 'dp-settings-extraction-card',
      headerCenter: '<span class="dp-settings-extraction-header-copy">Automatically extract supported archives after a download completes.</span>',
      action: `<label class="dp-settings-extraction-enable" for="${enableId}"><span class="form-label">Enable</span><span class="toggle">
          <input id="${enableId}" data-setting="extract_enabled" type="checkbox" ${commitAttributes('extract_enabled')} ${checked(s.extract_enabled)}>
          <span class="ttrack"></span>
        </span></label>`,
    });
  }

  function notificationsPanel(s) {
    const discord = card('Discord Notifications', `
      <div class="dp-settings-notifications-identity-row">
        ${input('discord_username', 'Display Name', s.discord_username || 'DebridPulse', {
          hint: 'Name shown as the sender of Discord notifications.'
        })}
        <div class="dp-settings-field">
          <label class="form-label" for="${fieldId('discord_avatar_url')}">Avatar URL</label>
          <input class="input" id="${fieldId('discord_avatar_url')}" data-setting="discord_avatar_url" type="text" value="${html(s.discord_avatar_url || '')}" placeholder="https://example.com/avatar.png">
          <span class="form-hint">Image shown with Discord notifications. Paste a direct image URL or upload one.</span>
          <div id="dp-settings-avatar-preview" class="dp-settings-avatar-preview dp-settings-avatar-preview--compact" ${s.discord_avatar_url ? '' : 'hidden'}>
            ${s.discord_avatar_url ? `<img src="${html(s.discord_avatar_url)}" alt="Discord avatar preview">` : ''}
            <span>${s.discord_avatar_url ? html(s.discord_avatar_url) : ''}</span>
          </div>
        </div>
        <div class="dp-settings-actions dp-settings-avatar-actions">
          <label class="btn btn-ghost btn-sm dp-settings-file-button">
            Upload Avatar
            <input id="dp-settings-avatar-file" type="file" accept="image/png,image/jpeg,image/gif,image/webp" hidden>
          </label>
          <button class="btn btn-ghost btn-sm" type="button" data-action="clear-avatar">Clear Avatar</button>
        </div>
      </div>
      <div class="dp-settings-notifications-delivery-row">
        ${secretField('discord_webhook_url', 'Discord Webhook', !!s.discord_webhook_url_configured, 'Primary Discord webhook', 'Primary Discord destination for enabled notifications.', {
          clearInside: true, clearClass: 'dp-settings-notifications-clear-secret',
          clearTitle: 'Clear Stored Webhook', clearDetail: 'Remove the saved primary webhook when Settings are applied.',
        })}
        ${secretField('discord_webhook_added', 'Download Added Webhook', !!s.discord_webhook_added_configured, 'Optional added-event webhook', 'Optional destination for new-download notifications. Leave blank to use the primary webhook.', {
          clearInside: true, clearClass: 'dp-settings-notifications-clear-secret',
          clearTitle: 'Clear Stored Download Added Webhook', clearDetail: 'Remove the saved Download Added webhook when Settings are applied.',
        })}
        ${input('update_check_interval_hours', 'Update Check Interval (Hours Between Checks)', s.update_check_interval_hours ?? 12, {
          type: 'number', min: 0, max: 168, step: 1,
          hint: 'Set how often DebridPulse checks for a newer release. Enter 0 to disable update checks.'
        })}
      </div>
      <div class="dp-settings-notifications-toggle-row dp-settings-notifications-toggle-row--primary">
        ${toggle('discord_notify_added', 'Download Added', 'Send a notification when a new download is accepted.', s.discord_notify_added)}
        ${toggle('discord_notify_finished', 'Download Completed', 'Send a notification when a download finishes successfully.', s.discord_notify_finished)}
        ${toggle('discord_notify_error', 'Download Error', 'Send a notification when a download fails.', s.discord_notify_error)}
      </div>
      <div class="dp-settings-notifications-toggle-row dp-settings-notifications-toggle-row--secondary">
        ${toggle('discord_notify_extract', 'Extraction Result', 'Send a notification when archive extraction completes or fails.', s.discord_notify_extract)}
        ${toggle('discord_notify_update', 'Update Available', 'Send a notification when a newer DebridPulse release is detected.', s.discord_notify_update)}
      </div>
    `, {
      className: 'dp-settings-discord-card',
      headerCenter: 'Configure notification identity, delivery destinations, and event alerts.',
      headerCenterClass: 'dp-settings-notifications-header-copy',
      action: '<div class="dp-settings-notifications-header-spacer" aria-hidden="true"></div>',
    });

    const reports = card('Statistics Reporting', `
      <div class="dp-settings-statistics-reporting-row">
        ${secretField('stats_report_webhook_url', 'Reporting Webhook', !!s.stats_report_webhook_url_configured, 'Optional reporting webhook', 'Optional destination for statistics reports. Leave blank to use the primary Discord webhook.', {
          clearInside: true, clearClass: 'dp-settings-notifications-clear-secret',
          clearTitle: 'Clear Stored Reporting Webhook', clearDetail: 'Remove the saved reporting webhook when Settings are applied.',
        })}
        ${input('stats_report_interval_hours', 'Automatic Report Interval (Hours Between Reports)', s.stats_report_interval_hours ?? 0, {
          type: 'number', min: 0, max: 168, step: 1,
          hint: 'Set how often DebridPulse sends statistics reports. Enter 0 to disable automatic reports.'
        })}
        ${selectField('stats_report_window_hours', 'Report Window', s.stats_report_window_hours ?? 24, [
          [24, '24 hours'],
          [168, '7 days'],
          [720, '30 days'],
          [8760, '1 year'],
        ], 'Choose how much recent activity each statistics report includes.')}
      </div>
    `, {
      className: 'dp-settings-statistics-reporting-card',
      headerCenter: 'Configure where reports are sent, how often they are delivered, and how much activity they summarize.',
      headerCenterClass: 'dp-settings-statistics-reporting-header-copy',
      action: '<div class="dp-settings-notifications-header-spacer" aria-hidden="true"></div>',
    });

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
    const backupEnabledId = fieldId('backup_enabled');
    const backups = card('Backups & Retention', `
      <div class="dp-settings-backups-field-grid">
        ${directoryField('backup_folder', 'Backup Folder', s.backup_folder || '/app/data/backups', {
          hint: 'Choose where DebridPulse stores database and configuration backups.',
          browseAction: 'browse-backup-folder',
          browseLabel: 'Browse server directories for Backup Folder',
        })}
        ${input('backup_interval_hours', 'Backup Interval (Hours Between Backups)', s.backup_interval_hours ?? 24, {type: 'number', min: 1, max: 168, hint: 'Set how often an automatic backup is created.'})}
        ${input('backup_keep_days', 'Backup Retention (Days to Keep)', s.backup_keep_days ?? 7, {type: 'number', min: 1, max: 90, hint: 'Delete backup files older than the configured number of days.'})}
        ${input('stats_snapshot_interval_minutes', 'Statistics Snapshot Interval (Minutes Between Snapshots)', s.stats_snapshot_interval_minutes ?? 60, {
          type: 'number', min: 0, max: 1440, hint: 'Set how often DebridPulse records a statistics snapshot.'
        })}
        ${input('stats_snapshot_keep_days', 'Statistics Snapshot Retention (Days to Keep)', s.stats_snapshot_keep_days ?? 30, {type: 'number', min: 1, max: 365, hint: 'Delete statistics snapshots older than the configured number of days.'})}
        ${input('events_keep_days', 'Event Log Retention (Days to Keep)', s.events_keep_days ?? 30, {type: 'number', min: 1, hint: 'Delete event log entries older than the configured number of days.'})}
      </div>
      <div class="dp-settings-actions dp-settings-backups-actions">
        <button class="btn btn-sm dp-settings-run-backup-success" type="button" data-action="run-backup">Run Backup Now</button>
        <button class="btn btn-ghost btn-sm" type="button" data-action="list-backups">List Backups</button>
      </div>
      <div id="dp-settings-backup-list" class="dp-settings-result-list"></div>
    `, {
      className: 'dp-settings-backups-retention-card',
      headerCenter: 'Configure automated backups and retention for backups, statistics snapshots, and event logs.',
      headerCenterClass: 'dp-settings-backups-header-copy',
      action: `<label class="toggle-row dp-settings-toggle dp-settings-backups-header-toggle" for="${backupEnabledId}">
        <span class="toggle-info">
          <span class="tl">Enable</span>
        </span>
        <span class="toggle">
          <input id="${backupEnabledId}" data-setting="backup_enabled" type="checkbox" ${checked(s.backup_enabled !== false)}>
          <span class="ttrack"></span>
        </span>
      </label>`,
    });
    const reset = card('Database Reset Controls', `
      <div class="dp-settings-caution">
        <b>Database Reset is Destructive</b>
        <span>Processing must be paused before the database can be reset. A backup can be created automatically before the reset begins.</span>
      </div>
      <div class="dp-settings-database-wipe-row">
        ${toggle('db_backup_before_wipe', 'Backup Database Before Reset', 'Create a backup before resetting the database. The reset is aborted if the backup fails.', s.db_backup_before_wipe !== false, 'dp-settings-database-wipe-toggle')}
        ${toggle('db_wipe_enabled', 'Allow Database Reset', 'Unlock the database reset action.', s.db_wipe_enabled, 'dp-settings-database-wipe-toggle')}
        <div class="dp-settings-actions dp-settings-database-wipe-action">
          <button class="btn btn-danger btn-sm" type="button" data-action="wipe-database">Reset Database</button>
        </div>
      </div>
    `, {
      className: 'dp-settings-database-wipe-card',
      headerCenter: 'Configure database safeguards. Perform a destructive database reset when required.',
      headerCenterClass: 'dp-settings-database-wipe-header-copy',
    });
    return backups + reset;
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
          <span class="dp-settings-save-hint">Changes remain unsaved until Apply Settings is selected.</span>
          <div class="dp-settings-context-actions">
            <button class="btn btn-ghost" type="button" data-context-action="notifications" data-action="test-discord"><span class="dp-settings-action-icon"><img class="dp-settings-action-glyph" src="/icons/lucide/flask-conical.svg" alt=""></span><span>Test Discord</span></button>
            <button class="btn btn-ghost" type="button" data-context-action="notifications" data-action="send-report"><span class="dp-settings-action-icon"><img class="dp-settings-action-glyph" src="/icons/lucide/send.svg" alt=""></span><span>Send Report Now</span></button>
            <button class="btn btn-ghost" type="button" data-context-action="authentication" data-action="verify-oidc">Test OIDC Sign-In</button>
          </div>
          <button class="btn btn-primary" type="button" data-action="save" data-deferred-apply>Apply Settings</button>
        </div>
      </section>`;

    activateTab(state.activeTab);
    bindEvents(view);
    updateOidcCallbackPreview();
    snapshotProviderControls(view);
    // Whatever was just rendered FROM canonical state IS the accepted baseline.
    window.DPSettingsPersistence.adopt(view);
    document.dispatchEvent(new CustomEvent('debridpulse:settings-rendered', {detail:{tab: state.activeTab}}));
  }

  /* Tabs that carry NO deferred Apply contract: every control on them is
   * committed by the canonical persistence owner at its own field boundary. */
  const FIELD_BOUNDARY_TABS = new Set(['downloads', 'extraction']);

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

    // A tab whose every control commits at its own field boundary has no
    // deferred Apply contract at all, so the footer must not offer one or
    // claim that anything is unsaved. Apply infrastructure stays exactly as it
    // is for the tabs that still need it.
    const deferred = !FIELD_BOUNDARY_TABS.has(name);
    root()?.querySelectorAll('[data-deferred-apply], .dp-settings-save-hint')
      .forEach(node => { node.hidden = !deferred; });
  }

  // A collapsed provider card can only be re-collapsed by un-checking Enable
  // while none of its own fields has been edited; remember the rendered values.
  const providerBaselines = new WeakMap();

  function controlSignature(el) {
    return el.type === 'checkbox' || el.type === 'radio' ? (el.checked ? '1' : '0') : String(el.value ?? '');
  }

  function providerControls(card) {
    return Array.from(card.querySelectorAll('.card-body input, .card-body select, .card-body textarea'))
      .filter(el => !el.matches('[data-integration-enabled]'));
  }

  function snapshotProviderControls(view) {
    view.querySelectorAll('.dp-settings-provider-card .dp-settings-disclosure').forEach(button => {
      const card = button.closest('.dp-settings-provider-card');
      if (card) providerBaselines.set(card, providerControls(card).map(el => [el, controlSignature(el)]));
    });
  }

  /* The ONE disclosure behaviour, shared by every canonical disclosure chip.
   * The body is addressed through aria-controls, so the same code serves a
   * provider card and an executor-tuning card without knowing either. */
  function setDisclosureExpanded(button, expanded) {
    if (!button) return;
    const persistKey = String(button.dataset.disclosurePersist || '');
    if (persistKey) disclosureChoices.set(persistKey, !!expanded);
    const body = document.getElementById(button.getAttribute('aria-controls'));
    if (body) body.hidden = !expanded;
    button.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    const label = `${expanded ? 'Collapse' : 'Expand'} ${button.dataset.disclosureSubject || 'section'}`;
    button.title = label;
    button.setAttribute('aria-label', label);
    button.closest('.dp-settings-provider-card')
      ?.classList.toggle('dp-settings-provider-card--collapsed', !expanded);
  }

  /* Render one integration card's presentation from COMMITTED canonical state.
   * Never from the operator's click: the visible toggle must not be able to
   * report a participation state the server has not accepted. */
  function renderIntegrationState(card, identity) {
    const entry = state.settings?.integrations?.[identity] || {};
    const enabled = entry.enabled !== false;
    const input = card.querySelector(`[data-integration-enabled="${identity}"]`);
    if (input) input.checked = enabled;
    const disclosure = card.querySelector('.dp-settings-disclosure');
    if (disclosure) {
      // Converging canonical truth is not a reason to OPEN a card: enabled
      // state is not expansion state. Withdrawing a provider does put its
      // configuration away again -- unless the operator has edits in it, which
      // this must never discard.
      const baseline = providerBaselines.get(card) || [];
      const dirty = baseline.some(([el, value]) => el.isConnected && controlSignature(el) !== value);
      if (!enabled && !dirty) setDisclosureExpanded(disclosure, false);
    }
    const status = providerStatus(enabled, !!entry.configured, !!entry.verified);
    const node = card.querySelector('.dp-settings-provider-config-status');
    if (node) {
      node.textContent = status.text;
      node.dataset.tone = status.tone;
      node.hidden = !status.text;
    }
  }

  /* The ONE immediate canonical operational control.
   *
   * An integration/source Enable toggle is operational state, not a deferred
   * form field: it decides whether that integration participates at all and
   * whether its managed lifecycle component has to be running. Leaving it in
   * the page-level Apply Settings write let the visible toggle read ON while
   * `integrations.<id>.enabled` stayed false, which is exactly the divergence
   * that made an enabled Usenet integration report an unreachable service.
   *
   * It persists through the existing generic integration-configuration
   * mutation -- no second enable endpoint, no per-integration bypass -- and the
   * identity comes from the control itself, so every toggle of this class
   * shares one path. Ordinary settings fields stay deferred. */
  async function providerEnableChanged(input) {
    const identity = input.dataset.integrationEnabled;
    const card = input.closest('.dp-settings-provider-card');
    if (!identity || !card) return;
    const desired = input.checked;
    input.disabled = true;
    try {
      const result = await request(
        'PATCH', `/integrations/${encodeURIComponent(identity)}/configuration`,
        {enabled: desired}, 15000);
      adoptIntegration(identity, result);
      renderIntegrationState(card, identity);
      // The ONE automatic expansion in Services, and it belongs to
      // the operator's ACTION rather than to rendering: admitting a provider
      // that has nothing configured leaves it unable to do the job it was just
      // admitted for, so its configuration is put in front of them. It follows
      // the state the server ACCEPTED, never the click.
      const accepted = state.settings?.integrations?.[identity] || {};
      if (accepted.enabled !== false && !accepted.configured) {
        setDisclosureExpanded(card.querySelector('.dp-settings-disclosure'), true);
      }
      const committed = accepted.enabled !== false;
      notify(`${integrationDisplayName(card, identity)} ${committed ? 'enabled' : 'disabled'}`, 'success');
    } catch (error) {
      // Nothing optimistic is left lying: the control and the card return to
      // the canonical namespace the server last confirmed.
      renderIntegrationState(card, identity);
      notify(error.message, 'error');
    } finally {
      input.disabled = false;
    }
    try { window.DPProviderStatus?.refresh(); } catch (_) {}
  }

  /* Render one integration GROUP's presentation from COMMITTED canonical
   * state -- the same rule as renderIntegrationState, for the same reason. */
  function renderGroupState(card, groupId) {
    const entry = state.settings?.integration_groups?.[groupId] || {};
    const input = card.querySelector(`[data-integration-group-enabled="${groupId}"]`);
    if (input) input.checked = entry.enabled !== false;
  }

  function adoptIntegrationGroup(groupId, result) {
    const {ok, group_id, ...entry} = result || {};
    syncGlobalSettings({...state.settings,
      integration_groups: {...state.settings?.integration_groups, [groupId]: entry}});
  }

  /* The group master, on the same immediate path as every member toggle.
   *
   * It is a participation GATE: it persists one boolean scoped to the group
   * and writes no member namespace, so the members' own preferences are
   * exactly as they were on both sides of it. */
  async function groupEnableChanged(input) {
    const groupId = input.dataset.integrationGroupEnabled;
    const card = input.closest('[data-integration-group]');
    if (!groupId || !card) return;
    const desired = input.checked;
    input.disabled = true;
    try {
      const result = await request(
        'PATCH', `/integration-groups/${encodeURIComponent(groupId)}/configuration`,
        {enabled: desired}, 15000);
      adoptIntegrationGroup(groupId, result);
      renderGroupState(card, groupId);
      const committed = state.settings?.integration_groups?.[groupId]?.enabled !== false;
      notify(`${result?.label || groupId} ${committed ? 'enabled' : 'disabled'}`, 'success');
    } catch (error) {
      // Nothing optimistic is left lying: the control returns to the canonical
      // group state the server last confirmed.
      renderGroupState(card, groupId);
      notify(error.message, 'error');
    } finally {
      input.disabled = false;
    }
    try { window.DPProviderStatus?.refresh(); } catch (_) {}
  }

  function integrationDisplayName(card, identity) {
    return card.querySelector('.card-title .dp-settings-card-title-text')?.textContent?.trim()
      || card.querySelector('.card-title')?.textContent?.trim()
      || identity;
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
      if (event.target.matches('[data-integration-enabled]')) void providerEnableChanged(event.target);
      if (event.target.matches('[data-integration-group-enabled]')) void groupEnableChanged(event.target);
      if (event.target.id === 'dp-auth-public-base-url') updateOidcCallbackPreview();
      if (event.target.id === 'dp-settings-avatar-file') uploadAvatar(event.target);
      if (event.target.matches(`[data-setting="api_token_enabled"]`)) setApiTokenEnabled(event.target);
    });

    view.addEventListener('click', event => {
      const disclosure = event.target.closest('.dp-settings-disclosure');
      if (disclosure) {
        event.preventDefault();
        setDisclosureExpanded(disclosure, disclosure.getAttribute('aria-expanded') !== 'true');
        return;
      }
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
      else if (action === 'test-usenet') testUsenet(button);
      else if (action === 'clear-alldebrid-key') clearAllDebridKey(button);
      else if (action === 'clear-archive-passwords') clearArchivePasswords(button);
      else if (action === 'test-discord') testConnection('discord', button);
      else if (action === 'clear-avatar') clearAvatar();
      else if (action === 'browse-download-folder') window.DPSettingsDirectoryPicker?.open('download');
      else if (action === 'browse-backup-folder') window.DPSettingsDirectoryPicker?.open('backup');
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

  function boolOf(key) {
    return !!fieldFor(key)?.checked;
  }

  function clearSecrets() {
    return Array.from(root()?.querySelectorAll('[data-clear-secret]:checked') || [])
      .map(input => input.dataset.clearSecret)
      .filter(Boolean);
  }

  // Provider, executor, transfer-policy, Extraction and runtime-limit settings
  // are owned by their canonical namespaces and are written exclusively through
  // the scoped field-boundary surfaces, never through the whole-settings
  // document and never through the deferred footer, so a stale page snapshot
  // can never undo a value an operator already committed.
  //
  // The footer therefore builds NO integration payload and NO transfer-policy
  // payload at all: every option either namespace holds is a declared control
  // of COMMIT_FIELDS, written one field at a time by the canonical persistence
  // owner. There is nothing left for a deferred write to replay.

  function nonAuthPayload() {
    // Canonical namespaces are never part of the whole-settings write, and the
    // read-only compatibility names the server derived from them are never
    // echoed back (the server lists exactly which names those are).
    const current = settingsDocument(state.settings);
    return {
      ...current,
      // Integration-owned secret clears travel with their own scoped request.
      clear_secrets: clearSecrets().filter(control => !INTEGRATION_SECRET_CONTROLS[control]),
      // Locally owned field-boundary values -- the Services full-sync interval
      // and every Downloads- and Extraction-owned value -- are carried forward
      // from the canonical document this write was built on by the spread
      // above, and are never re-read from the page. Naming one here would be
      // exactly the stale replay this removal exists to prevent.
      full_sync_interval_minutes: Number(current.full_sync_interval_minutes ?? 5),

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
    };
  }

  // Each scoped surface answers with the canonical namespace it just wrote;
  // adopt it into the cached document so every later read (and the whole-
  // settings echo) sees the value the server accepted.
  function adoptIntegration(identity, result) {
    const {ok, ...entry} = result || {};
    syncGlobalSettings({...state.settings,
      integrations: {...state.settings?.integrations, [identity]: entry}});
  }

  function adoptTransferPolicy(result) {
    const {ok, last_apply_error, ...policy} = result || {};
    syncGlobalSettings({...state.settings, transfer_policy: policy});
  }

  /* A control's committed value, in the shape its canonical namespace holds.
   *
   * A checkbox holds a boolean; a number control holds a number -- including a
   * fractional one, because a disk-space guard measured in gigabytes is not an
   * integer and truncating it here would silently save a value the operator did
   * not choose. A number control that cannot be read as a number is submitted
   * as one anyway, so the server -- the only authority on a field's bounds --
   * rejects it and the persistence owner rolls the control back. */
  function committedValue(key, raw) {
    const field = fieldFor(key);
    if (field && field.type === 'checkbox') return raw === true || raw === '1';
    if (!field || field.type !== 'number') return String(raw ?? '');
    const parsed = Number(String(raw).trim());
    return Number.isFinite(parsed) ? parsed : 0;
  }

  /* The WRITABLE whole-settings document: the canonical namespaces are written
   * only through their own scoped surfaces, and the read-only compatibility
   * names the server derives from them are never echoed back. */
  function settingsDocument(source) {
    const document = {...(source || {})};
    delete document.integrations;
    delete document.integration_groups;
    delete document.transfer_policy;
    delete document.execution_runtime_limits;
    for (const name of document.compatibility_fields || []) delete document[name];
    delete document.compatibility_fields;
    return document;
  }

  /* How each canonical namespace a changed-blur control can belong to is
   * written. Every scope dispatches exactly ONE scoped mutation carrying only
   * the field that changed, and adopts exactly what the server accepted. */
  /* How ONE integration namespace is written, for any integration.
   *
   * Every integration-owned option -- an AllDebrid rate limit, an aria2 split
   * count, a Usenet article cache -- is the same act against the same scoped
   * mutation, so it is declared once here rather than once per integration. */
  function registerIntegrationScope(persistence, identity) {
    persistence.defineScope(`integration:${identity}`, {
      commit: async ({key, draft}) => {
        const option = COMMIT_FIELDS[key].option;
        // A secret is declared by the page's own table of integration-owned
        // secret controls; the generic persistence owner knows nothing about it.
        const secret = INTEGRATION_SECRET_CONTROLS[key];
        const result = await request('PATCH', `/integrations/${identity}/configuration`,
          secret
            ? withTestedDrafts(identity, {options: {[option]: String(draft ?? '')}})
            : {options: {[option]: committedValue(key, draft)}}, 15000);
        adoptIntegration(identity, result);
        if (!secret) {
          const accepted = (state.settings?.integrations?.[identity]?.options || {})[option];
          return acceptedValue(key, accepted, draft);
        }
        // A credential is NEVER projected back into the browser. The accepted
        // presentation of one is the blank/configured row the backend's own
        // redacted projection describes, so returning '' is what makes the
        // canonical baseline hold no secret -- and what returns the visible
        // field to that presentation. Verification needs no step here: the
        // stored evidence simply stops describing the saved configuration.
        if (typeof secret.converge === 'function') secret.converge(draft);
        return '';
      },
    });
  }

  /* An accepted canonical value in the SIGNATURE shape the control shows, so a
   * boolean namespace value and a checkbox agree about what "unchanged" means. */
  function acceptedValue(key, accepted, draft) {
    if (accepted === undefined || accepted === null) return draft;
    if (typeof accepted === 'boolean') return accepted ? '1' : '0';
    return String(accepted);
  }

  function registerCommitScopes() {
    const persistence = window.DPSettingsPersistence;

    for (const identity of INTEGRATION_SCOPES) registerIntegrationScope(persistence, identity);

    persistence.defineScope('transfer-policy', {
      commit: async ({key, draft}) => {
        const option = COMMIT_FIELDS[key].option;
        const result = await request('PATCH', '/transfer-policy',
          {[option]: committedValue(key, draft)}, 15000);
        adoptTransferPolicy(result);
        return acceptedValue(key, policyOf(state.settings)[option], draft);
      },
    });

    persistence.defineScope('settings-document', {
      commit: async ({key, draft}) => {
        const option = COMMIT_FIELDS[key].option;
        const result = await writeSettingsDocument({[option]: committedValue(key, draft)});
        // A redacted option's canonical echo is blank BY DESIGN, so it cannot
        // describe what the server accepted. The draft it accepted is the
        // accepted value; nothing else here differs.
        return COMMIT_FIELDS[key].redacted ? draft : acceptedValue(key, result?.[option], draft);
      },
    });
  }

  /* The ONE whole-settings write.
   *
   * The surface has no partial write, so every caller -- a field commit and the
   * one destructive settings-document clear alike -- is a read-modify-write
   * against FRESHLY read canonical truth: never against the rendered page,
   * which would replay unrelated drafts, and never against a cached document,
   * which could be stale. Declared once so there is exactly one such write. */
  async function writeSettingsDocument(overrides, clears = []) {
    const canonical = await request('GET', '/settings', null, 15000);
    const result = await request('PUT', '/settings',
      {...settingsDocument(canonical), clear_secrets: clears, ...overrides}, 15000);
    syncGlobalSettings(result);
    return result;
  }

  /* Proof of what a successful Test actually exercised.
   *
   * This is NOT verification state and it is NOT authority. It is an opaque
   * token the SERVER minted for the draft the SERVER tested, held only so the
   * Save that promotes that draft can present it; the server then re-derives
   * the fingerprint from the configuration it actually saved and accepts the
   * token only for that. A token for a draft the operator has since changed
   * matches nothing, so testing A and saving B stays Configured.
   *
   * Page-lifetime only: never stored, never read back as truth, and the page
   * is not worse off without it -- the operator simply tests again.
   */
  const testedDrafts = new Map();

  function rememberTestedDraft(identity, proof) {
    if (!proof) return;
    testedDrafts.set(identity, [...(testedDrafts.get(identity) || []).slice(-3), String(proof)]);
  }

  const testedDraftProofs = identity => testedDrafts.get(identity) || [];

  /* Hygiene only, never authority: a Test that failed means the server has
   * already superseded the proofs it minted for that material, so holding them
   * would only send tokens it will refuse. The server remains the authority on
   * whether any proof is still valid. */
  const forgetTestedDrafts = identity => testedDrafts.delete(identity);

  /* A request carries only what it is about. Proof of a successful Test is
   * added ONLY when there is one to present. */
  function withTestedDrafts(identity, body) {
    const proofs = testedDraftProofs(identity);
    return proofs.length ? {...body, verification: proofs} : body;
  }

  /* Converge the credential row on ACCEPTED canonical state.
   *
   * The row has to change, because the accepted state changes what it SHOWS --
   * whether a key is present, and therefore whether the Clear action exists at
   * all. Only the parts that depend on that are rebuilt; the INPUT ELEMENT IS
   * NEVER REPLACED. Destroying a control the operator may still be editing
   * would remove focus from it, which IS its commit boundary -- so a draft
   * they had not finished would be persisted by this owner's own re-render
   * rather than by them leaving the field.
   *
   * A key typed after this write was dispatched is therefore simply left
   * alone: it stays visible, stays dirty against the blank accepted baseline,
   * and commits on its own blur. Only the draft this write actually carried is
   * consumed. */
  function renderAllDebridCredential(dispatched) {
    const card = root()?.querySelector('.dp-settings-provider-card--alldebrid');
    const row = card?.querySelector('.dp-settings-alldebrid-key-row');
    if (!card || !row) return;
    const configured = !!allDebridOf(state.settings).api_key_configured;

    const field = fieldFor('alldebrid_api_key');
    if (field) {
      if (String(field.value ?? '') === String(dispatched ?? '')) field.value = '';
      field.placeholder = ALLDEBRID_KEY_PLACEHOLDER(configured);
    }
    row.classList.toggle('is-configured', configured);
    // Only the three parts that DEPEND on whether a key is stored: the hint,
    // the in-field status and the destructive action. The input element itself
    // is still never replaced -- destroying a control the operator may be
    // editing would remove focus from it, which IS its commit boundary.
    const hint = row.querySelector('.dp-settings-inline-field-info > .form-hint');
    if (hint) hint.textContent = ALLDEBRID_KEY_HINT(configured);

    const control = row.querySelector('.dp-settings-inline-field-control');
    const present = control?.querySelector('.dp-settings-key-present');
    if (configured && control && !present) control.insertAdjacentHTML('beforeend', ALLDEBRID_KEY_PRESENT);
    else if (!configured && present) present.remove();

    const action = row.querySelector('.dp-settings-inline-field-action');
    if (configured && !action) {
      row.insertAdjacentHTML('beforeend',
        `<div class="dp-settings-inline-field-action">${ALLDEBRID_KEY_CLEAR}</div>`);
    } else if (!configured && action) {
      action.remove();
    }

    renderIntegrationState(card, 'alldebrid');
  }

  /* Erasing the stored credential.
   *
   * Destructive, so it is an explicit action behind the ONE canonical Settings
   * confirmation (ui-settings-modal.js) and never a commit boundary. The dialog
   * is a GATE in front of this owner: declining it performs no mutation at all,
   * and the accepted canonical projection is what renders on success.
   *
   * It carries ONLY the removal, through the same canonical integration
   * mutation every other AllDebrid write uses: a replacement the operator typed
   * belongs to its own changed-blur boundary, which the settle below orders
   * before this, so the stored key ends up removed either way and this request
   * never saves one. A failure converges nothing. */
  async function clearAllDebridKey(button) {
    const card = root()?.querySelector('.dp-settings-provider-card--alldebrid');
    if (!card) return;
    const confirmed = await window.DPSettingsModal.confirm({
      tone: 'danger',
      title: 'Clear AllDebrid API key?',
      message: 'The stored AllDebrid API key will be removed. AllDebrid cannot be used '
        + 'again until a key is configured.',
      confirmLabel: 'Clear AllDebrid API Key',
    });
    if (!confirmed) return;
    await window.DPSettingsPersistence.settle(root());
    setBusy(button, true, 'Clearing…');
    try {
      const result = await request('PATCH', '/integrations/alldebrid/configuration',
        {options: {}, clear_secrets: ['api_key']}, 15000);
      adoptIntegration('alldebrid', result);
      renderAllDebridCredential('');
      notify('AllDebrid API key cleared', 'success');
      try { window.DPProviderStatus?.refresh(); } catch (_) {}
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(button, false);
    }
  }

  /* Erasing the stored archive-password list.
   *
   * The same act, the same machinery and the same order as erasing the stored
   * AllDebrid key: destructive, therefore an explicit action behind the ONE
   * canonical Settings confirmation (ui-settings-modal.js) and never a commit
   * boundary. Declining performs no mutation at all. Every pending field
   * commit is settled first, so a list the operator was still editing is
   * written before -- never after -- the removal, and the request carries only
   * the removal itself through the one whole-settings write. The accepted
   * canonical projection is what the editor then converges on. */
  async function clearArchivePasswords(button) {
    const confirmed = await window.DPSettingsModal.confirm({
      tone: 'danger',
      title: 'Clear stored archive passwords?',
      message: 'Every stored archive password will be removed. Password-protected archives '
        + 'will not be extracted until passwords are entered again.',
      confirmLabel: 'Clear Passwords',
    });
    if (!confirmed) return;
    await window.DPSettingsPersistence.settle(root());
    setBusy(button, true, 'Clearing…');
    let removed = false;
    try {
      await writeSettingsDocument({}, ['extraction_password']);
      removed = true;
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(button, false);
    }
    // Convergence is the LAST word on this control: releasing the busy state
    // re-enables the button, and with nothing left to clear the editor's own
    // owner must be the one to decide whether it stays enabled.
    if (!removed) return;
    window.DPArchivePasswords?.clear();
    notify('Archive passwords cleared', 'success');
  }

  async function persistNonAuth({renderAfter = true, quiet = false} = {}) {
    const active = state.activeTab;
    // Canonical truth is read immediately before the write: the footer carries
    // forward every field it does not itself own, and a locally persisted
    // value must never be overwritten by an older copy of itself.
    syncGlobalSettings(await request('GET', '/settings', null, 15000));
    // No canonical namespace is written here. Every integration option,
    // transfer-policy value and Downloads-owned settings-document value is a
    // field-boundary control committed by its own scope, so a footer Apply on
    // ANOTHER tab has nothing of theirs to replay -- and cannot overwrite a
    // value the operator committed on Downloads.
    const result = await request('PUT', '/settings', nonAuthPayload(), 15000);
    syncGlobalSettings(result);
    if (renderAfter) {
      state.activeTab = active;
      renderPreservingViewport();
    }
    if (!quiet) notify('Settings saved', 'success');
    try { if (typeof checkConnections === 'function') checkConnections(); } catch (_) {}
    try { if (typeof loadRuntimeStatus === 'function') loadRuntimeStatus(); } catch (_) {}
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
    let openModeConfirmed = false;
    if (!payload.auth_password_enabled && !payload.auth_oidc_enabled && state.auth?.authentication_required && !payload.confirm_open_mode) {
      const confirmed = await window.DPSettingsModal.confirm({
        title: 'Disable interactive authentication?',
        message: 'Username & Password and OpenID Connect will both be disabled. DebridPulse and its API will be intentionally open.',
        confirmLabel: 'Continue to Open Mode',
        tone: 'warning',
      });
      if (!confirmed) return false;
      payload.confirm_open_mode = true;
      openModeConfirmed = true;
    }

    setBusy(button, true, 'Saving…');
    try {
      const auth = await request('PUT', '/auth/config', payload, 15000);
      const generation = acceptAuth(auth, {probe: false});
      state.activeTab = 'authentication';
      renderPreservingViewport();
      if (openModeConfirmed) focusSurvivor('[data-action="save"]');
      void probeOidcRuntime(auth, generation);
      notify(successMessage, 'success');
      return true;
    } catch (error) {
      notify(error.message, 'error');
      return false;
    } finally {
      setBusy(button, false);
      if (openModeConfirmed && button?.isConnected) button.focus();
    }
  }

  async function saveCurrent(button) {
    // One deterministic path: every pending changed-blur commit is finished
    // before the footer reads the form.
    await window.DPSettingsPersistence.settle(root());
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
      // Entry/replacement commits on changed blur and removal is its own
      // explicit action, so no credential intent is ever pending at Test time:
      // the draft this reads is whatever the field still holds -- after the
      // settle above, ordinarily nothing -- and never a removal.
      return {api_key: valueOf('alldebrid_api_key')};
    }
    if (kind === 'discord') {
      return {
        webhook_url: valueOf('discord_webhook_url'),
        clear_webhook: clears.has('discord_webhook_url'),
        username: valueOf('discord_username'),
        avatar_url: valueOf('discord_avatar_url'),
      };
    }
    throw new Error(`Unsupported connection test: ${kind}`);
  }

  async function testConnection(kind, button) {
    // An explicit action reads the CURRENT draft, and never races a blur that
    // the same click started.
    await window.DPSettingsPersistence.settle(root());
    const endpoints = {
      alldebrid: '/settings/validate-alldebrid',
      discord: '/settings/validate-discord',
    };
    const labels = {alldebrid: 'AllDebrid', discord: 'Discord'};
    setBusy(button, true, 'Testing…');
    try {
      const result = await request('POST', endpoints[kind], connectionTestPayload(kind), 20000);
      if (kind === 'alldebrid') {
        rememberTestedDraft('alldebrid', result.verification);
        // A Test of exactly the SAVED configuration establishes durable
        // verification, so the header must stop saying Unverified about a
        // configuration this action just proved. Published through the one
        // acceptance seam, like every other accepted canonical change.
        publishAccepted(result);
        notify(`AllDebrid connected${result.username ? ` as ${result.username}` : ''}`, 'success');
      } else {
        notify('Discord notification sent', 'success');
      }
    } catch (error) {
      if (kind === 'alldebrid') forgetTestedDrafts('alldebrid');
      notify(`${labels[kind]}: ${error.message}`, 'error');
    } finally {
      setBusy(button, false);
    }
  }

  /* The provider-level Usenet Test.
   *
   * It settles every pending field-boundary write first, so the collection it
   * asks the backend to test is the one the operator has actually finished
   * editing -- and then asks for exactly that: the CANONICAL SAVED collection.
   * No server list, no credential and no aggregation crosses the wire from
   * here. Which servers participate, what each proof exercises and whether
   * Usenet ends up Verified are all the backend's, decided from the same
   * per-server evidence the individual Test records. */
  async function testUsenet(button) {
    await window.DPSettingsPersistence.settle(root());
    setBusy(button, true, 'Testing…');
    try {
      const result = await request('POST', '/usenet/test', undefined, 60000);
      publishAccepted(result);
      if (!result.tested) {
        notify('Usenet: no enabled news server is configured', 'warn');
      } else if (result.ok) {
        notify(`Usenet: ${result.passed} of ${result.tested} news server(s) verified`, 'success');
      } else {
        const failed = (result.servers || []).filter(item => !item.ok);
        const named = failed.map(item => item.name).filter(Boolean).join(', ');
        notify(`Usenet: ${result.failed} of ${result.tested} news server(s) failed${named ? ` (${named})` : ''}`, 'error');
      }
    } catch (error) {
      notify(`Usenet: ${error.message}`, 'error');
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

  // Sends a report using the webhooks currently in the form, not only the
  // stored ones, so an unsaved destination can be verified before it is applied.
  async function sendReport(button) {
    setBusy(button, true, 'Sending…');
    try {
      const hours = Math.max(1, intOf('stats_report_window_hours', 24));
      const result = await request('POST', '/settings/send-stats-report', {
        hours,
        stats_report_webhook_url: valueOf('stats_report_webhook_url'),
        clear_stats_report_webhook: clearSecrets().includes('stats_report_webhook_url'),
        discord_webhook_url: valueOf('discord_webhook_url'),
        clear_discord_webhook: clearSecrets().includes('discord_webhook_url'),
      }, 20000);
      notify(`Report sent (${result.hours || hours}h)`, 'success');
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
    const confirmed = await window.DPSettingsModal.confirm({
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
      focusSurvivor('[data-action="wipe-database"]', '[data-action="save"]');
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(button, false);
      // A failed wipe kept its button: return to it once re-enabled so the retry is one keypress away.
      if (button?.isConnected) button.focus();
    }
  }

  async function clearPassword(button) {
    const payload = authPayload();
    const entersOpenMode = !payload.auth_oidc_enabled && state.auth?.authentication_required;
    const confirmed = await window.DPSettingsModal.confirm({
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
    if (await persistAuth(button, payload, 'Stored password cleared')) {
      // The stored-password control is now disabled; the password field is where the operator goes next.
      focusSurvivor('#dp-auth-new-password', '[data-action="save"]');
    } else if (button?.isConnected) {
      button.focus();
    }
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
    const confirmed = await window.DPSettingsModal.confirm({
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
      focusSurvivor('[data-action="generate-token"]');
      notify('API token revoked', 'success');
    } catch (error) {
      notify(error.message, 'error');
    } finally {
      setBusy(button, false);
      if (button?.isConnected) button.focus();
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

  /* The ONE neutral acceptance seam.
   *
   * This page is not the only canonical writer of provider configuration: a
   * Usenet server card writes its own records, and a saved, removed or
   * credentialed server can change that provider's derived ``configured`` and
   * ``verified`` state. When any such owner's scoped mutation is ACCEPTED it
   * publishes the integration's own identity and canonical public projection,
   * and this page adopts it exactly as it adopts the ones it issues itself --
   * through the same single synchronisation owner, re-rendering only that
   * provider's header state.
   *
   * It names no integration, so another owner needs no new callback; it
   * re-renders no page, so no pending draft is destroyed; and it polls for
   * nothing.
   */
  /* Publish an accepted canonical projection a response carried. The seam
   * below is the one consumer; a response that accepted nothing publishes
   * nothing. */
  function publishAccepted(result) {
    const identity = String(result?.integration_id || '');
    const integration = result?.integration;
    if (!identity || !integration) return;
    document.dispatchEvent(new CustomEvent('debridpulse:integration-accepted',
      {detail: {integration_id: identity, integration}}));
  }

  document.addEventListener('debridpulse:integration-accepted', event => {
    const identity = String(event.detail?.integration_id || '');
    const projection = event.detail?.integration;
    if (!identity || !projection || !state.settings) return;
    adoptIntegration(identity, projection);
    const card = root()?.querySelector(`[data-integration-enabled="${identity}"]`)
      ?.closest('.dp-settings-provider-card');
    if (card) renderIntegrationState(card, identity);
    try { window.DPProviderStatus?.refresh(); } catch (_) {}
  });

  registerCommitScopes();
  window.DPSettingsPage = Object.freeze({load});
})();
