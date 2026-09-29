const { test, expect } = require('@playwright/test');

/* DP 1.0.13 adverse multi-source convergence, end to end.
 *
 * The test-composition server (backend/tests/browser_contract_app.py) is the
 * real application with one in-memory transport. Three browser-level facts:
 *
 * 1. Two locked sources of ONE transfer each need their own login. The
 *    answered source's canonical attach is deliberately held (8 s); the next
 *    source's question must be presented while that work is still held -- one
 *    modal at a time, never duplicated, never resurrected.
 * 2. A canonical whose selected route's writer is admitted and then vanishes
 *    with no progress fails over to its verified alternate: the row never
 *    stays in Waiting for Retry.
 * 3. A contributor whose one source's proof only times out settles into the
 *    canonical transfer: it leaves the Downloads list, and the canonical
 *    transfer's route history keeps that source as Unverified.
 *
 * No client-side timing or optimism: the existing modal and list react to
 * durable backend state alone. The measured times are reported as annotations. */

const BASE = process.env.DP_CONTRACT_BASE_URL;
const USERNAME = 'contract-operator';
const PASSWORD = 'contract-password';
const HOLD_MS = 8000;

async function setup(page) {
  expect(BASE, 'DP_CONTRACT_BASE_URL must name the test-composition server').toBeTruthy();
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
}

async function add(page, links) {
  const added = await page.request.post(`${BASE}/api/links/add`, {data: {links}});
  expect(added.ok(), `the real admission path rejected ${links}`).toBeTruthy();
  return (await added.json()).id;
}

async function detail(page, id) {
  return (await page.request.get(`${BASE}/api/torrents/${id}`)).json();
}

// This spec's own in-progress canonical copy, submitted once per run (a
// second submission would be a second transfer that consolidates into it).
let seedId = null;
async function seed(page) {
  seedId = seedId ?? await add(page, ['https://seed.contract.test/multi.bin']);
  await expect.poll(async () => (await detail(page, seedId)).status, {timeout: 30000}).toBe('downloading');
  return seedId;
}

async function answer(page, id, modal) {
  const submitted = page.waitForResponse(response =>
    response.url().endsWith(`/api/torrents/${id}/input`) && response.request().method() === 'POST');
  await modal.locator('[data-dp-auth-username]').fill(USERNAME);
  await modal.locator('[data-dp-auth-secret]').fill(PASSWORD);
  const clicked = Date.now();
  await modal.locator('[data-dp-auth-continue]').click();
  expect((await submitted).ok(), 'the real input endpoint rejected the answer').toBeTruthy();
  return clicked;
}

async function detached(page, handle, timeout) {
  await page.waitForFunction(element => !element.isConnected, handle, {timeout});
}

test('each source of one transfer is asked promptly, one question at a time', async ({page}) => {
  await setup(page);
  await seed(page);
  const accepted = Date.now();
  const id = await add(page, ['https://multi-a.contract.test/multi.bin', 'https://multi-b.contract.test/multi.bin']);
  await expect.poll(async () => (await detail(page, id)).input_required?.origin ?? null,
    {timeout: 30000, message: 'the engine never raised the first question'}).toBe('evidence');
  const first = (await detail(page, id)).input_required;
  const firstCreated = Date.now() - accepted;

  await page.goto(`${BASE}/`);
  const modals = page.locator('[data-dp-input-required-modal]');
  const modal = page.locator(`[data-dp-input-required-modal][data-dp-auth-transfer-id="${id}"]`);
  await expect(modal).toBeVisible({timeout: 15000});
  const firstVisible = Date.now() - accepted;
  await expect(modals).toHaveCount(1);

  const firstModal = await modal.elementHandle();
  const clicked = await answer(page, id, modal);
  await detached(page, firstModal, HOLD_MS);
  const firstRetired = Date.now() - clicked;

  // The next source asks while the answered source's attach is still held.
  await expect.poll(async () => (await detail(page, id)).input_required?.subject ?? null,
    {timeout: HOLD_MS, message: 'the next source never asked'}).not.toBeNull();
  const second = (await detail(page, id)).input_required;
  expect(second.subject).not.toBe(first.subject);
  const secondCreated = Date.now() - clicked;
  await expect(modal).toBeVisible({timeout: HOLD_MS});
  const secondVisible = Date.now() - clicked;
  await expect(modals).toHaveCount(1);
  expect(secondVisible, `the next question waited ${secondVisible} ms -- the held post-auth work, not its own`)
    .toBeLessThan(HOLD_MS - 1000);

  const secondModal = await modal.elementHandle();
  const answeredSecond = await answer(page, id, modal);
  await detached(page, secondModal, HOLD_MS);
  const secondRetired = Date.now() - answeredSecond;

  // Nothing comes back: no stale modal, no second copy of either question.
  await page.waitForTimeout(3500);
  await expect(modals).toHaveCount(0);
  expect((await detail(page, id)).input_required ?? null).toBeNull();
  await expect.poll(async () => (await detail(page, id)).status, {timeout: 40000}).toBe('consolidated');
  test.info().annotations.push({type: 'latency_ms', description: JSON.stringify({
    first_challenge_created: firstCreated, first_modal_visible: firstVisible, first_modal_retired: firstRetired,
    next_challenge_created: secondCreated, next_modal_visible: secondVisible, next_modal_retired: secondRetired})});
});

