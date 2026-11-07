const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Integration Accounts card body below 780px
 * (fitIntegrationAccounts).
 *
 * Each account's header keeps one row: its actions as they are, then one
 * Manage Tokens menu holding them, then that menu reading "Manage", then its
 * status said by its name's colour (and to assistive technology). The token
 * tables share one stage and one set of column widths: four columns, then
 * each token's issue day beneath its name, then each Revoke its glyph alone,
 * then closer padding -- every row the four-column rows' height. From 780px
 * nothing changes.
 *
 * A real Admin session (DP_AUTH_BASE_URL) with a SYNTHETIC account directory
 * and token lists served around the signed-in account; every write is
 * recorded and refused. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const token = (id, label, created_at) => ({id, account_id: '', label, created_at, revoked_at: null, client_id: null});
// The operator's composition: a disabled account without tokens, one with a
// token, one with several.
const BASIC = {
  accounts: [
    {id: 'int-automation', role: 'integration', enabled: false, protected: '', username: 'automation', display_name: 'Automation'},
    {id: 'int-homepage', role: 'integration', enabled: true, protected: '', username: 'homepage', display_name: 'Homepage'},
    {id: 'int-arr', role: 'integration', enabled: true, protected: '', username: 'arr-suite', display_name: '*arr Suite'}],
  tokens: {
    'int-automation': [],
    'int-homepage': [token('tok-h1', 'Homepage', '2026-11-02T18:20:11Z')],
    'int-arr': [token('tok-a1', 'Sonarr', '2026-10-14T03:05:00Z'), token('tok-a2', 'Radarr', '2026-10-14T03:06:00Z'),
      token('tok-a3', 'Prowlarr', '2025-12-30T22:41:09Z')]},
};
// Long names, a token without an issue time, a protected account (no Delete).
const RICH = {
  accounts: [...BASIC.accounts,
    {id: 'int-long', role: 'integration', enabled: true, protected: 'system', username: 'long', display_name: 'Extraordinarily Long Integration Account Name'},
    {id: 'int-off', role: 'integration', enabled: false, protected: '', username: 'off', display_name: 'Retired Bridge'}],
  tokens: {...BASIC.tokens,
    'int-arr': [...BASIC.tokens['int-arr'], token('tok-a4', 'Lidarr secondary library instance', '2026-11-07T17:00:00Z')],
    'int-long': [token('tok-l1', 'x', ''), token('tok-l2', 'Overseerr request bridge', '2026-06-01T09:00:00Z')],
    'int-off': [token('tok-o1', 'Legacy', '2024-03-03T03:03:03Z')]},
};

async function open(browser, data = BASIC, viewport = {width: 1280, height: 900}, options = {}) {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const {acceptRevoke, ...contextOptions} = options;
  const context = await browser.newContext({baseURL: AUTH, viewport, ...contextOptions});
  const page = await context.newPage();
  page.__writes = [];
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto('/login');
  await page.locator('#username').fill('browser');
  await page.locator('#password').fill('browser-secret');
  await page.locator('button[type="submit"]').click();
  await expect(page).not.toHaveURL(/\/login/);
  await page.waitForFunction(() => Boolean(window.debridPulseAuth?.authority?.account_id)
    && typeof window.nav === 'function' && typeof window.loadSettings === 'function');
  const self = await page.evaluate(() => window.debridPulseAuth.authority.account_id);
  const accounts = [{id: self, role: 'admin', enabled: true, protected: '', username: 'browser', display_name: 'Browser Admin',
    has_password: true, oidc_identities: 0, email: ''}, ...data.accounts];
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET' && path === '/api/accounts') return route.fulfill({json: {accounts}});
    const tokens = path.match(/^\/api\/accounts\/([^/]+)\/tokens$/);
    if (request.method() === 'GET' && tokens) return route.fulfill({json: {tokens: data.tokens[tokens[1]] || []}});
    if (request.method() === 'GET' && path === '/api/external/profiles') return route.fulfill({json: {revision: 1, profiles: []}});
    // A test that changes the directory accepts a token's revocation (synthetic data only).
    const revoked = path.match(/^\/api\/accounts\/([^/]+)\/tokens\/([^/]+)$/);
    if (options.acceptRevoke && request.method() === 'DELETE' && revoked) {
      page.__writes.push(`${request.method()} ${path}`);
      data.tokens[revoked[1]] = data.tokens[revoked[1]].filter(item => item.id !== revoked[2]);
      return route.fulfill({json: {revoked: true}});
    }
    if (request.method() === 'GET' || path === '/api/settings/test-aria2') return route.continue();
    page.__writes.push(`${request.method()} ${path}`);
    return route.abort();
  });
  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
  await page.locator('#view-settings .dp-settings-tabs [data-tab="users"]:not(.dp-motion-still *)').first().dispatchEvent('click');
  await expect(page.locator(`.dp-settings-users-integration[data-account-row="${data.accounts.at(-1).id}"]`)).toBeVisible();
  return {context, page};
}

