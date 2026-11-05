const { test, expect } = require('@playwright/test');

/* DP 1.0.14 WS2 -- an unverified contributed source in the target's Details
 * offers its contributor's operator Retry, only when the backend says it may
 * (reconsider_available) and only to an operator; a consolidated contributor
 * still owed proof reads "Consolidated · Unverified" in the caution tone. */

const detail = (id, attempts, extra = {}) => ({
  id, name: 'Unverified contribution', status: 'downloading', progress: 10, size_bytes: 1024,
  source: 'direct_link', request_kinds: ['https'], hash: '',
  created_at: '2026-01-01T00:00:00Z', original_resource: 'https://hoster.example/f/movie.bin',
  files: [], source_outcomes: [], events: [], executors: [],
  current_provider_name: 'Debrid-Link', active_provider_name: 'Debrid-Link',
  route_attempts: attempts, ...extra,
});

const attempts = [
  {ordinal: 1, presentation_ordinal: 1, provider_id: 'debridlink', provider_name: 'Debrid-Link',
   outcome: 'completed', relation: 'original', route_identity: 'https://hoster.example/f/movie.bin'},
  {ordinal: 1, presentation_ordinal: 2, provider_id: 'alldebrid', provider_name: 'AllDebrid',
   outcome: 'resolved', relation: 'consolidated', verification_state: 'verified', contributing_transfer_id: 411,
   route_identity: 'https://verified.example/movie.bin'},
  {ordinal: 1, presentation_ordinal: 3, provider_id: 'alldebrid', provider_name: 'AllDebrid',
   outcome: 'resolved', relation: 'unverified', verification_state: 'unverified', contributing_transfer_id: 412,
   unverified_reason: 'sampler_unavailable', reconsider_available: true,
   route_identity: 'https://other.example/movie.bin'},
  {ordinal: 1, presentation_ordinal: 4, provider_id: 'alldebrid', provider_name: 'AllDebrid',
   outcome: 'resolved', relation: 'unverified', verification_state: 'unverified', contributing_transfer_id: 413,
   unverified_reason: 'sampler_unavailable', reconsider_available: false,
   route_identity: 'https://pending.example/movie.bin'},
];

async function withoutOperate(page) {
  const drop = authority => ({...authority, role: 'readonly',
    capabilities: authority.capabilities.filter(item => !['administer', 'transfers.operate'].includes(item))});
  await page.route(url => url.pathname === '/app.js', async route => {
    const body = await (await route.fetch()).text();
    const marker = 'window.debridPulseAuthority=Object.freeze(';
    if (!body.includes(marker)) return route.fulfill({status: 200, contentType: 'application/javascript', body});
    const [head, rest] = body.split(marker);
    const end = rest.indexOf(');');
    return route.fulfill({status: 200, contentType: 'application/javascript',
      body: head + marker + JSON.stringify(drop(JSON.parse(rest.slice(0, end)))) + rest.slice(end)});
  });
  await page.route('**/api/auth/session', async route => route.fulfill({status: 200, contentType: 'application/json',
    body: JSON.stringify(drop(await (await route.fetch()).json()))}));
}

async function open(page, payload, {retryStatus = 200} = {}) {
  const calls = {reads: 0, retries: []};
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route(url => url.pathname === `/api/torrents/${payload.id}`, route => {
    calls.reads += 1;
    return route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(payload)});
  });
  await page.route(url => url.pathname === '/api/torrents', route => route.fulfill({status: 200,
    contentType: 'application/json', body: JSON.stringify({items: [], total: 0})}));
  await page.route(url => /^\/api\/torrents\/\d+\/retry$/.test(url.pathname), route => {
    calls.retries.push(new URL(route.request().url()).pathname);
    return route.fulfill({status: retryStatus, contentType: 'application/json',
      body: JSON.stringify(retryStatus === 200 ? {ok: true} : {detail: 'Recovery failed'})});
  });
  await page.goto('/');
  await page.evaluate(id => showDetail(id), payload.id);
  await expect(page.locator('.dp-detail-route-row')).toHaveCount(payload.route_attempts.length);
  return calls;
}

const row = (page, contributor) => page.locator('.dp-detail-route-row', {hasText: `From #${contributor}`});

test('only the reconsiderable unverified contribution offers Retry, reachable by keyboard', async ({page}) => {
  const calls = await open(page, detail(980, attempts));
  await expect(page.locator('.dp-detail-route-retry')).toHaveCount(1);
  const retry = row(page, 412).getByRole('button', {name: 'Retry verifying the source from #412'});
  await expect(retry).toBeVisible();
  await expect(row(page, 412).locator('.dp-detail-route-outcome')).toHaveText('Unverified');
  await expect(row(page, 411).locator('button')).toHaveCount(0);   // verified contribution
  await expect(row(page, 413).locator('button')).toHaveCount(0);   // backend: not reconsiderable now
  const reads = calls.reads;
  await retry.focus();
  await expect(retry).toBeFocused();
  await page.keyboard.press('Enter');
  await expect.poll(() => calls.retries).toEqual(['/api/torrents/412/retry']);
  // Details shows the server's state again.
  await expect.poll(() => calls.reads).toBeGreaterThan(reads);
});

test('a refused Retry says so and Details is read again', async ({page}) => {
  const calls = await open(page, detail(981, attempts), {retryStatus: 409});
  const reads = calls.reads;
  await row(page, 412).getByRole('button', {name: 'Retry verifying the source from #412'}).click();
  await expect(page.locator('.toast').last()).toContainText('Recovery failed');
  await expect.poll(() => calls.reads).toBeGreaterThan(reads);
});

test('a viewer who may not operate sees the unverified source but no Retry', async ({page}) => {
  await withoutOperate(page);
  await open(page, detail(982, attempts));
  expect(await page.evaluate(() => dpCan('transfers.operate'))).toBe(false);
  await expect(row(page, 412).locator('.dp-detail-route-outcome')).toHaveText('Unverified');
  await expect(page.locator('.dp-detail-route-retry')).toHaveCount(0);
});

test('at 320px the Retry stays inside the row and the panel does not scroll sideways', async ({page}) => {
  await page.setViewportSize({width: 320, height: 720});
  await open(page, detail(983, attempts));
  const retry = row(page, 412).getByRole('button', {name: 'Retry verifying the source from #412'});
  await expect(retry).toBeVisible();
  const geometry = await page.evaluate(() => {
    const button = document.querySelector('.dp-detail-route-retry');
    const rowBox = button.closest('.dp-detail-route-row').getBoundingClientRect();
    const box = button.getBoundingClientRect();
    return {left: box.left, right: box.right, rowLeft: rowBox.left, rowRight: rowBox.right, height: box.height,
            pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth};
  });
  expect(geometry.left).toBeGreaterThanOrEqual(geometry.rowLeft);
  expect(geometry.right).toBeLessThanOrEqual(geometry.rowRight);
  expect(geometry.right).toBeLessThanOrEqual(320);
  expect(geometry.pageOverflow).toBeLessThanOrEqual(0);
});

test('a consolidated contributor still owed proof reads as unverified, in the caution tone', async ({page}) => {
  await open(page, detail(984, [], {status: 'consolidated', progress: 0, presentation_status: 'consolidated',
    presentation_label: 'Consolidated · Unverified', presentation_badge_status: 'partial', unverified_association_count: 1}));
  const status = page.locator('#modal-body .badge').first();
  await expect(status).toContainText('Consolidated · Unverified');
  await expect(status).toHaveClass(/partial/);
});
