const { test, expect } = require('@playwright/test');

/* DP 1.0.13 -- the Usenet news-server collection against the REAL backend.
 *
 * This file is the ONE owner of that shared collection. Playwright runs spec
 * FILES in parallel against a single backend, so a second file creating,
 * resetting or counting servers would be reading and destroying this one's
 * records; the collection therefore has exactly one spec, the same way it has
 * exactly one runtime owner.
 *
 * Each card is one canonical server addressed by a stable id, so a write to
 * one record never touches another and removing a card never disturbs a
 * survivor. A blank password is omitted from the request, which the backend
 * reads as "keep this server's stored credential"; erasing one requires the
 * explicit clear control.
 *
 * A card is NOT a credential transaction. Every control on it is classified by
 * ITS OWN semantics and risk, exactly like every other Settings control:
 *
 *   host / port / username / connections / priority /
 *   articles per request / timeout          changed-blur
 *   SSL                                     immediate
 *   password, Clear Stored Password         gated-save (the card's Save)
 *   display name                            committed when its dialog accepts
 *   Test / Remove / Add Server              explicit-action
 *
 * A card with no canonical id yet is the one deliberate exception: there is no
 * record to write a field to, so its Save is record CREATION. Once the backend
 * mints the id, the card joins the universal persistence model.
 *
 * Usenet owns no notification system: Save/Test/Remove RESULTS are the
 * canonical toast owner's, while inline FIELD VALIDATION is a different,
 * narrower thing that survives.
 */

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
const cardFor = (page, id) => collection(page).locator(`[data-usenet-server-id="${id}"]`);
const field = (card, name) => card.locator(`[data-usenet-field="${name}"]`);
const saveButton = card => card.locator('[data-usenet-action="save"]');
const toasts = page => page.locator('#toasts .toast');

async function rename(page, card, value) {
  await card.locator('[data-usenet-action="rename"]').click();
  const dialog = page.locator('.dp-modal-overlay .dp-modal-dialog');
  await expect(dialog).toBeVisible();
  await dialog.locator('.dp-modal-field .input').fill(value);
  await dialog.locator('[data-modal-accept]').click();
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
}

/* DP 1.0.13 work item F moved the per-server acquisition controls -- Connections,
 * Priority, Articles per Request and Server Timeout -- into one compact Advanced
 * disclosure so the normal card stays short. */
