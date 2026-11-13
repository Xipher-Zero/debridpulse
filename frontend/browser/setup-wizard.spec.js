const { test, expect } = require('@playwright/test');

/* DP 1.0.14 setup wizard, WS2: the wizard's presentation owner
 * (ui-setup-wizard.js) and My Account's Setup Wizard section (auth.js).
 *
 * W01 runs against the real open runtime, an existing installation whose
 * setup is complete: My Account's section, and an Admin's rerun opened over
 * My Account -- Next through pages whose choices are unchanged (nothing is
 * written but the rerun's own record), the Server rerun's Administrator page
 * offering guidance only, and Close ending the rerun with My Account's draft
 * kept. W02 shows a NEW installation's initial setup by answering the one
 * setup-state read with an unfinished setup (a mock of the server's fact,
 * labelled as such; the server's own decision is proven by
 * backend/tests/test_setup_lifecycle.py): it opens by itself, cannot be
 * dismissed, and re-words in place in en-XA and Arabic without sending
 * anything. W03 measures the rerun and My Account's section at 320px, in
 * English and en-XA. Locators are roles, stable data hooks and catalog keys,
 * never incidental English. Synthetic values only. */

/* Page turns slide (ui-motion.js); these cases read each page as it stands, so they run with reduced motion,
 * where a turn is immediate -- the motion itself is R30 and R32's (setup-wizard-structure.spec.js). */
async function load(page, {width = 1280, height = 900, motion = false} = {}) {
  await page.emulateMedia({reducedMotion: motion ? 'no-preference' : 'reduce'});
  await page.setViewportSize({width, height});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/');
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
  await page.evaluate(() => window.DPI18n.ready);
}

const wizard = page => page.locator('.dp-modal-dialog.dp-setup-dialog');
const account = page => page.locator('.dp-modal-dialog.dp-account-dialog');
const setLocale = (page, locale) => page.evaluate(locale => window.DPI18n.setLocale(locale), locale);
const state = page => page.evaluate(() => window.debridPulseAuth.fetch('/api/setup/state').then(response => response.json()));
const t = (page, key, values) => page.evaluate(([key, values]) => window.DPI18n.t(key, values), [key, values]);

async function openAccount(page) {
  if ((page.viewportSize()?.width || 1440) < 780) await page.locator('#topbar-account-button').click();
  else await page.locator('#sidebar-auth-row .dp-sidebar-profile').click();
  await expect(account(page)).toBeVisible();
}

test('W01: My Account offers Rerun Setup Wizard; a rerun opens over it, changes nothing it was not asked to, and Close ends it', async ({page}) => {
  await load(page);
  // No earlier rerun of this Admin's (a rerun resumes where it stopped).
  await page.evaluate(() => window.debridPulseAuth.fetch('/api/setup/rerun', {method: 'DELETE'}));
  const before = await state(page);
  expect(before.initial.complete).toBe(true);
  expect(before.rerun).toBeNull();
  await openAccount(page);
  const section = account(page).locator('.dp-account-setup');
  await expect(section).toBeVisible();
  // Heading, two sentences each on its own line, then the button: centred, inside the scrolling body.
  await expect(section.locator('[data-i18n="account.setup.title"]')).toHaveText(await t(page, 'account.setup.title'));
  await expect(section.locator('.dp-account-setup-hint')).toHaveCount(2);
  const geometry = await section.evaluate(node => {
    const body = node.closest('.dp-modal-body').getBoundingClientRect();
    const rule = getComputedStyle(node, '::before');
    const hints = [...node.querySelectorAll('.dp-account-setup-hint')].map(hint => hint.getBoundingClientRect());
    return {inBody: node.closest('.dp-modal-body') !== null, align: getComputedStyle(node).textAlign,
      ruleRatio: parseFloat(rule.width) / body.width, separateLines: hints[1].top >= hints[0].bottom - 1};
  });
  expect(geometry.inBody).toBe(true);
  expect(geometry.align).toBe('center');
  expect(geometry.ruleRatio).toBeGreaterThan(0.4);
  expect(geometry.ruleRatio).toBeLessThan(0.7);
  expect(geometry.separateLines).toBe(true);
  // An unsaved profile edit is neither saved nor discarded by the rerun.
  const name = account(page).locator('[data-field="display_name"]');
  const saved = await name.inputValue();
  await name.fill(`${saved} (draft)`);

  const requests = [];
  page.on('request', request => { if (request.method() !== 'GET') requests.push(`${request.method()} ${new URL(request.url()).pathname}`); });
  await section.locator('[data-account-setup-rerun]').click();
  await expect(wizard(page)).toBeVisible();
  const deployment = await page.evaluate(() => window.debridPulseAuth.authority.deployment_type);
  const total = deployment === 'standalone' ? 4 : 5;
  await step(page, 1, total);
  await expect(wizard(page).locator('[data-modal-cancel]')).toBeVisible();      // a rerun may be closed
  await expect(wizard(page).locator('[data-modal-action="back"]')).toBeHidden();    // the first page has no Back
  expect((await state(page)).rerun).toEqual({phase: 'welcome'});
  // Welcome (language unchanged) and Download Directory (folder unchanged): Next commits nothing new.
  await wizard(page).locator('[data-modal-accept]').click();
  await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
  await wizard(page).locator('[data-modal-accept]').click();
  if (deployment !== 'standalone') {
    // The Server rerun's Administrator page is guidance: no credential field, a way to Users & Access.
    await expect(wizard(page).locator('.dp-setup-administrator')).toBeVisible();
    await expect(wizard(page).locator('input')).toHaveCount(0);
    await expect(wizard(page).locator('[data-setup-users]')).toBeVisible();
  } else {
    await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
  }
  expect(requests.filter(entry => !entry.startsWith('PUT /api/setup/rerun'))).toEqual([]);
  await wizard(page).locator('[data-modal-cancel]').click();
  await expect(wizard(page)).toHaveCount(0);
  await expect(account(page)).toBeVisible();
  await expect(name).toHaveValue(`${saved} (draft)`);
  const after = await state(page);
  expect(after.rerun).toBeNull();
  expect(after.initial.complete).toBe(true);
  await account(page).locator('[data-modal-cancel]').click();
});

