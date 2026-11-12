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
      const section = element(`
        <section class="dp-setup-page dp-setup-welcome">
          <p class="dp-setup-lead" data-i18n="setup.welcome.intro"></p>
          <p class="dp-setup-label" id="dp-setup-language-label" data-i18n="setup.welcome.language"></p>
          <p class="dp-setup-hint" data-i18n="setup.welcome.language_hint"></p>
          <div class="dp-language-slot dp-setup-language" data-setup-language></div>
        </section>`);
      const slot = section.querySelector('[data-setup-language]');
      const picker = window.DPLanguagePicker;
      if (wizard.data.locale && wizard.data.choices && picker) {
        wizard.data.pending = wizard.data.pending || wizard.data.locale.configured || wizard.data.locale.locale;
        const rows = picker.list(slot, {
          languages: wizard.data.choices, selected: wizard.data.pending, label: t('setup.welcome.language'),
          onChoose: value => {
            wizard.data.pending = value;
            rows.setSelected(value);
            // A preview only: the page shows the language now; Next saves it.
            void picker.language(value).then(picker.apply);
          },
        });
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

  const deployment = {
    render(wizard) {
      const locked = wizard.state.initial.administrator !== 'none';
      wizard.data.deployment = wizard.data.deployment || deploymentType();
      const section = element(`
        <section class="dp-setup-page dp-setup-deployment">
          <p class="dp-setup-lead" data-i18n="setup.deployment.intro"></p>
          <div class="dp-setup-choices" role="radiogroup" data-i18n-attr="aria-label:setup.deployment.title">
            ${['standalone', 'server'].map(type => `
              <button type="button" class="dp-setup-choice" role="radio" data-deployment="${type}" ${locked ? 'disabled' : ''}>
                <span class="dp-setup-choice-name" data-i18n="settings.deployment.${type}"></span>
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
          <p class="dp-setup-lead" data-i18n="setup.directory.intro"></p>
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
      section.querySelector('[data-setup-browse]').addEventListener('click', () => {
        // The server's own folder browser; it sets this field and saves nothing.
        window.DPSettingsDirectoryPicker?.open('download', {field});
      });
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
            <p class="dp-setup-lead" data-i18n="setup.administrator.guidance.text"></p>
            <p class="dp-setup-hint" data-i18n="setup.administrator.oidc"></p>
            <button type="button" class="btn btn-ghost" data-setup-users data-i18n="setup.administrator.open_users"></button>
          </section>`);
        section.querySelector('[data-setup-users]').addEventListener('click', async () => {
          // The page is reached, not left behind another dialog: the surface
          // this rerun was opened over (My Account) is asked to step aside
          // first, through its own guard of any unsaved edit; kept, nothing moves.
          if (wizard.opener && !(await wizard.opener.mayLeave())) return;
          // The rerun is kept: Rerun Setup Wizard resumes it here.
          wizard.dialog.close();
          wizard.opener?.leave();
          window.DPSettingsPage?.openTab('users');
        });
        return section;
      }
      if (!this.creating(wizard)) {
        return element(`
          <section class="dp-setup-page dp-setup-administrator">
            <p class="dp-setup-lead" data-i18n="setup.administrator.created"></p>
            <p class="dp-setup-hint" data-i18n="setup.administrator.more_accounts"></p>
            <p class="dp-setup-hint" data-i18n="setup.administrator.oidc"></p>
          </section>`);
      }
      const section = element(`
        <section class="dp-setup-page dp-setup-administrator">
          <p class="dp-setup-lead" data-i18n="setup.administrator.intro"></p>
          ${wizard.state.initial.administrator === 'pending' ? '<p class="dp-setup-hint" data-i18n="setup.administrator.resubmit"></p>' : ''}
          <label class="dp-modal-field"><span class="form-label" data-i18n="settings.users.field.username"></span>
            <input class="input" data-setup-field="username" type="text" maxlength="256" autocomplete="username" spellcheck="false"></label>
          <label class="dp-modal-field"><span class="form-label" data-i18n="settings.users.field.password"></span>
            <input class="input" data-setup-field="password" type="password" maxlength="4096" autocomplete="new-password"></label>
          <label class="dp-modal-field"><span class="form-label" data-i18n="setup.administrator.confirm"></span>
            <input class="input" data-setup-field="confirm" type="password" maxlength="4096" autocomplete="new-password"></label>
          <p class="dp-setup-hint" data-i18n="setup.administrator.sign_in_on"></p>
          <p class="dp-setup-hint" data-i18n="setup.administrator.more_accounts"></p>
          <p class="dp-setup-hint" data-i18n="setup.administrator.oidc"></p>
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

  const premiumServices = {
    render() {
      return element(`
        <section class="dp-setup-page dp-setup-premium">
          <p class="dp-setup-lead" data-i18n="setup.premium.hint"></p>
        </section>`);
    },
    async commit() {},
  };

  const gettingStarted = {
    render(wizard) {
      const signIn = wizard.mode === 'initial' && wizard.deployment === 'server';
      return element(`
        <section class="dp-setup-page dp-setup-getting-started">
          <p class="dp-setup-lead" data-i18n="setup.getting_started.intro"></p>
          <figure class="dp-setup-illustration" aria-hidden="true"></figure>
          <ol class="dp-setup-steps">
            <li data-i18n="setup.getting_started.step_paste"></li>
            <li data-i18n="setup.getting_started.step_add"></li>
            <li data-i18n="setup.getting_started.step_watch"></li>
          </ol>
          ${signIn ? '<p class="dp-setup-hint" data-i18n="setup.getting_started.sign_in"></p>' : ''}
        </section>`);
    },
    async commit() {},
  };

  const PAGES = Object.freeze({
    welcome, deployment, download_directory: downloadDirectory, administrator,
    premium_services: premiumServices, getting_started: gettingStarted,
  });

  /* ── the wizard ───────────────────────────────────────────────────────── */

  function stepIndicator() {
    const node = document.createElement('p');
    node.className = 'dp-setup-step';
    return node;
  }

  async function readData() {
    const [locale, choices, settings] = await Promise.all([
      request('GET', '/settings/default-locale').catch(() => null),
      window.DPLanguagePicker ? window.DPLanguagePicker.choices().catch(() => null) : null,
      request('GET', '/settings').catch(() => null),
    ]);
    return {locale, choices, folder: settings ? String(settings.download_folder ?? '') : null};
  }

  function open(mode, state, data, opener = null) {
    const deploymentNow = deploymentType();
    const pages = ROSTERS[mode][deploymentNow];
    let recorded = mode === 'initial' ? state.initial.phase : state.rerun?.phase;
    // A deployment change whose resume page was never recorded (interrupted
    // between its two writes) leaves the other type's page: one this roster
    // does not have, or -- on a Server with no Administrator yet -- one beyond
    // Administrator Setup, which the lifecycle never admits there. The new
    // roster resumes where every deployment change does, at Download
    // Directory, and the record is brought there through the one phase write.
    const reconcile = mode === 'initial' && (!pages.includes(recorded)
      || (deploymentNow === 'server' && state.initial.administrator === 'none' && AFTER_ADMINISTRATOR.has(recorded)));
    if (reconcile) recorded = 'download_directory';
    let index = Math.max(0, pages.indexOf(recorded));
    // A recovered Administrator (its record unconfirmed, its own session
    // here) resumes at its page, whose Next confirms it.
    if (mode === 'initial' && state.initial.administrator === 'pending' && signedIn() && pages.includes('administrator')) {
      index = pages.indexOf('administrator');
    }
    const indicator = stepIndicator();
    const wizard = {mode, state, data, deployment: deploymentNow, pages, index, reloadAfter: false,
      phaseRecorded: false, resumeAt: null, dialog: null, body: null, error: null, opener,
      // The furthest page the server records: Next moves the record only beyond it.
      recordedIndex: Math.max(0, pages.indexOf(recorded)),
      async showOwnLanguage() {
        const own = await request('GET', '/account/locale').catch(() => null);
        const picker = window.DPLanguagePicker;
        if (own && picker) await picker.apply(await picker.language(own.locale));
      },
    };

    const page = () => PAGES[pages[wizard.index]];
    const titleKey = () => (page().title ? page().title(wizard) : TITLES[pages[wizard.index]]);
    const acceptKey = () => (wizard.index === pages.length - 1 ? 'setup.action.finish'
      : wizard.index === 0 && mode === 'initial' ? 'setup.action.get_started' : 'setup.action.next');

    function paint(handle) {
      const section = page().render(wizard);
      const error = element('<p class="dp-setup-error" role="alert" hidden></p>');
      wizard.error = error;
      wizard.body.replaceChildren(section, error);
      window.DPI18n.render(wizard.body);
      handle.setTitle(said(titleKey()));
      handle.setAcceptLabel(said(acceptKey()));
      handle.setActionHidden('back', wizard.index === 0);
      indicator.dataset.i18n = 'setup.step';
      indicator.dataset.i18nValues = JSON.stringify({current: wizard.index + 1, total: pages.length});
      window.DPI18n.render(indicator);
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

    async function next(handle) {
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
        handle.setActionEnabled('back', true);
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
      // Said words are re-said by the runtime; the language list's own name too.
      if (pages[wizard.index] === 'welcome') {
        wizard.body.querySelector('.dp-language-list')?.setAttribute('aria-label', t('setup.welcome.language'));
      }
    };

    const dialog = window.DPSettingsModal.open({
      title: said(titleKey()),
      className: 'dp-setup-dialog',
      bodyClassName: 'dp-setup-body',
      headerEnd: indicator,
      // The initial setup is finished, never dismissed.
      blocking: mode === 'initial',
      acceptLabel: said(acceptKey()),
      cancelLabel: said('dialog.close'),
      actions: [{id: 'back', label: said('settings.common.back'), placement: 'leading'}],
      mount(body, handle) {
        wizard.body = body;
        if (mode === 'initial') handle.setCancelHidden(true);
        paint(handle);
        return null;
      },
      onAction(id) {
        if (id !== 'back' || wizard.index === 0) return;
        wizard.error.hidden = true;
        wizard.index -= 1;
        paint(wizard.dialog);
      },
      onAccept: handle => next(handle),
      async onCancel() {
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
    document.addEventListener('debridpulse:locale-changed', relabel);
    void dialog.closed.then(() => {
      document.removeEventListener('debridpulse:locale-changed', relabel);
      if (active === wizard) active = null;
      // A previewed language that Next never saved is withdrawn.
      if (pages[wizard.index] === 'welcome') void wizard.showOwnLanguage();
    });
    active = wizard;
    return wizard;
  }

  async function readState() {
    return request('GET', '/setup/state');
  }

  /* At start: an Admin of an installation whose setup is unfinished sees it. */
  async function start() {
    if (!auth()?.can('administer')) return;
    let state;
    try { state = await readState(); } catch (_) { return; }
    if (!state?.initial || state.initial.complete || active) return;
    open('initial', state, await readData());
  }

  /* My Account's Rerun Setup Wizard: this Admin's own rerun, resumed where it
   * stopped or started at Welcome; never the installation's setup. `opener`
   * ({mayLeave, leave}) is the surface it opens over, which a page outside
   * the wizard asks to step aside before it is shown. */
  async function rerun({opener = null} = {}) {
    if (active) return;
    const state = await readState();
    if (!state.initial.complete) { open('initial', state, await readData(), opener); return; }
    const current = state.rerun ? state : await request('PUT', '/setup/rerun', {phase: 'welcome'});
    open('rerun', current, await readData(), opener);
  }

  window.DPSetupWizard = Object.freeze({rerun});

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => { void start(); }, {once: true});
  else void start();
})();