async function openAdvanced(card) {
  const toggle = card.locator('[data-usenet-advanced-toggle]');
  if ((await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click();
  await expect(card.locator('[data-usenet-field="connections"]')).toBeVisible();
}

// The SSL control is a styled toggle: its checkbox is visually hidden, so the
// operator clicks the surrounding label, exactly as this does.
async function setSsl(card, on) {
  if ((await field(card, 'ssl').isChecked()) === on) return;
  await card.locator('.dp-usenet-ssl').click();
  await expect(field(card, 'ssl')).toBeChecked({checked: on});
}

async function storedServers(page) {
  const settings = await page.request.get('/api/settings').then(r => r.json());
  return settings.integrations.usenet?.options?.servers || [];
}

const record = async (page, id) => (await storedServers(page)).find(server => server.id === id);

/* What the canonical persistence owner currently believes the server has
 * accepted for one record's control -- the value every dirty check is made
 * against. */
const committedBaseline = (page, id, name) => page.evaluate(([serverId, field]) =>
  window.DPSettingsPersistence.baseline(document.querySelector(
    `[data-usenet-server-id="${serverId}"] [data-usenet-field="${field}"]`)),
  [id, name]);

/** Remove every persisted server so each test starts from a known state. */
async function resetServers(page) {
  for (const server of await storedServers(page)) {
    await page.request.delete(`/api/usenet/servers/${server.id}`);
  }
}

/** Create one canonical record directly, so the UI starts from a saved server. */
async function seed(page, overrides = {}) {
  const response = await page.request.post('/api/usenet/servers', {data: {
    host: 'news.seed.net', port: 563, ssl: true, username: 'seeded',
    password: 'PW-SEED', connections: 8, priority: 0,
    articles_per_request: 2, timeout_seconds: 60, enabled: true, ...overrides,
  }});
  return (await response.json()).server_id;
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

async function clearToasts(page) {
  await page.evaluate(() => { const host = document.getElementById('toasts'); if (host) host.innerHTML = ''; });
}

/** Open a brand-new, not-yet-created card. */
async function draftCard(page, host) {
  await addTile(page).click();
  const card = serverCards(page).last();
  if (host !== undefined) await field(card, 'host').fill(host);
  return card;
}

/** Create one server through the UI and return its canonical id. */
async function addServer(page, {host, username, password, port}) {
  const card = await draftCard(page, host);
  if (username !== undefined) await field(card, 'username').fill(username);
  if (password !== undefined) await field(card, 'password').fill(password);
  if (port !== undefined) await field(card, 'port').fill(String(port));
  await saveButton(card).click();
  await expect.poll(async () => (await storedServers(page)).some(s => s.host === host)).toBeTruthy();
  await expect.poll(() => card.getAttribute('data-usenet-server-id')).not.toBe('');
  return card.getAttribute('data-usenet-server-id');
}

/* Every server RECORD mutation the page issues, in order. `/usenet/servers/test`
 * is a diagnostic probe, not a record write, so it is deliberately excluded. */
function writes(page) {
  const seen = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (!/^\/api\/usenet\/servers(\/(?!test$)[^/]+)?$/.test(url.pathname)) return;
    if (!['POST', 'PUT', 'DELETE'].includes(request.method())) return;
    let body = null;
    try { body = request.postDataJSON(); } catch (_) {}
    seen.push({method: request.method(), path: url.pathname, body});
  });
  return seen;
}


/* Hold the NEXT write to one record, so an edit can be made while an action's
 * request is genuinely in flight. Later writes pass straight through. */
async function delayNextWrite(page, id, ms) {
  let held = false;
  await page.route(url => url.pathname === `/api/usenet/servers/${id}`, async route => {
    if (route.request().method() === 'PUT' && !held) {
      held = true;
      await new Promise(resolve => setTimeout(resolve, ms));
    }
    await route.continue();
  });
}

async function delayNextCreate(page, ms) {
  let held = false;
  await page.route(url => url.pathname === '/api/usenet/servers', async route => {
    if (route.request().method() === 'POST' && !held) {
      held = true;
      await new Promise(resolve => setTimeout(resolve, ms));
    }
    await route.continue();
  });
}

test.beforeEach(async ({page}) => {
  await isolateExternalFonts(page);
  await page.goto('/');
  await openSources(page);
  await resetServers(page);
  await page.reload();
  await openSources(page);
  await enableUsenet(page);
  await clearToasts(page);
});

test.afterEach(async ({page}) => { await resetServers(page); });

// --- the collection, its records and their credentials -----------------

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

test('an ordinary field change preserves the stored credential', async ({page}) => {
  const id = await addServer(page, {host: 'news.keep.net', username: 'keeper', password: 'PW-KEEP'});
  await page.reload();
  await openSources(page);
  await enableUsenet(page);

  const card = collection(page).locator(`[data-usenet-server-id="${id}"]`);
  // The reloaded card holds no secret, only the "configured" fact.
  await expect(field(card, 'password')).toHaveValue('');
  await expect(card).toHaveAttribute('data-usenet-password-configured', '1');

  await field(card, 'host').fill('news.keep-renamed.net');
  await field(card, 'host').blur();
  await expect.poll(async () => (await storedServers(page))[0].host).toBe('news.keep-renamed.net');
  // Still configured: writing an ordinary field never touches the credential.
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

test('writing one card never touches another record', async ({page}) => {
  const first = await addServer(page, {host: 'news.alpha.net', username: 'a', password: 'PW-A'});
  await addServer(page, {host: 'news.beta.net', username: 'b', password: 'PW-B'});
  await page.reload();
  await openSources(page);
  await enableUsenet(page);

  const alpha = collection(page).locator(`[data-usenet-server-id="${first}"]`);
  const beta = serverCards(page).nth(1);
  const betaBefore = (await storedServers(page))[1];

  await openAdvanced(alpha);
  await field(alpha, 'connections').fill('12');
  await field(alpha, 'connections').blur();
  await expect.poll(async () => (await storedServers(page))[0].connections).toBe(12);

  // BETA is byte-identical: one record, one writer, one field.
  expect((await storedServers(page))[1]).toEqual(betaBefore);
  await expect(field(beta, 'host')).toHaveValue('news.beta.net');
  await expect(field(beta, 'username')).toHaveValue('b');
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
  // No visual hole: the surviving cards close up and the tile still trails them.
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

  // DP 1.0.13 work item B: renaming uses the canonical application dialog, so
  // there is no browser-native prompt to answer.
  await rename(page, card, 'Primary feed');
  await expect(card.locator('[data-usenet-display-name]')).toHaveText('Primary feed');
  await field(card, 'host').fill('news.changed.net');
  await expect(card.locator('[data-usenet-display-name]')).toHaveText('Primary feed');

  // Clearing the override returns to derived behaviour.
  await rename(page, card, '');
  await expect(card.locator('[data-usenet-display-name]')).toHaveText('news.changed.net');
});

test('a server card exposes no API-key field', async ({page}) => {
  await addTile(page).click();
  const card = serverCards(page).last();
  await openAdvanced(card);
  for (const name of ['host', 'port', 'ssl', 'username', 'password', 'connections', 'priority',
                      'articles_per_request', 'timeout_seconds']) {
    await expect(field(card, name)).toHaveCount(1);
  }
  await expect(card.locator('[data-usenet-field="api_key"]')).toHaveCount(0);
  await expect(card).toContainText('Lower values have priority.');
});

// --- per-control commit classification ---------------------------------

test('an ordinary server field persists on blur, carrying only that field', async ({page}) => {
  const id = await seed(page);
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);
  const seen = writes(page);

  await field(card, 'host').fill('news.edited.net');
  await field(card, 'host').blur();

  await expect.poll(async () => (await record(page, id)).host).toBe('news.edited.net');
  const sent = seen.filter(entry => entry.method === 'PUT');
  expect(sent).toHaveLength(1);
  expect(Object.keys(sent[0].body)).toEqual(['host']);
  // The credential and every other field are untouched.
  const saved = await record(page, id);
  expect(saved.password_configured).toBe(true);
  expect(saved.username).toBe('seeded');
  expect(saved.port).toBe(563);
  // Ordinary success is silent.
  await expect(toasts(page)).toHaveCount(0);
});

test('focusing and leaving an unchanged server field writes nothing', async ({page}) => {
  const id = await seed(page);
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);
  const seen = writes(page);

  await field(card, 'host').focus();
  await field(card, 'host').blur();
  await field(card, 'username').focus();
  await field(card, 'username').blur();
  await page.waitForTimeout(700);
  expect(seen).toEqual([]);
});

test('an advanced field persists on blur exactly like a visible one', async ({page}) => {
  const id = await seed(page);
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);
  await openAdvanced(card);

  await field(card, 'connections').fill('12');
  await field(card, 'connections').blur();
  await expect.poll(async () => (await record(page, id)).connections).toBe(12);
});

