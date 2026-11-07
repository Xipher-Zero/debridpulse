const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Integration Delivery Profiles table below 780px
 * (fitProfilesTable), and the profile dialog's Path Summary.
 *
 * As the room narrows the table gives back its secondary information one
 * stage at a time -- 1 Categories, 2 API Tokens (each profile's tokens then
 * named, by their labels alone, beneath its name), 3 closer padding -- Paths
 * keeping what Profile and Edit leave it, each path one line, its whole
 * mapping its tooltip. Every row keeps its Edit's height. Below 780px the
 * Create/Edit dialog shows the form's two paths as one read-only mapping.
 * From 780px nothing changes.
 *
 * A real Admin session (DP_AUTH_BASE_URL) with a SYNTHETIC directory, token
 * lists and profile catalog served around the signed-in account; every write
 * is recorded and refused. */

const AUTH = process.env.DP_AUTH_BASE_URL;
const token = (id, accountId, label, clientId) => ({id, account_id: accountId, label, created_at: '2026-10-14T03:05:00Z', revoked_at: null, client_id: clientId});
const profile = (id, categories, clients, local, imported) => ({id, categories, clients, source_kinds: ['links', 'magnet', 'torrent', 'nzb_link', 'nzb'],
  representation: 'shared_path', local_root: local, import_root: imported, retention_hours: 168});
const ACCOUNTS = [
  {id: 'int-arr', role: 'integration', enabled: true, protected: '', username: 'arr', display_name: '*arr Suite'},
  {id: 'int-home', role: 'integration', enabled: true, protected: '', username: 'home', display_name: 'Homepage'}];
const BASIC = {
  tokens: {'int-arr': [token('t1', 'int-arr', 'Radarr', 'c-radarr'), token('t2', 'int-arr', 'Sonarr', 'c-sonarr')],
    'int-home': [token('t3', 'int-home', 'Homepage', 'c-home')]},
  profiles: [profile('radarr', ['radarr', 'movies'], ['c-radarr'], '/download/AriaNG Downloads', '/Volumes/SABnzbdDATA/AriaNG Downloads'),
    profile('tv-sonarr', ['tv-sonarr', 'tv'], ['c-sonarr'], '/download/tv', '/data/media/downloads/tv'),
    profile('homepage', ['homepage'], ['c-home'], '/download', '')],
};
// Long names and paths, two clients, and a client no active token names.
const RICH = {
  tokens: {...BASIC.tokens, 'int-arr': [...BASIC.tokens['int-arr'], token('t4', 'int-arr', 'Lidarr secondary library instance token', 'c-lidarr')]},
  profiles: [BASIC.profiles[0],
    profile('lidarr-secondary-library-instance', ['music', 'lidarr', 'audiobooks'], ['c-lidarr', 'c-sonarr'],
      '/download/a/very/long/folder/structure/for/the/secondary/music/library', '/mnt/user/media/music/secondary/library/with/a/long/import/path'),
    profile('ghost', ['ghost'], ['c-ghost'], '/download/ghost', '/import/ghost'),
    BASIC.profiles[2]],
};

async function open(browser, data = BASIC, viewport = {width: 1280, height: 900}, options = {}) {
  expect(AUTH, 'DP_AUTH_BASE_URL must be provided by browser CI').toBeTruthy();
  const context = await browser.newContext({baseURL: AUTH, viewport, ...options});
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
    has_password: true, oidc_identities: 0, email: ''}, ...ACCOUNTS];
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === 'GET' && path === '/api/accounts') return route.fulfill({json: {accounts}});
    const tokens = path.match(/^\/api\/accounts\/([^/]+)\/tokens$/);
    if (request.method() === 'GET' && tokens) return route.fulfill({json: {tokens: data.tokens[tokens[1]] || []}});
    if (request.method() === 'GET' && path === '/api/external/profiles') return route.fulfill({json: {revision: 1, profiles: data.profiles}});
    if (request.method() === 'GET' || path === '/api/settings/test-aria2') return route.continue();
    page.__writes.push(`${request.method()} ${path}`);
    return route.abort();
  });
  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
  await page.locator('#view-settings .dp-settings-tabs [data-tab="users"]:not(.dp-motion-still *)').first().dispatchEvent('click');
  await expect(page.locator(`tr[data-profile-row="${data.profiles.at(-1).id}"]`)).toBeVisible();
  return {context, page};
}

const settle = page => page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
async function resize(page, width) {
  await page.setViewportSize({width, height: 900});
  await settle(page);
}

