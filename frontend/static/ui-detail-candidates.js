/* DebridPulse v1.0.12 Details candidate disclosure runtime.
 * Renders only the allowlisted backend candidate presentation. No candidate
 * relationship, source, provider, or disposition is reconstructed in-browser.
 */
(function () {
  'use strict';

  const expandedArtifacts = new Set();
  let activeTransferId = null;
  let latestDetail = null;
  let providerNames = new Map();
  let refreshQueued = false;

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
      ? candidate.dispositions.map(String).filter(Boolean) : [];
    return [String(candidate && candidate.relationship || '').trim(), ...values]
      .filter(Boolean).join(' · ');
  }

  function candidateList(file) {
    const candidates = Array.isArray(file.acquisition_candidates) ? file.acquisition_candidates : [];
    return '<div class="dp-detail-candidate-list">' + candidates.map(function (candidate) {
      return '<div class="dp-detail-candidate-item">' +
        '<div class="dp-detail-candidate-route"><span class="dp-detail-candidate-source">' + html(candidate.source_label || 'Source') + '</span>' +
        '<span class="dp-detail-candidate-arrow" aria-hidden="true">→</span>' +
        '<span class="dp-detail-candidate-provider">' + html(providerName(candidate)) + '</span></div>' +
        '<div class="dp-detail-candidate-disposition">' + html(candidateDisposition(candidate)) + '</div>' +
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
      'aria-expanded="' + (open ? 'true' : 'false') + '" aria-controls="' + html(detailsId) + '" ' +
      'aria-label="' + html((open ? 'Hide ' : 'Show ') + count + ' Candidates for ' + filename) + '">' +
      '<span class="dp-detail-candidate-count" aria-hidden="true">' + count + '</span>' +
      '<span>Candidates</span></button>';
  }

  function blockedPresentation(file) {
    if (file.blocked) {
      return '<span class="badge badge-error dp-detail-file-blocked">BLOCKED: ' + html(file.block_reason) + '</span>';
    }
    if (file.block_reason) {
      return '<div class="dp-detail-file-block-reason">' + html(file.block_reason) + '</div>';
    }
    return '';
  }

  function rows(files) {
    return files.map(function (file) {
      const artifactId = String(file.id);
      const open = expandedArtifacts.has(artifactId) && Number(file.candidate_count || 0) > 1;
      const detailsId = 'dp-detail-candidates-' + artifactId;
      const main = '<tr class="dp-detail-file-row" data-dp-artifact-id="' + html(artifactId) + '">' +
        '<td class="dp-detail-filename"><div class="dp-detail-filename-line"><span class="dp-detail-filename-copy">' + html(file.filename) + '</span>' +
        disclosure(file) + '</div>' + blockedPresentation(file) + '</td>' +
        '<td class="sz">' + fileSize(file.size_bytes) + '</td>' +
        '<td>' + fileStatus(file) + '</td></tr>';
      if (!open) return main;
      return main + '<tr class="dp-detail-candidate-row" data-dp-candidate-owner="' + html(artifactId) + '">' +
        '<td colspan="3"><div id="' + html(detailsId) + '" class="dp-detail-candidate-panel">' + candidateList(file) + '</div></td></tr>';
    }).join('');
  }

  function render(detail) {
    if (!detail || !Array.isArray(detail.files)) return;
    const tbody = document.querySelector('#modal-body .dp-detail-files-card .t-table tbody');
    if (!tbody) return;
    const valid = new Set(detail.files.map(function (file) { return String(file.id); }));
    Array.from(expandedArtifacts).forEach(function (id) {
      if (!valid.has(id) || Number((detail.files.find(function (file) { return String(file.id) === id; }) || {}).candidate_count || 0) <= 1) {
        expandedArtifacts.delete(id);
      }
    });
    tbody.innerHTML = rows(detail.files);
  }

  async function fetchPresentation(id) {
    if (typeof window.api !== 'function') return;
    const transferId = Number(id);
    const results = await Promise.all([
      window.api('GET', '/torrents/' + transferId),
      window.api('GET', '/settings').catch(function () { return null; })
    ]);
    if (activeTransferId !== transferId) return;
    latestDetail = results[0];
    if (results[1]) providerNames = integrationNames(results[1]);
    render(latestDetail);
  }

  function queueRefresh() {
    if (refreshQueued || activeTransferId == null) return;
    const overlay = document.getElementById('overlay');
    if (!overlay || !overlay.classList.contains('open')) return;
    refreshQueued = true;
    window.setTimeout(function () {
      refreshQueued = false;
      if (activeTransferId != null) fetchPresentation(activeTransferId).catch(function () {});
    }, 120);
  }

  function install() {
    if (typeof window.showDetail !== 'function' || window.showDetail.dpCandidateWrapped) return;
    const originalShowDetail = window.showDetail;
    const wrapped = async function (id) {
      const transferId = Number(id);
      if (activeTransferId !== transferId) expandedArtifacts.clear();
      activeTransferId = transferId;
      latestDetail = null;
      const result = await originalShowDetail.apply(this, arguments);
      try {
        await fetchPresentation(transferId);
      } catch (error) {
        console.error('Details candidate presentation unavailable', error);
      }
      return result;
    };
    wrapped.dpCandidateWrapped = true;
    window.showDetail = wrapped;

    if (typeof window.closeModal === 'function' && !window.closeModal.dpCandidateWrapped) {
      const originalCloseModal = window.closeModal;
      const closeWrapped = function () {
        expandedArtifacts.clear();
        activeTransferId = null;
        latestDetail = null;
        return originalCloseModal.apply(this, arguments);
      };
      closeWrapped.dpCandidateWrapped = true;
      window.closeModal = closeWrapped;
    }

    document.addEventListener('click', function (event) {
      const target = event.target instanceof Element ? event.target : null;
      const control = target ? target.closest('.dp-detail-candidate-disclosure') : null;
      if (!control) return;
      event.preventDefault();
      event.stopPropagation();
      const artifactId = String(control.dataset.dpArtifactId || '');
      if (!artifactId) return;
      if (expandedArtifacts.has(artifactId)) expandedArtifacts.delete(artifactId);
      else expandedArtifacts.add(artifactId);
      if (latestDetail) render(latestDetail);
    }, true);

    document.addEventListener('debridpulse:downloads-rendered', queueRefresh);
    document.addEventListener('debridpulse:dashboard-recent-rendered', queueRefresh);
  }

  function loadStyle() {
    if (document.querySelector('link[data-dp-detail-candidates-style]')) return;
    const style = document.createElement('link');
    style.rel = 'stylesheet';
    style.href = '/ui-detail-candidates.css?v=1';
    style.dataset.dpDetailCandidatesStyle = '1';
    document.head.appendChild(style);
  }

  loadStyle();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', install, {once: true});
  } else {
    install();
  }
})();
