const { test, expect } = require('@playwright/test');

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status:200, contentType:'text/css', body:''}));
}

async function keepTopbarEngineVisible(page) {
  await page.route(url => url.pathname === '/api/aria2/runtime', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({mode:'external', running:true, active:0, download_speed:0}),
  }));
}

function transferFixture(overrides = {}) {
  return {
    id: 1201,
    name: 'UI-fix provider fixture',
    status: 'completed',
    progress: 100,
    size_bytes: 1024,
    source: 'direct_link',
    label: '',
    hash: '',
    created_at: '2026-09-04T12:00:00Z',
    provider_provenance_status: 'recorded',
    current_provider_id: 'alldebrid',
    current_provider_name: 'AllDebrid',
    delivering_provider_id: 'alldebrid',
    delivering_provider_name: 'AllDebrid',
    ...overrides,
  };
}

async function topbarOrder(page) {
  return page.locator('#topbar').evaluate(topbar => Array.from(topbar.children)
    .map(node => {
      if (node.id === 'topbar-actions') return 'global-actions';
      if (node.id === 'aria2-speed-badge') return 'engine-widget';
      if (node.classList && node.classList.contains('topbar-theme-control')) return 'theme-control';
      return null;
    })
    .filter(Boolean));
}

async function assertTopbarGeometry(page, width) {
  await page.setViewportSize({width, height:900});
  await expect(page.locator('#topbar-actions')).toBeVisible();
  await expect(page.locator('#aria2-speed-badge')).toBeVisible();
  await expect(page.locator('.topbar-theme-control')).toBeVisible();

  expect(await topbarOrder(page)).toEqual(['global-actions', 'engine-widget', 'theme-control']);

  const heading = await page.locator('.dp-page-heading').boundingBox();
  const actions = await page.locator('#topbar-actions').boundingBox();
  const engine = await page.locator('#aria2-speed-badge').boundingBox();
  const theme = await page.locator('.topbar-theme-control').boundingBox();
  const topbar = await page.locator('#topbar').boundingBox();

  expect(heading).not.toBeNull();
  expect(actions).not.toBeNull();
  expect(engine).not.toBeNull();
  expect(theme).not.toBeNull();
  expect(topbar).not.toBeNull();
  expect(heading.x + heading.width).toBeLessThanOrEqual(actions.x + 1);
  expect(actions.x + actions.width).toBeLessThanOrEqual(engine.x + 1);
  expect(engine.x + engine.width).toBeLessThanOrEqual(theme.x + 1);
  expect(theme.x + theme.width).toBeLessThanOrEqual(topbar.x + topbar.width + 1);
}

async function providerChipContract(page, selector) {
  return page.locator(selector).evaluate(element => {
    const style = getComputedStyle(element);
    const box = element.getBoundingClientRect();
    return {
      radius: Number.parseFloat(style.borderTopLeftRadius),
      height: box.height,
      borderWidth: Number.parseFloat(style.borderTopWidth),
    };
  });
}

async function statusBadgeRadius(page, selector) {
  return page.locator(selector).evaluate(element => Number.parseFloat(getComputedStyle(element).borderTopLeftRadius));
}

test('dashboard topbar keeps global controls before engine widget before rightmost theme control', async ({ page }) => {
  await isolateExternalFonts(page);
  await keepTopbarEngineVisible(page);
  await page.goto('/');

  await assertTopbarGeometry(page, 1440);
  await page.screenshot({path:'test-results/checkpoint-ui-fix-ws1-p1-dark-1440.png', fullPage:true});

  await page.locator('#theme-toggle').click();
  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();
  await assertTopbarGeometry(page, 1440);
  await page.screenshot({path:'test-results/checkpoint-ui-fix-ws1-p1-light-1440.png', fullPage:true});

  await assertTopbarGeometry(page, 1180);
  await page.screenshot({path:'test-results/checkpoint-ui-fix-ws1-p1-light-1180.png', fullPage:true});

  await assertTopbarGeometry(page, 920);
  await page.screenshot({path:'test-results/checkpoint-ui-fix-ws1-p1-light-920.png', fullPage:true});
});

test('Recent Activity and Downloads share the rounded-rectangle routed-provider badge contract', async ({ page }) => {
  await isolateExternalFonts(page);
  const items = [transferFixture()];
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({items, total:items.length}),
  }));
  await page.goto('/');

  const recentChip = '#dash-tbody tr[data-torrent-id="1201"] .dp-provider-chip';
  const recentStatus = '#dash-tbody tr[data-torrent-id="1201"] .badge';
  await expect(page.locator(recentChip)).toHaveText('AllDebrid');
  const recent = await providerChipContract(page, recentChip);
  const recentCanonicalRadius = await statusBadgeRadius(page, recentStatus);
  expect(recent.borderWidth).toBeGreaterThan(0);
  expect(recent.radius).toBe(recentCanonicalRadius);
  expect(recent.radius).toBeGreaterThan(0);
  expect(recent.radius).toBeLessThan(recent.height / 2);
  await page.screenshot({path:'test-results/checkpoint-ui-fix-ws1-p1-recent-dark.png', fullPage:true});

  await page.locator('#theme-toggle').click();
  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();
  await expect(page.locator(recentChip)).toBeVisible();
  await page.screenshot({path:'test-results/checkpoint-ui-fix-ws1-p1-recent-light.png', fullPage:true});

  await page.locator('#theme-toggle').click();
  await expect.poll(() => page.evaluate(() => !document.body.classList.contains('light'))).toBeTruthy();
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  const downloadsChip = '#t-tbody tr[data-torrent-id="1201"] .dp-provider-chip';
  const downloadsStatus = '#t-tbody tr[data-torrent-id="1201"] .badge';
  await expect(page.locator(downloadsChip)).toHaveText('AllDebrid');
  const downloads = await providerChipContract(page, downloadsChip);
  const downloadsCanonicalRadius = await statusBadgeRadius(page, downloadsStatus);
  expect(downloads.borderWidth).toBeGreaterThan(0);
  expect(downloads.radius).toBe(downloadsCanonicalRadius);
  expect(downloads.radius).toBe(recent.radius);
  expect(downloads.radius).toBeLessThan(downloads.height / 2);

  await page.screenshot({path:'test-results/checkpoint-ui-fix-ws1-p1-downloads-dark.png', fullPage:true});
  await page.locator('#theme-toggle').click();
  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();
  await expect(page.locator(downloadsChip)).toBeVisible();
  await page.screenshot({path:'test-results/checkpoint-ui-fix-ws1-p1-downloads-light.png', fullPage:true});
});
