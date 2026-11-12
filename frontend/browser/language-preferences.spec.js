const { test, expect } = require('@playwright/test');

/* DP 1.0.14 language preferences: My Account's language (saved where it is
 * chosen) and Users & Access → Deployment & Language's default language
 * (saved by Apply), both through the one language list (ui-language-picker.js).
 *
 * This runs on the image's own manifest, where English and all 18 targets
 * are supported: both lists offer exactly those 19 (LANGUAGES, written out
 * here from the manifest and the approved flag table), never a pseudo locale.
 * The state the server holds is read back through the API each time (the
 * stored preference and the language shown are separate facts); failures are
 * the server's own answer replaced by the test. The open installation's
 * default Admin; synthetic values only. Each case leaves the account with no
 * language of its own and the default English. */

// [tag, flag, native name, direction], in the manifest's order.
const LANGUAGES = [
  ['en', '🇺🇸', 'English', 'ltr'], ['es', '🇪🇸', 'Español', 'ltr'], ['fr', '🇫🇷', 'Français', 'ltr'],
  ['de', '🇩🇪', 'Deutsch', 'ltr'], ['it', '🇮🇹', 'Italiano', 'ltr'], ['pt-BR', '🇧🇷', 'Português (Brasil)', 'ltr'],
  ['nl', '🇳🇱', 'Nederlands', 'ltr'], ['pt-PT', '🇵🇹', 'Português (Portugal)', 'ltr'], ['pl', '🇵🇱', 'Polski', 'ltr'],
  ['tr', '🇹🇷', 'Türkçe', 'ltr'], ['ru', '🇷🇺', 'Русский', 'ltr'], ['uk', '🇺🇦', 'Українська', 'ltr'],
  ['id', '🇮🇩', 'Bahasa Indonesia', 'ltr'], ['hi', '🇮🇳', 'हिन्दी', 'ltr'], ['ja', '🇯🇵', '日本語', 'ltr'],
  ['ko', '🇰🇷', '한국어', 'ltr'], ['zh-Hans', '🇨🇳', '简体中文', 'ltr'], ['zh-Hant', '🇹🇼', '繁體中文', 'ltr'],
  ['ar', '🇸🇦', 'العربية', 'rtl']];
const listed = (checked = null) => LANGUAGES.map(([tag, flag, native, dir]) =>
  ({value: tag, checked: tag === checked, text: `${flag}${native}`, lang: tag, dir}));

const ownLocale = page => page.evaluate(() => fetch('/api/account/locale').then(response => response.json()));
const defaultLocale = page => page.evaluate(() => fetch('/api/settings/default-locale').then(response => response.json()));
const account = page => page.locator('.dp-modal-dialog.dp-account-dialog');
const languageDialog = page => page.locator('.dp-modal-dialog.dp-language-dialog');

async function boot(page, width = 1440) {
  await page.setViewportSize({width, height: width < 780 ? 760 : 900});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
  await page.evaluate(() => window.DPI18n.ready);
}

async function openAccount(page) {
  if ((page.viewportSize()?.width || 1440) < 780) await page.locator('#topbar-account-button').click();
  else await page.locator('#sidebar-auth-row .dp-sidebar-profile').click();
  await expect(account(page).locator('[data-account-language] .dp-language-name')).toHaveText('English');
}

