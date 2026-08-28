/* DebridPulse v1.0.11 Settings presentation shell.
 *
 * Presentation only: wraps the existing reviewed Settings information
 * architecture in one shared master card. The page-level Settings title remains
 * outside the card; the card header owns the identity block plus the centered
 * section rail, while #settings-form remains the authoritative settings body.
 *
 * IMPORTANT: app.js renders Settings lazily when the view is opened. Do not
 * compose the master card around the initially-empty #settings-tabs / form
 * placeholders. Wait until the Settings IA pass has rebuilt the reviewed tabs
 * and subsection cards, then compose the shell. Keep observing so later
 * renderSettings() refreshes are re-composed after the IA pass completes.
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

  let compositionScheduled = false;
  let viewObserver = null;

  function loadStyles() {
    if (document.querySelector('link[data-dp-settings-presentation]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/ui-settings-presentation.css?v=1';
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

  function normalizeTabSemantics(tabs) {
    if (!tabs) return;
    tabs.classList.add('dp-settings-master__tabs');
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('aria-label', 'Settings sections');

    tabs.querySelectorAll('.stab').forEach(function (tab) {
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', tab.classList.contains('active') ? 'true' : 'false');
    });
  }

  function syncTabSelection(tabs) {
    if (!tabs) return;
    tabs.querySelectorAll('.stab').forEach(function (tab) {
      tab.setAttribute('aria-selected', tab.classList.contains('active') ? 'true' : 'false');
    });
  }

  function settingsReadyForPresentation() {
    const tabs = document.getElementById('settings-tabs');
    const form = document.getElementById('settings-form');
    if (!tabs || !form) return false;

    const tabIds = Array.from(tabs.querySelectorAll('.stab[data-tab]'))
      .map(function (tab) { return tab.dataset.tab; });

    const hasExpectedTabs = EXPECTED_TABS.every(function (id) {
      return tabIds.includes(id);
    });
    const hasExpectedPanels = EXPECTED_TABS.every(function (id) {
      return !!form.querySelector('#' + id);
    });
    const iaComposed = !!form.querySelector('.dp-settings-ia-card');

    return hasExpectedTabs && hasExpectedPanels && iaComposed;
  }

  function buildMaster() {
    const view = document.getElementById('view-settings');
    const tabs = document.getElementById('settings-tabs');
    const form = document.getElementById('settings-form');
    if (!view || !tabs || !form || !settingsReadyForPresentation()) return false;

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

      const saveBar = view.querySelector(':scope > .save-bar');
      view.insertBefore(master, saveBar || null);
      header.appendChild(tabs);
      header.appendChild(balance);
      body.appendChild(form);
    } else {
      const header = master.querySelector('.dp-settings-master__header');
      const body = master.querySelector('.dp-settings-master__body');
      if (header && tabs.parentElement !== header) {
        const balance = header.querySelector('.dp-settings-master__balance');
        header.insertBefore(tabs, balance || null);
      }
      if (body && form.parentElement !== body) body.appendChild(form);
    }

    view.classList.add('dp-settings-presented');
    normalizeTabSemantics(tabs);
    syncTabSelection(tabs);
    view.dataset.dpSettingsPresentation = '1';
    return true;
  }

  function installTabObserver() {
    const tabs = document.getElementById('settings-tabs');
    if (!tabs || tabs.dataset.dpSettingsPresentationObserved === '1') return;
    tabs.dataset.dpSettingsPresentationObserved = '1';
    new MutationObserver(function () {
      normalizeTabSemantics(tabs);
      syncTabSelection(tabs);
    }).observe(tabs, {childList: true, subtree: true, attributes: true, attributeFilter: ['class']});
  }

  function composeWhenReady() {
    compositionScheduled = false;
    if (!settingsReadyForPresentation()) return;
    if (buildMaster()) installTabObserver();
  }

  function scheduleComposition() {
    if (compositionScheduled) return;
    compositionScheduled = true;
    setTimeout(composeWhenReady, 0);
  }

  function installViewObserver() {
    const view = document.getElementById('view-settings');
    if (!view || viewObserver) return;
    viewObserver = new MutationObserver(function () {
      scheduleComposition();
    });
    viewObserver.observe(view, {childList: true, subtree: true});
  }

  function boot() {
    loadStyles();
    installViewObserver();
    scheduleComposition();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, {once: true});
  } else {
    boot();
  }
})();
