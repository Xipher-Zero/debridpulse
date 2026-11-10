/* DebridPulse canonical Help & Documentation page.
 *
 * Help deliberately does not consume the inherited Help tab markup or legacy
 * tab lifecycle. This runtime owns the current user-facing documentation,
 * master-card/tab composition, and performs no API or backend work.
 *
 * The manual explains the interface its reader actually has: the one context
 * is the interface's own (auth.js `debridPulseAuth.presentation.context`:
 * the role's capabilities, the deployment type, Easy or Advanced and the
 * Easy list's omissions), read again at every composition (`c`). It decides
 * the eligible sections (`eligibleTabs`: an Admin's Settings reference, a
 * User's own Notifications in its place, none for a ReadOnly User) -- the one
 * collection the strip, Previous / Next, the keys and the panels follow --
 * and which parts each section says. A presentation change while Help is
 * shown composes it again, keeping an eligible section. Without a context the
 * reader is treated as having no Admin or personal page.
 *
 * Words (1.0.14 localization): the context decides WHICH passages exist; the
 * catalog says them. Every sentence is an English catalog message its markup
 * names by key (`keyed`); DPI18n.render() says it once the manual is composed
 * and again, in place, on a language change -- so the section shown, an open
 * accordion, the scroll and focus stay as they are, nothing is composed again
 * and nothing is read. A passage that differs by reader is a whole message
 * per wording, never a sentence joined from parts. A <code> span is an
 * argument: a link form, path, file type or setting value is shown exactly,
 * in every language.
 */
