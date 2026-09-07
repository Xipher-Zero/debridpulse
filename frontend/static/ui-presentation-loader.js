/* DebridPulse v1.0.12 canonical presentation boot graph. Ordering only: no API I/O or DOM repair. */
(function () {
  'use strict';
  const RUNTIMES = Object.freeze([
    {src:'/ui-toast-contract.js?v=2', marker:'DPToastContract'},
    {src:'/ui-dashboard-transfer-presentation.js?v=1', marker:'DPDashboardTransferPresentation'},
    {src:'/ui-downloads-presentation.js?v=1', marker:'DPDownloadsPresentation'},
    {src:'/ui-processing-presentation.js?v=1', marker:'DPProcessingPresentation'},
    {src:'/ui-activity-log-runtime.js?v=1', marker:'DPActivityLog'},
    {src:'/ui-settings-archive-passwords.js?v=1', marker:'DPArchivePasswords'},
  ]);
  function loadAt(index) {
    if (index >= RUNTIMES.length) {
      document.dispatchEvent(new CustomEvent('debridpulse:presentation-ready'));
      return;
    }
    const definition = RUNTIMES[index];
    if (window[definition.marker]) { loadAt(index + 1); return; }
    const path = definition.src.split('?')[0];
    const existing = Array.from(document.scripts).find(node => {
      try { return new URL(node.src, location.href).pathname === path; } catch (_) { return false; }
    });
    if (existing) {
      existing.addEventListener('load', () => loadAt(index + 1), {once:true});
      return;
    }
    const script = document.createElement('script');
    script.src = definition.src;
    script.async = false;
    script.dataset.dpPresentationOwner = definition.marker;
    script.addEventListener('load', () => loadAt(index + 1), {once:true});
    script.addEventListener('error', () => {
      console.error('Unable to load presentation owner:', definition.src);
      loadAt(index + 1);
    }, {once:true});
    document.head.appendChild(script);
  }
  window.DPPresentationLoader = Object.freeze({runtimes:RUNTIMES.map(({src,marker}) => Object.freeze({src,marker}))});
  loadAt(0);
})();