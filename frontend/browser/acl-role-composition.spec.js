const { test, expect } = require('@playwright/test');

/* 1.0.13 ACL Stage 3: the interface is composed from the signed-in account's
 * server-issued authority, in real separate sessions on the password-auth
 * runtime. Every account here is a synthetic fixture this file creates under
 * a run-unique name and deletes again; no production credential is involved. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const ADMIN = {username: 'browser', password: 'browser-secret'};
const run = Date.now().toString(36);

async function signIn(browser, {username, password}) {
  const context = await browser.newContext({baseURL: AUTH, viewport: {width: 1440, height: 914}});
  const page = await context.newPage();
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  const errors = [];
  page.on('pageerror', error => errors.push(String(error.message)));
  await page.goto('/login');
  await page.locator('#username').fill(username);
  await page.locator('#password').fill(password);
  await page.locator('button[type="submit"]').click();
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.locator('#sidebar-account-row')).toBeVisible();
  return {context, page, errors};
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
    events: document.querySelectorAll('#view-events').length,
    bulkBar: document.querySelectorAll('#bulk-bar').length,
    dashboardColumns: document.querySelectorAll('#view-dashboard thead th').length,
    // Occupancy (active / max) is administrative runtime state; the live speed is everyone's.
    badgeOccupancy: document.querySelectorAll('#runtime-badge-active, #runtime-badge-max').length,
    badgeSpeed: document.querySelectorAll('#runtime-badge-speed').length,
    unmounted: document.querySelectorAll('template[data-requires]').length,
  }));
}

test.describe.configure({mode: 'serial'});

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
      dashboardColumns: 6, unmounted: 0, badgeOccupancy: 2, badgeSpeed: 1});

    // An Integration token is API-only: the interface itself is refused.
    const integration = await browser.newContext({baseURL: AUTH,
      extraHTTPHeaders: {Authorization: `Bearer ${issued.body.token}`}});
    expect((await integration.request.get('/')).status()).toBe(403);
    expect((await integration.request.get('/app.js')).status()).toBe(403);
    await integration.close();

    const signedUser = await signIn(browser, user);
    expect(await composition(signedUser.page)).toMatchObject({
      role: 'user', nav: ['dashboard', 'torrents', 'stats', 'help'],
      quickAdd: 1, topbarActions: 0, speedCap: 0, sidebarFooter: 0, settings: 0, events: 0, bulkBar: 1,
      dashboardColumns: 6, unmounted: 0, badgeOccupancy: 0, badgeSpeed: 1});
    // Only the live speed: no stand-in occupancy such as "0 / –".
    expect((await signedUser.page.locator('#runtime-speed-badge').innerText()).replace(/\s+/g, ' ').trim())
      .toMatch(/^\u25BC ?[\d.,]+ ?[KMGT]?B\/s$/);
    expect((await api(signedUser.page, 'GET', '/settings')).status).toBe(403);

    const signedViewer = await signIn(browser, viewer);
    expect(await composition(signedViewer.page)).toMatchObject({
      role: 'readonly', nav: ['dashboard', 'torrents', 'stats', 'help'],
      quickAdd: 0, topbarActions: 0, speedCap: 0, sidebarFooter: 0, settings: 0, events: 0, bulkBar: 0,
      dashboardColumns: 5, unmounted: 0, badgeOccupancy: 0, badgeSpeed: 1});
    await signedViewer.page.locator('#sidebar .nav-item[data-view="torrents"]').click();
    await expect(signedViewer.page.locator('#view-torrents thead th')).toHaveCount(6);
    await expect(signedViewer.page.locator('#view-torrents input[type="checkbox"]')).toHaveCount(0);

    // Demotion and promotion reach the open page at once, and it stays signed in.
    let reloaded = signedUser.page.waitForEvent('load');
    expect((await api(admin.page, 'PATCH', `/accounts/${user.id}`, {role: 'readonly'})).status).toBe(200);
    await reloaded;
    await expect(signedUser.page.locator('#sidebar-account-row')).toContainText('ReadOnly User');
    expect(await composition(signedUser.page)).toMatchObject({role: 'readonly', quickAdd: 0, bulkBar: 0});
    reloaded = signedUser.page.waitForEvent('load');
    expect((await api(admin.page, 'PATCH', `/accounts/${user.id}`, {role: 'user'})).status).toBe(200);
    await reloaded;
    expect(await composition(signedUser.page)).toMatchObject({role: 'user', quickAdd: 1, bulkBar: 1});
    expect(new URL(signedUser.page.url()).pathname).toBe('/');

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

    await admin.page.locator(`tr[data-account-row="${viewer.id}"] [data-user-action="delete"]`).click();
    const removal = admin.page.locator('.dp-modal-dialog.dp-account-impact');
    await expect(removal.locator('.dp-modal-footer [data-modal-accept]')).toHaveClass(/\bbtn-danger\b/);
    await expect(removal.locator('.dp-account-impact-note'))
      .toHaveText('The account’s history and the provenance of its past requests are retained.');
    await removal.locator('[data-modal-cancel]').click();

    expect([...admin.errors, ...signedUser.errors, ...signedViewer.errors]).toEqual([]);
  } finally {
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
    await admin.page.locator(`tr[data-account-row="${created.body.id}"] [data-user-action="activity"]`).click();
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
  } finally {
    await api(admin.page, 'DELETE', `/accounts/${created.body.id}`);
  }
});
