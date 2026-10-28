/* DebridPulse informational tooltips: the one owner.
 *
 * Every application-owned informational tooltip is declared by its own render
 * owner as `data-tooltip="text"` on the element it describes -- never `title`,
 * whose popup is the browser's -- and presented here, in DP's floating-surface
 * material (ui-components.css .dp-tooltip). One tooltip element serves the whole
 * application, in the top layer (above dialogs and outside every clipping or
 * scrolling container); its text is set as text only, never as markup.
 *
 * Shown after a 450 ms pointer hover, or at once when keyboard focus reaches
 * the element; passing from one described element to the next shows the next at
 * once. Hidden at once on leaving or blur, on Escape (until the pointer or focus
 * leaves), on a press or context menu, on scrolling, resizing or navigation, and
 * when the element leaves the document. Touch gets no hover tooltip. While shown,
 * the element is described by it (aria-describedby); its accessible name is its
 * own. A tooltip whose text is longer than its bound (an Original Resource URL)
 * scrolls, and the pointer can reach it to do so; every other tooltip lets the
 * pointer through to what lies beneath. This owns presentation only: it renders
 * nothing else and changes no element but its own and the described element's
 * aria-describedby while shown. An element whose tooltip repeats a label it
 * shows (data-tooltip-label) gets it only while that label is not shown.
 *
 * A section index (data-tooltip-index, the selector of a tablist inside the
 * described element -- the compact Settings and Help navigators) shows the
 * current section, its place and the count over every section in order, the
 * current one emphasised, read from that tablist's own tabs and selection
 * whenever it is shown or its data-tooltip changes; it is shown on keyboard
 * focus anywhere within the element, and the element's own scrolling (the
 * navigator's window) leaves it where it is.
 *
 * A described element may ask for a stationary dwell instead of the hover
 * delay (data-tooltip-dwell, milliseconds: the compact navigators' index).
 * Its tooltip is shown only once the pointer has rested over it -- moved no
 * more than JITTER -- for that long, with nothing inside it still moving;
 * never through the warm path. Moving off that rest, pressing within it
 * (choosing a section, First, Last) or anything moving inside it starts the
 * dwell again, so the tooltip appears only after a deliberate pause after the
 * last use; leaving it ends the dwell. Moving within it -- or the element's
 * content moving beneath a still pointer -- is not entering it. Keyboard
 * focus is unchanged: prompt. */
