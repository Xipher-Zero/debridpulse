const { test, expect } = require('@playwright/test');

/* 1.0.14: under sign-in, each human account owns its appearance.
 *
 * Real sessions on the password-auth runtime; every account is a synthetic
 * fixture this file creates under a run-unique name. The mode and both
 * palettes are the account's own record on the server: the same account in a
 * fresh browser gets them at first paint, another account in the same browser
 * never does, and this browser's own (sign-in-off) theme keys are neither read
 * nor written. The open runtime's browser-local path is covered by
 * theme-palette-picker.spec.js. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const ADMIN = {username: 'browser', password: 'browser-secret'};
const run = Date.now().toString(36);
const KEYS = ['theme', 'dp-theme-palette-dark', 'dp-theme-palette-light'];
// What a sign-in-off session left in this browser profile.
const BROWSER_CHOICES = [['theme', 'light'], ['dp-theme-palette-dark', 'gruvbox-dark'], ['dp-theme-palette-light', 'catppuccin-latte']];

const adminSession = require('./acl-admin-session');

// Records, before any page script, every read and write of the three keys and
// the body's mode and palette the moment the first-paint owner finishes.
function recorder(keys) {
  window.__theme = {reads: [], writes: [], first: null};
  const get = Storage.prototype.getItem;
  const set = Storage.prototype.setItem;
  Storage.prototype.getItem = function (key) { if (keys.includes(key)) window.__theme.reads.push(key); return get.call(this, key); };
  Storage.prototype.setItem = function (key, value) { if (keys.includes(key)) window.__theme.writes.push(key); return set.call(this, key, value); };
  Object.defineProperty(window, 'DPTheme', {configurable: true, set(value) {
    Object.defineProperty(window, 'DPTheme', {value, writable: true, configurable: true});
    queueMicrotask(() => {
      window.__theme.first = {light: document.body.classList.contains('light'), palette: document.body.getAttribute('data-dp-palette') || ''};
    });
  }});
}

async function signIn(browser, {username, password}) {
  const shared = username === ADMIN.username;
  const reuse = shared && adminSession.get() !== null;
  const storageState = reuse ? adminSession.get() : {cookies: [], origins: [{origin: new URL(AUTH).origin,
    localStorage: BROWSER_CHOICES.map(([name, value]) => ({name, value}))}]};
  const context = await browser.newContext({baseURL: AUTH, viewport: {width: 1440, height: 914}, storageState});
  await context.addInitScript(recorder, KEYS);
  const page = await context.newPage();
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  if (reuse) {
    await page.goto('/');
  } else {
    await page.goto('/login');
    await page.locator('#username').fill(username);
    await page.locator('#password').fill(password);
    await page.locator('button[type="submit"]').click();
  }
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.locator('#sidebar-auth-row .dp-sidebar-profile')).toBeVisible();
  if (shared && !reuse) adminSession.remember(await context.storageState());
  return {context, page};
}

const api = (page, method, path, body) => page.evaluate(async ([method, path, body]) => {
  const response = await window.debridPulseAuth.fetch(`/api${path}`, {
    method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  return {status: response.status, body: await response.json().catch(() => null)};
}, [method, path, body]);

async function person(admin, name, role) {
  const account = {username: `ta-${name}-${run}`, password: `ta-${name}-${run}-pass`};
  const created = await api(admin.page, 'POST', '/accounts', {kind: 'local', role, display_name: `${name} ${run}`, ...account});
  expect(created.status).toBe(200);
  return {...account, id: created.body.id};
}

const shown = page => page.evaluate(() => ({
  mode: window.DPTheme.mode(), dark: window.DPTheme.palette('dark'), light: window.DPTheme.palette('light'),
  palette: document.body.getAttribute('data-dp-palette') || '', first: window.__theme.first,
  reads: window.__theme.reads.slice(), writes: window.__theme.writes.slice(),
}));

const stored = page => page.evaluate(names => names.map(name => Object.prototype.hasOwnProperty.call(localStorage, name) ? localStorage[name] : null), KEYS);

// The page's save queue has delivered: the account's record says so.
const saved = (page, expected) => expect.poll(async () => {
  const record = (await api(page, 'GET', '/account/appearance')).body;
  return record && {mode: record.mode, dark_palette: record.dark_palette, light_palette: record.light_palette};
}).toEqual(expected);

test('each signed-in account keeps its own mode and palettes across browsers, never another account\'s or the browser\'s', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const admin = await signIn(browser, ADMIN);
  const alice = await person(admin, 'Alma', 'user');
  const bob = await person(admin, 'Boris', 'readonly');
  await admin.context.close();

  // A new account: native Dark, native Light kept for Light mode; the
  // browser's own theme keys are not read, so they cannot leak in.
  const a = await signIn(browser, alice);
  let state = await shown(a.page);
  expect(state).toMatchObject({mode: 'dark', dark: 'debridpulse-dark', light: 'debridpulse-light', palette: '',
    first: {light: false, palette: ''}, reads: [], writes: []});

  // Three independent choices, each applied at once and saved one by one.
  await a.page.evaluate(() => window.toggleTheme());
  await a.page.evaluate(() => window.DPTheme.choose('nord-light'));
  await a.page.evaluate(() => window.toggleTheme());
  await a.page.evaluate(() => window.DPTheme.choose('dracula'));
  await a.page.evaluate(() => window.toggleTheme());
  await expect(a.page.locator('body')).toHaveAttribute('data-dp-palette', 'nord-light');
  await saved(a.page, {mode: 'light', dark_palette: 'dracula', light_palette: 'nord-light'});
  expect(await stored(a.page)).toEqual(BROWSER_CHOICES.map(([, value]) => value));
  expect((await shown(a.page)).writes).toEqual([]);

  // The same account in a fresh browser: all three, at first paint.
  const elsewhere = await signIn(browser, alice);
  state = await shown(elsewhere.page);
  expect(state).toMatchObject({mode: 'light', dark: 'dracula', light: 'nord-light', palette: 'nord-light',
    first: {light: true, palette: 'nord-light'}, reads: []});
  await elsewhere.context.close();

  // Another account (ReadOnly) in a browser holding the same keys: its own
  // native appearance, and its own choice saved to its own record only.
  const b = await signIn(browser, bob);
  state = await shown(b.page);
  expect(state).toMatchObject({mode: 'dark', dark: 'debridpulse-dark', light: 'debridpulse-light', first: {light: false}, reads: []});
  await b.page.evaluate(() => window.DPTheme.choose('kanagawa-wave'));
  await saved(b.page, {mode: 'dark', dark_palette: 'kanagawa-wave', light_palette: 'debridpulse-light'});
  await saved(a.page, {mode: 'light', dark_palette: 'dracula', light_palette: 'nord-light'});

  // The self-scoped API refuses another account's selector and unknown values.
  expect((await api(b.page, 'PATCH', '/account/appearance', {account_id: alice.id, mode: 'light'})).status).toBe(422);
  expect((await api(b.page, 'PATCH', '/account/appearance', {dark_palette: 'nord-light'})).status).toBe(422);
  // A retired palette is no longer a choice, and the picker never offers one.
  expect((await api(b.page, 'PATCH', '/account/appearance', {dark_palette: 'tokyo-night'})).status).toBe(422);
  expect((await api(b.page, 'PATCH', '/account/appearance', {light_palette: 'solarized-light'})).status).toBe(422);
  expect(await b.page.evaluate(() => window.DPTheme.choose('tokyo-night'))).toBe(false);
  await saved(a.page, {mode: 'light', dark_palette: 'dracula', light_palette: 'nord-light'});

  // A reload of Alice's page: hers again, at first paint.
  await a.page.reload();
  await expect(a.page.locator('#sidebar-auth-row .dp-sidebar-profile')).toBeVisible();
  expect(await shown(a.page)).toMatchObject({first: {light: true, palette: 'nord-light'}, dark: 'dracula', reads: []});
  await a.context.close();
  await b.context.close();
});
