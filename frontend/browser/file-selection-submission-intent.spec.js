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
  await page.keyboard.press('Escape');                                          // inert: no answer
  await expect(dialog.locator('.dp-modal-code')).toHaveText(MIX_TWO);
  expect(capture.linksAdd.length).toBe(1);
  await dialog.locator('[data-modal-cancel]').click();                          // cancels THIS link only

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

// DP 1.0.13 collection modal UX: the scope dialog is blocking. Its hint is
// centred on the dialog frame and the link stays left-aligned; red Cancel
// Transfer leads the footer and Current Video / Playlist share its far end with
// equal, neutral weight. The backdrop, Escape and time decide nothing.
for (const width of [1440, 390]) {
  test(`the scope dialog is blocking, with its layout and neutral choices at ${width}px`, async ({page}) => {
    const capture = newCapture();
    await stubScopeAdmission(page, capture);
    await page.setViewportSize({width, height: 900});
    await bootQuickAdd(page);
    await page.fill('#q-transfer-input', MIX_ONE);
    await page.click('#btn-add-transfer');
    const dialog = page.locator('.dp-modal-overlay [role="dialog"]');
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveAttribute('aria-modal', 'true');

    const frame = await dialog.boundingBox();
    const hint = dialog.locator('.dp-modal-message--center');
    await expect(hint).toHaveCSS('text-align', 'center');
    const hintBox = await hint.boundingBox();
    expect(Math.abs((hintBox.x + hintBox.width / 2) - (frame.x + frame.width / 2))).toBeLessThanOrEqual(1);
    const code = dialog.locator('.dp-modal-code');
    await expect(code).toHaveText(MIX_ONE);
    expect(await code.evaluate(node => getComputedStyle(node).textAlign)).toMatch(/^(start|left)$/);

    const cancel = dialog.locator('[data-modal-cancel]');
    await expect(cancel).toHaveText('Cancel Transfer');
    await expect(cancel).toHaveClass(/\bbtn-danger\b/);
    const fsDanger = await page.evaluate(() => {
      const probe = document.createElement('button');
      probe.className = 'btn btn-danger dp-fs-cancel';
      document.body.appendChild(probe);
      const style = getComputedStyle(probe);
      const material = [style.backgroundImage, style.backgroundColor, style.color, style.borderColor];
      probe.remove();
      return material;
    });
    expect(await cancel.evaluate(node => {
      const style = getComputedStyle(node);
      return [style.backgroundImage, style.backgroundColor, style.color, style.borderColor];
    })).toEqual(fsDanger);
    const actions = dialog.locator('[data-modal-action]');
    await expect(actions).toHaveText(['Current Video', 'Playlist']);
    await expect(dialog.locator('[data-modal-accept]')).toHaveCount(0);
    const looks = await actions.evaluateAll(nodes => nodes.map(node => {
      const style = getComputedStyle(node);
      return {cls: node.className, bg: style.backgroundColor, image: style.backgroundImage, color: style.color,
        border: style.borderColor, weight: style.fontWeight, size: style.fontSize, pressed: node.getAttribute('aria-pressed')};
    }));
    expect(looks[0]).toEqual(looks[1]);
    expect(looks[0].cls).toContain('btn-ghost');
    expect(looks[0].cls).not.toMatch(/btn-(primary|success|danger)/);
    expect(looks[0].pressed).toBeNull();

    const footer = await dialog.locator('.dp-modal-footer').boundingBox();
    const cancelBox = await cancel.boundingBox();
    const video = await actions.nth(0).boundingBox();
    const playlist = await actions.nth(1).boundingBox();
    for (const box of [cancelBox, video, playlist]) {
      expect(box.x).toBeGreaterThanOrEqual(frame.x);
      expect(box.x + box.width).toBeLessThanOrEqual(frame.x + frame.width + 0.5);
    }
    if (width === 1440) {
      // Cancel Transfer at the far left; the two choices together at the far right.
      expect(cancelBox.x - footer.x).toBeLessThanOrEqual(19);
      expect(Math.abs((footer.x + footer.width) - (playlist.x + playlist.width) - 18)).toBeLessThanOrEqual(1);
      expect(Math.abs(video.y - playlist.y)).toBeLessThanOrEqual(0.5);
      expect(playlist.x - (video.x + video.width)).toBeLessThanOrEqual(10.5);
      expect(video.x - (cancelBox.x + cancelBox.width)).toBeGreaterThan(40);
    } else {
      // The narrow stack keeps the existing full-width column; Cancel Transfer is last.
      expect(cancelBox.y).toBeGreaterThan(video.y);
      expect(cancelBox.y).toBeGreaterThan(playlist.y);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    }

    // Blocking: the backdrop, Escape and waiting settle nothing.
    await page.mouse.click(4, 4);
    await page.keyboard.press('Escape');
    await page.waitForTimeout(1500);
    await expect(dialog).toBeVisible();
    expect(capture.linksAdd.length).toBe(1);
    // X cancels this link exactly as Cancel Transfer does.
    await dialog.locator('[data-modal-close]').click();
    await expect(page.locator('.dp-modal-overlay')).toHaveCount(0);
    expect(capture.linksAdd.length).toBe(1);
    await expect(page.locator('#q-transfer-input')).toHaveValue(MIX_ONE);
  });
}

