const { test, expect } = require('@playwright/test');

/* DP 1.0.14 My Account selector geometry and context-aware Help.
 *
 * WS1: My Account's Easy | Advanced is one control of two compartments with
 * one outline and one seam: the chosen compartment's own border, rounded on
 * its outer side, raised over its neighbour's (V01-V03). The header's far
 * end is [Help] [Easy | Advanced], right aligned, reflowing onto its own row
 * when the title and its hint need the room (V04); the Help opens My
 * Account's topic over the dialog, closes to its chip, and keeps every
 * unsaved field (V05); it explains only what that dialog offers that account
 * (V06).
 *
 * WS2: the Field Manual and the contextual topics read the interface's own
 * context (auth.js debridPulseAuth.presentation.context): role, deployment
 * type, Easy or Advanced. The context matrix below is driven through the
 * server's own bootstrap answers (the /app.js authority and presentation
 * prefixes and /api/auth/session), rewritten here for the role and
 * deployment type of each case -- a mock of the server's facts, labelled as
 * such; what the server decides is proven by its own tests. The open
 * runtime's default Admin's presentation is returned to Advanced after every
 * case. Synthetic values only. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const EVIDENCE = process.env.DP_EVIDENCE_DIR || '';
const ADMIN = {username: 'browser', password: 'browser-secret'};
const run = Date.now().toString(36);
const adminSession = require('./acl-admin-session');

const CAPABILITIES = {
  user: ['demand.own', 'interface', 'notifications.self', 'session', 'statistics.read', 'statistics.summary',
    'transfers.destroy', 'transfers.operate', 'transfers.read', 'transfers.submit'],
  readonly: ['interface', 'session', 'statistics.read', 'statistics.summary', 'transfers.read'],
};
const SECTIONS = ['quickstart', 'using', 'howitworks', 'services', 'engines', 'settings', 'trouble', 'license'];

async function isolateFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

const api = (page, method, path, body) => page.evaluate(async ([method, path, body]) => {
  const response = await window.debridPulseAuth.fetch(`/api${path}`, {
    method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
  return {status: response.status, body: await response.json().catch(() => null)};
}, [method, path, body]);

/* The server's bootstrap answers for one context: its role (a mock of the
 * authority the server would issue), deployment type and presentation. */
async function asContext(page, {role = 'admin', deployment = 'server', mode = 'advanced'} = {}) {
  const shape = authority => {
    authority.deployment_type = deployment;
    if (CAPABILITIES[role]) { authority.role = role; authority.capabilities = CAPABILITIES[role]; }
    return authority;
  };
  await page.route(url => url.pathname === '/app.js', async route => {
    const response = await route.fetch();
    const body = (await response.text())
      .replace(/^window\.debridPulseAuthority=Object\.freeze\((.*)\);$/m,
        (_, json) => `window.debridPulseAuthority=Object.freeze(${JSON.stringify(shape(JSON.parse(json)))});`)
      .replace(/^window\.debridPulsePresentation=Object\.freeze\((.*)\);$/m,
        `window.debridPulsePresentation=Object.freeze(${JSON.stringify({owned: true, mode})});`);
    return route.fulfill({response, body});
  });
  await page.route(url => url.pathname === '/api/auth/session', async route => {
    const response = await route.fetch();
    return route.fulfill({response, json: shape(await response.json())});
  });
}

// The account profile read, answered for a deployment type and sign-in state.
async function asProfile(page, facts) {
  await page.route(url => url.pathname === '/api/account', async route => {
    if (route.request().method() !== 'GET') return route.continue();
    const real = await (await route.fetch()).json();
    return route.fulfill({json: {...real, ...facts}});
  });
}

async function boot(page, width = 1440) {
  await page.setViewportSize({width, height: width < 780 ? 760 : 900});
  await isolateFonts(page);
  await page.goto('/');
  await expect(page.locator('#sidebar .nav-item[data-view="dashboard"]')).toBeAttached();
}

const dialog = page => page.locator('.dp-modal-dialog.dp-account-dialog');
const option = (page, mode) => dialog(page).locator(`[data-presentation-mode="${mode}"]`);
const helpDialog = page => page.locator('.dp-modal-dialog.dp-settings-help-dialog');

