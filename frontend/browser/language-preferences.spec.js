const { test, expect } = require('@playwright/test');

/* DP 1.0.14 language preferences: My Account's language (saved where it is
 * chosen) and Users & Access → Deployment & Language's default language
 * (saved by Apply), both through the one language list (ui-language-picker.js).
 *
 * This runs on the release image's own manifest, where every target is
 * planned: the list offers Use system default and English, and nothing
 * planned. The state the server holds is read back through the API each time
 * (the stored preference and the language shown are separate facts);
 * failures are the server's own answer replaced by the test. Real languages,
 * right-to-left and a translated first paint are qualified on an isolated
 * supported-locale fixture, outside this suite. The open installation's
 * default Admin; synthetic values only. Each case leaves the account with no
 * language of its own. */

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

async function clearOwn(page) {
  await page.evaluate(() => window.debridPulseAuth.fetch('/api/account/locale', {method: 'PUT',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({locale: null})}));
}

const rows = page => page.locator('.dp-language-list [role="radio"]').evaluateAll(nodes =>
  nodes.map(node => ({value: node.dataset.value, checked: node.getAttribute('aria-checked') === 'true', text: node.textContent.trim()})));

test.beforeEach(async ({page}) => { await boot(page); await clearOwn(page); });
test.afterEach(async ({page}) => { await clearOwn(page); });

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
  // English and the supported targets only: none is supported in this image.
  expect(await rows(page)).toEqual([
    {value: '', checked: true, text: 'Use system default🇺🇸English'},
    {value: 'en', checked: false, text: '🇺🇸English'}]);
  await expect(page.getByRole('radio', {name: /Use system default/})).toBeFocused();
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
  // No Use system default here: the default is a language.
  expect((await rows(page)).map(row => row.value)).toEqual(['en']);
  await page.getByRole('radio', {name: 'English', exact: true}).click();
  await languageDialog(page).locator('[data-modal-cancel]').click();
  await expect(languageDialog(page)).toHaveCount(0);
  await expect(button).toBeFocused();
  expect(saves).toEqual([]);

  await page.route('**/api/settings/default-locale', route => (route.request().method() === 'PUT'
    ? route.fulfill({status: 500, contentType: 'application/json', body: JSON.stringify({detail: 'synthetic failure', code: 'unclassified', params: {}})})
    : route.continue()));
  await button.click();
  await page.getByRole('radio', {name: 'English', exact: true}).click();
  await languageDialog(page).locator('[data-modal-accept]').click();
  const error = languageDialog(page).locator('.dp-language-error');
  await expect(error).toHaveText('The default language could not be saved. Apply it again, or cancel.');
  await expect(languageDialog(page)).toBeVisible();
  expect((await rows(page)).filter(row => row.checked).map(row => row.value)).toEqual(['en']);
  await page.unroute('**/api/settings/default-locale');
  expect(await defaultLocale(page)).toEqual(before);

  await languageDialog(page).locator('[data-modal-accept]').click();
  await expect(languageDialog(page)).toHaveCount(0);
  expect(await defaultLocale(page)).toEqual({locale: 'en', configured: 'en'});
  expect(saves).toEqual([{locale: 'en'}, {locale: 'en'}]);
  // The account still has no language of its own: it follows the default.
  expect(await ownLocale(page)).toEqual({locale: 'en', preference: null, default: 'en'});
});

test('LP05: at 320px the language button wraps beneath Email, the card reads Deployment with the language block beneath, and nothing overflows', async ({page}) => {
  await boot(page, 320);
  await openAccount(page);
  const geometry = await page.evaluate(() => {
    const dialog = document.querySelector('.dp-account-dialog'), body = dialog.querySelector('.dp-modal-body');
    const email = dialog.querySelector('.dp-account-email').getBoundingClientRect();
    const button = dialog.querySelector('[data-account-language]').getBoundingClientRect();
    const footer = dialog.querySelector('.dp-modal-footer').getBoundingClientRect();
    return {below: button.top >= email.bottom, left: Math.abs(button.left - email.left) < 1, clear: button.bottom <= footer.top,
      fits: body.scrollHeight <= body.clientHeight, inside: button.right <= dialog.getBoundingClientRect().right};
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