// The table as drawn.
const drawnTable = page => page.evaluate(() => {
  const table = document.querySelector('.dp-settings-profiles-table');
  const wrap = table.parentElement;
  const drawn = node => node.getClientRects().length > 0;
  const lines = node => {
    const range = document.createRange();
    range.selectNodeContents(node);
    return new Set([...range.getClientRects()].filter(box => box.width > 1 && box.height > 4).map(box => Math.round(box.bottom))).size;
  };
  const rows = [...table.tBodies[0].rows];
  return {fit: table.dataset.profilesFit ?? null,
    heads: [...table.tHead.rows[0].cells].filter(drawn).map(cell => cell.textContent.trim()),
    // Each drawn column's edges, from the table's own left edge.
    columns: [...table.tHead.rows[0].cells].filter(drawn).map(cell => {
      const box = cell.getBoundingClientRect(), left = table.getBoundingClientRect().left;
      return `${(box.left - left).toFixed(1)}-${(box.right - left).toFixed(1)}`;
    }),
    cells: rows.map(row => [...row.cells].filter(drawn).length),
    heights: rows.map(row => +row.getBoundingClientRect().height.toFixed(2)),
    overflow: wrap.scrollWidth > wrap.clientWidth + 1, page: document.documentElement.scrollWidth > window.innerWidth,
    cut: rows.flatMap(row => [...row.querySelectorAll('.dp-settings-profile-name, .dp-settings-profile-token')].filter(drawn)
      .filter(node => node.scrollWidth > node.clientWidth + 1).map(node => node.textContent)),
    // Every informational line drawn on more than one line: a name, its tokens, each path, Categories, each API Tokens client.
    wrapped: rows.flatMap(row => [...row.cells].filter(drawn).flatMap(cell => cell.cellIndex === 0 ? [...cell.children].filter(drawn)
      : cell.cellIndex === 3 ? [...cell.querySelectorAll('.dp-settings-profile-path')] : cell.cellIndex === 2 ? [...cell.children] : [cell])
      .filter(node => !node.querySelector('.btn') && lines(node) > 1)).length,
    tokens: rows.map(row => { const node = row.querySelector('.dp-settings-profile-token'); return drawn(node) ? node.textContent : null; }),
    edits: rows.map(row => {
      const button = row.querySelector('[data-user-action="edit-profile"]').getBoundingClientRect();
      const cell = row.lastElementChild.getBoundingClientRect();
      return {text: row.querySelector('[data-user-action="edit-profile"]').textContent.trim(), width: Math.round(button.width), height: Math.round(button.height),
        inside: button.left >= cell.left - 0.5 && button.right <= cell.right + 0.5 && button.right <= wrap.getBoundingClientRect().right + 0.5};
    }),
    // Each row's drawn content, centred: the largest offset of any cell's content from its row's middle.
    offCentre: Math.max(...rows.map(row => {
      const box = row.getBoundingClientRect();
      const mid = (box.top + box.bottom) / 2;
      return Math.max(...[...row.cells].filter(drawn).map(cell => {
        const leaves = [...cell.querySelectorAll('*')].filter(node => drawn(node) && !node.children.length);
        if (!leaves.length) return 0;
        return Math.abs((Math.min(...leaves.map(n => n.getBoundingClientRect().top)) + Math.max(...leaves.map(n => n.getBoundingClientRect().bottom))) / 2 - mid);
      }));
    })),
    touch: [...table.querySelectorAll('.dp-settings-profile-path-lines')].every(node => node.hasAttribute('data-tooltip-touch'))};
});

const HEADS = {0: ['Profile', 'Categories', 'API Tokens', 'Paths', 'Actions'], 1: ['Profile', 'API Tokens', 'Paths', 'Actions'],
  2: ['Profile', 'Paths', 'Actions'], 3: ['Profile', 'Paths', 'Actions']};