(function () {
  'use strict';

  const DELAY_MS = 450;
  const WARM_MS = 300;              // within this of the last tooltip, the next shows at once
  const JITTER = 4;                 // px a resting pointer may drift and still be at rest
  const GAP = 8;                    // px between the element and its tooltip
  const MARGIN = 8;                 // px kept clear of the viewport's edges
  const ID = 'dp-tooltip';
  const SELECTOR = '[data-tooltip]';

  let tip = null;                   // the one tooltip element
  let target = null;                // the element it describes now
  let pending = null;               // {element, timer}: a hover waiting out the delay
  let dismissed = null;             // the element Escape or a press dismissed
  let lastHidden = 0;
  let watch = null;                 // while shown: the element leaving the document hides it
  let bridging = false;             // the pointer is crossing between an element and its scrollable tooltip
  let shownText = '';               // the data-tooltip the shown tooltip presents
  let dwell = null;                 // {host, x, y, timer}: the pointer resting over a dwell element

  const textOf = element => String(element?.getAttribute('data-tooltip') || '').trim();
  const describedBy = element => (element.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);

  function element() {
    if (tip) return tip;
    tip = document.createElement('div');
    tip.id = ID;
    tip.className = 'dp-tooltip';
    tip.setAttribute('role', 'tooltip');
    if ('popover' in tip) tip.popover = 'manual';
    tip.hidden = true;
    document.body.appendChild(tip);
    tip.addEventListener('pointerleave', event => {
      if (!target || (event.relatedTarget instanceof Node && target.contains(event.relatedTarget))) return;
      if (crossing(event)) bridging = true;
      else hide();
    });
    return tip;
  }

  /* A scrollable tooltip can be reached: the pointer may travel by any straight
   * path from the element to its tooltip -- anywhere inside the convex hull of
   * the two -- without dismissing it; outside that region, it goes. */
  function crossing(event) {
    if (!target || !tip?.classList.contains('is-scrollable')) return false;
    const corners = [target.getBoundingClientRect(), tip.getBoundingClientRect()]
      .flatMap(r => [[r.left, r.top], [r.right, r.top], [r.right, r.bottom], [r.left, r.bottom]]);
    return insideHull(hull(corners), event.clientX, event.clientY);
  }
  // The convex hull of points, counter-clockwise (monotone chain).
  function hull(points) {
    const sorted = points.slice().sort((p, q) => p[0] - q[0] || p[1] - q[1]);
    const turn = (o, p, q) => (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0]);
    const half = list => list.reduce((chain, point) => {
      while (chain.length >= 2 && turn(chain[chain.length - 2], chain[chain.length - 1], point) <= 0) chain.pop();
      chain.push(point);
      return chain;
    }, []);
    const lower = half(sorted), upper = half(sorted.slice().reverse());
    return lower.slice(0, -1).concat(upper.slice(0, -1));
  }
  function insideHull(polygon, x, y) {
    return polygon.every((p, i) => {
      const q = polygon[(i + 1) % polygon.length];
      return (q[0] - p[0]) * (y - p[1]) - (q[1] - p[1]) * (x - p[0]) >= -0.5;
    });
  }

  // Above the element, centred on it -- or below when there is more room there
  // -- and never past the viewport's edges.
  function place(host) {
    const anchor = host.getBoundingClientRect();
    tip.classList.remove('is-below', 'is-scrollable');
    tip.style.maxHeight = '';
    tip.style.left = '0px';
    tip.style.top = '0px';
    const above = anchor.top - GAP - MARGIN, below = window.innerHeight - anchor.bottom - GAP - MARGIN;
    const height = tip.offsetHeight;
    const down = height > above && below > above;
    const room = Math.max(0, down ? below : above);
    if (height > room) tip.style.maxHeight = `${room}px`;
    tip.classList.toggle('is-below', down);
    tip.classList.toggle('is-scrollable', tip.scrollHeight > tip.clientHeight + 1);
    const box = tip.getBoundingClientRect();
    const left = Math.min(Math.max(MARGIN, anchor.left + anchor.width / 2 - box.width / 2),
                          Math.max(MARGIN, window.innerWidth - MARGIN - box.width));
    const top = down ? anchor.bottom + GAP : anchor.top - GAP - box.height;
    tip.style.left = `${Math.round(left)}px`;
    tip.style.top = `${Math.round(Math.max(MARGIN, top))}px`;
  }

  /* A section index from the tablist `data-tooltip-index` names inside the
   * element: "Current section", the current section, "N of M"; then every
   * section in its order, a middot between, the current one emphasised. Text
   * nodes only. Each section stays whole: a line may break only after a
   * middot. False (plain text instead) when there is no selected tab. */
  function renderIndex(host) {
    const list = host.querySelector(host.getAttribute('data-tooltip-index'));
    const tabs = list ? Array.from(list.querySelectorAll('[role="tab"]')) : [];
    const current = tabs.findIndex(tab => tab.getAttribute('aria-selected') === 'true');
    if (current < 0) return false;
    const label = tab => tab.textContent.trim().replace(/\s+/g, ' ');
    const node = (tag, className, text) => {
      const part = document.createElement(tag);
      if (className) part.className = className;
      part.textContent = text;
      return part;
    };
    const head = document.createElement('div');
    head.className = 'dp-tooltip-index-head';
    head.append(node('span', 'dp-tooltip-index-eyebrow', 'Current section'), ' ',
      node('strong', 'dp-tooltip-index-current', label(tabs[current])), ' ',
      node('span', 'dp-tooltip-index-count', `${current + 1} of ${tabs.length}`));
    const all = document.createElement('div');
    all.className = 'dp-tooltip-index-list';
    tabs.forEach((tab, k) => {
      if (k) all.append('\u00a0\u00b7 ');
      all.append(node(k === current ? 'strong' : 'span', k === current ? 'dp-tooltip-index-current' : '', label(tab)));
    });
    tip.replaceChildren(head, all);
    return true;
  }

  // The tooltip's content: a section index, or the element's text.
  function fill(host, text) {
    shownText = text;
    const index = host.hasAttribute('data-tooltip-index') && renderIndex(host);
    if (!index) tip.textContent = text;
    tip.classList.toggle('is-index', index);
  }

  function show(host) {
    cancel();
    const text = textOf(host);
    if (!text || !host.isConnected) return;
    if (target && target !== host) release();
    element();
    target = host;
    fill(host, text);
    tip.hidden = false;
    if (tip.popover && !tip.matches(':popover-open')) tip.showPopover();
    place(host);
    tip.classList.add('is-shown');
    // Described by it while shown, unless it only repeats the element's own name.
    const name = (host.getAttribute('aria-label') || host.textContent || '').trim();
    if (text !== name && !describedBy(host).includes(ID)) {
      host.setAttribute('aria-describedby', [...describedBy(host), ID].join(' '));
      host.dataset.dpTooltipDescribed = '';
    }
    // While shown: the element leaving the document hides it, and its text
    // changing (a speed badge, a pager's reason) is what it shows. One observer
    // at most: a tooltip shown again, or passed on to the next element, first
    // lets go of the one it had.
    unwatch();
    watch = new MutationObserver(() => {
      if (!target) return;
      const now = textOf(target);
      if (!target.isConnected || !now) return hide();
      if (now !== shownText) { fill(target, now); place(target); }
    });
    watch.observe(document.body, {childList: true, subtree: true, attributes: true, attributeFilter: ['data-tooltip']});
  }

  // The described element gives back its aria-describedby as it was.
  function release() {
    if (!target) return;
    if ('dpTooltipDescribed' in target.dataset) {
      const rest = describedBy(target).filter(id => id !== ID);
      if (rest.length) target.setAttribute('aria-describedby', rest.join(' '));
      else target.removeAttribute('aria-describedby');
      delete target.dataset.dpTooltipDescribed;
    }
    target = null;
  }

  function unwatch() {
    if (watch) { watch.disconnect(); watch = null; }
  }

  function hide() {
    cancel();
    unwatch();
    bridging = false;
    if (!target) return;
    release();
    lastHidden = performance.now();
    tip.classList.remove('is-shown');
    if (tip.popover && tip.matches(':popover-open')) tip.hidePopover();
    tip.hidden = true;
  }

  function cancel() {
    if (pending) clearTimeout(pending.timer);
    pending = null;
  }

  /* An element may name the label inside it that its tooltip repeats
   * (data-tooltip-label, a selector). While that label is shown the tooltip
   * has nothing to add; once it is faded out -- the compact sidebar rail's
   * labels -- the tooltip says it. */
  function repeatsShownLabel(host) {
    const selector = host.getAttribute('data-tooltip-label');
    if (!selector) return false;
    const label = host.querySelector(selector);
    return Boolean(label) && Number(getComputedStyle(label).opacity) > 0.5;
  }

  // The nearest described element -- passing over one that would only repeat
  // the label it shows, so a described element around it keeps its tooltip.
  function hostOf(node) {
    let host = node instanceof Element ? node.closest(SELECTOR) : null;
    while (host && repeatsShownLabel(host)) host = host.parentElement?.closest(SELECTOR) || null;
    return host && textOf(host) ? host : null;
  }

  const dwellOf = host => Number(host?.getAttribute('data-tooltip-dwell')) || 0;

  // What is still moving inside an element -- a navigator's window sliding
  // included, whose motion runs on its tabs and on the outgoing copy beside
  // its strip (which the control's own getAnimations() does not list).
  const moving = host => document.getAnimations().filter(animation => animation.playState === 'running'
    && animation.effect?.getTiming().iterations !== Infinity && animation.effect?.target instanceof Node && host.contains(animation.effect.target));

  /* The pointer at rest at (x, y) over a dwell element: (re)start its dwell.
   * When it runs out with something inside still moving, the dwell starts
   * again once that has finished. */
  function rest(host, x, y) {
    clearTimeout(dwell?.timer);
    dwell = {host, x, y, timer: setTimeout(() => {
      if (dwell?.host !== host || !host.isConnected) return;
      if (host.hasAttribute('data-tooltip-busy') || moving(host).length) return settle(host, dwell.x, dwell.y);
      dwell.timer = null;
      show(host);
    }, dwellOf(host))};
  }

  /* After a use of a dwell element, or while it is moving: the dwell begins
   * only once the element is settled -- looked at once the use's own
   * handlers have run. An owner that marks the element busy
   * (data-tooltip-busy: a navigator's window still sliding) says when it is
   * done (a bubbling dp-tooltip-ready); anything else moving in it is waited
   * for. The pointer's rest is followed meanwhile. */
  function settle(host, x, y) {
    clearTimeout(dwell?.timer);
    const mine = dwell = {host, x, y, timer: null, settling: true};
    mine.timer = setTimeout(() => {
      if (dwell !== mine) return;
      mine.timer = null;
      if (host.hasAttribute('data-tooltip-busy')) return;          // dp-tooltip-ready resumes it
      Promise.all(moving(host).map(animation => animation.finished.catch(() => {})))
        .then(() => { if (dwell === mine) rest(host, mine.x, mine.y); });
    }, 0);
  }

  document.addEventListener('dp-tooltip-ready', event => {
    if (!dwell?.settling || dwell.timer || !(event.target instanceof Node) || !dwell.host.contains(event.target)) return;
    settle(dwell.host, dwell.x, dwell.y);
  });

  function stopDwell() {
    clearTimeout(dwell?.timer);
    dwell = null;
  }

  function hover(host) {
    if (host === target || host === pending?.element || host === dismissed) return;
    const warm = target || performance.now() - lastHidden < WARM_MS;
    if (warm) return show(host);
    cancel();
    pending = {element: host, timer: setTimeout(() => { pending = null; show(host); }, DELAY_MS)};
  }

  document.addEventListener('pointerover', event => {
    if (event.pointerType === 'touch') return;             // no hover tooltips on touch
    if (bridging && (tip?.contains(event.target) || target?.contains(event.target))) bridging = false;
    const host = hostOf(event.target);
    if (host && dwellOf(host)) {
      // Entering it starts its dwell; moving within it, or its content moving
      // beneath the pointer, is not entering it.
      if (pending) cancel();
      const from = event.relatedTarget;
      if (dwell?.host !== host && !(from instanceof Node && host.contains(from)) && host !== dismissed) rest(host, event.clientX, event.clientY);
      return;
    }
    if (host) hover(host);
    else if (!(tip && tip.contains(event.target))) { if (pending) cancel(); }
  }, true);
  document.addEventListener('pointerout', event => {
    const host = hostOf(event.target);
    if (!host) return;
    const next = event.relatedTarget;
    if (next instanceof Node && (host.contains(next) || (tip && tip.contains(next) && tip.classList.contains('is-scrollable')))) return;
    if (target === host && crossing(event)) { bridging = true; return; }   // on the way to its tooltip
    if (dismissed === host) dismissed = null;
    if (pending?.element === host) cancel();
    if (dwell?.host === host) stopDwell();
    if (target === host) hide();
  }, true);
  document.addEventListener('pointermove', event => {
    if (bridging && !crossing(event)) hide();
    // Off its rest: a shown dwell tooltip goes, and the dwell starts again.
    if (dwell && event.pointerType !== 'touch' && Math.hypot(event.clientX - dwell.x, event.clientY - dwell.y) > JITTER) {
      if (dwell.settling) { dwell.x = event.clientX; dwell.y = event.clientY; return; }
      if (target === dwell.host) hide();
      rest(dwell.host, event.clientX, event.clientY);
    }
  }, true);
  document.addEventListener('focusin', event => {
    const host = hostOf(event.target);
    if (!host || (host !== event.target && !host.hasAttribute('data-tooltip-index'))) return;
    let keyboard = true;
    try { keyboard = event.target.matches(':focus-visible'); } catch (_) {}
    if (keyboard && host !== dismissed) show(host);
  }, true);
  // Focus leaving the element: its blur, heard in the capture phase (the
  // bubbling focus-out boundary is the Settings commit owner's alone).
  document.addEventListener('blur', event => {
    const host = hostOf(event.target);
    if (!host) return;
    if (dismissed === host) dismissed = null;
    if (target === host) hide();
  }, true);
  // Escape dismisses the tooltip; whatever else listens for Escape (a dialog,
  // a menu) still hears it, exactly as before there were tooltips.
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || !target) return;
    dismissed = target;
    if (dwell?.host === target) stopDwell();
    hide();
  }, true);
  const dismiss = event => {
    const host = hostOf(event.target);
    // Using a dwell element (a section, First, Last): its tooltip goes, and
    // only a fresh rest after this use brings it back.
    if (host && dwellOf(host) && event.pointerType !== 'touch') {
      hide();
      settle(host, event.clientX, event.clientY);
      return;
    }
    if (host) dismissed = host;
    if (!(tip && tip.contains(event.target))) hide();
  };
  document.addEventListener('pointerdown', dismiss, true);
  document.addEventListener('contextmenu', dismiss, true);
  // The use itself (a click lands after the press): the dwell settles again
  // from here.
  document.addEventListener('click', event => {
    const host = hostOf(event.target);
    if (host && dwellOf(host) && dwell?.host === host) settle(host, dwell.x, dwell.y);
  }, true);
  // Scrolling hides it -- but not the described element's own (a navigator's
  // window moving), which leaves the element where it is.
  document.addEventListener('scroll', event => {
    if (!target || (tip && tip.contains(event.target)) || (event.target instanceof Element && target.contains(event.target))) return;
    hide();
  }, true);
  window.addEventListener('resize', hide);
  window.addEventListener('blur', hide);
  document.addEventListener('debridpulse:navigation', hide);
  // The same edges end a dwell still waiting.
  window.addEventListener('resize', stopDwell);
  window.addEventListener('blur', stopDwell);
  document.addEventListener('debridpulse:navigation', stopDwell);

  window.DPTooltip = Object.freeze({hide});
})();