async function openAccount(page) {
  if ((page.viewportSize()?.width || 1440) < 780) {
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
}

async function closeAccount(page) {
  await dialog(page).locator('[data-modal-cancel]').click();
  await expect(dialog(page)).toHaveCount(0);
}

const settle = page => page.evaluate(() => Promise.all(document.getAnimations().map(animation => animation.finished.catch(() => {}))));

async function openHelp(page) {
  await page.locator('#sidebar .nav-item[data-view="help"]').evaluate(element => element.click());
  await expect(page.locator('#view-help .dp-help-tabs')).toBeAttached();
}

const helpTabs = page => page.locator('#view-help .dp-help-tab:not(.dp-motion-still *)')
  .evaluateAll(tabs => tabs.map(tab => `${tab.dataset.tab}:${tab.querySelector('.dp-help-tab-label').textContent.trim()}`));
const manual = page => page.locator('#view-help .dp-help-panels:not(.dp-motion-still *)').first()
  .evaluate(node => node.textContent.replace(/\s+/g, ' '));
const topicText = page => helpDialog(page).locator('.dp-modal-body').evaluate(node => node.textContent.replace(/\s+/g, ' '));

test.afterEach(async ({page}) => {
  if (!page.isClosed() && page.url().startsWith('http') && !page.url().startsWith(AUTH || '\u0000')) {
    await api(page, 'PUT', '/account/presentation', {presentation_mode: 'advanced'}).catch(() => {});
  }
});

/* ── WS1 ───────────────────────────────────────────────────────────────── */

test('V01-V03: one outline and one seam in both selections, accent-chosen, light and dark, 1440 and 320; focus and saving unchanged', async ({page}) => {
  for (const [width, theme] of [[1440, 'dark'], [320, 'light']]) {
    await boot(page, width);
    await page.evaluate(light => window.DPTheme.setMode(light), theme === 'light');
    await openAccount(page);
    for (const mode of ['easy', 'advanced']) {
      await choose(page, mode);
      await page.mouse.move(0, 0);
      await settle(page);
      const g = await dialog(page).locator('.dp-presentation-switch').evaluate(group => {
        const [easy, advanced] = group.querySelectorAll('.dp-presentation-option');
        const style = node => getComputedStyle(node);
        const rgb = value => (value.match(/rgba?\(([^)]+)\)/) || [, ''])[1].split(',').map(Number);
        const box = node => node.getBoundingClientRect();
        const chosen = group.querySelector('[aria-checked="true"]');
        return {
          seam: box(advanced).left - box(easy).right,                  // the borders overlap: one seam
          heights: [box(easy).height, box(advanced).height, box(easy).top, box(advanced).top],
          group: {border: style(group).borderTopWidth, overflow: style(group).overflow, radius: style(group).borderTopLeftRadius},
          radii: [style(easy).borderTopLeftRadius, style(easy).borderTopRightRadius,
            style(advanced).borderTopLeftRadius, style(advanced).borderBottomRightRadius],
          shadows: [style(easy).boxShadow, style(advanced).boxShadow],
          chosenBorder: rgb(style(chosen).borderTopColor), chosenSides: ['Top', 'Right', 'Bottom', 'Left'].map(side => style(chosen)[`border${side}Color`]),
          chosenZ: style(chosen).zIndex,
          accent: style(chosen).getPropertyValue('--dp-accent-rgb').split(',').map(Number),
          otherBorder: style(group.querySelector('[aria-checked="false"]')).borderTopColor,
        };
      });
      expect(g.seam).toBe(-1);
      expect(g.heights[0]).toBeCloseTo(g.heights[1], 1);
      expect(g.heights[2]).toBeCloseTo(g.heights[3], 1);
      expect(g.group.border).toBe('0px');                            // the compartments draw the one outline
      expect(g.group.overflow).toBe('visible');                      // nothing clips the chosen border
      expect(g.radii[0]).toBe(g.group.radius);                       // Easy: rounded left only
      expect(g.radii[1]).toBe('0px');
      expect(g.radii[2]).toBe('0px');                                // Advanced: rounded right only
      expect(g.radii[3]).toBe(g.group.radius);
      expect(g.shadows).toEqual(['none', 'none']);                   // no inset rectangle
      expect(g.chosenBorder.slice(0, 3)).toEqual(g.accent);          // the palette's accent on every side
      expect(new Set(g.chosenSides).size).toBe(1);
      expect(g.chosenZ).toBe('1');
      expect(g.otherBorder).not.toBe(g.chosenSides[0]);
      if (EVIDENCE) await dialog(page).locator('.dp-presentation-switch').screenshot({path: `${EVIDENCE}/after-${theme}-${width}-${mode}.png`});
    }
    // Focus is the keyboard's own ring, outside the border; arrows still choose.
    await option(page, 'advanced').focus();
    await page.keyboard.press('ArrowLeft');
    await expect(option(page, 'easy')).toBeFocused();
    await expect(option(page, 'easy')).toHaveAttribute('aria-checked', 'true');
    const focus = await option(page, 'easy').evaluate(node => ({outline: getComputedStyle(node).outlineStyle,
      width: getComputedStyle(node).outlineWidth, offset: getComputedStyle(node).outlineOffset, shadow: getComputedStyle(node).boxShadow}));
    expect(focus).toEqual({outline: 'solid', width: '2px', offset: '2px', shadow: 'none'});
    await expect(dialog(page).locator('.dp-presentation-switch')).toHaveAttribute('data-state', 'saved');
    // While a save is on its way the chosen border is fainter.
    let release;
    const held = new Promise(resolve => { release = resolve; });
    const pending = async route => { if (route.request().method() === 'PUT') await held; return route.fallback(); };
    await page.route(url => url.pathname === '/api/account/presentation', pending);
    await option(page, 'advanced').click();
    try {
      await expect(dialog(page).locator('.dp-presentation-switch')).toHaveAttribute('data-state', 'saving');
      await settle(page);
      const faint = await option(page, 'advanced').evaluate(node => getComputedStyle(node).borderTopColor);
      expect(faint).toMatch(/0\.4\)$/);
    } finally {
      release();
    }
    await expect(dialog(page).locator('.dp-presentation-switch')).toHaveAttribute('data-state', 'saved');
    await page.unroute(url => url.pathname === '/api/account/presentation', pending);
    await closeAccount(page);
  }
});