test('a field write of one record never disturbs another record', async ({page}) => {
  const first = await seed(page, {host: 'news.alpha.net', username: 'a', password: 'PW-A'});
  const second = await seed(page, {host: 'news.beta.net', username: 'b', password: 'PW-B'});
  await page.reload();
  await openSources(page);

  const before = await record(page, second);
  await field(cardFor(page, first), 'username').fill('alpha-renamed');
  await field(cardFor(page, first), 'username').blur();
  await expect.poll(async () => (await record(page, first)).username).toBe('alpha-renamed');

  expect(await record(page, second)).toEqual(before);
});

test('a failed field write converges the control to canonical state and reports it', async ({page}) => {
  const id = await seed(page);
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);
  await page.route(url => new RegExp(`/api/usenet/servers/${id}$`).test(url.pathname),
    route => route.request().method() === 'PUT'
      ? route.fulfill({status: 502, contentType: 'application/json',
          body: JSON.stringify({detail: 'news server rejected'})})
      : route.continue());

  await field(card, 'host').fill('news.rejected.net');
  await field(card, 'host').blur();

  await expect(toasts(page).first()).toContainText(/reject|error|fail/i);
  await expect(field(card, 'host')).toHaveValue('news.seed.net');
  expect((await record(page, id)).host).toBe('news.seed.net');
});

// --- immediate: SSL ------------------------------------------------------

test('SSL is immediate: no blur, no Save, and it carries the port it follows', async ({page}) => {
  const id = await seed(page);
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);
  const seen = writes(page);

  await card.locator('.dp-usenet-ssl').click();
  await expect(field(card, 'ssl')).not.toBeChecked();

  await expect.poll(async () => (await record(page, id)).ssl).toBe(false);
  await expect.poll(async () => (await record(page, id)).port).toBe(119);
  const sent = seen.filter(entry => entry.method === 'PUT');
  expect(sent).toHaveLength(1);
  expect(Object.keys(sent[0].body).sort()).toEqual(['port', 'ssl']);
  // The control and the port both show canonical truth afterwards.
  await expect(field(card, 'port')).toHaveValue('119');
});

test('a deliberate port survives an immediate SSL change', async ({page}) => {
  const id = await seed(page, {port: 9119});
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);

  await card.locator('.dp-usenet-ssl').click();
  await expect.poll(async () => (await record(page, id)).ssl).toBe(false);
  expect((await record(page, id)).port).toBe(9119);
  await expect(field(card, 'port')).toHaveValue('9119');
});

// --- gated-save: the credential and its confirmation --------------------

test('Save is inactive on an existing record with no gated state', async ({page}) => {
  const id = await seed(page);
  await page.reload();
  await openSources(page);
  await expect(saveButton(cardFor(page, id))).toBeDisabled();
});

test('a typed password is a pending draft that blur never persists', async ({page}) => {
  const id = await seed(page, {password: ''});
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);
  const seen = writes(page);

  await field(card, 'password').fill('PW-DRAFT');
  await field(card, 'password').blur();
  await page.waitForTimeout(700);
  expect(seen).toEqual([]);
  expect((await record(page, id)).password_configured).toBe(false);
  await expect(saveButton(card)).toBeEnabled();

  await saveButton(card).click();
  await expect.poll(async () => (await record(page, id)).password_configured).toBe(true);
  await expect(field(card, 'password')).toHaveValue('');
  await expect(saveButton(card)).toBeDisabled();
});

test('Clear Stored Password expresses intent only, and Save performs it', async ({page}) => {
  const id = await seed(page);
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);

  await card.locator('[data-usenet-clear-password]').check();
  await page.waitForTimeout(700);
  expect((await record(page, id)).password_configured).toBe(true);
  await expect(saveButton(card)).toBeEnabled();

  await saveButton(card).click();
  await expect.poll(async () => (await record(page, id)).password_configured).toBe(false);
});

