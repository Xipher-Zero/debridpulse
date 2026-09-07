/* DebridPulse topbar scheduler-concurrency projection.
 *
 * The badge denominator is DebridPulse scheduler capacity, not raw aria2 state.
 * Keep every render anchored to the universal transfer-policy setting and reject
 * zero/stale control-plane projections that would otherwise leave "active / 0".
 */
(function () {
  'use strict';

  const originalUpdate = window.updateAria2TopbarBadge;
  if (typeof originalUpdate !== 'function') return;

  function currentSettings() {
    try {
      return settingsData && typeof settingsData === 'object' ? settingsData : null;
    } catch (_) {
      return null;
    }
  }

  function positiveInteger(value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed) || parsed <= 0) return null;
    return Math.max(1, Math.trunc(parsed));
  }

  function canonicalConcurrency(cfg) {
    return positiveInteger(
      cfg && cfg.transfer_policy && cfg.transfer_policy.max_concurrent_executions
    );
  }

  function configuredConcurrency(cfg) {
    const settings = cfg || currentSettings() || {};
    return canonicalConcurrency(settings)
      || positiveInteger(settings.max_concurrent_downloads)
      || positiveInteger(settings.aria2_max_active_downloads)
      || 3;
  }

  function resolveConcurrency(patch) {
    const cfg = currentSettings() || {};
    const canonical = canonicalConcurrency(cfg);
    const requested = positiveInteger(patch && patch.maxDl);
    const aliases = [
      positiveInteger(cfg.max_concurrent_downloads),
      positiveInteger(cfg.aria2_max_active_downloads),
    ].filter(Boolean);

    /* A positive patch may represent an operator-applied concurrency change.
       Accept it when it already agrees with canonical state or with a positive
       cached alias updated by that operator path. Otherwise the universal
       transfer policy remains authoritative over stale/default aria2 data. */
    let resolved = canonical || configuredConcurrency(cfg);
    if (
      requested &&
      (!canonical || requested === canonical || aliases.includes(requested))
    ) {
      resolved = requested;
    }

    if (cfg && typeof cfg === 'object') {
      cfg.max_concurrent_downloads = resolved;
      cfg.aria2_max_active_downloads = resolved;
      if (cfg.transfer_policy && typeof cfg.transfer_policy === 'object') {
        cfg.transfer_policy.max_concurrent_executions = resolved;
      }
    }
    return resolved;
  }

  window.updateAria2TopbarBadge = function (patch) {
    const next = Object.assign({}, patch || {});
    next.maxDl = resolveConcurrency(next);
    return originalUpdate(next);
  };

  window.DPTopbarConcurrency = Object.freeze({
    configuredConcurrency: function () {
      return configuredConcurrency(currentSettings() || {});
    },
  });

  /* Replace the literal first-paint denominator immediately. Once /settings
     resolves, the next normal badge refresh reprojects the canonical value. */
  window.updateAria2TopbarBadge({});
})();
