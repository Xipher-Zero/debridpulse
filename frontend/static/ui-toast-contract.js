/* DebridPulse 1.0.12 canonical toast compatibility bridge.
 *
 * operator-title.js owns toast rendering, geometry, semantics, and lifetime.
 * This bridge preserves the reviewed Batch-1 copy corrections while preventing
 * older correction runtimes from replacing the canonical presenter.
 */
(function () {
  'use strict';

  function correctedToastMessage(message) {
    if (message && typeof message === 'object') return message;
    const text = String(message ?? '');
    if (/^Line \d+: enter an HTTP\(S\) link or magnet URI$/i.test(text)) {
      return 'DebridPulse stared at that for a moment. It is not a link, magnet, or torrent.';
    }
    if (text === 'Checking AllDebrid for ready torrents…' || text === 'Checking AllDebrid for ready torrents...') {
      return 'Checking transfers for recoverable work…';
    }
    return text;
  }

  function canonicalPresenter() {
    return window.DPIcons && typeof window.DPIcons.toast === 'function'
      ? window.DPIcons.toast
      : null;
  }

  function canonicalDuration(message) {
    const corrected = correctedToastMessage(message);
    if (window.DPIcons && typeof window.DPIcons.toastDuration === 'function') {
      return window.DPIcons.toastDuration(corrected);
    }
    const parts = corrected && typeof corrected === 'object'
      ? [corrected.title, corrected.body]
      : [corrected];
    const text = parts.filter(value => value != null).map(String).join(' ').trim();
    const words = text ? text.split(/\s+/u).filter(Boolean).length : 0;
    return Math.max(3000, Math.min(10000, words * 250));
  }

  function publicToast(message, type) {
    const presenter = canonicalPresenter();
    if (!presenter) return null;
    return presenter(correctedToastMessage(message), type);
  }

  function installPublicToast() {
    window.toast = publicToast;
    window.DPToastDuration = canonicalDuration;
  }

  function normalizedBatch(value) {
    if (!value || typeof value !== 'object') return value;
    return Object.freeze(Object.assign({}, value, {toastDuration: canonicalDuration}));
  }

  const existing = window.DPUICorrectionBatch1;
  let batchValue = normalizedBatch(existing);
  const descriptor = Object.getOwnPropertyDescriptor(window, 'DPUICorrectionBatch1');
  if (!descriptor || descriptor.configurable) {
    Object.defineProperty(window, 'DPUICorrectionBatch1', {
      configurable: true,
      enumerable: true,
      get: function () { return batchValue; },
      set: function (value) {
        batchValue = normalizedBatch(value);
        installPublicToast();
      }
    });
  } else if (existing) {
    try { window.DPUICorrectionBatch1 = batchValue; } catch (_error) { /* immutable legacy global */ }
  }

  installPublicToast();
  window.DPToastContract = Object.freeze({
    toast: publicToast,
    duration: canonicalDuration,
    correctedMessage: correctedToastMessage
  });
})();
