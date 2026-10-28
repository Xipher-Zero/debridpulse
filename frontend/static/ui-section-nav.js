/* DebridPulse section navigator: the one owner of how a master-card header
 * presents its section tabs on desktop and tablet (780px and wider).
 *
 * A page owns its header, its tabs (one tablist, each tab its own section),
 * their selection, panels, ARIA and events; it gives each header a
 * navigation of this shape and calls fit() and place():
 *
 *   <header>  <copy/>  <.dp-section-nav>
 *     <.dp-section-nav-control>
 *       <button.dp-section-nav-end>  (First: the first section)
 *       <[role=tablist].dp-section-nav-strip> [role=tab]... </>
 *       <button.dp-section-nav-end>  (Last: the last section)
 *
 * This owner only measures and places: which presentation the header shows
 * (data-presentation), the compact window's width, room, trim and position,
 * which end of the list the window shows (data-nav-at) and which sections an edge
 * cuts (data-nav-cut), with the custom properties ui-section-nav.css draws
 * them by. It keeps no selection of its own and renders nothing.
 */
(function () {
  'use strict';

  /* The header's one row. The navigation keeps the complete strip's own
   * place -- centred in the whole header while the copy fits beside it,
   * otherwise right after the copy -- and is the complete strip whenever
   * the copy, the row gap and the strip fit the header with NAV_SPARE to
   * spare. Otherwise it is the compact navigator: First, a window onto the
   * same strip, and Last. First and Last stand over the strip's ends; where
   * the window shows the list's first or last section -- whichever section
   * is selected -- the one that would lead there is gone and the window
   * takes its place (each arrangement's own room and side). The window is as wide as the
   * navigator can be while still centred in the header, and
   * wider (the navigator moving right, never past the copy) only while some
   * section would otherwise have fewer than three positions (navWindow);
   * navLayout() places the selected section in it. Where even the widest
   * window cannot hold the first or last section with its neighbour whole,
   * the tabs give up to NAV_TRIM of their side padding (navTrim) so that it
   * can; where that is not enough either, the selected section still comes
   * first -- whole, at the list's end -- and its neighbour gives up the
   * rest, cut at the window's edge and faded like any section an edge cuts.
   * That is where a render, a resize or a refit places the window; the
   * operator's own choices move it from where it is drawn (navChoice), so
   * that the section under the pointer at the window's edge is the next one
   * after each move, and a shown first or last section is reached at once.
   *
   * Everything is decided here, in this browser, from the rendered tabs,
   * copy and controls: the tab set and the fonts are whatever the page
   * actually drew, never an assumed tab count or a breakpoint. The tabs'
   * widths are measured when the header is drawn and when fonts finish
   * loading; the copy's, which a page may change with its width, at every
   * fit; a change of the page's width only recomputes from them. Below
   * 780px the handheld header keeps its own wrapping strip and is not
   * touched. */
  const NAV_SPARE = 4;
  const NAV_FADE = 0.3;      // the outer share of that part which fades
  const NAV_TARGET = 24;     // the least part of a section that is a position
  const NAV_TRIM = 5;        // the most side padding a compact tab gives up
  const NAV_CUT = [0.3, 0.9];
  const INDEX_DWELL_MS = 1000;   // the pointer at rest this long before the section index shows
  const navDesktop = window.matchMedia('(min-width: 780px)');

  const shown = node => node.checkVisibility?.({visibilityProperty: true})
    ?? (node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden');

  /* The compact navigator, as a whole, describes every section and which is
   * current: the one tooltip owner (ui-tooltip.js) shows it as an index read
   * from the tablist's own tabs (data-tooltip-index) once the pointer has
   * rested over the control (data-tooltip-dwell), or on focus within it;
   * data-tooltip says it as text and changes with the selection. The
   * complete strip shows every section and has none. */
  function describe(nav, tabs, index) {
    const control = nav.querySelector('.dp-section-nav-control');
    if (index < 0) {
      control.removeAttribute('data-tooltip');
      control.removeAttribute('data-tooltip-index');
      control.removeAttribute('data-tooltip-dwell');
      return;
    }
    control.dataset.tooltipIndex = '[role="tablist"]';
    // For the pointer, only after a deliberate pause over it (ui-tooltip.js).
    control.dataset.tooltipDwell = String(INDEX_DWELL_MS);
    control.dataset.tooltip = `Current section: ${tabs[index].textContent.trim().replace(/\s+/g, ' ')}, ${index + 1} of ${tabs.length}`;
  }

  /* One header's navigator. `nav` finds its .dp-section-nav (the page may
   * render it again); `copy` selects the header's identity copy. */
  function create({nav: locate, copy: copySelector}) {
    let navFit = null;
    // The slide that holds the control's busy mark, and that control: kept
    // apart from navFit, which a re-measure replaces while a slide may still
    // be running (the slide, not the fit, says when it is over).
    let busy = null;

    // The copy's own width: its parts (its box also takes the space that
    // centres the navigation).
    const copyWidth = header => Array.from(header.querySelector(copySelector).children)
      .reduce((sum, part, index) => sum + part.getBoundingClientRect().width
        + (index ? parseFloat(getComputedStyle(part.parentElement).columnGap) || 0 : 0), 0);

    function fitNavigation({measure = false} = {}) {
      if (measure) navFit = null;
      const nav = locate();
      const header = nav?.parentElement;
      if (nav && !navDesktop.matches) describe(nav, [], -1);
      if (!nav || !header.clientWidth || !navDesktop.matches) return;
      const strip = nav.querySelector('.dp-section-nav-strip');
      const tabs = Array.from(strip.querySelectorAll('[role="tab"]'));
      if (navFit?.nav !== nav) {
        header.dataset.presentation = 'full';
        const widths = tabs.map(tab => tab.getBoundingClientRect().width);
        const style = getComputedStyle(strip);
        const frame = ['paddingLeft', 'paddingRight', 'borderLeftWidth', 'borderRightWidth']
          .reduce((sum, key) => sum + parseFloat(style[key]), 0);
        // The compact control's own parts: First and Last (each over one end
        // of the strip, `slot` wide), its padding and border, and the room
        // kept at either end of the list.
        header.dataset.presentation = 'compact';
        const slot = Math.max(...Array.from(nav.querySelectorAll('.dp-section-nav-end'),
          end => end.getBoundingClientRect().width));
        const chrome = nav.querySelector('.dp-section-nav-control').getBoundingClientRect().width
          - strip.getBoundingClientRect().width + 2 * slot;
        const edge = parseFloat(getComputedStyle(tabs[0]).marginInlineStart) || 0;
        navFit = {nav, natural: widths, widths, strip: widths.reduce((sum, width) => sum + width, 0) + frame, copy: 0, chrome, slot, edge, window: 0, trim: 0, left: null};
      }
      navFit.copy = copyWidth(header);
      // The window as drawn now (none until it has been drawn compact).
      const drawn = navFit.drawn && header.dataset.presentation === 'compact' ? `${navFit.window} ${navFit.trim}` : null;
      const style = getComputedStyle(header);
      const width = header.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      const gap = parseFloat(style.columnGap);
      const presentation = navFit.strip + navFit.copy + gap + NAV_SPARE <= width ? 'full' : 'compact';
      if (presentation === 'compact') {
        navFit.widths = navFit.natural;
        navFit.window = navWindow(width, gap);
        navFit.trim = navTrim(navFit.window);
        navFit.widths = navFit.natural.map(each => each - 2 * navFit.trim);
        nav.style.setProperty('--dp-section-nav-window', `${navFit.window}px`);
        nav.style.setProperty('--dp-section-nav-slot', `${navFit.slot}px`);
      }
      if (presentation === 'compact' && navFit.trim) nav.style.setProperty('--dp-section-nav-trim', `${navFit.trim}px`);
      else nav.style.removeProperty('--dp-section-nav-trim');
      if (header.dataset.presentation !== presentation) header.dataset.presentation = presentation;
      if (presentation !== 'compact') describe(nav, [], -1);
      // A refit that leaves the compact window as it is (the page's height
      // changed, say) leaves its placement -- and a slide under way -- alone.
      if (presentation !== 'compact' || drawn !== `${navFit.window} ${navFit.trim}`) placeNavigation();
      // A focused control the new presentation no longer shows hands focus to
      // the selected tab, never to a hidden control.
      const focused = document.activeElement;
      if (nav.contains(focused) && !shown(focused)) strip.querySelector('[aria-selected="true"]')?.focus({preventScroll: true});
    }

    /* The compact window's width for a header this wide: the widest, up to
     * the width that keeps the navigator centred in the header, at which every
     * section has a layout (navLayout) of at least three positions; wider --
     * the navigator moving right, never past the copy -- only when there is
     * none. Arithmetic over the measured widths only. */
    function navWindow(width, gap) {
      const {widths, copy, chrome, edge} = navFit;
      const widest = Math.max(...widths);
      const most = Math.max(widest, Math.floor(Math.min(width - copy - gap - NAV_SPARE - chrome,
        widths.reduce((sum, each) => sum + each, 2 * edge) - 1)));
      const centred = Math.min(most, Math.max(widest, Math.floor(width - gap - 2 * (copy + NAV_SPARE) - chrome)));
      const need = Math.min(3, widths.length);
      // (With both travel actions there: the control keeps this width
      // whichever section is selected; an end section's own room is more.)
      const enough = window => widths.every((_, index) => (navLayout(window, index)?.positions ?? 0) >= need);
      for (let window = centred; window >= widest; window -= 1) if (enough(window)) return window;
      for (let window = centred + 1; window <= most; window += 1) if (enough(window)) return window;
      return most;
    }

    /* The side padding each tab gives up so that a window this wide holds
     * the first and the last section each with its neighbour whole (and the
     * room kept at that end), its own travel action gone: none while it
     * already does, else the least
     * (in half pixels, the two tabs giving it up on both sides) up to
     * NAV_TRIM. Half a pixel more than exact: the window scrolls in whole
     * pixels. (Past NAV_TRIM, the neighbour is cut by what remains.) */
    function navTrim(window) {
      const {natural: widths, edge, slot} = navFit;
      if (widths.length < 2) return 0;
      const pair = Math.max(widths[0] + widths[1], widths.at(-2) + widths.at(-1)) + edge + 0.5;
      return Math.min(NAV_TRIM, Math.max(0, Math.ceil((pair - window - slot) / 4 * 2) / 2));
    }

    /* Where a window this wide stands on the strip with this section
     * selected: {left, room, side}, `left` and `room` the part of the list
     * it shows. The selected section is whole; a window edge where sections
     * continue cuts one section, showing NAV_CUT of it, and fades. The window
     * starts at the list's first section (side 'first': First is gone and the
     * window takes its slot), ends at its last ('last': likewise Last), shows
     * all of it ('only') or lies between with a section cut at each edge;
     * of the possible layouts, the one centring the selection best, then the
     * one with more sections. null when there is none. */
    function navLayout(window, index) {
      const {widths, edge, slot} = navFit;
      const count = widths.length;
      const at = [];
      let end = edge;
      for (const each of widths) { at.push(end); end += each; }
      const total = end + edge;
      if (window + 2 * slot >= total) return {left: 0, room: window + 2 * slot, side: 'only', positions: count};
      let best = null;
      for (let from = 0; from <= index; from += 1) {
        for (let to = index; to < count; to += 1) {
          if ((from === index && from > 0) || (to === index && to < count - 1) || (from === 0 && to === count - 1)) continue;
          let left, room = window, side = '';
          if (from === 0) {
            room = window + slot;
            side = 'first';
            left = 0;
            const part = room - at[to];
            if (part < NAV_CUT[0] * widths[to] || part > NAV_CUT[1] * widths[to]) continue;
          } else if (to === count - 1) {
            room = window + slot;
            side = 'last';
            left = total - room;
            const part = at[from] + widths[from] - left;
            if (part < NAV_CUT[0] * widths[from] || part > NAV_CUT[1] * widths[from]) continue;
          } else {
            // Each cut shows NAV_CUT of its section: an interval of window
            // positions per edge; the selection as centred as they allow.
            const low = Math.max(at[from] + widths[from] * (1 - NAV_CUT[1]), at[to] - window + NAV_CUT[0] * widths[to]);
            const high = Math.min(at[from] + widths[from] * (1 - NAV_CUT[0]), at[to] - window + NAV_CUT[1] * widths[to]);
            if (low > high) continue;
            left = Math.min(Math.max(at[index] + widths[index] / 2 - window / 2, low), high);
          }
          const ideal = at[index] + widths[index] / 2 - room / 2;
          const candidate = {left, room, side, positions: to - from + 1, off: Math.abs(left - ideal)};
          if (!best || candidate.off < best.off - 0.5
            || (candidate.off <= best.off + 0.5 && candidate.positions > best.positions)) best = candidate;
        }
      }
      return best;
    }

    function navFallback(window, index) {
      const {widths, edge, slot} = navFit;
      const total = widths.reduce((sum, each) => sum + each, 2 * edge);
      const at = widths.slice(0, index).reduce((sum, each) => sum + each, edge);
      const left = at + widths[index] / 2 - window / 2;
      if (left <= edge) return {left: 0, room: window + slot, side: 'first'};
      if (left + window >= total - edge) return {left: Math.max(0, total - window - slot), room: window + slot, side: 'last'};
      return {left, room: window, side: ''};
    }

    // First is gone: the window shows the list's first section.
    const firstGone = side => side === 'first' || side === 'only';

    /* Where the window goes for the operator's choice of this section
     * (`motion`), from where it is drawn on screen (onScreen): the window keeps
     * the interaction targets where the operator's pointer is. A section the
     * drawn window already shows whole leaves it -- and the travel actions
     * it shows -- where it is. The rightmost
     * section shown (a part of NAV_TARGET or more), chosen going forward,
     * moves the window on by exactly as much as puts the next section where
     * the chosen one was -- so the next click, at the same place, chooses
     * the next section -- and the leftmost going back, the previous one;
     * where the next section is narrower than the part shown, it ends where
     * that part ended, never beyond. A move that would show the list's first
     * or last section whole is that end's arrangement (navLayout's sides).
     * The selection stays whole: the window
     * moves no farther than that allows, and never past the list's ends.
     * The first or last section chosen where it is already shown is the
     * end arrangement at once (`snap`: no selector slide); First and Last
     * (`motion.via === 'end'`) and a section the window does not show keep
     * the ordinary layout (navLayout). null: the ordinary layout. */
    function navChoice(index, motion) {
      const {widths, edge, slot, window: width} = navFit;
      const {left: drawn, room: drawnRoom, side: drawnSide, index: was} = onScreen();
      const count = widths.length;
      const at = [];
      let end = edge;
      for (const each of widths) { at.push(end); end += each; }
      const total = end + edge;
      // The drawn window on the list, and the sections it showed.
      const before = drawn - (firstGone(drawnSide) ? slot : 0);
      const part = k => Math.min(at[k] + widths[k], before + drawnRoom) - Math.max(at[k], before);
      const shown = widths.map((_, k) => k).filter(k => k === was || part(k) >= NAV_TARGET);
      if (!shown.includes(index)) return null;
      if (index === 0 || index === count - 1) {
        if (motion.via === 'end') return null;
        return {...(navLayout(width, index) || navFallback(width, index)), snap: true};
      }
      const whole = ({left, room}) => at[index] >= left - 0.5 && at[index] + widths[index] <= left + room + 0.5;
      const forward = index > was && index === shown.at(-1);
      const backward = index < was && index === shown[0];
      if (!forward && !backward) {
        const still = {left: before, room: drawnRoom, side: drawnSide};
        return whole(still) ? {...still, snap: false} : null;
      }
      const step = Math.min(widths[index] - Math.max(0, part(index) - widths[index + (forward ? 1 : -1)]),
        forward ? at[index] - before : before + drawnRoom - at[index] - widths[index]);
      // The sections move on screen by `step`; First, gone in the drawn
      // window, takes its slot again between them.
      let left = before + (forward ? step : -step) + (firstGone(drawnSide) ? slot : 0);
      let next = {left, room: width, side: ''};
      if (left <= edge) next = {left: 0, room: width + slot, side: 'first'};
      else if (left + width >= total - edge) next = {left: Math.max(0, total - width - slot), room: width + slot, side: 'last'};
      return whole(next) ? {...next, snap: false} : null;
    }

    /* The compact strip scrolls (never by the user) to where the selected
     * section's window goes -- for the operator's choice (`motion`: the
     * page's own, {direction, distance, via}) as navChoice decides, else
     * (a render, a resize, a refit) where navLayout places it -- and fades
     * where sections continue. The strip spans the whole control, First and
     * Last over its ends: it is clipped to the window, and where the window
     * shows the list's first or last section (data-nav-at, whatever is
     * selected) the action that would lead there is gone and the window
     * takes its place. A choice that moves the window from
     * where it is drawn slides it, by DPMotion's own slide of the strip
     * alone, the way the choice goes (as the panel does), unless it is an
     * end section's snap; anything else is drawn at once, ending a slide of
     * the strip still running. Where the window goes is decided here
     * (navFit.left); the slide's commit only draws the latest decision. */
    function placeNavigation(motion = null) {
      const nav = locate();
      if (!nav || navFit?.nav !== nav || nav.parentElement.dataset.presentation !== 'compact' || !navDesktop.matches) return;
      const strip = nav.querySelector('.dp-section-nav-strip');
      const tabs = Array.from(strip.querySelectorAll('[role="tab"]'));
      const index = tabs.findIndex(tab => tab.getAttribute('aria-selected') === 'true');
      if (index < 0) return;
      describe(nav, tabs, index);
      // (Without a layout -- only when no window width gives every section one
      // -- the selection as centred as the list's ends allow.)
      const {left, room, side, snap} = (motion && navFit.drawn && navChoice(index, motion))
        || {...(navLayout(navFit.window, index) || navFallback(navFit.window, index)), snap: false};
      // The window starts after First, unless First is gone; the strip's own
      // padding (one slot) lets it start before the list.
      const target = Math.round(left + (firstGone(side) ? navFit.slot : 0));
      navFit.left = target;
      navFit.index = index;
      navFit.room = room;
      navFit.side = side;
      if ((onScreen()?.left ?? strip.scrollLeft) !== target && motion?.direction && !snap && window.DPMotion) {
        const slide = navFit.slide = window.DPMotion.slide({element: strip, axis: 'x', direction: motion.direction,
          distance: motion.distance}, drawNavigation);
        // Busy while the window slides: the section index waits for it
        // (ui-tooltip.js data-tooltip-dwell). The latest slide holds the
        // mark; its own completion -- finished, skipped or superseded --
        // clears it and says the navigator is ready, whatever has re-measured
        // meanwhile. A control rendered anew leaves no mark on the old one.
        const control = strip.parentElement;
        if (busy && busy.control !== control) delete busy.control.dataset.tooltipBusy;
        const mine = busy = {slide, control};
        control.dataset.tooltipBusy = '';
        slide.settled.then(() => {
          if (busy !== mine) return;
          busy = null;
          delete control.dataset.tooltipBusy;
          control.dispatchEvent(new Event('dp-tooltip-ready', {bubbles: true}));
        });
        return;
      }
      navFit.slide?.skip();
      drawNavigation();
    }

    /* The window on screen: the one drawn last, or -- until that draw is
     * painted, the next frame -- the one shown before it. A slide's commit
     * draws its window at once (DPMotion.slide), so a second choice in the
     * same task or frame is judged from what the operator sees, not from a
     * window never shown. */
    const onScreen = () => navFit?.shown || navFit?.drawn;

    // The latest decided window, drawn.
    function drawNavigation() {
      const nav = locate();
      if (!nav || navFit?.nav !== nav || nav.parentElement.dataset.presentation !== 'compact' || !navDesktop.matches) return;
      const strip = nav.querySelector('.dp-section-nav-strip');
      const tabs = Array.from(strip.querySelectorAll('[role="tab"]'));
      const {widths, edge, slot, left: target, index, room, side} = navFit;
      if (!navFit.shown && navFit.drawn) {
        const fit = navFit;
        fit.shown = fit.drawn;
        requestAnimationFrame(() => { delete fit.shown; });
      }
      navFit.drawn = {left: target, index, room, side};
      // Which travel action is gone: the end of the list the window shows.
      const at = tabs.length === 1 ? 'only' : side;
      const control = strip.parentElement;
      if ((control.dataset.navAt || '') !== at) {
        if (at) control.dataset.navAt = at;
        else delete control.dataset.navAt;
      }
      if (strip.scrollLeft !== target) strip.scrollLeft = target;
      // The section each window edge cuts where sections continue fades
      // outwards from that edge, over NAV_FADE of what it shows (NAV_TARGET at
      // least); a part smaller than NAV_TARGET is not shown at all. On the tab
      // itself: the strip is DPMotion's slide surface, which cannot carry a
      // mask of its own.
      const from = target - (firstGone(side) ? slot : 0);
      let x = edge;
      tabs.forEach((tab, k) => {
        const part = Math.min(x + widths[k], from + room) - Math.max(x, from);
        const cut = part > 0 && x < from - 0.5 ? 'start'
          : part > 0 && x + widths[k] > from + room + 0.5 ? 'end' : '';
        x += widths[k];
        if (!cut) {
          delete tab.dataset.navCut;
          tab.style.removeProperty('--dp-section-nav-cut-edge');
          tab.style.removeProperty('--dp-section-nav-cut-fade');
          return;
        }
        tab.dataset.navCut = part < NAV_TARGET ? 'sliver' : cut;
        tab.style.setProperty('--dp-section-nav-cut-edge', `${widths[k] - part}px`);
        tab.style.setProperty('--dp-section-nav-cut-fade', `${Math.min(part, Math.max(NAV_FADE * part, NAV_TARGET))}px`);
      });
    }

    return Object.freeze({
      fit: fitNavigation,
      place: placeNavigation,
      // Whether this navigator places its strip (desktop and tablet): there,
      // focus must never scroll the strip itself.
      placing: () => navDesktop.matches,
    });
  }

  window.DPSectionNav = Object.freeze({create});
})();
