/* DownloadPulse desktop starting cover (window.DPDesktopStart): the one owner of
 * the installed desktop application's starting cover.
 *
 * In the desktop application the window opens on the application's page as
 * soon as its backend answers, and the page then assembles itself (settings,
 * figures, lists, the first-run wizard) piece by piece. This owner covers
 * that: the cover (static markup in index.html, #dp-desktop-start: the
 * product's mark, title and "Starting DownloadPulse", in the theme and language
 * in force) is shown from the first paint and lifted once the interface says
 * it is ready (app.js dispatches 'downloadpulse:interface-ready' once).
 *
 * Only in the desktop application: the server says so before the first paint
 * (window.downloadPulseDesktop, prepended to /ui-theme-bootstrap.js). A server
 * installation opened in a browser never shows it. This parser-blocking
 * script runs right after ui-i18n.js, before the body is parsed, and only
 * marks the root (html[data-dp-desktop-starting], ui-desktop-start.css): the
 * cover is drawn by CSS from the first frame, with no script waiting on it.
 *
 * Shown at least MIN_SHOWN_MS from the page's start (performance.now(), the
 * navigation's own clock): an interface ready sooner waits out the rest, one
 * ready later lifts it at once -- never a fixed wait added to a slow start.
 *
 * Its words are drawn once, in the face they are set in: the title and the
 * message wait (keeping their place; their line heights are fixed, so the
 * card's size does not depend on the face) until Outfit has loaded -- the
 * face arrives from the network after the first paint, and words drawn in a
 * fallback first would visibly change size when it came -- or for FACE_WAIT_MS
 * at most, after which they show in whatever face there is.
 *
 * Never a trap: should the interface never say it is ready, the cover lifts
 * by itself after LIFT_AFTER_MS and the reason is kept in
 * DPDesktopStart.diagnostics(). The lift fades (ui-desktop-start.css), at once
 * under reduced motion. Its text is a catalog message (desktop.starting),
 * rendered by ui-i18n.js with the rest of the page: never English first.
 */
(function () {
  'use strict';

  const STARTING = 'data-dp-desktop-starting';
  const LIFTING = 'data-dp-desktop-lifting';
  const LETTERED = 'data-dp-desktop-lettered';
  const FACE_WAIT_MS = 1000;
  const READY_EVENT = 'downloadpulse:interface-ready';
  const MIN_SHOWN_MS = 3000;
  const LIFT_AFTER_MS = 20000;
  const FADE_MS = 180;
  const root = document.documentElement;
  const problems = [];
  let lifted = null;
  let failsafe = null;

  if (window.downloadPulseDesktop !== true) {
    // Not the desktop application: the cover is never drawn (no root mark),
    // and its markup leaves the page once parsed.
    document.addEventListener('DOMContentLoaded', () => document.getElementById('dp-desktop-start')?.remove(),
                              {once: true});
    window.DPDesktopStart = Object.freeze({active: false, lifted: Promise.resolve('absent'),
                                           diagnostics: () => problems.slice()});
    return;
  }

  root.setAttribute(STARTING, '');
  // The words show once the face their own style names has loaded (each
  // weight and size they are set in), or at FACE_WAIT_MS; a page with no
  // such face (offline) shows them at once. Read once they are parsed.
  const lettered = () => root.setAttribute(LETTERED, '');
  const letter = () => {
    const faces = [...document.querySelectorAll('.dp-desktop-start-brand, .dp-desktop-start-text')].map(node => {
      const style = getComputedStyle(node);
      return `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
    });
    Promise.race([
      document.fonts ? Promise.all(faces.map(face => document.fonts.load(face))) : Promise.resolve(),
      new Promise(resolve => setTimeout(resolve, FACE_WAIT_MS)),
    ]).then(lettered, lettered);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', letter, {once: true});
  else letter();
  let resolveLifted;
  lifted = new Promise(resolve => { resolveLifted = resolve; });

  function lift(reason) {
    if (!root.hasAttribute(STARTING) || root.hasAttribute(LIFTING)) return;
    clearTimeout(failsafe);
    const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const done = () => {
      root.removeAttribute(LIFTING);
      root.removeAttribute(STARTING);
      document.getElementById('dp-desktop-start')?.remove();
      resolveLifted(reason);
    };
    if (still) { done(); return; }
    root.setAttribute(LIFTING, '');
    setTimeout(done, FADE_MS);
  }

  failsafe = setTimeout(() => {
    problems.push({kind: 'not-ready', event: READY_EVENT, waitedMs: LIFT_AFTER_MS});
    lift('failsafe');
  }, LIFT_AFTER_MS);
  document.addEventListener(READY_EVENT, () => {
    setTimeout(() => lift('ready'), Math.max(0, MIN_SHOWN_MS - performance.now()));
  }, {once: true});

  window.DPDesktopStart = Object.freeze({active: true, lifted, diagnostics: () => problems.slice()});
})();