/* The header's geometry: in a narrow dialog the title (wrapping to two
 * lines) and [Help] [Easy | Advanced] share the first row, centred against
 * each other, and the Standalone password line, when present, is the second
 * row, centred across the header; wider, the title stays on one line with the
 * hint beneath it and the pair right aligned on the same row. */
async function headerGeometry(page) {
  return dialog(page).evaluate(node => {
    const box = element => element.getBoundingClientRect();
    const lines = element => {
      const range = document.createRange();
      range.selectNodeContents(element);
      return new Set([...range.getClientRects()].map(rect => Math.round(rect.top))).size;
    };
    const textBox = element => { const range = document.createRange(); range.selectNodeContents(element); return range.getBoundingClientRect(); };
    const header = node.querySelector('.dp-modal-header');
    const title = node.querySelector('.dp-modal-title');
    const hint = node.querySelector('.dp-modal-title-hint');
    const chip = node.querySelector('.dp-account-header-end .dp-settings-help-chip');
    const pair = node.querySelector('.dp-presentation-switch');
    const end = node.querySelector('.dp-account-header-end');
    const style = getComputedStyle(header);
    const inner = {left: box(header).left + parseFloat(style.paddingLeft), right: box(header).right - parseFloat(style.paddingRight)};
    const centre = rect => rect.top + rect.height / 2;
    return {
      titleLines: lines(title), titleText: title.textContent,
      sameRow: box(title).top < box(end).bottom && box(end).top < box(title).bottom,
      centreDelta: Math.abs(centre(box(title)) - centre(box(end))),
      titleLeftOfEnd: box(title).right <= box(end).left + 0.5,
      chipGap: box(pair).left - box(chip).right,
      endRightInset: inner.right - box(end).right,
      hint: hint ? {below: box(hint).top >= Math.max(box(title).bottom, box(end).bottom) - 0.5, belowTitle: box(hint).top >= box(title).bottom - 0.5,
        centred: Math.abs((textBox(hint).left + textBox(hint).right) / 2 - (inner.left + inner.right) / 2),
        spans: box(hint).width >= (inner.right - inner.left) - 1, align: getComputedStyle(hint).textAlign,
        underTitle: Math.abs(box(hint).left - box(title).left) < 1, text: hint.textContent} : null,
      headerHeight: box(header).height,
      clipped: [...node.querySelectorAll('.dp-presentation-option')].some(item => item.scrollWidth > item.clientWidth)
        || chip.getBoundingClientRect().width < 34,
      overflow: node.scrollWidth > node.clientWidth || header.scrollWidth > header.clientWidth
        || document.documentElement.scrollWidth > window.innerWidth,
    };
  });
}

