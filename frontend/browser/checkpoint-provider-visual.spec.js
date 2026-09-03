const { test, expect } = require('@playwright/test');

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({ status: 200, contentType: 'text/css', body: '' }),
  );
}

function providerFixture(overrides = {}) {
  return {
    id: 951,
    name: 'Checkpoint provider visual fixture',
    status: 'completed',
    progress: 100,
    size_bytes: 1024,
    source: 'direct_link',
    label: '',
    hash: '',
    created_at: '2026-09-03T00:00:00Z',
    provider_provenance_status: 'recorded',
    current_provider_id: 'general_http',
    current_provider_name: 'HTTP & HTTPS',
    delivering_provider_id: 'general_http',
    delivering_provider_name: 'HTTP & HTTPS',
    ...overrides,
  };
}

test('checkpoint visually captures Recent Activity and Downloads provider indicators', async ({ page }) => {
  await isolateExternalFonts(page);
  const items = [
    providerFixture({
      id: 951,
      name: 'Active through AllDebrid',
      status: 'downloading',
      progress: 42,
      current_provider_id: 'alldebrid',
      current_provider_name: 'AllDebrid',
      delivering_provider_id: null,
      delivering_provider_name: null,
    }),
    providerFixture({
      id: 952,
      name: 'Completed through HTTP & HTTPS',
    }),
    providerFixture({
      id: 953,
      name: 'Legacy provider unknown',
      provider_provenance_status: 'unknown_legacy',
      current_provider_id: null,
      current_provider_name: null,
      delivering_provider_id: null,
      delivering_provider_name: null,
    }),
  ];

  await page.route(
    url => url.pathname === '/api/torrents',
    route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items, total: items.length }),
    }),
  );

  await page.goto('/');
  await expect(page.locator('#dash-tbody tr[data-torrent-id="951"] .dp-provider-chip')).toHaveText('AllDebrid');
  await expect(page.locator('#dash-tbody tr[data-torrent-id="952"] .dp-provider-chip')).toHaveText('HTTP & HTTPS');
  await expect(page.locator('#dash-tbody tr[data-torrent-id="953"] .dp-provider-chip')).toHaveText('Unknown');
  await page.screenshot({ path: 'test-results/checkpoint-recent-activity-provider-dark-desktop.png', fullPage: true });

  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
  await expect(page.locator('#t-tbody tr[data-torrent-id="951"] .dp-provider-chip')).toHaveText('AllDebrid');
  const httpProviderChip = page.locator('#t-tbody tr[data-torrent-id="952"] .dp-provider-chip');
  await expect(httpProviderChip).toHaveText('HTTP & HTTPS');
  await expect(page.locator('#t-tbody tr[data-torrent-id="953"] .dp-provider-chip')).toHaveText('Unknown');
  const providerHeading = page.locator('#view-torrents thead th').filter({ hasText: 'Provider / Source' });
  await expect(providerHeading).toHaveCount(1);
  expect(await httpProviderChip.evaluate(chip => {
    const chipRect = chip.getBoundingClientRect();
    const cellRect = chip.closest('td').getBoundingClientRect();
    return chip.scrollWidth <= chip.clientWidth && chipRect.right <= cellRect.right + 0.5;
  })).toBe(true);
  expect(await providerHeading.evaluate(heading => heading.scrollWidth <= heading.clientWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/checkpoint-downloads-provider-dark-desktop.png', fullPage: true });
});