test('the scope dialog is keyboard-complete: focus starts inside, stays inside, and X / Cancel Transfer / choices answer', async ({page}) => {
  const capture = newCapture();
  await stubScopeAdmission(page, capture);
  await bootQuickAdd(page);
  await page.fill('#q-transfer-input', [MIX_ONE, MIX_TWO, PLAYLIST].join('\n'));
  await page.click('#btn-add-transfer');
  const dialog = page.locator('.dp-modal-overlay [role="dialog"]');
  await expect(dialog).toHaveAttribute('aria-modal', 'true');
  await expect(dialog).toHaveAttribute('aria-labelledby', /.+/);
  await expect(dialog.locator('[data-modal-cancel]')).toBeFocused();
  const order = [];
  for (let step = 0; step < 4; step += 1) {
    await page.keyboard.press('Tab');
    order.push(await page.evaluate(() => {
      const node = document.activeElement;
      return node.closest('.dp-modal-dialog') ? (node.getAttribute('aria-label') || node.textContent.trim()) : 'OUTSIDE';
    }));
  }
  expect(order).toEqual(['Current Video', 'Playlist', 'Close', 'Cancel Transfer']);   // wraps, never leaves
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.press('Shift+Tab');
  await expect(dialog.locator('[data-modal-action="collection"]')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(dialog.locator('.dp-modal-code')).toHaveText(MIX_ONE);
  await page.keyboard.press('Enter');                                          // Playlist for MIX_ONE
  await expect(dialog.locator('.dp-modal-code')).toHaveText(MIX_TWO);
  await dialog.locator('[data-modal-close]').focus();
  await page.keyboard.press('Enter');                                          // X cancels MIX_TWO only
  await expect.poll(() => capture.linksAdd.length).toBe(2);
  expect(capture.linksAdd[1].links).toEqual([MIX_ONE, PLAYLIST]);
  expect(capture.linksAdd[1].acquisition_scopes).toEqual({[MIX_ONE]: 'collection'});
  await expect(page.locator('#q-transfer-input')).toHaveValue(MIX_TWO);
});

test('in one paste, a cancelled scope dialog and a cancelled collection selector each cancel only their own line', async ({page}) => {
  const capture = newCapture();
  await stubScopeAdmission(page, capture);
  const MIX_THREE = 'https://www.youtube.com/watch?v=ccccccccccc&list=PLccccccccccc';
  const members = [1, 2].map(n => ({entry_id: `m-${n}`, name: `Song ${n}.webm`, relative_path: `Song ${n}.webm`, size_bytes: 0}));
  const cancelled = [];
  const confirmed = {};
  for (const id of [21, 22]) {
    await page.route(url => url.pathname === `/api/torrents/${id}/file-selection`, route =>
      route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
        eligible: true, mutable: true, manifest_id: `manifest-${id}`, decision: 'pending', file_count: 2,
        entries: members, selected_entry_ids: [], auto_offer: true, decision_deadline: null, server_now: 1,
        explicit_only: true, file_selection_affordance: 'choose'})}));
    await page.route(url => url.pathname === `/api/torrents/${id}`, route =>
      route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({id, name: `collection ${id}`})}));
    await page.route(url => url.pathname === `/api/torrents/${id}/cancel`, route => {
      cancelled.push(id);
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true})});
    });
    await page.route(url => url.pathname === `/api/torrents/${id}/file-selection/confirm`, route => {
      confirmed[id] = route.request().postDataJSON();
      return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({ok: true})});
    });
  }
  await bootQuickAdd(page);
  await page.fill('#q-transfer-input', [MIX_ONE, MIX_TWO, MIX_THREE].join('\n'));
  await page.click('#btn-add-transfer');
  const dialog = page.locator('.dp-modal-overlay [role="dialog"]');
  await expect(dialog.locator('.dp-modal-code')).toHaveText(MIX_ONE);
  await dialog.locator('[data-modal-cancel]').click();                          // line 1 only
  await expect(dialog.locator('.dp-modal-code')).toHaveText(MIX_TWO);
  await dialog.locator('[data-modal-action="collection"]').click();
  await expect(dialog.locator('.dp-modal-code')).toHaveText(MIX_THREE);
  await dialog.locator('[data-modal-action="collection"]').click();
  await expect.poll(() => capture.linksAdd.length).toBe(2);
  expect(capture.linksAdd[1].links).toEqual([MIX_TWO, MIX_THREE]);
  await expect(page.locator('#q-transfer-input')).toHaveValue(MIX_ONE);

  // Lines 2 and 3 were admitted as their own collections (21, 22). Cancelling
  // 21 from its selector cancels 21 alone; 22 is still offered and decidable.
  const offer = id => page.evaluate(transferId => document.dispatchEvent(new CustomEvent(
    'debridpulse:file-selection-available', {detail: {transfer_id: transferId}})), id);
  await offer(21);
  await expect(page.locator('#modal[data-dp-modal-mode="file-selection"]')).toBeVisible();
  await page.locator('#modal-footer .dp-fs-cancel').click();
  await expect.poll(() => cancelled).toEqual([21]);
  await offer(22);
  await expect(page.locator('#modal[data-dp-modal-mode="file-selection"]')).toBeVisible();
  await expect(page.locator('.dp-fs-subtitle')).toHaveText('collection 22');
  await page.locator('#modal-footer .dp-fs-confirm').click();
  await expect.poll(() => confirmed[22]).toEqual({manifest_id: 'manifest-22', entry_ids: ['m-1', 'm-2']});
  expect(cancelled).toEqual([21]);
  await expect(page.locator('#q-transfer-input')).toHaveValue(MIX_ONE);
});
