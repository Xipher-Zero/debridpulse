/* DebridPulse's one localization runtime (window.DPI18n).
 *
 * Every first-party message the interface shows is a key in a catalog
 * (/i18n/<locale>.json, served as /i18n/<locale>.js): English is the
 * canonical source and the fallback for each single key; a key a locale has
 * not translated yet is shown in English and counted as that locale's
 * translation debt (DPI18n.debt()), never the whole page. The server
 * delivers a target's current translations only: a translation made from an
 * older English text is withheld and named as stale (the catalog's third
 * argument), so it too shows the current English and is counted as stale
 * debt, apart from the missing keys. Messages are ICU
 * MessageFormat, formatted by the vendored FormatJS formatter
 * (ui-i18n-formatter.js attaches it): arguments, numbers and dates through
 * Intl, plural categories from the browser's own CLDR data, select, and
 * rich-text tags, which become elements here -- a translation is untrusted
 * text and never markup.
 *
 * The page's language is decided before its first paint: the server prepends
 * window.debridPulseLocale to /ui-theme-bootstrap.js (the account's own
 * choice, the default Admin's while sign-in is off; null when unread), and
 * this parser-blocking script runs right after it, before the body is parsed:
 * it sets the document's lang and dir and, for a page not in English, keeps
 * the page veiled (html[data-dp-locale-pending], ui-i18n.css) until its
 * messages are in place, so English never shows first. A catalog that does
 * not arrive in time leaves the page in English and says so in
 * DPI18n.diagnostics(), atomically: the veil lifts once, on one language.
 *
 * Owners render through DPI18n: t(key, values) for text, rich(key, values)
 * for a fragment with its tags as elements, number(value, digits) for a
 * figure in the shown language's digits and decimal mark (fixed digits, no
 * grouping: in English exactly what toFixed writes), render(root) for markup that
 * names its keys (data-i18n="key" for its text, data-i18n-attr="attr:key;…"
 * for attributes). When the language changes -- the late arrival of the
 * page's catalog, or setLocale(tag), the substrate a later language choice
 * uses -- 'debridpulse:locale-changed' is dispatched and each owner renders
 * its own messages again; nothing here rewrites text it does not own.
 *
 * Pseudo locales (en-XA: accented and ~40% longer; ar-XB: right-to-left)
 * are generated here from English for tests; they are never offered.
 */
