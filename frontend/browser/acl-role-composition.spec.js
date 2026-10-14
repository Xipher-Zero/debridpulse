const { test, expect } = require('@playwright/test');

/* 1.0.13 ACL Stage 3: the interface is composed from the signed-in account's
 * server-issued authority, in real separate sessions on the password-auth
 * runtime. Every account here is a synthetic fixture this file creates under
 * a run-unique name and deletes again; no production credential is involved. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const ADMIN = {username: 'browser', password: 'browser-secret'};
const run = Date.now().toString(36);

// The Admin signs in once per worker (acl-admin-session.js: the login
// challenge limit is shared by every auth-runtime file).
const adminSession = require('./acl-admin-session');
async function signIn(browser, {username, password}, {fresh = false, holdPulse = false} = {}) {
  const shared = username === ADMIN.username && !fresh;
  const reuse = shared && adminSession.get() !== null;
  const context = await browser.newContext({baseURL: AUTH, viewport: {width: 1440, height: 914},
    ...(reuse ? {storageState: adminSession.get()} : {})});
  if (!shared) signedIn.push(context);
  // ``holdPulse``: the page's live pulse stays unsubscribed until released.
  const heldPulse = [];
  if (holdPulse) await context.route('**/api/events/stream', route => { heldPulse.push(route); });
  const releasePulse = async () => {
    await Promise.all(heldPulse.splice(0).map(route => route.continue()));
    await context.unroute('**/api/events/stream');
  };
  // The application's own live pulse: a role change reaches an open page as
  // its account_changed event, so a test that awaits that reset first waits
  // for the page's pulse to be connected (pulseConnected below).
  await context.addInitScript(() => document.addEventListener('debridpulse:pulse-connected', () => {
    window.__dpPulseConnected = true;
  }));
  const page = await context.newPage();
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  const errors = [];
  page.on('pageerror', error => errors.push(String(error.message)));
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
  return {context, page, errors, releasePulse};
}

// The page's live pulse is connected (its 'connected' event was delivered), so
// an authority change made from now on reaches it.
const pulseConnected = page => expect.poll(() => page.evaluate(() => window.__dpPulseConnected === true)).toBe(true);

// Every account page a test signs in (never the shared Admin): closed before
// those accounts are removed, so no stale page is sent to the login page --
// each one would be one more sign-in challenge against the shared limit.
const signedIn = [];
async function closeSignedIn() {
  await Promise.all(signedIn.splice(0).map(context => context.close()));
}

