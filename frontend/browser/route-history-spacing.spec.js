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
  //
  // DP 1.0.13 Item 1: the five tracks are now declared once on the list and
  // shared by every row as a subgrid, so the status column is one column and
  // every status element is necessarily the same width. This assertion used
  // to read that sameness as a reserved column by checking the widths DIFFER
  // -- which is the per-row sizing the shared provenance edge replaces. What
  // "content-sized" means under a shared track is that the track is exactly
  // the widest status label, reserving nothing beyond it.
  const status = await page.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll('.dp-detail-route-outcome'));
    const textWidth = node => {
      const range = document.createRange();
      range.selectNodeContents(node);
      return range.getBoundingClientRect().width;
    };
    return {track: nodes[0].getBoundingClientRect().width,
            widestText: Math.max(...nodes.map(textWidth))};
  });
  expect(Math.abs(status.track - status.widestText)).toBeLessThanOrEqual(1);
  // The identity column absorbs the remaining width.
  //
  // Stated as the residual rather than as a share of the row: under the shared
  // tracks of Item 1 the fixed columns size to the widest row rather than to
  // each row, so a percentage calibrated to per-row sizing no longer describes
  // anything. What must remain true -- and is what the `minmax(0,1fr)`
  // identity track exists for -- is that the URL is the one track that takes
  // whatever the other four and their gutters leave.
  const grid = await page.evaluate(() => {
    const list = document.querySelector('.dp-detail-route-list');
    const style = getComputedStyle(list);
    return {
      tracks: style.gridTemplateColumns.split(' ').map(parseFloat),
      gap: parseFloat(style.columnGap),
      width: list.getBoundingClientRect().width,
    };
  });
  expect(grid.tracks).toHaveLength(5);
  const identity = grid.tracks[2];
  // The URL track is the largest, and the five tracks plus their gutters fill
  // the list exactly -- so the URL is what the other four leave behind.
  expect(Math.max(...grid.tracks)).toBe(identity);
  const consumed = grid.tracks.reduce((total, track) => total + track, 0) + 4 * grid.gap;
  expect(Math.abs(consumed - grid.width)).toBeLessThanOrEqual(1);
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

/* DP 1.0.13 Item 1 -- one shared provenance column.
 *
 * The constant gap above is necessary but not sufficient: with per-row `auto`
 * tracks each row sizes provenance and status independently, so the gap holds
 * while the provenance right edges wander with the length of the status label
 * beside them. The column itself must be shared. */

const provenanceEdges = page => page.evaluate(() =>
  Array.from(document.querySelectorAll('.dp-detail-route-row')).map(row => ({
    provenance: row.querySelector('.dp-detail-route-relation').textContent.trim(),
    status: row.querySelector('.dp-detail-route-outcome').textContent.trim(),
    right: row.querySelector('.dp-detail-route-relation').getBoundingClientRect().right,
    statusLeft: row.querySelector('.dp-detail-route-outcome').getBoundingClientRect().left,
  })));

test('every provenance string ends on one shared right edge', async ({page}) => {
  await open(page, detail(982, attempts));
  const rows = await provenanceEdges(page);
  // The sample really does contain provenance strings of three different widths.
  expect(new Set(rows.map(r => r.provenance)).size).toBeGreaterThanOrEqual(3);
  const spread = Math.max(...rows.map(r => r.right)) - Math.min(...rows.map(r => r.right));
  expect(spread).toBeLessThanOrEqual(0.5);
});

test('status begins from its own independent shared column', async ({page}) => {
  await open(page, detail(983, attempts));
  const rows = await provenanceEdges(page);
  expect(new Set(rows.map(r => r.status)).size).toBeGreaterThanOrEqual(3);
  const spread = Math.max(...rows.map(r => r.statusLeft)) - Math.min(...rows.map(r => r.statusLeft));
  expect(spread).toBeLessThanOrEqual(0.5);
});

test('a long provenance string cannot indent the status column', async ({page}) => {
  const widened = attempts.map((attempt, index) => index === 1
    ? {...attempt, contributing_transfer_id: 999999999} : attempt);
  await open(page, detail(984, widened));
  const rows = await provenanceEdges(page);
  const spread = Math.max(...rows.map(r => r.statusLeft)) - Math.min(...rows.map(r => r.statusLeft));
  expect(spread).toBeLessThanOrEqual(0.5);
});

test('narrow viewports keep the stacked presentation', async ({page}) => {
  await page.setViewportSize({width: 520, height: 900});
  await open(page, detail(985, attempts));
  const stacked = await page.evaluate(() => {
    const row = document.querySelector('.dp-detail-route-row');
    const relation = row.querySelector('.dp-detail-route-relation').getBoundingClientRect();
    const identity = row.querySelector('.dp-detail-route-identity').getBoundingClientRect();
    return {below: relation.top >= identity.bottom - 1, aligned: Math.abs(relation.left - identity.left) < 1};
  });
  expect(stacked.below).toBe(true);
  expect(stacked.aligned).toBe(true);
});
