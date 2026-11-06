const { test, expect } = require('@playwright/test');

/* DP 1.0.14 Usenet server card composition, at every width.
 *
 * One card from 320px to desktop: a ONE-row header -- the name with its Edit
 * from the leading edge, the Test/Enable island at the trailing edge, at its
 * own size -- then Host, then Port with SSL (one row while the three fit,
 * the pair beneath Host, edge to edge with it, when they do not), labels
 * above every control, Username, Password with Clear inside its border, the
 * four Advanced fields as a 2x2, and the footer: Advanced, then Remove. A
 * name too long for its room ends in an ellipsis; the whole name stays its
 * text, its tooltip and the controls' names.
 *
 * The settings document is SERVED (usenet-server-cards.spec.js owns the real
 * server collection), and every write the cards make is answered here, so
 * nothing is persisted. */

const LONG = 'Primary Backbone Provider - EU West Cluster';
const SERVERS = [
  {id: 'layout-short', host: 'news.example.com', port: 563, ssl: true, username: 'u', password: '',
   password_configured: true, connections: 8, priority: 0, articles_per_request: 2, timeout_seconds: 60,
   enabled: true, display_name: ''},
  {id: 'layout-long', host: 'eu.backbone.example.net', port: 119, ssl: false, username: '', password: '',
   password_configured: false, connections: 20, priority: 1, articles_per_request: 2, timeout_seconds: 60,
   enabled: false, display_name: LONG},
];
const USENET = {
  enabled: true, priority: 0, name: 'Usenet', kind: 'provider_executor', configured: true,
  presentation: {status_name: 'Usenet', premium: true, status_endpoint: null, static_status: 'healthy',
    display_order: 20, status_group: null, status_group_label: null,
    status_tier: 'general_family', status_tier_label: 'General'},
  options: {operation_timeout_seconds: 30, article_cache_megabytes: 1024, direct_write: true,
    max_acquisition_retries: 3, servers: SERVERS},
};

async function prepare(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  const live = await page.request.get('/api/settings').then(response => response.json());
  const fixture = JSON.parse(JSON.stringify(USENET));
  const served = {...live, integrations: {...live.integrations, usenet: fixture}};
  page.__writes = [];
  await page.route(url => url.pathname === '/api/settings', route =>
    (route.request().method() === 'GET' ? route.fulfill({json: served}) : route.fallback()));
  await page.route(url => url.pathname.startsWith('/api/usenet/servers'), route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    page.__writes.push({method: request.method(), path, body: request.postDataJSON?.() || null});
    if (path.endsWith('/test')) return route.fulfill({json: {ok: true, message: 'Connection successful'}});
    const id = path.split('/').pop();
    const server = fixture.options.servers.find(entry => entry.id === id);
    if (request.method() === 'PUT' && server) {
      for (const [key, value] of Object.entries(request.postDataJSON() || {})) {
        if (key !== 'password' && key !== 'clear_password') server[key] = value;
      }
    }
    return route.fulfill({json: {ok: true, server_id: id, servers: fixture.options.servers}});
  });
  await page.goto('/');
  await page.waitForFunction(() => typeof window.nav === 'function' && typeof window.loadSettings === 'function');
  await page.evaluate(() => window.nav(document.querySelector('#sidebar .nav-item[data-view="settings"]')));
  await page.locator('#view-settings .dp-settings-tabs [data-tab="sources"]:not(.dp-motion-still *)').first()
    .dispatchEvent('click');
  const usenet = page.locator('#view-settings .dp-settings-provider-card--usenet');
  const disclosure = usenet.locator(':scope > .card-header .dp-settings-disclosure');
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  await expect(page.locator('[data-usenet-collection] [data-usenet-server-id]')).toHaveCount(2);
  for (const toggle of await page.locator('[data-usenet-advanced-toggle]').all()) await toggle.click();
  await expect(page.locator('.dp-usenet-advanced-body:visible')).toHaveCount(2);
  await settle(page);
}

// Finite animations only: an endless one (a spinner) never finishes.
const settle = page => page.evaluate(() => Promise.all(document.getAnimations()
  .filter(motion => Number.isFinite(motion.effect?.getComputedTiming().endTime)).map(motion => motion.finished))
  .then(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))));

async function width(page, value) {
  await page.setViewportSize({width: value, height: 1000});
  await settle(page);
}

const card = (page, id) => page.locator(`[data-usenet-server-id="${id}"]`);

