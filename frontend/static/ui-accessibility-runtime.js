/* DebridPulse v1.0.11 cross-cutting interaction accessibility.
 *
 * Presentation/interaction semantics only. This module does not call the API,
 * alter transfer state, or replace established app.js behavior; it makes the
 * inherited clickable-div surfaces keyboard-operable, keeps ARIA state in
 * sync with the existing active-class contract, and normalizes a small number
 * of legacy presentation-only DOM details that cannot live in CSS alone.
 */
(function () {
  'use strict';

  let dpDropdownLayer = null;
  let dpActiveDropdown = null;
  let dpDropdownSequence = 0;

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

  function normalizeDownloadsLegacyPresentation() {
    const pagination = document.getElementById('torrent-pagination');
    if (!pagination) return;
    /* The old static markup painted a divider inline. The Downloads page now
       owns footer geometry and intentionally has no separator, so remove the
       legacy inline source instead of fighting it with a CSS !important. */
    pagination.style.removeProperty('border-top');
  }

  function normalizeProviderPremiumLabel() {
    const label = document.getElementById('lbl-premium');
    if (!label || label.querySelector('.dp-provider-premium-until')) return;

    const raw = (label.textContent || '').replace(/\s+/g, ' ').trim();
    const match = raw.match(/^Premium until (\d{2}\.\d{2}\.\d{4}) \((\d+) days(?: remaining)?\)$/i);
    if (!match) return;

    const until = document.createElement('span');
    until.className = 'dp-provider-premium-until';
    until.textContent = 'AllDebrid Premium until ' + match[1];

    const remaining = document.createElement('span');
    remaining.className = 'dp-provider-premium-days';
    remaining.textContent = '(' + match[2] + ' days remaining)';

    label.replaceChildren(until, remaining);
  }

  function installProviderStatusPresentation() {
    const label = document.getElementById('lbl-premium');
    if (!label) return;
    normalizeProviderPremiumLabel();

    if (label.dataset.dpProviderObserved === '1') return;
    label.dataset.dpProviderObserved = '1';
    new MutationObserver(normalizeProviderPremiumLabel).observe(label, {
      childList: true,
      subtree: true,
      characterData: true
    });
  }

  /* ── Universal select dropdowns ──────────────────────────────────────── */
  function shouldEnhanceSelect(select) {
    if (!select || select.tagName !== 'SELECT') return false;
    if (select.dataset.dpDropdownEnhanced === '1') return false;
    if (select.hasAttribute('data-dp-native-select')) return false;
    if (select.multiple) return false;
    if (Number(select.getAttribute('size') || 0) > 1) return false;
    return true;
  }

  function selectAccessibleName(select) {
    const explicit = select.getAttribute('aria-label');
    if (explicit) return explicit;

    const labelledBy = select.getAttribute('aria-labelledby');
    if (labelledBy) {
      const copy = labelledBy.split(/\s+/).map(function (id) {
        const node = document.getElementById(id);
        return node ? (node.textContent || '').trim() : '';
      }).filter(Boolean).join(' ');
      if (copy) return copy;
    }

    if (select.id) {
      const label = document.querySelector('label[for="' + CSS.escape(select.id) + '"]');
      if (label) {
        const copy = (label.textContent || '').trim();
        if (copy) return copy;
      }
    }

    return select.name || select.id || 'Select option';
  }

  function selectedOption(select) {
    return select && select.selectedIndex >= 0 ? select.options[select.selectedIndex] : null;
  }

  function syncProjectedSelect(select) {
    if (!select || select.dataset.dpDropdownEnhanced !== '1') return;
    const shell = select._dpDropdownShell;
    const trigger = select._dpDropdownTrigger;
    if (!shell || !trigger) return;

    const option = selectedOption(select);
    const value = trigger.querySelector('.dp-dropdown__value');
    if (value) value.textContent = option ? (option.label || option.textContent || '') : 'Select…';

    trigger.disabled = !!select.disabled;
    trigger.setAttribute('aria-disabled', select.disabled ? 'true' : 'false');
    trigger.setAttribute('aria-label', selectAccessibleName(select));

    const sourceDisplayNone = select.dataset.dpSourceDisplayNone === '1';
    const sourceHidden = select.hidden;
    shell.hidden = sourceHidden || sourceDisplayNone;

    if (dpActiveDropdown && dpActiveDropdown.select === select) {
      if (select.disabled || sourceHidden || sourceDisplayNone || !select.isConnected) {
        closeProjectedSelect(false);
      } else {
        renderProjectedOptions(select);
        positionProjectedMenu();
      }
    }
  }

  function projectedOptionButtons() {
    if (!dpDropdownLayer) return [];
    return Array.from(dpDropdownLayer.querySelectorAll('.dp-dropdown__option:not(:disabled)'));
  }

  function focusProjectedOption(preference) {
    const buttons = projectedOptionButtons();
    if (!buttons.length) return;

    let target = null;
    if (preference === 'last') {
      target = buttons[buttons.length - 1];
    } else if (preference === 'first') {
      target = buttons[0];
    } else {
      target = buttons.find(function (button) {
        return button.getAttribute('aria-selected') === 'true';
      }) || buttons[0];
    }
    target.focus({preventScroll: true});
  }

  function moveProjectedOption(delta) {
    const buttons = projectedOptionButtons();
    if (!buttons.length) return;
    const current = document.activeElement;
    let index = buttons.indexOf(current);
    if (index < 0) {
      index = buttons.findIndex(function (button) {
        return button.getAttribute('aria-selected') === 'true';
      });
    }
    if (index < 0) index = 0;
    const next = (index + delta + buttons.length) % buttons.length;
    buttons[next].focus({preventScroll: true});
  }

  function ensureDropdownLayer() {
    if (dpDropdownLayer && dpDropdownLayer.isConnected) return dpDropdownLayer;

    const layer = document.createElement('div');
    layer.id = 'dp-dropdown-layer';
    layer.className = 'dp-dropdown-menu';
    layer.setAttribute('role', 'listbox');
    layer.hidden = true;
    document.body.appendChild(layer);

    layer.addEventListener('keydown', function (event) {
      if (!dpActiveDropdown) return;
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        moveProjectedOption(1);
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        moveProjectedOption(-1);
      } else if (event.key === 'Home') {
        event.preventDefault();
        focusProjectedOption('first');
      } else if (event.key === 'End') {
        event.preventDefault();
        focusProjectedOption('last');
      } else if (event.key === 'Escape') {
        event.preventDefault();
        closeProjectedSelect(true);
      }
    });

    dpDropdownLayer = layer;
    return layer;
  }

  function optionIsDisabled(option) {
    if (!option) return true;
    const group = option.parentElement && option.parentElement.tagName === 'OPTGROUP'
      ? option.parentElement
      : null;
    return !!option.disabled || !!(group && group.disabled);
  }

  function chooseProjectedOption(select, optionIndex) {
    const option = select && select.options ? select.options[optionIndex] : null;
    if (!option || optionIsDisabled(option)) return;

    select.selectedIndex = optionIndex;
    select.dispatchEvent(new Event('input', {bubbles: true}));
    select.dispatchEvent(new Event('change', {bubbles: true}));
    syncProjectedSelect(select);
    closeProjectedSelect(true);
  }

  function appendProjectedOption(select, option, optionIndex, layer) {
    if (!option || option.hidden) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'dp-dropdown__option';
    button.setAttribute('role', 'option');
    button.dataset.dpOptionIndex = String(optionIndex);
    button.textContent = option.label || option.textContent || '';
    button.disabled = optionIsDisabled(option);
    button.setAttribute('aria-disabled', button.disabled ? 'true' : 'false');
    button.setAttribute('aria-selected', option.selected ? 'true' : 'false');
    button.addEventListener('click', function () {
      chooseProjectedOption(select, optionIndex);
    });
    layer.appendChild(button);
  }

  function renderProjectedOptions(select) {
    const layer = ensureDropdownLayer();
    layer.replaceChildren();
    const allOptions = Array.from(select.options || []);

    Array.from(select.children).forEach(function (child) {
      if (child.tagName === 'OPTGROUP') {
        const group = document.createElement('div');
        group.className = 'dp-dropdown__group';
        group.textContent = child.label || '';
        layer.appendChild(group);
        Array.from(child.children).forEach(function (option) {
          const index = allOptions.indexOf(option);
          if (index >= 0) appendProjectedOption(select, option, index, layer);
        });
      } else if (child.tagName === 'OPTION') {
        const index = allOptions.indexOf(child);
        if (index >= 0) appendProjectedOption(select, child, index, layer);
      }
    });

    if (!layer.querySelector('.dp-dropdown__option')) {
      const empty = document.createElement('div');
      empty.className = 'dp-dropdown__empty';
      empty.textContent = 'No options available';
      layer.appendChild(empty);
    }
  }

  function positionProjectedMenu() {
    if (!dpActiveDropdown || !dpDropdownLayer || dpDropdownLayer.hidden) return;
    const trigger = dpActiveDropdown.trigger;
    if (!trigger || !trigger.isConnected) {
      closeProjectedSelect(false);
      return;
    }

    const rect = trigger.getBoundingClientRect();
    const viewportWidth = document.documentElement.clientWidth || window.innerWidth;
    const viewportHeight = document.documentElement.clientHeight || window.innerHeight;
    const edge = 8;
    const gap = 6;
    const width = rect.width;

    /* Form/list selects obey the master geometry rule: menu width equals the
       trigger width exactly. Only viewport clamping can move the menu. */
    dpDropdownLayer.style.width = width + 'px';
    dpDropdownLayer.style.maxWidth = Math.max(0, viewportWidth - edge * 2) + 'px';
    dpDropdownLayer.style.left = Math.min(
      Math.max(edge, rect.left),
      Math.max(edge, viewportWidth - edge - width)
    ) + 'px';
    dpDropdownLayer.style.top = edge + 'px';

    const menuHeight = dpDropdownLayer.getBoundingClientRect().height;
    const below = viewportHeight - rect.bottom - gap - edge;
    const above = rect.top - gap - edge;
    const openAbove = menuHeight > below && above > below;
    const desiredTop = openAbove
      ? rect.top - gap - menuHeight
      : rect.bottom + gap;

    dpDropdownLayer.style.top = Math.min(
      Math.max(edge, desiredTop),
      Math.max(edge, viewportHeight - edge - menuHeight)
    ) + 'px';
  }

  function closeProjectedSelect(restoreFocus) {
    if (!dpActiveDropdown) return;
    const active = dpActiveDropdown;
    dpActiveDropdown = null;

    if (active.trigger) active.trigger.setAttribute('aria-expanded', 'false');
    if (dpDropdownLayer) {
      dpDropdownLayer.hidden = true;
      dpDropdownLayer.replaceChildren();
    }

    if (restoreFocus && active.trigger && active.trigger.isConnected) {
      active.trigger.focus({preventScroll: true});
    }
  }

  function openProjectedSelect(select, focusPreference) {
    if (!select || select.disabled) return;
    const trigger = select._dpDropdownTrigger;
    if (!trigger) return;

    if (dpActiveDropdown && dpActiveDropdown.select === select) {
      closeProjectedSelect(true);
      return;
    }
    if (dpActiveDropdown) closeProjectedSelect(false);

    syncProjectedSelect(select);
    renderProjectedOptions(select);
    dpActiveDropdown = {select: select, trigger: trigger};
    trigger.setAttribute('aria-expanded', 'true');
    ensureDropdownLayer().hidden = false;
    positionProjectedMenu();
    focusProjectedOption(focusPreference || 'selected');
  }

  function copySelectGeometry(select, shell) {
    const source = select.style;
    const computed = window.getComputedStyle(select);
    const rect = select.getBoundingClientRect();
    const parentRect = select.parentElement ? select.parentElement.getBoundingClientRect() : null;
    const properties = [
      'minWidth', 'maxWidth',
      'height', 'minHeight', 'maxHeight',
      'marginTop', 'marginRight', 'marginBottom', 'marginLeft',
      'flex', 'flexGrow', 'flexShrink', 'flexBasis', 'alignSelf'
    ];
    properties.forEach(function (property) {
      if (source[property]) shell.style[property] = source[property];
    });

    /* The projected control must preserve the geometry the native select had
       before it is visually hidden. Full-width form controls remain fluid;
       bounded/filter selects retain their actual rendered width instead of
       inheriting an accidental 100% wrapper width. */
    if (source.width) {
      shell.style.width = source.width;
    } else if (Number.isFinite(rect.width) && rect.width > 0) {
      const parentWidth = parentRect && Number.isFinite(parentRect.width) ? parentRect.width : 0;
      const fillsParent = parentWidth > 0 && rect.width >= parentWidth - 2;
      shell.style.width = fillsParent ? '100%' : rect.width + 'px';
    }

    if (!source.maxWidth && computed.maxWidth && computed.maxWidth !== 'none') {
      shell.style.maxWidth = computed.maxWidth;
    }
    if (!source.minWidth && computed.minWidth && computed.minWidth !== 'auto') {
      shell.style.minWidth = computed.minWidth;
    }

    const height = parseFloat(computed.height);
    const fontSize = parseFloat(computed.fontSize);
    if ((Number.isFinite(height) && height <= 32) ||
        (Number.isFinite(fontSize) && fontSize <= 12 && (source.width || rect.width > 0))) {
      shell.classList.add('dp-dropdown-shell--compact');
    }
  }

  function enhanceSelect(select) {
    if (!shouldEnhanceSelect(select)) return;

    const shell = document.createElement('span');
    shell.className = 'dp-dropdown-shell';
    copySelectGeometry(select, shell);

    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'dp-dropdown__trigger';
    trigger.setAttribute('role', 'combobox');
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute('aria-controls', 'dp-dropdown-layer');

    const value = document.createElement('span');
    value.className = 'dp-dropdown__value';
    const chevron = document.createElement('span');
    chevron.className = 'dp-dropdown__chevron';
    chevron.setAttribute('aria-hidden', 'true');
    trigger.append(value, chevron);
    shell.appendChild(trigger);
    select.insertAdjacentElement('afterend', shell);

    select.dataset.dpDropdownEnhanced = '1';
    select.dataset.dpSourceDisplayNone = select.style.display === 'none' ? '1' : '0';
    select.classList.add('dp-native-select--enhanced');
    select.tabIndex = -1;
    select.setAttribute('aria-hidden', 'true');
    select._dpDropdownShell = shell;
    select._dpDropdownTrigger = trigger;

    trigger.addEventListener('click', function () {
      openProjectedSelect(select, 'selected');
    });
    trigger.addEventListener('keydown', function (event) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        if (!dpActiveDropdown || dpActiveDropdown.select !== select) openProjectedSelect(select, 'selected');
        else moveProjectedOption(1);
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        if (!dpActiveDropdown || dpActiveDropdown.select !== select) openProjectedSelect(select, 'selected');
        else moveProjectedOption(-1);
      } else if (event.key === 'Escape' && dpActiveDropdown && dpActiveDropdown.select === select) {
        event.preventDefault();
        closeProjectedSelect(true);
      } else if ((event.key === 'Enter' || event.key === ' ') &&
                 (!dpActiveDropdown || dpActiveDropdown.select !== select)) {
        event.preventDefault();
        openProjectedSelect(select, 'selected');
      }
    });

    /* Preserve label[for] behavior: programmatic focus of the authoritative
       native select is redirected to the visible projected trigger. */
    select.addEventListener('focus', function () {
      trigger.focus({preventScroll: true});
    });
    select.addEventListener('change', function () {
      syncProjectedSelect(select);
    });
    select.addEventListener('input', function () {
      syncProjectedSelect(select);
    });

    new MutationObserver(function (records) {
      records.forEach(function (record) {
        if (record.type === 'attributes' && record.target === select && record.attributeName === 'style') {
          select.dataset.dpSourceDisplayNone = select.style.display === 'none' ? '1' : '0';
        }
      });
      syncProjectedSelect(select);
    }).observe(select, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['disabled', 'hidden', 'style', 'selected', 'label', 'value']
    });

    syncProjectedSelect(select);
  }

  function enhanceSelectTree(root) {
    if (!root) return;
    if (root.nodeType === Node.ELEMENT_NODE && root.matches && root.matches('select')) {
      enhanceSelect(root);
    }
    if (root.querySelectorAll) root.querySelectorAll('select').forEach(enhanceSelect);
  }

  function installUniversalSelectDropdowns() {
    if (!document.body) return;
    enhanceSelectTree(document);

    if (document.documentElement.dataset.dpDropdownObserver !== '1') {
      document.documentElement.dataset.dpDropdownObserver = '1';
      new MutationObserver(function (records) {
        records.forEach(function (record) {
          record.addedNodes.forEach(enhanceSelectTree);
        });
      }).observe(document.body, {childList: true, subtree: true});
    }

    if (document.documentElement.dataset.dpDropdownGlobalEvents !== '1') {
      document.documentElement.dataset.dpDropdownGlobalEvents = '1';
      document.addEventListener('pointerdown', function (event) {
        if (!dpActiveDropdown) return;
        const trigger = dpActiveDropdown.trigger;
        if ((dpDropdownLayer && dpDropdownLayer.contains(event.target)) ||
            (trigger && trigger.contains(event.target))) return;
        closeProjectedSelect(false);
      }, true);
      window.addEventListener('resize', positionProjectedMenu, {passive: true});
      window.addEventListener('scroll', positionProjectedMenu, {passive: true, capture: true});
    }

    window.DPDropdowns = {
      refresh: function () { enhanceSelectTree(document); },
      close: function () { closeProjectedSelect(false); }
    };
  }

  function initializeAccessibilityContract() {
    installNavigationSemantics();
    installNavigationNamingHook();
    normalizeActivityNaming();
    installFilterSemantics();
    installTabSemantics();
    installDashboardErrorCardSemantics();
    installModalCloseSemantics();
    normalizeDownloadsLegacyPresentation();
    installProviderStatusPresentation();
    installUniversalSelectDropdowns();
  }

  initializeAccessibilityContract();
  document.addEventListener('DOMContentLoaded', initializeAccessibilityContract, {once: true});
})();