test('W02: a new installation\'s setup opens by itself, cannot be dismissed, and re-words in place in en-XA and Arabic (mocked setup state)', async ({page}) => {
  await page.route(url => url.pathname === '/api/setup/state', route => route.fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify({initial: {complete: false, phase: 'welcome', administrator: 'none'}, rerun: null})}));
  const sent = [];
  page.on('request', request => { if (request.method() !== 'GET') sent.push(`${request.method()} ${new URL(request.url()).pathname}`); });
  await load(page);
  await expect(wizard(page)).toBeVisible();
  const deployment = await page.evaluate(() => window.debridPulseAuth.authority.deployment_type);
  const total = deployment === 'standalone' ? 5 : 6;
  await step(page, 1, total);
  await expect(wizard(page).locator('[data-modal-cancel]')).toBeHidden();
  await expect(wizard(page).locator('[data-modal-action="back"]')).toBeHidden();
  // Welcome's action is Next, as its words ask (it was Get Started).
  await expect(wizard(page).locator('[data-modal-accept]')).toHaveText(await t(page, 'setup.action.next'));
  await page.keyboard.press('Escape');
  await expect(wizard(page)).toBeVisible();

  // A language switch while it is open: words change in place, the dialog and focus stay.
  const button = wizard(page).locator('[data-setup-language]');
  await button.focus();
  const englishTitle = await wizard(page).locator('.dp-modal-title').textContent();
  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  await expect(wizard(page).locator('.dp-modal-title')).toHaveText(await t(page, 'setup.welcome.title'));
  expect(await wizard(page).locator('.dp-modal-title').textContent()).not.toBe(englishTitle);
  await expect(button).toBeFocused();
  // Arabic through the wizard's own language view (one row of the one list, in
  // this same dialog): right to left, back on Welcome, nothing saved.
  const overlays = await page.locator('.dp-modal-overlay').count();
  await button.click();
  await expect(wizard(page).locator('.dp-setup-language-view .dp-language-list')).toBeVisible();
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(overlays);
  await wizard(page).locator('.dp-language-option[data-value="ar"]').click();
  await expect(page.locator('html')).toHaveAttribute('lang', 'ar');
  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
  await expect(wizard(page).locator('.dp-setup-welcome')).toBeVisible();
  await expect(button).toHaveAttribute('data-language', 'ar');
  await expect(button).toBeFocused();
  await expect(wizard(page).locator('[data-modal-accept]')).toHaveText(await t(page, 'setup.action.next'));
  await expect(wizard(page)).toBeVisible();
  expect(await setLocale(page, 'en')).toBe('en');
  // Nothing the wizard could write was sent (the page's own engine probe is not the wizard's).
  const WIZARD_WRITES = /^(PUT|POST|DELETE) \/api\/(setup\/|settings\/default-locale|settings$|auth\/deployment-type|account\/locale)/;
  expect(sent.filter(entry => WIZARD_WRITES.test(entry))).toEqual([]);
});

test('W03: at 320px the rerun and My Account\'s section fit, in English and en-XA', async ({page}) => {
  await load(page, {width: 320, height: 640});
  for (const locale of ['en', 'en-XA']) {
    expect(await setLocale(page, locale)).toBe(locale);
    await openAccount(page);
    const section = account(page).locator('.dp-account-setup');
    await section.scrollIntoViewIfNeeded();
    const fits = await section.evaluate(node => [...node.children].every(child => child.scrollWidth <= child.clientWidth + 1
      && child.getBoundingClientRect().right <= node.getBoundingClientRect().right + 1));
    expect(fits).toBe(true);
    await section.locator('[data-account-setup-rerun]').click();
    await expect(wizard(page)).toBeVisible();
    const box = await wizard(page).evaluate(node => {
      const dialog = node.getBoundingClientRect();
      const overflowing = [...node.querySelectorAll('.dp-modal-header > *, .dp-modal-footer > *, .dp-setup-page *')]
        .filter(child => child.getClientRects().length && child.getBoundingClientRect().right > dialog.right + 1).length;
      return {left: dialog.left, right: dialog.right, overflowing, pageScroll: document.documentElement.scrollWidth};
    });
    expect(box.left).toBeGreaterThanOrEqual(0);
    expect(box.right).toBeLessThanOrEqual(320);
    expect(box.overflowing).toBe(0);
    expect(box.pageScroll).toBeLessThanOrEqual(320);
    await wizard(page).locator('[data-modal-cancel]').click();
    await expect(wizard(page)).toHaveCount(0);
    await account(page).locator('[data-modal-cancel]').click();
    await expect(account(page)).toHaveCount(0);
  }
  expect(await setLocale(page, 'en')).toBe('en');
});

