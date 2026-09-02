const { test, expect } = require('@playwright/test');

async function waitForSessionBootstrap(page) {
  await expect.poll(() => page.evaluate(() => window.debridPulseAuth?.session() !== null)).toBeTruthy();
}

test('unrelated application 401 does not redirect an open-mode browser to login', async ({ page }) => {
  let loginRequests = 0;
  let sessionRequests = 0;

  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname === '/login') loginRequests += 1;
    if (url.pathname === '/api/auth/session') sessionRequests += 1;
  });

  await page.route('**/api/bootstrap-401-probe', route => route.fulfill({
    status: 401,
    contentType: 'application/json',
    body: JSON.stringify({detail: 'Endpoint-specific authorization failure'}),
  }));

  await page.goto('/');
  await waitForSessionBootstrap(page);
  expect(await page.evaluate(() => window.debridPulseAuth.session().authenticated)).toBe(false);

  const sessionsBeforeProbe = sessionRequests;
  const status = await page.evaluate(async () => {
    const response = await fetch('/api/bootstrap-401-probe');
    return response.status;
  });

  expect(status).toBe(401);
  await expect.poll(() => sessionRequests).toBeGreaterThan(sessionsBeforeProbe);
  await page.waitForTimeout(150);
  expect(loginRequests).toBe(0);
  expect(new URL(page.url()).pathname).toBe('/');
  expect(await page.evaluate(() => window.debridPulseAuth.session().authenticated)).toBe(false);
});

test('application 401 still redirects when canonical session confirmation reports expiration', async ({ page }) => {
  let sessionExpired = false;

  await page.route('**/api/auth/session', route => {
    if (sessionExpired) {
      return route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({detail: 'Unauthorized'}),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        authenticated: true,
        mechanism: 'password_session',
        subject: 'operator',
        display_name: 'operator',
        csrf_token: 'browser-test-csrf',
        session_expires_in_seconds: 3600,
      }),
    });
  });

  await page.route('**/api/bootstrap-expired-probe', route => route.fulfill({
    status: 401,
    contentType: 'application/json',
    body: JSON.stringify({detail: 'Unauthorized'}),
  }));

  await page.route('**/login**', route => route.fulfill({
    status: 200,
    contentType: 'text/html',
    body: '<!doctype html><title>Login test target</title>',
  }));

  await page.goto('/');
  await waitForSessionBootstrap(page);
  expect(await page.evaluate(() => window.debridPulseAuth.session().authenticated)).toBe(true);

  sessionExpired = true;
  await page.evaluate(() => fetch('/api/bootstrap-expired-probe'));

  await expect(page).toHaveURL(/\/login\?next=/);
});
