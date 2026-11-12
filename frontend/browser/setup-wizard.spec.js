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

async function load(page, {width = 1280, height = 900} = {}) {
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
  await expect(wizard(page).locator('.dp-setup-step')).toHaveText(await t(page, 'setup.step', {current: 1, total}));
  await expect(wizard(page).locator('[data-modal-cancel]')).toBeVisible();      // a rerun may be closed
  await expect(wizard(page).locator('[data-modal-action="back"]')).toBeHidden();
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
  await expect(wizard(page).locator('.dp-setup-step')).toHaveText(await t(page, 'setup.step', {current: 1, total}));
  await expect(wizard(page).locator('[data-modal-cancel]')).toBeHidden();
  await expect(wizard(page).locator('[data-modal-action="back"]')).toBeHidden();
  await expect(wizard(page).locator('[data-modal-accept]')).toHaveText(await t(page, 'setup.action.get_started'));
  await page.keyboard.press('Escape');
  await expect(wizard(page)).toBeVisible();

  // A language switch while it is open: words change in place, the dialog and focus stay.
  const row = wizard(page).locator('.dp-language-option[aria-checked="true"]');
  await row.focus();
  const englishTitle = await wizard(page).locator('.dp-modal-title').textContent();
  expect(await setLocale(page, 'en-XA')).toBe('en-XA');
  await expect(wizard(page).locator('.dp-modal-title')).toHaveText(await t(page, 'setup.welcome.title'));
  expect(await wizard(page).locator('.dp-modal-title').textContent()).not.toBe(englishTitle);
  await expect(row).toBeFocused();
  // Arabic through the wizard's own preview (a row of its language list): right to left, nothing saved.
  await wizard(page).locator('.dp-language-option[data-value="ar"]').click();
  await expect(page.locator('html')).toHaveAttribute('lang', 'ar');
  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
  await expect(wizard(page).locator('.dp-language-option[data-value="ar"]')).toHaveAttribute('aria-checked', 'true');
  await expect(wizard(page).locator('[data-modal-accept]')).toHaveText(await t(page, 'setup.action.get_started'));
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
  // Discarded, not saved; and the rerun is kept to resume from My Account.
  expect((await page.evaluate(() => window.debridPulseAuth.fetch('/api/account').then(r => r.json()))).display_name).toBe(savedName);
  expect((await state(page)).rerun).toEqual({phase: 'administrator'});
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

const step = async (page, current, total) => expect(wizard(page).locator('.dp-setup-step')).toHaveText(await t(page, 'setup.step', {current, total}));

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