test('V04/X01: at 320 the two-line title and [Help] [Easy | Advanced] share a centred first row and the password line is a centred second row; wider, one row', async ({page}) => {
  for (const [hint, signInRequired] of [['No password required to sign in', false], ['Password required to sign in', true]]) {
    await page.unrouteAll({behavior: 'ignoreErrors'});
    await isolateFonts(page);
    await asProfile(page, {deployment_type: 'standalone', sign_in_required: signInRequired});
    for (const [width, theme, mode] of [[320, 'light', 'advanced'], [320, 'dark', 'easy'], [600, 'dark', 'advanced'], [1440, 'light', 'easy']]) {
      await boot(page, width);
      await page.evaluate(light => window.DPTheme.setMode(light), theme === 'light');
      await openAccount(page);
      await choose(page, mode);
      await settle(page);
      const g = await headerGeometry(page);
      expect(g.titleText).toBe('My Account');
      expect(g.sameRow).toBe(true);
      expect(g.titleLeftOfEnd).toBe(true);
      expect(g.chipGap).toBeCloseTo(8, 0);
      expect(g.endRightInset).toBeLessThan(1.5);
      expect(g.clipped).toBe(false);
      expect(g.overflow).toBe(false);
      expect(g.hint.text).toBe(hint);
      expect(g.hint.belowTitle).toBe(true);
      if (width === 320) {
        expect(g.hint.below).toBe(true);                                 // its own second row
        expect(g.titleLines).toBe(2);                                    // My / Account
        expect(g.centreDelta).toBeLessThan(1.5);
        expect(g.hint.spans).toBe(true);
        expect(g.hint.align).toBe('center');
        expect(g.hint.centred).toBeLessThan(1.5);
        expect(g.headerHeight).toBeLessThan(96);                         // no dead space
      } else {
        expect(g.titleLines).toBe(1);
        expect(g.hint.underTitle).toBe(true);
        expect(g.hint.align).not.toBe('center');
      }
      await expect(dialog(page).locator('[data-modal-accept]')).toBeInViewport();
      // Still one legible, working control: both labels choose.
      await option(page, mode === 'easy' ? 'advanced' : 'easy').click();
      await expect(dialog(page).locator('.dp-presentation-switch')).toHaveAttribute('data-state', 'saved');
      if (EVIDENCE && width !== 1440) await dialog(page).locator('.dp-modal-header').screenshot({path: `${EVIDENCE}/header-${signInRequired ? 'protected' : 'open'}-${width}-${theme}-${mode}.png`});
      await closeAccount(page);
    }
  }
  // Server: no password line, the title and the pair alone on one row.
  await page.unrouteAll({behavior: 'ignoreErrors'});
  await isolateFonts(page);
  await boot(page, 320);
  await openAccount(page);
  await settle(page);
  const server = await headerGeometry(page);
  expect(server.hint).toBe(null);
  expect(server.sameRow).toBe(true);
  expect(server.centreDelta).toBeLessThan(1.5);
  expect(server.overflow).toBe(false);
  expect(server.headerHeight).toBeLessThan(72);
  if (EVIDENCE) await dialog(page).locator('.dp-modal-header').screenshot({path: `${EVIDENCE}/header-server-320.png`});
  await closeAccount(page);
});

test('V05: My Account Help opens over the dialog, closes to its chip, and keeps every unsaved field and the selector', async ({page}) => {
  await boot(page, 1440);
  const writes = [];
  // Writes only: a Test probe (test-*) reads and writes nothing.
  page.on('request', request => {
    if (request.method() !== 'GET' && request.url().includes('/api/') && !/\/test-/.test(request.url())) writes.push(new URL(request.url()).pathname);
  });
  await openAccount(page);
  const name = dialog(page).locator('[data-field="display_name"]');
  await name.fill(`Draft ${run}`);
  await dialog(page).locator('[data-field="actual_name"]').fill(`Actual ${run}`);
  const chip = dialog(page).locator('.dp-account-header-end .dp-settings-help-chip');
  for (let round = 0; round < 3; round += 1) {                          // repeated opens leak nothing
    await chip.click();
    await expect(helpDialog(page)).toBeVisible();
    await expect(helpDialog(page).locator('.dp-modal-title')).toHaveText('My Account');
    await expect(dialog(page)).toBeAttached();                           // stacked, not replaced
    await expect(page.locator('.dp-modal-overlay')).toHaveCount(2);
    if (round === 0) await page.keyboard.press('Escape');
    else await helpDialog(page).locator('[data-modal-cancel]').click();
    await expect(helpDialog(page)).toHaveCount(0);
    await expect(chip).toBeFocused();
  }
  await expect(name).toHaveValue(`Draft ${run}`);
  await expect(dialog(page).locator('[data-field="actual_name"]')).toHaveValue(`Actual ${run}`);
  await expect(option(page, 'advanced')).toHaveAttribute('aria-checked', 'true');
  expect(writes).toEqual([]);
  // From the password view: the view and its fields stay as they were.
  await dialog(page).locator('[data-modal-action="password"]').click();
  await dialog(page).locator('[data-field="next"]').fill('synthetic-new');
  await chip.click();
  await expect(helpDialog(page)).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog(page).locator('[data-account-view="password"]')).toBeVisible();
  await expect(dialog(page).locator('[data-field="next"]')).toHaveValue('synthetic-new');
  await dialog(page).locator('[data-modal-cancel]').click();            // Cancel: back to the profile
  await expect(dialog(page).locator('[data-account-view="profile"]')).toBeVisible();
  await expect(dialog(page).locator('[data-field="display_name"]')).toHaveValue(`Draft ${run}`);
  await closeAccount(page);
  expect(writes).toEqual([]);
});

