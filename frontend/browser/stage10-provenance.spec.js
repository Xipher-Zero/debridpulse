const { test, expect } = require('@playwright/test');

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status:200, contentType:'text/css', body:''}));
}
async function openSettings(page) {
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  await expect(page.locator('.dp-settings-panel[data-panel="sources"]')).toBeVisible();
}
async function primaryTextColor(page) {
  return page.evaluate(() => {
    const probe = document.createElement('span');
    probe.style.color = 'var(--dp-text-primary)';
    document.body.appendChild(probe);
    const color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  });
}
function integrationInput(page, identity) {
  return page.locator(`[data-integration-enabled="${identity}"]`);
}
function integrationControl(page, identity) {
  return page.locator(`label[for="dp-settings-integration-${identity}-enabled"]`);
}
async function setIntegrationChecked(page, identity, value) {
  const input = integrationInput(page, identity);
  if ((await input.isChecked()) !== value) await integrationControl(page, identity).click();
  await expect(input).toBeChecked({checked:value});
}
async function saveSettings(page) {
  const responsePromise = page.waitForResponse(
    response => response.url().endsWith('/api/settings') && response.request().method() === 'PUT',
    {timeout:20000},
  );
  await page.locator('#view-settings [data-action="save"]').click();
  const response = await responsePromise;
  expect(response.ok()).toBeTruthy();
}
function listFixture(overrides = {}) {
  return {
    id:901, name:'Stage 10 fixture', status:'completed', progress:100, size_bytes:1024,
    source:'direct_link', label:'', hash:'', created_at:'2026-09-02T12:00:00Z',
    provider_provenance_status:'recorded', current_provider_id:'general_http', current_provider_name:'HTTP & HTTPS',
    delivering_provider_id:'general_http', delivering_provider_name:'HTTP & HTTPS', ...overrides,
  };
}

test('Sources & Providers exposes canonical AllDebrid and General HTTP enable controls without HTTP tuning', async ({ page }) => {
  await isolateExternalFonts(page); await page.goto('/'); await openSettings(page);
  await expect(page.locator('.dp-settings-debrid-services')).toContainText('Debrid Services');
  await expect(page.locator('.dp-settings-provider-card--alldebrid')).toContainText('AllDebrid');
  await expect(integrationControl(page, 'alldebrid')).toBeVisible();
  await expect(page.locator('.dp-settings-general-sources')).toContainText('General Sources');
  const httpCard = page.locator('.dp-settings-provider-card--general-http');
  await expect(httpCard).toContainText('HTTP & HTTPS');
  await expect(httpCard).toContainText('Direct downloads from standard HTTP and HTTPS URLs.');
  await expect(integrationControl(page, 'general_http')).toBeVisible();
  await expect(httpCard.locator('input')).toHaveCount(1);
  for (const text of ['User Agent','Timeout','Retry','Proxy']) await expect(httpCard).not.toContainText(text);
  const headerCopy = await page.locator('.dp-settings-header-copy').boundingBox();
  const tabsBox = await page.locator('.dp-settings-tabs').boundingBox();
  expect(headerCopy.y + headerCopy.height).toBeLessThanOrEqual(tabsBox.y + 1);
  await page.screenshot({path:'test-results/checkpoint-settings-dark-desktop.png', fullPage:true});
  await page.locator('#theme-toggle').click();
  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();
  const primaryText = await primaryTextColor(page);
  await expect(page.locator('.dp-settings-tabs .stab[aria-selected="true"]')).toHaveCSS('color', primaryText);
  await expect(page.locator('#sidebar .nav-item[data-view="settings"].active')).toHaveCSS('color', primaryText);
  await page.screenshot({path:'test-results/checkpoint-settings-light-desktop.png', fullPage:true});
});

test('both provider enable controls round-trip through the running backend and survive reload', async ({ page }) => {
  await isolateExternalFonts(page); await page.goto('/');
  const original = await page.request.get('/api/settings').then(response => response.json());
  const originalAd = original.integrations.alldebrid.enabled;
  const originalHttp = original.integrations.general_http.enabled;
  await openSettings(page);

  const firstAd = !originalAd;
  const firstHttp = originalHttp || !firstAd;
  await setIntegrationChecked(page, 'alldebrid', firstAd);
  await setIntegrationChecked(page, 'general_http', firstHttp);
  await saveSettings(page);
  await expect.poll(async () => {
    const s = await page.request.get('/api/settings').then(r => r.json()); return [s.integrations.alldebrid.enabled, s.integrations.general_http.enabled];
  }).toEqual([firstAd, firstHttp]);
  await page.reload(); await openSettings(page);
  await expect(integrationInput(page, 'alldebrid')).toBeChecked({checked:firstAd});
  await expect(integrationInput(page, 'general_http')).toBeChecked({checked:firstHttp});

  const secondAd = true;
  const secondHttp = !originalHttp;
  await setIntegrationChecked(page, 'alldebrid', secondAd);
  await setIntegrationChecked(page, 'general_http', secondHttp);
  await saveSettings(page);
  await expect.poll(async () => {
    const s = await page.request.get('/api/settings').then(r => r.json()); return [s.integrations.alldebrid.enabled, s.integrations.general_http.enabled];
  }).toEqual([secondAd, secondHttp]);

  await setIntegrationChecked(page, 'alldebrid', originalAd);
  await setIntegrationChecked(page, 'general_http', originalHttp);
  await saveSettings(page);
  await expect.poll(async () => {
    const s = await page.request.get('/api/settings').then(r => r.json()); return [s.integrations.alldebrid.enabled, s.integrations.general_http.enabled];
  }).toEqual([originalAd, originalHttp]);
});

