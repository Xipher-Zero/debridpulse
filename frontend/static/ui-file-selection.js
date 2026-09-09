/* DebridPulse v1.0.12 universal file-selection presentation owner.
 *
 * Bounded owner for the file-selection modal (specification sections 41-50).
 * It renders the tri-state folder tree, owns browser-local checkbox draft
 * state, the presentation-only countdown, automatic presentation, and the
 * Details manual entry point.
 *
 * It owns NO policy. The Universal Transfer Core decides ALL-vs-subset, the
 * 60s auto-offer window, the 120s cached hold, and every deadline. This module
 * only reads authoritative state from the dedicated file-selection API, POSTs
 * manifest_id + entry_ids on Confirm, and POSTs manifest_id on dismiss. It
 * never authorizes ALL locally and never wraps or reassigns the shared modal
 * coordinator globals — app.js owns the shared modal shell and emits the
 * lifecycle events this module listens to.
 */
(function () {
  'use strict';

  const OFFERS_URL = '/file-selections/offers';

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
  const draft = new Set();                // browser-local selected entry_ids
  let manifestEntries = [];               // [{entry_id, name, relative_path, size_bytes}]
  let countdownTimer = null;
  let countdownDeadlineMs = null;         // wall-clock ms when the hold expires
  let confirmInFlight = false;
  let detailTransferId = null;            // transfer currently shown in Details

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
      '<div class="dp-fs-tree" role="tree" aria-label="Files in this transfer">' +
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

  function renderFooter(view) {
    const footer = modal().footer();
    if (!footer) return;
    const hasHold = view.decision_deadline != null;
    footer.innerHTML =
      '<div class="dp-fs-footer">' +
      '<p class="dp-fs-foot-note">Confirm downloads only the selected files. ' +
      'Cancel stops the entire transfer.</p>' +
      '<p class="dp-fs-foot-countdown"' + (hasHold ? '' : ' hidden') + '>' +
      'All files will download automatically in <span class="dp-fs-clock">—</span> ' +
      'if no selection is confirmed.</p>' +
      '<div class="dp-fs-actions">' +
      '<button type="button" class="btn btn-danger dp-fs-cancel">Cancel Transfer</button>' +
      '<span class="dp-fs-actions-spacer"></span>' +
      '<button type="button" class="btn btn-ghost dp-fs-close">Close</button>' +
      '<button type="button" class="btn btn-primary dp-fs-confirm">Confirm</button>' +
      '</div>' +
      '</div>';
    footer.hidden = false;

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
    if (countNode) countNode.textContent = selected.length + ' of ' + total + ' files selected';
    if (bytesNode) bytesNode.textContent = bytes > 0 ? fmtSize(bytes) : '';
    const toggle = body.querySelector('.dp-fs-toggle-all');
    if (toggle) {
      toggle.textContent = selected.length === total && total > 0 ? 'Deselect all' : 'Select all';
    }
    const footer = modal().footer();
    const confirmBtn = footer ? footer.querySelector('.dp-fs-confirm') : null;
    if (confirmBtn) {
      confirmBtn.disabled = selected.length === 0 || confirmInFlight;
      confirmBtn.setAttribute('aria-disabled', confirmBtn.disabled ? 'true' : 'false');
    }
  }

  function onToggleAll() {
    const selectingAll = manifestEntries.some(function (entry) {
      return !draft.has(String(entry.entry_id));
    });
    draft.clear();
    if (selectingAll) {
      manifestEntries.forEach(function (entry) { draft.add(String(entry.entry_id)); });
    }
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
      Number(view.file_count || 0) > 1);
  }

  function initialDraftFromView(view) {
    draft.clear();
    const persisted = Array.isArray(view.selected_entry_ids) ? view.selected_entry_ids : [];
    if (persisted.length) {
      persisted.forEach(function (id) { draft.add(String(id)); });
    } else {
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
    initialDraftFromView(view);

    modal().open({
      mode: 'file-selection',
      title: 'Select files',
      closeLabel: 'Close file selection',
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
        toast('The file-selection window closed. All files will download.', 'info');
        refreshTransferViews();
        return;
      }
      if (view.manifest_id !== selectorManifestId) {
        // The bound provider resource changed — this generation is stale.
        modal().finishClose('superseded');
        resetSelectorState();
        toast('The file list changed. Reopen file selection from Details to choose again.', 'warn');
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
      modal().finishClose('confirmed');
      resetSelectorState();
      toast(count === 1 ? '1 file selected for download' : count + ' files selected for download',
        'success');
      refreshTransferViews();
    }).catch(function (error) {
      confirmInFlight = false;
      const message = String(error && error.message || '');
      if (/already|committed|conflict|superseded|stale|no longer/i.test(message)) {
        toast('Selection changed or the download already started. Refreshing…', 'warn');
        refreshAuthoritative();
      } else {
        toast(message || 'That file selection could not be applied.', 'error');
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
    api('POST', '/torrents/' + transferId + '/cancel').then(function () {
      toast('Transfer cancelled', 'success');
      refreshTransferViews();
    }).catch(function (error) {
      toast(String(error && error.message || 'The transfer could not be cancelled.'), 'error');
      refreshTransferViews();
    });
  }

  function handleModalClose() {
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

  // ── Automatic presentation ──────────────────────────────────────────────
  function handleOffer(transferId) {
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

  // ── Details manual entry point ──────────────────────────────────────────
  function renderDetailEntry(transferId) {
    const host = document.getElementById('dp-detail-actions');
    if (!host) return;
    fetchSelection(transferId).then(function (view) {
      if (document.getElementById('dp-detail-actions') !== host) return;
      if (!view || !view.eligible) { host.innerHTML = ''; return; }
      const selectedCount = Array.isArray(view.selected_entry_ids)
        ? view.selected_entry_ids.length : 0;
      if (view.mutable) {
        const explicit = view.decision === 'explicit' || selectedCount > 0;
        const label = explicit ? 'Change file selection' : 'Select files';
        host.innerHTML = '<button type="button" class="btn btn-ghost btn-sm dp-file-selection-entry">' +
          esc(label) + '</button>';
        host.querySelector('.dp-file-selection-entry').addEventListener('click', function () {
          fetchSelection(transferId).then(function (fresh) {
            if (!fresh || !fresh.eligible || !fresh.mutable) {
              toast('File selection is no longer available for this transfer.', 'info');
              renderDetailEntry(transferId);
              return;
            }
            openSelector(transferId, fresh, {auto: false});
          }).catch(function (error) {
            toast(String(error && error.message || 'File selection is unavailable.'), 'error');
          });
        });
      } else if (view.decision === 'explicit' && selectedCount > 0) {
        host.innerHTML = '<span class="dp-file-selection-summary">' + selectedCount + ' of ' +
          esc(String(view.file_count || selectedCount)) + ' files selected</span>';
      } else {
        host.innerHTML = '';
      }
    }).catch(function () { host.innerHTML = ''; });
  }

  // ── Wiring ──────────────────────────────────────────────────────────────
  function init() {
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
      renderDetailEntry(transferId);
    });
    document.addEventListener('debridpulse:detail-closed', function () {
      detailTransferId = null;
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
  });
})();
