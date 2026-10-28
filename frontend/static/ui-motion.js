/* DebridPulse navigation and disclosure motion: presentation only.
 *
 * Each surface keeps its own navigation and disclosure state; this module only
 * moves what that owner commits. It never decides what is shown, holds no page,
 * tab, view or disclosure state, fetches nothing and renders nothing.
 *
 * slide() moves a surface from what it showed to what its owner's commit
 * puts there, with the Web Animations API, the same way in every engine that
 * has it: what it showed leaves one way while the real, already committed
 * content enters from the other, both clipped to the surface. What leaves
 * depends on what the owner's commit does with it. Where the commit only
 * hides it (a sidebar view, a Settings or Help panel: KEEPS), that real
 * element is kept on screen for the slide -- inert, hidden from assistive
 * technology, marked data-dp-motion-outgoing and never made the owner's
 * selection again -- and then released. Where the commit replaces it (a
 * navigator's window, a page of rows), a still copy of the surface is made
 * first and stands beside it over the same box -- inert, hidden from
 * assistive technology, without IDs, form names or any secret value. Content
 * the surface gains while it moves (rows, a resolve() still) joins the motion
 * where it is. A newer slide of the same surface ends the one still running
 * first. Everything outside the surface stays still and live. With reduced
 * motion, or without the Web Animations API, the commit simply runs at once.
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

  /* Surfaces whose owner's commit only hides what they showed, which is
   * therefore kept and moved itself (its IDs, styling, canvases and form
   * state are the real ones): the main view region (app.js navigateTo()
   * moves .active between its views) and the Settings and Help panels
   * (chooseTab() sets [hidden]), one wrapper deep. */
  const KEEPS = '#content, #view-settings .dp-settings-scroll, #view-help .dp-help-scroll';
  // Where a surface's alternatives stand: itself, or its only child.
  const level = surface => (surface.children.length === 1 ? surface.firstElementChild : surface);
  /* What an outgoing copy never carries: every secret DP draws -- a password
   * field (provider API keys, account, Usenet and webhook credentials, the
   * OIDC client secret), the archive-password editor and its source, and a
   * one-time token disclosure. Each is copied as an empty field of its kind,
   * the same shape, created anew rather than cloned, so no value, attribute or
   * text of it reaches the copy. */
  const SECRET = 'input[type="password"], .dp-settings-extraction-password-editor :is(input, textarea), '
    + 'textarea[data-setting="extraction_password"], .dp-token-disclosure input';
  const SHAPE = ['class', 'style', 'type', 'rows', 'cols', 'size', 'readonly', 'disabled'];
  const blank = source => {
    const made = document.createElement(source.localName);
    SHAPE.forEach(name => { if (source.hasAttribute(name)) made.setAttribute(name, source.getAttribute(name)); });
    return made;
  };

  /* An inert copy of `node` as drawn: no IDs and no form names (so it is
   * never a lookup's answer or a member of a radio group), a secret only as
   * its empty shape, canvas pixels where the canvas allows, the selection of
   * each select, and -- once dpMotionPlace() is called with the copy in the
   * document -- each scrolled descendant's offset. What is not drawn at all
   * (display: none, such as the panels not shown) is left out, and so is
   * anything already moving in a copy of its own (a .dp-motion-still). */
  function copy(node) {
    const scrolled = [];
    const walk = source => {
      if (source.nodeType !== Node.ELEMENT_NODE) return source.cloneNode(false);
      if (source.classList.contains('dp-motion-still')) return null;
      // (A closed select draws its options without boxes of their own.)
      if (!source.getClientRects().length && !source.closest('select') && getComputedStyle(source).display === 'none') return null;
      if (source.matches(SECRET)) return blank(source);
      const made = source.cloneNode(false);
      made.removeAttribute('id');
      if ('form' in made && made.localName !== 'form') { made.removeAttribute('name'); made.removeAttribute('form'); }
      for (const child of source.childNodes) {
        const part = walk(child);
        if (part) made.appendChild(part);
      }
      if (source instanceof HTMLCanvasElement) {
        try { made.getContext('2d').drawImage(source, 0, 0); } catch (_) {}   // a tainted canvas stays blank
      } else if (source instanceof HTMLSelectElement) {
        Array.from(source.options).forEach((option, index) => { if (made.options[index]) made.options[index].selected = option.selected; });
      }
      if (source.scrollTop || source.scrollLeft) scrolled.push([made, source.scrollTop, source.scrollLeft]);
      return made;
    };
    const made = walk(node);
    made.inert = true;
    made.setAttribute('aria-hidden', 'true');
    // Called once the copy is in the document, where it can scroll.
    made.dpMotionPlace = () => scrolled.forEach(([part, top, left]) => { part.scrollTop = top; part.scrollLeft = left; });
    return made;
  }

  // The slide each surface is making, so a newer one ends it first and a
  // resolve() still knows where the content it covers is drawn at rest.
  const flights = new WeakMap();
  const drawn = node => node.getClientRects().length > 0 && !node.classList.contains('dp-motion-still');

  /* Run `commit` as one slide of the surface `element` along `axis` ('x' or
   * 'y'): `direction` +1 for a later destination, -1 for an earlier one (0 or
   * the same place: no motion), `distance` the number of positions it moves
   * (a farther destination travels longer and farther). Only that surface
   * moves; the rest of the page stays live and takes input throughout. The
   * commit runs once, at once: `done` resolves once it has run (the DOM holds
   * the destination); `settled` once the motion has finished, was skipped or
   * was superseded, with everything it put in place gone; `skip()` ends the
   * motion at once, keeping the committed destination. */
  function slide({element, axis = 'x', direction = 0, distance = 1}, commit) {
    const previous = element ? flights.get(element) : null;
    previous?.end();
    const idle = {done: Promise.resolve(), settled: Promise.resolve(), skip() {}};
    if (!element || !direction || reduced() || typeof element.animate !== 'function') {
      commit();
      return idle;
    }
    const keeps = element.matches(KEEPS);
    // What is on screen now -- or, when the slide this one ends has not been
    // drawn even once (a second choice in the same task or frame), what that
    // one was leaving, which is still what is on screen.
    const unpainted = previous && !previous.painted;
    let ghost = null, parts = [], before = null, shown = [];
    if (keeps) {
      shown = unpainted ? previous.shown : Array.from(level(element).children).filter(drawn)
        .map(node => ({node, box: node.getBoundingClientRect(), display: getComputedStyle(node).display}));
    } else if (unpainted) {
      ({ghost, parts, before} = previous);
    } else {
      // Copied before the owner replaces it.
      before = {top: element.scrollTop, left: element.scrollLeft};
      ghost = element.cloneNode(false);
      ghost.removeAttribute('id');
      ghost.removeAttribute('aria-live');
      ghost.classList.add('dp-motion-still');
      ghost.inert = true;
      ghost.setAttribute('aria-hidden', 'true');
      parts = Array.from(element.children).filter(drawn).map(copy);
      ghost.append(...parts);
    }

    commit();
    if (!element.isConnected || !element.parentNode) return idle;
    // Kept: what was on screen and the commit hid. None (the destination is
    // what is already on screen): nothing moves.
    const host = keeps ? level(element) : element;
    const outgoing = shown.filter(({node}) => node.parentElement === host && !node.getClientRects().length);
    if (keeps && !outgoing.length) return idle;

    // Everything the slide puts in place, released by end() whether the slide
    // finishes, is skipped or superseded, or fails to start.
    const moving = new Map();                       // live node -> its animation
    const leaving = [];
    const held = [];                                // [element, property, value, priority]
    const marked = [];                              // [element, inert, aria-hidden]
    const unstyled = new Set();                     // elements that had no style attribute
    const hold = (node, property, value) => {
      if (!node.hasAttribute('style')) unstyled.add(node);
      held.push([node, property, node.style.getPropertyValue(property), node.style.getPropertyPriority(property)]);
      node.style.setProperty(property, value, 'important');
    };
    let join = () => {};
    // Whatever the surface gains while it moves joins the motion where it is.
    const observer = new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(node => {
      if (node instanceof Element && node.parentNode === host && !moving.has(node)) join(node);
    })));
    let settle;
    const settled = new Promise(resolve => { settle = resolve; });
    const flight = {
      end() {
        if (flight.ended) return;
        flight.ended = true;
        observer.disconnect();
        moving.forEach(animation => animation.cancel());
        leaving.forEach(animation => animation.cancel());
        ghost?.remove();
        // A kept element goes back to exactly what its owner made it.
        marked.forEach(([node, inert, hidden]) => {
          node.inert = inert;
          if (hidden === null) node.removeAttribute('aria-hidden');
          else node.setAttribute('aria-hidden', hidden);
          delete node.dataset.dpMotionOutgoing;
        });
        held.reverse().forEach(([node, property, value, priority]) => {
          if (value) node.style.setProperty(property, value, priority);
          else node.style.removeProperty(property);
        });
        unstyled.forEach(node => { if (!node.getAttribute('style')) node.removeAttribute('style'); });
        if (flights.get(element) === flight) flights.delete(element);
        element.classList.remove('dp-motion-surface');
        delete element.dataset.dpMotionAxis;
        delete element.dataset.dpMotionDirection;
        delete element.dataset.dpMotionStep;
        settle();
      },
      moving,
      join: node => join(node),
      ghost,
      parts,
      before,
      shown,
      painted: false,
    };
    try {
      element.dataset.dpMotionAxis = axis;
      element.dataset.dpMotionDirection = direction > 0 ? 'forward' : 'backward';
      element.dataset.dpMotionStep = reach(Math.abs(distance));
      element.classList.add('dp-motion-surface');
      const token = name => getComputedStyle(element).getPropertyValue(name).trim();
      const duration = parseFloat(token('--dp-motion-duration')) || TIMING[element.dataset.dpMotionStep];
      const fade = Number(token('--dp-motion-fade')) || 1;
      const easing = token('--dp-motion-curve') || 'cubic-bezier(0.4, 0, 0.2, 1)';

      /* Both presentations are clipped to the surface. A surface that does not
       * clip (the narrow Settings and Help pages) clips for the slide; one that
       * would scroll only because something is moving or kept in it (a
       * classic scrollbar appearing mid-slide) does not for the slide; and a
       * kept element is placed in the surface itself, scrolling with it. */
      const style = getComputedStyle(element);
      const clips = style.overflowX !== 'visible' || style.overflowY !== 'visible';
      if (!clips) hold(element, 'overflow', 'clip');
      if (clips && style.overflowX === 'auto' && element.scrollWidth <= element.clientWidth) hold(element, 'overflow-x', 'hidden');
      if (clips && style.overflowY === 'auto' && element.scrollHeight <= element.clientHeight) hold(element, 'overflow-y', 'hidden');
      if (keeps && style.position === 'static') hold(element, 'position', 'relative');

      const place = (node, box) => {
        hold(node, 'left', '0px');
        hold(node, 'top', '0px');
        const origin = node.getBoundingClientRect();
        node.style.setProperty('left', `${box.left - origin.left}px`, 'important');
        node.style.setProperty('top', `${box.top - origin.top}px`, 'important');
      };
      if (keeps) {
        // The kept element, drawn where it was and as it was: its view or
        // panel styling follows data-dp-motion-outgoing (never .active or a
        // selection), and it takes no input and says nothing.
        outgoing.forEach(({node, box, display}) => {
          marked.push([node, node.inert, node.getAttribute('aria-hidden')]);
          node.dataset.dpMotionOutgoing = '';
          node.inert = true;
          node.setAttribute('aria-hidden', 'true');
          hold(node, 'display', display);
          hold(node, 'position', 'absolute');
          hold(node, 'box-sizing', 'border-box');
          hold(node, 'width', `${box.width}px`);
          hold(node, 'height', `${box.height}px`);
          hold(node, 'margin', '0px');
          hold(node, 'pointer-events', 'none');
          place(node, box);
        });
      } else {
        // The copy stands over the surface's box, scrolled where the surface was.
        const box = element.getBoundingClientRect();
        Object.assign(ghost.style, {boxSizing: 'border-box', width: `${box.width}px`, height: `${box.height}px`,
                                    minWidth: '0', maxWidth: 'none', minHeight: '0', maxHeight: 'none',
                                    left: '0px', top: '0px', right: 'auto', bottom: 'auto', transform: 'none'});
        if (!clips) ghost.style.overflow = 'clip';
        // Out of flow whatever the surface's own classes say of its position.
        ghost.style.setProperty('position', 'absolute', 'important');
        element.parentNode.insertBefore(ghost, element.nextSibling);
        const origin = ghost.getBoundingClientRect();
        ghost.style.left = `${box.left - origin.left}px`;
        ghost.style.top = `${box.top - origin.top}px`;
        ghost.scrollTop = before.top;
        ghost.scrollLeft = before.left;
        parts.forEach(part => part.dpMotionPlace());
      }

      // Forward: the old content leaves toward the start (left, or up) and the
      // destination arrives from the end (right, or below); backward: the
      // reverse. Travel is a share of the surface's own viewport.
      const travel = (parseFloat(token('--dp-motion-travel')) || 100) / 100
        * (axis === 'y' ? element.clientHeight : element.clientWidth) * (direction > 0 ? 1 : -1);
      const toward = px => (axis === 'y' ? `translateY(${px}px)` : `translateX(${px}px)`);
      const timing = {duration, easing, fill: 'both'};
      const leave = [{transform: 'none', opacity: 1}, {transform: toward(-travel), opacity: fade}];
      const enter = [{transform: toward(travel), opacity: fade}, {transform: 'none', opacity: 1}];

      let lead = null;
      join = node => {
        moving.get(node)?.cancel();
        const animation = node.animate(enter, timing);
        if (lead) {
          if (lead.startTime !== null) animation.startTime = lead.startTime;
          else animation.currentTime = lead.currentTime || 0;
        }
        moving.set(node, animation);
        if (flight.waiting) animation.pause();
      };
      const old = keeps ? outgoing.map(({node}) => node) : parts;
      leaving.push(...old.map(node => node.animate(leave, timing)));
      lead = leaving[0] || null;
      Array.from(host.children).filter(node => drawn(node) && !node.hasAttribute('data-dp-motion-outgoing')).forEach(join);
      lead ||= moving.values().next().value || null;
      observer.observe(host, {childList: true});
      /* The travel waits at its start until this prepared presentation has
       * been drawn once: an engine that must first lay out and paint what
       * leaves and what arrives would otherwise spend the travel's clock
       * before any of it is shown (WebKitGTK spent most of it). */
      flight.waiting = true;
      [...leaving, ...moving.values()].forEach(animation => animation.pause());
    } catch (_) {
      flight.end();
      return {done: Promise.resolve(), settled, skip() {}};
    }
    flights.set(element, flight);
    // Drawn from the first frame it is part of (requestAnimationFrame runs just
    // before that frame is painted, and nothing else runs between the two);
    // the travel starts with the next frame, once that one is on screen.
    requestAnimationFrame(() => {
      flight.painted = true;
      requestAnimationFrame(() => {
        if (flight.ended) return;
        flight.waiting = false;
        [...leaving, ...moving.values()].forEach(animation => animation.play());
      });
    });
    Promise.all([...leaving, ...moving.values()].map(animation => animation.finished.catch(() => {}))).then(() => flight.end());
    return {done: Promise.resolve(), settled, skip: () => flight.end()};
  }

  /* A page turn's authoritative content arriving where its placeholder
   * stands: `commit` replaces the placeholder `region` (a table body, or an
   * element) inside the surface `host`, and the two crossfade in place -- a
   * still copy of the placeholder, drawn exactly where it was, fades out over
   * `content()` (what the commit put there) fading in. Nothing moves of its
   * own: a slide of `host` still running carries both, the still joining it
   * where it is (slide()). Reduced motion, or no placeholder on screen: the
   * commit, at once. Resolves once the crossfade has ended. */
  function resolve({region, host, content}, commit) {
    if (!region || !host || !region.isConnected || reduced() || typeof host.animate !== 'function') {
      commit();
      return Promise.resolve();
    }
    const ghost = still(region, host);
    commit();
    host.appendChild(ghost);                       // after the commit, which may empty the host
    ghost.dpMotionPlace();
    const nodes = (content() || []).filter(Boolean);
    // Under a slide of `host`, both join it first, so this crossfade -- the
    // newer animation -- is the one their opacity follows.
    const flight = flights.get(host);
    if (flight) [ghost, ...nodes].forEach(node => { if (node.parentNode === host && !flight.moving.has(node)) flight.join(node); });
    const timing = {duration: TIMING.hydrate, easing: 'cubic-bezier(0.4, 0, 0.2, 1)'};
    nodes.forEach(node => node.animate([{opacity: 0}, {opacity: 1}], timing));
    return ghost.animate([{opacity: 1}, {opacity: 0}], {...timing, fill: 'forwards'}).finished
      .catch(() => {}).then(() => ghost.remove());
  }
  // A still, inert copy of `region` (copy()), placed (in `host`'s content box)
  // where it is drawn at rest -- under a slide of `host` still running, where
  // the slide is carrying it to, since the still joins that slide. A table
  // body keeps its columns: it is copied into a copy of its table, each
  // column at the width it is drawn.
  function still(region, host) {
    let ghost;
    if (region instanceof HTMLTableSectionElement) {
      const body = copy(region);
      ghost = region.closest('table').cloneNode(false);
      ghost.removeAttribute('id');
      ghost.inert = true;
      ghost.setAttribute('aria-hidden', 'true');
      ghost.dpMotionPlace = body.dpMotionPlace;
      const widths = region.rows[0] ? Array.from(region.rows[0].cells, cell => cell.getBoundingClientRect().width) : [];
      if (body.rows[0]) Array.from(body.rows[0].cells).forEach((cell, index) => { cell.style.width = `${widths[index]}px`; });
      ghost.style.tableLayout = 'fixed';
      ghost.append(body);
    } else {
      ghost = copy(region);
    }
    ghost.classList.add('dp-motion-still');
    const at = region.getBoundingClientRect(), frame = host.getBoundingClientRect();
    let carrier = region;
    while (carrier.parentElement && carrier.parentElement !== host) carrier = carrier.parentElement;
    const moved = flights.get(host)?.moving.has(carrier) ? new DOMMatrixReadOnly(getComputedStyle(carrier).transform) : null;
    Object.assign(ghost.style, {
      left: `${at.left - frame.left - host.clientLeft + host.scrollLeft - (moved?.m41 || 0)}px`,
      top: `${at.top - frame.top - host.clientTop + host.scrollTop - (moved?.m42 || 0)}px`,
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
