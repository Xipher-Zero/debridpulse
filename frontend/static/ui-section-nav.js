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
 * which end the selection is at (data-nav-at) and which sections an edge
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
   * same strip, and Last. First and Last stand over the strip's ends; at the
   * first or last section the one that would lead there is gone and the
   * window takes its place (navRoom). The window is as wide as the
   * navigator can be while still centred in the header, and
   * wider (the navigator moving right, never past the copy) only while some
   * section would otherwise have fewer than three positions (navWindow);
   * navLayout() places the selected section in it. Where even the widest
   * window cannot hold the first or last section with its neighbour whole,
   * the tabs give up to NAV_TRIM of their side padding (navTrim) so that it
   * can; where that is not enough either, the selected section still comes
   * first -- whole, at the list's end -- and its neighbour gives up the
   * rest, cut at the window's edge and faded like any section an edge cuts.
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
  const navDesktop = window.matchMedia('(min-width: 780px)');

  const shown = node => node.checkVisibility?.({visibilityProperty: true})
    ?? (node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden');

  /* One header's navigator. `nav` finds its .dp-section-nav (the page may
   * render it again); `copy` selects the header's identity copy. */
  function create({nav: locate, copy: copySelector}) {
    let navFit = null;

    // The copy's own width: its parts (its box also takes the space that
    // centres the navigation).
    const copyWidth = header => Array.from(header.querySelector(copySelector).children)
      .reduce((sum, part, index) => sum + part.getBoundingClientRect().width
        + (index ? parseFloat(getComputedStyle(part.parentElement).columnGap) || 0 : 0), 0);

    function fitNavigation({measure = false} = {}) {
      if (measure) navFit = null;
      const nav = locate();
      const header = nav?.parentElement;
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
      placeNavigation();
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

    /* The window's room with this section selected: at the first or last
     * section the travel action that would lead there is gone, and the window
     * takes its place. */
    function navRoom(window, index) {
      const {widths, slot} = navFit;
      return window + slot * ((index === 0) + (index === widths.length - 1));
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
     * selected. The selected section is whole; a window edge where sections
     * continue cuts one section, showing NAV_CUT of it, and fades. The window starts at the list's first
     * section, ends at its last, or lies between with a section cut at each
     * edge; of the possible layouts, the one centring the selection best, then
     * the one with more sections. null when there is none. */
    function navLayout(window, index) {
      const {widths, edge} = navFit;
      const count = widths.length;
      const at = [];
      let end = edge;
      for (const each of widths) { at.push(end); end += each; }
      const total = end + edge;
      if (window >= total) return {left: 0, positions: count};
      const ideal = at[index] + widths[index] / 2 - window / 2;
      let best = null;
      for (let from = 0; from <= index; from += 1) {
        for (let to = index; to < count; to += 1) {
          if ((from === index && from > 0) || (to === index && to < count - 1) || (from === 0 && to === count - 1)) continue;
          let left;
          if (from === 0) {
            left = 0;
            const part = window - at[to];
            if (part < NAV_CUT[0] * widths[to] || part > NAV_CUT[1] * widths[to]) continue;
          } else if (to === count - 1) {
            left = total - window;
            const part = at[from] + widths[from] - left;
            if (part < NAV_CUT[0] * widths[from] || part > NAV_CUT[1] * widths[from]) continue;
          } else {
            // Each cut shows NAV_CUT of its section: an interval of window
            // positions per edge; the selection as centred as they allow.
            const low = Math.max(at[from] + widths[from] * (1 - NAV_CUT[1]), at[to] - window + NAV_CUT[0] * widths[to]);
            const high = Math.min(at[from] + widths[from] * (1 - NAV_CUT[0]), at[to] - window + NAV_CUT[1] * widths[to]);
            if (low > high) continue;
            left = Math.min(Math.max(ideal, low), high);
          }
          const candidate = {left, positions: to - from + 1, off: Math.abs(left - ideal)};
          if (!best || candidate.off < best.off - 0.5
            || (candidate.off <= best.off + 0.5 && candidate.positions > best.positions)) best = candidate;
        }
      }
      return best;
    }

    function navFallback(window, index) {
      const {widths, edge} = navFit;
      const total = widths.reduce((sum, each) => sum + each, 2 * edge);
      const at = widths.slice(0, index).reduce((sum, each) => sum + each, edge);
      return {left: Math.min(Math.max(at + widths[index] / 2 - window / 2, 0), Math.max(0, total - window))};
    }

    /* The compact strip scrolls (never by the user) to where navLayout places
     * the selected section, and fades where sections continue. The strip
     * spans the whole control, First and Last over its ends: it is clipped
     * to the window, and at the first or last section (data-nav-at) the
     * action that would lead there is gone and the window takes its place.
     * A choice that moves the window (`motion`: the page's own choice,
     * {direction, distance}, the step its panel slides by) slides it, by
     * DPMotion's own slide of the strip alone, the way the choice goes (as
     * the panel does); one that leaves the window where it is, a resize or
     * a refit moves nothing. Where the window is is where it was last placed
     * (navFit.left), even while that slide has yet to draw it. */
    function placeNavigation(motion = null) {
      const nav = locate();
      if (!nav || navFit?.nav !== nav || nav.parentElement.dataset.presentation !== 'compact' || !navDesktop.matches) return;
      const strip = nav.querySelector('.dp-section-nav-strip');
      const tabs = Array.from(strip.querySelectorAll('[role="tab"]'));
      const index = tabs.findIndex(tab => tab.getAttribute('aria-selected') === 'true');
      if (index < 0) return;
      const {widths, edge, slot} = navFit;
      const room = navRoom(navFit.window, index);
      // (Without a layout -- only when no window width gives every section one
      // -- the selection as centred as the list's ends allow.)
      const layout = navLayout(room, index) || navFallback(room, index);
      // The window starts after First, unless First is gone; the strip's own
      // padding (one slot) lets it start before the list.
      const start = index > 0 ? slot : 0;
      const target = Math.round(layout.left + slot - start);
      const placed = navFit.left ?? strip.scrollLeft;
      navFit.left = target;
      if (placed !== target && motion?.direction && window.DPMotion) {
        window.DPMotion.slide({element: strip, axis: 'x', direction: motion.direction,
          distance: motion.distance}, () => placeNavigation());
        return;
      }
      const at = tabs.length === 1 ? 'only' : index === 0 ? 'first' : index === tabs.length - 1 ? 'last' : '';
      const control = strip.parentElement;
      if ((control.dataset.navAt || '') !== at) {
        if (at) control.dataset.navAt = at;
        else delete control.dataset.navAt;
      }
      if (strip.scrollLeft !== target) strip.scrollLeft = target;
      // The section each window edge cuts where sections continue fades
      // outwards from that edge, over NAV_FADE of what it shows (NAV_TARGET at
      // least); a part smaller than NAV_TARGET -- only where no layout fits
      // (navFallback) -- is not shown at all. On the tab itself: the strip is
      // DPMotion's slide surface, which cannot carry a mask of its own.
      const from = target - slot + start;
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
