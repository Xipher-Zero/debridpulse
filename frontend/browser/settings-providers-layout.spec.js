const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Settings interaction foundation -- items 2, 4 and 5.
 *
 * Geometry is measured against the RENDERED layout and the element's OWN
 * available viewport, never against guessed pixel constants: the invariants
 * are "centred in the space it has" and "one shared row", both of which must
 * survive a different viewport width and a different number of cards.
 */

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

async function openSources(page) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await page.locator('#view-settings [data-tab="sources"]').click();
  await expect(page.locator('.dp-settings-panel[data-panel="sources"]')).toBeVisible();
}

const collection = page => page.locator('[data-usenet-collection]');
const serverCards = page => collection(page).locator('[data-usenet-server-id]');
const addTile = page => collection(page).locator('[data-usenet-action="add"]');

async function storedServers(page) {
  const settings = await page.request.get('/api/settings').then(r => r.json());
  return settings.integrations.usenet?.options?.servers || [];
}

async function resetServers(page) {
  for (const server of await storedServers(page)) {
    await page.request.delete(`/api/usenet/servers/${server.id}`);
  }
}

async function enableUsenet(page) {
  const toggle = page.locator('[data-integration-enabled="usenet"]');
  if (!(await toggle.isChecked())) {
    await page.locator('label[for="dp-settings-integration-usenet-enabled"]').click();
  }
  await expect.poll(async () =>
    (await page.request.get('/api/settings').then(r => r.json())).integrations.usenet.enabled).toBe(true);
  // Expansion is local presentation state, never a projection of enabled
  // state, so a card an earlier case already enabled still arrives collapsed.
  // These cases measure the card's contents, so they open it the way the
  // operator does -- through the one canonical disclosure.
  const card = page.locator('.dp-settings-provider-card--usenet');
  const disclosure = card.locator('.dp-settings-disclosure');
  if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
  await expect(card).not.toHaveClass(/dp-settings-provider-card--collapsed/);
}

/* Every rendered row of the server collection, measured against the
 * collection's own content box -- the available Usenet card viewport. */
async function rows(page) {
  return page.evaluate(() => {
    const host = document.querySelector('[data-usenet-collection]');
    const style = getComputedStyle(host);
    const box = host.getBoundingClientRect();
    const left = box.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft);
    const right = box.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight);
    const grouped = new Map();
    for (const child of host.children) {
      const rect = child.getBoundingClientRect();
      const key = Math.round(rect.top);
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(rect);
    }
    return [...grouped.entries()].sort((a, b) => a[0] - b[0]).map(([, rects]) => ({
      count: rects.length,
      leading: Math.min(...rects.map(r => r.left)) - left,
      trailing: right - Math.max(...rects.map(r => r.right)),
    }));
  });
}

/* DP 1.0.13: there is no Save. A draft card becomes a record when its Host
 * has a value and an ordinary changed-blur boundary is crossed. */
async function addServer(page, host) {
  await addTile(page).click();
  const card = serverCards(page).last();
  const field = card.locator('[data-usenet-field="host"]');
  await field.fill(host);
  await field.blur();
  await expect.poll(async () => (await storedServers(page)).some(s => s.host === host)).toBeTruthy();
}