// One card as drawn: every region's geometry against the card's content box.
const measure = (page, id) => page.evaluate(id => {
  const node = document.querySelector(`[data-usenet-server-id="${id}"]`);
  node.scrollIntoView({block: 'start'});
  const q = selector => node.querySelector(selector);
  const r = el => { const b = el.getBoundingClientRect(); return {l: b.left, r: b.right, t: b.top, b: b.bottom, w: b.width, h: b.height, cy: (b.top + b.bottom) / 2}; };
  const style = getComputedStyle(node);
  const box = node.getBoundingClientRect();
  const content = {l: box.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft),
                   r: box.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight)};
  const name = q('[data-usenet-display-name]');
  const range = document.createRange(); range.selectNodeContents(name);
  const parts = {
    title: r(q('.dp-usenet-server-title')), name: r(name), edit: r(q('[data-usenet-action="rename"]')),
    island: r(q('.dp-usenet-server-controls')), test: r(q('[data-usenet-action="test"]')),
    toggle: r(q('.dp-usenet-enable .toggle')),
    hostLabel: r(q('.dp-usenet-field--host > .form-label')), host: r(q('[data-usenet-field="host"]')),
    portLabel: r(q('.dp-usenet-field--port > .form-label')), port: r(q('[data-usenet-field="port"]')),
    sslLabel: r(q('.dp-usenet-field--ssl > .form-label')), ssl: r(q('.dp-usenet-ssl')),
    group: r(q('.dp-usenet-port-ssl')), username: r(q('[data-usenet-field="username"]')),
    passwordField: r(q('.dp-usenet-field--password .dp-action-field')), password: r(q('[data-usenet-field="password"]')),
    clear: r(q('[data-usenet-action="clear-password"]')),
    advanced: r(q('[data-usenet-advanced-toggle]')), remove: r(q('[data-usenet-action="remove"]')),
    footer: r(q('[data-usenet-advanced]')),
    connections: r(q('[data-usenet-field="connections"]')), priority: r(q('[data-usenet-field="priority"]')),
    articles: r(q('[data-usenet-field="articles_per_request"]')), timeout: r(q('[data-usenet-field="timeout_seconds"]')),
  };
  // Every drawn descendant inside the card's content edges (focus rings aside).
  const escapes = [...node.querySelectorAll('*')].filter(el => el.getClientRects().length)
    .map(el => [el, el.getBoundingClientRect()])
    .filter(([, b]) => b.width && (b.left < content.l - 1 || b.right > content.r + 1))
    .map(([el, b]) => `${el.tagName.toLowerCase()}.${[...el.classList].join('.')} ${b.left.toFixed(1)}-${b.right.toFixed(1)}`);
  // Header parts pairwise: no two intersect.
  const header = ['name', 'edit', 'test', 'toggle'];
  const overlaps = [];
  for (let i = 0; i < header.length; i += 1) for (let j = i + 1; j < header.length; j += 1) {
    const a = parts[header[i]]; const b = parts[header[j]];
    if (Math.min(a.r, b.r) - Math.max(a.l, b.l) > 0.5 && Math.min(a.b, b.b) - Math.max(a.t, b.t) > 0.5) overlaps.push(`${header[i]}~${header[j]}`);
  }
  const hits = ['edit', 'test', 'clear', 'advanced', 'remove'].filter(key => {
    const el = q({edit: '[data-usenet-action="rename"]', test: '[data-usenet-action="test"]',
      clear: '[data-usenet-action="clear-password"]', advanced: '[data-usenet-advanced-toggle]',
      remove: '[data-usenet-action="remove"]'}[key]);
    const b = el.getBoundingClientRect();
    const hit = document.elementFromPoint((b.left + b.right) / 2, (b.top + b.bottom) / 2);
    return !(el.contains(hit) || hit?.contains(el));
  });
  const passwordStyle = getComputedStyle(q('[data-usenet-field="password"]'));
  return {content, parts, escapes, overlaps, unreachable: hits,
    text: name.textContent, tooltip: name.dataset.tooltip,
    editName: q('[data-usenet-action="rename"]').getAttribute('aria-label'),
    enableName: q('[data-usenet-field="enabled"]').getAttribute('aria-label'),
    truncated: range.getBoundingClientRect().width > name.getBoundingClientRect().width + 0.5,
    nameEllipsis: getComputedStyle(name).textOverflow,
    passwordTextEnd: parts.password.r - parseFloat(passwordStyle.paddingRight),
    overflow: node.scrollWidth - node.clientWidth,
    pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth};
}, id);

