/* DebridPulse v1.0.11 Settings presentation shell.
 *
 * Presentation only. Settings information architecture remains authoritative for
 * control ownership and placement. This runtime loads after that architecture
 * layer and composes the reviewed master-card + persistent footer-card form.
 *
 * Lifecycle contract: no MutationObservers. The architecture runtime already
 * owns one explicit renderSettings hook. This layer wraps that hardened hook
 * once, composes after each legitimate Settings render, and performs one initial
 * composition in case Settings rendered before the presentation loader arrived.
 */
(function () {
  'use strict';

  const EXPECTED_TABS = Object.freeze([
    'tab-general',
    'tab-download',
    'tab-extract',
    'tab-notifications',
    'tab-authentication',
    'tab-database',
    'tab-advanced',
  ]);

  function loadStyles() {
    if (document.querySelector('link[data-dp-settings-presentation]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/ui-settings-presentation.css?v=2';
    link.dataset.dpSettingsPresentation = '1';
    document.head.appendChild(link);
  }

  function buildIdentity() {
    const identity = document.createElement('div');
    identity.className = 'dp-settings-master__identity';

    const icon = document.createElement('div');
    icon.className = 'dp-settings-master__icon-frame';
    icon.setAttribute('aria-hidden', 'true');
    icon.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.38a2 2 0 0 0-.73-2.73l-.15-.09a2 2 0 0 1-1-1.74v-.51a2 2 0 0 1 1-1.72l.15-.1a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2Z"/><circle cx="12" cy="12" r="3"/></svg>';

    const copy = document.createElement('div');
    copy.className = 'dp-settings-master__copy';

    const title = document.createElement('div');
    title.className = 'dp-settings-master__title';
    title.textContent = 'Settings';

    const subtitle = document.createElement('div');
    subtitle.className = 'dp-settings-master__subtitle';
    subtitle.textContent = 'Configure providers, downloads, notifications, and system behavior.';

    copy.appendChild(title);
    copy.appendChild(subtitle);
    identity.appendChild(icon);
    identity.appendChild(copy);
    return identity;
  }

  function settingsReadyForPresentation() {
    const tabs = document.getElementById('settings-tabs');
    const form = document.getElementById('settings-form');
    if (!tabs || !form) return false;

    const tabIds = Array.from(tabs.querySelectorAll('.stab[data-tab]'))
      .map(function (tab) { return tab.dataset.tab; });

    return EXPECTED_TABS.every(function (id) { return tabIds.includes(id); })
      && EXPECTED_TABS.every(function (id) { return !!form.querySelector('#' + id); })
      && !!form.querySelector('.dp-settings-ia-card');
  }

  function syncTabSelection(tabs) {
    if (!tabs) return;
    tabs.querySelectorAll('.stab').forEach(function (tab) {
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', tab.classList.contains('active') ? 'true' : 'false');
    });
  }

  function installTabInteraction(tabs) {
    if (!tabs || tabs.dataset.dpSettingsPresentationClick === '1') return;
    tabs.dataset.dpSettingsPresentationClick = '1';
    tabs.addEventListener('click', function (event) {
      if (!event.target.closest('.stab')) return;
      queueMicrotask(function () { syncTabSelection(tabs); });
    });
  }

  function ensureMaster(view, tabs, form, saveBar) {
    let master = document.getElementById('dp-settings-master');
    if (!master) {
      master = document.createElement('section');
      master.id = 'dp-settings-master';
      master.className = 'dp-card dp-settings-master';
      master.setAttribute('aria-label', 'Settings configuration');

      const header = document.createElement('header');
      header.className = 'dp-settings-master__header';
      header.appendChild(buildIdentity());

      const balance = document.createElement('div');
      balance.className = 'dp-settings-master__balance';
      balance.setAttribute('aria-hidden', 'true');

      const body = document.createElement('div');
      body.className = 'dp-settings-master__body';

      master.appendChild(header);
      master.appendChild(body);
      view.insertBefore(master, saveBar);
      header.appendChild(tabs);
      header.appendChild(balance);
      body.appendChild(form);
      return master;
    }

    const header = master.querySelector('.dp-settings-master__header');
    const body = master.querySelector('.dp-settings-master__body');
    if (header && tabs.parentElement !== header) {
      const balance = header.querySelector('.dp-settings-master__balance');
      header.insertBefore(tabs, balance || null);
    }
    if (body && form.parentElement !== body) body.appendChild(form);
    if (master.parentElement !== view) view.insertBefore(master, saveBar);
    return master;
  }

  function normalizeFooter(view, saveBar, master) {
    saveBar.classList.add('dp-card', 'dp-settings-footer');
    saveBar.setAttribute('aria-label', 'Settings actions');

    /* Keep the persistent action card as a sibling below the master card. */
    if (saveBar.parentElement !== view || master.nextElementSibling !== saveBar) {
      view.insertBefore(saveBar, master.nextElementSibling);
    }
  }

  function composeSettingsPresentation() {
    const view = document.getElementById('view-settings');
    const tabs = document.getElementById('settings-tabs');
    const form = document.getElementById('settings-form');
    const saveBar = view && view.querySelector('.save-bar');
    if (!view || !tabs || !form || !saveBar || !settingsReadyForPresentation()) return false;

    const master = ensureMaster(view, tabs, form, saveBar);
    normalizeFooter(view, saveBar, master);

    tabs.classList.add('dp-settings-master__tabs');
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('aria-label', 'Settings sections');
    syncTabSelection(tabs);
    installTabInteraction(tabs);

    view.classList.add('dp-settings-presented');
    view.dataset.dpSettingsPresentation = '1';
    return true;
  }

  function installSettingsPresentationHook() {
    const previous = window.renderSettings;
    if (typeof previous !== 'function') {
      console.error('[DebridPulse] Settings presentation not installed: renderSettings is unavailable.');
      return false;
    }
    if (previous.dpSettingsPresentation === '1') return true;
    if (previous.dpSettingsArchitecture !== '1') {
      console.error('[DebridPulse] Settings presentation not installed: architecture lifecycle owner is unavailable.');
      return false;
    }

    const wrapped = function () {
      const result = previous.apply(this, arguments);
      composeSettingsPresentation();
      return result;
    };
    wrapped.dpSettingsArchitecture = '1';
    wrapped.dpSettingsPresentation = '1';
    window.renderSettings = wrapped;
    return true;
  }

  function initialize() {
    loadStyles();
    installSettingsPresentationHook();
    /* Settings can already be rendered when the sequential presentation loader
       reaches this runtime. Compose that generation once; future generations
       flow through the explicit renderSettings lifecycle above. */
    composeSettingsPresentation();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialize, {once: true});
  } else {
    initialize();
  }
})();
