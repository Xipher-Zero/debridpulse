const { test, expect } = require('@playwright/test');

// Torrent/Magnet File-Selection Lifecycle Correction §6 / §24 — the built-in
// browser is an interactive client and MUST explicitly opt every torrent/magnet
// submission into the interactive file-selection lifecycle
// (selection_mode=interactive). Direct-link submission declares the same
// interactive intent (DP 1.0.13): intent never forces a picker -- the server
// offers one only for an actionable multi-file manifest. The server never
// infers interactive intent from an SSE connection, a session, a user agent,
// or the source string.

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

test('direct-link submission sends selection_mode=interactive', async ({page}) => {
  const capture = newCapture();
  await stubShell(page, capture);
  await page.goto('/');
  await page.waitForFunction(() => typeof window.addDashboardEntries === 'function');

  await page.fill('#q-transfer-input', 'https://example-hoster.test/file/abcdef');
  await page.click('#btn-add-transfer');

  await expect.poll(() => capture.linksAdd.length).toBe(1);
  expect(capture.linksAdd[0].selection_mode).toBe('interactive');
  expect(capture.addMagnet).toEqual([]);
});

// DP 1.0.13 generalized collection acquisition: a link naming one item inside
// an enclosing collection is admitted only with the operator's explicit scope.
// The mocked admission answers exactly as the backend does: 409
// ``acquisition_scope`` naming each unanswered link by its position, nothing
// admitted, until every such link carries its own answer.
const VIDEO = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ';
const MIX_ONE = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ&index=12';
const MIX_TWO = 'https://music.youtube.com/watch?v=aaaaaaaaaaa&list=RDAMVMaaaaaaaaaaa';
const PLAYLIST = 'https://www.youtube.com/playlist?list=PLbpi6ZahtOH6Ar_3GPy3workQZiTQKzxs';
const CHOICES = [{scope: 'item', label: 'Current Video'}, {scope: 'collection', label: 'Playlist'}];

async function stubScopeAdmission(page, capture) {
  await stubShell(page, capture);
  await page.unroute(url => url.pathname === '/api/links/add').catch(() => {});
  await page.route(url => url.pathname === '/api/links/add', route => {
    const body = route.request().postDataJSON();
    capture.linksAdd.push(body);
    const cells = body.links.flatMap(link => link.split('\t'));
    const answers = body.acquisition_scopes || {};
    const missing = cells.map((cell, index) => ({cell, index}))
      .filter(({cell}) => /[?&]v=/.test(cell) && /[?&]list=/.test(cell) && !answers[cell])
      .map(({index}) => ({index, choices: CHOICES}));
    if (missing.length) {
      return route.fulfill({status: 409, contentType: 'application/json', body: JSON.stringify({detail: {
        confirmation: 'acquisition_scope', links: missing,
        message: 'Choose whether to download the linked item or its collection.'}})});
    }
    return route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({id: 13, name: 'links', status: 'pending', items: [], accepted: cells.length})});
  });
}

async function bootQuickAdd(page) {
  await page.goto('/');
  await page.waitForFunction(() => typeof window.addDashboardEntries === 'function' && window.DPSettingsModal);
}

test('an ordinary video link and a playlist-only link are submitted with no scope dialog', async ({page}) => {
  const capture = newCapture();
  await stubScopeAdmission(page, capture);
  await bootQuickAdd(page);
  await page.fill('#q-transfer-input', [VIDEO, PLAYLIST].join('\n'));
  await page.click('#btn-add-transfer');
  await expect.poll(() => capture.linksAdd.length).toBe(1);
  expect(capture.linksAdd[0].links).toEqual([VIDEO, PLAYLIST]);
  expect(capture.linksAdd[0].acquisition_scopes).toBeUndefined();
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
});

test('a video inside its playlist asks Current Video or Playlist; the choice is sent for that link', async ({page}) => {
  const capture = newCapture();
  await stubScopeAdmission(page, capture);
  await bootQuickAdd(page);
  await page.fill('#q-transfer-input', MIX_ONE);
  await page.click('#btn-add-transfer');
  const dialog = page.locator('.dp-modal-overlay [role="dialog"]');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('.dp-modal-title')).toHaveText('Download the item or its collection?');
  await expect(dialog.locator('.dp-modal-code')).toHaveText(MIX_ONE);
  await expect(dialog.locator('[data-modal-action]')).toHaveText(['Current Video', 'Playlist']);
  await expect(dialog.locator('[data-modal-accept]')).toHaveCount(0);           // no default answer
  await expect(dialog.locator('[data-modal-cancel]')).toBeFocused();
  await dialog.locator('[data-modal-action="collection"]').click();
  await expect.poll(() => capture.linksAdd.length).toBe(2);
  expect(capture.linksAdd[1]).toMatchObject({links: [MIX_ONE], selection_mode: 'interactive',
    acquisition_scopes: {[MIX_ONE]: 'collection'}});
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
  await expect(page.locator('#q-transfer-input')).toHaveValue('');
});

test('several ambiguous lines are asked one at a time in input order; a cancel keeps only its own line', async ({page}) => {
  const capture = newCapture();
  await stubScopeAdmission(page, capture);
  await bootQuickAdd(page);
  const fileA = 'https://files.example.test/a.bin';
  const fileB = 'https://files.example.test/b.bin';
  await page.fill('#q-transfer-input', [fileA, MIX_ONE, fileB, MIX_TWO, PLAYLIST].join('\n'));
  await page.click('#btn-add-transfer');

  const dialog = page.locator('.dp-modal-overlay [role="dialog"]');
  await expect(dialog.locator('.dp-modal-code')).toHaveText(MIX_ONE);
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(1);              // never stacked
  await dialog.locator('[data-modal-action="item"]').click();
  await expect(dialog.locator('.dp-modal-code')).toHaveText(MIX_TWO);
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(1);
  await page.keyboard.press('Escape');                                          // cancels THIS link only

  await expect.poll(() => capture.linksAdd.length).toBe(2);
  expect(capture.linksAdd[1].links).toEqual([fileA, MIX_ONE, fileB, PLAYLIST]);
  expect(capture.linksAdd[1].acquisition_scopes).toEqual({[MIX_ONE]: 'item'});   // no batch default
  await expect(page.locator('#q-transfer-input')).toHaveValue(MIX_TWO);
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
});

test('a link file with a video inside its playlist is declined, never guessed', async ({page}) => {
  const capture = newCapture();
  await stubShell(page, capture);
  let uploads = 0;
  await page.route(url => url.pathname === '/api/links/add-file', route => {
    uploads += 1;
    return route.fulfill({status: 409, contentType: 'application/json', body: JSON.stringify({detail: {
      confirmation: 'acquisition_scope', links: [{index: 0, choices: CHOICES}],
      message: 'Choose whether to download the linked item or its collection.'}})});
  });
  await page.goto('/');
  await page.waitForSelector('#torrent-file-input', {state: 'attached'});
  await page.setInputFiles('#torrent-file-input', {name: 'links.txt', mimeType: 'text/plain',
    buffer: Buffer.from(MIX_ONE + '\n')});
  await expect(page.locator('.toast')).toContainText('Quick Add');
  expect(uploads).toBe(1);
  await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
});
