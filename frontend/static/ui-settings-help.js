/* DebridPulse Settings contextual help.
 *
 * The ONE owner of what each Settings card's help says. Every Settings card
 * (ui-settings-page.js, the sole Settings markup owner) renders one help chip
 * naming its topic here; activating it calls `DPSettingsHelp.open(topic)`,
 * which presents that topic read-only in the canonical dialog shell
 * (ui-settings-modal.js, `dismiss: true`): its own title, a body, and one
 * Close control, with that shell's focus trap, Escape, focus return, and
 * stacking above an open dialog.
 *
 * Content only. Nothing here reads or writes a setting, issues a request,
 * or names a stored value, and every sentence states behaviour the backend
 * implements today. Text is assigned through textContent; no topic carries
 * markup.
 *
 * Words (1.0.14 localization): every title, heading, paragraph, list item
 * and table cell is a catalog key (a string), or said words with their
 * arguments (`say(key, values)`: a setting value, variable or file type,
 * shown exactly). The context decides which parts exist; the catalog says
 * them. Each node keeps its key, so a language change re-says an open topic
 * in place (ui-i18n.js render) -- the topic, its scroll and focus stay, and
 * nothing is read. A part that differs by reader is a whole message per
 * wording, never a sentence joined from parts.
 *
 * A topic explains the interface its reader actually has. The one context
 * is the interface's own (auth.js `debridPulseAuth.presentation.context`:
 * the role's capabilities, the deployment type, Easy or Advanced and the
 * Easy list's omissions), plus any facts the opener states about its own
 * subject (My Account's profile) or about where it is read: the setup
 * wizard opens a topic with `{wizard: true}` (its Premium Services and each
 * service's view there), and a topic read there explains what the wizard
 * offers -- its Save and Cancel, never a Settings card's controls or another
 * Settings page. A section, a paragraph or a list item may
 * carry `when(c)`: it is shown only where that holds, so a part the reader's
 * interface does not show is never explained as if it were there. Without a
 * context the reader is treated as having no Admin or personal page: never
 * a fuller explanation than it can use.
 */
