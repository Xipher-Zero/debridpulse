const { test, expect } = require('@playwright/test');

/* DP 1.0.14 User Accounts table below 780px (fitUsersTable).
 *
 * As the room narrows the table gives back its secondary information one
 * stage at a time -- 1 the emails, 2 the Sign-In column, 3 the Status column
 * (each name then in its account's enabled/disabled colour, the state still
 * said to assistive technology), 4 closer padding -- and the built-in
 * Administrator's notice takes the longest of its three wordings that fits
 * the stage chosen. The Edit User dialog's line names the sign-in methods
 * below 780px. From 780px nothing changes.
 *
 * A real Admin session (DP_AUTH_BASE_URL) with a SYNTHETIC account directory
 * served around the signed-in account; every write is recorded and refused. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const directory = (self, {longName = true} = {}) => [
  {id: self, role: 'admin', enabled: true, protected: '', username: 'browser', display_name: 'Browser Admin', has_password: true, oidc_identities: 0, email: ''},
  {id: 'acct-builtin', role: 'admin', enabled: true, protected: 'default_admin', username: 'admin', display_name: 'Administrator', has_password: true, oidc_identities: 0, email: ''},
  {id: 'acct-local', role: 'user', enabled: true, protected: '', username: 'synthetic-viewer', display_name: 'Synthetic Viewer', has_password: true, oidc_identities: 0, email: 'viewer@example.invalid'},
  {id: 'acct-disabled', role: 'readonly', enabled: false, protected: '', username: 'synthetic-reader', display_name: 'Synthetic Reader', has_password: true, oidc_identities: 0, email: ''},
  {id: 'acct-oidc', role: 'user', enabled: true, protected: '', username: '', display_name: 'Synthetic Federated', has_password: false, oidc_identities: 1, email: 'federated@example.invalid'},
  {id: 'acct-multi', role: 'admin', enabled: true, protected: '', username: 'synthetic-multi', display_name: 'Synthetic Multi', has_password: true, oidc_identities: 2, email: 'multi@example.invalid'},
  ...(longName ? [{id: 'acct-long', role: 'user', enabled: false, protected: '', username: 'synthetic-extraordinarily-long-username',
    display_name: 'Synthetic Extraordinarily Long Display Name', has_password: true, oidc_identities: 0, email: 'a-very-long-secondary-address@example.invalid'}] : []),
];

// The operator's narrow composition: the built-in Administrator, the
// signed-in account (You), and three editable accounts.
const operator = self => [
  {id: 'acct-builtin', role: 'admin', enabled: true, protected: 'default_admin', username: 'admin', display_name: 'Admin', has_password: true, oidc_identities: 0, email: ''},
  {id: self, role: 'admin', enabled: true, protected: '', username: 'browser', display_name: 'Xipher', has_password: true, oidc_identities: 1, email: ''},
  {id: 'acct-a', role: 'user', enabled: true, protected: '', username: 'synthetic-a', display_name: 'Synthetic A', has_password: true, oidc_identities: 0, email: ''},
  {id: 'acct-b', role: 'readonly', enabled: false, protected: '', username: 'synthetic-b', display_name: 'Synthetic B', has_password: true, oidc_identities: 0, email: ''},
  {id: 'acct-c', role: 'user', enabled: true, protected: '', username: '', display_name: 'Synthetic C', has_password: false, oidc_identities: 1, email: ''},
];

async function open(browser, options = {}) {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const context = await browser.newContext({baseURL: AUTH, viewport: {width: 1280, height: 900}});
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
  const accounts = (options.accounts || directory)(self, options);
  page.__accounts = accounts;
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET' && path === '/api/accounts') return route.fulfill({json: {accounts}});
    if (request.method() === 'GET' && /^\/api\/accounts\/acct-[a-z]+$/.test(path)) {
      return route.fulfill({json: accounts.find(account => path.endsWith(`/${account.id}`))});
    }
    if (request.method() === 'GET' && path === '/api/external/profiles') return route.fulfill({json: {revision: 1, profiles: []}});
    if (request.method() === 'GET' || path === '/api/settings/test-aria2') return route.continue();
    page.__writes.push(`${request.method()} ${path}`);
    return route.abort();
  });
  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
  await page.locator('#view-settings .dp-settings-tabs [data-tab="users"]:not(.dp-motion-still *)').first().dispatchEvent('click');
  await expect(page.locator(`.dp-settings-users-table tr[data-account-row="${accounts.at(-1).id}"]`)).toBeVisible();
  return {context, page};
}

const settle = page => page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
async function resize(page, width) {
  await page.setViewportSize({width, height: 900});
  await settle(page);
}

// The table as drawn.
const table = page => page.evaluate(() => {
  const t = document.querySelector('.dp-settings-users-table');
  const wrap = t.parentElement;
  const drawn = node => node.getClientRects().length > 0;
  const heads = [...t.querySelectorAll('thead th')].filter(drawn).map(th => th.textContent.trim());
  const short = t.querySelector('.dp-settings-users-hint-short');
  const rows = [...t.querySelectorAll('tbody tr')].map(row => {
    const name = row.querySelector('.dp-settings-users-name');
    const state = row.querySelector('.dp-settings-users-state');
    return {id: row.dataset.accountRow, cells: [...row.children].filter(drawn).length,
      enabled: name.dataset.enabled, color: getComputedStyle(name).color, cut: name.scrollWidth > name.clientWidth + 1,
      // The state as assistive technology meets it: in the row's own name cell (only while Status is gone) or its Status cell.
      stateSaid: getComputedStyle(state).display !== 'none' ? state.textContent : null,
      statusCell: drawn(row.children[3]) ? row.children[3].textContent.trim() : null,
      email: !!row.querySelector('td:first-child > .dp-settings-users-detail')?.getClientRects().length,
      name: name.textContent,
      actionsInside: (() => { const a = row.querySelector('.dp-settings-users-actions').getBoundingClientRect(), c = row.lastElementChild.getBoundingClientRect(); return a.left >= c.left - 0.5 && a.right <= c.right + 0.5; })()};
  });
  // Each row's height; the informational cells (all but Actions) drawn on
  // more lines than their content intends -- the name one, its email one,
  // every other cell one; the notice's lines; an Edit row's height.
  const lines = node => {
    const range = document.createRange();
    range.selectNodeContents(node);
    return new Set([...range.getClientRects()].filter(box => box.width > 2 && box.height > 6).map(box => Math.round(box.bottom))).size;
  };
  const wrapped = [...t.tBodies[0].rows].flatMap(row => [...row.cells].filter(drawn).slice(0, -1).flatMap(cell => cell.cellIndex === 0
    ? [...cell.querySelectorAll(':scope > .dp-settings-users-name, :scope > .dp-settings-users-detail')].filter(drawn)
    : [cell]).filter(node => lines(node) > 1).map(node => `${row.dataset.accountRow}:${node.className || node.cellIndex}`));
  const heights = [...t.tBodies[0].rows].map(row => row.getBoundingClientRect().height);
  const editRow = t.querySelector('[data-user-action="edit"]')?.closest('tr');
  const hint = t.querySelector('.dp-settings-users-hint');
  const button = t.querySelector('[data-user-action="edit"]');
  return {fit: t.dataset.usersFit ?? null, label: t.dataset.usersLabel ?? null, heads, rows, heights, wrapped,
    spread: Math.max(...heights) - Math.min(...heights), editHeight: editRow ? editRow.getBoundingClientRect().height : null,
    // The notice as drawn (whichever wording) against an Edit button: it never sets its row's height.
    hintHeight: hint ? hint.getBoundingClientRect().height : 0, buttonHeight: button ? button.getBoundingClientRect().height : 0,
    admin: short.getClientRects().length ? getComputedStyle(short, '::before').content.replace(/"/g, '').replace(/\\a /g, ' ') : 'full',
    adminText: t.querySelector('.dp-settings-users-hint').textContent.replace(/\s+/g, ' ').trim(),
    overflow: wrap.scrollWidth > wrap.clientWidth + 1, page: document.documentElement.scrollWidth > window.innerWidth,
    padding: getComputedStyle(t.querySelector('tbody td')).paddingLeft};
});

const COLUMNS = {0: ['Account', 'Role', 'Sign-In', 'Status', 'Actions'], 1: ['Account', 'Role', 'Sign-In', 'Status', 'Actions'],
  2: ['Account', 'Role', 'Status', 'Actions'], 3: ['Account', 'Role', 'Actions'], 4: ['Account', 'Role', 'Actions']};

test('the table gives back its information in order, fits from 3 columns at 320px, and narrowing and widening agree', async ({browser}) => {
  const {context, page} = await open(browser);
  const widths = [];
  for (let width = 779; width >= 320; width -= 3) widths.push(width);
  const seen = {};
  const states = {};
  for (const width of widths) {
    await resize(page, width);
    const t = await table(page);
    const fit = Number(t.fit);
    const at = `@${width} (stage ${fit}, label ${t.label})`;
    expect(t.heads, at).toEqual(COLUMNS[fit]);
    for (const row of t.rows) {
      expect(row.cells, `${at} ${row.id}`).toBe(COLUMNS[fit].length);
      expect(row.email, `${at} ${row.id}`).toBe(fit === 0 && page.__accounts.find(a => a.id === row.id).email !== '');
      expect(row.actionsInside, `${at} ${row.id}`).toBe(true);
    }
    // Every stage but the last fits whole: no scrolling, no name cut short.
    if (fit < 4) expect([t.overflow, t.rows.some(row => row.cut)], at).toEqual([false, false]);
    expect([t.overflow, t.page], at).toEqual([false, false]);
    // Vertically too: no informational cell wraps, the notice keeps its own
    // two lines at most, and every row is one height -- an Edit row's.
    expect(t.wrapped, at).toEqual([]);
    expect(t.hintHeight, at).toBeLessThanOrEqual(t.buttonHeight + 0.5);
    expect(t.spread, at).toBeLessThanOrEqual(0.5);
    expect(Math.abs(t.heights[0] - t.editHeight), at).toBeLessThanOrEqual(0.5);
    seen[fit] = true;
    states[width] = `${t.fit}/${t.label}`;
  }
  // The stages only ever take more back as the room narrows.
  const fits = widths.map(width => Number(states[width].split('/')[0]));
  for (let index = 1; index < fits.length; index += 1) expect(fits[index], `@${widths[index]}`).toBeGreaterThanOrEqual(fits[index - 1]);
  // (This directory's long name and three-method account take Sign-In back
  // even at 779px; the full table at 779px is the operator directory's case.)
  expect(fits.at(-1)).toBe(4);
  expect(Object.keys(seen)).toEqual(expect.arrayContaining(['4']));
  // Closer padding only at the last stage.
  await resize(page, 320);
  const narrow = await table(page);
  await resize(page, 779);
  expect(Number.parseFloat(narrow.padding)).toBeLessThan(Number.parseFloat((await table(page)).padding));
  for (const width of [...widths].reverse()) {
    await resize(page, width);
    const t = await table(page);
    expect(`${t.fit}/${t.label}`, `@${width} widening`).toBe(states[width]);
  }
  // From 780px: no stage, the five columns and the full notice.
  for (const width of [780, 1280]) {
    await resize(page, width);
    const t = await table(page);
    expect([t.fit, t.label, t.heads, t.admin], `@${width}`).toEqual([null, null, COLUMNS[0], 'full']);
  }
  expect(page.__writes).toEqual([]);
  await context.close();
});

test('without a name too long for it, the three-column stage keeps its normal padding', async ({browser}) => {
  const {context, page} = await open(browser, {longName: false});
  const seen = new Set();
  for (let width = 779; width >= 320; width -= 4) {
    await resize(page, width);
    seen.add((await table(page)).fit);
  }
  expect([...seen]).toContain('3');
  await context.close();
});

test('with Status gone, each name is its account\'s state colour and the state is still said, once', async ({browser}) => {
  const {context, page} = await open(browser);
  const color = name => page.evaluate(variable => {
    const probe = document.createElement('span');
    probe.style.color = `var(${variable})`;
    document.querySelector('#view-settings').appendChild(probe);
    const value = getComputedStyle(probe).color;
    probe.remove();
    return value;
  }, name);
  const success = await color('--dp-state-success');
  const caution = await color('--dp-state-caution');
  await resize(page, 320);
  const narrow = await table(page);
  expect(narrow.heads).toEqual(COLUMNS[4]);
  for (const row of narrow.rows) {
    const account = page.__accounts.find(a => a.id === row.id);
    expect(row.enabled, row.id).toBe(String(account.enabled));
    expect(row.color, row.id).toBe(account.enabled ? success : caution);
    expect([row.stateSaid, row.statusCell], row.id).toEqual([account.enabled ? 'Enabled' : 'Disabled', null]);
    // A name cut short keeps its whole text.
    expect(row.name, row.id).toBe(account.display_name || account.username);
  }
  await expect(page.locator('tr[data-account-row="acct-long"] .dp-settings-users-name'))
    .toHaveAccessibleName('Synthetic Extraordinarily Long Display Name');
  // While Status is drawn, the state is said there alone, and names keep their colour.
  await resize(page, 779);
  const wide = await table(page);
  for (const row of wide.rows) {
    const account = page.__accounts.find(a => a.id === row.id);
    expect([row.stateSaid, row.statusCell], row.id).toEqual([null, account.enabled ? 'Enabled' : 'Disabled']);
    expect([success, caution], row.id).not.toContain(row.color);
  }
  expect(page.__writes).toEqual([]);
  await context.close();
});

test('the built-in Administrator\'s notice takes the longest wording that fits, stays an indicator, and keeps its words', async ({browser}) => {
  const {context, page} = await open(browser);
  const wordings = new Set();
  for (let width = 779; width >= 320; width -= 3) {
    await resize(page, width);
    const t = await table(page);
    wordings.add(t.admin);
    expect(t.adminText).toBe('Password managed in Authentication');
  }
  expect([...wordings].sort()).toEqual(['Built-in Admin Account', 'Default', 'full']);
  const hint = page.locator('tr[data-account-row="acct-builtin"] .dp-settings-users-hint');
  await expect(hint.locator('button, a, [tabindex], [role]')).toHaveCount(0);
  await expect(hint).not.toHaveAttribute('tabindex', /.*/);
  await resize(page, 320);
  expect((await table(page)).admin).toBe('Default');
  await resize(page, 1280);
  await expect(hint).toHaveText(/Password managed\s*in Authentication/);
  expect((await table(page)).admin).toBe('full');
  await context.close();
});

