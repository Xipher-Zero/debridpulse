const { test, expect } = require('@playwright/test');

/* DP 1.0.13 INPUT_REQUIRED authentication latency, end to end.
 *
 * The test-composition server (backend/tests/browser_contract_app.py) is the
 * real application. An open host holds an in-progress canonical copy; an
 * equivalent on a locked host needs the operator's login before its evidence
 * can be sampled -- an evidence-origin challenge. Once the locked sample has
 * answered, the rest of that materialization decision is deliberately held
 * (EVIDENCE_HOLD_SECONDS, 8 s). The modal must follow the AUTHENTICATION
 * outcome: close, or show the failure, well inside that hold -- never wait for
 * the decision. No client-side timing or optimism is involved: the existing
 * modal polls the transfer and reacts to the durable question alone. */

const BASE = process.env.DP_CONTRACT_BASE_URL;
const USERNAME = 'contract-operator';
const PASSWORD = 'contract-password';
const HOLD_MS = 8000;
let seedId = null;

async function challenged(page, path) {
  expect(BASE, 'DP_CONTRACT_BASE_URL must name the test-composition server').toBeTruthy();
  await page.route('https://fonts.googleapis.com/**', route =>
    route.fulfill({status: 200, contentType: 'text/css', body: ''}));
  const add = async link => {
    const added = await page.request.post(`${BASE}/api/links/add`, {data: {links: [link]}});
    expect(added.ok(), `the real admission path rejected ${link}`).toBeTruthy();
    return (await added.json()).id;
  };
  const detail = async id => (await page.request.get(`${BASE}/api/torrents/${id}`)).json();
  // One in-progress canonical copy serves both tests (the composition has one
  // execution slot, which it keeps).
  seedId = seedId ?? await add('https://seed.contract.test/evidence.bin');
  await expect.poll(async () => (await detail(seedId)).status, {timeout: 30000}).toBe('downloading');
  const id = await add(`https://evidence.contract.test${path}`);
  await expect.poll(async () => (await detail(id)).input_required?.origin ?? null,
    {timeout: 30000, message: 'the engine never raised the evidence challenge'}).toBe('evidence');
  await page.goto(`${BASE}/`);
  const modal = page.locator(`[data-dp-input-required-modal][data-dp-auth-transfer-id="${id}"]`);
  await expect(modal).toBeVisible({timeout: 15000});
  return {id, modal, detail: () => detail(id)};
}

async function answer(page, id, modal, password) {
  const submitted = page.waitForResponse(response =>
    response.url().endsWith(`/api/torrents/${id}/input`) && response.request().method() === 'POST');
  await modal.locator('[data-dp-auth-username]').fill(USERNAME);
  await modal.locator('[data-dp-auth-secret]').fill(password);
  const clicked = Date.now();
  await modal.locator('[data-dp-auth-continue]').click();
  expect((await submitted).ok(), 'the real input endpoint rejected the answer').toBeTruthy();
  return clicked;
}

test('an accepted login closes the modal while the materialization decision is still held', async ({page}) => {
  const {id, modal, detail} = await challenged(page, '/evidence.bin');
  const clicked = await answer(page, id, modal, PASSWORD);
  await expect(modal.locator('[data-dp-auth-continue]')).toHaveText(/Authenticating/);
  await expect(modal).toHaveCount(0, {timeout: HOLD_MS});
  const closedAfter = Date.now() - clicked;
  // The decision the login unblocked is still running: nothing consolidated yet.
  const now = await detail();
  expect(now.input_required ?? null).toBeNull();
  expect(now.status).not.toBe('consolidated');
  expect(closedAfter, `the modal waited ${closedAfter} ms -- the held decision, not the login`).toBeLessThan(HOLD_MS / 2);
  // ...and it still completes its materialization decision correctly.
  await expect.poll(async () => (await detail()).status, {timeout: 30000}).toBe('consolidated');
});

test('a refused login returns the modal to the question with the failure at once', async ({page}) => {
  const {id, modal, detail} = await challenged(page, '/refused/evidence.bin');
  const before = (await detail()).input_required.generation;
  const clicked = await answer(page, id, modal, 'not-the-password');
  await expect(modal.locator('[data-dp-auth-error]')).toHaveText(
    'Authentication failed. Check your credentials and try again.', {timeout: HOLD_MS});
  const shownAfter = Date.now() - clicked;
  await expect(modal.locator('[data-dp-auth-continue]')).not.toHaveText(/Authenticating/);
  expect((await detail()).input_required.generation).toBe(before + 1);
  expect(shownAfter, `the failure waited ${shownAfter} ms`).toBeLessThan(HOLD_MS / 2);
  // The reissued generation is answerable.
  await answer(page, id, modal, PASSWORD);
  await expect(modal).toHaveCount(0, {timeout: HOLD_MS});
  await expect.poll(async () => (await detail()).status, {timeout: 30000}).toBe('consolidated');
});
