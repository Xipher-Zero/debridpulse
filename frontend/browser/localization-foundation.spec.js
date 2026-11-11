const { test, expect } = require('@playwright/test');

/* DP 1.0.14 localization foundation (WS0-WS2): the one runtime (DPI18n),
 * the English catalog, the pseudo locales and the first-paint language, on
 * the proof surfaces only -- the shell's navigation, My Account and its
 * Help. Every other surface is still English literals (WS3/WS4 debt), so
 * nothing here asserts on them.
 *
 * en-XA (accented, ~40% longer) and ar-XB (right-to-left) are test locales:
 * the server accepts them only on a runtime started with
 * DEBRIDPULSE_I18N_TEST_LOCALES=1 (DP_I18N_TEST_LOCALES=1 here). Without it
 * the cold-load case states the same first-paint projection itself; no case
 * skips. A live
 * switch uses DPI18n.setLocale, the substrate a later language choice will
 * use; nothing here offers a language. The catalog a case serves itself
 * (fr, de) is a synthetic test double, never a translation. Synthetic
 * accounts and values only. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const TEST_LOCALES = process.env.DP_I18N_TEST_LOCALES === '1';
const EVIDENCE = process.env.DP_EVIDENCE_DIR || '';
const ADMIN = {username: 'browser', password: 'browser-secret'};
const run = Date.now().toString(36);
const adminSession = require('./acl-admin-session');

const NAV = {dashboard: 'Dashboard', torrents: 'Downloads', stats: 'Statistics', settings: 'Settings', help: 'Help & License'};
const KEYLIKE = /^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$/;

async function isolateFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

const api = (page, method, path, body) => page.evaluate(async ([method, path, body]) => {
  const response = await window.debridPulseAuth.fetch(`/api${path}`, {
    method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  return {status: response.status, body: await response.json().catch(() => null)};
}, [method, path, body]);

const dialog = page => page.locator('.dp-modal-dialog.dp-account-dialog');
const helpDialog = page => page.locator('.dp-modal-dialog:not(.dp-account-dialog)').last();

async function boot(page, width = 1440) {
  await page.setViewportSize({width, height: width < 780 ? 760 : 900});
  await isolateFonts(page);
  await page.goto('/');
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
  await page.evaluate(() => window.DPI18n.ready);
}

async function openAccount(page) {
  if ((page.viewportSize()?.width || 1440) < 780) {
    const button = page.locator('#topbar-account-button');
    await button.click();
    if (await button.getAttribute('data-session') === 'authenticated') {
      await page.locator('#topbar-account-sheet [data-account-action="profile"]').click();
    }
  } else {
    await page.locator('#sidebar-auth-row .dp-sidebar-profile').click();
  }
  await expect(dialog(page)).toBeVisible();
}

async function closeAccount(page) {
  await dialog(page).locator('[data-modal-cancel]').click();
  await expect(dialog(page)).toHaveCount(0);
}

const navLabels = page => page.locator('#sidebar .nav-item[data-view] .nav-label').evaluateAll(nodes =>
  Object.fromEntries(nodes.map(node => [node.closest('[data-view]').dataset.view, node.textContent.trim()])));

// Every element that names its key shows a message, never a key or nothing.
const unrendered = page => page.evaluate(KEYLIKE => [...document.querySelectorAll('[data-i18n],[data-i18n-attr]')].flatMap(node => {
  const out = [];
  if (node.dataset.i18n && (!node.textContent.trim() || new RegExp(KEYLIKE).test(node.textContent.trim()))) out.push(node.dataset.i18n);
  for (const pair of String(node.dataset.i18nAttr || '').split(';').filter(Boolean)) {
    const [attribute] = pair.split(':');
    const value = node.getAttribute(attribute.trim()) || '';
    if (!value || new RegExp(KEYLIKE).test(value)) out.push(pair);
  }
  return out;
}), KEYLIKE.source);

const pageOverflow = page => page.evaluate(() => document.scrollingElement.scrollWidth - window.innerWidth);

test('L01: English is the catalog English, whole, with no debt and no unrendered key', async ({page}) => {
  await boot(page);
  const state = await page.evaluate(() => ({lang: document.documentElement.lang, dir: document.documentElement.dir,
    pending: document.documentElement.hasAttribute('data-dp-locale-pending'), shown: window.DPI18n.locale(),
    debt: window.DPI18n.debt(), problems: window.DPI18n.diagnostics()}));
  expect(state).toEqual({lang: 'en', dir: 'ltr', pending: false, shown: 'en', debt: {}, problems: []});
  const labels = await navLabels(page);
  for (const [view, text] of Object.entries(NAV)) expect(labels[view]).toBe(text);
  await expect(page.locator('#page-title')).toHaveText('Dashboard');
  await expect(page.locator('#shell-dock')).toHaveAttribute('aria-label', 'Pages');
  expect(await unrendered(page)).toEqual([]);
  await openAccount(page);
  await expect(dialog(page).locator('.dp-modal-title')).toHaveText('My Account');
  await expect(dialog(page).locator('[data-modal-cancel]')).toHaveText('Cancel');
  expect(await unrendered(page)).toEqual([]);
  await dialog(page).locator('.dp-account-header-end .dp-settings-help-chip').click();
  await expect(helpDialog(page).locator('.dp-modal-title')).toHaveText('My Account');
  expect(await unrendered(page)).toEqual([]);
  await page.keyboard.press('Escape');
  await closeAccount(page);
  expect(await page.evaluate(() => window.DPI18n.diagnostics())).toEqual([]);
});

test('L02: en-XA, live: every proof surface re-renders accented and ~40% longer, and the shell still fits', async ({page}) => {
  for (const width of [1440, 390]) {
    await boot(page, width);
    expect(await page.evaluate(() => window.DPI18n.setLocale('en-XA'))).toBe('en-XA');
    await expect(page.locator('html')).toHaveAttribute('lang', 'en-XA');
    if (width >= 780) {
      const labels = await navLabels(page);
      for (const view of Object.keys(NAV)) expect(labels[view]).toMatch(/^⟦.+·+⟧$/);
      expect(labels.dashboard).toBe('⟦Dàšhböàrd ····⟧');
    } else {
      await expect(page.locator('#shell-dock')).toHaveAttribute('aria-label', /^⟦Pàgéš ·+⟧$/);
    }
    await expect(page.locator('#page-title')).toHaveText('⟦Dàšhböàrd ····⟧');
    expect(await unrendered(page)).toEqual([]);
    expect(await pageOverflow(page)).toBeLessThanOrEqual(0);
    await openAccount(page);
    await expect(dialog(page).locator('.dp-modal-title')).toHaveText(/^⟦Mý Àççöüñţ ·+⟧$/);
    await expect(dialog(page).locator('[data-modal-cancel]')).toHaveText(/^⟦Çàñçél ·+⟧$/);
    const fit = await dialog(page).evaluate(node => ({right: node.getBoundingClientRect().right, inner: window.innerWidth,
      overflowing: [...node.querySelectorAll('button, .dp-modal-title')].filter(el => el.scrollWidth - el.clientWidth > 1
        && getComputedStyle(el).overflow === 'visible' && getComputedStyle(el).textOverflow !== 'ellipsis').length}));
    expect(fit.right).toBeLessThanOrEqual(fit.inner);
    expect(await pageOverflow(page)).toBeLessThanOrEqual(0);
    if (EVIDENCE) await page.screenshot({path: `${EVIDENCE}/en-XA-my-account-${width}.png`});
    await dialog(page).locator('.dp-account-header-end .dp-settings-help-chip').click();
    await expect(helpDialog(page).locator('.dp-modal-title')).toHaveText(/^⟦Mý Àççöüñţ ·+⟧$/);
    const help = await helpDialog(page).locator('.dp-modal-body').innerText();
    expect(help).toContain('⟦');
    expect(help).not.toMatch(/\b(the|your) /);                    // its English is gone
    await page.keyboard.press('Escape');
    await closeAccount(page);
    // Back to English, in place, with nothing left over.
    expect(await page.evaluate(() => window.DPI18n.setLocale('en'))).toBe('en');
    await expect(page.locator('#page-title')).toHaveText('Dashboard');
    if (width >= 780) expect((await navLabels(page)).dashboard).toBe('Dashboard');
    expect(await page.locator('body').innerText()).not.toContain('⟦');
  }
});

test('L03: ar-XB: the document turns right-to-left and every argument is bidi-isolated', async ({page}) => {
  await boot(page);
  expect(await page.evaluate(() => window.DPI18n.setLocale('ar-XB'))).toBe('ar-XB');
  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
  await expect(page.locator('html')).toHaveAttribute('lang', 'ar-XB');
  const text = await page.evaluate(() => ({
    plain: window.DPI18n.t('shell.nav.dashboard'),
    named: window.DPI18n.t('session.my_account.named', {name: 'Ann (QA) 42', role: 'Admin'}),
  }));
  expect(text.plain).toBe('‮Dashboard‬');
  // The argument is an isolate inside the right-to-left text, as given.
  expect(text.named).toContain('⁨Ann (QA) 42⁩');
  expect(text.named).toContain('⁨Admin⁩');
  expect(await unrendered(page)).toEqual([]);
  await page.evaluate(() => window.DPI18n.setLocale('en'));
  await expect(page.locator('html')).toHaveAttribute('dir', 'ltr');
});

test('L03b: right-to-left: a literal is a left-to-right isolate with its exact characters, a printed argument a <bdi>', async ({page}) => {
  await boot(page, 320);
  const CONTROL = /[‎‏‪-‮⁦-⁩؜]/;
  const args = {file: '.hidden.part1.rar', host: 'rapidgator.net'};
  // Left to right: rich text is exactly the plain text, no element added.
  expect(await page.evaluate(a => { const d = document.createElement('div'); d.append(window.DPI18n.rich('group_candidates.artifact_switched', a)); return d.innerHTML; }, args))
    .toBe('.hidden.part1.rar file source switched to rapidgator.net');
  expect(await page.evaluate(() => window.DPI18n.setLocale('ar-XB'))).toBe('ar-XB');
  const rich = await page.evaluate(a => { const d = document.createElement('div'); d.append(window.DPI18n.rich('group_candidates.artifact_switched', a)); return [...d.querySelectorAll('bdi')].map(b => b.textContent); }, args);
  expect(rich).toEqual(['.hidden.part1.rar', 'rapidgator.net']);
  await page.locator('#sidebar .nav-item[data-view="help"]').evaluate(element => element.click());
  const literals = await page.evaluate(() => [...document.querySelectorAll('#view-help code')].filter(code => code.getClientRects().length)
    .map(code => {
      const text = code.firstChild, range = document.createRange();
      const left = at => { range.setStart(text, at); range.setEnd(text, at + 1); return range.getBoundingClientRect().left; };
      return {text: code.textContent, dir: code.dir, simple: code.childNodes.length === 1 && text.nodeType === 3,
        firstLeft: code.textContent.length < 2 || left(0) < left(1)};
    }));
  expect(literals.some(literal => literal.text === '.torrent')).toBe(true);
  for (const literal of literals) {
    expect(literal.dir).toBe('ltr');
    expect(literal.simple).toBe(true);                   // its own characters, nothing around them
    expect(literal.text).not.toMatch(CONTROL);
    expect(literal.firstLeft).toBe(true);                // ".torrent", never "torrent."
  }
});

test('L03c: right-to-left: the compact section navigator keeps the chosen section whole, and Left goes to the next one', async ({page}) => {
  await boot(page, 320);
  expect(await page.evaluate(() => window.DPI18n.setLocale('ar-XB'))).toBe('ar-XB');
  await page.locator('#sidebar .nav-item[data-view="help"]').evaluate(element => element.click());
  const strip = page.locator('#view-help .dp-section-nav-strip:not(.dp-motion-still):not(.dp-motion-still *)');
  await expect(strip).toBeVisible();
  const whole = () => strip.evaluate(element => {
    const box = element.getBoundingClientRect(), tab = element.querySelector('[role="tab"][aria-selected="true"]').getBoundingClientRect();
    return tab.left >= box.left - 0.5 && tab.right <= box.right + 0.5;
  });
  const selected = () => page.locator('#view-help .dp-help-tab[aria-selected="true"]:not(.dp-motion-still *)').getAttribute('data-tab');
  expect(await selected()).toBe('quickstart');
  await page.locator('#view-help .dp-section-nav-end:not(.dp-motion-still *)').last().click();
  await expect.poll(selected).toBe('using');
  await expect.poll(whole).toBe(true);
  await page.locator('#view-help .dp-help-tab[aria-selected="true"]:not(.dp-motion-still *)').focus();
  await page.keyboard.press('ArrowLeft');
  await expect.poll(selected).toBe('howitworks');
  await expect.poll(whole).toBe(true);
  await page.keyboard.press('ArrowRight');
  await expect.poll(selected).toBe('using');
  await page.evaluate(() => window.DPI18n.setLocale('en'));
});

test('L24b: 320px: Japanese and Arabic keep the whole Dashboard name in the header and dock; Japanese and Korean break lines as their scripts do', async ({page}) => {
  await boot(page, 320);
  // The development catalogs these locales have (planned: delivered, never admitted).
  const show = tag => page.evaluate(t => window.DPI18n.setLocale(t, {catalog: `/i18n/${t}.js`, dir: t === 'ar' ? 'rtl' : 'ltr'}), tag);
  const whole = () => page.evaluate(() => {
    const label = document.querySelector('.dp-shell-dock-item[data-view="dashboard"] .dp-shell-label');
    const title = document.getElementById('page-title');
    return {dock: label.scrollWidth <= label.clientWidth, title: title.scrollWidth <= title.clientWidth};
  });
  // Lines that start inside a word: a Japanese small kana or long-vowel mark, two kana of one katakana word, two syllables of one Korean word.
  const badBreaks = () => page.evaluate(() => {
    const SMALL = /[ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶー]/, KATA = /[゠-ヿ]/, HANGUL = /[가-힯]/;
    const found = [], range = document.createRange();
    const walker = document.createTreeWalker(document.getElementById('main'), NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.parentElement?.getClientRects().length) continue;
      let top = null;
      for (let i = 0; i < node.textContent.length; i += 1) {
        range.setStart(node, i); range.setEnd(node, i + 1);
        const box = range.getClientRects()[0];
        if (!box?.width) continue;
        const [a, c] = [node.textContent[i - 1], node.textContent[i]];
        if (top !== null && box.top > top + 4 && (SMALL.test(c) || (HANGUL.test(a) && HANGUL.test(c)))) found.push(node.textContent.slice(Math.max(0, i - 6), i + 3));
        top = box.top;
      }
    }
    return found;
  });
  for (const tag of ['ja', 'ar']) {
    expect(await show(tag)).toBe(tag);
    await page.locator('#sidebar .nav-item[data-view="dashboard"]').evaluate(element => element.click());
    await expect.poll(whole).toEqual({dock: true, title: true});
  }
  for (const tag of ['ja', 'ko']) {
    expect(await show(tag)).toBe(tag);
    await page.locator('#sidebar .nav-item[data-view="dashboard"]').evaluate(element => element.click());
    expect(await badBreaks()).toEqual([]);
  }
  await show('en');
});

test('L24c: 320px: the shortened page names fit the unchanged dock and header (French Dashboard and Downloads; Ukrainian Downloads; German, Spanish, Brazilian Portuguese, Ukrainian and Italian Settings)', async ({page}) => {
  await boot(page, 320);
  const show = tag => page.evaluate(t => window.DPI18n.setLocale(t, {catalog: `/i18n/${t}.js`, dir: 'ltr'}), tag);
  const whole = view => page.evaluate(view => {
    const label = document.querySelector(`.dp-shell-dock-item[data-view="${view}"] .dp-shell-label`);
    const title = document.getElementById('page-title');
    return {dock: label.scrollWidth <= label.clientWidth, title: title.scrollWidth <= title.clientWidth};
  }, view);
  for (const [tag, view] of [['fr', 'dashboard'], ['fr', 'torrents'], ['uk', 'torrents'], ['de', 'settings'], ['es', 'settings'], ['pt-BR', 'settings'], ['uk', 'settings'], ['it', 'settings']]) {
    expect(await show(tag)).toBe(tag);
    await page.locator(`#sidebar .nav-item[data-view="${view}"]`).first().evaluate(element => element.click());
    await expect.poll(() => whole(view), {message: `${tag} ${view}`}).toEqual({dock: true, title: true});
  }
  await show('en');
});

test('L08b: an open My Account dialog is re-worded in place on a language change: title, footer and actions, its focus kept', async ({page}) => {
  await boot(page, 390);
  await openAccount(page);
  const words = () => dialog(page).evaluate(node => ({
    title: node.querySelector('.dp-modal-title').textContent,
    buttons: [...node.querySelectorAll('button')].filter(button => button.getClientRects().length && !button.closest('.dp-modal-header-end'))
      .map(button => (button.getAttribute('aria-label') || button.textContent).trim()).filter(Boolean),
    focusInside: node.contains(document.activeElement),
  }));
  const before = await words();
  expect(before.title).toBe('My Account');
  expect(await page.evaluate(() => window.DPI18n.setLocale('en-XA'))).toBe('en-XA');
  const switched = await words();
  expect(switched.title).toMatch(/^⟦Mý Àççöüñţ ·+⟧$/);
  expect(switched.buttons.length).toBe(before.buttons.length);
  for (const label of switched.buttons) expect(label).toMatch(/^⟦.+⟧$/);      // every control re-worded, none left in English
  expect(switched.focusInside).toBe(true);
  expect(await page.evaluate(() => window.DPI18n.setLocale('en'))).toBe('en');
  expect(await words()).toEqual(before);
  await closeAccount(page);
});

test('L07b: a catalog that never arrives leaves the page in English, said once; a partial or stale one is English key by key', async ({page}) => {
  await boot(page);
  await page.route('**/i18n/de.js*', () => {});                                    // never answered
  const started = Date.now();
  expect(await page.evaluate(() => window.DPI18n.setLocale('de', {catalog: '/i18n/de.js?v=test'}))).toBe('en');
  expect(Date.now() - started).toBeGreaterThanOrEqual(2900);
  expect(await page.evaluate(() => window.DPI18n.diagnostics().map(p => p.kind))).toEqual(['catalog timed out']);
  await expect(page.locator('#page-title')).toHaveText('Dashboard');
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  // A synthetic partial catalog (a test double, not a translation) as the
  // server delivers one after English changed for shell.nav.settings: that
  // translation withheld and named stale (core.localization.delivered).
  await page.route('**/i18n/fr.js*', route => route.fulfill({status: 200, contentType: 'application/javascript',
    body: 'window.DPI18nCatalog("fr",{"shell.nav.dashboard":"[fr-test] Dashboard","shell.page.dashboard.title":"[fr-test] Dashboard"},'
      + '{"stale":["shell.nav.settings"]});'}));
  expect(await page.evaluate(() => window.DPI18n.setLocale('fr', {catalog: '/i18n/fr.js?v=test'}))).toBe('fr');
  await expect(page.locator('#page-title')).toHaveText('[fr-test] Dashboard');
  const labels = await navLabels(page);
  expect(labels.dashboard).toBe('[fr-test] Dashboard');                           // current: the translation
  expect(labels.settings).toBe('Settings');                                       // stale: the current English
  expect(labels.torrents).toBe('Downloads');                                      // missing: English
  const debt = await page.evaluate(() => window.DPI18n.debt().fr);
  expect(debt.stale).toEqual(['shell.nav.settings']);
  expect(debt.missing).toContain('shell.nav.downloads');
  expect(debt.missing).not.toContain('shell.nav.settings');
  expect([...debt.missing, ...debt.stale]).not.toContain('shell.nav.dashboard');
  await page.evaluate(() => window.DPI18n.setLocale('en'));
});

