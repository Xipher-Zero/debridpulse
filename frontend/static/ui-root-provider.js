/* DebridPulse 1.0.13 root provider: the one live badge, picker and switch
 * action for Transfer Details (Files header and each file), Recent Activity
 * and Downloads.
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
 * Any other transfer switches per ROOT (``route_switch_scope``): its list
 * badge keeps the normal active-provider label; with one root (``root``) it
 * opens that root's picker, with several (``transfer``) it opens the
 * transfer's best-effort provider picker (GET /api/torrents/{id}/route/transfer):
 * each provider with how many of the transfer's links it may take now and why
 * the others take no part. Choosing one asks the backend to switch every link
 * that provider may take, each through the one-root switch (POST
 * .../route/transfer, naming the provider the operator saw on each link); the
 * result says link by link what switched, what already was on it, what could
 * not take part and what was refused -- never a clean whole-transfer switch it
 * did not make. Each file whose producing root may move (``route_root``)
 * carries its own launcher in Details, naming that root's provider; its picker
 * reads the status for that file (``?artifact_id=``) and its switch names the
 * file and the root the operator saw (``artifact_id``, ``expected_root_id``).
 *
 * Interaction follows the existing candidate chooser: a non-modal dialog
 * anchored to its launcher, Escape / click-away to close, arrow keys between
 * actions, focus returned to the launcher. Choosing a provider closes the
 * picker at once: while the one switch request is outstanding every surface
 * keeps the committed provider and says "Switching to <provider>…" beside the
 * launchers it concerns -- the transfer's badge for a transfer-wide switch,
 * and in Details only the files of the links being switched -- (re-rendered
 * by the surfaces' ordinary refreshes), and that transfer takes no second
 * switch until the request settles.
 */
