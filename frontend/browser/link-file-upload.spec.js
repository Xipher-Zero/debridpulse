const { test, expect } = require('@playwright/test');

// A file no structured upload owner claims goes through the one existing
// picker to the server, which reads it by its content, never its name.

async function isolateExternalFonts(page) {
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

async function choose(page, name, buffer) {
  const response = page.waitForResponse(r => r.url().endsWith('/api/links/add-file'));
  await page.locator('#torrent-file-input').setInputFiles({name, mimeType: 'application/octet-stream', buffer});
  return response;
}

test('any file that is not a structured transfer file is submitted as a link list by its content', async ({page}) => {
  await isolateExternalFonts(page);
  await page.goto('/');
  const picker = page.locator('#torrent-file-input');
  await expect(picker).not.toHaveAttribute('accept', /./);  // no extension allowlist
  await expect(page.locator('input[type="file"]')).toHaveCount(1);  // the one picker, no new surface

  const created = [];
  for (const [name, buffer] of [
    ['downloads', Buffer.from('# links\nhttps://dp-link-file.example/a.bin\n')],
    ['whatever.xyz', Buffer.from('name,url\nb,https://dp-link-file.example/b.bin\n')],
  ]) {
    const response = await choose(page, name, buffer);
    expect(response.status()).toBe(200);
    const body = await response.json();
    expect(body.accepted).toBe(1);
    created.push(...body.items.map(item => item.id));
    await expect(page.locator('.toast').last()).toContainText('Link file added');
  }

  for (const [name, buffer, message] of [
    ['picture.png', Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00]), 'not a text file'],
    ['notes.txt', Buffer.from('I got this from https://dp-link-file.example/c.bin\n'), 'Line 1 is not a single link'],
  ]) {
    const response = await choose(page, name, buffer);
    expect(response.status()).toBe(400);
    await expect(page.locator('.toast').last()).toContainText(message);
  }
  for (const id of created) await page.request.delete(`/api/torrents/${id}`);
});

test('a link file naming a private-network host asks the existing confirmation and resubmits with consent', async ({page}) => {
  await isolateExternalFonts(page);
  const uploads = [];
  await page.route(url => url.pathname === '/api/links/add-file', route => {
    uploads.push(route.request().postData() || '');
    if (uploads.length === 1) {
      return route.fulfill({status: 409, contentType: 'application/json', body: JSON.stringify({detail: {
        confirmation: 'local_network', hosts: ['192.168.77.5'],
        message: 'This transfer connects to an address on your private network.'}})});
    }
    return route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({ok: true, accepted: 1, items: [{id: 51}]})});
  });
  await page.goto('/');
  await page.locator('#torrent-file-input').setInputFiles({
    name: 'lan.list', mimeType: 'text/plain', buffer: Buffer.from('http://192.168.77.5/a.bin\n')});

  const dialog = page.locator('.dp-modal-dialog');
  await expect(dialog.locator('.dp-modal-title')).toHaveText('Connect to a local network address?');
  await dialog.locator('[data-modal-accept]').click();
  await expect.poll(() => uploads.length).toBe(2);
  expect(uploads[0]).not.toContain('allow_local_network');
  expect(uploads[1]).toContain('name="allow_local_network"');
  expect(uploads[1]).toContain('name="selection_mode"');
  await expect(page.locator('.toast').last()).toContainText('Link file added');
});
