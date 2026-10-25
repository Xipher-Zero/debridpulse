/* DebridPulse first-paint theme bootstrap and the one theme state owner.
 *
 * Three browser-local choices, each kept apart from the others: the mode
 * (localStorage `theme`, as it always was), the dark palette and the light
 * palette. Choosing a palette changes only the active mode's palette; the
 * mode toggle (app.js toggleTheme) restores the other mode's own choice.
 * Nothing here ever chooses one mode's palette from the other's.
 *
 * DebridPulse Dark and Light are the native palettes: with them the body
 * carries no data-dp-palette and every token keeps its original value
 * (ui-theme-palettes.css declares only the alternatives). A missing, unknown
 * or unreadable stored choice is the native palette of that mode.
 */
(function () {
  'use strict';
  const palette = (id, label) => Object.freeze({id: id, label: label});
  const CATALOG = Object.freeze({
    dark: Object.freeze([
      palette('debridpulse-dark', 'DebridPulse (Default)'),
      palette('catppuccin-mocha', 'Catppuccin Mocha'),
      palette('dracula', 'Dracula'),
      palette('nord-dark', 'Nord Dark'),
      palette('gruvbox-dark', 'Gruvbox Dark'),
      palette('solarized-dark', 'Solarized Dark'),
      palette('tokyo-night', 'Tokyo Night'),
    ]),
    light: Object.freeze([
      palette('debridpulse-light', 'DebridPulse (Default)'),
      palette('catppuccin-latte', 'Catppuccin Latte'),
      palette('nord-light', 'Nord Light'),
      palette('gruvbox-light', 'Gruvbox Light'),
      palette('solarized-light', 'Solarized Light'),
      palette('tokyo-night-day', 'Tokyo Night Day'),
    ]),
  });
  const KEYS = Object.freeze({dark: 'dp-theme-palette-dark', light: 'dp-theme-palette-light'});

  function read(key) {
    try { return localStorage.getItem(key); } catch (_) { return null; }
  }
  function write(key, value) {
    try { localStorage.setItem(key, value); } catch (_) {}
  }
  function known(mode, id) {
    return CATALOG[mode].some(function (item) { return item.id === id; });
  }

  // The choices in force: read once, then kept here, so a browser that cannot
  // store them still applies them for this page.
  const chosen = {};
  ['dark', 'light'].forEach(function (mode) {
    const stored = read(KEYS[mode]);
    chosen[mode] = known(mode, stored) ? stored : CATALOG[mode][0].id;
  });

  function mode() {
    return document.body.classList.contains('light') ? 'light' : 'dark';
  }
  // The body names the active mode's palette, or nothing for the native one.
  function paint() {
    const active = mode();
    if (chosen[active] === CATALOG[active][0].id) document.body.removeAttribute('data-dp-palette');
    else document.body.setAttribute('data-dp-palette', chosen[active]);
  }

  window.DPTheme = Object.freeze({
    catalog: CATALOG,
    mode: mode,
    palette: function (which) { return chosen[which || mode()]; },
    // The mode toggle's one path: the mode and that mode's own palette, in
    // the same frame.
    setMode: function (light) {
      document.body.classList.toggle('light', !!light);
      write('theme', light ? 'light' : 'dark');
      paint();
    },
    // A palette of the active mode, applied before consumers are told.
    choose: function (id) {
      const active = mode();
      if (!known(active, id) || chosen[active] === id) return false;
      chosen[active] = id;
      write(KEYS[active], id);
      paint();
      document.dispatchEvent(new CustomEvent('debridpulse:theme-changed', {detail: {light: active === 'light'}}));
      return true;
    },
  });

  if (read('theme') === 'light') document.body.classList.add('light');
  paint();
})();
