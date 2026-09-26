const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Transfer Trace Log -- a Details header action, immediately left of
 * Close, that downloads the file the backend's one trace owner builds. The UI
 * builds nothing: it requests GET /api/torrents/{id}/trace and saves the reply. */

const detail = {
  id: 7301, name: 'Trace subject', status: 'consolidated', progress: 100, size_bytes: 1024,
  created_at: '2026-01-01T00:00:00Z', completed_at: null, hash: '', files: [], source_outcomes: [],
  events: [], executors: [], route_attempts: [], current_provider_name: 'Parcel', delivering_provider_name: 'Parcel',
};
const tracePath = `/api/torrents/${detail.id}/trace`;
const filename = `debridpulse-transfer-${detail.id}-trace-20260926T120000Z.json`;

async function openDetail(page, traceReply) {
  const requested = [];
  await page.route(url => url.pathname === tracePath, route => {
    requested.push(route.request().method());
    return route.fulfill(traceReply);
  });
  await page.route(url => url.pathname === `/api/torrents/${detail.id}`,
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(detail)}));
  await page.route(url => url.pathname === '/api/torrents',
    route => route.fulfill({status: 200, contentType: 'application/json',
      body: JSON.stringify({items: [detail], total: 1})}));
  await page.goto('/');
  await page.evaluate(id => showDetail(id), detail.id);
  await expect(page.locator('#modal-title')).toHaveText(detail.name);
  return requested;
}

test('Transfer Trace Log sits immediately left of Close and downloads the backend trace', async ({page}) => {
  const requested = await openDetail(page, {
    status: 200, contentType: 'application/json',
    headers: {'Content-Disposition': `attachment; filename="${filename}"`},
    body: JSON.stringify({metadata: {requested_transfer_id: detail.id}}),
  });
  const trace = page.locator('#modal .modal-hdr .dp-detail-trace');
  await expect(trace).toBeVisible();
  await expect(trace).toHaveText('Transfer Trace Log');
  expect(await trace.evaluate(node => node.tagName === 'BUTTON' && node.type === 'button'
    && node.nextElementSibling === document.querySelector('#modal .modal-close'))).toBe(true);

  await trace.focus();
  const [download] = await Promise.all([page.waitForEvent('download'), page.keyboard.press('Enter')]);
  expect(download.suggestedFilename()).toBe(filename);
  expect(requested).toEqual(['GET']);
  await expect(trace).toHaveText('Transfer Trace Log');
  await expect(trace).toBeEnabled();

  // Close is unchanged: it still closes Details, and the action leaves with it.
  await page.locator('#modal .modal-close').click();
  await expect(page.locator('#overlay')).not.toHaveClass(/open/);
  await expect(trace).toBeHidden();
});

test('a failed trace export reports through the toast path and restores the control', async ({page}) => {
  await openDetail(page, {status: 404, contentType: 'application/json',
    body: JSON.stringify({detail: 'Transfer not found'})});
  const trace = page.locator('#modal .dp-detail-trace');
  let downloaded = false;
  page.on('download', () => { downloaded = true; });
  await trace.click();
  await expect(page.locator('#toasts .toast').last()).toContainText('Transfer not found');
  await expect(trace).toHaveText('Transfer Trace Log');
  await expect(trace).toBeEnabled();
  expect(downloaded).toBe(false);
  await expect(page.locator('#overlay')).toHaveClass(/open/);
});