test('W04: Open Users & Access reaches the visible Settings tab; an unsaved My Account edit is kept or discarded only by the operator\'s choice', async ({page}) => {
  await load(page);
  await page.evaluate(() => window.debridPulseAuth.fetch('/api/setup/rerun', {method: 'DELETE'}));
  expect(await page.evaluate(() => window.debridPulseAuth.authority.deployment_type)).toBe('server');
  const savedName = (await page.evaluate(() => window.debridPulseAuth.fetch('/api/account').then(r => r.json()))).display_name;
  await openAccount(page);
  const name = account(page).locator('[data-field="display_name"]');
  await name.fill(`${await name.inputValue()} (draft)`);
  const draft = await name.inputValue();
  await account(page).locator('[data-account-setup-rerun]').click();
  await expect(wizard(page)).toBeVisible();
  await wizard(page).locator('[data-modal-accept]').click();
  await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
  await wizard(page).locator('[data-modal-accept]').click();
  const open = wizard(page).locator('[data-setup-users]');
  await expect(open).toBeVisible();

  // Keep Editing: nothing moves -- the wizard, My Account and its edit stay.
  await open.click();
  const guard = page.locator('.dp-modal-dialog[role="alertdialog"]');
  await expect(guard).toBeVisible();
  await expect(guard.locator('.dp-modal-title')).toHaveText(await t(page, 'account.leave.title'));
  await guard.locator('[data-modal-cancel]').click();
  await expect(guard).toHaveCount(0);
  await expect(wizard(page)).toBeVisible();
  await expect(account(page)).toBeVisible();
  await expect(name).toHaveValue(draft);
  await expect(page.locator('#view-settings')).not.toHaveClass(/\bactive\b/);

  // Discard Changes: both dialogs give way and Users & Access is the visible page.
  await open.click();
  await expect(guard).toBeVisible();
  await guard.locator('[data-modal-accept]').click();
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  const tab = page.locator('#view-settings .dp-settings-tabs [data-tab="users"]:not(.dp-motion-still *)');
  await expect(tab).toHaveAttribute('aria-selected', 'true');
  const panel = page.locator('#view-settings [data-panel="users"]');
  await expect(panel).toBeVisible();
  const unobscured = await panel.evaluate(node => {
    const box = node.getBoundingClientRect();
    const x = box.left + Math.min(box.width / 2, 200), y = box.top + Math.min(box.height / 2, 120);
    const hit = document.elementFromPoint(x, y);
    return Boolean(hit && node.contains(hit));
  });
  expect(unobscured).toBe(true);
  // Discarded, not saved. The rerun's record still says where it stopped...
  expect((await page.evaluate(() => window.debridPulseAuth.fetch('/api/account').then(r => r.json()))).display_name).toBe(savedName);
  expect((await state(page)).rerun).toEqual({phase: 'administrator'});
  // ...but an explicit Rerun Setup Wizard starts again at Welcome, page 1 of 5, nothing completed.
  await openAccount(page);
  await account(page).locator('[data-account-setup-rerun]').click();
  await expect(wizard(page).locator('.dp-setup-welcome')).toBeVisible();
  await step(page, 1, 5);
  await expect(wizard(page).locator('.dp-setup-progress-step[data-state="done"]')).toHaveCount(0);
  expect((await state(page)).rerun).toEqual({phase: 'welcome'});
  await wizard(page).locator('[data-modal-cancel]').click();
  await account(page).locator('[data-modal-cancel]').click();
  await page.evaluate(() => window.debridPulseAuth.fetch('/api/setup/rerun', {method: 'DELETE'}));
});

/* The server's setup record and deployment type, emulated in this browser so
 * a new installation's setup can go Back and change type without changing the
 * shared runtime (a mock of the server's facts, labelled as such; WS1's own
 * rules are proven by backend/tests/test_setup_lifecycle.py and a real-backend
 * probe in the Gate 9 packet). It answers /app.js with its deployment type and
 * records every setup write the wizard sends. */
async function emulateNewInstallation(page, {deployment, phase}) {
  // `interruptPhase`: how many phase writes are lost in transit (aborted, never reaching the record).
  const server = {deployment, state: {initial: {complete: false, phase, administrator: 'none'}, rerun: null}, writes: [],
    interruptPhase: 0};
  await page.route(url => url.pathname === '/app.js', async route => {
    const body = (await (await route.fetch()).text()).replace(/^window\.debridPulseAuthority=Object\.freeze\((.*)\);$/m,
      (_, json) => `window.debridPulseAuthority=Object.freeze(${JSON.stringify({...JSON.parse(json), deployment_type: server.deployment})});`);
    await route.fulfill({status: 200, contentType: 'application/javascript', body});
  });
  await page.route(url => url.pathname === '/api/setup/state', route => route.fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify(server.state)}));
  await page.route(url => url.pathname === '/api/setup/initial/phase', route => {
    const {phase: next} = route.request().postDataJSON();
    if (server.interruptPhase > 0) {
      server.interruptPhase -= 1;
      server.writes.push(`phase ${next} (lost)`);
      return route.abort('failed');
    }
    server.writes.push(`phase ${next}`);
    server.state.initial.phase = next;
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(server.state)});
  });
  await page.route(url => url.pathname === '/api/auth/deployment-type', route => {
    const {deployment_type: type} = route.request().postDataJSON();
    server.writes.push(`deployment ${type}`);
    server.deployment = type;
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true, deployment_type: type})});
  });
  return server;
}

// Page `current` of `total`: the Connected Page Progress Indicator's bubble count and current bubble.
async function step(page, current, total) {
  const steps = wizard(page).locator('.dp-setup-progress-step');
  await expect(steps).toHaveCount(total);
  await expect(steps.nth(current - 1)).toHaveAttribute('aria-current', 'step');
}

test('W05: Standalone at Getting Started, Back to Deployment, Server: resumes at Download Directory and then requires Administrator Setup (emulated record)', async ({page}) => {
  const server = await emulateNewInstallation(page, {deployment: 'standalone', phase: 'getting_started'});
  await load(page);
  await expect(wizard(page).locator('.dp-setup-getting-started')).toBeVisible();
  await step(page, 5, 5);
  for (let i = 0; i < 3; i += 1) await wizard(page).locator('[data-modal-action="back"]').click();
  await expect(wizard(page).locator('.dp-setup-deployment')).toBeVisible();
  await wizard(page).locator('[data-deployment="server"]').click();
  await Promise.all([page.waitForEvent('load'), wizard(page).locator('[data-modal-accept]').click()]);
  expect(server.writes).toEqual(['deployment server', 'phase download_directory']);
  await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
  await step(page, 3, 6);
  await wizard(page).locator('[data-modal-accept]').click();
  await expect(wizard(page).locator('[data-setup-field="username"]')).toBeVisible();
  await step(page, 4, 6);
  expect(server.writes).toEqual(['deployment server', 'phase download_directory', 'phase administrator']);
});

