/* DebridPulse v1.0.11 deterministic presentation-runtime loader.
 *
 * app.js and parser-deferred core runtimes have already executed before this
 * file is loaded. Presentation layers are then loaded one at a time in an
 * explicit order. A missing presentation asset is logged and skipped rather
 * than preventing the remaining UI layers or core application from running.
 */
(function () {
  'use strict';

  const STYLES = Object.freeze([
    {href: '/ui-settings-downloads-completion.css?v=4', marker: 'data-dp-settings-downloads-completion-style'},
    {href: '/ui-settings-password-layout-followup.css?v=1', marker: 'data-dp-settings-password-layout-followup-style'},
    {href: '/ui-settings-aria2-live.css?v=3', marker: 'data-dp-settings-aria2-live-style'},
    {href: '/ui-settings-maintenance-wipe.css?v=3', marker: 'data-dp-settings-maintenance-wipe-style'},
    {href: '/ui-settings-notifications.css?v=2', marker: 'data-dp-settings-notifications-style'},
    {href: '/ui-settings-authentication.css?v=1', marker: 'data-dp-settings-authentication-style'},
    {href: '/ui-settings-authentication-polish.css?v=1', marker: 'data-dp-settings-authentication-polish-style'},
  ]);

  const RUNTIMES = Object.freeze([
    {src: '/ui-shell-runtime.js?v=1', marker: 'data-dp-shell-runtime'},
    {src: '/ui-visual-behavior-fixes.js?v=23', marker: 'data-dp-visual-behavior-fixes'},
    {src: '/ui-statistics-orchestrator.js?v=1', marker: 'data-dp-statistics-orchestrator'},
    {src: '/ui-statistics-batch3.js?v=3', marker: 'data-dp-statistics-batch3'},
    {src: '/ui-statistics-batch4.js?v=2', marker: 'data-dp-statistics-batch4'},
    {src: '/ui-statistics-batch5.js?v=7', marker: 'data-dp-statistics-batch5'},
    {src: '/ui-settings-page.js?v=4', marker: 'data-dp-settings-page'},
    {src: '/ui-settings-maintenance-wipe.js?v=3', marker: 'data-dp-settings-maintenance-wipe'},
    {src: '/ui-settings-notifications.js?v=2', marker: 'data-dp-settings-notifications'},
    {src: '/ui-settings-authentication.js?v=1', marker: 'data-dp-settings-authentication'},
    {src: '/ui-settings-authentication-polish.js?v=1', marker: 'data-dp-settings-authentication-polish'},
    {src: '/ui-settings-downloads-completion.js?v=4&statefix=1', marker: 'data-dp-settings-downloads-completion'},
    {src: '/ui-settings-aria2-live.js?v=5', marker: 'data-dp-settings-aria2-live'},
    {src: '/ui-error-semantics.js?v=21', marker: 'data-dp-error-semantics'},
  ]);

  function alreadyLoaded(runtime) {
    return Boolean(document.querySelector('script[' + runtime.marker + ']'));
  }

  function styleAlreadyLoaded(style) {
    return Boolean(document.querySelector('link[' + style.marker + ']'));
  }

  function loadStyle(style) {
    if (styleAlreadyLoaded(style)) return Promise.resolve();

    return new Promise(function (resolve, reject) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = style.href;
      link.setAttribute(style.marker, '1');
      link.onload = function () { resolve(); };
      link.onerror = function () {
        reject(new Error('Unable to load ' + style.href));
      };
      document.head.appendChild(link);
    });
  }

  function loadRuntime(runtime) {
    if (alreadyLoaded(runtime)) return Promise.resolve();

    return new Promise(function (resolve, reject) {
      const script = document.createElement('script');
      script.src = runtime.src;
      script.async = false;
      script.setAttribute(runtime.marker, '1');
      script.onload = function () { resolve(); };
      script.onerror = function () {
        reject(new Error('Unable to load ' + runtime.src));
      };
      document.head.appendChild(script);
    });
  }

  async function loadPresentationRuntimes() {
    if (document.documentElement.dataset.dpPresentationLoaderStarted === '1') return;
    document.documentElement.dataset.dpPresentationLoaderStarted = '1';

    for (const style of STYLES) {
      try {
        await loadStyle(style);
      } catch (error) {
        console.error('[DebridPulse] presentation style skipped:', style.href, error);
        continue;
      }
    }

    for (const runtime of RUNTIMES) {
      try {
        await loadRuntime(runtime);
      } catch (error) {
        console.error('[DebridPulse] presentation runtime skipped:', runtime.src, error);
        continue;
      }
    }

    document.documentElement.dataset.dpPresentationLoaderReady = '1';
    document.dispatchEvent(new CustomEvent('debridpulse:presentation-ready'));
  }

  void loadPresentationRuntimes();
})();
