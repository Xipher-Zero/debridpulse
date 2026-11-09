// @ts-check
const {test, expect} = require('@playwright/test');

/* DebridPulse 1.0.14 localization, WS3d: Settings administration and
 * integrations -- Authentication, Users & Access, Notifications and Data &
 * Maintenance -- say every word through the catalog, and a live language
 * change re-says them in place: an unsaved credential keeps its value, the
 * same field and its focus; an open confirmation, Edit User or the Backups
 * manager is re-worded where it stands, its choices and drafts kept; and
 * nothing is read again or written.
 *
 * Needs a Server-deployment installation in open mode (the Browser Runtime's
 * open instance); the Admin's default presentation is Advanced. The Edit User
 * case adds its own synthetic ReadOnly account and removes it afterwards.
 * Shown in the test-only pseudo locale en-XA (every message ⟦ … ⟧). */

// Proper names, example values and what the server wrote (a backup's
// contents, "DP State", is the server's description of it).
const NAMES = ['DP State', 'DebridPulse', 'Discord', 'OpenID Connect', 'OIDC', 'Sonarr', 'Radarr', 'qBittorrent', 'Usenet',
  'openid profile email', 'operator', 'groups',
  'https://download.example.com', 'https://id.example/application/o/debridpulse', 'https://example.com/avatar.png'];

// The installation's own data, shown as it is: account names, usernames and
// emails, token labels, delivery profile IDs, categories and paths.
async function dataWords(page) {
  const words = [];
  const accounts = await (await page.request.get('/api/accounts')).json().catch(() => ({}));
  for (const account of accounts.accounts || []) {
    words.push(account.display_name, account.username, account.email);
    const tokens = await (await page.request.get(`/api/accounts/${account.id}/tokens`)).json().catch(() => ({}));
    for (const token of tokens.tokens || []) words.push(token.label);
  }
  const catalog = await (await page.request.get('/api/external/profiles')).json().catch(() => ({}));
  for (const profile of catalog.profiles || []) words.push(profile.id, ...(profile.categories || []), profile.local_root, profile.import_root);
  return words.filter(word => typeof word === 'string' && word.trim()).sort((a, b) => b.length - a.length);
}

async function boot(page, width = 1440) {
  await page.setViewportSize({width, height: width < 780 ? 820 : 900});
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
const settle = page => page.evaluate(() => new Promise(resolve => setTimeout(resolve, 400)));

const englishLeft = (page, selector, extra = []) => page.evaluate(([selector, names]) => {
  const visible = element => element.getClientRects().length > 0 && getComputedStyle(element).visibility !== 'hidden';
  // A date the installation formats (English under a pseudo locale, by design) is not words.
  const dated = text => /\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2}, \d{4}\b|\b\d{1,2}:\d{2} [AP]M\b/g;
  const english = text => !text.includes('⟦')
    && /[A-Za-z]{3,}/.test(names.reduce((left, name) => left.split(name).join(' '), text).replace(dated(), ' '));
  const found = [];
  for (const root of document.querySelectorAll(selector)) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const element = node.parentElement;
      const text = node.textContent.trim();
      if (!text || !element || !visible(element) || element.closest('script, style, svg, template, option')) continue;
      if (english(text)) found.push(`text: ${text}`);
    }
    for (const element of [root, ...root.querySelectorAll('*')]) {
      if (!visible(element)) continue;
      for (const attribute of ['aria-label', 'data-tooltip', 'placeholder', 'title']) {
        const value = element.getAttribute(attribute);
        if (value && english(value)) found.push(`[${attribute}]: ${value}`);
      }
    }
  }
  return [...new Set(found)];
}, [selector, [...extra, ...NAMES]]);

// What Settings reads and every write, after the page is shown: the
// application's own background polling (transfers, throughput, runtime and
// session status) is not Settings' and is left out; the sidebar's aria2 probe too.
const SETTINGS_READS = ['/api/settings', '/api/auth/config', '/api/accounts', '/api/admin/backups', '/api/external/profiles',
  '/api/requests', '/api/account/notifications'];
