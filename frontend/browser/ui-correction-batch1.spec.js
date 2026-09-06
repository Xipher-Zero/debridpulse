const { test, expect } = require('@playwright/test');

async function waitForBatch(page) {
  await page.goto('/');
  await page.waitForFunction(() => Boolean(window.DPUICorrectionBatch1));
}

test('Batch 1 removes Quick Add Import and keeps neutral recovery as sole header action', async ({ page }) => {
  await waitForBatch(page);
  const quick = page.locator('.dp-dashboard-quick-add');
  await expect(quick.locator('#btn-import-existing')).toHaveCount(0);
  await expect(quick.locator('#btn-recover-all')).toHaveCount(1);
  await expect(quick.locator('.dp-card-header-actions button')).toHaveCount(1);
  await expect(quick.locator('#btn-recover-all')).toHaveAttribute('title', 'Check transfers for recoverable work');
});

test('authoritative pause state owns both Quick Add and Downloads pause surfaces', async ({ page }) => {
  await waitForBatch(page);
  await page.evaluate(() => {
    settingsData = settingsData || {};
    settingsData.paused = true;
    renderTopbarActions();
  });

  const quickPause = page.locator('.dp-global-pause-center');
  await expect(quickPause).toHaveClass(/is-visible/);
  await expect(quickPause).toContainText('PROCESSING PAUSED');
  await expect(quickPause).toContainText('New downloads can still be added. They will remain queued until processing is resumed.');

  await page.evaluate(() => nav(document.querySelector('[data-view="torrents"]')));
  const shim = page.locator('.dp-downloads-pause-shim');
  await expect(shim).toHaveClass(/is-visible/);
  await expect(shim).toHaveText('Processing paused. Queued and newly added downloads will not start until processing is resumed.');

  await page.evaluate(() => {
    settingsData.paused = false;
    renderTopbarActions();
  });
  await expect(shim).not.toHaveClass(/is-visible/);
});

test('pager keeps three physical slots and current page stays centered at both boundaries', async ({ page }) => {
  await waitForBatch(page);
  await page.evaluate(async () => {
    nav(document.querySelector('[data-view="torrents"]'));
    await loadTorrents();
  });

  const buttons = page.locator('#torrent-page-btns');
  await page.evaluate(() => renderTorrentPagination(30, 10, 0));
  await expect(buttons.locator('.dp-pager-slot')).toHaveCount(2);
  await expect(buttons.locator('.dp-pager-placeholder')).toHaveCount(1);
  await expect(buttons.locator('.dp-pager-current')).toHaveText('1');

  const firstCenter = await buttons.locator('.dp-pager-current').boundingBox();
  const firstBox = await buttons.boundingBox();

  await page.evaluate(() => renderTorrentPagination(30, 10, 20));
  await expect(buttons.locator('.dp-pager-placeholder')).toHaveCount(1);
  await expect(buttons.locator('.dp-pager-current')).toHaveText('3');
  const lastCenter = await buttons.locator('.dp-pager-current').boundingBox();
  const lastBox = await buttons.boundingBox();

  expect(Math.abs((firstCenter.x + firstCenter.width / 2) - (firstBox.x + firstBox.width / 2))).toBeLessThan(1);
  expect(Math.abs((lastCenter.x + lastCenter.width / 2) - (lastBox.x + lastBox.width / 2))).toBeLessThan(1);
});

test('date menu is an options control and host icons use domain-boundary matching', async ({ page }) => {
  await waitForBatch(page);
  await page.evaluate(() => nav(document.querySelector('[data-view="torrents"]')));
  const trigger = page.locator('.dp-date-menu-trigger');
  await expect(trigger).toHaveCount(1);
  await expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
  await trigger.click();
  await expect(page.getByRole('menuitemradio', { name: 'Friendly' })).toBeVisible();
  await expect(page.getByRole('menuitemradio', { name: 'International' })).toBeVisible();
  await expect(page.getByRole('menuitemradio', { name: 'ISO' })).toBeVisible();
  await expect(page.getByRole('menuitemradio', { name: '24-hour' })).toBeVisible();
  await expect(page.getByRole('menuitemradio', { name: '12-hour' })).toBeVisible();

  const assets = await page.evaluate(() => ({
    exact: DPUICorrectionBatch1.hostAsset('rapidgator.net'),
    subdomain: DPUICorrectionBatch1.hostAsset('cdn.rapidgator.net'),
    boundary: DPUICorrectionBatch1.hostAsset('notrapidgator.net'),
    unknown: DPUICorrectionBatch1.hostAsset('example.invalid'),
  }));
  expect(assets.exact).toBe('/icons/hosts/rapidgator.png');
  expect(assets.subdomain).toBe('/icons/hosts/rapidgator.png');
  expect(assets.boundary).toBe('');
  expect(assets.unknown).toBe('');
});

