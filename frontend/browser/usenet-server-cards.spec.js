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
 * explicit, confirmed Clear.
 *
 * A card is NOT a credential transaction. Every control on it is classified by
 * ITS OWN semantics and risk, exactly like every other Settings control:
 *
 *   host / port / username / connections / priority /
 *   articles per request / timeout          changed-blur
 *   password (entry / replacement)          changed-blur  (DP 1.0.13)
 *   SSL                                     immediate
 *   display name                            committed when its dialog accepts
 *   Test / Remove / Clear / Add Server      explicit-action
 *
 * DP 1.0.13: there is no Save. A card with no canonical id yet is the one
 * deliberate exception -- nothing can be written to a record that does not
 * exist -- so the canonical persistence owner asks the Usenet scope to
 * MATERIALIZE it the first time an ordinary commit boundary is crossed on a
 * draft that has a Host. From that moment the card is an ordinary member of
 * the universal persistence model.
 *
 * Usenet owns no notification system: action RESULTS are the canonical toast
 * owner's, while inline FIELD VALIDATION is a different, narrower thing that
 * survives.
 */

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

/* Arrive at Services with the Usenet card OPEN.
 *
 * Every expandable card is collapsed on navigation -- expansion is local
 * presentation state, never a projection of enabled/configured/verified state
 * -- so reaching this collection means opening the card, exactly as the
 * operator does, through the one canonical disclosure. This whole spec is
 * about the server collection inside that card, so opening it is part of
 * "get to the collection" rather than something each case restates.
 *
 * `expand: false` is for the one case that is ABOUT the collapsed arrival. */
async function openSources(page, {expand = true} = {}) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator('#view-settings [data-tab="sources"]').click();
  await expect(page.locator('.dp-settings-panel[data-panel="sources"]')).toBeVisible();
  if (expand) await expandUsenet(page);
}

const usenetCard = page => page.locator('.dp-settings-provider-card--usenet');
const collection = page => page.locator('[data-usenet-collection]');
const serverCards = page => collection(page).locator('[data-usenet-server-id]');
const addTile = page => collection(page).locator('[data-usenet-action="add"]');
const cardFor = (page, id) => collection(page).locator(`[data-usenet-server-id="${id}"]`);
const field = (card, name) => card.locator(`[data-usenet-field="${name}"]`);
const clearButton = card => card.locator('[data-usenet-action="clear-password"]');
const removeButton = card => card.locator('[data-usenet-action="remove"]');

/* The ONE canonical Settings confirmation (ui-settings-modal.js). Both
 * destructive server actions open it; the card itself carries no confirmation
 * representation at all, so this spec drives that one dialog. */
const dangerDialog = page => page.locator('.dp-modal-overlay .dp-modal-dialog[data-tone="danger"]');
const acceptDanger = page => dangerDialog(page).locator('[data-modal-accept]');
const cancelDanger = page => dangerDialog(page).locator('[data-modal-cancel]');
/** Press a destructive control and agree to what it asks. */
async function confirmDanger(page, control) {
  await control.click();
  await expect(dangerDialog(page)).toBeVisible();
  await acceptDanger(page).click();
  await expect(dangerDialog(page)).toHaveCount(0);
}
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

/** Turn Usenet on the way an operator does: the toggle IS the commit. */
async function enableUsenet(page) {
  const toggle = page.locator('[data-integration-enabled="usenet"]');
  if (!(await toggle.isChecked())) {
    await page.locator('label[for="dp-settings-integration-usenet-enabled"]').click();
  }
  await expect(toggle).toBeChecked();
  await expect.poll(async () => {
    const s = await page.request.get('/api/settings').then(r => r.json());
    return s.integrations.usenet.enabled;
  }).toBe(true);
  await expandUsenet(page);
}

/* Expansion is LOCAL PRESENTATION STATE, never a projection of enabled,
 * configured or verified state, so a card an earlier case already enabled
 * still renders collapsed. These cases operate the card's controls, so they
 * open it through the one canonical disclosure the operator uses. */
