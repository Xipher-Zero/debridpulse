// @ts-check
const {test, expect} = require('@playwright/test');
const fs = require('fs');
const path = require('path');

/* DebridPulse 1.0.14 localization, WS5: the locale probe a language candidate
 * is qualified with. One locale (DP_LOCALE; the test-only pseudo locale en-XA
 * unless given) is shown on the instance's own reader -- its role and
 * deployment type -- across the shell pages, every Settings tab it may open,
 * the Field Manual's sections and every Settings Help topic for each reader
 * context, and compared with English on the same page.
 *
 * Three kinds of outcome, kept apart:
 *  - catalog or semantic failures (they fail the test): a message shown in
 *    English that the locale translates, a key shown as itself, keys the locale
 *    left to English (DPI18n.debt), runtime notes, a critical warning not in
 *    the locale, a Help control reference whose bold label is not the control's
 *    own translation, or a language that changes what exists -- navigation,
 *    tabs, Help passages, a topic's parts or the role table's answers;
 *  - geometry (recorded, never failing here; WS6 owns layout): horizontal
 *    overflow and clipped geometry-noted labels at 320 px, attached as
 *    locale-geometry.json;
 *  - test infrastructure (failing, marked INFRASTRUCTURE): the locale's catalog
 *    cannot be loaded, so nothing about the application is concluded.
 *
 * It reads the catalog, its metadata and the Help label map from this
 * checkout (frontend/), so it qualifies these exact files. */

const ROOT = path.resolve(__dirname, '..');
const LOCALE = process.env.DP_LOCALE || 'en-XA';
const read = file => JSON.parse(fs.readFileSync(path.join(ROOT, file), 'utf8'));
const MANIFEST = read('static/i18n/manifest.json');
const ENGLISH = read('static/i18n/en.json').messages;
const META = read('i18n/en.meta.json').meta;
const LABELS = read('i18n/en.labels.json').spans;
const PROTECTED = read('i18n/terms/en.json').protected.filter(each => each.kind === 'name').map(each => each.text);
const EASY = ['activity-log', 'provider-additional-settings', 'download-behavior-advanced', 'transfer-method-settings',
  'disk-space-recovery', 'download-engine-activity', 'username-password', 'openid-connect', 'deployment-type',
  'statistics-reporting', 'event-logging', 'backup-additional-settings', 'database-reset'];
const ROLES = {admin: [true, false, true, true], user: [false, true, true, true], readonly: [false, false, false, false]};

/* English as it shows when a message has no arguments: text -> its keys. A
 * message that is only protected names (a product name) is not English. */
const ENGLISH_TEXT = {};
for (const [key, message] of Object.entries(ENGLISH)) {
  if (/[{}]/.test(message)) continue;
  const text = message.replace(/<\/?[a-z]+>/g, '').trim();
  let rest = text;
  for (const name of PROTECTED) rest = rest.split(name).join('');
  if (/\p{L}{2,}/u.test(rest)) (ENGLISH_TEXT[text] = ENGLISH_TEXT[text] || []).push(key);
}
const CRITICAL = Object.keys(META).filter(key => META[key].criticality === 'critical-warning');
const GEOMETRY = Object.keys(META).filter(key => META[key].geometry);
// Where the probe also writes what it found (Playwright's line reporter keeps no attachments).
const record = (name, body) => {
  if (!process.env.DP_PROBE_OUT) return;
  const port = new URL(process.env.DP_BASE_URL || 'http://localhost').port || 'default';
  fs.mkdirSync(process.env.DP_PROBE_OUT, {recursive: true});
  fs.writeFileSync(path.join(process.env.DP_PROBE_OUT, `${LOCALE}-${port}-${name}.json`), JSON.stringify(body, null, 1));
};
const CONTROLS = Object.fromEntries(Object.entries(LABELS).map(([key, spans]) => [key,
  spans.filter(span => span.kind === 'control').map(span => ({keys: span.keys || [span.key], index: spans.indexOf(span)}))]).filter(([, c]) => c.length));

async function show(page, locale) {
  const info = MANIFEST.locales[locale];
  if (!info) throw new Error(`INFRASTRUCTURE: ${locale} is not a manifest locale`);
  if (locale === 'en' || info.status === 'pseudo') return page.evaluate(tag => window.DPI18n.setLocale(tag), locale);
  const asset = await page.request.get(`/i18n/${locale}.js`);
  if (!asset.ok()) throw new Error(`INFRASTRUCTURE: /i18n/${locale}.js answered ${asset.status()}; the locale's catalog is not served`);
  const shown = await page.evaluate(([tag, dir]) => window.DPI18n.setLocale(tag, {catalog: `/i18n/${tag}.js`, dir}), [locale, info.dir]);
  if (shown !== locale) throw new Error(`INFRASTRUCTURE: the page shows ${shown}, not ${locale}: ${JSON.stringify(await page.evaluate(() => window.DPI18n.diagnostics()))}`);
  return shown;
}

