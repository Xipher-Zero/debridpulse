const { test, expect } = require('@playwright/test');

/* DP 1.0.13 ACL UI follow-up, Contract E: Transfer Details' Requested By is a
 * field of the metadata grid, in the right column beside Original Resource.
 * The server projects one entry per request record, oldest first, withdrawn
 * ones in place (api/routes.py _requested_by); the earliest request is always
 * shown, and every later one opens beneath it, in order and with its own
 * time, from Details' disclosure chevron, which counts them. */

const EVIDENCE = process.env.DP_EVIDENCE_DIR || '';
const at = minute => `2026-10-01T12:${String(minute).padStart(2, '0')}:00+00:00`;
const request = (name, minute, extra = {}) => ({name, kind: 'account', standing: null, token_label: null,
  via_transfer_id: null, requested_at: at(minute), withdrawn_at: null, reference: null, ...extra});
const transfer = requestedBy => ({
  id: 7, name: 'Transfer 7', display_name: 'Transfer 7', status: 'completed', progress: 100, size_bytes: 1024,
  source: 'direct_link', hash: 'hash-7', created_at: at(0), completed_at: at(0), files: [], source_outcomes: [],
  executors: ['aria2'], route_attempts: [], execution_attempts: [], events: [],
  original_resource: 'https://files.example.com/a/very/long/path/to/the/original/resource.bin',
  ...(requestedBy ? {requested_by: requestedBy} : {}),
});

async function details(page, requestedBy, width = 1440) {
  await page.setViewportSize({width, height: 900});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route(url => /^\/api\/torrents\/7$/.test(url.pathname),
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(transfer(requestedBy))}));
  await page.goto('/');
  await page.waitForFunction(() => typeof showDetail === 'function' && typeof window.downloadsDateMarkup === 'function');
  // One fixed display zone, so each request's own time reads exactly.
  await page.evaluate(() => { settingsData = {...(settingsData || {}), timezone: 'UTC'}; });
  await page.evaluate(() => showDetail(7));
  await expect(page.locator('#modal-body .detail-grid')).toBeVisible();
  return page.locator('#modal-body .detail-grid > .dp-detail-requested-by');
}
const names = field => field.locator('.dp-detail-requester .dp-detail-requester-name');
const facts = field => field.locator('.dp-detail-requester .dp-detail-requester-facts');
async function shot(page, name) {
  if (EVIDENCE) await page.locator('#modal').screenshot({path: `${EVIDENCE}/${name}.png`});
}

test('one request: a metadata field beside Original Resource, no chevron, no separate card', async ({page}) => {
  const field = await details(page, {requests: [request('Alice', 0)]});
  await expect(field.locator('.dk')).toHaveText('Requested By');
  await expect(field.locator('.dk')).toHaveCSS('text-transform', 'uppercase');
  await expect(names(field)).toHaveText(['Alice']);
  await expect(facts(field)).toHaveText(['requested Oct 1 12:00']);
  await expect(field.locator('summary')).toHaveCount(0);
  // The standalone card is gone: Route History follows the grid directly.
  await expect(page.locator('#modal-body .card.dp-detail-requested-by, #modal-body .dp-detail-requested-body')).toHaveCount(0);
  await expect(page.locator('#modal-body .card-title', {hasText: 'Requested By'})).toHaveCount(0);
  expect(await page.evaluate(() => document.querySelector('#modal-body .detail-grid').nextElementSibling.classList
    .contains('dp-detail-route-history'))).toBe(true);
  const place = await page.evaluate(() => {
    const grid = document.querySelector('#modal-body .detail-grid');
    const box = node => node.getBoundingClientRect();
    const cell = label => [...grid.children].find(node => node.querySelector(':scope > .dk')?.textContent === label);
    const [resource, requested, added, completed] = ['Original Resource', 'Requested By', 'Added', 'Completed'].map(cell).map(box);
    const body = document.querySelector('#modal-body');
    return {sameRow: Math.abs(resource.top - requested.top) < 1, rightOf: requested.left >= resource.right,
      belowDates: requested.top >= Math.max(added.bottom, completed.bottom),
      rightColumn: Math.abs(requested.right - box(grid).right) < 1,
      noScroll: body.scrollWidth <= body.clientWidth + 1};
  });
  expect(place).toEqual({sameRow: true, rightOf: true, belowDates: true, rightColumn: true, noScroll: true});
  await shot(page, 'requested-by-one');
});