test('V06: My Account Help explains what this account is offered: open Standalone, protected Standalone and Server', async ({page}) => {
  // Open Standalone: no DP Username, its first password turns sign-in on.
  await asContext(page, {deployment: 'standalone'});
  await asProfile(page, {deployment_type: 'standalone', sign_in_required: false, has_password: false});
  await boot(page, 1440);
  await openAccount(page);
  await dialog(page).locator('.dp-account-header-end .dp-settings-help-chip').click();
  let text = await topicText(page);
  for (const phrase of ['Sign-in is off, so DebridPulse is used as its built-in Administrator', 'Set Password gives the built-in Administrator its password',
    'No password required to sign in: sign-in is off now', 'It is not a reading of whether a password is stored', 'Easy | Advanced chooses how much',
    'Advanced, the default, shows the full interface your role and this installation’s deployment type allow',
    'Choosing Advanced never adds anything your role or the deployment type does not allow', 'every browser of this installation shows the same choice']) {
    expect(text).toContain(phrase);
  }
  for (const absent of ['DP Username', 'Clear Password', 'identity provider', 'Change Password']) expect(text).not.toContain(absent);
  if (EVIDENCE) await helpDialog(page).screenshot({path: `${EVIDENCE}/my-account-help-open-standalone.png`});
  await page.keyboard.press('Escape');
  await closeAccount(page);
  await page.unrouteAll({behavior: 'ignoreErrors'});

  // Server (this runtime's open session: the default Admin): the DP Username,
  // Change Password, no Standalone sign-in line and no Clear Password.
  await isolateFonts(page);
  await asProfile(page, {deployment_type: 'server', sign_in_required: false, has_password: true, username: 'operator'});
  await boot(page, 1440);
  await openAccount(page);
  await dialog(page).locator('.dp-account-header-end .dp-settings-help-chip').click();
  text = await topicText(page);
  for (const phrase of ['DP Username is the name your account signs in with', 'Change Password asks for your current password', 'Display Name', 'Actual Name']) {
    expect(text).toContain(phrase);
  }
  for (const absent of ['Clear Password', 'No password required to sign in', 'Set Password gives']) expect(text).not.toContain(absent);
  await page.keyboard.press('Escape');
  await closeAccount(page);
});

/* ── WS2 ───────────────────────────────────────────────────────────────── */

/* The context matrix: role first, then deployment type, then the Easy list.
 * `has` must be in the manual, `not` must not be (distinctive control
 * references), `tabs` is the eligible collection. */