test('W06: Server at Administrator Setup (none created), Back to Deployment, Standalone: resumes at Download Directory, not Welcome (emulated record)', async ({page}) => {
  const server = await emulateNewInstallation(page, {deployment: 'server', phase: 'administrator'});
  await load(page);
  await expect(wizard(page).locator('[data-setup-field="username"]')).toBeVisible();
  await step(page, 4, 6);
  for (let i = 0; i < 2; i += 1) await wizard(page).locator('[data-modal-action="back"]').click();
  await wizard(page).locator('[data-deployment="standalone"]').click();
  await Promise.all([page.waitForEvent('load'), wizard(page).locator('[data-modal-accept]').click()]);
  expect(server.writes).toEqual(['deployment standalone', 'phase download_directory']);
  await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
  await step(page, 3, 5);
});

/* The other mutations the recovery must never send. */
function forbiddenWrites(page) {
  const sent = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (request.method() !== 'GET' && /^\/api\/(setup\/initial\/administrator|setup\/initial\/finish|settings$|settings\/default-locale)/.test(path)) {
      sent.push(`${request.method()} ${path}`);
    }
  });
  return sent;
}

test('W07: Standalone at Getting Started, changed to Server, its phase write lost, reloaded: resumes at Download Directory, then Administrator Setup (emulated record)', async ({page}) => {
  const server = await emulateNewInstallation(page, {deployment: 'standalone', phase: 'getting_started'});
  const forbidden = forbiddenWrites(page);
  await load(page);
  for (let i = 0; i < 3; i += 1) await wizard(page).locator('[data-modal-action="back"]').click();
  await wizard(page).locator('[data-deployment="server"]').click();
  server.interruptPhase = 1;
  await wizard(page).locator('[data-modal-accept]').click();
  await expect(wizard(page).locator('.dp-setup-error')).toBeVisible();
  expect(server.writes).toEqual(['deployment server', 'phase download_directory (lost)']);
  expect(server.state.initial.phase).toBe('getting_started');   // the old roster's page is what the record holds
  await page.reload();
  await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
  await step(page, 3, 6);
  await expect.poll(() => server.state.initial.phase).toBe('download_directory');
  await wizard(page).locator('[data-modal-accept]').click();
  await expect(wizard(page).locator('[data-setup-field="username"]')).toBeVisible();
  await step(page, 4, 6);
  // One deployment change only; the recovery writes only the resume page.
  expect(server.writes).toEqual(['deployment server', 'phase download_directory (lost)', 'phase download_directory', 'phase administrator']);
  expect(forbidden).toEqual([]);
});

test('W08: Server at Administrator Setup (none created), changed to Standalone, its phase write lost, reloaded: resumes at Download Directory, not Welcome (emulated record)', async ({page}) => {
  const server = await emulateNewInstallation(page, {deployment: 'server', phase: 'administrator'});
  const forbidden = forbiddenWrites(page);
  await load(page);
  for (let i = 0; i < 2; i += 1) await wizard(page).locator('[data-modal-action="back"]').click();
  await wizard(page).locator('[data-deployment="standalone"]').click();
  server.interruptPhase = 1;
  await wizard(page).locator('[data-modal-accept]').click();
  await expect(wizard(page).locator('.dp-setup-error')).toBeVisible();
  expect(server.state.initial.phase).toBe('administrator');
  await page.reload();
  await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
  await step(page, 3, 5);
  await expect.poll(() => server.state.initial.phase).toBe('download_directory');
  expect(server.writes).toEqual(['deployment standalone', 'phase download_directory (lost)', 'phase download_directory']);
  expect(forbidden).toEqual([]);
});

test('W09: the phase write lost in place, Next again: only the record is retried, never the deployment change (emulated record)', async ({page}) => {
  const server = await emulateNewInstallation(page, {deployment: 'standalone', phase: 'getting_started'});
  await load(page);
  for (let i = 0; i < 3; i += 1) await wizard(page).locator('[data-modal-action="back"]').click();
  await wizard(page).locator('[data-deployment="server"]').click();
  server.interruptPhase = 1;
  await wizard(page).locator('[data-modal-accept]').click();
  await expect(wizard(page).locator('.dp-setup-error')).toBeVisible();
  await Promise.all([page.waitForEvent('load'), wizard(page).locator('[data-modal-accept]').click()]);
  await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
  await step(page, 3, 6);
  expect(server.writes).toEqual(['deployment server', 'phase download_directory (lost)', 'phase download_directory']);
});

/* TEMPORARY, development only (WS3b): Test First-Run Wizard, on the rerun's
 * Welcome. It previews every first-run page of BOTH deployment types with the
 * wizard's own pages and commits nothing: no language, deployment, folder,
 * Administrator, phase or completion. Leaving it -- Finish or Close --
 * returns to the rerun. Removed with this control at the final cleanup. */