test('the gated Save writes no ordinary field of its own', async ({page}) => {
  const id = await seed(page, {password: ''});
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);
  const seen = writes(page);

  await field(card, 'password').fill('PW-ONLY');
  await saveButton(card).click();
  await expect.poll(async () => (await record(page, id)).password_configured).toBe(true);

  const gated = seen.filter(entry => entry.method === 'PUT').pop();
  expect(Object.keys(gated.body).sort()).toEqual(['clear_password', 'password']);
});

// --- explicit actions ----------------------------------------------------

test('Test uses the unsaved draft password without persisting it', async ({page}) => {
  const id = await seed(page, {password: ''});
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);

  let probed = null;
  await page.route(url => url.pathname === '/api/usenet/servers/test', async route => {
    probed = route.request().postDataJSON();
    await route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({ok: true, message: 'Connected'})});
  });

  await field(card, 'password').fill('PW-DRAFT-ONLY');
  await card.locator('[data-usenet-action="test"]').click();
  await expect(toasts(page).first()).toContainText('Connected');
  expect(probed.password).toBe('PW-DRAFT-ONLY');
  expect((await record(page, id)).password_configured).toBe(false);
});

test('renaming commits the display name immediately', async ({page}) => {
  const id = await seed(page);
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);

  await card.locator('[data-usenet-action="rename"]').click();
  const dialog = page.locator('.dp-modal-overlay .dp-modal-dialog');
  await expect(dialog).toBeVisible();
  await dialog.locator('.dp-modal-field .input').fill('Primary feed');
  await dialog.locator('[data-modal-accept]').click();

  await expect.poll(async () => (await record(page, id)).display_name).toBe('Primary feed');
});

// --- record creation: the one deliberate exception ----------------------

test('a new card writes no field before its record exists, and Save creates it', async ({page}) => {
  const seen = writes(page);
  await addTile(page).click();
  const card = serverCards(page).last();
  await expect(card).toHaveAttribute('data-usenet-server-id', '');

  await field(card, 'host').fill('news.created.net');
  await field(card, 'host').blur();
  await field(card, 'username').fill('creator');
  await field(card, 'username').blur();
  await page.waitForTimeout(700);
  // No record exists yet, so nothing could be written to one.
  expect(seen).toEqual([]);
  expect(await storedServers(page)).toHaveLength(0);

  await saveButton(card).click();
  await expect.poll(async () => (await storedServers(page)).length).toBe(1);
  expect(seen.filter(entry => entry.method === 'POST')).toHaveLength(1);
  const created = (await storedServers(page))[0];
  expect(created.host).toBe('news.created.net');
  expect(created.username).toBe('creator');
});

test('once created, the card joins the universal model and persists on blur', async ({page}) => {
  await addTile(page).click();
  const card = serverCards(page).last();
  await field(card, 'host').fill('news.joined.net');
  await saveButton(card).click();
  await expect.poll(async () => (await storedServers(page)).length).toBe(1);
  const id = (await storedServers(page))[0].id;
  await expect(card).toHaveAttribute('data-usenet-server-id', id);

  const seen = writes(page);
  await field(card, 'username').fill('now-ordinary');
  await field(card, 'username').blur();
  await expect.poll(async () => (await record(page, id)).username).toBe('now-ordinary');
  const sent = seen.filter(entry => entry.method === 'PUT');
  expect(sent).toHaveLength(1);
  expect(Object.keys(sent[0].body)).toEqual(['username']);
});

// --- action results belong to the canonical toast owner ----------------

test('the private inline action-result surface no longer exists anywhere', async ({page}) => {
  await draftCard(page, 'news.absent.net');
  await expect(page.locator('[data-usenet-status]')).toHaveCount(0);
  await expect(page.locator('.dp-usenet-server-status')).toHaveCount(0);
});

test('Save success is reported by the canonical toast owner', async ({page}) => {
  const card = await draftCard(page, 'news.saved.net');
  await card.locator('[data-usenet-action="save"]').click();
  await expect(toasts(page)).toHaveCount(1);
  await expect(toasts(page).first()).toContainText(/saved/i);
  await expect(page.locator('[data-usenet-status]')).toHaveCount(0);
});

test('Save failure is reported by the canonical toast owner and claims nothing', async ({page}) => {
  await page.route(url => /\/api\/usenet\/servers$/.test(url.pathname),
    route => route.fulfill({status: 502, contentType: 'application/json',
      body: JSON.stringify({detail: 'news server rejected'})}));
  const card = await draftCard(page, 'news.failed.net');
  await card.locator('[data-usenet-action="save"]').click();
  await expect(toasts(page).first()).toContainText(/reject|could not|fail/i);
  expect(await storedServers(page)).toHaveLength(0);
});

