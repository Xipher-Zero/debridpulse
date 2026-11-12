const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Easy / Advanced presentation, Pass 1.
 *
 * Each human account owns one presentation, Advanced until it chooses: My
 * Account's Easy | Advanced, saved to the account (PUT
 * /api/account/presentation) and stated with the application bundle before
 * the interface is composed. Easy omits, in either deployment type, exactly
 * the parts its Easy list names -- Activity Log; the five premium services'
 * Additional Settings; Download Behavior's Advanced Settings, Transfer Method
 * Settings, Disk Space & Recovery and Download Engine Activity; Username &
 * Password and OpenID Connect; Deployment Type; Statistics Reporting; Event
 * Logging, the Additional Backup & Retention Settings and Database Reset
 * Controls -- from what role and deployment type already allow, live, with
 * no reload and every unsaved edit kept.
 *
 * Admin cases run on the open runtime, whose every request is the default
 * Admin; its choice is shared by every browser of that runtime, so each case
 * returns it to Advanced. Standalone is driven through the server's own reads
 * answered as standalone, as in standalone-presentation.spec.js. User and
 * ReadOnly cases run on the password-auth runtime as synthetic accounts this
 * file creates. Synthetic values only. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const EVIDENCE = process.env.DP_EVIDENCE_DIR || '';
const ADMIN = {username: 'browser', password: 'browser-secret'};
const run = Date.now().toString(36);
const adminSession = require('./acl-admin-session');
const PREMIUM = ['alldebrid', 'debridlink', 'premiumize', 'realdebrid', 'torbox'];
const SECTION_KEYS = ['sources', 'downloads', 'extraction', 'authentication', 'users', 'notifications', 'maintenance'];

async function isolateFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