test.describe('Usenet server cards centre on their own viewport', () => {
  test.beforeEach(async ({page}) => {
    await isolateExternalFonts(page);
    await page.goto('/');
    await openSources(page);
    await resetServers(page);
    await page.reload();
    await openSources(page);
    await enableUsenet(page);
  });

  test.afterEach(async ({page}) => { await resetServers(page); });

  test('the Add Server tile alone is centred, not packed left', async ({page}) => {
    await expect(serverCards(page)).toHaveCount(0);
    const [row] = await rows(page);
    expect(row.count).toBe(1);
    expect(Math.abs(row.leading - row.trailing)).toBeLessThanOrEqual(1);
  });

  test('one configured server and the tile centre as one group', async ({page}) => {
    await addServer(page, 'news.one.example.com');
    const measured = await rows(page);
    expect(measured.length).toBe(1);
    expect(measured[0].count).toBe(2);
    expect(Math.abs(measured[0].leading - measured[0].trailing)).toBeLessThanOrEqual(1);
  });

  test('two configured servers stay centred as they expand outward', async ({page}) => {
    await addServer(page, 'news.one.example.com');
    await addServer(page, 'news.two.example.com');
    for (const row of await rows(page)) {
      expect(Math.abs(row.leading - row.trailing)).toBeLessThanOrEqual(1);
    }
  });

  test('every rendered row stays centred when the collection wraps', async ({page}) => {
    for (const host of ['news.one.example.com', 'news.two.example.com',
                        'news.three.example.com', 'news.four.example.com']) {
      await addServer(page, host);
    }
    await page.setViewportSize({width: 900, height: 1000});
    const measured = await rows(page);
    expect(measured.length).toBeGreaterThan(1);
    for (const row of measured) {
      expect(Math.abs(row.leading - row.trailing),
        `row of ${row.count} is not centred`).toBeLessThanOrEqual(1);
    }
    await page.setViewportSize({width: 1440, height: 1000});
  });
});

/* DP 1.0.13 final interaction pass -- the provider card's operational header
 * rail, and the tuning-only disclosure beneath it.
 *
 * Test is a PROVIDER-level action, so it lives in the header beside the state it
 * proves and the participation control it is about. Every invariant below is
 * measured against the RENDERED layout, and the point of them is that the rail
 * does not move: not when the body opens, not when the credential state
 * changes, not when the body grows. */