test('Test success is reported by the canonical toast owner', async ({page}) => {
  await page.route(url => /\/api\/usenet\/servers\/test$/.test(url.pathname),
    route => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({ok: true, message: 'Connected to news.tested.net'})}));
  const card = await draftCard(page, 'news.tested.net');
  await card.locator('[data-usenet-action="test"]').click();
  await expect(toasts(page).first()).toContainText('Connected to news.tested.net');
});

test('Test failure is reported by the canonical toast owner', async ({page}) => {
  await page.route(url => /\/api\/usenet\/servers\/test$/.test(url.pathname),
    route => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({ok: false, message: 'Authentication rejected'})}));
  const card = await draftCard(page, 'news.badauth.net');
  await card.locator('[data-usenet-action="test"]').click();
  await expect(toasts(page).first()).toContainText('Authentication rejected');
});

test('inline field validation survives and is not a toast', async ({page}) => {
  const card = await draftCard(page, '');
  await card.locator('[data-usenet-action="save"]').click();
  const validation = card.locator('[data-usenet-validation]');
  await expect(validation).toBeVisible();
  await expect(validation).toContainText(/host is required/i);
  await expect(toasts(page)).toHaveCount(0);
  expect(await storedServers(page)).toHaveLength(0);

  // Correcting the field clears the validation without any notification.
  await card.locator('[data-usenet-field="host"]').fill('news.corrected.net');
  await card.locator('[data-usenet-action="save"]').click();
  await expect(validation).toBeHidden();
});

// --- an action's response never overwrites a newer edit -------------------
//
// Settling before dispatch orders everything that existed BEFORE the request.
// It says nothing about an edit made while that request is in flight, so an
// action converges only the controls it actually wrote, and only while they
// still hold what it sent; and every write to one record shares that record's
// lane, so two writes can never overlap.

test('an SSL response cannot overwrite an edit made while it was in flight', async ({page}) => {
  const id = await seed(page);
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);
  await delayNextWrite(page, id, 1500);

  await card.locator('.dp-usenet-ssl').click();
  await field(card, 'username').fill('typed-during-ssl');

  await expect.poll(async () => (await record(page, id)).ssl, {timeout: 15000}).toBe(false);
  await page.waitForTimeout(700);
  // The newer draft survived the older response ...
  await expect(field(card, 'username')).toHaveValue('typed-during-ssl');
  // ... and was not silently swallowed: it is dirty and commits on its blur.
  await field(card, 'username').blur();
  await expect.poll(async () => (await record(page, id)).username).toBe('typed-during-ssl');
});

test('a credential Save response cannot overwrite an ORDINARY edit made while it was in flight',
  async ({page}) => {
    const id = await seed(page, {password: ''});
    await page.reload();
    await openSources(page);
    const card = cardFor(page, id);
    await field(card, 'password').fill('PW-INFLIGHT');
    await delayNextWrite(page, id, 1500);

    await saveButton(card).click();
    await field(card, 'username').fill('typed-during-save');

    await expect.poll(async () => (await record(page, id)).password_configured,
      {timeout: 15000}).toBe(true);
    await page.waitForTimeout(700);
    await expect(field(card, 'username')).toHaveValue('typed-during-save');
    await field(card, 'username').blur();
    await expect.poll(async () => (await record(page, id)).username).toBe('typed-during-save');
  });

test('a rename response cannot overwrite an edit made while it was in flight', async ({page}) => {
  const id = await seed(page);
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);
  await delayNextWrite(page, id, 1500);

  await rename(page, card, 'Primary feed');
  await field(card, 'username').fill('typed-during-rename');

  await expect.poll(async () => (await record(page, id)).display_name,
    {timeout: 15000}).toBe('Primary feed');
  await page.waitForTimeout(700);
  await expect(field(card, 'username')).toHaveValue('typed-during-rename');
  await field(card, 'username').blur();
  await expect.poll(async () => (await record(page, id)).username).toBe('typed-during-rename');
});

test('a port changed after SSL carried it converges on the operator value', async ({page}) => {
  const id = await seed(page);                       // ssl true, conventional port 563
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);
  await delayNextWrite(page, id, 1200);

  // The toggle carries the conventional port it follows ...
  await card.locator('.dp-usenet-ssl').click();
  // ... and the operator then chooses a deliberate one.
  await field(card, 'port').fill('9119');
  await field(card, 'port').blur();

  await expect.poll(async () => (await record(page, id)).port, {timeout: 15000}).toBe(9119);
  await page.waitForTimeout(900);
  const saved = await record(page, id);
  expect(saved.port).toBe(9119);
  expect(saved.ssl).toBe(false);
  await expect(field(card, 'port')).toHaveValue('9119');

  // The baseline converged too: an unchanged blur now writes nothing.
  const seen = writes(page);
  await field(card, 'port').focus();
  await field(card, 'port').blur();
  await page.waitForTimeout(600);
  expect(seen).toEqual([]);
});

// --- a gated mutation consumes only the intent it dispatched --------------
//
// Ordinary controls are protected by scoped convergence. The GATED controls --
// the credential itself and its Clear confirmation -- need the same rule: a
// completed write may consume only the exact intent it sent, and must never
// erase intent the operator created after dispatch.

