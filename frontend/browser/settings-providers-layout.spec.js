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

async function addServer(page, host) {
  await addTile(page).click();
  const card = serverCards(page).last();
  await card.locator('[data-usenet-field="host"]').fill(host);
  await card.locator('[data-usenet-action="save"]').click();
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

test.describe('the AllDebrid disclosure and its action group share one row', () => {
  const card = page => page.locator('.dp-settings-provider-card--alldebrid');
  const group = page => card(page).locator('.dp-settings-provider-advanced');
  const summary = page => card(page).locator('.dp-settings-additional > summary');
  const actions = page => card(page).locator('.dp-settings-provider-actions');
  const optionBody = page => card(page).locator('.dp-settings-additional-body');

  const boxOf = locator => locator.evaluate(el => {
    const r = el.getBoundingClientRect();
    return {top: r.top, bottom: r.bottom, left: r.left, right: r.right,
            centerY: (r.top + r.bottom) / 2};
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

  test('collapsed: Additional Settings and [Test][Save] occupy the same row', async ({page}) => {
    await expect(page.locator('.dp-settings-additional')).not.toHaveAttribute('open', /.*/);
    const head = await boxOf(summary(page));
    const bar = await boxOf(actions(page));
    // One row: the action group's vertical centre matches the summary's, and
    // it introduces no band of its own above or below that row.
    expect(Math.abs(head.centerY - bar.centerY)).toBeLessThanOrEqual(2);
    expect(bar.top).toBeGreaterThanOrEqual(head.top - 2);
    expect(bar.bottom).toBeLessThanOrEqual(head.bottom + 2);
  });

  test('collapsed: the action group is flush with the card viewport right edge', async ({page}) => {
    const bar = await boxOf(actions(page));
    const row = await boxOf(group(page));
    expect(Math.abs(row.right - bar.right)).toBeLessThanOrEqual(1);
  });

  test('collapsed: no dedicated action row is created beneath the disclosure', async ({page}) => {
    const row = await boxOf(group(page));
    const head = await boxOf(summary(page));
    // The whole group is exactly the disclosure row, not the row plus a band.
    expect(row.bottom - head.bottom).toBeLessThanOrEqual(2);
  });

  test('expanded: [Test][Save] stays right-aligned and bottom-aligned with the options', async ({page}) => {
    const collapsedRight = (await boxOf(actions(page))).right;
    await summary(page).click();
    await expect(optionBody(page)).toBeVisible();
    const bar = await boxOf(actions(page));
    const body = await boxOf(optionBody(page));
    const row = await boxOf(group(page));
    expect(Math.abs(bar.right - collapsedRight)).toBeLessThanOrEqual(1);
    // Vertically aligned with the bottom row of the expanded option controls.
    expect(Math.abs(bar.bottom - body.bottom)).toBeLessThanOrEqual(2);
    // And no blank band beneath the option grid.
    expect(row.bottom - body.bottom).toBeLessThanOrEqual(2);
  });

  test('Save sits immediately to the right of Test', async ({page}) => {
    const testButton = await boxOf(actions(page).locator('[data-action="test-alldebrid"]'));
    const saveButton = await boxOf(actions(page).locator('[data-action="save-alldebrid"]'));
    expect(saveButton.left).toBeGreaterThanOrEqual(testButton.right);
    expect(saveButton.left - testButton.right).toBeLessThanOrEqual(16);
    expect(Math.abs(saveButton.centerY - testButton.centerY)).toBeLessThanOrEqual(1);
  });
});