async function boot(page, width) {
  await page.setViewportSize({width, height: 900});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await page.evaluate(() => window.DPI18n.ready);
}
const go = (page, view) => page.locator(`#sidebar .nav-item[data-view="${view}"]`).evaluate(element => element.click());
// Finite animations finished (a status pulse runs forever and is not waited for).
const settled = page => page.waitForFunction(() => document.getAnimations()
  .every(animation => animation.playState !== 'running' || animation.effect?.getComputedTiming().iterations === Infinity), null, {timeout: 10_000});
const stage = (...what) => console.log(`[probe ${LOCALE}]`, ...what);

/* What the shown surface says: English it should not, keys as themselves,
 * critical keyed text left in English, and Help control labels. */
const inspect = (page, root) => page.evaluate(({root, englishText, critical, controls, english, pseudo}) => {
  const scope = document.querySelector(root) || document.body;
  const seen = element => element.getClientRects().length > 0 && getComputedStyle(element).visibility !== 'hidden';
  const t = key => window.DPI18n.t(key);
  const translated = text => (englishText[text] || []).every(key => t(key) !== text);
  const found = {english: [], keys: [], critical: [], labels: []};
  const elements = [...scope.querySelectorAll('*')].filter(element => !element.closest('svg, script, style') && seen(element));
  for (const element of elements) {
    const own = [...element.childNodes].filter(node => node.nodeType === 3).map(node => node.textContent).join('').trim();
    const whole = element.textContent.trim();
    if (whole && englishText[whole] && translated(whole) && ![...element.children].some(child => child.textContent.trim() === whole)) {
      found.english.push({text: whole, keys: englishText[whole]});
    }
    if (own && /^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$/.test(own) && window.DPI18n.has(own)) found.keys.push(own);
    for (const name of ['aria-label', 'title', 'placeholder', 'data-tooltip']) {
      const value = (element.getAttribute(name) || '').trim();
      if (value && englishText[value] && translated(value)) found.english.push({text: value, attribute: name, keys: englishText[value]});
    }
    const key = element.dataset.i18n;
    if (key && critical.includes(key) && !element.dataset.i18nValues && element.textContent.trim() === english[key]?.replace(/<\/?[a-z]+>/g, '')) {
      found.critical.push(key);
    }
    if (key && controls[key]) {
      const bold = [...element.querySelectorAll('b')].map(b => b.textContent.trim());
      for (const control of controls[key]) {
        // A pseudo locale wraps each message whole (⟦ … ··⟧, RLO … PDF): a label inside a sentence is compared unwrapped.
        const label = control.keys.map(t).map(text => (pseudo ? text.replace(/[⟦⟧·\u202e\u202c]/g, '') : text).trim());
        const shown = bold.some(text => (pseudo ? text.replace(/[⟦⟧·\u202e\u202c]/g, '').trim() : text).replace(/[\s  ]*[:：]$/, '').split(/\s*[→←]\s*/).join('\u0000') === label.join('\u0000'));
        if (!shown) found.labels.push({key, span: control.index, expected: label.join(' → '), bold});
      }
    }
  }
  return found;
}, {root, englishText: ENGLISH_TEXT, critical: CRITICAL, controls: CONTROLS, english: ENGLISH, pseudo: MANIFEST.locales[LOCALE]?.status === 'pseudo'});

/* Which things exist: navigation, Settings tabs, the manual's passages. */
const existing = page => page.evaluate(() => ({
  nav: [...document.querySelectorAll('#sidebar .nav-item[data-view]')].filter(e => e.getClientRects().length).map(e => e.dataset.view),
  context: (({role, deployment, mode}) => ({role, deployment, mode}))(window.debridPulseAuth.presentation.context() || {}),
}));