test('a credential Save consumes only the credential it dispatched', async ({page}) => {
  const id = await seed(page, {password: ''});
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);
  const seen = writes(page);

  await field(card, 'password').fill('PW-A');
  await delayNextWrite(page, id, 1500);
  await saveButton(card).click();
  // Newer gated intent, created while PW-A is still on the wire.
  await field(card, 'password').fill('PW-B');

  await expect.poll(async () => (await record(page, id)).password_configured,
    {timeout: 15000}).toBe(true);
  await page.waitForTimeout(700);

  // PW-A is what was accepted ...
  expect(seen.filter(entry => entry.method === 'PUT')[0].body.password).toBe('PW-A');
  // ... and PW-B is still pending, visible, and still offered for commit.
  await expect(field(card, 'password')).toHaveValue('PW-B');
  await expect(saveButton(card)).toBeEnabled();

  await saveButton(card).click();
  await expect.poll(() => seen.filter(entry => entry.method === 'PUT').length).toBe(2);
  expect(seen.filter(entry => entry.method === 'PUT')[1].body.password).toBe('PW-B');
  await expect(field(card, 'password')).toHaveValue('');
  await expect(saveButton(card)).toBeDisabled();
});

test('a credential Save never disarms a Clear confirmation armed after dispatch',
  async ({page}) => {
    // Seeded WITH a credential: the Clear confirmation only exists on a card
    // that has something stored to clear.
    const id = await seed(page);
    await page.reload();
    await openSources(page);
    const card = cardFor(page, id);

    await field(card, 'password').fill('PW-A');
    await delayNextWrite(page, id, 1500);
    await saveButton(card).click();          // dispatched with clear_password: false
    // The operator changes their mind while the write is in flight.
    await card.locator('[data-usenet-clear-password]').check();

    await expect.poll(async () => (await record(page, id)).password_configured,
      {timeout: 15000}).toBe(true);
    await page.waitForTimeout(700);

    // The newer gated intent survived the older response.
    await expect(card.locator('[data-usenet-clear-password]')).toBeChecked();
    await expect(saveButton(card)).toBeEnabled();

    await saveButton(card).click();
    await expect.poll(async () => (await record(page, id)).password_configured).toBe(false);
  });

test('record creation consumes only the credential it dispatched', async ({page}) => {
  const seen = writes(page);
  const card = await draftCard(page, 'news.created-gated.net');
  await field(card, 'password').fill('PW-A');
  await delayNextCreate(page, 1500);

  await saveButton(card).click();
  // Newer gated intent, typed while the record is still being minted.
  await field(card, 'password').fill('PW-B');

  await expect.poll(async () => (await storedServers(page)).length, {timeout: 15000}).toBe(1);
  await page.waitForTimeout(700);
  const created = (await storedServers(page))[0];
  expect(created.password_configured).toBe(true);
  expect(seen.filter(entry => entry.method === 'POST')[0].body.password).toBe('PW-A');

  // The freshly minted record keeps PW-B as pending gated intent.
  await expect(card).toHaveAttribute('data-usenet-server-id', created.id);
  await expect(field(card, 'password')).toHaveValue('PW-B');
  await expect(saveButton(card)).toBeEnabled();

  await saveButton(card).click();
  await expect.poll(() => seen.filter(entry => entry.method === 'PUT').length).toBe(1);
  expect(seen.filter(entry => entry.method === 'PUT')[0].body.password).toBe('PW-B');
});

// --- the same invariant, on an INSTANCED control -------------------------
//
// The record-scoped identity (scope | key | instance) must obey the generic
// owner's rollback rule exactly as a page-level control does.

test('an older record write that succeeded is what a newer failed write rolls back to',
  async ({page}) => {
    const id = await seed(page);
    await page.reload();
    await openSources(page);
    const card = cardFor(page, id);

    let attempt = 0;
    await page.route(url => url.pathname === `/api/usenet/servers/${id}`, async route => {
      if (route.request().method() !== 'PUT') return route.continue();
      attempt += 1;
      if (attempt === 1) {                          // the older write: slow, accepted
        await new Promise(resolve => setTimeout(resolve, 1500));
        return route.continue();
      }
      return route.fulfill({status: 502, contentType: 'application/json',     // the newer one: refused
        body: JSON.stringify({detail: 'news server rejected'})});
    });

    await field(card, 'host').fill('news.accepted.net');
    await field(card, 'host').blur();
    // Queued behind the first while it is still in flight.
    await field(card, 'host').fill('news.refused.net');
    await field(card, 'host').blur();

    await expect(toasts(page).first()).toContainText(/reject|error|fail/i, {timeout: 15000});
    await page.waitForTimeout(700);

    // The record holds the older, accepted value ...
    expect((await record(page, id)).host).toBe('news.accepted.net');
    // ... and so does the control.
    await expect(field(card, 'host')).toHaveValue('news.accepted.net');

    // The baseline converged there too: an unchanged blur now writes nothing.
    const seen = writes(page);
    await field(card, 'host').focus();
    await field(card, 'host').blur();
    await page.waitForTimeout(700);
    expect(seen).toEqual([]);
  });

