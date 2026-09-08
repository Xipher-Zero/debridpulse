const { test, expect } = require('@playwright/test');

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({
    status: 200,
    contentType: 'text/css',
    body: '',
  }));
}

function completedTransfer(id) {
  return {
    id,
    name: `Historical Transfer ${String(id).padStart(4, '0')}`,
    hash: `history-${String(id).padStart(4, '0')}-0123456789abcdef`,
    status: 'completed',
    progress: 100,
    size_bytes: 1024 * 1024 * id,
    created_at: '2026-08-01T12:00:00Z',
    completed_at: '2026-08-01T12:05:00Z',
    source: 'direct_link',
    label: null,
    current_provider_id: 'general_http',
    current_provider_name: 'HTTP & HTTPS',
    delivering_provider_id: 'general_http',
    delivering_provider_name: 'HTTP & HTTPS',
    provider_provenance_status: 'known',
    extraction_status: null,
    source_failure_count: 0,
  };
}

async function installLargeHistoryFixture(page, total = 1000) {
  const downloads = Array.from({length: total}, (_, index) => completedTransfer(index + 1));
  const listRequests = [];

  await page.route('**/api/torrents**', async route => {
    const request = route.request();
    const url = new URL(request.url());

    // Only replace the paginated Downloads collection read. Dashboard recent
    // activity and lifecycle probes intentionally continue to the real backend.
    if (
      url.pathname !== '/api/torrents' ||
      request.method() !== 'GET' ||
      !url.searchParams.has('offset')
    ) {
      return route.fallback();
    }

    const limit = Math.max(1, Number(url.searchParams.get('limit')) || 25);
    const offset = Math.max(0, Number(url.searchParams.get('offset')) || 0);
    const status = String(url.searchParams.get('status') || '').trim();
    const search = String(url.searchParams.get('search') || '').trim().toLowerCase();
    listRequests.push({limit, offset, status, search});

    let filtered = downloads;
    if (status) filtered = filtered.filter(item => item.status === status);
    if (search) {
      filtered = filtered.filter(item => String(item.name || '').toLowerCase().includes(search));
    }

    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        items: filtered.slice(offset, offset + limit),
        total: filtered.length,
      }),
    });
  });

  return {listRequests, total};
}

async function openDownloads(page) {
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
  await expect(page.locator('#page-title')).toHaveText('Downloads');
  await expect(page.locator('#t-tbody .dp-downloads-detail-row').first()).toBeVisible();
}

async function waitForDownloadsListToSettle(rows, fixture) {
  let previousSignature = '';
  let stableObservations = 0;

  await expect.poll(async () => {
    const request = fixture.listRequests.at(-1);
    if (!request) return false;

    const count = await rows.count();
    const expectedCount = Math.min(
      request.limit,
      Math.max(0, fixture.total - request.offset)
    );
    const signature = `${fixture.listRequests.length}:${request.limit}:${request.offset}:${count}`;

    if (signature === previousSignature) stableObservations += 1;
    else stableObservations = 0;
    previousSignature = signature;

    return stableObservations >= 3 && count === expectedCount;
  }, {
    timeout: 4000,
    intervals: [100, 100, 100, 100, 100, 200, 200],
  }).toBe(true);

  return fixture.listRequests.at(-1);
}

test('Session 3 Browser Runtime keeps a 1,000-item historical collection bounded and paginated', async ({ page }) => {
  await isolateExternalFonts(page);
  const fixture = await installLargeHistoryFixture(page, 1000);

  await page.goto('/');
  await openDownloads(page);

  const rows = page.locator('#t-tbody .dp-downloads-detail-row');
  const firstRequest = await waitForDownloadsListToSettle(rows, fixture);
  const pageSize = firstRequest.limit;

  expect(firstRequest.offset).toBe(0);
  expect(pageSize).toBeGreaterThan(0);
  expect(pageSize).toBeLessThanOrEqual(100);
  await expect(rows).toHaveCount(pageSize);
  await expect(page.locator('.dp-downloads-detail-row[data-torrent-id="1"]')).toBeVisible();
  await expect(page.locator(`.dp-downloads-detail-row[data-torrent-id="${pageSize}"]`)).toBeVisible();
  await expect(page.locator('#torrent-page-info')).toContainText('1000');

  const beforeNext = fixture.listRequests.length;
  await page.locator('#torrent-page-btns button[aria-label="Next page"]').click();
  await expect.poll(() => fixture.listRequests.length).toBeGreaterThan(beforeNext);
  const nextRequest = await waitForDownloadsListToSettle(rows, fixture);

  expect(nextRequest.limit).toBe(pageSize);
  expect(nextRequest.offset).toBe(pageSize);
  await expect(page.locator(`.dp-downloads-detail-row[data-torrent-id="${pageSize + 1}"]`)).toBeVisible();
  await expect(page.locator(`.dp-downloads-detail-row[data-torrent-id="${pageSize * 2}"]`)).toBeVisible();
  await expect(rows).toHaveCount(pageSize);

  const lastPage = Math.ceil(fixture.total / pageSize);
  const lastOffset = (lastPage - 1) * pageSize;
  const lastCount = fixture.total - lastOffset;
  const beforeLast = fixture.listRequests.length;

  await page.evaluate(pageNumber => goToTorrentPage(pageNumber), lastPage);
  await expect.poll(() => fixture.listRequests.length).toBeGreaterThan(beforeLast);
  const lastRequest = await waitForDownloadsListToSettle(rows, fixture);

  expect(lastRequest.limit).toBe(pageSize);
  expect(lastRequest.offset).toBe(lastOffset);
  await expect(page.locator(`.dp-downloads-detail-row[data-torrent-id="${lastOffset + 1}"]`)).toBeVisible();
  await expect(page.locator('.dp-downloads-detail-row[data-torrent-id="1000"]')).toBeVisible();
  await expect(rows).toHaveCount(lastCount);
  await expect(page.locator('#torrent-page-info')).toContainText('1000');

  expect(fixture.listRequests.length).toBeGreaterThanOrEqual(3);
  expect(fixture.listRequests.every(request => request.limit <= 100)).toBe(true);
});