const api = (page, method, path, body) => page.evaluate(async ([method, path, body]) => {
  const response = await window.debridPulseAuth.fetch(`/api${path}`, {
    method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  return {status: response.status, body: await response.json().catch(() => null)};
}, [method, path, body]);

async function composition(page) {
  return page.evaluate(() => ({
    role: window.debridPulseAuth.authority.role,
    nav: [...document.querySelectorAll('#sidebar .nav-item[data-view]')].map(item => item.dataset.view),
    quickAdd: document.querySelectorAll('.dp-dashboard-quick-add').length,
    topbarActions: document.querySelectorAll('#topbar-actions').length,
    speedCap: document.querySelectorAll('.runtime-cap-control').length,
    sidebarFooter: document.querySelectorAll('.sidebar-footer').length,
    settings: document.querySelectorAll('#view-settings').length,
    // Whose configuration the Settings slot edits: the installation's
    // (global, Admin) or the signed-in User's own Notifications (self).
    settingsContext: (view => (view ? view.dataset.settingsContext || 'global' : null))(
      document.querySelector('#view-settings')),
    settingsLabel: document.querySelector('#sidebar .nav-item[data-view="settings"] .nav-label')?.textContent || null,
    events: document.querySelectorAll('#view-events').length,
    bulkBar: document.querySelectorAll('#bulk-bar').length,
    dashboardColumns: document.querySelectorAll('#view-dashboard thead th').length,
    // Occupancy (active / max) is administrative runtime state; the live speed is everyone's.
    badgeOccupancy: document.querySelectorAll('#runtime-badge-active, #runtime-badge-max').length,
    badgeSpeed: document.querySelectorAll('#runtime-badge-speed').length,
    unmounted: document.querySelectorAll('template[data-requires]').length,
  }));
}

test('each role is composed from its server authority, and a role change resets without signing out', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const admin = await signIn(browser, ADMIN);
  const created = [];
  const user = {username: `acl-user-${run}`, password: `acl-user-${run}-pass`};
  const viewer = {username: `acl-viewer-${run}`, password: `acl-viewer-${run}-pass`};
  try {
    for (const [account, role] of [[user, 'user'], [viewer, 'readonly']]) {
      const result = await api(admin.page, 'POST', '/accounts',
        {kind: 'local', role, display_name: account.username, ...account});
      expect(result.status).toBe(200);
      created.push(result.body.id);
      account.id = result.body.id;
    }
    const bot = await api(admin.page, 'POST', '/accounts', {kind: 'integration', display_name: `acl-bot-${run}`});
    created.push(bot.body.id);
    const issued = await api(admin.page, 'POST', `/accounts/${bot.body.id}/tokens`, {label: 'acl-spec'});
    expect(issued.body.token.startsWith('dp_')).toBe(true);

    expect(await composition(admin.page)).toMatchObject({
      role: 'admin', nav: ['dashboard', 'torrents', 'events', 'stats', 'settings', 'help'],
      quickAdd: 1, topbarActions: 1, speedCap: 1, sidebarFooter: 1, settings: 1, events: 1, bulkBar: 1,
      settingsContext: 'global', settingsLabel: 'Settings',
      dashboardColumns: 6, unmounted: 0, badgeOccupancy: 2, badgeSpeed: 1});

    // An Integration token is API-only: the interface itself is refused.
    const integration = await browser.newContext({baseURL: AUTH,
      extraHTTPHeaders: {Authorization: `Bearer ${issued.body.token}`}});
    expect((await integration.request.get('/')).status()).toBe(403);
    expect((await integration.request.get('/app.js')).status()).toBe(403);
    await integration.close();

    const signedUser = await signIn(browser, user);
    expect(await composition(signedUser.page)).toMatchObject({
      // A User's Settings slot is their OWN Notifications page, never Settings.
      role: 'user', nav: ['dashboard', 'torrents', 'stats', 'settings', 'help'],
      quickAdd: 1, topbarActions: 0, speedCap: 0, sidebarFooter: 0, settings: 1, events: 0, bulkBar: 1,
      settingsContext: 'self', settingsLabel: 'Notifications',
      dashboardColumns: 6, unmounted: 0, badgeOccupancy: 0, badgeSpeed: 1});
    // Only the live speed: no stand-in occupancy such as "0 / –".
    expect((await signedUser.page.locator('#runtime-speed-badge').innerText()).replace(/\s+/g, ' ').trim())
      .toMatch(/^\u25BC ?[\d.,]+ ?[KMGT]?B\/s$/);
    expect((await api(signedUser.page, 'GET', '/settings')).status).toBe(403);

    const signedViewer = await signIn(browser, viewer, {holdPulse: true});
    expect(await composition(signedViewer.page)).toMatchObject({
      role: 'readonly', nav: ['dashboard', 'torrents', 'stats', 'help'],
      quickAdd: 0, topbarActions: 0, speedCap: 0, sidebarFooter: 0, settings: 0, events: 0, bulkBar: 0,
      settingsContext: null, settingsLabel: null,
      dashboardColumns: 5, unmounted: 0, badgeOccupancy: 0, badgeSpeed: 1});
    await signedViewer.page.locator('#sidebar .nav-item[data-view="torrents"]').click();
    await expect(signedViewer.page.locator('#view-torrents thead th')).toHaveCount(6);
    await expect(signedViewer.page.locator('#view-torrents input[type="checkbox"]')).toHaveCount(0);

    // A role change made before a page's live pulse subscribed is announced to
    // no stream; the pulse's connection re-reads the session and resets the page.
    const viewerReset = signedViewer.page.waitForEvent('load');
    expect((await api(admin.page, 'PATCH', `/accounts/${viewer.id}`, {role: 'user'})).status).toBe(200);
    await signedViewer.releasePulse();
    await viewerReset;
    expect(await composition(signedViewer.page)).toMatchObject({role: 'user', quickAdd: 1, bulkBar: 1});

    // Demotion and promotion reach the open page at once, and it stays signed in.
    await pulseConnected(signedUser.page);
    let reloaded = signedUser.page.waitForEvent('load');
    expect((await api(admin.page, 'PATCH', `/accounts/${user.id}`, {role: 'readonly'})).status).toBe(200);
    await reloaded;
    await expect(signedUser.page.locator('#sidebar-auth-row .dp-sidebar-profile')).toContainText('ReadOnly User');
    expect(await composition(signedUser.page)).toMatchObject(
      {role: 'readonly', quickAdd: 0, bulkBar: 0, settings: 0, settingsContext: null});
    await pulseConnected(signedUser.page);           // the reloaded page's own pulse
    reloaded = signedUser.page.waitForEvent('load');
    expect((await api(admin.page, 'PATCH', `/accounts/${user.id}`, {role: 'user'})).status).toBe(200);
    await reloaded;
    expect(await composition(signedUser.page)).toMatchObject(
      {role: 'user', quickAdd: 1, bulkBar: 1, settings: 1, settingsContext: 'self'});
    expect(new URL(signedUser.page.url()).pathname).toBe('/');
    await pulseConnected(signedUser.page);

    // Disabling goes through the impact confirmation, with the server's
    // figures, and sends the account's open page to sign-in.
    await admin.page.locator('#sidebar .nav-item[data-view="settings"]').click();
    await admin.page.locator('#view-settings [data-tab="users"]').click();
    await admin.page.locator(`tr[data-account-row="${user.id}"] [data-user-action="disable"]`).click();
    const impact = admin.page.locator('.dp-modal-dialog.dp-account-impact');
    await expect(impact.locator('.dp-account-impact-lead'))
      .toHaveText('Disabling this account will revoke its access and withdraw its outstanding download requests.');
    await expect(impact.locator('.dp-account-impact-lead')).toHaveCSS('text-align', 'center');
    await expect(impact.locator('.dp-account-impact-figures dd')).toHaveText(['0', '0', '0']);
    await expect(impact.locator('.dp-account-impact-qualifier'))
      .toHaveText('These figures reflect the current account and transfer state. They may change before confirmation.');
    await expect(impact.locator('.dp-modal-footer .btn')).toHaveText(['Cancel', 'Disable account']);
    await expect(impact).not.toContainText('Illustrative');
    const signedOut = signedUser.page.waitForURL(/\/login/);
    await impact.locator('[data-modal-accept]').click();
    await signedOut;

    await signedViewer.context.close();          // its removal is not what this page shows
    await admin.page.locator(`tr[data-account-row="${viewer.id}"] [data-user-action="remove"]`).click();
    const removal = admin.page.locator('.dp-modal-dialog.dp-account-impact');
    await expect(removal.locator('.dp-modal-footer [data-modal-accept]')).toHaveClass(/\bbtn-danger\b/);
    await expect(removal.locator('.dp-account-impact-note'))
      .toHaveText('Transfers other accounts still need continue, and completed downloads are not undone. '
        + 'The account’s history and the provenance of its past requests are retained.');
    await removal.locator('[data-modal-cancel]').click();

    expect([...admin.errors, ...signedUser.errors, ...signedViewer.errors]).toEqual([]);
  } finally {
    await closeSignedIn();
    for (const id of created) await api(admin.page, 'DELETE', `/accounts/${id}`);
  }
});

test('a withdrawal is reported as the server answered it, and an Admin withdraws one account\'s request', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const admin = await signIn(browser, ADMIN);
  const user = {username: `acl-withdraw-${run}`, password: `acl-withdraw-${run}-pass`};
  const created = await api(admin.page, 'POST', '/accounts', {kind: 'local', role: 'user', display_name: user.username, ...user});
  expect(created.status).toBe(200);
  try {
    const signed = await signIn(browser, user);
    const hash = (run + '0'.repeat(40)).replace(/[^0-9a-f]/g, 'a').slice(0, 40);
    const submitted = await api(signed.page, 'POST', '/torrents/add-magnet',
      {magnet: `magnet:?xt=urn:btih:${hash}&dn=acl-withdraw-${run}`});
    expect(submitted.status).toBe(200);
    const transferId = submitted.body.id;
    const toast = signed.page.locator('.toast').last();
    const withdraw = async () => {
      await signed.page.evaluate(id => window.showDetail(id), transferId);
      await signed.page.locator('.dp-detail-withdraw').click();
      await signed.page.locator('.dp-modal-overlay [data-modal-accept]').click();
    };

    // Requested By names who asked, from the demand ledger.
    await signed.page.evaluate(id => window.showDetail(id), transferId);
    const requested = signed.page.locator('.dp-detail-requested-by');
    await expect(requested.locator('.card-title')).toHaveText('Requested By');
    await expect(requested.locator('.dp-detail-requesters > .dp-detail-requester .dp-detail-requester-name'))
      .toHaveText([user.username]);
    await expect(requested.locator('.dp-detail-requesters-history')).toHaveCount(0);
    await signed.page.evaluate(() => window.closeModal());

    // Refused: the server's own reason, never "no longer outstanding".
    await signed.page.route('**/api/requests/*/withdraw', route => route.fulfill({
      status: 500, contentType: 'application/json', body: JSON.stringify({detail: 'fixture refusal'})}));
    await withdraw();
    await expect(toast).toHaveText('Request not withdrawn: fixture refusal');
    // Details is re-read: the request is still outstanding, so the control remains.
    await expect(signed.page.locator('.dp-detail-withdraw')).toBeVisible();
    await signed.page.evaluate(() => window.closeModal());

    // No answer at all: the outcome is unknown, and it says so.
    await signed.page.unroute('**/api/requests/*/withdraw');
    await signed.page.route('**/api/requests/*/withdraw', route => route.abort('failed'));
    await withdraw();
    await expect(toast).toHaveText('Withdrawal not confirmed: no response from the server. Check this transfer again.');
    await signed.page.evaluate(() => window.closeModal());
    await signed.page.unroute('**/api/requests/*/withdraw');

    // The Admin withdraws exactly this account's request from its Activity.
    await admin.page.locator('#sidebar .nav-item[data-view="settings"]').click();
    await admin.page.locator('#view-settings [data-tab="users"]').click();
    await admin.page.locator(`tr[data-account-row="${created.body.id}"] [data-user-action="details"]`).click();
    const activity = admin.page.locator('.dp-users-activity-dialog');
    await expect(activity.locator('tbody tr')).toHaveCount(1);
    await activity.locator('[data-withdraw-demand]').click();
    await admin.page.locator('.dp-modal-dialog[role="alertdialog"] [data-modal-accept]').click();
    await expect(admin.page.locator('.toast').last()).toHaveText('Request withdrawn.');
    await expect(activity.locator('tbody tr')).toContainText('Withdrawn');
    await expect(activity.locator('[data-withdraw-demand]')).toHaveCount(0);

    // The account's own Details now reflects it: nothing left to withdraw.
    await signed.page.evaluate(id => window.showDetail(id), transferId);
    await expect(signed.page.locator('#modal-body .detail-grid')).toBeVisible();
    await expect(signed.page.locator('.dp-detail-withdraw')).toHaveCount(0);
    // ...and Requested By keeps the request, as withdrawn history.
    await expect(requested.locator('.dp-detail-requesters-empty')).toHaveText('No outstanding requests.');
    await expect(requested.locator('.dp-detail-requesters-history > summary')).toHaveText('Withdrawn requests (1)');
    await expect(requested.locator('.dp-detail-requester.is-withdrawn .dp-detail-requester-name'))
      .toHaveText([user.username]);
  } finally {
    await closeSignedIn();
    await api(admin.page, 'DELETE', `/accounts/${created.body.id}`);
  }
});

