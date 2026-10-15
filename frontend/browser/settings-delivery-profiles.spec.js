const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Integration Delivery Profiles -- the Admin's card for the
 * external delivery-profile catalog, proven against the RENDERED page and
 * the REAL backend (GET/PUT /api/external/profiles, the account token API).
 *
 *   - one card, directly beneath Integration Accounts, in the Users & Access
 *     family identity, with its purpose beneath its title;
 *   - Create and Edit are one form in the canonical dialog; Create API Token
 *     is a view of that SAME dialog, so the draft survives it, the secret is
 *     shown once and the new client is selected without typing an ID;
 *   - every write carries the whole catalog and the revision it was read at;
 *     a stale write is refused and the draft kept; the server's own
 *     profile_in_use refusal (and its count) is what the page shows.
 *
 * Every case restores the catalog and removes the accounts it created.
 */

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

async function openUsers(page) {
  await isolateExternalFonts(page);
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator('#view-settings [data-tab="users"]').click();
  await expect(page.locator('.dp-settings-panel[data-panel="users"]')).toBeVisible();
  await expect(page.locator('.dp-settings-delivery-profiles-card')).toBeVisible();
}

async function catalog(page) {
  const response = await page.request.get('/api/external/profiles');
  expect(response.ok()).toBeTruthy();
  return response.json();
}

async function putCatalog(page, profiles) {
  const current = await catalog(page);
  const response = await page.request.put('/api/external/profiles',
    {data: {expected_revision: current.revision, profiles}});
  expect(response.ok(), await response.text()).toBeTruthy();
  return response.json();
}

async function settingsFolder(page) {
  return (await (await page.request.get('/api/settings')).json()).download_folder;
}

async function integration(page, name, label) {
  const created = await page.request.post('/api/accounts', {data: {kind: 'integration', display_name: name}});
  expect(created.ok()).toBeTruthy();
  const {id} = await created.json();
  const issued = await (await page.request.post(`/api/accounts/${id}/tokens`, {data: {label}})).json();
  return {id, token: issued.token, client: issued.client_id};
}

const dialog = page => page.locator('.dp-modal-dialog.dp-profile-dialog');

let cleanup = [];
test.beforeEach(() => { cleanup = []; });
test.afterEach(async ({page}) => {
  for (const step of cleanup.reverse()) await step(page).catch(() => {});
});

test('the card sits directly beneath Integration Accounts in the Users & Access identity', async ({page}) => {
  const before = await catalog(page);
  cleanup.push(p => putCatalog(p, before.profiles));
  await putCatalog(page, []);
  await openUsers(page);
  const cards = page.locator('[data-users-mount] > section.card');
  const titles = await cards.locator('.card-title .dp-settings-card-title-text').allTextContents();
  expect(titles.slice(-2)).toEqual(['Integration Accounts', 'Integration Delivery Profiles']);
  const card = page.locator('.dp-settings-delivery-profiles-card');
  const title = card.locator('> .card-header .card-title');
  await expect(title.locator('.dp-settings-card-title-hint'))
    .toHaveText('Configure how external applications access completed downloads.');
  const chip = title.locator(':scope > .dp-settings-protocol-chip');
  await expect(chip).toHaveAttribute('data-section', 'users');
  await expect(chip.locator('img')).toHaveAttribute('src', '/icons/lucide/workflow-users.svg');
  // Title over hint, the pair centred against the glyph.
  const geometry = await title.evaluate(node => {
    const box = element => element.getBoundingClientRect();
    const chipBox = box(node.querySelector('.dp-settings-protocol-chip'));
    const stack = box(node.querySelector('.dp-settings-card-title-stack'));
    const text = box(node.querySelector('.dp-settings-card-title-text'));
    const hint = box(node.querySelector('.dp-settings-card-title-hint'));
    return {centre: Math.abs((chipBox.top + chipBox.height / 2) - (stack.top + stack.height / 2)),
            stacked: hint.top >= text.bottom - 0.5, gap: stack.left - chipBox.right};
  });
  expect(geometry.centre).toBeLessThanOrEqual(1);
  expect(geometry.stacked).toBe(true);
  expect(geometry.gap).toBeGreaterThan(0);
  await expect(card.locator('> .card-header [data-user-action="create-profile"]')).toHaveText('Create Delivery Profile');
  await expect(card.locator('.card-body')).toContainText('No delivery profiles.');
  // No second token table, no invented status or enabled control.
  await expect(card.locator('.dp-settings-users-tokens')).toHaveCount(0);
  await expect(card.locator('input[type="checkbox"]')).toHaveCount(0);
});