const api = (page, method, path, body) => page.evaluate(async ([method, path, body]) => {
  const response = await window.debridPulseAuth.fetch(`/api${path}`, {
    method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  return {status: response.status, body: await response.json().catch(() => null)};
}, [method, path, body]);

const dialog = page => page.locator('.dp-modal-dialog.dp-account-dialog');
const option = (page, mode) => dialog(page).locator(`[data-presentation-mode="${mode}"]`);

async function openAccount(page) {
  const handheld = (page.viewportSize()?.width || 1440) < 780;
  if (handheld) {
    const button = page.locator('#topbar-account-button');
    await button.click();
    if (await button.getAttribute('data-session') === 'authenticated') {
      await page.locator('#topbar-account-sheet [data-account-action="profile"]').click();
    }
  } else {
    await page.locator('#sidebar-auth-row .dp-sidebar-profile').click();
  }
  await expect(dialog(page)).toBeVisible();
  await expect(dialog(page).locator('.dp-presentation-switch')).toHaveAttribute('data-state', /saved|unavailable/);
}

async function choose(page, mode) {
  await option(page, mode).click();
  await expect(dialog(page).locator('.dp-presentation-switch')).toHaveAttribute('data-state', 'saved');
  await expect(option(page, mode)).toHaveAttribute('aria-checked', 'true');
}

async function closeAccount(page) {
  await dialog(page).locator('[data-modal-cancel]').click();
  await expect(dialog(page)).toHaveCount(0);
}

async function boot(page, width = 1440) {
  await page.setViewportSize({width, height: width < 780 ? 760 : 900});
  await isolateFonts(page);
  await page.goto('/');
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
}

async function openSettings(page) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').evaluate(element => element.click());
  await expect(page.locator('#view-settings .dp-settings-tabs')).toBeAttached();
}

async function showSection(page, key) {
  await page.locator(`#view-settings .dp-settings-tabs [data-tab="${key}"]:not(.dp-motion-still *)`)
    .evaluate(element => element.click());
  await expect(page.locator(`#view-settings [data-panel="${key}"]`)).toBeVisible();
}

const sidebarViews = page => page.locator('#sidebar .nav-item[data-view]').evaluateAll(items => items.map(item => item.dataset.view));
const dockViews = page => page.locator('#shell-dock .dp-shell-dock-item:not(.dp-shell-dock-more)')
  .evaluateAll(items => items.map(item => item.dataset.view));
const moreViews = page => page.locator('#shell-more-list .dp-shell-sheet-item').evaluateAll(items => items.map(item => item.dataset.view));
// A panel's own cards, by title, in order (never a group's member cards).
const panelCards = (page, key) => page.locator(`#view-settings [data-panel="${key}"] .card-title, `
  + `#view-settings [data-panel="${key}"] .dp-settings-card-title-text`).evaluateAll(nodes => [...new Set(nodes
  .filter(node => !node.closest('.dp-settings-source-group .dp-settings-source-group, .dp-executor-tuning-group .card .card'))
  .map(node => node.textContent.trim()).filter(Boolean))]);

// The open runtime's one account goes back to Advanced after every case.
test.afterEach(async ({page}) => {
  if (!page.isClosed() && page.url().startsWith('http') && !page.url().startsWith(AUTH || '\u0000')) {
    await api(page, 'PUT', '/account/presentation', {presentation_mode: 'advanced'}).catch(() => {});
  }
});

test('E03/E18: My Account carries one contiguous, right-aligned Easy | Advanced control, accent-chosen, at 1440 and 320, dark and light', async ({page}) => {
  // A Standalone installation's profile: its password hint stays under the title.
  await page.route(url => url.pathname === '/api/account', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    const real = await (await route.fetch()).json();
    return route.fulfill({json: {...real, deployment_type: 'standalone', sign_in_required: false}});
  });
  for (const [width, mode] of [[1440, 'dark'], [320, 'light']]) {
    await boot(page, width);
    await page.evaluate(light => window.DPTheme.setMode(light), mode === 'light');
    await openAccount(page);
    const group = dialog(page).locator('.dp-presentation-switch');
    await expect(group).toHaveAttribute('role', 'radiogroup');
    expect(await group.locator('[role="radio"]').allTextContents()).toEqual(['Easy', 'Advanced']);
    await expect(option(page, 'advanced')).toHaveAttribute('aria-checked', 'true');
    await expect(option(page, 'easy')).toHaveAttribute('aria-checked', 'false');
    await expect(dialog(page).locator('.dp-modal-title-hint')).toHaveText('No password required to sign in');
    const geometry = await dialog(page).evaluate(node => {
      const box = element => element.getBoundingClientRect();
      const header = node.querySelector('.dp-modal-header');
      const [easy, advanced] = node.querySelectorAll('.dp-presentation-option');
      const switcher = node.querySelector('.dp-presentation-switch');
      const heading = node.querySelector('.dp-modal-heading');
      const rgb = value => (value.match(/rgba?\(([^)]+)\)/) || [, ''])[1].split(',').map(Number);
      const chosen = rgb(getComputedStyle(advanced).borderTopColor);
      return {
        gap: box(advanced).left - box(easy).right, rows: [box(easy).top, box(advanced).top, box(easy).height, box(advanced).height],
        rightInset: box(header).right - box(switcher).right, headerPadding: parseFloat(getComputedStyle(header).paddingRight),
        // The title block ends before the pair, or the pair has its own row below it.
        clear: box(heading).right <= box(switcher).left || box(switcher).top >= box(heading).bottom - 0.5,
        clipped: [easy, advanced].some(item => item.scrollWidth > item.clientWidth),
        overflow: node.scrollWidth > node.clientWidth || document.documentElement.scrollWidth > window.innerWidth,
        chosen, accent: getComputedStyle(advanced).getPropertyValue('--dp-accent-rgb').split(',').map(Number),
        unchosenShadow: getComputedStyle(easy).boxShadow,
      };
    });
    expect(geometry.gap).toBe(-1);                              // contiguous: one outline, one shared seam
    expect(geometry.rows[0]).toBeCloseTo(geometry.rows[1], 1);
    expect(geometry.rows[2]).toBeCloseTo(geometry.rows[3], 1);
    expect(geometry.rows[2]).toBeGreaterThanOrEqual(32);
    expect(Math.abs(geometry.rightInset - geometry.headerPadding)).toBeLessThan(1.5);   // right aligned
    expect(geometry.clear).toBe(true);
    expect(geometry.clipped).toBe(false);
    expect(geometry.overflow).toBe(false);
    expect(geometry.chosen.slice(0, 3)).toEqual(geometry.accent);  // the palette's accent, not a success green
    expect(geometry.unchosenShadow).toBe('none');
    if (EVIDENCE) {
      await page.evaluate(() => Promise.all(document.getAnimations().map(animation => animation.finished.catch(() => {}))));
      await dialog(page).screenshot({path: `${EVIDENCE}/my-account-selector-${width}-${mode}.png`});
    }
    // A radio group: the arrow keys move the choice, and focus with it.
    await option(page, 'advanced').focus();
    await page.keyboard.press('ArrowLeft');
    await expect(option(page, 'easy')).toHaveAttribute('aria-checked', 'true');
    await expect(option(page, 'easy')).toBeFocused();
    await expect(option(page, 'easy')).toHaveAttribute('tabindex', '0');
    await expect(option(page, 'advanced')).toHaveAttribute('tabindex', '-1');
    await expect(group).toHaveAttribute('data-state', 'saved');
    await page.keyboard.press('ArrowRight');
    await expect(option(page, 'advanced')).toHaveAttribute('aria-checked', 'true');
    await expect(group).toHaveAttribute('data-state', 'saved');
    await closeAccount(page);
  }
});