test('L08a: a language changes words only: Easy stays omitted', async ({page}) => {
  await boot(page);
  const set = mode => api(page, 'PUT', '/account/presentation', {presentation_mode: mode});
  expect((await set('easy')).status).toBe(200);
  try {
    await page.reload();
    await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
    const before = await page.locator('#sidebar .nav-item[data-view]').evaluateAll(items =>
      items.filter(item => item.offsetParent !== null).map(item => item.dataset.view));
    expect(before).not.toContain('events');
    await page.evaluate(() => window.DPI18n.setLocale('en-XA'));
    const after = await page.locator('#sidebar .nav-item[data-view]').evaluateAll(items =>
      items.filter(item => item.offsetParent !== null).map(item => item.dataset.view));
    expect(after).toEqual(before);
    // The Activity Log entry's translated label does not make it shown.
    const activity = page.locator('#sidebar .nav-item[data-view="events"]');
    if (await activity.count()) await expect(activity).toBeHidden();
  } finally {
    await page.evaluate(() => window.DPI18n.setLocale('en'));
    expect((await set('advanced')).status).toBe(200);
  }
});

test.describe('the password-auth runtime', () => {
  test.skip(!AUTH, 'DP_AUTH_BASE_URL is not configured');

  async function signIn(browser, {username, password}, viewport, init) {
    const shared = username === ADMIN.username;
    const reuse = shared && adminSession.get() !== null;
    const context = await browser.newContext({baseURL: AUTH, viewport, ...(reuse ? {storageState: adminSession.get()} : {})});
    const page = await context.newPage();
    await isolateFonts(page);
    if (init) await page.addInitScript(init);
    if (reuse) {
      await page.goto('/');
    } else {
      await page.goto('/login');
      await page.locator('#username').fill(username);
      await page.locator('#password').fill(password);
      await page.locator('button[type="submit"]').click();
    }
    await expect(page).not.toHaveURL(/\/login/);
    await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
    if (shared && !reuse) adminSession.remember(await context.storageState());
    return {context, page};
  }

  async function person(admin, name, role) {
    const account = {username: `l10n-${name}-${run}`, password: `l10n-${name}-${run}-pass`};
    const created = await api(admin.page, 'POST', '/accounts', {kind: 'local', role, display_name: `${name} ${run}`, ...account});
    expect(created.status).toBe(200);
    return account;
  }

  // Each frame until the page is first visible: what it shows then.
  const firstVisibleFrame = () => {
    window.__dpFirstVisible = null;
    const look = () => {
      const body = document.body;
      if (body && getComputedStyle(body).visibility === 'visible' && document.querySelector('#sidebar .nav-label')) {
        window.__dpFirstVisible = {lang: document.documentElement.lang,
          labels: [...document.querySelectorAll('#sidebar .nav-item[data-view] .nav-label')].map(n => n.textContent.trim()),
          title: document.getElementById('page-title')?.textContent.trim() || ''};
        return;
      }
      requestAnimationFrame(look);
    };
    requestAnimationFrame(look);
  };

  // The projection the server states for an account that chose en-XA
  // (api.auth_routes._locale; its proof: test_account_locale.py).
  const PSEUDO_PROJECTION = 'window.debridPulseLocale=Object.freeze({"owned":true,"locale":"en-XA","dir":"ltr","status":"pseudo","catalog":null});';

  test('L07a/L05: an en-XA account\'s cold load is veiled until its language is in place: no English frame', async ({browser}) => {
    const admin = await signIn(browser, ADMIN, {width: 1440, height: 914});
    const user = await person(admin, 'paint', 'user');
    await admin.context.close();
    const first = await signIn(browser, user, {width: 1440, height: 914});
    // Where the runtime accepts test locales the account's own saved choice
    // is read at first paint; elsewhere (the release image) the page receives
    // the same stated projection, so the client's cold path is the same.
    if (TEST_LOCALES) expect((await api(first.page, 'PUT', '/account/locale', {locale: 'en-XA'})).body).toEqual({locale: 'en-XA'});
    // Another browser of the same account, cold: it reads the choice at first paint.
    const state = await first.context.storageState();
    await first.context.close();
    const context = await browser.newContext({baseURL: AUTH, viewport: {width: 1440, height: 914}, storageState: state});
    const page = await context.newPage();
    await isolateFonts(page);
    await page.addInitScript(firstVisibleFrame);
    if (!TEST_LOCALES) {
      await page.route('**/ui-theme-bootstrap.js*', async route => {
        const response = await route.fetch();
        const body = (await response.text()).replace(/^window\.debridPulseLocale=.*$/m, PSEUDO_PROJECTION);
        await route.fulfill({response, body});
      });
    }
    await page.goto('/');
    await expect.poll(() => page.evaluate(() => window.__dpFirstVisible)).not.toBeNull();
    const frame = await page.evaluate(() => window.__dpFirstVisible);
    expect(frame.lang).toBe('en-XA');
    expect(frame.labels.length).toBeGreaterThan(3);
    for (const label of frame.labels) expect(label).toMatch(/^⟦.+⟧$/);
    for (const english of Object.values(NAV)) expect(frame.labels).not.toContain(english);
    expect(await page.evaluate(() => window.DPI18n.diagnostics())).toEqual([]);
    if (EVIDENCE) await page.screenshot({path: `${EVIDENCE}/en-XA-cold-paint.png`});
    // The other account in that installation is untouched: still English.
    const other = await signIn(browser, ADMIN, {width: 1440, height: 914});
    expect(await other.page.evaluate(() => window.DPI18n.locale())).toBe('en');
    expect((await navLabels(other.page)).dashboard).toBe('Dashboard');
    await other.context.close();
    await context.close();
  });

  test('L08b/L05: a ReadOnly account gains no control in another language, and a refused password change is said in it', async ({browser}) => {
    const admin = await signIn(browser, ADMIN, {width: 1440, height: 914});
    const reader = await person(admin, 'reader', 'readonly');
    await admin.context.close();
    const signed = await signIn(browser, reader, {width: 1440, height: 914});
    const page = signed.page;
    const controls = () => page.evaluate(() => ({
      nav: [...document.querySelectorAll('#sidebar .nav-item[data-view]')].filter(n => n.offsetParent !== null).map(n => n.dataset.view),
      account: [...document.querySelectorAll('.dp-account-dialog button, .dp-account-dialog input')]
        .filter(n => n.offsetParent !== null).map(n => n.dataset.modalAction || n.dataset.field || n.dataset.presentationMode || n.className).sort(),
    }));
    await openAccount(page);
    const english = await controls();
    await closeAccount(page);
    await page.evaluate(() => window.DPI18n.setLocale('en-XA'));
    await openAccount(page);
    expect(await controls()).toEqual(english);
    // A wrong current password: the server's code, in the page's language.
    await dialog(page).locator('[data-modal-action="password"]').click();
    await dialog(page).locator('[data-field="current"]').fill('not-the-password');
    await dialog(page).locator('[data-field="next"]').fill('l10n-new-password-1');
    await dialog(page).locator('[data-field="confirm"]').fill('l10n-new-password-1');
    const refused = page.waitForResponse(response => response.url().endsWith('/api/account/password'));
    await dialog(page).locator('[data-modal-accept]').click();
    const response = await refused;
    expect(response.status()).toBe(409);
    expect((await response.json()).code).toBe('account.current_password_incorrect');
    await expect(page.locator('.toast').last()).toHaveText(/^⟦Ţhé çürréñţ pàššwörd îš ñöţ çörréçţ ·+⟧$/);
    await expect(dialog(page)).toBeVisible();                                 // still signed in, form kept
    await signed.context.close();
  });
});