const settle = page => page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
async function resize(page, width) {
  await page.setViewportSize({width, height: 900});
  await settle(page);
}

// The card as drawn: each account's header and each token table.
const drawnCard = page => page.evaluate(() => {
  const drawn = node => node.getClientRects().length > 0;
  const cut = node => node.scrollWidth > node.clientWidth + 1;
  const lines = node => {
    const range = document.createRange();
    range.selectNodeContents(node);
    return new Set([...range.getClientRects()].filter(box => box.width > 1 && box.height > 4).map(box => Math.round(box.bottom))).size;
  };
  const card = document.querySelector('.dp-settings-users-integrations-card');
  const heads = [...card.querySelectorAll('.dp-settings-users-integration-head')].map(head => {
    const name = head.querySelector('.dp-settings-users-name');
    const state = head.querySelector('.dp-settings-users-state');
    const status = head.querySelector('.dp-settings-users-status');
    const trigger = head.querySelector('.dp-settings-header-test-trigger');
    const short = trigger.querySelector('.dp-settings-fit-label-short');
    const mids = new Set([...head.children].filter(drawn).map(node => { const box = node.getBoundingClientRect(); return Math.round((box.top + box.bottom) / 2); }));
    return {id: head.parentElement.dataset.accountRow, fit: head.dataset.accountFit ?? null, oneRow: mids.size === 1,
      overflow: head.scrollWidth > head.clientWidth + 1, cut: cut(name), enabled: name.dataset.enabled, color: getComputedStyle(name).color,
      status: drawn(status) ? status.textContent : null,
      stateSaid: getComputedStyle(state).display !== 'none' ? state.textContent : null,
      buttons: [...head.querySelectorAll(':scope > .dp-settings-users-actions > .btn')].filter(drawn).map(button => button.textContent.trim()),
      trigger: drawn(trigger) ? (drawn(short) ? getComputedStyle(short, '::before').content.replace(/"/g, '') : trigger.textContent.trim()) : null};
  });
  const tables = [...card.querySelectorAll('.dp-settings-users-tokens')].map(table => {
    const section = table.closest('.dp-settings-users-integration');
    const rows = [...table.tBodies[0].rows];
    return {id: section.dataset.accountRow, fit: table.dataset.tokensFit ?? null,
      heads: [...table.tHead.rows[0].cells].filter(drawn).map(cell => cell.textContent.trim()),
      // Each drawn column's left and right edge.
      columns: [...table.tHead.rows[0].cells].filter(drawn).map(cell => { const box = cell.getBoundingClientRect(); return `${box.left.toFixed(1)}-${box.right.toFixed(1)}`; }),
      heights: rows.map(row => +row.getBoundingClientRect().height.toFixed(2)),
      cells: rows.map(row => [...row.cells].filter(drawn).length),
      // Every informational line on more lines than it intends (name one, day one, every other cell one).
      wrapped: rows.flatMap(row => [...row.cells].filter(drawn).flatMap(cell => cell.cellIndex === 0 ? [...cell.children].filter(drawn) : [cell])
        .filter(node => !node.querySelector('.btn') && lines(node) > 1).map(node => node.className || node.cellIndex)).length,
      day: rows.map(row => { const day = row.querySelector('.dp-settings-users-token-issued'); return drawn(day) ? day.textContent : null; }),
      revoke: rows.map(row => {
        const button = row.querySelector('[data-user-action="revoke-token"]');
        const box = button.getBoundingClientRect(), cell = button.closest('td').getBoundingClientRect();
        return {icon: drawn(button.querySelector('.dp-row-action-icon')), width: Math.round(box.width), height: Math.round(box.height),
          inside: box.left >= cell.left - 0.5 && box.right <= cell.right + 0.5 && box.right <= section.getBoundingClientRect().right + 0.5};
      }),
      overflow: section.scrollWidth > section.clientWidth + 1};
  });
  return {heads, tables, page: document.documentElement.scrollWidth > window.innerWidth};
});

const STAGE_HEADS = {0: ['Token', 'Issued', 'Status', 'Actions'], 1: ['Token', 'Status', 'Actions'], 2: ['Token', 'Status', 'Actions'], 3: ['Token', 'Status', 'Actions']};

test('each account header keeps one row: actions, then Manage Tokens, then Manage, then its status as its name\'s colour', async ({browser}) => {
  const {context, page} = await open(browser, RICH);
  const seen = new Set();
  for (let width = 779; width >= 320; width -= 3) {
    await resize(page, width);
    const {heads} = await drawnCard(page);
    for (const head of heads) {
      const at = `@${width} ${head.id} (step ${head.fit})`;
      const fit = Number(head.fit);
      seen.add(`${head.id}:${fit}`);
      expect(head.oneRow, at).toBe(true);
      expect(head.overflow, at).toBe(false);
      // Each composition is chosen only where the one before it cuts the name short.
      if (fit < 3) expect(head.cut, at).toBe(false);
      expect(head.buttons.length > 0, at).toBe(fit === 0);
      expect(head.trigger, at).toBe(fit === 0 ? null : fit === 1 ? 'Manage Tokens' : 'Manage');
      // The status leaves only at the last step, and its state is then said beside the name, once.
      expect(head.status, at).toBe(fit === 3 ? null : head.enabled === 'true' ? 'Enabled' : 'Disabled');
      expect(head.stateSaid, at).toBe(fit === 3 ? (head.enabled === 'true' ? 'Enabled' : 'Disabled') : null);
      if (fit === 3) expect(head.color, at).toBe(head.enabled === 'true' ? 'rgb(0, 230, 118)' : 'rgb(255, 202, 40)');
    }
  }
  // The full ladder is met: the actions as they are, both menu labels, and the coloured name.
  for (const step of ['int-homepage:0', 'int-homepage:1', 'int-homepage:2', 'int-long:3']) expect([...seen], step).toContain(step);
  // At 320px an account whose name fits keeps its status.
  await resize(page, 320);
  const narrow = await drawnCard(page);
  expect(narrow.heads.find(head => head.id === 'int-homepage').status).toBe('Enabled');
  expect(narrow.heads.find(head => head.id === 'int-automation').status).toBe('Disabled');
  expect(page.__writes).toEqual([]);
  await context.close();
});

test('Manage holds exactly the account\'s own actions, by their full names, and each runs its existing handler', async ({browser}) => {
  const {context, page} = await open(browser, RICH);
  await resize(page, 320);
  const head = id => page.locator(`.dp-settings-users-integration[data-account-row="${id}"] > .dp-settings-users-integration-head`);
  const expected = {'int-automation': ['Enable', 'Delete'], 'int-homepage': ['Issue Token', 'Disable', 'Delete'],
    'int-long': ['Issue Token', 'Disable'], 'int-off': ['Enable', 'Delete']};
  for (const [id, names] of Object.entries(expected)) {
    const trigger = head(id).locator('.dp-settings-header-test-trigger');
    const menu = head(id).locator('.dp-settings-header-test-menu');
    // The visible "Manage" is the same control: its name is its full words.
    await expect(trigger, id).toHaveAccessibleName('Manage Tokens');
    // Keyboard: Enter opens it on its first action; Escape closes it to the trigger.
    await trigger.focus();
    await page.keyboard.press('Enter');
    await expect(menu, id).toBeVisible();
    await expect(menu.locator('button'), id).toHaveText(names);
    await expect(menu.locator('button').first(), id).toBeFocused();
    const inside = await menu.evaluate(node => {
      const box = node.getBoundingClientRect();
      return box.left >= 0 && box.right <= window.innerWidth && box.top >= 0 && box.bottom <= window.innerHeight;
    });
    expect(inside, id).toBe(true);
    await page.keyboard.press('Escape');
    await expect(menu, id).toBeHidden();
    await expect(trigger, id).toBeFocused();
  }
  const dialog = page.locator('.dp-modal-overlay .dp-modal-dialog').last();
  // Disable and Delete: the menu closes and the existing confirmation opens; refused, nothing is written.
  for (const name of ['Disable', 'Delete']) {
    await head('int-homepage').locator('.dp-settings-header-test-trigger').click();
    await head('int-homepage').locator('.dp-settings-header-test-menu').getByRole('button', {name, exact: true}).click();
    await expect(head('int-homepage').locator('.dp-settings-header-test-menu')).toBeHidden();
    await expect(dialog, name).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(dialog, name).toBeHidden();
  }
  // Issue Token: the existing issue dialog, for this account.
  await head('int-homepage').locator('.dp-settings-header-test-trigger').click();
  await head('int-homepage').locator('.dp-settings-header-test-menu').getByRole('button', {name: 'Issue Token'}).click();
  await expect(dialog).toContainText('Issue Token: Homepage');
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  // Enable runs at once, as the button does: its one request (refused here).
  await head('int-automation').locator('.dp-settings-header-test-trigger').click();
  await head('int-automation').locator('.dp-settings-header-test-menu').getByRole('button', {name: 'Enable'}).click();
  await expect.poll(() => page.__writes).toEqual(['PATCH /api/accounts/int-automation']);
  // From 780px: the actions as they were, no menu.
  await resize(page, 780);
  await expect(head('int-homepage').locator('.dp-settings-header-test-trigger')).toBeHidden();
  await expect(head('int-homepage').locator(':scope > .dp-settings-users-actions > button')).toHaveText(['Issue Token', 'Disable', 'Delete']);
  await context.close();
});

test('every token table shares its columns, keeps the four-column row height, and fits Token, Status and Actions at 320px', async ({browser}) => {
  for (const data of [BASIC, RICH]) {
    const {context, page} = await open(browser, data);
    const seen = new Set();
    let reference = null;
    for (let width = 779; width >= 320; width -= 3) {
      await resize(page, width);
      const {tables, page: pageOverflow} = await drawnCard(page);
      const at = `@${width}`;
      expect(pageOverflow, at).toBe(false);
      // One stage and one set of column edges for every table.
      expect(new Set(tables.map(table => table.fit)).size, at).toBe(1);
      expect(new Set(tables.map(table => table.columns.join())).size, at).toBe(1);
      const fit = Number(tables[0].fit);
      seen.add(fit);
      const heights = tables.flatMap(table => table.heights);
      reference ??= heights[0];
      for (const table of tables) {
        const where = `${at} ${table.id} (stage ${fit})`;
        expect(table.heads, where).toEqual(STAGE_HEADS[fit]);
        expect(table.cells.every(cells => cells === STAGE_HEADS[fit].length), where).toBe(true);
        expect(table.overflow, where).toBe(false);
        expect(table.wrapped, where).toBe(0);
        // The day is beneath the name exactly while Issued is not a column.
        expect(table.day.every(day => (day !== null) === (fit >= 1)), where).toBe(true);
        expect(table.revoke.every(revoke => revoke.inside && revoke.icon === (fit >= 2) && revoke.height === 30), where).toBe(true);
      }
      // Every row is one height at every stage: the day beneath the name, and the glyph alone, never grow a row.
      expect(Math.max(...heights) - Math.min(...heights), at).toBeLessThanOrEqual(0.01);
      expect(Math.abs(heights[0] - reference), at).toBeLessThanOrEqual(0.01);
    }
    expect([...seen].sort(), 'stages met').toEqual(data === BASIC ? [0, 1, 2] : [0, 1, 2, 3]);
    // A missing issue time is the application's dash, never an invented day.
    if (data === RICH) {
      await resize(page, 320);
      const day = page.locator('.dp-settings-users-integration[data-account-row="int-long"] tbody tr').first().locator('.dp-settings-users-token-issued');
      await expect(day).toHaveText('—');
    }
    expect(page.__writes).toEqual([]);
    await context.close();
  }
});

test('the four-to-three-column change keeps each row\'s height, and centres the name and its day, Status and Revoke', async ({browser}) => {
  const {context, page} = await open(browser);
  const rows = () => page.evaluate(() => [...document.querySelectorAll('.dp-settings-users-tokens tbody tr')].map(row => {
    const box = row.getBoundingClientRect();
    const mid = (box.top + box.bottom) / 2;
    const parts = [...row.cells[0].children].filter(node => node.getClientRects().length).map(node => node.getBoundingClientRect());
    const group = (Math.min(...parts.map(part => part.top)) + Math.max(...parts.map(part => part.bottom))) / 2;
    const centre = node => { const b = node.getBoundingClientRect(); return (b.top + b.bottom) / 2; };
    return {height: box.height, fit: row.closest('table').dataset.tokensFit,
      offsets: [group, centre(row.querySelector('.dp-settings-users-status')), centre(row.querySelector('[data-user-action="revoke-token"]'))].map(c => Math.abs(c - mid))};
  }));
  // The widest four-column width and the narrowest three-column one, found as drawn.
  let four = null, three = null;
  for (let width = 600; width >= 330; width -= 1) {
    await resize(page, width);
    const fit = await page.locator('.dp-settings-users-tokens').first().getAttribute('data-tokens-fit');
    if (fit === '0') four = await rows();
    else { three = await rows(); break; }
  }
  expect(four?.[0].fit).toBe('0');
  expect(three?.[0].fit).toBe('1');
  expect(three.map(row => row.height)).toEqual(four.map(row => row.height));
  for (const row of [...four, ...three]) for (const offset of row.offsets) expect(offset).toBeLessThanOrEqual(1);
  await context.close();
});

test('the day beneath a name keeps its exact time as its tooltip; a Revoke drawn as its glyph names its token and says so', async ({browser}) => {
  const {context, page} = await open(browser);
  await resize(page, 320);
  const row = page.locator('.dp-settings-users-integration[data-account-row="int-arr"] tbody tr').first();
  const day = row.locator('.dp-settings-users-token-issued .dp-downloads-date-value');
  // The Issued column has left the table and its accessibility tree.
  await expect(page.locator('.dp-settings-users-integration[data-account-row="int-arr"]').getByRole('columnheader')).toHaveText(['Token', 'Status', 'Actions']);
  await expect(row.getByRole('cell').first()).toHaveText(/^Sonarr.+$/);
  const exact = await page.evaluate(() => window.downloadsDateMarkup('2026-10-14T03:05:00Z').match(/data-tooltip="([^"]+)"/)[1]);
  await expect(day).toHaveAttribute('data-tooltip', exact);
  await day.focus();
  await expect(page.locator('#dp-tooltip')).toHaveText(exact);
  await page.keyboard.press('Escape');
  const revoke = row.locator('[data-user-action="revoke-token"]');
  await expect(revoke).toHaveAccessibleName('Revoke Sonarr');
  await revoke.hover();
  await expect(page.locator('#dp-tooltip')).toHaveText('Revoke Sonarr');
  await page.mouse.move(2, 2);
  await expect(page.locator('#dp-tooltip')).toBeHidden();
  await revoke.focus();
  await expect(page.locator('#dp-tooltip')).toHaveText('Revoke Sonarr');
  // Its press is the existing confirmation, for this token; refused, nothing is written.
  await revoke.click();
  const dialog = page.locator('.dp-modal-overlay .dp-modal-dialog').last();
  await expect(dialog).toContainText('Revoke this token?');
  await dialog.getByRole('button', {name: 'Revoke Token'}).click();
  await expect.poll(() => page.__writes).toEqual(['DELETE /api/accounts/int-arr/tokens/tok-a1']);
  // Where the text is drawn, its name and tooltip are the button's own word.
  await resize(page, 779);
  await expect(page.locator('[data-user-action="revoke-token"]').first()).toHaveAccessibleName('Revoke');
  await page.locator('[data-user-action="revoke-token"]').first().hover();
  await page.waitForTimeout(700);
  await expect(page.locator('#dp-tooltip')).toBeHidden();
  await context.close();
});

test('narrowing and widening choose the same compositions, and a changed directory refits', async ({browser}) => {
  const data = structuredClone(RICH);
  const {context, page} = await open(browser, data, undefined, {acceptRevoke: true});
  const key = card => JSON.stringify({heads: card.heads.map(head => [head.fit, head.trigger, head.status, head.color]),
    tables: card.tables.map(table => [table.fit, table.columns, table.heights])});
  const down = {};
  for (let width = 779; width >= 320; width -= 7) {
    await resize(page, width);
    down[width] = key(await drawnCard(page));
  }
  for (let width = 320; width <= 779; width += 7) {
    if (!(width in down)) continue;
    await resize(page, width);
    expect(key(await drawnCard(page)), `@${width}`).toBe(down[width]);
  }
  // At one width the long token goes (its Revoke, accepted): the card refits
  // from scratch to exactly what a fresh page with that directory draws.
  await resize(page, 390);
  const before = await drawnCard(page);
  expect(before.tables[0].fit).toBe('3');
  await page.locator('[data-user-action="revoke-token"][data-token-id="tok-a4"]').click();
  await page.locator('.dp-modal-overlay .dp-modal-dialog').last().getByRole('button', {name: 'Revoke Token'}).click();
  await expect(page.locator('[data-token-id="tok-a4"]')).toHaveCount(0);
  await settle(page);
  const after = await drawnCard(page);
  const fresh = await open(browser, data, {width: 390, height: 900});
  expect(key(after)).toBe(key(await drawnCard(fresh.page)));
  expect(after.tables[0].fit).not.toBe(before.tables[0].fit);
  expect(page.__writes).toEqual(['DELETE /api/accounts/int-arr/tokens/tok-a4']);
  await fresh.context.close();
  await context.close();
});

test('from 780px the card is as it was: no stages, its actions, four columns and text Revoke', async ({browser}) => {
  const {context, page} = await open(browser, RICH);
  for (const width of [780, 1280]) {
    await resize(page, width);
    const card = await drawnCard(page);
    for (const head of card.heads) {
      expect(head.fit, `@${width} ${head.id}`).toBeNull();
      expect(head.trigger, `@${width} ${head.id}`).toBeNull();
      expect(head.stateSaid, `@${width} ${head.id}`).toBeNull();
      expect(head.status, `@${width} ${head.id}`).toBe(head.enabled === 'true' ? 'Enabled' : 'Disabled');
    }
    for (const table of card.tables) {
      expect(table.fit, `@${width} ${table.id}`).toBeNull();
      expect(table.heads, `@${width} ${table.id}`).toEqual(STAGE_HEADS[0]);
      expect(table.day.every(day => day === null), `@${width} ${table.id}`).toBe(true);
      expect(table.revoke.every(revoke => !revoke.icon), `@${width} ${table.id}`).toBe(true);
    }
    // The account without tokens keeps its explanation, and no table.
    const empty = page.locator('.dp-settings-users-integration[data-account-row="int-automation"]');
    await expect(empty.locator('.dp-settings-users-tokens')).toHaveCount(0);
    await expect(empty).toContainText('No API tokens. Issue one to connect automation.');
  }
  await context.close();
});

test.describe('on a touch screen', () => {
  test('a tap opens Manage; a tap on a glyph Revoke shows its tooltip, the next its confirmation', async ({browser}) => {
    const {context, page} = await open(browser, BASIC, {width: 320, height: 900}, {hasTouch: true});
    const head = page.locator('.dp-settings-users-integration[data-account-row="int-homepage"] > .dp-settings-users-integration-head');
    await head.locator('.dp-settings-header-test-trigger').tap();
    await expect(head.locator('.dp-settings-header-test-menu button')).toHaveText(['Issue Token', 'Disable', 'Delete']);
    await page.keyboard.press('Escape');
    const revoke = page.locator('.dp-settings-users-integration[data-account-row="int-homepage"] [data-user-action="revoke-token"]');
    await revoke.scrollIntoViewIfNeeded();
    await revoke.tap();
    await expect(page.locator('#dp-tooltip')).toHaveText('Revoke Homepage');
    const dialog = page.locator('.dp-modal-overlay .dp-modal-dialog');
    await expect(dialog).toHaveCount(0);
    await revoke.tap();
    await expect(dialog.last()).toContainText('Revoke this token?');
    await page.keyboard.press('Escape');
    expect(page.__writes).toEqual([]);
    await context.close();
  });
});