test('E06/E14: Easy withdraws Activity Log live from the sidebar, the page, the dock and More; Advanced returns it', async ({page}) => {
  const errors = [];
  page.on('pageerror', error => errors.push(String(error.message)));
  await boot(page, 1440);
  await page.locator('#sidebar .nav-item[data-view="events"]').click();
  await expect(page.locator('#page-title')).toHaveText('Activity Log');
  await openAccount(page);
  await choose(page, 'easy');
  // The page it was on is gone: Dashboard, current everywhere, with no reload.
  await expect(page.locator('#page-title')).toHaveText('Dashboard');
  await expect(page.locator('#view-dashboard')).toHaveClass(/\bactive\b/);
  await closeAccount(page);
  expect(await sidebarViews(page)).toEqual(['dashboard', 'torrents', 'stats', 'settings', 'help']);
  await expect(page.locator('#view-events')).toHaveCount(0);
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toHaveAttribute('aria-current', 'page');
  await expect(page.locator('#sidebar [aria-current="page"]')).toHaveCount(1);

  await page.setViewportSize({width: 320, height: 760});
  expect(await dockViews(page)).toEqual(['torrents', 'stats', 'dashboard', 'settings', 'help']);
  await expect(page.locator('#shell-dock .dp-shell-dock-more')).toHaveCount(0);
  await expect(page.locator('#shell-dock [data-view="dashboard"]')).toHaveAttribute('aria-current', 'page');
  await expect(page.locator('#shell-dock [aria-current="page"]')).toHaveCount(1);
  if (EVIDENCE) await page.screenshot({path: `${EVIDENCE}/admin-easy-dock-320.png`});

  await openAccount(page);
  await choose(page, 'advanced');
  await closeAccount(page);
  expect(await dockViews(page)).toEqual(['torrents', 'stats', 'dashboard', 'settings']);
  await expect(page.locator('#shell-dock .dp-shell-dock-more')).toHaveCount(1);
  expect(await moreViews(page)).toEqual(['events', 'help']);
  await expect(page.locator('#shell-dock [data-view="dashboard"]')).toHaveAttribute('aria-current', 'page');
  // Restored where it was, not reopened, and usable again from the dock's More.
  expect(await sidebarViews(page)).toEqual(['dashboard', 'torrents', 'events', 'stats', 'settings', 'help']);
  await expect(page.locator('#sidebar .nav-item[data-view="events"]')).not.toHaveClass(/\bactive\b/);
  await page.locator('#shell-dock .dp-shell-dock-more').click();
  await page.locator('#shell-more-list .dp-shell-sheet-item[data-view="events"]').click();
  await expect(page.locator('#page-title')).toHaveText('Activity Log');
  await expect(page.locator('#view-events')).toHaveClass(/\bactive\b/);
  expect(errors).toEqual([]);
});