test('a writer that keeps vanishing fails over to its verified alternate instead of waiting for retry', async ({page}) => {
  await setup(page);
  const id = await add(page, ['https://vanish.contract.test/retry.bin']);
  await expect.poll(async () => ((await detail(page, id)).files || []).length, {timeout: 30000}).toBe(1);
  const alternate = await add(page, ['https://alive.contract.test/retry.bin']);
  await expect.poll(async () => (await detail(page, alternate)).status, {timeout: 30000}).toBe('consolidated');

  await page.goto(`${BASE}/`);
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
  const row = page.locator(`#t-tbody tr[data-torrent-id="${id}"]`);
  const started = Date.now();
  let waiting = 0;
  let last = Date.now();
  const listedStatus = async () => {
    const listed = await (await page.request.get(`${BASE}/api/torrents?limit=500`)).json();
    return (listed.items.find(item => item.id === id) || {}).status ?? null;
  };
  await expect.poll(async () => {
    const now = Date.now();
    const status = await listedStatus();
    if (status === 'waiting_for_retry') waiting += now - last;
    last = now;
    return status;
  }, {timeout: 45000, message: 'the vanishing route was never failed over'}).toBe('completed');
  const selected = (await detail(page, id)).files[0].acquisition_candidates.find(item => item.is_selected);
  expect(selected.source_label).toBe('alive.contract.test');
  await expect(row).toBeVisible();
  await expect(row).not.toContainText(/Waiting for Retry/i);
  expect(waiting, `the row sat ${waiting} ms in Waiting for Retry`).toBeLessThan(20000);
  test.info().annotations.push({type: 'failover_ms', description: JSON.stringify({
    completed_after: Date.now() - started, waiting_for_retry: waiting})});
});

test('a contributor with an unverified source settles into the canonical transfer and stays in its history', async ({page}) => {
  await setup(page);
  const seedId = await seed(page);
  const id = await add(page, ['https://good.contract.test/multi.bin', 'https://slow.contract.test/multi.bin']);
  await expect.poll(async () => (await detail(page, id)).status,
    {timeout: 30000, message: 'the contributor never settled'}).toBe('consolidated');
  const listed = await (await page.request.get(`${BASE}/api/torrents?limit=500`)).json();
  expect(listed.items.map(item => item.id)).not.toContain(id);

  await page.goto(`${BASE}/`);
  await page.locator('#sidebar .nav-item[data-view="torrents"]').click();
  await expect(page.locator('#view-torrents')).toHaveClass(/\bactive\b/);
  await expect(page.locator(`#t-tbody tr[data-torrent-id="${seedId}"]`)).toBeVisible({timeout: 15000});
  await expect(page.locator(`#t-tbody tr[data-torrent-id="${id}"]`)).toHaveCount(0);

  await page.evaluate(transferId => showDetail(transferId), seedId);
  const unverified = page.locator('.dp-detail-route-row[data-route-relation="unverified"]');
  await expect(unverified).toHaveCount(1, {timeout: 15000});
  await expect(unverified.locator('.dp-detail-route-identity')).toContainText('slow.contract.test');
  await expect(unverified.locator('.dp-detail-route-outcome')).toHaveText('Unverified');
});
