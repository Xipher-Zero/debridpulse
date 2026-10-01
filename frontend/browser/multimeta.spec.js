const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Multimeta: one more Network Sources peer, an upload kind, and the
 * origin it keeps in every compact transfer presentation.
 *
 * Its Services box, chip (Lucide Network turned upside down, Hot Fuchsia
 * #E879F9) and Enable are proven with every other member by
 * settings-protocol-icons.spec.js, direct-sources-ftp-sftp.spec.js and
 * general-sources-master.spec.js (the one owner of
 * `integrations.multimeta.enabled`). This file writes no canonical state. */

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

test('Multimeta has no Transfer Method Settings card', async ({page}) => {
  await isolateExternalFonts(page);
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="settings"]').click();
  await page.locator('#view-settings [data-tab="downloads"]').click();
  const panel = page.locator('.dp-settings-panel[data-panel="downloads"]');
  await expect(panel).toBeVisible();
  await expect(panel.locator('[data-executor-tuning]')).not.toHaveCount(0);
  await expect(panel.locator('[data-executor-tuning="multimeta"], [data-protocol="multimeta"]')).toHaveCount(0);
  expect(await panel.innerText()).not.toMatch(/Multimeta|Metalink/);
});

test('an uploaded .meta4 file goes to the Multimeta upload, as an interactive submission', async ({page}) => {
  await isolateExternalFonts(page);
  const uploads = [];
  await page.route(url => url.pathname === '/api/multimeta/add-file', async route => {
    uploads.push(route.request().postData() || '');
    await route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({id: 41, name: 'release', status: 'pending'})});
  });
  await page.goto('/');
  await expect(page.locator('#torrent-file-input')).toHaveAttribute('accept', /\.meta4/);
  await page.locator('#torrent-file-input').setInputFiles({
    name: 'release.meta4', mimeType: 'application/metalink4+xml',
    buffer: Buffer.from('<metalink xmlns="urn:ietf:params:xml:ns:metalink"/>')});
  await expect.poll(() => uploads.length).toBe(1);
  expect(uploads[0]).toContain('filename="release.meta4"');
  expect(uploads[0]).toContain('name="selection_mode"');
  expect(uploads[0]).toContain('interactive');
  await expect(page.locator('.toast').last()).toContainText('Metalink file added');
});

function item(overrides = {}) {
  return {
    id: 991, name: 'release.iso', status: 'downloading', presentation_status: 'downloading', progress: 40,
    size_bytes: 2048, source: 'manual_file', request_kinds: ['meta4'], label: '', hash: '',
    created_at: '2026-10-01T00:00:00Z', provider_provenance_status: 'recorded',
    current_source_identity: {kind: 'host', host: 'mirror.example.org'},
    origin_provider_id: 'multimeta', origin_provider_name: 'Multimeta',
    current_provider_id: 'general_http', current_provider_name: 'HTTP(S)',
    delivering_provider_id: 'general_http', delivering_provider_name: 'HTTP(S)',
    ...overrides,
  };
}

test('a Multimeta transfer keeps its origin while the real route and engine stay visible', async ({page}) => {
  await isolateExternalFonts(page);
  const listed = item();
  const detail = item({
    original_resource: 'release.meta4', files: [], source_outcomes: [], events: [], executors: ['aria2'],
    execution_attempts: [],
    route_attempts: [
      {ordinal: 1, presentation_ordinal: 1, provider_id: 'multimeta', provider_name: 'Multimeta',
       outcome: 'resolved', relation: 'original', route_identity: '', route_location: 'release.meta4'},
      {ordinal: 2, presentation_ordinal: 2, provider_id: 'general_http', provider_name: 'HTTP(S)',
       outcome: 'active', relation: 'original', route_identity: 'https://mirror.example.org',
       route_location: 'https://mirror.example.org/pub/release.iso'},
    ],
  });
  await page.route(url => url.pathname === '/api/torrents',
    route => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({items: [listed], total: 1})}));
  await page.route(url => url.pathname === `/api/torrents/${listed.id}`,
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(detail)}));

  await page.goto('/');
  await expect(page.locator(`#dash-tbody tr[data-torrent-id="${listed.id}"] .dp-provider-chip`)).toHaveText('Multimeta');
  await page.evaluate(id => showDetail(id), listed.id);
  await expect(page.getByText('Submitted As').locator('..')).toContainText('Metalink file');
  // Origin is Multimeta; the route history and the current provider name what
  // really serves the file.
  await expect(page.locator('.dp-detail-route-row .dp-detail-route-provider')).toHaveText(['Multimeta', 'HTTP(S)']);
  await expect(page.getByText('Origin Provider ID').locator('..')).toContainText('multimeta');
  await expect(page.getByText('Current Provider ID').locator('..')).toContainText('general_http');
});