function watchRequests(page) {
  const seen = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/') || path === '/api/settings/test-aria2') return;
    if (request.method() !== 'GET' || SETTINGS_READS.some(prefix => path === prefix || path.startsWith(`${prefix}/`))) {
      seen.push(`${request.method()} ${path}`);
    }
  });
  return seen;
}
const writesOf = seen => seen.filter(entry => !entry.startsWith('GET '));

const SECTIONS = ['authentication', 'users', 'notifications', 'maintenance'];

test('L10-WS3d: in en-XA, Authentication, Users & Access, Notifications and Data & Maintenance are the catalog\'s', async ({page}) => {
  await boot(page);
  const data = await dataWords(page);
  await setLocale(page, 'en-XA');
  for (const tab of SECTIONS) {
    await openSettings(page, tab);
    await settle(page);
    expect(await englishLeft(page, `#view-settings [data-panel="${tab}"]`, data), tab).toEqual([]);
  }
});

test('L12-WS3d: an unsaved username and password keep their values, their fields and focus; the Status values are re-said', async ({page}) => {
  await boot(page);
  await openSettings(page, 'authentication');
  const username = page.locator('#dp-settings-field-auth-username');
  const password = page.locator('#dp-settings-field-auth-password');
  await username.fill('synthetic-draft-user');
  await password.fill('synthetic-draft-secret');
  await password.focus();
  // The same nodes afterwards: marked now, read after the change.
  await page.evaluate(() => {
    document.getElementById('dp-settings-field-auth-username').dpWs3dMark = 1;
    document.getElementById('dp-settings-field-auth-password').dpWs3dMark = 1;
  });
  const seen = watchRequests(page);
  const kpi = page.locator('#view-settings .dp-settings-auth-kpi[data-auth-kpi="mode"] .dhs-val');
  const englishKpi = (await kpi.innerText()).trim();
  for (const locale of ['en-XA', 'en', 'en-XA']) {
    await setLocale(page, locale);
    await settle(page);
    await expect(username).toHaveValue('synthetic-draft-user');
    await expect(password).toHaveValue('synthetic-draft-secret');
    await expect(password).toBeFocused();
    expect(await page.evaluate(() => [document.getElementById('dp-settings-field-auth-username').dpWs3dMark,
      document.getElementById('dp-settings-field-auth-password').dpWs3dMark])).toEqual([1, 1]);
    const said = (await kpi.innerText()).trim();
    if (locale === 'en') expect(said).toBe(englishKpi);
    else expect(said).toContain('⟦');
    // The password's own label, in the shown language.
    const label = (await page.locator('label[for="dp-settings-field-auth-password"], #view-settings [data-panel="authentication"] .form-label')
      .first().innerText()).trim();
    expect(label.includes('⟦')).toBe(locale === 'en-XA');
  }
  expect(seen, 'no request at all').toEqual([]);
});

test('L12-WS3d: an open destructive confirmation is re-worded in place, stays destructive, and sends nothing', async ({page}) => {
  await boot(page);
  await openSettings(page, 'users');
  const seen = watchRequests(page);
  await page.locator('#view-settings [data-user-action="deployment"]').click();
  const dialog = page.locator('.dp-modal-dialog[data-tone="danger"]');
  await expect(dialog).toBeVisible();
  const title = dialog.locator('[id$="-title"]');
  const accept = dialog.locator('[data-modal-accept]');
  // The default Cancel (no label given): the catalog's, keyed like the rest.
  const cancel = dialog.locator('[data-modal-cancel]').last();
  await expect(title).toHaveText('Switch to Standalone?');
  await expect(cancel).toHaveText('Cancel');
  await accept.focus();
  await setLocale(page, 'en-XA');
  await settle(page);
  await expect(dialog).toBeVisible();
  await expect(title).toContainText('⟦');
  await expect(accept).toContainText('⟦');
  await expect(cancel).toContainText('⟦');
  await expect(dialog.locator('.dp-modal-message')).toContainText('⟦');
  await expect(accept).toBeFocused();
  await expect(dialog).toHaveAttribute('data-tone', 'danger');
  await setLocale(page, 'en');
  await settle(page);
  await expect(title).toHaveText('Switch to Standalone?');
  await expect(accept).toHaveText('Switch to Standalone');
  await expect(cancel).toHaveText('Cancel');
  await expect(accept).toBeFocused();
  await cancel.click();
  await expect(dialog).toBeHidden();
  expect(writesOf(seen), 'nothing written').toEqual([]);
  expect(seen.filter(entry => entry.includes('/deployment-type'))).toEqual([]);
});

