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
  // The purpose hint is the hinted header's (1.0.14): on the header's
  // midpoint while it fits, else the exact words of its Help chip.
  const header = card.locator('> .card-header.dp-settings-card-header--hinted');
  const hint = 'Configure how external applications access completed downloads.';
  await expect(header.locator('> .dp-settings-card-header-center > span')).toHaveText(hint);
  expect([hint, 'Help: Integration Delivery Profiles'])
    .toContain(await header.locator('.dp-settings-help-chip').getAttribute('data-tooltip'));
  const chip = title.locator(':scope > .dp-settings-protocol-chip');
  await expect(chip).toHaveAttribute('data-section', 'users');
  await expect(chip.locator('img')).toHaveAttribute('src', '/icons/lucide/workflow-users.svg');
  // The title, centred against the glyph.
  const geometry = await title.evaluate(node => {
    const box = element => element.getBoundingClientRect();
    const chipBox = box(node.querySelector('.dp-settings-protocol-chip'));
    const text = box(node.querySelector('.dp-settings-card-title-text'));
    return {centre: Math.abs((chipBox.top + chipBox.height / 2) - (text.top + text.height / 2)), gap: text.left - chipBox.right};
  });
  expect(geometry.centre).toBeLessThanOrEqual(1);
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
  await expect(form.locator('[data-modal-action="delete"]')).toHaveCount(0);      // nothing to delete yet
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

async function submitJob(page, client, category, cleanup, tag) {
  const job = await page.request.post('/api/external/v1/jobs', {
    headers: {Authorization: `Bearer ${client.token}`},
    data: {idempotency_key: `${tag}-${Date.now()}`, category,
           source: {kind: 'magnet', magnet: `magnet:?xt=urn:btih:${tag[0].repeat(40)}&dn=${tag}`}}});
  expect([200, 201]).toContain(job.status());
  const {id} = await job.json();
  cleanup.push(p => p.request.post(`/api/external/v1/jobs/${id}/withdraw`, {headers: {Authorization: `Bearer ${client.token}`}}));
  return id;
}

// The catalog writes the page sends.
function catalogWrites(page) {
  const writes = [];
  page.on('request', request => {
    if (request.url().endsWith('/api/external/profiles') && request.method() === 'PUT') writes.push(request.postDataJSON());
  });
  return writes;
}