const WIDTHS = [320, 360, 390, 440, 779, 780, 1024];

test('every server card keeps its composition and fits its container at every required width', async ({page}) => {
  await prepare(page);
  const ledger = {};
  const desktop = {};
  for (const value of [1024, ...WIDTHS]) {
    await width(page, value);
    for (const server of SERVERS) {
      const m = await measure(page, server.id);
      const p = m.parts;
      const at = `${server.id} ${value}px`;
      const full = server.display_name || server.host;
      if (value === 1024 && !desktop[server.id]) desktop[server.id] = {test: p.test, toggle: p.toggle, island: p.island};

      // Nothing leaves the card, nothing overflows anywhere.
      expect(m.escapes, at).toEqual([]);
      expect(m.overflow, at).toBeLessThanOrEqual(0);
      expect(m.pageOverflow, at).toBeLessThanOrEqual(0);
      expect(m.unreachable, at).toEqual([]);

      // Header: one row; name + Edit from the leading edge; island at the trailing edge, natural size.
      for (const key of ['name', 'edit', 'island']) expect(Math.abs(p[key].cy - p.island.cy), `${at} ${key}`).toBeLessThanOrEqual(2);
      expect(Math.abs(p.name.l - m.content.l), at).toBeLessThanOrEqual(1);
      expect(Math.abs(p.island.r - m.content.r), at).toBeLessThanOrEqual(1);
      expect(p.edit.l - p.name.r, at).toBeGreaterThanOrEqual(0);
      expect(p.edit.l - p.name.r, at).toBeLessThanOrEqual(8);
      expect(m.overlaps, at).toEqual([]);
      for (const key of ['test', 'toggle', 'island']) {
        expect(Math.abs(p[key].w - desktop[server.id][key].w), `${at} ${key} width`).toBeLessThanOrEqual(0.5);
        expect(Math.abs(p[key].h - desktop[server.id][key].h), `${at} ${key} height`).toBeLessThanOrEqual(0.5);
      }
      // The whole name is the text, the tooltip and both controls' names;
      // an ellipsis only where the name takes all the room it has.
      expect(m.text, at).toBe(full);
      expect(m.tooltip, at).toBe(full);
      expect(m.editName, at).toBe(`Edit display name for ${full}`);
      expect(m.enableName, at).toBe(`Enable ${full}`);
      expect(m.nameEllipsis, at).toBe('ellipsis');
      if (m.truncated) expect(p.island.l - p.edit.r, `${at} truncated with room to spare`).toBeLessThanOrEqual(9);

      // Labels above their controls, SSL's at every width; SSL centred on Port's box.
      expect(p.hostLabel.b, at).toBeLessThanOrEqual(p.host.t + 0.5);
      expect(p.portLabel.b, at).toBeLessThanOrEqual(p.port.t + 0.5);
      expect(p.sslLabel.b, at).toBeLessThanOrEqual(p.ssl.t + 0.5);
      expect(Math.abs(p.sslLabel.cy - p.portLabel.cy), at).toBeLessThanOrEqual(1);
      expect(Math.abs(p.ssl.cy - p.port.cy), at).toBeLessThanOrEqual(1.5);
      // Port and SSL move together: beside Host, or one row beneath it edge to edge.
      const wrapped = p.port.t > p.host.b;
      if (wrapped) {
        expect(Math.abs(p.group.l - p.host.l), at).toBeLessThanOrEqual(1);
        expect(Math.abs(p.group.r - p.host.r), at).toBeLessThanOrEqual(1);
        expect(Math.abs(p.port.l - p.host.l), at).toBeLessThanOrEqual(1);
        expect(Math.abs(p.ssl.r - p.host.r), at).toBeLessThanOrEqual(6);
      } else {
        expect(Math.abs(p.port.cy - p.host.cy), at).toBeLessThanOrEqual(1);
        expect(Math.abs(p.ssl.cy - p.host.cy), at).toBeLessThanOrEqual(1.5);
      }
      // Username and Password span the form; Clear is inside Password's
      // border, trailing, centred, after the text's end.
      expect(Math.abs(p.username.r - m.content.r), at).toBeLessThanOrEqual(1);
      expect(Math.abs(p.passwordField.r - m.content.r), at).toBeLessThanOrEqual(1);
      expect(p.clear.l, at).toBeGreaterThan(p.passwordField.l);
      expect(p.passwordField.r - p.clear.r, at).toBeLessThanOrEqual(6);
      expect(Math.abs(p.clear.cy - p.passwordField.cy), at).toBeLessThanOrEqual(1);
      expect(m.passwordTextEnd, at).toBeLessThanOrEqual(p.clear.l + 0.5);
      // Advanced fields: a 2x2, inputs on one band per row, columns aligned.
      expect(Math.abs(p.connections.t - p.priority.t), at).toBeLessThanOrEqual(1);
      expect(Math.abs(p.articles.t - p.timeout.t), at).toBeLessThanOrEqual(1);
      expect(Math.abs(p.connections.l - p.articles.l), at).toBeLessThanOrEqual(1);
      expect(Math.abs(p.priority.l - p.timeout.l), at).toBeLessThanOrEqual(1);
      expect(p.priority.l, at).toBeGreaterThan(p.connections.r);
      expect(p.articles.t, at).toBeGreaterThan(p.connections.b);
      // Footer: Advanced leading, Remove trailing, one row, no band of its own.
      expect(Math.abs(p.advanced.l - m.content.l), at).toBeLessThanOrEqual(2);
      expect(Math.abs(p.remove.r - m.content.r), at).toBeLessThanOrEqual(1);
      expect(Math.abs(p.advanced.cy - p.remove.cy), at).toBeLessThanOrEqual(2);
      expect(p.remove.b, at).toBeLessThan(p.connections.t);

      ledger[`${server.id} ${value}`] = {wrapped, truncated: m.truncated};
    }
  }
  // The wide card keeps Host, Port and SSL on one row; the phone card splits them.
  expect(ledger['layout-short 1024'].wrapped).toBe(false);
  expect(ledger['layout-short 780'].wrapped).toBe(false);
  expect(ledger['layout-short 320'].wrapped).toBe(true);
  // A name is whole wherever its room holds it, and gives way where it does not.
  expect(ledger['layout-short 779'].truncated).toBe(false);
  expect(ledger['layout-long 779'].truncated).toBe(false);
  expect(ledger['layout-short 320'].truncated).toBe(true);
  expect(ledger['layout-long 320'].truncated).toBe(true);
  test.info().annotations.push({type: 'ledger', description: JSON.stringify(ledger)});
});

