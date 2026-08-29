/* DebridPulse v1.0.11 deterministic presentation-runtime loader. */
(function () {
  'use strict';

  const RUNTIMES = Object.freeze([
    {src: '/ui-shell-runtime.js?v=1', marker: 'data-dp-shell-runtime'},
    {src: '/ui-visual-behavior-fixes.js?v=23', marker: 'data-dp-visual-behavior-fixes'},
    {src: '/ui-statistics-orchestrator.js?v=1', marker: 'data-dp-statistics-orchestrator'},
    {src: '/ui-statistics-batch3.js?v=3', marker: 'data-dp-statistics-batch3'},
    {src: '/ui-statistics-batch4.js?v=2', marker: 'data-dp-statistics-batch4'},
    {src: '/ui-statistics-batch5.js?v=7', marker: 'data-dp-statistics-batch5'},
    {src: '/ui-settings-page.js?v=5', marker: 'data-dp-settings-page'},
    {src: '/ui-error-semantics.js?v=21', marker: 'data-dp-error-semantics'},
  ]);

  function loadRuntime(index) {
    if (index >= RUNTIMES.length) return;
    const runtime = RUNTIMES[index];
    if (document.querySelector('script[' + runtime.marker + ']')) {
      loadRuntime(index + 1);
      return;
    }

    const script = document.createElement('script');
    script.src = runtime.src;
    script.async = false;
    script.setAttribute(runtime.marker, '1');
    script.addEventListener('load', function () { loadRuntime(index + 1); }, {once: true});
    script.addEventListener('error', function () {
      console.error('[DebridPulse] failed to load presentation runtime:', runtime.src);
      loadRuntime(index + 1);
    }, {once: true});
    document.head.appendChild(script);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { loadRuntime(0); }, {once: true});
  } else {
    loadRuntime(0);
  }
})();