async function expandUsenet(page) {
  const card = page.locator('.dp-settings-provider-card--usenet');
  const disclosure = card.locator('.dp-settings-disclosure');
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  await expect(card).not.toHaveClass(/dp-settings-provider-card--collapsed/);
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

/* The canonical id of THE CARD UNDER TEST, once its record exists.
 *
 * Deliberately read from the card rather than from "the only stored server":
 * a record is identified by its identity, never by its position, and an
 * assertion that assumes the collection holds exactly one row is measuring the
 * collection instead of the card. */
async function mintedId(card) {
  await expect.poll(() => card.getAttribute('data-usenet-server-id'), {timeout: 20000}).not.toBe('');
  return card.getAttribute('data-usenet-server-id');
}

/* The boundary that turns a draft card into a canonical record: an ordinary
 * changed-blur commit on a card that has a Host. There is no Save. */
async function materialize(page, card) {
  await field(card, 'host').blur();
  return mintedId(card);
}

/** Create one server through the UI and return its canonical id. */
async function addServer(page, {host, username, password, port}) {
  const card = await draftCard(page, host);
  const id = await materialize(page, card);
  await expect.poll(async () => (await storedServers(page)).some(s => s.host === host)).toBeTruthy();
  const commit = async (name, value) => {
    await field(card, name).fill(String(value));
    await field(card, name).blur();
  };
  if (port !== undefined) {
    await commit('port', port);
    await expect.poll(async () => (await record(page, id)).port).toBe(Number(port));
  }
  if (username !== undefined) {
    await commit('username', username);
    await expect.poll(async () => (await record(page, id)).username).toBe(username);
  }
  if (password !== undefined) {
    await commit('password', password);
    await expect.poll(async () => (await record(page, id)).password_configured).toBe(true);
  }
  return id;
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

/* The ONE automatic expansion in Sources & Providers: an operator who admits
 * a provider that has nothing configured is immediately shown what to
 * configure. Navigation never does this, and re-rendering never does this. */
test('enabling an unconfigured Usenet expands the card and offers a single Add Server tile', async ({page}) => {
  // Back to disabled, then navigate: the card is collapsed on arrival.
  await page.locator('label[for="dp-settings-integration-usenet-enabled"]').click();
  await expect.poll(async () =>
    (await page.request.get('/api/settings').then(r => r.json())).integrations.usenet.enabled).toBe(false);
  await page.reload();
  await openSources(page, {expand: false});
  await expect(usenetCard(page)).toHaveClass(/dp-settings-provider-card--collapsed/);
  await expect(usenetCard(page).locator('.dp-settings-provider-config-status')).toBeHidden();

  // The accepted enable of an UNCONFIGURED provider opens it.
  await page.locator('label[for="dp-settings-integration-usenet-enabled"]').click();
  await expect.poll(async () =>
    (await page.request.get('/api/settings').then(r => r.json())).integrations.usenet.enabled).toBe(true);
  await expect(usenetCard(page)).not.toHaveClass(/dp-settings-provider-card--collapsed/);
  const header = usenetCard(page).locator('.dp-settings-provider-config-status');
  await expect(header).toHaveText('Unconfigured');
  await expect(header).toHaveAttribute('data-tone', 'error');
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

test('clearing the password is explicit, confirmed, and does erase it', async ({page}) => {
  const id = await addServer(page, {host: 'news.clear.net', username: 'c', password: 'PW-CLEAR'});
  await page.reload();
  await openSources(page);
  await enableUsenet(page);
  const card = collection(page).locator(`[data-usenet-server-id="${id}"]`);
  await expect(clearButton(card)).toBeEnabled();
  await expect(clearButton(card)).toHaveText('Clear Password');
  await confirmDanger(page, clearButton(card));
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

  await confirmDanger(page, removeButton(
    collection(page).locator(`[data-usenet-server-id="${middle}"]`)));
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
  await confirmDanger(page, removeButton(
    collection(page).locator(`[data-usenet-server-id="${only}"]`)));
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

// --- the credential: changed-blur entry, explicit confirmed removal -------

test('an entered password persists on blur and is never retained by the browser',
  async ({page}) => {
    const id = await seed(page, {password: ''});
    await page.reload();
    await openSources(page);
    const card = cardFor(page, id);
    const seen = writes(page);

    await field(card, 'password').fill('PW-ON-BLUR');
    await field(card, 'password').blur();

    await expect.poll(async () => (await record(page, id)).password_configured).toBe(true);
    const sent = seen.filter(entry => entry.method === 'PUT');
    expect(sent).toHaveLength(1);
    // Only the credential travelled.
    expect(Object.keys(sent[0].body)).toEqual(['password']);
    // The accepted presentation of a secret is blank, in the field and in the
    // canonical persistence owner's baseline alike.
    await expect(field(card, 'password')).toHaveValue('');
    await expect.poll(() => committedBaseline(page, id, 'password')).toBe('');

    // So leaving it again writes nothing.
    const after = writes(page);
    await field(card, 'password').focus();
    await field(card, 'password').blur();
    await page.waitForTimeout(700);
    expect(after).toEqual([]);
  });

test('a blank password is "no replacement", never a removal', async ({page}) => {
  const id = await seed(page);
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);
  const seen = writes(page);

  await expect(field(card, 'password')).toHaveValue('');
  await field(card, 'password').focus();
  await field(card, 'password').blur();
  await page.waitForTimeout(700);
  expect(seen).toEqual([]);
  expect((await record(page, id)).password_configured).toBe(true);
});

test('Clear is confirmation-gated, carries only the removal, and converges on success',
  async ({page}) => {
    const id = await seed(page);
    await page.reload();
    await openSources(page);
    const card = cardFor(page, id);
    const seen = writes(page);

    // No card-local gate survives: the action is available, and opening the
    // canonical confirmation is not a mutation. Cancelling performs none.
    await expect(clearButton(card)).toBeEnabled();
    await clearButton(card).click();
    await expect(dangerDialog(page)).toBeVisible();
    await expect(acceptDanger(page)).toHaveText('Clear Password');
    await cancelDanger(page).click();
    await expect(dangerDialog(page)).toHaveCount(0);
    await page.waitForTimeout(500);
    expect(seen).toEqual([]);
    expect((await record(page, id)).password_configured).toBe(true);

    await confirmDanger(page, clearButton(card));
    await expect.poll(async () => (await record(page, id)).password_configured).toBe(false);
    // Clear carries only the removal -- never a replacement value.
    const sent = seen.filter(entry => entry.method === 'PUT');
    expect(sent).toHaveLength(1);
    expect(sent[0].body).toEqual({clear_password: true});
    // DP 1.0.13: nothing is left to clear, and the action says so in place
    // rather than vanishing -- the password row keeps its own geometry either
    // way, so saving or clearing a credential never moves the field.
    await expect(clearButton(card)).toBeVisible();
    await expect(clearButton(card)).toBeDisabled();
  });

test('the clear confirmation names the server, and falls back to its host', async ({page}) => {
  const id = await seed(page, {host: 'news.identity.net'});
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);

  // No chosen display name: the host is the server's visible identity.
  await clearButton(card).click();
  await expect(dangerDialog(page).locator('.dp-modal-title'))
    .toHaveText('Clear password for news.identity.net?');
  await expect(dangerDialog(page).locator('.dp-modal-message'))
    .toContainText('news.identity.net');
  await cancelDanger(page).click();

  // A chosen display name outranks the host.
  await page.request.put(`/api/usenet/servers/${id}`, {data: {display_name: 'Primary Feed'}});
  await page.reload();
  await openSources(page);
  await clearButton(cardFor(page, id)).click();
  await expect(dangerDialog(page).locator('.dp-modal-title'))
    .toHaveText('Clear password for Primary Feed?');
  await cancelDanger(page).click();
});

test('a failed Clear renders no false cleared state', async ({page}) => {
  const id = await seed(page);
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);

  await page.route(url => url.pathname === `/api/usenet/servers/${id}`,
    route => route.request().method() === 'PUT'
      ? route.fulfill({status: 502, contentType: 'application/json',
          body: JSON.stringify({detail: 'news server rejected'})})
      : route.continue());

  await confirmDanger(page, clearButton(card));
  await expect(toasts(page).first()).toContainText(/reject|could not|fail/i);
  // A password is still stored, so the action that erases one is still offered.
  await expect(clearButton(card)).toBeVisible();
  await expect(clearButton(card)).toBeEnabled();
  await page.unrouteAll({behavior: 'ignoreErrors'}).catch(() => {});
  expect((await record(page, id)).password_configured).toBe(true);
});

test('Remove is confirmation-gated and names the server', async ({page}) => {
  const id = await seed(page, {host: 'news.removable.net'});
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);
  const seen = writes(page);

  await removeButton(card).click();
  await expect(dangerDialog(page)).toBeVisible();
  await expect(dangerDialog(page).locator('.dp-modal-title'))
    .toHaveText('Remove news.removable.net?');
  await expect(acceptDanger(page)).toHaveText('Remove Server');
  await expect(dangerDialog(page).locator('.dp-modal-message'))
    .toContainText(/removed from DebridPulse/i);

  // Cancel removes nothing: not the card, and not the record.
  await cancelDanger(page).click();
  await expect(dangerDialog(page)).toHaveCount(0);
  await page.waitForTimeout(500);
  await expect(card).toBeVisible();
  expect(seen.filter(entry => entry.method === 'DELETE')).toEqual([]);
  expect(await record(page, id)).toBeTruthy();

  // Confirming runs the existing removal owner exactly once.
  await confirmDanger(page, removeButton(card));
  await expect.poll(async () => (await storedServers(page)).length).toBe(0);
  await expect(card).toHaveCount(0);
});

test('an unsaved draft is confirmed too, and invents no backend DELETE', async ({page}) => {
  const seen = writes(page);
  await addTile(page).click();
  const card = serverCards(page).last();
  // A blank local draft has no identity to state, so the dialog says so.
  await removeButton(card).click();
  await expect(dangerDialog(page).locator('.dp-modal-title'))
    .toHaveText('Remove this Usenet server?');
  await cancelDanger(page).click();
  await expect(card).toBeVisible();

  await confirmDanger(page, removeButton(card));
  await expect(serverCards(page)).toHaveCount(0);
  await page.waitForTimeout(700);
  // The draft was never a record, so nothing was asked of the backend.
  expect(seen.filter(entry => entry.method === 'DELETE')).toEqual([]);
  expect(await storedServers(page)).toEqual([]);
});

test('no Save action survives on a server card', async ({page}) => {
  const id = await seed(page);
  await page.reload();
  await openSources(page);
  await expect(cardFor(page, id).locator('[data-usenet-action="save"]')).toHaveCount(0);
  await draftCard(page, 'news.nosave.net');
  await expect(serverCards(page).last().locator('[data-usenet-action="save"]')).toHaveCount(0);
});

// --- explicit actions ----------------------------------------------------

/* DP 1.0.13: Test settles pending changed-blur commits first, so a credential
 * the operator has just typed is persisted by ITS OWN boundary before Test
 * runs -- Test never saves it. The request then carries a blank secret, which
 * the endpoint reads as "use this server's stored credential", so Test still
 * exercises exactly what the operator entered. */
test('Test settles the typed credential first and never saves it itself', async ({page}) => {
  const id = await seed(page, {password: ''});
  await page.reload();
  await openSources(page);
  const card = cardFor(page, id);

  const order = [];
  page.on('requestfinished', request => {
    const path = new URL(request.url()).pathname;
    if (path === `/api/usenet/servers/${id}` && request.method() === 'PUT') order.push('commit-finished');
  });
  let probed = null;
  await page.route(url => url.pathname === '/api/usenet/servers/test', async route => {
    probed = route.request().postDataJSON();
    order.push('test-sent');
    await route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({ok: true, message: 'Connected'})});
  });

  await field(card, 'password').fill('PW-SETTLED');
  // No explicit blur: clicking Test is what removes focus.
  await card.locator('[data-usenet-action="test"]').click();
  await expect(toasts(page).first()).toContainText('Connected');

  expect(order).toEqual(['commit-finished', 'test-sent']);
  // Test carried no credential of its own ...
  expect(probed.password).toBe('');
  expect(probed.server_id).toBe(id);
  // ... because the credential's own boundary had already persisted it.
  expect((await record(page, id)).password_configured).toBe(true);
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

test('Add Server alone creates no backend record', async ({page}) => {
  const seen = writes(page);
  await addTile(page).click();
  const card = serverCards(page).last();
  await expect(card).toHaveAttribute('data-usenet-server-id', '');
  await page.waitForTimeout(700);
  expect(seen).toEqual([]);
  expect(await storedServers(page)).toHaveLength(0);
});

test('a valid Host plus one ordinary commit boundary creates exactly one record',
  async ({page}) => {
    const seen = writes(page);
    const card = await draftCard(page, 'news.created.net');
    await field(card, 'username').fill('creator');
    // Moving focus off Host is the boundary; the creation carries the card's
    // current values, which is the one approved creation exception.
    await field(card, 'username').blur();

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    expect(seen.filter(entry => entry.method === 'POST')).toHaveLength(1);
    const created = await record(page, await mintedId(card));
    expect(created.host).toBe('news.created.net');
    await expect(card).toHaveAttribute('data-usenet-server-id', created.id);
    // The username boundary reaches the record either with the creation or
    // immediately after it, without a second blur.
    await expect.poll(async () => (await record(page, created.id)).username,
      {timeout: 20000}).toBe('creator');
  });

test('a creation carries a password already entered on the draft card', async ({page}) => {
  const seen = writes(page);
  await addTile(page).click();
  const card = serverCards(page).last();
  // Entered while the card has no Host at all, so no boundary can create a
  // record yet -- the approved creation exception is exercised deliberately.
  await field(card, 'password').fill('PW-AT-CREATION');
  await field(card, 'host').fill('news.created-pw.net');
  await field(card, 'host').blur();

  await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
  // The creation itself carried the credential, because there was no record
  // for it to have been written to.
  const posts = seen.filter(entry => entry.method === 'POST');
  expect(posts).toHaveLength(1);
  expect(posts[0].body.password).toBe('PW-AT-CREATION');
  const id = await mintedId(card);
  await expect.poll(async () => (await record(page, id)).password_configured,
    {timeout: 20000}).toBe(true);
  // The browser retains no secret, and exactly one record exists.
  await expect(field(card, 'password')).toHaveValue('');
  await expect.poll(() => committedBaseline(page, id, 'password')).toBe('');
  expect(await storedServers(page)).toHaveLength(1);
});

test('a blank Host creates nothing and keeps inline field validation', async ({page}) => {
  const seen = writes(page);
  const card = await draftCard(page, '');
  await field(card, 'username').fill('nobody');
  await field(card, 'username').blur();

  const validation = card.locator('[data-usenet-validation]');
  await expect(validation).toBeVisible();
  await expect(validation).toContainText(/host is required/i);
  await expect(toasts(page)).toHaveCount(0);
  expect(seen).toEqual([]);
  expect(await storedServers(page)).toHaveLength(0);

  // Correcting the Host creates it, and the validation clears with no toast
  // of its own.
  await field(card, 'host').fill('news.corrected.net');
  await field(card, 'host').blur();
  await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
  await expect(validation).toBeHidden();
});

test('several boundaries crossed while the creation is in flight create one record',
  async ({page}) => {
    const seen = writes(page);
    const card = await draftCard(page, 'news.single-record.net');
    await delayNextCreate(page, 1500);
    await field(card, 'host').blur();
    // More boundaries, all while the one creation is still on the wire.
    await field(card, 'username').fill('a');
    await field(card, 'username').blur();
    await field(card, 'username').fill('b');
    await field(card, 'username').blur();

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 25000}).toBe(1);
    await page.waitForTimeout(1500);
    expect(seen.filter(entry => entry.method === 'POST')).toHaveLength(1);
    expect(await storedServers(page)).toHaveLength(1);
  });

