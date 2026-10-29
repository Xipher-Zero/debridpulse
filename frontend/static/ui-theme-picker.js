/* DebridPulse theme picker: the chevron beside the mode toggle and its menu
 * of the active mode's palettes. ui-theme-bootstrap.js (window.DPTheme) owns
 * the catalog, the choices and their application; this file only presents
 * them. The menu is a popover in the top layer, so opening it never moves the
 * topbar; the browser's popover gives it light dismiss, Escape and focus
 * return to the chevron.
 *
 * Below 780px (the handheld shell) the same menu is a bottom sheet
 * (ui-theme-picker.css): the popover's own box is its scrim, so a press beside
 * the panel closes it without reaching the page; the chevron stays its
 * visible, separate control, and a long press on the mode toggle opens it too
 * (on the lift) -- that press never also toggles the mode.
 */
(function () {
  'use strict';
  const trigger = document.getElementById('theme-picker-toggle');
  const menu = document.getElementById('theme-picker');
  const list = document.getElementById('theme-picker-list');
  if (!trigger || !menu || !list || !window.DPTheme) return;
  const theme = window.DPTheme;
  const MARGIN = 8;
  const GAP = 8;
  const handheld = window.matchMedia('(max-width: 779px)');
  const toggle = document.getElementById('theme-toggle');
  const HOLD_MS = 480;
  const HOLD_SLOP = 10;
  const CHECK = '<svg class="lucide dp-utility-icon" xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M20 6 9 17l-5-5"/></svg>';

  // The rows of the active mode only, each with its own swatch: an
  // alternative's swatch carries that palette (ui-theme-palettes.css), the
  // native one the native mode tokens, so a swatch is the applied palette.
  function render() {
    const mode = theme.mode();
    const current = theme.palette();
    const word = mode === 'light' ? 'Light' : 'Dark';
    document.getElementById('theme-picker-title').textContent = word + ' themes';
    document.getElementById('theme-picker-note').textContent = 'Changes ' + mode + ' appearance only';
    list.replaceChildren(...theme.catalog[mode].map(function (item, index) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'dp-theme-picker-option';
      row.setAttribute('role', 'menuitemradio');
      row.setAttribute('aria-checked', String(item.id === current));
      row.tabIndex = item.id === current ? 0 : -1;
      row.dataset.palette = item.id;
      const swatch = document.createElement('span');
      swatch.className = 'dp-theme-swatch';
      swatch.setAttribute('aria-hidden', 'true');
      if (index === 0) swatch.dataset.theme = mode;
      else swatch.dataset.dpPalette = item.id;
      swatch.innerHTML = '<i data-role="background"></i><i data-role="surface"></i><i data-role="accent"></i><i data-role="tone"></i>';
      const name = document.createElement('span');
      name.className = 'dp-theme-picker-name';
      name.textContent = item.label;
      const check = document.createElement('span');
      check.className = 'dp-theme-picker-check';
      check.innerHTML = CHECK;
      row.append(swatch, name, check);
      return row;
    }));
  }

  function rows() { return Array.from(list.querySelectorAll('.dp-theme-picker-option')); }

  // Below the chevron, its right edge on the chevron's and never past the
  // window; taller than the room below, it scrolls on its own.
  function place() {
    if (!menu.matches(':popover-open')) return;
    if (handheld.matches) {
      menu.style.left = menu.style.top = menu.style.maxHeight = '';
      return;
    }
    const anchor = trigger.getBoundingClientRect();
    const width = menu.offsetWidth;
    const left = Math.max(MARGIN, Math.min(anchor.right - width, window.innerWidth - MARGIN - width));
    const top = anchor.bottom + GAP;
    menu.style.left = left + 'px';
    menu.style.top = top + 'px';
    menu.style.maxHeight = Math.max(120, window.innerHeight - top - MARGIN) + 'px';
  }

  menu.addEventListener('beforetoggle', function (event) {
    if (event.newState === 'open') render();
    else opener = trigger;
  });
  menu.addEventListener('toggle', function (event) {
    const open = event.newState === 'open';
    trigger.setAttribute('aria-expanded', String(open));
    if (!open) return;
    place();
    (rows().find(function (row) { return row.getAttribute('aria-checked') === 'true'; }) || rows()[0])?.focus();
  });

  // The control that opened the sheet: focus returns there when a press on
  // the scrim closes it (the press itself took focus out of the sheet).
  let opener = trigger;
  menu.addEventListener('click', function (event) {
    if (!handheld.matches || event.target !== menu) return;
    const back = opener;
    menu.hidePopover();
    back.focus();
  });
  handheld.addEventListener('change', function () {
    if (menu.matches(':popover-open')) menu.hidePopover();
  });

  /* The long press: held still on the mode toggle for HOLD_MS, the toggle
   * shows it is armed (data-theme-hold), and lifting then opens this menu.
   * Moving, leaving the toggle or a cancelled pointer ends it with nothing
   * done; a lift before HOLD_MS is the ordinary tap. It opens on the lift,
   * not before: a touch stays captured by the toggle until it lifts, so a
   * menu already open would take that lift as a press outside it and close.
   * The click that ends an opening press -- on the toggle, or on the menu
   * now under the finger -- is not a tap: it is stopped before any handler,
   * so the mode never changes and the menu stays open; any new press clears
   * that, so no later click is ever taken for it. */
  let hold = null;
  let held = false;
  function endHold() {
    if (hold) window.clearTimeout(hold.timer);
    hold = null;
    if (toggle) delete toggle.dataset.themeHold;
  }
  if (toggle) {
    window.addEventListener('pointerdown', function () { held = false; }, true);
    toggle.addEventListener('pointerdown', function (event) {
      if (!handheld.matches || !event.isPrimary || event.button !== 0) return;
      endHold();
      hold = {x: event.clientX, y: event.clientY, armed: false, timer: window.setTimeout(function () {
        if (!hold) return;
        hold.armed = true;
        toggle.dataset.themeHold = 'armed';
      }, HOLD_MS)};
    });
    toggle.addEventListener('pointermove', function (event) {
      if (hold && Math.hypot(event.clientX - hold.x, event.clientY - hold.y) > HOLD_SLOP) endHold();
    });
    toggle.addEventListener('pointerup', function () {
      const armed = !!hold?.armed;
      endHold();
      if (!armed) return;
      held = true;
      if (menu.matches(':popover-open')) return;
      opener = toggle;
      menu.showPopover({source: toggle});
    });
    ['pointercancel', 'pointerleave'].forEach(function (type) { toggle.addEventListener(type, endHold); });
    toggle.addEventListener('contextmenu', function (event) {
      if (handheld.matches) event.preventDefault();
    });
    window.addEventListener('click', function (event) {
      if (!held) return;
      held = false;
      event.preventDefault();
      event.stopImmediatePropagation();
    }, true);
  }

  list.addEventListener('click', function (event) {
    const row = event.target.closest('.dp-theme-picker-option');
    if (!row) return;
    theme.choose(row.dataset.palette);
    menu.hidePopover();
  });

  menu.addEventListener('keydown', function (event) {
    const all = rows();
    const at = all.indexOf(document.activeElement);
    let next = -1;
    if (event.key === 'ArrowDown') next = (at + 1) % all.length;
    else if (event.key === 'ArrowUp') next = (at - 1 + all.length) % all.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = all.length - 1;
    else if (event.key === 'Tab') { menu.hidePopover(); return; }
    if (next < 0) return;
    event.preventDefault();
    all[next].focus();
  });

  window.addEventListener('resize', place);
  window.addEventListener('scroll', place, true);
})();