test('E05: a saved Easy is composed before the first paint; an unread choice is neutral and Advanced; a refused save returns; the newest choice wins', async ({page}) => {
  await boot(page, 1440);
  await api(page, 'PUT', '/account/presentation', {presentation_mode: 'easy'});
  // Any Activity Log entry ever connected while the page is built is recorded.
  await page.addInitScript(() => {
    window.__dpEventsSeen = 0;
    new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(node => {
      if (node.nodeType === 1 && node.matches('.nav-item[data-view="events"], #view-events') && node.isConnected) {
        window.__dpEventsSeen += 1;
      }
    }))).observe(document, {childList: true, subtree: true});
  });
  await page.reload();
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
  expect(await page.evaluate(() => window.__dpEventsSeen)).toBe(0);
  await expect(page.locator('#sidebar .nav-item[data-view="events"]')).toHaveCount(0);
  await openAccount(page);
  await expect(option(page, 'easy')).toHaveAttribute('aria-checked', 'true');
  await closeAccount(page);

  // Unread at first paint (the bundle could not read it in time) and still
  // unreadable: Advanced, and neither compartment chosen.
  const bundle = url => url.pathname === '/app.js';
  const presentationApi = url => url.pathname === '/api/account/presentation';
  await page.route(bundle, async route => {
    const response = await route.fetch();
    return route.fulfill({response, body: (await response.text()).replace(
      /window\.debridPulsePresentation=Object\.freeze\(\{"owned":true,"mode":"easy"\}\);/,
      'window.debridPulsePresentation=Object.freeze({"owned":true,"mode":null});')});
  });
  const unreadable = route =>
    route.request().method() === 'GET' ? route.fulfill({status: 503, json: {detail: 'unavailable'}}) : route.fallback();
  await page.route(presentationApi, unreadable);
  await page.reload();
  await expect(page.locator('#sidebar .nav-item[data-view="events"]')).toHaveCount(1);
  await openAccount(page);
  await expect(dialog(page).locator('.dp-presentation-switch')).toHaveAttribute('data-state', 'unavailable');
  await expect(dialog(page).locator('[role="radio"][aria-checked="true"]')).toHaveCount(0);
  await closeAccount(page);
  // Read again where it is offered: the saved Easy is applied once read.
  await page.unroute(presentationApi, unreadable);
  await openAccount(page);
  await expect(option(page, 'easy')).toHaveAttribute('aria-checked', 'true');
  await expect(page.locator('#sidebar .nav-item[data-view="events"]')).toHaveCount(0);
  await choose(page, 'advanced');
  await closeAccount(page);
  await page.unroute(bundle);

  // A refused save: the UI returns to the saved choice and says so.
  const refused = route =>
    route.request().method() === 'PUT' ? route.fulfill({status: 500, json: {detail: 'synthetic refusal'}}) : route.fallback();
  await page.route(presentationApi, refused);
  await openAccount(page);
  await option(page, 'easy').click();
  await expect(option(page, 'advanced')).toHaveAttribute('aria-checked', 'true');
  await expect(dialog(page).locator('.dp-presentation-switch')).toHaveAttribute('data-state', 'saved');
  await expect(dialog(page).locator('.dp-presentation-status')).toHaveText('Easy was not saved.');
  await expect(page.locator('.toast, .dp-toast').filter({hasText: 'synthetic refusal'}).first()).toBeVisible();
  await expect(page.locator('#sidebar .nav-item[data-view="events"]')).toHaveCount(1);
  await page.unroute(presentationApi, refused);
  expect((await api(page, 'GET', '/account/presentation')).body).toEqual({presentation_mode: 'advanced'});

  // Rapid choices: one save at a time, the newest last, so the server and the
  // page both end on the last choice.
  const sent = [];
  let release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route(presentationApi, async route => {
    if (route.request().method() !== 'PUT') return route.fallback();
    sent.push(route.request().postDataJSON().presentation_mode);
    if (sent.length === 1) await held;
    return route.fallback();
  });
  await option(page, 'easy').click();
  await expect(dialog(page).locator('.dp-presentation-switch')).toHaveAttribute('data-state', 'saving');
  await option(page, 'advanced').click();
  await option(page, 'easy').click();
  await option(page, 'advanced').click();
  expect(sent).toEqual(['easy']);                      // the next waits for this answer
  release();
  await expect(dialog(page).locator('.dp-presentation-switch')).toHaveAttribute('data-state', 'saved');
  expect(sent).toEqual(['easy', 'advanced']);
  await expect(option(page, 'advanced')).toHaveAttribute('aria-checked', 'true');
  await expect(page.locator('#sidebar .nav-item[data-view="events"]')).toHaveCount(1);
  expect((await api(page, 'GET', '/account/presentation')).body).toEqual({presentation_mode: 'advanced'});
});