test('the table gives back Categories, then API Tokens beneath each name, keeps its rows\' height, and narrowing and widening agree', async ({browser}) => {
  // Two catalogs, each swept both ways: longer than one view's budget.
  test.setTimeout(120000);
  for (const data of [BASIC, RICH]) {
    const {context, page} = await open(browser, data);
    const seen = new Set();
    const down = {};
    let reference = null;
    for (let width = 779; width >= 320; width -= 5) {
      await resize(page, width);
      const table = await drawnTable(page);
      const fit = Number(table.fit);
      const at = `@${width} (stage ${fit})`;
      seen.add(fit);
      down[width] = JSON.stringify([table.fit, table.columns, table.heights, table.tokens]);
      expect(table.heads, at).toEqual(HEADS[fit]);
      expect(table.cells.every(cells => cells === HEADS[fit].length), at).toBe(true);
      expect(table.overflow || table.page, at).toBe(false);
      expect(table.wrapped, at).toBe(0);
      // A name or its tokens is cut short only at the last stage.
      if (fit < 3) expect(table.cut, at).toEqual([]);
      expect(table.tokens.every(tokens => (tokens !== null) === (fit >= 2)), at).toBe(true);
      expect(table.edits.every(edit => edit.text === 'Edit' && edit.inside && edit.width === 44 && edit.height === 30), at).toBe(true);
      expect(table.touch, at).toBe(true);
      expect(table.offCentre, at).toBeLessThanOrEqual(1);
      // One height for every row at every stage: the tokens beneath a name never grow a row.
      reference ??= table.heights[0];
      expect(Math.max(...table.heights) - Math.min(...table.heights), at).toBeLessThanOrEqual(0.01);
      expect(Math.abs(table.heights[0] - reference), at).toBeLessThanOrEqual(0.01);
    }
    // Categories leaves first: the basic catalog meets every stage from four columns down.
    if (data === BASIC) expect([...seen].sort(), 'stages met').toEqual([1, 2, 3]);
    for (let width = 320; width <= 779; width += 1) {
      if (!(width in down)) continue;
      await resize(page, width);
      const table = await drawnTable(page);
      expect(JSON.stringify([table.fit, table.columns, table.heights, table.tokens]), `widening @${width}`).toBe(down[width]);
    }
    expect(page.__writes).toEqual([]);
    await context.close();
  }
});

test('the name\'s second line is its tokens\' own labels: no account, and a missing association in the table\'s own words', async ({browser}) => {
  const {context, page} = await open(browser, RICH);
  await resize(page, 390);
  const table = await drawnTable(page);
  expect(table.tokens).toEqual(['Radarr', 'Lidarr secondary library instance token, Sonarr', 'Client with no active token', 'Homepage']);
  for (const tokens of table.tokens) expect(tokens).not.toContain('*arr Suite');
  // The missing association keeps the muted detail treatment the table gives it.
  await expect(page.locator('tr[data-profile-row="ghost"] .dp-settings-profile-token')).toHaveClass(/dp-settings-users-detail/);
  await expect(page.locator('tr[data-profile-row="radarr"] .dp-settings-profile-token')).not.toHaveClass(/dp-settings-users-detail/);
  // Hidden columns leave the accessibility tree; the name and its tokens are the first cell, whole.
  const card = page.locator('.dp-settings-delivery-profiles-card');
  await expect(card.getByRole('columnheader')).toHaveText(['Profile', 'Paths', 'Actions']);
  await expect(page.locator('tr[data-profile-row="lidarr-secondary-library-instance"]').getByRole('cell').first())
    .toHaveText('lidarr-secondary-library-instanceLidarr secondary library instance token, Sonarr');
  await expect(page.locator('tr[data-profile-row="radarr"]').getByRole('button', {name: 'Edit'})).toHaveCount(1);
  await context.close();
});

test('a shortened path says its whole mapping as its tooltip, by hover, keyboard and touch; Edit opens the editor', async ({browser}) => {
  const {context, page} = await open(browser, RICH, {width: 320, height: 900}, {hasTouch: true});
  const local = RICH.profiles[1].local_root;
  const imported = RICH.profiles[1].import_root;
  const row = page.locator('tr[data-profile-row="lidarr-secondary-library-instance"]');
  const paths = row.locator('.dp-settings-profile-path-lines');
  await expect(row.locator('.dp-settings-profile-path')).toHaveText([local, `→ ${imported}`]);
  expect(await paths.evaluate(node => [...node.querySelectorAll('.dp-settings-profile-path-text')].every(text => text.scrollWidth > text.clientWidth))).toBe(true);
  await paths.scrollIntoViewIfNeeded();
  await paths.tap();
  await expect(page.locator('#dp-tooltip')).toHaveText(`${local} → ${imported}`);
  await page.locator('.dp-settings-delivery-profiles-card .card-body').tap({position: {x: 4, y: 4}});
  await expect(page.locator('#dp-tooltip')).toBeHidden();
  await paths.focus();
  await expect(page.locator('#dp-tooltip')).toHaveText(`${local} → ${imported}`);
  await page.keyboard.press('Escape');
  await row.getByRole('button', {name: 'Edit'}).tap();
  const dialog = page.locator('.dp-modal-dialog.dp-profile-dialog');
  await expect(dialog.locator('.dp-modal-title')).toHaveText('Edit Delivery Profile: lidarr-secondary-library-instance');
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await context.close();
  // A pointer's hover too.
  const pointer = await open(browser, RICH, {width: 320, height: 900});
  await pointer.page.locator('tr[data-profile-row="lidarr-secondary-library-instance"] .dp-settings-profile-path-lines').hover();
  await expect(pointer.page.locator('#dp-tooltip')).toHaveText(`${local} → ${imported}`);
  expect(pointer.page.__writes).toEqual([]);
  await pointer.context.close();
});