test('Batch 1 toast compatibility delegates duration and rendering to the canonical presenter', async ({ page }) => {
  await waitForBatch(page);
  const contract = await page.evaluate(() => ({
    three: DPToastDuration('one two three', 'info'),
    twelve: DPUICorrectionBatch1.toastDuration('one two three four five six seven eight nine ten eleven twelve', 'error'),
    canonicalThree: DPIcons.toastDuration('one two three'),
    publicBridge: Boolean(window.DPToastContract),
  }));
  expect(contract).toEqual({three: 3000, twelve: 3000, canonicalThree: 3000, publicBridge: true});

  await page.evaluate(() => toast('Compatibility rendering remains canonical now.', 'info'));
  const node = page.locator('#toasts .toast').last();
  await expect(node).toBeVisible();
  await expect(node.locator('.dp-toast-close, .dp-toast-dismiss, button')).toHaveCount(0);
  await expect(node).toHaveAttribute('data-dp-toast-duration-ms', '3000');
});

for (const height of [760, 980]) {
  test(`Downloads measured capacity can request fewer than fifteen rows at ${height}px`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height });
    await waitForBatch(page);
    await page.evaluate(() => nav(document.querySelector('[data-view="torrents"]')));

    await page.evaluate(() => {
      const body = document.getElementById('t-tbody');
      body.innerHTML = Array.from({ length: 8 }, (_, index) =>
        `<tr data-torrent-id="${index + 1}" style="height:58px"><td colspan="8">row ${index + 1}</td></tr>`
      ).join('');
      torrentPage = 1;
      torrentPageSize = 25;
      DPUICorrectionBatch1.recalculateDownloadsCapacity();
    });
    await page.waitForTimeout(350);
    const size = await page.evaluate(() => torrentPageSize);
    expect(size).toBeGreaterThanOrEqual(1);
    expect(size).toBeLessThan(15);
  });
}

test('Provider Status heading precedes premium row and Direct Sources aggregate reflects enablement', async ({ page }) => {
  await waitForBatch(page);

  const order = await page.evaluate(() => {
    const footer = document.querySelector('.sidebar-footer');
    const heading = footer?.querySelector(':scope > .dp-provider-status-heading');
    const premium = document.getElementById('premium-row');
    return {
      heading: heading ? Array.from(footer.children).indexOf(heading) : -1,
      premium: premium ? Array.from(footer.children).indexOf(premium) : -1,
    };
  });
  expect(order.heading).toBeGreaterThanOrEqual(0);
  expect(order.premium).toBeGreaterThan(order.heading);

  const states = await page.evaluate(() => {
    const base = {
      integrations: {
        general_http: {
          kind: 'provider', enabled: false, configured: true,
          presentation: {
            status_name: 'HTTP & HTTPS', static_status: 'healthy', display_order: 100,
            status_group: 'direct_sources', status_group_label: 'Direct Sources',
          },
        },
      },
    };
    const disabled = DPProviderStatus.candidates(base);
    const disabledState = DPProviderStatus.aggregateState(disabled);
    base.integrations.future_direct = {
      ...base.integrations.general_http,
      enabled: true,
      presentation: {...base.integrations.general_http.presentation, status_name: 'Future Direct'},
    };
    const mixed = DPProviderStatus.candidates(base).map(entry => ({...entry, state: entry.enabled ? 'healthy' : 'disabled'}));
    base.integrations.general_http.enabled = true;
    const enabled = DPProviderStatus.candidates(base).map(entry => ({...entry, state: 'healthy'}));
    return {disabledState, mixedState: DPProviderStatus.aggregateState(mixed), enabledState: DPProviderStatus.aggregateState(enabled)};
  });
  expect(states).toEqual({disabledState: 'disabled', mixedState: 'mixed', enabledState: 'healthy'});
});

test('supplied Rapidgator host artwork is served with bounded source-icon geometry in both themes', async ({ page }) => {
  await waitForBatch(page);
  const response = await page.request.get('/icons/hosts/rapidgator.png');
  expect(response.ok()).toBeTruthy();
  expect((await response.body()).length).toBeGreaterThan(1000);

  await page.evaluate(() => {
    const host = document.createElement('div');
    host.id = 'batch1-source-geometry-probe';
    host.innerHTML = `<span class="dp-source-icon-slot">${DPUICorrectionBatch1.sourceIconMarkup({kind: 'host', host: 'rapidgator.net'})}</span>`;
    document.body.appendChild(host);
  });
  for (const theme of ['dark', 'light']) {
    await page.evaluate(value => document.documentElement.setAttribute('data-theme', value), theme);
    const slot = page.locator('#batch1-source-geometry-probe .dp-source-icon-slot');
    const logo = slot.locator('img.dp-source-host-logo');
    await expect(logo).toHaveCount(1);
    const box = await slot.boundingBox();
    expect(Math.round(box.width)).toBe(20);
    expect(Math.round(box.height)).toBe(20);
  }
});
