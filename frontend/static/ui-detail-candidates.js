/* DebridPulse v1.0.12 Details candidate disclosure + manual failover runtime. */
(function () {
  'use strict';

  const expanded = new Set();
  const switching = new Set();
  let activeTransferId = null;
  let generation = 0;
  let providerNames = new Map();
  let pointerActive = false;
  let deferredDetail = null;

  const h = value => typeof window.esc === 'function'
    ? window.esc(value)
    : String(value == null ? '' : value)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');

  function names(settings) {
    const result = new Map();
    const integrations = settings?.integrations || {};
    Object.entries(integrations).forEach(([id, value]) => {
      if (id && value?.name) result.set(String(id), String(value.name));
    });
    return result;
  }

  function providerName(candidate) {
    return providerNames.get(String(candidate?.provider_id || '')) || 'Provider';
  }

  function disposition(candidate) {
    const values = Array.isArray(candidate?.dispositions)
      ? candidate.dispositions.map(String).filter(value => !/^(active|selected|delivering)$/i.test(value))
      : [];
    return [String(candidate?.relationship || '').trim(), ...values].filter(Boolean).join(' · ');
  }

  function action(file, candidate) {
    if (candidate?.is_active || candidate?.is_selected) {
      return '<span class="dp-detail-candidate-active" aria-label="Active source">ACTIVE</span>';
    }
    if (!candidate?.switch_eligible) return '';
    const artifactId = String(file.id);
    return '<button type="button" class="dp-detail-candidate-switch" data-artifact-id="' + h(artifactId) +
      '" data-candidate-id="' + h(candidate.candidate_id || '') + '"' +
      (switching.has(artifactId) ? ' disabled aria-disabled="true"' : '') +
      '>Switch to this source</button>';
  }

  function candidateList(file) {
    const candidates = Array.isArray(file?.acquisition_candidates) ? file.acquisition_candidates : [];
    return '<div class="dp-detail-candidate-list">' + candidates.map(candidate =>
      '<div class="dp-detail-candidate-item" data-candidate-id="' + h(candidate.candidate_id || '') + '">' +
        '<div class="dp-detail-candidate-copy"><div class="dp-detail-candidate-route">' +
          '<span class="dp-detail-candidate-source">' + h(candidate.source_label || 'Source') + '</span>' +
          '<span class="dp-detail-candidate-arrow" aria-hidden="true">→</span>' +
          '<span class="dp-detail-candidate-provider">' + h(providerName(candidate)) + '</span>' +
        '</div><div class="dp-detail-candidate-disposition">' + h(disposition(candidate)) + '</div></div>' +
        '<div class="dp-detail-candidate-action">' + action(file, candidate) + '</div>' +
      '</div>'
    ).join('') + '</div>';
  }

  function disclosure(file) {
    const count = Number(file?.candidate_count || 0);
    if (!Number.isInteger(count) || count <= 1) return '';
    const artifactId = String(file.id);
    const open = expanded.has(artifactId);
    return '<button type="button" class="dp-detail-candidate-disclosure" data-artifact-id="' + h(artifactId) +
      '" aria-expanded="' + (open ? 'true' : 'false') + '" aria-controls="dp-detail-candidates-' + h(artifactId) +
      '" aria-label="' + h((open ? 'Hide ' : 'Show ') + count + ' Candidates for ' + String(file.filename || 'artifact')) + '">' +
      '<span class="dp-detail-candidate-count" aria-hidden="true">' + count + '</span><span>Candidates</span></button>';
  }

  function fileRow(file) {
    const artifactId = String(file.id);
    const blocked = file.blocked
      ? '<span class="badge badge-error dp-detail-file-blocked">BLOCKED: ' + h(file.block_reason) + '</span>'
      : (file.block_reason ? '<div class="dp-detail-file-block-reason">' + h(file.block_reason) + '</div>' : '');
    const fmt = typeof window.fmtSize === 'function' ? window.fmtSize(file.size_bytes) : String(file.size_bytes || 0);
    const badge = typeof window.badge === 'function' ? window.badge(file.status, file) : h(file.status || '');
    const main = '<tr class="dp-detail-file-row" data-artifact-id="' + h(artifactId) + '">' +
      '<td class="dp-detail-filename"><div class="dp-detail-filename-line"><span class="dp-detail-filename-copy">' + h(file.filename) +
      '</span>' + disclosure(file) + '</div>' + blocked + '</td><td class="sz">' + fmt + '</td><td>' + badge + '</td></tr>';
    if (!expanded.has(artifactId) || Number(file.candidate_count || 0) <= 1) return main;
    return main + '<tr class="dp-detail-candidate-row" data-candidate-owner="' + h(artifactId) + '"><td colspan="3">' +
      '<div id="dp-detail-candidates-' + h(artifactId) + '" class="dp-detail-candidate-panel">' + candidateList(file) + '</div></td></tr>';
  }

  function renderNow(detail) {
    if (!detail || !Array.isArray(detail.files)) return;
    const tbody = document.querySelector('#modal-body .dp-detail-files-card .t-table tbody');
    if (!tbody) return;
    const valid = new Set(detail.files.map(file => String(file.id)));
    [...expanded].forEach(id => {
      const file = detail.files.find(item => String(item.id) === id);
      if (!valid.has(id) || !file || Number(file.candidate_count || 0) <= 1) expanded.delete(id);
    });
    tbody.innerHTML = detail.files.map(fileRow).join('');
    bind(detail.files);
  }

  function render(detail) {
    if (pointerActive) {
      deferredDetail = detail;
      return;
    }
    deferredDetail = null;
    renderNow(detail);
  }

  async function fetchDetail(transferId, expectedGeneration) {
    const [detail, settings] = await Promise.all([
      window.api('GET', '/torrents/' + transferId),
      window.api('GET', '/settings').catch(() => null),
    ]);
    if (activeTransferId !== transferId || generation !== expectedGeneration) return null;
    if (settings) providerNames = names(settings);
    render(detail);
    return detail;
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
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch('/api/torrents/' + transferId + '/artifacts/' + artifactId + '/candidate', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({candidate_id:candidateId}), signal:controller.signal,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(normalizedFailure(payload.detail, response.statusText));
        error.detail = payload.detail;
        throw error;
      }
      return payload;
    } catch (error) {
      if (error?.name === 'AbortError') throw new Error('Request timed out after 8s');
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  async function refreshAll(artifactId) {
    const transferId = activeTransferId;
    const nextGeneration = ++generation;
    await fetchDetail(transferId, nextGeneration);
    const work = [];
    if (typeof window.loadTorrents === 'function') work.push(Promise.resolve(window.loadTorrents()));
    if (typeof window.loadRecent === 'function') work.push(Promise.resolve(window.loadRecent()));
    await Promise.allSettled(work);
    document.querySelector('#modal-body tr.dp-detail-file-row[data-artifact-id="' + CSS.escape(String(artifactId)) + '"] .dp-detail-candidate-disclosure')
      ?.focus({preventScroll:true});
  }

  async function requestSwitch(button, file) {
    if (!button || button.disabled || activeTransferId == null) return;
    const artifactId = String(file.id);
    const candidateId = String(button.dataset.candidateId || '');
    if (!candidateId || switching.has(artifactId)) return;
    switching.add(artifactId);
    button.disabled = true;
    button.setAttribute('aria-disabled','true');
    try {
      const result = await switchRequest(activeTransferId, artifactId, candidateId);
      await refreshAll(artifactId);
      window.toast?.(String(result.filename || file.filename || 'artifact') + ' file source switched to ' + String(result.source_host || 'source'), 'success');
    } catch (error) {
      await refreshAll(artifactId).catch(() => {});
      window.toast?.('Unable to switch source for ' + String(file.filename || 'artifact') + ': ' + normalizedFailure(error?.detail, error?.message), 'error');
    } finally {
      switching.delete(artifactId);
    }
  }

  function bind(files) {
    files.forEach(file => {
      const artifactId = String(file.id);
      const row = document.querySelector('#modal-body tr.dp-detail-file-row[data-artifact-id="' + CSS.escape(artifactId) + '"]');
      const control = row?.querySelector('.dp-detail-candidate-disclosure');
      if (control && control.dataset.bound !== '1') {
        control.addEventListener('click', () => {
          if (expanded.has(artifactId)) expanded.delete(artifactId); else expanded.add(artifactId);
          renderNow({files});
        });
        control.dataset.bound = '1';
      }
      document.querySelectorAll('#modal-body tr.dp-detail-candidate-row[data-candidate-owner="' + CSS.escape(artifactId) + '"] .dp-detail-candidate-switch')
        .forEach(button => {
          if (button.dataset.bound === '1') return;
          button.addEventListener('click', () => requestSwitch(button, file));
          button.dataset.bound = '1';
        });
    });
  }

  function reset() {
    expanded.clear();
    switching.clear();
    activeTransferId = null;
    deferredDetail = null;
    pointerActive = false;
    generation += 1;
  }

  function install() {
    if (typeof window.showDetail !== 'function' || window.showDetail.dpCandidateWrapped) return;
    const originalShow = window.showDetail;
    const wrappedShow = async function (id) {
      const transferId = Number(id);
      if (activeTransferId !== transferId) expanded.clear();
      activeTransferId = transferId;
      const expectedGeneration = ++generation;
      const result = await originalShow.apply(this, arguments);
      try { await fetchDetail(transferId, expectedGeneration); }
      catch (error) { console.error('Details candidate presentation unavailable', error); }
      return result;
    };
    wrappedShow.dpCandidateWrapped = true;
    window.showDetail = wrappedShow;

    if (typeof window.closeModal === 'function' && !window.closeModal.dpCandidateWrapped) {
      const originalClose = window.closeModal;
      const wrappedClose = function (eventObj) {
        const overlay = document.getElementById('overlay');
        if (!eventObj || eventObj.target === overlay) reset();
        return originalClose.apply(this, arguments);
      };
      wrappedClose.dpCandidateWrapped = true;
      window.closeModal = wrappedClose;
    }

    const modalBody = document.getElementById('modal-body');
    if (modalBody && modalBody.dataset.dpCandidatePointerGuard !== '1') {
      modalBody.addEventListener('pointerdown', event => {
        if (!(event.target instanceof Element) || !event.target.closest('.dp-detail-files-card')) return;
        pointerActive = true;
        try { modalBody.setPointerCapture(event.pointerId); } catch (_) {}
      });
      const release = event => {
        try { if (modalBody.hasPointerCapture(event.pointerId)) modalBody.releasePointerCapture(event.pointerId); } catch (_) {}
        pointerActive = false;
        if (deferredDetail) {
          const pending = deferredDetail;
          deferredDetail = null;
          requestAnimationFrame(() => renderNow(pending));
        }
      };
      modalBody.addEventListener('pointerup', release);
      modalBody.addEventListener('pointercancel', release);
      modalBody.dataset.dpCandidatePointerGuard = '1';
    }
  }

  function loadStyle() {
    if (document.querySelector('link[data-dp-detail-candidates-style]')) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/ui-detail-candidates.css?v=2';
    link.dataset.dpDetailCandidatesStyle = '1';
    document.head.appendChild(link);
  }

  loadStyle();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install, {once:true});
  else install();
})();
