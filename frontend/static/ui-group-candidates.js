/* DebridPulse v1.0.12 transfer-level common-source group switching.
 *
 * A thin wrapper over the already-qualified per-file canonical candidate and
 * manual-switch machinery. It answers one derived question — which source
 * hosts exist as a canonical candidate on EVERY actual file of a transfer
 * (common-source MEMBERSHIP) — and, separately, which of those common hosts
 * the whole group can currently converge to (ACTIONABILITY) — then drives the
 * existing exact per-file switch endpoint for the files that actually need to
 * move. Membership and actionability are independent: a source stays common
 * (counted, shown) even when it is not currently switchable for every file.
 * It owns NO switching engine, candidate model, recovery, provenance,
 * routing, or lifecycle: those stay backend-owned.
 *
 * One shared runtime for three surfaces (Details Files header, Downloads,
 * Dashboard Recent Items). The per-file candidate disclosure in Details is a
 * separate owner (ui-detail-candidates.js) and is never touched here.
 */
(function () {
  'use strict';

  const TRIGGER_SELECTOR = '[data-dp-group-candidates-trigger]';
  const MOUNT_SELECTOR = '[data-dp-group-candidates-mount]';
  const SWITCH_ACTION = 'Switch to this source';

  let menuEl = null;
  let menuOwnerTrigger = null;
  let menuTransferId = null;
  let menuBusy = false;
  let menuGroup = null;
  let detailTransferId = null;

  function esc(value) {
    if (typeof window.esc === 'function') return window.esc(value);
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function glyph() {
    return (window.DPIcons && typeof window.DPIcons.svg === 'function')
      ? window.DPIcons.svg('network', 'dp-candidate-chip-icon') : '';
  }

  function toast(payload, kind) {
    if (typeof window.toast === 'function') window.toast(payload, kind);
  }

  // ── Pure group computation ───────────────────────────────────────────────
  //
  // ``files`` is the authoritative Details payload's file list. Participating
  // artifacts are exactly those the backend projected a ``source_candidates``
  // array onto (the current-authoritative artifacts: physical, unblocked,
  // non-standby). ``source_candidates`` entries are
  // {source_host, candidate_id, is_selected, switch_eligible}.
  //
  // MEMBERSHIP and ACTIONABILITY are independent facts and must stay that way:
  //   commonHosts     — raw canonical-host intersection across every
  //                      participating file's candidates, regardless of
  //                      is_selected/switch_eligible/artifact state. This is
  //                      what "common source" means and what the launcher
  //                      count/visibility is based on.
  //   activeHost      — the one common host every file is uniformly selected
  //                      on, or null.
  //   actionableHosts — the subset of commonHosts the group can currently
  //                      converge to: every participating file's candidate for
  //                      that host is already selected or switch_eligible.
  //                      Controls only whether the chooser offers "Switch to
  //                      this source" — never membership, count, or launcher
  //                      visibility.

  function computeGroup(files) {
    const participants = (Array.isArray(files) ? files : [])
      .filter(function (file) { return Array.isArray(file && file.source_candidates); });
    if (!participants.length) {
      return {commonHosts: [], count: 0, activeHost: null, actionableHosts: [], targetsByHost: {}, participants: []};
    }

    // Raw membership: every host present as ANY candidate for a file, no
    // regard to whether that candidate is selected or switch-eligible.
    let common = null;
    participants.forEach(function (file) {
      const hosts = new Set();
      file.source_candidates.forEach(function (entry) {
        const host = String(entry && entry.source_host || '');
        if (host) hosts.add(host);
      });
      common = common === null
        ? hosts
        : new Set([...common].filter(function (host) { return hosts.has(host); }));
    });
    const commonHosts = [...(common || new Set())].sort();
    const commonSet = new Set(commonHosts);

    // Group ACTIVE: every file's currently selected candidate is the same
    // common host.
    const selectedHosts = participants.map(function (file) {
      const selected = file.source_candidates.find(function (entry) { return entry.is_selected; });
      return selected ? String(selected.source_host || '') : null;
    });
    const firstSelected = selectedHosts[0];
    const activeHost = (
      firstSelected &&
      selectedHosts.every(function (host) { return host === firstSelected; }) &&
      commonSet.has(firstSelected)
    ) ? firstSelected : null;

    // Actionability: a common host the whole group can converge to right now
    // — every file already selected on it, or switch_eligible for it.
    const actionableHosts = commonHosts.filter(function (host) {
      return participants.every(function (file) {
        return file.source_candidates.some(function (entry) {
          return String(entry.source_host || '') === host && (entry.is_selected || entry.switch_eligible);
        });
      });
    });

    // Host -> per-file exact candidate_id map for every common host (used only
    // once a host is chosen; membership already guarantees each participant
    // has its own candidate for a common host).
    const targetsByHost = {};
    commonHosts.forEach(function (host) {
      const map = {};
      participants.forEach(function (file) {
        const entry = file.source_candidates.find(function (item) {
          return String(item.source_host || '') === host;
        });
        if (entry) map[String(file.id)] = String(entry.candidate_id);
      });
      targetsByHost[host] = map;
    });

    return {
      commonHosts: commonHosts, count: commonHosts.length, activeHost: activeHost,
      actionableHosts: actionableHosts, targetsByHost: targetsByHost, participants: participants,
    };
  }

  function groupFromTransfer(transfer) {
    return computeGroup(transfer && Array.isArray(transfer.files) ? transfer.files : []);
  }

  // ── Launcher chip ────────────────────────────────────────────────────────

  function launcherMarkup(item) {
    const count = Number(item && item.common_candidate_count);
    if (!Number.isFinite(count) || count < 2) return '';
    const total = Math.round(count);
    const transferId = String(item.id == null ? '' : item.id);
    return '<button type="button" class="dp-candidate-chip dp-group-candidate-launcher" ' +
      'data-dp-group-candidates-trigger data-dp-transfer-id="' + esc(transferId) + '" ' +
      'aria-haspopup="dialog" aria-expanded="false" ' +
      'title="' + esc(total + ' sources common to every file') + '" ' +
      'aria-label="' + esc('Choose a common source for every file: ' + total + ' common sources') + '">' +
      glyph() +
      '<span class="dp-candidate-chip-count">' + total + '</span>' +
      '<span>Candidates</span></button>';
  }

  // ── Detail Files-header mount ────────────────────────────────────────────

  function renderMount(transfer) {
    const mount = document.querySelector('#modal-body ' + MOUNT_SELECTOR);
    if (!mount) return;
    const transferId = String((transfer && transfer.id) || mount.dataset.dpTransferId || '');
    const group = groupFromTransfer(transfer);
    if (group.count < 2) {
      mount.innerHTML = '';
      return;
    }
    mount.innerHTML = launcherMarkup({id: transferId, common_candidate_count: group.count});
  }

  function onDetailRendered(event) {
    const transferId = Number(event && event.detail && event.detail.transferId);
    detailTransferId = Number.isFinite(transferId) ? transferId : null;
    closeMenu();
    renderMount(event && event.detail && event.detail.transfer);
  }

  function onDetailClosed() {
    detailTransferId = null;
    closeMenu();
  }

  async function refreshDetailMount() {
    if (detailTransferId == null || typeof window.api !== 'function') return;
    try {
      const transfer = await window.api('GET', '/torrents/' + detailTransferId);
      renderMount(transfer);
    } catch (_) { /* the modal owns its own error surface */ }
  }

  // ── Popover ─────────────────────────────────────────────────────────────

  function actionableButtons() {
    return menuEl ? Array.from(menuEl.querySelectorAll('.dp-group-candidate-switch:not(:disabled)')) : [];
  }

  function rowMarkup(group) {
    // Every common host is rendered. Only its action differs: ACTIVE when the
    // whole group is already uniformly on it, a Switch action when the group
    // can currently converge to it (actionable), or no action at all when it
    // is common but at least one file cannot currently switch to it — the
    // existing candidate-UI convention (an ineligible candidate simply gets no
    // switch action; it is never hidden).
    return group.commonHosts.map(function (host) {
      const active = group.activeHost === host;
      const actionable = group.actionableHosts.indexOf(host) !== -1;
      const action = active
        ? '<span class="dp-group-candidate-active" aria-label="Active for every file">ACTIVE</span>'
        : actionable
          ? '<button type="button" class="dp-group-candidate-switch" data-dp-host="' + esc(host) + '"' +
            (menuBusy ? ' disabled aria-disabled="true"' : '') + '>' + SWITCH_ACTION + '</button>'
          : '<span class="dp-group-candidate-unavailable">Not switchable for every file</span>';
      return '<div class="dp-group-candidate-row" role="group" aria-label="' + esc(host) + '">' +
        '<span class="dp-group-candidate-host">' + esc(host) + '</span>' +
        '<span class="dp-group-candidate-action">' + action + '</span></div>';
    }).join('');
  }

  function renderMenu(group) {
    if (!menuEl) return;
    menuGroup = group;
    if (group.count < 2) { closeMenu(); return; }
    const heading = 'dp-group-candidates-heading';
    menuEl.innerHTML =
      '<div class="dp-group-candidate-title" id="' + heading + '">Common sources</div>' +
      '<div class="dp-group-candidate-note">Applies to every file in this transfer.</div>' +
      rowMarkup(group);
    menuEl.setAttribute('aria-labelledby', heading);
    positionMenu();
    menuEl.querySelectorAll('.dp-group-candidate-switch').forEach(function (button) {
      button.addEventListener('click', function () { chooseHost(button.dataset.dpHost); });
    });
  }

  function positionMenu() {
    if (!menuEl || !menuOwnerTrigger) return;
    const rect = menuOwnerTrigger.getBoundingClientRect();
    menuEl.style.visibility = 'hidden';
    menuEl.hidden = false;
    const menuRect = menuEl.getBoundingClientRect();
    const margin = 8;
    let left = rect.left;
    if (left + menuRect.width > window.innerWidth - margin) {
      left = Math.max(margin, window.innerWidth - margin - menuRect.width);
    }
    let top = rect.bottom + 4;
    if (top + menuRect.height > window.innerHeight - margin) {
      const above = rect.top - 4 - menuRect.height;
      top = above >= margin ? above : Math.max(margin, window.innerHeight - margin - menuRect.height);
    }
    menuEl.style.left = Math.round(left) + 'px';
    menuEl.style.top = Math.round(top) + 'px';
    menuEl.style.visibility = '';
  }

  function ensureMenu() {
    if (menuEl) return menuEl;
    menuEl = document.createElement('div');
    menuEl.className = 'dp-dropdown-menu dp-group-candidate-menu';
    menuEl.setAttribute('role', 'dialog');
    menuEl.setAttribute('aria-modal', 'false');
    menuEl.tabIndex = -1;
    menuEl.hidden = true;
    menuEl.addEventListener('keydown', onMenuKeydown);
    document.body.appendChild(menuEl);
    return menuEl;
  }

  function onMenuKeydown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      closeMenu({focusTrigger: true});
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      const buttons = actionableButtons();
      if (!buttons.length) return;
      event.preventDefault();
      const current = buttons.indexOf(document.activeElement);
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      const next = current === -1 ? 0 : (current + delta + buttons.length) % buttons.length;
      buttons[next].focus();
      return;
    }
    if (event.key === 'Home' || event.key === 'End') {
      const buttons = actionableButtons();
      if (!buttons.length) return;
      event.preventDefault();
      buttons[event.key === 'Home' ? 0 : buttons.length - 1].focus();
    }
  }

  function onDocumentPointerDown(event) {
    if (!menuEl || menuEl.hidden) return;
    const target = event.target instanceof Node ? event.target : null;
    if (target && (menuEl.contains(target) || (menuOwnerTrigger && menuOwnerTrigger.contains(target)))) return;
    closeMenu();
  }

  function onViewportChange() {
    if (menuEl && !menuEl.hidden) positionMenu();
  }

  function closeMenu(options) {
    const focusTrigger = options && options.focusTrigger;
    const trigger = menuOwnerTrigger;
    if (menuEl) {
      menuEl.hidden = true;
      menuEl.innerHTML = '';
      menuEl.style.left = '';
      menuEl.style.top = '';
    }
    if (menuOwnerTrigger) menuOwnerTrigger.setAttribute('aria-expanded', 'false');
    menuOwnerTrigger = null;
    menuTransferId = null;
    menuBusy = false;
    menuGroup = null;
    if (focusTrigger && trigger && trigger.isConnected) trigger.focus({preventScroll: true});
  }

  async function fetchTransfer(transferId) {
    if (typeof window.api !== 'function') throw new Error('offline');
    return window.api('GET', '/torrents/' + transferId);
  }

  async function open(transferId, trigger) {
    if (typeof window.api !== 'function' || transferId == null) return;
    if (menuOwnerTrigger === trigger && menuEl && !menuEl.hidden) {
      closeMenu({focusTrigger: true});
      return;
    }
    closeMenu();
    ensureMenu();
    menuOwnerTrigger = trigger || null;
    menuTransferId = Number(transferId);
    menuBusy = false;
    if (menuOwnerTrigger) menuOwnerTrigger.setAttribute('aria-expanded', 'true');
    menuEl.hidden = false;
    menuEl.innerHTML = '<div class="dp-group-candidate-note">Loading sources…</div>';
    positionMenu();
    try {
      const transfer = await fetchTransfer(menuTransferId);
      if (menuTransferId !== Number(transferId)) return;
      const group = groupFromTransfer(transfer);
      renderMenu(group);
      const first = actionableButtons()[0];
      if (first) first.focus({preventScroll: true});
      else if (menuEl) menuEl.focus({preventScroll: true});
    } catch (_) {
      closeMenu({focusTrigger: true});
      toast('Common sources are unavailable right now.', 'error');
    }
  }

  // ── Group switch orchestration ─────────────────────────────────────────

  async function switchOne(transferId, artifactId, candidateId) {
    const response = await fetch(
      '/api/torrents/' + transferId + '/artifacts/' + artifactId + '/candidate',
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({candidate_id: candidateId}),
      },
    );
    const payload = await response.json().catch(function () { return {}; });
    if (!response.ok) {
      const detail = payload && payload.detail;
      const message = (detail && (detail.message || detail.category)) ||
        response.statusText || 'switch failed';
      const error = new Error(String(message).replace(/_/g, ' '));
      error.detail = detail;
      throw error;
    }
    return payload;
  }

  async function refreshSurfaces() {
    const jobs = [];
    if (typeof window.loadTorrents === 'function') jobs.push(Promise.resolve(window.loadTorrents()));
    if (typeof window.loadRecent === 'function') jobs.push(Promise.resolve(window.loadRecent()));
    await Promise.allSettled(jobs);
    await refreshDetailMount();
  }

  async function chooseHost(host) {
    if (!host || menuBusy || menuTransferId == null) return;
    const transferId = menuTransferId;
    menuBusy = true;
    if (menuGroup) renderMenu(menuGroup);  // disable every row while the switch runs

    // Action-time revalidation: the menu can go stale while open.
    let transfer;
    try {
      transfer = await fetchTransfer(transferId);
    } catch (_) {
      menuBusy = false;
      toast('Could not re-check the transfer. Nothing was changed.', 'error');
      await refreshSurfaces();
      closeMenu();
      return;
    }
    // Revalidation distinguishes two independent stale-truth failure modes:
    // the host may no longer be a MEMBER of the common set at all (one file
    // lost that canonical host entirely), or it may still be common but no
    // longer ACTIONABLE (a file's candidate for it stopped being switch-
    // eligible). Neither issues a mutation; each gets its own concise report.
    const group = groupFromTransfer(transfer);
    if (group.commonHosts.indexOf(host) === -1) {
      menuBusy = false;
      renderMenu(group);
      await refreshSurfaces();
      toast(host + ' is no longer common to the entire transfer.', 'error');
      return;
    }
    if (group.actionableHosts.indexOf(host) === -1) {
      menuBusy = false;
      renderMenu(group);
      await refreshSurfaces();
      toast(host + ' is currently not switchable for every file.', 'error');
      return;
    }
    const targets = group.targetsByHost[host];

    const moves = [];
    group.participants.forEach(function (file) {
      const selected = file.source_candidates.find(function (entry) { return entry.is_selected; });
      const currentHost = selected ? String(selected.source_host || '') : null;
      const candidateId = targets[String(file.id)];
      if (currentHost === host || !candidateId) return;
      moves.push({artifactId: file.id, candidateId: candidateId});
    });

    let switched = 0;
    let failure = null;
    for (const move of moves) {
      try {
        await switchOne(transferId, move.artifactId, move.candidateId);
        switched += 1;
      } catch (error) {
        failure = error;
        break;
      }
    }

    menuBusy = false;
    await refreshSurfaces();

    // The list re-render / mount refresh may have replaced the launcher node the
    // chooser was anchored to. Re-acquire it before re-rendering the popover
    // from authoritative truth; if it is gone, the chooser closes.
    const reAnchor = document.querySelector(
      TRIGGER_SELECTOR + '[data-dp-transfer-id="' + String(transferId) + '"]');
    if (reAnchor && menuOwnerTrigger && reAnchor !== menuOwnerTrigger) {
      menuOwnerTrigger.setAttribute('aria-expanded', 'false');
      menuOwnerTrigger = reAnchor;
      menuOwnerTrigger.setAttribute('aria-expanded', 'true');
    }
    let fresh = null;
    try { fresh = groupFromTransfer(await fetchTransfer(transferId)); } catch (_) { /* keep going */ }
    if (fresh && fresh.count >= 2 && menuOwnerTrigger && menuOwnerTrigger.isConnected) renderMenu(fresh);
    else closeMenu();

    const alreadyOn = moves.length === 0;
    if (failure) {
      toast({
        title: 'Group did not fully converge on ' + host,
        body: switched + ' of ' + moves.length + ' file' + (moves.length === 1 ? '' : 's') +
          ' switched before: ' + (failure.message || 'a file could not switch') + '.',
      }, 'error');
    } else if (alreadyOn) {
      toast('Every file is already on ' + host + '.', 'info');
    } else {
      toast('Every file switched to ' + host + '.', 'success');
    }
  }

  // ── Wiring ─────────────────────────────────────────────────────────────

  function onDocumentClick(event) {
    const target = event.target instanceof Element ? event.target.closest(TRIGGER_SELECTOR) : null;
    if (!target) return;
    event.preventDefault();
    event.stopPropagation();
    const transferId = Number(target.dataset.dpTransferId);
    if (Number.isFinite(transferId)) open(transferId, target);
  }

  function install() {
    document.addEventListener('click', onDocumentClick);
    document.addEventListener('pointerdown', onDocumentPointerDown, true);
    document.addEventListener('debridpulse:detail-rendered', onDetailRendered);
    document.addEventListener('debridpulse:detail-closed', onDetailClosed);
    window.addEventListener('resize', onViewportChange, {passive: true});
    window.addEventListener('scroll', onViewportChange, {passive: true, capture: true});
  }

  window.DPGroupCandidates = Object.freeze({computeGroup: computeGroup, launcherMarkup: launcherMarkup, open: open});

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', install, {once: true});
  } else {
    install();
  }
})();