test('Path Summary: below 780px, after the External Import Path and before Retention, the form\'s own paths as they change', async ({browser}) => {
  const {context, page} = await open(browser, BASIC, {width: 390, height: 900});
  const dialog = page.locator('.dp-modal-dialog.dp-profile-dialog');
  const summary = dialog.locator('[data-profile-path-summary]');
  const route = () => summary.locator('.dp-profile-path-summary-line').allTextContents();
  // Edit: the profile's own mapping, placed between the two fields, with nothing to operate.
  await page.locator('[data-user-action="edit-profile"][data-profile-id="radarr"]').click();
  await expect(summary).toBeVisible();
  await expect(summary).toHaveAccessibleName('Path Summary');
  await expect(summary.locator('.form-label')).toHaveText('Path Summary');
  await expect.poll(route).toEqual(['/download/AriaNG Downloads', '→ /Volumes/SABnzbdDATA/AriaNG Downloads']);
  const order = await dialog.evaluate(node => [...node.querySelector('[data-profile-view="form"]').children].map(child => child.dataset.profileSection || (child.matches('[data-profile-path-summary]') ? 'summary' : child.className)));
  expect(order.slice(order.indexOf('import_root'), order.indexOf('import_root') + 3)).toEqual(['import_root', 'summary', 'retention_hours']);
  await expect(summary.locator('input, button, select, textarea')).toHaveCount(0);
  const look = await summary.evaluate(node => { const style = getComputedStyle(node); return {border: style.borderTopWidth, style: style.borderTopStyle}; });
  expect(look).toEqual({border: '1px', style: 'solid'});
  // Unsaved edits: the summary follows each field; an unchanged import path is one line, as in the table.
  const local = dialog.locator('[data-profile-input="local_root"]');
  const imported = dialog.locator('[data-profile-input="import_root"]');
  await imported.fill('/mnt/a/very/long/import/path/that/wraps/naturally/inside/the/modal/content/width/without/being/cut');
  await expect.poll(route).toEqual(['/download/AriaNG Downloads', '→ /mnt/a/very/long/import/path/that/wraps/naturally/inside/the/modal/content/width/without/being/cut']);
  const whole = await summary.evaluate(node => [...node.querySelectorAll('.dp-profile-path-summary-line')].every(line => line.scrollWidth <= line.clientWidth + 1));
  expect(whole, 'full paths wrap, never cut').toBe(true);
  await imported.fill('/download/AriaNG Downloads');
  await expect.poll(route).toEqual(['/download/AriaNG Downloads']);
  await local.fill('');
  await expect.poll(route).toEqual(['—', '→ /download/AriaNG Downloads']);
  // Cancel: nothing written.
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  // Create: the default path mirrors into the import path, and the summary follows both.
  await page.locator('[data-user-action="create-profile"]').click();
  await expect.poll(route).toEqual([await dialog.locator('[data-profile-input="local_root"]').inputValue()]);
  await dialog.locator('[data-profile-input="local_root"]').fill('/download/new');
  await expect.poll(route).toEqual(['/download/new']);
  await dialog.locator('[data-profile-input="import_root"]').fill('/data/new');
  await expect.poll(route).toEqual(['/download/new', '→ /data/new']);
  // From 780px the dialog is as it was: no summary.
  await resize(page, 780);
  await expect(summary).toBeHidden();
  await page.keyboard.press('Escape');
  expect(page.__writes).toEqual([]);
  await context.close();
});

test('from 780px the table is as it was: no stages, its columns, no tokens beneath names, no tap tooltip', async ({browser}) => {
  const {context, page} = await open(browser, RICH);
  for (const width of [780, 1280]) {
    await resize(page, width);
    const table = await drawnTable(page);
    expect(table.fit, `@${width}`).toBeNull();
    expect(table.heads, `@${width}`).toEqual(width === 1280 ? ['Profile', 'Categories', 'API Tokens', 'Paths', 'Retention', 'Actions'] : HEADS[0]);
    expect(table.tokens.every(tokens => tokens === null), `@${width}`).toBe(true);
    expect(await page.locator('.dp-settings-profile-path-lines[data-tooltip-touch]').count(), `@${width}`).toBe(0);
    // The API Tokens column keeps its composite labels.
    await expect(page.locator('tr[data-profile-row="radarr"] td').nth(2)).toHaveText('Radarr · *arr Suite');
  }
  await context.close();
});