test('L12-WS3d: an open Edit User keeps its typed password and chosen role, and is re-said in place', async ({page}) => {
  await boot(page);
  // Its own synthetic account, removed afterwards.
  const name = `synthetic-reader-${Date.now().toString(36)}`;
  const created = await (await page.request.post('/api/accounts', {data: {kind: 'local', display_name: name, username: name,
    password: 'synthetic-Passw0rd-123', role: 'readonly'}})).json();
  expect(created.id).toBeTruthy();
  try {
    await openSettings(page, 'users');
    const row = page.locator('#view-settings tr', {hasText: name});
    await row.locator('[data-user-action="edit"]').click();
    const dialog = page.locator('.dp-users-edit-dialog');
    await expect(dialog).toBeVisible();
    const title = dialog.locator('[id$="-title"]');
    await expect(title).toHaveText(`Edit User: ${name}`);
    await page.evaluate(() => {
      const select = document.querySelector('.dp-users-edit-dialog select[data-field="role"]');
      select.value = 'user';
      select.dispatchEvent(new Event('change', {bubbles: true}));
    });
    const next = dialog.locator('[data-edit-field="password"]');
    const confirm = dialog.locator('[data-edit-field="confirm"]');
    await next.fill('synthetic-Edit-Passw0rd');
    await confirm.fill('synthetic-Edit-Passw0rd');
    await confirm.focus();
    const seen = watchRequests(page);
    await setLocale(page, 'en-XA');
    await settle(page);
    await expect(title).toContainText('⟦');
    await expect(title).toContainText(name);
    await expect(dialog.locator('.dp-users-password-status')).toContainText('⟦');
    await expect(dialog.locator('.dp-users-edit-identity')).toContainText('⟦');
    await expect(dialog.locator('[data-modal-accept]')).toContainText('⟦');
    await expect(next).toHaveValue('synthetic-Edit-Passw0rd');
    await expect(confirm).toHaveValue('synthetic-Edit-Passw0rd');
    await expect(confirm).toBeFocused();
    expect(await page.evaluate(() => document.querySelector('.dp-users-edit-dialog select[data-field="role"]').value)).toBe('user');
    await setLocale(page, 'en');
    await settle(page);
    await expect(title).toHaveText(`Edit User: ${name}`);
    await expect(dialog.locator('.dp-users-password-status')).toHaveText('Passwords match');
    await dialog.locator('[data-modal-cancel]').last().click();
    await expect(dialog).toBeHidden();
    expect(seen, 'no request at all').toEqual([]);
  } finally {
    await page.request.delete(`/api/accounts/${created.id}`);
  }
});

test('L12-WS3d: the open Backups manager is re-said in place and nothing is read again or written', async ({page}) => {
  await boot(page);
  await openSettings(page, 'maintenance');
  await page.locator('#view-settings [data-action="backups"]').first().click();
  const dialog = page.locator('.dp-backup-manager-dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('[id$="-title"]')).toHaveText('Backups');
  const seen = watchRequests(page);
  await setLocale(page, 'en-XA');
  await settle(page);
  await expect(dialog.locator('[id$="-title"]')).toContainText('⟦');
  await expect(dialog.locator('.dp-backup-instruction')).toContainText('⟦');
  for (const action of await dialog.locator('[data-modal-action]').all()) await expect(action).toContainText('⟦');
  expect(await englishLeft(page, '.dp-backup-manager-dialog', await dataWords(page))).toEqual([]);
  await setLocale(page, 'en');
  await settle(page);
  await expect(dialog.locator('[id$="-title"]')).toHaveText('Backups');
  expect(seen, 'no request at all').toEqual([]);
});

test('L10-WS3d: at 390px, in en-XA, the four sections never scroll the page sideways', async ({page}) => {
  await boot(page, 390);
  await setLocale(page, 'en-XA');
  for (const tab of SECTIONS) {
    await openSettings(page, tab);
    await settle(page);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow, tab).toBeLessThanOrEqual(0);
  }
});

