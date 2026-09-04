const { test, expect } = require('@playwright/test');

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({
    status: 200,
    contentType: 'text/css',
    body: '',
  }));
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function transfer(id, name, overrides = {}) {
  return {
    id,
    name,
    hash: `ws2-p2-${id}-0123456789abcdef`,
    status: 'downloading',
    progress: 15,
    size_bytes: 1024 * 1024 * id,
    created_at: '2026-09-04T12:00:00Z',
    completed_at: null,
    source: 'direct_link',
    label: null,
    current_provider_id: 'general_http',
    current_provider_name: 'HTTP & HTTPS',
    delivering_provider_id: null,
    delivering_provider_name: null,
    provider_provenance_status: 'known',
    extraction_status: null,
    source_failure_count: 0,
    ...overrides,
  };
}

function integratedSettings(base) {
  const settings = clone(base);
  settings.integrations ||= {};
  settings.integrations.alldebrid = {
    ...(settings.integrations.alldebrid || {}),
    enabled: false,
    priority: settings.integrations.alldebrid?.priority || 0,
    name: 'AllDebrid',
    kind: 'provider',
    configured: true,
    presentation: {
      status_name: 'AllDebrid',
      premium: true,
      status_endpoint: '/integration-status/alldebrid',
      static_status: null,
      display_order: 10,
    },
    options: {
      ...(settings.integrations.alldebrid?.options || {}),
      api_key: '',
      api_key_configured: true,
    },
  };
  settings.integrations.general_http = {
    ...(settings.integrations.general_http || {}),
    enabled: true,
    priority: settings.integrations.general_http?.priority || 0,
    name: 'HTTP & HTTPS',
    kind: 'provider',
    configured: true,
    presentation: {
      status_name: 'General Downloads',
      premium: false,
      status_endpoint: null,
      static_status: 'healthy',
      display_order: 100,
    },
    options: {},
  };
  settings.alldebrid_api_key_configured = true;
  settings.alldebrid_rate_limit_per_minute ??= 60;
  settings.poll_interval_seconds ??= 30;
  settings.full_sync_interval_minutes ??= 5;
  settings.upload_fail_retry_count ??= 3;
  settings.upload_fail_retry_delay_minutes ??= 5;
  return settings;
}

async function installSettingsFixture(page, settings) {
  await page.route('**/api/settings', route => {
    if (route.request().method() === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(settings),
      });
    }
    return route.fallback();
  });

  await page.route('**/api/integration-status/alldebrid', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({state: 'disabled', checked: false}),
  }));
}

async function installDownloadsFixture(page, initialDownloads) {
  let downloads = clone(initialDownloads);
  const requests = {bulk: []};

  await page.route('**/api/torrents**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();

    if (url.pathname === '/api/torrents/bulk' && method === 'POST') {
      const body = request.postDataJSON() || {};
      const ids = Array.isArray(body.ids) ? body.ids.map(Number) : [];
      requests.bulk.push({ids: [...ids], action: body.action});
      if (body.action === 'delete') {
        downloads = downloads.filter(item => !ids.includes(Number(item.id)));
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ok: ids.length, failed: 0}),
      });
    }

    if (url.pathname === '/api/torrents' && method === 'GET') {
      const status = String(url.searchParams.get('status') || '').trim();
      const search = String(url.searchParams.get('search') || '').trim().toLowerCase();
      const limit = Math.max(1, Number(url.searchParams.get('limit')) || 25);
      const offset = Math.max(0, Number(url.searchParams.get('offset')) || 0);
      let filtered = downloads.filter(item => !status || item.status === status);
      if (search) filtered = filtered.filter(item => String(item.name || '').toLowerCase().includes(search));
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({items: clone(filtered.slice(offset, offset + limit)), total: filtered.length}),
      });
    }

    return route.fallback();
  });

  return {
    setDownloads(value) { downloads = clone(value); },
    snapshot() { return {downloads: clone(downloads), requests: clone(requests)}; },
  };
}

async function openDownloads(page) {
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
  await expect(page.locator('#page-title')).toHaveText('Downloads');
  await expect(page.locator('#t-tbody .dp-downloads-detail-row').first()).toBeVisible();
}

async function selectedIds(page) {
  return page.evaluate(() => [..._selectedIds].sort((a, b) => a - b));
}

async function topbarOrder(page) {
  return page.locator('#topbar').evaluate(topbar => Array.from(topbar.children)
    .map(node => {
      if (node.id === 'topbar-actions') return 'global-actions';
      if (node.id === 'aria2-speed-badge') return 'engine-widget';
      if (node.classList?.contains('topbar-theme-control')) return 'theme-control';
      return null;
    })
    .filter(Boolean));
}