test('once created, the card joins the universal model and persists on blur', async ({page}) => {
  const card = await draftCard(page, 'news.joined.net');
  const id = await materialize(page, card);
  await expect.poll(async () => (await storedServers(page)).length).toBe(1);
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

test('creation success is reported by the canonical toast owner', async ({page}) => {
  const card = await draftCard(page, 'news.saved.net');
  await field(card, 'host').blur();
  await expect(toasts(page)).toHaveCount(1);
  await expect(toasts(page).first()).toContainText(/saved/i);
  await expect(page.locator('[data-usenet-status]')).toHaveCount(0);
});

test('creation failure is reported by the canonical toast owner and claims nothing',
  async ({page}) => {
    await page.route(url => /\/api\/usenet\/servers$/.test(url.pathname),
      route => route.fulfill({status: 502, contentType: 'application/json',
        body: JSON.stringify({detail: 'news server rejected'})}));
    const card = await draftCard(page, 'news.failed.net');
    await field(card, 'host').blur();
    await expect(toasts(page).first()).toContainText(/reject|could not|fail/i);
    expect(await storedServers(page)).toHaveLength(0);
  });

test('Test success is reported by the canonical toast owner', async ({page}) => {
  await page.route(url => /\/api\/usenet\/servers\/test$/.test(url.pathname),
    route => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({ok: true, message: 'Connected to news.tested.net'})}));
  const card = await draftCard(page, 'news.tested.net');
  // Test settles first, which materializes the draft -- so the creation
  // reports itself too. Both results belong to the one canonical toast owner.
  await card.locator('[data-usenet-action="test"]').click();
  await expect(toasts(page).filter({hasText: 'Connected to news.tested.net'}))
    .toHaveCount(1);
});

