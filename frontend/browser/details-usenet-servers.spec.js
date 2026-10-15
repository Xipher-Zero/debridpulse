const { test, expect } = require('@playwright/test');

/* DP 1.0.13 Transfer Details, Usenet Server(s): for a Usenet transfer the
 * right column reads Provider, Usenet Server(s), Size, Added, Requested By;
 * the left column (Status, Progress, Submitted As, Completed, Original
 * Resource) is unchanged, and every other provider's Details is exactly as
 * before. The servers are the transfer's own recorded execution evidence
 * (api/routes.py _usenet_servers): named, never guessed. */

const EVIDENCE = process.env.DP_EVIDENCE_DIR || '';
const at = minute => `2026-10-01T12:${String(minute).padStart(2, '0')}:00+00:00`;
const server = (name, extra = {}) => ({server_id: name.toLowerCase(), name, recorded_name: name, attributed: true,
  removed: false, reported: '1.2 GB', approximate_bytes: 1288490189, ...extra});
const transfer = (extra = {}) => ({
  id: 9, name: 'Release', display_name: 'Release', status: 'completed', progress: 100, size_bytes: 734003200,
  source: 'manual_file', hash: 'hash-9', created_at: at(0), completed_at: at(5), files: [], source_outcomes: [],
  executors: ['sabnzbd'], route_attempts: [], execution_attempts: [], events: [],
  original_resource: 'Release.nzb', active_provider_id: 'usenet', active_provider_name: 'Usenet',
  requested_by: {requests: [{name: 'Radarr', kind: 'account', standing: null, token_label: null,
    via_transfer_id: null, requested_at: at(0), withdrawn_at: null, reference: null}]},
  ...extra,
});

async function details(page, body, {width = 1440, theme} = {}) {
  await page.setViewportSize({width, height: 900});
  await page.route('https://fonts.googleapis.com/**', route => route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  await page.route(url => /^\/api\/torrents\/9$/.test(url.pathname),
    route => route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(body)}));
  await page.goto('/');
  if (theme) await page.evaluate(value => document.documentElement.setAttribute('data-theme', value), theme);
  await page.waitForFunction(() => typeof showDetail === 'function' && typeof window.downloadsDateMarkup === 'function');
  await page.evaluate(() => { settingsData = {...(settingsData || {}), timezone: 'UTC'}; });
  await page.evaluate(() => showDetail(9));
  await expect(page.locator('#modal-body .detail-grid')).toBeVisible();
}

async function columns(page) {
  return page.evaluate(() => {
    const grid = document.querySelector('#modal-body .detail-grid');
    const cells = [...grid.children].filter(node => node.querySelector(':scope > .dk'));
    const middle = grid.getBoundingClientRect().left + grid.getBoundingClientRect().width / 2;
    const order = side => cells.filter(node => (node.getBoundingClientRect().left < middle) === (side === 'left'))
      .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)
      .map(node => node.querySelector(':scope > .dk').textContent)
      .filter(label => !['Transfer ID', 'Hash', 'Local Path', 'Error', 'Extraction', 'Extraction Error'].includes(label));
    const body = document.querySelector('#modal-body');
    return {left: order('left'), right: order('right'), noScroll: body.scrollWidth <= body.clientWidth + 1};
  });
}
async function shot(page, name) {
  if (EVIDENCE) await page.locator('#modal').screenshot({path: `${EVIDENCE}/${name}.png`});
}

const LEFT = ['Status', 'Progress', 'Submitted As', 'Completed', 'Original Resource'];

test('one contributing server sits beneath Provider; the left column is unchanged', async ({page}) => {
  await details(page, transfer({usenet_servers: {state: 'known', servers: [server('Primary')]}}));
  expect(await columns(page)).toEqual({left: LEFT,
    right: ['Provider', 'Usenet Server', 'Size', 'Added', 'Requested By'], noScroll: true});
  const field = page.locator('#modal-body .detail-grid > .dp-detail-usenet-servers');
  await expect(field.locator('.dk')).toHaveCSS('text-transform', 'uppercase');
  await expect(field.locator('.dv')).toHaveText('Primary');
  await expect(field.locator('.dp-detail-usenet-server')).toHaveAttribute('title', /about 1\.2 GB of articles delivered/);
  await shot(page, 'usenet-server-one');
});

test('several servers, a removed one and an unattributed name are all said as they are', async ({page}) => {
  await details(page, transfer({usenet_servers: {state: 'known', servers: [
    server('Primary'), server('Backup', {removed: true}),
    server('news.example.net', {server_id: null, attributed: false})]}}), {theme: 'light'});
  const field = page.locator('#modal-body .detail-grid > .dp-detail-usenet-servers');
  await expect(field.locator('.dk')).toHaveText('Usenet Servers');
  await expect(field.locator('.dv')).toHaveText('Primary, Backup (removed), news.example.net');
  await expect(field.locator('.dp-detail-usenet-server').nth(2)).toHaveAttribute('title', /not matched to one configured server/);
  expect((await columns(page)).right).toEqual(['Provider', 'Usenet Servers', 'Size', 'Added', 'Requested By']);
  await shot(page, 'usenet-servers-many-light');
});

test('no recorded evidence is said, never filled from configuration', async ({page}) => {
  await details(page, transfer({usenet_servers: {state: 'unavailable', servers: []}}));
  const field = page.locator('#modal-body .detail-grid > .dp-detail-usenet-servers');
  await expect(field.locator('.dv')).toHaveText('Not available');
  await expect(field.locator('.dv .is-unknown')).toHaveCount(1);
  await details(page, transfer({usenet_servers: {state: 'none', servers: []}}));
  await expect(page.locator('#modal-body .detail-grid > .dp-detail-usenet-servers .dv')).toHaveText('None reported');
});

test('any other provider is exactly as before', async ({page}) => {
  await details(page, transfer({active_provider_id: 'alldebrid', active_provider_name: 'AllDebrid'}));
  await expect(page.locator('#modal-body .dp-detail-usenet-servers')).toHaveCount(0);
  expect(await columns(page)).toEqual({left: LEFT, right: ['Provider', 'Size', 'Added', 'Requested By'],
    noScroll: true});
});

test('narrow Details keeps one column, in reading order, without horizontal scroll', async ({page}) => {
  await details(page, transfer({usenet_servers: {state: 'known', servers: [server('A very long Usenet provider name for wrapping')]}}),
    {width: 600});
  const order = await page.evaluate(() => [...document.querySelectorAll('#modal-body .detail-grid > div > .dk')]
    .map(node => node.textContent).slice(0, 4));
  expect(order).toEqual(['Status', 'Provider', 'Progress', 'Usenet Server']);
  const body = await page.evaluate(() => {
    const element = document.querySelector('#modal-body');
    return element.scrollWidth <= element.clientWidth + 1;
  });
  expect(body).toBe(true);
  await shot(page, 'usenet-server-narrow');
});