test.describe('the AllDebrid card header rail carries state, Test and Enable', () => {
  const card = page => page.locator('.dp-settings-provider-card--alldebrid');
  const header = page => card(page).locator(':scope > .card-header');
  const rail = page => header(page).locator('.dp-settings-card-header-controls');
  const status = page => rail(page).locator('.dp-settings-provider-config-status');
  const testButton = page => rail(page).locator('[data-action="test-alldebrid"]');
  const enable = page => rail(page).locator('.dp-settings-integration-header-enable');
  const summary = page => card(page).locator('.dp-settings-additional > summary');
  const optionBody = page => card(page).locator('.dp-settings-additional-body');
  // A cell is a cell wherever it sits: directly on the line, or inside a
  // relationship group that draws a light shared outline around it.
  const cells = page => card(page).locator('.dp-settings-tuning-grid .dp-settings-field');

  const boxOf = locator => locator.evaluate(el => {
    const r = el.getBoundingClientRect();
    return {top: r.top, bottom: r.bottom, left: r.left, right: r.right,
            width: r.width, height: r.height,
            centerX: (r.left + r.right) / 2, centerY: (r.top + r.bottom) / 2};
  });

  /* The card body is hidden while the integration is switched off. Expanding
   * the card is LOCAL presentation and writes no canonical state, so this
   * geometry spec never depends on another spec's enable/disable timing. */
  test.beforeEach(async ({page}) => {
    await isolateExternalFonts(page);
    await page.goto('/');
    await openSources(page);
    await expect(card(page)).toBeVisible();
    const disclosure = card(page).locator('.dp-settings-disclosure');
    if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
    await expect(card(page).locator(':scope > .card-body')).toBeVisible();
  });

  test('the rail reads state, then Test, then Enable/toggle', async ({page}) => {
    // Semantic order, read off the rendered DOM.
    const order = await rail(page).evaluate(el => Array.from(el.children).map(child =>
      child.matches('.dp-settings-provider-config-status') ? 'state'
        : child.matches('.dp-settings-header-action') ? 'action'
        : child.matches('.dp-settings-integration-header-enable') ? 'enable' : 'other'));
    expect(order).toEqual(['state', 'action', 'enable']);
    // Visual order, left to right, on one shared centreline.
    const [s, t, e] = [await boxOf(status(page)), await boxOf(testButton(page)),
                       await boxOf(enable(page))];
    expect(s.right).toBeLessThanOrEqual(t.left + 1);
    expect(t.right).toBeLessThanOrEqual(e.left + 1);
    expect(Math.abs(s.centerY - t.centerY)).toBeLessThanOrEqual(2);
    expect(Math.abs(t.centerY - e.centerY)).toBeLessThanOrEqual(2);
    await expect(enable(page)).toContainText('Enable');
  });

  test('Test stays in the header rail collapsed and expanded, and the body never holds it',
    async ({page}) => {
      await expect(page.locator('.dp-settings-additional')).not.toHaveAttribute('open', /.*/);
      const collapsed = await boxOf(testButton(page));
      // Test is the header's, so the body cannot hold a copy of it in either state.
      await expect(card(page).locator(':scope > .card-body [data-action="test-alldebrid"]'))
        .toHaveCount(0);

      await summary(page).click();
      await expect(optionBody(page)).toBeVisible();
      const expanded = await boxOf(testButton(page));
      // The body grew by a whole option grid; the rail did not move at all.
      expect(Math.abs(expanded.top - collapsed.top)).toBeLessThanOrEqual(1);
      expect(Math.abs(expanded.left - collapsed.left)).toBeLessThanOrEqual(1);
      await expect(card(page).locator(':scope > .card-body [data-action="test-alldebrid"]'))
        .toHaveCount(0);
      await expect(testButton(page)).toHaveCount(1);
      // Still exactly one Test on the page.
      await expect(page.locator('[data-action="test-alldebrid"]')).toHaveCount(1);
    });

  test('Test is not in the credential row, and clicking it writes no credential',
    async ({page}) => {
      await expect(card(page).locator('.dp-settings-alldebrid-key-row [data-action="test-alldebrid"]'))
        .toHaveCount(0);
      const writes = [];
      page.on('request', request => {
        if (new URL(request.url()).pathname === '/api/integrations/alldebrid/configuration'
            && request.method() === 'PATCH') writes.push(request.postDataJSON());
      });
      // Nothing typed, so there is no pending commit for Test to settle: a Test
      // on its own must not produce a credential write of its own.
      await testButton(page).click();
      await page.waitForTimeout(1200);
      expect(writes).toEqual([]);
    });

  test('no provider action footer survives beneath the disclosure', async ({page}) => {
    await expect(card(page).locator('[data-action="save-alldebrid"]')).toHaveCount(0);
    await expect(card(page).locator('.dp-settings-provider-actions')).toHaveCount(0);
    await expect(card(page).locator('.dp-settings-provider-advanced')).toHaveCount(0);
    // The disclosure is the whole of the card's secondary row: it is the last
    // thing in the body, so nothing sits to its right and no band follows it.
    expect(await card(page).locator(':scope > .card-body').evaluate(el =>
      el.lastElementChild.classList.contains('dp-settings-additional'))).toBe(true);
    const head = await boxOf(summary(page));
    const details = await boxOf(card(page).locator('.dp-settings-additional'));
    expect(details.bottom - head.bottom).toBeLessThanOrEqual(2);
  });

  test('expanded: exactly five compact tuning cells, each still bound to its setting',
    async ({page}) => {
      await summary(page).click();
      await expect(optionBody(page)).toBeVisible();
      await expect(cells(page)).toHaveCount(5);
      for (const id of ['#dp-settings-field-alldebrid-rate-limit-per-minute',
                        '#dp-settings-field-poll-interval-seconds',
                        '#dp-settings-field-full-sync-interval-minutes',
                        '#dp-settings-field-upload-fail-retry-count',
                        '#dp-settings-field-upload-fail-retry-delay-minutes']) {
        await expect(page.locator(id)).toBeVisible();
        // Each control is inside a cell of the grid, and none lost its binding.
        await expect(page.locator(`.dp-settings-tuning-grid ${id}`)).toHaveCount(1);
        await expect(page.locator(id)).toHaveAttribute('data-setting', /.+/);
      }
      // No Test, no Save, no footer inside the tuning region.
      await expect(optionBody(page).locator('button')).toHaveCount(0);
    });

  test('the tuning cells are bounded, centred and never overflow horizontally',
    async ({page}) => {
      await summary(page).click();
      await expect(optionBody(page)).toBeVisible();
      const grid = await boxOf(card(page).locator('.dp-settings-tuning-grid'));

      for (let i = 0; i < 5; i += 1) {
        const cell = cells(page).nth(i);
        const cellBox = await boxOf(cell);
        // Bounded: a cell does not stretch to consume the row.
        expect(cellBox.width).toBeLessThanOrEqual(220);
        // Label, control and help are each centred AS ELEMENTS on the cell axis.
        for (const part of ['.form-label', '.input', '.form-hint']) {
          const partBox = await boxOf(cell.locator(part));
          expect(Math.abs(partBox.centerX - cellBox.centerX),
            `cell ${i} ${part} is not centred`).toBeLessThanOrEqual(2);
        }
        // The value inside the control keeps the canonical field alignment.
        expect(await cell.locator('.input').evaluate(el =>
          getComputedStyle(el).textAlign)).toBe('left');
      }

      // Wrapped rows stay centred inside the grid, and nothing scrolls.
      for (const width of [1440, 900, 700, 420]) {
        await page.setViewportSize({width, height: 1000});
        await expect(optionBody(page)).toBeVisible();
        const rows = await card(page).locator('.dp-settings-tuning-grid').evaluate(el => {
          const host = el.getBoundingClientRect();
          const byTop = new Map();
          // The CELLS are the layout unit, whether or not a relationship group
          // currently wraps some of them.
          for (const child of el.querySelectorAll('.dp-settings-field')) {
            const r = child.getBoundingClientRect();
            const key = Math.round(r.top);
            if (!byTop.has(key)) byTop.set(key, []);
            byTop.get(key).push(r);
          }
          return Array.from(byTop.values()).map(boxes => ({
            leading: Math.min(...boxes.map(b => b.left)) - host.left,
            trailing: host.right - Math.max(...boxes.map(b => b.right)),
          }));
        });
        for (const row of rows) {
          expect(Math.abs(row.leading - row.trailing),
            `tuning row is not centred at ${width}px`).toBeLessThanOrEqual(2);
        }
        expect(await page.evaluate(() =>
          document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1),
          `horizontal overflow at ${width}px`).toBeTruthy();
      }
      await page.setViewportSize({width: 1440, height: 1000});
    });

  test('the rail keeps its order when it reflows at a narrow width', async ({page}) => {
    await page.setViewportSize({width: 560, height: 1000});
    await expect(testButton(page)).toBeVisible();
    // Reflow is allowed; losing the order, or overflowing, is not.
    const order = await rail(page).evaluate(el => Array.from(el.children).map(child =>
      child.matches('.dp-settings-provider-config-status') ? 'state'
        : child.matches('.dp-settings-header-action') ? 'action'
        : child.matches('.dp-settings-integration-header-enable') ? 'enable' : 'other'));
    expect(order).toEqual(['state', 'action', 'enable']);
    expect(await page.evaluate(() =>
      document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBeTruthy();
    await page.setViewportSize({width: 1440, height: 1000});
  });
});

/* DP 1.0.13 items 17-19 -- the Usenet server card's own geometry.
 *
 * Every invariant is measured against the RENDERED layout, never a guessed
 * constant, and the card owns exactly one Test and one Remove in both
 * disclosure states. */
test.describe('one Usenet server card lays its actions out structurally', () => {
  test.beforeEach(async ({page}) => {
    await isolateExternalFonts(page);
    await page.goto('/');
    await openSources(page);
    await resetServers(page);
    await page.reload();
    await openSources(page);
    await enableUsenet(page);
    await addServer(page, 'news.geometry.example.com');
  });

  test.afterEach(async ({page}) => { await resetServers(page); });

  const box = locator => locator.evaluate(el => {
    const r = el.getBoundingClientRect();
    return {top: r.top, bottom: r.bottom, left: r.left, right: r.right,
            centerX: (r.left + r.right) / 2, centerY: (r.top + r.bottom) / 2};
  });

  /* The card's own content box -- what "centred to the whole card" means. */
  const contentBox = locator => locator.evaluate(el => {
    const style = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const left = r.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft);
    const right = r.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight);
    return {left, right, centerX: (left + right) / 2};
  });

  const only = page => serverCards(page).first();

  test('exactly one Test and one Remove exist in either disclosure state', async ({page}) => {
    const card = only(page);
    await expect(card.locator('[data-usenet-action="test"]')).toHaveCount(1);
    await expect(card.locator('[data-usenet-action="remove"]')).toHaveCount(1);
    await expect(card.locator('[data-usenet-action="save"]')).toHaveCount(0);
    await card.locator('[data-usenet-advanced-toggle]').click();
    await expect(card.locator('.dp-usenet-advanced-body')).toBeVisible();
    await expect(card.locator('[data-usenet-action="test"]')).toHaveCount(1);
    await expect(card.locator('[data-usenet-action="remove"]')).toHaveCount(1);
  });

  test('collapsed: Advanced stays left and the pair shares its row, centred on the card',
    async ({page}) => {
      const card = only(page);
      const toggle = await box(card.locator('[data-usenet-advanced-toggle]'));
      const actions = await box(card.locator('.dp-usenet-actions'));
      const content = await contentBox(card);
      // One row.
      expect(Math.abs(toggle.centerY - actions.centerY)).toBeLessThanOrEqual(2);
      // Advanced is on the card's left datum.
      expect(toggle.left - content.left).toBeLessThanOrEqual(2);
      // The pair is centred on the WHOLE card, not on the leftover space to
      // the right of the disclosure.
      expect(Math.abs(actions.centerX - content.centerX)).toBeLessThanOrEqual(2);
      // And they do not overlap.
      expect(actions.left).toBeGreaterThan(toggle.right);
      // No dedicated action band beneath the row.
      const advanced = await box(card.locator('[data-usenet-advanced]'));
      expect(advanced.bottom - Math.max(toggle.bottom, actions.bottom)).toBeLessThanOrEqual(3);
    });

  test('expanded: the pair owns the final row beneath every advanced field, still centred',
    async ({page}) => {
      const card = only(page);
      await card.locator('[data-usenet-advanced-toggle]').click();
      await expect(card.locator('.dp-usenet-advanced-body')).toBeVisible();
      const body = await box(card.locator('.dp-usenet-advanced-body'));
      const actions = await box(card.locator('.dp-usenet-actions'));
      const toggle = await box(card.locator('[data-usenet-advanced-toggle]'));
      const content = await contentBox(card);
      expect(toggle.bottom).toBeLessThanOrEqual(body.top + 2);
      expect(actions.top).toBeGreaterThanOrEqual(body.bottom - 2);
      expect(Math.abs(actions.centerX - content.centerX)).toBeLessThanOrEqual(2);
    });

  test('the Priority hint sits directly beneath the Priority input, in its column',
    async ({page}) => {
      const card = only(page);
      await card.locator('[data-usenet-advanced-toggle]').click();
      await expect(card.locator('.dp-usenet-advanced-body')).toBeVisible();
      const priority = await box(card.locator('[data-usenet-field="priority"]'));
      const hint = await box(card.locator('.dp-usenet-priority-hint'));
      const connections = await box(card.locator('[data-usenet-field="connections"]'));
      // Directly beneath, with a small gap -- not a distant centred paragraph.
      expect(hint.top).toBeGreaterThanOrEqual(priority.bottom - 1);
      expect(hint.top - priority.bottom).toBeLessThanOrEqual(10);
      // Aligned to the Priority column, not centred across the card.
      expect(Math.abs(hint.left - priority.left)).toBeLessThanOrEqual(2);
      // The two tuning inputs still share one band.
      expect(Math.abs(connections.centerY - priority.centerY)).toBeLessThanOrEqual(2);
    });

  test('SSL is centred against the Host and Port input boxes themselves', async ({page}) => {
    const card = only(page);
    const host = await box(card.locator('[data-usenet-field="host"]'));
    const port = await box(card.locator('[data-usenet-field="port"]'));
    const ssl = await box(card.locator('.dp-usenet-ssl'));
    expect(Math.abs(host.centerY - port.centerY)).toBeLessThanOrEqual(1);
    expect(Math.abs(ssl.centerY - host.centerY)).toBeLessThanOrEqual(2);
    // And not centred against the label+input wrapper, whose centre sits higher.
    const wrapper = await box(card.locator('.dp-usenet-field--host'));
    expect(Math.abs(ssl.centerY - wrapper.centerY)).toBeGreaterThan(2);
  });
});

