/* The signed-in Admin session the auth-runtime spec files share.
 *
 * Every sign-in page counts against the login challenge limit (20 per client
 * per rolling minute; auth/throttle.py), and the auth-runtime files run in one
 * shard against one backend. The Admin therefore signs in once per Playwright
 * worker (a worker runs file after file) and later tests open their own
 * browser context on that session's storage state. A test that ends the
 * Admin's session (Log Out) signs in a fresh one of its own instead. */
let storageState = null;

module.exports = {
  get: () => storageState,
  remember: value => { storageState = value; },
};
