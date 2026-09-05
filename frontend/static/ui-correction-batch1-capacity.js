/* Batch 1 Downloads capacity bridge.
 * Canonical app.js remains the transfer/list renderer; this bridge corrects the
 * legacy minimum-15 request clamp so the measured desktop page size is honored.
 * Pagination presentation remains owned by the Batch 1 renderer itself.
 */
(function () {
  'use strict';

  const DESKTOP_QUERY = '(min-width: 701px)';
  const originalApi = typeof api === 'function' ? api : null;
  const originalToast = typeof toast === 'function' ? toast : null;
  const originalLoadTorrents = typeof loadTorrents === 'function' ? loadTorrents : null;

  function downloadsActive() {
    return Boolean(
      window.matchMedia(DESKTOP_QUERY).matches
      && document.getElementById('view-torrents')?.classList.contains('active')
    );
  }

  function confirmationOverlayOpen() {
    const overlay = document.querySelector('.dp-settings-confirm-overlay');
    if (!overlay || !overlay.isConnected || overlay.hidden) return false;
    const style = getComputedStyle(overlay);
    return style.display !== 'none' && style.visibility !== 'hidden';
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

  /* A deferred capacity measurement must not replace the opener row while a
     canonical confirmation dialog is active. Accept removes the overlay before
     its caller continues, so post-confirm refreshes still run normally. */
  if (originalLoadTorrents) {
    try {
      loadTorrents = function (...args) {
        if (confirmationOverlayOpen()) return Promise.resolve(null);
        return originalLoadTorrents.apply(this, args);
      };
    } catch (_) {}
  }

  /* Preserve the canonical toast DOM contract while retaining Batch 1 adaptive
     timing. Existing error/storage owners legitimately target .dp-toast-copy. */
  if (originalToast) {
    try {
      toast = function (...args) {
        const node = originalToast.apply(this, args);
        node?.querySelector('.dp-toast-message')?.classList.add('dp-toast-copy');
        return node;
      };
    } catch (_) {}
  }

  window.DPBatch1CapacityBridge = Object.freeze({
    rewriteDownloadsListPath,
    effectivePageSize,
    confirmationOverlayOpen,
  });
})();