test('a profile jobs depend on is deleted from its editor only with its jobs, after an explicit, counted confirmation', async ({page}) => {
  const before = await catalog(page);
  cleanup.push(p => putCatalog(p, before.profiles));
  const folder = await settingsFolder(page);
  const client = await integration(page, `Profiles Guard ${Date.now()}`, 'sonarr');
  cleanup.push(p => p.request.delete(`/api/accounts/${client.id}`));
  await putCatalog(page, [{id: 'guarded', categories: ['guarded-tv'], clients: [client.client], local_root: folder}]);
  const jobId = await submitJob(page, client, 'guarded-tv', cleanup, 'cguard');
  await openUsers(page);
  const writes = catalogWrites(page);
  const confirmation = page.locator('.dp-modal-overlay [role="alertdialog"]').last();
  // The row's one action is Edit.
  await expect(page.locator('[data-profile-row="guarded"] .dp-settings-users-actions button')).toHaveText(['Edit']);

  // Delete, from the editor's footer, states the consequence with the backend's
  // own count in the same dialog; Back returns to the untouched draft.
  await page.locator('[data-user-action="edit-profile"][data-profile-id="guarded"]').click();
  const form = dialog(page);
  const instance = await form.elementHandle();
  await form.locator('[data-profile-input="retention_hours"]').fill('99');            // an unsaved edit
  await expect(form.locator('.dp-modal-footer .btn:visible')).toHaveText(['Delete', 'Cancel', 'Save Profile']);
  await expect(form.locator('[data-modal-action="delete"]')).toHaveClass(/\bbtn-danger\b/);
  await form.locator('[data-modal-action="delete"]').click();
  await expect(form.locator('.dp-modal-title')).toHaveText('Delete delivery profile?');
  expect(await instance.evaluate(node => node.isConnected)).toBe(true);
  await expect(page.locator('.dp-modal-dialog')).toHaveCount(1);                      // no stacked dialog
  await expect(form.locator('[data-profile-view="delete"]')).toContainText('associated with 1 external job');
  await expect(form.locator('[data-profile-view="delete"]')).toContainText('Downloaded files are not deleted');
  await expect(form.locator('.dp-modal-footer .btn:visible')).toHaveText(['Back', 'Delete Profile & Retire Jobs']);
  await expect(form.locator('[data-modal-accept]')).toHaveClass(/\bbtn-danger\b/);
  await form.locator('[data-modal-cancel]').click();                                  // Back
  await expect(form.locator('.dp-modal-title')).toHaveText('Edit Delivery Profile: guarded');
  await expect(form.locator('[data-profile-input="retention_hours"]')).toHaveValue('99');
  expect(writes).toEqual([]);
  expect((await catalog(page)).profiles.map(profile => profile.id)).toContain('guarded');

  // Moving its download path asks before retiring; declined, the draft and the count stay.
  await form.locator('[data-profile-input="retention_hours"]').fill('1');
  await form.locator('[data-profile-input="local_root"]').fill(`${folder}/moved`);
  await form.locator('[data-modal-accept]').click();
  await expect(confirmation.locator('[data-modal-accept]')).toHaveText('Save Profile & Retire Jobs');
  await confirmation.locator('[data-modal-cancel]').last().click();
  await expect(form.locator('[data-profile-error]')).toContainText('1 unsettled external job');
  await expect(form.locator('[data-profile-input="local_root"]')).toHaveValue(`${folder}/moved`);
  // What governs only new submissions remains editable without retiring anything.
  await form.locator('[data-profile-input="local_root"]').fill(folder);
  await form.locator('[data-profile-input="retention_hours"]').fill('72');
  await form.locator('[data-modal-accept]').click();
  await expect(form).toHaveCount(0);
  expect((await catalog(page)).profiles.find(profile => profile.id === 'guarded').retention_hours).toBe(72);

  // Confirmed: only the deletion is written -- never the draft -- and the job is retired, as the server reports.
  writes.length = 0;
  await page.locator('[data-user-action="edit-profile"][data-profile-id="guarded"]').click();
  await form.locator('[data-profile-input="retention_hours"]').fill('5');
  await form.locator('[data-modal-action="delete"]').click();
  await form.locator('[data-modal-accept]').click();
  await expect(form).toHaveCount(0);
  await expect(page.locator('[data-profile-row="guarded"]')).toHaveCount(0);
  await expect(page.locator('.toast').last()).toContainText('Delivery profile deleted; 1 external job retired');
  expect(writes).toHaveLength(1);
  expect(writes[0].profiles.map(profile => profile.id)).not.toContain('guarded');
  expect(writes[0].retire_dependents).toEqual(['guarded']);
  const view = await page.request.get(`/api/external/v1/jobs/${jobId}`, {headers: {Authorization: `Bearer ${client.token}`}});
  expect((await view.json()).phase).toBe('withdrawn');
});