(function () {
  'use strict';

  const TRIGGER_ATTR = 'data-dp-root-provider-trigger';
  const MOUNT_SELECTOR = '[data-dp-root-provider-mount]';
  const STATUS_LABELS = {
    current: 'Current',
    prepared: 'Ready',
    preparing: 'Preparing…',
    waiting_for_selection: 'Waiting for selection',
    deferred: 'Deferred',
    retrying: 'Retrying…',
    available: 'Not checked',
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
  // Why a link takes no part in a transfer-wide choice of one provider
  // (``transfer_route_status``), and why a link was refused when switched.
  const PARTICIPATION_LABELS = {
    already_selected: 'already on it',
    unsupported: 'cannot use this link',
    unavailable: 'unavailable for this link',
    preparing: 'still preparing',
    pending: 'waiting for this provider\'s copy',
    inactive: 'finished',
    decomposed: 'expanded into several files',
    shared: 'shared with another download',
    no_route: 'not routed yet',
  };
  const REFUSAL_LABELS = {
    concurrency_limited: 'no free capacity',
    account_limited: 'account limit',
    provider_unavailable: 'provider unavailable',
    resource_state_conflict: 'changed or busy meanwhile; try again',
    unsupported_request: 'not switchable',
    changed: 'changed since the list was read',
    gone: 'download no longer available',
    error: 'stopped by an error',
    not_attempted: 'not attempted after an error',
    discard_confirmation_required: 'would discard downloaded data; switch its source to confirm',
  };

  let menuEl = null;
  let menuTrigger = null;
  let menuSurface = null;
  let menuTransferId = null;
  let menuStatus = null;
  let menuArtifactId = null;
  let menuScope = null;
  let menuRefreshing = false;
  let detailTransferId = null;
  // transferId -> {target, transfer, roots}, while that transfer's one switch
  // request is outstanding: ``target`` the provider label, ``transfer`` true
  // for a switch of the whole transfer (its badge says it), ``roots`` the
  // root ids it moves (null: every root of the transfer).
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
  // Any other transfer's (``route_switch_scope``) names ``provider``, the
  // list's ordinary active-provider presentation.
  function badgeMarkup(item, surface, provider) {
    if (item && !Object.prototype.hasOwnProperty.call(item, 'route_provider_id') && item.route_switch_scope) {
      return sourceBadgeMarkup(item, surface, provider || {});
    }
    const label = providerLabel(item && item.route_provider_name, item && item.route_provider_id);
    const known = Boolean(item && item.route_provider_id);
    const identity = identityOf(known ? item.route_provider_id : '');
    // Switching a route redirects everyone's work: offered only where the
    // server says this caller may (``access.destroy``).
    if (!known || !(item && item.route_switch_available && window.debridPulseAuth?.mayDestroy(item))) {
      return '<span class="dp-provider-chip dp-root-provider-badge" data-provider-state="' +
        (known ? 'known' : 'pending') + '"' + identity.attributes + '>' + identity.glyph + esc(label) + '</span>' +
        switchingMarkup(item && item.id) + deferredMarkup(item);
    }
    return launcherMarkup(item.id, item.route_provider_id, label, surface) + deferredMarkup(item);
  }

  function sourceBadgeMarkup(item, surface, provider) {
    const state = String(provider.state || 'pending');
    const providerId = state === 'known' ? String(provider.id || '') : '';
    const label = String(provider.label || 'Pending');
    if (!(item.route_switch_available && window.debridPulseAuth?.mayDestroy(item))) {
      const identity = identityOf(providerId);
      return '<span class="dp-provider-chip" data-provider-state="' + esc(state) + '"' + identity.attributes + '>' +
        identity.glyph + esc(label) + '</span>' + switchingMarkup(item.id) + deferredMarkup(item) + pendingMarkup(item);
    }
    return launcherMarkup(item.id, providerId, label, surface, {scope: item.route_switch_scope}) +
      deferredMarkup(item) + pendingMarkup(item);
  }

  // One file's launcher in Details: the provider of the ROOT that produced
  // its selected source, offered only where that root may move now.
  function artifactBadgeMarkup(detail, file) {
    const route = file && file.route_root;
    const artifactId = file && (file.artifact_id != null ? file.artifact_id : file.id);
    if (!route || !route.switch_available || artifactId == null || !detail ||
        !window.debridPulseAuth?.mayDestroy(detail)) return '';
    return launcherMarkup(detail.id, route.provider_id, providerLabel(route.provider_name, route.provider_id),
      'details', {artifactId: artifactId, rootId: route.root_id});
  }

  // The committed provider's badge identity, from the one identity owner.
  function identityOf(providerId) {
    return window.DPTransferSourcePresentation.providerBadgeIdentity(providerId);
  }

  // The provider a launcher's surface says it is switching to, or null: a
  // file's launcher (``rootId``) only while its own root moves, any other
  // launcher only while the whole transfer is switched.
  function switchingTarget(transferId, rootId) {
    const entry = transferId == null ? null : switching.get(Number(transferId));
    if (!entry) return null;
    if (rootId == null || rootId === '') return entry.transfer ? entry.target : null;
    return entry.roots === null || entry.roots.has(String(rootId)) ? entry.target : null;
  }

  // An operator's pending provider choice (``pending_provider_ids``): a
  // preference awaiting that provider's copy, shown beside -- never as -- the
  // committed provider the badge names.
  function pendingMarkup(item) {
    const ids = (item && Array.isArray(item.pending_provider_ids)) ? item.pending_provider_ids : [];
    if (!ids.length) return '';
    const given = Array.isArray(item.pending_provider_names) ? item.pending_provider_names : [];
    const names = ids.map(function (id, index) { return providerLabel(given[index], id); }).join(', ');
    return '<span class="dp-root-provider-switching dp-root-provider-pending" role="status" ' +
      'data-dp-pending-providers="' + esc(ids.join(' ')) + '">' + esc('Preparing ' + names + '…') + '</span>';
  }
  // A source switch the server accepted while the download is paused
  // (``deferred_switch``): its destination, said beside -- never as -- the
  // committed provider the badge names, until Resume completes it. Only what
  // the server reports: no provider, no message; a host only when it named one.
  // While the transfer is paused it waits for Resume; once resumed it is being
  // applied (until the server no longer reports it).
  function deferredMarkup(item) {
    const target = item && item.deferred_switch;
    if (!target || !target.provider_id) return '';
    const name = providerLabel(target.provider_name, target.provider_id);
    const destination = target.host ? name + ' (' + target.host + ')' : name;
    const waiting = String(item.status || '').toLowerCase() === 'paused';
    return '<span class="dp-root-provider-switching dp-root-provider-deferred" role="status" ' +
      'data-dp-deferred-provider="' + esc(target.provider_id) + '"' +
      (target.host ? ' data-dp-deferred-host="' + esc(target.host) + '"' : '') + '>' +
      esc(waiting ? 'Switch to ' + destination + ' on resume' : 'Switching to ' + destination + '…') + '</span>';
  }
  function switchingMarkup(transferId, rootId) {
    const target = switchingTarget(transferId, rootId);
    return target == null ? '' : '<span class="dp-root-provider-switching" role="status" ' +
      'data-dp-switching-transfer="' + esc(transferId) + '">' + esc('Switching to ' + target + '…') + '</span>';
  }

  function launcherAction(scope) {
    return scope === 'transfer' ? '. Choose a provider for this transfer\'s links' : '. Choose another provider';
  }

  function launcherMarkup(transferId, providerId, label, surface, subject) {
    const busy = switching.has(Number(transferId));
    const identity = identityOf(providerId);
    const scope = subject && subject.scope === 'transfer' ? 'transfer' : '';
    const artifact = subject && subject.artifactId != null ? subject.artifactId : null;
    const rootId = subject && subject.rootId != null ? String(subject.rootId) : null;
    return '<button type="button" class="dp-provider-chip dp-root-provider-launcher" ' + TRIGGER_ATTR + ' ' +
      'data-dp-transfer-id="' + esc(transferId) + '" data-dp-provider-id="' + esc(providerId) + '" ' +
      (scope ? 'data-dp-scope="transfer" ' : '') +
      (artifact != null ? 'data-dp-artifact-id="' + esc(artifact) + '" ' : '') +
      (rootId != null ? 'data-dp-root-id="' + esc(rootId) + '" ' : '') +
      'data-dp-surface="' + esc(surface || '') + '" data-provider-state="known"' + identity.attributes + ' ' +
      'aria-haspopup="dialog" aria-expanded="false"' + (busy ? ' aria-disabled="true"' : '') + ' ' +
      'aria-label="' + esc('Provider: ' + label + (busy ? '. A provider switch is in progress' : launcherAction(scope))) +
      '">' + identity.glyph +
      '<span class="dp-root-provider-name">' + esc(label) + '</span>' +
      '<span class="dp-root-provider-caret" aria-hidden="true"></span></button>' + switchingMarkup(transferId, rootId);
  }

  // Say the switch on every surface already rendered, without a reload: each
  // badge keeps its committed provider label.
  function showSwitching(transferId) {
    const id = CSS.escape(String(transferId));
    document.querySelectorAll('[data-dp-switching-transfer="' + id + '"]').forEach(function (node) { node.remove(); });
    if (!switching.has(Number(transferId))) {
      document.querySelectorAll('[' + TRIGGER_ATTR + '][data-dp-transfer-id="' + id + '"]').forEach(function (node) {
        node.removeAttribute('aria-disabled');
        node.setAttribute('aria-label', 'Provider: ' + node.textContent +
          launcherAction(node.getAttribute('data-dp-scope')));
      });
      return;
    }
    document.querySelectorAll('[' + TRIGGER_ATTR + '][data-dp-transfer-id="' + id + '"]').forEach(function (node) {
      node.setAttribute('aria-disabled', 'true');
      node.setAttribute('aria-label', 'Provider: ' + node.textContent + '. A provider switch is in progress');
      node.insertAdjacentHTML('afterend', switchingMarkup(transferId, node.getAttribute('data-dp-root-id')));
    });
  }

  async function readStatus(transferId, artifactId, scope) {
    const response = await window.debridPulseAuth.fetch('/api/torrents/' + transferId + '/route' +
      (scope === 'transfer' ? '/transfer' : artifactId != null ? '?artifact_id=' + encodeURIComponent(artifactId) : ''));
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
      route_provider_name: transfer.route_provider_name, route_switch_available: false}, 'details');
    if (!transfer.route_provider_id) return;
    try {
      const status = await readStatus(transfer.id);
      const live = document.querySelector('#modal-body ' + MOUNT_SELECTOR);
      if (live && Number(transfer.id) === detailTransferId && status.switchable && window.debridPulseAuth?.mayDestroy(transfer) &&
          status.current_provider_id === transfer.route_provider_id) {
        live.innerHTML = launcherMarkup(transfer.id, transfer.route_provider_id, label, 'details');
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

  function formatWait(seconds) {
    const total = Math.max(0, Math.round(seconds));
    if (total < 60) return total + ' s';
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    return rest ? minutes + ' min ' + rest + ' s' : minutes + ' min';
  }

  function rowMarkup(item) {
    const label = providerLabel(item.provider_name, item.provider_id);
    const status = STATUS_LABELS[item.status] || STATUS_LABELS.unavailable;
    let reason = item.reason ? (REASON_LABELS[item.reason] || '') : '';
    if (item.status === 'retrying') {
      // A backup whose preparation failed recoverably: what failed and when
      // it is tried again, from the read model (never a client clock).
      const wait = Number(item.retry_in_seconds);
      const next = Number.isFinite(wait) ? (wait > 0 ? 'next attempt in ' + formatWait(wait) : 'next attempt now') : '';
      reason = [item.detail || '', next].filter(Boolean).join('; ');
    }
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

  // What keeps a transfer's other links out of one provider's choice:
  // "2 already on it · 1 cannot use this link", from each link's status.
  function participationSummary(item) {
    const counts = {};
    (item.roots || []).forEach(function (root) {
      if (root.status === 'eligible') return;
      const label = root.status === 'unavailable' && root.reason && REASON_LABELS[root.reason]
        ? REASON_LABELS[root.reason] : (PARTICIPATION_LABELS[root.status] || 'not switchable');
      counts[label] = (counts[label] || 0) + 1;
    });
    return Object.keys(counts).map(function (label) { return counts[label] + ' ' + label; }).join(' · ');
  }

  // One provider of a transfer-wide choice: how many of the transfer's links
  // it may take now, and why the others take no part. Best effort: each link
  // is switched, or refused, on its own.
  function transferRowMarkup(item) {
    const label = providerLabel(item.provider_name, item.provider_id);
    const total = Number(item.participants) || 0;
    const eligible = Number(item.eligible) || 0;
    const already = Number(item.already_selected) || 0;
    const status = already === total && total > 0 ? 'On all ' + total + ' links'
      : eligible ? 'Can take ' + eligible + ' of ' + total + ' link' + (total === 1 ? '' : 's')
        : 'No link can switch';
    const reason = participationSummary(item);
    const action = already === total && total > 0
      ? '<span class="dp-root-provider-current">CURRENT</span>'
      : item.selectable
        ? '<button type="button" class="dp-root-provider-switch" data-dp-provider-id="' + esc(item.provider_id) +
          '">Switch</button>'
        : '<button type="button" class="dp-root-provider-switch" disabled aria-disabled="true">Switch</button>';
    return '<div class="dp-root-provider-row" role="group" aria-label="' + esc(label + ': ' + status) + '" ' +
      'data-dp-provider-status="' + esc(item.selectable ? 'eligible' : 'ineligible') + '" ' +
      'data-dp-eligible="' + esc(eligible) + '" data-dp-participants="' + esc(total) + '">' +
      '<span class="dp-root-provider-row-name">' + esc(label) + '</span>' +
      '<span class="dp-root-provider-status">' + esc(status) +
      (reason ? '<span class="dp-root-provider-reason"> — ' + esc(reason) + '</span>' : '') + '</span>' +
      action + '</div>';
  }

  function renderMenu(status) {
    const menu = ensureMenu();
    const heading = 'dp-root-provider-heading';
    const transfer = status && status.scope === 'transfer';
    menu.setAttribute('aria-labelledby', heading);
    menu.innerHTML = '<div class="dp-root-provider-title" id="' + heading + '">' +
      (transfer ? 'Provider for this transfer' : 'Provider') + '</div>' +
      (transfer ? '<div class="dp-root-provider-note">Best effort: each link switches on its own; ' +
        'links a provider cannot take stay where they are.</div>' : '') +
      (status.providers || []).map(transfer ? transferRowMarkup : rowMarkup).join('');
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
    menuArtifactId = trigger ? trigger.getAttribute('data-dp-artifact-id') : null;
    menuScope = trigger ? trigger.getAttribute('data-dp-scope') : null;
    menuTransferId = transferId;
    if (menuTrigger) menuTrigger.setAttribute('aria-expanded', 'true');
    let status;
    try {
      status = await readStatus(transferId, menuArtifactId, menuScope);
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
    menuArtifactId = null;
    menuScope = null;
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

  // THE one switch action every surface uses. ``subject`` names the file and
  // the root the operator saw, for a root that is not a torrent's one root.
  async function switchProvider(transferId, providerId, expectedProviderId, subject) {
    const body = {provider_id: providerId, expected_provider_id: expectedProviderId};
    if (subject && subject.artifactId != null) body.artifact_id = Number(subject.artifactId);
    if (subject && subject.rootId) body.expected_root_id = String(subject.rootId);
    const response = await window.debridPulseAuth.fetch('/api/torrents/' + transferId + '/route', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(function () { return {}; });
    if (!response.ok) {
      const error = new Error(failureMessage(payload));
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  // THE transfer-wide action: one request; the backend switches each link
  // through the one-root switch and reports every link's outcome.
  async function switchTransferProvider(transferId, providerId, expectedRoutes) {
    const response = await window.debridPulseAuth.fetch('/api/torrents/' + transferId + '/route/transfer', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider_id: providerId, expected_routes: expectedRoutes}),
    });
    const payload = await response.json().catch(function () { return {}; });
    if (!response.ok) {
      const error = new Error(failureMessage(payload));
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function links(count) {
    return count + ' link' + (count === 1 ? '' : 's');
  }

  // Every link's outcome, counted by what happened and why: never a clean
  // whole-transfer switch the backend did not report.
  function outcomeToast(result, targetName) {
    const switched = Number(result.switched) || 0;
    const parts = [];
    if (result.already_selected) parts.push(links(result.already_selected) + ' already on ' + targetName);
    // Accepted but not switched yet: each waits for the provider's copy and
    // keeps downloading from its current source meanwhile.
    if (result.pending) parts.push(links(result.pending) + ' will switch when ' + targetName + '\'s copy is ready');
    const reasons = {};
    (result.roots || []).forEach(function (root) {
      if (root.outcome === 'ineligible') {
        const label = REASON_LABELS[root.reason] || PARTICIPATION_LABELS[root.reason] || 'not switchable';
        reasons['not switched (' + label + ')'] = (reasons['not switched (' + label + ')'] || 0) + 1;
      } else if (root.outcome === 'refused') {
        const label = REFUSAL_LABELS[root.reason] || 'refused';
        reasons['refused (' + label + ')'] = (reasons['refused (' + label + ')'] || 0) + 1;
      }
    });
    Object.keys(reasons).forEach(function (label) { parts.push(links(reasons[label]) + ' ' + label); });
    const body = parts.join('; ') + (parts.length ? '.' : '');
    if (result.result === 'complete') {
      toast({title: 'Switched ' + links(switched) + ' to ' + targetName + '.', body: body}, 'success');
    } else if (result.result === 'pending') {
      toast({title: (switched ? 'Switched ' + links(switched) + ' to ' + targetName + '; ' : '') +
        'preparing ' + targetName + ' for ' + links(Number(result.pending) || 0) + '.', body: body}, 'info');
    } else if (result.result === 'partial') {
      toast({title: 'Switched ' + switched + ' of ' + links(Number(result.participants) || 0) + ' to ' +
        targetName + '.', body: body}, 'warning');
    } else {
      toast({title: 'No link was switched to ' + targetName + '.', body: body || 'Nothing was switched.'}, 'error');
    }
  }

  async function chooseForTransfer(providerId) {
    const transferId = Number(menuTransferId);
    const status = menuStatus;
    const target = (status.providers || []).find(function (item) { return item.provider_id === providerId; });
    if (!target) return;
    const targetName = providerLabel(target.provider_name, providerId);
    // Exactly what the operator saw: each link this provider may take, with
    // the provider it was on. The backend refuses any link that moved since.
    const expected = {};
    (target.roots || []).forEach(function (root) {
      if (root.status === 'eligible') expected[root.root_id] = root.current_provider_id;
    });
    switching.set(transferId, {target: targetName, transfer: true, roots: new Set(Object.keys(expected))});
    closeMenu({focusTrigger: true});
    showSwitching(transferId);
    try {
      outcomeToast(await switchTransferProvider(transferId, providerId, expected), targetName);
    } catch (error) {
      toast({title: 'Could not switch this transfer to ' + targetName + '.',
        body: String((error && error.message) || 'Nothing was switched.') +
          ' The list is refreshed to show each link\'s provider.'}, 'error');
    } finally {
      switching.delete(transferId);
      showSwitching(transferId);
    }
    await refreshSurfaces(transferId);
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
    if (menuStatus.scope === 'transfer') {
      await chooseForTransfer(providerId);
      return;
    }
    const transferId = Number(menuTransferId);
    const status = menuStatus;
    const subject = {artifactId: menuArtifactId, rootId: status.root_id};
    const current = status.current_provider_id;
    const currentName = providerLabel(status.current_provider_name, current);
    const target = (status.providers || []).find(function (item) { return item.provider_id === providerId; });
    const targetName = providerLabel(target && target.provider_name, providerId);
    // One request per transfer; the picker never holds the page meanwhile.
    // A file's root switch is said beside that root's files only -- unless
    // that root is the transfer's only one, which is the whole transfer.
    const roots = new Set(Array.from(document.querySelectorAll('[' + TRIGGER_ATTR + '][data-dp-transfer-id="' +
      CSS.escape(String(transferId)) + '"][data-dp-root-id]')).map(function (node) {
      return node.getAttribute('data-dp-root-id');
    }));
    const fileRoot = menuArtifactId != null && status.root_id != null && roots.size > 1;
    switching.set(transferId, {target: targetName, transfer: !fileRoot,
      roots: fileRoot ? new Set([String(status.root_id)]) : null});
    closeMenu({focusTrigger: true});
    showSwitching(transferId);
    try {
      await switchProvider(transferId, providerId, current, subject);
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
    if (!menuEl || menuEl.hidden) return;
    refreshOpenMenu();
    if (menuTrigger && menuTrigger.isConnected) return;
    const live = menuTransferId == null ? null : document.querySelector('[' + TRIGGER_ATTR + '][data-dp-transfer-id="' +
      CSS.escape(String(menuTransferId)) + '"][data-dp-surface="' + CSS.escape(String(menuSurface || '')) + '"]' +
      (menuArtifactId != null ? '[data-dp-artifact-id="' + CSS.escape(String(menuArtifactId)) + '"]' : ''));
    if (live) {
      menuTrigger = live;
      live.setAttribute('aria-expanded', 'true');
      positionMenu();
    } else {
      closeMenu();
    }
  }

  // While the picker stays open, the surface refresh that re-renders its row
  // (the existing refresh lifecycle; no poller of its own) also re-reads that
  // one transfer's provider status, at most one read at a time, and redraws
  // only what changed -- keeping focus on the row the operator was on.
  async function refreshOpenMenu() {
    if (menuRefreshing || menuTransferId == null || switching.has(Number(menuTransferId))) return;
    const transferId = menuTransferId;
    const artifactId = menuArtifactId;
    const scope = menuScope;
    menuRefreshing = true;
    let status;
    try {
      status = await readStatus(transferId, artifactId, scope);
    } catch (error) {
      return;
    } finally {
      menuRefreshing = false;
    }
    if (menuTransferId !== transferId || menuArtifactId !== artifactId || !menuEl || menuEl.hidden) return;
    if (JSON.stringify(status) === JSON.stringify(menuStatus)) return;
    const focused = menuEl.contains(document.activeElement) && document.activeElement.closest('[data-dp-provider-status]');
    const focusedId = focused && focused.querySelector('[data-dp-provider-id]')
      ? focused.querySelector('[data-dp-provider-id]').getAttribute('data-dp-provider-id') : null;
    const hadFocus = menuEl.contains(document.activeElement);
    menuStatus = status;
    renderMenu(status);
    positionMenu();
    if (!hadFocus) return;
    const again = focusedId && menuEl.querySelector('[data-dp-provider-id="' + CSS.escape(focusedId) + '"]');
    (again || actionableButtons()[0] || menuEl).focus({preventScroll: true});
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

  window.DPRootProvider = Object.freeze({badgeMarkup: badgeMarkup, artifactBadgeMarkup: artifactBadgeMarkup, open: open,
    switchProvider: switchProvider, switchTransferProvider: switchTransferProvider});

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', install, {once: true});
  } else {
    install();
  }
})();
