/* DebridPulse v1.0.12 cross-cutting accessibility and dropdown semantics.
 *
 * This module does not call the API, alter transfer state, replace established
 * app.js behavior, or repair canonical presentation. It makes inherited
 * clickable controls keyboard-operable, keeps ARIA state synchronized with the
 * active-class contract, and projects native-select dropdown semantics for
 * dynamically rendered controls.
 *
 * A select whose list is too long to scan declares data-dp-dropdown-search
 * (the search field's accessible name): its open menu adds that field above
 * the list and filters the options as the operator types. An option may carry
 * data-dp-detail (a quieter second label, shown and searched) and
 * data-dp-search (further words it answers to). The native select stays the
 * only value and event source; searching never changes it.
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
    syncNavigationState();
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


  function installFilterSemantics() {
    syncFilterGroup(
      document.querySelector('#view-torrents .filter-tabs'),
      'Download status filter'
    );
    syncFilterGroup(
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


  function installTabSemantics() {
    syncTablist(document.getElementById('help-tabs'), 'Help sections');
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

  function searchable(select) {
    return !!(select && select.dataset.dpDropdownSearch);
  }

  function projectedSearch() {
    return dpDropdownLayer ? dpDropdownLayer.querySelector('.dp-dropdown__search') : null;
  }

  /* Focus never scrolls the page, but the focused option must still be in
     view inside the menu's own scrolling list. */
  function revealProjectedOption(button) {
    const list = button.parentElement;
    if (!list) return;
    const box = list.getBoundingClientRect();
    const item = button.getBoundingClientRect();
    if (item.top < box.top) list.scrollTop -= box.top - item.top;
    else if (item.bottom > box.bottom) list.scrollTop += item.bottom - box.bottom;
  }

  function focusOption(button) {
    button.focus({preventScroll: true});
    revealProjectedOption(button);
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
    focusOption(target);
  }

  function moveProjectedOption(delta) {
    const buttons = projectedOptionButtons();
    if (!buttons.length) return;
    const current = document.activeElement;
    let index = buttons.indexOf(current);
    // Up from the first option of a searchable menu returns to its search.
    const search = projectedSearch();
    if (search && index === 0 && delta < 0) {
      search.focus({preventScroll: true});
      return;
    }
    if (index < 0) {
      index = buttons.findIndex(function (button) {
        return button.getAttribute('aria-selected') === 'true';
      });
    }
    if (index < 0) index = 0;
    const next = (index + delta + buttons.length) % buttons.length;
    focusOption(buttons[next]);
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
      } else {
        // Typing on an option of a searchable menu types into its search.
        const search = projectedSearch();
        if (search && event.target !== search && event.key.length === 1 &&
            !event.ctrlKey && !event.metaKey && !event.altKey) {
          search.focus({preventScroll: true});
        }
      }
    });

    dpDropdownLayer = layer;
    return layer;
  }

  function fold(text) {
    return String(text || '').normalize('NFD').replace(/\p{M}/gu, '').toLocaleLowerCase('en').trim();
  }

  /* How well an option answers a search: 0 an exact label, detail or search
     word; 1 a word of one starts with it; 2 one contains it; -1 no match. */
  function searchRank(option, query) {
    const texts = [option.label || option.textContent, option.dataset.dpDetail, option.dataset.dpSearch].map(fold);
    const words = texts.join(' ').split(/[^\p{L}\p{N}-]+/u).filter(Boolean);
    if (texts.includes(query) || words.includes(query)) return 0;
    if (words.some(function (word) { return word.startsWith(query); }) ||
        texts.some(function (text) { return text.startsWith(query); })) return 1;
    return texts.some(function (text) { return text.includes(query); }) ? 2 : -1;
  }

  function projectedSearchField(select) {
    const search = document.createElement('input');
    search.type = 'search';
    search.className = 'input dp-dropdown__search';
    search.autocomplete = 'off';
    search.spellcheck = false;
    search.placeholder = select.dataset.dpDropdownSearch;
    search.setAttribute('aria-label', select.dataset.dpDropdownSearch);
    search.setAttribute('aria-controls', 'dp-dropdown-list');
    search.addEventListener('input', function () {
      if (!dpActiveDropdown) return;
      renderProjectedOptions(dpActiveDropdown.select);
      const list = search.nextElementSibling;
      if (list) list.scrollTop = 0;
    });
    search.addEventListener('keydown', function (event) {
      if (!dpActiveDropdown) return;
      const buttons = projectedOptionButtons();
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        // The list is entered from its search; the caret keys stay the field's.
        event.preventDefault();
        event.stopPropagation();
        if (buttons.length) focusOption(event.key === 'ArrowDown' ? buttons[0] : buttons[buttons.length - 1]);
      } else if (event.key === 'Home' || event.key === 'End') {
        event.stopPropagation();
      } else if (event.key === 'Enter') {
        // Enter takes the best match, the first option shown.
        event.preventDefault();
        if (buttons.length) buttons[0].click();
      }
    });
    return search;
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
    const label = option.label || option.textContent || '';
    const detail = option.dataset.dpDetail || '';
    if (detail) {
      const name = document.createElement('span');
      name.className = 'dp-dropdown__option-label';
      name.textContent = label;
      const quiet = document.createElement('span');
      quiet.className = 'dp-dropdown__option-detail';
      quiet.textContent = detail;
      button.append(name, quiet);
      button.setAttribute('aria-label', label + ', ' + detail);
    } else {
      button.textContent = label;
    }
    button.disabled = optionIsDisabled(option);
    button.setAttribute('aria-disabled', button.disabled ? 'true' : 'false');
    button.setAttribute('aria-selected', option.selected ? 'true' : 'false');
    button.addEventListener('click', function () {
      chooseProjectedOption(select, optionIndex);
    });
    layer.appendChild(button);
  }

  /* A plain menu is itself the listbox. A searchable one is its search field
     above a listbox of its own, which alone scrolls; the field (and what was
     typed into it) is kept while the list is redrawn. */
  function renderProjectedOptions(select) {
    const layer = ensureDropdownLayer();
    let list = layer;
    let query = '';
    if (searchable(select)) {
      let search = projectedSearch();
      list = layer.querySelector('#dp-dropdown-list');
      if (!search || !list) {
        search = projectedSearchField(select);
        list = document.createElement('div');
        list.id = 'dp-dropdown-list';
        list.className = 'dp-dropdown__list';
        list.setAttribute('role', 'listbox');
        list.setAttribute('aria-label', selectAccessibleName(select));
        layer.replaceChildren(search, list);
      }
      layer.classList.add('dp-dropdown-menu--search');
      layer.removeAttribute('role');
      query = fold(search.value);
    } else {
      layer.classList.remove('dp-dropdown-menu--search');
      layer.setAttribute('role', 'listbox');
    }
    list.replaceChildren();
    const allOptions = Array.from(select.options || []);

    if (query) {
      // Best matches first; equally good ones keep the select's own order.
      allOptions.map(function (option, index) {
        return {option: option, index: index, rank: searchRank(option, query)};
      }).filter(function (entry) {
        return entry.rank >= 0;
      }).sort(function (a, b) {
        return a.rank - b.rank || a.index - b.index;
      }).forEach(function (entry) {
        appendProjectedOption(select, entry.option, entry.index, list);
      });
    } else {
      Array.from(select.children).forEach(function (child) {
        if (child.tagName === 'OPTGROUP') {
          const group = document.createElement('div');
          group.className = 'dp-dropdown__group';
          group.textContent = child.label || '';
          list.appendChild(group);
          Array.from(child.children).forEach(function (option) {
            const index = allOptions.indexOf(option);
            if (index >= 0) appendProjectedOption(select, option, index, list);
          });
        } else if (child.tagName === 'OPTION') {
          const index = allOptions.indexOf(child);
          if (index >= 0) appendProjectedOption(select, child, index, list);
        }
      });
    }

    if (!list.querySelector('.dp-dropdown__option')) {
      const empty = document.createElement('div');
      empty.className = 'dp-dropdown__empty';
      empty.textContent = query ? 'No matches' : 'No options available';
      list.appendChild(empty);
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
    const search = projectedSearch();
    if (search) {
      // A searchable menu opens on its search, the current choice in view.
      const current = dpDropdownLayer.querySelector('.dp-dropdown__option[aria-selected="true"]');
      if (current) revealProjectedOption(current);
      search.focus({preventScroll: true});
    } else {
      focusProjectedOption(focusPreference || 'selected');
    }
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
    if (searchable(select)) shell.classList.add('dp-dropdown-shell--search');
    copySelectGeometry(select, shell);

    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'dp-dropdown__trigger';
    trigger.setAttribute('role', 'combobox');
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute('aria-controls', searchable(select) ? 'dp-dropdown-list' : 'dp-dropdown-layer');

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
      close: function () { closeProjectedSelect(false); },
      // A native value assigned programmatically raises no change/input event
      // and no attribute mutation: its owner redraws the projected label here.
      sync: function (select) { syncProjectedSelect(select); }
    };
  }


  function installAccessibilityLifecycle() {
    if (document.documentElement.dataset.dpAccessibilityLifecycle === '1') return;
    document.documentElement.dataset.dpAccessibilityLifecycle = '1';

    document.addEventListener('debridpulse:navigation', function () {
      queueMicrotask(function () {
        installNavigationSemantics();
        installFilterSemantics();
        installTabSemantics();
      });
    });
    document.addEventListener('debridpulse:downloads-rendered', installFilterSemantics);
    // A filter, a Help tab, or a period chosen from the handheld Statistics
    // menu (the period filters' own choice, ui-statistics.js).
    document.addEventListener('click', function (event) {
      if (!event.target.closest('.filter-tabs .ftab, .dp-help-tabs .stab, #stats-period-menu [role="option"]')) return;
      queueMicrotask(function () {
        installFilterSemantics();
        installTabSemantics();
      });
    });
  }

  function initializeAccessibilityContract() {
    installNavigationSemantics();
    installFilterSemantics();
    installTabSemantics();
    installDashboardErrorCardSemantics();
    installModalCloseSemantics();
    installUniversalSelectDropdowns();
    installAccessibilityLifecycle();
  }

  initializeAccessibilityContract();
  document.addEventListener('DOMContentLoaded', initializeAccessibilityContract, {once: true});
})();
