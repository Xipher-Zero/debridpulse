/* DebridPulse v1.0.12 Details candidate disclosure + manual failover runtime.
 * Candidate presentation is backend-authored. Disclosure activation mutates only
 * its adjacent row; authoritative refresh owns source-state changes after switch.
 */
(function () {
  'use strict';

  const expandedRows = new Set();
  const switchingRows = new Set();
  let activeTransferId = null;
  let latestDetail = null;
  let providerNames = new Map();
  let presentationGeneration = 0;
  let refreshTimer = null;
  let filesPointerActive = false;
  let deferredDetail = null;
  let deferredFrame = 0;
  let installed = false;

  function html(value) {
    if (typeof window.esc === 'function') return window.esc(value);
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function fileSize(value) {
    return typeof window.fmtSize === 'function' ? window.fmtSize(value) : String(value || 0);
  }

  function fileStatus(file) {
    return typeof window.badge === 'function'
      ? window.badge(file.status, file)
      : '<span>' + html(file.status || '') + '</span>';
  }

  // ---------------------------------------------------------------------- //
  // Row identity versus artifact-mutation identity.
  //
  // presentation_id is the backend's render identity for a Files row. It is the
  // ONLY identity DOM bookkeeping uses -- row keys, expansion state, focus
  // restoration -- because a Details Files row is not necessarily an artifact:
  // it may be an artifact another transfer contributed, or a terminal
  // UNVERIFIED association that has no artifact at all.
  //
  // artifact_id is the real artifact-MUTATION identity. It is null for an
  // association row, and a contributed row's is a REAL foreign artifact that
  // must still never be mutated from this transfer's Details. So mutation is
  // permitted only for a row the backend marked as this transfer's own
  // physical artifact.
  //
  // A legacy or fabricated payload predating file_presentations carries
  // neither field. There the row IS a physical artifact and its own id is both
  // identities -- the single fallback, which invents nothing.
  // ---------------------------------------------------------------------- //
  function rowKey(file) {
    if (file && file.presentation_id != null) return String(file.presentation_id);
    return String(file && file.id != null ? file.id : '');
  }

  function artifactMutationId(file) {
    if (!file) return '';
    if (file.presentation_id == null) return file.id == null ? '' : String(file.id);
    if (String(file.relationship || '') !== 'original') return '';
    return file.artifact_id == null ? '' : String(file.artifact_id);
  }

  // The Details Files card renders the canonical-object presentation when the
  // backend provides it, and the physical collection otherwise. One owner
  // decides this for the card title and the rows alike, so the two can never
  // disagree about which collection is on screen.
  function displayRows(detail) {
    if (detail && Array.isArray(detail.file_presentations)) return detail.file_presentations;
    return detail && Array.isArray(detail.files) ? detail.files : [];
  }

  function hasDisplayRows(detail) {
    return Boolean(detail && (Array.isArray(detail.file_presentations) || Array.isArray(detail.files)));
  }

  // Backend-projected provenance only: which transfer contributed this row. A
  // native row is this transfer's own artifact and carries no redundant
  // subtitle. Nothing is derived from a URL, host, filename or adjacency.
  function originSubtitle(file) {
    const relationship = String(file.relationship || '');
    if (!relationship || relationship === 'original') return '';
    const contributor = Number(file.contributing_transfer_id);
    if (!Number.isInteger(contributor) || contributor <= 0) return '';
    return '<div class="dp-detail-file-origin">From #' + html(String(contributor)) + '</div>';
  }

  function integrationNames(settings) {
    const names = new Map();
    const integrations = settings && settings.integrations && typeof settings.integrations === 'object'
      ? settings.integrations : {};
    Object.entries(integrations).forEach(function (entry) {
      const identity = String(entry[0] || '').trim();
      const value = entry[1] || {};
      if (identity && value.name) names.set(identity, String(value.name));
    });
    return names;
  }

  function providerName(candidate) {
    const identity = String(candidate && candidate.provider_id || '').trim();
    return providerNames.get(identity) || 'Provider';
  }

  function candidateDisposition(candidate) {
    const values = Array.isArray(candidate && candidate.dispositions)
      ? candidate.dispositions.map(String).filter(Boolean).filter(function (value) {
        return !/^(active|selected|delivering)$/i.test(value);
      }) : [];
    return [String(candidate && candidate.relationship || '').trim(), ...values]
      .filter(Boolean).join(' · ');
  }

  function candidateAction(file, candidate) {
    const active = Boolean(candidate && (candidate.is_active || candidate.is_selected));
    if (active) {
      return '<span class="dp-detail-candidate-active" aria-label="Active source">ACTIVE</span>';
    }
    if (!candidate || !candidate.switch_eligible) return '';
    // No real artifact-mutation identity, no switch control. A contributed or
    // UNVERIFIED row can therefore never render one, whatever else it carries.
    const artifactId = artifactMutationId(file);
    if (!artifactId) return '';
    const candidateId = String(candidate.candidate_id || '');
    const busy = switchingRows.has(rowKey(file));
    return '<button type="button" class="dp-detail-candidate-switch" ' +
      'data-dp-artifact-id="' + html(artifactId) + '" data-dp-candidate-id="' + html(candidateId) + '"' +
      (busy ? ' disabled aria-disabled="true"' : '') + '>Switch to this source</button>';
  }

  function candidateList(file) {
    const candidates = Array.isArray(file.acquisition_candidates) ? file.acquisition_candidates : [];
    return '<div class="dp-detail-candidate-list">' + candidates.map(function (candidate) {
      return '<div class="dp-detail-candidate-item" data-dp-candidate-id="' + html(candidate.candidate_id || '') + '">' +
        '<div class="dp-detail-candidate-copy"><div class="dp-detail-candidate-route">' +
        '<span class="dp-detail-candidate-source">' + html(candidate.source_label || 'Source') + '</span>' +
        '<span class="dp-detail-candidate-arrow" aria-hidden="true">→</span>' +
        '<span class="dp-detail-candidate-provider">' + html(providerName(candidate)) + '</span></div>' +
        '<div class="dp-detail-candidate-disposition">' + html(candidateDisposition(candidate)) + '</div></div>' +
        '<div class="dp-detail-candidate-action">' + candidateAction(file, candidate) + '</div>' +
      '</div>';
    }).join('') + '</div>';
  }

  function candidateGlyph() {
    return (window.DPIcons && typeof window.DPIcons.svg === 'function')
      ? window.DPIcons.svg('network', 'dp-candidate-chip-icon') : '';
  }

  function disclosure(file) {
    const count = Number(file.candidate_count || 0);
    if (!Number.isInteger(count) || count <= 1) return '';
    // Candidate membership and failover belong to the canonical actionable
    // artifact alone. A row with no artifact-mutation identity gets no
    // disclosure, independently of what it carries.
    if (!artifactMutationId(file)) return '';
    const key = rowKey(file);
    const open = expandedRows.has(key);
    const detailsId = 'dp-detail-candidates-' + key;
    const filename = String(file.filename || 'artifact');
    // One coherent ghost-style rounded button sharing the passive chip visual
    // family (dp-candidate-chip) and the canonical Network glyph. No circular
    // count badge. Interactive: aria-expanded / aria-controls / dynamic
    // Show/Hide label are preserved for the disclosure contract.
    return '<button type="button" class="dp-candidate-chip dp-detail-candidate-disclosure" data-dp-row-id="' + html(key) + '" ' +
      'data-dp-candidate-count="' + count + '" aria-expanded="' + (open ? 'true' : 'false') + '" ' +
      'aria-controls="' + html(detailsId) + '" aria-label="' +
      html((open ? 'Hide ' : 'Show ') + count + ' Candidates for ' + filename) + '">' +
      candidateGlyph() +
      '<span class="dp-candidate-chip-count">' + count + '</span>' +
      '<span>Candidates</span></button>';
  }

  function blockedPresentation(file) {
    if (file.blocked) {
      return '<span class="badge badge-error dp-detail-file-blocked">BLOCKED: ' + html(file.block_reason) + '</span>';
    }
    if (file.block_reason) return '<div class="dp-detail-file-block-reason">' + html(file.block_reason) + '</div>';
    return '';
  }

  function candidateRow(file) {
    const key = rowKey(file);
    return '<tr class="dp-detail-candidate-row" data-dp-candidate-owner="' + html(key) + '">' +
      '<td colspan="3"><div id="dp-detail-candidates-' + html(key) + '" class="dp-detail-candidate-panel">' +
      candidateList(file) + '</div></td></tr>';
  }

  // The Details Files rows are rendered here and only here: app.js provides the
  // table shell and asks this owner for the rows (single render, from the same
  // transfer payload it just loaded), and refreshes below re-render through the
  // same function. Provider names come from the cached canonical settings.
  function rows(files) {
    try { providerNames = integrationNames(settingsData); } catch (_) { /* names stay as last known */ }
    return files.map(function (file) {
      const key = rowKey(file);
      const artifactId = artifactMutationId(file);
      const open = expandedRows.has(key) && Number(file.candidate_count || 0) > 1;
      // data-dp-artifact-id is present ONLY on a row that may drive an artifact
      // mutation, so nothing can address a contributed or association row as an
      // artifact by selector either.
      const reason = file.unverified_reason
        ? ' title="Equivalence unproven: ' + html(file.unverified_reason) + '"' : '';
      const main = '<tr class="dp-detail-file-row" data-dp-row-id="' + html(key) + '"' +
        (artifactId ? ' data-dp-artifact-id="' + html(artifactId) + '"' : '') + '>' +
        '<td class="dp-detail-filename"><div class="dp-detail-filename-line"><span class="dp-detail-filename-copy">' + html(file.filename) + '</span>' +
        disclosure(file) + '</div>' + originSubtitle(file) + blockedPresentation(file) + '</td>' +
        '<td class="sz">' + fileSize(file.size_bytes) + '</td><td' + reason + '>' + fileStatus(file) + '</td></tr>';
      return open ? main + candidateRow(file) : main;
    }).join('');
  }

  function updateDisclosure(control, file, open) {
    const key = String(control && control.dataset.dpRowId || '');
    const owner = control ? control.closest('tr.dp-detail-file-row') : null;
    if (!key || !owner) return;
    const count = Number(file && file.candidate_count || control.dataset.dpCandidateCount || 0);
    const filenameNode = owner.querySelector('.dp-detail-filename-copy');
    const filename = String((file && file.filename) || (filenameNode && filenameNode.textContent) || 'artifact');
    control.dataset.dpCandidateCount = String(count);
    control.setAttribute('aria-expanded', open ? 'true' : 'false');
    control.setAttribute('aria-label', (open ? 'Hide ' : 'Show ') + count + ' Candidates for ' + filename);
    const countNode = control.querySelector('.dp-candidate-chip-count');
    if (countNode) countNode.textContent = String(count);
    const existing = owner.parentElement ? owner.parentElement.querySelector(
      'tr.dp-detail-candidate-row[data-dp-candidate-owner="' + CSS.escape(key) + '"]') : null;
    if (open) {
      if (!existing) owner.insertAdjacentHTML('afterend', candidateRow(file));
      bindSwitches(file);
      return;
    }
    if (existing) existing.remove();
  }

  function bindDisclosure(control, file) {
    if (!control || control.dataset.dpCandidateBound === '1') return;
    const key = rowKey(file);
    control.addEventListener('click', function () {
      const open = control.getAttribute('aria-expanded') === 'true';
      if (open) {
        expandedRows.delete(key);
        updateDisclosure(control, file, false);
      } else if (Number(file.candidate_count || 0) > 1) {
        expandedRows.add(key);
        updateDisclosure(control, file, true);
      }
    });
    control.dataset.dpCandidateBound = '1';
  }

  function normalizedFailure(detail, fallback) {
    if (detail && typeof detail === 'object') {
      if (detail.message) return String(detail.message);
      if (detail.category) return String(detail.category).replace(/_/g, ' ').toLowerCase();
    }
    if (typeof detail === 'string' && detail.trim()) return detail.trim();
    return String(fallback || 'The selected candidate could not be established.');
  }

  async function switchRequest(transferId, artifactId, candidateId) {
    const controller = new AbortController();
    const timeout = window.setTimeout(function () { controller.abort(); }, 8000);
    try {
      const response = await window.debridPulseAuth.fetch('/api/torrents/' + transferId + '/artifacts/' + artifactId + '/candidate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({candidate_id: candidateId}),
        signal: controller.signal
      });
      const payload = await response.json().catch(function () { return {}; });
      if (!response.ok) {
        const error = new Error(normalizedFailure(payload.detail, response.statusText));
        error.detail = payload.detail;
        throw error;
      }
      return payload;
    } catch (error) {
      if (error && error.name === 'AbortError') throw new Error('Request timed out after 8s');
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function refreshAfterSwitch(transferId, key) {
    const generation = ++presentationGeneration;
    await fetchPresentation(transferId, generation);
    const jobs = [];
    if (typeof window.loadTorrents === 'function') jobs.push(Promise.resolve(window.loadTorrents()));
    if (typeof window.loadRecent === 'function') jobs.push(Promise.resolve(window.loadRecent()));
    await Promise.allSettled(jobs);
    const disclosureControl = document.querySelector(
      '#modal-body tr.dp-detail-file-row[data-dp-row-id="' + CSS.escape(String(key)) + '"] .dp-detail-candidate-disclosure');
    if (disclosureControl) disclosureControl.focus({preventScroll:true});
  }

  async function requestSwitch(button, file) {
    if (!button || button.disabled || activeTransferId == null) return;
    // The ONE place a Files row becomes an artifact mutation. An association or
    // contributed row resolves to no mutation identity and stops here, so no
    // request is ever issued for a presentation id.
    const artifactId = artifactMutationId(file);
    if (!artifactId) return;
    const key = rowKey(file);
    const candidateId = String(button.dataset.dpCandidateId || '');
    if (!candidateId || switchingRows.has(key)) return;
    switchingRows.add(key);
    button.disabled = true;
    button.setAttribute('aria-disabled', 'true');
    try {
      const result = await switchRequest(activeTransferId, artifactId, candidateId);
      switchingRows.delete(key);
      await refreshAfterSwitch(activeTransferId, key);
      if (typeof window.toast === 'function') {
        window.toast(String(result.filename || file.filename || 'artifact') + ' file source switched to ' + String(result.source_host || 'source'), 'success');
      }
    } catch (error) {
      switchingRows.delete(key);
      await refreshAfterSwitch(activeTransferId, key).catch(function () {});
      if (typeof window.toast === 'function') {
        window.toast({
          title: 'Unable to switch source for ' + String(file.filename || 'artifact'),
          body: normalizedFailure(error && error.detail, error && error.message)
        }, 'error');
      }
    }
  }

  function bindSwitches(file) {
    const panel = document.querySelector(
      '#modal-body tr.dp-detail-candidate-row[data-dp-candidate-owner="' + CSS.escape(rowKey(file)) + '"]');
    if (!panel) return;
    panel.querySelectorAll('.dp-detail-candidate-switch').forEach(function (button) {
      if (button.dataset.dpCandidateSwitchBound === '1') return;
      button.addEventListener('click', function () { requestSwitch(button, file); });
      button.dataset.dpCandidateSwitchBound = '1';
    });
  }

  function bindDisclosures(files) {
    files.forEach(function (file) {
      const row = document.querySelector(
        '#modal-body tr.dp-detail-file-row[data-dp-row-id="' + CSS.escape(rowKey(file)) + '"]');
      if (!row) return;
      bindDisclosure(row.querySelector('.dp-detail-candidate-disclosure'), file);
      if (expandedRows.has(rowKey(file))) bindSwitches(file);
    });
  }

  function renderNow(detail) {
    if (!hasDisplayRows(detail)) return;
    const tbody = document.querySelector('#modal-body .dp-detail-files-card .t-table tbody');
    if (!tbody) return;
    const displayed = displayRows(detail);
    const focused = document.activeElement;
    const focusedRowId = focused instanceof HTMLElement &&
      focused.matches('.dp-detail-candidate-disclosure') && tbody.contains(focused)
      ? String(focused.dataset.dpRowId || '') : '';
    const valid = new Set(displayed.map(rowKey));
    Array.from(expandedRows).forEach(function (key) {
      const file = displayed.find(function (item) { return rowKey(item) === key; });
      if (!valid.has(key) || !file || Number(file.candidate_count || 0) <= 1) expandedRows.delete(key);
    });
    tbody.innerHTML = rows(displayed);
    bindDisclosures(displayed);
    if (focusedRowId) {
      const restored = tbody.querySelector(
        'tr.dp-detail-file-row[data-dp-row-id="' + CSS.escape(focusedRowId) + '"] .dp-detail-candidate-disclosure');
      if (restored) restored.focus({preventScroll:true});
    }
  }

  function render(detail) {
    if (filesPointerActive) {
      deferredDetail = detail;
      return;
    }
    deferredDetail = null;
    renderNow(detail);
  }

  function flushDeferredRender() {
    if (filesPointerActive || !deferredDetail) return;
    const detail = deferredDetail;
    deferredDetail = null;
    renderNow(detail);
  }

  function releaseFilesPointer() {
    if (!filesPointerActive) return;
    filesPointerActive = false;
    if (deferredFrame) window.cancelAnimationFrame(deferredFrame);
    deferredFrame = window.requestAnimationFrame(function () {
      deferredFrame = 0;
      flushDeferredRender();
    });
  }

  async function fetchPresentation(id, generation) {
    if (typeof window.api !== 'function') return;
    const transferId = Number(id);
    const detail = await window.api('GET', '/torrents/' + transferId);
    if (activeTransferId !== transferId || generation !== presentationGeneration) return;
    latestDetail = detail;
    render(latestDetail);
  }

  function queueRefresh() {
    if (activeTransferId == null) return;
    const overlay = document.getElementById('overlay');
    if (!overlay || !overlay.classList.contains('open')) return;
    const transferId = activeTransferId;
    const generation = ++presentationGeneration;
    if (refreshTimer != null) window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(function () {
      refreshTimer = null;
      fetchPresentation(transferId, generation).catch(function () {});
    }, 120);
  }

  function clearRefreshState() {
    if (refreshTimer != null) window.clearTimeout(refreshTimer);
    refreshTimer = null;
    presentationGeneration += 1;
  }

  function resetDetailState() {
    expandedRows.clear();
    switchingRows.clear();
    activeTransferId = null;
    latestDetail = null;
    deferredDetail = null;
    filesPointerActive = false;
    clearRefreshState();
    if (deferredFrame) window.cancelAnimationFrame(deferredFrame);
    deferredFrame = 0;
  }

  // app.js is the sole owner of the shared detail-modal globals and the
  // #overlay shell. Candidate presentation attaches to the stable lifecycle
  // events it emits (debridpulse:detail-rendered / debridpulse:detail-closed)
  // and never wraps or reassigns a coordinator global.
  function onDetailRendered(event) {
    const transferId = Number(event && event.detail && event.detail.transferId);
    if (!Number.isFinite(transferId)) return;
    if (activeTransferId !== transferId) expandedRows.clear();
    clearRefreshState();
    activeTransferId = transferId;
    deferredDetail = null;
    const transfer = event.detail.transfer;
    latestDetail = hasDisplayRows(transfer) ? transfer : null;
    // The rows were rendered by rowsMarkup() from this same payload; only bind them.
    if (latestDetail) bindDisclosures(displayRows(latestDetail));
  }

  function onDetailClosed() {
    resetDetailState();
  }

  function install() {
    if (installed) return;
    installed = true;

    document.addEventListener('debridpulse:detail-rendered', onDetailRendered);
    document.addEventListener('debridpulse:detail-closed', onDetailClosed);

    const modalBody = document.getElementById('modal-body');
    if (modalBody && modalBody.dataset.dpCandidatePointerGuard !== '1') {
      modalBody.addEventListener('pointerdown', function (event) {
        const target = event.target instanceof Element ? event.target : null;
        if (target && target.closest('.dp-detail-files-card')) filesPointerActive = true;
      });
      modalBody.addEventListener('pointerup', releaseFilesPointer);
      modalBody.addEventListener('pointercancel', releaseFilesPointer);
      modalBody.addEventListener('pointerleave', function (event) {
        if (!event.buttons) releaseFilesPointer();
      });
      modalBody.dataset.dpCandidatePointerGuard = '1';
    }

    document.addEventListener('debridpulse:downloads-rendered', queueRefresh);
    document.addEventListener('debridpulse:dashboard-recent-rendered', queueRefresh);
  }

  window.DPDetailCandidates = Object.freeze({rowsMarkup: rows, displayRows: displayRows});

  // Candidate styling is loaded through the canonical style.css @import
  // graph, not injected here, so it has one loaded owner.
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install, {once:true});
  else install();
})();