test('WS2-P2 integrated UI boundary keeps all six remediation contracts coherent', async ({ page }) => {
  await isolateExternalFonts(page);
  await page.route('**/api/aria2/runtime', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({mode: 'external', running: true, active: 0, download_speed: 0}),
  }));

  await page.goto('/');
  await expect.poll(() => page.evaluate(() => !!settingsData?.integrations)).toBeTruthy();
  const base = await page.evaluate(() => JSON.parse(JSON.stringify(settingsData)));
  const settings = integratedSettings(base);
  await installSettingsFixture(page, settings);

  const fixture = await installDownloadsFixture(page, [
    transfer(1, 'Alpha', {
      status: 'completed',
      progress: 100,
      current_provider_id: 'alldebrid',
      current_provider_name: 'AllDebrid',
      delivering_provider_id: 'alldebrid',
      delivering_provider_name: 'AllDebrid',
    }),
    transfer(2, 'Beta', {status: 'paused', progress: 0}),
  ]);

  const dialogs = [];
  page.on('dialog', dialog => {
    dialogs.push(`${dialog.type()}:${dialog.message()}`);
    dialog.dismiss().catch(() => {});
  });

  await page.reload();
  await expect(page.locator('#dash-tbody .dp-downloads-detail-row').first()).toBeVisible();

  expect(await topbarOrder(page)).toEqual(['global-actions', 'engine-widget', 'theme-control']);
  const theme = await page.locator('.topbar-theme-control').boundingBox();
  const engine = await page.locator('#aria2-speed-badge').boundingBox();
  expect(theme).not.toBeNull();
  expect(engine).not.toBeNull();
  expect(engine.x + engine.width).toBeLessThanOrEqual(theme.x + 1);

  const recentChip = page.locator('#dash-tbody tr[data-torrent-id="1"] .dp-provider-chip');
  await expect(recentChip).toHaveText('AllDebrid');
  expect(await recentChip.evaluate(node => Number.parseFloat(getComputedStyle(node).borderTopLeftRadius))).toBe(6);

  await page.evaluate(next => { settingsData = next; }, settings);
  await page.evaluate(() => window.DPProviderStatus.refresh());
  const statusNames = await page.locator('#provider-status-list .dp-provider-status-row > span').allTextContents();
  expect(statusNames).toEqual(['General Downloads']);
  await expect(page.locator('#provider-status-list [data-provider-id="alldebrid"]')).toHaveCount(0);
  await page.screenshot({path: 'test-results/checkpoint-ui-fix-ws2-p2-integrated-dashboard-dark.png', fullPage: true});

  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await expect(page.locator('#view-settings')).toHaveClass(/\bactive\b/);
  const card = page.locator('.dp-settings-provider-card--alldebrid');
  await expect(card).toBeVisible();
  await expect(card.locator('.dp-settings-provider-disclosure')).toHaveAttribute('aria-expanded', 'false');
  await expect(card.locator(':scope > .card-body')).toBeHidden();
  await expect(card.locator('.dp-settings-provider-config-status')).toHaveText('Provider configured');
  await expect(card.locator('.dp-settings-provider-config-status')).toHaveAttribute('data-tone', 'info');

  await page.locator('#theme-toggle').click();
  await expect.poll(() => page.evaluate(() => document.body.classList.contains('light'))).toBeTruthy();
  await page.screenshot({path: 'test-results/checkpoint-ui-fix-ws2-p2-integrated-settings-light.png', fullPage: true});

  await openDownloads(page);
  const alpha = page.locator('.t-chk[data-id="1"]');
  const beta = page.locator('.t-chk[data-id="2"]');
  await alpha.check();
  expect(await selectedIds(page)).toEqual([1]);

  fixture.setDownloads([
    transfer(2, 'Beta', {status: 'downloading', progress: 47}),
    transfer(1, 'Alpha', {
      status: 'completed',
      progress: 100,
      current_provider_id: 'alldebrid',
      current_provider_name: 'AllDebrid',
      delivering_provider_id: 'alldebrid',
      delivering_provider_name: 'AllDebrid',
    }),
  ]);
  await page.evaluate(() => loadTorrents());
  await expect(alpha).toBeChecked();
  expect(await selectedIds(page)).toEqual([1]);

  const remove = page.locator('.dp-downloads-bulk-action--delete');
  await remove.click();
  await expect(page.locator('.dp-settings-confirm-overlay')).toBeVisible();
  await expect(page.locator('[data-confirm-accept]')).toHaveClass(/\bbtn-danger\b/);
  await expect(page.locator('[data-confirm-cancel]')).toHaveText('Cancel');
  await page.locator('[data-confirm-cancel]').click();
  await expect(alpha).toBeChecked();
  expect(fixture.snapshot().requests.bulk).toEqual([]);

  await remove.click();
  await page.evaluate(() => {
    const currentAlpha = document.querySelector('.t-chk[data-id="1"]');
    const currentBeta = document.querySelector('.t-chk[data-id="2"]');
    currentAlpha.checked = false;
    onCheckboxChange(currentAlpha);
    currentBeta.checked = true;
    onCheckboxChange(currentBeta);
  });
  expect(await selectedIds(page)).toEqual([2]);
  await page.screenshot({path: 'test-results/checkpoint-ui-fix-ws2-p2-integrated-remove-light.png', fullPage: true});
  await page.locator('[data-confirm-accept]').click();

  await expect.poll(() => fixture.snapshot().requests.bulk.length).toBe(1);
  expect(fixture.snapshot().requests.bulk[0]).toEqual({ids: [1], action: 'delete'});
  await expect(page.locator('.dp-downloads-detail-row[data-torrent-id="1"]')).toHaveCount(0);
  await expect(beta).toBeChecked();
  expect(await selectedIds(page)).toEqual([2]);
  expect(dialogs).toEqual([]);
});

test('WS2-P2 Downloads pagination changes explicitly clear stable selection scope', async ({ page }) => {
  await isolateExternalFonts(page);
  const downloads = Array.from({length: 30}, (_, index) => transfer(index + 1, `Transfer ${index + 1}`));
  await installDownloadsFixture(page, downloads);

  await page.goto('/');
  await openDownloads(page);

  await page.locator('.t-chk[data-id="1"]').check();
  expect(await selectedIds(page)).toEqual([1]);
  await expect(page.locator('#torrent-page-btns button[aria-label="Next page"]')).toBeVisible();

  await page.locator('#torrent-page-btns button[aria-label="Next page"]').click();
  await expect.poll(() => selectedIds(page)).toEqual([]);
  await expect(page.locator('.dp-downloads-detail-row[data-torrent-id="26"]')).toBeVisible();
  await expect(page.locator('#bulk-bar')).not.toHaveClass(/\bvisible\b/);
});