// --- the record CREATION boundary ----------------------------------------
//
// Creation is the one moment a card has no canonical identity, so nothing on
// it can be field-committed. The card stays interactive throughout, which
// means the operator can express intent AFTER the creation write is dispatched
// and BEFORE the id comes back. That intent must be carried onto the freshly
// minted record in the right semantic order -- it may never be stranded in the
// browser, silently replaced by the creation response, or left behind as a
// backend record with no card.

test('an ordinary field left while the record is being minted persists without a second blur',
  async ({page}) => {
    const seen = writes(page);
    const card = await draftCard(page, 'news.creation-field.net');
    await field(card, 'username').fill('before');
    await delayNextCreate(page, 1500);

    await saveButton(card).click();
    // Newer ordinary intent whose commit boundary is genuinely crossed while
    // the record is still being minted.
    await field(card, 'username').fill('after');
    await field(card, 'username').blur();

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    const id = (await storedServers(page))[0].id;
    expect(seen.filter(entry => entry.method === 'POST')[0].body.username).toBe('before');

    // The operator already left the field: no second blur may be required.
    await expect.poll(async () => (await record(page, id)).username, {timeout: 20000}).toBe('after');
    await expect(field(card, 'username')).toHaveValue('after');
  });

test('an SSL toggle made while the record is being minted reaches the canonical record',
  async ({page}) => {
    const card = await draftCard(page, 'news.creation-ssl.net');
    await delayNextCreate(page, 1500);

    await saveButton(card).click();
    // SSL is immediate: performing it IS the act, so it cannot wait for a
    // later blur the operator has no reason to make.
    await setSsl(card, false);

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    const id = (await storedServers(page))[0].id;
    await expect.poll(async () => (await record(page, id)).ssl, {timeout: 20000}).toBe(false);
    // The conventional port travelled with it: one operator action, not two.
    expect((await record(page, id)).port).toBe(119);
    await expect(field(card, 'ssl')).not.toBeChecked();
    await expect(field(card, 'port')).toHaveValue('119');
  });

test('a display name chosen while the record is being minted survives and is canonical',
  async ({page}) => {
    const card = await draftCard(page, 'news.creation-name.net');
    await delayNextCreate(page, 2500);

    await saveButton(card).click();
    await rename(page, card, 'Chosen Later');

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    const id = (await storedServers(page))[0].id;
    // The creation response carried the derived name; the newer choice wins.
    await expect.poll(async () => (await record(page, id)).display_name, {timeout: 20000})
      .toBe('Chosen Later');
    await expect(card.locator('[data-usenet-display-name]')).toHaveText('Chosen Later');
  });

test('removing a card while its record is being minted never leaves an orphan record',
  async ({page}) => {
    const card = await draftCard(page, 'news.creation-orphan.net');
    await delayNextCreate(page, 1500);

    await saveButton(card).click();
    await card.locator('[data-usenet-action="remove"]').click();

    // The minting may well succeed; what may never happen is a surviving
    // backend record with no card to govern it.
    await expect.poll(async () => (await storedServers(page)).length, {timeout: 25000}).toBe(0);
    await expect(serverCards(page)).toHaveCount(0);
    await page.waitForTimeout(1500);
    expect(await storedServers(page)).toEqual([]);
  });

// --- WHICH draft crossed the boundary, not merely which control did --------
//
// A commit boundary belongs to a specific DRAFT. While a record is being
// minted nothing can be written, so the draft that crossed the boundary is
// remembered and replayed once identity exists -- but a draft typed AFTER it,
// which never crossed a boundary of its own, must not be promoted along with
// it. It stays visible, dirty, and commits on its own blur, exactly as it
// would have done had the record existed all along.

test('only the draft that crossed the boundary during creation is committed, never a later unblurred one',
  async ({page}) => {
    const seen = writes(page);
    const card = await draftCard(page, 'news.creation-draft.net');
    await field(card, 'username').fill('first');
    await delayNextCreate(page, 1500);

    await saveButton(card).click();
    // Crossed its boundary while the record was being minted.
    await field(card, 'username').fill('second');
    await field(card, 'username').blur();
    // Typed after that, and deliberately never left.
    await field(card, 'username').fill('third');

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    const id = (await storedServers(page))[0].id;
    expect(seen.filter(entry => entry.method === 'POST')[0].body.username).toBe('first');

    // The blurred draft is what reaches the record ...
    await expect.poll(async () => (await record(page, id)).username, {timeout: 20000}).toBe('second');
    await expect.poll(() => committedBaseline(page, id, 'username')).toBe('second');
    // ... and the unblurred one is still the operator's, still pending.
    await expect(field(card, 'username')).toHaveValue('third');

    // Leaving it now commits it exactly like any other changed blur.
    await field(card, 'username').blur();
    await expect.poll(async () => (await record(page, id)).username, {timeout: 20000}).toBe('third');
    await expect.poll(() => committedBaseline(page, id, 'username')).toBe('third');

    // The baseline converged, so an unchanged blur writes nothing.
    const after = writes(page);
    await field(card, 'username').focus();
    await field(card, 'username').blur();
    await page.waitForTimeout(700);
    expect(after).toEqual([]);
  });

