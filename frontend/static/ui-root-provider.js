/* DebridPulse 1.0.13 torrent root provider: the one live badge, picker and
 * switch action for Transfer Details (Files header), Recent Activity and
 * Downloads.
 *
 * The provider shown is the backend's committed ROOT route
 * (``route_provider_id``) -- a live route-state indicator, never an origin, a
 * child file's provider or a provider resource. The picker reads the
 * authoritative provider status fresh when it opens
 * (GET /api/torrents/{id}/route); list rows only carry a hint
 * (``route_switch_available``). Every surface switches through the ONE action
 * (POST /api/torrents/{id}/route), naming the provider the operator saw as
 * current: the backend refuses -- changing nothing -- when the route moved,
 * the provider cannot take the root now, or its capacity is not available.
 * Nothing here is shown as current until the backend committed it; on a
 * refusal the badge stays on the provider it was on.
 *
 * Interaction follows the existing candidate chooser: a non-modal dialog
 * anchored to its launcher, Escape / click-away to close, arrow keys between
 * actions, focus returned to the launcher. Choosing a provider closes the
 * picker at once: while the one switch request is outstanding every surface
 * keeps the committed provider and says "Switching to <provider>…" beside it
 * (re-rendered by the surfaces' ordinary refreshes), and that transfer takes
 * no second switch until the request settles.
 */