(function () {
  'use strict';

  const t = (key, values) => window.DPI18n.t(key, values);
  // A catalog message that prints as its words in the page's language (the
  // section navigator writes a level's words as it places it, String(said)).
  const say = key => ({key, toString: () => t(key)});
  // The attributes that name a message for DPI18n.render(): its key, and its
  // arguments (the <code> values) as JSON.
  const quoted = value => String(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
  const keyed = (key, values) => `data-i18n="${key}" data-i18n-rich${values ? ` data-i18n-values="${quoted(JSON.stringify(values))}"` : ''}`;

  const TABS = Object.freeze([
    ['quickstart', 'help.tab.quickstart', 'rocket'],
    ['using', 'help.tab.using', 'activity'],
    ['howitworks', 'help.tab.howitworks', 'workflow'],
    ['services', 'help.tab.services', 'plug'],
    ['engines', 'help.tab.engines', 'download'],
    ['settings', 'help.tab.settings', 'settings'],
    ['trouble', 'help.tab.trouble', 'wrench'],
    ['license', 'help.tab.license', 'scale'],
  ]);

  const state = { activeTab: 'quickstart' };
  const root = () => document.getElementById('view-help');

  const UNREAD = Object.freeze({admin: false, personal: false, submit: false, operate: false, standalone: false, server: true,
    easy: false, advanced: true, shows: () => true});
  const context = () => window.debridPulseAuth?.presentation?.context?.() || UNREAD;
  // The context the sections were composed for.
  let c = context();
  // What a composition depends on: the same signature composes the same manual.
  const signature = given => [given.admin, given.personal, given.submit, given.standalone, given.easy].join('|');
  // An Admin who sees a part its Easy list names (advanced Admin controls).
  const adminSees = part => c.admin && c.shows(part);

  /* The ONE collection of the sections this reader has, in order, with the
   * labels (catalog keys) it reads them by. */
  function eligibleTabs() {
    return TABS.flatMap(([id, label, icon]) => {
      if (id !== 'settings' || c.admin) return [[id, label, icon]];
      return c.personal ? [[id, 'help.tab.notifications', 'bell-ring']] : [];
    });
  }

  /* The header's one row: the complete strip or the compact navigator,
   * measured and placed by the shared section navigator (ui-section-nav.js),
   * as in Settings. On a handheld (below 780px) the identity beside it gives
   * way, as the navigator needs, through these approved levels and last to
   * none at all; from 780px it is always the first. */
  const navigation = window.DPSectionNav.create({
    nav: () => root()?.querySelector('.dp-help-nav'),
    copy: '.dp-help-header-copy',
    identity: {title: '.dp-help-header-title', hint: '.dp-help-header-subtitle', levels: [
      {title: say('help.header.title'), hint: say('help.header.hint')},
      {title: say('help.header.title'), hint: say('help.header.hint_short')},
      {title: say('help.header.title_short'), hint: say('help.header.hint_short')},
      {title: say('help.header.title_shortest'), hint: ''},
    ]},
  });

  function step(number, title, copy) {
    return `
      <article class="dp-help-step">
        <div class="dp-help-step-number" aria-hidden="true">${number}</div>
        <div class="dp-help-step-copy">
          <div class="dp-help-step-title" ${keyed(title)}></div>
          <div class="dp-help-copy">${copy}</div>
        </div>
      </article>`;
  }

  function pipeline(label, copy, tone = '') {
    return `
      <div class="dp-help-pipeline-row${tone ? ` is-${tone}` : ''}">
        <div class="dp-help-pipeline-node" ${keyed(label)}></div>
        <div class="dp-help-pipeline-arrow" aria-hidden="true">→</div>
        <div class="dp-help-copy" ${keyed(copy)}></div>
      </div>`;
  }

  function inset(title, copy) {
    return `
      <article class="dp-help-inset">
        <h3 ${keyed(title)}></h3>
        <div class="dp-help-copy dp-help-prose">${copy}</div>
      </article>`;
  }

  function accordion(summary, copy, open = false) {
    return `
      <details class="dp-help-accordion"${open ? ' open' : ''}>
        <summary ${keyed(summary)}></summary>
        <div class="dp-help-accordion-body dp-help-copy dp-help-prose">${copy}</div>
      </details>`;
  }

  function quickStartPanel() {
    if (!c.admin) return memberQuickStartPanel();
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-quickstart-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-quickstart-heading" ${keyed('help.quickstart.title')}></h2>
          <p ${keyed('help.quickstart.summary.admin')}></p>
        </div>

        <div class="dp-help-copy dp-help-copy--lead dp-help-prose">
          <p ${keyed('help.quickstart.lead.debridpulse_application_use_submit', {file_type_2: '.nzb', file_type_1: '.torrent'})}></p>
          <p ${keyed('help.quickstart.lead.do_not_need')}></p>
        </div>

        ${inset('help.quickstart.before_begin.title', `
          <ul>
            <li ${keyed('help.quickstart.before_begin.storage')}></li>
            ${c.standalone ? `
            <li ${keyed('help.quickstart.before_begin.login_protection.standalone')}></li>
            <li ${keyed('help.quickstart.before_begin.who_can_do_what.standalone')}></li>` : `
            ${c.shows('username-password') ? `<li ${keyed('help.quickstart.before_begin.login_protection.username_password_shown')}></li>`
              : `<li ${keyed('help.quickstart.before_begin.login_protection.username_password_hidden')}></li>`}
            <li ${keyed('help.quickstart.before_begin.who_can_do_what.server')}></li>`}
          </ul>`)}

        <div class="dp-help-steps">
          ${step(1, 'help.quickstart.check_where_downloads.title', `
            <div class="dp-help-prose">
              <p ${keyed('help.quickstart.check_where_downloads.open_settings_downloads', {path: '/download'})}></p>
              <p ${keyed('help.quickstart.check_where_downloads.if_debridpulse_runs', {path: '/download'})}></p>
            </div>`)}

          ${step(2, 'help.quickstart.choose_services_want.title', `
            <div class="dp-help-prose">
              <p ${keyed('help.quickstart.choose_services_want.open_settings_services')}></p>
              <p ${keyed('help.quickstart.choose_services_want.every_premium_service')}></p>
              <ul>
                <li ${keyed('help.quickstart.choose_services_want.alldebrid_debrid_link_premiumize')}></li>
                <li ${keyed('help.quickstart.choose_services_want.real_debrid_torbox')}></li>
                <li ${keyed('help.quickstart.choose_services_want.usenet')}></li>
              </ul>
              <p ${keyed('help.quickstart.choose_services_want.press_test_card')}></p>
            </div>`)}

          ${step(3, 'help.quickstart.add_first_download.title', `
            <div class="dp-help-prose">
              <p ${keyed('help.quickstart.add_first_download.dashboard_paste_link', {file_type_2: '.nzb', file_type_3: '.meta4', file_type_1: '.torrent'})}></p>
              <p ${keyed('help.quickstart.add_first_download.if_download_contains')}></p>
            </div>`)}

          ${step(4, 'help.quickstart.follow_finish.title', `
            <div class="dp-help-prose">
              <p ${keyed('help.quickstart.follow_finish.download_appears_download.admin')}></p>
              ${c.shows('activity-log') ? `<p ${keyed('help.quickstart.follow_finish.if_something_does.activity_log_shown')}></p>`
                : `<p ${keyed('help.quickstart.follow_finish.if_something_does.activity_log_hidden')}></p>`}
            </div>`)}
        </div>

        ${inset('help.quickstart.where_read_next.title', `
          <ul>
            <li ${keyed('help.quickstart.where_read_next.everyday_use')}></li>
            <li ${keyed('help.quickstart.where_read_next.how_works')}></li>
            <li ${keyed('help.quickstart.where_read_next.services')}></li>
            <li ${keyed('help.quickstart.where_read_next.settings')}></li>
          </ul>
          ${c.standalone ? `<p ${keyed('help.quickstart.where_read_next.optional_features_such.standalone')}></p>` : `<p ${keyed('help.quickstart.where_read_next.optional_features_such.server')}></p>`}`)}
      </section>`;
  }

  function usingPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-using-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-using-heading" ${keyed('help.using.title')}></h2>
          <p ${keyed('help.using.summary')}></p>
        </div>

        <div class="dp-help-copy dp-help-copy--lead dp-help-prose">
          ${adminSees('activity-log') ? `<p ${keyed('help.using.lead.sidebar_has_three.activity_log_shown')}></p>`
            : `${c.admin ? `<p ${keyed('help.using.lead.sidebar_has_three.admin')}></p>` : c.personal ? `<p ${keyed('help.using.lead.sidebar_has_three.personal')}></p>` : `<p ${keyed('help.using.lead.sidebar_has_three.no_personal')}></p>`}`}
          ${c.standalone ? `<p ${keyed('help.using.lead.standalone_installation_only')}></p>`
            : `<p ${keyed('help.using.lead.what_see_depends')}></p>`}
          ${c.easy ? `<p ${keyed('help.using.lead.presentation_easy_so')}></p>` : ''}
        </div>

        <div class="dp-help-accordion-list">
          ${c.standalone ? accordion('help.using.who_uses_installation.title', `
            <ul>
              <li ${keyed('help.using.who_uses_installation.built_administrator')}></li>
              <li ${keyed('help.using.who_uses_installation.other_people_accounts')}></li>
              <li ${keyed('help.using.who_uses_installation.integration')}></li>
            </ul>
            <p ${keyed('help.using.who_uses_installation.my_account')}></p>`, true) : accordion('help.using.what_role_lets.title', `
            <ul>
              <li ${keyed('help.using.what_role_lets.admin')}></li>
              <li ${keyed('help.using.what_role_lets.user')}></li>
              <li ${keyed('help.using.what_role_lets.readonly_user')}></li>
              <li ${keyed('help.using.who_uses_installation.integration')}></li>
            </ul>
            <p ${keyed('help.using.what_role_lets.name_bottom_sidebar')}></p>
            <p ${keyed('help.using.what_role_lets.my_account_easy')}></p>`, true)}

          ${accordion('help.using.header_controls.title', `
            <ul>
              ${c.admin ? `<li ${keyed('help.using.header_controls.pause_all')}></li>
              <li ${keyed('help.using.header_controls.resume_paused_n')}></li>
              <li ${keyed('help.using.header_controls.activity_badge.admin')}></li>` : `<li ${keyed('help.using.header_controls.activity_badge.member')}></li>
              <li ${keyed('help.using.header_controls.when_admin_pauses')}></li>`}
              <li ${keyed('help.using.header_controls.theme_button')}></li>
            </ul>
            <p ${keyed('help.using.header_controls.while_downloads_running')}></p>`, true)}

          ${c.admin ? accordion('help.using.provider_status_sidebar.title', `
            <p ${keyed('help.using.provider_status_sidebar.sidebar_lists_services')}></p>
            <ul>
              <li ${keyed('help.using.provider_status_sidebar.green')}></li>
              <li ${keyed('help.using.provider_status_sidebar.amber')}></li>
              <li ${keyed('help.using.provider_status_sidebar.red')}></li>
              <li ${keyed('help.using.provider_status_sidebar.pulsing_amber')}></li>
            </ul>
            <p ${keyed('help.using.provider_status_sidebar.network_sources_share')}></p>`) : ''}

          ${accordion(c.submit ? 'help.using.dashboard_quick_add.title' : 'help.using.dashboard.title', `
            <p ${keyed('help.using.dashboard_quick_add.summary_cards_count')}></p>
${c.submit ? `<p ${keyed('help.using.dashboard_quick_add.quick_add')}></p>
            <ul>
              <li ${keyed('help.using.dashboard_quick_add.links', {link_2: 'https://', link_3: 'ftp://', link_4: 'sftp://', link_5: 'scp://', link_6: 'ssh://', link_7: 'rsync://', link_8: 'rsync+ssh://', link_9: 'webdav://', link_10: 'webdavs://', link_11: 'dav://', link_12: 'davs://', link_1: 'http://'})}></li>
              <li ${keyed('help.using.dashboard_quick_add.magnet_links', {link: 'magnet:?'})}></li>
              <li ${keyed('help.using.dashboard_quick_add.alternate_sources')}></li>
            </ul>
            <p ${keyed('help.using.dashboard_quick_add.press_add_ctrl')}></p>
            <p ${keyed('help.using.dashboard_quick_add.add_file_empty')}></p>
            <ul>
              <li ${keyed('help.using.dashboard_quick_add.torrent_torrent_file', {file_type: '.torrent'})}></li>
              <li ${keyed('help.using.dashboard_quick_add.nzb_usenet_download', {file_type: '.nzb'})}></li>
              <li ${keyed('help.using.dashboard_quick_add.meta4_metalink_file', {file_type: '.meta4'})}></li>
              <li ${keyed('help.using.dashboard_quick_add.any_other_file', {literal: '#'})}></li>
            </ul>` : ''}

            ${c.admin ? `<p ${keyed('help.using.dashboard_quick_add.recover_all')}></p>` : ''}
            ${c.submit ? `<p ${keyed('help.using.dashboard_quick_add.download_activity.submit')}></p>` : `<p ${keyed('help.using.dashboard_quick_add.download_activity.viewer')}></p>`}`)}

          ${c.submit ? accordion('help.using.questions_debridpulse_may.title', `
            <ul>
              <li ${keyed('help.using.questions_debridpulse_may.connect_local_network_address')}></li>
              <li ${keyed('help.using.questions_debridpulse_may.download_item_collection')}></li>
              <li ${keyed('help.using.questions_debridpulse_may.authentication_required')}></li>
              <li ${keyed('help.using.questions_debridpulse_may.verify_server_identity')}></li>
            </ul>
            <p ${keyed('help.using.questions_debridpulse_may.these_dialogs_do')}></p>`) : ''}

          ${c.submit ? accordion('help.using.choosing_files.title', `
            <p ${keyed('help.using.choosing_files.when_torrent_magnet')}></p>
            <ul>
              <li ${keyed('help.using.choosing_files.if_do_nothing')}></li>
              <li ${keyed('help.using.choosing_files.cancel_transfer')}></li>
              <li ${keyed('help.using.choosing_files.can_reopen_selection')}></li>
              <li ${keyed('help.using.choosing_files.collection_separate_items')}></li>
              <li ${keyed('help.using.choosing_files.nzb_downloads_always')}></li>
            </ul>`) : ''}

          ${accordion('help.using.downloads_page.title', `
            <ul>
              <li ${keyed('help.using.downloads_page.filters')}></li>
              <li ${keyed('help.using.downloads_page.order')}></li>
              ${c.operate ? `<li ${keyed('help.using.downloads_page.row_actions')}></li>
              <li ${keyed('help.using.downloads_page.several_once')}></li>
              <li ${keyed('help.using.downloads_page.queue_order')}></li>` : ''}
              <li ${keyed('help.using.downloads_page.beneath_each_name')}></li>
              <li ${keyed('help.using.downloads_page.date_column_menu')}></li>
            </ul>`)}

          ${accordion('help.using.download_details.title', `
            <p ${keyed('help.using.download_details.click_any_download')}></p>
            <ul>
              <li ${keyed('help.using.download_details.provider')}></li>
              <li ${keyed('help.using.download_details.requested')}></li>
              <li ${keyed('help.using.download_details.route_history')}></li>
              <li ${keyed('help.using.download_details.advanced_acquisition_details')}></li>
              ${c.operate ? `<li ${keyed('help.using.download_details.files.operate')}></li>` : `<li ${keyed('help.using.download_details.files.viewer')}></li>`}
              ${c.admin ? `<li ${keyed('help.using.download_details.events.admin')}></li>` : `<li ${keyed('help.using.download_details.events.member')}></li>`}
            </ul>
            <p ${keyed('help.using.download_details.progress_marked_not')}></p>`)}

          ${adminSees('activity-log') ? accordion('help.using.activity_log_admin.title', `
            <p ${keyed('help.using.activity_log_admin.activity_log_full')}></p>
            <p ${keyed('help.using.activity_log_admin.events_kept_until')}></p>`) : ''}

          ${accordion('help.using.statistics.title', `
            <ul>
              <li ${keyed('help.using.statistics.top_cards_completions')}></li>
              <li ${keyed('help.using.statistics.overall_rolling_metrics')}></li>
              <li ${keyed('help.using.statistics.completions_chart_splits')}></li>
              <li ${keyed('help.using.statistics.transfer_methods')}></li>
              <li ${keyed('help.using.statistics.transfer_disposition')}></li>
            </ul>`)}
        </div>
      </section>`;
  }

  function howItWorksPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-howitworks-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-howitworks-heading" ${keyed('help.howitworks.title')}></h2>
          <p ${keyed('help.howitworks.summary')}></p>
        </div>

        <div class="dp-help-copy dp-help-copy--lead dp-help-prose">
          <p ${keyed('help.howitworks.lead.each_thing_add')}></p>
        </div>

        <div class="dp-help-pipeline">
          ${pipeline('help.howitworks.pipeline.add.title', 'help.howitworks.pipeline.add.copy')}
          ${pipeline('help.howitworks.pipeline.choose_service.title', 'help.howitworks.pipeline.choose_service.copy')}
          ${pipeline('help.howitworks.pipeline.prepare.title', 'help.howitworks.pipeline.prepare.copy')}
          ${pipeline('help.howitworks.pipeline.download.title', 'help.howitworks.pipeline.download.copy', 'active')}
          ${pipeline('help.howitworks.pipeline.check_finish.title', 'help.howitworks.pipeline.check_finish.copy', 'success')}
          ${pipeline('help.howitworks.pipeline.extract.title', 'help.howitworks.pipeline.extract.copy')}
        </div>

        <div class="dp-help-accordion-list">
          ${accordion('help.howitworks.how_service_chosen.title', `
            <ul>
              <li ${keyed('help.howitworks.how_service_chosen.specialist_services_come_first', {file_type: '.torrent'})}></li>
              <li ${keyed('help.howitworks.how_service_chosen.links_added_together_one')}></li>
              <li ${keyed('help.howitworks.how_service_chosen.waits_rather_than_guesses')}></li>
              <li ${keyed('help.howitworks.how_service_chosen.ready_content_preferred')}></li>
              <li ${keyed('help.howitworks.how_service_chosen.no_ranking_setting')}></li>
              <li ${keyed('help.howitworks.how_service_chosen.enabled_not_same_usable')}></li>
            </ul>`, true)}

          ${accordion('help.howitworks.what_status_words.title', `
            <ul>
              <li ${keyed('help.howitworks.what_status_words.pending_resolving_processing')}></li>
              <li ${keyed('help.howitworks.what_status_words.ready_queued_waiting_execution')}></li>
              <li ${keyed('help.howitworks.what_status_words.downloading_verifying')}></li>
              <li ${keyed('help.howitworks.what_status_words.waiting_provider_waiting_storage')}></li>
              <li ${keyed('help.howitworks.what_status_words.input_required')}></li>
              <li ${keyed('help.howitworks.what_status_words.awaiting_confirmation')}></li>
              <li ${keyed('help.howitworks.what_status_words.paused_recovering')}></li>
              <li ${keyed('help.howitworks.what_status_words.extracting')}></li>
              <li ${keyed('help.howitworks.what_status_words.done')}></li>
              <li ${keyed('help.howitworks.what_status_words.failed_requires_attention_cancelled')}></li>
            </ul>`)}

          ${accordion('help.howitworks.when_something_fails.title', `
            <p ${keyed('help.howitworks.when_something_fails.debridpulse_sorts_failures')}></p>
            <ul>
              ${adminSees('download-behavior-advanced') ? `<li ${keyed('help.howitworks.when_something_fails.temporary_problem.download_behavior_advanced_shown')}></li>`
                : `<li ${keyed('help.howitworks.when_something_fails.temporary_problem.download_behavior_advanced_hidden')}></li>`}
              <li ${keyed('help.howitworks.when_something_fails.problem_one_service')}></li>
              <li ${keyed('help.howitworks.when_something_fails.problem_download_itself')}></li>
              <li ${keyed('help.howitworks.when_something_fails.other_sources_file')}></li>
              <li ${keyed('help.howitworks.when_something_fails.stalled_downloads')}></li>
            </ul>
            <p ${keyed('help.howitworks.when_something_fails.switching_another_source')}></p>`)}

          ${accordion('help.howitworks.switching_service_source.title', `
            <p ${keyed('help.howitworks.switching_service_source.download_stays_same')}></p>
            <ul>
              <li ${keyed('help.howitworks.switching_service_source.choosing_service')}></li>
              <li ${keyed('help.howitworks.switching_service_source.what_service_switch_does')}></li>
              <li ${keyed('help.howitworks.switching_service_source.choosing_source')}></li>
              <li ${keyed('help.howitworks.switching_service_source.service_shown_one_committed')}></li>
              <li ${keyed('help.howitworks.switching_service_source.not_every_choice_accepted')}></li>
              <li ${keyed('help.howitworks.switching_service_source.why_choices_change')}></li>
              <li ${keyed('help.howitworks.switching_service_source.progress')}></li>
            </ul>
            <p ${keyed('help.howitworks.switching_service_source.while_download_paused')}></p>`)}

          ${accordion('help.howitworks.backup_torrents.title', adminSees('provider-additional-settings') ? `
            <p ${keyed('help.howitworks.backup_torrents.each_debrid_service')}></p>` : `
            ${c.admin ? `<p ${keyed('help.howitworks.backup_torrents.debrid_service_can.admin')}></p>` : `<p ${keyed('help.howitworks.backup_torrents.debrid_service_can.member')}></p>`}`)}

          ${accordion('help.howitworks.pause_resume_retry.title', `
            <ul>
              <li ${keyed('help.howitworks.pause_resume_retry.pause')}></li>
              <li ${keyed('help.howitworks.pause_resume_retry.pause_all')}></li>
              <li ${keyed('help.howitworks.pause_resume_retry.retry')}></li>
              <li ${keyed('help.howitworks.pause_resume_retry.recover_all')}></li>
              <li ${keyed('help.howitworks.pause_resume_retry.remove')}></li>
            </ul>
            <p ${keyed('help.howitworks.pause_resume_retry.admin_can_remove')}></p>`)}

          ${accordion('help.howitworks.adding_same_thing.title', `
            <p ${keyed('help.howitworks.adding_same_thing.if_add_magnet', {file_type: '.torrent'})}></p>`)}

          ${accordion('help.howitworks.what_survives_restart.title', `
            <p ${keyed('help.howitworks.what_survives_restart.downloads_their_history')}></p>`)}

          ${accordion('help.howitworks.passwords_private_links.title', `
            <p ${keyed('help.howitworks.passwords_private_links.username_password_written', {link: 'scheme://user:password@host/...'})}></p>
            <p ${keyed('help.howitworks.passwords_private_links.sftp_scp_ssh')}></p>`)}
        </div>
      </section>`;
  }

  function servicesPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-services-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-services-heading" ${keyed('help.services.title')}></h2>
          ${c.admin ? `<p ${keyed('help.services.summary.admin')}></p>`
            : `<p ${keyed('help.services.summary.member')}></p>`}
        </div>

        <div class="dp-help-copy dp-help-copy--lead dp-help-prose">
          <p ${keyed('help.services.lead.settings_services_has')}></p>
        </div>

        ${!c.admin ? inset('help.services.who_sets_up.title', `
          <p ${keyed('help.services.who_sets_up.admin_connects_configures')}></p>`) : inset('help.services.how_service_card.title', `
          <ul>
            <li ${keyed('help.services.how_service_card.enable')}></li>
            <li ${keyed('help.services.how_service_card.status_word_says')}></li>
            <li ${keyed('help.services.how_service_card.test')}></li>
            ${c.shows('provider-additional-settings') ? `<li ${keyed('help.services.how_service_card.additional_settings')}></li>`
              : `<li ${keyed('help.services.how_service_card.each_card_optional')}></li>`}
            <li ${keyed('help.services.how_service_card.while_service_still')}></li>
            <li ${keyed('help.services.how_service_card.help_button_card')}></li>
          </ul>`)}

        <h3 ${keyed('help.services.group.premium_services')}></h3>
        <div class="dp-help-accordion-list">
          ${accordion('help.services.alldebrid.title', `
            <p ${keyed('help.services.alldebrid.for', {file_type: '.torrent'})}></p>
            ${c.admin ? `<p ${keyed('help.services.alldebrid.need')}></p>` : ''}
            <p ${keyed('help.services.alldebrid.what_happens')}></p>
            ${adminSees('provider-additional-settings') ? `<p ${keyed('help.services.alldebrid.good_know.provider_additional_settings_shown')}></p>` : `<p ${keyed('help.services.alldebrid.good_know.provider_additional_settings_hidden')}></p>`}
            ${c.admin ? `<p ${keyed('help.services.alldebrid.if_fails')}></p>` : ''}`)}

          ${accordion('help.services.debrid_link.title', `
            <p ${keyed('help.services.debrid_link.for', {file_type: '.torrent'})}></p>
            ${c.admin ? `<p ${keyed('help.services.debrid_link.need')}></p>` : ''}
            <p ${keyed('help.services.debrid_link.what_happens')}></p>
            ${adminSees('provider-additional-settings') ? `<p ${keyed('help.services.debrid_link.good_know.provider_additional_settings_shown')}></p>` : `<p ${keyed('help.services.debrid_link.good_know.provider_additional_settings_hidden')}></p>`}
            ${c.admin ? `<p ${keyed('help.services.debrid_link.if_fails')}></p>` : ''}`)}

          ${accordion('help.services.premiumize.title', `
            <p ${keyed('help.services.premiumize.for', {file_type: '.torrent'})}></p>
            ${c.admin ? `<p ${keyed('help.services.premiumize.need')}></p>` : ''}
            <p ${keyed('help.services.premiumize.what_happens')}></p>
            ${adminSees('provider-additional-settings') ? `<p ${keyed('help.services.premiumize.good_know.provider_additional_settings_shown')}></p>` : `<p ${keyed('help.services.premiumize.good_know.provider_additional_settings_hidden')}></p>`}
            ${c.admin ? `<p ${keyed('help.services.premiumize.premiumize_news_server')}></p>` : ''}
            ${c.admin ? `<p ${keyed('help.services.premiumize.if_fails')}></p>` : ''}`)}

          ${accordion('help.services.real_debrid.title', `
            <p ${keyed('help.services.real_debrid.for', {file_type: '.torrent'})}></p>
            ${c.admin ? `<p ${keyed('help.services.real_debrid.need')}></p>` : ''}
            <p ${keyed('help.services.real_debrid.what_happens')}></p>
            ${adminSees('provider-additional-settings') ? `<p ${keyed('help.services.real_debrid.good_know.provider_additional_settings_shown')}></p>` : `<p ${keyed('help.services.real_debrid.good_know.provider_additional_settings_hidden')}></p>`}
            ${c.admin ? `<p ${keyed('help.services.real_debrid.if_fails')}></p>` : ''}`)}

          ${accordion('help.services.torbox.title', `
            <p ${keyed('help.services.torbox.for', {file_type: '.torrent'})}></p>
            ${c.admin ? `<p ${keyed('help.services.torbox.need')}></p>` : ''}
            <p ${keyed('help.services.torbox.what_happens')}></p>
            ${adminSees('provider-additional-settings') ? `<p ${keyed('help.services.torbox.plan_matters.provider_additional_settings_shown')}></p>` : `<p ${keyed('help.services.torbox.plan_matters.provider_additional_settings_hidden')}></p>`}
            <p ${keyed('help.services.torbox.good_know')}></p>`)}

          ${accordion('help.services.usenet.title', `
            <p ${keyed('help.services.usenet.for')}></p>
            ${c.admin ? `<p ${keyed('help.services.usenet.need')}></p>` : ''}
            <p ${keyed('help.services.usenet.what_happens')}></p>
            ${adminSees('transfer-method-settings') ? `<p ${keyed('help.services.usenet.good_know.transfer_method_settings_shown')}></p>` : c.admin ? `<p ${keyed('help.services.usenet.good_know.admin')}></p>` : `<p ${keyed('help.services.usenet.good_know.member')}></p>`}
            ${c.admin ? `<p ${keyed('help.services.usenet.if_fails')}></p>` : ''}`)}
        </div>

        <h3 ${keyed('help.services.group.network_sources')}></h3>
        <div class="dp-help-copy dp-help-prose">
          ${c.admin ? `${c.shows('transfer-method-settings') ? `<p ${keyed('help.services.network_sources.all_network_sources.transfer_method_settings_shown')}></p>` : `<p ${keyed('help.services.network_sources.all_network_sources.transfer_method_settings_hidden')}></p>`}`
            : `<p ${keyed('help.services.network_sources.all_network_sources.member')}></p>`}
        </div>
        <div class="dp-help-accordion-list">
          ${accordion('help.services.https.title', `
            ${adminSees('transfer-method-settings') ? `<p ${keyed('help.services.https.ordinary_web_links.transfer_method_settings_shown', {link_2: 'https://', link_1: 'http://'})}></p>` : `<p ${keyed('help.services.https.ordinary_web_links.transfer_method_settings_hidden', {link_2: 'https://', link_1: 'http://'})}></p>`}`)}

          ${accordion('help.services.sftp.title', `
            <p ${keyed('help.services.sftp.ftp_sftp_links', {link_2: 'sftp://', link_1: 'ftp://'})}></p>`)}

          ${accordion('help.services.scp.title', `
            <p ${keyed('help.services.scp.scp_ssh_links', {link_2: 'ssh://', link_3: 'scp://host/path/file', link_4: 'ssh://host:2222/path/folder/', link_5: 'scp://host/path/*.iso', literal_2: '?', path_2: '~name', link_1: 'scp://', literal_1: '*', path_1: '~/'})}></p>
            <p ${keyed('help.services.scp.files_downloaded_over')}></p>`)}

          ${accordion('help.services.rsync.title', `
            <p ${keyed('help.services.rsync.rsync_host_path', {link_2: 'rsync://host/', link_3: 'rsync+ssh://host/path', link_1: 'rsync://host/path'})}></p>
            <p ${keyed('help.services.rsync.folder_includes_subfolders')}></p>
            ${adminSees('transfer-method-settings') ? `<p ${keyed('help.services.rsync.over_ssh_server.transfer_method_settings_shown')}></p>` : `<p ${keyed('help.services.rsync.over_ssh_server.transfer_method_settings_hidden')}></p>`}`)}

          ${accordion('help.services.webdav.title', `
            <p ${keyed('help.services.webdav.webdavs_davs_links', {link_2: 'davs://', link_3: 'webdav://', link_4: 'dav://', link_5: 'http://', link_6: 'https://', path: '/', literal: '?', link_1: 'webdavs://'})}></p>
            ${adminSees('transfer-method-settings') ? `<p ${keyed('help.services.webdav.folder_opens_file.transfer_method_settings_shown')}></p>` : `<p ${keyed('help.services.webdav.folder_opens_file.transfer_method_settings_hidden')}></p>`}`)}

          ${accordion('help.services.metalink_multimeta.title', `
            <p ${keyed('help.services.metalink_multimeta.reads_metalink_files', {file_type_2: '.meta4', file_type_1: '.meta4'})}></p>`)}

          ${accordion('help.services.media_downloads.title', `
            <p ${keyed('help.services.media_downloads.downloads_video_audio')}></p>
            <ul>
              <li ${keyed('help.services.media_downloads.what_get', {literal: 'Title [id]'})}></li>
              ${adminSees('transfer-method-settings') ? `<li ${keyed('help.services.media_downloads.choices.transfer_method_settings_shown')}></li>`
                : `<li ${keyed('help.services.media_downloads.choices.transfer_method_settings_hidden')}></li>`}
              <li ${keyed('help.services.media_downloads.playlists')}></li>
              <li ${keyed('help.services.media_downloads.not_supported')}></li>
              <li ${keyed('help.services.media_downloads.good_know')}></li>
            </ul>`)}
        </div>
      </section>`;
  }

  function enginesPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-engines-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-engines-heading" ${keyed('help.engines.title')}></h2>
          <p ${keyed('help.engines.summary')}></p>
        </div>

        <div class="dp-help-copy dp-help-copy--lead dp-help-prose">
          <p ${keyed('help.engines.lead.service_decides_where')}></p>
        </div>

        <div class="dp-help-integration-grid">
          ${inset('help.engines.aria2.title', `
            <p ${keyed('help.engines.aria2.downloads_web_http')}></p>
            <p ${keyed('help.engines.aria2.admin_sidebar_shows')}></p>`)}
          ${inset('help.services.rsync.title', `
            <p ${keyed('help.engines.rsync.downloads_rsync_links')}></p>`)}
          ${inset('help.engines.usenet_sabnzbd.title', `
            <p ${keyed('help.engines.usenet_sabnzbd.private_copy_sabnzbd')}></p>`)}
          ${inset('help.engines.media_downloads_yt.title', `
            <p ${keyed('help.engines.media_downloads_yt.downloads_media_supported')}></p>`)}
        </div>

        ${inset('help.engines.limits_apply_every.title', `
          <ul>
            <li ${keyed('help.engines.limits_apply_every.maximum_concurrent_downloads')}></li>
            <li ${keyed('help.engines.limits_apply_every.speed_cap_header')}></li>
            ${adminSees('disk-space-recovery') ? `<li ${keyed('help.engines.limits_apply_every.disk_space_recovery')}></li>`
              : `<li ${keyed('help.engines.limits_apply_every.free_space_guard')}></li>`}
          </ul>`)}

        ${adminSees('transfer-method-settings') || adminSees('download-engine-activity') ? `<div class="dp-help-accordion-list">
          ${adminSees('transfer-method-settings') ? accordion('help.engines.tuning_aria2_transfer.title', `
            <ul>
              <li ${keyed('help.engines.tuning_aria2_transfer.connections_per_server')}></li>
              <li ${keyed('help.engines.tuning_aria2_transfer.minimum_split_size')}></li>
              <li ${keyed('help.engines.tuning_aria2_transfer.continue_partial_downloads')}></li>
              <li ${keyed('help.engines.tuning_aria2_transfer.lowest_speed_limit', {literal: '0'})}></li>
              <li ${keyed('help.engines.tuning_aria2_transfer.disk_cache')}></li>
              <li ${keyed('help.engines.tuning_aria2_transfer.file_allocation')}></li>
            </ul>
            <p ${keyed('help.engines.tuning_aria2_transfer.more_connections_do')}></p>`, true) : ''}

          ${adminSees('transfer-method-settings') ? accordion('help.engines.tuning_rsync_usenet.title', `
            <ul>
              <li ${keyed('help.engines.tuning_rsync_usenet.rsync')}></li>
              <li ${keyed('help.engines.tuning_rsync_usenet.usenet')}></li>
              <li ${keyed('help.engines.tuning_rsync_usenet.media_downloads')}></li>
            </ul>`) : ''}

          ${adminSees('download-engine-activity') ? accordion('help.engines.download_engine_activity.title', `
            <p ${keyed('help.engines.download_engine_activity.settings_downloads_download_engine')}></p>`) : ''}
        </div>` : ''}
      </section>`;
  }

  function settingsPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-settings-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-settings-heading" ${keyed('help.settings.title')}></h2>
          ${c.server && c.advanced ? `<p ${keyed('help.settings.summary.server_advanced')}></p>`
            : `${c.standalone ? `${c.easy ? `<p ${keyed('help.settings.summary.standalone_easy')}></p>` : `<p ${keyed('help.settings.summary.standalone_advanced')}></p>`}` : `${c.easy ? `<p ${keyed('help.settings.summary.server_easy')}></p>` : `<p ${keyed('help.settings.summary.server_advanced')}></p>`}`}`}
        </div>

        ${inset('help.settings.how_saving_works.title', `
          <p ${keyed('help.settings.how_saving_works.there_no_save')}></p>
          <p ${keyed('help.settings.how_saving_works.stored_secrets_such')}></p>
          <p ${keyed('help.settings.how_saving_works.test')}></p>
          <p ${keyed('help.settings.how_saving_works.every_card_has')}></p>`)}

        <div class="dp-help-accordion-list">
          ${accordion('help.settings.services.title', `
            <ul>
              ${c.shows('provider-additional-settings') ? `<li ${keyed('help.settings.services.premium_services.provider_additional_settings_shown')}></li>`
                : `<li ${keyed('help.settings.services.premium_services.provider_additional_settings_hidden')}></li>`}
              <li ${keyed('help.settings.services.network_sources')}></li>
            </ul>
            <p ${keyed('help.settings.services.services_tab_manual')}></p>`, true)}

          ${accordion('help.settings.downloads.title', `
            <ul>
${c.shows('download-behavior-advanced') ? `              <li ${keyed('help.settings.downloads.download_behavior_limits.download_behavior_advanced_shown')}></li>
              <li ${keyed('help.settings.downloads.transfer_method_settings')}></li>
              <li ${keyed('help.settings.downloads.disk_space_recovery')}></li>
              <li ${keyed('help.settings.downloads.download_engine_activity')}></li>`
              : `<li ${keyed('help.settings.downloads.download_behavior_limits.download_behavior_advanced_hidden')}></li>
              <li ${keyed('help.settings.downloads.rest_tab_advanced')}></li>`}
            </ul>`)}

          ${accordion('help.settings.extraction.title', `
            <p ${keyed('help.settings.extraction.automatic_extraction_card')}></p>
            <ul>
              <li ${keyed('help.settings.extraction.concurrent_extractions')}></li>
              <li ${keyed('help.settings.extraction.delete_archives_after_extraction')}></li>
              <li ${keyed('help.settings.extraction.archive_passwords')}></li>
            </ul>
            <p ${keyed('help.settings.extraction.supported_formats_include')}></p>`)}

          ${c.standalone ? accordion('help.settings.signing.title', `
            <p ${keyed('help.settings.signing.standalone_installation_has')}></p>
            <p ${keyed('help.settings.signing.when_sign_prometheus', {path: '/api/metrics'})}></p>`)
            : c.easy ? accordion('help.settings.authentication.title', `
            <ul>
              <li ${keyed('help.settings.authentication.authentication_status')}></li>
              <li ${keyed('help.settings.authentication.turning_username_password')}></li>
            </ul>
            <p ${keyed('help.settings.authentication.when_sign_prometheus.easy', {path: '/api/metrics'})}></p>`)
            : ''}
          ${c.server && c.advanced ? accordion('help.settings.authentication.title', `
            <ul>
              <li ${keyed('help.settings.authentication.authentication_status')}></li>
              <li ${keyed('help.settings.authentication.username_password')}></li>
              <li ${keyed('help.settings.authentication.openid_connect', {literal_2: 'email_verified=true', literal_3: 'PUBLIC_BASE_URL', literal_1: 'issuer|subject-id'})}></li>
            </ul>
            <p ${keyed('help.settings.authentication.turning_off_last')}></p>
            <p ${keyed('help.settings.authentication.when_sign_prometheus.server_advanced', {path: '/api/metrics'})}></p>`) : ''}

          ${c.standalone ? accordion('help.settings.access_integrations.title', `
            ${c.shows('deployment-type') ? `<p ${keyed('help.settings.access_integrations.deployment_type')}></p>` : ''}
            <p ${keyed('help.settings.access_integrations.only_built_administrator')}></p>
            <p ${keyed('help.settings.access_integrations.integration')}></p>
            <p ${keyed('help.settings.access_integrations.integration_accounts', {literal: 'Authorization: Bearer <token>'})}></p>
            <p ${keyed('help.settings.access_integrations.integration_delivery_profiles')}></p>
            <p ${keyed('help.settings.access_integrations.administrator_changes_display')}></p>`) : ''}
          ${c.server ? accordion('help.settings.users_access.title', `
            <p ${keyed('help.settings.users_access.every_account_has')}></p>
            <ul>
              <li ${keyed('help.settings.users_access.admin')}></li>
              <li ${keyed('help.settings.users_access.user')}></li>
              <li ${keyed('help.settings.users_access.readonly_user')}></li>
              <li ${keyed('help.settings.users_access.integration')}></li>
            </ul>
            <p ${keyed('help.settings.users_access.help_button_user')}></p>
            <p ${keyed('help.settings.users_access.user_accounts')}></p>
            <p ${keyed('help.settings.access_integrations.integration_accounts', {literal: 'Authorization: Bearer <token>'})}></p>
            <p ${keyed('help.settings.access_integrations.integration_delivery_profiles')}></p>
            <p ${keyed('help.settings.users_access.anyone_signed_can')}></p>            ${c.shows('deployment-type') ? `<p ${keyed('help.settings.users_access.deployment_type')}></p>` : ''}
            <p ${keyed('help.settings.users_access.my_account_also')}></p>`) : ''}

          ${accordion('help.settings.notifications.title', `
            <ul>
              <li ${keyed('help.settings.notifications.webhook_notifications')}></li>
              ${c.shows('statistics-reporting') ? `<li ${keyed('help.settings.notifications.statistics_reporting.statistics_reporting_shown')}></li>`
                : `<li ${keyed('help.settings.notifications.statistics_reporting.statistics_reporting_hidden')}></li>`}
            </ul>
            ${c.server ? `<p ${keyed('help.settings.notifications.user_has_their')}></p>` : ''}
            <p ${keyed('help.settings.notifications.uploaded_avatar_served')}></p>`)}

          ${c.easy ? accordion('help.settings.data_maintenance.title', `
            <ul>
              <li ${keyed('help.settings.data_maintenance.backups_retention.easy')}></li>
              <li ${keyed('help.settings.data_maintenance.backups')}></li>
              <li ${keyed('help.settings.data_maintenance.events_keep_being')}></li>
            </ul>`) : ''}
          ${c.advanced ? accordion('help.settings.data_maintenance.title', `
            <ul>
              <li ${keyed('help.settings.data_maintenance.event_logging')}></li>
              <li ${keyed('help.settings.data_maintenance.backups_retention.advanced')}></li>
              <li ${keyed('help.settings.data_maintenance.backups')}></li>
              <li ${keyed('help.settings.data_maintenance.database_reset_controls', {literal: 'WIPE'})}></li>
            </ul>`) : ''}
        </div>
      </section>`;
  }

  function troubleshootingPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-trouble-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-trouble-heading" ${keyed('help.trouble.title')}></h2>
          <p ${keyed('help.trouble.summary')}></p>
        </div>

        ${inset('help.trouble.before_changing_anything.title', `
          ${adminSees('activity-log') ? `<p ${keyed('help.trouble.before_changing_anything.open_download_details.activity_log_shown')}></p>` : `<p ${keyed('help.trouble.before_changing_anything.open_download_details.activity_log_hidden')}></p>`}
          ${c.admin ? `<p ${keyed('help.trouble.before_changing_anything.avoid_removing_download.admin')}></p>`
            : `${c.operate ? `<p ${keyed('help.trouble.before_changing_anything.avoid_removing_download.operate')}></p>` : `<p ${keyed('help.trouble.before_changing_anything.if_problem_needs')}></p>`}`}`)}

        <div class="dp-help-accordion-list">
          ${accordion('help.trouble.button_page_setting.title', `
            ${c.easy ? `<p ${keyed('help.trouble.button_page_setting.presentation_easy_which')}></p>` : ''}
            ${c.standalone ? `<p ${keyed('help.trouble.button_page_setting.standalone_installation_only')}></p>`
              : `<p ${keyed('help.trouble.button_page_setting.most_likely_role')}></p>`}`, true)}

          ${accordion('help.trouble.i_added_something.title', `
            <ul>
              ${c.admin ? `<li ${keyed('help.trouble.i_added_something.check_pause_all')}></li>`
                : `<li ${keyed('help.trouble.i_added_something.processing_may_paused')}></li>`}
              <li ${keyed('help.trouble.i_added_something.queued')}></li>
              ${c.submit ? `<li ${keyed('help.trouble.i_added_something.input_required.submit')}></li>`
                : `<li ${keyed('help.trouble.i_added_something.input_required.viewer')}></li>`}
              ${c.admin ? `<li ${keyed('help.trouble.i_added_something.waiting_provider.admin')}></li>` : `<li ${keyed('help.trouble.i_added_something.waiting_provider.member')}></li>`}
              <li ${keyed('help.trouble.i_added_something.waiting_storage')}></li>
            </ul>`)}

          ${c.submit ? accordion('help.trouble.quick_add_refuses.title', `
            <ul>
              <li ${keyed('help.trouble.quick_add_refuses.line_n_enter_https')}></li>
              <li ${keyed('help.trouble.quick_add_refuses.batch_more_than')}></li>
              <li ${keyed('help.trouble.quick_add_refuses.local_network_transfer_not')}></li>
              <li ${keyed('help.trouble.quick_add_refuses.file_over_size', {file_type_2: '.meta4', file_type_3: '.nzb', file_type_1: '.torrent'})}></li>
              <li ${keyed('help.trouble.quick_add_refuses.link_list_points')}></li>
            </ul>`) : ''}

          ${c.admin ? accordion('help.trouble.service_amber_red.title', `
            <ul>
              <li ${keyed('help.trouble.service_amber_red.api_key_services')}></li>
              <li ${keyed('help.trouble.service_amber_red.real_debrid_torbox')}></li>
              <li ${keyed('help.trouble.service_amber_red.usenet')}></li>
              <li ${keyed('help.trouble.service_amber_red.amber_after_plan')}></li>
              <li ${keyed('help.trouble.service_amber_red.finish_remove_existing_resources')}></li>
            </ul>`) : ''}

          ${accordion('help.trouble.download_stays_processing.title', `
            ${c.operate ? `<p ${keyed('help.trouble.download_stays_processing.torrent_may_take.operate')}></p>` : `<p ${keyed('help.trouble.download_stays_processing.torrent_may_take.viewer')}></p>`}
${adminSees('download-behavior-advanced') ? `<p ${keyed('help.trouble.download_stays_processing.timing_lives_under')}></p>` : ''}`)}

          ${accordion('help.trouble.download_slow_stalls.title', `
            <ul>
              <li ${keyed('help.trouble.download_slow_stalls.check_speed_cap')}></li>
              <li ${keyed('help.trouble.download_slow_stalls.read_error_details')}></li>
              ${c.operate ? `<li ${keyed('help.trouble.download_slow_stalls.if_other_sources')}></li>` : ''}
              ${c.admin ? `<li ${keyed('help.trouble.download_slow_stalls.if_sidebar_shows')}></li>` : ''}
              ${adminSees('download-engine-activity') ? `<li ${keyed('help.trouble.download_slow_stalls.job_truly_stuck')}></li>` : ''}
            </ul>`)}

          ${c.submit ? accordion('help.trouble.server_keeps_asking.title', `
            <p ${keyed('help.trouble.server_keeps_asking.rejected_password_never')}></p>`) : ''}

          ${c.admin ? accordion('help.trouble.usenet_download_fails.title', `
            <ul>
              <li ${keyed('help.trouble.usenet_download_fails.usenet_no_enabled_news')}></li>
              <li ${keyed('help.trouble.usenet_download_fails.if_posting_incomplete')}></li>
              <li ${keyed('help.trouble.usenet_download_fails.passworded_7z_rar')}></li>
            </ul>`) : ''}

          ${accordion('help.trouble.media_download_fails.title', `
            <p ${keyed('help.trouble.media_download_fails.media_downloads_cannot')}></p>`)}

          ${c.admin ? accordion('help.trouble.files_not_where.title', `
            <ul>
              <li ${keyed('help.trouble.files_not_where.check_download_folder')}></li>
              <li ${keyed('help.trouble.files_not_where.debridpulse_must_allowed')}></li>
              ${c.shows('disk-space-recovery') ? `<li ${keyed('help.trouble.files_not_where.when_free_space.disk_space_recovery_shown')}></li>` : `<li ${keyed('help.trouble.files_not_where.when_free_space.disk_space_recovery_hidden')}></li>`}
            </ul>`) : ''}

          ${c.admin ? accordion('help.trouble.automatic_extraction_did.title', `
            <p ${keyed('help.trouble.automatic_extraction_did.check_settings_extraction')}></p>`) : ''}

          ${c.admin ? accordion('help.trouble.webhook_notifications_prometheus.title', `
            ${c.standalone ? `<p ${keyed('help.trouble.webhook_notifications_prometheus.webhook_notifications_press.standalone', {path: '/api/metrics'})}></p>` : `<p ${keyed('help.trouble.webhook_notifications_prometheus.webhook_notifications_press.server', {path: '/api/metrics'})}></p>`}`)
            : c.personal ? accordion('help.trouble.my_webhook_notifications.title', `
            <p ${keyed('help.trouble.my_webhook_notifications.notifications_page_press')}></p>`) : ''}

          ${accordion('help.trouble.page_stops_updating.title', `
            <p ${keyed('help.trouble.page_stops_updating.debridpulse_sends_live')}></p>`)}

          ${c.standalone ? accordion('help.trouble.i_cannot_sign.title', `
            <p ${keyed('help.trouble.i_cannot_sign.standalone_installation_signs')}></p>
            <p ${keyed('help.trouble.i_cannot_sign.if_password_lost', {path: '/app/config/config.json', literal: 'auth_password'})}></p>`)
            : c.admin ? accordion('help.trouble.i_cannot_sign.title', `
            <p ${keyed('help.trouble.i_cannot_sign.if_another_sign')}></p>
            ${c.easy ? `<p ${keyed('help.trouble.i_cannot_sign.username_password_openid')}></p>` : ''}
            <p ${keyed('help.trouble.i_cannot_sign.last_resort_if_no', {path: '/app/config/config.json', literal_2: 'auth_oidc_enabled', literal_3: 'false', literal_1: 'auth_password_enabled'})}></p>`)
            : accordion('help.trouble.i_cannot_sign.title', `
            <p ${keyed('help.trouble.i_cannot_sign.sign_methods_accounts')}></p>`)}

          ${c.admin ? accordion('help.trouble.backup_restore_reset.title', `
            <ul>
              <li ${keyed('help.trouble.backup_restore_reset.backup_disabled_settings')}></li>
              ${c.shows('database-reset') ? `<li ${keyed('help.trouble.backup_restore_reset.reset_needs_allow', {literal: 'WIPE'})}></li>` : ''}
              <li ${keyed('help.trouble.backup_restore_reset.if_safety_backup')}></li>
              <li ${keyed('help.trouble.backup_restore_reset.if_restore_could')}></li>
            </ul>`) : ''}
        </div>
      </section>`;
  }

  function licensePanel() {
    return `
      <section class="dp-help-document dp-help-license" aria-labelledby="dp-help-license-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-license-heading" ${keyed('help.license.title')}></h2>
          <p ${keyed('help.license.summary')}></p>
        </div>

        <div class="dp-help-copy dp-help-copy--lead dp-help-prose">
          <p ${keyed('help.license.lead.copyright', {notice: 'Copyright © 2026 Chris Moore', literal: 'GPL-2.0-or-later'})}></p>
          <p ${keyed('help.license.lead.gpl_permits_use')}></p>
          <p class="dp-help-license-warranty" ${keyed('help.license.lead.debridpulse_provided_without_warranty')}></p>
        </div>

        <article class="dp-help-inset">
          <h3 ${keyed('help.license.group.documents_can_open_here')}></h3>
          <div class="dp-help-copy dp-help-prose">
            <ul>
              <li ${keyed('help.license.documents_can_open.gpl_2_0_later')}></li>
              <li ${keyed('help.license.documents_can_open.source_offer')}></li>
              <li ${keyed('help.license.documents_can_open.third_party_licenses')}></li>
            </ul>
          </div>
        </article>

        <div class="dp-help-license-actions">
          <button type="button" class="dp-btn dp-btn--primary dp-help-local-document-button" data-legal-document="gpl" ${keyed('help.license.documents.gpl_open')}></button>
          <button type="button" class="dp-btn dp-btn--ghost dp-help-local-document-button" data-legal-document="source-offer" ${keyed('help.license.documents.source_offer_open')}></button>
          <button type="button" class="dp-btn dp-btn--ghost dp-help-local-document-button" data-legal-document="third-party" ${keyed('help.license.documents.third_party_open')}></button>
        </div>

        <div class="dp-help-copy dp-help-license-note dp-help-prose">
          <p ${keyed('help.license.documents.those_buttons_open')}></p>
          <p ${keyed('help.license.documents.complete_license_notices')}></p>
        </div>
      </section>`;
  }

  /* Quick Start for a person who is not an Admin: what their role does, in
   * the order they meet it. */
  function memberQuickStartPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-quickstart-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-quickstart-heading" ${keyed('help.quickstart.title')}></h2>
          ${c.submit ? `<p ${keyed('help.quickstart.summary.submit')}></p>` : `<p ${keyed('help.quickstart.summary.viewer')}></p>`}
        </div>

        <div class="dp-help-copy dp-help-copy--lead dp-help-prose">
          ${c.submit ? `<p ${keyed('help.quickstart.lead.debridpulse_application_submits_routes.submit')}></p>` : `<p ${keyed('help.quickstart.lead.debridpulse_application_submits_routes.viewer')}></p>`}
        </div>

        <div class="dp-help-steps">
          ${c.submit ? `
          ${step(1, 'help.quickstart.add_first_download.title', `
            <div class="dp-help-prose">
              <p ${keyed('help.quickstart.add_first_download.dashboard_paste_link', {file_type_2: '.nzb', file_type_3: '.meta4', file_type_1: '.torrent'})}></p>
              <p ${keyed('help.quickstart.add_first_download.if_download_contains')}></p>
            </div>`)}
          ${step(2, 'help.quickstart.follow_finish.title', `
            <div class="dp-help-prose">
              <p ${keyed('help.quickstart.follow_finish.download_appears_download.submit')}></p>
            </div>`)}
          ${c.personal ? step(3, 'help.quickstart.hear_about.title', `
            <div class="dp-help-prose">
              ${c.shows('statistics-reporting') ? `<p ${keyed('help.quickstart.hear_about.notifications.statistics_reporting_shown')}></p>` : `<p ${keyed('help.quickstart.hear_about.notifications.statistics_reporting_hidden')}></p>`}
            </div>`) : ''}` : `
          ${step(1, 'help.quickstart.see_what_happening.title', `
            <div class="dp-help-prose">
              <p ${keyed('help.quickstart.see_what_happening.dashboard_shows_downloads')}></p>
            </div>`)}
          ${step(2, 'help.quickstart.open_download.title', `
            <div class="dp-help-prose">
              <p ${keyed('help.quickstart.open_download.click_any_download')}></p>
            </div>`)}
          ${step(3, 'help.quickstart.look_back.title', `
            <div class="dp-help-prose">
              <p ${keyed('help.quickstart.look_back.statistics')}></p>
            </div>`)}`}
        </div>

        ${inset('help.quickstart.where_read_next.title', `
          <ul>
            <li ${keyed('help.quickstart.where_read_next.everyday_use')}></li>
            <li ${keyed('help.quickstart.where_read_next.how_works')}></li>
            <li ${keyed('help.quickstart.where_read_next.services')}></li>
            ${c.personal ? `<li ${keyed('help.quickstart.where_read_next.notifications')}></li>` : ''}
          </ul>
          ${c.submit ? `<p ${keyed('help.quickstart.where_read_next.settings_controls_marked')}></p>` : `<p ${keyed('help.quickstart.where_read_next.changes_downloads_made')}></p>`}`)}
      </section>`;
  }

  /* A User's own Notifications, in the place of the Settings reference. */
  function personalNotificationsPanel() {
    return `
      <section class="dp-help-document" aria-labelledby="dp-help-notifications-heading">
        <div class="dp-help-section-heading">
          <h2 id="dp-help-notifications-heading" ${keyed('help.notifications.title')}></h2>
          <p ${keyed('help.notifications.summary')}></p>
        </div>

        ${inset('help.settings.how_saving_works.title', `
          <p ${keyed('help.notifications.how_saving_works.there_no_save')}></p>
          <p ${keyed('help.notifications.how_saving_works.each_card_has')}></p>`)}

        <div class="dp-help-accordion-list">
          ${accordion('help.notifications.webhook_notifications.title', `
            <p ${keyed('help.notifications.webhook_notifications.alerts_about_downloads')}></p>
            <ul>
              <li ${keyed('help.notifications.webhook_notifications.enable')}></li>
              <li ${keyed('help.notifications.webhook_notifications.primary_webhook')}></li>
              <li ${keyed('help.notifications.webhook_notifications.display_name')}></li>
              <li ${keyed('help.notifications.webhook_notifications.choose_which_events')}></li>
              <li ${keyed('help.notifications.webhook_notifications.test')}></li>
            </ul>`, true)}
          ${c.shows('statistics-reporting') ? accordion('help.notifications.statistics_reporting.title', `
            <p ${keyed('help.notifications.statistics_reporting.periodic_summary_download')}></p>
            <ul>
              <li ${keyed('help.notifications.statistics_reporting.report_window')}></li>
              <li ${keyed('help.notifications.statistics_reporting.automatic_report_interval')}></li>
              <li ${keyed('help.notifications.statistics_reporting.test')}></li>
            </ul>`)
            : accordion('help.notifications.statistics_reports.title', `
            <p ${keyed('help.notifications.statistics_reports.statistics_reports_previously')}></p>`)}
        </div>
      </section>`;
  }

  function canonicalDocumentMarkup(markup) {
    const template = document.createElement('template');
    template.innerHTML = String(markup || '').trim();
    const section = template.content.firstElementChild;
    if (!section || !section.classList.contains('dp-help-document')) return markup;

    section.classList.add('card', 'dp-help-section-card', 'dp-large-panel-surface');
    const heading = section.querySelector(':scope > .dp-help-section-heading');
    if (!heading) return template.innerHTML;

    const header = document.createElement('div');
    header.className = 'card-header dp-help-section-card-header';
    heading.before(header);
    header.appendChild(heading);

    const body = document.createElement('div');
    body.className = 'card-body dp-help-section-card-body';
    while (header.nextSibling) body.appendChild(header.nextSibling);
    section.appendChild(body);
    return template.innerHTML;
  }

  function panel(name, body) {
    const active = state.activeTab === name;
    return `
      <section class="dp-help-panel" id="dp-help-panel-${name}" data-panel="${name}" role="tabpanel"
               aria-labelledby="dp-help-tab-${name}" ${active ? '' : 'hidden'}>
        ${canonicalDocumentMarkup(body)}
      </section>`;
  }

  function activateTab(name, focus = false) {
    selectTabControl(name, focus);
    showTabPanel();
  }

  // A tab name, or Quick Start when it names no section this reader has.
  const tabName = name => (eligibleTabs().some(([id]) => id === name) ? name : 'quickstart');

  /* Select a tab at once; `motion` ({direction, distance}, the choice's own)
   * slides the compact navigator's window when the selection moves it. */
  function selectTabControl(name, focus = false, motion = null) {
    name = tabName(name);
    state.activeTab = name;

    const tabs = Array.from(root()?.querySelectorAll('.dp-help-tab:not(.dp-motion-still *)') || []);
    const selected = tabs.find(tab => tab.dataset.tab === name) || null;
    // The navigator marks it selected when it places it: after copying the
    // window that leaves, where a slide shows the choice.
    navigation.place(motion, {tab: selected, apply: () => tabs.forEach(tab => {
      const active = tab.dataset.tab === name;
      tab.classList.toggle('is-active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
      tab.tabIndex = active ? 0 : -1;
    })});
    // (Where the navigator places its own window, focus never scrolls it.)
    if (focus) selected?.focus({preventScroll: navigation.placing()});
  }

  // The panel of the tab selected NOW (a later choice made meanwhile wins).
  function showTabPanel() {
    const view = root();
    if (!view) return;
    view.querySelectorAll('.dp-help-panel:not(.dp-motion-still *)').forEach(section => {
      section.hidden = section.dataset.panel !== state.activeTab;
    });

    const scroller = view.querySelector('.dp-help-scroll');
    if (scroller) scroller.scrollTop = 0;
  }

  /* The operator chose a section (`via` 'tab', or 'travel' through Previous
   * / Next): the strip changes at once and the panel slides across, by the
   * tabs' displayed order. */
  function chooseTab(name, focus = false, via = 'tab') {
    const view = root();
    const order = Array.from(view?.querySelectorAll('.dp-help-tab:not(.dp-motion-still *)') || []).map(tab => tab.dataset.tab);
    // From the section chosen last -- what a slide still under way will
    // show -- so a quick return turns back.
    const from = order.indexOf(state.activeTab), to = order.indexOf(tabName(name));
    const step = from >= 0 && to >= 0 ? to - from : 0;
    // The compact navigator's window, when it moves, slides the same way.
    selectTabControl(name, focus, {direction: Math.sign(step), distance: Math.abs(step), via});
    if (window.DPMotion) window.DPMotion.slide({element: view?.querySelector('.dp-help-scroll'), axis: 'x', direction: Math.sign(step), distance: Math.abs(step)}, showTabPanel);
    else showTabPanel();
  }

  /* A Help accordion rolls its body down as it opens and up before it closes,
   * through the one disclosure motion owner (DPMotion.disclose) -- the same
   * way every Settings disclosure moves. The native <details> stays the
   * state: `open` is set before an opening and cleared only once the closing
   * motion has finished, so the body is never hidden before it has rolled up,
   * and a reversal mid-motion simply turns it around from where it is drawn.
   * Without the motion owner or the Web Animations API the click is left to
   * the browser, which opens and closes <details> at once. Reduced motion is
   * the motion owner's to honour: it settles at once. */
  const canAnimateDisclosure = () => !!window.DPMotion && typeof Element.prototype.animate === 'function';

  function toggleAccordion(details, summary) {
    const body = details.querySelector(':scope > .dp-help-accordion-body');
    if (!body) { details.open = !details.open; return; }
    if (!details.open || details.dataset.dpClosing === '1') {
      delete details.dataset.dpClosing;
      details.open = true;
      window.DPMotion.disclose(body, true, {animate: true});
      return;
    }
    details.dataset.dpClosing = '1';
    window.DPMotion.disclose(body, false, {animate: true, focusReturn: summary, onClosed: () => {
      if (details.dataset.dpClosing !== '1') return;
      delete details.dataset.dpClosing;
      details.open = false;
      body.hidden = false;           // a closed <details> hides it; ready for the next opening
    }});
  }

  function bindEvents(view) {
    if (view.dataset.dpHelpEventsBound === '1') return;
    view.dataset.dpHelpEventsBound = '1';

    // The page's width changed (window, sidebar, or the page shown again):
    // the header's navigation compares its measured widths with the new room.
    if (typeof ResizeObserver === 'function') new ResizeObserver(() => navigation.fit()).observe(view);

    view.addEventListener('click', event => {
      const summary = event.target.closest('.dp-help-accordion > summary');
      if (summary && canAnimateDisclosure()) {
        event.preventDefault();
        toggleAccordion(summary.parentElement, summary);
        return;
      }
      const tab = event.target.closest('.dp-help-tab');
      if (tab) {
        chooseTab(tab.dataset.tab);
        return;
      }
      // Previous / Next: one section from the selected one, through the same
      // choice. Focus stays on the action while it has somewhere to go, and
      // follows to the section once it has none (the action is then gone).
      const travel = event.target.closest('.dp-help-nav-end');
      if (!travel) return;
      const tabs = Array.from(view.querySelectorAll('.dp-help-tab:not(.dp-motion-still *)'));
      const index = tabs.findIndex(tab => tab.getAttribute('aria-selected') === 'true')
        + (travel.dataset.helpEnd === 'previous' ? -1 : 1);
      const target = tabs[index];
      if (target) {
        chooseTab(target.dataset.tab, document.activeElement === travel && (index === 0 || index === tabs.length - 1), 'travel');
      }
    });

    view.addEventListener('keydown', event => {
      const current = event.target.closest('.dp-help-tab');
      if (!current || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const tabs = Array.from(view.querySelectorAll('.dp-help-tab:not(.dp-motion-still *)'));
      let index = tabs.indexOf(current);
      if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = tabs.length - 1;
      else index = (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      chooseTab(tabs[index].dataset.tab, true);
    });
  }

  function render() {
    const view = root();
    if (!view) return;
    c = context();
    if (view.dataset.dpHelpRendered === signature(c)) {
      activateTab(state.activeTab);
      return;
    }

    view.classList.add('dp-help-clean-view');
    state.activeTab = tabName(state.activeTab);

    const tabs = eligibleTabs().map(([id, label, icon]) => {
      const active = state.activeTab === id;
      return `
        <button class="dp-tab dp-help-tab${active ? ' is-active' : ''}" id="dp-help-tab-${id}"
                type="button" role="tab" data-tab="${id}"
                aria-controls="dp-help-panel-${id}"
                aria-selected="${active ? 'true' : 'false'}"
                tabindex="${active ? '0' : '-1'}">
          <span class="dp-help-tab-chip" aria-hidden="true"><img class="dp-help-tab-glyph" src="/icons/lucide/${icon}.svg" alt=""></span>
          <span class="dp-help-tab-label" data-i18n="${label}"></span>
        </button>`;
    }).join('');
    // The compact navigator's two travel actions: actions, not tabs, each one
    // section from the selected one.
    const travel = (step, glyph, label) => `
      <button class="dp-help-nav-end dp-section-nav-end" type="button" data-help-end="${step}"
              data-i18n-attr="aria-label:${label}">
        ${window.DPIcons?.svg?.(glyph) || ''}
      </button>`;

    view.innerHTML = `
      <section class="dp-card dp-help-master-card dp-list-workspace-surface" data-i18n-attr="aria-label:help.card_label">
        <header class="dp-card__header dp-help-master-header">
          <div class="dp-help-header-copy">
            <img class="dp-help-title-icon" src="/icons/dp/document.svg" alt="" aria-hidden="true">
            <div class="dp-help-header-text">
              <div class="dp-help-header-title">${t('help.header.title')}</div>
              <div class="dp-help-header-subtitle">${t('help.header.hint')}</div>
            </div>
          </div>
          <div class="dp-help-nav dp-section-nav">
            <div class="dp-help-nav-control dp-section-nav-control">
              ${travel('previous', 'chevronLeft', 'settings.nav.previous')}
              <div class="dp-tabs dp-help-tabs dp-section-nav-strip" role="tablist" data-i18n-attr="aria-label:a11y.help_sections">${tabs}</div>
              ${travel('next', 'chevronRight', 'settings.nav.next')}
            </div>
          </div>
        </header>
        <div class="dp-help-master-body">
          <div class="dp-help-scroll">
            <div class="dp-help-panels">
              ${panel('quickstart', quickStartPanel())}
              ${panel('using', usingPanel())}
              ${panel('howitworks', howItWorksPanel())}
              ${panel('services', servicesPanel())}
              ${panel('engines', enginesPanel())}
              ${eligibleTabs().some(([id]) => id === 'settings')
                ? panel('settings', c.admin ? settingsPanel() : personalNotificationsPanel()) : ''}
              ${panel('trouble', troubleshootingPanel())}
              ${panel('license', licensePanel())}
            </div>
          </div>
        </div>
      </section>`;

    // The composed manual says its words (ui-i18n.js).
    window.DPI18n.render(view);
    view.dataset.dpHelpRendered = signature(c);
    bindEvents(view);
    activateTab(state.activeTab);
    navigation.fit({measure: true});
    document.dispatchEvent(new CustomEvent('debridpulse:help-rendered', {detail: {view: 'help'}}));
  }

  // The one Help entry (app.js navigation): every entry opens on Quick Start.
  function load() {
    state.activeTab = 'quickstart';
    render();
  }

  // Fonts that finish loading change the tabs' and the copy's own widths.
  document.fonts?.addEventListener?.('loadingdone', () => navigation.fit({measure: true}));

  // The reader chose Easy or Advanced (auth.js) while Help is shown: compose
  // it again for that interface, on the same section where it still has it.
  // Shown later, its entry composes it (render compares the signature).
  document.addEventListener('debridpulse:presentation-changed', () => {
    const view = root();
    if (view?.dataset.dpHelpRendered && view.classList.contains('active')) render();
  });

  // The language changed: DPI18n.render() has already said every keyed word in
  // place (the section shown, an open accordion, the scroll and focus are
  // untouched, and nothing is composed again or read). The navigator measures
  // the new words: it fits them now, or, while Help is not shown, when it is.
  document.addEventListener('debridpulse:locale-changed', () => {
    if (root()?.dataset.dpHelpRendered) navigation.fit({measure: true});
  });

  // app.js owns generic navigation and calls this canonical Help entry point.
  window.loadHelp = load;
  try { loadHelp = load; } catch (_) {}
  window.DPHelpPage = Object.freeze({load, activateTab});
})();