// @ts-check
const {test, expect} = require('@playwright/test');

/* DebridPulse 1.0.14 localization, WS3e: a refusal the server names by code is
 * said in the reader's language (`error.<code>`, a coded cause inside it too,
 * a transfer refusal by its category), while an absent, unknown or
 * `unclassified` code keeps the server's English `detail` exactly. Every
 * refusal here is synthetic (page.route): nothing is backed up, saved or
 * searched for real, and each action asks the server exactly once.
 *
 * Shown in the test-only pseudo locale en-XA (every message ⟦ … ⟧). Needs the
 * Browser Runtime's open instance (an Admin, Advanced). */

async function boot(page) {
  await page.setViewportSize({width: 1440, height: 900});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await expect(page.locator('#sidebar .nav-item[data-view="settings"]')).toBeAttached();
  await page.evaluate(() => window.DPI18n.ready);
}

async function openSettings(page, tab) {
  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
  await expect(page.locator('#view-settings .dp-settings-master-card')).toBeVisible();
  await page.locator(`#view-settings .dp-settings-tabs [data-tab="${tab}"]:not([inert])`).click();
  await expect(page.locator(`#view-settings [data-panel="${tab}"]`)).toBeVisible();
}

const setLocale = (page, locale) => page.evaluate(locale => window.DPI18n.setLocale(locale), locale);
const lastToast = page => page.locator('#toasts .toast').last();

/* Run Backup answered by `answer` (status, body): the toast it leaves, and how
 * many times the button asked. */
async function refusedBackup(page, status, body) {
  let asked = 0;
  await page.route('**/api/admin/backup', route => {
    if (route.request().method() !== 'POST') return route.continue();
    asked += 1;
    return route.fulfill({status, contentType: 'application/json', body: JSON.stringify(body)});
  });
  await page.evaluate(() => document.querySelectorAll('#toasts .toast').forEach(node => node.remove()));
  await page.locator('#view-settings [data-action="run-backup"]').first().click();
  await expect(lastToast(page)).toBeVisible();
  const words = (await lastToast(page).innerText()).trim();
  await page.unroute('**/api/admin/backup');
  return {words, asked};
}

test('L13-WS3e: a coded refusal is said in the shown language; English is the server\'s detail exactly', async ({page}) => {
  await boot(page);
  await openSettings(page, 'maintenance');
  const busy = {detail: 'Other work is still running, so nothing was changed. Try again when it has finished.',
    code: 'maintenance.busy', params: {}};
  const english = await refusedBackup(page, 409, busy);
  expect(english.words).toContain(busy.detail);
  expect(english.asked).toBe(1);
  await setLocale(page, 'en-XA');
  const pseudo = await refusedBackup(page, 409, busy);
  expect(pseudo.words).toContain('⟦');
  expect(pseudo.words).not.toContain(busy.detail);
  expect(pseudo.asked).toBe(1);
  // A coded cause inside the refusal is said from its own code.
  const nested = await refusedBackup(page, 507, {
    detail: 'Backup could not be created. There is not enough free space in the Backup Folder for this backup.',
    code: 'backup.create_detail', params: {detail: {$t: 'error.backup.detail.space', $v: {}}}});
  expect(nested.words).toContain('⟦');
  expect(nested.words).not.toContain('There is not enough free space');
  expect((nested.words.match(/⟦/g) || []).length).toBeGreaterThanOrEqual(2);
  // A permission refusal and a transfer refusal by its category.
  const forbidden = await refusedBackup(page, 403, {detail: 'Forbidden', code: 'auth.forbidden', params: {}});
  expect(forbidden.words).toContain('⟦');
  const transfer = await refusedBackup(page, 409, {detail: 'Provider unavailable',
    error: {category: 'provider_unavailable', message: 'Provider unavailable'}});
  expect(transfer.words).toContain('⟦');
  await setLocale(page, 'en');
  const back = await refusedBackup(page, 409, {detail: 'Provider unavailable',
    error: {category: 'provider_unavailable', message: 'Provider unavailable'}});
  expect(back.words).toContain('Provider unavailable');
});

test('L13-WS3e: an absent, unknown or unclassified code keeps the server\'s English detail exactly', async ({page}) => {
  await boot(page);
  await openSettings(page, 'maintenance');
  await setLocale(page, 'en-XA');
  for (const body of [
    {detail: 'Upstream said no'},
    {detail: 'Upstream said no', code: 'made.up_code', params: {}},
    {detail: 'Upstream said no', code: 'unclassified', params: {}},
    // A known code whose coded cause the catalog cannot say: the whole refusal stays English.
    {detail: 'Upstream said no', code: 'backup.create_detail', params: {detail: {$t: 'error.no.such_key', $v: {}}}},
  ]) {
    const refused = await refusedBackup(page, 502, body);
    expect(refused.words, JSON.stringify(body)).toContain('Upstream said no');
    expect(refused.words, JSON.stringify(body)).not.toContain('⟦');
    expect(refused.asked).toBe(1);
  }
});

