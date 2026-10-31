/* DebridPulse theme picker: the chevron beside the mode toggle and its menu
 * of the active mode's palettes. ui-theme-bootstrap.js (window.DPTheme) owns
 * the catalog, the choices and their application; this file only presents
 * them. The menu is a popover in the top layer, so opening it never moves the
 * topbar; it opens and closes through the shell's one sheet lifetime (app.js
 * DPShellSheet), which gives it its dismissal by a press outside it and by
 * Escape, focus return to the chevron, and below 780px its whole roll back
 * up before it hides.
 *
 * Below 780px (the handheld shell) the same menu is a bottom sheet
 * (ui-theme-picker.css): the popover's own box is its scrim, so a press beside
 * the panel closes it without reaching the page; the chevron stays its
 * visible, separate control, and a long press on the mode toggle opens it too
 * (on the lift) -- that press never also toggles the mode.
 *
 * For an Admin the menu ends with the Login Theme: the theme the sign-in page
 * shows (the installation's, read from the server each time the menu opens)
 * and Set current, which publishes the theme this page shows. Choosing a row
 * or the mode never publishes anything. Set current waits for this page's
 * choices to be saved (DPTheme.settled) and sends the pair it showed when
 * pressed; the server publishes it only if that is the pair saved for the
 * Admin, and only the server's answer changes what is shown here.
 */
