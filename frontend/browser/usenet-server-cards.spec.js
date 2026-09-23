const { test, expect } = require('@playwright/test');

// DP 1.0.13 Gate-9 remediation: the Usenet news-server collection against the
// REAL backend. Each card is one canonical server addressed by a stable id, so
// saving a card never persists another card's unsaved edits, a redacted (blank)
// password preserves the stored credential, and removing one server leaves
// every survivor's credential intact.

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

async function openSources(page) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator('#view-settings [data-tab="sources"]').click();
  await expect(page.locator('.dp-settings-panel[data-panel="sources"]')).toBeVisible();
}

const usenetCard = page => page.locator('.dp-settings-provider-card--usenet');
const collection = page => page.locator('[data-usenet-collection]');
const serverCards = page => collection(page).locator('[data-usenet-server-id]');
const addTile = page => collection(page).locator('[data-usenet-action="add"]');
const field = (card, name) => card.locator(`[data-usenet-field="${name}"]`);
// The SSL control is a styled toggle: its checkbox is visually hidden, so the
// operator clicks the surrounding label, exactly as this does.
async function setSsl(card, on) {
  if ((await field(card, 'ssl').isChecked()) === on) return;
  await card.locator('.dp-usenet-ssl').click();
  await expect(field(card, 'ssl')).toBeChecked({checked: on});
}

async function storedServers(page) {
  const settings = await page.request.get('/api/settings').then(r => r.json());
  return settings.integrations.usenet.options.servers || [];
}

/** Remove every persisted server so each test starts from a known state. */
async function resetServers(page) {
  for (const server of await storedServers(page)) {
    await page.request.delete(`/api/usenet/servers/${server.id}`);
  }
}

/** Turn Usenet on the way an operator does: toggle, then Apply Settings. */
async function enableUsenet(page) {
  const toggle = page.locator('[data-integration-enabled="usenet"]');
  if (!(await toggle.isChecked())) {
    await page.locator('label[for="dp-settings-integration-usenet-enabled"]').click();
  }
  await expect(toggle).toBeChecked();
  const settings = await page.request.get('/api/settings').then(r => r.json());
  if (settings.integrations.usenet.enabled !== true) {
    await page.locator('#view-settings button[data-action="save"]:visible').first().click();
    await expect.poll(async () => {
      const s = await page.request.get('/api/settings').then(r => r.json());
      return s.integrations.usenet.enabled;
    }).toBe(true);
  }
  // The enabled card is expanded, so its controls are operable.
  await expect(page.locator('.dp-settings-provider-card--usenet'))
    .not.toHaveClass(/dp-settings-provider-card--collapsed/);
}

/** Create one server through the UI and return its canonical id. */
async function addServer(page, {host, username, password, port}) {
  await addTile(page).click();
  const card = serverCards(page).last();
  await field(card, 'host').fill(host);
  if (username !== undefined) await field(card, 'username').fill(username);
  if (password !== undefined) await field(card, 'password').fill(password);
  if (port !== undefined) await field(card, 'port').fill(String(port));
  await card.locator('[data-usenet-action="save"]').click();
  await expect.poll(async () => (await storedServers(page)).some(s => s.host === host)).toBeTruthy();
  await expect.poll(() => card.getAttribute('data-usenet-server-id')).not.toBe('');
  return card.getAttribute('data-usenet-server-id');
}

test.beforeEach(async ({page}) => {
  await isolateExternalFonts(page);
  await page.goto('/');
  await openSources(page);
  await resetServers(page);
  await page.reload();
  await openSources(page);
  await enableUsenet(page);
});

test('enabling Usenet expands the card and offers a single Add Server tile', async ({page}) => {
  await expect(usenetCard(page)).not.toHaveClass(/dp-settings-provider-card--collapsed/);
  await expect(addTile(page)).toBeVisible();
  await expect(serverCards(page)).toHaveCount(0);
  // Enabled but unconfigured is never reported ready.
  const status = await page.request.get('/api/integration-status/usenet').then(r => r.json());
  expect(status.state).not.toBe('healthy');
});

test('Add Server opens a card and keeps the tile after the last server', async ({page}) => {
  await addTile(page).click();
  await expect(serverCards(page)).toHaveCount(1);
  const children = collection(page).locator(':scope > *');
  await expect(children.last()).toHaveAttribute('data-usenet-action', 'add');
});

test('a saved server gets a canonical id that is not its list position', async ({page}) => {
  const first = await addServer(page, {host: 'news.one.net', username: 'u1', password: 'PW-ONE'});
  const second = await addServer(page, {host: 'news.two.net', username: 'u2', password: 'PW-TWO'});
  expect(first).toBeTruthy();
  expect(second).toBeTruthy();
  expect(first).not.toBe(second);
  const stored = await storedServers(page);
  expect(stored.map(s => s.id)).toEqual([first, second]);
  // Credentials are never projected back to the browser.
  expect(stored.every(s => s.password === '')).toBeTruthy();
  expect(stored.every(s => s.password_configured === true)).toBeTruthy();
});

test('saving with a blank password preserves the stored credential', async ({page}) => {
  const id = await addServer(page, {host: 'news.keep.net', username: 'keeper', password: 'PW-KEEP'});
  await page.reload();
  await openSources(page);
  await enableUsenet(page);

  const card = collection(page).locator(`[data-usenet-server-id="${id}"]`);
  // The reloaded card holds no secret, only the "configured" fact.
  await expect(field(card, 'password')).toHaveValue('');
  await expect(card).toHaveAttribute('data-usenet-password-configured', '1');

  await field(card, 'host').fill('news.keep-renamed.net');
  await card.locator('[data-usenet-action="save"]').click();
  await expect.poll(async () => (await storedServers(page))[0].host).toBe('news.keep-renamed.net');
  // Still configured: the blank field did not erase the stored password.
  expect((await storedServers(page))[0].password_configured).toBe(true);
});