const MATRIX = [
  {id: 'H02', role: 'admin', deployment: 'server', mode: 'advanced', tabs: SECTIONS,
    has: ['The seven Settings tabs', 'Settings → Authentication', 'User Accounts lists each person', 'Under Advanced Settings:',
      'Tuning aria2: Transfer Method Settings → Network Sources', 'Activity Log (Admin only)', 'Database Reset Controls:',
      'Pause All (Admin only)', 'Recover All (Admin only)'],
    not: ['Your presentation is Easy', 'part of the Advanced presentation', 'built-in Administrator', 'Update badge']},
  {id: 'H03', role: 'admin', deployment: 'server', mode: 'easy', tabs: SECTIONS,
    has: ['The seven Settings tabs, their cards as the Easy presentation shows them', 'Authentication Status:', 'Your presentation is Easy',
      'its card is shown when My Account is set to Advanced', 'keep working with their saved values'],
    not: ['Under Advanced Settings:', 'Tuning aria2', 'Activity Log (Admin only)', 'Database Reset Controls:', 'OIDC Callback URL to register',
      'Then check the Activity Log']},
  {id: 'H04', role: 'admin', deployment: 'standalone', mode: 'advanced', tabs: SECTIONS,
    has: ['The six Settings tabs', 'A Standalone installation has no Authentication tab', 'Access & Integrations', 'built-in Administrator',
      'Integration Delivery Profiles tell', 'are not deleted or converted', 'keep their data but cannot sign in while it is Standalone',
      'Integration accounts keep working as before'],
    not: ['Settings → Authentication', 'User Accounts lists each person', 'DP Username', 'Add Local Account', 'Default Role for New OIDC Users',
      'Every account has one of four fixed roles', 'Every account other than the built-in Administrator', 'has every page and control',
      'there are no other roles', 'everything your role allows', 'is the one person']},
  {id: 'H05', role: 'admin', deployment: 'standalone', mode: 'easy', tabs: SECTIONS,
    has: ['The six Settings tabs, their cards as the Easy presentation shows them', 'A Standalone installation has no Authentication tab',
      'Integration Accounts are for automation', 'the full interface your role and this installation\'s deployment type allow',
      'Advanced does not add them'],
    not: ['Settings → Authentication', 'User Accounts lists each person', 'DP Username', 'Deployment & Language, first on this tab', 'Default Language is the language of every account', 'Under Advanced Settings:',
      'Tuning aria2', 'everything your role allows']},
  {id: 'H06', role: 'user', deployment: 'server', mode: 'advanced', tabs: SECTIONS.map(id => (id === 'settings' ? 'settings:Notifications' : id)),
    has: ['Your Notifications', 'Statistics Reporting A periodic summary of the download activity attributed to you', 'Add your first download',
      'withdraw your own request', 'Press Add (or Ctrl+Enter)', 'Row actions:', 'Untick what you do not want',
      'choose another by clicking the service name'],
    not: ['Settings reference', 'The seven Settings tabs', 'How a service card works', 'Tuning aria2', 'Under Advanced Settings:',
      'Provider Status (sidebar, Admin only)', 'Activity Log (Admin only)', 'press Connect Real-Debrid', 'Pause All (Admin only)',
      'Recover All (Admin only), in the Quick Add header', 'aria2: offline, the engine is not responding', 'Transfer Trace Log (Admin only) saves']},
  {id: 'H07', role: 'user', deployment: 'server', mode: 'easy', tabs: SECTIONS.map(id => (id === 'settings' ? 'settings:Notifications' : id)),
    has: ['Your Notifications', 'Webhook Notifications', 'Statistics reports you previously configured continue on their saved schedule',
      'select Advanced in My Account, then open Notifications → Statistics Reporting', 'Your presentation is Easy'],
    not: ['Their card is shown when your presentation is Advanced, in My Account', 'Report Window chooses', 'Statistics Reporting sends reports of your own activity', 'Settings reference', 'Tuning aria2']},
  {id: 'H08', role: 'readonly', deployment: 'server', mode: 'advanced', tabs: SECTIONS.filter(id => id !== 'settings'),
    has: ['See what is happening', 'Statistics shows completions', 'you cannot change downloads or settings',
      'changes nothing in downloads or settings; its own profile and presentation in My Account remain its own',
      'Click a row to open its details', 'from someone who can operate the download', 'DebridPulse moves on after its retries'],
    not: ['Settings reference', 'Your Notifications', 'Add your first download', 'How a service card works', 'Tuning aria2', 'press Connect Real-Debrid',
      'Quick Add refuses a link or file', 'Press Add (or Ctrl+Enter)', 'Untick what you do not want', 'Row actions:', 'Several at once:',
      'Open the download if no dialog is showing', 'choose another by clicking the service name', 'you can switch the remaining files',
      'Recover All (Admin only)', 'Pause All (Admin only)', 'Verify & Continue', 'Avoid removing a download', 'and change nothing.']},
];

for (const row of MATRIX) {
  test(`${row.id}: the manual for ${row.role} / ${row.deployment} / ${row.mode}`, async ({page}) => {
    const errors = [];
    page.on('pageerror', error => errors.push(String(error.message)));
    await asContext(page, row);
    await boot(page, 1440);
    await openHelp(page);
    const tabs = await helpTabs(page);
    expect(tabs.map(tab => (tab.startsWith('settings:Notifications') ? tab : tab.split(':')[0]))).toEqual(row.tabs);
    const text = await manual(page);
    for (const phrase of row.has) expect(text, `${row.id} should say: ${phrase}`).toContain(phrase);
    for (const phrase of row.not) expect(text, `${row.id} should not say: ${phrase}`).not.toContain(phrase);
    // Every section has content; License keeps its three documents.
    for (const tab of tabs) {
      const key = tab.split(':')[0];
      const length = await page.locator(`#view-help [data-panel="${key}"]:not(.dp-motion-still *)`).evaluate(node => node.textContent.trim().length);
      expect(length, key).toBeGreaterThan(400);
    }
    expect(await page.locator('#view-help [data-legal-document]').evaluateAll(buttons => buttons.map(button => button.dataset.legalDocument)))
      .toEqual(['gpl', 'source-offer', 'third-party']);
    expect(errors).toEqual([]);
  });
}

