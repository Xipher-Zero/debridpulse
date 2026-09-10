const { test, expect } = require('@playwright/test');

// Torrent/Magnet File-Selection Lifecycle Correction §6 / §24 — the built-in
// browser is an interactive client and MUST explicitly opt every torrent/magnet
// submission into the interactive file-selection lifecycle
// (selection_mode=interactive). Direct-link submission is unchanged and carries
// no selection_mode. The server never infers interactive intent from an SSE
// connection, a session, a user agent, or the source string.

async function stubShell(page, capture) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));

  await page.route(url => url.pathname === '/api/torrents/add-magnet', route => {
    capture.addMagnet.push(route.request().postDataJSON());
    return route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({id: 11, name: 'magnet', status: 'pending'})});
  });
  await page.route(url => url.pathname === '/api/torrents/add-file', route => {
    capture.addFile.push(route.request().postData() || '');
    return route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({id: 12, name: 'file', status: 'pending'})});
  });
  await page.route(url => url.pathname === '/api/links/add', route => {
    capture.linksAdd.push(route.request().postDataJSON());
    return route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({id: 13, name: 'links', status: 'pending', items: [], accepted: 1})});
  });
}

function newCapture() {
  return {addMagnet: [], addFile: [], linksAdd: []};
}

test('manual magnet submission sends selection_mode=interactive', async ({page}) => {
  const capture = newCapture();
  await stubShell(page, capture);
  await page.goto('/');
  await page.waitForFunction(() => typeof window.addDashboardEntries === 'function');

  await page.fill('#q-transfer-input', 'magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
  await page.click('#btn-add-transfer');

  await expect.poll(() => capture.addMagnet.length).toBe(1);
  expect(capture.addMagnet[0].selection_mode).toBe('interactive');
  expect(capture.linksAdd).toEqual([]);
});

test('bulk magnet submission sends selection_mode=interactive on every entry', async ({page}) => {
  const capture = newCapture();
  await stubShell(page, capture);
  await page.goto('/');
  await page.waitForFunction(() => typeof window.addDashboardEntries === 'function');

  const magnets = [
    'magnet:?xt=urn:btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    'magnet:?xt=urn:btih:cccccccccccccccccccccccccccccccccccccccc',
  ];
  await page.fill('#q-transfer-input', magnets.join('\n'));
  await page.click('#btn-add-transfer');

  await expect.poll(() => capture.addMagnet.length).toBe(2);
  for (const body of capture.addMagnet) {
    expect(body.selection_mode).toBe('interactive');
  }
});

test('torrent file upload appends selection_mode=interactive to the multipart form', async ({page}) => {
  const capture = newCapture();
  await stubShell(page, capture);
  await page.goto('/');
  await page.waitForSelector('#torrent-file-input', {state: 'attached'});

  await page.setInputFiles('#torrent-file-input', {
    name: 'example.torrent',
    mimeType: 'application/x-bittorrent',
    buffer: Buffer.from('d8:announce4:test4:infod4:name4:test6:lengthi1eee'),
  });

  await expect.poll(() => capture.addFile.length).toBe(1);
  expect(capture.addFile[0]).toContain('name="selection_mode"');
  expect(capture.addFile[0]).toContain('interactive');
});

test('direct-link submission carries no selection_mode', async ({page}) => {
  const capture = newCapture();
  await stubShell(page, capture);
  await page.goto('/');
  await page.waitForFunction(() => typeof window.addDashboardEntries === 'function');

  await page.fill('#q-transfer-input', 'https://example-hoster.test/file/abcdef');
  await page.click('#btn-add-transfer');

  await expect.poll(() => capture.linksAdd.length).toBe(1);
  expect(capture.linksAdd[0]).not.toHaveProperty('selection_mode');
  expect(capture.addMagnet).toEqual([]);
});