test('W10: Test First-Run Wizard previews the first-run pages of Standalone and Server, writes nothing, and returns to the rerun', async ({page}) => {
  await load(page);
  await page.evaluate(() => window.debridPulseAuth.fetch('/api/setup/rerun', {method: 'DELETE'}));
  const deployment = await page.evaluate(() => window.debridPulseAuth.authority.deployment_type);
  const folder = (await page.evaluate(() => window.debridPulseAuth.fetch('/api/settings').then(response => response.json()))).download_folder;
  await openAccount(page);
  await account(page).locator('[data-account-setup-rerun]').click();
  await expect(wizard(page)).toBeVisible();
  const entry = wizard(page).locator('[data-setup-preview]');
  await expect(entry).toBeVisible();
  const overlays = await page.locator('.dp-modal-overlay').count();
  const writes = [];
  // The page's own aria2 reachability probe (a POST that writes nothing) aside.
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (request.method() !== 'GET' && path !== '/api/settings/test-aria2') writes.push(`${request.method()} ${path}`);
  });

  const accept = () => wizard(page).locator('[data-modal-accept]').click();
  const title = async key => expect(wizard(page).locator('.dp-modal-title')).toHaveText(await t(page, key));
  async function walk(type) {
    await entry.click();
    await expect(wizard(page).locator('.dp-setup-preview-note')).toBeVisible();
    await expect(page.locator('.dp-modal-overlay')).toHaveCount(overlays);      // the same one dialog layer
    await expect(wizard(page).locator('[data-setup-preview]')).toHaveCount(0);  // not offered inside itself
    await expect(wizard(page).locator('[data-modal-cancel]')).toBeVisible();    // a preview may be left
    const total = type === 'server' ? 6 : 5;
    await title('setup.welcome.title');
    await step(page, 1, deployment === 'server' ? 6 : 5);
    await accept();
    await title('setup.deployment.title');
    await wizard(page).locator(`[data-deployment="${type}"]`).click();
    await accept();
    await title('setup.directory.title');
    await step(page, 3, total);
    await wizard(page).locator('#dp-setup-download-folder').fill('/synthetic/preview-only');
    await accept();
    if (type === 'server') {
      await title('setup.administrator.title');
      await wizard(page).locator('[data-setup-field="username"]').fill('synthetic-preview');
      await wizard(page).locator('[data-setup-field="password"]').fill('synthetic-preview-pass');
      await wizard(page).locator('[data-setup-field="confirm"]').fill('synthetic-preview-pass');
      await accept();
    }
    await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
    await step(page, total - 1, total);
    await accept();
    await title('setup.getting_started.title');
    // The preview creates no Administrator, so its Finish signs nobody in: no note (initial Server's own: R10).
    await expect(wizard(page).locator('[data-i18n="setup.getting_started.sign_in"]')).toHaveCount(0);
    await expect(wizard(page).locator('[data-modal-accept]')).toHaveText(await t(page, 'setup.action.finish'));
  }

  await walk('standalone');
  await accept();                                                                // Finish ends the preview
  await expect(wizard(page).locator('[data-setup-preview]')).toBeVisible();      // back at the rerun's Welcome
  await walk('server');
  await wizard(page).locator('[data-modal-cancel]').click();                    // Close ends it too
  await expect(wizard(page).locator('[data-setup-preview]')).toBeVisible();

  expect(writes).toEqual([]);
  const after = await state(page);
  expect(after.initial.complete).toBe(true);
  expect(after.rerun).toEqual({phase: 'welcome'});
  expect(await page.evaluate(() => window.debridPulseAuth.authority.deployment_type)).toBe(deployment);
  expect((await page.evaluate(() => window.debridPulseAuth.fetch('/api/settings').then(response => response.json()))).download_folder).toBe(folder);
  await wizard(page).locator('[data-modal-cancel]').click();
  await page.evaluate(() => window.debridPulseAuth.fetch('/api/setup/rerun', {method: 'DELETE'}));
});

/* TEMPORARY (WS3b): the preview is offered only where the server says this is
 * a development build -- its own release identity, the running VERSION's
 * CHANGELOG entry titled Development (core.version.is_development_build,
 * proven in backend/tests/test_setup_lifecycle.py). A released build is shown
 * here by answering the one setup-state read with `development_build: false`
 * (a labelled mock of that one server fact). */
test('W11: Test First-Run Wizard is offered only to a development build', async ({page}) => {
  await load(page);
  await page.evaluate(() => window.debridPulseAuth.fetch('/api/setup/rerun', {method: 'DELETE'}));
  // This source tree is a development build: the server says so itself.
  expect((await state(page)).development_build).toBe(true);
  let released = true;
  await page.route(url => url.pathname === '/api/setup/state', async route => {
    const response = await route.fetch();
    const body = await response.json();
    return route.fulfill({response, json: released ? {...body, development_build: false} : body});
  });
  for (const expected of [0, 1]) {
    await openAccount(page);
    await account(page).locator('[data-account-setup-rerun]').click();
    await expect(wizard(page).locator('.dp-setup-welcome')).toBeVisible();
    await expect(wizard(page).locator('[data-setup-preview]')).toHaveCount(expected);
    await wizard(page).locator('[data-modal-cancel]').click();
    await expect(wizard(page)).toHaveCount(0);
    await account(page).locator('[data-modal-cancel]').click();
    released = false;
  }
  await page.evaluate(() => window.debridPulseAuth.fetch('/api/setup/rerun', {method: 'DELETE'}));
});

/* The wizard shell and its Connected Page Progress Indicator.
 *
 * One dialog of one size for the viewport on every page and provider view;
 * the header (progress and heading) and the footer keep their places; only
 * the body scrolls. The indicator is a reading -- bubbles and strings, never
 * controls -- and completed is not "before the current page": a page is
 * completed once its Next succeeded and the wizard moved on, and stays so when
 * the operator goes Back. */

const progress = page => wizard(page).locator('.dp-setup-progress');
async function bubbles(page) {
  return progress(page).locator('.dp-setup-progress-step').evaluateAll(steps => steps.map(item => ({
    state: item.dataset.state, current: item.getAttribute('aria-current') === 'step',
    check: !!item.querySelector('svg[data-dp-lucide="check"]'), number: item.querySelector('.dp-setup-progress-bubble').textContent.trim(),
    link: item.querySelector('.dp-setup-progress-link')?.dataset.link || null,
    label: item.getAttribute('aria-label'),
  })));
}
async function openRerun(page) {
  await page.evaluate(() => window.debridPulseAuth.fetch('/api/setup/rerun', {method: 'DELETE'}));
  await openAccount(page);
  await account(page).locator('[data-account-setup-rerun]').click();
  await expect(wizard(page).locator('.dp-setup-welcome')).toBeVisible();
}
const rect = locator => locator.evaluate(node => { const box = node.getBoundingClientRect(); return [box.left, box.top, box.width, box.height].map(Math.round); });
const next = page => wizard(page).locator('[data-modal-accept]').click();

