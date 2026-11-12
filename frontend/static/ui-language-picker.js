/* DebridPulse's one language list (window.DPLanguagePicker): the languages
 * an account or the installation may choose, and how each is shown -- its
 * flag and its own (native) name -- for both places that choose one: My
 * Account (auth.js, the account's own language, saved at once) and Users &
 * Access → Deployment & Language (ui-settings-page.js, the installation's
 * default, saved by Apply). Each caller owns its saving; this owns the list,
 * its rows, their selection mark, keyboard and scrolling, and nothing else.
 *
 * The choices are the locale manifest's (/i18n/manifest.json, the one
 * locale registry): English and every target it marks `supported`, in its
 * order -- never a `planned` or pseudo one. The server decides which language
 * an account is shown in (GET /api/account/locale); the list only offers.
 * A flag is a display mark beside the name, fixed per tag, never a region
 * choice; it is hidden from assistive technology, so a row is named by its
 * language alone, and a name is its own language run (lang, dir), so an
 * Arabic name reads right to left inside a left-to-right list.
 */
(function () {
  'use strict';

  const FLAGS = Object.freeze({
    en: '🇺🇸', de: '🇩🇪', fr: '🇫🇷', es: '🇪🇸', it: '🇮🇹', nl: '🇳🇱', 'pt-BR': '🇧🇷', 'pt-PT': '🇵🇹',
    pl: '🇵🇱', ru: '🇷🇺', uk: '🇺🇦', tr: '🇹🇷', id: '🇮🇩', hi: '🇮🇳', ja: '🇯🇵', ko: '🇰🇷',
    'zh-Hans': '🇨🇳', 'zh-Hant': '🇹🇼', ar: '🇸🇦',
  });
  const OFFERED = new Set(['canonical', 'supported']);
  const CHECK = '<svg class="lucide dp-utility-icon" xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M20 6 9 17l-5-5"/></svg>';
  const t = (key, values) => window.DPI18n.t(key, values);

  let manifest = null;

  /* The manifest's locales, read once per page (a failed read is tried again),
   * always revalidated with the server (no-cache: an unchanged file is a 304),
   * so a changed admission -- a language activated or withdrawn -- is the list
   * the next page offers, never a copy a browser kept. */
  function locales() {
    if (!manifest) {
      manifest = fetch('/i18n/manifest.json', {credentials: 'same-origin', cache: 'no-cache', headers: {Accept: 'application/json'}})
        .then(response => (response.ok ? response.json() : Promise.reject(new Error(String(response.status)))))
        .then(document => document.locales || {})
        .catch(error => { manifest = null; throw error; });
    }
    return manifest;
  }

  /* The languages that may be chosen, in the manifest's order: [{tag, native, dir}]. */
  async function choices() {
    return Object.entries(await locales())
      .filter(([, info]) => OFFERED.has(info.status))
      .map(([tag, info]) => ({tag, native: String(info.native || tag), dir: info.dir === 'rtl' ? 'rtl' : 'ltr'}));
  }

  /* A language as it is shown: its flag, then its own name as its own run. */
  function face(language) {
    const fragment = document.createDocumentFragment();
    const flag = document.createElement('span');
    flag.className = 'dp-language-flag';
    flag.setAttribute('aria-hidden', 'true');
    flag.textContent = FLAGS[language.tag] || '';
    const name = document.createElement('span');
    name.className = 'dp-language-name';
    name.lang = language.tag;
    name.dir = language.dir;
    name.textContent = language.native;
    fragment.append(flag, name);
    return fragment;
  }

  /* The language of a tag, from the manifest's entry, or English. */
  async function language(tag) {
    const all = await locales();
    const known = all[tag] ? tag : 'en';
    const info = all[known] || {};
    return {tag: known, native: String(info.native || known), dir: info.dir === 'rtl' ? 'rtl' : 'ltr'};
  }

  /* A button's content: the flag and native name only; its accessible name
   * is `label` (said with the language's name by its caller). */
  function paintButton(button, chosen, label) {
    button.replaceChildren(face(chosen));
    button.dataset.language = chosen.tag;
    button.setAttribute('aria-label', label);
  }

  /* Show a language in the page from now on, through the one runtime. */
  function apply(chosen) {
    return window.DPI18n.setLocale(chosen.tag, {
      catalog: chosen.tag === 'en' ? null : `/i18n/${encodeURIComponent(chosen.tag)}.js`, dir: chosen.dir});
  }

  /* The list, into `container`: one radio group, a row per language (and,
   * first, `systemDefault` -- {language} -- where the caller offers it, its
   * value `null`). `selected` is the chosen tag, or null for the default.
   * `onChoose(value)` is the caller's. Returns {setSelected, setBusy, focus}. */
  function list(container, {languages, selected = null, systemDefault = null, label, onChoose}) {
    const group = document.createElement('div');
    group.className = 'dp-language-list';
    group.setAttribute('role', 'radiogroup');
    group.setAttribute('aria-label', label);
    const rows = [];
    let busy = false;

    const row = (value, content) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'dp-language-option';
      item.setAttribute('role', 'radio');
      item.dataset.value = value === null ? '' : value;
      const body = document.createElement('span');
      body.className = 'dp-language-option-body';
      body.append(content);
      const mark = document.createElement('span');
      mark.className = 'dp-language-check';
      mark.innerHTML = CHECK;
      item.append(body, mark);
      item.addEventListener('click', () => { if (!busy) onChoose(value); });
      rows.push({value, item});
      group.append(item);
    };

    if (systemDefault) {
      const content = document.createDocumentFragment();
      const name = document.createElement('span');
      name.className = 'dp-language-system';
      name.dataset.i18n = 'language.picker.system_default';
      name.textContent = t('language.picker.system_default');
      const current = document.createElement('span');
      current.className = 'dp-language-system-current';
      current.append(face(systemDefault.language));
      content.append(name, current);
      row(null, content);
    }
    languages.forEach(entry => row(entry.tag, face(entry)));

    function setSelected(value) {
      rows.forEach(({value: own, item}) => {
        const checked = own === value;
        item.setAttribute('aria-checked', String(checked));
        item.tabIndex = checked ? 0 : -1;
      });
      if (!rows.some(({item}) => item.tabIndex === 0) && rows[0]) rows[0].item.tabIndex = 0;
    }

    // One tab stop; the arrows, Home and End move along the list.
    group.addEventListener('keydown', event => {
      const index = rows.findIndex(({item}) => item === document.activeElement);
      if (index < 0) return;
      const next = {ArrowDown: index + 1, ArrowRight: index + 1, ArrowUp: index - 1, ArrowLeft: index - 1,
        Home: 0, End: rows.length - 1}[event.key];
      if (next === undefined) return;
      event.preventDefault();
      const target = rows[Math.max(0, Math.min(rows.length - 1, next))].item;
      rows.forEach(({item}) => { item.tabIndex = item === target ? 0 : -1; });
      target.focus();
    });

    setSelected(selected);
    container.replaceChildren(group);
    return {
      setSelected,
      setBusy(value) {
        busy = Boolean(value);
        group.setAttribute('aria-busy', String(busy));
        rows.forEach(({item}) => item.setAttribute('aria-disabled', String(busy)));
      },
      focus() {
        const current = rows.find(({item}) => item.tabIndex === 0) || rows[0];
        if (!current) return;
        current.item.focus();
        current.item.scrollIntoView({block: 'nearest'});
      },
    };
  }

  window.DPLanguagePicker = Object.freeze({choices, language, face, paintButton, apply, list, FLAGS});
})();
