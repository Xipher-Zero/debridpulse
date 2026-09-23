const { test, expect } = require('@playwright/test');

/* DP 1.0.13 work item I -- one explicit constant gap between the Route History
 * provenance and its route status, with the URL keeping the flexible space. */

const detail = (id, attempts) => ({
  id, name: 'Route spacing', status: 'completed', progress: 100, size_bytes: 1024,
  source: 'direct_link', request_kinds: ['https'], hash: '',
  created_at: '2026-01-01T00:00:00Z', completed_at: '2026-01-01T01:00:00Z',
  original_resource: 'https://downloads.example.com/a/deliberately/long/path/ubuntu-24.04.1-desktop-amd64.iso',
  files: [], source_outcomes: [], events: [], executors: [],
  current_provider_name: 'HTTP & HTTPS', delivering_provider_name: 'HTTP & HTTPS',
  route_attempts: attempts,
});

const attempts = [
  {ordinal: 1, presentation_ordinal: 1, provider_id: 'general_http', provider_name: 'HTTP & HTTPS',
   outcome: 'completed', relation: 'original',
   route_identity: 'https://mirror-one.example.com/releases/ubuntu-24.04.1-desktop-amd64.iso',
   route_location: 'https://mirror-one.example.com/releases/ubuntu-24.04.1-desktop-amd64.iso'},
  {ordinal: 2, presentation_ordinal: 2, provider_id: 'general_http', provider_name: 'HTTP & HTTPS',
   outcome: 'resolved', relation: 'consolidated', contributing_transfer_id: 411,
   route_identity: 'https://mirror-two.example.com/x.iso', route_location: 'https://mirror-two.example.com/x.iso'},
  {ordinal: 3, presentation_ordinal: 3, provider_id: 'alldebrid', provider_name: 'AllDebrid',
   outcome: 'failed', relation: 'unverified', verification_state: 'unverified',
   contributing_transfer_id: 412, route_identity: 'https://m3.example/x.iso', route_location: 'https://m3.example/x.iso'},
  {ordinal: 4, presentation_ordinal: 4, provider_id: 'alldebrid', provider_name: 'AllDebrid',
   outcome: 'cancelled', relation: 'original',
   route_identity: 'https://m4.example/x.iso', route_location: 'https://m4.example/x.iso'},
];

async function open(page, payload) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route(url => url.pathname === `/api/torrents/${payload.id}`,
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(payload)}));
  await page.route(url => url.pathname === '/api/torrents',
    route => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({items: [payload], total: 1})}));
  await page.goto('/');
  await page.evaluate(id => showDetail(id), payload.id);
  await expect(page.locator('.dp-detail-route-row')).toHaveCount(payload.route_attempts.length);
}

const measure = page => page.evaluate(() => Array.from(document.querySelectorAll('.dp-detail-route-row')).map(row => {
  const relation = row.querySelector('.dp-detail-route-relation');
  const outcome = row.querySelector('.dp-detail-route-outcome');
  const identity = row.querySelector('.dp-detail-route-identity');
  return {
    status: outcome.textContent.trim(),
    gap: outcome.getBoundingClientRect().left - relation.getBoundingClientRect().right,
    identityWidth: identity.getBoundingClientRect().width,
    relationAlign: getComputedStyle(relation).textAlign,
    outcomeWidth: outcome.getBoundingClientRect().width,
  };
}));

test('the provenance/status gap is one constant, whatever the status label', async ({page}) => {
  await open(page, detail(980, attempts));
  const rows = await measure(page);
  expect(new Set(rows.map(r => r.status)).size).toBe(4);
  const gaps = rows.map(r => Math.round(r.gap * 100) / 100);
  expect(Math.max(...gaps) - Math.min(...gaps)).toBeLessThanOrEqual(0.5);
  // Explicit, not the incidental grid gutter.
  expect(gaps[0]).toBeGreaterThanOrEqual(18);
});

test('provenance is right-aligned, status keeps its natural width, the URL keeps the space', async ({page}) => {
  await open(page, detail(981, attempts));
  const rows = await measure(page);
  for (const row of rows) expect(row.relationAlign).toBe('right');
  // Status is content-sized, never a reserved column.
  expect(new Set(rows.map(r => Math.round(r.outcomeWidth))).size).toBeGreaterThan(1);
  // The identity column absorbs the remaining width.
  const widest = Math.max(...rows.map(r => r.identityWidth));
  const header = await page.locator('.dp-detail-route-row').first().boundingBox();
  expect(widest).toBeGreaterThan(header.width * 0.45);
});

test('the responsive stack stays readable', async ({page}) => {
  await page.setViewportSize({width: 480, height: 900});
  await open(page, detail(982, attempts));
  const overflow = await page.evaluate(() => {
    const list = document.querySelector('.dp-detail-route-list');
    return list.scrollWidth - list.clientWidth;
  });
  expect(overflow).toBeLessThanOrEqual(1);
});
