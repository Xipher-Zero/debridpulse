/* DebridPulse v1.0.6 operator-title state stabilization.
 *
 * The backend already exposes authoritative transfer counts in stats.by_status.
 * Treat queued + downloading as one logical download phase so brief aria2 file
 * handoffs do not make the browser tab appear idle. Throughput remains live and
 * may truthfully fall to zero during a handoff. A short, cancelable idle delay
 * absorbs transient state ordering without masking a real pause.
 */
(function () {
  'use strict';

  const IDLE_CONFIRM_MS = 1500;
  let idleTimer = null;
  let latestLogicalActive = 0;

  function removeLegacyStartupDebugSurface() {
    // app.js still contains a defensive startup retry/debug helper from the
    // inherited UI. The helper already no-ops when this node is absent, so
    // remove only the dashboard presentation while preserving retry behavior,
    // connection indicators, Event Log reporting and backend diagnostics.
    const debugStatus = document.getElementById('debug-status');
    if (debugStatus) debugStatus.remove();
  }

  function installDuplicateStatusStyle() {
    if (document.getElementById('debridpulse-duplicate-status-style')) return;
    const style = document.createElement('style');
    style.id = 'debridpulse-duplicate-status-style';
    style.textContent = [
      '.badge-duplicate {',
      '  background: rgba(234,179,8,.14);',
      '  color: var(--yellow);',
      '  text-transform: capitalize;',
      '}'
    ].join('\n');
    document.head.appendChild(style);
  }

  removeLegacyStartupDebugSurface();
  installDuplicateStatusStyle();

  function nonNegativeCount(value) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return 0;
    return Math.max(0, Math.trunc(parsed));
  }

  function logicalActiveCount(stats) {
    const byStatus = stats && stats.by_status;

    if (byStatus && typeof byStatus === 'object') {
      return nonNegativeCount(byStatus.downloading) +
        nonNegativeCount(byStatus.queued);
    }

    // Compatibility fallback for an older/incomplete stats payload.
    return nonNegativeCount(stats && stats.operator_active_downloads);
  }

  function cancelIdleTimer() {
    if (idleTimer !== null) {
      clearTimeout(idleTimer);
      idleTimer = null;
    }
  }

  function setIdleNow() {
    cancelIdleTimer();
    latestLogicalActive = 0;
    _operatorTitleState.active = 0;
    _operatorTitleState.progress = 0;
    renderOperatorTitle();
  }

  window.updateOperatorTitle = function updateOperatorTitle(stats) {
    const logicalActive = logicalActiveCount(stats);
    latestLogicalActive = logicalActive;

    // A global pause is explicit operator intent; do not delay its presentation.
    if (stats && stats.paused) {
      setIdleNow();
      return;
    }

    if (logicalActive > 0) {
      cancelIdleTimer();
      _operatorTitleState.active = logicalActive;

      const rawProgress = stats && stats.operator_active_progress_pct;
      const progress = rawProgress == null ? NaN : Number(rawProgress);
      if (Number.isFinite(progress)) {
        _operatorTitleState.progress = Math.min(
          100,
          Math.max(0, Math.round(progress))
        );
      }

      renderOperatorTitle();
      return;
    }

    if (_operatorTitleState.active === 0) {
      cancelIdleTimer();
      renderOperatorTitle();
      return;
    }

    // Do not let one transient zero sample erase an active logical transfer.
    // If queued/downloading work reappears before the delay expires, the timer
    // is cancelled above. Otherwise the title settles to idle promptly.
    if (idleTimer === null) {
      idleTimer = setTimeout(function () {
        idleTimer = null;
        if (latestLogicalActive === 0) {
          _operatorTitleState.active = 0;
          _operatorTitleState.progress = 0;
          renderOperatorTitle();
        }
      }, IDLE_CONFIRM_MS);
    }

    renderOperatorTitle();
  };
})();
