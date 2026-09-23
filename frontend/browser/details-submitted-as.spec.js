const { test, expect } = require('@playwright/test');

/* DP 1.0.13 work item H -- "Submitted As" reports the canonical request kind,
 * never the submission channel's legacy assumption and never the provider. */

const base = (id, extra) => ({
  id, name: 'Posting', status: 'completed', progress: 100, size_bytes: 1024,
  created_at: '2026-01-01T00:00:00Z', completed_at: '2026-01-01T01:00:00Z', hash: '',
  files: [], source_outcomes: [], events: [], executors: [], route_attempts: [],
  current_provider_name: 'Usenet', delivering_provider_name: 'Usenet', ...extra,
});

async function showDetail(page, detail) {
  await page.route(url => url.pathname === `/api/torrents/${detail.id}`,
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(detail)}));
  await page.route(url => url.pathname === '/api/torrents',
    route => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({items: [detail], total: 1})}));
  await page.goto('/');
  await page.evaluate(id => showDetail(id), detail.id);
}

const submittedAs = page =>
  page.locator('.detail-grid > div', {has: page.locator('.dk', {hasText: 'Submitted As'})}).locator('.dv');

test('an uploaded NZB reports NZB file', async ({page}) => {
  await showDetail(page, base(940, {source: 'manual_file', request_kinds: ['nzb'],
    original_resource: 'posting.nzb'}));
  await expect(submittedAs(page)).toHaveText('NZB file');
});

test('an uploaded torrent still reports Torrent file', async ({page}) => {
  await showDetail(page, base(941, {source: 'manual_file', request_kinds: ['torrent'],
    current_provider_name: 'AllDebrid', delivering_provider_name: 'AllDebrid'}));
  await expect(submittedAs(page)).toHaveText('Torrent file');
});

test('the label never comes from provider identity', async ({page}) => {
  // Provider says Usenet, but the canonical request kind is a torrent upload.
  await showDetail(page, base(942, {source: 'manual_file', request_kinds: ['torrent'],
    current_provider_name: 'Usenet', delivering_provider_name: 'Usenet'}));
  await expect(submittedAs(page)).toHaveText('Torrent file');
  // ...and the reverse.
  await showDetail(page, base(943, {source: 'manual_file', request_kinds: ['nzb'],
    current_provider_name: 'AllDebrid', delivering_provider_name: 'AllDebrid'}));
  await expect(submittedAs(page)).toHaveText('NZB file');
});

test('the other submission channels keep their labels', async ({page}) => {
  for (const [source, label] of [['manual', 'Magnet link'], ['direct_link', 'Direct link'],
                                 ['api', 'API'], ['inventory', 'Provider inventory']]) {
    await showDetail(page, base(950 + label.length, {source, request_kinds: []}));
    await expect(submittedAs(page)).toHaveText(label);
  }
});

test('a mixed or unknown upload gets a truthful neutral label', async ({page}) => {
  await showDetail(page, base(960, {source: 'manual_file', request_kinds: ['nzb', 'torrent']}));
  await expect(submittedAs(page)).toHaveText('Uploaded file');
  await showDetail(page, base(961, {source: 'manual_file'}));
  await expect(submittedAs(page)).toHaveText('Uploaded file');
});

test('the Downloads list uses the same formatter and the same canonical fact', async ({page}) => {
  const rows = [base(970, {source: 'manual_file', request_kinds: ['nzb']}),
                base(971, {source: 'manual_file', request_kinds: ['torrent']})];
  await page.route(url => url.pathname === '/api/torrents',
    route => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({items: rows, total: rows.length})}));
  await page.goto('/');
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect(page.locator('#t-tbody tr[data-torrent-id="970"] .dp-transfer-source-label'))
    .toHaveText('NZB file');
  await expect(page.locator('#t-tbody tr[data-torrent-id="971"] .dp-transfer-source-label'))
    .toHaveText('Torrent file');
});

test('the real backend projects the canonical request kind for an uploaded NZB', async ({page}) => {
  const nzb = ['<?xml version="1.0" encoding="iso-8859-1" ?>',
    '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">',
    '<file poster="p@example.com" date="1700000000" subject="dp-test [1/1] - &quot;dp.bin&quot; yEnc (1/1)">',
    '<groups><group>alt.binaries.test</group></groups>',
    '<segments><segment bytes="1024" number="1">seg1@example</segment></segments>',
    '</file></nzb>'].join('');
  const response = await page.request.post('/api/usenet/add-file', {
    multipart: {file: {name: 'dp-submitted-as.nzb', mimeType: 'application/x-nzb', buffer: Buffer.from(nzb)}},
  });
  expect(response.ok()).toBeTruthy();
  const created = await response.json();
  try {
    const detail = await page.request.get(`/api/torrents/${created.id}`).then(r => r.json());
    expect(detail.source).toBe('manual_file');
    expect(detail.request_kinds).toEqual(['nzb']);
  } finally {
    // This is the one case here that admits REAL durable work, so it must leave
    // none behind: a live Usenet transfer fences the news-server set against
    // change, which would poison any later spec that configures one.
    await page.request.delete(`/api/torrents/${created.id}`);
    await expect.poll(async () => {
      const listed = await page.request.get('/api/torrents?limit=100').then(r => r.json());
      return (listed.items || []).some(item => item.id === created.id);
    }, {timeout: 20000}).toBe(false);
  }
});
