/* DebridPulse v1.0.12 transfer-level common-source group switching.
 *
 * A thin wrapper over the already-qualified per-file canonical candidate and
 * manual-switch machinery. It answers one derived question — which source
 * hosts exist as a canonical candidate on EVERY actual file of a transfer
 * (common-source MEMBERSHIP) — and, separately, which of those common hosts
 * the REMAINING-WORK part of the group can currently converge to
 * (ACTIONABILITY) — then drives the existing exact per-file switch endpoint
 * for the files that actually need to move. Membership and actionability are
 * independent: a source stays common (counted, shown) even when it is not
 * currently switchable for every remaining file. It owns NO switching engine,
 * candidate model, recovery, provenance, routing, or lifecycle: those stay
 * backend-owned.
 *
 * One shared runtime for three surfaces — Details Files header, Downloads,
 * Dashboard Recent Items — distinguished by an explicit `surface` identity
 * (`dashboard_recent` | `downloads` | `details`). The per-file candidate
 * disclosure in Details is a separate owner (ui-detail-candidates.js) and is
 * never touched here.
 *
 * ── Anchor identity ──────────────────────────────────────────────────────
 * The open chooser is owned by a SEMANTIC anchor — (transferId, surface) —
 * never a bare DOM node reference. A list/Details re-render legitimately
 * replaces the launcher element for the same transfer/surface; the runtime
 * re-resolves the live launcher by that identity rather than trusting a
 * stale pointer, and never re-anchors across surfaces merely because a
 * transfer id happens to match elsewhere. While the live launcher is
 * transiently unresolvable the chooser keeps its last valid geometry instead
 * of teleporting to the viewport origin; once a surface completes a render
 * cycle with no equivalent launcher, that is CONFIRMED permanent loss and the
 * chooser closes gracefully (geometry cleared, focus never left on a
 * detached node) without disturbing an already-issued switch in flight.
 */