(function () {
  'use strict';

  const TRIGGER_ATTR = 'data-dp-root-provider-trigger';
  const MOUNT_SELECTOR = '[data-dp-root-provider-mount]';
  const STATUS_LABELS = {
    current: 'Current',
    prepared: 'Prepared',
    preparing: 'Preparing…',
    deferred: 'Deferred',
    available: 'Available',
    failed_earlier: 'Failed earlier',
    unavailable: 'Unavailable',
  };
  const REASON_LABELS = {
    disabled: 'Disabled',
    not_entitled: 'Not available on this account',
    entitlement_unknown: 'Account not confirmed yet',
    unhealthy: 'Temporarily unhealthy',
    declined: 'Declined this transfer',
    applicability_unknown: 'Support not confirmed yet',
    held: 'Held by another provider',
  };

  let menuEl = null;
  let menuTrigger = null;
  let menuSurface = null;
  let menuTransferId = null;
  let menuStatus = null;
  let detailTransferId = null;
  // transferId -> target provider label, while that transfer's one switch
  // request is outstanding.
  const switching = new Map();

  function esc(value) {
    if (typeof window.esc === 'function') return window.esc(value);
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function toast(payload, kind) {
    if (typeof window.toast === 'function') window.toast(payload, kind);
  }

  function providerLabel(name, id) {
    return String(name || id || 'Pending');
  }

  // A torrent root's badge: interactive only when a real alternative exists.
  // It always names the COMMITTED route; a switch in flight is said beside it.
  function badgeMarkup(item, surface) {
    const label = providerLabel(item && item.route_provider_name, item && item.route_provider_id);
    const known = Boolean(item && item.route_provider_id);
    const theme = known ? themeAttribute(item.route_provider_theme) : '';
    if (!known || !(item && item.route_switch_available)) {
      return '<span class="dp-provider-chip dp-root-provider-badge" data-provider-state="' +
        (known ? 'known' : 'pending') + '"' + theme + '>' + esc(label) + '</span>' + switchingMarkup(item && item.id);
    }
    return launcherMarkup(item.id, item.route_provider_id, label, surface, theme);
  }

  // The committed provider's declared badge theme, if it has one.
  function themeAttribute(theme) {
    return theme ? ' data-provider-theme="' + esc(theme) + '"' : '';
  }

  function switchingMarkup(transferId) {
    const target = transferId == null ? null : switching.get(Number(transferId));
    return target == null ? '' : '<span class="dp-root-provider-switching" role="status" ' +
      'data-dp-switching-transfer="' + esc(transferId) + '">' + esc('Switching to ' + target + '…') + '</span>';
  }

  function launcherMarkup(transferId, providerId, label, surface, theme) {
    const busy = switching.has(Number(transferId));
    return '<button type="button" class="dp-provider-chip dp-root-provider-launcher" ' + TRIGGER_ATTR + ' ' +
      'data-dp-transfer-id="' + esc(transferId) + '" data-dp-provider-id="' + esc(providerId) + '" ' +
      'data-dp-surface="' + esc(surface || '') + '" data-provider-state="known"' + (theme || '') + ' ' +
      'aria-haspopup="dialog" aria-expanded="false"' + (busy ? ' aria-disabled="true"' : '') + ' ' +
      'aria-label="' + esc('Provider: ' + label + (busy ? '. A provider switch is in progress' :
        '. Choose another provider')) + '">' +
      '<span class="dp-root-provider-name">' + esc(label) + '</span>' +
      '<span class="dp-root-provider-caret" aria-hidden="true"></span></button>' + switchingMarkup(transferId);
  }

  // Say the switch on every surface already rendered, without a reload: each
  // badge keeps its committed provider label.
  function showSwitching(transferId) {
    const id = CSS.escape(String(transferId));
    document.querySelectorAll('[data-dp-switching-transfer="' + id + '"]').forEach(function (node) { node.remove(); });
    if (!switching.has(Number(transferId))) {
      document.querySelectorAll('[' + TRIGGER_ATTR + '][data-dp-transfer-id="' + id + '"]').forEach(function (node) {
        node.removeAttribute('aria-disabled');
        node.setAttribute('aria-label', 'Provider: ' + node.textContent + '. Choose another provider');
      });
      return;
    }
    document.querySelectorAll('[' + TRIGGER_ATTR + '][data-dp-transfer-id="' + id + '"]').forEach(function (node) {
      node.setAttribute('aria-disabled', 'true');
      node.setAttribute('aria-label', 'Provider: ' + node.textContent + '. A provider switch is in progress');
      node.insertAdjacentHTML('afterend', switchingMarkup(transferId));
    });
  }

  async function readStatus(transferId) {
    const response = await window.debridPulseAuth.fetch('/api/torrents/' + transferId + '/route');
    if (!response.ok) throw new Error('Provider status is unavailable.');
    return response.json();
  }

  // ── Details: Files header, right aligned ─────────────────────────────────
  async function renderDetailMount(transfer) {
    const mount = document.querySelector('#modal-body ' + MOUNT_SELECTOR);
    if (!mount || !transfer || !Object.prototype.hasOwnProperty.call(transfer, 'route_provider_id')) {
      if (mount) mount.innerHTML = '';
      return;
    }
    const label = providerLabel(transfer.route_provider_name, transfer.route_provider_id);
    mount.innerHTML = badgeMarkup({id: transfer.id, route_provider_id: transfer.route_provider_id,
      route_provider_name: transfer.route_provider_name, route_provider_theme: transfer.route_provider_theme,
      route_switch_available: false}, 'details');
    if (!transfer.route_provider_id) return;
    try {
      const status = await readStatus(transfer.id);
      const live = document.querySelector('#modal-body ' + MOUNT_SELECTOR);
      if (live && Number(transfer.id) === detailTransferId && status.switchable &&
          status.current_provider_id === transfer.route_provider_id) {
        live.innerHTML = launcherMarkup(transfer.id, transfer.route_provider_id, label, 'details',
          themeAttribute(transfer.route_provider_theme));
      }
    } catch (_) { /* informational badge stays */ }
  }

  // ── The picker ───────────────────────────────────────────────────────────
  function ensureMenu() {
    if (menuEl) return menuEl;
    menuEl = document.createElement('div');
    menuEl.className = 'dp-dropdown-menu dp-root-provider-menu';
    menuEl.setAttribute('role', 'dialog');
    menuEl.setAttribute('aria-modal', 'false');
    menuEl.tabIndex = -1;
    menuEl.hidden = true;
    menuEl.addEventListener('keydown', onMenuKeydown);
    menuEl.addEventListener('click', onMenuClick);
    document.body.appendChild(menuEl);
    return menuEl;
  }

  function rowMarkup(item) {
    const label = providerLabel(item.provider_name, item.provider_id);
    const status = STATUS_LABELS[item.status] || STATUS_LABELS.unavailable;
    const reason = item.reason ? (REASON_LABELS[item.reason] || '') : '';
    let action;
    if (item.status === 'current') {
      action = '<span class="dp-root-provider-current">CURRENT</span>';
    } else if (item.selectable) {
      action = '<button type="button" class="dp-root-provider-switch" data-dp-provider-id="' + esc(item.provider_id) +
        '">Switch</button>';
    } else {
      action = '<button type="button" class="dp-root-provider-switch" disabled aria-disabled="true">Switch</button>';
    }
    return '<div class="dp-root-provider-row" role="group" aria-label="' + esc(label + ': ' + status) + '" ' +
      'data-dp-provider-status="' + esc(item.status) + '">' +
      '<span class="dp-root-provider-row-name">' + esc(label) + '</span>' +
      '<span class="dp-root-provider-status" data-status="' + esc(item.status) + '">' + esc(status) +
      (reason ? '<span class="dp-root-provider-reason"> — ' + esc(reason) + '</span>' : '') + '</span>' +
      action + '</div>';
  }

  function renderMenu(status) {
    const menu = ensureMenu();
    const heading = 'dp-root-provider-heading';
    menu.setAttribute('aria-labelledby', heading);
    menu.innerHTML = '<div class="dp-root-provider-title" id="' + heading + '">Provider</div>' +
      (status.providers || []).map(rowMarkup).join('');
  }

  function positionMenu() {
    if (!menuEl || menuEl.hidden || !menuTrigger || !menuTrigger.isConnected) return;
    const rect = menuTrigger.getBoundingClientRect();
    menuEl.style.visibility = 'hidden';
    const menuRect = menuEl.getBoundingClientRect();
    const margin = 8;
    let left = Math.min(rect.left, window.innerWidth - margin - menuRect.width);
    left = Math.max(margin, left);
    let top = rect.bottom + 4;
    if (top + menuRect.height > window.innerHeight - margin) {
      const above = rect.top - 4 - menuRect.height;
      top = above >= margin ? above : Math.max(margin, window.innerHeight - margin - menuRect.height);
    }
    menuEl.style.left = Math.round(left) + 'px';
    menuEl.style.top = Math.round(top) + 'px';
    menuEl.style.visibility = '';
  }

  function actionableButtons() {
    return menuEl ? Array.from(menuEl.querySelectorAll('.dp-root-provider-switch:not(:disabled)')) : [];
  }

  async function open(transferId, trigger) {
    if (switching.has(Number(transferId))) return;
    closeMenu();
    menuTrigger = trigger || null;
    menuSurface = trigger ? trigger.getAttribute('data-dp-surface') : null;
    menuTransferId = transferId;
    if (menuTrigger) menuTrigger.setAttribute('aria-expanded', 'true');
    let status;
    try {
      status = await readStatus(transferId);
    } catch (error) {
      closeMenu({focusTrigger: true});
      toast('Provider status is unavailable right now.', 'error');
      return;
    }
    if (menuTransferId !== transferId) return;
    menuStatus = status;
    renderMenu(status);
    menuEl.hidden = false;
    positionMenu();
    const first = actionableButtons()[0];
    (first || menuEl).focus({preventScroll: true});
  }

  function closeMenu(options) {
    const trigger = menuTrigger;
    if (trigger) trigger.setAttribute('aria-expanded', 'false');
    if (menuEl) {
      menuEl.hidden = true;
      menuEl.innerHTML = '';
    }
    menuTrigger = null;
    menuSurface = null;
    menuTransferId = null;
    menuStatus = null;
    if (options && options.focusTrigger && trigger && trigger.isConnected) trigger.focus({preventScroll: true});
  }

  function failureMessage(payload) {
    const detail = payload && payload.detail;
    if (detail && typeof detail === 'object') {
      const category = String(detail.category || '');
      if (category === 'concurrency_limited') return 'The provider has no free capacity right now. Try again shortly.';
      if (category === 'provider_unavailable') return 'Provider is no longer available for this transfer.';
      if (category === 'account_limited') return 'This provider\'s account cannot take this transfer right now.';
      if (category === 'resource_state_conflict' && detail.domain === 'provider') {
        return 'That provider is still preparing this transfer; nothing was switched.';
      }
      if (category === 'resource_state_conflict') return 'The transfer changed meanwhile; nothing was switched.';
    }
    return 'Nothing was switched.';
  }

  // THE one switch action every surface uses.
  async function switchProvider(transferId, providerId, expectedProviderId) {
    const response = await window.debridPulseAuth.fetch('/api/torrents/' + transferId + '/route', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider_id: providerId, expected_provider_id: expectedProviderId}),
    });
    const payload = await response.json().catch(function () { return {}; });
    if (!response.ok) {
      const error = new Error(failureMessage(payload));
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  async function refreshSurfaces(transferId) {
    const jobs = [];
    if (typeof window.loadTorrents === 'function') jobs.push(Promise.resolve(window.loadTorrents()));
    if (typeof window.loadRecent === 'function') jobs.push(Promise.resolve(window.loadRecent()));
    await Promise.allSettled(jobs);
    // The Details owner re-renders the open transfer (its public entry,
    // never wrapped): the header field and the Files-header control follow.
    const showDetail = window.showDetail;
    if (detailTransferId === Number(transferId) && typeof showDetail === 'function') await showDetail(transferId);
  }

  async function choose(providerId) {
    if (!menuStatus || menuTransferId == null || switching.has(Number(menuTransferId))) return;
    const transferId = Number(menuTransferId);
    const status = menuStatus;
    const current = status.current_provider_id;
    const currentName = providerLabel(status.current_provider_name, current);
    const target = (status.providers || []).find(function (item) { return item.provider_id === providerId; });
    const targetName = providerLabel(target && target.provider_name, providerId);
    // One request per transfer; the picker never holds the page meanwhile.
    switching.set(transferId, targetName);
    closeMenu({focusTrigger: true});
    showSwitching(transferId);
    try {
      await switchProvider(transferId, providerId, current);
      toast('Switched provider to ' + targetName + '.', 'success');
    } catch (error) {
      toast({title: 'Could not switch provider; transfer remains on ' + currentName + '.',
        body: String((error && error.message) || 'Nothing was switched.')}, 'error');
    } finally {
      switching.delete(transferId);
      showSwitching(transferId);
    }
    await refreshSurfaces(transferId);
  }

  function onMenuClick(event) {
    const button = event.target instanceof Element ? event.target.closest('.dp-root-provider-switch') : null;
    if (!button || button.disabled) return;
    event.preventDefault();
    choose(button.getAttribute('data-dp-provider-id'));
  }

  function onMenuKeydown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      closeMenu({focusTrigger: true});
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'Home' || event.key === 'End') {
      const buttons = actionableButtons();
      if (!buttons.length) return;
      event.preventDefault();
      if (event.key === 'Home' || event.key === 'End') {
        buttons[event.key === 'Home' ? 0 : buttons.length - 1].focus();
        return;
      }
      const index = buttons.indexOf(document.activeElement);
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      buttons[index === -1 ? 0 : (index + delta + buttons.length) % buttons.length].focus();
    }
  }

  function onDocumentClick(event) {
    const trigger = event.target instanceof Element ? event.target.closest('[' + TRIGGER_ATTR + ']') : null;
    if (!trigger) return;
    event.preventDefault();
    event.stopPropagation();
    const transferId = Number(trigger.getAttribute('data-dp-transfer-id'));
    if (menuTrigger === trigger && menuEl && !menuEl.hidden) {
      closeMenu({focusTrigger: true});
      return;
    }
    open(transferId, trigger);
  }

  function onDocumentPointerDown(event) {
    if (!menuEl || menuEl.hidden) return;
    const target = event.target instanceof Node ? event.target : null;
    if (target && (menuEl.contains(target) || (menuTrigger && menuTrigger.contains(target)))) return;
    closeMenu();
  }

  // The open picker is owned by its semantic anchor (transfer, surface), not
  // a DOM node: a refresh that re-renders the row re-anchors it to the new
  // launcher; only when no equivalent launcher exists any more does it close.
  function onSurfaceRendered() {
    if (!menuEl || menuEl.hidden || (menuTrigger && menuTrigger.isConnected)) return;
    const live = menuTransferId == null ? null : document.querySelector('[' + TRIGGER_ATTR + '][data-dp-transfer-id="' +
      CSS.escape(String(menuTransferId)) + '"][data-dp-surface="' + CSS.escape(String(menuSurface || '')) + '"]');
    if (live) {
      menuTrigger = live;
      live.setAttribute('aria-expanded', 'true');
      positionMenu();
    } else {
      closeMenu();
    }
  }

  function install() {
    document.addEventListener('click', onDocumentClick, true);
    document.addEventListener('pointerdown', onDocumentPointerDown, true);
    document.addEventListener('debridpulse:detail-rendered', function (event) {
      const id = Number(event && event.detail && event.detail.transferId);
      detailTransferId = Number.isFinite(id) ? id : null;
      onSurfaceRendered();
      renderDetailMount(event && event.detail && event.detail.transfer);
    });
    document.addEventListener('debridpulse:detail-closed', function () {
      detailTransferId = null;
      closeMenu();
    });
    document.addEventListener('debridpulse:dashboard-recent-rendered', function () { queueMicrotask(onSurfaceRendered); });
    document.addEventListener('debridpulse:downloads-rendered', function () { queueMicrotask(onSurfaceRendered); });
    window.addEventListener('resize', positionMenu, {passive: true});
    window.addEventListener('scroll', positionMenu, {passive: true, capture: true});
  }

  window.DPRootProvider = Object.freeze({badgeMarkup: badgeMarkup, open: open, switchProvider: switchProvider});

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', install, {once: true});
  } else {
    install();
  }
})();
