/* Batch 1 Downloads capacity compatibility bridge.
 * app.js still contains the pre-Batch-1 minimum-15 request clamp. Until that
 * legacy constructor is retired, this bridge narrows only Downloads list GETs
 * so the measured desktop page size reaches the backend unchanged.
 */
(function () {
  'use strict';

  const DESKTOP_QUERY = '(min-width: 701px)';
  const originalApi = typeof api === 'function' ? api : null;

  function downloadsActive() {
    return Boolean(
      window.matchMedia(DESKTOP_QUERY).matches
      && document.getElementById('view-torrents')?.classList.contains('active')
    );
  }

  function effectivePageSize() {
    const value = Number(typeof torrentPageSize === 'undefined' ? 0 : torrentPageSize);
    return Number.isFinite(value) ? Math.max(1, Math.min(100, Math.floor(value))) : 0;
  }

  function effectivePage() {
    const value = Number(typeof torrentPage === 'undefined' ? 1 : torrentPage);
    return Number.isFinite(value) ? Math.max(1, Math.floor(value)) : 1;
  }

  function rewriteDownloadsListPath(path) {
    if (!downloadsActive() || typeof path !== 'string' || !path.startsWith('/torrents?')) return path;
    const size = effectivePageSize();
    if (!size || size >= 15) return path;
    const query = new URLSearchParams(path.slice(path.indexOf('?') + 1));
    const requested = Number(query.get('limit'));
    if (!Number.isFinite(requested) || requested < 15) return path;
    query.set('limit', String(size));
    query.set('offset', String((effectivePage() - 1) * size));
    return `/torrents?${query.toString()}`;
  }

  if (originalApi) {
    try {
      api = function (method, path, body, timeoutMs, options) {
        const correctedPath = String(method || '').toUpperCase() === 'GET'
          ? rewriteDownloadsListPath(path)
          : path;
        return originalApi.call(this, method, correctedPath, body, timeoutMs, options);
      };
    } catch (_) {}
  }

  window.DPBatch1CapacityBridge = Object.freeze({
    rewriteDownloadsListPath,
    effectivePageSize,
  });
})();