test('header Test, Enable, Edit, and the footer Remove keep their behaviour after the move', async ({page}) => {
  await prepare(page);
  await width(page, 320);
  const short = card(page, 'layout-short');

  // Test: one control, in the header island, the same request and toast.
  await expect(short.locator('[data-usenet-action="test"]')).toHaveCount(1);
  await expect(short.locator('.dp-usenet-server-controls [data-usenet-action="test"]')).toHaveCount(1);
  await short.locator('[data-usenet-action="test"]').click();
  await expect.poll(() => page.__writes.filter(write => write.path.endsWith('/test')).length).toBe(1);
  await expect(page.locator('.toast, .dp-toast').filter({hasText: 'Connection successful'}).first()).toBeVisible();

  // Enable: no visible word, the server's own immediate write.
  const enable = short.locator('[data-usenet-field="enabled"]');
  await expect(short.locator('.dp-usenet-enable .tl')).toHaveCount(0);
  await expect(enable).toHaveAccessibleName('Enable news.example.com');
  await short.locator('.dp-usenet-enable .toggle').click();
  await expect.poll(() => page.__writes.some(write => write.method === 'PUT'
    && write.path.endsWith('/layout-short') && write.body?.enabled === false)).toBe(true);
  await expect(enable).not.toBeChecked();

  // Rename: the shown name, tooltip and control names follow it.
  await short.locator('[data-usenet-action="rename"]').click();
  const dialog = page.locator('.dp-modal-dialog');
  await dialog.locator('input').fill(LONG + ' Two');
  await dialog.locator('[data-modal-accept]').click();
  const name = short.locator('[data-usenet-display-name]');
  await expect(name).toHaveText(LONG + ' Two');
  await expect(name).toHaveAttribute('data-tooltip', LONG + ' Two');
  await expect(short.locator('[data-usenet-action="rename"]')).toHaveAccessibleName(`Edit display name for ${LONG} Two`);
  await expect(enable).toHaveAccessibleName(`Enable ${LONG} Two`);
  const m = await measure(page, 'layout-short');
  expect(m.truncated).toBe(true);
  expect(m.overlaps).toEqual([]);

  // Clear: inside the field, keyboard-reachable, still the confirmed action; declining writes nothing.
  const before = page.__writes.length;
  const clear = short.locator('[data-usenet-action="clear-password"]');
  await expect(clear).toHaveText('Clear');
  await expect(clear).toBeEnabled();
  await short.locator('[data-usenet-field="password"]').focus();
  await page.keyboard.press('Tab');
  await expect(clear).toBeFocused();
  await page.keyboard.press('Enter');
  const danger = page.locator('.dp-modal-overlay .dp-modal-dialog[data-tone="danger"]');
  await expect(danger).toBeVisible();
  await danger.locator('[data-modal-cancel]').click();
  await expect(card(page, 'layout-long').locator('[data-usenet-action="clear-password"]')).toBeDisabled();

  // Remove: the footer's trailing action, still confirmed; declining keeps the card.
  await short.locator('[data-usenet-action="remove"]').click();
  await expect(danger).toBeVisible();
  await danger.locator('[data-modal-cancel]').click();
  await expect(page.locator('[data-usenet-server-id]')).toHaveCount(2);
  expect(page.__writes.slice(before).filter(write => write.method !== 'GET')).toEqual([]);

  // Advanced still discloses its fields.
  await short.locator('[data-usenet-advanced-toggle]').click();
  await expect(short.locator('.dp-usenet-advanced-body')).toBeHidden();
  await short.locator('[data-usenet-advanced-toggle]').click();
  await expect(short.locator('[data-usenet-field="timeout_seconds"]')).toBeVisible();

  // The Add tile spans the one track the cards do.
  const tile = await page.locator('[data-usenet-action="add"]').boundingBox();
  const cardBox = await short.boundingBox();
  expect(Math.abs(tile.width - cardBox.width)).toBeLessThanOrEqual(0.5);
});