test('below 780px the Edit User line names the sign-in methods the Sign-In column names; from 780px it is as it was', async ({browser}) => {
  const {context, page} = await open(browser);
  // What the Sign-In column says, read at 1280px.
  const column = {};
  for (const id of ['acct-multi', 'acct-oidc', 'acct-local']) {
    column[id] = (await page.locator(`tr[data-account-row="${id}"] > td:nth-child(3)`).textContent()).trim();
  }
  expect(column['acct-multi']).toBe('Password (synthetic-multi) · OIDC ×2');
  for (const [width, signIn] of [[320, true], [1280, false]]) {
    await resize(page, width);
    for (const id of ['acct-multi', 'acct-oidc', 'acct-local']) {
      const account = page.__accounts.find(a => a.id === id);
      await page.locator(`tr[data-account-row="${id}"] [data-user-action="edit"]`).click();
      const dialog = page.locator('.dp-users-edit-dialog');
      await expect(dialog).toBeVisible();
      const line = dialog.locator('.dp-users-edit-identity');
      const base = [account.email, {admin: 'Admin', user: 'User', readonly: 'ReadOnly User'}[account.role], 'Enabled'].filter(Boolean).join(' · ');
      // The column's methods, each one item of the list, the list one item of the line.
      // As drawn: from 780px the methods item is not drawn, nor read.
      await expect(line, `${id} @${width}`).toHaveText(signIn ? `${base} · ${column[id].split(' · ').join(', ')}` : base, {useInnerText: true});
      if (width === 320) {
        const fits = await dialog.evaluate(node => node.scrollWidth <= node.clientWidth + 1 && node.getBoundingClientRect().right <= window.innerWidth);
        expect(fits, id).toBe(true);
        await expect(dialog.getByLabel('New Password', {exact: true})).toBeVisible();
      }
      await page.keyboard.press('Escape');
      await expect(dialog).toBeHidden();
    }
  }
  expect(page.__writes).toEqual([]);
  await context.close();
});