test(`L18-L21 locale probe (${LOCALE}): every surface the reader has is said in the locale, and the language changes nothing that exists`, async ({page}) => {
  test.setTimeout(240_000);
  await boot(page, 1440);
  const before = await existing(page);
  const failures = {};
  const fail = (where, what) => { if (what && (!Array.isArray(what) || what.length)) failures[where] = what; };
  const structure = {en: {}, [LOCALE]: {}};

  for (const locale of ['en', LOCALE]) {
    await show(page, locale);
    stage(locale, 'pages', before.nav.join(' '));
    for (const view of before.nav.filter(view => view !== 'settings' && view !== 'help')) {
      await go(page, view);
      await settled(page);
      if (locale === LOCALE) {
        const found = await inspect(page, 'body');
        fail(`${view}: English shown`, found.english);
        fail(`${view}: keys shown`, found.keys);
        fail(`${view}: critical in English`, found.critical);
      }
    }
    // Settings: each tab the reader may open.
    if (before.nav.includes('settings')) {
      await go(page, 'settings');
      await expect(page.locator('#view-settings .dp-settings-tabs')).toBeVisible();
      const tabs = await page.locator('#view-settings .dp-settings-tabs [data-tab]:not([inert])').evaluateAll(list => list.map(e => e.dataset.tab));
      structure[locale].settingsTabs = tabs;
      for (const tab of tabs) {
        stage(locale, 'settings', tab);
        await page.locator(`#view-settings .dp-settings-tabs [data-tab="${tab}"]:not([inert])`).click();
        await expect(page.locator(`.dp-settings-panel[data-panel="${tab}"]`)).toBeVisible();
        structure[locale][`settings:${tab}`] = await page.locator(`.dp-settings-panel[data-panel="${tab}"] [data-i18n]`).evaluateAll(list => list.map(e => e.dataset.i18n));
        if (locale === LOCALE) {
          const found = await inspect(page, `.dp-settings-panel[data-panel="${tab}"]`);
          fail(`settings ${tab}: English shown`, found.english);
          fail(`settings ${tab}: keys shown`, found.keys);
          fail(`settings ${tab}: critical in English`, found.critical);
        }
      }
    }
    // The Field Manual: each section.
    await go(page, 'help');
    await expect(page.locator('#view-help .dp-help-tabs')).toBeVisible();
    const sections = await page.locator('#view-help .dp-help-tab[data-tab]').evaluateAll(list => list.map(e => e.dataset.tab));
    structure[locale].helpSections = sections;
    for (const id of sections) {
      stage(locale, 'help', id);
      await page.locator(`#view-help .dp-help-tab[data-tab="${id}"]:not(.dp-motion-still *)`).first().click();
      await settled(page);
      await expect(page.locator(`#dp-help-panel-${id}`)).toBeVisible();
      // Every passage, accordions included, as composed for this reader.
      structure[locale][`help:${id}`] = await page.locator(`#dp-help-panel-${id} [data-i18n]`).evaluateAll(list => list.map(e => e.dataset.i18n));
      if (locale === LOCALE) {
        await page.locator(`#dp-help-panel-${id} details:not([open]) > summary`).evaluateAll(list => list.forEach(s => s.click()));
        const found = await inspect(page, `#dp-help-panel-${id}`);
        fail(`help ${id}: English shown`, found.english);
        fail(`help ${id}: keys shown`, found.keys);
        fail(`help ${id}: critical in English`, found.critical);
        fail(`help ${id}: control labels`, found.labels);
      }
    }
  }
  for (const [where, list] of Object.entries(structure.en)) {
    if (JSON.stringify(list) !== JSON.stringify(structure[LOCALE][where])) failures[`${where}: the language changed what exists`] = {en: list, [LOCALE]: structure[LOCALE][where]};
  }
  const after = await existing(page);
  if (JSON.stringify(after) !== JSON.stringify(before)) failures['navigation or reader context changed'] = {before, after};
  fail('runtime notes', await page.evaluate(() => window.DPI18n.diagnostics()));
  const debt = (await page.evaluate(() => window.DPI18n.debt()))[LOCALE];
  if (debt && (debt.missing.length || debt.stale.length)) failures[`keys left to English (debt) in ${LOCALE}`] = debt;
  const found = {locale: LOCALE, reader: before.context, nav: before.nav, failures, compared: Object.keys(structure.en)};
  record('surfaces', found);
  await test.info().attach('locale-probe.json', {body: JSON.stringify(found, null, 1), contentType: 'application/json'});
  expect(failures).toEqual({});
});

