const { test, expect, request: playwrightRequest } = require('@playwright/test');

/* WS-S1: connecting a browser, on the password-auth runtime. The extension's
 * side (start, poll, the browser-client API) is driven over HTTP with an
 * extension origin, exactly as an extension's background sends it; the
 * person's side is the real interface: the Connect a browser dialog opened
 * from /?connect-browser=, and My Account's Connected browsers. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const EXTENSION = 'moz-extension://5b1f0c9e-2d55-4d5e-9b51-6a2f1d0c7e11';
const adminSession = require('./acl-admin-session');
const run = Date.now().toString(36);

async function extensionClient() {
  return playwrightRequest.newContext({baseURL: AUTH, extraHTTPHeaders: {Origin: EXTENSION}});
}

async function adminPage(browser) {
  const context = await browser.newContext({baseURL: AUTH, viewport: {width: 1440, height: 914}});
  const page = await context.newPage();
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  const errors = [];
  page.on('pageerror', error => errors.push(String(error.message)));
  await adminSession.signIn(page);
  return {context, page, errors};
}

async function start(extension, name) {
  const started = await extension.post('/api/browser/v1/pairings', {data: {client_name: name}});
  expect(started.status()).toBe(200);
  return started.json();
}

async function poll(extension, deviceCode) {
  const answer = await extension.post('/api/browser/v1/pairings/poll', {data: {device_code: deviceCode}});
  expect(answer.status()).toBe(200);
  return answer.json();
}

test('a browser is connected from its link and disconnected from My Account', async ({browser}) => {
  test.setTimeout(90_000);
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const extension = await extensionClient();
  const admin = await adminPage(browser);
  try {
    const pairing = await start(extension, `Firefox ${run}`);
    await admin.page.goto(pairing.verification_path);
    const dialog = admin.page.locator('.dp-browser-pairing-dialog');
    await expect(dialog).toBeVisible();
    // The link is read once and leaves the address bar.
    await expect.poll(() => new URL(admin.page.url()).search).toBe('');
    await expect(dialog.locator('[data-pairing-code]')).toHaveText(pairing.user_code);
    await expect(dialog.locator('[data-pairing-browser]')).toHaveText(`Firefox ${run}`);
    await expect(dialog.locator('.dp-browser-pairing-warning')).toBeVisible();
    await dialog.locator('[data-pairing-name]').fill(`Laptop ${run}`);
    await dialog.getByRole('button', {name: 'Connect', exact: true}).click();
    await expect(dialog).toBeHidden();

    await expect.poll(async () => (await poll(extension, pairing.device_code)).status, {intervals: [2100]}).toBe('approved');
    // (the approved answer is handed out once: read it from a fresh pairing below)

    // A second browser: approved, collected, then disconnected from My Account.
    const second = await start(extension, `Chrome ${run}`);
    await admin.page.goto(second.verification_path);
    await admin.page.locator('.dp-browser-pairing-dialog').getByRole('button', {name: 'Connect', exact: true}).click();
    let collected = null;
    await expect.poll(async () => { collected = await poll(extension, second.device_code); return collected.status; },
      {intervals: [2100]}).toBe('approved');
    const session = await extension.get('/api/browser/v1/session', {headers: {Authorization: `Bearer ${collected.token}`}});
    expect(session.status()).toBe(200);

    await admin.page.locator('#sidebar-auth-row .dp-sidebar-profile').click();
    const account = admin.page.locator('.dp-account-dialog');
    await account.getByRole('button', {name: 'Connected browsers'}).click();
    const row = account.locator('.dp-account-browser', {hasText: `Chrome ${run}`});
    await expect(row).toBeVisible();
    await expect(account.locator('.dp-account-browser', {hasText: `Laptop ${run}`})).toBeVisible();
    await row.getByRole('button', {name: 'Disconnect'}).click();
    await admin.page.locator('.dp-modal-dialog').last().getByRole('button', {name: 'Disconnect'}).click();
    await expect(row).toBeHidden();
    const after = await extension.get('/api/browser/v1/session', {headers: {Authorization: `Bearer ${collected.token}`}});
    expect(after.status()).toBe(401);
    expect(admin.errors).toEqual([]);
  } finally {
    await admin.context.close();
    await extension.dispose();
  }
});

test('Deny refuses the browser, and an answered link says so', async ({browser}) => {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const extension = await extensionClient();
  const admin = await adminPage(browser);
  try {
    const pairing = await start(extension, `Edge ${run}`);
    await admin.page.goto(pairing.verification_path);
    await admin.page.locator('.dp-browser-pairing-dialog').getByRole('button', {name: 'Deny'}).click();
    await expect(admin.page.locator('.dp-browser-pairing-dialog')).toBeHidden();
    expect((await poll(extension, pairing.device_code)).status).toBe('denied');
    // The same link again: already answered.
    await admin.page.goto(pairing.verification_path);
    await expect(admin.page.locator('.dp-browser-pairing-dialog .dp-browser-pairing-message'))
      .toHaveText('This connection request has expired or was already answered.');
    expect(admin.errors).toEqual([]);
  } finally {
    await admin.context.close();
    await extension.dispose();
  }
});