test('Create: defaults, suggested and custom categories, an API token created in the same dialog, one write', async ({page}) => {
  const before = await catalog(page);
  cleanup.push(p => putCatalog(p, before.profiles));
  const name = `Profiles Spec ${Date.now()}`;
  const folder = await settingsFolder(page);
  await openUsers(page);
  const writes = [];
  page.on('request', request => {
    if (request.url().endsWith('/api/external/profiles') && request.method() === 'PUT') writes.push(request.postDataJSON());
  });
  await page.locator('[data-user-action="create-profile"]').click();
  const form = dialog(page);
  await expect(form.locator('.dp-modal-title')).toHaveText('Create Delivery Profile');
  // Defaults: every source type, the download folder, the same import path, 168 hours.
  for (const kind of ['links', 'magnet', 'torrent', 'nzb_link', 'nzb']) {
    await expect(form.locator(`[data-profile-kind="${kind}"]`)).toBeChecked();
  }
  await expect(form.locator('[data-profile-kind]')).toHaveCount(5);
  await expect(form.locator('[data-profile-input="local_root"]')).toHaveValue(folder);
  await expect(form.locator('[data-profile-input="import_root"]')).toHaveValue(folder);
  await expect(form.locator('[data-profile-input="retention_hours"]')).toHaveValue('168');

  const id = `spec-${Date.now()}`;
  await form.locator('[data-profile-input="id"]').fill(id);
  await form.locator('[data-profile-suggest="tv-sonarr"]').click();
  await form.locator('[data-profile-input="category"]').fill('custom-anime');
  await form.locator('[data-profile-input="category"]').press('Enter');   // adds, never submits
  await expect(form).toBeVisible();
  await expect(form.locator('[data-profile-categories]')).toContainText('tv-sonarr');
  await expect(form.locator('[data-profile-categories]')).toContainText('custom-anime');
  // The import path follows the local path until the Admin edits it.
  await form.locator('[data-profile-input="local_root"]').fill(`${folder}/tv`);
  await expect(form.locator('[data-profile-input="import_root"]')).toHaveValue(`${folder}/tv`);
  await form.locator('[data-profile-input="import_root"]').fill('/media/downloads/tv');
  await form.locator('[data-profile-input="local_root"]').fill(`${folder}/series`);
  await expect(form.locator('[data-profile-input="import_root"]')).toHaveValue('/media/downloads/tv');

  // Create API Token without leaving the dialog; the draft survives it.
  await form.locator('[data-profile-create-token]').click();
  await expect(form.locator('[data-profile-view="form"]')).toBeHidden();
  await form.locator('[data-token-input="account"]').selectOption('__new__');
  await form.locator('[data-token-input="account_name"]').fill(name);
  await form.locator('[data-token-input="label"]').fill('sonarr');
  await form.locator('[data-modal-accept]').click();
  const disclosure = form.locator('.dp-token-disclosure');
  await expect(disclosure).toBeVisible();
  await expect(disclosure.locator('.dp-token-disclosure-warning'))
    .toHaveText('Copy this token now. DebridPulse will not display it again.');
  const token = await disclosure.locator('input').inputValue();
  expect(token.startsWith('dp_')).toBe(true);
  const account = (await (await page.request.get('/api/accounts')).json()).accounts.find(item => item.display_name === name);
  cleanup.push(p => p.request.delete(`/api/accounts/${account.id}`));
  await form.locator('[data-modal-accept]').click();   // Done
  await expect(disclosure).toHaveCount(0);
  expect(await page.evaluate(value => document.documentElement.innerHTML.includes(value), token)).toBe(false);
  await expect(form.locator('[data-profile-input="id"]')).toHaveValue(id);
  await expect(form.locator('[data-profile-categories]')).toContainText('custom-anime');
  const tokens = (await (await page.request.get(`/api/accounts/${account.id}/tokens`)).json()).tokens;
  await expect(form.locator(`[data-profile-client="${tokens[0].client_id}"]`)).toBeChecked();
  await expect(form.locator('[data-profile-clients]')).toContainText(`sonarr · ${name}`);

  await form.locator('[data-modal-accept]').click();
  await expect(form).toHaveCount(0);
  expect(writes).toHaveLength(1);
  expect(writes[0].expected_revision).toBe(before.revision);
  const saved = (await catalog(page)).profiles.find(profile => profile.id === id);
  expect(saved).toEqual({id, categories: ['tv-sonarr', 'custom-anime'], clients: [tokens[0].client_id],
    source_kinds: ['links', 'magnet', 'nzb_link', 'nzb', 'torrent'], representation: 'shared_path',
    local_root: `${folder}/series`, import_root: '/media/downloads/tv', retention_hours: 168});
  expect(JSON.stringify(await catalog(page))).not.toContain(token);
  const row = page.locator(`[data-profile-row="${id}"]`);
  await expect(row).toContainText('tv-sonarr');
  await expect(row).toContainText(`sonarr · ${name}`);
  await expect(row).toContainText('→ /media/downloads/tv');
});