test('the operator\'s narrow table: every row as tall as an Edit row, the notice "Default", You centred, no scrolling', async ({browser}) => {
  const {context, page} = await open(browser, {accounts: operator});
  // Where it fits, the full table.
  await resize(page, 779);
  const wide = await table(page);
  expect([wide.fit, wide.heads, wide.admin, wide.spread <= 0.5]).toEqual(['0', COLUMNS[0], 'full', true]);
  await resize(page, 1280);
  const button = await page.locator('tr[data-account-row="acct-a"] [data-user-action="edit"]').boundingBox();
  for (const width of [320, 326, 360]) {
    await resize(page, width);
    const t = await table(page);
    const at = `@${width} (stage ${t.fit})`;
    expect(t.heads, at).toEqual(['Account', 'Role', 'Actions']);
    expect(t.admin, at).toBe('Default');
    expect([t.wrapped, t.overflow], at).toEqual([[], false]);
    expect(t.hintHeight, at).toBeLessThanOrEqual(t.buttonHeight + 0.5);
    expect(t.spread, at).toBeLessThanOrEqual(0.5);
    expect(Math.abs(t.heights[0] - t.editHeight), at).toBeLessThanOrEqual(0.5);
    // You and the notice centred in their rows, as every Edit is; no control sizes them.
    for (const id of ['acct-builtin', null, 'acct-a']) {
      const off = await page.evaluate(id => {
        const row = id ? document.querySelector(`tr[data-account-row="${id}"]`)
          : document.querySelector(`tr[data-account-row="${window.debridPulseAuth.authority.account_id}"]`);
        const actions = row.lastElementChild.querySelector(':scope > .dp-settings-users-actions > *');
        const r = row.getBoundingClientRect(), a = actions.getBoundingClientRect();
        return {off: Math.abs((a.top + a.bottom) / 2 - (r.top + r.bottom) / 2), controls: row.lastElementChild.querySelectorAll('button').length};
      }, id);
      expect(off.off, `${at} ${id || 'You'}`).toBeLessThanOrEqual(1);
      expect(off.controls, `${at} ${id || 'You'}`).toBe(id === 'acct-a' ? 1 : 0);
    }
    // Edit keeps its size.
    const edit = await page.locator('tr[data-account-row="acct-a"] [data-user-action="edit"]').boundingBox();
    expect([edit.width, edit.height], at).toEqual([button.width, button.height]);
  }
  expect(page.__writes).toEqual([]);
  await context.close();
});