test('clearing the password is explicit and does erase it', async ({page}) => {
  const id = await addServer(page, {host: 'news.clear.net', username: 'c', password: 'PW-CLEAR'});
  await page.reload();
  await openSources(page);
  await enableUsenet(page);
  const card = collection(page).locator(`[data-usenet-server-id="${id}"]`);
  await card.locator('[data-usenet-clear-password]').check();
  await card.locator('[data-usenet-action="save"]').click();
  await expect.poll(async () => (await storedServers(page))[0].password_configured).toBe(false);
});

test('saving one card never persists another card unsaved edits', async ({page}) => {
  const first = await addServer(page, {host: 'news.alpha.net', username: 'a', password: 'PW-A'});
  await addServer(page, {host: 'news.beta.net', username: 'b', password: 'PW-B'});
  await page.reload();
  await openSources(page);
  await enableUsenet(page);

  const alpha = collection(page).locator(`[data-usenet-server-id="${first}"]`);
  const beta = serverCards(page).nth(1);
  // Type into BETA but never save it, then save ALPHA.
  await field(beta, 'host').fill('news.beta-UNSAVED.net');
  await field(beta, 'username').fill('UNSAVED');
  await field(alpha, 'connections').fill('12');
  await alpha.locator('[data-usenet-action="save"]').click();

  await expect.poll(async () => (await storedServers(page))[0].connections).toBe(12);
  const stored = await storedServers(page);
  expect(stored[1].host).toBe('news.beta.net');
  expect(stored[1].username).toBe('b');
  expect(stored[1].password_configured).toBe(true);
});

test('removing one server preserves every survivor credential and packs left', async ({page}) => {
  await addServer(page, {host: 'news.one.net', username: '1', password: 'PW-1'});
  const middle = await addServer(page, {host: 'news.two.net', username: '2', password: 'PW-2'});
  await addServer(page, {host: 'news.three.net', username: '3', password: 'PW-3'});
  await page.reload();
  await openSources(page);
  await enableUsenet(page);

  await collection(page).locator(`[data-usenet-server-id="${middle}"]`)
    .locator('[data-usenet-action="remove"]').click();
  await expect.poll(async () => (await storedServers(page)).length).toBe(2);

  const stored = await storedServers(page);
  expect(stored.map(s => s.host)).toEqual(['news.one.net', 'news.three.net']);
  expect(stored.every(s => s.password_configured === true)).toBeTruthy();
  // No visual hole: the cards pack left and the tile still trails them.
  await expect(serverCards(page)).toHaveCount(2);
  await expect(collection(page).locator(':scope > *').last())
    .toHaveAttribute('data-usenet-action', 'add');
});

test('removing the final server returns to the Add tile with Usenet still ON', async ({page}) => {
  const only = await addServer(page, {host: 'news.only.net', username: 'o', password: 'PW-O'});
  await collection(page).locator(`[data-usenet-server-id="${only}"]`)
    .locator('[data-usenet-action="remove"]').click();
  await expect.poll(async () => (await storedServers(page)).length).toBe(0);
  await expect(serverCards(page)).toHaveCount(0);
  await expect(addTile(page)).toBeVisible();
  await expect(page.locator('[data-integration-enabled="usenet"]')).toBeChecked();
});

test('the SSL toggle follows a conventional port but never a deliberate one', async ({page}) => {
  await addTile(page).click();
  const card = serverCards(page).last();
  await expect(field(card, 'port')).toHaveValue('563');

  await setSsl(card, false);
  await expect(field(card, 'port')).toHaveValue('119');
  await setSsl(card, true);
  await expect(field(card, 'port')).toHaveValue('563');

  // A deliberate port survives an SSL change.
  await field(card, 'port').fill('9119');
  await setSsl(card, false);
  await expect(field(card, 'port')).toHaveValue('9119');
});

test('the display name derives from Host and a manual override survives Host edits', async ({page}) => {
  await addTile(page).click();
  const card = serverCards(page).last();
  await field(card, 'host').fill('news.derived.net');
  await expect(card.locator('[data-usenet-display-name]')).toHaveText('news.derived.net');

  page.once('dialog', dialog => dialog.accept('Primary feed'));
  await card.locator('[data-usenet-action="rename"]').click();
  await expect(card.locator('[data-usenet-display-name]')).toHaveText('Primary feed');
  await field(card, 'host').fill('news.changed.net');
  await expect(card.locator('[data-usenet-display-name]')).toHaveText('Primary feed');

  // Clearing the override returns to derived behaviour.
  page.once('dialog', dialog => dialog.accept(''));
  await card.locator('[data-usenet-action="rename"]').click();
  await expect(card.locator('[data-usenet-display-name]')).toHaveText('news.changed.net');
});

test('a server card exposes no API-key field', async ({page}) => {
  await addTile(page).click();
  const card = serverCards(page).last();
  for (const name of ['host', 'port', 'ssl', 'username', 'password', 'connections', 'priority']) {
    await expect(field(card, name)).toHaveCount(1);
  }
  await expect(card.locator('[data-usenet-field="api_key"]')).toHaveCount(0);
  await expect(card).toContainText('Lower values have priority.');
});
