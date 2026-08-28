/* DebridPulse v1.0.11 Statistics lifecycle orchestrator.
 *
 * This is the sole presentation wrapper around app.js loadDetailedStats. Page
 * layers subscribe to one post-render event instead of wrapping each other.
 * Statistics data/API ownership remains in app.js and the backend.
 */
(function () {
  'use strict';

  const EVENT_NAME = 'debridpulse:statistics-rendered';

  function selectedPeriod(explicit) {
    if (explicit) return explicit;
    const active = document.querySelector('#stats-period-tabs .ftab.active');
    return (active && active.dataset.period) || '7d';
  }

  function formatCompactDuration(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value <= 0) return '—';

    const totalMinutes = Math.max(1, Math.round(value / 60));
    const days = Math.floor(totalMinutes / 1440);
    const hours = Math.floor((totalMinutes % 1440) / 60);
    const minutes = totalMinutes % 60;
    const parts = [];

    if (days) parts.push(days + 'D');
    if (hours) parts.push(hours + 'H');
    if (minutes) parts.push(minutes + 'M');
    return parts.join(' ');
  }

  function install() {
    const previous = window.loadDetailedStats;
    if (typeof previous !== 'function') {
      console.error('[DebridPulse] Statistics lifecycle not installed: loadDetailedStats is unavailable.');
      return false;
    }
    if (previous.dpStatisticsOrchestrator === '1') return true;

    const wrapped = async function (period) {
      const resolved = selectedPeriod(period);
      const result = await previous.call(this, resolved);
      document.dispatchEvent(new CustomEvent(EVENT_NAME, {
        detail: {period: resolved}
      }));
      return result;
    };
    wrapped.dpStatisticsOrchestrator = '1';
    window.loadDetailedStats = wrapped;
    window.fmtDuration = formatCompactDuration;
    return true;
  }

  window.DPStatisticsLifecycle = Object.freeze({
    event: EVENT_NAME,
    install: install,
  });

  install();
})();
