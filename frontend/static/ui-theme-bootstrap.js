/* DebridPulse first-paint theme bootstrap.
 * Required application modules are normal parser-deferred dependencies.
 */
(function () {
  'use strict';
  try {
    if (localStorage.getItem('theme') === 'light') document.body.classList.add('light');
  } catch (_) {}
})();