test('only the LATEST draft that crossed a boundary during creation is committed',
  async ({page}) => {
    const seen = writes(page);
    const card = await draftCard(page, 'news.creation-latest.net');
    await field(card, 'username').fill('first');
    await delayNextCreate(page, 2000);

    await saveButton(card).click();
    await field(card, 'username').fill('second');
    await field(card, 'username').blur();
    await field(card, 'username').fill('third');
    await field(card, 'username').blur();
    // Never left, so never committed.
    await field(card, 'username').fill('fourth');

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    const id = (await storedServers(page))[0].id;
    await expect.poll(async () => (await record(page, id)).username, {timeout: 20000}).toBe('third');
    await page.waitForTimeout(700);

    // Exactly one record write replayed the boundary -- the latest one.
    const replayed = seen.filter(entry => entry.method === 'PUT' && 'username' in (entry.body || {}));
    expect(replayed.map(entry => entry.body.username)).toEqual(['third']);
    await expect(field(card, 'username')).toHaveValue('fourth');
  });

// --- an immediate ACTION is remembered as an action -----------------------
//
// SSL is immediate: performing it IS the act, and the conventional port it
// moves is part of that one act. While a record is being minted the act
// cannot be written, so the exact payload it WOULD have written is what is
// remembered -- never the form state the card happens to hold later. A value
// typed afterwards is a different intent and is not folded into it.

test('an SSL action during creation replays its own payload, never later form state',
  async ({page}) => {
    const card = await draftCard(page, 'news.creation-ssl-draft.net');
    await delayNextCreate(page, 2000);

    await saveButton(card).click();
    // The act: SSL off, carrying the conventional port it follows.
    await setSsl(card, false);
    // Typed afterwards and deliberately never left: a changed-blur draft that
    // has crossed no boundary of its own.
    await field(card, 'port').fill('9119');

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    const id = (await storedServers(page))[0].id;
    await expect.poll(async () => (await record(page, id)).ssl, {timeout: 20000}).toBe(false);
    // The action's OWN port, not the draft that came after it.
    await expect.poll(async () => (await record(page, id)).port, {timeout: 20000}).toBe(119);
    await expect(field(card, 'port')).toHaveValue('9119');
    await expect.poll(() => committedBaseline(page, id, 'port')).toBe('119');

    // Leaving it now commits it exactly like any other changed blur.
    await field(card, 'port').blur();
    await expect.poll(async () => (await record(page, id)).port, {timeout: 20000}).toBe(9119);
    await expect.poll(() => committedBaseline(page, id, 'port')).toBe('9119');
  });

test('a port boundary crossed after an SSL action during creation wins over it',
  async ({page}) => {
    const seen = writes(page);
    const card = await draftCard(page, 'news.creation-ssl-order.net');
    await delayNextCreate(page, 2500);

    await saveButton(card).click();
    await setSsl(card, false);
    await field(card, 'port').fill('9119');
    await field(card, 'port').blur();

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    const id = (await storedServers(page))[0].id;
    await expect.poll(async () => (await record(page, id)).ssl, {timeout: 20000}).toBe(false);
    await expect.poll(async () => (await record(page, id)).port, {timeout: 20000}).toBe(9119);
    await page.waitForTimeout(900);

    // Ordering, not final-value reconstruction: the SSL act replayed its OWN
    // port, and the later boundary then won -- and nothing put 119 back.
    const puts = seen.filter(entry => entry.method === 'PUT');
    const act = puts.filter(entry => 'ssl' in (entry.body || {}));
    expect(act.map(entry => entry.body.port)).toEqual([119]);
    expect(puts.some(entry => entry.body?.port === 9119)).toBeTruthy();
    expect((await record(page, id)).port).toBe(9119);
    await expect(field(card, 'port')).toHaveValue('9119');
  });

test('an SSL action that moves the port supersedes a port boundary crossed before it',
  async ({page}) => {
    const card = await draftCard(page, 'news.creation-ssl-supersede.net');
    await delayNextCreate(page, 3000);

    await saveButton(card).click();
    // A boundary crossed first ...
    await field(card, 'port').fill('119');
    await field(card, 'port').blur();
    // ... then two acts, the last of which moves the port back. The port it
    // moves is part of THAT act, so it supersedes the earlier boundary.
    await setSsl(card, false);
    await setSsl(card, true);

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    const id = (await storedServers(page))[0].id;
    await expect.poll(async () => (await record(page, id)).ssl, {timeout: 20000}).toBe(true);
    await page.waitForTimeout(900);
    expect((await record(page, id)).port).toBe(563);
    await expect(field(card, 'port')).toHaveValue('563');
    await expect.poll(() => committedBaseline(page, id, 'port')).toBe('563');
  });