(function () {
  'use strict';

  const t = (key, values) => window.DPI18n.t(key, values);
  // A message's arguments: one that is itself a message ({$t, $v}) is said first.
  const plain = values => values && Object.fromEntries(Object.entries(values).map(([name, value]) =>
    [name, value && typeof value === 'object' && typeof value.$t === 'string' ? t(value.$t, plain(value.$v) || undefined) : value]));
  // Said words: a key and its arguments; they print as their words in the page's language.
  const say = (key, values = null) => Object.freeze({key, values, toString: () => t(key, plain(values) || undefined)});
  // A message as another message's argument (a card's own label inside its title).
  const said = (key, values = {}) => ({$t: key, $v: values});

  // The role table's two plain answers: what each says is the catalog's;
  // which it is (data-answer) is the fact itself, never its words.
  const YES = Object.freeze({key: 'help.roles.yes', answer: 'yes'});
  const NO = Object.freeze({key: 'help.roles.no', answer: 'no'});

  /* Shared by User Accounts (the card) and the Role selectors of Add Local
   * Account and Edit User. Derived from backend/auth/permissions.py
   * (ROLE_CAPABILITIES, ACTIONS, destruction_precondition). */
  const ROLE_TABLE = Object.freeze({
    caption: 'help.roles.caption',
    head: ['help.roles.capability', 'settings.role.admin', 'settings.role.user', 'settings.role.readonly', 'settings.role.integration'],
    rows: [
      ['help.roles.sign_browser_interface', YES, YES, YES, NO],
      ['help.roles.view_dashboard_downloads_transfer', YES, YES, YES, NO],
      ['help.roles.view_statistics', YES, YES, YES, 'help.roles.summary_counts_through_api'],
      ['help.roles.add_downloads', YES, YES, NO, 'help.roles.through_api_only'],
      ['help.roles.pause_resume_retry_reorder', YES, YES, NO,
        'help.roles.asks_pause_resume_own'],
      ['help.roles.delete_cancel_redirect_transfers', YES,
        'help.roles.only_when_no_other', NO, NO],
      ['help.roles.withdraw_download_requests', 'help.roles.any_account', 'help.roles.own', NO, 'help.roles.own_jobs'],
      ['help.roles.webhook_notifications_statistics_reports', 'help.roles.global_ones_settings',
        'help.roles.own_under_notifications', NO, NO],
      ['help.roles.settings_accounts_maintenance_activity', YES, NO, NO, NO],
      ['help.roles.hold_api_tokens', NO, NO, NO, 'help.roles.yes_issued_admin'],
    ],
  });

  // The conditions topics name: a part the reader's presentation shows (or
  // omits), and the deployment type.
  const shown = part => c => c.shows(part);
  const omitted = part => c => !c.shows(part);
  const onServer = c => c.server;
  const onStandalone = c => c.standalone;
  // Read in the setup wizard, or on Settings (and anywhere else).
  const inWizard = c => c.wizard === true;
  const inSettings = c => c.wizard !== true;

  const TOPICS = Object.freeze({
    /* ── Services ─────────────────────────────────────────────────────── */
    'premium-services': {
      title: 'settings.services.premium',
      sections: [
        // Read in the wizard: its own list, views, Save and Cancel.
        {when: inWizard, text: [
          'help.topic.premium_services.wizard.intro',
          'help.topic.premium_services.wizard.optional',
        ]},
        {heading: 'help.topic.heading.setting_up_service', when: inWizard, list: [
          'help.topic.premium_services.wizard.open_service',
          'help.topic.premium_services.wizard.connect_ways',
          'help.topic.premium_services.wizard.save_cancel',
          'help.topic.premium_services.wizard.immediate',
          'help.topic.premium_services.wizard.test',
          'help.topic.premium_services.wizard.service_help',
        ]},
        {heading: 'help.topic.heading.later', when: inWizard, text: [
          'help.topic.premium_services.wizard.later',
        ]},
        // Read on Settings: its cards.
        {when: inSettings, text: [
          'help.topic.premium_services.intro.premium_services',
          'help.topic.premium_services.intro.every_premium_service',
        ]},
        {heading: 'help.topic.heading.each_card', when: inSettings, list: [
          'help.topic.premium_services.each_card.enable_decides_whether',
          'help.topic.premium_services.each_card.help_each_card',
          'help.topic.premium_services.each_card.credentials',
          'help.topic.premium_services.each_card.test_checks_saved',
          {text: 'help.topic.premium_services.each_card.additional_settings_hold',
            when: shown('provider-additional-settings')},
          {text: 'help.topic.premium_services.each_card.each_service_own',
            when: omitted('provider-additional-settings')},
        ]},
        {heading: 'help.topic.heading.usenet', when: inSettings, text: [
          'help.topic.premium_services.usenet.usenet_uses_configured',
        ]},
        {heading: 'help.topic.heading.related', when: inSettings, text: [
          'help.topic.premium_services.related.direct_sources_need',
        ]},
      ],
    },
    usenet: {
      title: 'help.topic.heading.usenet',
      sections: [
        {text: [
          'help.topic.usenet.intro.usenet_downloads_nzb',
        ]},
        {heading: 'help.topic.heading.news_servers', list: [
          'help.topic.usenet.news_servers.each_server_has',
          'help.topic.usenet.news_servers.connections',
          'help.topic.usenet.news_servers.priority_orders_servers',
          'help.topic.usenet.news_servers.articles_per_request',
        ]},
        {heading: 'help.topic.heading.status_test', list: [
          'help.topic.usenet.status_test.usenet',
          'help.topic.usenet.status_test.server_own_test',
        ]},
        {heading: 'help.topic.heading.downloads_recovery', list: [
          'help.topic.usenet.downloads_recovery.usenet_download',
          'help.topic.usenet.downloads_recovery.debridpulse_verifies_repairs',
          {text: 'help.topic.usenet.downloads_recovery.behavior_shared_every',
            when: c => inSettings(c) && c.shows('transfer-method-settings')},
        ]},
        {heading: 'help.topic.heading.nzbs_premium_services', list: [
          {text: 'help.topic.usenet.nzbs_premium_services.torbox_premiumize_can.provider_additional_settings_shown',
            when: shown('provider-additional-settings')},
          {text: 'help.topic.usenet.nzbs_premium_services.torbox_premiumize_can.provider_additional_settings_hidden',
            when: omitted('provider-additional-settings')},
          'help.topic.usenet.nzbs_premium_services.if_first_service',
        ]},
      ],
    },
    alldebrid: {
      title: 'help.topic.alldebrid.title',
      sections: [
        {text: [
          say('help.topic.alldebrid.intro.alldebrid', {file_type: '.torrent'}),
        ]},
        {heading: 'help.topic.heading.connecting', list: [
          {text: 'help.topic.alldebrid.connecting.paste_api_key', when: inSettings},
          {text: 'help.topic.alldebrid.connecting.test_checks_key', when: inSettings},
          {text: say('help.topic.shared.wizard.paste_api_key', {provider: said('help.topic.alldebrid.title')}), when: inWizard},
          {text: say('help.topic.shared.wizard.test_saved_key', {provider: said('help.topic.alldebrid.title')}), when: inWizard},
        ]},
        {heading: 'help.topic.heading.what_account_can', list: [
          say('help.topic.alldebrid.what_account_can.per_alldebrid_documentation', {file_type: '.torrent'}),
          'help.topic.alldebrid.what_account_can.hoster_links_work',
        ]},
        {heading: 'help.topic.heading.additional_settings', when: shown('provider-additional-settings'), list: [
          'help.topic.alldebrid.additional_settings.prepare_backup_torrents',
          'help.topic.alldebrid.additional_settings.maximum_active_torrents',
          'help.topic.alldebrid.additional_settings.api_calls_per',
        ]},
        {heading: 'help.topic.heading.shared_settings', when: inSettings, text: [
          {text: 'help.topic.shared.behavior_shared_every',
            when: shown('download-behavior-advanced')},
          {text: 'help.topic.shared.maximum_concurrent_downloads',
            when: omitted('download-behavior-advanced')},
        ]},
      ],
    },
    debridlink: {
      title: 'help.topic.debridlink.title',
      sections: [
        {text: [
          say('help.topic.debridlink.intro.debrid_link', {file_type: '.torrent'}),
        ]},
        {heading: 'help.topic.heading.connecting', list: [
          {text: 'help.topic.debridlink.connecting.paste_api_key', when: inSettings},
          {text: 'help.topic.debridlink.connecting.test_checks_key', when: inSettings},
          {text: say('help.topic.shared.wizard.paste_api_key', {provider: said('help.topic.debridlink.title')}), when: inWizard},
          {text: say('help.topic.shared.wizard.test_saved_key', {provider: said('help.topic.debridlink.title')}), when: inWizard},
        ]},
        {heading: 'help.topic.heading.what_account_can', list: [
          say('help.topic.debridlink.what_account_can.premium_debrid_link', {file_type: '.torrent'}),
          'help.topic.debridlink.what_account_can.free_account_can',
          'help.topic.debridlink.what_account_can.limits_debrid_link',
        ]},
        {heading: 'help.topic.heading.additional_settings', when: shown('provider-additional-settings'), list: [
          'help.topic.debridlink.additional_settings.prepare_backup_torrents',
          'help.topic.debridlink.additional_settings.request_timeout',
        ]},
        {heading: 'help.topic.heading.shared_settings', when: inSettings, text: [
          {text: 'help.topic.shared.behavior_shared_every',
            when: shown('download-behavior-advanced')},
          {text: 'help.topic.shared.maximum_concurrent_downloads',
            when: omitted('download-behavior-advanced')},
        ]},
      ],
    },
    premiumize: {
      title: 'help.topic.premiumize.title',
      sections: [
        {text: [
          say('help.topic.premiumize.intro.premiumize', {file_type: '.torrent'}),
        ]},
        {heading: 'help.topic.heading.connecting', list: [
          {text: 'help.topic.premiumize.connecting.paste_api_key', when: inSettings},
          {text: 'help.topic.premiumize.connecting.test_checks_key', when: inSettings},
          {text: say('help.topic.shared.wizard.paste_api_key', {provider: said('help.topic.premiumize.title')}), when: inWizard},
          {text: say('help.topic.shared.wizard.test_saved_key', {provider: said('help.topic.premiumize.title')}), when: inWizard},
        ]},
        {heading: 'help.topic.heading.what_account_can', list: [
          say('help.topic.premiumize.what_account_can.while_premiumize_premium', {file_type: '.torrent'}),
          'help.topic.premiumize.what_account_can.what_free_expired',
          'help.topic.premiumize.what_account_can.premiumize_refuses_work',
        ]},
        {heading: 'help.topic.heading.additional_settings', when: shown('provider-additional-settings'), list: [
          'help.topic.premiumize.additional_settings.use_premiumize_before',
          'help.topic.premiumize.additional_settings.prepare_backup_torrents',
          'help.topic.premiumize.additional_settings.request_timeout',
        ]},
        {heading: 'help.topic.heading.shared_settings', when: inSettings, text: [
          {text: 'help.topic.shared.behavior_shared_every',
            when: shown('download-behavior-advanced')},
          {text: 'help.topic.shared.maximum_concurrent_downloads',
            when: omitted('download-behavior-advanced')},
        ]},
      ],
    },
    realdebrid: {
      title: 'help.topic.realdebrid.title',
      sections: [
        {text: [
          say('help.topic.realdebrid.intro.real_debrid', {file_type: '.torrent'}),
        ]},
        {heading: 'help.topic.heading.connecting', list: [
          'help.topic.realdebrid.connecting.connect_real_debrid',
          'help.topic.realdebrid.connecting.test_checks_stored',
          'help.topic.realdebrid.connecting.disconnect_forgets_stored',
        ]},
        {heading: 'help.topic.heading.what_account_can', list: [
          'help.topic.realdebrid.what_account_can.per_real_debrid',
          'help.topic.realdebrid.what_account_can.free_account_may',
        ]},
        {heading: 'help.topic.heading.additional_settings', when: shown('provider-additional-settings'), list: [
          'help.topic.realdebrid.additional_settings.prepare_backup_torrents',
          'help.topic.realdebrid.additional_settings.maximum_active_torrents',
          'help.topic.realdebrid.additional_settings.api_calls_per',
          'help.topic.realdebrid.additional_settings.request_timeout',
        ]},
        {heading: 'help.topic.heading.shared_settings', when: inSettings, text: [
          {text: 'help.topic.shared.behavior_shared_every',
            when: shown('download-behavior-advanced')},
          {text: 'help.topic.shared.maximum_concurrent_downloads',
            when: omitted('download-behavior-advanced')},
        ]},
      ],
    },
    torbox: {
      title: 'help.topic.torbox.title',
      sections: [
        {text: [
          'help.topic.torbox.intro.torbox',
        ]},
        {heading: 'help.topic.heading.connecting', list: [
          'help.topic.torbox.connecting.connect_torbox_shows',
          'help.topic.torbox.connecting.test_checks_stored',
          'help.topic.torbox.connecting.disconnect_forgets_stored',
        ]},
        {heading: 'help.topic.heading.what_plan_can', list: [
          'help.topic.torbox.what_plan_can.debridpulse_follows_torbox',
          'help.topic.torbox.what_plan_can.nzbs_only_where',
          'help.topic.torbox.what_plan_can.debridpulse',
          'help.topic.torbox.what_plan_can.torbox',
        ]},
        {heading: 'help.topic.heading.additional_settings', when: shown('provider-additional-settings'), list: [
          'help.topic.torbox.additional_settings.prepare_backup_torrents',
          'help.topic.torbox.additional_settings.maximum_active_torrents',
          'help.topic.torbox.additional_settings.use_torbox_before',
          'help.topic.torbox.additional_settings.api_calls_per',
        ]},
        {heading: 'help.topic.heading.shared_settings', when: inSettings, text: [
          {text: 'help.topic.shared.behavior_shared_every',
            when: shown('download-behavior-advanced')},
          {text: 'help.topic.shared.maximum_concurrent_downloads',
            when: omitted('download-behavior-advanced')},
        ]},
      ],
    },
    'network-sources': {
      title: 'integration.group.direct_sources',
      sections: [
        {text: [
          'help.topic.network_sources.intro.network_sources',
        ]},
        {heading: 'help.topic.heading.enable', list: [
          'help.topic.network_sources.enable.enable_switch_card',
          'help.topic.network_sources.enable.turning_group_off',
        ]},
        {heading: 'help.topic.heading.related', when: shown('transfer-method-settings'), text: [
          'help.topic.network_sources.related.how_each_protocol',
        ]},
      ],
    },

    /* ── Downloads ────────────────────────────────────────────────────── */
    'download-behavior': {
      title: 'settings.downloads.behavior',
      sections: [
        {text: [
          'help.topic.download_behavior.intro.these_controls_apply',
        ]},
        {heading: 'help.topic.heading.principal_controls', list: [
          'help.topic.download_behavior.principal_controls.download_folder',
          'help.topic.download_behavior.principal_controls.maximum_concurrent_downloads',
        ]},
        {heading: 'help.topic.heading.everything_else', when: omitted('download-behavior-advanced'), text: [
          'help.topic.download_behavior.everything_else.retries_disk_space',
        ]},
        {heading: 'help.topic.heading.advanced_settings', when: shown('download-behavior-advanced'), list: [
          'help.topic.download_behavior.advanced_settings.material_checkpoint_interval',
          'help.topic.download_behavior.advanced_settings.graceful_stop_timeout',
          'help.topic.download_behavior.advanced_settings.local_network_connections',
          'help.topic.download_behavior.advanced_settings.provider_poll_interval',
          'help.topic.download_behavior.advanced_settings.resolution_failure_retries',
        ]},
      ],
    },
    'transfer-methods': {
      title: 'settings.downloads.methods',
      sections: [
        {text: [
          'help.topic.transfer_methods.intro.each_card_group',
        ]},
        {heading: 'help.topic.heading.methods', list: [
          'help.topic.transfer_methods.methods.network_sources',
          'help.topic.transfer_methods.methods.rsync_compression_modification',
          'help.topic.transfer_methods.methods.webdav',
          'help.topic.transfer_methods.methods.usenet',
          'help.topic.transfer_methods.methods.media_downloads',
        ]},
        {heading: 'help.topic.heading.related', text: [
          'help.topic.transfer_methods.related.whether_method',
        ]},
      ],
    },
    'disk-recovery': {
      title: 'settings.downloads.recovery',
      sections: [
        {heading: 'help.topic.heading.disk_space_guard', list: [
          'help.topic.disk_recovery.disk_space_guard.when_free_space',
          'help.topic.disk_recovery.disk_space_guard.new_downloads_start',
        ]},
        {heading: 'help.topic.heading.recovery', list: [
          'help.topic.disk_recovery.recovery.download_error_retries',
          'help.topic.disk_recovery.recovery.stalled_download_timeout',
        ]},
      ],
    },
    'engine-activity': {
      title: 'settings.engine_activity.title',
      sections: [
        {text: [
          'help.topic.engine_activity.intro.live_list_jobs',
        ]},
        {heading: 'help.topic.heading.controls', list: [
          'help.topic.engine_activity.controls.filters_show_active',
          'help.topic.engine_activity.controls.pause_resume_act',
          'help.topic.engine_activity.controls.terminate',
        ]},
      ],
    },

    /* ── Downloads -> Transfer Method Settings: one topic per method card.
     * Defaults and limits are the backend's own option models
     * (executors/aria2/definition.py Aria2Options, executors/rsync/definition.py
     * RsyncOptions, providers/general_rsync|general_webdav/definition.py,
     * integrations/usenet/definition.py UsenetOptions,
     * integrations/media/definition.py MediaOptions, core/config.py). */
    'tuning-direct': {
      title: say('settings.tuning.title', {method: said('integration.group.direct_sources')}),
      sections: [
        {text: [
          'help.topic.tuning_direct.intro.these_settings_tune',
        ]},
        {heading: 'help.topic.heading.connections_segments', list: [
          'help.topic.tuning_direct.connections_segments.connections_per_server',
          'help.topic.tuning_direct.connections_segments.segments_per_file',
          say('help.topic.tuning_direct.connections_segments.minimum_split_size', {lowest: '1M', highest: '1024M', default: '10M'}),
          'help.topic.tuning_direct.connections_segments.changes_also_apply',
        ]},
        {heading: 'help.topic.heading.interrupted_slow_downloads', list: [
          'help.topic.tuning_direct.interrupted_slow_downloads.continue_partial_downloads',
          'help.topic.tuning_direct.interrupted_slow_downloads.lowest_speed_limit',
        ]},
        {heading: 'help.topic.heading.disk', list: [
          say('help.topic.tuning_direct.disk.disk_cache', {default: '64M'}),
          'help.topic.tuning_direct.disk.file_allocation',
        ]},
        {heading: 'help.topic.heading.related', text: [
          'help.topic.tuning_direct.related.which_network_protocols',
        ]},
      ],
    },
    'tuning-rsync': {
      title: say('settings.tuning.title', {method: 'rsync'}),
      sections: [
        {text: [
          'help.topic.tuning_rsync.intro.these_settings_apply',
        ]},
        {heading: 'help.topic.heading.transfers', list: [
          'help.topic.tuning_rsync.transfers.partial_transfers',
          'help.topic.tuning_rsync.transfers.compression',
          'help.topic.tuning_rsync.transfers.preserve_modification_time',
        ]},
        {heading: 'help.topic.heading.timeouts', list: [
          'help.topic.tuning_rsync.timeouts.connection_timeout',
          'help.topic.tuning_rsync.timeouts.transfer_timeout',
        ]},
        {heading: 'help.topic.heading.directory_depth', list: [
          'help.topic.tuning_rsync.directory_depth.how_many_folder',
        ]},
        {heading: 'help.topic.heading.related', text: [
          'help.topic.tuning_rsync.related.whether_rsync_sources',
        ]},
      ],
    },
    'tuning-webdav': {
      title: say('settings.tuning.title', {method: 'WebDAV'}),
      sections: [
        {text: [
          'help.topic.tuning_webdav.intro.these_settings_decide',
        ]},
        {heading: 'help.topic.heading.collecting_folder', list: [
          'help.topic.tuning_webdav.collecting_folder.directory_depth',
          'help.topic.tuning_webdav.collecting_folder.maximum_files',
          'help.topic.tuning_webdav.collecting_folder.collection_scan_timeout',
        ]},
        {heading: 'help.topic.heading.related', text: [
          'help.topic.tuning_webdav.related.whether_webdav_sources',
        ]},
      ],
    },
    'tuning-usenet': {
      title: say('settings.tuning.title', {method: 'Usenet'}),
      sections: [
        {text: [
          'help.topic.tuning_usenet.intro.these_settings_apply',
        ]},
        {heading: 'help.topic.heading.settings', list: [
          'help.topic.tuning_usenet.settings.article_cache_limit',
          'help.topic.tuning_usenet.settings.maximum_retries',
          'help.topic.tuning_usenet.settings.request_timeout',
          'help.topic.tuning_usenet.settings.direct_write',
        ]},
      ],
    },
    'tuning-media': {
      title: say('settings.tuning.title', {method: said('settings.tuning.media')}),
      sections: [
        {text: [
          'help.topic.tuning_media.intro.these_settings_apply',
        ]},
        {heading: 'help.topic.heading.choosing_what_downloaded', list: [
          'help.topic.tuning_media.choosing_what_downloaded.target_resolution',
          'help.topic.tuning_media.choosing_what_downloaded.video_quality_chooses',
          'help.topic.tuning_media.choosing_what_downloaded.preferred_video_codec',
          'help.topic.tuning_media.choosing_what_downloaded.preferred_subtitle_language',
        ]},
        {heading: 'help.topic.heading.downloading', list: [
          'help.topic.tuning_media.downloading.concurrent_fragment_downloads',
          'help.topic.tuning_media.downloading.youtube_player_client',
        ]},
        {heading: 'help.topic.heading.related', text: [
          'help.topic.tuning_media.related.whether_media_downloads',
        ]},
      ],
    },

    /* ── Extraction ───────────────────────────────────────────────────── */
    'automatic-extraction': {
      title: 'settings.extraction.title',
      sections: [
        {text: [
          'help.topic.automatic_extraction.intro.when_enable',
        ]},
        {heading: 'help.topic.heading.principal_controls', list: [
          'help.topic.automatic_extraction.principal_controls.delete_archives_after',
          'help.topic.automatic_extraction.principal_controls.concurrent_extractions_limits',
          'help.topic.automatic_extraction.principal_controls.archive_passwords',
        ]},
        {heading: 'help.topic.heading.safety', text: [
          'help.topic.automatic_extraction.safety.archives',
        ]},
      ],
    },

    /* ── Authentication ───────────────────────────────────────────────── */
    'auth-status': {
      title: 'settings.auth.status_title',
      sections: [
        {text: [
          'help.topic.auth_status.intro.summary_how_people',
        ]},
        {heading: 'help.topic.heading.browser_sessions', list: [
          'help.topic.auth_status.browser_sessions.browser_session_lifetime',
          'help.topic.auth_status.browser_sessions.active_browser_sessions',
          'help.topic.auth_status.browser_sessions.log_out_current',
          'help.topic.auth_status.browser_sessions.sessions_concern_browser',
        ]},
        {heading: 'help.topic.heading.changing_how_people', when: omitted('username-password'), text: [
          'help.topic.auth_status.changing_how_people.turning_username_password',
        ]},
        {heading: 'help.topic.heading.related', text: [
          'help.topic.auth_status.related.integration_accounts_never',
        ]},
      ],
    },
    'username-password': {
      title: 'settings.auth.credentials_title',
      sections: [
        {text: [
          'help.topic.username_password.intro.local_sign_username',
        ]},
        {heading: 'help.topic.heading.principal_controls', list: [
          'help.topic.username_password.principal_controls.enable',
          'help.topic.username_password.principal_controls.username_password',
          'help.topic.username_password.principal_controls.stored_password',
          'help.topic.username_password.principal_controls.applying_new_credential',
          'help.topic.username_password.principal_controls.clear_inside_password',
          'help.topic.username_password.principal_controls.status_beside_help',
        ]},
        {heading: 'help.topic.heading.related', text: [
          'help.topic.username_password.related.other_people_local',
        ]},
      ],
    },
    oidc: {
      title: 'settings.oidc.title',
      sections: [
        {text: [
          'help.topic.oidc.intro.lets_people_sign',
        ]},
        {heading: 'help.topic.heading.connection', list: [
          say('help.topic.oidc.connection.public_debridpulse_base', {variable: 'PUBLIC_BASE_URL'}),
          'help.topic.oidc.connection.issuer_url_client',
          say('help.topic.oidc.connection.scopes', {scope: 'openid'}),
          'help.topic.oidc.connection.test_signs_through',
        ]},
        {heading: 'help.topic.heading.access_control', list: [
          'help.topic.oidc.access_control.allow_any_authenticated',
          say('help.topic.oidc.access_control.off_user_must', {format: 'issuer|subject-id', claim: 'email_verified=true'}),
          'help.topic.oidc.access_control.email_addresses_match',
        ]},
        {heading: 'help.topic.heading.accounts', list: [
          'help.topic.oidc.accounts.someone_admitted_first',
          'help.topic.oidc.accounts.identity',
        ]},
      ],
    },

    /* ── Users & Access ───────────────────────────────────────────────── */
    'user-accounts': {
      title: 'settings.users.title',
      sections: [
        {text: [
          'help.topic.user_accounts.intro.people_who_sign',
        ]},
        {heading: 'help.topic.heading.roles', list: [
          'help.topic.user_accounts.roles.admin',
          'help.topic.user_accounts.roles.user',
          'help.topic.user_accounts.roles.readonly_user',
          'help.topic.user_accounts.roles.integration',
        ]},
        {table: ROLE_TABLE},
        {heading: 'help.topic.heading.account_actions', list: [
          'help.topic.user_accounts.account_actions.add_local_account',
          'help.topic.user_accounts.account_actions.every_person_has',
          'help.topic.user_accounts.account_actions.disabling_account_ends',
          'help.topic.user_accounts.account_actions.merge_combines_accounts',
          'help.topic.user_accounts.account_actions.debridpulse_always_keeps',
        ]},
      ],
    },
    'integration-accounts': {
      title: 'settings.integrations.title',
      sections: [
        {text: [
          'help.topic.integration_accounts.intro.integration_account_represents',
        ]},
        {heading: 'help.topic.heading.tokens', list: [
          'help.topic.integration_accounts.tokens.issue_token_creates',
          'help.topic.integration_accounts.tokens.each_token',
          'help.topic.integration_accounts.tokens.revoke',
          {text: 'help.topic.integration_accounts.tokens.only_integration_accounts.server', when: onServer},
          {text: 'help.topic.integration_accounts.tokens.only_integration_accounts.standalone', when: onStandalone},
        ]},
        {heading: 'help.topic.heading.what_integration_can', list: [
          'help.topic.integration_accounts.what_integration_can.submit_jobs_into',
          'help.topic.integration_accounts.what_integration_can.ask_pause_resume',
          'help.topic.integration_accounts.what_integration_can.cannot_pause_delete',
        ]},
        {heading: 'help.topic.heading.disable_delete', list: [
          'help.topic.integration_accounts.disable_delete.disabling_account',
          'help.topic.integration_accounts.disable_delete.deleting_also_revokes',
        ]},
      ],
    },
    'delivery-profiles': {
      title: 'settings.profiles.title',
      sections: [
        {text: [
          'help.topic.delivery_profiles.intro.delivery_profile_tells',
        ]},
        {heading: 'help.topic.heading.what_profile_sets', list: [
          'help.topic.delivery_profiles.what_profile_sets.categories',
          'help.topic.delivery_profiles.what_profile_sets.authorized_api_tokens',
          'help.topic.delivery_profiles.what_profile_sets.source_types',
          'help.topic.delivery_profiles.what_profile_sets.dp_download_path',
          'help.topic.delivery_profiles.what_profile_sets.retention',
        ]},
        {heading: 'help.topic.heading.jobs_files', list: [
          'help.topic.delivery_profiles.jobs_files.submitting_job_makes',
          'help.topic.delivery_profiles.jobs_files.completed_files',
          'help.topic.delivery_profiles.jobs_files.download_client_interfaces',
        ]},
      ],
    },
    'deployment-type': {
      title: 'settings.deployment.title',
      sections: [
        {text: [
          'help.topic.deployment_type.intro.deployment_type_decides',
        ]},
        {heading: 'help.topic.heading.server', list: [
          'help.topic.deployment_type.server.every_enabled_admin',
        ]},
        {heading: 'help.topic.heading.standalone', list: [
          'help.topic.deployment_type.standalone.only_built_administrator',
          'help.topic.deployment_type.standalone.password_set_sign',
          'help.topic.deployment_type.standalone.without_password_no',
        ]},
        {heading: 'help.topic.heading.what_switching_keeps', list: [
          'help.topic.deployment_type.what_switching_keeps.no_account',
          'help.topic.deployment_type.what_switching_keeps.returning_server',
          'help.topic.deployment_type.what_switching_keeps.saved_openid_connect',
          'help.topic.deployment_type.what_switching_keeps.integration_accounts_their',
        ]},
        {heading: 'help.topic.heading.what_switching_changes', list: [
          'help.topic.deployment_type.what_switching_changes.each_switch_either',
          'help.topic.deployment_type.what_switching_changes.clears_built_administrator',
          'help.topic.deployment_type.what_switching_changes.afterwards_no_sign',
          'help.topic.deployment_type.what_switching_changes.stays_open_until',
        ]},
        {heading: 'help.topic.heading.default_language', list: [
          'help.topic.deployment_type.default_language.what',
          'help.topic.deployment_type.default_language.apply',
          'help.topic.deployment_type.default_language.inherited',
          'help.topic.deployment_type.default_language.sessions',
        ]},
      ],
    },

    /* ── Notifications (global) ───────────────────────────────────────── */
    discord: {
      title: 'settings.notifications.webhook_title',
      sections: [
        {text: [
          'help.topic.discord.intro.instance_wide_webhook',
          'help.topic.discord.intro.discord_webhook_receives',
        ]},
        {heading: 'help.topic.heading.principal_controls', list: [
          'help.topic.discord.principal_controls.enable',
          'help.topic.discord.principal_controls.primary_webhook_receives',
          'help.topic.discord.principal_controls.display_name_avatar',
          'help.topic.discord.principal_controls.notification_events_choose',
          'help.topic.discord.principal_controls.test',
        ]},
        {heading: 'help.topic.heading.related', text: [
          {text: 'help.topic.discord.related.each_user_can',
            when: onServer},
          {text: 'help.topic.discord.related.statistics_reporting', when: shown('statistics-reporting')},
          {text: 'help.topic.discord.related.statistics_reports_which',
            when: omitted('statistics-reporting')},
        ]},
      ],
    },
    'statistics-reporting': {
      title: 'settings.reports.title',
      sections: [
        {text: [
          'help.topic.statistics_reporting.intro.outbound_summary_debridpulse',
        ]},
        {heading: 'help.topic.heading.who_receives', list: [
          'help.topic.statistics_reporting.who_receives.only_configured_destination',
          'help.topic.statistics_reporting.who_receives.it',
          {text: 'help.topic.statistics_reporting.who_receives.users_who_set', when: onServer},
        ]},
        {heading: 'help.topic.heading.what_when', list: [
          'help.topic.statistics_reporting.what_when.report_window',
          'help.topic.shared.automatic_report_interval',
          'help.topic.statistics_reporting.what_when.test',
          'help.topic.statistics_reporting.what_when.no_destination_nothing',
        ]},
      ],
    },

    /* ── Notifications (a User's own) ─────────────────────────────────── */
    'discord-own': {
      title: 'settings.notifications.webhook_title',
      sections: [
        {text: [
          'help.topic.discord_own.intro.own_webhook_alerts',
          'help.topic.discord_own.intro.webhooks_must_discord',
        ]},
        {heading: 'help.topic.heading.principal_controls', list: [
          'help.topic.discord_own.principal_controls.enable',
          'help.topic.discord_own.principal_controls.primary_webhook_receives',
          'help.topic.discord_own.principal_controls.choose_which_events',
          'help.topic.discord_own.principal_controls.alert',
        ]},
        {heading: 'help.topic.heading.statistics_reports', when: omitted('statistics-reporting'), text: [
          'help.topic.discord_own.statistics_reports.statistics_reports_previously',
        ]},
      ],
    },
    'statistics-reporting-own': {
      title: 'settings.reports.title',
      sections: [
        {text: [
          'help.topic.statistics_reporting_own.intro.own_statistics_report',
        ]},
        {heading: 'help.topic.heading.who_receives', list: [
          'help.topic.statistics_reporting_own.who_receives.only_destination',
        ]},
        {heading: 'help.topic.heading.what_when', list: [
          'help.topic.statistics_reporting_own.what_when.report_window',
          'help.topic.shared.automatic_report_interval',
          'help.topic.statistics_reporting_own.what_when.test',
        ]},
      ],
    },

    /* ── Data & Maintenance ───────────────────────────────────────────── */
    'event-logging': {
      title: 'settings.events.title',
      sections: [
        {text: [
          'help.topic.event_logging.intro.debridpulse_records_event',
        ]},
        {heading: 'help.topic.heading.principal_controls', list: [
          'help.topic.event_logging.principal_controls.activity_log_page',
          'help.topic.event_logging.principal_controls.recorded_events',
        ]},
      ],
    },
    backups: {
      title: 'settings.backups.title',
      sections: [
        {heading: 'help.topic.heading.backups', list: [
          'help.topic.backups.backups.backup_holds_database',
          'help.topic.backups.backups.while_enable',
          {text: 'help.topic.backups.backups.backup',
            when: shown('backup-additional-settings')},
          {text: 'help.topic.backups.backups.older_backups',
            when: omitted('backup-additional-settings')},
          {text: 'help.topic.backups.backups.restoring_resetting_database', when: shown('database-reset')},
          {text: 'help.topic.backups.backups.restoring_makes_safety', when: omitted('database-reset')},
        ]},
        {heading: 'help.topic.heading.statistics_snapshots', when: shown('backup-additional-settings'), list: [
          'help.topic.backups.statistics_snapshots.separately_debridpulse_saves',
          'help.topic.backups.statistics_snapshots.snapshots',
        ]},
        {heading: 'help.topic.heading.statistics_snapshots', when: omitted('backup-additional-settings'), list: [
          'help.topic.backups.statistics_snapshots.debridpulse_keeps_saving',
        ]},
      ],
    },
    'database-reset': {
      title: 'settings.reset.title',
      sections: [
        {text: [
          'help.topic.database_reset.intro.erases_debridpulse_download',
        ]},
        {heading: 'help.topic.heading.what_removed_kept', list: [
          'help.topic.database_reset.what_removed_kept.removed',
          'help.topic.database_reset.what_removed_kept.kept',
        ]},
        {heading: 'help.topic.heading.safeguards', list: [
          'help.topic.database_reset.safeguards.reset_database_works',
          'help.topic.database_reset.safeguards.processing_pauses_reset',
        ]},
      ],
    },

    /* ── My Account (auth.js openAccountDialog) ─────────────────────────────
     * Read for the account the dialog was opened for: `c.account` is what
     * that dialog knows and decided from (the profile read, including the
     * deployment type it states; never a credential), so only what it offers
     * this account is explained. */
    'my-account': {
      titleKey: 'help.my_account.title',
      sections: [
        {text: [
          {key: 'help.my_account.intro'},
          {key: 'help.my_account.intro.open', when: c => c.account?.open === true},
        ]},
        {headingKey: 'help.my_account.profile', list: [
          {key: 'help.my_account.profile.username', when: c => c.account?.showsUsername === true},
          {key: 'help.my_account.profile.display_name'},
          {key: 'help.my_account.profile.actual_name'},
          {key: 'help.my_account.profile.email.own',
            when: c => c.account?.standalone === true || !c.account?.email || c.account?.emailSelfEntered === true},
          {key: 'help.my_account.profile.email.provider',
            when: c => c.account?.standalone !== true && c.account?.email === true && c.account?.emailSelfEntered !== true},
          {key: 'help.my_account.profile.save'},
        ]},
        {headingKey: 'help.my_account.password', when: c => c.account?.passwordAction === 'change', list: [
          {key: 'help.my_account.password.change'},
          {key: 'help.my_account.password.change.sessions'},
        ]},
        {headingKey: 'help.my_account.password', when: c => c.account?.passwordAction === 'set' && c.account?.standaloneFirst !== true, list: [
          {key: 'help.my_account.password.first'},
          {key: 'help.my_account.password.first.enabled'},
        ]},
        {headingKey: 'help.my_account.password', when: c => c.account?.standaloneFirst === true, list: [
          {key: 'help.my_account.password.standalone_first'},
          {key: 'help.my_account.password.standalone_first.signin'},
        ]},
        {headingKey: 'help.my_account.signin', when: c => c.account?.standalone === true && typeof c.account?.signInRequired === 'boolean', list: [
          {key: 'help.my_account.signin.required', when: c => c.account.signInRequired === true},
          {key: 'help.my_account.signin.not_required', when: c => c.account.signInRequired === false},
          {key: 'help.my_account.signin.reading'},
        ]},
        {headingKey: 'help.my_account.clear', when: c => c.account?.clearable === true, list: [
          {key: 'help.my_account.clear.steps'},
          // The same consequence the dialog states, word for word.
          {key: 'account.clear.consequence'},
        ]},
        {headingKey: 'help.my_account.presentation', when: c => c.account?.presentation !== false, list: [
          {key: 'help.my_account.presentation.choice'},
          {key: 'help.my_account.presentation.admin_example', when: c => c.admin},
          {key: 'help.my_account.presentation.user_example', when: c => c.personal},
          {key: 'help.my_account.presentation.unchanged'},
          {key: 'help.my_account.presentation.saved'},
          {key: 'help.my_account.presentation.open', when: c => c.account?.open === true},
        ]},
        // Only where the dialog offers the account's language (its own read succeeded).
        {headingKey: 'help.my_account.language', when: c => c.account?.language === true, list: [
          {key: 'help.my_account.language.button'},
          {key: 'help.my_account.language.system_default'},
          {key: 'help.my_account.language.explicit'},
          {key: 'help.my_account.language.saved'},
          {key: 'help.my_account.language.not_saved'},
        ]},
        // Only where the dialog offers Rerun Setup Wizard (an Admin's).
        {headingKey: 'help.my_account.setup_wizard', when: c => c.account?.setupWizard === true, list: [
          {key: 'help.my_account.setup_wizard.rerun'},
          {key: 'help.my_account.setup_wizard.server', when: c => c.account?.standalone !== true},
          {key: 'help.my_account.setup_wizard.close'},
        ]},
      ],
    },
  });

  // Said words into a node, keyed so that DPI18n.render() re-says them in place.
  function sayInto(element, words) {
    element.dataset.i18n = words.key;
    if (words.values) element.dataset.i18nValues = JSON.stringify(words.values);
    element.textContent = t(words.key, plain(words.values) || undefined);
    return element;
  }
  // A cell, a heading or a part: a key, said words, or a plain answer.
  const wordsOf = value => (typeof value === 'string' ? {key: value, values: null} : value);

  function paragraph(words, className) {
    const element = document.createElement('p');
    element.className = className;
    return sayInto(element, words);
  }

  function table(spec) {
    const wrapper = document.createElement('div');
    wrapper.className = 'dp-settings-help-table-scroll';
    wrapper.tabIndex = 0;
    wrapper.setAttribute('role', 'region');
    named(wrapper, spec.caption);
    const grid = document.createElement('table');
    grid.className = 'dp-settings-help-table';
    sayInto(grid.createCaption(), wordsOf(spec.caption));
    const headRow = grid.createTHead().insertRow();
    spec.head.forEach(label => {
      const cell = document.createElement('th');
      cell.scope = 'col';
      headRow.appendChild(sayInto(cell, wordsOf(label)));
    });
    const body = grid.createTBody();
    spec.rows.forEach(([label, ...values]) => {
      const row = body.insertRow();
      const header = document.createElement('th');
      header.scope = 'row';
      row.appendChild(sayInto(header, wordsOf(label)));
      values.forEach(value => {
        const cell = sayInto(row.insertCell(), wordsOf(value));
        if (value === YES || value === NO) cell.dataset.answer = value.answer;
      });
    });
    wrapper.appendChild(grid);
    const matrix = document.createElement('div');
    matrix.className = 'dp-settings-help-matrix';
    matrix.append(wrapper, answers(spec));
    return matrix;
  }

  /* The same matrix capability by capability, each answer named by its role:
   * where the dialog is too narrow for the table's columns, this is shown in
   * its place (ui-settings-page.css), and only one of the two is ever shown. */
  // A region's accessible name, keyed like its words.
  function named(element, key) {
    element.dataset.i18nAttr = `aria-label:${key}`;
    element.setAttribute('aria-label', t(key));
  }

  function answers(spec) {
    const [, ...roles] = spec.head;
    const list = document.createElement('div');
    list.className = 'dp-settings-help-roles';
    list.setAttribute('role', 'region');
    named(list, spec.caption);
    list.appendChild(paragraph(wordsOf(spec.caption), 'dp-settings-help-roles-caption'));
    spec.rows.forEach(([label, ...values]) => {
      const item = document.createElement('div');
      item.className = 'dp-settings-help-role-item';
      const heading = document.createElement('h4');
      heading.className = 'dp-settings-help-role-capability';
      sayInto(heading, wordsOf(label));
      const pairs = document.createElement('dl');
      pairs.className = 'dp-settings-help-role-answers';
      roles.forEach((role, index) => {
        const pair = document.createElement('div');
        pair.className = 'dp-settings-help-role-answer';
        const term = sayInto(document.createElement('dt'), wordsOf(role));
        const value = sayInto(document.createElement('dd'), wordsOf(values[index]));
        if (values[index] === YES || values[index] === NO) value.dataset.answer = values[index].answer;
        pair.append(term, value);
        pairs.appendChild(pair);
      });
      item.append(heading, pairs);
      list.appendChild(item);
    });
    return list;
  }

  /* The context a topic is read in (see the header). `shows(part)` is
   * whether the presentation shows a part its Easy list names. */
  const UNREAD = Object.freeze({admin: false, personal: false, submit: false, operate: false, standalone: false, server: true,
    easy: false, advanced: true, shows: () => true});

  function context(facts = {}) {
    const c = {...(window.debridPulseAuth?.presentation?.context?.() || UNREAD), ...facts};
    // The wizard draws every service as Easy does, with no Additional
    // Settings (ui-settings-page.js, the wizard surface), whatever the
    // reader's presentation.
    if (!c.wizard) return c;
    const shows = c.shows;
    return {...c, shows: part => part !== 'provider-additional-settings' && shows(part)};
  }

  // A part applies unless its own condition says otherwise.
  const applies = (part, c) => typeof part?.when !== 'function' || part.when(c) === true;
  // A part's words: a catalog key, said words, either as ``text`` under a
  // condition, or (My Account) ``{key}``.
  const words = (part, c) => (applies(part, c)
    ? [typeof part === 'string' ? wordsOf(part) : part.key ? {key: part.key, values: part.values || null} : wordsOf(part.text)] : []);

  function render(body, topic, c) {
    const content = document.createElement('div');
    content.className = 'dp-settings-help-content';
    topic.sections.filter(section => applies(section, c)).forEach(section => {
      const block = document.createElement('section');
      block.className = 'dp-settings-help-section';
      if (section.heading || section.headingKey) {
        const heading = document.createElement('h3');
        heading.className = 'dp-settings-help-heading';
        block.appendChild(sayInto(heading, wordsOf(section.headingKey || section.heading)));
      }
      (section.text || []).flatMap(text => words(text, c))
        .forEach(text => block.appendChild(paragraph(text, 'dp-settings-help-text')));
      const items = (section.list || []).flatMap(text => words(text, c));
      if (items.length) {
        const list = document.createElement('ul');
        list.className = 'dp-settings-help-list';
        items.forEach(text => list.appendChild(sayInto(document.createElement('li'), text)));
        block.appendChild(list);
      }
      if (section.table) block.appendChild(table(section.table));
      content.appendChild(block);
    });
    body.appendChild(content);
    return content.querySelector('.dp-settings-help-text, .dp-settings-help-list');
  }

  /* Open a topic over whatever dialog is open (the dialog stack), for this
   * page's context and the opener's own `facts`. */
  function open(name, facts = {}) {
    const topic = Object.prototype.hasOwnProperty.call(TOPICS, name) ? TOPICS[name] : null;
    if (!topic || !window.DPSettingsModal) return null;
    const c = context(facts);
    // The title as said words: an open topic's title is re-said with its body.
    const titled = topic.titleKey || topic.title;
    return window.DPSettingsModal.open({
      title: typeof titled === 'string' ? say(titled) : titled,
      dismiss: true,
      closeControl: true,
      className: 'dp-settings-help-dialog',
      mount: body => render(body, topic, c),
    });
  }

  window.DPSettingsHelp = Object.freeze({open, topics: Object.freeze(Object.keys(TOPICS))});
})();
