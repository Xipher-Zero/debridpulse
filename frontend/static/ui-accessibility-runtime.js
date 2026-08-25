/* DebridPulse v1.0.11 cross-cutting interaction accessibility.
 *
 * Presentation/interaction semantics only. This module does not call the API,
 * alter transfer state, or replace established app.js behavior; it makes the
 * inherited clickable-div surfaces keyboard-operable and keeps ARIA state in
 * sync with the existing active-class contract.
 */
(function () {
  'use strict';

  function bindKeyboardActivation(element) {
    if (!element || element.dataset.dpKeyboardActivation === '1') return;
    element.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      element.click();
    });
    element.dataset.dpKeyboardActivation = '1';
  }

  function syncNavigationState() {
    document.querySelectorAll('#sidebar .nav-item[data-view]').forEach(function (item) {
      item.setAttribute('role', 'button');
      item.tabIndex = 0;
      bindKeyboardActivation(item);
      if (item.classList.contains('active')) item.setAttribute('aria-current', 'page');
      else item.removeAttribute('aria-current');
    });
  }

  function installNavigationSemantics() {
    const navHost = document.querySelector('#sidebar nav');
    if (!navHost) return;
    syncNavigationState();

    if (navHost.dataset.dpA11yObserved !== '1') {
      navHost.dataset.dpA11yObserved = '1';
      new MutationObserver(syncNavigationState).observe(navHost, {
        subtree: true,
        attributes: true,
        attributeFilter: ['class']
      });
    }
  }

  function normalizeActivityNaming() {
    const navLabel = document.querySelector('#sidebar .nav-item[data-view="events"] .nav-label');
    if (navLabel && navLabel.textContent.trim() === 'Event Log') navLabel.textContent = 'Activity Log';

    const cardTitle = document.querySelector('#view-events .card-title');
    if (cardTitle && cardTitle.textContent.trim() === 'Event Log') cardTitle.textContent = 'Activity Log';

    const pageTitle = document.getElementById('page-title');
    if (pageTitle && pageTitle.textContent.trim() === 'Event Log') pageTitle.textContent = 'Activity Log';
  }

  function installNavigationNamingHook() {
    if (typeof window.nav !== 'function' || window.nav.dpActivityNaming === '1') return;
    const previous = window.nav;
    const wrapped = function () {
      const result = previous.apply(this, arguments);
      normalizeActivityNaming();
      syncNavigationState();
      return result;
    };
    wrapped.dpActivityNaming = '1';
    window.nav = wrapped;
  }

  function syncFilterGroup(group, label) {
    if (!group) return;
    group.setAttribute('role', 'group');
    if (label) group.setAttribute('aria-label', label);

    group.querySelectorAll('.ftab').forEach(function (control) {
      control.setAttribute('role', 'button');
      control.tabIndex = 0;
      control.setAttribute('aria-pressed', control.classList.contains('active') ? 'true' : 'false');
      control.removeAttribute('aria-selected');
      bindKeyboardActivation(control);
    });
  }

  function observeFilterGroup(group, label) {
    if (!group) return;
    syncFilterGroup(group, label);
    if (group.dataset.dpA11yObserved === '1') return;
    group.dataset.dpA11yObserved = '1';
    new MutationObserver(function () {
      syncFilterGroup(group, label);
    }).observe(group, {
      subtree: true,
      attributes: true,
      attributeFilter: ['class']
    });
  }

  function installFilterSemantics() {
    observeFilterGroup(
      document.querySelector('#view-torrents .filter-tabs'),
      'Download status filter'
    );
    observeFilterGroup(
      document.getElementById('stats-period-tabs'),
      'Statistics period'
    );
  }

  function tabControls(tablist) {
    return Array.from(tablist ? tablist.querySelectorAll('.stab') : []);
  }

  function syncTablist(tablist, label) {
    if (!tablist) return;
    tablist.setAttribute('role', 'tablist');
    if (label) tablist.setAttribute('aria-label', label);

    const tabs = tabControls(tablist);
    tabs.forEach(function (tab) {
      const selected = tab.classList.contains('active');
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', selected ? 'true' : 'false');
      tab.tabIndex = selected ? 0 : -1;

      const helpId = tab.dataset.htab;
      if (helpId && document.getElementById('htab-' + helpId)) {
        tab.setAttribute('aria-controls', 'htab-' + helpId);
      }

      if (tab.dataset.dpTabKeys !== '1') {
        tab.addEventListener('keydown', function (event) {
          const currentTabs = tabControls(tablist);
          const index = currentTabs.indexOf(tab);
          if (index < 0 || currentTabs.length === 0) return;

          let target = null;
          if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
            target = currentTabs[(index + 1) % currentTabs.length];
          } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
            target = currentTabs[(index - 1 + currentTabs.length) % currentTabs.length];
          } else if (event.key === 'Home') {
            target = currentTabs[0];
          } else if (event.key === 'End') {
            target = currentTabs[currentTabs.length - 1];
          } else if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            tab.click();
            return;
          } else {
            return;
          }

          event.preventDefault();
          target.focus();
          target.click();
        });
        tab.dataset.dpTabKeys = '1';
      }
    });
  }

  function observeTablist(tablist, label) {
    if (!tablist) return;
    syncTablist(tablist, label);
    if (tablist.dataset.dpTabA11yObserved === '1') return;
    tablist.dataset.dpTabA11yObserved = '1';
    new MutationObserver(function () {
      syncTablist(tablist, label);
    }).observe(tablist, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['class']
    });
  }

  function installTabSemantics() {
    observeTablist(document.getElementById('help-tabs'), 'Help sections');
    observeTablist(document.getElementById('settings-tabs'), 'Settings sections');
  }

  function installDashboardErrorCardSemantics() {
    const card = document.getElementById('dash-error-card');
    if (!card) return;
    card.setAttribute('role', 'button');
    card.tabIndex = 0;
    card.setAttribute('aria-label', 'View downloads with errors');
    bindKeyboardActivation(card);
  }

  function installModalCloseSemantics() {
    const close = document.querySelector('#modal .modal-close');
    if (!close) return;
    close.setAttribute('role', 'button');
    close.tabIndex = 0;
    close.setAttribute('aria-label', 'Close details');
    bindKeyboardActivation(close);
  }

  function initializeAccessibilityContract() {
    installNavigationSemantics();
    installNavigationNamingHook();
    normalizeActivityNaming();
    installFilterSemantics();
    installTabSemantics();
    installDashboardErrorCardSemantics();
    installModalCloseSemantics();
  }

  initializeAccessibilityContract();
  document.addEventListener('DOMContentLoaded', initializeAccessibilityContract, {once: true});
})();
