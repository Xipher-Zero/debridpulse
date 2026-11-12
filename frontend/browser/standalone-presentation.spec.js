const { test, expect } = require('@playwright/test');

/* DP 1.0.14 deployment-aware presentation: Server and Standalone.
 *
 * Settings presents ONE collection of sections for the installation's
 * deployment type (``deployment_type``, from the settings read): a Server has
 * all seven, in their order, with Users & Access; a Standalone installation
 * has no Authentication section and its `users` section -- the same key --
 * reads Access & Integrations. The strip, Previous / Next, the arrow keys and
 * the panels all follow that collection. In both, Deployment Type is the
 * section's first card; Standalone omits User Accounts (no card, no actions).
 *
 * My Account on a Standalone installation has no DP Username line, and its
 * header says whether signing in needs a password now (``sign_in_required``,
 * the profile read), or nothing while that is unread. A password-protected
 * Standalone Administrator is offered Clear Password beside Change Password:
 * its own destructive view, proving the current password, then a confirmation
 * stating the consequence. Cancel at either sends nothing.
 *
 * The Browser Runtime containers are Server installations: the Standalone
 * presentation is driven through the server's own reads (the settings
 * document, the account profile) answered with deployment_type standalone,
 * and the clear request is recorded and answered here. What the server
 * decides is proven by the backend's own tests (test_deployment_types.py,
 * C01-C07). Synthetic values only. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const SERVER_SECTIONS = ['Services', 'Downloads', 'Extraction', 'Authentication', 'Users & Access',
  'Notifications', 'Data & Maintenance'];
const STANDALONE_KEYS = ['sources', 'downloads', 'extraction', 'users', 'notifications', 'maintenance'];
const CONSEQUENCE = 'Clearing your password turns off password sign-in. Anyone who can reach DebridPulse '
  + 'can then access this installation without a password, until you set a new one in My Account.';

async function isolateFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

// The settings read, answered as the given deployment type.
async function asDeployment(page, type) {
  await page.route(url => url.pathname === '/api/settings', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    const real = await (await route.fetch()).json();
    return route.fulfill({json: {...real, deployment_type: type}});
  });
}

async function openSettings(page, {width = 1440, type = null} = {}) {
  await page.setViewportSize({width, height: 900});
  await isolateFonts(page);
  if (type) await asDeployment(page, type);
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').evaluate(element => element.click());
  await expect(page.locator('#view-settings .dp-settings-tabs')).toBeAttached();
}

// The strip itself, never a slide's still copy of the window it leaves.
const sections = page => page.locator('#view-settings .dp-settings-tabs [data-tab]:not(.dp-motion-still *)');
const selected = page => page.locator('#view-settings .dp-settings-tabs [data-tab][aria-selected="true"]:not(.dp-motion-still *)');
const usersCards = page => page.locator('#view-settings [data-panel="users"] section.card > .card-header .dp-settings-card-title-text');

test('Server: Authentication and Users & Access, Deployment Type first and the account cards after it', async ({page}) => {
  await openSettings(page);
  expect(await sections(page).locator('.dp-settings-tab-label').allTextContents()).toEqual(SERVER_SECTIONS);
  await expect(page.locator('#view-settings [data-panel="authentication"]')).toHaveCount(1);
  await page.locator('#view-settings [data-tab="users"]').click();
  await expect(page.locator('[data-users-mount] .dp-settings-users-card').first()).toBeVisible();
  expect(await usersCards(page).allTextContents()).toEqual(
    ['Deployment & Language', 'User Accounts', 'Integration Accounts', 'Integration Delivery Profiles']);
  await expect(page.locator('[data-panel="users"] [data-user-action="create-local"]')).toHaveCount(1);
  await expect(page.locator('.dp-settings-deployment')).toHaveAttribute('data-deployment-type', 'server');
});

test('Standalone: no Authentication tab or panel, Access & Integrations on the users key, and no User Accounts card or action', async ({page}) => {
  await openSettings(page, {type: 'standalone'});
  expect(await sections(page).evaluateAll(tabs => tabs.map(tab => tab.dataset.tab))).toEqual(STANDALONE_KEYS);
  await expect(page.locator('#view-settings [data-tab="authentication"]')).toHaveCount(0);
  await expect(page.locator('#view-settings [data-panel="authentication"]')).toHaveCount(0);
  await expect(page.locator('#view-settings .dp-settings-auth-status-card')).toHaveCount(0);
  const users = page.locator('#view-settings [data-tab="users"]');
  await expect(users.locator('.dp-settings-tab-label')).toHaveText('Access & Integrations');
  await expect(users.locator('img')).toHaveAttribute('src', '/icons/lucide/users.svg');
  await users.click();
  await expect(page.locator('.dp-settings-users-integrations-card')).toBeVisible();
  expect(await usersCards(page).allTextContents()).toEqual(
    ['Deployment & Language', 'Integration Accounts', 'Integration Delivery Profiles']);
  await expect(page.locator('#view-settings .dp-settings-users-table')).toHaveCount(0);
  await expect(page.locator('#view-settings [data-user-action="create-local"], #view-settings [data-user-action="merge"]')).toHaveCount(0);
  // The integration cards keep their actions.
  await expect(page.locator('#view-settings [data-user-action="create-integration"]')).toBeVisible();
  await expect(page.locator('#view-settings [data-user-action="create-profile"]')).toBeVisible();
  // Deployment Type, first, is the Standalone one and keeps its one action.
  await expect(page.locator('.dp-settings-deployment')).toHaveAttribute('data-deployment-type', 'standalone');
  await expect(page.locator('[data-user-action="deployment"]')).toHaveText('Switch to Server');
});

for (const width of [1440, 320]) {
  test(`Standalone navigation at ${width}px: Previous / Next, the arrow keys and the panels travel only the presented sections`, async ({page}) => {
    await openSettings(page, {width, type: 'standalone'});
    await expect(selected(page)).toHaveAttribute('data-tab', 'sources');
    const visited = ['sources'];
    const next = page.locator('#view-settings .dp-settings-nav-end[data-settings-end="next"]:not(.dp-motion-still *)');
    if (await next.isVisible()) {
      // The compact navigator: Next to the last section, one at a time.
      while (await next.isVisible()) {
        await next.click();
        const key = await selected(page).getAttribute('data-tab');
        await expect(page.locator(`#view-settings [data-panel="${key}"]`)).toBeVisible();
        visited.push(key);
      }
      expect(visited).toEqual(STANDALONE_KEYS);
      const previous = page.locator('#view-settings .dp-settings-nav-end[data-settings-end="previous"]:not(.dp-motion-still *)');
      await previous.click();
      await expect(selected(page)).toHaveAttribute('data-tab', 'notifications');
    }
    // The arrow keys, Home and End: the same collection, focus on the selection.
    const services = sections(page).and(page.locator('[data-tab="sources"]'));
    await services.evaluate(tab => tab.click());
    await expect(selected(page)).toHaveAttribute('data-tab', 'sources');
    await services.focus();
    const keyed = [];
    for (let step = 1; step < STANDALONE_KEYS.length; step += 1) {
      await page.keyboard.press('ArrowRight');
      keyed.push(await page.evaluate(() => document.activeElement?.dataset?.tab));
    }
    expect(keyed).toEqual(STANDALONE_KEYS.slice(1));
    await page.keyboard.press('ArrowRight');
    await expect(selected(page)).toHaveAttribute('data-tab', 'sources');
    await page.keyboard.press('End');
    await expect(selected(page)).toHaveAttribute('data-tab', 'maintenance');
    await expect(sections(page).and(page.locator('[data-tab="maintenance"]'))).toBeFocused();
    await expect(page.locator('#view-settings [data-panel="maintenance"]')).toBeVisible();
    await expect(selected(page)).toHaveCount(1);
    expect(await sections(page).evaluateAll(tabs => tabs.filter(tab => tab.tabIndex === 0).length)).toBe(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBe(0);
  });
}

test('a Settings entry after a change to Standalone opens on Services, never a section it does not present', async ({page}) => {
  await openSettings(page);
  await page.locator('#view-settings [data-tab="authentication"]').click();
  await expect(page.locator('#view-settings [data-panel="authentication"]')).toBeVisible();
  await asDeployment(page, 'standalone');
  await page.locator('#sidebar .nav-item[data-view="dashboard"]').evaluate(element => element.click());
  await page.locator('#sidebar .nav-item[data-view="settings"]').evaluate(element => element.click());
  await expect(page.locator('#view-settings [data-tab="authentication"]')).toHaveCount(0);
  await expect(selected(page)).toHaveAttribute('data-tab', 'sources');
  await expect(page.locator('#view-settings [data-panel="sources"]')).toBeVisible();
});

// ── My Account ─────────────────────────────────────────────────────────────

async function signedIn(browser, profile) {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const context = await browser.newContext({baseURL: AUTH, viewport: {width: 1280, height: 900}});
  const page = await context.newPage();
  page.__cleared = [];
  page.__writes = [];
  page.__answer = 409;
  await isolateFonts(page);
  await page.goto('/login');
  await page.locator('#username').fill('browser');
  await page.locator('#password').fill('browser-secret');
  await page.locator('button[type="submit"]').click();
  await expect(page).not.toHaveURL(/\/login/);
  await page.waitForFunction(() => Boolean(window.debridPulseAuth?.authority?.account_id));
  await page.route(url => url.pathname.startsWith('/api/account'), async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET' && path === '/api/account' && profile) {
      const real = await (await route.fetch()).json();
      return route.fulfill({json: {...real, ...profile}});
    }
    if (request.method() === 'POST' && path === '/api/account/clear-password') {
      page.__cleared.push(JSON.parse(request.postData()));
      return route.fulfill(page.__answer === 200 ? {json: {ok: true}}
        : {status: page.__answer, json: {detail: 'The current password is not correct'}});
    }
    if (request.method() !== 'GET') { page.__writes.push(`${request.method()} ${path}`); return route.abort(); }
    return route.continue();
  });
  return {context, page};
}

async function myAccount(page) {
  await page.locator('#sidebar-auth-row .dp-sidebar-profile').click();
  const dialog = page.locator('.dp-account-dialog');
  await expect(dialog.locator('[data-field="display_name"]')).toHaveValue(/.+/);
  return dialog;
}

const footer = dialog => dialog.locator('.dp-modal-footer .btn:not([hidden])');
const PROTECTED = {deployment_type: 'standalone', sign_in_required: true, has_password: true};

test('Server My Account is unchanged: its DP Username line, no status hint, and no Clear Password', async ({browser}) => {
  const {context, page} = await signedIn(browser, null);
  const dialog = await myAccount(page);
  await expect(dialog.locator('.dp-account-username .form-label')).toHaveText('DP Username');
  await expect(dialog.locator('.dp-account-username-note')).toHaveCount(1);
  await expect(dialog.locator('.dp-modal-title-hint')).toHaveCount(0);
  await expect(dialog.locator('.dp-modal-title')).toHaveText('My Account');
  expect(await footer(dialog).allTextContents()).toEqual(['Change Password', 'Cancel', 'Save']);
  await expect(dialog.locator('[data-account-view="clear"]')).toHaveCount(0);
  await context.close();
});

test('password-protected Standalone: "Password required to sign in", no DP Username, and Change Password beside a destructive Clear Password', async ({browser}) => {
  const {context, page} = await signedIn(browser, PROTECTED);
  const dialog = await myAccount(page);
  await expect(dialog.locator('.dp-modal-title')).toHaveText('My Account');
  await expect(dialog.locator('.dp-modal-heading .dp-modal-title-hint')).toHaveText('Password required to sign in');
  await expect(dialog.locator('.dp-account-username, [data-account-username], .dp-account-username-note')).toHaveCount(0);
  await expect(dialog.getByText('DP Username')).toHaveCount(0);
  expect(await dialog.locator('[data-account-view="profile"] .form-label').allTextContents())
    .toEqual(['Display Name', 'Actual Name', 'Email']);
  expect(await footer(dialog).allTextContents()).toEqual(['Change Password', 'Clear Password', 'Cancel', 'Save']);
  await expect(dialog.getByRole('button', {name: 'Clear Password'})).toHaveClass(/\bbtn-danger\b/);
  // One row, inside the dialog.
  const rows = await footer(dialog).evaluateAll(buttons => {
    const box = buttons[0].closest('.dp-modal-dialog').getBoundingClientRect();
    return {tops: new Set(buttons.map(b => Math.round(b.getBoundingClientRect().top))).size,
      inside: buttons.every(b => b.getBoundingClientRect().left >= box.left && b.getBoundingClientRect().right <= box.right)};
  });
  expect(rows).toEqual({tops: 1, inside: true});

  // Change Password is still its own view, with its Current Password.
  await dialog.getByRole('button', {name: 'Change Password'}).click();
  await expect(dialog.locator('[data-account-view="password"] [data-field="current"]')).toBeVisible();
  await dialog.locator('[data-modal-cancel]').click();
  await expect(dialog.locator('[data-account-view="profile"]')).toBeVisible();
  expect(page.__writes).toEqual([]);
  await context.close();
});

test('Clear Password: a destructive view that proves the current password; Back and the confirmation\'s Cancel send nothing', async ({browser}) => {
  const {context, page} = await signedIn(browser, PROTECTED);
  const dialog = await myAccount(page);
  await dialog.locator('[data-field="display_name"]').fill('Unsaved Standalone Name');
  await dialog.getByRole('button', {name: 'Clear Password'}).click();
  const view = dialog.locator('[data-account-view="clear"]');
  await expect(view).toBeVisible();
  await expect(dialog).toHaveAttribute('data-tone', 'danger');
  await expect(dialog.locator('[data-modal-accept]')).toHaveText('Clear Password');
  await expect(dialog.locator('[data-modal-accept]')).toHaveClass(/\bbtn-danger\b/);
  await expect(view.locator('[data-field="clear_current"]')).toBeFocused();
  await expect(view.locator('[data-field="clear_current"]')).toHaveAttribute('autocomplete', 'current-password');
  await expect(view.locator('.dp-account-clear-warning')).toHaveText(CONSEQUENCE);
  await expect(dialog.getByRole('button', {name: 'Change Password'})).toBeHidden();

  // Back: the profile, its unsaved edit kept, nothing sent.
  await dialog.locator('[data-modal-cancel]').click();
  await expect(dialog.locator('[data-account-view="profile"]')).toBeVisible();
  await expect(dialog).not.toHaveAttribute('data-tone', 'danger');
  await expect(dialog.locator('[data-modal-accept]')).toHaveText('Save');
  await expect(dialog.locator('[data-field="display_name"]')).toHaveValue('Unsaved Standalone Name');

  // No password: refused here, nothing sent.
  await dialog.getByRole('button', {name: 'Clear Password'}).click();
  await dialog.locator('[data-modal-accept]').click();
  await expect(page.locator('.toast').last()).toHaveText('Enter your current password.');
  await view.locator('[data-field="clear_current"]').fill('synthetic-current-1');
  await dialog.locator('[data-modal-accept]').click();
  const confirm = page.locator('.dp-modal-dialog[role="alertdialog"]');
  await expect(confirm.locator('.dp-modal-title')).toHaveText('Clear Password?');
  await expect(confirm.locator('.dp-modal-message')).toHaveText(CONSEQUENCE);
  await expect(confirm).toHaveAttribute('data-tone', 'danger');
  await confirm.locator('[data-modal-cancel]').click();
  await expect(confirm).toHaveCount(0);
  await expect(view).toBeVisible();
  expect(page.__cleared).toEqual([]);

  // A refusal keeps the view and says why.
  await dialog.locator('[data-modal-accept]').click();
  await confirm.locator('[data-modal-accept]').click();
  await expect(page.locator('.toast').last()).toHaveText('The current password is not correct');
  await expect(view).toBeVisible();
  expect(page.__cleared).toEqual([{current_password: 'synthetic-current-1'}]);

  // Accepted: the page starts again, as the server now admits it.
  page.__answer = 200;
  await dialog.locator('[data-modal-accept]').click();
  await Promise.all([page.waitForEvent('load'), confirm.locator('[data-modal-accept]').click()]);
  expect(page.__cleared).toHaveLength(2);
  expect(page.__writes).toEqual([]);
  await context.close();
});

test('open Standalone: "No password required to sign in" and Set Password, no Clear; an unread reading says neither', async ({browser}) => {
  const open = await signedIn(browser, {deployment_type: 'standalone', sign_in_required: false, has_password: false});
  let dialog = await myAccount(open.page);
  await expect(dialog.locator('.dp-modal-title-hint')).toHaveText('No password required to sign in');
  expect(await footer(dialog).allTextContents()).toEqual(['Set Password', 'Cancel', 'Save']);
  await expect(dialog.locator('.dp-account-username')).toHaveCount(0);
  await open.context.close();

  const unread = await signedIn(browser, {deployment_type: 'standalone', sign_in_required: undefined});
  dialog = await myAccount(unread.page);
  await expect(dialog.locator('.dp-account-username')).toHaveCount(0);
  await expect(dialog.locator('.dp-modal-title-hint')).toHaveCount(0);
  await expect(dialog.getByText(/password required to sign in/i)).toHaveCount(0);
  await expect(dialog.getByRole('button', {name: 'Clear Password'})).toHaveCount(0);
  await unread.context.close();
});
