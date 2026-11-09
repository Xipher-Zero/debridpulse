/* DebridPulse v1.0.12 universal file-selection presentation owner.
 *
 * Bounded owner for the file-selection modal (specification sections 41-50).
 * It renders the tri-state folder tree, owns browser-local checkbox draft
 * state, the presentation-only countdown, automatic presentation, and the
 * Details manual entry point.
 *
 * It owns NO policy. The Universal Transfer Core decides ALL-vs-subset, the
 * 60s auto-offer window, the 120s cached hold, and every deadline. A
 * collection of independent members (``explicit_only``) has no deadline and
 * no ALL: its draft starts with every displayed entry checked, but only
 * Confirm commits anything; the backdrop and Escape do nothing, and X, Close
 * and Cancel Transfer all cancel the transfer. This module
 * only reads authoritative state from the dedicated file-selection API, POSTs
 * manifest_id + entry_ids on Confirm, and POSTs manifest_id on dismiss. It
 * never authorizes ALL locally and never wraps or reassigns the shared modal
 * coordinator globals — app.js owns the shared modal shell and emits the
 * lifecycle events this module listens to.
 */
(function () {
  'use strict';
  const t = (key, values) => window.DPI18n.t(key, values);

  const OFFERS_URL = '/file-selections/offers';
  const PENDING_MODE = 'file-selection-pending';

  // ── Ambient helpers from app.js (single owners) ──────────────────────────
  function api() { return window.api.apply(null, arguments); }
  function toast(message, kind) {
    if (typeof window.toast === 'function') window.toast(message, kind || 'info');
  }
  function esc(value) {
    return typeof window.esc === 'function'
      ? window.esc(value)
      : String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
        return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
      });
  }
  function fmtSize(bytes) {
    return typeof window.fmtSize === 'function' ? window.fmtSize(bytes) : String(bytes || 0) + ' B';
  }
  function modal() { return window.DPModal; }

  // ── Session state ───────────────────────────────────────────────────────
  const dismissedManifests = new Set();   // manifest_ids the user closed this session
  let selectorTransferId = null;          // transfer shown in the selector modal
  let selectorManifestId = null;
  let selectorTransferName = '';
  let selectorExplicitOnly = false;       // independent collection members: explicit choice only
  const draft = new Set();                // browser-local selected entry_ids
  let manifestEntries = [];               // [{entry_id, name, relative_path, size_bytes}]
  let countdownTimer = null;
  let countdownDeadlineMs = null;         // wall-clock ms when the hold expires
  let confirmInFlight = false;
  let detailTransferId = null;            // transfer currently shown in Details
  let detailEntryShown = null;            // {transferId, view}: the Details entry's last answer, said again on relabel

  // ── Path / tree helpers ─────────────────────────────────────────────────
  function normalizePath(value) {
    return String(value || '').replace(/\\+/g, '/').replace(/^\/+/, '');
  }

  function buildTree(entries) {
    const root = {name: '', path: '', dirs: new Map(), files: []};
    entries.forEach(function (entry) {
      const parts = normalizePath(entry.relative_path).split('/').filter(Boolean);
      const leaf = parts.length ? parts[parts.length - 1] : (entry.name || 'file');
      let node = root;
      for (let i = 0; i < parts.length - 1; i += 1) {
        const segment = parts[i];
        if (!node.dirs.has(segment)) {
          node.dirs.set(segment, {
            name: segment,
            path: node.path ? node.path + '/' + segment : segment,
            dirs: new Map(), files: [],
          });
        }
        node = node.dirs.get(segment);
      }
      node.files.push({entry: entry, label: leaf});
    });
    return root;
  }

  function folderEntryIds(node, out) {
    node.files.forEach(function (file) { out.push(String(file.entry.entry_id)); });
    node.dirs.forEach(function (child) { folderEntryIds(child, out); });
    return out;
  }

  function renderNode(node, depth) {
    const pad = 'style="--dp-fs-depth:' + depth + '"';
    let html = '';
    Array.from(node.dirs.values())
      .sort(function (a, b) { return a.name.localeCompare(b.name); })
      .forEach(function (dir) {
        const folderId = 'dp-fs-folder-' + esc(dir.path).replace(/[^a-zA-Z0-9_-]/g, '-');
        html += '<div class="dp-fs-row dp-fs-row--folder" ' + pad + ' role="treeitem">' +
          '<button type="button" class="dp-fs-caret" aria-expanded="true" ' +
          'aria-controls="' + folderId + '" aria-label="Collapse ' + esc(dir.name) + '">' +
          '<span class="dp-fs-caret-glyph" aria-hidden="true"></span></button>' +
          '<label class="dp-fs-check-label">' +
          '<input type="checkbox" class="dp-fs-check dp-fs-check--folder" ' +
          'data-folder-path="' + esc(dir.path) + '">' +
          '<span class="dp-fs-name dp-fs-name--folder">' + esc(dir.name) + '</span></label>' +
          '</div>';
        html += '<div class="dp-fs-children" id="' + folderId + '">' +
          renderNode(dir, depth + 1) + '</div>';
      });
    node.files
      .slice()
      .sort(function (a, b) { return a.label.localeCompare(b.label); })
      .forEach(function (file) {
        const entry = file.entry;
        const size = Number(entry.size_bytes || 0);
        html += '<div class="dp-fs-row dp-fs-row--file" ' + pad + ' role="treeitem">' +
          '<span class="dp-fs-caret dp-fs-caret--spacer" aria-hidden="true"></span>' +
          '<label class="dp-fs-check-label">' +
          '<input type="checkbox" class="dp-fs-check dp-fs-check--file" ' +
          'data-entry-id="' + esc(entry.entry_id) + '">' +
          '<span class="dp-fs-name">' + esc(file.label) + '</span></label>' +
          '<span class="dp-fs-size">' + (size > 0 ? esc(fmtSize(size)) : '—') + '</span>' +
          '</div>';
      });
    return html;
  }

  // ── Selector rendering ──────────────────────────────────────────────────
  function bodyEl() { return document.getElementById('modal-body'); }

  // A collection the source holds more of than its bounded snapshot: say so,
  // with the source's total only where the source stated one.
  function truncationNotice(view) {
    if (!view.source_truncated) return '';
    const shown = Array.isArray(view.entries) ? view.entries.length : 0;
    const known = Number.isInteger(view.source_total) && view.source_total > shown;
    const key = known ? 'file_selection.truncated_known' : 'file_selection.truncated_unknown';
    const values = known ? {shown, total: view.source_total} : {shown};
    return '<p class="dp-fs-notice" role="note" data-i18n="' + key + '" data-i18n-values=\'' + esc(JSON.stringify(values)) + '\'>'
      + esc(t(key, values)) + '</p>';
  }

  function renderSelector(view) {
    manifestEntries = Array.isArray(view.entries) ? view.entries.slice() : [];
    const tree = buildTree(manifestEntries);
    const body = bodyEl();
    if (!body) return;
    body.innerHTML =
      '<div class="dp-fs">' +
      '<div class="dp-fs-toolbar">' +
      '<span class="dp-fs-subtitle" data-dp-fs-subtitle>' +
      (selectorTransferName ? esc(selectorTransferName) : '') + '</span>' +
      '<button type="button" class="btn btn-ghost btn-sm dp-fs-toggle-all"></button>' +
      '</div>' +
      truncationNotice(view) +
      '<div class="dp-fs-tree" role="tree" data-i18n-attr="aria-label:' + treeKey() + '" aria-label="' + esc(t(treeKey())) + '">' +
      renderNode(tree, 0) + '</div>' +
      '<div class="dp-fs-summary">' +
      '<span class="dp-fs-count"></span>' +
      '<span class="dp-fs-bytes"></span>' +
      '</div>' +
      '</div>';

    bindSelectorEvents();
    syncCheckboxesFromDraft();
    updateFolderStates();
    updateSummary();
  }

  function treeKey() { return selectorExplicitOnly ? 'file_selection.tree_entries' : 'file_selection.tree_files'; }
  // Keyed footer markup: a language change re-words it in place (ui-i18n.js
  // render); the countdown sentence holds the live clock and is said here.
  const keyed = (tag, cls, key) => '<' + tag + ' class="' + cls + '" data-i18n="' + key + '">' + esc(t(key)) + '</' + tag + '>';
  const keyedButton = (cls, key) => '<button type="button" class="' + cls + '" data-i18n="' + key + '">' + esc(t(key)) + '</button>';
  function paintCountdown(paragraph) {
    if (!paragraph) return;
    const clock = paragraph.querySelector('.dp-fs-clock') || Object.assign(document.createElement('span'), {className: 'dp-fs-clock', textContent: '—'});
    paragraph.replaceChildren(window.DPI18n.rich('file_selection.countdown', {clock}));
  }

  function renderFooter(view) {
    const footer = modal().footer();
    if (!footer) return;
    const hasHold = view.decision_deadline != null;
    footer.innerHTML =
      '<div class="dp-fs-footer' + (selectorExplicitOnly ? ' dp-fs-footer--collection' : '') + '">' +
      keyed('p', 'dp-fs-foot-note', selectorExplicitOnly ? 'file_selection.note_entries' : 'file_selection.note_files') +
      '<p class="dp-fs-foot-countdown"' + (hasHold ? '' : ' hidden') + '></p>' +
      '<div class="dp-fs-actions">' +
      keyedButton('btn btn-danger dp-fs-cancel', 'submission.scope.cancel') +
      '<span class="dp-fs-actions-spacer"></span>' +
      keyedButton('btn btn-ghost dp-fs-close', 'dialog.close') +
      keyedButton('btn btn-primary dp-fs-confirm', 'dialog.confirm') +
      '</div>' +
      '</div>';
    footer.hidden = false;
    paintCountdown(footer.querySelector('.dp-fs-foot-countdown'));

    footer.querySelector('.dp-fs-cancel').addEventListener('click', onCancelTransfer);
    footer.querySelector('.dp-fs-close').addEventListener('click', function () {
      modal().requestModalClose('close');
    });
    footer.querySelector('.dp-fs-confirm').addEventListener('click', onConfirm);
  }

  function bindSelectorEvents() {
    const body = bodyEl();
    if (!body) return;
    body.querySelector('.dp-fs-toggle-all').addEventListener('click', onToggleAll);
    body.querySelectorAll('.dp-fs-caret:not(.dp-fs-caret--spacer)').forEach(function (caret) {
      caret.addEventListener('click', function () {
        const panel = document.getElementById(caret.getAttribute('aria-controls'));
        const open = caret.getAttribute('aria-expanded') === 'true';
        caret.setAttribute('aria-expanded', open ? 'false' : 'true');
        if (panel) panel.hidden = open;
      });
    });
    body.querySelectorAll('.dp-fs-check--file').forEach(function (checkbox) {
      checkbox.addEventListener('change', function () {
        const id = String(checkbox.dataset.entryId);
        if (checkbox.checked) draft.add(id); else draft.delete(id);
        updateFolderStates();
        updateSummary();
      });
    });
    body.querySelectorAll('.dp-fs-check--folder').forEach(function (checkbox) {
      checkbox.addEventListener('change', function () {
        const path = normalizePath(checkbox.dataset.folderPath);
        const ids = descendantIdsForFolder(path);
        ids.forEach(function (id) {
          if (checkbox.checked) draft.add(id); else draft.delete(id);
        });
        syncCheckboxesFromDraft();
        updateFolderStates();
        updateSummary();
      });
    });
  }

  function descendantIdsForFolder(path) {
    const prefix = path ? path + '/' : '';
    return manifestEntries
      .filter(function (entry) {
        const p = normalizePath(entry.relative_path);
        return prefix === '' ? true : p.indexOf(prefix) === 0;
      })
      .map(function (entry) { return String(entry.entry_id); });
  }

  function syncCheckboxesFromDraft() {
    const body = bodyEl();
    if (!body) return;
    body.querySelectorAll('.dp-fs-check--file').forEach(function (checkbox) {
      checkbox.checked = draft.has(String(checkbox.dataset.entryId));
    });
  }

  function updateFolderStates() {
    const body = bodyEl();
    if (!body) return;
    body.querySelectorAll('.dp-fs-check--folder').forEach(function (checkbox) {
      const ids = descendantIdsForFolder(normalizePath(checkbox.dataset.folderPath));
      const selected = ids.filter(function (id) { return draft.has(id); }).length;
      checkbox.checked = ids.length > 0 && selected === ids.length;
      checkbox.indeterminate = selected > 0 && selected < ids.length;
    });
  }

  function updateSummary() {
    const body = bodyEl();
    if (!body) return;
    const total = manifestEntries.length;
    const selected = manifestEntries.filter(function (entry) {
      return draft.has(String(entry.entry_id));
    });
    const bytes = selected.reduce(function (sum, entry) {
      return sum + Math.max(0, Number(entry.size_bytes || 0));
    }, 0);
    const countNode = body.querySelector('.dp-fs-count');
    const bytesNode = body.querySelector('.dp-fs-bytes');
    if (countNode) {
      countNode.textContent = t(selectorExplicitOnly ? 'file_selection.count_entries' : 'file_selection.count_files', {selected: selected.length, total});
    }
    if (bytesNode) bytesNode.textContent = bytes > 0 ? fmtSize(bytes) : '';
    const toggle = body.querySelector('.dp-fs-toggle-all');
    if (toggle) {
      const selectable = selectableEntryIds();
      toggle.textContent = t(allSelected(selectable) ? 'file_selection.deselect_all' : 'file_selection.select_all');
      toggle.disabled = selectable.length === 0;
    }
    const footer = modal().footer();
    const confirmBtn = footer ? footer.querySelector('.dp-fs-confirm') : null;
    if (confirmBtn) {
      confirmBtn.disabled = selected.length === 0 || confirmInFlight;
      confirmBtn.setAttribute('aria-disabled', confirmBtn.disabled ? 'true' : 'false');
    }
  }

  // The entries the operator can select: every displayed file row whose
  // checkbox is enabled. A disabled row is never counted or changed.
  function selectableEntryIds() {
    const body = bodyEl();
    if (!body) return [];
    return Array.from(body.querySelectorAll('.dp-fs-check--file:not(:disabled)'), function (checkbox) {
      return String(checkbox.dataset.entryId);
    });
  }

  function allSelected(ids) {
    return ids.length > 0 && ids.every(function (id) { return draft.has(id); });
  }

  // The one Select All / Deselect All for both selectors: every selectable
  // entry checked unchecks them all; otherwise it checks them all.
  function onToggleAll() {
    const selectable = selectableEntryIds();
    const deselecting = allSelected(selectable);
    selectable.forEach(function (id) {
      if (deselecting) draft.delete(id); else draft.add(id);
    });
    syncCheckboxesFromDraft();
    updateFolderStates();
    updateSummary();
  }

  // ── Countdown (presentation only) ───────────────────────────────────────
  function startCountdown(view) {
    stopCountdown();
    if (view.decision_deadline == null) return;
    const serverNow = Number(view.server_now || 0);
    const deadline = Number(view.decision_deadline);
    const remainingSeconds = Math.max(0, deadline - serverNow);
    countdownDeadlineMs = Date.now() + remainingSeconds * 1000;
    renderCountdown();
    countdownTimer = window.setInterval(renderCountdown, 1000);
  }

  function stopCountdown() {
    if (countdownTimer != null) window.clearInterval(countdownTimer);
    countdownTimer = null;
    countdownDeadlineMs = null;
  }

  function renderCountdown() {
    const footer = modal().footer();
    const clock = footer ? footer.querySelector('.dp-fs-clock') : null;
    if (countdownDeadlineMs == null || !clock) return;
    const remaining = Math.max(0, Math.round((countdownDeadlineMs - Date.now()) / 1000));
    const minutes = Math.floor(remaining / 60);
    const seconds = remaining % 60;
    clock.textContent = minutes + ':' + (seconds < 10 ? '0' : '') + seconds;
    if (remaining <= 0) {
      stopCountdown();
      // The browser timer never authorizes ALL — re-check authoritative state.
      refreshAuthoritative();
    }
  }

  // ── Authoritative state ─────────────────────────────────────────────────
  function fetchSelection(transferId) {
    return api('GET', '/torrents/' + transferId + '/file-selection');
  }

  function isAutoPresentable(view) {
    return Boolean(view && view.eligible && view.mutable && view.auto_offer &&
      Number(view.file_count || 0) > (view.explicit_only ? 0 : 1));
  }

  function affordanceLabel(affordance) {
    return t(affordance === 'change' ? 'file_selection.change' : 'file_selection.choose');
  }

  function initialDraftFromView(view) {
    draft.clear();
    const persisted = Array.isArray(view.selected_entry_ids) ? view.selected_entry_ids : [];
    if (persisted.length) {
      persisted.forEach(function (id) { draft.add(String(id)); });
    } else {
      // Every displayed entry starts checked. For a collection this is only
      // the draft: nothing is committed until Confirm.
      (view.entries || []).forEach(function (entry) { draft.add(String(entry.entry_id)); });
    }
  }

  function openSelector(transferId, view, options) {
    const opts = options || {};
    if (!view || !view.eligible || !view.mutable) return;
    // Block only when a different transfer's selector is already active. Opening
    // over Details (mode 'details') for the manual entry point is expected; the
    // auto-offer path guards against stomping any open modal before it calls in.
    if (selectorTransferId != null && selectorTransferId !== transferId) return;
    if (modal().mode === 'details') {
      document.dispatchEvent(new CustomEvent('debridpulse:detail-closed', {detail: {reason: 'file-selection'}}));
    }

    selectorTransferId = transferId;
    selectorManifestId = view.manifest_id;
    selectorTransferName = opts.transferName || selectorTransferName || '';
    selectorExplicitOnly = Boolean(view.explicit_only);
    initialDraftFromView(view);

    modal().open({
      mode: 'file-selection',
      title: t(selectorExplicitOnly ? 'file_selection.title_entries' : 'file_selection.title_files'),
      closeLabel: t(selectorExplicitOnly ? 'file_selection.close_entries' : 'file_selection.close_files'),
      contain: selectorExplicitOnly,
      onClose: handleModalClose,
    });
    renderSelector(view);
    renderFooter(view);
    updateSummary();
    startCountdown(view);

    if (!selectorTransferName) {
      api('GET', '/torrents/' + transferId).then(function (transfer) {
        selectorTransferName = String(transfer && transfer.name || '');
        const node = document.querySelector('[data-dp-fs-subtitle]');
        if (node && selectorTransferId === transferId) node.textContent = selectorTransferName;
      }).catch(function () {});
    }
  }

  function refreshAuthoritative() {
    const transferId = selectorTransferId;
    if (transferId == null) return Promise.resolve();
    return fetchSelection(transferId).then(function (view) {
      if (selectorTransferId !== transferId) return;
      if (!view || !view.eligible || !view.mutable || view.decision !== 'pending') {
        modal().finishClose('expired');
        resetSelectorState();
        toast(t('file_selection.expired'), 'info');
        refreshTransferViews();
        return;
      }
      if (view.manifest_id !== selectorManifestId) {
        // The bound provider resource changed — this generation is stale.
        modal().finishClose('superseded');
        resetSelectorState();
        toast(t('file_selection.superseded'), 'warn');
        refreshTransferViews();
        return;
      }
      renderFooter(view);
      startCountdown(view);
      updateSummary();
    }).catch(function () {});
  }

  // ── Confirm / Cancel / Dismiss ──────────────────────────────────────────
  function onConfirm() {
    if (confirmInFlight || selectorTransferId == null) return;
    const entryIds = Array.from(draft);
    if (!entryIds.length) return;
    confirmInFlight = true;
    updateSummary();
    api('POST', '/torrents/' + selectorTransferId + '/file-selection/confirm', {
      manifest_id: selectorManifestId,
      entry_ids: entryIds,
    }).then(function () {
      confirmInFlight = false;
      const count = entryIds.length;
      stopCountdown();
      const explicit = selectorExplicitOnly;
      modal().finishClose('confirmed');
      resetSelectorState();
      toast(t(explicit ? 'file_selection.confirmed_entries' : 'file_selection.confirmed_files', {count}), 'success');
      refreshTransferViews();
    }).catch(function (error) {
      confirmInFlight = false;
      const message = String(error && error.message || '');
      if (/already|committed|conflict|superseded|stale|no longer/i.test(message)) {
        toast(t('file_selection.conflict'), 'warn');
        refreshAuthoritative();
      } else {
        toast(message || t('file_selection.failed'), 'error');
      }
      updateSummary();
    });
  }

  function onCancelTransfer() {
    if (selectorTransferId == null) return;
    const transferId = selectorTransferId;
    stopCountdown();
    modal().finishClose('cancel-transfer');
    resetSelectorState();
    cancelTransfer(transferId);
  }

  function cancelTransfer(transferId) {
    api('POST', '/torrents/' + transferId + '/cancel').then(function () {
      toast(t('file_selection.transfer_cancelled'), 'success');
      refreshTransferViews();
    }).catch(function (error) {
      toast(String(error && error.message || t('file_selection.cancel_failed')), 'error');
      refreshTransferViews();
    });
  }

  function handleModalClose(reason) {
    // A collection of independent members is blocking: the backdrop does
    // nothing, and X / Close cancel the transfer exactly as Cancel Transfer
    // does -- nothing is committed and nothing is left pending.
    if (selectorExplicitOnly) {
      if (reason === 'backdrop') return false;
      const cancelled = selectorTransferId;
      resetSelectorState();
      if (cancelled != null) cancelTransfer(cancelled);
      return undefined;
    }
    // Close / X: no subset is committed, default ALL stays authoritative, and
    // an active cached hold is released server-side. Draft is discarded.
    const transferId = selectorTransferId;
    const manifestId = selectorManifestId;
    stopCountdown();
    if (manifestId) dismissedManifests.add(String(manifestId));
    if (transferId != null && manifestId) {
      api('POST', '/torrents/' + transferId + '/file-selection/dismiss', {manifest_id: manifestId})
        .then(function () { refreshTransferViews(); })
        .catch(function () {});
    }
    resetSelectorState();
    // undefined return → coordinator proceeds with the close
  }

  function resetSelectorState() {
    selectorTransferId = null;
    selectorManifestId = null;
    selectorTransferName = '';
    selectorExplicitOnly = false;
    manifestEntries = [];
    draft.clear();
    confirmInFlight = false;
    stopCountdown();
  }

  function refreshTransferViews() {
    if (typeof window.loadTorrents === 'function') window.loadTorrents().catch(function () {});
    if (typeof window.loadRecent === 'function') window.loadRecent().catch(function () {});
    if (typeof window.loadStats === 'function') window.loadStats().catch(function () {});
    if (detailTransferId != null) renderDetailEntry(detailTransferId);
  }

  // Choosing files is offered only to a caller who may destroy the transfer's
  // work: closing the selector cancels the whole transfer. An Admin may; a
  // User only where the server's ``access.destroy`` says no one else needs it.
  function mayChoose(transferId) {
    const auth = window.debridPulseAuth;
    if (!auth || !auth.can('transfers.operate')) return Promise.resolve(false);
    if (auth.can('administer')) return Promise.resolve(true);
    return api('GET', '/torrents/' + Number(transferId))
      .then(function (detail) { return Boolean(detail && auth.mayDestroy(detail)); })
      .catch(function () { return false; });
  }

  // ── Automatic presentation ──────────────────────────────────────────────
  function handleOffer(transferId) {
    mayChoose(transferId).then(function (allowed) { if (allowed) presentOffer(transferId); });
  }

  function presentOffer(transferId) {
    const id = Number(transferId);
    if (!Number.isFinite(id)) return;
    if (selectorTransferId != null) return;         // a selector is already open
    if (modal().mode) return;                       // another modal owns the shell
    fetchSelection(id).then(function (view) {
      if (!isAutoPresentable(view)) return;
      if (view.manifest_id && dismissedManifests.has(String(view.manifest_id))) return;
      openSelector(id, view, {auto: true});
    }).catch(function () {});
  }

  function pollOffers() {
    if (selectorTransferId != null || modal().mode) return;
    api('GET', OFFERS_URL).then(function (payload) {
      const offers = payload && Array.isArray(payload.offers) ? payload.offers : [];
      const next = offers.find(function (offer) {
        return offer && offer.manifest_id && !dismissedManifests.has(String(offer.manifest_id));
      });
      if (next) handleOffer(next.transfer_id);
    }).catch(function () {});
  }

  // ── Manifest-not-ready informational overlay (§6.5) ─────────────────────
  function openPendingManifestModal() {
    if (selectorTransferId != null) return;   // a real selector is already open
    if (modal().mode && modal().mode !== 'details') return;  // another modal owns the shell
    if (modal().mode === 'details') {
      document.dispatchEvent(new CustomEvent('debridpulse:detail-closed', {detail: {reason: 'file-selection'}}));
    }
    modal().open({
      mode: PENDING_MODE,
      title: t('file_selection.pending_title'),
      closeLabel: t('dialog.close'),
    });
    const body = bodyEl();
    if (body) {
      body.innerHTML = '<div class="dp-fs-pending-manifest">' + keyed('p', 'dp-fs-pending-copy', 'file_selection.pending_message') + '</div>';
    }
  }

  // ── Fresh click authority (§6.8) — shared by Details and both list chips.
  // The bounded list's file_selection_affordance is a HINT only; every click
  // re-fetches authoritative state before deciding what to open. ──────────
  // The one canonical backend affordance (transfers.file_selection
  // .file_selection_affordance) is never reclassified client-side, and never
  // gated on a second field here -- the ineligible/no-generation case is
  // itself classified by that same backend function and always arrives as
  // an explicit 'none', so this only ever consumes the field.
  function affordanceOf(view) {
    return (view && view.file_selection_affordance) || 'none';
  }

  function handleAffordanceClick(transferId, options) {
    const opts = options || {};
    fetchSelection(transferId).then(function (view) {
      const affordance = affordanceOf(view);
      if (affordance === 'pending_manifest') {
        openPendingManifestModal();
        return;
      }
      if (affordance === 'choose' || affordance === 'change') {
        openSelector(transferId, view, {auto: false, transferName: opts.transferName});
        return;
      }
      // No longer applicable/mutable: never open a stale picker (§6.8.4).
      toast(t('file_selection.no_longer'), 'info');
      if (detailTransferId === transferId) renderDetailEntry(transferId);
      refreshTransferViews();
    }).catch(function (error) {
      toast(String(error && error.message || t('file_selection.unavailable')), 'error');
    });
  }

  // ── Compact glyph-only list chip (Dashboard Recent / Downloads, §6.3) ────
  function chipMarkup(item) {
    const affordance = item && item.file_selection_affordance;
    if (!affordance || affordance === 'none' || !window.debridPulseAuth?.mayDestroy(item)) return '';
    const label = affordanceLabel(affordance);
    const icon = window.DPIcons && typeof window.DPIcons.svg === 'function'
      ? window.DPIcons.svg('folder') : '';
    return '<button type="button" class="dp-file-selection-chip" data-dp-file-selection-chip ' +
      'data-transfer-id="' + esc(String(item.id)) + '" data-tooltip="' + esc(label) + '" ' +
      'aria-label="' + esc(label) + '">' + icon + '</button>';
  }

  // ── Details manual entry point (Files-section header, right side) ───────
  // ``relabel``: Details re-said the same transfer in another language; the
  // entry is drawn again from its last answer, without asking again.
  function renderDetailEntry(transferId, relabel) {
    const host = document.querySelector(
      '[data-dp-file-selection-mount][data-dp-transfer-id="' + transferId + '"]');
    if (!host) return;
    if (relabel && detailEntryShown && detailEntryShown.transferId === transferId) {
      paintDetailEntry(host, transferId, detailEntryShown.view);
      return;
    }
    mayChoose(transferId).then(function (allowed) {
      return allowed ? fetchSelection(transferId) : null;
    }).then(function (view) {
      const currentHost = document.querySelector(
        '[data-dp-file-selection-mount][data-dp-transfer-id="' + transferId + '"]');
      if (currentHost !== host) return;
      detailEntryShown = {transferId: transferId, view: view};
      paintDetailEntry(host, transferId, view);
    }).catch(function () { host.innerHTML = ''; });
  }

  function paintDetailEntry(host, transferId, view) {
    if (!view || !view.eligible) { host.innerHTML = ''; return; }
    const affordance = affordanceOf(view);
    if (affordance === 'choose' || affordance === 'change' || affordance === 'pending_manifest') {
      const key = affordance === 'change' ? 'file_selection.change' : 'file_selection.choose';
      host.innerHTML = '<button type="button" class="btn btn-ghost btn-sm dp-file-selection-entry" ' +
        'data-i18n="' + key + '" data-i18n-attr="data-tooltip:' + key + '" data-tooltip="' + esc(t(key)) + '">' + esc(t(key)) + '</button>';
      host.querySelector('.dp-file-selection-entry').addEventListener('click', function () {
        handleAffordanceClick(transferId);
      });
      return;
    }
    // Locked/not-applicable: a truthful passive summary only when an
    // explicit subset was actually recorded (§6.9) -- otherwise no dead
    // button, nothing rendered.
    const selectedCount = Array.isArray(view.selected_entry_ids)
      ? view.selected_entry_ids.length : 0;
    if (view.decision === 'explicit' && selectedCount > 0) {
      const values = {selected: selectedCount, total: Number(view.file_count || selectedCount)};
      host.innerHTML = '<span class="dp-file-selection-summary" data-i18n="file_selection.count_files" data-i18n-values=\'' +
        esc(JSON.stringify(values)) + '\'>' + esc(t('file_selection.count_files', values)) + '</span>';
    } else {
      host.innerHTML = '';
    }
  }

  // A language change: the open chooser's keyed words are re-rendered in place
  // (ui-i18n.js); here its title, close name, count, Select All and countdown
  // sentence -- the draft, focus and countdown are untouched.
  document.addEventListener('debridpulse:locale-changed', function () {
    const mode = modal().mode;
    const title = document.getElementById('modal-title');
    const close = document.querySelector('#modal .modal-close');
    if (selectorTransferId != null && mode === 'file-selection') {
      if (title) title.textContent = t(selectorExplicitOnly ? 'file_selection.title_entries' : 'file_selection.title_files');
      const label = t(selectorExplicitOnly ? 'file_selection.close_entries' : 'file_selection.close_files');
      if (close) { close.setAttribute('aria-label', label); close.dataset.tooltip = label; }
      updateSummary();
      const footer = modal().footer();
      paintCountdown(footer ? footer.querySelector('.dp-fs-foot-countdown') : null);
    } else if (mode === PENDING_MODE) {
      if (title) title.textContent = t('file_selection.pending_title');
      if (close) { close.setAttribute('aria-label', t('dialog.close')); close.dataset.tooltip = t('dialog.close'); }
    }
  });

  // ── Wiring ──────────────────────────────────────────────────────────────
  function init() {
    // Choosing files is an operating control: a read-only caller has none.
    if (!(window.debridPulseAuth && window.debridPulseAuth.can('transfers.operate'))) return;
    document.addEventListener('debridpulse:file-selection-available', function (event) {
      const payload = event && event.detail || {};
      handleOffer(payload.transfer_id);
    });
    document.addEventListener('debridpulse:pulse-connected', pollOffers);
    document.addEventListener('debridpulse:detail-rendered', function (event) {
      const payload = event && event.detail || {};
      const transferId = Number(payload.transferId);
      if (!Number.isFinite(transferId)) { detailTransferId = null; return; }
      detailTransferId = transferId;
      renderDetailEntry(transferId, Boolean(payload.relabel));
    });
    document.addEventListener('debridpulse:detail-closed', function () {
      detailTransferId = null;
      detailEntryShown = null;
    });
    // Semantic invalidation (§7/§12 -- stale Choose File must clear itself):
    // the backend crossed the durable manifest-commit boundary and the
    // existing torrent_updated semantic transport delivered it. Re-read
    // authoritative state for the open Details entry -- never synthesize
    // mutability client-side, never just remove the DOM node.
    document.addEventListener('debridpulse:transfer-updated', function (event) {
      const payload = event && event.detail || {};
      const transferId = Number(payload.transferId);
      if (Number.isFinite(transferId) && detailTransferId === transferId) {
        renderDetailEntry(transferId);
      }
    });
    // Dashboard Recent / Downloads glyph-only chip (§6.3, §6.7-6.8): one
    // delegated listener, no per-row rebinding on every render pass. The
    // chip is a real <button>, so the row's own dpIsInteractiveRowTarget
    // guard already prevents this click from also opening Details (§6.10
    // Case H) -- no stopPropagation() needed here.
    document.addEventListener('click', function (event) {
      const chip = event.target && event.target.closest
        ? event.target.closest('[data-dp-file-selection-chip]') : null;
      if (!chip) return;
      const transferId = Number(chip.dataset.transferId);
      if (!Number.isFinite(transferId)) return;
      handleAffordanceClick(transferId);
    });
    // Cold load (§40): recover an offer created before this tab connected.
    pollOffers();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, {once: true});
  } else {
    init();
  }

  window.DPFileSelection = Object.freeze({
    handleOffer,
    pollOffers,
    renderDetailEntry,
    openSelector,
    chipMarkup,
    handleAffordanceClick,
  });
})();