/* DP 1.0.13 -- the Downloads tuning collections.
 *
 * Network Sources, Usenet and Download Safety & Recovery share ONE tuning-cell
 * grammar with the AllDebrid region above. What is measured here is the part
 * only a real layout can answer: that a cell never stretches, that every row
 * (including a partial one) is centred, that nothing scrolls horizontally at
 * any width, and that a relationship outline is drawn ONLY while the group it
 * describes is contiguous on one row -- disappearing entirely rather than
 * splitting across a wrap.
 */
test.describe('the Downloads tuning collections share one cell grammar', () => {
  const REGIONS = [
    ['[data-executor-tuning="direct"]', 'Network Sources', 7],
    ['[data-executor-tuning="usenet"]', 'Usenet', 4],
    ['.dp-settings-download-recovery-card', 'Download Safety & Recovery', 5],
  ];

  const geom = locator => locator.evaluate(el => {
    const r = el.getBoundingClientRect();
    return {top: r.top, bottom: r.bottom, left: r.left, right: r.right,
            width: r.width, height: r.height};
  });

  test.beforeEach(async ({page}) => {
    await isolateExternalFonts(page);
    await page.setViewportSize({width: 1440, height: 1000});
    await page.goto('/');
    await page.locator('#sidebar .nav-item[data-view="settings"]').click();
    await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
    await page.locator('#view-settings [data-tab="downloads"]').click();
    await expect(page.locator('#view-settings [data-panel="downloads"]')).toBeVisible();
    // The two executor tuning cards render collapsed; open them.
    for (const id of ['direct', 'usenet']) {
      const disclosure = page.locator(`[data-executor-tuning="${id}"] .dp-settings-disclosure`);
      if ((await disclosure.getAttribute('aria-expanded')) !== 'true') await disclosure.click();
    }
  });

  test('every region renders its controls as cells of the one collection', async ({page}) => {
    for (const [selector, label, count] of REGIONS) {
      const region = page.locator(selector);
      await expect(region.locator('.dp-settings-tuning-grid'), `${label} has no tuning collection`)
        .toHaveCount(1);
      await expect(region.locator('.dp-settings-tuning-grid .dp-settings-field'), label)
        .toHaveCount(count);
      // File Allocation is an ordinary cell: no band of its own survives.
      await expect(region.locator('.dp-settings-engine-file-allocation')).toHaveCount(0);
      await expect(region.locator('.dp-settings-engine-tuning-grid')).toHaveCount(0);
    }
  });

  test('cells stay bounded, rows stay centred, and nothing scrolls at any width',
    async ({page}) => {
      for (const width of [1600, 1280, 1024, 860, 700, 520, 400]) {
        await page.setViewportSize({width, height: 1100});
        for (const [selector, label] of REGIONS) {
          const grid = page.locator(`${selector} .dp-settings-tuning-grid`);
          const host = await geom(grid);
          const cells = await grid.locator('.dp-settings-field').evaluateAll(nodes =>
            nodes.map(n => {
              const r = n.getBoundingClientRect();
              return {top: Math.round(r.top), left: r.left, right: r.right, width: r.width};
            }));
          for (const cell of cells) {
            // Bounded: a cell fits the row, it never fills it.
            expect(cell.width, `${label} cell stretched at ${width}px`).toBeLessThanOrEqual(220);
          }
          // Every row -- including a partial one -- is centred in the collection.
          const byRow = new Map();
          for (const cell of cells) {
            if (!byRow.has(cell.top)) byRow.set(cell.top, []);
            byRow.get(cell.top).push(cell);
          }
          for (const [, row] of byRow) {
            const leading = Math.min(...row.map(c => c.left)) - host.left;
            const trailing = host.right - Math.max(...row.map(c => c.right));
            expect(Math.abs(leading - trailing),
              `${label} row not centred at ${width}px`).toBeLessThanOrEqual(2);
          }
        }
        expect(await page.evaluate(() =>
          document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1),
          `horizontal overflow at ${width}px`).toBeTruthy();
      }
      await page.setViewportSize({width: 1440, height: 1000});
    });

  test('a relationship outline is drawn only while its group is contiguous on one row',
    async ({page}) => {
      let sawDrawn = false;
      let sawWithdrawn = false;

      for (const width of [1600, 1280, 1024, 860, 700, 520, 400]) {
        await page.setViewportSize({width, height: 1100});
        const groups = await page.locator('#view-settings [data-panel="downloads"] .dp-settings-tuning-group')
          .evaluateAll(nodes => nodes.map(node => {
            const style = getComputedStyle(node);
            const cells = Array.from(node.querySelectorAll('.dp-settings-field'))
              .map(c => Math.round(c.getBoundingClientRect().top));
            return {
              // `display: contents` means the group is not a layout box at all.
              drawn: style.display !== 'contents',
              outlined: style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0,
              rows: new Set(cells).size,
              span: Number(node.dataset.tuningSpan),
              members: cells.length,
            };
          }));
        expect(groups.length, 'no relationship groups rendered').toBeGreaterThan(0);

        for (const group of groups) {
          expect(group.members, 'a group lost a member').toBe(group.span);
          if (group.drawn) {
            sawDrawn = true;
            // Drawn means one unbroken row, with a real outline on it.
            expect(group.rows, `outline split across ${group.rows} rows at ${width}px`).toBe(1);
            expect(group.outlined, `a drawn group carries no outline at ${width}px`).toBe(true);
          } else {
            sawWithdrawn = true;
            // Withdrawn entirely: no box, so no outline can be drawn at all.
            expect(group.outlined).toBe(false);
          }
        }
      }
      // The rule is meaningful only if both states actually occur.
      expect(sawDrawn, 'no outline was ever drawn').toBe(true);
      expect(sawWithdrawn, 'no outline was ever withdrawn at a narrow width').toBe(true);
      await page.setViewportSize({width: 1440, height: 1000});
    });

  test('the Usenet footer is a full-width centred row beneath the cells', async ({page}) => {
    const region = page.locator('[data-executor-tuning="usenet"]');
    const footer = region.locator('.dp-settings-tuning-footer');
    await expect(footer).toHaveText(/Per-server acquisition tuning belongs to each news server under\s+Services\./);
    for (const width of [1440, 900, 600]) {
      await page.setViewportSize({width, height: 1100});
      const body = await geom(region.locator('.card-body'));
      const grid = await geom(region.locator('.dp-settings-tuning-grid'));
      const line = await geom(footer);
      // Beneath the cells...
      expect(line.top).toBeGreaterThanOrEqual(grid.bottom - 1);
      // ...spanning the card, and centred on the CARD rather than on whatever
      // width the cell collection happened to occupy.
      expect(Math.abs((line.left + line.right) / 2 - (body.left + body.right) / 2))
        .toBeLessThanOrEqual(2);
    }
    await page.setViewportSize({width: 1440, height: 1000});
  });
});