test('Test failure is reported by the canonical toast owner', async ({page}) => {
  await page.route(url => /\/api\/usenet\/servers\/test$/.test(url.pathname),
    route => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({ok: false, message: 'Authentication rejected'})}));
  const card = await draftCard(page, 'news.badauth.net');
  await card.locator('[data-usenet-action="test"]').click();
  await expect(toasts(page).filter({hasText: 'Authentication rejected'})).toHaveCount(1);
});

test('inline field validation survives and is not a toast', async ({page}) => {
  const card = await draftCard(page, '');
  await field(card, 'username').fill('nobody');
  await field(card, 'username').blur();
  const validation = card.locator('[data-usenet-validation]');
  await expect(validation).toBeVisible();
  await expect(validation).toContainText(/host is required/i);
  await expect(toasts(page)).toHaveCount(0);
  expect(await storedServers(page)).toHaveLength(0);

  // Correcting the field clears the validation without any notification.
  await field(card, 'host').fill('news.corrected.net');
  await field(card, 'host').blur();
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

test('a credential response cannot overwrite an ORDINARY edit made while it was in flight',
  async ({page}) => {
    const id = await seed(page, {password: ''});
    await page.reload();
    await openSources(page);
    const card = cardFor(page, id);
    await delayNextWrite(page, id, 1500);
    await field(card, 'password').fill('PW-INFLIGHT');
    await field(card, 'password').blur();
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

// --- a credential write consumes only the draft it dispatched -------------
//
// Ordinary controls are protected by scoped convergence. The credential needs
// the same rule: a completed write may consume only the exact draft it sent,
// and must never erase intent the operator created after dispatch.

test('a credential write leaves a newer password typed while it was in flight alone',
  async ({page}) => {
    const id = await seed(page, {password: ''});
    await page.reload();
    await openSources(page);
    const card = cardFor(page, id);
    const seen = writes(page);

    await delayNextWrite(page, id, 1500);
    await field(card, 'password').fill('PW-A');
    await field(card, 'password').blur();
    // Newer intent, typed while PW-A is still on the wire.
    await field(card, 'password').fill('PW-B');

    await expect.poll(async () => (await record(page, id)).password_configured,
      {timeout: 15000}).toBe(true);
    await page.waitForTimeout(700);

    // PW-A is what was accepted ...
    expect(seen.filter(entry => entry.method === 'PUT')[0].body.password).toBe('PW-A');
    // ... and PW-B is still the operator's, dirty against the blank accepted
    // baseline, so it commits on its own blur.
    await expect(field(card, 'password')).toHaveValue('PW-B');
    await expect.poll(() => committedBaseline(page, id, 'password')).toBe('');

    await field(card, 'password').blur();
    await expect.poll(() => seen.filter(entry => entry.method === 'PUT').length).toBe(2);
    expect(seen.filter(entry => entry.method === 'PUT')[1].body.password).toBe('PW-B');
    await expect(field(card, 'password')).toHaveValue('');
  });

test('a credential write in flight leaves the Clear action reachable and truthful',
  async ({page}) => {
    // Seeded WITH a credential: the Clear action only exists on a card that has
    // something stored to clear.
    const id = await seed(page);
    await page.reload();
    await openSources(page);
    const card = cardFor(page, id);

    await delayNextWrite(page, id, 1500);
    await field(card, 'password').fill('PW-A');
    await field(card, 'password').blur();

    await page.waitForTimeout(2200);
    // The card converged on the accepted record. A password is still stored, so
    // the destructive action it offers is still reachable and still truthful --
    // there is no armed state to preserve, because the question is asked when
    // the operator acts.
    await expect(clearButton(card)).toBeVisible();
    await expect(clearButton(card)).toBeEnabled();

    await confirmDanger(page, clearButton(card));
    await expect.poll(async () => (await record(page, id)).password_configured).toBe(false);
  });

test('record creation consumes only the credential it dispatched', async ({page}) => {
  const seen = writes(page);
  await delayNextCreate(page, 1500);
  await addTile(page).click();
  const card = serverCards(page).last();
  // Entered while the card has no Host, so nothing can be created yet and the
  // creation deterministically carries it.
  await field(card, 'password').fill('PW-A');
  await field(card, 'host').fill('news.created-credential.net');
  await field(card, 'host').blur();
  // Newer intent, typed while the record is still being minted.
  await field(card, 'password').fill('PW-B');

  await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
  await page.waitForTimeout(900);
  const created = await record(page, await mintedId(card));
  expect(created.password_configured).toBe(true);
  expect(seen.filter(entry => entry.method === 'POST')[0].body.password).toBe('PW-A');

  // The freshly minted record keeps PW-B as the operator's pending draft: the
  // completed creation consumed only PW-A, which is what it actually carried.
  await expect(card).toHaveAttribute('data-usenet-server-id', created.id);
  await expect(field(card, 'password')).toHaveValue('PW-B');
  await expect.poll(() => committedBaseline(page, created.id, 'password')).toBe('');

  await field(card, 'password').blur();
  await expect.poll(() => seen.filter(entry => entry.method === 'PUT'
    && entry.body.password === 'PW-B').length, {timeout: 20000}).toBe(1);
  await page.waitForTimeout(700);
  // The operator's LATEST credential is the last one written, and therefore
  // what the record ends up holding: every write to one record shares that
  // record's lane, so the carried draft's replay can never overtake it.
  const credentials = seen.filter(entry => entry.method === 'PUT' && entry.body.password !== undefined);
  expect(credentials.pop().body.password).toBe('PW-B');
  await expect(field(card, 'password')).toHaveValue('');
});

/* A credential boundary crossed BEFORE the record existed is carried by the
 * creation AND remembered by the canonical persistence owner, which replays it
 * once the record has an identity. A secret's accepted presentation is blank,
 * so the replay cannot be recognised as already-applied and re-writes the same
 * value once. It is idempotent, it is ordered on the record's own lane, and it
 * is the ordinary deferred-draft semantics rather than a second writer -- but
 * it is real, so it is stated rather than left to be discovered. */
test('a credential carried by creation is replayed at most once, idempotently',
  async ({page}) => {
    const seen = writes(page);
    await addTile(page).click();
    const card = serverCards(page).last();
    await field(card, 'password').fill('PW-CARRIED');
    await field(card, 'host').fill('news.carried-once.net');
    await field(card, 'host').blur();

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    await page.waitForTimeout(1200);
    const created = await record(page, await mintedId(card));

    expect(seen.filter(entry => entry.method === 'POST')).toHaveLength(1);
    expect(seen.filter(entry => entry.method === 'POST')[0].body.password).toBe('PW-CARRIED');
    const replays = seen.filter(entry => entry.method === 'PUT' && entry.body.password !== undefined);
    expect(replays.length).toBeLessThanOrEqual(1);
    for (const replay of replays) expect(replay.body.password).toBe('PW-CARRIED');
    // One record, the credential stored, and nothing retained by the browser.
    expect(await storedServers(page)).toHaveLength(1);
    expect(created.password_configured).toBe(true);
    await expect(field(card, 'password')).toHaveValue('');
    await expect.poll(() => committedBaseline(page, created.id, 'password')).toBe('');
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
    await delayNextCreate(page, 1500);
    const card = await draftCard(page, 'news.creation-field.net');
    await field(card, 'host').blur();
    // Newer ordinary intent whose commit boundary is genuinely crossed while
    // the record is still being minted.
    await field(card, 'username').fill('after');
    await field(card, 'username').blur();

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    const id = await mintedId(card);
    expect(seen.filter(entry => entry.method === 'POST')).toHaveLength(1);

    // The operator already left the field: no second blur may be required.
    await expect.poll(async () => (await record(page, id)).username, {timeout: 20000}).toBe('after');
    await expect(field(card, 'username')).toHaveValue('after');
  });

test('an SSL toggle made while the record is being minted reaches the canonical record',
  async ({page}) => {
    await delayNextCreate(page, 1500);
    const card = await draftCard(page, 'news.creation-ssl.net');
    await field(card, 'host').blur();
    // SSL is immediate: performing it IS the act, so it cannot wait for a
    // later blur the operator has no reason to make.
    await setSsl(card, false);

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    const id = await mintedId(card);
    await expect.poll(async () => (await record(page, id)).ssl, {timeout: 20000}).toBe(false);
    // The conventional port travelled with it: one operator action, not two.
    expect((await record(page, id)).port).toBe(119);
    await expect(field(card, 'ssl')).not.toBeChecked();
    await expect(field(card, 'port')).toHaveValue('119');
  });

test('a display name chosen while the record is being minted survives and is canonical',
  async ({page}) => {
    await delayNextCreate(page, 2500);
    const card = await draftCard(page, 'news.creation-name.net');
    await field(card, 'host').blur();
    await rename(page, card, 'Chosen Later');

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    const id = await mintedId(card);
    // The creation response carried the derived name; the newer choice wins.
    await expect.poll(async () => (await record(page, id)).display_name, {timeout: 20000})
      .toBe('Chosen Later');
    await expect(card.locator('[data-usenet-display-name]')).toHaveText('Chosen Later');
  });

test('removing a card while its record is being minted never leaves an orphan record',
  async ({page}) => {
    await delayNextCreate(page, 1500);
    const card = await draftCard(page, 'news.creation-orphan.net');
    await field(card, 'host').blur();
    // The confirmation gates the existing removal owner; it does not replace
    // any part of its creation serialization.
    await confirmDanger(page, removeButton(card));

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
    await delayNextCreate(page, 1500);
    const card = await draftCard(page, 'news.creation-draft.net');
    await field(card, 'host').blur();
    // Crossed its boundary while the record was being minted.
    await field(card, 'username').fill('second');
    await field(card, 'username').blur();
    // Typed after that, and deliberately never left.
    await field(card, 'username').fill('third');

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    const id = await mintedId(card);
    expect(seen.filter(entry => entry.method === 'POST')).toHaveLength(1);

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
    await delayNextCreate(page, 2000);
    const card = await draftCard(page, 'news.creation-latest.net');
    await field(card, 'host').blur();
    await field(card, 'username').fill('second');
    await field(card, 'username').blur();
    await field(card, 'username').fill('third');
    await field(card, 'username').blur();
    // Never left, so never committed.
    await field(card, 'username').fill('fourth');

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    const id = await mintedId(card);
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
    await delayNextCreate(page, 2000);
    const card = await draftCard(page, 'news.creation-ssl-draft.net');
    await field(card, 'host').blur();
    // The act: SSL off, carrying the conventional port it follows.
    await setSsl(card, false);
    // Typed afterwards and deliberately never left: a changed-blur draft that
    // has crossed no boundary of its own.
    await field(card, 'port').fill('9119');

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    const id = await mintedId(card);
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
    await delayNextCreate(page, 2500);
    const card = await draftCard(page, 'news.creation-ssl-order.net');
    await field(card, 'host').blur();
    await setSsl(card, false);
    await field(card, 'port').fill('9119');
    await field(card, 'port').blur();

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    const id = await mintedId(card);
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
    await delayNextCreate(page, 3000);
    const card = await draftCard(page, 'news.creation-ssl-supersede.net');
    await field(card, 'host').blur();
    // A boundary crossed first ...
    await field(card, 'port').fill('119');
    await field(card, 'port').blur();
    // ... then two acts, the last of which moves the port back. The port it
    // moves is part of THAT act, so it supersedes the earlier boundary.
    await setSsl(card, false);
    await setSsl(card, true);

    await expect.poll(async () => (await storedServers(page)).length, {timeout: 20000}).toBe(1);
    const id = await mintedId(card);
    await expect.poll(async () => (await record(page, id)).ssl, {timeout: 20000}).toBe(true);
    await page.waitForTimeout(900);
    expect((await record(page, id)).port).toBe(563);
    await expect(field(card, 'port')).toHaveValue('563');
    await expect.poll(() => committedBaseline(page, id, 'port')).toBe('563');
  });