for (const [width, height] of [[1280, 900], [375, 667]]) {
  test(`W12: one shell at ${width}x${height}: the same dialog, footer and indicator on every page and provider view, the header sized by its reading; only the body scrolls`, async ({page}) => {
    await load(page, {width, height});
    await openRerun(page);
    const dialog = wizard(page);
    // One frame: every rectangle read in the same evaluation.
    const shape = () => dialog.evaluate(node => {
      const rect = element => { const box = element.getBoundingClientRect(); return [box.left, box.top, box.width, box.height].map(Math.round); };
      return {dialog: rect(node), header: rect(node.querySelector('.dp-modal-header')), footer: rect(node.querySelector('.dp-modal-footer')),
        progress: rect(node.querySelector('.dp-setup-progress')), heading: rect(node.querySelector('.dp-modal-heading')),
        reading: Math.round(node.querySelector('.dp-modal-title-hint').getBoundingClientRect().height)};
    });
    // Next, and the next page shown (its title changed) before it is measured.
    const advance = async () => {
      const before = await dialog.locator('.dp-modal-title').textContent();
      await next(page);
      await expect(dialog.locator('.dp-modal-title')).not.toHaveText(before);
    };
    const seen = [await shape()];
    const deployment = await page.evaluate(() => window.debridPulseAuth.authority.deployment_type);
    for (let i = 0; i < (deployment === 'standalone' ? 2 : 3); i += 1) { await advance(); seen.push(await shape()); }
    await expect(dialog.locator('.dp-setup-premium')).toBeVisible();
    for (const id of ['debridlink', 'torbox', 'usenet']) {
      await dialog.locator(`[data-service="${id}"]`).click();
      await expect(dialog.locator('.dp-setup-provider-surface')).toBeVisible();
      seen.push({...await shape(), view: true});
      if (id === 'usenet') {
        for (let n = 0; n < 4; n += 1) await dialog.locator('.dp-setup-provider-surface [data-usenet-action="add"]').click();
        await dialog.locator('.dp-setup-provider-surface [data-usenet-advanced-toggle]').first().click();
        seen.push({...await shape(), view: true});
        // Long content scrolls inside the body; the footer's Save stays reachable.
        const body = await dialog.locator('.dp-modal-body').evaluate(node => ({scrolls: node.scrollHeight > node.clientHeight, overflow: getComputedStyle(node).overflowY}));
        expect(body).toEqual({scrolls: true, overflow: 'auto'});
        await expect(dialog.locator('[data-modal-accept]')).toBeInViewport({ratio: 1});
      }
      await dialog.locator('[data-modal-cancel]').click();
      await expect(dialog.locator('.dp-setup-premium')).toBeVisible();
    }
    await next(page);
    await expect(dialog.locator('.dp-setup-getting-started')).toBeVisible();
    seen.push(await shape());
    for (const key of ['dialog', 'footer', 'progress']) {
      expect(new Set(seen.map(entry => JSON.stringify(entry[key]))).size, key).toBe(1);
    }
    // The header keeps its place and width, and everything in it but the
    // reading keeps its size: the header is its chrome plus its reading's
    // lines (R17), so the one-line taglines share one header and a longer
    // reading (the rerun's Administrator guidance, a provider's) is taller.
    expect(new Set(seen.map(entry => JSON.stringify(entry.header.slice(0, 3)))).size, 'header position').toBe(1);
    expect(new Set(seen.map(entry => entry.header[3] - entry.reading)).size, 'header chrome').toBe(1);
    expect(new Set(seen.map(entry => entry.heading[1])).size, 'heading top').toBe(1);
    const [left, top, w, h] = seen[0].dialog;
    expect(left).toBeGreaterThanOrEqual(0); expect(top).toBeGreaterThanOrEqual(0);
    expect(left + w).toBeLessThanOrEqual(width); expect(top + h).toBeLessThanOrEqual(height);
    expect(h).toBeGreaterThan(w);                                                   // portrait
    // No top close control, no step label.
    await expect(dialog.locator('[data-modal-close], .dp-setup-step')).toHaveCount(0);
    await dialog.locator('[data-modal-cancel]').click();
  });
}

test('W13: the indicator is a reading: no completion at the start, the current page marked, no control in it', async ({page}) => {
  await load(page);
  await openRerun(page);
  const deployment = await page.evaluate(() => window.debridPulseAuth.authority.deployment_type);
  const total = deployment === 'standalone' ? 4 : 5;
  const now = await bubbles(page);
  expect(now.map(item => [item.state, item.current, item.number])).toEqual(
    Array.from({length: total}, (_, index) => ['todo', index === 0, String(index + 1)]));
  expect(now.map(item => item.label)).toEqual(await Promise.all(
    Array.from({length: total}, (_, index) => t(page, 'setup.step', {current: index + 1, total}))));
  await expect(progress(page)).toHaveAttribute('aria-label', await t(page, 'setup.progress.label'));
  await expect(progress(page).locator('button, a, input, [tabindex], [onclick], [role="button"]')).toHaveCount(0);
  // Clicking a later bubble moves nothing.
  await progress(page).locator('.dp-setup-progress-bubble').nth(2).click({force: true});
  expect((await bubbles(page)).findIndex(item => item.current)).toBe(0);
  await wizard(page).locator('[data-modal-cancel]').click();
});

test('W14: a page is completed only once its Next was accepted and the wizard moved on; a refused Next leaves it incomplete', async ({page}) => {
  await load(page);
  await openRerun(page);
  let release;
  const held = new Promise(resolve => { release = resolve; });
  let refuse = false;
  await page.route(url => url.pathname === '/api/setup/rerun', async route => {
    if (route.request().method() !== 'PUT') return route.continue();
    if (refuse) return route.fulfill({status: 500, contentType: 'application/json', body: JSON.stringify({detail: 'synthetic failure', code: 'unclassified', params: {}})});
    await held;
    return route.continue();
  });
  await next(page);
  // Pending: nothing completed, still on the first page.
  await page.waitForTimeout(400);
  expect((await bubbles(page)).map(item => [item.state, item.current]).slice(0, 2)).toEqual([['todo', true], ['todo', false]]);
  release();
  await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
  let now = await bubbles(page);
  expect(now.map(item => item.state).slice(0, 3)).toEqual(['done', 'todo', 'todo']);
  expect(now[0].check && now[0].number === '' && now[1].current).toBe(true);
  expect(now[0].label).toBe(await t(page, 'setup.progress.step_done', {current: 1, total: now.length}));
  // Strings follow completion: the boundary from the completed page into the next, unfinished beyond.
  expect([now[1].link, now[2].link]).toEqual(['boundary', 'todo']);
  // A refused Next: the error, the same page, still incomplete.
  refuse = true;
  await next(page);
  await expect(wizard(page).locator('.dp-setup-error')).toBeVisible();
  now = await bubbles(page);
  expect(now.map(item => item.state).slice(0, 3)).toEqual(['done', 'todo', 'todo']);
  expect(now[1].current).toBe(true);
  await page.unroute(url => url.pathname === '/api/setup/rerun');
  await wizard(page).locator('[data-modal-cancel]').click();
});