test('L12-WS3d: an open restore or remove confirmation formats its backup date again in the shown language', async ({page}) => {
  // One synthetic backup, answered by the test: nothing real is read, restored or removed.
  const backup = {id: 'synthetic-backup-0001', created_at: '2026-09-28T22:35:00Z', contents: 'DP State', size_bytes: 2048};
  await page.route(url => url.pathname === '/api/admin/backups', route => route.fulfill({json: {backups: [backup]}}));
  await boot(page);
  // A test-only language held in memory, with no messages (English words),
  // whose Intl dates differ from English. No catalog file exists for it.
  await page.evaluate(() => window.DPI18nCatalog('de', {}));
  await openSettings(page, 'maintenance');
  await page.locator('#view-settings [data-action="backups"]').first().click();
  const manager = page.locator('.dp-backup-manager-dialog');
  await expect(manager).toBeVisible();
  await manager.locator('[data-backup-select]').check();
  // eslint-disable-next-line no-undef -- the application's own settings global
  const zone = await page.evaluate(() => String((typeof settingsData !== 'undefined' && settingsData && settingsData.timezone) || '').trim() || undefined);
  const expected = await page.evaluate(([iso, zone]) => Object.fromEntries(['en', 'de'].map(tag => {
    const date = new Date(iso);
    return [tag, `${date.toLocaleDateString(tag, {month: 'short', day: 'numeric', year: 'numeric', timeZone: zone})} · ${
      date.toLocaleTimeString(tag, {hour: 'numeric', minute: '2-digit', timeZone: zone})}`];
  })), [backup.created_at, zone]);
  expect(expected.de).not.toBe(expected.en);
  const seen = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (path === '/api/settings/test-aria2') return;
    if (path.startsWith('/api/admin/backups') || request.method() !== 'GET') seen.push(`${request.method()} ${path}`);
  });

  // Restore: the target date, again in each language; selection, tone and focus kept.
  await manager.locator('[data-modal-action="restore"]').click();
  const restore = page.locator('.dp-backup-restore-dialog');
  const target = restore.locator('.dp-backup-restore-target');
  await expect(target).toHaveText(expected.en);
  await restore.locator('[data-modal-accept]').focus();
  for (const [locale, words] of [['de', expected.de], ['en', expected.en]]) {
    await setLocale(page, locale);
    await settle(page);
    await expect(target).toHaveText(words);
    await expect(restore.locator('[data-modal-accept]')).toBeFocused();
    await expect(restore.locator('[data-modal-accept]')).toBeEnabled();
  }
  await expect(restore).toHaveAttribute('data-tone', 'success');
  await restore.locator('[data-modal-cancel]').last().click();
  await expect(restore).toBeHidden();
  await expect(manager.locator('[data-backup-select]')).toBeChecked();

  // Remove: its sentence names the date, again in each language; still destructive.
  await manager.locator('[data-backup-remove]').click();
  const remove = page.locator('.dp-modal-dialog[data-tone="danger"]');
  const sentence = remove.locator('.dp-modal-message');
  await expect(sentence).toHaveText(`${expected.en} will be permanently removed from DebridPulse backup storage.`);
  for (const [locale, words] of [['de', expected.de], ['en', expected.en]]) {
    await setLocale(page, locale);
    await settle(page);
    await expect(sentence).toHaveText(`${words} will be permanently removed from DebridPulse backup storage.`);
    await expect(remove).toHaveAttribute('data-tone', 'danger');
  }
  await remove.locator('[data-modal-cancel]').last().click();
  await expect(remove).toBeHidden();
  await expect(manager.locator('[data-backup-select]')).toBeChecked();
  expect(seen, 'no backup read, restore or removal').toEqual([]);
});