test('a profile no job depends on is deleted from its editor with the concise confirmation, never with its draft', async ({page}) => {
  const before = await catalog(page);
  cleanup.push(p => putCatalog(p, before.profiles));
  const folder = await settingsFolder(page);
  const client = await integration(page, `Profiles Plain ${Date.now()}`, 'radarr');
  cleanup.push(p => p.request.delete(`/api/accounts/${client.id}`));
  await putCatalog(page, [...before.profiles,
    {id: 'plain', categories: ['plain-movies'], clients: [client.client], local_root: folder},
    {id: 'kept', categories: ['kept-movies'], clients: [client.client], local_root: folder, retention_hours: 24}]);
  await openUsers(page);
  const writes = catalogWrites(page);
  await page.locator('[data-user-action="edit-profile"][data-profile-id="plain"]').click();
  const form = dialog(page);
  await form.locator('[data-profile-input="retention_hours"]').fill('99');
  await form.locator('[data-modal-action="delete"]').click();
  await expect(form.locator('.dp-modal-title')).toHaveText('Delete the plain profile?');
  await expect(form.locator('[data-profile-view="delete"]'))
    .toContainText('This removes only the delivery profile. API tokens, downloads, and files are not deleted.');
  await expect(form.locator('[data-modal-accept]')).toHaveText('Delete Profile');
  const current = await catalog(page);
  await form.locator('[data-modal-accept]').click();
  await expect(form).toHaveCount(0);
  await expect(page.locator('[data-profile-row="plain"]')).toHaveCount(0);
  // The write is the catalog as it stands, without this profile: the draft is not in it.
  expect(writes).toHaveLength(1);
  expect(writes[0].retire_dependents).toBeUndefined();
  expect(writes[0].expected_revision).toBe(current.revision);
  expect(writes[0].profiles).toEqual(current.profiles.filter(profile => profile.id !== 'plain'));
});

test('jobs that arrive after the count need a renewed confirmation with their own count', async ({page}) => {
  const before = await catalog(page);
  cleanup.push(p => putCatalog(p, before.profiles));
  const folder = await settingsFolder(page);
  const client = await integration(page, `Profiles Late ${Date.now()}`, 'sonarr');
  cleanup.push(p => p.request.delete(`/api/accounts/${client.id}`));
  await putCatalog(page, [{id: 'late', categories: ['late-tv'], clients: [client.client], local_root: folder}]);
  await openUsers(page);
  const writes = catalogWrites(page);
  await page.locator('[data-user-action="edit-profile"][data-profile-id="late"]').click();
  const form = dialog(page);
  await form.locator('[data-modal-action="delete"]').click();
  await expect(form.locator('[data-modal-accept]')).toHaveText('Delete Profile');       // counted: none
  await submitJob(page, client, 'late-tv', cleanup, 'dlate');                         // one arrives now
  await form.locator('[data-modal-accept]').click();
  await expect(form.locator('[data-profile-delete-error]')).toHaveText('Jobs now depend on this profile. Confirm again to retire 1 external job.');
  await expect(form.locator('.dp-modal-title')).toHaveText('Delete delivery profile?');
  await expect(form.locator('[data-profile-view="delete"]')).toContainText('associated with 1 external job');
  await expect(form.locator('[data-modal-accept]')).toHaveText('Delete Profile & Retire Jobs');
  expect((await catalog(page)).profiles.map(profile => profile.id)).toContain('late');  // nothing retired yet
  expect(writes.map(write => write.retire_dependents)).toEqual([undefined]);
  await form.locator('[data-modal-accept]').click();
  await expect(form).toHaveCount(0);
  expect(writes.map(write => write.retire_dependents)).toEqual([undefined, ['late']]);
  expect((await catalog(page)).profiles.map(profile => profile.id)).not.toContain('late');
});