test('the new-account name field is shown only for "New Integration Account…" and takes no room otherwise', async ({page}) => {
  const before = await catalog(page);
  cleanup.push(p => putCatalog(p, before.profiles));
  const existing = await integration(page, `Profiles Existing ${Date.now()}`, 'existing');
  cleanup.push(p => p.request.delete(`/api/accounts/${existing.id}`));
  await openUsers(page);
  await page.locator('[data-user-action="create-profile"]').click();
  const form = dialog(page);
  await form.locator('[data-profile-create-token]').click();
  const account = form.locator('[data-token-input="account"]');
  const name = form.locator('[data-token-new-account]');
  // Hidden means absent from the layout, not merely invisible.
  const occupies = () => name.evaluate(node => ({display: getComputedStyle(node).display,
    height: node.getBoundingClientRect().height}));
  await account.selectOption(existing.id);
  await expect(name).toBeHidden();
  expect(await occupies()).toEqual({display: 'none', height: 0});
  await account.selectOption('__new__');
  await expect(name).toBeVisible();
  expect((await occupies()).height).toBeGreaterThan(0);
  await account.selectOption(existing.id);
  await expect(name).toBeHidden();
  expect(await occupies()).toEqual({display: 'none', height: 0});
  // Back to the draft and into the token view again: the field follows the
  // selection the view opens with, never a previous one.
  await form.locator('[data-modal-cancel]').click();
  await form.locator('[data-profile-create-token]').click();
  await expect(name).toHaveCount(1);
  expect(await account.inputValue() === '__new__').toBe(await name.isVisible());
  await page.keyboard.press('Escape');
});

test('Edit prepopulates, keeps other profiles and grants, and a stale write keeps the draft until reloaded', async ({page}) => {
  const before = await catalog(page);
  cleanup.push(p => putCatalog(p, before.profiles));
  const folder = await settingsFolder(page);
  const client = await integration(page, `Profiles Edit ${Date.now()}`, 'radarr');
  cleanup.push(p => p.request.delete(`/api/accounts/${client.id}`));
  await putCatalog(page, [
    {id: 'edit-a', categories: ['radarr'], clients: [client.client, 'retired-client'], source_kinds: ['magnet'],
     local_root: folder, import_root: '', retention_hours: 24},
    {id: 'edit-b', categories: ['other'], clients: [client.client], local_root: folder}]);
  await openUsers(page);
  await page.locator('[data-user-action="edit-profile"][data-profile-id="edit-a"]').click();
  const form = dialog(page);
  await expect(form.locator('[data-profile-input="id"]')).toHaveValue('edit-a');
  await expect(form.locator('[data-profile-input="id"]')).toHaveAttribute('readonly', '');
  await expect(form.locator('[data-profile-input="retention_hours"]')).toHaveValue('24');
  await expect(form.locator('[data-profile-kind="magnet"]')).toBeChecked();
  await expect(form.locator('[data-profile-kind="links"]')).not.toBeChecked();
  // A client no active token names stays granted, and says so.
  await expect(form.locator('[data-profile-client="retired-client"]')).toBeChecked();
  await expect(form.locator('[data-profile-clients]')).toContainText('Client with no active token');
  await form.locator('[data-profile-input="retention_hours"]').fill('48');

  // Someone else writes the catalog meanwhile.
  const other = await catalog(page);
  await putCatalog(page, other.profiles.map(profile => profile.id === 'edit-b' ? {...profile, retention_hours: 12} : profile));
  await form.locator('[data-modal-accept]').click();
  await expect(form.locator('[data-profile-error]')).toContainText('changed elsewhere');
  await expect(form.locator('[data-profile-input="retention_hours"]')).toHaveValue('48');
  expect((await catalog(page)).profiles.find(profile => profile.id === 'edit-a').retention_hours).toBe(24);
  await form.locator('[data-modal-action="reload"]').click();
  await expect(form.locator('[data-profile-error]')).toContainText('reloaded');
  await form.locator('[data-modal-accept]').click();
  await expect(form).toHaveCount(0);
  const saved = await catalog(page);
  expect(saved.profiles.find(profile => profile.id === 'edit-a')).toMatchObject(
    {retention_hours: 48, clients: [client.client, 'retired-client'], source_kinds: ['magnet']});
  expect(saved.profiles.find(profile => profile.id === 'edit-b').retention_hours).toBe(12);

  // Cancel writes nothing.
  await page.locator('[data-user-action="edit-profile"][data-profile-id="edit-a"]').click();
  await form.locator('[data-profile-input="retention_hours"]').fill('99');
  await form.locator('[data-modal-cancel]').click();
  expect(await catalog(page)).toEqual(saved);
});