test('E04/E07-E10/E12/E15: Server Settings in Easy omit exactly the listed parts, every section stays, and an unsaved draft survives', async ({page}) => {
  await boot(page, 1440);
  await openSettings(page);
  await showSection(page, 'authentication');
  const username = page.locator('#dp-settings-field-auth-username');
  const password = page.locator('#dp-settings-field-auth-password');
  await username.fill(`draft-${run}`);
  await password.fill(`draft-${run}-pass`);
  await expect(page.locator('[data-action="apply-credentials"]')).toBeEnabled();
  const writes = [];
  page.on('request', request => {
    // Writes only: a Test probe (test-*) reads and writes nothing.
    if (request.method() !== 'GET' && /\/api\/(settings|auth\/config|transfer-policy|integrations|execution)/.test(request.url())
        && !/\/test-/.test(request.url())) {
      writes.push(`${request.method()} ${new URL(request.url()).pathname}`);
    }
  });
  await openAccount(page);
  await choose(page, 'easy');
  await closeAccount(page);

  // Every section stays, in its order, on the same selection.
  expect(await page.locator('#view-settings .dp-settings-tabs [data-tab]:not(.dp-motion-still *)')
    .evaluateAll(tabs => tabs.map(tab => tab.dataset.tab))).toEqual(SECTION_KEYS);
  await expect(page.locator('#view-settings .dp-settings-tabs [aria-selected="true"]:not(.dp-motion-still *)'))
    .toHaveAttribute('data-tab', 'authentication');
  expect(await panelCards(page, 'authentication')).toEqual(['Authentication Status']);
  await expect(page.locator('#dp-settings-field-auth-username')).toHaveCount(0);
  await showSection(page, 'sources');
  for (const id of PREMIUM) await expect(page.locator(`#view-settings [data-integration-enabled="${id}"]`)).toHaveCount(1);
  await expect(page.locator('#view-settings details.dp-settings-additional')).toHaveCount(0);
  await expect(page.locator('#view-settings [data-integration-enabled="usenet"]')).toHaveCount(1);
  await showSection(page, 'downloads');
  expect(await panelCards(page, 'downloads')).toEqual(['Download Behavior & Limits']);
  await expect(page.locator('#dp-settings-field-download-folder')).toBeVisible();
  await expect(page.locator('#dp-settings-field-aria2-max-active-downloads')).toBeVisible();
  await expect(page.locator('#view-settings [data-panel="downloads"] details, #view-settings [data-dp-executor-work-card]')).toHaveCount(0);
  await showSection(page, 'users');
  await expect(page.locator('[data-users-mount] .dp-settings-users-card').first()).toBeVisible();
  expect(await panelCards(page, 'users')).toEqual(['User Accounts', 'Integration Accounts', 'Integration Delivery Profiles']);
  await showSection(page, 'notifications');
  expect(await panelCards(page, 'notifications')).toEqual(['Webhook Notifications']);
  await showSection(page, 'maintenance');
  expect(await panelCards(page, 'maintenance')).toEqual(['Backups & Retention']);
  await expect(page.locator('#dp-settings-field-backup-folder')).toBeVisible();
  await expect(page.locator('#view-settings [data-panel="maintenance"] [data-action="run-backup"]')).toHaveCount(1);
  await expect(page.locator('#view-settings [data-panel="maintenance"] details')).toHaveCount(0);
  if (EVIDENCE) await page.screenshot({path: `${EVIDENCE}/server-easy-maintenance-1440.png`});

  await openAccount(page);
  await choose(page, 'advanced');
  await closeAccount(page);
  expect(await panelCards(page, 'maintenance')).toEqual(['Event Logging', 'Backups & Retention', 'Database Reset Controls']);
  await showSection(page, 'authentication');
  expect(await panelCards(page, 'authentication')).toEqual(['Authentication Status', 'Username & Password', 'OpenID Connect']);
  // The draft is the operator's, still unsaved, and nothing was written for it.
  await expect(page.locator('#dp-settings-field-auth-username')).toHaveValue(`draft-${run}`);
  await expect(page.locator('#dp-settings-field-auth-password')).toHaveValue(`draft-${run}-pass`);
  await expect(page.locator('[data-action="apply-credentials"]')).toBeEnabled();
  await showSection(page, 'users');
  expect((await panelCards(page, 'users'))[0]).toBe('Deployment & Language');
  await showSection(page, 'sources');
  await expect(page.locator('#view-settings details.dp-settings-additional')).toHaveCount(5);
  expect(writes).toEqual([]);
});

