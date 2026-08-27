/* DebridPulse v1.0.11 concise error semantics.
 *
 * Presentation only. Persistent lifecycle status and progress remain authoritative;
 * this layer gives terminal failures a high-salience progress treatment and turns
 * retained diagnostics into short operator-facing status labels.
 */
(function () {
  'use strict';

  const FAILURE_LABELS = Object.freeze({
    unsupported_host: 'Unsupported Host',
    source_unavailable: 'Source Unavailable',
    invalid_link: 'Invalid Link',
    link_unavailable: 'Link Unavailable',
    link_timeout: 'Link Timeout',
    magnet_rejected: 'Magnet Rejected',
    torrent_rejected: 'Torrent Rejected',
    provider_expired: 'Provider Expired',
    provider_unreachable: 'Provider Unreachable',
    provider_sync_failed: 'Provider Sync Failed',
    provider_auth_failed: 'Provider Auth Failed',
    queue_failed: 'Queue Failed',
    downloader_offline: 'Downloader Offline',
    download_failed: 'Download Failed',
    disk_full: 'Disk Full',
    write_failed: 'Write Failed',
    extraction_failed: 'Extraction Failed',
    provider_error: 'Provider Error'
  });

  const detailCache = new Map();
  const inflight = new Map();
  let installed = false;

  function normalizedDiagnostic(detail) {
    if (!detail || typeof detail !== 'object') return '';
    const values = [
      detail.error_message,
      detail.provider_status,
      detail.provider_status_code,
      detail.extraction_status
    ];

    (detail.files || []).forEach(function (file) {
      values.push(
        file && file.block_reason,
        file && file.status,
        file && file.error_message
      );
    });
    (detail.events || []).forEach(function (event) {
      values.push(event && event.message);
    });

    return values
      .filter(function (value) { return value !== null && value !== undefined && value !== ''; })
      .map(function (value) { return String(value); })
      .join('\n');
  }

  function classifyFailure(detail) {
    const raw = normalizedDiagnostic(detail);
    const lower = raw.toLowerCase();
    const upper = raw.toUpperCase();

    /* Most specific source/link causes first. */
    if (
      upper.includes('LINK_HOST_NOT_SUPPORTED') ||
      lower.includes('host or link is not supported') ||
      lower.includes('unsupported host')
    ) return 'unsupported_host';

    if (
      upper.includes('LINK_DOWN') ||
      lower.includes('no longer available on the source host') ||
      lower.includes('no longer available on their source hosts') ||
      lower.includes('source file') && lower.includes('missing') ||
      lower.includes('magnet no longer exists on alldebrid')
    ) return 'source_unavailable';

    if (
      lower.includes('invalid debrid link') ||
      lower.includes('at least one http or https link is required') ||
      lower.includes('malformed url') ||
      lower.includes('invalid url')
    ) return 'invalid_link';

    if (
      lower.includes('returned no download link') ||
      lower.includes('no usable download url') ||
      lower.includes('streaming selection instead of a direct download link') ||
      lower.includes('empty download url from unlock')
    ) return 'link_unavailable';

    if (
      lower.includes('delayed link generation timed out') ||
      lower.includes('link generation timed out')
    ) return 'link_timeout';

    /* Local storage failures need to beat the generic aria2/download category. */
    if (
      lower.includes('no space left on device') ||
      lower.includes('disk full') ||
      lower.includes('not enough disk space')
    ) return 'disk_full';

    if (
      lower.includes('write failed') ||
      lower.includes('failed to write') ||
      lower.includes('write error') ||
      lower.includes('filesystem error')
    ) return 'write_failed';

    if (
      String(detail && detail.extraction_status || '').toLowerCase() === 'error' ||
      lower.includes('extraction failed') ||
      lower.includes('extract failed') ||
      lower.includes('auto-extract') && lower.includes('failed')
    ) return 'extraction_failed';

    if (
      lower.includes('expired') && (
        lower.includes('alldebrid') ||
        lower.includes('provider') ||
        lower.includes('files removed from cache')
      )
    ) return 'provider_expired';

    if (
      lower.includes('polling issue') ||
      lower.includes('polling failed') ||
      lower.includes('provider sync')
    ) return 'provider_sync_failed';

    if (
      upper.includes('AUTH_') ||
      upper.includes('APIKEY') ||
      lower.includes('api key') && (
        lower.includes('invalid') ||
        lower.includes('missing') ||
        lower.includes('not configured')
      ) ||
      lower.includes('unauthorized') ||
      lower.includes('forbidden') ||
      lower.includes('authentication failed')
    ) return 'provider_auth_failed';

    if (
      lower.includes('network error') ||
      /alldebrid http 5\d\d/.test(lower) ||
      lower.includes('provider unreachable') ||
      lower.includes('could not connect to alldebrid')
    ) return 'provider_unreachable';

    if (
      lower.includes('aria2 rejected download request') ||
      lower.includes('unable to queue aria2 download') ||
      lower.includes('aria2-dispatch') && lower.includes('rejected')
    ) return 'queue_failed';

    if (
      lower.includes('aria2 unreachable') ||
      lower.includes('aria2 not reachable') ||
      lower.includes('downloader offline') ||
      lower.includes('cannot connect to host') && lower.includes('aria2')
    ) return 'downloader_offline';

    if (
      lower.includes('one or more aria2 transfers failed') ||
      lower.includes('aria2 download failed') ||
      lower.includes('max retries') && lower.includes('aria2') ||
      lower.includes('download failed') && lower.includes('aria2')
    ) return 'download_failed';

    if (
      lower.includes('rejected the magnet') ||
      lower.includes('magnet rejected') ||
      lower.includes('invalid magnet')
    ) return 'magnet_rejected';

    if (
      lower.includes('alldebrid upload error') ||
      lower.includes('torrent upload') && (
        lower.includes('rejected') ||
        lower.includes('failed')
      )
    ) return 'torrent_rejected';

    return 'provider_error';
  }

  function transferIdForRow(row) {
    if (!row) return '';
    const dataId = String(row.dataset && row.dataset.torrentId || '').trim();
    if (/^\d+$/.test(dataId)) return dataId;

    const onclick = row.getAttribute && String(row.getAttribute('onclick') || '');
    const match = onclick.match(/showDetail\(\s*(\d+)/);
    return match ? match[1] : '';
  }

  function failedRows() {
    return Array.from(document.querySelectorAll(
      '#dash-tbody tr, #t-tbody tr'
    )).filter(function (row) {
      return !!row.querySelector('.badge-error');
    });
  }

  function actualPercent(row) {
    const label = row && row.querySelector('.prog-pct');
    const match = String(label && label.textContent || '').match(/-?\d+(?:\.\d+)?/);
    if (!match) return 0;
    const parsed = Number(match[0]);
    if (!Number.isFinite(parsed)) return 0;
    return Math.max(0, Math.min(100, parsed));
  }

  function paintFailedProgress(row) {
    if (!row || !row.querySelector('.badge-error')) return;
    const fill = row.querySelector('.prog-fill');
    if (!fill) return;

    const pct = actualPercent(row);
    const visualWidth = pct <= 0 ? 100 : pct;

    /* Strip any stale success paint before applying the terminal-error contract.
       Inline !important is intentional: several migration-era progress rules also
       carry !important, and failure red must be the final visual authority. */
    fill.classList.remove('done');
    fill.classList.add('error', 'dp-terminal-error-progress');
    fill.style.setProperty('width', visualWidth + '%');
    fill.style.setProperty('opacity', '1');
    fill.style.setProperty('background', 'var(--dp-state-error)', 'important');
    fill.style.setProperty('background-color', 'var(--dp-state-error)', 'important');
    fill.style.setProperty('background-image', 'none', 'important');
    fill.style.setProperty(
      'box-shadow',
      '0 0 8px color-mix(in srgb, var(--dp-state-error) 88%, transparent), 0 0 17px color-mix(in srgb, var(--dp-state-error) 46%, transparent)',
      'important'
    );
    fill.style.setProperty('filter', 'saturate(1.12) brightness(1.08)');
    fill.dataset.dpActualProgress = String(pct);
    fill.dataset.dpVisualProgress = String(visualWidth);
  }

  function setFailureBadgeLabel(row, code) {
    const badge = row && row.querySelector('.badge-error');
    const label = badge && badge.querySelector('.dp-status-label');
    const copy = FAILURE_LABELS[code] || FAILURE_LABELS.provider_error;
    if (!badge) return;

    badge.dataset.dpFailureCode = code || 'provider_error';
    badge.setAttribute('title', copy);

    if (label) {
      if (label.textContent !== copy) label.textContent = copy;
      return;
    }

    /* Defensive fallback for a row rendered before canonical badge decoration. */
    Array.from(badge.childNodes).forEach(function (node) {
      if (!(node.nodeType === 1 && node.classList && node.classList.contains('dp-status-icon'))) {
        node.remove();
      }
    });
    const span = document.createElement('span');
    span.className = 'dp-status-label';
    span.textContent = copy;
    badge.appendChild(span);
  }

  async function fetchDetail(id) {
    if (detailCache.has(id)) return detailCache.get(id);
    if (inflight.has(id)) return inflight.get(id);

    const request = fetch('/api/torrents/' + encodeURIComponent(id), {
      headers: {'Accept': 'application/json'}
    })
      .then(function (response) {
        if (!response.ok) throw new Error('detail fetch failed: ' + response.status);
        return response.json();
      })
      .then(function (detail) {
        detailCache.set(id, detail);
        inflight.delete(id);
        return detail;
      })
      .catch(function (error) {
        inflight.delete(id);
        throw error;
      });

    inflight.set(id, request);
    return request;
  }

  async function enrichFailureRow(row) {
    if (!row || !row.isConnected || !row.querySelector('.badge-error')) return;

    paintFailedProgress(row);
    const id = transferIdForRow(row);
    if (!id) {
      setFailureBadgeLabel(row, 'provider_error');
      return;
    }

    if (row.dataset.dpFailureDetail === 'resolved') return;
    row.dataset.dpFailureDetail = 'loading';

    try {
      const detail = await fetchDetail(id);
      if (!row.isConnected) return;
      setFailureBadgeLabel(row, classifyFailure(detail));
      row.dataset.dpFailureDetail = 'resolved';
      paintFailedProgress(row);
    } catch (_) {
      if (!row.isConnected) return;
      setFailureBadgeLabel(row, 'provider_error');
      row.dataset.dpFailureDetail = 'unavailable';
      paintFailedProgress(row);
    }
  }

  function enrichVisibleFailures() {
    failedRows().forEach(function (row) {
      paintFailedProgress(row);
      void enrichFailureRow(row);
    });
  }

  function installProgressOverride() {
    /* Preserve stored/visible percentage. A zero-byte terminal failure uses a
       full-width red signal only visually; its label remains 0%. */
    window.progress = function progressWithTerminalFailure(pct, status) {
      const state = String(status || '').toLowerCase();
      const done = state === 'completed';
      const failed = state === 'error';
      const active = state === 'downloading';
      const raw = Number(pct);
      const actual = done
        ? 100
        : Math.min(Math.max(Number.isFinite(raw) ? raw : 0, 0), 100);
      const showStripe = active && actual === 0;
      const visual = failed && actual === 0 ? 100 : actual;
      let fillStyle = showStripe
        ? 'width:100%;opacity:.35;background:repeating-linear-gradient(90deg,var(--accent) 0,var(--accent) 8px,transparent 8px,transparent 16px)'
        : 'width:' + visual + '%';
      if (failed) {
        fillStyle += ';opacity:1;background:var(--dp-state-error)!important;background-color:var(--dp-state-error)!important;background-image:none!important;box-shadow:0 0 8px color-mix(in srgb,var(--dp-state-error) 88%,transparent),0 0 17px color-mix(in srgb,var(--dp-state-error) 46%,transparent)!important;filter:saturate(1.12) brightness(1.08)';
      }
      const cls = done ? 'done' : (failed ? 'error dp-terminal-error-progress' : '');
      const label = done ? '100%' : (showStripe ? '…' : actual.toFixed(0) + '%');
      const attrs = failed
        ? ' data-dp-actual-progress="' + actual + '" data-dp-visual-progress="' + visual + '"'
        : '';
      return '<div class="prog"><div class="prog-fill ' + cls + '" style="' + fillStyle + '"' + attrs + '></div></div>' +
             '<span class="prog-pct">' + label + '</span>';
    };
  }

  function observeTransferTables() {
    ['dash-tbody', 't-tbody'].forEach(function (id) {
      const host = document.getElementById(id);
      if (!host || host.dataset.dpErrorSemanticsObserved === '1') return;
      host.dataset.dpErrorSemanticsObserved = '1';
      new MutationObserver(function () {
        window.requestAnimationFrame(enrichVisibleFailures);
      }).observe(host, {
        childList: true,
        subtree: true,
        characterData: true
      });
    });
  }

  function initialize() {
    if (installed) {
      enrichVisibleFailures();
      return;
    }
    installed = true;
    installProgressOverride();
    observeTransferTables();
    enrichVisibleFailures();
    window.DPFailureSemantics = Object.freeze({
      labels: FAILURE_LABELS,
      classify: classifyFailure
    });
  }

  function startWhenReady() {
    if (typeof window.progress !== 'function' || typeof window.badge !== 'function') {
      window.setTimeout(startWhenReady, 0);
      return;
    }
    initialize();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startWhenReady, {once: true});
  } else {
    startWhenReady();
  }
})();
