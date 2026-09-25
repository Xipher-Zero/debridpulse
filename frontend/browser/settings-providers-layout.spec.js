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

  test('collapsed: Additional Settings and the action group occupy the same row', async ({page}) => {
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

  test('expanded: the action group stays right-aligned and bottom-aligned with the options', async ({page}) => {
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

  /* Item 13: the localized Save is gone entirely, and Test inherits the
   * right-hand action position Save occupied -- the only geometric
   * consequence of there now being one button. */
  test('no Save action survives, and Test alone occupies the right-hand datum', async ({page}) => {
    await expect(card(page).locator('[data-action="save-alldebrid"]')).toHaveCount(0);
    await expect(actions(page).locator('button')).toHaveCount(1);
    const testButton = await boxOf(actions(page).locator('[data-action="test-alldebrid"]'));
    const row = await boxOf(group(page));
    expect(Math.abs(row.right - testButton.right)).toBeLessThanOrEqual(1);
  });

  test('the expanded Additional Settings keep every field they had', async ({page}) => {
    await summary(page).click();
    await expect(optionBody(page)).toBeVisible();
    for (const id of ['#dp-settings-field-alldebrid-rate-limit-per-minute',
                      '#dp-settings-field-poll-interval-seconds',
                      '#dp-settings-field-full-sync-interval-minutes',
                      '#dp-settings-field-upload-fail-retry-count',
                      '#dp-settings-field-upload-fail-retry-delay-minutes']) {
      await expect(page.locator(id)).toBeVisible();
    }
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