test('W15: Back keeps every completed page: the page viewed again keeps its check under the current glow', async ({page}) => {
  await load(page);
  await openRerun(page);
  await next(page);
  await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
  await next(page);
  await expect(wizard(page).locator('.dp-setup-administrator, .dp-setup-premium')).toBeVisible();
  await wizard(page).locator('[data-modal-action="back"]').click();
  await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
  const now = await bubbles(page);
  expect(now.map(item => [item.state, item.current, item.check]).slice(0, 3)).toEqual([['done', false, true], ['done', true, true], ['todo', false, false]]);
  // Strings: completed green between the two completed pages; the boundary where completion ends.
  expect([now[1].link, now[2].link]).toEqual(['done', 'boundary']);
  // The current glow is the strong one, and differs from a completed page's normal glow.
  const glow = await progress(page).locator('.dp-setup-progress-bubble').evaluateAll(nodes => nodes.map(node => getComputedStyle(node).boxShadow));
  expect(glow[1]).not.toBe(glow[0]);                                                // its hue and strength: W19
  // Back again, to the first page: the glow moves, the boundary does not.
  await wizard(page).locator('[data-modal-action="back"]').click();
  await expect(wizard(page).locator('.dp-setup-welcome')).toBeVisible();
  const first = await bubbles(page);
  expect(first.map(item => [item.state, item.current]).slice(0, 3)).toEqual([['done', true], ['done', false], ['todo', false]]);
  expect([first[1].link, first[2].link]).toEqual(['done', 'boundary']);
  await wizard(page).locator('[data-modal-cancel]').click();
});

test('W16: an optional page is completed by its Next alone; a provider view changes no progress and does not complete Premium Services', async ({page}) => {
  await load(page);
  await openRerun(page);
  const deployment = await page.evaluate(() => window.debridPulseAuth.authority.deployment_type);
  for (let i = 0; i < (deployment === 'standalone' ? 2 : 3); i += 1) await next(page);
  await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
  const atPremium = await bubbles(page);
  const premium = atPremium.findIndex(item => item.current);
  await wizard(page).locator('[data-service="realdebrid"]').click();
  await expect(wizard(page).locator('.dp-setup-provider-surface')).toBeVisible();
  expect(await bubbles(page)).toEqual(atPremium);
  await wizard(page).locator('[data-modal-accept]').click();                       // its Save (nothing to store)
  await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
  expect(await bubbles(page)).toEqual(atPremium);
  expect(atPremium[premium].state).toBe('todo');
  await next(page);                                                                 // nothing configured
  await expect(wizard(page).locator('.dp-setup-getting-started')).toBeVisible();
  expect((await bubbles(page))[premium].state).toBe('done');
  await wizard(page).locator('[data-modal-cancel]').click();
});

test('W17: the preview completes its pages for itself, writing nothing', async ({page}) => {
  await load(page);
  await openRerun(page);
  const writes = [];
  page.on('request', request => {
    const path = new URL(request.url()).pathname;
    if (request.method() !== 'GET' && path !== '/api/settings/test-aria2') writes.push(`${request.method()} ${path}`);
  });
  await wizard(page).locator('[data-setup-preview]').click();
  await expect(wizard(page).locator('.dp-setup-preview-note')).toBeVisible();
  expect((await bubbles(page)).every(item => item.state === 'todo')).toBe(true);
  await next(page);
  await wizard(page).locator('[data-deployment="server"]').click();
  await next(page);
  await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
  const now = await bubbles(page);
  expect(now.length).toBe(6);
  expect(now.map(item => item.state)).toEqual(['done', 'done', 'todo', 'todo', 'todo', 'todo']);
  expect(writes).toEqual([]);
  await wizard(page).locator('[data-modal-cancel]').click();
  await expect(wizard(page).locator('[data-setup-preview]')).toBeVisible();
  expect((await bubbles(page)).every(item => item.state === 'todo')).toBe(true);   // the rerun's own progress, untouched
  expect((await state(page)).rerun).toEqual({phase: 'welcome'});
  await wizard(page).locator('[data-modal-cancel]').click();
});

test('W18: an interrupted initial setup resumes with the progress its record holds (emulated record)', async ({page}) => {
  const server = await emulateNewInstallation(page, {deployment: 'standalone', phase: 'premium_services'});
  await load(page);
  await expect(wizard(page).locator('.dp-setup-premium')).toBeVisible();
  const now = await bubbles(page);
  expect(now.map(item => [item.state, item.current])).toEqual([['done', false], ['done', false], ['done', false], ['todo', true], ['todo', false]]);
  await wizard(page).locator('[data-modal-action="back"]').click();
  await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
  expect((await bubbles(page)).map(item => [item.state, item.current])).toEqual([['done', false], ['done', false], ['done', true], ['todo', false], ['todo', false]]);
  expect(server.writes).toEqual([]);
});

/* The indicator as rendered, in both themes, at desktop and phone widths,
 * anchored to the theme's own colour authorities -- the accent (`--accent`)
 * and the completion green (`--dp-state-success`), each resolved in the page
 * and normalised to RGBA by drawing it -- never to the indicator's own
 * variables: every string meets its two bubbles' borders; completed bubbles
 * and strings are the green; incomplete bubbles and strings are the accent's
 * hue (at reduced intensity, never grey); the boundary string runs from the
 * green into that accent; the current bubble's strong glow is its own state's
 * colour -- green around a completed page, the accent around an incomplete
 * one -- while the others keep their normal glow; and Back moves the glow,
 * never the boundary. */