(function () {
  'use strict';

  const TRIGGER_ATTR = 'data-dp-group-candidates-trigger';
  const TRIGGER_SELECTOR = '[' + TRIGGER_ATTR + ']';
  const MOUNT_SELECTOR = '[data-dp-group-candidates-mount]';
  const SWITCH_ACTION = 'Switch to this source';

  let menuEl = null;
  let anchorTrigger = null;       // best-known LIVE DOM node for the open anchor, or null
  let lastValidRect = null;       // last known geometry; survives transient trigger loss
  let menuTransferId = null;
  let menuSurface = null;         // 'dashboard_recent' | 'downloads' | 'details'
  let menuMode = 'group';         // 'group' | 'artifact' (DP 1.0.12 Contextual Candidate Action Scope task)
  let menuArtifactId = null;      // artifact target when menuMode === 'artifact'
  let menuArtifactFile = null;    // last-rendered fresh Details file row for the open artifact chooser
  let menuBusy = false;
  let menuVisible = false;
  let menuGroup = null;
  let menuSession = 0;            // bumped on every close/loss; invalidates stale async tails
  let detailTransferId = null;

  function esc(value) {
    if (typeof window.esc === 'function') return window.esc(value);
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function cssEscape(value) {
    return (window.CSS && typeof window.CSS.escape === 'function')
      ? window.CSS.escape(value) : String(value).replace(/["\\]/g, '\\$&');
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
  // THREE distinct truths, kept independent on purpose:
  //   commonHosts / count — MEMBERSHIP/HISTORY: raw canonical-host
  //                      intersection across every participating file's
  //                      candidates (including completed files), regardless
  //                      of is_selected/switch_eligible/artifact state. This
  //                      is what "common source" means and what the launcher
  //                      count/history badge is based on. UNCHANGED by
  //                      remaining-work exclusion.
  //   activeHost      — REMAINING-WORK ACTIVE: the one common host every
  //                      REMAINING-WORK participant is uniformly selected on,
  //                      or null. A completed file's own selection never
  //                      vetoes this — it keeps its own truthful historical
  //                      provenance instead. When there is no remaining work
  //                      at all, this is always null: never a vacuous truth
  //                      over an empty set.
  //   actionableHosts — REMAINING-WORK ACTIONABILITY: the subset of
  //                      commonHosts the REMAINING-WORK group can currently
  //                      converge to — every remaining-work file's candidate
  //                      for that host is already selected or
  //                      switch_eligible. Controls only whether the chooser
  //                      offers "Switch to this source" — never membership,
  //                      count, or launcher visibility.

  function isCompletedFile(file) {
    return String(file && file.status || '').trim().toLowerCase() === 'completed';
  }

  function computeGroup(files) {
    const participants = (Array.isArray(files) ? files : [])
      .filter(function (file) { return Array.isArray(file && file.source_candidates); });
    if (!participants.length) {
      return {
        commonHosts: [], count: 0, activeHost: null, actionableHosts: [], targetsByHost: {},
        participants: [], actionParticipants: [],
      };
    }

    // Raw membership: every host present as ANY candidate for a file, no
    // regard to whether that candidate is selected or switch-eligible, and
    // including completed files — membership/history is whole-transfer truth.
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

    // Remaining-work participants: membership files that still need
    // acquisition. A completed file stays a MEMBERSHIP participant above (it
    // still owns intersection/count/history) but never gates or receives
    // group-switch mutation, and never vetoes remaining-work ACTIVE — it
    // already delivered its bytes from wherever it delivered them, and that
    // truthful historical provenance is never rewritten.
    const actionParticipants = participants.filter(function (file) { return !isCompletedFile(file); });

    // Remaining-work ACTIVE: every REMAINING-WORK file's currently selected
    // candidate is the same common host. Completed files are excluded from
    // this computation entirely. With no remaining work at all, there is no
    // operational ACTIVE source to switch (never a vacuous truth over an
    // empty set).
    let activeHost = null;
    if (actionParticipants.length) {
      const selectedHosts = actionParticipants.map(function (file) {
        const selected = file.source_candidates.find(function (entry) { return entry.is_selected; });
        return selected ? String(selected.source_host || '') : null;
      });
      const firstSelected = selectedHosts[0];
      activeHost = (
        firstSelected &&
        selectedHosts.every(function (host) { return host === firstSelected; }) &&
        commonSet.has(firstSelected)
      ) ? firstSelected : null;
    }

    // Actionability: a common host the REMAINING-WORK group can converge to
    // right now — every unfinished file already selected on it, or
    // switch_eligible for it. When there is no remaining work at all, no host
    // is actionable: there is no acquisition left to move, never a vacuous
    // truth over an empty set.
    const actionableHosts = actionParticipants.length ? commonHosts.filter(function (host) {
      return actionParticipants.every(function (file) {
        return file.source_candidates.some(function (entry) {
          return String(entry.source_host || '') === host && (entry.is_selected || entry.switch_eligible);
        });
      });
    }) : [];

    // Host -> per-file exact candidate_id map, restricted to files that still
    // need to move (only these are ever POSTed to; membership already
    // guarantees each of them has its own candidate for a common host).
    const targetsByHost = {};
    commonHosts.forEach(function (host) {
      const map = {};
      actionParticipants.forEach(function (file) {
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
      actionParticipants: actionParticipants,
    };
  }

  function groupFromTransfer(transfer) {
    return computeGroup(transfer && Array.isArray(transfer.files) ? transfer.files : []);
  }

  // ── Candidate-action scope (DP 1.0.12 Contextual Candidate Action Scope
  // task) ── a THIRD, orthogonal read fact alongside group MEMBERSHIP/HISTORY
  // (commonHosts/count, above) and per-host ACTIONABILITY (actionableHosts):
  // the smallest unambiguous candidate-switch operation scope available right
  // now — none | artifact | group. Never redefines membership; never picks an
  // arbitrary artifact when more than one is movable.
  //
  // A "movable" file is the exact same operation-classification bar the
  // backend bounded projection uses: a distinct, backend-authored
  // ``switch_eligible`` candidate exists on the file's full canonical
  // ``acquisition_candidates`` set (never a JS lifecycle-state whitelist, and
  // never gated on ``source_scope === 'host'``).

  function isMovableFile(file) {
    return Array.isArray(file && file.acquisition_candidates) &&
      file.acquisition_candidates.some(function (candidate) { return candidate && candidate.switch_eligible; });
  }

  // Fresh, click-time operation-context classification from an authoritative
  // Details payload — the same precedence the backend classifier uses
  // (exactly one movable artifact always wins over a group target):
  //   movable.length === 1  -> artifact, targeting that exact file
  //   movable.length > 1    -> group, IF at least one common host is both
  //                            actionable and movement-producing (excludes the
  //                            already-uniform ACTIVE host, which needs zero
  //                            movement)
  //   otherwise              -> none
  function classifyFreshScope(transfer) {
    const files = transfer && Array.isArray(transfer.files) ? transfer.files : [];
    const movable = files.filter(isMovableFile);
    if (movable.length === 1) {
      const file = movable[0];
      const count = Array.isArray(file.acquisition_candidates) ? file.acquisition_candidates.length : 0;
      return {scope: 'artifact', artifactId: Number(file.id), count: count};
    }
    if (movable.length > 1) {
      const group = groupFromTransfer(transfer);
      const movementHosts = group.actionableHosts.filter(function (host) { return host !== group.activeHost; });
      if (movementHosts.length > 0) {
        return {scope: 'group', artifactId: null, count: movementHosts.length};
      }
    }
    return {scope: 'none', artifactId: null, count: 0};
  }

  // ── Launcher chip ────────────────────────────────────────────────────────

  function actionLauncherMarkup(transferId, variant, surface, mode, artifactId, count) {
    const total = Math.max(0, Math.round(Number(count) || 0));
    const labeled = variant === 'labeled';
    const title = mode === 'artifact'
      ? total + ' sources available for this file'
      : total + ' sources can converge for remaining files';
    const ariaLabel = mode === 'artifact'
      ? 'Choose a source for this file; ' + total + ' sources are available.'
      : 'Choose a source for remaining files; ' + total + ' sources can converge.';
    return '<button type="button" class="dp-candidate-chip dp-group-candidate-launcher" ' +
      TRIGGER_ATTR + ' data-dp-transfer-id="' + esc(String(transferId == null ? '' : transferId)) + '" ' +
      (surface ? 'data-dp-surface="' + esc(surface) + '" ' : '') +
      'data-dp-mode="' + esc(mode) + '" ' +
      (mode === 'artifact' ? 'data-dp-artifact-id="' + esc(String(artifactId)) + '" ' : '') +
      'aria-haspopup="dialog" aria-expanded="false" ' +
      'title="' + esc(title) + '" ' +
      'aria-label="' + esc(ariaLabel) + '">' +
      glyph() +
      '<span class="dp-candidate-chip-count">' + total + '</span>' +
      (labeled ? ' <span>Candidates</span>' : '') +
      '</button>';
  }

  function staticHistoryMarkup(item, variant) {
    const count = Number(item && item.common_candidate_count);
    if (!Number.isFinite(count) || count < 2) return '';
    const total = Math.round(count);
    const labeled = variant === 'labeled';
    const historyTitle = total + ' sources common to every file';
    // No meaningful current candidate-switch action: the common-source count
    // remains useful HISTORY. Render a visually equivalent, non-interactive
    // indicator — not a <button>, no aria-haspopup/aria-expanded, never opens
    // a chooser. common_candidate_count itself is never overwritten.
    return '<span class="dp-candidate-chip dp-group-candidate-history" ' +
      'title="' + esc(historyTitle) + '" ' +
      'aria-label="' + esc(total + ' sources were common to every file') + '">' +
      glyph() +
      '<span class="dp-candidate-chip-count">' + total + '</span>' +
      (labeled ? ' <span>Candidates</span>' : '') +
      '</span>';
  }

  // Preserved byte-for-byte for callers/fixtures that do not (yet) supply
  // ``candidate_action_scope`` (e.g. a pre-existing browser-test fixture
  // built before this task). Every real production row from
  // api/operational_downloads.py now always supplies the new field.
  function legacyLauncherMarkup(item, variant, surface) {
    const count = Number(item && item.common_candidate_count);
    if (!Number.isFinite(count) || count < 2) return '';
    const total = Math.round(count);
    const transferId = String(item.id == null ? '' : item.id);
    const labeled = variant === 'labeled';
    const remainingField = item && item.group_remaining_count;
    const hasRemainingWork = remainingField == null || !Number.isFinite(Number(remainingField))
      ? true : Number(remainingField) > 0;
    const historyTitle = total + ' sources common to every file';
    if (!hasRemainingWork) {
      return '<span class="dp-candidate-chip dp-group-candidate-history" ' +
        'title="' + esc(historyTitle) + '" ' +
        'aria-label="' + esc(total + ' sources were common to every file') + '">' +
        glyph() +
        '<span class="dp-candidate-chip-count">' + total + '</span>' +
        (labeled ? ' <span>Candidates</span>' : '') +
        '</span>';
    }
    return '<button type="button" class="dp-candidate-chip dp-group-candidate-launcher" ' +
      TRIGGER_ATTR + ' data-dp-transfer-id="' + esc(transferId) + '" ' +
      (surface ? 'data-dp-surface="' + esc(surface) + '" ' : '') +
      'aria-haspopup="dialog" aria-expanded="false" ' +
      'title="' + esc(historyTitle) + '" ' +
      'aria-label="' + esc('Choose a source for remaining files; ' + total + ' sources are common to every file.') + '">' +
      glyph() +
      '<span class="dp-candidate-chip-count">' + total + '</span>' +
      (labeled ? ' <span>Candidates</span>' : '') +
      '</button>';
  }

  // Consumed as-is by ui-downloads-presentation.js / ui-dashboard-transfer-
  // presentation.js — the bounded list item is already plumbed through
  // unchanged; this is the only place list-row candidate-action semantics
  // are interpreted (DP 1.0.12 Contextual Candidate Action Scope task, §9).
  function launcherMarkup(item, variant, surface) {
    const scope = item && item.candidate_action_scope;
    if (scope === 'artifact') {
      return actionLauncherMarkup(item.id, variant, surface, 'artifact', item.candidate_action_artifact_id, item.candidate_action_count);
    }
    if (scope === 'group') {
      return actionLauncherMarkup(item.id, variant, surface, 'group', null, item.candidate_action_count);
    }
    if (scope === 'none') {
      return staticHistoryMarkup(item, variant);
    }
    return legacyLauncherMarkup(item, variant, surface);
  }

  // ── Detail Files-header mount ────────────────────────────────────────────
  //
  // DP 1.0.12 Final Pre-Commit Verification (Case O): a prior round added
  // File-Selection-applicability arbitration here, gated on the premise that
  // File Selection and an actionable candidate could compete for this one
  // slot. Verification against production (transfers/file_selection.py
  // selection_mutable, transfers/repository.py commit_selected_manifest,
  // transfers/engine.py's AVAILABLE/selecting branch, transfers/canonical.py
  // attach()/canonical_artifacts()) proved STRUCTURAL_MUTUAL_EXCLUSION for
  // the actual competing operation: File Selection's INTERACTIVE control
  // (mutable=true) can never coexist with a materialized per-file artifact,
  // because commit_selected_manifest sets manifest_committed_at (which is
  // the entirety of selection_mutable's condition) strictly BEFORE the
  // engine ever fans out the per-file child requests/artifacts that any
  // candidate-action classification or cross-transfer canonical.py:attach()
  // could ever target -- see backend/tests/test_file_selection_api.py
  // test_mutable_file_selection_and_materialized_artifacts_are_temporally_exclusive.
  // A settled, non-mutable File Selection summary (decision=explicit) CAN
  // legitimately coexist with an actionable candidate for the same torrent
  // (Section 1's product note), but that summary is passive/historical, not
  // a competing operation -- the same passive-vs-action distinction this
  // codebase already draws for common_candidate_count history badges. No
  // arbitration is needed. This restores the pre-Case-O rendering, keeping
  // the (unrelated, still-valid) one-movable-artifact ARTIFACT-mode addition.

  function renderMount(transfer) {
    const mount = document.querySelector('#modal-body ' + MOUNT_SELECTOR);
    if (!mount) return;
    const transferId = String((transfer && transfer.id) || mount.dataset.dpTransferId || '');
    const files = transfer && Array.isArray(transfer.files) ? transfer.files : [];
    const movable = files.filter(isMovableFile);
    // Exactly one movable artifact always wins over a group target (matches
    // the backend classifier precedence exactly) — purely additive: when
    // movable.length is 0 or > 1, the EXISTING group gating below is
    // completely unchanged.
    if (movable.length === 1) {
      const file = movable[0];
      const count = Array.isArray(file.acquisition_candidates) ? file.acquisition_candidates.length : 0;
      mount.innerHTML = actionLauncherMarkup(transferId, 'labeled', 'details', 'artifact', file.id, count);
      onSurfaceRendered('details');
      return;
    }
    const group = groupFromTransfer(transfer);
    // The group launcher is an ACTION affordance, not a history viewer:
    // Details exposes it only when there is meaningful remaining acquisition
    // work that can still be switched. A completed transfer or a terminal
    // failed/non-actionable transfer gets no launcher at all here (Details
    // already has the per-file/candidate/provenance surfaces for historical
    // inspection) — never a disabled/static chip in its place. This is the
    // EXISTING (pre-task) gating, preserved exactly as shipped.
    if (group.count < 2 || !group.actionParticipants.length) {
      mount.innerHTML = '';
      return;
    }
    mount.innerHTML = actionLauncherMarkup(transferId, 'labeled', 'details', 'group', null, group.count);
    onSurfaceRendered('details');
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
    // Every common host is rendered. Only its action differs: ACTIVE when
    // every remaining-work participant is already uniformly on it, a Switch
    // action when the remaining-work group can currently converge to it
    // (actionable), or no action at all when it is common but at least one
    // remaining-work file cannot currently switch to it — the existing
    // candidate-UI convention (an ineligible candidate simply gets no switch
    // action; it is never hidden).
    return group.commonHosts.map(function (host) {
      const active = group.activeHost === host;
      const actionable = group.actionableHosts.indexOf(host) !== -1;
      const action = active
        ? '<span class="dp-group-candidate-active" aria-label="Active for remaining files">ACTIVE</span>'
        : actionable
          ? '<button type="button" class="dp-group-candidate-switch" data-dp-host="' + esc(host) + '"' +
            (menuBusy ? ' disabled aria-disabled="true"' : '') + '>' + SWITCH_ACTION + '</button>'
          : '<span class="dp-group-candidate-unavailable">Not switchable for every remaining file</span>';
      return '<div class="dp-group-candidate-row" role="group" aria-label="' + esc(host) + '">' +
        '<span class="dp-group-candidate-host">' + esc(host) + '</span>' +
        '<span class="dp-group-candidate-action">' + action + '</span></div>';
    }).join('');
  }

  function progressMarkup(host, completed, total) {
    const pct = total > 0 ? Math.round((completed / total) * 100) : 0;
    return '<div class="dp-group-candidate-title">Common sources</div>' +
      '<div class="dp-group-candidate-progress">' +
        '<div class="dp-group-candidate-progress-label">Switching to ' + esc(host) + '</div>' +
        '<div class="dp-group-candidate-progress-count">' + completed + ' of ' + total +
          ' file' + (total === 1 ? '' : 's') + '</div>' +
        '<div class="dp-group-candidate-progress-bar" aria-hidden="true">' +
          '<div class="dp-group-candidate-progress-fill" style="width:' + pct + '%"></div></div>' +
      '</div>';
  }

  // A multi-file group switch runs several sequential POSTs; a disabled
  // button alone does not tell the operator how far a several-second
  // operation has gotten. The chooser stays open and visibly busy for its
  // whole duration instead of just disabling its own controls. Progress
  // NEVER establishes a new popover location: it replaces only the menu's
  // internal content and repositions relative to the SAME semantic anchor.
  function renderProgress(host, completed, total) {
    if (!menuEl || !menuVisible) return;
    menuEl.setAttribute('aria-busy', 'true');
    menuEl.innerHTML = progressMarkup(host, completed, total);
    positionMenu();
  }

  function renderMenu(group) {
    if (!menuEl) return;
    menuGroup = group;
    if (group.count < 2) { closeMenu(); return; }
    menuEl.hidden = false;
    menuVisible = true;
    menuEl.removeAttribute('aria-busy');
    const heading = 'dp-group-candidates-heading';
    const note = group.actionParticipants.length
      ? 'Switches the remaining files in this transfer.'
      : 'No remaining files can be switched.';
    menuEl.innerHTML =
      '<div class="dp-group-candidate-title" id="' + heading + '">Common sources</div>' +
      '<div class="dp-group-candidate-note">' + esc(note) + '</div>' +
      rowMarkup(group);
    menuEl.setAttribute('aria-labelledby', heading);
    positionMenu();
    menuEl.querySelectorAll('.dp-group-candidate-switch').forEach(function (button) {
      button.addEventListener('click', function () { chooseHost(button.dataset.dpHost); });
    });
  }

  // ── Semantic anchor resolution ───────────────────────────────────────────
  //
  // The anchor identity is (transferId, surface), never a bare DOM node. A
  // list/Details re-render legitimately produces a NEW element instance for
  // the same logical launcher; this re-resolves it by identity so a stale
  // pointer never causes a (0,0) teleport or a cross-surface jump.

  function resolveTrigger(transferId, surface) {
    if (transferId == null || !surface) return null;
    return document.querySelector(
      TRIGGER_SELECTOR +
      '[data-dp-transfer-id="' + cssEscape(String(transferId)) + '"]' +
      '[data-dp-surface="' + cssEscape(surface) + '"]',
    );
  }

  function currentTrigger() {
    if (anchorTrigger && anchorTrigger.isConnected) return anchorTrigger;
    const resolved = resolveTrigger(menuTransferId, menuSurface);
    if (resolved) anchorTrigger = resolved;
    return (anchorTrigger && anchorTrigger.isConnected) ? anchorTrigger : null;
  }

  function positionMenu() {
    if (!menuEl || !menuVisible) return;
    const trigger = currentTrigger();
    let rect = null;
    if (trigger) {
      rect = trigger.getBoundingClientRect();
      lastValidRect = rect;
    } else if (lastValidRect) {
      // Transient absence (e.g. a same-surface re-render is mid-flight): keep
      // the chooser at its last valid position instead of jumping to (0,0).
      rect = lastValidRect;
    }
    if (!rect) return;
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

  // Called whenever a surface (Dashboard Recent, Downloads, Details) finishes
  // a render/refresh cycle. If the open chooser belongs to that surface, this
  // is the single point that distinguishes transient replacement (an
  // equivalent live launcher is found -> silently reattach and reposition)
  // from CONFIRMED PERMANENT anchor loss (no equivalent launcher after a
  // completed render -> close gracefully). Never touches a different surface.
  function onSurfaceRendered(surface) {
    if (menuTransferId == null || menuSurface !== surface) return;
    const resolved = resolveTrigger(menuTransferId, menuSurface);
    if (resolved) {
      // Section 7 stale-launcher rule: a background LIST-surface refresh can
      // reveal that the SAME (transferId, surface) launcher now represents a
      // different operation scope/target (group -> artifact, artifact ->
      // group/another artifact, either -> none) while a chooser for the OLD
      // scope is open. Absent data-dp-mode (a pre-task/legacy fixture that
      // never carried candidate_action_scope) defaults to 'group', matching
      // menuMode's own default, so this never spuriously fires for a caller
      // that has not been wired for the new fields. Exempts exactly the
      // pre-existing Details Files-header GROUP entry point (surface ===
      // 'details' && menuMode === 'group'): it already recomputes fresh at
      // every render (renderMount) and its own long-shipped revalidation
      // (renderMenu/chooseHost) must keep an open chooser visible through
      // its OWN internal refresh cycles rather than being force-closed here
      // (Section 4.1's "existing group-mode... may remain exactly as
      // shipped" carve-out). The NEW Details ARTIFACT-mode chooser has no
      // such legacy behavior to preserve and stays protected.
      const resolvedMode = String(resolved.dataset.dpMode || 'group');
      const resolvedArtifactId = resolved.dataset.dpArtifactId != null && resolved.dataset.dpArtifactId !== ''
        ? Number(resolved.dataset.dpArtifactId) : null;
      const legacyDetailsGroup = surface === 'details' && menuMode === 'group';
      const scopeChanged = !legacyDetailsGroup && menuVisible && !menuBusy && (resolvedMode !== menuMode ||
        (resolvedMode === 'artifact' && resolvedArtifactId !== menuArtifactId));
      if (scopeChanged) {
        // Never silently morph one semantic operation into another under the
        // operator: close and let them act on the freshly rendered control.
        closeMenu();
        toast('Candidate options changed.', 'info');
        return;
      }
      anchorTrigger = resolved;
      lastValidRect = resolved.getBoundingClientRect();
      if (menuVisible) {
        resolved.setAttribute('aria-expanded', 'true');
        positionMenu();
      }
      return;
    }
    closeMenu();
  }

  function safeContainerFor(surface) {
    const id = surface === 'dashboard_recent' ? 'view-dashboard'
      : surface === 'downloads' ? 'view-torrents'
      : surface === 'details' ? 'modal-body' : null;
    return id ? document.getElementById(id) : null;
  }

  // Never leave focus on a detached node, and never let it silently fall
  // back to <body> merely because the row re-rendered or the popover was
  // hidden. Only reclaims focus when it has actually gone stray (landed on
  // <body> as a side effect of the DOM mutation) — an unrelated, deliberate
  // click elsewhere on the page is left alone.
  function reclaimFocusAfterLoss(surface) {
    const active = document.activeElement;
    if (active && active !== document.body) return;
    const container = safeContainerFor(surface);
    if (!container) return;
    if (!container.hasAttribute('tabindex')) container.setAttribute('tabindex', '-1');
    container.focus({preventScroll: true});
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
      // A running group switch is not cancellable; do not let Escape imply
      // otherwise or silently abandon visual ownership of it mid-flight.
      if (menuBusy) return;
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
    if (!menuEl || menuEl.hidden || menuBusy) return;
    const target = event.target instanceof Node ? event.target : null;
    const trigger = currentTrigger();
    if (target && (menuEl.contains(target) || (trigger && trigger.contains(target)))) return;
    closeMenu();
  }

  function onViewportChange() {
    if (menuVisible) positionMenu();
  }

  // The single graceful-close primitive, used for BOTH a user-initiated
  // dismissal (Escape, click-away, toggle) and a confirmed-permanent-anchor-
  // loss close. While a group switch is busy, closing the visual chooser
  // MUST NOT be interpreted as cancelling the already-issued operation: the
  // anchor identity (transferId/surface/busy) is preserved so the in-flight
  // switch can finish/fail normally and still report its final toast: only
  // ``menuSession`` is bumped, which stops that operation's tail from
  // resurrecting the chooser afterward (on this surface or any other).
  function closeMenu(options) {
    const focusTrigger = options && options.focusTrigger;
    const surface = menuSurface;
    if (anchorTrigger) anchorTrigger.setAttribute('aria-expanded', 'false');
    const liveTrigger = resolveTrigger(menuTransferId, menuSurface);
    if (liveTrigger && liveTrigger !== anchorTrigger) liveTrigger.setAttribute('aria-expanded', 'false');
    if (menuEl) {
      menuEl.hidden = true;
      menuEl.innerHTML = '';
      menuEl.style.left = '';
      menuEl.style.top = '';
      menuEl.removeAttribute('aria-busy');
    }
    anchorTrigger = null;
    lastValidRect = null;
    menuVisible = false;
    menuGroup = null;
    menuSession += 1;
    if (!menuBusy) {
      menuTransferId = null;
      menuSurface = null;
      menuMode = 'group';
      menuArtifactId = null;
      menuArtifactFile = null;
    }
    if (focusTrigger && liveTrigger && liveTrigger.isConnected) {
      liveTrigger.focus({preventScroll: true});
    } else {
      reclaimFocusAfterLoss(surface);
    }
  }

  async function fetchTransfer(transferId) {
    if (typeof window.api !== 'function') throw new Error('offline');
    return window.api('GET', '/torrents/' + transferId);
  }

  // ``strict`` (DP 1.0.12 Contextual Candidate Action Scope task, §7):
  // list-surface launchers are a discovery HINT that can go stale relative
  // to fresh Details, so those callers pass strict=true to abort (no
  // mutation, no morph, a "Candidate options changed." toast, and a
  // surface refresh) when the freshly classified scope no longer agrees.
  // The pre-existing Details Files-header group entry point is called with
  // strict=false (its long-shipped per-host revalidation, in renderMenu/
  // chooseHost below, is left completely untouched — Section 4.1's explicit
  // "existing group-mode... may remain exactly as shipped" carve-out).
  async function open(transferId, surface, trigger, options) {
    const strict = Boolean(options && options.strict);
    if (typeof window.api !== 'function' || transferId == null) return;
    if (menuBusy) return;  // a running switch stays visually owned throughout
    if (menuTransferId === Number(transferId) && menuSurface === surface && menuMode === 'group' && menuVisible) {
      closeMenu({focusTrigger: true});
      return;
    }
    closeMenu();
    ensureMenu();
    menuTransferId = Number(transferId);
    menuSurface = surface || null;
    menuMode = 'group';
    menuArtifactId = null;
    anchorTrigger = trigger || resolveTrigger(menuTransferId, menuSurface);
    lastValidRect = anchorTrigger ? anchorTrigger.getBoundingClientRect() : null;
    menuBusy = false;
    menuVisible = true;
    const session = menuSession;
    if (anchorTrigger) anchorTrigger.setAttribute('aria-expanded', 'true');
    menuEl.hidden = false;
    menuEl.innerHTML = '<div class="dp-group-candidate-note">Loading sources…</div>';
    positionMenu();
    try {
      const transfer = await fetchTransfer(menuTransferId);
      if (menuSession !== session) return;
      if (strict) {
        const fresh = classifyFreshScope(transfer);
        if (fresh.scope !== 'group') {
          closeMenu();
          toast('Candidate options changed.', 'info');
          await refreshSurfaces();
          return;
        }
      }
      const group = groupFromTransfer(transfer);
      renderMenu(group);
      const first = actionableButtons()[0];
      if (first) first.focus({preventScroll: true});
      else if (menuEl) menuEl.focus({preventScroll: true});
    } catch (_) {
      if (menuSession !== session) return;
      closeMenu({focusTrigger: true});
      toast('Common sources are unavailable right now.', 'error');
    }
  }

  // ── Artifact-mode chooser (DP 1.0.12 Contextual Candidate Action Scope
  // task) ── the smallest unambiguous scope: exactly one movable artifact.
  // Reuses the SAME popover/anchor/keyboard/focus machinery as group mode
  // (Section 14.1: no second modal, no second switching subsystem) and the
  // SAME existing per-artifact candidate-switch POST (switchOne, below).

  function artifactRowMarkup(file) {
    const candidates = Array.isArray(file && file.acquisition_candidates) ? file.acquisition_candidates : [];
    // Every canonical candidate the generic Details presentation normally
    // shows is rendered; only a backend-authored switch_eligible non-selected
    // candidate receives a switch action — a candidate may be displayed
    // without being switchable (Section 14.2).
    return candidates.map(function (candidate) {
      const active = Boolean(candidate && candidate.is_selected);
      const action = active
        ? '<span class="dp-group-candidate-active" aria-label="Active source">ACTIVE</span>'
        : (candidate && candidate.switch_eligible)
          ? '<button type="button" class="dp-group-candidate-switch" data-dp-candidate-id="' + esc(candidate.candidate_id) + '"' +
            (menuBusy ? ' disabled aria-disabled="true"' : '') + '>' + SWITCH_ACTION + '</button>'
          : '<span class="dp-group-candidate-unavailable">Not currently switchable</span>';
      const label = String((candidate && candidate.source_label) || 'Source');
      return '<div class="dp-group-candidate-row" role="group" aria-label="' + esc(label) + '">' +
        '<span class="dp-group-candidate-host">' + esc(label) + '</span>' +
        '<span class="dp-group-candidate-action">' + action + '</span></div>';
    }).join('');
  }

  function renderArtifactMenu(file) {
    if (!menuEl) return;
    menuArtifactFile = file;
    menuEl.hidden = false;
    menuVisible = true;
    menuEl.removeAttribute('aria-busy');
    const heading = 'dp-group-candidates-heading';
    menuEl.innerHTML =
      '<div class="dp-group-candidate-title" id="' + heading + '">Candidate sources</div>' +
      '<div class="dp-group-candidate-note">Switches the source for this file.</div>' +
      artifactRowMarkup(file);
    menuEl.setAttribute('aria-labelledby', heading);
    positionMenu();
    menuEl.querySelectorAll('.dp-group-candidate-switch').forEach(function (button) {
      button.addEventListener('click', function () { chooseArtifactCandidate(button.dataset.dpCandidateId); });
    });
  }

  // Always strict (Section 7): an artifact-mode launcher is either a list
  // discovery hint or the Details Files-header's own fresh-computed target,
  // and both must be reconfirmed against a fresh Details fetch before
  // rendering any row, exactly like the stale-launcher rule requires.
  async function openArtifact(transferId, artifactId, surface, trigger) {
    if (typeof window.api !== 'function' || transferId == null || artifactId == null) return;
    if (menuBusy) return;
    if (menuTransferId === Number(transferId) && menuSurface === surface && menuMode === 'artifact' &&
        menuArtifactId === Number(artifactId) && menuVisible) {
      closeMenu({focusTrigger: true});
      return;
    }
    closeMenu();
    ensureMenu();
    menuTransferId = Number(transferId);
    menuSurface = surface || null;
    menuMode = 'artifact';
    menuArtifactId = Number(artifactId);
    anchorTrigger = trigger || resolveTrigger(menuTransferId, menuSurface);
    lastValidRect = anchorTrigger ? anchorTrigger.getBoundingClientRect() : null;
    menuBusy = false;
    menuVisible = true;
    const session = menuSession;
    if (anchorTrigger) anchorTrigger.setAttribute('aria-expanded', 'true');
    menuEl.hidden = false;
    menuEl.innerHTML = '<div class="dp-group-candidate-note">Loading sources…</div>';
    positionMenu();
    try {
      const transfer = await fetchTransfer(menuTransferId);
      if (menuSession !== session) return;
      const fresh = classifyFreshScope(transfer);
      if (fresh.scope !== 'artifact' || fresh.artifactId !== menuArtifactId) {
        closeMenu();
        toast('Candidate options changed.', 'info');
        await refreshSurfaces();
        return;
      }
      const file = (Array.isArray(transfer.files) ? transfer.files : [])
        .find(function (item) { return Number(item.id) === menuArtifactId; });
      if (!file) {
        closeMenu();
        toast('Candidate options changed.', 'info');
        await refreshSurfaces();
        return;
      }
      renderArtifactMenu(file);
      const first = actionableButtons()[0];
      if (first) first.focus({preventScroll: true});
      else if (menuEl) menuEl.focus({preventScroll: true});
    } catch (_) {
      if (menuSession !== session) return;
      closeMenu({focusTrigger: true});
      toast('Candidate sources are unavailable right now.', 'error');
    }
  }

  async function chooseArtifactCandidate(candidateId) {
    if (!candidateId || menuBusy || menuTransferId == null || menuArtifactId == null) return;
    const transferId = menuTransferId;
    const artifactId = menuArtifactId;
    const session = menuSession;
    menuBusy = true;
    if (menuArtifactFile) renderArtifactMenu(menuArtifactFile);  // disable rows while the switch runs

    // Action-time revalidation against fresh Details, mirroring group mode's
    // own chooseHost revalidation below: the chooser can go stale while open.
    let transfer;
    try {
      transfer = await fetchTransfer(transferId);
    } catch (_) {
      menuBusy = false;
      toast('Could not re-check the transfer. Nothing was changed.', 'error');
      await refreshSurfaces();
      if (menuSession === session) closeMenu();
      return;
    }
    const file = (Array.isArray(transfer.files) ? transfer.files : [])
      .find(function (item) { return Number(item.id) === artifactId; });
    const candidate = file && Array.isArray(file.acquisition_candidates)
      ? file.acquisition_candidates.find(function (item) { return String(item.candidate_id) === String(candidateId); })
      : null;
    if (!file || !candidate || !candidate.switch_eligible) {
      menuBusy = false;
      if (menuSession === session) { menuArtifactFile = file || null; renderArtifactMenu(file || {acquisition_candidates: []}); }
      await refreshSurfaces();
      toast('That source is no longer switchable for this file.', 'error');
      return;
    }

    try {
      const result = await switchOne(transferId, artifactId, candidateId);
      menuBusy = false;
      await refreshSurfaces();
      if (menuSession === session && menuVisible) {
        let freshFile = null;
        try {
          const freshTransfer = await fetchTransfer(transferId);
          freshFile = (Array.isArray(freshTransfer.files) ? freshTransfer.files : [])
            .find(function (item) { return Number(item.id) === artifactId; });
        } catch (_) { /* keep going */ }
        if (freshFile && isMovableFile(freshFile)) renderArtifactMenu(freshFile);
        else closeMenu();
      }
      toast(String(result.filename || file.filename || 'artifact') + ' file source switched to ' + String(result.source_host || 'source'), 'success');
    } catch (error) {
      menuBusy = false;
      await refreshSurfaces();
      if (menuSession === session) closeMenu();
      toast({
        title: 'Unable to switch source for ' + String(file.filename || 'artifact'),
        body: String((error && error.message) || 'The selected candidate could not be established.'),
      }, 'error');
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
    const session = menuSession;
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
      if (menuSession === session) closeMenu();
      return;
    }
    // Revalidation distinguishes two independent stale-truth failure modes:
    // the host may no longer be a MEMBER of the common set at all (one file
    // lost that canonical host entirely), or it may still be common but no
    // longer ACTIONABLE for remaining work (a remaining-work file's candidate
    // for it stopped being switch-eligible). Neither issues a mutation; each
    // gets its own concise report.
    const group = groupFromTransfer(transfer);
    if (group.commonHosts.indexOf(host) === -1) {
      menuBusy = false;
      if (menuSession === session) renderMenu(group);
      await refreshSurfaces();
      toast(host + ' is no longer common to the entire transfer.', 'error');
      return;
    }
    if (group.actionableHosts.indexOf(host) === -1) {
      menuBusy = false;
      if (menuSession === session) renderMenu(group);
      await refreshSurfaces();
      toast(host + ' is currently not switchable for every remaining file.', 'error');
      return;
    }
    const targets = group.targetsByHost[host];

    // Only remaining-work files ever move. A completed file keeps its
    // truthful historical/current source; an unfinished file already on the
    // target needs nothing either — both are simply absent from ``moves``.
    const moves = [];
    group.actionParticipants.forEach(function (file) {
      const selected = file.source_candidates.find(function (entry) { return entry.is_selected; });
      const currentHost = selected ? String(selected.source_host || '') : null;
      const candidateId = targets[String(file.id)];
      if (currentHost === host || !candidateId) return;
      moves.push({artifactId: file.id, candidateId: candidateId});
    });

    let switched = 0;
    let failure = null;
    if (menuSession === session) renderProgress(host, 0, moves.length);
    for (const move of moves) {
      try {
        await switchOne(transferId, move.artifactId, move.candidateId);
        switched += 1;
        if (menuSession === session) renderProgress(host, switched, moves.length);
      } catch (error) {
        failure = error;
        break;
      }
    }

    menuBusy = false;
    // Authoritative refresh: list surfaces, Details mount, and (as a side
    // effect, via onSurfaceRendered) the open chooser's own anchor
    // reattachment/permanent-loss handling for whichever surface it belongs
    // to. No optimistic ACTIVE assignment happens anywhere in this function.
    await refreshSurfaces();

    if (menuSession === session && menuVisible) {
      let fresh = null;
      try { fresh = groupFromTransfer(await fetchTransfer(transferId)); } catch (_) { /* keep going */ }
      // Re-render the still-open chooser only if remaining-work is still
      // actionable/appropriate; otherwise close it. ACTIVE always comes from
      // this fresh authoritative refetch, never fabricated optimistically.
      if (fresh && fresh.count >= 2 && fresh.actionParticipants.length) renderMenu(fresh);
      else closeMenu();
    }

    const alreadyOn = moves.length === 0;
    if (failure) {
      // Non-transactional: whatever already switched stays switched.
      toast({
        title: 'Group did not fully converge on ' + host,
        body: switched + ' of ' + moves.length + ' file' + (moves.length === 1 ? '' : 's') +
          ' switched — convergence incomplete: ' + (failure.message || 'a file could not switch') + '.',
      }, 'error');
    } else if (alreadyOn) {
      toast('Remaining files are already on ' + host + '.', 'info');
    } else {
      toast('Remaining files switched to ' + host + '.', 'success');
    }
  }

  // ── Wiring ─────────────────────────────────────────────────────────────

  function onDocumentClick(event) {
    const target = event.target instanceof Element ? event.target.closest(TRIGGER_SELECTOR) : null;
    if (!target) return;
    event.preventDefault();
    event.stopPropagation();
    const transferId = Number(target.dataset.dpTransferId);
    const surface = String(target.dataset.dpSurface || '');
    if (!Number.isFinite(transferId)) return;
    const mode = target.dataset.dpMode;
    if (mode === 'artifact') {
      const artifactId = Number(target.dataset.dpArtifactId);
      if (Number.isFinite(artifactId)) openArtifact(transferId, artifactId, surface, target);
      return;
    }
    // A legacy launcher (no data-dp-mode at all -- a caller/fixture that
    // never carried candidate_action_scope) and the pre-existing Details
    // Files-header group entry point (mode === 'group' AND surface ===
    // 'details') keep the EXACT pre-task behavior unchanged (Section 4.1's
    // explicit "existing group-mode... may remain exactly as shipped"
    // carve-out). Only a NEW-classified list-surface group launcher (mode
    // === 'group' on a non-details surface) is a genuine discovery hint
    // that can go stale and must be reconfirmed against fresh Details
    // (Section 7) before a chooser opens.
    const strict = mode === 'group' && surface !== 'details';
    open(transferId, surface, target, {strict: strict});
  }

  function install() {
    document.addEventListener('click', onDocumentClick);
    document.addEventListener('pointerdown', onDocumentPointerDown, true);
    document.addEventListener('debridpulse:detail-rendered', onDetailRendered);
    document.addEventListener('debridpulse:detail-closed', onDetailClosed);
    // Deferred to a microtask: Downloads (and, defensively, Recent) enrich
    // their raw row markup with the group launcher in a SEPARATE listener
    // for this same event (ui-downloads-presentation.js's
    // applyProviderSourcePresentation, lazily installed and so registered
    // AFTER this module's own listener). Checking synchronously within the
    // same dispatch would see the launcher before that enrichment runs and
    // misreport a confirmed permanent loss. A microtask always runs after
    // every same-dispatch listener has finished, independent of
    // registration order.
    document.addEventListener('debridpulse:dashboard-recent-rendered', function () { queueMicrotask(function () { onSurfaceRendered('dashboard_recent'); }); });
    document.addEventListener('debridpulse:downloads-rendered', function () { queueMicrotask(function () { onSurfaceRendered('downloads'); }); });
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
