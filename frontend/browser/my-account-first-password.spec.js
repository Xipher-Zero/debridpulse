const { test, expect } = require('@playwright/test');

/* DP 1.0.14 My Account: Set Password, an account's first local password.
 *
 * The one My Account dialog offers Set Password exactly when the account's
 * own profile read says it has no local password (an account that signs in
 * through its identity provider), and Change Password -- unchanged, with its
 * Current Password -- when it has one; neither when the read fails. Set
 * Password's view has New and Confirm only, the account's own username for
 * a password manager (never sent), and one static line on local sign-in. It
 * sends only the new password, and the account then signs in again. Nothing
 * offers it unasked.
 *
 * A real signed-in session (DP_AUTH_BASE_URL). The account's own profile read
 * is served as an account without a password, and the first-password request
 * is recorded and answered here: what the server decides is proven by the
 * backend's own tests (test_own_first_password.py). Synthetic values only. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const NEW = 'synthetic-browser-new-1';

async function open(browser, {profile = null, viewport = {width: 1280, height: 900}, answer = 200} = {}) {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const context = await browser.newContext({baseURL: AUTH, viewport});
  const page = await context.newPage();
  page.__first = [];
  page.__writes = [];
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
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
      if (profile === 'unreadable') return route.fulfill({status: 500, json: {detail: 'unavailable'}});
      const real = await (await route.fetch()).json();
      return route.fulfill({json: {...real, ...profile}});
    }
    if (request.method() === 'POST' && path === '/api/account/first-password') {
      page.__first.push(request.postData());
      return route.fulfill(answer === 200 ? {json: {ok: true}} : {status: answer, json: {detail: 'A password must be at least 8 characters'}});
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

const NO_PASSWORD = {has_password: false, username: 'oidc-person'};

test('an account without a password is offered Set Password: New and Confirm only, its username for a password manager, one static line', async ({browser}) => {
  const {context, page} = await open(browser, {profile: NO_PASSWORD});
  // Nothing offers it unasked: no dialog, no prompt, no Set Password anywhere until My Account is opened.
  await expect(page.locator('.dp-account-dialog')).toHaveCount(0);
  await expect(page.getByText('Set Password')).toHaveCount(0);
  const dialog = await myAccount(page);
  await expect(dialog.getByRole('button', {name: 'Change Password'})).toHaveCount(0);
  await dialog.getByRole('button', {name: 'Set Password'}).click();
  const view = dialog.locator('[data-account-view="password"]');
  await expect(view).toBeVisible();
  // No Current Password, for sight or for assistive technology.
  await expect(view.locator('[data-field="current"]')).toHaveCount(0);
  await expect(view.getByText('Current Password')).toHaveCount(0);
  await expect(dialog.getByRole('textbox', {name: 'Current Password'})).toHaveCount(0);
  await expect(view.locator('input[type="password"]')).toHaveCount(2);
  await expect(view.locator('[data-field="next"]')).toHaveAttribute('autocomplete', 'new-password');
  await expect(view.locator('[data-field="confirm"]')).toHaveAttribute('autocomplete', 'new-password');
  await expect(view.locator('[data-field="next"]')).toBeFocused();
  // The account's own username, for a password manager: present, not shown, not a field of the form.
  const username = view.locator('input[autocomplete="username"]');
  await expect(username).toHaveCount(1);
  await expect(username).toHaveValue('oidc-person');
  await expect(username).toBeHidden();
  await expect(username).toHaveAttribute('readonly', '');
  await expect(username).not.toHaveAttribute('data-field', /.*/);
  await expect(view.getByText('Local password sign-in works only if your administrator has enabled it.')).toBeVisible();
  await expect(view.locator('p')).toHaveCount(1);
  expect(page.__first).toEqual([]);
  await context.close();
});

