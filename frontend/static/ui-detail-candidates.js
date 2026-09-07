/* DebridPulse v1.0.12 Details candidate disclosure + manual failover runtime.
 * Candidate presentation is backend-authored. Disclosure activation mutates only
 * its adjacent row; authoritative refresh owns source-state changes after switch.
 */
(function () {
  'use strict';

  const expandedArtifacts = new Set();
  const switchingArtifacts = new Set();
  let activeTransferId = null;
  let latestDetail = null;
  let providerNames = new Map();
  let presentationGeneration = 0;
  let refreshTimer = null;
  let filesPointerActive = false;
  let deferredDetail = null;
  let deferredFrame = 0;

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
    const artifactId = String(file.id);
    const candidateId = String(candidate.candidate_id || '');
    const busy = switchingArtifacts.has(artifactId);
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

  function disclosure(file) {
    const count = Number(file.candidate_count || 0);
    if (!Number.isInteger(count) || count <= 1) return '';
    const artifactId = String(file.id);
    const open = expandedArtifacts.has(artifactId);
    const detailsId = 'dp-detail-candidates-' + artifactId;
    const filename = String(file.filename || 'artifact');
    return '<button type="button" class="dp-detail-candidate-disclosure" data-dp-artifact-id="' + html(artifactId) + '" ' +
      'data-dp-candidate-count="' + count + '" aria-expanded="' + (open ? 'true' : 'false') + '" ' +
      'aria-controls="' + html(detailsId) + '" aria-label="' +
      html((open ? 'Hide ' : 'Show ') + count + ' Candidates for ' + filename) + '">' +
      '<span class="dp-detail-candidate-count" aria-hidden="true">' + count + '</span>' +
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
    const artifactId = String(file.id);
    return '<tr class="dp-detail-candidate-row" data-dp-candidate-owner="' + html(artifactId) + '">' +
      '<td colspan="3"><div id="dp-detail-candidates-' + html(artifactId) + '" class="dp-detail-candidate-panel">' +
      candidateList(file) + '</div></td></tr>';
  }

  function rows(files) {
    return files.map(function (file) {
      const artifactId = String(file.id);
      const open = expandedArtifacts.has(artifactId) && Number(file.candidate_count || 0) > 1;
      const main = '<tr class="dp-detail-file-row" data-dp-artifact-id="' + html(artifactId) + '">' +
        '<td class="dp-detail-filename"><div class="dp-detail-filename-line"><span class="dp-detail-filename-copy">' + html(file.filename) + '</span>' +
        disclosure(file) + '</div>' + blockedPresentation(file) + '</td>' +
        '<td class="sz">' + fileSize(file.size_bytes) + '</td><td>' + fileStatus(file) + '</td></tr>';
      return open ? main + candidateRow(file) : main;
    }).join('');
  }

  function updateDisclosure(control, file, open) {
    const artifactId = String(control && control.dataset.dpArtifactId || '');
    const owner = control ? control.closest('tr.dp-detail-file-row') : null;
    if (!artifactId || !owner) return;
    const count = Number(file && file.candidate_count || control.dataset.dpCandidateCount || 0);
    const filenameNode = owner.querySelector('.dp-detail-filename-copy');
    const filename = String((file && file.filename) || (filenameNode && filenameNode.textContent) || 'artifact');
    control.dataset.dpCandidateCount = String(count);
    control.setAttribute('aria-expanded', open ? 'true' : 'false');
    control.setAttribute('aria-label', (open ? 'Hide ' : 'Show ') + count + ' Candidates for ' + filename);
    const existing = owner.parentElement ? owner.parentElement.querySelector(
      'tr.dp-detail-candidate-row[data-dp-candidate-owner="' + CSS.escape(artifactId) + '"]') : null;
    if (open) {
      if (!existing) owner.insertAdjacentHTML('afterend', candidateRow(file));
      bindSwitches(file);
      return;
    }
    if (existing) existing.remove();
  }

  function bindDisclosure(control, file) {
    if (!control || control.dataset.dpCandidateBound === '1') return;
    const artifactId = String(file.id);
    control.addEventListener('click', function () {
      const open = control.getAttribute('aria-expanded') === 'true';
      if (open) {
        expandedArtifacts.delete(artifactId);
        updateDisclosure(control, file, false);
      } else if (Number(file.candidate_count || 0) > 1) {
        expandedArtifacts.add(artifactId);
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
      const response = await fetch('/api/torrents/' + transferId + '/artifacts/' + artifactId + '/candidate', {
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

  async function refreshAfterSwitch(transferId, artifactId) {
    const generation = ++presentationGeneration;
    await fetchPresentation(transferId, generation);
    const jobs = [];
    if (typeof window.loadTorrents === 'function') jobs.push(Promise.resolve(window.loadTorrents()));
    if (typeof window.loadRecent === 'function') jobs.push(Promise.resolve(window.loadRecent()));
    await Promise.allSettled(jobs);
    const disclosureControl = document.querySelector(
      '#modal-body tr.dp-detail-file-row[data-dp-artifact-id="' + CSS.escape(String(artifactId)) + '"] .dp-detail-candidate-disclosure');
    if (disclosureControl) disclosureControl.focus({preventScroll:true});
  }

  async function requestSwitch(button, file) {
    if (!button || button.disabled || activeTransferId == null) return;
    const artifactId = String(file.id);
    const candidateId = String(button.dataset.dpCandidateId || '');
    if (!candidateId || switchingArtifacts.has(artifactId)) return;
    switchingArtifacts.add(artifactId);
    button.disabled = true;
    button.setAttribute('aria-disabled', 'true');
    try {
      const result = await switchRequest(activeTransferId, artifactId, candidateId);
      switchingArtifacts.delete(artifactId);
      await refreshAfterSwitch(activeTransferId, artifactId);
      if (typeof window.toast === 'function') {
        window.toast(String(result.filename || file.filename || 'artifact') + ' file source switched to ' + String(result.source_host || 'source'), 'success');
      }
    } catch (error) {
      switchingArtifacts.delete(artifactId);
      await refreshAfterSwitch(activeTransferId, artifactId).catch(function () {});
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
      '#modal-body tr.dp-detail-candidate-row[data-dp-candidate-owner="' + CSS.escape(String(file.id)) + '"]');
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
        '#modal-body tr.dp-detail-file-row[data-dp-artifact-id="' + CSS.escape(String(file.id)) + '"]');
      if (!row) return;
      bindDisclosure(row.querySelector('.dp-detail-candidate-disclosure'), file);
      if (expandedArtifacts.has(String(file.id))) bindSwitches(file);
    });
  }

  function renderNow(detail) {
    if (!detail || !Array.isArray(detail.files)) return;
    const tbody = document.querySelector('#modal-body .dp-detail-files-card .t-table tbody');
    if (!tbody) return;
    const valid = new Set(detail.files.map(function (file) { return String(file.id); }));
    Array.from(expandedArtifacts).forEach(function (id) {
      const file = detail.files.find(function (item) { return String(item.id) === id; });
      if (!valid.has(id) || !file || Number(file.candidate_count || 0) <= 1) expandedArtifacts.delete(id);
    });
    tbody.innerHTML = rows(detail.files);
    bindDisclosures(detail.files);
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
    const results = await Promise.all([
      window.api('GET', '/torrents/' + transferId),
      window.api('GET', '/settings').catch(function () { return null; })
    ]);
    if (activeTransferId !== transferId || generation !== presentationGeneration) return;
    latestDetail = results[0];
    if (results[1]) providerNames = integrationNames(results[1]);
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
    expandedArtifacts.clear();
    switchingArtifacts.clear();
    activeTransferId = null;
    latestDetail = null;
    deferredDetail = null;
    filesPointerActive = false;
    clearRefreshState();
    if (deferredFrame) window.cancelAnimationFrame(deferredFrame);
    deferredFrame = 0;
  }

  function install() {
    if (typeof window.showDetail !== 'function' || window.showDetail.dpCandidateWrapped) return;
    const originalShowDetail = window.showDetail;
    const wrapped = async function (id) {
      const transferId = Number(id);
      if (activeTransferId !== transferId) expandedArtifacts.clear();
      clearRefreshState();
      activeTransferId = transferId;
      latestDetail = null;
      deferredDetail = null;
      const generation = presentationGeneration;
      const result = await originalShowDetail.apply(this, arguments);
      try { await fetchPresentation(transferId, generation); }
      catch (error) { console.error('Details candidate presentation unavailable', error); }
      return result;
    };
    wrapped.dpCandidateWrapped = true;
    window.showDetail = wrapped;

    if (typeof window.closeModal === 'function' && !window.closeModal.dpCandidateWrapped) {
      const originalCloseModal = window.closeModal;
      const closeWrapped = function (eventObj) {
        const overlay = document.getElementById('overlay');
        if (!eventObj || (overlay && eventObj.target === overlay)) resetDetailState();
        return originalCloseModal.apply(this, arguments);
      };
      closeWrapped.dpCandidateWrapped = true;
      window.closeModal = closeWrapped;
    }

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

  function loadStyle() {
    if (document.querySelector('link[data-dp-detail-candidates-style]')) return;
    const style = document.createElement('link');
    style.rel = 'stylesheet';
    style.href = '/ui-detail-candidates.css?v=3';
    style.dataset.dpDetailCandidatesStyle = '1';
    document.head.appendChild(style);
  }

  loadStyle();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install, {once:true});
  else install();
})();