test('the server refuses to take a profile from a job that depends on it, and the page shows its count', async ({page}) => {
  const before = await catalog(page);
  cleanup.push(p => putCatalog(p, before.profiles));
  const folder = await settingsFolder(page);
  const client = await integration(page, `Profiles Guard ${Date.now()}`, 'sonarr');
  cleanup.push(p => p.request.delete(`/api/accounts/${client.id}`));
  await putCatalog(page, [{id: 'guarded', categories: ['guarded-tv'], clients: [client.client], local_root: folder}]);
  const job = await page.request.post('/api/external/v1/jobs', {
    headers: {Authorization: `Bearer ${client.token}`},
    data: {idempotency_key: `guard-${Date.now()}`, category: 'guarded-tv',
           source: {kind: 'magnet', magnet: `magnet:?xt=urn:btih:${'c'.repeat(40)}&dn=guard`}}});
  expect([200, 201]).toContain(job.status());
  const {id: jobId} = await job.json();
  cleanup.push(p => p.request.post(`/api/external/v1/jobs/${jobId}/withdraw`,
    {headers: {Authorization: `Bearer ${client.token}`}}));
  await openUsers(page);

  // Delete is refused by the server; the profile stays.
  await page.locator('[data-user-action="delete-profile"][data-profile-id="guarded"]').click();
  await page.locator('.dp-modal-overlay [data-modal-accept]').click();
  await expect(page.locator('[data-profile-row="guarded"]')).toBeVisible();
  expect((await catalog(page)).profiles.map(profile => profile.id)).toContain('guarded');

  // Moving its download path is refused with the server's count; the draft stays.
  await page.locator('[data-user-action="edit-profile"][data-profile-id="guarded"]').click();
  const form = dialog(page);
  await form.locator('[data-profile-input="local_root"]').fill(`${folder}/moved`);
  await form.locator('[data-modal-accept]').click();
  await expect(form.locator('[data-profile-error]')).toContainText('1 unsettled external job');
  await expect(form.locator('[data-profile-input="local_root"]')).toHaveValue(`${folder}/moved`);
  // What governs only new submissions remains editable.
  await form.locator('[data-profile-input="local_root"]').fill(folder);
  await form.locator('[data-profile-input="retention_hours"]').fill('72');
  await form.locator('[data-modal-accept]').click();
  await expect(form).toHaveCount(0);
  expect((await catalog(page)).profiles.find(profile => profile.id === 'guarded').retention_hours).toBe(72);
});

test('on a short phone viewport the dialog keeps its header and footer and scrolls its own body', async ({page}) => {
  const before = await catalog(page);
  cleanup.push(p => putCatalog(p, before.profiles));
  // Navigate at desktop width (the phone shell's sidebar is off-canvas), then
  // look at the dialog on a short phone viewport.
  await openUsers(page);
  await page.setViewportSize({width: 390, height: 640});
  await page.locator('[data-user-action="create-profile"]').click();
  const form = dialog(page);
  await expect(form).toBeVisible();
  const geometry = await form.evaluate(node => {
    const box = element => element.getBoundingClientRect();
    const body = node.querySelector('.dp-modal-body');
    return {top: box(node.querySelector('.dp-modal-header')).top, bottom: box(node.querySelector('.dp-modal-footer')).bottom,
            scrolls: body.scrollHeight > body.clientHeight, overflow: getComputedStyle(body).overflowY,
            width: box(node).width};
  });
  expect(geometry.top).toBeGreaterThanOrEqual(0);
  expect(geometry.bottom).toBeLessThanOrEqual(640);
  expect(geometry.scrolls).toBe(true);
  expect(geometry.overflow).toBe('auto');
  expect(geometry.width).toBeLessThanOrEqual(390);
  await form.locator('[data-profile-input="retention_hours"]').scrollIntoViewIfNeeded();
  await expect(form.locator('[data-modal-accept]')).toBeInViewport();
  await page.keyboard.press('Escape');
  await expect(form).toHaveCount(0);
  expect(await catalog(page)).toEqual(before);
});

for (const theme of ['dark', 'light']) {
  test(`the card's glyph wears the Users & Access colour (${theme})`, async ({page}) => {
    await openUsers(page);
    if (theme === 'light') await page.evaluate(() => document.body.classList.add('light'));
    const chip = page.locator('.dp-settings-delivery-profiles-card > .card-header .dp-settings-protocol-chip');
    const colour = await chip.evaluate(node => getComputedStyle(node).getPropertyValue('--dp-protocol-color').trim());
    expect(colour.toUpperCase()).toBe('#1DDB69');
    const glow = await chip.evaluate(node => getComputedStyle(node).filter);
    expect(glow).toContain('drop-shadow');
  });
}