test('H09/X02: contextual topics read the same context; an unread context is no Admin\'s', async ({page}) => {
  const read = async (topic) => {
    await page.evaluate(name => window.DPSettingsHelp.open(name), topic);
    await expect(helpDialog(page)).toBeVisible();
    const text = await topicText(page);
    await page.keyboard.press('Escape');
    await expect(helpDialog(page)).toHaveCount(0);
    return text;
  };
  await asContext(page, {mode: 'easy'});
  await boot(page, 1440);
  let text = await read('premium-services');
  expect(text).not.toContain('Additional Settings hold');
  expect(text).toContain('keep working with their saved values');
  text = await read('alldebrid');
  expect(text).not.toContain('Maximum Active Torrents');
  expect(text).toContain('Maximum Concurrent Downloads, under Downloads, is one limit');
  text = await read('auth-status');
  expect(text).toContain('choose Advanced in My Account to show them');
  text = await read('backups');
  expect(text).not.toContain('Backup Retention or is no longer');
  expect(text).toContain('Restoring makes a safety backup first.');
  expect(text).not.toContain('resetting the database');
  text = await read('discord');
  expect(text).toContain('keep being sent as they were set up');
  expect(text).not.toContain('Notifications → Statistics Reporting');
  await page.unrouteAll({behavior: 'ignoreErrors'});

  await isolateFonts(page);
  await asContext(page, {deployment: 'standalone'});
  await boot(page, 1440);
  text = await read('integration-accounts');
  expect(text).toContain('the built-in Administrator does not');
  expect(text).not.toContain('Admin and User accounts do not');
  text = await read('discord');
  expect(text).not.toContain('Each User can also set up their own notifications');
  expect(text).not.toContain('previously configured continue');
  text = await read('alldebrid');
  expect(text).toContain('Maximum Active Torrents');               // Advanced: the whole topic
  await page.unrouteAll({behavior: 'ignoreErrors'});

  await isolateFonts(page);
  await asContext(page, {role: 'user', mode: 'easy'});
  await boot(page, 1440);
  text = await read('discord-own');
  // Reports keep running on their saved settings; My Account holds the
  // presentation, Notifications -> Statistics Reporting the configuration.
  expect(text).toContain('Statistics reports you previously configured continue on their saved schedule.');
  expect(text).toContain('select Advanced in My Account');
  expect(text).toContain('then open Notifications → Statistics Reporting.');
  expect(text).not.toContain('shown when your presentation is Advanced, in My Account');
  // Without the presentation owner's context, a topic is read as no Admin's.
  const unread = await page.evaluate(() => {
    const auth = window.debridPulseAuth;
    Object.defineProperty(window, 'debridPulseAuth', {value: undefined, configurable: true});
    window.DPSettingsHelp.open('integration-accounts');
    const body = document.querySelector('.dp-settings-help-dialog .dp-modal-body').textContent;
    Object.defineProperty(window, 'debridPulseAuth', {value: auth, configurable: true});
    return body;
  });
  expect(unread).toContain('Admin and User accounts do not');
});