const put = (page, path, body) => page.evaluate(([path, body]) => window.debridPulseAuth.fetch(path, {method: 'PUT',
  headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then(response => response.status), [path, body]);

async function reset(page) {
  await put(page, '/api/account/locale', {locale: null});
  await put(page, '/api/settings/default-locale', {locale: 'en'});
}

const rows = page => page.locator('.dp-language-list [role="radio"]').evaluateAll(nodes =>
  nodes.map(node => ({value: node.dataset.value, checked: node.getAttribute('aria-checked') === 'true', text: node.textContent.trim(),
    lang: node.querySelector('.dp-language-name')?.lang, dir: node.querySelector('.dp-language-name')?.dir})));
const withoutRun = list => list.map(({value, checked, text}) => ({value, checked, text}));

// A two-part control drawn as one (Easy | Advanced, the handheld theme toggle and its chevron): read in
// screen order, its outer corners are round and its shared edge square, in either reading direction,
// and the two meet on one line -- overlapping borders (-1) or one divider (0).
const segmented = (page, first, second) => page.evaluate(([first, second]) => {
  const [left, right] = [document.querySelector(first), document.querySelector(second)]
    .sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
  const s = node => getComputedStyle(node), r = value => parseFloat(value) > 0;
  const seamLines = (s(left).borderRightWidth !== '0px') + (s(right).borderLeftWidth !== '0px');
  return {outerRound: r(s(left).borderTopLeftRadius) && r(s(left).borderBottomLeftRadius) && r(s(right).borderTopRightRadius) && r(s(right).borderBottomRightRadius),
    seamSquare: !r(s(left).borderTopRightRadius) && !r(s(left).borderBottomRightRadius) && !r(s(right).borderTopLeftRadius) && !r(s(right).borderBottomLeftRadius),
    meet: Math.round(right.getBoundingClientRect().left - left.getBoundingClientRect().right), seamLines};
}, [first, second]);

test.beforeEach(async ({page}) => {
  await boot(page);
  await reset(page);
  if (await page.evaluate(() => window.DPI18n.locale()) !== 'en') await boot(page);
});
test.afterEach(async ({page}) => { await reset(page); });

test('LP01: My Account shows the language as a flag and its name, and its list is a view of the same dialog with Use system default checked', async ({page}) => {
  await openAccount(page);
  const button = account(page).locator('[data-account-language]');
  await expect(button).toHaveAccessibleName('Language: English');
  await expect(button.locator('.dp-language-flag')).toHaveAttribute('aria-hidden', 'true');
  await expect(button).toHaveText('🇺🇸English');
  expect(await ownLocale(page)).toEqual({locale: 'en', preference: null, default: 'en'});

  await button.click();
  const view = account(page).locator('[data-account-view="language"]');
  await expect(view).toBeVisible();
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(1);
  await expect(account(page).locator('.dp-modal-title')).toHaveText('Language');
  await expect(account(page).locator('[data-modal-accept]')).toBeHidden();
  await expect(account(page).locator('[data-modal-cancel]')).toHaveText('Back');
  await expect(page.getByRole('radiogroup', {name: 'Language'})).toBeVisible();
  // Use system default, then English and the 18 supported targets: 19 languages, no pseudo locale.
  const shown = await rows(page);
  expect(withoutRun(shown.slice(0, 1))).toEqual([{value: '', checked: true, text: 'Use system default🇺🇸English'}]);
  expect(shown.slice(1)).toEqual(listed());
  expect(shown.map(row => row.value)).not.toContain('en-XA');
  expect(shown.map(row => row.value)).not.toContain('ar-XB');
  await expect(page.getByRole('radio', {name: /Use system default/})).toBeFocused();
  // Each flag is decoration; a row is named by its language alone.
  await expect(page.getByRole('radio', {name: 'Deutsch', exact: true})).toHaveCount(1);
  // One tab stop; the arrows and Home/End move along the list without choosing.
  await page.keyboard.press('ArrowDown');
  await expect(page.getByRole('radio', {name: 'English', exact: true})).toBeFocused();
  await page.keyboard.press('Home');
  await expect(page.getByRole('radio', {name: /Use system default/})).toBeFocused();
  expect(await ownLocale(page)).toEqual({locale: 'en', preference: null, default: 'en'});

  // Back saves nothing and returns to the button.
  await account(page).locator('[data-modal-cancel]').click();
  await expect(view).toBeHidden();
  await expect(button).toBeFocused();
  await expect(account(page).locator('[data-modal-accept]')).toBeVisible();
  expect(await ownLocale(page)).toEqual({locale: 'en', preference: null, default: 'en'});
  await page.keyboard.press('Escape');
  await expect(account(page)).toHaveCount(0);
});

test('LP02: choosing English is a choice of its own, Use system default clears it, and the profile drafts stay', async ({page}) => {
  await openAccount(page);
  const name = account(page).locator('[data-field="display_name"]');
  const draft = `Draft ${Date.now().toString(36)}`;
  await name.fill(draft);
  const saves = [];
  page.on('request', request => { if (request.method() === 'PUT' && request.url().endsWith('/api/account/locale')) saves.push(request.postDataJSON()); });

  await account(page).locator('[data-account-language]').click();
  await page.getByRole('radio', {name: 'English', exact: true}).click();
  await expect(account(page).locator('[data-account-view="profile"]')).toBeVisible();
  await expect(account(page).locator('[data-account-language]')).toBeFocused();
  expect(await ownLocale(page)).toEqual({locale: 'en', preference: 'en', default: 'en'});
  await expect(name).toHaveValue(draft);

  await account(page).locator('[data-account-language]').click();
  expect((await rows(page)).filter(row => row.checked).map(row => row.value)).toEqual(['en']);
  await page.getByRole('radio', {name: /Use system default/}).click();
  await expect(account(page).locator('[data-account-view="profile"]')).toBeVisible();
  expect(await ownLocale(page)).toEqual({locale: 'en', preference: null, default: 'en'});
  expect(saves).toEqual([{locale: 'en'}, {locale: null}]);
  await expect(name).toHaveValue(draft);
  // The profile's own Cancel discards the draft and undoes no language choice.
  await account(page).locator('[data-modal-cancel]').click();
  await expect(account(page)).toHaveCount(0);
  expect(await ownLocale(page)).toEqual({locale: 'en', preference: null, default: 'en'});
});

test('LP03: a refused save keeps the list open and says so; a second press while saving sends nothing', async ({page}) => {
  await openAccount(page);
  let release;
  const held = new Promise(resolve => { release = resolve; });
  let sent = 0;
  await page.route('**/api/account/locale', async route => {
    if (route.request().method() !== 'PUT') return route.continue();
    sent += 1;
    await held;
    await route.fulfill({status: 500, contentType: 'application/json', body: JSON.stringify({detail: 'synthetic failure', code: 'unclassified', params: {}})});
  });
  await account(page).locator('[data-account-language]').click();
  const english = page.getByRole('radio', {name: 'English', exact: true});
  await english.click();
  // A second press while the save is in flight (the row is aria-disabled
  // then, so it is dispatched as the press itself): ignored.
  await expect(page.locator('.dp-language-list')).toHaveAttribute('aria-busy', 'true');
  await english.dispatchEvent('click');
  release();
  const error = account(page).locator('[data-account-language-error]');
  await expect(error).toBeVisible();
  await expect(error).toHaveAttribute('role', 'alert');
  await expect(error).toHaveText('Your language could not be saved. Choose it again, or go back.');
  expect(sent).toBe(1);
  await expect(account(page).locator('[data-account-view="language"]')).toBeVisible();
  expect(await page.evaluate(() => [document.documentElement.lang, window.DPI18n.locale()])).toEqual(['en', 'en']);
  expect((await rows(page)).filter(row => row.checked).map(row => row.value)).toEqual(['']);
  await page.unroute('**/api/account/locale');
  expect(await ownLocale(page)).toEqual({locale: 'en', preference: null, default: 'en'});
  await account(page).locator('[data-modal-cancel]').click();
  await expect(account(page).locator('[data-account-view="profile"]')).toBeVisible();
});

async function openUsers(page) {
  // The page's own navigation (the sidebar entry; below 780px the dock's).
  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
  await page.locator('.dp-settings-tabs [data-tab="users"]').click();
  const button = page.locator('[data-user-action="default-language"]');
  await expect(button.locator('.dp-language-name')).toHaveText('English');
  return button;
}

test('LP04: Default Language keeps a pending choice until Apply; Cancel and a refused Apply change nothing', async ({page}) => {
  const card = page.locator('.dp-settings-deployment-card');
  const button = await openUsers(page);
  await expect(card.locator('.dp-settings-card-title-full')).toHaveText('Deployment & Language');
  await expect(card.locator('.dp-settings-deployment-copy .form-label')).toHaveText('Current Deployment Type');
  await expect(button).toHaveAccessibleName('Default language: English');
  const before = await defaultLocale(page);
  expect(before.locale).toBe('en');
  const saves = [];
  page.on('request', request => { if (request.method() === 'PUT' && request.url().endsWith('/api/settings/default-locale')) saves.push(request.postDataJSON()); });

  await button.click();
  await expect(languageDialog(page)).toBeVisible();
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(1);
  // No Use system default here: the default is one of the same 19 languages.
  expect(await rows(page)).toEqual(listed('en'));
  // A row is only the pending choice: Cancel discards it.
  await page.getByRole('radio', {name: 'Deutsch', exact: true}).click();
  expect((await rows(page)).filter(row => row.checked).map(row => row.value)).toEqual(['de']);
  await languageDialog(page).locator('[data-modal-cancel]').click();
  await expect(languageDialog(page)).toHaveCount(0);
  await expect(button).toBeFocused();
  expect(saves).toEqual([]);
  expect(await defaultLocale(page)).toEqual(before);

  await page.route('**/api/settings/default-locale', route => (route.request().method() === 'PUT'
    ? route.fulfill({status: 500, contentType: 'application/json', body: JSON.stringify({detail: 'synthetic failure', code: 'unclassified', params: {}})})
    : route.continue()));
  await button.click();
  await page.getByRole('radio', {name: 'Deutsch', exact: true}).click();
  await languageDialog(page).locator('[data-modal-accept]').click();
  const error = languageDialog(page).locator('.dp-language-error');
  await expect(error).toHaveText('The default language could not be saved. Apply it again, or cancel.');
  await expect(languageDialog(page)).toBeVisible();
  expect((await rows(page)).filter(row => row.checked).map(row => row.value)).toEqual(['de']);
  expect(await page.evaluate(() => document.documentElement.lang)).toBe('en');
  await page.unroute('**/api/settings/default-locale');
  expect(await defaultLocale(page)).toEqual(before);

  // Apply saves the pending choice once; this Admin has no language of its own, so it follows at once.
  await languageDialog(page).locator('[data-modal-accept]').click();
  await expect(languageDialog(page)).toHaveCount(0);
  expect(await defaultLocale(page)).toEqual({locale: 'de', configured: 'de'});
  expect(saves).toEqual([{locale: 'de'}, {locale: 'de'}]);
  expect(await ownLocale(page)).toEqual({locale: 'de', preference: null, default: 'de'});
  await expect.poll(() => page.evaluate(() => document.documentElement.lang)).toBe('de');
  await expect(button.locator('.dp-language-name')).toHaveText('Deutsch');
  // An explicit English is the account's own: a new default does not move it.
  expect(await put(page, '/api/account/locale', {locale: 'en'})).toBe(200);
  expect(await put(page, '/api/settings/default-locale', {locale: 'ja'})).toBe(200);
  expect(await ownLocale(page)).toEqual({locale: 'en', preference: 'en', default: 'ja'});
});

test('LP05: at 320px the language button wraps beneath Email, the card reads Deployment with the language block beneath, and nothing overflows', async ({page}) => {
  await boot(page, 320);
  // The handheld theme toggle and its chevron: one capsule with one seam (English, left to right).
  expect(await segmented(page, '.topbar-theme-control .theme-toggle', '.topbar-theme-control .dp-theme-picker-trigger'))
    .toEqual({outerRound: true, seamSquare: true, meet: 0, seamLines: 1});
  await openAccount(page);
  const geometry = await page.evaluate(() => {
    const dialog = document.querySelector('.dp-account-dialog'), body = dialog.querySelector('.dp-modal-body');
    const email = dialog.querySelector('.dp-account-email').getBoundingClientRect();
    const button = dialog.querySelector('[data-account-language]').getBoundingClientRect();
    const footer = dialog.querySelector('.dp-modal-footer').getBoundingClientRect();
    // The profile through the language button fits without scrolling; an Admin's Setup Wizard section
    // follows it inside the same scrolling body (setup-wizard.spec.js measures that section).
    return {below: button.top >= email.bottom, left: Math.abs(button.left - email.left) < 1, clear: button.bottom <= footer.top,
      fits: body.scrollTop === 0 && button.bottom <= body.getBoundingClientRect().bottom,
      inside: button.right <= dialog.getBoundingClientRect().right};
  });
  expect(geometry).toEqual({below: true, left: true, clear: true, fits: true, inside: true});
  await account(page).locator('[data-account-language]').click();
  const footer = await page.evaluate(() => {
    const dialog = document.querySelector('.dp-account-dialog'), f = dialog.querySelector('.dp-modal-footer').getBoundingClientRect();
    return {inView: f.bottom <= innerHeight, listAbove: dialog.querySelector('.dp-language-list').getBoundingClientRect().bottom <= f.top};
  });
  expect(footer).toEqual({inView: true, listAbove: true});
  await page.keyboard.press('Escape');
  await page.keyboard.press('Escape');
  await expect(account(page)).toHaveCount(0);

  await openUsers(page);
  const card = await page.evaluate(() => {
    const c = document.querySelector('.dp-settings-deployment-card');
    const pair = c.querySelector('.dp-settings-deployment-language');
    const deployment = c.querySelector('.dp-settings-deployment').getBoundingClientRect();
    const block = c.querySelector('[data-default-language]').getBoundingClientRect();
    const short = c.querySelector('.dp-settings-card-title-short');
    return {layout: pair.dataset.languageLayout, below: block.top >= deployment.bottom,
      title: getComputedStyle(short).display === 'none' ? 'full' : getComputedStyle(short, '::before').content,
      overflow: pair.scrollWidth > pair.clientWidth || document.documentElement.scrollWidth > innerWidth,
      hintOneLine: (() => { const h = c.querySelector('[data-default-language-hint]'); return h.hidden || h.getBoundingClientRect().height < 24; })()};
  });
  expect(card).toEqual({layout: 'below', below: true, title: '"Deployment"', overflow: false, hintOneLine: true});
  await page.locator('[data-user-action="default-language"]').click();
  const dialogFit = await page.evaluate(() => {
    const d = document.querySelector('.dp-language-dialog'), f = d.querySelector('.dp-modal-footer').getBoundingClientRect();
    return {inView: f.bottom <= innerHeight && d.getBoundingClientRect().top >= 0,
      listAbove: d.querySelector('.dp-language-list').getBoundingClientRect().bottom <= f.top};
  });
  expect(dialogFit).toEqual({inView: true, listAbove: true});
  await languageDialog(page).locator('[data-modal-cancel]').click();
});

test('LP06: German chosen in My Account switches the page after the save, keeps the draft, and Use system default returns it to the default', async ({page}) => {
  await openAccount(page);
  const name = account(page).locator('[data-field="display_name"]');
  const draft = `Entwurf ${Date.now().toString(36)}`;
  await name.fill(draft);
  const german = await page.evaluate(() => fetch('/i18n/de.json').then(response => response.json()).then(catalog => catalog.messages));
  await account(page).locator('[data-account-language]').click();
  await page.getByRole('radio', {name: 'Deutsch', exact: true}).click();
  await expect.poll(() => page.evaluate(() => document.documentElement.lang)).toBe('de');
  await expect(account(page).locator('[data-account-view="profile"]')).toBeVisible();
  await expect(account(page).locator('.dp-modal-title')).toHaveText(german['account.title']);
  await expect(account(page).locator('[data-modal-accept]')).toHaveText(german['common.save']);
  await expect(account(page).locator('[data-account-language]')).toHaveText('🇩🇪Deutsch');
  await expect(account(page).locator('[data-account-language]')).toBeFocused();
  await expect(name).toHaveValue(draft);
  expect(await ownLocale(page)).toEqual({locale: 'de', preference: 'de', default: 'en'});
  await expect(page.locator('#sidebar .nav-item[data-view="settings"] .nav-label')).toHaveText(german['shell.nav.settings']);

  await account(page).locator('[data-account-language]').click();
  expect((await rows(page)).filter(row => row.checked).map(row => row.value)).toEqual(['de']);
  await page.getByRole('radio', {name: /Use system default|Systemstandard/}).click();
  await expect.poll(() => page.evaluate(() => document.documentElement.lang)).toBe('en');
  await expect(name).toHaveValue(draft);
  expect(await ownLocale(page)).toEqual({locale: 'en', preference: null, default: 'en'});
  // The profile's Cancel does not undo a saved language.
  await account(page).locator('[data-modal-cancel]').click();
  await expect(account(page)).toHaveCount(0);
  expect(await ownLocale(page)).toEqual({locale: 'en', preference: null, default: 'en'});
});

test('LP07: a saved Arabic is the first paint at 320px -- right to left and veiled, never English first -- and the list and Help say it', async ({page}) => {
  expect(await put(page, '/api/account/locale', {locale: 'ar'})).toBe(200);
  const arabic = await page.evaluate(() => fetch('/i18n/ar.json').then(response => response.json()).then(catalog => catalog.messages));
  await page.setViewportSize({width: 320, height: 760});
  await page.addInitScript(() => document.addEventListener('DOMContentLoaded', () => {
    window.__firstPaint = {lang: document.documentElement.lang, dir: document.documentElement.dir,
      veiled: document.documentElement.hasAttribute('data-dp-locale-pending'),
      settings: document.querySelector('#sidebar .nav-item[data-view="settings"] .nav-label')?.textContent};
  }));
  await page.goto('/');
  await page.evaluate(() => window.DPI18n.ready);
  // No English frame: before the body is shown the page is Arabic and right to left, and either still
  // veiled or already in Arabic (a catalog that arrives first lifts the veil with its words in place).
  const first = await page.evaluate(() => window.__firstPaint);
  expect([first.lang, first.dir]).toEqual(['ar', 'rtl']);
  expect(first.veiled || first.settings === arabic['shell.nav.settings']).toBe(true);
  expect(await page.evaluate(() => [document.documentElement.lang, document.documentElement.dir,
    document.documentElement.hasAttribute('data-dp-locale-pending')])).toEqual(['ar', 'rtl', false]);
  await expect(page.locator('#page-title')).toHaveText(arabic['shell.page.dashboard.title']);

  // Right to left, both two-part controls keep their outer corners round and one square seam.
  expect(await segmented(page, '.topbar-theme-control .theme-toggle', '.topbar-theme-control .dp-theme-picker-trigger'))
    .toEqual({outerRound: true, seamSquare: true, meet: 0, seamLines: 1});
  await page.locator('#topbar-account-button').click();
  await expect(account(page).locator('[data-account-language]')).toHaveText('🇸🇦العربية');
  expect(await segmented(page, '.dp-account-dialog .dp-presentation-option:first-of-type', '.dp-account-dialog .dp-presentation-option:last-of-type'))
    .toEqual({outerRound: true, seamSquare: true, meet: -1, seamLines: 2});
  // ... in reading order: Easy at the start (the right), Advanced at its end.
  expect(await page.evaluate(() => [...document.querySelectorAll('.dp-account-dialog .dp-presentation-option')].map(node => Math.round(node.getBoundingClientRect().left))
    .reduce((first, second) => first > second))).toBe(true);
  await expect(account(page).locator('.dp-modal-title')).toHaveText(arabic['account.title']);
  await account(page).locator('[data-account-language]').click();
  const shown = await rows(page);
  expect(shown.slice(1)).toEqual(listed('ar'));
  expect(shown[0].checked).toBe(false);
  const fits = await page.evaluate(() => { const d = document.querySelector('.dp-account-dialog').getBoundingClientRect();
    return [...document.querySelectorAll('.dp-language-option')].every(row => { const r = row.getBoundingClientRect(); return r.left >= d.left - 1 && r.right <= d.right + 1; })
      && document.documentElement.scrollWidth <= innerWidth; });
  expect(fits).toBe(true);
  await account(page).locator('[data-modal-cancel]').click();
  await account(page).locator('.dp-account-header-end .dp-settings-help-chip').click();
  const help = page.locator('.dp-modal-dialog').last();
  await expect(help.locator('[data-i18n="help.my_account.language"]')).toHaveText(arabic['help.my_account.language']);
});

test('LP08: Deployment & Language keeps its two blocks side by side and centred while they fit, stacks only when they do not, and fits once', async ({page}) => {
  const card = () => page.evaluate(() => {
    const c = document.querySelector('.dp-settings-deployment-card'), pair = c.querySelector('.dp-settings-deployment-language');
    const body = c.querySelector('.card-body').getBoundingClientRect(), pad = parseFloat(getComputedStyle(c.querySelector('.card-body')).paddingLeft);
    const span = el => { const boxes = [...el.children].filter(n => n.getClientRects().length).map(n => n.getBoundingClientRect());
      return {left: Math.min(...boxes.map(b => b.left)), right: Math.max(...boxes.map(b => b.right))}; };
    const d = span(c.querySelector('.dp-settings-deployment')), l = span(c.querySelector('[data-default-language]'));
    const divider = c.querySelector('[data-default-language]').getBoundingClientRect().left;
    const left = Math.min(d.left, l.left), right = Math.max(d.right, l.right);
    return {layout: pair.dataset.languageLayout, centred: Math.abs((left + right) / 2 - (body.left + body.right) / 2) <= 1,
      roomBefore: Math.round(divider - d.right), roomAfter: Math.round(l.left - divider - 1),
      deploymentFit: c.querySelector('.dp-settings-deployment').dataset.deploymentFit,
      clipped: [...c.querySelectorAll('.form-label, .form-hint, button')].some(n => n.getClientRects().length && n.scrollWidth > n.clientWidth + 1)};
  });
  // The default language's read is held, so the card is fitted before its button is filled.
  let release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route('**/api/settings/default-locale', async route => { if (route.request().method() === 'GET') await held; await route.continue(); });
  await page.setViewportSize({width: 700, height: 900});
  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
  await page.locator('.dp-settings-tabs [data-tab="users"]').click();
  await expect(page.locator('.dp-settings-deployment-card')).toBeVisible();
  // The card's own attributes only: the fitter's measuring clones are removed as soon as measured.
  await page.evaluate(() => { window.__layoutWrites = 0;
    new MutationObserver(list => { window.__layoutWrites += list.filter(m => m.target.isConnected).length; }).observe(document.querySelector('.dp-settings-deployment-card'),
      {subtree: true, attributes: true, attributeFilter: ['data-language-layout', 'data-deployment-fit', 'data-default-language-fit']}); });
  release();
  await expect(page.locator('[data-user-action="default-language"] .dp-language-name')).toHaveText('English');
  // At 700px both fit side by side once the deployment hint takes its approved shorter wording.
  expect(await card()).toEqual({layout: 'beside', centred: true, roomBefore: 24, roomAfter: 24, deploymentFit: '1', clipped: false});
  expect(await page.evaluate(() => window.__layoutWrites)).toBe(0);           // the button's arrival changed nothing
  await page.setViewportSize({width: 1440, height: 900});
  await expect.poll(async () => (await card()).deploymentFit).toBe('0');
  expect(await card()).toEqual({layout: 'beside', centred: true, roomBefore: 24, roomAfter: 24, deploymentFit: '0', clipped: false});
  await page.setViewportSize({width: 420, height: 800});
  await expect.poll(async () => (await card()).layout).toBe('below');
  const stacked = await card();
  expect([stacked.centred, stacked.clipped]).toEqual([true, false]);
});