test('L13-WS3e: a refused Settings value names its field and the rule the server names, in the shown language', async ({page}) => {
  await boot(page);
  await openSettings(page, 'maintenance');
  const section = page.locator('#view-settings [aria-controls="dp-settings-section-body-backup-retention"]');
  if ((await section.getAttribute('aria-expanded')) !== 'true') await section.click();
  const field = page.locator('#dp-settings-field-backup-interval-hours');
  await expect(field).toBeVisible();
  const original = await field.inputValue();
  let writes = 0;
  await page.route('**/api/settings', route => {
    if (route.request().method() !== 'PUT') return route.continue();
    writes += 1;
    return route.fulfill({status: 400, contentType: 'application/json', body: JSON.stringify({detail: {
      message: 'Value must be 24 or less.', requirement: 'must be 24 or less', field: 'backup_interval_hours',
      code: 'validation.less_than_equal', params: {le: 24}}})});
  });
  await setLocale(page, 'en-XA');
  await page.evaluate(() => document.querySelectorAll('#toasts .toast').forEach(node => node.remove()));
  await field.fill(String(Number(original || 24) === 30 ? 31 : 30));
  await field.blur();
  await expect(lastToast(page)).toBeVisible();
  const words = (await lastToast(page).innerText()).trim();
  expect(words).toContain('⟦');
  expect(words).toContain('24');
  expect(words).not.toContain('must be 24 or less');
  expect(writes).toBe(1);
  // The refused value is not kept: the field returns to what the server holds.
  await expect(field).toHaveValue(original);
});

test('L13-WS3e: a refused search keeps its machine code and is said by its message code', async ({page}) => {
  await boot(page);
  await setLocale(page, 'en-XA');
  let asked = 0;
  await page.route(url => url.pathname === '/api/torrents' && url.searchParams.has('search'), route => {
    asked += 1;
    return route.fulfill({status: 400, contentType: 'application/json', body: JSON.stringify({detail: {
      code: 'invalid_query', message: 'Search is limited to 200 characters.', message_code: 'search.too_long',
      params: {count: 200}}})});
  });
  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="torrents"]')));
  const search = page.locator('#view-torrents input[type="search"]').first();
  await expect(search).toBeVisible();
  await page.evaluate(() => document.querySelectorAll('#toasts .toast').forEach(node => node.remove()));
  await search.fill('refused-synthetic-query');
  await search.press('Enter');
  await expect(lastToast(page)).toBeVisible();
  const words = (await lastToast(page).innerText()).trim();
  expect(words).toContain('⟦');
  expect(words).not.toContain('Search is limited to 200 characters.');
  expect(asked).toBeGreaterThanOrEqual(1);
});

test('L13-WS3e: an aria2 size refusal says its field by identifier: sentence and field name both follow the language', async ({page}) => {
  await boot(page);
  // The exact field refusal the server builds (backend test_an_aria2_size_refusal_names_its_field_by_identifier…).
  const english = 'Minimum split size must be a whole number with an optional K or M suffix (for example 64M), from 1M to 1024M';
  const body = {detail: {message: english, field: 'min_split_size', code: 'aria2.size_range',
    params: {field: {$t: 'error.aria2.field.min_split_size', $v: {}}, lowest: '1M', highest: '1024'}}};
  let asked = 0;
  await page.route('**/api/integrations/aria2/configuration', route => {
    asked += 1;
    return route.fulfill({status: 400, contentType: 'application/json', body: JSON.stringify(body)});
  });
  const refused = () => page.evaluate(() => window.api('PATCH', '/integrations/aria2/configuration', {options: {}})
    .then(() => null, error => ({message: error.message, english: error.english, localized: error.localized})));
  const inEnglish = await refused();
  expect(inEnglish).toEqual({message: english, english, localized: true});
  await setLocale(page, 'en-XA');
  const pseudo = await refused();
  const field = await page.evaluate(() => window.DPI18n.t('error.aria2.field.min_split_size'));
  expect(field).toContain('⟦');
  expect(pseudo.localized).toBe(true);
  expect(pseudo.message).toContain('⟦');
  expect(pseudo.message).toContain(field);
  expect(pseudo.message).not.toContain('Minimum split size');
  expect(pseudo.message).toContain('1024');
  expect(pseudo.english).toBe(english);
  expect(asked).toBe(2);
});