/* DP 1.0.14 services correction: the server card's Test is text-only. No
 * flask chip, so nothing in the button is drawn or reserved for one, and the
 * room it took is the name region's: the title runs to the island. */
test('the server Test is text-only and its former icon room goes to the name', async ({page}) => {
  await prepare(page);
  for (const value of [320, 1024]) {
    await width(page, value);
    for (const id of ['layout-short', 'layout-long']) {
      const drawn = await card(page, id).evaluate(node => {
        const test = node.querySelector('[data-usenet-action="test"]');
        const style = getComputedStyle(test);
        const label = test.querySelector(':scope > span').getBoundingClientRect();
        const title = node.querySelector('.dp-usenet-server-title').getBoundingClientRect();
        const island = node.querySelector('.dp-usenet-server-controls').getBoundingClientRect();
        const name = node.querySelector('.dp-usenet-server-name');
        const edit = node.querySelector('.dp-usenet-name-edit').getBoundingClientRect();
        const px = key => parseFloat(style[key]);
        return {
          children: test.children.length,
          marks: test.querySelectorAll('img, svg, .dp-settings-action-chip').length,
          text: test.textContent.trim(),
          // Button width is its word and its padding/border: no icon slot.
          slack: test.getBoundingClientRect().width - label.width
            - px('paddingLeft') - px('paddingRight') - px('borderLeftWidth') - px('borderRightWidth'),
          // The title region ends at the header gap before the island.
          titleToIsland: island.left - title.right,
          // The name fills the title region beside Edit whenever it is cut.
          cut: name.scrollWidth > name.clientWidth + 1,
          nameRoom: title.width - edit.width - 6 - name.getBoundingClientRect().width,
        };
      });
      expect(drawn.children, `${id} @${value}`).toBe(1);
      expect(drawn.marks, `${id} @${value}`).toBe(0);
      expect(drawn.text).toBe('Test');
      expect(Math.abs(drawn.slack), `${id} @${value}`).toBeLessThanOrEqual(1);
      expect(Math.abs(drawn.titleToIsland - 8), `${id} @${value}`).toBeLessThanOrEqual(1);
      if (drawn.cut) expect(Math.abs(drawn.nameRoom), `${id} @${value}`).toBeLessThanOrEqual(1);
    }
  }
  await expect(card(page, 'layout-short').getByRole('button', {name: 'Test', exact: true})).toHaveCount(1);
});
