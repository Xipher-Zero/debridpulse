const { test, expect } = require('@playwright/test');

/* DP 1.0.13 interactive intent meets remote collections, end to end.
 *
 * Nothing here is fabricated. The test-composition server
 * (backend/tests/browser_contract_app.py, run from the candidate image) is the
 * real application -- real admission, real engine, real GeneralHttpProvider /
 * GeneralFtpProvider / ScpProvider, real core-run discovery and the real
 * neutral file-selection owner -- with one in-memory transport that reports
 * whether a remote path is one file or a directory of three files.
 *
 * Every link is submitted through the real Add box, so the browser's own
 * interactive intent reaches the real backend. Intent never forces a picker:
 * a single file of any protocol completes with no selector; a multi-file
 * FTP/SFTP/SCP directory opens the selector before any member writer exists,
 * Confirm materializes only the chosen subset, and Close keeps every member.
 *
 * This file writes no shared settings key, uses its own server and submits
 * each source spelling exactly once. */

const BASE = process.env.DP_CONTRACT_BASE_URL;
const HOST = 'remote.contract.test';
const MEMBERS = ['alpha.bin', 'beta.bin', 'gamma.bin'];
const SELECTOR = '#modal[data-dp-modal-mode="file-selection"]';

async function open(page) {
  expect(BASE, 'DP_CONTRACT_BASE_URL must name the test-composition server').toBeTruthy();
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.goto(`${BASE}/`);
  await page.waitForFunction(() => typeof window.addDashboardEntries === 'function');
}

async function submit(page, link) {
  const added = page.waitForResponse(response =>
    response.url().endsWith('/api/links/add') && response.request().method() === 'POST');
  await page.fill('#q-transfer-input', link);
  await page.click('#btn-add-transfer');
  const response = await added;
  expect(response.ok(), `the real admission path rejected ${link}`).toBeTruthy();
  expect(response.request().postDataJSON().selection_mode).toBe('interactive');
  return (await response.json()).id;
}

async function detail(page, id) {
  return (await page.request.get(`${BASE}/api/torrents/${id}`)).json();
}

async function selection(page, id) {
  return (await page.request.get(`${BASE}/api/torrents/${id}/file-selection`)).json();
}

async function completedNames(page, id) {
  await expect.poll(async () => (await detail(page, id)).status,
    {timeout: 30000, message: 'the transfer never completed'}).toBe('completed');
  return (await detail(page, id)).files.map(file => file.filename).sort();
}

for (const link of [`https://${HOST}/file.bin`, `ftp://${HOST}/file.bin`, `sftp://${HOST}/file.bin`,
  `scp://${HOST}/file.bin`]) {
  test(`a single ${link.split(':')[0].toUpperCase()} file never offers the selector`, async ({page}) => {
    await open(page);
    const id = await submit(page, link);
    expect(await completedNames(page, id)).toEqual(['file.bin']);
    expect((await selection(page, id)).eligible).toBe(false);
    await expect(page.locator(SELECTOR)).toHaveCount(0);
  });
}

for (const link of [`ftp://${HOST}/dir`, `sftp://${HOST}/dir/`, `scp://${HOST}/dir/`]) {
  test(`a multi-file ${link.split(':')[0].toUpperCase()} directory opens the selector before any writer; Confirm keeps only the subset`,
    async ({page}) => {
      await open(page);
      const id = await submit(page, link);
      const selector = page.locator(SELECTOR);
      await expect(selector).toBeVisible({timeout: 30000});
      await expect(page.locator('.dp-fs-tree .dp-fs-check--file')).toHaveCount(MEMBERS.length);
      // The decision is outstanding: no member has materialized.
      const view = await selection(page, id);
      expect(view.decision).toBe('pending');
      expect((await detail(page, id)).files).toEqual([]);

      const gamma = view.entries.find(entry => entry.name === 'gamma.bin');
      await page.locator(`.dp-fs-check--file[data-entry-id="${gamma.entry_id}"]`).uncheck();
      const confirmed = page.waitForResponse(response =>
        response.url().endsWith(`/api/torrents/${id}/file-selection/confirm`));
      await page.locator('#modal-footer .dp-fs-confirm').click();
      expect((await confirmed).ok(), 'the real confirm endpoint rejected the subset').toBeTruthy();
      await expect(selector).toHaveCount(0);
      expect(await completedNames(page, id)).toEqual(['alpha.bin', 'beta.bin']);
    });
}

test('closing the selector for an SFTP directory keeps every member', async ({page}) => {
  await open(page);
  const id = await submit(page, `sftp://${HOST}/dir`);
  await expect(page.locator(SELECTOR)).toBeVisible({timeout: 30000});
  expect((await detail(page, id)).files).toEqual([]);
  const dismissed = page.waitForResponse(response =>
    response.url().endsWith(`/api/torrents/${id}/file-selection/dismiss`));
  await page.locator('#modal-footer .dp-fs-close').click();
  expect((await dismissed).ok(), 'the real dismiss endpoint rejected the close').toBeTruthy();
  expect(await completedNames(page, id)).toEqual(MEMBERS);
});
