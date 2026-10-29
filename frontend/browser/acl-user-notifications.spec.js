const { test, expect } = require('@playwright/test');

/* 1.0.13: a standard User's own Notifications in the Settings slot.
 *
 * Real sessions on the password-auth runtime; every account is a synthetic
 * fixture this file creates under a run-unique name. The webhook values are
 * syntactically valid Discord endpoints with made-up identifiers; no Test is
 * pressed, so nothing here ever reaches Discord. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const EVIDENCE = process.env.DP_EVIDENCE_DIR || '';
const ADMIN = {username: 'browser', password: 'browser-secret'};
const run = Date.now().toString(36);
const HOOK = `https://discord.com/api/webhooks/9000${Date.now() % 100000}/fixture-${run}`;

// The Admin signs in once per worker (acl-admin-session.js: the login
// challenge limit is shared by every auth-runtime file).
const adminSession = require('./acl-admin-session');
async function signIn(browser, {username, password}, viewport = {width: 1440, height: 914}) {
  const shared = username === ADMIN.username;
  const reuse = shared && adminSession.get() !== null;
  const context = await browser.newContext({baseURL: AUTH, viewport, ...(reuse ? {storageState: adminSession.get()} : {})});
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
  return {context, page, errors};
}

// The page's live pulse is connected (its 'connected' event was delivered), so
// an authority change made from now on reaches it.
const pulseConnected = page => expect.poll(() => page.evaluate(() => window.__dpPulseConnected === true)).toBe(true);

const api = (page, method, path, body) => page.evaluate(async ([method, path, body]) => {
  const response = await window.debridPulseAuth.fetch(`/api${path}`, {
    method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  return {status: response.status, body: await response.json().catch(() => null)};
}, [method, path, body]);

async function person(admin, name, role) {
  const account = {username: `un-${name}-${run}`, password: `un-${name}-${run}-pass`};
  const created = await api(admin.page, 'POST', '/accounts', {kind: 'local', role, display_name: `${name} ${run}`, ...account});
  expect(created.status).toBe(200);
  return {...account, id: created.body.id};
}

const navLabels = page => page.locator('#sidebar .nav-item[data-view] .nav-label').allTextContents();

async function shot(page, name) {
  if (EVIDENCE) await page.screenshot({path: `${EVIDENCE}/${name}.png`, fullPage: false});
}

test('Admin keeps Settings → Notifications unchanged; a User gets their own Notifications page; ReadOnly gets neither', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const admin = await signIn(browser, ADMIN);
  const user = await person(admin, 'Una', 'user');
  const reader = await person(admin, 'Rhea', 'readonly');

  // ── Admin: the existing Settings, with the existing global Notifications tab.
  expect(await navLabels(admin.page)).toContain('Settings');
  expect(await navLabels(admin.page)).not.toContain('Notifications');
  await admin.page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(admin.page.locator('#page-title')).toHaveText('Settings');
  await expect(admin.page.locator('#view-settings .dp-settings-tabs')).toBeVisible();
  await admin.page.locator('#view-settings [data-tab="notifications"]').click();
  const global = admin.page.locator('#view-settings [data-panel="notifications"]');
  await expect(global.locator('.dp-settings-discord-card')).toBeVisible();
  await expect(global.locator('.dp-settings-statistics-reporting-card')).toBeVisible();
  for (const key of ['discord_notify_update', 'update_check_interval_hours']) {
    await expect(global.locator(`[data-setting="${key}"]`)).toHaveCount(1);
  }
  // The Avatar URL field's one action (Upload while empty, Clear with a URL).
  await expect(global.locator('[data-avatar-action]')).toHaveCount(1);
  await expect(global.locator('[data-settings-help="discord"], [data-settings-help="statistics-reporting"]')).toHaveCount(2);
  await shot(admin.page, 'admin-settings-notifications');
  for (const width of [800, 390]) {
    await admin.page.setViewportSize({width, height: 900});
    await admin.page.evaluate(() => window.closeSidebar?.());
    await shot(admin.page, `admin-settings-notifications-${width}`);
  }
  await admin.page.setViewportSize({width: 1440, height: 914});
  expect(admin.errors).toEqual([]);

  // ── User: Notifications in the Settings slot; a standalone page.
  const signed = await signIn(browser, user);
  const page = signed.page;
  const labels = await navLabels(page);
  expect(labels).not.toContain('Settings');
  expect(labels.slice(labels.indexOf('Statistics'))).toEqual(['Statistics', 'Notifications', 'Help & License']);
  // 1440px is the compact desktop rail: the User's own entries are its icons,
  // each keeping its accessible name and DP tooltip; no Admin entry and no
  // Provider Status appear on the rail either.
  await expect(page.locator('#sidebar-handle')).toBeVisible();
  const ownEntry = page.locator('#sidebar .nav-item', {hasText: 'Notifications'});
  await expect(ownEntry).toHaveAccessibleName('Notifications');
  await expect(ownEntry).toHaveAttribute('data-tooltip', 'Notifications');
  await expect(ownEntry.locator('.nav-label')).toHaveCSS('opacity', '0');
  await expect(page.locator('#sidebar .nav-item[data-view="events"], #sidebar .nav-item[data-view="settings"]:not([data-title])')).toHaveCount(0);
  await expect(page.locator('#sidebar .sidebar-footer')).toHaveCount(0);
  await page.locator('#sidebar .nav-item', {hasText: 'Notifications'}).click();
  await expect(page.locator('#sidebar .nav-item.active .nav-label')).toHaveText('Notifications');
  await expect(page.locator('#page-title')).toHaveText('Notifications');
  await expect(page.locator('#page-subtitle')).toHaveText('Configure notifications for your downloads.');
  const view = page.locator('#view-settings');
  await expect(view.locator('.dp-settings-master-card')).toHaveCount(1);
  await expect(view.locator('.dp-settings-tabs, [data-tab]')).toHaveCount(0);
  await expect(view.locator('.dp-settings-header-title')).toHaveText('The Grapevine');
  await expect(view).not.toContainText('Tuning Deck');
  await expect(view.locator('[data-panel]')).toHaveCount(1);
  await expect(view.locator('.dp-settings-discord-card .card-title')).toContainText('Discord Notifications');
  await expect(view.locator('.dp-settings-statistics-reporting-card .card-title')).toContainText('Statistics Reporting');
  const settings = await view.locator('[data-setting]').evaluateAll(nodes => nodes.map(node => node.dataset.setting));
  expect(new Set(settings)).toEqual(new Set([
    'discord_notifications_enabled', 'discord_username', 'discord_avatar_url', 'discord_webhook_url',
    'discord_webhook_added', 'discord_notify_added', 'discord_notify_finished', 'discord_notify_error',
    'discord_notify_extract', 'stats_reporting_enabled', 'stats_report_webhook_url',
    'stats_report_interval_hours', 'stats_report_window_hours']));
  await expect(view.locator('[data-avatar-action], [data-action="upload-avatar"], [data-action="clear-avatar"]')).toHaveCount(0);
  await expect(view.locator('[data-action="test-discord"]')).toHaveCount(1);
  await expect(view.locator('[data-action="send-report"]')).toHaveCount(1);
  // The same two cards carry the help of the User's OWN scope, never the
  // instance-wide explanation the Admin's cards carry.
  await expect(view.locator('.dp-settings-help-chip')).toHaveCount(2);
  await expect(view.locator('.dp-settings-discord-card [data-settings-help="discord-own"]')).toHaveCount(1);
  await expect(view.locator('.dp-settings-statistics-reporting-card [data-settings-help="statistics-reporting-own"]'))
    .toHaveAttribute('aria-label', 'Help: Statistics Reporting');
  await view.locator('[data-settings-help="statistics-reporting-own"]').click();
  const ownHelp = page.locator('.dp-modal-dialog.dp-settings-help-dialog');
  await expect(ownHelp.locator('.dp-modal-title')).toHaveText('Statistics Reporting');
  await expect(ownHelp).toContainText('activity attributed to you');
  await expect(ownHelp).toContainText('An Admin’s destinations are never used.');
  await page.keyboard.press('Escape');
  await expect(ownHelp).toHaveCount(0);
  // Disabled and unconfigured until the User chooses otherwise.
  await expect(view.locator('[data-setting="discord_notifications_enabled"]')).not.toBeChecked();
  await expect(view.locator('.dp-settings-discord-card')).toContainText('Unconfigured');

  // Save a destination at its own field boundary; it is stored, masked, and survives a reload.
  const webhook = view.locator('[data-setting="discord_webhook_url"]');
  await webhook.fill(HOOK);
  await webhook.press('Tab');
  await expect(view.locator('.dp-settings-discord-card')).toContainText('Configured');
  await expect(webhook).toHaveValue('');
  await expect(webhook).toHaveAttribute('placeholder', /configured/);
  await page.locator('label[for="dp-settings-field-discord-notifications-enabled"] .ttrack').click();
  await expect.poll(async () => (await api(page, 'GET', '/account/notifications')).body.discord_notifications_enabled)
    .toBe(true);
  // A non-Discord destination is refused by the server and stores nothing.
  const refused = await api(page, 'PUT', '/account/notifications', {discord_webhook_added: 'https://example.com/api/webhooks/1/x'});
  expect(refused.status).toBe(422);
  await page.reload();
  await page.locator('#sidebar .nav-item', {hasText: 'Notifications'}).click();
  await expect(view.locator('.dp-settings-discord-card')).toContainText('Configured');
  await expect(view.locator('[data-setting="discord_notifications_enabled"]')).toBeChecked();
  const own = await api(page, 'GET', '/account/notifications');
  expect(own.body.discord_webhook_url).toBe('');
  expect(own.body.discord_webhook_url_configured).toBe(true);
  expect(own.body.discord_webhook_added_configured).toBe(false);
  expect(await page.content()).not.toContain(HOOK);
  // The User's own Clear is inside the field, reads Clear, and clears through
  // the account-scoped endpoint only -- after the canonical confirmation.
  const clear = view.locator('[data-action="clear-webhook"][data-webhook="discord_webhook_url"]');
  expect(await clear.evaluate(button => !!button.closest('.dp-action-field') && button.textContent.trim())).toBe('Clear');
  await expect(clear).toHaveAttribute('aria-label', 'Clear the stored primary Discord webhook');
  const writes = [];
  page.on('request', request => { if (request.method() === 'PUT') writes.push([new URL(request.url()).pathname, request.postDataJSON()]); });
  await clear.click();
  await page.locator('.dp-modal-overlay [data-modal-cancel]').click();
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
  expect(writes).toEqual([]);
  await clear.click();
  await page.locator('.dp-modal-overlay').getByRole('button', {name: 'Clear Webhook'}).click();
  await expect.poll(async () => (await api(page, 'GET', '/account/notifications')).body.discord_webhook_url_configured).toBe(false);
  expect(writes).toEqual([['/api/account/notifications', {clear_secrets: ['discord_webhook_url']}]]);
  await expect(clear).toBeDisabled();
  // The global surfaces stay closed to a User, whatever the client sends.
  expect((await api(page, 'GET', '/settings')).status).toBe(403);
  expect((await api(page, 'POST', '/settings/validate-discord')).status).toBe(403);
  await shot(page, 'user-notifications-1440');
  for (const width of [1024, 800, 390]) {
    await page.setViewportSize({width, height: 900});
    await page.evaluate(() => window.closeSidebar?.());
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    expect(overflow, `no horizontal page scroll at ${width}px`).toBe(false);
    if (width !== 390) {
      // A User's own fields reflow as the Admin's do: two bordered islands, one control width (no upload action).
      const grouped = await view.evaluate(panel => ({
        borders: ['.dp-settings-notifications-identity-row', '.dp-settings-notifications-delivery-row']
          .map(selector => getComputedStyle(panel.querySelector(selector)).borderTopStyle),
        widths: new Set(['discord_username', 'discord_avatar_url', 'discord_webhook_url', 'discord_webhook_added'].map(key =>
          Math.round(panel.querySelector(`[data-setting="${key}"]`).closest('.dp-settings-inline-field')
            .querySelector(':scope > .dp-settings-inline-field-control').getBoundingClientRect().width))).size}));
      expect(grouped, `${width}px`).toEqual({borders: ['solid', 'solid'], widths: 1});
    }
    await shot(page, `user-notifications-${width}`);
  }
  await page.setViewportSize({width: 1440, height: 914});
  await page.evaluate(() => window.toggleTheme());
  await expect(page.locator('body')).toHaveClass(/light/);
  // The theme change animates; capture what it settles on.
  await page.evaluate(() => Promise.all(document.getAnimations().map(animation => animation.finished.catch(() => null))));
  await shot(page, 'user-notifications-light');
  await page.evaluate(() => window.toggleTheme());
  expect(signed.errors).toEqual([]);

  // A role change: the server refuses at once, and the open page recomposes
  // itself. The application reloads it under the new authority (its
  // account_changed pulse re-reads the session); the test waits for that one
  // reload rather than racing it with a second.
  await pulseConnected(page);
  const recomposed = page.waitForEvent('load');
  const demoted = await api(admin.page, 'PATCH', `/accounts/${user.id}`, {role: 'readonly'});
  expect(demoted.status).toBe(200);
  // (Probed outside the page, which is reloading.)
  expect((await page.request.get('/api/account/notifications')).status()).toBe(403);
  await recomposed;
  await expect(page.locator('#sidebar-auth-row .dp-sidebar-profile')).toContainText('ReadOnly User');
  expect(await navLabels(page)).not.toContain('Notifications');
  await expect(page.locator('#view-settings')).toHaveCount(0);

  // ── ReadOnly: neither entry, no page, no personal API.
  const readonly = await signIn(browser, reader);
  const readLabels = await navLabels(readonly.page);
  expect(readLabels).not.toContain('Settings');
  expect(readLabels).not.toContain('Notifications');
  await expect(readonly.page.locator('#view-settings')).toHaveCount(0);
  expect((await api(readonly.page, 'GET', '/account/notifications')).status).toBe(403);
  expect((await api(readonly.page, 'PUT', '/account/notifications', {discord_notifications_enabled: true})).status).toBe(403);
  expect(readonly.errors).toEqual([]);
});
