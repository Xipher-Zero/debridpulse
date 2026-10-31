/* DebridPulse first-paint theme bootstrap and the one theme state owner.
 *
 * Three choices, each kept apart from the others: the mode, the dark palette
 * and the light palette. Choosing a palette changes only the active mode's
 * palette; the mode toggle (app.js toggleTheme) restores the other mode's own
 * choice. Nothing here ever chooses one mode's palette from the other's.
 *
 * Where the choices live is the server's decision for this page, prepended to
 * this script before it runs (window.debridPulseAppearance, served by
 * /ui-theme-bootstrap.js), so the first paint already has them:
 *   - 'browser': interactive sign-in is off. This browser's localStorage
 *     (`theme`, `dp-theme-palette-dark`, `dp-theme-palette-light`) holds them,
 *     as it always did.
 *   - 'account': the signed-in account's own record on the server. The
 *     browser's keys are never read or written; a choice is applied at once
 *     and saved to the account. When the record could not be read for this
 *     page, the native appearance is shown and the record is asked for again
 *     when the session is next confirmed. This owner makes no request itself:
 *     auth.js, the owner of the authenticated request path, connects the
 *     account's record (DPTheme.connect).
 * Anything else is an account whose appearance is unknown: the native
 * appearance, never a browser choice.
 *
 * DebridPulse Dark and Light are the native palettes: with them the body
 * carries no data-dp-palette and every token keeps its original value
 * (ui-theme-palettes.css declares only the alternatives). A missing or unknown
 * choice -- a retired palette included -- is the native palette of that mode,
 * for that mode only, and is never written back: only a choice is saved.
 *
 * The product logo is drawn here too, inline, so it follows the theme in force
 * (ui-logo.css): its one source, logo.svg, is served with this script
 * (window.debridPulseLogo) and <dp-logo> is defined before the body below is
 * parsed, so the sidebar's mark is drawn at first paint.
 */
