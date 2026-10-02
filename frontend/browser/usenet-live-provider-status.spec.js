const { test, expect } = require('@playwright/test');

/* DP 1.0.13 -- Provider Status follows the managed Usenet runtime live.
 *
 * The reported defect: enable Usenet, the row appears non-green while the
 * managed service starts, the service becomes healthy -- and the row stays
 * stale until some unrelated GUI action happens to refresh it.
 *
 * This file uses its own candidate server (DP_USENET_BASE_URL; Browser
 * Runtime's dp-browser-usenet), never the shared backend. Its configuration is
 * seeded through the image's own canonical functions: Usenet DISABLED, one
 * news server, and that server's durable verification evidence. `configured`
 * and `verified` are therefore canonically true, and runtime health is the only
 * thing between the row and green.
 *
 * The page clock is installed and then held, so no page timer can fire -- in
 * particular not the existing 60 s fallback refresh. After the operator's one
 * action (the Enable toggle) the test performs no GUI action at all: a green
 * row can only come from the backend announcing the transition and the
 * Provider Status owner re-observing the canonical status endpoint. */

const BASE = process.env.DP_USENET_BASE_URL;
const row = page => page.locator('#provider-status-list [data-provider-id="usenet"]');
const json = async response => (await response).json();

test('enabled Usenet turns green when its managed service converges, with no GUI action after the toggle',
  async ({page}) => {
    test.setTimeout(240_000);
    expect(BASE, 'DP_USENET_BASE_URL must name the dedicated Usenet server').toBeTruthy();
    await page.route('https://fonts.googleapis.com/**', route =>
      route.fulfill({status: 200, contentType: 'text/css', body: ''}));

    // Begin disabled with the managed service stopped: the seeded state, and
    // re-established here so a bounded rerun starts from the same place.
    const disabled = await page.request.patch(`${BASE}/api/integrations/usenet/configuration`,
      {data: {enabled: false}});
    expect(disabled.ok(), 'Usenet could not be disabled').toBeTruthy();
    await expect.poll(async () => (await json(page.request.get(`${BASE}/api/usenet/drift`))).reachable,
      {timeout: 90_000, message: 'the managed service never stopped once Usenet was disabled'}).toBe(false);
    const seeded = (await json(page.request.get(`${BASE}/api/settings`))).integrations.usenet;
    expect(seeded).toMatchObject({enabled: false, configured: true, verified: true});

    await page.clock.install();
    await page.goto(`${BASE}/`);
    await expect(page.locator('#provider-status-list [data-provider-state="checking"]')).toHaveCount(0);
    await page.clock.pauseAt(new Date(Date.now() + 1000));
    // Disabled Usenet is absent from Provider Status (accepted composition).
    await expect(row(page)).toHaveCount(0);

    // Every state the row takes from now on, recorded without touching the page.
    await page.evaluate(() => {
      window.__usenetRow = [];
      const host = document.getElementById('provider-status-list');
      const record = () => {
        const state = host.querySelector('[data-provider-id="usenet"]')?.dataset.providerState ?? 'absent';
        if (window.__usenetRow.at(-1) !== state) window.__usenetRow.push(state);
      };
      new MutationObserver(record).observe(host, {childList: true, subtree: true, attributes: true});
      record();
    });

    // The operator's one action.
    await page.locator('#sidebar .nav-item[data-view="settings"]').click();
    const toggle = page.locator('[data-integration-enabled="usenet"]');
    await expect(toggle).not.toBeChecked();
    await page.locator('label[for="dp-settings-integration-usenet-enabled"]').click();
    await expect(toggle).toBeChecked();

    // The row appears, truthfully not ready, while the service is not healthy.
    await expect(row(page)).toHaveCount(1);
    const first = await page.evaluate(() => window.__usenetRow.find(state => state !== 'absent'));
    expect(first, 'the row did not appear in a non-ready state').toBeTruthy();
    expect(first).not.toBe('healthy');

    // The managed service converges in the background; the canonical endpoint
    // reports it (read here by the test, never by the page).
    await expect.poll(async () => (await json(page.request.get(`${BASE}/api/integration-status/usenet`))).state,
      {timeout: 150_000, message: 'the managed service never became healthy'}).toBe('healthy');

    // No timer can fire and nothing was clicked: only the live signal can do this.
    await expect(row(page)).toHaveAttribute('data-provider-state', 'healthy', {timeout: 30_000});
    const history = await page.evaluate(() => window.__usenetRow);
    expect(history[0]).toBe('absent');
    expect(history.at(-1)).toBe('healthy');
  });