test('the table refits when its accounts change: state, sign-in and a long name, with no stale stage or height', async ({browser}) => {
  const {context, page} = await open(browser, {accounts: operator});
  await resize(page, 560);
  const before = await table(page);
  // A re-read directory: Synthetic A disabled with three sign-in methods, and a long-named account.
  const a = page.__accounts.find(account => account.id === 'acct-a');
  Object.assign(a, {enabled: false, oidc_identities: 2});
  page.__accounts.push({id: 'acct-long', role: 'user', enabled: true, protected: '', username: 'synthetic-extraordinarily-long-username',
    display_name: 'Synthetic Extraordinarily Long Display Name', has_password: true, oidc_identities: 1, email: 'a-very-long-secondary-address@example.invalid'});
  await page.evaluate(() => window.loadSettings());
  await page.locator('#view-settings .dp-settings-tabs [data-tab="users"]:not(.dp-motion-still *)').first().dispatchEvent('click');
  await expect(page.locator('tr[data-account-row="acct-long"]')).toBeVisible();
  await settle(page);
  for (const width of [560, 440, 320, 440, 560, 779]) {
    await resize(page, width);
    const t = await table(page);
    const at = `@${width} (stage ${t.fit})`;
    expect([t.wrapped, t.overflow, t.page], at).toEqual([[], false, false]);
    expect(t.spread, at).toBeLessThanOrEqual(0.5);
    const row = t.rows.find(entry => entry.id === 'acct-a');
    expect(row.enabled, at).toBe('false');
    if (t.heads.includes('Status')) expect(row.statusCell, at).toBe('Disabled');
    else expect(row.stateSaid, at).toBe('Disabled');
  }
  // The new long name takes back information it did not need before, never row height.
  await resize(page, 560);
  expect(Number((await table(page)).fit)).toBeGreaterThanOrEqual(Number(before.fit));
  expect(page.__writes).toEqual([]);
  await context.close();
});