test('E07/E09/E10: Standalone keeps no Authentication section and no User Accounts in either mode; Easy omits Deployment Type and the Additional Settings', async ({page}) => {
  await page.route(url => url.pathname === '/api/settings', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    const real = await (await route.fetch()).json();
    return route.fulfill({json: {...real, deployment_type: 'standalone'}});
  });
  await boot(page, 1440);
  await openSettings(page);
  for (const mode of ['easy', 'advanced']) {
    await openAccount(page);
    await choose(page, mode);
    await closeAccount(page);
    expect(await page.locator('#view-settings .dp-settings-tabs [data-tab]:not(.dp-motion-still *)')
      .evaluateAll(tabs => tabs.map(tab => tab.dataset.tab))).toEqual(SECTION_KEYS.filter(key => key !== 'authentication'));
    await showSection(page, 'users');
    await expect(page.locator('#view-settings [data-tab="users"] .dp-settings-tab-label').first()).toHaveText('Access & Integrations');
    await expect(page.locator('[data-users-mount] .dp-settings-users-card').first()).toBeVisible();
    expect(await panelCards(page, 'users')).toEqual(mode === 'easy'
      ? ['Integration Accounts', 'Integration Delivery Profiles']
      : ['Deployment & Language', 'Integration Accounts', 'Integration Delivery Profiles']);
    await showSection(page, 'sources');
    for (const id of PREMIUM) await expect(page.locator(`#view-settings [data-integration-enabled="${id}"]`)).toHaveCount(1);
    await expect(page.locator('#view-settings details.dp-settings-additional')).toHaveCount(mode === 'easy' ? 0 : 5);
  }
});