test('every request is its own entry: the earliest always, the rest collapsed, in order, each with its own time', async ({page}) => {
  const field = await details(page, {requests: [
    request('Alice', 0, {withdrawn_at: at(30)}),
    request('Homepage', 5, {kind: 'integration', token_label: 'dashboard'}),
    request('Homepage', 7, {kind: 'integration', token_label: 'dashboard'}),
    request('Homepage', 9, {kind: 'integration', token_label: 'spare'}),
    request('Alice', 11, {via_transfer_id: 12}),
    request('Bob', 13, {standing: 'deleted', withdrawn_at: at(20)}),
    request('Alice', 15),
  ]});
  const more = field.locator('details.dp-detail-requesters-more');
  const summary = more.locator('summary');
  // The earliest request leads, withdrawn as it is; the count is of requests.
  await expect(summary).toHaveText('6 more requests (1 withdrawn)');
  expect(await more.evaluate(node => node.open)).toBe(false);
  await expect(names(field).filter({visible: true})).toHaveText(['Alice']);
  await expect(facts(field).filter({visible: true})).toHaveText(['requested Oct 1 12:00 · withdrawn Oct 1 12:30']);
  await expect(field.locator('.dp-detail-requesters').first().locator('.dp-detail-requester')).toHaveClass(/is-withdrawn/);
  await shot(page, 'requested-by-collapsed');

  // Keyboard: the chevron is focusable and opens and closes with Enter and Space.
  await summary.focus();
  await expect(summary).toBeFocused();
  expect(await summary.evaluate(node => getComputedStyle(node).outlineStyle)).not.toBe('none');
  await page.keyboard.press('Enter');
  expect(await more.evaluate(node => node.open)).toBe(true);
  // Each later request separately, in order, never repeating the first: the
  // same account and token twice stays two entries, with two times.
  await expect(names(field)).toHaveText(['Alice', 'Homepage', 'Homepage', 'Homepage', 'Alice', 'Bob', 'Alice']);
  await expect(facts(more)).toHaveText([
    'Integration · token “dashboard” · requested Oct 1 12:05',
    'Integration · token “dashboard” · requested Oct 1 12:07',
    'Integration · token “spare” · requested Oct 1 12:09',
    'via consolidated transfer #12 · requested Oct 1 12:11',
    'Deleted account · requested Oct 1 12:13 · withdrawn Oct 1 12:20',
    'requested Oct 1 12:15',
  ]);
  await expect(more.locator('.dp-detail-requester.is-withdrawn')).toHaveCount(1);
  await shot(page, 'requested-by-expanded');
  await page.keyboard.press('Space');
  expect(await more.evaluate(node => node.open)).toBe(false);
});

test('two requests: one more behind the chevron; a withdrawn earliest one still leads', async ({page}) => {
  const field = await details(page, {requests: [
    request('Legacy / Unknown', 0, {kind: 'legacy', withdrawn_at: at(1)}), request('Carol', 2)]});
  await expect(names(field).filter({visible: true})).toHaveText(['Legacy / Unknown']);
  await expect(field.locator('summary')).toHaveText('1 more request');
  await field.locator('summary').click();
  await expect(names(field)).toHaveText(['Legacy / Unknown', 'Carol']);
});

test('without a Requested By projection, Original Resource keeps the full row', async ({page}) => {
  const field = await details(page, null);
  await expect(field).toHaveCount(0);
  await expect(page.locator('#modal-body .dp-detail-original-resource')).toHaveAttribute('style', /grid-column:\s*1\s*\/\s*-1/);
});

test('narrow Details: one column, long names wrap, no horizontal scroll', async ({page}) => {
  const long = 'An Account With A Remarkably Long Display Name That Must Wrap Rather Than Widen The Grid';
  const field = await details(page, {requests: [request(long, 0), request('Bob', 1)]}, 390);
  await field.locator('summary').click();
  const layout = await page.evaluate(() => {
    const body = document.querySelector('#modal-body');
    const grid = body.querySelector('.detail-grid');
    const field = grid.querySelector('.dp-detail-requested-by');
    return {columns: getComputedStyle(grid).gridTemplateColumns.split(' ').length,
      inside: field.getBoundingClientRect().right <= grid.getBoundingClientRect().right + 0.5,
      noScroll: body.scrollWidth <= body.clientWidth + 1};
  });
  expect(layout).toEqual({columns: 1, inside: true, noScroll: true});
  await expect(names(field)).toHaveText([long, 'Bob']);
  await shot(page, 'requested-by-narrow');
});