test('Change Role offers every human role inside its dialog, and each choice is applied', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const admin = await signIn(browser, ADMIN);
  const person = {username: `acl-role-${run}`, password: `acl-role-${run}-pass`};
  const created = await api(admin.page, 'POST', '/accounts', {kind: 'local', role: 'user', display_name: person.username, ...person});
  expect(created.status).toBe(200);
  try {
    await admin.page.locator('#sidebar .nav-item[data-view="settings"]').click();
    await admin.page.locator('#view-settings [data-tab="users"]').click();
    const roleOf = async () => (await api(admin.page, 'GET', '/accounts')).body.accounts
      .find(account => account.id === created.body.id).role;
    for (const [label, role] of [['ReadOnly User', 'readonly'], ['Admin', 'admin'], ['User', 'user']]) {
      await admin.page.locator(`tr[data-account-row="${created.body.id}"] [data-user-action="role"]`).click();
      await admin.page.locator('.dp-users-dialog .dp-dropdown__trigger').click();
      // The projected menu sits above the dialog: every human role, and only those, can be chosen.
      const options = admin.page.locator('.dp-dropdown-menu .dp-dropdown__option');
      await expect(options).toHaveText(['Admin', 'User', 'ReadOnly User']);
      await options.filter({hasText: new RegExp(`^${label}$`)}).click();
      await expect(admin.page.locator('.dp-users-dialog .dp-dropdown__value')).toHaveText(label);
      await admin.page.locator('.dp-users-dialog [data-modal-accept]').click();
      await expect.poll(roleOf).toBe(role);
      await expect(admin.page.locator(`tr[data-account-row="${created.body.id}"]`)).toContainText(label);
    }
  } finally {
    await closeSignedIn();
    await api(admin.page, 'DELETE', `/accounts/${created.body.id}`);
  }
});