test('H11/H12/H13: switching presentation while Help is shown recomposes it live on the same section; navigation follows the eligible sections', async ({page}) => {
  await boot(page, 1440);
  await openHelp(page);
  await page.locator('#view-help .dp-help-tab[data-tab="engines"]').click();
  await expect(page.locator('#view-help [data-panel="engines"]')).toBeVisible();
  await expect(page.locator('#view-help [data-panel="engines"] summary').filter({hasText: 'Tuning aria2'})).toHaveCount(1);
  await openAccount(page);
  await choose(page, 'easy');
  await closeAccount(page);
  // Live: same section, recomposed; nothing stale under the dialog.
  await expect(page.locator('#view-help .dp-help-tab[aria-selected="true"]:not(.dp-motion-still *)')).toHaveAttribute('data-tab', 'engines');
  await expect(page.locator('#view-help [data-panel="engines"] summary').filter({hasText: 'Tuning aria2'})).toHaveCount(0);
  expect(await manual(page)).toContain('The free-space guard, retries and stalled-download recovery apply to every engine.');
  // One handler per control: an accordion opens once per click, and closes again.
  await page.locator('#view-help .dp-help-tab[data-tab="using"]').click();
  const details = page.locator('#view-help [data-panel="using"] details').first();
  const initially = await details.evaluate(node => node.open);
  await details.locator('summary').click();
  await expect.poll(() => details.evaluate(node => node.open)).toBe(!initially);
  await details.locator('summary').click();
  await expect.poll(() => details.evaluate(node => node.open)).toBe(initially);
  // Keyboard: Home / End and the arrows over the eligible collection only.
  const tab = page.locator('#view-help .dp-help-tab[aria-selected="true"]:not(.dp-motion-still *)');
  await tab.focus();
  await page.keyboard.press('End');
  await expect(page.locator('#view-help .dp-help-tab[data-tab="license"]')).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await expect(page.locator('#view-help .dp-help-tab[data-tab="quickstart"]')).toBeFocused();
  await openAccount(page);
  await choose(page, 'advanced');
  await closeAccount(page);
  await expect(page.locator('#view-help .dp-help-tab[aria-selected="true"]:not(.dp-motion-still *)')).toHaveAttribute('data-tab', 'quickstart');
  await page.locator('#view-help .dp-help-tab[data-tab="engines"]').click();
  await expect(page.locator('#view-help [data-panel="engines"] summary').filter({hasText: 'Tuning aria2'})).toHaveCount(1);
});

test('H11/H13: a ReadOnly reader has no Settings section; a jump to it lands on Quick Start, never a blank panel', async ({page}) => {
  await asContext(page, {role: 'readonly'});
  await boot(page, 320);
  await openHelp(page);
  await page.evaluate(() => window.DPHelpPage.activateTab('settings'));
  await expect(page.locator('#view-help .dp-help-tab[aria-selected="true"]:not(.dp-motion-still *)')).toHaveAttribute('data-tab', 'quickstart');
  await expect(page.locator('#view-help [data-panel="quickstart"]')).toBeVisible();
  await expect(page.locator('#view-help [data-panel="settings"]')).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  const order = [];
  for (let step = 0; step < 7; step += 1) {
    order.push(await page.locator('#view-help .dp-help-tab[aria-selected="true"]:not(.dp-motion-still *)').getAttribute('data-tab'));
    const next = page.locator('#view-help [data-help-end="next"]');
    if (step < 6) { await next.click(); await settle(page); }
  }
  expect(order).toEqual(SECTIONS.filter(id => id !== 'settings'));
  if (EVIDENCE) await page.screenshot({path: `${EVIDENCE}/help-readonly-320.png`});
});

test.describe('the password-auth runtime', () => {
  test.skip(!AUTH, 'DP_AUTH_BASE_URL is not configured');

  test('V06: a protected Standalone Administrator is told about Clear Password and its open-access consequence', async ({browser}) => {
    const reuse = adminSession.get() !== null;
    const context = await browser.newContext({baseURL: AUTH, viewport: {width: 1440, height: 914}, ...(reuse ? {storageState: adminSession.get()} : {})});
    const page = await context.newPage();
    await isolateFonts(page);
    await asProfile(page, {deployment_type: 'standalone', sign_in_required: true, has_password: true});
    if (reuse) {
      await page.goto('/');
    } else {
      await page.goto('/login');
      await page.locator('#username').fill(ADMIN.username);
      await page.locator('#password').fill(ADMIN.password);
      await page.locator('button[type="submit"]').click();
      await expect(page).not.toHaveURL(/\/login/);
      adminSession.remember(await context.storageState());
    }
    await openAccount(page);
    await expect(dialog(page).locator('[data-modal-action="clear"]')).toHaveCount(1);
    await dialog(page).locator('.dp-account-header-end .dp-settings-help-chip').click();
    const text = await topicText(page);
    for (const phrase of ['Password required to sign in: password sign-in is on now', 'Clear Password asks for your current password and then a confirmation',
      'Anyone who can reach DebridPulse can then access this installation without a password, until you set a new one in My Account.',
      'Change Password asks for your current password']) {
      expect(text).toContain(phrase);
    }
    expect(text).not.toContain('DP Username');
    if (EVIDENCE) await helpDialog(page).screenshot({path: `${EVIDENCE}/my-account-help-protected-standalone.png`});
    await context.close();
  });
});
