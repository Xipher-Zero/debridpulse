/* The signed-in Admin session the auth-runtime spec files share.
 *
 * Every sign-in page counts against the login challenge limit (20 per client
 * per rolling minute; auth/throttle.py), and the auth-runtime files run in one
 * shard against one backend: a shard that drew several files signing in for
 * every test reached that limit, and its next sign-in page answered 429. The
 * Admin therefore signs in once per Playwright worker (a worker runs file after
 * file) and later tests open their own browser context on that session. A test
 * that ends the Admin's session (Log Out) signs in a fresh one of its own
 * instead. */
const {expect} = require('@playwright/test');

const ADMIN = {username: 'browser', password: 'browser-secret'};
let storageState = null;

/* Signs ``page`` (a page of a context on the auth runtime, not yet navigated)
 * in as the Admin: on the worker's session when there is one, else -- or when
 * that session has ended, and the application sends the page to /login --
 * through the sign-in page, whose new session is then the worker's. */
async function signIn(page) {
  if (storageState) {
    await page.context().addCookies(storageState.cookies);
    await page.goto('/');
    if (!/^\/login\b/.test(new URL(page.url()).pathname)) return;
  } else {
    await page.goto('/login');
  }
  await page.locator('#username').fill(ADMIN.username);
  await page.locator('#password').fill(ADMIN.password);
  await page.locator('button[type="submit"]').click();
  await expect(page).not.toHaveURL(/\/login/);
  storageState = await page.context().storageState();
}

module.exports = {
  ADMIN,
  signIn,
  get: () => storageState,
  remember: value => { storageState = value; },
};
