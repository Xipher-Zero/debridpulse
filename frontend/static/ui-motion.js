/* DebridPulse navigation and disclosure motion: presentation only.
 *
 * Each surface keeps its own navigation and disclosure state; this module only
 * moves what that owner commits. It never decides what is shown, holds no page,
 * tab, view or disclosure state, fetches nothing and renders nothing.
 *
 * slide() runs an owner's synchronous commit inside a view transition scoped
 * to that surface's element: the old content is a snapshot leaving one way
 * while the real, already laid-out destination enters from the other, clipped
 * to the surface (ui-motion.css). The live DOM is never transformed, so
 * anything that measures it (the Dashboard heartbeat route, Chart.js) reads
 * settled geometry throughout, and everything outside the surface stays live.
 * A newer slide of the same surface supersedes one still running; the commit
 * is the owner's, so the owner decides what the latest intent is. Without
 * element-scoped view transitions, or with reduced motion, the commit simply
 * runs at once.
 *
 * disclose() opens or closes a disclosure body by animating its real height,
 * so the content around it moves with it; the owner keeps aria-expanded and any
 * remembered choice, and decides whether a change is the operator's (animated)
 * or a reconciliation (immediate). */
(function () {
  'use strict';

  const reducedMotion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : {matches: false};
  const reduced = () => reducedMotion.matches;
  // The one motion vocabulary (ui-motion.css holds the same values as tokens).
  const TIMING = Object.freeze({adjacent: 220, jump: 170, expand: 250, collapse: 200});

  // Per surface: the newest commit asked for. An older one still waiting for
  // its transition to start is dropped, on its own surface only.
  const latest = new WeakMap();
  let sequence = 0;

  /* Run `commit` as one slide of the surface `element` along `axis` ('x' or
   * 'y'): `direction` +1 for a later destination, -1 for an earlier one (0 or
   * the same place: no motion), `jump` when it skips over steps. The view
   * transition is scoped to that element, so only it is captured and only it
   * moves; the rest of the page stays live and takes input throughout.
   * `done` resolves once the commit has run (the DOM holds the destination);
   * `settled` once the motion has finished or was superseded. */
  function slide({element, axis = 'x', direction = 0, jump = false}, commit) {
    const token = ++sequence;
    if (element) latest.set(element, token);
    if (!element || !direction || reduced() || typeof element.startViewTransition !== 'function') {
      commit();
      return {done: Promise.resolve(), settled: Promise.resolve()};
    }
    element.dataset.dpMotionAxis = axis;
    element.dataset.dpMotionDirection = direction > 0 ? 'forward' : 'backward';
    element.dataset.dpMotionStep = jump ? 'jump' : 'adjacent';
    element.classList.add('dp-motion-surface');
    const transition = element.startViewTransition(() => {
      // A newer slide of this surface took over before this commit ran: the
      // owner's latest intent is committed by that one, never twice.
      if (latest.get(element) === token) commit();
    });
    // A slide superseded by a newer one is skipped by the browser: that is
    // the intended outcome, not an error.
    transition.ready.catch(() => {});
    const settled = transition.finished.catch(() => {}).then(() => {
      if (latest.get(element) !== token) return;
      element.classList.remove('dp-motion-surface');
      delete element.dataset.dpMotionAxis;
      delete element.dataset.dpMotionDirection;
      delete element.dataset.dpMotionStep;
    });
    return {done: transition.updateCallbackDone, settled};
  }

  // The disclosure animation running on each body, so a reversal starts from
  // wherever the body is drawn now.
  const running = new WeakMap();
  const BLOCK = ['height', 'paddingTop', 'paddingBottom', 'marginTop', 'marginBottom'];

  function box(body) {
    const style = getComputedStyle(body);
    const frame = {height: `${body.getBoundingClientRect().height}px`};
    BLOCK.slice(1).forEach(property => { frame[property] = style[property]; });
    return frame;
  }
  /* A body with no height at all. In a grid or column flex parent, the gap
   * before it exists as soon as it is laid out, so it starts (and ends) that
   * much higher -- nothing below it moves until it really opens. */
  function closed(body) {
    const frame = Object.fromEntries(BLOCK.map(property => [property, '0px']));
    const parent = body.parentElement && getComputedStyle(body.parentElement);
    const stacked = parent && (parent.display.includes('grid')
      || (parent.display.includes('flex') && parent.flexDirection.startsWith('column')));
    const gap = stacked && body.previousElementSibling ? parseFloat(parent.rowGap) || 0 : 0;
    if (gap) frame.marginTop = `${-gap}px`;
    return frame;
  }

  /* Show (`expanded`) or put away a disclosure body. Animated only when the
   * owner says the operator asked for it; otherwise the final state at once.
   * A closing body is inert from the first frame (focus inside it returns to
   * `focusReturn`) and hidden at the last; an opening one is usable at once. */
  function disclose(body, expanded, {animate = false, focusReturn = null, onClosed = null} = {}) {
    if (!body) return;
    const previous = running.get(body);
    const from = previous ? box(body) : null;      // where an interrupted motion has it drawn
    if (previous) { previous.cancel(); running.delete(body); }
    const finish = () => {
      body.style.overflow = '';
      body.inert = false;
      running.delete(body);
    };
    if (expanded) {
      body.inert = false;
      body.hidden = false;
      if (!animate || reduced()) { finish(); return; }
      const to = box(body);
      body.style.overflow = 'hidden';
      const motion = body.animate([from || closed(body), to], {duration: TIMING.expand, easing: 'cubic-bezier(0.22, 1, 0.36, 1)'});
      running.set(body, motion);
      motion.onfinish = finish;
      return;
    }
    if (body.hidden) { finish(); return; }
    if (focusReturn && body.contains(document.activeElement)) focusReturn.focus();
    const done = () => {
      body.hidden = true;
      finish();
      if (onClosed) onClosed();
    };
    if (!animate || reduced()) { done(); return; }
    body.inert = true;
    body.style.overflow = 'hidden';
    const motion = body.animate([from || box(body), closed(body)], {duration: TIMING.collapse, easing: 'cubic-bezier(0.4, 0, 0.6, 1)'});
    running.set(body, motion);
    motion.onfinish = done;
  }

  window.DPMotion = Object.freeze({slide, disclose, reduced, TIMING});
})();