(function () {
  'use strict';
  const KEYS = Object.freeze({dark: 'dp-theme-palette-dark', light: 'dp-theme-palette-light'});

  const source = window.debridPulseAppearance || {};
  const browser = source.authority === 'browser';

  // The one palette catalog, each mode's palettes in picker order with the
  // native one first, served with this script from the server's authority
  // (services/account_appearance.py CATALOG), which accepts exactly these IDs.
  // Without it only the native palettes -- the absence of a palette -- exist.
  const NATIVE = {dark: 'debridpulse-dark', light: 'debridpulse-light'};
  const CATALOG = Object.freeze(['dark', 'light'].reduce(function (catalog, mode) {
    const served = Array.isArray(source.catalog && source.catalog[mode]) ? source.catalog[mode] : [];
    const entries = served.length ? served : [{id: NATIVE[mode], label: 'DebridPulse (Default)'}];
    catalog[mode] = Object.freeze(entries.map(function (item) {
      return Object.freeze({id: String(item.id), label: String(item.label)});
    }));
    return catalog;
  }, {}));

  function read(key) {
    try { return localStorage.getItem(key); } catch (_) { return null; }
  }
  function write(key, value) {
    try { localStorage.setItem(key, value); } catch (_) {}
  }
  function known(mode, id) {
    return CATALOG[mode].some(function (item) { return item.id === id; });
  }

  // The choices in force, kept here, so a browser that cannot store them
  // still applies them for this page.
  const stored = browser
    ? {dark_palette: read(KEYS.dark), light_palette: read(KEYS.light)}
    : (source.preferences || {});
  const light = browser ? read('theme') === 'light' : stored.mode === 'light';
  const chosen = {};
  ['dark', 'light'].forEach(function (mode) {
    const id = stored[mode + '_palette'];
    chosen[mode] = known(mode, id) ? id : CATALOG[mode][0].id;
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
  function changed(light) {
    document.dispatchEvent(new CustomEvent('debridpulse:theme-changed', {detail: {light: light}}));
  }

  // ── the account's record ─────────────────────────────────────────────────
  // Whether this page holds the account's record, and which choices were made
  // here since it loaded: a record read later never overrides those.
  let resolved = browser || !!source.preferences;
  const madeHere = new Set();
  // Choices not yet acknowledged by the server, saved one request at a time,
  // so a later choice is never overtaken by an earlier one. A failed save
  // keeps them for the next choice or the next session confirmation; it is
  // reported once until a save succeeds again.
  const unsaved = {};
  let saving = false;
  let failing = false;
  let reading = false;
  // The account's record ({save(changes) -> Promise<ok>, read() -> Promise<record|null>}).
  let store = null;
  // Who waits for every choice made so far to be saved (settled()): told true
  // once the queue is empty and acknowledged, false when a save fails.
  const waiting = [];
  function settle(ok) {
    waiting.splice(0).forEach(function (done) { done(ok); });
  }

  function notify(text, kind) {
    try { window.toast(text, kind); } catch (_) {}
  }

  function save() {
    if (saving) return;
    if (!Object.keys(unsaved).length) { settle(true); return; }
    if (!store) return;
    const body = Object.assign({}, unsaved);
    Object.keys(body).forEach(function (field) { delete unsaved[field]; });
    saving = true;
    store.save(body).then(function (ok) { return ok === true; }, function () { return false; }).then(function (ok) {
      saving = false;
      if (!ok) {
        Object.keys(body).forEach(function (field) { if (!(field in unsaved)) unsaved[field] = body[field]; });
        if (!failing) notify('Theme applied here, but not saved to your account.', 'error');
        failing = true;
        settle(false);
        return;
      }
      if (failing && !Object.keys(unsaved).length) notify('Theme saved to your account.', 'success');
      failing = false;
      save();
    });
  }

  function keep(field, value) {
    madeHere.add(field);
    if (browser) {
      write(field === 'mode' ? 'theme' : KEYS[field.replace('_palette', '')], value);
      return;
    }
    unsaved[field] = value;
    save();
  }

  // The record this page could not read at first paint, read now. It is
  // applied only to this page's own account, and only for the choices not
  // made here since; mode and palette change together, then consumers are
  // told once.
  function resolve() {
    if (resolved || reading || !store) return;
    reading = true;
    store.read().catch(function () { return null; }).then(function (record) {
      reading = false;
      if (resolved || !record || !source.account_id || record.account_id !== source.account_id) return;
      resolved = true;
      const before = mode() + ' ' + chosen[mode()];
      ['dark', 'light'].forEach(function (which) {
        const id = record[which + '_palette'];
        if (!madeHere.has(which + '_palette') && known(which, id)) chosen[which] = id;
      });
      if (!madeHere.has('mode')) document.body.classList.toggle('light', record.mode === 'light');
      paint();
      if (mode() + ' ' + chosen[mode()] !== before) changed(mode() === 'light');
    });
  }

  if (!browser) {
    // auth.js confirms the session at load, periodically and on reconnect.
    document.addEventListener('debridpulse:session-changed', function () {
      resolve();
      save();
    });
  }

  window.DPTheme = Object.freeze({
    catalog: CATALOG,
    mode: mode,
    palette: function (which) { return chosen[which || mode()]; },
    // The mode toggle's one path: the mode and that mode's own palette, in
    // the same frame.
    setMode: function (light) {
      document.body.classList.toggle('light', !!light);
      paint();
      keep('mode', light ? 'light' : 'dark');
    },
    // auth.js hands over the account's record once; the browser authority
    // never takes one.
    connect: function (next) {
      if (browser || store || !next) return;
      store = next;
      resolve();
      save();
    },
    // Resolves true once every choice made here so far is saved to the
    // account (at once while none is waiting), false if one could not be:
    // the one order between this page's choices and anything that must act
    // on the saved theme (ui-theme-picker.js's login theme publication).
    settled: function () {
      if (browser || (!saving && !Object.keys(unsaved).length)) return Promise.resolve(true);
      if (!store) return Promise.resolve(false);
      const result = new Promise(function (done) { waiting.push(done); });
      save();   // a choice left unsaved by a failure is tried again now
      return result;
    },
    // A palette of the active mode, applied before consumers are told.
    choose: function (id) {
      const active = mode();
      if (!known(active, id) || chosen[active] === id) return false;
      chosen[active] = id;
      paint();
      keep(active + '_palette', id);
      changed(active === 'light');
      return true;
    },
  });

  if (light) document.body.classList.add('light');
  paint();

  const LOGO = typeof window.debridPulseLogo === 'string' ? window.debridPulseLogo : '';
  if (LOGO && window.customElements && !customElements.get('dp-logo')) {
    customElements.define('dp-logo', class extends HTMLElement {
      connectedCallback() {
        if (!this.firstElementChild) this.innerHTML = LOGO;
      }
    });
  }
})();