test('E15: at 320 the Easy Settings navigator walks every section, each with content, without overflow', async ({page}) => {
  await boot(page, 320);
  await openAccount(page);
  await choose(page, 'easy');
  await closeAccount(page);
  await openSettings(page);
  const visited = [];
  for (let step = 0; step < SECTION_KEYS.length; step += 1) {
    const key = await page.locator('#view-settings .dp-settings-tabs [aria-selected="true"]:not(.dp-motion-still *)').getAttribute('data-tab');
    visited.push(key);
    const panel = page.locator(`#view-settings [data-panel="${key}"]`);
    await expect(panel).toBeVisible();
    expect(await panel.locator('section.card').count()).toBeGreaterThan(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    if (step < SECTION_KEYS.length - 1) {
      await page.locator('#view-settings [data-settings-end="next"]').click();
      await expect(page.locator('#view-settings .dp-settings-tabs [aria-selected="true"]:not(.dp-motion-still *)'))
        .not.toHaveAttribute('data-tab', key);
    }
  }
  expect(visited).toEqual(SECTION_KEYS);
});

test.describe('the password-auth runtime', () => {
  test.skip(!AUTH, 'DP_AUTH_BASE_URL is not configured');

  async function signIn(browser, {username, password}, viewport) {
    const shared = username === ADMIN.username;
    const reuse = shared && adminSession.get() !== null;
    const context = await browser.newContext({baseURL: AUTH, viewport, ...(reuse ? {storageState: adminSession.get()} : {})});
    const page = await context.newPage();
    await isolateFonts(page);
    if (reuse) {
      await page.goto('/');
    } else {
      await page.goto('/login');
      await page.locator('#username').fill(username);
      await page.locator('#password').fill(password);
      await page.locator('button[type="submit"]').click();
    }
    await expect(page).not.toHaveURL(/\/login/);
    await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
    if (shared && !reuse) adminSession.remember(await context.storageState());
    return {context, page};
  }

  async function person(admin, name, role) {
    const account = {username: `ea-${name}-${run}`, password: `ea-${name}-${run}-pass`};
    const created = await api(admin.page, 'POST', '/accounts', {kind: 'local', role, display_name: `${name} ${run}`, ...account});
    expect(created.status).toBe(200);
    return account;
  }

  test('E11/E13/E01/E18: a User\'s bell opens their own Notifications at 320; Easy omits Statistics Reporting there, on every device', async ({browser}) => {
    const admin = await signIn(browser, ADMIN, {width: 1440, height: 914});
    const user = await person(admin, 'user', 'user');
    await admin.context.close();

    const phone = await signIn(browser, user, {width: 320, height: 760});
    const page = phone.page;
    expect(await dockViews(page)).toEqual(['torrents', 'stats', 'dashboard', 'settings', 'help']);
    const bell = page.locator('#shell-dock .dp-shell-dock-item').nth(3);
    await expect(bell).toHaveAttribute('aria-label', 'Notifications');
    await expect(bell.locator('svg[data-dp-lucide="bell-ring"]')).toHaveCount(1);
    await bell.click();
    await expect(page.locator('#page-title')).toHaveText('Notifications');
    await expect(page.locator('#view-settings')).toHaveAttribute('data-settings-context', 'self');
    await expect(page.locator('#view-settings .dp-settings-master-card--own')).toBeVisible();
    await expect(page.locator('#view-settings .dp-settings-tabs')).toHaveCount(0);
    expect(await panelCards(page, 'notifications')).toEqual(['Webhook Notifications', 'Statistics Reporting']);
    if (EVIDENCE) await page.screenshot({path: `${EVIDENCE}/user-advanced-notifications-320.png`});

    await openAccount(page);
    await choose(page, 'easy');
    await closeAccount(page);
    expect(await panelCards(page, 'notifications')).toEqual(['Webhook Notifications']);
    await expect(page.locator('#page-title')).toHaveText('Notifications');
    expect(await dockViews(page)).toEqual(['torrents', 'stats', 'dashboard', 'settings', 'help']);
    await expect(page.locator('#shell-dock [data-view="settings"]')).toHaveAttribute('aria-current', 'page');
    if (EVIDENCE) await page.screenshot({path: `${EVIDENCE}/user-easy-notifications-320.png`});
    expect((await api(page, 'GET', '/settings')).status).toBe(403);   // no Admin settings, either mode

    // The same account on another device: Easy from its first paint.
    const desk = await signIn(browser, user, {width: 1440, height: 914});
    expect(await sidebarViews(desk.page)).toEqual(['dashboard', 'torrents', 'stats', 'settings', 'help']);
    await expect(desk.page.locator('#sidebar .nav-item[data-view="settings"] .nav-label')).toHaveText('Notifications');
    await desk.page.locator('#sidebar .nav-item[data-view="settings"]').click();
    await expect(desk.page.locator('#view-settings .dp-settings-master-card--own')).toBeVisible();
    expect(await panelCards(desk.page, 'notifications')).toEqual(['Webhook Notifications']);
    await openAccount(desk.page);
    await expect(option(desk.page, 'easy')).toHaveAttribute('aria-checked', 'true');
    await choose(desk.page, 'advanced');
    await closeAccount(desk.page);
    expect(await panelCards(desk.page, 'notifications')).toEqual(['Webhook Notifications', 'Statistics Reporting']);
    await phone.context.close();
    await desk.context.close();
  });

  test('E16/E18: a ReadOnly account owns its own choice, independent of the Admin\'s', async ({browser}) => {
    const admin = await signIn(browser, ADMIN, {width: 1440, height: 914});
    const reader = await person(admin, 'reader', 'readonly');
    const signedIn = await signIn(browser, reader, {width: 1440, height: 914});
    await openAccount(signedIn.page);
    await choose(signedIn.page, 'easy');
    await closeAccount(signedIn.page);
    expect((await api(signedIn.page, 'GET', '/account/presentation')).body).toEqual({presentation_mode: 'easy'});
    expect((await api(admin.page, 'GET', '/account/presentation')).body).toEqual({presentation_mode: 'advanced'});
    await signedIn.context.close();
    await admin.context.close();
  });
});