test(`L14-L16 locale probe (${LOCALE}): every Settings Help topic, for every reader, is said whole with the same parts and answers as English`, async ({page}) => {
  test.setTimeout(240_000);
  await boot(page, 1440);
  const read = (given, locale) => page.evaluate(async ({given, EASY}) => {
    const real = window.__realAuth || (window.__realAuth = window.debridPulseAuth);
    const shows = part => !(given.mode === 'easy' && EASY.includes(part));
    window.debridPulseAuth = Object.freeze({...real, presentation: Object.freeze({...real.presentation, context: () => Object.freeze({
      ...given, standalone: given.deployment === 'standalone', server: given.deployment !== 'standalone',
      easy: given.mode === 'easy', advanced: given.mode !== 'easy', shows, omits: part => !shows(part)})})});
    const found = {};
    for (const topic of window.DPSettingsHelp.topics) {
      const handle = window.DPSettingsHelp.open(topic, topic === 'my-account' ? {account: {presentation: true}} : {});
      const node = [...document.querySelectorAll('.dp-settings-help-dialog')].pop();
      const keyed = [...node.querySelectorAll('[data-i18n]')];
      found[topic] = {parts: [...node.querySelectorAll('.dp-modal-body [data-i18n]')].map(e => e.dataset.i18n),
        answers: [...node.querySelectorAll('[data-answer]')].map(e => e.dataset.answer).join(),
        texts: keyed.map(e => [e.dataset.i18n, e.textContent.trim(), !!e.dataset.i18nValues]),
        bad: keyed.filter(e => !e.textContent.trim() || e.textContent.trim() === e.dataset.i18n).map(e => e.dataset.i18n)};
      handle.close();
      await new Promise(resolve => setTimeout(resolve, 0));
    }
    window.debridPulseAuth = real;
    return found;
  }, {given, EASY});
  const failures = {};
  let topics = 0;
  for (const [role, [admin, personal, submit, operate]] of Object.entries(ROLES)) {
    for (const deployment of ['server', 'standalone']) {
      for (const mode of ['advanced', 'easy']) {
        const given = {role, deployment, mode, admin, personal, submit, operate};
        await show(page, 'en');
        const english = await read(given, 'en');
        await show(page, LOCALE);
        const said = await read(given, LOCALE);
        for (const [topic, words] of Object.entries(said)) {
          const at = `${role}/${deployment}/${mode} ${topic}`;
          topics += 1;
          if (JSON.stringify(words.parts) !== JSON.stringify(english[topic].parts)) failures[`${at}: parts`] = {en: english[topic].parts, [LOCALE]: words.parts};
          if (words.answers !== english[topic].answers) failures[`${at}: answers`] = {en: english[topic].answers, [LOCALE]: words.answers};
          if (words.bad.length) failures[`${at}: keyed nodes without words`] = words.bad;
          // A keyed node still saying its English, where the locale translates it.
          const left = words.texts.filter(([, text, valued], i) => !valued && ENGLISH_TEXT[text] && text === english[topic].texts[i]?.[1]);
          const untranslated = left.length ? await page.evaluate(left => left.filter(([key, text]) => window.DPI18n.t(key) !== text).map(([key]) => key), left) : [];
          if (untranslated.length) failures[`${at}: English shown`] = untranslated;
        }
      }
    }
  }
  expect(topics).toBe(12 * (await page.evaluate(() => window.DPSettingsHelp.topics.length)));
  const debt = (await page.evaluate(() => window.DPI18n.debt()))[LOCALE];
  if (debt && (debt.missing.length || debt.stale.length)) failures[`keys left to English (debt) in ${LOCALE}`] = debt;
  fail(failures, 'runtime notes', await page.evaluate(() => window.DPI18n.diagnostics()));
  record('topics', {locale: LOCALE, topics, contexts: 12, failures});
  expect(failures).toEqual({});
});

function fail(failures, where, what) { if (what && what.length) failures[where] = what; }

test(`L24/L26 locale probe (${LOCALE}): at 320 px the locale's overflow and clipped labels are measured and recorded for WS6, never failed here`, async ({page}) => {
  test.setTimeout(180_000);
  await boot(page, 320);
  await show(page, LOCALE);
  const geometry = [];
  const measure = (where) => page.evaluate(({where, keys}) => {
    const clipped = [...document.querySelectorAll('[data-i18n]')].filter(e => keys.includes(e.dataset.i18n) && e.getClientRects().length)
      .filter(e => { const s = getComputedStyle(e); return s.overflow !== 'visible' || s.textOverflow === 'ellipsis' ? e.scrollWidth > e.clientWidth + 1 : false; })
      .map(e => ({key: e.dataset.i18n, text: e.textContent.trim(), scrollWidth: e.scrollWidth, clientWidth: e.clientWidth}));
    return {where, overflow: document.documentElement.scrollWidth - innerWidth, clipped};
  }, {where, keys: GEOMETRY});
  const views = await page.locator('#sidebar .nav-item[data-view]').evaluateAll(list => list.map(e => e.dataset.view));
  for (const view of views) {
    await go(page, view);
    await settled(page);
    geometry.push(await measure(view));
  }
  const defects = geometry.filter(g => g.overflow > 0 || g.clipped.length);
  record('geometry', {locale: LOCALE, width: 320, measured: geometry, defects});
  await test.info().attach('locale-geometry.json', {body: JSON.stringify({locale: LOCALE, width: 320, measured: geometry, defects}, null, 1), contentType: 'application/json'});
  for (const g of defects) test.info().annotations.push({type: 'geometry (WS6)', description: `${g.where}: overflow ${g.overflow}px, ${g.clipped.length} clipped labels`});
  // Recorded, not judged: what was measured, on every page the reader has.
  expect(geometry.length).toBe(views.length);
  expect(await page.evaluate(() => window.DPI18n.locale())).toBe(LOCALE);
});
