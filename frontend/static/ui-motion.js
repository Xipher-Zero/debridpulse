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
 * A page turn slides at once, before its page is fetched: the owner's commit
 * draws a placeholder shaped like the destination (or the destination itself,
 * when its answer is already in), and resolve() crossfades the placeholder
 * into the authoritative page whenever it arrives -- during the slide, which
 * carries on unchanged, or after it, where the placeholder waits still.
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
  const TIMING = Object.freeze({adjacent: 280, short: 380, long: 450, expand: 250, collapse: 200,
                                hydrate: 140, modalEnter: 200, modalExit: 160});
  // How far a navigation goes, by how many positions it moves: the next one,
  // two or three, or four and more.
  const reach = distance => (distance >= 4 ? 'long' : distance >= 2 ? 'short' : 'adjacent');

  // Per surface: the newest commit asked for. An older one still waiting for
  // its transition to start is dropped, on its own surface only.
  const latest = new WeakMap();
  let sequence = 0;

  /* Run `commit` as one slide of the surface `element` along `axis` ('x' or
   * 'y'): `direction` +1 for a later destination, -1 for an earlier one (0 or
   * the same place: no motion), `distance` the number of positions it moves
   * (a farther destination travels longer and farther). The view
   * transition is scoped to that element, so only it is captured and only it
   * moves; the rest of the page stays live and takes input throughout.
   * `done` resolves once the commit has run (the DOM holds the destination);
   * `settled` once the motion has finished or was superseded; `skip()` ends
   * the motion at once (the turn it was making did not happen). */
  function slide({element, axis = 'x', direction = 0, distance = 1}, commit) {
    const token = ++sequence;
    if (element) latest.set(element, token);
    if (!element || !direction || reduced() || typeof element.startViewTransition !== 'function') {
      commit();
      return {done: Promise.resolve(), settled: Promise.resolve(), skip() {}};
    }
    element.dataset.dpMotionAxis = axis;
    element.dataset.dpMotionDirection = direction > 0 ? 'forward' : 'backward';
    element.dataset.dpMotionStep = reach(Math.abs(distance));
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
    return {done: transition.updateCallbackDone, settled, skip: () => transition.skipTransition()};
  }

  /* A page turn's authoritative content arriving where its placeholder
   * stands: `commit` replaces the placeholder `region` (a table body, or an
   * element) inside the surface `host`, and the two crossfade in place -- a
   * still copy of the placeholder, drawn exactly where it was, fades out over
   * `content()` (what the commit put there) fading in. Nothing moves, so a
   * slide still running carries both on its own timeline. Reduced motion, or
   * no placeholder on screen: the commit, at once. Resolves once the
   * crossfade has ended. */
  function resolve({region, host, content}, commit) {
    if (!region || !host || !region.isConnected || reduced() || typeof host.animate !== 'function') {
      commit();
      return Promise.resolve();
    }
    const ghost = still(region, host);
    commit();
    host.appendChild(ghost);                       // after the commit, which may empty the host
    const timing = {duration: TIMING.hydrate, easing: 'cubic-bezier(0.4, 0, 0.2, 1)'};
    (content() || []).forEach(node => node && node.animate([{opacity: 0}, {opacity: 1}], timing));
    return ghost.animate([{opacity: 1}, {opacity: 0}], {...timing, fill: 'forwards'}).finished
      .catch(() => {}).then(() => ghost.remove());
  }
  // A still, inert copy of `region`, placed (in `host`'s content box) over it. A table body
  // keeps its columns: it is copied into a copy of its table, each column at
  // the width it is drawn.
  function still(region, host) {
    let ghost;
    if (region instanceof HTMLTableSectionElement) {
      ghost = region.closest('table').cloneNode(false);
      const body = region.cloneNode(true);
      const widths = region.rows[0] ? Array.from(region.rows[0].cells, cell => cell.getBoundingClientRect().width) : [];
      if (body.rows[0]) Array.from(body.rows[0].cells).forEach((cell, index) => { cell.style.width = `${widths[index]}px`; });
      ghost.style.tableLayout = 'fixed';
      ghost.append(body);
    } else {
      ghost = region.cloneNode(true);
    }
    [ghost, ...ghost.querySelectorAll('[id]')].forEach(node => node.removeAttribute('id'));
    ghost.classList.add('dp-motion-still');
    ghost.setAttribute('aria-hidden', 'true');
    ghost.inert = true;
    const at = region.getBoundingClientRect(), frame = host.getBoundingClientRect();
    Object.assign(ghost.style, {
      left: `${at.left - frame.left - host.clientLeft + host.scrollLeft}px`,
      top: `${at.top - frame.top - host.clientTop + host.scrollTop}px`,
      width: `${at.width}px`, height: `${at.height}px`,
    });
    return ghost;
  }

  /* A fullscreen modal opening while one of its kind (`selector`) is still
   * fading out continues that backdrop instead of stacking a second one: the
   * leaving overlay goes at once and the new one appears without fading in
   * again. Otherwise it fades in (ui-modal-contract.css). */
  function enter(overlay, selector) {
    const leaving = Array.from(document.querySelectorAll(`${selector}.is-closing`));
    leaving.forEach(node => node.remove());
    if (overlay && leaving.length) overlay.classList.add('is-continued');
  }

  /* A fullscreen modal's exit. The owner marks it closing (ui-modal-contract.css
   * fades its panel and backdrop) and keeps it mounted -- still covering the
   * page, with its own locks held -- until this resolves; only then does the
   * owner remove or hide it, release its locks and restore focus
   * (`restore`). Meanwhile its panel takes no input and the keyboard is held
   * on the leaving overlay itself, so nothing beneath it is reached.
   * `returnTo` is where focus belongs once it has gone; a modal opened during
   * the exit inherits it (`focusOrigin`). Resolves at once with reduced
   * motion or nothing to fade. */
  function leave(overlay, panel, returnTo = null) {
    if (!overlay) return Promise.resolve();
    overlay.classList.add('is-closing');
    overlay.dpReturnFocus = returnTo || null;
    const ownTabIndex = overlay.hasAttribute('tabindex');
    if (!ownTabIndex) overlay.tabIndex = -1;
    const hold = event => {
      if (!overlay.classList.contains('is-closing')) return;
      event.preventDefault();
      event.stopPropagation();
    };
    overlay.addEventListener('keydown', hold);
    if (panel) panel.inert = true;
    const active = document.activeElement;
    if (!active || active === document.body || overlay.contains(active)) overlay.focus({preventScroll: true});
    getComputedStyle(overlay).opacity;             // the closing style is now in effect
    const fading = overlay.getAnimations({subtree: true}).filter(motion => motion instanceof CSSTransition);
    return Promise.all(fading.map(motion => motion.finished.catch(() => {}))).then(() => {
      overlay.removeEventListener('keydown', hold);
      if (!ownTabIndex) overlay.removeAttribute('tabindex');
    });
  }

  /* Where a modal opening now should return focus when it closes: the page
   * control a still-leaving modal was going to return to, when focus is held
   * on that leaving modal; otherwise whatever has focus. */
  function focusOrigin() {
    const active = document.activeElement;
    const leaving = active && active.closest ? active.closest('.is-closing') : null;
    if (leaving && 'dpReturnFocus' in leaving) return leaving.dpReturnFocus;
    return active instanceof HTMLElement && active !== document.body ? active : null;
  }

  /* The end of an exit, once: the overlay is gone, and focus returns to
   * `target` only if nothing has taken it meanwhile (it is still on the body
   * or held on this overlay) -- never out of another modal. `remove` false
   * leaves a persistent overlay in place (the shared shell). */
  function restore(overlay, target, {remove = true} = {}) {
    // A dialog replaced while leaving (`enter`) handed its return target on to
    // its successor, which returns focus in its own time.
    const replaced = overlay && !overlay.isConnected;
    const active = document.activeElement;
    const free = !replaced && (!active || active === document.body || overlay.contains(active));
    if (overlay && remove) overlay.remove();
    else if (overlay && overlay.contains(document.activeElement)) document.activeElement.blur();
    if (free && target && target.isConnected && typeof target.focus === 'function') {
      try { target.focus({preventScroll: true}); } catch (_) {}
    }
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

  window.DPMotion = Object.freeze({slide, resolve, disclose, enter, leave, restore, focusOrigin, reduced, TIMING});
})();
