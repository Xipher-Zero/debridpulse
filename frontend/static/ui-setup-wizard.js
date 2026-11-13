/* DebridPulse's setup wizard (window.DPSetupWizard): the ONE owner of the
 * wizard's presentation -- which pages the installation's initial setup or
 * an Admin's rerun shows, in which order, and how the operator moves through
 * them -- inside one dialog of the canonical dialog owner (ui-settings-modal.js).
 *
 * It owns no setting. Each page's Next commits that page's choice through the
 * choice's own owner -- the default language (PUT /api/settings/default-locale),
 * the deployment type (PUT /api/auth/deployment-type), the Download Folder
 * (DPSettingsPage.writeDocument, the one whole-settings write), the named
 * Administrator (POST /api/setup/initial/administrator) -- and only then
 * records where the wizard resumes (services.setup_lifecycle, the one
 * lifecycle owner): the server, never the browser, says whether setup is
 * finished and where it continues. Back moves between pages and commits
 * nothing; what an earlier Next committed stays committed.
 *
 * Two changes give the page a different authority, and the page is then
 * loaded again under it, as Settings does after a deployment change: a new
 * deployment type, and the named Administrator's own session replacing the
 * open one. The wizard opens again where the server says it resumes.
 *
 * The initial setup opens by itself for an Admin while the installation's
 * setup is unfinished, and cannot be dismissed; a rerun is opened from My
 * Account (auth.js), resumes where that Admin's own rerun stopped, and may be
 * closed at any page.
 */