test('My Account shows the account profile and saves both names', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const admin = await signIn(browser, ADMIN);
  const person = {username: `acl-profile-${run}`, password: `acl-profile-${run}-pass`};
  const created = await api(admin.page, 'POST', '/accounts', {kind: 'local', role: 'user', display_name: person.username, ...person});
  expect(created.status).toBe(200);
  try {
    const signed = await signIn(browser, person);
    const open = async () => {
      await signed.page.locator('#sidebar-auth-row .dp-sidebar-profile').click();
      await expect(signed.page.locator('.dp-account-dialog [data-field="display_name"]')).toHaveValue(/.+/);
      return signed.page.locator('.dp-account-dialog');
    };
    let dialog = await open();
    await expect(dialog.locator('[data-field="display_name"]')).toHaveValue(person.username);
    await expect(dialog.locator('[data-field="actual_name"]')).toHaveValue('');
    await expect(dialog.locator('[data-field="email"]')).toHaveValue('');          // never fabricated
    await dialog.locator('[data-field="display_name"]').fill('Profile Person');
    await dialog.locator('[data-field="actual_name"]').fill('Pat Profile');
    const reloaded = signed.page.waitForEvent('load');
    await dialog.locator('[data-modal-accept]').click();
    await reloaded;
    expect((await api(signed.page, 'GET', '/account')).body).toMatchObject(
      {display_name: 'Profile Person', actual_name: 'Pat Profile', email: '', email_verification: null});
    dialog = await open();
    await expect(dialog.locator('[data-field="display_name"]')).toHaveValue('Profile Person');
    await expect(dialog.locator('[data-field="actual_name"]')).toHaveValue('Pat Profile');
  } finally {
    await closeSignedIn();
    await api(admin.page, 'DELETE', `/accounts/${created.body.id}`);
  }
});
