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
 * aria-describedby while shown. */
(function () {
  'use strict';

  const DELAY_MS = 450;
  const WARM_MS = 300;              // within this of the last tooltip, the next shows at once
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

  function show(host) {
    cancel();
    const text = textOf(host);
    if (!text || !host.isConnected) return;
    if (target && target !== host) release();
    element();
    target = host;
    tip.textContent = text;
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
      if (now !== tip.textContent) { tip.textContent = now; place(target); }
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

  function hostOf(node) {
    const host = node instanceof Element ? node.closest(SELECTOR) : null;
    return host && textOf(host) ? host : null;
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
    if (target === host) hide();
  }, true);
  document.addEventListener('pointermove', event => {
    if (bridging && !crossing(event)) hide();
  }, true);
  document.addEventListener('focusin', event => {
    const host = hostOf(event.target);
    if (!host || host !== event.target) return;
    let keyboard = true;
    try { keyboard = host.matches(':focus-visible'); } catch (_) {}
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
    hide();
  }, true);
  const dismiss = event => {
    const host = hostOf(event.target);
    if (host) dismissed = host;
    if (!(tip && tip.contains(event.target))) hide();
  };
  document.addEventListener('pointerdown', dismiss, true);
  document.addEventListener('contextmenu', dismiss, true);
  document.addEventListener('scroll', event => { if (target && !(tip && tip.contains(event.target))) hide(); }, true);
  window.addEventListener('resize', hide);
  window.addEventListener('blur', hide);
  document.addEventListener('debridpulse:navigation', hide);

  window.DPTooltip = Object.freeze({hide});
})();