(function () {
  'use strict';

  const t = (key, values) => window.DPI18n.t(key, values);
  // Said words (ui-settings-modal.js): a key that travels with the words, so
  // an open dialog is re-said in place on a language change.
  const said = (key, values = null) => ({key, values, toString: () => t(key, values || undefined)});
  const ROSTERS = Object.freeze({
    initial: Object.freeze({
      server: ['welcome', 'deployment', 'download_directory', 'administrator', 'premium_services', 'getting_started'],
      standalone: ['welcome', 'deployment', 'download_directory', 'premium_services', 'getting_started'],
    }),
    rerun: Object.freeze({
      server: ['welcome', 'download_directory', 'administrator', 'premium_services', 'getting_started'],
      standalone: ['welcome', 'download_directory', 'premium_services', 'getting_started'],
    }),
  });
  // The pages after a Server installation's Administrator exists.
  const AFTER_ADMINISTRATOR = new Set(['premium_services', 'getting_started']);
  // Each page's heading: its mark and the reading beneath its title (the
  // dialog owner's title icon and hint). Welcome's mark is DebridPulse's own
  // logo, drawn inline in the theme in force (<dp-logo>, ui-logo.css). The
  // reading is the page's short tagline -- personality, never instruction:
  // the body says what the page asks. Administrator says what it is now.
  const ICONS = Object.freeze({
    deployment: 'network', download_directory: 'folder-sync', administrator: 'shield-user',
    premium_services: 'crown', getting_started: 'rocket',
  });
  const HINTS = Object.freeze({
    welcome: 'setup.welcome.tagline', deployment: 'setup.deployment.tagline', download_directory: 'setup.directory.tagline',
    premium_services: 'setup.premium.tagline', getting_started: 'setup.getting_started.tagline',
  });
  const TITLES = Object.freeze({
    welcome: 'setup.welcome.title', deployment: 'setup.deployment.title',
    download_directory: 'setup.directory.title', premium_services: 'settings.services.premium',
    getting_started: 'setup.getting_started.title',
  });

  let active = null;

  const auth = () => window.debridPulseAuth;
  const deploymentType = () => (auth()?.authority?.deployment_type === 'standalone' ? 'standalone' : 'server');
  const signedIn = () => auth()?.authority?.authenticated === true;
  const request = (method, path, body, timeout = 15000) => window.api(method, path, body, timeout);

  function element(html) {
    const template = document.createElement('template');
    template.innerHTML = html.trim();
    return template.content.firstElementChild;
  }

  /* ── pages ────────────────────────────────────────────────────────────────
   * Each page: render(wizard) -> its section; commit(wizard) -> resolves once
   * its own choice is saved (throws to stay); optional `reloads` after Next. */

  const welcome = {
    render(wizard) {
      const picker = window.DPLanguagePicker;
      const offered = Boolean(wizard.data.locale && wizard.data.choices && picker);
      const section = element(`
        <section class="dp-setup-page dp-setup-welcome">
          <div class="dp-setup-copy">
            <p data-i18n="setup.welcome.body.thanks"></p>
            <p data-i18n="setup.welcome.body.guide"></p>
            <p data-i18n="setup.welcome.body.next"></p>
          </div>
          ${offered ? `
          <div class="dp-setup-language-row">
            <div class="dp-setup-language-copy">
              <p class="dp-setup-label" id="dp-setup-language-label" data-i18n="setup.welcome.change_language"></p>
              <p class="dp-setup-hint" id="dp-setup-language-hint" data-i18n="setup.welcome.change_language_hint"></p>
            </div>
            <button type="button" class="btn dp-language-button dp-setup-language-button" data-setup-language
              aria-describedby="dp-setup-language-hint"></button>
          </div>` : ''}
        </section>`);
      // TEMPORARY, development only (WS3b): a preview of the first-run setup
      // from an Admin's rerun, for refining its pages -- offered only where the
      // server says this is a development build (its release identity,
      // ``development_build``). Removed with the final wizard cleanup.
      if (wizard.mode === 'rerun' && !wizard.preview && wizard.data.development === true) {
        const entry = element(`
          <div class="dp-setup-preview-entry">
            <button type="button" class="btn btn-ghost btn-sm" data-setup-preview>Test First-Run Wizard</button>
          </div>`);
        entry.querySelector('[data-setup-preview]').addEventListener('click', () => { void previewInitial(wizard); });
        section.append(entry);
      }
      if (offered) {
        wizard.data.pending = wizard.data.pending || wizard.data.locale.configured || wizard.data.locale.locale;
        const button = section.querySelector('[data-setup-language]');
        void paintLanguageButton(wizard, button);
        button.addEventListener('click', () => openLanguage(wizard));
      }
      return section;
    },
    async commit(wizard) {
      const saved = wizard.data.locale;
      if (!saved) throw new Error(t('setup.error.language_unavailable'));
      const chosen = wizard.data.pending || saved.locale;
      if (chosen !== (saved.configured || saved.locale)) {
        wizard.data.locale = await request('PUT', '/settings/default-locale', {locale: chosen});
      }
      // The page shows what this account is shown in now: its own language,
      // if it has one, still wins over the default just saved.
      await wizard.showOwnLanguage();
    },
  };

  /* Welcome's language button: the language chosen on this page (not saved
   * until Next), as My Account's button shows one -- its flag and own name;
   * its accessible name says what it does and which language it holds. */
  async function paintLanguageButton(wizard, button) {
    const picker = window.DPLanguagePicker;
    if (!button || !picker) return;
    const shown = await picker.language(wizard.data.pending);
    if (!button.isConnected) return;
    picker.paintButton(button, shown, t('setup.welcome.language_button', {language: shown.native}));
  }

  // Each deployment type's generic mark (a Lucide glyph, DPIcons): one
  // person's workstation; a shared server.
  const DEPLOYMENT_GLYPHS = Object.freeze({standalone: 'monitor', server: 'server'});

  const deployment = {
    render(wizard) {
      const locked = wizard.state.initial.administrator !== 'none';
      wizard.data.deployment = wizard.data.deployment || deploymentType();
      const section = element(`
        <section class="dp-setup-page dp-setup-deployment">
          <div class="dp-setup-copy">
            <p data-i18n="setup.deployment.body.types" data-i18n-rich></p>
            <p data-i18n="setup.deployment.body.choose"></p>
          </div>
          <div class="dp-setup-choices" role="radiogroup" data-i18n-attr="aria-label:setup.deployment.title">
            ${['standalone', 'server'].map(type => `
              <button type="button" class="dp-setup-choice" role="radio" data-deployment="${type}" ${locked ? 'disabled' : ''}>
                <span class="dp-setup-choice-name" data-i18n="settings.deployment.${type}"></span>
                <span class="dp-setup-choice-glyph" aria-hidden="true">${window.DPIcons.svg(DEPLOYMENT_GLYPHS[type])}</span>
                <span class="dp-setup-choice-text" data-i18n="setup.deployment.${type}.text"></span>
              </button>`).join('')}
          </div>
          ${locked ? '<p class="dp-setup-hint" data-i18n="setup.deployment.locked"></p>' : ''}
        </section>`);
      const options = Array.from(section.querySelectorAll('[data-deployment]'));
      const paint = () => options.forEach(option => {
        const checked = option.dataset.deployment === wizard.data.deployment;
        option.setAttribute('aria-checked', String(checked));
        option.tabIndex = checked ? 0 : -1;
      });
      options.forEach((option, index) => {
        option.addEventListener('click', () => { wizard.data.deployment = option.dataset.deployment; paint(); });
        option.addEventListener('keydown', event => {
          const step = {ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1}[event.key];
          if (!step) return;
          event.preventDefault();
          const next = options[(index + step + options.length) % options.length];
          wizard.data.deployment = next.dataset.deployment;
          paint();
          next.focus();
        });
      });
      paint();
      return section;
    },
    async commit(wizard) {
      // Already changed by an earlier Next whose record failed: only the record is retried.
      if (wizard.resumeAt) return;
      // The type already in force is accepted as it is: nothing is reset.
      if (wizard.data.deployment === deploymentType() || wizard.state.initial.administrator !== 'none') return;
      await request('PUT', '/auth/deployment-type', {deployment_type: wizard.data.deployment, confirm: true});
      // The new type's roster resumes at Download Directory, whatever page the
      // record held for the old one (a Back from further on included).
      wizard.resumeAt = 'download_directory';
      wizard.reloadAfter = true;
    },
  };

  const downloadDirectory = {
    render(wizard) {
      const section = element(`
        <section class="dp-setup-page dp-setup-directory">
          <div class="dp-setup-copy">
            <p data-i18n="setup.directory.body.purpose"></p>
            <p data-i18n="setup.directory.body.choose"></p>
          </div>
          <label class="dp-modal-field" for="dp-setup-download-folder">
            <span class="form-label" data-i18n="settings.downloads.folder"></span>
          </label>
          <div class="dp-setup-directory-row">
            <input class="input" id="dp-setup-download-folder" type="text" spellcheck="false" autocomplete="off" maxlength="4096">
            <button type="button" class="btn btn-ghost" data-setup-browse data-i18n="settings.browse"></button>
          </div>
          <p class="dp-setup-hint" data-i18n="setup.directory.hint"></p>
        </section>`);
      const field = section.querySelector('input');
      field.value = wizard.data.folderDraft ?? wizard.data.folder ?? '';
      field.addEventListener('input', () => { wizard.data.folderDraft = field.value; });
      // The server's own folder browser, as a view of this dialog: choosing
      // only fills this page's field; Next still saves it.
      section.querySelector('[data-setup-browse]').addEventListener('click', () => openDirectory(wizard));
      return section;
    },
    async commit(wizard) {
      const value = String(wizard.data.folderDraft ?? wizard.data.folder ?? '').trim();
      if (!value) throw new Error(t('setup.directory.required'));
      if (value === wizard.data.folder) return;
      const result = await window.DPSettingsPage.writeDocument({download_folder: value});
      wizard.data.folder = String(result?.download_folder ?? value);
      wizard.data.folderDraft = wizard.data.folder;
    },
  };

  const administrator = {
    title(wizard) {
      return wizard.mode === 'rerun' ? 'setup.administrator.guidance.title' : 'setup.administrator.title';
    },
    // The tagline only while an account is being created; otherwise what is
    // true now: guidance (a rerun), created, or an unfinished creation to
    // finish -- never a playful reading over a recovery.
    hint(wizard) {
      if (wizard.mode === 'rerun') return 'setup.administrator.guidance.text';
      if (!this.creating(wizard)) return 'setup.administrator.created';
      return wizard.state.initial.administrator === 'pending' ? 'setup.administrator.resubmit' : 'setup.administrator.tagline';
    },
    // The initial setup creates the Administrator here, once; afterwards --
    // and in every rerun -- this page only points to where accounts are kept.
    creating(wizard) {
      const created = wizard.state.initial.administrator;
      return wizard.mode === 'initial' && (created === 'none' || (created === 'pending' && !signedIn()));
    },
    render(wizard) {
      if (wizard.mode === 'rerun') {
        const section = element(`
          <section class="dp-setup-page dp-setup-administrator">
            <p class="dp-setup-hint" data-i18n="setup.administrator.oidc"></p>
            <button type="button" class="btn btn-ghost" data-setup-users data-i18n="setup.administrator.open_users"></button>
          </section>`);
        section.querySelector('[data-setup-users]').addEventListener('click', async () => {
          // The page is reached, not left behind another dialog: the surface
          // this rerun was opened over (My Account) is asked to step aside
          // first, through its own guard of any unsaved edit; kept, nothing moves.
          if (wizard.opener && !(await wizard.opener.mayLeave())) return;
          // What this rerun committed stays committed; the next Rerun Setup
          // Wizard starts again at Welcome.
          wizard.dialog.close();
          wizard.opener?.leave();
          window.DPSettingsPage?.openTab('users');
        });
        return section;
      }
      if (!this.creating(wizard)) {
        return element(`
          <section class="dp-setup-page dp-setup-administrator">
            <div class="dp-setup-notes">
              <p class="dp-setup-hint" data-i18n="setup.administrator.more_accounts"></p>
              <p class="dp-setup-hint" data-i18n="setup.administrator.oidc"></p>
            </div>
          </section>`);
      }
      const section = element(`
        <section class="dp-setup-page dp-setup-administrator">
          ${wizard.state.initial.administrator === 'pending' ? '' : `
          <div class="dp-setup-copy">
            <p data-i18n="setup.administrator.body"></p>
          </div>`}
          <label class="dp-modal-field"><span class="form-label" data-i18n="settings.users.field.username"></span>
            <input class="input" data-setup-field="username" type="text" maxlength="256" autocomplete="username" spellcheck="false"></label>
          <label class="dp-modal-field"><span class="form-label" data-i18n="settings.users.field.password"></span>
            <input class="input" data-setup-field="password" type="password" maxlength="4096" autocomplete="new-password"></label>
          <label class="dp-modal-field"><span class="form-label" data-i18n="setup.administrator.confirm"></span>
            <input class="input" data-setup-field="confirm" type="password" maxlength="4096" autocomplete="new-password"></label>
          <div class="dp-setup-notes">
            <p class="dp-setup-hint" data-i18n="setup.administrator.sign_in_on"></p>
          </div>
        </section>`);
      section.querySelectorAll('[data-setup-field]').forEach(input => {
        const name = input.dataset.setupField;
        input.value = wizard.data.administrator?.[name] || '';
        input.addEventListener('input', () => {
          wizard.data.administrator = {...(wizard.data.administrator || {}), [name]: input.value};
        });
      });
      return section;
    },
    async commit(wizard) {
      if (wizard.mode !== 'initial' || !this.creating(wizard)) return;
      const entered = wizard.data.administrator || {};
      const username = String(entered.username || '').trim();
      if (!username) throw new Error(t('setup.administrator.username_required'));
      if (!entered.password) throw new Error(t('setup.administrator.password_required'));
      if (entered.password !== entered.confirm) throw new Error(t('setup.administrator.passwords_differ'));
      await request('POST', '/setup/initial/administrator', {username, password: entered.password}, 30000);
      // The Administrator's own session now holds this browser, and its
      // record says where setup resumes: the page continues under it.
      wizard.data.administrator = null;
      wizard.reloadAfter = true;
      wizard.phaseRecorded = true;
    },
  };

  /* Premium Services: six services in this fixed order -- Usenet, the one
   * service category, first; then the debrid providers alphabetically -- each
   * showing what the server says it is: configured or not, verified or not,
   * and disabled when it is off (three facts, never merged). Choosing one
   * shows its configuration, as Easy shows it, in this same dialog
   * (DPSettingsPage.mountProvider). Each button wears the service's own
   * artwork, the mark its Settings card wears (DPSettingsPage.providerArtwork).
   * None is required: Next is the ordinary Next however many are configured. */
  const SERVICES = Object.freeze([
    ['usenet', 'Usenet'], ['alldebrid', 'AllDebrid'], ['debridlink', 'Debrid-Link'],
    ['premiumize', 'Premiumize'], ['realdebrid', 'Real-Debrid'], ['torbox', 'TorBox'],
  ]);
  // What acts at once in a service's configuration, said under it: the
  // account services connect on the provider's own site, Usenet's servers
  // have their own immediate controls, the rest a key.
  const IMMEDIATE_NOTES = Object.freeze({
    usenet: 'setup.premium.immediate_usenet', realdebrid: 'setup.premium.immediate_account',
    torbox: 'setup.premium.immediate_account',
  });

  function serviceState(fact) {
    if (!fact) return '';
    const parts = [fact.configured ? 'settings.status.configured' : 'settings.status.unconfigured'];
    if (fact.configured) parts.push(fact.verified ? 'settings.status.verified' : 'settings.status.unverified');
    if (!fact.enabled) parts.push('provider_status.state.disabled');
    return parts.map(key => `<span class="dp-setup-service-fact" data-fact="${key.split('.').pop()}" data-i18n="${key}"></span>`).join('');
  }

  const premiumServices = {
    render(wizard) {
      const section = element(`
        <section class="dp-setup-page dp-setup-premium">
          <div class="dp-setup-copy">
            <p data-i18n="setup.premium.body.what"></p>
            <p data-i18n="setup.premium.body.optional" data-i18n-rich></p>
          </div>
          <div class="dp-setup-services">
            ${SERVICES.map(([id, name]) => `
              <button type="button" class="dp-setup-service" data-service="${id}">
                <span class="dp-setup-service-art">${window.DPSettingsPage.providerArtwork(id)}</span>
                <span class="dp-setup-service-copy">
                  <span class="dp-setup-service-name">${name}</span>
                  <span class="dp-setup-service-state">${serviceState(wizard.data.services?.[id])}</span>
                </span>
              </button>`).join('')}
          </div>
        </section>`);
      section.querySelectorAll('[data-service]').forEach(button => {
        button.addEventListener('click', () => { void openProvider(wizard, button.dataset.service); });
      });
      // The state shown is read afresh whenever the list is shown.
      void refreshServices(wizard, section);
      return section;
    },
    async commit() {},
  };

  async function refreshServices(wizard, section) {
    let facts;
    try { facts = await window.DPSettingsPage.providerStatuses(); } catch (_) { return; }
    wizard.data.services = facts;
    if (!section.isConnected) return;
    section.querySelectorAll('[data-service]').forEach(button => {
      const slot = button.querySelector('.dp-setup-service-state');
      slot.innerHTML = serviceState(facts[button.dataset.service]);
      window.DPI18n.render(slot);
    });
  }

  /* One service's configuration, in place of the list, in the same dialog:
   * Save and Cancel take the footer. Save commits every changed field
   * through its own canonical scope and returns to the list once all were
   * accepted; Cancel discards what was not saved. Test, Enable, Remove,
   * Connect and Disconnect act at once, as on Services, and Cancel does not
   * undo them. A provider's own Test (its header's) is offered only while
   * nothing is unsaved, so it proves what is saved; a Usenet server's Test
   * checks what that server shows, as it does on Services. */
  async function openProvider(wizard, id) {
    if (wizard.provider) return;
    const name = SERVICES.find(([service]) => service === id)[1];
    const section = element(`
      <section class="dp-setup-page dp-setup-provider" data-provider="${id}">
        <div class="dp-setup-provider-slot"></div>
        <p class="dp-setup-hint" data-setup-test-note data-i18n="setup.premium.test_after_save" hidden></p>
      </section>`);
    wizard.provider = {id, section, handle: null};
    wizard.error.hidden = true;
    wizard.body.replaceChildren(section, wizard.error);
    window.DPI18n.render(section);
    const handle = wizard.dialog;
    // A view of Premium Services, not a page of its own: its heading names the
    // service under the page's mark, and the progress stays where it is.
    handle.setTitle(name);
    // Its reading: what acts at once there, and what Save and Cancel do.
    handle.setTitleHint(said(IMMEDIATE_NOTES[id] || 'setup.premium.immediate'));
    handle.setAcceptLabel(said('common.save'));
    handle.setCancelLabel(said('common.cancel'));
    handle.setCancelHidden(false);
    handle.setActionHidden('back', true);
    try {
      wizard.provider.handle = await window.DPSettingsPage.mountProvider(id, section.querySelector('.dp-setup-provider-slot'));
    } catch (error) {
      fail(wizard, error);
      return;
    }
    const note = section.querySelector('[data-setup-test-note]');
    const paintTest = () => {
      const unsaved = wizard.provider?.handle?.dirty() === true;
      const tests = section.querySelectorAll('button[data-action^="test-"]');
      tests.forEach(test => { test.disabled = unsaved; });
      note.hidden = !unsaved || !tests.length;
    };
    section.addEventListener('input', paintTest);
    section.addEventListener('dp-settings-settled', paintTest);
    paintTest();
    section.querySelector('.dp-setup-provider-slot input:not([type="checkbox"])')?.focus({preventScroll: true});
  }

  function closeProvider(wizard) {
    if (!wizard.provider) return;
    wizard.provider = null;
    wizard.repaint();
  }

  async function saveProvider(wizard) {
    const handle = wizard.provider?.handle;
    if (!handle) return false;
    wizard.error.hidden = true;
    const accepted = await handle.save();
    if (accepted) closeProvider(wizard);
    else fail(wizard, new Error(t('setup.error.not_saved')));
    return false;
  }

  function fail(wizard, error) {
    wizard.error.textContent = String(error?.message || t('setup.error.not_saved'));
    wizard.error.hidden = false;
  }

  /* A view of the current page in place of its body, in this same dialog:
   * Welcome's language list and Download Directory's folder browser. The
   * header names the view and the footer answers it (``accept``; Cancel or
   * Back, and Escape, leave it); the progress stays on its page. A view
   * commits nothing and records nothing: what it chooses is the page's draft,
   * which only the page's own Next saves. Leaving returns focus to the
   * control that opened it. */
  function showView(wizard, view, {title, hint, accept = null, cancel}) {
    wizard.view = view;
    wizard.error.hidden = true;
    wizard.body.replaceChildren(view.section, wizard.error);
    window.DPI18n.render(view.section);
    const handle = wizard.dialog;
    handle.setTitle(said(title));
    handle.setTitleHint(said(hint));
    handle.setAcceptHidden(!accept);
    if (accept) handle.setAcceptLabel(said(accept));
    handle.setCancelLabel(said(cancel));
    handle.setCancelHidden(false);
    handle.setActionHidden('back', true);
    wizard.body.scrollTop = 0;
  }

  function closeView(wizard) {
    const view = wizard.view;
    if (!view) return;
    wizard.view = null;
    view.release();
    wizard.repaint();
    wizard.body.querySelector(view.opener)?.focus({preventScroll: true});
  }

  /* Welcome's language list (DPLanguagePicker, the one list): a row is the
   * page's language at once -- shown now, saved only by Welcome's Next -- and
   * returns to Welcome; Back returns with nothing changed. */
  function openLanguage(wizard) {
    const picker = window.DPLanguagePicker;
    if (wizard.view || !picker || !wizard.data.choices) return;
    const section = element(`
      <section class="dp-setup-page dp-setup-language-view">
        <div class="dp-language-slot" data-setup-language-list></div>
      </section>`);
    let choosing = false;
    const view = {kind: 'language', section, opener: '[data-setup-language]', rows: null, release() {}};
    showView(wizard, view, {title: 'setup.welcome.change_language', hint: 'setup.welcome.change_language_hint',
      cancel: 'settings.common.back'});
    view.rows = picker.list(section.querySelector('[data-setup-language-list]'), {
      languages: wizard.data.choices, selected: wizard.data.pending, label: t('setup.welcome.change_language'),
      onChoose: value => {
        if (choosing || wizard.view !== view) return;
        choosing = true;
        wizard.data.pending = value;
        view.rows.setSelected(value);
        view.rows.setBusy(true);
        // A preview only: the page shows the language now; Next saves it.
        void picker.language(value).then(picker.apply).finally(() => { if (wizard.view === view) closeView(wizard); });
      },
    });
    view.rows.focus();
  }

  /* Download Directory's folder browser: the server's own
   * (DPSettingsDirectoryPicker.mount, the browser Settings' Browse shows),
   * starting at the folder this page holds. Use This Folder is offered only
   * where the server says the folder shown may be chosen; it fills this
   * page's field and returns. Cancel returns with the field as it was.
   * Nothing is saved here. */
  function openDirectory(wizard) {
    const pickerOwner = window.DPSettingsDirectoryPicker;
    if (wizard.view || !pickerOwner) return;
    const handle = wizard.dialog;
    const section = element('<section class="dp-setup-page dp-setup-directory-view"></section>');
    const view = {
      kind: 'directory', section, opener: '[data-setup-browse]', browser: null,
      accept() {
        const chosen = view.browser?.selected();
        if (chosen == null) return false;
        wizard.data.folderDraft = chosen;
        closeView(wizard);
        return false;
      },
      release() {
        view.browser?.release();
        handle.setBusy(false);
      },
    };
    showView(wizard, view, {title: 'directory_picker.download.title', hint: 'setup.directory.browse_hint',
      accept: 'directory_picker.accept', cancel: 'common.cancel'});
    handle.setAcceptEnabled(false);
    view.browser = pickerOwner.mount('download', section, {
      path: String(wizard.data.folderDraft ?? wizard.data.folder ?? ''),
      host: {
        setAcceptEnabled: enabled => { if (wizard.view === view) handle.setAcceptEnabled(enabled); },
        setBusy: busy => { if (wizard.view === view) handle.setBusy(busy); },
      },
    });
    section.querySelector('[data-directory-up]')?.focus({preventScroll: true});
  }

  const gettingStarted = {
    // The sign-in note is a fact of this flow, never of the deployment alone:
    // only the initial setup of a Server installation whose Administrator
    // exists ends its session at Finish (/setup/initial/finish answers
    // ``sign_in_required`` for exactly that); a rerun and a Standalone
    // installation go straight to the Dashboard, and the preview creates no
    // Administrator, so its Finish signs nobody in.
    signsIn(wizard) {
      return wizard.mode === 'initial' && !wizard.preview && wizard.deployment === 'server'
        && wizard.state.initial.administrator === 'created';
    },
    render(wizard) {
      return element(`
        <section class="dp-setup-page dp-setup-getting-started">
          <div class="dp-setup-copy">
            <p data-i18n="setup.getting_started.body"></p>
          </div>
          <ol class="dp-setup-steps">
            <li data-i18n="setup.getting_started.step.open" data-i18n-rich></li>
            <li data-i18n="setup.getting_started.step.add" data-i18n-rich></li>
            <li data-i18n="setup.getting_started.step.monitor" data-i18n-rich></li>
          </ol>
          ${this.signsIn(wizard) ? `
          <p class="dp-setup-sign-in-note" data-i18n="setup.getting_started.sign_in"></p>` : ''}
        </section>`);
    },
    async commit() {},
  };

  const PAGES = Object.freeze({
    welcome, deployment, download_directory: downloadDirectory, administrator,
    premium_services: premiumServices, getting_started: gettingStarted,
  });

  /* ── the wizard ───────────────────────────────────────────────────────── */

  /* The Connected Page Progress Indicator: one bubble per page of the active
   * sequence, joined by strings, above the heading. A reading, never a
   * control -- pages change only through Back, Next and Finish.
   *
   * Completed and current are separate facts. A page is completed once its
   * Next succeeded and the wizard moved on: every page before the furthest
   * page the record holds (the server's for the initial setup and a rerun,
   * the preview's own for the preview), which only a successful Next moves.
   * Going Back undoes nothing, so a completed page keeps its check; the
   * current page -- completed or not -- carries the strong glow. A string is
   * read from the two pages it joins, never from where the operator is: solid
   * completion green between two completed pages, one gradient from that green
   * into the unfinished colour where a completed page meets an incomplete one
   * (the completion boundary, wherever Back has taken the operator), and the
   * unfinished colour between two incomplete pages. */
  const iconUrl = name => `/icons/lucide/${name}.svg`;

  function progressList() {
    const node = document.createElement('ol');
    node.className = 'dp-setup-progress';
    node.dataset.i18nAttr = 'aria-label:setup.progress.label';
    node.setAttribute('aria-label', t('setup.progress.label'));
    return node;
  }

  /* The header's top region (the dialog owner's ``headerTop``): the
   * indicator, then one slot of fixed height between it and the heading,
   * reserved on every page so no heading moves. It holds Premium Services'
   * Optional badge, a reading (never a control), shown on that page and its
   * provider views. Behind the whole header lies the sidebar's own signal
   * field (<dp-signal-field>, decoration only). */
  function headerTop(progress) {
    const node = element(`
      <div class="dp-setup-header-top">
        <dp-signal-field class="dp-signal-field" aria-hidden="true"></dp-signal-field>
        <p class="dp-setup-badge-slot"><span class="dp-badge dp-badge--caution dp-setup-optional" data-i18n="setup.premium.optional" hidden></span></p>
      </div>`);
    node.prepend(progress);
    window.DPI18n.render(node);
    return node;
  }

  function paintProgress(node, wizard) {
    const total = wizard.pages.length;
    const done = index => index < wizard.recordedIndex;
    node.innerHTML = wizard.pages.map((_, index) => {
      const current = index === wizard.index;
      const values = JSON.stringify({current: index + 1, total});
      const label = done(index) ? 'setup.progress.step_done' : 'setup.step';
      // done -> done: completed; done -> incomplete: the boundary; else unfinished.
      const link = index === 0 ? ''
        : `<span class="dp-setup-progress-link" data-link="${!done(index - 1) ? 'todo' : done(index) ? 'done' : 'boundary'}" aria-hidden="true"></span>`;
      return `<li class="dp-setup-progress-step" data-state="${done(index) ? 'done' : 'todo'}"${current ? ' aria-current="step"' : ''}
          data-i18n-attr="aria-label:${label}" data-i18n-values='${values}'>${link}<span class="dp-setup-progress-bubble" aria-hidden="true">${
          done(index) ? window.DPIcons.svg('check') : index + 1}</span></li>`;
    }).join('');
    window.DPI18n.render(node);
  }

  async function readData() {
    const [locale, choices, settings] = await Promise.all([
      request('GET', '/settings/default-locale').catch(() => null),
      window.DPLanguagePicker ? window.DPLanguagePicker.choices().catch(() => null) : null,
      request('GET', '/settings').catch(() => null),
    ]);
    return {locale, choices, folder: settings ? String(settings.download_folder ?? '') : null};
  }

  /* ``preview`` (TEMPORARY, development only): the first-run pages shown from
   * an Admin's rerun, committing nothing -- no language, deployment, folder,
   * Administrator, phase or completion. Its Deployment choice only picks which
   * roster is shown. A provider's own Save still saves, as it always does. */
  function open(mode, state, data, opener = null, {preview = false} = {}) {
    const deploymentNow = deploymentType();
    let pages = ROSTERS[mode][deploymentNow];
    let recorded = mode === 'initial' ? state.initial.phase : state.rerun?.phase;
    // A deployment change whose resume page was never recorded (interrupted
    // between its two writes) leaves the other type's page: one this roster
    // does not have, or -- on a Server with no Administrator yet -- one beyond
    // Administrator Setup, which the lifecycle never admits there. The new
    // roster resumes where every deployment change does, at Download
    // Directory, and the record is brought there through the one phase write.
    const reconcile = mode === 'initial' && !preview && (!pages.includes(recorded)
      || (deploymentNow === 'server' && state.initial.administrator === 'none' && AFTER_ADMINISTRATOR.has(recorded)));
    if (reconcile) recorded = 'download_directory';
    let index = Math.max(0, pages.indexOf(recorded));
    // A recovered Administrator (its record unconfirmed, its own session
    // here) resumes at its page, whose Next confirms it.
    if (mode === 'initial' && state.initial.administrator === 'pending' && signedIn() && pages.includes('administrator')) {
      index = pages.indexOf('administrator');
    }
    const progress = progressList();
    const top = headerTop(progress);
    const wizard = {mode, preview, state, data, deployment: deploymentNow, pages, index, reloadAfter: false,
      phaseRecorded: false, resumeAt: null, dialog: null, body: null, error: null, opener, provider: null, view: null,
      repaint: null,
      // The furthest page the server records: Next moves the record only beyond
      // it, and every page before it is completed.
      recordedIndex: Math.max(0, pages.indexOf(recorded)),
      async showOwnLanguage() {
        const own = await request('GET', '/account/locale').catch(() => null);
        const picker = window.DPLanguagePicker;
        if (own && picker) await picker.apply(await picker.language(own.locale));
      },
    };

    const page = () => PAGES[pages[wizard.index]];
    const titleKey = () => (page().title ? page().title(wizard) : TITLES[pages[wizard.index]]);
    const hintKey = () => (page().hint ? page().hint(wizard) : HINTS[pages[wizard.index]]);
    const acceptKey = () => (wizard.index === pages.length - 1 ? 'setup.action.finish' : 'setup.action.next');
    // The page's mark: Welcome's is the product logo, drawn inline each time.
    const mark = () => (pages[wizard.index] === 'welcome' ? document.createElement('dp-logo') : iconUrl(ICONS[pages[wizard.index]]));

    function paint(handle) {
      const section = page().render(wizard);
      // A page (never a view of one): its explanation at the top, its
      // controls toward the bottom (ui-setup-wizard.css .dp-setup-main).
      section.classList.add('dp-setup-main');
      const error = element('<p class="dp-setup-error" role="alert" hidden></p>');
      wizard.error = error;
      wizard.body.replaceChildren(section, error);
      if (preview) {
        wizard.body.prepend(element('<p class="dp-setup-preview-note">Preview: nothing on these pages is saved. A provider\'s own Save, and its immediate actions such as Connect, Test and Enable, still take effect.</p>'));
      }
      window.DPI18n.render(wizard.body);
      handle.setTitle(said(titleKey()));
      handle.setTitleIcon(mark());
      handle.setTitleHint(said(hintKey()));
      handle.setAcceptLabel(said(acceptKey()));
      handle.setAcceptHidden(false);
      handle.setAcceptEnabled(true);
      top.querySelector('.dp-setup-optional').hidden = pages[wizard.index] !== 'premium_services';
      // Back is always in its place -- the footer keeps one shape from page to
      // page -- and has nowhere to go from the first page.
      handle.setActionHidden('back', false);
      handle.setActionEnabled('back', wizard.index > 0);
      paintProgress(progress, wizard);
      wizard.body.scrollTop = 0;
      // The page's first thing to answer: a field, the chosen option, a control.
      const first = section.querySelector('input') || section.querySelector('[role="radio"][tabindex="0"]:not([disabled])')
        || section.querySelector('button:not([disabled])');
      first?.focus({preventScroll: true});
    }

    function fail(error) {
      wizard.error.textContent = String(error?.message || t('setup.error.not_saved'));
      wizard.error.hidden = false;
    }

    // The page as it is now, labels and all (a provider's view returns here).
    wizard.repaint = () => {
      if (mode === 'initial' && !preview) wizard.dialog.setCancelHidden(true);
      wizard.dialog.setCancelLabel(said('dialog.close'));
      paint(wizard.dialog);
    };

    // A preview's Next only moves on (Finish ends the preview); after
    // Deployment it shows the chosen type's pages.
    function previewNext(handle) {
      wizard.error.hidden = true;
      if (wizard.index === pages.length - 1) return true;
      const current = pages[wizard.index];
      if (current === 'deployment') {
        wizard.deployment = wizard.data.deployment;
        pages = wizard.pages = ROSTERS.initial[wizard.deployment];
      }
      wizard.index = pages.indexOf(current) + 1;
      // The preview's own record: this page's Next succeeded (nothing is written).
      wizard.recordedIndex = Math.max(wizard.recordedIndex, wizard.index);
      paint(handle);
      return false;
    }

    async function next(handle) {
      // A provider's view: its Save. A page's own view: its choice.
      if (wizard.provider) return saveProvider(wizard);
      if (wizard.view) return wizard.view.accept?.() ?? false;
      if (preview) return previewNext(handle);
      wizard.error.hidden = true;
      handle.setActionEnabled('back', false);
      try {
        await page().commit(wizard);
        if (wizard.index === pages.length - 1) return await finish();
        const target = wizard.index + 1;
        if (wizard.resumeAt) {
          // A deployment change: the record is set to where the new roster
          // resumes, even before the furthest page the old one reached.
          wizard.state = await request('PUT', '/setup/initial/phase', {phase: wizard.resumeAt});
        } else if (!wizard.phaseRecorded && target > wizard.recordedIndex) {
          const result = mode === 'initial'
            ? await request('PUT', '/setup/initial/phase', {phase: pages[target]})
            : await request('PUT', '/setup/rerun', {phase: pages[target]});
          wizard.state = result;
          wizard.recordedIndex = target;
        }
        if (wizard.reloadAfter) {
          window.location.reload();
          return false;
        }
        wizard.index = target;
        paint(handle);
        return false;
      } catch (error) {
        fail(error);
        return false;
      } finally {
        handle.setActionEnabled('back', wizard.index > 0);
      }
    }

    async function finish() {
      if (mode === 'rerun') {
        await request('DELETE', '/setup/rerun');
        openDashboard();
        return true;
      }
      const result = await request('POST', '/setup/initial/finish');
      if (result?.sign_in_required) {
        // The wizard's session has ended: the Administrator signs in again.
        window.location.assign('/login');
        return false;
      }
      openDashboard();
      return true;
    }

    function openDashboard() {
      const entries = Array.from(document.querySelectorAll('.nav-item[data-view="dashboard"]'));
      const entry = entries.find(item => item.getClientRects().length) || entries[0];
      if (entry && typeof window.nav === 'function') window.nav(entry);
    }

    const relabel = () => {
      // Said words are re-said by the runtime; the language list's own name
      // and the language button's too.
      if (pages[wizard.index] !== 'welcome') return;
      wizard.body.querySelector('.dp-language-list')?.setAttribute('aria-label', t('setup.welcome.change_language'));
      void paintLanguageButton(wizard, wizard.body.querySelector('[data-setup-language]'));
    };

    const dialog = window.DPSettingsModal.open({
      title: said(titleKey()),
      className: 'dp-setup-dialog',
      bodyClassName: 'dp-setup-body',
      headerTop: top,
      titleIcon: mark(),
      titleHint: said(hintKey()),
      // The initial setup is finished, never dismissed (its preview is).
      blocking: mode === 'initial' && !preview,
      acceptLabel: said(acceptKey()),
      cancelLabel: said('dialog.close'),
      actions: [{id: 'back', label: said('settings.common.back'), placement: 'leading'}],
      mount(body, handle) {
        wizard.body = body;
        if (mode === 'initial' && !preview) handle.setCancelHidden(true);
        paint(handle);
        return null;
      },
      onAction(id) {
        if (id !== 'back' || wizard.index === 0 || wizard.provider || wizard.view) return;
        wizard.error.hidden = true;
        wizard.index -= 1;
        paint(wizard.dialog);
      },
      onAccept: handle => next(handle),
      async onCancel() {
        // A provider's view: Cancel (and Escape) return to the list; what was
        // not saved is discarded.
        if (wizard.provider) { closeProvider(wizard); return false; }
        // A page's own view: Back or Cancel (and Escape) return to the page,
        // its draft as it was.
        if (wizard.view) { closeView(wizard); return false; }
        if (preview) return true;
        if (mode === 'initial') return false;
        // Dismissed: the rerun ends; nothing it committed is undone.
        try { await request('DELETE', '/setup/rerun'); } catch (_) { /* the record stays; reopening resumes it */ }
        return true;
      },
    });
    wizard.dialog = dialog;
    if (reconcile) {
      // Nothing else is redone: the deployment type is already the new one.
      void request('PUT', '/setup/initial/phase', {phase: 'download_directory'})
        .then(result => { wizard.state = result; }, () => { /* the next Next records forward from here */ });
    }
    // A service saved or connected meanwhile (an account authorized after
    // its view was left) shows on the list at once.
    const accepted = () => {
      const list = !wizard.provider && wizard.body?.querySelector('.dp-setup-premium');
      if (list) void refreshServices(wizard, list);
    };
    document.addEventListener('debridpulse:locale-changed', relabel);
    document.addEventListener('debridpulse:integration-accepted', accepted);
    void dialog.closed.then(() => {
      document.removeEventListener('debridpulse:locale-changed', relabel);
      document.removeEventListener('debridpulse:integration-accepted', accepted);
      if (active === wizard) active = null;
      // A previewed language that Next never saved is withdrawn.
      if (preview || pages[wizard.index] === 'welcome') void wizard.showOwnLanguage();
      // The preview returns to the rerun it was opened from.
      if (preview) void rerun({opener: wizard.opener, resume: true});
    });
    active = wizard;
    return wizard;
  }

  async function readState() {
    return request('GET', '/setup/state');
  }

  /* TEMPORARY, development only (WS3b): Test First-Run Wizard. The rerun is
   * set aside, not ended (its record stays at Welcome), and the first-run
   * pages open as a preview with nothing created, chosen or recorded; leaving
   * the preview reopens the rerun. */
  async function previewInitial(wizard) {
    const data = await readData();
    wizard.dialog.close();
    open('initial', {initial: {phase: 'welcome', complete: false, administrator: 'none'}}, data, wizard.opener, {preview: true});
  }

  /* At start: an Admin of an installation whose setup is unfinished sees it. */
  async function start() {
    if (!auth()?.can('administer')) return;
    let state;
    try { state = await readState(); } catch (_) { return; }
    if (!state?.initial || state.initial.complete || active) return;
    open('initial', state, await readData());
  }

  /* My Account's Rerun Setup Wizard: this Admin's own rerun, never the
   * installation's setup. Every explicit Rerun starts at Welcome: the rerun's
   * record (where it stopped last time, if anywhere) is set back to Welcome
   * through its one owner (PUT /setup/rerun) -- only that navigation record;
   * every setting, account and provider stays exactly as saved, and each page
   * reads its values afresh (readData), so no unsaved draft of an earlier
   * rerun returns. An unfinished initial setup still resumes where its own
   * record says (open('initial')). `opener` ({mayLeave, leave}) is the
   * surface it opens over, which a page outside the wizard asks to step aside
   * before it is shown. ``resume`` is the development preview's own return to
   * the rerun it was opened from (at Welcome, its record kept): it writes
   * nothing, as the preview never does. */
  async function rerun({opener = null, resume = false} = {}) {
    if (active) return;
    const state = await readState();
    if (!state.initial.complete) { open('initial', state, await readData(), opener); return; }
    const current = resume && state.rerun ? state : await request('PUT', '/setup/rerun', {phase: 'welcome'});
    // TEMPORARY (WS3b): whether this build offers the development-only preview.
    open('rerun', current, {...await readData(), development: state.development_build === true}, opener);
  }

  window.DPSetupWizard = Object.freeze({rerun});

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => { void start(); }, {once: true});
  else void start();
})();