(function () {
  'use strict';

  const DEFAULT = 'en';
  const PENDING = 'data-dp-locale-pending';
  const CATALOG_TIMEOUT_MS = 3000;
  const RICH_TAGS = new Set(['b', 'strong', 'em', 'i', 'code', 'kbd']);
  // The FormatJS AST element types (vendor/intl-messageformat.esm.js TYPE).
  const LITERAL = 0, ARGUMENT = 1, SELECT = 5, PLURAL = 6, TAG = 8;

  const stated = window.debridPulseLocale || {};
  const catalogs = new Map();            // tag -> messages
  const staleKeys = new Map();           // tag -> Set of keys withheld as stale (server-named)
  const formatters = new Map();          // `${tag}\0${key}` -> IntlMessageFormat
  const missing = new Map();             // tag -> Set of keys shown in English (untranslated or stale)
  const problems = [];                   // diagnostics: {kind, detail}
  let Formatter = null;                  // IntlMessageFormat, once attached
  let shown = DEFAULT;                   // the language the page is shown in
  let wanted = DEFAULT;                  // the language being made ready
  let directions = {};                   // tag -> 'ltr' | 'rtl' (from the projection)
  let timer = null;
  let resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });

  const pseudo = tag => tag === 'en-XA' || tag === 'ar-XB';

  function note(kind, detail) {
    problems.push({kind, detail: String(detail || '')});
    try { console.warn(`DebridPulse localization: ${kind}`, detail || ''); } catch (_) {}
  }

  // ── catalogs ──────────────────────────────────────────────────────────────
  // A catalog script (/i18n/<tag>.js) registers itself here; `info.stale`
  // names the keys the server withheld because English changed since.
  window.DPI18nCatalog = function register(tag, messages, info = {}) {
    if (typeof tag !== 'string' || !messages || typeof messages !== 'object') return note('invalid catalog', tag);
    catalogs.set(tag, Object.freeze({...messages}));
    staleKeys.set(tag, new Set(Array.isArray(info && info.stale) ? info.stale.map(String) : []));
    advance();
  };

  function loadCatalog(tag, url) {
    if (catalogs.has(tag) || !url) return;
    const script = document.createElement('script');
    script.src = url;
    script.async = true;
    script.addEventListener('error', () => { note('catalog unavailable', tag); fail(tag); });
    document.head.appendChild(script);
  }

  // ── formatting ────────────────────────────────────────────────────────────
  function pseudoLiteral(text, tag) {
    if (tag === 'ar-XB') return text ? `‮${text}‬` : text;
    const map = {a: 'à', c: 'ç', e: 'é', i: 'î', n: 'ñ', o: 'ö', u: 'ü', s: 'š', t: 'ţ', y: 'ý', z: 'ž',
      A: 'À', C: 'Ç', E: 'É', I: 'Î', N: 'Ñ', O: 'Ö', U: 'Ü', S: 'Š', T: 'Ţ', Y: 'Ý', Z: 'Ž'};
    return text.replace(/[aceinoustyzACEINOUSTYZ]/g, ch => map[ch]);
  }

  // Transform an English AST's literal text only; arguments, plural and
  // select branches and tags keep their structure and names.
  function pseudoAst(elements, tag) {
    return elements.map(element => {
      if (element.type === LITERAL) return {...element, value: pseudoLiteral(element.value, tag)};
      if (element.type === TAG) return {...element, children: pseudoAst(element.children, tag)};
      if (element.type === SELECT || element.type === PLURAL) {
        const options = {};
        for (const [name, option] of Object.entries(element.options)) {
          options[name] = {...option, value: pseudoAst(option.value, tag)};
        }
        return {...element, options};
      }
      return element;
    }).flatMap(element => (tag === 'ar-XB' && element.type === ARGUMENT
      ? [{type: LITERAL, value: '⁨'}, element, {type: LITERAL, value: '⁩'}] : [element]));
  }

  function letters(elements) {
    return elements.reduce((sum, element) => sum + (element.type === LITERAL ? element.value.replace(/\s/g, '').length
      : element.type === TAG ? letters(element.children) : 0), 0);
  }

  function formatter(tag, key) {
    const id = `${tag}\0${key}`;
    if (formatters.has(id)) return formatters.get(id);
    let made = null;
    const english = catalogs.get(DEFAULT)?.[key];
    try {
      if (pseudo(tag)) {
        if (english !== undefined) {
          const ast = new Formatter(english, DEFAULT).getAst();
          let elements = pseudoAst(ast, tag);
          if (tag === 'en-XA') {
            const pad = '·'.repeat(Math.max(1, Math.round(letters(ast) * 0.4)));
            elements = [{type: LITERAL, value: '⟦'}, ...elements, {type: LITERAL, value: ` ${pad}⟧`}];
          }
          made = new Formatter(elements, DEFAULT);
        }
      } else {
        const own = catalogs.get(tag)?.[key];
        if (own !== undefined) made = new Formatter(own, tag);
        else if (english !== undefined) {
          if (tag !== DEFAULT) (missing.get(tag) || missing.set(tag, new Set()).get(tag)).add(key);
          made = new Formatter(english, DEFAULT);
        }
      }
    } catch (error) {
      note('invalid message', `${tag} ${key}: ${error && error.message}`);
      made = english !== undefined && tag !== DEFAULT ? new Formatter(english, DEFAULT) : null;
    }
    if (!made) note('missing key', key);
    formatters.set(id, made);
    return made;
  }

  const numberFormats = new Map();         // `${tag}\0${digits}` -> Intl.NumberFormat

  /* A figure with exactly `digits` decimals, in the shown language (a pseudo
   * locale writes English figures). */
  function number(value, digits = 0) {
    const n = Number(value);
    const tag = pseudo(shown) ? DEFAULT : shown;
    const id = `${tag}\0${digits}`;
    if (!numberFormats.has(id)) {
      const options = {minimumFractionDigits: digits, maximumFractionDigits: digits, useGrouping: false};
      let made;
      try { made = new Intl.NumberFormat(tag, options); } catch (_) { made = new Intl.NumberFormat(DEFAULT, options); }
      numberFormats.set(id, made);
    }
    return numberFormats.get(id).format(n === 0 ? 0 : n);      // never "-0"
  }

  const plainTags = () => Object.fromEntries([...RICH_TAGS].map(name => [name, chunks => chunks.join('')]));

  /* A message as plain text: rich-text tags keep their text only. */
  function t(key, values = {}) {
    const made = Formatter && formatter(shown, key);
    if (!made) return Formatter ? key : '';
    try {
      const out = made.format({...plainTags(), ...values});
      return Array.isArray(out) ? out.join('') : String(out);
    } catch (error) {
      note('format failed', `${key}: ${error && error.message}`);
      return key;
    }
  }

  /* A message as a fragment: its tags become those elements (b, strong, em,
   * i, code, kbd) and every text is a text node; a value that is a Node is
   * placed as it is (the owner built it). */
  function rich(key, values = {}) {
    const fragment = document.createDocumentFragment();
    const made = Formatter && formatter(shown, key);
    if (!made) { fragment.append(Formatter ? key : ''); return fragment; }
    const wrap = name => chunks => {
      const element = document.createElement(name);
      element.append(...chunks.map(chunk => (chunk instanceof Node ? chunk : String(chunk))));
      return element;
    };
    const tags = Object.fromEntries([...RICH_TAGS].map(name => [name, wrap(name)]));
    try {
      const out = made.format({...tags, ...values});
      (Array.isArray(out) ? out : [out]).forEach(part => fragment.append(part instanceof Node ? part : String(part)));
    } catch (error) {
      note('format failed', `${key}: ${error && error.message}`);
      fragment.append(key);
    }
    return fragment;
  }

  /* Render markup that names its keys: data-i18n (its text; with
   * data-i18n-rich, its rich text; with data-i18n-values, a JSON object of
   * its arguments) and data-i18n-attr ("attr:key;…"). */
  function render(root = document) {
    if (!Formatter || !root) return;
    const nodes = [];
    if (root.nodeType === 1 && root.matches('[data-i18n],[data-i18n-attr]')) nodes.push(root);
    if (root.querySelectorAll) nodes.push(...root.querySelectorAll('[data-i18n],[data-i18n-attr]'));
    nodes.forEach(node => {
      const key = node.getAttribute('data-i18n');
      // A control showing its pending state (data-pending, app.js
      // setButtonPending) keeps its words: its owner says that state in the
      // new language. Its attributes (its idle label, its tooltip) follow here.
      if (key && !node.closest('[data-pending="1"]')) {
        let values = {};
        if (node.hasAttribute('data-i18n-values')) {
          try { values = JSON.parse(node.getAttribute('data-i18n-values')) || {}; } catch (_) { note('invalid values', key); }
        }
        if (node.hasAttribute('data-i18n-rich')) node.replaceChildren(rich(key, values));
        else node.textContent = t(key, values);
      }
      for (const pair of String(node.getAttribute('data-i18n-attr') || '').split(';')) {
        const [attribute, attributeKey] = pair.split(':').map(part => part && part.trim());
        if (attribute && attributeKey) node.setAttribute(attribute, t(attributeKey));
      }
    });
  }

  // ── the page's language ───────────────────────────────────────────────────
  function direction(tag) {
    return directions[tag] || (tag === 'ar-XB' || tag === 'ar' ? 'rtl' : 'ltr');
  }

  function setDocument(tag) {
    const root = document.documentElement;
    root.lang = tag;
    root.dir = direction(tag);
  }

  function fail(tag) {
    if (wanted !== tag) return;
    wanted = DEFAULT;
    advance();
  }

  // Ready to show `wanted` once the formatter, English and that language are
  // here (a pseudo locale needs English only). Applied once, atomically.
  function advance() {
    if (!Formatter || !catalogs.has(DEFAULT)) return;
    if (wanted !== DEFAULT && !pseudo(wanted) && !catalogs.has(wanted)) return;
    if (timer) { clearTimeout(timer); timer = null; }
    const previous = shown;
    shown = wanted;
    formatters.clear();
    setDocument(shown);
    render(document);
    document.documentElement.removeAttribute(PENDING);
    resolveReady(shown);
    if (previous !== shown) {
      document.dispatchEvent(new CustomEvent('debridpulse:locale-changed', {detail: {locale: shown, previous}}));
    }
  }

  /* Show the page in `tag` from now on (the substrate a later language
   * choice will use; it saves nothing). `catalog` is that locale's asset
   * URL where it has one. Resolves with the language actually shown. */
  function setLocale(tag, {catalog = null, dir = null} = {}) {
    if (dir) directions = {...directions, [tag]: dir};
    wanted = tag;
    if (tag !== DEFAULT && !pseudo(tag) && !catalogs.has(tag)) {
      if (!catalog) { note('no catalog', tag); wanted = DEFAULT; }
      else {
        loadCatalog(tag, catalog);
        if (timer) clearTimeout(timer);
        timer = setTimeout(() => { note('catalog timed out', tag); fail(tag); }, CATALOG_TIMEOUT_MS);
      }
    }
    advance();
    return new Promise(resolve => {
      const check = () => (shown === wanted || wanted === DEFAULT && shown === DEFAULT
        ? resolve(shown) : setTimeout(check, 20));
      check();
    });
  }

  // Before the body is parsed: the language the server stated for this page.
  (function firstPaint() {
    const tag = typeof stated.locale === 'string' ? stated.locale : DEFAULT;
    if (stated.dir) directions[tag] = stated.dir;
    const shows = tag === DEFAULT ? DEFAULT
      : stated.status === 'pseudo' ? tag
        : stated.catalog ? tag : DEFAULT;          // a locale without a catalog is English, key by key
    wanted = shows;
    if (shows !== DEFAULT) {
      setDocument(shows);
      document.documentElement.setAttribute(PENDING, '');
      if (stated.catalog) loadCatalog(shows, stated.catalog);
      timer = setTimeout(() => { note('catalog timed out', shows); fail(shows); }, CATALOG_TIMEOUT_MS);
    }
  })();

  window.DPI18n = Object.freeze({
    t,
    rich,
    number,
    render,
    setLocale,
    ready,
    has: key => Boolean(catalogs.get(DEFAULT) && Object.prototype.hasOwnProperty.call(catalogs.get(DEFAULT), key)),
    locale: () => shown,
    /* The tag for Intl formatters (a pseudo locale formats as English). */
    intlLocale: () => (pseudo(shown) ? DEFAULT : shown),
    preference: () => (typeof stated.locale === 'string' ? stated.locale : null),
    attachFormatter(IntlMessageFormat) {
      if (Formatter || typeof IntlMessageFormat !== 'function') return;
      Formatter = IntlMessageFormat;
      advance();
    },
    /* Keys shown in English so far, per locale: {missing, stale}. */
    debt: () => Object.fromEntries([...missing].map(([tag, keys]) => {
      const stale = staleKeys.get(tag) || new Set();
      const sorted = [...keys].sort();
      return [tag, {missing: sorted.filter(key => !stale.has(key)), stale: sorted.filter(key => stale.has(key))}];
    })),
    diagnostics: () => problems.slice(),
  });
})();
