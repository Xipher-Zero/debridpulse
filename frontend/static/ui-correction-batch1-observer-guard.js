/* Batch 1 observer guard.
 * The measured Downloads pagination owner needs geometry triggers, not a broad
 * observer over its own rendered row subtree. Ignore only that self-observation
 * target; every other MutationObserver/ResizeObserver use remains native.
 */
(function () {
  'use strict';

  function isDownloadsCard(target) {
    return Boolean(
      target
      && target.nodeType === 1
      && typeof target.matches === 'function'
      && target.matches('#view-torrents > .card')
    );
  }

  const NativeMutationObserver = window.MutationObserver;
  if (typeof NativeMutationObserver === 'function') {
    class DPGuardedMutationObserver extends NativeMutationObserver {
      observe(target, options) {
        if (
          isDownloadsCard(target)
          && options
          && options.subtree
          && (options.childList || options.attributes)
        ) {
          return;
        }
        return super.observe(target, options);
      }
    }
    window.MutationObserver = DPGuardedMutationObserver;
  }

  const NativeResizeObserver = window.ResizeObserver;
  if (typeof NativeResizeObserver === 'function') {
    class DPGuardedResizeObserver extends NativeResizeObserver {
      observe(target, options) {
        if (isDownloadsCard(target)) return;
        return super.observe(target, options);
      }
    }
    window.ResizeObserver = DPGuardedResizeObserver;
  }

  window.DPBatch1ObserverGuard = Object.freeze({active: true});
})();