const INDICATOR = () => {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 1;
  const context = canvas.getContext('2d', {willReadFrequently: true});
  const rgba = color => {
    context.clearRect(0, 0, 1, 1);
    context.fillStyle = '#000'; context.fillStyle = color;
    context.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = context.getImageData(0, 0, 1, 1).data;
    return [r, g, b, Math.round(a / 2.55) / 100];
  };
  const colours = text => (String(text).match(/(?:rgba?|color|oklab|oklch|lab|lch|hsla?)\([^()]*\)|#[0-9a-f]{3,8}\b/gi) || []).map(rgba);
  const dialog = document.querySelector('.dp-setup-dialog');
  const probe = (property, value) => {
    const node = document.createElement('span');
    node.style.setProperty(property, value);
    dialog.append(node);
    const read = getComputedStyle(node).getPropertyValue(property);
    node.remove();
    return read;
  };
  const tokens = {accent: rgba(probe('color', 'var(--accent)')), green: rgba(probe('color', 'var(--dp-state-success)')),
    normalGreenGlow: probe('box-shadow', 'var(--dp-glow-success)')};
  const steps = [...document.querySelectorAll('.dp-setup-progress-step')].map(step => {
    const bubble = step.querySelector('.dp-setup-progress-bubble'), link = step.querySelector('.dp-setup-progress-link');
    const b = bubble.getBoundingClientRect(), l = link?.getBoundingClientRect();
    const style = getComputedStyle(bubble), linkStyle = link && getComputedStyle(link);
    return {state: step.dataset.state, current: step.getAttribute('aria-current') === 'step', kind: link?.dataset.link || null,
      bubble: {left: b.left, right: b.right, mid: b.top + b.height / 2, border: rgba(style.borderTopColor),
        shadow: style.boxShadow, glow: colours(style.boxShadow)},
      link: l && {left: l.left, right: l.right, mid: l.top + l.height / 2, colour: rgba(linkStyle.backgroundColor),
        stops: linkStyle.backgroundImage === 'none' ? [] : colours(linkStyle.backgroundImage)}};
  });
  return {tokens, steps};
};
const hue = (colour, of) => [0, 1, 2].every(channel => Math.abs(colour[channel] - of[channel]) <= 3);

for (const [width, height] of [[1280, 900], [375, 667]]) {
  for (const theme of ['dark', 'light']) {
    test(`W19: completion green, the theme accent and state-coloured glows, joined (${theme}, ${width}px)`, async ({page}) => {
      await load(page, {width, height});
      await page.evaluate(light => document.body.classList.toggle('light', light), theme === 'light');
      await openRerun(page);
      await next(page);
      await expect(wizard(page).locator('.dp-setup-directory')).toBeVisible();
      await next(page);
      await expect(wizard(page).locator('.dp-setup-administrator, .dp-setup-premium')).toBeVisible();
      const check = async expectedCurrent => {
        const {tokens, steps} = await page.evaluate(INDICATOR);
        const {accent, green} = tokens;
        expect(hue(accent, green), 'the accent and the green are distinct colours').toBe(false);
        expect(steps.findIndex(step => step.current)).toBe(expectedCurrent);
        steps.forEach((step, index) => {
          const {bubble} = step;
          if (step.state === 'done') expect(bubble.border, `bubble ${index}`).toEqual([...green.slice(0, 3), 1]);
          else expect(hue(bubble.border, accent) && bubble.border[3] >= .5, `bubble ${index} has the accent's hue: ${bubble.border}`).toBe(true);
          // Glow: the current bubble's is its own state's colour, the strong one; others keep their normal glow.
          if (step.current) {
            const own = step.state === 'done' ? green : accent, other = step.state === 'done' ? accent : green;
            expect(bubble.glow.length).toBeGreaterThanOrEqual(2);
            expect(bubble.glow.every(colour => hue(colour, own)), `current glow ${bubble.shadow}`).toBe(true);
            expect(bubble.glow.some(colour => hue(colour, other))).toBe(false);
          } else if (step.state === 'done') {
            expect(bubble.shadow).toBe(tokens.normalGreenGlow);
            expect(bubble.glow.some(colour => hue(colour, accent))).toBe(false);
          } else {
            expect(bubble.shadow).toBe('none');
          }
          if (index === 0) return;
          const {link} = step;
          // Joined: no seam at either border, on the bubbles' centre line.
          expect(Math.abs(link.left - steps[index - 1].bubble.right), `seam before ${index}`).toBeLessThan(0.75);
          expect(Math.abs(link.right - bubble.left), `seam after ${index}`).toBeLessThan(0.75);
          expect(Math.abs(link.mid - bubble.mid)).toBeLessThan(1);
          const kind = steps[index - 1].state !== 'done' ? 'todo' : step.state === 'done' ? 'done' : 'boundary';
          expect(step.kind).toBe(kind);
          if (kind === 'done') expect(link.colour).toEqual([...green.slice(0, 3), 1]);
          if (kind === 'todo') {
            expect(hue(link.colour, accent), `string ${index} has the accent's hue: ${link.colour}`).toBe(true);
            expect(link.colour).toEqual(bubble.border);                      // the same unfinished accent as the bubble
          }
          if (kind === 'boundary') {
            expect(link.stops.length).toBe(2);
            expect(link.stops[0]).toEqual([...green.slice(0, 3), 1]);           // starts at the completion green
            expect(hue(link.stops[1], accent), `ends at the accent: ${link.stops[1]}`).toBe(true);
            expect(link.stops[1]).toEqual(bubble.border);                    // ... the incomplete bubble's own border
          }
        });
        return steps;
      };
      const forward = await check(2);
      const boundary = forward.findIndex(step => step.kind === 'boundary');
      expect(boundary).toBe(2);
      for (const back of [1, 0]) {
        await wizard(page).locator('[data-modal-action="back"]').click();
        const steps = await check(back);
        expect(steps.findIndex(step => step.kind === 'boundary')).toBe(boundary);
      }
      await wizard(page).locator('[data-modal-cancel]').click();
    });
  }
}