test('Recent Activity shows final provider and neutral legacy unknown without URL inference', async ({ page }) => {
  await isolateExternalFonts(page);
  const items = [listFixture(), listFixture({id:902,name:'Legacy unknown',provider_provenance_status:'unknown_legacy',current_provider_id:null,current_provider_name:null,delivering_provider_id:null,delivering_provider_name:null})];
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items,total:items.length})}));
  await page.goto('/');
  await expect(page.locator('#dash-tbody tr[data-torrent-id="901"] .dp-provider-chip')).toHaveText('HTTP & HTTPS');
  await expect(page.locator('#dash-tbody tr[data-torrent-id="902"] .dp-provider-chip')).toHaveText('Unknown');
});

test('Downloads uses current provider for active transfers and delivering provider for completed transfers', async ({ page }) => {
  await isolateExternalFonts(page);
  const items = [
    listFixture({id:903,name:'Active',status:'downloading',progress:42,delivering_provider_id:null,delivering_provider_name:null,current_provider_id:'alldebrid',current_provider_name:'AllDebrid'}),
    listFixture({id:904,name:'Completed'}),
    listFixture({id:905,name:'Pending',status:'pending',progress:0,provider_provenance_status:'pending',current_provider_id:null,current_provider_name:null,delivering_provider_id:null,delivering_provider_name:null}),
  ];
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items,total:items.length})}));
  await page.goto('/'); await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect(page.locator('#t-tbody tr[data-torrent-id="903"] .dp-provider-chip')).toHaveText('AllDebrid');
  await expect(page.locator('#t-tbody tr[data-torrent-id="904"] .dp-provider-chip')).toHaveText('HTTP & HTTPS');
  await expect(page.locator('#t-tbody tr[data-torrent-id="905"] .dp-provider-chip')).toHaveText('Pending');
  await expect(page.locator('#view-torrents thead')).toContainText('Provider / Source');
});

test('Details separates safe original resource, final provider, ordered failover history, and advanced executor identity', async ({ page }) => {
  await isolateExternalFonts(page);
  const detail = {...listFixture({id:906,name:'Failover detail'}), original_resource:'https://downloads.example/file.bin?…', executors:['aria2'],
    route_attempts:[{ordinal:1,provider_id:'alldebrid',provider_name:'AllDebrid',outcome:'failed'},{ordinal:2,provider_id:'general_http',provider_name:'HTTP & HTTPS',outcome:'completed'}], files:[], source_outcomes:[], events:[]};
  await page.route(url => url.pathname === '/api/torrents/906', route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(detail)}));
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items:[detail],total:1})}));
  await page.goto('/'); await page.evaluate(() => showDetail(906));
  await expect(page.locator('.dp-detail-provider .dv')).toHaveText('HTTP & HTTPS');
  await expect(page.locator('.dp-detail-original-resource .dv')).toHaveText('https://downloads.example/file.bin?…');
  const rows = page.locator('.dp-detail-route-row'); await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText('AllDebrid'); await expect(rows.nth(0)).toContainText('Failed');
  await expect(rows.nth(1)).toContainText('HTTP & HTTPS'); await expect(rows.nth(1)).toContainText('Completed');
  await expect(page.locator('.detail-grid')).not.toContainText('aria2');
  await page.locator('.dp-detail-advanced > summary').click(); await expect(page.locator('.dp-detail-advanced-grid')).toContainText('aria2');
  await page.screenshot({path:'test-results/checkpoint-details-failover-dark-desktop.png', fullPage:true});
});

test('provider controls remain readable in light theme and narrow layout', async ({ page }) => {
  await isolateExternalFonts(page); await page.goto('/'); await openSettings(page);
  await page.locator('#theme-toggle').click();
  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();
  await page.setViewportSize({width:680,height:900});
  const httpCard = page.locator('.dp-settings-provider-card--general-http'); await expect(httpCard).toBeVisible();
  await expect(integrationControl(page, 'general_http')).toBeVisible();
  const box = await httpCard.boundingBox(); expect(box.width).toBeLessThanOrEqual(680);
  await page.screenshot({path:'test-results/checkpoint-settings-light-narrow.png', fullPage:true});
});