(function () {
  'use strict';
  const trigger = document.getElementById('theme-picker-toggle');
  const menu = document.getElementById('theme-picker');
  const list = document.getElementById('theme-picker-list');
  if (!trigger || !menu || !list || !window.DPTheme || !window.DPShellSheet) return;
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

  // ── the Login Theme (an Admin's menu only) ───────────────────────────────
  const authority = window.debridPulseAuthority || {};
  const auth = window.debridPulseAuth;
  const login = auth && authority.authenticated && Array.isArray(authority.capabilities)
    && authority.capabilities.includes('administer') ? loginSection() : null;
  // What the server last said is published ({mode, palette}), whether it
  // could be read, a publication in flight, and the last refusal to report.
  let published = null;
  let readState = 'reading';
  let reads = 0;
  let publishing = false;
  let note = '';

  function loginSection() {
    const section = document.createElement('div');
    section.className = 'dp-theme-picker-login';
    section.setAttribute('role', 'group');
    section.setAttribute('aria-labelledby', 'theme-picker-login-title');
    section.innerHTML = '<div class="dp-theme-picker-login-title" id="theme-picker-login-title">Login Theme</div>'
      + '<div class="dp-theme-picker-login-state"><span class="dp-theme-picker-login-label">Current login theme</span>'
      + '<span class="dp-theme-picker-login-value" aria-live="polite"></span></div>'
      + '<button type="button" class="dp-btn dp-btn--secondary dp-btn--sm dp-theme-picker-login-set" role="menuitem">Set current</button>'
      + '<div class="dp-theme-picker-login-note" role="status"></div>';
    list.after(section);
    section.querySelector('button').addEventListener('click', publish);
    return section;
  }

  function shown() { return {mode: theme.mode(), palette: theme.palette()}; }
  function named(pair) {
    const item = (theme.catalog[pair.mode] || []).find(function (entry) { return entry.id === pair.palette; });
    return (item ? item.label : pair.palette) + ' \u00b7 ' + (pair.mode === 'light' ? 'Light' : 'Dark');
  }
  function paintLogin() {
    if (!login) return;
    const button = login.querySelector('button');
    const now = shown();
    const current = readState === 'ready' && published && published.mode === now.mode && published.palette === now.palette;
    login.querySelector('.dp-theme-picker-login-value').textContent =
      readState === 'ready' && published ? named(published) : readState === 'reading' ? 'Checking\u2026' : 'Unavailable';
    login.querySelector('.dp-theme-picker-login-note').textContent = note;
    button.textContent = publishing ? 'Setting\u2026' : current ? 'Current' : 'Set current';
    const off = publishing || !!current;
    // A control that loses focus as it is disabled hands it to the menu's
    // chosen row, so the keyboard stays in the menu.
    if (off && document.activeElement === button) (rows().find(function (row) { return row.getAttribute('aria-checked') === 'true'; }) || rows()[0])?.focus();
    button.disabled = off;
  }
  function readPublished() {
    if (!login) return Promise.resolve();
    const read = ++reads;
    readState = 'reading';
    paintLogin();
    return auth.fetch('/api/login-appearance').then(function (response) {
      return response.ok ? response.json() : null;
    }).catch(function () { return null; }).then(function (pair) {
      if (read !== reads) return;
      published = pair && pair.mode && pair.palette ? pair : null;
      readState = published ? 'ready' : 'unavailable';
      paintLogin();
    });
  }
  function publish() {
    if (publishing) return;
    const pair = shown();
    publishing = true;
    note = '';
    paintLogin();
    theme.settled().then(function (saved) {
      if (!saved) return 'Your theme is not saved yet, so the login theme is unchanged.';
      return auth.fetch('/api/login-appearance', {method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(pair)}).then(function (response) {
        if (!response.ok) {
          return response.status === 409 ? 'Your theme changed before it was saved, so the login theme is unchanged.'
            : 'The login theme could not be set; it is unchanged.';
        }
        return response.json().then(function (answer) {
          reads += 1;   // the server's answer is newer than any read in flight
          published = answer;
          readState = 'ready';
          return '';
        });
      });
    }).catch(function () {
      return 'The login theme could not be set; it is unchanged.';
    }).then(function (message) {
      publishing = false;
      note = message;
      // After a refusal, show what the sign-in page actually shows now.
      if (message) readPublished();
      else paintLogin();
    });
  }

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

  const sheets = window.DPShellSheet;
  // The control that opened the menu: focus returns there when it closes
  // (and when a press on the scrim, which took focus out of the sheet, does).
  let opener = trigger;
  function show(from) {
    opener = from;
    sheets.open(menu, {opener: from, control: trigger});
  }
  function close(options) { return sheets.close(menu, options); }

  menu.addEventListener('beforetoggle', function (event) {
    if (event.newState !== 'open') return;
    render();
    note = '';
    readPublished();
  });
  document.addEventListener('debridpulse:theme-changed', paintLogin);
  menu.addEventListener('toggle', function (event) {
    if (event.newState !== 'open') return;
    place();
    (rows().find(function (row) { return row.getAttribute('aria-checked') === 'true'; }) || rows()[0])?.focus();
  });
  trigger.addEventListener('click', function () {
    if (sheets.isOpen(menu)) close();
    else show(trigger);
  });

  menu.addEventListener('click', function (event) {
    if (!handheld.matches || event.target !== menu) return;
    const back = opener;
    close();
    back.focus();
  });
  handheld.addEventListener('change', function () { close({immediate: true}); });

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
      if (sheets.isOpen(menu)) return;
      show(toggle);
    });
    ['pointercancel', 'pointerleave'].forEach(function (type) { toggle.addEventListener(type, endHold); });
    // A tap on the mode toggle while the menu is open (after a long press the
    // toggle is its opener, so a press on it is not a press outside the menu)
    // closes the menu as the mode changes: the header's controls stay usable,
    // one menu at a time.
    toggle.addEventListener('click', function () {
      if (handheld.matches) close();
    });
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
    close();
  });

  menu.addEventListener('keydown', function (event) {
    const all = rows();
    const set = login && login.querySelector('button');
    if (set && !set.disabled) all.push(set);
    const at = all.indexOf(document.activeElement);
    let next = -1;
    if (event.key === 'ArrowDown') next = (at + 1) % all.length;
    else if (event.key === 'ArrowUp') next = (at - 1 + all.length) % all.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = all.length - 1;
    else if (event.key === 'Tab') { close(); return; }
    if (next < 0) return;
    event.preventDefault();
    all[next].focus();
  });

  window.addEventListener('resize', place);
  window.addEventListener('scroll', place, true);
})();
