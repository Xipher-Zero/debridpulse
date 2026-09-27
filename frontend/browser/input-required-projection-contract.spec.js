const { test, expect } = require('@playwright/test');

/* DP 1.0.13 INPUT_REQUIRED projection-to-modal contract, end to end.
 *
 * Nothing here is fabricated. The test-composition server
 * (backend/tests/browser_contract_app.py, run from the candidate image) is the
 * real application with one locked in-memory HTTPS host: submitting a link to
 * it drives the real engine to a real durable executor challenge. The modal
 * must discover that challenge through the REAL bounded
 * `GET /api/torrents?status=input_required` read model -- the seam production
 * transfer 399 fell through, which the fixture-driven modal specs cannot see --
 * and the answer must go through the real `/torrents/{id}/input` endpoint.
 *
 * This file writes no shared settings key and uses its own server. */

const BASE = process.env.DP_CONTRACT_BASE_URL;
const LINK = 'https://locked.contract.test/modal-contract.bin';
const USERNAME = 'contract-operator';
const PASSWORD = 'contract-password';

test('a real backend challenge opens the modal through the bounded list and resolves through the real input endpoint',
  async ({page}) => {
    expect(BASE, 'DP_CONTRACT_BASE_URL must name the test-composition server').toBeTruthy();
    await page.route('https://fonts.googleapis.com/**', route =>
      route.fulfill({status: 200, contentType: 'text/css', body: ''}));

    const added = await page.request.post(`${BASE}/api/links/add`, {data: {links: [LINK]}});
    expect(added.ok(), 'the real admission path rejected the link').toBeTruthy();
    const id = (await added.json()).id;

    const detail = async () => (await page.request.get(`${BASE}/api/torrents/${id}`)).json();
    await expect.poll(async () => (await detail()).input_required?.reason ?? null,
      {timeout: 30000, message: 'the engine never raised the challenge'}).toBe('auth_required');

    // The bounded list the modal reads carries the same canonical challenge.
    const listed = await (await page.request.get(`${BASE}/api/torrents?status=input_required&limit=5000`)).json();
    const item = listed.items.find(entry => entry.id === id);
    expect(item, 'the challenged transfer is missing from the bounded list').toBeTruthy();
    expect(item.input_required).toEqual((await detail()).input_required);

    await page.goto(`${BASE}/`);
    const modal = page.locator(`[data-dp-input-required-modal][data-dp-auth-transfer-id="${id}"]`);
    await expect(modal).toBeVisible({timeout: 15000});
    await expect(modal.locator('#dp-auth-required-title')).toHaveText('Authentication Required');
    await expect(modal.locator('[data-dp-auth-username]')).toBeVisible();
    await expect(modal.locator('[data-dp-auth-secret]')).toBeVisible();

    const submitted = page.waitForResponse(response =>
      response.url().endsWith(`/api/torrents/${id}/input`) && response.request().method() === 'POST');
    await modal.locator('[data-dp-auth-username]').fill(USERNAME);
    await modal.locator('[data-dp-auth-secret]').fill(PASSWORD);
    await modal.locator('[data-dp-auth-continue]').click();
    expect((await submitted).ok(), 'the real input endpoint rejected the answer').toBeTruthy();

    await expect(modal).toHaveCount(0, {timeout: 20000});
    await expect.poll(async () => (await detail()).input_required ?? null,
      {timeout: 20000, message: 'the challenge never resolved'}).toBeNull();
  });