test('a stale or refused deletion is reported as not deleted, and a new attempt starts from a fresh count', async ({page}) => {
  const before = await catalog(page);
  cleanup.push(p => putCatalog(p, before.profiles));
  const folder = await settingsFolder(page);
  const client = await integration(page, `Profiles Stale ${Date.now()}`, 'radarr');
  cleanup.push(p => p.request.delete(`/api/accounts/${client.id}`));
  await putCatalog(page, [{id: 'stale', categories: ['stale-movies'], clients: [client.client], local_root: folder},
    {id: 'other', categories: ['other-movies'], clients: [client.client], local_root: folder}]);
  await openUsers(page);
  await page.locator('[data-user-action="edit-profile"][data-profile-id="stale"]').click();
  const form = dialog(page);
  await form.locator('[data-modal-action="delete"]').click();
  await expect(form.locator('[data-modal-accept]')).toHaveText('Delete Profile');
  const other = await catalog(page);                                                  // someone else writes meanwhile
  await putCatalog(page, other.profiles.map(profile => profile.id === 'other' ? {...profile, retention_hours: 12} : profile));
  await form.locator('[data-modal-accept]').click();
  await expect(form.locator('[data-profile-delete-error]')).toContainText('Not deleted: The profile catalog was changed elsewhere');
  await expect(form.locator('[data-modal-accept]')).toBeDisabled();
  expect((await catalog(page)).profiles.map(profile => profile.id)).toContain('stale');
  await form.locator('[data-modal-cancel]').click();                                  // Back
  await expect(form.locator('[data-modal-action="reload"]')).toBeVisible();
  await form.locator('[data-modal-action="delete"]').click();                          // a fresh count, a fresh revision
  await form.locator('[data-modal-accept]').click();
  await expect(form).toHaveCount(0);
  expect((await catalog(page)).profiles.map(profile => profile.id)).toEqual(['other']);
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

test('the table: no Sources, one line per path, Retention only where it fits, and Edit always in view', async ({page}) => {
  const before = await catalog(page);
  cleanup.push(p => putCatalog(p, before.profiles));
  const client = await integration(page, 'Table Sonarr', 'table-sonarr');
  cleanup.push(p => p.request.delete(`/api/accounts/${client.id}`));
  const folder = await settingsFolder(page);
  const longLocal = `${folder}/integrations/very-long-folder-name-for-anime-series/season-collections/incoming`;
  const longImport = '/mnt/user/media/downloads/integrations/very-long-folder-name-for-anime-series/season-collections/incoming';
  await putCatalog(page, [
    {id: 'mapped', categories: ['tv-sonarr', 'tv'], clients: [client.client], local_root: `${folder}/tv`, import_root: '/data/tv', retention_hours: 168},
    {id: 'shared', categories: ['radarr'], clients: [client.client], local_root: `${folder}/movies`, import_root: `${folder}/movies`},
    {id: 'long', categories: ['anime'], clients: [client.client], local_root: longLocal, import_root: longImport, retention_hours: 24}]);
  await page.setViewportSize({width: 1440, height: 900});
  await openUsers(page);
  const card = page.locator('.dp-settings-delivery-profiles-card');
  const table = card.locator('.dp-settings-profiles-table');
  const row = id => table.locator(`tr[data-profile-row="${id}"]`);
  const shown = () => table.locator('thead th').evaluateAll(cells => cells
    .filter(cell => getComputedStyle(cell).display !== 'none').map(cell => cell.textContent.trim()));
  // Fit: the table within its wrapper, every Edit inside it, Retention shown exactly while its container is 780px or wider.
  const fit = () => card.locator('.dp-settings-profiles-table-wrap').evaluate(wrap => {
    const box = wrap.getBoundingClientRect();
    const retention = [...wrap.querySelectorAll('.dp-settings-profiles-retention')].map(cell => getComputedStyle(cell).display !== 'none');
    return {width: wrap.clientWidth, overflow: wrap.scrollWidth - wrap.clientWidth, retention,
      edits: [...wrap.querySelectorAll('[data-user-action="edit-profile"]')].every(button => {
        const b = button.getBoundingClientRect();
        return b.width > 0 && b.left >= box.left - 0.5 && b.right <= box.right + 0.5;
      })};
  });
  const expectFit = async (retained) => {
    const facts = await fit();
    expect(facts.overflow, 'no horizontal scroll').toBeLessThanOrEqual(0);
    expect(facts.edits, 'every Edit is inside the table region').toBe(true);
    expect(facts.width >= 780, 'Retention follows the container width').toBe(retained);
    expect(facts.retention.every(visible => visible === retained), 'header and cells together').toBe(true);
  };

  await expect.poll(shown).toEqual(['Profile', 'Categories', 'API Tokens', 'Paths', 'Retention', 'Actions']);
  await expectFit(true);
  // Sources is the editor's alone.
  await expect(table).not.toContainText('Sources');
  await expect(row('mapped')).not.toContainText('All');

  // Paths: a shared path is one line, a mapped one two, the second led by the arrow; never wrapped.
  await expect(row('shared').locator('.dp-settings-profile-path')).toHaveCount(1);
  await expect(row('mapped').locator('.dp-settings-profile-path')).toHaveText([`${folder}/tv`, '→ /data/tv']);
  await expect(row('mapped').locator('.dp-settings-profile-path-arrow')).toBeVisible();
  const lines = () => table.locator('.dp-settings-profile-path').evaluateAll(nodes => nodes.map(node => {
    const text = node.querySelector('.dp-settings-profile-path-text');
    const arrow = node.querySelector('.dp-settings-profile-path-arrow')?.getBoundingClientRect();
    const cell = node.closest('td').getBoundingClientRect();
    return {row: node.closest('tr').dataset.profileRow, height: node.getBoundingClientRect().height,
      line: parseFloat(getComputedStyle(text).lineHeight) || parseFloat(getComputedStyle(text).fontSize) * 1.6,
      shortened: text.scrollWidth > text.clientWidth + 0.5, ellipsis: getComputedStyle(text).textOverflow,
      arrowInside: arrow ? arrow.width > 0 && arrow.left >= cell.left && arrow.right <= cell.right : null};
  }));
  for (const line of await lines()) expect(line.height, `${line.row}: one line`).toBeLessThanOrEqual(line.line + 1);
  // A path is shortened only when its column is narrower than it: /data/tv is always whole.
  const mappedImport = async () => (await lines()).filter(line => line.row === 'mapped')[1];
  expect((await mappedImport()).shortened, '/data/tv is whole at 1440').toBe(false);
  // The whole route is the tooltip of one keyboard-reachable group, and the cell's own text.
  const longPaths = row('long').locator('.dp-settings-profile-path-lines');
  await expect(longPaths).toHaveAttribute('data-tooltip', `${longLocal} → ${longImport}`);
  await expect(longPaths).toHaveAttribute('tabindex', '0');
  await expect(longPaths).toContainText(longImport);

  // The constrained desktop: Retention leaves, everything else fits; the long paths shorten, the arrow stays.
  await page.setViewportSize({width: 780, height: 900});
  await expect.poll(shown).toEqual(['Profile', 'Categories', 'API Tokens', 'Paths', 'Actions']);
  await expectFit(false);
  const narrow = (await lines()).filter(line => line.row === 'long');
  expect(narrow.map(line => line.shortened)).toEqual([true, true]);
  expect(narrow.every(line => line.ellipsis === 'ellipsis')).toBe(true);
  expect(narrow[1].arrowInside, 'the arrow stays in view').toBe(true);
  expect((await mappedImport()).shortened, '/data/tv is whole at 780').toBe(false);
  for (const line of await lines()) expect(line.height, `${line.row}: one line at 780`).toBeLessThanOrEqual(line.line + 1);

  // Edit works from here; the editor keeps the whole paths and the Source Types.
  await row('long').locator('[data-user-action="edit-profile"]').click();
  const form = dialog(page);
  await expect(form.locator('[data-profile-input="local_root"]')).toHaveValue(longLocal);
  await expect(form.locator('[data-profile-input="import_root"]')).toHaveValue(longImport);
  await expect(form.locator('[role="group"][aria-label="Source Types"] input')).toHaveCount(5);
  await form.locator('[data-modal-cancel]').click();
  await expect(form).toHaveCount(0);

  // Wider again: Retention returns.
  await page.setViewportSize({width: 1440, height: 900});
  await expect.poll(shown).toEqual(['Profile', 'Categories', 'API Tokens', 'Paths', 'Retention', 'Actions']);
  await expectFit(true);
});