test('Set Password checks its two fields first, keeps profile edits on Cancel, clears secrets, and sends only the new password', async ({browser}) => {
  const {context, page} = await open(browser, {profile: NO_PASSWORD});
  const dialog = await myAccount(page);
  const view = dialog.locator('[data-account-view="password"]');
  await dialog.locator('[data-field="display_name"]').fill('Edited Display Name');
  await dialog.getByRole('button', {name: 'Set Password'}).click();
  // Empty, then mismatched: the established feedback, nothing sent.
  await dialog.locator('[data-modal-accept]').click();
  await expect(page.locator('.toast').last()).toContainText('Enter a new password.');
  await view.locator('[data-field="next"]').fill(NEW);
  await view.locator('[data-field="confirm"]').fill(`${NEW}-different`);
  await dialog.locator('[data-modal-accept]').click();
  await expect(page.locator('.toast').last()).toContainText('The new passwords do not match.');
  expect(page.__first).toEqual([]);
  // Cancel: back to the profile, its unsaved edit kept; the secrets gone.
  await dialog.locator('[data-modal-cancel]').click();
  await expect(dialog.locator('[data-account-view="profile"]')).toBeVisible();
  await expect(dialog.locator('[data-field="display_name"]')).toHaveValue('Edited Display Name');
  await dialog.getByRole('button', {name: 'Set Password'}).click();
  await expect(view.locator('[data-field="next"]')).toHaveValue('');
  await expect(view.locator('[data-field="confirm"]')).toHaveValue('');
  // Save: exactly the new password -- no username, account or profile field -- then sign in again.
  await view.locator('[data-field="next"]').fill(NEW);
  await view.locator('[data-field="confirm"]').fill(NEW);
  const signIn = page.waitForURL(/\/login|\/$/);
  await dialog.locator('[data-modal-accept]').click();
  await expect.poll(() => page.__first.length).toBe(1);
  expect(JSON.parse(page.__first[0])).toEqual({new_password: NEW});
  await signIn;
  expect(page.__writes).toEqual([]);
  await context.close();
});

test('a refused Set Password keeps the dialog open and says why', async ({browser}) => {
  const {context, page} = await open(browser, {profile: NO_PASSWORD, answer: 409});
  const dialog = await myAccount(page);
  await dialog.getByRole('button', {name: 'Set Password'}).click();
  await dialog.locator('[data-field="next"]').fill('short7!');
  await dialog.locator('[data-field="confirm"]').fill('short7!');
  await dialog.locator('[data-modal-accept]').click();
  await expect(page.locator('.toast').last()).toContainText('A password must be at least 8 characters');
  await expect(dialog.locator('[data-account-view="password"]')).toBeVisible();
  await expect(page).not.toHaveURL(/\/login/);
  await context.close();
});

test('an account with a password keeps Change Password, its Current Password and no Set Password; an unread profile offers neither', async ({browser}) => {
  const signed = await open(browser);
  const dialog = await myAccount(signed.page);
  await expect(dialog.getByRole('button', {name: 'Set Password'})).toHaveCount(0);
  await dialog.getByRole('button', {name: 'Change Password'}).click();
  const view = dialog.locator('[data-account-view="password"]');
  await expect(view.locator('[data-field="current"]')).toHaveAttribute('autocomplete', 'current-password');
  await expect(view.locator('[data-field="current"]')).toBeFocused();
  await expect(view.locator('input[autocomplete="username"]')).toHaveCount(0);
  await expect(view.getByText('Local password sign-in works only')).toHaveCount(0);
  await signed.context.close();

  const unread = await open(browser, {profile: 'unreadable'});
  const fallback = unread.page.locator('.dp-account-dialog');
  await unread.page.locator('#sidebar-auth-row .dp-sidebar-profile').click();
  await expect(fallback.locator('[data-field="display_name"]')).toBeVisible();
  await expect(fallback.getByRole('button', {name: /Password/})).toHaveCount(0);
  await unread.context.close();
});

test('on a phone the same dialog offers Set Password from the account sheet', async ({browser}) => {
  const {context, page} = await open(browser, {profile: NO_PASSWORD, viewport: {width: 390, height: 844}});
  await page.locator('#topbar-account-button').click();
  await page.locator('[data-account-action="profile"]').click();
  const dialog = page.locator('.dp-account-dialog');
  await dialog.getByRole('button', {name: 'Set Password'}).click();
  await expect(dialog.locator('[data-account-view="password"] input[type="password"]')).toHaveCount(2);
  await expect(dialog.locator('[data-field="current"]')).toHaveCount(0);
  await context.close();
});
